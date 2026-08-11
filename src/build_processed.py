#!/usr/bin/env python3
"""
build_processed.py — Offline transform: raw/ -> processed/ + audit/.

Reads ONLY the immutable raw archive (no network), so it is fast and fully
re-runnable. If the shot detector or a parse rule improves, re-run this; never
re-scrape (docs/SCHEMA.md).

Produces:
  processed/players.parquet            playerId master (join key; METHODS.md §2)
  processed/shots/{season}.parquet     every shot attempt in the play-by-play
  processed/shifts/{season}.parquet    per-player shift intervals
  processed/games/{season}.parquet     game dimension table (dates, teams)
  processed/faceoffs/{season}.parquet  every faceoff, for zone-start work
  processed/events/{season}.parquet    goal x player x distance. Carries BOTH
                                        d_goalframe (always available) and
                                        d_shotframe (the inferred release,
                                        METHODS.md §7), window-averaged, keyed
                                        on playerId
  audit/completeness_{season}.parquet  the reconciliation table (METHODS.md §9)
                                        plus per-goal shot-detection confidence

Opt-in, behind --tracking because it is by far the largest table:
  processed/tracking/{season}.parquet  goal x frame x entity. Every 0.1 s frame
                                        of every goal's sprite, one row per
                                        player and one for the puck, in absolute
                                        rink feet AND in attack-oriented
                                        coordinates with the conceding team's
                                        net always at x = +89

Shot-frame distances are SCORER-ANCHORED: the play-by-play names the scorer, so
we search for the frame where the puck was last at that player rather than
inferring a shooter from puck kinematics. The frames it picks put the puck a
median 2.04 ft from where the NHL scorer recorded the shot, 90% within 10 ft.
See docs/METHODS.md §7.

Usage (normally via run_pipeline.py, and from the repo root):
    python src/build_processed.py --root . --seasons 20242025
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

import pipeline_common as pc
import shifts_html

# Window-average each distance over center +/- 2 frames (METHODS.md §7.3).
SHOT_FRAME_WINDOW_HALF = 2


# ---------------------------------------------------------------------------
# Small sprite helpers (built on pipeline_common.to_std)
# ---------------------------------------------------------------------------

def load_json(path: Path):
    try:
        return json.loads(path.read_bytes())
    except (ValueError, OSError):
        return None


MAX_PLAUSIBLE_ONICE = 14      # both teams combined
MAX_PLAUSIBLE_PER_TEAM = 7    # 6 skaters + 1 goalie is the physical maximum


def valid_puck_frame_from_end(frames, max_entities=MAX_PLAUSIBLE_ONICE,
                              max_per_team=MAX_PLAUSIBLE_PER_TEAM):
    """
    Walk backward to the last frame whose puck ('1') has valid coords AND whose
    on-ice count is physically plausible. Returns (index, puck_x, puck_y,
    frames_back) or None.

    Two failure modes are handled:
      * null puck at the goal frame (~1% of goals) — step back to valid coords.
      * BENCH CELEBRATION: on overtime / game-winning goals the scoring team
        empties the bench and the tracker picks everyone up, so the final frames
        can carry 20-35 "on-ice" entities (observed: a 3v3 OT goal with 17 CGY
        players at the goal frame). Anchoring to the last frame with a plausible
        count puts us at the goal itself rather than mid-celebration.
    """
    for offset in range(len(frames) - 1, -1, -1):
        oi = frames[offset].get("onIce", {}) or {}
        p = oi.get("1")
        if p is None:
            continue
        px, py = pc.to_std(p.get("x"), p.get("y"))
        if px is None:
            continue
        n_players = sum(1 for eid in oi if eid != "1")
        if n_players > max_entities:
            continue
        # Per-team cap catches celebrations the total cap misses: a 3v3 OT
        # winner can show 14 total while one bench (8-10 players) has emptied.
        per_team = {}
        for eid, ent in oi.items():
            if eid == "1":
                continue
            t = ent.get("teamId")
            per_team[t] = per_team.get(t, 0) + 1
        if per_team and max(per_team.values()) > max_per_team:
            continue
        return offset, px, py, (len(frames) - 1 - offset)
    return None


def player_records_at(frame, puck_x, puck_y):
    """
    All on-ice non-puck entities in ONE frame with distance to the given puck
    point. Returns dict playerId -> {teamId, teamAbbrev, sweater, dist}.
    """
    out = {}
    oi = frame.get("onIce", {}) or {}
    for eid, ent in oi.items():
        if eid == "1":
            continue
        ex, ey = pc.to_std(ent.get("x"), ent.get("y"))
        if ex is None:
            continue
        pid = ent.get("playerId")
        if pid in (None, ""):
            continue
        out[pid] = {
            "team_id": ent.get("teamId"),
            "team_abbrev": ent.get("teamAbbrev"),
            "sweater_number": ent.get("sweaterNumber"),  # reference only, never a key
            "dist": math.hypot(ex - puck_x, ey - puck_y),
        }
    return out


def window_avg_distances(frames, center, half):
    """
    Per-player distance to puck, averaged over frames [center-half, center+half]
    where both the puck and that player have valid coords. Positions barely move
    frame-to-frame at release, so this only removes detection jitter
    (METHODS.md §7.3).

    IMPORTANT: the on-ice ROSTER is anchored to the CENTER frame. Averaging over
    a window would otherwise union everyone seen in any window frame, so a line
    change inside the window inflates the set (observed up to 35 "on-ice"
    players before this was anchored). We want the players on ice at the moment,
    with their distances smoothed — not everyone who appeared nearby in time.

    Returns dict playerId -> {team_id, team_abbrev, sweater_number, dist(mean)}.
    """
    center_oi = frames[center].get("onIce", {}) or {}
    roster = {
        ent.get("playerId")
        for eid, ent in center_oi.items()
        if eid != "1" and ent.get("playerId") not in (None, "")
    }
    if not roster:
        return {}

    acc = {}  # pid -> [sum, n, meta]
    lo = max(0, center - half)
    hi = min(len(frames) - 1, center + half)
    for i in range(lo, hi + 1):
        oi = frames[i].get("onIce", {}) or {}
        p = oi.get("1")
        if p is None:
            continue
        px, py = pc.to_std(p.get("x"), p.get("y"))
        if px is None:
            continue
        for pid, rec in player_records_at(frames[i], px, py).items():
            if pid not in roster:
                continue
            if pid not in acc:
                acc[pid] = [0.0, 0, rec]
            acc[pid][0] += rec["dist"]
            acc[pid][1] += 1
    out = {}
    for pid, (s, n, meta) in acc.items():
        out[pid] = {**meta, "dist": s / n}
    return out


def nearest_player(dist_map):
    """playerId with the smallest 'dist' in a records dict, or None."""
    best_pid, best_d = None, math.inf
    for pid, rec in dist_map.items():
        if rec["dist"] < best_d:
            best_d, best_pid = rec["dist"], pid
    return best_pid, (best_d if best_pid is not None else None)


# ---------------------------------------------------------------------------
# Scorer-anchored shot frame
# ---------------------------------------------------------------------------
# WHY THE MEASUREMENT POINT IS NOT THE GOAL FRAME
# -----------------------------------------------
# Measuring player-to-puck distance at the GOAL frame is outcome-conditioned by
# construction: the puck is in the net, so the nearest player is the conceding
# goalie 66% of the time and one of his defencemen another 14%. It is the
# recorded scorer only 4.6% of the time. Any statistic built on it inherits an
# asymmetry that mostly encodes which team scored.
#
# Anchoring inverts the problem. The play-by-play already tells us WHO scored,
# so we only need to find WHEN the puck was last at that player: a
# one-dimensional search with a known target rather than an inference. That
# leaves only the timing to validate, which shotframe_validation.py does against
# an outside source.
#
# METHOD: walk backwards from the goal frame over the scorer-puck distance and
# take the most recent LOCAL MINIMUM that is close enough to be a puck contact.
# "Most recent" matters: an early carry or a pass reception by the same player
# is also a local minimum, but the shot is the LAST time he had it.

SCORER_MAX_CONTACT_FT = 12.0   # puck within this of the scorer counts as contact
SCORER_LOCAL_HALF = 3          # +/- frames that must be no closer (local min)
NET_X = 89.0                   # goal line; std coords are +/-100 x, +/-42.5 y

# THE GOAL INSTANT IS NOT THE LAST FRAME.
# The sprite keeps recording after the puck goes in: it sits in the net for
# seconds of dead time while play stops. Observed on 2024-25 goals, the last
# valid frame is a median of ~40 frames (4 s) after the puck actually arrived.
# That matters twice over:
#   * the scorer skates to the net to celebrate, so his "closest approach to
#     the puck" lands AFTER the goal unless the search is bounded;
#   * d_goalframe was being measured during the celebration, not at the goal.
# So we locate the goal instant as the START of the final sustained run with
# the puck parked at the net, and use that as both the goal frame and the upper
# bound of the shot search.
# How close the tracked puck must sit to the PBP's own recorded shot location
# for the detection to count as verified. 20 ft is deliberately loose: the PBP
# coordinate is placed by eye and is itself only accurate to a few feet, so this
# is a check for gross failure (wrong touch, wrong end) rather than precision.
# 96.5% of goals clear it; the ones that do not are concentrated in the
# `global-min` fallback and beyond 70 ft.
SHOT_PBP_AGREE_FT = 20.0

NET_ARRIVAL_FT = 3.0           # puck this close to the net counts as "in it"
NET_ARRIVAL_RUN = 4            # frames it must stay there to count as arrival

# PRIMARY RULE: find where the puck DIES.
# Motion is a better signal than net proximity because it needs no estimate of
# which net or where it is -- the coordinates are absolute and a play can run
# toward either end. Measured: the puck moves a median 2.0 ft/frame during live
# play and 0.07 ft/frame once it is dead in the net.
# So: walk back over the terminal run of frames where the puck is not moving;
# the goal instant is where that run starts. Net proximity stays as a fallback
# for goals where the puck is retrieved immediately and never rests.
REST_FT_PER_FRAME = 0.6        # slower than this (6 ft/s) is a dead puck
REST_RUN = 5                   # frames it must stay dead to count as settled
GOAL_INSTANT_FALLBACKS = []    # diagnostic: how often the motion rule missed


def puck_rest_index(frames, gf_idx):
    """
    First frame of the terminal run in which the puck has stopped moving.
    Returns None if the puck never settles for `REST_RUN` frames.
    """
    pts = []
    for i in range(gf_idx + 1):
        x, y = _puck_xy(frames[i])
        if x is not None:
            pts.append((i, x, y))
    if len(pts) < REST_RUN + 2:
        return None

    disp = {}
    for k in range(1, len(pts)):
        i, x, y = pts[k]
        _, px, py = pts[k - 1]
        disp[i] = math.hypot(x - px, y - py)

    idx = [i for i, _, _ in pts[1:]]
    j = len(idx) - 1
    while j >= 0 and disp[idx[j]] < REST_FT_PER_FRAME:
        j -= 1
    n_dead = len(idx) - 1 - j
    if n_dead < REST_RUN:
        return None
    return idx[j + 1]


def goal_instant(frames, gf_idx):
    """
    When the goal actually happened, as opposed to the last recorded frame.

    Primary rule: the frame where the puck comes to rest (`puck_rest_index`).
    Fallback: the start of the final sustained run with the puck at the net.

    Returns (index, frames_trimmed).
    """
    rest = puck_rest_index(frames, gf_idx)
    if rest is not None:
        return rest, gf_idx - rest
    GOAL_INSTANT_FALLBACKS.append(1)
    gpx, _ = _puck_xy(frames[gf_idx])
    if gpx is None:
        return gf_idx, 0
    net_x = math.copysign(NET_X, gpx)

    d = {}
    for i in range(0, gf_idx + 1):
        px, py = _puck_xy(frames[i])
        if px is not None:
            d[i] = math.hypot(px - net_x, py)
    if not d:
        return gf_idx, 0

    idx = sorted(d)
    near = [i for i in idx if d[i] <= NET_ARRIVAL_FT]
    if not near:
        return gf_idx, 0

    # walk back from the last near-net frame to the start of its contiguous run
    end = near[-1]
    start = end
    pos = idx.index(end)
    while pos > 0:
        prev = idx[pos - 1]
        if d[prev] > NET_ARRIVAL_FT:
            break
        start = prev
        pos -= 1
    if end - start + 1 < NET_ARRIVAL_RUN:
        return gf_idx, 0
    return start, gf_idx - start


def _entity_xy(frame, pid):
    """(x, y) of one playerId in one frame, or (None, None)."""
    for eid, ent in (frame.get("onIce", {}) or {}).items():
        if eid == "1":
            continue
        if ent.get("playerId") == pid:
            return pc.to_std(ent.get("x"), ent.get("y"))
    return None, None


def _puck_xy(frame):
    p = (frame.get("onIce", {}) or {}).get("1")
    if p is None:
        return None, None
    return pc.to_std(p.get("x"), p.get("y"))


def scorer_anchored_shot_frame(frames, scorer_id, gf_idx,
                               max_contact=SCORER_MAX_CONTACT_FT,
                               local_half=SCORER_LOCAL_HALF,
                               net_x=None):
    """
    Find the frame where the puck was last at the PBP-identified scorer.

    Returns dict with keys:
        index          chosen frame, or None if the scorer is untrackable
        scorer_dist_ft puck-to-scorer distance at that frame
        method         'local-min' | 'global-min' | None
        n_contacts     how many separate local minima were under `max_contact`
                       (>1 means a rebound/second touch; useful as a flag)
        net_dist_ft    puck distance to the target net at that frame -- the
                       implied shot distance, and the main sanity check
        netward        did the puck move toward that net right after? (bool)
    """
    out = {"index": None, "scorer_dist_ft": None, "method": None,
           "n_contacts": 0, "net_dist_ft": None, "netward": None,
           "net_source": None}
    if scorer_id is None or gf_idx is None:
        return out

    gi_idx, _ = goal_instant(frames, gf_idx)

    # scorer-to-puck distance in every frame up to the goal instant
    d = {}
    for i in range(0, gi_idx + 1):
        px, py = _puck_xy(frames[i])
        if px is None:
            continue
        sx, sy = _entity_xy(frames[i], scorer_id)
        if sx is None:
            continue
        d[i] = math.hypot(sx - px, sy - py)
    if not d:
        return out

    # TARGET NET — PBP FIRST, PUCK SIGN ONLY AS FALLBACK.
    #
    # Reading the net off the sign of puck-x at the goal frame assumes the puck
    # is still in the net there. On 251 of 23,888 goals (1.05%) it is not: the
    # puck is fished out and sent back up ice before the clip ends, so it never
    # comes to rest, the dead-run trim in goal_instant() finds nothing, the goal
    # frame stays at the last frame, and by then puck-x has crossed centre ice.
    # Those goals came out at a median 166 ft when the PBP put them at 16 ft.
    #
    # `homeTeamDefendingSide` fixes the attacking direction from the PBP with no
    # dependence on any tracking frame. Using it lifts the correlation between
    # our implied shot distance and the PBP's own from 0.78 to 0.90. The
    # puck-sign rule stays as a fallback for goals missing the field.
    # See docs/METHODS.md §7.2.
    if net_x is None:
        gpx, _ = _puck_xy(frames[gf_idx])
        net_x = math.copysign(NET_X, gpx) if gpx is not None else None
        out["net_source"] = "puck-sign" if net_x is not None else None
    else:
        out["net_source"] = "pbp"

    idx = sorted(d)
    # bound the search at the goal instant, not the last frame: after the puck
    # is in the net the scorer closes on it to celebrate, which would otherwise
    # be selected as his "last touch".
    last = gi_idx - 1

    def is_local_min(i):
        di = d[i]
        for j in range(i - local_half, i + local_half + 1):
            if j != i and j in d and d[j] < di:
                return False
        return True

    contacts = [i for i in idx if i <= last and d[i] <= max_contact
                and is_local_min(i)]
    out["n_contacts"] = len(contacts)

    if contacts:
        chosen = contacts[-1]          # the LAST touch before the goal
        out["method"] = "local-min"
    else:
        cand = [i for i in idx if i <= last]
        if not cand:
            return out
        chosen = min(cand, key=lambda i: d[i])
        out["method"] = "global-min"

    out["index"] = chosen
    out["scorer_dist_ft"] = round(d[chosen], 2)

    px, py = _puck_xy(frames[chosen])
    if px is not None and net_x is not None:
        out["net_dist_ft"] = round(math.hypot(px - net_x, py), 2)
        # did the puck close on the net over the next few frames?
        for j in range(chosen + 1, min(chosen + 6, gf_idx + 1)):
            qx, qy = _puck_xy(frames[j])
            if qx is None:
                continue
            out["netward"] = math.hypot(qx - net_x, qy) < out["net_dist_ft"]
            break
    return out


# ---------------------------------------------------------------------------
# players.parquet  (master, all seasons found on disk)
# ---------------------------------------------------------------------------

def build_players(lay: pc.Layout):
    """
    Player master from every raw PBP rosterSpots on disk. Keyed on playerId
    (METHODS.md §2). Returns {playerId: positionCode} for goalie tagging.
    """
    seen = {}
    for pbp_path in sorted((lay.root / "raw" / "pbp").glob("*/*.json")):
        pbp = load_json(pbp_path)
        if not isinstance(pbp, dict):
            continue
        for rs in pbp.get("rosterSpots", []):
            pid = rs.get("playerId")
            if pid in (None, ""):
                continue
            fn = (rs.get("firstName") or {}).get("default")
            ln = (rs.get("lastName") or {}).get("default")
            seen[pid] = {
                "playerId": pid,
                "firstName": fn,
                "lastName": ln,
                "positionCode": rs.get("positionCode"),
            }
    df = pd.DataFrame(sorted(seen.values(), key=lambda r: r["playerId"]))
    out = lay.players()
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    print(f"    players.parquet: {len(df)} unique playerIds")
    return {r["playerId"]: r["positionCode"] for r in seen.values()}


# ---------------------------------------------------------------------------
# games / faceoffs {season}.parquet  (context: back-to-backs, zone starts)
# ---------------------------------------------------------------------------

def build_games(lay: pc.Layout, season):
    """
    One row per game: date + home/away ids. Feeds the back-to-back control
    (a team playing on consecutive dates) and supplies home/away context.
    """
    rows = []
    for pbp_path in sorted(lay.raw_pbp_dir(season).glob("*.json")):
        pbp = load_json(pbp_path)
        if not isinstance(pbp, dict):
            continue
        home = pbp.get("homeTeam") or {}
        away = pbp.get("awayTeam") or {}
        rows.append({
            "season": season,
            "game_id": pbp.get("id"),
            "game_date": pbp.get("gameDate"),
            "game_type": pbp.get("gameType"),
            "home_team_id": home.get("id"),
            "home_team_abbrev": home.get("abbrev"),
            "away_team_id": away.get("id"),
            "away_team_abbrev": away.get("abbrev"),
        })
    df = pd.DataFrame(rows)
    lay.games(season).parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(lay.games(season), index=False)
    print(f"    games/{season}.parquet: {len(df)} games")
    return len(df)


def build_faceoffs(lay: pc.Layout, season):
    """
    Every faceoff, for the zone-start control. Stores raw geometry facts only
    (coords, zoneCode, which side home defends, who owns the event); resolving
    them to offensive/defensive zone starts PER TEAM is done in the modeling
    layer, where the team perspective is known.
    """
    rows = []
    for pbp_path in sorted(lay.raw_pbp_dir(season).glob("*.json")):
        pbp = load_json(pbp_path)
        if not isinstance(pbp, dict):
            continue
        game = pbp.get("id")
        for play in pbp.get("plays", []):
            if play.get("typeDescKey") != "faceoff":
                continue
            d = play.get("details", {}) or {}
            period = (play.get("periodDescriptor") or {}).get("number")
            tip = play.get("timeInPeriod")
            rows.append({
                "season": season,
                "game_id": game,
                "event_id": play.get("eventId"),
                "period": period,
                "time_in_period": tip,
                "abs_game_seconds": pc.abs_game_seconds(period, tip),
                "x_coord": d.get("xCoord"),
                "y_coord": d.get("yCoord"),
                "zone_code": d.get("zoneCode"),
                "event_owner_team_id": d.get("eventOwnerTeamId"),
                "winning_player_id": d.get("winningPlayerId"),
                "losing_player_id": d.get("losingPlayerId"),
                # geometry needed to map a faceoff to each team's O/D zone
                "home_team_defending_side": play.get("homeTeamDefendingSide"),
            })
    df = pd.DataFrame(rows)
    lay.faceoffs(season).parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(lay.faceoffs(season), index=False)
    print(f"    faceoffs/{season}.parquet: {len(df)} faceoffs")
    return len(df)


# ---------------------------------------------------------------------------
# edge_skaters / edge_goalies {season}.parquet  (season-level Edge stats)
# ---------------------------------------------------------------------------

def _nav(d, *path):
    """Safe nested lookup: _nav(d,'a','b') -> d['a']['b'] or None."""
    cur = d
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _loc(summary, code, field):
    """Pull `field` from the list-row whose locationCode == code (e.g. 'all')."""
    if not isinstance(summary, list):
        return None
    for row in summary:
        if isinstance(row, dict) and row.get("locationCode") == code:
            return row.get(field)
    return None


def build_edge(lay: pc.Layout, season):
    """Flatten raw Edge skater/goalie JSON into two compact season tables."""
    # --- skaters ---
    srows = []
    sdir = lay.root / "raw" / "edge" / season / "skater"
    for path in sorted(sdir.glob("*.json")):
        j = load_json(path)
        if not isinstance(j, dict):
            continue
        p = j.get("player", {}) or {}
        srows.append({
            "season": season,
            "player_id": p.get("id"),
            "position": p.get("position"),
            "team_abbrev": _nav(p, "team", "abbrev"),
            "games_played": p.get("gamesPlayed"),
            "top_shot_speed_mph": _nav(j, "topShotSpeed", "imperial"),
            "top_shot_speed_pctile": _nav(j, "topShotSpeed", "percentile"),
            "skating_speed_max_mph": _nav(j, "skatingSpeed", "speedMax", "imperial"),
            "skating_speed_max_pctile": _nav(j, "skatingSpeed", "speedMax", "percentile"),
            "bursts_over_20": _nav(j, "skatingSpeed", "burstsOver20", "value"),
            "total_distance_skated_mi": _nav(j, "totalDistanceSkated", "imperial"),
            "total_distance_pctile": _nav(j, "totalDistanceSkated", "percentile"),
            "sog_all": _loc(j.get("sogSummary"), "all", "shots"),
            "goals_all": _loc(j.get("sogSummary"), "all", "goals"),
            "shooting_pctg": _loc(j.get("sogSummary"), "all", "shootingPctg"),
            "off_zone_pctg": _nav(j, "zoneTimeDetails", "offensiveZonePctg"),
            "def_zone_pctg": _nav(j, "zoneTimeDetails", "defensiveZonePctg"),
            "neu_zone_pctg": _nav(j, "zoneTimeDetails", "neutralZonePctg"),
        })
    sdf = pd.DataFrame(srows)
    lay.edge_skaters(season).parent.mkdir(parents=True, exist_ok=True)
    sdf.to_parquet(lay.edge_skaters(season), index=False)

    # --- goalies ---
    grows = []
    gdir = lay.root / "raw" / "edge" / season / "goalie"
    for path in sorted(gdir.glob("*.json")):
        j = load_json(path)
        if not isinstance(j, dict):
            continue
        p = j.get("player", {}) or {}
        grows.append({
            "season": season,
            "player_id": p.get("id"),
            "position": p.get("position"),
            "team_abbrev": _nav(p, "team", "abbrev"),
            "games_played": p.get("gamesPlayed"),
            "goals_against_avg": _nav(j, "stats", "goalsAgainstAvg", "value"),
            "goal_diff_per60": _nav(j, "stats", "goalDifferentialPer60", "value"),
            "point_pctg": _nav(j, "stats", "pointPctg", "value"),
            "saves_all": _loc(j.get("shotLocationSummary"), "all", "saves"),
            "goals_against_all": _loc(j.get("shotLocationSummary"), "all", "goalsAgainst"),
            "save_pctg_all": _loc(j.get("shotLocationSummary"), "all", "savePctg"),
        })
    gdf = pd.DataFrame(grows)
    lay.edge_goalies(season).parent.mkdir(parents=True, exist_ok=True)
    gdf.to_parquet(lay.edge_goalies(season), index=False)

    print(f"    edge_skaters/{season}.parquet: {len(sdf)} skaters | "
          f"edge_goalies/{season}.parquet: {len(gdf)} goalies")
    return len(sdf), len(gdf)


# ---------------------------------------------------------------------------
# shots/{season}.parquet  (every shot attempt in the play-by-play)
# ---------------------------------------------------------------------------

def build_shots(lay: pc.Layout, season):
    rows = []
    for pbp_path in sorted(lay.raw_pbp_dir(season).glob("*.json")):
        pbp = load_json(pbp_path)
        if not isinstance(pbp, dict):
            continue
        game = pbp.get("id")
        home_id = (pbp.get("homeTeam") or {}).get("id")
        away_id = (pbp.get("awayTeam") or {}).get("id")
        for play in pbp.get("plays", []):
            t = play.get("typeDescKey")
            if t not in pc.SHOT_EVENT_TYPES:
                continue
            d = play.get("details", {}) or {}
            period = (play.get("periodDescriptor") or {}).get("number")
            tip = play.get("timeInPeriod")
            rows.append({
                "season": season,
                "game_id": game,
                "event_id": play.get("eventId"),
                "event_type": t,
                "period": period,
                "time_in_period": tip,
                "abs_game_seconds": pc.abs_game_seconds(period, tip),
                "situation_code": play.get("situationCode"),
                "home_team_id": home_id,
                "away_team_id": away_id,
                "home_team_defending_side": play.get("homeTeamDefendingSide"),
                # Shooter/scorer attribution, kept raw so the consumer decides
                # who owns the attempt. Worth knowing: for a blocked shot this
                # feed's owner team is the BLOCKING team, not the shooting one,
                # so both the team and the shooter id are stored.
                "shooting_player_id": d.get("shootingPlayerId"),
                "scoring_player_id": d.get("scoringPlayerId"),
                "blocking_player_id": d.get("blockingPlayerId"),
                "event_owner_team_id": d.get("eventOwnerTeamId"),
                "goalie_in_net_id": d.get("goalieInNetId"),
                "shot_type": d.get("shotType"),
                "zone_code": d.get("zoneCode"),
                "x_coord": d.get("xCoord"),
                "y_coord": d.get("yCoord"),
            })
    df = pd.DataFrame(rows)
    out = lay.shots(season)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    print(f"    shots/{season}.parquet: {len(df)} shot attempts")
    return len(df)


# ---------------------------------------------------------------------------
# shifts/{season}.parquet  (stints)
# ---------------------------------------------------------------------------

def build_shifts(lay: pc.Layout, season):
    """
    Stints from the JSON shiftcharts feed, with the HTML TOI reports as a
    fallback for games the JSON feed serves empty (2024-25 games 1235-1291).
    Both sources emit the same schema; a `source` column records which.
    """
    rows = []
    games_with_json = set()
    shift_dir = lay.root / "raw" / "shifts" / season
    for path in sorted(shift_dir.glob("*.json")):
        payload = load_json(path)
        if not isinstance(payload, dict):
            continue
        for s in payload.get("data", []):
            start = s.get("startTime")
            end = s.get("endTime")
            # Real shift rows have start/end; the feed also carries goal marker
            # rows (no usable interval) — drop those.
            if not start or not end:
                continue
            period = s.get("period")
            start_abs = pc.abs_game_seconds(period, start)
            end_abs = pc.abs_game_seconds(period, end)
            if start_abs is None or end_abs is None or end_abs <= start_abs:
                continue
            rows.append({
                "season": season,
                "game_id": s.get("gameId"),
                "player_id": s.get("playerId"),
                "team_id": s.get("teamId"),
                "period": period,
                "start_time": start,
                "end_time": end,
                "start_abs_seconds": start_abs,
                "end_abs_seconds": end_abs,
                "duration_seconds": end_abs - start_abs,
                "shift_number": s.get("shiftNumber"),
                "source": "json",
            })
            games_with_json.add(str(s.get("gameId")))

    # --- HTML fallback for games the JSON feed serves empty ---
    n_html_games = 0
    for pbp_path in sorted(lay.raw_pbp_dir(season).glob("*.json")):
        game = pbp_path.stem
        if game in games_with_json:
            continue
        html_rows, note = shifts_html.parse_game(lay, season, game)
        if html_rows:
            rows.extend(html_rows)
            n_html_games += 1

    df = pd.DataFrame(rows)
    out = lay.shifts_table(season)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    ngames = df["game_id"].nunique() if len(df) else 0
    print(f"    shifts/{season}.parquet: {len(df)} shifts across {ngames} games"
          f" ({n_html_games} games recovered from HTML reports)")
    return len(df)


# ---------------------------------------------------------------------------
# events/{season}.parquet + completeness_{season}.parquet
# ---------------------------------------------------------------------------

def process_goal(frames, goal, position_map, home_team_id=None):
    """
    Compute goal-frame and shot-frame distances for one goal's sprite.
    Returns (event_rows, audit_fields). event_rows is a list of per-player
    dicts; audit_fields is the per-goal completeness/confidence record.

    `home_team_id` lets the PBP fix the attacking direction (and therefore the
    target net) without relying on any tracking frame; see the note in
    scorer_anchored_shot_frame. Omitting it falls back to the puck-sign rule.
    """
    pbp_x, pbp_y = goal.get("x_coord"), goal.get("y_coord")
    pbp_net_x = pc.pbp_target_net_x(goal, home_team_id, NET_X)
    audit = {
        "parsed": True,
        "frame_count": len(frames),
        "onice_entities_goalframe": None,
        "n_onice_goalframe": None,
        "puck_has_coords_goalframe": False,
        "frames_back_to_valid_puck": None,
        "frames_trimmed_dead": None,
        "goalframe_index": None,
        "shotframe_index": None,
        "shot_method": None,
        "frames_before_goal": None,
        "nearest_ft_goalframe": None,
        "nearest_ft_shotframe": None,
        "tracking_shooter_goalframe": None,
        "tracking_shooter_shotframe": None,
        "scorer_match_goalframe": None,   # expected ~5%; see METHODS.md §7.1
        "scorer_match_shotframe": None,   # consistency check, not validation
        "scorer_dist_shotframe": None,    # puck-to-scorer at the chosen frame
        "shot_net_dist_ft": None,         # implied shot distance -- sanity check
        "shot_netward": None,             # did the puck close on the net after?
        "shot_n_contacts": None,          # >1 => rebound / multiple touches
        "shot_pbp_err_ft": None,          # INDEPENDENT: gap to the PBP's own xy
        "net_source": None,               # 'pbp' | 'puck-sign'
        "shot_confidence": None,
        "note": "",
    }

    gf = valid_puck_frame_from_end(frames)
    if gf is None:
        audit["parsed"] = True
        audit["note"] = "no-valid-puck-in-any-frame"
        return [], audit

    gf_last, gpx, gpy, frames_back = gf
    # The last valid frame is deep in the dead time after the goal (median ~4 s
    # on 2024-25). Anchor the goal frame on the puck's ARRIVAL at the net so
    # d_goalframe describes the goal rather than the celebration.
    gf_idx, trimmed = goal_instant(frames, gf_last)
    audit["goalframe_index"] = gf_idx
    audit["frames_trimmed_dead"] = trimmed
    audit["frames_back_to_valid_puck"] = frames_back
    audit["puck_has_coords_goalframe"] = True
    audit["onice_entities_goalframe"] = len(frames[gf_idx].get("onIce", {}) or {})

    scorer = goal.get("scoring_player_id")
    scoring_team = goal.get("event_owner_team_id")

    # --- Option A: goal-frame distances (window-averaged around goal frame) ---
    gf_dists = window_avg_distances(frames, gf_idx, SHOT_FRAME_WINDOW_HALF)
    audit["n_onice_goalframe"] = len(gf_dists)   # sane values are 11-13
    gf_near_pid, gf_near_d = nearest_player(gf_dists)
    audit["tracking_shooter_goalframe"] = gf_near_pid
    audit["nearest_ft_goalframe"] = round(gf_near_d, 2) if gf_near_d is not None else None
    if scorer is not None and gf_near_pid is not None:
        audit["scorer_match_goalframe"] = int(gf_near_pid == scorer)

    # --- Option B: SCORER-ANCHORED shot frame (the primary spec) ---
    # Anchored on the PBP scorer rather than inferred from puck kinematics; see
    # the note above scorer_anchored_shot_frame for why the goal frame cannot
    # serve as the measurement point.
    sf_dists = {}
    try:
        sa = scorer_anchored_shot_frame(frames, scorer, gf_idx,
                                        net_x=pbp_net_x)
        audit["shot_method"] = sa["method"]
        audit["scorer_dist_shotframe"] = sa["scorer_dist_ft"]
        audit["shot_net_dist_ft"] = sa["net_dist_ft"]
        audit["shot_netward"] = sa["netward"]
        audit["shot_n_contacts"] = sa["n_contacts"]
        audit["net_source"] = sa["net_source"]
        sf_idx = sa["index"]
        if sf_idx is not None:
            audit["shotframe_index"] = sf_idx
            audit["frames_before_goal"] = gf_idx - sf_idx
            # INDEPENDENT ACCURACY CHECK. How far is the puck, at the frame we
            # chose, from where the NHL scorer said the shot was taken? Nothing
            # in the detection uses this, so unlike the scorer-match statistic
            # it can actually fail. Median 2.0 ft over 23,888 goals; 90% within
            # 10 ft. It is the basis of the confidence flag below.
            spx, spy = _puck_xy(frames[sf_idx])
            if spx is not None and pbp_x is not None and pbp_y is not None:
                audit["shot_pbp_err_ft"] = round(
                    math.hypot(spx - pbp_x, spy - pbp_y), 2)
            sf_dists = window_avg_distances(frames, sf_idx, SHOT_FRAME_WINDOW_HALF)
            sf_near_pid, sf_near_d = nearest_player(sf_dists)
            audit["tracking_shooter_shotframe"] = sf_near_pid
            audit["nearest_ft_shotframe"] = (round(sf_near_d, 2)
                                             if sf_near_d is not None else None)
            if scorer is not None and sf_near_pid is not None:
                # NOT an independent validation any more (the frame is chosen to
                # put the puck at the scorer); it is a consistency check that the
                # window average did not drift off the anchor.
                audit["scorer_match_shotframe"] = int(sf_near_pid == scorer)
            # CONFIDENCE, graded against an outside source: not just whether we
            # found a real puck contact (which the detection is built to find and
            # so cannot fail informatively), but whether the frame we picked sits
            # where the NHL said the shot was. Failure modes it catches: the
            # `global-min` fallback is >20 ft wrong 57% of the time (n=150), and
            # goals we place beyond 70 ft are wrong 29% of the time, almost always
            # because the chosen frame is an earlier touch in the same possession.
            contact_ok = (sa["method"] == "local-min"
                          and sa["scorer_dist_ft"] is not None
                          and sa["scorer_dist_ft"] <= SCORER_MAX_CONTACT_FT)
            err = audit.get("shot_pbp_err_ft")
            if not contact_ok:
                audit["shot_confidence"] = "low"
            elif err is None:
                # no PBP coordinates to check against — contact-only evidence
                audit["shot_confidence"] = "unverified"
            elif err <= SHOT_PBP_AGREE_FT:
                audit["shot_confidence"] = "high"
            else:
                audit["shot_confidence"] = "low"
        else:
            audit["shot_confidence"] = "none"
            audit["note"] = (audit["note"] + ";scorer-not-tracked").strip(";")
    except Exception as e:  # detection is fallible by design; never crash a goal
        audit["note"] = (audit["note"] + f";shot-infer-failed:{e!r}").strip(";")

    # --- assemble per-player event rows (union of players seen at either frame) ---
    all_pids = set(gf_dists) | set(sf_dists)
    rows = []
    for pid in all_pids:
        meta = gf_dists.get(pid) or sf_dists.get(pid)
        pos = position_map.get(pid)
        rows.append({
            "player_id": pid,
            "team_id": meta.get("team_id"),
            "team_abbrev": meta.get("team_abbrev"),
            "sweater_number": meta.get("sweater_number"),  # reference only
            "position_code": pos,
            "is_goalie": (pos == "G"),
            "is_scorer": (pid == scorer),
            "is_scoring_team": (meta.get("team_id") == scoring_team)
                               if scoring_team is not None else None,
            "d_goalframe": round(gf_dists[pid]["dist"], 3) if pid in gf_dists else None,
            "d_shotframe": round(sf_dists[pid]["dist"], 3) if pid in sf_dists else None,
        })
    return rows, audit


def build_events_and_audit(lay: pc.Layout, season, position_map):
    event_rows = []
    audit_rows = []
    n_goals = n_sprites = 0

    for pbp_path in sorted(lay.raw_pbp_dir(season).glob("*.json")):
        pbp = load_json(pbp_path)
        if not isinstance(pbp, dict):
            continue
        game = pbp.get("id")
        home_id = (pbp.get("homeTeam") or {}).get("id")
        away_id = (pbp.get("awayTeam") or {}).get("id")
        for goal in pc.enumerate_goals(pbp):
            n_goals += 1
            eid = goal["event_id"]
            base = {
                "season": season,
                "game_id": game,
                "event_id": eid,
                "pbp_scorer_id": goal.get("scoring_player_id"),
                "scoring_team_id": goal.get("event_owner_team_id"),
                "home_team_id": home_id,
                "away_team_id": away_id,
                "shot_type": goal.get("shot_type"),
                "situation_code": goal.get("situation_code"),
                "period": goal.get("period"),
                "time_in_period": goal.get("time_in_period"),
            }
            sp_path = lay.sprite(season, game, eid)
            if not pc.exists_nonempty(sp_path):
                audit_rows.append({**base, "sprite_exists": False, "parsed": False,
                                   "note": "sprite-missing-or-403"})
                continue
            frames = load_json(sp_path)
            if not isinstance(frames, list) or not frames:
                audit_rows.append({**base, "sprite_exists": True, "parsed": False,
                                   "note": "sprite-unparseable-or-empty"})
                continue
            n_sprites += 1
            rows, audit = process_goal(frames, goal, position_map,
                                       home_team_id=home_id)
            audit_rows.append({**base, "sprite_exists": True, **audit})
            # Seconds between the shot release and the goal. The distances
            # describe the shot frame, so roster, strength and score state must
            # be resolved THERE, not at the goal (median 0.9 s later, IQR
            # 0.6-1.7 s -- long enough for a line change).
            lead = audit.get("frames_before_goal")
            shot_lead_s = (None if lead is None
                           else round(lead * pc.SECONDS_PER_FRAME, 2))
            for r in rows:
                event_rows.append({**{"season": season, "game_id": game,
                                      "event_id": eid,
                                      "scoring_team_id": base["scoring_team_id"],
                                      "home_team_id": home_id,
                                      "situation_code": base["situation_code"],
                                      "shot_lead_seconds": shot_lead_s},
                                   **r})

    ev = pd.DataFrame(event_rows)
    au = pd.DataFrame(audit_rows)
    lay.events(season).parent.mkdir(parents=True, exist_ok=True)
    lay.completeness(season).parent.mkdir(parents=True, exist_ok=True)
    ev.to_parquet(lay.events(season), index=False)
    au.to_parquet(lay.completeness(season), index=False)

    # Detection summary (METHODS.md §7.4). The shot-frame figure is a
    # consistency check, not proof: the frame is anchored on the scorer, so it
    # cannot fail informatively. src/shotframe_validation.py is the test that
    # can, because it grades against the play-by-play's own coordinates.
    def match_rate(col):
        s = au[col].dropna() if col in au else pd.Series(dtype=float)
        return (100.0 * s.mean()) if len(s) else float("nan")

    print(f"    events/{season}.parquet: {len(ev)} (goal x player) rows "
          f"from {n_sprites}/{n_goals} goals with sprites")
    print(f"    completeness_{season}.parquet: {len(au)} goals")
    print(f"      scorer-match @ goal frame : {match_rate('scorer_match_goalframe'):.1f}%  "
          f"(expected ~5% — the puck is in the net, so the goalie is nearest)")
    print(f"      scorer-match @ shot frame : {match_rate('scorer_match_shotframe'):.1f}%  "
          f"(consistency check — see shotframe_validation.py)")
    return len(ev), len(au)


# ---------------------------------------------------------------------------
# tracking/{season}.parquet  (goal x frame x entity — the whole clip)
# ---------------------------------------------------------------------------
# events/{season}.parquet reduces each clip to two instants. This keeps all of
# it: every frame, every entity, in long format. ~140 frames x ~13 entities x
# ~8,000 goals is on the order of 14M rows per season, which is why it is
# opt-in and why it streams to disk rather than going through a DataFrame.
#
# TWO COORDINATE FRAMES PER ROW.
#   x, y          absolute standard rink feet (+/-100 x, +/-42.5 y), the PBP
#                 convention, straight out of pc.to_std().
#   x_att, y_att  the same point after flipping the rink so the ATTACKED net --
#                 the net of the team that conceded -- is always the one at
#                 x = +89.
# The flip is a 180-degree ROTATION (negate both x and y), not a mirror of x
# alone. Rotating preserves handedness, so a play down the left wing is still
# down the left wing after the flip; mirroring x would silently swap the wings
# on every goal that needed flipping.

TRACKING_FLUSH_ROWS = 100_000   # rows buffered before a parquet row group

TRACKING_SCHEMA = pa.schema([
    # --- goal (constant within a goal; dictionary-encodes to almost nothing) ---
    ("season", pa.string()),
    ("game_id", pa.int64()),
    ("event_id", pa.int64()),
    ("period", pa.int32()),
    ("time_in_period", pa.string()),
    ("abs_game_seconds", pa.int32()),
    ("situation_code", pa.string()),
    ("shot_type", pa.string()),
    ("home_team_id", pa.int32()),
    ("away_team_id", pa.int32()),
    ("scoring_team_id", pa.int32()),
    ("conceding_team_id", pa.int32()),
    ("pbp_scorer_id", pa.int64()),
    ("net_x", pa.float32()),
    ("net_source", pa.string()),
    ("flip", pa.int8()),
    ("goal_frame_idx", pa.int32()),
    ("shot_frame_idx", pa.int32()),
    # --- frame ---
    ("frame_idx", pa.int32()),
    ("time_stamp", pa.int64()),
    ("seconds_to_goal", pa.float32()),
    ("seconds_to_shot", pa.float32()),
    ("is_goal_frame", pa.bool_()),
    ("is_shot_frame", pa.bool_()),
    ("n_onice", pa.int16()),
    # --- entity ---
    ("entity_id", pa.int32()),
    ("entity_type", pa.string()),
    ("player_id", pa.int64()),
    ("team_id", pa.int32()),
    ("team_abbrev", pa.string()),
    ("sweater_number", pa.int32()),
    ("position_code", pa.string()),
    ("is_goalie", pa.bool_()),
    ("is_scorer", pa.bool_()),
    ("is_assist1", pa.bool_()),
    ("is_assist2", pa.bool_()),
    ("is_scoring_team", pa.bool_()),
    ("is_home", pa.bool_()),
    # --- coordinates ---
    ("x", pa.float32()),
    ("y", pa.float32()),
    ("x_att", pa.float32()),
    ("y_att", pa.float32()),
    ("dist_to_net_ft", pa.float32()),
    ("angle_to_net_deg", pa.float32()),
    ("dist_to_puck_ft", pa.float32()),
])


class _BatchedParquetWriter:
    """
    Stream row dicts into one parquet file in fixed-schema batches.

    Every other builder here collects a list of dicts and hands it to
    pd.DataFrame in one go. At 14M rows that costs well over 10 GB, so this
    fans each row out into per-column lists and flushes a row group every
    `flush_rows`. The schema is declared rather than inferred because an
    all-null chunk (say a flush in which every goal fell back to a null
    net_source) would otherwise infer a different type and be rejected.
    """

    def __init__(self, path: Path, schema, flush_rows=TRACKING_FLUSH_ROWS):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.schema = schema
        self.flush_rows = flush_rows
        self.n_rows = 0
        self._cols = {name: [] for name in schema.names}
        self._buffered = 0
        self._writer = pq.ParquetWriter(path, schema, compression="snappy")

    def append(self, row: dict):
        for name, col in self._cols.items():
            col.append(row.get(name))
        self._buffered += 1
        self.n_rows += 1
        if self._buffered >= self.flush_rows:
            self.flush()

    def flush(self):
        if not self._buffered:
            return
        self._writer.write_table(
            pa.Table.from_pydict(self._cols, schema=self.schema))
        for col in self._cols.values():
            col.clear()
        self._buffered = 0

    def close(self):
        self.flush()
        self._writer.close()


def _round(v, digits):
    """round(), None-safe: an unresolvable coordinate stays null, not 0.0."""
    return None if v is None else round(v, digits)


def _as_int(v):
    """Feed ints, None-safe. The puck's id/team/sweater fields arrive as ''."""
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def locate_goal_and_shot_frames(frames, goal, home_team_id):
    """
    Goal instant, shot frame and target net for one goal's sprite.

    Mirrors the first half of process_goal(): same helpers, same order, same
    arguments, so the indices returned here equal the goalframe_index and
    shotframe_index already published in audit/completeness_{season}.parquet.

    process_goal() is deliberately NOT refactored into a shared code path. Its
    outputs are measured and quoted throughout docs/METHODS.md §7 (median
    2.04 ft from the PBP's own shot coordinate, the confidence-grade rates), and
    what is duplicated below is ten lines of call sequencing, not logic — every
    function it calls is the one process_goal calls. The equality of the two is
    asserted against the audit table rather than enforced by shared code.

    One deliberate difference: the net is resolved here even when the shot
    detection bails (no scorer in the PBP, or a scorer who never appears in a
    frame). process_goal only records a net_source when the detector ran, but
    every row of the tracking table needs a net to flip against.

    Returns (gf_idx, sf_idx, net_x, net_source); any of them may be None.
    """
    gf = valid_puck_frame_from_end(frames)
    if gf is None:
        return None, None, None, None
    gf_idx, _ = goal_instant(frames, gf[0])

    # PBP first, puck sign only as fallback — see the note in
    # scorer_anchored_shot_frame for why the sign rule is not trusted alone.
    net_x = pc.pbp_target_net_x(goal, home_team_id, NET_X)
    net_source = "pbp" if net_x is not None else None
    if net_x is None:
        gpx, _ = _puck_xy(frames[gf_idx])
        if gpx is not None:
            net_x, net_source = math.copysign(NET_X, gpx), "puck-sign"

    # Passing net_x in is what process_goal does. It cannot change which frame
    # is chosen — net_x only feeds the reported net_dist_ft/netward fields, not
    # the scorer-distance search — so the index still matches the audit table
    # on goals where the PBP had no defending side.
    sf_idx = None
    try:
        sf_idx = scorer_anchored_shot_frame(
            frames, goal.get("scoring_player_id"), gf_idx, net_x=net_x)["index"]
    except Exception:  # detection is fallible by design; never crash a goal
        pass
    return gf_idx, sf_idx, net_x, net_source


def tracking_rows(frames, goal, position_map, base, gf_idx, sf_idx, net_x,
                  net_source):
    """Yield one row dict per entity per frame for a single goal's sprite."""
    flip = None if net_x is None else (1 if net_x > 0 else -1)
    scorer = goal.get("scoring_player_id")
    a1, a2 = goal.get("assist1_player_id"), goal.get("assist2_player_id")
    scoring_team = base["scoring_team_id"]
    home_id = base["home_team_id"]

    goal_meta = {
        **base,
        "net_x": net_x,
        "net_source": net_source,
        "flip": flip,
        "goal_frame_idx": gf_idx,
        "shot_frame_idx": sf_idx,
    }

    def offset_s(fi, anchor):
        if anchor is None:
            return None
        return round((fi - anchor) * pc.SECONDS_PER_FRAME, 2)

    for fi, frame in enumerate(frames):
        oi = frame.get("onIce") or {}
        if not oi:
            continue
        puck_x, puck_y = _puck_xy(frame)
        frame_meta = {
            **goal_meta,
            "frame_idx": fi,
            "time_stamp": _as_int(frame.get("timeStamp")),
            "seconds_to_goal": offset_s(fi, gf_idx),
            "seconds_to_shot": offset_s(fi, sf_idx),
            # Plain booleans, never null, so they work as masks. "No shot frame
            # was detected" is carried by a null shot_frame_idx (and a null
            # seconds_to_shot), not by a null flag -- a nullable boolean column
            # cannot be used to index a DataFrame, which for a table meant to be
            # filtered on these two is a wart not worth the extra distinction.
            "is_goal_frame": fi == gf_idx,
            "is_shot_frame": fi == sf_idx,
            # Counts the entities the tracker reported, NOT a legal roster. On
            # overtime and game-winning goals the scoring bench empties and gets
            # picked up, so frames after the goal can carry 20-35 entities (see
            # valid_puck_frame_from_end). Those frames are in this table by
            # design; filter on seconds_to_goal <= 0 or on this count.
            "n_onice": sum(1 for k in oi if k != "1"),
        }

        for key, ent in oi.items():
            ex, ey = pc.to_std(ent.get("x"), ent.get("y"))
            if ex is None:
                continue
            is_puck = (key == "1")
            pid = None if is_puck else _as_int(ent.get("playerId"))
            if not is_puck and pid is None:
                continue    # untagged entity; matches player_records_at
            team_id = None if is_puck else _as_int(ent.get("teamId"))
            pos = position_map.get(pid) if pid is not None else None

            if flip is None:
                x_att = y_att = dist_net = angle_net = None
            else:
                x_att, y_att = ex * flip, ey * flip
                dx = NET_X - x_att
                dist_net = math.hypot(dx, y_att)
                # 0 deg is straight out from the net along the centre line,
                # growing toward +y_att.
                angle_net = math.degrees(math.atan2(y_att, dx))

            # NOT window-averaged, unlike the d_* columns in events/: this is a
            # continuous series, so smoothing is the consumer's decision.
            d_puck = (None if is_puck or puck_x is None
                      else math.hypot(ex - puck_x, ey - puck_y))

            yield {
                **frame_meta,
                "entity_id": _as_int(key),
                "entity_type": "puck" if is_puck else "player",
                "player_id": pid,
                "team_id": team_id,
                "team_abbrev": (None if is_puck
                                else (ent.get("teamAbbrev") or None)),
                "sweater_number": (None if is_puck
                                   else _as_int(ent.get("sweaterNumber"))),
                "position_code": pos,
                "is_goalie": None if is_puck else (pos == "G"),
                "is_scorer": None if is_puck else (pid == scorer),
                "is_assist1": None if is_puck else (pid == a1),
                "is_assist2": None if is_puck else (pid == a2),
                "is_scoring_team": (None if is_puck or scoring_team is None
                                    else team_id == scoring_team),
                "is_home": (None if is_puck or home_id is None
                            else team_id == home_id),
                "x": round(ex, 3),
                "y": round(ey, 3),
                "x_att": _round(x_att, 3),
                "y_att": _round(y_att, 3),
                "dist_to_net_ft": _round(dist_net, 3),
                "angle_to_net_deg": _round(angle_net, 2),
                "dist_to_puck_ft": _round(d_puck, 3),
            }


def build_tracking(lay: pc.Layout, season, position_map):
    """
    Every frame of every goal's sprite, long format, one row per entity.

    Reads only raw/, like the rest of the build, so it is re-runnable offline.
    Goals whose sprite is missing or unparseable are skipped silently: they are
    already accounted for, with a reason, in audit/completeness_{season}.parquet.
    """
    out = lay.tracking(season)
    writer = _BatchedParquetWriter(out, TRACKING_SCHEMA)
    n_goals = n_sprites = n_frames = 0

    try:
        for pbp_path in sorted(lay.raw_pbp_dir(season).glob("*.json")):
            pbp = load_json(pbp_path)
            if not isinstance(pbp, dict):
                continue
            game = pbp.get("id")
            home_id = (pbp.get("homeTeam") or {}).get("id")
            away_id = (pbp.get("awayTeam") or {}).get("id")
            for goal in pc.enumerate_goals(pbp):
                n_goals += 1
                eid = goal["event_id"]
                sp_path = lay.sprite(season, game, eid)
                if not pc.exists_nonempty(sp_path):
                    continue
                frames = load_json(sp_path)
                if not isinstance(frames, list) or not frames:
                    continue
                n_sprites += 1
                n_frames += len(frames)

                scoring_team = goal.get("event_owner_team_id")
                conceding = None
                if scoring_team is not None and None not in (home_id, away_id):
                    conceding = away_id if scoring_team == home_id else home_id
                period = goal.get("period")
                tip = goal.get("time_in_period")
                base = {
                    "season": season,
                    "game_id": game,
                    "event_id": eid,
                    "period": period,
                    "time_in_period": tip,
                    "abs_game_seconds": pc.abs_game_seconds(period, tip),
                    "situation_code": goal.get("situation_code"),
                    "shot_type": goal.get("shot_type"),
                    "home_team_id": home_id,
                    "away_team_id": away_id,
                    "scoring_team_id": scoring_team,
                    "conceding_team_id": conceding,
                    "pbp_scorer_id": goal.get("scoring_player_id"),
                }
                gf_idx, sf_idx, net_x, net_source = locate_goal_and_shot_frames(
                    frames, goal, home_id)
                for row in tracking_rows(frames, goal, position_map, base,
                                         gf_idx, sf_idx, net_x, net_source):
                    writer.append(row)

                if n_sprites % 1000 == 0:
                    print(f"      {n_sprites} sprites, {writer.n_rows} rows...",
                          flush=True)
    finally:
        writer.close()

    print(f"    tracking/{season}.parquet: {writer.n_rows} "
          f"(goal x frame x entity) rows from {n_sprites}/{n_goals} goals "
          f"with sprites ({n_frames} frames)")
    return writer.n_rows


# ---------------------------------------------------------------------------

def build_season(lay: pc.Layout, season, position_map, tracking=False):
    print(f"\n=== BUILD season {season} ===", flush=True)
    build_games(lay, season)
    build_faceoffs(lay, season)
    build_shots(lay, season)
    build_shifts(lay, season)
    build_events_and_audit(lay, season, position_map)
    if (lay.root / "raw" / "edge" / season).exists():
        build_edge(lay, season)
    # Last, and only on request: it is the largest table by two orders of
    # magnitude and nothing else in the build depends on it.
    if tracking:
        build_tracking(lay, season, position_map)


def main():
    ap = argparse.ArgumentParser(description="raw -> processed build (offline).")
    ap.add_argument("--seasons", nargs="+", default=["20242025"])
    ap.add_argument("--root", default=".")
    ap.add_argument("--tracking", action="store_true",
                    help="also build processed/tracking/{season}.parquet, the "
                         "full goal x frame x entity clip table (~14M rows per "
                         "season). Off by default: it is much the largest and "
                         "slowest table and nothing else depends on it.")
    args = ap.parse_args()

    lay = pc.Layout(args.root)
    position_map = build_players(lay)  # global, from all raw pbp on disk
    for season in args.seasons:
        build_season(lay, season, position_map, tracking=args.tracking)


if __name__ == "__main__":
    main()
