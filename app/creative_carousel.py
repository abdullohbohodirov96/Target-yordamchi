"""creative_carousel.py — Kreativ studiya: KARUSEL postlar (2026-09-30,
docs/PLAN.md 3-bosqich; egasi so'rovi: "karusel postlarni shablon qilib
ko'rsatish kerak, lekin real dizaynlarni, har xil turdagi uslubda").

Karusel -- 3-6 karta, bitta voqea:
  1-karta  -- "ilgak": sarlavha (+ taklif), surish belgisi;
  o'rtada  -- bittadan afzallik (raqamlangan);
  oxirgisi -- CTA: narx, chaqiriq, telefon, logotip.

6 ta UMUMAN boshqacha uslub (`CAROUSEL_STYLES`) -- har biri o'z qatlam
quruvchisiga ega. Matn/logotip/narx -- `creative_studio.render_composite()`
orqali Pillow bilan (AI faqat fon chizadi, 1-vazifa qoidasi).

Ikki manba:
  * tayyor kreativ (`source_asset`) -- uning fon rasmi va (tahrirlangan)
    matnlari qayta ishlatiladi;
  * manbasiz -- uslubning o'z fonidan (brend ranglari bilan), matnlar
    kompaniya profili/brifidan.
Ikkalasida ham OpenAI CHAQIRILMAYDI -- kvota sarflanmaydi.

Har bir karta -- oddiy `CreativeAsset` (muharrirda alohida tahrirlanadi).
Guruhlash `brief_answers_json` ichida (`carousel_group`, `carousel_index`,
`carousel_total`, `carousel_style`) -- baza tuzilishi o'zgarmaydi."""

import io
import re
import secrets
import logging

import db
import storage_backend
import company_context as company_context_module
import creative_studio as cs
from creative_studio import CreativeError, _v_badge, _v_logo, _v_panel, _v_text

logger = logging.getLogger("creative_carousel")

MIN_CARDS = 3
MAX_CARDS = 6
DEFAULT_CARDS = 4
_GROUP_RE = re.compile(r"^[a-f0-9]{12}$")


# ---------------------------------------------------------------------------
# MATNLAR VA KARTALAR REJASI
# ---------------------------------------------------------------------------

def _layer_text(layers: list[dict], layer_id: str) -> str:
    for layer in layers or []:
        if layer.get("id") == layer_id and not layer.get("hidden"):
            text = (layer.get("text") or "").strip()
            return "" if "{{" in text else text
    return ""


def texts_for(ctx: dict, brief_answers: dict, source_asset=None) -> dict:
    """Kompaniya profili/brifidan zaxira qiymatlar; manba kreativ bo'lsa --
    uning (foydalanuvchi tahrirlagan) qatlam matnlari ustun."""
    values = cs.fallback_placeholder_values(ctx, brief_answers or {}, None)
    texts = {
        "headline": values.get("headline") or "",
        "subheadline": values.get("subheadline") or "",
        "cta_text": values.get("cta_text") or cs.FALLBACK_CTA,
        "price_text": values.get("price_text") or "",
        "offer_text": "" if values.get("offer_text") == "AKSIYA" else (values.get("offer_text") or ""),
        "phone_line": values.get("phone_line") or "",
        "features": [values.get(f"feature_{i}") for i in range(1, 5) if values.get(f"feature_{i}")],
    }
    if source_asset is not None:
        layers = cs.get_layers(source_asset)
        for key, layer_id in (("headline", "headline"), ("subheadline", "subheadline"), ("cta_text", "cta_badge"),
                              ("price_text", "price_badge"), ("phone_line", "phone")):
            text = _layer_text(layers, layer_id)
            if text:
                texts[key] = text
        offer = _layer_text(layers, "offer_badge")
        if offer and offer != "AKSIYA":
            texts["offer_text"] = offer
        feats = [t for t in (_layer_text(layers, f"feature_{i}") for i in range(1, 5)) if t]
        if feats:
            texts["features"] = feats
    return texts


def plan_cards(texts: dict, n_cards: int) -> list[dict]:
    """[{"kind": "hook"|"feature"|"cta", "text": ...}] -- afzallik yetmasa
    o'rtaga qo'shimcha matn/taklif/narx qo'yiladi; umuman bo'lmasa karta
    soni kamayadi (kamida 3)."""
    n = max(MIN_CARDS, min(MAX_CARDS, int(n_cards or DEFAULT_CARDS)))
    middle = [{"kind": "feature", "text": f} for f in texts.get("features") or []]
    for extra in (texts.get("subheadline"), texts.get("offer_text"), texts.get("price_text")):
        if extra and all(extra != m["text"] for m in middle):
            middle.append({"kind": "feature", "text": extra})
    if not middle:
        middle = [{"kind": "feature", "text": texts.get("headline") or ""}]
    return [{"kind": "hook"}] + middle[: n - 2] + [{"kind": "cta"}]


# ---------------------------------------------------------------------------
# 6 TA USLUB -- har biri (card, texts, i, n, accent, accent2) -> qatlamlar
# (1:1 koordinatalarda; 9:16 uchun `adapt_layers_for_aspect` siqadi).
# ---------------------------------------------------------------------------

def _counter(i, n, *, bg, color, x=0.80, y=0.055, w=0.14, h=0.06, opacity=0.92):
    return _v_badge("carousel_counter", x, y, w, h, f"{i}/{n}" + (" ›" if i < n else ""),
                    bg_color=bg, color=color, size_ratio=0.022, opacity=opacity)


def _style_gradient_bold(card, t, i, n, accent, accent2):
    if card["kind"] == "hook":
        layers = [
            _v_panel("tint", 0.0, 0.0, 1.0, 1.0, accent, opacity=0.35, gradient=True),
            _v_logo(0.06, 0.055, 0.22, 0.09, align="left"),
            _counter(i, n, bg="#FFFFFF", color="#111111"),
            _v_text("headline", 0.07, 0.40, 0.86, 0.30, t["headline"], size_ratio=0.098),
            _v_text("subheadline", 0.07, 0.72, 0.86, 0.10, t["subheadline"], font="regular", size_ratio=0.034, color="#F1F5F9"),
        ]
        if t["offer_text"]:
            layers.append(_v_badge("offer_badge", 0.07, 0.28, 0.40, 0.08, t["offer_text"], bg_color="#FACC15", color="#111111", size_ratio=0.032))
        return layers
    if card["kind"] == "cta":
        layers = [
            _v_panel("card", 0.08, 0.20, 0.84, 0.62, "#FFFFFF", opacity=0.96),
            _v_logo(0.30, 0.06, 0.40, 0.11, align="center"),
            _v_text("headline", 0.14, 0.25, 0.72, 0.16, t["headline"], align="center", size_ratio=0.058, color="#0F172A"),
        ]
        if t["price_text"]:
            layers.append(_v_badge("price_badge", 0.26, 0.43, 0.48, 0.10, t["price_text"], bg_color=accent2, size_ratio=0.040))
        layers += [
            _v_badge("cta_badge", 0.20, 0.58, 0.60, 0.09, t["cta_text"], bg_color=accent, size_ratio=0.034),
            _v_text("phone", 0.14, 0.70, 0.72, 0.06, t["phone_line"], align="center", size_ratio=0.030, color="#334155"),
        ]
        return layers
    return [
        _v_panel("tint", 0.0, 0.0, 1.0, 1.0, accent if i % 2 else accent2, opacity=0.80, gradient=True),
        _v_logo(0.06, 0.055, 0.20, 0.08, align="left"),
        _counter(i, n, bg="#FFFFFF", color="#111111"),
        _v_text("feature_number", 0.07, 0.22, 0.50, 0.20, f"{i - 1:02d}", size_ratio=0.16, color="#FFFFFF"),
        _v_panel("divider", 0.07, 0.45, 0.16, 0.01, "#FFFFFF", opacity=0.9),
        _v_text("feature_text", 0.07, 0.49, 0.86, 0.30, card["text"], size_ratio=0.070),
        _v_text("headline", 0.07, 0.86, 0.86, 0.06, t["headline"], font="regular", size_ratio=0.026, color="#E2E8F0"),
    ]


def _style_minimal_white(card, t, i, n, accent, accent2):
    if card["kind"] == "hook":
        layers = [
            _v_panel("card", 0.0, 0.54, 1.0, 0.46, "#FFFFFF", opacity=0.96),
            _v_logo(0.06, 0.055, 0.22, 0.09, align="left"),
            _counter(i, n, bg="#111111", color="#FFFFFF"),
            _v_panel("accent_bar", 0.07, 0.60, 0.12, 0.012, accent),
            _v_text("headline", 0.07, 0.64, 0.86, 0.17, t["headline"], size_ratio=0.072, color="#111111"),
            _v_text("subheadline", 0.07, 0.83, 0.86, 0.09, t["subheadline"], font="regular", size_ratio=0.030, color="#52525B"),
        ]
        if t["offer_text"]:
            layers.append(_v_badge("offer_badge", 0.07, 0.45, 0.36, 0.07, t["offer_text"], bg_color="#111111", size_ratio=0.028))
        return layers
    if card["kind"] == "cta":
        layers = [
            _v_panel("card", 0.0, 0.0, 1.0, 1.0, "#FFFFFF", opacity=0.94),
            _v_logo(0.06, 0.06, 0.24, 0.09, align="left"),
            _counter(i, n, bg="#111111", color="#FFFFFF"),
            _v_panel("accent_bar", 0.07, 0.30, 0.12, 0.012, accent),
            _v_text("headline", 0.07, 0.34, 0.86, 0.16, t["headline"], size_ratio=0.066, color="#111111"),
        ]
        if t["price_text"]:
            layers.append(_v_text("price_text", 0.07, 0.52, 0.86, 0.09, t["price_text"], size_ratio=0.052, color=accent))
        layers += [
            _v_badge("cta_badge", 0.07, 0.66, 0.52, 0.085, t["cta_text"], bg_color="#111111", size_ratio=0.032),
            _v_text("phone", 0.07, 0.79, 0.86, 0.06, t["phone_line"], font="regular", size_ratio=0.030, color="#3F3F46"),
        ]
        return layers
    return [
        _v_panel("card", 0.06, 0.06, 0.88, 0.88, "#FFFFFF", opacity=0.95),
        _counter(i, n, bg="#111111", color="#FFFFFF", x=0.74, y=0.10, w=0.14),
        _v_text("feature_number", 0.12, 0.14, 0.40, 0.10, f"{i - 1:02d} —", size_ratio=0.060, color=accent),
        _v_text("feature_text", 0.12, 0.34, 0.76, 0.36, card["text"], size_ratio=0.075, color="#111111"),
        _v_text("headline", 0.12, 0.80, 0.76, 0.06, t["headline"], font="regular", size_ratio=0.026, color="#71717A"),
    ]


_GOLD = "#C9A45C"


def _style_dark_luxury(card, t, i, n, accent, accent2):
    frame = [
        _v_panel("frame_top", 0.06, 0.04, 0.88, 0.004, _GOLD),
        _v_panel("frame_bottom", 0.06, 0.956, 0.88, 0.004, _GOLD),
    ]
    if card["kind"] == "hook":
        layers = frame + [
            _v_panel("shade", 0.0, 0.50, 1.0, 0.50, "#000000", opacity=0.85, gradient=True),
            _v_logo(0.35, 0.07, 0.30, 0.09, align="center"),
            _counter(i, n, bg="#000000", color=_GOLD, x=0.78, y=0.07),
            _v_text("headline", 0.08, 0.62, 0.84, 0.18, t["headline"], align="center", size_ratio=0.074, color="#F5F0E6"),
            _v_text("subheadline", 0.08, 0.82, 0.84, 0.08, t["subheadline"], align="center", font="regular", size_ratio=0.028, color=_GOLD),
        ]
        if t["offer_text"]:
            layers.append(_v_badge("offer_badge", 0.32, 0.52, 0.36, 0.07, t["offer_text"], bg_color=_GOLD, color="#111111", size_ratio=0.028))
        return layers
    if card["kind"] == "cta":
        layers = frame + [
            _v_panel("shade", 0.0, 0.0, 1.0, 1.0, "#0B0B0F", opacity=0.86),
            _v_logo(0.33, 0.10, 0.34, 0.11, align="center"),
            _v_text("headline", 0.10, 0.28, 0.80, 0.16, t["headline"], align="center", size_ratio=0.060, color="#F5F0E6"),
        ]
        if t["price_text"]:
            layers.append(_v_text("price_text", 0.10, 0.46, 0.80, 0.09, t["price_text"], align="center", size_ratio=0.050, color=_GOLD))
        layers += [
            _v_badge("cta_badge", 0.24, 0.60, 0.52, 0.085, t["cta_text"], bg_color=_GOLD, color="#111111", size_ratio=0.030),
            _v_text("phone", 0.10, 0.73, 0.80, 0.06, t["phone_line"], align="center", font="regular", size_ratio=0.028, color="#E7E0D2"),
        ]
        return layers
    return frame + [
        _v_panel("shade", 0.0, 0.0, 1.0, 1.0, "#0B0B0F", opacity=0.80),
        _counter(i, n, bg="#000000", color=_GOLD, x=0.78, y=0.07),
        _v_text("feature_number", 0.10, 0.20, 0.80, 0.14, f"{i - 1:02d}", align="center", size_ratio=0.11, color=_GOLD),
        _v_panel("divider", 0.44, 0.37, 0.12, 0.004, _GOLD),
        _v_text("feature_text", 0.10, 0.42, 0.80, 0.30, card["text"], align="center", size_ratio=0.064, color="#F5F0E6"),
        _v_text("headline", 0.10, 0.82, 0.80, 0.06, t["headline"], align="center", font="regular", size_ratio=0.024, color=_GOLD),
    ]


def _style_split_color(card, t, i, n, accent, accent2):
    left = i % 2 == 1  # kartalar navbatma-navbat chap/o'ng
    px = 0.0 if left else 0.5
    tx = px + 0.05
    color = accent if left else accent2
    if card["kind"] == "hook":
        layers = [
            _v_panel("half", px, 0.0, 0.5, 1.0, color, opacity=0.96),
            _v_logo(tx, 0.06, 0.24, 0.08, align="left"),
            _counter(i, n, bg="#FFFFFF", color="#111111", x=(0.80 if left else 0.06)),
            _v_text("headline", tx, 0.30, 0.40, 0.36, t["headline"], size_ratio=0.072),
            _v_text("subheadline", tx, 0.68, 0.40, 0.16, t["subheadline"], font="regular", size_ratio=0.028, color="#F8FAFC"),
        ]
        if t["offer_text"]:
            layers.append(_v_badge("offer_badge", tx, 0.86, 0.32, 0.07, t["offer_text"], bg_color="#FFFFFF", color="#111111", size_ratio=0.026))
        return layers
    if card["kind"] == "cta":
        layers = [
            _v_panel("half", 0.0, 0.0, 1.0, 1.0, color, opacity=0.93),
            _v_logo(0.06, 0.06, 0.24, 0.09, align="left"),
            _v_text("headline", 0.06, 0.26, 0.88, 0.18, t["headline"], size_ratio=0.066),
        ]
        if t["price_text"]:
            layers.append(_v_badge("price_badge", 0.06, 0.47, 0.46, 0.10, t["price_text"], bg_color="#FFFFFF", color="#111111", size_ratio=0.040))
        layers += [
            _v_badge("cta_badge", 0.06, 0.62, 0.56, 0.09, t["cta_text"], bg_color="#111111", size_ratio=0.032),
            _v_text("phone", 0.06, 0.75, 0.88, 0.06, t["phone_line"], size_ratio=0.032),
        ]
        return layers
    return [
        _v_panel("half", px, 0.0, 0.5, 1.0, color, opacity=0.96),
        _counter(i, n, bg="#FFFFFF", color="#111111", x=(0.80 if left else 0.06)),
        _v_text("feature_number", tx, 0.10, 0.40, 0.16, f"{i - 1:02d}", size_ratio=0.12),
        _v_text("feature_text", tx, 0.34, 0.40, 0.44, card["text"], size_ratio=0.058),
        _v_text("headline", tx, 0.86, 0.40, 0.08, t["headline"], font="regular", size_ratio=0.022, color="#F1F5F9"),
    ]


def _style_numbered_steps(card, t, i, n, accent, accent2):
    band_text = t["subheadline"] if card["kind"] == "cta" else t["headline"]  # CTA'da sarlavha pastda bor
    band = [
        _v_panel("band", 0.0, 0.0, 1.0, 0.15, accent, opacity=0.97),
        _v_text("band_text", 0.06, 0.035, 0.60, 0.08, band_text, size_ratio=0.034),
        _counter(i, n, bg="#FFFFFF", color="#111111", y=0.045),
    ]
    if card["kind"] == "hook":
        layers = [
            _v_panel("bottom", 0.0, 0.58, 1.0, 0.42, "#FFFFFF", opacity=0.96),
            _v_panel("band", 0.0, 0.0, 1.0, 0.15, accent, opacity=0.97),
            _v_logo(0.06, 0.03, 0.24, 0.09, align="left"),
            _counter(i, n, bg="#FFFFFF", color="#111111", y=0.045),
            _v_text("headline", 0.06, 0.63, 0.88, 0.18, t["headline"], size_ratio=0.074, color="#0F172A"),
            _v_text("subheadline", 0.06, 0.83, 0.88, 0.09, t["subheadline"], font="regular", size_ratio=0.030, color="#475569"),
        ]
        if t["offer_text"]:
            layers.append(_v_badge("offer_badge", 0.06, 0.48, 0.38, 0.075, t["offer_text"], bg_color=accent2, size_ratio=0.030))
        return layers
    if card["kind"] == "cta":
        layers = band + [
            _v_panel("bottom", 0.0, 0.15, 1.0, 0.85, "#FFFFFF", opacity=0.95),
            _v_logo(0.06, 0.22, 0.26, 0.10, align="left"),
            _v_text("headline", 0.06, 0.36, 0.88, 0.14, t["headline"], size_ratio=0.058, color="#0F172A"),
        ]
        if t["price_text"]:
            layers.append(_v_badge("price_badge", 0.06, 0.52, 0.46, 0.10, t["price_text"], bg_color=accent2, size_ratio=0.040))
        layers += [
            _v_badge("cta_badge", 0.06, 0.67, 0.58, 0.09, t["cta_text"], bg_color=accent, size_ratio=0.032),
            _v_text("phone", 0.06, 0.80, 0.88, 0.06, t["phone_line"], size_ratio=0.032, color="#0F172A"),
        ]
        return layers
    return band + [
        _v_panel("bottom", 0.0, 0.52, 1.0, 0.48, "#FFFFFF", opacity=0.96),
        _v_badge("feature_number", 0.06, 0.40, 0.22, 0.22, f"{i - 1}", bg_color=accent2, size_ratio=0.12),
        _v_text("feature_text", 0.06, 0.66, 0.88, 0.26, card["text"], size_ratio=0.062, color="#0F172A"),
    ]


_POP_YELLOW = "#FFE600"
_POP_PINK = "#FF3D7F"


def _style_sticker_pop(card, t, i, n, accent, accent2):
    if card["kind"] == "hook":
        layers = [
            _v_logo(0.06, 0.055, 0.22, 0.09, align="left"),
            _counter(i, n, bg="#111111", color=_POP_YELLOW),
            _v_badge("headline", 0.06, 0.52, 0.88, 0.22, t["headline"], bg_color=_POP_YELLOW, color="#111111", size_ratio=0.070),
            _v_badge("subheadline", 0.06, 0.77, 0.70, 0.09, t["subheadline"], bg_color="#111111", color="#FFFFFF", font="regular", size_ratio=0.026),
        ]
        if t["offer_text"]:
            layers.append(_v_badge("offer_badge", 0.60, 0.36, 0.34, 0.12, t["offer_text"], bg_color=_POP_PINK, size_ratio=0.046))
        return layers
    if card["kind"] == "cta":
        layers = [
            _v_panel("tint", 0.0, 0.0, 1.0, 1.0, "#111111", opacity=0.55),
            _v_logo(0.06, 0.06, 0.24, 0.09, align="left"),
            _v_badge("headline", 0.06, 0.24, 0.88, 0.16, t["headline"], bg_color="#FFFFFF", color="#111111", size_ratio=0.052),
        ]
        if t["price_text"]:
            layers.append(_v_badge("price_badge", 0.40, 0.44, 0.54, 0.13, t["price_text"], bg_color=_POP_YELLOW, color="#111111", size_ratio=0.048))
        layers += [
            _v_badge("cta_badge", 0.06, 0.62, 0.64, 0.10, t["cta_text"], bg_color=_POP_PINK, size_ratio=0.036),
            _v_badge("phone", 0.06, 0.76, 0.70, 0.07, t["phone_line"], bg_color="#111111", color=_POP_YELLOW, size_ratio=0.028),
        ]
        return layers
    sticker = _POP_YELLOW if i % 2 == 0 else _POP_PINK
    ink = "#111111" if sticker == _POP_YELLOW else "#FFFFFF"
    return [
        _v_logo(0.06, 0.055, 0.20, 0.08, align="left"),
        _counter(i, n, bg="#111111", color=_POP_YELLOW),
        _v_badge("feature_number", 0.06, 0.26, 0.24, 0.16, f"#{i - 1}", bg_color="#111111", color=sticker, size_ratio=0.070),
        _v_badge("feature_text", 0.06, 0.46, 0.88, 0.30, card["text"], bg_color=sticker, color=ink, size_ratio=0.060),
        _v_badge("headline", 0.06, 0.80, 0.70, 0.07, t["headline"], bg_color="#FFFFFF", color="#111111", font="regular", size_ratio=0.024),
    ]


# Har bir uslub: preview foni (manbasiz yaratishda ham ishlatiladi; brend
# ranglari bo'lsa, ular bilan almashtiriladi -- `accent_bg`).
CAROUSEL_STYLES = [
    {"key": "gradient_bold", "name": "Yorqin gradient", "build": _style_gradient_bold,
     "description": "Katta raqamlar, brend ranglaridagi to'liq fon -- e'tiborni darhol tortadi.",
     "accents": ("#2563EB", "#7C3AED"), "background": {"type": "gradient", "colors": ["#1E3A8A", "#7C3AED"], "direction": "diagonal"}, "accent_bg": True},
    {"key": "minimal_white", "name": "Minimal oq", "build": _style_minimal_white,
     "description": "Oq kartalar, toza tipografiya -- mebel, ko'chmas mulk, xizmatlar uchun.",
     "accents": ("#E11D48", "#111111"), "background": {"type": "gradient", "colors": ["#E7E5E4", "#A8A29E"], "direction": "vertical"}},
    {"key": "dark_luxury", "name": "Qora hashamat", "build": _style_dark_luxury,
     "description": "Qora fon, oltin chiziqlar -- premium mahsulotlar, go'zallik, zargarlik.",
     "accents": (_GOLD, "#8C6D3B"), "background": {"type": "gradient", "colors": ["#0B0B0F", "#2A2316"], "direction": "vertical"}},
    {"key": "split_color", "name": "Ikki rangli", "build": _style_split_color,
     "description": "Yarmi rang, yarmi rasm -- kartalar navbatma-navbat almashadi, surishga undaydi.",
     "accents": ("#0EA5E9", "#F97316"), "background": {"type": "gradient", "colors": ["#334155", "#64748B"], "direction": "horizontal"}, "accent_bg": True},
    {"key": "numbered_steps", "name": "Qadamlar", "build": _style_numbered_steps,
     "description": "Yuqori lenta va katta raqamlar -- \"3 qadamda\", ta'lim, yo'riqnomalar uchun.",
     "accents": ("#16A34A", "#0F766E"), "background": {"type": "gradient", "colors": ["#14532D", "#65A30D"], "direction": "diagonal"}, "accent_bg": True},
    {"key": "sticker_pop", "name": "Stiker pop", "build": _style_sticker_pop,
     "description": "Sariq va pushti stikerlar -- yoshlar, kiyim, fast-food, aksiyalar.",
     "accents": (_POP_PINK, _POP_YELLOW), "background": {"type": "gradient", "colors": ["#7C3AED", "#EC4899"], "direction": "diagonal"}},
]
STYLES_BY_KEY = {s["key"]: s for s in CAROUSEL_STYLES}


def get_style(key: "str | None") -> dict:
    return STYLES_BY_KEY.get(key or "") or CAROUSEL_STYLES[0]


def card_layers(style: dict, card: dict, texts: dict, index: int, total: int, accents: "tuple[str, str]", aspect: str) -> list[dict]:
    """Bitta karta qatlamlari: uslub quruvchisi -> bo'sh matnli qatlamlar
    yashiriladi -> tekshiruv (`sanitize_layers`) -> format moslashuvi."""
    keys = ("headline", "subheadline", "cta_text", "price_text", "offer_text", "phone_line")
    t = {k: (texts.get(k) or "") for k in keys}
    t["features"] = list(texts.get("features") or [])
    card = {"kind": card["kind"], "text": card.get("text") or ""}
    layers = style["build"](card, t, index, total, accents[0], accents[1])
    for layer in layers:
        if layer.get("type") in ("text", "badge") and not (layer.get("text") or "").strip():
            layer["hidden"] = True
    return cs.adapt_layers_for_aspect(cs.sanitize_layers(layers), aspect)


def _brand_accents(style: dict, brand_kit) -> "tuple[str, str]":
    primary = getattr(brand_kit, "primary_color", None)
    secondary = getattr(brand_kit, "secondary_color", None)
    if primary:
        return primary, secondary or style["accents"][1]
    return style["accents"]


def _style_background(style: dict, accents: "tuple[str, str]", size):
    bg = dict(style["background"])
    if style.get("accent_bg"):
        bg["colors"] = [accents[0], accents[1]]
    return cs.background_image(bg, size)


# ---------------------------------------------------------------------------
# YARATISH
# ---------------------------------------------------------------------------

def create_carousel(session, company, manager_id, *, style_key: str, n_cards: int = DEFAULT_CARDS,
                    source_asset=None, aspect: str = "1:1") -> list:
    """Karusel kartalarini yaratadi (OpenAI'siz). Qaytaradi: tartiblangan
    `CreativeAsset` ro'yxati."""
    style = get_style(style_key)
    brand_kit = cs.get_brand_kit(session, company.id)
    accents = _brand_accents(style, brand_kit)
    ctx = company_context_module.build_company_context(company, session)
    if source_asset is not None:
        if source_asset.company_id != company.id:
            raise CreativeError("Kreativ topilmadi.")
        if source_asset.status != "ready" or not source_asset.base_image_storage_path:
            raise CreativeError("Karusel uchun avval kreativni tayyorlang (fon rasmi bo'lishi kerak).")
        if source_asset.get_brief_answers().get("carousel_group"):
            raise CreativeError("Bu rasm allaqachon karusel kartasi -- asl kreativdan karusel yarating.")
        base_src = storage_backend.ensure_local(cs.CREATIVE_ROOT, source_asset.base_image_storage_path, key_prefix="creative_studio")
        if not base_src.exists():
            raise CreativeError("Fon rasmi diskda topilmadi -- kreativni qayta generatsiya qiling.")
        base_bytes = base_src.read_bytes()
        aspect = source_asset.aspect
        brief = source_asset.get_brief_answers()
        title_base = source_asset.title or "Kreativ"
    else:
        if aspect not in cs.ASPECTS:
            raise CreativeError("Rasm nisbati faqat 1:1, 4:5 yoki 9:16 bo'lishi mumkin.")
        buf = io.BytesIO()
        _style_background(style, accents, cs._target_pixels_for_aspect(aspect)).save(buf, format="PNG")
        base_bytes = buf.getvalue()
        brief = {}
        title_base = style["name"]
    texts = texts_for(ctx, brief, source_asset)
    if not texts.get("features"):
        # Afzalliklar yo'q -- arzon MATN AI'si (gpt-4o-mini, rasm kvotasi
        # SARFLANMAYDI) qisqa afzalliklar yozib beradi. Ishlamasa -- jim o'tadi.
        copy_ = cs.generate_ad_copy(ctx, brief, None) or {}
        feats = [f for f in (copy_.get("features") or []) if isinstance(f, str) and f.strip()]
        if feats:
            texts["features"] = feats
        if not texts.get("subheadline") and copy_.get("subheadline"):
            texts["subheadline"] = copy_["subheadline"]
    if not texts.get("headline"):
        raise CreativeError("Karusel uchun sarlavha topilmadi -- avval Biznes profilini to'ldiring.")
    if not (texts.get("features") or texts.get("subheadline") or texts.get("offer_text") or texts.get("price_text")):
        raise CreativeError(
            "Karusel kartalari uchun ma'lumot yetarli emas. Biznes profilida \"Qo'shimcha ma'lumot\" "
            "maydoniga vergul bilan 2-3 ta afzallik yozing (masalan: \"Bepul yetkazish, 1 yil kafolat\") "
            "yoki tayyor kreativ muharriridan karusel yarating."
        )
    cards = plan_cards(texts, n_cards)
    group = secrets.token_hex(6)
    total = len(cards)
    created = []
    for i, card in enumerate(cards, start=1):
        asset = db.CreativeAsset(
            company_id=company.id, created_by_manager_id=manager_id, kind="template", status="generating",
            template_key=getattr(source_asset, "template_key", None), aspect=aspect,
            title=f"{title_base[:180]} — karusel {i}/{total}",
        )
        asset.set_brief_answers({**brief, "carousel_group": group, "carousel_index": i, "carousel_total": total,
                                 "carousel_style": style["key"], "carousel_source_id": getattr(source_asset, "id", None)})
        session.add(asset)
        session.commit()  # id kerak (papka nomi uchun)
        try:
            d = cs._asset_dir(asset)
            d.mkdir(parents=True, exist_ok=True)
            (d / "base.png").write_bytes(base_bytes)
            asset.base_image_storage_path = cs._asset_rel(asset, "base.png")
            storage_backend.upload_file(d / "base.png", f"creative_studio/{asset.base_image_storage_path}")
            asset.set_layers(card_layers(style, card, texts, i, total, accents, aspect))
            cs._render_asset(session, asset)
            asset.status = "ready"
            asset.missing_fields_json = "[]"
            session.commit()
        except Exception as e:  # noqa: BLE001
            logger.exception("creative_carousel: karta yaratishda xato (asset=%s)", asset.id)
            asset.status = "failed"
            asset.error_message = "Karusel kartasini yaratib bo'lmadi. Qayta urinib ko'ring."
            session.commit()
            raise CreativeError(asset.error_message) from e
        created.append(asset)
    return created


def info(asset) -> "dict | None":
    answers = asset.get_brief_answers() if asset is not None else {}
    group = answers.get("carousel_group")
    if not group:
        return None
    return {"group": group, "index": answers.get("carousel_index"), "total": answers.get("carousel_total"),
            "style": answers.get("carousel_style")}


def group_assets(session, company_id: int, group: str) -> list:
    """Guruhdagi kartalar (tartib bo'yicha), faqat shu kompaniyaniki."""
    if not _GROUP_RE.match(group or ""):
        return []
    rows = (
        session.query(db.CreativeAsset)
        .filter(db.CreativeAsset.company_id == company_id,
                db.CreativeAsset.brief_answers_json.like(f'%"carousel_group": "{group}"%'))
        .all()
    )
    rows = [r for r in rows if (r.get_brief_answers() or {}).get("carousel_group") == group]
    return sorted(rows, key=lambda r: (r.get_brief_answers() or {}).get("carousel_index") or 0)


def zip_bytes(assets: list) -> bytes:
    """Kartalarning yakuniy PNG'lari bitta ZIP'da (karusel_01.png, ...)."""
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for asset in assets:
            meta = info(asset) or {}
            zf.write(cs.export_png_path(asset), arcname=f"karusel_{int(meta.get('index') or asset.id):02d}.png")
    return buf.getvalue()
