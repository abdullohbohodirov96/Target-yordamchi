"""
call_analytics.py — `db.CallRecord` yozuvlaridan "haqiqiy gaplashildimi"
tahlilini hisoblaydi.

ASOSIY MANTIQ (foydalanuvchi so'ragan qoidalar bo'yicha):
  - Bir XIL telefon raqamiga 2 SOAT ichida bir necha marta qo'ng'iroq
    qilingan bo'lsa -- bular ALOHIDA emas, BITTA "gaplashuv sessiyasi"
    deb hisoblanadi (masalan menejer uzilib qolgani uchun qayta qo'ng'iroq
    qilgan bo'lishi mumkin). Sessiya davomiyligi -- shu oraliqdagi barcha
    qo'ng'iroqlar davomiyligining YIG'INDISI, "necha marta" -- shu
    oraliqdagi qo'ng'iroqlar SONI.
  - STANDART: bitta odam bilan o'rtacha ~1 daqiqa (60 soniya) gaplashish
    kerak. Sessiya umumiy davomiyligi 60 soniyadan KAM bo'lsa -- "shubhali"
    (haqiqiy suhbat bo'lmagan, faqat "ushlab-qo'yib yubordim" qo'ng'iroq
    bo'lishi mumkin) deb belgilanadi.
"""

import datetime as dt
from collections import defaultdict

SESSION_GAP = dt.timedelta(hours=2)
MIN_REAL_TALK_SECONDS = 60


def group_call_sessions(calls: list) -> list[dict]:
    """`calls` -- bitta TELEFON RAQAMIGA tegishli `CallRecord` obyektlari
    ro'yxati (aralash raqamlar bo'lsa avval o'zi guruhlaydi). Qaytaradi:
    har biri {"started_at", "ended_at", "call_count", "total_duration",
    "is_suspicious", "manager_id", "calls": [...]} bo'lgan sessiyalar
    ro'yxati, VAQT bo'yicha o'sish tartibida."""
    by_phone = defaultdict(list)
    for c in calls:
        if not c.phone_number:
            continue
        by_phone[c.phone_number].append(c)

    sessions = []
    for phone, phone_calls in by_phone.items():
        dated = [c for c in phone_calls if c.started_at]
        dated.sort(key=lambda c: c.started_at)
        current = None
        for c in dated:
            if current is not None and (c.started_at - current["ended_at"]) <= SESSION_GAP:
                current["calls"].append(c)
                current["ended_at"] = max(current["ended_at"], c.started_at)
                current["total_duration"] += c.duration_seconds or 0
                current["call_count"] += 1
                if c.manager_id and not current["manager_id"]:
                    current["manager_id"] = c.manager_id
            else:
                if current is not None:
                    sessions.append(current)
                current = {
                    "phone_number": phone,
                    "started_at": c.started_at,
                    "ended_at": c.started_at,
                    "call_count": 1,
                    "total_duration": c.duration_seconds or 0,
                    "manager_id": c.manager_id,
                    "lead_id": c.lead_id,
                    "calls": [c],
                }
        if current is not None:
            sessions.append(current)

    for s in sessions:
        s["is_suspicious"] = s["total_duration"] < MIN_REAL_TALK_SECONDS

    sessions.sort(key=lambda s: s["started_at"] or dt.datetime.min)
    return sessions


def build_individual_check(session, since: dt.datetime) -> dict:
    """Admin uchun "Individual tekshirish" sahifasidagi to'liq ma'lumotni
    tayyorlaydi: har bir lead uchun (agar mos qo'ng'iroq topilsa) sessiyalar
    ro'yxati + menejerlar bo'yicha kunlik aloqa soni."""
    from db import CallRecord, Lead, Manager

    calls = session.query(CallRecord).filter(CallRecord.started_at >= since).all()
    all_sessions = group_call_sessions(calls)

    leads_by_id = {l.id: l for l in session.query(Lead).filter(Lead.id.in_(
        {s["lead_id"] for s in all_sessions if s["lead_id"]}
    )).all()} if any(s["lead_id"] for s in all_sessions) else {}
    managers_by_id = {m.id: m for m in session.query(Manager).all()}

    rows = []
    for s in all_sessions:
        lead = leads_by_id.get(s["lead_id"]) if s["lead_id"] else None
        manager = managers_by_id.get(s["manager_id"]) if s["manager_id"] else None
        rows.append({
            "phone_number": s["phone_number"],
            "lead_name": lead.full_name if lead else None,
            "lead_id": lead.id if lead else None,
            "manager_name": (manager.full_name or manager.username) if manager else "Noma'lum",
            "started_at": s["started_at"],
            "call_count": s["call_count"],
            "total_duration": s["total_duration"],
            "is_suspicious": s["is_suspicious"],
        })
    rows.sort(key=lambda r: r["started_at"] or dt.datetime.min, reverse=True)

    # Menejerlar bo'yicha kunlik aloqa soni (sessiya = 1 aloqa, sana bo'yicha guruhlangan)
    daily_by_manager = defaultdict(lambda: defaultdict(int))
    for s in all_sessions:
        if not s["manager_id"] or not s["started_at"]:
            continue
        manager = managers_by_id.get(s["manager_id"])
        mname = (manager.full_name or manager.username) if manager else f"ID {s['manager_id']}"
        day = s["started_at"].strftime("%Y-%m-%d")
        daily_by_manager[mname][day] += 1

    manager_summary = []
    for mname, by_day in daily_by_manager.items():
        total = sum(by_day.values())
        days_active = len(by_day)
        manager_summary.append({
            "manager_name": mname,
            "total_sessions": total,
            "days_active": days_active,
            "avg_per_day": round(total / days_active, 1) if days_active else 0,
        })
    manager_summary.sort(key=lambda m: m["total_sessions"], reverse=True)

    return {
        "sessions": rows,
        "manager_summary": manager_summary,
        "total_sessions": len(all_sessions),
        "suspicious_count": sum(1 for s in all_sessions if s["is_suspicious"]),
        "has_data": bool(calls),
    }


def build_daily_call_counts(session, manager_id: int, start: dt.datetime, end: dt.datetime, norm: int = 60) -> dict:
    """Bitta menejer uchun [start, end) oralig'idagi HAR KUNLIK "haqiqiy
    gaplashuv" sonini hisoblaydi -- Analitika sahifasidagi "Kunlik gaplashuv
    KPI" kartochkasi (kunlik 60 kishi bilan gaplashish rejasi) uchun.

    "Haqiqiy gaplashuv" = `group_call_sessions()` bo'yicha SHUBHASIZ (ya'ni
    umumiy davomiyligi >=60 soniya) sessiya -- bir xil raqamga 2 soat ichida
    bir necha marta qo'ng'iroq qilingan bo'lsa ham BITTA hisoblanadi.

    Qaytaradi: {
      "configured": bool,           -- Moi Zvonki umuman ulanganmi
      "days": [ {"date": "YYYY-MM-DD", "count": int, "pct": float,
                 "status": "met"|"missed"|"off"}, ... ],  -- kun bo'yicha, o'sish tartibida
      "days_met": int, "days_total": int,
    }
    "off" holati -- shu kunga OID hech qanday qo'ng'iroq yozuvi yo'q (dam olish
    kuni yoki hali ma'lumot kelmagan) -- bu "missed" (ishladi lekin reja
    bajarilmadi) dan farq qilishi uchun alohida belgilanadi."""
    import call_sync
    from db import CallRecord

    configured = call_sync.is_configured()

    calls = (
        session.query(CallRecord)
        .filter(
            CallRecord.manager_id == manager_id,
            CallRecord.started_at >= start,
            CallRecord.started_at < end,
        )
        .all()
    )
    all_sessions = group_call_sessions(calls)

    counts_by_day: dict[str, int] = defaultdict(int)
    activity_by_day: set = set()
    for c in calls:
        if c.started_at:
            activity_by_day.add(c.started_at.strftime("%Y-%m-%d"))
    for s in all_sessions:
        if s["started_at"] and not s["is_suspicious"]:
            day = s["started_at"].strftime("%Y-%m-%d")
            counts_by_day[day] += 1

    days = []
    cur = start.date() if isinstance(start, dt.datetime) else start
    end_date = end.date() if isinstance(end, dt.datetime) else end
    while cur < end_date:
        key = cur.strftime("%Y-%m-%d")
        count = counts_by_day.get(key, 0)
        if key not in activity_by_day:
            status = "off"
        elif count >= norm:
            status = "met"
        else:
            status = "missed"
        days.append({
            "date": key,
            "count": count,
            "pct": round((count / norm) * 100) if norm else 0,
            "status": status,
        })
        cur += dt.timedelta(days=1)

    days_with_activity = [d for d in days if d["status"] != "off"]
    return {
        "configured": configured,
        "norm": norm,
        "days": days,
        "days_met": sum(1 for d in days if d["status"] == "met"),
        "days_total": len(days_with_activity),
    }
