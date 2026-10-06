from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from booth import cli, inactives, jobs
from booth.data import nflverse
from booth.state import write_week

FIX = Path(__file__).parent / "fixtures"
ET = ZoneInfo("America/New_York")
KICKOFF = datetime(2026, 10, 18, 16, 25, tzinfo=ET)  # BUF @ LV, week 6


def at(hh, mm=0, d=18):
    return datetime(2026, 10, d, hh, mm, tzinfo=ET)


def player(name, team, status="", projection=10.0):
    return {"name": name, "nfl_team": team, "status": status, "projection": projection}


LINEUP = {
    "QB": player("Josh Allen", "BUF"),
    "RB1": player("James Cook", "BUF", "Questionable"),
    "RB2": player("Ashton Jeanty", "LV"),
    "WR1": player("CeeDee Lamb", "DAL", "Questionable"),  # 8:20 PM game, not this window
    "WR2": player("Davante Adams", "LAR"),
    "TE": player("Brock Bowers", "LV"),
    "FLEX1": player("Nico Collins", "HOU"),
    "FLEX2": player("Khalil Shakir", "BUF"),
    "K": player("Tyler Bass", "BUF"),
    "DEF": player("Buffalo", "BUF", "Questionable"),
}


@pytest.fixture
def games():
    return nflverse.games(path=FIX / "games.csv")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOTH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("BOOTH_DRY_RUN", raising=False)
    monkeypatch.setattr(jobs, "ROOT", tmp_path)
    monkeypatch.setattr(inactives, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(inactives, "_official", lambda week, names: {})
    monkeypatch.setattr(inactives, "_roster_teams", lambda: {})
    marker = jobs.state_dir() / "installed_at"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(at(0, d=13).isoformat())
    return tmp_path


def save_lineup(lineup=LINEUP, run="sun", week=6):
    write_week({"season": nflverse.SEASON, "week": week, "runs": {run: {"run": run, "lineup": lineup}}})


def job(week=6):
    return jobs._bookkeeping(nflverse.SEASON, week)["inactives"][KICKOFF.isoformat()]


class Checker:
    """Stands in for the Claude run: returns the queued results (or raises them) in order."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, week, now, kickoff, players, games):
        self.calls.append({"week": week, "now": now, "kickoff": kickoff, "players": players})
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


INACTIVE = {
    "players": [{"name": "James Cook", "status": "inactive", "source": "https://example.com/buf"}],
    "message": "Lineup change before 4:25 PM ET:\nBench James Cook (INACTIVE). Start Ray Davis (RB, BUF, 4:25 PM).\n"
               "- Cook ruled out with an ankle injury.",
}
ACTIVE = {"players": [{"name": "James Cook", "status": "active"}], "message": ""}
UNKNOWN = {"players": [{"name": "James Cook", "status": "unknown"}], "message": ""}


def run(now, games, check, sent, notified=None, clock=None):
    return inactives.run_inactives(now, games, check, sent.append,
                                   (notified if notified is not None else []).append, clock)


@pytest.mark.parametrize("now,due", [
    (at(15, 4), []),
    (at(15, 5), [KICKOFF]),
    (at(16, 19), [KICKOFF]),
    (at(16, 20), []),
    (at(8, 15), [at(9, 30)]),  # the London game
])
def test_due_kickoff_window(games, now, due):
    assert inactives.due_kickoffs(now, 6, games) == due


def test_at_risk_picks_injured_starters_in_that_game():
    risky = inactives.at_risk(LINEUP, {"BUF", "LV"}, {}, {})
    assert [(p["slot"], p["name"], p["team"], p["status"]) for p in risky] == [
        ("RB1", "James Cook", "BUF", "Questionable")]  # not Lamb (later game) or the Bills defense


def test_at_risk_uses_the_official_report_and_its_team():
    lineup = {
        "WR1": player("Davante Adams", "LAR"),
        "WR2": {"name": "Jakobi Meyers", "status": "", "projection": 8.0},  # no team in the report
        "TE": player("Brock Bowers", "LV", "healthy"),
        "RB1": player("Ashton Jeanty", "LV", "Probable"),
    }
    official = {
        "davante adams": {"status": "Doubtful", "team": "LA"},
        "jakobi meyers": {"status": "Questionable", "team": "LV"},
        "brock bowers": {"status": "", "team": "LV"},
    }
    risky = inactives.at_risk(lineup, {"LA", "LV"}, official, {})
    assert [(p["name"], p["team"], p["status"]) for p in risky] == [
        ("Davante Adams", "LA", "Doubtful"), ("Jakobi Meyers", "LV", "Questionable")]


def test_at_risk_falls_back_to_the_roster_for_teams():
    lineup = {"WR1": {"name": "Jakobi Meyers", "status": "Q", "projection": 8.0}}
    assert inactives.at_risk(lineup, {"LV"}, {}, {"jakobi meyers": "LV"})[0]["team"] == "LV"
    assert inactives.at_risk(lineup, {"LV"}, {}, {}) == []


def test_nothing_due_outside_the_window(env, games):
    check, sent = Checker(), []
    assert run(at(14, 0), games, check, sent) == "inactives: nothing due"
    assert check.calls == [] and sent == []


def test_no_lineup_report_means_nothing_to_check(env, games):
    check, sent = Checker(), []
    assert run(at(15, 10), games, check, sent) == "inactives Sun 4:25 PM: no lineup report this week"
    assert run(at(15, 40), games, check, sent) == "inactives Sun 4:25 PM: no lineup report this week"
    assert check.calls == [] and sent == []


def test_healthy_starters_mean_no_claude_run(env, games):
    lineup = {**LINEUP, "RB1": player("James Cook", "BUF", "healthy")}
    save_lineup(lineup)
    check, sent = Checker(), []
    assert run(at(15, 10), games, check, sent).endswith("no starters with an injury designation")
    assert check.calls == [] and sent == []
    assert job()["done"]


def test_inactive_starter_is_sent_once(env, games):
    save_lineup()
    check, sent = Checker(INACTIVE), []
    assert run(at(15, 10), games, check, sent) == "inactives Sun 4:25 PM: sent: James Cook inactive"
    assert run(at(15, 40), games, check, sent) == "inactives Sun 4:25 PM: sent: James Cook inactive"
    assert len(check.calls) == 1 and len(sent) == 1
    assert [p["name"] for p in check.calls[0]["players"]] == ["James Cook"]
    assert sent[0] == (
        "Booth: inactives alert, week 6\n\n"
        "Lineup change before 4:25 PM ET:\n"
        "Bench James Cook (INACTIVE). Start Ray Davis (RB, BUF, 4:25 PM).\n"
        "- Cook ruled out with an ankle injury."
    )
    assert job()["sent"] == [at(15, 10).isoformat()] and job()["done"]


def test_dry_run_prefix(env, games, monkeypatch):
    monkeypatch.setenv("BOOTH_DRY_RUN", "1")
    save_lineup()
    sent = []
    run(at(15, 10), games, Checker(INACTIVE), sent)
    assert sent[0].startswith("[DRY RUN] Booth: inactives alert, week 6\n\n")


def test_falls_back_to_saturdays_lineup(env, games):
    save_lineup(run="sat")
    sent = []
    run(at(15, 10), games, Checker(INACTIVE), sent)
    assert len(sent) == 1


def test_all_active_sends_nothing(env, games):
    save_lineup()
    check, sent = Checker(ACTIVE), []
    assert run(at(15, 10), games, check, sent) == "inactives Sun 4:25 PM: all active"
    assert run(at(15, 40), games, check, sent) == "inactives Sun 4:25 PM: all active"
    assert len(check.calls) == 1 and sent == []


def test_not_announced_yet_checks_again(env, games):
    save_lineup()
    check, sent = Checker(UNKNOWN, ACTIVE), []
    assert run(at(15, 10), games, check, sent) == "inactives Sun 4:25 PM: not announced yet, will check again"
    assert not job().get("done")
    assert run(at(15, 40), games, check, sent) == "inactives Sun 4:25 PM: all active"
    assert len(check.calls) == 2 and sent == []


def test_still_unknown_at_the_last_check_says_so(env, games):
    save_lineup()
    check, sent = Checker(UNKNOWN, UNKNOWN), []
    run(at(15, 10), games, check, sent)
    assert run(at(15, 40), games, check, sent) == "inactives Sun 4:25 PM: sent: couldn't confirm James Cook"
    assert sent == ["Booth: inactives alert, week 6\n\nCouldn't confirm before the 4:25 PM ET kickoff: "
                    "James Cook (Questionable). Check the inactives yourself."]


def test_late_first_check_has_no_retry(env, games):
    save_lineup()
    check, sent = Checker(UNKNOWN), []
    assert run(at(16, 0), games, check, sent) == "inactives Sun 4:25 PM: sent: couldn't confirm James Cook"
    assert len(sent) == 1


def test_player_claude_skipped_counts_as_unknown(env, games):
    save_lineup()
    check, sent = Checker({"players": [], "message": ""}), []
    assert run(at(15, 10), games, check, sent).endswith("not announced yet, will check again")


def test_inactive_without_wording_still_goes_out(env, games):
    save_lineup()
    sent = []
    run(at(15, 10), games, Checker({**INACTIVE, "message": "  "}), sent)
    assert sent == ["Booth: inactives alert, week 6\n\nLineup change before 4:25 PM ET:\n"
                    "James Cook inactive. Swap in a healthy bench player."]


def test_not_sent_after_kickoff(env, games):
    save_lineup()
    sent = []
    out = run(at(16, 15), games, Checker(INACTIVE), sent, clock=lambda: at(16, 26))
    assert out == "inactives Sun 4:25 PM: kicked off before it could be sent"
    assert sent == [] and job()["done"]


def test_failed_check_retries_then_alerts_once(env, games):
    save_lineup()
    check, sent, notified = Checker(RuntimeError("claude timed out"), RuntimeError("claude timed out")), [], []
    assert run(at(15, 10), games, check, sent, notified).startswith("failed inactives Sun 4:25 PM")
    assert sent == []  # another try is coming
    assert run(at(15, 40), games, check, sent, notified).startswith("failed inactives Sun 4:25 PM")
    assert sent == ["Booth couldn't check inactives for the 4:25 PM ET games: James Cook (Questionable). "
                    "Check them yourself before kickoff."]
    assert run(at(16, 10), games, check, sent, notified) == "inactives Sun 4:25 PM: gave up after 2 checks"
    assert len(check.calls) == 2 and len(sent) == 1 and job()["last_error"] == "claude timed out"


def test_failed_send_is_retried_without_a_new_check(env, games):
    save_lineup()
    check, sent, attempts = Checker(INACTIVE), [], []

    def flaky_send(text):
        attempts.append(text)
        if len(attempts) == 1:
            raise RuntimeError("Messages didn't answer")
        sent.append(text)

    assert inactives.run_inactives(at(15, 10), games, check, flaky_send, lambda t: None).startswith("failed")
    assert inactives.run_inactives(at(15, 40), games, check, flaky_send, lambda t: None).endswith(
        "sent: James Cook inactive")
    assert len(check.calls) == 1 and len(sent) == 1 and sent[0] == attempts[0]


def test_last_chance_send_failure_falls_back_to_a_notification(env, games):
    save_lineup()
    notified = []

    def broken_send(text):
        raise RuntimeError("no network")

    inactives.run_inactives(at(16, 0), games, Checker(INACTIVE), broken_send, notified.append)
    assert len(notified) == 1 and "Bench James Cook (INACTIVE)" in notified[0]


def test_kickoff_before_install_is_skipped(env, games):
    (jobs.state_dir() / "installed_at").write_text(at(15, 30).isoformat())
    save_lineup()
    check, sent = Checker(INACTIVE), []
    assert run(at(15, 40), games, check, sent) == "inactives Sun 4:25 PM: before the schedule was installed"
    assert check.calls == []


def test_busy_lock_skips(env, games):
    save_lineup()
    check, sent = Checker(INACTIVE), []
    with jobs._Lock():
        assert run(at(15, 10), games, check, sent) == "inactives: skipped, another run in progress"
    assert check.calls == []


def test_prompt_lists_the_players(env, games):
    risky = [{"slot": "RB1", "name": "James Cook", "team": "BUF", "status": "Questionable"}]
    prompt = inactives.build_prompt(6, at(15, 10), KICKOFF, risky, games)
    assert "Inactives check for the 4:25 PM ET kickoff" in prompt
    assert "- James Cook (RB1, BUF): Questionable" in prompt
    assert "{" not in prompt.replace("{}", "")


@pytest.mark.parametrize("due,inact,code,line", [
    ("nothing due", "inactives: nothing due", 0, "nothing due"),
    ("nothing due", "inactives Sun 4:25 PM: all active", 0, "nothing due; inactives Sun 4:25 PM: all active"),
    ("delivered sun week 6", "failed inactives Sun 4:25 PM: boom", 1,
     "delivered sun week 6; failed inactives Sun 4:25 PM: boom"),
])
def test_run_due_command_reports_both(monkeypatch, capsys, due, inact, code, line):
    monkeypatch.setattr(jobs, "run_due", lambda: due)
    monkeypatch.setattr(inactives, "run_inactives", lambda: inact)
    assert cli.main(["run", "due"]) == code
    assert capsys.readouterr().out.rstrip().endswith(f"  {line}")


def test_run_due_command_survives_an_inactives_crash(monkeypatch, capsys):
    def boom():
        raise ValueError("bad schedule")

    monkeypatch.setattr(jobs, "run_due", lambda: "nothing due")
    monkeypatch.setattr(inactives, "run_inactives", boom)
    assert cli.main(["run", "due"]) == 1
    assert capsys.readouterr().out.rstrip().endswith("  nothing due; failed inactives: bad schedule")


def late_window_games(games):
    """Week 6 plus a 4:05 PM game, like most real Sundays (4:05 and 4:25 windows overlap)."""
    extra = dict(next(g for g in games if g["game_id"] == "2026_06_BUF_LV"),
                 game_id="2026_06_ARI_LA", away_team="ARI", home_team="LA", gametime="16:05")
    return games + [extra]


EARLY = datetime(2026, 10, 18, 16, 5, tzinfo=ET)


@pytest.mark.parametrize("phase", [0, 5, 10, 15, 20, 25])
def test_425_game_is_checked_even_with_a_405_game(env, games, phase):
    """Every launchd phase checks the 4:25 starters, and retries when the list isn't out."""
    games = late_window_games(games)
    save_lineup({**LINEUP, "WR2": player("Davante Adams", "LAR")})  # 4:05 starter, healthy
    check, sent = Checker(UNKNOWN, ACTIVE), []
    runs = [at(14, 30 + phase) + timedelta(minutes=30 * i) for i in range(5)]
    outcomes = [run(t, games, check, sent) for t in runs]
    assert any("4:05 PM: no starters with an injury designation" in o for o in outcomes)
    assert [c["kickoff"] for c in check.calls] == [KICKOFF] * len(check.calls) and check.calls
    if phase < 10:  # first 4:25 check by 3:10 or so leaves room for a second one
        assert len(check.calls) == 2 and job()["result"] == "all active"


def test_both_late_kickoffs_checked_in_one_run(env, games):
    games = late_window_games(games)
    save_lineup({**LINEUP, "WR2": player("Davante Adams", "LAR", "Questionable")})
    check, sent = Checker(ACTIVE | {"players": [{"name": "Davante Adams", "slot": "WR2", "status": "active"}]},
                          INACTIVE), []
    out = run(at(15, 10), games, check, sent)
    assert out == "inactives Sun 4:05 PM: all active; inactives Sun 4:25 PM: sent: James Cook inactive"
    assert [c["kickoff"] for c in check.calls] == [EARLY, KICKOFF] and len(sent) == 1


TWO_Q = {**LINEUP, "TE": player("Brock Bowers", "LV", "Questionable")}


def test_inactive_goes_out_now_and_the_unconfirmed_starter_is_rechecked(env, games):
    save_lineup(TWO_Q)
    first = {"players": [{"name": "James Cook", "slot": "RB1", "status": "inactive"},
                         {"name": "Brock Bowers", "slot": "TE", "status": "unknown"}],
             "message": INACTIVE["message"]}
    check, sent = Checker(first, {"players": [{"name": "Brock Bowers", "slot": "TE", "status": "active"}],
                                  "message": ""}), []
    assert run(at(15, 10), games, check, sent) == "inactives Sun 4:25 PM: sent: James Cook inactive; rest pending"
    assert "Bench James Cook (INACTIVE)" in sent[0]
    assert sent[0].endswith("Not announced yet: Brock Bowers (Questionable). Booth will check again before kickoff.")
    assert run(at(15, 40), games, check, sent) == "inactives Sun 4:25 PM: sent: follow-up"
    assert [p["name"] for p in check.calls[1]["players"]] == ["Brock Bowers"]  # only the one still open
    assert sent[1] == ("Booth: inactives alert, week 6\n\n"
                       "Now confirmed active: Brock Bowers (Questionable). No change needed.")
    assert run(at(16, 10), games, check, sent).endswith("sent: follow-up") and len(sent) == 2


def test_inactive_with_no_time_to_recheck_names_the_unconfirmed(env, games):
    save_lineup(TWO_Q)
    first = {"players": [{"name": "James Cook", "slot": "RB1", "status": "inactive"},
                         {"name": "Brock Bowers", "slot": "TE", "status": "unknown"}],
             "message": INACTIVE["message"]}
    sent = []
    assert run(at(16, 0), games, Checker(first), sent).endswith(
        "sent: James Cook inactive; couldn't confirm Brock Bowers")
    assert "Bench James Cook" in sent[0] and "Couldn't confirm before the 4:25 PM ET kickoff: Brock Bowers" in sent[0]


@pytest.mark.parametrize("said", [
    {"name": "James Cook (RB1, BUF)", "slot": "RB1", "status": "inactive"},
    {"name": "J. Cook", "slot": "RB1", "status": "inactive"},
    {"name": "Cook", "slot": "", "status": "inactive"},
])
def test_verdict_matches_echoed_names_and_slots(env, games, said):
    save_lineup()
    sent = []
    assert run(at(15, 10), games, Checker({"players": [said], "message": INACTIVE["message"]}), sent).endswith(
        "sent: James Cook inactive")


def test_a_written_swap_is_sent_even_if_names_dont_line_up(env, games):
    save_lineup()
    sent = []
    said = {"players": [{"name": "Jim Cook", "slot": "RB", "status": "inactive"}], "message": INACTIVE["message"]}
    assert run(at(15, 10), games, Checker(said), sent).endswith("sent: James Cook inactive")
    assert "Bench James Cook (INACTIVE)" in sent[0]
