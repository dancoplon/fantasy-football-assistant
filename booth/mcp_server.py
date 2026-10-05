"""Booth's Yahoo Fantasy MCP server: six read-only tools for one league.

Run with `uv run booth-mcp` (stdio). Claude Code picks it up from .mcp.json.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from booth.auth import AuthError
from booth.config import ConfigError, Settings
from booth.manual import manual_roster
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

    week: NFL week; omit for today's roster. If Yahoo is unreachable, returns the manual
    roster from config/manual_roster.json with source="manual" and its as_of date.
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
    """
    try:
        return client().free_agents(position=position, limit=limit, sort_type=sort_type)
    except (ConfigError, AuthError, YahooAccessError) as exc:
        return _error(exc)


@mcp.tool(annotations=READ_ONLY)
def get_matchup(week: int | None = None) -> dict:
    """My head-to-head matchup: both teams' points and Yahoo projections, plus the opponent's roster.

    week: NFL week; omit for the current week.
    """
    try:
        return client().matchup(week)
    except (ConfigError, AuthError, YahooAccessError) as exc:
        return _error(exc)


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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
