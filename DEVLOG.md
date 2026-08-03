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

## Phase 14 — Scientists: the Jetpack Joyride knock-over bonus

Added the genre's signature ground-level bystander, the one mechanic that *rewards*
contact instead of punishing it. Scientists jog along the floor of each lane, and
running into one bowls it over for bonus coins.

- **`Scientist` class** (`__slots__`, same shape as `Obstacle`/`Missile`) with a
  three-step state machine: `"running"` → `"knocked"` → despawn. `knock_over()`
  flips the state and resets `anim_t` to 0 so the knockdown starts from frame 0;
  `done()` reports when the animation has finished *and* held its last frame for
  `SCIENTIST_KNOCKED_HOLD` (0.7s), at which point `Lane.update()` filters it out.
  Each scientist carries its own `anim_t` rather than reading the global clock —
  the one-shot knockdown needs time measured from the moment of impact (a global
  clock would already be clamped past the last frame before the first draw), and
  as a bonus it desynchronises the run cycles so a line of scientists doesn't
  march in lockstep.
- **Owned by `Lane`, on its own spawn timer** (`next_scientist_in`,
  `SCIENTIST_SPAWN_MIN/MAX` = 3.5–7.5s), completely independent of the obstacle,
  missile, seeker, coin and token timers — same pattern as the missile logic. No
  start delay, since unlike the hazards there's nothing to ramp up to. Spawns
  reuse the light-touch blocker check the coin/token spawns already use, so a
  scientist can't appear pre-embedded in a ground-level obstacle.
- **Ground-anchored, not randomized.** Unlike obstacles/coins the `y` is fixed to
  `lane_top + lane_h - SCIENTIST_H`, i.e. the collision box sits flush on the lane
  floor — exactly where a grounded player's own box sits, so a player running along
  the floor connects reliably.
- **Kept out of `Lane.hazards()` on purpose.** That generator feeds the kill logic,
  so scientists get their own collision pass in `main()` via
  `Lane.knock_over_scientists()`, which only considers `"running"` ones (an
  already-knocked scientist can't be re-scored no matter how long the player sits
  on it) and returns the ones it knocked over. `hazards()`' docstring now says why
  they're absent, so this doesn't get "helpfully" fixed later. They also survive
  `detonate_hazards()` — a vehicle pickup shouldn't blow up the reward.
- **Movement.** A running scientist covers only `SCIENTIST_SPEED_FRAC` (0.75) of the
  lane's scroll each frame: same direction as the scroll but slower than the
  scenery, i.e. in world terms it's fleeing the way the player flies and not quite
  keeping up, which is what drifts it back into the player. Once knocked it stops
  running and rides the lane at the full scroll speed like any other scenery.
- **Scoring.** A knockdown pays `SCIENTIST_BONUS_COINS` (3) into `Player.coins`
  rather than `distance`, so it feeds the existing separate coin leaderboard.
  Feedback is a `ScorePopup` ("+3" floating up and fading over 0.7s) — a
  lane-owned, purely-visual object that scrolls with the lane, modelled on
  `Explosion`. `Lane.knock_over_scientists()` spawns the popup itself, so the lane
  keeps owning its own VFX the way `detonate_hazards()` does.
- **`anim_frame()` gained a `loop=False` mode** that clamps the index to the last
  frame instead of wrapping it with modulo, for one-shot animations. Looping and
  ping-pong behaviour are untouched.

**Assets.** The run cycle is `assets/manager_run_00..07.png` (8 frames, 10 FPS,
looping) and the knockdown `assets/manager_down_00..03.png` (4 frames, 9 FPS, played
once). Both are loaded with the existing `load_scaled_sprite` at a shared
`SCIENTIST_SPRITE_H`, and unlike the player frames they aren't tinted per lane —
scientists are world objects, like obstacles and coins, so both lanes share one set.
The frames `rebuild_manager_sprites.py` produces carry ~20% of their canvas height as
empty padding beneath the feet (headroom for the composited bobblehead), so
`draw_scientist()` nudges the sprite down by `SCIENTIST_FOOT_PAD` to land the visible
feet on the lane floor instead of the blank bottom edge of the canvas. The collision
box (`SCIENTIST_W/H` = 22×34) is tuned to the visible body rather than the padded
canvas, and kept slightly generous — catching one is the reward.

**Verification**: 27 headless checks (`SDL_VIDEODRIVER=dummy`) against `Scientist`/
`Lane` directly — ground anchoring (box flush on the floor, `y` identical across 25
spawns, overlapping a grounded player), absence from `hazards()`, survival of
`detonate_hazards()`, the bonus paying once and only once into coins with the player
left alive and `distance` untouched, the full `running → knocked → hold → despawn`
timeline (every knockdown frame shown in order, then frame 3 held, then filtered out
by `Lane.update`), measured per-frame movement confirming a runner covers exactly
0.75× the scenery's travel while a knocked one matches it 1:1, spawn-timer
independence from the other four timers, and both draw paths including the clamped
and fully-faded edge cases. Plus a 421-frame run of the real `main()` loop under the
dummy driver, which ended with a live player on 3 coins — one knockdown — and still
alive, and screenshots confirming the feet land on the floor.

**Known art issue**: the four `manager_down_*` frames don't actually depict a
knockdown. `rebuild_manager_sprites.py`'s `ROW2` crops point at the sprite sheet's
row 2, which is a standing/fighting idle — so a knocked-over scientist currently
stands calmly for a second and vanishes. The sheet does contain a real
fall-to-prone sequence (stumble → double over → collapse → flat on the ground) lower
down, around `y≈228–262`, with frames near `x≈57–98`, `105–134`, `171–208` and
`216–262`. Swapping `ROW2` to those coordinates and re-running the script is the
whole fix; no game code changes, since the mechanic just plays whatever four frames
are in those files.

## Phase 15 — Zappers: replacing the placeholder obstacle with the real thing

The biggest remaining gameplay-feel gap. The old `Obstacle` was one sprite stretched to
a random width/height and spawned one at a time, which gave a row of isolated blocks.
Real zappers are electric beams strung between two emitter nodes, they arrive in
**groups** forming a field you weave through, and they show up at several orientations
and lengths. `Obstacle` is gone, replaced by `Zapper`.

- **Defined by two endpoints, not a w/h box.** `Zapper(x0, y0, x1, y1)` (`__slots__`,
  same shape as `Missile`) covers vertical, horizontal and diagonal beams at any
  length from one class. This is why the pre-assembled `zapper_full_*.png` frames
  aren't used — they're a fixed 118px assembly, so building from a node pair plus a
  tiled beam is what buys arbitrary length and angle. They stay in `assets/` as
  reference art.
- **Segment collision, not a bounding box.** `Zapper.collides()` uses pygame's own
  `Rect.clipline()` against the beam segment, plus a box for each solid emitter node.
  For a 45° diagonal the bounding box covers roughly twice the area the beam visibly
  occupies, so a bbox check kills you in visibly empty space. The beam counts as
  zero-width while it draws ~16px thick, which errs a few pixels in the player's
  favour — the right direction to err.
- **`Lane.hazards()` now yields hazard *objects* rather than rects.** A zapper is a
  line segment, not a box, so it has to test itself; `Missile` and `SeekingMissile`
  gained a matching `collides(rect)`. `main()`'s kill check became
  `any(hazard.collides(p_rect) for hazard in lanes[i].hazards())`, so the kill path is
  still exactly one call site rather than splitting into a rect path and a segment
  path. Scientists remain excluded, and `detonate_hazards()` still clears zappers.
- **Spawning emits patterns.** `Lane._spawn()` now picks one of four hand-authored
  builders in `ZAPPER_PATTERNS` per spawn event: `thread` (a vertical pair sharing an
  x with a gap to fly through), `stagger` (a floor-hugging horizontal then a
  ceiling-hugging one, forcing an up-then-down weave), `staircase` (three diagonals
  stepping across the lane — the case a bounding box would badly over-claim), and
  `corridor` (two long horizontals inset from ceiling and floor, leaving a channel
  down the middle). Each builder returns its own pixel width, and `next_spawn_x`
  advances by that width plus `ZAPPER_MIN_GAP..MAX_GAP` (190–320px, widened from the
  old 260–420 measured off a ~50px obstacle, since a pattern is 150–320px across on
  its own) — so the existing pacing logic is untouched.
- **The passable-gap rule is enforced in code, not just authored.**
  `pattern_is_passable()` sweeps the pattern's x-span in 8px columns (well under the
  player's height, so no beam can slip between two samples), merges the y-spans each
  zapper blocks in that column — beam interpolated across the column and padded by its
  drawn thickness, plus both node boxes — and requires the largest remaining opening to
  be at least `PLAYER_SIZE * 2.5`. `_spawn()` rerolls up to 8 times, falling back to a
  single short floor beam that cannot wall the lane off by construction. What this
  proves is per-column clearance, not full path connectivity; proving a route exists
  would mean pathfinding against the player's climb rate, and per-column clearance is
  the bar that catches the failure that actually matters.
- **This immediately earned its keep.** The check rejected `_pattern_thread` on ~a
  third of rolls: the two emitter nodes face each other across the gap and each eats
  `ZAPPER_NODE_SIZE / 2` of it, so an opening authored as `ZAPPER_MIN_GAP_H * 1.05`
  (84px) left only 64px actually free, under the 80px bar. The authored figure now
  adds a whole node width on top. That is exactly the bug that would have shipped if
  the guarantee had been left to the hand-authored numbers.
- **Animated beams.** `draw_zapper()` builds the beam as a horizontal strip, tiling
  *consecutive* arc frames along its length (which reads as travelling crackle rather
  than one shape flashing in place), then rotates the finished strip once to the
  segment's angle — far simpler than placing each tile along a diagonal, at a cost of
  one rotate per zapper per frame. Each zapper gets a phase offset from its own x (the
  same trick `draw_coin` uses) so beams on screen don't crackle in unison.

**Assets.** Cut from the Stage Hazards sheet in the previous step. The beam uses
`zapper_arc_g2_00..07.png` — the *tileable* set, confirmed by butting the frames
edge-to-edge and getting one seamless unbroken band (uniform 22px source width); the
other extracted set is the emitter burst, which butts into separate blobs with gaps and
is not used here yet. Emitters use the 4-frame `zapper_node_00..03.png`. Both load
through the existing `load_scaled_sprite`.

**Verification**: 35 headless checks (`SDL_VIDEODRIVER=dummy`) — that the empty corner
of a diagonal's bbox is *not* a hit while every one of 41 sample points along that same
diagonal is; hits and misses on vertical and horizontal beams and on the emitter nodes;
that a full-height wall and a deliberately-64px gap are both rejected while a 110px gap
is accepted; that no vertical beam slips between column samples (swept across 60
consecutive x positions); 3000 rolls of each of the four patterns in both lanes all
passing the gap rule and all emitting 2–4 zappers; that 300 `Lane._spawn()` calls always
emit a group and always advance `next_spawn_x`; that `hazards()` still feeds the kill
path, still excludes scientists, still gates telegraphing seekers, and that every
hazard it yields exposes `collides()`; that `detonate_hazards()` clears zappers and
drops one explosion each; that both endpoints scroll together so beam shape is
preserved, and off-screen beams are culled; and that `draw_zapper` handles all
orientations including a degenerate zero-length beam. Plus a 701-frame run of the real
`main()` loop, which held a **median frame time of 16.52ms against the 60fps
(16.67ms) cap** — the per-frame rotate costs nothing measurable — and screenshots
confirming all four patterns render with correct rotation.

## Phase 16 — Coin formations

Coins used to drop one at a time at a random y every 1.1–2.3s, which made them
scenery. In the real game they arrive in **formations** that trace a path, and the path
is bait: following a trail is exactly what pulls you into a zapper. That tension is the
point, and a lone random coin never created it.

- `_spawn_coin` is replaced by **`_spawn_coin_formation()`**, which picks one of five
  shapes from `COIN_FORMATIONS` and lays 5–10 coins along it at a uniform
  `COIN_SPACING` (34px): `line` (flat run), `arc_up` (a hill, sine-shaped, peaking in
  the middle), `arc_down` (its mirror), `zigzag` (triangle wave, reversing every 3
  coins) and `staircase` (monotonic steps, up or down). Each shape is a small function
  returning n vertical offsets to hang off a common baseline, so adding a sixth is a
  handful of lines.
- **The whole shape is kept inside the lane** by deriving the legal baseline band from
  the shape's own vertical travel (`min`/`max` of its offsets) — a tall arc just gets a
  narrower band to sit in rather than clipping through the ceiling.
- **Placement is all-or-nothing.** The existing blocker check is kept but applied to
  every coin in the formation: if any one of them would land in something, the whole
  formation is re-rolled at a different baseline (`COIN_FORMATION_TRIES` = 6) before
  the spawn is skipped. A trail that runs into a beam is indistinguishable from a safe
  one until it's too late, so a partially-placed formation would be worse than none.
- **Timer lengthened to `COIN_SPAWN_MIN/MAX` = 3.2–5.6s**, now per *formation* rather
  than per coin — a formation is up to ~300px long, so the old single-coin cadence
  would have run them into each other.
- `Player.coins`, `collect_coins()`, `collect_all_coins()` (the Profit Bird magnet) and
  the leaderboard are all untouched, and there's a test asserting exactly that.

**Verification**: 22 headless checks — shape geometry (arc peaks mid-run and returns to
baseline, arc_down mirrors it exactly, zigzag reverses more than once, staircase is
monotonic); no coin escaping the lane across 4000 formations in *both* lanes; every
formation holding 5–10 coins at exactly uniform x spacing and never a single; the
blocker check holding against a lane walled with zappers and against tokens; a blocked
spawn still rearming the timer; the three collection entry points behaving identically
to before; and `_spawn_coin` being gone from `Lane` entirely.

## Phase 17 — One missile type, with a telegraph

The original has exactly one missile: a warning marker appears at the screen edge for a
beat, then it fires. The plain `Missile` — no warning, no homing — didn't exist in it.
Rather than delete a hazard, it now gets the warning half of that treatment while
staying non-homing, which keeps it distinct from `SeekingMissile`.

- New **`MissileWarning`** (`__slots__`, `y` + `timer`), spawned by
  `_spawn_missile_warning()` where `_spawn_missile` used to fire directly. When its
  timer passes `MISSILE_WARN_TIME` (1.2s), `Lane.update` calls `_launch_missile(w.y)`
  and drops the warning.
- **Nothing to collide with, by construction.** The warning isn't in `Lane.hazards()`,
  but more than that: the `Missile` object isn't *built* until the warning elapses, so
  for the entire telegraph window there is no missile in the lane at all. The class has
  no `rect()` and no `collides()` — there's no surface to hit it through even by
  mistake.
- **The missile inherits the warned `y` exactly**, so the marker genuinely tells you
  where the missile is coming from rather than being decoration. Speed is still read at
  *launch* rather than at warning time, keeping it scroll_speed-relative as intended.
- `draw_missile_warning()` is pygame primitives only — a blinking red rounded rect with
  a white border and a white "!" (stem plus separate dot), pinned to the right edge of
  the lane, clamped vertically so a marker for a missile at the very top or bottom of a
  lane can't poke into the neighbouring one.

**Verification**: 12 headless checks — a warning appearing with no missile alongside it;
the missile not existing before 1.2s and firing within one frame after; the warning
being consumed exactly when the missile appears; the missile taking the warned y to the
bit; the missile still ignoring the player's y after 30 frames with the player parked
far above it (a homer would climb); a player box swept down the entire right edge across
the marker never registering a hit; and the marker staying inside its own lane when
drawn at both lane extremes.

## Phase 18 — One course per round

Both lanes built their own unseeded `random.Random()` and never reseeded, so the two
players raced completely different courses. For a head-to-head that made the winner
partly a matter of who drew the kinder lane, and it meant the leaderboard was ranking
runs that were never comparable.

- `Lane.reset()` takes an optional **`seed`**; `main()` generates one
  `random.randrange(2**31)` per round in `reset_game()` and passes the same value to
  both lanes. Every spawn position and timing in a lane is drawn from that one rng, so
  seeding it identically makes the two courses identical and skill the only variable.
- Passing no seed keeps the old arbitrary-course behaviour, which the headless tests
  lean on.
- The seed is shown small under the restart prompt on the game-over screen, so a round
  can be referenced or replayed later.

**Verification**: stepping a top-lane and a bottom-lane instance seeded identically
through 60 in-game seconds and comparing a full course signature every 5 frames —
zappers, coins, missiles, missile warnings, seekers, tokens and scientists, all in
lane-relative coordinates — with zero divergence across ~4800 zapper, ~2400 coin, 130
warning and 480 scientist samples, plus the exact underlying invariant that both lanes'
rngs finish in an identical state. Different seeds are confirmed to diverge and the same
seed to replay identically on a fresh lane. (Worth recording: the first version of this
test failed spuriously. Comparing *absolute* y values meant adding a 320px lane offset
and taking it back off again, which perturbs the last floating-point bit — a 1.4e-14
"difference" that was an artifact of the comparison, not the courses. The signature now
converts to lane-relative before rounding.)

## Phase 19 — Coin/zapper separation, and a scalable display

Two things from a play session.

- **Coins could still clip into beams.** The cause was spawn ordering, not the check
  itself: coin formations occupy x `SCREEN_W+20..+326` while zapper patterns spawn from
  `next_spawn_x` (`SCREEN_W+100` and up), so the two spawn regions overlap in x and
  whichever spawned *second* landed on the other. Coins checked the zappers that existed
  at the time; a pattern spawning afterwards had no coin check at all. Fixed in both
  directions with one shared predicate, `coin_is_clear()`: formations avoid existing
  zappers, and `_spawn()` culls any coins its new pattern lands on. Culled coins are
  always still off the right edge at that moment, so nothing visibly vanishes. The test
  asserts that specifically. The rule also now tests against the beam **segment** rather
  than the zapper's bounding box, so coins can sit in the open space beside a diagonal
  instead of avoiding the whole box, with `COIN_CLEARANCE` (half the beam's drawn
  thickness plus a 7px margin) keeping a visible gap.
- **Windowed / windowed-fullscreen / fullscreen, fully scalable.** The game now renders
  every frame to a fixed 1100x640 `canvas` surface, and `present()` scales that into
  whatever the window is as the very last step. Scaling is uniform on both axes with the
  remainder as letterbox bars, so no mode stretches the art or crops the playfield. F11
  cycles the three modes (`apply_display_mode`), the windowed mode is `RESIZABLE` and
  handles `VIDEORESIZE`, and the current mode is labelled on the name-entry screen and
  in the corner during play. `smoothscale` rather than nearest: at the non-integer
  factors an arbitrary window produces, nearest leaves sprite pixels unevenly sized and
  the antialiased UI text visibly jagged.

  The point of doing it this way is that **no game code is resolution-aware** — not one
  collision box, spawn coordinate, sprite scale or layout constant changed, and
  `SCREEN_W`/`SCREEN_H` remain plain constants. A test asserts `window.get_size()`
  appears exactly once in the whole file, inside `present()`.

**Verification**: 25 headless checks — zero coin/beam overlaps across 5 seeds x 2 lanes
x 2 in-game minutes (75,446 coin samples), tested tighter than the spawn rule itself;
both spawn orders covered explicitly, including the previously-broken one; no coin ever
removed while on screen; `coin_is_clear` allowing a coin in the open corner of a
diagonal's bbox while rejecting one on the beam; and `present()` fitting without
cropping and preserving aspect ratio to within 0.01 at 1100x640, 1920x1080, 2560x1440,
800x480, 1280x1024 and 640x640, with correct letterbox fill. Plus a 761-frame run of the
real `main()` loop cycling through all three display modes mid-run without an exception,
holding a median 16.64ms frame against the 60fps cap.

## Open items / next up

- Homing/tracking missiles — **done, see Phase 10.**
- Real sprite art for vehicle tokens, coins, Profit Bird, and player death — **done,
  see Phase 11.**
- Real sprite art + animation for Lil' Stomper, pickup-triggered hazard clearing,
  and a real background — **done, see Phase 12.**
- A seeking-missile sprite — **done, see Phase 13.** A dedicated explosion-burst
  sprite/particle effect is still a primitive placeholder.
- Knock-over scientists — **done, see Phase 14.** Their knockdown frames still need
  re-cropping from the sheet's actual fall sequence (see the known art issue there);
  `rebuild_manager_sprites.py` also still points at the sandbox paths it was
  originally written under (`/mnt/user-data/uploads`, `/home/claude`) rather than
  local ones, so it needs its three path constants updated before it will re-run.
- Zappers (beams between emitter nodes, spawned in patterns) — **done, see Phase 15.**
  The extracted emitter-burst frames (`zapper_arc_g1_*`) aren't used yet; they'd suit a
  flash at each node, or the explosion VFX that's still a primitive placeholder.
- Coin formations — **done, see Phase 16**; coin/zapper separation, **Phase 19.**
- A telegraph on the straight missile — **done, see Phase 17.**
- Both players racing an identical course — **done, see Phase 18.**
- Windowed / borderless / fullscreen scaling — **done, see Phase 19.**
- Still missing versus the original: spin tokens and the end-of-run slot machine,
  missions/objectives, gadgets, and the death slide ("final blast") where Barry tumbles
  along the ground for bonus distance. The vehicle roster is also two of the original's
  nine or so.
- Possibly split into multiple files if the project keeps growing — held
  off so far since a single file is easier to hand around for a demo.
