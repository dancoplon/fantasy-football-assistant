"""Install Booth's launchd jobs on the Mac.

com.booth.listener runs `booth listen` all the time, to answer Dan's Telegram messages.
com.booth.scheduler runs `booth run due`:
  - at each report time (Tue 8 AM, Thu 12 PM, Sat 8 AM, Sun 8 AM, Eastern),
  - every 30 minutes, and at login.
launchd runs a calendar job missed during sleep as soon as the Mac wakes, and
`booth run due` never sends the same report twice, so the extra triggers only
add catch-up chances.

Optional: a pmset wake a minute after the 8 AM reports on Tue/Sat/Sun (needs an
admin password once). Waking just after the report time makes launchd start the
missed run right away, and the run keeps the Mac awake until it's done. It works
only with the lid open; a closed lid on battery always sleeps.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from booth.config import ROOT
from booth.jobs import EASTERN, SCHEDULE, mark_installed

LABEL = "com.booth.scheduler"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
# The listener answers Dan's Telegram messages; launchd keeps it running (booth/chat.py).
LISTENER = "com.booth.listener"
LISTENER_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LISTENER}.plist"
# launchd weekday numbers: 0 = Sunday ... 6 = Saturday
RUN_WEEKDAY = {"tue": 2, "thu": 4, "sat": 6, "sun": 0}


def _local_tz() -> ZoneInfo:
    try:
        target = os.readlink("/etc/localtime")
        return ZoneInfo(target.split("zoneinfo/")[-1])
    except (OSError, KeyError, ValueError):
        return EASTERN


def calendar_intervals(local_tz: ZoneInfo | None = None) -> list[dict]:
    """Report times converted from Eastern to the Mac's local time."""
    local_tz = local_tz or _local_tz()
    out = []
    # A reference Sunday; only weekday/hour/minute matter.
    ref_sunday = datetime(2026, 10, 11, tzinfo=EASTERN)
    for run, (_, hh, mm) in SCHEDULE.items():
        days_from_sunday = RUN_WEEKDAY[run]
        et = (ref_sunday + timedelta(days=days_from_sunday)).replace(hour=hh, minute=mm)
        local = et.astimezone(local_tz)
        out.append({"Weekday": (local.weekday() + 1) % 7, "Hour": local.hour, "Minute": local.minute})
    return out


def pmset_wake_command(local_tz: ZoneInfo | None = None) -> str:
    """`pmset repeat` allows one time for all days, so it covers the 8 AM reports (Tue/Sat/Sun)."""
    local_tz = local_tz or _local_tz()
    tue = datetime(2026, 10, 13, SCHEDULE["tue"][1], SCHEDULE["tue"][2], tzinfo=EASTERN) + timedelta(minutes=1)
    return f"pmset repeat wakeorpoweron TSU {tue.astimezone(local_tz).strftime('%H:%M:%S')}"


def _bin(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / name
    if candidate.exists():
        return str(candidate)
    raise FileNotFoundError(f"Couldn't find `{name}`. Install it first.")


def _env(uv: str) -> dict:
    home = str(Path.home())
    dirs = [str(Path(uv).parent), f"{home}/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return {"PATH": ":".join(dict.fromkeys(dirs)), "HOME": home}


def listener_plist_dict() -> dict:
    uv = _bin("uv")
    logs = ROOT / "logs"
    return {
        "Label": LISTENER,
        "ProgramArguments": [uv, "run", "--quiet", "--project", str(ROOT), "booth", "listen"],
        "WorkingDirectory": str(ROOT),
        "EnvironmentVariables": _env(uv),
        "RunAtLoad": True,
        "KeepAlive": True,  # restarted if it ever stops
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "StandardOutPath": str(logs / "listener.out.log"),
        "StandardErrorPath": str(logs / "listener.err.log"),
    }


def plist_dict() -> dict:
    uv = _bin("uv")
    logs = ROOT / "logs"
    return {
        "Label": LABEL,
        "ProgramArguments": [uv, "run", "--quiet", "--project", str(ROOT), "booth", "run", "due"],
        "WorkingDirectory": str(ROOT),
        "EnvironmentVariables": _env(uv),
        "StartCalendarInterval": calendar_intervals(),
        "StartInterval": 1800,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "StandardOutPath": str(logs / "launchd.out.log"),
        "StandardErrorPath": str(logs / "launchd.err.log"),
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _unload(domain: str, label: str, wait_seconds: float = 10) -> None:
    """Unload a job and wait until launchd has really let go of it, so loading it again doesn't fail."""
    _launchctl("bootout", f"{domain}/{label}")  # fine if it wasn't loaded
    deadline = time.time() + wait_seconds
    while time.time() < deadline and _launchctl("print", f"{domain}/{label}").returncode == 0:
        time.sleep(0.5)


def _load(label: str, plist: Path, data: dict) -> None:
    plist.parent.mkdir(parents=True, exist_ok=True)
    with plist.open("wb") as f:
        plistlib.dump(data, f)
    domain = f"gui/{os.getuid()}"
    _unload(domain, label)
    res = _launchctl("bootstrap", domain, str(plist))
    if res.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed for {label}: {res.stderr.strip()}")


def install() -> str:
    """Install (or reinstall) the report scheduler and the Telegram listener."""
    (ROOT / "logs").mkdir(exist_ok=True)
    _load(LABEL, PLIST, plist_dict())
    mark_installed()
    _load(LISTENER, LISTENER_PLIST, listener_plist_dict())
    return f"Installed {PLIST} and {LISTENER_PLIST}"


def uninstall() -> str:
    for label, plist in ((LABEL, PLIST), (LISTENER, LISTENER_PLIST)):
        _launchctl("bootout", f"gui/{os.getuid()}/{label}")
        plist.unlink(missing_ok=True)
    return f"Removed {LABEL} and {LISTENER}"


def status() -> str:
    res = _launchctl("print", f"gui/{os.getuid()}/{LABEL}")
    if res.returncode != 0:
        return "Not installed."
    keep = [ln.strip() for ln in res.stdout.splitlines() if any(k in ln for k in ("state =", "last exit code", "runs ="))]
    return "\n".join([
        "Installed.", *keep,
        "Recent checks (every scheduled run, including ones with nothing due):", *_tail(ROOT / "logs" / "launchd.out.log", 5),
        "Recent reports and problems:", *_tail(ROOT / "logs" / "booth.log", 8),
        f"Telegram listener: {'running' if _launchctl('print', f'gui/{os.getuid()}/{LISTENER}').returncode == 0 else 'not installed'}",
        *_tail(ROOT / "logs" / "chat.log", 5),
    ])


def _tail(path: Path, n: int) -> list[str]:
    lines = path.read_text(errors="replace").splitlines()[-n:] if path.exists() else []
    return [f"  {ln}" for ln in lines] or ["  (none yet)"]


def test_send(via: str | None = None, wait_seconds: int = 180) -> str:
    """Send a test message (by the channel in use, or `via`) from a one-shot LaunchAgent, the
    same context scheduled reports use.

    For iMessage this matters: the macOS "control Messages" permission is granted per app,
    so this makes the prompt name Booth's runner instead of whatever app you happen to be
    typing in. `via="imessage"` checks the iMessage backup while Telegram is in use.
    """
    import time

    from booth.deliver import channel

    label = "com.booth.sendtest"
    plist = PLIST.parent / f"{label}.plist"
    log = ROOT / "logs" / "sendtest.log"
    (ROOT / "logs").mkdir(exist_ok=True)
    log.unlink(missing_ok=True)
    base = plist_dict()
    job = {
        "Label": label,
        "ProgramArguments": base["ProgramArguments"][:-2] + ["send-test"] + (["--via", via] if via else []),
        "WorkingDirectory": base["WorkingDirectory"],
        "EnvironmentVariables": base["EnvironmentVariables"],
        "RunAtLoad": True,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }
    plist.parent.mkdir(parents=True, exist_ok=True)
    with plist.open("wb") as f:
        plistlib.dump(job, f)
    domain = f"gui/{os.getuid()}"
    _unload(domain, label)
    res = _launchctl("bootstrap", domain, str(plist))
    if res.returncode != 0:
        plist.unlink(missing_ok=True)
        raise RuntimeError(f"launchctl bootstrap failed: {res.stderr.strip()}")
    try:
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            text = log.read_text() if log.exists() else ""
            if "sent via" in text or "error" in text.lower():
                return text.strip()
            time.sleep(2)
        return ("No result yet. If a 'control Messages' prompt is showing, click OK and run this again."
                if via == "imessage" or (not via and channel() == "imessage")
                else "No result yet. Check logs/sendtest.log, or run `uv run booth send-test` to see the error.")
    finally:
        _launchctl("bootout", f"{domain}/{label}")
        plist.unlink(missing_ok=True)


def install_wake() -> str:
    """Ask macOS to wake just after the 8 AM reports on Tue/Sat/Sun. Shows the standard admin password dialog."""
    command = pmset_wake_command()
    script = f'do shell script "{command}" with administrator privileges'
    res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"pmset wake not set: {res.stderr.strip()}")
    return f"Wake scheduled: Tue/Sat/Sun at {command.rsplit(' ', 1)[-1]} (only helps with the lid open)."
