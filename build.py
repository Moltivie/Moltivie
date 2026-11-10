#!/usr/bin/env python3
"""Render the neofetch-style profile card (dark_mode.svg / light_mode.svg).

Content comes from profile.toml, the portrait from ascii.txt, and the numbers
from the GitHub GraphQL API. Standard library only.

    python3 build.py              fetch stats, then render
    python3 build.py --no-fetch   render from cache/stats.json

Token: $ACCESS_TOKEN, else $GITHUB_TOKEN, else `gh auth token`.
A token that can read private repos includes them in the totals.
"""

import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"
STATS_FILE = CACHE / "stats.json"
LOC_FILE = CACHE / "loc.json"

# ---------------------------------------------------------------- layout ----

PAD = 15
FONT = 16  # info column
CW = 9.6  # advance of one 16px monospace cell
LH = 20
COLS = 60  # info column width, in cells

ART_FONT = 10  # the portrait is set smaller so it carries more detail
ART_CW = 6.0
ART_LH = 12.5

THEMES = {
    "dark_mode.svg": {
        "bg": "#161b22", "text": "#c9d1d9", "key": "#ffa657", "value": "#a5d6ff",
        "dim": "#616e7f", "add": "#3fb950", "del": "#f85149",
    },
    "light_mode.svg": {
        "bg": "#f6f8fa", "text": "#24292f", "key": "#953800", "value": "#0a3069",
        "dim": "#8c959f", "add": "#1a7f37", "del": "#cf222e",
    },
}

# ----------------------------------------------------------------- stats ----


def token():
    for name in ("ACCESS_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
    try:
        return subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        sys.exit("No token: set ACCESS_TOKEN / GITHUB_TOKEN or log in with gh.")


def gql(tok, query, **variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    for attempt in range(5):
        req = urllib.request.Request(
            "https://api.github.com/graphql",
            data=body,
            headers={"Authorization": f"bearer {tok}", "User-Agent": "profile-card"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as err:
            if err.code in (502, 503, 504, 403, 429) and attempt < 4:
                time.sleep(5 * (attempt + 1))
                continue
            raise
        if payload.get("data") is None:
            raise RuntimeError(f"GraphQL error: {payload.get('errors')}")
        return payload["data"]
    raise RuntimeError("GraphQL: retries exhausted")


REPOS_Q = """
query($login: String!, $aff: [RepositoryAffiliation], $cursor: String) {
  user(login: $login) {
    repositories(first: 100, after: $cursor, ownerAffiliations: $aff) {
      pageInfo { hasNextPage endCursor }
      nodes { nameWithOwner stargazerCount pushedAt owner { login } }
    }
  }
}"""

USER_Q = """
query($login: String!) {
  user(login: $login) { id createdAt followers { totalCount } }
}"""

LOC_Q = """
query($owner: String!, $name: String!, $id: ID!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { target { ... on Commit {
      history(first: 100, after: $cursor, author: {id: $id}) {
        pageInfo { hasNextPage endCursor }
        nodes { additions deletions }
      }
    } } }
  }
}"""


def all_repos(tok, login):
    repos, cursor = [], None
    while True:
        page = gql(
            tok, REPOS_Q, login=login, cursor=cursor,
            aff=["OWNER", "COLLABORATOR", "ORGANIZATION_MEMBER"],
        )["user"]["repositories"]
        repos += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return repos
        cursor = page["pageInfo"]["endCursor"]


def repo_loc(tok, full_name, user_id):
    """The user's commits on the default branch, with lines added/removed."""
    owner, name = full_name.split("/", 1)
    add = rem = commits = 0
    cursor = None
    while True:
        repo = gql(tok, LOC_Q, owner=owner, name=name, id=user_id, cursor=cursor)
        ref = (repo.get("repository") or {}).get("defaultBranchRef")
        if not ref:  # empty repository
            break
        hist = ref["target"]["history"]
        for node in hist["nodes"]:
            add += node["additions"]
            rem += node["deletions"]
            commits += 1
        if not hist["pageInfo"]["hasNextPage"]:
            break
        cursor = hist["pageInfo"]["endCursor"]
    return {"add": add, "del": rem, "commits": commits}


def fetch_stats(login):
    tok = token()
    user = gql(tok, USER_Q, login=login)["user"]
    repos = all_repos(tok, login)
    owned = [r for r in repos if r["owner"]["login"].lower() == login.lower()]

    # Per-repo line counts are cached and only recomputed after a push.
    # The cache is gitignored and its keys are hashed, so private repository
    # names stay out of the repo; only the totals in stats.json are committed.
    old = json.loads(LOC_FILE.read_text()) if LOC_FILE.exists() else {}
    new = {}
    for repo in repos:
        key = hashlib.sha256(repo["nameWithOwner"].encode()).hexdigest()[:16]
        hit = old.get(key)
        if hit and hit.get("pushed") == repo["pushedAt"]:
            new[key] = hit
            continue
        try:
            new[key] = {"pushed": repo["pushedAt"], **repo_loc(tok, repo["nameWithOwner"], user["id"])}
        except Exception as err:  # one bad repo must not sink the whole run
            print(f"  skipped a repository: {type(err).__name__}", file=sys.stderr)
            if hit:
                new[key] = hit
    added = sum(r["add"] for r in new.values())
    removed = sum(r["del"] for r in new.values())

    stats = {
        "repos": len(owned),
        "contributed": len(repos),
        "stars": sum(r["stargazerCount"] for r in owned),
        # Counted from the commits themselves: the contributions API only
        # reports public commits, which undercounts a mostly-private account.
        "commits": sum(r["commits"] for r in new.values()),
        "followers": user["followers"]["totalCount"],
        "loc_add": added,
        "loc_del": removed,
        "loc": added - removed,
    }
    CACHE.mkdir(exist_ok=True)
    LOC_FILE.write_text(json.dumps(new, indent=1, sort_keys=True) + "\n")
    STATS_FILE.write_text(json.dumps(stats, indent=1) + "\n")
    return stats


# ---------------------------------------------------------------- render ----


def uptime(since, today):
    years = today.year - since.year
    months = today.month - since.month
    days = today.day - since.day
    if days < 0:
        months -= 1
        prev_month_end = today.replace(day=1) - dt.timedelta(days=1)
        days += prev_month_end.day
    if months < 0:
        years -= 1
        months += 12

    def unit(n, word):
        return f"{n} {word}{'' if n == 1 else 's'}"

    return f"{unit(years, 'year')}, {unit(months, 'month')}, {unit(days, 'day')}"


def kv(key, value, width, value_cls="value"):
    """`Key: ....... value` padded with a dot leader to exactly `width` cells."""
    dots = width - len(key) - len(value) - 3
    if dots < 1:
        print(f"  warning: '{key}' is too long for the card", file=sys.stderr)
        return [(key, "key"), (": ", "text"), (value, value_cls)]
    return [(key, "key"), (":", "text"), (" " + "." * dots + " ", "dim"), (value, value_cls)]


def build_lines(cfg, stats, today):
    """Each line is ("rule", title) or a list of (text, colour class)."""
    lines = [("rule", cfg["title"], False)]
    since = cfg.get("uptime_since")
    for i, section in enumerate(cfg.get("section", [])):
        if not section.get("items"):
            continue
        if section.get("title"):
            lines.append(("rule", section["title"], True))
        for key, value in section["items"]:
            if since:
                value = value.replace("{uptime}", uptime(since, today))
            lines.append([(". ", "dim"), *kv(key, value, COLS - 2)])
        lines.append([(".", "dim")])

    n = lambda v: f"{v:,}"
    left, right = 31, COLS - 2 - 31 - 3
    lines.append(("rule", "GitHub Stats", True))
    repos = kv("Repos", n(stats["repos"]), left - len(f" {{Contributed: {n(stats['contributed'])}}}"))
    lines.append([
        (". ", "dim"), *repos, (" {", "text"), ("Contributed", "key"), (": ", "text"),
        (n(stats["contributed"]), "value"), ("}", "text"),
        (" | ", "text"), *kv("Stars", n(stats["stars"]), right),
    ])
    lines.append([
        (". ", "dim"), *kv("Commits", n(stats["commits"]), left),
        (" | ", "text"), *kv("Followers", n(stats["followers"]), right),
    ])
    tail = [
        (" ( ", "text"), (n(stats["loc_add"]) + "++", "add"), (", ", "text"),
        (n(stats["loc_del"]) + "--", "del"), (" )", "text"),
    ]
    tail_len = sum(len(t) for t, _ in tail)
    lines.append([
        (". ", "dim"), *kv("Lines of Code", n(stats["loc"]), COLS - 2 - tail_len), *tail,
    ])
    return lines


def render(theme, art, lines):
    art_w = max(len(row) for row in art) * ART_CW
    art_h = len(art) * ART_LH
    info_h = len(lines) * LH
    inner_h = max(art_h, info_h)
    info_x = PAD + art_w + PAD
    width = round(info_x + COLS * CW + PAD)
    height = round(inner_h + 2 * PAD)
    art_top = PAD + (inner_h - art_h) / 2
    info_top = PAD + (inner_h - info_h) / 2

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="t" xml:space="preserve">',
        '<title id="t">GitHub profile card</title>',
        "<style>",
        'text { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, '
        '"Liberation Mono", "DejaVu Sans Mono", monospace; white-space: pre; }',
        f".info {{ font-size: {FONT}px; }} .art {{ font-size: {ART_FONT}px; }}",
        *(f".{name} {{ fill: {theme[name]}; }}" for name in ("text", "key", "value", "dim", "add", "del")),
        f".rule {{ stroke: {theme['text']}; stroke-width: 1.5; }}",
        "</style>",
        f'<rect width="{width}" height="{height}" rx="15" fill="{theme["bg"]}"/>',
    ]

    # Every run is pinned to its grid cell (x + textLength), so the layout holds
    # whichever monospace font the viewer's system ends up supplying.
    def run(text, x, cell, cls):
        return (
            f'<tspan x="{x:.1f}" textLength="{len(text) * cell:.1f}" '
            f'class="{cls}">{escape(text)}</tspan>'
        )

    out.append('<g class="art" aria-hidden="true">')
    for i, row in enumerate(art):
        body = row.lstrip()
        if not body:
            continue
        x = PAD + (len(row) - len(body)) * ART_CW
        y = art_top + (i + 0.8) * ART_LH
        out.append(f'<text y="{y:.1f}">{run(body, x, ART_CW, "text")}</text>')
    out.append("</g>")

    out.append('<g class="info">')
    right_edge = info_x + COLS * CW
    for i, line in enumerate(lines):
        y = info_top + (i + 0.72) * LH
        if line[0] == "rule":
            _, title, dashed = line
            label = f"- {title} " if dashed else f"{title} "
            out.append(f'<text y="{y:.1f}">{run(label, info_x, CW, "text")}</text>')
            ly = y - FONT * 0.3
            x0 = info_x + len(label) * CW
            out.append(
                f'<path class="rule" d="M{x0:.1f} {ly:.1f}H{right_edge - 3.2 * CW:.1f}'
                f'M{right_edge - 2.4 * CW:.1f} {ly:.1f}h{0.5 * CW:.1f}'
                f'M{right_edge - 1.2 * CW:.1f} {ly:.1f}H{right_edge:.1f}"/>'
            )
            continue
        col, spans = 0, []
        for text, cls in line:
            spans.append(run(text, info_x + col * CW, CW, cls))
            col += len(text)
        out.append(f'<text y="{y:.1f}">{"".join(spans)}</text>')
    out.append("</g>")
    out.append("</svg>")
    return "\n".join(out) + "\n"


def main():
    cfg = tomllib.loads((ROOT / "profile.toml").read_text())
    art = (ROOT / "ascii.txt").read_text().rstrip("\n").split("\n")
    if "--no-fetch" in sys.argv:
        if not STATS_FILE.exists():
            sys.exit("cache/stats.json is missing: run once without --no-fetch.")
        stats = json.loads(STATS_FILE.read_text())
    else:
        stats = fetch_stats(cfg["username"])
    lines = build_lines(cfg, stats, dt.date.today())
    for filename, theme in THEMES.items():
        (ROOT / filename).write_text(render(theme, art, lines))
    print(json.dumps(stats))


if __name__ == "__main__":
    main()
