#!/usr/bin/env python3
"""Fetch the ESPN league's in-season state and publish it as league.json.

Two requests to ESPN's undocumented v3 league API answer everything the weekly optimizer
needs. The league endpoint with the `mSettings`, `mTeam`, `mRoster` and `mStatus` views
gives the lineup shape, position caps, waiver settings and order, the current scoring
period, and my roster with its lineup slots. The `kona_player_info` view, filtered to
free agents and waiver players at QB/RB/WR/TE/D/ST, gives everyone I could add.

Every player row keeps ESPN's own league-scored projections: `espn_week` for the current
scoring period and `espn_ros`, ESPN's in-season season-split projection, which covers the
remaining weeks through week 18 (a healthy player at week 3 carries 15 games: weeks 3-18
minus his bye). The optimizer prices players from GridironAI and reads ESPN's numbers only
where GridironAI has none (rest-of-season D/ST).

The league id, season, and auth cookies are draft_pipeline/fetch_draft.py's; refresh the
cookies there when ESPN answers 401/403.

Usage:
    uv run league_pipeline/fetch_league.py
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = REPO_ROOT / "league.json"

# One copy of the league id and cookies: they expire on ESPN's schedule.
sys.path.insert(0, str(REPO_ROOT / "draft_pipeline"))
from fetch_draft import (  # noqa: E402
    API,
    ESPN_S2,
    ESPN_TEAM_ABBREV,
    LEAGUE_ID,
    MY_SWID,
    SEASON,
    TIMEOUT_SECONDS,
)

LEAGUE_VIEWS = ("mSettings", "mTeam", "mRoster", "mStatus")

#: ESPN defaultPositionId -> position. The league has no kicker slot.
POSITIONS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 16: "D/ST"}

#: ESPN lineup slot id -> slot, for every slot this league uses.
SLOTS = {0: "QB", 2: "RB", 4: "WR", 6: "TE", 23: "FLEX", 16: "D/ST", 20: "BE", 21: "IR"}

#: The player-pool filter's slot ids: QB, RB, WR, TE, D/ST.
POOL_SLOT_IDS = [0, 2, 4, 6, 16]

#: The whole pool is ~850 players, so a full page means truncation.
POOL_LIMIT = 2000


def get(url: str, player_filter: dict | None = None) -> dict:
    headers = {
        "Accept": "application/json",
        "Cookie": f"espn_s2={ESPN_S2}; SWID={MY_SWID}",
        # ESPN's edge rejects urllib's default agent.
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
    }
    if player_filter is not None:
        headers["x-fantasy-filter"] = json.dumps(player_filter)
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read())


def projection(player: dict, period: int) -> float | None:
    """ESPN's league-scored projection: a week when period > 0, the rest of season at 0."""
    split = 1 if period else 0
    for stat in player.get("stats") or []:
        if (
            stat["statSourceId"] == 1
            and stat["seasonId"] == SEASON
            and stat["scoringPeriodId"] == period
            and stat["statSplitTypeId"] == split
        ):
            return round(stat["appliedTotal"], 2)
    return None


def player_row(player: dict, period: int, **fields) -> dict:
    if player["defaultPositionId"] not in POSITIONS:
        raise ValueError(
            f"{player['fullName']} has ESPN position {player['defaultPositionId']}, "
            "which no lineup slot here takes"
        )
    return {
        "espn_id": player["id"],
        "name": player["fullName"],
        "position": POSITIONS[player["defaultPositionId"]],
        "team": ESPN_TEAM_ABBREV.get(player["proTeamId"]),
        "injury_status": player.get("injuryStatus"),
        **fields,
        "espn_week": projection(player, period),
        "espn_ros": projection(player, 0),
    }


def build(league: dict, pool: dict) -> dict:
    settings = league["settings"]
    roster_settings = settings["rosterSettings"]
    acquisition = settings["acquisitionSettings"]
    period = league["scoringPeriodId"]

    slots = {}
    for slot_id, count in roster_settings["lineupSlotCounts"].items():
        if not count:
            continue
        if int(slot_id) not in SLOTS:
            raise ValueError(f"the league uses ESPN lineup slot {slot_id}, which is not modeled")
        slots[SLOTS[int(slot_id)]] = count
    limits = {
        pos: (None if limit < 0 else limit)
        for pos_id, pos in POSITIONS.items()
        for limit in [roster_settings["positionLimits"][str(pos_id)]]
    }

    mine = next((t for t in league["teams"] if MY_SWID in (t.get("owners") or [])), None)
    if mine is None:
        raise ValueError(f"no team in league {LEAGUE_ID} is owned by {MY_SWID}")

    players = []
    for entry in mine["roster"]["entries"]:
        pool_entry = entry["playerPoolEntry"]
        players.append(
            player_row(
                pool_entry["player"],
                period,
                status="MINE",
                lineup_slot=SLOTS[entry["lineupSlotId"]],
                locked=pool_entry["lineupLocked"],
                droppable=pool_entry["player"]["droppable"],
                waivers_clear_at=None,
            )
        )
    for pool_entry in pool["players"]:
        clears = pool_entry.get("waiverProcessDate")
        players.append(
            player_row(
                pool_entry["player"],
                period,
                status=pool_entry["status"],
                lineup_slot=None,
                locked=pool_entry["lineupLocked"],
                droppable=None,
                waivers_clear_at=(
                    dt.datetime.fromtimestamp(clears / 1000, dt.timezone.utc).isoformat()
                    if pool_entry["status"] == "WAIVERS" and clears
                    else None
                ),
            )
        )

    return {
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "league_id": LEAGUE_ID,
        "league_name": settings.get("name"),
        "season": SEASON,
        "scoring_period": period,
        "final_scoring_period": league["status"]["finalScoringPeriod"],
        "me": {"team_id": mine["id"], "team_name": mine.get("name")},
        "waivers": {
            "type": acquisition["acquisitionType"],
            "uses_budget": acquisition["isUsingAcquisitionBudget"],
            "my_rank": mine["waiverRank"],
            "teams": len(league["teams"]),
        },
        "lineup_slots": slots,
        "position_limits": limits,
        "players": players,
    }


def main() -> int:
    league_url = (
        f"{API}/seasons/{SEASON}/segments/0/leagues/{LEAGUE_ID}?"
        + "&".join(f"view={view}" for view in LEAGUE_VIEWS)
    )
    print(f"GET {league_url}", file=sys.stderr)
    try:
        league = get(league_url)
        # The period parameter makes the pool carry that week's projections.
        pool = get(
            f"{API}/seasons/{SEASON}/segments/0/leagues/{LEAGUE_ID}"
            f"?scoringPeriodId={league['scoringPeriodId']}&view=kona_player_info",
            {
                "players": {
                    "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
                    "filterSlotIds": {"value": POOL_SLOT_IDS},
                    "limit": POOL_LIMIT,
                    # ESPN answers 400 to a player filter without a sort.
                    "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
                }
            },
        )
    except urllib.error.HTTPError as exc:
        hint = ""
        if exc.code in (401, 403):
            hint = " — cookies expired? refresh them in draft_pipeline/fetch_draft.py"
        print(f"error: ESPN answered {exc.code} {exc.reason}{hint}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"error: request failed: {exc}", file=sys.stderr)
        return 1

    if len(pool["players"]) >= POOL_LIMIT:
        print(
            f"error: the player pool filled a {POOL_LIMIT}-player page — truncated",
            file=sys.stderr,
        )
        return 1
    try:
        document = build(league, pool)
    except (KeyError, TypeError, ValueError) as exc:
        print(f"error: league {LEAGUE_ID} is not shaped as expected: {exc!r}", file=sys.stderr)
        return 1

    with OUTPUT.open("w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    rows = document["players"]
    waivers = document["waivers"]
    print(
        f"league: week {document['scoring_period']}, "
        f"{sum(r['status'] == 'MINE' for r in rows)} rostered, "
        f"{sum(r['status'] == 'FREEAGENT' for r in rows)} free agents, "
        f"{sum(r['status'] == 'WAIVERS' for r in rows)} on waivers; "
        f"waiver rank {waivers['my_rank']} of {waivers['teams']} "
        f"-> {OUTPUT.relative_to(REPO_ROOT)}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
