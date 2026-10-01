"""ig_hot_leads.py -- PLAN 6-bosqich: Instagram/Facebook DM'da kalit so'z
bo'yicha "issiq lid"ni aniqlash (2026-10-01).

Mijoz DM'da "narxi qancha", "buyurtma", "цена" kabi yozsa:
  1. CRM'da avtomatik lid ochiladi (suhbatga bog'lanadi, bir marta);
  2. kompaniyaning Telegram guruhiga xabar ketadi;
  3. (ixtiyoriy) egasi yozgan avtojavob mijozga BIR MARTA yuboriladi.
Hammasi STANDART O'CHIQ -- kompaniya o'zi yoqadi (Instagram xabarlar sahifasi).
AI ishlatilmaydi (bepul, darhol). Sozlamalar kv'da (migratsiyasiz).
"""

from __future__ import annotations

import logging
import re

import db
import kv_store

logger = logging.getLogger("ig-hot-leads")

DEFAULT_KEYWORDS = [
    "narx", "qancha", "necha pul", "buyurtma", "zakaz", "sotib ol", "olmoqchi", "manzil", "yetkaz", "dostavka",
    "цена", "сколько", "стоимость", "заказ", "купить", "доставк", "адрес",
    "price", "order", "buy",
]
_SETTINGS_KEY = "ig_hot_leads"
MAX_KEYWORDS = 40
MAX_REPLY_LEN = 600


def _scoped(company_id) -> str:
    return f"{_SETTINGS_KEY}:{company_id}"


def get_settings(company_id) -> dict:
    try:
        raw = kv_store.get_json(_scoped(company_id), default=None) or {}
    except Exception:  # noqa: BLE001
        raw = {}
    kws = raw.get("keywords")
    return {
        "enabled": bool(raw.get("enabled", False)),
        "keywords": [k for k in (kws if isinstance(kws, list) else DEFAULT_KEYWORDS) if isinstance(k, str) and k.strip()][:MAX_KEYWORDS],
        "auto_reply": bool(raw.get("auto_reply", False)),
        "reply_text": str(raw.get("reply_text") or "")[:MAX_REPLY_LEN],
    }


def save_settings(company_id, *, enabled: bool, keywords_text: str, auto_reply: bool, reply_text: str) -> dict:
    kws = []
    for part in re.split(r"[,\n;]+", keywords_text or ""):
        p = part.strip().lower()
        if p and p not in kws:
            kws.append(p[:40])
    reply_text = (reply_text or "").strip()[:MAX_REPLY_LEN]
    data = {
        "enabled": bool(enabled),
        "keywords": kws[:MAX_KEYWORDS] or list(DEFAULT_KEYWORDS),
        "auto_reply": bool(auto_reply and reply_text),  # matnsiz avtojavob yoqilmaydi
        "reply_text": reply_text,
    }
    kv_store.set_json(_scoped(company_id), data)
    return data


def _norm(text: str) -> str:
    return (text or "").lower().replace("ʻ", "'").replace("’", "'").replace("‘", "'").replace("`", "'")


def match_keyword(text: str, keywords: list[str]) -> "str | None":
    t = _norm(text)
    if not t.strip():
        return None
    for kw in keywords:
        k = _norm(kw).strip()
        if k and k in t:
            return kw
    return None


_PHONE_RE = re.compile(r"(?:\+?998[\s\-()]*)?(?:\d[\s\-()]*){9}")


def extract_phone(text: str) -> "str | None":
    m = _PHONE_RE.search(text or "")
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(0))
    if len(digits) == 9:
        digits = "998" + digits
    return "+" + digits if len(digits) == 12 and digits.startswith("998") else None


def _send_telegram(chat_id, text: str) -> None:
    try:
        import scheduler  # aylanma importdan qochish uchun shu yerda
        scheduler._tg_send(int(chat_id), text)
    except Exception:  # noqa: BLE001
        logger.exception("Issiq lid: Telegram xabarini yuborib bo'lmadi")


def process_customer_messages(company_id: int, conversation_id: int, texts: list[str]) -> "dict | None":
    """Yangi kelgan MIJOZ xabarlari uchun chaqiriladi. Natija yoki None."""
    if not company_id or not conversation_id or not texts:
        return None
    settings = get_settings(company_id)
    if not settings["enabled"]:
        return None
    keyword = None
    for t in texts:
        keyword = match_keyword(t, settings["keywords"])
        if keyword:
            break
    if not keyword:
        return None

    session = db.get_session()
    try:
        with db.scoped_as(company_id):
            conv = session.get(db.IgDmConversation, conversation_id)
            if conv is None or conv.linked_lead_id:
                return None  # allaqachon lid -- qayta ochilmaydi, qayta xabar yo'q
            company = session.get(db.Company, company_id)
            phone = next((p for p in (extract_phone(t) for t in texts) if p), None)
            ad_name = None
            if conv.source_ad_id:
                src = session.query(db.IgDmAdSource).filter_by(ad_id=conv.source_ad_id).first()
                ad_name = src.ad_name if src else None
            lead = db.Lead(
                company_id=company_id,
                full_name=conv.customer_username or "Instagram mijozi",
                phone=phone,
                ad_id=conv.source_ad_id, ad_name=ad_name,
                source="dm", status="new",
                quality_note=f"Issiq lid: DM'da \"{keyword}\" deb yozdi (avtomatik).",
            )
            session.add(lead)
            session.flush()
            conv.linked_lead_id = lead.id
            session.commit()
            lead_id = lead.id
            group_id = company.telegram_group_id if company else None
            channel = conv.channel or "instagram"
            customer_id = conv.customer_ig_id
            page_id = company.meta_page_id if company else None
            token = company.get_meta_access_token() if company else None
            who = conv.customer_username or "mijoz"
    finally:
        session.close()

    if group_id:
        preview = next((t for t in texts if match_keyword(t, [keyword])), texts[-1])[:200]
        _send_telegram(group_id, (
            f"🔥 Issiq lid ({'Instagram' if channel == 'instagram' else 'Facebook'} DM): {who}\n"
            f"Yozdi: \"{preview}\"\n"
            + (f"Telefon: {phone}\n" if phone else "")
            + "CRM'da lid ochildi — tezroq javob bering."
        ))

    replied = False
    if settings["auto_reply"] and settings["reply_text"] and channel == "instagram" and customer_id and token:
        flag = f"ig_hot_auto_replied:{conversation_id}"
        try:
            already = kv_store.get_json(flag, default=None)
        except Exception:  # noqa: BLE001
            already = None
        if not already:
            try:
                import meta_api
                meta_api.send_instagram_message(customer_id, settings["reply_text"], page_id=page_id, access_token=token)
                kv_store.set_json(flag, True)
                replied = True
            except Exception as e:  # noqa: BLE001
                logger.warning("Issiq lid: avtojavob yuborilmadi (conv=%s): %s", conversation_id, type(e).__name__)
    return {"lead_id": lead_id, "keyword": keyword, "phone": phone, "auto_replied": replied}
