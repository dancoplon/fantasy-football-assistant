## Tuesday waiver report (for week {week})
Goal: tell Dan which waiver claims to submit before claims process, with FAAB bids.

1. Find the holes in Dan's week {week} lineup: starters on bye (get_schedule byes), players Out or on IR (get_injury_report, statuses), and weak spots.
2. Find candidates: get_free_agents for each position of need, and get_trending_players (add, last 24-48 hours) to see who is rising. In this 12-team dynasty league most trending players are already rostered: skip a trending player who isn't in get_free_agents results, unless get_free_agents failed or doesn't cover his position. Check each candidate's usage (get_player_usage) and week {week} matchup.
3. If Dan has no healthy, non-bye QB for week {week}, the top recommendation MUST be a QB streamer.
   Look one week ahead too (get_schedule for next week): if a starting slot will have no healthy, non-bye player next week, say so. If that fill-in should be claimed now (good options are scarce, or other teams share the bye and will want him), make it claim #1.
4. For each add, name the drop (or an IR move that frees a spot; roster is full at 25). Pick drops by least long-term dynasty value AND least 2026 help. Prefer IR moves over drops when a player qualifies. Every claim needs its own spot: never name the same drop or IR move for two claims, since Yahoo rejects a claim whose drop is already gone.
5. Bids: a whole-dollar FAAB bid per claim from Dan's remaining budget (league.json), following the reserve guidance in the strategy. A claim that fills an otherwise empty starting slot (this week or next) gets a bid high enough to beat other teams, not a token $1-2.
6. Rank up to 5 claims by how much each matters, slot-fillers first. Also give the projected starting lineup for week {week} as it would look after the top claim.
