"""The draft pool: pool.json rows as Player objects.

The value input is GridironAI's projection distribution — `points` (the median
one-season projection at 0.5/rec, this league's scoring) flanked by `points_low` and
`points_high`, the provider's calibrated 10th/90th-percentile season outcomes.
`points` is the one scalar the solver reads; rank.py reprices it to the wire plus the
truncated expected value above the wire (value.reprice_with_uncertainty) before any
board is built. Draftsharks' 3D value is ignored entirely and
is not even carried into the pool: it is a provider-scaled ordinal that already bakes in
someone else's roster assumptions, and it is not in points, so it cannot enter a
points-denominated lineup objective. Kickers and IDP are already dropped upstream
because the roster has no slot for them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .league import POINTS_FIELD, POINTS_HIGH_FIELD, POINTS_LOW_FIELD, POSITIONS


@dataclass(slots=True)
class Player:
    player_id: int
    name: str
    position: str
    team: str
    age: float | None
    bye_week: int | None
    is_rookie: bool
    points: float
    provider_adp: float | None
    sleeper_id: str | None = None  # the only key draft.json shares with the pool
    availability_index: int = 0  # rank on the opponents' consensus board, 0-based
    # The projection quantiles around `points`, which starts as the median and is
    # repriced to a certainty-equivalent; `points_base` keeps the untouched median.
    # None when the provider published no quantiles (Sleeper-fallback players):
    # the reprice then collapses the distribution to a point mass at the median.
    points_low: float | None = None
    points_high: float | None = None
    points_base: float = 0.0


def load_pool(path: Path) -> tuple[list[Player], dict]:
    """Read pool.json: already filtered to QB/RB/WR/TE with a usable one-season projection.

    build_pool.py does the filtering — positions with no roster slot, the source's zeros
    where a null belongs — so this only re-checks the
    invariants it promises rather than re-deriving them. The guards stay because a
    hand-edited or stale pool is the likeliest way this ever gets bad input.
    """
    raw = json.loads(path.read_text())
    players: list[Player] = []
    dropped = {"non_offense": 0, "zero_projection": []}
    for rec in raw["players"]:
        if rec["position"] not in POSITIONS:
            dropped["non_offense"] += 1
            continue
        # Direct key access: a pool.json without the value columns predates the
        # GridironAI switch and must be rebuilt, not silently valued at zero.
        # Null quantiles are a real value: a Sleeper-fallback player carries a
        # median only, and both sides must agree about it.
        points = rec[POINTS_FIELD]
        low, high = rec[POINTS_LOW_FIELD], rec[POINTS_HIGH_FIELD]
        if not points or points <= 0:
            dropped["zero_projection"].append(rec["name"])
            continue
        if (low is None) != (high is None):
            raise ValueError(
                f"{rec['name']} has one quantile of two ({low}/{high}) — rebuild pool.json"
            )
        if low is not None and not low <= points <= high:
            raise ValueError(
                f"{rec['name']} has disordered projection quantiles "
                f"{low}/{points}/{high} — rebuild pool.json"
            )
        players.append(
            Player(
                player_id=rec["player_id"],
                name=rec["name"],
                position=rec["position"],
                team=rec["team"],
                age=rec.get("age"),
                bye_week=rec.get("bye_week"),
                is_rookie=bool(rec.get("is_rookie")),
                points=float(points),
                provider_adp=rec.get("adp"),
                sleeper_id=rec.get("sleeper_id"),
                points_low=low,
                points_high=high,
                points_base=float(points),
            )
        )

    players.sort(key=lambda p: (-p.points, p.player_id))
    counts = {pos: 0 for pos in POSITIONS}
    for p in players:
        counts[p.position] += 1

    meta = {
        "source_file": str(path),
        "source_player_count": raw.get("player_count", len(raw["players"])),
        "source_of_pool": raw.get("source_file"),
        "points_source": (raw.get("points_source") or {}).get("file"),
        "pool_size": len(players),
        "by_position": counts,
        # The join key to draft.json. A pool player without one can never be recognised
        # as drafted, so a shortfall here is a silent way for the live board to go wrong.
        "with_sleeper_id": sum(1 for p in players if p.sleeper_id),
        "dropped_non_offense": dropped["non_offense"],
        "dropped_zero_projection": sorted(dropped["zero_projection"]),
    }
    return players, meta


def by_position(players: list[Player]) -> dict[str, list[Player]]:
    out: dict[str, list[Player]] = {pos: [] for pos in POSITIONS}
    for p in players:
        out[p.position].append(p)
    return out
