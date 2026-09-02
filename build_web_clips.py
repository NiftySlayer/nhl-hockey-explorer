from pathlib import Path
import pandas as pd


# ============================================================
# PATHS
# ============================================================

TRACKING_DIR = Path("processed/tracking")
OUTPUT_DIR = Path("processed/web_clips")

OUTPUT_DIR.mkdir(
    parents=True,
    exist_ok=True
)


# ============================================================
# COLUMNS NEEDED BY THE WEB PLAYER
# ============================================================

COLUMNS = [
    "game_id",
    "event_id",
    "frame_idx",
    "seconds_to_goal",
    "entity_type",
    "player_id",
    "team_id",
    "sweater_number",
    "is_goalie",
    "is_scorer",
    "x",
    "y",
    "home_team_id",
    "away_team_id",
]


# ============================================================
# BUILD EACH SEASON
# ============================================================

for parquet_path in sorted(
    TRACKING_DIR.glob("*.parquet")
):

    season = parquet_path.stem

    print()
    print("=" * 70)
    print(f"SEASON {season}")
    print("=" * 70)

    print("Reading tracking data...")

    df = pd.read_parquet(
        parquet_path,
        columns=COLUMNS
    )

    clip_count = 0


    # --------------------------------------------------------
    # ONE FILE PER GAME / EVENT
    # --------------------------------------------------------

    for (
        game_id,
        event_id
    ), clip in df.groupby(
        [
            "game_id",
            "event_id"
        ],
        sort=False
    ):

        game_id = int(game_id)
        event_id = int(event_id)

        game_dir = (
            OUTPUT_DIR
            / season
            / str(game_id)
        )

        game_dir.mkdir(
            parents=True,
            exist_ok=True
        )

        output_path = (
            game_dir
            / f"ev{event_id}.parquet"
        )

        clip = clip.sort_values(
            "frame_idx"
        )

        clip.to_parquet(
            output_path,
            index=False,
            compression="zstd"
        )

        clip_count += 1

        if clip_count % 1000 == 0:
            print(
                f"  {clip_count:,} clips written..."
            )


    print(
        f"{season}: {clip_count:,} clips"
    )


print()
print("=" * 70)
print("FINISHED")
print("=" * 70)
print()

print(
    f"Web clips saved under: "
    f"{OUTPUT_DIR}"
)