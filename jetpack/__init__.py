"""
A two-player, split-screen jetpack side-scroller driven by NI DAQ buttons, with an
automatic keyboard fallback.

Entry point is `main.py` at the repo root, which calls `jetpack.game.main()`.
Module map:

    config      every tunable constant, data only
    daq         NI DAQ button/LED I/O on a background thread
    entities    Player and the objects that live in a lane
    patterns    zapper patterns, coin formations, and the fairness predicates
    lane        one player's world and its spawn timers
    render      sprite loading, drawing, and canvas-to-window scaling
    scores      the persistent leaderboard
    ui          name entry, hold-to-start, admin modal
    game        the loop that ties it together
"""
