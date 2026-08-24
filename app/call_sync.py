"""
call_sync.py — "Mening qo'ng'iroqlarim" (Moi Zvonki, moizvonki.ru)
xizmatidan qo'ng'iroq yozuvlarini muntazam CRM bazasiga tortib oladi.

NEGA KERAK: menejer lead bilan HAQIQATAN gaplashganini (necha marta, qancha
davomiylikda) faqat menejerning o'zi yozgan izohidan bilib bo'lmaydi --
u "gaplashdim" deb yozib qo'yishi mumkin, aslida qo'ng'iroq umuman
bo'lmagan yoki 5 soniyada tashlab yuborgan bo'lishi mumkin. Shuning uchun
haqiqiy TELEFON qo'ng'iroq yozuvlari (davomiylik, vaqt) kerak --
"Individual tekshirish" bo'limi (`app.py`) shu ma'lumotdan lead'ning
telefon raqami bilan mos keladigan qo'ng'iroqlarni topib, 2 soat ichidagi
bir nechta qo'ng'iroqni BITTA "gaplashuv sessiyasi" deb hisoblab, umumiy
davomiylik va necha marta gaplashilganini ko'rsatadi.

MUHIM -- HOZIRGI HOLAT (2026-08): Moi Zvonki'ning ochiq (public) API
hujjatlari yo'q -- API manzili ("company.moizvonki.ru" ko'rinishida) va
kaliti FAQAT sizning shu xizmatdagi shaxsiy kabinetingizda (Sozlamalar ->
Integratsiya bo'limida) ko'rsatiladi. Shuning uchun bu fayl HOZIRCHA:
  1. Agar MOIZVONKI_API_ADDRESS / MOIZVONKI_API_KEY sozlanmagan bo'lsa --
     hech narsa qilmaydi, aniq xabar bilan qaytadi (xato bermaydi).
  2. Agar sozlangan bo'lsa -- eng keng tarqalgan REST naqshiga asoslanib
     (`{address}/api/calls?...`) so'rov yuborishga HARAKAT qiladi, lekin
     bu ENDPOINT ANIQ TASDIQLANMAGAN -- xizmat kabinetidagi haqiqiy
     hujjat/misol javobga qarab moslashtirish kerak bo'lishi mumkin. Xato
     bo'lsa ilova YIQILMAYDI, faqat aniq xatolik matni bilan qaytadi
     ("Individual tekshirish" sahifasida ko'rinadi).

Bu bo'lim to'liq ishlashi uchun quyidagilar kerak: (a) Moi Zvonki
kabinetidagi API manzil+kalit, (b) bitta haqiqiy qo'ng'iroq javobi
namunasi (JSON) -- shular asosida quyidagi `_map_raw_call()` funksiyasi
aniq maydonlarga moslashtiriladi.
"""

import os
import json
import logging
import datetime as dt

import requests

from db import get_session, CallRecord, Manager, Lead
from phone_utils import normalize_phone, phone_key9

logger = logging.getLogger("call_sync")

API_ADDRESS = os.environ.get("MOIZVONKI_API_ADDRESS", "").strip().rstrip("/")
API_KEY = os.environ.get("MOIZVONKI_API_KEY", "").strip()


def is_configured() -> bool:
    return bool(API_ADDRESS and API_KEY)


def _map_raw_call(raw: dict) -> dict:
    """Xizmatdan kelgan xom JSON'ni CallRecord maydonlariga moslaydi.
    ESLATMA: quyidagi kalit nomlari ("id"/"phone"/"duration"/"date"/...)
    ENG KENG TARQALGAN naqsh asosida taxminiy tanlangan -- haqiqiy javob
    boshqacha bo'lsa, shu funksiyani real namunaga qarab yangilash kerak."""
    return {
        "external_id": str(raw.get("id") or raw.get("call_id") or raw.get("uuid") or ""),
        "phone_number": normalize_phone(raw.get("phone") or raw.get("client_phone") or raw.get("from")),
        "manager_phone_number": normalize_phone(raw.get("employee_phone") or raw.get("internal_number") or raw.get("to")),
        "direction": raw.get("direction") or raw.get("type"),
        "duration_seconds": int(raw.get("duration") or raw.get("talk_duration") or 0),
        "started_at_raw": raw.get("date") or raw.get("started_at") or raw.get("created_at"),
        "recording_url": raw.get("record_url") or raw.get("recording") or raw.get("audio_url"),
    }


def _fetch_calls(since: dt.datetime | None = None) -> list[dict]:
    """Moi Zvonki API'dan qo'ng'iroqlar ro'yxatini so'raydi. Aniq endpoint
    tasdiqlangach shu funksiya yangilanadi -- hozircha eng ehtimolli REST
    naqsh (`GET {address}/api/calls`, `?key=`) bilan sinaydi."""
    params = {"key": API_KEY, "limit": 200}
    if since:
        params["date_from"] = since.strftime("%Y-%m-%d %H:%M:%S")
    r = requests.get(f"{API_ADDRESS}/api/calls", params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict):
        return data.get("data") or data.get("calls") or data.get("items") or []
    if isinstance(data, list):
        return data
    return []


def sync_once(since: dt.datetime | None = None) -> dict:
    """Bitta sinxronizatsiya tsiklini bajaradi. Qaytaradi:
    {"configured": bool, "new_calls": N, "errors": [...]}"""
    result = {"configured": is_configured(), "new_calls": 0, "errors": []}
    if not result["configured"]:
        result["errors"].append(
            "MOIZVONKI_API_ADDRESS / MOIZVONKI_API_KEY sozlanmagan -- "
            "Mening qo'ng'iroqlarim kabinetingizdagi Sozlamalar -> "
            "Integratsiya bo'limidan oling va Render environment "
            "o'zgaruvchilariga qo'shing."
        )
        return result

    if since is None:
        since = dt.datetime.utcnow() - dt.timedelta(days=2)

    try:
        raw_calls = _fetch_calls(since=since)
    except Exception as e:
        logger.exception("Moi Zvonki'dan ma'lumot olishda xatolik")
        result["errors"].append(f"Moi Zvonki bilan bog'lanib bo'lmadi: {e}")
        return result

    session = get_session()
    try:
        managers_by_phone = {}
        for m in session.query(Manager).filter(Manager.phone_number.isnot(None)).all():
            key = phone_key9(m.phone_number)
            if key:
                managers_by_phone[key] = m.id

        for raw in raw_calls:
            try:
                mapped = _map_raw_call(raw)
            except Exception:
                logger.exception("Qo'ng'iroq yozuvini o'qishda xatolik, o'tkazib yuborildi: %r", raw)
                continue

            external_id = mapped["external_id"]
            if external_id and session.query(CallRecord).filter_by(external_id=external_id).first():
                continue  # allaqachon bazada bor

            started_at = None
            raw_dt = mapped.get("started_at_raw")
            if raw_dt:
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                    try:
                        started_at = dt.datetime.strptime(str(raw_dt)[:19], fmt)
                        break
                    except ValueError:
                        continue

            phone_key = phone_key9(mapped["phone_number"])
            lead_id = None
            if phone_key:
                lead = session.query(Lead).filter(Lead.phone.ilike(f"%{phone_key}%")).first()
                if lead:
                    lead_id = lead.id

            manager_key = phone_key9(mapped["manager_phone_number"])
            manager_id = managers_by_phone.get(manager_key) if manager_key else None

            record = CallRecord(
                external_id=external_id or None,
                manager_id=manager_id,
                lead_id=lead_id,
                phone_number=mapped["phone_number"],
                manager_phone_number=mapped["manager_phone_number"],
                direction=mapped["direction"],
                duration_seconds=mapped["duration_seconds"],
                started_at=started_at,
                recording_url=mapped["recording_url"],
                raw_data=json.dumps(raw, ensure_ascii=False)[:8000],
            )
            session.add(record)
            result["new_calls"] += 1

        session.commit()
    finally:
        session.close()

    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(sync_once(), ensure_ascii=False, indent=2))
