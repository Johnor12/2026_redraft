# League pipeline

`fetch_league.py` is the network-only, in-season entry point that publishes `league.json`
on demand. Like the draft pipeline it caches nothing: every run asks ESPN again.

```bash
uv run league_pipeline/fetch_league.py
```

`weekly.py` at the repo root runs it as its first step, so it is rarely run by hand.

## Inputs

Two requests to ESPN's undocumented v3 league API:

- the league endpoint with the `mSettings`, `mTeam`, `mRoster`, and `mStatus` views:
  lineup slot counts, position caps, waiver settings and order, the current scoring
  period, and my roster with its lineup slots
- the `kona_player_info` view with an `x-fantasy-filter` header: every free agent and
  waiver player at QB/RB/WR/TE/D/ST (~850 players). ESPN answers 400 to a filter
  without a sort, and a response that fills the page limit is treated as truncated

The league id, season, and SWID/espn_s2 cookies are imported from
`draft_pipeline/fetch_draft.py`, so there is one copy to refresh when ESPN answers 401/403.

## `league.json`

- `scoring_period`: ESPN's current week; `weekly.py` picks the GridironAI exports by it
- `me`: my ESPN team id and name
- `waivers`: acquisition type, whether a budget is used, and my waiver rank of the team
  count. The league runs traditional rolling waivers with no budget
- `lineup_slots`: nonzero ESPN slot counts by name (`QB`, `RB`, `WR`, `TE`, `FLEX`,
  `D/ST`, `BE`, `IR`); an unmodeled slot type is fatal
- `position_limits`: ESPN's roster caps per position, null when unlimited
- `players`: my roster (`status` `MINE`) followed by every available player (`FREEAGENT`
  or `WAIVERS`). Each row carries `espn_id`, name, position, NFL team, ESPN
  `injury_status`, `lineup_slot` (mine only), `locked` (game started),
  `droppable` (mine only), and `waivers_clear_at` (UTC, waiver players only)

Every row also keeps ESPN's own league-scored projections: `espn_week` for the current
scoring period and `espn_ros`, ESPN's in-season season split. That split is a
rest-of-season projection through week 18: a healthy player at week 3 carries 15 games
(weeks 3–18 minus his bye). `weekly.py` prices from GridironAI and reads `espn_ros` only
for D/ST, which GridironAI does not project past the current week.
