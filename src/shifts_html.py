#!/usr/bin/env python3
"""
shifts_html.py — Fallback shift source: the NHL HTML TOI reports.

WHY THIS EXISTS
---------------
The JSON shiftcharts feed returns `total=0` — HTTP 200, but an empty body — for
whole contiguous blocks of games. Observed: 57 games in 2024-25
(2024021235-2024021291) and 505 games in 2025-26, 38% of the season, in nine
separate blocks. The classic HTML TOI reports DO carry the data for them:

    https://www.nhl.com/scores/htmlreports/{season}/TV{gamenum}.HTM   (visitor)
    https://www.nhl.com/scores/htmlreports/{season}/TH{gamenum}.HTM   (home)

Without this fallback those games have no on-ice rosters and drop out of any
analysis built on shifts.

The pipeline only fetches these reports where the JSON feed is empty, so the two
sources never overlap in the archive. Checked separately on 12 games that have
both: per-player TOI correlates 0.99998 (456 player-games, median difference
0 s), and shift counts match exactly for 99.3% of players.

REPORT STRUCTURE
----------------
    td.teamHeading    -> "CAROLINA HURRICANES"
    td.playerHeading  -> "4 GOSTISBEHERE, SHAYNE"     (sweater + name)
    then shift rows:  Shift# | Per | Start Elapsed/Game | End Elapsed/Game |
                      Duration | Event

"Start of Shift 1:38 / 18:22" is elapsed-in-period / clock-remaining. We take
the ELAPSED value, which matches the JSON feed's startTime semantics.

JOIN KEY DISCIPLINE (docs/METHODS.md §2)
----------------------------------------
The HTML only exposes sweater number + name. We never key on those: sweater is
resolved to `playerId` via that game's own PBP `rosterSpots` (teamId +
sweaterNumber -> playerId), an authoritative, game-scoped lookup. Everything
downstream is keyed on playerId. Team identity comes from the file itself
(TV = away, TH = home) rather than by matching team-name strings.

Output rows match the JSON shift schema exactly, so both sources feed one table.
"""

from __future__ import annotations

import argparse
import json
import time

from bs4 import BeautifulSoup

import pipeline_common as pc

HTML_URL = "https://www.nhl.com/scores/htmlreports/{season}/{which}{gamenum}.HTM"


def report_url(season, game_id, which):
    """`which` in {'TV','TH'}. Game number is the last 6 digits of the game id."""
    return HTML_URL.format(season=season, which=which, gamenum=str(game_id)[-6:])


def _elapsed(cell):
    """'1:38 / 18:22' -> '1:38' (elapsed in period)."""
    return cell.split("/")[0].strip() if cell else None


def _period(cell):
    """Period cell -> int. Handles 'OT'/'SO' labels as well as digits."""
    c = (cell or "").strip().upper()
    if c.isdigit():
        return int(c)
    if c == "OT":
        return 4
    if c == "SO":
        return None          # shootout: not real ice time
    return None


def parse_html_shifts(html, game_id, team_id, roster_by_sweater, season):
    """
    Parse one TV/TH report into shift rows matching the JSON schema.

    `roster_by_sweater` maps int(sweaterNumber) -> playerId for THIS team in
    THIS game (built from PBP rosterSpots).
    """
    soup = BeautifulSoup(html, "lxml")
    rows = []
    unresolved = set()

    for head in soup.find_all("td", class_="playerHeading"):
        text = head.get_text(" ", strip=True)          # "4 GOSTISBEHERE, SHAYNE"
        sweater = text.split(" ", 1)[0].strip()
        if not sweater.isdigit():
            continue
        player_id = roster_by_sweater.get(int(sweater))
        if player_id is None:
            unresolved.add(sweater)
            continue

        # Shift rows are the sibling <tr>s after this player's heading row,
        # up until the next player heading.
        tr = head.find_parent("tr")
        for sib in tr.find_next_siblings("tr"):
            if sib.find("td", class_="playerHeading"):
                break
            cells = [c.get_text(" ", strip=True) for c in sib.find_all("td")]
            if len(cells) < 5 or not cells[0].isdigit():
                continue                                # header / summary row
            period = _period(cells[1])
            if period is None:
                continue
            start = _elapsed(cells[2])
            end = _elapsed(cells[3])
            start_abs = pc.abs_game_seconds(period, start)
            end_abs = pc.abs_game_seconds(period, end)
            if start_abs is None or end_abs is None or end_abs <= start_abs:
                continue
            rows.append({
                "season": season,
                "game_id": game_id,
                "player_id": player_id,
                "team_id": team_id,
                "period": period,
                "start_time": start,
                "end_time": end,
                "start_abs_seconds": start_abs,
                "end_abs_seconds": end_abs,
                "duration_seconds": end_abs - start_abs,
                "shift_number": int(cells[0]),
                "source": "html",
            })
    return rows, unresolved


def roster_maps(pbp):
    """(home_id, away_id, {teamId: {sweater:int -> playerId}}) from a PBP payload."""
    home_id = (pbp.get("homeTeam") or {}).get("id")
    away_id = (pbp.get("awayTeam") or {}).get("id")
    by_team = {}
    for rs in pbp.get("rosterSpots", []):
        t = rs.get("teamId")
        sw = rs.get("sweaterNumber")
        pid = rs.get("playerId")
        if t is None or sw is None or pid is None:
            continue
        by_team.setdefault(t, {})[int(sw)] = pid
    return home_id, away_id, by_team


def games_missing_shifts(lay: pc.Layout, season):
    """Games whose raw JSON shift file is absent or carries zero rows."""
    missing = []
    for pbp_path in sorted(lay.raw_pbp_dir(season).glob("*.json")):
        game = pbp_path.stem
        sp = lay.shifts(season, game)
        if not pc.exists_nonempty(sp):
            missing.append(game)
            continue
        try:
            d = json.loads(sp.read_bytes())
        except (ValueError, OSError):
            missing.append(game)
            continue
        if not d.get("data"):
            missing.append(game)
    return missing


def fetch_missing(lay: pc.Layout, season, delay=pc.DEFAULT_DELAY, force=False):
    """Download TV+TH reports for every game lacking JSON shift data."""
    missing = games_missing_shifts(lay, season)
    print(f"\n=== HTML SHIFT FALLBACK {season}: {len(missing)} games missing "
          f"JSON shifts ===", flush=True)
    if not missing:
        return {"games": 0, "files": 0, "errors": 0}

    totals = {"games": len(missing), "files": 0, "cached": 0, "errors": 0}
    t0 = time.time()
    for n, game in enumerate(missing, 1):
        for which in ("TV", "TH"):
            path = lay.shifts_html(season, game, which)
            if pc.exists_nonempty(path) and not force:
                totals["cached"] += 1
                continue
            res = pc.get(report_url(season, game, which), delay=delay,
                         parse_json=False)
            pc.append_jsonl(lay.fetch_log(season),
                            {"season": season, "game": game, "kind": f"shifts_html_{which}",
                             "status": res.status, "attempts": res.attempts,
                             "bytes": (len(res.content) if res.content else 0),
                             "error": res.error, "ts": time.time()})
            if res.ok:
                pc.write_raw(path, res.content)
                totals["files"] += 1
            else:
                totals["errors"] += 1
        if n % 10 == 0:
            print(f"    {n}/{len(missing)} games, {totals['files']} files, "
                  f"{totals['errors']} errors, {(time.time()-t0)/60:.1f} min",
                  flush=True)

    print(f"    DONE: {totals['files']} files fetched, {totals['cached']} cached, "
          f"{totals['errors']} errors, {(time.time()-t0)/60:.1f} min", flush=True)
    return totals


def parse_game(lay: pc.Layout, season, game):
    """Parse both reports for one game into shift rows. Returns (rows, notes)."""
    pbp_path = lay.pbp(season, game)
    if not pc.exists_nonempty(pbp_path):
        return [], "no-pbp"
    try:
        pbp = json.loads(pbp_path.read_bytes())
    except (ValueError, OSError):
        return [], "pbp-unparseable"

    home_id, away_id, by_team = roster_maps(pbp)
    rows, notes = [], []
    for which, team_id in (("TV", away_id), ("TH", home_id)):
        path = lay.shifts_html(season, game, which)
        if not pc.exists_nonempty(path) or team_id is None:
            notes.append(f"{which}-missing")
            continue
        html = path.read_bytes().decode("utf-8", errors="replace")
        r, unresolved = parse_html_shifts(html, int(game), team_id,
                                          by_team.get(team_id, {}), season)
        rows.extend(r)
        if unresolved:
            notes.append(f"{which}-unresolved-sweaters:{sorted(unresolved)}")
    return rows, ";".join(notes)


def main():
    ap = argparse.ArgumentParser(description="Fetch HTML TOI reports for games "
                                             "missing JSON shift data.")
    ap.add_argument("--seasons", nargs="+", default=["20242025"])
    ap.add_argument("--root", default=".")
    ap.add_argument("--delay", type=float, default=pc.DEFAULT_DELAY)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    lay = pc.Layout(args.root)
    for season in args.seasons:
        fetch_missing(lay, season, args.delay, args.force)


if __name__ == "__main__":
    main()
