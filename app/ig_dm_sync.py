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
from db import get_session, IgDmConversation, IgDmMessage

logger = logging.getLogger("ig_dm_sync")

BASE_DIR = Path(__file__).resolve().parent
_business_rules = json.loads((BASE_DIR / "business_rules.json").read_text(encoding="utf-8"))
UNANSWERED_ALERT_MINUTES = _business_rules.get("ig_dm_unanswered_alert_minutes", 30)


def is_configured() -> bool:
    return bool(meta_api.ACCESS_TOKEN and meta_api.PAGE_ID)


def _parse_dt(value: "str | None") -> "dt.datetime | None":
    if not value:
        return None
    try:
        # Meta odatda "2026-08-20T10:00:00+0000" formatida qaytaradi.
        return dt.datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


_ig_business_id_cache: dict = {"id": None, "checked": False}


def _get_ig_business_id() -> "str | None":
    """Bir marta olib keshlaydi -- har sinxronizatsiyada qayta so'ramaslik
    uchun (jarayon qayta ishga tushirilganda tabiiy ravishda yangilanadi)."""
    if not _ig_business_id_cache["checked"]:
        try:
            _ig_business_id_cache["id"] = meta_api.get_instagram_business_account_id()
        except meta_api.MetaAPIError:
            _ig_business_id_cache["id"] = None
        _ig_business_id_cache["checked"] = True
    return _ig_business_id_cache["id"]


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


def _upsert_conversation_and_messages(session, conv: dict, ig_business_id: "str | None") -> dict:
    """Bitta suhbatni sinxronlaydi. Qaytaradi:
    {"new_messages": N, "became_overdue": bool, "row": IgDmConversation|None}."""
    external_id = conv.get("id")
    if not external_id:
        return {"new_messages": 0, "became_overdue": False, "row": None}

    row = session.query(IgDmConversation).filter_by(external_id=external_id).first()
    if row is None:
        row = IgDmConversation(external_id=external_id)
        session.add(row)
        session.flush()  # id kerak (IgDmMessage.conversation_id uchun)

    participants = ((conv.get("participants") or {}).get("data")) or []
    customer_participant = next(
        (p for p in participants if p.get("id") != ig_business_id), None,
    )
    if customer_participant:
        row.customer_ig_id = customer_participant.get("id")
        row.customer_username = customer_participant.get("username") or row.customer_username

    try:
        raw_messages = meta_api.get_instagram_conversation_messages(external_id, limit=40)
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


def sync_once() -> dict:
    """Bitta sinxronizatsiya tsiklini bajaradi. Qaytaradi:
    {"configured": bool, "conversations_checked": N, "new_messages": N,
    "overdue": [{"conversation_id", "customer", "preview", "since_minutes"}],
    "errors": [...]}.

    `overdue` -- `UNANSWERED_ALERT_MINUTES`dan ko'proq vaqt javobsiz qolgan
    VA hali ogohlantirish yuborilmagan suhbatlar (Telegram xabarini
    `scheduler.job_ig_dm_sync` yuboradi va shu suhbatning
    `unanswered_alert_sent_at`ini belgilaydi)."""
    result = {"configured": True, "conversations_checked": 0, "new_messages": 0, "overdue": [], "errors": []}
    if not is_configured():
        result["configured"] = False
        result["errors"].append("META_ACCESS_TOKEN yoki META_PAGE_ID sozlanmagan -- Instagram DM sinxronizatsiya o'tkazib yuborildi.")
        return result

    ig_business_id = _get_ig_business_id()
    if not ig_business_id:
        result["configured"] = False
        result["errors"].append(
            "Instagram Business akkaunt Facebook Page'ga ulanmagan (yoki topilmadi) -- "
            "Instagram DM sinxronizatsiya o'tkazib yuborildi."
        )
        return result

    session = get_session()
    try:
        try:
            conversations = meta_api.get_instagram_conversations(limit=50)
        except meta_api.MetaAPIError as e:
            result["errors"].append(_friendly_meta_error(e))
            return result

        now = dt.datetime.utcnow()
        for conv in conversations:
            result["conversations_checked"] += 1
            try:
                outcome = _upsert_conversation_and_messages(session, conv, ig_business_id)
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

    _save_status(result)
    return result


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


def _save_status(result: dict) -> None:
    try:
        kv_store.set_json("ig_dm_sync_status", {**result, "last_run_at": dt.datetime.utcnow().isoformat()})
    except Exception:
        logger.exception("ig_dm_sync_status'ni kv_store'ga yozishda xato (o'zi kritik emas)")


def get_last_status() -> "dict | None":
    return kv_store.get_json("ig_dm_sync_status", default=None)
