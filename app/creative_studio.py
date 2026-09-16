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
import storage_backend
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
# 2026-09, uslub namunasiga asoslangan generatsiya (`_request_openai_image_edit`)
# -- faqat `gpt-image-*` modellar bu endpoint'ni shu multipart shaklda
# qo'llab-quvvatlaydi (dall-e-* UCHUN ishlatilmaydi -- chaqiruvchi tomonda tekshiriladi).
OPENAI_IMAGE_EDITS_URL = "https://api.openai.com/v1/images/edits"
_OPENAI_IMAGE_TIMEOUT = 180

ALLOWED_LOGO_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
_LOGO_EXT_TO_TYPE = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
MAX_LOGO_BYTES = 8 * 1024 * 1024
MAX_PRODUCT_IMAGE_BYTES = 30 * 1024 * 1024
# 2026-09, uslub namunasi (reference) rasmi -- haqiqiy foto, logotipdan
# kattaroq bo'lishi normal, shuning uchun chegara ham kattaroq.
MAX_STYLE_REFERENCE_BYTES = 15 * 1024 * 1024

# ---------------------------------------------------------------------------
# USLUB TAKSONOMIYASI (2026-09, foydalanuvchi fikri: "AI o'zi taxmin
# qilmasin, birinchi marta kirganda qaysi uslubni yoqtirishimizni
# so'rasin"). Kichik, QOTIRILGAN lug'at -- 20 ta tayyor shablonning HAR
# BIRIGA (`creative_templates.CREATIVE_TEMPLATES[i]["styles"]`) VA
# kompaniyaning saqlangan tanloviga (`CompanyBrandKit.preferred_styles`)
# BIR XIL tag'lar ishlatiladi, shuning uchun ular shu yerda -- BITTA
# joyda -- aniqlanadi (creative_templates.py bu modulni import
# QILMAYDI, aylanma import bo'lmasin, lekin string tag'lar bir xil).
# `category` (shablonning SOHA yorlig'i -- sale/luxury/tech/food) bilan
# ARALASHTIRILMASIN: bu yerdagi tag'lar VIZUAL til (rang/kompozitsiya
# kayfiyati), bittasi biznes, ikkinchisi dizayn.
# ---------------------------------------------------------------------------
STYLE_TAGS = ["minimalism", "maximalism", "luxury", "playful", "bold", "corporate", "elegant", "warm"]

STYLE_LABELS = {
    "minimalism": "Minimalizm", "maximalism": "Maksimalizm", "luxury": "Hashamatli",
    "playful": "O'ynoqi", "bold": "Jasur", "corporate": "Korporativ",
    "elegant": "Nafis", "warm": "Iliq",
}

# UI'da chip/swatch rangi (Onboarding oynasida, real namuna surat o'rniga
# -- vaqt byudjeti tejash uchun rangli belgi yetarli).
STYLE_ACCENTS = {
    "minimalism": "#94A3B8", "maximalism": "#DB2777", "luxury": "#C9A227",
    "playful": "#FF6B6B", "bold": "#DC2626", "corporate": "#1E3A5F",
    "elegant": "#8B5E34", "warm": "#E76F51",
}

# `build_image_prompt()`ga QO'SHIMCHA (additive) ko'rsatma sifatida
# qo'shiladigan inglizcha uslub-tavsif iborasi (2026-09). Tanlangan
# shablonning o'z `style_prompt`i har doim BIRINCHI/asosiy bo'lib qoladi --
# bu faqat o'sha ustiga qo'shiladigan nozik ishora, ALMASHTIRMAYDI.
STYLE_DESCRIPTORS = {
    "minimalism": "minimalist, clean, generous negative space, understated elegance",
    "maximalism": "rich, dense, vibrant, maximalist layered composition",
    "luxury": "luxurious, premium, upscale mood, refined materials and finishes",
    "playful": "playful, cheerful, fun, energetic and approachable mood",
    "bold": "bold, high-contrast, vivid, attention-grabbing visual energy",
    "corporate": "professional, corporate, polished, trustworthy business aesthetic",
    "elegant": "elegant, refined, graceful, softly sophisticated",
    "warm": "warm, cozy, inviting tones, soft natural warmth",
}


def style_tag_options() -> list[dict]:
    """UI (onboarding oynasi/sozlamalar) uchun: [{"key","label","accent"}]
    `STYLE_TAGS` tartibida."""
    return [{"key": k, "label": STYLE_LABELS[k], "accent": STYLE_ACCENTS[k]} for k in STYLE_TAGS]

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
    # Eski logotip (boshqa kengaytmali) VA eski tozalangan nusxa/marker
    # qolib ketmasin (R2'dagi eski nusxa ham -- eng yaxshi urinish;
    # `logo_clean.*` HECH QACHON R2'ga yuklanmagan, faqat lokal keshi o'chiriladi).
    for old in list(abs_dir.glob("logo.*")):
        try:
            storage_backend.delete_object(f"brand_kit/{rel_dir / old.name}")
            old.unlink()
        except OSError:
            pass
    for old in list(abs_dir.glob("logo_clean.*")):
        # `logo_clean.*` R2'ga HECH QACHON yuklanmagan (faqat lokal kesh).
        try:
            old.unlink()
        except OSError:
            pass
    (abs_dir / f"logo{ext}").write_bytes(data)
    # 2026-09, R2 doimiy saqlash: ASL logotip (foydalanuvchi yuklagan)
    # R2'ga ham yuklanadi -- `logo_clean.png` (avto-kesilgan derivativ kesh)
    # EMAS, u asl fayldan arzon qayta hisoblanadi, shuning uchun yuklanmaydi.
    storage_backend.upload_file(abs_dir / f"logo{ext}", f"brand_kit/{rel_dir / f'logo{ext}'}")
    # 2026-09, foydalanuvchi shikoyati: oq/qora fonli (shaffof bo'lmagan)
    # logotip reklamada "oq quti" bo'lib ko'rinardi -- fon YUKLASH paytida
    # BIR MARTA avtomatik kesiladi (`logo_clean.png`), render shu faylni oladi.
    _ensure_clean_logo(abs_dir / f"logo{ext}")

    kit = _get_or_create_brand_kit(session, company_id)
    kit.logo_storage_path = str(rel_dir / f"logo{ext}")
    kit.logo_content_type = ct
    kit.updated_at = dt.datetime.utcnow()
    session.commit()
    return kit


# ---------------------------------------------------------------------------
# LOGOTIP FONINI AVTOMATIK KESISH (2026-09, foydalanuvchi shikoyati:
# "logotipning oq/qora fonini o'zi kesib, to'g'ri joylashtirsin").
#
# NEGA ML (rembg/onnxruntime) EMAS: deploy `render.yaml` -> `plan: starter`
# (512 MB RAM, bitta gunicorn worker + APScheduler bir jarayonda). rembg
# onnxruntime + ~170 MB U2Net og'irliklarini talab qiladi va inferensiya
# paytida 300+ MB xotira oladi -- production'ni yiqitish xavfi katta.
# Logotip -- deyarli har doim BIR XIL rangli (oq/qora/och) fon ustidagi
# belgi, shuning uchun oddiy evristika yetarli va xavfsiz:
#   1. Rasmda allaqachon haqiqiy shaffoflik bo'lsa -- tegilmaydi.
#   2. Chekka (border) piksellarining mediana rangi topiladi; chekka
#      bir xil rangda bo'lmasa (foto/gradient) -- tegilmaydi (taxmin
#      qilinmaydi -- xato kesishdan ko'ra asl holda qoldirish yaxshi).
#   3. Har bir piksel uchun fon rangigacha Chebyshev masofa hisoblanadi
#      (`ImageChops`, sof Pillow, numpy'siz): <= T0 -> to'liq shaffof,
#      >= T1 -> to'liq ko'rinadi, oralig'i -- yumshoq (anti-aliasing,
#      qirralar tishli chiqmaydi). T0/T1 KONSERVATIV: JPEG artefaktlari
#      (+-10..15) kesiladi, lekin och-kulrang detallar saqlanib qoladi.
#   MA'LUM CHEKLOV: logotip fon bilan deyarli bir xil rangli katta
#   yuzalarga ega bo'lsa (masalan oq fondagi oq-krem belgi), o'sha
#   qismlar ham shaffof bo'ladi -- bunday holatda foydalanuvchi shaffof
#   PNG yuklashi kerak (forma shuni tavsiya qiladi).
# Natija `logo_clean.png` (RGBA) -- `brand_logo_file_path()` shu faylni
# qaytaradi; u yo'q bo'lsa (eski yuklangan logotiplar) BIRINCHI o'qishda
# o'zi yaratiladi ("self-heal", qayta yuklash shart emas). Kesish kerak
# bo'lmasa `logo_clean.skip` markeri yoziladi (har renderda qayta tahlil
# qilinmasin).
# ---------------------------------------------------------------------------
LOGO_CLEAN_NAME = "logo_clean.png"
LOGO_CLEAN_SKIP = "logo_clean.skip"
LOGO_INNER_PAD = 0.06       # logotip qutisi ichidagi hoshiya (qisqa tomonga nisbatan)
_LOGO_BG_T0 = 18            # shu masofagacha -- to'liq shaffof
_LOGO_BG_T1 = 56            # shundan uzoq -- to'liq ko'rinadi
_LOGO_BORDER_TOL = 40       # chekka bir xillik tekshiruvi (Chebyshev)
_LOGO_BORDER_MIN_FRAC = 0.85  # chekka piksellarining kamida shuncha qismi fon rangida bo'lsin


def _has_real_transparency(img) -> bool:
    """RGBA rasmda MA'NOLI shaffoflik bormi (kamida ~1% piksel alpha<250)."""
    if img.mode not in ("RGBA", "LA"):
        return False
    alpha = img.getchannel("A")
    lo, _hi = alpha.getextrema()
    if lo >= 250:
        return False
    hist = alpha.histogram()
    total = max(1, img.width * img.height)
    return sum(hist[:250]) / total >= 0.01


def _border_background_color(rgb) -> "tuple[int, int, int] | None":
    """Chekka (4 tomon) piksellarining mediana rangi -- agar chekka
    yetarlicha BIR XIL bo'lsa, aks holda None (fon aniqlanmadi)."""
    w, h = rgb.size
    strip = max(1, int(min(w, h) * 0.03))
    boxes = [(0, 0, w, strip), (0, h - strip, w, h), (0, 0, strip, h), (w - strip, 0, w, h)]
    pixels: list = []
    for box in boxes:
        pixels.extend(rgb.crop(box).getdata())
    if not pixels:
        return None
    chans = list(zip(*pixels))
    median = tuple(sorted(ch)[len(ch) // 2] for ch in chans)
    near = sum(1 for p in pixels if max(abs(p[i] - median[i]) for i in range(3)) <= _LOGO_BORDER_TOL)
    if near / len(pixels) < _LOGO_BORDER_MIN_FRAC:
        return None
    return median


def remove_logo_background(img) -> "tuple[object, bool]":
    """Pillow rasm -> (RGBA rasm, o'zgardimi). Yuqoridagi evristika;
    shaffoflik allaqachon bo'lsa yoki fon aniqlanmasa -- (asl RGBA, False)."""
    from PIL import Image, ImageChops
    rgba = img.convert("RGBA")
    if rgba.width < 8 or rgba.height < 8 or _has_real_transparency(rgba):
        return rgba, False
    rgb = rgba.convert("RGB")
    bg = _border_background_color(rgb)
    if bg is None:
        return rgba, False
    r, g, b = rgb.split()
    dist = None
    for ch, val in ((r, bg[0]), (g, bg[1]), (b, bg[2])):
        d = ImageChops.difference(ch, Image.new("L", rgb.size, val))
        dist = d if dist is None else ImageChops.lighter(dist, d)
    t0, t1 = _LOGO_BG_T0, _LOGO_BG_T1
    alpha = dist.point(lambda v: 0 if v <= t0 else (255 if v >= t1 else int(255 * (v - t0) / (t1 - t0))))
    # Asl alpha (masalan 255 dan bir oz kam bo'lgan) bilan birlashtirish.
    alpha = ImageChops.darker(alpha, rgba.getchannel("A"))
    out = rgba.copy()
    out.putalpha(alpha)
    if out.getchannel("A").getbbox() is None:
        # Hammasi kesilib ketdi (bir rangli rasm) -- xavfsiz tomon: asl holat.
        return rgba, False
    return out, True


def _ensure_clean_logo(original_path: "Path") -> "Path | None":
    """`logo_clean.png` (fon kesilgan) yo'lini qaytaradi: bor bo'lsa --
    darhol; yo'q bo'lsa asl fayldan BIR MARTA yaratadi (atomik yozish).
    Kesish kerak bo'lmasa `logo_clean.skip` marker yoziladi va None
    qaytadi (asl fayl ishlatiladi). Har qanday xato -> None (render asl
    faylni oladi, hech narsa yiqilmaydi)."""
    original_path = Path(original_path)
    d = original_path.parent
    clean, skip = d / LOGO_CLEAN_NAME, d / LOGO_CLEAN_SKIP
    if clean.exists():
        return clean
    if skip.exists() or not original_path.exists():
        return None
    try:
        from PIL import Image
        with Image.open(original_path) as raw:
            raw.load()
            out, changed = remove_logo_background(raw)
        if not changed:
            skip.write_bytes(b"")
            return None
        tmp = d / f".{LOGO_CLEAN_NAME}.{os.getpid()}.tmp"
        out.save(tmp, format="PNG", optimize=True)
        os.replace(tmp, clean)
        return clean
    except Exception as e:  # noqa: BLE001 -- logotip tozalash render'ni to'xtatmasin
        logger.warning("creative_studio: logotip fonini kesib bo'lmadi (%s): %s", original_path, e)
        return None


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


# ---------------------------------------------------------------------------
# BIRINCHI MARTA USLUB TANLASH -- ONBOARDING (2026-09). Uchala yo'l ham
# TENG darajada ixtiyoriy -- birortasi ham majburiy emas, "O'tkazib
# yuborish" har doim ochiq (`skip_style_onboarding`). Har uchalasi ham
# `style_onboarded_at`ni o'rnatadi -- web-agent shu maydon NULL bo'lsagina
# onboarding oynasini avtomatik ko'rsatadi (bir marta ko'rsatilgach qayta
# avtomatik chiqmaydi, lekin Sozlamalar'dan istalgan payt qayta chaqirish
# mumkin -- `app.py`dagi `/kreativ?open_style=1`).
# ---------------------------------------------------------------------------

def save_style_preference(session, company_id: int, styles: list) -> "db.CompanyBrandKit":
    """Foydalanuvchi tanlagan uslub yorliqlarini saqlaydi: noma'lum tag'lar
    (STYLE_TAGS'da yo'q) xato bermasdan e'tiborsiz qoldiriladi, ko'pi bilan
    3 tasi (takrorlanmagan holda) saqlanadi, `style_onboarded_at`
    o'rnatiladi."""
    clean: list[str] = []
    for raw in (styles or []):
        tag = str(raw or "").strip().lower()
        if tag in STYLE_TAGS and tag not in clean:
            clean.append(tag)
        if len(clean) >= 3:
            break
    kit = _get_or_create_brand_kit(session, company_id)
    kit.set_preferred_styles(clean)
    kit.style_onboarded_at = dt.datetime.utcnow()
    kit.updated_at = dt.datetime.utcnow()
    session.commit()
    return kit


def skip_style_onboarding(session, company_id: int) -> "db.CompanyBrandKit":
    """Foydalanuvchi uslub tanlashni ATAYLAB o'tkazib yubordi -- hech qanday
    tanlov/namuna MAJBURLANMAYDI, faqat `style_onboarded_at` o'rnatiladi
    (oyna qayta avtomatik chiqmasin)."""
    kit = _get_or_create_brand_kit(session, company_id)
    kit.style_onboarded_at = dt.datetime.utcnow()
    kit.updated_at = dt.datetime.utcnow()
    session.commit()
    return kit


def brand_logo_file_path(brand_kit) -> "Path | None":
    """Render/ko'rsatish uchun logotipning diskdagi yo'li (brend kit/
    logotip bo'lmasa `None`). Fon kesilgan `logo_clean.png` bo'lsa (yoki
    shu chaqiruvda bir marta yaratilsa) -- o'sha; aks holda asl fayl.
    Asl fayl o'chirilgan bo'lsa ham yo'l qaytariladi (chaqiruvchi
    `.exists()` tekshiradi -- avvalgi xatti-harakat saqlangan)."""
    if brand_kit is None or not getattr(brand_kit, "logo_storage_path", None):
        return None
    original = storage_backend.ensure_local(BRAND_ROOT, brand_kit.logo_storage_path, key_prefix="brand_kit")
    if original.exists():
        clean = _ensure_clean_logo(original)
        if clean is not None:
            return clean
    return original


def brand_logo_original_path(brand_kit) -> "Path | None":
    """Foydalanuvchi yuklagan ASL logotip fayli (kesilmagan)."""
    if brand_kit is None or not getattr(brand_kit, "logo_storage_path", None):
        return None
    return storage_backend.ensure_local(BRAND_ROOT, brand_kit.logo_storage_path, key_prefix="brand_kit")


# ---------------------------------------------------------------------------
# USLUB NAMUNASI (reference) RASMI (2026-09, foydalanuvchi so'zlari bilan:
# "yaqin turishi kerak, yonma-yon" -- uslubga YAQINLASHTIRISH, ANIQ nusxa
# EMAS). `save_brand_logo()` bilan BIR XIL naqsh (joylashuv, hajm/tur
# tekshiruvi, R2'ga yuklash), lekin `_ensure_clean_logo()` (fon kesish)
# QO'LLANMAYDI -- bu haqiqiy fotosurat, belgi/ikonka emas.
# ---------------------------------------------------------------------------

def save_style_reference_image(session, company_id: int, file_storage_or_bytes, filename: str, content_type: "str | None") -> "db.CompanyBrandKit":
    """Uslub namunasi rasmini tekshiradi (PNG/JPG/WEBP, max
    `MAX_STYLE_REFERENCE_BYTES`), `BRAND_ROOT/<company_id>/style_ref.<ext>`ga
    saqlaydi (R2'ga ham), `CompanyBrandKit.style_reference_*`ni yangilaydi
    va `style_onboarded_at`ni o'rnatadi (onboarding oynasi qayta
    ko'rsatilmasin). Eski namuna (boshqa kengaytmali bo'lsa ham) o'chiriladi."""
    ct = _detect_logo_type(filename, content_type)
    if ct not in ALLOWED_LOGO_TYPES:
        raise CreativeError("Uslub namunasi rasmi faqat PNG, JPG yoki WEBP formatida bo'lishi mumkin.")
    data = _read_bytes(file_storage_or_bytes)
    if not data:
        raise CreativeError("Fayl bo'sh.")
    if len(data) > MAX_STYLE_REFERENCE_BYTES:
        raise CreativeError(f"Rasm juda katta -- chegara {MAX_STYLE_REFERENCE_BYTES // (1024 * 1024)} MB.")
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
    except Exception as e:  # noqa: BLE001 -- buzilgan/soxta rasm
        raise CreativeError("Rasmni o'qib bo'lmadi -- fayl buzilgan yoki rasm emas.") from e

    ext = ALLOWED_LOGO_TYPES[ct]
    rel_dir = Path(str(company_id))
    abs_dir = Path(BRAND_ROOT) / rel_dir
    abs_dir.mkdir(parents=True, exist_ok=True)
    for old in list(abs_dir.glob("style_ref.*")):
        try:
            storage_backend.delete_object(f"brand_kit/{rel_dir / old.name}")
            old.unlink()
        except OSError:
            pass
    (abs_dir / f"style_ref{ext}").write_bytes(data)
    storage_backend.upload_file(abs_dir / f"style_ref{ext}", f"brand_kit/{rel_dir / f'style_ref{ext}'}")

    kit = _get_or_create_brand_kit(session, company_id)
    kit.style_reference_storage_path = str(rel_dir / f"style_ref{ext}")
    kit.style_reference_content_type = ct
    kit.style_onboarded_at = dt.datetime.utcnow()
    kit.updated_at = dt.datetime.utcnow()
    session.commit()
    return kit


def brand_style_reference_path(brand_kit) -> "Path | None":
    """Uslub namunasi rasmining diskdagi yo'li (bo'lmasa `None`) --
    `brand_logo_original_path()` uslubida, R2'dan self-heal bilan."""
    if brand_kit is None or not getattr(brand_kit, "style_reference_storage_path", None):
        return None
    return storage_backend.ensure_local(BRAND_ROOT, brand_kit.style_reference_storage_path, key_prefix="brand_kit")


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
    # 2026-09: telefon -- kompaniya profilida (`ctx['phone']`) bo'lmasa
    # MAJBURIY (O'zbekiston bozorida reklama rasmi odatda raqam bilan
    # chiqadi; AI matn yozuvchisi ham shuni ishlatadi). Profilda bo'lsa
    # so'ralmaydi (`visible_brief_questions`). Majburiy savollar oldinda turadi.
    ("phone", "Mijozlar bog'lanadigan telefon raqamingiz? (reklama rasmida ko'rsatiladi)",
     "Masalan: +998 90 123 45 67", True),
    ("offer_text", "Aksiya/chegirma yoki maxsus taklif bormi? (bo'lsa matnini yozing, bo'lmasa 'yo'q' deb qoldiring)",
     "Masalan: -30% chegirma, bepul yetkazib berish", True),
    ("cta_preference", "Qanday chaqiriq matni bo'lsin? (bo'sh qoldirsangiz AI biznesingizga mos tanlaydi)",
     "Masalan: Hoziroq buyurtma bering, Narxini bilib oling", True),
    ("style_notes", "Rasm qanday ko'rinishda bo'lsin? (muhit, rang, kayfiyat -- ixtiyoriy)",
     "Masalan: oq studiya foni, tabiiy yorug'lik, premium ko'rinish", True),
]
_BRIEF_BY_KEY = {k: (k, q, ph, opt) for k, q, ph, opt in CREATIVE_BRIEF_QUESTIONS}
_NEGATIVE_ANSWERS = {"yo'q", "yoq", "yo`q", "yoʻq", "нет", "no", "-", "yok"}
_PHONE_DIGITS_RE = re.compile(r"\d")


def _answered(value) -> bool:
    return bool(value is not None and str(value).strip())


def _is_negative(value) -> bool:
    return str(value or "").strip().lower() in _NEGATIVE_ANSWERS


def _question_dict(key: str, *, required: bool) -> dict:
    k, q, ph, opt = _BRIEF_BY_KEY[key]
    return {"key": k, "question": q, "placeholder": ph, "optional": (opt and not required), "required": required}


def normalize_phone(value: "str | None") -> str:
    """Telefonni yengil tozalaydi: faqat raqam, '+', bo'sh joy, qavs va
    tire qoladi; kamida 7 ta raqam bo'lmasa `CreativeError`. (Formatni
    qat'iy majburlamaymiz -- foydalanuvchi qanday yozsa, shunday chiqadi.)"""
    v = re.sub(r"[^\d+()\-\s]", "", str(value or "")).strip()
    v = re.sub(r"\s+", " ", v)
    if len(_PHONE_DIGITS_RE.findall(v)) < 7:
        raise CreativeError("Telefon raqami noto'g'ri ko'rinadi -- masalan +998 90 123 45 67 shaklida yozing.")
    return v[:32]


def phone_known(ctx: dict) -> bool:
    return bool((ctx or {}).get("phone"))


def visible_brief_questions(ctx: dict) -> list[tuple]:
    """Shu kompaniya uchun UMUMAN ma'noli brif savollari (kalit, savol,
    placeholder, ixtiyoriymi): telefon profilda bo'lsa so'ralmaydi."""
    return [q for q in CREATIVE_BRIEF_QUESTIONS if not (q[0] == "phone" and phone_known(ctx))]


def missing_questions(ctx: dict, existing_answers: dict) -> list[dict]:
    """`company_context.build_company_context()` natijasi (ctx) VA
    hozirgacha yig'ilgan `existing_answers`dan kelib chiqib HALI kerak
    bo'lgan savollarni qaytaradi.

    MAJBURIY (`required=True`) savollar:
      - 'focus'  -- `ctx['missing_fields']`da 'product_or_service' bo'lsa
                    (profilda mahsulot yo'q) VA `existing_answers['focus']`
                    bo'sh bo'lsa;
      - 'phone'  -- `ctx['phone']` bo'sh bo'lsa VA javobda ham bo'lmasa
                    (2026-09: "to'liq ma'lumot olgandan keyin generatsiya").
    Kamida bitta majburiy savol bo'lsa, hali javob berilmagan (kaliti
    `existing_answers`da umuman yo'q) ixtiyoriy savollar ham BIR MARTA
    birga qaytariladi (foydalanuvchi bitta formada hammasini ko'rsin --
    generatsiya faqat hammasi yig'ilgach). Bo'sh ro'yxat = hammasi
    yetarli, generatsiyaga tayyor."""
    ctx = ctx or {}
    existing_answers = existing_answers or {}
    out: list[dict] = []
    product_missing = "product_or_service" in (ctx.get("missing_fields") or []) or not (ctx.get("profile_answers") or {}).get("product_or_service")
    if product_missing and not _answered(existing_answers.get("focus")):
        out.append(_question_dict("focus", required=True))
    if not phone_known(ctx) and not _answered(existing_answers.get("phone")):
        out.append(_question_dict("phone", required=True))
    if not out:
        return []
    required_keys = {q["key"] for q in out}
    for key, *_ in visible_brief_questions(ctx):
        if key in required_keys or key in existing_answers:
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
    value = (value or "").strip()[:500]
    if key == "phone" and value:
        value = normalize_phone(value)
        # Kompaniya profilida telefon bo'lmasa -- shu raqam profilga ham
        # yoziladi (keyingi kreativlarda va AI rejalarda qayta so'ralmaydi).
        # Mavjud raqam HECH QACHON ustidan yozilmaydi.
        company = _load_company(session, asset)
        if company is not None and not (getattr(company, "phone", None) or "").strip():
            company.phone = value
            logger.info("creative_studio: kompaniya %s telefoni brifdan to'ldirildi", company.id)
    answers[key] = value
    asset.set_brief_answers(answers)
    _refresh_missing(session, asset)
    asset.updated_at = dt.datetime.utcnow()
    session.commit()
    return asset


# ---------------------------------------------------------------------------
# AI BRIF SUHBATI (2026-09, foydalanuvchi fikri: "savollar bir xil shablon
# bo'lmasin, agent ishlasin") -- ESKI statik `CREATIVE_BRIEF_QUESTIONS`/
# `submit_brief_answer()`/`missing_questions()` YUQORIDA ATAYLAB
# TEGILMAGAN (hali ham to'g'ri ishlaydi, boshqa kod ular bilan bog'liq --
# pastga qarang), lekin YANGI asset'lar uchun ASOSIY oqim ENDI shu:
# `start_brief()` -> [`answer_brief()` bir nechta marta] -> "done" bo'lsa
# `brief_answers_json` ESKI tekis lug'at shaklida to'ladi (yuqoridagi
# `submit_brief_answer` bilan BIR XIL natija) -- `_run_generation()`,
# `fallback_placeholder_values()`, AI kopirayter O'ZGARISHSIZ ishlaydi.
# Haqiqiy savol-tanlash mantig'i -- `creative_brief_agent.py` (LLM).
# ---------------------------------------------------------------------------

def _apply_brief_step(session, asset: "db.CreativeAsset", conversation: list, step: dict) -> None:
    """`creative_brief_agent.next_step()` natijasini suhbatga qo'shadi va
    commit qiladi: savol bo'lsa -- 'agent' burilishi; 'done' bo'lsa --
    `brief_answers_json` (ESKI tekis shakl) to'ldiriladi, telefon (bo'lsa)
    profilga backfill qilinadi (`submit_brief_answer` bilan bir xil
    xatti-harakat), `missing_fields_json` QAYTA hisoblanadi (LLM xatosiga
    qaramay, generatsiya qattiq talablarsiz hech qachon ishga tushmaydi --
    `_refresh_missing` ESKI, sof deterministik tekshiruvni ishlatadi)."""
    conversation = list(conversation or [])
    if step.get("done"):
        brief = step.get("brief") if isinstance(step.get("brief"), dict) else {}
        answers = {k: str(brief.get(k) or "").strip() for k in ("focus", "offer_text", "cta_preference", "style_notes", "phone")}
        phone = answers.get("phone")
        if phone:
            company = _load_company(session, asset)
            if company is not None and not (getattr(company, "phone", None) or "").strip():
                company.phone = phone
                logger.info("creative_studio: kompaniya %s telefoni AI brif suhbatidan to'ldirildi", company.id)
        asset.set_brief_answers(answers)
        conversation.append({"role": "agent", "text": "Rahmat! Ma'lumot yetarli -- pastdagi tugmani bosib rasmni yarating."})
        asset.set_brief_conversation(conversation)
        _refresh_missing(session, asset)
    else:
        conversation.append({"role": "agent", "text": step.get("question") or "", "placeholder": step.get("placeholder")})
        asset.set_brief_conversation(conversation)
    asset.updated_at = dt.datetime.utcnow()
    session.commit()


def start_brief(session, asset: "db.CreativeAsset", ctx: dict) -> dict:
    """Yangi (AI) asset yaratilgach BIR MARTA chaqiriladi (`create_draft_asset`
    dan keyin, yoki eski/holati 'collecting_brief' bo'lgan lekin suhbati
    hali boshlanmagan asset uchun "self-heal" sifatida GET marshrutida) --
    suhbat bo'sh bo'lsa BIRINCHI savolni oladi va saqlaydi. Suhbat
    allaqachon boshlangan bo'lsa -- idempotent (LLM QAYTA chaqirilmaydi)."""
    import creative_brief_agent
    conversation = asset.get_brief_conversation()
    if conversation:
        return {"done": False, "question": None, "placeholder": None, "brief": None}
    step = creative_brief_agent.next_step(ctx or {}, [])
    _apply_brief_step(session, asset, conversation, step)
    return step


def answer_brief(session, asset: "db.CreativeAsset", message: str, ctx: "dict | None" = None) -> dict:
    """Foydalanuvchining erkin matndagi javobini suhbatga qo'shadi va
    `creative_brief_agent.next_step()` orqali keyingi qadamni oladi --
    yangi savol (saqlanadi, UI ko'rsatadi) yoki "done" (`brief_answers_json`
    ESKI tekis shaklda to'ladi, `missing_fields_json` qayta hisoblanadi --
    keyingi qadam allaqachon mavjud `/generate` marshruti, bu funksiya
    faqat brifni "tayyor" qilib qo'yadi)."""
    import creative_brief_agent
    message = (message or "").strip()[:1000]
    if not message:
        raise CreativeError("Javob bo'sh -- biror narsa yozing.")
    if ctx is None:
        company = _load_company(session, asset)
        ctx = company_context_module.build_company_context(company, session) if company is not None else {}
    conversation = list(asset.get_brief_conversation())
    conversation.append({"role": "user", "text": message})
    step = creative_brief_agent.next_step(ctx or {}, conversation)
    _apply_brief_step(session, asset, conversation, step)
    return step


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


FALLBACK_CTA = "Bog'laning"


def brief_phone(ctx: dict, brief_answers: dict) -> str:
    """Reklamada ko'rsatiladigan telefon: brif javobi, bo'lmasa profil."""
    return ((brief_answers or {}).get("phone") or "").strip() or ((ctx or {}).get("phone") or "").strip()


def fallback_placeholder_values(ctx: dict, brief_answers: dict, template: "dict | None") -> dict:
    """AI'SIZ (deterministik) placeholder qiymatlari -- `placeholder_values`
    ning zaxira yo'li (AI matn yozuvchisi ishlamasa) va shablondan tezkor
    yaratish uchun:
      headline    -- focus (brif) yoki best_seller yoki product_or_service (QISQA)
      subheadline -- offer_text ("yo'q" bo'lmasa) yoki extra_notes
      cta_text    -- cta_preference yoki shablonning default_cta yoki FALLBACK_CTA
                     (2026-09: umumiy "Batafsil" ENDI ishlatilmaydi)
      offer_text  -- offer_text yoki "AKSIYA"; price_text -- price_range;
      brand_name  -- kompaniya nomi; quote_text -- extra_notes/offer;
      phone / phone_line -- brif yoki profil telefoni ("Tel: ..." ko'rinishida);
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
    phone = brief_phone(ctx, brief_answers)
    values = {
        "headline": _short(headline_src, 48),
        "subheadline": _short(offer, 70) if offer else _short(extra, 70),
        "cta_text": cta or ((template or {}).get("default_cta") or FALLBACK_CTA),
        "offer_text": _short(offer, 28) if offer else "AKSIYA",
        "price_text": _short(profile.get("price_range") or "", 24),
        "brand_name": (ctx.get("company_name") or "").strip(),
        "quote_text": _short(extra or offer, 110),
        "phone": phone,
        "phone_line": f"Tel: {phone}" if phone else "",
    }
    for i in range(4):
        values[f"feature_{i + 1}"] = features[i] if i < len(features) else ""
    return values


# ---------------------------------------------------------------------------
# AI MATN YOZUVCHISI (2026-09, foydalanuvchi shikoyati: "armatura
# sotishimiz kerak" deb yozgan xom javobi AYNAN sarlavha bo'lib chiqdi --
# AI buni to'g'ri, chiroyli reklama matniga aylantirsin; CTA ham
# umumiy "Batafsil" emas, biznesga mos bo'lsin).
# Arzon matn modeli (`OPENAI_MODEL`, standart gpt-4o-mini -- orchestrator
# yengil so'rovlar bilan bir xil), chat/completions, JSON javob, qisqa
# prompt, max_tokens kichik. HAR QANDAY xato -> None (chaqiruvchi
# `fallback_placeholder_values`ga qaytadi) -- rasm generatsiyasini
# HECH QACHON to'xtatmaydi.
# ---------------------------------------------------------------------------
OPENAI_TEXT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
_OPENAI_COPY_TIMEOUT = 40
_COPY_LIMITS = {"headline": 48, "subheadline": 80, "cta_text": 32, "price_text": 24}
_MAX_AI_FEATURES = 3
_AI_FEATURE_CHAR_LIMIT = 28

_COPY_SYSTEM_PROMPT = """Sen O'zbekiston bozori uchun Facebook/Instagram reklama rasmlariga matn yozadigan professional kopirayter san.
Vazifa: kompaniya ma'lumotlari va mijozning XOM javoblaridan reklama rasmiga qo'yiladigan TOZA, SAVODLI, JOZIBALI o'zbekcha (lotin) matn tuzish.
FAQAT JSON qaytar (izohsiz, ``` belgisiz):
{"headline": "...", "subheadline": "...", "cta_text": "...", "price_text": "...", "features": ["...", "..."]}
QOIDALAR:
- headline: 2-6 so'z, ko'pi bilan 40 belgi, o'qishga oson, reklama uslubida (masalan "Sifatli armatura — zavod narxida"). Mijozning xom so'zlarini AYNAN ko'chirma ("sotishimiz kerak", "reklama qilmoqchimiz" kabi ichki gaplar bo'lmasin) -- lekin haqiqiy faktlarni (mahsulot, aksiya, raqamlar) SAQLA, yangi va'da/raqam O'YLAB TOPMA. Narxni headline ICHIGA QO'SHMA -- narx alohida "price_text" maydonida, o'z belgisida ko'rsatiladi.
- subheadline: 1 qisqa gap, ko'pi bilan 70 belgi -- taklif/foyda/kafolat/yetkazib berish (aksiya bo'lsa, shuni). Aksiya ham, boshqa fakt ham bo'lmasa -- mahsulot haqida qisqa ishonchli gap.
- cta_text: 2-4 so'z, buyruq maylida, AYNAN shu biznesga mos ("Buyurtma bering", "Narxini bilib oling", "Qo'ng'iroq qiling", "Navbatga yoziling", "Ko'rishga keling"). Umumiy "Batafsil" YOZMA. Agar mijoz o'zi chaqiriq matnini bergan bo'lsa -- uni AYNAN qaytar.
- price_text: FAQAT pastda berilgan "Narx segmenti" satrida (yoki mijozning xom javoblarida ANIQ aytilgan narx bo'lsa, o'shanda) narx haqida ma'lumot bo'lsagina to'ldir -- shu narxni QISQA va JOZIBALI shaklga keltir (masalan "29.000 so'mdan boshlab", "300.000 so'mdan"). HECH QANDAY narx ma'lumoti berilmagan bo'lsa -- BO'SH satr "" qaytar, HECH QACHON raqam O'YLAB TOPMA yoki taxmin qilma.
- features: ko'pi bilan 3 ta QISQA (har biri ko'pi bilan 28 belgi) afzallik/xususiyat iborasi -- FAQAT "Qo'shimcha (profil)" yoki mijozning taklif/uslub javoblarida ANIQ aytilgan narsalardan (masalan "bepul yetkazib berish" aytilgan bo'lsa -- "Bepul yetkazib berish" deb qisqa yoz -- bu xulosa, RUXSAT ETILGAN). Matnda sifat/tezlik/kafolat haqida hech qanday aniq ishora bo'lmasa -- BO'SH ro'yxat [] qaytar, "Yuqori sifat"/"Tez yetkazib berish" kabi umumiy iboralarni HECH NARSAGA ASOSLANMAGAN holda O'ZINGDAN O'YLAB TOPMA (bu soxta da'vo bo'ladi).
- Apostroflar to'g'ri: o', g', so'm, ko'ring. Emoji, qo'shtirnoq, undov belgilarini ko'p ishlatma."""


def _copy_user_content(ctx: dict, brief_answers: dict, template: "dict | None") -> str:
    profile = ctx.get("profile_answers") or {}
    lines = [
        f"Kompaniya: {ctx.get('company_name') or '-'}",
        f"Soha: {((ctx.get('business_category') or {}).get('label')) or '-'}",
        f"Mahsulot/xizmat: {profile.get('product_or_service') or '-'}",
        f"Eng ko'p sotiladigani: {profile.get('best_seller') or '-'}",
        f"Auditoriya: {profile.get('target_audience') or '-'}",
        f"Narx segmenti: {profile.get('price_range') or '-'}",
        f"Qo'shimcha (profil): {profile.get('extra_notes') or '-'}",
        f"Telefon: {brief_phone(ctx, brief_answers) or '-'}",
        "",
        "MIJOZNING XOM JAVOBLARI (shu reklama uchun):",
        f"- Nimaga urg'u: {brief_answers.get('focus') or '-'}",
        f"- Aksiya/taklif: {brief_answers.get('offer_text') or '-'}",
        "- Chaqiriq matni (mijoz xohishi): " + (brief_answers.get("cta_preference") or "(bermagan -- o'zing tanla)"),
        f"- Uslub izohi: {brief_answers.get('style_notes') or '-'}",
    ]
    if template:
        lines.append(f"Shablon: {template.get('name')} (tavsiya etilgan chaqiriq: {template.get('default_cta') or '-'})")
    return "\n".join(lines)


def _request_ad_copy(body: dict):
    """Chat/completions HTTP chaqiruvi -- alohida funksiya (testlar shuni
    mock qiladi; rasm chaqiruvi `_openai_request` bilan aralashmasin)."""
    import call_analysis
    api_key = os.environ.get("OPENAI_API_KEY")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    return call_analysis._openai_request("POST", OPENAI_CHAT_URL, headers=headers, json_body=body, timeout=_OPENAI_COPY_TIMEOUT)


def _parse_copy_json(text: str) -> "dict | None":
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I)
    try:
        data = json.loads(t)
    except (TypeError, ValueError):
        m = re.search(r"\{.*\}", t, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except ValueError:
            return None
    if not isinstance(data, dict):
        return None
    out = {}
    for key, limit in _COPY_LIMITS.items():
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            out[key] = " ".join(v.strip().split())[:limit].strip(' "\'')
    feats = data.get("features")
    if isinstance(feats, list):
        clean_feats = []
        for f in feats:
            if isinstance(f, str) and f.strip():
                clean_feats.append(" ".join(f.strip().split())[:_AI_FEATURE_CHAR_LIMIT].strip(' "\''))
            if len(clean_feats) >= _MAX_AI_FEATURES:
                break
        if clean_feats:
            out["features"] = clean_feats
    return out or None


def generate_ad_copy(ctx: dict, brief_answers: dict, template: "dict | None") -> "dict | None":
    """AI kopirayter: {"headline", "subheadline", "cta_text", "price_text",
    "features": [...]} (faqat mavjud/bo'sh bo'lmagan kalitlar -- 2026-09,
    "yarim ma'lumot berayapsiz" shikoyati: narx va afzalliklar ENDI alohida
    maydon, AI ularni faqat berilgan real ma'lumotdan (narx segmenti,
    mijoz javoblari) chiqaradi, hech qachon o'ylab topmaydi -- yo'q bo'lsa
    bo'sh qaytaradi) yoki None (kalit sozlanmagan, kredit tugagan,
    tarmoq/HTTP xatosi, JSON buzuq). HECH QACHON exception ko'tarmaydi --
    rasm generatsiyasi davom etadi (zaxira matn bilan)."""
    if not os.environ.get("OPENAI_API_KEY"):
        return None
    body = {
        "model": OPENAI_TEXT_MODEL, "temperature": 0.5, "max_tokens": 160,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _COPY_SYSTEM_PROMPT},
            {"role": "user", "content": _copy_user_content(ctx or {}, brief_answers or {}, template)},
        ],
    }
    try:
        resp = _request_ad_copy(body)
    except Exception as e:  # noqa: BLE001 -- tarmoq va h.k.
        logger.warning("creative_studio: AI matn yozuvchisi tarmoq xatosi: %s", e)
        return None
    try:
        if resp.status_code == 429 and _is_quota_exhausted_response(resp):
            logger.error("creative_studio: AI matn yozuvchisi -- OpenAI kredit tugagan: %s", _extract_openai_error(resp))
            return None
        if resp.status_code != 200:
            logger.warning("creative_studio: AI matn yozuvchisi HTTP %s: %s", resp.status_code, _extract_openai_error(resp))
            return None
        content = resp.json()["choices"][0]["message"]["content"]
    except Exception as e:  # noqa: BLE001 -- kutilmagan javob shakli
        logger.warning("creative_studio: AI matn yozuvchisi javobini o'qib bo'lmadi: %s", e)
        return None
    parsed = _parse_copy_json(content)
    if not parsed:
        logger.warning("creative_studio: AI matn yozuvchisi JSON qaytarmadi")
    return parsed


def placeholder_values(ctx: dict, brief_answers: dict, template: "dict | None", *, use_ai: bool = True) -> dict:
    """Shablon/standart qatlamlardagi `{{...}}` placeholder'lar uchun REAL
    matnlar: `fallback_placeholder_values` (deterministik) USTIGA AI
    kopirayter natijasi (`generate_ad_copy`: headline/subheadline/cta_text/
    price_text/features) qo'yiladi. Mijoz `cta_preference` bergan bo'lsa --
    u har doim ustun. `price_text` -- AI bergan bo'lsa (bo'sh bo'lmasa)
    zaxira (`price_range`dan) ustidan yoziladi. `features` -- AI ro'yxat
    bergan bo'lsa (bo'sh bo'lmasa) `feature_1..4`ni TO'LIQ almashtiradi
    (yaxshiroq iboralangan bo'lgani uchun), aks holda zaxira (`extra_notes`
    vergul bo'yicha bo'lingan) qoladi. AI ishlamasa -- faqat zaxira
    qiymatlar (generatsiya to'xtamaydi). `use_ai=False` -- shablondan
    tezkor yaratish (OpenAI'siz va'dasi)."""
    values = fallback_placeholder_values(ctx, brief_answers, template)
    if not use_ai:
        return values
    copy_ = generate_ad_copy(ctx, brief_answers, template)
    if not copy_:
        return values
    for key in ("headline", "subheadline"):
        if copy_.get(key):
            values[key] = copy_[key]
    cta_pref = ((brief_answers or {}).get("cta_preference") or "").strip()
    if not cta_pref and copy_.get("cta_text"):
        values["cta_text"] = copy_["cta_text"]
    if copy_.get("price_text"):
        values["price_text"] = copy_["price_text"]
    feats = copy_.get("features")
    if isinstance(feats, list) and feats:
        for i in range(4):
            values[f"feature_{i + 1}"] = feats[i] if i < len(feats) else ""
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
    # 2026-09, onboarding'da tanlangan doimiy uslub tanlovi -- QO'SHIMCHA
    # (additive) nozik ishora, yuqoridagi shablon/standart "Visual style"
    # qatorini ALMASHTIRMAYDI (aniq shablon tanlovi -- kuchliroq signal,
    # doimiy tanlov -- shunchaki bezak yo'nalishi).
    preferred_styles = [s for s in (brand.get("preferred_styles") or []) if s in STYLE_DESCRIPTORS]
    if preferred_styles:
        nudge = "; ".join(STYLE_DESCRIPTORS[s] for s in preferred_styles)
        lines.append(f"Company's preferred visual style (subtle additional guidance, keep the visual style above as primary): {nudge}.")
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


def _request_openai_image_edit(prompt: str, size: str, reference_image_bytes: bytes, reference_content_type: "str | None") -> "tuple[bytes, str | None]":
    """OpenAI Images EDITS API'siga (`/v1/images/edits`, multipart/form-data)
    POST -- uslub namunasi rasmini `image` maydonida yuboradi. Bu rasmning
    rang palitrasi/yorug'lik/kompozitsiya KAYFIYATIGA yaqinlashtiradi --
    OpenAI fon rasmini QAYTADAN yaratadi, bu ANIQ NUSXA EMAS (piksel-piksel
    o'xshash rasm va'da qilinmaydi, faqat uslub yo'naltiriladi). Xato
    ishlov berish `_request_openai_image()` bilan BIR XIL (o'zbekcha
    `CreativeError`, xom matn faqat logda) -- chaqiruvchi (`_run_generation`)
    HAR QANDAY xatoda (shu jumladan bu funksiya ko'targan `CreativeError`)
    oddiy (referencesiz) generatsiyaga o'tadi, hech qachon generatsiyani
    to'xtatmaydi. `call_analysis._openai_request()` JSON'dan tashqari
    `data=`/`files=` (multipart) parametrlarini ham qo'llab-quvvatlaydi --
    shuning uchun xuddi shu umumiy qayta-urinish/xato-aniqlash qatlami
    orqali chaqiriladi (alohida xom `requests.post` YOZILMAYDI)."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise CreativeError(
            "OPENAI_API_KEY sozlanmagan -- AI rasm-generatsiya ishlamaydi. "
            "Administrator sozlab qo'ysin (shablondan foydalanish mumkin)."
        )
    # DIQQAT: Content-Type QO'LGA sozlanmaydi -- multipart bo'lgani uchun
    # `requests` o'zi to'g'ri boundary bilan sarlavhani qo'yadi (JSON
    # yo'lidan farqli, u yerda `_request_openai_image()` qo'lda sozlaydi).
    headers = {"Authorization": f"Bearer {api_key}"}
    data = {"model": OPENAI_IMAGE_MODEL, "prompt": prompt, "size": size, "n": 1, "quality": OPENAI_IMAGE_QUALITY}
    ext = ALLOWED_LOGO_TYPES.get((reference_content_type or "").split(";")[0].strip().lower(), ".png")
    files = {"image": (f"style_reference{ext}", reference_image_bytes, reference_content_type or "image/png")}
    try:
        resp = _openai_request("POST", OPENAI_IMAGE_EDITS_URL, headers=headers, data=data, files=files, timeout=_OPENAI_IMAGE_TIMEOUT)
    except requests.RequestException as e:
        logger.error("creative_studio: OpenAI (images/edits) tarmoq xatosi: %s", e)
        raise CreativeError(_OPENAI_GENERIC_MSG) from e
    if resp.status_code == 429 and _is_quota_exhausted_response(resp):
        logger.error("creative_studio: OpenAI (images/edits) kredit tugagan: %s", _extract_openai_error(resp))
        raise CreativeError(_OPENAI_CREDIT_MSG)
    if resp.status_code != 200:
        raw = _extract_openai_error(resp)
        logger.error("creative_studio: OpenAI images/edits xatosi HTTP %s: %s", resp.status_code, raw)
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
        logger.error("creative_studio: OpenAI (images/edits) javobini o'qib bo'lmadi: %s", e)
        raise CreativeError(_OPENAI_GENERIC_MSG) from e
    out = None
    if item.get("b64_json"):
        try:
            out = base64.b64decode(item["b64_json"])
        except Exception as e:  # noqa: BLE001
            logger.error("creative_studio: b64 dekod xatosi (images/edits): %s", e)
            raise CreativeError(_OPENAI_GENERIC_MSG) from e
    elif item.get("url"):
        try:
            r = requests.get(item["url"], timeout=60)
            r.raise_for_status()
            out = r.content
        except requests.RequestException as e:
            logger.error("creative_studio: rasm URL yuklab bo'lmadi (images/edits): %s", e)
            raise CreativeError(_OPENAI_GENERIC_MSG) from e
    if not out:
        raise CreativeError(_OPENAI_GENERIC_MSG)
    response_id = payload.get("id") or (str(payload["created"]) if payload.get("created") else None)
    return out, response_id


# ---------------------------------------------------------------------------
# QATLAMLAR (layers)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# SHABLONSIZ ("AI bilan yaratish") YO'L UCHUN BIR NECHTA BOY, XILMA-XIL
# KOMPOZITSIYA (2026-09, foydalanuvchi shikoyati, skrinshot bilan: "dizayn
# juda oddiy bo'lib qolyapti, har safar boshqacha bo'lsin, matn katta,
# sifatli, to'liq ma'lumot -- narx, xususiyat -- bersin, yarim-yorti emas").
#
# Ilgari BU YO'L (shablon tanlanmagan -- eng ko'p ishlatiladigan yo'l)
# doim BITTA qattiq `default_layers()`ni ishlatardi: 6 ta qatlam, headline
# `size_ratio=0.062`, hech qachon narx/xususiyat ko'rsatilmasdi -- har bir
# generatsiya BIR XIL ko'rinardi. Endi -- bir nechta ORIGINAL, real dizayn
# fikri bilan qurilgan variant (`_LAYOUT_VARIANTS`), va `select_default_
# layout()` ulardan qaysi biri HAQIQATAN mos kelishini (narx/xususiyat/
# taklif/iqtibos ma'lumoti bormi) `placeholder_values()` natijasidan
# ANIQLAYDI -- bo'sh joyga hech qachon bo'sh narx/xususiyat blokini
# ko'rsatmaydi. Bir xil `asset.id` -- doim bir xil variant (qayta tahrirda
# barqaror), turli `asset.id`lar -- turli variantlar (`random.Random`
# asset id bilan "urug'lantirilgan" -- xuddi bir xil kompaniya uchun ham
# har safar boshqacha ko'rinish).
# ---------------------------------------------------------------------------

def _v_text(id_, x, y, w, h, text, *, align="left", font="bold", size_ratio=0.06, color="#FFFFFF"):
    return {"id": id_, "type": "text", "x": x, "y": y, "w": w, "h": h, "align": align,
            "font": font, "size_ratio": size_ratio, "color": color, "text": text}


def _v_badge(id_, x, y, w, h, text, *, bg_color="#111111", color="#FFFFFF", size_ratio=0.026, font="bold", align="center", opacity=1.0):
    return {"id": id_, "type": "badge", "x": x, "y": y, "w": w, "h": h, "align": align,
            "font": font, "size_ratio": size_ratio, "color": color, "bg_color": bg_color,
            "opacity": opacity, "text": text}


def _v_panel(id_, x, y, w, h, bg_color, *, opacity=1.0, gradient=False):
    return {"id": id_, "type": "panel", "x": x, "y": y, "w": w, "h": h,
            "bg_color": bg_color, "opacity": opacity, "gradient": gradient}


def _v_logo(x, y, w, h, align="right"):
    return {"id": "logo", "type": "logo", "x": x, "y": y, "w": w, "h": h, "align": align}


def _v_phone(x, y, w, h, *, align="left", color="#FFFFFF", size_ratio=0.026, font="bold"):
    # 2026-09: telefon (brif/profil) -- lid-reklama uchun odatiy element;
    # raqam bo'lmasa qatlam yashirin (`resolve_layers`). HAR bir variantda
    # bo'lishi SHART (logotip kabi) -- "telefon yo'qolib qoladi" shikoyati
    # takrorlanmasin.
    return _v_text("phone", x, y, w, h, "{{phone_line}}", align=align, font=font, size_ratio=size_ratio, color=color)


def _layout_bottom_bold_simple(values: dict) -> list[dict]:
    """Bazaviy variant -- to'liq kenglikdagi pastki panel, lekin sarlavha
    ESKISIGA qaraganda SEZILARLI kattaroq (0.062 -> 0.082) va panel
    balandroq/quyuqroq (matn o'qilishi uchun kontrast kuchliroq)."""
    return [
        _v_badge("cta_badge", 0.06, 0.055, 0.34, 0.065, "{{cta_text}}", bg_color="#111111", opacity=0.92),
        _v_logo(0.72, 0.055, 0.22, 0.10),
        _v_panel("bottom_panel", 0.0, 0.58, 1.0, 0.42, "#000000", opacity=0.78, gradient=True),
        _v_text("headline", 0.06, 0.68, 0.88, 0.16, "{{headline}}", size_ratio=0.082),
        _v_text("subheadline", 0.06, 0.855, 0.88, 0.07, "{{subheadline}}", font="regular", size_ratio=0.030, color="#E5E7EB"),
        _v_phone(0.06, 0.94, 0.88, 0.045, size_ratio=0.026),
    ]


def _layout_corner_card_clean(values: dict) -> list[dict]:
    """Muqobil bazaviy variant -- to'liq kengdagi panel EMAS, pastki
    O'NG burchakda ixcham "karta" (logotip esa CHAP tomonda) -- fonning
    ko'p qismi ochiq qoladi, kompozitsiya butunlay boshqacha tuyuladi."""
    return [
        _v_logo(0.06, 0.06, 0.20, 0.09, align="left"),
        _v_panel("card", 0.34, 0.56, 0.60, 0.40, "#0F172A", opacity=0.88),
        _v_text("headline", 0.38, 0.605, 0.52, 0.17, "{{headline}}", size_ratio=0.078),
        _v_text("subheadline", 0.38, 0.755, 0.52, 0.09, "{{subheadline}}", font="regular", size_ratio=0.028, color="#CBD5E1"),
        _v_badge("cta_badge", 0.38, 0.865, 0.40, 0.07, "{{cta_text}}", bg_color="#FFFFFF", color="#0F172A", size_ratio=0.024),
        _v_phone(0.38, 0.945, 0.52, 0.04, font="regular", size_ratio=0.020, color="#E2E8F0"),
    ]


def _layout_price_spotlight(values: dict) -> list[dict]:
    """Narx MA'LUM bo'lgandagina tanlanadi -- narx katta, sariq "yorliq"
    sifatida yuqori o'ng burchakda alohida element (headline'ga
    aralashtirilmagan, foydalanuvchi aynan shuni so'ragan)."""
    return [
        _v_logo(0.06, 0.06, 0.20, 0.09, align="left"),
        _v_panel("price_shadow", 0.605, 0.075, 0.34, 0.20, "#0C4A6E", opacity=0.45),
        _v_badge("price_badge", 0.585, 0.06, 0.34, 0.20, "{{price_text}}", bg_color="#FACC15", color="#1F2937", size_ratio=0.058),
        _v_panel("bottom_panel", 0.0, 0.64, 1.0, 0.36, "#000000", opacity=0.80, gradient=True),
        _v_text("headline", 0.06, 0.72, 0.88, 0.13, "{{headline}}", size_ratio=0.078),
        _v_text("subheadline", 0.06, 0.855, 0.88, 0.06, "{{subheadline}}", font="regular", size_ratio=0.028, color="#E5E7EB"),
        _v_badge("cta_badge", 0.06, 0.92, 0.40, 0.06, "{{cta_text}}", bg_color="#FACC15", color="#1F2937", size_ratio=0.024),
        _v_phone(0.50, 0.93, 0.44, 0.045, align="right", font="regular", size_ratio=0.022),
    ]


def _layout_feature_stack(values: dict) -> list[dict]:
    """Xususiyat/afzallik matni MAVJUD bo'lgandagina tanlanadi -- o'ng
    tomonda 3 ta kichik "belgi" ustma-ust joylashadi, headline chap
    tomonda torroq ustunda (xususiyatlar bilan bir qatorda o'qiladi)."""
    return [
        _v_logo(0.06, 0.055, 0.20, 0.09, align="left"),
        _v_badge("feature_1", 0.62, 0.32, 0.32, 0.09, "{{feature_1}}", bg_color="#FFFFFF", color="#111111", size_ratio=0.021, align="left", opacity=0.93),
        _v_badge("feature_2", 0.62, 0.43, 0.32, 0.09, "{{feature_2}}", bg_color="#FFFFFF", color="#111111", size_ratio=0.021, align="left", opacity=0.93),
        _v_badge("feature_3", 0.62, 0.54, 0.32, 0.09, "{{feature_3}}", bg_color="#FFFFFF", color="#111111", size_ratio=0.021, align="left", opacity=0.93),
        _v_panel("bottom_panel", 0.0, 0.68, 1.0, 0.32, "#000000", opacity=0.80, gradient=True),
        _v_text("headline", 0.06, 0.735, 0.52, 0.14, "{{headline}}", size_ratio=0.075),
        _v_text("subheadline", 0.06, 0.865, 0.52, 0.06, "{{subheadline}}", font="regular", size_ratio=0.026, color="#E5E7EB"),
        _v_badge("cta_badge", 0.62, 0.865, 0.32, 0.06, "{{cta_text}}", bg_color="#FFFFFF", color="#111111", size_ratio=0.022),
        _v_phone(0.06, 0.94, 0.88, 0.04, font="regular", size_ratio=0.020, color="#E5E7EB"),
    ]


def _layout_quote_spotlight(values: dict) -> list[dict]:
    """Mijoz/mahsulot haqida "iqtibos"ga o'xshash matn (`quote_text`)
    MAVJUD bo'lgandagina tanlanadi -- markazlashgan oq karta ustida katta
    qo'shtirnoq bezagi + iqtibos, headline IKKINCHI darajali (kichikroq,
    chunki bu yerda diqqat markazi -- matn, headline emas)."""
    return [
        _v_logo(0.40, 0.05, 0.20, 0.09, align="center"),
        _v_panel("quote_card", 0.06, 0.24, 0.88, 0.50, "#FFFFFF", opacity=0.92),
        _v_text("quote_mark", 0.10, 0.215, 0.18, 0.14, "“", size_ratio=0.20, color="#E8C9A0"),
        _v_text("quote_text", 0.12, 0.30, 0.76, 0.20, "{{quote_text}}", font="regular", size_ratio=0.038, color="#1F2937", align="center"),
        _v_text("headline", 0.12, 0.52, 0.76, 0.08, "{{headline}}", align="center", size_ratio=0.040, color="#7C5B2B"),
        _v_badge("cta_badge", 0.32, 0.625, 0.36, 0.065, "{{cta_text}}", bg_color="#1F2937", color="#FFFFFF", size_ratio=0.024),
        _v_phone(0.12, 0.70, 0.76, 0.035, align="center", font="regular", size_ratio=0.018, color="#57534E"),
    ]


def _layout_price_feature_showcase(values: dict) -> list[dict]:
    """ENG BOY variant (narx VA xususiyatlar ikkalasi ham mavjud bo'lganda
    tanlanadi): yuqorida narx yorlig'i (+ agar HAQIQIY aksiya bo'lsa --
    alohida qizil taklif belgisi), o'rtada 3 ta xususiyat qatorda, pastda
    to'liq matn paneli -- foydalanuvchi so'ragan "to'liq ma'lumot"ning
    aynan o'zi (narx, xususiyat, taklif -- barchasi ALOHIDA, aniq ko'rinadi)."""
    layers = [
        _v_logo(0.06, 0.05, 0.18, 0.08, align="left"),
        _v_badge("price_badge", 0.68, 0.045, 0.27, 0.11, "{{price_text}}", bg_color="#FACC15", color="#1F2937", size_ratio=0.030),
    ]
    if (values.get("offer_text") or "").strip() and values["offer_text"] != "AKSIYA":
        layers.append(_v_badge("offer_badge", 0.28, 0.05, 0.38, 0.075, "{{offer_text}}", bg_color="#DC2626", color="#FFFFFF", size_ratio=0.024))
    layers += [
        _v_badge("feature_1", 0.06, 0.40, 0.28, 0.09, "{{feature_1}}", bg_color="#FFFFFF", color="#111111", size_ratio=0.020, opacity=0.93),
        _v_badge("feature_2", 0.36, 0.40, 0.28, 0.09, "{{feature_2}}", bg_color="#FFFFFF", color="#111111", size_ratio=0.020, opacity=0.93),
        _v_badge("feature_3", 0.66, 0.40, 0.28, 0.09, "{{feature_3}}", bg_color="#FFFFFF", color="#111111", size_ratio=0.020, opacity=0.93),
        _v_panel("bottom_panel", 0.0, 0.60, 1.0, 0.40, "#000000", opacity=0.80, gradient=True),
        _v_text("headline", 0.06, 0.66, 0.88, 0.13, "{{headline}}", size_ratio=0.076),
        _v_text("subheadline", 0.06, 0.80, 0.88, 0.06, "{{subheadline}}", font="regular", size_ratio=0.026, color="#E5E7EB"),
        _v_badge("cta_badge", 0.06, 0.875, 0.40, 0.06, "{{cta_text}}", bg_color="#FACC15", color="#1F2937", size_ratio=0.022),
        _v_phone(0.50, 0.885, 0.44, 0.04, align="right", font="regular", size_ratio=0.020),
    ]
    return layers


def _layout_price_feature_editorial(values: dict) -> list[dict]:
    """Narx VA xususiyatlar mavjud bo'lganda `price_feature_showcase`ga
    MUQOBIL, butunlay BOSHQA kompozitsiya (bir xil ma'lumot -- ikkinchi
    tanlov bo'lmasa, bir xil "boy" holat ham har doim bir xil ko'rinardi):
    to'liq balandlikdagi CHAP yon panel (narx+xususiyatlar+CTA shu yerda
    ustma-ust), headline esa O'NG pastda, fon rasmi ustida to'g'ridan-to'g'ri."""
    return [
        _v_panel("side_panel", 0.0, 0.0, 0.38, 1.0, "#111111", opacity=0.82),
        _v_logo(0.04, 0.05, 0.30, 0.09, align="left"),
        _v_badge("price_badge", 0.04, 0.18, 0.30, 0.11, "{{price_text}}", bg_color="#FACC15", color="#1F2937", size_ratio=0.028),
        _v_badge("feature_1", 0.04, 0.34, 0.30, 0.08, "{{feature_1}}", bg_color="#FFFFFF", color="#111111", size_ratio=0.018, align="left", opacity=0.92),
        _v_badge("feature_2", 0.04, 0.44, 0.30, 0.08, "{{feature_2}}", bg_color="#FFFFFF", color="#111111", size_ratio=0.018, align="left", opacity=0.92),
        _v_badge("feature_3", 0.04, 0.54, 0.30, 0.08, "{{feature_3}}", bg_color="#FFFFFF", color="#111111", size_ratio=0.018, align="left", opacity=0.92),
        _v_badge("cta_badge", 0.04, 0.66, 0.30, 0.07, "{{cta_text}}", bg_color="#FFFFFF", color="#111111", size_ratio=0.022),
        _v_phone(0.04, 0.90, 0.30, 0.05, font="regular", size_ratio=0.020, color="#E5E7EB"),
        _v_panel("headline_scrim", 0.40, 0.66, 0.58, 0.34, "#000000", opacity=0.55, gradient=True),
        _v_text("headline", 0.44, 0.72, 0.50, 0.20, "{{headline}}", size_ratio=0.080),
        _v_text("subheadline", 0.44, 0.88, 0.50, 0.08, "{{subheadline}}", font="regular", size_ratio=0.026, color="#E5E7EB"),
    ]


def _has_value(values: dict, key: str) -> bool:
    return bool(str((values or {}).get(key) or "").strip())


def _has_real_offer(values: dict) -> bool:
    v = str((values or {}).get("offer_text") or "").strip()
    return bool(v) and v != "AKSIYA"


# Har bir variant: (kalit, qurish funksiyasi, boylik darajasi -- richness,
# tanlanish sharti -- values'da HAQIQIY (bo'sh bo'lmagan) ma'lumot bormi).
# richness KATTAROQ -> ustuvor; bir xil richness'dagi barcha mos variantlar
# orasidan `random.Random(asset_id)` bilan tanlanadi (xilma-xillik).
_LAYOUT_VARIANTS = [
    ("bottom_bold_simple", _layout_bottom_bold_simple, 0, lambda v: True),
    ("corner_card_clean", _layout_corner_card_clean, 0, lambda v: True),
    ("price_spotlight", _layout_price_spotlight, 1, lambda v: _has_value(v, "price_text")),
    ("feature_stack", _layout_feature_stack, 1, lambda v: _has_value(v, "feature_1")),
    ("quote_spotlight", _layout_quote_spotlight, 1, lambda v: _has_value(v, "quote_text")),
    ("price_feature_showcase", _layout_price_feature_showcase, 2, lambda v: _has_value(v, "price_text") and _has_value(v, "feature_1")),
    ("price_feature_editorial", _layout_price_feature_editorial, 2, lambda v: _has_value(v, "price_text") and _has_value(v, "feature_1")),
]


def select_default_layout(values: dict, seed=None) -> list[dict]:
    """Shablon tanlanmagan ("AI bilan yaratish") yo'l uchun ENG BOY, lekin
    HAQIQATAN mos keladigan kompozitsiyani tanlaydi (`_LAYOUT_VARIANTS`):
      1. Faqat `values`da HAQIQIY (bo'sh bo'lmagan) ma'lumoti bor
         variantlar ko'rib chiqiladi (masalan narx yorlig'i ko'rsatadigan
         variant narx bo'lmasa UMUMAN tanlanmaydi -- bo'sh belgi chizilmaydi).
      2. Ular orasidan ENG YUQORI "richness" (necha ta qo'shimcha element
         -- narx/xususiyat/iqtibos) darajasidagilar tanlanadi.
      3. Bir nechtasi teng bo'lsa (masalan headline/subheadline/cta'dan
         boshqa hech narsa yo'q -- ikkita bazaviy variant ham mos) --
         `random.Random(seed)` bilan BARQAROR (bir xil `seed` -- doim bir
         xil natija, qayta tahrirda o'zgarmaydi) lekin XILMA-XIL (turli
         `seed` -- turli natija) tanlov qilinadi.
    Hech qanday variant mos kelmasa (nazariy jihatdan bo'lmasligi kerak --
    ikkita bazaviy variant shart har doim `True`) -- eng birinchi bazaviy
    variant qaytariladi (xavfsizlik uchun)."""
    import random
    eligible = [(key, build, richness) for key, build, richness, cond in _LAYOUT_VARIANTS if cond(values or {})]
    if not eligible:
        return _layout_bottom_bold_simple(values or {})
    max_richness = max(r for _, _, r in eligible)
    richest = [(key, build) for key, build, r in eligible if r == max_richness]
    if len(richest) == 1:
        chosen = richest[0]
    else:
        rng = random.Random(seed if seed is not None else 0)
        chosen = rng.choice(richest)
    return chosen[1](values or {})


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
# CHAT ORQALI TEZKOR TAHRIR ("logotipni kattaroq qil", "sarlavhani
# qisqartir" -- 2026-09, foydalanuvchi so'rovi). MUHIM: bu OpenAI rasm
# generatsiyasini QAYTA CHAQIRMAYDI -- LLM faqat MAVJUD qatlamlar ustidan
# kichik, tekshirilgan patch beradi, qolgani `set_layers()` (sof Pillow,
# tez, kvota sarflamaydi) orqali ketadi. `check_quota()`/`increment_usage()`
# bu yo'lda HECH QAERDA chaqirilmaydi.
# ---------------------------------------------------------------------------

class LayerEditUnavailableError(CreativeError):
    """AI tahrir agenti (LLM) ishlamadi -- qatlamlar TEGILMAYDI."""


_LAYER_EDIT_ALLOWED_FIELDS = {"text", "color", "bg_color", "align", "size_ratio", "opacity", "font", "gradient", "hidden", "x", "y", "w", "h"}
_LAYER_EDIT_UNAVAILABLE_MSG = (
    "AI tahrir agenti hozir javob bera olmadi. Birozdan keyin qayta urinib ko'ring yoki qatlamlarni qo'lda tahrirlang."
)

_LAYER_EDIT_SYSTEM = """Sen Replix Kreativ studiyasining TEZKOR TAHRIR AGENTISAN. Foydalanuvchi tabiiy tilda buyruq
beradi (masalan "logotipni kattaroq qil", "sarlavhani qisqartir", "fon rangini to'qroq qil"), sen buni MAVJUD
qatlamlar ustidan STRUKTURALI patch'ga aylantirasan -- YANGI rasm CHIZMAYSAN, faqat berilgan qatlamlarning
xususiyatlarini o'zgartirasan. FAQAT JSON qaytar (izohsiz, ``` belgisiz):
{"changes": [{"layer_id": "...", "field": "...", "value": ...}, ...], "reply": "<qisqa o'zbekcha javob>"}

QOIDALAR:
- "layer_id" FAQAT pastda berilgan MAVJUD qatlamlar ro'yxatidagi "id" bo'lishi kerak -- yangi id o'ylab topma.
- Ruxsat etilgan "field" qiymatlari: text, color, bg_color, align, size_ratio, opacity, font, gradient, hidden,
  x, y, w, h (x/y/w/h -- 0..1 nisbiy koordinata, rasmning chap-yuqori burchagidan).
- "kattaroq/kichikroq qil" -- shu qatlamning "w" VA "h"ni mutanosib oshir/kamayt (masalan 20% kattaroq -> ikkalasini
  ham ~1.2 barobar, lekin 0..1 oralig'idan chiqmasin).
- Rang so'ralsa -- matn uchun "color", fon/panel/tugma uchun "bg_color", "#RRGGBB" formatida.
- "matnni qisqartir/uzunroq/kuchliroq yoz" -- "text" maydoniga YANGI matn yoz (haqiqiy faktlarni O'YLAB TOPMA,
  faqat mavjud matnni qisqartir/uslubini o'zgartir).
- "yashir/olib tashla" -- {"field": "hidden", "value": true}; "qaytar/ko'rsat" -- {"field": "hidden", "value": false}.
- Qaysi qatlamga tegishli ekanligi aniq aytilmasa -- eng mos qatlamni TANLA (masalan "sarlavha" -> id="headline"
  yoki eng katta shriftli "text" turi; "tugma"/"chaqiriq" -> "badge" turi).
- Bir nechta qatlamga tegishli bo'lsa -- barchasi uchun alohida element qo'sh (masalan "hammasini oqqa bo'ya").
- reply -- 1 qisqa o'zbekcha jumla, nima o'zgarganini ayt."""


def _layer_edit_llm(system_prompt: str, user_content: str) -> dict:
    """`_llm()` yupqa o'rami (`ai_campaign_planner`/`creative_brief_agent`
    bilan bir xil naqsh) -- import chaqiruv paytida emas."""
    import orchestrator
    try:
        return orchestrator._call_agent(system_prompt, user_content)
    except (orchestrator.TargetologFormatError, orchestrator.AgentUnavailableError) as e:
        logger.warning("creative_studio: tahrir agenti LLM javob bermadi (%s)", type(e).__name__)
        raise LayerEditUnavailableError(_LAYER_EDIT_UNAVAILABLE_MSG) from e


def _layer_edit_user_content(ctx: dict, layers: list, message: str) -> str:
    lines = [
        company_context_module.company_context_prompt_block(ctx or {}),
        "",
        "# HOZIRGI QATLAMLAR (JSON)",
        json.dumps(layers, ensure_ascii=False),
        "",
        "# FOYDALANUVCHI BUYRUG'I",
        message,
        "",
        "Yuqoridagi qoidalar bo'yicha FAQAT JSON qaytar.",
    ]
    return "\n".join(lines)


def chat_edit_layers(session, asset: "db.CreativeAsset", ctx: dict, message: str) -> "tuple[db.CreativeAsset, str]":
    """Erkin matndagi tahrir buyrug'ini (masalan "logotipni kattaroq qil")
    LLM orqali kichik, TEKSHIRILGAN patch'ga aylantiradi va qo'llaydi:
      1. `asset.get_layers()`ning NUSXASI olinadi (LLM xato bersa asl
         qatlamlar TEGILMAYDI).
      2. LLM'dan `{"changes": [{"layer_id","field","value"}, ...], "reply"}`
         so'raladi.
      3. Har bir element TEKSHIRILADI: `layer_id` MAVJUD qatlamda bo'lishi
         SHART, `field` `_LAYER_EDIT_ALLOWED_FIELDS`dan bo'lishi SHART
         (`type`/`id` kabi strukturaviy maydonlar HECH QACHON o'zgarmaydi)
         -- mos kelmagani jimgina (log bilan) tashlab yuboriladi, qolgan
         to'g'ri elementlar baribir qo'llanadi.
      4. Kamida bitta o'zgarish qo'llanmasa -- `CreativeError` (qatlamlar
         tegilmagan).
      5. `set_layers()` (sanitize + `_render_asset` -- sof Pillow, OpenAI
         CHAQIRILMAYDI, kvota sarflanmaydi) -- xuddi qo'lda tahrirlagandek.
    Qaytaradi: (yangilangan asset, LLM'ning qisqa o'zbekcha javobi)."""
    message = (message or "").strip()[:500]
    if not message:
        raise CreativeError("Buyruq bo'sh -- nimani o'zgartirishni yozing.")
    if not asset.final_storage_path:
        raise CreativeError("Avval rasmni generatsiya qiling -- keyin tahrirlash mumkin.")
    layers = copy.deepcopy(asset.get_layers())
    if not layers:
        raise CreativeError("Bu rasmda qatlamlar yo'q -- avval qatlam qo'shing.")

    raw = _layer_edit_llm(_LAYER_EDIT_SYSTEM, _layer_edit_user_content(ctx or {}, layers, message))
    if not isinstance(raw, dict):
        raise LayerEditUnavailableError(_LAYER_EDIT_UNAVAILABLE_MSG)
    reply = str(raw.get("reply") or "").strip()
    changes = raw.get("changes") if isinstance(raw.get("changes"), list) else []

    by_id = {l.get("id"): l for l in layers if isinstance(l, dict) and l.get("id")}
    applied = 0
    for item in changes:
        if not isinstance(item, dict):
            continue
        layer_id = item.get("layer_id")
        field = item.get("field")
        if layer_id not in by_id:
            logger.warning("creative_studio: chat tahrir -- noma'lum layer_id rad etildi: %r", layer_id)
            continue
        if field not in _LAYER_EDIT_ALLOWED_FIELDS:
            logger.warning("creative_studio: chat tahrir -- ruxsat etilmagan field rad etildi: %r", field)
            continue
        by_id[layer_id][field] = item.get("value")
        applied += 1
    if not applied:
        raise CreativeError(reply or "AI hech qanday o'zgartirish taklif qilmadi -- buyruqni aniqroq yozing.")

    set_layers(session, asset, layers)
    return asset, (reply or "O'zgartirildi.")


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
    # Avto-moslash: ko'rinadigan qism (shaffof hoshiyalarsiz) olinadi --
    # fon kesilgan yoki keng shaffof hoshiyali PNG ham to'rtburchakni
    # to'liq ishlatadi.
    bbox = logo.getchannel("A").getbbox()
    if bbox and (bbox[2] - bbox[0]) >= 2 and (bbox[3] - bbox[1]) >= 2:
        logo = logo.crop(bbox)
    x, y = int(layer["x"] * W), int(layer["y"] * H)
    w, h = max(1, int(layer["w"] * W)), max(1, int(layer["h"] * H))
    # Ichki hoshiya (~6%) -- logotip qutiga "yopishib" turmasin.
    pad = int(min(w, h) * LOGO_INNER_PAD)
    iw, ih = max(1, w - 2 * pad), max(1, h - 2 * pad)
    scale = min(iw / logo.width, ih / logo.height)
    nw, nh = max(1, int(logo.width * scale)), max(1, int(logo.height * scale))
    logo = logo.resize((nw, nh), Image.LANCZOS)
    align = layer.get("align") or "right"
    if align == "left":
        lx = x + pad
    elif align == "center":
        lx = x + (w - nw) // 2
    else:
        lx = x + w - pad - nw
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
    base_path = storage_backend.ensure_local(CREATIVE_ROOT, asset.base_image_storage_path, key_prefix="creative_studio")
    if not base_path.exists():
        raise CreativeError("Fon rasmi diskda topilmadi -- qayta generatsiya qiling.")
    size = _target_pixels_for_aspect(asset.aspect)
    out_rel = _asset_rel(asset, "final.png")
    out_path = Path(CREATIVE_ROOT) / out_rel
    brand_kit = get_brand_kit(session, asset.company_id)
    render_composite(base_path, asset.get_layers(), brand_kit, out_path, target_size=size)
    # 2026-09, R2 doimiy saqlash: yakuniy kompozit -- foydalanuvchi ko'rgan/
    # eksport qilgan/Autopilot'ga tashlagan rasm, hech qachon jimgina
    # yo'qolmasligi kerak.
    storage_backend.upload_file(out_path, f"creative_studio/{out_rel}")
    asset.final_storage_path = out_rel
    asset.width, asset.height = size


def _initial_layers_for(asset, ctx: dict, template: "dict | None", values: "dict | None" = None) -> list[dict]:
    if values is None:
        values = placeholder_values(ctx, asset.get_brief_answers(), template, use_ai=False)
    source = template["layers"] if template else select_default_layout(values, asset.id)
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
        "preferred_styles": brand_kit.get_preferred_styles() if brand_kit is not None else [],
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
        size = _size_for_aspect(asset.aspect)
        # 2026-09, "uslubga yaqin turishi kerak" (foydalanuvchi so'zi bilan --
        # namuna rasm bilan "yonma-yon"): agar kompaniyada uslub namunasi
        # rasmi bo'lsa VA model `images/edits`ni qo'llab-quvvatlasa
        # (`gpt-image-*`, dall-e-* EMAS), fon o'sha rasmning rang palitrasi/
        # yorug'lik/kayfiyatiga YAQINLASHTIRILGAN holda so'raladi. Bu ANIQ
        # nusxa EMAS -- OpenAI kompozitsiyani qaytadan yaratadi, faqat uslub
        # yo'naltiriladi. ISHLAMASA (har qanday sabab -- tarmoq, format,
        # xavfsizlik) -- log ogohlantirish bilan ODDIY (referencesiz)
        # generatsiyaga o'tiladi, generatsiya HECH QACHON shu sabab bilan
        # to'xtamaydi.
        data = None
        response_id = None
        if OPENAI_IMAGE_MODEL.startswith("gpt-image") and getattr(brand_kit, "style_reference_storage_path", None):
            try:
                ref_path = storage_backend.ensure_local(BRAND_ROOT, brand_kit.style_reference_storage_path, key_prefix="brand_kit")
                if ref_path.exists():
                    data, response_id = _request_openai_image_edit(prompt, size, ref_path.read_bytes(), brand_kit.style_reference_content_type)
            except Exception as e:  # noqa: BLE001 -- reference yo'li ishlamasa oddiy generatsiyaga o'tiladi
                logger.warning("creative_studio: uslub namunasiga asoslangan generatsiya ishlamadi (asset=%s), oddiy generatsiyaga o'tildi: %s", asset.id, e)
                data = None
        if data is None:
            data, response_id = _request_openai_image(prompt, size)
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
        # 2026-09, R2 doimiy saqlash: base.png -- OpenAI'dan qimmat olingan
        # manba rasm, jimgina yo'qolib qolmasligi SHART.
        storage_backend.upload_file(d / "base.png", f"creative_studio/{asset.base_image_storage_path}")
        asset.openai_response_id = (response_id or "")[:128] or None
        if not (keep_layers and asset.get_layers()):
            # AI kopirayter (arzon matn modeli) -- xom brif javoblari
            # o'rniga savodli sarlavha/tavsif/chaqiriq; ishlamasa zaxira.
            values = placeholder_values(ctx, asset.get_brief_answers(), template, use_ai=True)
            asset.set_layers(_initial_layers_for(asset, ctx, template, values))
            if not asset.title:
                asset.title = values.get("headline") or None
        if not asset.title:
            asset.title = fallback_placeholder_values(ctx, asset.get_brief_answers(), template).get("headline") or None
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
        storage_backend.upload_file(d / "base.png", f"creative_studio/{asset.base_image_storage_path}")
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
    path = storage_backend.ensure_local(CREATIVE_ROOT, asset.final_storage_path, key_prefix="creative_studio")
    if not path.exists():
        raise CreativeError("Yakuniy rasm diskda topilmadi -- qatlamlarni qayta saqlang yoki qayta generatsiya qiling.")
    return path


def asset_base_image_path(asset) -> "Path | None":
    """Fon (matnsiz) rasmning diskdagi yo'li (`asset` yoki
    `base_image_storage_path` bo'lmasa `None`) -- `brand_logo_original_path`
    uslubida, R2'dan self-heal bilan (`ensure_local`)."""
    if asset is None or not getattr(asset, "base_image_storage_path", None):
        return None
    return storage_backend.ensure_local(CREATIVE_ROOT, asset.base_image_storage_path, key_prefix="creative_studio")


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
    """Qatorni, diskdagi papkasini VA R2'dagi nusxalarini (sozlangan
    bo'lsa) o'chiradi (commit qiladi)."""
    import shutil
    d = _asset_dir(asset)
    r2_prefix = f"creative_studio/{asset.company_id}/{asset.id}/"
    session.delete(asset)
    session.commit()
    try:
        if d.exists():
            shutil.rmtree(d)
    except OSError as e:
        logger.warning("creative_studio: papka o'chirilmadi (%s): %s", d, e)
    storage_backend.delete_prefix(r2_prefix)
