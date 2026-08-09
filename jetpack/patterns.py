"""
Course geometry: the hand-authored zapper patterns, the coin formations, and the
two rules that keep them fair.

Both families follow the same shape -- a small builder function per variant plus a
tuple collecting them -- so adding a sixth coin shape or a fifth zapper pattern is
a function and one tuple entry, with no change to the spawn logic that consumes
them.

The two predicates here are the interesting part. `pattern_is_passable` enforces
the "every course is flyable" guarantee in code rather than trusting the authored
numbers, and `coin_is_clear` is deliberately one predicate used in both directions
so coins and beams can never disagree about whether they overlap.
"""
import math

from .config import *
from .entities import Coin, Zapper


def pattern_is_passable(zappers, lane_top, lane_h, step=8):
    """True if every column a pattern covers leaves an opening of at least ZAPPER_MIN_GAP_H.

    Sweeps the pattern's x-span in `step`-wide columns, merges the y-spans each zapper
    blocks in that column, and measures the largest remaining gap. `step` is well under
    the player's height, so no beam can slip between two samples.

    What this guarantees is per-column clearance, not full path connectivity -- proving
    a route exists would mean pathfinding against the player's climb rate. In practice
    the authored patterns are all traversable by shape; this is the backstop that catches
    an unlucky roll walling the lane off, which is the failure that actually matters.
    """
    if not zappers:
        return True
    x_start = int(min(min(z.x0, z.x1) for z in zappers)) - ZAPPER_NODE_SIZE
    x_end = int(max(max(z.x0, z.x1) for z in zappers)) + ZAPPER_NODE_SIZE
    for xa in range(x_start, x_end + 1, step):
        blocked = [r for r in (z.blocked_y_range(xa, xa + step) for z in zappers) if r]
        if not blocked:
            continue
        blocked.sort()
        merged = [list(blocked[0])]
        for lo, hi in blocked[1:]:
            if lo <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        widest = 0.0
        cursor = float(lane_top)
        for lo, hi in merged:
            widest = max(widest, lo - cursor)
            cursor = max(cursor, hi)
        widest = max(widest, lane_top + lane_h - cursor)
        if widest < ZAPPER_MIN_GAP_H:
            return False
    return True


# ---------------------------------------------------------------------------
# Zapper patterns. Each builder returns (list_of_zappers, pattern_width_px); Lane._spawn
# picks one at random per spawn event and advances next_spawn_x by the returned width,
# so the existing pacing logic keeps working unchanged. Every result is run through
# pattern_is_passable() before it's accepted.
# ---------------------------------------------------------------------------
def _pattern_thread(rng, lane_top, lane_h, x):
    """A vertical pair sharing one x, with a gap in the middle to thread."""
    # The two facing emitter nodes each eat ZAPPER_NODE_SIZE/2 of the opening, so the
    # authored figure has to clear the bar by a whole node before pattern_is_passable()
    # will accept it -- authoring it as a bare ZAPPER_MIN_GAP_H left only 64px free.
    gap = ZAPPER_NODE_SIZE + rng.uniform(ZAPPER_MIN_GAP_H * 1.05, ZAPPER_MIN_GAP_H * 1.5)
    top = lane_top + ZAPPER_EDGE_MARGIN
    bottom = lane_top + lane_h - ZAPPER_EDGE_MARGIN
    lo, hi = top + gap / 2, bottom - gap / 2
    cy = rng.uniform(lo, hi) if hi > lo else lane_top + lane_h / 2
    return [Zapper(x, top, x, cy - gap / 2),
            Zapper(x, cy + gap / 2, x, bottom)], 30.0


def _pattern_stagger(rng, lane_top, lane_h, x):
    """A floor-hugging horizontal, then a ceiling-hugging one further along -- forces an
    up-then-down weave rather than a single altitude change."""
    span1 = rng.uniform(110, 180)
    span2 = rng.uniform(110, 180)
    spacing = rng.uniform(90, 150)
    y_low = lane_top + lane_h - ZAPPER_EDGE_MARGIN - ZAPPER_NODE_SIZE / 2
    y_high = lane_top + ZAPPER_EDGE_MARGIN + ZAPPER_NODE_SIZE / 2
    x2 = x + span1 + spacing
    return [Zapper(x, y_low, x + span1, y_low),
            Zapper(x2, y_high, x2 + span2, y_high)], span1 + spacing + span2


def _pattern_staircase(rng, lane_top, lane_h, x):
    """Three diagonals stepping across the lane -- the case a bounding box would badly
    over-claim, and the reason collision is segment-based."""
    run = rng.uniform(70, 100)
    rise = rng.uniform(45, 70)
    descending = rng.random() < 0.5
    top = lane_top + ZAPPER_EDGE_MARGIN
    bottom = lane_top + lane_h - ZAPPER_EDGE_MARGIN
    y = lane_top + (lane_h * 0.25 if descending else lane_h * 0.75)
    zappers, cx = [], x
    for _ in range(3):
        y2 = max(top, min(bottom, y + rise if descending else y - rise))
        zappers.append(Zapper(cx, y, cx + run, y2))
        cx += run + rng.uniform(25, 55)
        y = y2
    return zappers, cx - x


def _pattern_corridor(rng, lane_top, lane_h, x):
    """Two long horizontals inset from the ceiling and floor, leaving a channel to fly
    down the middle."""
    span = rng.uniform(190, 280)
    inset = rng.uniform(60, 100)
    y_top = lane_top + inset
    y_bot = lane_top + lane_h - inset
    return [Zapper(x, y_top, x + span, y_top),
            Zapper(x, y_bot, x + span, y_bot)], span


ZAPPER_PATTERNS = (_pattern_thread, _pattern_stagger, _pattern_staircase, _pattern_corridor)


def _pattern_fallback(rng, lane_top, lane_h, x):
    """Emergency path only, if the random rolls somehow fail pattern_is_passable() every
    attempt: one short floor beam, which cannot wall the lane off by construction."""
    span = rng.uniform(90, 140)
    y = lane_top + lane_h - ZAPPER_EDGE_MARGIN - ZAPPER_NODE_SIZE / 2
    return [Zapper(x, y, x + span, y)], span


# ---------------------------------------------------------------------------
# Coin formations. Each returns a list of n vertical offsets (px, negative = up) to hang
# off a common baseline; Lane._spawn_coin_formation lays the coins at COIN_SPACING
# intervals along the offsets. `amp` is the shape's total vertical travel.
# ---------------------------------------------------------------------------
def _coin_line(rng, n, amp):
    """A flat horizontal run."""
    return [0.0] * n


def _coin_arc_up(rng, n, amp):
    """A hill -- rises to a peak in the middle and comes back down."""
    return [-amp * math.sin(math.pi * i / (n - 1)) for i in range(n)]


def _coin_arc_down(rng, n, amp):
    """A valley -- the mirror of the hill."""
    return [amp * math.sin(math.pi * i / (n - 1)) for i in range(n)]


def _coin_zigzag(rng, n, amp):
    """Triangle wave: climbs for `leg` coins, then reverses."""
    leg = 3
    step = amp / leg
    offsets, y, direction = [], 0.0, -1.0
    for i in range(n):
        offsets.append(y)
        y += step * direction
        if (i + 1) % leg == 0:
            direction = -direction
    return offsets


def _coin_staircase(rng, n, amp):
    """Monotonic steps, climbing or descending."""
    direction = -1.0 if rng.random() < 0.5 else 1.0
    step = amp / max(1, n - 1)
    return [direction * step * i for i in range(n)]


COIN_FORMATIONS = (_coin_line, _coin_arc_up, _coin_arc_down, _coin_zigzag, _coin_staircase)


def coin_is_clear(coin_rect, zappers, tokens=()):
    """Whether a coin has room where it sits.

    One rule used in both directions -- coins avoid the zappers already in the lane, and
    a zapper pattern spawning afterwards culls the coins it would land on. Checking only
    at coin-spawn time isn't enough: coin formations occupy x SCREEN_W+20..+326 while
    zapper patterns spawn from next_spawn_x (SCREEN_W+100 and up), so the two spawn
    regions overlap and whichever spawns second used to land on the other.

    Tested against the beam *segment*, not the zapper's bounding box, so coins can still
    sit in the open space beside a diagonal instead of avoiding the whole box.
    """
    padded = coin_rect.inflate(COIN_CLEARANCE * 2, COIN_CLEARANCE * 2)
    if any(z.collides(padded) for z in zappers):
        return False
    return not any(padded.colliderect(t.rect()) for t in tokens)
