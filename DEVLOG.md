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

---

## Open items / next up

- Homing/tracking missiles (explicitly deferred from the original ask).
- Possibly split into multiple files if the project keeps growing — held
  off so far since a single file is easier to hand around for a demo.
