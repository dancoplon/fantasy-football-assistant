"""Delivery: one swappable send() so the channel can change without touching the pipeline.

Channels:
- telegram: a message from Dan's own Booth bot. TELEGRAM_BOT_TOKEN lives in .env; the chat
  to send to is saved by `uv run booth telegram-setup`. If Telegram fails, the message goes
  by iMessage instead (when IMESSAGE_RECIPIENT is set), so a report is never lost to an outage.
- imessage: AppleScript on the Mac running Booth, to IMESSAGE_RECIPIENT (never in code).
- stdout: print it (no channel configured).
BOOTH_DELIVERY in .env forces one; otherwise Telegram whenever a bot token is set (unset
up, it fails loudly or falls back to iMessage, never silently to stdout), then iMessage.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

from booth.config import ROOT
from booth.state import state_dir

TELEGRAM_API = "https://api.telegram.org"
# Telegram's limit is 4096 characters per message; split longer reports at blank lines.
TELEGRAM_CHUNK = 4000
TOKEN_SHAPE = re.compile(r"\d+:[A-Za-z0-9_-]+")  # what @BotFather hands out
SETUP_CODE_TTL = timedelta(hours=23)  # Telegram keeps unread bot messages for 24 hours

# Message text and recipient are passed as arguments, never spliced into the script,
# so quotes or newlines in a report can't break (or inject into) the AppleScript.
SEND_SCRIPT = """
on run argv
    set theText to item 1 of argv
    set theRecipient to item 2 of argv
    tell application "Messages"
        set theService to 1st account whose service type = iMessage
        send theText to participant theRecipient of theService
    end tell
end run
"""

# A notification on the Mac itself. It's a Standard Additions command run by osascript,
# not an Apple Event to Messages, so it works when Messages can't send.
NOTIFY_SCRIPT = """
on run argv
    display notification (item 1 of argv) with title "Booth"
end run
"""


class DeliveryError(RuntimeError):
    pass


def channel() -> str:
    load_dotenv(ROOT / ".env")
    if os.getenv("BOOTH_DELIVERY"):
        return os.getenv("BOOTH_DELIVERY")
    if os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
        return "telegram"
    return "imessage" if os.getenv("IMESSAGE_RECIPIENT") else "stdout"


def send(text: str, via: str | None = None) -> str:
    """Deliver one message, by `via` if given, else the configured channel. Returns how it went out."""
    ch = via or channel()
    if ch == "stdout":
        print(text)
        return ch
    if ch == "telegram":
        try:
            _send_telegram(text)
            return ch
        except DeliveryError as exc:
            if via or not os.getenv("IMESSAGE_RECIPIENT"):
                raise
            reason = str(exc)[:200]  # already free of the token
            try:
                _send_imessage(f"(Sent by text because Telegram failed: {reason})\n\n{text}")
            except DeliveryError as im:
                raise DeliveryError(f"Telegram failed ({str(exc)[:150]}); the iMessage backup also failed "
                                    f"({str(im)[:150]})") from im
            return f"imessage (Telegram failed: {reason})"
    if ch == "imessage":
        _send_imessage(text)
        return ch
    raise DeliveryError(f"Unknown BOOTH_DELIVERY channel '{ch}'.")


def _send_imessage(text: str) -> None:
    recipient = os.getenv("IMESSAGE_RECIPIENT")
    if not recipient:
        raise DeliveryError("IMESSAGE_RECIPIENT is not set in .env.")
    if sys.platform != "darwin":
        raise DeliveryError("iMessage delivery only works on a Mac.")
    try:
        proc = subprocess.run(
            ["osascript", "-", text, recipient], input=SEND_SCRIPT, capture_output=True, text=True, timeout=120
        )
    except subprocess.TimeoutExpired as exc:
        raise DeliveryError(
            "Messages didn't respond within 2 minutes. macOS is probably waiting for someone to "
            "click OK on a 'control Messages' permission prompt."
        ) from exc
    if proc.returncode != 0:
        raise DeliveryError(f"Messages refused to send: {proc.stderr.strip()[:300]}")


# --- Telegram -----------------------------------------------------------------------

def _telegram_file():
    return state_dir() / "telegram.json"


def telegram_chat_id() -> int | None:
    try:
        return json.loads(_telegram_file().read_text())["chat_id"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _telegram(method: str, **params) -> dict | list:
    """Call the Bot API. Errors never include the token, which sits in the URL."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise DeliveryError("TELEGRAM_BOT_TOKEN is not set in .env.")
    if not TOKEN_SHAPE.fullmatch(token):
        raise DeliveryError("TELEGRAM_BOT_TOKEN in .env doesn't look like a bot token (numbers, a colon, then "
                            "letters and numbers, with no quotes or spaces). Paste it again from @BotFather.")
    try:
        resp = requests.post(f"{TELEGRAM_API}/bot{token}/{method}", json=params, timeout=30)
        body = resp.json()
    except (requests.RequestException, ValueError) as exc:
        msg = re.sub(r"/bot[^/\s'\"]*", "/bot<token>", str(exc).replace(token, "<token>"))
        raise DeliveryError(f"Telegram unreachable: {msg[:200]}") from None
    if not isinstance(body, dict) or not body.get("ok"):
        desc = body.get("description") if isinstance(body, dict) else body
        hint = " (check TELEGRAM_BOT_TOKEN in .env)" if resp.status_code in (401, 404) else ""
        raise DeliveryError(f"Telegram refused {method}: {str(desc)[:200]}{hint}")
    return body["result"]


def _chunks(text: str, size: int = TELEGRAM_CHUNK) -> list[str]:
    parts: list[str] = []
    while len(text) > size:
        cut = text.rfind("\n\n", 0, size)
        if cut <= 0:
            cut = text.rfind("\n", 0, size)
        if cut <= 0:
            cut = size
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    return parts + [text]


def _send_telegram(text: str) -> None:
    chat_id = telegram_chat_id()
    if not chat_id:
        raise DeliveryError("Telegram isn't connected yet: run `uv run booth telegram-setup` in the Booth folder.")
    for part in _chunks(text):
        _telegram("sendMessage", chat_id=chat_id, text=part, disable_web_page_preview=True)


def _write_json(path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data) + "\n")
    tmp.replace(path)  # never a half-written file


def _setup_code() -> str:
    """The code that proves a chat is Dan's: kept until it's used, renewed after a day."""
    path = state_dir() / "telegram_setup.json"
    try:
        saved = json.loads(path.read_text())
        if datetime.now(timezone.utc) - datetime.fromisoformat(saved["created"]) < SETUP_CODE_TTL:
            return saved["code"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    code = secrets.token_hex(4)
    _write_json(path, {"code": code, "created": datetime.now(timezone.utc).isoformat()})
    return code


def telegram_setup(replace: bool = False) -> tuple[bool, str]:
    """Connect the chat that opened the bot with this setup's code. Returns (connected, what to say).

    Anyone can find a bot and message it, so a chat counts only if it sent the code from
    the link Booth printed ("/start <code>"). Run it once for the link, open the link and
    tap Start, then run it again.
    """
    load_dotenv(ROOT / ".env")
    bot = _telegram("getMe")
    name = bot.get("username", "your bot")
    current = telegram_chat_id()
    if current and not replace:
        return True, (f"Booth is already connected to a chat with @{name}. To connect a different chat, "
                      "run `uv run booth telegram-setup --replace`.")
    code = _setup_code()
    found = None
    for update in _telegram("getUpdates", allowed_updates=["message"]):
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        if chat.get("type") == "private" and code in (msg.get("text") or ""):
            found = chat
    if not found:
        return False, (f"Almost done. On the phone with Telegram, open https://t.me/{name}?start={code} and tap "
                       f"Start (if there's no Start button, send the bot this message: {code}). Then run this again.")
    _write_json(_telegram_file(), {"chat_id": found["id"], "bot": name})
    (state_dir() / "telegram_setup.json").unlink(missing_ok=True)
    _telegram("sendMessage", chat_id=found["id"], text="Booth is connected. Your reports will arrive here from now on.")
    handle = f"(@{found['username']})" if found.get("username") else ""
    who = " ".join(x for x in (found.get("first_name"), handle) if x) or "your Telegram account"
    return True, f"Connected @{name} to {who}. Sent a hello there."


def notify(text: str) -> None:
    """Show a notification on the Mac: the fallback alert when a message can't be sent."""
    if sys.platform != "darwin":
        raise DeliveryError("Mac notifications only work on a Mac.")
    try:
        proc = subprocess.run(["osascript", "-", text[:250]], input=NOTIFY_SCRIPT, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise DeliveryError("The notification didn't show within 30 seconds.") from exc
    if proc.returncode != 0:
        raise DeliveryError(f"Notification failed: {proc.stderr.strip()[:300]}")
