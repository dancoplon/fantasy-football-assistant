# Fantasy Football Assistant

A personal, read-only assistant for managing a single Yahoo Fantasy Football team.

## What it does

Reads my own team's data from the Yahoo Fantasy Sports API on a fixed weekly schedule and sends me a short report:

- **Tuesday** — waiver wire suggestions with reasoning
- **Thursday** — projected starting lineup for the week's matchup
- **Saturday** — updated lineup, injury and weather flags
- **Sunday** — final start/sit check before games lock
- **Game days** — a short alert when a recommended starter is ruled inactive (only then)

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
| `config/faab_market.json` | his league home page: each team's waiver budget (standings) and winning bids (recent transactions) | always, as a running log of what this league pays; Tuesday bids are priced from it. Budgets are flagged once a Wednesday claims run has passed since the copy. Add each week's winning bids and keep the old ones: it stays useful after Yahoo works, since Yahoo only lists recent moves |

After writing any of them, run `uv run booth manual-status`: it lists what each file holds, how old it is, whether Booth will use it, and any problems (exit code 1 if there are problems).

## External data and league context

The same MCP server also exposes free public data, none of which needs Yahoo:

- `get_schedule`: kickoff times (ET), byes, roof, Vegas spread/total and implied team points (nflverse)
- `get_player_usage`: weekly snap %, targets, target share, carries, and half-PPR points (nflverse)
- `get_injury_report`: the NFL's official Wed-Fri injury report (nflverse)
- `get_trending_players`: most-added/dropped players across Sleeper leagues
- `get_game_weather`: kickoff-window forecast with wind/rain/snow/cold flags (Open-Meteo)
- `get_league_context`: `config/league.json` (league rules), `config/strategy.md` (strategy, in plain English), and a summary of `config/faab_market.json` (what this league pays on waivers)

Downloads are cached in `state/cache/`; if a source is down, the last good copy is used. `uv run booth data-check` pulls each source once and prints a summary.

## Reports

`uv run booth report tue|thu|sat|sun [--week N]` runs Claude Code headless (`claude -p`) with the MCP server above, web search, and the prompts in `prompts/`. Claude returns the message plus a structured snapshot (lineup with projections, bench flags, waiver recs with FAAB bids, weather flags, sources). Booth then:

- saves the snapshot to `state/2026-wkNN.json` (one file per week, every run kept as a decision log; `state/latest.json` points at the newest week),
- for Saturday and Sunday, diffs against the previous run's snapshot (Thursday, then Saturday) and opens the message with "Changes since last report: ..." or "No changes. Lineup stands." Only structured fields count, so reworded reasoning never triggers a change,
- writes the final text to `reports/2026-wkNN-<run>.txt`.

Set `BOOTH_MODEL` to pick a model; the default is Claude Code's. Runs use `--effort high`; set `BOOTH_EFFORT` to `low`, `medium`, `xhigh` or `max` to change it, or to `default` to leave it to Claude Code.

## Schedule and delivery (macOS)

- `uv run booth schedule install` installs two LaunchAgents: the Telegram listener (`com.booth.listener`, see below) and `com.booth.scheduler`, which runs `booth run due` at each report time (Tue 8 AM, Thu 12 PM, Sat 8 AM, Sun 8 AM Eastern, converted to the Mac's time zone), every 30 minutes while awake, and at login. A report missed while the Mac slept or was off goes out on the next wake; a report already delivered is never sent twice, and one superseded by a later report is skipped.
- When a game is played before Thursday night (week 1's Wednesday opener, Thanksgiving eve), the Thursday check moves to noon that day.
- A lineup report stays sendable until the week's last kickoff (Monday night), so a Monday catch-up still helps with the players who haven't played. One sent after a kickoff it should have preceded starts with "LATE:", judged at the moment it's sent. If the Mac slept through the whole window, Booth says once that it missed the report.
- A run keeps the Mac from idle-sleeping until it finishes (`caffeinate`). A closed lid on battery still sleeps, and a report that finishes after its window closes isn't sent.
- Reports are built with Booth's MCP server started by the same Python (no PATH dependence). A run where the server didn't connect, or where Claude used none of its tools, counts as a failure, not a report.
- If a report can't be built, Booth sends one alert and retries every 30 minutes, up to 6 tries; if it gives up, it says so. A report that was built but couldn't be sent keeps retrying the send. When Messages itself is failing, the alert falls back to a notification on the Mac.
- Delivery goes through `booth/deliver.py`, a single `send()` function. Telegram is preferred: create a bot with @BotFather and put its token in `.env` as `TELEGRAM_BOT_TOKEN`. Then run `uv run booth telegram-setup`: it prints a link with a one-time code; open it on the phone and tap Start, then run setup again. Only the chat that sent that code is connected (anyone can find a bot and message it); the chat is saved to `state/telegram.json` and gets a hello. Running setup again keeps the saved chat unless you pass `--replace`. Messages then come from the bot, so they don't show up twice the way a text to yourself does.
- With a token set, Telegram is the channel. If a send fails (or setup hasn't been finished), Booth sends it by iMessage instead, prefixed with why, and `booth schedule status` shows "delivered ... by imessage (Telegram failed: ...)". Without an iMessage backup the send fails and Booth alerts by Mac notification and retries; it never goes quietly unsent.
- iMessage goes to `IMESSAGE_RECIPIENT` in `.env`. `uv run booth schedule test-send` sends a test from the same background context the schedule uses, by the channel in use; add `--via imessage` to check the iMessage backup (that's where macOS asks for the "control Messages" permission) or `--via telegram` to check Telegram alone.
- Inactives alert (`booth/inactives.py`): teams name inactive players 90 minutes before kickoff. When a run falls 80 to 5 minutes before a kickoff, Booth looks at that game's starters in the week's latest lineup report. If any carries an injury designation (in that report or the official injury report), one Claude run checks the inactives and picks a bench swap. Dan gets a message only if a starter is out, or if the last check before kickoff still couldn't confirm one; an alert goes out at once, and any starter whose list wasn't out yet is checked again (with a follow-up if he's active). Overlapping windows (4:05 and 4:25 PM) are each checked. Up to 2 checks per kickoff; nothing is sent after kickoff; healthy starters aren't checked; with the Mac asleep there's no check.
- `uv run booth report <run> --send` sends a report by hand; if it's the one currently due, the schedule won't send it again.
- `BOOTH_DRY_RUN=1` prefixes every scheduled message with `[DRY RUN]`.
- `uv run booth schedule wake` (optional) asks for an admin password once and sets a wake one minute after the 8 AM reports on Tue/Sat/Sun. It only helps with the lid open.
- Scheduled runs need Claude Code's standalone CLI signed in (`~/.local/bin/claude`).
- `uv run booth schedule status` shows the job and recent log lines; logs are in `logs/`.

## Chat (two-way Telegram)

Dan can write to the Booth bot and get answers while the Mac is awake. `booth listen` (the `com.booth.listener` LaunchAgent, kept running by launchd) collects his messages and answers each batch with `claude -p` on his Claude subscription: the same MCP tools, web search and data as the reports, with `prompts/chat.md`, his notes, the last few reports and the recent conversation. Answers use `--effort medium` (set `BOOTH_CHAT_EFFORT` to change it). Besides answering, Claude can:

- keep notes Dan wants remembered (`state/notes.json`); reports read them through `get_league_context` (`dan_notes`),
- update his roster copy (`config/manual_roster.local.json`) when he says he made a move in Yahoo: add, drop, move to a slot/bench/IR, or FAAB left. Each change is listed under `chat_updates` in that file,
- rerun a report (`booth rerun <run>` in the background; it waits for any run in progress, and marks the report delivered if it's the one due).

Only Dan's chat (the one saved by `booth telegram-setup`) is answered. Logs are in `logs/chat.log`.

Messages reach the Mac one of two ways:

- **The mailbox** (`worker/`, a Cloudflare Worker with a SQLite Durable Object, free plan, no AI and no API key). Telegram's webhook delivers each message there; the Mac checks in every 10 seconds while awake and collects them. When the Mac hasn't checked in for 2 minutes, the mailbox replies "Booth is asleep..." (once per sleep, again after 3 hours), and Booth answers everything when the Mac wakes. Strangers get their chat id once, then silence. The Mac finds the mailbox from the bot's webhook (or `BOOTH_MAILBOX_URL`); requests are signed with a secret derived from the bot token.
- **Telegram's getUpdates**, when no webhook is set: the same answers, but no "asleep" reply, and Telegram drops messages after about a day.

The mailbox deploys from GitHub Actions (`.github/workflows/mailbox.yml`) on each change to `worker/`, or by hand from the Actions tab. It needs three repository secrets: `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` and `TELEGRAM_BOT_TOKEN` (Booth's bot). Without them it only runs the checks. After deploying, it points the bot's webhook at `https://booth-mailbox.<subdomain>.workers.dev/telegram`. Worker tests: `cd worker && npm ci && npm run check && npm test`.

Run the tests with `uv run pytest`.
