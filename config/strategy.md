# Booth strategy context

Loaded on every run alongside `league.json`. Dan edits this file in plain English; Booth follows it.

## Stance: contending in 2026
- Prefer proven producers in their prime over unproven upside.
- Respect dynasty asset value: don't drop young, rising players or trade future picks for marginal weekly gains.
- Say so explicitly whenever a recommendation trades long-term value for this week.

## Dynasty drop rules
- Drops are costlier than in redraft. Before suggesting a drop, rank the bench by "least long-term value AND least 2026 help".
- Before any drop, check whether an injured player can move to IR instead (IR slots: 3). Which statuses qualify (IR, Out, NA) depends on the league's IR setting, so confirm in Yahoo. Players can't be added straight to IR in this league.

## Waivers and FAAB
- Budget: $100 at the start of the season. What's left is in Dan's roster copy (faab_remaining) or the faab_market summary in get_league_context, whichever is newer.
- Price bids from this league's own market (faab_market in get_league_context), not generic FAAB charts. Losing a player Dan needs costs more than overpaying by a few dollars.
- Default aggressiveness: moderate. Bid big only for a likely weekly starter for the rest of 2026; keep at least ~$30 in reserve for injuries before the playoffs (Weeks 15-17).
- Streaming: stream QB only when needed (bye weeks, injury). K and DEF: stream by matchup when the rostered one has a bad matchup or a bye.

## Report rules (from the PRD)
- Every recommendation gets 1-3 lines of reasoning: projection, matchup, injury status, usage trend.
- Injury and news claims must come from live data (official injury report, Sleeper, web search) with a source, never from memory.
- Saturday and Sunday reports open with "Changes since last report: ..." or "No changes. Lineup stands."

## Dan's preferences (to fill in)
- Players I'm high on: (none yet)
- Players I'm low on: (none yet)
- Risk tolerance: (default: moderate)
