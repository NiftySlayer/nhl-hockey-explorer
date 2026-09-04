from flask import Flask, request, render_template
from rink_data import RINK_DATA_URL
import json
import os
import io
import time
import boto3
import pandas as pd
from botocore.config import Config
from botocore.exceptions import ClientError


app = Flask(__name__)


# ============================================================
# CLOUDFLARE R2
# ============================================================

R2_BUCKET = os.environ["R2_BUCKET"]


# ------------------------------------------------------------
# R2 CONNECTION SETTINGS
#
# We do not want a single stalled request to sit for 20+ sec.
#
# connect_timeout:
#   Maximum time to establish the connection.
#
# read_timeout:
#   Maximum time to wait while reading the object.
#
# retries:
#   If a request fails/stalls, boto3 can try again.
#
# max_pool_connections:
#   Allows connections to be reused efficiently.
# ------------------------------------------------------------

r2_config = Config(

    connect_timeout=3,

    read_timeout=5,

    retries={
        "max_attempts": 3,
        "mode": "standard",
    },

    max_pool_connections=20,

    tcp_keepalive=True,
)


r2 = boto3.client(

    "s3",

    endpoint_url=os.environ["R2_ENDPOINT"],

    aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],

    aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],

    region_name="auto",

    config=r2_config,
)


# ============================================================
# HOME PAGE
# ============================================================

@app.route("/")
def home():

    return """
    <html>
        <head>
            <title>NHL Goal Clips</title>
        </head>

        <body>
            <h1>NHL Goal Clips</h1>

            <p>
                The web app is running.
            </p>
        </body>
    </html>
    """


# ============================================================
# CLIP PAGE
# ============================================================

@app.route("/clip")
def clip():

    start_total = time.perf_counter()


    # --------------------------------------------------------
    # READ VALUES FROM URL
    # --------------------------------------------------------

    season = request.args.get("season")
    game_id = request.args.get("game_id")
    event_id = request.args.get("event_id")

    game_date = request.args.get("date")

    home_abbr = request.args.get("home")
    away_abbr = request.args.get("away")

    scorer = request.args.get("scorer")


    # --------------------------------------------------------
    # VALIDATE REQUIRED VALUES
    # --------------------------------------------------------

    if (
        not season
        or not game_id
        or not event_id
        or not game_date
        or not home_abbr
        or not away_abbr
        or not scorer
    ):

        return (
            "Missing required clip information",
            400
        )


    # --------------------------------------------------------
    # CONVERT IDS TO INTEGERS
    # --------------------------------------------------------

    try:

        game_id = int(game_id)
        event_id = int(event_id)

    except ValueError:

        return (
            "game_id and event_id must be numbers",
            400
        )


    # --------------------------------------------------------
    # BUILD R2 OBJECT KEY
    # --------------------------------------------------------

    object_key = (
        f"{season}/"
        f"{game_id}/"
        f"ev{event_id}.parquet"
    )


    print("")
    print("========================================")
    print("R2 CLIP REQUEST")
    print("Season:", season)
    print("Game:", game_id)
    print("Event:", event_id)
    print("R2 Key:", object_key)
    print("========================================")


    # ========================================================
    # R2 DOWNLOAD
    # ========================================================

    start_r2 = time.perf_counter()

    try:

        r2_object = r2.get_object(
            Bucket=R2_BUCKET,
            Key=object_key
        )

        parquet_bytes = r2_object["Body"].read()

        print(
            "Downloaded bytes:",
            len(parquet_bytes)
        )


    except ClientError as e:

        error_code = (
            e.response
            .get("Error", {})
            .get("Code", "")
        )


        if error_code in (
            "NoSuchKey",
            "404",
            "NotFound"
        ):

            return (
                f"""
                <h2>Tracking parquet not found</h2>

                <p>Season: {season}</p>

                <p>Game ID: {game_id}</p>

                <p>Event ID: {event_id}</p>

                <p>R2 Key: {object_key}</p>
                """,
                404
            )


        return (
            f"<h2>Error accessing R2</h2>"
            f"<pre>{e}</pre>",
            500
        )


    except Exception as e:

        return (
            f"""
            <h2>Unable to load goal clip</h2>

            <p>
                The clip storage request timed out or could not
                be completed.
            </p>

            <p>
                Please refresh the page and try again.
            </p>

            <pre>{e}</pre>
            """,
            503
        )


    end_r2 = time.perf_counter()


    # ========================================================
    # READ PARQUET
    # ========================================================

    start_parquet = time.perf_counter()

    try:

        clip_data = pd.read_parquet(
            io.BytesIO(parquet_bytes),
            dtype_backend="pyarrow"
        )

    except Exception as e:

        return (
            f"<h2>Error reading tracking data</h2>"
            f"<pre>{e}</pre>",
            500
        )


    end_parquet = time.perf_counter()


    # --------------------------------------------------------
    # MAKE SURE DATA EXISTS
    # --------------------------------------------------------

    if clip_data.empty:

        return (
            f"""
            <h2>No tracking data found</h2>

            <p>Season: {season}</p>

            <p>Game ID: {game_id}</p>

            <p>Event ID: {event_id}</p>
            """,
            404
        )


    print(
        "Rows loaded:",
        len(clip_data)
    )


    # ========================================================
    # BUILD ANIMATION
    # ========================================================

    start_frames = time.perf_counter()


    # --------------------------------------------------------
    # SORT ONCE
    # --------------------------------------------------------

    clip_data = clip_data.sort_values(
        "frame_idx"
    )


    # --------------------------------------------------------
    # DETERMINE HOME / AWAY TEAM IDS
    # --------------------------------------------------------

    home_team_id = int(
        clip_data["home_team_id"]
        .dropna()
        .iloc[0]
    )

    away_team_id = int(
        clip_data["away_team_id"]
        .dropna()
        .iloc[0]
    )


    # --------------------------------------------------------
    # BUILD ALL FRAMES IN A SINGLE PASS
    # --------------------------------------------------------

    animation_frames = []

    current_frame_number = None
    current_frame = None


    columns_needed = [
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
    ]


    records = (
        clip_data[
            columns_needed
        ]
        .itertuples(
            index=False,
            name=None
        )
    )


    for (
        frame_idx,
        seconds_to_goal,
        entity_type,
        player_id,
        team_id,
        sweater_number,
        is_goalie,
        is_scorer,
        x,
        y,
    ) in records:


        # ----------------------------------------------------
        # START A NEW FRAME
        # ----------------------------------------------------

        frame_number = int(frame_idx)


        if frame_number != current_frame_number:

            current_frame_number = frame_number


            if pd.isna(seconds_to_goal):

                frame_seconds_to_goal = None

            else:

                frame_seconds_to_goal = float(
                    seconds_to_goal
                )


            current_frame = {

                "frame_idx":
                    frame_number,

                "seconds_to_goal":
                    frame_seconds_to_goal,

                "players":
                    [],

                "puck":
                    None

            }


            animation_frames.append(
                current_frame
            )


        # ----------------------------------------------------
        # GET SECONDS_TO_GOAL FROM ANOTHER ROW IF NECESSARY
        # ----------------------------------------------------

        elif (
            current_frame["seconds_to_goal"] is None
            and not pd.isna(seconds_to_goal)
        ):

            current_frame[
                "seconds_to_goal"
            ] = float(
                seconds_to_goal
            )


        # ----------------------------------------------------
        # PUCK
        # ----------------------------------------------------

        if entity_type == "puck":

            if (
                not pd.isna(x)
                and
                not pd.isna(y)
            ):

                current_frame["puck"] = {

                    "x":
                        float(x),

                    "y":
                        float(y)

                }

            continue


        # ----------------------------------------------------
        # PLAYER MUST HAVE REQUIRED VALUES
        # ----------------------------------------------------

        if (
            pd.isna(x)
            or
            pd.isna(y)
            or
            pd.isna(player_id)
            or
            pd.isna(team_id)
        ):

            continue


        player_id_int = int(
            player_id
        )

        team_id_int = int(
            team_id
        )


        # ----------------------------------------------------
        # HOME / AWAY
        # ----------------------------------------------------

        if team_id_int == home_team_id:

            team = "home"

        elif team_id_int == away_team_id:

            team = "away"

        else:

            continue


        # ----------------------------------------------------
        # JERSEY NUMBER
        # ----------------------------------------------------

        if pd.isna(sweater_number):

            sweater = ""

        else:

            sweater = str(
                int(
                    sweater_number
                )
            )


        # ----------------------------------------------------
        # PLAYER OBJECT
        # ----------------------------------------------------

        current_frame[
            "players"
        ].append(
            {
                "player_id":
                    player_id_int,

                "x":
                    float(x),

                "y":
                    float(y),

                "team":
                    team,

                "goalie":
                    bool(is_goalie),

                "scorer":
                    bool(is_scorer),

                "sweater":
                    sweater
            }
        )


    end_frames = time.perf_counter()


    # ========================================================
    # RINK IMAGE
    # ========================================================
    rink_url = RINK_DATA_URL
  
  
  
  


    # ========================================================
    # JSON SERIALIZATION
    # ========================================================

    start_json = time.perf_counter()

    frames_json = json.dumps(
        animation_frames
    )

    end_json = time.perf_counter()


    # ========================================================
    # RENDER TEMPLATE
    # ========================================================

    start_render = time.perf_counter()


    html = render_template(

        "clip.html",

        season=season,

        game_id=game_id,

        event_id=event_id,

        game_date=game_date,

        home_abbr=home_abbr,

        away_abbr=away_abbr,

        scorer=scorer,

        rink_url=rink_url,

        frames_json=frames_json

    )


    end_render = time.perf_counter()

    end_total = time.perf_counter()


    # ========================================================
    # PERFORMANCE REPORT
    # ========================================================

    print("")
    print("========================================")
    print("CLIP PERFORMANCE")
    print("========================================")

    print(
        f"R2 download:        "
        f"{end_r2 - start_r2:.3f} sec"
    )

    print(
        f"Parquet read:       "
        f"{end_parquet - start_parquet:.3f} sec"
    )

    print(
        f"Build animation:    "
        f"{end_frames - start_frames:.3f} sec"
    )

    print(
        f"JSON serialization: "
        f"{end_json - start_json:.3f} sec"
    )

    print(
        f"Render template:    "
        f"{end_render - start_render:.3f} sec"
    )

    print("----------------------------------------")

    print(
        f"TOTAL SERVER TIME:  "
        f"{end_total - start_total:.3f} sec"
    )

    print(
        "Frames:",
        len(animation_frames)
    )

    print(
        "Rows:",
        len(clip_data)
    )

    print(
        "JSON size:",
        f"{len(frames_json) / 1024:.1f} KB"
    )

    print("========================================")
    print("")


    return html


# ============================================================
# START FLASK
# ============================================================

if __name__ == "__main__":

    app.run(

        host="127.0.0.1",

        port=5001,

        debug=True,

        use_reloader=False

    )