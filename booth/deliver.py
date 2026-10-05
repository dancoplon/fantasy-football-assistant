"""Delivery: one swappable send() so the channel can move (Telegram, WhatsApp) without touching the pipeline.

Today: iMessage via AppleScript on the Mac running Booth. The recipient comes from
IMESSAGE_RECIPIENT in .env (phone number or Apple ID email) and never lives in code.
"""

from __future__ import annotations

import os
import subprocess
import sys

from dotenv import load_dotenv

from booth.config import ROOT

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
    return os.getenv("BOOTH_DELIVERY", "imessage" if os.getenv("IMESSAGE_RECIPIENT") else "stdout")


def send(text: str) -> str:
    """Deliver one message. Returns the channel used."""
    ch = channel()
    if ch == "stdout":
        print(text)
        return ch
    if ch == "imessage":
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
        return ch
    raise DeliveryError(f"Unknown BOOTH_DELIVERY channel '{ch}'.")


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
