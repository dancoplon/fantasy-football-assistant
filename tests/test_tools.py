import json
import asyncio
from unittest import mock

import pytest

from booth import mcp_server
from booth.config import Settings
from booth.manual import manual_roster
from booth.yahoo import YahooAccessError, YahooClient, parse_scoreboard, summarize_transaction

ME = "461.l.890283.t.7"
OPP = "461.l.890283.t.3"


def _team(key, name, pts, proj):
    return {
        "team": [
            [{"team_key": key}, {"team_id": key[-1]}, {"name": name}, [], {"url": "x"}],
            {"team_points": {"coverage_type": "week", "week": "5", "total": pts},
             "team_projected_points": {"coverage_type": "week", "week": "5", "total": proj},
             "win_probability": 0.62},
        ]
    }


SCOREBOARD = {
    "fantasy_content": {
        "league": [
            {"league_key": "461.l.890283"},
            {"scoreboard": {"0": {"matchups": {
                "0": {"matchup": {"week": "5", "week_start": "2026-10-08", "week_end": "2026-10-12",
                                  "status": "midevent",
                                  "0": {"teams": {"0": _team(ME, "Mayor of Titty City", "12.40", "118.20"),
                                                  "1": _team(OPP, "Opponent", "0.00", "104.75"),
                                                  "count": 2}}}},
                "1": {"matchup": {"week": "5", "status": "midevent",
                                  "0": {"teams": {"0": _team("461.l.890283.t.1", "A", "1", "2"),
                                                  "1": _team("461.l.890283.t.2", "B", "3", "4"),
                                                  "count": 2}}}},
                "count": 2}}, "week": "5"}},
        ]
    }
}

TXN = {
    "transaction_key": "461.l.890283.tr.200", "type": "add/drop", "status": "successful",
    "timestamp": "1760511600", "faab_bid": "7",
    "players": {
        "0": {"player": [
            [{"player_key": "461.p.1"}, {"player_id": "1"}, {"name": {"full": "Some Back"}},
             {"editorial_team_abbr": "NYJ"}, {"display_position": "RB"}],
            {"transaction_data": [{"type": "add", "source_type": "waivers",
                                   "destination_type": "team", "destination_team_name": "MOTC"}]},
        ]},
        "1": {"player": [
            [{"player_key": "461.p.2"}, {"player_id": "2"}, {"name": {"full": "Cut Guy"}},
             {"editorial_team_abbr": "LV"}, {"display_position": "WR"}],
            {"transaction_data": {"type": "drop", "source_type": "team", "source_team_name": "MOTC",
                                  "destination_type": "waivers"}},
        ]},
        "count": 2,
    },
}


def test_parse_scoreboard():
    matchups = parse_scoreboard(SCOREBOARD)
    assert len(matchups) == 2
    first = matchups[0]
    assert first["week"] == "5" and first["status"] == "midevent"
    assert first["teams"][0] == {"team_key": ME, "name": "Mayor of Titty City", "points": 12.4,
                                 "projected_points": 118.2, "win_probability": 0.62}


def test_summarize_transaction():
    s = summarize_transaction(TXN)
    assert s["faab_bid"] == "7"
    assert s["moves"][0] == {"player": "Some Back", "nfl_team": "NYJ", "position": "RB", "action": "add",
                             "source": "waivers", "to_team": "MOTC", "from_team": None}
    assert s["moves"][1]["action"] == "drop"
    assert s["time_et"].startswith("Wed Oct 15")


@pytest.fixture
def client():
    league = mock.Mock()
    league.league_id = "461.l.890283"
    league.team_key.return_value = ME
    league.matchups.return_value = SCOREBOARD
    team = mock.Mock()
    team.roster.return_value = [{"name": "Opp Guy"}]
    league.to_team.return_value = team
    settings = Settings("cid", "sec", "https://localhost:8000", mock.Mock(), "890283", "nfl")
    return YahooClient(settings, league=league)


def test_matchup_finds_my_game_and_opponent_roster(client):
    m = client.matchup()
    assert m["me"]["name"] == "Mayor of Titty City"
    assert m["opponent"]["team_key"] == OPP
    client.league.to_team.assert_called_with(OPP)
    assert m["opponent_roster"] == [{"name": "Opp Guy"}]


def test_standings_marks_my_team(client):
    client.league.standings.return_value = [{"team_key": OPP, "rank": 1}, {"team_key": ME, "rank": 11}]
    rows = client.standings()
    assert [r["is_me"] for r in rows] == [False, True]


def test_free_agents_pages_until_limit(client):
    page = object()
    client.league._players_from_page.side_effect = [(25, [{"n": i} for i in range(25)]),
                                                     (25, [{"n": i} for i in range(25, 50)])]
    client.league.yhandler.get.return_value = page
    out = client.free_agents(position="QB", limit=30)
    assert len(out) == 30
    first_uri = client.league.yhandler.get.call_args_list[0].args[0]
    assert "status=A" in first_uri and "position=QB" in first_uri and "sort=AR" in first_uri


def test_api_errors_become_access_errors(client):
    client.league.standings.side_effect = RuntimeError("401 Client Error: Forbidden")
    with pytest.raises(YahooAccessError, match="provisioned"):
        client.standings()


def test_manual_roster_matches_brief():
    r = manual_roster()
    assert r["source"] == "manual"
    assert len(r["players"]) == 25
    by_name = {p["name"]: p for p in r["players"]}
    assert by_name["Justin Jefferson"]["status"] == "O"
    assert {p["name"] for p in r["players"] if p["bye_week"] == 6} >= {"Joe Burrow", "Kyler Murray"}


def test_roster_tool_falls_back_to_manual_when_yahoo_fails(monkeypatch):
    def boom():
        raise YahooAccessError("401 not provisioned")
    monkeypatch.setattr(mcp_server, "client", boom)
    out = mcp_server.get_my_roster()
    assert out["source"] == "manual" and "provisioned" in out["yahoo_error"]


def test_other_tools_return_error_not_crash(monkeypatch):
    def boom():
        raise YahooAccessError("401 not provisioned")
    monkeypatch.setattr(mcp_server, "client", boom)
    assert "error" in mcp_server.get_standings()


def test_server_lists_read_only_tools():
    tools = asyncio.run(mcp_server.mcp.list_tools())
    assert {t.name for t in tools} == {"get_league_settings", "get_my_roster", "get_free_agents",
                                       "get_matchup", "get_transactions", "get_standings",
                                       "get_league_context", "get_schedule", "get_player_usage",
                                       "get_injury_report", "get_trending_players", "get_game_weather"}
    assert all(t.annotations.read_only_hint for t in tools)


def test_league_context_loads():
    ctx = mcp_server.get_league_context()
    assert ctx["league"]["scoring"]["reception"] == 0.5
    assert "Contending" in ctx["strategy"] or "contending" in ctx["strategy"]



@pytest.fixture
def saved_list(monkeypatch):
    from booth import manual
    monkeypatch.setattr(manual, "MANUAL_FREE_AGENTS", manual.ROOT / "config" / "manual_free_agents.example.json")
    return manual


def test_manual_free_agents_filters_by_position_and_expires(saved_list):
    from datetime import datetime, timezone
    fresh = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
    qbs = saved_list.manual_free_agents("QB", now=fresh)
    assert qbs["source"] == "manual" and qbs["as_of"].startswith("2026-10-05") and "offense only" in qbs["note"]
    assert [p["name"] for p in qbs["players"]] == ["Example Quarterback", "Example Backup QB"]
    assert {p["position"] for p in saved_list.manual_free_agents("W/R/T", now=fresh)["players"]} == {"RB", "WR", "TE"}
    assert {p["position"] for p in saved_list.manual_free_agents("wr, te", now=fresh)["players"]} == {"WR", "TE"}
    assert saved_list.manual_free_agents("DEF", now=fresh)["players"] == []
    assert len(saved_list.manual_free_agents(limit=2, now=fresh)["players"]) == 2
    assert saved_list.manual_free_agents(now=datetime(2026, 10, 12, 21, 38, tzinfo=timezone.utc)) is not None
    assert saved_list.manual_free_agents(now=datetime(2026, 10, 12, 21, 40, tzinfo=timezone.utc)) is None


def test_manual_free_agents_missing_file(saved_list, monkeypatch, tmp_path):
    monkeypatch.setattr(saved_list, "MANUAL_FREE_AGENTS", tmp_path / "none.json")
    assert saved_list.manual_free_agents() is None


def test_free_agent_tool_falls_back_to_saved_list(saved_list, monkeypatch):
    def boom():
        raise YahooAccessError("401 not provisioned")
    monkeypatch.setattr(mcp_server, "client", boom)
    monkeypatch.setattr(saved_list, "FREE_AGENTS_MAX_AGE", saved_list.timedelta(days=36500))
    out = mcp_server.get_free_agents(position="TE")
    assert out["source"] == "manual" and "provisioned" in out["yahoo_error"]
    assert [p["name"] for p in out["players"]] == ["Example Tight End"]
    monkeypatch.setattr(saved_list, "FREE_AGENTS_MAX_AGE", saved_list.timedelta(0))
    out = mcp_server.get_free_agents()
    assert "provisioned" in out["error"] and "saved list" in out["error"]


def _write(path, data):
    path.write_text(json.dumps(data))
    return path


def test_local_roster_copy_wins_and_goes_stale(monkeypatch, tmp_path):
    from datetime import datetime, timezone
    from booth import manual
    seed = json.loads(manual.MANUAL_ROSTER.read_text())
    local = dict(seed, as_of="2026-10-07T21:00:00-04:00", players=seed["players"][:24] + [
        {"name": "Aaron Rodgers", "position": "QB", "nfl_team": "PIT", "bye_week": 9, "status": "", "slot": "BN"}])
    monkeypatch.setattr(manual, "MANUAL_ROSTER_LOCAL", _write(tmp_path / "local.json", local))
    r = manual.manual_roster(now=datetime(2026, 10, 8, 16, tzinfo=timezone.utc))
    assert r["source"] == "manual" and "Aaron Rodgers" in {p["name"] for p in r["players"]} and "warning" not in r
    assert "Dan may have made moves" in manual.manual_roster(now=datetime(2026, 10, 16, tzinfo=timezone.utc))["warning"]


def test_matchup_copy_only_for_its_week(monkeypatch):
    from datetime import datetime, timezone
    from booth import manual
    monkeypatch.setattr(manual, "MANUAL_MATCHUP", manual.CONFIG / "manual_matchup.example.json")
    thu = datetime(2026, 10, 8, 16, tzinfo=timezone.utc)
    m = manual.manual_matchup(5, now=thu)
    assert m["source"] == "manual" and m["opponent"]["team_name"] == "Example Opponent" and "_note" not in m
    assert manual.manual_matchup(6, now=thu) is None
    assert manual.manual_matchup(None, now=thu)["week"] == 5
    assert manual.manual_matchup(None, now=datetime(2026, 10, 17, tzinfo=timezone.utc)) is None


def test_matchup_tool_falls_back_to_copy(monkeypatch):
    from booth import manual

    def boom():
        raise YahooAccessError("401 not provisioned")
    monkeypatch.setattr(mcp_server, "client", boom)
    monkeypatch.setattr(manual, "MANUAL_MATCHUP", manual.CONFIG / "manual_matchup.example.json")
    monkeypatch.setattr(manual, "MATCHUP_MAX_AGE", manual.timedelta(days=36500))
    monkeypatch.setattr(mcp_server.nflverse, "games", lambda: [])
    monkeypatch.setattr(mcp_server.nflverse, "current_week", lambda games: 5)
    out = mcp_server.get_matchup()
    assert out["source"] == "manual" and out["week"] == 5 and "provisioned" in out["yahoo_error"]
    out = mcp_server.get_matchup(week=6)
    assert "provisioned" in out["error"] and "matchup page" in out["error"]


def test_file_checks_catch_bad_copies():
    from booth import manual
    assert manual.check_roster({"players": [{"name": "A", "position": "QB", "nfl_team": "X"}]}) == [
        '"as_of" is missing or not an ISO date/time',
        "only 1 players: looks like part of the roster (a full one is about 25 plus IR)"]
    dupes = [{"name": "A", "position": "XX", "nfl_team": "X"}] * 2 + [{"name": f"P{i}", "position": "RB"} for i in range(20)]
    probs = manual.check_roster({"as_of": "2026-10-07", "players": dupes})
    assert any("position \"XX\"" in p for p in probs) and any("listed twice" in p for p in probs)
    assert any('missing "nfl_team"' in p for p in probs)
    assert manual.check_matchup({"as_of": "2026-10-08", "week": 19, "opponent": {}}) == [
        '"week" must be a whole number from 1 to 18', '"opponent.team_name" is missing']
    assert manual.check_matchup({"as_of": "x", "week": 5, "me": {"projected_points": "112"},
                                 "opponent": {"team_name": "T", "starters": [{"name": "Q"}]}}) == [
        '"as_of" is missing or not an ISO date/time', '"me.projected_points" must be a number',
        'opponent starter 1 (Q): missing "slot"']


def test_manual_status_summarizes_each_copy(monkeypatch, tmp_path):
    from datetime import datetime, timezone
    from booth import manual
    monkeypatch.setattr(manual, "MANUAL_FREE_AGENTS", manual.CONFIG / "manual_free_agents.example.json")
    monkeypatch.setattr(manual, "MANUAL_MATCHUP", manual.CONFIG / "manual_matchup.example.json")
    text = manual.status(now=datetime(2026, 10, 8, 16, tzinfo=timezone.utc), current_week=5)
    assert "Roster (manual_roster.json): 25 players" in text
    assert "Available players: 6 players" in text and "In use." in text and "Mon Oct 12 5:39 PM" in text
    assert "Matchup: week 5 vs Example Opponent" in text and "Not used" not in text and "problem:" not in text
    text = manual.status(now=datetime(2026, 10, 14, 16, tzinfo=timezone.utc), current_week=6)
    assert "STALE" in text and "EXPIRED" in text and "Not used: this is week 6." in text
    monkeypatch.setattr(manual, "MANUAL_MATCHUP", _write(tmp_path / "m.json", {"week": 5}))
    assert "problem:" in manual.status(current_week=5)


def test_unreadable_local_roster_falls_back_to_the_seed(monkeypatch, tmp_path):
    from booth import manual
    bad = tmp_path / "local.json"
    bad.write_text('{"as_of": "2026-10-07", "players": [')  # cut off mid-write
    monkeypatch.setattr(manual, "MANUAL_ROSTER_LOCAL", bad)
    r = manual.manual_roster()
    assert r["source"] == "manual" and len(r["players"]) == 25 and "couldn't be read" in r["warning"]

    def boom():
        raise YahooAccessError("401 not provisioned")
    monkeypatch.setattr(mcp_server, "client", boom)
    assert "couldn't be read" in mcp_server.get_my_roster()["warning"]


def test_local_roster_without_a_date_is_still_used(monkeypatch, tmp_path):
    from booth import manual
    seed = json.loads(manual.MANUAL_ROSTER.read_text())
    for as_of in (None, "Oct 7"):
        local = {k: v for k, v in seed.items() if k != "as_of"} | ({"as_of": as_of} if as_of else {})
        local["players"] = seed["players"][:24] + [{"name": "Aaron Rodgers", "position": "QB", "nfl_team": "PIT"}]
        monkeypatch.setattr(manual, "MANUAL_ROSTER_LOCAL", _write(tmp_path / "local.json", local))
        r = manual.manual_roster()
        assert "Aaron Rodgers" in {p["name"] for p in r["players"]} and "no readable as_of" in r["warning"]


@pytest.mark.parametrize("content", ['{"week": 5, "as_of": ', '{"week": "five", "as_of": "2026-10-08"}', "[]"])
def test_unreadable_matchup_or_list_counts_as_missing(monkeypatch, tmp_path, content):
    from booth import manual
    (tmp_path / "m.json").write_text(content)
    monkeypatch.setattr(manual, "MANUAL_MATCHUP", tmp_path / "m.json")
    monkeypatch.setattr(manual, "MANUAL_FREE_AGENTS", tmp_path / "m.json")
    assert manual.manual_matchup(5) is None and manual.manual_matchup(None) is None
    assert manual.manual_free_agents() is None


@pytest.mark.parametrize("which", ["MANUAL_ROSTER_LOCAL", "MANUAL_MATCHUP", "MANUAL_FREE_AGENTS"])
def test_manual_status_fails_on_a_broken_copy(monkeypatch, tmp_path, capsys, which):
    from booth import cli, manual
    (tmp_path / "x.json").write_text("{not json")
    monkeypatch.setattr(manual, which, tmp_path / "x.json")
    monkeypatch.setattr(mcp_server.nflverse, "games", lambda: [])
    monkeypatch.setattr(mcp_server.nflverse, "current_week", lambda games: 5)
    assert cli.main(["manual-status"]) == 1
    assert "problem:" in capsys.readouterr().out


def test_faab_market_summarizes_the_example(monkeypatch):
    from datetime import datetime, timezone
    from booth import manual
    monkeypatch.setattr(manual, "FAAB_MARKET", manual.CONFIG / "faab_market.example.json")
    m = manual.faab_market(now=datetime(2026, 10, 1, 12, tzinfo=timezone.utc))  # before the Wed claims run
    assert m["source"] == "manual" and m["dan_remaining"] == 72 and "warning" not in m
    assert m["league_spent"] == 90 and m["teams_with_nothing_spent"] == 1
    assert [t["team"] for t in m["teams"]] == ["Example Spender", "My Team", "Example Saver"]
    assert m["teams"][0]["spent"] == 62
    assert [c["bid"] for c in m["recent_claims"]] == [22, 9, 3]
    assert m["by_position"]["QB"] == {"claims": 1, "top": 9, "median": 9}
    assert [c["player"] for c in m["dan_outbid"]] == ["Example Quarterback"]
    assert "_note" not in m
    assert manual.check_faab_market(json.loads((manual.CONFIG / "faab_market.example.json").read_text())) == []


def test_faab_market_keeps_old_prices_and_flags_budgets_after_a_claims_run(monkeypatch, tmp_path):
    from datetime import datetime, timezone
    from booth import manual
    claims = [{"week": w, "player": f"P{w}{i}", "position": "WR", "nfl_team": "X", "bid": b, "team": "T"}
              for w, bids in ((1, [30]), (2, [2, 4]), (3, [6]), (4, [8]), (5, [10, 1])) for i, b in enumerate(bids)]
    data = {"as_of": "2026-10-06T12:00:00-04:00", "starting_budget": 100,  # Tuesday, before that week's run
            "teams": [{"team": "Me", "remaining": 95, "is_me": True}], "claims": claims}
    monkeypatch.setattr(manual, "FAAB_MARKET", _write(tmp_path / "f.json", data))
    m = manual.faab_market(now=datetime(2026, 10, 7, 7, 30, tzinfo=timezone.utc))  # Wed 3:30 AM ET
    assert {c["week"] for c in m["recent_claims"]} == {3, 4, 5}  # the last 3 weeks one by one
    assert m["by_position"]["WR"] == {"claims": 7, "top": 30, "median": 6}  # but every week in the ranges
    assert "warning" not in m
    m = manual.faab_market(now=datetime(2026, 10, 7, 7, 45, tzinfo=timezone.utc))  # Wed 3:45 AM ET
    assert "before the latest Wednesday claims run" in m["warning"]


def test_last_claims_run():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from booth import manual
    et = ZoneInfo("America/New_York")
    assert manual.last_claims_run(datetime(2026, 10, 13, 8, tzinfo=et)) == datetime(2026, 10, 7, 3, 40, tzinfo=et)
    assert manual.last_claims_run(datetime(2026, 10, 7, 3, 40, tzinfo=et)) == datetime(2026, 10, 7, 3, 40, tzinfo=et)
    assert manual.last_claims_run(datetime(2026, 10, 7, 3, 39, tzinfo=et)) == datetime(2026, 9, 30, 3, 40, tzinfo=et)


@pytest.mark.parametrize("bad", [
    {"claims": [{"player": "P", "position": "WR", "bid": 11, "team": "T"}]},  # no week
    {"claims": [{"week": "Week 5", "player": "P", "position": "WR", "bid": 11, "team": "T"}]},
    {"teams": [{"team": "Me", "remaining": "$95", "is_me": True}]},
    {"teams": [{"team": "Me", "remaining": None, "is_me": True}]},
    {"starting_budget": "100"},
    {"as_of": "Oct 7"},
    {"claims": ["not a claim"]},
])
def test_one_bad_entry_doesnt_lose_the_market(monkeypatch, tmp_path, bad):
    from booth import manual
    good = json.loads((manual.CONFIG / "faab_market.example.json").read_text())
    data = {**good, **{k: v + good[k] if isinstance(v, list) else v for k, v in bad.items()}}
    monkeypatch.setattr(manual, "FAAB_MARKET", _write(tmp_path / "f.json", data))
    m = manual.faab_market()
    assert m is not None and m["by_position"]["RB"]["top"] == 22
    assert "problem:" in manual.status(current_week=5)


def test_faab_market_missing_or_unreadable(monkeypatch, tmp_path):
    from booth import manual
    assert manual.faab_market() is None  # conftest points it at an empty folder
    assert "No copy" in mcp_server.get_league_context()["faab_market"]["note"]
    bad = tmp_path / "f.json"
    bad.write_text('{"as_of": "2026-10-07", "teams": [')
    monkeypatch.setattr(manual, "FAAB_MARKET", bad)
    assert manual.faab_market() is None
    assert "couldn't be read" in mcp_server.get_league_context()["faab_market"]["note"]
    for junk in ({"as_of": "2026-10-07"}, [], {"teams": "x", "claims": {"a": 1}}):
        monkeypatch.setattr(manual, "FAAB_MARKET", _write(tmp_path / "g.json", junk))
        assert manual.faab_market() is None
        manual.status(current_week=5)  # never crashes


def test_faab_market_check_catches_bad_copies():
    from booth import manual
    probs = manual.check_faab_market({
        "as_of": "2026-10-07", "starting_budget": 100,
        "teams": [{"team": "A", "remaining": 120}, {"remaining": 50}, "x"],
        "claims": [{"week": 0, "player": "P", "position": "WR", "team": "A", "bid": "13"},
                   {"week": 5, "player": "Q", "position": "FB", "bid": 3}]})
    assert probs == [
        'team 1 (A): "remaining" must be a number from 0 to 100',
        'team 2 (?): missing "team"',
        "team 3: not an object",
        'mark exactly one team as Dan\'s ("is_me": true)',
        'claim 1 (P): "week" must be a whole number from 1 to 18',
        'claim 1 (P): "bid" must be a dollar amount',
        'claim 2 (Q): missing "team"',
        'claim 2 (Q): position "FB" is not one of [\'DEF\', \'K\', \'QB\', \'RB\', \'TE\', \'WR\']',
    ]
    assert manual.check_faab_market({"as_of": "2026-10-07"}) == ['"teams" must be a non-empty list']


def test_manual_status_reports_the_faab_market(monkeypatch, tmp_path):
    from datetime import datetime, timezone
    from booth import manual
    assert "FAAB market: not there." in manual.status(current_week=5)
    monkeypatch.setattr(manual, "FAAB_MARKET", manual.CONFIG / "faab_market.example.json")
    text = manual.status(now=datetime(2026, 10, 1, 12, tzinfo=timezone.utc), current_week=4)
    assert "FAAB market: 3 winning bids (latest week 4), 3 team budgets as of 2026-09-30T09:00:00-04:00" in text
    assert "STALE" not in text.split("FAAB market")[1]
    text = manual.status(now=datetime(2026, 10, 8, 16, tzinfo=timezone.utc), current_week=5)
    assert "Budgets STALE: send a fresh league page." in text
    monkeypatch.setattr(manual, "FAAB_MARKET", _write(tmp_path / "f.json", {"as_of": "2026-10-07", "teams": []}))
    text = manual.status(current_week=5)
    assert "Not used by reports until fixed." in text and '  problem: "teams" must be a non-empty list' in text


def test_league_context_includes_the_faab_market(monkeypatch):
    from booth import manual
    monkeypatch.setattr(manual, "FAAB_MARKET", manual.CONFIG / "faab_market.example.json")
    assert mcp_server.get_league_context()["faab_market"]["dan_remaining"] == 72


def test_roster_adds_faab_slots_and_team_name(client):
    team = mock.Mock()
    team.roster.return_value = [{"name": "Joe Burrow", "selected_position": "QB", "editorial_team_abbr": "Cin"}]
    client.league.to_team.return_value = team
    client.league.teams.return_value = {ME: {"team_key": ME, "name": "Mayor of Titty City",
                                             "faab_balance": "95", "waiver_priority": 4}}
    out = client.my_roster()
    assert out["source"] == "yahoo" and out["as_of"]
    assert out["faab_remaining"] == 95 and out["waiver_priority"] == 4
    assert out["team_name"] == "Mayor of Titty City"
    assert out["players"][0]["slot"] == "QB"


def test_roster_survives_missing_team_info(client):
    team = mock.Mock()
    team.roster.return_value = []
    client.league.to_team.return_value = team
    client.league.teams.side_effect = RuntimeError("boom")
    out = client.my_roster()
    assert "faab_remaining" not in out and out["players"] == []


def test_standings_add_each_teams_faab(client):
    client.league.standings.return_value = [{"team_key": OPP}, {"team_key": ME}]
    client.league.teams.return_value = {OPP: {"faab_balance": "50"}, ME: {"faab_balance": "95"}}
    rows = client.standings()
    assert [r["faab_remaining"] for r in rows] == [50, 95]


def test_free_agents_never_sends_lastweek(client):
    client.league._players_from_page.return_value = (3, [{"n": 1}])
    client.free_agents(limit=5, sort_type="lastweek")
    client.free_agents(limit=5)
    client.free_agents(limit=5, sort_type="season")
    uris = [c.args[0] for c in client.league.yhandler.get.call_args_list]
    assert "sort_type=lastmonth" in uris[0] and "sort_type=lastmonth" in uris[1]
    assert "sort_type=season" in uris[2]
    assert not any("lastweek" in u for u in uris)


def test_team_codes_and_bye_weeks():
    from booth.data import nflverse
    games = [{"week": "1", "home_team": "CIN", "away_team": "LA"},
             {"week": "1", "home_team": "JAX", "away_team": "WAS"},
             {"week": "2", "home_team": "CIN", "away_team": "JAX"},
             {"week": "3", "home_team": "LA", "away_team": "WAS"}]
    byes = nflverse.bye_weeks(games)
    assert byes == {"LA": 2, "WAS": 2, "CIN": 3, "JAX": 3}
    assert [nflverse.team_code(a) for a in ("Cin", "LAR", "Jac", "Wsh", None)] == ["CIN", "LA", "JAX", "WAS", None]


def test_roster_tool_adds_nfl_team_and_bye(monkeypatch):
    from booth.data import nflverse
    fake = mock.Mock()
    fake.my_roster.return_value = {"source": "yahoo", "players": [{"name": "Joe Burrow", "editorial_team_abbr": "Cin"}]}
    monkeypatch.setattr(mcp_server, "client", lambda: fake)
    monkeypatch.setattr(nflverse, "games", lambda: [{"week": "5", "home_team": "CIN", "away_team": "MIA"},
                                                   {"week": "6", "home_team": "MIA", "away_team": "BUF"}])
    p = mcp_server.get_my_roster()["players"][0]
    assert p["nfl_team"] == "CIN" and p["bye_week"] == 6


def test_roster_tool_without_schedule_still_returns(monkeypatch):
    from booth.data import nflverse
    from booth.data.cache import DataSourceError
    fake = mock.Mock()
    fake.my_roster.return_value = {"source": "yahoo", "players": [{"name": "X", "editorial_team_abbr": "Buf"}]}
    monkeypatch.setattr(mcp_server, "client", lambda: fake)

    def down():
        raise DataSourceError("offline")
    monkeypatch.setattr(nflverse, "games", down)
    p = mcp_server.get_my_roster()["players"][0]
    assert p["nfl_team"] == "BUF" and "bye_week" not in p
