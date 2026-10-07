"""Two-way Telegram: Booth answers Dan's messages while the Mac is awake.

`booth listen` runs all the time under launchd (com.booth.listener). It collects Dan's
messages, answers them with `claude -p` on his Claude subscription (the same model, tools
and data as the reports), and replies in the Booth chat. Besides answering, Claude can:
- save or remove notes Dan wants kept (state/notes.json; reports read them through
  get_league_context),
- update Booth's copy of his roster when he says he made a move in Yahoo,
- start a fresh run of one of the reports (`booth rerun <run>`, in the background).

Messages arrive one of two ways:
- the mailbox (worker/, a free Cloudflare Worker with no AI in it), once the deploy has
  pointed the bot's webhook at it. It holds messages while the Mac sleeps and tells Dan
  Booth is asleep. Booth finds it from the bot's webhook address (or BOOTH_MAILBOX_URL).
- otherwise Telegram's own getUpdates, which holds messages for about a day but can't
  say anything while the Mac sleeps.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from booth.config import ROOT
from booth.state import state_dir

EASTERN = ZoneInfo("America/New_York")
RUNS = ("tue", "thu", "sat", "sun")
SLOTS = {"QB", "RB", "WR", "TE", "W/R/T", "K", "DEF", "BN", "IR"}
POLL_SECONDS = 10  # how often the Mac checks the mailbox (it says "asleep" after 2 quiet minutes)
URL_RECHECK = timedelta(hours=1)  # how often to look up where the bot's webhook points
HISTORY_TURNS = 16  # earlier messages, both sides, sent with each new one
KEEP_TURNS = 60
MAX_NOTES = 40
CHAT_TIMEOUT = 600
# Chat answers should come quickly and spend less of Dan's subscription than reports.
DEFAULT_CHAT_EFFORT = "medium"
SNAG = "Booth hit a snag answering that ({}). Try again in a minute?"


class ChatError(RuntimeError):
    pass


def _log(msg: str) -> None:
    try:
        logs = ROOT / "logs"
        logs.mkdir(exist_ok=True)
        stamp = datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S %Z")
        with (logs / "chat.log").open("a") as f:
            f.write(f"{stamp}  {msg}\n")
    except OSError:
        pass


def _read_json(path, default):
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def _write_json(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)  # never a half-written file


# --- The mailbox ---------------------------------------------------------------------------------

def secret(purpose: str) -> str:
    """A secret derived from the bot token, matching the mailbox's deriveSecret."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    return hmac.new(token.encode(), f"booth-{purpose}".encode(), hashlib.sha256).hexdigest()


def mailbox_url(now: datetime | None = None, refresh: bool = False) -> str:
    """The mailbox's address, or "" when the bot has no webhook (then Booth uses getUpdates).

    The deploy points the bot's webhook at <mailbox>/telegram, so Telegram says where it is.
    """
    from booth.deliver import DeliveryError, _telegram

    load_dotenv(ROOT / ".env")
    if os.getenv("BOOTH_MAILBOX_URL"):
        return os.environ["BOOTH_MAILBOX_URL"].rstrip("/")
    now = now or datetime.now(timezone.utc)
    path = state_dir() / "mailbox.json"
    saved = _read_json(path, {})
    try:
        fresh = now - datetime.fromisoformat(saved["checked_at"]) < URL_RECHECK
    except (KeyError, TypeError, ValueError):
        fresh = False
    if fresh and not refresh:
        return saved.get("url", "")
    try:
        hook = str(_telegram("getWebhookInfo").get("url") or "")
    except DeliveryError:
        return saved.get("url", "")  # Telegram is unreachable: keep the last known answer
    url = re.sub(r"/telegram/?$", "", hook) if hook.startswith("https://") else ""
    _write_json(path, {"url": url, "checked_at": now.isoformat()})
    return url


def _mailbox(method: str, path: str, payload: dict | None = None, params: dict | None = None) -> dict:
    base = mailbox_url()
    if not base:
        raise ChatError("the mailbox isn't set up")
    try:
        resp = requests.request(method, base + path, json=payload, params=params, timeout=20,
                                headers={"Authorization": f"Bearer {secret('api')}"})
    except requests.RequestException as exc:
        raise ChatError(f"couldn't reach the mailbox ({type(exc).__name__})") from None
    if resp.status_code != 200:
        raise ChatError(f"the mailbox returned HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError:
        raise ChatError("the mailbox sent back something that isn't JSON") from None


def mailbox_chats() -> list[dict]:
    """Everyone who has messaged the bot (newest first), with their last message."""
    return _mailbox("GET", "/chats").get("chats", [])


class MailboxSource:
    """Messages held by the mailbox. Every fetch also tells it the Mac is awake."""

    def __init__(self, chat: int):
        self.chat = chat

    def fetch(self) -> list[dict]:
        got = _mailbox("GET", "/poll", params={"chat": str(self.chat)}).get("messages", [])
        return [_message(m.get("updateId"), m.get("text"), m.get("replyTo"), m.get("receivedAt"))
                for m in got if isinstance(m, dict)]

    def heartbeat(self) -> None:
        self.fetch()  # results are fetched again once the current answer is done

    def done(self, ids: list[int]) -> None:
        _mailbox("POST", "/ack", {"ids": ids})

    def wait(self) -> None:
        time.sleep(POLL_SECONDS)


class TelegramSource:
    """Telegram's getUpdates, when there's no mailbox: long polls, so it waits on Telegram, not here."""

    def __init__(self, chat: int):
        self.chat = chat
        self.path = state_dir() / "telegram_offset.json"

    def fetch(self) -> list[dict]:
        from booth.deliver import _telegram

        offset = _read_json(self.path, {}).get("offset", 0)
        updates = _telegram("getUpdates", offset=offset, timeout=25, allowed_updates=["message"])
        out, others = [], []
        for u in updates:
            msg = u.get("message") or {}
            chat = msg.get("chat") or {}
            if chat.get("id") == self.chat:
                when = datetime.fromtimestamp(msg.get("date", time.time()), timezone.utc).isoformat()
                text = msg["text"] if isinstance(msg.get("text"), str) else attachment_text(msg)
                out.append(_message(u["update_id"], text, (msg.get("reply_to_message") or {}).get("text"), when))
            elif chat.get("type") == "private" and isinstance(msg.get("text"), str):
                others.append({"id": chat.get("id"), "name": chat.get("first_name", ""), "text": msg["text"][:200]})
        if others:  # so `booth telegram-setup --replace` can still find a new chat's code
            seen = _read_json(state_dir() / "telegram_others.json", [])
            _write_json(state_dir() / "telegram_others.json", [o for o in others if o not in seen][::-1][:20] + seen[:20])
        if updates and not out:  # nothing for Booth: confirm them so Telegram moves on
            self.done([u["update_id"] for u in updates])
        return out

    def heartbeat(self) -> None:
        pass

    def done(self, ids: list[int]) -> None:
        """Confirm everything up to the newest of these. Later updates are fetched again."""
        offset = _read_json(self.path, {}).get("offset", 0)
        if ids and max(ids) + 1 > offset:
            _write_json(self.path, {"offset": max(ids) + 1})

    def wait(self) -> None:
        pass  # getUpdates already waited


def attachment_text(msg: dict) -> str:
    """What Booth sees of a message it can't read (as the mailbox's attachmentText)."""
    kind = ("a photo" if "photo" in msg else "a file" if "document" in msg
            else "a voice message" if "voice" in msg or "audio" in msg else "a video" if "video" in msg
            else "a sticker" if "sticker" in msg else "something")
    caption = msg.get("caption")
    return f"[sent {kind} with the caption: {caption}]" if isinstance(caption, str) and caption else f"[sent {kind}]"


def _message(update_id, text, reply_to, received) -> dict:
    return {"id": int(update_id or 0), "text": str(text or ""), "reply_to": str(reply_to or ""), "at": str(received or "")}


# --- Notes and history ---------------------------------------------------------------------------

def _notes_file():
    return state_dir() / "notes.json"


def dan_notes() -> list[dict]:
    """Dan's standing notes from Telegram, oldest first: [{"id", "text", "at"}]."""
    data = _read_json(_notes_file(), {})
    return [n for n in data.get("notes", []) if isinstance(n, dict) and n.get("text")]


def change_notes(add: list[str], remove: list[int], now: datetime) -> list[str]:
    """Apply note changes. Returns problems worth telling Dan about."""
    data = _read_json(_notes_file(), {})
    notes = [n for n in data.get("notes", []) if isinstance(n, dict) and n.get("text")]
    next_id = max([data.get("next_id", 1), *(n.get("id", 0) + 1 for n in notes)])
    problems = []
    ids = {n["id"] for n in notes}
    for i in remove:
        if i not in ids:
            problems.append(f"there was no note {i} to remove")
    notes = [n for n in notes if n["id"] not in set(remove)]
    for text in add:
        text = str(text).strip()[:500]
        if not text:
            continue
        if len(notes) >= MAX_NOTES:
            problems.append(f"Booth keeps at most {MAX_NOTES} notes, so it didn't save \"{text[:60]}\"")
            continue
        notes.append({"id": next_id, "text": text, "at": now.astimezone(EASTERN).strftime("%Y-%m-%d")})
        next_id += 1
    if add or remove:
        _write_json(_notes_file(), {"next_id": next_id, "notes": notes})
    return problems


def _history_file():
    return state_dir() / "chat_history.json"


def history() -> list[dict]:
    return [t for t in _read_json(_history_file(), {}).get("turns", []) if isinstance(t, dict)]


def remember(said: str, reply: str, now: datetime) -> None:
    at = now.isoformat()
    turns = history() + [{"role": "user", "text": said, "at": at}, {"role": "assistant", "text": reply, "at": at}]
    _write_json(_history_file(), {"turns": turns[-KEEP_TURNS:]})


# --- Roster moves Dan reports ----------------------------------------------------------------------

def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\b(jr|sr|ii|iii)\b\.?", "", name.lower()))


def _find(players: list[dict], name: str) -> int | None:
    """The roster index for a name: an exact match, else a single loose one ("Bagent")."""
    want = _norm(name)
    if not want:
        return None
    exact = [i for i, p in enumerate(players) if _norm(p.get("name", "")) == want]
    if len(exact) == 1:
        return exact[0]
    loose = [i for i, p in enumerate(players)
             if want in _norm(p.get("name", "")) or _norm(str(p.get("name", "")).split(" ")[-1]) == want]
    return loose[0] if len(loose) == 1 else None


def apply_move(move: dict, now: datetime) -> tuple[bool, str]:
    """Apply one move Dan made in Yahoo to his roster copy. Returns (applied, what happened)."""
    from booth import manual

    action = move.get("action")
    player = str(move.get("player") or "").strip()
    path = manual.MANUAL_ROSTER_LOCAL
    if path.exists():
        data = json.loads(path.read_text())
    else:  # start from the seeded list, which is tracked in the public repo and never edited
        data = json.loads(manual.MANUAL_ROSTER.read_text())
        data.pop("_note", None)
    players = data.get("players")
    if not isinstance(players, list):
        return False, "the roster copy has no player list"

    if action == "add":
        position = str(move.get("position") or "").upper()
        if not player or position not in manual.POSITIONS:
            return False, f"adding {player or 'a player'} needs his name and position"
        if any(_norm(p.get("name", "")) == _norm(player) for p in players):
            return False, f"{player} is already on the roster copy"
        slot = str(move.get("slot") or "BN").upper()
        slot = slot if slot in SLOTS else "BN"
        players.append({"name": player, "position": position, "nfl_team": str(move.get("nfl_team") or "").upper(),
                        "status": "", "slot": slot})
        spent = move.get("faab_spent") or 0
        spent = spent if isinstance(spent, (int, float)) and not isinstance(spent, bool) and spent > 0 else 0
        if spent and isinstance(data.get("faab_remaining"), (int, float)):
            data["faab_remaining"] = max(0, data["faab_remaining"] - spent)
        if isinstance(data.get("open_roster_spots"), int) and slot != "IR":
            data["open_roster_spots"] = max(0, data["open_roster_spots"] - 1)
        done = f"added {player} ({position}{f', ${spent:g}' if spent else ''})"
    elif action == "drop":
        i = _find(players, player)
        if i is None:
            return False, f"couldn't find {player or 'that player'} on the roster copy"
        gone = players.pop(i)
        if isinstance(data.get("open_roster_spots"), int) and gone.get("slot") != "IR":
            data["open_roster_spots"] += 1
        done = f"dropped {gone['name']}"
    elif action == "slot":
        i = _find(players, player)
        slot = str(move.get("slot") or "").upper()
        if i is None:
            return False, f"couldn't find {player or 'that player'} on the roster copy"
        if slot not in SLOTS:
            return False, f"\"{slot}\" isn't a roster slot"
        players[i]["slot"] = slot
        done = f"moved {players[i]['name']} to {slot}"
    elif action == "faab":
        amount = move.get("faab_remaining")
        if not isinstance(amount, (int, float)) or isinstance(amount, bool) or not 0 <= amount <= 1000:
            return False, "setting FAAB needs the dollars left"
        data["faab_remaining"] = amount
        done = f"set FAAB left to ${amount:g}"
    else:
        return False, f"unknown roster move \"{action}\""

    when = now.astimezone(EASTERN).strftime("%b %-d %-I:%M %p ET")
    data.setdefault("chat_updates", []).append(f"{when}: {done} (Dan, in Telegram)")
    _write_json(path, data)
    return True, done


# --- Answering ------------------------------------------------------------------------------------

CHAT_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string", "description": "The message to send back to Dan."},
        "notes_add": {"type": "array", "items": {"type": "string"},
                      "description": "New notes to keep, one short sentence each. Usually empty."},
        "notes_remove": {"type": "array", "items": {"type": "integer"},
                         "description": "Ids of notes Dan took back or that are out of date. Usually empty."},
        "roster_moves": {
            "type": "array",
            "description": "Moves Dan says he has already made in Yahoo. Usually empty.",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "drop", "slot", "faab"]},
                    "player": {"type": "string"},
                    "position": {"type": "string", "description": "add: QB, RB, WR, TE, K or DEF"},
                    "nfl_team": {"type": "string"},
                    "slot": {"type": "string", "description": "slot: QB, RB, WR, TE, W/R/T, K, DEF, BN or IR"},
                    "faab_spent": {"type": "number"},
                    "faab_remaining": {"type": "number"},
                },
                "required": ["action"],
            },
        },
        "rerun": {"type": "string", "enum": ["none", *RUNS],
                  "description": "A report to build and send again now, only when Dan asks for one."},
    },
    "required": ["reply", "notes_add", "notes_remove", "roster_moves", "rerun"],
}


def _week(now: datetime) -> int | None:
    from booth.data import nflverse

    try:
        return nflverse.current_week(nflverse.games(), now.astimezone(EASTERN).date())
    except Exception:  # noqa: BLE001 - the prompt says "unknown" instead
        return None


def _recent_reports(week: int | None) -> str:
    from booth.data import nflverse
    from booth.report import REPORTS

    if not week:
        return "(none found)"
    found = []
    for w in (week - 1, week):
        for run in RUNS:
            path = REPORTS / f"{nflverse.SEASON}-wk{w:02d}-{run}.txt"
            if path.exists():
                found.append(f'<report week="{w}" run="{run}">\n{path.read_text().strip()[:5000]}\n</report>')
    return "\n".join(found[-4:]) or "(none yet this week)"


def _when(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(EASTERN).strftime("%a %-I:%M %p")
    except ValueError:
        return ""


def said_text(messages: list[dict]) -> str:
    """Dan's new message(s), with what each replied to. Several arrive together after the Mac slept."""
    parts = []
    for m in messages:
        text = m["text"]
        if m.get("reply_to"):
            quoted = m["reply_to"] if len(m["reply_to"]) <= 1500 else m["reply_to"][:1500] + " [...]"
            text = f"[Replying to this earlier message:\n{quoted}]\n{text}"
        if len(messages) > 1:
            text = f"({_when(m.get('at', '')) or 'earlier'}) {text}"
        parts.append(text)
    return "\n\n".join(parts)


def build_prompt(messages: list[dict], now: datetime) -> str:
    week = _week(now)
    notes = "\n".join(f"- [{n['id']}] ({n.get('at', '')}) {n['text']}" for n in dan_notes()) or "(none)"
    turns = history()[-HISTORY_TURNS:]
    convo = "\n\n".join(f"{'Dan' if t['role'] == 'user' else 'Booth'}: {t['text']}" for t in turns) or "(this is the first message)"
    waited = ""
    if messages and messages[0].get("at") and now - _parse(messages[0]["at"]) > timedelta(minutes=10):
        waited = ("Dan sent this while the Mac was asleep, so the answer is late: if any of it is out of date by now, "
                  "answer for now, and start with a few words acknowledging the wait.\n\n")
    fields = {
        "today": now.astimezone(EASTERN).strftime("%A %b %-d, %Y, %-I:%M %p"),
        "week": week or "unknown",
        "notes": notes,
        "reports": _recent_reports(week),
        "conversation": convo,
        "waited": waited,
        "message": said_text(messages),
    }
    text = (ROOT / "prompts" / "chat.md").read_text()
    for k, v in fields.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def _parse(iso: str) -> datetime:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def start_rerun(run: str) -> None:
    """Build and send a report in the background, so Booth keeps answering meanwhile."""
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    with (logs / "rerun.log").open("a") as out:
        subprocess.Popen([sys.executable, "-m", "booth.cli", "rerun", run], cwd=ROOT, stdin=subprocess.DEVNULL,
                         stdout=out, stderr=subprocess.STDOUT, start_new_session=True)


def answer(messages: list[dict], now: datetime | None = None, claude=None, rerun_fn=start_rerun) -> str:
    """Answer Dan's message(s) and carry out what Claude decided. Returns the reply to send."""
    from booth.report import RUN_LABELS, run_claude

    now = now or datetime.now(timezone.utc)
    claude = claude or (lambda prompt: run_claude(prompt, timeout=CHAT_TIMEOUT, schema=CHAT_SCHEMA,
                                                  effort=os.getenv("BOOTH_CHAT_EFFORT", DEFAULT_CHAT_EFFORT),
                                                  require_tools=False))
    out = claude(build_prompt(messages, now))["structured_output"]
    reply = str(out.get("reply") or "").strip()
    if not reply:
        raise ChatError("Claude's answer was empty")
    problems = change_notes([str(t) for t in out.get("notes_add") or []],
                            [i for i in out.get("notes_remove") or [] if isinstance(i, int)], now)
    for move in out.get("roster_moves") or []:
        if not isinstance(move, dict):
            continue
        try:
            ok, detail = apply_move(move, now)
        except (OSError, ValueError, TypeError) as exc:
            ok, detail = False, f"the roster copy couldn't be updated ({str(exc)[:80]})"
        _log(f"roster move {move}: {detail}")
        if not ok:
            problems.append(detail)
    run = out.get("rerun")
    if run in RUNS:
        try:
            rerun_fn(run)
            _log(f"started a rerun of {run}")
        except OSError as exc:
            problems.append(f"the {RUN_LABELS[run]} couldn't be started ({str(exc)[:80]})")
    if problems:
        reply += "\n\n(Booth couldn't do everything: " + "; ".join(problems) + ".)"
    remember(said_text(messages), reply, now)
    return reply


# --- The loop -------------------------------------------------------------------------------------

def _source(chat: int, refresh: bool = False):
    return MailboxSource(chat) if mailbox_url(refresh=refresh) else TelegramSource(chat)


def _keep_typing(stop: threading.Event, source) -> None:
    """While Claude works: show "typing..." in Telegram and keep telling the mailbox the Mac is awake."""
    from booth.deliver import DeliveryError, _telegram, telegram_chat_id

    while not stop.wait(0):
        try:
            _telegram("sendChatAction", chat_id=telegram_chat_id(), action="typing")
        except DeliveryError:
            pass
        try:
            source.heartbeat()
        except Exception:  # noqa: BLE001 - only a heartbeat
            pass
        stop.wait(4.5)  # Telegram shows "typing" for 5 seconds


def _answered() -> list[int]:
    return [i for i in _read_json(state_dir() / "chat_answered.json", []) if isinstance(i, int)]


def _outbox():
    return state_dir() / "chat_outbox.json"


def deliver_outbox(source, send_fn=None) -> bool:
    """Send an answer that's waiting to go out, then mark its messages done.

    Returns False if it still can't be sent (say, Wi-Fi isn't back after a wake): the answer
    stays in the outbox and its messages stay unacknowledged, so nothing is lost."""
    from booth.deliver import _send_telegram

    box = _read_json(_outbox(), {})
    if not box:
        return True
    try:
        (send_fn or _send_telegram)(box["reply"])
    except Exception as exc:  # noqa: BLE001 - tried again next round
        _log(f"couldn't send the answer to {box.get('ids')} yet: {exc}")
        return False
    _outbox().unlink(missing_ok=True)
    _log(f"answered {box.get('ids')}")
    _ack(source, box.get("ids") or [])
    return True


def _ack(source, ids: list[int]) -> None:
    try:
        source.done(ids)
    except Exception as exc:  # noqa: BLE001 - they're on the answered list, so they're acked next time
        _log(f"couldn't mark {ids} done yet: {exc}")


def handle(messages: list[dict], source, send_fn=None, answer_fn=answer) -> bool:
    """Answer one batch. Every message is answered exactly once: messages already answered (an
    acknowledgement that didn't get through) are only marked done, and an answer is saved
    before it's sent, so a failed send is retried instead of answering again. A failed answer
    still gets a reply ("hit a snag"), so a bad message can't loop forever."""
    answered = _answered()
    fresh = [m for m in messages if m["id"] not in answered]
    if not fresh:
        if _read_json(_outbox(), {}):
            return deliver_outbox(source, send_fn)  # their answer hasn't gone out yet
        _ack(source, [m["id"] for m in messages])
        return True
    stop = threading.Event()
    typing = threading.Thread(target=_keep_typing, args=(stop, source), daemon=True)
    typing.start()
    try:
        reply = answer_fn(fresh)
    except Exception as exc:  # noqa: BLE001 - tell Dan instead of going quiet
        _log(f"FAILED answering {[m['id'] for m in fresh]}: {exc}")
        reply = SNAG.format(str(exc)[:150])
    finally:
        stop.set()
    _write_json(_outbox(), {"ids": [m["id"] for m in messages], "reply": reply})
    _write_json(state_dir() / "chat_answered.json", (answered + [m["id"] for m in fresh])[-500:])
    return deliver_outbox(source, send_fn)


def listen(max_rounds: int | None = None) -> None:
    """Run forever (launchd restarts it if it stops): collect Dan's messages and answer them."""
    from booth.deliver import DeliveryError, telegram_chat_id

    load_dotenv(ROOT / ".env")
    rounds = 0
    source = None
    picked_at = 0.0
    failures = unsent = 0
    recheck = False
    while max_rounds is None or rounds < max_rounds:
        rounds += 1
        chat = telegram_chat_id()
        if not chat or not os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
            time.sleep(60)  # Telegram isn't connected yet
            continue
        if source is None or source.chat != chat or time.time() - picked_at > URL_RECHECK.total_seconds():
            source, picked_at = _source(chat, refresh=recheck), time.time()
            recheck = False
            _log(f"listening via {type(source).__name__}")
        try:
            messages = source.fetch()
            failures = 0
        except (ChatError, DeliveryError) as exc:
            failures += 1
            if failures in (1, 10) or failures % 100 == 0:
                _log(f"couldn't check for messages ({failures}x): {exc}")
            if "webhook" in str(exc).lower() or isinstance(source, MailboxSource):
                source, recheck = None, True  # the webhook may have changed: ask Telegram again
            time.sleep(min(60, 5 * failures))
            continue
        if not deliver_outbox(source):  # an earlier answer still waiting to go out
            unsent += 1
            time.sleep(min(60, 5 * unsent))
            continue
        unsent = 0
        if messages:
            try:
                handle(messages, source)
            except Exception as exc:  # noqa: BLE001 - never let one batch stop the listener
                _log(f"FAILED handling {[m['id'] for m in messages]}: {exc}")
                time.sleep(10)
        else:
            source.wait()
