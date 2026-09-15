"""
ig_dm_analytics.py — `/instagram-xabarlar` sahifasi uchun `IgDmConversation`
jadvalidan tayyor hisobot (ro'yxat + umumiy statistika) yig'adi. `ig_dm_sync.py`
(Meta'dan tortish) va `ig_dm_analysis.py` (AI baho) bilan ARALASHMAYDI --
faqat bazadagi ALLAQACHON saqlangan holatni o'qiydi (smm_analytics.py bilan
bir xil ajratish: sync/tahlil boshqa faylda, hisobot-qurish shu yerda).

2026-09, foydalanuvchi so'rovi ("Instagram DM tahlilini ko'rinadigan qilish"):
`build_dm_report()` YUQORIDAGI FUNKSIYA faqat "hozirgi holat" (real-time
inbox -- suhbatlar ro'yxati + joriy hot/warm/cold hisobi) ko'rsatadi, DAVR
bo'yicha tahlil (trend, o'rtacha javob vaqti, davr ichida qanday
taqsimlangani) UMUMAN yo'q edi -- Lead Analytics CRM uchun bergan narsani
IG DM uchun ham berish uchun `build_period_analytics()` qo'shildi."""

import datetime as dt
import json
from collections import Counter

from sqlalchemy import func

import tz_utils
from db import IgDmConversation, IgDmMessage


def _conversation_to_dict(c: IgDmConversation) -> dict:
    try:
        reasons = json.loads(c.ai_reasons) if c.ai_reasons else []
    except (TypeError, ValueError):
        reasons = []
    return {
        "id": c.id,
        # 2026-09, foydalanuvchi so'rovi ("facebookga otdelniy ikonkasi
        # bo'lsin... instagramni, instagram ikonkasi bo'lsin"): eski
        # qatorlarda `channel` bazada NULL bo'lishi mumkin (migratsiya
        # yangi ustunni NULL bilan qo'shadi) -- ular haqiqatan ham FAQAT
        # Instagram orqali yig'ilgan edi, shuning uchun `None` "instagram"
        # sifatida ko'rsatiladi.
        "channel": c.channel or "instagram",
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

    today_start = tz_utils.to_utc(tz_utils.now_local().replace(hour=0, minute=0, second=0, microsecond=0))
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


def build_period_analytics(session, since: "dt.datetime | None", until: "dt.datetime | None" = None) -> dict:
    """Berilgan davr (UTC, naive, [since, until) -- `since=None` bo'lsa
    pastki chegarasiz, "maksimal" ma'nosida) uchun IG DM'ning DAVR
    bo'yicha tahlili -- `build_dm_report()`dagi "hozirgi holat" (real-time
    inbox)dan FARQLI, Lead Analytics'ga o'xshab tanlangan davr ichida NIMA
    bo'lganini ko'rsatadi: nechta yangi suhbat boshlandi, nechta mijoz
    xabari keldi, o'rtacha javob vaqti qancha, kunlik trend, va davrda
    faol bo'lgan suhbatlarning hot/warm/cold taqsimoti + AI bahosining eng
    ko'p uchraydigan sabablari (mavjud `ai_reasons` ma'lumotidan -- YANGI
    LLM chaqiruvi QILINMAYDI, xarajat oshmaydi)."""
    msg_query = session.query(IgDmMessage)
    if since is not None:
        msg_query = msg_query.filter(IgDmMessage.sent_at >= since)
    if until is not None:
        msg_query = msg_query.filter(IgDmMessage.sent_at < until)
    messages = msg_query.order_by(IgDmMessage.conversation_id.asc(), IgDmMessage.sent_at.asc()).all()

    active_conv_ids = set()
    customer_count = 0
    business_count = 0
    daily_counts: dict[str, int] = {}
    response_gaps_minutes = []
    by_conv: dict[int, list] = {}

    for m in messages:
        active_conv_ids.add(m.conversation_id)
        by_conv.setdefault(m.conversation_id, []).append(m)
        if m.sender == "customer":
            customer_count += 1
            if m.sent_at:
                local_day = tz_utils.to_local(m.sent_at).date().isoformat()
                daily_counts[local_day] = daily_counts.get(local_day, 0) + 1
        elif m.sender == "business":
            business_count += 1

    for msgs in by_conv.values():
        msgs.sort(key=lambda m: m.sent_at or dt.datetime.min)
        for prev, cur in zip(msgs, msgs[1:]):
            if prev.sender == "customer" and cur.sender == "business" and prev.sent_at and cur.sent_at:
                gap = (cur.sent_at - prev.sent_at).total_seconds() / 60.0
                if gap >= 0:
                    response_gaps_minutes.append(gap)

    # "Yangi suhbat" -- shu suhbatning ENG BIRINCHI xabari (butun tarixi
    # bo'yicha, faqat shu davr ichidagi xabarlar emas) shu davr ichida
    # bo'lgan bo'lsa.
    new_conversations = 0
    if active_conv_ids:
        first_at_rows = (
            session.query(IgDmMessage.conversation_id, func.min(IgDmMessage.sent_at))
            .filter(IgDmMessage.conversation_id.in_(active_conv_ids))
            .group_by(IgDmMessage.conversation_id)
            .all()
        )
        for _conv_id, first_at in first_at_rows:
            if first_at is None:
                continue
            if since is not None and first_at < since:
                continue
            if until is not None and first_at >= until:
                continue
            new_conversations += 1

    conversations = (
        session.query(IgDmConversation).filter(IgDmConversation.id.in_(active_conv_ids)).all()
        if active_conv_ids else []
    )

    hot = warm = cold = not_analyzed = 0
    reason_counter: Counter = Counter()
    for c in conversations:
        if c.ai_lead_quality == "hot":
            hot += 1
        elif c.ai_lead_quality == "warm":
            warm += 1
        elif c.ai_lead_quality == "cold":
            cold += 1
        else:
            not_analyzed += 1
        if c.ai_reasons:
            try:
                for r in json.loads(c.ai_reasons):
                    if isinstance(r, str) and r.strip():
                        reason_counter[r.strip()] += 1
            except (TypeError, ValueError):
                pass

    daily_trend = [
        {"date": d, "day_label": dt.datetime.strptime(d, "%Y-%m-%d").strftime("%d.%m"), "count": c}
        for d, c in sorted(daily_counts.items())
    ]
    avg_response_minutes = (
        round(sum(response_gaps_minutes) / len(response_gaps_minutes), 1) if response_gaps_minutes else None
    )

    return {
        "active_conversations": len(active_conv_ids),
        "new_conversations": new_conversations,
        "customer_messages": customer_count,
        "business_messages": business_count,
        "avg_response_minutes": avg_response_minutes,
        "response_sample_size": len(response_gaps_minutes),
        "hot_count": hot, "warm_count": warm, "cold_count": cold, "not_analyzed_count": not_analyzed,
        "daily_trend": daily_trend,
        "top_reasons": [{"text": t, "count": c} for t, c in reason_counter.most_common(5)],
    }
