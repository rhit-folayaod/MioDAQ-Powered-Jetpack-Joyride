"""
Builds 'manager scientist' sprites: extracts frames from the sprite sheet and
swaps in a photo head. All tunables are at the top so you can re-run and eyeball.
"""
from PIL import Image, ImageDraw, ImageFilter
import numpy as np
from scipy.ndimage import label
import os, sys

SHEET_PATH = '/mnt/user-data/uploads/Scientist.jpg'
PHOTO_PATH = '/mnt/user-data/uploads/Austin_Faceshot.jfif'
OUT = '/home/claude/manager_sprites'

# ---- TUNABLES -------------------------------------------------
HEAD_SCALE   = 1.35   # 1.0 = same width as the sprite's own head. >1 = bobblehead (more readable at small px)
HEAD_Y_NUDGE = -0.15  # fraction of head height; negative = move head up
HEAD_X_NUDGE = 0.0    # fraction of head width; positive = move head right
HEAD_BOX     = (52, 2, 152, 108)   # crop from the photo: x0,y0,x1,y1
# ---------------------------------------------------------------

os.makedirs(OUT, exist_ok=True)
sheet = Image.open(SHEET_PATH).convert('RGB')
photo = Image.open(PHOTO_PATH).convert('RGB')

def cutout(x0, y0, x1, y1):
    """Crop a sprite; only border-connected white becomes transparent,
    so the white lab coat stays opaque."""
    sub = np.array(sheet.crop((x0, y0, x1, y1))).astype(int)
    white = (np.abs(sub - 255).sum(axis=2) < 110)
    lab, _ = label(white)
    border = set(lab[0,:]) | set(lab[-1,:]) | set(lab[:,0]) | set(lab[:,-1])
    border.discard(0)
    alpha = np.where(np.isin(lab, list(border)), 0, 255).astype(np.uint8)
    return Image.fromarray(np.dstack([sub.astype(np.uint8), alpha]), 'RGBA')

ROW1 = [(30,60),(60,90),(93,123),(127,156),(158,186),(190,218),(223,251),(255,283)]
ROW2 = [(106,126),(129,151),(159,179),(187,206)]
frames = [cutout(a,2,b+1,51) for a,b in ROW1] + [cutout(a-1,56,b+2,108) for a,b in ROW2]

head_src = photo.crop(HEAD_BOX)

def make_head(target_w):
    ar = head_src.height / head_src.width
    w = max(6, int(round(target_w))); h = max(6, int(round(target_w*ar)))
    ss = 6
    big = head_src.resize((w*ss, h*ss), Image.LANCZOS)
    m = Image.new('L', (w*ss, h*ss), 0)
    ImageDraw.Draw(m).ellipse([w*ss*0.03, h*ss*0.03, w*ss*0.97, h*ss*0.97], fill=255)
    m = m.filter(ImageFilter.GaussianBlur(w*ss*0.04))
    big.putalpha(m)
    return big.resize((w, h), Image.LANCZOS)

def head_metrics(img):
    """Locate the sprite's own head: the connected blob containing the topmost pixel."""
    op = np.array(img)[:,:,3] > 128
    ys, xs = np.where(op)
    top = ys.min()
    lab, _ = label(op)
    top_xs = xs[ys == top]
    blob_id = lab[top, int(np.median(top_xs))]
    blob = (lab == blob_id)
    # restrict to the upper portion of that blob = the head, not the whole body
    bys, bxs = np.where(blob)
    depth = max(5, int((bys.max()-bys.min()+1) * 0.22))
    sel = blob[top:top+depth, :]
    sy, sx = np.where(sel)
    return top, sx.mean(), (sx.max()-sx.min()+1), depth

final, report = [], []
for i, f in enumerate(frames):
    top, cx, hw, depth = head_metrics(f)
    hd = make_head(hw * HEAD_SCALE)
    padx = pady = 12
    canvas = Image.new('RGBA', (f.width+padx*2, f.height+pady*2), (0,0,0,0))
    canvas.paste(f, (padx, pady), f)
    hx = int(round(padx + cx - hd.width/2 + hd.width*HEAD_X_NUDGE))
    hy = int(round(pady + top + depth*0.5 - hd.height*0.5 + hd.height*HEAD_Y_NUDGE))
    canvas.alpha_composite(hd, (max(0,hx), max(0,hy)))
    final.append(canvas)
    report.append((i, f.size, round(float(cx),1), int(hw), hd.size, (hx,hy)))

for r in report: print(r)

names = [f'manager_run_{i:02d}.png' for i in range(8)] + [f'manager_down_{i:02d}.png' for i in range(4)]
for f, n in zip(final, names):
    f.save(f'{OUT}/{n}')

mw, mh = max(f.width for f in final), max(f.height for f in final)
cols, rown, pad, sc = 4, 3, 6, 6
cs = Image.new('RGB', (cols*(mw+pad)+pad, rown*(mh+pad)+pad), (35,38,48))
for i,f in enumerate(final):
    cs.paste(f, (pad+(i%cols)*(mw+pad), pad+(i//cols)*(mh+pad)), f)
cs.resize((cs.width*sc, cs.height*sc), Image.NEAREST).save('/home/claude/contact_manager2.png')
print('wrote', len(final), 'frames to', OUT)
