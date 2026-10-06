"""Manual fallbacks for when the Yahoo API isn't reachable or provisioned.

While Yahoo access is pending, Booth works from copies of Dan's Yahoo pages (his
screenshots, turned into JSON on the Mac). They hold league data, so the live copies
are gitignored; config/*.example.json show their shape.

- config/manual_roster.local.json: his current roster (falls back to the seeded
  config/manual_roster.json). Used until Yahoo works; flagged as stale after 7 days.
- config/manual_free_agents.json: Yahoo's available-players list. Ignored after 7 days.
- config/manual_matchup.json: this week's matchup page. Used only for its own week.

`booth manual-status` checks all three.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from booth.config import ROOT

CONFIG = ROOT / "config"
MANUAL_ROSTER = CONFIG / "manual_roster.json"
MANUAL_ROSTER_LOCAL = CONFIG / "manual_roster.local.json"
MANUAL_FREE_AGENTS = CONFIG / "manual_free_agents.json"
MANUAL_MATCHUP = CONFIG / "manual_matchup.json"
# Waivers turn over weekly, so an older saved list would mostly recommend players who are gone.
FREE_AGENTS_MAX_AGE = timedelta(days=7)
ROSTER_STALE_AFTER = timedelta(days=7)
MATCHUP_MAX_AGE = timedelta(days=8)
FLEX = {"RB", "WR", "TE"}
POSITIONS = {"QB", "RB", "WR", "TE", "K", "DEF"}


def _as_of(value: str) -> datetime:
    """as_of is an ISO date or datetime; a bare date or naive time counts as US Eastern."""
    from zoneinfo import ZoneInfo

    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))
    return dt


def _public(data: dict) -> dict:
    return {"source": "manual", **{k: v for k, v in data.items() if not k.startswith("_")}}


def roster_path() -> Path:
    return MANUAL_ROSTER_LOCAL if MANUAL_ROSTER_LOCAL.exists() else MANUAL_ROSTER


def manual_roster(now: datetime | None = None) -> dict:
    data = json.loads(roster_path().read_text())
    out = _public(data)
    if now is None:
        now = datetime.now(timezone.utc)
    if now - _as_of(str(data["as_of"])) > ROSTER_STALE_AFTER:
        out["warning"] = f"This roster copy is from {data['as_of']}; Dan may have made moves since."
    return out


def manual_free_agents(position: str | None = None, limit: int = 50, now: datetime | None = None) -> dict | None:
    """Dan's saved copy of Yahoo's available-players list, or None if there isn't a usable one."""
    if not MANUAL_FREE_AGENTS.exists():
        return None
    data = json.loads(MANUAL_FREE_AGENTS.read_text())
    if (now or datetime.now(timezone.utc)) - _as_of(data["as_of"]) > FREE_AGENTS_MAX_AGE:
        return None
    wanted = {p.strip().upper() for p in position.split(",")} if position else None
    if wanted and "W/R/T" in wanted:
        wanted = (wanted - {"W/R/T"}) | FLEX
    players = [p for p in data["players"] if not wanted or p["position"] in wanted]
    return {
        "source": "manual",
        "as_of": data["as_of"],
        "note": data["note"],
        "players": players[:limit],
    }


def manual_matchup(week: int | None, now: datetime | None = None) -> dict | None:
    """Dan's copy of his Yahoo matchup page, if it's for the requested week (or, when the
    week is unknown, if it's recent). None otherwise."""
    if not MANUAL_MATCHUP.exists():
        return None
    data = json.loads(MANUAL_MATCHUP.read_text())
    if week is not None and int(data["week"]) != int(week):
        return None
    if (now or datetime.now(timezone.utc)) - _as_of(data["as_of"]) > MATCHUP_MAX_AGE:
        return None
    return _public(data)


# --- Checks, for whoever writes these files ---------------------------------------------

def _check_as_of(data: dict, problems: list[str]) -> datetime | None:
    try:
        return _as_of(str(data["as_of"]))
    except (KeyError, ValueError):
        problems.append('"as_of" is missing or not an ISO date/time')
        return None


def _check_player(p: dict, where: str, problems: list[str], need: tuple[str, ...]) -> None:
    for key in need:
        if not p.get(key):
            problems.append(f'{where}: missing "{key}"')
    if p.get("position") and p["position"] not in POSITIONS:
        problems.append(f'{where}: position "{p["position"]}" is not one of {sorted(POSITIONS)}')


def check_roster(data: dict) -> list[str]:
    problems: list[str] = []
    _check_as_of(data, problems)
    players = data.get("players")
    if not isinstance(players, list) or not players:
        return problems + ['"players" must be a non-empty list']
    for i, p in enumerate(players):
        _check_player(p, f"player {i + 1} ({p.get('name', '?')})", problems, ("name", "position", "nfl_team"))
    names = [p.get("name") for p in players]
    if len(set(names)) != len(names):
        problems.append("the same player is listed twice")
    if len(players) < 20:
        problems.append(f"only {len(players)} players: looks like part of the roster (a full one is about 25 plus IR)")
    return problems


def check_free_agents(data: dict) -> list[str]:
    problems: list[str] = []
    _check_as_of(data, problems)
    if not data.get("note"):
        problems.append('"note" (what the list covers) is missing')
    players = data.get("players")
    if not isinstance(players, list) or not players:
        return problems + ['"players" must be a non-empty list']
    for i, p in enumerate(players):
        _check_player(p, f"player {i + 1} ({p.get('name', '?')})", problems, ("name", "position", "nfl_team"))
    return problems


def check_matchup(data: dict) -> list[str]:
    problems: list[str] = []
    _check_as_of(data, problems)
    if not isinstance(data.get("week"), int) or not 1 <= data["week"] <= 18:
        problems.append('"week" must be a whole number from 1 to 18')
    opp = data.get("opponent")
    if not isinstance(opp, dict) or not opp.get("team_name"):
        return problems + ['"opponent.team_name" is missing']
    for side, team in (("me", data.get("me") or {}), ("opponent", opp)):
        proj = team.get("projected_points")
        if proj is not None and not isinstance(proj, (int, float)):
            problems.append(f'"{side}.projected_points" must be a number')
    for i, p in enumerate(opp.get("starters") or []):
        _check_player(p, f"opponent starter {i + 1} ({p.get('name', '?')})", problems, ("name", "slot"))
    return problems


def status(now: datetime | None = None, current_week: int | None = None) -> str:
    """One line per manual file: what it holds, how old it is, and whether Booth uses it now."""
    now = now or datetime.now(timezone.utc)
    lines = []

    def load(path: Path):
        try:
            return json.loads(path.read_text()), None
        except FileNotFoundError:
            return None, "not there"
        except ValueError as exc:
            return None, f"not valid JSON ({exc})"

    def age(as_of: datetime) -> str:
        hours = (now - as_of).total_seconds() / 3600
        return f"{hours:.0f} hours old" if hours < 48 else f"{hours / 24:.0f} days old"

    path = roster_path()
    data, err = load(path)
    if err:
        lines.append(f"Roster ({path.name}): {err}")
    else:
        problems = check_roster(data)
        as_of = _check_as_of(data, [])
        when = f", as of {data.get('as_of')} ({age(as_of)})" if as_of else ""
        stale = " STALE: send a fresh team screenshot." if as_of and now - as_of > ROSTER_STALE_AFTER else ""
        lines.append(f"Roster ({path.name}): {len(data.get('players') or [])} players{when}.{stale}")
        lines += [f"  problem: {p}" for p in problems]

    data, err = load(MANUAL_FREE_AGENTS)
    if err:
        lines.append(f"Available players: {err}. Waiver picks will be marked \"check he's available\".")
    else:
        problems = check_free_agents(data)
        as_of = _check_as_of(data, [])
        used = as_of and now - as_of <= FREE_AGENTS_MAX_AGE
        expires = f"; ignored after {(as_of + FREE_AGENTS_MAX_AGE):%a %b %-d %-I:%M %p}" if as_of else ""
        lines.append(f"Available players: {len(data.get('players') or [])} players, as of {data.get('as_of')}"
                     f" ({age(as_of) if as_of else '?'}){expires}. {'In use.' if used else 'EXPIRED: not used.'}")
        lines += [f"  problem: {p}" for p in problems]

    data, err = load(MANUAL_MATCHUP)
    if err:
        lines.append(f"Matchup: {err}. Reports will say the opponent is unknown.")
    else:
        problems = check_matchup(data)
        opp = (data.get("opponent") or {}).get("team_name", "?")
        wk = data.get("week")
        note = ""
        if current_week is not None and wk != current_week:
            note = f" Not used: this is week {current_week}."
        lines.append(f"Matchup: week {wk} vs {opp}, as of {data.get('as_of')}.{note}")
        lines += [f"  problem: {p}" for p in problems]
    return "\n".join(lines)
