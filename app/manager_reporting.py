"""manager_reporting.py -- "bitta menejer nechta odam bilan kuniga lead
bilan gaplashyapti, kvalifikatsiya nechta odam bilan qilyapti" (2026-09,
foydalanuvchi so'rovi) uchun kunlik hisobot.

`db.LeadStatusEvent` -- har bir status/izoh saqlashning audit-jurnali
(qarang: `app.py: lead_detail()`) -- shu yerda BERILGAN KUN uchun
menejer bo'yicha guruhlanadi: nechta ALOHIDA lead bilan "ishlandi"
(kamida bitta voqea), va shulardan nechtasi "sifatli"/"sifatsiz"/
"sotildi" kategoriyasiga o'tkazildi (FunnelStage.category asosida --
bosqich nomlari kompaniya bo'yicha moslashuvchan bo'lgani uchun, kod
"qualified"/"unqualified"/"sold" QAT'IY kategoriyalarga qaraydi, aniq
bosqich kalitiga emas)."""
from __future__ import annotations

import datetime as dt

import db


def daily_manager_activity(session, company_id: "int | None", day: "dt.date | None" = None) -> list[dict]:
    day = day or dt.datetime.utcnow().date()
    start = dt.datetime.combine(day, dt.time.min)
    end = dt.datetime.combine(day, dt.time.max)

    q = session.query(db.LeadStatusEvent).filter(
        db.LeadStatusEvent.created_at >= start,
        db.LeadStatusEvent.created_at <= end,
    )
    if company_id is not None:
        q = q.filter(db.LeadStatusEvent.company_id == company_id)
    events = q.all()

    stages_q = session.query(db.FunnelStage)
    if company_id is not None:
        stages_q = stages_q.filter_by(company_id=company_id)
    category_by_key = {s.key: s.category for s in stages_q.all()}

    by_manager: dict[int, dict] = {}
    for e in events:
        mgr_key = e.manager_id or 0
        row = by_manager.setdefault(mgr_key, {
            "manager_id": mgr_key, "contacted_leads": set(),
            "qualified": 0, "unqualified": 0, "sold": 0, "events": 0,
        })
        row["contacted_leads"].add(e.lead_id)
        row["events"] += 1
        category = category_by_key.get(e.new_status)
        if category == "qualified":
            row["qualified"] += 1
        elif category == "unqualified":
            row["unqualified"] += 1
        elif category == "sold":
            row["sold"] += 1

    manager_ids = [k for k in by_manager if k]
    managers_lookup = {}
    if manager_ids:
        managers_lookup = {m.id: m for m in session.query(db.Manager).filter(db.Manager.id.in_(manager_ids)).all()}

    result = []
    for mgr_key, row in by_manager.items():
        mgr = managers_lookup.get(mgr_key)
        result.append({
            "manager_id": mgr_key,
            "manager_name": (mgr.full_name or mgr.username) if mgr else "Biriktirilmagan/noma'lum",
            "contacted_count": len(row["contacted_leads"]),
            "qualified": row["qualified"],
            "unqualified": row["unqualified"],
            "sold": row["sold"],
            "events": row["events"],
        })
    result.sort(key=lambda r: r["contacted_count"], reverse=True)
    return result
