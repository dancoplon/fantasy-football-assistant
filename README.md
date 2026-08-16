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
