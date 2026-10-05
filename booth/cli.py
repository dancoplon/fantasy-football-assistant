"""Booth command line.

  uv run booth auth    one-time Yahoo authorization (opens a browser)
  uv run booth check   refresh the token and pull league settings (Milestone 1 check)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from booth.auth import AuthError, authorize_interactive
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="booth")
    sub = parser.add_subparsers(dest="command", required=True)
    p_auth = sub.add_parser("auth", help="one-time Yahoo authorization")
    p_auth.add_argument("--no-browser", action="store_true", help="print the URL instead of opening it")
    p_auth.set_defaults(func=cmd_auth)
    sub.add_parser("check", help="refresh token and pull league settings").set_defaults(func=cmd_check)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, AuthError, YahooAccessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
