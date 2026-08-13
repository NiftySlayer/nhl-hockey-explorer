#!/usr/bin/env python3
"""
stints.py — Reconstruct stint-level intervals from the shift charts.

A STINT is a maximal interval of a game with no substitutions: the players on
the ice are constant from start to end. It is the standard unit of observation
for on-ice impact work, and it is not published anywhere — it has to be
reconstructed by sweeping every shift start and end in a game, which is what
this module does.

BOUNDARY RULE (validated against the data, not assumed)
-------------------------------------------------------
A player is on ice at instant t iff `shift.start < t <= shift.end`. At a goal
the whistle blows: the outgoing line's shifts END at t while the incoming
line's shifts START at t. An inclusive-both-ends rule counts BOTH lines and
gives a median of 20 players on ice over 24,688 goals (mode 22); this rule gives
a median of 12 (mode 12), within 8-13 for 97.1% of goals.

Equivalently, for a stint spanning (a, b] a player is on ice iff
`shift.start <= a and shift.end >= b`, which the sweep below maintains.

OUTPUTS
-------
processed/stints/{season}.parquet          one row per stint (the observation)
processed/stint_players/{season}.parquet   stint x player (who was on ice)

Each stint row carries its duration, goals for/against, skater counts and
goalie flags (from which strength state follows), score state before the stint,
zone start, and a back-to-back flag for each team. Strength and score are left
as raw counts rather than labels so the consumer decides how to bucket them.
"""

from __future__ import annotations

import argparse
from collections import defaultdict

import numpy as np
import pandas as pd

import pipeline_common as pc

# Regulation/OT only; shootout "goals" are not real ice time (METHODS.md §9).
MAX_PERIOD = 4
NEUTRAL_ZONE_ABS_X = 25.0   # |x| below this is a neutral-zone faceoff dot

# A stint only has a real zone start if it begins at a stoppage. Beyond this
# tolerance the line changed during play — an "on the fly" start, which
# hockeystats.com treats as its own category rather than inheriting the zone of
# whatever faceoff happened to precede it.
FACEOFF_TOLERANCE_S = 2.0

# Shifts just after a power play ends carry a strong context effect —
# hockeystats.com calls the power-play-expiry control "by far the most
# important". We derive it from the strength sequence itself (no penalty
# parsing needed): an even-strength stint beginning within this window of an
# unequal-strength stint ending is flagged, with the sign recording who had
# been up a skater.
POST_PP_WINDOW_S = 15.0


def _zone_for_home(x_coord, home_defending_side):
    """
    Faceoff location -> 'O' / 'D' / 'N' from the HOME team's perspective.
    `home_defending_side` is per-play (teams switch ends each period).
    """
    if x_coord is None or home_defending_side not in ("left", "right"):
        return None
    if abs(x_coord) < NEUTRAL_ZONE_ABS_X:
        return "N"
    end = 1 if x_coord > 0 else -1
    home_def_end = 1 if home_defending_side == "right" else -1
    return "D" if end == home_def_end else "O"


def build_back_to_back(games):
    """{(team_id, game_id): bool} — did this team play the previous calendar day?"""
    g = games.copy()
    g["game_date"] = pd.to_datetime(g["game_date"])
    rows = []
    for tcol in ("home_team_id", "away_team_id"):
        rows.append(g[["game_id", "game_date", tcol]].rename(columns={tcol: "team_id"}))
    tg = pd.concat(rows).sort_values(["team_id", "game_date"])
    tg["prev"] = tg.groupby("team_id")["game_date"].shift(1)
    tg["b2b"] = (tg["game_date"] - tg["prev"]).dt.days.eq(1).fillna(False)
    return {(int(r.team_id), int(r.game_id)): bool(r.b2b) for r in tg.itertuples()}


def stints_for_game(game_id, shifts_g, goals_g, faceoffs_g, home_id, away_id,
                    goalie_ids):
    """
    Sweep one game's shifts into stints. Returns (stint_rows, player_rows).
    """
    # --- sweep change points ---
    starts = defaultdict(list)   # time -> [(pid, tid)]
    ends = defaultdict(list)
    for r in shifts_g.itertuples():
        starts[r.start_abs_seconds].append((r.player_id, r.team_id))
        ends[r.end_abs_seconds].append((r.player_id, r.team_id))
    cps = sorted(set(starts) | set(ends))
    if len(cps) < 2:
        return [], []

    # goals and faceoffs indexed by time for interval assignment
    goal_times = [(t, tid) for t, tid in
                  zip(goals_g.abs_game_seconds, goals_g.event_owner_team_id)]
    goal_times.sort()
    fo = faceoffs_g.sort_values("abs_game_seconds")
    fo_times = fo.abs_game_seconds.to_numpy()
    fo_x = fo.x_coord.to_numpy()
    fo_side = fo.home_team_defending_side.to_numpy()

    on_ice = {}          # pid -> tid
    stint_rows, player_rows = [], []
    gi = 0
    score_home = score_away = 0
    stint_idx = 0

    for i in range(len(cps) - 1):
        t0, t1 = cps[i], cps[i + 1]
        # apply changes AT t0: remove shifts ending here, add shifts starting here
        for pid, tid in ends.get(t0, []):
            on_ice.pop(pid, None)
        for pid, tid in starts.get(t0, []):
            on_ice[pid] = tid

        dur = t1 - t0
        if dur <= 0 or not on_ice:
            continue

        # goals in (t0, t1]
        gf_home = gf_away = 0
        while gi < len(goal_times) and goal_times[gi][0] <= t0:
            gi += 1
        j = gi
        while j < len(goal_times) and goal_times[j][0] <= t1:
            if goal_times[j][1] == home_id:
                gf_home += 1
            elif goal_times[j][1] == away_id:
                gf_away += 1
            j += 1

        # roster composition
        home_sk = [p for p, t in on_ice.items() if t == home_id and p not in goalie_ids]
        away_sk = [p for p, t in on_ice.items() if t == away_id and p not in goalie_ids]
        home_g = [p for p, t in on_ice.items() if t == home_id and p in goalie_ids]
        away_g = [p for p, t in on_ice.items() if t == away_id and p in goalie_ids]

        # zone start: only if this stint actually begins at a stoppage;
        # otherwise the change happened during play ("on the fly")
        k = np.searchsorted(fo_times, t0, side="right") - 1
        if k >= 0 and (t0 - fo_times[k]) <= FACEOFF_TOLERANCE_S:
            zone_home = _zone_for_home(fo_x[k], fo_side[k])
        else:
            zone_home = "OTF"

        stint_rows.append({
            "game_id": game_id,
            "stint_idx": stint_idx,
            "start_abs_seconds": t0,
            "end_abs_seconds": t1,
            "duration_seconds": dur,
            "home_team_id": home_id,
            "away_team_id": away_id,
            "n_home_skaters": len(home_sk),
            "n_away_skaters": len(away_sk),
            "home_goalie_on": len(home_g) > 0,
            "away_goalie_on": len(away_g) > 0,
            "goals_home": gf_home,
            "goals_away": gf_away,
            "score_diff_home": score_home - score_away,   # BEFORE this stint
            "zone_start_home": zone_home,
        })
        for p, t in on_ice.items():
            player_rows.append({
                "game_id": game_id,
                "stint_idx": stint_idx,
                "player_id": p,
                "team_id": t,
                "is_home": t == home_id,
                "is_goalie": p in goalie_ids,
            })
        score_home += gf_home
        score_away += gf_away
        stint_idx += 1

    _flag_post_special_teams(stint_rows)
    return stint_rows, player_rows


def _flag_post_special_teams(rows):
    """
    Mark even-strength stints that begin shortly after unequal strength ends.
    `post_pp_home` = home had been up a skater; `post_pk_home` = home had been
    down one. Derived from the strength sequence, so no penalty parsing needed
    (and it captures penalties ended by a goal as well as by expiry).
    """
    last_end = None
    last_adv = 0
    for r in rows:
        adv = r["n_home_skaters"] - r["n_away_skaters"]
        r["post_pp_home"] = False
        r["post_pk_home"] = False
        if adv != 0:
            last_end = r["end_abs_seconds"]
            last_adv = 1 if adv > 0 else -1
        elif last_end is not None and \
                (r["start_abs_seconds"] - last_end) <= POST_PP_WINDOW_S:
            r["post_pp_home"] = last_adv > 0
            r["post_pk_home"] = last_adv < 0


def build_stints(lay: pc.Layout, season):
    # Unlike the rest, this step reads processed/ rather than raw/, so it is the
    # one that breaks when the steps are run out of order.
    if not lay.shifts_table(season).exists():
        raise SystemExit(
            f"\nNo shift table for {season}: {lay.shifts_table(season)}\n"
            f"Stints are swept from the built tables, not from raw/, so run the "
            f"build first:\n"
            f"    python src/run_pipeline.py --root {lay.root} "
            f"--steps build --seasons {season}\n")
    print(f"\n=== STINTS {season} ===", flush=True)
    shifts = pd.read_parquet(lay.shifts_table(season))
    games = pd.read_parquet(lay.games(season))
    shots = pd.read_parquet(lay.shots(season))
    faceoffs = pd.read_parquet(lay.faceoffs(season))
    players = pd.read_parquet(lay.players())
    goalie_ids = set(players.loc[players.positionCode == "G", "playerId"])

    shifts = shifts[shifts.period <= MAX_PERIOD]
    goals = shots[(shots.event_type == "goal") & (shots.period <= MAX_PERIOD)]
    faceoffs = faceoffs[faceoffs.period <= MAX_PERIOD]

    ginfo = {int(r.game_id): (int(r.home_team_id), int(r.away_team_id))
             for r in games.itertuples()}
    sh_by = dict(tuple(shifts.groupby("game_id")))
    go_by = dict(tuple(goals.groupby("game_id")))
    fo_by = dict(tuple(faceoffs.groupby("game_id")))

    all_s, all_p = [], []
    empty_fo = pd.DataFrame({"abs_game_seconds": [], "x_coord": [],
                             "home_team_defending_side": []})
    empty_go = pd.DataFrame({"abs_game_seconds": [], "event_owner_team_id": []})
    for n, (gid, sg) in enumerate(sh_by.items(), 1):
        gid = int(gid)
        if gid not in ginfo:
            continue
        home_id, away_id = ginfo[gid]
        s, p = stints_for_game(gid, sg, go_by.get(gid, empty_go),
                               fo_by.get(gid, empty_fo), home_id, away_id,
                               goalie_ids)
        all_s.extend(s)
        all_p.extend(p)
        if n % 300 == 0:
            print(f"    {n}/{len(sh_by)} games, {len(all_s)} stints", flush=True)

    st = pd.DataFrame(all_s)
    sp = pd.DataFrame(all_p)

    # back-to-back flags per team-game
    b2b = build_back_to_back(games)
    st["b2b_home"] = [b2b.get((h, g), False) for h, g in zip(st.home_team_id, st.game_id)]
    st["b2b_away"] = [b2b.get((a, g), False) for a, g in zip(st.away_team_id, st.game_id)]

    # strength label from the home perspective, e.g. "5v5", "5v4"
    st["strength_home"] = (st.n_home_skaters.astype(str) + "v"
                           + st.n_away_skaters.astype(str))

    for path, df, name in ((lay.stints(season), st, "stints"),
                           (lay.stint_players(season), sp, "stint_players")):
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path, index=False)
    print(f"    stints/{season}.parquet: {len(st)} stints across "
          f"{st.game_id.nunique()} games")
    print(f"    stint_players/{season}.parquet: {len(sp)} stint-player rows")
    return st, sp


def reconcile(lay: pc.Layout, season):
    """Sanity checks that must pass before anything is built on top of stints."""
    st = pd.read_parquet(lay.stints(season))
    sp = pd.read_parquet(lay.stint_players(season))
    shifts = pd.read_parquet(lay.shifts_table(season))
    shots = pd.read_parquet(lay.shots(season))
    goals = shots[(shots.event_type == "goal") & (shots.period <= MAX_PERIOD)]

    print(f"\n=== RECONCILIATION {season} ===")
    n_goals_stints = st.goals_home.sum() + st.goals_away.sum()
    print(f"  goals in stints : {n_goals_stints}   (PBP regulation+OT goals: {len(goals)})")

    # per-player TOI: stint durations vs raw shift durations
    dur = st.set_index(["game_id", "stint_idx"]).duration_seconds
    sp2 = sp.join(dur, on=["game_id", "stint_idx"])
    toi_stint = sp2.groupby("player_id").duration_seconds.sum() / 60
    toi_shift = shifts[shifts.period <= MAX_PERIOD].groupby("player_id").duration_seconds.sum() / 60
    j = pd.concat([toi_stint.rename("stint"), toi_shift.rename("shift")], axis=1).dropna()
    j["diff"] = j.stint - j["shift"]
    print(f"  players compared: {len(j)}")
    print(f"  TOI corr stint vs shift: {j.stint.corr(j['shift']):.6f}")
    print(f"  median abs diff: {j['diff'].abs().median():.3f} min | "
          f"95th pct: {j['diff'].abs().quantile(.95):.3f} min")

    print(f"  stint duration: mean {st.duration_seconds.mean():.1f}s "
          f"median {st.duration_seconds.median():.1f}s")
    print(f"  strength mix (top 6): ")
    print(st.groupby("strength_home").duration_seconds.sum().div(3600).nlargest(6).round(1).to_string())
    print(f"  zone starts: {dict(st.zone_start_home.value_counts(dropna=False))}")
    print(f"  b2b stints: home {st.b2b_home.mean():.3f} away {st.b2b_away.mean():.3f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", nargs="+", default=["20242025"])
    ap.add_argument("--root", default=".")
    args = ap.parse_args()
    lay = pc.Layout(args.root)
    for s in args.seasons:
        build_stints(lay, s)
        reconcile(lay, s)


if __name__ == "__main__":
    main()
