"""Booth's MCP server: six read-only Yahoo tools for one league, plus external data tools.

Run with `uv run booth-mcp` (stdio). Claude Code picks it up from .mcp.json.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from booth.auth import AuthError
from booth.config import ConfigError, Settings
from booth.context import league_config, strategy_text
from booth.data import nflverse, sleeper, weather
from booth.data.cache import DataSourceError
from booth.manual import faab_market, manual_free_agents, manual_matchup, manual_roster
from booth.yahoo import YahooAccessError, YahooClient

mcp = MCPServer("yahoo-fantasy")

# Every tool is read-only; Booth has no write path to Yahoo.
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)

_client: YahooClient | None = None


def client() -> YahooClient:
    global _client
    if _client is None:
        _client = YahooClient(Settings.load())
    return _client


def _error(exc: Exception) -> dict:
    return {"error": str(exc)}


@mcp.tool(annotations=READ_ONLY)
def get_league_settings() -> dict:
    """League settings for UTA Hall of Famers: scoring, roster slots, waiver rules, FAAB, trade deadline, playoffs."""
    try:
        return client().league_settings()
    except (ConfigError, AuthError, YahooAccessError) as exc:
        return _error(exc)


@mcp.tool(annotations=READ_ONLY)
def get_my_roster(week: int | None = None) -> dict:
    """My team's roster (Mayor of Titty City) with each player's slot, eligible positions, and injury status.

    week: NFL week; omit for today's roster. If Yahoo is unreachable, returns Dan's copy of his
    team page (config/manual_roster.local.json, else the seeded config/manual_roster.json) with
    source="manual", its as_of date, and a "warning" when it may be out of date.
    """
    try:
        return client().my_roster(week)
    except (ConfigError, AuthError, YahooAccessError) as exc:
        return {**manual_roster(), "yahoo_error": str(exc)}


@mcp.tool(annotations=READ_ONLY)
def get_free_agents(position: str | None = None, limit: int = 50, sort_type: str = "lastweek") -> list[dict] | dict:
    """Available players (free agents and players on waivers), ranked by Yahoo actual rank.

    position: QB, RB, WR, TE, K, DEF, or W/R/T; omit for all positions.
    limit: max players to return (default 50).
    sort_type: ranking window, one of lastweek, lastmonth, season.
    If Yahoo is unreachable, returns Dan's saved list from config/manual_free_agents.json
    with source="manual", its as_of time, and a note on what it covers.
    """
    try:
        return client().free_agents(position=position, limit=limit, sort_type=sort_type)
    except (ConfigError, AuthError, YahooAccessError) as exc:
        saved = manual_free_agents(position, limit)
        if saved is None:
            return {"error": f"{exc} No saved list of available players from the last 7 days either."}
        return {**saved, "yahoo_error": str(exc)}


@mcp.tool(annotations=READ_ONLY)
def get_matchup(week: int | None = None) -> dict:
    """My head-to-head matchup: both teams' points and Yahoo projections, plus the opponent's roster.

    week: NFL week; omit for the current week. If Yahoo is unreachable, returns Dan's copy of
    his Yahoo matchup page (config/manual_matchup.json, source="manual") when it's for that week.
    """
    try:
        return client().matchup(week)
    except (ConfigError, AuthError, YahooAccessError) as exc:
        if week is None:
            try:
                week = nflverse.current_week(nflverse.games())
            except DataSourceError:
                pass  # fall back to the copy's age alone
        saved = manual_matchup(week)
        if saved is None:
            return {"error": f"{exc} No copy of this week's matchup page either."}
        return {**saved, "yahoo_error": str(exc)}


@mcp.tool(annotations=READ_ONLY)
def get_transactions(types: str = "add,drop,trade", count: int = 25) -> list[dict] | dict:
    """Recent league transactions, newest first, with times in ET and FAAB bids where shown.

    types: comma-separated subset of add, drop, trade, commish.
    count: how many transactions to return.
    """
    try:
        return client().transactions(types, count)
    except (ConfigError, AuthError, YahooAccessError) as exc:
        return _error(exc)


@mcp.tool(annotations=READ_ONLY)
def get_standings() -> list[dict] | dict:
    """League standings: rank, record, points for/against; my team is marked is_me=true."""
    try:
        return client().standings()
    except (ConfigError, AuthError, YahooAccessError) as exc:
        return _error(exc)


# --- External data (no Yahoo needed) ------------------------------------------


@mcp.tool(annotations=READ_ONLY)
def get_league_context() -> dict:
    """League rules (scoring, roster slots, waivers, deadlines), Dan's strategy notes, and what this
    league pays on waivers ("faab_market": each team's budget left, recent winning bids, price
    ranges by position, claims Dan lost). Read this first on every run."""
    market = faab_market() or {"note": "No copy of the league's FAAB history yet (config/faab_market.json)."}
    return {"league": league_config(), "strategy": strategy_text(), "faab_market": market}


@mcp.tool(annotations=READ_ONLY)
def get_schedule(week: int | None = None) -> dict:
    """NFL schedule for a week: kickoff times (ET), bye teams, stadium/roof, Vegas spread and total,
    and each team's implied points. week: omit for the current week (rolls forward on Tuesday)."""
    try:
        games = nflverse.games()
        return nflverse.schedule(week or nflverse.current_week(games), games)
    except DataSourceError as exc:
        return _error(exc)


@mcp.tool(annotations=READ_ONLY)
def get_player_usage(
    names: list[str] | None = None,
    team: str | None = None,
    position: str | None = None,
    last_n_weeks: int = 3,
) -> list[dict] | dict:
    """Recent usage from nflverse play-by-play: snap %, targets, target share, carries, yards, TDs,
    and points in this league's half-PPR scoring, week by week. Filter by names, NFL team (e.g. "HOU"),
    and/or position. Data updates the day after games."""
    try:
        return nflverse.player_usage(names=names, team=team, position=position, last_n_weeks=last_n_weeks)
    except DataSourceError as exc:
        return _error(exc)


@mcp.tool(annotations=READ_ONLY)
def get_injury_report(week: int | None = None, names: list[str] | None = None, teams: list[str] | None = None) -> dict:
    """The NFL's official injury report (game status: Out/Doubtful/Questionable, plus practice participation).
    Published Wednesday through Friday; cite the returned source URL when quoting it."""
    try:
        return nflverse.injury_report(week=week, names=names, teams=teams)
    except DataSourceError as exc:
        return _error(exc)


@mcp.tool(annotations=READ_ONLY)
def get_trending_players(
    direction: str = "add", lookback_hours: int = 24, limit: int = 25, position: str | None = None
) -> list[dict] | dict:
    """Most-added or most-dropped players across Sleeper fantasy leagues (waiver-wire momentum).
    direction: "add" or "drop". position: QB, RB, WR, TE, K, DEF, or omit."""
    try:
        return sleeper.trending(direction=direction, lookback_hours=lookback_hours, limit=limit, position=position)
    except (DataSourceError, ValueError) as exc:
        return _error(exc)


@mcp.tool(annotations=READ_ONLY)
def get_game_weather(week: int | None = None) -> list[dict] | dict:
    """Kickoff-window forecast (Open-Meteo) for every game in a week, with fantasy flags for wind,
    rain/snow, and cold. Indoor games are marked; forecasts more than 3 days out are low confidence."""
    try:
        games = nflverse.games()
        sched = nflverse.schedule(week or nflverse.current_week(games), games)
        return weather.game_weather(sched["games"])
    except DataSourceError as exc:
        return _error(exc)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
