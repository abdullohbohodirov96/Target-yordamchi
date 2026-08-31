"""
ig_dm_analytics.py — `/instagram-xabarlar` sahifasi uchun `IgDmConversation`
jadvalidan tayyor hisobot (ro'yxat + umumiy statistika) yig'adi. `ig_dm_sync.py`
(Meta'dan tortish) va `ig_dm_analysis.py` (AI baho) bilan ARALASHMAYDI --
faqat bazadagi ALLAQACHON saqlangan holatni o'qiydi (smm_analytics.py bilan
bir xil ajratish: sync/tahlil boshqa faylda, hisobot-qurish shu yerda)."""

import json
import datetime as dt

from db import IgDmConversation

_TASHKENT_OFFSET = dt.timedelta(hours=5)


def _conversation_to_dict(c: IgDmConversation) -> dict:
    try:
        reasons = json.loads(c.ai_reasons) if c.ai_reasons else []
    except (TypeError, ValueError):
        reasons = []
    return {
        "id": c.id,
        "customer": c.customer_username or c.customer_ig_id or "noma'lum",
        "last_message_text": c.last_message_text,
        "last_message_from": c.last_message_from,
        "last_message_at": c.last_message_at,
        "is_unanswered": c.is_unanswered,
        "unanswered_since": c.unanswered_since,
        "message_count": c.message_count,
        "ai_lead_quality": c.ai_lead_quality,
        "ai_summary": c.ai_summary,
        "ai_reasons": reasons,
        "ai_analyzed_at": c.ai_analyzed_at,
        "ai_error": c.ai_error,
        "needs_analysis": c.message_count > (c.ai_analyzed_message_count or 0),
    }


def build_dm_report(session, limit: int = 100) -> dict:
    """Qaytaradi: {"conversations": [...], "stats": {...}}. `conversations`
    eng oxirgi xabar kelgan suhbatdan boshlab tartiblangan."""
    rows = (
        session.query(IgDmConversation)
        .order_by(IgDmConversation.last_message_at.desc())
        .limit(limit)
        .all()
    )
    conversations = [_conversation_to_dict(c) for c in rows]

    today_start = (dt.datetime.utcnow() + _TASHKENT_OFFSET).replace(hour=0, minute=0, second=0, microsecond=0) - _TASHKENT_OFFSET
    new_today = sum(1 for c in rows if c.last_message_at and c.last_message_at >= today_start and c.last_message_from == "customer")

    stats = {
        "total_conversations": session.query(IgDmConversation).count(),
        "unanswered_count": sum(1 for c in rows if c.is_unanswered),
        "hot_count": sum(1 for c in rows if c.ai_lead_quality == "hot"),
        "warm_count": sum(1 for c in rows if c.ai_lead_quality == "warm"),
        "cold_count": sum(1 for c in rows if c.ai_lead_quality == "cold"),
        "not_analyzed_count": sum(1 for c in rows if not c.ai_lead_quality),
        "new_customer_messages_today": new_today,
    }
    return {"conversations": conversations, "stats": stats}
