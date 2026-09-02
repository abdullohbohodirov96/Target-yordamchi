"""meta_events.py — 2026-09, "production-ready Meta Ads + Conversions API"
so'rovi: CRM voqealari (yangi lead, sifatli lead, sotuv) bilan Meta'ga
CAPI signali yuborish MANTIG'INI AI-tahlil/lead-saqlash kodidan ALOHIDA
joyga chiqaradi (foydalanuvchi talabi: "dispatch logic separate from AI
analysis logic for maintainability").

Ilgari bu mantiq (`app.py`dagi `_send_capi_lead_signal`) faqat
QualifiedLead/Purchase hodisalarini yuborardi va HECH QANDAY jurnal
(log) yozmasdi. Bu modul:
  - Yangi "Lead" hodisasini ham qo'shadi (ilgari yo'q edi).
  - HAR BIR urinishni `MetaEventLog` jadvaliga yozadi (muvaffaqiyatli
    ham, muvaffaqiyatsiz ham) -- token HECH QACHON yozilmaydi.
  - Kompaniyaning QOʻLDA kiritilgan (Advanced/Manual) CAPI hisob
    ma'lumotlarini (`meta_capi_dataset_id`/`meta_capi_access_token`) OAuth
    orqali olingan (`meta_pixel_id`/`meta_access_token`) ma'lumotlaridan
    USTUN qo'yadi -- foydalanuvchi qo'lda kiritgan bo'lsa, aynan o'sha
    doimiy (muddatsiz) tokendan foydalanish afzal (60 kunlik OAuth
    tokeniga qaraganda ishonchliroq).
  - Meta xato kodi 190 (token muddati o'tgan/bekor qilingan) kelsa --
    kompaniyaning `meta_integration_status`sini "reauth_required"ga
    o'tkazadi (foydalanuvchi talabi: "Never silently fail for weeks").
  - HECH QACHON chaqiruvchiga (lead saqlash/sotuv qo'shish oqimiga)
    xato otib yubormaydi -- CAPI muvaffaqiyatsiz bo'lishi CRM'ning
    asosiy vazifasini (ma'lumotni saqlash) HECH QACHON to'xtatmasligi
    kerak.

MUHIM (xavfsizlik, foydalanuvchi talabi -- "Do NOT send: call
transcript, AI conversation analysis, private notes, medical
information, financial information..."): bu modul Meta'ga FAQAT
moslashtirish (matching) uchun zarur maydonlarni yuboradi (telefon,
email, tashqi ID, fbp/fbc, sotuv summasi) -- qo'ng'iroq matni, AI
tahlili, shaxsiy eslatmalar HECH QACHON bu yerdan o'tmaydi (ular hatto
funksiya argumentlariga ham qabul qilinmaydi)."""

import datetime as dt
import logging

import meta_api
from db import Company, MetaEventLog

logger = logging.getLogger("meta_events")


def _resolve_capi_credentials(company: "Company | None") -> tuple[str | None, str | None]:
    """Kompaniyaning CAPI uchun ishlatiladigan (access_token, dataset_id)
    juftini qaytaradi. QOʻLDA (Advanced/Manual) kiritilgan ma'lumotlar
    ustuvor -- ular OAuth token muddati tugashiga bog'liq emas."""
    if company is None:
        return None, None
    manual_token = company.get_meta_capi_token()
    if manual_token and company.meta_capi_dataset_id:
        return manual_token, company.meta_capi_dataset_id
    oauth_token = company.get_meta_access_token()
    if oauth_token and company.meta_pixel_id:
        return oauth_token, company.meta_pixel_id
    return None, None


def capi_credentials_configured(company: "Company | None") -> bool:
    token, dataset_id = _resolve_capi_credentials(company)
    return meta_api.is_capi_configured(pixel_id=dataset_id, access_token=token)


def _log_and_mark(session, company, *, event_name, event_id, lead=None, sale=None,
                   status, http_status=None, meta_response_id=None, error_code=None,
                   safe_error=None) -> None:
    try:
        session.add(MetaEventLog(
            company_id=company.id if company else None,
            event_name=event_name,
            event_id=event_id,
            lead_id=lead.id if lead is not None else None,
            sale_id=sale.id if sale is not None else None,
            status=status,
            http_status=http_status,
            meta_response_id=meta_response_id,
            error_code=error_code,
            safe_error_message=safe_error,
            created_at=dt.datetime.utcnow(),
            sent_at=dt.datetime.utcnow() if status == "sent" else None,
        ))
        if status == "sent" and company is not None:
            company.meta_last_event_at = dt.datetime.utcnow()
        session.commit()
    except Exception:
        logger.exception("MetaEventLog yozishda xatolik (event=%s)", event_name)
        try:
            session.rollback()
        except Exception:
            pass


def _dispatch(session, lead, event_name: str, *, sale=None, value: float | None = None,
              event_id_suffix: str = "") -> None:
    """Ichki umumiy yuborish funksiyasi -- HECH QACHON chaqiruvchiga xato
    otib yubormaydi."""
    company = None
    if lead.company_id:
        try:
            company = session.get(Company, lead.company_id)
        except Exception:
            company = None

    token, dataset_id = _resolve_capi_credentials(company)
    if not meta_api.is_capi_configured(pixel_id=dataset_id, access_token=token):
        return  # CAPI ulanmagan -- jim o'tkazib yuborish (ixtiyoriy funksiya)

    event_id = f"lead-{lead.id}-{event_name.lower()}{event_id_suffix}"
    try:
        result = meta_api.send_conversion_event(
            event_name,
            phone=lead.phone,
            email=lead.email,
            lead_id=lead.meta_lead_id,
            external_id=str(lead.id),
            event_id=event_id,
            value=value,
            pixel_id=dataset_id,
            access_token=token,
            fbp=getattr(lead, "fbp", None),
            fbc=getattr(lead, "fbc", None),
            event_source_url=getattr(lead, "landing_url", None),
            action_source="website" if getattr(lead, "fbc", None) or getattr(lead, "fbp", None) else "system_generated",
        )
    except Exception as e:
        is_expired = False
        try:
            is_expired = meta_api.is_token_expired_error(e)
        except Exception:
            pass
        if is_expired and company is not None:
            company.meta_integration_status = "reauth_required"
        logger.exception("CAPI hodisasini yuborishda xatolik (lead_id=%s, event=%s)", lead.id, event_name)
        _log_and_mark(
            session, company, event_name=event_name, event_id=event_id, lead=lead, sale=sale,
            status="failed",
            error_code=(e.args[0].get("code") if isinstance(getattr(e, "args", None), tuple) and e.args and isinstance(e.args[0], dict) else None),
            safe_error=meta_api.safe_error_message(e) if isinstance(e, meta_api.MetaAPIError) else "Meta bilan bog'lanishda kutilmagan xatolik.",
        )
        return

    meta_response_id = (result or {}).get("fbtrace_id") if result else None
    _log_and_mark(
        session, company, event_name=event_name, event_id=event_id, lead=lead, sale=sale,
        status="sent" if result is not None else "failed",
        http_status=200 if result is not None else None,
        meta_response_id=meta_response_id,
        safe_error=None if result is not None else "Meta CAPI hodisani qabul qilmadi.",
    )


def dispatch_lead_event(session, lead) -> None:
    """Yangi lead CRM'ga tushganda darhol chaqiriladi (`lead_sync.py`)."""
    _dispatch(session, lead, "Lead")


def dispatch_qualified_lead_event(session, lead) -> None:
    """Lead holati "sifatli" (qualified) kategoriyasiga o'tganda chaqiriladi."""
    _dispatch(session, lead, "QualifiedLead")


def dispatch_purchase_event(session, lead, sale=None, *, value: float | None = None, event_id_suffix: str = "") -> None:
    """Sotuv qo'shilganda / lead "sotildi" kategoriyasiga o'tganda chaqiriladi.

    MUHIM (foydalanuvchi talabi -- "Do NOT automatically treat every
    incoming lead as a sale."): bu funksiya FAQAT chaqiruvchi haqiqiy
    sotuv (`Sale` yozuvi yoki lead.sale_amount) borligini o'zi
    aniqlagandan KEYIN chaqirilishi kerak -- bu yerda avtomatik
    aniqlash/hech qanday taxmin qilinmaydi."""
    _dispatch(session, lead, "Purchase", sale=sale, value=value, event_id_suffix=event_id_suffix)
