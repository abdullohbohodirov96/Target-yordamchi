"""company_context.py — Meta Ads Autopilot uchun KOMPANIYA KONTEKSTI (2026-09,
foydalanuvchi so'rovi: "Replix ... Ads Manager'dagi har bir maydonni
boshidan o'zi to'ldirmasligi kerak" -- AI kampaniyani kompaniyaning
SAQLANGAN biznes-profilidan kelib chiqib rejalashtirishi kerak, umumiy
"shablon" javob bermasligi kerak).

Bu modul HAR BIR AI chaqiruvi (rejalashtiruvchi `ai_campaign_planner.
plan_campaign`, chat-tahrirlovchi `chat_edit`) oladigan YAGONA haqiqat
manbaini quradi: kompaniya nomi/tarifi, biznes yo'nalishi, profil savollariga
javoblar, ulangan Meta aktivlari (reklama hisobi/sahifa/IG/Pixel), standart
hudud (profil javobidan ajratib olinadi), so'nggi 30 kunlik lead-natijalar
(arzon bo'lsa) va rejalashtiruvchi uchun YETISHMAYOTGAN maydonlar ro'yxati.

Ikki ochiq funksiya:
  - `build_company_context(company, session=None) -> dict`
  - `company_context_prompt_block(ctx) -> str` -- promptga qo'shiladigan
    ixcham o'zbekcha matn bloki.
"""

import logging
import datetime as dt

import business_profile

logger = logging.getLogger("company_context")

# Rejalashtiruvchi (AI) eng ko'p tayanadigan profil maydonlari -- bo'sh
# bo'lsa `missing_fields`ga tushadi (UI "profilni to'ldiring" deb ko'rsatadi,
# planner esa `missing_questions()` orqali kerak bo'lsa so'raydi).
PLANNER_WANTED_FIELDS = ["business_category", "product_or_service", "target_audience", "price_range", "location"]

# O'zbekistonning yirik shaharlari/viloyatlari -- `target_audience` javobidan
# ("20-40 yosh, ko'proq ayollar, Toshkent shahri") standart hududni ajratib
# olish uchun. Bu FAQAT taxminiy "hint" -- haqiqiy Meta geo-kaliti keyin
# `meta_api.search_geo_location()` orqali aniqlanadi (hech qachon uydirma
# ID ishlatilmaydi). Kalit -- normallashtirilgan nom, qiymat -- Meta
# qidiruviga beriladigan lotincha nom.
_KNOWN_LOCATIONS = [
    ("toshkent", "Tashkent"), ("tashkent", "Tashkent"), ("ташкент", "Tashkent"),
    ("samarqand", "Samarkand"), ("samarkand", "Samarkand"), ("самарканд", "Samarkand"),
    ("buxoro", "Bukhara"), ("bukhara", "Bukhara"), ("бухара", "Bukhara"),
    ("andijon", "Andijan"), ("andijan", "Andijan"), ("андижан", "Andijan"),
    ("farg'ona", "Fergana"), ("fargona", "Fergana"), ("fergana", "Fergana"), ("фергана", "Fergana"),
    ("namangan", "Namangan"), ("наманган", "Namangan"),
    ("nukus", "Nukus"), ("нукус", "Nukus"),
    ("qarshi", "Qarshi"), ("karshi", "Qarshi"), ("карши", "Qarshi"),
    ("termiz", "Termez"), ("termez", "Termez"), ("термез", "Termez"),
    ("urganch", "Urgench"), ("urgench", "Urgench"), ("ургенч", "Urgench"),
    ("xiva", "Khiva"), ("khiva", "Khiva"),
    ("jizzax", "Jizzakh"), ("jizzakh", "Jizzakh"), ("джизак", "Jizzakh"),
    ("navoiy", "Navoi"), ("navoi", "Navoi"), ("навои", "Navoi"),
    ("guliston", "Gulistan"), ("гулистан", "Gulistan"),
    ("chirchiq", "Chirchiq"), ("chirchik", "Chirchiq"), ("чирчик", "Chirchiq"),
    ("angren", "Angren"), ("olmaliq", "Almalyk"), ("almalyk", "Almalyk"),
    ("kokand", "Kokand"), ("qo'qon", "Kokand"), ("qoqon", "Kokand"),
    ("margilon", "Margilan"), ("marg'ilon", "Margilan"),
    ("shahrisabz", "Shahrisabz"),
    ("o'zbekiston", "Uzbekistan"), ("ozbekiston", "Uzbekistan"), ("uzbekistan", "Uzbekistan"), ("узбекистан", "Uzbekistan"),
]


def extract_default_location(text: "str | None") -> "str | None":
    """Profil javobidan (masalan "20-40 yosh, ko'proq ayollar, Toshkent
    shahri") shahar/hudud nomini ajratib oladi. Topilmasa `None` -- planner
    bu holda hududni foydalanuvchidan so'raydi (`missing_questions`).
    Bir nechta joy bo'lsa BIRINCHI uchragani (matn tartibida) qaytadi."""
    if not text:
        return None
    lowered = text.lower()
    best: "tuple[int, str] | None" = None
    for needle, canonical in _KNOWN_LOCATIONS:
        idx = lowered.find(needle)
        if idx >= 0 and (best is None or idx < best[0]):
            best = (idx, canonical)
    return best[1] if best else None


def _recent_performance(session, company_id: "int | None") -> dict:
    """So'nggi 30 kundagi lead'lar soni va eng ko'p lead bergan 3 ta reklama
    (`Lead.ad_name` bo'yicha). Arzon so'rov; xato bo'lsa (masalan jadval
    hali yo'q, sessiya berilmagan) -- bo'sh natija, kontekst qurish HECH
    QACHON shu sabab yiqilmasligi kerak."""
    if session is None or company_id is None:
        return {"leads_last_30d": None, "top_ads": []}
    try:
        from sqlalchemy import func
        import db as db_module
        since = dt.datetime.utcnow() - dt.timedelta(days=30)
        with db_module.scoped_as(company_id):
            total = (
                session.query(func.count(db_module.Lead.id))
                .filter(db_module.Lead.company_id == company_id, db_module.Lead.created_at >= since)
                .scalar()
            ) or 0
            rows = (
                session.query(db_module.Lead.ad_name, func.count(db_module.Lead.id).label("n"))
                .filter(
                    db_module.Lead.company_id == company_id,
                    db_module.Lead.created_at >= since,
                    db_module.Lead.ad_name.isnot(None),
                )
                .group_by(db_module.Lead.ad_name)
                .order_by(func.count(db_module.Lead.id).desc())
                .limit(3)
                .all()
            )
        return {
            "leads_last_30d": int(total),
            "top_ads": [{"ad_name": r[0], "leads": int(r[1])} for r in rows],
        }
    except Exception as e:  # noqa: BLE001 -- kontekst uchun ixtiyoriy ma'lumot, log + davom
        logger.info("company_context: natija xulosasi olinmadi (company=%s): %s", company_id, e)
        return {"leads_last_30d": None, "top_ads": []}


def build_company_context(company, session=None) -> dict:
    """AI rejalashtiruvchi/chat-tahrirlovchi oladigan YAGONA kontekst lug'ati.
    `company` -- `db.Company` (yoki `None` -- bo'sh kontekst). `session`
    berilsa, so'nggi natijalar (lead soni) ham qo'shiladi.

    Qaytariladigan kalitlar: company_id, company_name, plan,
    business_category {key,label}, business_category_note, profile_answers
    {barcha savol kalitlari}, profile_summary_text, meta_assets {ad_account_id,
    page_id, ig_business_id, pixel_id, business_id, has_* bool'lar},
    default_location, recent_performance, missing_fields."""
    if company is None:
        return {
            "company_id": None, "company_name": "", "phone": None, "plan": None,
            "business_category": {"key": None, "label": None}, "business_category_note": "",
            "profile_answers": {k: "" for k, *_ in business_profile.BUSINESS_PROFILE_QUESTIONS},
            "profile_summary_text": None,
            "meta_assets": {"ad_account_id": None, "page_id": None, "ig_business_id": None, "pixel_id": None, "business_id": None,
                            "has_ad_account": False, "has_page": False, "has_instagram": False, "has_pixel": False, "has_business": False},
            "default_location": None,
            "recent_performance": {"leads_last_30d": None, "top_ads": []},
            "missing_fields": list(PLANNER_WANTED_FIELDS),
        }

    answers = business_profile.parse_business_profile_answers(getattr(company, "business_profile_answers", None))
    profile_answers = {k: (answers.get(k) or "").strip() for k, *_ in business_profile.BUSINESS_PROFILE_QUESTIONS}
    category_key = getattr(company, "business_category", None) or None
    default_location = extract_default_location(profile_answers.get("target_audience")) or extract_default_location(profile_answers.get("extra_notes"))

    ad_account_id = getattr(company, "meta_ad_account_id", None) or None
    page_id = getattr(company, "meta_page_id", None) or None
    ig_id = getattr(company, "ig_business_id", None) or None
    pixel_id = getattr(company, "meta_pixel_id", None) or None
    business_id = getattr(company, "meta_business_id", None) or None

    missing = []
    if not category_key:
        missing.append("business_category")
    for key in ("product_or_service", "target_audience", "price_range"):
        if not profile_answers.get(key):
            missing.append(key)
    if not default_location:
        missing.append("location")

    return {
        "company_id": getattr(company, "id", None),
        "company_name": getattr(company, "name", "") or "",
        "phone": (getattr(company, "phone", None) or "").strip() or None,
        "plan": getattr(company, "plan", None),
        "business_category": {"key": category_key, "label": business_profile.category_label(category_key)},
        "business_category_note": (getattr(company, "business_category_note", None) or "").strip(),
        "profile_answers": profile_answers,
        "profile_summary_text": business_profile.business_profile_summary_text(company),
        "meta_assets": {
            "ad_account_id": ad_account_id, "page_id": page_id, "ig_business_id": ig_id,
            "pixel_id": pixel_id, "business_id": business_id,
            "has_ad_account": bool(ad_account_id), "has_page": bool(page_id),
            "has_instagram": bool(ig_id), "has_pixel": bool(pixel_id), "has_business": bool(business_id),
        },
        "default_location": default_location,
        "recent_performance": _recent_performance(session, getattr(company, "id", None)),
        "missing_fields": missing,
    }


def company_context_prompt_block(ctx: dict) -> str:
    """`build_company_context()` natijasidan promptga qo'shiladigan ixcham
    o'zbekcha matn. Sarlavha ATAYLAB qat'iy: AI umumiy javob bermasligi,
    HAR DOIM shu kompaniyadan kelib chiqishi kerak."""
    ctx = ctx or {}
    lines = ["# KOMPANIYA KONTEKSTI (HAR DOIM shundan kelib chiq, umumiy javob berma)"]
    lines.append(f"- Kompaniya: {ctx.get('company_name') or '(nomsiz)'}")
    cat = ctx.get("business_category") or {}
    if cat.get("label"):
        note = ctx.get("business_category_note") or ""
        lines.append(f"- Biznes yo'nalishi: {cat['label']}" + (f" — {note}" if note else ""))
    elif ctx.get("business_category_note"):
        lines.append(f"- Qo'shimcha izoh: {ctx['business_category_note']}")
    labels = {k: label for k, label, *_ in business_profile.BUSINESS_PROFILE_QUESTIONS}
    for key, value in (ctx.get("profile_answers") or {}).items():
        if value:
            lines.append(f"- {labels.get(key, key)} {value}")
    if ctx.get("default_location"):
        lines.append(f"- Standart hudud (profil bo'yicha): {ctx['default_location']}")
    if ctx.get("phone"):
        lines.append(f"- Kompaniya telefoni: {ctx['phone']}")
    assets = ctx.get("meta_assets") or {}
    asset_bits = []
    asset_bits.append("reklama hisobi " + ("ULANGAN" if assets.get("has_ad_account") else "YO'Q"))
    asset_bits.append("Facebook sahifa " + ("ULANGAN" if assets.get("has_page") else "YO'Q"))
    asset_bits.append("Instagram " + ("ULANGAN" if assets.get("has_instagram") else "YO'Q"))
    asset_bits.append("Pixel " + ("BOR" if assets.get("has_pixel") else "YO'Q"))
    lines.append("- Meta aktivlari: " + ", ".join(asset_bits))
    perf = ctx.get("recent_performance") or {}
    if perf.get("leads_last_30d") is not None:
        top = ", ".join(f"{a['ad_name']} ({a['leads']})" for a in perf.get("top_ads") or [])
        lines.append(f"- So'nggi 30 kun: {perf['leads_last_30d']} ta lead" + (f"; eng yaxshi reklamalar: {top}" if top else ""))
    missing = ctx.get("missing_fields") or []
    if missing:
        lines.append("- TO'LDIRILMAGAN profil maydonlari: " + ", ".join(missing) + " (taxmin qilma -- kerak bo'lsa savol ber)")
    return "\n".join(lines)
