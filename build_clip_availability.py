from pathlib import Path
import pandas as pd


TRACKING_DIR = Path("processed/tracking")
OUTPUT_PATH = Path("processed/clip_availability.parquet")


all_clips = []


for parquet_path in sorted(TRACKING_DIR.glob("*.parquet")):

    season = parquet_path.stem

    print(f"Reading {season}...")

    df = pd.read_parquet(
        parquet_path,
        columns=[
            "game_id",
            "event_id"
        ]
    )

    df = (
        df[
            [
                "game_id",
                "event_id"
            ]
        ]
        .drop_duplicates()
    )

    df.insert(
        0,
        "season",
        int(season)
    )

    all_clips.append(df)


clip_availability = pd.concat(
    all_clips,
    ignore_index=True
)


clip_availability = (
    clip_availability
    .drop_duplicates()
    .sort_values(
        [
            "season",
            "game_id",
            "event_id"
        ]
    )
)


clip_availability.to_parquet(
    OUTPUT_PATH,
    index=False
)


print()
print(
    f"Created {OUTPUT_PATH}"
)

print(
    f"Available clips: {len(clip_availability):,}"
)