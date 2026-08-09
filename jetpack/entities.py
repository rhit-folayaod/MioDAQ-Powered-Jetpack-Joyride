"""
The things that exist in a lane, and the player.

Each class here owns its own state and the rules for advancing it, and stays
ignorant of how it is drawn and of where its input came from -- `Player.update()`
takes a plain `thrust_held` bool, which is what lets a DAQ button and a keyboard
key be genuinely interchangeable upstream.

The hazards share an informal protocol rather than a base class: anything yielded
by `Lane.hazards()` exposes `collides(player_rect)` and tests itself. That is what
lets a zapper be a line segment while a missile is a box without the kill check in
the game loop having to know the difference.

Most of these use `__slots__`: a lane holds a few hundred of them and they are
rebuilt every round, so the dict-per-instance saving is worth the one line.
"""
import math

import pygame

from .config import *


class Player:
    """One player's flight state, and the three control schemes it can be under.

    The key design fact is the signature of `update()`: it takes a single
    `thrust_held` bool and has no idea whether that came from a DAQ line or a
    keyboard key. Every mechanic here is written against that bool, which is the
    entire reason hardware and keyboard are interchangeable with no adapter layer.

    Vehicles are modelled as modes of this one class rather than subclasses. They
    differ only in how a press maps to vertical motion and where the ceiling sits,
    and a mode that lasts 6.5 seconds and then reverts is far more awkward to
    express by swapping an object's type than by branching on a field.

    Position is vertical-only -- x is fixed at PLAYER_X and the world scrolls past.
    """

    def __init__(self, index, lane_top, lane_h):
        self.index = index
        self.lane_top = lane_top
        self.lane_h = lane_h
        self.color = PLAYER_COLORS[index]
        self.reset()

    def reset(self):
        """Returns the player to a fresh round's starting state.

        Defined once and called from `__init__` too, so there is exactly one place
        that knows the full field list -- a new piece of per-run state added here
        cannot be forgotten at round boundaries, which is the bug this shape exists
        to make impossible.
        """
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
        # Time since the current Profit Bird wing flap started. Seeded at a full cycle
        # (not 0.0) so a freshly-mounted bird is already past the end of the one-shot
        # animation and sits on the glide frame instead of flapping unprompted.
        self.flap_anim_t = PROFIT_BIRD_FLAP_CYCLE

    def rect(self):
        return pygame.Rect(PLAYER_X - PLAYER_SIZE // 2, int(self.y - PLAYER_SIZE // 2),
                            PLAYER_SIZE, PLAYER_SIZE)

    def update(self, dt, thrust_held, scroll_speed):
        """Advances one frame of flight under whichever control scheme is active.

        Three schemes share this method because they share everything after the
        acceleration is chosen -- the velocity clamp, the lane bounds, the ground
        test and distance accrual are identical for all of them. Only the mapping
        from `thrust_held` to acceleration differs:

            base jetpack  hold to accelerate upward, drawing on a 3s tank
            Lil' Stomper  tap for an impulse, hold for a weak limited float
            Profit Bird   each fresh press sets velocity outright, no hold, no fuel

        `just_pressed` is computed from `_was_held` rather than read from pygame,
        because a press *edge* is what two of the three schemes key off and the DAQ
        has no equivalent of a KEYDOWN event -- only a level, sampled per frame.
        Deriving the edge here is what lets both input sources behave identically.
        """
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
            self.flap_anim_t += dt
            if just_pressed:
                self.vy = -PROFIT_BIRD_FLAP_SPEED
                self.flap_anim_t = 0.0  # restart the wing beat on the same frame as the hop
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
    """A straight-flying rocket. No homing -- that is `SeekingMissile`'s job.

    Kept as a distinct hazard from the seeker rather than folded into it with a
    flag: the two differ in spawn timing, telegraph style, art and behaviour, and
    the only thing they share is a rect and a `collides()`.

    `vx` is stored already negated (leftward) and is set at launch from the lane's
    *current* scroll speed plus an offset, never as an absolute -- see
    `Lane._launch_missile` for why that distinction matters.
    """
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
    """A single collectible. Deliberately dumb -- it has no behaviour at all.

    Coins are never spawned individually: `Lane._spawn_coin_formation` lays 5-10 of
    them along a shape, and the shape is what carries the design intent. Keeping the
    coin itself to four numbers is what makes an all-or-nothing formation placement
    check cheap enough to re-roll six times per spawn.
    """
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
