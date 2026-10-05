import plistlib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from booth import jobs, schedule
from booth.data import nflverse

FIX = Path(__file__).parent / "fixtures"
ET = ZoneInfo("America/New_York")


@pytest.fixture
def games():
    return nflverse.games(path=FIX / "games.csv")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOTH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("BOOTH_DRY_RUN", raising=False)
    monkeypatch.setattr(jobs, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(jobs, "ROOT", tmp_path)
    monkeypatch.setattr(jobs, "load_dotenv", lambda *a, **k: None)
    return tmp_path


def at(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def test_schedule_times_for_week6(games):
    assert jobs.scheduled_at("tue", 6, games) == at(2026, 10, 13, 8)
    assert jobs.scheduled_at("thu", 6, games) == at(2026, 10, 15, 12)
    assert jobs.scheduled_at("sat", 6, games) == at(2026, 10, 17, 8)
    assert jobs.scheduled_at("sun", 6, games) == at(2026, 10, 18, 8)


@pytest.mark.parametrize(
    "now,expected",
    [
        (at(2026, 10, 13, 7, 59), ("sun", 5)),   # before Tuesday 8 AM: last week's Sunday report
        (at(2026, 10, 13, 8, 0), ("tue", 6)),
        (at(2026, 10, 14, 18, 0), ("tue", 6)),   # laptop opened Wednesday: Tuesday report catches up
        (at(2026, 10, 15, 12, 30), ("thu", 6)),
        (at(2026, 10, 17, 9, 0), ("sat", 6)),    # Thursday report superseded by Saturday's
        (at(2026, 10, 18, 7, 0), ("sat", 6)),
        (at(2026, 10, 18, 8, 0), ("sun", 6)),
    ],
)
def test_due_run(games, now, expected):
    assert jobs.due_run(now, games) == expected


def _gen(calls):
    def gen(run, week, now):
        calls.append((run, week))
        path = jobs.REPORTS / f"2026-wk{week:02d}-{run}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"report {run} {week}")
        return {"message": f"report {run} {week}"}
    return gen


def test_run_due_sends_once(env, games):
    calls, sent = [], []
    now = at(2026, 10, 13, 8, 5)
    assert jobs.run_due(now, games, _gen(calls), sent.append) == "delivered tue week 6"
    assert jobs.run_due(at(2026, 10, 13, 8, 35), games, _gen(calls), sent.append) == "tue week 6 already delivered"
    assert calls == [("tue", 6)] and sent == ["report tue 6"]


def test_failed_send_resends_without_regenerating(env, games):
    calls, sent = [], []

    def flaky_send(msg):
        if not sent:
            sent.append("FAIL")
            raise RuntimeError("Messages not signed in")
        sent.append(msg)

    now = at(2026, 10, 15, 12, 1)
    out1 = jobs.run_due(now, games, _gen(calls), flaky_send)
    assert out1.startswith("failed thu week 6")
    out2 = jobs.run_due(at(2026, 10, 15, 12, 31), games, _gen(calls), flaky_send)
    assert out2 == "delivered thu week 6"
    assert calls == [("thu", 6)]  # generated once


def test_generation_failure_alerts_once_and_gives_up(env, games):
    sent = []

    def boom(run, week, now):
        raise RuntimeError("claude not logged in")

    for i in range(4):
        out = jobs.run_due(at(2026, 10, 17, 8, 1 + i), games, boom, sent.append)
    assert out == "sat week 6 gave up after 3 attempts"
    assert len(sent) == 1 and "couldn't build the Saturday" in sent[0]


def test_late_thursday_report_is_labeled(env, games):
    sent = []
    jobs.run_due(at(2026, 10, 16, 9, 0), games, _gen([]), sent.append)  # Friday morning catch-up
    assert sent[0].startswith("LATE:") and "SEA@DEN" in sent[0]


def test_dry_run_prefix(env, games, monkeypatch):
    monkeypatch.setenv("BOOTH_DRY_RUN", "1")
    sent = []
    jobs.run_due(at(2026, 10, 13, 8, 5), games, _gen([]), sent.append)
    assert sent[0].startswith("[DRY RUN] report tue 6")


def test_calendar_intervals_eastern_and_pacific():
    et = schedule.calendar_intervals(ZoneInfo("America/New_York"))
    assert {"Weekday": 2, "Hour": 8, "Minute": 0} in et and {"Weekday": 4, "Hour": 12, "Minute": 0} in et
    pt = schedule.calendar_intervals(ZoneInfo("America/Los_Angeles"))
    assert {"Weekday": 2, "Hour": 5, "Minute": 0} in pt and {"Weekday": 0, "Hour": 5, "Minute": 0} in pt


def test_plist_is_valid(monkeypatch):
    monkeypatch.setattr(schedule, "_bin", lambda name: f"/Users/x/.local/bin/{name}")
    d = schedule.plist_dict()
    plistlib.loads(plistlib.dumps(d))
    assert d["ProgramArguments"][-3:] == ["booth", "run", "due"]
    assert d["RunAtLoad"] and d["StartInterval"] == 1800 and len(d["StartCalendarInterval"]) == 4


def test_on_time_saturday_report_not_late_despite_thursday_game(env, games):
    sent = []
    jobs.run_due(at(2026, 10, 17, 8, 2), games, _gen([]), sent.append)
    assert not sent[0].startswith("LATE")


def test_sunday_catch_up_after_london_kickoff_is_late(env, games):
    sent = []
    jobs.run_due(at(2026, 10, 18, 10, 0), games, _gen([]), sent.append)
    assert sent[0].startswith("LATE:") and "HOU@JAX" in sent[0] and "SEA@DEN" not in sent[0]
