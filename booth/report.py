"""Report generation: run Claude Code headless with Booth's MCP server, then diff and save.

    booth report tue|thu|sat|sun [--week N]

Claude returns structured output (the message plus a snapshot, per SNAPSHOT_SCHEMA).
Booth adds the change header itself from a structured diff, so reworded prose never
reads as a change.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from booth.config import ROOT
from booth.data import nflverse
from booth.diff import change_header, diff_snapshots
from booth.state import DIFF_AGAINST, prior_snapshot, save_snapshot

EASTERN = ZoneInfo("America/New_York")
PROMPTS = ROOT / "prompts"
REPORTS = ROOT / "reports"

RUN_LABELS = {
    "tue": "Tuesday waiver report",
    "thu": "Thursday preliminary lineup check",
    "sat": "Saturday preliminary lineup check",
    "sun": "Sunday final lineup pass",
}
LENGTH_HINTS = {
    "tue": "under about 1,800 characters",
    "thu": "under about 1,200 characters",
    "sat": "under about 1,200 characters",
    "sun": "under about 900 characters; a few lines if nothing changed",
}

_player = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "nfl_team": {"type": "string"},
        "status": {"type": "string"},
        "projection": {"type": ["number", "null"]},
    },
    "required": ["name", "status", "projection"],
}
SLOTS = ["QB", "RB1", "RB2", "WR1", "WR2", "TE", "FLEX1", "FLEX2", "K", "DEF"]
SNAPSHOT_SCHEMA = {
    "type": "object",
    "properties": {
        "message": {"type": "string"},
        "lineup": {"type": "object", "properties": {s: _player for s in SLOTS}, "required": SLOTS},
        "bench_flags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "status": {"type": "string"}, "note": {"type": "string"}},
                "required": ["name", "note"],
            },
        },
        "waiver_recs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "add": {"type": "string"},
                    "drop": {"type": ["string", "null"]},
                    "faab_bid": {"type": "number"},
                    "reasoning": {"type": "string"},
                },
                "required": ["add", "faab_bid", "reasoning"],
            },
        },
        "weather_flags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"game": {"type": "string"}, "note": {"type": "string"}},
                "required": ["game", "note"],
            },
        },
        "sources": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["message", "lineup", "bench_flags", "waiver_recs", "weather_flags", "sources"],
}

ALLOWED_TOOLS = ["mcp__yahoo-fantasy", "WebSearch", "WebFetch"]


class ReportError(RuntimeError):
    pass


def build_prompt(run: str, week: int, now: datetime, tnf_kickoff: str = "Thursday 8:15 PM ET") -> str:
    fields = {
        "week": week,
        "today": now.strftime("%Y-%m-%d"),
        "weekday": now.strftime("%A"),
        "run_label": RUN_LABELS[run],
        "length_hint": LENGTH_HINTS[run],
        "tnf_kickoff": tnf_kickoff,
    }
    text = (PROMPTS / "common.md").read_text() + "\n" + (PROMPTS / f"{run}.md").read_text()
    for k, v in fields.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def run_claude(prompt: str, timeout: int = 900) -> dict:
    """Run `claude -p` with Booth's MCP server and return its structured output."""
    cmd = [
        os.getenv("BOOTH_CLAUDE_BIN", "claude"),
        "-p",
        prompt,
        "--output-format", "json",
        "--json-schema", json.dumps(SNAPSHOT_SCHEMA),
        "--mcp-config", str(ROOT / ".mcp.json"),
        "--strict-mcp-config",
        "--permission-mode", "dontAsk",
        "--allowedTools", *ALLOWED_TOOLS,
    ]
    if os.getenv("BOOTH_MODEL"):
        cmd += ["--model", os.environ["BOOTH_MODEL"]]
    try:
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise ReportError("Claude Code isn't installed or not on PATH (set BOOTH_CLAUDE_BIN).") from exc
    except subprocess.TimeoutExpired as exc:
        raise ReportError(f"Claude didn't finish within {timeout // 60} minutes.") from exc
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ReportError(f"Unexpected output from claude (exit {proc.returncode}): {proc.stderr[-500:] or proc.stdout[-500:]}") from exc
    if out.get("is_error") or not out.get("structured_output"):
        raise ReportError(f"Claude run failed: {out.get('subtype')} {str(out.get('result'))[:300]}")
    return out


def tnf_kickoff_for(week: int, games: list[dict]) -> str:
    sched = nflverse.schedule(week, games)["games"]
    thursday = [g for g in sched if g["day"] == "Thursday"]
    if not thursday:
        return "no Thursday game this week"
    g = thursday[0]
    hh, mm = (int(x) for x in g["kickoff_et"].split(":"))
    t = datetime(2000, 1, 1, hh, mm).strftime("%-I:%M %p")
    return f"Thursday {t} ET, {g['away']}@{g['home']}"


def generate(run: str, week: int | None = None, now: datetime | None = None, claude=run_claude) -> dict:
    """Generate one report. Returns {"message", "path", "snapshot", "changes"}."""
    if run not in RUN_LABELS:
        raise ReportError(f"Unknown report '{run}'. Use one of: {', '.join(RUN_LABELS)}.")
    now = now or datetime.now(EASTERN)
    games = nflverse.games()
    week = week or nflverse.current_week(games, now.date())
    prompt = build_prompt(run, week, now, tnf_kickoff_for(week, games))

    result = claude(prompt)
    out = result["structured_output"]
    snapshot = {
        "season": nflverse.SEASON,
        "week": week,
        "run": run,
        "generated_at": now.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **{k: out.get(k) for k in ("lineup", "bench_flags", "waiver_recs", "weather_flags", "sources")},
        "message": out["message"],
        "cost_usd": result.get("total_cost_usd"),
    }

    changes: list[str] = []
    message = out["message"].strip()
    if run in DIFF_AGAINST:
        prev = prior_snapshot(nflverse.SEASON, week, run)
        changes = diff_snapshots(prev, snapshot)
        header = change_header(changes) if prev else "First lineup check this week (nothing to compare against)."
        message = header + "\n\n" + message
    title = f"Booth: {RUN_LABELS[run]}, week {week}"
    message = f"{title}\n\n{message}"
    snapshot["changes"] = changes

    save_snapshot(snapshot)
    REPORTS.mkdir(exist_ok=True)
    path = REPORTS / f"{nflverse.SEASON}-wk{week:02d}-{run}.txt"
    path.write_text(message + "\n")
    return {"message": message, "path": path, "snapshot": snapshot, "changes": changes}
