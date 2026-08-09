"""
The persistent leaderboard: a JSON file next to the game, and the panel that shows it.

A flat file rather than a database because the demo needs exactly one thing a
database would give it -- surviving a restart -- and nothing else. Every load and
save is wrapped defensively: a corrupt or unreadable board degrades to an empty one
and the game keeps running, because a leaderboard failing in front of an audience
should never be what ends the round.

Entries are `(name, distance, coins)`. Distance alone determines rank; coins ride
along and are displayed beside it.
"""
import json
import os

import pygame

from .config import MAX_HIGH_SCORES, SCORES_PATH, SCREEN_W, WHITE


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
    """Overwrites the board file, swallowing any failure.

    Not atomic -- a crash mid-write loses the board. Acceptable here because the
    board is a nice-to-have that is rewritten after every round, and the failure it
    guards against instead (an unwritable path taking the game down between rounds)
    is the one that would actually be visible to an audience.
    """
    try:
        with open(SCORES_PATH, "w") as f:
            json.dump([{"name": n, "score": s, "coins": c} for n, s, c in scores], f, indent=2)
    except Exception as exc:
        print(f"Could not save high scores: {exc}")


def clear_high_scores():
    """Wipes the persistent leaderboard -- the admin portal's only action.

    Returns the now-empty list rather than returning nothing, so the caller's in-memory
    copy is replaced from the same call that rewrites the file and the two can't drift.
    Overwrites high_scores.json with an empty list instead of deleting it, so the next
    save writes to a file that's already there.
    """
    save_high_scores([])
    return []


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
