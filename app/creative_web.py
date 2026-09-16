"""creative_web.py — Kreativ studiya (AI rasm-generatsiya): WEB QATLAM
yordamchilari (2026-09, foydalanuvchi so'rovi: "kompaniya brifidan kelib
chiqib OpenAI orqali to'liq tayyor reklama rasmini AI yaratib bersin ...
natijani tahrirlash, PNG/PDF eksport, Avtopilotga tashlash; 20 ta tayyor
shablon").

`app.py`dagi `/kreativ/...` marshrutlari yupqa -- katta ishlar shu yerda,
xuddi `autopilot_web.py` kabi:
  - `serialize_asset()`  -- bitta `CreativeAsset`ni JS muharriri
                            (static/creative_studio.js) o'qiydigan BITTA
                            JSON'ga aylantiradi (holat, brif savollari,
                            qatlamlar, rasm URL'lari, kvota, brend kit).
                            Server HAR javobda TO'LIQ yangilangan holatni
                            qaytaradi, JS uni bitta `store`ga yozadi.
  - `list_assets_for_company()` -- galereya uchun qisqa ro'yxat.
  - `template_cards()`   -- 20 ta shablon galereyasi (statik preview PNG).
  - `error_payload()`    -- `CreativeError`/`QuotaExceededError`ni
                            foydalanuvchiga ko'rsatiladigan JSON'ga o'giradi
                            (xuddi Autopilot'da `PublishError` kabi).
Asosiy biznes mantiq -- `creative_studio.py` (bu modul uni faqat chaqiradi).
"""

import logging
import datetime as dt

import plans
import creative_studio
import creative_templates

logger = logging.getLogger("creative_web")

STATUS_LABELS = {
    "collecting_brief": "Savol-javob", "generating": "AI yaratmoqda", "ready": "Tayyor", "failed": "Xato",
}
STATUS_COLORS = {"collecting_brief": "warn", "generating": "blue", "ready": "good", "failed": "bad"}
KIND_LABELS = {"ai_generated": "AI", "template": "Shablon", "upload": "Yuklangan"}
ASPECT_LABELS = {"1:1": "1:1 (kvadrat, lenta)", "4:5": "4:5 (vertikal, lenta)", "9:16": "9:16 (Story / Reels)"}


def _iso(value) -> "str | None":
    if isinstance(value, dt.datetime):
        return value.replace(microsecond=0).isoformat()
    return value


def _version(asset) -> str:
    """Rasm URL'lari uchun kesh-buster: qatlam saqlanganda `updated_at`
    o'zgaradi -> brauzer yangi rasmni oladi (`max_age` bilan ham)."""
    ts = asset.updated_at or asset.created_at
    return str(int(ts.timestamp())) if isinstance(ts, dt.datetime) else "0"


# ---------------------------------------------------------------------------
# KVOTA / BREND
# ---------------------------------------------------------------------------

def quota_info(session, company) -> dict:
    """`creative_studio.quota_status` + tarif nomi + UI uchun qisqa matn
    ("Bu oy: 3/10 rasm")."""
    plan_def = plans.get_plan(getattr(company, "plan", None))
    st = creative_studio.quota_status(session, company, plan_def)
    st["plan_key"] = getattr(company, "plan", None)
    st["plan_name"] = plan_def.name
    if st["limit"] is None:
        st["label"] = f"Bu oy: {st['used']} rasm (cheksiz)"
    elif st["limit"] <= 0:
        st["label"] = f"\"{plan_def.name}\" tarifida AI rasm yo'q"
    else:
        st["label"] = f"Bu oy: {st['used']}/{st['limit']} rasm ishlatildi"
    st["can_generate"] = bool(st["enabled"] and (st["remaining"] is None or st["remaining"] > 0))
    return st


def brand_info(brand_kit, logo_url: "str | None" = None) -> dict:
    """Brend kit JSON'i (muharrir canvas'i logotipni chizishi uchun URL)."""
    has_logo = bool(creative_studio.brand_logo_file_path(brand_kit))
    return {
        "has_logo": has_logo,
        "logo_url": logo_url if has_logo else None,
        "primary_color": getattr(brand_kit, "primary_color", None),
        "secondary_color": getattr(brand_kit, "secondary_color", None),
    }


# ---------------------------------------------------------------------------
# BRIF SAVOLLARI
# ---------------------------------------------------------------------------

def brief_questions_for(ctx: dict, asset) -> "tuple[list[dict], list[dict]]":
    """UI uchun ikkita ro'yxat: (barcha brif savollari `answered`/`required`
    belgisi bilan, HALI majburiy bo'lgan savollar). Savol-javob oqimi
    javob berilmagan savollarni birma-bir ko'rsatadi; ixtiyoriylarini
    o'tkazib yuborish mumkin (bo'sh javob ham "ko'rib chiqildi" deb
    saqlanadi -- `creative_studio.submit_brief_answer`)."""
    answers = asset.get_brief_answers() if asset is not None else {}
    missing = creative_studio.missing_questions(ctx, answers)
    required_keys = {q["key"] for q in missing if q.get("required")}
    all_q = []
    # Telefon profilda bo'lsa savol umuman ko'rsatilmaydi (2026-09).
    for key, question, placeholder, optional in creative_studio.visible_brief_questions(ctx):
        all_q.append({
            "key": key, "question": question, "placeholder": placeholder,
            "required": key in required_keys, "optional": key not in required_keys,
            "answered": key in answers, "answer": answers.get(key, ""),
        })
    return all_q, [q for q in missing if q.get("required")]


# ---------------------------------------------------------------------------
# SERIALIZATSIYA
# ---------------------------------------------------------------------------

def target_options(ctx: "dict | None", *, can_create: bool) -> dict:
    """"Targetga ochish" (2026-09) mini-formasi uchun: maqsadlar ro'yxati,
    hudud so'raladimi (profilda standart hudud bo'lmasa), ruxsat (admin)."""
    import campaign_draft
    ctx = ctx or {}
    return {
        "can_create": bool(can_create),
        "objectives": [{"value": o, "label": campaign_draft.OBJECTIVE_LABELS[o]} for o in campaign_draft.OBJECTIVES],
        "default_objective": "MESSAGES",
        "needs_location": not bool(ctx.get("default_location")),
        "default_location": ctx.get("default_location") or "",
    }


def serialize_asset(asset, brand_kit, quota: "dict | None", *, ctx: "dict | None" = None, urls: "dict | None" = None, target: "dict | None" = None) -> dict:
    """JS muharriri uchun TO'LIQ ko'rinish. `urls` -- app.py `url_for` bilan
    hisoblab beradi: image (final PNG), base_image (matnsiz fon), export_png,
    export_pdf, base (asset marshrutlari ildizi, masalan /kreativ/12), list,
    templates, brand_settings, pricing, brand_logo, autopilot (agar
    `from_autopilot` bo'lsa)."""
    urls = urls or {}
    ctx = ctx or {}
    questions, missing = brief_questions_for(ctx, asset)
    template = creative_templates.get_template(asset.template_key) if asset.template_key else None
    is_ready = asset.status == "ready" and bool(asset.final_storage_path)
    has_base = bool(asset.base_image_storage_path)
    v = _version(asset)
    width, height = (asset.width, asset.height) if (asset.width and asset.height) else creative_studio._target_pixels_for_aspect(asset.aspect)
    out = {
        "id": asset.id,
        "title": asset.title or (template["name"] if template else None) or f"Kreativ #{asset.id}",
        "status": asset.status, "status_label": STATUS_LABELS.get(asset.status, asset.status),
        "status_color": STATUS_COLORS.get(asset.status, "dim"),
        "kind": asset.kind, "kind_label": KIND_LABELS.get(asset.kind, asset.kind),
        "template_key": asset.template_key,
        "template": {"key": template["key"], "name": template["name"], "description": template["description"]} if template else None,
        "aspect": asset.aspect, "aspect_label": ASPECT_LABELS.get(asset.aspect, asset.aspect),
        "width": width, "height": height,
        "brief_answers": asset.get_brief_answers(),
        "questions": questions,
        "missing_questions": missing,
        "layers": creative_studio.get_layers(asset),
        "image_url": (urls.get("image") + "?v=" + v) if (is_ready and urls.get("image")) else None,
        "base_image_url": (urls.get("base_image") + "?v=" + v) if (has_base and urls.get("base_image")) else None,
        "is_ready": is_ready,
        "has_base_image": has_base,
        "error_message": asset.error_message,
        "created_at": _iso(asset.created_at), "updated_at": _iso(asset.updated_at),
        "quota": quota or {},
        "brand": brand_info(brand_kit, urls.get("brand_logo")),
        "base_url": urls.get("base") or f"/kreativ/{asset.id}",
        "urls": {k: urls.get(k) for k in ("export_png", "export_pdf", "list", "templates", "brand_settings", "pricing", "autopilot", "new", "target_create", "autopilot_new")},
        "from_autopilot": urls.get("from_autopilot_draft_id"),
        # 2026-09: "Targetga ochish" -- tayyor kreativdan Avtopilot qoralamasi
        "target": target or {"can_create": False, "objectives": [], "needs_location": False, "default_location": "", "default_objective": "MESSAGES"},
    }
    return out


def list_assets_for_company(session, company_id: int, image_url_builder=None, *, limit: int = 100) -> list[dict]:
    """Galereya uchun qisqa ro'yxat (yangi -> eski): thumbnail URL (faqat
    tayyor bo'lsa), sarlavha, holat, sana."""
    rows = creative_studio.list_assets(session, company_id, limit=limit)
    out = []
    for a in rows:
        template = creative_templates.get_template(a.template_key) if a.template_key else None
        ready = a.status == "ready" and bool(a.final_storage_path)
        out.append({
            "id": a.id,
            "title": a.title or (template["name"] if template else None) or f"Kreativ #{a.id}",
            "status": a.status, "status_label": STATUS_LABELS.get(a.status, a.status),
            "status_color": STATUS_COLORS.get(a.status, "dim"),
            "kind": a.kind, "kind_label": KIND_LABELS.get(a.kind, a.kind),
            "aspect": a.aspect, "template_name": template["name"] if template else None,
            "is_ready": ready,
            "thumbnail_url": (image_url_builder(a.id) + "?v=" + _version(a)) if (ready and image_url_builder) else None,
            "error_message": a.error_message,
            "created_at": a.created_at, "updated_at": a.updated_at,
        })
    return out


# Asosiy /kreativ sahifasida dastlab ko'rsatiladigan shablonlar soni
# (qolganlari "Yana ... ta" tugmasi bilan o'sha joyda ochiladi, sahifa
# almashmaydi).
INLINE_TEMPLATES_INITIAL = 8


def template_cards(preview_url_builder=None) -> list[dict]:
    """20 ta shablon galereyasi: nom, tavsif, kategoriya, standart nisbat,
    preview (`static/creative_templates/<key>.png`)."""
    out = []
    for t in creative_templates.CREATIVE_TEMPLATES:
        out.append({
            "key": t["key"], "name": t["name"], "description": t["description"], "category": t["category"],
            "aspect_default": t["aspect_default"], "aspect_label": ASPECT_LABELS.get(t["aspect_default"], t["aspect_default"]),
            "default_cta": t.get("default_cta"),
            "preview_url": preview_url_builder(t["key"]) if preview_url_builder else None,
        })
    return out


# ---------------------------------------------------------------------------
# XATOLAR
# ---------------------------------------------------------------------------

def error_payload(e: Exception) -> "tuple[dict, int]":
    """`CreativeError` (shu jumladan `QuotaExceededError`) -> (JSON, HTTP
    kod). Kvota tugagan bo'lsa `quota_exceeded: True` -- JS "Tarifni
    oshiring" havolasini ko'rsatadi. Xom OpenAI/tarmoq matni bu yerga
    hech qachon tushmaydi (creative_studio o'zi friendly matn beradi)."""
    if isinstance(e, creative_studio.QuotaExceededError):
        return {"error": str(e), "quota_exceeded": True}, 400
    if isinstance(e, creative_studio.CreativeError):
        return {"error": str(e)}, 400
    logger.exception("creative_web: kutilmagan xato")
    return {"error": "Kutilmagan xatolik yuz berdi. Birozdan keyin qayta urinib ko'ring."}, 500


def parse_layers_body(body) -> list:
    """`POST /layers` JSON tanasidan qatlamlar ro'yxatini oladi
    ({"layers": [...]} yoki to'g'ridan-to'g'ri ro'yxat)."""
    if isinstance(body, dict):
        body = body.get("layers")
    if not isinstance(body, list):
        raise creative_studio.CreativeError("Qatlamlar ro'yxati yuborilmadi.")
    return body
