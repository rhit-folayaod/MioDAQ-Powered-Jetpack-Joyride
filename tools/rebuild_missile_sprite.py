"""
Builds the "Mateo missile" (assets/missile.png) by compositing a photo head onto the nose
of the rocket art -- the same gag as the scientists in rebuild_manager_sprites.py.

assets/missile_base.png is the original faceless rocket, kept as this script's source so
the composite can be re-run or retuned without having lost the art underneath it.

The whole problem here is legibility: the rocket draws 24px tall and crosses the lane in
about a second, so anything subtle (a face inset in a porthole on the hull) reads as a
smudge. Instead the head is sized to nearly the full height of the hull and placed over
the *nose*, which the rocket leads with -- it fills the one part of the silhouette a
player's eye is already tracking, and leaves the fins, nozzle and flame untouched so the
thing is still obviously a rocket.

Sized so the composite still fits the original 62x24 canvas: no padding, so the sprite
scale, the MISSILE_W x MISSILE_H collision box and the draw call in the game are all
exactly as they were.

Run (from the repo root):
    python tools/rebuild_missile_sprite.py
"""
import os

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "assets", "missile_base.png")   # the original rocket, faceless
PHOTO = os.path.join(ROOT, "assets", "source", "mateo_faceshot.jfif")
OUT = os.path.join(ROOT, "assets", "missile.png")
CONTACT = os.path.join(ROOT, "contact_missile.png")      # gitignored build output

# ---- TUNABLES -------------------------------------------------
HEAD_BOX = (174, 46, 258, 180)   # crop from the photo: x0,y0,x1,y1
HEAD_H = 20                      # head height in sprite px (width follows the crop's ratio)
HEAD_CENTER = (12.5, 11.5)       # where the head sits on the 62x24 rocket
RIM = (24, 26, 32)               # keyline drawn around the head -- every shape in this art has one
SS = 8                           # supersample factor while masking, for a clean ellipse edge
# ---------------------------------------------------------------

rocket = Image.open(SRC).convert("RGBA")
photo = Image.open(PHOTO).convert("RGB")
head_src = photo.crop(HEAD_BOX)


def make_head(height):
    """The face, cropped to a soft-edged ellipse and scaled down to sprite size.

    Masked and blurred at SSx *before* the downscale: doing it at final size on a ~20px
    head leaves the ellipse edge visibly stair-stepped against the rocket's clean keyline.
    """
    h = max(4, int(round(height)))
    w = max(4, int(round(h * head_src.width / head_src.height)))
    big = head_src.resize((w * SS, h * SS), Image.LANCZOS)
    mask = Image.new("L", (w * SS, h * SS), 0)
    ImageDraw.Draw(mask).ellipse([w * SS * 0.03, h * SS * 0.03, w * SS * 0.97, h * SS * 0.97],
                                 fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(w * SS * 0.03))
    big.putalpha(mask)
    return big.resize((w, h), Image.LANCZOS)


def with_rim(head):
    """Adds the art's black keyline around the head, so it reads as part of the sprite
    rather than as a photo pasted on top of it."""
    pad = 1
    alpha = np.array(head)[:, :, 3] > 120
    ring = np.zeros((head.height + pad * 2, head.width + pad * 2), bool)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            ring[pad + dy:pad + dy + head.height, pad + dx:pad + dx + head.width] |= alpha
    ring[pad:pad + head.height, pad:pad + head.width] &= ~alpha
    rim_arr = np.zeros((ring.shape[0], ring.shape[1], 4), np.uint8)
    rim_arr[ring] = (*RIM, 255)
    out = Image.fromarray(rim_arr, "RGBA")
    out.alpha_composite(head, (pad, pad))
    return out


head = with_rim(make_head(HEAD_H))
cx, cy = HEAD_CENTER
ox = int(round(cx - head.width / 2))
oy = int(round(cy - head.height / 2))
if ox < 0 or oy < 0 or ox + head.width > rocket.width or oy + head.height > rocket.height:
    raise SystemExit(f"head {head.size} at ({ox},{oy}) would clip the {rocket.size} canvas "
                     f"-- lower HEAD_H or move HEAD_CENTER")

canvas = rocket.copy()
canvas.alpha_composite(head, (ox, oy))
canvas.save(OUT)
print(f"head {head.size} at ({ox},{oy}) on a {rocket.size} rocket -- canvas unchanged")

# Contact sheet: the sprite at game scale beside a 6x blowup, on the lane's background
# tint, since 24px on a dark lane is the only view that says whether the face reads.
sc, gap = 6, 10
sheet = Image.new("RGB", (canvas.width + gap * 3 + canvas.width * sc,
                          canvas.height * sc + gap * 2), (35, 40, 55))
sheet.paste(canvas, (gap, (sheet.height - canvas.height) // 2), canvas)
big = canvas.resize((canvas.width * sc, canvas.height * sc), Image.NEAREST)
sheet.paste(big, (gap * 2 + canvas.width, gap), big)
sheet.save(CONTACT)
print(f"wrote {OUT} and {CONTACT}")
