#!/usr/bin/env python3
"""
tracking_validation.py — Grade processed/tracking/{season}.parquet against the
play-by-play and against the shot-frame tables it is supposed to agree with.

WHY THIS EXISTS
---------------
`shotframe_validation.py` grades the shot DETECTION. This grades the continuous
tracking TABLE: the same clips reduced to one row per entity per frame, in
absolute rink feet and in the attack frame. Two things can go wrong there that
the shot-frame checks cannot see.

  1. A SECOND IMPLEMENTATION OF THE SHOT DETECTOR. build_tracking does not call
     process_goal; `locate_goal_and_shot_frames` re-sequences the same helpers
     (build_processed.py, see its docstring). The claim is that the frame
     indices it picks are identical to the ones already published in
     audit/completeness_{season}.parquet. That is checkable, and if it fails
     every per-frame flag in the table is anchored to the wrong instant.

  2. THE ATTACK FRAME. `x_att`/`y_att` rotate the rink so the conceding team's
     net is at x = +89. A rotation that is silently a mirror, or a flip that
     points at the wrong net, produces coordinates that look entirely plausible
     — right magnitudes, right rink, wrong end or wrong wing.

Neither is falsifiable from inside the table. Both are falsifiable against the
play-by-play, which records its own (x, y) for every goal, placed by an NHL
scorer watching the game, and its own `homeTeamDefendingSide`.

THE HEADLINE CHECK is the shooter at the shot frame. At release the scorer is
holding the puck, so his absolute coordinates should sit where the play-by-play
put the goal. That is measured here against the same baseline the shot-frame
dataset is measured against (METHODS §7.4: the puck a median 2.04 ft from the
PBP's location, 90.1% within 10 ft, pooled over three seasons).

This module reads only published tables — no sprite parsing. It grades the
parquet, not the code that wrote it.

    python src/tracking_validation.py --root . --seasons 20242025
"""

from __future__ import annotations

import argparse
import math

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

import build_processed as bp
import pipeline_common as pc

SEASONS = ["20232024", "20242025", "20252026"]
NET_X = bp.NET_X

# Agreement thresholds. The first two are arithmetic identities held to a
# rounding tolerance; the third is the empirical band from METHODS §7.4.
EXACT_FT = 0.011           # both sides are rounded to 2-3 dp before storage
ROT_FT = 0.002             # x_att == x * flip, to float32 storage error
FLIP_MIN_SHARE = 0.15      # share fitting the NEGATED frame better (checks 4)
AGREE_FT = 10.0            # the "within 10 ft" band the shot dataset reports

# HOW FAR OUTSIDE THE RINK A COORDINATE MAY LEGITIMATELY SIT.
# The nominal rink is 200 x 85 ft, so |x| <= 100 and |y| <= 42.5, but the feed
# reports up to a foot past that on every side: measured over 2024-25 the
# extremes are x in [-100.99, 100.95] and y in [-43.45, 43.50], never more than
# 1.0 ft out. It is a player pressed against the boards (and, past the side
# boards, the benches), not a transform error, so the bound is checked with a
# tolerance rather than at the nominal edge.
RINK_SLOP_FT = 1.05


def _f(s):
    """Arrow-backed column -> float64 numpy with NaN for null."""
    return s.to_numpy(dtype="float64", na_value=np.nan)


class Verdicts:
    """Collects a pass/fail per check so the tail of the report is scannable."""

    def __init__(self):
        self.rows = []

    def add(self, name, ok, detail):
        self.rows.append((name, bool(ok), detail))
        print(f"  ==> {'PASS' if ok else 'FAIL'}  {detail}")

    def summary(self):
        print("\n" + "=" * 78)
        print("VERDICT")
        print("=" * 78)
        for name, ok, detail in self.rows:
            print(f"  {'PASS' if ok else 'FAIL'}  {name:<44} {detail}")
        n_bad = sum(1 for _, ok, _ in self.rows if not ok)
        print(f"\n  {len(self.rows) - n_bad}/{len(self.rows)} checks passed")
        return n_bad == 0


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------

def load_shot_frames(lay: pc.Layout, season):
    """Every row at a detected shot frame, via a parquet predicate pushdown.

    ~100k rows out of ~14M, so this never materialises the season. The
    pyarrow dtype backend is not optional: with the default backend the
    nullable integer ids come back as float64 and will not join.
    """
    return pd.read_parquet(lay.tracking(season),
                           filters=[("is_shot_frame", "==", True)],
                           dtype_backend="pyarrow")


def load_goal_frame_pucks(lay: pc.Layout, season):
    return pd.read_parquet(lay.tracking(season),
                           filters=[("is_goal_frame", "==", True),
                                    ("entity_type", "==", "puck")],
                           dtype_backend="pyarrow")


def load_pbp_goals(lay: pc.Layout, season):
    """The PBP's own record of each goal: location and defending side."""
    s = pd.read_parquet(lay.shots(season))
    return s[s.event_type == "goal"][
        ["game_id", "event_id", "x_coord", "y_coord", "zone_code",
         "home_team_defending_side", "home_team_id", "event_owner_team_id",
         "goalie_in_net_id"]].copy()


# ---------------------------------------------------------------------------
# 1. the duplicated code path
# ---------------------------------------------------------------------------

def check_frame_indices(v, sf, au):
    """tracking's frame indices vs the ones the audit already published."""
    print("\n" + "-" * 78)
    print("CHECK 1 — does the tracking build pick the SAME frames as the audit?")
    print("-" * 78)
    print("  build_tracking re-sequences the detector instead of calling")
    print("  process_goal. If the two ever diverge, they diverge silently.")

    per_goal = sf.groupby(["game_id", "event_id"], as_index=False).first()[
        ["game_id", "event_id", "goal_frame_idx", "shot_frame_idx"]]
    m = per_goal.merge(au[["game_id", "event_id", "goalframe_index",
                           "shotframe_index"]],
                       on=["game_id", "event_id"], how="inner")
    print(f"\n  goals comparable: {len(m):,}")
    for ours, theirs, lab in (("goal_frame_idx", "goalframe_index", "goal"),
                              ("shot_frame_idx", "shotframe_index", "shot")):
        a, b = _f(m[ours]), m[theirs].to_numpy(dtype="float64")
        eq = (a == b) | (np.isnan(a) & np.isnan(b))
        print(f"    {lab + ' frame index equal':<28} {eq.mean():7.4%}  "
              f"({(~eq).sum()} differ)")
        v.add(f"{lab} frame index == audit", eq.all(),
              f"{eq.mean():.4%} of {len(m):,} goals")
    return m


# ---------------------------------------------------------------------------
# 2. shooter-to-puck distance
# ---------------------------------------------------------------------------

def check_scorer_puck_distance(v, sf, au):
    """dist_to_puck_ft for the scorer vs the audit's scorer_dist_shotframe."""
    print("\n" + "-" * 78)
    print("CHECK 2 — shooter-to-puck distance at the shot frame")
    print("-" * 78)
    print("  The same quantity the shot-frame dataset already publishes,")
    print("  recomputed independently in the tracking build. Unsmoothed on both")
    print("  sides, so agreement should be exact to rounding.")

    sc = sf[sf.is_scorer.fillna(False).astype(bool)]
    m = sc.merge(au[["game_id", "event_id", "scorer_dist_shotframe"]],
                 on=["game_id", "event_id"], how="inner")
    ours = _f(m.dist_to_puck_ft)
    theirs = m.scorer_dist_shotframe.to_numpy(dtype="float64")
    ok = np.isclose(ours, theirs, atol=EXACT_FT, equal_nan=True)
    print(f"\n  scorer rows at a shot frame: {len(sc):,}   "
          f"joined to the audit: {len(m):,}")
    print(f"  agree to {EXACT_FT} ft: {ok.mean():.4%}   "
          f"max |difference| {np.nanmax(np.abs(ours - theirs)):.4f} ft")
    v.add("scorer dist_to_puck == audit", ok.all(),
          f"{ok.mean():.4%} of {len(m):,} goals")

    d = ours[~np.isnan(ours)]
    print(f"\n  distribution (n={len(d):,}):")
    print(f"    median {np.median(d):5.2f} ft   IQR "
          f"{np.percentile(d, 25):.2f}-{np.percentile(d, 75):.2f}   "
          f"p90 {np.percentile(d, 90):5.2f}   p99 {np.percentile(d, 99):6.2f}")
    print("\n  bands:")
    for t in (2, 5, 10, 15, 20):
        print(f"    within {t:>2} ft: {(d <= t).mean():7.2%}")
    return d


# ---------------------------------------------------------------------------
# 3. the shooter's absolute location vs the play-by-play
# ---------------------------------------------------------------------------

def _bands(err, label):
    print(f"    {label:<26} median {np.median(err):5.2f}  "
          f"p75 {np.percentile(err, 75):5.2f}  "
          f"p90 {np.percentile(err, 90):6.2f}  "
          f"p99 {np.percentile(err, 99):7.2f}   (n={len(err):,})")


def check_shooter_location(v, sf, pbp, au):
    """Absolute (x, y) of the identified shooter vs the PBP's goal location."""
    print("\n" + "-" * 78)
    print("CHECK 3 — is the shooter where the play-by-play says the goal was?")
    print("-" * 78)
    print("  The independent check: nothing in the pipeline reads the PBP's own")
    print("  goal coordinates, so unlike a self-consistency statistic this one")
    print("  can actually fail. The puck at the same frame is the published")
    print(f"  baseline (METHODS §7.4: median 2.04 ft, 90.1% within {AGREE_FT:.0f} ft).")

    who = sf.entity_type == "player"
    sc = sf[who & sf.is_scorer.fillna(False).astype(bool)]
    pk = sf[sf.entity_type == "puck"]

    m = sc.merge(pbp, on=["game_id", "event_id"], how="inner",
                 suffixes=("", "_pbp"))
    m = m.merge(pk[["game_id", "event_id", "x", "y"]]
                .rename(columns={"x": "puck_x", "y": "puck_y"}),
                on=["game_id", "event_id"], how="left")
    m = m.merge(au[["game_id", "event_id", "shot_confidence", "shot_method",
                    "shot_net_dist_ft"]], on=["game_id", "event_id"], how="left")
    m = m[m.x_coord.notna() & m.y_coord.notna()].copy()

    px, py = m.x_coord.to_numpy(float), m.y_coord.to_numpy(float)
    m["err_scorer"] = np.hypot(_f(m.x) - px, _f(m.y) - py)
    m["err_puck"] = np.hypot(_f(m.puck_x) - px, _f(m.puck_y) - py)
    # the same comparison with the tracked point negated — see check 4
    m["err_scorer_neg"] = np.hypot(-_f(m.x) - px, -_f(m.y) - py)

    es = m.err_scorer.dropna().to_numpy()
    ep = m.err_puck.dropna().to_numpy()
    print(f"\n  goals with a shot frame, a tracked scorer and PBP coordinates: "
          f"{len(es):,}")
    print("\n  Distance from the play-by-play's recorded goal location:")
    _bands(es, "SHOOTER at shot frame")
    _bands(ep, "puck at shot frame")

    print("\n  Agreement bands:")
    print(f"    {'':<12}{'shooter':>10}{'puck':>10}")
    for t in (5, 10, 15, 20, 30, 50):
        print(f"    within {t:>2} ft {(es <= t).mean():9.2%}"
              f"{(ep <= t).mean():10.2%}")

    share = (es <= AGREE_FT).mean()
    puck_share = (ep <= AGREE_FT).mean()
    v.add("shooter at the PBP goal location", share > 0.5,
          f"{share:.2%} within {AGREE_FT:.0f} ft (majority test)")
    # The shooter is not the puck: at release they sit a couple of feet apart,
    # so a few points below the puck's band is expected. A big gap is not.
    v.add("shooter band vs puck band", (puck_share - share) <= 0.10,
          f"shooter {share:.2%} vs puck {puck_share:.2%} "
          f"({puck_share - share:+.2%})")

    print("\n  by shot_confidence (shooter error):")
    print(m.groupby("shot_confidence", observed=True).agg(
        n=("err_scorer", "size"), median=("err_scorer", "median"),
        within10=("err_scorer", lambda s: (s <= AGREE_FT).mean())
    ).round(3).to_string())
    print("\n  by shot_method (shooter error):")
    print(m.groupby("shot_method", observed=True).agg(
        n=("err_scorer", "size"), median=("err_scorer", "median"),
        within10=("err_scorer", lambda s: (s <= AGREE_FT).mean())
    ).round(3).to_string())
    return m


# ---------------------------------------------------------------------------
# 4. orientation
# ---------------------------------------------------------------------------

def check_orientation(v, m):
    """Does the whole season share one orientation with the play-by-play?"""
    print("\n" + "-" * 78)
    print("CHECK 4 — do the tracking table and the PBP share a coordinate frame?")
    print("-" * 78)
    print("  If a subset of the season were stored with the rink reversed, the")
    print("  negated coordinates would fit the PBP better for that subset.")

    d = m.dropna(subset=["err_scorer", "err_scorer_neg"])
    better = (d.err_scorer_neg < d.err_scorer)
    print(f"\n  as-is      : median {d.err_scorer.median():6.2f} ft   "
          f"mean {d.err_scorer.mean():6.2f}")
    print(f"  x,y negated: median {d.err_scorer_neg.median():6.2f} ft   "
          f"mean {d.err_scorer_neg.mean():6.2f}")
    print(f"  goals fitting the NEGATED frame better: {better.mean():.2%}")
    v.add("single shared orientation", better.mean() < FLIP_MIN_SHARE,
          f"{better.mean():.2%} fit the negated frame better "
          f"(threshold {FLIP_MIN_SHARE:.0%})")


# ---------------------------------------------------------------------------
# 5. the attack frame
# ---------------------------------------------------------------------------

def check_attack_frame(v, lay, season, sf, gp, pbp):
    """The rotation, the net it points at, and where the puck ends up."""
    print("\n" + "-" * 78)
    print("CHECK 5 — the attack frame")
    print("-" * 78)

    # (a) rotation, not mirror — streamed over the whole season
    print("\n  (a) x_att == x x flip AND y_att == y x flip, on every row.")
    print("      Negating x alone would mirror the rink and swap the wings on")
    print("      every goal that needed flipping.")
    n = n_bad_x = n_bad_y = 0
    worst = 0.0
    f = pq.ParquetFile(lay.tracking(season))
    for batch in f.iter_batches(batch_size=500_000,
                                columns=["x", "y", "x_att", "y_att", "flip"]):
        b = batch.to_pydict()
        x = np.array(b["x"], dtype="float64")
        y = np.array(b["y"], dtype="float64")
        xa = np.array([np.nan if q is None else q for q in b["x_att"]])
        ya = np.array([np.nan if q is None else q for q in b["y_att"]])
        fl = np.array([np.nan if q is None else q for q in b["flip"]])
        live = ~np.isnan(xa)
        n += live.sum()
        dx = np.abs(xa[live] - x[live] * fl[live])
        dy = np.abs(ya[live] - y[live] * fl[live])
        n_bad_x += int((dx > ROT_FT).sum())
        n_bad_y += int((dy > ROT_FT).sum())
        if len(dx):
            worst = max(worst, float(dx.max()), float(dy.max()))
    print(f"      rows with an attack frame: {n:,}   "
          f"x mismatches {n_bad_x}   y mismatches {n_bad_y}   "
          f"max deviation {worst:.6f} ft")
    v.add("x_att/y_att are a 180 deg rotation", n_bad_x == 0 and n_bad_y == 0,
          f"{n:,} rows, {n_bad_x + n_bad_y} mismatches")

    # (b) the net the flip points at, recomputed from the PBP alone
    print("\n  (b) net_x vs the net implied by homeTeamDefendingSide + which")
    print("      team scored, recomputed here from the shots table.")
    per_goal = sf.groupby(["game_id", "event_id"], as_index=False).first()[
        ["game_id", "event_id", "net_x", "net_source", "flip"]]
    g = per_goal.merge(pbp, on=["game_id", "event_id"], how="inner")
    g["pbp_net_x"] = [
        pc.pbp_target_net_x(
            {"home_team_defending_side": r.home_team_defending_side,
             "event_owner_team_id": r.event_owner_team_id},
            r.home_team_id, NET_X)
        for r in g.itertuples()]
    cmp = g.dropna(subset=["pbp_net_x"])
    agree = np.sign(_f(cmp.net_x)) == np.sign(cmp.pbp_net_x.to_numpy(float))
    print(f"      comparable goals: {len(cmp):,}")
    print(f"      net sign agrees : {agree.mean():.4%}  "
          f"({(~agree).sum()} disagree)")
    print("      net_source: " + ", ".join(
        f"{k}={v_}" for k, v_ in per_goal.net_source.value_counts(
            dropna=False).items()))
    v.add("net_x == the PBP's target net", agree.all(),
          f"{agree.mean():.4%} of {len(cmp):,} goals")

    # (c) the end-to-end proof: the puck ends up IN the attacked net
    print("\n  (c) the puck's x_att at the goal frame. The goal instant is the")
    print("      puck's arrival at the net, so it should sit near +89 — this is")
    print("      what proves the flip points at the right end.")
    xa = _f(gp.x_att)
    xa = xa[~np.isnan(xa)]
    print(f"      n={len(xa):,}   median {np.median(xa):6.2f} ft   "
          f"p10 {np.percentile(xa, 10):6.2f}   p90 {np.percentile(xa, 90):6.2f}")
    print(f"      in the attacking half (x_att > 0): {(xa > 0).mean():.2%}")
    print(f"      within 15 ft of the goal line     : "
          f"{(np.abs(xa - NET_X) <= 15).mean():.2%}")
    v.add("puck ends in the attacked net", (xa > 0).mean() > 0.95,
          f"{(xa > 0).mean():.2%} of goal frames have the puck at x_att > 0, "
          f"median {np.median(xa):.1f} ft")

    # (d) the derived distance is the distance to that net
    print("\n  (d) dist_to_net_ft == hypot(89 - x_att, y_att).")
    d = sf.dropna(subset=["dist_to_net_ft"])
    recomputed = np.hypot(NET_X - _f(d.x_att), _f(d.y_att))
    ok = np.isclose(_f(d.dist_to_net_ft), recomputed, atol=EXACT_FT)
    print(f"      rows checked {len(d):,}   mismatches {(~ok).sum()}")
    v.add("dist_to_net_ft identity", ok.all(),
          f"{ok.mean():.4%} of {len(d):,} shot-frame rows")
    return g


# ---------------------------------------------------------------------------
# 6. structure
# ---------------------------------------------------------------------------

def check_structure(v, lay, season, sf, au):
    """Row counts, coordinate ranges, one puck per frame, null rates."""
    print("\n" + "-" * 78)
    print("CHECK 6 — structure")
    print("-" * 78)

    f = pq.ParquetFile(lay.tracking(season))
    total = f.metadata.num_rows
    size_mb = lay.tracking(season).stat().st_size / 1e6
    print(f"\n  rows {total:,}   row groups {f.metadata.num_row_groups}   "
          f"columns {f.metadata.num_columns}   {size_mb:,.0f} MB on disk")

    n_x = n_y = n_bad = n_puck_frames = n_frames = 0
    over_x = over_y = 0.0
    for batch in f.iter_batches(batch_size=500_000,
                                columns=["x", "y", "entity_type"]):
        b = batch.to_pydict()
        x = np.array(b["x"], dtype="float64")
        y = np.array(b["y"], dtype="float64")
        et = np.array(b["entity_type"], dtype=object)
        dx, dy = np.abs(x) - 100.0, np.abs(y) - 42.5
        n_x += int((dx > 0.0001).sum())
        n_y += int((dy > 0.0001).sum())
        n_bad += int(((dx > RINK_SLOP_FT) | (dy > RINK_SLOP_FT)).sum())
        over_x = max(over_x, float(dx.max()))
        over_y = max(over_y, float(dy.max()))
        n_puck_frames += int((et == "puck").sum())
        n_frames += len(x)
    print(f"\n  past the nominal boards: x {n_x:,} ({n_x / n_frames:.3%}), "
          f"y {n_y:,} ({n_y / n_frames:.3%})")
    print(f"  furthest out: x {over_x:+.2f} ft, y {over_y:+.2f} ft   "
          f"(the feed's own slop against the boards, not a transform error)")
    print(f"  beyond the {RINK_SLOP_FT} ft tolerance: {n_bad}")
    v.add("coordinates inside the rink + slop", n_bad == 0,
          f"{n_bad} rows past {RINK_SLOP_FT} ft of slop, worst "
          f"{max(over_x, over_y):+.2f} ft, of {n_frames:,}")

    print(f"  puck rows {n_puck_frames:,}  ->  "
          f"{(n_frames - n_puck_frames) / n_puck_frames:.2f} tracked entities "
          f"per frame")

    # Exactly one scorer row and one puck row per shot frame. Built as plain
    # numpy columns first: an agg lambda over an arrow-backed column has its
    # result cast BACK to that column's dtype, so counting `entity_type ==
    # "puck"` in place would hand back the string "1" and silently compare
    # false against 1.
    flags = pd.DataFrame({
        "game_id": sf.game_id.to_numpy(dtype="int64"),
        "event_id": sf.event_id.to_numpy(dtype="int64"),
        "frame_idx": sf.frame_idx.to_numpy(dtype="int64"),
        "is_scorer": sf.is_scorer.fillna(False).to_numpy(dtype=bool).astype(int),
        "is_puck": (sf.entity_type == "puck").to_numpy(dtype=bool).astype(int),
    })
    per = flags.groupby(["game_id", "event_id"]).agg(
        n_scorer=("is_scorer", "sum"), n_puck=("is_puck", "sum"),
        n_frames=("frame_idx", "nunique"))
    print(f"\n  goals with a shot frame: {len(per):,}")
    print(f"    exactly one frame flagged : {(per.n_frames == 1).mean():.4%}")
    print(f"    exactly one scorer row    : {(per.n_scorer == 1).mean():.4%}"
          f"   (0: {(per.n_scorer == 0).sum()}, "
          f">1: {(per.n_scorer > 1).sum()})")
    print(f"    exactly one puck row      : {(per.n_puck == 1).mean():.4%}")
    v.add("one shot frame, one scorer, one puck",
          bool((per.n_frames == 1).all() and (per.n_scorer == 1).all()
               and (per.n_puck == 1).all()),
          f"{len(per):,} goals")

    # the goals in the table vs the goals the audit says have a shot frame
    have = set(map(tuple, per.reset_index()[["game_id", "event_id"]].values))
    expect = set(map(tuple, au[au.shotframe_index.notna()]
                     [["game_id", "event_id"]].values))
    print(f"\n  goals the audit gives a shot frame: {len(expect):,}   "
          f"present here: {len(have & expect):,}   "
          f"missing: {len(expect - have):,}   extra: {len(have - expect):,}")
    v.add("shot-frame goals match the audit", have == expect,
          f"{len(have & expect):,} shared, {len(expect - have)} missing, "
          f"{len(have - expect)} extra")

    # The per-player columns are null on puck rows by design, so the expected
    # null rate for all of them is exactly the puck's share of the rows.
    puck_share = (sf.entity_type == "puck").mean()
    print(f"\n  null rate at shot frames (puck rows are {puck_share:.2%} of "
          f"them, and carry no player fields):")
    nulls = sf.isna().mean()
    for col, rate in nulls[nulls > 0].sort_values(ascending=False).items():
        print(f"    {col:<20} {rate:7.2%}")


# ---------------------------------------------------------------------------

def validate_season(lay: pc.Layout, season):
    print("\n" + "=" * 78)
    print(f"TRACKING TABLE VALIDATION — {season}")
    print("=" * 78)

    sf = load_shot_frames(lay, season)
    gp = load_goal_frame_pucks(lay, season)
    au = pd.read_parquet(lay.completeness(season))
    pbp = load_pbp_goals(lay, season)
    print(f"\n  {len(sf):,} rows at a shot frame, {len(gp):,} goal-frame puck "
          f"rows, {len(au):,} audit rows, {len(pbp):,} PBP goals")

    v = Verdicts()
    check_frame_indices(v, sf, au)
    check_scorer_puck_distance(v, sf, au)
    m = check_shooter_location(v, sf, pbp, au)
    check_orientation(v, m)
    check_attack_frame(v, lay, season, sf, gp, pbp)
    check_structure(v, lay, season, sf, au)
    ok = v.summary()
    return ok, m


def main():
    ap = argparse.ArgumentParser(
        description="Validate processed/tracking/{season}.parquet.")
    ap.add_argument("--root", default=".")
    ap.add_argument("--seasons", nargs="*", default=SEASONS)
    ap.add_argument("--out", default=None,
                    help="write the per-goal shooter-vs-PBP comparison here")
    args = ap.parse_args()

    lay = pc.Layout(args.root)
    all_ok = True
    frames = []
    for season in args.seasons:
        if not lay.tracking(season).exists():
            print(f"\n{season}: no tracking table — build it with "
                  f"`--steps tracking`. Skipped.")
            continue
        ok, m = validate_season(lay, season)
        all_ok &= ok
        frames.append(m)

    if args.out and frames:
        pd.concat(frames).to_parquet(args.out, index=False)
        print(f"\n  wrote {args.out}")
    raise SystemExit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
