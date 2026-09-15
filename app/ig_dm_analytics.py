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
IG DM uchun ham berish uchun `build_period_analytics()` qo'shildi.

2026-09, foydalanuvchi so'rovi ("birato'lasini to'g'irlab yubor... smsga
target yoqilinadi, o'shani aniqlash yo'lini topishimiz kerak... sifatli
chiqsa, copyni ulash kerak"): endi har bir suhbat, agar Meta ad-referral
orqali boshlangan bo'lsa, QAYSI reklamadan kelgani (`source_ad_id`) va
o'sha reklamaning nomi/"copy" matni (`ig_dm_sync.resolve_ad_sources()`
tomonidan `IgDmAdSource`ga keshlangan) bilan birga qaytariladi -- menejer
sifatli chiqqan suhbatning reklamasini Meta Ads Manager'da qo'lda
nusxalashi uchun. Shuningdek, suhbat qo'lda CRM lidiga bog'langan bo'lsa
(`linked_lead_id` -- `app.py`dagi "Lid sifatida saqlash" tugmasi
yozadi), shu lidning holati (`Lead.status`, jumladan "sold"/"sotildi"mi)
ham qo'shiladi -- shu orqali "nechta DM sotib olishga aylandi" savoliga
javob beriladi."""

import datetime as dt
import json
from collections import Counter

from sqlalchemy import func

import tz_utils
from db import IgDmConversation, IgDmMessage, IgDmAdSource, Lead


def _ad_sources_map(session, ad_ids: "set[str]") -> dict:
    """Berilgan `ad_id`lar to'plami uchun keshlangan `IgDmAdSource`
    qatorlarini `{ad_id: {"ad_name":..., "ad_copy":..., "resolve_error":...}}`
    lug'atiga aylantiradi -- N+1 so'rovlarning oldini olish uchun BIR
    marta so'raladi (chaqiruvchi keyin har bir suhbatga shu lug'atdan
    biriktiradi)."""
    if not ad_ids:
        return {}
    rows = session.query(IgDmAdSource).filter(IgDmAdSource.ad_id.in_(ad_ids)).all()
    return {
        r.ad_id: {"ad_name": r.ad_name, "ad_copy": r.ad_copy, "resolve_error": r.resolve_error}
        for r in rows
    }


def _lead_status_map(session, lead_ids: "set[int]") -> dict:
    """Berilgan lid ID'lar uchun `{lead_id: status}` lug'ati -- DM
    suhbati qaysi lidga bog'langani (`linked_lead_id`) ma'lum bo'lsa,
    o'sha lidning JORIY holatini (masalan "sold") ko'rsatish uchun."""
    if not lead_ids:
        return {}
    rows = session.query(Lead.id, Lead.status).filter(Lead.id.in_(lead_ids)).all()
    return {lead_id: status for lead_id, status in rows}


def _conversation_to_dict(c: IgDmConversation, *, ad_source: "dict | None" = None, lead_status: "str | None" = None) -> dict:
    try:
        reasons = json.loads(c.ai_reasons) if c.ai_reasons else []
    except (TypeError, ValueError):
        reasons = []
    ad_source = ad_source or {}
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
        # 2026-09, target-aniqlash so'rovi:
        "source_ad_id": c.source_ad_id,
        "source_ad_name": ad_source.get("ad_name"),
        "source_ad_copy": ad_source.get("ad_copy"),
        "linked_lead_id": c.linked_lead_id,
        "linked_lead_status": lead_status,
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
    ad_ids = {c.source_ad_id for c in rows if c.source_ad_id}
    ad_map = _ad_sources_map(session, ad_ids)
    lead_ids = {c.linked_lead_id for c in rows if c.linked_lead_id}
    lead_status_map = _lead_status_map(session, lead_ids)
    conversations = [
        _conversation_to_dict(
            c,
            ad_source=ad_map.get(c.source_ad_id),
            lead_status=lead_status_map.get(c.linked_lead_id),
        )
        for c in rows
    ]

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
        # 2026-09, target-aniqlash so'rovi ("nechta lead tushsa... sotib
        # olsa"): DM'dan CRM lidiga qo'lda bog'langanlar soni va shulardan
        # "sold" (sotilgan) holatiga yetganlari -- reklama sifatini oxirigi
        # natija (sotuv) bilan bog'lab ko'rsatish uchun.
        "linked_leads_count": sum(1 for c in rows if c.linked_lead_id),
        "sold_count": sum(1 for c in rows if c.linked_lead_id and lead_status_map.get(c.linked_lead_id) == "sold"),
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
    linked_leads_count = 0
    sold_count = 0
    reason_counter: Counter = Counter()
    ad_ids = {c.source_ad_id for c in conversations if c.source_ad_id}
    ad_map = _ad_sources_map(session, ad_ids)
    lead_ids = {c.linked_lead_id for c in conversations if c.linked_lead_id}
    lead_status_map = _lead_status_map(session, lead_ids)
    # 2026-09, target-aniqlash so'rovi ("qaysi target(lar) yoqilgan,
    # qaysinisi sifatli lid berayapti"): davr ichida faol bo'lgan
    # suhbatlarni reklama (`source_ad_id`) bo'yicha guruhlab, har birining
    # hot/warm/cold taqsimotini hisoblaymiz -- menejer eng ko'p "hot" DM
    # bergan reklamaning copy'sini shu ro'yxatdan ko'rib, Meta Ads
    # Manager'da qo'lda nusxalay oladi.
    by_ad: dict[str, dict] = {}
    for c in conversations:
        if c.ai_lead_quality == "hot":
            hot += 1
        elif c.ai_lead_quality == "warm":
            warm += 1
        elif c.ai_lead_quality == "cold":
            cold += 1
        else:
            not_analyzed += 1
        if c.linked_lead_id:
            linked_leads_count += 1
            if lead_status_map.get(c.linked_lead_id) == "sold":
                sold_count += 1
        if c.ai_reasons:
            try:
                for r in json.loads(c.ai_reasons):
                    if isinstance(r, str) and r.strip():
                        reason_counter[r.strip()] += 1
            except (TypeError, ValueError):
                pass
        if c.source_ad_id:
            entry = by_ad.setdefault(c.source_ad_id, {
                "ad_id": c.source_ad_id,
                "ad_name": ad_map.get(c.source_ad_id, {}).get("ad_name"),
                "ad_copy": ad_map.get(c.source_ad_id, {}).get("ad_copy"),
                "conversation_count": 0, "hot_count": 0, "warm_count": 0, "cold_count": 0,
                "sold_count": 0,
            })
            entry["conversation_count"] += 1
            if c.ai_lead_quality == "hot":
                entry["hot_count"] += 1
            elif c.ai_lead_quality == "warm":
                entry["warm_count"] += 1
            elif c.ai_lead_quality == "cold":
                entry["cold_count"] += 1
            if c.linked_lead_id and lead_status_map.get(c.linked_lead_id) == "sold":
                entry["sold_count"] += 1

    by_ad_list = sorted(by_ad.values(), key=lambda e: (e["hot_count"], e["conversation_count"]), reverse=True)

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
        "linked_leads_count": linked_leads_count,
        "sold_count": sold_count,
        "by_ad": by_ad_list,
    }
