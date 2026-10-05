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

Run the tests with `uv run pytest`.
