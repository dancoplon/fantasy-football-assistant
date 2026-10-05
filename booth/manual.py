"""Manual roster and free-agent fallbacks for when the Yahoo API isn't reachable or provisioned."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from booth.config import ROOT

MANUAL_ROSTER = ROOT / "config" / "manual_roster.json"
MANUAL_FREE_AGENTS = ROOT / "config" / "manual_free_agents.json"
# Waivers turn over weekly, so an older saved list would mostly recommend players who are gone.
FREE_AGENTS_MAX_AGE = timedelta(days=7)
FLEX = {"RB", "WR", "TE"}


def manual_roster() -> dict:
    data = json.loads(MANUAL_ROSTER.read_text())
    return {"source": "manual", **{k: v for k, v in data.items() if not k.startswith("_")}}


def manual_free_agents(position: str | None = None, limit: int = 50, now: datetime | None = None) -> dict | None:
    """Dan's saved copy of Yahoo's available-players list, or None if there isn't a usable one."""
    if not MANUAL_FREE_AGENTS.exists():
        return None
    data = json.loads(MANUAL_FREE_AGENTS.read_text())
    as_of = datetime.fromisoformat(data["as_of"])
    if (now or datetime.now(timezone.utc)) - as_of > FREE_AGENTS_MAX_AGE:
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
