#!/usr/bin/env python3
"""
scrape_raw.py — The overnight NETWORK job. Fetches and archives raw data
verbatim; does no parsing beyond what it needs to enumerate goals.

For each regular-season game in each requested season it writes three kinds
of immutable raw file (docs/SCHEMA.md):
  raw/pbp/{season}/{game}.json          play-by-play (goals, shots, rosters)
  raw/shifts/{season}/{game}.json       shift charts (stints / on-ice rosters)
  raw/sprites/{season}/{game}/ev{e}.json  goal tracking (one per goal event)

Design for an unattended overnight run (docs/METHODS.md §1):
  * IDEMPOTENT — a file already on disk is skipped, so re-running after a
    crash/interruption resumes for free. Use --force to re-fetch.
  * NEVER HALTS — every failure is a logged row in audit/fetch_log_{season}.jsonl,
    not an exception.
  * POLITE — one request at a time, ~0.7 s apart, retry with backoff.
  * RAW IS IMMUTABLE — bytes written verbatim, only on HTTP 200.

Games are enumerated by walking regular-season numbers (type 02) upward and
stopping after a run of consecutive 404s — no hardcoded season length, works
for a finished season (2024-25 = 1312 games) or a partial one (2025-26).

Usage (normally invoked via run_pipeline.py):
    python scrape_raw.py --seasons 20242025
    python scrape_raw.py --seasons 20242025 20232024 20252026 --delay 0.7
"""

from __future__ import annotations

import argparse
import time

import pipeline_common as pc


def scrape_game(lay: pc.Layout, season, game, delay, force, log_path):
    """
    Fetch PBP + shifts + every goal sprite for one game. Returns a small
    counts dict. Logs each HTTP outcome to the fetch log. Never raises.
    """
    counts = {"pbp": 0, "shifts": 0, "sprites": 0, "sprite_403": 0,
              "cached": 0, "errors": 0}

    def logrow(**kw):
        pc.append_jsonl(log_path, {"season": season, "game": game,
                                   "ts": time.time(), **kw})

    # --- PBP (needed to enumerate goals + later parsing) ---
    pbp_path = lay.pbp(season, game)
    pbp = None
    if pc.exists_nonempty(pbp_path) and not force:
        counts["cached"] += 1
        try:
            import json
            pbp = json.loads(pbp_path.read_bytes())
        except ValueError:
            pbp = None
    if pbp is None:
        res = pc.get(pc.PBP_URL.format(game=game), delay=delay)
        logrow(kind="pbp", status=res.status, attempts=res.attempts,
               bytes=(len(res.content) if res.content else 0), error=res.error)
        if not res.ok:
            counts["errors"] += 1
            # 404 here = game does not exist; caller treats as a miss.
            return counts, res.status, pbp
        pc.write_raw(pbp_path, res.content)
        counts["pbp"] += 1
        pbp = res.json

    if not isinstance(pbp, dict):
        counts["errors"] += 1
        logrow(kind="pbp", status="unparseable")
        return counts, 200, None

    # --- shift charts (stints) ---
    shifts_path = lay.shifts(season, game)
    if pc.exists_nonempty(shifts_path) and not force:
        counts["cached"] += 1
    else:
        res = pc.get(pc.SHIFTS_URL.format(game=game), delay=delay)
        logrow(kind="shifts", status=res.status, attempts=res.attempts,
               bytes=(len(res.content) if res.content else 0), error=res.error)
        if res.ok:
            pc.write_raw(shifts_path, res.content)
            counts["shifts"] += 1
        else:
            counts["errors"] += 1  # logged; not fatal

    # --- one sprite per goal event ---
    for g in pc.enumerate_goals(pbp):
        eid = g["event_id"]
        if eid is None:
            continue
        sp_path = lay.sprite(season, game, eid)
        if pc.exists_nonempty(sp_path) and not force:
            counts["cached"] += 1
            continue
        res = pc.get(pc.SPRITE_URL.format(season=season, game=game, event=eid),
                     delay=delay)
        logrow(kind="sprite", event=eid, status=res.status,
               attempts=res.attempts,
               bytes=(len(res.content) if res.content else 0), error=res.error)
        if res.ok:
            pc.write_raw(sp_path, res.content)
            counts["sprites"] += 1
        elif res.status == 403:
            # Expected for the rare goal with no public sprite (e.g. shootout
            # deciders). An audit finding, not an error.
            counts["sprite_403"] += 1
        else:
            counts["errors"] += 1

    return counts, 200, pbp


def scrape_season(lay: pc.Layout, season, game_type, delay, force,
                  stop_after_404, max_games):
    """Walk regular-season game numbers until a run of consecutive 404s."""
    log_path = lay.fetch_log(season)
    print(f"\n=== SCRAPE season {season} (type {game_type}) ===", flush=True)
    print(f"    logging to {log_path}", flush=True)

    totals = {"games": 0, "goals_sprites": 0, "sprite_403": 0,
              "shifts": 0, "errors": 0, "missing_404": 0}
    consecutive_404 = 0
    number = 1
    t0 = time.time()

    while number <= max_games:
        gid = pc.game_id(season, game_type, number)
        counts, status, _ = scrape_game(lay, season, gid, delay, force, log_path)

        if status == 404:
            consecutive_404 += 1
            totals["missing_404"] += 1
            if consecutive_404 >= stop_after_404:
                print(f"    stop: {consecutive_404} consecutive 404s at "
                      f"game #{number}; season end reached.", flush=True)
                break
            number += 1
            continue

        consecutive_404 = 0
        totals["games"] += 1
        totals["goals_sprites"] += counts["sprites"]
        totals["sprite_403"] += counts["sprite_403"]
        totals["shifts"] += counts["shifts"]
        totals["errors"] += counts["errors"]

        if number % 25 == 0 or counts["errors"]:
            elapsed = time.time() - t0
            print(f"    game #{number} {gid}: "
                  f"+{counts['sprites']} sprites "
                  f"({counts['cached']} cached)  "
                  f"| season so far: {totals['games']} games, "
                  f"{totals['goals_sprites']} new sprites, "
                  f"{totals['errors']} errors, {elapsed/60:.1f} min",
                  flush=True)
        number += 1

    dt = (time.time() - t0) / 60.0
    print(f"    DONE {season}: {totals['games']} games, "
          f"{totals['goals_sprites']} new sprites, "
          f"{totals['sprite_403']} sprite-403, "
          f"{totals['shifts']} shift files, "
          f"{totals['errors']} errors, {dt:.1f} min", flush=True)
    return totals


def main():
    ap = argparse.ArgumentParser(description="NHL raw scrape (overnight).")
    ap.add_argument("--seasons", nargs="+", default=["20242025"],
                    help="e.g. 20242025 20232024 20252026")
    ap.add_argument("--root", default=".", help="archive root (holds raw/, audit/)")
    ap.add_argument("--game-type", type=int, default=2, help="2=regular season")
    ap.add_argument("--delay", type=float, default=pc.DEFAULT_DELAY)
    ap.add_argument("--force", action="store_true", help="re-fetch cached files")
    ap.add_argument("--stop-after-404", type=int, default=8,
                    help="consecutive missing games that mark season end")
    ap.add_argument("--max-games", type=int, default=1500,
                    help="hard cap on game number walked per season")
    args = ap.parse_args()

    lay = pc.Layout(args.root)
    for season in args.seasons:
        scrape_season(lay, season, args.game_type, args.delay, args.force,
                      args.stop_after_404, args.max_games)


if __name__ == "__main__":
    main()
