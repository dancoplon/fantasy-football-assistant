"""Sleeper's public trending-players API: league-wide add/drop momentum (free, no auth).

https://docs.sleeper.com/#trending-players. Sleeper asks that the full players map
(~5 MB) be pulled at most once a day, so it's cached for 24h.
"""

from __future__ import annotations

import json

from booth.data.cache import fetch, get_json

BASE = "https://api.sleeper.app/v1"
FANTASY_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DEF"}


def players_map() -> dict:
    path, _ = fetch(f"{BASE}/players/nfl", "sleeper_players.json", max_age_hours=24)
    return json.loads(path.read_text())


def trending(
    direction: str = "add",
    lookback_hours: int = 24,
    limit: int = 25,
    position: str | None = None,
    players: dict | None = None,
    raw: list | None = None,
) -> list[dict]:
    """Most-added (or most-dropped) players across Sleeper leagues in the lookback window."""
    if direction not in ("add", "drop"):
        raise ValueError("direction must be 'add' or 'drop'")
    if raw is None:
        # Over-fetch so position filtering still leaves `limit` players.
        raw = get_json(
            f"{BASE}/players/nfl/trending/{direction}",
            {"lookback_hours": lookback_hours, "limit": min(200, limit * (4 if position else 1))},
        )
    players = players if players is not None else players_map()
    out = []
    for item in raw:
        p = players.get(str(item["player_id"]), {})
        pos = p.get("position")
        if pos not in FANTASY_POSITIONS or (position and pos != position):
            continue
        name = p.get("full_name") or " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x)
        out.append(
            {
                "name": name,
                "position": pos,
                "team": p.get("team"),
                "count": item["count"],
                "injury_status": p.get("injury_status"),
                "depth_chart_order": p.get("depth_chart_order"),
                "age": p.get("age"),
                "years_exp": p.get("years_exp"),
                "sleeper_id": str(item["player_id"]),
            }
        )
        if len(out) >= limit:
            break
    return out
