# Pool pipeline

This independent, offline pipeline turns a saved DraftSharks projections page and a
GridironAI projections export into the league's `pool.json`. It is rerun when
projections change, not during every live-draft refresh.

## Stages

```text
data/projections.html
  -> parse_projections.py      -> data/projections.json
  -> build_pool.py             -> ../pool.json
  -> match_sleeper.py          -> ../pool.json with sleeper_id
  -> apply_gridiron_points.py  -> ../pool.json re-priced from data/gridironai-rankings-*.csv
```

`pipeline.py` runs those stages in order and stops on the first failure. Every stage is
also a standalone CLI:

```bash
uv run pool_pipeline/pipeline.py --report
uv run pool_pipeline/pipeline.py --only pool
uv run pool_pipeline/parse_projections.py in.html -o out.json
uv run pool_pipeline/build_pool.py --limit 450 -o big.json
uv run pool_pipeline/match_sleeper.py --report
uv run pool_pipeline/apply_gridiron_points.py --report
uv run pool_pipeline/fetch_sleeper.py
uv run pool_pipeline/fetch_sleeper_projections.py
```

`fetch_sleeper.py` is manual and is not a pipeline stage. Sleeper's player dump is about
14 MB and should not be downloaded more than once per day. It is cached under `data/`;
the small metadata file records when it was fetched.

`fetch_sleeper_projections.py` is also manual: it writes `data/sleeper_projections.json`,
the top 250 players by Sleeper/Rotowire season projection with half-PPR ADP. It no
longer prices the pool; the file is kept because the data-source investigator reads it
as the Sleeper ADP opponent board.

The GridironAI export (stage 4's input) is hand-saved from the site's rankings page
**with this league's own scoring selected** and dropped into `data/` as
`gridironai-rankings-<season>-<n>.csv`; the newest one wins.

## File contracts

`projections.json` is a faithful provider export: identity fields, eight scoring
schemes, four horizons, displayed and derived ranks, ADP, and analysis text. The printed
ranks in the saved HTML are stale; consumers use the gap-free ranks derived from 3D value.
The saved page is the provider's dynasty export; this league consumes only its one-season
projection, which is an ordinary season projection.

`pool.json` is the narrow draft input: every QB/RB/WR/TE player both sources know
(DraftSharks' pool intersected with GridironAI's depth-chart projections), with 13
fields per player. DraftSharks supplies identity and ADP; GridironAI supplies the value
columns. The ranker uses projected points, not DraftSharks' provider-scaled 3D value.

- `points`: GridironAI's expected (median) one-season points in this league's scoring,
  joined by name by stage 4. Stage 2 first fills the column from the provider's
  `half_ppr`, but stage 4 replaces it. A player GridironAI does not list (mostly
  recently signed free agents its depth charts lag on) falls back to his Sleeper
  projection from `data/sleeper_projections.json`, divided by the per-position median
  Sleeper/GridironAI ratio of the players both project — Sleeper's medians run ~1.27x
  hotter, and a raw copy would overprice every fallback row. A player in neither
  source is dropped
- `points_low` / `points_high`: GridironAI's downside and upside projections — the
  calibrated 10th/90th-percentile season outcomes; null on Sleeper-fallback rows,
  which the ranker prices from the median alone. The ranker collapses the
  quantiles into a truncated expected value above the waiver baseline
- `adp`: overall 1QB ADP decoded from the provider's 12-team round.pick notation
- `sleeper_id`: the join key used by the live draft and the investigator
- `rank`: descending `points`; ties keep the previous pool order

## GridironAI matching

There is no shared provider id, so stage 4 reuses the Sleeper matcher's conservative
name tiers: full normalized name; name without suffix; then last name plus team.
Position must always agree, ambiguity is left unmatched, and one GridironAI row
matching two pool players is fatal. Re-running is idempotent because the quantile
columns are dropped and re-derived.

## Sleeper matching

There is no shared provider id, so `match_sleeper.py` uses three conservative name
tiers: full normalized name; name without suffix; then last name plus team and nearby
age. Position must always agree, ambiguity is left unmatched, and duplicate Sleeper ids
are fatal. Re-running is idempotent because ids are dropped and re-derived.
