# Asset credits and licensing scope

**The MIT license in [LICENSE](LICENSE) covers the source code only.** It does not
cover the artwork in `assets/`, most of which is third-party and not mine to
relicense. This file records where each piece came from.

This is a non-commercial personal project built during an internship. Nothing here
is sold or distributed as a product.

## Character, hazard and UI sprites

Cut from *Dan the Man* mobile asset sheets — © [Halfbrick
Studios](https://www.halfbrick.com/) — including art from that game's Jetpack
Joyride crossover event. The original packed sheets are kept in
`assets/source/` so every extraction is traceable back to what it came from.

| Sprite set | Source sheet |
|---|---|
| `run_00..09`, `jetpack_00..04` | Playable Characters — Barry Steakfries |
| `death_00..03` | Miscellaneous — Charred Death Animation |
| `zapper_node_*`, `zapper_arc_*`, `zapper_full_*`, `missile_base.png`, `obstacle.png` | Miscellaneous — Stage Hazards (Jetpack Joyride Event) |
| `profit_bird.png` | Miscellaneous — Jetpack Joyride Event Cutscenes |
| `vehicle_token.png` | Miscellaneous — Coin Counter |
| `manager_run_*`, `manager_down_*` (bodies only) | a scientist sheet from the same family |

Used here as an unlicensed homage for a personal, non-commercial demo, with no
claim of ownership or endorsement. If you are from Halfbrick and would like this
removed, open an issue and I will take it down.

*Jetpack Joyride* and *Dan the Man* are trademarks of Halfbrick Studios. This
project is not affiliated with or endorsed by Halfbrick.

## Generated sprites

Three sets are produced by scripts in `tools/` rather than cut directly from a
sheet, each from source art kept in the repo so they can be re-run:

- **`manager_run_00..07`, `manager_down_00..03`** — `tools/rebuild_manager_sprites.py`
  composites a photograph of a colleague's face onto scientist sprite bodies: an
  in-joke for the internal audience this was originally demoed to.
- **`missile.png`** — `tools/rebuild_missile_sprite.py`, the same gag on the nose
  of the rocket, over `missile_base.png`.
- **`profit_bird_00..05`** — `tools/rebuild_profit_bird_sprites.py` synthesises a
  six-frame wing-flap cycle from the single static pose in `profit_bird.png`.
  No new source art; entirely derived from the sheet extraction above.

## Other art

| Asset | Notes |
|---|---|
| `assets/coin_ni_64.png` | The National Instruments logo, styled as a coin. NI's trademark, used to fit the demo's hardware theme. Not affiliated with or endorsed by NI. |
| `assets/background.png` | Stock cartoon desert parallax background. Original source and license were not recorded at the time — **provenance unverified**. |
| `assets/lilstomper_00..05` | Extracted from `assets/source/LilStomperConcept2.webp`, a reference image supplied during development. Original source and license were not recorded — **provenance unverified**. |

The two "provenance unverified" rows are honest gaps rather than claims of
clearance. Both would need replacing before this could be used for anything
beyond a personal portfolio piece.
