#!/usr/bin/env python3
"""Build sectional standings from MaxPreps schedules.

Reads sectionals.yml, fetches the MaxPreps schedule page for every school,
and writes data/standings.json for index.html.

Seeding rules:
  - Only games between two schools in the same sectional count.
  - Only the first meeting between two schools counts. A game without a
    final score is skipped, so the next meeting with a score counts instead.
  - Games on or after the optional `cutoff` date do not count.
  - Sort: sectional win %, sectional wins, overall win %, name.

The bracket seeds come from the standings. Games on or after `cutoff`
fill in the bracket results.

Usage: python scripts/build_standings.py [--config sectionals.yml] [--out data/standings.json]
"""

import argparse
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130 Safari/537.36"
)
LOCAL_TZ = ZoneInfo("America/Indiana/Indianapolis")
GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

# MaxPreps sends schedule contests as positional arrays, not objects.
# These indexes were mapped from the 25-26 pages. validate_contest() fails
# loudly if the layout changes, so the workflow keeps the last good file.
C_TEAMS = 0  # list of two team entries
C_ID = 1  # contest GUID
C_DATE = 11  # local start time, "2026-04-03T18:00:00"
C_HAS_RESULT = 17  # bool
C_URL = 18  # game page

T_TEAM_ID = 1  # school GUID
T_RESULT = 5  # "W" / "L" / "T" / None
T_SCORE = 6  # int / None
T_HOME_AWAY = 11  # 0 home, 1 away, 2 neutral
T_NAME = 14


class LayoutError(Exception):
    pass


def fetch(url, attempts=3):
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8")
        except Exception as exc:  # noqa: BLE001
            if attempt == attempts:
                raise RuntimeError(f"fetch failed for {url}: {exc}") from exc
            time.sleep(2 * attempt)


def page_props(html, url):
    m = NEXT_DATA_RE.search(html)
    if not m:
        raise LayoutError(f"no __NEXT_DATA__ on {url}")
    return json.loads(m.group(1))["props"]["pageProps"]


def validate_contest(c, url):
    ok = (
        isinstance(c, list)
        and len(c) > C_URL
        and isinstance(c[C_ID], str)
        and GUID_RE.match(c[C_ID])
        and isinstance(c[C_DATE], str)
        and isinstance(c[C_HAS_RESULT], bool)
        and (c[C_TEAMS] is None or isinstance(c[C_TEAMS], list))
    )
    for t in (c[C_TEAMS] or []) if ok else []:
        ok = ok and isinstance(t, list) and len(t) > T_NAME
        ok = ok and t[T_RESULT] in (None, "W", "L", "T")
        ok = ok and (t[T_SCORE] is None or isinstance(t[T_SCORE], int))
        ok = ok and t[T_HOME_AWAY] in (0, 1, 2)
    if not ok:
        raise LayoutError(f"unexpected contest layout on {url}: {str(c)[:200]}")


def schedule_url(school_url, sport, season):
    return f"{school_url.rstrip('/')}/{sport.strip('/')}/{season}/schedule/"


def load_school(school, sport, season):
    url = schedule_url(school["maxpreps"], sport, season)
    props = page_props(fetch(url), url)
    team = props["teamContext"]["data"]
    if team.get("year") != season:
        raise LayoutError(f"{url} returned season {team.get('year')}, expected {season}")
    contests = props.get("contests") or []
    for c in contests:
        validate_contest(c, url)
    return {"id": team["teamId"], "url": url, "contests": contests}


def overall_record(team_id, contests):
    rec = {"w": 0, "l": 0, "t": 0}
    for c in contests:
        if not c[C_HAS_RESULT]:
            continue
        for t in c[C_TEAMS] or []:
            if t[T_TEAM_ID] == team_id and t[T_RESULT]:
                rec[t[T_RESULT].lower()] += 1
    return rec


def pct(rec):
    gp = rec["w"] + rec["l"] + rec["t"]
    return (rec["w"] + 0.5 * rec["t"]) / gp if gp else 0.0


def build_sectional(sec, schools, cutoff, today):
    ids = {schools[s["name"]]["id"]: s["name"] for s in sec["schools"]}

    # Merge every school's page. A game shows on both teams' pages, so key
    # by contest ID and prefer the copy that has a result.
    merged = {}
    for s in sec["schools"]:
        for c in schools[s["name"]]["contests"]:
            teams = c[C_TEAMS] or []
            if len(teams) != 2 or not all(t[T_TEAM_ID] in ids for t in teams):
                continue
            prev = merged.get(c[C_ID])
            if prev is None or (c[C_HAS_RESULT] and not prev[C_HAS_RESULT]):
                merged[c[C_ID]] = c

    games = []
    for c in sorted(merged.values(), key=lambda c: c[C_DATE]):
        a, b = c[C_TEAMS]
        home, away = (b, a) if a[T_HOME_AWAY] == 1 else (a, b)
        played = c[C_HAS_RESULT] and a[T_SCORE] is not None and b[T_SCORE] is not None
        if played:
            status = "final"
        elif c[C_DATE][:10] < today:
            status = "unreported"
        else:
            status = "scheduled"
        games.append({
            "date": c[C_DATE],
            "home": ids[home[T_TEAM_ID]],
            "away": ids[away[T_TEAM_ID]],
            "neutral": a[T_HOME_AWAY] == 2,
            "home_score": home[T_SCORE] if played else None,
            "away_score": away[T_SCORE] if played else None,
            "home_result": home[T_RESULT] if played else None,
            "status": status,
            "counts": False,
            "note": "",
            "url": c[C_URL],
        })

    # Each team can enter the same game, so one game can show up twice with
    # different contest IDs. MaxPreps also uses 1900/1901 dates for undated
    # games. Drop copies without a score when a scored copy or an earlier
    # copy exists on that date, and drop undated games without a score.
    finals = {(frozenset((g["home"], g["away"])), g["date"][:10]) for g in games if g["status"] == "final"}
    kept, seen = [], set()
    for g in games:
        key = (frozenset((g["home"], g["away"])), g["date"][:10])
        if g["status"] != "final" and (key in finals or key in seen or g["date"] < "2000"):
            continue
        seen.add(key)
        kept.append(g)
    games = kept

    counted_pairs = set()
    for g in games:
        pair = frozenset((g["home"], g["away"]))
        if cutoff and g["date"][:10] >= cutoff:
            g["note"] = "tournament"
        elif g["status"] != "final":
            g["note"] = "no score yet" if g["status"] == "unreported" else ""
        elif pair in counted_pairs:
            g["note"] = "repeat meeting"
        else:
            counted_pairs.add(pair)
            g["counts"] = True
            g["note"] = "first meeting"

    rows = {}
    for s in sec["schools"]:
        info = schools[s["name"]]
        rows[s["name"]] = {
            "name": s["name"],
            "maxpreps": info["url"],
            "overall": overall_record(info["id"], info["contests"]),
            "sectional": {"w": 0, "l": 0, "t": 0},
            "wins_over": [],
        }
    for g in games:
        if not g["counts"]:
            continue
        h, a = rows[g["home"]], rows[g["away"]]
        hs, as_ = g["home_score"], g["away_score"]
        if g["home_result"] == "T":
            h["sectional"]["t"] += 1
            a["sectional"]["t"] += 1
            continue
        winner, loser = (h, a) if g["home_result"] == "W" else (a, h)
        winner["sectional"]["w"] += 1
        loser["sectional"]["l"] += 1
        winner["wins_over"].append({
            "opponent": loser["name"],
            "score": f"{max(hs, as_)}-{min(hs, as_)}",
            "date": g["date"][:10],
        })

    standings = sorted(
        rows.values(),
        key=lambda r: (-pct(r["sectional"]), -r["sectional"]["w"], -pct(r["overall"]), r["name"]),
    )
    for i, r in enumerate(standings, 1):
        r["seed"] = i
    bracket = build_bracket([r["name"] for r in standings], games, cutoff)
    for g in games:
        del g["home_result"]

    return {
        "name": sec["name"],
        "host": sec.get("host"),
        "standings": standings,
        "bracket": bracket,
        "games": games,
    }


def seed_order(size):
    """Standard bracket order, e.g. size 8 -> [1, 8, 4, 5, 2, 7, 3, 6]."""
    order = [1]
    while len(order) < size:
        n = len(order) * 2 + 1
        order = [x for s in order for x in (s, n - s)]
    return order


def build_bracket(seeded, games, cutoff):
    """Single elimination with byes for the top seeds.

    Tournament results come from final games on or after the cutoff date.
    If a game has no score but one team shows up in the next round, that
    team advanced, and the earlier match is filled in without a score.

    A slot is a team ({"seed", "name"}), None for a bye (first round only),
    or {"seed": None, "name": None} when the team is not decided yet.
    """
    size = 1
    while size < len(seeded):
        size *= 2
    tourney = [g for g in games if cutoff and g["date"][:10] >= cutoff and g["status"] == "final"]
    teams = {name: {"seed": i, "name": name} for i, name in enumerate(seeded, 1)}
    tbd = {"seed": None, "name": None}

    def find_game(top, bottom):
        for g in tourney:
            for x, y in ((g["home"], g["away"]), (g["away"], g["home"])):
                if x in top and y in bottom:
                    return g, x, y
        return None

    # Each slot keeps the names that could fill it and the match it came from.
    slots = [(teams.get(seeded[s - 1]) if s <= len(seeded) else None, None) for s in seed_order(size)]
    rounds = []
    while len(slots) > 1:
        matches = []
        next_slots = []
        for (a, a_info), (b, b_info) in zip(slots[::2], slots[1::2]):
            m = {"top": a, "bottom": b, "winner": None, "score": None, "date": None, "bye": False}
            matches.append(m)
            if a is None or b is None:
                m["bye"] = True
                m["winner"] = a or b
                next_slots.append((m["winner"], None))
                continue
            a_names = a_info["names"] if a_info else {a["name"]}
            b_names = b_info["names"] if b_info else {b["name"]}
            found = find_game(a_names, b_names)
            if not found:
                m["winner"] = tbd
                next_slots.append((tbd, {"names": a_names | b_names, "match": m}))
                continue
            game, a_name, b_name = found
            # Fill in earlier matches whose scores were not reported.
            for name, info, side in ((a_name, a_info, "top"), (b_name, b_info, "bottom")):
                if info:
                    m[side] = teams[name]
                    info["match"]["winner"] = teams[name]
            home_won = game["home_result"] == "W"
            m["winner"] = teams[game["home"] if home_won else game["away"]]
            hs, as_ = game["home_score"], game["away_score"]
            m["score"] = f"{max(hs, as_)}-{min(hs, as_)}"
            m["date"] = game["date"][:10]
            next_slots.append((m["winner"], None))
        rounds.append(matches)
        slots = next_slots
    return {"size": size, "rounds": rounds, "champion": slots[0][0] if slots else None}


def main():
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=root / "sectionals.yml", type=Path)
    ap.add_argument("--out", default=root / "data" / "standings.json", type=Path)
    args = ap.parse_args()

    cfg = yaml.safe_load(args.config.read_text())
    season = str(cfg["season"])
    sport = cfg.get("sport", "lacrosse/girls")
    cutoff = str(cfg["cutoff"]) if cfg.get("cutoff") else None
    today = datetime.now(LOCAL_TZ).date().isoformat()

    schools = {}
    for sec in cfg["sectionals"]:
        for s in sec["schools"]:
            print(f"fetching {s['name']}", file=sys.stderr)
            schools[s["name"]] = load_school(s, sport, season)
            time.sleep(1)

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "season": season,
        "cutoff": cutoff,
        "sectionals": [build_sectional(sec, schools, cutoff, today) for sec in cfg["sectionals"]],
    }

    # Keep the old file when only the timestamp would change, so the daily
    # job commits only when MaxPreps data changes.
    if args.out.exists():
        old = json.loads(args.out.read_text())
        if {**old, "generated_at": None} == {**out, "generated_at": None}:
            print("no changes", file=sys.stderr)
            return

    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=2) + "\n")
    tmp.replace(args.out)
    print(f"wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
