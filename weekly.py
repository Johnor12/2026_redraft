#!/usr/bin/env python3
"""Refresh the ESPN league state, then optimize this week's lineup and my waiver claims.

Two steps:

    1. league_pipeline/fetch_league.py   ESPN league API -> league.json
    2. this script                       league.json + GridironAI exports -> stdout

Projections are GridironAI's, exported from the site with this league's scoring selected
and saved at the repo root for ESPN's current scoring period:

    gridironai-rankings-<season>-<week>.csv                           this week, D/ST included
    gridironai-rankings-<season>-rest-of-season-from-week-<week>.csv  rest of season, no D/ST

Offense joins to ESPN by name through the pool pipeline's GridironAI matcher, D/ST by
team. GridironAI publishes no rest-of-season D/ST, so that one value is ESPN's own
league-scored projection. Available players GridironAI does not list are not considered.

Lineup: each player's expected points this week (Swanson's 0.3/0.4/0.3 over GridironAI's
10th/50th/90th percentiles), maximized over the starting slots. Players whose game has
started keep their slot and IR stays IR. Filling the most restrictive slot first by
descending points is exact for this lineup shape.

Waivers: the league runs traditional rolling waivers with no budget, so the output is an
ordered add/drop claim list rather than bids. A claim is worth its rest-of-season change in
expected optimal lineup points — the draft ranker's roster objective (ranker/value.py) —
with every player repriced at the wire plus his truncated expected value above it. Both the
wire and the one fallback body per position are the best available players other than the
add: the alternative to claiming him is losing him. Each add takes its best drop.

The list is built in rounds to match how ESPN processes it. A round is the best claim
followed by the next-best claims with the same drop: a claim lost to a team ahead in
priority falls through to the next, and once the drop is gone the rest of the round fails.
The next round is ranked on the roster the round's first claim would leave.

Nothing is submitted to ESPN.

Usage:
    uv run weekly.py
"""

from __future__ import annotations

import collections
import csv
import datetime as dt
import json
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from ranker import league
from ranker.value import expected_lineup_value, reprice_with_uncertainty

REPO_ROOT = Path(__file__).resolve().parent
LEAGUE = REPO_ROOT / "league.json"

sys.path.insert(0, str(REPO_ROOT / "pool_pipeline"))
from apply_gridiron_points import (  # noqa: E402
    GRIDIRON_TEAM_ALIASES,
    GridironIndex,
    load_gridiron,
    match,
)

STARTERS = {**league.STARTING_SLOTS, "D/ST": 1}
SLOT_CHAIN = {**league.SLOT_CHAIN, "D/ST": ("D/ST",)}
LINEUP_SLOTS = {**STARTERS, "BE": league.BENCH_SLOTS, "IR": league.IR_SLOTS}
SLOT_ORDER = ("QB", "RB", "WR", "TE", "FLEX", "D/ST", "BE", "IR")
POSITIONS = (*league.POSITIONS, "D/ST")

# ESPN keeps a player placed in IR while Out legal after an upgrade to Q or D; one left
# there once healthy blocks roster moves until he is activated or dropped.
IR_LEGAL = {"OUT", "INJURY_RESERVE", "QUESTIONABLE", "DOUBTFUL"}

# A claim sends me to the back of the waiver order; a rest-of-season gain below this is
# not worth the priority.
MIN_CLAIM_GAIN = 3.0


@dataclass(slots=True)
class Player:
    player_id: int  # ESPN id; value.py breaks ties on it
    name: str
    position: str  # QB, RB, WR, TE or D/ST
    team: str | None
    slot: str | None  # my current lineup slot
    injury: str | None
    locked: bool
    droppable: bool
    clears_at: str | None  # None for a free agent
    week: float  # expected points this week
    # Rest-of-season value under the names value.py reads: `points` is the median until a
    # repriced copy is made; quantiles are None for ESPN-priced D/ST.
    points: float
    points_base: float
    points_low: float | None
    points_high: float | None


@dataclass(slots=True)
class Claim:
    add: Player
    drop: Player | None
    gain: float
    week_gain: float = 0.0
    round: int = 0


# --- inputs -----------------------------------------------------------------------


def expectation(low: float, median: float, high: float) -> float:
    w_low, w_mid, w_high = league.QUANTILE_WEIGHTS
    return w_low * low + w_mid * median + w_high * high


def gridiron_offense(rows: list[dict], path: Path) -> dict[int, dict]:
    """GridironAI row per ESPN id, joined by name exactly as the pool pipeline joins."""
    index = GridironIndex(load_gridiron(path))
    joined: dict[int, dict] = {}
    claimed: dict[int, int] = {}
    for row in rows:
        if row["position"] == "D/ST":
            continue
        source, _, _ = match(row, index)
        if source is None:
            continue
        if claimed.setdefault(id(source), row["espn_id"]) != row["espn_id"]:
            raise ValueError(f"{path.name}: {source['name']} matched two ESPN players")
        joined[row["espn_id"]] = source
    return joined


def gridiron_dst(path: Path) -> dict[str, tuple[float, float, float]]:
    """GridironAI's weekly D/ST rows by team: (low, median, high)."""
    with path.open(encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = [column.split(" (")[0].strip() for column in next(reader)]
        team, pos = header.index("Team"), header.index("Pos.")
        values = [
            header.index(c) for c in ("Downside Points", "Expected Points", "Upside Points")
        ]
        return {
            GRIDIRON_TEAM_ALIASES.get(raw[team], raw[team]): tuple(
                float(raw[i]) for i in values
            )
            for raw in reader
            if raw[pos] == "DST"
        }


def load_players(
    document: dict, weekly: Path, ros: Path
) -> tuple[list[Player], list[Player], list[str]]:
    """(my roster, available players, warnings) with GridironAI's projections."""
    rows = document["players"]
    week_rows = gridiron_offense(rows, weekly)
    ros_rows = gridiron_offense(rows, ros)
    dst_week = gridiron_dst(weekly)

    mine: list[Player] = []
    available: list[Player] = []
    warnings: list[str] = []
    unlisted: list[dict] = []
    for row in rows:
        is_mine = row["status"] == "MINE"
        if row["position"] == "D/ST":
            quantiles = dst_week.get(row["team"])
            week = expectation(*quantiles) if quantiles else None
            median, low, high = row["espn_ros"] or 0.0, None, None
        else:
            weekly_row, ros_row = week_rows.get(row["espn_id"]), ros_rows.get(row["espn_id"])
            week = (
                expectation(
                    weekly_row["points_low"], weekly_row["points"], weekly_row["points_high"]
                )
                if weekly_row
                else None
            )
            if ros_row is None and not is_mine:
                unlisted.append(row)
                continue
            median, low, high = (
                (ros_row["points"], ros_row["points_low"], ros_row["points_high"])
                if ros_row
                else (0.0, None, None)
            )
            if is_mine and ros_row is None:
                warnings.append(
                    f"GridironAI has no rest-of-season row for my {row['name']}; valued at 0"
                )
        if is_mine and week is None:
            warnings.append(
                f"GridironAI has no week {document['scoring_period']} row for my "
                f"{row['name']}; 0 this week"
            )
        player = Player(
            player_id=row["espn_id"],
            name=row["name"],
            position=row["position"],
            team=row["team"],
            slot=row["lineup_slot"],
            injury=row["injury_status"],
            locked=row["locked"],
            droppable=bool(row["droppable"]),
            clears_at=row["waivers_clear_at"],
            week=week or 0.0,
            points=median,
            points_base=median,
            points_low=low,
            points_high=high,
        )
        (mine if is_mine else available).append(player)

    if unlisted:
        unlisted.sort(key=lambda r: -(r["espn_ros"] or 0.0))
        warnings.append(
            f"GridironAI does not list {len(unlisted)} available players, so they are not "
            "considered; ESPN's biggest: "
            + ", ".join(f"{r['name']} ({r['position']}, {r['espn_ros']})" for r in unlisted[:5])
        )
    return mine, available, warnings


# --- lineup -----------------------------------------------------------------------


def best_lineup(roster: list[Player]) -> dict[int, str]:
    """Slot per player: the week's expected-points optimum, locked players and IR fixed."""
    open_slots = dict(STARTERS)
    lineup: dict[int, str] = {}
    for p in roster:
        if p.slot is not None and (p.locked or p.slot == "IR"):
            lineup[p.player_id] = p.slot
            if p.slot in open_slots:
                open_slots[p.slot] -= 1
    free = sorted(
        (p for p in roster if p.player_id not in lineup), key=lambda p: (-p.week, p.player_id)
    )
    for p in free:
        slot = next((s for s in SLOT_CHAIN[p.position] if open_slots[s] > 0), "BE")
        if slot != "BE":
            open_slots[slot] -= 1
        lineup[p.player_id] = slot
    return lineup


def lineup_points(roster: list[Player], lineup: dict[int, str]) -> float:
    return sum(p.week for p in roster if lineup[p.player_id] in STARTERS)


# --- waivers ----------------------------------------------------------------------


def roster_value(roster: list[Player], wire: dict[str, float]) -> float:
    """Rest-of-season expected optimal lineup points."""
    offense = [p for p in roster if p.position != "D/ST"]
    # One D/ST slot and no flex: the best defense, rostered or the wire's, starts.
    dst = max([p.points for p in roster if p.position == "D/ST"] + [wire["D/ST"]])
    return expected_lineup_value(offense, wire) + dst


def priced(
    mine: list[Player], available: list[Player], baseline: dict[str, float]
) -> tuple[list[Player], dict[int, Player], dict[str, list[Player]]]:
    """Copies repriced against `baseline`: (my roster, available by id, top two available
    per position by repriced value)."""
    roster = [replace(p) for p in mine]
    pool = [replace(p) for p in available]
    reprice_with_uncertainty([p for p in roster + pool if p.position != "D/ST"], baseline)
    top = {
        pos: sorted(
            (p for p in pool if p.position == pos), key=lambda p: (-p.points, p.player_id)
        )[:2]
        for pos in POSITIONS
    }
    return roster, {p.player_id: p for p in pool}, top


def rank_claims(mine: list[Player], available: list[Player], limits: dict) -> list[Claim]:
    """Each available player's claim with his best drop, best first."""
    capacity = sum(STARTERS.values()) + league.BENCH_SLOTS
    held = collections.Counter(p.position for p in mine)
    week_now = lineup_points(mine, best_lineup(mine))

    # Every claim is "take him or lose him to a claim ahead of mine", so both the wire a
    # bust is cut for (the best available median) and the fallback body (the best
    # available repriced value) exclude the add. Only a position's head changes the
    # baseline, so few repricings are needed.
    by_median = {
        pos: sorted(
            (p for p in available if p.position == pos),
            key=lambda p: (-p.points_base, p.player_id),
        )[:2]
        for pos in league.POSITIONS
    }
    boards: dict[tuple, tuple] = {}

    claims = []
    for add in available:
        baseline = {
            pos: next((p.points_base for p in heads if p is not add), 0.0)
            for pos, heads in by_median.items()
        }
        key = tuple(baseline.values())
        if key not in boards:
            boards[key] = priced(mine, available, baseline)
        roster, pool, top = boards[key]
        candidate = pool[add.player_id]
        wire = {
            pos: next((p.points for p in top[pos] if p is not candidate), 0.0)
            for pos in POSITIONS
        }
        base = roster_value(roster, wire)

        active = [p for p in roster if p.slot != "IR"]
        drops: list[Player | None] = [p for p in active if p.droppable]
        if len(active) < capacity:
            drops.append(None)  # an open roster spot: add without dropping
        limit = limits[add.position]
        best: Claim | None = None
        for drop in drops:
            after = held[add.position] + 1 - (drop is not None and drop.position == add.position)
            if limit is not None and after > limit:
                continue
            gain = roster_value([p for p in roster if p is not drop] + [candidate], wire) - base
            if best is None or gain > best.gain:
                best = Claim(candidate, drop, gain)
        if best is not None and best.gain >= MIN_CLAIM_GAIN:
            after = [p for p in roster if p is not best.drop] + [candidate]
            best.week_gain = lineup_points(after, best_lineup(after)) - week_now
            claims.append(best)
    claims.sort(key=lambda c: (-c.gain, c.add.player_id))
    return claims


def plan_claims(mine: list[Player], available: list[Player], limits: dict) -> list[Claim]:
    """ESPN's claim list, in rounds: the best claim, backed by the next-best claims sharing
    its drop, then the next round on the roster that best claim leaves."""
    plan: list[Claim] = []
    round_number = 0
    while options := rank_claims(mine, available, limits):
        round_number += 1
        head = options[0]
        drop_id = head.drop.player_id if head.drop else None
        for claim in options:
            if (claim.drop.player_id if claim.drop else None) == drop_id:
                claim.round = round_number
                plan.append(claim)
        added = next(p for p in available if p.player_id == head.add.player_id)
        # Never drop a player the list has just claimed.
        mine = [p for p in mine if p.player_id != drop_id] + [
            replace(added, slot="BE", droppable=False)
        ]
        available = [p for p in available if p is not added]
    return plan


# --- report -----------------------------------------------------------------------


def label(p: Player) -> str:
    return f"{p.name} ({p.position} {p.team or 'FA'})"


def when(iso: str | None) -> str:
    if iso is None:
        return "free agent, add now"
    return "clears " + dt.datetime.fromisoformat(iso).astimezone().strftime("%a %m-%d %H:%M")


def report(document: dict, mine: list[Player], claims: list[Claim]) -> None:
    week = document["scoring_period"]
    lineup = best_lineup(mine)
    current = {p.player_id: p.slot for p in mine}
    print(f"\nWeek {week} · {document['me']['team_name']} · {document['league_name']}")
    print(
        f"\nLineup: {lineup_points(mine, lineup):.1f} expected "
        f"(current ESPN lineup {lineup_points(mine, current):.1f})"
    )
    for p in sorted(mine, key=lambda p: (SLOT_ORDER.index(lineup[p.player_id]), -p.week)):
        flags = [p.injury] if p.injury not in (None, "ACTIVE") else []
        if p.locked:
            flags.append("locked")
        print(
            f"  {lineup[p.player_id]:<5} {p.name:<24} {p.team or 'FA':<4} {p.week:>5.1f}  "
            + " ".join(flags)
        )
    moves = [
        f"start {p.name}"
        for p in mine
        if lineup[p.player_id] in STARTERS and current[p.player_id] not in STARTERS
    ] + [
        f"bench {p.name}"
        for p in mine
        if lineup[p.player_id] not in STARTERS and current[p.player_id] in STARTERS
    ]
    print("Moves: " + (", ".join(moves) if moves else "none, the current starters are optimal"))

    waivers = document["waivers"]
    print(
        f"\nWaiver claims, in priority order (rank {waivers['my_rank']} of {waivers['teams']}; "
        f"rest-of-season gain >= {MIN_CLAIM_GAIN})\n"
        "  A round's claims share a drop, so at most one of them wins; each round assumes "
        "the round before won its first claim."
    )
    if not claims:
        print("  none")
    letters = collections.Counter()
    for claim in claims:
        letter = "abcdefghijklmnopqrstuvwxyz"[letters[claim.round]]
        letters[claim.round] += 1
        drop = label(claim.drop) if claim.drop else "(open spot)"
        print(
            f"  {claim.round:>2}{letter}. add {label(claim.add):<34} drop {drop:<34} "
            f"{claim.gain:>+6.1f} ROS {claim.week_gain:>+5.1f} wk{week}  "
            + when(claim.add.clears_at)
        )


def main() -> int:
    print("\n=== [1/2] league ===", file=sys.stderr)
    code = subprocess.run(
        ["uv", "run", "league_pipeline/fetch_league.py"], cwd=REPO_ROOT
    ).returncode
    if code != 0:
        print(f"weekly failed at step 'league' (exit {code})", file=sys.stderr)
        return code

    print("\n=== [2/2] optimize ===", file=sys.stderr)
    document = json.loads(LEAGUE.read_text(encoding="utf-8"))
    if document["lineup_slots"] != LINEUP_SLOTS:
        print(
            f"error: ESPN lineup {document['lineup_slots']} is not {LINEUP_SLOTS}",
            file=sys.stderr,
        )
        return 1
    if document["waivers"]["uses_budget"]:
        print("error: the league now uses a waiver budget; bids are not modeled", file=sys.stderr)
        return 1

    season, week = document["season"], document["scoring_period"]
    weekly = REPO_ROOT / f"gridironai-rankings-{season}-{week}.csv"
    ros = REPO_ROOT / f"gridironai-rankings-{season}-rest-of-season-from-week-{week}.csv"
    missing = [path.name for path in (weekly, ros) if not path.is_file()]
    if missing:
        print(
            f"error: missing {', '.join(missing)} — export GridironAI's week {week} and "
            f"rest-of-season rankings (league scoring) to the repo root",
            file=sys.stderr,
        )
        return 1

    try:
        mine, available, warnings = load_players(document, weekly, ros)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for p in mine:
        if p.slot == "IR" and p.injury not in IR_LEGAL:
            warnings.append(
                f"{p.name} is in IR but {p.injury}: ESPN blocks roster moves until he is "
                "activated or dropped"
            )
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)

    report(document, mine, plan_claims(mine, available, document["position_limits"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
