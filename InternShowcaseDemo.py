"""
Two-Player Jetpack Joyride-style Demo
======================================
Dependencies:
    pip install pygame nidaqmx
    (nidaqmx is optional at runtime -- the game auto-falls-back to keyboard-only
     mode if the package or the DAQ device isn't available)

Hardware (optional -- NI USB DAQ, e.g. an NI myDAQ):
    Update DEVICE below to match the name in NI MAX.
    Buttons -> port0/line0:1 (DI)   LEDs -> port0/line6:7 (DO)
    (this is the line mapping from the originally-tested working script;
     if your physical wiring differs, update BUTTON_LINES / LED_LINES below)

Controls:
    Name entry: type on the keyboard, TAB to switch player field, ENTER to start.
    Player 1: Button 1  (falls back to SPACE only when no DAQ is connected)
    Player 2: Button 2  (falls back to UP ARROW only when no DAQ is connected)
    R (or both buttons together): once both players are down, returns to name entry to play again
    ESC: quit

Run:
    python InternShowcaseDemo.py
"""

import json
import math
import os
import random
import sys
import threading
import time

import pygame

try:
    import nidaqmx
    from nidaqmx.constants import LineGrouping
    NIDAQMX_AVAILABLE = True
except ImportError:
    NIDAQMX_AVAILABLE = False

# Sprite art: cropped from the "Dan the Man" asset packs (Playable Characters, Stage
# Hazards, Jetpack Joyride Event Cutscenes, Coin Counter, Charred Death Animation) plus
# a standalone coin icon (coin_ni_64.png). Loaded and scaled once in main(). Seeking
# missiles reuse a tinted duplicate of the straight-missile sprite (assets/missile_seeker.png,
# see draw_seeking_missile) rather than a primitive shape. The ground-running scientists
# (assets/manager_run_00..07.png + manager_down_00..03.png) are built separately by
# rebuild_manager_sprites.py, which cuts frames out of a scientist sprite sheet and
# composites a photo head onto each one.
ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

# Persistent (across runs of the program) leaderboard -- a simple JSON file next to
# the script, so it survives restarts but needs no external DB for a demo.
SCORES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "high_scores.json")
MAX_HIGH_SCORES = 5
MAX_NAME_LEN = 14


# ---------------------------------------------------------------------------
# DAQ I/O -- polls buttons / drives LEDs on a background thread so a slow or
# hiccuping DAQ read can never stall the pygame frame rate.
# ---------------------------------------------------------------------------
DEVICE = "Dev1"  # <-- update to match your device name in NI MAX
BUTTON_LINES = f"{DEVICE}/port0/line0:1"  # -> [Button 1, Button 2]
LED_LINES = f"{DEVICE}/port0/line6:7"     # -> [LED 1, LED 2]
DAQ_POLL_INTERVAL = 0.01  # seconds between DAQ reads/writes on the bg thread
DAQ_DEBUG_PRINT = True    # print raw DI reads to the terminal so wiring/polarity is easy to verify


class DaqIO:
    """Wraps the NI DAQ buttons/LEDs. Falls back to a keyboard-only no-op if unavailable."""

    def __init__(self):
        self.available = False
        self._di_task = None
        self._do_task = None
        self._buttons = [False, False]
        self._leds = [False, False]
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None

        if not NIDAQMX_AVAILABLE:
            print("nidaqmx not installed -- running in keyboard-only mode.")
            return

        try:
            self._di_task = nidaqmx.Task()
            self._do_task = nidaqmx.Task()
            self._di_task.di_channels.add_di_chan(
                BUTTON_LINES, line_grouping=LineGrouping.CHAN_PER_LINE)
            self._do_task.do_channels.add_do_chan(
                LED_LINES, line_grouping=LineGrouping.CHAN_PER_LINE)
            self.available = True
            self._thread = threading.Thread(target=self._poll_loop, daemon=True)
            self._thread.start()
            print(f"NI DAQ '{DEVICE}' connected -- buttons and LEDs are live.")
        except Exception as exc:
            print(f"NI DAQ not available ({exc}) -- running in keyboard-only mode.")
            self.available = False
            self._di_task = None
            self._do_task = None

    def _poll_loop(self):
        last_debug_print = 0.0
        last_printed = None
        while not self._stop_event.is_set():
            try:
                button1, button2 = self._di_task.read()
                with self._lock:
                    self._buttons = [button1, button2]
                    leds_to_write = list(self._leds)
                # LED wiring is physically reversed from DO line order (line6 -> LED2,
                # line7 -> LED1), confirmed by pressing each button on real hardware.
                self._do_task.write([leds_to_write[1], leds_to_write[0]])
            except Exception as exc:
                print(f"DAQ polling stopped due to error: {exc}")
                self.available = False
                return

            if DAQ_DEBUG_PRINT:
                now = time.monotonic()
                state = (button1, button2)
                # Print on every state change, plus a heartbeat every 2s so it's
                # obvious the poll loop is alive even if nothing is pressed.
                if state != last_printed or now - last_debug_print > 2.0:
                    print(f"[DAQ] button1={button1}  button2={button2}")
                    last_debug_print = now
                    last_printed = state

            time.sleep(DAQ_POLL_INTERVAL)

    def get_buttons(self):
        """Returns (button1_pressed, button2_pressed); (False, False) if no DAQ."""
        if not self.available:
            return False, False
        with self._lock:
            return tuple(self._buttons)

    def set_leds(self, led1_on, led2_on):
        if not self.available:
            return
        with self._lock:
            self._leds = [led1_on, led2_on]

    def close(self):
        if not self.available:
            return
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        try:
            self._do_task.write([False, False])  # leave both LEDs off on exit
        except Exception:
            pass
        self._di_task.close()
        self._do_task.close()


# ---------------------------------------------------------------------------
# Game config
# ---------------------------------------------------------------------------
SCREEN_W, SCREEN_H = 1100, 640
FPS = 60
LANE_H = SCREEN_H // 2

# Display modes, cycled with F11. The game always renders to a fixed SCREEN_W x SCREEN_H
# canvas and that canvas is scaled to whatever the window happens to be as the very last
# step (see present()), so every mode is just a different window size -- no game logic,
# collision box, spawn coordinate or sprite scale is resolution-aware, and none of them
# had to change to support this.
DISPLAY_MODES = ("windowed", "borderless", "fullscreen")
DISPLAY_MODE_LABELS = {"windowed": "Windowed", "borderless": "Windowed Fullscreen",
                        "fullscreen": "Fullscreen"}

GRAVITY = 1500.0       # px/s^2, pulls player down
THRUST_ACCEL = 2400.0  # px/s^2, applied upward while thrusting (base, before boost/fuel)
MAX_FALL_SPEED = 640.0
MAX_RISE_SPEED = 480.0  # capped lower than fall speed so a boosted press can't rocket you into the ceiling

PLAYER_X = 150
PLAYER_SIZE = 32  # collision box; the sprite is drawn a touch larger than this, centered on it
PLAYER_SPRITE_H = 36  # rendered sprite height in px (width follows the source aspect ratio)

RUN_FRAME_COUNT = 10     # assets/run_00.png .. run_09.png -- played while not thrusting
RUN_FPS = 10.0
JETPACK_FRAME_COUNT = 5  # assets/jetpack_00.png .. jetpack_04.png -- played while thrusting
JETPACK_FPS = 9.0        # ping-ponged back and forth for a hover "wobble" instead of looping

# Death animation: assets/death_00.png .. death_03.png (Dan the Man "Charred Death"
# sheet, row 1). Plays once from the moment a player dies and freezes on the last
# (most-charred) frame, replacing the old flat dim-overlay effect.
DEATH_FRAME_COUNT = 4
DEATH_FPS = 9.0
DEATH_SPRITE_H = 46  # slightly larger than PLAYER_SPRITE_H -- the flame silhouette reads better bigger

# Profit Bird vehicle: a single static sprite swap (no run/jetpack animation frames
# exist for it), given a gentle hover bob at draw time so it doesn't look frozen.
PROFIT_BIRD_SPRITE_H = 54

# Lil' Stomper vehicle: assets/lilstomper_00..05.png (a 6-frame walk/stomp cycle),
# looped the same way the run cycle loops.
STOMPER_FRAME_COUNT = 6
STOMPER_FPS = 7.0
STOMPER_SPRITE_H = 54

# Lil' Stomper control scheme: a tap is an instant "long jump" impulse (up to the same
# MAX_RISE_SPEED ceiling normal flight caps at); holding past that initial jump engages
# the jetpack for a brief, limited float from its own small tank (independent of the
# base jetpack's FUEL_MAX) instead of flying indefinitely.
STOMPER_FLOAT_ACCEL = 950.0       # px/s^2 upward while floating -- weaker than THRUST_ACCEL, a hover assist
STOMPER_FLOAT_FUEL_MAX = 1.1      # seconds of float assist per jump/ground-charge
STOMPER_FLOAT_REGEN_RATE = STOMPER_FLOAT_FUEL_MAX / 1.0  # only regenerates while grounded

# Profit Bird control scheme: Flappy-Bird-style -- each fresh press is a discrete
# upward hop (velocity set outright, not additive, same as the genre), no hold-to-fly
# and no fuel; gravity does the rest between hops.
PROFIT_BIRD_FLAP_SPEED = 420.0  # px/s upward impulse applied on each fresh press

# Fuel system: holding thrust continuously drains the tank; running dry cuts thrust
# off (you fall) until it's refilled by letting go. Matches the "~3s of hold" ask.
FUEL_MAX = 3.0                    # seconds of continuous thrust before empty
FUEL_REGEN_RATE = FUEL_MAX / 1.5  # refills faster than it drains while button is released

# Each fresh press (release -> press edge) gets a thrust multiplier from this list,
# indexed by how many presses deep into the current "streak" you are (clamped to the
# last entry beyond that). The streak resets once fuel fully refills, i.e. once you've
# rested long enough. This rewards a slight first kick and punishes rapid re-tapping,
# without being so strong that the first press alone rockets you into a zapper.
PRESS_BOOST_MULTIPLIERS = [1.2, 1.08, 1.0]

SCROLL_SPEED0 = 260.0  # px/s starting scroll speed
SCROLL_ACCEL = 4.0     # px/s^2, the lane speeds up over the run

# Zappers: the staple hazard -- an electric beam strung between two emitter nodes,
# replacing the old single-sprite-stretched-to-a-random-box obstacle. Defined by two
# endpoints rather than a w/h box, so one class covers vertical, horizontal and diagonal
# beams at any length, and they spawn in hand-authored *patterns* (ZAPPER_PATTERNS)
# rather than one at a time -- a field to weave through instead of a row of lone blocks.
ZAPPER_NODE_FRAME_COUNT = 4    # assets/zapper_node_00..03.png
ZAPPER_NODE_FPS = 8.0
ZAPPER_NODE_SIZE = 20          # rendered emitter size (source art is ~20x20)
ZAPPER_ARC_FRAME_COUNT = 8     # assets/zapper_arc_g2_00..07.png -- the tileable beam segment
ZAPPER_ARC_FPS = 14.0          # fast, so beams crackle instead of sitting static
ZAPPER_ARC_H = 16              # rendered beam thickness
ZAPPER_EDGE_MARGIN = 6         # keeps emitter nodes just inside the lane edges
# Space left *after* a pattern before the next one begins. Wider than the old
# per-obstacle gap because a pattern is 150-320px across on its own, so without this the
# patterns would overlap into an unreadable wall.
ZAPPER_MIN_GAP, ZAPPER_MAX_GAP = 190, 320
# Every spawned pattern is verified to leave a vertical opening at least this tall at
# every x it covers -- enforced in code by pattern_is_passable(), not merely trusted to
# the authored numbers.
ZAPPER_MIN_GAP_H = PLAYER_SIZE * 2.5

# Missiles launch from off-screen right and fly straight left (same direction as the
# lane scroll), ignoring the player's position entirely (no tracking, that's a
# future feature). They never collide with zappers -- only with players. No missiles
# for the first 10-15s of a run, and spawn frequency ramps up as scroll_speed climbs.
MISSILE_W, MISSILE_H = 50, 20  # matches the aspect ratio of the missile sprite art
# Added on top of the lane's current scroll_speed (not absolute) so missiles always
# read as clearly faster than the scrolling zappers, no matter how far into a run.
MISSILE_SPEED_MIN, MISSILE_SPEED_MAX = 240.0, 320.0        # px/s, extra over scroll_speed
MISSILE_SPAWN_MIN, MISSILE_SPAWN_MAX = 2.5, 7.0            # seconds between spawns, per lane (fast/slow difficulty)
MISSILE_START_DELAY_MIN, MISSILE_START_DELAY_MAX = 10.0, 15.0  # seconds before the first missile of a run
MISSILE_DIFFICULTY_RANGE = 400.0  # scroll_speed increase (px/s) over which spawn gap ramps from MAX to MIN
# Every straight missile is now preceded by an exclamation-point telegraph pinned to the
# right edge of the lane at the y it will fire from -- the original never fires one
# unannounced. The missile object doesn't exist at all until the warning elapses, so
# there is nothing to collide with while it shows.
MISSILE_WARN_TIME = 1.2           # seconds the telegraph shows before the missile fires
MISSILE_WARN_W, MISSILE_WARN_H = 26, 30
MISSILE_WARN_BLINK_HZ = 6.0       # on/off cycles per second

# Seeking missiles: a rarer, later-arriving hazard on its own per-lane timer, independent
# of the straight-line Missile timer above so both types can appear in the same run.
# Reuses a tinted duplicate of the straight-missile sprite (see draw_seeking_missile).
SEEKER_W, SEEKER_H = 46, 22
# Spawns visible at the right edge (not deep off-screen like straight missiles) so the
# blink telegraph below is actually visible as a warning before it launches.
SEEKER_SPEED_MIN, SEEKER_SPEED_MAX = 260.0, 340.0             # px/s, extra over scroll_speed once launched
SEEKER_SPAWN_MIN, SEEKER_SPAWN_MAX = 9.0, 18.0                 # seconds between seekers -- rarer than straight missiles
SEEKER_START_DELAY_MIN, SEEKER_START_DELAY_MAX = 22.0, 30.0    # later than straight missiles' 10-15s
SEEKER_DIFFICULTY_RANGE = 550.0
SEEKER_BLINK_COUNT = 3       # full on/off blinks before launch
SEEKER_BLINK_INTERVAL = 0.27  # seconds per blink half-cycle (on or off)
SEEKER_MAX_VY = 220.0         # px/s cap on homing vertical speed -- kept well under player fall/rise
                               # speed so a last-second dodge can always outrun the turn
SEEKER_TURN_RATE = 900.0      # px/s^2 max rate vy can change -- gives it a turn radius instead of a snap
SEEKER_STEER_GAIN = 4.0       # converts y-distance-to-player into desired vy (clamped to SEEKER_MAX_VY)

# Vehicle power-ups (Profit Bird / Lil' Stomper, simplified): a token grants temporary
# invulnerability; a hit while active knocks the player out of the vehicle instead of
# killing them. In-lane pickup uses assets/vehicle_token.png (tinted per kind) for both
# vehicle kinds -- only Profit Bird has a dedicated in-vehicle player sprite so far
# (assets/profit_bird.png); Lil' Stomper still uses a tinted recolor of the base sprite.
TOKEN_W, TOKEN_H = 30, 30
TOKEN_SPAWN_MIN, TOKEN_SPAWN_MAX = 13.0, 20.0
VEHICLE_DURATION = 6.5         # seconds of invulnerability once picked up
VEHICLE_KINDS = ("profit_bird", "lil_stomper")
VEHICLE_COLORS = {"profit_bird": (255, 205, 60), "lil_stomper": (150, 205, 255)}
VEHICLE_LABELS = {"profit_bird": "PROFIT BIRD", "lil_stomper": "LIL' STOMPER"}
PROFIT_BIRD_DISTANCE_MULT = 1.5    # brief "speed boost" -- distance accrues faster while active
STOMPER_CEILING_FRAC = 0.55        # ground-hugging cap: can't climb above this fraction of the lane
HIT_GRACE_PERIOD = 0.5             # seconds of invulnerability right after a vehicle absorbs a hit,
                                    # so losing the vehicle can't immediately chain into a fatal hit
                                    # from the same hazard on the very next frame

# Picking up a vehicle token detonates every zapper/missile/seeker currently in that
# lane (Lane.detonate_hazards()) -- doesn't pause or end the run, just clears the
# immediate area so the reward isn't immediately undone by whatever's already in flight.
EXPLOSION_DURATION = 0.45  # seconds an individual blast VFX plays for
EXPLOSION_MAX_RADIUS = 34

# Coins: small, frequent, non-hazard collectibles -- track a running count per player,
# separate from the distance score. Uses assets/coin_ni_64.png.
COIN_W, COIN_H = 20, 20
COIN_SPACING = 34            # px between coin centres along a formation
COIN_MIN_COUNT, COIN_MAX_COUNT = 5, 10
COIN_ARC_HEIGHT_MIN, COIN_ARC_HEIGHT_MAX = 60, 130  # vertical travel of the curved shapes
COIN_FORMATION_TRIES = 6     # vertical offsets attempted before giving up on a spawn
# Clear space kept between a coin and a beam. Half the beam's drawn thickness (collision
# treats a beam as a zero-width segment) plus a visible margin on top.
COIN_CLEARANCE = ZAPPER_ARC_H // 2 + 7
# Per *formation* now, not per coin -- a formation is 5-10 coins and up to ~300px long,
# so the old 1.1-2.3s single-coin cadence would have run them into each other.
COIN_SPAWN_MIN, COIN_SPAWN_MAX = 3.2, 5.6

# Scientists: ground-running bystanders and a *reward*, not a hazard -- running into one
# knocks it over for bonus coins and never damages the player. Deliberately kept out of
# Lane.hazards() (which feeds the kill logic) and collision-checked on their own path.
SCIENTIST_SPRITE_H = 46   # rendered sprite height; the source frames carry transparent
                           # padding, so the *visible* scientist lands near the player's size
SCIENTIST_W, SCIENTIST_H = 22, 34  # collision box, tuned to the visible body rather than the
                                    # padded canvas -- generous, since catching one is the reward
# The source frames leave roughly a fifth of the canvas as empty space beneath the feet;
# the sprite is nudged down by that much at draw time so a ground-anchored scientist
# actually stands on the lane floor instead of hovering above it (see draw_scientist).
SCIENTIST_FOOT_PAD = round(SCIENTIST_SPRITE_H * 0.20)
SCIENTIST_RUN_FRAME_COUNT = 8   # assets/manager_run_00.png .. manager_run_07.png, loops
SCIENTIST_RUN_FPS = 10.0
SCIENTIST_DOWN_FRAME_COUNT = 4  # assets/manager_down_00.png .. manager_down_03.png, plays once
SCIENTIST_DOWN_FPS = 9.0
# Fraction of the lane's scroll_speed a *running* scientist moves at. Under 1.0, so it
# travels the same direction as the scroll but slower than the scenery -- i.e. in world
# terms it's fleeing the same way the player flies, just not fast enough, which is what
# makes it drift back into the player. Once knocked over it stops running and rides the
# lane at the full scroll speed like any other scenery.
SCIENTIST_SPEED_FRAC = 0.75
SCIENTIST_SPAWN_MIN, SCIENTIST_SPAWN_MAX = 3.5, 7.5  # seconds between spawns, per lane
SCIENTIST_KNOCKED_HOLD = 0.7   # seconds the final knocked frame holds before despawning
SCIENTIST_BONUS_COINS = 3      # counted as coins so it feeds the existing coin leaderboard,
                                # not the distance score

# Floating "+N" feedback popped on a scientist knockdown -- purely visual, lane-owned so
# it scrolls along with everything else (same as Explosion).
POPUP_DURATION = 0.7
POPUP_RISE = 26        # px the text floats upward over its lifetime
POPUP_COLOR = (255, 220, 90)

WHITE = (240, 240, 240)
BLACK = (15, 15, 15)
LANE_BG = [(35, 40, 55), (30, 34, 48)]  # translucent tint over assets/background.png, not an opaque fill
PLAYER_COLORS = [(80, 200, 255), (255, 150, 80)]
THRUST_COLOR = (255, 220, 90)
FUEL_BAR_COLOR = (90, 220, 140)
FUEL_BAR_EMPTY_COLOR = (70, 70, 80)


class Player:
    def __init__(self, index, lane_top, lane_h):
        self.index = index
        self.lane_top = lane_top
        self.lane_h = lane_h
        self.color = PLAYER_COLORS[index]
        self.reset()

    def reset(self):
        self.y = self.lane_top + self.lane_h / 2
        self.vy = 0.0
        self.alive = True
        self.thrusting = False
        self.distance = 0.0
        self.fuel = FUEL_MAX
        self.press_streak = 0
        self._was_held = False
        self.coins = 0
        self.vehicle_active = False
        self.vehicle_kind = None
        self.vehicle_timer = 0.0
        self.hit_grace = 0.0
        self.death_anim_t = 0.0
        self.on_ground = False
        self.stomper_float_fuel = STOMPER_FLOAT_FUEL_MAX

    def rect(self):
        return pygame.Rect(PLAYER_X - PLAYER_SIZE // 2, int(self.y - PLAYER_SIZE // 2),
                            PLAYER_SIZE, PLAYER_SIZE)

    def update(self, dt, thrust_held, scroll_speed):
        if not self.alive:
            self.death_anim_t += dt  # keeps ticking so draw_player can play the death animation once
            return

        if self.vehicle_active:
            self.vehicle_timer -= dt
            if self.vehicle_timer <= 0.0:
                self.vehicle_active = False
                self.vehicle_kind = None
                self.vehicle_timer = 0.0
        if self.hit_grace > 0.0:
            self.hit_grace = max(0.0, self.hit_grace - dt)

        just_pressed = thrust_held and not self._was_held
        self._was_held = thrust_held
        is_stomper = self.vehicle_active and self.vehicle_kind == "lil_stomper"
        is_profit_bird = self.vehicle_active and self.vehicle_kind == "profit_bird"

        if is_stomper:
            # Tap = an instant "long jump" impulse (capped by the same MAX_RISE_SPEED
            # clamp below, same ceiling normal flight uses); holding past that initial
            # jump engages the jetpack for a brief, limited float instead of flying
            # forever -- its own small tank, separate from the base jetpack fuel.
            if just_pressed:
                self.vy = -MAX_RISE_SPEED
                self.stomper_float_fuel = STOMPER_FLOAT_FUEL_MAX
            floating = thrust_held and not self.on_ground and self.stomper_float_fuel > 0.0
            self.thrusting = floating
            if floating:
                accel = -STOMPER_FLOAT_ACCEL
                self.stomper_float_fuel = max(0.0, self.stomper_float_fuel - dt)
            else:
                accel = GRAVITY
        elif is_profit_bird:
            # Flappy-Bird style: each fresh press is a discrete hop (velocity set
            # outright, not held/accumulated), no fuel involved.
            if just_pressed:
                self.vy = -PROFIT_BIRD_FLAP_SPEED
            self.thrusting = False
            accel = GRAVITY
        else:
            can_thrust = thrust_held and self.fuel > 0.0
            if just_pressed and can_thrust:
                self.press_streak += 1

            self.thrusting = can_thrust
            if can_thrust:
                idx = min(self.press_streak, len(PRESS_BOOST_MULTIPLIERS)) - 1
                accel = -THRUST_ACCEL * PRESS_BOOST_MULTIPLIERS[idx]
                self.fuel = max(0.0, self.fuel - dt)
            else:
                accel = GRAVITY

        self.vy += accel * dt
        self.vy = max(-MAX_RISE_SPEED, min(MAX_FALL_SPEED, self.vy))
        self.y += self.vy * dt

        top = self.lane_top + PLAYER_SIZE / 2
        if is_stomper:
            top = self.lane_top + self.lane_h * STOMPER_CEILING_FRAC  # ground-hugging: can't climb as high
        bottom = self.lane_top + self.lane_h - PLAYER_SIZE / 2
        if self.y < top:
            self.y, self.vy = top, 0.0
        elif self.y > bottom:
            self.y, self.vy = bottom, 0.0
        self.on_ground = self.y >= bottom - 0.5

        # Base jetpack fuel only refills once actually resting on the lane floor and
        # not holding -- it used to refill mid-air too, which let a player hover
        # indefinitely by tapping just enough to stay airborne while fuel quietly
        # topped back up underneath them.
        if not is_stomper and not is_profit_bird and not thrust_held and self.on_ground:
            self.fuel = min(FUEL_MAX, self.fuel + FUEL_REGEN_RATE * dt)
            if self.fuel >= FUEL_MAX:
                self.press_streak = 0  # fully rested -- next press starts a new streak
        if is_stomper and self.on_ground:
            self.stomper_float_fuel = min(STOMPER_FLOAT_FUEL_MAX,
                                           self.stomper_float_fuel + STOMPER_FLOAT_REGEN_RATE * dt)

        dist_mult = PROFIT_BIRD_DISTANCE_MULT if is_profit_bird else 1.0
        self.distance += scroll_speed * dt * dist_mult

    def kill(self):
        self.alive = False

    def activate_vehicle(self, kind):
        self.vehicle_active = True
        self.vehicle_kind = kind
        self.vehicle_timer = VEHICLE_DURATION
        if kind == "lil_stomper":
            self.stomper_float_fuel = STOMPER_FLOAT_FUEL_MAX  # fresh float charge on pickup

    def on_hazard_hit(self):
        """A vehicle absorbs one hit (knocked out, not killed); otherwise it's fatal."""
        if self.hit_grace > 0.0:
            return
        if self.vehicle_active:
            self.vehicle_active = False
            self.vehicle_kind = None
            self.vehicle_timer = 0.0
            self.hit_grace = HIT_GRACE_PERIOD
        else:
            self.alive = False


class Zapper:
    """An electric beam strung between two emitter nodes.

    Defined by its two endpoints rather than a w/h box, so a single class covers
    vertical, horizontal and diagonal beams at any length. Collision is measured against
    the beam segment itself, never its bounding box: for a diagonal that box covers
    roughly twice the area the beam visibly occupies, and dying to the empty corner of it
    reads as a cheat.
    """
    __slots__ = ("x0", "y0", "x1", "y1")

    def __init__(self, x0, y0, x1, y1):
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1

    def rect(self):
        """Bounding box -- for spawn-blocker checks, culling and explosion placement.
        Never used for collision; see collides()."""
        left, right = min(self.x0, self.x1), max(self.x0, self.x1)
        top, bottom = min(self.y0, self.y1), max(self.y0, self.y1)
        half = ZAPPER_NODE_SIZE // 2
        return pygame.Rect(int(left) - half, int(top) - half,
                            int(right - left) + ZAPPER_NODE_SIZE,
                            int(bottom - top) + ZAPPER_NODE_SIZE)

    def node_rects(self):
        half = ZAPPER_NODE_SIZE // 2
        for x, y in ((self.x0, self.y0), (self.x1, self.y1)):
            yield pygame.Rect(int(x) - half, int(y) - half, ZAPPER_NODE_SIZE, ZAPPER_NODE_SIZE)

    def collides(self, rect):
        """Segment-vs-rect (pygame's own exact clipline) plus the two solid emitter nodes.

        The beam counts as zero-width here while it draws ~ZAPPER_ARC_H thick, so the
        check errs a few pixels in the player's favour -- the right direction to err.
        """
        if rect.clipline((int(self.x0), int(self.y0)), (int(self.x1), int(self.y1))):
            return True
        return any(rect.colliderect(n) for n in self.node_rects())

    def blocked_y_range(self, xa, xb):
        """The vertical span this zapper occupies across the column [xa, xb), or None.

        Feeds pattern_is_passable(). Covers the beam (interpolated across the column and
        padded by its drawn thickness) plus both emitter nodes.
        """
        spans = []
        half_arc = ZAPPER_ARC_H / 2.0
        half_node = ZAPPER_NODE_SIZE / 2.0
        left, right = min(self.x0, self.x1), max(self.x0, self.x1)
        # The x-padding matters for a vertical beam, which has no x-extent of its own --
        # without it a column sweep could step straight over one and call the lane clear.
        if xb > left - half_arc and xa < right + half_arc:
            if abs(self.x1 - self.x0) < 1e-6:
                ya, yb = min(self.y0, self.y1), max(self.y0, self.y1)
            else:
                span = self.x1 - self.x0
                t0 = max(0.0, min(1.0, (max(xa, left) - self.x0) / span))
                t1 = max(0.0, min(1.0, (min(xb, right) - self.x0) / span))
                ea = self.y0 + (self.y1 - self.y0) * t0
                eb = self.y0 + (self.y1 - self.y0) * t1
                ya, yb = min(ea, eb), max(ea, eb)
            spans.append((ya - half_arc, yb + half_arc))
        for x, y in ((self.x0, self.y0), (self.x1, self.y1)):
            if xb > x - half_node and xa < x + half_node:
                spans.append((y - half_node, y + half_node))
        if not spans:
            return None
        return min(s[0] for s in spans), max(s[1] for s in spans)


class MissileWarning:
    """The exclamation-point telegraph that precedes every straight missile.

    Purely visual and deliberately absent from Lane.hazards() -- but more than that, the
    Missile object isn't constructed until this elapses, so for the whole warning window
    there is literally nothing in the lane to collide with.
    """
    __slots__ = ("y", "timer")

    def __init__(self, y):
        self.y, self.timer = y, 0.0

    def ready(self):
        return self.timer >= MISSILE_WARN_TIME


class Missile:
    __slots__ = ("x", "y", "w", "h", "vx")

    def __init__(self, x, y, w, h, vx):
        self.x, self.y, self.w, self.h, self.vx = x, y, w, h, vx

    def rect(self):
        return pygame.Rect(int(self.x), int(self.y), int(self.w), int(self.h))

    def collides(self, rect):
        return rect.colliderect(self.rect())


class SeekingMissile:
    """Blinks in place as a telegraph, then launches and gently homes on the player's y.

    Only becomes a live hazard once it's launched (state == "seeking") -- while still
    blinking it's a visible warning, not yet dangerous, per the "fair warning" ask.
    """
    __slots__ = ("x", "y", "w", "h", "vx", "vy", "speed", "state", "blink_timer", "toggle_count", "visible")

    def __init__(self, x, y, w, h, speed):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.vx = 0.0
        self.vy = 0.0
        self.speed = speed
        self.state = "telegraph"
        self.blink_timer = 0.0
        self.toggle_count = 0
        self.visible = True

    def rect(self):
        return pygame.Rect(int(self.x), int(self.y), int(self.w), int(self.h))

    def collides(self, rect):
        return rect.colliderect(self.rect())

    def update(self, dt, target_y):
        if self.state == "telegraph":
            self.blink_timer += dt
            if self.blink_timer >= SEEKER_BLINK_INTERVAL:
                self.blink_timer -= SEEKER_BLINK_INTERVAL
                self.visible = not self.visible
                self.toggle_count += 1
                if self.toggle_count >= SEEKER_BLINK_COUNT * 2:
                    self.state = "seeking"
                    self.visible = True
                    self.vx = -self.speed
            return

        # Steer vy towards the player with a clamped turn rate -- a wide enough turn
        # radius that a last-second move up/down can still dodge it.
        desired_vy = max(-SEEKER_MAX_VY, min(SEEKER_MAX_VY, (target_y - self.y) * SEEKER_STEER_GAIN))
        max_delta = SEEKER_TURN_RATE * dt
        self.vy += max(-max_delta, min(max_delta, desired_vy - self.vy))
        self.x += self.vx * dt
        self.y += self.vy * dt


class VehicleToken:
    """A Profit Bird / Lil' Stomper pickup -- grants Player.activate_vehicle() on collect."""
    __slots__ = ("x", "y", "w", "h", "kind")

    def __init__(self, x, y, w, h, kind):
        self.x, self.y, self.w, self.h, self.kind = x, y, w, h, kind

    def rect(self):
        return pygame.Rect(int(self.x), int(self.y), int(self.w), int(self.h))


class Coin:
    __slots__ = ("x", "y", "w", "h")

    def __init__(self, x, y, w, h):
        self.x, self.y, self.w, self.h = x, y, w, h

    def rect(self):
        return pygame.Rect(int(self.x), int(self.y), int(self.w), int(self.h))


class Scientist:
    """A ground-running bystander -- a reward, not a hazard.

    Simple state machine: "running" -> "knocked" -> despawn. Only "running" scientists
    are collision-checked (see Lane.knock_over_scientists); once knocked it stops running
    under its own power, plays the knockdown animation once, holds the final frame for
    SCIENTIST_KNOCKED_HOLD, then drops out of the lane.

    `anim_t` is the scientist's own elapsed time rather than the global clock, both so
    each one runs on its own phase instead of in lockstep with every other scientist and
    because the one-shot knockdown (anim_frame(..., loop=False)) needs time measured from
    the moment it was knocked over.
    """
    __slots__ = ("x", "y", "w", "h", "state", "anim_t")

    def __init__(self, x, y, w, h):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.state = "running"
        self.anim_t = 0.0

    def rect(self):
        return pygame.Rect(int(self.x), int(self.y), int(self.w), int(self.h))

    def knock_over(self):
        self.state = "knocked"
        self.anim_t = 0.0  # restarts the clock so the knockdown plays from frame 0

    def update(self, dt):
        self.anim_t += dt

    def done(self):
        """True once the knockdown has finished playing and held its last frame."""
        if self.state != "knocked":
            return False
        return self.anim_t >= SCIENTIST_DOWN_FRAME_COUNT / SCIENTIST_DOWN_FPS + SCIENTIST_KNOCKED_HOLD


class Explosion:
    """Purely visual, no collision -- marks where a hazard got detonated by a vehicle pickup."""
    __slots__ = ("x", "y", "timer")

    def __init__(self, x, y):
        self.x, self.y, self.timer = x, y, 0.0


class ScorePopup:
    """Purely visual -- a small '+N' that floats up and fades where a bonus was scored."""
    __slots__ = ("x", "y", "text", "timer")

    def __init__(self, x, y, text):
        self.x, self.y, self.text, self.timer = x, y, text, 0.0


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


class Lane:
    """Owns one player's independent zapper/missile streams, so each run is self-contained."""

    def __init__(self, lane_top, lane_h):
        self.lane_top = lane_top
        self.lane_h = lane_h
        self.rng = random.Random()
        self.seed = None
        self.reset()

    def reset(self, seed=None):
        """Rebuilds the lane. Passing the same `seed` to both lanes gives both players a
        byte-identical course, which is the whole point of a head-to-head race: every
        spawn position and timing is drawn from this rng, so seeding it identically makes
        skill the only variable. Left unseeded (None) the lane keeps its previous
        behaviour of an arbitrary course, which is what the headless tests lean on.
        """
        self.seed = seed
        if seed is not None:
            self.rng.seed(seed)
        self.zappers = []
        self.next_spawn_x = float(SCREEN_W + 100)
        self.scroll_speed = SCROLL_SPEED0
        self.missiles = []
        self.missile_warnings = []
        # Grace period before the first missile of a run -- no ramp-up applied yet.
        self.next_missile_in = self.rng.uniform(MISSILE_START_DELAY_MIN, MISSILE_START_DELAY_MAX)
        self.seekers = []
        self.next_seeker_in = self.rng.uniform(SEEKER_START_DELAY_MIN, SEEKER_START_DELAY_MAX)
        self.tokens = []
        self.next_token_in = self.rng.uniform(TOKEN_SPAWN_MIN, TOKEN_SPAWN_MAX)
        self.coins = []
        self.next_coin_in = self.rng.uniform(COIN_SPAWN_MIN, COIN_SPAWN_MAX)
        self.scientists = []
        # Own spawn timer, independent of the zapper/missile/coin ones -- no start
        # delay, since a scientist is a reward rather than a hazard to ramp up to.
        self.next_scientist_in = self.rng.uniform(SCIENTIST_SPAWN_MIN, SCIENTIST_SPAWN_MAX)
        self.explosions = []
        self.popups = []

    def _spawn(self):
        """Emits one whole *pattern* of zappers, not a single obstacle.

        Rerolls until pattern_is_passable() accepts the result, so the gap guarantee
        holds no matter what the random parameters come out as; the fallback below only
        runs if every attempt somehow failed, and cannot block the lane by construction.
        """
        for _ in range(8):
            builder = self.rng.choice(ZAPPER_PATTERNS)
            zappers, width = builder(self.rng, self.lane_top, self.lane_h, self.next_spawn_x)
            if pattern_is_passable(zappers, self.lane_top, self.lane_h):
                break
        else:
            zappers, width = _pattern_fallback(self.rng, self.lane_top, self.lane_h,
                                                self.next_spawn_x)
        self.zappers.extend(zappers)
        # The other half of the no-overlap rule: a pattern can spawn into x that a coin
        # formation already claimed, so drop any coins it lands on. They're still off the
        # right edge at this point, so nothing visibly vanishes.
        self.coins = [c for c in self.coins if coin_is_clear(c.rect(), zappers)]
        self.next_spawn_x += width + self.rng.uniform(ZAPPER_MIN_GAP, ZAPPER_MAX_GAP)

    def _next_missile_gap(self):
        # Gap shrinks from MISSILE_SPAWN_MAX towards MISSILE_SPAWN_MIN as scroll_speed
        # climbs, so missiles come more often the faster/harder the run gets.
        ramp = (self.scroll_speed - SCROLL_SPEED0) / MISSILE_DIFFICULTY_RANGE
        ramp = min(1.0, max(0.0, ramp))
        base_gap = MISSILE_SPAWN_MAX - ramp * (MISSILE_SPAWN_MAX - MISSILE_SPAWN_MIN)
        return base_gap * self.rng.uniform(0.85, 1.15)

    def _spawn_missile_warning(self):
        """Fires the *telegraph*, not the missile. The missile itself is only constructed
        once the warning elapses (see update), so nothing exists to hit until then."""
        y = self.rng.uniform(self.lane_top, self.lane_top + self.lane_h - MISSILE_H)
        self.missile_warnings.append(MissileWarning(y))
        self.next_missile_in = self._next_missile_gap()

    def _launch_missile(self, y):
        # Speed is scroll_speed-relative, not absolute -- otherwise once scroll_speed
        # (which grows unbounded via SCROLL_ACCEL) exceeds the missile's fixed speed,
        # missiles would visually crawl backwards relative to the scrolling zappers.
        # Read at launch rather than at warning time so it tracks the speed the lane is
        # actually running at when the missile appears.
        speed = self.scroll_speed + self.rng.uniform(MISSILE_SPEED_MIN, MISSILE_SPEED_MAX)
        self.missiles.append(Missile(float(SCREEN_W), y, MISSILE_W, MISSILE_H, -speed))

    def _next_seeker_gap(self):
        # Same shrinking-gap idea as straight missiles, just rarer and over a wider range.
        ramp = (self.scroll_speed - SCROLL_SPEED0) / SEEKER_DIFFICULTY_RANGE
        ramp = min(1.0, max(0.0, ramp))
        base_gap = SEEKER_SPAWN_MAX - ramp * (SEEKER_SPAWN_MAX - SEEKER_SPAWN_MIN)
        return base_gap * self.rng.uniform(0.85, 1.15)

    def _spawn_seeker(self):
        y = self.rng.uniform(self.lane_top, self.lane_top + self.lane_h - SEEKER_H)
        speed = self.scroll_speed + self.rng.uniform(SEEKER_SPEED_MIN, SEEKER_SPEED_MAX)
        # Spawns flush against the right edge (not deep off-screen like straight missiles)
        # so the blink telegraph is actually visible as a warning before it launches.
        spawn_x = float(SCREEN_W - SEEKER_W)
        self.seekers.append(SeekingMissile(spawn_x, y, SEEKER_W, SEEKER_H, speed))
        self.next_seeker_in = self._next_seeker_gap()

    def _spawn_token(self):
        kind = self.rng.choice(VEHICLE_KINDS)
        spawn_x = float(SCREEN_W + 40)
        for _ in range(5):
            y = self.rng.uniform(self.lane_top, self.lane_top + self.lane_h - TOKEN_H)
            candidate = pygame.Rect(int(spawn_x), int(y), TOKEN_W, TOKEN_H)
            blockers = [z.rect().inflate(20, 20) for z in self.zappers]
            if not any(candidate.colliderect(b) for b in blockers):
                self.tokens.append(VehicleToken(spawn_x, y, TOKEN_W, TOKEN_H, kind))
                break
        self.next_token_in = self.rng.uniform(TOKEN_SPAWN_MIN, TOKEN_SPAWN_MAX)

    def _spawn_coin_formation(self):
        """Lays 5-10 coins along one of COIN_FORMATIONS instead of dropping a lone coin.

        A formation traces a path, and the path is bait: following a trail is exactly
        what pulls a player into a zapper, which is the tension the single random coin
        never created. Placement is all-or-nothing -- the whole shape has to clear the
        zappers and tokens already in the lane, since a trail that runs into a beam is
        indistinguishable from a safe one until it's too late.
        """
        shape = self.rng.choice(COIN_FORMATIONS)
        n = self.rng.randint(COIN_MIN_COUNT, COIN_MAX_COUNT)
        amp = self.rng.uniform(COIN_ARC_HEIGHT_MIN, COIN_ARC_HEIGHT_MAX)
        offsets = shape(self.rng, n, amp)
        spawn_x = float(SCREEN_W + 20)
        # Baseline range that keeps every coin in the shape inside the lane, whatever
        # its vertical travel -- so a tall arc simply gets a narrower band to sit in
        # rather than clipping through the ceiling or floor.
        base_lo = self.lane_top - min(offsets)
        base_hi = self.lane_top + self.lane_h - COIN_H - max(offsets)
        if base_hi >= base_lo:
            for _ in range(COIN_FORMATION_TRIES):
                base_y = self.rng.uniform(base_lo, base_hi)
                placed = [Coin(spawn_x + i * COIN_SPACING, base_y + dy, COIN_W, COIN_H)
                          for i, dy in enumerate(offsets)]
                if all(coin_is_clear(c.rect(), self.zappers, self.tokens) for c in placed):
                    self.coins.extend(placed)
                    break
        self.next_coin_in = self.rng.uniform(COIN_SPAWN_MIN, COIN_SPAWN_MAX)

    def _spawn_scientist(self):
        # Ground-anchored: unlike zappers/coins the y isn't randomized at all -- the
        # collision box always sits flush on the lane floor (which is exactly where a
        # grounded player's own box sits, so a player running along the floor connects).
        y = float(self.lane_top + self.lane_h - SCIENTIST_H)
        spawn_x = float(SCREEN_W + 30)
        candidate = pygame.Rect(int(spawn_x), int(y), SCIENTIST_W, SCIENTIST_H)
        # Same light-touch check the coin/token spawns use -- keeps a scientist from
        # appearing already embedded in a ground-level zapper.
        blockers = [z.rect().inflate(20, 20) for z in self.zappers]
        if not any(candidate.colliderect(b) for b in blockers):
            self.scientists.append(Scientist(spawn_x, y, SCIENTIST_W, SCIENTIST_H))
        self.next_scientist_in = self.rng.uniform(SCIENTIST_SPAWN_MIN, SCIENTIST_SPAWN_MAX)

    def detonate_hazards(self):
        """Clears every zapper/missile/seeker in the lane (triggered by a vehicle
        pickup) and drops an Explosion VFX at each one's former position. Doesn't touch
        spawn timers -- future hazards still arrive on schedule, this just clears what's
        already in flight so the pickup isn't immediately undone."""
        for z in self.zappers:
            self.explosions.append(Explosion(*z.rect().center))
        for m in self.missiles:
            self.explosions.append(Explosion(*m.rect().center))
        for s in self.seekers:
            self.explosions.append(Explosion(*s.rect().center))
        self.zappers = []
        self.missiles = []
        self.seekers = []

    def update(self, dt, alive, target_y):
        if not alive:
            return  # freeze this lane once its player is out
        self.scroll_speed += SCROLL_ACCEL * dt
        dx = self.scroll_speed * dt
        for z in self.zappers:
            z.x0 -= dx
            z.x1 -= dx
        self.next_spawn_x -= dx
        self.zappers = [z for z in self.zappers if z.rect().right > -20]
        while self.next_spawn_x < SCREEN_W + 300:
            self._spawn()

        self.next_missile_in -= dt
        if self.next_missile_in <= 0:
            self._spawn_missile_warning()
        for w in self.missile_warnings:
            w.timer += dt
            if w.ready():
                self._launch_missile(w.y)
        self.missile_warnings = [w for w in self.missile_warnings if not w.ready()]
        for m in self.missiles:
            m.x += m.vx * dt
        self.missiles = [m for m in self.missiles if m.x + m.w > -20]

        self.next_seeker_in -= dt
        if self.next_seeker_in <= 0:
            self._spawn_seeker()
        for s in self.seekers:
            s.update(dt, target_y)
        self.seekers = [s for s in self.seekers if s.x + s.w > -20]

        self.next_token_in -= dt
        if self.next_token_in <= 0:
            self._spawn_token()
        for tk in self.tokens:
            tk.x -= dx
        self.tokens = [tk for tk in self.tokens if tk.x + tk.w > -20]

        self.next_coin_in -= dt
        if self.next_coin_in <= 0:
            self._spawn_coin_formation()
        for c in self.coins:
            c.x -= dx
        self.coins = [c for c in self.coins if c.x + c.w > -20]

        self.next_scientist_in -= dt
        if self.next_scientist_in <= 0:
            self._spawn_scientist()
        for sci in self.scientists:
            sci.update(dt)
            # A running scientist covers only part of the scroll under its own power, so
            # it slides left slower than the scenery; a knocked-over one has stopped
            # running and just rides the lane at the full scroll speed.
            sci.x -= dx * (SCIENTIST_SPEED_FRAC if sci.state == "running" else 1.0)
        self.scientists = [s for s in self.scientists if s.x + s.w > -20 and not s.done()]

        for e in self.explosions:
            e.timer += dt
            e.x -= dx  # scrolls along with everything else instead of hanging in place
        self.explosions = [e for e in self.explosions if e.timer < EXPLOSION_DURATION]

        for pop in self.popups:
            pop.timer += dt
            pop.x -= dx
        self.popups = [p for p in self.popups if p.timer < POPUP_DURATION]

    def hazards(self):
        """Every live zapper/missile/launched-seeker in this lane, for collision checks.

        Yields the hazard *objects* rather than rects: a zapper is a line segment, not a
        box, so it has to test itself. Every hazard here exposes collides(player_rect),
        which keeps the kill path a single call site in main().

        Scientists are deliberately *not* included -- this feeds the kill logic, and they
        are a reward (see knock_over_scientists for their separate collision path).
        """
        for z in self.zappers:
            yield z
        for m in self.missiles:
            yield m
        for s in self.seekers:
            if s.state == "seeking":
                yield s

    def collect_tokens(self, player_rect):
        """Removes and returns any vehicle tokens overlapping player_rect."""
        hit = [t for t in self.tokens if player_rect.colliderect(t.rect())]
        if hit:
            self.tokens = [t for t in self.tokens if t not in hit]
        return hit

    def collect_coins(self, player_rect):
        """Removes and returns any coins overlapping player_rect."""
        hit = [c for c in self.coins if player_rect.colliderect(c.rect())]
        if hit:
            self.coins = [c for c in self.coins if c not in hit]
        return hit

    def knock_over_scientists(self, player_rect):
        """Knocks over and returns any *running* scientists overlapping player_rect.

        Already-knocked ones are skipped, so a single scientist can only ever pay out
        once no matter how long the player stays on top of it. Drops the "+N" popup here
        rather than in main() so the lane keeps owning its own VFX, the same way
        detonate_hazards() spawns its explosions.
        """
        hit = [s for s in self.scientists
               if s.state == "running" and player_rect.colliderect(s.rect())]
        for s in hit:
            s.knock_over()
            cx, cy = s.rect().center
            self.popups.append(ScorePopup(float(cx), float(cy), f"+{SCIENTIST_BONUS_COINS}"))
        return hit

    def collect_all_coins(self):
        """Removes and returns every coin currently in the lane -- Profit Bird's
        magnet-style auto-collect, instead of requiring exact overlap with the hitbox."""
        hit = self.coins
        self.coins = []
        return hit


def apply_display_mode(mode, desktop_size):
    """(Re)creates the window for `mode` and returns the new display surface.

    `desktop_size` is captured once at startup: querying it later would report the
    current fullscreen window's size instead of the desktop's.
    """
    if mode == "fullscreen":
        return pygame.display.set_mode(desktop_size, pygame.FULLSCREEN)
    if mode == "borderless":
        return pygame.display.set_mode(desktop_size, pygame.NOFRAME)
    return pygame.display.set_mode((SCREEN_W, SCREEN_H), pygame.RESIZABLE)


def present(window, canvas):
    """Scales the fixed-size canvas into the window and flips.

    Scaling is uniform on both axes and the leftover space becomes letterbox bars, so no
    mode ever stretches the art or crops the playfield -- a 16:9 screen just gets bars
    beside the 1100x640 (55:32) frame. smoothscale rather than nearest: at the arbitrary
    non-integer factors an arbitrary window produces, nearest leaves the sprite pixels
    unevenly sized and the antialiased UI text visibly jagged.
    """
    win_w, win_h = window.get_size()
    scale = min(win_w / SCREEN_W, win_h / SCREEN_H)
    size = (max(1, round(SCREEN_W * scale)), max(1, round(SCREEN_H * scale)))
    window.fill(BLACK)
    frame = canvas if size == (SCREEN_W, SCREEN_H) else pygame.transform.smoothscale(canvas, size)
    window.blit(frame, frame.get_rect(center=(win_w // 2, win_h // 2)))
    pygame.display.flip()


def draw_text_center(surface, text, font, color, center):
    surf = font.render(text, True, color)
    surface.blit(surf, surf.get_rect(center=center))


def load_scaled_sprite(path, target_h):
    """Loads a PNG and scales it (preserving aspect ratio) to a target pixel height."""
    img = pygame.image.load(path).convert_alpha()
    scale = target_h / img.get_height()
    size = (max(1, round(img.get_width() * scale)), target_h)
    return pygame.transform.scale(img, size)


def tinted_sprite(sprite, color):
    """Multiplies a sprite by a flat color to give a distinct recolor for player 2.

    `color` should be pre-lightened (blended towards white) before calling this --
    multiplying by a fully-saturated color crushes the sprite's shading to a single
    hue (e.g. everything reading as "red") instead of a subtle recolor.
    """
    tint = sprite.copy()
    overlay = pygame.Surface(tint.get_size(), pygame.SRCALPHA)
    overlay.fill((*color, 255))
    tint.blit(overlay, (0, 0), special_flags=pygame.BLEND_RGB_MULT)
    return tint


def lighten(color, amount):
    """Blends `color` towards white by `amount` (0-1), for a gentler tint multiplier."""
    return tuple(round(c + (255 - c) * amount) for c in color)


def anim_frame(frames, t, fps, pingpong=False, loop=True):
    """Picks the current frame surface from `frames` given clock time `t` and playback `fps`.

    With `loop=False` the index clamps to the last frame instead of wrapping, so a one-shot
    animation (the scientist knockdown) plays through once and then holds on its final
    frame. In that mode `t` must be time elapsed since the animation started, not global
    clock time -- otherwise it would already be clamped by the time it was first drawn.
    """
    n = len(frames)
    if n == 1:
        return frames[0]
    if pingpong:
        period = 2 * (n - 1)
        i = int(t * fps) % period
        i = i if i < n else period - i
    elif loop:
        i = int(t * fps) % n
    else:
        i = min(int(t * fps), n - 1)
    return frames[i]


def draw_flame(surface, tip, t):
    """Small flickering triangle flame trailing downward from `tip`, animated by clock time `t`."""
    wobble = math.sin(t * 26) * 3
    length = 14 + math.sin(t * 18) * 4
    x, y = tip
    pts = [(x - 6, y), (x + 6, y), (x + wobble, y + length)]
    color = THRUST_COLOR if int(t * 16) % 2 == 0 else (255, 170, 60)
    pygame.draw.polygon(surface, color, pts)


def draw_player(surface, p, t, run_frames, jetpack_frames, death_frames):
    """Cycles through running/jetpack while alive; plays the charred-death animation once on death."""
    if not p.alive:
        idx = min(int(p.death_anim_t * DEATH_FPS), len(death_frames) - 1)
        sprite = death_frames[idx]
        img_rect = sprite.get_rect(center=(PLAYER_X, int(p.y)))
        surface.blit(sprite, img_rect)
        return

    if p.thrusting:
        sprite = anim_frame(jetpack_frames, t, JETPACK_FPS, pingpong=True)
    else:
        sprite = anim_frame(run_frames, t, RUN_FPS)
    img_rect = sprite.get_rect(center=(PLAYER_X, int(p.y)))
    if p.thrusting:
        flame_tip = (img_rect.left + img_rect.width * 0.32, img_rect.bottom - 3)
        draw_flame(surface, flame_tip, t)
    surface.blit(sprite, img_rect)


def draw_vehicle_player(surface, p, t, frames, fps):
    """Vehicle-mode sprite swap: Profit Bird passes a single-frame list (static, just
    bobs); Lil' Stomper passes its 6-frame walk cycle (anim_frame loops it like run_frames).
    Draws a jetpack flame while Lil' Stomper is actively floating (p.thrusting), same as
    the base character's jetpack flame, so the limited float is visually legible."""
    sprite = anim_frame(frames, t, fps)
    bob = math.sin(t * 5) * 3
    img_rect = sprite.get_rect(center=(PLAYER_X, int(p.y + bob)))
    if p.thrusting:
        flame_tip = (img_rect.left + img_rect.width * 0.32, img_rect.bottom - 3)
        draw_flame(surface, flame_tip, t)
    surface.blit(sprite, img_rect)


def draw_zapper(surface, z, t, arc_frames, node_frames):
    """An emitter node at each end with the animated arc tiled along the segment between.

    The beam is built as a horizontal strip (tiling consecutive arc frames along its
    length, which reads as travelling crackle rather than one shape flashing in place)
    and then rotated once to the segment's angle -- far simpler than trying to place
    each tile along a diagonal, and it costs one rotate per zapper per frame.

    The `z.x0 * 0.013` phase offset is the same trick draw_coin uses: it keeps every
    beam on screen from crackling in perfect unison.
    """
    phase = t + z.x0 * 0.013
    node = anim_frame(node_frames, phase, ZAPPER_NODE_FPS)
    dx, dy = z.x1 - z.x0, z.y1 - z.y0
    length = math.hypot(dx, dy)
    if length >= 1.0:
        span = int(length)
        strip = pygame.Surface((span, ZAPPER_ARC_H), pygame.SRCALPHA)
        i = int(phase * ZAPPER_ARC_FPS)
        x = 0
        while x < span:
            tile = arc_frames[i % len(arc_frames)]
            strip.blit(tile, (x, (ZAPPER_ARC_H - tile.get_height()) // 2))
            x += tile.get_width()
            i += 1
        # pygame rotates counter-clockwise, screen y grows downward -- hence the negation.
        strip = pygame.transform.rotate(strip, -math.degrees(math.atan2(dy, dx)))
        surface.blit(strip, strip.get_rect(center=(int((z.x0 + z.x1) / 2), int((z.y0 + z.y1) / 2))))
    for x, y in ((z.x0, z.y0), (z.x1, z.y1)):
        surface.blit(node, node.get_rect(center=(int(x), int(y))))


def draw_missile(surface, m, sprite):
    """Missile sprite -- art already noses left, matching the missile's right-to-left travel."""
    img_rect = sprite.get_rect(center=m.rect().center)
    surface.blit(sprite, img_rect)


def draw_missile_warning(surface, warn, t, lane_top, lane_h):
    """Blinking exclamation-point marker pinned to the right edge of the lane, at the y
    the missile will fire from. Primitives only, no sprite. Purely visual -- the missile
    it announces doesn't exist yet, so there is nothing here to collide with."""
    rect = pygame.Rect(0, 0, MISSILE_WARN_W, MISSILE_WARN_H)
    cy = int(warn.y + MISSILE_H / 2)
    # Clamp so a marker for a missile at the very top/bottom of the lane doesn't poke
    # out into the neighbouring lane.
    cy = max(lane_top + MISSILE_WARN_H // 2, min(lane_top + lane_h - MISSILE_WARN_H // 2, cy))
    rect.center = (SCREEN_W - MISSILE_WARN_W // 2 - 4, cy)
    bright = int(t * MISSILE_WARN_BLINK_HZ) % 2 == 0
    pygame.draw.rect(surface, (235, 60, 50) if bright else (140, 38, 32), rect, border_radius=5)
    pygame.draw.rect(surface, WHITE, rect, width=2, border_radius=5)
    # The "!" -- a stem and a separate dot.
    bar_w = 4
    stem_x = rect.centerx - bar_w // 2
    pygame.draw.rect(surface, WHITE, (stem_x, rect.top + 6, bar_w, MISSILE_WARN_H - 17))
    pygame.draw.rect(surface, WHITE, (stem_x, rect.bottom - 8, bar_w, bar_w))


def draw_seeking_missile(surface, s, sprite_telegraph, sprite_armed):
    """Reuses a tinted duplicate of the straight-missile sprite -- orange while
    blink-telegraphing, red once armed/launched -- keeping the blink warning intact."""
    if s.state == "telegraph" and not s.visible:
        return  # mid-blink "off" phase
    sprite = sprite_telegraph if s.state == "telegraph" else sprite_armed
    img_rect = sprite.get_rect(center=s.rect().center)
    surface.blit(sprite, img_rect)


def draw_vehicle_token(surface, token, t, sprite):
    """Vehicle powerup pickup -- assets/vehicle_token.png with a gentle pulse and a per-kind
    color halo behind it (a flat tint crushes the coin's own gold to a muddy color for
    blue-ish kinds, since multiply can't add a channel the base image doesn't have)."""
    pulse = 1.0 + math.sin(t * 6) * 0.08
    size = (round(sprite.get_width() * pulse), round(sprite.get_height() * pulse))
    img = pygame.transform.smoothscale(sprite, size)
    rect = token.rect()
    halo_radius = max(size) // 2 + 5
    halo = pygame.Surface((halo_radius * 2, halo_radius * 2), pygame.SRCALPHA)
    pygame.draw.circle(halo, (*VEHICLE_COLORS[token.kind], 130), (halo_radius, halo_radius), halo_radius)
    surface.blit(halo, halo.get_rect(center=rect.center))
    surface.blit(img, img.get_rect(center=rect.center))


def draw_coin(surface, coin, t, sprite):
    """assets/coin_ni_64.png, with a subtle per-coin bob so a run of coins doesn't look static."""
    cx, cy = coin.rect().center
    bob = math.sin(t * 4 + coin.x * 0.05) * 2
    surface.blit(sprite, sprite.get_rect(center=(cx, cy + bob)))


def draw_scientist(surface, sci, run_frames, down_frames):
    """Loops the 8-frame run cycle, or plays the 4-frame knockdown once and holds its last
    frame (loop=False). Driven by the scientist's own anim_t, not the global clock."""
    if sci.state == "knocked":
        sprite = anim_frame(down_frames, sci.anim_t, SCIENTIST_DOWN_FPS, loop=False)
    else:
        sprite = anim_frame(run_frames, sci.anim_t, SCIENTIST_RUN_FPS)
    rect = sci.rect()
    # Pushed down by the frames' built-in transparent footer so the visible feet land on
    # the lane floor rather than the empty bottom edge of the canvas.
    surface.blit(sprite, sprite.get_rect(midbottom=(rect.centerx, rect.bottom + SCIENTIST_FOOT_PAD)))


def draw_score_popup(surface, popup, font):
    """Small '+N' that floats upward and fades out over POPUP_DURATION."""
    progress = min(1.0, popup.timer / POPUP_DURATION)
    alpha = int(255 * (1.0 - progress * progress))  # holds bright, then drops off late
    if alpha <= 0:
        return
    text = font.render(popup.text, True, POPUP_COLOR)
    text.set_alpha(alpha)
    surface.blit(text, text.get_rect(center=(int(popup.x), int(popup.y - POPUP_RISE * progress))))


def draw_explosion(surface, e):
    """PLACEHOLDER: primitive expanding ring + fading core -- no blast sprite/particles yet."""
    progress = min(1.0, e.timer / EXPLOSION_DURATION)
    radius = max(1, int(EXPLOSION_MAX_RADIUS * progress))
    alpha = int(255 * (1.0 - progress))
    if alpha <= 0:
        return
    surf = pygame.Surface((radius * 2, radius * 2), pygame.SRCALPHA)
    pygame.draw.circle(surf, (255, 200, 60, alpha), (radius, radius), radius, width=max(2, radius // 4))
    pygame.draw.circle(surf, (255, 90, 40, alpha), (radius, radius), max(1, int(radius * 0.5)))
    surface.blit(surf, surf.get_rect(center=(int(e.x), int(e.y))))


def load_high_scores():
    """Entries are (name, distance, coins) -- distance still determines rank; coins just rides along."""
    if not os.path.exists(SCORES_PATH):
        return []
    try:
        with open(SCORES_PATH) as f:
            data = json.load(f)
        # .get("coins", 0) so older high_scores.json entries (saved before coins existed) still load.
        return [(d["name"], d["score"], d.get("coins", 0)) for d in data][:MAX_HIGH_SCORES]
    except Exception as exc:
        print(f"Could not load high scores ({exc}) -- starting with an empty board.")
        return []


def save_high_scores(scores):
    try:
        with open(SCORES_PATH, "w") as f:
            json.dump([{"name": n, "score": s, "coins": c} for n, s, c in scores], f, indent=2)
    except Exception as exc:
        print(f"Could not save high scores: {exc}")


def add_high_scores(scores, entries):
    """Merges new (name, distance, coins) entries in, re-sorts by distance, trims, and persists."""
    merged = scores + entries
    merged.sort(key=lambda entry: entry[1], reverse=True)
    merged = merged[:MAX_HIGH_SCORES]
    save_high_scores(merged)
    return merged


def draw_high_scores(surface, font_header, font_row, scores):
    """Top-5 leaderboard (ranked by distance, coins shown alongside), right-aligned in the top-right corner."""
    right_x = SCREEN_W - 16
    y = 14
    header = font_header.render("TOP 5", True, WHITE)
    surface.blit(header, (right_x - header.get_width(), y))
    y += 26
    if not scores:
        text = font_row.render("no runs yet", True, (150, 150, 160))
        surface.blit(text, (right_x - text.get_width(), y))
        return
    for i, (name, score, coins) in enumerate(scores, start=1):
        text = font_row.render(f"{i}. {name} - {int(score)}m, {coins}c", True, WHITE)
        surface.blit(text, (right_x - text.get_width(), y))
        y += 20


def draw_name_entry(surface, font_big, font_med, font_small, names, active_field, display_label):
    draw_text_center(surface, "ENTER PLAYER NAMES", font_big, WHITE, (SCREEN_W // 2, 90))
    box_w, box_h = 340, 48
    for i in range(2):
        box = pygame.Rect(0, 0, box_w, box_h)
        box.center = (SCREEN_W // 2, 220 + i * 100)
        color = PLAYER_COLORS[i]
        pygame.draw.rect(surface, (35, 38, 50), box, border_radius=8)
        pygame.draw.rect(surface, color, box, width=3 if active_field == i else 1, border_radius=8)

        label = font_small.render(f"Player {i + 1}", True, color)
        surface.blit(label, (box.left, box.top - 22))

        text_surf = font_med.render(names[i], True, WHITE)
        surface.blit(text_surf, (box.left + 12, box.centery - text_surf.get_height() // 2))

        if active_field == i and int(time.time() * 2) % 2 == 0:
            cursor_x = box.left + 12 + text_surf.get_width() + 2
            pygame.draw.line(surface, WHITE, (cursor_x, box.top + 8), (cursor_x, box.bottom - 8), 2)

    draw_text_center(surface, "Type a name  -  TAB to switch field  -  ENTER to start",
                      font_small, (170, 170, 180), (SCREEN_W // 2, SCREEN_H - 40))
    draw_text_center(surface, f"F11: display mode ({display_label})  -  ESC to quit",
                      font_small, (130, 130, 140), (SCREEN_W // 2, SCREEN_H - 16))


def main():
    pygame.init()
    pygame.display.set_caption("Two-Player Jetpack Joyride Demo")
    # Captured before any window exists -- once we're in fullscreen, a display query
    # would report the window's size rather than the desktop's.
    desktop_size = pygame.display.get_desktop_sizes()[0]
    display_mode = DISPLAY_MODES[0]
    window = apply_display_mode(display_mode, desktop_size)
    # Everything draws here at a fixed size, whatever the window is doing.
    canvas = pygame.Surface((SCREEN_W, SCREEN_H))
    clock = pygame.time.Clock()
    font_big = pygame.font.SysFont("consolas", 48, bold=True)
    font_med = pygame.font.SysFont("consolas", 28, bold=True)
    font_small = pygame.font.SysFont("consolas", 20)

    p2_tint = lighten(PLAYER_COLORS[1], 0.55)  # gentle tint -- a raw multiply crushed everything to red
    run_frames_base = [load_scaled_sprite(os.path.join(ASSET_DIR, f"run_{i:02d}.png"), PLAYER_SPRITE_H)
                        for i in range(RUN_FRAME_COUNT)]
    jetpack_frames_base = [load_scaled_sprite(os.path.join(ASSET_DIR, f"jetpack_{i:02d}.png"), PLAYER_SPRITE_H)
                            for i in range(JETPACK_FRAME_COUNT)]
    run_frames = [run_frames_base, [tinted_sprite(f, p2_tint) for f in run_frames_base]]
    jetpack_frames = [jetpack_frames_base, [tinted_sprite(f, p2_tint) for f in jetpack_frames_base]]

    death_frames = [load_scaled_sprite(os.path.join(ASSET_DIR, f"death_{i:02d}.png"), DEATH_SPRITE_H)
                     for i in range(DEATH_FRAME_COUNT)]

    # Vehicle-mode sprite swaps -- Profit Bird passed to draw_vehicle_player() as a
    # single-frame list (static, just bobs); Lil' Stomper as its real 6-frame walk cycle.
    profit_bird_frames = [load_scaled_sprite(os.path.join(ASSET_DIR, "profit_bird.png"), PROFIT_BIRD_SPRITE_H)]
    stomper_frames = [load_scaled_sprite(os.path.join(ASSET_DIR, f"lilstomper_{i:02d}.png"), STOMPER_SPRITE_H)
                       for i in range(STOMPER_FRAME_COUNT)]

    # Vehicle token pickup uses one shared icon (assets/vehicle_token.png) -- kind is
    # communicated with a color halo (see draw_vehicle_token) rather than tinting the
    # coin's own pixels, since multiplying a mostly-gold sprite by a blue-ish tint just
    # crushes it to a muddy gray instead of reading as blue.
    vehicle_token_sprite = load_scaled_sprite(os.path.join(ASSET_DIR, "vehicle_token.png"), TOKEN_H)

    coin_sprite = load_scaled_sprite(os.path.join(ASSET_DIR, "coin_ni_64.png"), COIN_H)

    # Scientists are world objects (like zappers and coins), so unlike the player frames
    # they aren't tinted per lane -- both lanes share one set.
    scientist_run_frames = [load_scaled_sprite(os.path.join(ASSET_DIR, f"manager_run_{i:02d}.png"),
                                                SCIENTIST_SPRITE_H)
                             for i in range(SCIENTIST_RUN_FRAME_COUNT)]
    scientist_down_frames = [load_scaled_sprite(os.path.join(ASSET_DIR, f"manager_down_{i:02d}.png"),
                                                 SCIENTIST_SPRITE_H)
                              for i in range(SCIENTIST_DOWN_FRAME_COUNT)]

    missile_sprite = load_scaled_sprite(os.path.join(ASSET_DIR, "missile.png"), MISSILE_H + 4)

    # Zapper art, cut from the Stage Hazards sheet. The arc frames are the *tileable*
    # beam segment (uniform 22px source width, seamless butted end-to-end); the other
    # extracted set on that sheet is the emitter burst, and zapper_full_*.png is a fixed
    # 118px pre-assembled zapper kept as reference art -- neither is used here, since
    # building from nodes + a tiled beam is what allows arbitrary length and angle.
    zapper_node_frames = [load_scaled_sprite(os.path.join(ASSET_DIR, f"zapper_node_{i:02d}.png"),
                                              ZAPPER_NODE_SIZE)
                           for i in range(ZAPPER_NODE_FRAME_COUNT)]
    zapper_arc_frames = [load_scaled_sprite(os.path.join(ASSET_DIR, f"zapper_arc_g2_{i:02d}.png"),
                                             ZAPPER_ARC_H)
                          for i in range(ZAPPER_ARC_FRAME_COUNT)]

    # Seeking missiles: a duplicate of the straight-missile art (assets/missile_seeker.png),
    # tinted per state so the blink telegraph (orange) vs. armed/launched (red) distinction
    # from the old placeholder chevron still reads, just on a real sprite now.
    seeker_sprite_base = load_scaled_sprite(os.path.join(ASSET_DIR, "missile_seeker.png"), SEEKER_H + 6)
    seeker_sprite_telegraph = tinted_sprite(seeker_sprite_base, lighten((255, 140, 60), 0.35))
    seeker_sprite_armed = tinted_sprite(seeker_sprite_base, lighten((255, 60, 60), 0.35))

    background_sprite = pygame.image.load(os.path.join(ASSET_DIR, "background.png")).convert()
    background_sprite = pygame.transform.smoothscale(background_sprite, (SCREEN_W, SCREEN_H))
    # Translucent per-lane tint over the background -- keeps the two lanes visually
    # separated and hazards/text readable without fully hiding the artwork underneath.
    lane_overlays = []
    for color in LANE_BG:
        overlay = pygame.Surface((SCREEN_W, LANE_H), pygame.SRCALPHA)
        overlay.fill((*color, 165))
        lane_overlays.append(overlay)

    daq = DaqIO()

    lanes = [Lane(0, LANE_H), Lane(LANE_H, LANE_H)]
    players = [Player(0, 0, LANE_H), Player(1, LANE_H, LANE_H)]
    key_map = [pygame.K_SPACE, pygame.K_UP]

    round_seed = None

    def reset_game():
        # One seed per round, shared by both lanes, so the two players race the exact
        # same course and the result is skill rather than who drew the kinder lane.
        nonlocal round_seed
        round_seed = random.randrange(2 ** 31)
        for lane in lanes:
            lane.reset(round_seed)
        for p in players:
            p.reset()

    high_scores = load_high_scores()
    player_names = ["Player 1", "Player 2"]
    name_inputs = ["", ""]
    active_field = 0
    scores_recorded = False
    state = "enter_names"  # "enter_names" or "playing" (game-over is just "playing" + both dead)
    pygame.key.start_text_input()  # on for name entry; turned off during "playing" (see below)

    def go_to_name_entry():
        # Returning to name entry from game-over (via R or both-buttons-down). Re-enabling
        # text input here -- rather than leaving it on throughout "playing" -- is what stops
        # the R keypress that triggers this from also leaking an "r" into the name field:
        # while text input is off, SDL never generates a TEXTINPUT event for that keypress
        # in the first place, so there's nothing to leak regardless of event ordering.
        nonlocal name_inputs, active_field, state
        name_inputs = [player_names[0], player_names[1]]  # prefilled, quick restart
        active_field = 0
        state = "enter_names"
        pygame.key.start_text_input()

    running = True
    while running:
        dt = min(clock.tick(FPS) / 1000.0, 0.05)  # clamp so a hiccup can't cause a physics jump

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.VIDEORESIZE and display_mode == "windowed":
                window = pygame.display.set_mode(event.size, pygame.RESIZABLE)
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_F11:
                    # Cycle windowed -> windowed fullscreen -> fullscreen. Handled before
                    # the state checks so it works on every screen, including mid-run.
                    display_mode = DISPLAY_MODES[(DISPLAY_MODES.index(display_mode) + 1)
                                                  % len(DISPLAY_MODES)]
                    window = apply_display_mode(display_mode, desktop_size)
                elif event.key == pygame.K_ESCAPE:
                    running = False
                elif state == "enter_names":
                    if event.key == pygame.K_BACKSPACE:
                        name_inputs[active_field] = name_inputs[active_field][:-1]
                    elif event.key == pygame.K_TAB:
                        active_field = 1 - active_field
                    elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                        player_names[0] = name_inputs[0].strip() or "Player 1"
                        player_names[1] = name_inputs[1].strip() or "Player 2"
                        reset_game()
                        scores_recorded = False
                        state = "playing"
                        pygame.key.stop_text_input()  # stops KEYDOWN presses during play (e.g. R) from
                                                       # also generating a TEXTINPUT event
                elif state == "playing" and event.key == pygame.K_r and all(not p.alive for p in players):
                    go_to_name_entry()
            elif event.type == pygame.TEXTINPUT and state == "enter_names":
                if len(name_inputs[active_field]) < MAX_NAME_LEN:
                    name_inputs[active_field] += event.text

        if state == "playing":
            if daq.available:
                # Buttons are the primary input during the demo -- keyboard is ignored
                # so an audience member near a keyboard can't steal control.
                daq_b1, daq_b2 = daq.get_buttons()
                thrust = [daq_b1, daq_b2]
            else:
                # No DAQ connected -- fall back to keyboard for dev/testing.
                keys = pygame.key.get_pressed()
                thrust = [keys[key_map[0]], keys[key_map[1]]]

            for i, p in enumerate(players):
                p.update(dt, thrust[i], lanes[i].scroll_speed)
                lanes[i].update(dt, p.alive, p.y)
                if p.alive:
                    p_rect = p.rect()
                    if any(hazard.collides(p_rect) for hazard in lanes[i].hazards()):
                        p.on_hazard_hit()
                    tokens_hit = lanes[i].collect_tokens(p_rect)
                    for token in tokens_hit:
                        p.activate_vehicle(token.kind)
                    if tokens_hit:
                        # Clears the immediate area so the reward isn't undone by
                        # something already in flight the instant it's picked up.
                        lanes[i].detonate_hazards()
                    if p.vehicle_active and p.vehicle_kind == "profit_bird":
                        # Auto-collect: Profit Bird acts as a coin magnet for its lane.
                        p.coins += len(lanes[i].collect_all_coins())
                    else:
                        p.coins += len(lanes[i].collect_coins(p_rect))
                    # Scientists are a reward, so they get their own collision pass rather
                    # than riding on hazards() above -- running into one only knocks it
                    # over, never damages the player. Paid out in coins (not distance) so
                    # the bonus feeds the separate coin leaderboard.
                    p.coins += len(lanes[i].knock_over_scientists(p_rect)) * SCIENTIST_BONUS_COINS

            daq.set_leds(thrust[0] and players[0].alive, thrust[1] and players[1].alive)

            if all(not p.alive for p in players) and not scores_recorded:
                entries = [(player_names[i], players[i].distance, players[i].coins) for i in range(2)]
                high_scores = add_high_scores(high_scores, entries)
                scores_recorded = True

            # Alternate restart: both players' buttons held down together, once the
            # round is over -- same trigger as R, for a hands-on-the-buttons restart
            # that doesn't need reaching for the keyboard.
            if all(not p.alive for p in players) and thrust[0] and thrust[1]:
                go_to_name_entry()
        else:
            daq.set_leds(False, False)

        # ---- draw ----
        anim_t = pygame.time.get_ticks() / 1000.0
        canvas.blit(background_sprite, (0, 0))

        if state == "enter_names":
            dim = pygame.Surface((SCREEN_W, SCREEN_H), pygame.SRCALPHA)
            dim.fill((0, 0, 0, 120))
            canvas.blit(dim, (0, 0))
            draw_name_entry(canvas, font_big, font_med, font_small, name_inputs, active_field,
                             DISPLAY_MODE_LABELS[display_mode])
        else:
            for i, lane in enumerate(lanes):
                canvas.blit(lane_overlays[i], (0, lane.lane_top))
            pygame.draw.line(canvas, BLACK, (0, LANE_H), (SCREEN_W, LANE_H), 2)

            for i, (lane, p) in enumerate(zip(lanes, players)):
                for z in lane.zappers:
                    draw_zapper(canvas, z, anim_t, zapper_arc_frames, zapper_node_frames)
                for sci in lane.scientists:
                    draw_scientist(canvas, sci, scientist_run_frames, scientist_down_frames)
                for m in lane.missiles:
                    draw_missile(canvas, m, missile_sprite)
                for w in lane.missile_warnings:
                    draw_missile_warning(canvas, w, anim_t, lane.lane_top, lane.lane_h)
                for s in lane.seekers:
                    draw_seeking_missile(canvas, s, seeker_sprite_telegraph, seeker_sprite_armed)
                for c in lane.coins:
                    draw_coin(canvas, c, anim_t, coin_sprite)
                for tk in lane.tokens:
                    draw_vehicle_token(canvas, tk, anim_t, vehicle_token_sprite)
                for e in lane.explosions:
                    draw_explosion(canvas, e)
                for pop in lane.popups:
                    draw_score_popup(canvas, pop, font_med)

                if p.alive and p.vehicle_active and p.vehicle_kind == "profit_bird":
                    draw_vehicle_player(canvas, p, anim_t, profit_bird_frames, 1.0)
                elif p.alive and p.vehicle_active and p.vehicle_kind == "lil_stomper":
                    draw_vehicle_player(canvas, p, anim_t, stomper_frames, STOMPER_FPS)
                else:
                    draw_player(canvas, p, anim_t, run_frames[i], jetpack_frames[i], death_frames)

                canvas.blit(font_med.render(f"{player_names[i]}: {int(p.distance)} m   {p.coins} coins", True, WHITE),
                            (16, lane.lane_top + 12))

                # Fuel bar -- shows Lil' Stomper's float-assist charge while that vehicle
                # is active (its own small tank), otherwise the base jetpack fuel.
                if p.vehicle_active and p.vehicle_kind == "lil_stomper":
                    fuel_frac = p.stomper_float_fuel / STOMPER_FLOAT_FUEL_MAX
                else:
                    fuel_frac = p.fuel / FUEL_MAX
                bar_x, bar_y, bar_w, bar_h = 16, lane.lane_top + 46, 140, 10
                pygame.draw.rect(canvas, FUEL_BAR_EMPTY_COLOR, (bar_x, bar_y, bar_w, bar_h), border_radius=4)
                fill_w = int(bar_w * fuel_frac)
                if fill_w > 0:
                    pygame.draw.rect(canvas, FUEL_BAR_COLOR, (bar_x, bar_y, fill_w, bar_h), border_radius=4)

                # Vehicle-mode timer bar, only shown while active
                if p.vehicle_active:
                    vbar_x, vbar_y, vbar_w, vbar_h = 16, lane.lane_top + 64, 140, 8
                    v_color = VEHICLE_COLORS[p.vehicle_kind]
                    pygame.draw.rect(canvas, FUEL_BAR_EMPTY_COLOR, (vbar_x, vbar_y, vbar_w, vbar_h), border_radius=4)
                    vfill_w = int(vbar_w * (p.vehicle_timer / VEHICLE_DURATION))
                    if vfill_w > 0:
                        pygame.draw.rect(canvas, v_color, (vbar_x, vbar_y, vfill_w, vbar_h), border_radius=4)
                    label = font_small.render(VEHICLE_LABELS[p.vehicle_kind], True, v_color)
                    canvas.blit(label, (vbar_x + vbar_w + 8, vbar_y - 6))

                if not p.alive:
                    draw_text_center(canvas, f"{player_names[i]} Down!", font_big, (255, 80, 80),
                                      (SCREEN_W // 2, lane.lane_top + lane.lane_h // 2))

            if all(not p.alive for p in players):
                d0, d1 = players[0].distance, players[1].distance
                if d0 > d1:
                    winner_text = f"{player_names[0]} Wins!"
                elif d1 > d0:
                    winner_text = f"{player_names[1]} Wins!"
                else:
                    winner_text = "It's a tie!"

                overlay = pygame.Surface((SCREEN_W, SCREEN_H), pygame.SRCALPHA)
                overlay.fill((0, 0, 0, 160))
                canvas.blit(overlay, (0, 0))
                draw_text_center(canvas, "GAME OVER", font_big, WHITE, (SCREEN_W // 2, SCREEN_H // 2 - 90))
                draw_text_center(canvas, winner_text, font_med, (255, 220, 90), (SCREEN_W // 2, SCREEN_H // 2 - 45))
                draw_text_center(
                    canvas,
                    f"{player_names[0]}: {int(d0)} m, {players[0].coins}c    "
                    f"{player_names[1]}: {int(d1)} m, {players[1].coins}c",
                    font_med, WHITE, (SCREEN_W // 2, SCREEN_H // 2 + 5))
                draw_text_center(canvas, "Press R -- or both buttons -- to play again", font_small, WHITE,
                                  (SCREEN_W // 2, SCREEN_H // 2 + 55))
                # Both lanes ran this seed, so it identifies the course both players
                # raced -- enough to reference or rerun a round later.
                draw_text_center(canvas, f"seed {round_seed}", font_small, (150, 150, 160),
                                  (SCREEN_W // 2, SCREEN_H // 2 + 88))

            hint = "DAQ: connected" if daq.available else "DAQ: not found (keyboard-only)"
            canvas.blit(font_small.render(hint, True, (160, 160, 160)), (SCREEN_W - 260, SCREEN_H - 28))
            canvas.blit(font_small.render(f"F11: {DISPLAY_MODE_LABELS[display_mode]}", True,
                                           (130, 130, 140)), (16, SCREEN_H - 28))

        draw_high_scores(canvas, font_small, font_small, high_scores)

        present(window, canvas)

    daq.close()
    pygame.quit()
    sys.exit()


if __name__ == "__main__":
    main()
