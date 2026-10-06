# Fantasy Football Assistant

A personal, read-only assistant for managing a single Yahoo Fantasy Football team.

## What it does

Reads my own team's data from the Yahoo Fantasy Sports API on a fixed weekly schedule and sends me a short report:

- **Tuesday** — waiver wire suggestions with reasoning
- **Thursday** — projected starting lineup for the week's matchup
- **Saturday** — updated lineup, injury and weather flags
- **Sunday** — final start/sit check before games lock

Reports combine Yahoo data with public sources (injury news, weather, player usage trends). Every suggestion includes a one- or two-line explanation.

## What it does not do

It does not make roster moves. There is no write path — no add/drop, no lineup changes, no trades, no messages to other managers. I read the report and make any changes myself in the Yahoo app.

## Yahoo data accessed

Read-only requests for my own league and team: league settings, my roster, free agents, my weekly matchup, league transactions, and standings.

## Scope

Personal, non-commercial, single user. Data is used only to generate reports for my own team and is not published, shared, resold, or made available to anyone else. OAuth tokens are stored locally and never committed.

## Built with

Python, the Yahoo Fantasy Sports API (OAuth 2.0), and a local scheduler on macOS.

## Setup (macOS)

Requires [uv](https://docs.astral.sh/uv/) (`brew install uv`).

```sh
uv sync
cp .env.example .env      # then fill in YAHOO_CLIENT_ID, YAHOO_CLIENT_SECRET
uv run booth auth         # one time: approve in the browser, paste the redirect URL back
uv run booth check        # refreshes the token and pulls league settings
```

During `booth auth`, Yahoo redirects to `https://localhost:8000/?code=...`. Nothing runs there, so the browser shows a connection error. That's expected: copy the whole URL from the address bar into the terminal.

Tokens are saved to `secrets/yahoo_token.json` (owner-only permissions, gitignored). Every run refreshes the access token first. If `booth check` returns a 401/403 after `booth auth` succeeded, Yahoo hasn't provisioned Fantasy API access for the app yet.

## Yahoo MCP server

`uv run booth-mcp` starts a stdio MCP server with six read-only tools: `get_league_settings`, `get_my_roster`, `get_free_agents`, `get_matchup`, `get_transactions`, `get_standings`. Claude Code loads it from `.mcp.json` when run in this repo (try `claude` then "what's my roster?").

If Yahoo can't be reached (no token yet, or API access not provisioned), `get_my_roster` falls back to Dan's roster copy (`config/manual_roster.local.json`, gitignored; see the table below) or, without one, the seeded `config/manual_roster.json`, and says so (`"source": "manual"`). Don't edit the seeded file: it's tracked in this public repo. Likewise `get_free_agents` falls back to `config/manual_free_agents.json`, a saved copy of Yahoo's available-players page, for 7 days after its `as_of` time. That file holds league data, so it stays on the Mac (gitignored); `config/manual_free_agents.example.json` shows its shape.

### Until Yahoo access works

Booth works from copies of Dan's Yahoo pages. He sends screenshots (or saved PDFs) in the project thread, and a Claude session on the Mac turns them into JSON. These files hold league data, so they are gitignored; `config/*.example.json` show the shape.

| File | From | Used |
| --- | --- | --- |
| `config/manual_roster.local.json` | his team page (optional `slot`: where he has each player, `BN` = bench) | instead of the seeded `config/manual_roster.json`; flagged as possibly stale after 7 days |
| `config/manual_free_agents.json` | the available-players page | for 7 days after `as_of` |
| `config/manual_matchup.json` | his matchup page | only for its `week`, and not after 8 days |

After writing any of them, run `uv run booth manual-status`: it lists what each file holds, how old it is, whether Booth will use it, and any problems (exit code 1 if there are problems).

## External data and league context

The same MCP server also exposes free public data, none of which needs Yahoo:

- `get_schedule`: kickoff times (ET), byes, roof, Vegas spread/total and implied team points (nflverse)
- `get_player_usage`: weekly snap %, targets, target share, carries, and half-PPR points (nflverse)
- `get_injury_report`: the NFL's official Wed-Fri injury report (nflverse)
- `get_trending_players`: most-added/dropped players across Sleeper leagues
- `get_game_weather`: kickoff-window forecast with wind/rain/snow/cold flags (Open-Meteo)
- `get_league_context`: `config/league.json` (league rules) and `config/strategy.md` (strategy, in plain English)

Downloads are cached in `state/cache/`; if a source is down, the last good copy is used. `uv run booth data-check` pulls each source once and prints a summary.

## Reports

`uv run booth report tue|thu|sat|sun [--week N]` runs Claude Code headless (`claude -p`) with the MCP server above, web search, and the prompts in `prompts/`. Claude returns the message plus a structured snapshot (lineup with projections, bench flags, waiver recs with FAAB bids, weather flags, sources). Booth then:

- saves the snapshot to `state/2026-wkNN.json` (one file per week, every run kept as a decision log; `state/latest.json` points at the newest week),
- for Saturday and Sunday, diffs against the previous run's snapshot (Thursday, then Saturday) and opens the message with "Changes since last report: ..." or "No changes. Lineup stands." Only structured fields count, so reworded reasoning never triggers a change,
- writes the final text to `reports/2026-wkNN-<run>.txt`.

Set `BOOTH_MODEL` to pick a model; the default is Claude Code's.

## Schedule and delivery (macOS)

- `uv run booth schedule install` installs one LaunchAgent (`com.booth.scheduler`) that runs `booth run due` at each report time (Tue 8 AM, Thu 12 PM, Sat 8 AM, Sun 8 AM Eastern, converted to the Mac's time zone), every 30 minutes while awake, and at login. A report missed while the Mac slept or was off goes out on the next wake; a report already delivered is never sent twice, and one superseded by a later report is skipped.
- When a game is played before Thursday night (week 1's Wednesday opener, Thanksgiving eve), the Thursday check moves to noon that day.
- A lineup report stays sendable until the week's last kickoff (Monday night), so a Monday catch-up still helps with the players who haven't played. One sent after a kickoff it should have preceded starts with "LATE:", judged at the moment it's sent. If the Mac slept through the whole window, Booth says once that it missed the report.
- A run keeps the Mac from idle-sleeping until it finishes (`caffeinate`). A closed lid on battery still sleeps, and a report that finishes after its window closes isn't sent.
- Reports are built with Booth's MCP server started by the same Python (no PATH dependence). A run where the server didn't connect, or where Claude used none of its tools, counts as a failure, not a report.
- If a report can't be built, Booth sends one alert and retries every 30 minutes, up to 6 tries; if it gives up, it says so. A report that was built but couldn't be sent keeps retrying the send. When Messages itself is failing, the alert falls back to a notification on the Mac.
- Delivery is iMessage to `IMESSAGE_RECIPIENT` in `.env` via `booth/deliver.py`, a single `send()` function so the channel can be swapped later. `uv run booth schedule test-send` checks it from the same background context the schedule uses (that's where macOS asks for the "control Messages" permission).
- `uv run booth report <run> --send` sends a report by hand; if it's the one currently due, the schedule won't send it again.
- `BOOTH_DRY_RUN=1` prefixes every scheduled message with `[DRY RUN]`.
- `uv run booth schedule wake` (optional) asks for an admin password once and sets a wake one minute after the 8 AM reports on Tue/Sat/Sun. It only helps with the lid open.
- Scheduled runs need Claude Code's standalone CLI signed in (`~/.local/bin/claude`).
- `uv run booth schedule status` shows the job and recent log lines; logs are in `logs/`.

Run the tests with `uv run pytest`.
