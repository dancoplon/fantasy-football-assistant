import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from booth import report, state
from booth.data import nflverse
from booth.diff import change_header, diff_snapshots

FIX = Path(__file__).parent / "fixtures"


def _p(name, proj, status="healthy", team="X"):
    return {"name": name, "nfl_team": team, "status": status, "projection": proj}


def snap(run="thu", lineup=None, recs=None, wx=None, bench=None):
    lineup = lineup or {"QB": _p("Joe Burrow", 18.0), "RB1": _p("Jonathan Taylor", 16.0), "WR1": _p("Nico Collins", 14.0)}
    return {"season": 2026, "week": 5, "run": run, "lineup": lineup, "bench_flags": bench or [],
            "waiver_recs": recs or [], "weather_flags": wx or [], "sources": []}


def test_identical_snapshots_no_changes_even_if_prose_differs():
    a, b = snap(), snap("sat")
    a["message"], b["message"] = "Start Taylor, he's great", "Taylor remains a locked-in start"
    assert diff_snapshots(a, b) == []
    assert change_header([]) == "No changes. Lineup stands."


def test_slot_reshuffle_is_not_a_change():
    a = snap()
    b = snap("sat", lineup={"QB": _p("Joe Burrow", 18.0), "RB1": _p("Nico Collins", 14.0), "WR1": _p("Jonathan Taylor", 16.0)})
    assert diff_snapshots(a, b) == []


def test_lineup_status_projection_changes():
    a = snap()
    b = snap("sat", lineup={"QB": _p("Joe Burrow", 18.5), "RB1": _p("Jonathan Taylor", 11.0, "Q"), "WR1": _p("Rome Odunze", 12.0)})
    changes = diff_snapshots(a, b)
    assert "Lineup: start Rome Odunze; bench Nico Collins" in changes
    assert "Injury: Jonathan Taylor healthy → Q" in changes
    assert "Projection: Jonathan Taylor 16 → 11" in changes
    assert not any("Burrow" in c for c in changes)  # 0.5 swing is below threshold


def test_waiver_and_weather_changes():
    a = snap(recs=[{"add": "QB Guy", "drop": "Devin Singletary", "faab_bid": 8, "reasoning": "x"}],
             wx=[{"game": "CHI@GB", "note": "wind 21 mph"}])
    b = snap("sat", recs=[{"add": "QB Guy", "drop": "Devin Singletary", "faab_bid": 12, "reasoning": "reworded"},
                          {"add": "TE Guy", "drop": None, "faab_bid": 0, "reasoning": "y"}],
             wx=[{"game": "SEA@DEN", "note": "snow"}])
    changes = diff_snapshots(a, b)
    assert "FAAB bid on QB Guy: $8 → $12" in changes
    assert "New waiver rec: add TE Guy" in changes
    assert "Weather: SEA@DEN snow" in changes and "Weather cleared: CHI@GB" in changes


def test_state_store_roundtrip_and_prior(tmp_path):
    state.save_snapshot(snap("thu"), base=tmp_path)
    assert state.prior_snapshot(2026, 5, "sat", base=tmp_path)["run"] == "thu"
    assert state.prior_snapshot(2026, 5, "thu", base=tmp_path) is None
    state.save_snapshot(snap("sat"), base=tmp_path)
    assert state.prior_snapshot(2026, 5, "sun", base=tmp_path)["run"] == "sat"
    data = json.loads((tmp_path / "2026-wk05.json").read_text())
    assert set(data["runs"]) == {"thu", "sat"}
    assert (tmp_path / "latest.json").resolve() == (tmp_path / "2026-wk05.json").resolve()


def test_prompt_fills_placeholders():
    now = datetime(2026, 10, 13, 8, 0, tzinfo=ZoneInfo("America/New_York"))
    p = report.build_prompt("tue", 6, now, "Thursday 8:15 PM ET, SEA@DEN", "none yet")
    assert "week 6" in p and "Tuesday" in p and "8:00 AM ET" in p and "QB streamer" in p
    assert "{" not in p.replace("{}", "")


def test_schema_matches_prd_fields():
    req = set(report.SNAPSHOT_SCHEMA["required"])
    assert {"lineup", "bench_flags", "waiver_recs", "weather_flags", "sources", "message"} <= req


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    games = nflverse.games(path=FIX / "games.csv")
    monkeypatch.setattr(report.nflverse, "games", lambda: games)
    monkeypatch.setattr(report, "REPORTS", tmp_path / "reports")
    monkeypatch.setenv("BOOTH_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


def _fake_claude(lineup_qb_status="healthy", message="Start everyone."):
    def run(prompt):
        assert "week 5" in prompt
        return {"total_cost_usd": 0.42, "structured_output": {
            "message": message,
            "lineup": {"QB": _p("Joe Burrow", 18.0, lineup_qb_status), "RB1": _p("Jonathan Taylor", 16.0)},
            "bench_flags": [], "waiver_recs": [], "weather_flags": [], "sources": ["https://example.com/inj"]}}
    return run


def test_generate_thu_then_sat_adds_change_header(isolated):
    thu = datetime(2026, 10, 8, 12, 0, tzinfo=ZoneInfo("America/New_York"))
    r1 = report.generate("thu", now=thu, claude=_fake_claude())
    assert r1["message"].startswith("Booth: Thursday preliminary lineup check, week 5")
    assert "Changes since" not in r1["message"]

    sat = datetime(2026, 10, 10, 8, 0, tzinfo=ZoneInfo("America/New_York"))
    r2 = report.generate("sat", now=sat, claude=_fake_claude(message="Same lineup, reworded."))
    assert "No changes. Lineup stands." in r2["message"]

    sun = datetime(2026, 10, 11, 8, 0, tzinfo=ZoneInfo("America/New_York"))
    r3 = report.generate("sun", now=sun, claude=_fake_claude(lineup_qb_status="Q"))
    assert "Changes since last report:\n- Injury: Joe Burrow healthy → Q" in r3["message"]
    saved = json.loads((isolated / "state" / "2026-wk05.json").read_text())
    assert set(saved["runs"]) == {"thu", "sat", "sun"} and saved["runs"]["sun"]["cost_usd"] == 0.42
    assert (isolated / "reports" / "2026-wk05-sun.txt").exists()


def test_early_and_locked_games():
    games = nflverse.games(path=FIX / "games.csv")
    assert report.early_games_for(5, games) == "Thursday 8:15 PM ET, TB@DAL"
    sun_10am = datetime(2026, 10, 11, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    assert report.locked_games_at(5, games, sun_10am) == "TB@DAL; PHI@JAX"


def _stream(*events):
    return "\n".join(json.dumps(e) for e in events)


INIT_OK = {"type": "system", "subtype": "init", "mcp_servers": [{"name": "yahoo-fantasy", "status": "connected"}]}
TOOL = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "mcp__yahoo-fantasy__get_my_roster"}]}}
RESULT = {"type": "result", "subtype": "success", "is_error": False, "structured_output": {"message": "hi"}, "total_cost_usd": 0.3}


def test_parse_stream_accepts_a_report_built_from_booth_data():
    out = report.parse_stream(_stream(INIT_OK, TOOL, RESULT))
    assert out["structured_output"] == {"message": "hi"} and out["total_cost_usd"] == 0.3


@pytest.mark.parametrize("events,match", [
    ((dict(INIT_OK, mcp_servers=[{"name": "yahoo-fantasy", "status": "failed"}]), RESULT), "didn't start"),
    ((INIT_OK, RESULT), "without using any of Booth's data tools"),
    ((INIT_OK, TOOL, dict(RESULT, is_error=True, subtype="error_max_turns")), "error_max_turns"),
    ((INIT_OK, TOOL), "Unexpected output"),
])
def test_parse_stream_rejects_guesswork(events, match):
    with pytest.raises(report.ReportError, match=match):
        report.parse_stream(_stream(*events))


def test_mcp_server_starts_with_this_python():
    cfg = json.loads(report.mcp_config())["mcpServers"]["yahoo-fantasy"]
    assert cfg["command"] == sys.executable and cfg["args"] == ["-m", "booth.mcp_server"]
