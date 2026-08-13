#!/usr/bin/env python3
"""
figures.py — Optional. Render the validation figures to figures/.

Nothing else in the pipeline reads or writes these, and no table depends on
them, so this is opt-in like the tracking build:

    pip install matplotlib
    python src/figures.py --root . --all

Each figure states its own conclusion in its subtitle and is drawn only from
tables already on disk — no figure recomputes a measurement, so a figure can
never quietly disagree with the number it illustrates. Where a figure needs a
statistic the validators already compute, it imports the validator's own loader
rather than re-deriving it (see `tracking_validation`).

WHAT EACH ONE NEEDS

    fig1_shot_validation      audit/completeness_{season}.parquet   [ships in the repo]
    fig2_tracking_validation  processed/tracking/ + processed/shots/
    fig3_goal_map             processed/tracking/
    fig4_puck_motion          processed/tracking/

fig1 is the one that matters most and costs least: the audit tables are
committed, so it renders on a bare clone with no archive and no build. The
other three need `--steps tracking` to have run.

Missing inputs are reported and skipped, never raised — a figure you cannot
draw yet should not stop the ones you can.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import logging

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import pipeline_common as pc
import tracking_validation as tv

SEASONS = ["20232024", "20242025", "20252026"]
OUT = Path("figures")

# --- palette ---------------------------------------------------------------
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
BLUE = "#2a78d6"
RED = "#e34948"
DEEMPH = "#c9c8c2"
SEQ_LO, SEQ_MID, SEQ_HI = "#86b6ef", "#2a78d6", "#104281"

logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)

mpl.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "font.family": ["Segoe UI", "DejaVu Sans"],
    "text.color": INK, "axes.labelcolor": INK_2, "axes.edgecolor": AXIS,
    "xtick.color": MUTED, "ytick.color": MUTED,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
    "grid.linestyle": "-", "axes.axisbelow": True,
    "font.size": 10, "figure.dpi": 140,
})


def _sub(ax, title, subtitle):
    """Panel title plus a subtitle that states the panel's conclusion.

    Both are anchored to the axes, not the figure, so that in a two-panel
    figure each column's text stays over its own panel. The subtitle's height
    is what sets the title's offset: a fixed pad collides as soon as the
    subtitle wraps to a third line.
    """
    n_lines = subtitle.count("\n") + 1
    ax.set_title(title, loc="left", fontsize=11.5, fontweight="600",
                 color=INK, pad=14 + 12.5 * n_lines)
    ax.text(0, 1.02, subtitle, transform=ax.transAxes, fontsize=8.8,
            color=INK_2, va="bottom", linespacing=1.5)


def _save(fig, name):
    OUT.mkdir(parents=True, exist_ok=True)
    for ext in ("png", "svg"):
        fig.savefig(OUT / f"{name}.{ext}", bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {OUT}/{name}.png / .svg")


def _ecdf(ax, values, color, label, annotate=True):
    """Draw an ECDF and return (median, p90)."""
    e = np.sort(np.asarray(values, dtype=float))
    frac = np.arange(1, len(e) + 1) / len(e)
    ax.plot(e, frac, color=color, lw=2.2, zorder=4, label=label)
    med, p90 = float(np.median(e)), float(np.percentile(e, 90))
    if annotate:
        ax.fill_between(e, 0, frac, color=color, alpha=0.08, zorder=1, lw=0)
        for v, lab, y in ((med, f"median {med:.1f} ft", 0.50),
                          (p90, f"90th pct {p90:.1f} ft", 0.90)):
            ax.plot([v, v], [0, y], color=INK_2, lw=1, ls=(0, (3, 2)), zorder=2)
            ax.plot([0, v], [y, y], color=INK_2, lw=1, ls=(0, (3, 2)), zorder=2)
            ax.plot(v, y, "o", ms=6, color=INK, zorder=5)
            ax.annotate(lab, xy=(v, y), xytext=(v + 4, y - 0.05),
                        fontsize=9.5, color=INK, fontweight="600")
    return med, p90


def _exists(paths, what):
    missing = [p for p in paths if not Path(p).exists()]
    if missing:
        print(f"  skipped — {what} needs {missing[0]}")
        return False
    return True


# ---------------------------------------------------------------------------
# 1. shot-frame detection vs the play-by-play          (audit tables only)
# ---------------------------------------------------------------------------

def fig_shot_validation(lay: pc.Layout, seasons):
    """Is the inferred shot release trustworthy?

    Panel A is the headline: an ECDF of the error against the PBP's own
    recorded shot location, which is one number verifiable at a glance. Panel B
    shows it holds across shot distance rather than only on average, which is
    the thing a skeptical reader asks next — a detector that worked on
    slot goals and failed at the point would have the same median.

    `shot_pbp_err_ft` is written per goal by build_processed, so this needs no
    sprite re-reading and no tracking table: just audit/, which is committed.
    """
    parts = []
    for s in seasons:
        p = lay.completeness(s)
        if not p.exists():
            continue
        au = pd.read_parquet(p)
        au = au[au.parsed.astype(bool) & au.shot_pbp_err_ft.notna()].copy()
        au["season"] = s
        parts.append(au[["season", "game_id", "event_id", "shot_pbp_err_ft",
                         "shot_net_dist_ft", "shot_confidence"]])
    if not parts:
        print("  skipped — no audit/completeness_*.parquet found")
        return
    df = pd.concat(parts, ignore_index=True)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12.6, 5.2),
                                   gridspec_kw={"width_ratios": [1, 1.05]})

    e = df.shot_pbp_err_ft.to_numpy(float)
    _ecdf(ax1, e, BLUE, None)
    ax1.set_xlim(0, 50)
    ax1.set_ylim(0, 1.02)
    ax1.set_xlabel("Distance from the inferred shot location to the PBP's (ft)")
    ax1.set_ylabel("Cumulative share of goals")
    _sub(ax1, "The inferred shot agrees with the NHL's own record",
         f"n={len(df):,} goals, {len(seasons)} season(s). "
         f"{(e <= 10).mean():.1%} land within 10 ft, {(e <= 20).mean():.1%} "
         "within 20 ft —\nan independent check, since neither location is "
         "used to find the other.")

    labs = ["0-10", "10-20", "20-30", "30-40", "40-50", "50-60", "60-70", "70+"]
    d = df.copy()
    d["b"] = pd.cut(d.shot_net_dist_ft, [0, 10, 20, 30, 40, 50, 60, 70, 200],
                    labels=labs)
    med = d.groupby("b", observed=True).shot_pbp_err_ft.median().reindex(labs)
    n = d.groupby("b", observed=True).size().reindex(labs)
    x = np.arange(len(labs))
    vals = med.to_numpy(float)
    ax2.bar(x, vals, width=0.6, color=SEQ_MID, zorder=3)
    for xi, v in zip(x, vals):
        if np.isfinite(v):
            ax2.text(xi, v + max(vals) * 0.03, f"{v:.1f}", ha="center",
                     va="bottom", fontsize=9.5, color=INK, fontweight="600")
    ax2.set_xticks(x)
    ax2.set_xticklabels([f"{b}\nn={0 if pd.isna(v) else int(v):,}"
                         for b, v in zip(labs, n)], fontsize=7.6)
    ax2.set_xlabel("Distance of the shot from the net (ft)")
    ax2.set_ylabel("Median error vs the PBP location (ft)")
    ax2.set_ylim(0, np.nanmax(vals) * 1.35)
    ax2.grid(axis="x", visible=False)
    _sub(ax2, "…at every shot distance",
         "A detector that worked near the net and failed at the point would\n"
         "show the same overall median as one that worked everywhere.")

    fig.subplots_adjust(wspace=0.28, top=0.80)
    _save(fig, "fig1_shot_validation")


# ---------------------------------------------------------------------------
# 2. the tracking table's own shooter, vs the play-by-play
# ---------------------------------------------------------------------------

def fig_tracking_validation(lay: pc.Layout, seasons):
    """Does the tracking table put the SHOOTER where the goal was scored from?

    The published check (fig 1) grades the puck. This grades the identified
    player, which is the stronger claim and the one the tracking table exists
    to support: the shooter and the puck are different points a median ~2.7 ft
    apart, so the two ECDFs landing on top of each other is the result. Drawn
    with tracking_validation's own loaders so the figure and check 3 cannot
    drift apart.
    """
    rows = []
    for s in seasons:
        if not (lay.tracking(s).exists() and lay.shots(s).exists()
                and lay.completeness(s).exists()):
            continue
        sf = tv.load_shot_frames(lay, s)
        pbp = tv.load_pbp_goals(lay, s)
        au = pd.read_parquet(lay.completeness(s))[
            ["game_id", "event_id", "shot_confidence"]]

        sc = sf[(sf.entity_type == "player")
                & sf.is_scorer.fillna(False).astype(bool)]
        pk = sf[sf.entity_type == "puck"][["game_id", "event_id", "x", "y"]]
        m = sc.merge(pbp, on=["game_id", "event_id"], how="inner")
        m = m.merge(pk.rename(columns={"x": "puck_x", "y": "puck_y"}),
                    on=["game_id", "event_id"], how="left")
        m = m.merge(au, on=["game_id", "event_id"], how="left")
        m = m[m.x_coord.notna() & m.y_coord.notna()].copy()

        px, py = m.x_coord.to_numpy(float), m.y_coord.to_numpy(float)
        m["err_scorer"] = np.hypot(tv._f(m.x) - px, tv._f(m.y) - py)
        m["err_puck"] = np.hypot(tv._f(m.puck_x) - px, tv._f(m.puck_y) - py)
        rows.append(m[["err_scorer", "err_puck", "shot_confidence"]])

    if not rows:
        print("  skipped — needs processed/tracking/ (run --steps tracking)")
        return
    m = pd.concat(rows, ignore_index=True).dropna(subset=["err_scorer"])

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12.6, 5.2),
                                   gridspec_kw={"width_ratios": [1, 1.05]})

    s_med, _ = _ecdf(ax1, m.err_scorer, BLUE, "The identified shooter")
    pk_ok = m.err_puck.notna()
    p_med, _ = _ecdf(ax1, m.err_puck[pk_ok], MUTED, "The puck (published baseline)",
                     annotate=False)
    ax1.set_xlim(0, 50)
    ax1.set_ylim(0, 1.02)
    ax1.set_xlabel("Distance to the PBP's recorded goal location (ft)")
    ax1.set_ylabel("Cumulative share of goals")
    ax1.legend(frameon=False, fontsize=9, loc="lower right")
    _sub(ax1, "The shooter is where the goal was scored from",
         f"n={len(m):,} goals. Shooter median {s_med:.2f} ft against "
         f"{p_med:.2f} ft for the puck at the same frame;\n"
         f"{(m.err_scorer <= 10).mean():.1%} within 10 ft against "
         f"{(m.err_puck[pk_ok] <= 10).mean():.1%}. Two points ~2.7 ft apart, "
         "landing equally often.")

    order = [c for c in ("high", "medium", "low")
             if (m.shot_confidence == c).any()]
    x = np.arange(len(order))
    meds = [m.err_scorer[m.shot_confidence == c].median() for c in order]
    ns = [int((m.shot_confidence == c).sum()) for c in order]
    within = [(m.err_scorer[m.shot_confidence == c] <= 10).mean()
              for c in order]
    ax2.bar(x, meds, width=0.55, color=[SEQ_HI, SEQ_MID, SEQ_LO][:len(order)],
            zorder=3)
    for xi, v, w in zip(x, meds, within):
        ax2.text(xi, v + max(meds) * 0.03, f"{v:.1f} ft\n{w:.0%} within 10",
                 ha="center", va="bottom", fontsize=9.5, color=INK,
                 fontweight="600")
    ax2.set_xticks(x)
    ax2.set_xticklabels([f"{c}\nn={n:,}" for c, n in zip(order, ns)],
                        fontsize=9.5, color=INK_2)
    ax2.set_xlabel("The audit's own shot_confidence grade")
    ax2.set_ylabel("Median shooter error vs the PBP (ft)")
    ax2.set_ylim(0, max(meds) * 1.45)
    ax2.grid(axis="x", visible=False)
    _sub(ax2, "…and it flags its own misses",
         "The audit grades each goal's detection before any of this is\n"
         "measured. The low-confidence goals are the ones that miss.")

    fig.subplots_adjust(wspace=0.28, top=0.80)
    _save(fig, "fig2_tracking_validation")


# ---------------------------------------------------------------------------
# 3. one goal, drawn
# ---------------------------------------------------------------------------

def _rink(ax, half=False):
    """Standard NHL rink in the pipeline's own coordinates: x +/-100, y +/-42.5."""
    LINE = "#b8b7ad"
    ax.add_patch(plt.Rectangle((-100, -42.5), 200, 85, fill=False,
                               edgecolor=AXIS, lw=1.4, zorder=2))
    ax.axvline(0, color=RED, lw=1.6, alpha=0.55, zorder=1)
    for x in (-25, 25):
        ax.axvline(x, color=BLUE, lw=1.6, alpha=0.45, zorder=1)
    for x in (-89, 89):                       # goal lines
        ax.plot([x, x], [-38, 38], color=RED, lw=1.1, alpha=0.55, zorder=1)
        ax.add_patch(plt.Rectangle((x - 2 if x > 0 else x, -3), 2, 6,
                                   fill=False, edgecolor=INK_2, lw=1.2,
                                   zorder=2))
    for cx in (-69, 69):                      # faceoff circles
        for cy in (-22, 22):
            ax.add_patch(plt.Circle((cx, cy), 15, fill=False,
                                    edgecolor=LINE, lw=1, zorder=1))
    ax.add_patch(plt.Circle((0, 0), 15, fill=False, edgecolor=LINE, lw=1,
                            zorder=1))
    ax.set_xlim(0 if half else -101, 101)
    ax.set_ylim(-43.5, 43.5)
    ax.set_aspect("equal")
    ax.grid(False)
    ax.set_xticks([])
    ax.set_yticks([])
    for sp in ax.spines.values():
        sp.set_visible(False)


def fig_goal_map(lay: pc.Layout, seasons, game_id=None, event_id=None):
    """One goal's whole clip, in the attack frame.

    The table's central claim drawn rather than asserted: the attacked net is
    always the one on the right, whichever end the goal was actually scored at
    and whichever team scored it. Trajectories run from the start of the clip
    to the goal instant; post-goal frames are dropped, because that is where
    the benches empty (SCHEMA.md).
    """
    season = next((s for s in seasons if lay.tracking(s).exists()), None)
    if season is None:
        print("  skipped — needs processed/tracking/ (run --steps tracking)")
        return

    if game_id is None:
        sf = pd.read_parquet(lay.tracking(season),
                             filters=[("is_shot_frame", "==", True),
                                      ("entity_type", "==", "puck")],
                             dtype_backend="pyarrow")
        sf = sf[sf.x_att.notna()]
        # a goal with a long clip and a shot from distance shows more motion
        sf = sf[sf.dist_to_net_ft.astype(float).between(25, 55)]
        if sf.empty:
            print("  skipped — no goal with an attack frame found")
            return
        r = sf.iloc[len(sf) // 2]
        game_id, event_id = int(r.game_id), int(r.event_id)

    clip = pd.read_parquet(lay.tracking(season), dtype_backend="pyarrow",
                           filters=[("game_id", "==", int(game_id)),
                                    ("event_id", "==", int(event_id))])
    if clip.empty:
        print(f"  skipped — no rows for game {game_id} event {event_id}")
        return
    clip = clip[clip.frame_idx <= clip.goal_frame_idx.max()]

    fig, ax = plt.subplots(figsize=(11.4, 5.6))
    _rink(ax)

    puck = clip[clip.entity_type == "puck"].sort_values("frame_idx")
    players = clip[clip.entity_type == "player"]
    for _, g in players.groupby("player_id"):
        g = g.sort_values("frame_idx")
        scoring = bool(g.is_scoring_team.iloc[0])
        color = BLUE if scoring else RED
        ax.plot(g.x_att.astype(float), g.y_att.astype(float), color=color,
                lw=1.1, alpha=0.30, zorder=3)
        ax.plot(g.x_att.astype(float).iloc[-1], g.y_att.astype(float).iloc[-1],
                "o", ms=7, color=color, alpha=0.85, zorder=5,
                markeredgecolor=SURFACE, markeredgewidth=1.2)

    ax.plot(puck.x_att.astype(float), puck.y_att.astype(float), color=INK,
            lw=2.0, zorder=6, solid_capstyle="round")

    shot = clip[clip.is_shot_frame & (clip.entity_type == "puck")]
    if not shot.empty:
        ax.plot(float(shot.x_att.iloc[0]), float(shot.y_att.iloc[0]), "o",
                ms=11, color=INK, zorder=7, markeredgecolor=SURFACE,
                markeredgewidth=1.8)
        ax.annotate("release", xy=(float(shot.x_att.iloc[0]),
                                   float(shot.y_att.iloc[0])),
                    xytext=(-26, -24), textcoords="offset points",
                    fontsize=9.5, color=INK, fontweight="600",
                    arrowprops=dict(arrowstyle="->", color=INK_2, lw=1.1))

    scorer = clip[clip.is_scorer.fillna(False).astype(bool)
                  & clip.is_shot_frame]
    if not scorer.empty:
        ax.plot(float(scorer.x_att.iloc[0]), float(scorer.y_att.iloc[0]), "*",
                ms=19, color=BLUE, zorder=8, markeredgecolor=SURFACE,
                markeredgewidth=1.2)

    r0 = clip.iloc[0]
    secs = float(clip.frame_idx.max() - clip.frame_idx.min()) * pc.SECONDS_PER_FRAME
    _sub(ax, "One goal, every 0.1 s, in the attack frame",
         f"Game {int(r0.game_id)}, event {int(r0.event_id)} — "
         f"{r0.shot_type or 'shot'}, period {int(r0.period)} "
         f"{r0.time_in_period}, {secs:.1f} s of clip to the goal instant. "
         "Blue is the scoring team,\nred the conceding team, black the puck. "
         "The attacked net is on the right BY CONSTRUCTION — coordinates are "
         "rotated\n180° where needed, so every goal in the table stacks the "
         "same way round.")
    _save(fig, "fig3_goal_map")


# ---------------------------------------------------------------------------
# 4. how the goal instant is found
# ---------------------------------------------------------------------------

def fig_puck_motion(lay: pc.Layout, seasons):
    """Why the last frame is not the goal.

    The detector's whole premise is that a puck in the net stops moving while
    the clip keeps recording. That is a claim about a distribution, and it is
    the kind of claim a histogram either supports or destroys — two modes with
    a wide gap, or one smear.
    """
    season = next((s for s in seasons if lay.tracking(s).exists()), None)
    if season is None:
        print("  skipped — needs processed/tracking/ (run --steps tracking)")
        return

    pk = pd.read_parquet(lay.tracking(season), dtype_backend="pyarrow",
                         filters=[("entity_type", "==", "puck")],
                         columns=["game_id", "event_id", "frame_idx", "x", "y",
                                  "seconds_to_goal"])
    pk = pk.sort_values(["game_id", "event_id", "frame_idx"])
    x = pk.x.astype(float)
    y = pk.y.astype(float)
    same = (pk.game_id == pk.game_id.shift(1)) & (pk.event_id == pk.event_id.shift(1))
    step = np.hypot(x.diff(), y.diff()).where(same)
    s2g = pk.seconds_to_goal.astype(float)

    live = step[(s2g < 0) & step.notna()].to_numpy()
    dead = step[(s2g > 0) & step.notna()].to_numpy()
    if len(live) == 0 or len(dead) == 0:
        print("  skipped — no live/dead frames to compare")
        return

    fig, ax = plt.subplots(figsize=(9.6, 5.3))
    bins = np.linspace(0, 8, 90)
    ax.hist(live, bins=bins, color=BLUE, alpha=0.75, zorder=3,
            label=f"Before the goal instant  (n={len(live):,})")
    ax.hist(dead, bins=bins, color=RED, alpha=0.65, zorder=4,
            label=f"After it  (n={len(dead):,})")
    for v, c in ((np.median(live), BLUE), (np.median(dead), RED)):
        ax.axvline(v, color=c, lw=1.4, ls=(0, (4, 3)), zorder=5)
    ax.annotate(f"median {np.median(dead):.2f} ft",
                xy=(np.median(dead), 0), xytext=(1.1, 0.62),
                textcoords="axes fraction", fontsize=9.5, color=RED,
                fontweight="600", ha="left")
    ax.text(np.median(live) + 0.25, 0.80, f"median {np.median(live):.2f} ft",
            transform=ax.get_xaxis_transform(), fontsize=9.5, color=BLUE,
            fontweight="600")
    ax.set_xlim(0, 8)
    ax.set_xlabel("How far the puck moved between consecutive frames (ft per 0.1 s)")
    ax.set_ylabel("Frames")
    ax.grid(axis="x", visible=False)
    ax.legend(frameon=False, fontsize=9, loc="upper right")
    ax.set_title("The clip keeps recording after the puck is in the net",
                 loc="left", fontsize=13, fontweight="600", color=INK, pad=32)
    ax.text(0, 1.04,
            f"{season}, every puck frame. The separation is what locates the "
            "goal instant: recording continues while the\npuck sits in the net "
            "and the scorer skates in to celebrate, so the last frame of a clip "
            "is not the goal.",
            transform=ax.transAxes, fontsize=9, color=INK_2, va="bottom",
            linespacing=1.45)
    _save(fig, "fig4_puck_motion")


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Render validation figures to figures/ (optional).")
    ap.add_argument("--root", default=".")
    ap.add_argument("--seasons", nargs="*", default=SEASONS)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--only", nargs="*", default=None,
                    help="subset of: shot_validation tracking_validation "
                         "goal_map puck_motion")
    ap.add_argument("--game-id", type=int, default=None,
                    help="pin fig3 to one goal (with --event-id)")
    ap.add_argument("--event-id", type=int, default=None)
    ap.add_argument("--out", default="figures", help="output directory")
    args = ap.parse_args()

    global OUT
    OUT = Path(args.out)
    lay = pc.Layout(args.root)

    steps = {
        "shot_validation": lambda: fig_shot_validation(lay, args.seasons),
        "tracking_validation": lambda: fig_tracking_validation(lay, args.seasons),
        "goal_map": lambda: fig_goal_map(lay, args.seasons, args.game_id,
                                         args.event_id),
        "puck_motion": lambda: fig_puck_motion(lay, args.seasons),
    }
    if not args.all and not args.only:
        ap.error("pass --all, or --only with one or more of: "
                 + " ".join(steps))

    for name, fn in steps.items():
        if args.all or (args.only and name in args.only):
            print(f"\n=== {name} ===", flush=True)
            fn()


if __name__ == "__main__":
    main()
