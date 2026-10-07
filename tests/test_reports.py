import json
import subprocess
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


@pytest.mark.parametrize("run", ["tue", "thu", "sat", "sun"])
def test_every_prompt_is_fully_filled(run):
    now = datetime(2026, 10, 17, 8, 0, tzinfo=ZoneInfo("America/New_York"))
    p = report.build_prompt(run, 6, now, "Thursday 8:15 PM ET, SEA@DEN", "SEA@DEN", "none")
    assert "{" not in p.replace("{}", ""), p


def test_prior_snapshot_counts_only_delivered_reports_once_scheduled(tmp_path):
    state.save_snapshot(snap("thu"), base=tmp_path)
    data = state.load_week(2026, 5, base=tmp_path)
    data["jobs"] = {"thu": {"generated": True}}  # built but never reached Dan
    state.write_week(data, base=tmp_path)
    assert state.prior_snapshot(2026, 5, "sat", base=tmp_path) is None
    data["jobs"]["thu"]["delivered_at"] = "2026-10-08T12:05:00-04:00"
    state.write_week(data, base=tmp_path)
    assert state.prior_snapshot(2026, 5, "sat", base=tmp_path)["run"] == "thu"


def test_schema_matches_prd_fields():
    req = set(report.SNAPSHOT_SCHEMA["required"])
    assert {"lineup", "bench_flags", "waiver_recs", "weather_flags", "sources", "message_blocks"} <= req


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
    thu_night = datetime(2026, 10, 8, 21, 0, tzinfo=ZoneInfo("America/New_York"))
    assert report.early_games_for(5, games, after=thu_night) == "none"


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


def _blank_before(text):
    lines = text.split("\n")
    return [lines[i] for i in range(1, len(lines)) if lines[i] and not lines[i - 1]]


def test_space_sections_separates_claims_and_sections():
    raw = (FIX / "tue_wk5_dry_run.txt").read_text()
    out = report.space_sections(raw)
    assert [ln.split(" (")[0][:20] for ln in _blank_before(out)] == [
        "2) Add Roman Wilson", "3) Add Dohnte Meyers", "Total $15, which lea", "Week 5 lineup",
    ]
    assert out.startswith("Submit before claims process Wed:\n1) Add Aaron Rodgers")  # heading stays on its list
    assert "\n\n\n" not in out and out == out.strip()
    assert report.space_sections(out) == out
    # nothing but blank lines was added
    assert [ln for ln in out.split("\n") if ln] == [ln.rstrip() for ln in raw.strip().split("\n") if ln.strip()]


def test_space_sections_edge_cases():
    sp = report.space_sections
    assert sp("A\n\n\n\nB\r\nC   \n") == "A\n\nB\nC"
    assert sp("Watch:\n- Spears Q\n- Dobbins weak\nWeather: wind at CHI@GB") == (
        "Watch:\n- Spears Q\n- Dobbins weak\n\nWeather: wind at CHI@GB")
    assert sp("Lineup\nQB Burrow 20\n12.7 pts/gm for Higbee\n2 TDs last week") == (
        "Lineup\nQB Burrow 20\n12.7 pts/gm for Higbee\n2 TDs last week")
    assert sp("1. Start Taylor\n- reason\n  more on that reason\n2. Sit Spears") == (
        "1. Start Taylor\n- reason\n  more on that reason\n\n2. Sit Spears")
    assert sp("Bench notes:\nWeather (6 days out):") == "Bench notes:\nWeather (6 days out):"
    assert sp("- note ends with colon:\n- next") == "- note ends with colon:\n- next"
    assert sp("") == ""


def test_generate_spaces_the_model_message(isolated):
    thu = datetime(2026, 10, 8, 12, 0, tzinfo=ZoneInfo("America/New_York"))
    r = report.generate("thu", now=thu, claude=_fake_claude(message="1) Start Taylor\n- reason\n2) Sit Spears\n- reason"))
    assert r["message"].endswith("1) Start Taylor\n- reason\n\n2) Sit Spears\n- reason")
    assert r["path"].read_text() == r["message"] + "\n"



UNCHANGED = [
    # "-" notes under lineup lines, and an undecided slot ending in ":", stay inside the lineup
    "Week 5 lineup (est):\nQB Burrow @MIA 20\nRB Dobbins @LAC 6\n- weak spot\nWR Collins @TEN 15\n- Q (hamstring)\nTE LaPorta @ARI 13",
    "Lineup:\nQB Burrow @MIA 20\nFLEX1 (decide after 11:30 AM ET inactives):\n- Kraft if Watson is out\n- Concepcion if Watson plays\nFLEX2 Wilson @PIT 7\nK Aubrey vs TB 9",
    "Final lineup:\nRB Taylor @PIT 18 (Q)\n- If Taylor is out, start Spears\nRB Dobbins @LAC 6",
    # indented lines are continuations: never headings or numbered items
    "Injuries to watch:\n- Taylor (Q): limited\n  If he's out Sunday, pivot to:\n  - Dobbins\n  - Spears\n- Collins (full)",
    "Watch:\n- Taylor (Q). If he sits, in order:\n  1. Dobbins\n  2. Spears\n- Collins healthy",
]


@pytest.mark.parametrize("text", UNCHANGED)
def test_space_sections_leaves_blocks_together(text):
    assert report.space_sections(text) == text


def test_space_sections_keeps_drop_lines_with_their_claim():
    raw = ("1) Add Rodgers (QB PIT), bid $12\n- No QB in wk 6.\nIR: move Mason Taylor (O) to IR.\n"
           "2) Add Wilson (WR PIT), bid $3\n- 6 targets a week.\nDrop: Darnell Mooney\n- 3.2 avg.\n"
           "3) Add Meyers (WR CIN), bid $2\n- 89% snaps.\nDrop: Jalen Nailor\nTotal $17, $83 left.")
    assert report.space_sections(raw) == (
        "1) Add Rodgers (QB PIT), bid $12\n- No QB in wk 6.\nIR: move Mason Taylor (O) to IR.\n\n"
        "2) Add Wilson (WR PIT), bid $3\n- 6 targets a week.\nDrop: Darnell Mooney\n- 3.2 avg.\n\n"
        "3) Add Meyers (WR CIN), bid $2\n- 89% snaps.\nDrop: Jalen Nailor\n\nTotal $17, $83 left.")


def test_space_sections_sub_headings():
    sp = report.space_sections
    assert sp("Check inactives:\n1:00 PM ET:\n- Taylor (Q)\n4:25 PM ET:\n- Collins (Q)") == (
        "Check inactives:\n1:00 PM ET:\n- Taylor (Q)\n\n4:25 PM ET:\n- Collins (Q)")
    assert sp("Final lineup:\n1 PM ET:\nQB Burrow 20\n4:25 PM ET:\nRB Dobbins 6") == (
        "Final lineup:\n1 PM ET:\nQB Burrow 20\n\n4:25 PM ET:\nRB Dobbins 6")


ROUND2 = [
    # headings that start with a slot word are still headings
    ("Total $15, $85 left.\nQB streamers for week 6, in order:\n1) Aaron Rodgers (PIT) $10, IR Mason Taylor\n"
     "2) Tyson Bagent (CHI) $1\nIR moves first:\n1) Move Mason Taylor (O) to IR.",
     "Total $15, $85 left.\n\nQB streamers for week 6, in order:\n1) Aaron Rodgers (PIT) $10, IR Mason Taylor\n\n"
     "2) Tyson Bagent (CHI) $1\n\nIR moves first:\n1) Move Mason Taylor (O) to IR."),
    ("Waivers done.\nK/DEF streamers for week 6:\n1) Aubrey\n2) Texans",
     "Waivers done.\n\nK/DEF streamers for week 6:\n1) Aubrey\n\n2) Texans"),
    # a claim's own "...:" lines stay with the claim
    ("1) Add Aaron Rodgers (QB PIT) $12, move Mason Taylor (O) to IR.\n- Burrow and Murray on bye wk 6.\n"
     "If you lose him, backups in order:\n- Bagent (CHI) $1\n2) Add Roman Wilson (WR PIT) $3\n"
     "Drop one (both are dead weight):\n- Darnell Mooney (3.2 avg)\n- Jalen Nailor (1.8 avg)\nTotal $15, $85 left.",
     "1) Add Aaron Rodgers (QB PIT) $12, move Mason Taylor (O) to IR.\n- Burrow and Murray on bye wk 6.\n"
     "If you lose him, backups in order:\n- Bagent (CHI) $1\n\n2) Add Roman Wilson (WR PIT) $3\n"
     "Drop one (both are dead weight):\n- Darnell Mooney (3.2 avg)\n- Jalen Nailor (1.8 avg)\n\nTotal $15, $85 left."),
    ("2) Add Wilson (WR PIT) $3\n- 6 targets a week.\nDrop:\n- Darnell Mooney (3.2 avg)\n3) Add Meyers (WR CIN) $2, drop Nailor",
     "2) Add Wilson (WR PIT) $3\n- 6 targets a week.\nDrop:\n- Darnell Mooney (3.2 avg)\n\n3) Add Meyers (WR CIN) $2, drop Nailor"),
    ("Final calls:\n1) Start Taylor (Q, ankle).\n- ESPN Sat 9 PM: expected to play.\nIf he's ruled out at 11:30 AM ET:\n"
     "- Start Spears in his RB slot.\n2) Sit Collins (Q, hamstring).\n- Didn't practice Fri.",
     "Final calls:\n1) Start Taylor (Q, ankle).\n- ESPN Sat 9 PM: expected to play.\nIf he's ruled out at 11:30 AM ET:\n"
     "- Start Spears in his RB slot.\n\n2) Sit Collins (Q, hamstring).\n- Didn't practice Fri."),
    # a numbered item ending in ":" keeps its sub-heading
    ("1) Thursday night, TB@DAL 8:15 PM ET:\nStart now:\n- Aubrey (K DAL)\n2) Sunday:\nCheck 11:30 AM ET inactives:\n- Taylor (Q)",
     "1) Thursday night, TB@DAL 8:15 PM ET:\nStart now:\n- Aubrey (K DAL)\n\n2) Sunday:\nCheck 11:30 AM ET inactives:\n- Taylor (Q)"),
    # a lineup line after a heading's note isn't split off
    ("Week 5 lineup after claim 1:\n- all projections est\nQB Burrow @MIA 20\nRB Taylor @PIT 18",
     "Week 5 lineup after claim 1:\n- all projections est\nQB Burrow @MIA 20\nRB Taylor @PIT 18"),
    # the lineup's last line still gets a gap before the next section
    ("Lineup:\nK Aubrey vs TB 9\nDEF Texans @TEN 8\nWeather:\n- wind at CHI@GB",
     "Lineup:\nK Aubrey vs TB 9\nDEF Texans @TEN 8\n\nWeather:\n- wind at CHI@GB"),
]

UNCHANGED_ROUND2 = [
    # notes ending in ":" between lineup lines stay inside the lineup
    "Week 5 lineup (est):\nQB Burrow @MIA 20\nRB Dobbins @LAC 6\nWeak spot. Swap in if Spears clears:\n"
    "- Tyjae Spears (TEN)\nWR Collins @TEN 15\nDEF Texans @TEN 8",
    "Final lineup:\nQB Burrow @MIA 20\nRB Taylor @PIT 18 (Q)\nIf he's ruled out at 11:30 AM ET:\n- Spears\n"
    "RB Dobbins @LAC 6\nWR Collins @TEN 15",
    "Final lineup:\nQB Burrow @MIA 20\nRB Taylor @PIT 18\nStart one at FLEX after 11:30 inactives:\n"
    "- Kraft if Watson is out\n- Concepcion if Watson plays\nTE LaPorta @ARI 13\nK Aubrey vs TB 9",
    "Lineup:\nRB2: Dobbins @LAC 6\nWR1: Collins @TEN 15",
]


@pytest.mark.parametrize("raw,expected", ROUND2)
def test_space_sections_round2(raw, expected):
    assert report.space_sections(raw) == expected
    assert report.space_sections(expected) == expected


@pytest.mark.parametrize("text", UNCHANGED_ROUND2)
def test_space_sections_keeps_lineup_notes_inside(text):
    assert report.space_sections(text) == text


def test_message_blocks_are_joined_with_blank_lines():
    out = {"message_blocks": [
        "Submit before claims process Wed:\n1) Add Rodgers (QB PIT) $10, IR Mason Taylor.\n- No QB in wk 6.",
        "2) Add Wilson (WR PIT) $3, drop Mooney.\n- 6 targets a week.\r\n",
        "  ",
        "Week 5 lineup (est):\nQB Burrow @MIA 20\nRB Dobbins @LAC 6\nWeak spot. Swap in if Spears clears:\n- Spears\n"
        "WR Collins @TEN 15",
        "Top waiver names next week:\n1. Bagent\n2. Daniels\n\n\nWatch:\n- Taylor (Q)",
    ]}
    assert report.message_text(out) == (
        "Submit before claims process Wed:\n1) Add Rodgers (QB PIT) $10, IR Mason Taylor.\n- No QB in wk 6.\n\n"
        "2) Add Wilson (WR PIT) $3, drop Mooney.\n- 6 targets a week.\n\n"
        "Week 5 lineup (est):\nQB Burrow @MIA 20\nRB Dobbins @LAC 6\nWeak spot. Swap in if Spears clears:\n- Spears\n"
        "WR Collins @TEN 15\n\n"
        "Top waiver names next week:\n1. Bagent\n\n2. Daniels\n\nWatch:\n- Taylor (Q)")


def test_message_blocks_space_claims_written_together():
    out = {"message_blocks": ["Claims:\n1) Add X $3\n- reason\n2) Add Y $2\n- reason", "Lineup:\nQB Burrow 20"]}
    assert report.message_text(out) == "Claims:\n1) Add X $3\n- reason\n\n2) Add Y $2\n- reason\n\nLineup:\nQB Burrow 20"


def test_crammed_blocks_are_spaced_too():
    out = {"message_blocks": [
        "Submit before claims process Wed:\n1) Add Rodgers $10, IR Mason Taylor.\n- reason a\n2) Add Wilson $3, drop Mooney.\n"
        "- reason\n3) Add Meyers $2, drop Nailor.\n- reason\nTotal $15, $85 left.",
        "Roster from manual list (Yahoo access pending)."]}
    assert report.message_text(out) == (
        "Submit before claims process Wed:\n1) Add Rodgers $10, IR Mason Taylor.\n- reason a\n\n"
        "2) Add Wilson $3, drop Mooney.\n- reason\n\n3) Add Meyers $2, drop Nailor.\n- reason\n\nTotal $15, $85 left.\n\n"
        "Roster from manual list (Yahoo access pending).")


def test_if_he_sits_lines_stay_with_their_call():
    raw = ("1) Start Taylor (Q).\n- expected to play.\nIf he sits: start Spears.\n2) Sit Collins (Q).\n- DNP Fri.\n"
           "Otherwise start Wilson.\nWeather:\n- wind at CHI@GB\nIf you only make one move, start Spears.")
    assert report.space_sections(raw) == (
        "1) Start Taylor (Q).\n- expected to play.\nIf he sits: start Spears.\n\n2) Sit Collins (Q).\n- DNP Fri.\n"
        "Otherwise start Wilson.\n\nWeather:\n- wind at CHI@GB\n\nIf you only make one move, start Spears.")


def test_generate_joins_message_blocks(isolated):
    thu = datetime(2026, 10, 8, 12, 0, tzinfo=ZoneInfo("America/New_York"))

    def claude(prompt):
        out = _fake_claude()(prompt)
        out["structured_output"].pop("message")
        out["structured_output"]["message_blocks"] = ["Start everyone.", "Lineup:\nQB Burrow @MIA 20"]
        return out
    r = report.generate("thu", now=thu, claude=claude)
    assert r["message"].endswith("Start everyone.\n\nLineup:\nQB Burrow @MIA 20")
    assert r["snapshot"]["message"] == "Start everyone.\n\nLineup:\nQB Burrow @MIA 20"


def test_one_block_or_old_message_falls_back_to_space_sections():
    raw = (FIX / "tue_wk5_dry_run.txt").read_text()
    assert report.message_text({"message_blocks": [raw]}) == report.space_sections(raw)
    assert report.message_text({"message": raw}) == report.space_sections(raw)
    assert report.message_text({"message_blocks": []}) == ""


@pytest.mark.parametrize("env,expected", [(None, ["--effort", "high"]), ("max", ["--effort", "max"]), ("default", [])])
def test_run_claude_sets_effort(monkeypatch, env, expected):
    from booth import report
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(report.subprocess, "run", fake_run)
    if env is None:
        monkeypatch.delenv("BOOTH_EFFORT", raising=False)
    else:
        monkeypatch.setenv("BOOTH_EFFORT", env)
    with pytest.raises(report.ReportError):
        report.run_claude("hi")
    cmd = seen["cmd"]
    got = cmd[cmd.index("--effort"):cmd.index("--effort") + 2] if "--effort" in cmd else []
    assert got == expected
