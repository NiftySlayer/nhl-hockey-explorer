# Field reference

Field-level documentation for the NHL endpoints this pipeline reads.

**None of this is officially documented.** The community reference at
[Zmalski/NHL-API-Reference](https://github.com/Zmalski/NHL-API-Reference) covers
endpoint paths and parameters but not response fields for any of these payloads.
Everything below was derived by profiling the raw archive (three seasons,
~24,000 goal sprites and 3,936 games), and every semantic claim that could be
tested against another field was tested.

**Confidence is marked throughout:**

| | |
|---|---|
| ✅ | Verified against data — the decode was tested and holds |
| ⚠️ | Inferred from naming and observed values; consistent but not independently confirmed |
| ❓ | **Unknown.** Observed in the payload, meaning not determined |

Counts and percentages come from a 40–200 game sample of 2024-25 unless stated.

---

## 1. Sprites — `wsr.nhle.com/sprites/{season}/{game}/ev{event}.json`

### The URL does not have to be constructed

✅ The play-by-play carries it directly. Goal plays include a `pptReplayUrl`
field whose value is exactly the sprite URL:

```json
"pptReplayUrl": "https://wsr.nhle.com/sprites/20242025/2024020001/ev274.json"
```

This pipeline builds the URL from a template instead (`pipeline_common.SPRITE_URL`),
which works, but reading `pptReplayUrl` off the play would be more robust to a
host change. Present on 396 of 19,859 plays in the sample — goals, and a handful
of other replay-eligible events.

Zmalski documents a related pair of endpoints, `/v1/ppt-replay/goal/{game}/{event}`
and `/v1/ppt-replay/{game}/{event}`. ❓ Their relationship to the `wsr.nhle.com`
sprite payload is untested here — they may be the same data behind a different
host, or a different structure entirely.

### Structure

✅ **The file is a bare JSON array of frames.** There is no wrapper object — the
top level is a list.

```json
[
  {"timeStamp": 17280637538,
   "onIce": {
     "1":    {"id": 1,    "playerId": "",      "x": 1778.6, "y": 247.7,  "sweaterNumber": "", "teamId": "", "teamAbbrev": ""},
     "7004": {"id": 7004, "playerId": 8481524, "x": 2324.4, "y": 624.0,  "sweaterNumber": 4,  "teamId": 7,  "teamAbbrev": "BUF"}
   }},
  ...
]
```

| Field | Type | Meaning | |
|---|---|---|---|
| `timeStamp` | int | **Deciseconds since the Unix epoch — see below** | ✅ |
| `onIce` | dict | Keyed by entity id (string). **Key `"1"` is the puck** | ✅ |
| `onIce[k].id` | int | Entity id. Always equals `int(k)` — the dict key duplicated | ✅ |
| `onIce[k].playerId` | int | Universal NHL player id. **Empty string `""` for the puck** | ✅ |
| `onIce[k].x`, `.y` | float | Position in **inches from the rink corner at standard (−100, +42.5)**. Ranges 0–2400 and 0–1020 in. See [METHODS §3](METHODS.md) for the transform and the y-axis inversion | ✅ |
| `onIce[k].sweaterNumber` | int | Jersey number; `""` for the puck | ✅ |
| `onIce[k].teamId`, `.teamAbbrev` | int, str | Team; `""` for the puck | ✅ |

❓ **What the entity id actually is.** Non-puck ids look like 1008, 1013, 7004 —
they are neither player ids nor sweater numbers. They are stable within a game
and map one-to-one to a `playerId`, so they behave like a per-game tracking-tag
or chip id. Whether they persist across games is untested. **Do not join on
them** — join on `playerId`.

### `timeStamp` is a wall clock

⚠️ **Widely described as an opaque tick counter. It is tenths of a second since
the Unix epoch.**

```
17280637538 / 10 = 1728063753.8  →  2024-10-04 17:42:33.8 UTC
```

That game's PBP records `gameDate: 2024-10-04` and `startTimeUTC: 17:00:00Z`, so
the first goal's clip lands 42 minutes of real time into the broadcast. Checked
further: the timestamp gap between consecutive goals in a game runs 2.0–3.9×
the game-clock gap between them, which is what elapsed real time should look
like once stoppages and intermissions are included.

Two consequences:

1. ✅ **The frame rate is confirmed directly.** The step between consecutive
   frames is exactly `+1`, with no exceptions in any file checked, so one frame
   = 0.1 s = **10 fps**. This corroborates the speed-test derivation in
   [METHODS §4](METHODS.md) independently.
2. It gives an absolute wall-clock anchor for each clip, usable to align sprites
   against other time-stamped feeds.

Marked ⚠️ rather than ✅ because the epoch interpretation is an inference from a
good fit, not from documentation. The operational guidance is the same either
way: **`dt` = 0.1 s.** This pipeline treats the field as ordering only and
hardcodes `SECONDS_PER_FRAME = 0.1`.

### Frame counts

✅ 119–121 frames in 2023-24 (~12 s), ~140 in later seasons (~14 s), ~210 in
overtime. Window length varies; the rate does not.

The whole array is available in tabular form: `processed/tracking/{season}.parquet`
([SCHEMA](SCHEMA.md)) is one row per entity per frame, with the fields above
decoded — `x`/`y` in feet, `timeStamp` carried through verbatim, and `entity_type`
separating the puck from the players. It is opt-in (`--steps tracking`).

---

## 2. Play-by-play — `api-web.nhle.com/v1/gamecenter/{game}/play-by-play`

### Top level

✅ `id`, `season`, `gameType`, `gameDate`, `startTimeUTC`, `venue`,
`venueLocation`, `venueUTCOffset`, `easternUTCOffset`, `homeTeam`, `awayTeam`,
`periodDescriptor`, `clock`, `plays`, `rosterSpots`, `summary`, `tvBroadcasts`,
`gameState`, `gameOutcome`, `gameScheduleState`, `displayPeriod`, `maxPeriods`,
`regPeriods`, `otInUse`, `shootoutInUse`, `limitedScoring`, `specialEvent`.

❓ `limitedScoring`, `specialEvent`, `gameScheduleState`, `displayPeriod` —
observed but their semantics were not determined. None are used by this pipeline.

### `rosterSpots[]`

✅ Exactly seven fields, present on every entry:

| Field | Notes |
|---|---|
| `playerId` | **The universal join key** |
| `teamId` | |
| `sweaterNumber` | With `teamId`, this is what resolves HTML TOI reports to player ids ([METHODS §2](METHODS.md)) |
| `firstName`, `lastName` | Localised objects — read `.default` |
| `positionCode` | `C`/`L`/`R`/`D`/`G` |
| `headshot` | Image URL |

### `plays[]`

✅ Every play carries all of these; only `details` and `pptReplayUrl` are
conditional.

| Field | Present | Notes |
|---|---|---|
| `eventId` | 100% | **Joins to the sprite filename**: `eventId` 274 ↔ `ev274.json` |
| `periodDescriptor` | 100% | `{number, periodType, maxRegulationPeriods}`. `periodType` ∈ `REG`, `OT`, `SO` |
| `timeInPeriod` | 100% | `"MM:SS"` **elapsed** in the period |
| `timeRemaining` | 100% | `"MM:SS"` remaining — the complement |
| `situationCode` | 100% | Four digits; see below |
| `homeTeamDefendingSide` | 100% | `"left"` / `"right"`. **The end the HOME team defends** in that period |
| `typeCode` | 100% | Numeric event code; mapping below |
| `typeDescKey` | 100% | String event type |
| `sortOrder` | 100% | ✅ Unique and strictly increasing within a game, but **not contiguous** (observed range 10–874 across 349 plays). Use it for ordering, never as an index |
| `details` | 97.7% | Absent on `period-start`, `period-end`, `game-end` |
| `pptReplayUrl` | 2.0% | The sprite URL (see §1) |

⚠️ `homeTeamDefendingSide` was present on 100% of plays in the sample, but this
pipeline still carries a fallback for its absence, having encountered it missing
in some early-season files. Treat it as near-universal, not guaranteed.

### `typeCode` ↔ `typeDescKey`

✅ Complete mapping observed across 40 games:

| Code | Key | | Code | Key |
|---|---|---|---|---|
| 502 | `faceoff` | | 516 | `stoppage` |
| 503 | `hit` | | 520 | `period-start` |
| 504 | `giveaway` | | 521 | `period-end` |
| 505 | `goal` | | 523 | `shootout-complete` |
| 506 | `shot-on-goal` | | 524 | `game-end` |
| 507 | `missed-shot` | | 525 | `takeaway` |
| 508 | `blocked-shot` | | 535 | `delayed-penalty` |
| 509 | `penalty` | | | |

❓ Codes in the gaps (510–515, 517–519, 522, 526–534) were never observed. They
may be unused, or may cover event types absent from the sample. Do not assume
the list is exhaustive.

⚠️ **`typeCode` is overloaded.** The play-level `typeCode` above is an integer
event code. Inside `details` on a *penalty*, `typeCode` is a **string** severity
(`"MIN"`, `"MAJ"`, …). And in the shiftcharts feed, `typeCode` is yet another
integer namespace (§3). Three unrelated meanings, same name.

### `situationCode`

✅ **Four digits: `[awayGoalie, awaySkaters, homeSkaters, homeGoalie]`.**

`"1551"` = away goalie in, 5 away skaters, 5 home skaters, home goalie in — 5v5.
`"0651"` = away goalie **pulled**, 6 away skaters vs 5 home — a 6v5 with the
away net empty. `"1560"` = the mirror image.

Verified by cross-checking against `goalieInNetId`: across 1,287 goals, the
conceding team's goalie digit was `0` in exactly the 86 cases where the PBP
reported no `goalieInNetId`, and `1` in all 1,201 where it did. Clean separation,
no exceptions.

Observed frequencies: `1551` 80.3%, `1451` 7.3%, `1541` 6.5%, `0651` 1.7%,
`1560` 1.7%, then a long tail. ❓ `"1010"` appears 22 times and does not fit the
pattern — likely a shootout or a data artifact. Not investigated.

### `details.*` by event type

Presence rates are within that event type.

**`goal`** ✅
| Field | Present | Notes |
|---|---|---|
| `xCoord`, `yCoord` | 100% | **The NHL scorer's own record of the shot location.** This is what makes independent validation possible ([METHODS §10](METHODS.md)) |
| `zoneCode` | 100% | See below |
| `shotType` | 100% | |
| `scoringPlayerId` | 100% | **The anchor for shot-frame detection** |
| `eventOwnerTeamId` | 100% | The scoring team |
| `awayScore`, `homeScore` | 100% | Score **after** this goal |
| `scoringPlayerTotal` | 97.5% | ⚠️ Season goal total for that player |
| `goalieInNetId` | 93.7% | Absent ⇒ empty net |
| `assist1PlayerId`, `assist1PlayerTotal` | 90.4% | |
| `assist2PlayerId`, `assist2PlayerTotal` | 72.0% | |
| `discreteClip`, `highlightClip`, `highlightClipSharingUrl` | 92–97% | Video asset ids/URLs. `*Fr` variants are French-language |
| `goalInGame` | 11.8% | ❓ Meaning unclear. Sample values are small integers, and it is absent from most goals — an odd combination if it were simply "nth goal of the game" |

**`shot-on-goal`** ✅ — `xCoord`, `yCoord`, `zoneCode`, `shotType`,
`shootingPlayerId`, `goalieInNetId`, `eventOwnerTeamId`, `awaySOG`, `homeSOG`
(running shot totals). All 100%.

**`missed-shot`** ✅ — same as above minus the SOG counters, plus `reason`
(100%): `wide-left`, `wide-right`, `high-and-wide-left`, `high-and-wide-right`,
`above-crossbar`, `hit-left-post`, `hit-right-post`, `hit-crossbar`, `short`.
`goalieInNetId` present 98.9%.

**`blocked-shot`** ✅ — `xCoord`, `yCoord`, `zoneCode`, `shootingPlayerId`,
`blockingPlayerId`, `eventOwnerTeamId`, `reason` (`blocked`,
`teammate-blocked`, `other-block`).

> ⚠️ **`eventOwnerTeamId` on a blocked shot is the BLOCKING team**, not the
> shooting team. Getting this wrong silently inverts shot attribution for ~2,100
> events per 60 games. This pipeline stores both the team and `shootingPlayerId`
> so the consumer can decide.

**`faceoff`** ✅ — `winningPlayerId`, `losingPlayerId`, `eventOwnerTeamId`
(the winning team), `xCoord`, `yCoord`, `zoneCode`.

**`penalty`** ✅ — `typeCode` (string severity: `MIN`, `MAJ`, `BEN`, `MIS`,
`MAT`), `descKey` (infraction, e.g. `slashing`), `duration` (minutes),
`committedByPlayerId` (97.3%), `drawnByPlayerId` (92.9%), `servedByPlayerId`
(5.2% — bench minors, where someone else serves it), coordinates.

**`hit`** ✅ — `hittingPlayerId`, `hitteePlayerId`, `eventOwnerTeamId`, coords.

**`giveaway` / `takeaway`** ✅ — `playerId` (note: bare `playerId`, not a
role-specific name), `eventOwnerTeamId`, coords.

**`stoppage`** ✅ — `reason`, with values including `goalie-stopped-after-sog`,
`icing`, `offside`, `puck-in-netting`, `tv-timeout`, `puck-in-crowd`,
`puck-frozen`, `puck-in-benches`, `referee-or-linesman`, `hand-pass`,
`high-stick`, `home-timeout`.

### `zoneCode`

✅ **Relative to the event-owner team**, not to the home team. Tested on
2,332 shots and goals: 95.6% carry `O`, which is only coherent if `O` means the
*shooting* team's offensive zone. Values `O`, `D`, `N`.

The `D` and `N` cases are real — long-range attempts and empty-net shots from
behind the centre line.

### `shotType`

✅ Observed values, by frequency: `wrist`, `snap`, `slap`, `tip-in`, `backhand`,
`deflected`, `wrap-around`, `poke`, `bat`, `between-legs`, `cradle`.

Shot-frame accuracy varies by type — see the table in
[METHODS §7.4](METHODS.md). Tips and deflections locate well but have low
scorer-match rates, because of the net-front traffic they occur in;
wrap-arounds and backhands are the weaker cases for locating the release.

---

## 3. Shift charts — `api.nhle.com/stats/rest/en/shiftcharts?cayenneExp=gameId={game}`

✅ Top level is `{data: [...], total: n}`.

> ⚠️ **`total: 0` with HTTP 200 is the documented failure mode** — a successful,
> empty response for whole blocks of games ([METHODS §1](METHODS.md),
> [DATA_SOURCES §3](DATA_SOURCES.md)). Always check `total` or `len(data)`;
> the status code will not tell you.

### `data[]`

| Field | Notes | |
|---|---|---|
| `playerId` | **The join key** | ✅ |
| `gameId`, `teamId`, `teamAbbrev`, `teamName` | | ✅ |
| `firstName`, `lastName` | Plain strings here, unlike the PBP's localised objects | ✅ |
| `period` | int | ✅ |
| `startTime`, `endTime` | `"MM:SS"` **elapsed in period** — same convention as the PBP | ✅ |
| `duration` | `"MM:SS"`. **`null` on goal-marker rows** | ✅ |
| `shiftNumber` | Sequential per player per game | ✅ |
| `id` | Row id | ✅ |
| `typeCode` | `517` = a real shift; `505` = a goal marker | ✅ |
| `hexValue` | Team colour, e.g. `"#C8102E"` | ⚠️ |
| `eventNumber` | Matches the PBP `eventId` on marker rows | ⚠️ |
| `eventDescription` | `EVG`, `PPG`, `SHG`, `EN`, `Shootout` on marker rows; `null` on shifts | ✅ |
| `eventDetails` | Assist names as a comma-joined string on marker rows | ✅ |
| `detailCode` | `0` on shifts; `801`–`807` on marker rows | ❓ |

> ⚠️ **The feed mixes two row types.** `typeCode` 505 rows are goal markers, not
> shifts: `startTime == endTime`, `duration` is null, and `eventDescription` is
> set. Including them as shifts corrupts any time-on-ice calculation. This
> pipeline filters on the presence of both `startTime` and `endTime`, which
> excludes them.

❓ **`detailCode` on marker rows.** Values 801–807 correlate loosely with
`eventDescription` but not one-to-one — `803` appears with `EVG`, `PPG`, `EN`
and `Shootout`. Plausibly an assist-count or strength encoding, but the observed
combinations do not resolve it. **Not decoded.**

---

## 4. HTML TOI reports — `nhl.com/scores/htmlreports/{season}/T{V|H}{gamenum}.HTM`

Not JSON. `TV` is the visiting team, `TH` the home team, and the game number is
the last six digits of the game id.

✅ Structure:

```
td.teamHeading    -> "CAROLINA HURRICANES"
td.playerHeading  -> "4 GOSTISBEHERE, SHAYNE"     (sweater + name)
then shift rows:   Shift# | Per | Start Elapsed/Game | End Elapsed/Game |
                   Duration | Event
```

✅ Time cells read `"1:38 / 18:22"` — elapsed-in-period / clock-remaining. Take
the **elapsed** value to match the JSON feed's `startTime` semantics.

✅ Period cells are digits, or `OT`, or `SO`. This pipeline maps `OT` → period 4
and drops `SO` as not being real ice time.

> **The report exposes only sweater number and name — no player id.** Never key
> on either. Resolve sweater to `playerId` through that game's own PBP
> `rosterSpots` ([METHODS §2](METHODS.md)), and take team identity from which
> file it is rather than by matching team-name strings.

---

## 5. NHL Edge — `api-web.nhle.com/v1/edge/{skater|goalie}-detail/{playerId}/{season}/{gameType}`

Season-level per-player aggregates. Coverage starts in 2021-22, two seasons
earlier than the sprite floor. A player with no data for a season returns a
clean 404.

⚠️ Field paths this pipeline reads (nested; `build_processed._nav` walks them):

**Skaters** — `player.{id, position, team.abbrev, gamesPlayed}`,
`topShotSpeed.{imperial, percentile}`,
`skatingSpeed.speedMax.{imperial, percentile}`,
`skatingSpeed.burstsOver20.value`,
`totalDistanceSkated.{imperial, percentile}`,
`zoneTimeDetails.{offensiveZonePctg, defensiveZonePctg, neutralZonePctg}`,
and `sogSummary[]` — a list of rows keyed by `locationCode`, from which the
`"all"` row gives `shots`, `goals`, `shootingPctg`.

**Goalies** — `player.*`, `stats.goalsAgainstAvg.value`,
`stats.goalDifferentialPer60.value`, `stats.pointPctg.value`, and
`shotLocationSummary[]` keyed the same way, `"all"` row giving `saves`,
`goalsAgainst`, `savePctg`.

❓ **The other `locationCode` values are not documented here.** Both summary
lists contain per-location breakdowns beyond `"all"`; this pipeline reads only
the aggregate row. ❓ The full set of available Edge metrics has not been
enumerated — these are the fields the pipeline needed, not everything the
endpoint returns.

---

## Summary of open questions

Everything marked ❓ above, collected:

| Question | Where |
|---|---|
| What the sprite entity `id` represents, and whether it persists across games | §1 |
| Whether `/v1/ppt-replay/goal/...` serves the same payload as `wsr.nhle.com/sprites/...` | §1 |
| Semantics of `limitedScoring`, `specialEvent`, `gameScheduleState`, `displayPeriod` | §2 |
| Unobserved `typeCode` values (510–515, 517–519, 522, 526–534) | §2 |
| `details.goalInGame` — present on only 11.8% of goals | §2 |
| `situationCode` `"1010"` — does not fit the four-digit pattern | §2 |
| Shift `detailCode` 801–807 | §3 |
| Edge `locationCode` values beyond `"all"`, and the full metric set | §5 |

Corrections and additions are welcome — open an issue.
