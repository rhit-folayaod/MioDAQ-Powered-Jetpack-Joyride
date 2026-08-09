"""
Two-Player Jetpack Joyride-style Demo -- entry point.

Dependencies:
    pip install -r requirements.txt
    (nidaqmx is optional at runtime -- the game auto-falls-back to keyboard-only
     mode if the package or the DAQ device isn't available)

Hardware (optional -- developed against an NI mioDAQ):
    Update DEVICE in jetpack/config.py to match the name in NI MAX.
    Buttons -> port0/line0:1 (DI)   LEDs -> port0/line6:7 (DO)
    (this is the line mapping from the originally-tested working script;
     if your physical wiring differs, update BUTTON_LINES / LED_LINES there)

Controls:
    Name entry: type on the keyboard, TAB to switch player field, ENTER to start.
    Start a round hands-free: both players hold their buttons together for 2 seconds
        (SPACE + UP on the keyboard works too, DAQ connected or not).
    Player 1: Button 1  (falls back to SPACE only when no DAQ is connected)
    Player 2: Button 2  (falls back to UP ARROW only when no DAQ is connected)
    R (or both buttons together): once both players are down, returns to name entry to play again
    Shift+Q: admin portal -- type the passcode to wipe the saved leaderboard. Only opens
        between rounds (name entry / game over), and is deliberately not shown on screen.
        Shift-modified so a plain "q" still types into a name field.
    F11: cycle windowed / windowed-fullscreen / fullscreen
    ESC: quit

Run:
    python main.py

The game itself lives in the `jetpack` package; see jetpack/__init__.py for the
module map, or docs/ARCHITECTURE.md for why it is split the way it is.
"""
from jetpack.game import main

if __name__ == "__main__":
    main()
