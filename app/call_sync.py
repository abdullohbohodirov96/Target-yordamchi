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
    user_name'ning o'zinikini). Sahifalash: `results_remains`/
    `results_next_offset` orqali (0 bo'lsa tugagan).
  * `start_time`/`answer_time`/`end_time` -- UNIX timestamp (UTC), STRING
    SANA EMAS (ilgarigi taxmin xato edi).

MENEJERGA BIRIKTIRISH (2026-08, YAKUNIY holat -- debug orqali aniqlandi):
oldin FAQAT telefon (`src_number`) ishlatilgan edi, lekin haqiqiy Moi
Zvonki javobida `src_number` BA'ZI akkauntlarda BUTUNLAY BO'SH keladi --
shu sabab HAMMA qo'ng'iroq "mos kelmadi" deb o'tkazib yuborilardi (0 ta
CallRecord). Shuning uchun ENDI IKKI BOSQICHLI moslashtirish:

  1) TELEFON (`src_number` -> `Manager.phone_number`, oxirgi 9 xona
     bo'yicha) -- birinchi navbatda, agar to'ldirilgan bo'lsa.
  2) LOGIN (`user_account` -> `Manager.moizvonki_login`, kichik harfda
     aynan mos) -- telefon topilmasa fallback sifatida (ilgari bu usul
     "begona raqamlarni tortib kelyapti" degan shubha bilan olib
     tashlangan edi, lekin haqiqiy sabab aslida BOSHQA -- mos kelmagan
     qo'ng'iroqlar HAM bazaga yozilardi; ENDI bu tuzatilgan, pastga
     qarang -- shuning uchun login'ni qaytarish endi xavfsiz).

  Ikkalasi ham mos kelmasa -- bu qo'ng'iroq BAZAGA UMUMAN YOZILMAYDI
  (foydalanuvchi so'rovi: "faqat menejerga bog'liq qo'ng'iroqlarni
  hisoblasin, boshqa/egasiz raqamlarni tortmasin" -- bu YAGONA haqiqiy
  talab edi, "login ishlatilmasin" degani emas). Skip qilinganlar soni
  `sync_once()` natijasida "skipped_unmatched" sifatida qaytariladi
  (diagnostika uchun, bazaga yozilmaydi).

  Har bir menejer uchun KAMIDA BITTASI (telefon YOKI moizvonki_login)
  to'ldirilishi kerak -- ikkalasi ham bo'sh bo'lsa, o'sha menejerning
  qo'ng'iroqlari HECH QACHON tortilmaydi.

  ESKI (fix'lardan OLDIN yozilgan) noto'g'ri/begona qo'ng'iroqlarni
  bazadan tozalash uchun `reconcile_existing_records()` bor -- joriy
  menejerlar telefon RAQAMI VA login'iga qayta tekshirib, ikkalasiga ham
  mos kelmaganlarini O'CHIRADI. Manual trigger:
  `/api/trigger/call-cleanup?secret=...`.

  Xom (raw) qo'ng'iroq namunasi va menejerlar ma'lumotini yonma-yon
  ko'rish uchun `debug_sample_calls()` / `/api/trigger/call-debug` bor
  (vaqtinchalik diagnostika, xohlasa keyinroq olib tashlash mumkin).

Ishlashi uchun kerak (barchasi Render environment variables, HECH QACHON
kodga yozilmaydi):
  - MOIZVONKI_API_ADDRESS -- masalan "https://kompaniya.moizvonki.ru"
  - MOIZVONKI_API_KEY     -- Sozlamalar -> Integratsiya
  - MOIZVONKI_USER_NAME   -- akkauntga ADMIN sifatida kiradigan login
    (email) -- supervised=1 uchun shart

VA CRM ichida har bir menejer uchun (Menejerlar sahifasi) -- KAMIDA
BITTASI to'ldirilishi SHART:
  - Telefon raqami -- Moi Zvonki'da shu xodimga biriktirilgan SIM raqami
    bilan bir xil (agar akkauntda `src_number` to'ldirilib kelsa ishlaydi).
  - Moi Zvonki login/email -- shu xodimning Moi Zvonki tizimidagi shaxsiy
    login-emaili (ko'pincha CRM login'idan FARQLI, masalan avtomatik
    "ism.familiya.N@inbox.ru" formatida bo'lishi mumkin -- aniq qiymatni
    `/api/trigger/call-debug` orqali ko'rish mumkin, `user_account` maydoni).
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
    # "src_number" -- shu qo'ng'iroqni qilgan/qabul qilgan XODIMNING o'z
    # SIM raqami (mijozning "client_number"idan FARQLI) -- menejerga
    # birinchi navbatda shu orqali biriktiramiz (pastga qarang).
    employee_number = normalize_phone(raw.get("src_number"))

    return {
        "external_id": str(raw.get("db_call_id") or raw.get("event_pbx_call_id") or ""),
        "phone_number": normalize_phone(raw.get("client_number")),
        "moizvonki_login": login,
        "employee_number": employee_number,
        "direction": direction,
        "duration_seconds": int(raw.get("duration") or 0),
        "started_at": started_at,
        "recording_url": raw.get("recording") or None,
    }


def _build_managers_by_phone(session) -> dict:
    """{telefon_kaliti (oxirgi 9 xona): manager_id} -- FAOL menejerlarning
    `phone_number`i to'ldirilganlari uchun."""
    managers_by_phone = {}
    for m in session.query(Manager).filter(Manager.is_active == True).all():  # noqa: E712
        key = phone_key9(m.phone_number)
        if key:
            managers_by_phone[key] = m.id
    return managers_by_phone


def _build_managers_by_login(session) -> dict:
    """{moizvonki_login (kichik harf): manager_id} -- FAOL menejerlarning
    `moizvonki_login`i to'ldirilganlari uchun."""
    managers_by_login = {}
    for m in session.query(Manager).filter(Manager.is_active == True).all():  # noqa: E712
        key = (m.moizvonki_login or "").strip().lower()
        if key:
            managers_by_login[key] = m.id
    return managers_by_login


def _match_manager_id(managers_by_phone: dict, managers_by_login: dict, employee_number, login) -> int | None:
    """Menejerga biriktirish: AVVAL telefon (`src_number`), topilmasa LOGIN
    (`user_account`) orqali (2026-08 TUZATISH: `src_number` maydoni ba'zi
    Moi Zvonki akkauntlarida BUTUNLAY BO'SH keladi -- shu sabab faqat
    telefonga tayanish HAMMA qo'ng'iroqni "mos kelmadi" deb o'tkazib
    yuborishga olib kelgan edi. Login odatda ishonchli to'ldirilgan bo'ladi,
    shuning uchun fallback sifatida qaytarildi). Ikkalasi ham mos kelmasa --
    None (chaqiruvchi bu holda yozuvni SAQLAMAYDI, foydalanuvchi so'rovi:
    faqat menejerga bog'liq qo'ng'iroqlar hisoblansin)."""
    key = phone_key9(employee_number)
    manager_id = managers_by_phone.get(key) if key else None
    if manager_id:
        return manager_id
    return managers_by_login.get(login) if login else None


def reconcile_existing_records() -> dict:
    """Bazadagi BARCHA mavjud `CallRecord`larni joriy menejerlar telefon
    raqami VA Moi Zvonki login'iga qayta tekshiradi -- bu fix'dan OLDIN
    (umuman filtrsiz yoki eski mantiq bilan) yozilgan, menejerlarga
    aloqasi yo'q "begona" qo'ng'iroqlarni bazadan TOZALASH uchun
    (foydalanuvchi so'rovi: faqat menejerga bog'liq qo'ng'iroqlar qolsin).
    Login'ni saqlangan `raw_data` JSON'idan (`user_account`) qayta o'qiydi --
    alohida ustun sifatida saqlanmagan.

    Har bir yozuv uchun:
      - Telefon (`manager_phone_number`) yoki login joriy biror menejerga
        mos kelsa -- `manager_id` shunga TENGLASHTIRILADI (eskirgan/noto'g'ri
        bo'lsa ham tuzatiladi).
      - Ikkalasi ham mos kelmasa -- yozuv O'CHIRILADI.

    Qaytaradi: {"total": N, "reassigned": N, "deleted": N, "kept": N}."""
    session = get_session()
    try:
        managers_by_phone = _build_managers_by_phone(session)
        managers_by_login = _build_managers_by_login(session)
        records = session.query(CallRecord).all()
        stats = {"total": len(records), "reassigned": 0, "deleted": 0, "kept": 0}
        for r in records:
            login = None
            if r.raw_data:
                try:
                    login = (json.loads(r.raw_data).get("user_account") or "").strip().lower() or None
                except (ValueError, AttributeError):
                    login = None
            manager_id = _match_manager_id(managers_by_phone, managers_by_login, r.manager_phone_number, login)
            if manager_id is None:
                session.delete(r)
                stats["deleted"] += 1
            elif r.manager_id != manager_id:
                r.manager_id = manager_id
                stats["reassigned"] += 1
                stats["kept"] += 1
            else:
                stats["kept"] += 1
        session.commit()
        return stats
    finally:
        session.close()


def debug_sample_calls(n: int = 5) -> dict:
    """VAQTINCHALIK DIAGNOSTIKA (2026-08): foydalanuvchi menejer telefon
    raqamlarini to'g'ri to'ldirgan bo'lsa-yu, BARCHA qo'ng'iroqlar
    "skipped_unmatched" bo'lib chiqsa -- demak Moi Zvonki API'dan kelayotgan
    `src_number` maydoni biz taxmin qilgandek "xodimning SIM raqami" emas
    (bo'sh, ichki extension, yoki boshqa narsa bo'lishi mumkin). Bu funksiya
    bir nechta XOM (raw) qo'ng'iroq yozuvini va joriy menejerlarning
    bazadagi telefon raqamlarini yonma-yon qaytaradi -- solishtirib,
    haqiqiy sababni ko'rish uchun. `/api/trigger/call-debug`."""
    result = {"configured": is_configured(), "raw_samples": [], "managers_phone_numbers": [], "errors": []}
    if not result["configured"]:
        result["errors"].append("Moi Zvonki sozlanmagan.")
        return result
    try:
        raw_calls = _fetch_calls(since=dt.datetime.utcnow() - dt.timedelta(days=2))
    except Exception as e:
        result["errors"].append(f"Moi Zvonki bilan bog'lanib bo'lmadi: {e}")
        return result
    result["total_fetched"] = len(raw_calls)
    result["raw_samples"] = raw_calls[:n]

    session = get_session()
    try:
        for m in session.query(Manager).filter(Manager.is_active == True).all():  # noqa: E712
            result["managers_phone_numbers"].append({
                "id": m.id, "full_name": m.full_name, "username": m.username,
                "phone_number": m.phone_number, "phone_key9": phone_key9(m.phone_number),
                "moizvonki_login": m.moizvonki_login,
            })
    finally:
        session.close()
    return result


def sync_once(since: dt.datetime | None = None) -> dict:
    """Bitta sinxronizatsiya tsiklini bajaradi. Qaytaradi:
    {"configured": bool, "new_calls": N, "skipped_unmatched": N, "errors": [...]}
    `skipped_unmatched` -- API'dan kelgan, lekin HECH QAYSI menejerga
    (telefon ham, login ham) biriktirilmagani uchun BAZAGA YOZILMAGAN
    qo'ng'iroqlar soni (butun kompaniyaning egasiz qo'ng'iroqlari CRM'ga
    tushmasligi uchun -- foydalanuvchi so'rovi)."""
    result = {"configured": is_configured(), "new_calls": 0, "skipped_unmatched": 0, "errors": []}
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
        managers_by_phone = _build_managers_by_phone(session)
        managers_by_login = _build_managers_by_login(session)

        for raw in raw_calls:
            try:
                mapped = _map_raw_call(raw)
            except Exception:
                logger.exception("Qo'ng'iroq yozuvini o'qishda xatolik, o'tkazib yuborildi: %r", raw)
                continue

            external_id = mapped["external_id"]
            if external_id and session.query(CallRecord).filter_by(external_id=external_id).first():
                continue  # allaqachon bazada bor

            # --- Menejerga biriktirish: AVVAL telefon (xodimning SIM raqami
            # -- "src_number"), topilmasa LOGIN ("user_account") orqali
            # (ba'zi akkauntlarda src_number bo'sh keladi). Ikkalasi ham
            # mos kelmasa -- bu qo'ng'iroq BAZAGA UMUMAN YOZILMAYDI
            # (foydalanuvchi so'rovi: faqat menejerga bog'liq qo'ng'iroqlar
            # hisoblansin, boshqa raqamlar/egasizlar tortilmasin). ---
            manager_id = _match_manager_id(
                managers_by_phone, managers_by_login, mapped["employee_number"], mapped["moizvonki_login"]
            )
            if not manager_id:
                result["skipped_unmatched"] += 1
                continue

            phone_key = phone_key9(mapped["phone_number"])
            lead_id = None
            if phone_key:
                lead = session.query(Lead).filter(Lead.phone.ilike(f"%{phone_key}%")).first()
                if lead:
                    lead_id = lead.id

            record = CallRecord(
                external_id=external_id or None,
                manager_id=manager_id,
                lead_id=lead_id,
                phone_number=mapped["phone_number"],
                manager_phone_number=mapped["employee_number"],
                direction=mapped["direction"],
                duration_seconds=mapped["duration_seconds"],
                started_at=mapped["started_at"],
                recording_url=mapped["recording_url"],
                raw_data=json.dumps(raw, ensure_ascii=False)[:8000],
            )
            session.add(record)
            result["new_calls"] += 1

        if result["skipped_unmatched"]:
            result["errors"].append(
                f"{result['skipped_unmatched']} ta qo'ng'iroq hech qaysi menejerning telefon "
                "raqamiga mos kelmagani uchun bazaga yozilmadi -- Menejerlar sahifasida shu "
                "xodimning Moi Zvonki'dagi SIM raqamini 'Telefon raqami' maydoniga aynan bir "
                "xil qilib kiritib qo'ying."
            )

        session.commit()
    finally:
        session.close()

    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(sync_once(), ensure_ascii=False, indent=2))
