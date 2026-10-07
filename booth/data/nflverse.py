"""nflverse public data: schedule (with betting lines), weekly player usage, official injury reports.

Sources (free, no auth):
  schedule  https://github.com/nflverse/nfldata  data/games.csv  (kickoff times are ET)
  usage     nflverse-data releases: stats_player_week_{season}.csv, snap_counts_{season}.csv
  injuries  nflverse-data releases: injuries_{season}.csv (the NFL's Wed-Fri practice/game reports)
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from booth.data.cache import fetch
from booth.data.names import normalize

SEASON = 2026
GAMES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
RELEASES = "https://github.com/nflverse/nflverse-data/releases/download"


def _rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _num(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "", "NA") else None
    except ValueError:
        return None


# --- Schedule ------------------------------------------------------------------


def games(season: int = SEASON, path: Path | None = None) -> list[dict]:
    path = path or fetch(GAMES_URL, "games.csv", max_age_hours=6)[0]
    return [r for r in _rows(path) if r["season"] == str(season) and r["game_type"] == "REG"]


def current_week(all_games: list[dict], today: date | None = None) -> int:
    """The week whose last game hasn't happened yet (Tuesday rolls to the next week)."""
    today = today or date.today()
    last_day: dict[int, str] = {}
    for g in all_games:
        wk = int(g["week"])
        last_day[wk] = max(last_day.get(wk, ""), g["gameday"])
    for wk in sorted(last_day):
        if date.fromisoformat(last_day[wk]) >= today:
            return wk
    return max(last_day)


def _implied_totals(g: dict) -> tuple[float | None, float | None]:
    """nflverse spread_line is home-minus-away margin (positive = home favored)."""
    spread, total = _num(g.get("spread_line")), _num(g.get("total_line"))
    if spread is None or total is None:
        return None, None
    home = round((total + spread) / 2, 1)
    return home, round(total - home, 1)


# Yahoo writes some team codes differently from nflverse (and in mixed case, e.g. "Cin").
YAHOO_TEAM_CODES = {"LAR": "LA", "JAC": "JAX", "WSH": "WAS"}


def team_code(abbr: str | None) -> str | None:
    """nflverse's code for a Yahoo editorial_team_abbr (e.g. "Cin" -> "CIN", "LAR" -> "LA")."""
    if not abbr:
        return None
    code = abbr.strip().upper()
    return YAHOO_TEAM_CODES.get(code, code)


def bye_weeks(all_games: list[dict]) -> dict[str, int]:
    """Each team's bye week: the regular-season week it has no game."""
    weeks = sorted({int(g["week"]) for g in all_games})
    playing: dict[int, set[str]] = {}
    for g in all_games:
        playing.setdefault(int(g["week"]), set()).update((g["home_team"], g["away_team"]))
    teams = set().union(*playing.values()) if playing else set()
    byes: dict[str, int] = {}
    for wk in weeks:
        for t in teams - playing[wk]:
            byes.setdefault(t, wk)
    return byes


def schedule(week: int, all_games: list[dict]) -> dict:
    wk_games = [g for g in all_games if int(g["week"]) == week]
    teams_all = {t for g in all_games for t in (g["home_team"], g["away_team"])}
    playing = {t for g in wk_games for t in (g["home_team"], g["away_team"])}
    out = []
    for g in sorted(wk_games, key=lambda g: (g["gameday"], g["gametime"])):
        home_imp, away_imp = _implied_totals(g)
        out.append(
            {
                "game_id": g["game_id"],
                "away": g["away_team"],
                "home": g["home_team"],
                "day": g["weekday"],
                "date": g["gameday"],
                "kickoff_et": g["gametime"],
                "neutral_site": g["location"] == "Neutral",
                "stadium": g["stadium"],
                "stadium_id": g["stadium_id"],
                "roof": g["roof"] or "retractable (TBD)",
                "spread_home": _num(g.get("spread_line")),
                "total": _num(g.get("total_line")),
                "implied_home": home_imp,
                "implied_away": away_imp,
                "away_qb": g.get("away_qb_name") or None,
                "home_qb": g.get("home_qb_name") or None,
                "final": None if not g.get("home_score") else f"{g['away_team']} {g['away_score']} - {g['home_team']} {g['home_score']}",
            }
        )
    return {"season": SEASON, "week": week, "byes": sorted(teams_all - playing), "games": out}


# --- Usage -----------------------------------------------------------------------


def _stats_rows(season: int) -> list[dict]:
    path, _ = fetch(f"{RELEASES}/stats_player/stats_player_week_{season}.csv", f"stats_player_week_{season}.csv", 12)
    return _rows(path)


def _snap_rows(season: int) -> list[dict]:
    path, _ = fetch(f"{RELEASES}/snap_counts/snap_counts_{season}.csv", f"snap_counts_{season}.csv", 12)
    return _rows(path)


def half_ppr_points(row: dict) -> float | None:
    """League scoring = nflverse standard points + 0.5 per reception.

    nflverse 'fantasy_points' already uses 25 pass yds/pt, 4/pass TD, -2/INT,
    10 rush+rec yds/pt, 6/TD, -2/fumble lost, +2/2PT, matching UTA Hall of Famers.
    """
    std, rec = _num(row.get("fantasy_points")), _num(row.get("receptions")) or 0.0
    return None if std is None else round(std + 0.5 * rec, 2)


def player_usage(
    names: list[str] | None = None,
    team: str | None = None,
    position: str | None = None,
    last_n_weeks: int = 3,
    season: int = SEASON,
    stats_rows: list[dict] | None = None,
    snap_rows: list[dict] | None = None,
) -> list[dict]:
    """Per-player recent usage: weekly targets, target share, carries, snap %, half-PPR points."""
    stats_rows = stats_rows if stats_rows is not None else _stats_rows(season)
    snap_rows = snap_rows if snap_rows is not None else _snap_rows(season)
    weeks = sorted({int(r["week"]) for r in stats_rows if r.get("season_type", "REG") == "REG"})
    window = set(weeks[-last_n_weeks:])
    wanted = {normalize(n) for n in names} if names else None

    snaps = {
        (normalize(r["player"]), r["team"], int(r["week"])): _num(r["offense_pct"])
        for r in snap_rows
        if int(r["week"]) in window
    }
    players: dict[tuple[str, str], dict] = {}
    for r in stats_rows:
        wk = int(r["week"])
        if wk not in window or r.get("season_type", "REG") != "REG":
            continue
        key = normalize(r["player_display_name"])
        if wanted is not None and key not in wanted:
            continue
        if team and r["team"] != team:
            continue
        if position and r["position"] != position:
            continue
        p = players.setdefault(
            (key, r["team"]),
            {"name": r["player_display_name"], "team": r["team"], "position": r["position"], "weeks": []},
        )
        p["weeks"].append(
            {
                "week": wk,
                "opp": r["opponent_team"],
                "snap_pct": snaps.get((key, r["team"], wk)),
                "targets": int(_num(r["targets"]) or 0),
                "target_share": round(_num(r["target_share"]) or 0, 3),
                "carries": int(_num(r["carries"]) or 0),
                "receptions": int(_num(r["receptions"]) or 0),
                "yards": int((_num(r["rushing_yards"]) or 0) + (_num(r["receiving_yards"]) or 0) + (_num(r["passing_yards"]) or 0)),
                "tds": int(sum(_num(r[k]) or 0 for k in ("passing_tds", "rushing_tds", "receiving_tds"))),
                "points": half_ppr_points(r),
            }
        )
    out = []
    for p in players.values():
        p["weeks"].sort(key=lambda w: w["week"])
        pts = [w["points"] for w in p["weeks"] if w["points"] is not None]
        p["avg_points"] = round(sum(pts) / len(pts), 2) if pts else None
        out.append(p)
    return sorted(out, key=lambda p: -(p["avg_points"] or 0))


# --- Injury reports ----------------------------------------------------------------


def injury_report(
    week: int | None = None,
    names: list[str] | None = None,
    teams: list[str] | None = None,
    season: int = SEASON,
    rows: list[dict] | None = None,
) -> dict:
    """The NFL's official injury report for a week (game status + practice participation).

    Published Wed-Fri before games, so early in the week it may be empty for the new week.
    """
    if rows is None:
        path, stale = fetch(f"{RELEASES}/injuries/injuries_{season}.csv", f"injuries_{season}.csv", 3)
        rows = _rows(path)
    else:
        stale = False
    weeks = sorted({int(r["week"]) for r in rows})
    week = week or (weeks[-1] if weeks else None)
    wanted = {normalize(n) for n in names} if names else None
    entries = [
        {
            "name": r["full_name"],
            "team": r["team"],
            "position": r["position"],
            "game_status": r["report_status"] or None,
            "injury": r["report_primary_injury"] or r["practice_primary_injury"] or None,
            "practice": r["practice_status"] or None,
        }
        for r in rows
        if week is not None
        and int(r["week"]) == week
        and (wanted is None or normalize(r["full_name"]) in wanted)
        and (not teams or r["team"] in teams)
    ]
    return {
        "season": season,
        "week": week,
        "latest_week_published": weeks[-1] if weeks else None,
        "stale_copy": stale,
        "source": f"{RELEASES}/injuries/injuries_{season}.csv",
        "entries": entries,
    }
