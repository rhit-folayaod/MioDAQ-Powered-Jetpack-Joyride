"""
Picks the COURSE_SEEDS pool baked into jetpack/config.py: a set of ~20 vetted course
seeds spanning easy to hard, biased away from the punishing end.

Why a curated pool at all: a round used to draw `random.randrange(2**31)`, so the course
could land anywhere in the difficulty distribution -- including the tail that ends a demo
run in ten seconds. This scores a large sample of candidate seeds offline and keeps a
spread of them, so a round is still a random course, just never a random *disaster*.

How difficulty is measured. Each candidate is simulated with the game's own `Lane` class
at a fixed 60fps for SIM_SECONDS, and every frame the largest vertical opening across the
player's own column is measured with the same merge `pattern_is_passable` uses -- i.e.
literally the slot the player has to be in at that moment. Frames where that slot is
constrained are grouped into obstacle "events", and a seed is scored on:

    density    how many obstacle events the run contains
    tightness  the mean narrowest opening per event, and the 10th-percentile opening
    travel     the vertical speed needed to get from one event's opening to the next

Deliberately *not* scored: missiles, seekers and tokens. They were measured first and
barely move between seeds (8-10 missiles, 2-3 seekers, 3 tokens over a minute, near
enough every time) because their spawn timers are tightly clustered -- so including them
would add noise to the ranking rather than signal. Their *first-arrival times* are
reported per chosen seed for eyeballing, since those do vary by several seconds.

What this does not measure is whether a course is fun, or reachable by a specific player;
it's a proxy for how much precision the geometry demands. `pattern_is_passable` already
guarantees every course is passable at all, so this grades within that guarantee.

Run (from the repo root):
    python tools/pick_course_seeds.py
"""
import os
import random
import statistics
import sys
import time

# The game is a package at the repo root and this script lives one level down, so
# the root goes on sys.path before importing it -- letting the scorer run from
# anywhere rather than only from the root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")  # scoring never opens a window

import pygame

pygame.init()
pygame.display.set_mode((64, 64))

from jetpack import config as G
from jetpack.lane import Lane

# ---- TUNABLES -------------------------------------------------
CANDIDATES = 2000       # random seeds scored to build the difficulty distribution
POOL_SIZE = 20          # how many end up in COURSE_SEEDS
PCT_LO, PCT_HI = 2, 80  # percentile band of that distribution the pool spans.
                        # The top fifth is dropped outright -- that's the "some of them
                        # should be less difficult" ask -- while keeping a real easy->hard
                        # spread rather than making everything easy.
SIM_SECONDS = 60        # a strong run is ~50-60s at this scroll ramp, so this is about
                        # one full run's worth of course
SEARCH_SEED = 20260804  # fixed, so re-running reproduces the same pool
DT = 1 / 60
CONSTRAINED = 260       # opening (px) below which a frame counts as part of an obstacle
# Weights on the z-scored metrics. Tightness leads because a narrow slot is what actually
# kills; density next; required travel last, since at these magnitudes (~80-170 px/s
# against a 480 px/s climb rate) it costs attention rather than being anywhere near
# physically infeasible.
WEIGHTS = {"density": 1.0, "ev_gap": 1.2, "g10": 0.8, "sp90": 0.8, "spmean": 0.5}
# ---------------------------------------------------------------

COL_A = G.PLAYER_X - G.PLAYER_SIZE / 2
COL_B = G.PLAYER_X + G.PLAYER_SIZE / 2


def opening_at_player(lane):
    """(largest vertical opening, its centre) across the player's own column.

    The same span-merge `pattern_is_passable` runs, pinned to one column instead of swept
    across a pattern -- so it reports the gap the player is being asked to fit through
    right now rather than a property of the pattern in the abstract.
    """
    blocked = [r for r in (z.blocked_y_range(COL_A, COL_B) for z in lane.zappers) if r]
    if not blocked:
        return lane.lane_h, lane.lane_top + lane.lane_h / 2
    blocked.sort()
    merged = [list(blocked[0])]
    for lo, hi in blocked[1:]:
        if lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    best, best_c = 0.0, lane.lane_top + lane.lane_h / 2
    cursor = float(lane.lane_top)
    for lo, hi in merged:
        if lo - cursor > best:
            best, best_c = lo - cursor, (cursor + lo) / 2
        cursor = max(cursor, hi)
    if lane.lane_top + lane.lane_h - cursor > best:
        best = lane.lane_top + lane.lane_h - cursor
        best_c = (cursor + lane.lane_top + lane.lane_h) / 2
    return best, best_c


def measure(seed):
    """Simulates one course and returns its raw difficulty metrics."""
    lane = Lane(0, G.LANE_H)
    firsts = {}
    orig = (lane._launch_missile, lane._spawn_seeker, lane._spawn_token)
    clock = [0.0]

    def note(key, fn):
        def wrapper(*a):
            firsts.setdefault(key, round(clock[0], 1))
            return fn(*a)
        return wrapper

    lane._launch_missile = note("missile", orig[0])
    lane._spawn_seeker = note("seeker", orig[1])
    lane._spawn_token = note("token", orig[2])
    lane.reset(seed)

    gaps, events, run = [], [], []
    y = lane.lane_top + lane.lane_h / 2
    for i in range(int(SIM_SECONDS / DT)):
        clock[0] = i * DT
        lane.update(DT, True, y)
        gap, cy = opening_at_player(lane)
        y = cy                      # a notional player riding the middle of the slot
        gaps.append(gap)
        if gap < CONSTRAINED:
            run.append((clock[0], gap, cy))
        elif run:
            tightest = min(run, key=lambda r: r[1])
            events.append(tightest)
            run = []

    speeds = [abs(c2 - c1) / (t2 - t1)
              for (t1, _, c1), (t2, _, c2) in zip(events, events[1:]) if t2 > t1]
    gaps.sort()
    speeds_sorted = sorted(speeds)
    return {
        "seed": seed,
        "density": len(events),
        "ev_gap": statistics.mean([g for _, g, _ in events]) if events else G.LANE_H,
        "g10": gaps[len(gaps) // 10],
        "sp90": speeds_sorted[int(len(speeds_sorted) * 0.9)] if speeds_sorted else 0.0,
        "spmean": statistics.mean(speeds) if speeds else 0.0,
        "t_missile": firsts.get("missile", SIM_SECONDS),
        "t_seeker": firsts.get("seeker", SIM_SECONDS),
        "t_token": firsts.get("token", SIM_SECONDS),
    }


def score_all(rows):
    """Z-scores each metric across the sample and combines them into one difficulty number.

    Z-scored rather than hand-normalised because the metrics are on wildly different
    scales (a count, two pixel measures, two px/s measures); this puts them in the same
    units -- spread of this sample -- so WEIGHTS means what it looks like it means.
    """
    stats = {}
    for key in WEIGHTS:
        vals = [r[key] for r in rows]
        mean = statistics.mean(vals)
        sd = statistics.pstdev(vals) or 1.0
        stats[key] = (mean, sd)
    # ev_gap and g10 are *openings*: bigger is easier, so their contribution is negated.
    sign = {"density": 1.0, "ev_gap": -1.0, "g10": -1.0, "sp90": 1.0, "spmean": 1.0}
    for r in rows:
        r["score"] = sum(WEIGHTS[k] * sign[k] * ((r[k] - stats[k][0]) / stats[k][1])
                         for k in WEIGHTS)
    return stats


def main():
    rng = random.Random(SEARCH_SEED)
    seeds = [rng.randrange(2 ** 31) for _ in range(CANDIDATES)]
    t0 = time.time()
    rows = []
    for i, s in enumerate(seeds):
        rows.append(measure(s))
        if (i + 1) % 250 == 0:
            print(f"  scored {i + 1}/{CANDIDATES} "
                  f"({(time.time() - t0) / (i + 1) * 1000:.0f} ms/seed)", file=sys.stderr)
    score_all(rows)
    rows.sort(key=lambda r: r["score"])

    picks = []
    for i in range(POOL_SIZE):
        pct = PCT_LO + (PCT_HI - PCT_LO) * i / (POOL_SIZE - 1)
        picks.append(rows[min(len(rows) - 1, int(pct / 100 * len(rows)))])

    print(f"\nScored {CANDIDATES} seeds in {time.time() - t0:.0f}s. "
          f"Pool spans percentiles {PCT_LO}-{PCT_HI} of that distribution.\n")
    head = ("pct", "seed", "score", "events", "ev_gap", "g10", "sp90", "spmean",
            "1st msl", "1st skr", "1st tok")
    print("  ".join(f"{h:>9}" for h in head))
    for i, r in enumerate(picks):
        pct = PCT_LO + (PCT_HI - PCT_LO) * i / (POOL_SIZE - 1)
        print("  ".join(f"{v:>9}" for v in (
            f"{pct:.0f}", r["seed"], f"{r['score']:+.2f}", r["density"],
            f"{r['ev_gap']:.0f}", f"{r['g10']:.0f}", f"{r['sp90']:.0f}",
            f"{r['spmean']:.0f}", f"{r['t_missile']:.1f}", f"{r['t_seeker']:.1f}",
            f"{r['t_token']:.1f}")))

    all_scores = [r["score"] for r in rows]
    print(f"\nfull sample score range: {min(all_scores):+.2f} .. {max(all_scores):+.2f}, "
          f"median {statistics.median(all_scores):+.2f}")
    print(f"pool score range:        {picks[0]['score']:+.2f} .. {picks[-1]['score']:+.2f}, "
          f"median {statistics.median([p['score'] for p in picks]):+.2f}")

    print("\nCOURSE_SEEDS = [")
    for i in range(0, POOL_SIZE, 5):
        print("    " + " ".join(f"{r['seed']}," for r in picks[i:i + 5]))
    print("]")


if __name__ == "__main__":
    main()
