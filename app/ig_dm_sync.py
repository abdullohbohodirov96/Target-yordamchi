"""
ig_dm_sync.py — Instagram Direct (DM) suhbatlarini Meta Graph API'dan
tortib, Postgres'ga saqlaydi (2026-08, foydalanuvchi so'rovi: "ig chatlarni
tahlilini ham qoshish kerak, lekin byudjetni yo'lini top, qimmat bo'p
ketmasin").

QO'SHIMCHA ENVIRONMENT VARIABLE KERAK EMAS -- `smm_sync.py` bilan bir xil:
allaqachon sozlangan `META_ACCESS_TOKEN` + `META_PAGE_ID` yetarli (Page
Access Token orqali). QO'SHIMCHA RUXSAT KERAK: `instagram_manage_messages`
-- bu SMM (`pages_read_engagement`) yoki reklama ruxsatlaridan ALOHIDA.
O'Z akkauntingiz uchun odatda Meta App Dashboard -> App Roles ->
"Instagram Testers" bo'limiga akkauntni tester sifatida qo'shish YETARLI
(to'liq ommaviy App Review SHART EMAS) -- batafsil `meta_api.py`dagi
`get_instagram_conversations()` izohiga qarang.

2026-09, multi-tenant (`smm_sync.py` bilan bir xil naqsh): `sync_once()`
endi ixtiyoriy `company` qabul qiladi, `sync_all_companies()` esa
`meta_page_id`+`meta_access_token` ulagan HAR BIR kompaniya bo'yicha
aylanadi. Javobsiz-suhbat Telegram ogohlantirishi (`scheduler.py`)
kompaniyaning O'Z `telegram_group_id`siga yuboriladi (sozlanmagan bo'lsa --
platforma egasining umumiy guruhiga EMAS, shunchaki yuborilmaydi, chunki
boshqa kompaniyaning mijozi bilan yozishmasi begona Telegram guruhiga
"sizib chiqishi" mumkin emas).

XARAJATNI NAZORAT QILISH -- BU MODUL ATAYLAB AI ISHLATMAYDI. Faqat:
  1. Meta'dan yangi suhbat/xabarlarni tortib bazaga yozadi (upsert).
  2. "Menejer javob berdimi yo'qmi" holatini ODDIY vaqt/tomon
     solishtirish orqali hisoblaydi (`is_unanswered`/`unanswered_since`).
  3. Uzoq vaqt javobsiz qolgan suhbatlarni ANIQLAYDI (Telegram xabarini
     O'ZI YUBORMAYDI -- buni `scheduler.job_ig_dm_sync` bajaradi, xuddi
     `orchestrator.enforce_cpl_hard_kill` + `scheduler.job_cpl_hard_kill`
     ajratilgani kabi).
Haqiqiy AI (gpt-4o-mini) tahlili FAQAT alohida `ig_dm_analysis.py`da,
DAVRIY (odatda har 2-3 soatda) ishga tushadi -- bu yerda EMAS.
"""

import json
import logging
import datetime as dt
from pathlib import Path

import meta_api
import kv_store
import db
from db import get_session, IgDmConversation, IgDmMessage

logger = logging.getLogger("ig_dm_sync")

BASE_DIR = Path(__file__).resolve().parent
_business_rules = json.loads((BASE_DIR / "business_rules.json").read_text(encoding="utf-8"))
UNANSWERED_ALERT_MINUTES = _business_rules.get("ig_dm_unanswered_alert_minutes", 30)


def is_configured(company=None) -> bool:
    """`company` berilsa -- O'SHA kompaniyaning O'Z ulanishini tekshiradi
    (2026-09, multi-tenant). Berilmasa -- eski global (ENV) tekshiruv."""
    if company is not None:
        return bool(getattr(company, "meta_access_token", None) and getattr(company, "meta_page_id", None))
    return bool(meta_api.ACCESS_TOKEN and meta_api.PAGE_ID)


def _parse_dt(value: "str | None") -> "dt.datetime | None":
    if not value:
        return None
    try:
        # Meta odatda "2026-08-20T10:00:00+0000" formatida qaytaradi.
        return dt.datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


# 2026-09, multi-tenant: HAR BIR kompaniyaning O'Z Page'i uchun alohida IG
# Business ID kerak -- keshni `page_id` bo'yicha saqlaymiz (avval "bitta
# global" deb faraz qilingan yagona kalit emas).
_ig_business_id_cache: dict = {}


def _get_ig_business_id(*, page_id: str | None = None, access_token: str | None = None) -> "str | None":
    """Bir marta olib keshlaydi -- har sinxronizatsiyada qayta so'ramaslik
    uchun (jarayon qayta ishga tushirilganda tabiiy ravishda yangilanadi)."""
    cache_key = page_id or "__default__"
    if cache_key not in _ig_business_id_cache:
        try:
            _ig_business_id_cache[cache_key] = meta_api.get_instagram_business_account_id(page_id=page_id, access_token=access_token)
        except meta_api.MetaAPIError:
            _ig_business_id_cache[cache_key] = None
    return _ig_business_id_cache[cache_key]


def _message_sender(raw_msg: dict, ig_business_id: "str | None") -> str:
    from_id = (raw_msg.get("from") or {}).get("id")
    if ig_business_id and from_id == ig_business_id:
        return "business"
    return "customer"


def _friendly_meta_error(e: meta_api.MetaAPIError) -> str:
    meta_err = e.args[0] if e.args else {}
    message = meta_err.get("message", str(e)) if isinstance(meta_err, dict) else str(e)
    code = meta_err.get("code") if isinstance(meta_err, dict) else None
    if code in (10, 200, 294) or "permission" in message.lower():
        return (
            f"Instagram DM'larni o'qish uchun ruxsat yetarli emas (Meta xatosi: \"{message}\"). "
            "BU KODDAGI XATO EMAS -- Meta App Dashboard'da tokenga "
            "'instagram_manage_messages' ruxsati yoqilganini (yoki akkauntingiz "
            "App Roles -> Instagram Testers ro'yxatida borligini) tekshiring."
        )
    return message


def _upsert_conversation_and_messages(
    session, conv: dict, ig_business_id: "str | None", *,
    company_id: int, page_id: "str | None" = None, access_token: "str | None" = None,
) -> dict:
    """Bitta suhbatni sinxronlaydi. Qaytaradi:
    {"new_messages": N, "became_overdue": bool, "row": IgDmConversation|None}."""
    external_id = conv.get("id")
    if not external_id:
        return {"new_messages": 0, "became_overdue": False, "row": None}

    # MUHIM (2026-09, multi-tenant): `company_id` bo'yicha HAM qidiramiz --
    # aks holda boshqa kompaniyaning (bir xil external_id bilan -- amalda
    # bo'lmaydi, lekin himoya sifatida) yozuvi noto'g'ri yangilanib qolishi
    # mumkin edi.
    row = session.query(IgDmConversation).filter_by(external_id=external_id).first()
    if row is None:
        row = IgDmConversation(external_id=external_id, company_id=company_id)
        session.add(row)
        session.flush()  # id kerak (IgDmMessage.conversation_id uchun)
    else:
        row.company_id = company_id

    participants = ((conv.get("participants") or {}).get("data")) or []
    customer_participant = next(
        (p for p in participants if p.get("id") != ig_business_id), None,
    )
    if customer_participant:
        row.customer_ig_id = customer_participant.get("id")
        row.customer_username = customer_participant.get("username") or row.customer_username

    try:
        raw_messages = meta_api.get_instagram_conversation_messages(external_id, limit=40, page_id=page_id, access_token=access_token)
    except meta_api.MetaAPIError as e:
        raise  # chaqiruvchi (sync_once) tutib, xatolar ro'yxatiga yozadi

    new_messages = 0
    for m in raw_messages:
        ext_msg_id = m.get("id")
        if ext_msg_id:
            exists = session.query(IgDmMessage).filter_by(external_id=ext_msg_id).first()
            if exists:
                continue
        msg_row = IgDmMessage(
            conversation_id=row.id,
            external_id=ext_msg_id,
            sender=_message_sender(m, ig_business_id),
            text=m.get("message"),
            sent_at=_parse_dt(m.get("created_time")),
            company_id=row.company_id,
        )
        session.add(msg_row)
        new_messages += 1

    session.flush()
    row.message_count = session.query(IgDmMessage).filter_by(conversation_id=row.id).count()

    all_msgs = (
        session.query(IgDmMessage)
        .filter_by(conversation_id=row.id)
        .order_by(IgDmMessage.sent_at.asc())
        .all()
    )

    was_unanswered = row.is_unanswered
    if all_msgs:
        last = all_msgs[-1]
        row.last_message_at = last.sent_at
        row.last_message_text = last.text
        row.last_message_from = last.sender

        if last.sender == "customer":
            # Javobsizlik davrining BOSHLANISH vaqtini topish uchun,
            # oxiridan boshlab orqaga qarab, ketma-ket "customer"
            # xabarlar davom etayotgan joygacha yuramiz.
            unanswered_since = last.sent_at
            for m in reversed(all_msgs[:-1]):
                if m.sender == "customer":
                    unanswered_since = m.sent_at or unanswered_since
                else:
                    break
            row.is_unanswered = True
            if row.unanswered_since != unanswered_since:
                # Yangi javobsizlik davri boshlandi (yoki avval umuman
                # javobsiz bo'lmagan) -- eski ogohlantirish belgisi
                # endi ahamiyatsiz, tozalanadi.
                row.unanswered_since = unanswered_since
                row.unanswered_alert_sent_at = None
        else:
            row.is_unanswered = False
            row.unanswered_since = None
            row.unanswered_alert_sent_at = None
    session.commit()

    became_overdue = row.is_unanswered and not was_unanswered
    return {"new_messages": new_messages, "became_overdue": became_overdue, "row": row}


def sync_once(company=None) -> dict:
    """Bitta sinxronizatsiya tsiklini bajaradi. `company` berilsa
    (`db.Company` qatori) -- O'SHA kompaniyaning O'Z `meta_page_id`/
    `meta_access_token`i bilan, natija shu kompaniyaning `company_id`si
    bilan saqlanadi (2026-09, multi-tenant, `smm_sync.py` bilan bir xil
    naqsh). Berilmasa -- eski global (ENV) xatti-harakat (default
    kompaniyaga yoziladi, CLI/skript uchun orqaga moslik).

    Qaytaradi:
    {"configured": bool, "conversations_checked": N, "new_messages": N,
    "overdue": [{"conversation_id", "customer", "preview", "since_minutes"}],
    "errors": [...]}.

    `overdue` -- `UNANSWERED_ALERT_MINUTES`dan ko'proq vaqt javobsiz qolgan
    VA hali ogohlantirish yuborilmagan suhbatlar (Telegram xabarini
    `scheduler.job_ig_dm_sync` yuboradi va shu suhbatning
    `unanswered_alert_sent_at`ini belgilaydi)."""
    result = {"configured": True, "conversations_checked": 0, "new_messages": 0, "overdue": [], "errors": []}
    company_id = company.id if company else db.get_default_company_id()
    if not is_configured(company):
        result["configured"] = False
        result["errors"].append("META_ACCESS_TOKEN yoki META_PAGE_ID sozlanmagan -- Instagram DM sinxronizatsiya o'tkazib yuborildi.")
        _save_status(result, company_id=company.id if company else None)
        return result

    page_id = company.meta_page_id if company else None
    access_token = company.meta_access_token if company else None

    ig_business_id = _get_ig_business_id(page_id=page_id, access_token=access_token)
    if not ig_business_id:
        result["configured"] = False
        result["errors"].append(
            "Instagram Business akkaunt Facebook Page'ga ulanmagan (yoki topilmadi) -- "
            "Instagram DM sinxronizatsiya o'tkazib yuborildi."
        )
        _save_status(result, company_id=company.id if company else None)
        return result

    session = get_session()
    try:
        try:
            conversations = meta_api.get_instagram_conversations(limit=50, page_id=page_id, access_token=access_token)
        except meta_api.MetaAPIError as e:
            result["errors"].append(_friendly_meta_error(e))
            _save_status(result, company_id=company.id if company else None)
            return result

        now = dt.datetime.utcnow()
        for conv in conversations:
            result["conversations_checked"] += 1
            try:
                outcome = _upsert_conversation_and_messages(
                    session, conv, ig_business_id,
                    company_id=company_id, page_id=page_id, access_token=access_token,
                )
            except meta_api.MetaAPIError as e:
                result["errors"].append(_friendly_meta_error(e))
                continue
            result["new_messages"] += outcome["new_messages"]
            row = outcome["row"]
            if row and row.is_unanswered and row.unanswered_alert_sent_at is None and row.unanswered_since:
                since_minutes = (now - row.unanswered_since).total_seconds() / 60.0
                if since_minutes >= UNANSWERED_ALERT_MINUTES:
                    result["overdue"].append({
                        "conversation_id": row.id,
                        "customer": row.customer_username or row.customer_ig_id or "noma'lum",
                        "preview": (row.last_message_text or "")[:150],
                        "since_minutes": round(since_minutes),
                    })
    finally:
        session.close()

    _save_status(result, company_id=company.id if company else None)
    return result


def sync_all_companies() -> dict:
    """2026-09, multi-tenant: `meta_page_id`+`meta_access_token` ulagan HAR
    BIR kompaniya bo'yicha aylanib, har birining Instagram DM'larini
    ALOHIDA (o'z hisobi bilan) sinxronlaydi (`smm_sync.sync_all_companies()`
    bilan bir xil naqsh). `scheduler.py`ning davriy IG DM job'i endi shuni
    chaqiradi -- eski yagona-akkaunt `sync_once()` o'rniga. Bitta
    kompaniyaning sinxronizatsiyasi muvaffaqiyatsiz bo'lishi qolganlarini
    to'xtatmaydi. Har bir kompaniyaning `overdue` ro'yxati o'zi bilan
    birga qaytadi -- `scheduler.job_ig_dm_sync` shu kompaniyaning O'Z
    Telegram guruhiga yuborish uchun `company_id`ni bilishi kerak."""
    from db import Company

    session = get_session()
    try:
        rows = (
            session.query(Company)
            .filter(Company.meta_page_id.isnot(None), Company.meta_access_token.isnot(None), Company.is_active.is_(True))
            .all()
        )
        companies = [{"id": c.id, "name": c.name, "meta_page_id": c.meta_page_id, "meta_access_token": c.meta_access_token} for c in rows]
    finally:
        session.close()

    per_company = {}
    for c in companies:
        fake_company = _CompanyCreds(id=c["id"], meta_page_id=c["meta_page_id"], meta_access_token=c["meta_access_token"])
        try:
            per_company[c["id"]] = sync_once(company=fake_company)
        except Exception as e:
            logger.exception("IG DM sync: '%s' (id=%s) kompaniyasi uchun xato", c["name"], c["id"])
            per_company[c["id"]] = {"errors": [f"Kutilmagan xato: {e}"]}
    return {"companies_synced": len(companies), "per_company": per_company}


class _CompanyCreds:
    """`sync_once(company=...)`ga uzatish uchun yengil obyekt (`smm_sync.py`
    bilan bir xil) -- to'liq `db.Company` ORM qatori shart emas, faqat shu
    uchta maydon kerak (session yopilgandan keyin ham ishlatish uchun
    detach qilingan)."""
    def __init__(self, id, meta_page_id, meta_access_token):
        self.id = id
        self.meta_page_id = meta_page_id
        self.meta_access_token = meta_access_token


def mark_alert_sent(conversation_id: int) -> None:
    """`scheduler.job_ig_dm_sync` Telegram xabarini muvaffaqiyatli
    yuborgandan keyin chaqiradi -- shu bilan bir xil suhbat uchun
    ogohlantirish qayta-qayta yuborilmaydi (javob kelib, keyin yana
    javobsiz qolmaguncha)."""
    session = get_session()
    try:
        row = session.get(IgDmConversation, conversation_id)
        if row is not None:
            row.unanswered_alert_sent_at = dt.datetime.utcnow()
            session.commit()
    finally:
        session.close()


def _status_key(company_id: "int | None") -> str:
    return "ig_dm_sync_status" if company_id is None else f"ig_dm_sync_status:{company_id}"


def _save_status(result: dict, *, company_id: "int | None" = None) -> None:
    try:
        kv_store.set_json(_status_key(company_id), {**result, "last_run_at": dt.datetime.utcnow().isoformat()})
    except Exception:
        logger.exception("ig_dm_sync_status'ni kv_store'ga yozishda xato (o'zi kritik emas)")


def get_last_status(company_id: "int | None" = None) -> "dict | None":
    return kv_store.get_json(_status_key(company_id), default=None)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(sync_all_companies(), ensure_ascii=False, indent=2))
