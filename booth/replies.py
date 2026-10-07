"""Two-way Telegram: the Mac's side of Booth's reply service (worker/, a Cloudflare Worker).

Telegram sends Dan's messages to the reply service, which answers them with Claude. The
service can't see this Mac and the repo is public, so league data reaches it only from here.
Each scheduled run (`booth run due`):

1. pulls what Dan told Booth in chat: roster moves he says he made, notes for future reports,
   and requests to rerun a report. Roster moves are applied to his roster copy
   (config/manual_roster.local.json); notes are saved to state/notes.json, which reports read
   through get_league_context. Each result goes back to the service, and a move that couldn't
   be applied is also sent to Dan.
2. runs any report Dan asked for (a request waits while the Mac sleeps, up to RERUN_MAX_AGE),
3. uploads Booth's current picture (roster, available players, matchup, FAAB market, this
   week's reports and schedule) whenever it has changed, so answers come from fresh data.

The service's address comes from the bot's webhook (set by the deploy workflow), or
BOOTH_REPLIES_URL. Until a webhook is set, all of this is skipped quietly.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from booth.config import ROOT
from booth.state import state_dir

EASTERN = ZoneInfo("America/New_York")
URL_RECHECK = timedelta(hours=6)  # how long a looked-up service address is trusted
RERUN_MAX_AGE = timedelta(hours=6)  # an older rerun request (the Mac slept through it) is dropped
RUNS = ("tue", "thu", "sat", "sun")
SLOTS = {"QB", "RB", "WR", "TE", "W/R/T", "K", "DEF", "BN", "IR"}
NOT_SET_UP = "replies: not set up"


class RepliesError(RuntimeError):
    pass


def secret(purpose: str) -> str:
    """A secret derived from the bot token, matching the worker's deriveSecret."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    return hmac.new(token.encode(), f"booth-{purpose}".encode(), hashlib.sha256).hexdigest()


# --- Small local state: the service address and what was last uploaded ---------------------

def _state_file():
    return state_dir() / "replies.json"


def _load_state() -> dict:
    try:
        data = json.loads(_state_file().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(data: dict) -> None:
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def service_url(now: datetime | None = None) -> str:
    """The reply service's base address, or "" when it isn't set up.

    The deploy workflow points the bot's webhook at <service>/telegram, so Telegram itself
    says where the service is; that answer is kept for URL_RECHECK.
    """
    from booth.deliver import DeliveryError, _telegram

    load_dotenv(ROOT / ".env")
    if os.getenv("BOOTH_REPLIES_URL"):
        return os.environ["BOOTH_REPLIES_URL"].rstrip("/")
    if not os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
        return ""
    now = now or datetime.now(timezone.utc)
    st = _load_state()
    try:
        fresh = now - datetime.fromisoformat(st["url_checked_at"]) < URL_RECHECK
    except (KeyError, TypeError, ValueError):
        fresh = False
    if fresh:
        return st.get("url", "")
    try:
        hook = str(_telegram("getWebhookInfo").get("url") or "")
    except DeliveryError:
        return st.get("url", "")  # Telegram is down: keep using the last known address
    url = re.sub(r"/telegram/?$", "", hook) if hook.startswith("https://") else ""
    st.update(url=url, url_checked_at=now.isoformat())
    _save_state(st)
    return url


def _request(method: str, path: str, payload: dict | None = None, params: dict | None = None) -> dict:
    base = service_url()
    if not base:
        raise RepliesError("the reply service isn't set up")
    try:
        resp = requests.request(method, base + path, json=payload, params=params, timeout=30,
                                headers={"Authorization": f"Bearer {secret('api')}"})
    except requests.RequestException as exc:
        raise RepliesError(f"couldn't reach the reply service: {type(exc).__name__}") from None
    if resp.status_code != 200:
        raise RepliesError(f"the reply service returned HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError:
        raise RepliesError("the reply service sent back something that isn't JSON") from None


def chats() -> list[dict]:
    """Everyone who has messaged the bot (newest first), with their last message."""
    return _request("GET", "/chats").get("chats", [])


# --- Notes: what Dan told Booth in chat to keep in mind --------------------------------------

def _notes_file():
    return state_dir() / "notes.json"


def dan_notes() -> list[dict]:
    """Dan's standing notes from Telegram, oldest first, for reports to follow."""
    try:
        data = json.loads(_notes_file().read_text())
        return [n for n in data.get("notes", []) if isinstance(n, dict) and n.get("text")]
    except (OSError, ValueError, AttributeError):
        return []


def _save_notes(notes: list[dict]) -> None:
    path = _notes_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"notes": [{"text": n["text"], "at": n.get("at", "")} for n in notes]}, indent=2) + "\n")
    tmp.replace(path)


# --- Roster moves Dan reports in chat ---------------------------------------------------------

def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower().replace(" jr", "").replace(" sr", ""))


def _find(players: list[dict], name: str) -> int | None:
    """The roster index for a name: an exact match, else a single loose one (last name, "Bagent")."""
    want = _norm(name)
    if not want:
        return None
    exact = [i for i, p in enumerate(players) if _norm(p.get("name", "")) == want]
    if len(exact) == 1:
        return exact[0]
    loose = [i for i, p in enumerate(players) if want in _norm(p.get("name", ""))
             or _norm(p.get("name", "").split(" ")[-1]) == want]
    return loose[0] if len(loose) == 1 else None


def apply_move(move: dict, now: datetime) -> tuple[bool, str]:
    """Apply one roster move to Dan's roster copy. Returns (applied, what happened)."""
    from booth import manual

    action = move.get("action")
    player = str(move.get("player") or "").strip()
    path = manual.MANUAL_ROSTER_LOCAL
    if not path.exists():
        # Start from the seeded list (tracked in the public repo, so never edited in place).
        seed = json.loads(manual.MANUAL_ROSTER.read_text())
        seed.pop("_note", None)
        data = seed
    else:
        data = json.loads(path.read_text())
    players = data.get("players")
    if not isinstance(players, list):
        return False, "the roster copy has no player list"

    if action == "add":
        position = str(move.get("position") or "").upper()
        if not player or position not in manual.POSITIONS:
            return False, f"add {player or '?'}: need the player's name and position"
        if any(_norm(p.get("name", "")) == _norm(player) for p in players):
            return False, f"{player} is already on the roster copy"
        slot = str(move.get("slot") or "BN").upper()
        players.append({"name": player, "position": position, "nfl_team": str(move.get("nfl_team") or "").upper(),
                        "status": "", "slot": slot if slot in SLOTS else "BN"})
        spent = move.get("faab_spent") or 0
        if isinstance(spent, (int, float)) and spent > 0 and isinstance(data.get("faab_remaining"), (int, float)):
            data["faab_remaining"] = max(0, data["faab_remaining"] - spent)
        if isinstance(data.get("open_roster_spots"), int) and slot != "IR":
            data["open_roster_spots"] = max(0, data["open_roster_spots"] - 1)
        done = f"added {player} ({position}{', $' + str(spent) if spent else ''})"
    elif action == "drop":
        i = _find(players, player)
        if i is None:
            return False, f"couldn't find {player or '?'} on the roster copy"
        gone = players.pop(i)
        if isinstance(data.get("open_roster_spots"), int) and gone.get("slot") != "IR":
            data["open_roster_spots"] += 1
        done = f"dropped {gone['name']}"
    elif action == "slot":
        i = _find(players, player)
        slot = str(move.get("slot") or "").upper()
        if i is None:
            return False, f"couldn't find {player or '?'} on the roster copy"
        if slot not in SLOTS:
            return False, f"move {players[i]['name']}: \"{slot}\" isn't a roster slot"
        players[i]["slot"] = slot
        done = f"moved {players[i]['name']} to {slot}"
    elif action == "faab":
        amount = move.get("faab_remaining")
        if not isinstance(amount, (int, float)) or isinstance(amount, bool) or not 0 <= amount <= 1000:
            return False, "set FAAB: need the dollars left"
        data["faab_remaining"] = amount
        done = f"set FAAB left to ${amount:g}"
    else:
        return False, f"unknown roster move \"{action}\""

    when = now.astimezone(EASTERN).strftime("%b %-d %-I:%M %p ET")
    data.setdefault("chat_updates", []).append(f"{when}: {done} (Dan, in Telegram)")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)
    return True, done


# --- One sync, as run by `booth run due` -------------------------------------------------------

def _chat() -> str:
    from booth.deliver import telegram_chat_id

    chat = telegram_chat_id()
    return str(chat) if chat else ""


def pull(now: datetime | None = None, send_fn=None) -> tuple[str, list[dict]]:
    """Fetch and apply what Dan said in chat. Returns (outcome, rerun requests to run)."""
    from booth.jobs import lock

    now = now or datetime.now(timezone.utc)
    if not service_url(now):
        return NOT_SET_UP, []
    chat = _chat()
    if not chat:
        return "replies: Telegram isn't connected", []
    got = _request("GET", "/changes", params={"chat": chat})
    _save_notes([n for n in got.get("notes", []) if isinstance(n, dict) and n.get("text")])
    changes = [c for c in got.get("changes", []) if isinstance(c, dict) and "id" in c]
    if not changes:
        return "replies: nothing new", []
    results, reruns, failed = [], [], []
    with lock():
        for c in changes:
            data = c.get("data") if isinstance(c.get("data"), dict) else {}
            if c.get("kind") == "rerun":
                reruns.append(c)  # reported once it has run
                continue
            if c.get("kind") != "roster":
                results.append({"id": c["id"], "ok": False, "detail": f"unknown change \"{c.get('kind')}\""})
                continue
            try:
                ok, detail = apply_move(data, now)
            except (OSError, ValueError, TypeError) as exc:
                ok, detail = False, f"the roster copy couldn't be updated ({str(exc)[:80]})"
            results.append({"id": c["id"], "ok": ok, "detail": detail})
            if not ok:
                failed.append(detail)
    if results:
        _request("POST", "/changes/ack", {"chat": chat, "results": results})
    if failed and send_fn:
        send_fn("Booth couldn't update your roster copy from chat: " + "; ".join(failed)
                + ". Tell Booth again with the exact name, or send a fresh team page screenshot.")
    applied = sum(r["ok"] for r in results)
    return f"replies: applied {applied} of {len(results)} roster moves, {len(reruns)} reruns asked", reruns


def run_reruns(reruns: list[dict], now: datetime | None = None, generate_fn=None, send_fn=None) -> list[str]:
    """Build and send each report Dan asked for in chat, then tell the service how it went."""
    from booth.jobs import _stay_awake, lock, record_manual_delivery
    from booth.report import RUN_LABELS, generate

    if not reruns:
        return []
    generate_fn = generate_fn or generate
    if send_fn is None:
        from booth.deliver import send as send_fn
    now = now or datetime.now(timezone.utc)
    outcomes, results = [], []
    done_runs: set[str] = set()
    for c in reruns:
        data = c.get("data") if isinstance(c.get("data"), dict) else {}
        run = data.get("run")
        try:
            asked = datetime.fromisoformat(str(c.get("at")).replace("Z", "+00:00"))
        except ValueError:
            asked = now
        if run not in RUNS:
            results.append({"id": c["id"], "ok": False, "detail": f"\"{run}\" isn't a report"})
            continue
        if run in done_runs:
            results.append({"id": c["id"], "ok": True, "detail": "same report as another request, sent once"})
            continue
        if now - asked > RERUN_MAX_AGE:
            when = asked.astimezone(EASTERN).strftime("%a %-I:%M %p")
            detail = f"skipped: the Mac was asleep from {when} ET until now"
            results.append({"id": c["id"], "ok": False, "detail": detail})
            try:
                send_fn(f"Your Mac was asleep, so the {RUN_LABELS[run]} you asked for at {when} ET didn't run. "
                        "Ask again if you still want it.")
            except Exception:  # noqa: BLE001 - the service hears about it either way
                pass
            continue
        try:
            with lock():
                _stay_awake()
                result = generate_fn(run, now=now.astimezone(EASTERN))
                message = result["message"]
                if os.getenv("BOOTH_DRY_RUN") == "1":
                    message = f"[DRY RUN] {message}"
                send_fn(f"(The rerun you asked for in chat.)\n\n{message}")
                record_manual_delivery(run, result["snapshot"]["week"])
            done_runs.add(run)
            results.append({"id": c["id"], "ok": True, "detail": "sent"})
            outcomes.append(f"reran {run}")
        except Exception as exc:  # noqa: BLE001 - say what failed instead of dropping the request
            detail = f"failed: {str(exc)[:150]}"
            results.append({"id": c["id"], "ok": False, "detail": detail})
            outcomes.append(f"failed rerun {run}: {str(exc)[:100]}")
            try:
                send_fn(f"Booth couldn't rerun the {RUN_LABELS[run]} you asked for: {str(exc)[:200]}")
            except Exception:  # noqa: BLE001
                pass
    chat = _chat()
    if results and chat:
        _request("POST", "/changes/ack", {"chat": chat, "results": results})
    return outcomes


def _reports(season: int, week: int) -> list[dict]:
    from booth.report import REPORTS

    out = []
    for w in (week - 1, week):
        for run in RUNS:
            path = REPORTS / f"{season}-wk{w:02d}-{run}.txt"
            if path.exists():
                out.append({"week": w, "run": run, "text": path.read_text()[:6000]})
    return out


def _report_times(season: int, week: int, games: list[dict]) -> dict:
    from booth.jobs import _bookkeeping, scheduled_at

    jobs = _bookkeeping(season, week)
    out = {}
    for run in RUNS:
        try:
            at = scheduled_at(run, week, games).astimezone(EASTERN).strftime("%a %b %-d %-I:%M %p ET")
        except Exception:  # noqa: BLE001 - a missing week in the schedule just drops the time
            at = None
        out[run] = {"scheduled": at, "delivered_at": (jobs.get(run) or {}).get("delivered_at")}
    return out


def build_context(now: datetime | None = None) -> dict:
    """What the reply service answers from: the same data the reports use, as of now."""
    from booth import mcp_server
    from booth.context import league_config, strategy_text
    from booth.data import nflverse

    now = now or datetime.now(timezone.utc)

    def safe(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - one missing piece shouldn't stop the rest
            return {"error": str(exc)[:200]}

    games = safe(nflverse.games)
    week = nflverse.current_week(games, now.astimezone(EASTERN).date()) if isinstance(games, list) else None
    ctx = {
        "season": nflverse.SEASON,
        "week": week,
        "league": safe(league_config),
        "strategy": safe(strategy_text),
        "roster": safe(mcp_server.get_my_roster),
        "matchup": safe(mcp_server.get_matchup),
        "available_players": safe(mcp_server.get_free_agents, limit=60),
        "faab_market": safe(mcp_server.faab_market) or {"note": "No copy of the league's FAAB history yet."},
        "dry_run": os.getenv("BOOTH_DRY_RUN") == "1",
    }
    if week:
        ctx["schedule"] = safe(nflverse.schedule, week, games)
        ctx["reports"] = safe(_reports, nflverse.SEASON, week)
        ctx["report_times"] = safe(_report_times, nflverse.SEASON, week, games)
    return ctx


def upload(now: datetime | None = None, force: bool = False) -> str:
    """Send the current picture to the service when it has changed since the last upload."""
    now = now or datetime.now(timezone.utc)
    if not service_url(now):
        return NOT_SET_UP
    chat = _chat()
    if not chat:
        return "replies: Telegram isn't connected"
    ctx = build_context(now)
    digest = hashlib.sha256(json.dumps([chat, ctx], sort_keys=True, default=str).encode()).hexdigest()
    st = _load_state()
    if not force and st.get("uploaded_hash") == digest:
        return "replies: up to date"
    built = now.astimezone(EASTERN).strftime("%a %b %-d %-I:%M %p ET")
    _request("POST", "/context", {"chat": chat, "built_at": built, "context": ctx})
    st = _load_state()  # service_url may have saved since
    st.update(uploaded_hash=digest, uploaded_at=now.isoformat())
    _save_state(st)
    return "replies: uploaded"


def sync(now: datetime | None = None, generate_fn=None, send_fn=None) -> list[str]:
    """Everything above, for `booth replies sync`: pull, rerun, upload. Returns outcomes."""
    outcome, reruns = pull(now, send_fn=send_fn)
    outcomes = [outcome, *run_reruns(reruns, now, generate_fn, send_fn)]
    outcomes.append(upload(now, force=True))
    return outcomes
