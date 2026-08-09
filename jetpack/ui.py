"""
The non-gameplay screens: name entry, the hold-to-start meter, and the admin modal.

These are drawn over the same background the game uses, so each one carries its own
readability treatment (a scrim, a panel) rather than assuming a flat backdrop. Like
`render`, nothing here mutates state -- each function receives what it should show
and draws it.
"""
import time

import pygame

from .config import *
from .render import draw_text_center


def draw_name_entry(surface, font_big, font_med, font_small, names, active_field, display_label):
    """The pre-round screen: two name fields, one outlined in each player's colour.

    Colour-coding the boxes to match the players' sprites is what tells two people
    standing at a cabinet which field is theirs without a word of instruction, and
    it is why the active field is marked by border weight rather than by colour --
    colour is already carrying player identity here.
    """
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


def draw_start_hold(surface, font_small, progress):
    """Hold-to-start meter on the name screen, filling while both buttons are held.

    Drawn even at zero progress rather than only appearing once a hold begins: an empty
    meter sitting under the name boxes is the thing that tells a player standing at the
    buttons that holding them is how a round starts. The keyboard fallback is spelled out
    because it works here whether or not the DAQ is connected, unlike during a run.
    """
    filling = progress > 0.0
    draw_text_center(surface, "STARTING..." if filling else "HOLD BOTH BUTTONS (or SPACE + UP) TO START",
                      font_small, (255, 220, 90) if filling else (170, 170, 180),
                      (SCREEN_W // 2, 400))
    track = pygame.Rect(0, 0, 380, 16)
    track.center = (SCREEN_W // 2, 428)
    pygame.draw.rect(surface, FUEL_BAR_EMPTY_COLOR, track, border_radius=8)
    fill_w = int(track.width * min(1.0, progress))
    if fill_w > 0:
        pygame.draw.rect(surface, (255, 220, 90), (track.left, track.top, fill_w, track.height),
                          border_radius=8)
    pygame.draw.rect(surface, (95, 95, 110), track, width=1, border_radius=8)


def draw_admin_portal(surface, font_big, font_med, font_small, typed, message, message_ok):
    """Modal passcode prompt, drawn over whatever screen was underneath it.

    The dim is light enough to leave the leaderboard in the corner readable through it,
    so when the passcode lands the operator watches the board go empty behind the panel
    -- confirmation of the wipe rather than just a line of text claiming it happened.
    """
    overlay = pygame.Surface((SCREEN_W, SCREEN_H), pygame.SRCALPHA)
    overlay.fill((0, 0, 0, 170))
    surface.blit(overlay, (0, 0))

    panel = pygame.Rect(0, 0, 540, 250)
    panel.center = (SCREEN_W // 2, SCREEN_H // 2)
    pygame.draw.rect(surface, (28, 31, 42), panel, border_radius=10)
    pygame.draw.rect(surface, (200, 170, 70), panel, width=2, border_radius=10)

    draw_text_center(surface, "ADMIN PORTAL", font_big, (255, 220, 90),
                      (SCREEN_W // 2, panel.top + 44))
    draw_text_center(surface, "Enter passcode to reset the leaderboard", font_small,
                      (170, 170, 180), (SCREEN_W // 2, panel.top + 82))

    box = pygame.Rect(0, 0, 340, 44)
    box.center = (SCREEN_W // 2, panel.top + 132)
    pygame.draw.rect(surface, (18, 20, 28), box, border_radius=8)
    pygame.draw.rect(surface, (200, 170, 70), box, width=2, border_radius=8)
    text = font_med.render(typed, True, WHITE)
    surface.blit(text, (box.left + 12, box.centery - text.get_height() // 2))
    if int(time.time() * 2) % 2 == 0:
        cursor_x = box.left + 12 + text.get_width() + 2
        pygame.draw.line(surface, WHITE, (cursor_x, box.top + 8), (cursor_x, box.bottom - 8), 2)

    if message:
        draw_text_center(surface, message, font_small,
                          (120, 230, 150) if message_ok else (255, 110, 100),
                          (SCREEN_W // 2, panel.top + 178))
    draw_text_center(surface, "ENTER to confirm  -  ESC to close", font_small,
                      (140, 140, 150), (SCREEN_W // 2, panel.bottom - 26))
