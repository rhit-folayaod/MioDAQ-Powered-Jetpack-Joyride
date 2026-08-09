"""
One player's world.

A `Lane` owns every hazard, collectible and effect on one half of the screen, plus
the independent spawn timers that produce them. Two lanes run side by side and
never interact -- one player dying freezes their lane and leaves the other's run
untouched.

Everything a lane spawns is drawn from `self.rng`, a `random.Random` it owns
outright. That single fact is what makes a fair race possible: seed two lanes
identically and they generate byte-identical courses, so the only variable left
between the two players is skill.
"""
import random

import pygame

from .config import *
from .entities import (Coin, Explosion, Missile, MissileWarning, Scientist,
                       ScorePopup, SeekingMissile, VehicleToken, Zapper)
from .patterns import (COIN_FORMATIONS, ZAPPER_PATTERNS, _pattern_fallback,
                       coin_is_clear, pattern_is_passable)


class Lane:
    """Owns one player's independent zapper/missile streams, so each run is self-contained."""

    def __init__(self, lane_top, lane_h):
        """Builds an empty lane occupying the horizontal band [lane_top, +lane_h).

        The `random.Random()` instance is per-lane and never shared with the global
        `random` module. That is the load-bearing decision in this whole class: a
        lane drawing from the global RNG could not be reproduced, because anything
        else in the process consuming randomness would shift its sequence -- and
        reproducibility is precisely what the fair-race guarantee is built on.
        """
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
        """Seconds until the next missile telegraph, tightening as the run speeds up.

        Difficulty is ramped off `scroll_speed` rather than elapsed time because
        scroll speed is what actually makes a course harder, and it is already the
        one number that grows monotonically through a run. Tying the ramp to it
        means the two never drift apart.

        The final jitter multiplier keeps the cadence from becoming a metronome a
        player can subconsciously count against.
        """
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
        """Constructs the actual missile at the y its telegraph promised.

        Called from `update()` when a warning's timer elapses, not from the spawn
        path -- which is what makes the telegraph safe by construction rather than
        by a flag: until this runs there is no missile object in the lane at all.
        """
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
        """Advances the whole lane one frame: scroll, spawn, move, cull.

        Freezing outright when `alive` is False is what makes two players on one
        screen independent. A dead player's lane stops generating and stops
        scrolling, so their half of the screen holds its final state as a scoreboard
        while the survivor's run continues at full speed, with no shared clock
        between them.

        Every entity family follows the same four beats in the same order --
        decrement timer, spawn if due, move what exists, drop what left the screen.
        The repetition is deliberate: each family has genuinely different movement
        rules (zappers ride the scroll, missiles carry their own velocity, running
        scientists move at a fraction of the scroll), and a generic entity loop
        would need a per-family callback to express that, which is the same code
        with indirection on top.

        `target_y` is the player's current y, needed only by seekers for homing.
        It is passed in rather than the lane holding a Player reference, which keeps
        the lane simulatable with no player at all -- what the seed scorer relies on.
        """
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
