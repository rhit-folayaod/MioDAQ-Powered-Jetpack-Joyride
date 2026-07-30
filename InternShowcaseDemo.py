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
    R:   once both players are down, returns to name entry to play again
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

# Sprite art: cropped from the "Dan the Man" / Jetpack Joyride Event asset packs
# (assets/player.png, assets/missile.png). Loaded and scaled once in main().
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

WHITE = (240, 240, 240)
BLACK = (15, 15, 15)
BG = (25, 28, 38)
LANE_BG = [(35, 40, 55), (30, 34, 48)]
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

    def rect(self):
        return pygame.Rect(PLAYER_X - PLAYER_SIZE // 2, int(self.y - PLAYER_SIZE // 2),
                            PLAYER_SIZE, PLAYER_SIZE)

    def update(self, dt, thrust_held, scroll_speed):
        if not self.alive:
            return

        just_pressed = thrust_held and not self._was_held
        self._was_held = thrust_held
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
            if not thrust_held:
                self.fuel = min(FUEL_MAX, self.fuel + FUEL_REGEN_RATE * dt)
                if self.fuel >= FUEL_MAX:
                    self.press_streak = 0  # fully rested -- next press starts a new streak

        self.vy += accel * dt
        self.vy = max(-MAX_RISE_SPEED, min(MAX_FALL_SPEED, self.vy))
        self.y += self.vy * dt

        top = self.lane_top + PLAYER_SIZE / 2
        bottom = self.lane_top + self.lane_h - PLAYER_SIZE / 2
        if self.y < top:
            self.y, self.vy = top, 0.0
        elif self.y > bottom:
            self.y, self.vy = bottom, 0.0

        self.distance += scroll_speed * dt

    def kill(self):
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

    def update(self, dt, alive):
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

    def hazards(self):
        """All obstacle + missile rects in this lane, for collision checks."""
        for obs in self.obstacles:
            yield obs.rect()
        for m in self.missiles:
            yield m.rect()


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


def anim_frame(frames, t, fps, pingpong=False):
    """Picks the current frame surface from `frames` given clock time `t` and playback `fps`."""
    n = len(frames)
    if n == 1:
        return frames[0]
    if pingpong:
        period = 2 * (n - 1)
        i = int(t * fps) % period
        i = i if i < n else period - i
    else:
        i = int(t * fps) % n
    return frames[i]


def draw_flame(surface, tip, t):
    """Small flickering triangle flame trailing downward from `tip`, animated by clock time `t`."""
    wobble = math.sin(t * 26) * 3
    length = 14 + math.sin(t * 18) * 4
    x, y = tip
    pts = [(x - 6, y), (x + 6, y), (x + wobble, y + length)]
    color = THRUST_COLOR if int(t * 16) % 2 == 0 else (255, 170, 60)
    pygame.draw.polygon(surface, color, pts)


def draw_player(surface, p, t, run_frames, jetpack_frames):
    """Cycles through the running (grounded) or jetpack (thrusting) animation, facing right."""
    if p.alive and p.thrusting:
        sprite = anim_frame(jetpack_frames, t, JETPACK_FPS, pingpong=True)
    else:
        sprite = anim_frame(run_frames, t, RUN_FPS)
    img_rect = sprite.get_rect(center=(PLAYER_X, int(p.y)))
    if p.alive and p.thrusting:
        flame_tip = (img_rect.left + img_rect.width * 0.32, img_rect.bottom - 3)
        draw_flame(surface, flame_tip, t)
    surface.blit(sprite, img_rect)
    if not p.alive:
        dim = pygame.Surface(img_rect.size, pygame.SRCALPHA)
        dim.fill((110, 110, 120, 255))
        surface.blit(dim, img_rect.topleft, special_flags=pygame.BLEND_RGB_MULT)


def draw_obstacle(surface, obs, sprite):
    """Laser-hazard sprite, stretched to fill this obstacle's own (randomized) box."""
    rect = obs.rect()
    img = pygame.transform.scale(sprite, (rect.width, rect.height))
    surface.blit(img, rect.topleft)


def draw_missile(surface, m, sprite):
    """Missile sprite -- art already noses left, matching the missile's right-to-left travel."""
    img_rect = sprite.get_rect(center=m.rect().center)
    surface.blit(sprite, img_rect)


def load_high_scores():
    if not os.path.exists(SCORES_PATH):
        return []
    try:
        with open(SCORES_PATH) as f:
            data = json.load(f)
        return [(d["name"], d["score"]) for d in data][:MAX_HIGH_SCORES]
    except Exception as exc:
        print(f"Could not load high scores ({exc}) -- starting with an empty board.")
        return []


def save_high_scores(scores):
    try:
        with open(SCORES_PATH, "w") as f:
            json.dump([{"name": n, "score": s} for n, s in scores], f, indent=2)
    except Exception as exc:
        print(f"Could not save high scores: {exc}")


def add_high_scores(scores, entries):
    """Merges new (name, score) entries in, re-sorts, trims to the top N, and persists."""
    merged = scores + entries
    merged.sort(key=lambda entry: entry[1], reverse=True)
    merged = merged[:MAX_HIGH_SCORES]
    save_high_scores(merged)
    return merged


def draw_high_scores(surface, font_header, font_row, scores):
    """Top-5 leaderboard, right-aligned in the top-right corner."""
    right_x = SCREEN_W - 16
    y = 14
    header = font_header.render("TOP 5", True, WHITE)
    surface.blit(header, (right_x - header.get_width(), y))
    y += 26
    if not scores:
        text = font_row.render("no runs yet", True, (150, 150, 160))
        surface.blit(text, (right_x - text.get_width(), y))
        return
    for i, (name, score) in enumerate(scores, start=1):
        text = font_row.render(f"{i}. {name} - {int(score)}m", True, WHITE)
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

    missile_sprite = load_scaled_sprite(os.path.join(ASSET_DIR, "missile.png"), MISSILE_H + 4)
    obstacle_sprite = pygame.image.load(os.path.join(ASSET_DIR, "obstacle.png")).convert_alpha()

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
                elif state == "playing" and event.key == pygame.K_r and all(not p.alive for p in players):
                    name_inputs = [player_names[0], player_names[1]]  # prefilled, quick restart
                    active_field = 0
                    state = "enter_names"
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
                lanes[i].update(dt, p.alive)
                if p.alive:
                    p_rect = p.rect()
                    if any(p_rect.colliderect(hazard) for hazard in lanes[i].hazards()):
                        p.kill()

            daq.set_leds(thrust[0] and players[0].alive, thrust[1] and players[1].alive)

            if all(not p.alive for p in players) and not scores_recorded:
                entries = [(player_names[i], players[i].distance) for i in range(2)]
                high_scores = add_high_scores(high_scores, entries)
                scores_recorded = True
        else:
            daq.set_leds(False, False)

        # ---- draw ----
        anim_t = pygame.time.get_ticks() / 1000.0
        screen.fill(BG)

        if state == "enter_names":
            draw_name_entry(screen, font_big, font_med, font_small, name_inputs, active_field)
        else:
            for i, lane in enumerate(lanes):
                pygame.draw.rect(screen, LANE_BG[i], (0, lane.lane_top, SCREEN_W, lane.lane_h))
            pygame.draw.line(screen, BLACK, (0, LANE_H), (SCREEN_W, LANE_H), 2)

            for i, (lane, p) in enumerate(zip(lanes, players)):
                for obs in lane.obstacles:
                    draw_obstacle(screen, obs, obstacle_sprite)
                for m in lane.missiles:
                    draw_missile(screen, m, missile_sprite)

                draw_player(screen, p, anim_t, run_frames[i], jetpack_frames[i])

                screen.blit(font_med.render(f"{player_names[i]}: {int(p.distance)} m", True, WHITE),
                            (16, lane.lane_top + 12))

                # Fuel bar
                bar_x, bar_y, bar_w, bar_h = 16, lane.lane_top + 46, 140, 10
                pygame.draw.rect(screen, FUEL_BAR_EMPTY_COLOR, (bar_x, bar_y, bar_w, bar_h), border_radius=4)
                fill_w = int(bar_w * (p.fuel / FUEL_MAX))
                if fill_w > 0:
                    pygame.draw.rect(screen, FUEL_BAR_COLOR, (bar_x, bar_y, fill_w, bar_h), border_radius=4)

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
                    f"{player_names[0]}: {int(d0)} m    {player_names[1]}: {int(d1)} m",
                    font_med, WHITE, (SCREEN_W // 2, SCREEN_H // 2 + 5))
                draw_text_center(screen, "Press R to play again", font_small, WHITE,
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
