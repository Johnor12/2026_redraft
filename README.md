# 2026 redraft

A toolkit for a 10-team 0.5 PPR redraft league. Independent
processes publish stable JSON artifacts at the repository root; the ranker consumes
those artifacts and the static dashboard renders the result. In season, `weekly.py`
refreshes the league state and optimizes my lineup and waiver claims.

## League assumptions

- 0.5 PPR, no tight end premium
- Starters: 1 QB, 2 RB, 2 WR, 1 TE, 2 W/R/T flex, 1 D/ST
- 4 bench and 1 IR (the IR spot is not drafted into)
- Position caps: 4 QB, 8 RB, 8 WR, 3 TE
- 10 teams and 13 drafted players per team (130 picks)
- Plain snake draft, no third-round reversal
- My slot is assumed to be 2 (1.02, 2.09, 3.02, 4.09, …, 12.09, 13.02) until the
  real draft order is published; `draft.json` overrides it with a complaint
- The D/ST slot is drafted but not modeled: the pool and rankings are offense-only,
  so a made D/ST pick is carried as an off-pool roster spot
- Traditional rolling waivers with no FAAB budget: pickups are ordered add/drop claims
  processed by waiver priority, and a won claim sends that team to the back

These are project assumptions, not runtime configuration. Ranker constants live in
`ranker/league.py`.

## Setup

[uv](https://docs.astral.sh/uv/) pins Python 3.12.

```bash
uv sync
uv run <script>
```

Commands work from the repository root. Pipeline defaults are anchored to their own
directories, so their documented direct invocations also work from inside the pipeline.

## Data flow

```text
pool_pipeline/ ───────────────> pool.json ──────────┐
                                                   │
draft_pipeline/ ──────────────> draft.json ─────────┼─> rank.py ────> rankings.json
                                                   │
data_source_investigator/ ────> data_source_matches.json
                         └────> data/rankings.json ─┘

In season:

league_pipeline/ ──────────────> league.json ───────┐
GridironAI weekly + rest-of-season CSVs ────────────┴─> weekly.py ──> lineup + claims
```

The published files have distinct owners:

- `pool.json`: ~380 QB/RB/WR/TE players keyed to Sleeper, priced by GridironAI's
  league-scored projection distribution — median `points` plus `points_low`/
  `points_high`, the 10th/90th-percentile season outcomes (identity and ADP from
  DraftSharks). Players GridironAI doesn't list carry a scale-calibrated Sleeper
  median with null quantiles
- `draft.json`: all 130 made and pending picks from the ESPN league API, with made
  picks overlaid from a hand-pasted `draft_history.txt` during the live draft
  (ESPN's read API lags the draft room by minutes or more)
- `data_source_matches.json`: the provider board closest to each opponent's picks
- `rankings.json`: undrafted-player rankings, recommendations, simulations, and validation
- `league.json`: the in-season ESPN state — my roster and lineup slots, every available
  player, waiver order, and ESPN's own weekly and rest-of-season projections

`sleeper_id` is the cross-process player key. `roster_id` and `draft_slot` connect
opponent source matches to the live board. In season, `weekly.py` keys players by ESPN
id and joins GridironAI to them by name.

## Components

- [Pool pipeline](pool_pipeline/README.md): provider HTML to the league-specific pool
- [Draft pipeline](draft_pipeline/README.md): ESPN league API to the complete live board
- [Data-source investigator](data_source_investigator/README.md): normalize provider
  boards and infer opponent strategies
- [Ranker](ranker/README.md): wire-level solver, opponent simulation, planning,
  and output contracts
- [League pipeline](league_pipeline/README.md): ESPN league API to the in-season
  `league.json`
- `weekly.py`: refreshes `league.json`, then prints this week's optimal lineup and the
  ranked waiver claims
- `index.html`: dependency-free dashboard for `rankings.json`
- `data_source_investigator/index.html`: source-fit and pick-evidence dashboard
- `serve.py`: serves both dashboards at http://127.0.0.1:8123

Each component keeps its own paths, entry points, and implementation context. Offline
checks remain beside the draft, investigator, and ranker code they exercise. The pipelines
meet through their published JSON contracts rather than shared orchestration.

The ranker values a roster as expected optimal lineup points from one-season
projections. Before any board is built it measures the projected post-draft wire on
raw median projections, then reprices every player at the wire plus his truncated
expected value above it (Swanson's 0.3/0.4/0.3 over the three quantiles) — a season
below the wire is worth the wire, so early picks price near their median and late
picks by their ceiling. Position-wide availability determines when depth is called
on, and one unique final waiver body per position supplies the fallback. Personal and
opponent strategies are intentionally separate: my slot uses the projection-based roster
objective, while each opponent follows its inferred external board with roster-balance
adjustments and fitted choice noise. Opponent picks never use my projections or board.

## Common workflows

Rebuild the projection pool after saving updated provider HTML:

```bash
uv run pool_pipeline/pipeline.py --report
```

Re-price the pool after saving a fresh GridironAI export (league scoring selected)
into `pool_pipeline/data/`:

```bash
uv run pool_pipeline/pipeline.py --report
```

Refresh ranking snapshots and opponent associations:

```bash
uv run data_source_investigator/pipeline.py --report
```

Refresh the live board and recommendations between picks — first use Ctrl+A, Ctrl+C
in the draft room and paste the full page into `draft_history.txt` (a pick-history-only
copy also works; ESPN's API alone won't have the new picks yet), then:

```bash
uv run refresh.py --report
```

`refresh.py` deliberately does only three live steps: fetch the draft, re-run source
inference against the existing provider snapshot, then rank. It does not rebuild the
offline pool or fetch provider boards.

Run offline checks:

```bash
uv run rank.py --selftest
uv run draft_pipeline/fetch_draft.py --selftest
uv run data_source_investigator/investigate.py --selftest
uv run evaluate_opponents.py
```

Before and after changing an opponent model or pick policy, run
`uv run evaluate_opponents.py` and compare its replay accuracy.

## In season

Each week, export two GridironAI rankings CSVs with the league's scoring selected and
save them at the repo root under the site's names for ESPN's current week:
`gridironai-rankings-<season>-<week>.csv` (this week, D/ST included) and
`gridironai-rankings-<season>-rest-of-season-from-week-<week>.csv`. Then:

```bash
uv run weekly.py
```

It fetches the league (`league_pipeline/fetch_league.py`), then prints the lineup and the
claim list. It submits nothing to ESPN. Run it before waivers process for the claims, and
again before kickoff once claims have cleared and projections have moved.

- Lineup: maximizes this week's expected points, Swanson's 0.3/0.4/0.3 over
  GridironAI's weekly quantiles. Players whose game has started keep their slot, and IR
  stays IR
- Claims: each add is valued at its rest-of-season change in expected optimal lineup
  points. This is the ranker's roster objective (`ranker/value.py`), repricing every
  player at the wire plus his truncated expected value above it. The wire and the
  fallback body are the best available players other than the add. Each add takes its
  best drop and must clear a +3 rest-of-season gain (`MIN_CLAIM_GAIN`). The list comes
  in rounds: a round's claims share a drop, so at most one wins, and each round assumes
  the round before won its first claim
- D/ST: weekly start/sit uses GridironAI's weekly D/ST projection. Rest-of-season D/ST
  value is ESPN's projection, since GridironAI publishes none. Available players
  GridironAI does not list are not considered, and the biggest are named in a warning

## Dashboard and automation

Run `uv run serve.py` and open the local URL; direct `file://` access cannot fetch the
JSON files. The main dashboard's live-status strip still tries to poll Sleeper as a
cross-check; against the ESPN league that poll fails harmlessly (ESPN's API needs auth
cookies and blocks browser CORS), so the strip runs on the `draft.json` snapshot, shows
when it is stale, and pins the status of any active refresh or deploy workflow run.

`.github/workflows/refresh.yml` runs the live refresh on manual dispatch and commits the
generated board, rankings, and source matches. `.github/workflows/deploy-pages.yml`
publishes both dashboards plus `rankings.json` and `data_source_matches.json` to GitHub
Pages, with the investigator at `data_source_investigator/`. They share one concurrency
group so refresh and deploy do not overlap.
