"""creative_studio.py — KREATIV STUDIYA: AI rasm-generatsiya yadrosi (2026-09,
foydalanuvchi so'rovi: "kompaniya brifidan kelib chiqib OpenAI orqali
to'liq tayyor, brend logotipi ilova qilingan, matni chotki reklama rasmini
AI generatsiya qilib bersin; ma'lumot yetmasa -- generatsiyadan OLDIN
savol so'rasin; natijani tahrirlash, PNG/PDF eksport, Autopilot'ga tashlash
mumkin bo'lsin; 20 ta tayyor shablon; tariflar bo'yicha oylik limit").

Bu modul TOZA Python (Flask/Jinja'ga bog'liq emas -- xuddi
`campaign_draft.py`/`ai_campaign_planner.py` kabi); web-qatlam (marshrutlar,
canvas-muharrir UI) KEYINGI bosqichda shu funksiyalar ustiga quriladi.

ASOSIY ARXITEKTURAVIY QAROR (web-agent uchun ham muhim):
  OpenAI rasm modellari matnni (ayniqsa lotin-o'zbekcha apostrofli
  so'zlarni: "o'quv", "so'm", "ko'ring") rasm ICHIGA ISHONCHLI chizmaydi.
  Shuning uchun:
    1. OpenAI'dan FAQAT matnsiz fon/mahsulot tasviri olinadi
       (`generate_base_image` -> `base_image_storage_path`; promptda doim
       "no text, no words, no letters" ko'rsatmasi bor).
    2. Sarlavha/tavsif/CTA/logotip Pillow orqali dasturiy ravishda, aniq
       shrift (fonts/DejaVuSans*.ttf) bilan rasm USTIGA chiziladi
       (`render_composite`, qatlamlar `layers_json`da -- 0..1 nisbiy
       koordinatalar, sxemasi `creative_templates.py`da).
    3. Foydalanuvchi qatlamlarni (matn/pozitsiya/rang) muharrirda
       o'zgartiradi -> `set_layers()` -> server QAYTA chizadi. Server HAR
       DOIM "haqiqat manbai": brauzer canvas'i faqat oldindan ko'rish.
  Natija: matn 100% aniq va o'qiladigan, logotip haqiqiy fayl, tahrirlash
  OpenAI'ni qayta chaqirmaydi (kvota sarflanmaydi).

OQIM (web-agent shunga tayanadi):
  create_draft_asset() -> [missing_questions() bo'sh bo'lguncha
  submit_brief_answer()] -> generate_base_image() -> get_layers()/
  set_layers() (tahrir) -> export_png_path()/export_pdf() yoki
  asset_image_bytes() (Autopilot media).
  Shablondan tezkor: create_from_template() (OpenAI'siz, kvota sarflamaydi).

Kvota: `plans.Plan.image_generation_monthly_limit` (None=cheksiz, 0=yopiq)
+ `db.ImageGenerationUsage` (kompaniya+oy). `increment_usage()` FAQAT rasm
HAQIQATAN olingandan keyin chaqiriladi -- xato bo'lsa kvota sarflanmaydi.
"""

import io
import os
import re
import copy
import json
import base64
import logging
import datetime as dt
from pathlib import Path

import requests

import db
import creative_templates
import company_context as company_context_module
from call_analysis import (  # noqa: F401 -- umumiy OpenAI infratuzilmasi QAYTA ishlatiladi
    _openai_request, _extract_openai_error, _is_quota_exhausted_response, OpenAICreditExhaustedError,
)

logger = logging.getLogger("creative_studio")

BASE_DIR = Path(__file__).parent
# Testlar/deploy bu qiymatlarni o'zgartirishi mumkin -- funksiyalar ularni
# CHAQIRUV paytida o'qiydi (modul yuklanganda qotib qolmaydi), xuddi
# `campaign_media.MEDIA_ROOT` kabi.
CREATIVE_ROOT = BASE_DIR / "uploads" / "creative_studio"
BRAND_ROOT = BASE_DIR / "uploads" / "brand_kit"
FONTS_DIR = BASE_DIR / "fonts"
FONT_BOLD = FONTS_DIR / "DejaVuSans-Bold.ttf"
FONT_REGULAR = FONTS_DIR / "DejaVuSans.ttf"

OPENAI_IMAGE_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-1")
# Xarajatni nazorat qilish uchun standart "medium" ("high" emas).
OPENAI_IMAGE_QUALITY = os.environ.get("OPENAI_IMAGE_QUALITY", "medium")
OPENAI_IMAGES_URL = "https://api.openai.com/v1/images/generations"
_OPENAI_IMAGE_TIMEOUT = 180

ALLOWED_LOGO_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
_LOGO_EXT_TO_TYPE = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
MAX_LOGO_BYTES = 8 * 1024 * 1024
MAX_PRODUCT_IMAGE_BYTES = 30 * 1024 * 1024

ASPECTS = ("1:1", "4:5", "9:16")
ASSET_KINDS = ("ai_generated", "template", "upload")
LAYER_TYPES = ("text", "badge", "panel", "logo")
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-z_0-9]+)\s*\}\}")
_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


class CreativeError(Exception):
    """Foydalanuvchiga ko'rsatiladigan o'zbekcha xato (xuddi
    `campaign_media.MediaError`/`campaign_draft.DraftPatchError` kabi).
    Xom OpenAI/tarmoq matni HECH QACHON bu xabarga tushmaydi -- faqat log."""


class QuotaExceededError(CreativeError):
    """Oylik AI rasm-generatsiya limiti tugagan yoki tarifda yopiq."""


# ---------------------------------------------------------------------------
# KVOTA
# ---------------------------------------------------------------------------

def current_period_key(now=None) -> str:
    """Joriy oy kaliti, masalan "2026-09" (UTC)."""
    now = now or dt.datetime.utcnow()
    return now.strftime("%Y-%m")


def get_usage(session, company_id: int, period: "str | None" = None) -> int:
    """Berilgan kompaniyaning shu oydagi (yoki `period` oyidagi)
    muvaffaqiyatli generatsiyalar soni (qator bo'lmasa 0)."""
    period = period or current_period_key()
    with db.scoped_as(company_id):
        row = (
            session.query(db.ImageGenerationUsage)
            .filter(db.ImageGenerationUsage.company_id == company_id, db.ImageGenerationUsage.period_key == period)
            .first()
        )
    return int(row.count or 0) if row else 0


def check_quota(company, plan_def, session, *, need: int = 1) -> None:
    """`plan_def.image_generation_monthly_limit` bilan joriy oylik
    ishlatishni solishtiradi. Limit 0 (tarifda yopiq) yoki tugagan bo'lsa
    `QuotaExceededError` (o'zbekcha, aniq son bilan). `None` limit --
    cheksiz, hech narsa tekshirmaydi."""
    limit = getattr(plan_def, "image_generation_monthly_limit", 0)
    if limit is None:
        return
    plan_name = getattr(plan_def, "name", None) or "joriy"
    if limit <= 0:
        raise QuotaExceededError(
            f"\"{plan_name}\" tarifida AI rasm-generatsiya mavjud emas. "
            "Kreativ studiyadan foydalanish uchun tarifni oshiring."
        )
    used = get_usage(session, company.id)
    if used + need > limit:
        raise QuotaExceededError(
            f"Bu oy uchun rasm generatsiya limitingiz tugadi ({used}/{limit}). "
            "Keyingi oyda yangilanadi yoki tarifni oshiring."
        )


def increment_usage(session, company_id: int) -> None:
    """Joriy oy sanog'ini +1 qiladi (qator yo'q bo'lsa yaratadi) va commit
    qiladi. FAQAT muvaffaqiyatli generatsiyadan KEYIN chaqiriladi."""
    period = current_period_key()
    with db.scoped_as(company_id):
        row = (
            session.query(db.ImageGenerationUsage)
            .filter(db.ImageGenerationUsage.company_id == company_id, db.ImageGenerationUsage.period_key == period)
            .first()
        )
    if row is None:
        row = db.ImageGenerationUsage(company_id=company_id, period_key=period, count=0)
        session.add(row)
    row.count = int(row.count or 0) + 1
    row.updated_at = dt.datetime.utcnow()
    session.commit()


def quota_status(session, company, plan_def) -> dict:
    """UI uchun: {"used", "limit" (None=cheksiz), "remaining" (None=cheksiz),
    "enabled" (tarifda ochiqmi), "period"}."""
    limit = getattr(plan_def, "image_generation_monthly_limit", 0)
    used = get_usage(session, company.id) if company is not None else 0
    remaining = None if limit is None else max(0, int(limit) - used)
    return {"used": used, "limit": limit, "remaining": remaining, "enabled": limit is None or limit > 0,
            "period": current_period_key()}


# ---------------------------------------------------------------------------
# BREND KIT (logotip + ranglar)
# ---------------------------------------------------------------------------

def get_brand_kit(session, company_id: int) -> "db.CompanyBrandKit | None":
    with db.scoped_as(company_id):
        return session.query(db.CompanyBrandKit).filter(db.CompanyBrandKit.company_id == company_id).first()


def _get_or_create_brand_kit(session, company_id: int) -> "db.CompanyBrandKit":
    kit = get_brand_kit(session, company_id)
    if kit is None:
        kit = db.CompanyBrandKit(company_id=company_id)
        session.add(kit)
    return kit


def _read_bytes(file_storage_or_bytes) -> bytes:
    if isinstance(file_storage_or_bytes, (bytes, bytearray)):
        return bytes(file_storage_or_bytes)
    stream = getattr(file_storage_or_bytes, "stream", None) or file_storage_or_bytes
    if hasattr(stream, "read"):
        data = stream.read()
        return data if isinstance(data, bytes) else bytes(data)
    raise CreativeError("Fayl o'qib bo'lmadi.")


def _detect_logo_type(filename: str, content_type: "str | None") -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in ALLOWED_LOGO_TYPES:
        return ct
    ext = os.path.splitext(filename or "")[1].lower()
    return _LOGO_EXT_TO_TYPE.get(ext, ct or "application/octet-stream")


def save_brand_logo(session, company_id: int, file_storage_or_bytes, filename: str, content_type: "str | None") -> "db.CompanyBrandKit":
    """`campaign_media.save_uploaded_media` uslubida: turi/hajmini tekshiradi
    (faqat PNG/JPG/WEBP, shaffof fon uchun PNG tavsiya etiladi, max 8MB),
    `BRAND_ROOT/<company_id>/logo.<ext>`ga saqlaydi, `CompanyBrandKit`
    qatorini yaratadi/yangilaydi (commit qiladi)."""
    ct = _detect_logo_type(filename, content_type)
    if ct not in ALLOWED_LOGO_TYPES:
        raise CreativeError("Logotip faqat PNG, JPG yoki WEBP formatida bo'lishi mumkin (shaffof fon uchun PNG tavsiya etiladi).")
    data = _read_bytes(file_storage_or_bytes)
    if not data:
        raise CreativeError("Fayl bo'sh.")
    if len(data) > MAX_LOGO_BYTES:
        raise CreativeError(f"Logotip juda katta -- chegara {MAX_LOGO_BYTES // (1024 * 1024)} MB.")
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
    except Exception as e:  # noqa: BLE001 -- buzilgan/soxta rasm
        raise CreativeError("Logotip faylini o'qib bo'lmadi -- fayl buzilgan yoki rasm emas.") from e

    ext = ALLOWED_LOGO_TYPES[ct]
    rel_dir = Path(str(company_id))
    abs_dir = Path(BRAND_ROOT) / rel_dir
    abs_dir.mkdir(parents=True, exist_ok=True)
    # Eski logotip (boshqa kengaytmali) qolib ketmasin.
    for old in abs_dir.glob("logo.*"):
        try:
            old.unlink()
        except OSError:
            pass
    (abs_dir / f"logo{ext}").write_bytes(data)

    kit = _get_or_create_brand_kit(session, company_id)
    kit.logo_storage_path = str(rel_dir / f"logo{ext}")
    kit.logo_content_type = ct
    kit.updated_at = dt.datetime.utcnow()
    session.commit()
    return kit


def _normalize_color(value: "str | None") -> "str | None":
    if not value:
        return None
    v = str(value).strip()
    if not v.startswith("#"):
        v = "#" + v
    if len(v) == 4:  # #RGB -> #RRGGBB
        v = "#" + "".join(ch * 2 for ch in v[1:])
    if not _HEX_COLOR_RE.match(v):
        raise CreativeError(f"Rang noto'g'ri formatda: {value!r} (kutilgan: #RRGGBB).")
    return v.upper()


def save_brand_colors(session, company_id: int, primary: "str | None", secondary: "str | None") -> "db.CompanyBrandKit":
    """Brend ranglarini ("#RRGGBB"; bo'sh -> NULL) saqlaydi va commit qiladi."""
    kit = _get_or_create_brand_kit(session, company_id)
    kit.primary_color = _normalize_color(primary)
    kit.secondary_color = _normalize_color(secondary)
    kit.updated_at = dt.datetime.utcnow()
    session.commit()
    return kit


def brand_logo_file_path(brand_kit) -> "Path | None":
    """Logotipning diskdagi to'liq yo'li (brend kit/logotip bo'lmasa `None`)."""
    if brand_kit is None or not getattr(brand_kit, "logo_storage_path", None):
        return None
    return Path(BRAND_ROOT) / brand_kit.logo_storage_path


# ---------------------------------------------------------------------------
# BRIF (savol-javob oqimi, generatsiyadan OLDIN)
# ---------------------------------------------------------------------------
# (key, savol matni, placeholder, ixtiyoriymi) -- `business_profile.
# BUSINESS_PROFILE_QUESTIONS` uslubida, lekin bular REKLAMA-RASMga xos.
# "focus" -- kompaniya profilida `product_or_service` bo'sh bo'lsa MAJBURIY
# (aks holda taklif sifatida ko'rsatiladi, javob bo'lmasa profil ishlatiladi).
CREATIVE_BRIEF_QUESTIONS = [
    ("focus", "Aynan qaysi mahsulot/xizmatga urg'u berilsin?",
     "Masalan: erkaklar klassik tuflisi, yangi kolleksiya", True),
    ("offer_text", "Aksiya/chegirma yoki maxsus taklif bormi? (bo'lsa matnini yozing, bo'lmasa 'yo'q' deb qoldiring)",
     "Masalan: -30% chegirma, bepul yetkazib berish", True),
    ("cta_preference", "Qanday chaqiriq matni bo'lsin? (bo'sh qoldirsangiz AI o'zi tanlaydi)",
     "Masalan: Hoziroq buyurtma bering, Batafsil", True),
    ("style_notes", "Rasm qanday ko'rinishda bo'lsin? (muhit, rang, kayfiyat -- ixtiyoriy)",
     "Masalan: oq studiya foni, tabiiy yorug'lik, premium ko'rinish", True),
]
_BRIEF_BY_KEY = {k: (k, q, ph, opt) for k, q, ph, opt in CREATIVE_BRIEF_QUESTIONS}
_NEGATIVE_ANSWERS = {"yo'q", "yoq", "yo`q", "yoʻq", "нет", "no", "-", "yok"}


def _answered(value) -> bool:
    return bool(value is not None and str(value).strip())


def _is_negative(value) -> bool:
    return str(value or "").strip().lower() in _NEGATIVE_ANSWERS


def _question_dict(key: str, *, required: bool) -> dict:
    k, q, ph, opt = _BRIEF_BY_KEY[key]
    return {"key": k, "question": q, "placeholder": ph, "optional": (opt and not required), "required": required}


def missing_questions(ctx: dict, existing_answers: dict) -> list[dict]:
    """`company_context.build_company_context()` natijasi (ctx) VA
    hozirgacha yig'ilgan `existing_answers`dan kelib chiqib HALI kerak
    bo'lgan savollarni qaytaradi.

    Qoida: `ctx['missing_fields']`da 'product_or_service' bo'lsa VA
    `existing_answers['focus']` ham bo'sh bo'lsa -- 'focus' MAJBURIY
    (`required=True`). Kamida bitta majburiy savol bo'lsa, hali javob
    berilmagan (kaliti `existing_answers`da umuman yo'q) ixtiyoriy
    savollar ham BIR MARTA birga qaytariladi (foydalanuvchi bitta formada
    hammasini ko'rsin). Bo'sh ro'yxat = hammasi yetarli, generatsiyaga
    tayyor. (Ixtiyoriy savollarni UI istalgan vaqtda alohida ham
    ko'rsatishi mumkin -- `CREATIVE_BRIEF_QUESTIONS`.)"""
    ctx = ctx or {}
    existing_answers = existing_answers or {}
    out: list[dict] = []
    product_missing = "product_or_service" in (ctx.get("missing_fields") or []) or not (ctx.get("profile_answers") or {}).get("product_or_service")
    if product_missing and not _answered(existing_answers.get("focus")):
        out.append(_question_dict("focus", required=True))
    if not out:
        return []
    for key, *_ in CREATIVE_BRIEF_QUESTIONS:
        if key == "focus" or key in existing_answers:
            continue
        out.append(_question_dict(key, required=False))
    return out


def _load_company(session, asset) -> "db.Company | None":
    if asset is None or asset.company_id is None:
        return None
    return session.get(db.Company, asset.company_id)


def _refresh_missing(session, asset, company=None) -> list[dict]:
    company = company or _load_company(session, asset)
    ctx = company_context_module.build_company_context(company, session)
    missing = missing_questions(ctx, asset.get_brief_answers())
    asset.missing_fields_json = json.dumps([q["key"] for q in missing], ensure_ascii=False)
    return missing


def create_draft_asset(session, company, manager_id: "int | None", *, template_key: "str | None" = None, aspect: str = "1:1") -> "db.CreativeAsset":
    """Yangi `CreativeAsset(status='collecting_brief')` yaratadi (commit) va
    qaytaradi. `template_key` berilsa mavjudligi tekshiriladi va aspekt
    berilmagan bo'lsa shablonning `aspect_default`i olinadi."""
    template = None
    if template_key:
        template = creative_templates.get_template(template_key)
        if template is None:
            raise CreativeError("Bunday shablon topilmadi.")
    aspect = (aspect or "").strip() or (template["aspect_default"] if template else "1:1")
    if aspect not in ASPECTS:
        raise CreativeError("Rasm nisbati faqat 1:1, 4:5 yoki 9:16 bo'lishi mumkin.")
    asset = db.CreativeAsset(
        company_id=company.id, created_by_manager_id=manager_id, kind="ai_generated",
        status="collecting_brief", template_key=template["key"] if template else None, aspect=aspect,
    )
    asset.set_brief_answers({})
    session.add(asset)
    session.commit()
    _refresh_missing(session, asset, company)
    session.commit()
    return asset


def submit_brief_answer(session, asset: "db.CreativeAsset", key: str, value: str) -> "db.CreativeAsset":
    """`brief_answers_json`ga bitta javobni qo'shib (bo'sh javob ham
    "ko'rib chiqildi" deb saqlanadi -- ixtiyoriy savol qayta so'ralmasin),
    `missing_fields_json`ni qayta hisoblab commit qiladi."""
    key = (key or "").strip()
    if key not in _BRIEF_BY_KEY:
        raise CreativeError("Noma'lum brif savoli.")
    answers = asset.get_brief_answers()
    answers[key] = (value or "").strip()[:500]
    asset.set_brief_answers(answers)
    _refresh_missing(session, asset)
    asset.updated_at = dt.datetime.utcnow()
    session.commit()
    return asset


# ---------------------------------------------------------------------------
# PROMPT + PLACEHOLDER QIYMATLARI
# ---------------------------------------------------------------------------

def _short(text: "str | None", limit: int = 48) -> str:
    """Sarlavha uchun QISQA matn: birinchi jumla/bo'lak, `limit` belgigacha."""
    t = re.split(r"[.;\n]|,\s", (text or "").strip(), maxsplit=1)[0].strip()
    if len(t) > limit:
        cut = t[:limit].rsplit(" ", 1)[0]
        t = (cut or t[:limit]).rstrip(" ,-")
    return t


def _split_features(text: "str | None", n: int = 4) -> list[str]:
    parts = [p.strip(" .") for p in re.split(r"[,;\n]|\s-\s|\s\|\s", text or "") if p.strip(" .")]
    return [_short(p, 32) for p in parts[:n]]


def placeholder_values(ctx: dict, brief_answers: dict, template: "dict | None") -> dict:
    """Shablon/standart qatlamlardagi `{{...}}` placeholder'lar uchun REAL
    matnlar (o'zbekcha, kompaniya konteksti + brif javoblaridan):
      headline    -- focus (brif) yoki best_seller yoki product_or_service (QISQA)
      subheadline -- offer_text ("yo'q" bo'lmasa) yoki extra_notes
      cta_text    -- cta_preference yoki shablonning default_cta yoki "Batafsil"
      offer_text  -- offer_text yoki "AKSIYA"; price_text -- price_range;
      brand_name  -- kompaniya nomi; quote_text -- extra_notes/offer;
      feature_1..4 -- extra_notes'dan vergul bo'yicha bo'laklar.
    Bo'sh qiymatli placeholder'li qatlam chizilmaydi (`resolve_layers`)."""
    ctx = ctx or {}
    brief_answers = brief_answers or {}
    profile = ctx.get("profile_answers") or {}
    focus = (brief_answers.get("focus") or "").strip()
    offer = (brief_answers.get("offer_text") or "").strip()
    if _is_negative(offer):
        offer = ""
    cta = (brief_answers.get("cta_preference") or "").strip()
    extra = (profile.get("extra_notes") or "").strip()
    headline_src = focus or profile.get("best_seller") or profile.get("product_or_service") or ctx.get("company_name") or ""
    features = _split_features(extra)
    values = {
        "headline": _short(headline_src, 48),
        "subheadline": _short(offer, 70) if offer else _short(extra, 70),
        "cta_text": cta or ((template or {}).get("default_cta") or "Batafsil"),
        "offer_text": _short(offer, 28) if offer else "AKSIYA",
        "price_text": _short(profile.get("price_range") or "", 24),
        "brand_name": (ctx.get("company_name") or "").strip(),
        "quote_text": _short(extra or offer, 110),
    }
    for i in range(4):
        values[f"feature_{i + 1}"] = features[i] if i < len(features) else ""
    return values


def build_image_prompt(ctx: dict, brief_answers: dict, template: "dict | None") -> str:
    """company_context + brif javoblari + (tanlangan bo'lsa) shablonning
    `style_prompt`idan YAGONA ingliz promptini quradi. HAR DOIM "no text,
    no words, no letters" ko'rsatmasi qo'shiladi (matnni Pillow chizadi).
    Kompaniya matnlari o'zbekcha bo'lishi mumkin -- OpenAI ko'p tilli,
    lekin ko'rsatmalarning o'zi inglizcha (ishonchliroq)."""
    ctx = ctx or {}
    brief_answers = brief_answers or {}
    profile = ctx.get("profile_answers") or {}
    category = ((ctx.get("business_category") or {}).get("label")) or ""
    focus = (brief_answers.get("focus") or "").strip() or (profile.get("best_seller") or "").strip() or (profile.get("product_or_service") or "").strip()
    audience = (profile.get("target_audience") or "").strip()
    price_range = (profile.get("price_range") or "").strip()
    style_notes = (brief_answers.get("style_notes") or "").strip()
    offer = (brief_answers.get("offer_text") or "").strip()
    brand = ctx.get("brand_kit") or {}

    lines = ["Professional advertising background image for a Facebook/Instagram ad (Uzbekistan market)."]
    if category:
        lines.append(f"Business type: {category}.")
    if focus:
        lines.append(f"Main subject to depict: {focus}. Show the product/service visually, realistic and appealing.")
    if audience:
        lines.append(f"Target audience: {audience}.")
    if price_range:
        lines.append(f"Price segment: {price_range} (match the visual tone: premium vs. affordable).")
    if offer and not _is_negative(offer):
        lines.append("Mood: promotional / special offer, energetic but tasteful.")
    if template and template.get("style_prompt"):
        lines.append(f"Visual style: {template['style_prompt']}.")
    else:
        lines.append("Visual style: clean, modern, high-quality commercial photography, soft studio lighting, plenty of clean negative space in the lower third for overlay text.")
    if style_notes:
        lines.append(f"Additional style notes from the client: {style_notes}.")
    colors = [c for c in (brand.get("primary_color"), brand.get("secondary_color")) if c]
    if colors:
        lines.append(f"Brand color accents to incorporate subtly: {', '.join(colors)}.")
    lines.append(
        "STRICT: no text, no words, no letters, no numbers, no typography, no captions, no logos, "
        "no watermarks, no signs with writing anywhere in the image. Leave clean negative space for overlay text. "
        "No people's faces in close-up. Photorealistic unless the style says otherwise."
    )
    return "\n".join(lines)


def _size_for_aspect(aspect: str) -> str:
    """OpenAI images API uchun ruxsat etilgan o'lcham: '1:1'->'1024x1024',
    '4:5' yoki '9:16'->'1024x1536' (portret), aks holda '1024x1024'.
    (dall-e-3 modeli uchun portret '1024x1792'.)"""
    portrait = "1024x1792" if OPENAI_IMAGE_MODEL.startswith("dall-e") else "1024x1536"
    return portrait if aspect in ("4:5", "9:16") else "1024x1024"


def _target_pixels_for_aspect(aspect: str) -> "tuple[int, int]":
    """Yakuniy eksport piksel o'lchami (Meta tavsiyalari): '1:1'->(1080,1080),
    '4:5'->(1080,1350), '9:16'->(1080,1920)."""
    return {"1:1": (1080, 1080), "4:5": (1080, 1350), "9:16": (1080, 1920)}.get(aspect, (1080, 1080))


# ---------------------------------------------------------------------------
# OPENAI IMAGES API
# ---------------------------------------------------------------------------
_OPENAI_GENERIC_MSG = (
    "AI rasm xizmati hozir javob bera olmadi. Birozdan keyin qayta urinib ko'ring "
    "yoki shablondan foydalaning."
)
_OPENAI_CREDIT_MSG = (
    "AI rasm xizmati (OpenAI) balansi tugagan -- administrator balansni to'ldirib "
    "qo'ysin, keyin qayta urinib ko'ring."
)
_SAFETY_MARKERS = ("safety", "moderation", "content_policy", "content policy", "not allowed")


def _request_openai_image(prompt: str, size: str) -> "tuple[bytes, str | None]":
    """OpenAI Images API'ga POST. Qaytaradi: (PNG baytlar, response id).
    Barcha xatolar `CreativeError` (o'zbekcha, friendly) sifatida
    ko'tariladi; xom matn faqat logga yoziladi."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise CreativeError(
            "OPENAI_API_KEY sozlanmagan -- AI rasm-generatsiya ishlamaydi. "
            "Administrator sozlab qo'ysin (shablondan foydalanish mumkin)."
        )
    body = {"model": OPENAI_IMAGE_MODEL, "prompt": prompt, "size": size, "n": 1}
    if OPENAI_IMAGE_MODEL.startswith("gpt-image"):
        body["quality"] = OPENAI_IMAGE_QUALITY
    else:
        # dall-e-* modellari b64 uchun aniq so'rov talab qiladi.
        body["response_format"] = "b64_json"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    try:
        resp = _openai_request("POST", OPENAI_IMAGES_URL, headers=headers, json_body=body, timeout=_OPENAI_IMAGE_TIMEOUT)
    except requests.RequestException as e:
        logger.error("creative_studio: OpenAI tarmoq xatosi: %s", e)
        raise CreativeError(_OPENAI_GENERIC_MSG) from e
    if resp.status_code == 429 and _is_quota_exhausted_response(resp):
        logger.error("creative_studio: OpenAI kredit tugagan: %s", _extract_openai_error(resp))
        raise CreativeError(_OPENAI_CREDIT_MSG)
    if resp.status_code != 200:
        raw = _extract_openai_error(resp)
        logger.error("creative_studio: OpenAI images xatosi HTTP %s: %s", resp.status_code, raw)
        if resp.status_code == 400 and any(m in raw.lower() for m in _SAFETY_MARKERS):
            raise CreativeError(
                "AI bu brif bo'yicha rasm yaratishdan bosh tortdi (xavfsizlik qoidalari). "
                "Mahsulot tavsifi yoki uslub izohini o'zgartirib qayta urinib ko'ring."
            )
        raise CreativeError(_OPENAI_GENERIC_MSG)
    try:
        payload = resp.json()
        item = (payload.get("data") or [])[0]
    except Exception as e:  # noqa: BLE001 -- kutilmagan javob shakli
        logger.error("creative_studio: OpenAI javobini o'qib bo'lmadi: %s", e)
        raise CreativeError(_OPENAI_GENERIC_MSG) from e
    data = None
    if item.get("b64_json"):
        try:
            data = base64.b64decode(item["b64_json"])
        except Exception as e:  # noqa: BLE001
            logger.error("creative_studio: b64 dekod xatosi: %s", e)
            raise CreativeError(_OPENAI_GENERIC_MSG) from e
    elif item.get("url"):
        try:
            r = requests.get(item["url"], timeout=60)
            r.raise_for_status()
            data = r.content
        except requests.RequestException as e:
            logger.error("creative_studio: rasm URL yuklab bo'lmadi: %s", e)
            raise CreativeError(_OPENAI_GENERIC_MSG) from e
    if not data:
        raise CreativeError(_OPENAI_GENERIC_MSG)
    response_id = payload.get("id") or (str(payload["created"]) if payload.get("created") else None)
    return data, response_id


# ---------------------------------------------------------------------------
# QATLAMLAR (layers)
# ---------------------------------------------------------------------------

def default_layers() -> list[dict]:
    """Shablon tanlanmagan holat uchun ODDIY standart qatlamlar
    (headline/subheadline/cta_badge/logo) -- shablon sxemasi bilan bir xil."""
    return [
        {"id": "cta_badge", "type": "badge", "x": 0.06, "y": 0.06, "w": 0.34, "h": 0.07, "align": "center",
         "font": "bold", "size_ratio": 0.026, "color": "#FFFFFF", "bg_color": "#111111", "opacity": 0.92, "text": "{{cta_text}}"},
        {"id": "logo", "type": "logo", "x": 0.80, "y": 0.06, "w": 0.14, "h": 0.09, "align": "right"},
        {"id": "bottom_panel", "type": "panel", "x": 0.0, "y": 0.62, "w": 1.0, "h": 0.38, "bg_color": "#000000", "opacity": 0.75, "gradient": True},
        {"id": "headline", "type": "text", "x": 0.06, "y": 0.72, "w": 0.88, "h": 0.12, "align": "left",
         "font": "bold", "size_ratio": 0.062, "color": "#FFFFFF", "text": "{{headline}}"},
        {"id": "subheadline", "type": "text", "x": 0.06, "y": 0.85, "w": 0.88, "h": 0.08, "align": "left",
         "font": "regular", "size_ratio": 0.03, "color": "#E5E7EB", "text": "{{subheadline}}"},
    ]


def resolve_layers(layers: list[dict], values: dict) -> list[dict]:
    """Qatlamlardagi `{{placeholder}}`larni `values`dagi real matn bilan
    almashtiradi (nusxa qaytaradi, asl ro'yxat o'zgarmaydi). Qiymati bo'sh
    placeholder -> matn bo'sh (render o'tkazib yuboradi; `badge` bo'lsa
    faqat to'rtburchak ham chizilmaydi -- bo'sh badge ma'nosiz)."""
    out = []
    for layer in copy.deepcopy(layers or []):
        text = layer.get("text")
        if isinstance(text, str) and "{{" in text:
            layer["text"] = _PLACEHOLDER_RE.sub(lambda m: str(values.get(m.group(1), "") or ""), text).strip()
            if not layer["text"]:
                # Qiymati yo'q placeholder -- qatlam yashirin (muharrirda
                # foydalanuvchi matn kiritib qayta yoqishi mumkin).
                layer["hidden"] = True
        out.append(layer)
    return out


def _clamp01(v, default=0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, f))


def sanitize_layers(layers) -> list[dict]:
    """Muharrirdan (brauzer) kelgan qatlamlarni TEKSHIRADI/tozalaydi:
    faqat ma'lum `type`lar, koordinatalar 0..1, ranglar #RRGGBB, matn
    uzunligi chegaralangan. Noto'g'ri element -> `CreativeError`."""
    if not isinstance(layers, list):
        raise CreativeError("Qatlamlar ro'yxat bo'lishi kerak.")
    if len(layers) > 40:
        raise CreativeError("Juda ko'p qatlam (maksimum 40).")
    clean: list[dict] = []
    for i, raw in enumerate(layers):
        if not isinstance(raw, dict):
            raise CreativeError(f"{i + 1}-qatlam noto'g'ri formatda.")
        ltype = str(raw.get("type") or "").strip()
        if ltype not in LAYER_TYPES:
            raise CreativeError(f"{i + 1}-qatlam turi noma'lum: {ltype!r}.")
        layer = {
            "id": re.sub(r"[^A-Za-z0-9_-]", "", str(raw.get("id") or f"layer_{i + 1}"))[:40] or f"layer_{i + 1}",
            "type": ltype,
            "x": _clamp01(raw.get("x"), 0.0), "y": _clamp01(raw.get("y"), 0.0),
            "w": _clamp01(raw.get("w"), 0.1), "h": _clamp01(raw.get("h"), 0.1),
        }
        if ltype in ("text", "badge"):
            layer["align"] = raw.get("align") if raw.get("align") in ("left", "center", "right") else ("center" if ltype == "badge" else "left")
            layer["font"] = "bold" if raw.get("font", "bold") == "bold" else "regular"
            sr = raw.get("size_ratio")
            try:
                sr = float(sr)
            except (TypeError, ValueError):
                sr = 0.04
            layer["size_ratio"] = max(0.008, min(0.5, sr))
            layer["color"] = _normalize_color(raw.get("color")) or "#111111"
            layer["text"] = str(raw.get("text") or "")[:300]
        if ltype in ("badge", "panel"):
            layer["bg_color"] = _normalize_color(raw.get("bg_color")) or "#111111"
            layer["opacity"] = _clamp01(raw.get("opacity", 1.0), 1.0)
        if ltype == "panel":
            layer["gradient"] = bool(raw.get("gradient"))
        if ltype == "logo":
            layer["align"] = raw.get("align") if raw.get("align") in ("left", "center", "right") else "right"
        if raw.get("hidden"):
            layer["hidden"] = True
        clean.append(layer)
    return clean


def get_layers(asset) -> list[dict]:
    """`layers_json`dagi qatlamlar (bo'sh bo'lsa [])."""
    return asset.get_layers() if asset is not None else []


def set_layers(session, asset, layers: list[dict]) -> "db.CreativeAsset":
    """Yangi `layers_json`ni (tekshirib) saqlaydi VA DARHOL
    `render_composite()` qayta chaqirib `final_storage_path`ni yangilaydi
    (server HAR DOIM "haqiqat manbai" -- brauzer canvas'i faqat oldindan
    ko'rish). OpenAI chaqirilmaydi, kvota sarflanmaydi."""
    if not asset.base_image_storage_path:
        raise CreativeError("Avval rasm generatsiya qilinishi kerak.")
    clean = sanitize_layers(layers)
    asset.set_layers(clean)
    _render_asset(session, asset)
    asset.updated_at = dt.datetime.utcnow()
    session.commit()
    return asset


# ---------------------------------------------------------------------------
# RENDER (Pillow)
# ---------------------------------------------------------------------------

def _hex_to_rgb(color: str, default=(17, 17, 17)) -> "tuple[int, int, int]":
    try:
        c = _normalize_color(color) or ""
        return (int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16))
    except Exception:  # noqa: BLE001
        return default


def _font(kind: str, size: int):
    from PIL import ImageFont
    path = FONT_BOLD if kind == "bold" else FONT_REGULAR
    try:
        return ImageFont.truetype(str(path), max(6, int(size)))
    except OSError:
        logger.error("creative_studio: shrift topilmadi: %s", path)
        return ImageFont.load_default()


def _wrap_lines(text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for para in (text or "").split("\n"):
        words = para.split()
        if not words:
            lines.append("")
            continue
        cur = words[0]
        for w in words[1:]:
            if font.getlength(cur + " " + w) <= max_width:
                cur += " " + w
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    return lines


def _fit_text(text: str, kind: str, size: int, box_w: int, box_h: int):
    """Shrift hajmini `box_w/box_h`ga sig'guncha kichraytiradi (min 40%)."""
    min_size = max(8, int(size * 0.4))
    while True:
        font = _font(kind, size)
        lines = _wrap_lines(text, font, box_w)
        line_h = int(size * 1.2)
        too_wide = any(font.getlength(ln) > box_w for ln in lines)
        if (not too_wide and line_h * len(lines) <= box_h) or size <= min_size:
            return font, lines, line_h
        size = max(min_size, int(size * 0.9))


def _cover_resize(img, target_size):
    from PIL import Image
    tw, th = target_size
    sw, sh = img.size
    scale = max(tw / sw, th / sh)
    nw, nh = max(1, round(sw * scale)), max(1, round(sh * scale))
    resized = img.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - tw) // 2, (nh - th) // 2
    return resized.crop((left, top, left + tw, top + th))


def background_image(background: dict, size: "tuple[int, int]"):
    """Shablonning `background` ta'rifidan (solid/gradient) Pillow RGB
    rasm yasaydi -- `create_from_template()` va shablon preview'lari uchun."""
    from PIL import Image
    w, h = size
    colors = [(_hex_to_rgb(c)) for c in (background or {}).get("colors") or ["#F4F4F2"]]
    if (background or {}).get("type") != "gradient" or len(colors) < 2:
        return Image.new("RGB", (w, h), colors[0])
    c1, c2 = colors[0], colors[-1]
    direction = (background or {}).get("direction") or "vertical"
    # Kichik gradientni yasab, keyin cho'zamiz -- tez va silliq.
    if direction == "horizontal":
        grad = Image.new("RGB", (256, 1))
        grad.putdata([tuple(int(c1[k] + (c2[k] - c1[k]) * i / 255) for k in range(3)) for i in range(256)])
    elif direction == "diagonal":
        grad = Image.new("RGB", (256, 256))
        px = []
        for y in range(256):
            for x in range(256):
                t = (x + y) / 510
                px.append(tuple(int(c1[k] + (c2[k] - c1[k]) * t) for k in range(3)))
        grad.putdata(px)
    else:
        grad = Image.new("RGB", (1, 256))
        grad.putdata([tuple(int(c1[k] + (c2[k] - c1[k]) * i / 255) for k in range(3)) for i in range(256)])
    return grad.resize((w, h), Image.BILINEAR)


def _draw_panel(canvas, layer, W, H):
    from PIL import Image
    x, y = int(layer["x"] * W), int(layer["y"] * H)
    w, h = max(1, int(layer["w"] * W)), max(1, int(layer["h"] * H))
    rgb = _hex_to_rgb(layer.get("bg_color"))
    opacity = _clamp01(layer.get("opacity", 1.0), 1.0)
    overlay = Image.new("RGBA", (w, h), rgb + (0,))
    if layer.get("gradient"):
        alpha = Image.new("L", (1, h))
        alpha.putdata([int(255 * opacity * i / max(1, h - 1)) for i in range(h)])
        overlay.putalpha(alpha.resize((w, h)))
    else:
        overlay.putalpha(int(255 * opacity))
    canvas.alpha_composite(overlay, (x, y))


def _draw_text_layer(canvas, layer, W, H):
    from PIL import ImageDraw
    text = (layer.get("text") or "").strip()
    if not text:
        return
    x, y = int(layer["x"] * W), int(layer["y"] * H)
    w, h = max(1, int(layer["w"] * W)), max(1, int(layer["h"] * H))
    size = max(8, int(float(layer.get("size_ratio") or 0.04) * H))
    font, lines, line_h = _fit_text(text, layer.get("font") or "bold", size, w, h)
    draw = ImageDraw.Draw(canvas)
    color = _hex_to_rgb(layer.get("color"))
    align = layer.get("align") or "left"
    cy = y
    for ln in lines:
        lw = font.getlength(ln)
        if align == "center":
            lx = x + (w - lw) / 2
        elif align == "right":
            lx = x + w - lw
        else:
            lx = x
        draw.text((lx, cy), ln, font=font, fill=color + (255,))
        cy += line_h


def _draw_badge(canvas, layer, W, H):
    from PIL import Image, ImageDraw
    text = (layer.get("text") or "").strip()
    x, y = int(layer["x"] * W), int(layer["y"] * H)
    w, h = max(1, int(layer["w"] * W)), max(1, int(layer["h"] * H))
    rgb = _hex_to_rgb(layer.get("bg_color"))
    opacity = _clamp01(layer.get("opacity", 1.0), 1.0)
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    radius = max(2, int(min(w, h) * 0.22))
    od.rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=rgb + (int(255 * opacity),))
    if text:
        pad_x, pad_y = int(w * 0.08), int(h * 0.12)
        size = max(8, int(float(layer.get("size_ratio") or 0.028) * H))
        font, lines, line_h = _fit_text(text, layer.get("font") or "bold", size, w - 2 * pad_x, h - 2 * pad_y)
        total_h = line_h * len(lines)
        cy = (h - total_h) / 2
        color = _hex_to_rgb(layer.get("color"), (255, 255, 255))
        align = layer.get("align") or "center"
        for ln in lines:
            lw = font.getlength(ln)
            if align == "left":
                lx = pad_x
            elif align == "right":
                lx = w - pad_x - lw
            else:
                lx = (w - lw) / 2
            od.text((lx, cy), ln, font=font, fill=color + (255,))
            cy += line_h
    canvas.alpha_composite(overlay, (x, y))


def _draw_logo(canvas, layer, W, H, logo_path: "Path | None"):
    from PIL import Image
    if not logo_path or not Path(logo_path).exists():
        return
    try:
        with Image.open(logo_path) as raw:
            logo = raw.convert("RGBA")
    except Exception as e:  # noqa: BLE001 -- buzilgan logotip render'ni to'xtatmasin
        logger.warning("creative_studio: logotip ochilmadi (%s): %s", logo_path, e)
        return
    x, y = int(layer["x"] * W), int(layer["y"] * H)
    w, h = max(1, int(layer["w"] * W)), max(1, int(layer["h"] * H))
    scale = min(w / logo.width, h / logo.height)
    nw, nh = max(1, int(logo.width * scale)), max(1, int(logo.height * scale))
    logo = logo.resize((nw, nh), Image.LANCZOS)
    align = layer.get("align") or "right"
    if align == "left":
        lx = x
    elif align == "center":
        lx = x + (w - nw) // 2
    else:
        lx = x + w - nw
    ly = y + (h - nh) // 2
    canvas.alpha_composite(logo, (lx, ly))


def render_composite(base_image_path: "Path", layers: list[dict], brand_kit, out_path: "Path", *, target_size: "tuple[int, int]") -> None:
    """Pillow orqali: base rasmni `target_size`ga 'cover' rejimida (nisbatni
    saqlab, markazdan kesib) moslaydi, so'ng har bir qatlamni chizadi:
      - text  -- DejaVuSans-Bold/Regular, size_ratio * balandlik, avto
                 word-wrap (+ sig'masa kichrayadi), color, align.
      - badge -- yumaloq burchakli to'rtburchak (bg_color, opacity) + matn.
      - panel -- dekorativ to'rtburchak (opacity, gradient overlay).
      - logo  -- brend logotipi "contain" rejimida (shaffoflik saqlanadi);
                 logotip bo'lmasa o'tkazib yuboriladi.
    Natija PNG (RGB) sifatida `out_path`ga yoziladi. `layers`dagi
    `{{placeholder}}`lar ALLAQACHON almashtirilgan bo'lishi kerak
    (`resolve_layers`); qolib ketgan placeholder chizilmaydi."""
    from PIL import Image
    W, H = target_size
    with Image.open(base_image_path) as raw:
        base = raw.convert("RGB")
    canvas = _cover_resize(base, (W, H)).convert("RGBA")
    logo_path = brand_logo_file_path(brand_kit)
    for layer in layers or []:
        if layer.get("hidden"):
            continue
        try:
            ltype = layer.get("type")
            if ltype == "panel":
                _draw_panel(canvas, layer, W, H)
            elif ltype == "text":
                if "{{" in (layer.get("text") or ""):
                    continue
                _draw_text_layer(canvas, layer, W, H)
            elif ltype == "badge":
                if "{{" in (layer.get("text") or ""):
                    continue
                _draw_badge(canvas, layer, W, H)
            elif ltype == "logo":
                _draw_logo(canvas, layer, W, H, logo_path)
        except Exception as e:  # noqa: BLE001 -- bitta buzuq qatlam butun rasmni yiqitmasin
            logger.warning("creative_studio: qatlam chizilmadi (%s): %s", layer.get("id"), e)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(out_path, format="PNG", optimize=True)


# ---------------------------------------------------------------------------
# GENERATSIYA (to'liq oqim)
# ---------------------------------------------------------------------------

def _asset_dir(asset) -> Path:
    return Path(CREATIVE_ROOT) / str(asset.company_id) / str(asset.id)


def _asset_rel(asset, name: str) -> str:
    return str(Path(str(asset.company_id)) / str(asset.id) / name)


def _render_asset(session, asset) -> None:
    """`asset.layers_json` + base rasm -> `final_storage_path` (PNG)."""
    base_path = Path(CREATIVE_ROOT) / asset.base_image_storage_path
    if not base_path.exists():
        raise CreativeError("Fon rasmi diskda topilmadi -- qayta generatsiya qiling.")
    size = _target_pixels_for_aspect(asset.aspect)
    out_rel = _asset_rel(asset, "final.png")
    out_path = Path(CREATIVE_ROOT) / out_rel
    brand_kit = get_brand_kit(session, asset.company_id)
    render_composite(base_path, asset.get_layers(), brand_kit, out_path, target_size=size)
    asset.final_storage_path = out_rel
    asset.width, asset.height = size


def _initial_layers_for(asset, ctx: dict, template: "dict | None") -> list[dict]:
    values = placeholder_values(ctx, asset.get_brief_answers(), template)
    source = template["layers"] if template else default_layers()
    return resolve_layers(source, values)


def _run_generation(session, asset, company, plan_def, *, keep_layers: bool) -> "db.CreativeAsset":
    if asset.status == "generating":
        raise CreativeError("Bu rasm uchun generatsiya allaqachon ketyapti -- biroz kuting.")
    ctx = company_context_module.build_company_context(company, session)
    brand_kit = get_brand_kit(session, asset.company_id)
    ctx["brand_kit"] = {
        "primary_color": getattr(brand_kit, "primary_color", None),
        "secondary_color": getattr(brand_kit, "secondary_color", None),
        "has_logo": bool(brand_logo_file_path(brand_kit)),
    }
    missing = missing_questions(ctx, asset.get_brief_answers())
    if missing:
        asset.missing_fields_json = json.dumps([q["key"] for q in missing], ensure_ascii=False)
        session.commit()
        raise CreativeError(
            "Rasm yaratishdan oldin bir nechta savolga javob bering: "
            + "; ".join(q["question"] for q in missing if q["required"])
        )
    check_quota(company, plan_def, session)
    template = creative_templates.get_template(asset.template_key) if asset.template_key else None

    asset.status = "generating"
    asset.error_message = None
    asset.updated_at = dt.datetime.utcnow()
    session.commit()
    try:
        prompt = build_image_prompt(ctx, asset.get_brief_answers(), template)
        asset.prompt_used = prompt
        data, response_id = _request_openai_image(prompt, _size_for_aspect(asset.aspect))
        # OpenAI qaytargan baytlarni PNG sifatida qayta saqlaymiz (format
        # kafolati -- webp/jpeg kelsa ham base.png doim PNG bo'ladi).
        from PIL import Image
        try:
            with Image.open(io.BytesIO(data)) as img:
                img = img.convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                data = buf.getvalue()
        except Exception as e:  # noqa: BLE001 -- OpenAI rasm emas qaytardi
            logger.error("creative_studio: OpenAI qaytargan ma'lumot rasm emas: %s", e)
            raise CreativeError(_OPENAI_GENERIC_MSG) from e
        d = _asset_dir(asset)
        d.mkdir(parents=True, exist_ok=True)
        (d / "base.png").write_bytes(data)
        asset.base_image_storage_path = _asset_rel(asset, "base.png")
        asset.openai_response_id = (response_id or "")[:128] or None
        if not (keep_layers and asset.get_layers()):
            asset.set_layers(_initial_layers_for(asset, ctx, template))
        if not asset.title:
            asset.title = placeholder_values(ctx, asset.get_brief_answers(), template).get("headline") or None
        _render_asset(session, asset)
        asset.status = "ready"
        asset.missing_fields_json = "[]"
        asset.updated_at = dt.datetime.utcnow()
        session.commit()
        # Kvota FAQAT rasm haqiqatan olingandan KEYIN sarflanadi.
        increment_usage(session, asset.company_id)
        return asset
    except CreativeError as e:
        asset.status = "failed"
        asset.error_message = str(e)
        asset.updated_at = dt.datetime.utcnow()
        session.commit()
        raise
    except Exception as e:  # noqa: BLE001 -- kutilmagan xato: friendly matn, xom xato faqat logda
        logger.exception("creative_studio: generatsiya kutilmagan xato (asset=%s)", asset.id)
        asset.status = "failed"
        asset.error_message = _OPENAI_GENERIC_MSG
        asset.updated_at = dt.datetime.utcnow()
        session.commit()
        raise CreativeError(_OPENAI_GENERIC_MSG) from e


def generate_base_image(session, asset: "db.CreativeAsset", company, plan_def) -> "db.CreativeAsset":
    """TO'LIQ oqim: 1) brif to'liq emas (`missing_questions()` bo'sh emas)
    -> `CreativeError` (OpenAI chaqirilmaydi). 2) `check_quota()` (limit
    tugagan -> `QuotaExceededError`). 3) status='generating' + commit.
    4) `build_image_prompt()`. 5) OpenAI Images API (retry/xato-aniqlash
    `call_analysis` naqshi bilan; xom xato foydalanuvchiga ko'rsatilmaydi).
    6) `CREATIVE_ROOT/<company_id>/<asset_id>/base.png`. 7) Shablon
    (yoki standart) qatlamlar, placeholder'lar brifdan real matn bilan.
    8) `render_composite()` -> final.png. 9) status='ready', width/height,
    `increment_usage()`, commit. Xato bo'lsa: status='failed',
    `error_message` (o'zbekcha), commit, xato QAYTA ko'tariladi."""
    return _run_generation(session, asset, company, plan_def, keep_layers=False)


def regenerate_base_image(session, asset, company, plan_def) -> "db.CreativeAsset":
    """Xuddi `generate_base_image`, lekin MAVJUD asset uchun -- yangi fon
    rasm oladi (kvota YANA sarflanadi -- UI buni aniq ko'rsatishi kerak),
    foydalanuvchi tahrirlagan qatlamlar (matn/pozitsiya) SAQLANIB qoladi."""
    if asset.kind == "template":
        # Shablondan yaratilgan (OpenAI'siz) rasm ham AI fon olishi mumkin.
        asset.kind = "ai_generated"
    return _run_generation(session, asset, company, plan_def, keep_layers=True)


# ---------------------------------------------------------------------------
# SHABLONDAN BOSHLASH (OpenAI'siz, tezkor)
# ---------------------------------------------------------------------------

def create_from_template(session, company, manager_id, template_key: str, *, product_image_bytes: "bytes | None" = None, aspect: "str | None" = None) -> "db.CreativeAsset":
    """OpenAI CHAQIRMAYDI (kvota sarflamaydi): shablonning `background`idan
    (yoki `product_image_bytes` berilsa o'sha rasmdan, 'cover' rejimida)
    bazaviy rasm yasaydi, shablon qatlamlarini (placeholder'lar kompaniya
    profilidan to'ldirilib) `layers_json`ga yozadi, status='ready' qilib
    darhol `render_composite()` chaqiradi. Foydalanuvchi o'z mahsulot
    fotosini shablon uslubiga moslamoqchi bo'lganda (savol-javobsiz)."""
    template = creative_templates.get_template(template_key)
    if template is None:
        raise CreativeError("Bunday shablon topilmadi.")
    aspect = (aspect or "").strip() or template.get("aspect_default") or "1:1"
    if aspect not in ASPECTS:
        raise CreativeError("Rasm nisbati faqat 1:1, 4:5 yoki 9:16 bo'lishi mumkin.")
    from PIL import Image
    size = _target_pixels_for_aspect(aspect)
    if product_image_bytes:
        if len(product_image_bytes) > MAX_PRODUCT_IMAGE_BYTES:
            raise CreativeError(f"Rasm juda katta -- chegara {MAX_PRODUCT_IMAGE_BYTES // (1024 * 1024)} MB.")
        try:
            with Image.open(io.BytesIO(product_image_bytes)) as raw:
                base = _cover_resize(raw.convert("RGB"), size)
        except Exception as e:  # noqa: BLE001
            raise CreativeError("Mahsulot rasmini o'qib bo'lmadi -- fayl buzilgan yoki rasm emas.") from e
    else:
        base = background_image(template.get("background") or {}, size)

    asset = db.CreativeAsset(
        company_id=company.id, created_by_manager_id=manager_id, kind="template",
        status="generating", template_key=template["key"], aspect=aspect,
    )
    asset.set_brief_answers({})
    session.add(asset)
    session.commit()  # id kerak (papka nomi uchun)
    try:
        d = _asset_dir(asset)
        d.mkdir(parents=True, exist_ok=True)
        base.save(d / "base.png", format="PNG")
        asset.base_image_storage_path = _asset_rel(asset, "base.png")
        ctx = company_context_module.build_company_context(company, session)
        asset.set_layers(_initial_layers_for(asset, ctx, template))
        asset.title = template["name"]
        _render_asset(session, asset)
        asset.status = "ready"
        asset.missing_fields_json = "[]"
        session.commit()
        return asset
    except CreativeError as e:
        asset.status = "failed"
        asset.error_message = str(e)
        session.commit()
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("creative_studio: shablondan yaratishda xato (asset=%s)", asset.id)
        asset.status = "failed"
        asset.error_message = "Shablondan rasm yaratib bo'lmadi. Qayta urinib ko'ring."
        session.commit()
        raise CreativeError(asset.error_message) from e


# ---------------------------------------------------------------------------
# EKSPORT + AUTOPILOT
# ---------------------------------------------------------------------------

def export_png_path(asset) -> "Path":
    """`final_storage_path`ning to'liq yo'li (fayl bo'lmasa `CreativeError`)."""
    if not asset or not asset.final_storage_path:
        raise CreativeError("Rasm hali tayyor emas.")
    path = Path(CREATIVE_ROOT) / asset.final_storage_path
    if not path.exists():
        raise CreativeError("Yakuniy rasm diskda topilmadi -- qatlamlarni qayta saqlang yoki qayta generatsiya qiling.")
    return path


def export_pdf(asset, out_path: "Path") -> None:
    """reportlab orqali yakuniy PNG'ni rasm nisbatiga mos BITTA PDF
    sahifasiga (A4 kengligida, balandligi nisbatga qarab) joylashtiradi
    va `out_path`ga saqlaydi."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas as pdf_canvas
    png_path = export_png_path(asset)
    reader = ImageReader(str(png_path))
    iw, ih = reader.getSize()
    page_w = A4[0]
    page_h = page_w * ih / max(1, iw)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    c = pdf_canvas.Canvas(str(out_path), pagesize=(page_w, page_h))
    c.setTitle(asset.title or "Replix kreativ")
    c.drawImage(reader, 0, 0, width=page_w, height=page_h)
    c.showPage()
    c.save()


def asset_image_bytes(asset) -> bytes:
    """`final_storage_path`dagi PNG'ning xom baytlari --
    `campaign_media.save_uploaded_media(session, company_id, draft_id,
    asset_image_bytes(asset), f"creative_{asset.id}.png", "image/png")`
    orqali Autopilot mediasi sifatida ishlatish uchun (web-agent shu
    bog'lashni quradi; `CampaignDraftMedia.creative_asset_id`ga `asset.id`
    yozib qo'yish tavsiya etiladi)."""
    return export_png_path(asset).read_bytes()


def list_assets(session, company_id: int, *, limit: int = 50) -> list:
    """Kompaniyaning rasmlari (yangi -> eski), galereya uchun."""
    with db.scoped_as(company_id):
        return (
            session.query(db.CreativeAsset)
            .filter(db.CreativeAsset.company_id == company_id)
            .order_by(db.CreativeAsset.id.desc())
            .limit(limit)
            .all()
        )


def delete_asset(session, asset) -> None:
    """Qatorni va diskdagi papkasini o'chiradi (commit qiladi)."""
    import shutil
    d = _asset_dir(asset)
    session.delete(asset)
    session.commit()
    try:
        if d.exists():
            shutil.rmtree(d)
    except OSError as e:
        logger.warning("creative_studio: papka o'chirilmadi (%s): %s", d, e)
