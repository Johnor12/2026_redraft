#!/usr/bin/env python3
"""Re-price pool.json from GridironAI's projection export. Stage 4 of the build.

``build_pool.py`` leaves the pool priced with DraftSharks points. This stage swaps
the value column and widens it to a distribution: each player's ``points`` becomes
GridironAI's expected (median) season projection, and ``points_low`` / ``points_high``
carry their downside and upside projections — calibrated 10th/90th-percentile season
outcomes per the provider. The ranker collapses the three into a truncated expected
value above the waiver baseline; this stage only delivers them. Identity fields and
the DraftSharks ADP are untouched.

The CSV is a hand-saved export from the site's rankings page and **must be exported
with this league's own scoring selected** — the numbers here differ from GridironAI's
full-PPR article defaults, which is what a league-scored export looks like. The newest
``data/gridironai-rankings-*.csv`` wins when several are present.

**The join is by name, because there is no shared key.** Same tiered scheme as
``match_sleeper.py``, minus the age tier (the export carries no age):

    1. full name            "Josh Allen"        -> josh allen, QB
    2. name without suffix  "Chris Godwin"      -> chris godwin (jr.), TE-level suffix noise
    3. last name + team     "Cameron Skattebo"  -> Cam Skattebo, NYG

Position must agree in every tier; ambiguity is left unmatched rather than guessed;
one GridironAI row matching two pool players is fatal.

A pool player with no GridironAI projection falls back to his Sleeper/Rotowire
projection from ``data/sleeper_projections.json``, joined on ``sleeper_id`` — mostly
recently signed free agents GridironAI's depth charts lag on. Sleeper's medians run
hotter than GridironAI's (~1.27x at RB/WR/TE on the players both project), so a raw
copy would overprice every fallback row; the Sleeper number is divided by the
per-position median ratio measured from this run's own joins. Fallback rows carry
``points_low``/``points_high`` as null — Sleeper publishes no quantiles — and the
ranker prices them from the median alone. A player in neither source is dropped.
GridironAI players the pool cannot represent are printed when they would beat a kept
player at their position: a missing high scorer would silently distort the board.

Ranks are re-derived from the new points; ties keep the previous pool order.
Re-running is idempotent: quantile columns are dropped and re-derived, and the
dropped count accumulates instead of resetting.

Usage:
    uv run pool_pipeline/apply_gridiron_points.py [pool.json] [--gridiron FILE] [--report]
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import sys
import unicodedata
from pathlib import Path

import paths
from match_sleeper import sleeper_team, strip_suffix
from match_sleeper import last_name as ascii_last_name
from match_sleeper import norm as ascii_norm


def fold(value: str | None) -> str:
    """Accents to ASCII: Estimé -> Estime. match_sleeper's normalization deletes
    non-alphanumerics outright, which silently deletes accented letters; GridironAI
    prints them where DraftSharks does not, so every name goes through here first."""
    return unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()


def norm(value: str | None) -> str:
    return ascii_norm(fold(value))


def last_name(name: str) -> str:
    return ascii_last_name(fold(name))

POINTS_FIELD = "points"
LOW_FIELD = "points_low"
HIGH_FIELD = "points_high"

POSITIONS = ("QB", "RB", "WR", "TE")

#: GridironAI team code -> Sleeper team code (the pool side goes through
#: match_sleeper.sleeper_team), so tier 3 compares teams in one code space.
GRIDIRON_TEAM_ALIASES = {"LA": "LAR", "LVR": "LV"}

#: CSV headers carry a parenthetical description; columns are found by the bare name.
COLUMNS = {
    "name": "Name",
    "team": "Team",
    "position": "Pos.",
    POINTS_FIELD: "Expected Points",
    LOW_FIELD: "Downside Points",
    HIGH_FIELD: "Upside Points",
}

TIERS = ("name", "name_without_suffix", "last_name_team")

FIELD_DESCRIPTIONS = {
    POINTS_FIELD: (
        "GridironAI's expected (median) one-season fantasy points under this league's "
        "own scoring, joined by name by apply_gridiron_points.py. Players GridironAI "
        "does not list carry their Sleeper/Rotowire projection instead, scaled down by "
        "the per-position median Sleeper/GridironAI ratio of the players both project."
    ),
    LOW_FIELD: (
        "GridironAI's downside one-season points: the calibrated 10th-percentile "
        "season outcome, same scoring as points. Null when only a Sleeper median exists."
    ),
    HIGH_FIELD: (
        "GridironAI's upside one-season points: the calibrated 90th-percentile "
        "season outcome, same scoring as points. Null when only a Sleeper median exists."
    ),
}

#: A joined pair prices the Sleeper->GridironAI calibration only when both medians
#: clear this, keeping deep-tail ratio noise out of the scale estimate.
CALIBRATION_FLOOR = 50.0


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def load_gridiron(path: Path) -> list[dict]:
    """The export's QB/RB/WR/TE rows as small dicts, quantiles as floats."""
    with path.open(encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        index: dict[str, int] = {}
        bare = [column.split(" (")[0].strip() for column in header]
        for field, wanted in COLUMNS.items():
            if wanted not in bare:
                raise ValueError(f"{path.name} has no '{wanted}' column")
            index[field] = bare.index(wanted)
        rows = []
        for raw in reader:
            if raw[index["position"]] not in POSITIONS:
                continue
            row: dict = {
                "name": raw[index["name"]],
                "team": raw[index["team"]],
                "position": raw[index["position"]],
                POINTS_FIELD: float(raw[index[POINTS_FIELD]]),
                LOW_FIELD: float(raw[index[LOW_FIELD]]),
                HIGH_FIELD: float(raw[index[HIGH_FIELD]]),
            }
            if not row[LOW_FIELD] <= row[POINTS_FIELD] <= row[HIGH_FIELD]:
                raise ValueError(
                    f"{path.name}: {row['name']} has disordered quantiles "
                    f"{row[LOW_FIELD]}/{row[POINTS_FIELD]}/{row[HIGH_FIELD]}"
                )
            row["team"] = GRIDIRON_TEAM_ALIASES.get(row["team"], row["team"])
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


class GridironIndex:
    """The export's rows, keyed the three ways the tiers look them up."""

    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.by_name: dict[str, list[dict]] = collections.defaultdict(list)
        self.by_base: dict[str, list[dict]] = collections.defaultdict(list)
        self.by_last: dict[str, list[dict]] = collections.defaultdict(list)
        for row in rows:
            name = norm(row["name"])
            self.by_name[name].append(row)
            self.by_base[strip_suffix(name)].append(row)
            self.by_last[last_name(row["name"])].append(row)


def narrow(row: dict, candidates: list[dict]) -> list[dict]:
    """Position must agree, always; team breaks ties rather than filtering."""
    survivors = [c for c in candidates if c["position"] == row["position"]]
    if len(survivors) <= 1:
        return survivors
    team = sleeper_team(row.get("team"))
    if team:
        on_team = [c for c in survivors if c["team"] == team]
        if on_team:
            survivors = on_team
    return survivors


def match(row: dict, index: GridironIndex) -> tuple[dict | None, str, list[dict]]:
    """Return (gridiron row or None, tier, the candidates that caused an ambiguity)."""
    name = norm(row["name"])
    for tier, candidates in (
        ("name", index.by_name.get(name, [])),
        ("name_without_suffix", index.by_base.get(strip_suffix(name), [])),
    ):
        survivors = narrow(row, candidates)
        if len(survivors) == 1:
            return survivors[0], tier, []
        if survivors:
            return None, tier, survivors

    # Tier 3: the first name is unusable, so the team carries the whole join.
    team = sleeper_team(row.get("team"))
    if team:
        candidates = [
            c for c in index.by_last.get(last_name(row["name"]), []) if c["team"] == team
        ]
        survivors = narrow(row, candidates)
        if len(survivors) == 1:
            return survivors[0], "last_name_team", []
        if survivors:
            return None, "last_name_team", survivors

    return None, "none", []


# ---------------------------------------------------------------------------
# Repricing
# ---------------------------------------------------------------------------


def sleeper_scale(
    joins: list[tuple[dict, dict]], sleeper: dict[str, float]
) -> dict[str, float]:
    """Per-position median Sleeper/GridironAI points ratio over this run's own joins."""
    ratios: dict[str, list[float]] = {pos: [] for pos in POSITIONS}
    for row, source in joins:
        sleeper_id = row.get("sleeper_id")
        theirs = sleeper.get(sleeper_id) if sleeper_id else None
        if (
            theirs
            and theirs >= CALIBRATION_FLOOR
            and source[POINTS_FIELD] >= CALIBRATION_FLOOR
        ):
            ratios[row["position"]].append(theirs / source[POINTS_FIELD])
    out = {}
    for pos, values in ratios.items():
        if not values:
            raise ValueError(f"no {pos} pairs above {CALIBRATION_FLOOR} pts to calibrate with")
        values.sort()
        out[pos] = values[len(values) // 2]
    return out


def with_quantiles(row: dict, points: float, low: float | None, high: float | None) -> dict:
    """The row with its value columns replaced, quantiles beside points.

    Quantiles are dropped and re-derived, so re-running is idempotent."""
    rebuilt = {k: v for k, v in row.items() if k not in (LOW_FIELD, HIGH_FIELD)}
    rebuilt[POINTS_FIELD] = points
    ordered = {}
    for key, value in rebuilt.items():
        ordered[key] = value
        if key == POINTS_FIELD:
            ordered[LOW_FIELD] = low
            ordered[HIGH_FIELD] = high
    return ordered


def reprice(
    document: dict, index: GridironIndex, source_name: str, sleeper: dict[str, float]
) -> dict:
    """Overwrite each pool row's points from GridironAI's, in place. Returns stats.

    `sleeper` maps sleeper_id to the league-scored Sleeper projection, the fallback
    price for players GridironAI does not list."""
    tiers: collections.Counter[str] = collections.Counter()
    joins: dict[str, list] = {tier: [] for tier in TIERS}
    claimed: dict[int, str] = {}  # id(gridiron row) -> pool player name
    collisions: list[str] = []
    matched: list[tuple[dict, dict]] = []
    unmatched: list[dict] = []
    ambiguous: list[dict] = []

    for row in document["players"]:
        source, tier, clash = match(row, index)
        if source is None:
            unmatched.append(row)
            if clash:
                ambiguous.append({"row": row, "tier": tier, "candidates": clash})
            continue
        previous = claimed.setdefault(id(source), row["name"])
        if previous != row["name"]:
            collisions.append(f"{source['name']} <- {previous} and {row['name']}")
        tiers[tier] += 1
        joins[tier].append((row, source))
        matched.append((row, source))

    kept = [
        with_quantiles(row, source[POINTS_FIELD], source[LOW_FIELD], source[HIGH_FIELD])
        for row, source in matched
    ]
    scale = sleeper_scale(matched, sleeper)
    fallbacks: list[tuple[dict, float]] = []
    dropped: list[dict] = []
    for row in unmatched:
        sleeper_id = row.get("sleeper_id")
        theirs = sleeper.get(sleeper_id) if sleeper_id else None
        if not theirs:
            dropped.append(row)
            continue
        calibrated = round(theirs / scale[row["position"]], 1)
        fallbacks.append((row, theirs))
        kept.append(with_quantiles(row, calibrated, None, None))

    # Old rank breaks ties, keeping the previous (provider) order deterministic.
    kept.sort(key=lambda r: (-r[POINTS_FIELD], r["rank"]))
    seen: collections.Counter[str] = collections.Counter()
    for rank, row in enumerate(kept, start=1):
        seen[row["position"]] += 1
        row["rank"] = rank
        row["positional_rank"] = seen[row["position"]]

    document["players"] = kept
    document["player_count"] = len(kept)
    document["excluded"]["no_projection"] = (
        document["excluded"].get("no_projection", 0) + len(dropped)
    )
    document["scoring_scheme"]["points_copied_from"] = source_name
    fields = {
        key: value
        for key, value in document["fields"].items()
        if key not in (LOW_FIELD, HIGH_FIELD)
    }
    document["fields"] = {}
    for key, value in fields.items():
        document["fields"][key] = FIELD_DESCRIPTIONS.get(key, value)
        if key == POINTS_FIELD:
            document["fields"][LOW_FIELD] = FIELD_DESCRIPTIONS[LOW_FIELD]
            document["fields"][HIGH_FIELD] = FIELD_DESCRIPTIONS[HIGH_FIELD]
    document["points_source"] = {
        "file": source_name,
        "provider": "GridironAI",
        "quantiles": "points_low/points/points_high are the 10th/50th/90th percentiles",
        "note": "exported from the site with this league's own scoring selected",
        "sleeper_fallback": {
            "file": paths.SLEEPER_PROJECTIONS.name,
            "players": len(fallbacks),
            "scale_divisor": {pos: round(s, 3) for pos, s in scale.items()},
            "note": (
                "players GridironAI does not list carry their Sleeper projection "
                "divided by the per-position median Sleeper/GridironAI ratio of this "
                "run's joins, with null quantiles"
            ),
        },
    }

    # Worth a warning only when he would beat a kept player at his position.
    floor = {
        pos: min((row[POINTS_FIELD] for row in kept if row["position"] == pos), default=0.0)
        for pos in POSITIONS
    }
    unrepresented = [
        row
        for row in index.rows
        if id(row) not in claimed and row[POINTS_FIELD] > floor[row["position"]]
    ]

    return {
        "by_tier": {tier: tiers[tier] for tier in TIERS if tiers[tier]},
        "joins": joins,
        "fallbacks": fallbacks,
        "scale": scale,
        "dropped": dropped,
        "ambiguous": ambiguous,
        "collisions": collisions,
        "unrepresented": unrepresented,
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def report(document: dict, stats: dict) -> None:
    out = sys.stderr
    rows = document["players"]
    counts = collections.Counter(row["position"] for row in rows)
    print(
        "\npool: " + ", ".join(f"{pos} {n}" for pos, n in counts.most_common())
        + f" = {len(rows)}",
        file=out,
    )
    print(
        "matched by tier: "
        + ", ".join(f"{tier} {count}" for tier, count in stats["by_tier"].items()),
        file=out,
    )

    for tier in TIERS[1:]:
        joined = stats["joins"][tier]
        if not joined:
            continue
        print(f"\ntier '{tier}' joins ({len(joined)})", file=out)
        for row, source in joined:
            print(
                f"  {row['name']:<24} ({row['position']} {row['team']}) -> "
                f"{source['name']:<22} {source['position']} {source['team']} "
                f"{source[POINTS_FIELD]}",
                file=out,
            )

    if stats["fallbacks"]:
        scale = stats["scale"]
        print(
            f"\nsleeper fallbacks ({len(stats['fallbacks'])}) — no GridironAI row; "
            "scale divisor "
            + ", ".join(f"{pos} {s:.2f}" for pos, s in scale.items()),
            file=out,
        )
        for row, theirs in sorted(stats["fallbacks"], key=lambda t: -t[1]):
            print(
                f"  {row['name']:<24} {row['position']} {row['team']:<4} "
                f"sleeper {theirs:>6} -> {theirs / scale[row['position']]:>6.1f}",
                file=out,
            )

    if stats["dropped"]:
        print(f"\ndropped with no projection in either source ({len(stats['dropped'])})", file=out)
        for row in sorted(stats["dropped"], key=lambda r: -r[POINTS_FIELD]):
            print(
                f"  {row['name']:<24} {row['position']} {row['team']} "
                f"(old points {row[POINTS_FIELD]})",
                file=out,
            )
    for clash in stats["ambiguous"]:
        row = clash["row"]
        print(
            f"  ^ {row['name']} was ambiguous at tier '{clash['tier']}': "
            + ", ".join(
                f"{c['name']} ({c['position']} {c['team']})" for c in clash["candidates"]
            ),
            file=out,
        )

    ranks_ok = [row["rank"] for row in rows] == list(range(1, len(rows) + 1))
    monotone = all(
        rows[i][POINTS_FIELD] >= rows[i + 1][POINTS_FIELD] for i in range(len(rows) - 1)
    )
    ordered = all(
        row[LOW_FIELD] <= row[POINTS_FIELD] <= row[HIGH_FIELD]
        for row in rows
        if row[LOW_FIELD] is not None
    )
    per_pos: collections.Counter[str] = collections.Counter()
    positional_ok = True
    for row in rows:
        per_pos[row["position"]] += 1
        positional_ok &= row["positional_rank"] == per_pos[row["position"]]
    ids = {row["sleeper_id"] for row in rows if row.get("sleeper_id")}
    print(
        f"\n  rank 1..{len(rows)} gap-free: {ranks_ok}; monotone in {POINTS_FIELD}: "
        f"{monotone}; quantiles ordered: {ordered}; positional ranks consistent: "
        f"{positional_ok}; unique sleeper_ids: {len(ids) == len(rows)}",
        file=out,
    )

    print("\ntop 5 and the last 2 in the pool (low / points / high)", file=out)
    for row in rows[:5] + rows[-2:]:
        low = row[LOW_FIELD] if row[LOW_FIELD] is not None else "-"
        high = row[HIGH_FIELD] if row[HIGH_FIELD] is not None else "-"
        print(
            f"  {row['rank']:>3} {row['name']:<22} {row['position']}"
            f"{row['positional_rank']:<3} {low:>6} /{row[POINTS_FIELD]:>6} /"
            f"{high:>6}   adp {row['adp']}",
            file=out,
        )
    print(file=out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("pool", nargs="?", default=paths.POOL, type=Path)
    ap.add_argument(
        "--gridiron",
        default=None,
        type=Path,
        help="GridironAI CSV export (default: newest data/gridironai-rankings-*.csv)",
    )
    ap.add_argument("--report", action="store_true", help="print a validation summary to stderr")
    ap.add_argument("--indent", type=int, default=2, help="JSON indent; 0 for compact")
    args = ap.parse_args(argv)

    gridiron = args.gridiron or paths.gridiron_rankings()
    if gridiron is None or not gridiron.is_file():
        print(
            "error: no GridironAI export — save the site's rankings CSV (with this "
            f"league's scoring selected) as {paths.display(paths.DATA_DIR)}/"
            "gridironai-rankings-<season>-<n>.csv",
            file=sys.stderr,
        )
        return 1
    if not args.pool.is_file():
        print(f"error: {args.pool} not found — run the pool stage first", file=sys.stderr)
        return 1
    if not paths.SLEEPER_PROJECTIONS.is_file():
        print(
            f"error: {paths.display(paths.SLEEPER_PROJECTIONS)} not found — run "
            "fetch_sleeper_projections.py (it prices the players GridironAI lacks)",
            file=sys.stderr,
        )
        return 1

    with args.pool.open(encoding="utf-8") as handle:
        document = json.load(handle)
    with paths.SLEEPER_PROJECTIONS.open(encoding="utf-8") as handle:
        sleeper = {
            row["sleeper_id"]: row[POINTS_FIELD]
            for row in json.load(handle)["players"]
            if row["position"] in POSITIONS
        }
    try:
        rows = load_gridiron(gridiron)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not rows:
        print(f"error: no QB/RB/WR/TE rows in {gridiron}", file=sys.stderr)
        return 1

    try:
        stats = reprice(document, GridironIndex(rows), gridiron.name, sleeper)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if stats["collisions"]:
        for line in stats["collisions"]:
            print(f"error: one GridironAI row matched two pool players: {line}", file=sys.stderr)
        return 1
    if not document["players"]:
        print("error: no pool player has a GridironAI projection", file=sys.stderr)
        return 1
    # The GridironAI tail runs far past the DraftSharks pool, so only the biggest
    # absences are worth reading; a summary line keeps the rest countable.
    unrepresented = sorted(stats["unrepresented"], key=lambda r: -r[POINTS_FIELD])
    for row in unrepresented[:10]:
        print(
            f"warning: GridironAI projects {row['name']} ({row['position']} {row['team']}, "
            f"{row[POINTS_FIELD]} pts) but the pool cannot represent him",
            file=sys.stderr,
        )
    if len(unrepresented) > 10:
        print(
            f"warning: ... and {len(unrepresented) - 10} more unrepresented GridironAI "
            f"players at or below {unrepresented[10][POINTS_FIELD]} pts",
            file=sys.stderr,
        )

    with args.pool.open("w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=args.indent or None, ensure_ascii=False)
        handle.write("\n")

    print(
        f"points: {len(document['players'])} players re-priced from "
        f"{paths.display(gridiron)} ({len(stats['fallbacks'])} priced from calibrated "
        f"Sleeper medians), {len(stats['dropped'])} dropped with no projection in "
        f"either source -> {paths.display(args.pool)}",
        file=sys.stderr,
    )
    if args.report:
        report(document, stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
