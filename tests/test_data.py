import csv
import time
from datetime import date
from pathlib import Path
from unittest import mock

import pytest
import requests

from booth.data import cache, nflverse, sleeper, weather
from booth.data.names import normalize

FIX = Path(__file__).parent / "fixtures"


def _csv(name):
    with (FIX / name).open(newline="") as f:
        return list(csv.DictReader(f))


@pytest.fixture
def games():
    return nflverse.games(path=FIX / "games.csv")


@pytest.mark.parametrize(
    "raw,expected",
    [("J.K. Dobbins", "jk dobbins"), ("Mike Washington Jr.", "mike washington"),
     ("KC Concepcion Jr.", "kc concepcion"), ("Amon-Ra St. Brown", "amon ra st brown"), ("Ja'Marr Chase", "jamarr chase")],
)
def test_normalize(raw, expected):
    assert normalize(raw) == expected


def test_current_week_rolls_over_on_tuesday(games):
    assert nflverse.current_week(games, date(2026, 10, 7)) == 5
    assert nflverse.current_week(games, date(2026, 10, 12)) == 5   # MNF day
    assert nflverse.current_week(games, date(2026, 10, 13)) == 6   # waiver Tuesday


def test_schedule_week6_byes_and_tnf(games):
    s = nflverse.schedule(6, games)
    # Fixture only has a few games, so byes are relative to it; the real file gives CIN, DET, MIA, MIN.
    assert "CIN" in s["byes"] and "MIA" in s["byes"]
    tnf = s["games"][0]
    assert (tnf["away"], tnf["home"], tnf["day"], tnf["kickoff_et"]) == ("SEA", "DEN", "Thursday", "20:15")
    london = next(g for g in s["games"] if g["home"] == "JAX")
    assert london["neutral_site"] and london["stadium"] == "Wembley Stadium"


def test_implied_totals(games):
    tnf = nflverse.schedule(5, games)["games"][0]
    assert (tnf["away"], tnf["home"], tnf["spread_home"], tnf["total"]) == ("TB", "DAL", 9.5, 47.5)
    assert (tnf["implied_home"], tnf["implied_away"]) == (28.5, 19.0)
    assert tnf["roof"] == "retractable (TBD)"


def test_half_ppr_matches_league_scoring():
    row = {"fantasy_points": "10.0", "receptions": "6"}
    assert nflverse.half_ppr_points(row) == 13.0


def test_player_usage(games):
    out = nflverse.player_usage(["Rome Odunze", "J.K. Dobbins"], stats_rows=_csv("stats_player_week.csv"),
                                snap_rows=_csv("snap_counts.csv"), last_n_weeks=2)
    names = {p["name"] for p in out}
    assert names == {"Rome Odunze", "J.K. Dobbins"}
    odunze = next(p for p in out if p["name"] == "Rome Odunze")
    assert [w["week"] for w in odunze["weeks"]] == [3, 4]
    wk4 = odunze["weeks"][-1]
    assert wk4["targets"] == 7 and wk4["snap_pct"] == 0.89 and wk4["points"] == 12.4


def test_injury_report_filters(games):
    r = nflverse.injury_report(names=["Justin Jefferson", "Mason Taylor"], rows=_csv("injuries.csv"))
    assert r["week"] == 4
    statuses = {e["name"]: e["game_status"] for e in r["entries"]}
    assert statuses == {"Justin Jefferson": "Out", "Mason Taylor": "Out"}


SLEEPER_PLAYERS = {
    "1": {"full_name": "Backup Back", "position": "RB", "team": "NYJ", "injury_status": None, "age": 23},
    "2": {"first_name": "Lineman", "last_name": "Guy", "position": "OT", "team": "NYJ"},
    "3": {"full_name": "Stream Qb", "position": "QB", "team": "NO", "injury_status": "Questionable"},
}


def test_sleeper_trending_maps_and_filters():
    raw = [{"player_id": "1", "count": 900}, {"player_id": "2", "count": 500}, {"player_id": "3", "count": 300}]
    out = sleeper.trending(raw=raw, players=SLEEPER_PLAYERS)
    assert [p["name"] for p in out] == ["Backup Back", "Stream Qb"]  # OT dropped
    qbs = sleeper.trending(raw=raw, players=SLEEPER_PLAYERS, position="QB")
    assert qbs[0]["injury_status"] == "Questionable" and qbs[0]["count"] == 300


def _forecast(day, kick_hour, wind=5.0, gust=10.0, prob=0, precip=0.0, snow=0.0, temp=60.0):
    times = [f"{day}T{h:02d}:00" for h in range(24)]
    return {"hourly": {
        "time": times,
        "temperature_2m": [temp] * 24,
        "wind_speed_10m": [wind if kick_hour <= h < kick_hour + 4 else 3.0 for h in range(24)],
        "wind_gusts_10m": [gust] * 24,
        "precipitation_probability": [prob] * 24,
        "precipitation": [precip] * 24,
        "snowfall": [snow] * 24,
    }}


def test_weather_flags_wind_and_skips_indoor(games):
    sched = nflverse.schedule(6, games)["games"]
    calls = []

    def fake(lat, lon, day):
        calls.append((lat, lon, day))
        return _forecast(day, 20, wind=18.0, gust=30.0)

    out = weather.game_weather(sched, today=date(2026, 10, 14), fetch_forecast=fake)
    tnf = next(o for o in out if o["game"] == "SEA@DEN")
    assert tnf["flags"] and "wind 18 mph" in tnf["flags"][0]
    assert tnf["low_confidence"] is False
    assert all(o.get("indoor") is not True or o["flags"] == [] for o in out)


def test_weather_rain_snow_cold_flags():
    hours = [{"temp_f": 20.0, "wind_mph": 5, "gust_mph": 8, "precip_prob": 80, "precip_in": 0.1, "snow_in": 0.3}]
    flags = weather.flags_for(hours)
    assert any("snow" in f for f in flags) and any("cold" in f for f in flags)
    calm = [{"temp_f": 65.0, "wind_mph": 5, "gust_mph": 8, "precip_prob": 10, "precip_in": 0, "snow_in": 0}]
    assert weather.flags_for(calm) == []


def test_weather_out_of_range_not_fetched(games):
    sched = nflverse.schedule(6, games)["games"]
    out = weather.game_weather(sched, today=date(2026, 9, 1), fetch_forecast=lambda *a: pytest.fail("fetched"))
    assert all(o.get("note") == "outside forecast range" or o.get("indoor") for o in out)


def test_cache_falls_back_to_stale_copy(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("old")
    old = time.time() - 48 * 3600
    import os
    os.utime(p, (old, old))
    with mock.patch.object(cache.requests, "get", side_effect=requests.ConnectionError("down")):
        path, stale = cache.fetch("https://example.invalid/x.csv", "x.csv", 1, cache_dir=tmp_path)
    assert stale and path.read_text() == "old"


def test_cache_raises_without_any_copy(tmp_path):
    with mock.patch.object(cache.requests, "get", side_effect=requests.ConnectionError("down")):
        with pytest.raises(cache.DataSourceError):
            cache.fetch("https://example.invalid/y.csv", "y.csv", 1, cache_dir=tmp_path)


def test_every_stadium_in_fixture_has_coordinates(games):
    import json
    stadiums = json.loads((Path(__file__).parent.parent / "config" / "stadiums.json").read_text())
    assert {g["stadium_id"] for g in games} <= set(stadiums)
