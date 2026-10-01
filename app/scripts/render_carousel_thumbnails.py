"""render_carousel_thumbnails.py — Kreativ studiya: 6 ta KARUSEL uslubining
har biri uchun REAL namuna (4 ta karta yonma-yon, "lenta") rasmini
OpenAI'SIZ chizib `app/static/creative_templates/carousel_<key>.png` qilib
saqlaydi. Galereya shu rasmlarni ko'rsatadi (tashqi so'rov yo'q).

Hammasi AYNAN `creative_carousel.card_layers()` + `creative_studio.
render_composite()` orqali -- ya'ni preview haqiqiy karusel qanday
chiqishini halol ko'rsatadi. Fon -- `render_template_thumbnails._scene()`
(foto-ga o'xshash protseduraviy sahna) yoki uslubning o'z foni.

Uslub o'zgartirilsa qayta ishga tushiring:
    cd app && python3 scripts/render_carousel_thumbnails.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(tempfile.mkdtemp(), 'thumbs.db')}")

from PIL import Image  # noqa: E402

import creative_carousel  # noqa: E402
import creative_studio  # noqa: E402
import creative_templates  # noqa: E402
import render_template_thumbnails as base_thumbs  # noqa: E402

OUT_DIR = base_thumbs.OUT_DIR
CARD_PX = 1080
CARD_THUMB = 300  # galereyada bitta karta ~150px, 2x retina
GAP = 16

# Har bir uslubga mos sahna (mavjud shablon sahnasi) va real o'zbekcha matn.
SAMPLES = {
    "gradient_bold": ("tech_gradient", {
        "headline": "Yangi smartfonlar keldi", "subheadline": "Rasmiy kafolat, muddatli to'lov", "offer_text": "-20%",
        "features": ["Rasmiy 1 yil kafolat", "0% muddatli to'lov", "Bugun yetkazamiz"],
        "price_text": "3 490 000 so'm", "cta_text": "Narxini bilish", "phone_line": "Tel: +998 90 123 45 67"}),
    "minimal_white": ("minimal_clean", {
        "headline": "Sifatli mebel — qulay narxda", "subheadline": "Toshkent bo'ylab bepul yetkazib beramiz",
        "features": ["Tabiiy yog'och", "10 yil kafolat", "Bepul yig'ib beramiz"],
        "price_text": "2 990 000 so'm", "cta_text": "Buyurtma bering", "phone_line": "Tel: +998 90 123 45 67"}),
    "dark_luxury": ("luxury_dark", {
        "headline": "Premium charm sumkalar", "subheadline": "Cheklangan kolleksiya",
        "features": ["Italiya charmi", "Qo'lda tikilgan", "Sovg'a qadog'i"],
        "price_text": "1 850 000 so'm", "cta_text": "Kolleksiyani ko'rish", "phone_line": "Tel: +998 90 123 45 67"}),
    "split_color": ("fashion_editorial", {
        "headline": "Kuzgi kolleksiya", "subheadline": "Yangi modellar har hafta", "offer_text": "-30%",
        "features": ["Turkiya sifati", "S dan XXL gacha", "Almashtirish 14 kun"],
        "price_text": "399 000 so'm", "cta_text": "Hoziroq yozing", "phone_line": "Tel: +998 90 123 45 67"}),
    "numbered_steps": ("education_friendly", {
        "headline": "IELTS 7.0 — 3 oyda", "subheadline": "Bepul sinov darsi bor",
        "features": ["Darajangizni aniqlaymiz", "Shaxsiy reja tuzamiz", "Haftalik mock test"],
        "price_text": "690 000 so'm / oy", "cta_text": "Sinov darsiga yoziling", "phone_line": "Tel: +998 90 123 45 67"}),
    "sticker_pop": ("warm_food", {
        "headline": "Burger kombo!", "subheadline": "Faqat shu hafta", "offer_text": "2+1",
        "features": ["100% mol go'shti", "30 daqiqada yetkazamiz", "Kartoshka bepul"],
        "price_text": "49 000 so'm", "cta_text": "Buyurtma berish", "phone_line": "Tel: +998 90 123 45 67"}),
}


def render_all() -> list[Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp())
    creative_studio.BRAND_ROOT = tmp
    base_thumbs._sample_logo(tmp / "logo_light.png", False)
    base_thumbs._sample_logo(tmp / "logo_dark.png", True)
    written = []
    for style in creative_carousel.CAROUSEL_STYLES:
        scene_key, sample = SAMPLES[style["key"]]
        tpl = creative_templates.get_template(scene_key)
        scene = base_thumbs._scene(tpl, (CARD_PX, CARD_PX))
        base_path = tmp / f"{style['key']}_base.png"
        scene.save(base_path, format="PNG")
        cards = creative_carousel.plan_cards(sample, 4)
        light_logo = style["key"] not in ("minimal_white", "numbered_steps")
        kit = base_thumbs._FakeBrandKit("logo_light.png" if light_logo else "logo_dark.png")
        thumbs = []
        for i, card in enumerate(cards, start=1):
            layers = creative_carousel.card_layers(style, card, sample, i, len(cards), style["accents"], "1:1")
            out = tmp / f"{style['key']}_{i}.png"
            creative_studio.render_composite(base_path, layers, kit, out, target_size=(CARD_PX, CARD_PX))
            thumbs.append(Image.open(out).resize((CARD_THUMB, CARD_THUMB), Image.LANCZOS))
        strip = Image.new("RGB", (CARD_THUMB * len(thumbs) + GAP * (len(thumbs) - 1), CARD_THUMB), (255, 255, 255))
        for k, im in enumerate(thumbs):
            strip.paste(im, (k * (CARD_THUMB + GAP), 0))
        path = OUT_DIR / f"carousel_{style['key']}.png"
        strip.save(path, format="PNG", optimize=True)
        written.append(path)
        print(f"OK  {path.name} ({strip.width}x{strip.height})")
    return written


if __name__ == "__main__":
    paths = render_all()
    print(f"\n{len(paths)} ta karusel preview yozildi: {OUT_DIR}")
