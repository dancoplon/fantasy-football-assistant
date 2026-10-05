"""The scheduled entrypoint: `booth run due`.

launchd starts it at each report time, every 30 minutes, and at login/wake. Each
start works out which report is due, skips it if it was already delivered or a
later report has superseded it, and otherwise builds and sends it. That makes a
missed run (laptop asleep, shut down, on battery) catch up on the next wake
without ever sending the same report twice.

Report times (ET): Tue 8:00 AM waivers, Thu 12:00 PM, Sat 8:00 AM, Sun 8:00 AM.
"""

from __future__ import annotations

import json
import os
import time
import traceback
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from booth.config import ROOT
from booth.data import nflverse
from booth.deliver import send
from booth.report import REPORTS, RUN_LABELS, generate
from booth.state import load_week, save_snapshot, state_dir, week_path

EASTERN = ZoneInfo("America/New_York")
SEQUENCE = ["tue", "thu", "sat", "sun"]
# (days after the week's Thursday, hour, minute) in ET
SCHEDULE = {"tue": (-2, 8, 0), "thu": (0, 12, 0), "sat": (2, 8, 0), "sun": (3, 8, 0)}
MAX_ATTEMPTS = 3
LOCK_STALE_SECONDS = 25 * 60


def _log(msg: str) -> None:
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    stamp = datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S %Z")
    with (logs / "booth.log").open("a") as f:
        f.write(f"{stamp}  {msg}\n")


def week_thursday(week: int, games: list[dict]) -> date:
    """The Thursday of an NFL week (3 days before its main Sunday slate)."""
    sundays = sorted({g["gameday"] for g in games if int(g["week"]) == week and g["weekday"] == "Sunday"})
    if sundays:
        return date.fromisoformat(sundays[0]) - timedelta(days=3)
    first = min(g["gameday"] for g in games if int(g["week"]) == week)
    d = date.fromisoformat(first)
    return d - timedelta(days=(d.weekday() - 3) % 7)


def scheduled_at(run: str, week: int, games: list[dict]) -> datetime:
    days, hh, mm = SCHEDULE[run]
    d = week_thursday(week, games) + timedelta(days=days)
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=EASTERN)


def due_run(now: datetime, games: list[dict]) -> tuple[str, int] | None:
    """The most recent report whose time has passed, as (run, week)."""
    weeks = sorted({int(g["week"]) for g in games})
    cw = nflverse.current_week(games, now.date())
    best = None
    for wk in (cw - 1, cw, cw + 1):
        if wk not in weeks:
            continue
        for run in SEQUENCE:
            t = scheduled_at(run, wk, games)
            if t <= now and (best is None or t > best[0]):
                best = (t, run, wk)
    return (best[1], best[2]) if best else None


def lateness_note(run: str, week: int, now: datetime, games: list[dict]) -> str | None:
    sched = nflverse.schedule(week, games)["games"]

    def kickoff(g):
        hh, mm = (int(x) for x in g["kickoff_et"].split(":"))
        d = date.fromisoformat(g["date"])
        return datetime(d.year, d.month, d.day, hh, mm, tzinfo=EASTERN)

    # Only games that kicked off after this report was due make it late.
    due = scheduled_at(run, week, games)
    started = [g for g in sched if due < kickoff(g) <= now]
    if run in ("thu", "sat", "sun") and started:
        names = ", ".join(f"{g['away']}@{g['home']}" for g in started[:4])
        return f"LATE: this ran at {now.strftime('%a %-I:%M %p')} ET, after kickoff of {names}. Those players are already locked."
    return None


class _Lock:
    def __init__(self):
        self.path = state_dir() / ".booth.lock"

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and time.time() - self.path.stat().st_mtime < LOCK_STALE_SECONDS:
            raise RuntimeError("another Booth run is in progress")
        self.path.write_text(str(os.getpid()))
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


def _bookkeeping(season: int, week: int) -> dict:
    return load_week(season, week).setdefault("jobs", {})


def _save_bookkeeping(season: int, week: int, jobs: dict) -> None:
    path = week_path(season, week)
    data = load_week(season, week)
    data["jobs"] = jobs
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def run_due(now: datetime | None = None, games: list[dict] | None = None, generate_fn=generate, send_fn=send) -> str:
    """Run whatever report is due. Returns a one-line outcome (also logged)."""
    load_dotenv(ROOT / ".env")
    now = now or datetime.now(EASTERN)
    games = games or nflverse.games()
    found = due_run(now, games)
    if not found:
        return "nothing due"
    run, week = found
    season = nflverse.SEASON
    jobs = _bookkeeping(season, week)
    job = jobs.get(run, {})
    if job.get("delivered_at"):
        return f"{run} week {week} already delivered"
    if job.get("attempts", 0) >= MAX_ATTEMPTS:
        return f"{run} week {week} gave up after {MAX_ATTEMPTS} attempts"

    label = RUN_LABELS[run]
    try:
        with _Lock():
            report_file = REPORTS / f"{season}-wk{week:02d}-{run}.txt"
            if job.get("generated") and report_file.exists():
                message = report_file.read_text()  # built earlier, only the send failed
            else:
                job["attempts"] = job.get("attempts", 0) + 1
                jobs[run] = job
                _save_bookkeeping(season, week, jobs)
                result = generate_fn(run, week=week, now=now)
                message = result["message"]
                job["generated"] = True
                jobs[run] = job
                _save_bookkeeping(season, week, jobs)
            note = lateness_note(run, week, now, games)
            if note and not message.startswith("LATE"):
                message = f"{note}\n\n{message}"
            if os.getenv("BOOTH_DRY_RUN") == "1":
                message = f"[DRY RUN] {message}"
            send_fn(message)
            job["delivered_at"] = now.isoformat()
            jobs[run] = job
            _save_bookkeeping(season, week, jobs)
    except RuntimeError as exc:
        if "in progress" in str(exc):
            return "skipped: another run in progress"
        return _fail(run, week, label, exc, jobs, job, send_fn, season)
    except Exception as exc:  # anything else: alert instead of failing silently
        return _fail(run, week, label, exc, jobs, job, send_fn, season)
    outcome = f"delivered {run} week {week}"
    _log(outcome)
    return outcome


def _fail(run, week, label, exc, jobs, job, send_fn, season) -> str:
    _log(f"FAILED {run} week {week}: {exc}\n{traceback.format_exc()}")
    job["last_error"] = str(exc)[:500]
    if not job.get("alerted"):
        try:
            send_fn(
                f"Booth couldn't build the {label} (week {week}): {str(exc)[:300]}\n"
                f"Booth will retry. Check your lineup in Yahoo yourself if this was before a lock."
            )
            job["alerted"] = True
        except Exception as send_exc:
            _log(f"alert also failed: {send_exc}")
    jobs[run] = job
    _save_bookkeeping(season, week, jobs)
    return f"failed {run} week {week}: {exc}"
