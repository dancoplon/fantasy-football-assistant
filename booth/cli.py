"""Booth command line.

  uv run booth auth    one-time Yahoo authorization (opens a browser)
  uv run booth check   refresh the token and pull league settings (Milestone 1 check)
  uv run booth data-check   pull each external source once and print a short summary
  uv run booth report thu   generate a report (tue, thu, sat, sun) into reports/ and print it
  uv run booth report thu --send   ...and deliver it
  uv run booth run due      what launchd runs: build and send whichever report is due
                            (and near each kickoff, check for inactive starters)
  uv run booth send-test    send a short test message (Telegram once set up, else iMessage)
  uv run booth telegram-setup   connect the Booth bot: prints a link to open in Telegram, then run it again
  uv run booth manual-status   check the copies of Dan's Yahoo pages used until Yahoo access works
  uv run booth schedule install|uninstall|status|wake|test-send
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


def cmd_report(args: argparse.Namespace) -> int:
    from booth.deliver import DeliveryError
    from booth.jobs import LockBusy, lock, record_manual_delivery
    from booth.report import ReportError, generate

    try:
        with lock():  # never alongside a scheduled run
            result = generate(args.run, week=args.week)
            print(result["message"])
            print(f"\n[saved {result['path'].relative_to(ROOT)}; cost ${result['snapshot'].get('cost_usd') or 0:.2f}]")
            if args.send:
                from booth.deliver import send

                print(f"[sent via {send(result['message'])}]")
                if record_manual_delivery(args.run, result["snapshot"]["week"]):
                    print("[marked delivered: the schedule won't send this report again]")
    except LockBusy:
        print("error: a scheduled Booth run is in progress; try again in a few minutes.", file=sys.stderr)
        return 1
    except (ReportError, DeliveryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from booth.inactives import run_inactives
    from booth.jobs import EASTERN, run_due

    outcomes = [run_due()]
    try:
        outcomes.append(run_inactives())
    except Exception as exc:  # never let the inactives check hide the report's outcome
        outcomes.append(f"failed inactives: {exc}")
    shown = [o for o in outcomes if o != "inactives: nothing due"]
    # launchd appends this to logs/launchd.out.log, which `booth schedule status` shows.
    print(f"{datetime.now(EASTERN).strftime('%Y-%m-%d %H:%M:%S %Z')}  {'; '.join(shown)}")
    return 1 if any(o.startswith("failed") for o in outcomes) else 0


def cmd_send_test(args: argparse.Namespace) -> int:
    from booth.deliver import DeliveryError, send

    try:
        ch = send("Booth test: if you can read this, report delivery works.", via=args.via)
    except DeliveryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"sent via {ch}")
    return 0


def cmd_manual_status(args: argparse.Namespace) -> int:
    from booth import manual
    from booth.data import nflverse
    from booth.data.cache import DataSourceError

    try:
        week = nflverse.current_week(nflverse.games())
    except DataSourceError:
        week = None
    text = manual.status(current_week=week)
    print(text)
    return 1 if "problem:" in text else 0


def cmd_telegram_setup(args: argparse.Namespace) -> int:
    from booth.deliver import DeliveryError, telegram_setup

    try:
        connected, text = telegram_setup(replace=args.replace)
    except DeliveryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(text)
    return 0 if connected else 2


def cmd_schedule(args: argparse.Namespace) -> int:
    from booth import schedule

    action = {"install": schedule.install, "uninstall": schedule.uninstall,
              "status": schedule.status, "wake": schedule.install_wake,
              "test-send": lambda: schedule.test_send(via=args.via)}[args.action]
    try:
        print(action())
    except (RuntimeError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


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
    p_rep = sub.add_parser("report", help="generate a report now")
    p_rep.add_argument("run", choices=["tue", "thu", "sat", "sun"])
    p_rep.add_argument("--week", type=int, help="NFL week (default: current week)")
    p_rep.add_argument("--send", action="store_true", help="also deliver it")
    p_rep.set_defaults(func=cmd_report)
    p_run = sub.add_parser("run", help="scheduled entrypoint")
    p_run.add_argument("what", choices=["due"])
    p_run.set_defaults(func=cmd_run)
    p_test = sub.add_parser("send-test", help="send a test message")
    p_test.add_argument("--via", choices=["telegram", "imessage"], help="test this channel (default: the one in use)")
    p_test.set_defaults(func=cmd_send_test)
    p_tg = sub.add_parser("telegram-setup", help="connect your Booth bot to your Telegram chat")
    p_tg.add_argument("--replace", action="store_true", help="connect a different chat than the saved one")
    p_tg.set_defaults(func=cmd_telegram_setup)
    sub.add_parser("manual-status", help="check the roster/players/matchup copies used until Yahoo works").set_defaults(
        func=cmd_manual_status)
    p_sch = sub.add_parser("schedule", help="manage the launchd job (macOS)")
    p_sch.add_argument("action", choices=["install", "uninstall", "status", "wake", "test-send"])
    p_sch.add_argument("--via", choices=["telegram", "imessage"], help="test-send: test this channel")
    p_sch.set_defaults(func=cmd_schedule)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, AuthError, YahooAccessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
