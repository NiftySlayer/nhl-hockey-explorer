#!/usr/bin/env python3
"""
shotframe_validation.py — Cross-check the tracking-inferred shot against the
play-by-play's own record of the goal.

WHY THIS EXISTS
---------------
Every distance this pipeline emits is measured at a frame it infers, and the
obvious statistic for that inference — "the nearest player at the chosen frame
is the recorded scorer" — is close to circular. The scorer is what the frame is
ANCHORED on. At best that number confirms we found a frame where the scorer had
the puck; it says nothing about whether we found the frame where the shot left
his stick, or whether we placed it on the right part of the ice.

A validation statistic that cannot fail is not a validation statistic. This
module supplies one that can.

The PBP records its own (x, y) for every goal, assigned by an NHL scorer who
watched it. That is an INDEPENDENT observation of the same event, and it can
falsify three things the tracking pipeline assumes:

  1. COORDINATE FRAME. The sprite feed gives inches-from-corner, converted by
     pc.to_std. If the sprite and the PBP disagree about rink orientation — for
     instance if one flips ends between periods and the other does not — then
     every position is right in magnitude and wrong in sign for half the data.
     Test: compare our shot-frame puck position with the PBP coordinates, as-is
     versus x/y-negated. If the negated version fits better for a subset, there
     is a flip.

  2. ATTACK DIRECTION / TARGET NET. The naive rule is to take the target net
     from the sign of the puck's x at the goal frame — the puck ends up in the
     net, so its side identifies which net. That holds only when the goal frame
     is right, and this check is what caught the ~1% of goals where it is not.
     build_processed now takes the net from the PBP's `home_team_defending_side`
     plus which team owns the event, falling back to the puck sign only when
     that field is missing; the check below compares the two. When they
     disagree, `shot_net_dist_ft` — and any shot-distance stratification built
     on it — is measured to the wrong end of the rink.

  3. THE SHOT FRAME ITSELF. At the moment of release the scorer is holding the
     puck, so BOTH the puck and the scorer should sit at the PBP's recorded shot
     location. A large gap means the chosen frame is not the release: most
     likely an earlier touch in the same possession.

Anything the PBP itself is unsure about is excluded rather than counted as
agreement: goals with no PBP coordinates, and goals where the scorer is not
trackable in the sprite.

    python src/shotframe_validation.py --root . --seasons 20242025 --sample 3000
"""

from __future__ import annotations

import argparse
import json
import math

import numpy as np
import pandas as pd

import build_processed as bp
import pipeline_common as pc

SEASONS = ["20232024", "20242025", "20252026"]
NET_X = bp.NET_X


def pbp_target_net(row):
    """Target-net x sign from the PBP alone: defending side + event owner.

    `home_team_defending_side` is the side the HOME team defends in that period,
    so the home team attacks the other end. Returns None when the field is
    missing (it is occasionally absent in early-season files).
    """
    side = row.get("home_team_defending_side")
    if side not in ("left", "right"):
        return None
    home_attacks = -NET_X if side == "right" else NET_X
    is_home = row["event_owner_team_id"] == row["home_team_id"]
    return home_attacks if is_home else -home_attacks


def frame_positions(frames, idx, scorer_id):
    """Puck and scorer (x, y) at one frame, in standard coords."""
    if idx is None or idx >= len(frames):
        return (None, None), (None, None)
    return bp._puck_xy(frames[idx]), bp._entity_xy(frames[idx], scorer_id)


def collect(lay, seasons, sample=None, seed=0):
    """One row per goal: our geometry beside the PBP's."""
    rows = []
    for season in seasons:
        shots = pd.read_parquet(lay.shots(season))
        g = shots[(shots.event_type == "goal") & shots.x_coord.notna()].copy()
        au = pd.read_parquet(lay.completeness(season))
        au = au[au.shotframe_index.notna() & au.parsed.astype(bool)]
        m = g.merge(au[["game_id", "event_id", "shotframe_index",
                        "goalframe_index", "pbp_scorer_id", "shot_net_dist_ft",
                        "frames_before_goal", "shot_method"]],
                    on=["game_id", "event_id"], how="inner")
        if sample is not None and len(m) > sample:
            m = m.sample(sample, random_state=seed)
        print(f"  {season}: {len(m):,} goals to check", flush=True)

        for _, r in m.iterrows():
            path = lay.sprite(season, r.game_id, r.event_id)
            if not path.exists():
                continue
            # the sprite file IS the frame array — no wrapper object
            try:
                frames = json.loads(path.read_bytes())
            except Exception:
                continue
            if not isinstance(frames, list) or not frames:
                continue

            sid = r.pbp_scorer_id
            sid = None if pd.isna(sid) else int(sid)
            (spx, spy), (ssx, ssy) = frame_positions(
                frames, int(r.shotframe_index), sid)
            gpx, _ = frame_positions(frames, int(r.goalframe_index), sid)[0] \
                if not pd.isna(r.goalframe_index) else (None, None)

            rows.append({
                "season": season, "game_id": r.game_id, "event_id": r.event_id,
                "pbp_x": float(r.x_coord), "pbp_y": float(r.y_coord),
                "zone_code": r.zone_code,
                "puck_x": spx, "puck_y": spy,
                "scorer_x": ssx, "scorer_y": ssy,
                "goal_puck_x": gpx,
                "pbp_net_x": pbp_target_net(r),
                "our_net_x": (math.copysign(NET_X, gpx)
                              if gpx is not None else None),
                "our_shot_dist": r.shot_net_dist_ft,
                "lead_s": (r.frames_before_goal * 0.1
                           if not pd.isna(r.frames_before_goal) else None),
                "method": r.shot_method,
                "empty_net": bool(pd.isna(r.goalie_in_net_id)),
            })
    return pd.DataFrame(rows)


def report(df):
    d = df.dropna(subset=["puck_x", "puck_y"]).copy()
    print("\n" + "=" * 78)
    print("SHOT-FRAME VALIDATION AGAINST THE PLAY-BY-PLAY")
    print("=" * 78)
    print(f"\n{len(df):,} goals collected, {len(d):,} with a tracked puck at the "
          f"inferred shot frame")

    # ---- 1. coordinate frame -------------------------------------------
    print("\n" + "-" * 78)
    print("CHECK 1 — do the sprite and the PBP share a coordinate frame?")
    print("-" * 78)
    same = np.hypot(d.puck_x - d.pbp_x, d.puck_y - d.pbp_y)
    flip = np.hypot(-d.puck_x - d.pbp_x, -d.puck_y - d.pbp_y)
    d["err_same"], d["err_flip"] = same, flip
    better_flipped = (flip < same)
    print(f"  as-is      : median {same.median():6.1f} ft   "
          f"mean {same.mean():6.1f}")
    print(f"  x,y negated: median {flip.median():6.1f} ft   "
          f"mean {flip.mean():6.1f}")
    print(f"  goals fitting the NEGATED frame better: {better_flipped.mean():.1%}")
    if better_flipped.mean() > 0.15:
        print("  ==> A SUBSTANTIAL SUBSET IS FLIPPED. The two feeds do not share")
        print("      an orientation and the transform must account for it.")
    else:
        print("  ==> single shared orientation; no per-period flip to correct.")

    # ---- 2. attack direction -------------------------------------------
    print("\n" + "-" * 78)
    print("CHECK 2 — is the target net inferred correctly?")
    print("-" * 78)
    nn = d.dropna(subset=["pbp_net_x", "our_net_x"])
    agree = np.sign(nn.pbp_net_x) == np.sign(nn.our_net_x)
    print(f"  comparable goals: {len(nn):,}")
    print(f"  our net sign == PBP net sign: {agree.mean():.2%}")
    if agree.mean() < 0.98:
        print("  ==> TARGET NET DISAGREES more often than rounding can explain.")
        bad = nn[~agree]
        print(f"      {len(bad):,} disagreements; median our shot distance "
              f"{bad.our_shot_dist.median():.1f} ft")
    else:
        print("  ==> attack direction is being inferred correctly.")

    # ---- 3. is the chosen frame the release? ----------------------------
    print("\n" + "-" * 78)
    print("CHECK 3 — is the chosen frame the RELEASE?")
    print("-" * 78)
    print("  Distance from the PBP's recorded shot location to what we tracked:")
    for lab, col in (("puck at our shot frame", "err_same"),):
        s = d[col]
        print(f"    {lab:<26} median {s.median():5.1f}  "
              f"p75 {s.quantile(.75):5.1f}  p90 {s.quantile(.90):5.1f}  "
              f"p99 {s.quantile(.99):6.1f}")
    sc = d.dropna(subset=["scorer_x"])
    es = np.hypot(sc.scorer_x - sc.pbp_x, sc.scorer_y - sc.pbp_y)
    print(f"    {'scorer at our frame':<26} median {es.median():5.1f}  "
          f"p75 {es.quantile(.75):5.1f}  p90 {es.quantile(.90):5.1f}  "
          f"p99 {es.quantile(.99):6.1f}   (n={len(sc):,})")

    print("\n  Agreement bands (puck vs PBP location):")
    for t in (5, 10, 15, 20, 30, 50):
        print(f"    within {t:>2} ft: {(d.err_same <= t).mean():6.1%}")

    # ---- 4. where does it fail? ----------------------------------------
    print("\n" + "-" * 78)
    print("CHECK 4 — which goals does it get wrong?")
    print("-" * 78)
    d["bad"] = d.err_same > 20
    d["dist_bin"] = pd.cut(d.our_shot_dist, [0, 15, 25, 35, 50, 70, 100, 200],
                           labels=["0-15", "15-25", "25-35", "35-50", "50-70",
                                   "70-100", "100+"])
    print(d.groupby("dist_bin", observed=True).agg(
        n=("bad", "size"), pct_wrong=("bad", "mean"),
        median_err=("err_same", "median"),
        median_lead_s=("lead_s", "median")).round(3).to_string())
    if "empty_net" in d:
        print("\n  by empty net:")
        print(d.groupby("empty_net").agg(
            n=("bad", "size"), pct_wrong=("bad", "mean"),
            median_err=("err_same", "median")).round(3).to_string())
    print("\n  by detection method:")
    print(d.groupby("method").agg(
        n=("bad", "size"), pct_wrong=("bad", "mean"),
        median_err=("err_same", "median")).round(3).to_string())
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--seasons", nargs="*", default=SEASONS)
    ap.add_argument("--sample", type=int, default=None,
                    help="goals per season; omit for all")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    lay = pc.Layout(args.root)
    df = collect(lay, args.seasons, args.sample)
    d = report(df)
    if args.out:
        d.to_parquet(args.out, index=False)
        print(f"\n  wrote {args.out}")


if __name__ == "__main__":
    main()
