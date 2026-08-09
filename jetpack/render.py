"""
Drawing, sprite loading and the window/canvas scaling layer.

Nothing in here mutates game state -- every function takes a surface and something
to draw on it. That separation is what makes the headless verification the project
leaned on possible: a `Lane` can be stepped for sixty simulated seconds with no
display at all, because none of the update path passes through this module.

`present()` is the whole resolution story. The game draws every frame to a fixed
SCREEN_W x SCREEN_H canvas and this scales that canvas into whatever the window
happens to be, as the last act of the frame. No collision box, spawn coordinate or
sprite scale anywhere else in the package is resolution-aware.
"""
import math

import pygame

from .config import *


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


def draw_vehicle_player(surface, p, t, frames, fps, frame_t=None, loop=True):
    """Vehicle-mode sprite swap: Lil' Stomper passes its 6-frame walk cycle (anim_frame
    loops it like run_frames); Profit Bird passes its flap cycle with loop=False.
    Draws a jetpack flame while Lil' Stomper is actively floating (p.thrusting), same as
    the base character's jetpack flame, so the limited float is visually legible.

    `frame_t` (default: the global clock `t`) is the clock the *frames* advance on, kept
    separate from `t` so Profit Bird can drive its one-shot flap off the player's own
    per-flap timer while the hover bob still rides the global clock -- otherwise the bob
    would freeze along with the wings whenever the bird was gliding.
    """
    sprite = anim_frame(frames, t if frame_t is None else frame_t, fps, loop=loop)
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
