"""Thin access layer over yahoo_fantasy_api for Booth's one league."""

from __future__ import annotations

import yahoo_fantasy_api as yfa

from booth.auth import get_session
from booth.config import Settings


class YahooAccessError(RuntimeError):
    pass


def _explain(exc: Exception) -> YahooAccessError:
    text = str(exc)
    lowered = text.lower()
    if "401" in text or "403" in text or "forbidden" in lowered or "not authorized" in lowered:
        return YahooAccessError(
            "OAuth worked but the Fantasy API refused the request (401/403). "
            "Yahoo may not have provisioned API access for the app yet. "
            f"Raw error: {text[:300]}"
        )
    return YahooAccessError(f"Yahoo Fantasy API call failed: {text[:300]}")


def get_league(settings: Settings, sc=None) -> yfa.League:
    sc = sc or get_session(settings)
    try:
        game = yfa.Game(sc, settings.game_code)
        league_key = f"{game.game_id()}.l.{settings.league_id}"
        return game.to_league(league_key)
    except Exception as exc:  # yahoo_fantasy_api raises bare RuntimeError/HTTP errors
        raise _explain(exc) from exc


def get_league_settings(settings: Settings, league: yfa.League | None = None) -> dict:
    league = league or get_league(settings)
    try:
        return league.settings()
    except Exception as exc:
        raise _explain(exc) from exc


def _has_waiver_source(node) -> bool:
    if isinstance(node, dict):
        if node.get("source_type") == "waivers":
            return True
        return any(_has_waiver_source(v) for v in node.values())
    if isinstance(node, list):
        return any(_has_waiver_source(v) for v in node)
    return False


def waiver_claim_times(settings: Settings, league: yfa.League | None = None, count: int = 50) -> list[int]:
    """Unix timestamps of recent adds that came off waivers.

    Yahoo's settings say which days waivers run but not the hour claims process,
    so Booth reads it from when past claims actually went through.
    """
    league = league or get_league(settings)
    try:
        txns = league.transactions("add", str(count))
    except Exception as exc:
        raise _explain(exc) from exc
    return sorted(
        (int(t["timestamp"]) for t in txns if "timestamp" in t and _has_waiver_source(t)),
        reverse=True,
    )


# --- Helpers for Yahoo's JSON shape -------------------------------------------
# Yahoo returns objects as lists of single-key dicts and collections as dicts
# keyed "0", "1", ... plus "count". These helpers flatten that.


def _merge(node) -> dict:
    """Merge a Yahoo list-of-dicts (possibly nested lists) into one dict."""
    out: dict = {}
    if isinstance(node, dict):
        out.update(node)
    elif isinstance(node, list):
        for item in node:
            out.update(_merge(item))
    return out


def _indexed(collection: dict) -> list:
    """Values of a Yahoo collection dict ({"0": ..., "1": ..., "count": n}) in order."""
    if not isinstance(collection, dict):
        return []
    keys = sorted((k for k in collection if k.isdigit()), key=int)
    return [collection[k] for k in keys]


def _find_all(node, key: str):
    """Yield every value stored under `key` anywhere in a nested structure."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                yield v
            yield from _find_all(v, key)
    elif isinstance(node, list):
        for v in node:
            yield from _find_all(v, key)


def _total(points) -> float | None:
    if isinstance(points, dict) and points.get("total") not in (None, ""):
        return float(points["total"])
    return None


def parse_scoreboard(raw: dict) -> list[dict]:
    """Turn a league scoreboard into [{week, status, teams: [{team_key, name, points, projected_points}]}]."""
    matchups = []
    for m in _find_all(raw, "matchup"):
        if not isinstance(m, dict):
            continue
        teams = []
        for teams_coll in _find_all(m, "teams"):
            for entry in _indexed(teams_coll):
                team = entry.get("team", [])
                info = _merge(team[0]) if team else {}
                stats = team[1] if len(team) > 1 and isinstance(team[1], dict) else {}
                teams.append(
                    {
                        "team_key": info.get("team_key"),
                        "name": info.get("name"),
                        "points": _total(stats.get("team_points")),
                        "projected_points": _total(stats.get("team_projected_points")),
                        "win_probability": stats.get("win_probability"),
                    }
                )
            break
        matchups.append(
            {
                "week": m.get("week"),
                "week_start": m.get("week_start"),
                "week_end": m.get("week_end"),
                "status": m.get("status"),
                "teams": teams,
            }
        )
    return matchups


def summarize_transaction(txn: dict, tz=None) -> dict:
    """Flatten one yahoo_fantasy_api transaction into names and moves."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = tz or ZoneInfo("America/New_York")
    moves = []
    for entry in _indexed(txn.get("players", {})):
        player = entry.get("player", [])
        info = _merge(player[0]) if player else {}
        data = _merge(player[1].get("transaction_data")) if len(player) > 1 else {}
        name = info.get("name", {})
        moves.append(
            {
                "player": name.get("full") if isinstance(name, dict) else name,
                "nfl_team": info.get("editorial_team_abbr"),
                "position": info.get("display_position"),
                "action": data.get("type"),
                "source": data.get("source_type"),
                "to_team": data.get("destination_team_name"),
                "from_team": data.get("source_team_name"),
            }
        )
    ts = txn.get("timestamp")
    return {
        "type": txn.get("type"),
        "status": txn.get("status"),
        "faab_bid": txn.get("faab_bid"),
        "time_et": datetime.fromtimestamp(int(ts), tz).strftime("%a %b %d %I:%M %p") if ts else None,
        "moves": moves,
    }


# --- The six read-only calls behind the MCP tools ----------------------------


class YahooClient:
    """Read-only access to one league. Refreshes the token once on creation."""

    def __init__(self, settings: Settings, league: yfa.League | None = None):
        self.settings = settings
        self._league = league
        self._team_key: str | None = None

    @property
    def league(self) -> yfa.League:
        if self._league is None:
            self._league = get_league(self.settings)
        return self._league

    @property
    def team_key(self) -> str:
        if self._team_key is None:
            try:
                self._team_key = self.league.team_key()
            except Exception as exc:
                raise _explain(exc) from exc
        return self._team_key

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except YahooAccessError:
            raise
        except Exception as exc:
            raise _explain(exc) from exc

    def league_settings(self) -> dict:
        return self._call(self.league.settings)

    def my_roster(self, week: int | None = None) -> dict:
        team = self.league.to_team(self.team_key)
        players = self._call(team.roster, week=week)
        return {"source": "yahoo", "team_key": self.team_key, "week": week, "players": players}

    def free_agents(
        self, position: str | None = None, limit: int = 50, sort: str = "AR", sort_type: str = "lastweek"
    ) -> list[dict]:
        """Available players (free agents and players on waivers), best first.

        sort: AR = actual rank, OR = overall rank, PTS = fantasy points.
        sort_type: season | lastweek | lastmonth.
        """
        players: list[dict] = []
        start = 0
        while len(players) < limit:
            uri = f"league/{self.league.league_id}/players;start={start};count=25;status=A"
            if position:
                uri += f";position={position}"
            uri += f";sort={sort};sort_type={sort_type}/percent_owned"
            page = self._call(self.league.yhandler.get, uri)
            num, found = self._call(self.league._players_from_page, page)
            players += found
            if num < 25:
                break
            start += 25
        return players[:limit]

    def matchup(self, week: int | None = None) -> dict:
        raw = self._call(self.league.matchups, week)
        for m in parse_scoreboard(raw):
            keys = [t["team_key"] for t in m["teams"]]
            if self.team_key in keys:
                me = next(t for t in m["teams"] if t["team_key"] == self.team_key)
                opp = next((t for t in m["teams"] if t["team_key"] != self.team_key), None)
                result = {"week": m["week"], "status": m["status"], "me": me, "opponent": opp}
                if opp and opp.get("team_key"):
                    opp_team = self.league.to_team(opp["team_key"])
                    wk = int(m["week"]) if m.get("week") else week
                    result["opponent_roster"] = self._call(opp_team.roster, week=wk)
                return result
        raise YahooAccessError(f"No matchup found for team {self.team_key} in week {week or 'current'}.")

    def transactions(self, types: str = "add,drop,trade", count: int = 25) -> list[dict]:
        txns = self._call(self.league.transactions, types, str(count))
        return [summarize_transaction(t) for t in txns]

    def standings(self) -> list[dict]:
        rows = self._call(self.league.standings)
        for r in rows:
            r["is_me"] = r.get("team_key") == self.team_key
        return rows
