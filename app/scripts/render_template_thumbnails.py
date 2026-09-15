"""render_template_thumbnails.py — Kreativ studiya: 20 ta shablonning
HAR BIRI uchun NAMUNA (preview) rasmini OpenAI'SIZ, faqat Pillow bilan
chizib `app/static/creative_templates/<key>.png` qilib saqlaydi.

Bu BIR MARTALIK skript -- natija (statik PNG'lar) git'ga commit qilinadi,
shablon galereyasi shu rasmlarni ko'rsatadi (hech qanday tashqi so'rov
yubormaydi). Shablon dizayni o'zgartirilsa, qayta ishga tushiring:

    cd app && python3 scripts/render_template_thumbnails.py

Fon -- shablonning `background`i (solid/gradient), matn -- o'rnida turgan
namuna ("Mahsulot nomi" / "Tavsif matni" / "Xarid qiling" ...). Logotip
qatlami bo'sh (brend kit yo'q -- o'tkazib yuboriladi), o'rniga kichik
"LOGO" belgisi chiziladi, foydalanuvchi qayerda logotip turishini ko'rsin.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# `db` importi DATABASE_URL talab qilmaydi (engine=None bo'lsa ham modul
# yuklanadi) -- lekin xavfsizlik uchun bo'sh SQLite beramiz.
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(tempfile.mkdtemp(), 'thumbs.db')}")

import creative_templates  # noqa: E402
import creative_studio  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent.parent / "static" / "creative_templates"
THUMB_WIDTH = 400

SAMPLE_VALUES = {
    "headline": "Mahsulot nomi",
    "subheadline": "Tavsif matni -- qisqa va aniq",
    "cta_text": "Xarid qiling",
    "offer_text": "-30%",
    "price_text": "299 000 so'm",
    "brand_name": "Brend",
    "quote_text": "“Juda sifatli, tez yetkazib berishdi!”",
    "feature_1": "Sifat kafolati",
    "feature_2": "Bepul yetkazish",
    "feature_3": "1 yil kafolat",
    "feature_4": "Qulay narx",
}


def _thumb_size(aspect: str) -> "tuple[int, int]":
    w, h = creative_studio._target_pixels_for_aspect(aspect)
    return THUMB_WIDTH, round(THUMB_WIDTH * h / w)


def render_all() -> list[Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for tpl in creative_templates.CREATIVE_TEMPLATES:
        size = _thumb_size(tpl["aspect_default"])
        base = creative_studio.background_image(tpl["background"], size)
        base_path = OUT_DIR / f".{tpl['key']}_base.png"
        base.save(base_path, format="PNG")
        layers = creative_studio.resolve_layers(tpl["layers"], SAMPLE_VALUES)
        # Logotip o'rnini ko'rsatuvchi belgi (haqiqiy logotip yo'q).
        for layer in layers:
            if layer.get("type") == "logo":
                layers.append({
                    "id": "logo_placeholder", "type": "badge", "x": layer["x"], "y": layer["y"],
                    "w": layer["w"], "h": layer["h"], "align": "center", "font": "regular",
                    "size_ratio": 0.022, "color": "#FFFFFF", "bg_color": "#000000", "opacity": 0.25, "text": "LOGO",
                })
                break
        out_path = OUT_DIR / f"{tpl['key']}.png"
        creative_studio.render_composite(base_path, layers, None, out_path, target_size=size)
        try:
            base_path.unlink()
        except OSError:
            pass
        written.append(out_path)
        print(f"OK  {out_path.name} ({size[0]}x{size[1]})")
    return written


if __name__ == "__main__":
    paths = render_all()
    print(f"\n{len(paths)} ta shablon preview yozildi: {OUT_DIR}")
