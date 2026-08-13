#!/usr/bin/env python3
"""
edge_scrape.py — Fetch NHL Edge season-level per-player stats.

Season-level Edge data: top shot speed, skating speed, distance skated,
shot-location summary and zone time for skaters; save %, GAA and shot-location
for goalies. These are per-player-per-season AGGREGATES, a separate data source
from the frame-level goal tracking and not an input to it. It runs as an
ISOLATED step after the main scrape: if it fails wholesale it never touches the
core raw archive or the tables built from it.

Edge coverage starts in 2021-22, two seasons earlier than the sprite floor, so
this reaches back further than anything else in the pipeline.

Player list comes from the season's own PBP rosterSpots (keyed on playerId,
docs/METHODS.md §2), so the main scrape must have run first. Position routes each
player to skater-detail vs goalie-detail. A player with no Edge data for the
season returns a clean 404 — logged, skipped (same discipline as sprites).

Endpoints (nhlpy client.edge.*, confirmed live 2026-07-18):
  edge/skater-detail/{playerId}/{season}/{gameType}
  edge/goalie-detail/{playerId}/{season}/{gameType}

Usage (normally via run_pipeline.py):
    python edge_scrape.py --seasons 20242025 --game-type 2
"""

from __future__ import annotations

import argparse
import time

import pipeline_common as pc


def scrape_edge_season(lay: pc.Layout, season, game_type, delay, force):
    log_path = lay.edge_log(season)
    players = pc.season_players(lay, season)
    if not players:
        print(f"\n=== EDGE season {season}: no raw PBP found - run scrape first. ===",
              flush=True)
        return {"skaters": 0, "goalies": 0, "no_edge_404": 0, "errors": 0}

    print(f"\n=== EDGE season {season}: {len(players)} players "
          f"(type {game_type}) ===", flush=True)
    print(f"    logging to {log_path}", flush=True)

    totals = {"skaters": 0, "goalies": 0, "cached": 0, "no_edge_404": 0,
              "errors": 0}
    t0 = time.time()
    n = 0
    for pid, pos in players.items():
        n += 1
        is_goalie = (pos == "G")
        kind = "goalie" if is_goalie else "skater"
        path = lay.edge(season, kind, pid)
        if pc.exists_nonempty(path) and not force:
            totals["cached"] += 1
            continue
        url = (pc.EDGE_GOALIE_URL if is_goalie else pc.EDGE_SKATER_URL).format(
            player=pid, season=season, game_type=game_type)
        res = pc.get(url, delay=delay)
        pc.append_jsonl(log_path, {"season": season, "player": pid, "kind": kind,
                                   "status": res.status, "attempts": res.attempts,
                                   "bytes": (len(res.content) if res.content else 0),
                                   "error": res.error, "ts": time.time()})
        if res.ok:
            pc.write_raw(path, res.content)
            totals["skaters" if not is_goalie else "goalies"] += 1
        elif res.status == 404:
            # No Edge data for this player-season (didn't play / below minimum).
            totals["no_edge_404"] += 1
        else:
            totals["errors"] += 1

        if n % 100 == 0:
            print(f"    {n}/{len(players)} players | "
                  f"{totals['skaters']} skaters, {totals['goalies']} goalies, "
                  f"{totals['no_edge_404']} no-edge, {totals['errors']} err, "
                  f"{(time.time()-t0)/60:.1f} min", flush=True)

    print(f"    DONE edge {season}: {totals['skaters']} skaters, "
          f"{totals['goalies']} goalies, {totals['no_edge_404']} no-edge-404, "
          f"{totals['cached']} cached, {totals['errors']} errors, "
          f"{(time.time()-t0)/60:.1f} min", flush=True)
    return totals


def main():
    ap = argparse.ArgumentParser(description="NHL Edge season-level scrape.")
    ap.add_argument("--seasons", nargs="+", default=["20242025"])
    ap.add_argument("--root", default=".")
    ap.add_argument("--game-type", type=int, default=2)
    ap.add_argument("--delay", type=float, default=pc.DEFAULT_DELAY)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    lay = pc.Layout(args.root)
    for season in args.seasons:
        scrape_edge_season(lay, season, args.game_type, args.delay, args.force)


if __name__ == "__main__":
    main()
