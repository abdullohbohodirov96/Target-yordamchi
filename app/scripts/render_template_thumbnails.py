"""render_template_thumbnails.py — Kreativ studiya: 20 ta shablonning
HAR BIRI uchun NAMUNA (preview) rasmini OpenAI'SIZ, faqat Pillow bilan
chizib `app/static/creative_templates/<key>.png` qilib saqlaydi.

Bu BIR MARTALIK skript -- natija (statik PNG'lar) git'ga commit qilinadi,
shablon galereyasi shu rasmlarni ko'rsatadi (hech qanday tashqi so'rov
yubormaydi). Shablon dizayni o'zgartirilsa, qayta ishga tushiring:

    cd app && python3 scripts/render_template_thumbnails.py

2026-09 (foydalanuvchi shikoyati: "shablon rasmlari chunarsiz" -- avvalgi
preview'lar faqat bir rangli/gradient fon ustidagi "Mahsulot nomi" edi):
endi har bir preview HAQIQIY reklama kabi ko'rinadi:
  - fon -- shablon kategoriyasiga mos, foto-ga o'xshash PROTSEDURAVIY
    sahna (yumshoq gradientlar, nur dog'lari, mahsulot/ob'ektni eslatuvchi
    shakllar, bokeh, blur) -- `_scene()`; haqiqiy foto emas, lekin tekis
    rang bloki ham emas;
  - matn -- har bir shablon uchun REAL o'zbekcha namuna (sarlavha, tavsif,
    chaqiriq, aksiya, narx, xususiyatlar) -- `SAMPLES`;
  - logotip -- shaffof fonli namunaviy "wordmark" (haqiqiy `_draw_logo`
    yo'li orqali, brend kit obyekti bilan) -- foydalanuvchi qayerda va
    qanday o'lchamda logotip turishini ko'radi;
  - hammasi AYNAN `creative_studio.render_composite()` orqali -- ya'ni
    preview haqiqiy generatsiya qanday chiqishini HALOL ko'rsatadi (haddan
    tashqari va'da bermaydi).
"""
import os
import sys
import math
import random
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# `db` importi DATABASE_URL talab qilmaydi (engine=None bo'lsa ham modul
# yuklanadi) -- lekin xavfsizlik uchun bo'sh SQLite beramiz.
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(tempfile.mkdtemp(), 'thumbs.db')}")

from PIL import Image, ImageDraw, ImageFilter, ImageFont  # noqa: E402

import creative_templates  # noqa: E402
import creative_studio  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent.parent / "static" / "creative_templates"
# Galereya kartasi ~300px kenglikda; 2x (retina) uchun 600.
THUMB_WIDTH = 600

# Har bir shablon uchun REAL o'zbekcha namuna matnlar (kategoriyaga mos).
_BASE = {
    "offer_text": "-30%", "price_text": "2 990 000 so'm", "brand_name": "Brend",
    "quote_text": "“Juda sifatli, tez yetkazib berishdi!”",
    "feature_1": "Sifat kafolati", "feature_2": "Bepul yetkazish", "feature_3": "1 yil kafolat", "feature_4": "Muddatli to'lov",
}
SAMPLES = {
    "minimal_clean": {"headline": "Sifatli mebel — qulay narxda", "subheadline": "Toshkent bo'ylab bepul yetkazib beramiz", "cta_text": "Buyurtma bering"},
    "bold_sale": {"headline": "Yozgi katta chegirma", "subheadline": "Faqat 3 kun — barcha mahsulotlarga", "cta_text": "Hoziroq xarid qiling", "offer_text": "-30%"},
    "luxury_dark": {"headline": "Premium charm sumkalar", "subheadline": "Cheklangan kolleksiya, qo'lda tikilgan", "cta_text": "Kolleksiyani ko'ring"},
    "tech_gradient": {"headline": "Yangi smartfonlar keldi", "subheadline": "Rasmiy kafolat va muddatli to'lov", "cta_text": "Narxini bilib oling"},
    "warm_food": {"headline": "Tandir somsa — issiq va mazali", "subheadline": "30 daqiqada yetkazib beramiz", "cta_text": "Buyurtma bering", "offer_text": "2 ta olsangiz, 3-chisi bepul"},
    "real_estate_clean": {"headline": "Yunusobodda 3 xonali kvartira", "subheadline": "Yangi bino, remont bilan, 78 m²", "cta_text": "Ko'rishga yoziling", "price_text": "85 000 $ dan"},
    "fashion_editorial": {"headline": "Kuzgi kolleksiya 2026", "subheadline": "Ayollar uchun palto va kostyumlar", "cta_text": "Yangi kolleksiya →"},
    "before_after_split": {"headline": "Natija 1 oyda ko'rinadi", "subheadline": "Kosmetolog nazoratida, kafolat bilan", "cta_text": "Natijani ko'ring"},
    "countdown_urgency": {"headline": "Aksiya tugashiga 2 kun qoldi", "subheadline": "Kondisionerlarga maxsus narx", "cta_text": "Ulgurib qoling", "offer_text": "-40%"},
    "testimonial_quote": {"headline": "Dilnoza R., Toshkent", "subheadline": "500+ mamnun mijoz", "cta_text": "Siz ham sinab ko'ring", "quote_text": "“Yetkazib berish tez, sifati a'lo — hammaga tavsiya qilaman!”"},
    "feature_grid": {"headline": "Nega aynan bizni tanlashadi?", "cta_text": "Buyurtma bering", "feature_1": "Sifat kafolati", "feature_2": "Bepul yetkazish", "feature_3": "1 yil kafolat", "feature_4": "Muddatli to'lov"},
    "new_arrival": {"headline": "Yangi model — endi sotuvda", "subheadline": "Birinchi 100 ta xaridorga sovg'a", "cta_text": "Birinchilardan bo'ling"},
    "seasonal_promo": {"headline": "Navro'z sovg'alari tayyor", "subheadline": "Yaqinlaringizni xursand qiling", "cta_text": "Sovg'a tanlang", "offer_text": "-25%"},
    "corporate_professional": {"headline": "Biznesingiz uchun buxgalteriya xizmati", "subheadline": "Hisobotlar o'z vaqtida, jarimalarsiz", "cta_text": "Taklif oling", "feature_1": "Soliq hisoboti", "feature_2": "1C yuritish", "feature_3": "Konsultatsiya"},
    "beauty_soft": {"headline": "Yuz parvarishi — 20% chegirma", "subheadline": "Professional kosmetologlar, steril jihozlar", "cta_text": "Navbatga yoziling"},
    "auto_dynamic": {"headline": "Avto detallar — 1 kunda yetkazamiz", "subheadline": "Original va analog, kafolat bilan", "cta_text": "Hozir bog'laning", "price_text": "150 000 so'mdan"},
    "education_friendly": {"headline": "Ingliz tili — 0 dan IELTS gacha", "subheadline": "Birinchi dars bepul, kichik guruhlar", "cta_text": "Bepul darsga yoziling"},
    "collage_multi": {"headline": "Butun oila uchun poyabzal", "subheadline": "200 dan ortiq model bir joyda", "cta_text": "Katalogni ko'ring", "feature_1": "Erkaklar", "feature_2": "Ayollar", "feature_3": "Bolalar", "feature_4": "Sport"},
    "story_fullbleed": {"headline": "Bugun oxirgi kun!", "subheadline": "Barcha tovarlarga -20%", "cta_text": "Yuqoriga suring ↑"},
    "price_tag_highlight": {"headline": "Kir yuvish mashinasi", "subheadline": "8 kg, inverter, 2 yil kafolat", "cta_text": "Hoziroq buyurtma bering", "price_text": "3 490 000 so'm"},
}


def _thumb_size(aspect: str) -> "tuple[int, int]":
    w, h = creative_studio._target_pixels_for_aspect(aspect)
    return THUMB_WIDTH, round(THUMB_WIDTH * h / w)


def _hex(c):
    return creative_studio._hex_to_rgb(c)


def _mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _lighten(c, t):
    return _mix(c, (255, 255, 255), t)


def _darken(c, t):
    return _mix(c, (0, 0, 0), t)


def _radial(size, center, radius, color, alpha_max=200):
    """Yumshoq nur dog'i (bokeh / spotlight) -- RGBA qatlam."""
    w, h = size
    layer = Image.new("RGBA", size, color + (0,))
    mask = Image.new("L", size, 0)
    d = ImageDraw.Draw(mask)
    cx, cy = center
    steps = 24
    for i in range(steps, 0, -1):
        r = radius * i / steps
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=int(alpha_max * (1 - i / steps) ** 1.6))
    mask = mask.filter(ImageFilter.GaussianBlur(radius * 0.25))
    layer.putalpha(mask)
    return layer


def _shadow_shape(size, box, color=(0, 0, 0), alpha=120, blur=18, shape="ellipse"):
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    if shape == "ellipse":
        d.ellipse(box, fill=color + (alpha,))
    else:
        d.rounded_rectangle(box, radius=int(min(box[2] - box[0], box[3] - box[1]) * 0.12), fill=color + (alpha,))
    return layer.filter(ImageFilter.GaussianBlur(blur))


def _product_block(canvas, box, color, radius_ratio=0.12, shine=True):
    """Mahsulotni eslatuvchi yumaloq to'rtburchak "quti" + soya + yaltiroq."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    canvas.alpha_composite(_shadow_shape(canvas.size, (x0 + w * 0.05, y1 - h * 0.12, x1 - w * 0.05, y1 + h * 0.10), alpha=110, blur=int(w * 0.06)))
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle(box, radius=int(min(w, h) * radius_ratio), fill=color + (255,))
    # Yuqori chap yaltiroq
    if shine:
        d.rounded_rectangle((x0 + w * 0.08, y0 + h * 0.06, x0 + w * 0.45, y0 + h * 0.20), radius=int(h * 0.06), fill=_lighten(color, 0.5) + (110,))
        d.rounded_rectangle((x0 + w * 0.08, y0 + h * 0.30, x1 - w * 0.08, y1 - h * 0.10), radius=int(h * 0.04), fill=_darken(color, 0.2) + (90,))
    canvas.alpha_composite(layer)


def _scene(tpl: dict, size: "tuple[int, int]") -> Image.Image:
    """Shablon kategoriyasiga mos foto-ga o'xshash protseduraviy sahna (RGB)."""
    w, h = size
    rnd = random.Random(tpl["key"])  # deterministik -- har safar bir xil rasm
    bg = tpl.get("background") or {}
    colors = [_hex(c) for c in (bg.get("colors") or ["#EEEEEE"])]
    c1, c2 = colors[0], colors[-1]
    base = creative_studio.background_image(bg, size).convert("RGBA")
    cat = tpl["category"]

    # 1) Yumshoq yorug'lik/vignetka -- har qanday sahnaga "foto" chuqurligi
    light = _lighten(_mix(c1, c2, 0.5), 0.35)
    base.alpha_composite(_radial(size, (int(w * 0.65), int(h * 0.25)), int(w * 0.75), light, 150))
    vign = Image.new("RGBA", size, (0, 0, 0, 0))
    vd = ImageDraw.Draw(vign)
    vd.rectangle((0, 0, w, h), fill=(0, 0, 0, 70))
    vd.ellipse((-w * 0.1, -h * 0.1, w * 1.1, h * 1.1), fill=(0, 0, 0, 0))
    base.alpha_composite(vign.filter(ImageFilter.GaussianBlur(int(w * 0.12))))

    # 2) Kategoriyaga xos "ob'ekt"
    if cat in ("universal", "tech", "launch", "features", "sale"):
        # Studiya podiumi + mahsulot qutisi (elektronika/quti)
        accent = _darken(_mix(c1, c2, 0.5), 0.45) if cat != "universal" else (36, 41, 52)
        base.alpha_composite(_shadow_shape(size, (w * 0.18, h * 0.50, w * 0.82, h * 0.62), alpha=90, blur=int(w * 0.05)))
        _product_block(base, (w * 0.36, h * 0.18, w * 0.64, h * 0.56), accent, radius_ratio=0.10)
        d = ImageDraw.Draw(base)
        d.rounded_rectangle((w * 0.40, h * 0.23, w * 0.60, h * 0.50), radius=int(w * 0.02), fill=_lighten(accent, 0.18) + (255,))
        if cat == "sale":
            for _ in range(14):
                cx, cy, r = rnd.uniform(0, w), rnd.uniform(0, h * 0.6), rnd.uniform(w * 0.02, w * 0.07)
                base.alpha_composite(_radial(size, (cx, cy), r, (255, 240, 200), 140))
    elif cat == "luxury":
        gold = (201, 162, 39)
        base.alpha_composite(_radial(size, (int(w * 0.5), int(h * 0.30)), int(w * 0.5), gold, 90))
        base.alpha_composite(_shadow_shape(size, (w * 0.25, h * 0.52, w * 0.75, h * 0.60), color=gold, alpha=60, blur=int(w * 0.04)))
        _product_block(base, (w * 0.34, h * 0.22, w * 0.66, h * 0.60), (48, 36, 26), radius_ratio=0.16)
        d = ImageDraw.Draw(base)
        d.rounded_rectangle((w * 0.44, h * 0.18, w * 0.56, h * 0.27), radius=int(w * 0.02), fill=gold + (255,))
    elif cat == "food":
        # Yog'och stol + tarelka
        d = ImageDraw.Draw(base)
        for i in range(9):
            y = h * (0.05 + i * 0.11)
            d.rectangle((0, y, w, y + h * 0.10), fill=_mix(c1, c2, 0.3 + 0.05 * (i % 3)) + (255,))
        base.alpha_composite(_shadow_shape(size, (w * 0.20, h * 0.20, w * 0.80, h * 0.70), alpha=120, blur=int(w * 0.05)))
        d = ImageDraw.Draw(base)
        d.ellipse((w * 0.22, h * 0.14, w * 0.78, h * 0.66), fill=(246, 240, 228, 255))
        d.ellipse((w * 0.27, h * 0.19, w * 0.73, h * 0.61), fill=(233, 224, 206, 255))
        for _ in range(7):
            cx, cy = rnd.uniform(w * 0.34, w * 0.66), rnd.uniform(h * 0.27, h * 0.53)
            r = rnd.uniform(w * 0.05, w * 0.09)
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=_mix((214, 120, 60), (150, 70, 30), rnd.random()) + (255,))
        base = base.filter(ImageFilter.GaussianBlur(0.6))
    elif cat == "real_estate":
        d = ImageDraw.Draw(base)
        sky = (198, 222, 240)
        d.rectangle((0, 0, w, h * 0.62), fill=sky + (255,))
        base.alpha_composite(_radial(size, (int(w * 0.8), int(h * 0.1)), int(w * 0.35), (255, 250, 235), 160))
        d = ImageDraw.Draw(base)
        for bx, top, wid, col in ((0.05, 0.22, 0.30, (226, 232, 240)), (0.40, 0.12, 0.40, (241, 245, 249)), (0.75, 0.30, 0.22, (203, 213, 225))):
            x0, x1 = w * bx, w * (bx + wid)
            d.rectangle((x0, h * top, x1, h * 0.66), fill=col + (255,))
            for fy in range(int(h * top) + int(h * 0.03), int(h * 0.62), int(h * 0.055)):
                for fx in range(int(x0) + int(w * 0.02), int(x1) - int(w * 0.03), int(w * 0.055)):
                    d.rectangle((fx, fy, fx + w * 0.03, fy + h * 0.03), fill=(120, 150, 185, 255))
        d.rectangle((0, h * 0.62, w, h * 0.72), fill=(180, 190, 200, 255))
    elif cat == "fashion":
        # Studiya fon + siluet (uzun ellips) chapdan o'ngga yorug'lik
        base.alpha_composite(_radial(size, (int(w * 0.3), int(h * 0.3)), int(w * 0.7), (255, 255, 250), 120))
        base.alpha_composite(_shadow_shape(size, (w * 0.50, h * 0.66, w * 0.90, h * 0.74), alpha=100, blur=int(w * 0.04)))
        layer = Image.new("RGBA", size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        d.ellipse((w * 0.62, h * 0.08, w * 0.78, h * 0.24), fill=(60, 45, 40, 255))
        d.rounded_rectangle((w * 0.52, h * 0.24, w * 0.88, h * 0.70), radius=int(w * 0.10), fill=(76, 58, 50, 255))
        d.rounded_rectangle((w * 0.58, h * 0.30, w * 0.82, h * 0.68), radius=int(w * 0.06), fill=(110, 84, 70, 255))
        base.alpha_composite(layer)
    elif cat == "testimonial" and tpl["key"] == "before_after_split":
        d = ImageDraw.Draw(base)
        d.rectangle((0, 0, w * 0.5, h), fill=(96, 104, 116, 255))
        d.rectangle((w * 0.5, 0, w, h), fill=(232, 240, 236, 255))
        for side, col in ((0.0, (140, 146, 156)), (0.5, (120, 190, 150))):
            base.alpha_composite(_shadow_shape(size, (w * (side + 0.12), h * 0.56, w * (side + 0.38), h * 0.62), alpha=90, blur=int(w * 0.03)))
            _product_block(base, (w * (side + 0.15), h * 0.16, w * (side + 0.35), h * 0.58), col, radius_ratio=0.3, shine=False)
    elif cat == "testimonial":
        base.alpha_composite(_radial(size, (int(w * 0.5), int(h * 0.5)), int(w * 0.7), (255, 255, 255), 90))
        d = ImageDraw.Draw(base)
        for i in range(5):
            cx, cy = w * (0.12 + i * 0.19), h * 0.955
            d.polygon([(cx + w * 0.02 * math.cos(math.radians(90 + k * 72)), cy - h * 0.012 * math.sin(math.radians(90 + k * 72)) * 1.8) for k in range(5)], fill=(232, 178, 60, 255))
    elif cat == "urgency":
        base.alpha_composite(_radial(size, (int(w * 0.5), int(h * 0.35)), int(w * 0.55), (255, 120, 90), 170))
        layer = Image.new("RGBA", size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        for i in range(6):
            d.line((w * 0.5, h * 0.35, w * (0.5 + 0.9 * math.cos(math.radians(200 + i * 26))), h * (0.35 + 0.9 * math.sin(math.radians(200 + i * 26)))), fill=(255, 200, 180, 40), width=int(w * 0.03))
        base.alpha_composite(layer.filter(ImageFilter.GaussianBlur(4)))
        _product_block(base, (w * 0.38, h * 0.18, w * 0.62, h * 0.50), (40, 20, 24), radius_ratio=0.12)
    elif cat == "seasonal":
        for _ in range(28):
            cx, cy, r = rnd.uniform(0, w), rnd.uniform(0, h * 0.7), rnd.uniform(w * 0.015, w * 0.06)
            base.alpha_composite(_radial(size, (cx, cy), r, (255, 214, 120), 150))
        _product_block(base, (w * 0.36, h * 0.24, w * 0.64, h * 0.50), (150, 30, 50), radius_ratio=0.08)
        d = ImageDraw.Draw(base)
        d.rectangle((w * 0.485, h * 0.24, w * 0.515, h * 0.50), fill=(252, 211, 77, 255))
        d.rectangle((w * 0.36, h * 0.355, w * 0.64, h * 0.385), fill=(252, 211, 77, 255))
    elif cat == "b2b":
        layer = Image.new("RGBA", size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        for i in range(7):
            x = w * (0.1 + i * 0.13)
            d.line((x, 0, x + w * 0.35, h), fill=(255, 255, 255, 18), width=int(w * 0.03))
        base.alpha_composite(layer)
        d = ImageDraw.Draw(base)
        for i, hh in enumerate((0.30, 0.42, 0.36, 0.50, 0.44)):
            x0 = w * (0.10 + i * 0.16)
            d.rectangle((x0, h * (0.60 - hh), x0 + w * 0.10, h * 0.60), fill=_lighten(_mix(c1, c2, 0.5), 0.15 + 0.1 * i) + (255,))
    elif cat == "beauty":
        base.alpha_composite(_radial(size, (int(w * 0.35), int(h * 0.3)), int(w * 0.6), (255, 255, 255), 140))
        base.alpha_composite(_shadow_shape(size, (w * 0.30, h * 0.60, w * 0.70, h * 0.66), alpha=70, blur=int(w * 0.04)))
        layer = Image.new("RGBA", size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        d.rounded_rectangle((w * 0.42, h * 0.20, w * 0.58, h * 0.62), radius=int(w * 0.07), fill=(250, 235, 240, 255))
        d.rounded_rectangle((w * 0.45, h * 0.14, w * 0.55, h * 0.22), radius=int(w * 0.02), fill=(220, 170, 190, 255))
        d.rounded_rectangle((w * 0.45, h * 0.34, w * 0.55, h * 0.50), radius=int(w * 0.01), fill=(236, 200, 214, 255))
        base.alpha_composite(layer)
    elif cat == "auto":
        layer = Image.new("RGBA", size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        for i in range(5):
            y = h * (0.15 + i * 0.12)
            d.line((0, y + h * 0.2, w, y - h * 0.05), fill=(230, 40, 60, 90), width=int(h * 0.012))
        base.alpha_composite(layer.filter(ImageFilter.GaussianBlur(3)))
        base.alpha_composite(_shadow_shape(size, (w * 0.12, h * 0.55, w * 0.88, h * 0.66), alpha=160, blur=int(w * 0.04)))
        layer = Image.new("RGBA", size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        d.rounded_rectangle((w * 0.15, h * 0.34, w * 0.85, h * 0.58), radius=int(w * 0.06), fill=(30, 32, 38, 255))
        d.rounded_rectangle((w * 0.30, h * 0.22, w * 0.70, h * 0.40), radius=int(w * 0.06), fill=(45, 48, 56, 255))
        for cx in (0.28, 0.72):
            d.ellipse((w * (cx - 0.07), h * 0.50, w * (cx + 0.07), h * 0.64), fill=(15, 15, 18, 255))
            d.ellipse((w * (cx - 0.035), h * 0.535, w * (cx + 0.035), h * 0.605), fill=(120, 120, 130, 255))
        base.alpha_composite(layer)
    elif cat == "education":
        base.alpha_composite(_radial(size, (int(w * 0.5), int(h * 0.35)), int(w * 0.6), (255, 255, 255), 110))
        d = ImageDraw.Draw(base)
        for i, col in enumerate(((59, 130, 246), (16, 185, 129), (245, 158, 11))):
            d.rounded_rectangle((w * (0.25 + i * 0.04), h * (0.30 + i * 0.12), w * (0.75 - i * 0.04), h * (0.40 + i * 0.12)), radius=int(w * 0.015), fill=col + (255,))
        d.ellipse((w * 0.66, h * 0.18, w * 0.84, h * 0.36), fill=(253, 224, 71, 255))
    elif cat == "collage":
        d = ImageDraw.Draw(base)
        cols = ((120, 80, 60), (200, 60, 80), (60, 120, 180), (60, 150, 90))
        for i, col in enumerate(cols):
            gx, gy = i % 2, i // 2
            x0, y0 = w * (0.08 + gx * 0.44), h * (0.06 + gy * 0.27)
            base.alpha_composite(_shadow_shape(size, (x0, y0 + h * 0.02, x0 + w * 0.40, y0 + h * 0.25), alpha=80, blur=int(w * 0.03)))
            d = ImageDraw.Draw(base)
            d.rounded_rectangle((x0, y0, x0 + w * 0.40, y0 + h * 0.23), radius=int(w * 0.02), fill=(255, 255, 255, 255))
            d.rounded_rectangle((x0 + w * 0.06, y0 + h * 0.04, x0 + w * 0.34, y0 + h * 0.19), radius=int(w * 0.03), fill=col + (255,))
    elif cat == "story":
        base.alpha_composite(_radial(size, (int(w * 0.5), int(h * 0.30)), int(w * 0.9), (255, 235, 200), 150))
        base.alpha_composite(_shadow_shape(size, (w * 0.2, h * 0.52, w * 0.8, h * 0.58), alpha=120, blur=int(w * 0.05)))
        layer = Image.new("RGBA", size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        d.ellipse((w * 0.36, h * 0.12, w * 0.64, h * 0.27), fill=(90, 60, 50, 255))
        d.rounded_rectangle((w * 0.22, h * 0.27, w * 0.78, h * 0.58), radius=int(w * 0.14), fill=(200, 90, 70, 255))
        base.alpha_composite(layer)
    return base.convert("RGB")


class _FakeBrandKit:
    """`creative_studio.brand_logo_file_path()` uchun minimal obyekt."""

    def __init__(self, rel_path: str):
        self.logo_storage_path = rel_path
        self.logo_content_type = "image/png"


def _sample_logo(path: Path, dark: bool) -> None:
    """Shaffof fonli namunaviy wordmark-logotip (haqiqiy `_draw_logo` yo'li)."""
    img = Image.new("RGBA", (560, 200), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fg = (24, 24, 27, 255) if dark else (255, 255, 255, 255)
    d.rounded_rectangle((8, 30, 148, 170), radius=34, fill=fg)
    d.ellipse((48, 70, 108, 130), fill=(0, 0, 0, 0))
    font = ImageFont.truetype(str(creative_studio.FONT_BOLD), 96)
    d.text((176, 46), "BREND", font=font, fill=fg)
    img.save(path, format="PNG")


def _logo_is_dark(tpl: dict, scene: Image.Image, size) -> bool:
    """Logotip zonasining fon yorug'ligi -- och fon bo'lsa qora logotip."""
    layer = next((l for l in tpl["layers"] if l.get("type") == "logo"), None)
    if layer is None:
        return True
    w, h = size
    box = (int(layer["x"] * w), int(layer["y"] * h), int((layer["x"] + layer["w"]) * w), int((layer["y"] + layer["h"]) * h))
    region = scene.crop(box).resize((8, 8))
    lum = sum(0.299 * r + 0.587 * g + 0.114 * b for r, g, b in region.getdata()) / 64
    return lum > 140


def render_all() -> list[Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp())
    creative_studio.BRAND_ROOT = tmp  # namunaviy logotip uchun (real brend kit yo'q)
    _sample_logo(tmp / "logo_dark.png", True)
    _sample_logo(tmp / "logo_light.png", False)
    written = []
    for tpl in creative_templates.CREATIVE_TEMPLATES:
        size = _thumb_size(tpl["aspect_default"])
        scene = _scene(tpl, size)
        base_path = tmp / f"{tpl['key']}_base.png"
        scene.save(base_path, format="PNG")
        values = dict(_BASE)
        values.update(SAMPLES.get(tpl["key"], {}))
        layers = creative_studio.resolve_layers(tpl["layers"], values)
        kit = _FakeBrandKit("logo_dark.png" if _logo_is_dark(tpl, scene, size) else "logo_light.png")
        out_path = OUT_DIR / f"{tpl['key']}.png"
        creative_studio.render_composite(base_path, layers, kit, out_path, target_size=size)
        written.append(out_path)
        print(f"OK  {out_path.name} ({size[0]}x{size[1]})")
    return written


if __name__ == "__main__":
    paths = render_all()
    print(f"\n{len(paths)} ta shablon preview yozildi: {OUT_DIR}")
