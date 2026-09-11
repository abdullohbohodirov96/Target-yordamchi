"""
integrations.py — "Marketplace": tashqi tizimlar (CRM va h.k.) bilan
UNIVERSAL (deyarli istalgan CRM bilan ishlaydigan) ulanish.

2026-09, foydalanuvchi ANIQ so'rovi: "hamma crmlarni ulash mumkin bolsin,
kop integratsiyalarni qilish mumkin bolsin, aniq ishlasin". Har bir CRM
uchun alohida (OAuth) integratsiya yozish oylab vaqt va real hisob/API
kalitlari talab qiladi -- bularsiz haqiqiy tekshiruvdan o'tkazib bo'lmaydi.
Shuning uchun UNIVERSAL WEBHOOK yondashuvi tanlandi -- bu deyarli har
qanday zamonaviy CRM/avtomatlashtirish tizimi (amoCRM, Bitrix24,
Zapier, Make, HubSpot va h.k.) bilan ishlaydi, chunki ularning
DEYARLI BARCHASIDA "webhook orqali qabul qilish" (kiruvchi) va
"chiquvchi avtomatlashtirish/webhook" (chiquvchi) funksiyasi bor:

  1. CHIQUVCHI (Replix -> tashqi CRM): kompaniya admin Marketplace
     sahifasida bitta "webhook URL" kiritadi (masalan tashqi CRM yoki
     Zapier/Make ssenariysining "webhook orqali qabul qilish" manzili).
     Replix'da YANGI lead paydo bo'lganda (manba qanday bo'lishidan
     qat'iy nazar -- Meta, qo'lda, Excel import, yoki kiruvchi webhook),
     shu URL'ga JSON POST qilinadi (`dispatch_pending_webhooks`,
     scheduler.py: `job_deliver_webhooks`, har 5 daqiqada).
  2. KIRUVCHI (tashqi CRM -> Replix): har bir kompaniya UNIKAL token bilan
     o'z shaxsiy URL'iga ega (`/api/webhook/leads/<token>`, app.py) --
     tashqi tizim (yoki Zapier/Make ssenariysi) shu manzilga JSON POST
     qilsa, avtomatik yangi Lead sifatida Replix CRM'iga tushadi
     (`parse_inbound_payload` turli kalit nomlarini moslashtiradi).

Bu ikkalasi birgalikda "istalgan CRM bilan ikki tomonlama
sinxronizatsiya"ni beradi -- har bir CRM uchun alohida qo'llab-quvvatlash
kodi yozmasdan.
"""

import datetime as dt
import logging
import secrets

import requests

logger = logging.getLogger("integrations")

_WEBHOOK_TIMEOUT_SECONDS = 8
_DISPATCH_BATCH_LIMIT = 50


def generate_inbound_token() -> str:
    """Har bir kompaniyaning shaxsiy kiruvchi webhook URL'i uchun --
    taxmin qilib bo'lmaydigan, URL-xavfsiz token."""
    return secrets.token_urlsafe(24)


def ensure_inbound_token(company) -> str:
    """Kompaniyaning `inbound_lead_token`i hali yo'q bo'lsa -- yaratadi
    (lekin COMMIT QILMAYDI, chaqiruvchi session'ni o'zi commit qiladi).
    Har doim joriy (yangi yoki mavjud) tokenni qaytaradi."""
    if not getattr(company, "inbound_lead_token", None):
        company.inbound_lead_token = generate_inbound_token()
    return company.inbound_lead_token


def lead_payload(lead) -> dict:
    """Bitta `Lead` qatoridan tashqi CRM'ga yuboriladigan JSON payload
    quradi -- keng tarqalgan/tushunarli maydon nomlari bilan (turli
    CRM/Zapier ssenariylari oson moslashtira olishi uchun bir nechta
    sinonim ham qo'shiladi: "full_name" VA "name", "phone" VA
    "phone_number")."""
    return {
        "id": lead.id,
        "full_name": lead.full_name,
        "name": lead.full_name,
        "phone": lead.phone,
        "phone_number": lead.phone,
        "phone2": lead.phone2,
        "email": lead.email,
        "source": lead.source,
        "status": lead.status,
        "campaign_name": lead.campaign_name,
        "adset_name": lead.adset_name,
        "ad_name": lead.ad_name,
        "quality_note": lead.quality_note,
        "created_at": lead.created_at.isoformat() if lead.created_at else None,
    }


def _post_webhook(url: str, secret: "str | None", payload: dict) -> dict:
    """Past darajadagi, umumiy POST -- xato bo'lsa HECH QACHON exception
    ko'tarmaydi, doim `{"ok": bool, "status_code": int|None, "error": str|None}`
    qaytaradi. `secret` berilgan bo'lsa, `X-Replix-Secret` sarlavhasi
    (ko'p CRM/Zapier "webhook orqali qabul qilish" bosqichlarida oddiy
    umumiy-parol tekshiruvi sifatida qo'llab-quvvatlaydi) va
    `Authorization: Bearer <secret>` (ba'zilari buni kutadi) ikkalasi ham
    yuboriladi -- qabul qiluvchi tomon qaysi birini tekshirishidan
    qat'iy nazar ishlashi uchun."""
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Replix-Secret"] = secret
        headers["Authorization"] = f"Bearer {secret}"
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=_WEBHOOK_TIMEOUT_SECONDS)
        ok = 200 <= resp.status_code < 300
        return {
            "ok": ok,
            "status_code": resp.status_code,
            "error": None if ok else f"HTTP {resp.status_code}: {resp.text[:200]}",
        }
    except requests.RequestException as e:
        return {"ok": False, "status_code": None, "error": str(e)[:300]}


def send_test_webhook(url: str, secret: "str | None" = None) -> dict:
    """Marketplace sahifasidagi "Test yuborish" tugmasi uchun -- sinov
    payload'i bilan haqiqiy so'rov yuboradi (`_post_webhook` bilan bir xil
    natija shakli)."""
    test_payload = {
        "test": True,
        "message": "Bu Replix'dan (Target-yordamchi) sinov xabari -- webhook to'g'ri sozlangan.",
        "sent_at": dt.datetime.utcnow().isoformat(),
    }
    return _post_webhook(url, secret, test_payload)


def dispatch_pending_webhooks(session, limit: int = _DISPATCH_BATCH_LIMIT) -> dict:
    """Chiquvchi webhook sozlagan HAR BIR kompaniya uchun, hali
    yuborilmagan (`webhook_delivered_at IS NULL`) lead'larni topib,
    birma-bir yuboradi. `scheduler.py: job_deliver_webhooks()` orqali
    davriy chaqiriladi. Bitta kompaniyaning/lead'ning xatosi qolganlarini
    to'xtatmaydi."""
    from db import Company, Lead

    companies = (
        session.query(Company)
        .filter(Company.webhook_out_url.isnot(None), Company.is_active.is_(True))
        .all()
    )
    result = {"companies": 0, "sent": 0, "failed": 0}
    for company in companies:
        url = company.webhook_out_url
        if not url:
            continue
        result["companies"] += 1
        pending = (
            session.query(Lead)
            .filter(
                Lead.company_id == company.id,
                Lead.webhook_delivered_at.is_(None),
            )
            .order_by(Lead.created_at.asc())
            .limit(limit)
            .all()
        )
        for lead in pending:
            outcome = _post_webhook(url, company.webhook_out_secret, lead_payload(lead))
            now = dt.datetime.utcnow()
            if outcome["ok"]:
                lead.webhook_delivered_at = now
                lead.webhook_delivery_error = None
                result["sent"] += 1
            else:
                lead.webhook_delivery_error = outcome["error"]
                result["failed"] += 1
            company.webhook_out_last_status = "ok" if outcome["ok"] else "error"
            company.webhook_out_last_at = now
            company.webhook_out_last_error = outcome["error"]
        if pending:
            try:
                session.commit()
            except Exception:
                logger.exception("dispatch_pending_webhooks: commit xatosi (company_id=%s)", company.id)
                session.rollback()
    return result


# Turli CRM/forma/Zapier ssenariylari lead maydonlarini har xil nom bilan
# yuborishi mumkin -- shu ro'yxat orqali eng ko'p uchraydigan sinonimlar
# (o'zbek/rus/ingliz) moslashtiriladi. Birinchi topilgan (bo'sh bo'lmagan)
# qiymat ishlatiladi.
_NAME_KEYS = ("full_name", "name", "fio", "ism", "customer_name", "client_name")
_PHONE_KEYS = ("phone", "phone_number", "telefon", "tel", "mobile", "msisdn")
_PHONE2_KEYS = ("phone2", "phone_2", "second_phone", "qoshimcha_telefon", "additional_phone")
_EMAIL_KEYS = ("email", "e-mail", "mail")
_NOTE_KEYS = ("note", "comment", "message", "izoh", "quality_note")


def _first_present(data: dict, keys) -> "str | None":
    for k in keys:
        v = data.get(k)
        if v is None:
            continue
        v = str(v).strip()
        if v:
            return v
    return None


def parse_inbound_payload(data: dict) -> dict:
    """Tashqi tizimdan (`/api/webhook/leads/<token>`) kelgan xom JSON'ni
    Replix `Lead`ga mos maydonlarga o'giradi -- turli CRM/Zapier
    ssenariylari har xil kalit nomi yuborishi mumkinligi uchun bir nechta
    sinonim tekshiriladi (`_NAME_KEYS`/`_PHONE_KEYS`/...). Notanish
    maydonlar hech qayerga yo'qolib qolmasligi uchun `raw` ichida
    saqlanadi (Lead.raw_field_data'ga yoziladi)."""
    if not isinstance(data, dict):
        data = {}
    normalized = {str(k).strip().lower(): v for k, v in data.items()}
    return {
        "full_name": _first_present(normalized, _NAME_KEYS),
        "phone": _first_present(normalized, _PHONE_KEYS),
        "phone2": _first_present(normalized, _PHONE2_KEYS),
        "email": _first_present(normalized, _EMAIL_KEYS),
        "note": _first_present(normalized, _NOTE_KEYS),
        "raw": data,
    }
