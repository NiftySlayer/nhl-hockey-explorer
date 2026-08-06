# The raw archive

The repository ships code, documentation, and the small audit tables. The raw
archive itself — 6.8 GB of verbatim sprite, play-by-play, and shift JSON across
three seasons — is published separately, because it is too large for git and
because it is the part of this project that is genuinely expensive to
reproduce: the endpoints are undocumented, unversioned, and could change or
disappear without notice.

**Archive DOI: [10.5281/zenodo.21608977](https://doi.org/10.5281/zenodo.21608977)**

---

## Why publish the raw bytes at all

Every processed table in this repository is mechanically re-derivable from
`raw/` — that's the whole point of writing bytes verbatim before parsing them.
But re-deriving requires re-scraping, and re-scraping requires the endpoint to
still exist, still answer, and still return the same shape it did in 2026. None
of that is guaranteed for an undocumented feed. Publishing the raw archive turns
a one-time, rate-limited, multi-hour scrape of a fragile source into a
permanent, versioned download — which is also what makes the pipeline itself
independently checkable: anyone can pull the archive and re-run
`build_processed.py` against it to confirm the processed tables in this repo
are what the code actually produces from that input.

---

## What's in it

Three zip files, one per season, each laid out so that extracting it at a
repository root reproduces `raw/` directly:

```
nhl-tracking-raw-20232024.zip
nhl-tracking-raw-20242025.zip
nhl-tracking-raw-20252026.zip
```

Each contains:

```
sprites/{season}/{game}/ev{event}.json     goal-recreation tracking, one file per goal
pbp/{season}/{game}.json                    play-by-play
shifts/{season}/{game}.json                 shift charts (JSON feed)
shifts_html/{season}/{game}_{TV|TH}.HTM     HTML TOI fallback, where the JSON feed came back empty
edge/{season}/{skater|goalie}/{player}.json NHL Edge season aggregates
```

| Season | Files | Raw size | Zip size |
|---|---|---|---|
| 2023-24 | 11,892 | 2.18 GB | 0.21 GB |
| 2024-25 | 11,829 | 2.43 GB | 0.23 GB |
| 2025-26 | 12,752 | 2.57 GB | 0.24 GB |

JSON compresses hard — the three zips total **0.68 GB against 7.18 GB raw**, about
9.5% of the original size.

Exact file counts, byte counts, and a **sha256 for every individual file** are
in `manifest-{season}.jsonl` (one JSON object per line: `path`, `bytes`,
`sha256`), uploaded alongside the zips. A separate sha256 for each zip file
itself is in `build_summary.json` and in the Zenodo record's own checksum
field.

---

## Coverage, checked directly

This is not "the scrape ran, here's what came out" — the archive was audited
against expectations before publishing:

| Check | Result |
|---|---|
| Play-by-play files per season | 1,312 / 1,312 / 1,312 — every scheduled regular-season game |
| Sprite game-directories per season | 1,312 / 1,312 / 1,311 |
| JSON shift files per season | 1,312 / 1,312 / 1,312 |
| HTML shift-fallback files per season | 0 / 114 / 1,010 |

The one season short on sprite directories (2025-26) is not a scrape failure.
Game `2025020349` ended 0–1, decided entirely in the shootout — every recorded
"goal" in its play-by-play has `periodDescriptor.periodType == "SO"`, meaning
zero goals occurred in regulation or overtime. The pipeline's own
`MAX_GOAL_PERIOD = 4` filter correctly never requested a sprite for it, so no
directory was ever created. This is the pipeline behaving correctly, not a gap.

The HTML fallback counts (114 = 57 games × 2 reports, 1,010 = 505 games × 2)
match the documented per-season figures in
[METHODS.md](METHODS.md) exactly, confirming that fallback fetch ran to
completion for both affected seasons before this archive was packaged — not
just partially.

Coverage of regulation-and-overtime goals with usable tracking: **99.25% /
99.92% / 99.93%**, detailed per-goal in the `audit/completeness_{season}.parquet`
tables already committed to this repository (no need to download the raw
archive just to check that).

---

## Verifying a download

```python
import hashlib, json
from pathlib import Path

manifest = [json.loads(l) for l in Path("manifest-20242025.jsonl").open()]
bad = []
for row in manifest:
    p = Path(row["path"])          # after extracting the zip at a repo root
    if not p.exists():
        bad.append((row["path"], "missing"))
        continue
    actual = hashlib.sha256(p.read_bytes()).hexdigest()
    if actual != row["sha256"]:
        bad.append((row["path"], "checksum mismatch"))

print(f"{len(manifest) - len(bad)}/{len(manifest)} files verified")
```

---

## Rebuilding instead of downloading

If you'd rather scrape it yourself than download 6.8 GB — the endpoints are
public, and the pipeline is idempotent and rate-limited to be polite about it:

```bash
python src/run_pipeline.py --root . --seasons 20242025 20232024 20252026
```

See [README §Running it](../README.md#running-it) and
[docs/METHODS.md §1](METHODS.md) for the scraping discipline this follows.

---

## License and provenance

The archive is a verbatim mirror of publicly accessible NHL data, packaged and
checksummed by this project. It is not affiliated with or endorsed by the
National Hockey League. The pipeline code that produced it is MIT-licensed
(see [LICENSE](../LICENSE)); the underlying data belongs to its original source.
