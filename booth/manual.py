"""Manual fallbacks for when the Yahoo API isn't reachable or provisioned.

While Yahoo access is pending, Booth works from copies of Dan's Yahoo pages (his
screenshots, turned into JSON on the Mac). They hold league data, so the live copies
are gitignored; config/*.example.json show their shape.

- config/manual_roster.local.json: his current roster (falls back to the seeded
  config/manual_roster.json). Used until Yahoo works; flagged as stale after 7 days.
- config/manual_free_agents.json: Yahoo's available-players list. Ignored after 7 days.
- config/manual_matchup.json: this week's matchup page. Used only for its own week.
- config/faab_market.json: what this league pays on waivers (winning bids, each team's
  budget left), from his league home page. Kept as a running log: old prices stay useful,
  and budgets are flagged as stale once a Wednesday claims run has passed since the copy. Unlike the others, it stays useful after
  Yahoo access works, since Yahoo's transaction list only covers recent moves.

`booth manual-status` checks all four.
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
FAAB_MARKET = CONFIG / "faab_market.json"
# Waivers turn over weekly, so an older saved list would mostly recommend players who are gone.
FREE_AGENTS_MAX_AGE = timedelta(days=7)
ROSTER_STALE_AFTER = timedelta(days=7)
MATCHUP_MAX_AGE = timedelta(days=8)
CLAIMS_RUN_ET = (2, 3, 40)  # Wednesday 3:40 AM ET: when claims process and budgets change (seen Oct 7 2026)
RECENT_CLAIM_WEEKS = 3  # weeks of winning bids listed one by one; older ones only count in the totals
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


def _load_roster(path: Path, now: datetime) -> dict:
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or not isinstance(data.get("players"), list) or not data["players"]:
        raise ValueError("no players list")
    out = _public(data)
    try:
        stale = now - _as_of(str(data["as_of"])) > ROSTER_STALE_AFTER
    except (KeyError, ValueError):  # the date only drives the warning; the roster is still good
        out["warning"] = "This roster copy has no readable as_of date, so it may be out of date."
        return out
    if stale:
        out["warning"] = f"This roster copy is from {data['as_of']}; Dan may have made moves since."
    return out


def manual_roster(now: datetime | None = None) -> dict:
    """Dan's roster copy, else the seeded one (with a warning saying so)."""
    now = now or datetime.now(timezone.utc)
    if MANUAL_ROSTER_LOCAL.exists():
        try:
            return _load_roster(MANUAL_ROSTER_LOCAL, now)
        except (OSError, ValueError, TypeError) as exc:
            out = _load_roster(MANUAL_ROSTER, now)
            out["warning"] = (f"Dan's roster copy ({MANUAL_ROSTER_LOCAL.name}) couldn't be read ({str(exc)[:80]}), "
                              f"so this is the older seeded list from {out.get('as_of')}; it may be out of date.")
            return out
    return _load_roster(MANUAL_ROSTER, now)


def manual_free_agents(position: str | None = None, limit: int = 50, now: datetime | None = None) -> dict | None:
    """Dan's saved copy of Yahoo's available-players list, or None if there isn't a usable one."""
    if not MANUAL_FREE_AGENTS.exists():
        return None
    try:
        data = json.loads(MANUAL_FREE_AGENTS.read_text())
        if (now or datetime.now(timezone.utc)) - _as_of(str(data["as_of"])) > FREE_AGENTS_MAX_AGE:
            return None
        wanted = {p.strip().upper() for p in position.split(",")} if position else None
        if wanted and "W/R/T" in wanted:
            wanted = (wanted - {"W/R/T"}) | FLEX
        players = [p for p in data["players"] if not wanted or p.get("position") in wanted]
        return {
            "source": "manual",
            "as_of": data["as_of"],
            "note": data.get("note", ""),
            "players": players[:limit],
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError):  # unreadable: same as no list
        return None


def manual_matchup(week: int | None, now: datetime | None = None) -> dict | None:
    """Dan's copy of his Yahoo matchup page, if it's for the requested week (or, when the
    week is unknown, if it's recent). None otherwise."""
    if not MANUAL_MATCHUP.exists():
        return None
    try:
        data = json.loads(MANUAL_MATCHUP.read_text())
        copy_week = int(data["week"])
        if week is not None and copy_week != int(week):
            return None
        if (now or datetime.now(timezone.utc)) - _as_of(str(data["as_of"])) > MATCHUP_MAX_AGE:
            return None
        return _public(data)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):  # unreadable: same as no copy
        return None


def _median(values: list[float]) -> float:
    v = sorted(values)
    mid = len(v) // 2
    return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2


def last_claims_run(now: datetime) -> datetime:
    """The most recent Wednesday claims run at or before `now`."""
    from zoneinfo import ZoneInfo

    weekday, hour, minute = CLAIMS_RUN_ET
    local = now.astimezone(ZoneInfo("America/New_York"))
    run = local.replace(hour=hour, minute=minute, second=0, microsecond=0) - timedelta(
        days=(local.weekday() - weekday) % 7)
    return run if run <= local else run - timedelta(days=7)


def _budgets_stale(as_of: datetime, now: datetime) -> bool:
    return as_of < last_claims_run(now)


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _week(value) -> int | None:
    try:
        week = int(value)
    except (TypeError, ValueError):
        return None
    return week if 1 <= week <= 18 else None


def faab_market(now: datetime | None = None) -> dict | None:
    """What this league pays on waivers, from Dan's copies of his league page: each team's
    budget left, recent winning bids, and price ranges by position. A bad entry is skipped
    (and counted in "warning"), not the whole file. None if there's no usable copy."""
    if not FAAB_MARKET.exists():
        return None
    try:
        data = json.loads(FAAB_MARKET.read_text())
        if not isinstance(data, dict):
            return None
        start = data.get("starting_budget") if _number(data.get("starting_budget")) else 100
        raw_teams = [t for t in data.get("teams") or [] if isinstance(t, dict)]
        teams = [{**t, "spent": start - t["remaining"]} for t in raw_teams if _number(t.get("remaining"))]
        raw_claims = [c for c in data.get("claims") or [] if isinstance(c, dict)]
        claims = [{**c, "week": _week(c.get("week"))} for c in raw_claims
                  if _number(c.get("bid")) and _week(c.get("week"))]
    except (OSError, ValueError, TypeError):  # unreadable: same as no copy
        return None
    if not teams and not claims:
        return None
    weeks = sorted({c["week"] for c in claims}, reverse=True)[:RECENT_CLAIM_WEEKS]
    by_position: dict[str, dict] = {}
    for pos in sorted({str(c["position"]) for c in claims if c.get("position")}):
        bids = [c["bid"] for c in claims if c.get("position") == pos]
        by_position[pos] = {"claims": len(bids), "top": max(bids), "median": _median(bids)}
    out = {
        "source": "manual",
        "as_of": data.get("as_of"),
        "note": data.get("note", ""),
        "starting_budget": start,
        "dan_remaining": next((t["remaining"] for t in teams if t.get("is_me")), None),
        "league_spent": sum(t["spent"] for t in teams),
        "teams_with_nothing_spent": sum(1 for t in teams if t["spent"] == 0),
        "teams": sorted(teams, key=lambda t: t["remaining"]),
        "recent_claims": sorted((c for c in claims if c["week"] in weeks), key=lambda c: (-c["week"], -c["bid"])),
        "by_position": by_position,
        "dan_outbid": [c for c in claims if c.get("outbid_dan")],
    }
    warnings = []
    skipped = len(raw_teams) - len(teams) + len(raw_claims) - len(claims)
    if skipped:
        warnings.append(f"{skipped} entries couldn't be read and were left out; run booth manual-status.")
    try:
        if _budgets_stale(_as_of(str(data["as_of"])), now or datetime.now(timezone.utc)):
            warnings.append(f"Budgets are from {data['as_of']}, before the latest Wednesday claims run, so "
                            "teams have bid since. Winning bids still show the going rate.")
    except (KeyError, ValueError):
        warnings.append("This copy has no readable as_of date, so the budgets may be out of date.")
    if warnings:
        out["warning"] = " ".join(warnings)
    return out


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


def check_faab_market(data: dict) -> list[str]:
    problems: list[str] = []
    _check_as_of(data, problems)
    start = data.get("starting_budget", 100)
    if not _number(start):
        problems.append('"starting_budget" must be a number')
        start = 100
    teams = data.get("teams")
    if not isinstance(teams, list) or not teams:
        problems.append('"teams" must be a non-empty list')
    else:
        for i, t in enumerate(teams):
            if not isinstance(t, dict):
                problems.append(f"team {i + 1}: not an object")
                continue
            where = f"team {i + 1} ({t.get('team', '?')})"
            if not t.get("team"):
                problems.append(f'{where}: missing "team"')
            if not _number(t.get("remaining")) or not 0 <= t["remaining"] <= start:
                problems.append(f'{where}: "remaining" must be a number from 0 to {start}')
        if sum(1 for t in teams if isinstance(t, dict) and t.get("is_me")) != 1:
            problems.append('mark exactly one team as Dan\'s ("is_me": true)')
    claims = data.get("claims") or []
    if not isinstance(claims, list):
        return problems + ['"claims" must be a list']
    for i, c in enumerate(claims):
        if not isinstance(c, dict):
            problems.append(f"claim {i + 1}: not an object")
            continue
        where = f"claim {i + 1} ({c.get('player', '?')})"
        for key in ("player", "position", "team"):
            if not c.get(key):
                problems.append(f'{where}: missing "{key}"')
        if c.get("position") and c["position"] not in POSITIONS:
            problems.append(f'{where}: position "{c["position"]}" is not one of {sorted(POSITIONS)}')
        if not isinstance(c.get("week"), int) or not 1 <= c["week"] <= 18:
            problems.append(f'{where}: "week" must be a whole number from 1 to 18')
        if not _number(c.get("bid")) or c["bid"] < 0:
            problems.append(f'{where}: "bid" must be a dollar amount')
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
        if path == MANUAL_ROSTER_LOCAL:
            lines.append(f"  problem: {path.name} can't be read, so reports use the older seeded roster. Fix or rewrite it.")
        else:
            lines.append(f"  problem: {path.name} can't be read, so reports have no roster.")
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
        if err != "not there":
            lines.append(f"  problem: {MANUAL_FREE_AGENTS.name} can't be read. Fix or rewrite it.")
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
        if err != "not there":
            lines.append(f"  problem: {MANUAL_MATCHUP.name} can't be read. Fix or rewrite it.")
    else:
        problems = check_matchup(data)
        opp = (data.get("opponent") or {}).get("team_name", "?")
        wk = data.get("week")
        note = ""
        if current_week is not None and wk != current_week:
            note = f" Not used: this is week {current_week}."
        lines.append(f"Matchup: week {wk} vs {opp}, as of {data.get('as_of')}.{note}")
        lines += [f"  problem: {p}" for p in problems]

    data, err = load(FAAB_MARKET)
    if err:
        lines.append(f"FAAB market: {err}. Bids won't be checked against what this league pays.")
        if err != "not there":
            lines.append(f"  problem: {FAAB_MARKET.name} can't be read. Fix or rewrite it.")
    elif not isinstance(data, dict):
        lines.append("FAAB market: not a JSON object. Not used by reports.")
        lines.append(f"  problem: {FAAB_MARKET.name} must be a JSON object. Fix or rewrite it.")
    else:
        problems = check_faab_market(data)
        as_of = _check_as_of(data, [])
        market = faab_market(now)
        if market is None:
            lines.append("FAAB market: nothing usable in it. Not used by reports until fixed.")
        else:
            weeks = sorted({c["week"] for c in market["recent_claims"]})
            span = f" (latest week {weeks[-1]})" if weeks else ""
            stale = " Budgets STALE: send a fresh league page." if as_of and _budgets_stale(as_of, now) else ""
            lines.append(f"FAAB market: {sum(p['claims'] for p in market['by_position'].values())} winning bids"
                         f"{span}, {len(market['teams'])} team budgets as of {data.get('as_of')}"
                         f" ({age(as_of) if as_of else '?'}).{stale}")
        lines += [f"  problem: {p}" for p in problems]
    return "\n".join(lines)
