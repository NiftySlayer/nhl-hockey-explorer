# On-disk layout and output schema

## Layout

Every path is resolved through `pipeline_common.Layout`, relative to whatever
you pass as `--root`. Nothing constructs paths by hand.

```
raw/                                    NEVER overwritten after first write
  sprites/{season}/{game}/ev{event}.json    goal tracking, one file per goal
  pbp/{season}/{game}.json                  play-by-play
  shifts/{season}/{game}.json               shift charts
  shifts_html/{season}/{game}_{TV|TH}.HTM   HTML TOI fallback
  edge/{season}/{skater|goalie}/{player}.json

processed/                              re-derivable from raw/, always
  players.parquet
  games/{season}.parquet
  faceoffs/{season}.parquet
  shots/{season}.parquet
  shifts/{season}.parquet
  events/{season}.parquet
  stints/{season}.parquet
  stint_players/{season}.parquet
  edge_skaters/{season}.parquet
  edge_goalies/{season}.parquet

audit/
  completeness_{season}.parquet             one row per play-by-play goal
  fetch_log_{season}.jsonl                  per-request scrape log
  edge_log_{season}.jsonl
```

**`raw/` is immutable.** Bytes are written verbatim, only on HTTP 200, via a
temp-file-then-rename so an interrupted write never leaves a truncated file. The
scraper checks for an existing file before fetching, so re-running is free.

**Everything in `processed/` and `audit/` is derived.** If a parse rule changes,
re-run the build against `raw/` — never re-fetch. That separation is the whole
reason raw bytes are stored unparsed.

Seasons are the 8-digit NHL form: `20242025`.

---

## `processed/events/{season}.parquet`

**One row per (goal × on-ice player).** The tracking output — this is the table
most consumers want.

| Column | Type | Notes |
|---|---|---|
| `season`, `game_id`, `event_id` | str, int, int | `event_id` joins to the sprite filename and the play-by-play `eventId` |
| `player_id` | int | **The join key.** Universal NHL player id |
| `team_id`, `team_abbrev`, `sweater_number` | int, str, int | Display fields — never join on these |
| `position_code` | str | From the play-by-play roster; `"G"` for goalies |
| `is_goalie`, `is_scorer` | bool | |
| `is_scoring_team` | bool | Whether this player's team scored |
| `scoring_team_id`, `home_team_id` | int | |
| `situation_code` | str | 4 digits, e.g. `"1551"` = 5v5 |
| **`d_shotframe`** | float | **Distance in feet to the puck at the inferred shot release**, averaged over ±2 frames. The primary measurement |
| `d_goalframe` | float | Same, at the goal instant. Outcome-conditioned — see [METHODS §7.1](METHODS.md). Provided for comparison, not for use |
| `shot_lead_seconds` | float | Seconds between the detected release and the goal. Median 0.9 s. Use this to resolve roster/strength/score state at the shot rather than the goal |

Either distance may be null when that player had no valid coordinates in the
relevant frame window. Rows are the union of players seen at either frame.

---

## `audit/completeness_{season}.parquet`

**One row per goal in the play-by-play**, whether or not tracking resolved. This
is a deliverable, not a log — filter your analysis on it.

**Identity**

| Column | Notes |
|---|---|
| `season`, `game_id`, `event_id` | |
| `pbp_scorer_id`, `scoring_team_id`, `home_team_id`, `away_team_id` | |
| `shot_type`, `situation_code`, `period`, `time_in_period` | |

**Did tracking resolve**

| Column | Notes |
|---|---|
| `sprite_exists`, `parsed` | bool |
| `frame_count` | Frames in the sprite |
| `onice_entities_goalframe`, `n_onice_goalframe` | Sane values are 11–13. Large numbers mean a bench emptied |
| `puck_has_coords_goalframe` | bool |
| `frames_back_to_valid_puck` | How far the walk-back went to find a valid puck |
| `frames_trimmed_dead` | Frames of post-goal dead time removed ([METHODS §5](METHODS.md)) |
| `goalframe_index`, `shotframe_index` | Chosen frame indices |
| `note` | Free text: `sprite-missing-or-403`, `no-valid-puck-in-any-frame`, `scorer-not-tracked`, etc. |

**Shot detection**

| Column | Notes |
|---|---|
| `shot_method` | `local-min` (a real puck contact) or `global-min` (fallback — **56.7% of these are more than 20 ft off**) |
| `scorer_dist_shotframe` | Puck-to-scorer at the chosen frame. Median 2.73 ft |
| `frames_before_goal` | Detection lead. A long lead is the signature of a wrong frame |
| `shot_net_dist_ft` | Implied shot distance to the target net. Median 19.7 ft |
| `shot_netward` | Did the puck close on the net immediately after? 93.1% true |
| `shot_n_contacts` | Separate local minima found. >1 means a rebound or multiple touches |
| `net_source` | `pbp` (from `homeTeamDefendingSide`) or `puck-sign` (fallback) |
| `nearest_ft_goalframe`, `nearest_ft_shotframe` | Nearest-player distance at each frame |
| `tracking_shooter_goalframe`, `tracking_shooter_shotframe` | Nearest player's id at each frame. **Not a shooter identification** — see the two rows below |
| `scorer_match_goalframe` | Is the nearest player at the goal frame the recorded scorer? **4.6%.** Expected, and the reason the goal frame is not the measurement point ([METHODS §7.1](METHODS.md)) |
| `scorer_match_shotframe` | Same at the shot frame. **74.2%** — and a consistency check only, since the frame is anchored on the scorer. Use `shot_pbp_err_ft` below for actual accuracy |

**Confidence**

| Column | Notes |
|---|---|
| `shot_pbp_err_ft` | **The independent accuracy measure.** Feet between the tracked puck at the chosen frame and the play-by-play's own recorded shot location. Median 2.04 ft, 90.1% within 10 ft; nothing in the detection uses it, so it can genuinely fail |
| `shot_confidence` | `high` — real contact *and* within 20 ft of the PBP location. `low` — no genuine contact, or more than 20 ft off. `unverified` — real contact but no PBP coordinates to check against. `none` — no shot frame found |

---

## `processed/stints/{season}.parquet`

**One row per stint** — a maximal interval with no substitution.

| Column | Notes |
|---|---|
| `game_id`, `stint_idx` | Composite key; `stint_idx` is sequential within a game |
| `start_abs_seconds`, `end_abs_seconds`, `duration_seconds` | Absolute seconds from puck drop |
| `home_team_id`, `away_team_id` | |
| `n_home_skaters`, `n_away_skaters` | Skaters only, goalies excluded |
| `home_goalie_on`, `away_goalie_on` | **Check these before calling a stint even strength** ([METHODS §8](METHODS.md)) |
| `strength_home` | Convenience label from the home perspective, e.g. `"5v5"`, `"5v4"` |
| `goals_home`, `goals_away` | Goals in `(start, end]` |
| `score_diff_home` | Score differential **before** this stint |
| `zone_start_home` | `O` / `D` / `N` from the home perspective, or `OTF` when the change happened during play |
| `post_pp_home`, `post_pk_home` | Even-strength stint beginning within 15 s of unequal strength ending, with the sign recording who had been up a skater |
| `b2b_home`, `b2b_away` | Did that team play the previous calendar day? |

`processed/stint_players/{season}.parquet` is the companion: `game_id`,
`stint_idx`, `player_id`, `team_id`, `is_home`, `is_goalie` — one row per
player on the ice for that stint.

---

## Other tables

**`processed/shots/{season}.parquet`** — one row per shot attempt (goal, shot on
goal, missed shot, blocked shot): `season`, `game_id`, `event_id`, `event_type`,
`period`, `time_in_period`, `abs_game_seconds`, `situation_code`,
`home_team_id`, `away_team_id`, `home_team_defending_side`, `shooting_player_id`,
`scoring_player_id`, `blocking_player_id`, `event_owner_team_id`,
`goalie_in_net_id`, `shot_type`, `zone_code`, `x_coord`, `y_coord`.

*For blocked shots, `event_owner_team_id` is the **blocking** team.*

**`processed/shifts/{season}.parquet`** — one row per player shift: `season`,
`game_id`, `player_id`, `team_id`, `period`, `start_time`, `end_time`,
`start_abs_seconds`, `end_abs_seconds`, `duration_seconds`, `shift_number`,
`source` (`json` or `html` — which feed it came from).

**`processed/players.parquet`** — `playerId`, `firstName`, `lastName`,
`positionCode`. Built from every raw play-by-play `rosterSpots` on disk, so it
spans all seasons present.

*Caveat: `positionCode` is last-write-wins across the archive. A forward listed
as C in one game and L in another resolves to whichever game was read last, so
the value can shift depending on how much of the archive is on disk. It is
reliable for the distinction the pipeline actually uses it for — goalie versus
skater — and should be treated as approximate for anything finer.*

**`processed/games/{season}.parquet`** — `season`, `game_id`, `game_date`,
`game_type`, `home_team_id`, `home_team_abbrev`, `away_team_id`,
`away_team_abbrev`.

**`processed/faceoffs/{season}.parquet`** — every faceoff with `x_coord`,
`y_coord`, `zone_code`, `event_owner_team_id`, `winning_player_id`,
`losing_player_id`, `home_team_defending_side`. Raw geometry only; resolving a
faceoff to an offensive or defensive zone start *per team* is left to the
consumer, who knows which perspective they want.

**`processed/edge_skaters` / `edge_goalies`** — flattened season aggregates.
Skaters: top shot speed, max skating speed, bursts over 20 mph, distance
skated, shots/goals/shooting %, zone-time shares. Goalies: GAA, goal
differential per 60, saves, goals against, save %.

---

## Conventions

- **Join on `player_id` / `playerId`.** Sweater number, team id and name are
  display fields ([METHODS §2](METHODS.md)).
- **Times are absolute game seconds** from puck drop, computed with a flat
  1,200 s period. Overtime ordering is therefore approximate; it is used for
  ordering within a game, not for wall-clock arithmetic.
- **Distances are in feet**, on standard rink coordinates: x ∈ [−100, 100],
  y ∈ [−42.5, 42.5], centre ice at the origin, goal lines at |x| = 89.
- **Regulation and overtime only.** Shootouts are excluded everywhere
  (`MAX_GOAL_PERIOD = 4`, `MAX_PERIOD = 4`).
