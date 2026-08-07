# Methods

How the raw feeds become the processed tables. Every number below is measured on
the three-season archive (2023-24, 2024-25, 2025-26; 24,073 regulation and
overtime goals) rather than assumed.

[DATA_SOURCES.md](DATA_SOURCES.md) covers the endpoints and payload shapes.
[SCHEMA.md](SCHEMA.md) has the output columns.

---

## 1. Acquisition

Implemented in `pipeline_common.py` and `scrape_raw.py`.

| Decision | Reason |
|---|---|
| Write every raw JSON verbatim **before** anything parses it | Parsing rules changed repeatedly during development. Each time the tables were re-derived from `raw/` rather than re-fetched from an undocumented endpoint |
| 0.7 s between requests, single-threaded | Undocumented endpoint, one-time historical batch |
| 403 and 404 terminal and logged; network errors, 429 and 5xx retried with exponential backoff | A 403 means no sprite exists for that event — data, not failure |
| Never halt the run on a failure | A failed game becomes a row in the audit table |
| Idempotent: a file already on disk is skipped | Re-running after an interruption resumes for free |
| Browser headers (User-Agent, Referer, Origin) | The endpoints refuse the request without them |

Games are enumerated by walking regular-season game numbers upward and stopping
after a run of consecutive 404s, so no season length is hardcoded.

The feed is assumed stable and complete for the seasons it serves. That
assumption is tested by the completeness audit (§9).

---

## 2. Join key: `playerId`

Everything joins on `playerId`, the universal NHL player id. Sweater number,
team id, team abbreviation and name are display fields, stored for reference and
never used as keys.

The one place the source data does not offer a choice is the HTML TOI reports
(`shifts_html.py`), which expose only a sweater number and a name —
`4 GOSTISBEHERE, SHAYNE`. Each sweater is resolved through *that game's own*
play-by-play `rosterSpots`: `(teamId, sweaterNumber) → playerId`. Team identity
comes from which file the report is (`TV` = visitor, `TH` = home), not from
matching team-name strings.

The sprite feed and the play-by-play share the event id: sprite file `ev258`
corresponds to play-by-play `eventId` 258. That is the event-level join.

---

## 3. Coordinates

Raw sprite coordinates are in **inches, measured from one corner of the rink**.
`pipeline_common.to_std` converts to feet from centre ice on a 200 × 85 ft
surface:

```
std_x = raw_x / 12 - 100
std_y = 42.5 - raw_y / 12
```

The origin corner is the one at standard `(-100, +42.5)`. Three checks fix that,
measured over 1.07 million tracked entity positions and 3,100 goals:

| Check | Result |
|---|---|
| Raw ranges | `x` spans 0–2400 in (= 200 ft), `y` spans 0–1020 in (= 85 ft). 99.98% and 99.64% of observations fall inside; the overshoots are a few inches, players against the boards |
| Puck at the goal instant, x | median `\|std_x\|` = **89.6 ft** — the goal line sits at 89 ft, which fixes the x offset |
| Puck at the goal instant, y | median `std_y` = **−0.07 ft**, median `\|std_y\|` = 2.3 ft — the net mouth is 6 ft wide and centred on `y = 0`, which fixes the y offset |

The **subtraction from 42.5 in the second line is not a typo**. The sprite's raw
y grows toward the opposite board from the play-by-play convention, so the
intuitive `raw_y/12 - 42.5` mirrors the rink about the centre line. Measured
against the play-by-play's own recorded shot coordinates on 2,500 goals:

| Transform | Median distance to the PBP shot location | Within 10 ft | y correlation |
|---|---|---|---|
| `42.5 - raw_y/12` | **1.9 ft** | 91% | **+0.943** |
| `raw_y/12 - 42.5` | 15.7 ft | 35% | −0.943 |

An inverted y is invisible in every distance this pipeline emits. Distances
reduce y to a `math.hypot` and the target net sits at `y = 0`, so a global sign
flip negates both operands of every difference. Running both transforms across
all ~24,000 goals and 286,029 player-rows gives distances that differ by
**0.0000000000**. The sign matters only to consumers that read it: rink maps,
left/right splits, and joins against play-by-play coordinates. It is therefore
fixed at the source rather than at each use.

`to_std` does not bounds-check, so a corrupt coordinate would pass through
silently. Maximum observed player-to-puck distance is 187.5 ft against a 217 ft
rink diagonal.

---

## 4. Frame rate

**10 fps — `dt` = 0.1 s.** Two independent lines of evidence.

**Physical plausibility.** Implied puck speeds are sane at `dt` = 0.1 s and
impossible — 200+ mph — at 0.033 s. This derivation does not depend on
interpreting any field.

**The `timeStamp` field.** It is deciseconds since the Unix epoch, not the
opaque tick counter it is usually described as. `17280637538 / 10` resolves to
2024-10-04 17:42:33.8 UTC, and that game's play-by-play records
`gameDate: 2024-10-04` with `startTimeUTC 17:00:00Z` — a first goal 42 minutes
of real time into the broadcast. Timestamp gaps between consecutive goals run
2.0–3.9× the game-clock gap, as elapsed real time should once stoppages and
intermissions are counted. The step between consecutive frames is exactly +1
with no exceptions in any file checked, so one frame is 0.1 s directly from the
field. See [FIELD_REFERENCE §1](FIELD_REFERENCE.md).

The pipeline hardcodes `SECONDS_PER_FRAME = 0.1` and treats the field as
ordering only, which is correct under either reading.

Window length varies by season and situation — 120 frames in 2023-24, 140 in
later seasons, 210 in overtime — but the rate does not. Re-check rather than
assume if you extend this to a new season.

---

## 5. The goal instant is not the last frame

The sprite keeps recording after the puck goes in. The puck sits in the net for
a median **3.4 s**, during which the scorer skates over to celebrate. Anchoring
on the last valid frame therefore measures the celebration, and puts the
scorer's closest approach to the puck *after* the goal rather than at the shot.

`build_processed.goal_instant` uses two rules.

**Primary — puck motion.** Walk back over the terminal run of frames in which
the puck is not moving; the goal instant is where that run starts. The puck
moves a median **2.0 ft/frame during live play and 0.07 ft/frame once dead**.
Thresholds: 0.6 ft/frame (≈6 ft/s), minimum run 5 frames.

**Fallback — net proximity.** The start of the final sustained run within 3 ft
of the nearer net, used when the puck never settles.

Motion is primary because it needs no estimate of which net or where it is — the
coordinates are absolute and play can run toward either end. The net rule then
validates it independently: `|puck_x|` at the detected goal instant is median
**89.6 ft** against a goal line at 89 ft, with 85% within 6 ft and 89% within
10 ft.

---

## 6. Bench celebrations

On overtime and game-winning goals the scoring team empties the bench and the
tracker records everyone who comes over the boards — up to **35 "on-ice"
entities**, with one 3v3 overtime goal showing 17 skaters for a single team.

Two defences:

1. `valid_puck_frame_from_end` only anchors on frames with a physically
   plausible count: **≤14 entities total and ≤7 per team** (6 skaters plus a
   goalie). The per-team cap catches what the total cap misses — a 3v3 winner
   can show 14 total while one bench has emptied.
2. On-ice rosters come from the shift charts rather than from tracking (§8).

---

## 7. The shot frame

### 7.1 Why not the goal frame

With the puck in the net, proximity to it is outcome-conditioned by
construction. The nearest player at the goal frame is the **goalie 66%** of the
time and a defenceman another **14%**; it is the recorded scorer only **4.6%**
of the time (4.4 / 4.8 / 4.7% by season), because the scorer is back at the
release point.

`d_goalframe` is still emitted for comparison. It is not a proximity measure.

### 7.2 Choosing the shot frame

The play-by-play names the scorer, so the pipeline searches for *when* the puck
was last at that player rather than inferring *who* had it. Shooter
identification is not a failure mode here; frame selection is, and §10 tests it
independently.

Walk backward from the goal instant over the scorer-to-puck distance and take
the **most recent local minimum** under a 12 ft contact threshold. "Most recent"
matters: an earlier carry or a pass reception by the same player is also a local
minimum, but the shot is the last time he had it. The count of separate contacts
is recorded as `shot_n_contacts`; more than one means a rebound or multiple
touches.

The search is bounded at the goal *instant*, not the last frame, for the reason
in §5 — otherwise the scorer's post-goal approach to the puck is selected as his
last touch.

**Target net.** Taken from the play-by-play: `homeTeamDefendingSide` gives the
end the home team defends in that period (teams switch ends each period), and
the event owner gives which team scored. This touches no tracking frame.

The alternative — read the net off the sign of the puck's x at the goal frame —
fails on **251 of 23,888 goals (1.05%)**, and the failures have one cause. In
those clips the puck is fished out of the net and sent back up ice before the
clip ends. It does reach the correct net (median closest approach **1.4 ft**,
0.5 s after the detected shot frame) and is then a median **112 ft** from that
net by the last frame, against 5.8 ft on a normal goal. Because the puck never
comes to rest, the motion rule in §5 finds no dead run to trim
(`frames_trimmed_dead == 0` on 97.6% of them against 6.6% otherwise), the goal
frame stays at the last frame, and by then the puck's x has crossed centre ice.
These are ordinary goals — 98% from the offensive zone, median play-by-play shot
distance 16 ft, empty-net rate 6.8% against 6.1% overall — so the effect was a
166 ft implied shot where the play-by-play said 16 ft, which looks like a real
long shot rather than an error. Using the play-by-play net raises the
correlation between implied and recorded shot distance from **0.78 to 0.90**.
The puck-sign rule stays as a fallback for goals missing
`homeTeamDefendingSide`.

The net was never an input to frame selection, so this changed no shot-frame
index — only the distances measured from it.

### 7.3 Window averaging

Per-player distance is averaged over the chosen frame **±2 frames** (0.2 s).
Positions barely change frame-to-frame at release, so this removes detection
jitter without smearing.

The on-ice roster is anchored to the **centre frame**, not unioned across the
window. Unioning would include everyone seen in any frame of the window, so a
line change inside it inflates the set.

### 7.4 Accuracy and limits

Pooled over all three seasons (23,888 goals with a detected shot frame):

| Metric | Value |
|---|---|
| Goals with a detected shot frame | **98.7 / 99.4 / 99.6%** by season |
| Puck-to-scorer distance at that frame | median **2.73 ft** (IQR 2.1–3.5) |
| Implied shot distance to net | median **19.7 ft** (IQR 9.5–33.2) |
| Puck travels net-ward immediately after | **93.1%** |
| Shot → goal elapsed | median **0.90 s** (IQR 0.6–1.6) |
| Distance from the play-by-play's own shot location (§10) | median **2.04 ft**, 90.1% within 10 ft |

The nearest player to the puck at the detected shot frame is the recorded scorer
**74.2%** of the time, even though the frame is chosen by finding the scorer: at
release a checker can sit closer to the puck than the shooter, and both
positions carry measurement error. The implication is that **"nearest player to
the puck" is not a shooter proxy** — not at the goal frame (4.6%) and not at the
shot frame — which is why the shot frame is anchored on the play-by-play scorer
rather than derived from proximity.

Accuracy by shot type, over the same 23,888 goals:

| Shot type | n | Median error vs PBP | Within 10 ft | Nearest player is the scorer |
|---|---|---|---|---|
| slap | 1,924 | 1.51 ft | 95.0% | 86.5% |
| snap | 5,759 | 1.63 ft | 92.1% | 73.8% |
| wrist | 10,729 | 1.99 ft | 89.4% | 80.2% |
| deflected | 595 | 2.40 ft | 94.8% | 60.0% |
| tip-in | 2,317 | 2.94 ft | 94.2% | 44.8% |
| poke | 153 | 4.20 ft | 81.7% | 33.3% |
| backhand | 2,047 | 4.43 ft | 80.2% | 75.4% |
| wrap-around | 125 | 5.52 ft | 72.0% | 72.8% |

Two things to read off this. Tips and deflections locate **well** — the frame
chosen is the deflection, and the play-by-play records the deflection too — but
their low scorer-match rate reflects the net-front traffic they happen in, not a
detection failure. The genuinely weaker cases are wrap-arounds and backhands,
where the scorer is carrying the puck and "the last touch" is a stretch of frames
rather than a moment.

**Assumptions**, in decreasing order of comfort:

1. The play-by-play `scoringPlayerId` is correct.
2. The last time the scorer was near the puck is the shot release. Safe for
   clean shots; for a tip-in or deflection this is the deflection point, not
   where the puck was originally shot from.
3. Distances are stable over ±2 frames (0.2 s).

Every goal carries a `shot_confidence` flag graded against the independent check
in §10, not against the scorer anchor, which cannot fail informatively.

---

## 8. On-ice rosters and stints

**The shift charts are authoritative for who was on the ice. Tracking supplies
only where they were.** Using tracking as the roster put a player in the set who
was not actually on the ice for 1 goal in 5 (80.2% exact match, 0.28 extra
tracked players per goal).

**Boundary rule: `shift.start < t <= shift.end`.** At a goal the whistle blows,
so the outgoing line's shifts end at `t` while the incoming line's shifts start
at `t`. Counting both ends inclusively counts both lines: over 24,688 goals it
gives a median of **20** players on the ice, mode 22. The half-open rule gives a
median of **12**, mode 12, within 8–13 for **97.1%** of goals. Equivalently, for
a stint spanning `(a, b]` a player is on ice iff `shift.start <= a and
shift.end >= b`, which is what the sweep in `stints.py` maintains.

**Resolved at the shot, not the goal.** The distances describe the release, a
median 0.9 s before the goal — long enough for a line change. `shot_lead_seconds`
is emitted per goal so roster, strength and score state can be read at
`t_shot = t_goal − frames_before_goal × 0.1 s`.

Shift-chart players with no tracking coordinates (0.037 per goal) are kept
rather than dropped, so the lineup stays complete.

**A stint** is a maximal interval with no substitution. It is not published
anywhere and is reconstructed by sweeping every shift start and end in a game.
Each stint carries duration, goals for and against, skater counts, goalie-on
flags, score state *before* the stint, zone start, and back-to-back flags.

Two things are left raw for the consumer:

- **Zone starts** are only meaningful when a stint begins at a stoppage. Beyond
  a 2 s tolerance from the nearest faceoff the change happened during play, and
  the stint is labelled `OTF` ("on the fly") rather than inheriting the zone of
  whatever faceoff preceded it. That covers 87.7% of stints, which is expected —
  stints are sub-shift intervals, and most begin mid-play.
- **Strength state** is emitted as raw skater counts plus goalie-on flags, not
  as a label. If you intend "even strength" to require both goalies in net — the
  standard definition for 5v5 rate stats — apply that yourself. Pulled-goalie
  time is **2.2–2.3% of all stint time**, and the common mistake is to drop
  empty-net *goals* while keeping the pulled-goalie *ice time*, which biases any
  rate statistic downward for whoever was out there. Exclude both together, at
  the stint level, before attaching goals.

Reconciliation after the sweep (`stints.reconcile`): stint-derived TOI
correlates **0.99996–0.99999** with raw shift-chart TOI, and recovered team TOI
is 60.6–60.8 min/game against a physical expectation of ~60.

---

## 9. Completeness audit

Every goal in the authoritative play-by-play gets a row in
`audit/completeness_{season}.parquet`, whether or not tracking resolved: sprite
exists, parsed, frame count, on-ice count, puck coordinates valid, frames
stepped back to a valid puck, detected shot frame and method, scorer distance,
implied shot distance, net-ward flag, error against the play-by-play location,
and confidence.

**Shootout goals are excluded** (`MAX_GOAL_PERIOD = 4`). They are recorded as
goals in the play-by-play but are not real ice time and have no goal
recreation: 169 in 2024-25, of which only 11 had a sprite. Counting them
understated coverage as 97.96% against a true regulation-and-overtime figure of
99.92%.

| Season | Goals (reg + OT) | Sprite present and parses | Shot frame detected |
|---|---|---|---|
| 2023-24 | 8,086 | 8,025 (**99.25%**) | 7,982 (**98.71%**) |
| 2024-25 | 7,901 | 7,895 (**99.92%**) | 7,855 (**99.42%**) |
| 2025-26 | 8,086 | 8,080 (**99.93%**) | 8,051 (**99.57%**) |

The second column is the scrape's coverage; the third is what is actually usable
for a distance measurement, and is the number to quote. The 185 goals in the gap
break down as: 50 where the scorer is not trackable in the sprite, 62 with no
valid puck in any frame, 44 unparseable or empty sprites, 29 with no sprite at
all.

---

## 10. Validation against the play-by-play

`shotframe_validation.py`. This is the check that can fail.

The scorer-match statistic — "the nearest player at the chosen frame is the
recorded scorer" — is close to circular, because the frame is anchored on the
scorer. At best it confirms a frame was found where the scorer had the puck. It
says nothing about whether that frame is the release or sits on the right part
of the ice.

The play-by-play records its own (x, y) for every goal, placed by an NHL scorer
watching the game. It is an independent observation of the same event, and it
can falsify three assumptions:

1. **Coordinate frame** — compare the tracked puck position against the PBP
   coordinates as-is versus negated. If a substantial subset fits the negated
   version better, the two feeds disagree about rink orientation (§3).
2. **Attack direction** — the target net derived from tracking must agree in
   sign with the one derived from `homeTeamDefendingSide`. This is the check
   that caught the 1.05% net mis-assignment in §7.2.
3. **The shot frame** — at release the scorer is holding the puck, so both
   should sit at the recorded shot location. A large gap means the chosen frame
   is an earlier touch in the same possession.

Goals the play-by-play is itself unsure about are excluded rather than counted
as agreement: no PBP coordinates, or a scorer not trackable in the sprite.

**Result over all 23,888 goals:** the puck at the inferred shot frame sits a
median **2.04 ft** from the play-by-play's recorded shot location, with **90.1%
within 10 ft and 96.5% within 20 ft**. The play-by-play coordinate is placed by
eye, so a few feet of disagreement is expected from either side and this is
about as close as the comparison can resolve.

Where it disagrees by more than 20 ft:

| Inferred shot distance | Share > 20 ft off | Median detection lead |
|---|---|---|
| 0–50 ft | 1.4–4.3% | 0.8–1.0 s |
| 50–70 ft | 9.0% | 1.3 s |
| **70–100 ft** | **29.3%** | 1.7 s |
| 100+ ft | 17.3% | 3.9 s |
| **`global-min` fallback** (n=150) | **56.7%** | — |

The failure mode is the same throughout: the chosen frame is an earlier touch in
the same possession — a breakout or a carry — rather than the release. A long
detection lead is its signature, so `frames_before_goal` is worth filtering on.

`shot_confidence` is graded on this: `high` requires both a genuine
local-minimum contact and agreement within 20 ft of the play-by-play location.
The 20 ft threshold is deliberately loose, since the PBP coordinate is itself
placed by eye; it is a check for gross failure, not precision.
