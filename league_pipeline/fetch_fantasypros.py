#!/usr/bin/env python3
"""Download FantasyPros' rest-of-season half-PPR expert consensus and publish fantasypros.json.

The rankings page embeds its table as `var ecrData = {...};` in a script tag, so one GET
with a browser user agent (the site rejects urllib's default) and a regex are enough; no
login. The object is written as-is. Its `players` rows carry `player_name`,
`player_team_id`, `player_position_id`, `pos_rank` ("WR22"), `rank_ecr` (overall) and
`rank_std`; `last_updated` and `total_experts` describe the consensus. Half PPR is this
league's scoring (README), so the URL is fixed; D/ST and K rows come along and are ignored
downstream.

Usage:
    uv run league_pipeline/fetch_fantasypros.py
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = REPO_ROOT / "fantasypros.json"

URL = "https://www.fantasypros.com/nfl/rankings/ros-half-point-ppr-overall.php"
TIMEOUT_SECONDS = 60
EMBEDDED = re.compile(r"var ecrData = (\{.*?\});\s*\n", re.S)


def main() -> int:
    request = urllib.request.Request(
        URL,
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
    )
    print(f"GET {URL}", file=sys.stderr)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            html = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        print(f"error: FantasyPros answered {exc.code} {exc.reason}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"error: request failed: {exc}", file=sys.stderr)
        return 1

    embedded = EMBEDDED.search(html)
    if embedded is None:
        print(
            "error: no `var ecrData = {...};` in the page; the markup changed",
            file=sys.stderr,
        )
        return 1
    data = json.loads(embedded.group(1))
    if not data.get("players"):
        print("error: ecrData has no players", file=sys.stderr)
        return 1

    with OUTPUT.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1, ensure_ascii=False)
        handle.write("\n")
    print(
        f"fantasypros: {len(data['players'])} players, {data.get('total_experts')} experts, "
        f"updated {data.get('last_updated')} -> {OUTPUT.relative_to(REPO_ROOT)}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
