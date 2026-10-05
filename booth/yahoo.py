"""Thin access layer over yahoo_fantasy_api for Booth's one league."""

from __future__ import annotations

import yahoo_fantasy_api as yfa

from booth.auth import get_session
from booth.config import Settings


class YahooAccessError(RuntimeError):
    pass


def _explain(exc: Exception) -> YahooAccessError:
    text = str(exc)
    if "401" in text or "403" in text or "forbidden" in text.lower():
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
