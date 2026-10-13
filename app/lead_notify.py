"""lead_notify.py -- 2026-10, foydalanuvchi so'rovlari:

1. "Lid qo'shsam menejerlarga ko'rinmayapti": yangi lid (Meta formadan --
   real-vaqt yoki polling, yoki qo'lda qo'shilgan) tushganda menejer(lar)ga
   DARHOL Telegram xabari boradi. Lid menejerga biriktirilgan bo'lsa -- faqat
   o'shanga; biriktirilmagan bo'lsa -- kompaniyaning Telegram ulagan barcha
   faol menejerlariga (ular bo'lmasa -- kompaniyaning vazifalar guruhiga).

2. "Lid sifatini har kuni hisobot qilib bersin -- bugun buncha lid tushdi,
   buncha sifatli, buncha sifatsiz, aniq son bilan": `build_lead_quality_report()`
   -- bitta kun (Toshkent) ichida TUSHGAN lidlar bo'yicha aniq sonlar.
   `scheduler.job_lead_quality_report()` har kuni kechqurun yuboradi.
   Ostida -- shu kuni SOTILGANLAR: lid qachon tushgani -> qachon sotilgani,
   summa, menejer, manba.

Har ikkisi ham kompaniya bo'yicha qat'iy ajratilgan: xabar faqat O'SHA
kompaniyaning menejerlari/guruhiga boradi.
"""

import datetime as dt
import logging
import os
from collections import Counter

import db
import tz_utils

logger = logging.getLogger("lead_notify")

# Bir martada shundan ko'p lid tushsa (masalan uzoq uzilishdan keyingi
# sinxronizatsiya) -- har biriga alohida emas, bitta umumiy xabar.
_MAX_INDIVIDUAL = 8


def _send(chat_id, text: str) -> bool:
    import requests
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token or not chat_id:
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat_id, "text": text[:4000]}, timeout=15)
        ok = bool(r.json().get("ok"))
        if not ok:
            logger.warning("Telegram rad etdi (chat_id=%s): %s", chat_id, r.text[:200])
        return ok
    except Exception:  # noqa: BLE001
        logger.exception("Telegram xabarini yuborishda xato (chat_id=%s)", chat_id)
        return False


def _base_url() -> str:
    return (os.environ.get("APP_BASE_URL") or "https://replix.uz").rstrip("/")


def _lead_line(lead) -> str:
    parts = [f"👤 {lead.full_name or 'Ismsiz'}"]
    if lead.phone:
        parts.append(f"📞 {lead.phone}")
    src = lead.form_name or lead.campaign_name
    if src:
        parts.append(f"📋 {src}")
    return "\n".join(parts)


def notify_new_leads(company_id, lead_ids) -> int:
    """Yangi lidlar haqida menejerlarga xabar. Qaytaradi: yuborilgan xabarlar soni."""
    if not company_id or not lead_ids:
        return 0
    session = db.get_session()
    try:
        with db.unscoped():
            company = session.get(db.Company, company_id)
            leads = session.query(db.Lead).filter(db.Lead.company_id == company_id, db.Lead.id.in_(list(lead_ids))).all()
            managers = (
                session.query(db.Manager)
                .filter(db.Manager.company_id == company_id, db.Manager.is_active.is_(True),
                        db.Manager.telegram_user_id.isnot(None))
                .all()
            )
            mgr_by_id = {m.id: m for m in managers}
            # Faqat "Lidlar" bo'limi ochiq menejerlar (admin -- har doim).
            import permissions
            lead_managers = [m for m in managers
                             if m.role == "admin" or "leads" in permissions.parse_allowed_modules(m.allowed_modules)]
            group_id = company.resolved_tasks_group_id() if company is not None else None
            lead_rows = [(l.id, l.assigned_manager_id, _lead_line(l)) for l in leads]
    finally:
        session.close()

    if not lead_rows:
        return 0
    sent = 0
    base = _base_url()
    if len(lead_rows) > _MAX_INDIVIDUAL:
        text = f"🔔 {len(lead_rows)} ta yangi lid tushdi.\nCRM: {base}/leads?status_group=new"
        targets = [m.telegram_user_id for m in lead_managers] or ([group_id] if group_id else [])
        for t in targets:
            sent += _send(t, text)
        return sent

    for lead_id, assigned_id, line in lead_rows:
        text = f"🔔 Yangi lid\n{line}\n\n{base}/leads/{lead_id}"
        assigned = mgr_by_id.get(assigned_id)
        if assigned is not None:
            targets = [assigned.telegram_user_id]
        else:
            targets = [m.telegram_user_id for m in lead_managers] or ([group_id] if group_id else [])
        for t in targets:
            sent += _send(t, text)
    return sent


# ---------------------------------------------------------------------------
# Kunlik lid sifati hisoboti
# ---------------------------------------------------------------------------

def _day_bounds_utc(day: dt.date):
    start_utc = tz_utils.to_utc(dt.datetime.combine(day, dt.time.min))
    return start_utc, start_utc + dt.timedelta(days=1)


def lead_quality_counts(company_id: int, start_utc, end_utc) -> dict:
    """[start, end) oralig'ida TUSHGAN lidlar (Meta'dagi yaratilish vaqti, u
    bo'lmasa CRM'ga yozilgan vaqt) va ularning HOZIRGI holati bo'yicha sonlar.
    Kategoriya kompaniyaning o'z voronka bosqichlaridan olinadi."""
    from sqlalchemy import func
    session = db.get_session()
    try:
        with db.unscoped():
            effective = func.coalesce(db.Lead.lead_created_time, db.Lead.created_at)
            leads = (
                session.query(db.Lead)
                .filter(db.Lead.company_id == company_id, effective >= start_utc, effective < end_utc)
                .all()
            )
            stages = (
                session.query(db.FunnelStage)
                .filter(db.FunnelStage.company_id == company_id, db.FunnelStage.is_active.is_(True))
                .order_by(db.FunnelStage.sort_order.asc())
                .all()
            )
            new_key = next((s.key for s in stages if s.category == "active"), "new")
            category_by_key = {s.key: s.category for s in stages}
            rows = [(l.status, l.source, l.form_name or l.campaign_name or "", l.assigned_manager.full_name if l.assigned_manager else None)
                    for l in leads]
    finally:
        session.close()

    out = {"total": len(rows), "new": 0, "in_progress": 0, "qualified": 0, "unqualified": 0, "sold": 0,
           "by_source": Counter(), "by_form": {}, "by_manager": {}}
    for status, source, form, manager in rows:
        cat = category_by_key.get(status, "active")
        if cat == "active":
            key = "new" if status == new_key else "in_progress"
        else:
            key = cat if cat in ("qualified", "unqualified", "sold") else "in_progress"
        out[key] += 1
        out["by_source"][source or "meta"] += 1
        f = out["by_form"].setdefault(form or "—", Counter())
        f["total"] += 1
        f[key] += 1
        if manager:
            m = out["by_manager"].setdefault(manager, Counter())
            m["total"] += 1
            m[key] += 1
    return out


_SOURCE_LABELS = {"meta": "Meta forma", "manual": "Qo'lda", "import": "Import", "dm": "Instagram DM"}


def _pct(part: int, total: int) -> str:
    return f"{round(part * 100 / total)}%" if total else "0%"


def sales_for_day(company_id: int, start_utc, end_utc) -> list:
    """2026-10, foydalanuvchi so'rovi: "sotilganlar bo'lsa -- mana bu chisloda
    tushgan, mana bu sotildi deb ma'lumot bersin". [start, end) oralig'idagi
    (qaytarilmagan) har bir sotuv: lid ismi, lid QACHON tushgani, qachon
    sotilgani, summa, menejer, manba (forma/target)."""
    session = db.get_session()
    try:
        with db.unscoped():
            sales = (
                session.query(db.Sale)
                .join(db.Lead, db.Sale.lead_id == db.Lead.id)
                .filter(db.Lead.company_id == company_id, db.Sale.is_returned.is_(False),
                        db.Sale.sold_at >= start_utc, db.Sale.sold_at < end_utc)
                .order_by(db.Sale.sold_at.asc())
                .all()
            )
            out = []
            for sale in sales:
                lead = sale.lead
                came = (lead.lead_created_time or lead.created_at) if lead else None
                out.append({
                    "name": (lead.full_name if lead else None) or "Ismsiz",
                    "phone": lead.phone if lead else None,
                    "came_local": tz_utils.to_local(came) if came else None,
                    "sold_local": tz_utils.to_local(sale.sold_at) if sale.sold_at else None,
                    "amount": sale.amount or 0,
                    "sale_number": sale.sale_number or 1,
                    "manager": (sale.manager.full_name or sale.manager.username) if sale.manager else None,
                    "source": (lead.form_name or lead.campaign_name) if lead else None,
                })
            return out
    finally:
        session.close()


def _money(v) -> str:
    try:
        return f"{int(round(float(v))):,}".replace(",", " ") + " so'm"
    except (TypeError, ValueError):
        return "—"


def _sales_lines(sales: list) -> list:
    if not sales:
        return ["💰 Bugun sotuv: 0 ta"]
    total = sum(x["amount"] for x in sales)
    lines = [f"💰 Bugun sotildi: {len(sales)} ta — jami {_money(total)}"]
    for x in sales:
        came = x["came_local"].strftime("%d.%m.%Y") if x["came_local"] else "?"
        sold = x["sold_local"].strftime("%d.%m %H:%M") if x["sold_local"] else "?"
        days = ""
        if x["came_local"] and x["sold_local"]:
            d = (x["sold_local"].date() - x["came_local"].date()).days
            days = " (o'sha kuni)" if d == 0 else f" ({d} kunda)"
        repeat = f", {x['sale_number']}-xarid" if x["sale_number"] > 1 else ""
        line = f"• {x['name']}: {came}da tushgan → {sold}da sotildi{days} — {_money(x['amount'])}{repeat}"
        extra = ", ".join(v for v in (x["manager"] and f"menejer: {x['manager']}", x["source"] and f"manba: {x['source']}") if v)
        lines.append(line + (f"\n   {extra}" if extra else ""))
    return lines


def build_lead_quality_report(company_id: int, day: "dt.date | None" = None, *, title: "str | None" = None) -> "str | None":
    """Bitta kun bo'yicha aniq sonli matn. Lid umuman bo'lmasa ham qisqa
    xabar qaytaradi ("bugun lid tushmadi") -- egasi jimlikdan xavotirlanmasin."""
    day = day or tz_utils.now_local().date()
    start, end = _day_bounds_utc(day)
    c = lead_quality_counts(company_id, start, end)
    head = title or f"📊 Lid sifati hisoboti — {day.strftime('%d.%m.%Y')}"
    sales = sales_for_day(company_id, start, end)
    if c["total"] == 0:
        return "\n".join([head, "", "Bugun lid tushmadi (0 ta).", "", *_sales_lines(sales)])
    good = c["qualified"] + c["sold"]
    decided = good + c["unqualified"]
    lines = [
        head, "",
        f"📥 Jami tushgan lid: {c['total']} ta",
        f"✅ Sifatli: {good} ta" + (f" (shundan sotildi: {c['sold']})" if c["sold"] else ""),
        f"❌ Sifatsiz: {c['unqualified']} ta",
        f"📞 Bog'lanilgan, hali baholanmagan: {c['in_progress']} ta",
        f"🆕 Hali hech kim gaplashmagan: {c['new']} ta",
    ]
    if decided:
        lines.append(f"🎯 Sifat darajasi (baholanganlar orasida): {_pct(good, decided)}")
    if len(c["by_source"]) > 1:
        lines.append("")
        lines.append("Manba bo'yicha: " + ", ".join(
            f"{_SOURCE_LABELS.get(k, k)} — {v}" for k, v in c["by_source"].most_common()))
    forms = sorted(c["by_form"].items(), key=lambda kv: -kv[1]["total"])[:8]
    if forms and not (len(forms) == 1 and forms[0][0] == "—"):
        lines.append("")
        lines.append("Forma / target bo'yicha:")
        for name, f in forms:
            lines.append(f"• {name}: {f['total']} ta — sifatli {f['qualified'] + f['sold']}, sifatsiz {f['unqualified']}, yangi {f['new']}")
    if c["by_manager"]:
        lines.append("")
        lines.append("Menejer bo'yicha:")
        for name, m in sorted(c["by_manager"].items(), key=lambda kv: -kv[1]["total"]):
            lines.append(f"• {name}: {m['total']} ta — sifatli {m['qualified'] + m['sold']}, sifatsiz {m['unqualified']}")
    lines.append("")
    lines.extend(_sales_lines(sales))
    if c["new"]:
        lines.append("")
        lines.append(f"⚠️ {c['new']} ta lid bilan hali bog'lanilmagan.")
    return "\n".join(lines)
