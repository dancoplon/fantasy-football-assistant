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
import json
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
    """Best effort: a full disk must never stop an alert from going out."""
    try:
        logs = ROOT / "logs"
        logs.mkdir(exist_ok=True)
        stamp = datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S %Z")
        with (logs / "booth.log").open("a") as f:
            f.write(f"{stamp}  {msg}\n")
    except OSError:
        pass


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


def _game_names(games: list[dict]) -> str:
    games = sorted(games, key=_kickoff)
    names = ", ".join(f"{g['away_team']}@{g['home_team']}" for g in games[:4])
    return names + (f" and {len(games) - 4} more" if len(games) > 4 else "")


def _counted(run: str, week: int, games: list[dict]) -> bool:
    """Reports due before the schedule was installed aren't Booth's to send or to miss."""
    since = installed_at()
    return since is None or scheduled_at(run, week, games) >= since


def _owed(week: int, jobs: dict, games: list[dict], now: datetime) -> list[str]:
    """This week's lineup reports that were due by `now` but never reached Dan, and that no
    later delivered report has covered (a delivered report's LATE line covers the gap)."""
    owed: list[str] = []
    for run in LINEUP_RUNS:
        if scheduled_at(run, week, games) > now or not _counted(run, week, games):
            continue
        owed = [] if jobs.get(run, {}).get("delivered_at") else owed + [run]
    return owed


def lateness_note(run: str, week: int, now: datetime, games: list[dict], jobs: dict | None = None) -> str | None:
    """A LATE line for a lineup report sent after games it should have come before.

    `now` is when the report is sent. The window starts at the earliest lineup report this
    week that never reached Dan (this one, or an earlier one it replaces), so an on-time
    Saturday report after Thursday's isn't late, but one that replaces a missed Thursday
    check names Thursday's game.
    """
    if run not in LINEUP_RUNS:
        return None
    owed = _owed(week, jobs, games, now) if jobs is not None else []
    start = scheduled_at(owed[0] if owed else run, week, games)
    started = [g for g in games if int(g["week"]) == week and start < _kickoff(g) <= now]
    if not started:
        return None
    return f"LATE: sent {now.strftime('%a %-I:%M %p')} ET, after kickoff of {_game_names(started)}. Those players are already locked."


def _kicked_off_between(week: int, games: list[dict], start_iso: str | None, end: datetime) -> bool:
    if not start_iso:
        return False
    start = datetime.fromisoformat(start_iso)
    return any(start < _kickoff(g) <= end for g in games if int(g["week"]) == week)


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


def _alert_log() -> dict:
    try:
        return json.loads((ROOT / "logs" / "alerts.json").read_text())
    except (OSError, ValueError):
        return {}


def _recently_alerted(key: str, hours: float = 6) -> bool:
    """True if this alert reached Dan within the last few hours. Backs up the per-report
    flags in the week file when that can't be saved (full disk, permissions)."""
    last = _alert_log().get(key)
    return bool(last) and datetime.now(EASTERN) - datetime.fromisoformat(last) < timedelta(hours=hours)


def _mark_alerted(key: str) -> None:
    seen = _alert_log()
    seen[key] = datetime.now(EASTERN).isoformat()
    try:
        path = ROOT / "logs" / "alerts.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(seen))
    except OSError:
        pass


def _alert_once(key: str, text: str, send_fn, notify_fn, hours: float = 6) -> bool:
    """Alert unless the same one went out recently. Recorded only once it actually got through."""
    if _recently_alerted(key, hours):
        return False
    if not _alert(text, send_fn, notify_fn):
        return False
    _mark_alerted(key)
    return True


def _missed_lines(week: int, jobs: dict, games: list[dict], now: datetime) -> tuple[str, str] | None:
    """(report label, games that locked without it) for a finished week, or None if nothing is
    owed or it was already said."""
    owed = _owed(week, jobs, games, now)
    if not owed or jobs.get("sun", {}).get("missed_alerted"):
        return None
    # A report that already told Dan "no report is coming" needs no second notice.
    unannounced = [r for r in owed if not jobs.get(r, {}).get("gave_up_alerted")]
    if not unannounced:
        return None
    start = scheduled_at(owed[0], week, games)
    locked = [g for g in games if int(g["week"]) == week and _kickoff(g) > start]
    return RUN_LABELS[unannounced[-1]], _game_names(locked)


def _report_missed(up_to_week: int, games: list[dict], now: datetime, send_fn, notify_fn) -> None:
    """Say once, in one message, which finished weeks had lineup reports that never reached Dan.

    Every finished week since the install is checked, so even a long absence is reported.
    """
    since = installed_at()
    season = nflverse.SEASON
    found = []
    for week in sorted({int(g["week"]) for g in games}):
        if week > up_to_week or now < expires_at("sun", week, games):
            continue
        if since and expires_at("sun", week, games) < since:
            continue  # finished before Booth was installed
        jobs = _bookkeeping(season, week)
        line = _missed_lines(week, jobs, games, now)
        if line and not _recently_alerted(f"missed-{season}-{week}", hours=24 * 30):
            found.append((week, jobs, line))
    if not found:
        return
    if len(found) == 1:
        week, _, (label, locked) = found[0]
        text = (f"Booth didn't get the {label} to you for week {week} (the Mac was asleep or off, or the run "
                f"failed). These games locked without a lineup check: {locked}. Nothing to do now.")
    else:
        text = ("Booth didn't get these reports to you (the Mac was asleep or off, or the run failed):\n"
                + "\n".join(f"- Week {w}: the {label}. Locked without a lineup check: {locked}." for w, _, (label, locked) in found)
                + "\nNothing to do now.")
    _log(f"missed lineup reports for week(s) {', '.join(str(w) for w, _, _ in found)}")
    if not _alert(text, send_fn, notify_fn):
        return
    for week, jobs, _ in found:
        _mark_alerted(f"missed-{season}-{week}")
        jobs.setdefault("sun", {})["missed_alerted"] = True
        try:
            _save_bookkeeping(season, week, jobs)
        except Exception as exc:
            _log(f"couldn't save week {week}: {exc}")


def _kickoff_soon(week: int, games: list[dict], now: datetime, minutes: int = 45) -> bool:
    """A game this week kicks off within the next `minutes`: no time to rebuild a report."""
    return any(now < _kickoff(g) <= now + timedelta(minutes=minutes) for g in games if int(g["week"]) == week)


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
    try:
        if installed_at() is None:
            mark_installed(now)  # first run ever: nothing before this moment is Booth's
    except OSError as exc:
        _log(f"couldn't record the install time: {exc}")  # the lock below fails too, and alerts
    if not _counted(run, week, games):
        return f"{run} week {week} was due before the schedule was installed"
    season = nflverse.SEASON
    label = RUN_LABELS[run]
    jobs: dict = {}
    job: dict = {}
    via = None
    stage = "setup"
    try:
        with _Lock():
            try:
                _report_missed(week, games, now, send_fn, notify_fn)
            except Exception as exc:
                _log(f"couldn't check for missed reports: {exc}")
            jobs = _bookkeeping(season, week)
            job = jobs.setdefault(run, {})
            if job.get("delivered_at"):
                return f"{run} week {week} already delivered"
            if now >= expires_at(run, week, games):
                return f"{run} week {week} expired, nothing to send"
            report_file = REPORTS / f"{season}-wk{week:02d}-{run}.txt"
            built = bool(job.get("generated") and report_file.exists())
            earlier = None
            if built:
                earlier = report_file.read_text()  # built earlier, only the send failed
                if _kicked_off_between(week, games, job.get("generated_at"), now):
                    built_at = datetime.fromisoformat(job["generated_at"])
                    earlier = f"(Built {built_at.strftime('%a %-I:%M %p')} ET; some games have kicked off since.)\n\n{earlier}"
                    # Out of date: rebuild it, unless a kickoff is too close to risk the time.
                    if job.get("attempts", 0) < MAX_ATTEMPTS and not _kickoff_soon(week, games, now):
                        built = False
            if not built and not earlier and job.get("attempts", 0) >= MAX_ATTEMPTS:
                return f"{run} week {week} gave up after {MAX_ATTEMPTS} attempts"

            _stay_awake()
            if built:
                message = earlier
                sent_at = clock()
            else:
                stage = "build"
                job["attempts"] = job.get("attempts", 0) + 1
                _save_bookkeeping(season, week, jobs)
                try:
                    message = generate_fn(run, week=week, now=now)["message"]
                except Exception as exc:
                    if earlier is None:
                        raise
                    _log(f"rebuilding {run} week {week} failed, sending the earlier version: {exc}")
                    message = earlier
                    sent_at = clock()
                else:
                    sent_at = clock()  # building takes minutes and the Mac may have slept meanwhile
                    job["generated"] = True
                    job["generated_at"] = sent_at.isoformat()
                    _save_bookkeeping(season, week, jobs)

            if sent_at >= expires_at(run, week, games) or due_run(sent_at, games) != (run, week):
                outcome = f"{run} week {week} finished at {sent_at:%a %-I:%M %p}, too late to send"
                _log(outcome)
                try:
                    _report_missed(week, games, sent_at, send_fn, notify_fn)
                except Exception as exc:
                    _log(f"couldn't check for missed reports: {exc}")
                return outcome
            note = lateness_note(run, week, sent_at, games, jobs)
            if note:
                message = f"{note}\n\n{message}"
            if os.getenv("BOOTH_DRY_RUN") == "1":
                message = f"[DRY RUN] {message}"
            stage = "send"
            via = send_fn(message)
            job["delivered_at"] = sent_at.isoformat()
            _save_bookkeeping(season, week, jobs)
    except LockBusy:
        _log(f"{run} week {week} skipped: another run in progress")
        return "skipped: another run in progress"
    except Exception as exc:  # anything else: alert instead of failing silently
        return _fail(run, week, label, exc, jobs, job, send_fn, notify_fn, season, stage)
    outcome = f"delivered {run} week {week}"
    if isinstance(via, str) and via.startswith("imessage (Telegram failed"):
        outcome += f" by {via}"  # so `booth schedule status` shows why it came by text
    _log(outcome)
    return outcome


def _fail(run, week, label, exc, jobs, job, send_fn, notify_fn, season, stage="build") -> str:
    _log(f"FAILED {run} week {week}: {exc}\n{traceback.format_exc()}")
    if not jobs and not job:  # failed before the week's bookkeeping loaded
        _alert_once(f"{run}-{week}-early-{type(exc).__name__}",
                    f"Booth hit an error before the {label} (week {week}): {str(exc)[:300]}", send_fn, notify_fn)
        return f"failed {run} week {week}: {exc}"
    job["last_error"] = str(exc)[:500]
    attempts = job.get("attempts", 0)
    if stage == "send":
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
    if not job.get(key) and _alert_once(f"{run}-{week}-{key}", text, send_fn, notify_fn):
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
