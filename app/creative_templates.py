"""creative_templates.py — Kreativ studiya: 20 ta TAYYOR SHABLON ta'rifi
(2026-09, foydalanuvchi so'rovi: "20 ta tayyor shablon bo'lsin, har biri
original dizayn uslubi/kompozitsiyasi, foydalanuvchi shablonni tanlab,
o'zining mahsuloti/matnini shu shablon uslubiga moslab qo'yishi mumkin").

Bu STATIK Python ma'lumot (DB jadvali EMAS -- xuddi `campaign_draft.
OBJECTIVE_META` kabi konstantalar moduli). Barcha dizaynlar ORIGINAL --
hech qanday tashqi/mualliflik huquqi bilan himoyalangan shablon
nusxalanmagan, faqat umumiy kompozitsiya prinsiplari (rang palitrasi,
matn joylashuvi) ishlatilgan.

SHABLON SXEMASI (web-agent Pillow-render VA brauzer canvas-muharrirda
AYNAN shu koordinatalarni ishlatadi):

  {
    "key": "minimal_clean",            # unique, snake_case
    "name": "Minimalist toza",         # o'zbekcha ko'rinadigan nom
    "category": "universal",           # filtrlash yorlig'i
    "description": "...",              # 1 gap, o'zbekcha
    "style_prompt": "...",             # OpenAI FON-rasm promptiga qo'shiladigan INGLIZCHA uslub
    "background": {"type": "solid"|"gradient", "colors": [...], "direction": "vertical"|"horizontal"|"diagonal"},
    "aspect_default": "1:1" | "4:5" | "9:16",
    "default_cta": "Batafsil",         # brifda CTA berilmasa shu shablonga mos standart
    "layers": [ ... ]                  # BOSHLANG'ICH qatlamlar (muharrirda o'zgartiriladi)
  }

QATLAM (layer) SXEMASI -- `x/y/w/h` canvasning 0..1 NISBIY koordinatalari
(chap-yuqori burchakdan), istalgan piksel o'lchamiga ko'paytirib olinadi:

  - type="text":  {"id","type","x","y","w","h","align":"left|center|right",
                   "font":"bold|regular","size_ratio":<balandlikka nisbatan>,
                   "color":"#RRGGBB","text":"..."} -- avto word-wrap, `w`ga
                   sig'masa shrift avtomatik kichrayadi. Matn bo'sh bo'lsa
                   qatlam chizilmaydi.
  - type="badge": text kabi + "bg_color" (yumaloq burchakli to'rtburchak
                   ustida markazlangan matn), ixtiyoriy "opacity" (0..1).
                   Matn bo'sh bo'lsa -- faqat rangli to'rtburchak (panel).
  - type="panel": {"id","type","x","y","w","h","bg_color","opacity",
                   "gradient": bool} -- dekorativ to'rtburchak; gradient=true
                   bo'lsa yuqorida shaffof, pastda bg_color (Story overlay).
  - type="logo":  {"id","type","x","y","w","h","align"} -- kompaniya
                   logotipi shu to'rtburchakka "contain" rejimida; logotip
                   bo'lmasa shunchaki o'tkazib yuboriladi.

PLACEHOLDER'LAR (`text` ichida, `creative_studio.resolve_layers()`
haqiqiy matn bilan almashtiradi): {{headline}}, {{subheadline}},
{{cta_text}}, {{offer_text}}, {{price_text}}, {{brand_name}},
{{quote_text}}, {{feature_1}}..{{feature_4}}. Almashtirilmagan (qiymati
bo'sh) placeholder'li matn qatlami chizilmaydi.
"""


def _text(id_, x, y, w, h, text, *, align="left", font="bold", size_ratio=0.06, color="#111111"):
    return {"id": id_, "type": "text", "x": x, "y": y, "w": w, "h": h, "align": align,
            "font": font, "size_ratio": size_ratio, "color": color, "text": text}


def _badge(id_, x, y, w, h, text, *, bg_color="#111111", color="#FFFFFF", size_ratio=0.028, font="bold", align="center", opacity=1.0):
    return {"id": id_, "type": "badge", "x": x, "y": y, "w": w, "h": h, "align": align,
            "font": font, "size_ratio": size_ratio, "color": color, "bg_color": bg_color,
            "opacity": opacity, "text": text}


def _panel(id_, x, y, w, h, bg_color, *, opacity=1.0, gradient=False):
    return {"id": id_, "type": "panel", "x": x, "y": y, "w": w, "h": h,
            "bg_color": bg_color, "opacity": opacity, "gradient": gradient}


def _logo(x=0.80, y=0.06, w=0.14, h=0.09, align="right"):
    return {"id": "logo", "type": "logo", "x": x, "y": y, "w": w, "h": h, "align": align}


_NO_TEXT = "no text, no words, no letters, no typography, no watermark, no logo"

CREATIVE_TEMPLATES = [
    {
        "key": "minimal_clean",
        "name": "Minimalist toza",
        "category": "universal",
        "description": "Oq-och kulrang fon, ko'p bo'sh joy, mahsulot markazda -- har qanday biznes uchun.",
        "style_prompt": f"clean minimalist studio background, soft white and light grey tones, soft diffused lighting, plenty of negative space at the bottom for overlay text, {_NO_TEXT}",
        "background": {"type": "solid", "colors": ["#F4F4F2"]},
        "aspect_default": "1:1",
        "default_cta": "Batafsil",
        "layers": [
            _badge("cta_badge", 0.08, 0.06, 0.30, 0.07, "{{cta_text}}", bg_color="#111111", color="#FFFFFF"),
            _logo(0.80, 0.06, 0.14, 0.09),
            _text("headline", 0.08, 0.72, 0.84, 0.12, "{{headline}}", size_ratio=0.062, color="#111111"),
            _text("subheadline", 0.08, 0.85, 0.84, 0.08, "{{subheadline}}", font="regular", size_ratio=0.032, color="#444444"),
        ],
    },
    {
        "key": "bold_sale",
        "name": "Katta chegirma",
        "category": "sale",
        "description": "Yorqin qizil-sariq, katta aksiya belgisi -- chegirma va aksiyalar uchun.",
        "style_prompt": f"vibrant energetic background in bright red and warm yellow tones, dynamic light rays, bold commercial sale mood, clean empty area at the bottom for overlay text, {_NO_TEXT}",
        "background": {"type": "gradient", "colors": ["#E63946", "#F4A261"], "direction": "diagonal"},
        "aspect_default": "1:1",
        "default_cta": "Hoziroq xarid qiling",
        "layers": [
            _badge("offer_badge", 0.06, 0.06, 0.46, 0.16, "{{offer_text}}", bg_color="#FFD60A", color="#1D1D1D", size_ratio=0.055),
            _logo(0.80, 0.06, 0.14, 0.09),
            _text("headline", 0.06, 0.66, 0.88, 0.14, "{{headline}}", size_ratio=0.075, color="#FFFFFF"),
            _text("subheadline", 0.06, 0.80, 0.88, 0.07, "{{subheadline}}", font="regular", size_ratio=0.032, color="#FFF3E0"),
            _badge("cta_badge", 0.06, 0.88, 0.44, 0.075, "{{cta_text}}", bg_color="#1D1D1D", color="#FFFFFF"),
        ],
    },
    {
        "key": "luxury_dark",
        "name": "Premium qorong'i",
        "category": "luxury",
        "description": "Qora fon, oltin aksent, nafis markazlashgan matn -- premium mahsulotlar uchun.",
        "style_prompt": f"luxurious dark background, deep black with subtle gold light reflections, elegant premium product photography mood, dramatic rim lighting, empty dark space at the bottom for overlay text, {_NO_TEXT}",
        "background": {"type": "gradient", "colors": ["#0B0B0F", "#1F1A12"], "direction": "vertical"},
        "aspect_default": "1:1",
        "default_cta": "Kolleksiyani ko'ring",
        "layers": [
            _logo(0.40, 0.05, 0.20, 0.10, align="center"),
            _text("headline", 0.10, 0.68, 0.80, 0.12, "{{headline}}", align="center", size_ratio=0.058, color="#C9A227"),
            _text("subheadline", 0.15, 0.80, 0.70, 0.07, "{{subheadline}}", align="center", font="regular", size_ratio=0.028, color="#D9D2B6"),
            _badge("cta_badge", 0.30, 0.89, 0.40, 0.07, "{{cta_text}}", bg_color="#C9A227", color="#0B0B0F", size_ratio=0.026),
        ],
    },
    {
        "key": "tech_gradient",
        "name": "Texnologik gradient",
        "category": "tech",
        "description": "Ko'k-binafsha gradient, zamonaviy va raqamli uslub -- elektronika, IT, xizmatlar.",
        "style_prompt": f"modern technology background, deep blue to violet gradient, subtle glowing geometric shapes and light particles, futuristic clean feel, empty area at the bottom left for overlay text, {_NO_TEXT}",
        "background": {"type": "gradient", "colors": ["#1E3A8A", "#7C3AED"], "direction": "diagonal"},
        "aspect_default": "1:1",
        "default_cta": "Batafsil",
        "layers": [
            _badge("cta_badge", 0.06, 0.06, 0.28, 0.07, "{{cta_text}}", bg_color="#22D3EE", color="#0B1020"),
            _logo(0.80, 0.06, 0.14, 0.09),
            _text("headline", 0.06, 0.70, 0.88, 0.12, "{{headline}}", size_ratio=0.064, color="#FFFFFF"),
            _text("subheadline", 0.06, 0.83, 0.88, 0.08, "{{subheadline}}", font="regular", size_ratio=0.03, color="#DDD6FE"),
        ],
    },
    {
        "key": "warm_food",
        "name": "Issiq taom",
        "category": "food",
        "description": "To'q sariq-jigarrang iliq tonlar -- restoran, kafe, oziq-ovqat uchun.",
        "style_prompt": f"warm appetizing food photography background, rustic wooden table, warm orange and brown tones, soft natural window light, shallow depth of field, clear space at the bottom for overlay text, {_NO_TEXT}",
        "background": {"type": "gradient", "colors": ["#7A2E0E", "#E76F51"], "direction": "vertical"},
        "aspect_default": "1:1",
        "default_cta": "Buyurtma bering",
        "layers": [
            _badge("offer_badge", 0.06, 0.06, 0.36, 0.08, "{{offer_text}}", bg_color="#F4A261", color="#3D1A08", size_ratio=0.028),
            _text("headline", 0.06, 0.68, 0.88, 0.12, "{{headline}}", size_ratio=0.066, color="#FFF3E0"),
            _text("subheadline", 0.06, 0.81, 0.62, 0.08, "{{subheadline}}", font="regular", size_ratio=0.03, color="#FFE8D6"),
            _badge("cta_badge", 0.06, 0.90, 0.40, 0.065, "{{cta_text}}", bg_color="#FFF3E0", color="#7A2E0E", size_ratio=0.025),
            _logo(0.78, 0.86, 0.16, 0.10),
        ],
    },
    {
        "key": "real_estate_clean",
        "name": "Ko'chmas mulk",
        "category": "real_estate",
        "description": "Oq-kulrang professional uslub, katta rasm zonasi, pastda ma'lumot paneli.",
        "style_prompt": f"bright modern architecture background, clean apartment interior or building exterior, white and light grey tones, natural daylight, professional real estate photography, lower part kept plain for overlay text, {_NO_TEXT}",
        "background": {"type": "gradient", "colors": ["#E2E8F0", "#F8FAFC"], "direction": "vertical"},
        "aspect_default": "4:5",
        "default_cta": "Ko'rishga yoziling",
        "layers": [
            _panel("info_panel", 0.0, 0.70, 1.0, 0.30, "#FFFFFF", opacity=0.94),
            _logo(0.80, 0.05, 0.15, 0.08),
            _badge("price_badge", 0.06, 0.62, 0.36, 0.065, "{{price_text}}", bg_color="#0EA5E9", color="#FFFFFF", size_ratio=0.024),
            _text("headline", 0.06, 0.73, 0.88, 0.10, "{{headline}}", size_ratio=0.05, color="#0F172A"),
            _text("subheadline", 0.06, 0.83, 0.88, 0.07, "{{subheadline}}", font="regular", size_ratio=0.026, color="#475569"),
            _badge("cta_badge", 0.06, 0.91, 0.38, 0.055, "{{cta_text}}", bg_color="#0F172A", color="#FFFFFF", size_ratio=0.022),
        ],
    },
    {
        "key": "fashion_editorial",
        "name": "Moda jurnali",
        "category": "fashion",
        "description": "Vertikal katta rasm, minimal matn, bej fon -- kiyim-kechak va aksessuarlar.",
        "style_prompt": f"editorial fashion photography background, neutral beige and cream studio backdrop, soft directional light, elegant minimal composition with the subject slightly off-center, clean space at the bottom left for overlay text, {_NO_TEXT}",
        "background": {"type": "solid", "colors": ["#EFE9E1"]},
        "aspect_default": "4:5",
        "default_cta": "Yangi kolleksiya",
        "layers": [
            _text("headline", 0.06, 0.80, 0.70, 0.08, "{{headline}}", size_ratio=0.042, color="#1C1917"),
            _text("subheadline", 0.06, 0.88, 0.70, 0.05, "{{subheadline}}", font="regular", size_ratio=0.022, color="#57534E"),
            _text("cta_text", 0.06, 0.93, 0.50, 0.04, "{{cta_text}}", font="regular", size_ratio=0.02, color="#1C1917"),
            _logo(0.78, 0.86, 0.16, 0.09),
        ],
    },
    {
        "key": "before_after_split",
        "name": "Avval / Keyin",
        "category": "testimonial",
        "description": "Ikkiga bo'lingan (chap/o'ng) kompozitsiya, AVVAL/KEYIN belgilari -- natija ko'rsatish uchun.",
        "style_prompt": f"split composition background divided vertically into two halves, left half muted and dull grey tones, right half bright and vivid, symmetric layout, plain area at the bottom for overlay text, {_NO_TEXT}",
        "background": {"type": "gradient", "colors": ["#4B5563", "#F3F4F6"], "direction": "horizontal"},
        "aspect_default": "1:1",
        "default_cta": "Natijani ko'ring",
        "layers": [
            _panel("divider", 0.495, 0.0, 0.01, 0.78, "#FFFFFF", opacity=0.9),
            _badge("before_badge", 0.06, 0.06, 0.24, 0.065, "AVVAL", bg_color="#111827", color="#FFFFFF", size_ratio=0.024),
            _badge("after_badge", 0.70, 0.06, 0.24, 0.065, "KEYIN", bg_color="#16A34A", color="#FFFFFF", size_ratio=0.024),
            _logo(0.40, 0.04, 0.20, 0.08, align="center"),
            _panel("bottom_panel", 0.0, 0.78, 1.0, 0.22, "#111827", opacity=0.92),
            _text("headline", 0.06, 0.81, 0.62, 0.09, "{{headline}}", size_ratio=0.046, color="#FFFFFF"),
            _text("subheadline", 0.06, 0.90, 0.62, 0.06, "{{subheadline}}", font="regular", size_ratio=0.024, color="#D1D5DB"),
            _badge("cta_badge", 0.70, 0.84, 0.26, 0.065, "{{cta_text}}", bg_color="#16A34A", color="#FFFFFF", size_ratio=0.022),
        ],
    },
    {
        "key": "countdown_urgency",
        "name": "Shoshiling!",
        "category": "urgency",
        "description": "Qizil-qora, 'vaqt tugayapti' uslubi -- muddatli aksiyalar uchun.",
        "style_prompt": f"dramatic dark red and black background, urgent high-contrast mood, spotlight beam on the center, subtle motion blur streaks, clean dark area at the bottom for overlay text, {_NO_TEXT}",
        "background": {"type": "gradient", "colors": ["#B91C1C", "#111111"], "direction": "vertical"},
        "aspect_default": "1:1",
        "default_cta": "Ulgurib qoling",
        "layers": [
            _badge("urgency_badge", 0.06, 0.06, 0.44, 0.075, "OXIRGI KUNLAR", bg_color="#FEF08A", color="#7F1D1D", size_ratio=0.028),
            _logo(0.80, 0.06, 0.14, 0.09),
            _badge("offer_badge", 0.06, 0.52, 0.50, 0.13, "{{offer_text}}", bg_color="#FFFFFF", color="#B91C1C", size_ratio=0.05),
            _text("headline", 0.06, 0.68, 0.88, 0.12, "{{headline}}", size_ratio=0.064, color="#FFFFFF"),
            _text("subheadline", 0.06, 0.81, 0.88, 0.06, "{{subheadline}}", font="regular", size_ratio=0.028, color="#FECACA"),
            _badge("cta_badge", 0.06, 0.89, 0.42, 0.07, "{{cta_text}}", bg_color="#FEF08A", color="#111111", size_ratio=0.026),
        ],
    },
    {
        "key": "testimonial_quote",
        "name": "Mijoz fikri",
        "category": "testimonial",
        "description": "Och fon, katta qo'shtirnoq bezagi, mijoz sharhi matni -- ishonch uyg'otish uchun.",
        "style_prompt": f"soft warm cream background with gentle paper texture, calm and trustworthy mood, subtle light vignette, very simple and airy, large empty center area for overlay text, {_NO_TEXT}",
        "background": {"type": "solid", "colors": ["#FDF6EC"]},
        "aspect_default": "1:1",
        "default_cta": "Siz ham sinab ko'ring",
        "layers": [
            _text("quote_mark", 0.06, 0.04, 0.30, 0.30, "“", size_ratio=0.34, color="#E8C9A0"),
            _logo(0.80, 0.06, 0.14, 0.09),
            _text("quote_text", 0.10, 0.34, 0.80, 0.30, "{{quote_text}}", font="regular", size_ratio=0.04, color="#3F2A14", align="center"),
            _text("headline", 0.10, 0.68, 0.80, 0.08, "{{headline}}", align="center", size_ratio=0.036, color="#8B5E34"),
            _text("subheadline", 0.10, 0.76, 0.80, 0.06, "{{subheadline}}", align="center", font="regular", size_ratio=0.024, color="#7C6650"),
            _badge("cta_badge", 0.30, 0.87, 0.40, 0.07, "{{cta_text}}", bg_color="#8B5E34", color="#FFFFFF", size_ratio=0.025),
        ],
    },
    {
        "key": "feature_grid",
        "name": "Xususiyatlar 2x2",
        "category": "features",
        "description": "Yuqorida sarlavha, pastda 4 ta xususiyat bloki (2x2) -- afzalliklarni sanab o'tish uchun.",
        "style_prompt": f"fresh mint green and white background, clean flat product presentation, soft even lighting, subject in the upper center, lower half kept plain for overlay blocks, {_NO_TEXT}",
        "background": {"type": "gradient", "colors": ["#D1FAE5", "#ECFDF5"], "direction": "vertical"},
        "aspect_default": "1:1",
        "default_cta": "Batafsil",
        "layers": [
            _text("headline", 0.06, 0.06, 0.70, 0.10, "{{headline}}", size_ratio=0.05, color="#064E3B"),
            _logo(0.80, 0.06, 0.14, 0.09),
            _badge("feature_1", 0.06, 0.56, 0.42, 0.14, "{{feature_1}}", bg_color="#FFFFFF", color="#064E3B", size_ratio=0.026, opacity=0.95),
            _badge("feature_2", 0.52, 0.56, 0.42, 0.14, "{{feature_2}}", bg_color="#FFFFFF", color="#064E3B", size_ratio=0.026, opacity=0.95),
            _badge("feature_3", 0.06, 0.73, 0.42, 0.14, "{{feature_3}}", bg_color="#FFFFFF", color="#064E3B", size_ratio=0.026, opacity=0.95),
            _badge("feature_4", 0.52, 0.73, 0.42, 0.14, "{{feature_4}}", bg_color="#FFFFFF", color="#064E3B", size_ratio=0.026, opacity=0.95),
            _badge("cta_badge", 0.06, 0.90, 0.40, 0.065, "{{cta_text}}", bg_color="#047857", color="#FFFFFF", size_ratio=0.024),
        ],
    },
    {
        "key": "new_arrival",
        "name": "Yangi mahsulot",
        "category": "launch",
        "description": "Yorqin qizil-sariq gradient, 'YANGI' belgisi urg'usi -- yangi kelgan mahsulotlar uchun.",
        "style_prompt": f"bright playful background, coral red to sunny yellow gradient, confetti-like bokeh light spots, fresh product launch mood, plain area at the bottom for overlay text, {_NO_TEXT}",
        "background": {"type": "gradient", "colors": ["#FF6B6B", "#FFD93D"], "direction": "diagonal"},
        "aspect_default": "1:1",
        "default_cta": "Birinchilardan bo'ling",
        "layers": [
            _badge("new_badge", 0.06, 0.06, 0.26, 0.10, "YANGI", bg_color="#111111", color="#FFFFFF", size_ratio=0.04),
            _logo(0.80, 0.06, 0.14, 0.09),
            _text("headline", 0.06, 0.70, 0.88, 0.12, "{{headline}}", size_ratio=0.066, color="#1A1A1A"),
            _text("subheadline", 0.06, 0.83, 0.60, 0.07, "{{subheadline}}", font="regular", size_ratio=0.03, color="#3B2A00"),
            _badge("cta_badge", 0.60, 0.88, 0.34, 0.07, "{{cta_text}}", bg_color="#111111", color="#FFFFFF", size_ratio=0.024),
        ],
    },
    {
        "key": "seasonal_promo",
        "name": "Bayram aksiyasi",
        "category": "seasonal",
        "description": "Iliq bayramona ranglar, oltin chegara bezagi -- mavsumiy/bayram aksiyalari uchun.",
        "style_prompt": f"festive warm background, deep burgundy and amber tones, soft golden bokeh lights, cozy celebratory atmosphere, gift-like elegant mood, plain area at the bottom for overlay text, {_NO_TEXT}",
        "background": {"type": "gradient", "colors": ["#7F1D1D", "#B45309"], "direction": "diagonal"},
        "aspect_default": "1:1",
        "default_cta": "Sovg'a tanlang",
        "layers": [
            _panel("border_top", 0.04, 0.04, 0.92, 0.008, "#FCD34D"),
            _panel("border_bottom", 0.04, 0.952, 0.92, 0.008, "#FCD34D"),
            _panel("border_left", 0.04, 0.04, 0.006, 0.92, "#FCD34D"),
            _panel("border_right", 0.954, 0.04, 0.006, 0.92, "#FCD34D"),
            _logo(0.40, 0.07, 0.20, 0.10, align="center"),
            _badge("offer_badge", 0.30, 0.52, 0.40, 0.10, "{{offer_text}}", bg_color="#FCD34D", color="#7F1D1D", size_ratio=0.04),
            _text("headline", 0.08, 0.66, 0.84, 0.12, "{{headline}}", align="center", size_ratio=0.058, color="#FDE68A"),
            _text("subheadline", 0.10, 0.78, 0.80, 0.06, "{{subheadline}}", align="center", font="regular", size_ratio=0.028, color="#FEF3C7"),
            _badge("cta_badge", 0.30, 0.86, 0.40, 0.065, "{{cta_text}}", bg_color="#FEF3C7", color="#7F1D1D", size_ratio=0.024),
        ],
    },
    {
        "key": "corporate_professional",
        "name": "Korporativ B2B",
        "category": "b2b",
        "description": "To'q ko'k-kulrang, jiddiy va ishonchli uslub -- B2B xizmatlar, ulgurji savdo.",
        "style_prompt": f"professional corporate background, deep navy blue and slate grey tones, subtle abstract geometric lines, modern office atmosphere, restrained and trustworthy, plain dark area at the bottom for overlay text, {_NO_TEXT}",
        "background": {"type": "gradient", "colors": ["#0F2942", "#1E3A5F"], "direction": "vertical"},
        "aspect_default": "1:1",
        "default_cta": "Taklif oling",
        "layers": [
            _logo(0.06, 0.06, 0.18, 0.09, align="left"),
            _panel("accent_bar", 0.06, 0.64, 0.08, 0.008, "#3B82F6"),
            _text("headline", 0.06, 0.67, 0.88, 0.12, "{{headline}}", size_ratio=0.056, color="#FFFFFF"),
            _text("subheadline", 0.06, 0.80, 0.88, 0.07, "{{subheadline}}", font="regular", size_ratio=0.028, color="#CBD5E1"),
            _badge("cta_badge", 0.06, 0.89, 0.36, 0.065, "{{cta_text}}", bg_color="#3B82F6", color="#FFFFFF", size_ratio=0.024),
        ],
    },
    {
        "key": "beauty_soft",
        "name": "Go'zallik pastel",
        "category": "beauty",
        "description": "Yumshoq pushti-pastel tonlar, nozik uslub -- go'zallik salonlari, kosmetika.",
        "style_prompt": f"soft pastel pink beauty background, silky smooth gradients, delicate light and airy feel, gentle glow, feminine cosmetic photography mood, plain area at the bottom for overlay text, {_NO_TEXT}",
        "background": {"type": "gradient", "colors": ["#FCE7F3", "#FBCFE8"], "direction": "vertical"},
        "aspect_default": "4:5",
        "default_cta": "Navbatga yoziling",
        "layers": [
            _logo(0.40, 0.05, 0.20, 0.09, align="center"),
            _text("headline", 0.08, 0.72, 0.84, 0.10, "{{headline}}", align="center", size_ratio=0.05, color="#831843"),
            _text("subheadline", 0.10, 0.82, 0.80, 0.06, "{{subheadline}}", align="center", font="regular", size_ratio=0.026, color="#9D174D"),
            _badge("cta_badge", 0.30, 0.90, 0.40, 0.06, "{{cta_text}}", bg_color="#BE185D", color="#FFFFFF", size_ratio=0.022),
        ],
    },
    {
        "key": "auto_dynamic",
        "name": "Avto dinamik",
        "category": "auto",
        "description": "Qora-qizil, diagonal dinamik kompozitsiya -- avtomobil, ehtiyot qismlar, sport.",
        "style_prompt": f"dynamic automotive background, black asphalt and dark metal tones with red light streaks, diagonal motion energy, dramatic low-angle lighting, plain dark area at the bottom right for overlay text, {_NO_TEXT}",
        "background": {"type": "gradient", "colors": ["#000000", "#7F1D1D"], "direction": "diagonal"},
        "aspect_default": "1:1",
        "default_cta": "Hozir bog'laning",
        "layers": [
            _panel("stripe", 0.0, 0.0, 0.05, 1.0, "#DC2626"),
            _logo(0.10, 0.06, 0.16, 0.09, align="left"),
            _text("headline", 0.10, 0.66, 0.84, 0.12, "{{headline}}", align="right", size_ratio=0.064, color="#FFFFFF"),
            _text("subheadline", 0.10, 0.79, 0.84, 0.06, "{{subheadline}}", align="right", font="regular", size_ratio=0.028, color="#FCA5A5"),
            _badge("cta_badge", 0.58, 0.88, 0.36, 0.07, "{{cta_text}}", bg_color="#DC2626", color="#FFFFFF", size_ratio=0.025),
        ],
    },
    {
        "key": "education_friendly",
        "name": "Ta'lim do'stona",
        "category": "education",
        "description": "Ochiq sariq-ko'k, do'stona va samimiy uslub -- o'quv markazlari, kurslar.",
        "style_prompt": f"friendly bright education background, light yellow and sky blue tones, cheerful classroom or study desk atmosphere, soft cartoon-like shapes, optimistic mood, plain area at the bottom for overlay text, {_NO_TEXT}",
        "background": {"type": "gradient", "colors": ["#FEF3C7", "#BFDBFE"], "direction": "diagonal"},
        "aspect_default": "1:1",
        "default_cta": "Bepul darsga yoziling",
        "layers": [
            _badge("offer_badge", 0.06, 0.06, 0.40, 0.075, "{{offer_text}}", bg_color="#F59E0B", color="#1E3A8A", size_ratio=0.026),
            _logo(0.80, 0.06, 0.14, 0.09),
            _panel("bottom_panel", 0.0, 0.66, 1.0, 0.34, "#2563EB", opacity=0.92),
            _text("headline", 0.06, 0.70, 0.88, 0.11, "{{headline}}", size_ratio=0.056, color="#FFFFFF"),
            _text("subheadline", 0.06, 0.81, 0.88, 0.06, "{{subheadline}}", font="regular", size_ratio=0.027, color="#DBEAFE"),
            _badge("cta_badge", 0.06, 0.89, 0.46, 0.065, "{{cta_text}}", bg_color="#FDE68A", color="#1E3A8A", size_ratio=0.024),
        ],
    },
    {
        "key": "collage_multi",
        "name": "Kollaj (bir nechta mahsulot)",
        "category": "collage",
        "description": "2x2 rasm zonasi + pastda umumiy matn -- bir nechta mahsulotni birga ko'rsatish uchun.",
        "style_prompt": f"flat lay composition of several products arranged in a neat 2 by 2 grid on a light grey surface, top-down view, even soft lighting, equal spacing between items, lower part of the frame kept plain for overlay text, {_NO_TEXT}",
        "background": {"type": "solid", "colors": ["#F1F5F9"]},
        "aspect_default": "1:1",
        "default_cta": "Katalogni ko'ring",
        "layers": [
            _panel("cell_1", 0.05, 0.05, 0.43, 0.30, "#FFFFFF", opacity=0.18),
            _panel("cell_2", 0.52, 0.05, 0.43, 0.30, "#FFFFFF", opacity=0.18),
            _panel("cell_3", 0.05, 0.38, 0.43, 0.30, "#FFFFFF", opacity=0.18),
            _panel("cell_4", 0.52, 0.38, 0.43, 0.30, "#FFFFFF", opacity=0.18),
            _panel("bottom_panel", 0.0, 0.72, 1.0, 0.28, "#0F172A", opacity=0.9),
            _text("headline", 0.06, 0.75, 0.66, 0.10, "{{headline}}", size_ratio=0.048, color="#FFFFFF"),
            _text("subheadline", 0.06, 0.85, 0.66, 0.06, "{{subheadline}}", font="regular", size_ratio=0.025, color="#CBD5E1"),
            _badge("cta_badge", 0.74, 0.77, 0.22, 0.06, "{{cta_text}}", bg_color="#38BDF8", color="#0F172A", size_ratio=0.02),
            _logo(0.76, 0.86, 0.18, 0.09),
        ],
    },
    {
        "key": "story_fullbleed",
        "name": "Story to'liq ekran",
        "category": "story",
        "description": "9:16 Story/Reels uchun: to'liq ekran rasm, pastda qorong'i gradient va matn.",
        "style_prompt": f"vertical full-frame lifestyle background for a mobile story, subject in the upper two thirds, rich natural colors, cinematic lighting, lower third gradually darker and plain for overlay text, {_NO_TEXT}",
        "background": {"type": "gradient", "colors": ["#334155", "#0F172A"], "direction": "vertical"},
        "aspect_default": "9:16",
        "default_cta": "Yuqoriga suring",
        "layers": [
            _logo(0.40, 0.04, 0.20, 0.06, align="center"),
            _panel("overlay", 0.0, 0.55, 1.0, 0.45, "#000000", opacity=0.85, gradient=True),
            _text("headline", 0.07, 0.72, 0.86, 0.10, "{{headline}}", size_ratio=0.042, color="#FFFFFF"),
            _text("subheadline", 0.07, 0.82, 0.86, 0.05, "{{subheadline}}", font="regular", size_ratio=0.02, color="#E2E8F0"),
            _badge("cta_badge", 0.07, 0.89, 0.50, 0.045, "{{cta_text}}", bg_color="#FFFFFF", color="#0F172A", size_ratio=0.018),
        ],
    },
    {
        "key": "price_tag_highlight",
        "name": "Narx yorlig'i",
        "category": "sale",
        "description": "Narxni KATTA ko'rsatadigan sariq yorliq elementi -- aniq narxli takliflar uchun.",
        "style_prompt": f"clean commercial background in fresh sky blue tones, product centered with soft shadow, bright even studio lighting, simple and clear, empty area on the right side and bottom for overlay price tag and text, {_NO_TEXT}",
        "background": {"type": "gradient", "colors": ["#0EA5E9", "#0369A1"], "direction": "vertical"},
        "aspect_default": "1:1",
        "default_cta": "Hoziroq buyurtma bering",
        "layers": [
            _logo(0.06, 0.06, 0.18, 0.09, align="left"),
            _panel("tag_shadow", 0.585, 0.105, 0.36, 0.20, "#0C4A6E", opacity=0.6),
            _badge("price_badge", 0.57, 0.09, 0.36, 0.20, "{{price_text}}", bg_color="#FACC15", color="#1F2937", size_ratio=0.052),
            _text("headline", 0.06, 0.68, 0.88, 0.12, "{{headline}}", size_ratio=0.06, color="#FFFFFF"),
            _text("subheadline", 0.06, 0.81, 0.88, 0.06, "{{subheadline}}", font="regular", size_ratio=0.028, color="#E0F2FE"),
            _badge("cta_badge", 0.06, 0.89, 0.50, 0.07, "{{cta_text}}", bg_color="#FACC15", color="#1F2937", size_ratio=0.025),
        ],
    },
]

TEMPLATES_BY_KEY = {t["key"]: t for t in CREATIVE_TEMPLATES}

TEMPLATE_CATEGORIES = sorted({t["category"] for t in CREATIVE_TEMPLATES})


def get_template(key: str) -> "dict | None":
    """Kalit bo'yicha shablon (topilmasa `None`). Qaytarilgan dict'ni
    O'ZGARTIRMANG -- `copy.deepcopy` qiling (`creative_studio.resolve_layers`
    shunday qiladi)."""
    return TEMPLATES_BY_KEY.get((key or "").strip())
