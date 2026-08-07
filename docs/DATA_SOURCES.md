# Data sources

Four public NHL endpoints plus NHL Edge, what each returns, and the quirks that
matter. All were live as of July 2026. They are **undocumented** — treat
availability and shape as things to verify, not guarantee.

Every endpoint requires browser headers (User-Agent, Referer `https://www.nhl.com/`,
Origin, Accept) or it refuses the request. See `pipeline_common.HEADERS`.

---

## 1. Goal recreation ("sprites") — the tracking data

```
https://wsr.nhle.com/sprites/{season}/{game_id}/ev{event_id}.json
```

Frame-by-frame positional tracking for a single goal. Written to
`raw/sprites/{season}/{game}/ev{event}.json`.

**Grain:** the file *is* a JSON array of frames — there is no wrapper object.
Each frame is `{timeStamp, onIce}`, where `onIce` is a dict keyed by entity id.

- Entity `"1"` is the puck.
- Every other entity carries `playerId`, `x`, `y`, `sweaterNumber`, `teamId`,
  `teamAbbrev`.
- Coordinates are inches from the rink corner at standard `(-100, +42.5)`; see
  [METHODS §3](METHODS.md) for the transform and the y-axis inversion.

**Availability floor is 2023-24.** A six-season probe found a clean boundary:
2020-21, 2021-22 and 2022-23 return 403 for *every* sprite, while 2023-24,
2024-25 and 2025-26 return 200 and parse. League-wide player tracking became
operational in 2021-22, but the public goal-recreation feed is a separate
pipeline from the tracking system itself. The usable archive is three seasons.

**Quirks:**

| | |
|---|---|
| `timeStamp` is deciseconds since the Unix epoch | Steps exactly +1 per frame, so one frame = 0.1 s = 10 fps. Often mis-described as an opaque tick counter ([METHODS §4](METHODS.md), [FIELD_REFERENCE §1](FIELD_REFERENCE.md)) |
| Window length varies | 120 frames in 2023-24 (~12 s), 140 in later seasons (~14 s), 210 in overtime. Same fps, longer pre-goal window |
| Recording continues past the goal | Median 3.4 s of dead time with the puck in the net ([METHODS §5](METHODS.md)) |
| Bench emptying | Up to 35 entities per frame on overtime and game-winning goals ([METHODS §6](METHODS.md)) |
| Null puck | ~1% of goals have no puck coordinates at the final frame; the pipeline walks back to a valid frame. 62 goals across three seasons have no valid puck in any frame and are flagged |
| Missing sprites | A goal with no public recreation returns 403. This is data, not failure — mostly shootout deciders |

**Size:** ~24,000 files, 7.2 GB across three seasons.

---

## 2. Play-by-play — the authoritative event log

```
https://api-web.nhle.com/v1/gamecenter/{game_id}/play-by-play
```

Written to `raw/pbp/{season}/{game}.json`. One file per game, 3,936 files for
three seasons.

This is the spine of the pipeline. It supplies the goal list to scrape sprites
for, the `rosterSpots` that resolve every player id, and an independent record
of where each goal was scored from, which is what makes the shot-frame
validation in [METHODS §10](METHODS.md) possible.

**Fields the pipeline reads:**

| Field | Use |
|---|---|
| `plays[].eventId` | Joins to the sprite filename (`ev258` ↔ `eventId` 258) |
| `plays[].typeDescKey` | Event type filter (`goal`, `shot-on-goal`, `missed-shot`, `blocked-shot`, `faceoff`) |
| `plays[].periodDescriptor.number`, `timeInPeriod` | Absolute game time |
| `plays[].situationCode` | 4 digits — `[awayGoalie, awaySkaters, homeSkaters, homeGoalie]`. `"1551"` is 5v5. Authoritative strength state |
| `plays[].homeTeamDefendingSide` | `"left"` / `"right"`, per period, since teams switch ends. Fixes attacking direction and therefore the target net, with no dependence on tracking |
| `details.scoringPlayerId` | The anchor for shot detection ([METHODS §7.2](METHODS.md)) |
| `details.xCoord`, `yCoord` | The NHL scorer's own record of the shot location — the independent validation target |
| `details.shootingPlayerId`, `blockingPlayerId`, `goalieInNetId`, `shotType`, `zoneCode` | Event attribution |
| `rosterSpots[]` | `playerId`, `sweaterNumber`, `teamId`, position, names. The player master, and the sweater→playerId lookup |

**Quirks:**

- **Shootout goals** appear as goals but are not real ice time and have no
  recreation. Excluded via `MAX_GOAL_PERIOD = 4`; see
  [METHODS §9](METHODS.md) for why this matters to reported coverage.
- For a **blocked shot**, `eventOwnerTeamId` is the *blocking* team, not the
  shooting team. Both the team and `shootingPlayerId` are stored so the
  consumer can decide ownership.
- `homeTeamDefendingSide` is occasionally absent in early-season files; the
  pipeline falls back to the puck-sign rule when it is.

---

## 3. Shift charts — on-ice rosters

```
https://api.nhle.com/stats/rest/en/shiftcharts?cayenneExp=gameId={game_id}
```

Written to `raw/shifts/{season}/{game}.json`. One row per player shift, with
`gameId`, `playerId`, `period`, `startTime`, `endTime` (both "MM:SS" elapsed in
period), and `shiftNumber`.

These are authoritative for who was on the ice; tracking is not
([METHODS §8](METHODS.md)).

**The quirk that matters:** the feed returns **HTTP 200 with `total = 0`** — a
successful, empty response — for whole contiguous blocks of games. Observed: 57
games in 2024-25 and 505 in 2025-26 (38% of the season), in nine separate
blocks. Nothing about the response signals failure. Without a fallback, every
affected game silently loses its on-ice rosters.

The feed also carries goal-marker rows with no usable interval alongside real
shifts; rows missing a start or end are dropped.

---

## 4. HTML TOI reports — the shift-chart fallback

```
https://www.nhl.com/scores/htmlreports/{season}/TV{gamenum}.HTM   (visitor)
https://www.nhl.com/scores/htmlreports/{season}/TH{gamenum}.HTM   (home)
```

Written to `raw/shifts_html/{season}/{game}_{TV|TH}.HTM`. The game number is the
last six digits of the game id.

The classic HTML reports carry shift data for the games the JSON feed serves
empty. Parsed with BeautifulSoup into rows matching the JSON schema, so both
sources feed one table with a `source` column recording which.

**Structure:**

```
td.teamHeading    -> "CAROLINA HURRICANES"
td.playerHeading  -> "4 GOSTISBEHERE, SHAYNE"     (sweater + name)
then shift rows:   Shift# | Per | Start Elapsed/Game | End Elapsed/Game |
                   Duration | Event
```

`"1:38 / 18:22"` is elapsed-in-period / clock-remaining. The pipeline takes the
**elapsed** value, matching the JSON feed's `startTime` semantics.

**Join key resolution:** the HTML exposes only sweater number and name. Sweater
is resolved to `playerId` through that game's own play-by-play `rosterSpots`
([METHODS §2](METHODS.md)). Team identity comes from which file it is
(`TV` = away, `TH` = home), never from matching team-name strings.

**Validation.** The pipeline only fetches these reports for games the JSON feed
serves empty, so the two sources never overlap in the archive. To check the
parser, reports were fetched separately for 12 games that *do* have JSON shifts:
per-player TOI correlates **0.99998** across 456 player-games, median difference
0 s, 99.3% within 5 s, and shift counts match exactly for 99.3% of players.
1,010 files were fetched for 2025-26 with zero errors.

---

## 5. NHL Edge — season-level aggregates

```
https://api-web.nhle.com/v1/edge/skater-detail/{playerId}/{season}/{gameType}
https://api-web.nhle.com/v1/edge/goalie-detail/{playerId}/{season}/{gameType}
```

Written to `raw/edge/{season}/{skater|goalie}/{playerId}.json`.

Per-player-per-season aggregates: top shot speed, max skating speed, bursts over
20 mph, total distance skated, shot-location summary and zone-time share for
skaters; GAA, save percentage and shot-location breakdown for goalies.

This is a separate data source, not an input to the goal tracking. It runs as an
isolated step so a wholesale failure never touches the core archive. Edge
coverage starts in 2021-22, two seasons before the sprite floor.

The player list comes from the season's own PBP `rosterSpots`, so the main
scrape must have run first. A player with no Edge data for a season returns a
clean 404 — logged and skipped.
