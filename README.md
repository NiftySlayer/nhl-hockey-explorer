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

---

## Documentation

| Doc | What's in it |
|---|---|
| [docs/METHODS.md](docs/METHODS.md) | Every processing decision and the measurement behind it. The main document |
| [docs/FIELD_REFERENCE.md](docs/FIELD_REFERENCE.md) | **Field-level documentation for all four endpoints**, derived by profiling the raw archive, with confidence markers and open questions |
| [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) | The endpoints, what each returns, and their quirks |
| [docs/SCHEMA.md](docs/SCHEMA.md) | On-disk layout and output table columns |
| [docs/RAW_ARCHIVE.md](docs/RAW_ARCHIVE.md) | **The full 6.8 GB raw archive**, published separately — what's in it, per-file checksums, and coverage verified before publishing |

The field reference exists because none of these payloads are documented
anywhere. The best community reference,
[Zmalski/NHL-API-Reference](https://github.com/Zmalski/NHL-API-Reference),
covers endpoint paths and parameters but not response fields for any of them.
Everything in it was derived empirically and is marked ✅ verified,
⚠️ inferred, or ❓ unknown — including a list of what remains unresolved.

Two findings from that work worth surfacing here:

- **The play-by-play tells you the sprite URL.** Goal plays carry a
  `pptReplayUrl` field whose value is exactly the sprite endpoint, so the URL
  need not be constructed at all.
- **The sprite `timeStamp` is a wall clock**, not the opaque tick counter it is
  usually described as: deciseconds since the Unix epoch, stepping exactly +1
  per frame. That independently confirms the 10 fps rate from a second
  direction.

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

Individual steps (`scrape`, `shifts-html`, `edge`, `build`, `stints`) can be run
alone with `--steps`, and each module also has its own CLI.

⚠️ **If you run steps by hand, do not skip `shifts-html`.** The JSON shift feed
answers HTTP 200 with an empty body for whole blocks of games, so the build
falls back to HTML reports that are only on disk if that step ran. Skipping it
drops those games' on-ice rosters silently — no error, no warning, just missing
games. `--steps all` includes it.

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

## The raw archive is published separately

The repository itself carries code and documentation. The raw archive — 6.8 GB
of verbatim sprite, play-by-play, and shift JSON across three seasons — is
published on Zenodo rather than committed to git:
**[10.5281/zenodo.21608977](https://doi.org/10.5281/zenodo.21608977)**. It's a
permanent, checksummed, versioned mirror of a scrape against an undocumented
endpoint that could change or disappear without notice, which is the whole
reason to archive it rather than only document how to reproduce it.

Full contents, per-file sha256 manifests, and the coverage checks run before
publishing are in [docs/RAW_ARCHIVE.md](docs/RAW_ARCHIVE.md). The processed
tables are excluded the same way — they're mechanically re-derivable from raw/
with `run_pipeline.py --steps build`, so shipping them separately would just be
redundant with what the code already produces.

The `audit/` completeness tables are the exception to both. All three seasons
ship in this repo (836 KB), one row per goal, so every coverage and accuracy
claim above can be checked without downloading the raw archive or running a
multi-hour scrape:

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
