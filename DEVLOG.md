# Dev Log: Two-Player Jetpack Joyride DAQ Demo

A build log for an internal engineering-showcase demo: a two-player,
Jetpack-Joyride-style side-scroller in `pygame`, controlled by an NI DAQ
(physical buttons + LEDs) with automatic keyboard fallback for dev/testing.
Kept mostly to a single file (`InternShowcaseDemo.py`) on purpose, since this
is a demo project, not a shipped product.

This log is written chronologically, in the order the decisions actually got
made (including the wrong turns), since that's more useful for a write-up
than a cleaned-up "final architecture" description.

---

## Phase 1 — Base game over the existing DAQ script

Starting point was a working script that read two buttons on DI and mirrored
them straight to two LEDs on DO using `nidaqmx`, visualized live with
`matplotlib`/`FuncAnimation`. The ask was to keep the DAQ plumbing but drive
a real two-player game loop instead of a plot.

Design decisions for the first pass:
- **Two lanes, stacked vertically**, each player fixed at the same x
  position; obstacles scroll from right to left past them (classic endless
  runner illusion — the "camera" is fixed, the world moves).
- **Flight physics, not jumping**: gravity pulls down every frame; holding
  the button applies upward acceleration. Released early = short hop into a
  fall; held = climb.
- **DAQ on a background thread.** A digital read of 2 lines is fast, but the
  ask was explicit that DAQ polling must never stall rendering, so the
  `DaqIO` class polls buttons and writes LEDs in a small loop on a daemon
  thread, with the game loop just reading the latest cached button state
  each frame (`threading.Lock`-protected).
- **Graceful fallback**: `import nidaqmx` and `nidaqmx.Task()` setup are both
  wrapped in `try/except`, so missing hardware *or* a missing package just
  prints a message and switches to keyboard input, no crash.
- Each player has an **independent collision + independent obstacle lane**,
  so one player going down doesn't stop the other's run. Game only ends once
  both are out, with a shared game-over screen showing both scores.

## Phase 2 — Environment: pygame wouldn't install

`pip install pygame` failed on this machine — turned out to be Python 3.14.6
(very new at the time), and classic `pygame` 2.6.1 has no prebuilt wheel for
`cp314` yet, so pip fell back to a source build that needs MSYS2/pacman
(not present here) and failed immediately.

Fix: installed **`pygame-ce`** instead (the actively-maintained community
fork, drop-in `import pygame`-compatible API) — it already ships a `cp314`
wheel. No code changes needed.

## Phase 3 — Hardware: buttons didn't respond

First live test: keyboard worked, physical buttons did nothing. Root-caused
by testing iteratively:

1. Added a throttled `[DAQ] button1=... button2=...` debug line to the
   background poll loop (prints on every state change + a 2s heartbeat), so
   button presses are directly observable in the terminal without needing to
   trust the game's visual response.
2. Discovered I had "corrected" a line-mapping swap in the original script
   based on a stale comment, when the *actual wiring* matched the original
   (untouched) code. Reverted `BUTTON_LINES`/`LED_LINES` back to the
   originally-tested mapping.
3. Separately found the DO *write order* was physically reversed from the DI
   *read order* (pressing button 1 lit LED 2) — fixed by swapping the write
   order in the poll loop, independent of the line-mapping fix.
4. Per a follow-up ask, buttons became the **sole input whenever a DAQ is
   connected** — keyboard now only works as a fallback when no DAQ is found,
   so a bystander near a keyboard can't hijack the demo mid-showcase.

Lesson: don't "fix" wiring based on a code comment without hardware
evidence — the working code was the source of truth, the comment was stale.

## Phase 4 — Gameplay systems: fuel, press-boost, missiles

Added on top of the base flight physics:

- **Fuel system**: a 3-second tank that only drains while actually
  thrusting; hitting empty cuts thrust even if the button is still held,
  until you let go and it regenerates (~1.5x the drain rate). Gives the
  hold-forever strategy a real cost.
- **Press-boost**: each *fresh* press (release→press edge, not just holding)
  gets a multiplier from `PRESS_BOOST_MULTIPLIERS`, indexed by how many
  presses deep into the current streak you are. First tap in a streak is
  strongest, tapering off, resetting once fuel fully refills. Rewards a
  deliberate first kick, punishes frantic re-tapping.
  - First version (`[1.5, 1.15, 1.0]`) was too strong — an early press could
    rocket the player into an obstacle before they could react. Tuned down
    to `[1.2, 1.08, 1.0]` and separately capped `MAX_RISE_SPEED` below
    `MAX_FALL_SPEED`, so a boosted press accelerates fast but can't exceed a
    safe climb speed. Verified the fix with a headless physics sim before
    trusting it (no display needed to check `vy` over time).
- **Missiles**: separate hazard type from obstacles. Independent per-lane
  spawn timer (not tied to the same right-edge spawn-point logic as
  obstacles), a start delay before the first one of a run, and a spawn-gap
  that shrinks as that lane's `scroll_speed` (difficulty) climbs.
  - **Direction went back and forth more than once.** Worth being honest
    about this in the log: it moved left→right, then a fix flipped it to
    right→left, then a later message was read (in hindsight, ambiguously)
    as asking to flip it back, then it needed flipping again. Landed on:
    missiles spawn off-screen right and travel right-to-left, matching the
    same direction obstacles scroll. Added a quick headless sim (spawn a
    `Lane`, step it for 20 in-game seconds, print spawn time/velocity sign)
    to *verify* direction and timing objectively instead of eyeballing it
    from a screenshot each time.

## Phase 5 — Space and pacing

Feedback: player felt too big for the lane, and an early press could rocket
you into an obstacle before you could react. Changes:
- Window enlarged 960×540 → 1100×640 (lane height 270 → 320px).
- `PLAYER_SIZE` 36 → 32 (collision box).
- Obstacle width/height ranges both trimmed down slightly, so a taller lane
  reliably has more dodge room, not just a bigger box.
- (Combined with the press-boost/rise-speed tuning from Phase 4.)

## Phase 6 — Real sprite art

Swapped the procedural rectangle/polygon placeholders for real art, cropped
from two "Dan the Man" asset sheets (`Mobile - Dan the Man - Playable
Characters - Barry Steakfries.png` and `...Stage Hazards (Jetpack Joyride
Event).png`).

Process, since the sheets are large and not laid out as a clean fixed grid:
- Used `PIL` + `scipy.ndimage.label` to find **connected components** (alpha
  > 10 threshold) in the sheet, which gives precise per-sprite bounding
  boxes without manually eyeballing pixel coordinates.
- For counting "rows" the way a human looking at the sheet would (the user
  referenced specific frames by row/index), plain alpha-gap detection wasn't
  reliable — adjacent frames' hair/fists overlap row bands. Instead rendered
  the sheet at 1.4x with a red grid every 50px and row-index labels overlaid,
  and visually counted rows against that, then re-ran connected-component
  detection scoped to just that row's y-range to get exact per-frame boxes.
- Extracted:
  - `assets/missile.png` — a rocket sprite from the hazards sheet.
  - `assets/obstacle.png` — a two-nozzle vertical laser-beam hazard from the
    same sheet, used as the "barrier" — it's symmetric top/bottom and
    left/right, so no facing-direction concerns, and it stretches cleanly
    via `pygame.transform.scale()` to fit each obstacle's randomized
    width/height at draw time.
  - Player frames — see Phase 7.
- **Facing direction** needed several passes: the run-cycle frames in the
  sheet face left by default; mirrored with `PIL.ImageOps.mirror()` to face
  right (world scrolls left, so the player should face the direction of
  travel). The missile sprite naturally noses left with the flame trailing
  right in the source art, which conveniently already matched its
  right-to-left travel with zero mirroring needed — until Phase 4's
  direction flip-flopping meant it briefly got mirrored and un-mirrored to
  match. Current state: missile noses left (leading edge), matching travel.
- Verified composition by rendering actual game frames **offscreen**
  (`SDL_VIDEODRIVER=dummy`, `pygame.image.save()`) rather than trusting code
  review alone — catches scaling/positioning/tint bugs that are only
  obvious visually.

## Phase 7 — Character animation

Replaced the single static player sprite with two real animation sets, both
extracted from the Barry sheet and mirrored to face right:

- **Running** (`run_00.png`–`run_09.png`): the 10-frame row-1 run cycle,
  played at 10fps whenever the player isn't actively thrusting.
- **Jetpack** (`jetpack_00.png`–`jetpack_04.png`): the sheet has a "turn
  around" sequence (around row 6) where the character rotates from a clean
  side profile (jetpack fully visible) to a front-on pose (jetpack mostly
  hidden behind him). Only the first 5 frames of that sequence were kept —
  the later frames hide the jetpack too much to read well at this sprite
  size — and those 5 are **ping-ponged** (played forward then backward)
  rather than looped, for a subtle hover-wobble instead of a jarring reset.
- Frame selection is time-based and stateless (`anim_frame(frames, t, fps,
  pingpong=...)`), not stored per-player — consistent with how the flame
  flicker already worked, and one less thing to reset when a run restarts.

**Bug**: Player 2's color-differentiation tint made the whole sprite read as
solid red. Cause: multiplying every pixel by a fully-saturated tint color
(`BLEND_RGB_MULT` with raw `(255, 150, 80)`) crushes shading toward that
color instead of subtly recoloring it. Fixed by lightening the tint color
55% toward white before multiplying (`lighten()` helper) — preserves much
more of the original shading, reads as a warm variant instead of a flat
recolor.

## Phase 8 — Missiles "floating"/going slow

Report: missiles sometimes looked very slow, almost floating, and it looked
like obstacles were somehow interfering with them.

They aren't — obstacles and missiles never collide with each other in the
code; the only collision check that exists is player-vs-hazard. The actual
bug: missile speed was a **fixed** `240-320 px/s`, while `scroll_speed`
(what obstacles move at) starts at `260` and grows *unbounded* over a run
via `SCROLL_ACCEL`. Roughly 20s into a run, `scroll_speed` alone could
already exceed a missile's max possible speed — at that point the missile is
still moving left in absolute terms, but *slower* than the world is
scrolling left past it, so relative to the obstacles it visually crawls or
even drifts backward. That reads exactly like "floating."

Fix: missile speed is now `scroll_speed + uniform(MISSILE_SPEED_MIN,
MISSILE_SPEED_MAX)` at spawn time, i.e. the existing 240-320 range is added
*on top of* the lane's current scroll speed rather than used as an absolute
value. Verified with a headless sim stepping a `Lane` through 90 in-game
seconds and printing `missile_speed - scroll_speed` at each spawn — stayed
in the intended 240-320 range the entire time instead of shrinking toward
(or past) zero.

## Phase 9 — Player names and a persistent leaderboard

Added a proper front-end to the demo instead of jumping straight into play:

- **Name entry screen**: a new `"enter_names"` state, separate from
  `"playing"`. Two text fields (one per player, outlined in that player's
  color, active field highlighted with a blinking cursor). Typing uses
  pygame's `TEXTINPUT` event (handles shift/layout automatically) rather
  than reconstructing text from `KEYDOWN`; `BACKSPACE` deletes, `TAB` swaps
  the active field, `ENTER` confirms and starts the round. Blank fields
  default to "Player 1"/"Player 2".
  - This is a deliberate, explicit exception to the "buttons are the
    primary input" rule from Phase 3 — typing a name on two momentary
    buttons isn't practical, so name entry always uses the keyboard
    regardless of whether a DAQ is connected. Gameplay itself is unaffected;
    buttons still take over as soon as the round starts.
- Restarting (`R`, once both players are down) returns to the name-entry
  screen instead of jumping straight back into a new run, but **prefills**
  both fields with the previous names — same players can restart with one
  keystroke (`ENTER`), new players can clear and retype.
- **Winner**: since `distance` only ever accumulates while a player is
  alive (frozen the instant they die), it's already exactly equivalent to
  "who survived longest" — no separate time-tracking needed, just compare
  final `distance` between the two players once both are down.
- **Persistent top-5 leaderboard**: every completed round appends both
  players' `(name, distance)` as separate entries (not just the winner) to
  a small JSON file (`high_scores.json`, sibling to the script), re-sorted
  descending and trimmed to 5 entries on each save. Rendered as a fixed
  panel in the top-right corner across every screen (name entry, gameplay,
  and game-over), so it persists across program restarts, not just
  in-session.
- Recording is guarded by a one-shot `scores_recorded` flag set the instant
  both players are down, cleared again when a new round actually starts —
  otherwise every subsequent frame of the (static) game-over screen would
  re-append the same scores.

## Phase 10 — Seeking missiles, vehicle power-ups, and coins

Three new systems added on top of the existing `Lane`/`Player`/`Missile`/`Obstacle`
structure, each following the same "Lane owns its own spawn timer" pattern the
straight missiles already established.

- **Seeking missiles** (`SeekingMissile`, new class alongside `Missile`): spawn on
  their own per-lane timer, separate from the straight-missile timer, so both types
  can appear in the same run without dogpiling the player. Two deliberate difficulty
  choices to keep them a rarer, later threat: `SEEKER_START_DELAY` (22-30s) is later
  than the straight-missile start delay (10-15s), and `SEEKER_SPAWN_MIN/MAX` (9-18s)
  is a noticeably longer gap than straight missiles (2.5-7s), also ramping with
  `scroll_speed` the same way straight missiles already do.
  - **Telegraph**: blinks in place 3 times (visible/dim toggle, ~0.27s per half-blink)
    before launching. One deliberate deviation from the literal ask: it spawns flush
    against the right edge of the screen (`SCREEN_W - SEEKER_W`) rather than fully
    off-screen like straight missiles — spawning it off-screen would make the blink
    warning invisible until it's already moving, defeating the point of a "fair
    warning." It's also excluded from `Lane.hazards()` entirely while still
    telegraphing, so it can't collide with anything until it actually launches.
  - **Homing**: once launched, steers `vy` toward the player's current y with a
    turn-rate clamp (`SEEKER_TURN_RATE`) rather than snapping straight at them —
    verified headlessly (see below) that a wide-enough turn radius means it
    converges smoothly on the target without overshoot/oscillation, and its vertical
    speed is capped (`SEEKER_MAX_VY = 220`) well under the player's own vertical
    range (rise/fall speeds 480-640), so a last-second dodge can always outrun it.
- **Vehicle power-up tokens** (`VehicleToken`, simplified Profit Bird / Lil' Stomper):
  spawn as collectible orbs on their own timer, with a small placement check against
  current obstacle positions so they don't land on top of one. Picking one up grants
  `VEHICLE_DURATION` (6.5s) of invulnerability and a distinct player recolor (see
  below). Matching how vehicles work in the real game: a hazard hit while a vehicle
  is active knocks the player out of vehicle mode instead of killing them (one hit
  "absorbed"), and the mode also ends early if the timer runs out first.
  - Added a short **hit-grace window** (`HIT_GRACE_PERIOD = 0.5s`) after a vehicle
    absorbs a hit — not explicitly requested, but without it a wide hazard could hit
    the player again on the very next frame (before they'd had any chance to react)
    and kill them immediately after losing the vehicle, which felt like it defeated
    the "absorbs a hit" promise. Verified headlessly that an immediate repeat hit
    during grace doesn't kill, and a hit after grace expires (with no vehicle left)
    does.
  - Simple physics twists per vehicle, since both were easy to add without touching
    the core flight model: **Profit Bird** multiplies distance accrual by 1.5x while
    active (a "speed boost" that doesn't change actual collision physics); **Lil'
    Stomper** clamps how high the player can climb (`STOMPER_CEILING_FRAC = 0.55` of
    the lane) for a "ground-hugging" feel.
  - On-screen indicator: a second small timer bar under the fuel bar, styled the same
    way, showing time remaining and colored/labeled per vehicle kind.
- **Coins**: small, frequent collectibles (`Coin`) spawned on their own timer,
  checked against current obstacle *and* token positions at spawn time so they don't
  land on top of either. Tracked as a running `Player.coins` counter, separate from
  `distance` — coins never factor into the distance score.
  - High-score storage extended from `(name, score)` to `(name, score, coins)` per
    the "extend the existing entries" option in the ask (simpler than a second
    leaderboard for a demo). `distance` still determines rank/sort order; `coins`
    just rides along and is rendered next to it (`"1. Rene - 5735m, 12c"`). Loader
    uses `d.get("coins", 0)` so the pre-existing `high_scores.json` (saved before
    this phase, no `"coins"` key) still loads without a migration step.

**Verification**: rather than trusting a live playtest to exercise all three systems
(a seeker alone takes 22-30s+ to even appear), wrote a headless sim in the same
style as Phases 4/8 — step a `Lane`/`Player` through 40-45 in-game seconds with
`SDL_VIDEODRIVER=dummy`, and directly drive `SeekingMissile.update()` /
`Player.activate_vehicle()` / `Player.on_hazard_hit()` in isolation. Confirmed: the
blink telegraph fires exactly 3 full blinks (6 toggles) and holds position the whole
time; the seeker's homing velocity converges on the target without runaway
oscillation; a vehicle absorbs exactly one hit within its grace window and a
follow-up hit after grace expires is fatal; and the Stomper ceiling clamp actually
caps climb height. Also rendered one frame of every new entity type off-screen to
catch draw-time errors before trusting it visually.

**New sprite assets needed** (all three are currently pygame-primitive placeholders,
flagged in-code with `PLACEHOLDER` docstrings on `draw_seeking_missile`,
`draw_vehicle_token`, and `draw_coin`):
- A seeking-missile sprite (currently a solid-color triangle/chevron).
- Profit Bird / Lil' Stomper token sprites (currently pulsing colored circles —
  gold and light-blue respectively).
- A coin sprite (currently a solid yellow circle).
- Vehicle-mode player recolor currently reuses the existing run/jetpack frames with
  a flat tint (same `tinted_sprite()` trick as the player-2 recolor from Phase 7)
  rather than a real vehicle sprite swap — fine as a placeholder, but a real Profit
  Bird / Lil' Stomper character swap would read much better.
  - **Update, Phase 11**: vehicle token, regular coin, Profit Bird, and a proper
    death animation all got real art. Seeking missiles are the only placeholder
    left standing.

---

## Phase 11 — Real sprites for tokens, coins, Profit Bird, and death

Swapped four more placeholders for real art, sourced (per direction) from three more
"Dan the Man" sheets already dropped in the repo root, plus a standalone coin icon:

- **assets/coin_ni_64.png** → the regular per-run `Coin` collectible. Already a clean
  standalone 64x64 icon, no extraction needed — just loaded and scaled like everything
  else. Given a subtle per-coin bob (`sin(t*4 + coin.x*0.05)`) so a run of several
  coins in a row doesn't read as static.
- **"Dan the Man - Miscellaneous - Coin Counter.png", row 1 sprite 1** → the in-lane
  `VehicleToken` pickup icon (`assets/vehicle_token.png`), shared by both Profit Bird
  and Lil' Stomper tokens rather than needing two separate icons.
- **"Dan the Man - Miscellaneous - Jetpack Joyride Event Cutscenes.png"** → the
  Profit Bird rider sprite (`assets/profit_bird.png`). The sheet actually has two
  bird graphics; picked the one with a helmeted rider visible (bbox `(387, 154, 481,
  219)`) over the plain unmanned bird icon near the top of the sheet, since that's
  the one that's actually "the profit bird guy." Source faces left — mirrored with
  `PIL.ImageOps.mirror()` to match the existing right-facing convention (world
  scrolls left, character faces direction of travel, same reasoning as Phase 6).
- **"Dan the Man - Miscellaneous - Charred Death Animation.png", row 1, frames 1-4**
  → `assets/death_00.png..death_03.png`, a black-silhouette-engulfed-in-flame burst.
  Row 1 (the flaming humanoid, arms-out silhouette) was chosen over row 2 (a
  black-and-white skeleton character) since it stays visually consistent with the
  existing player silhouette instead of swapping to an unrelated character design —
  reads as "caught fire," not "became a different character."
- Extraction used the same workflow as Phase 6: connected-component detection
  (`scipy.ndimage.label`, alpha > 10) scoped to a row's y-range, located by first
  rendering each sheet at 2.5x with a coordinate grid overlay and visually matching
  the description ("3rd row 5th sprite," "1st row 1st sprite," "1st row, first 4")
  against the actual content, since these are packed sheets, not a uniform grid.

**Player death**: replaced the old flat gray dim-overlay with the real 4-frame
animation. Added `Player.death_anim_t`, which starts ticking the moment `alive`
flips `False` (the `update()` early-return now increments it before returning
instead of doing nothing) and plays forward once at `DEATH_FPS`, freezing on the
last (most-charred) frame rather than looping — matches how the jetpack ping-pong
and run-cycle are already time-driven/stateless, just without the loop-back.

**Profit Bird in vehicle mode**: since there's only one static rider sprite (no
run/jetpack frame set for it), `draw_vehicle_player()` swaps the player's sprite to
it entirely while `vehicle_kind == "profit_bird"`, with a small procedural hover bob
(`sin(t*5)*3`) so it doesn't look frozen — same idea as the flame flicker, driven
off clock time rather than stored per-player state. Lil' Stomper still has no
dedicated sprite, so it keeps the Phase-10 tinted-recolor approach.

**Bug found and fixed while wiring this up**: the original plan was to tint
`vehicle_token.png` per kind the same way the player-2/Stomper recolors work
(`tinted_sprite()`, i.e. multiply by a flat color). That works fine on the
run/jetpack sprites (varied skin/clothing tones across all three channels), but
`vehicle_token.png` is a coin — almost pure gold, with a near-zero blue channel.
Multiplying a near-zero channel by *anything* still leaves it near zero, so tinting
it "light blue" for Lil' Stomper didn't read as blue at all — it crushed to a muddy
olive-gray (`(133, 146, 124)` measured directly from a rendered frame, instead of
anything resembling the intended `(150, 205, 255)`). Caught this by rendering both
tokens off-screen and sampling actual pixel colors rather than trusting the code
alone. Fixed by leaving the coin's own gold color untouched and drawing a
translucent per-kind color halo *behind* it instead (`draw_vehicle_token`) — Profit
Bird gets a warm gold halo, Lil' Stomper a clearly-blue one, and the coin art stays
intact either way.

**Verification**: re-ran the Phase 10 headless gameplay sims unchanged (confirms the
`draw_player`/`draw_coin`/`draw_vehicle_token` signature changes didn't touch any
game logic), plus a dedicated offscreen render pass exercising every new sprite path
(alive/thrusting/dead player, both death-animation extremes, Profit Bird vehicle
mode, both token kinds, a row of coins) and visually inspected the composited output
before and after the halo fix.

---

## Phase 12 — Profit Bird fix, Lil' Stomper art, pickup explosions, real background

Four follow-up items from a first look at Phase 11 in the actual game window.

- **Profit Bird was facing backwards.** The Phase 11 mirror was based on "player
  faces right, so mirror to match" reasoning that turned out wrong once seen
  in-game — reverted to the sheet's original (un-mirrored, nose-left) orientation,
  which is what actually reads correctly. Noted here since it contradicts the
  Phase 11 writeup and the Phase 6 mirroring logic doesn't universally apply to
  every asset; trust what's on screen over the "should face the same way as the
  run cycle" assumption.
- **Lil' Stomper got a real animated sprite.** User supplied two reference images:
  `LilStomperConcept.webp` (mood-board-style concept art, several inconsistent
  designs, not laid out as an extractable grid) and `LilStomperConcept2.webp`
  ("Prototype Lil' Stomper sprite sheet," a clean 2x3 grid of a two-seat mech on
  a navy background). Used the second one — extracted all 6 frames
  (`assets/lilstomper_00..05.png`) by keying out the navy background (soft
  alpha falloff from the sampled background color, not a hard cutoff, for
  cleaner edges than a plain colorkey) and tightly cropping each cell to its
  content. These 6 frames show a walking/stomping leg-cycle and are now looped
  as a real animation (`STOMPER_FPS = 7`, via the existing `anim_frame()` loop
  path) instead of the old flat-tinted-recolor placeholder. `draw_vehicle_player`
  was generalized to take a frame list + fps (Profit Bird just passes a
  single-frame list, so the same function covers both a static swap and a real
  cycle without two code paths).
- **Vehicle pickup now detonates nearby hazards.** Ask: getting a vehicle
  power-up shouldn't immediately get undone by whatever obstacle/missile was
  already in flight when you grabbed it, but this shouldn't pause or end the
  run. Implementation: `Lane.detonate_hazards()` clears that lane's current
  `obstacles`/`missiles`/`seekers` outright (spawn timers are untouched, so
  future hazards still arrive on schedule) and drops an `Explosion` — a
  lightweight, collision-free, timer-only entity — at each cleared hazard's
  former position. Triggered from `main()` right after a token pickup, alongside
  `Player.activate_vehicle()`. `Explosion` VFX is a primitive expanding
  ring + fading core (`draw_explosion`, `EXPLOSION_DURATION = 0.45s`) since no
  blast sprite was supplied — flagged as a placeholder like the seeker missile.
- **Real background.** `background.jfif` (a desert scene) is loaded once, scaled
  to fill the screen, and blitted first each frame in place of the old flat
  `BG` color fill (which is now dead code, removed). The two lanes still need
  visual separation and obstacles/text still need to read clearly against a much
  busier image, so the previously-opaque `LANE_BG` rects became translucent
  overlays (`pygame.SRCALPHA`, alpha 165) blitted over the background instead of
  replacing it — precomputed once at startup rather than rebuilt every frame.
  The name-entry screen also got a translucent dark scrim for the same
  readability reason, since it now sits over the same busy artwork instead of a
  flat color.

**Verification**: unit-tested `detonate_hazards()` directly (clears all three
hazard lists, spawns one `Explosion` per cleared hazard, explosions expire and get
culled after `EXPLOSION_DURATION`), then re-ran the exact pickup→detonate call
sequence `main()` uses (place a token under the player, collect it, confirm
`activate_vehicle` + `detonate_hazards` both fire and hazards actually clear) rather
than only testing the method in isolation. Re-ran the full Phase 10 headless
gameplay sim unchanged to confirm nothing in the physics/spawn logic regressed.
Rendered a full composited frame (background + translucent lane tints + both
explosion stages + Profit Bird + Lil' Stomper) offscreen and inspected it visually
— background reads well through the tint, both vehicle sprites display correctly,
Profit Bird's corrected orientation confirmed.

---

## Phase 13 — Five bug reports: air-hover fuel, vehicle controls, restart leak, seeker sprite

A batch of five issues reported after a hands-on playtest, each root-caused and fixed
independently:

- **Fuel refilled mid-air, letting the base jetpack hover forever.** `Player.update()`
  regenerated fuel any frame the button wasn't held, with no check on whether the
  player was actually touching the ground — so a player could stay airborne
  indefinitely by tapping just enough to never run dry, since fuel quietly topped
  back up between taps regardless of altitude. Added `Player.on_ground` (computed
  every frame from the existing floor-clamp check, `self.y >= bottom - 0.5`) and
  gated fuel regen on it. Verified headlessly: draining fuel fully while holding
  continuously in the air, then releasing but staying airborne, fuel stays flat at
  0 until the player actually lands.
- **Lil' Stomper could float forever, and its controls didn't match the ask.** It was
  reusing the exact same hold-to-fly physics as the base jetpack (just with a lower
  climb ceiling), so holding the button simply flew it around like the regular
  character with no distinct "jump" feel. Rebuilt its control scheme as its own
  branch in `Player.update()`: a fresh press is an instant upward velocity impulse
  (`self.vy = -MAX_RISE_SPEED`, i.e. a "long jump" up to the same ceiling normal
  flight caps at), and holding past that initial jump engages a much weaker
  `STOMPER_FLOAT_ACCEL` hover assist drawn from its own small tank
  (`stomper_float_fuel`, `STOMPER_FLOAT_FUEL_MAX = 1.1s`) instead of the base jetpack
  fuel — independent of the Phase-13 fuel-regen fix above, and only refills while
  grounded, so holding indefinitely can no longer float forever. A flame now also
  draws under Lil' Stomper while the float is actually engaged (`draw_vehicle_player`),
  matching the base character's jetpack flame, so the limited float reads visually.
  Verified headlessly: a tap imparts an upward impulse, and holding continuously for
  several seconds straight eventually resumes falling despite the button still being
  held (the exact "infinite float" bug this reproduces if it regresses).
- **Profit Bird's movement was wrong** — same hold-to-fly physics as the base
  character again, not the Flappy-Bird-style hopping that was asked for. Gave it its
  own branch: each fresh press snaps `vy` outright to `-PROFIT_BIRD_FLAP_SPEED`
  (standard Flappy Bird physics — a discrete hop, not additive to existing velocity),
  holding does nothing extra, no fuel involved. Also **coins now auto-collect** while
  Profit Bird is active — added `Lane.collect_all_coins()` (sweeps every coin in the
  lane, not just ones overlapping the hitbox) and switched to it in `main()` whenever
  `vehicle_kind == "profit_bird"`. Verified headlessly: a press snaps velocity to the
  flap speed regardless of prior vy, holding without a fresh press edge lets gravity
  resume immediately, and `collect_all_coins()` sweeps a lane clean regardless of
  player position.
- **Restarting with R auto-typed an "r" into the name field.** Root cause: pygame
  generates a `TEXTINPUT` event alongside `KEYDOWN` for any key that produces text,
  and text-input mode was on for the entire program, including during "playing" — so
  pressing R to restart both flipped `state` to `"enter_names"` *and* (via the
  same keypress's `TEXTINPUT` event, processed later in that frame's event queue)
  appended "r" into whichever name field was active. Fixed at the source rather than
  by filtering events after the fact: `pygame.key.stop_text_input()` is now called
  the moment gameplay starts, so SDL never generates a `TEXTINPUT` event for any key
  pressed during "playing" (R included) in the first place; `start_text_input()` is
  called again on the way back to the name-entry screen. Per a follow-up ask, restart
  now also works by **holding both players' buttons down together** once the round is
  over (`thrust[0] and thrust[1]`, checked right alongside the existing R-key path,
  both now routed through one `go_to_name_entry()` helper) — covers the keyboard
  fallback too (SPACE + UP together), not just the DAQ.
- **Seeking missiles now use real sprite art.** Per the ask, duplicated
  `assets/missile.png` to `assets/missile_seeker.png` (its own file, so future edits
  to one don't affect the other) and load it scaled to the seeker's size. The old
  primitive chevron's telegraph/armed color distinction is preserved by tinting two
  copies of the sprite (`tinted_sprite` + `lighten`, the same trick already used for
  Player 2's recolor) — orange while blink-telegraphing, red once launched — and the
  blink itself (hide on the "off" half-cycle) is untouched. Rendered both states
  offscreen to confirm the tint reads correctly and the blink-hide path still works.

**Verification**: wrote a headless sim (`SDL_VIDEODRIVER=dummy`) exercising all five
fixes directly against `Player`/`Lane` methods — grounded-only fuel regen, the Stomper
jump-then-runs-out-of-float behavior, Profit Bird's discrete-hop physics, the coin
magnet, and the seeker sprite draw path (including the blink-hidden no-op frame) —
plus a 3-second live run of the actual `main()` loop under the dummy driver to confirm
nothing crashes on startup or during normal play. The `TEXTINPUT`-suppression fix
relies on documented `pygame.key.start_text_input()/stop_text_input()` SDL behavior
(no `TEXTINPUT` event is generated at all while stopped) rather than something
reproducible in a headless event-injection test, so worth a quick real keyboard
playtest to confirm on hardware.

## Open items / next up

- Homing/tracking missiles — **done, see Phase 10.**
- Real sprite art for vehicle tokens, coins, Profit Bird, and player death — **done,
  see Phase 11.**
- Real sprite art + animation for Lil' Stomper, pickup-triggered hazard clearing,
  and a real background — **done, see Phase 12.**
- A seeking-missile sprite — **done, see Phase 13.** A dedicated explosion-burst
  sprite/particle effect is still a primitive placeholder.
- Possibly split into multiple files if the project keeps growing — held
  off so far since a single file is easier to hand around for a demo.
