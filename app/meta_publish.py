"""meta_publish.py — Meta Ads Autopilot: qoralamani Meta'ga CHIQARISH
(tranzaksiya), faollashtirish, sinxronizatsiya, import va yangilanishlarni
Meta'ga yuborish (2026-09, foydalanuvchi so'rovi: "Replix ... Ads
Manager'dagi har bir maydonni boshidan o'zi to'ldirmasligi kerak").

ASOSIY KAFOLATLAR:
  - Nashr FAQAT uchala daraja (campaign/adset/ad) tasdiqlangandan keyin
    (`validate_for_publish`).
  - Pipeline IDEMPOTENT va DAVOM ETTIRILADIGAN: har Meta obyekt yaratilgach
    ID DARHOL bazaga yoziladi (`session.commit()`), `publish_step` har
    qadam oldidan yangilanadi -- jarayon o'rtada uzilsa (crash/tarmoq),
    qayta urinish ALLAQACHON yaratilgan qadamlarni o'tkazib yuboradi,
    dublikat kampaniya/adset/reklama HECH QACHON yaratilmaydi.
  - Hammasi PAUSED holatda yaratiladi; ACTIVE qilish -- alohida, ANIQ
    `activate_draft()` qadami (foydalanuvchi pul sarflanishini o'zi
    boshlaydi).
  - Meta xatosi foydalanuvchiga XOM API matni bilan EMAS, tushunarli
    o'zbekcha xabar bilan ko'rsatiladi (`friendly_publish_error`); xom matn
    `last_meta_error_raw`da diagnostika uchun saqlanadi.
  - Har qadam `CampaignDraftEvent` audit-jurnaliga yoziladi.
"""

import json
import copy
import hashlib
import logging
import threading
import datetime as dt

import db
import plans
import meta_api
import campaign_draft
import campaign_media

logger = logging.getLogger("meta_publish")


class PublishError(Exception):
    """Nashr pipeline'i to'xtagan qadam + foydalanuvchiga ko'rsatiladigan
    xabar + xom Meta matni (faqat log/diagnostika uchun)."""

    def __init__(self, step: str, friendly: str, raw: "str | None" = None):
        self.step = step
        self.friendly = friendly
        self.raw = raw
        super().__init__(friendly)


# ---------------------------------------------------------------------------
# META AKTIVLARI (kesh)
# ---------------------------------------------------------------------------
_assets_cache: "dict[int, tuple[float, dict]]" = {}
_assets_cache_lock = threading.Lock()
_ASSETS_CACHE_TTL = 5 * 60


def invalidate_meta_assets_cache(company_id: "int | None") -> None:
    """Kompaniya Meta'ni qayta ulaganda / yangi forma-auditoriya
    yaratilganda chaqiriladi -- eskirgan ro'yxat ko'rsatilmasin."""
    with _assets_cache_lock:
        _assets_cache.pop(company_id, None)


def get_meta_assets(company, *, use_cache: bool = True) -> dict:
    """Kompaniya tokeni bilan Meta'dan barcha kerakli aktivlar ro'yxatini
    oladi: reklama hisobi (valyuta), sahifalar (+IG), IG akkauntlar,
    Pixel'lar, Custom auditoriyalar, Instant Form'lar, oldin yuklangan
    rasmlar, kampaniyalar (import uchun). HAR BIR sub-so'rov ALOHIDA
    himoyalangan -- bittasi xato bersa (masalan ruxsat yo'q), qolganlari
    baribir qaytadi; xatolar `errors` ro'yxatida. 5 daqiqa keshlanadi."""
    company_id = getattr(company, "id", None)
    if use_cache and company_id is not None:
        with _assets_cache_lock:
            cached = _assets_cache.get(company_id)
        if cached and (dt.datetime.utcnow().timestamp() - cached[0]) < _ASSETS_CACHE_TTL:
            return copy.deepcopy(cached[1])

    token = company.get_meta_access_token() if hasattr(company, "get_meta_access_token") else None
    ad_account_id = getattr(company, "meta_ad_account_id", None)
    page_id = getattr(company, "meta_page_id", None)
    assets: dict = {
        "ad_account": None, "pages": [], "instagram_accounts": [], "pixels": [], "custom_audiences": [],
        "lead_forms": [], "recent_images": [], "campaigns": [], "errors": [],
        "pixel_id": getattr(company, "meta_pixel_id", None), "page_id": page_id,
        "ig_business_id": getattr(company, "ig_business_id", None),
    }
    if not token:
        assets["errors"].append("Meta ulanmagan (token yo'q).")
        return assets

    def _try(name, fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 -- bitta aktiv ro'yxati xatosi qolganlarini to'xtatmasin
            logger.warning("get_meta_assets(%s): %s xato: %s", company_id, name, meta_api.safe_error_message(e))
            assets["errors"].append(f"{name}: {meta_api.safe_error_message(e)}")
            return None

    if ad_account_id:
        assets["ad_account"] = _try("ad_account", lambda: meta_api.get_ad_account_info(ad_account_id, access_token=token))
        assets["pixels"] = _try("pixels", lambda: meta_api.get_ad_account_pixels(ad_account_id, token)) or []
        assets["custom_audiences"] = _try("custom_audiences", lambda: meta_api.list_custom_audiences(ad_account_id, access_token=token)) or []
        assets["recent_images"] = _try("recent_images", lambda: meta_api.list_ad_images(ad_account_id, access_token=token)) or []
        assets["campaigns"] = _try("campaigns", lambda: meta_api.list_ad_account_campaigns(ad_account_id, access_token=token)) or []
    pages = _try("pages", lambda: meta_api.oauth_list_pages(token)) or []
    assets["pages"] = [{"id": p.get("id"), "name": p.get("name"), "instagram_business_account": p.get("instagram_business_account")} for p in pages]
    ig_accounts = []
    seen_ig = set()
    for p in pages:
        ig = p.get("instagram_business_account") or {}
        if ig.get("id") and ig["id"] not in seen_ig:
            seen_ig.add(ig["id"])
            ig_accounts.append({"id": ig["id"], "username": ig.get("username"), "page_id": p.get("id")})
    if assets["ig_business_id"] and assets["ig_business_id"] not in seen_ig:
        ig_accounts.append({"id": assets["ig_business_id"], "username": None, "page_id": page_id})
    assets["instagram_accounts"] = ig_accounts
    if page_id:
        assets["lead_forms"] = _try("lead_forms", lambda: meta_api.get_lead_forms(page_id, access_token=token)) or []
        if not any(str(p.get("id")) == str(page_id) for p in assets["pages"]):
            assets["pages"].append({"id": page_id, "name": None, "instagram_business_account": None})

    if company_id is not None:
        with _assets_cache_lock:
            _assets_cache[company_id] = (dt.datetime.utcnow().timestamp(), copy.deepcopy(assets))
    return assets


# ---------------------------------------------------------------------------
# YORDAMCHILAR
# ---------------------------------------------------------------------------

def _log_event(session, draft, *, actor: str, action: str, scope: "str | None" = None, details: "dict | None" = None, manager_id: "int | None" = None) -> None:
    try:
        session.add(db.CampaignDraftEvent(
            company_id=draft.company_id, draft_id=draft.id, manager_id=manager_id, actor=actor,
            action=action, scope=scope, details_json=json.dumps(details or {}, ensure_ascii=False, default=str),
        ))
    except Exception as e:  # noqa: BLE001 -- audit-jurnal asosiy oqimni buzmasin
        logger.warning("meta_publish: event yozilmadi (%s): %s", action, e)


def _raw_error_text(e: Exception) -> str:
    if isinstance(e, meta_api.MetaAPIError) and e.args and isinstance(e.args[0], dict):
        try:
            return json.dumps(e.args[0], ensure_ascii=False)
        except (TypeError, ValueError):
            return str(e.args[0])
    return f"{type(e).__name__}"


def friendly_publish_error(e: Exception, step: str) -> str:
    """Meta xatosini foydalanuvchi tushunadigan o'zbekcha xabarga
    aylantiradi. Kod/matn bo'yicha eng ko'p uchraydigan holatlar alohida,
    qolgani umumiy xabar (xom API matni ekranga chiqmaydi)."""
    err = e.args[0] if isinstance(e, meta_api.MetaAPIError) and e.args and isinstance(e.args[0], dict) else {}
    code = err.get("code")
    subcode = err.get("error_subcode")
    text = " ".join(str(err.get(k) or "") for k in ("message", "error_user_msg", "error_user_title")).lower()
    if code == 190 or "access token" in text and ("expired" in text or "invalid" in text or "session" in text):
        return "Meta ulanishi muddati tugagan -- Sozlamalar'dan Facebook'ni qayta ulang."
    if code in (10, 200, 294) or "permission" in text or "ads_management" in text:
        return "Meta ruxsati yetarli emas -- Facebook'ni qayta ulab, reklama boshqaruvi (ads_management) ruxsatini bering."
    if "instagram" in text and ("not connected" in text or "connect" in text or "actor" in text or "linked" in text):
        return "Instagram akkaunt reklama akkauntiga ulanmagan. Meta Business Suite'da Instagram'ni sahifaga ulang."
    if "pixel" in text or (step == "adset" and "promoted_object" in text):
        return "Bu maqsad uchun Pixel tanlash kerak (Meta Events Manager)."
    if "page" in text and ("not" in text and ("own" in text or "admin" in text or "access" in text)):
        return "Bu sahifa kompaniyaga ulanmagan yoki unga ruxsat yo'q."
    if "targeting" in text or "audience" in text or "geo" in text or "location" in text or subcode in (1487079, 1487760):
        return "Tanlangan targeting Meta tomonidan qabul qilinmadi. Hudud/yosh/qiziqishlarni tekshirib qayta urinib ko'ring."
    if "budget" in text or "minimum" in text and "amount" in text:
        return "Byudjet Meta'ning minimal chegarasidan kam -- kunlik byudjetni oshiring."
    if "image" in text or "video" in text or "creative" in text or step == "creative":
        return "Kreativ (rasm/video/matn) Meta tomonidan qabul qilinmadi. Rasm hajmi va matnni tekshiring."
    if "lead" in text and "form" in text or step == "lead_form":
        return "Instant Form yaratib bo'lmadi -- savollar va maxfiylik havolasini tekshiring."
    if code in (4, 17, 32, 613) or "rate" in text and "limit" in text:
        return "Meta so'rovlar chegarasi -- bir necha daqiqadan keyin qayta urinib ko'ring."
    if err.get("message"):
        return f"Meta xatosi ({step} bosqichi): {err['message']}"
    return "Meta bilan bog'lanishda xatolik yuz berdi. Birozdan keyin qayta urinib ko'ring."


def _state_hash(state: dict) -> str:
    return hashlib.sha256(json.dumps(state or {}, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def _expected_snapshot(draft, state: dict) -> dict:
    """Biz Meta'ga chiqargan (kutilayotgan) holatning qisqa ko'rinishi --
    sinxronizatsiyada Meta'dagi haqiqiy holat bilan solishtiriladi."""
    adset = state.get("adset") or {}
    t = adset.get("targeting") or {}
    status = "ACTIVE" if draft.status == "active" else "PAUSED"
    currency = adset.get("currency") or "UZS"
    budget = adset.get("daily_budget") if (adset.get("budget_type") or "daily") == "daily" else None
    return {
        "campaign": {"name": (state.get("campaign") or {}).get("name"), "status": status},
        "adset": {
            "name": adset.get("name"), "status": status,
            "daily_budget": campaign_draft.to_minor_units(budget, currency) if budget else None,
            "age_min": t.get("age_min"), "age_max": t.get("age_max"),
            "end_time": adset.get("end_time"),
        },
        "ad": {"name": (state.get("ad") or {}).get("name"), "status": status},
    }


def _meta_snapshot(campaign: dict, adset: dict, ad: dict) -> dict:
    t = adset.get("targeting") or {}
    return {
        "campaign": {"name": campaign.get("name"), "status": campaign.get("status")},
        "adset": {
            "name": adset.get("name"), "status": adset.get("status"),
            "daily_budget": int(adset["daily_budget"]) if adset.get("daily_budget") not in (None, "") else None,
            "age_min": t.get("age_min"), "age_max": t.get("age_max"),
            "end_time": adset.get("end_time"),
        },
        "ad": {"name": ad.get("name"), "status": ad.get("status")},
    }


def _same_time(a, b) -> bool:
    """Ikki ISO vaqtni solishtiradi. Biz Meta'ga vaqtni zonasiz (naive)
    yuboramiz -- Meta uni reklama hisobi zonasida talqin qiladi va
    o'qiganda O'SHA zonaning offset'i bilan ("+0500") qaytaradi. Shuning
    uchun biror tomon zonasiz bo'lsa, DEVOR SOATI (offset'siz qismi)
    solishtiriladi; ikkalasi ham zonali bo'lsa -- haqiqiy lahza."""
    if not a or not b:
        return True  # biror tomonda yo'q bo'lsa solishtirmaymiz
    try:
        da = dt.datetime.fromisoformat(str(a).replace("Z", "+00:00"))
        db_ = dt.datetime.fromisoformat(str(b).replace("Z", "+00:00"))
        if da.tzinfo is None or db_.tzinfo is None:
            da = da.replace(tzinfo=None)
            db_ = db_.replace(tzinfo=None)
        return abs((da - db_).total_seconds()) < 60
    except ValueError:
        return str(a) == str(b)


def _diff_snapshots(expected: dict, actual: dict) -> list[str]:
    diffs: list[str] = []
    labels = {"campaign": "Kampaniya", "adset": "Ad Set", "ad": "Reklama"}
    for level in ("campaign", "adset", "ad"):
        exp = expected.get(level) or {}
        act = actual.get(level) or {}
        for key, val in exp.items():
            if val is None or key not in act:
                continue
            if key == "end_time":
                if not _same_time(val, act.get(key)):
                    diffs.append(f"{labels[level]}: tugash sanasi farq qiladi")
                continue
            if str(val) != str(act.get(key)):
                diffs.append(f"{labels[level]}: {key} farq qiladi (biz: {val}, Meta: {act.get(key)})")
    return diffs


def _promoted_object(objective: str, *, page_id: "str | None", pixel_id: "str | None") -> "dict | None":
    """Maqsadga qarab `promoted_object` (Meta v21 hujjati bo'yicha):
    MESSAGES/LEADS/CALLS -> {page_id}; SALES -> {pixel_id, custom_event_type:
    PURCHASE}; TRAFFIC/AWARENESS/ENGAGEMENT -> yo'q."""
    if objective in ("MESSAGES", "LEADS", "CALLS"):
        return {"page_id": str(page_id)} if page_id else None
    if objective == "SALES":
        return {"pixel_id": str(pixel_id), "custom_event_type": "PURCHASE"} if pixel_id else None
    return None


def _find_media_row(session, draft, state: dict):
    media = (state.get("ad") or {}).get("media") or {}
    with db.scoped_as(draft.company_id):
        if media.get("media_id"):
            row = session.get(db.CampaignDraftMedia, int(media["media_id"]))
            if row is not None and row.draft_id == draft.id:
                return row
        return (
            session.query(db.CampaignDraftMedia)
            .filter(db.CampaignDraftMedia.draft_id == draft.id)
            .order_by(db.CampaignDraftMedia.id.desc())
            .first()
        )


# ---------------------------------------------------------------------------
# NASHR OLDI TEKSHIRUVI
# ---------------------------------------------------------------------------

def validate_for_publish(draft, company, assets: "dict | None") -> list[str]:
    """Nashrdan oldingi barcha tekshiruvlar -- xabarlar ro'yxati (bo'sh =
    nashr qilsa bo'ladi): tarif ruxsati, token/reklama hisobi, uchala
    tasdiq, `campaign_draft.validate_state` (egalik bilan), media
    yuklanganligi, maqsadga xos talablar."""
    messages: list[str] = []
    plan = plans.get_plan(getattr(company, "plan", None))
    if not plan.can_connect_meta_ads:
        messages.append("Joriy tarifda Meta reklama ulash imkoni yo'q -- tarifni yangilang.")
    token = company.get_meta_access_token() if hasattr(company, "get_meta_access_token") else None
    if not token or not getattr(company, "meta_ad_account_id", None):
        messages.append("Meta reklama hisobi ulanmagan -- Sozlamalar > Ulanishlar orqali ulang.")
    if not draft.campaign_approved:
        messages.append("Kampaniya darajasi hali tasdiqlanmagan.")
    if not draft.adset_approved:
        messages.append("Ad Set darajasi hali tasdiqlanmagan.")
    if not draft.ad_approved:
        messages.append("Reklama (Ad) darajasi hali tasdiqlanmagan.")
    if draft.status in ("publishing",):
        messages.append("Nashr allaqachon davom etmoqda.")
    if draft.status in ("published", "active"):
        messages.append("Bu qoralama allaqachon Meta'ga chiqarilgan.")
    state = draft.get_state()
    for err in campaign_draft.validate_state(state, company=company, meta_assets=assets):
        messages.append(err["message"])
    return messages


# ---------------------------------------------------------------------------
# NASHR (tranzaksiya)
# ---------------------------------------------------------------------------

def _fail(session, draft, step: str, e: Exception, manager_id) -> PublishError:
    friendly = friendly_publish_error(e, step)
    raw = _raw_error_text(e)
    draft.status = "failed"
    draft.publish_step = step
    draft.publish_error = friendly
    draft.last_meta_error_raw = raw
    draft.sync_status = "sync_error"
    _log_event(session, draft, actor="system", action="meta_error", scope=None, details={"step": step, "error": friendly, "raw": raw}, manager_id=manager_id)
    _log_event(session, draft, actor="system", action="publish_failed", scope=None, details={"step": step, "error": friendly}, manager_id=manager_id)
    session.commit()
    logger.warning("publish_draft(%s) %s bosqichida xato: %s", draft.id, step, raw)
    return PublishError(step, friendly, raw)


def publish_draft(session, draft, company, *, manager_id: "int | None" = None) -> dict:
    """Qoralamani Meta'ga chiqaradi (hammasi PAUSED). Qadamlar:
    validate -> upload_media -> lead_form -> campaign -> adset -> creative ->
    ad -> verify. Har muvaffaqiyatli Meta yaratuvidan keyin ID DARHOL
    saqlanadi -- qayta urinish yaratilgan qadamlarni o'tkazib yuboradi.
    Xato: `PublishError(step, friendly, raw)`; qoralama `status="failed"`,
    `publish_step` -- to'xtagan qadam."""
    warnings: list[str] = []
    token = company.get_meta_access_token()
    ad_account_id = company.meta_ad_account_id

    # --- validate
    previous_step = draft.publish_step
    # "publishing" holatida qotib qolgan (process o'lgan) qoralama 10 daqiqadan
    # keyin qayta urinishga ruxsat etiladi -- aks holda uni hech qachon
    # davom ettirib bo'lmasdi; 10 daqiqa ichida esa parallel ikkinchi nashr
    # bloklanadi (dublikat xavfi).
    stuck = draft.status == "publishing" and draft.updated_at is not None and (dt.datetime.utcnow() - draft.updated_at) > dt.timedelta(minutes=10)
    resume = draft.status == "failed" or stuck
    draft.publish_step = "validate"
    assets = get_meta_assets(company)
    problems = _validate_resume(draft, company, assets) if resume else validate_for_publish(draft, company, assets)
    if problems:
        draft.publish_step = previous_step
        session.commit()
        raise PublishError("validate", " ".join(problems))
    state = draft.get_state()
    objective = state["objective"]
    draft.status = "publishing"
    draft.sync_status = "publishing"
    draft.publish_error = None
    _log_event(session, draft, actor="user" if manager_id else "system", action="publish_started", scope="all", details={"resume_from": draft.publish_step}, manager_id=manager_id)
    session.commit()

    currency = ((assets.get("ad_account") or {}).get("currency")) or state["adset"].get("currency") or "UZS"
    page_id = state["ad"].get("page_id") or company.meta_page_id
    ig_actor = state["ad"].get("instagram_actor_id") or None
    pixel_id = assets.get("pixel_id") or company.meta_pixel_id

    # --- upload_media
    draft.publish_step = "upload_media"
    session.commit()
    image_hash = state["ad"]["media"].get("image_hash")
    video_id = state["ad"]["media"].get("video_id")
    media_row = _find_media_row(session, draft, state)
    if media_row is not None and not (image_hash or video_id):
        try:
            campaign_media.ensure_uploaded_to_meta(session, media_row, company)
        except Exception as e:  # noqa: BLE001 -- friendly xatoga aylantiriladi
            raise _fail(session, draft, "upload_media", e, manager_id)
        state["ad"]["media"]["media_id"] = media_row.id
        if media_row.kind == "image":
            image_hash = state["ad"]["media"]["image_hash"] = media_row.meta_image_hash
        else:
            video_id = state["ad"]["media"]["video_id"] = media_row.meta_video_id
        draft.set_state(state)
        session.commit()
    if not (image_hash or video_id):
        raise _fail(session, draft, "upload_media", meta_api.MetaAPIError({"message": "Rasm yoki video yuklanmagan."}), manager_id)

    # --- lead_form (faqat LEADS)
    lead_form_id = draft.meta_lead_form_id
    if objective == "LEADS":
        draft.publish_step = "lead_form"
        session.commit()
        lf = state["ad"].get("lead_form") or {}
        if lf.get("mode") == "existing":
            lead_form_id = lf.get("existing_form_id")
            draft.meta_lead_form_id = lead_form_id
            session.commit()
        elif not lead_form_id:
            try:
                res = meta_api.create_lead_form(page_id, campaign_draft.lead_form_config_from_state(state), access_token=token)
                lead_form_id = str(res.get("id"))
            except Exception as e:  # noqa: BLE001
                raise _fail(session, draft, "lead_form", e, manager_id)
            draft.meta_lead_form_id = lead_form_id
            _log_event(session, draft, actor="system", action="meta_lead_form_created", scope="ad", details={"lead_form_id": lead_form_id}, manager_id=manager_id)
            session.commit()

    # --- campaign
    draft.publish_step = "campaign"
    session.commit()
    if not draft.meta_campaign_id:
        try:
            res = meta_api.create_campaign(
                state["campaign"]["name"], state["campaign"]["meta_objective"], "PAUSED",
                state["campaign"].get("special_ad_categories") or [],
                access_token=token, ad_account_id=ad_account_id,
            )
        except Exception as e:  # noqa: BLE001
            raise _fail(session, draft, "campaign", e, manager_id)
        draft.meta_campaign_id = str(res.get("id"))
        _log_event(session, draft, actor="system", action="meta_campaign_created", scope="campaign", details={"campaign_id": draft.meta_campaign_id}, manager_id=manager_id)
        session.commit()

    # --- adset
    draft.publish_step = "adset"
    session.commit()
    if not draft.meta_adset_id:
        adset = state["adset"]
        budget_type = adset.get("budget_type") or "daily"
        daily_cents = campaign_draft.to_minor_units(adset.get("daily_budget"), currency) if budget_type == "daily" else 0
        lifetime_cents = campaign_draft.to_minor_units(adset.get("lifetime_budget"), currency) if budget_type == "lifetime" else None
        try:
            res = meta_api.create_adset(
                draft.meta_campaign_id, adset["name"], daily_cents, campaign_draft.to_meta_targeting(state),
                adset["optimization_goal"], adset.get("billing_event") or "IMPRESSIONS",
                adset.get("bid_strategy") or "LOWEST_COST_WITHOUT_CAP", "PAUSED",
                _promoted_object(objective, page_id=page_id, pixel_id=pixel_id),
                access_token=token, ad_account_id=ad_account_id,
                lifetime_budget_cents=lifetime_cents, start_time=adset.get("start_time"), end_time=adset.get("end_time"),
                destination_type=adset.get("destination_type"),
            )
        except Exception as e:  # noqa: BLE001
            raise _fail(session, draft, "adset", e, manager_id)
        draft.meta_adset_id = str(res.get("id"))
        _log_event(session, draft, actor="system", action="meta_adset_created", scope="adset", details={"adset_id": draft.meta_adset_id}, manager_id=manager_id)
        session.commit()

    # --- creative
    draft.publish_step = "creative"
    session.commit()
    if not draft.meta_creative_id:
        spec = campaign_draft.to_meta_creative_spec(
            state, page_id=page_id, instagram_actor_id=ig_actor, image_hash=image_hash, video_id=video_id, lead_form_id=lead_form_id,
        )
        try:
            res = meta_api.create_ad_creative(ad_account_id, f"{state['ad']['name']} — kreativ", spec, access_token=token)
        except Exception as e:  # noqa: BLE001
            raise _fail(session, draft, "creative", e, manager_id)
        draft.meta_creative_id = str(res.get("id"))
        _log_event(session, draft, actor="system", action="meta_creative_created", scope="ad", details={"creative_id": draft.meta_creative_id}, manager_id=manager_id)
        session.commit()

    # --- ad
    draft.publish_step = "ad"
    session.commit()
    if not draft.meta_ad_id:
        try:
            res = meta_api.create_ad(draft.meta_adset_id, state["ad"]["name"], draft.meta_creative_id, "PAUSED", access_token=token, ad_account_id=ad_account_id)
        except Exception as e:  # noqa: BLE001
            raise _fail(session, draft, "ad", e, manager_id)
        draft.meta_ad_id = str(res.get("id"))
        _log_event(session, draft, actor="system", action="meta_ad_created", scope="ad", details={"ad_id": draft.meta_ad_id}, manager_id=manager_id)
        session.commit()

    # --- verify (xato bo'lsa nashr baribir muvaffaqiyatli -- faqat ogohlantirish)
    draft.publish_step = "verify"
    session.commit()
    snapshot = None
    try:
        c = meta_api.get_campaign_basic(draft.meta_campaign_id, access_token=token)
        a = meta_api.get_adset_basic(draft.meta_adset_id, access_token=token)
        d = meta_api.get_ad_basic(draft.meta_ad_id, access_token=token)
        snapshot = _meta_snapshot(c, a, d)
        warnings.extend(_diff_snapshots(_expected_snapshot(draft, state), snapshot))
    except Exception as e:  # noqa: BLE001
        warnings.append("Nashrdan keyingi tekshiruv bajarilmadi: " + meta_api.safe_error_message(e))

    draft.status = "published"
    draft.sync_status = "synced"
    draft.publish_step = None
    draft.publish_error = None
    draft.last_synced_at = dt.datetime.utcnow()
    draft.set_meta_snapshot({"meta": snapshot, "published_state_hash": _state_hash(state), "synced_at": draft.last_synced_at.isoformat()})
    _log_event(session, draft, actor="system", action="publish_verified", scope="all", details={
        "campaign_id": draft.meta_campaign_id, "adset_id": draft.meta_adset_id, "creative_id": draft.meta_creative_id,
        "ad_id": draft.meta_ad_id, "warnings": warnings,
    }, manager_id=manager_id)
    session.commit()
    invalidate_meta_assets_cache(company.id)
    return {
        "campaign_id": draft.meta_campaign_id, "adset_id": draft.meta_adset_id,
        "creative_id": draft.meta_creative_id, "ad_id": draft.meta_ad_id, "warnings": warnings,
    }


def _validate_resume(draft, company, assets) -> list[str]:
    """`status="failed"` qoralamani QAYTA urinishda "allaqachon chiqarilgan"
    tekshiruvi qo'llanmaydi, qolganlari (tasdiqlar, holat) bir xil."""
    messages: list[str] = []
    plan = plans.get_plan(getattr(company, "plan", None))
    if not plan.can_connect_meta_ads:
        messages.append("Joriy tarifda Meta reklama ulash imkoni yo'q -- tarifni yangilang.")
    token = company.get_meta_access_token() if hasattr(company, "get_meta_access_token") else None
    if not token or not getattr(company, "meta_ad_account_id", None):
        messages.append("Meta reklama hisobi ulanmagan -- Sozlamalar > Ulanishlar orqali ulang.")
    if not draft.all_approved:
        messages.append("Uchala daraja ham tasdiqlanishi kerak.")
    for err in campaign_draft.validate_state(draft.get_state(), company=company, meta_assets=assets):
        messages.append(err["message"])
    return messages


# ---------------------------------------------------------------------------
# FAOLLASHTIRISH
# ---------------------------------------------------------------------------

def activate_draft(session, draft, company, *, manager_id: "int | None" = None) -> dict:
    """Nashr qilingan (PAUSED) kampaniyani ANIQ foydalanuvchi buyrug'i bilan
    ACTIVE qiladi -- tartib: campaign -> adset -> ad (yuqori daraja
    o'chiq bo'lsa pastkisi baribir ko'rsatilmaydi). Xato bo'lsa
    `PublishError("activate", ...)`, `status` o'zgarmaydi."""
    if draft.status not in ("published",):
        raise PublishError("activate", "Faollashtirish uchun qoralama avval Meta'ga chiqarilgan (published) bo'lishi kerak.")
    if not (draft.meta_campaign_id and draft.meta_adset_id and draft.meta_ad_id):
        raise PublishError("activate", "Meta obyektlari to'liq yaratilmagan -- avval nashrni yakunlang.")
    token = company.get_meta_access_token()
    done: list[str] = []
    try:
        for label, object_id in (("campaign", draft.meta_campaign_id), ("adset", draft.meta_adset_id), ("ad", draft.meta_ad_id)):
            meta_api.activate_object(object_id, access_token=token)
            done.append(label)
    except Exception as e:  # noqa: BLE001
        friendly = friendly_publish_error(e, "activate")
        draft.publish_error = friendly
        draft.last_meta_error_raw = _raw_error_text(e)
        _log_event(session, draft, actor="system", action="meta_error", scope=None, details={"step": "activate", "done": done, "error": friendly}, manager_id=manager_id)
        session.commit()
        raise PublishError("activate", friendly, draft.last_meta_error_raw) from e
    draft.status = "active"
    draft.publish_error = None
    snap = draft.get_meta_snapshot()
    meta_snap = snap.get("meta") or {}
    for level in ("campaign", "adset", "ad"):
        if isinstance(meta_snap.get(level), dict):
            meta_snap[level]["status"] = "ACTIVE"
    snap["meta"] = meta_snap
    draft.set_meta_snapshot(snap)
    _log_event(session, draft, actor="user" if manager_id else "system", action="activated", scope="all", details={"order": done}, manager_id=manager_id)
    session.commit()
    return {"activated": done}


# ---------------------------------------------------------------------------
# SINXRONIZATSIYA
# ---------------------------------------------------------------------------

def sync_draft_from_meta(session, draft, company) -> str:
    """Nashr qilingan qoralama uchun Meta'dagi haqiqiy holatni o'qib,
    `sync_status`ni aniqlaydi: "meta_changed" (Meta'da kimdir Ads
    Manager'dan o'zgartirgan), "local_changes" (biz chiqargan holatdan
    keyin mahalliy tahrir bor), "synced", xatoda "sync_error". Meta'dan
    o'qilgan qisqa holat `meta_snapshot_json`ga yoziladi."""
    if not (draft.meta_campaign_id and draft.meta_adset_id and draft.meta_ad_id):
        return draft.sync_status or "local"
    token = company.get_meta_access_token()
    try:
        c = meta_api.get_campaign_basic(draft.meta_campaign_id, access_token=token)
        a = meta_api.get_adset_basic(draft.meta_adset_id, access_token=token)
        d = meta_api.get_ad_basic(draft.meta_ad_id, access_token=token)
    except Exception as e:  # noqa: BLE001
        draft.sync_status = "sync_error"
        draft.last_meta_error_raw = _raw_error_text(e)
        _log_event(session, draft, actor="system", action="meta_error", details={"step": "sync", "error": meta_api.safe_error_message(e)})
        session.commit()
        return "sync_error"
    state = draft.get_state()
    actual = _meta_snapshot(c, a, d)
    prev = draft.get_meta_snapshot()
    published_hash = prev.get("published_state_hash")
    expected = _expected_snapshot(draft, state)
    # Meta'dagi holat biz kutgan (oxirgi chiqarilgan/sinxronlangan) holatdan farq qiladimi?
    baseline = prev.get("meta") or expected
    meta_diffs = _diff_snapshots(baseline, actual)
    local_changed = bool(published_hash) and published_hash != _state_hash(state)
    if meta_diffs:
        status = "meta_changed"
    elif local_changed:
        status = "local_changes"
    else:
        status = "synced"
    # Meta status bo'yicha mahalliy statusni ham moslaymiz (ACTIVE/PAUSED)
    if actual["campaign"].get("status") == "ACTIVE" and draft.status == "published":
        draft.status = "active"
    elif actual["campaign"].get("status") == "PAUSED" and draft.status == "active":
        draft.status = "published"
    draft.sync_status = status
    draft.last_synced_at = dt.datetime.utcnow()
    draft.set_meta_snapshot({"meta": actual, "published_state_hash": published_hash or _state_hash(state), "synced_at": draft.last_synced_at.isoformat(), "diffs": meta_diffs})
    _log_event(session, draft, actor="system", action="synced", scope="all", details={"sync_status": status, "diffs": meta_diffs})
    session.commit()
    return status


# ---------------------------------------------------------------------------
# IMPORT (Meta'dagi mavjud kampaniyani qoralamaga aylantirish)
# ---------------------------------------------------------------------------
_LEGACY_OBJECTIVE_MAP = {
    "MESSAGES": "MESSAGES", "LEAD_GENERATION": "LEADS", "CONVERSIONS": "SALES", "LINK_CLICKS": "TRAFFIC",
    "REACH": "AWARENESS", "BRAND_AWARENESS": "AWARENESS", "POST_ENGAGEMENT": "ENGAGEMENT", "VIDEO_VIEWS": "ENGAGEMENT",
}
_KNOWN_TARGETING_KEYS = {
    "geo_locations", "age_min", "age_max", "genders", "locales", "flexible_spec", "custom_audiences",
    "excluded_custom_audiences", "targeting_automation", "publisher_platforms", "facebook_positions",
    "instagram_positions", "messenger_positions", "device_platforms", "targeting_optimization", "brand_safety_content_filter_levels",
}


def _reverse_objective(meta_objective: str, optimization_goal: "str | None", destination_type: "str | None") -> str:
    mo = (meta_objective or "").upper()
    og = (optimization_goal or "").upper()
    dest = (destination_type or "").upper()
    if mo == "OUTCOME_ENGAGEMENT":
        if og == "CONVERSATIONS" or dest in ("MESSENGER", "INSTAGRAM_DIRECT", "WHATSAPP"):
            return "MESSAGES"
        return "ENGAGEMENT"
    if mo == "OUTCOME_LEADS":
        if og == "QUALITY_CALL" or dest == "PHONE_CALL":
            return "CALLS"
        return "LEADS"
    if mo == "OUTCOME_SALES":
        return "SALES"
    if mo == "OUTCOME_TRAFFIC":
        return "TRAFFIC"
    if mo == "OUTCOME_AWARENESS":
        return "AWARENESS"
    return _LEGACY_OBJECTIVE_MAP.get(mo, "ENGAGEMENT")


def state_from_campaign_tree(tree: dict, *, company=None, currency: "str | None" = None) -> tuple[dict, list[str]]:
    """`meta_api.get_campaign_tree()` natijasini kanonik holatga TESKARI
    tarjima qiladi (birinchi adset + birinchi reklama). Replix hali
    qo'llamaydigan sozlamalar ogohlantirishga tushadi."""
    warnings: list[str] = []
    adsets = tree.get("adsets") or []
    adset_raw = adsets[0] if adsets else {}
    ads = adset_raw.get("ads") or []
    ad_raw = ads[0] if ads else {}
    creative = ad_raw.get("creative") or {}
    spec = creative.get("object_story_spec") or {}
    objective = _reverse_objective(tree.get("objective"), adset_raw.get("optimization_goal"), adset_raw.get("destination_type"))
    state = campaign_draft.new_empty_state(objective)

    if len(adsets) > 1:
        warnings.append(f"Kampaniyada {len(adsets)} ta Ad Set bor -- faqat birinchisi import qilindi.")
    if len(ads) > 1:
        warnings.append(f"Ad Set'da {len(ads)} ta reklama bor -- faqat birinchisi import qilindi.")
    if tree.get("daily_budget") or tree.get("lifetime_budget"):
        warnings.append("Kampaniya darajasidagi byudjet (CBO) Replix'da hali qo'llanmaydi -- byudjet Ad Set'da boshqariladi.")

    state["campaign"]["name"] = tree.get("name") or ""
    state["campaign"]["special_ad_categories"] = [c for c in (tree.get("special_ad_categories") or []) if c in campaign_draft.SPECIAL_AD_CATEGORIES]

    adset = state["adset"]
    adset["name"] = adset_raw.get("name") or ""
    cur = (currency or adset.get("currency") or "UZS").upper()
    adset["currency"] = cur
    if adset_raw.get("lifetime_budget") not in (None, "", "0", 0):
        adset["budget_type"] = "lifetime"
        adset["lifetime_budget"] = campaign_draft.from_minor_units(adset_raw["lifetime_budget"], cur)
    else:
        adset["budget_type"] = "daily"
        adset["daily_budget"] = campaign_draft.from_minor_units(adset_raw.get("daily_budget"), cur) if adset_raw.get("daily_budget") else None
    for key in ("start_time", "end_time"):
        if adset_raw.get(key):
            try:
                adset[key] = campaign_draft._parse_iso(adset_raw[key])
            except campaign_draft.DraftPatchError:
                adset[key] = None
    if adset_raw.get("bid_strategy") in campaign_draft.BID_STRATEGIES:
        adset["bid_strategy"] = adset_raw["bid_strategy"]
    if adset_raw.get("destination_type") in campaign_draft.DESTINATION_TYPES:
        adset["destination_type"] = adset_raw["destination_type"]

    t_raw = adset_raw.get("targeting") or {}
    t = adset["targeting"]
    geo = t_raw.get("geo_locations") or {}
    t["geo_locations"] = {
        "countries": [str(c) for c in geo.get("countries") or []],
        "cities": [{"key": str(c.get("key")), "name": c.get("name") or "", "radius": int(c.get("radius") or 0), "distance_unit": c.get("distance_unit") or "kilometer"} for c in geo.get("cities") or [] if c.get("key")],
        "regions": [{"key": str(r.get("key")), "name": r.get("name") or ""} for r in geo.get("regions") or [] if r.get("key")],
    }
    for extra in ("zips", "places", "custom_locations", "geo_markets"):
        if geo.get(extra):
            warnings.append(f"Hudud turi '{extra}' Replix'da hali qo'llanmaydi.")
    if t_raw.get("excluded_geo_locations"):
        warnings.append("Chiqarib tashlangan hududlar (excluded_geo_locations) Replix'da hali qo'llanmaydi.")
    t["age_min"] = int(t_raw.get("age_min") or 18)
    t["age_max"] = int(t_raw.get("age_max") or 65)
    t["genders"] = [int(g) for g in t_raw.get("genders") or [] if int(g) in (1, 2)]
    t["locales"] = [int(l) for l in t_raw.get("locales") or []]
    interests: list[dict] = []
    behaviors: list[dict] = []
    for fs in t_raw.get("flexible_spec") or []:
        for i in fs.get("interests") or []:
            if i.get("id"):
                interests.append({"id": str(i["id"]), "name": i.get("name") or ""})
        for b in fs.get("behaviors") or []:
            if b.get("id"):
                behaviors.append({"id": str(b["id"]), "name": b.get("name") or ""})
        other = set(fs.keys()) - {"interests", "behaviors"}
        if other:
            warnings.append("Batafsil targeting turlari qo'llanmaydi: " + ", ".join(sorted(other)))
    if len(t_raw.get("flexible_spec") or []) > 1:
        warnings.append("Bir nechta flexible_spec (AND) guruhlari birlashtirildi.")
    t["interests"] = interests
    t["behaviors"] = behaviors
    t["custom_audiences"] = [{"id": str(a.get("id")), "name": a.get("name") or ""} for a in t_raw.get("custom_audiences") or [] if a.get("id")]
    t["excluded_custom_audiences"] = [{"id": str(a.get("id")), "name": a.get("name") or ""} for a in t_raw.get("excluded_custom_audiences") or [] if a.get("id")]
    automation = t_raw.get("targeting_automation") or {}
    t["advantage_audience"] = bool(automation.get("advantage_audience")) if "advantage_audience" in automation else False
    if t_raw.get("publisher_platforms"):
        t["placements"] = {
            "mode": "manual",
            "publisher_platforms": [p for p in t_raw["publisher_platforms"] if p in campaign_draft.PLACEMENT_OPTIONS["publisher_platforms"]],
            "facebook_positions": [p for p in t_raw.get("facebook_positions") or [] if p in campaign_draft.PLACEMENT_OPTIONS["facebook_positions"]],
            "instagram_positions": [p for p in t_raw.get("instagram_positions") or [] if p in campaign_draft.PLACEMENT_OPTIONS["instagram_positions"]],
        }
    unknown = sorted(set(t_raw.keys()) - _KNOWN_TARGETING_KEYS)
    if unknown:
        warnings.append("Bu kampaniyada Replix hali qo'llamaydigan sozlamalar bor: " + ", ".join(unknown))

    ad = state["ad"]
    ad["name"] = ad_raw.get("name") or ""
    ad["page_id"] = str(spec.get("page_id")) if spec.get("page_id") else (getattr(company, "meta_page_id", None) if company is not None else None)
    ad["instagram_actor_id"] = str(spec.get("instagram_actor_id")) if spec.get("instagram_actor_id") else None
    block = spec.get("video_data") or spec.get("link_data") or spec.get("photo_data") or {}
    ad["primary_text"] = block.get("message") or ""
    ad["headline"] = block.get("title") or block.get("name") or ""
    ad["description"] = block.get("link_description") or block.get("description") or ""
    ad["link_url"] = block.get("link") or None
    ad["display_link"] = block.get("caption") or None
    ad["media"] = {
        "media_id": None,
        "image_hash": block.get("image_hash") or creative.get("image_hash") or None,
        "video_id": str(block["video_id"]) if block.get("video_id") else None,
        "selected_variant": None,
    }
    if not (ad["media"]["image_hash"] or ad["media"]["video_id"]):
        warnings.append("Reklama medias (rasm/video) aniqlanmadi -- ehtimol mavjud post (object_story_id) ishlatilgan.")
    cta = block.get("call_to_action") or {}
    cta_type = str(cta.get("type") or "").upper()
    if cta_type == "MESSAGE_PAGE":
        cta_type = "SEND_MESSAGE"
    if cta_type in campaign_draft.CTA_TYPES:
        ad["cta"] = cta_type
    elif cta_type:
        warnings.append(f"CTA turi '{cta_type}' Replix ro'yxatida yo'q -- standart qo'yildi.")
    cta_value = cta.get("value") or {}
    if cta_value.get("lead_gen_form_id"):
        ad["lead_form"] = {"mode": "existing", "existing_form_id": str(cta_value["lead_gen_form_id"]), "new_form": ad["lead_form"]["new_form"]}
    pwm = block.get("page_welcome_message")
    if pwm:
        try:
            parsed = json.loads(pwm) if isinstance(pwm, str) else pwm
            msg = ((parsed.get("text_format") or {}).get("message") or {})
            ad["messages"] = {"greeting": msg.get("text") or "", "quick_replies": [b.get("title") for b in msg.get("ice_breakers") or [] if b.get("title")]}
        except (ValueError, AttributeError):
            warnings.append("Xabar shabloni (page_welcome_message) o'qilmadi.")
    if not ad["primary_text"]:
        ad["primary_text"] = meta_api.extract_ad_copy_text(spec)
    campaign_draft.apply_objective_defaults(state)
    if adset_raw.get("optimization_goal") in campaign_draft.OPTIMIZATION_GOALS:
        state["adset"]["optimization_goal"] = adset_raw["optimization_goal"]
    return state, warnings


def import_campaign(session, company, campaign_id: str, *, manager_id: "int | None" = None):
    """Meta'dagi mavjud kampaniyani `CampaignDraft` sifatida import qiladi
    (source="IMPORTED", status="published", barcha maydonlar META_IMPORTED,
    Meta ID'lar to'ldirilgan, sync_status="synced"). Import qilinmagan
    sozlamalar reja ogohlantirishlariga yoziladi."""
    token = company.get_meta_access_token()
    if not token:
        raise PublishError("import", "Meta ulanmagan -- import qilib bo'lmaydi.")
    try:
        tree = meta_api.get_campaign_tree(campaign_id, access_token=token)
    except Exception as e:  # noqa: BLE001
        raise PublishError("import", friendly_publish_error(e, "import"), _raw_error_text(e)) from e
    assets = get_meta_assets(company)
    currency = ((assets.get("ad_account") or {}).get("currency")) or "UZS"
    state, warnings = state_from_campaign_tree(tree, company=company, currency=currency)
    adsets = tree.get("adsets") or []
    adset_raw = adsets[0] if adsets else {}
    ads = adset_raw.get("ads") or []
    ad_raw = ads[0] if ads else {}
    creative = ad_raw.get("creative") or {}
    meta_status = str(tree.get("status") or "PAUSED").upper()

    draft = db.CampaignDraft(
        company_id=company.id, created_by_manager_id=manager_id, title=tree.get("name") or f"Import {campaign_id}",
        source="IMPORTED", status="active" if meta_status == "ACTIVE" else "published", objective=state["objective"],
        campaign_approved=True, adset_approved=True, ad_approved=True,
        meta_campaign_id=str(tree.get("id") or campaign_id), meta_adset_id=str(adset_raw["id"]) if adset_raw.get("id") else None,
        meta_creative_id=str(creative["id"]) if creative.get("id") else None, meta_ad_id=str(ad_raw["id"]) if ad_raw.get("id") else None,
        meta_lead_form_id=(state["ad"].get("lead_form") or {}).get("existing_form_id"),
        sync_status="synced", last_synced_at=dt.datetime.utcnow(),
    )
    draft.set_state(state)
    draft.set_field_sources({p: "META_IMPORTED" for p in campaign_draft.ALLOWED_PATHS if campaign_draft.PATH_TYPES[p] != "dict"})
    draft.set_ai_plan({"reasoning_summary": "Meta'dan import qilingan kampaniya.", "explanations": {}, "confidence": {}, "questions": [], "warnings": warnings})
    draft.set_meta_snapshot({
        "meta": _meta_snapshot(tree, adset_raw, ad_raw), "published_state_hash": _state_hash(state),
        "synced_at": draft.last_synced_at.isoformat(),
    })
    session.add(draft)
    session.flush()
    _log_event(session, draft, actor="user" if manager_id else "system", action="imported", scope="all", details={"campaign_id": campaign_id, "warnings": warnings}, manager_id=manager_id)
    session.commit()
    return draft


# ---------------------------------------------------------------------------
# MAHALLIY O'ZGARISHLARNI META'GA YUBORISH (nashr qilingan qoralama)
# ---------------------------------------------------------------------------
_ADSET_TARGETING_PREFIX = "adset.targeting."
_CREATIVE_PATHS = {"ad.primary_text", "ad.headline", "ad.description", "ad.cta", "ad.link_url", "ad.display_link",
                   "ad.media.image_hash", "ad.media.video_id", "ad.messages.greeting", "ad.messages.quick_replies",
                   "ad.page_id", "ad.instagram_actor_id"}


def push_updates_to_meta(session, draft, company, changed_paths: list[str], *, manager_id: "int | None" = None) -> dict:
    """Nashr qilingan/import qilingan qoralamaning mahalliy o'zgarishlarini
    Meta'ga yuboradi -- FAQAT yangilanadigan maydonlar: kampaniya nomi;
    adset nomi/byudjet/tugash sanasi/targeting; reklama nomi; kreativ
    matn/CTA/media o'zgarsa YANGI kreativ yaratilib reklamaga biriktiriladi
    (kreativlar immutable -- `create_ad_creative_with_new_copy` naqshi).
    Qo'llab-quvvatlanmagan yo'llar `skipped`ga tushadi."""
    if not (draft.meta_campaign_id and draft.meta_adset_id and draft.meta_ad_id):
        raise PublishError("push", "Bu qoralama hali Meta'ga chiqarilmagan -- avval nashr qiling.")
    token = company.get_meta_access_token()
    state = draft.get_state()
    changed = set(changed_paths or [])
    pushed: list[str] = []
    skipped: list[str] = []
    try:
        if "campaign.name" in changed:
            meta_api.update_campaign(draft.meta_campaign_id, {"name": state["campaign"]["name"]}, access_token=token)
            pushed.append("campaign.name")
        adset_fields: dict = {}
        adset = state["adset"]
        currency = adset.get("currency") or "UZS"
        if "adset.name" in changed:
            adset_fields["name"] = adset["name"]
        if "adset.daily_budget" in changed and adset.get("daily_budget"):
            adset_fields["daily_budget"] = campaign_draft.to_minor_units(adset["daily_budget"], currency)
        if "adset.lifetime_budget" in changed and adset.get("lifetime_budget"):
            adset_fields["lifetime_budget"] = campaign_draft.to_minor_units(adset["lifetime_budget"], currency)
        if ("adset.end_time" in changed or "adset.duration_days" in changed) and adset.get("end_time"):
            adset_fields["end_time"] = adset["end_time"]
        if "adset.bid_strategy" in changed:
            adset_fields["bid_strategy"] = adset["bid_strategy"]
        if any(p.startswith(_ADSET_TARGETING_PREFIX) for p in changed):
            adset_fields["targeting"] = campaign_draft.to_meta_targeting(state)
        if adset_fields:
            meta_api.update_adset(draft.meta_adset_id, adset_fields, access_token=token)
            pushed.extend(f"adset.{k}" for k in adset_fields)
        if "ad.name" in changed:
            meta_api.update_ad(draft.meta_ad_id, {"name": state["ad"]["name"]}, access_token=token)
            pushed.append("ad.name")
        if changed & _CREATIVE_PATHS:
            media = state["ad"].get("media") or {}
            spec = campaign_draft.to_meta_creative_spec(
                state, page_id=state["ad"].get("page_id") or company.meta_page_id,
                instagram_actor_id=state["ad"].get("instagram_actor_id"),
                image_hash=media.get("image_hash"), video_id=media.get("video_id"), lead_form_id=draft.meta_lead_form_id,
            )
            res = meta_api.create_ad_creative(company.meta_ad_account_id, f"{state['ad']['name']} — yangilangan", spec, access_token=token)
            new_creative_id = str(res.get("id"))
            meta_api.update_ad_creative(draft.meta_ad_id, new_creative_id, access_token=token)
            draft.meta_creative_id = new_creative_id
            pushed.append("ad.creative")
            _log_event(session, draft, actor="system", action="meta_creative_created", scope="ad", details={"creative_id": new_creative_id, "reason": "push_updates"}, manager_id=manager_id)
        for p in changed:
            if p not in pushed and not p.startswith(_ADSET_TARGETING_PREFIX) and p not in _CREATIVE_PATHS and p not in ("adset.duration_days",):
                skipped.append(p)
    except Exception as e:  # noqa: BLE001
        friendly = friendly_publish_error(e, "push")
        draft.sync_status = "sync_error"
        draft.publish_error = friendly
        draft.last_meta_error_raw = _raw_error_text(e)
        _log_event(session, draft, actor="system", action="meta_error", details={"step": "push", "pushed": pushed, "error": friendly}, manager_id=manager_id)
        session.commit()
        raise PublishError("push", friendly, draft.last_meta_error_raw) from e
    draft.sync_status = "synced"
    draft.last_synced_at = dt.datetime.utcnow()
    snap = draft.get_meta_snapshot()
    snap["published_state_hash"] = _state_hash(state)
    snap["synced_at"] = draft.last_synced_at.isoformat()
    snap["meta"] = _expected_snapshot(draft, state)
    draft.set_meta_snapshot(snap)
    _log_event(session, draft, actor="user" if manager_id else "system", action="synced", scope="all", details={"pushed": pushed, "skipped": sorted(skipped)}, manager_id=manager_id)
    session.commit()
    return {"pushed": pushed, "skipped": sorted(skipped)}
