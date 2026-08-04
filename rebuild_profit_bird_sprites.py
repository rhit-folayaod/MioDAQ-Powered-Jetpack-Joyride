"""
Builds the Profit Bird flap cycle (assets/profit_bird_00..05.png) out of the single
static assets/profit_bird.png.

There was never a flap animation in the source art -- only one static pose -- so rather
than hand-drawing frames this cuts the wing out of the existing sprite and re-composites
it at a series of angles about the shoulder. Two details make that read as animation
rather than as a pasted-on layer:

  * The hole the wing leaves behind is filled from the nearest surviving *body* pixel
    rather than one flat red, so the patch inherits the belly's own shading and the
    raised-wing frames show an unbroken body instead of a bite mark. Filling the whole
    hole (rather than only the part enclosed by the remaining silhouette) is deliberate:
    the wing's feather tips overhang the belly, and healing them into the red reads as
    the bird's tail, whereas leaving them transparent leaves a ragged notch.
  * Every frame is padded to one common canvas sized to the widest swing, so the frames
    stay registered with each other -- the bird's body sits at the same place in all six
    and only the wing moves. PROFIT_BIRD_SPRITE_H in the game accounts for that padding.

All tunables are at the top so you can re-run and eyeball contact_profit_bird.png.

Run:
    python rebuild_profit_bird_sprites.py
"""
import os

import numpy as np
from PIL import Image
from scipy.ndimage import binary_dilation, distance_transform_edt, label

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "assets", "profit_bird.png")
OUT_DIR = os.path.join(HERE, "assets")
CONTACT = os.path.join(HERE, "contact_profit_bird.png")

# ---- TUNABLES -------------------------------------------------
# (wing angle in degrees, body offset in px). Positive angle sweeps the wing tip DOWN;
# positive dy pushes the whole bird down. Frame 5 is the neutral/glide pose (the original
# art untouched) and is what's held between flaps, so the cycle starts and ends at rest.
FRAMES = [
    (-26.0, 1),   # 0 wind-up, wing raised
    (12.0, -1),   # 1 sweeping down
    (34.0, -2),   # 2 full downstroke, body lifted by it
    (16.0, -1),   # 3 recovering
    (-10.0, 1),   # 4 wing back up
    (0.0, 0),     # 5 neutral glide -- identical to the source art
]
PIVOT = (56.0, 47.0)   # shoulder, where the wing meets the body (source-image px)
WING_COLORS = [(214, 140, 43), (173, 101, 5), (110, 65, 18), (251, 186, 96)]
BODY_COLORS = [(199, 41, 44), (142, 8, 17), (240, 89, 83)]
COLOR_TOL = 12         # per-channel match tolerance when classifying palette colors
WING_BBOX = (18, 40, 62, 65)   # x0,y0,x1,y1 -- limits the wing search to the near wing,
                                # keeping the beak's similarly-orange shading out of it
DARK_MAX = 60          # a pixel this dark on every channel counts as outline
# ---------------------------------------------------------------

src = Image.open(SRC).convert("RGBA")
arr = np.array(src).astype(int)
r, g, b, alpha = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2], arr[:, :, 3]
opaque = alpha > 100


def match(colors):
    m = np.zeros(opaque.shape, bool)
    for c in colors:
        m |= ((np.abs(r - c[0]) < COLOR_TOL) & (np.abs(g - c[1]) < COLOR_TOL)
              & (np.abs(b - c[2]) < COLOR_TOL))
    return m & opaque


# --- 1. isolate the wing ---------------------------------------------------
wing_seed = match(WING_COLORS)
box = np.zeros(opaque.shape, bool)
box[WING_BBOX[1]:WING_BBOX[3], WING_BBOX[0]:WING_BBOX[2]] = True
wing_seed &= box
lab, n = label(wing_seed)
if n == 0:
    raise SystemExit("no wing blob found -- check WING_COLORS / WING_BBOX")
sizes = [(lab == i).sum() for i in range(1, n + 1)]
wing = lab == (1 + int(np.argmax(sizes)))

# The wing's black outline: dark pixels touching the wing blob. Where wing and body share
# an edge that outline is the wing's own, so taking it leaves a clean red hole behind.
dark = (r < DARK_MAX) & (g < DARK_MAX) & (b < DARK_MAX) & opaque
wing_full = wing | (binary_dilation(wing, iterations=2) & dark)
print(f"wing: {wing.sum()} px + outline -> {wing_full.sum()} px")

# --- 2. body with the wing removed and its hole healed ---------------------
# Every hole pixel takes the colour of the nearest surviving body pixel, so the patch
# inherits the belly's shading gradient instead of reading as one flat red slab.
body_arr = arr.copy()
body_src = match(BODY_COLORS) & ~wing_full
_, (iy, ix) = distance_transform_edt(~body_src, return_indices=True)
body_arr[wing_full] = arr[iy[wing_full], ix[wing_full]]
body_img = Image.fromarray(body_arr.astype(np.uint8), "RGBA")
print(f"healed {wing_full.sum()} px of belly from {body_src.sum()} body px")
wing_arr = arr.copy()
wing_arr[~wing_full] = 0
wing_img = Image.fromarray(wing_arr.astype(np.uint8), "RGBA")

# --- 3. pad to one shared canvas ------------------------------------------
# Sized by actually rotating the wing's pixel coordinates for every frame rather than
# from a trig estimate of the swing, so no feather tip can clip; symmetric so the body
# stays dead centre, since the game blits these centred on the player's y.
ys, xs = np.where(wing_full)
over = np.zeros(4)  # how far past each edge (left, top, right, bottom) any frame reaches
for angle, dy in FRAMES:
    # Negated: screen y grows downward, so a plain positive rotation would lift the tip,
    # and the FRAMES convention is that positive means the tip sweeps down.
    th = np.radians(-angle)
    rx = PIVOT[0] + (xs - PIVOT[0]) * np.cos(th) - (ys - PIVOT[1]) * np.sin(th)
    ry = PIVOT[1] + (xs - PIVOT[0]) * np.sin(th) + (ys - PIVOT[1]) * np.cos(th) + dy
    over = np.maximum(over, [-rx.min(), -ry.min(),
                             rx.max() - (src.width - 1), ry.max() - (src.height - 1)])
pad_x = int(np.ceil(max(over[0], over[2]))) + 1
pad_y = int(np.ceil(max(over[1], over[3]))) + 1
canvas_size = (src.width + pad_x * 2, src.height + pad_y * 2)
print(f"swing overhang l/t/r/b {np.round(over, 1)} -> pad x{pad_x} y{pad_y}, "
      f"canvas {canvas_size}")


def pad(img, dy=0):
    out = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    out.paste(img, (pad_x, pad_y + dy))
    return out


frames = []
for angle, dy in FRAMES:
    layer = pad(wing_img, dy)
    if angle:
        # PIL rotates counter-clockwise on screen, and the wing extends left of the
        # pivot, so a positive angle here drops the tip -- the downstroke.
        layer = layer.rotate(angle, resample=Image.NEAREST,
                             center=(PIVOT[0] + pad_x, PIVOT[1] + pad_y + dy))
    frame = pad(body_img, dy)
    frame.alpha_composite(layer)
    frames.append(frame)

for i, f in enumerate(frames):
    f.save(os.path.join(OUT_DIR, f"profit_bird_{i:02d}.png"))

# Contact sheet, upscaled, on the lane's background tint -- the only way to tell whether
# the healed belly and the wing angles actually hold up.
sc, pad_c = 4, 6
w, h = canvas_size
sheet = Image.new("RGB", (len(frames) * (w + pad_c) + pad_c, h + pad_c * 2), (35, 40, 55))
for i, f in enumerate(frames):
    sheet.paste(f, (pad_c + i * (w + pad_c), pad_c), f)
sheet.resize((sheet.width * sc, sheet.height * sc), Image.NEAREST).save(CONTACT)
print(f"wrote {len(frames)} frames to {OUT_DIR} and {CONTACT}")
