#!/usr/bin/env python3
"""Download this week's and the rest-of-season GridironAI rankings CSVs for my team.

GridironAI is an Angular app over a Django API, so no browser is needed. Logging in is
one JSON POST to /customer/login/ that sets a session cookie, and the rankings page's
"Download Data" button is a GET of the team's player-rankings endpoint with
`response_type=csv` (this week) or `response_type=csv&board=ros` (rest of season). The
export carries every position and column whatever the page's `pos=`/`cols=` view state,
and the site names the file itself (Content-Disposition):

    gridironai-rankings-<season>-<week>.csv
    gridironai-rankings-<season>-rest-of-season-from-week-<week>.csv

Those are the names weekly.py reads, so both land at the repo root as-is. The week is
GridironAI's current week, which is normally ESPN's; when they disagree (GridironAI rolls
over after ESPN does) weekly.py reports the week it could not find.

Team 2568 is this league's team on GridironAI, with the league's scoring configured there.
The password is GRIDIRON_PASSWORD, read from the environment or the git-ignored repo-root
.env; when neither has it the script prompts for it.

Usage:
    uv run league_pipeline/fetch_gridiron.py
"""

from __future__ import annotations

import getpass
import http.cookiejar
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"

SITE = "https://gridironai.com"
EMAIL = "johnmcgue94@gmail.com"
TEAM_ID = 2568

TIMEOUT_SECONDS = 60

#: Query per board; the site's own file name says which is which.
BOARDS = ("response_type=csv", "response_type=csv&board=ros")

FILENAME = re.compile(r'filename="(gridironai-rankings-\d{4}-[a-z0-9-]+\.csv)"')


def password() -> str:
    """GRIDIRON_PASSWORD from the environment, else the repo-root .env, else a prompt."""
    value = os.environ.get("GRIDIRON_PASSWORD")
    if not value and ENV_FILE.is_file():
        # .env is bare KEY=value lines, no quoting or comments, so `#` and `%` pass through.
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            key, _, rest = line.partition("=")
            if key.strip() == "GRIDIRON_PASSWORD":
                value = rest.strip()
    return value or getpass.getpass("GridironAI password: ")


def login(opener: urllib.request.OpenerDirector, secret: str) -> None:
    body = json.dumps({"username": EMAIL, "password": secret}).encode()
    request = urllib.request.Request(
        f"{SITE}/customer/login/",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
        answer = json.loads(response.read())
    if not answer.get("success"):
        raise ValueError(f"GridironAI refused the login: {answer.get('errors')}")


def download(opener: urllib.request.OpenerDirector, query: str) -> Path:
    url = f"{SITE}/app/fantasy-team/{TEAM_ID}/player-rankings/?{query}"
    print(f"GET {url}", file=sys.stderr)
    with opener.open(url, timeout=TIMEOUT_SECONDS) as response:
        content_type = response.headers.get("Content-Type", "")
        disposition = response.headers.get("Content-Disposition", "")
        data = response.read()
    named = FILENAME.search(disposition)
    if not content_type.startswith("text/csv") or named is None:
        raise ValueError(
            f"expected a CSV export, got {content_type!r} with disposition {disposition!r}"
        )
    path = REPO_ROOT / named.group(1)
    path.write_bytes(data)
    rows = data.count(b"\n") - 1
    print(f"gridiron: {rows} rows -> {path.relative_to(REPO_ROOT)}", file=sys.stderr)
    return path


def main() -> int:
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    opener.addheaders = [("User-Agent", "Mozilla/5.0 (X11; Linux x86_64)")]
    try:
        login(opener, password())
        for query in BOARDS:
            download(opener, query)
    except urllib.error.HTTPError as exc:
        print(f"error: GridironAI answered {exc.code} {exc.reason}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
