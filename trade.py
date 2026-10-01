#!/usr/bin/env python3
"""Price a trade with one opponent, or sweep every simple package with one or all of them.

Three steps, like weekly.py:

    1. league_pipeline/fetch_league.py        ESPN -> league.json (every roster)
    2. league_pipeline/fetch_fantasypros.py   FantasyPros ROS consensus -> fantasypros.json
    3. this script                            + weekly.py's GridironAI CSVs -> stdout

Every roster is valued with weekly.py's objective — rest-of-season expected optimal lineup
points (ranker/value.py) with one always-available wire body per position — under three
lenses:

    gai   GridironAI's ROS quantiles repriced at the wire: my truth
    espn  ESPN's ROS projection as the app shows it: what the opponent sees
    fp    FantasyPros' ROS consensus, each positional rank mapped onto GridironAI's
          positional point curve so it lives on the same scale: the market

My side of a trade is valued under gai, the opponent's under espn and fp. Both rosters are
settled before and after the trade with the free moves open to them anyway — drop over a
cap, sign the best free agent under capacity, then improving drop-and-sign swaps — so a
2-for-1 gets no credit for the free agent it makes room for.

Replacement level is free agents only (a waiver player is one claim, not a renewable body),
filtered by GridironAI's injury-aware median to within FA_HEALTH of the position's best
free agent, because ESPN's rest-of-season numbers ignore injuries.

Acceptance is a heuristic, not a prediction: the opponent's gain in whichever consensus
lens is less favorable to him (70%) blended with the other (30%), in GridironAI units,
minus a penalty for asking for a top-36 consensus player, through a logistic centered at
+4 with width 8 — an even-looking deal is ~40%, +10 ~70%, +20 ~90%.

The sweep tries every package of up to MAX_SIDE players a side, keeps the ones that gain
me at least MIN_SWEEP_GAIN, drops throw-ins (a player either side would cut at once) and
packages that send my starting QB without one back — the ranker's upside repricing puts
free-agent QB ceilings near my starter, so those look free and are not trusted — and
ranks by gain times acceptance.

Usage:
    uv run trade.py "Fell For It" --send "Bucky Irving" --get "Rashee Rice"   price one trade
    uv run trade.py "Fell For It"                                               sweep one opponent
    uv run trade.py                                                             sweep every opponent

Teams match by ESPN id or a case-insensitive part of the name; players by a part of the
name within the roster they come from. Nothing is submitted to ESPN.
"""

from __future__ import annotations

import argparse
import collections
import itertools
import json
import math
import multiprocessing
import statistics
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

import weekly
from ranker import league
from ranker.value import (
    expected_lineup_value,
    position_expected_value,
    reprice_with_uncertainty,
)
from weekly import Player

REPO_ROOT = Path(__file__).resolve().parent
LEAGUE = REPO_ROOT / "league.json"
FANTASYPROS = REPO_ROOT / "fantasypros.json"

sys.path.insert(0, str(REPO_ROOT / "pool_pipeline"))
from apply_gridiron_points import GridironIndex, load_gridiron, match  # noqa: E402

POSITIONS = league.POSITIONS
ROSTER_SPOTS = sum(weekly.STARTERS.values()) + league.BENCH_SLOTS  # 13 active, IR apart

# A free agent is replacement level only within this share of the position's best free
# agent's GridironAI median: ESPN still projects injured quarterbacks at starter money.
FA_HEALTH = 0.6
FA_DEPTH = 4  # free agents a settling roster may sign per position, best first per lens
SWAP_MIN = 0.5  # an improving drop-and-sign below this is noise
SWAP_DROPS = 6  # only a roster's cheapest bodies are swap candidates

MAX_SIDE = 3
MIN_SWEEP_GAIN = 3.0
SWEEP_ROWS = 12

STAR_ECR = 36
STAR_PENALTY = 6.0
ACCEPT_MIDPOINT = 4.0
ACCEPT_WIDTH = 8.0

FP_TEAM_ALIASES = {"JAC": "JAX"}
LENSES = ("gai", "espn", "fp")


@dataclass
class Market:
    """Every roster and the free-agent pool under the three lenses."""

    week: int
    weeks_left: int
    my_id: int
    limits: dict[str, int | None]
    teams: dict[int, dict]
    rosters: dict[int, list[int]]  # team id -> offense ids, IR included
    ir: dict[int, set[int]]
    capacity: dict[int, int]  # active offense spots: ROSTER_SPOTS minus D/STs held
    players: dict[int, Player]  # GridironAI-priced; `points` is the raw ROS median
    espn_ros: dict[int, float]
    fp_rank: dict[int, tuple[int, int] | None]  # id -> (positional rank, overall ECR)
    lens: dict[str, dict[int, Player]]  # lens -> id -> Player priced under it
    fa_top: dict[str, dict[str, list[int]]]  # lens -> position -> signable, best first
    scale: dict[str, float] = field(default_factory=dict)  # lens units per gai unit
    _baselines: dict = field(default_factory=dict)

    def baseline(
        self, team_id: int, lens: str
    ) -> tuple[list[int], float, list[int], list[int]]:
        """A roster settled before any trade; memoized, since every priced trade compares
        against it."""
        key = (team_id, lens)
        if key not in self._baselines:
            self._baselines[key] = self.settle(
                self.rosters[team_id], self.ir[team_id], self.capacity[team_id], lens
            )
        return self._baselines[key]

    def wire(self, lens: str, used: frozenset = frozenset()) -> dict[str, float]:
        priced = self.lens[lens]
        return {
            pos: next(
                (priced[i].points for i in self.fa_top[lens][pos] if i not in used), 0.0
            )
            for pos in POSITIONS
        }

    def value(self, ids: list[int], lens: str, used: frozenset = frozenset()) -> float:
        priced = self.lens[lens]
        return expected_lineup_value([priced[i] for i in ids], self.wire(lens, used))

    def settle(
        self, ids: list[int], ir_ids: set[int], cap: int, lens: str
    ) -> tuple[list[int], float, list[int], list[int]]:
        """Best legal shape under one lens with free moves only. Returns (ids, value,
        signed, dropped). The same moves are open with or without a trade, so settling both
        sides of a comparison isolates the trade."""
        priced = self.lens[lens]
        ids = list(ids)
        signed: list[int] = []
        dropped: list[int] = []
        while True:
            active = [i for i in ids if i not in ir_ids]
            counts = collections.Counter(priced[i].position for i in active)
            over = [
                pos
                for pos in POSITIONS
                if self.limits[pos] is not None and counts[pos] > self.limits[pos]
            ]
            used = frozenset(signed)
            if over or len(active) > cap:
                candidates = (
                    [i for i in active if priced[i].position in over]
                    if over
                    else active
                )
                drop = max(
                    candidates,
                    key=lambda i: (
                        self.value([j for j in ids if j != i], lens, used),
                        i,
                    ),
                )
                ids.remove(drop)
                dropped.append(drop)
            elif len(active) < cap:
                base = self.value(ids, lens, used)
                candidates = [
                    f
                    for pos in POSITIONS
                    for f in self.fa_top[lens][pos]
                    if f not in used
                    and f not in ids
                    and (self.limits[pos] is None or counts[pos] < self.limits[pos])
                ]
                best = max(
                    candidates, key=lambda f: self.value(ids + [f], lens, used | {f})
                )
                if self.value(ids + [best], lens, used | {best}) - base <= 0:
                    break
                ids.append(best)
                signed.append(best)
            else:
                break
        for _ in range(4):
            active = [i for i in ids if i not in ir_ids]
            counts = collections.Counter(priced[i].position for i in active)
            used = frozenset(signed)
            base = self.value(ids, lens, used)
            best = None
            for drop in sorted(active, key=lambda i: priced[i].points)[:SWAP_DROPS]:
                rest = [j for j in ids if j != drop]
                for pos in POSITIONS:
                    after = counts[pos] - (priced[drop].position == pos) + 1
                    if self.limits[pos] is not None and after > self.limits[pos]:
                        continue
                    for sign in self.fa_top[lens][pos][:2]:
                        if sign in used or sign in ids:
                            continue
                        gain = self.value(rest + [sign], lens, used | {sign}) - base
                        if best is None or gain > best[0]:
                            best = (gain, drop, sign)
            if best is None or best[0] < SWAP_MIN:
                break
            _, drop, sign = best
            ids.remove(drop)
            ids.append(sign)
            dropped.append(drop)
            signed.append(sign)
        return ids, self.value(ids, lens, frozenset(signed)), signed, dropped

    def ir_after(self, team_id: int, ids: list[int], incoming: list[int]) -> set[int]:
        """A team keeps its IR stash; an incoming player who sat in the other side's IR
        takes the receiver's IR slot only while one is free."""
        kept = {i for i in self.ir[team_id] if i in ids}
        for i in incoming:
            if (
                any(i in stash for stash in self.ir.values())
                and len(kept) < league.IR_SLOTS
            ):
                kept.add(i)
        return kept

    def week_points(self, ids: list[int]) -> float:
        roster = [
            replace(
                self.lens["gai"][i],
                slot="IR" if i in self.ir[self.my_id] else None,
                locked=False,
            )
            for i in ids
        ]
        return weekly.lineup_points(roster, weekly.best_lineup(roster))

    def ecr(self, i: int) -> int:
        hit = self.fp_rank[i]
        return hit[1] if hit else 999

    def accept(
        self, sends: list[int], gets: list[int], him_espn: float, him_fp: float
    ) -> float:
        a, b = him_espn / self.scale["espn"], him_fp / self.scale["fp"]
        perceived = 0.7 * min(a, b) + 0.3 * max(a, b)
        star = min(sends + gets, key=self.ecr)
        if star in gets and self.ecr(star) <= STAR_ECR:
            perceived -= STAR_PENALTY
        return 1 / (1 + math.exp(-(perceived - ACCEPT_MIDPOINT) / ACCEPT_WIDTH))


@dataclass
class Priced:
    his_id: int
    sends: list[int]
    gets: list[int]
    me: float  # my rest-of-season gain, GridironAI lens
    me_week: float  # my lineup change this week
    him_espn: float
    him_fp: float
    accept: float
    my_before: list[int]  # my settled baseline roster
    my_after: list[int]
    my_signs: list[int]
    my_drops: list[int]
    his_signs: list[int]
    his_drops: list[int]
    my_used: frozenset  # free agents signed after the trade, excluded from my wire

    @property
    def ev(self) -> float:
        return self.me * self.accept


# --- inputs -----------------------------------------------------------------------


def load_market(
    document: dict, weekly_csv: Path, ros_csv: Path, fantasypros: dict
) -> Market:
    available_rows = [r for r in document["players"] if r["status"] != "MINE"]
    teams = {t["team_id"]: t for t in document["teams"]}
    rosters: dict[int, list[int]] = {}
    ir: dict[int, set[int]] = {}
    capacity: dict[int, int] = {}
    players: dict[int, Player] = {}
    available: list[Player] = []
    for team_id, team in teams.items():
        rows = [dict(r, status="MINE", waivers_clear_at=None) for r in team["roster"]]
        mine, available, warnings = weekly.load_players(
            {
                "players": rows + available_rows,
                "scoring_period": document["scoring_period"],
            },
            weekly_csv,
            ros_csv,
        )
        for warning in warnings:
            if "does not list" not in warning:
                print(f"warning [{team['name']}]: {warning}", file=sys.stderr)
        offense = [p for p in mine if p.position != "D/ST"]
        rosters[team_id] = [p.player_id for p in offense]
        ir[team_id] = {p.player_id for p in offense if p.slot == "IR"}
        capacity[team_id] = ROSTER_SPOTS - sum(p.position == "D/ST" for p in mine)
        players.update({p.player_id: p for p in offense})
    available = [p for p in available if p.position != "D/ST"]
    players.update({p.player_id: p for p in available})

    espn_ros = {r["espn_id"]: r["espn_ros"] or 0.0 for r in document["players"]}
    for team in teams.values():
        espn_ros.update({r["espn_id"]: r["espn_ros"] or 0.0 for r in team["roster"]})

    fp_rows = [
        {
            "name": r["player_name"],
            "team": FP_TEAM_ALIASES.get(r["player_team_id"], r["player_team_id"]),
            "position": r["player_position_id"],
            "rank": int(r["pos_rank"][len(r["player_position_id"]) :]),
            "ecr": r["rank_ecr"],
        }
        for r in fantasypros["players"]
        if r["player_position_id"] in POSITIONS
    ]
    fp_index = GridironIndex(fp_rows)
    fp_count = collections.Counter(r["position"] for r in fp_rows)
    curve = {
        pos: sorted(
            (r["points"] for r in load_gridiron(ros_csv) if r["position"] == pos),
            reverse=True,
        )
        for pos in POSITIONS
    }

    def fp_lookup(p: Player) -> tuple[int, int] | None:
        # Full-name tiers only: the last-name+team tier matched Kyle Allen to Josh Allen.
        hit, tier, _ = match(
            {"name": p.name, "team": p.team, "position": p.position}, fp_index
        )
        return (hit["rank"], hit["ecr"]) if hit and tier != "last_name_team" else None

    def fp_points(p: Player, hit: tuple[int, int] | None) -> float:
        rank = (
            hit[0] if hit else fp_count[p.position] + 1
        )  # unranked: below the last ranked
        points = curve[p.position]
        return points[min(rank, len(points)) - 1]

    fp_rank = {i: fp_lookup(p) for i, p in players.items()}

    free_agents = [p for p in available if p.clears_at is None]
    best_fa = {
        pos: max(p.points_base for p in free_agents if p.position == pos)
        for pos in POSITIONS
    }
    fa_pool = {
        pos: [
            p.player_id
            for p in free_agents
            if p.position == pos and p.points_base >= FA_HEALTH * best_fa[pos]
        ]
        for pos in POSITIONS
    }
    gai = {i: replace(p) for i, p in players.items()}
    reprice_with_uncertainty(list(gai.values()), best_fa)
    lens = {
        "gai": gai,
        "espn": {
            i: replace(p, points=espn_ros.get(i, 0.0)) for i, p in players.items()
        },
        "fp": {
            i: replace(p, points=fp_points(p, fp_rank[i])) for i, p in players.items()
        },
    }
    fa_top = {
        name: {
            pos: sorted(fa_pool[pos], key=lambda i: (-priced[i].points, i))[:FA_DEPTH]
            for pos in POSITIONS
        }
        for name, priced in lens.items()
    }
    market = Market(
        week=document["scoring_period"],
        weeks_left=document["final_scoring_period"] - document["scoring_period"] + 1,
        my_id=document["me"]["team_id"],
        limits=document["position_limits"],
        teams=teams,
        rosters=rosters,
        ir=ir,
        capacity=capacity,
        players=players,
        espn_ros=espn_ros,
        fp_rank=fp_rank,
        lens=lens,
        fa_top=fa_top,
    )
    # League-average lineup value under each lens relative to GridironAI puts opponent-lens
    # gains on GridironAI's scale.
    market.scale = {
        name: statistics.mean(
            market.value(ids, name) / market.value(ids, "gai")
            for ids in rosters.values()
        )
        for name in ("espn", "fp")
    }
    return market


# --- pricing ----------------------------------------------------------------------


def price(market: Market, his_id: int, sends: list[int], gets: list[int]) -> Priced:
    me, cap = market.my_id, market.capacity[market.my_id]
    my_before, base, _, _ = market.baseline(me, "gai")
    my_ids = [i for i in market.rosters[me] if i not in sends] + list(gets)
    my_after, my_value, my_signs, my_drops = market.settle(
        my_ids, market.ir_after(me, my_ids, gets), cap, "gai"
    )
    his_cap = market.capacity[his_id]
    _, his_base, _, _ = market.baseline(his_id, "espn")
    his_ids = [i for i in market.rosters[his_id] if i not in gets] + list(sends)
    his_after, his_value, his_signs, his_drops = market.settle(
        his_ids, market.ir_after(his_id, his_ids, sends), his_cap, "espn"
    )
    him_espn = his_value - his_base
    him_fp = market.value(his_after, "fp", frozenset(his_signs)) - market.value(
        market.rosters[his_id], "fp"
    )
    return Priced(
        his_id=his_id,
        sends=list(sends),
        gets=list(gets),
        me=my_value - base,
        me_week=market.week_points(my_after) - market.week_points(my_before),
        him_espn=him_espn,
        him_fp=him_fp,
        accept=market.accept(list(sends), list(gets), him_espn, him_fp),
        my_before=my_before,
        my_after=my_after,
        my_signs=my_signs,
        my_drops=my_drops,
        his_signs=his_signs,
        his_drops=his_drops,
        my_used=frozenset(my_signs),
    )


def sweep(market: Market, his_id: int) -> list[Priced]:
    """Every <= MAX_SIDE-for-<= MAX_SIDE package worth proposing, best expected value first."""
    mine = market.rosters[market.my_id]
    my_qbs = [i for i in mine if market.players[i].position == "QB"]
    his_pool = [
        i for i in market.rosters[his_id] if market.players[i].points_base >= 40
    ]
    out = []
    for k, m in itertools.product(range(1, MAX_SIDE + 1), repeat=2):
        for sends in itertools.combinations(mine, k):
            sells_qb = set(my_qbs) <= set(sends)
            for gets in itertools.combinations(his_pool, m):
                if sells_qb and not any(
                    market.players[i].position == "QB" for i in gets
                ):
                    continue  # see the module docstring: QB sales are not trusted
                priced = price(market, his_id, list(sends), list(gets))
                if priced.me < MIN_SWEEP_GAIN:
                    continue
                # A player either side cuts at once is a throw-in; the package without him
                # is the same trade.
                if set(gets) & set(priced.my_drops) or set(sends) & set(
                    priced.his_drops
                ):
                    continue
                out.append(priced)
    out.sort(key=lambda t: -t.ev)
    return out


# --- report -----------------------------------------------------------------------


def tag(market: Market, i: int) -> str:
    p = market.players[i]
    hit = market.fp_rank[i]
    fp = f"{p.position}{hit[0]}" if hit else "unranked"
    return (
        f"{p.name} ({p.position} {p.team or 'FA'}: gai {p.points_base:.0f}, "
        f"espn {market.espn_ros[i]:.0f}, fp {fp})"
    )


def lineup_line(market: Market, ids: list[int]) -> str:
    """The starting lineup by GridironAI median, greedy over the slot chain."""
    open_slots = dict(league.STARTING_SLOTS)
    slots: dict[str, list[int]] = collections.defaultdict(list)
    for i in sorted(ids, key=lambda i: -market.players[i].points_base):
        slot = next(
            (s for s in league.SLOT_CHAIN[market.players[i].position] if open_slots[s]),
            "BE",
        )
        if slot != "BE":
            open_slots[slot] -= 1
        slots[slot].append(i)

    def group(slot: str) -> str:
        return " / ".join(
            f"{market.players[i].name} {market.players[i].points_base:.0f}"
            for i in slots[slot]
        )

    return " | ".join(
        f"{slot} {group(slot)}" for slot in ("QB", "RB", "WR", "TE", "FLEX", "BE")
    )


def slot_groups(market: Market, ids: list[int], used: frozenset) -> dict[str, float]:
    """The lineup value split into each position's dedicated slots and the flex pair."""
    priced = market.lens["gai"]
    wire = market.wire("gai", used)
    groups = {
        pos: position_expected_value(
            [priced[i] for i in ids if priced[i].position == pos],
            wire[pos],
            league.DEDICATED_SLOTS[pos],
        )
        for pos in POSITIONS
    }
    groups["FLEX"] = market.value(ids, "gai", used) - sum(groups.values())
    return groups


def names(market: Market, ids: list[int]) -> str:
    return ", ".join(market.players[i].name for i in ids)


def moves(market: Market, t: Priced) -> str:
    parts = []
    if t.my_drops:
        parts.append(f"I drop {names(market, t.my_drops)}")
    if t.my_signs:
        parts.append(f"I sign {names(market, t.my_signs)}")
    if t.his_drops:
        parts.append(f"they drop {names(market, t.his_drops)}")
    if t.his_signs:
        parts.append(f"they sign {names(market, t.his_signs)}")
    return "; ".join(parts) or "none"


def team_line(market: Market, team_id: int) -> str:
    team = market.teams[team_id]
    return (
        f"{team['name']} ({team['owner']}, {team['wins']}-{team['losses']}, "
        f"{team['trades']} trades made)"
    )


def report_trade(market: Market, t: Priced) -> None:
    print(
        f"\nTrade with {team_line(market, t.his_id)}, week {market.week}, {market.weeks_left} weeks left"
    )
    print("  send: " + "; ".join(tag(market, i) for i in t.sends))
    print("  get:  " + "; ".join(tag(market, i) for i in t.gets))
    print(
        f"\nMe (GridironAI): {t.me:+.1f} rest-of-season lineup points "
        f"({t.me / market.weeks_left:+.2f}/wk), week {market.week} lineup {t.me_week:+.1f}"
    )
    print(
        "  before: "
        + lineup_line(
            market, [i for i in t.my_before if i not in market.ir[market.my_id]]
        )
    )
    print(
        "  after:  "
        + lineup_line(
            market, [i for i in t.my_after if i not in market.ir[market.my_id]]
        )
    )
    before = slot_groups(market, t.my_before, frozenset())
    after = slot_groups(market, t.my_after, t.my_used)
    print(
        "  by slot group: "
        + "  ".join(f"{k} {after[k] - before[k]:+.1f}" for k in after)
    )
    print(
        f"\nThem: ESPN {t.him_espn:+.1f}, FantasyPros {t.him_fp:+.1f} -> "
        f"accept ~{t.accept:.0%} (heuristic)"
    )
    print(f"  then: {moves(market, t)}")


def report_sweep(market: Market, his_id: int, results: list[Priced]) -> None:
    print(
        f"\n{team_line(market, his_id)}: {len(results)} packages gain me >= {MIN_SWEEP_GAIN:.0f}"
    )
    seen: set[frozenset] = set()
    for t in results:
        key = frozenset(t.gets)
        if key in seen:
            continue  # one package per set of targets
        seen.add(key)
        print(
            f"  {t.me:+6.1f} ROS ({t.me / market.weeks_left:+.2f}/wk, wk{market.week} {t.me_week:+5.1f})"
            f" | them espn {t.him_espn:+6.1f} fp {t.him_fp:+6.1f} | accept {t.accept:4.0%} | EV {t.ev:+5.1f}"
        )
        print(f"      send {names(market, t.sends)}  for  {names(market, t.gets)}")
        if len(seen) >= SWEEP_ROWS:
            break


def report_baseline(market: Market) -> None:
    _, _, signs, drops = market.baseline(market.my_id, "gai")
    if drops:
        swaps = "; ".join(
            f"drop {market.players[d].name} for {market.players[s].name}"
            for d, s in zip(drops, signs)
        )
        print(f"\nFree moves already netted out of every gain below: {swaps}")


# --- CLI --------------------------------------------------------------------------


def resolve_team(market: Market, text: str) -> int:
    if text.isdigit() and int(text) in market.teams:
        return int(text)
    hits = [
        team_id
        for team_id, team in market.teams.items()
        if team_id != market.my_id
        and (
            text.lower() in (team["name"] or "").lower()
            or text.lower() == (team["abbrev"] or "").lower()
        )
    ]
    if len(hits) != 1:
        found = ", ".join(market.teams[t]["name"] for t in hits) or "nothing"
        raise SystemExit(f"error: team {text!r} matches {found}")
    return hits[0]


def resolve_player(market: Market, team_id: int, text: str) -> int:
    hits = [
        i
        for i in market.rosters[team_id]
        if text.lower() in market.players[i].name.lower()
    ]
    if len(hits) != 1:
        found = ", ".join(market.players[i].name for i in hits) or "nobody"
        raise SystemExit(
            f"error: {text!r} on {market.teams[team_id]['name']} matches {found}"
        )
    return hits[0]


_MARKET: Market | None = None


def _sweep_worker(his_id: int) -> tuple[int, list[Priced]]:
    assert _MARKET is not None
    return his_id, sweep(_MARKET, his_id)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "team", nargs="?", help="opponent: ESPN team id or part of the name"
    )
    parser.add_argument(
        "--send", action="append", default=[], metavar="PLAYER", help="my player"
    )
    parser.add_argument(
        "--get", action="append", default=[], metavar="PLAYER", help="their player"
    )
    args = parser.parse_args()
    if bool(args.send) != bool(args.get) or (args.send and not args.team):
        parser.error("a trade needs a team, --send and --get; omit all three to sweep")

    fetches = [
        ("league", "league_pipeline/fetch_league.py"),
        ("fantasypros", "league_pipeline/fetch_fantasypros.py"),
    ]
    for number, (name, script) in enumerate(fetches, start=1):
        print(f"\n=== [{number}/3] {name} ===", file=sys.stderr)
        code = subprocess.run(["uv", "run", script], cwd=REPO_ROOT).returncode
        if code != 0:
            print(f"trade failed at step '{name}' (exit {code})", file=sys.stderr)
            return code

    print("\n=== [3/3] price ===", file=sys.stderr)
    document = json.loads(LEAGUE.read_text(encoding="utf-8"))
    season, week = document["season"], document["scoring_period"]
    weekly_csv = REPO_ROOT / f"gridironai-rankings-{season}-{week}.csv"
    ros_csv = (
        REPO_ROOT / f"gridironai-rankings-{season}-rest-of-season-from-week-{week}.csv"
    )
    missing = [path.name for path in (weekly_csv, ros_csv) if not path.is_file()]
    if missing:
        print(
            f"error: missing {', '.join(missing)} — run weekly.py first",
            file=sys.stderr,
        )
        return 1
    try:
        market = load_market(
            document,
            weekly_csv,
            ros_csv,
            json.loads(FANTASYPROS.read_text(encoding="utf-8")),
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    report_baseline(market)
    if args.send:
        his_id = resolve_team(market, args.team)
        sends = [resolve_player(market, market.my_id, text) for text in args.send]
        gets = [resolve_player(market, his_id, text) for text in args.get]
        report_trade(market, price(market, his_id, sends, gets))
    elif args.team:
        his_id = resolve_team(market, args.team)
        report_sweep(market, his_id, sweep(market, his_id))
    else:
        global _MARKET
        _MARKET = market
        opponents = [t for t in market.teams if t != market.my_id]
        with multiprocessing.get_context("fork").Pool() as pool:
            results = dict(pool.map(_sweep_worker, opponents))
        for his_id in opponents:
            report_sweep(market, his_id, results[his_id])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
