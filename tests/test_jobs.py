import plistlib
from datetime import date, datetime, timedelta
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
    install_at(at(2026, 10, 13, 0, 0))  # week 6 starts with the schedule installed
    return tmp_path


def at(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def install_at(when):
    marker = jobs.state_dir() / "installed_at"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(when.isoformat())


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


def test_generation_failure_alerts_once_then_says_when_it_gives_up(env, games):
    sent = []

    def boom(run, week, now):
        raise RuntimeError("claude not logged in")

    outs = [jobs.run_due(at(2026, 10, 17, 8 + i // 2, 30 * (i % 2)), games, boom, sent.append)
            for i in range(jobs.MAX_ATTEMPTS + 1)]
    assert outs[-1] == f"sat week 6 gave up after {jobs.MAX_ATTEMPTS} attempts"
    assert len(sent) == 2
    assert "couldn't build the Saturday" in sent[0] and "retry" in sent[0]
    assert "gave up" in sent[1] and "No report is coming" in sent[1]


def test_built_report_keeps_resending_after_attempt_cap(env, games):
    # Five failed builds, then a build whose send fails: the finished report must still go out.
    sent, calls = [], []
    tries = iter([RuntimeError("overloaded")] * (jobs.MAX_ATTEMPTS - 1) + [None])

    def gen(run, week, now):
        err = next(tries)
        if err:
            raise err
        return _gen(calls)(run, week, now)

    failed_once = []

    def send(msg):
        if "report sun 6" in msg and not failed_once:
            failed_once.append(True)
            raise RuntimeError("Messages timed out")
        sent.append(msg)

    for i in range(jobs.MAX_ATTEMPTS):
        jobs.run_due(at(2026, 10, 18, 8, i * 5), games, gen, send)
    assert jobs.run_due(at(2026, 10, 18, 9, 0), games, gen, send) == "delivered sun week 6"
    assert sent[-1].endswith("report sun 6") and calls == [("sun", 6)]


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
    jobs.run_due(at(2026, 10, 15, 12, 2), games, _gen([]), sent.append)
    jobs.run_due(at(2026, 10, 17, 8, 2), games, _gen([]), sent.append)
    assert sent[1] == "report sat 6"


def test_saturday_report_names_the_game_a_missed_thursday_check_skipped(env, games):
    sent = []
    jobs.run_due(at(2026, 10, 17, 9, 0), games, _gen([]), sent.append)  # asleep Thursday through Saturday 9 AM
    assert sent[0].startswith("LATE: sent Sat 9:00 AM ET, after kickoff of SEA@DEN.")


def test_sunday_catch_up_after_london_kickoff_is_late(env, games):
    sent = []
    jobs.run_due(at(2026, 10, 15, 12, 0), games, _gen([]), sent.append)
    jobs.run_due(at(2026, 10, 17, 8, 0), games, _gen([]), sent.append)
    jobs.run_due(at(2026, 10, 18, 10, 0), games, _gen([]), sent.append)
    assert sent[2].startswith("LATE: sent Sun 10:00 AM ET, after kickoff of HOU@JAX.")


def test_test_send_uses_same_runner_as_schedule(monkeypatch, tmp_path):
    monkeypatch.setattr(schedule, "_bin", lambda name: f"/Users/x/.local/bin/{name}")
    monkeypatch.setattr(schedule, "PLIST", tmp_path / "com.booth.scheduler.plist")
    monkeypatch.setattr(schedule, "ROOT", tmp_path)
    calls = []

    def fake_launchctl(*args):
        calls.append(args)
        if args[0] == "bootstrap":
            plist = plistlib.loads(Path(args[2]).read_bytes())
            assert plist["ProgramArguments"][-2:] == ["booth", "send-test"]
            (tmp_path / "logs" / "sendtest.log").write_text("sent via imessage\n")
        return type("R", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(schedule, "_launchctl", fake_launchctl)
    assert schedule.test_send(wait_seconds=5) == "sent via imessage"
    assert calls[-1][0] == "bootout" and not (tmp_path / "com.booth.sendtest.plist").exists()


def test_install_never_back_fills_older_reports(env, games):
    # Installed Monday afternoon: last Sunday's report is not Booth's to send, and after
    # Monday night's kickoff it isn't reported as missed either.
    install_at(at(2026, 10, 12, 13, 30))
    sent = []
    out = jobs.run_due(at(2026, 10, 12, 13, 30), games, _gen([]), sent.append)
    assert out == "sun week 5 was due before the schedule was installed" and sent == []
    jobs.run_due(at(2026, 10, 12, 21, 0), games, _gen([]), sent.append)
    assert sent == []
    jobs.mark_installed(at(2026, 10, 14, 9, 0))  # a reinstall keeps the first install time
    assert jobs.installed_at() == at(2026, 10, 12, 13, 30)


def test_no_install_record_means_nothing_before_now_is_owed(env, games):
    (jobs.state_dir() / "installed_at").unlink()
    sent = []
    assert jobs.run_due(at(2026, 10, 13, 8, 5), games, _gen([]), sent.append) == "tue week 6 was due before the schedule was installed"
    assert jobs.installed_at() == at(2026, 10, 13, 8, 5) and sent == []


def test_monday_catch_up_before_monday_night_game(env, games):
    install_at(at(2026, 10, 11, 0, 0))
    sent = []
    out = jobs.run_due(at(2026, 10, 12, 9, 0), games, _gen([]), sent.append)
    assert out == "delivered sun week 5"
    assert sent[0].startswith("LATE: sent Mon 9:00 AM ET, after kickoff of PHI@JAX, CHI@GB, CIN@MIA.")


def test_missed_lineup_report_is_reported_once(env, games):
    install_at(at(2026, 10, 1, 0, 0))
    sent = []
    for hh in (21, 22):  # after Monday night's kickoff, the Mac never ran week 5's lineup reports
        out = jobs.run_due(at(2026, 10, 12, hh, 0), games, _gen([]), sent.append)
    assert out == "sun week 5 expired, nothing to send"
    assert sent == ["Booth didn't get the Sunday final lineup pass to you for week 5 (the Mac was asleep or off, or the "
                    "run failed). These games locked without a lineup check: TB@DAL, PHI@JAX, CHI@GB, CIN@MIA and 1 more. "
                    "Nothing to do now."]


def test_missed_sunday_is_reported_on_tuesday_morning(env, games):
    install_at(at(2026, 10, 1, 0, 0))
    sent = []
    jobs.run_due(at(2026, 10, 8, 12, 5), games, _gen([]), sent.append)
    jobs.run_due(at(2026, 10, 10, 8, 5), games, _gen([]), sent.append)
    jobs.run_due(at(2026, 10, 13, 9, 0), games, _gen([]), sent.append)  # asleep Sat 8 AM to Tue 9 AM
    assert sent[2].startswith("Booth didn't get the Sunday final lineup pass to you for week 5")
    assert "PHI@JAX, CHI@GB, CIN@MIA, BUF@LA" in sent[2] and "TB@DAL" not in sent[2]
    assert sent[3] == "report tue 6" and len(sent) == 4
    jobs.run_due(at(2026, 10, 13, 9, 30), games, _gen([]), sent.append)
    assert len(sent) == 4


def test_stale_built_report_is_rebuilt(env, games):
    install_at(at(2026, 10, 11, 0, 0))
    calls, sent = [], []

    def send(msg):
        if not calls[1:]:
            raise RuntimeError("Messages timed out")
        sent.append(msg)

    jobs.run_due(at(2026, 10, 11, 8, 5), games, _gen(calls), send, notify_fn=lambda t: None)
    assert jobs.run_due(at(2026, 10, 12, 19, 0), games, _gen(calls), send) == "delivered sun week 5"
    assert calls == [("sun", 5), ("sun", 5)]  # rebuilt with Monday's news, not Sunday morning's


def test_late_label_uses_send_time(env, games):
    install_at(at(2026, 10, 11, 0, 0))
    # Started 9:20 Sunday, finished 9:34: PHI@JAX (9:30) kicked off while the report was being built.
    clock = iter([at(2026, 10, 11, 9, 34)])
    sent = []
    out = jobs.run_due(at(2026, 10, 11, 9, 20), games, _gen([]), sent.append, clock=lambda: next(clock))
    assert out == "delivered sun week 5" and sent[0].startswith("LATE: sent Sun 9:34 AM ET, after kickoff of PHI@JAX.")
    saved = jobs._bookkeeping(2026, 5)["sun"]
    assert saved["delivered_at"] == at(2026, 10, 11, 9, 34).isoformat()


def test_report_finished_after_its_window_is_not_sent(env, games):
    # The Mac slept mid-run and woke after Monday night's kickoff: no report, one notice.
    install_at(at(2026, 10, 11, 0, 0))
    clock = iter([at(2026, 10, 13, 7, 0)])
    sent = []
    out = jobs.run_due(at(2026, 10, 12, 20, 0), games, _gen([]), sent.append, clock=lambda: next(clock))
    assert "too late to send" in out
    assert len(sent) == 1 and sent[0].startswith("Booth didn't get the Sunday final lineup pass to you for week 5")
    jobs.run_due(at(2026, 10, 13, 7, 30), games, _gen([]), sent.append)
    assert len(sent) == 1


def test_lock_busy_skips_but_other_errors_alert(env, games):
    sent = []
    with jobs.lock():
        assert jobs.run_due(at(2026, 10, 13, 8, 5), games, _gen([]), sent.append) == "skipped: another run in progress"
    assert sent == []

    def in_progress_error(run, week, now):
        raise RuntimeError("The 1:00 PM games are already in progress")

    out = jobs.run_due(at(2026, 10, 13, 8, 10), games, in_progress_error, sent.append)
    assert out.startswith("failed tue week 6") and "couldn't build" in sent[0]


def test_lock_is_released_when_the_process_is_killed(env):
    import signal
    import subprocess
    import sys
    import time

    holder = subprocess.Popen(
        [sys.executable, "-c", "from booth import jobs; import time\nwith jobs.lock():\n    print('held', flush=True); time.sleep(60)"],
        stdout=subprocess.PIPE, text=True, env={**__import__("os").environ},
    )
    assert holder.stdout.readline().strip() == "held"
    with pytest.raises(jobs.LockBusy):
        with jobs.lock():
            pass
    holder.send_signal(signal.SIGTERM)
    holder.wait(timeout=10)
    time.sleep(0.1)
    with jobs.lock():
        pass


def test_corrupt_week_file_is_set_aside(env, games):
    path = env / "state" / "2026-wk06.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"season": 2026, "week"')  # truncated by a full disk
    sent = []
    assert jobs.run_due(at(2026, 10, 13, 8, 5), games, _gen([]), sent.append) == "delivered tue week 6"
    assert list(path.parent.glob("2026-wk06.json.corrupt-*"))


def test_failed_delivery_alerts_by_notification(env, games):
    notes = []

    def broken_send(msg):
        raise RuntimeError("Messages: not authorized to send Apple events")

    out = jobs.run_due(at(2026, 10, 13, 8, 5), games, _gen([]), broken_send, notify_fn=notes.append)
    assert out.startswith("failed tue week 6")
    assert len(notes) == 1 and "built the Tuesday waiver report" in notes[0]
    jobs.run_due(at(2026, 10, 13, 8, 35), games, _gen([]), broken_send, notify_fn=notes.append)
    assert len(notes) == 1  # alerted once, still retrying the send


def test_manual_send_is_recorded_only_for_the_due_report(env, games):
    assert jobs.record_manual_delivery("tue", 6, at(2026, 10, 13, 9, 0), games)
    sent = []
    assert jobs.run_due(at(2026, 10, 13, 9, 30), games, _gen([]), sent.append) == "tue week 6 already delivered"
    # A Sunday report sent by hand on Saturday doesn't stop Sunday's scheduled one.
    assert not jobs.record_manual_delivery("sun", 6, at(2026, 10, 17, 9, 0), games)


def _week12():
    rows = [("12", "2026-11-25", "Wednesday", "20:00", "GB", "LA"), ("12", "2026-11-26", "Thursday", "13:00", "CHI", "DET"),
            ("12", "2026-11-29", "Sunday", "13:00", "NYG", "PHI"), ("12", "2026-11-30", "Monday", "20:15", "WAS", "SF")]
    return [{"game_id": f"2026_12_{a}_{h}", "season": "2026", "game_type": "REG", "week": w, "gameday": d, "weekday": wd,
             "gametime": t, "away_team": a, "home_team": h, "location": "Home", "stadium": "", "stadium_id": "",
             "roof": "dome", "spread_line": "", "total_line": "", "home_score": ""} for w, d, wd, t, a, h in rows]


def test_wednesday_game_moves_first_lineup_check():
    games = _week12()
    assert jobs.scheduled_at("thu", 12, games) == at(2026, 11, 25, 12)
    assert jobs.expires_at("tue", 12, games) == at(2026, 11, 25, 12)
    assert jobs.due_run(at(2026, 11, 25, 12, 5), games) == ("thu", 12)


def test_sunday_report_still_sent_before_sunday_night_game(env, games):
    sent = []
    out = jobs.run_due(at(2026, 10, 18, 19, 0), games, _gen([]), sent.append)
    assert out == "delivered sun week 6" and sent[0].startswith("LATE:")


def test_report_windows(games):
    assert jobs.expires_at("tue", 6, games) == at(2026, 10, 15, 12)
    assert jobs.expires_at("sun", 6, games) == at(2026, 10, 18, 20, 20)  # no Monday game in the fixture
    assert jobs.expires_at("sun", 5, games) == at(2026, 10, 12, 20, 15)  # Monday night


def test_pmset_wake_is_just_after_the_8am_reports():
    assert schedule.pmset_wake_command(ZoneInfo("America/New_York")) == "pmset repeat wakeorpoweron TSU 08:01:00"
    assert schedule.pmset_wake_command(ZoneInfo("America/Los_Angeles")) == "pmset repeat wakeorpoweron TSU 05:01:00"


def test_plist_path_includes_uv_dir(monkeypatch):
    monkeypatch.setattr(schedule, "_bin", lambda name: f"/opt/tools/bin/{name}")
    assert schedule.plist_dict()["EnvironmentVariables"]["PATH"].startswith("/opt/tools/bin:")


def test_early_failures_alert_at_most_every_few_hours(env, games, monkeypatch):
    def broken(season, week):
        raise PermissionError("state folder not writable")

    monkeypatch.setattr(jobs, "_bookkeeping", broken)
    sent = []
    for mm in (5, 35):
        assert jobs.run_due(at(2026, 10, 13, 8, mm), games, _gen([]), sent.append).startswith("failed tue week 6")
    assert len(sent) == 1 and "error before the Tuesday waiver report" in sent[0]


def test_unwritable_log_never_blocks_an_alert(env, games):
    (env / "logs").write_text("not a folder")

    def boom(run, week, now):
        raise RuntimeError("claude not logged in")

    sent = []
    jobs.run_due(at(2026, 10, 13, 8, 5), games, boom, sent.append)
    assert len(sent) == 1 and "couldn't build" in sent[0]


def test_install_records_first_install(env, monkeypatch, tmp_path):
    (jobs.state_dir() / "installed_at").unlink()
    monkeypatch.setattr(schedule, "_bin", lambda name: f"/Users/x/.local/bin/{name}")
    monkeypatch.setattr(schedule, "PLIST", tmp_path / "com.booth.scheduler.plist")
    monkeypatch.setattr(schedule, "ROOT", tmp_path)
    monkeypatch.setattr(schedule, "_launchctl", lambda *a: type("R", (), {"returncode": 1 if a[0] == "print" else 0, "stderr": ""})())
    schedule.install()
    assert jobs.installed_at() is not None



def test_status_shows_every_check_not_just_reports(monkeypatch, tmp_path):
    monkeypatch.setattr(schedule, "ROOT", tmp_path)
    out = "\n".join(["\tstate = not running", "\truns = 3", "\tlast exit code = 0", "\tpath = x"])
    monkeypatch.setattr(schedule, "_launchctl", lambda *a: type("R", (), {"returncode": 0, "stdout": out, "stderr": ""})())
    text = schedule.status()
    assert "runs = 3" in text and "path = x" not in text
    assert text.count("(none yet)") == 2
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "launchd.out.log").write_text("".join(f"2026-10-05 18:{m:02d}:00 EDT  nothing due\n" for m in range(10)))
    text = schedule.status()
    assert "18:09:00 EDT  nothing due" in text and "18:04:00" not in text
    assert text.count("(none yet)") == 1


def _fails_n_times(n, calls=None):
    left = [n]

    def gen(run, week, now):
        if left[0] > 0:
            left[0] -= 1
            raise RuntimeError("overloaded")
        return _gen(calls if calls is not None else [])(run, week, now)
    return gen


def test_a_saturday_give_up_doesnt_hide_a_missed_sunday(env, games):
    sent = []
    jobs.run_due(at(2026, 10, 15, 12, 5), games, _gen([]), sent.append)
    for i in range(jobs.MAX_ATTEMPTS):  # Saturday's report fails every time
        jobs.run_due(at(2026, 10, 17, 8, 0) + timedelta(minutes=30 * i), games, _fails_n_times(1), sent.append)
    assert any("gave up on the Saturday" in m for m in sent)
    jobs.run_due(at(2026, 10, 20, 9, 0), games, _gen([]), sent.append)  # asleep until Tuesday
    assert sum("didn't get the Sunday final lineup pass to you for week 6" in m for m in sent) == 1


def test_missed_notice_isnt_repeated_when_the_week_file_cant_be_saved(env, games, monkeypatch):
    sent = []
    jobs.run_due(at(2026, 10, 15, 12, 5), games, _gen([]), sent.append)

    def no_disk(data, base=None):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(jobs, "write_week", no_disk)
    for hh in (21, 22, 23):  # Sunday night, after the last game, with the Mac asleep all weekend
        jobs.run_due(at(2026, 10, 18, hh, 0), games, _gen([]), sent.append)
    assert sum("didn't get" in m for m in sent) == 1


def test_an_alert_that_didnt_get_through_is_retried(env, games):
    sent, notes, tries = [], [], []

    def send(msg):
        tries.append(msg)
        if len(tries) == 1:
            raise RuntimeError("Messages hiccup")
        sent.append(msg)

    def notify(msg):
        if not notes:
            notes.append(None)
            raise RuntimeError("no notifications")

    boom = _fails_n_times(99)
    jobs.run_due(at(2026, 10, 15, 12, 0), games, boom, send, notify_fn=notify)
    jobs.run_due(at(2026, 10, 15, 12, 30), games, boom, send, notify_fn=notify)
    assert len(sent) == 1 and "couldn't build the Thursday" in sent[0]


def test_unusable_state_folder_still_alerts(env, games, monkeypatch):
    (env / "blocker").write_text("a file where the state folder should be")
    monkeypatch.setenv("BOOTH_STATE_DIR", str(env / "blocker" / "state"))
    sent = []
    out = jobs.run_due(at(2026, 10, 13, 8, 5), games, _gen([]), sent.append)
    assert out.startswith("failed tue week 6") and "error before the Tuesday" in sent[0]


def _with_weeks_7_and_8(games):
    out = list(games)
    for shift in (1, 2):
        for g in games:
            if g["week"] != "6":
                continue
            d = (date.fromisoformat(g["gameday"]) + timedelta(days=7 * shift)).isoformat()
            out.append({**g, "week": str(6 + shift), "gameday": d, "game_id": g["game_id"].replace("_06_", f"_{6 + shift:02d}_")})
    return out


def test_long_absence_reports_every_missed_week_in_one_message(env, games):
    games = _with_weeks_7_and_8(games)
    sent = []
    jobs.run_due(at(2026, 10, 15, 12, 5), games, _gen([]), sent.append)
    jobs.run_due(at(2026, 10, 27, 9, 0), games, _gen([]), sent.append)  # asleep Thu wk 6 to Tue wk 8
    notices = [m for m in sent if "didn't get" in m]
    assert len(notices) == 1 and "- Week 6: the Sunday final lineup pass" in notices[0] and "- Week 7:" in notices[0]
    assert sent[-1] == "report tue 8"


def test_failed_rebuild_sends_the_earlier_version(env, games):
    calls, sent = [], []
    failed = []

    def send(msg):
        if not failed:
            failed.append(msg)
            raise RuntimeError("Messages timed out")
        sent.append(msg)

    jobs.run_due(at(2026, 10, 18, 8, 0), games, _gen(calls), send, notify_fn=lambda t: None)
    out = jobs.run_due(at(2026, 10, 18, 12, 20), games, _fails_n_times(99), send)  # HOU@JAX played; Claude down
    assert out == "delivered sun week 6"
    assert "(Built Sun 8:00 AM ET; some games have kicked off since.)" in sent[-1] and sent[-1].endswith("report sun 6")


def test_no_rebuild_right_before_a_kickoff(env, games):
    calls, sent = [], []
    failed = []

    def send(msg):
        if not failed:
            failed.append(msg)
            raise RuntimeError("Messages timed out")
        sent.append(msg)

    jobs.run_due(at(2026, 10, 18, 8, 0), games, _gen(calls), send, notify_fn=lambda t: None)
    jobs.run_due(at(2026, 10, 18, 20, 0), games, _gen(calls), send)  # 20 minutes before the night game
    assert calls == [("sun", 6)] and sent[-1].endswith("report sun 6")
