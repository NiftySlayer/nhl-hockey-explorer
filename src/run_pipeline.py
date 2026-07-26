#!/usr/bin/env python3
"""
run_pipeline.py — One command for the overnight run.

Scrapes the raw archive and then builds the processed tables, per season, in
the order given. Put the season you care about most first: the run is long and
resumable, so an interruption leaves the earlier seasons complete.

Steps are decoupled by design (docs/SCHEMA.md):
  scrape  — the network job (writes immutable raw/); the slow, overnight part.
  edge    — an isolated second network job for the Edge season aggregates.
  build   — offline transform raw/ -> processed/ + audit/; fast, re-runnable.
  stints  — offline; sweeps the shift tables into stint intervals.

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
"""

from __future__ import annotations

import argparse
import time

import pipeline_common as pc
from scrape_raw import scrape_season
from edge_scrape import scrape_edge_season
from build_processed import build_players, build_season
from stints import build_stints


def main():
    ap = argparse.ArgumentParser(description="NHL tracking-data pipeline "
                                             "(scrape + build).")
    ap.add_argument("--seasons", nargs="+", default=["20242025"],
                    help="in priority order; an interrupted run leaves the "
                         "earlier ones complete. Full archive: "
                         "20242025 20232024 20252026")
    ap.add_argument("--steps",
                    choices=["all", "scrape", "edge", "build", "stints"],
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

    # Edge runs after the core scrape (it reads each season's PBP rosterSpots)
    # and is isolated: a failure here never touches the core raw archive.
    if args.steps == "edge" or (args.steps == "all" and not args.no_edge):
        for season in args.seasons:
            scrape_edge_season(lay, season, args.game_type, args.delay, args.force)

    if args.steps in ("all", "build"):
        position_map = build_players(lay)
        for season in args.seasons:
            build_season(lay, season, position_map)

    # Stints read the processed shift/game/shot/faceoff tables, so they come
    # after build rather than beside it.
    if args.steps in ("all", "stints"):
        for season in args.seasons:
            build_stints(lay, season)

    print(f"\nALL DONE in {(time.time()-t0)/60:.1f} min. "
          f"raw/ processed/ audit/ under {lay.root.resolve()}")


if __name__ == "__main__":
    main()
