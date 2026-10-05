"""Game-time weather from Open-Meteo (free, no key) for outdoor and retractable-roof games.

Forecasts reach ~16 days out but are only worth trusting inside ~3 days, so the
Thursday report treats them as early signals and Saturday/Sunday as the real check.
"""

from __future__ import annotations

import json
from datetime import date

from booth.config import ROOT
from booth.data.cache import DataSourceError, get_json

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
STADIUMS = ROOT / "config" / "stadiums.json"
INDOOR = {"dome", "closed"}

# Thresholds for a fantasy-relevant flag. Wind hurts passing and kicking first.
WIND_MPH = 15
GUST_MPH = 25
PRECIP_PROB = 50
COLD_F = 25


def _stadiums() -> dict:
    return json.loads(STADIUMS.read_text())


def flags_for(hours: list[dict]) -> list[str]:
    if not hours:
        return []
    wind = max(h["wind_mph"] or 0 for h in hours)
    gust = max(h["gust_mph"] or 0 for h in hours)
    prob = max(h["precip_prob"] or 0 for h in hours)
    snow = sum(h["snow_in"] or 0 for h in hours)
    rain = sum(h["precip_in"] or 0 for h in hours)
    temp = min(h["temp_f"] for h in hours if h["temp_f"] is not None) if any(h["temp_f"] is not None for h in hours) else None
    flags = []
    if wind >= WIND_MPH or gust >= GUST_MPH:
        flags.append(f"wind {round(wind)} mph, gusts {round(gust)}: downgrade passing and kickers")
    if snow >= 0.2:
        flags.append(f"snow ~{snow:.1f} in: downgrade passing, slight RB bump")
    elif prob >= PRECIP_PROB and rain >= 0.05:
        flags.append(f"rain likely ({prob}%, ~{rain:.2f} in): ball security and passing risk")
    if temp is not None and temp <= COLD_F:
        flags.append(f"cold ({round(temp)}°F)")
    return flags


def _game_hours(forecast: dict, game_date: str, kickoff_et: str) -> list[dict]:
    hourly = forecast.get("hourly", {})
    times = hourly.get("time", [])
    start = f"{game_date}T{kickoff_et[:2]}:00"
    try:
        i = times.index(start)
    except ValueError:
        return []
    out = []
    for j in range(i, min(i + 4, len(times))):  # kickoff through ~3 hours in
        def v(key):
            vals = hourly.get(key) or []
            return vals[j] if j < len(vals) else None
        out.append(
            {
                "time": times[j],
                "temp_f": v("temperature_2m"),
                "wind_mph": v("wind_speed_10m"),
                "gust_mph": v("wind_gusts_10m"),
                "precip_prob": v("precipitation_probability"),
                "precip_in": v("precipitation"),
                "snow_in": v("snowfall"),
            }
        )
    return out


def game_weather(games: list[dict], today: date | None = None, fetch_forecast=None) -> list[dict]:
    """games: entries from nflverse.schedule()['games']."""
    today = today or date.today()
    fetch_forecast = fetch_forecast or _fetch_forecast
    stadiums = _stadiums()
    out = []
    for g in games:
        entry = {"game": f"{g['away']}@{g['home']}", "date": g["date"], "kickoff_et": g["kickoff_et"], "roof": g["roof"]}
        if g["roof"] in INDOOR:
            out.append({**entry, "indoor": True, "flags": []})
            continue
        days_out = (date.fromisoformat(g["date"]) - today).days
        if days_out < 0 or days_out > 15:
            out.append({**entry, "note": "outside forecast range", "flags": []})
            continue
        st = stadiums.get(g["stadium_id"])
        if not st:
            out.append({**entry, "note": f"no coordinates for {g['stadium']}", "flags": []})
            continue
        try:
            hours = _game_hours(fetch_forecast(st["lat"], st["lon"], g["date"]), g["date"], g["kickoff_et"])
        except DataSourceError as exc:
            out.append({**entry, "error": str(exc), "flags": []})
            continue
        flags = flags_for(hours)
        if flags and g["roof"].startswith("retractable"):
            flags = [f + " (retractable roof may be closed)" for f in flags]
        out.append(
            {
                **entry,
                "indoor": False,
                "days_out": days_out,
                "low_confidence": days_out > 3,
                "kickoff_hour": hours[0] if hours else None,
                "flags": flags,
            }
        )
    return out


def _fetch_forecast(lat: float, lon: float, day: str) -> dict:
    return get_json(
        FORECAST_URL,
        {
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m,precipitation_probability,precipitation,snowfall,wind_speed_10m,wind_gusts_10m",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
            "timezone": "America/New_York",
            "start_date": day,
            "end_date": day,
        },
    )
