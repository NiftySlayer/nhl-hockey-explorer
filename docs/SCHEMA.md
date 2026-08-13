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
  tracking/{season}.parquet                 opt-in; see --steps tracking
  stints/{season}.parquet
  stint_players/{season}.parquet
  edge_skaters/{season}.parquet
  edge_goalies/{season}.parquet

audit/
  completeness_{season}.parquet             one row per play-by-play goal
  fetch_log_{season}.jsonl                  per-request scrape log
  edge_log_{season}.jsonl
```

`raw/` is immutable. Bytes are written verbatim, only on HTTP 200, via a
temp-file-then-rename so an interrupted write never leaves a truncated file. The
scraper checks for an existing file before fetching, so re-running is free.

Everything in `processed/` and `audit/` is derived. If a parse rule changes,
re-run the build against `raw/` rather than re-fetching.

Seasons are the 8-digit NHL form: `20242025`.

---

## `processed/events/{season}.parquet`

One row per (goal × on-ice player). The tracking output.

| Column | Type | Notes |
|---|---|---|
| `season`, `game_id`, `event_id` | str, int, int | `event_id` joins to the sprite filename and the play-by-play `eventId` |
| `player_id` | int | The join key. Universal NHL player id |
| `team_id`, `team_abbrev`, `sweater_number` | int, str, int | Display fields — never join on these |
| `position_code` | str | From the play-by-play roster; `"G"` for goalies |
| `is_goalie`, `is_scorer` | bool | |
| `is_scoring_team` | bool | Whether this player's team scored |
| `scoring_team_id`, `home_team_id` | int | |
| `situation_code` | str | 4 digits, e.g. `"1551"` = 5v5 |
| **`d_shotframe`** | float | Distance in feet to the puck at the inferred shot release, averaged over ±2 frames. The primary measurement |
| `d_goalframe` | float | Same, at the goal instant. Outcome-conditioned — see [METHODS §7.1](METHODS.md). For comparison, not for use |
| `shot_lead_seconds` | float | Seconds between the detected release and the goal. Median 0.9 s. Use it to resolve roster/strength/score state at the shot rather than the goal |

Either distance may be null when that player had no valid coordinates in the
relevant frame window. Rows are the union of players seen at either frame.

---

## `processed/tracking/{season}.parquet`

One row per (goal × frame × entity) — the whole clip, not just the two instants
`events/` keeps. Every 0.1 s frame of every goal's sprite, one row per on-ice
player and one for the puck.

**Opt-in.** It is not built by `--steps all`, because it is two orders of
magnitude larger than anything else here and nothing else in the pipeline reads
it. Build it with either:

```
python src/run_pipeline.py    --root . --steps tracking --seasons 20242025
python src/build_processed.py --root . --seasons 20242025 --tracking
```

It reads only `raw/`, so it can run before, after, or without the rest of the
build. Goals with no sprite are absent; `audit/completeness_{season}.parquet`
says which and why.

Size, measured on 2024-25: **14,287,503 rows, 448 MB** snappy-compressed, from
7,895 goals with a parseable sprite and 1,101,720 frames — 12.1 tracked players
per frame plus the puck. It builds from an extracted `raw/` and streams to disk
in 100k-row groups rather than going through a DataFrame, so peak memory stays
flat regardless of how many seasons you throw at it. The other two seasons hold a comparable number of frames
(`frame_count` in the audit tables), so expect the same order.

**Goal** — constant within a goal.

| Column | Type | Notes |
|---|---|---|
| `season`, `game_id`, `event_id` | str, int, int | `event_id` joins to the sprite filename, the play-by-play `eventId`, `events/` and `completeness_` |
| `period`, `time_in_period`, `abs_game_seconds` | int, str, int | |
| `situation_code`, `shot_type` | str | |
| `home_team_id`, `away_team_id` | int | |
| `scoring_team_id`, `conceding_team_id` | int | |
| `pbp_scorer_id` | int | |
| `net_x` | float | x of the attacked net in absolute coordinates: `+89` or `−89` |
| `net_source` | str | `pbp` (from `homeTeamDefendingSide`) or `puck-sign` (fallback). See [METHODS §3](METHODS.md) |
| `flip` | int | `+1` or `−1`, the rotation applied to get the attack frame |
| `goal_frame_idx`, `shot_frame_idx` | int | The same indices as `goalframe_index` / `shotframe_index` in `completeness_`. `shot_frame_idx` is null when the scorer was never tracked |

**Frame**

| Column | Type | Notes |
|---|---|---|
| `frame_idx` | int | 0-based index into the sprite's frame array |
| `time_stamp` | int | The feed's own value: deciseconds since the Unix epoch, stepping exactly +1 per frame ([FIELD_REFERENCE](FIELD_REFERENCE.md)) |
| `seconds_to_goal` | float | `0.1 × (frame_idx − goal_frame_idx)`. Negative before the goal, positive in the post-goal dead time |
| `seconds_to_shot` | float | Same against `shot_frame_idx`; null when no shot frame was detected |
| `is_goal_frame`, `is_shot_frame` | bool | Never null, so they work directly as masks. Both are false on every frame of a goal whose index is null |
| `n_onice` | int | Non-puck entities the tracker reported in this frame. **Not a legal roster — read the caveat below** |

**Entity**

| Column | Type | Notes |
|---|---|---|
| `entity_id` | int | The sprite's own per-game tracking-tag id. **Never join on it** ([FIELD_REFERENCE](FIELD_REFERENCE.md)) |
| `entity_type` | str | `player` or `puck` |
| `player_id` | int | The join key. Null on puck rows |
| `team_id`, `team_abbrev`, `sweater_number` | int, str, int | Display fields. Null on puck rows |
| `position_code` | str | From the play-by-play roster; `"G"` for goalies |
| `is_goalie`, `is_scorer`, `is_assist1`, `is_assist2` | bool | Null on puck rows |
| `is_scoring_team`, `is_home` | bool | Null on puck rows |

**Coordinates** — all in feet.

| Column | Type | Notes |
|---|---|---|
| `x`, `y` | float | Absolute standard rink coordinates, nominally x ∈ [−100, 100], y ∈ [−42.5, 42.5], the same convention as every other table here. The feed reports up to **1.0 ft past** those bounds for a player against the boards — 0.02% of x and 0.40% of y values in 2024-25, never further out than that. Clamp if your geometry needs it |
| `x_att`, `y_att` | float | The attack frame: the rink rotated so the **attacked** net — the net of the team that conceded — is always the one at x = +89 |
| `dist_to_net_ft` | float | Feet to the attacked net, `hypot(89 − x_att, y_att)` |
| `angle_to_net_deg` | float | `degrees(atan2(y_att, 89 − x_att))`. 0° is straight out from the net along the centre line, growing toward +`y_att` |
| `dist_to_puck_ft` | float | Feet to the puck **in that same frame**. Null on puck rows and wherever the puck has no coordinates |

The attack-frame columns are null when the net could not be resolved at all —
23 goals of 7,895 in 2024-25 (38,358 rows, 0.27%). `dist_to_puck_ft` is *not*
window-averaged, unlike `d_shotframe` in `events/`: this is a continuous series,
so smoothing is the consumer's decision.

⚠️ **The table contains post-goal frames, and they are not clean.** The clip
keeps recording for seconds after the puck goes in, and on overtime and
game-winning goals the scoring team empties its bench into the tracker's view —
frames after the goal have been observed carrying 20–35 "on-ice" entities
([METHODS §6](METHODS.md)). Those frames are here by design; the goal instant is
marked, not enforced. Filter on `seconds_to_goal <= 0`, or on `n_onice`, before
treating a frame's entity set as a roster.

**Reading it in pandas.** `player_id`, `team_id`, `sweater_number` and
`shot_frame_idx` are stored as proper integers with nulls. Plain
`pd.read_parquet` converts those to `float64` (so a player id reads as
`8478402.0` and will not join against an int column); pass
`dtype_backend="pyarrow"`, or filter to `entity_type == "player"` first.

Do not read the season to get one instant out of it. A parquet predicate is
pushed down to the row groups and takes under a second:

```python
sf = pd.read_parquet("processed/tracking/20242025.parquet",
                     filters=[("is_shot_frame", "==", True)],
                     dtype_backend="pyarrow")     # 101,294 rows of 14.3M
```

### Is it right?

`src/tracking_validation.py` grades the published parquet — not the code that
wrote it — against the play-by-play and against the shot-frame tables it has to
agree with. All 13 checks pass on 2024-25:

```bash
python src/tracking_validation.py --root . --seasons 20242025
```

| Check | Result on 2024-25 |
|---|---|
| `goal_frame_idx` / `shot_frame_idx` == the audit's own indices | **100%** of 7,855 goals. The tracking build re-sequences the detector rather than calling `process_goal`, and this is what holds the two together |
| Scorer's `dist_to_puck_ft` == the audit's `scorer_dist_shotframe` | **100%**, max difference 0.005 ft (rounding) |
| **The shooter is where the play-by-play says the goal was** | median **3.26 ft**, **90.1% within 10 ft** — against 90.7% for the puck at the same frame ([METHODS §10](METHODS.md)) |
| Shooter-to-puck distance at the shot frame | median **2.70 ft** (IQR 2.12–3.41), 92.6% within 5 ft, 98.5% within 10 ft |
| One shared orientation with the play-by-play | 0.56% of goals fit the negated frame better |
| `x_att == x × flip` and `y_att == y × flip` | 0 mismatches in 14,249,145 rows — the flip is a rotation, not a mirror |
| `net_x` == the net implied by `homeTeamDefendingSide` | **100%** of 7,855 goals; `net_source` is `pbp` for every one |
| The puck ends up in the attacked net | `x_att` at the goal frame: median **+89.45 ft**, 98.7% in the attacking half |
| One shot frame, one scorer row, one puck row per goal | 100% |

The shooter band matching the puck band to within 0.6 points is the load-bearing
result: the shooter and the puck are different points a median 2.7 ft apart, and
both land on the play-by-play's coordinate equally often, which is what "the
shooter is at the right place on the ice" means for a table nothing else can
check. Where it misses it misses for a known reason — goals graded
`shot_confidence == "low"` (n=300) sit a median 29 ft off, against 3.2 ft for the
7,555 graded `high`.

---

## `audit/completeness_{season}.parquet`

One row per goal in the play-by-play, whether or not tracking resolved. Filter
your analysis on it.

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
| `onice_entities_goalframe`, `n_onice_goalframe` | Anchor frames are capped at 14 entities and 7 per team ([METHODS §6](METHODS.md)); 97% fall in 8–13 |
| `puck_has_coords_goalframe` | bool |
| `frames_back_to_valid_puck` | How far the walk-back went to find a valid puck |
| `frames_trimmed_dead` | Frames of post-goal dead time removed ([METHODS §5](METHODS.md)) |
| `goalframe_index`, `shotframe_index` | Chosen frame indices |
| `note` | `sprite-missing-or-403`, `sprite-unparseable-or-empty`, `no-valid-puck-in-any-frame`, `scorer-not-tracked`, or empty |

**Shot detection**

| Column | Notes |
|---|---|
| `shot_method` | `local-min` (a real puck contact) or `global-min` (fallback — 56.7% of these are more than 20 ft off) |
| `scorer_dist_shotframe` | Puck-to-scorer at the chosen frame. Median 2.73 ft |
| `frames_before_goal` | Detection lead. A long lead is the signature of a wrong frame |
| `shot_net_dist_ft` | Implied shot distance to the target net. Median 19.7 ft |
| `shot_netward` | Did the puck close on the net immediately after? 93.1% true |
| `shot_n_contacts` | Separate local minima found. >1 means a rebound or multiple touches |
| `net_source` | `pbp` (from `homeTeamDefendingSide`) or `puck-sign` (fallback) |
| `nearest_ft_goalframe`, `nearest_ft_shotframe` | Nearest-player distance at each frame |
| `tracking_shooter_goalframe`, `tracking_shooter_shotframe` | Nearest player's id at each frame. Not a shooter identification — see the two rows below |
| `scorer_match_goalframe` | Is the nearest player at the goal frame the recorded scorer? 4.6%. The goalie is nearest 66% of the time, which is why the goal frame is not the measurement point ([METHODS §7.1](METHODS.md)) |
| `scorer_match_shotframe` | Same at the shot frame. 74.2%, and a consistency check only, since the frame is anchored on the scorer. Use `shot_pbp_err_ft` for accuracy |

**Confidence**

| Column | Notes |
|---|---|
| `shot_pbp_err_ft` | The independent accuracy measure: feet between the tracked puck at the chosen frame and the play-by-play's own recorded shot location. Median 2.04 ft, 90.1% within 10 ft. Nothing in the detection uses it |
| `shot_confidence` | `high` — real contact and within 20 ft of the PBP location (22,980 goals). `low` — no genuine contact, or more than 20 ft off (908). `unverified` — real contact but no PBP coordinates to check against (0 in this archive; every goal has them). `none` — the scorer is not trackable in the sprite (50). Null — no sprite, unparseable, or no valid puck in any frame (135) |

---

## `processed/stints/{season}.parquet`

One row per stint — a maximal interval with no substitution.

| Column | Notes |
|---|---|
| `game_id`, `stint_idx` | Composite key; `stint_idx` is sequential within a game |
| `start_abs_seconds`, `end_abs_seconds`, `duration_seconds` | Absolute seconds from puck drop |
| `home_team_id`, `away_team_id` | |
| `n_home_skaters`, `n_away_skaters` | Skaters only, goalies excluded |
| `home_goalie_on`, `away_goalie_on` | Check these before calling a stint even strength ([METHODS §8](METHODS.md)) |
| `strength_home` | Convenience label from the home perspective, e.g. `"5v5"`, `"5v4"` |
| `goals_home`, `goals_away` | Goals in `(start, end]` |
| `score_diff_home` | Score differential **before** this stint |
| `zone_start_home` | `O` / `D` / `N` from the home perspective, or `OTF` when the change happened during play |
| `post_pp_home`, `post_pk_home` | Even-strength stint beginning within 15 s of unequal strength ending, with the sign recording who had been up a skater |
| `b2b_home`, `b2b_away` | Did that team play the previous calendar day? |

`processed/stint_players/{season}.parquet` is the companion: `game_id`,
`stint_idx`, `player_id`, `team_id`, `is_home`, `is_goalie` — one row per player
on the ice for that stint.

---

## Other tables

**`processed/shots/{season}.parquet`** — one row per shot attempt (goal, shot on
goal, missed shot, blocked shot): `season`, `game_id`, `event_id`, `event_type`,
`period`, `time_in_period`, `abs_game_seconds`, `situation_code`,
`home_team_id`, `away_team_id`, `home_team_defending_side`, `shooting_player_id`,
`scoring_player_id`, `blocking_player_id`, `event_owner_team_id`,
`goalie_in_net_id`, `shot_type`, `zone_code`, `x_coord`, `y_coord`.

For blocked shots, `event_owner_team_id` is the **blocking** team.

**`processed/shifts/{season}.parquet`** — one row per player shift: `season`,
`game_id`, `player_id`, `team_id`, `period`, `start_time`, `end_time`,
`start_abs_seconds`, `end_abs_seconds`, `duration_seconds`, `shift_number`,
`source` (`json` or `html`).

**`processed/players.parquet`** — `playerId`, `firstName`, `lastName`,
`positionCode`. Built from every raw play-by-play `rosterSpots` on disk, so it
spans all seasons present.

`positionCode` is last-write-wins across the archive: a forward listed as C in
one game and L in another resolves to whichever game was read last, so the value
depends on how much of the archive is on disk. It is reliable for the
distinction the pipeline uses it for — goalie versus skater — and approximate
for anything finer.

**`processed/games/{season}.parquet`** — `season`, `game_id`, `game_date`,
`game_type`, `home_team_id`, `home_team_abbrev`, `away_team_id`,
`away_team_abbrev`.

**`processed/faceoffs/{season}.parquet`** — every faceoff with `x_coord`,
`y_coord`, `zone_code`, `event_owner_team_id`, `winning_player_id`,
`losing_player_id`, `home_team_defending_side`. Raw geometry only; resolving a
faceoff to an offensive or defensive zone start *per team* is left to the
consumer.

**`processed/edge_skaters` / `edge_goalies`** — flattened season aggregates.
Skaters: top shot speed, max skating speed, bursts over 20 mph, distance skated,
shots/goals/shooting %, zone-time shares. Goalies: GAA, goal differential per
60, saves, goals against, save %.

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
