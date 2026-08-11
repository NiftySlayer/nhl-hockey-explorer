#!/usr/bin/env python3
"""
pipeline_common.py — Shared plumbing for the NHL tracking-data pipeline.

Everything the scrape and build steps both need lives here: the browser
headers the endpoints require, the endpoint templates, a polite HTTP getter
with retry/backoff, the coordinate transform, time helpers, the on-disk
layout (docs/SCHEMA.md), and a small JSONL logger.

No network side effects at import time. Import-safe for both steps.

Endpoints (all confirmed live 2026-07-18):
  PBP     https://api-web.nhle.com/v1/gamecenter/{game}/play-by-play
  SPRITE  https://wsr.nhle.com/sprites/{season}/{game}/ev{event}.json   (goals only)
  SHIFTS  https://api.nhle.com/stats/rest/en/shiftcharts?cayenneExp=gameId={game}
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Constants — headers & endpoints (docs/METHODS.md §1)
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://www.nhl.com/",
    "Origin": "https://www.nhl.com",
    "Accept": "application/json",
}

PBP_URL = "https://api-web.nhle.com/v1/gamecenter/{game}/play-by-play"
SPRITE_URL = "https://wsr.nhle.com/sprites/{season}/{game}/ev{event}.json"
SHIFTS_URL = (
    "https://api.nhle.com/stats/rest/en/shiftcharts?cayenneExp=gameId={game}"
)
# NHL Edge season-level per-player stats (the nhlpy `client.edge.*` endpoints,
# base host api-web.nhle.com/v1). game_type 2 = regular season. A player with
# no Edge data for that season returns a clean 404 (terminal, logged, skipped).
# Edge coverage runs 2021-22 -> current (further back than the sprite floor).
EDGE_SKATER_URL = (
    "https://api-web.nhle.com/v1/edge/skater-detail/{player}/{season}/{game_type}"
)
EDGE_GOALIE_URL = (
    "https://api-web.nhle.com/v1/edge/goalie-detail/{player}/{season}/{game_type}"
)

# Usable sprite floor is 2023-24 (docs/DATA_SOURCES.md §1). 2024-25 is listed
# first because it is the most complete season on the feed, so an interrupted
# overnight run still leaves a usable archive behind.
ARCHIVE_SEASONS = ["20242025", "20232024", "20252026"]

DEFAULT_DELAY = 0.7  # seconds between requests (docs/METHODS.md §1: 0.5-1 s)

# Shot-attempt event types — the standard Corsi set. Goals included; a goal is
# also a shot attempt.
SHOT_EVENT_TYPES = ("goal", "shot-on-goal", "missed-shot", "blocked-shot")

# Frame rate is fixed at 10 fps (docs/METHODS.md §4). The per-frame timeStamp
# is deciseconds since the Unix epoch and steps exactly +1 per frame, so it
# agrees -- but it is used for ordering only and dt is hardcoded, so a feed that
# ever changed units could not silently rescale every distance in the archive.
SECONDS_PER_FRAME = 0.1

# THE MEASUREMENT POINT FOR PLAYER-TO-PUCK DISTANCE.
# 'd_shotframe' = scorer-anchored shot release. This is the one to use.
# 'd_goalframe' = puck at the net. Emitted alongside it, but only as a
# documented variant: it is outcome-conditioned, since the puck being in the
# net mechanically makes the conceding side's defencemen the nearest players
# (docs/METHODS.md §7.1).
DISTANCE_COL = "d_shotframe"


PERIOD_SECONDS = 1200  # 20:00 regulation period, for absolute game time


# ---------------------------------------------------------------------------
# HTTP — polite, retrying, never-raising
# ---------------------------------------------------------------------------

class Fetched:
    """Result of one HTTP GET. `ok` == HTTP 200 with a body."""

    __slots__ = ("status", "content", "json", "error", "attempts")

    def __init__(self, status, content, json_obj, error, attempts):
        self.status = status          # int HTTP status, or None on network error
        self.content = content        # raw bytes (verbatim), or None
        self.json = json_obj          # parsed JSON, or None
        self.error = error            # repr of a network/parse error, or None
        self.attempts = attempts

    @property
    def ok(self):
        return self.status == 200 and self.content is not None


def get(url, *, delay=DEFAULT_DELAY, max_retries=4, timeout=25, parse_json=True):
    """
    GET `url` with browser headers, retrying transient failures with
    exponential backoff. Sleeps `delay` seconds AFTER the request returns
    (rate limiting; the caller does not need to sleep).

    Terminal (no retry): 403 (sprite absent for this event) and 404 (game
    absent). These are normal, expected outcomes — logged, not retried.
    Retried: network errors, timeouts, 429, and 5xx.

    Never raises. Returns a Fetched.
    """
    last = Fetched(None, None, None, "not-attempted", 0)
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
        except requests.RequestException as e:
            last = Fetched(None, None, None, repr(e), attempt)
            _backoff(attempt)
            continue

        status = r.status_code
        if status in (403, 404):
            _sleep(delay)
            return Fetched(status, None, None, None, attempt)

        if status == 200:
            content = r.content
            parsed = None
            perr = None
            if parse_json:
                try:
                    parsed = r.json()
                except ValueError as e:
                    perr = f"json-parse:{e!r}"
            _sleep(delay)
            return Fetched(status, content, parsed, perr, attempt)

        # 429 / 5xx / anything else transient → retry with backoff
        last = Fetched(status, None, None, f"http-{status}", attempt)
        _backoff(attempt)

    _sleep(delay)
    return last


def _backoff(attempt):
    # 0.8, 1.6, 3.2, ... seconds
    time.sleep(min(0.8 * (2 ** (attempt - 1)), 15.0))


def _sleep(delay):
    if delay and delay > 0:
        time.sleep(delay)


# ---------------------------------------------------------------------------
# Coordinate & time transforms
# ---------------------------------------------------------------------------

def to_std(raw_x, raw_y):
    """Inches-from-corner -> feet-from-center (docs/METHODS.md §3). None-safe.

    Raw coordinates are inches from the rink corner at standard (-100, +42.5);
    raw x spans 0-2400 in (200 ft) and raw y spans 0-1020 in (85 ft).

    THE Y AXIS IS INVERTED RELATIVE TO THE RAW FEED. The sprite's raw y grows
    toward the opposite board from the NHL play-by-play convention, so the
    intuitive `raw_y/12 - 42.5` mirrors the rink about the centre line. Measured
    on 2,500 goals against the PBP's own recorded shot coordinates: with the
    negation the tracked puck sits a median 1.9 ft from where the NHL scorer
    placed the shot and 91% are within 10 ft; without it, 15.7 ft and 35%, and
    the y correlation is -0.943 instead of +0.943
    (src/shotframe_validation.py, check 1).

    THE SIGN IS INVISIBLE TO EVERY DISTANCE THIS PIPELINE EMITS. Distances
    reduce y to a `math.hypot` and the target net sits at y = 0: player-to-puck
    distance negates both operands, so the difference is unchanged, and
    distance-to-net uses |y| alone. The sign matters only to things that read it
    directly — rink maps, left/right-side splits, and joins against PBP
    coordinates — so it is fixed here at the source rather than at each use.
    """
    if raw_x is None or raw_y is None or raw_x == "" or raw_y == "":
        return None, None
    return (raw_x / 12.0) - 100.0, 42.5 - (raw_y / 12.0)


def mmss_to_seconds(mmss):
    """'MM:SS' -> int seconds. None/'' -> None."""
    if not mmss:
        return None
    try:
        m, s = mmss.split(":")
        return int(m) * 60 + int(s)
    except (ValueError, AttributeError):
        return None


def abs_game_seconds(period, time_in_period_mmss):
    """
    Absolute seconds from puck drop, for ordering shots against shifts within
    one game. Uses a flat 20:00 period length (regulation). OT/shootout are
    rare in the goals set and ordering-only here, so the flat offset is fine;
    documented limitation, not a bug.
    """
    if period is None:
        return None
    secs = mmss_to_seconds(time_in_period_mmss)
    if secs is None:
        return None
    return (int(period) - 1) * PERIOD_SECONDS + secs


# ---------------------------------------------------------------------------
# On-disk layout (docs/SCHEMA.md). Raw is immutable; processed/audit re-derive.
# ---------------------------------------------------------------------------

class Layout:
    """Resolves every path in the archive relative to a base root."""

    def __init__(self, root="."):
        self.root = Path(root)

    # --- raw (written verbatim, never overwritten) ---
    def sprite(self, season, game, event):
        return self.root / "raw" / "sprites" / season / str(game) / f"ev{event}.json"

    def pbp(self, season, game):
        return self.root / "raw" / "pbp" / season / f"{game}.json"

    def shifts(self, season, game):
        return self.root / "raw" / "shifts" / season / f"{game}.json"

    def shifts_html(self, season, game, which):
        # which in {"TV","TH"} — visitor/home HTML TOI report (fallback source
        # for games where the JSON shiftcharts feed returns total=0)
        return self.root / "raw" / "shifts_html" / season / f"{game}_{which}.HTM"

    def edge(self, season, kind, player):
        # kind in {"skater", "goalie"}
        return self.root / "raw" / "edge" / season / kind / f"{player}.json"

    # --- processed (parquet, re-derivable from raw) ---
    def events(self, season):
        return self.root / "processed" / "events" / f"{season}.parquet"

    def shots(self, season):
        return self.root / "processed" / "shots" / f"{season}.parquet"

    def shifts_table(self, season):
        return self.root / "processed" / "shifts" / f"{season}.parquet"

    def players(self):
        return self.root / "processed" / "players.parquet"

    def games(self, season):
        return self.root / "processed" / "games" / f"{season}.parquet"

    def stints(self, season):
        return self.root / "processed" / "stints" / f"{season}.parquet"

    def stint_players(self, season):
        return self.root / "processed" / "stint_players" / f"{season}.parquet"

    def faceoffs(self, season):
        return self.root / "processed" / "faceoffs" / f"{season}.parquet"

    def tracking(self, season):
        # goal x frame x entity — the continuous clip, built opt-in
        return self.root / "processed" / "tracking" / f"{season}.parquet"

    def edge_skaters(self, season):
        return self.root / "processed" / "edge_skaters" / f"{season}.parquet"

    def edge_goalies(self, season):
        return self.root / "processed" / "edge_goalies" / f"{season}.parquet"

    # --- audit ---
    def completeness(self, season):
        return self.root / "audit" / f"completeness_{season}.parquet"

    def fetch_log(self, season):
        return self.root / "audit" / f"fetch_log_{season}.jsonl"

    def edge_log(self, season):
        return self.root / "audit" / f"edge_log_{season}.jsonl"

    def raw_pbp_dir(self, season):
        return self.root / "raw" / "pbp" / season

    def raw_sprite_game_dir(self, season, game):
        return self.root / "raw" / "sprites" / season / str(game)


def write_raw(path: Path, content: bytes):
    """
    Write raw bytes verbatim, creating parent dirs. Immutable-raw contract:
    the caller must have already checked the file does not exist. Writes to a
    temp file then renames so an interrupted write never leaves a truncated
    raw file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(content)
    tmp.replace(path)


def exists_nonempty(path: Path):
    return path.exists() and path.stat().st_size > 0


def append_jsonl(path: Path, row: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")


# ---------------------------------------------------------------------------
# Game enumeration & PBP parsing
# ---------------------------------------------------------------------------

def game_id(season, game_type, number):
    """NHL game id: {startYear}{type:02d}{number:04d}. type 2 = regular."""
    return f"{season[:4]}{int(game_type):02d}{int(number):04d}"


def season_players(layout: "Layout", season):
    """
    Every playerId that dressed in a season, from that season's raw PBP
    rosterSpots, with position. Returns {playerId: positionCode}. Used to
    enumerate Edge fetches per season (keyed on playerId, docs/METHODS.md §2).
    Requires the season's raw PBP to already be on disk.
    """
    out = {}
    for pbp_path in sorted(layout.raw_pbp_dir(season).glob("*.json")):
        try:
            pbp = json.loads(pbp_path.read_bytes())
        except (ValueError, OSError):
            continue
        for rs in pbp.get("rosterSpots", []):
            pid = rs.get("playerId")
            if pid not in (None, ""):
                out[pid] = rs.get("positionCode")
    return out


# Regulation + OT. Shootout deciders are recorded as goals in the PBP but are
# not real ice time and have no goal recreation (2024-25: 169 shootout goals,
# only 11 with a sprite). Counting them collapsed reported sprite coverage from
# 99.9% to 98.0%. stints.py already filtered on this; the audit did not.
MAX_GOAL_PERIOD = 4


def enumerate_goals(pbp, max_period=MAX_GOAL_PERIOD):
    """Goal events from a PBP payload, with the fields the audit/events need."""
    goals = []
    for play in pbp.get("plays", []):
        if play.get("typeDescKey") != "goal":
            continue
        per = (play.get("periodDescriptor") or {}).get("number")
        if max_period is not None and per is not None and per > max_period:
            continue
        d = play.get("details", {}) or {}
        goals.append(
            {
                "event_id": play.get("eventId"),
                "scoring_player_id": d.get("scoringPlayerId"),
                "assist1_player_id": d.get("assist1PlayerId"),
                "assist2_player_id": d.get("assist2PlayerId"),
                "event_owner_team_id": d.get("eventOwnerTeamId"),
                "goalie_in_net_id": d.get("goalieInNetId"),
                "shot_type": d.get("shotType"),
                # situationCode: 4 digits [awayGoalie, awaySkaters, homeSkaters,
                # homeGoalie], e.g. "1551" = 5v5. Authoritative strength state;
                # decoded on the modeling side relative to the scoring team.
                "situation_code": play.get("situationCode"),
                "period": (play.get("periodDescriptor") or {}).get("number"),
                "time_in_period": play.get("timeInPeriod"),
                # The PBP's OWN record of where the goal was scored from, as
                # placed by the NHL scorer who watched it. This is an
                # independent observation of the same event, and the only
                # non-circular way to check the tracking-inferred shot frame
                # (see src/shotframe_validation.py). `homeTeamDefendingSide`
                # fixes the attacking direction, which is what identifies the
                # target net -- inferring it from the puck's own x-sign fails
                # whenever the goal frame is unreliable.
                "x_coord": d.get("xCoord"),
                "y_coord": d.get("yCoord"),
                "zone_code": d.get("zoneCode"),
                "home_team_defending_side": play.get("homeTeamDefendingSide"),
            }
        )
    return goals


def pbp_target_net_x(goal, home_team_id, net_x=89.0):
    """Target-net x from the PBP alone: defending side + which team scored.

    `homeTeamDefendingSide` is the end the HOME team defends in that period, so
    the home team attacks the other one. Returns None when the field is absent,
    in which case the caller falls back to the puck-sign rule.
    """
    side = goal.get("home_team_defending_side")
    if side not in ("left", "right"):
        return None
    home_attacks = -net_x if side == "right" else net_x
    owner = goal.get("event_owner_team_id")
    if owner is None or home_team_id is None:
        return None
    return home_attacks if owner == home_team_id else -home_attacks
