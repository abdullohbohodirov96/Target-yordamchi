"""
lead_analytics.py — "Lead Analytics" bo'limi uchun kengaytirilgan CRM+Meta
hisob-kitoblari.

`dashboard_data.get_kpis()` (Target sahifasi ishlatadigan, keshlangan
Meta+CRM birlashtirilgan hisob) allaqachon SPEND/LEADS/CPL/ACTIVE/QUAL/
WON/LOST/COST-WON'ni beradi -- shu funksiya o'shani qayta ishlatadi
(Meta'ga ikki marta murojaat qilinmaydi), ustiga uchta yangi ustun qo'shadi:

  ACT (bog'lanildi)   — lead voronkaning ENG BIRINCHI ("yangi", hali
                         umuman aloqa qilinmagan) bosqichida TURMAGAN
                         barcha leadlar soni. Boshqacha aytganda: jami
                         leadlardan nechtasi kamida bir marta "harakatga
                         tushgan" (kontaktga chiqilgan, sifatli/sifatsiz
                         deb belgilangan yoki sotilgan) -- qat'iy
                         "contacted_at" vaqti CRM'da saqlanmagani uchun bu
                         eng ishonchli proksi.
  CONV.RATE            — WON / jami CRM lead * 100 (necha foiz lead sotuvga
                         aylangan).
  DEAL TIME             — WON leadlar uchun "lead yaratilgan kun" bilan
                         "birinchi sotuv kuni" (`Sale`/`Lead.sold_at`)
                         orasidagi o'rtacha kunlar soni.

Bundan tashqari: kunlik "Won dynamics" qatori (grafika uchun, `Sale.sold_at`
bo'yicha -- CRM'dagi YAGONA aniq vaqt tamg'asi shu, `Lead.status`
o'zgargan payt hech qayerda alohida saqlanmaydi) va umumiy pliltka
raqamlari (WON/LOST/IN PIPELINE/CONV.RATE/COST-WON) tayyorlanadi.
"""

from collections import defaultdict

from sqlalchemy import func

import dashboard_data
from db import get_session, Lead, Sale, FunnelStage

LEVELS = dashboard_data.LEVELS


def _new_stage_keys(session) -> set[str]:
    """"Hali umuman bog'lanilmagan" deb hisoblanadigan bosqich kalit(lar)i --
    ACTIVE kategoriyasidagi bosqichlar orasida ENG BIRINCHISI (sort_order
    eng kichik). Standart voronkada bu "new" (undan keyin "contacted"
    keladi) -- lekin admin bosqichlarni qayta nomlagan/qayta tartiblagan
    bo'lishi ham mumkin, shuning uchun qattiq "new" satriga emas,
    sort_order'ga tayanamiz."""
    stages = session.query(FunnelStage).filter(FunnelStage.is_active == True).all()  # noqa: E712
    active_stages = sorted((s for s in stages if s.category == "active"), key=lambda s: s.sort_order)
    if not active_stages:
        return {"new"}
    return {active_stages[0].key}


def get_lead_analytics(
    level: str = "campaign", date_preset: str = "last_30d",
    date_from: str | None = None, date_to: str | None = None,
    active_only: bool = False,
    *, access_token: str | None = None, ad_account_id: str | None = None,
) -> dict:
    """`dashboard_data.get_kpis()` bilan bir xil qatorlarni qaytaradi, ustiga
    har bir qatorga `act`, `conv_rate`, `in_pipeline`, `deal_time_days`
    qo'shib. Qo'shimcha kalitlar: `won_dynamics` (kunlik WON soni ro'yxati)
    va `pipeline_totals` (WON/LOST/IN PIPELINE/CONV.RATE/COST-WON umumiy
    pliltkalar uchun)."""
    base = dashboard_data.get_kpis(
        level=level, date_preset=date_preset, date_from=date_from, date_to=date_to,
        active_only=active_only, access_token=access_token, ad_account_id=ad_account_id,
    )
    base.setdefault("won_dynamics", [])
    base.setdefault("pipeline_totals", {})
    if base.get("error") or base.get("not_connected"):
        return base

    cfg = LEVELS.get(level, LEVELS["campaign"])
    lead_attr = cfg["lead_attr"]

    if date_from and date_to:
        date_bounds = dashboard_data.custom_range_bounds_utc(date_from, date_to)
    else:
        date_bounds = dashboard_data._date_preset_bounds_utc(date_preset)

    session = get_session()
    try:
        new_keys = _new_stage_keys(session)
        lead_query = session.query(Lead)
        if date_bounds:
            start_utc, end_utc = date_bounds
            effective_created = func.coalesce(Lead.lead_created_time, Lead.created_at)
            lead_query = lead_query.filter(effective_created >= start_utc, effective_created < end_utc)
        leads = lead_query.all()

        act_by_id: dict = defaultdict(int)
        deal_days_by_id: dict = defaultdict(list)
        for lead in leads:
            oid = getattr(lead, lead_attr, None) or "unknown"
            if lead.status not in new_keys:
                act_by_id[oid] += 1
            if lead.sold_at:
                created = lead.lead_created_time or lead.created_at
                if created:
                    delta_days = (lead.sold_at - created).total_seconds() / 86400.0
                    if delta_days >= 0:
                        deal_days_by_id[oid].append(delta_days)

        # "Won dynamics" -- kunlik WON (sotuv) soni. Butun kompaniya bo'yicha
        # (barcha target/kampaniyalar yig'indisi), tanlangan davr uchun --
        # jadvaldagi har bir qator emas, sahifa pastidagi umumiy trend grafigi.
        won_dynamics = []
        sale_query = session.query(Sale).filter(Sale.is_returned == False)  # noqa: E712
        if date_bounds:
            start_utc, end_utc = date_bounds
            sale_query = sale_query.filter(Sale.sold_at >= start_utc, Sale.sold_at < end_utc)
        by_day: dict = defaultdict(int)
        for s in sale_query.all():
            if s.sold_at:
                by_day[s.sold_at.date()] += 1
        for day in sorted(by_day):
            won_dynamics.append({"date": day.isoformat(), "won": by_day[day]})
    finally:
        session.close()

    for row in base["rows"]:
        oid = row["id"]
        crm_total = row.get("crm_leads_total", 0) or 0
        won = row.get("sold", 0) or 0
        lost = row.get("unqualified", 0) or 0
        row["act"] = act_by_id.get(oid, 0)
        row["conv_rate"] = (won / crm_total * 100.0) if crm_total else 0.0
        row["in_pipeline"] = max(crm_total - won - lost, 0)
        deal_days = deal_days_by_id.get(oid) or []
        row["deal_time_days"] = (sum(deal_days) / len(deal_days)) if deal_days else None

    t = base["totals"]
    t_crm_total = t.get("crm_leads_total", 0) or 0
    t_won = t.get("sold", 0) or 0
    t_lost = t.get("unqualified", 0) or 0
    all_deal_days = [d for days in deal_days_by_id.values() for d in days]
    t["act"] = sum(act_by_id.values())
    t["conv_rate"] = (t_won / t_crm_total * 100.0) if t_crm_total else 0.0
    t["in_pipeline"] = max(t_crm_total - t_won - t_lost, 0)
    t["deal_time_days"] = (sum(all_deal_days) / len(all_deal_days)) if all_deal_days else None

    base["won_dynamics"] = won_dynamics
    base["pipeline_totals"] = {
        "won": t_won, "lost": t_lost, "in_pipeline": t["in_pipeline"],
        "conv_rate": t["conv_rate"], "cost_per_won": t.get("cost_per_won", 0.0),
    }
    return base
