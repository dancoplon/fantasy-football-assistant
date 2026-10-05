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

If Yahoo can't be reached (no token yet, or API access not provisioned), `get_my_roster` falls back to `config/manual_roster.json` and says so (`"source": "manual"`). Keep that file current by hand until Yahoo access works.

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

Run the tests with `uv run pytest`.
