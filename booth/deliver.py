"""Delivery: one swappable send() so the channel can change without touching the pipeline.

Channels:
- telegram: a message from Dan's own Booth bot. TELEGRAM_BOT_TOKEN lives in .env; the chat
  to send to is saved by `booth telegram-setup`. If Telegram fails, the message goes by
  iMessage instead (when IMESSAGE_RECIPIENT is set), so a report is never lost to an outage.
- imessage: AppleScript on the Mac running Booth, to IMESSAGE_RECIPIENT (never in code).
- stdout: print it (no channel configured).
BOOTH_DELIVERY in .env forces one; otherwise Telegram once it's set up, then iMessage.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import requests
from dotenv import load_dotenv

from booth.config import ROOT
from booth.state import state_dir

TELEGRAM_API = "https://api.telegram.org"
# Telegram's limit is 4096 characters per message; split longer reports at blank lines.
TELEGRAM_CHUNK = 4000

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
    if os.getenv("TELEGRAM_BOT_TOKEN") and telegram_chat_id():
        return "telegram"
    return "imessage" if os.getenv("IMESSAGE_RECIPIENT") else "stdout"


def send(text: str) -> str:
    """Deliver one message. Returns the channel used."""
    ch = channel()
    if ch == "stdout":
        print(text)
        return ch
    if ch == "telegram":
        try:
            _send_telegram(text)
            return ch
        except DeliveryError as exc:
            if not os.getenv("IMESSAGE_RECIPIENT"):
                raise
            _send_imessage(f"(Sent by text because Telegram failed: {str(exc)[:200]})\n\n{text}")
            return "imessage (Telegram failed)"
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
    try:
        resp = requests.post(f"{TELEGRAM_API}/bot{token}/{method}", json=params, timeout=30)
        body = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise DeliveryError(f"Telegram unreachable: {str(exc).replace(token, '<token>')[:200]}") from None
    if not body.get("ok"):
        hint = " (check TELEGRAM_BOT_TOKEN in .env)" if resp.status_code in (401, 404) else ""
        raise DeliveryError(f"Telegram refused {method}: {str(body.get('description'))[:200]}{hint}")
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
        raise DeliveryError("Telegram isn't connected yet: run `booth telegram-setup`.")
    for part in _chunks(text):
        _telegram("sendMessage", chat_id=chat_id, text=part, disable_web_page_preview=True)


def telegram_setup() -> str:
    """Find the chat where Dan said hi to his bot, remember it, and send a hello there."""
    load_dotenv(ROOT / ".env")
    bot = _telegram("getMe")
    chats = {}
    for update in _telegram("getUpdates", allowed_updates=["message"]):
        chat = (update.get("message") or {}).get("chat") or {}
        if chat.get("type") == "private":
            chats[chat["id"]] = chat.get("first_name") or "someone"
    if not chats:
        raise DeliveryError(
            f"No message found yet. In Telegram, open @{bot['username']}, send it \"hi\", then run this again "
            "(within a day: Telegram keeps unread bot messages for 24 hours)."
        )
    if len(chats) > 1:
        raise DeliveryError(
            f"More than one person has messaged @{bot['username']} ({', '.join(sorted(chats.values()))}), "
            "so Booth can't tell which chat is yours. Ask the others to stop, wait a day, and try again."
        )
    chat_id, name = next(iter(chats.items()))
    path = _telegram_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"chat_id": chat_id, "bot": bot["username"]}) + "\n")
    _telegram("sendMessage", chat_id=chat_id, text="Booth is connected. Your reports will arrive here from now on.")
    return f"Connected to {name}'s chat with @{bot['username']}. Sent a hello there."


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
