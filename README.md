# NHL goal-tracking pipeline

A documented, reproducible pipeline for extracting NHL player-tracking data from
the public goal-recreation feed, reconciling it against the official
play-by-play and shift charts, and turning it into analysis-ready tables.

The NHL exposes frame-by-frame positional tracking for **goal events only**,
through the public goal-recreation ("sprites") endpoint. This repository
scrapes that feed, works out *when in each 12–14 second clip the shot was
actually taken*, and measures every on-ice player's distance to the puck at
that instant — with an audit table saying, per goal, how much to trust it.

Three seasons are reachable: **2023-24, 2024-25 and 2025-26**, 1,312
regular-season games each, roughly **24,000 goal events** and 6.8 GB of raw
JSON.

Author: Elliott Kervin.

---

## Why this is not just a scraper

Scraping the endpoint is the easy part. The hard part is that the raw feed is
misleading in five specific ways, each of which silently corrupts any distance
you compute from it. All five are handled here, and each fix is calibrated
against data rather than assumed:

**1. The last frame is not the goal.** The feed keeps recording while the puck
sits in the net — a median **3.4 seconds** of dead time, during which the
scorer skates in to celebrate. Anything anchored on the last valid frame
measures the celebration. Detected by puck motion: the puck travels a median
**1.99 ft/frame during play and 0.15 ft/frame once dead**, a 13× separation that
makes the boundary unambiguous.

**2. The goal frame is unusable as a measurement point.** When the puck is in
the net, the conceding team's defencemen are *mechanically* the closest players
on the ice. The nearest player at the goal frame is the recorded scorer only
**4.6%** of the time. Any distance measured there is outcome-conditioned: it
mostly tells you where the net is.

**3. Inferring the shooter from puck kinematics does not work.** Scoring
near-stick frames on net-ward travel, acceleration and recency identified the
right shooter **47%** of the time. Inverting the problem does: the play-by-play
already names the scorer, so search for *when* the puck was last at that player
instead of *who* had it. That removes shooter identification as a failure mode
entirely, leaving only the timing to validate — and the shot location it implies
sits a median **2.04 ft** from where the NHL's own scorer placed the shot, with
90% within 10 ft, across all 23,888 goals where both exist.

**4. Benches empty on overtime winners.** The tracker picks up everyone who
comes over the boards — up to **35 "on-ice" players**, one 3v3 goal showing 17
skaters for a single team. Handled by plausibility bounds, and definitively by
taking rosters from the shift charts instead of from tracking.

**5. The shift-chart feed silently returns nothing.** HTTP 200 with `total = 0`,
for whole contiguous blocks of games: 57 in 2024-25 and **505 in 2025-26, 38% of
the season**. Those games have no on-ice rosters at all unless you fall back to
the classic HTML TOI reports, which this pipeline does — recovering TOI that
correlates **0.99996** with the JSON feed where both exist.

There is a sixth, quieter one worth reading if you plan to use the coordinates:
the sprite y-axis runs **opposite** to the play-by-play's, and because every
distance reduces to a `hypot` around a net at `y = 0`, getting it backwards is
invisible in every distance the pipeline emits. See
[docs/METHODS.md §3](docs/METHODS.md).

Full write-up of all of it, with the numbers behind each claim, in
[docs/METHODS.md](docs/METHODS.md).

---

## What it produces

| Table | Grain | What it carries |
|---|---|---|
| `processed/events/{season}.parquet` | goal × on-ice player | `d_shotframe` — distance to the puck at the inferred release, window-averaged. Plus `d_goalframe` for comparison, position, team, scorer flag |
| `processed/shots/{season}.parquet` | shot attempt | Every goal, shot on goal, missed shot and blocked shot, with location and situation |
| `processed/shifts/{season}.parquet` | player shift | Start/end in absolute game seconds, from the JSON feed or the HTML fallback |
| `processed/stints/{season}.parquet` | stint | Maximal intervals with no substitution, swept from the shifts |
| `processed/stint_players/{season}.parquet` | stint × player | Who was on the ice for each stint |
| `processed/games`, `faceoffs`, `players` | — | Dimension tables |
| `audit/completeness_{season}.parquet` | PBP goal | **The audit.** Did tracking resolve, how, and how much to trust it |

Column-level detail in [docs/SCHEMA.md](docs/SCHEMA.md).

**Coverage of regulation and overtime goals: 99.25% / 99.92% / 99.93%** across
the three seasons. The audit table is a first-class output, not a log — the
goals where tracking fails, and the reasons, are part of what the pipeline is
for.

---

## Running it

```bash
pip install -r requirements.txt
```

Always run from the repository root; `--root` is where the data archive lives.

```bash
python src/run_pipeline.py --root . --seasons 20242025
```

That scrapes one season and builds every table — roughly two hours, almost all
of it waiting on the rate limit. For the full archive:

```bash
python src/run_pipeline.py --root . --seasons 20242025 20232024 20252026
```

The scrape is **idempotent**: files already on disk are skipped, so an
interrupted overnight run resumes for free. Raw bytes are written **verbatim
before anything parses them**, so when a parse rule changes you re-derive rather
than re-fetch:

```bash
python src/run_pipeline.py --root . --steps build --seasons 20242025
```

To check the shot detection against the play-by-play's own coordinates:

```bash
python src/shotframe_validation.py --root . --seasons 20242025 --sample 3000
```

Individual steps (`scrape`, `edge`, `build`, `stints`) can be run alone with
`--steps`, and each module also has its own CLI.

---

## Module map

`src/` is flat on purpose — the modules import each other as siblings, which
works because Python puts the running script's directory on `sys.path`.

| Module | Role |
|---|---|
| `pipeline_common.py` | Shared plumbing: headers, endpoints, polite HTTP with retry/backoff, the coordinate transform, `Layout` (every path), JSONL logging. No network side effects at import |
| `scrape_raw.py` | The network job. Sprites, play-by-play and shift charts, written verbatim |
| `shifts_html.py` | HTML TOI fallback for games the JSON shift feed serves empty |
| `edge_scrape.py` | NHL Edge season aggregates. An isolated second source; reaches back to 2021-22 |
| `build_processed.py` | The offline transform. Shot-frame detection, per-player distances, the completeness audit |
| `stints.py` | Sweeps shifts into stint intervals and on-ice rosters |
| `run_pipeline.py` | One command for the whole thing |
| `shotframe_validation.py` | Grades the inferred shot against the play-by-play's own goal coordinates — the check that can actually fail |

---

## Data is not included

The repository carries code and documentation. The raw archive is 6.8 GB and
the processed tables are derived from it; both are excluded. Everything is
reproducible from the endpoints with `run_pipeline.py`.

The `audit/` completeness tables are the exception. All three seasons ship
(836 KB), one row per goal, so every coverage and accuracy claim above can be
checked without running a multi-hour scrape:

```python
import pandas as pd
au = pd.concat(pd.read_parquet(f"audit/completeness_{s}.parquet")
               for s in ("20232024", "20242025", "20252026"))

err = au.shot_pbp_err_ft.dropna()
err.median()          # 2.04 ft from the play-by-play's own shot location
(err <= 10).mean()    # 0.901
au.shot_confidence.value_counts()
```

---

## On scraping responsibly

These are public but **undocumented** NHL endpoints. This pipeline was built to
assemble a research archive once, and it is written accordingly:

- One request at a time, single-threaded, **0.7 s apart**. No parallel bursting.
- Retry with exponential backoff. 403 and 404 are treated as terminal answers
  and logged, never hammered.
- Idempotent, so re-running costs the endpoint nothing.
- Raw bytes written verbatim, so the archive never needs re-fetching when
  parsing changes.

Browser headers (User-Agent, Referer, Origin) are set because the endpoints
require them. Please keep the rate limit if you run this, respect the NHL's
terms of use, and note that undocumented endpoints can change or disappear
without notice — which is exactly why the raw archive is treated as immutable
here.

This project is not affiliated with or endorsed by the National Hockey League.

---

## License

MIT — see [LICENSE](LICENSE).
