"""Booth command line.

  uv run booth auth    one-time Yahoo authorization (opens a browser)
  uv run booth check   refresh the token and pull league settings (Milestone 1 check)
  uv run booth data-check   pull each external source once and print a short summary
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from booth.auth import AuthError, authorization_url, authorize_interactive, exchange_code, extract_code
from booth.config import ROOT, ConfigError, Settings
from booth.yahoo import YahooAccessError, get_league, get_league_settings, waiver_claim_times

# Settings fields worth eyeballing on the first pull, waiver timing especially.
SUMMARY_KEYS = [
    "name",
    "num_teams",
    "scoring_type",
    "current_week",
    "start_week",
    "end_week",
    "uses_faab",
    "waiver_type",
    "waiver_rule",
    "waiver_time",
    "weekly_deadline",
    "uses_playoff",
    "playoff_start_week",
    "num_playoff_teams",
    "can_trade_draft_picks",
    "trade_end_date",
    "edit_key",
]


def cmd_auth(args: argparse.Namespace) -> int:
    settings = Settings.load()
    if args.url_only:
        print(authorization_url(settings))
        return 0
    if args.code:
        exchange_code(settings, extract_code(args.code))
    else:
        authorize_interactive(settings, open_browser=not args.no_browser)
    print(f"\nAuthorized. Tokens saved to {settings.token_file.relative_to(ROOT)}.")
    print("Next: uv run booth check")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    settings = Settings.load()
    league = get_league(settings)
    league_settings = get_league_settings(settings, league)
    print("Token refreshed and league settings pulled.\n")
    for key in SUMMARY_KEYS:
        if key in league_settings:
            print(f"  {key:22} {league_settings[key]}")
    out = ROOT / "state" / "league_settings.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(league_settings, indent=2, default=str))
    print(f"\nFull settings written to {out.relative_to(ROOT)}")

    # When do waiver claims actually process? Read it off past waiver adds.
    eastern = ZoneInfo("America/New_York")
    times = waiver_claim_times(settings, league)
    if times:
        print("\nRecent waiver claims processed at (ET):")
        for ts in times[:10]:
            print("  " + datetime.fromtimestamp(ts, eastern).strftime("%a %b %d  %I:%M %p"))
    else:
        print("\nNo waiver claims found in recent transactions; can't infer processing time yet.")
    return 0


def cmd_data_check(args: argparse.Namespace) -> int:
    """Smoke-test every external source. Prints one line per source; exit 1 if any failed."""
    from booth.data import nflverse, sleeper, weather

    failures = 0

    def run(label, fn):
        nonlocal failures
        try:
            print(f"  OK    {label}: {fn()}")
        except Exception as exc:  # report and keep going; this is a diagnostic
            failures += 1
            print(f"  FAIL  {label}: {exc}")

    print("External data sources:")
    games: list = []

    def sched():
        games.extend(nflverse.games())
        wk = nflverse.current_week(games)
        s = nflverse.schedule(wk, games)
        return f"week {wk}, {len(s['games'])} games, byes {', '.join(s['byes']) or 'none'}"

    run("nflverse schedule", sched)
    run("nflverse usage", lambda: f"{len(nflverse.player_usage(last_n_weeks=1))} players with stats last week")
    run("nflverse injury report", lambda: (lambda r: f"week {r['week']}, {len(r['entries'])} entries")(nflverse.injury_report()))
    run("Sleeper trending adds", lambda: ", ".join(p["name"] for p in sleeper.trending(limit=5)))

    def wx():
        # Next week's games, so the forecast API is actually exercised even on a Monday
        # when the only game left in the current week is indoors.
        wk = min(nflverse.current_week(games) + 1, max(int(g["week"]) for g in games))
        out = weather.game_weather(nflverse.schedule(wk, games)["games"])
        errors = [o["error"] for o in out if o.get("error")]
        if errors:
            raise RuntimeError(f"{len(errors)} forecast(s) failed, e.g. {errors[0]}")
        fetched = [o for o in out if o.get("kickoff_hour")]
        flagged = [f"{o['game']}: {'; '.join(o['flags'])}" for o in out if o["flags"]]
        return f"week {wk}: {len(fetched)} outdoor forecasts; flags: {' | '.join(flagged) or 'none'}"

    run("Open-Meteo weather", wx)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="booth")
    sub = parser.add_subparsers(dest="command", required=True)
    p_auth = sub.add_parser("auth", help="one-time Yahoo authorization")
    p_auth.add_argument("--no-browser", action="store_true", help="print the URL instead of opening it")
    # Non-interactive two-step form, for when someone else drives the terminal:
    #   booth auth --url-only           -> open the printed URL, approve
    #   booth auth --code "<redirect URL or code>"
    p_auth.add_argument("--url-only", action="store_true", help="print the consent URL and exit")
    p_auth.add_argument("--code", help="finish auth with the pasted redirect URL (or bare code)")
    p_auth.set_defaults(func=cmd_auth)
    sub.add_parser("check", help="refresh token and pull league settings").set_defaults(func=cmd_check)
    sub.add_parser("data-check", help="pull each external data source once").set_defaults(func=cmd_data_check)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, AuthError, YahooAccessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
