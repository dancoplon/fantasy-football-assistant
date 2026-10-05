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
        proc = subprocess.run(
            ["osascript", "-", text, recipient], input=SEND_SCRIPT, capture_output=True, text=True, timeout=60
        )
        if proc.returncode != 0:
            raise DeliveryError(f"Messages refused to send: {proc.stderr.strip()[:300]}")
        return ch
    raise DeliveryError(f"Unknown BOOTH_DELIVERY channel '{ch}'.")
