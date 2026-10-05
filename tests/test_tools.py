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
