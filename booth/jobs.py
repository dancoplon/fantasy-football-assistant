"""The scheduled entrypoint: `booth run due`.

launchd starts it at each report time, every 30 minutes while the Mac is awake, at
login, and right after a wake that slept through a report time. Each start works out
which report is due, skips it if it was already delivered or a later report has
superseded it, and otherwise builds and sends it. That makes a missed run (laptop
asleep, shut down, on battery) catch up on the next wake without ever sending the
same report twice.

Report times (ET): Tue 8:00 AM waivers, Thu 12:00 PM (earlier when a game is played
before Thursday night), Sat 8:00 AM, Sun 8:00 AM.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import traceback
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from booth.config import ROOT
from booth.data import nflverse
from booth.deliver import notify, send
from booth.report import REPORTS, RUN_LABELS, generate
from booth.state import load_week, state_dir, write_week

EASTERN = ZoneInfo("America/New_York")
SEQUENCE = ["tue", "thu", "sat", "sun"]
LINEUP_RUNS = ("thu", "sat", "sun")
# (days after the week's Thursday, hour, minute) in ET
SCHEDULE = {"tue": (-2, 8, 0), "thu": (0, 12, 0), "sat": (2, 8, 0), "sun": (3, 8, 0)}
# Build attempts per report, one per 30-minute trigger (about 3 hours of retries).
# A report that was built but not delivered keeps retrying the send until it expires.
MAX_ATTEMPTS = 6


class LockBusy(Exception):
    """Another Booth run (scheduled or manual) is in progress."""


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


def _kickoff(g: dict) -> datetime:
    d = date.fromisoformat(g["gameday"])
    hh, mm = (int(x) for x in g["gametime"].split(":"))
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=EASTERN)


def _week_kickoffs(week: int, games: list[dict]) -> list[datetime]:
    return sorted(_kickoff(g) for g in games if int(g["week"]) == week)


def scheduled_at(run: str, week: int, games: list[dict]) -> datetime:
    days, hh, mm = SCHEDULE[run]
    d = week_thursday(week, games) + timedelta(days=days)
    t = datetime(d.year, d.month, d.day, hh, mm, tzinfo=EASTERN)
    if run == "thu":
        # A game before Thursday night (week 1's Wednesday opener, Thanksgiving eve and
        # morning) locks first, so the first lineup check moves to noon that day and at
        # least 2 hours before kickoff.
        first = _week_kickoffs(week, games)[0]
        if first < t + timedelta(hours=2):
            noon = datetime(first.year, first.month, first.day, 12, 0, tzinfo=EASTERN)
            t = min(t, noon, first - timedelta(hours=2))
    return t


def expires_at(run: str, week: int, games: list[dict]) -> datetime:
    """After this the report is useless, and a catch-up run skips it.

    Tuesday's waiver report gives way to the first lineup check. A lineup report stays
    useful until the week's last game kicks off (Monday night): a late check can still
    set the players who haven't played.
    """
    if run == "tue":
        return scheduled_at("thu", week, games)
    return _week_kickoffs(week, games)[-1]


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
    """A LATE line for a lineup report sent after games it should have come before.

    `now` is when the report is sent. Only games that kicked off after the report was
    due count: Thursday's game doesn't make an on-time Saturday report late.
    """
    if run not in LINEUP_RUNS:
        return None
    due = scheduled_at(run, week, games)
    started = [g for g in games if int(g["week"]) == week and due < _kickoff(g) <= now]
    if not started:
        return None
    started.sort(key=_kickoff)
    names = ", ".join(f"{g['away_team']}@{g['home_team']}" for g in started[:4])
    if len(started) > 4:
        names += f" and {len(started) - 4} more"
    return f"LATE: sent {now.strftime('%a %-I:%M %p')} ET, after kickoff of {names}. Those players are already locked."


class _Lock:
    """One Booth run at a time. The OS drops the lock when the process ends, even if it's killed."""

    def __init__(self):
        self.path = state_dir() / ".booth.lock"
        self.fd: int | None = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise LockBusy("another Booth run is in progress") from None
        self.fd = fd
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


def _stay_awake() -> None:
    """Keep the Mac from idle-sleeping (or sleeping at all on power) until this run ends.

    A run takes minutes; without this the Mac can doze off mid-report and finish it
    hours later. A closed lid on battery still sleeps; nothing can stop that.
    """
    if sys.platform == "darwin" and os.path.exists("/usr/bin/caffeinate"):
        subprocess.Popen(
            ["/usr/bin/caffeinate", "-i", "-s", "-w", str(os.getpid())],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


def installed_at() -> datetime | None:
    """When the schedule was first installed. Reports due before then aren't Booth's to send."""
    marker = state_dir() / "installed_at"
    try:
        return datetime.fromisoformat(marker.read_text().strip())
    except (OSError, ValueError):
        return None


def mark_installed(now: datetime | None = None) -> None:
    """Record the first install, so it never back-fills (or reports as missed) older reports."""
    marker = state_dir() / "installed_at"
    if not marker.exists():
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text((now or datetime.now(EASTERN)).isoformat())


def _bookkeeping(season: int, week: int) -> dict:
    return load_week(season, week).setdefault("jobs", {})


def _save_bookkeeping(season: int, week: int, jobs: dict) -> None:
    data = load_week(season, week)
    data["jobs"] = jobs
    write_week(data)


def _alert(text: str, send_fn, notify_fn) -> bool:
    """Tell Dan something went wrong: by message, else by a notification on the Mac."""
    try:
        send_fn(text)
        return True
    except Exception as exc:
        _log(f"alert by message failed: {exc}")
    try:
        notify_fn(text)
        return True
    except Exception as exc:
        _log(f"alert by notification also failed: {exc}")
        return False


def run_due(now: datetime | None = None, games: list[dict] | None = None, generate_fn=generate, send_fn=send,
            clock=None, notify_fn=notify) -> str:
    """Run whatever report is due. Returns a one-line outcome.

    `clock` gives the current time when the report is about to be sent (tests pass one
    that moves; by default it's the real clock, or `now` when a fixed `now` is given).
    """
    load_dotenv(ROOT / ".env")
    if clock is None:
        clock = (lambda: now) if now else (lambda: datetime.now(EASTERN))
    now = now or clock()
    try:
        games = games or nflverse.games()
    except Exception as exc:
        _log(f"FAILED: couldn't load the NFL schedule: {exc}")
        return f"failed: couldn't load the NFL schedule: {exc}"
    found = due_run(now, games)
    if not found:
        return "nothing due"
    run, week = found
    since = installed_at()
    if since and scheduled_at(run, week, games) < since:
        return f"{run} week {week} was due before the schedule was installed"
    season = nflverse.SEASON
    label = RUN_LABELS[run]
    jobs: dict = {}
    job: dict = {}
    try:
        with _Lock():
            jobs = _bookkeeping(season, week)
            job = jobs.setdefault(run, {})
            if job.get("delivered_at"):
                return f"{run} week {week} already delivered"
            if now >= expires_at(run, week, games):
                return _expired(run, week, label, jobs, job, season, send_fn, notify_fn)
            report_file = REPORTS / f"{season}-wk{week:02d}-{run}.txt"
            built = job.get("generated") and report_file.exists()
            if not built and job.get("attempts", 0) >= MAX_ATTEMPTS:
                return f"{run} week {week} gave up after {MAX_ATTEMPTS} attempts"

            _stay_awake()
            if built:
                message = report_file.read_text()  # built earlier, only the send failed
            else:
                job["attempts"] = job.get("attempts", 0) + 1
                _save_bookkeeping(season, week, jobs)
                message = generate_fn(run, week=week, now=now)["message"]
                job["generated"] = True
                _save_bookkeeping(season, week, jobs)

            # Building takes minutes and the Mac may have slept meanwhile: check again at send time.
            sent_at = clock()
            if sent_at >= expires_at(run, week, games) or due_run(sent_at, games) != (run, week):
                outcome = f"{run} week {week} finished at {sent_at:%a %-I:%M %p}, too late to send"
                _log(outcome)
                return outcome
            note = lateness_note(run, week, sent_at, games)
            if note:
                message = f"{note}\n\n{message}"
            if os.getenv("BOOTH_DRY_RUN") == "1":
                message = f"[DRY RUN] {message}"
            send_fn(message)
            job["delivered_at"] = sent_at.isoformat()
            _save_bookkeeping(season, week, jobs)
    except LockBusy:
        _log(f"{run} week {week} skipped: another run in progress")
        return "skipped: another run in progress"
    except Exception as exc:  # anything else: alert instead of failing silently
        return _fail(run, week, label, exc, jobs, job, send_fn, notify_fn, season)
    outcome = f"delivered {run} week {week}"
    _log(outcome)
    return outcome


def _expired(run, week, label, jobs, job, season, send_fn, notify_fn) -> str:
    """The report's window closed. If Booth never even tried a lineup report, say so once."""
    outcome = f"{run} week {week} expired, nothing to send"
    if run in LINEUP_RUNS and not job.get("attempts") and not job.get("missed_alerted"):
        _log(f"{outcome} (never ran: the Mac was asleep or off)")
        if _alert(f"Booth missed the {label} for week {week}: the Mac was asleep or off until the last game "
                  f"kicked off. Nothing to do now.", send_fn, notify_fn):
            job["missed_alerted"] = True
            _save_bookkeeping(season, week, jobs)
    return outcome


def _fail(run, week, label, exc, jobs, job, send_fn, notify_fn, season) -> str:
    _log(f"FAILED {run} week {week}: {exc}\n{traceback.format_exc()}")
    if not jobs and not job:  # failed before the week's bookkeeping loaded
        _alert(f"Booth hit an error before the {label} (week {week}): {str(exc)[:300]}", send_fn, notify_fn)
        return f"failed {run} week {week}: {exc}"
    job["last_error"] = str(exc)[:500]
    attempts = job.get("attempts", 0)
    if job.get("generated"):
        key = "send_alerted"
        text = (f"Booth built the {label} (week {week}) but couldn't send it: {str(exc)[:300]}\n"
                f"It will keep trying every 30 minutes while the Mac is awake.")
    elif attempts >= MAX_ATTEMPTS:
        key = "gave_up_alerted"
        text = (f"Booth gave up on the {label} (week {week}) after {attempts} tries: {str(exc)[:300]}\n"
                f"No report is coming. Check Yahoo yourself.")
    else:
        key = "alerted"
        text = (f"Booth couldn't build the {label} (week {week}): {str(exc)[:300]}\n"
                f"It will retry every 30 minutes while the Mac is awake, up to {MAX_ATTEMPTS} tries. "
                f"If a deadline is close, check Yahoo yourself.")
    if not job.get(key) and _alert(text, send_fn, notify_fn):
        job[key] = True
    jobs[run] = job
    try:
        _save_bookkeeping(season, week, jobs)
    except Exception as save_exc:
        _log(f"couldn't save bookkeeping: {save_exc}")
    return f"failed {run} week {week}: {exc}"


def record_manual_delivery(run: str, week: int, now: datetime | None = None, games: list[dict] | None = None) -> bool:
    """`booth report <run> --send` delivered this report by hand.

    If it's the report the schedule would send right now, mark it delivered so the
    scheduler doesn't send it again. Call while holding the lock. Returns True if marked.
    """
    now = now or datetime.now(EASTERN)
    games = games or nflverse.games()
    if due_run(now, games) != (run, week):
        return False
    season = nflverse.SEASON
    jobs = _bookkeeping(season, week)
    job = jobs.setdefault(run, {})
    job["generated"] = True
    job["delivered_at"] = now.isoformat()
    job["delivered_by"] = "manual"
    _save_bookkeeping(season, week, jobs)
    _log(f"delivered {run} week {week} (manual)")
    return True


def lock() -> _Lock:
    """The run lock, for commands outside the scheduler (e.g. `booth report --send`)."""
    return _Lock()
