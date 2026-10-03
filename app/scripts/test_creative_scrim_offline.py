"""test_creative_scrim_offline.py — Kreativ studiya: matn surat ustida
o'qilishi uchun avtomatik "scrim" (`creative_studio._auto_scrim`):

  1. Tekis to'q fonda oq matn -- kontrast yetarli, rasm O'ZGARMAYDI.
  2. Ola-bula (yorug'/qorong'i chiziqli) fonda oq matn -- matn ostidagi
     o'rtacha kontrast WCAG 4.5 dan oshadi (qoraytiriladi), tepasi tegilmaydi.
  3. Yorug' fonda qora matn -- oq yoritish chiziladi (fon yorug'lashadi).
  4. `no_scrim`, yashirin, bo'sh / placeholder matn -- e'tiborsiz qoladi.
  5. `render_composite` oxirigacha ishlaydi.
Tashqi tarmoq so'rovi yo'q.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(tempfile.mkdtemp(), 'scrim.db')}")

from PIL import Image, ImageDraw  # noqa: E402

import creative_studio as cs  # noqa: E402

W = H = 400
FAILED = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILED.append(name)


def text_layer(**kw):
    d = {"type": "text", "text": "Yangi kolleksiya", "x": 0.05, "y": 0.75, "w": 0.9, "h": 0.15, "color": "#FFFFFF"}
    d.update(kw)
    return d


def busy_canvas():
    im = Image.new("RGBA", (W, H), (255, 255, 255, 255))
    d = ImageDraw.Draw(im)
    for x in range(0, W, 20):
        d.rectangle((x, 0, x + 9, H), fill=(20, 20, 20, 255))
    return im


def region_contrast(canvas, layer):
    x, y = int(layer["x"] * W), int(layer["y"] * H)
    reg = canvas.crop((x, y, x + int(layer["w"] * W), y + int(layer["h"] * H))).convert("RGB")
    px = list(reg.getdata())
    avg = tuple(sum(p[i] for p in px) / len(px) for i in range(3))
    return cs._contrast(cs._rel_luminance(cs._hex_to_rgb(layer["color"])), cs._rel_luminance(avg))


# 1. Tekis to'q fon
solid = Image.new("RGBA", (W, H), (15, 23, 42, 255))
before = solid.tobytes()
cs._auto_scrim(solid, [text_layer()], W, H)
check("tekis to'q fon + oq matn -> o'zgarishsiz", solid.tobytes() == before)

# 2. Ola-bula fon, oq matn
busy = busy_canvas()
L = text_layer()
c0 = region_contrast(busy, L)
top_before = busy.crop((0, 0, W, 60)).tobytes()
cs._auto_scrim(busy, [L], W, H)
c1 = region_contrast(busy, L)
check(f"ola-bula fon: kontrast {c0:.2f} -> {c1:.2f} (>=4.5)", c0 < 4.5 <= c1)
check("pastki matn uchun scrim tepani o'zgartirmaydi", busy.crop((0, 0, W, 60)).tobytes() == top_before)

# 3. Kulrang fon, qora matn -> yoritish
light = Image.new("RGBA", (W, H), (100, 100, 100, 255))
D = text_layer(color="#111111", y=0.05)
c0 = region_contrast(light, D)
cs._auto_scrim(light, [D], W, H)
px = light.getpixel((W // 2, int(0.1 * H)))
check("to'q kulrang fonda qora matn: fon yorug'lashdi", px[0] > 100 and region_contrast(light, D) >= 4.5 and region_contrast(light, D) > c0)

# 4. no_scrim / yashirin / bo'sh / placeholder
for name, lay in (("no_scrim", text_layer(no_scrim=True)), ("bo'sh matn", text_layer(text="  ")),
                  ("placeholder", text_layer(text="{{headline}}")), ("yashirin", text_layer(hidden=True))):
    im = busy_canvas()
    b = im.tobytes()
    cs._auto_scrim(im, [lay], W, H)
    check(f"{name} -> e'tiborsiz", im.tobytes() == b)

# 5. render_composite oxirigacha
tmp = Path(tempfile.mkdtemp())
src = tmp / "src.png"
busy_canvas().convert("RGB").save(src)
out = tmp / "out.png"
cs.render_composite(src, [text_layer()], None, out, target_size=(W, H))
with Image.open(out) as o:
    check("render_composite: PNG to'g'ri o'lchamda", o.size == (W, H))

print()
if FAILED:
    print(f"{len(FAILED)} ta test YIQILDI: {FAILED}")
    sys.exit(1)
print("Hammasi o'tdi.")
