"""autopilot_web.py — Meta Ads Autopilot: WEB QATLAM yordamchilari (2026-09,
foydalanuvchi so'rovi: "Replix ... Ads Manager'dagi har bir maydonni
boshidan o'zi to'ldirmasligi kerak" -- AI reja tuzadi, foydalanuvchi
Ads Manager'ga o'xshash sahifada ko'rib, tasdiqlab, Meta'ga chiqaradi).

`app.py`dagi `/avtopilot/...` marshrutlari yupqa -- katta ishlar shu yerda:
  - `serialize_draft()`     -- qoralamani JS ilovasi (static/autopilot.js)
                               o'qiydigan BITTA JSON'ga aylantiradi
                               (holat, manbalar, AI reja, tasdiqlar, media,
                               Meta aktivlari, tekshiruv xatolari, audit).
  - `apply_and_persist_patch()` -- qo'lda YOKI chat orqali kelgan patch
                               uchun YAGONA quvur: apply_patch -> validate ->
                               approvals_after_change -> saqlash -> audit.
                               Forma ham, AI ham AYNAN SHU quvurdan o'tadi
                               (bitta kanonik holat, ikki xil kirish).
  - `resolve_geo_factory()` / `resolve_interests_factory()` -- Meta qidiruv
                               callback'lari kompaniya TOKENI bilan (xato
                               bo'lsa None/[] -- reja to'xtamaydi, uydirma
                               ID yo'q).
  - `preview_for_draft()`   -- HAQIQIY Meta preview (generatepreviews) yoki,
                               media/sahifa bo'lmasa, ANIQ belgilangan lokal
                               ko'rinish ("Meta preview emas").
  - `events_for_draft()`    -- audit-jurnal (kim/qachon/nima).
  - `publish_gate()`        -- "Nashr" tugmasi nega o'chiq ekanining sabablari.
"""

import copy
import html as html_stdlib
import logging
import json
import datetime as dt

import db
import plans
import meta_api
import campaign_draft
import campaign_media
import meta_publish

logger = logging.getLogger("autopilot_web")

STATUS_LABELS = {
    "draft": "Qoralama", "publishing": "Nashr qilinmoqda", "published": "Meta'da (PAUSED)",
    "active": "Faol (ACTIVE)", "failed": "Nashr xatosi", "archived": "Arxiv",
}
SYNC_LABELS = {
    "local": "Lokal", "synced": "Synced", "local_changes": "Lokal o'zgarishlar",
    "publishing": "Nashr qilinmoqda", "meta_changed": "Meta'da o'zgargan", "sync_error": "Sync xatosi",
}
SOURCE_LABELS = {"AI_RECOMMENDED": "✨ AI", "USER_OVERRIDDEN": "Siz o'zgartirdingiz", "META_IMPORTED": "Meta"}
ACTION_LABELS = {
    "ai_generated_plan": "AI reja tuzdi", "ai_replan": "AI qayta reja tuzdi", "user_edit": "Qo'lda tahrir",
    "ai_edit": "Chat orqali tahrir", "ai_regenerate_copy": "AI matnni qayta yozdi",
    "approve_campaign": "Kampaniya tasdiqlandi", "approve_adset": "Ad Set tasdiqlandi", "approve_ad": "Reklama tasdiqlandi",
    "media_uploaded": "Media Meta'ga yuklandi", "media_added": "Media yuklandi", "media_selected": "Media tanlandi",
    "publish_started": "Nashr boshlandi", "publish_verified": "Nashr yakunlandi (tekshirildi)", "publish_failed": "Nashr xatosi", "ai_edit_rejected": "Chat buyrug'i rad etildi",
    "meta_error": "Meta xatosi", "activated": "Faollashtirildi", "synced": "Sinxronlandi", "imported": "Meta'dan import",
    "archived": "Arxivlandi", "meta_creative_created": "Yangi kreativ yaratildi",
    "target_analysis_run": "Target Analizi ishga tushdi", "target_analysis_applied": "Target Analizi -- o'zgarish qo'llandi",
    "ai_extra_adset_created": "AI qo'shimcha ad set (yangi qoralama) yaratdi",
    "draft_saved": "Qoralama sifatida saqlandi",
}
ACTOR_LABELS = {"user": "Foydalanuvchi", "ai": "Replix AI", "system": "Tizim"}

# Xabar maqsadi uchun yo'nalishlar -- qaysi biri KOMPANIYA aktivlariga
# qarab mavjud. WhatsApp: Meta API orqali WhatsApp Business raqamini
# ulash Replix'da hozircha sozlanmagan -- UI'da o'chiq ko'rsatiladi.
DESTINATION_LABELS = {
    "MESSENGER": "Messenger", "INSTAGRAM_DIRECT": "Instagram Direct", "WHATSAPP": "WhatsApp",
    "ON_AD": "Instant Form (reklama ichida)", "WEBSITE": "Sayt", "PHONE_CALL": "Telefon qo'ng'irog'i",
}
_UNAVAILABLE_NOTE = "Meta API orqali hozircha sozlanmagan"


# ---------------------------------------------------------------------------
# META QIDIRUV CALLBACK'LARI (kompaniya tokeni bilan)
# ---------------------------------------------------------------------------

def _company_token(company) -> "str | None":
    try:
        return company.get_meta_access_token() if company is not None else None
    except Exception:  # noqa: BLE001 -- shifrlash kaliti yo'q va h.k.: token yo'q deb hisoblaymiz
        return None


def resolve_geo_factory(company):
    """`ai_campaign_planner.plan_campaign/chat_edit` uchun `resolve_geo(name)`
    callback'i: Meta `adgeolocation` qidiruvi, O'zbekiston (UZ) nomzodlari
    ustun; token yo'q yoki Meta xato bersa -> `None` (planner buni
    "topilmadi" deb ogohlantiradi, uydirma key HECH QACHON qaytmaydi)."""
    token = _company_token(company)

    def resolve_geo(name: str):
        if not token or not (name or "").strip():
            return None
        try:
            hits = meta_api.search_geo_location(name, access_token=token)
        except meta_api.MetaAPIError as e:
            logger.warning("resolve_geo('%s'): %s", name, meta_api.safe_error_message(e))
            return None
        except Exception as e:  # noqa: BLE001 -- tarmoq/proxy: reja to'xtamasin
            logger.warning("resolve_geo('%s') kutilmagan xato: %s", name, meta_api.safe_error_message(e))
            return None
        if not hits:
            return None
        preferred = [h for h in hits if str(h.get("country_code") or "").upper() == "UZ"]
        hit = (preferred or hits)[0]
        return {"key": hit.get("key"), "name": hit.get("name") or name, "type": hit.get("type") or "city",
                "country_code": hit.get("country_code"), "region": hit.get("region")}

    return resolve_geo


def resolve_interests_factory(company):
    """`resolve_interests(name) -> [{"id","name"}]` -- Meta `adinterest`
    qidiruvi; xatoda bo'sh ro'yxat."""
    token = _company_token(company)

    def resolve_interests(name: str):
        if not token or not (name or "").strip():
            return []
        try:
            return meta_api.search_targeting_interests(name, access_token=token, limit=5)
        except meta_api.MetaAPIError as e:
            logger.warning("resolve_interests('%s'): %s", name, meta_api.safe_error_message(e))
            return []
        except Exception as e:  # noqa: BLE001
            logger.warning("resolve_interests('%s') kutilmagan xato: %s", name, meta_api.safe_error_message(e))
            return []

    return resolve_interests


def safe_meta_assets(company) -> dict:
    """`meta_publish.get_meta_assets` -- lekin HECH QACHON sahifani
    yiqitmaydi (token yo'q/tarmoq yo'q bo'lsa bo'sh aktivlar + xato matni)."""
    try:
        return meta_publish.get_meta_assets(company)
    except Exception as e:  # noqa: BLE001
        logger.exception("get_meta_assets yiqildi (company=%s)", getattr(company, "id", None))
        return {"ad_account": None, "pages": [], "instagram_accounts": [], "pixels": [], "custom_audiences": [],
                "lead_forms": [], "recent_images": [], "campaigns": [], "errors": [meta_api.safe_error_message(e)],
                "pixel_id": getattr(company, "meta_pixel_id", None), "page_id": getattr(company, "meta_page_id", None),
                "ig_business_id": getattr(company, "ig_business_id", None)}


# ---------------------------------------------------------------------------
# ULANISH HOLATI / NASHR DARVOZASI
# ---------------------------------------------------------------------------

def connection_status(company, assets: "dict | None" = None) -> dict:
    """Sahifa yuqorisidagi banner uchun: tarif ruxsati, token, reklama
    hisobi, sahifa, Instagram. `ok` -- nashr uchun minimal shartlar bor."""
    plan = plans.get_plan(getattr(company, "plan", None)) if company is not None else plans.get_plan(None)
    token = _company_token(company)
    ad_account_id = getattr(company, "meta_ad_account_id", None)
    account = (assets or {}).get("ad_account") or {}
    ig_ids = [a.get("id") for a in ((assets or {}).get("instagram_accounts") or []) if a.get("id")]
    out = {
        "plan_key": getattr(company, "plan", None),
        "plan_name": plan.name,
        "plan_allows_meta": bool(plan.can_connect_meta_ads),
        "has_token": bool(token),
        "has_ad_account": bool(ad_account_id),
        "ad_account_id": ad_account_id,
        "ad_account_name": account.get("name"),
        "currency": (account.get("currency") or "UZS"),
        "timezone": account.get("timezone_name"),
        "has_page": bool(getattr(company, "meta_page_id", None)),
        "page_id": getattr(company, "meta_page_id", None),
        "has_instagram": bool(getattr(company, "ig_business_id", None) or ig_ids),
        "has_pixel": bool(getattr(company, "meta_pixel_id", None)),
        "asset_errors": list((assets or {}).get("errors") or []),
    }
    problems = []
    if not out["plan_allows_meta"]:
        problems.append(f"\"{plan.name}\" tarifida Meta reklama hisobini ulash imkoni yo'q -- AI reja tuzish ishlaydi, lekin Meta'ga nashr qilish uchun tarifni yangilang.")
    if not out["has_token"] or not out["has_ad_account"]:
        problems.append("Meta reklama hisobi ulanmagan -- Sozlamalar > Ulanishlar orqali Facebook'ni ulang.")
    elif not out["has_page"]:
        problems.append("Facebook sahifa tanlanmagan -- reklama sahifa nomidan chiqadi, Ulanishlar bo'limida sahifani tanlang.")
    out["problems"] = problems
    out["ok"] = not problems
    return out


def publish_gate(draft, company, assets: "dict | None") -> dict:
    """"Meta'ga nashr qilish" tugmasi holati: `can_publish` va sabablar
    (`meta_publish.validate_for_publish` bilan bir xil manba -- UI va
    server hech qachon turlicha gapirmaydi)."""
    try:
        reasons = meta_publish.validate_for_publish(draft, company, assets)
    except Exception as e:  # noqa: BLE001
        logger.exception("validate_for_publish yiqildi (draft=%s)", getattr(draft, "id", None))
        reasons = [f"Tekshiruvda xato: {meta_api.safe_error_message(e)}"]
    return {"can_publish": not reasons, "reasons": reasons}


# ---------------------------------------------------------------------------
# SERIALIZATSIYA
# ---------------------------------------------------------------------------

def _iso(value) -> "str | None":
    if isinstance(value, dt.datetime):
        return value.replace(microsecond=0).isoformat()
    return value


def media_for_draft(session, draft, url_builder=None) -> list[dict]:
    """Qoralamaga yuklangan media qatorlari (JSON). `url_builder(media_id)`
    -- faylni ko'rsatadigan URL (`app.autopilot_media_file`)."""
    with db.scoped_as(draft.company_id):
        rows = (
            session.query(db.CampaignDraftMedia)
            .filter(db.CampaignDraftMedia.draft_id == draft.id)
            .order_by(db.CampaignDraftMedia.id.asc())
            .all()
        )
    state = draft.get_state()
    current = ((state.get("ad") or {}).get("media") or {})
    out = []
    for idx, m in enumerate(rows):
        selected = (current.get("media_id") == m.id) or (
            not current.get("media_id") and ((m.meta_image_hash and current.get("image_hash") == m.meta_image_hash)
                                             or (m.meta_video_id and current.get("video_id") == m.meta_video_id)))
        out.append({
            "id": m.id, "index": idx, "kind": m.kind, "filename": m.filename, "content_type": m.content_type,
            "size_bytes": m.size_bytes, "width": m.width, "height": m.height,
            "image_hash": m.meta_image_hash, "video_id": m.meta_video_id,
            "upload_status": m.upload_status, "upload_error": m.upload_error,
            "url": url_builder(m.id) if url_builder else None,
            "selected": bool(selected),
            "created_at": _iso(m.created_at),
        })
    return out


def events_for_draft(session, draft, limit: int = 200) -> list[dict]:
    """Audit-jurnal: kim (actor + menejer nomi), qachon, nima (action +
    o'zbekcha yorliq), qaysi darajada, tafsilotlar."""
    with db.scoped_as(draft.company_id):
        rows = (
            session.query(db.CampaignDraftEvent)
            .filter(db.CampaignDraftEvent.draft_id == draft.id)
            .order_by(db.CampaignDraftEvent.id.desc())
            .limit(limit)
            .all()
        )
        manager_ids = {r.manager_id for r in rows if r.manager_id}
        names = {}
        if manager_ids:
            for m in session.query(db.Manager).filter(db.Manager.id.in_(manager_ids)).all():
                names[m.id] = m.full_name or m.username
    out = []
    for r in rows:
        out.append({
            "id": r.id, "actor": r.actor, "actor_label": ACTOR_LABELS.get(r.actor, r.actor),
            "manager_name": names.get(r.manager_id), "action": r.action,
            "action_label": ACTION_LABELS.get(r.action, r.action), "scope": r.scope,
            "details": r.get_details(), "created_at": _iso(r.created_at),
        })
    return out


_SYNC_POINT_ACTIONS = {"synced", "publish_verified", "imported"}


def changed_paths_since_sync(session, draft) -> list[str]:
    """Oxirgi sinxronizatsiya nuqtasidan (synced / publish_verified / imported
    audit yozuvi) keyin o'zgargan yo'llar -- `meta_publish.push_updates_to_meta`
    aynan qaysi maydonlarni Meta'ga yuborishini shu belgilaydi."""
    with db.scoped_as(draft.company_id):
        rows = (
            session.query(db.CampaignDraftEvent)
            .filter(db.CampaignDraftEvent.draft_id == draft.id)
            .order_by(db.CampaignDraftEvent.id.desc())
            .limit(500)
            .all()
        )
    paths: list[str] = []
    for r in rows:
        if r.action in _SYNC_POINT_ACTIONS:
            break
        if r.action == "ai_replan":
            return sorted(p for p in campaign_draft.ALLOWED_PATHS if campaign_draft.PATH_TYPES[p] != "dict")
        for p in (r.get_details().get("changed_paths") or []):
            if campaign_draft.is_allowed_path(p) and p not in paths:
                paths.append(p)
    return paths


def destination_options(objective: str, company, assets: "dict | None") -> list[dict]:
    """Yo'nalish (destination) tanlovlari + mavjudlik: Instagram Direct
    faqat IG ulangan bo'lsa, WhatsApp -- hozircha yo'q (Meta API orqali
    sozlanmagan). UI o'chiq variantni sabab bilan ko'rsatadi."""
    meta = campaign_draft.OBJECTIVE_META.get((objective or "").upper()) or {}
    ig_ids = [a.get("id") for a in ((assets or {}).get("instagram_accounts") or []) if a.get("id")]
    has_ig = bool(getattr(company, "ig_business_id", None) or ig_ids)
    out = []
    for d in meta.get("destination_types") or []:
        available, note = True, None
        if d == "INSTAGRAM_DIRECT" and not has_ig:
            available, note = False, "Instagram akkaunt ulanmagan -- " + _UNAVAILABLE_NOTE
        if d == "WHATSAPP":
            available, note = False, "WhatsApp raqami " + _UNAVAILABLE_NOTE
        out.append({"value": d, "label": DESTINATION_LABELS.get(d, d), "available": available, "note": note})
    return out


def serialize_draft(draft, assets: "dict | None", ctx: "dict | None", *, session=None, company=None, media_url_builder=None, base_url: "str | None" = None) -> dict:
    """JS ilovasi uchun qoralamaning TO'LIQ ko'rinishi. `session` berilsa
    media va audit-jurnal ham qo'shiladi (ro'yxat sahifasida kerak emas)."""
    state = draft.get_state()
    objective = (draft.objective or state.get("objective") or "MESSAGES").upper()
    field_sources = draft.get_field_sources()
    ai_plan = draft.get_ai_plan()
    try:
        validation = campaign_draft.validate_state(state, company=company, meta_assets=assets)
    except Exception as e:  # noqa: BLE001 -- buzilgan holat sahifani yiqitmasin
        logger.exception("validate_state yiqildi (draft=%s)", draft.id)
        validation = [{"scope": "campaign", "field": "state", "message": f"Holatni tekshirib bo'lmadi: {meta_api.safe_error_message(e)}"}]
    conn = connection_status(company, assets)
    gate = publish_gate(draft, company, assets) if company is not None else {"can_publish": False, "reasons": ["Kompaniya topilmadi."]}
    account = (assets or {}).get("ad_account") or {}
    currency = (state.get("adset") or {}).get("currency") or account.get("currency") or "UZS"
    has_meta_ids = bool(draft.meta_campaign_id and draft.meta_adset_id and draft.meta_ad_id)
    out = {
        "id": draft.id,
        "title": draft.title or (state.get("campaign") or {}).get("name") or f"Qoralama #{draft.id}",
        "objective": objective,
        "objective_label": campaign_draft.OBJECTIVE_LABELS.get(objective, objective),
        "objective_meta": campaign_draft.OBJECTIVE_META.get(objective, {}),
        "source": draft.source,
        "status": draft.status, "status_label": STATUS_LABELS.get(draft.status, draft.status),
        # 2026-09: standart -- ACTIVE (darhol ishga tushadi); foydalanuvchi
        # review oynasida PAUSED'ga o'tkazishi mumkin ("launch-status").
        "launch_active": bool(getattr(draft, "launch_active", True)),
        "sync_status": draft.sync_status or "local", "sync_label": SYNC_LABELS.get(draft.sync_status or "local", draft.sync_status),
        "approvals": {"campaign": bool(draft.campaign_approved), "adset": bool(draft.adset_approved), "ad": bool(draft.ad_approved)},
        "all_approved": bool(draft.all_approved),
        "meta": {"campaign_id": draft.meta_campaign_id, "adset_id": draft.meta_adset_id, "creative_id": draft.meta_creative_id,
                 "ad_id": draft.meta_ad_id, "lead_form_id": draft.meta_lead_form_id},
        "has_meta_ids": has_meta_ids,
        "is_editable": draft.status not in ("publishing", "archived"),
        "publish_step": draft.publish_step, "publish_error": draft.publish_error,
        # 2026-09: Meta'dan qaytgan XOM xatolik matni ham UI'ga chiqariladi
        # (avval faqat umumiy "tekshiring" degan qisqa xabar ko'rinardi --
        # aniq sababni topish qiyin edi). Bu faqat o'z kompaniyasiga tegishli
        # qoralama uchun, xavfsiz -- API kalitlar emas, Meta'ning javob matni.
        "last_meta_error_raw": draft.last_meta_error_raw,
        "last_synced_at": _iso(draft.last_synced_at), "created_at": _iso(draft.created_at), "updated_at": _iso(draft.updated_at),
        "state": state,
        "field_sources": field_sources,
        "source_labels": SOURCE_LABELS,
        "ai_plan": ai_plan,
        "validation": validation,
        "currency": currency,
        "connection": conn,
        "can_publish": gate["can_publish"], "publish_reasons": gate["reasons"],
        "assets": {
            "ad_account": account or None,
            "pages": (assets or {}).get("pages") or [],
            "instagram_accounts": (assets or {}).get("instagram_accounts") or [],
            "pixels": (assets or {}).get("pixels") or [],
            "custom_audiences": (assets or {}).get("custom_audiences") or [],
            "lead_forms": (assets or {}).get("lead_forms") or [],
            "recent_images": ((assets or {}).get("recent_images") or [])[:24],
            "errors": (assets or {}).get("errors") or [],
        },
        "options": {
            "objectives": [{"value": o, "label": campaign_draft.OBJECTIVE_LABELS[o]} for o in campaign_draft.OBJECTIVES],
            "cta": [{"value": c, "label": campaign_draft.CTA_LABELS.get(c, c)} for c in campaign_draft.CTA_TYPES],
            "placements": campaign_draft.PLACEMENT_OPTIONS,
            "destinations": destination_options(objective, company, assets),
            "unsupported": campaign_draft.META_UNSUPPORTED_UI_FIELDS,
            "preview_formats": list(meta_api.AD_PREVIEW_FORMATS.keys()),
            "special_ad_categories": sorted(campaign_draft.SPECIAL_AD_CATEGORIES),
            "lead_question_types": [
                {"value": t, "label": campaign_draft.LEAD_QUESTION_TYPE_LABELS.get(t, t)}
                for t in campaign_draft.LEAD_QUESTION_TYPES_ORDER
            ],
        },
        "company": {"id": getattr(company, "id", None), "name": getattr(company, "name", None), "phone": (ctx or {}).get("phone"),
                    "default_location": (ctx or {}).get("default_location")},
        "base_url": base_url or f"/avtopilot/{draft.id}",
    }
    if session is not None:
        out["media"] = media_for_draft(session, draft, media_url_builder)
        out["events"] = events_for_draft(session, draft)
    else:
        out["media"] = []
        out["events"] = []
    return out


def list_row(draft) -> dict:
    """Ro'yxat sahifasi uchun qisqa ko'rinish."""
    approved = sum(1 for f in (draft.campaign_approved, draft.adset_approved, draft.ad_approved) if f)
    objective = (draft.objective or "").upper()
    return {
        "id": draft.id, "title": draft.title or f"Qoralama #{draft.id}",
        "objective": objective, "objective_label": campaign_draft.OBJECTIVE_LABELS.get(objective, objective or "—"),
        "source": draft.source, "status": draft.status, "status_label": STATUS_LABELS.get(draft.status, draft.status),
        "sync_status": draft.sync_status or "local", "sync_label": SYNC_LABELS.get(draft.sync_status or "local", draft.sync_status),
        "approved": approved, "updated_at": draft.updated_at, "created_at": draft.created_at,
        "meta_campaign_id": draft.meta_campaign_id,
    }


# ---------------------------------------------------------------------------
# PATCH QUVURI (forma + chat uchun YAGONA)
# ---------------------------------------------------------------------------

def log_event(session, draft, *, actor: str, action: str, scope: "str | None" = None, details: "dict | None" = None, manager_id: "int | None" = None) -> None:
    """Audit-jurnalga yozadi; hech qachon asosiy oqimni to'xtatmaydi.
    (Commit chaqiruvchi zimmasida.)"""
    try:
        session.add(db.CampaignDraftEvent(
            company_id=draft.company_id, draft_id=draft.id, manager_id=manager_id, actor=actor,
            action=action, scope=scope, details_json=json.dumps(details or {}, ensure_ascii=False, default=str),
        ))
    except Exception as e:  # noqa: BLE001
        logger.warning("autopilot_web: event yozilmadi (%s): %s", action, e)


def apply_and_persist_patch(session, draft, patch: dict, *, source: str, manager_id: "int | None", actor: str,
                            action: str = "user_edit", details: "dict | None" = None, company=None, assets=None) -> dict:
    """Bitta patch'ni kanonik holatga qo'llaydi va saqlaydi:
      apply_patch (allowlist + tip) -> validate_state -> approvals_after_change
      -> sync_status ("local_changes" agar Meta'da bor bo'lsa) -> commit -> audit.
    Ruxsat etilmagan yo'l -> `campaign_draft.DraftPatchError` (holat
    O'ZGARMAYDI, chaqiruvchi 400 qaytaradi).
    Qaytaradi: {"changed_paths", "reset_scopes", "validation"}."""
    state = draft.get_state()
    sources = draft.get_field_sources()
    new_state, changed, new_sources = campaign_draft.apply_patch(state, patch, source=source, field_sources=sources)
    validation = campaign_draft.validate_state(new_state, company=company, meta_assets=assets)
    reset = campaign_draft.approvals_after_change(draft, changed)
    draft.set_state(new_state)
    draft.set_field_sources(new_sources)
    if "objective" in changed:
        draft.objective = new_state.get("objective")
    if "campaign.name" in changed and new_state.get("campaign", {}).get("name"):
        draft.title = new_state["campaign"]["name"][:255]
    if draft.meta_campaign_id and draft.status in ("published", "active", "failed"):
        draft.sync_status = "local_changes"
    draft.updated_at = dt.datetime.utcnow()
    info = dict(details or {})
    info.update({"scope": patch.get("scope"), "changed_paths": changed, "reset_approvals": reset,
                 "changes": {k: v for k, v in (patch.get("changes") or {}).items()}})
    log_event(session, draft, actor=actor, action=action, scope=patch.get("scope"), details=info, manager_id=manager_id)
    session.commit()
    return {"changed_paths": changed, "reset_scopes": reset, "validation": validation}


def duplicate_draft_for_new_adset(session, draft, *, label: "str | None", manager_id: "int | None") -> "db.CampaignDraft":
    """2026-09 bugfix ("ikkita ad set qilib ber ..." -> AI oldin "alohida
    so'rashni iltimos qiling" deb rad etardi): bu ilovada BITTA
    `CampaignDraft` = BITTA Campaign+Ad Set+Ad (Meta Marketing API bilan
    bir xil tuzilma) -- bitta qoralama ichida IKKINCHI ad set uchun sxemada
    joy yo'q. Shuning uchun "yana bir ad set" AYNAN shunday amalga
    oshiriladi: joriy qoralamaning TO'LIQ mustaqil nusxasi (holat, manba
    yorliqlari, AI reja, media) yangi `CampaignDraft` sifatida yaratiladi --
    chaqiruvchi (`ai_campaign_planner.chat_edit`ning `extra_ad_sets`i orqali,
    `app.py`da) shu ustiga o'sha auditoriyaning targeting patch'ini darhol
    qo'llaydi, foydalanuvchi ikkinchi marta yozishi SHART EMAS (bitta
    suhbat davrida ichki tsikl).

    Yangi qoralama Meta'ga HALI chiqarilmagan (meta_*_id yo'q, status=draft,
    uchala tasdiq bekor) -- alohida ko'rib chiqiladi/tasdiqlanadi/nashr
    qilinadi, xuddi qo'lda "nusxa olish" qilingandek."""
    state = copy.deepcopy(draft.get_state())
    sources = dict(draft.get_field_sources())
    base_title = (state.get("campaign") or {}).get("name") or draft.title or "Yangi kampaniya"
    if label:
        title = f"{base_title} | {label}"[:255]
        adset = state.setdefault("adset", {})
        adset["name"] = (f"{(adset.get('name') or '').strip()} | {label}".strip(" |"))[:255]
    else:
        title = base_title
    new_draft = db.CampaignDraft(
        company_id=draft.company_id, created_by_manager_id=manager_id,
        title=title, source="AI", status="draft",
        objective=state.get("objective"), sync_status="local",
        launch_active=getattr(draft, "launch_active", True),
    )
    new_draft.set_state(state)
    new_draft.set_field_sources(sources)
    new_draft.set_ai_plan(draft.get_ai_plan())
    session.add(new_draft)
    session.flush()
    # Media: fizik fayl bitta (storage_path o'zgarmaydi), faqat DB qatori
    # yangi `draft_id` bilan nusxalanadi -- shu orqali nashr quvuri
    # (`meta_publish._find_media_row`) yangi qoralamani MUSTAQIL deb ko'radi.
    with db.scoped_as(draft.company_id):
        media_rows = (
            session.query(db.CampaignDraftMedia)
            .filter(db.CampaignDraftMedia.draft_id == draft.id)
            .order_by(db.CampaignDraftMedia.id.asc())
            .all()
        )
    for m in media_rows:
        session.add(db.CampaignDraftMedia(
            company_id=draft.company_id, draft_id=new_draft.id, kind=m.kind, filename=m.filename,
            storage_path=m.storage_path, content_type=m.content_type, size_bytes=m.size_bytes,
            width=m.width, height=m.height, meta_image_hash=m.meta_image_hash, meta_video_id=m.meta_video_id,
            upload_status=m.upload_status, upload_error=m.upload_error, creative_asset_id=m.creative_asset_id,
        ))
    log_event(session, new_draft, actor="ai", action="ai_extra_adset_created", scope="all",
              details={"duplicated_from_draft_id": draft.id, "label": label}, manager_id=manager_id)
    session.commit()
    return new_draft


# ---------------------------------------------------------------------------
# PREVIEW
# ---------------------------------------------------------------------------

def local_preview_html(state: dict, *, page_name: "str | None", media_url: "str | None", fmt: str) -> str:
    """Meta'siz LOKAL ko'rinish -- ANIQ yorliq bilan ("Replix lokal
    ko'rinish — Meta preview emas"). Faqat matn/rasm joylashuvini ko'rsatadi."""
    ad = state.get("ad") or {}
    esc = html_stdlib.escape
    cta = campaign_draft.CTA_LABELS.get(ad.get("cta"), ad.get("cta") or "")
    story = fmt in ("instagram_story", "instagram_reels")
    media_html = (
        f'<img src="{esc(media_url)}" alt="" class="ap-lp-media">' if media_url
        else '<div class="ap-lp-media ap-lp-media-empty">Rasm/video yuklanmagan</div>'
    )
    return (
        f'<div class="ap-local-preview {"ap-local-preview-story" if story else ""}">'
        f'<div class="ap-lp-label">Replix lokal ko\'rinish — Meta preview emas</div>'
        f'<div class="ap-lp-head"><span class="ap-lp-avatar"></span><div><div class="ap-lp-page">{esc(page_name or "Sahifangiz")}</div><div class="ap-lp-sponsored">Reklama</div></div></div>'
        + ("" if story else f'<div class="ap-lp-text">{esc(ad.get("primary_text") or "")}</div>')
        + media_html
        + f'<div class="ap-lp-footer"><div><div class="ap-lp-headline">{esc(ad.get("headline") or "")}</div><div class="ap-lp-desc">{esc(ad.get("description") or "")}</div></div>'
        f'<span class="ap-lp-cta">{esc(cta)}</span></div>'
        + (f'<div class="ap-lp-text ap-lp-text-story">{esc(ad.get("primary_text") or "")}</div>' if story else "")
        + '</div>'
    )


def preview_for_draft(draft, company, assets: "dict | None", fmt: str, *, media_url: "str | None" = None) -> dict:
    """HAQIQIY Meta preview (iframe HTML) -- media Meta'ga yuklangan
    (image_hash/video_id) va sahifa tanlangan bo'lsa; aks holda lokal
    ko'rinish (`local: True`). Meta xatosi -> lokal + `error` matni
    (foydalanuvchiga Meta ko'rinishi deb ko'rsatilmaydi)."""
    fmt = fmt if fmt in meta_api.AD_PREVIEW_FORMATS else "instagram_feed"
    state = draft.get_state()
    ad = state.get("ad") or {}
    media = ad.get("media") or {}
    page_id = ad.get("page_id") or getattr(company, "meta_page_id", None)
    pages = (assets or {}).get("pages") or []
    page_name = next((p.get("name") for p in pages if str(p.get("id")) == str(page_id)), None)
    token = _company_token(company)
    ad_account_id = getattr(company, "meta_ad_account_id", None)
    can_meta = bool(token and ad_account_id and page_id and (media.get("image_hash") or media.get("video_id")))
    if not can_meta:
        reason = "Meta preview uchun rasm/video Meta'ga yuklangan va sahifa tanlangan bo'lishi kerak."
        return {"local": True, "fmt": fmt, "html": local_preview_html(state, page_name=page_name, media_url=media_url, fmt=fmt), "reason": reason}
    try:
        spec = campaign_draft.to_meta_creative_spec(
            state, page_id=str(page_id), instagram_actor_id=ad.get("instagram_actor_id"),
            image_hash=media.get("image_hash"), video_id=media.get("video_id"), lead_form_id=draft.meta_lead_form_id,
        )
        body = meta_api.generate_ad_preview(ad_account_id, spec, meta_api.AD_PREVIEW_FORMATS[fmt], access_token=token)
        return {"local": False, "fmt": fmt, "html": body}
    except Exception as e:  # noqa: BLE001 -- Meta rad etsa lokal ko'rinish + tushunarli xabar
        msg = meta_api.safe_error_message(e)
        logger.warning("generate_ad_preview (draft=%s, fmt=%s) xato: %s", draft.id, fmt, msg)
        return {"local": True, "fmt": fmt, "html": local_preview_html(state, page_name=page_name, media_url=media_url, fmt=fmt),
                "reason": "Meta preview olinmadi: " + msg}


# ---------------------------------------------------------------------------
# MEDIA -> holat
# ---------------------------------------------------------------------------

def media_patch_for_row(media_row, variant_index: "int | None" = None) -> dict:
    """Yuklangan/tanlangan media qatorini `ad.media` patch'iga aylantiradi
    (USER_OVERRIDDEN manba bilan qo'llanadi)."""
    return {"scope": "ad", "changes": {"ad.media": {
        "media_id": media_row.id,
        "image_hash": media_row.meta_image_hash if media_row.kind == "image" else None,
        "video_id": media_row.meta_video_id if media_row.kind == "video" else None,
        "selected_variant": variant_index,
    }}}


def media_from_creative_asset(session, draft, creative_asset, manager_id: "int | None") -> "db.CampaignDraftMedia":
    """2026-09, Kreativ studiya integratsiyasi: tayyor `CreativeAsset`ning
    yakuniy PNG'ini (`creative_studio.asset_image_bytes`) qoralama mediasi
    sifatida saqlaydi (`campaign_media.save_uploaded_media` -- oddiy
    yuklash bilan bir xil yo'l), `creative_asset_id` bog'lanishini yozadi va
    audit-jurnalga `media_selected` yozadi (commit qiladi). Meta'ga yuklash
    va `ad.media` patch'i chaqiruvchida (`try_upload_to_meta` +
    `apply_and_persist_patch`) -- xuddi qo'lda yuklashdagidek."""
    import creative_studio
    if creative_asset is None or creative_asset.company_id != draft.company_id:
        raise campaign_media.MediaError("Kreativ topilmadi.")
    if creative_asset.status != "ready" or not creative_asset.final_storage_path:
        raise campaign_media.MediaError("Bu kreativ hali tayyor emas -- avval rasmni yaratib, saqlang.")
    try:
        data = creative_studio.asset_image_bytes(creative_asset)
    except creative_studio.CreativeError as e:
        raise campaign_media.MediaError(str(e)) from e
    row = campaign_media.save_uploaded_media(
        session, company_id=draft.company_id, draft_id=draft.id, file_storage_or_bytes=data,
        filename=f"kreativ_{creative_asset.id}.png", content_type="image/png",
    )
    row.creative_asset_id = creative_asset.id
    log_event(session, draft, actor="user", action="media_selected", scope="ad",
              details={"media_id": row.id, "creative_asset_id": creative_asset.id, "source": "creative_studio"}, manager_id=manager_id)
    session.commit()
    return row


def try_upload_to_meta(session, media_row, company) -> "str | None":
    """Yuklangan faylni Meta'ga yuborishga urinadi; xato bo'lsa xabar
    qaytaradi (sahifa yiqilmaydi -- foydalanuvchi keyin qayta urinadi)."""
    if not _company_token(company) or not getattr(company, "meta_ad_account_id", None):
        return "Meta reklama hisobi ulanmagan -- fayl faqat lokal saqlandi (nashrdan oldin ulang)."
    try:
        campaign_media.ensure_uploaded_to_meta(session, media_row, company)
        return None
    except Exception as e:  # noqa: BLE001
        return meta_api.safe_error_message(e)


def regenerate_message(field: str) -> "str | None":
    """"Qayta yoz" tugmasi uchun tayyor chat buyruqlari (faqat ruxsat
    etilgan maydonlar)."""
    return {
        "primary_text": "Faqat asosiy matn (ad.primary_text) uchun yangi, kuchliroq va aniqroq variant yoz; ad.copy_variants.primary_text ro'yxatiga ham 3 ta yangi variant yoz.",
        "headline": "Faqat headline (ad.headline) uchun yangi, kuchliroq variant yoz; ad.copy_variants.headline ro'yxatiga ham 3 ta yangi variant yoz.",
        "description": "Faqat description (ad.description) uchun yangi, qisqa va aniq variant yoz; ad.copy_variants.description ro'yxatiga 3 ta variant yoz.",
        "messages": "Faqat xabar salomlashuvi (ad.messages.greeting) va 4-5 ta tezkor javob (ad.messages.quick_replies) uchun yangi, shu biznesga mos variant yoz.",
        "lead_form": "Faqat lead forma matnlarini (ad.lead_form.new_form.intro_headline, intro_description, thank_you_title, thank_you_body) yangi, kuchliroq qilib yoz.",
    }.get(field)
