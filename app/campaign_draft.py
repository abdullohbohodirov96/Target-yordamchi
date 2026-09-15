"""campaign_draft.py — Meta Ads Autopilot: kampaniya qoralamasining KANONIK
HOLATI, ruxsat etilgan maydonlar (allowlist), tekshiruv (validation), patch
qo'llash, tasdiq (approval) qoidalari va Meta Graph API'ga tarjima.

2026-09, foydalanuvchi so'rovi: "Replix ... Ads Manager'dagi har bir
maydonni boshidan o'zi to'ldirmasligi kerak" -- foydalanuvchi bir nechta
savolga javob beradi, AI to'liq Campaign -> Ad Set -> Ad rejasini tuzadi,
keyin foydalanuvchi qo'lda forma orqali YOKI chat buyrug'i ("Yoshni 25-50
qil", "Faqat Toshkent qil", "Budjetni 200 ming qil") orqali o'zgartiradi.

ASOSIY PRINSIPLAR (bu modul shularni kafolatlaydi):
  1. BITTA kanonik JSON holat (`new_empty_state()` sxemasi) -- forma ham,
     AI ham, import ham AYNAN SHU holat ustida ishlaydi.
  2. Faqat `ALLOWED_PATHS`dagi maydonlar o'zgartiriladi -- AI (LLM) chiqishi
     HECH QACHON to'g'ridan-to'g'ri Meta'ga/bazaga tushmaydi, avval
     `apply_patch()` (allowlist + tip tekshiruvi), keyin `validate_state()`.
  3. Kampaniya/Ad Set/Ad ALOHIDA tasdiqlanadi; darajadagi har qanday
     o'zgarish o'sha darajaning tasdig'ini bekor qiladi
     (`approvals_after_change()`).
  4. Meta ID/targeting HECH QACHON uydirma emas: geo/qiziqishlar faqat Meta
     qidiruv endpoint'lari orqali (`ai_campaign_planner`dagi callback'lar);
     bu modul faqat TAYYOR key/id'larni Graph API spec'iga tarjima qiladi
     (`to_meta_targeting()`, `to_meta_creative_spec()`).
  5. `USER_OVERRIDDEN` manbali maydon keyingi AI qayta-rejasida JIMGINA
     qayta yozilmaydi (`ai_campaign_planner.replan_preserving_overrides`).

Meta API tafsilotlari Meta v21 hujjati bo'yicha yozilgan; Meta rad etsa
`meta_publish.friendly_publish_error()` foydalanuvchiga tushunarli xato
ko'rsatadi (xom API matni ekranga chiqmaydi).
"""

import copy
import json
import datetime as dt

STATE_VERSION = 1

# ---------------------------------------------------------------------------
# MAQSADLAR (objective) -- foydalanuvchi ko'radigan 7 ta oddiy tanlov va
# ularning Meta'dagi RASMIY (ODAX) ekvivalenti. LLM'ga HECH QACHON
# `meta_objective`/`optimization_goal` tanlatilmaydi -- shu jadvaldan
# deterministik olinadi (`ai_campaign_planner` post-process qadami).
# ---------------------------------------------------------------------------
OBJECTIVES = ["MESSAGES", "LEADS", "SALES", "TRAFFIC", "CALLS", "AWARENESS", "ENGAGEMENT"]

OBJECTIVE_LABELS = {
    "MESSAGES": "Xabarlar (Instagram Direct / Messenger / WhatsApp)",
    "LEADS": "Lidlar (Instant Form)",
    "SALES": "Sotuvlar (sayt / Pixel)",
    "TRAFFIC": "Saytga trafik",
    "CALLS": "Qo'ng'iroqlar",
    "AWARENESS": "Brend tanilishi (reach)",
    "ENGAGEMENT": "Post bilan aloqa (engagement)",
}

OBJECTIVE_META = {
    "MESSAGES": {"meta_objective": "OUTCOME_ENGAGEMENT", "optimization_goal": "CONVERSATIONS", "billing_event": "IMPRESSIONS",
                 "destination_types": ["MESSENGER", "INSTAGRAM_DIRECT", "WHATSAPP"], "default_cta": "SEND_MESSAGE"},
    "LEADS": {"meta_objective": "OUTCOME_LEADS", "optimization_goal": "LEAD_GENERATION", "billing_event": "IMPRESSIONS",
              "destination_types": ["ON_AD"], "default_cta": "SIGN_UP"},
    "SALES": {"meta_objective": "OUTCOME_SALES", "optimization_goal": "OFFSITE_CONVERSIONS", "billing_event": "IMPRESSIONS",
              "destination_types": ["WEBSITE"], "default_cta": "SHOP_NOW", "requires_pixel": True},
    "TRAFFIC": {"meta_objective": "OUTCOME_TRAFFIC", "optimization_goal": "LINK_CLICKS", "billing_event": "IMPRESSIONS",
                "destination_types": ["WEBSITE"], "default_cta": "LEARN_MORE"},
    "CALLS": {"meta_objective": "OUTCOME_LEADS", "optimization_goal": "QUALITY_CALL", "billing_event": "IMPRESSIONS",
              "destination_types": ["PHONE_CALL"], "default_cta": "CALL_NOW"},
    "AWARENESS": {"meta_objective": "OUTCOME_AWARENESS", "optimization_goal": "REACH", "billing_event": "IMPRESSIONS",
                  "destination_types": [], "default_cta": "LEARN_MORE"},
    "ENGAGEMENT": {"meta_objective": "OUTCOME_ENGAGEMENT", "optimization_goal": "POST_ENGAGEMENT", "billing_event": "IMPRESSIONS",
                   "destination_types": [], "default_cta": "LEARN_MORE"},
}

# Foydalanuvchi ko'radigan CTA tanlovlari. `SEND_MESSAGE` -- UI'da qulay nom,
# Meta API'ga `MESSAGE_PAGE` sifatida yuboriladi (`CTA_API_MAP`).
CTA_TYPES = [
    "LEARN_MORE", "SEND_MESSAGE", "SIGN_UP", "SHOP_NOW", "CONTACT_US", "GET_QUOTE",
    "BOOK_TRAVEL", "CALL_NOW", "ORDER_NOW", "APPLY_NOW", "SUBSCRIBE", "WHATSAPP_MESSAGE",
    "GET_OFFER", "DOWNLOAD",
]
CTA_API_MAP = {"SEND_MESSAGE": "MESSAGE_PAGE"}
CTA_LABELS = {
    "LEARN_MORE": "Batafsil", "SEND_MESSAGE": "Xabar yuborish", "SIGN_UP": "Ro'yxatdan o'tish",
    "SHOP_NOW": "Xarid qilish", "CONTACT_US": "Bog'lanish", "GET_QUOTE": "Narx so'rash",
    "BOOK_TRAVEL": "Bron qilish", "CALL_NOW": "Qo'ng'iroq qilish", "ORDER_NOW": "Buyurtma berish",
    "APPLY_NOW": "Ariza berish", "SUBSCRIBE": "Obuna bo'lish", "WHATSAPP_MESSAGE": "WhatsApp'da yozish",
    "GET_OFFER": "Taklifni olish", "DOWNLOAD": "Yuklab olish",
}

PLACEMENT_OPTIONS = {
    "publisher_platforms": ["facebook", "instagram", "messenger", "audience_network"],
    "facebook_positions": ["feed", "story", "reels", "marketplace", "video_feeds", "search"],
    "instagram_positions": ["stream", "story", "reels", "explore"],
}

OPTIMIZATION_GOALS = {
    "CONVERSATIONS", "LEAD_GENERATION", "OFFSITE_CONVERSIONS", "LINK_CLICKS", "QUALITY_CALL",
    "REACH", "POST_ENGAGEMENT", "IMPRESSIONS", "LANDING_PAGE_VIEWS", "QUALITY_LEAD",
}
BID_STRATEGIES = {"LOWEST_COST_WITHOUT_CAP", "LOWEST_COST_WITH_BID_CAP", "COST_CAP"}
DESTINATION_TYPES = {"MESSENGER", "INSTAGRAM_DIRECT", "WHATSAPP", "ON_AD", "WEBSITE", "PHONE_CALL", "APP", "ON_POST", "ON_VIDEO", "ON_PAGE", "ON_EVENT"}
GENDERS = {1, 2}  # Meta: 1 = erkak, 2 = ayol; bo'sh ro'yxat = hammasi
# 2026-09, foydalanuvchi so'rovi ("ads menejerda instant forum yaratayotganingda
# to'liq hali bor... multiplay choice bor va boshqalar... shularni hammasini
# to'liq qil"): avval FAQAT 4 ta tur (FULL_NAME/PHONE/EMAIL/CUSTOM) bor edi --
# haqiqiy Meta Ads Manager'dagi standart savol maydonlari TO'LIQ ro'yxati bilan
# kengaytirildi, VA "CUSTOM" savoliga endi ixtiyoriy `options` (bir nechta
# variant -- multiple choice) qo'shish mumkin (pastda `coerce_value`/
# `_validate_lead_form`/`lead_form_config_from_state`ga qarang). Tartib
# `LEAD_QUESTION_TYPES_ORDER` -- UI'da "qo'shish" tanlovi shu tartibda chiqadi.
LEAD_QUESTION_TYPES_ORDER = [
    "FULL_NAME", "FIRST_NAME", "LAST_NAME", "EMAIL", "PHONE",
    "CITY", "STATE", "COUNTRY", "ZIP", "STREET_ADDRESS",
    "DOB", "GENDER", "MARITAL_STATUS", "RELATIONSHIP_STATUS", "MILITARY_STATUS",
    "COMPANY_NAME", "JOB_TITLE", "WORK_EMAIL", "WORK_PHONE_NUMBER",
    "CUSTOM",
]
LEAD_QUESTION_TYPES = set(LEAD_QUESTION_TYPES_ORDER)
LEAD_QUESTION_TYPE_LABELS = {
    "FULL_NAME": "To'liq ism", "FIRST_NAME": "Ism", "LAST_NAME": "Familiya",
    "EMAIL": "Email", "PHONE": "Telefon", "CITY": "Shahar", "STATE": "Viloyat",
    "COUNTRY": "Davlat", "ZIP": "Pochta indeksi", "STREET_ADDRESS": "Manzil",
    "DOB": "Tug'ilgan sana", "GENDER": "Jinsi", "MARITAL_STATUS": "Oilaviy holati",
    "RELATIONSHIP_STATUS": "Munosabat holati", "MILITARY_STATUS": "Harbiy holati",
    "COMPANY_NAME": "Kompaniya nomi", "JOB_TITLE": "Lavozim",
    "WORK_EMAIL": "Ish emaili", "WORK_PHONE_NUMBER": "Ish telefoni",
    "CUSTOM": "Maxsus savol",
}
SPECIAL_AD_CATEGORIES = {"NONE", "HOUSING", "EMPLOYMENT", "CREDIT", "ISSUES_ELECTIONS_POLITICS", "FINANCIAL_PRODUCTS_SERVICES"}

# Meta byudjeti eng kichik valyuta birligida (tiyin/sent) yuboriladi. Ko'p
# valyutada 1 birlik = 100 kichik birlik; quyidagilar uchun esa Meta
# "offset" 1 (kasr qismi yo'q). UZS -- Meta hujjati bo'yicha 100.
CURRENCY_OFFSETS = {"JPY": 1, "KRW": 1, "VND": 1, "CLP": 1, "ISK": 1, "PYG": 1, "TWD": 1, "XOF": 1, "XAF": 1}

# Ads Manager'da bor, lekin biz ATAYLAB faqat o'qish uchun ko'rsatadigan
# (Meta Marketing API orqali ishonchli boshqarib bo'lmaydigan yoki v1'ga
# kiritilmagan) sozlamalar -- UI "Meta API orqali boshqarilmaydi" deb ko'rsatadi.
META_UNSUPPORTED_UI_FIELDS = [
    {"key": "advantage_plus_creative", "label": "Advantage+ creative enhancements", "note": "Meta API orqali boshqarilmaydi"},
    {"key": "ab_test", "label": "A/B test setup", "note": "Meta API orqali boshqarilmaydi"},
    {"key": "cbo", "label": "Campaign-level budget (CBO) — v1", "note": "Meta API orqali boshqarilmaydi"},
    {"key": "dynamic_creative", "label": "Dynamic creative", "note": "Meta API orqali boshqarilmaydi"},
]


class DraftPatchError(Exception):
    """Ruxsat etilmagan yo'l (path) yoki noto'g'ri tipdagi qiymat -- xabar
    matni o'zbekcha, foydalanuvchiga to'g'ridan-to'g'ri ko'rsatsa bo'ladi."""


# ---------------------------------------------------------------------------
# VALYUTA
# ---------------------------------------------------------------------------

def to_minor_units(amount: "float | int | None", currency: "str | None") -> int:
    """Byudjetni Meta kutadigan eng kichik birlikka o'tkazadi
    (masalan 200000 UZS -> 20000000; 10 USD -> 1000; 1000 JPY -> 1000)."""
    if amount is None:
        return 0
    mult = CURRENCY_OFFSETS.get((currency or "").upper(), 100)
    return int(round(float(amount) * mult))


def from_minor_units(minor: "int | str | None", currency: "str | None") -> float:
    """`to_minor_units()`ning teskarisi -- Meta'dan o'qilgan `daily_budget`ni
    odam o'qiydigan summaga qaytaradi."""
    if minor in (None, ""):
        return 0.0
    mult = CURRENCY_OFFSETS.get((currency or "").upper(), 100)
    return float(minor) / mult


# ---------------------------------------------------------------------------
# KANONIK HOLAT
# ---------------------------------------------------------------------------

def new_empty_state(objective: str = "MESSAGES") -> dict:
    """Berilgan maqsad uchun BO'SH, lekin to'liq sxemali holat -- barcha
    kalitlar mavjud (UI/AI `None` bilan ishlaydi, `KeyError` bo'lmaydi).
    `meta_objective`/`optimization_goal`/`destination_type`/`cta`
    `OBJECTIVE_META`dan deterministik olinadi."""
    objective = (objective or "MESSAGES").upper()
    if objective not in OBJECTIVE_META:
        raise DraftPatchError(f"Noma'lum maqsad: {objective}. Ruxsat etilganlar: {', '.join(OBJECTIVES)}")
    meta = OBJECTIVE_META[objective]
    return {
        "version": STATE_VERSION,
        "objective": objective,
        "campaign": {
            "name": "",
            "meta_objective": meta["meta_objective"],
            "buying_type": "AUCTION",
            "special_ad_categories": [],
            "status": "PAUSED",
            "budget_mode": "ADSET",
        },
        "adset": {
            "name": "",
            "budget_type": "daily",
            "daily_budget": None,
            "lifetime_budget": None,
            "currency": "UZS",
            "start_time": None,
            "end_time": None,
            "duration_days": None,
            "optimization_goal": meta["optimization_goal"],
            "billing_event": meta["billing_event"],
            "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
            "destination_type": meta["destination_types"][0] if meta["destination_types"] else None,
            "targeting": {
                "geo_locations": {"countries": [], "cities": [], "regions": []},
                "age_min": 18,
                "age_max": 65,
                "genders": [],
                "locales": [],
                "interests": [],
                "behaviors": [],
                "custom_audiences": [],
                "excluded_custom_audiences": [],
                "advantage_audience": True,
                "placements": {"mode": "automatic", "publisher_platforms": [], "facebook_positions": [], "instagram_positions": []},
            },
        },
        "ad": {
            "name": "",
            "page_id": None,
            "instagram_actor_id": None,
            "media": {"media_id": None, "image_hash": None, "video_id": None, "selected_variant": None},
            "primary_text": "",
            "headline": "",
            "description": "",
            "cta": meta["default_cta"],
            "link_url": None,
            "display_link": None,
            "url_tags": None,
            "copy_variants": {"primary_text": [], "headline": [], "description": []},
            "messages": {"greeting": "", "quick_replies": []},
            "lead_form": {
                "mode": "new",
                "existing_form_id": None,
                "new_form": {
                    "name": "", "intro_headline": "", "intro_description": "",
                    "questions": [], "privacy_url": "", "thank_you_title": "", "thank_you_body": "",
                },
            },
        },
    }


def apply_objective_defaults(state: dict) -> dict:
    """Maqsad o'zgarganda (yoki AI rejasidan keyin) `OBJECTIVE_META`dan
    kelib chiqadigan HOSILA maydonlarni majburan to'g'rilaydi -- LLM yoki
    foydalanuvchi `meta_objective`ni noto'g'ri qo'ya olmaydi."""
    objective = (state.get("objective") or "MESSAGES").upper()
    meta = OBJECTIVE_META.get(objective) or OBJECTIVE_META["MESSAGES"]
    state["objective"] = objective
    state.setdefault("campaign", {})["meta_objective"] = meta["meta_objective"]
    state["campaign"]["buying_type"] = "AUCTION"
    state["campaign"]["status"] = "PAUSED"
    adset = state.setdefault("adset", {})
    adset["optimization_goal"] = meta["optimization_goal"]
    adset["billing_event"] = meta["billing_event"]
    allowed_dest = meta["destination_types"]
    if allowed_dest:
        if adset.get("destination_type") not in allowed_dest:
            adset["destination_type"] = allowed_dest[0]
    else:
        adset["destination_type"] = None
    ad = state.setdefault("ad", {})
    if ad.get("cta") not in CTA_TYPES:
        ad["cta"] = meta["default_cta"]
    return state


# ---------------------------------------------------------------------------
# ALLOWLIST -- har bir ruxsat etilgan yo'l va uning tipi. Bu yerda YO'Q
# yo'l (masalan `campaign.status`, `campaign.meta_objective`, `version`,
# `adset.billing_event`, `campaign.buying_type`) NA foydalanuvchi, NA AI
# tomonidan o'zgartirilmaydi.
# Tip belgilari: str | int | float | bool | iso | list_str | list_int |
#   list_dict | dict | enum:<A,B,C> | int_or_none | str_or_none
# ---------------------------------------------------------------------------
PATH_TYPES: dict[str, str] = {
    "objective": "enum:" + ",".join(OBJECTIVES),
    "campaign.name": "str",
    "campaign.special_ad_categories": "list_str",
    "campaign.budget_mode": "enum:ADSET",
    "adset.name": "str",
    "adset.budget_type": "enum:daily,lifetime",
    "adset.daily_budget": "float_or_none",
    "adset.lifetime_budget": "float_or_none",
    "adset.currency": "str",
    "adset.start_time": "iso_or_none",
    "adset.end_time": "iso_or_none",
    "adset.duration_days": "int_or_none",
    "adset.optimization_goal": "enum:" + ",".join(sorted(OPTIMIZATION_GOALS)),
    "adset.bid_strategy": "enum:" + ",".join(sorted(BID_STRATEGIES)),
    "adset.destination_type": "enum_or_none:" + ",".join(sorted(DESTINATION_TYPES)),
    "adset.targeting.geo_locations": "dict",
    "adset.targeting.geo_locations.countries": "list_str",
    "adset.targeting.geo_locations.cities": "list_dict",
    "adset.targeting.geo_locations.regions": "list_dict",
    "adset.targeting.age_min": "int",
    "adset.targeting.age_max": "int",
    "adset.targeting.genders": "list_int",
    "adset.targeting.locales": "list_int",
    "adset.targeting.interests": "list_dict",
    "adset.targeting.behaviors": "list_dict",
    "adset.targeting.custom_audiences": "list_dict",
    "adset.targeting.excluded_custom_audiences": "list_dict",
    "adset.targeting.advantage_audience": "bool",
    "adset.targeting.placements": "dict",
    "adset.targeting.placements.mode": "enum:automatic,manual",
    "adset.targeting.placements.publisher_platforms": "list_str",
    "adset.targeting.placements.facebook_positions": "list_str",
    "adset.targeting.placements.instagram_positions": "list_str",
    "ad.name": "str",
    "ad.page_id": "str_or_none",
    "ad.instagram_actor_id": "str_or_none",
    "ad.media": "dict",
    "ad.media.media_id": "int_or_none",
    "ad.media.image_hash": "str_or_none",
    "ad.media.video_id": "str_or_none",
    "ad.media.selected_variant": "int_or_none",
    "ad.primary_text": "str",
    "ad.headline": "str",
    "ad.description": "str",
    "ad.cta": "enum:" + ",".join(CTA_TYPES),
    "ad.link_url": "str_or_none",
    "ad.display_link": "str_or_none",
    "ad.url_tags": "str_or_none",
    "ad.copy_variants": "dict",
    "ad.copy_variants.primary_text": "list_str",
    "ad.copy_variants.headline": "list_str",
    "ad.copy_variants.description": "list_str",
    "ad.messages": "dict",
    "ad.messages.greeting": "str",
    "ad.messages.quick_replies": "list_str",
    "ad.lead_form": "dict",
    "ad.lead_form.mode": "enum:existing,new",
    "ad.lead_form.existing_form_id": "str_or_none",
    "ad.lead_form.new_form": "dict",
    "ad.lead_form.new_form.name": "str",
    "ad.lead_form.new_form.intro_headline": "str",
    "ad.lead_form.new_form.intro_description": "str",
    "ad.lead_form.new_form.questions": "list_dict",
    "ad.lead_form.new_form.privacy_url": "str",
    "ad.lead_form.new_form.thank_you_title": "str",
    "ad.lead_form.new_form.thank_you_body": "str",
}
ALLOWED_PATHS = frozenset(PATH_TYPES.keys())

# Dict-tipidagi yo'llar uchun ichida ruxsat etilgan kalitlar (butun dict
# almashtirilganda begona kalit kirib qolmasligi uchun).
_DICT_CHILD_KEYS = {
    "adset.targeting.geo_locations": ["countries", "cities", "regions"],
    "adset.targeting.placements": ["mode", "publisher_platforms", "facebook_positions", "instagram_positions"],
    "ad.media": ["media_id", "image_hash", "video_id", "selected_variant"],
    "ad.copy_variants": ["primary_text", "headline", "description"],
    "ad.messages": ["greeting", "quick_replies"],
    "ad.lead_form": ["mode", "existing_form_id", "new_form"],
    "ad.lead_form.new_form": ["name", "intro_headline", "intro_description", "questions", "privacy_url", "thank_you_title", "thank_you_body"],
}

_SCOPES = ("campaign", "adset", "ad", "objective")


def is_allowed_path(path: str) -> bool:
    return isinstance(path, str) and path in ALLOWED_PATHS


def scope_of_path(path: str) -> str:
    """`campaign.name` -> "campaign", `objective` -> "objective"."""
    if path == "objective":
        return "objective"
    return path.split(".", 1)[0]


def _parse_iso(value) -> "str | None":
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        return value.replace(microsecond=0).isoformat()
    if not isinstance(value, str):
        raise DraftPatchError("Sana ISO formatda bo'lishi kerak (masalan 2026-09-20T09:00:00).")
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = dt.datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError as e:
            raise DraftPatchError(f"Sana tushunarsiz: {value}") from e
    return parsed.replace(microsecond=0).isoformat()


def _to_int(value, path: str) -> int:
    if isinstance(value, bool):
        raise DraftPatchError(f"{path}: butun son kutilgan edi.")
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    raise DraftPatchError(f"{path}: butun son kutilgan edi, kelgani: {value!r}")


def _to_float(value, path: str) -> float:
    if isinstance(value, bool):
        raise DraftPatchError(f"{path}: son kutilgan edi.")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(" ", "").replace(",", ".")
        try:
            return float(cleaned)
        except ValueError:
            pass
    raise DraftPatchError(f"{path}: son kutilgan edi, kelgani: {value!r}")


def coerce_value(path: str, value):
    """Yo'lning tipiga qarab qiymatni tozalaydi/tekshiradi. Noto'g'ri bo'lsa
    `DraftPatchError` (o'zbekcha). `dict` tipidagi yo'llar uchun faqat
    ruxsat etilgan ichki kalitlar qoladi va har biri rekursiv tekshiriladi."""
    spec = PATH_TYPES.get(path)
    if spec is None:
        raise DraftPatchError(f"Bu maydonni o'zgartirib bo'lmaydi: {path}")
    if spec == "str":
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return value.strip()
    if spec == "str_or_none":
        if value in (None, ""):
            return None
        return str(value).strip()
    if spec == "int":
        return _to_int(value, path)
    if spec == "int_or_none":
        return None if value in (None, "") else _to_int(value, path)
    if spec == "float_or_none":
        return None if value in (None, "") else _to_float(value, path)
    if spec == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "ha", "yes", "on")
        raise DraftPatchError(f"{path}: ha/yo'q qiymati kutilgan edi.")
    if spec == "iso_or_none":
        return _parse_iso(value)
    if spec.startswith("enum_or_none:"):
        if value in (None, ""):
            return None
        spec = "enum:" + spec.split(":", 1)[1]
    if spec.startswith("enum:"):
        options = spec.split(":", 1)[1].split(",")
        text = str(value).strip()
        if path == "objective" or path.endswith(".cta") or path.endswith("_type") or path.endswith("_goal") or path.endswith("_strategy"):
            text = text.upper()
        if path.endswith("placements.mode") or path.endswith("budget_type") or path.endswith("lead_form.mode"):
            text = text.lower()
        # CTA uchun API nomi (MESSAGE_PAGE) kelsa ham UI nomiga (SEND_MESSAGE) qaytaramiz
        if path.endswith(".cta") and text == "MESSAGE_PAGE":
            text = "SEND_MESSAGE"
        if text not in options:
            raise DraftPatchError(f"{path}: ruxsat etilmagan qiymat '{value}'. Variantlar: {', '.join(options)}")
        return text
    if spec == "list_str":
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            raise DraftPatchError(f"{path}: ro'yxat kutilgan edi.")
        return [str(v).strip() for v in value if v is not None and str(v).strip()]
    if spec == "list_int":
        if value is None:
            return []
        if not isinstance(value, list):
            value = [value]
        return [_to_int(v, path) for v in value]
    if spec == "list_dict":
        if value is None:
            return []
        if not isinstance(value, list):
            raise DraftPatchError(f"{path}: ro'yxat kutilgan edi.")
        out = []
        for item in value:
            if not isinstance(item, dict):
                raise DraftPatchError(f"{path}: ro'yxat elementlari obyekt bo'lishi kerak.")
            out.append(_coerce_list_item(path, item))
        return out
    if spec == "dict":
        if not isinstance(value, dict):
            raise DraftPatchError(f"{path}: obyekt kutilgan edi.")
        allowed_children = _DICT_CHILD_KEYS.get(path, [])
        out = {}
        for child in allowed_children:
            if child in value:
                out[child] = coerce_value(f"{path}.{child}", value[child])
        return out
    raise DraftPatchError(f"{path}: noma'lum tip.")


def _coerce_list_item(path: str, item: dict) -> dict:
    """Ro'yxat elementlarini (shahar, qiziqish, auditoriya, forma savoli)
    faqat KUTILGAN kalitlar bilan qoldiradi -- Meta'ga ortiqcha maydon
    ketmasligi va uydirma tuzilma kirmasligi uchun."""
    if path.endswith("geo_locations.cities"):
        if not item.get("key"):
            raise DraftPatchError("Shahar uchun Meta `key` kerak (nom bilan emas -- geo qidiruv orqali aniqlanadi).")
        radius = item.get("radius")
        out = {"key": str(item["key"]), "name": str(item.get("name") or "")}
        if radius not in (None, ""):
            out["radius"] = _to_int(radius, path + ".radius")
            out["distance_unit"] = str(item.get("distance_unit") or "kilometer")
        return out
    if path.endswith("geo_locations.regions"):
        if not item.get("key"):
            raise DraftPatchError("Viloyat uchun Meta `key` kerak.")
        return {"key": str(item["key"]), "name": str(item.get("name") or "")}
    if path.endswith(".interests") or path.endswith(".behaviors") or path.endswith("custom_audiences"):
        if not item.get("id"):
            raise DraftPatchError(f"{path}: har bir element uchun Meta `id` kerak.")
        return {"id": str(item["id"]), "name": str(item.get("name") or "")}
    if path.endswith("new_form.questions"):
        qtype = str(item.get("type") or "CUSTOM").upper()
        if qtype not in LEAD_QUESTION_TYPES:
            raise DraftPatchError(f"Forma savoli turi noto'g'ri: {qtype}")
        out = {"type": qtype}
        if qtype == "CUSTOM":
            label = str(item.get("label") or "").strip()
            if not label:
                raise DraftPatchError("Maxsus (CUSTOM) savol uchun matn (label) kerak.")
            out["label"] = label
            out["key"] = str(item.get("key") or _slug(label))
            # 2026-09: `options` bo'lsa -- bu savol "bir nechta variant"
            # (multiple choice) turida (Ads Manager'dagi kabi). Ro'yxat
            # bo'lmasa (kalit umuman yo'q) -- oddiy erkin matnli CUSTOM savol.
            if "options" in item:
                raw_options = item.get("options") or []
                if not isinstance(raw_options, list):
                    raise DraftPatchError("Variantlar ro'yxat (list) bo'lishi kerak.")
                options = [str(o).strip() for o in raw_options if str(o or "").strip()]
                out["options"] = options
        else:
            if item.get("key"):
                out["key"] = str(item["key"])
            if item.get("label"):
                out["label"] = str(item["label"])
        return out
    return {k: v for k, v in item.items() if isinstance(k, str)}


def _slug(text: str) -> str:
    out = "".join(ch if ch.isalnum() else "_" for ch in text.lower().strip())
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_")[:40] or "savol"


def get_path(state: dict, path: str):
    node = state
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def set_path(state: dict, path: str, value) -> None:
    parts = path.split(".")
    node = state
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value


def _resolve_full_path(scope: str, key: str) -> str:
    """Patch'dagi kalit -- scope'ga NISBATAN ("targeting.age_min") yoki
    TO'LIQ ("adset.targeting.age_min") bo'lishi mumkin; ikkalasini ham
    to'liq yo'lga keltiradi."""
    key = (key or "").strip()
    if scope == "objective":
        return "objective"
    if key == "objective" or key.startswith("campaign.") or key.startswith("adset.") or key.startswith("ad."):
        return key
    return f"{scope}.{key}" if key else scope


def apply_patch(state: dict, patch: dict, *, source: str, field_sources: "dict | None") -> tuple[dict, list[str], dict]:
    """Holatga bitta patch qo'llaydi.

    patch = {"scope": "campaign|adset|ad|objective", "changes": {path: value}}
    Ro'yxat maydonlari (`ad.lead_form.new_form.questions`, `ad.messages.
    quick_replies`, `ad.copy_variants.*`) uchun to'liq almashtirish YOKI
    `{"$append": item}` / `{"$remove_index": i}` operatsiyalari.

    `source` -- "USER_OVERRIDDEN" | "AI_RECOMMENDED" | "META_IMPORTED" --
    har bir o'zgargan yo'l uchun `field_sources`ga yoziladi.

    Qaytaradi: (yangi holat -- CHUQUR NUSXA, o'zgargan yo'llar, yangi
    field_sources). Ruxsat etilmagan yo'l -> `DraftPatchError`, holat
    O'ZGARMAYDI (hammasi yoki hech narsa)."""
    if source not in ("USER_OVERRIDDEN", "AI_RECOMMENDED", "META_IMPORTED"):
        raise DraftPatchError(f"Noma'lum manba: {source}")
    if not isinstance(patch, dict):
        raise DraftPatchError("Patch obyekt bo'lishi kerak.")
    scope = str(patch.get("scope") or "").strip().lower()
    if scope not in _SCOPES:
        raise DraftPatchError(f"Noma'lum daraja (scope): '{scope}'. Ruxsat: campaign, adset, ad, objective")
    changes = patch.get("changes")
    if scope == "objective" and not isinstance(changes, dict):
        changes = {"objective": changes}
    if not isinstance(changes, dict) or not changes:
        raise DraftPatchError("Patch'da o'zgarishlar (changes) yo'q.")

    new_state = copy.deepcopy(state)
    new_sources = dict(field_sources or {})
    changed: list[str] = []

    for raw_key, raw_value in changes.items():
        full = _resolve_full_path(scope, raw_key)
        if not is_allowed_path(full):
            raise DraftPatchError(f"Bu maydonni o'zgartirishga ruxsat yo'q: {full}")
        if scope != "objective" and scope_of_path(full) != scope:
            raise DraftPatchError(f"'{full}' maydoni '{scope}' darajasiga tegishli emas.")
        spec = PATH_TYPES[full]
        if isinstance(raw_value, dict) and spec.startswith("list_") and ("$append" in raw_value or "$remove_index" in raw_value):
            current = list(get_path(new_state, full) or [])
            if "$append" in raw_value:
                appended = coerce_value(full, [raw_value["$append"]])
                current.extend(appended)
            if "$remove_index" in raw_value:
                idx = _to_int(raw_value["$remove_index"], full)
                if idx < 0 or idx >= len(current):
                    raise DraftPatchError(f"{full}: {idx}-element yo'q (jami {len(current)}).")
                current.pop(idx)
            value = coerce_value(full, current)
        else:
            value = coerce_value(full, raw_value)
        if spec == "dict":
            # Butun obyekt: mavjud kalitlarni saqlab, kelganlarini yangilaymiz
            merged = dict(get_path(new_state, full) or {})
            merged.update(value)
            value = merged
            for child_key in value:
                child_path = f"{full}.{child_key}"
                if child_path in ALLOWED_PATHS:
                    new_sources[child_path] = source
        set_path(new_state, full, value)
        new_sources[full] = source
        changed.append(full)

    if "objective" in changed:
        apply_objective_defaults(new_state)
    # Yosh tartibi -- foydalanuvchi "25-50" desa AI ikkalasini bir patch'da
    # yuboradi; alohida yuborilsa ham validate_state ogohlantiradi.
    return new_state, changed, new_sources


def approvals_after_change(draft_row, changed_paths: list[str]) -> list[str]:
    """Tahrir qilingan darajalarning tasdig'ini bekor qiladi:
    `campaign.*` -> campaign_approved=False, `adset.*` -> adset_approved,
    `ad.*` -> ad_approved, `objective` -> uchalasi ham. Qaytaradi: bekor
    qilingan darajalar ro'yxati (audit-jurnal uchun)."""
    reset: list[str] = []
    scopes = {scope_of_path(p) for p in changed_paths or []}
    if "objective" in scopes:
        scopes = {"campaign", "adset", "ad"}
    if "campaign" in scopes and getattr(draft_row, "campaign_approved", False):
        draft_row.campaign_approved = False
        reset.append("campaign")
    if "adset" in scopes and getattr(draft_row, "adset_approved", False):
        draft_row.adset_approved = False
        reset.append("adset")
    if "ad" in scopes and getattr(draft_row, "ad_approved", False):
        draft_row.ad_approved = False
        reset.append("ad")
    return reset


# ---------------------------------------------------------------------------
# TEKSHIRUV (validation) -- barcha xabarlar o'zbekcha, foydalanuvchiga
# to'g'ridan-to'g'ri ko'rsatiladi.
# ---------------------------------------------------------------------------

def _err(scope: str, field: str, message: str) -> dict:
    return {"scope": scope, "field": field, "message": message}


def validate_state(state: dict, *, company=None, meta_assets: "dict | None" = None) -> list[dict]:
    """Holatni sxema qoidalari bo'yicha tekshiradi. `meta_assets` (qarang
    `meta_publish.get_meta_assets`) berilsa, EGALIK tekshiruvi ham
    qilinadi: sahifa/IG/auditoriya haqiqatan shu kompaniyaga ulanganmi.
    Qaytaradi: [{"scope","field","message"}] -- bo'sh ro'yxat = hammasi joyida."""
    errors: list[dict] = []
    state = state or {}
    objective = (state.get("objective") or "").upper()
    if objective not in OBJECTIVE_META:
        errors.append(_err("campaign", "objective", "Kampaniya maqsadi tanlanmagan."))
        return errors
    meta = OBJECTIVE_META[objective]
    campaign = state.get("campaign") or {}
    adset = state.get("adset") or {}
    ad = state.get("ad") or {}
    targeting = adset.get("targeting") or {}

    # --- Campaign
    name = (campaign.get("name") or "").strip()
    if not name:
        errors.append(_err("campaign", "name", "Kampaniya nomi bo'sh."))
    elif len(name) > 255:
        errors.append(_err("campaign", "name", "Kampaniya nomi 255 belgidan oshmasligi kerak."))
    if campaign.get("meta_objective") != meta["meta_objective"]:
        errors.append(_err("campaign", "meta_objective", "Kampaniya maqsadi ichki mosligi buzilgan -- maqsadni qayta tanlang."))
    for cat in campaign.get("special_ad_categories") or []:
        if cat not in SPECIAL_AD_CATEGORIES:
            errors.append(_err("campaign", "special_ad_categories", f"Maxsus reklama toifasi noto'g'ri: {cat}"))

    # --- Ad Set
    adset_name = (adset.get("name") or "").strip()
    if not adset_name:
        errors.append(_err("adset", "name", "Ad Set nomi bo'sh."))
    elif len(adset_name) > 255:
        errors.append(_err("adset", "name", "Ad Set nomi 255 belgidan oshmasligi kerak."))
    budget_type = adset.get("budget_type") or "daily"
    budget = adset.get("daily_budget") if budget_type == "daily" else adset.get("lifetime_budget")
    if budget is None or float(budget) <= 0:
        errors.append(_err("adset", "daily_budget" if budget_type == "daily" else "lifetime_budget", "Byudjet kiritilmagan yoki 0 -- musbat summa kiriting."))
    elif float(budget) < 1:
        errors.append(_err("adset", "daily_budget", "Byudjet juda kichik (kamida 1 birlik)."))
    if not adset.get("currency"):
        errors.append(_err("adset", "currency", "Valyuta aniqlanmagan (reklama hisobidan olinadi)."))
    start = adset.get("start_time")
    end = adset.get("end_time")
    if start and end:
        try:
            if dt.datetime.fromisoformat(str(end)) <= dt.datetime.fromisoformat(str(start)):
                errors.append(_err("adset", "end_time", "Tugash sanasi boshlanish sanasidan keyin bo'lishi kerak."))
        except ValueError:
            errors.append(_err("adset", "start_time", "Sana formati noto'g'ri."))
    if budget_type == "lifetime" and not end:
        errors.append(_err("adset", "end_time", "Umumiy (lifetime) byudjet uchun tugash sanasi shart."))
    if adset.get("optimization_goal") not in OPTIMIZATION_GOALS:
        errors.append(_err("adset", "optimization_goal", "Optimallashtirish maqsadi noto'g'ri."))
    if adset.get("bid_strategy") not in BID_STRATEGIES:
        errors.append(_err("adset", "bid_strategy", "Stavka strategiyasi noto'g'ri."))
    allowed_dest = meta["destination_types"]
    dest = adset.get("destination_type")
    if allowed_dest and dest not in allowed_dest:
        errors.append(_err("adset", "destination_type", f"Bu maqsad uchun yo'nalish (destination) noto'g'ri. Variantlar: {', '.join(allowed_dest)}"))
    ig_present = bool(getattr(company, "ig_business_id", None)) if company is not None else True
    if meta_assets is not None:
        ig_ids = {str(a.get("id")) for a in (meta_assets.get("instagram_accounts") or []) if a.get("id")}
        ig_present = bool(ig_ids) or ig_present
    if objective == "MESSAGES" and dest == "INSTAGRAM_DIRECT" and not ig_present:
        errors.append(_err("adset", "destination_type", "Instagram Direct uchun kompaniyaga Instagram akkaunt ulangan bo'lishi kerak."))

    # --- Targeting
    age_min = targeting.get("age_min")
    age_max = targeting.get("age_max")
    if not isinstance(age_min, int) or not isinstance(age_max, int):
        errors.append(_err("adset", "age", "Yosh oralig'i butun son bo'lishi kerak."))
    else:
        if age_min < 13 or age_max > 65:
            errors.append(_err("adset", "age", "Yosh oralig'i 13 dan 65 gacha bo'lishi mumkin."))
        if age_min > age_max:
            errors.append(_err("adset", "age", "Minimal yosh maksimal yoshdan katta bo'lmasligi kerak."))
    for g in targeting.get("genders") or []:
        if g not in GENDERS:
            errors.append(_err("adset", "genders", "Jins qiymati noto'g'ri (1 -- erkak, 2 -- ayol)."))
    geo = targeting.get("geo_locations") or {}
    if not (geo.get("countries") or geo.get("cities") or geo.get("regions")):
        errors.append(_err("adset", "geo_locations", "Kamida bitta hudud (shahar/viloyat/davlat) tanlang."))
    for city in geo.get("cities") or []:
        if not city.get("key"):
            errors.append(_err("adset", "geo_locations", "Shahar Meta qidiruvi orqali tanlanmagan (key yo'q)."))
    placements = targeting.get("placements") or {}
    if placements.get("mode") == "manual":
        plats = placements.get("publisher_platforms") or []
        if not plats:
            errors.append(_err("adset", "placements", "Qo'lda joylashuv rejimida kamida bitta platforma tanlang."))
        for p in plats:
            if p not in PLACEMENT_OPTIONS["publisher_platforms"]:
                errors.append(_err("adset", "placements", f"Platforma noto'g'ri: {p}"))
        for p in placements.get("facebook_positions") or []:
            if p not in PLACEMENT_OPTIONS["facebook_positions"]:
                errors.append(_err("adset", "placements", f"Facebook joylashuvi noto'g'ri: {p}"))
        for p in placements.get("instagram_positions") or []:
            if p not in PLACEMENT_OPTIONS["instagram_positions"]:
                errors.append(_err("adset", "placements", f"Instagram joylashuvi noto'g'ri: {p}"))
        if "instagram" in plats and not ig_present:
            errors.append(_err("adset", "placements", "Instagram joylashuvi uchun Instagram akkaunt ulangan bo'lishi kerak."))

    # --- Ad
    ad_name = (ad.get("name") or "").strip()
    if not ad_name:
        errors.append(_err("ad", "name", "Reklama nomi bo'sh."))
    elif len(ad_name) > 255:
        errors.append(_err("ad", "name", "Reklama nomi 255 belgidan oshmasligi kerak."))
    if not ad.get("page_id"):
        errors.append(_err("ad", "page_id", "Facebook sahifa tanlanmagan -- reklama sahifa nomidan chiqadi."))
    media = ad.get("media") or {}
    if not (media.get("image_hash") or media.get("video_id")):
        errors.append(_err("ad", "media", "Rasm yoki video yuklanmagan."))
    primary = ad.get("primary_text") or ""
    if not primary.strip():
        errors.append(_err("ad", "primary_text", "Asosiy matn (primary text) bo'sh."))
    elif len(primary) > 1000:
        errors.append(_err("ad", "primary_text", "Asosiy matn 1000 belgidan oshmasligi kerak."))
    headline = ad.get("headline") or ""
    if len(headline) > 125:
        errors.append(_err("ad", "headline", "Sarlavha 125 belgidan oshmasligi kerak."))
    if ad.get("cta") not in CTA_TYPES:
        errors.append(_err("ad", "cta", "Tugma (CTA) turi noto'g'ri."))
    if objective in ("SALES", "TRAFFIC") and not (ad.get("link_url") or "").strip():
        errors.append(_err("ad", "link_url", "Bu maqsad uchun sayt havolasi (link) kerak."))
    if objective == "CALLS" and not (ad.get("link_url") or "").strip().lower().startswith("tel:"):
        # Meta v21: click-to-call reklamada CTA CALL_NOW qiymati `link` = "tel:+998..." bo'lishi kerak
        errors.append(_err("ad", "link_url", "Qo'ng'iroq maqsadi uchun telefon raqami kerak (masalan tel:+998901234567)."))

    # --- Maqsadga xos talablar
    if objective == "SALES":
        pixel = (meta_assets or {}).get("pixel_id") if meta_assets else None
        pixel = pixel or (getattr(company, "meta_pixel_id", None) if company is not None else None)
        if not pixel:
            errors.append(_err("campaign", "pixel", "Sotuv maqsadi uchun Pixel tanlash kerak (Meta Events Manager)."))
    if objective == "LEADS":
        errors.extend(_validate_lead_form(ad.get("lead_form") or {}))
    if objective == "MESSAGES":
        greeting = ((ad.get("messages") or {}).get("greeting") or "").strip()
        if not greeting:
            errors.append(_err("ad", "messages.greeting", "Xabar maqsadi uchun salomlashuv matni (greeting) kerak."))

    # --- Egalik tekshiruvi (meta_assets berilsa)
    if meta_assets:
        page_ids = {str(p.get("id")) for p in (meta_assets.get("pages") or []) if p.get("id")}
        if page_ids and ad.get("page_id") and str(ad["page_id"]) not in page_ids:
            errors.append(_err("ad", "page_id", "Bu sahifa kompaniyaga ulanmagan."))
        ig_ids = {str(a.get("id")) for a in (meta_assets.get("instagram_accounts") or []) if a.get("id")}
        if ad.get("instagram_actor_id") and ig_ids and str(ad["instagram_actor_id"]) not in ig_ids:
            errors.append(_err("ad", "instagram_actor_id", "Bu Instagram akkaunt kompaniyaga ulanmagan."))
        aud_ids = {str(a.get("id")) for a in (meta_assets.get("custom_audiences") or []) if a.get("id")}
        for key in ("custom_audiences", "excluded_custom_audiences"):
            for aud in targeting.get(key) or []:
                if aud_ids and str(aud.get("id")) not in aud_ids:
                    errors.append(_err("adset", key, f"Auditoriya '{aud.get('name') or aud.get('id')}' bu reklama hisobiga tegishli emas."))
        if objective == "LEADS":
            lf = ad.get("lead_form") or {}
            form_ids = {str(f.get("id")) for f in (meta_assets.get("lead_forms") or []) if f.get("id")}
            if lf.get("mode") == "existing" and lf.get("existing_form_id") and form_ids and str(lf["existing_form_id"]) not in form_ids:
                errors.append(_err("ad", "lead_form", "Tanlangan Instant Form bu sahifaga tegishli emas."))
    return errors


def _validate_lead_form(lead_form: dict) -> list[dict]:
    errors: list[dict] = []
    mode = lead_form.get("mode") or "new"
    if mode == "existing":
        if not lead_form.get("existing_form_id"):
            errors.append(_err("ad", "lead_form", "Mavjud Instant Form tanlanmagan."))
        return errors
    form = lead_form.get("new_form") or {}
    if not (form.get("name") or "").strip():
        errors.append(_err("ad", "lead_form.name", "Yangi forma nomi bo'sh."))
    questions = form.get("questions") or []
    if len(questions) < 2:
        errors.append(_err("ad", "lead_form.questions", "Formada kamida 2 ta savol bo'lishi kerak."))
    types = {str(q.get("type") or "").upper() for q in questions}
    if not ({"PHONE", "EMAIL"} & types):
        errors.append(_err("ad", "lead_form.questions", "Formada telefon yoki email savoli bo'lishi shart (aks holda lid bilan bog'lanib bo'lmaydi)."))
    for q in questions:
        if str(q.get("type") or "").upper() != "CUSTOM":
            continue
        if not (q.get("label") or "").strip():
            errors.append(_err("ad", "lead_form.questions", "Maxsus savol matni bo'sh."))
        if "options" in q:
            good_options = [o for o in (q.get("options") or []) if str(o).strip()]
            if len(good_options) < 2:
                errors.append(_err("ad", "lead_form.questions", "Bir nechta variantli savolda kamida 2 ta variant bo'lishi kerak."))
    privacy = (form.get("privacy_url") or "").strip()
    if not privacy.startswith("http"):
        errors.append(_err("ad", "lead_form.privacy_url", "Maxfiylik siyosati havolasi (privacy_url) kerak -- Meta buni talab qiladi."))
    return errors


# ---------------------------------------------------------------------------
# META'GA TARJIMA
# ---------------------------------------------------------------------------

def to_meta_targeting(state: dict) -> dict:
    """Kanonik holatdan HAQIQIY Graph API targeting spec'ini quradi.
    - geo_locations: countries (kodlar), cities [{key,radius,distance_unit}],
      regions [{key}] -- faqat mavjud bo'lganlari.
    - flexible_spec [{"interests": [{id,name}]}] FAQAT qiziqish bo'lsa.
    - targeting_automation.advantage_audience 1/0.
    - publisher_platforms/*_positions FAQAT placements.mode == "manual"."""
    adset = state.get("adset") or {}
    t = adset.get("targeting") or {}
    geo_in = t.get("geo_locations") or {}
    geo: dict = {}
    if geo_in.get("countries"):
        geo["countries"] = [str(c).upper() for c in geo_in["countries"]]
    if geo_in.get("cities"):
        cities = []
        for c in geo_in["cities"]:
            entry = {"key": str(c["key"])}
            if c.get("radius") is not None:
                entry["radius"] = int(c["radius"])
                entry["distance_unit"] = c.get("distance_unit") or "kilometer"
            cities.append(entry)
        geo["cities"] = cities
    if geo_in.get("regions"):
        geo["regions"] = [{"key": str(r["key"])} for r in geo_in["regions"]]
    spec: dict = {"geo_locations": geo}
    if t.get("age_min") is not None:
        spec["age_min"] = int(t["age_min"])
    if t.get("age_max") is not None:
        spec["age_max"] = int(t["age_max"])
    if t.get("genders"):
        spec["genders"] = [int(g) for g in t["genders"]]
    if t.get("locales"):
        spec["locales"] = [int(l) for l in t["locales"]]
    flexible: dict = {}
    if t.get("interests"):
        flexible["interests"] = [{"id": str(i["id"]), "name": i.get("name") or ""} for i in t["interests"]]
    if t.get("behaviors"):
        flexible["behaviors"] = [{"id": str(b["id"]), "name": b.get("name") or ""} for b in t["behaviors"]]
    if flexible:
        spec["flexible_spec"] = [flexible]
    if t.get("custom_audiences"):
        spec["custom_audiences"] = [{"id": str(a["id"])} for a in t["custom_audiences"]]
    if t.get("excluded_custom_audiences"):
        spec["excluded_custom_audiences"] = [{"id": str(a["id"])} for a in t["excluded_custom_audiences"]]
    spec["targeting_automation"] = {"advantage_audience": 1 if t.get("advantage_audience") else 0}
    placements = t.get("placements") or {}
    if placements.get("mode") == "manual":
        if placements.get("publisher_platforms"):
            spec["publisher_platforms"] = list(placements["publisher_platforms"])
        if placements.get("facebook_positions"):
            spec["facebook_positions"] = list(placements["facebook_positions"])
        if placements.get("instagram_positions"):
            spec["instagram_positions"] = list(placements["instagram_positions"])
    return spec


def build_page_welcome_message(greeting: str, quick_replies: "list[str] | None") -> str:
    """Click-to-Message reklama uchun `page_welcome_message` JSON-string'i.
    Meta v21 hujjati bo'yicha (VISUAL_EDITOR, ice_breakers, ko'pi bilan 4 ta
    tezkor javob); rad etilsa `meta_publish` friendly xato ko'rsatadi."""
    breakers = [{"title": str(q).strip()[:80], "response": ""} for q in (quick_replies or []) if str(q).strip()][:4]
    payload = {
        "type": "VISUAL_EDITOR",
        "version": 2,
        "landing_screen_type": "welcome_message",
        "media_type": "text",
        "text_format": {
            "customer_action_type": "ice_breakers",
            "message": {"ice_breakers": breakers, "text": (greeting or "").strip()},
        },
    }
    return json.dumps(payload, ensure_ascii=False)


def to_meta_creative_spec(
    state: dict, *, page_id: str, instagram_actor_id: "str | None" = None,
    image_hash: "str | None" = None, video_id: "str | None" = None, lead_form_id: "str | None" = None,
) -> dict:
    """Kanonik holatdan AdCreative uchun `object_story_spec` quradi.
    Rasm -> `link_data`, video -> `video_data`. CTA: UI nomi API nomiga
    (`CTA_API_MAP`) o'giriladi; qiymati maqsadga qarab: LEADS ->
    {"lead_gen_form_id"}, MESSAGES -> {"app_destination"}, boshqalar ->
    {"link"}. MESSAGES uchun `page_welcome_message` ham qo'shiladi.
    Meta v21 hujjati bo'yicha; rad etilsa friendly xato ko'rsatiladi."""
    objective = (state.get("objective") or "").upper()
    ad = state.get("ad") or {}
    adset = state.get("adset") or {}
    cta_ui = ad.get("cta") or OBJECTIVE_META.get(objective, {}).get("default_cta") or "LEARN_MORE"
    cta_type = CTA_API_MAP.get(cta_ui, cta_ui)
    link = (ad.get("link_url") or "").strip() or f"https://www.facebook.com/{page_id}"

    if objective == "LEADS" and lead_form_id:
        cta_value = {"lead_gen_form_id": str(lead_form_id)}
    elif objective == "MESSAGES":
        dest = adset.get("destination_type") or "MESSENGER"
        app_dest = {"MESSENGER": "MESSENGER", "INSTAGRAM_DIRECT": "INSTAGRAM_DIRECT", "WHATSAPP": "WHATSAPP"}.get(dest, "MESSENGER")
        cta_value = {"app_destination": app_dest}
        if cta_type == "MESSAGE_PAGE" and app_dest == "WHATSAPP":
            cta_type = "WHATSAPP_MESSAGE"
    elif objective == "CALLS":
        cta_value = {"link": link}
    else:
        cta_value = {"link": link}
    call_to_action = {"type": cta_type, "value": cta_value}

    spec: dict = {"page_id": str(page_id)}
    if instagram_actor_id:
        spec["instagram_actor_id"] = str(instagram_actor_id)
    if video_id:
        data = {
            "video_id": str(video_id),
            "message": ad.get("primary_text") or "",
            "title": ad.get("headline") or "",
            "link_description": ad.get("description") or "",
            "call_to_action": call_to_action,
        }
        if image_hash:
            data["image_hash"] = image_hash  # video uchun muqova (thumbnail) -- Meta v21 hujjati bo'yicha
        if objective == "MESSAGES":
            data["page_welcome_message"] = build_page_welcome_message(
                (ad.get("messages") or {}).get("greeting") or "", (ad.get("messages") or {}).get("quick_replies") or [])
        spec["video_data"] = data
    else:
        data = {
            "image_hash": str(image_hash or ""),
            "message": ad.get("primary_text") or "",
            "name": ad.get("headline") or "",
            "description": ad.get("description") or "",
            "link": link,
            "call_to_action": call_to_action,
        }
        if ad.get("display_link"):
            data["caption"] = ad["display_link"]
        if objective == "MESSAGES":
            data["page_welcome_message"] = build_page_welcome_message(
                (ad.get("messages") or {}).get("greeting") or "", (ad.get("messages") or {}).get("quick_replies") or [])
        spec["link_data"] = data
    return spec


def lead_form_config_from_state(state: dict) -> dict:
    """`ad.lead_form.new_form`dan `meta_api.create_lead_form()` kutadigan
    `form_config`ni quradi (Meta v21 `leadgen_forms` hujjati bo'yicha)."""
    form = ((state.get("ad") or {}).get("lead_form") or {}).get("new_form") or {}
    questions = []
    for q in form.get("questions") or []:
        qtype = str(q.get("type") or "CUSTOM").upper()
        if qtype == "CUSTOM":
            label = q.get("label") or ""
            entry = {"type": "CUSTOM", "key": q.get("key") or _slug(label or "savol"), "label": label}
            # 2026-09, foydalanuvchi so'rovi ("multiplay choice"): variantlar
            # bo'lsa -- Meta v21 `leadgen_forms` hujjati bo'yicha CUSTOM
            # savolga "options": [{"key","value"}, ...] qo'shiladi (bir nechta
            # tanlovli savol). Rad etilsa friendly xato ko'rsatiladi (boshqa
            # noaniq Meta shakllari kabi).
            good_options = [str(o).strip() for o in (q.get("options") or []) if str(o).strip()]
            if good_options:
                entry["options"] = [{"key": _slug(o) or f"variant_{i + 1}", "value": o} for i, o in enumerate(good_options)]
            questions.append(entry)
        else:
            questions.append({"type": qtype})
    return {
        "name": form.get("name") or "Replix lead forma",
        "intro": {"headline": form.get("intro_headline") or "", "description": form.get("intro_description") or ""},
        "questions": questions,
        "privacy_policy": {"url": form.get("privacy_url") or ""},
        "thank_you_page": {"title": form.get("thank_you_title") or "Rahmat!", "body": form.get("thank_you_body") or "Tez orada bog'lanamiz."},
    }


# ---------------------------------------------------------------------------
# YAKUNIY TASDIQ MODALI UCHUN XULOSA
# ---------------------------------------------------------------------------

def _fmt_money(amount, currency: str) -> str:
    if amount is None:
        return "—"
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return str(amount)
    if amount == int(amount):
        return f"{int(amount):,} {currency}".replace(",", " ")
    return f"{amount:,.2f} {currency}".replace(",", " ")


def summary_for_review(state: dict, ctx: "dict | None" = None) -> dict:
    """Nashrdan oldingi tasdiq oynasi uchun odam o'qiydigan xulosa."""
    ctx = ctx or {}
    objective = (state.get("objective") or "").upper()
    adset = state.get("adset") or {}
    ad = state.get("ad") or {}
    t = adset.get("targeting") or {}
    geo = t.get("geo_locations") or {}
    currency = adset.get("currency") or "UZS"
    budget_type = adset.get("budget_type") or "daily"
    budget = adset.get("daily_budget") if budget_type == "daily" else adset.get("lifetime_budget")
    duration = adset.get("duration_days")
    if not duration and adset.get("start_time") and adset.get("end_time"):
        try:
            duration = (dt.datetime.fromisoformat(adset["end_time"]) - dt.datetime.fromisoformat(adset["start_time"])).days or 1
        except ValueError:
            duration = None
    estimated_total = None
    if budget is not None:
        estimated_total = float(budget) * (duration or 1) if budget_type == "daily" else float(budget)

    locations = [c.get("name") or c.get("key") for c in geo.get("cities") or []]
    locations += [r.get("name") or r.get("key") for r in geo.get("regions") or []]
    locations += list(geo.get("countries") or [])
    genders = t.get("genders") or []
    gender_text = "Hammasi" if not genders else ("Erkaklar" if genders == [1] else "Ayollar" if genders == [2] else "Hammasi")
    interests = [i.get("name") or i.get("id") for i in t.get("interests") or []]
    audience_line = f"{t.get('age_min')}-{t.get('age_max')} yosh, {gender_text}"
    if interests:
        audience_line += ", qiziqishlar: " + ", ".join(interests)
    if t.get("advantage_audience"):
        audience_line += " (Advantage+ auditoriya yoqilgan)"
    placements = t.get("placements") or {}
    if placements.get("mode") == "manual":
        placements_line = ", ".join(placements.get("publisher_platforms") or []) or "qo'lda (platforma tanlanmagan)"
    else:
        placements_line = "Avtomatik (Advantage+ placements)"
    media = ad.get("media") or {}
    creative = "Video" if media.get("video_id") else ("Rasm" if media.get("image_hash") else "Media yuklanmagan")

    warnings = [e["message"] for e in validate_state(state, company=None, meta_assets=None)]
    return {
        "objective": objective,
        "objective_label": OBJECTIVE_LABELS.get(objective, objective),
        "campaign_name": (state.get("campaign") or {}).get("name"),
        "adset_name": adset.get("name"),
        "ad_name": ad.get("name"),
        "budget_line": ("Kunlik " if budget_type == "daily" else "Umumiy ") + _fmt_money(budget, currency),
        "duration_days": duration,
        "duration_line": f"{duration} kun" if duration else "Muddat belgilanmagan (cheksiz)",
        "start_time": adset.get("start_time"),
        "end_time": adset.get("end_time"),
        "locations": locations,
        "audience_line": audience_line,
        "placements_line": placements_line,
        "page_id": ad.get("page_id"),
        "instagram_actor_id": ad.get("instagram_actor_id"),
        "creative": creative,
        "primary_text": ad.get("primary_text"),
        "headline": ad.get("headline"),
        "cta": ad.get("cta"),
        "cta_label": CTA_LABELS.get(ad.get("cta"), ad.get("cta")),
        "destination_type": adset.get("destination_type"),
        "estimated_total_budget": estimated_total,
        "estimated_total_line": _fmt_money(estimated_total, currency) if estimated_total is not None else "—",
        "company_name": ctx.get("company_name"),
        "warnings": warnings,
    }


def compact_state_for_prompt(state: dict) -> str:
    """LLM promptiga qo'shish uchun holatning IXCHAM JSON ko'rinishi
    (copy_variants va uzun ro'yxatlar qisqartiriladi -- token tejash)."""
    s = copy.deepcopy(state or {})
    ad = s.get("ad") or {}
    cv = ad.get("copy_variants") or {}
    ad["copy_variants"] = {k: [f"[{i}] {v[:60]}" for i, v in enumerate(cv.get(k) or [])] for k in ("primary_text", "headline", "description")}
    for key in ("primary_text", "headline", "description"):
        if isinstance(ad.get(key), str) and len(ad[key]) > 200:
            ad[key] = ad[key][:200] + "…"
    return json.dumps(s, ensure_ascii=False)
