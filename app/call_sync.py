"""
call_sync.py — "Mening qo'ng'iroqlarim" (Moi Zvonki, moizvonki.ru)
xizmatidan qo'ng'iroq yozuvlarini muntazam CRM bazasiga tortib oladi.

NEGA KERAK: menejer lead bilan HAQIQATAN gaplashganini (necha marta, qancha
davomiylikda) faqat menejerning o'zi yozgan izohidan bilib bo'lmaydi --
u "gaplashdim" deb yozib qo'yishi mumkin, aslida qo'ng'iroq umuman
bo'lmagan yoki 5 soniyada tashlab yuborgan bo'lishi mumkin. Shuning uchun
haqiqiy TELEFON qo'ng'iroq yozuvlari (davomiylik, vaqt) kerak --
"Individual tekshirish" bo'limi va Analitika/Dashboard'dagi kunlik
gaplashuv KPI shu ma'lumotdan foydalanadi.

RASMIY API (2026-08, foydalanuvchi Moi Zvonki kabinetidan yuborgan
https://www.moizvonki.ru/guide/api/ hujjati asosida -- ILGARI bu fayl
tasdiqlanmagan taxmin bilan yozilgan edi, ENDI aniq kontrakt):

  * So'rov: POST https://{domain}/api/v1, forma-kodlangan tana
    `request_data=<JSON matn>` (hujjatdagi jQuery misoliga mos).
  * HAR bir so'rovda UCHTA autentifikatsiya maydoni kerak:
      - user_name -- akkauntning LOGIN email'i (ADMINISTRATOR bo'lishi
        kerak, aks holda supervised=1 butun kompaniya emas faqat shu
        foydalanuvchining o'z qo'ng'iroqlarini qaytaradi)
      - api_key   -- Sozlamalar -> Integratsiya'dagi API kalit
      - action    -- chaqirilayotgan metod nomi (masalan "calls.list")
  * Qo'ng'iroqlar ro'yxati: action="calls.list", `supervised=1` bilan
    BARCHA xodimlarning qo'ng'irog'ini qaytaradi (aks holda faqat
    user_name'ning o'zinikini). Javobda HAR BIR qo'ng'iroq egasi
    "user_account" (email) orqali aniqlanadi -- TELEFON RAQAMI ORQALI
    EMAS. Shuning uchun `Manager.moizvonki_login` (har bir menejerning
    Moi Zvonki login-email'i) admin tomonidan qo'lda to'ldirilishi kerak
    -- aks holda qo'ng'iroq hech qaysi menejerga biriktirilmaydi (lekin
    baribir bazaga yoziladi, keyin admin login'ni to'g'irlasa qayta
    ishlov berish shart emas -- yangi qo'ng'iroqlar to'g'ri biriktiriladi).
  * Sahifalash: `results_remains`/`results_next_offset` orqali (0 bo'lsa
    tugagan).
  * `start_time`/`answer_time`/`end_time` -- UNIX timestamp (UTC), STRING
    SANA EMAS (ilgarigi taxmin xato edi).

Ishlashi uchun kerak (barchasi Render environment variables, HECH QACHON
kodga yozilmaydi):
  - MOIZVONKI_API_ADDRESS -- masalan "https://kompaniya.moizvonki.ru"
  - MOIZVONKI_API_KEY     -- Sozlamalar -> Integratsiya
  - MOIZVONKI_USER_NAME   -- akkauntga ADMIN sifatida kiradigan login
    (email) -- supervised=1 uchun shart
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
API_USER_NAME = os.environ.get("MOIZVONKI_USER_NAME", "").strip()

_MAX_PAGES = 50  # xavfsizlik cheklovi -- server noto'g'ri offset qaytarsa ham cheksiz tsiklga tushib qolmaslik uchun


class MoiZvonkiError(Exception):
    pass


def is_configured() -> bool:
    return bool(API_ADDRESS and API_KEY and API_USER_NAME)


def _call_api(action: str, **params) -> dict:
    """Moi Zvonki REST API'ga bitta so'rov yuboradi
    (https://www.moizvonki.ru/guide/api/#rest)."""
    payload = {"user_name": API_USER_NAME, "api_key": API_KEY, "action": action}
    payload.update(params)
    url = f"{API_ADDRESS}/api/v1"
    r = requests.post(url, data={"request_data": json.dumps(payload, ensure_ascii=False)}, timeout=30)
    if r.status_code != 200:
        raise MoiZvonkiError(f"{action}: HTTP {r.status_code} -- {r.text[:300]}")
    try:
        return r.json()
    except ValueError:
        raise MoiZvonkiError(f"{action}: JSON bo'lmagan javob -- {r.text[:300]}")


def _fetch_calls(since: dt.datetime) -> list[dict]:
    """`calls.list` metodini chaqirib, `since`dan keyingi BARCHA (butun
    kompaniya, supervised=1) qo'ng'iroqlarni sahifalab yig'ib qaytaradi."""
    from_date = int(since.replace(tzinfo=dt.timezone.utc).timestamp())
    all_results: list[dict] = []
    offset = 0
    for _ in range(_MAX_PAGES):
        data = _call_api(
            "calls.list",
            from_date=from_date,
            max_results=100,
            from_offset=offset,
            supervised=1,
        )
        results = data.get("results") or []
        all_results.extend(results)
        remains = data.get("results_remains") or 0
        next_offset = data.get("results_next_offset")
        if not remains or next_offset is None or next_offset <= offset:
            break
        offset = next_offset
    return all_results


def _map_raw_call(raw: dict) -> dict:
    """Moi Zvonki `calls.list` javobidagi bitta yozuvni CallRecord
    maydonlariga moslaydi (rasmiy hujjat asosida)."""
    direction_raw = raw.get("direction")
    direction = "outgoing" if direction_raw == 1 else "incoming" if direction_raw == 0 else None

    started_at = None
    st = raw.get("start_time")
    if st:
        try:
            started_at = dt.datetime.utcfromtimestamp(int(st))
        except (TypeError, ValueError, OSError, OverflowError):
            started_at = None

    login = (raw.get("user_account") or "").strip().lower() or None

    return {
        "external_id": str(raw.get("db_call_id") or raw.get("event_pbx_call_id") or ""),
        "phone_number": normalize_phone(raw.get("client_number")),
        "moizvonki_login": login,
        "direction": direction,
        "duration_seconds": int(raw.get("duration") or 0),
        "started_at": started_at,
        "recording_url": raw.get("recording") or None,
    }


def sync_once(since: dt.datetime | None = None) -> dict:
    """Bitta sinxronizatsiya tsiklini bajaradi. Qaytaradi:
    {"configured": bool, "new_calls": N, "unmatched_manager_calls": N, "errors": [...]}"""
    result = {"configured": is_configured(), "new_calls": 0, "unmatched_manager_calls": 0, "errors": []}
    if not result["configured"]:
        missing = [
            name for name, val in (
                ("MOIZVONKI_API_ADDRESS", API_ADDRESS),
                ("MOIZVONKI_API_KEY", API_KEY),
                ("MOIZVONKI_USER_NAME", API_USER_NAME),
            ) if not val
        ]
        result["errors"].append(
            f"Sozlanmagan: {', '.join(missing)} -- Mening qo'ng'iroqlarim kabinetidagi "
            "Sozlamalar -> Integratsiya bo'limidan manzil+kalitni, va akkauntga ADMIN "
            "sifatida kiradigan login(email)ni Render environment o'zgaruvchilariga qo'shing."
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
        managers_by_login = {}
        for m in session.query(Manager).filter(Manager.moizvonki_login.isnot(None)).all():
            key = (m.moizvonki_login or "").strip().lower()
            if key:
                managers_by_login[key] = m.id

        for raw in raw_calls:
            try:
                mapped = _map_raw_call(raw)
            except Exception:
                logger.exception("Qo'ng'iroq yozuvini o'qishda xatolik, o'tkazib yuborildi: %r", raw)
                continue

            external_id = mapped["external_id"]
            if external_id and session.query(CallRecord).filter_by(external_id=external_id).first():
                continue  # allaqachon bazada bor

            phone_key = phone_key9(mapped["phone_number"])
            lead_id = None
            if phone_key:
                lead = session.query(Lead).filter(Lead.phone.ilike(f"%{phone_key}%")).first()
                if lead:
                    lead_id = lead.id

            manager_id = managers_by_login.get(mapped["moizvonki_login"]) if mapped["moizvonki_login"] else None
            if mapped["moizvonki_login"] and not manager_id:
                result["unmatched_manager_calls"] += 1

            record = CallRecord(
                external_id=external_id or None,
                manager_id=manager_id,
                lead_id=lead_id,
                phone_number=mapped["phone_number"],
                manager_phone_number=None,
                direction=mapped["direction"],
                duration_seconds=mapped["duration_seconds"],
                started_at=mapped["started_at"],
                recording_url=mapped["recording_url"],
                raw_data=json.dumps(raw, ensure_ascii=False)[:8000],
            )
            session.add(record)
            result["new_calls"] += 1

        if result["unmatched_manager_calls"]:
            result["errors"].append(
                f"{result['unmatched_manager_calls']} ta qo'ng'iroq hech qaysi menejerga "
                "biriktirilmadi -- Menejerlar sahifasida shu qo'ng'iroqlar egasining Moi "
                "Zvonki login(email)ini 'moizvonki_login' maydoniga kiritish kerak."
            )

        session.commit()
    finally:
        session.close()

    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(sync_once(), ensure_ascii=False, indent=2))
