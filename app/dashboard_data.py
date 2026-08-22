"""
dashboard_data.py — bitta web sahifada ko'rsatiladigan barcha KPI'larni
tayyorlaydi: har bir kampaniya/adset/ad uchun xarajat/ko'rishlar (Meta'dan)
+ lead soni/sifat taqsimoti/sotuvlar (bizning CRM bazamizdan).

Meta o'zi "sifatli lead" yoki "sotuv" tushunchasini bilmaydi -- shuning
uchun bularni FAQAT bizning `leads` jadvalimizdan hisoblaymiz, Meta esa
faqat xarajat/ko'rish/klik kabi reklama darajasidagi raqamlarni beradi.

Ustunlar nomi (CAMPAIGN/SPEND/LEADS/CPL/ACTIVE/WON/LOST/QUAL/COST-WON)
foydalanuvchi ko'rsatgan referens dashboard bilan bir xil ma'noda:
  ACTIVE = hali hal bo'lmagan lidlar (status: new yoki contacted)
  QUAL   = sifatli deb belgilangan, lekin hali sotilmagan (status: qualified)
  WON    = sotilgan (status: sold)
  LOST   = sifatsiz deb belgilangan (status: unqualified)
Bu to'rttasi BIR-BIRINI YOPMAYDI (har bir lead faqat bitta guruhda).
"""

import datetime as dt
from collections import defaultdict

import meta_api
from db import get_session, Lead

LEVELS = {
    "campaign": {"id_field": "campaign_id", "name_field": "campaign_name", "lead_attr": "campaign_id"},
    "adset": {"id_field": "adset_id", "name_field": "adset_name", "lead_attr": "adset_id"},
    "ad": {"id_field": "ad_id", "name_field": "ad_name", "lead_attr": "ad_id"},
}


def _extract_lead_count_from_actions(actions: list[dict] | None) -> int:
    for a in (actions or []):
        if a.get("action_type") in ("lead", "leadgen_grouped", "onsite_conversion.lead_grouped"):
            try:
                return int(float(a.get("value", 0)))
            except (TypeError, ValueError):
                continue
    return 0


def get_kpis(level: str = "campaign", date_preset: str = "last_30d") -> dict:
    """Qaytaradi: {"rows": [...], "totals": {...}, "generated_at": ISO, "level": level}

    Har bir qatorda: id, name, status, spend, impressions, meta_leads,
    crm_leads_total, active, qualified, unqualified, sold, revenue, cpl,
    cost_per_won, avg_check, roi_percent."""
    cfg = LEVELS.get(level, LEVELS["campaign"])
    id_field, name_field = cfg["id_field"], cfg["name_field"]

    try:
        insight_rows = meta_api.get_insights(
            level=level,
            date_preset=date_preset,
            fields=[id_field, name_field, "spend", "impressions", "actions"],
        )
    except meta_api.MetaAPIError as e:
        return {"error": str(e), "rows": [], "totals": {}, "generated_at": dt.datetime.utcnow().isoformat(), "level": level}

    status_by_id = {}
    try:
        structure = meta_api.get_account_structure(active_only=False)
        key = {"campaign": "campaigns", "adset": "adsets", "ad": "ads"}[level]
        status_by_id = {o["id"]: o.get("status", "") for o in structure.get(key, [])}
    except meta_api.MetaAPIError:
        pass

    meta_by_id = {}
    for row in insight_rows:
        oid = row.get(id_field)
        if not oid:
            continue
        meta_by_id[oid] = {
            "id": oid,
            "name": row.get(name_field, ""),
            "status": status_by_id.get(oid, ""),
            "spend": float(row.get("spend", 0) or 0),
            "impressions": int(row.get("impressions", 0) or 0),
            "meta_leads": _extract_lead_count_from_actions(row.get("actions")),
        }

    # CRM tomonidagi lead statistikasi (hammasi, sana filtrisiz -- oddiylik
    # uchun MVP'da "shu paytgacha jamlangan" holatni ko'rsatamiz).
    session = get_session()
    try:
        leads = session.query(Lead).all()
    finally:
        session.close()

    lead_attr = cfg["lead_attr"]
    crm_by_id = defaultdict(lambda: {
        "crm_leads_total": 0, "active": 0, "qualified": 0,
        "unqualified": 0, "sold": 0, "revenue": 0.0,
    })
    for lead in leads:
        oid = getattr(lead, lead_attr, None) or "unknown"
        bucket = crm_by_id[oid]
        bucket["crm_leads_total"] += 1
        if lead.status in ("new", "contacted"):
            bucket["active"] += 1
        elif lead.status == "qualified":
            bucket["qualified"] += 1
        elif lead.status == "unqualified":
            bucket["unqualified"] += 1
        elif lead.status == "sold":
            bucket["sold"] += 1
            bucket["revenue"] += (lead.sale_amount or 0.0)

    all_ids = set(meta_by_id) | set(crm_by_id)
    rows = []
    totals = {
        "spend": 0.0, "impressions": 0, "meta_leads": 0, "crm_leads_total": 0,
        "active": 0, "qualified": 0, "unqualified": 0, "sold": 0, "revenue": 0.0,
    }
    for oid in all_ids:
        meta_part = meta_by_id.get(oid, {
            "id": oid, "name": "(noma'lum)", "status": "",
            "spend": 0.0, "impressions": 0, "meta_leads": 0,
        })
        crm_part = crm_by_id.get(oid, {
            "crm_leads_total": 0, "active": 0, "qualified": 0,
            "unqualified": 0, "sold": 0, "revenue": 0.0,
        })
        row = {**meta_part, **crm_part}
        row["cpl"] = (row["spend"] / row["crm_leads_total"]) if row["crm_leads_total"] else 0.0
        row["cost_per_won"] = (row["spend"] / row["sold"]) if row["sold"] else 0.0
        row["avg_check"] = (row["revenue"] / row["sold"]) if row["sold"] else 0.0
        row["roi_percent"] = ((row["revenue"] - row["spend"]) / row["spend"] * 100.0) if row["spend"] else 0.0
        rows.append(row)

        for k in ("spend", "impressions", "meta_leads", "crm_leads_total",
                   "active", "qualified", "unqualified", "sold", "revenue"):
            totals[k] += row[k]

    rows.sort(key=lambda r: r["spend"], reverse=True)
    totals["cpl"] = (totals["spend"] / totals["crm_leads_total"]) if totals["crm_leads_total"] else 0.0
    totals["cost_per_won"] = (totals["spend"] / totals["sold"]) if totals["sold"] else 0.0
    totals["avg_check"] = (totals["revenue"] / totals["sold"]) if totals["sold"] else 0.0
    totals["roi_percent"] = ((totals["revenue"] - totals["spend"]) / totals["spend"] * 100.0) if totals["spend"] else 0.0

    return {"rows": rows, "totals": totals, "generated_at": dt.datetime.utcnow().isoformat(), "level": level}


# Eski nom bilan moslik (app.py hali shu nomni chaqirishi mumkin bo'lsa) --
# yangi kodda to'g'ridan-to'g'ri get_kpis() ishlatiladi.
def get_campaign_kpis(date_preset: str = "last_30d") -> dict:
    data = get_kpis(level="campaign", date_preset=date_preset)
    data["campaigns"] = data.pop("rows")
    return data
