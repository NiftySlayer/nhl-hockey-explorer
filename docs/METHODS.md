# Methods

How the raw feeds become the processed tables, and why each decision is what it
is. Every number below is measured on the three-season archive (2023-24,
2024-25, 2025-26; ~24,000 goal events) rather than assumed.

Read [DATA_SOURCES.md](DATA_SOURCES.md) first if you want the endpoints and
payload shapes. [SCHEMA.md](SCHEMA.md) has the output columns.

---

## 1. Acquisition and scraping discipline

Implemented in `pipeline_common.py` and `scrape_raw.py`.

| Decision | Why |
|---|---|
| Write every raw JSON verbatim **before** anything parses it | Parsing rules changed repeatedly while this was built. Each time, the tables were re-derived from `raw/` instead of re-hitting an undocumented endpoint that may disappear |
| 0.7 s between requests, single-threaded | An undocumented endpoint, and a one-time historical batch does not justify parallel bursting |
| 403 and 404 terminal and logged; network errors, 429 and 5xx retried with exponential backoff | A 403 means "no sprite exists for this event" — that is *data*, not a failure, and retrying it is just noise |
| Never halt the run on a failure | A failed game becomes a row in the audit table. An overnight run that dies at game 400 is worse than one that logs 3 misses |
| Idempotent: a file already on disk is skipped | Re-running after an interruption resumes for free and costs the endpoint nothing |
| Browser headers (User-Agent, Referer, Origin) | The endpoints refuse the request without them |

Games are enumerated by walking regular-season game numbers upward and stopping
after a run of consecutive 404s, so no season length is hardcoded — this works
identically on a finished season (1,312 games) and a partial one.

**Assumption:** the feed is stable and complete for the seasons it serves. That
is tested by the completeness audit (§9) rather than trusted.

---

## 2. Join key: `playerId`, and nothing else

**Everything joins on `playerId`, the universal NHL player id.** Sweater number,
team id, team abbreviation and name are display fields. They are stored for
reference and never used as keys.

This matters most in the one place where the source data does not give you a
choice. The HTML TOI reports (§ below, and `shifts_html.py`) expose only a
sweater number and a name — "4 GOSTISBEHERE, SHAYNE". Rather than string-match
names or assume sweater numbers are stable, each sweater is resolved through
*that game's own* play-by-play `rosterSpots`: `(teamId, sweaterNumber) →
playerId`, a game-scoped authoritative lookup. Team identity comes from which
file the report is (`TV` = visitor, `TH` = home) rather than by matching team
name strings.

The sprite feed and the play-by-play share the event id: sprite file `ev258`
corresponds to play-by-play `eventId` 258. That is the event-level join.

---

## 3. Coordinates

Raw sprite coordinates are **inches from the corner** of the rink.
`pipeline_common.to_std` converts to feet from centre ice on a 200 × 85 ft
surface:

```
std_x = raw_x / 12 - 100
std_y = 42.5 - raw_y / 12
```

**Note the second line. It is a subtraction *from* 42.5, not the other way
around, and this is the single easiest thing in the pipeline to get backwards.**
The sprite's raw y grows toward the opposite board from the NHL play-by-play
convention, so the intuitive `raw_y/12 - 42.5` mirrors the entire rink about the
centre line.

Measured against the play-by-play's own recorded shot coordinates on 2,500
goals:

| Transform | Median distance to the PBP's shot location | Within 10 ft | y correlation |
|---|---|---|---|
| `42.5 - raw_y/12` | **1.9 ft** | 91% | **+0.943** |
| `raw_y/12 - 42.5` | 15.7 ft | 35% | −0.943 |

**Why this is worth a warning rather than a footnote:** an inverted y-axis is
invisible in every distance this pipeline emits. Distances reduce y to a
`math.hypot` and the target net sits at `y = 0`, so player-to-puck distance
negates both operands and the difference is unchanged, while distance-to-net
uses `|y|` alone. A full-archive check confirmed this directly — running both
transforms across all ~24,000 goals and 286,029 player-rows gives distances that
differ by **0.0000000000**.

The sign matters only to consumers that read it: rink maps, left/right-side
splits, and joins against play-by-play coordinates. It is therefore fixed at the
source rather than at each use.

*Known weakness: `to_std` does not bounds-check. A corrupt coordinate would pass
through silently. Maximum observed player-to-puck distance is 187.5 ft against a
217 ft rink diagonal, so nothing in the current archive is out of range.*

---

## 4. Frame rate

**The rate is 10 fps — `dt` = 0.1 s.** Two independent lines of evidence agree.

**Physical plausibility.** Implied puck speeds are sane at `dt` = 0.1 s and
impossible — 200+ mph — at 0.033 s. This was the original derivation, and it
does not depend on interpreting any field.

**The `timeStamp` field itself.** This is commonly described (including in
earlier versions of this document) as an opaque tick counter that gives order
but not time. That turns out to be wrong: **it is deciseconds since the Unix
epoch.** `17280637538 / 10` resolves to 2024-10-04 17:42:33.8 UTC, and that
game's play-by-play records `gameDate: 2024-10-04` with `startTimeUTC 17:00:00Z`
— a first goal 42 minutes of real time into the broadcast. Timestamp gaps
between consecutive goals run 2.0–3.9× the game-clock gap, exactly as elapsed
real time should once stoppages and intermissions are counted.

The step between consecutive frames is **exactly +1, with no exceptions in any
file checked**, so one frame is 0.1 s directly from the field.

Practically nothing changes: `SECONDS_PER_FRAME` stays hardcoded at 0.1 and the
pipeline still treats the field as ordering only, which is correct under either
reading. What changes is that the frame rate now rests on two independent
confirmations rather than one inference. See
[FIELD_REFERENCE §1](FIELD_REFERENCE.md) for the full working.

Window length varies by season and situation (120 frames in 2023-24, 140 in
later seasons, 210 in overtime) but the rate does not. If you extend this
pipeline to a new season, re-check rather than assuming.

---

## 5. The goal instant is not the last frame

The sprite keeps recording after the puck goes in. **The puck sits in the net
for a median 3.4 seconds of dead time**, during which the scorer skates over to
celebrate. Two things go wrong if you anchor on the last valid frame: any
"goal-frame" measurement describes the celebration, and the scorer's closest
approach to the puck lands *after* the goal rather than at the shot.

`build_processed.goal_instant` uses two rules.

**Primary — puck motion.** Walk back over the terminal run of frames in which
the puck is not moving; the goal instant is where that run starts. Calibrated
empirically: the puck travels a median **1.99 ft/frame during live play and 0.15
ft/frame once dead**, a 13× separation. Thresholds: 0.6 ft/frame (≈6 ft/s),
minimum run 5 frames.

**Fallback — net proximity.** The start of the final sustained run within 3 ft
of the nearer net, used when the puck never settles (it was retrieved
immediately).

Motion is primary because it requires no estimate of *which* net or where it is —
the coordinates are absolute and play can run toward either end. The net rule
then validates it independently: `|puck_x|` at the detected goal instant is
median **89.6 ft**, with 90.2% within 6 ft of the 89 ft goal line.

---

## 6. Bench celebrations

On overtime and game-winning goals the scoring team empties the bench and the
tracker records everyone who comes over the boards — **up to 35 "on-ice"
entities**, with one 3v3 overtime goal showing 17 skaters for a single team.

Two defences:

1. `valid_puck_frame_from_end` only anchors on frames with a physically
   plausible count: **≤14 entities total and ≤7 per team** (6 skaters plus a
   goalie). The per-team cap catches what the total cap misses — a 3v3 winner
   can show 14 total while one bench has emptied.
2. Definitively, on-ice rosters come from the shift charts rather than from
   tracking (§8).

---

## 7. The shot frame

The most consequential decision in the pipeline.

### 7.1 Why the goal frame cannot be the measurement point

Measuring player-to-puck distance while the puck is in the net is
**outcome-conditioned by construction**. The puck sits in one net, so the
conceding side's defencemen are mechanically the nearest players on the ice.

The evidence is stark: the nearest player at the goal frame is the recorded
scorer only **4.6%** of the time (4.4 / 4.8 / 4.7% by season), because the
scorer is back at the release point. The resulting "defencemen are 26 ft away
when conceding and 47 ft when scoring" asymmetry is mostly a statement about
where the net is. Any statistic built on goal-frame distance inherits a
systematic asymmetry that encodes which team scored.

`d_goalframe` is still emitted, as a documented comparison. It should not be
used as a proximity measure.

### 7.2 Scorer-anchored detection

**First approach, abandoned.** Infer the shooter from puck kinematics: score
near-stick frames on net-ward puck travel, acceleration, and recency. It
**identified the right shooter 47% of the time** — good enough to look like it
was working, not good enough to build on.

**Current approach.** Invert the problem. The play-by-play already names the
scorer, so there is no need to infer *who*; only *when*. Walk backward from the
goal instant over the scorer-to-puck distance and take **the most recent local
minimum** under a 12 ft contact threshold.

This eliminates shooter identification as a failure mode by construction, which
means the remaining question — is this the right *frame*? — has to be answered
by something outside the anchor. That is §10, and it is the only accuracy
number in this document that could have come out badly.

"Most recent" is doing real work. An earlier carry, or a pass reception by the
same player, is also a local minimum — but the shot is the last time he had it.
The number of separate contacts is recorded as `shot_n_contacts`; more than one
means a rebound or multiple touches.

The search is bounded at the goal *instant*, not the last frame, for the reason
in §5: after the puck is in the net the scorer closes on it to celebrate, and
that would otherwise be selected as his last touch.

**The target net comes from the play-by-play.** The original rule took it from
the sign of the puck's x at the goal frame, reasoning that the puck ends up in
the net. That holds only when the goal frame is right. On **251 of 23,888 goals
(1.05%)** the puck had already been retrieved and carried back up ice, its x
flipped sign, and the shot was measured to the far end — a median **166 ft**
where the play-by-play said **16 ft**. Because their tracked *positions* were
fine (median 6 ft error), these looked like genuine long shots. Using
`homeTeamDefendingSide` plus which team owns the event fixes the attacking
direction with no dependence on any tracking frame, and raises the correlation
between the implied shot distance and the play-by-play's own from **0.780 to
0.896**. The puck-sign rule remains as a fallback for the rare goal where the
field is absent.

Note that the net was never an input to frame selection, so this changed no
shot-frame index for any goal — only the distances measured from it.

### 7.3 Window averaging

Per-player distance is averaged over the chosen frame **±2 frames** (0.2 s).
Positions barely change frame-to-frame at release, so this removes detection
jitter without smearing.

One subtlety that cost real debugging time: **the on-ice roster is anchored to
the centre frame**, not unioned across the window. Averaging naively would
include everyone seen in *any* frame of the window, so a line change inside it
inflates the set — up to 35 "on-ice" players before this was anchored. What you
want is the players on the ice at that moment, with their distances smoothed.

### 7.4 What the detection achieves, and where it fails

Pooled over all three seasons (23,888 goals with a detected shot frame):

| Metric | Value |
|---|---|
| Goals with a detected shot frame | **98.7 / 99.4 / 99.6%** by season |
| Puck-to-scorer distance at that frame | median **2.73 ft** (IQR 2.1–3.5) |
| Implied shot distance to net | median **19.7 ft** (IQR 9.5–33.2) |
| Puck travels net-ward immediately after | **93.1%** |
| Shot → goal elapsed | median **0.90 s** (IQR 0.6–1.6) |

**One number that surprises people:** the nearest player to the puck at the
detected shot frame is the recorded scorer only **74.2%** of the time — even
though the frame is chosen by finding the scorer. At release a checker's
position can easily sit closer to the puck than the shooter's own. Take the
implication seriously: **"nearest player to the puck" is not a usable shooter
proxy**, not at the goal frame (4.6%) and not at the shot frame either. That is
why the shot frame is anchored on the play-by-play's scorer rather than derived
from proximity.

**Assumptions this rests on**, in decreasing order of comfort:

1. The play-by-play `scoringPlayerId` is correct. *(Very safe.)*
2. The last time the scorer was near the puck is the shot release. *(Safe for
   clean shots. **Weakest for deflections and tips**, where the credited scorer
   touches a puck that is already travelling — which is precisely the situation
   where proximity is most interesting.)*
3. Distances are stable over ±2 frames. *(Safe: 0.2 s.)*

This is a fallible component, so it is measured rather than asserted. Every goal
carries a `shot_confidence` flag, graded against the independent check in §10 —
not against the scorer anchor, which cannot fail informatively.

---

## 8. On-ice rosters and stints

**The shift charts are authoritative for *who* was on the ice. Tracking supplies
only *where* they were.** Using tracking as the roster put a player in the set
who was not actually on the ice for **1 goal in 5** (80.2% exact match, 0.28
extra tracked players per goal).

**Boundary rule: `shift.start < t <= shift.end`.** At a goal the whistle blows,
so the outgoing line's shifts END at `t` while the incoming line's shifts START
at `t`. An inclusive-both-ends rule counts *both* lines and yields 14–18 players
on the ice. This rule yields 8–13 for **97.6%** of goals, mode 12. Equivalently,
for a stint spanning `(a, b]` a player is on ice iff `shift.start <= a and
shift.end >= b`, which is what the sweep in `stints.py` maintains.

**Resolved at the shot, not the goal.** The distances describe the release, a
median 0.9 s before the goal — long enough for a line change. `shot_lead_seconds`
is emitted per goal so roster, strength and score state can be read at
`t_shot = t_goal − frames_before_goal × 0.1 s`.

Shift-chart players with no tracking coordinates (0.037 per goal) are kept
rather than dropped, so the lineup stays complete.

**A stint** is a maximal interval with no substitution. It is not published
anywhere and has to be reconstructed by sweeping every shift start and end in a
game, which is what `stints.py` does. Each stint carries duration, goals for and
against, skater counts, goalie-on flags, score state *before* the stint, zone
start, and back-to-back flags.

Two things are deliberately left raw for the consumer to decide:

- **Zone starts** are only meaningful when a stint begins at a stoppage. Beyond
  a 2 s tolerance from the nearest faceoff the change happened during play, and
  the stint is labelled `OTF` ("on the fly") rather than inheriting the zone of
  whatever faceoff happened to precede it.
- **Strength state** is emitted as raw skater counts plus goalie-on flags, not
  as a label. If you intend "even strength" to require both goalies in net —
  the standard definition for 5v5 rate stats — you must apply that yourself.
  Worth knowing: pulled-goalie time is **2.2–2.3% of all stint time**, and the
  easy mistake is to drop empty-net *goals* while keeping the pulled-goalie
  *ice time*, which biases any rate statistic downward for whoever was out
  there. Exclude both together, at the stint level, before attaching goals.

Reconciliation checks run after the sweep (`stints.reconcile`): stint-derived
TOI correlates **0.99996** with raw shift-chart TOI, and recovered team TOI is
60.7–60.8 min/game against a physical expectation of ~60.

---

## 9. Completeness audit

Every goal in the authoritative play-by-play gets a row in
`audit/completeness_{season}.parquet`, whether or not tracking resolved: sprite
exists, parsed, frame count, on-ice count, puck coordinates valid, frames
stepped back to a valid puck, detected shot frame and method, scorer distance,
implied shot distance, net-ward flag, error against the play-by-play location,
and confidence.

The gaps are part of the output, not an afterthought — they are how you know
what to trust.

**Shootout goals are excluded** (`MAX_GOAL_PERIOD = 4`). They are recorded as
goals in the play-by-play but are not real ice time and have no goal
recreation: 169 in 2024-25, of which only 11 had a sprite. Counting them
understated coverage as 97.96% when the true regulation-and-overtime figure is
99.92%. This is the kind of denominator error that makes a pipeline look broken
when it is fine — or fine when it is broken.

| Season | Goals (regulation + OT) | With usable tracking | |
|---|---|---|---|
| 2023-24 | 8,086 | 8,025 | **99.25%** |
| 2024-25 | 7,901 | 7,895 | **99.92%** |
| 2025-26 | 8,086 | 8,080 | **99.93%** |

---

## 10. Validation against the play-by-play

`shotframe_validation.py`. This is the check that can actually fail.

The scorer-match statistic — "the nearest player at the chosen frame is the
recorded scorer" — is close to circular, because the frame is *anchored* on the
scorer. At best it confirms a frame was found where the scorer had the puck. It
says nothing about whether that frame is the release, or whether it sits on the
right part of the ice. (It is also only 74.2%, for the reason in §7.4, so it is
not even a flattering number to quote.)

The play-by-play records its own (x, y) for every goal, placed by an NHL scorer
who watched it. That is an **independent observation of the same event**, and it
can falsify three separate assumptions:

1. **Coordinate frame** — compare the tracked puck position against the PBP
   coordinates as-is versus negated. If a substantial subset fits the negated
   version better, the two feeds disagree about rink orientation (§3).
2. **Attack direction** — the target net derived from tracking must agree in
   sign with the one derived from `homeTeamDefendingSide`. This is the check
   that caught the 1.05% net-misassignment described in §7.2.
3. **The shot frame itself** — at release the scorer is holding the puck, so
   both should sit at the recorded shot location. A large gap means the chosen
   frame is an earlier touch in the same possession.

Goals the play-by-play is itself unsure about are excluded rather than counted
as agreement: no PBP coordinates, or a scorer not trackable in the sprite.

**Result over all 23,888 goals:** the puck at the inferred shot frame sits a
median **2.04 ft** from the play-by-play's recorded shot location, with **90.1%
within 10 ft and 96.5% within 20 ft**. Given that the play-by-play coordinate is
itself placed by eye, that is about as close to agreement as the comparison can
show.

Where it fails, and by how much (share more than 20 ft off):

| Inferred shot distance | Wrong | Median detection lead |
|---|---|---|
| 0–50 ft | 1.4–4.3% | 0.8–1.0 s |
| 50–70 ft | 9.0% | 1.3 s |
| **70–100 ft** | **29.3%** | 1.7 s |
| 100+ ft | 17.3% | 3.9 s |
| **`global-min` fallback** (n=150) | **56.7%** | — |

The failure mode is always the same: the chosen frame is an **earlier touch in
the same possession** — a breakout or a carry — rather than the release. A long
detection lead is its signature, which is why `frames_before_goal` is worth
filtering on.

`shot_confidence` in the audit table is graded on this: `high` requires both a
genuine local-minimum contact and agreement within 20 ft of the play-by-play
location. The 20 ft threshold is deliberately loose — the PBP coordinate is
placed by eye and is itself accurate only to a few feet, so this is a check for
gross failure, not precision.
