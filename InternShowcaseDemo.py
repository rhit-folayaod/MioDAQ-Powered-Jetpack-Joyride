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
# without being so strong that the first press alone rockets you into an obstacle.
PRESS_BOOST_MULTIPLIERS = [1.2, 1.08, 1.0]

SCROLL_SPEED0 = 260.0  # px/s starting obstacle speed
SCROLL_ACCEL = 4.0     # px/s^2, obstacles speed up over the run
OBSTACLE_MIN_GAP, OBSTACLE_MAX_GAP = 260, 420
OBSTACLE_MIN_W, OBSTACLE_MAX_W = 34, 60
OBSTACLE_MIN_H, OBSTACLE_MAX_H = 50, 120

# Missiles launch from off-screen right and fly straight left (same direction as the
# obstacle scroll), ignoring the player's position entirely (no tracking, that's a
# future feature). They never collide with obstacles -- only with players. No missiles
# for the first 10-15s of a run, and spawn frequency ramps up as scroll_speed climbs.
MISSILE_W, MISSILE_H = 50, 20  # matches the aspect ratio of the missile sprite art
# Added on top of the lane's current scroll_speed (not absolute) so missiles always
# read as clearly faster than the scrolling obstacles, no matter how far into a run.
MISSILE_SPEED_MIN, MISSILE_SPEED_MAX = 240.0, 320.0        # px/s, extra over scroll_speed
MISSILE_SPAWN_MIN, MISSILE_SPAWN_MAX = 2.5, 7.0            # seconds between spawns, per lane (fast/slow difficulty)
MISSILE_START_DELAY_MIN, MISSILE_START_DELAY_MAX = 10.0, 15.0  # seconds before the first missile of a run
MISSILE_DIFFICULTY_RANGE = 400.0  # scroll_speed increase (px/s) over which spawn gap ramps from MAX to MIN

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

# Picking up a vehicle token detonates every obstacle/missile/seeker currently in that
# lane (Lane.detonate_hazards()) -- doesn't pause or end the run, just clears the
# immediate area so the reward isn't immediately undone by whatever's already in flight.
EXPLOSION_DURATION = 0.45  # seconds an individual blast VFX plays for
EXPLOSION_MAX_RADIUS = 34

# Coins: small, frequent, non-hazard collectibles -- track a running count per player,
# separate from the distance score. Uses assets/coin_ni_64.png.
COIN_W, COIN_H = 20, 20
COIN_SPAWN_MIN, COIN_SPAWN_MAX = 1.1, 2.3

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


class Obstacle:
    __slots__ = ("x", "y", "w", "h")

    def __init__(self, x, y, w, h):
        self.x, self.y, self.w, self.h = x, y, w, h

    def rect(self):
        return pygame.Rect(int(self.x), int(self.y), int(self.w), int(self.h))


class Missile:
    __slots__ = ("x", "y", "w", "h", "vx")

    def __init__(self, x, y, w, h, vx):
        self.x, self.y, self.w, self.h, self.vx = x, y, w, h, vx

    def rect(self):
        return pygame.Rect(int(self.x), int(self.y), int(self.w), int(self.h))


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


class Lane:
    """Owns one player's independent obstacle/missile streams, so each run is self-contained."""

    def __init__(self, lane_top, lane_h):
        self.lane_top = lane_top
        self.lane_h = lane_h
        self.rng = random.Random()
        self.reset()

    def reset(self):
        self.obstacles = []
        self.next_spawn_x = float(SCREEN_W + 100)
        self.scroll_speed = SCROLL_SPEED0
        self.missiles = []
        # Grace period before the first missile of a run -- no ramp-up applied yet.
        self.next_missile_in = self.rng.uniform(MISSILE_START_DELAY_MIN, MISSILE_START_DELAY_MAX)
        self.seekers = []
        self.next_seeker_in = self.rng.uniform(SEEKER_START_DELAY_MIN, SEEKER_START_DELAY_MAX)
        self.tokens = []
        self.next_token_in = self.rng.uniform(TOKEN_SPAWN_MIN, TOKEN_SPAWN_MAX)
        self.coins = []
        self.next_coin_in = self.rng.uniform(COIN_SPAWN_MIN, COIN_SPAWN_MAX)
        self.scientists = []
        # Own spawn timer, independent of the obstacle/missile/coin ones -- no start
        # delay, since a scientist is a reward rather than a hazard to ramp up to.
        self.next_scientist_in = self.rng.uniform(SCIENTIST_SPAWN_MIN, SCIENTIST_SPAWN_MAX)
        self.explosions = []
        self.popups = []

    def _spawn(self):
        w = self.rng.uniform(OBSTACLE_MIN_W, OBSTACLE_MAX_W)
        h = self.rng.uniform(OBSTACLE_MIN_H, OBSTACLE_MAX_H)
        y = self.rng.uniform(self.lane_top, self.lane_top + self.lane_h - h)
        self.obstacles.append(Obstacle(self.next_spawn_x, y, w, h))
        self.next_spawn_x += w + self.rng.uniform(OBSTACLE_MIN_GAP, OBSTACLE_MAX_GAP)

    def _next_missile_gap(self):
        # Gap shrinks from MISSILE_SPAWN_MAX towards MISSILE_SPAWN_MIN as scroll_speed
        # climbs, so missiles come more often the faster/harder the run gets.
        ramp = (self.scroll_speed - SCROLL_SPEED0) / MISSILE_DIFFICULTY_RANGE
        ramp = min(1.0, max(0.0, ramp))
        base_gap = MISSILE_SPAWN_MAX - ramp * (MISSILE_SPAWN_MAX - MISSILE_SPAWN_MIN)
        return base_gap * self.rng.uniform(0.85, 1.15)

    def _spawn_missile(self):
        y = self.rng.uniform(self.lane_top, self.lane_top + self.lane_h - MISSILE_H)
        # Speed is scroll_speed-relative, not absolute -- otherwise once scroll_speed
        # (which grows unbounded via SCROLL_ACCEL) exceeds the missile's fixed speed,
        # missiles would visually crawl backwards relative to the scrolling obstacles.
        speed = self.scroll_speed + self.rng.uniform(MISSILE_SPEED_MIN, MISSILE_SPEED_MAX)
        self.missiles.append(Missile(float(SCREEN_W), y, MISSILE_W, MISSILE_H, -speed))
        self.next_missile_in = self._next_missile_gap()

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
            blockers = [o.rect().inflate(20, 20) for o in self.obstacles]
            if not any(candidate.colliderect(b) for b in blockers):
                self.tokens.append(VehicleToken(spawn_x, y, TOKEN_W, TOKEN_H, kind))
                break
        self.next_token_in = self.rng.uniform(TOKEN_SPAWN_MIN, TOKEN_SPAWN_MAX)

    def _spawn_coin(self):
        spawn_x = float(SCREEN_W + 20)
        for _ in range(5):
            y = self.rng.uniform(self.lane_top, self.lane_top + self.lane_h - COIN_H)
            candidate = pygame.Rect(int(spawn_x), int(y), COIN_W, COIN_H)
            blockers = ([o.rect().inflate(16, 16) for o in self.obstacles]
                        + [t.rect().inflate(16, 16) for t in self.tokens])
            if not any(candidate.colliderect(b) for b in blockers):
                self.coins.append(Coin(spawn_x, y, COIN_W, COIN_H))
                break
        self.next_coin_in = self.rng.uniform(COIN_SPAWN_MIN, COIN_SPAWN_MAX)

    def _spawn_scientist(self):
        # Ground-anchored: unlike obstacles/coins the y isn't randomized at all -- the
        # collision box always sits flush on the lane floor (which is exactly where a
        # grounded player's own box sits, so a player running along the floor connects).
        y = float(self.lane_top + self.lane_h - SCIENTIST_H)
        spawn_x = float(SCREEN_W + 30)
        candidate = pygame.Rect(int(spawn_x), int(y), SCIENTIST_W, SCIENTIST_H)
        # Same light-touch check the coin/token spawns use -- keeps a scientist from
        # appearing already embedded in a ground-level obstacle.
        blockers = [o.rect().inflate(20, 20) for o in self.obstacles]
        if not any(candidate.colliderect(b) for b in blockers):
            self.scientists.append(Scientist(spawn_x, y, SCIENTIST_W, SCIENTIST_H))
        self.next_scientist_in = self.rng.uniform(SCIENTIST_SPAWN_MIN, SCIENTIST_SPAWN_MAX)

    def detonate_hazards(self):
        """Clears every obstacle/missile/seeker in the lane (triggered by a vehicle
        pickup) and drops an Explosion VFX at each one's former position. Doesn't touch
        spawn timers -- future hazards still arrive on schedule, this just clears what's
        already in flight so the pickup isn't immediately undone."""
        for obs in self.obstacles:
            self.explosions.append(Explosion(*obs.rect().center))
        for m in self.missiles:
            self.explosions.append(Explosion(*m.rect().center))
        for s in self.seekers:
            self.explosions.append(Explosion(*s.rect().center))
        self.obstacles = []
        self.missiles = []
        self.seekers = []

    def update(self, dt, alive, target_y):
        if not alive:
            return  # freeze this lane once its player is out
        self.scroll_speed += SCROLL_ACCEL * dt
        dx = self.scroll_speed * dt
        for obs in self.obstacles:
            obs.x -= dx
        self.next_spawn_x -= dx
        self.obstacles = [o for o in self.obstacles if o.x + o.w > -20]
        while self.next_spawn_x < SCREEN_W + 300:
            self._spawn()

        self.next_missile_in -= dt
        if self.next_missile_in <= 0:
            self._spawn_missile()
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
            self._spawn_coin()
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
        """All obstacle + missile + launched-seeker rects in this lane, for collision checks.

        Scientists are deliberately *not* included -- this feeds the kill logic, and they
        are a reward (see knock_over_scientists for their separate collision path).
        """
        for obs in self.obstacles:
            yield obs.rect()
        for m in self.missiles:
            yield m.rect()
        for s in self.seekers:
            if s.state == "seeking":
                yield s.rect()

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


def draw_obstacle(surface, obs, sprite):
    """Laser-hazard sprite, stretched to fill this obstacle's own (randomized) box."""
    rect = obs.rect()
    img = pygame.transform.scale(sprite, (rect.width, rect.height))
    surface.blit(img, rect.topleft)


def draw_missile(surface, m, sprite):
    """Missile sprite -- art already noses left, matching the missile's right-to-left travel."""
    img_rect = sprite.get_rect(center=m.rect().center)
    surface.blit(sprite, img_rect)


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


def draw_name_entry(surface, font_big, font_med, font_small, names, active_field):
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


def main():
    pygame.init()
    pygame.display.set_caption("Two-Player Jetpack Joyride Demo")
    screen = pygame.display.set_mode((SCREEN_W, SCREEN_H))
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

    # Scientists are world objects (like obstacles and coins), so unlike the player frames
    # they aren't tinted per lane -- both lanes share one set.
    scientist_run_frames = [load_scaled_sprite(os.path.join(ASSET_DIR, f"manager_run_{i:02d}.png"),
                                                SCIENTIST_SPRITE_H)
                             for i in range(SCIENTIST_RUN_FRAME_COUNT)]
    scientist_down_frames = [load_scaled_sprite(os.path.join(ASSET_DIR, f"manager_down_{i:02d}.png"),
                                                 SCIENTIST_SPRITE_H)
                              for i in range(SCIENTIST_DOWN_FRAME_COUNT)]

    missile_sprite = load_scaled_sprite(os.path.join(ASSET_DIR, "missile.png"), MISSILE_H + 4)
    obstacle_sprite = pygame.image.load(os.path.join(ASSET_DIR, "obstacle.png")).convert_alpha()

    # Seeking missiles: a duplicate of the straight-missile art (assets/missile_seeker.png),
    # tinted per state so the blink telegraph (orange) vs. armed/launched (red) distinction
    # from the old placeholder chevron still reads, just on a real sprite now.
    seeker_sprite_base = load_scaled_sprite(os.path.join(ASSET_DIR, "missile_seeker.png"), SEEKER_H + 6)
    seeker_sprite_telegraph = tinted_sprite(seeker_sprite_base, lighten((255, 140, 60), 0.35))
    seeker_sprite_armed = tinted_sprite(seeker_sprite_base, lighten((255, 60, 60), 0.35))

    background_sprite = pygame.image.load(os.path.join(ASSET_DIR, "background.png")).convert()
    background_sprite = pygame.transform.smoothscale(background_sprite, (SCREEN_W, SCREEN_H))
    # Translucent per-lane tint over the background -- keeps the two lanes visually
    # separated and obstacles/text readable without fully hiding the artwork underneath.
    lane_overlays = []
    for color in LANE_BG:
        overlay = pygame.Surface((SCREEN_W, LANE_H), pygame.SRCALPHA)
        overlay.fill((*color, 165))
        lane_overlays.append(overlay)

    daq = DaqIO()

    lanes = [Lane(0, LANE_H), Lane(LANE_H, LANE_H)]
    players = [Player(0, 0, LANE_H), Player(1, LANE_H, LANE_H)]
    key_map = [pygame.K_SPACE, pygame.K_UP]

    def reset_game():
        for lane in lanes:
            lane.reset()
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
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
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
                    if any(p_rect.colliderect(hazard) for hazard in lanes[i].hazards()):
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
        screen.blit(background_sprite, (0, 0))

        if state == "enter_names":
            dim = pygame.Surface((SCREEN_W, SCREEN_H), pygame.SRCALPHA)
            dim.fill((0, 0, 0, 120))
            screen.blit(dim, (0, 0))
            draw_name_entry(screen, font_big, font_med, font_small, name_inputs, active_field)
        else:
            for i, lane in enumerate(lanes):
                screen.blit(lane_overlays[i], (0, lane.lane_top))
            pygame.draw.line(screen, BLACK, (0, LANE_H), (SCREEN_W, LANE_H), 2)

            for i, (lane, p) in enumerate(zip(lanes, players)):
                for obs in lane.obstacles:
                    draw_obstacle(screen, obs, obstacle_sprite)
                for sci in lane.scientists:
                    draw_scientist(screen, sci, scientist_run_frames, scientist_down_frames)
                for m in lane.missiles:
                    draw_missile(screen, m, missile_sprite)
                for s in lane.seekers:
                    draw_seeking_missile(screen, s, seeker_sprite_telegraph, seeker_sprite_armed)
                for c in lane.coins:
                    draw_coin(screen, c, anim_t, coin_sprite)
                for tk in lane.tokens:
                    draw_vehicle_token(screen, tk, anim_t, vehicle_token_sprite)
                for e in lane.explosions:
                    draw_explosion(screen, e)
                for pop in lane.popups:
                    draw_score_popup(screen, pop, font_med)

                if p.alive and p.vehicle_active and p.vehicle_kind == "profit_bird":
                    draw_vehicle_player(screen, p, anim_t, profit_bird_frames, 1.0)
                elif p.alive and p.vehicle_active and p.vehicle_kind == "lil_stomper":
                    draw_vehicle_player(screen, p, anim_t, stomper_frames, STOMPER_FPS)
                else:
                    draw_player(screen, p, anim_t, run_frames[i], jetpack_frames[i], death_frames)

                screen.blit(font_med.render(f"{player_names[i]}: {int(p.distance)} m   {p.coins} coins", True, WHITE),
                            (16, lane.lane_top + 12))

                # Fuel bar -- shows Lil' Stomper's float-assist charge while that vehicle
                # is active (its own small tank), otherwise the base jetpack fuel.
                if p.vehicle_active and p.vehicle_kind == "lil_stomper":
                    fuel_frac = p.stomper_float_fuel / STOMPER_FLOAT_FUEL_MAX
                else:
                    fuel_frac = p.fuel / FUEL_MAX
                bar_x, bar_y, bar_w, bar_h = 16, lane.lane_top + 46, 140, 10
                pygame.draw.rect(screen, FUEL_BAR_EMPTY_COLOR, (bar_x, bar_y, bar_w, bar_h), border_radius=4)
                fill_w = int(bar_w * fuel_frac)
                if fill_w > 0:
                    pygame.draw.rect(screen, FUEL_BAR_COLOR, (bar_x, bar_y, fill_w, bar_h), border_radius=4)

                # Vehicle-mode timer bar, only shown while active
                if p.vehicle_active:
                    vbar_x, vbar_y, vbar_w, vbar_h = 16, lane.lane_top + 64, 140, 8
                    v_color = VEHICLE_COLORS[p.vehicle_kind]
                    pygame.draw.rect(screen, FUEL_BAR_EMPTY_COLOR, (vbar_x, vbar_y, vbar_w, vbar_h), border_radius=4)
                    vfill_w = int(vbar_w * (p.vehicle_timer / VEHICLE_DURATION))
                    if vfill_w > 0:
                        pygame.draw.rect(screen, v_color, (vbar_x, vbar_y, vfill_w, vbar_h), border_radius=4)
                    label = font_small.render(VEHICLE_LABELS[p.vehicle_kind], True, v_color)
                    screen.blit(label, (vbar_x + vbar_w + 8, vbar_y - 6))

                if not p.alive:
                    draw_text_center(screen, f"{player_names[i]} Down!", font_big, (255, 80, 80),
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
                screen.blit(overlay, (0, 0))
                draw_text_center(screen, "GAME OVER", font_big, WHITE, (SCREEN_W // 2, SCREEN_H // 2 - 90))
                draw_text_center(screen, winner_text, font_med, (255, 220, 90), (SCREEN_W // 2, SCREEN_H // 2 - 45))
                draw_text_center(
                    screen,
                    f"{player_names[0]}: {int(d0)} m, {players[0].coins}c    "
                    f"{player_names[1]}: {int(d1)} m, {players[1].coins}c",
                    font_med, WHITE, (SCREEN_W // 2, SCREEN_H // 2 + 5))
                draw_text_center(screen, "Press R -- or both buttons -- to play again", font_small, WHITE,
                                  (SCREEN_W // 2, SCREEN_H // 2 + 55))

            hint = "DAQ: connected" if daq.available else "DAQ: not found (keyboard-only)"
            screen.blit(font_small.render(hint, True, (160, 160, 160)), (SCREEN_W - 260, SCREEN_H - 28))

        draw_high_scores(screen, font_small, font_small, high_scores)

        pygame.display.flip()

    daq.close()
    pygame.quit()
    sys.exit()


if __name__ == "__main__":
    main()
