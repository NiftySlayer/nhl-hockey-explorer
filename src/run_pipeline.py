#!/usr/bin/env python3
"""
run_pipeline.py — One command for the overnight run.

Scrapes the raw archive and then builds the processed tables, per season, in
the order given. Put the season you care about most first: the run is long and
resumable, so an interruption leaves the earlier seasons complete.

Steps are decoupled by design (docs/SCHEMA.md):
  scrape       — the network job (writes immutable raw/); the slow, overnight part.
  shifts-html  — network; fetches HTML TOI reports for the games whose JSON
                 shift payload came back empty. Not optional — see below.
  edge         — an isolated second network job for the Edge season aggregates.
  build        — offline transform raw/ -> processed/ + audit/; re-runnable.
  stints       — offline; sweeps the shift tables into stint intervals.
  tracking     — offline, OPT-IN, never part of `all`; re-reads the sprites and
                 writes every 0.1 s frame of every goal in long format
                 (processed/tracking/, ~14M rows per season). Independent of
                 build: it reads raw/ only, so it can run before, after, or
                 without it.

Because scrape is idempotent (skips files already on disk), re-running after an
interruption resumes for free. Because build reads only raw/, you can re-derive
every table after changing a parse rule or the shot detector WITHOUT
re-scraping — which is the point of writing raw bytes verbatim in the first
place.

Typical use:
    # One season end to end, ~2 h (most of it waiting on the rate limit):
    python src/run_pipeline.py --root . --seasons 20242025

    # Full 3-season archive overnight, ~6 h:
    python src/run_pipeline.py --root . --seasons 20242025 20232024 20252026

    # Re-build every table from an existing raw archive (no network):
    python src/run_pipeline.py --root . --steps build --seasons 20242025

    # The full continuous tracking table for one season (opt-in, no network):
    python src/run_pipeline.py --root . --steps tracking --seasons 20242025

If you run the steps by hand, do NOT skip shifts-html. The JSON shift feed
answers HTTP 200 with an empty body for whole blocks of games, so the build
falls back to HTML reports — which are only on disk if that step ran. Skipping
it drops those games' on-ice rosters silently, with no error to notice.
"""

from __future__ import annotations

import argparse
import time

import pipeline_common as pc
import shifts_html
from scrape_raw import scrape_season
from edge_scrape import scrape_edge_season
from build_processed import build_players, build_season, build_tracking
from stints import build_stints


def main():
    ap = argparse.ArgumentParser(description="NHL tracking-data pipeline "
                                             "(scrape + build).")
    ap.add_argument("--seasons", nargs="+", default=["20242025"],
                    help="in priority order; an interrupted run leaves the "
                         "earlier ones complete. Full archive: "
                         "20242025 20232024 20252026")
    ap.add_argument("--steps",
                    choices=["all", "scrape", "shifts-html", "edge", "build",
                             "stints", "tracking"],
                    default="all")
    ap.add_argument("--no-edge", action="store_true",
                    help="skip the NHL Edge season-level pull in --steps all")
    ap.add_argument("--root", default=".", help="archive root")
    ap.add_argument("--game-type", type=int, default=2, help="2=regular season")
    ap.add_argument("--delay", type=float, default=pc.DEFAULT_DELAY)
    ap.add_argument("--force", action="store_true", help="re-fetch cached raw")
    ap.add_argument("--stop-after-404", type=int, default=8)
    ap.add_argument("--max-games", type=int, default=1500)
    args = ap.parse_args()

    lay = pc.Layout(args.root)
    t0 = time.time()
    print(f"NHL tracking pipeline | steps={args.steps} | "
          f"seasons={args.seasons} | root={lay.root.resolve()}")

    if args.steps in ("all", "scrape"):
        for season in args.seasons:
            scrape_season(lay, season, args.game_type, args.delay, args.force,
                          args.stop_after_404, args.max_games)

    # THIS STEP IS NOT OPTIONAL IN PRACTICE. The JSON shiftcharts feed returns
    # HTTP 200 with an empty body for whole blocks of games (505 of them in
    # 2025-26), and the build silently falls back to HTML reports that are only
    # on disk if this ran. Skipping it costs those games their on-ice rosters
    # with no error anywhere. It must follow the scrape, since it reads each
    # game's PBP to resolve sweater numbers to player ids.
    if args.steps in ("all", "shifts-html"):
        for season in args.seasons:
            shifts_html.fetch_missing(lay, season, args.delay, args.force)

    # Edge runs after the core scrape (it reads each season's PBP rosterSpots)
    # and is isolated: a failure here never touches the core raw archive.
    if args.steps == "edge" or (args.steps == "all" and not args.no_edge):
        for season in args.seasons:
            scrape_edge_season(lay, season, args.game_type, args.delay, args.force)

    # Check EVERY season before writing anything. build_players reads all of
    # raw/pbp at once and writes processed/players.parquet, so it runs before
    # any per-season guard inside build_season would fire — pointed at a
    # misplaced archive it would truncate a good players.parquet to zero rows
    # and only then abort. Preflight the whole set instead.
    if args.steps in ("all", "build", "tracking"):
        for season in args.seasons:
            pc.require_raw(lay, season, sprites=(args.steps == "tracking"))

    if args.steps in ("all", "build"):
        position_map = build_players(lay)
        for season in args.seasons:
            build_season(lay, season, position_map)

    # Stints read the processed shift/game/shot/faceoff tables, so they come
    # after build rather than beside it.
    if args.steps in ("all", "stints"):
        for season in args.seasons:
            build_stints(lay, season)

    # Deliberately NOT in `all`. The clip table is ~14M rows per season, two
    # orders of magnitude larger than anything else here, and nothing else in
    # the pipeline reads it. Ask for it explicitly.
    if args.steps == "tracking":
        position_map = build_players(lay)
        for season in args.seasons:
            print(f"\n=== TRACKING season {season} ===", flush=True)
            build_tracking(lay, season, position_map)

    print(f"\nALL DONE in {(time.time()-t0)/60:.1f} min. "
          f"raw/ processed/ audit/ under {lay.root.resolve()}")


if __name__ == "__main__":
    main()
