"""Report generation: run Claude Code headless with Booth's MCP server, then diff and save.

    booth report tue|thu|sat|sun [--week N]

Claude returns structured output (the message plus a snapshot, per SNAPSHOT_SCHEMA).
Booth adds the change header itself from a structured diff, so reworded prose never
reads as a change.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import date, datetime
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

MCP_SERVER = "yahoo-fantasy"
ALLOWED_TOOLS = [f"mcp__{MCP_SERVER}", "WebSearch", "WebFetch"]


class ReportError(RuntimeError):
    pass


def build_prompt(run: str, week: int, now: datetime, early_games: str = "Thursday 8:15 PM ET",
                 locked_games: str = "none", early_games_left: str = "none") -> str:
    fields = {
        "week": week,
        "today": now.strftime("%Y-%m-%d"),
        "weekday": now.strftime("%A"),
        "time": now.astimezone(EASTERN).strftime("%-I:%M %p"),
        "run_label": RUN_LABELS[run],
        "length_hint": LENGTH_HINTS[run],
        "early_games": early_games,
        "locked_games": locked_games,
        "early_games_left": early_games_left,
    }
    text = (PROMPTS / "common.md").read_text() + "\n" + (PROMPTS / f"{run}.md").read_text()
    for k, v in fields.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def claude_bin() -> str:
    """BOOTH_CLAUDE_BIN, else `claude` on PATH, else the native installer's ~/.local/bin/claude."""
    if os.getenv("BOOTH_CLAUDE_BIN"):
        return os.environ["BOOTH_CLAUDE_BIN"]
    import shutil

    found = shutil.which("claude")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "claude"
    return str(local) if local.exists() else "claude"


def mcp_config() -> str:
    """Booth's MCP server, started with this same Python so it doesn't depend on PATH
    (launchd gives jobs a minimal one)."""
    return json.dumps({"mcpServers": {MCP_SERVER: {"command": sys.executable, "args": ["-m", "booth.mcp_server"]}}})


def parse_stream(stdout: str, returncode: int = 0, stderr: str = "") -> dict:
    """Pull the result out of `--output-format stream-json`, and check the report was
    built from Booth's data: the MCP server connected and at least one of its tools ran.
    Otherwise Claude may still return a well-formed report that is guesswork."""
    init, result, tools = None, None, []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            init = event
        elif kind == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tools.append(block.get("name", ""))
        elif kind == "result":
            result = event
    if result is None:
        raise ReportError(f"Unexpected output from claude (exit {returncode}): {stderr[-500:] or stdout[-500:]}")
    if result.get("is_error") or not result.get("structured_output"):
        raise ReportError(f"Claude run failed: {result.get('subtype')} {str(result.get('result'))[:300]}")
    servers = {s.get("name"): s.get("status") for s in (init or {}).get("mcp_servers") or []}
    if servers.get(MCP_SERVER) != "connected":
        raise ReportError(f"Booth's Yahoo data server didn't start (status: {servers.get(MCP_SERVER, 'missing')}).")
    if not any(name.startswith(f"mcp__{MCP_SERVER}__") for name in tools):
        raise ReportError("Claude wrote the report without using any of Booth's data tools.")
    return result


def run_claude(prompt: str, timeout: int = 900, schema: dict | None = None) -> dict:
    """Run `claude -p` with Booth's MCP server and return its result (with structured_output)."""
    cmd = [
        claude_bin(),
        "-p",
        prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--json-schema", json.dumps(schema or SNAPSHOT_SCHEMA),
        "--mcp-config", mcp_config(),
        "--strict-mcp-config",
        "--permission-mode", "dontAsk",
        "--allowedTools", *ALLOWED_TOOLS,
    ]
    if os.getenv("BOOTH_MODEL"):
        cmd += ["--model", os.environ["BOOTH_MODEL"]]
    try:
        proc = subprocess.run(cmd, cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise ReportError("Claude Code isn't installed or not on PATH (set BOOTH_CLAUDE_BIN).") from exc
    except subprocess.TimeoutExpired as exc:
        raise ReportError(f"Claude didn't finish within {timeout // 60} minutes.") from exc
    return parse_stream(proc.stdout, proc.returncode, proc.stderr)


def _kickoff(g: dict) -> datetime:
    d = date.fromisoformat(g["date"])
    hh, mm = (int(x) for x in g["kickoff_et"].split(":"))
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=EASTERN)


def _game_label(g: dict) -> str:
    return f"{g['day']} {_kickoff(g).strftime('%-I:%M %p')} ET, {g['away']}@{g['home']}"


def early_games_for(week: int, games: list[dict], after: datetime | None = None) -> str:
    """Games before the main Sunday slate (Thursday night, plus Wednesday, Thanksgiving,
    Friday or Saturday games): each one locks its players at kickoff. With `after`, only
    those that haven't kicked off yet."""
    sched = nflverse.schedule(week, games)["games"]
    sundays = sorted(g["date"] for g in sched if g["day"] == "Sunday")
    early = [g for g in sched if (not sundays or g["date"] < sundays[0]) and (after is None or _kickoff(g) > after)]
    return "; ".join(_game_label(g) for g in early) or "none"


def locked_games_at(week: int, games: list[dict], now: datetime) -> str:
    """This week's games that have already kicked off at `now`."""
    sched = nflverse.schedule(week, games)["games"]
    started = [g for g in sched if _kickoff(g) <= now]
    return "; ".join(f"{g['away']}@{g['home']}" for g in started) or "none yet"


_NUMBERED = re.compile(r"^\d{1,2}[.)]\s")
_BULLET = re.compile(r"^\s*[-•*]\s")
_SLOT = re.compile(r"^(QB|RB|WR|TE|FLEX|W/R/T|SUPERFLEX|SFLEX|K|DEF|DST|D/ST|BN|IR)\d?\b", re.IGNORECASE)
# Plain lines that still belong to the numbered claim above them ("Drop: X", "IR: move Y").
_CLAIM_CONTINUES = re.compile(r"^(drop|ir|move|free|bid|then)\b", re.IGNORECASE)


def _is_heading(line: str) -> bool:
    return (line.endswith(":") and not line[:1].isspace()
            and not _BULLET.match(line) and not _NUMBERED.match(line) and not _SLOT.match(line))


def space_sections(text: str) -> str:
    """Put a blank line between a report's sections so it isn't a wall of text on a phone.

    A blank line goes before each top-level numbered item and each heading line ending
    in ":" (unless a heading sits right above it), and after a "-" list that hangs off a
    heading or numbered item when plain text follows. Lineup lines, notes under them,
    indented lines, and a claim's drop/IR line stay together. Runs of blank lines
    collapse to one. The model is asked for this spacing too; this makes it certain.
    """
    out: list[str] = []
    anchor = ""  # the line the current "-" list hangs off, within this block
    tail = False  # the previous line was a "-" item or a claim's drop/IR line
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = line.rstrip()
        if not line:
            if out and out[-1]:
                out.append("")
            anchor, tail = "", False
            continue
        prev = out[-1] if out else ""
        top = not line[:1].isspace()
        bullet = bool(_BULLET.match(line))
        continues_claim = False
        if prev and top and not bullet:
            if _NUMBERED.match(line) or _is_heading(line):
                gap = not _is_heading(prev)
            elif tail and _is_heading(anchor):
                gap = True
            elif tail and _NUMBERED.match(anchor):
                continues_claim = bool(_CLAIM_CONTINUES.match(line))
                gap = not continues_claim
            else:
                gap = False
            if gap:
                out.append("")
        if top and not bullet and not continues_claim:
            anchor = line
        tail = bullet or continues_claim or (tail and not top)
        out.append(line)
    return "\n".join(out).strip("\n")


def generate(run: str, week: int | None = None, now: datetime | None = None, claude=run_claude) -> dict:
    """Generate one report. Returns {"message", "path", "snapshot", "changes"}."""
    if run not in RUN_LABELS:
        raise ReportError(f"Unknown report '{run}'. Use one of: {', '.join(RUN_LABELS)}.")
    now = now or datetime.now(EASTERN)
    games = nflverse.games()
    week = week or nflverse.current_week(games, now.date())
    prompt = build_prompt(run, week, now, early_games_for(week, games), locked_games_at(week, games, now),
                          early_games_for(week, games, after=now))

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
    message = space_sections(out["message"])
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
