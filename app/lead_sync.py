"""
lead_sync.py — Meta Lead Ads'dagi lidlarni muntazam (masalan har 15 daqiqada)
Postgres CRM bazasiga tortib oladi.

NEGA WEBHOOK EMAS (hozircha): Meta'ning haqiqiy real-vaqt lead webhook'i
alohida Meta App yaratish + shu Page'ni App'ga ulash + ba'zi hollarda App
Review talab qiladi -- bu boshlang'ich bosqichda ortiqcha to'siq. Polling
(har necha daqiqada so'rash) ancha sodda va tezda ishga tushadi; keyinchalik
xohlasa, `/leads_webhook` endpoint qo'shib to'liq real-vaqtga o'tish mumkin.

Oqim:
  1. META_PAGE_ID sahifadagi barcha Instant Form'larni ro'yxatlaydi.
  2. Har bir forma uchun yangi lidlarni so'raydi (`meta_api.get_leads`).
  3. Har bir lead uchun campaign_id/adset_id/ad_id orqali kampaniya NOMINI
     (`meta_api.get_account_structure`dan keshlangan xarita) biriktiradi.
  4. `meta_lead_id` bo'yicha dublikatni tekshirib, faqat YANGI lidlarni
     `leads` jadvaliga yozadi (status="new").

DIAGNOSTIKA (2026-08): "target yonib turibdi, lead kelgan, lekin CRM'ga
tushmayapti" kabi shikoyatlarni tekshirish oson bo'lishi uchun, har bir
sinxronizatsiya Meta'ning O'ZI qaytargan `leads_count` (shu forma UMUMAN
qancha lead olgan) bilan bizning bazamizdagi shu forma bo'yicha yozuvlar
sonini SOLISHTIRADI va natijaga qo'shadi (`form_diagnostics`). Katta farq
bo'lsa -- token/ruxsat muammosi yoki noto'g'ri META_PAGE_ID ehtimoli baland.
Oxirgi sinxronizatsiya natijasi `kv_store`ga ("lead_sync_status" kaliti)
yoziladi -- admin buni Analitika sahifasida ko'ra oladi, cron ishlab-
ishlamayotganini bilish uchun endi Render loglariga qarash shart emas.

"FAQAT YANGI LIDLAR" CURSOR (2026-08): oldin `meta_api.get_leads()` HAR
SAFAR forma bo'yicha BUTUN tarixiy lead ro'yxatini so'rar edi (Meta'ning
o'zi shunday qaytaradi, `since` filtri berilmasa) -- bazadagi dublikatni
tekshirish faqat `meta_lead_id` bo'yicha bo'lgani uchun bu odatda muammo
bermas edi. LEKIN (#190) Page Access Token xatoligi tuzatilgandan keyin
BIRINCHI muvaffaqiyatli sync butun tarixiy backlog'ni "yangi" deb bazaga
yozib yubordi. Shuni oldini olish uchun endi `kv_store`da
("lead_sync_since_unix" kaliti) OXIRGI muvaffaqiyatli tekshiruv vaqti
(unix timestamp, kichik overlap bilan) saqlanadi va har safar
`meta_api.get_leads(form_id, since=...)`ga uzatiladi -- shunday qilib har
bir sync FAQAT o'sha vaqtdan keyin yaratilgan lidlarni so'raydi. Agar bu
kalit hali mavjud bo'lmasa (birinchi marta ishga tushish yoki qo'lda
tozalangan holat), ESKI TARIXni tortib olmaslik uchun bu safar HECH QANDAY
lead so'ralmaydi -- faqat cursor "hozir"ga o'rnatiladi, keyingi
tekshiruvdan boshlab ENDI yaratiladigan lidlar tortiladi.
"""

import json
import logging
import re
import threading
import time
import datetime as dt

from sqlalchemy.exc import IntegrityError

import meta_api
import kv_store
import db
from db import get_session, Lead
from phone_utils import normalize_phone, clean_phone_raw

logger = logging.getLogger("lead_sync")

# Cursor'ni oldinga surganda shuncha soniyaga orqaga chekinamiz -- soat
# farqi yoki Meta'ning lead'ni indekslashdagi kechikishi tufayli chegara
# atrofidagi lead yo'qolib qolmasligi uchun (meta_lead_id dublikat
# tekshiruvi bor, shuning uchun overlap xavfsiz -- eng ko'pi bilan bir xil
# lead ikki marta tekshiriladi, lekin ikki marta YOZILMAYDI).
# 2026-10: 600 s -> 2 soat. Meta ba'zan lidni API'da yaratilganidan 10+
# daqiqa keyin ko'rsatadi -- tor oyna bunday lidlarni butunlay o'tkazib
# yuborardi ("lidlar ba'zan tushmay qoladi"). Dublikat tekshiruvi bor, shuning
# uchun keng oyna faqat bir xil lidlarni qayta TEKSHIRADI, qayta yozmaydi.
_SYNC_OVERLAP_SECONDS = 2 * 3600

# Bir kompaniya uchun bir vaqtda faqat BITTA sync (ulanishdan keyingi darhol
# sync + 15 daqiqalik cron bir vaqtga to'g'ri kelsa, ikkalasi bir xil lidni
# yozishga urinib IntegrityError bilan yiqilardi).
_company_locks: dict = {}
_company_locks_guard = threading.Lock()
# Bitta sync bundan uzoq "ishlayotgan" bo'lsa -- u qotib qolgan deb hisoblanadi
# va keyingi sync'lar uni kutmaydi (aks holda bitta qotgan sync shu kompaniyaning
# lidlarini ABADIY to'xtatib qo'yardi).
_LOCK_STALE_SECONDS = 15 * 60


def _company_lock(company_id) -> threading.Lock:
    with _company_locks_guard:
        entry = _company_locks.get(company_id)
        if entry is None:
            entry = _company_locks[company_id] = {"lock": threading.Lock(), "since": None}
        return entry["lock"]


def _acquire_company_lock(company_id) -> "threading.Lock | None":
    """Qulfni oladi; band bo'lsa va eskirgan bo'lsa -- yangisini yaratadi."""
    import time as _time
    lock = _company_lock(company_id)
    if lock.acquire(blocking=False):
        _company_locks[company_id]["since"] = _time.monotonic()
        return lock
    with _company_locks_guard:
        entry = _company_locks[company_id]
        since = entry.get("since")
        if since is not None and _time.monotonic() - since > _LOCK_STALE_SECONDS:
            logger.warning("Lead sync (company_id=%s): oldingi sync %s s'dan beri tugamagan -- qotgan deb hisoblanadi",
                           company_id, int(_time.monotonic() - since))
            entry["lock"] = threading.Lock()
            entry["lock"].acquire()
            entry["since"] = _time.monotonic()
            return entry["lock"]
    return None


# 2026-09 multi-tenant: har bir kompaniyaning O'Z cursor/holat kaliti bo'lishi
# kerak -- aks holda kompaniya A'ning "oxirgi tekshirilgan vaqti" kompaniya
# B'ning sinxronizatsiyasiga ta'sir qilib qo'yadi (yoki aksincha). Standart
# (asosiy) kompaniya uchun ESKI, prefikssiz kalit saqlab qolinadi -- shu orqali
# allaqachon ishlab turgan deploy'larning cursor holati YO'QOLMAYDI
# (`ig_dm_sync._status_key()` bilan bir xil naqsh).
def _since_key(company_id: "int | None") -> str:
    return "lead_sync_since_unix" if company_id is None else f"lead_sync_since_unix:{company_id}"


def _backlog_key(company_id: "int | None") -> str:
    # Cursor birinchi marta o'rnatilgan ANIQ vaqt -- keyinchalik `_since_key()`
    # har sync'da OLDINGA suriladi, lekin bu qiymat O'ZGARMAS qoladi (faqat bir
    # marta yoziladi). `cleanup_backlog_leads()` shu chegaradan FOYDALANIB, faqat
    # tuzatishdan OLDIN (ya'ni buzilgan birinchi sync paytida) yozilgan eski
    # lidlarnigina o'chiradi -- tuzatishdan KEYIN kelgan haqiqiy yangi lidlarga
    # HECH QACHON tegmaydi, cleanup qachon bosilishidan qat'iy nazar.
    return "lead_sync_backlog_cutoff_unix" if company_id is None else f"lead_sync_backlog_cutoff_unix:{company_id}"


def _status_key(company_id: "int | None") -> str:
    return "lead_sync_status" if company_id is None else f"lead_sync_status:{company_id}"


def _form_since_key(company_id: "int | None") -> str:
    # 2026-10: har bir forma uchun ALOHIDA cursor ({form_id: unix}). Ilgari
    # bitta umumiy cursor bor edi va biror forma xato bersa ham u oldinga
    # surilardi -- o'sha formadagi lidlar butunlay yo'qolardi. Endi xato bergan
    # formaning cursori joyida qoladi va keyingi safar shu oynadan qayta o'qiladi.
    return "lead_sync_form_since" if company_id is None else f"lead_sync_form_since:{company_id}"

# Meta forma savollari ko'pincha standart ingliz kalitlari bilan keladi
# (full_name, phone_number, email), lekin ADMIN o'zi qo'shgan maxsus savol
# bo'lsa, kalit o'sha savol matnidan avtomatik generatsiya qilinadi (masalan
# "Ismingizni kiriting?" -> "ismingizni_kiriting") -- shuning uchun keng
# ro'yxat + fallback qidiruv kerak.
_NAME_KEYS = (
    "full_name", "name", "your_name", "customer_name",
    "ism", "ismi", "ism_familiya", "ismingiz", "ismingizni_kiriting",
    "toliq_ism", "to'liq_ism", "familiya", "fio", "имя", "фио",
)
_PHONE_KEYS = (
    "phone_number", "phone", "mobile", "contact_number",
    "telefon", "telefon_raqami", "telefon_raqamingiz", "raqam", "raqamingiz",
    "nomer", "nomeringiz", "telefon_raqamingizni_kiriting", "телефон", "номер",
)
_EMAIL_KEYS = ("email", "e-mail", "email_address", "pochta", "elektron_pochta", "почта")

# Bu kalitlar hech qachon ism/telefon/email BO'LMAYDI -- fallback qidiruvda
# ularni chetlab o'tish uchun (aks holda "campaign_name" kabi maydonlar
# ism sifatida noto'g'ri o'qilishi mumkin).
_NEVER_NAME_OR_PHONE_KEYS = {
    "campaign_name", "campaign_id", "adset_name", "adset_id", "ad_name", "ad_id",
    "form_id", "form_name", "created_time", "platform", "is_organic", "lead_status",
}


def _field_data_to_dict(field_data: list[dict]) -> dict:
    """Meta lead javobini {"full_name": "...", "phone_number": "...", ...}
    ko'rinishiga soddalashtiradi -- forma savollari ixtiyoriy nom bilan
    kelgani uchun eng keng tarqalgan kalitlarni tanib olishga harakat qiladi."""
    out = {}
    for item in field_data or []:
        name = (item.get("name") or "").lower()
        values = item.get("values") or []
        out[name] = values[0] if values else None
    return out


def _looks_phoneish(value) -> bool:
    s = clean_phone_raw(value)
    if not s:
        return False
    digits = re.sub(r"\D", "", s)
    return 7 <= len(digits) <= 13


def _looks_nameish(value) -> bool:
    s = str(value or "").strip()
    if not (2 <= len(s) <= 80) or _looks_phoneish(value):
        return False
    if any(ch in s for ch in ("_", "(", ")", "²", "%", "@")):
        return False
    letters = sum(1 for ch in s if ch.isalpha())
    return letters >= max(2, len(s) * 0.5)


def _find_by_keys(fd: dict, keys: tuple) -> str | None:
    for k in keys:
        if k in fd and fd[k]:
            return fd[k]
    # Substring moslik -- Meta ba'zan kalitga qo'shimcha so'z qo'shib
    # yuboradi (masalan "phone_number_1").
    for fk, v in fd.items():
        if v and any(k in fk for k in keys):
            return v
    return None


def _find_all_by_keys(fd: dict, keys: tuple) -> list[str]:
    """`_find_by_keys` bilan bir xil moslik mantig'i, lekin FAQAT birinchi
    topilganini emas, mos kelgan HAMMA (takrorlanmagan) qiymatlarni,
    formadagi savollar tartibida qaytaradi -- 2026-09, foydalanuvchi
    so'rovi: "ikkita nomerdan bittasi tushmayapti" -- ba'zi Instant
    Form'larda ikkita alohida telefon-turidagi savol bo'ladi (masalan
    "sizning raqamingiz" + "yaqiningizning raqami"), ikkalasi ham
    kerak, biri emas."""
    found = []
    for fk, v in fd.items():
        if not v:
            continue
        if fk in keys or any(k in fk for k in keys):
            if v not in found:
                found.append(v)
    return found


def _extract_name_phone_email(fd: dict) -> tuple[str | None, str | None, str | None, str | None]:
    """Qaytaradi: (name, phone, phone2, email). `phone2` -- formada
    IKKINCHI telefon-turidagi savol bo'lsa (masalan qo'shimcha/yaqin
    kishining raqami), shu yerga tushadi; bo'lmasa `None`."""
    name = _find_by_keys(fd, _NAME_KEYS)
    if not name:
        first = fd.get("first_name", "")
        last = fd.get("last_name", "")
        name = f"{first} {last}".strip() or None
    phone_candidates = _find_all_by_keys(fd, _PHONE_KEYS)
    email = _find_by_keys(fd, _EMAIL_KEYS)

    # Fallback: agar standart/keng tarqalgan kalitlar orasida topilmasa,
    # QOLGAN barcha maydonlarni skanerlab, telefon/ism'ga O'XSHAGANINI
    # taxmin qilamiz -- bu aynan Excel import'da ishlagan mantiq bilan bir xil
    # (localised savol matnidan generatsiya qilingan g'alati kalitlar uchun).
    if not phone_candidates or not name:
        for k, v in fd.items():
            if k in _NEVER_NAME_OR_PHONE_KEYS or not v:
                continue
            if _looks_phoneish(v) and v not in phone_candidates:
                phone_candidates.append(v)
                continue
            if not name and _looks_nameish(v):
                name = v

    phone = phone_candidates[0] if phone_candidates else None
    phone2 = phone_candidates[1] if len(phone_candidates) > 1 else None

    if phone:
        normalized = normalize_phone(phone)
        if normalized:
            phone = normalized
    if phone2:
        normalized2 = normalize_phone(phone2)
        if normalized2:
            phone2 = normalized2
        # Ikkinchi raqam birinchisi bilan (normalizatsiyadan keyin) bir xil
        # chiqib qolsa -- bu ikkita savol emas, xuddi shu javobning
        # ikki xil formatdagi nusxasi, dublikat sifatida ko'rsatmaymiz.
        if phone2 == phone:
            phone2 = None
    if isinstance(email, str) and "@" not in email:
        # Ba'zan email deb nomlangan maydonga aslida boshqa narsa tushadi --
        # shubhali bo'lsa, email sifatida saqlamaymiz (bo'sh qoldiramiz).
        email = None
    return name, phone, phone2, email


_RECOGNIZED_KEY_SUBSTRINGS = _NAME_KEYS + _PHONE_KEYS + _EMAIL_KEYS + ("first_name", "last_name")


def other_form_answers(raw_field_data_json: "str | None", *, exclude_values: "list | None" = None) -> list[tuple[str, str]]:
    """`Lead.raw_field_data`dagi (Meta forma javoblarining TO'LIQ JSON
    nusxasi) ism/telefon/telefon2/email sifatida ALLAQACHON tanib olingan
    va CRM'da alohida ko'rsatiladigan maydonlardan boshqa -- ADMIN Meta
    Instant Form'ga o'zi qo'shgan qolgan har qanday savol/javobni
    qaytaradi (2026-09, foydalanuvchi so'rovi: "hamma savolga javobi
    tushadigan bo'lsin" -- forma qanday savol bo'lmasin, javobi CRM'da
    ko'rinishi kerak, faqat kod tanib oladigan standart maydonlar emas).

    Ekrandagi label sifatida Meta'ning o'zi bergan maydon kalitini
    (masalan "qoshimcha_izoh") ko'rsatamiz -- Meta webhook/API javobida
    savolning to'liq matni kelmaydi, faqat shu slug keladi."""
    if not raw_field_data_json:
        return []
    try:
        fd = json.loads(raw_field_data_json)
    except (TypeError, ValueError):
        return []
    if not isinstance(fd, dict):
        return []
    exclude_values = {v for v in (exclude_values or []) if v}
    out = []
    for k, v in fd.items():
        if not v or v in exclude_values:
            continue
        if k in _NEVER_NAME_OR_PHONE_KEYS:
            continue
        if k in _RECOGNIZED_KEY_SUBSTRINGS or any(rk in k for rk in _RECOGNIZED_KEY_SUBSTRINGS):
            continue
        out.append((k, v))
    return out


def sync_once(company=None) -> dict:
    """Kompaniya bo'yicha qulf bilan `_sync_once_impl` -- izohi o'sha yerda."""
    company_id = company.id if company else None
    lock = _acquire_company_lock(company_id)
    if lock is None:
        return {"new_leads": 0, "forms_checked": 0, "errors": [], "form_diagnostics": [],
                "notices": ["Bu kompaniya uchun boshqa sinxronizatsiya hozir ishlayapti -- bu safar o'tkazib yuborildi."],
                "skipped": True}
    try:
        return _sync_once_impl(company=company)
    finally:
        try:
            lock.release()
        except RuntimeError:
            pass


@db.company_scoped
def _sync_once_impl(company=None) -> dict:
    """Bitta sinxronizatsiya tsiklini bajaradi. `company` berilsa (yengil
    `_CompanyCreds` yoki `db.Company` qatori) -- O'SHA kompaniyaning O'Z
    `meta_page_id`/`meta_access_token`/`meta_ad_account_id`i bilan, natija
    shu kompaniyaning `company_id`si bilan saqlanadi va yaratilgan lead'lar
    ham O'SHA kompaniyaga yoziladi (2026-09, multi-tenant, `ig_dm_sync.py`
    bilan bir xil naqsh). Berilmasa -- eski global (ENV) xatti-harakat
    (standart kompaniyaga yoziladi, CLI/skript uchun orqaga moslik).

    Qaytaradi:
    {"new_leads": N, "forms_checked": N, "errors": [...], "notices": [...], "form_diagnostics": [...]}
    ("errors" -- muammo, qizil ko'rsatiladi; "notices" -- oddiy ma'lumot, masalan
    cursor birinchi marta o'rnatilgani, sariq/neytral ko'rsatiladi.)
    Natija HAR DOIM `kv_store`ga (kompaniyaga tegishli "lead_sync_status" kaliti)
    yoziladi -- muvaffaqiyatli yoki xatolik bilan tugaganidan qat'iy nazar."""
    company_id = company.id if company else db.get_default_company_id()
    page_id = company.meta_page_id if company else meta_api.PAGE_ID
    access_token = company.get_meta_access_token() if company else None
    ad_account_id = getattr(company, "meta_ad_account_id", None) if company else None
    since_key = _since_key(company_id)
    backlog_key = _backlog_key(company_id)

    result = {"new_leads": 0, "forms_checked": 0, "errors": [], "notices": [], "form_diagnostics": []}
    sync_started_at = dt.datetime.utcnow()

    # MUHIM (2026-08 tuzatish): `backlog_key` avvalroq FAQAT pastdagi
    # "since_unix hali yo'q" shoxobchasi ICHIDA o'rnatilar edi -- lekin agar
    # `since_key` ALLAQACHON boshqa (oldingi) deploy'da o'rnatilgan bo'lsa,
    # o'sha shoxobcha UMUMAN ishga tushmaydi va cutoff HECH QACHON yozilmay
    # qoladi (`cleanup_backlog_leads()` doim "cursor o'rnatilmagan" deb xato
    # qaytaveradi). Shuning uchun endi cutoff'ni since_unix holatidan
    # MUSTAQIL, HAR safar tekshirib, agar hali yo'q bo'lsa -- "hozir"ga
    # o'rnatamiz. Bu xavfsiz: chunki cutoff yo'q ekan, demak since-cursor
    # filtri bilan ishlagan hech bir sync haqiqiy yangi lead topmagan
    # bo'lishi kerak (aks holda backlog muammosi allaqachon ko'rinardi) --
    # ya'ni "hozirgacha" bazadagi barcha meta-lead'lar hali ham eski backlog.
    if kv_store.get_json(backlog_key, default=None) is None:
        kv_store.set_json(backlog_key, int(time.time()))

    since_unix = kv_store.get_json(since_key, default=None)
    if since_unix is None:
        # Cursor hali o'rnatilmagan (birinchi marta ishga tushish yoki qo'lda
        # tozalangan holat) -- ESKI TARIXIY lidlarni ommaviy tortib olishning
        # oldini olish uchun bu safar hech qanday lead so'ramaymiz, faqat
        # "shu vaqtdan keyingilarini kuzataman" degan boshlang'ich chegarani
        # qo'yamiz. Keyingi tekshiruvdan boshlab ENDI yaratiladigan lidlar
        # normal tortiladi.
        cursor = int(time.time()) - _SYNC_OVERLAP_SECONDS
        kv_store.set_json(since_key, cursor)
        result["notices"].append(
            "Lead-sync uchun boshlang'ich chegara o'rnatildi -- eski tarixiy "
            "lidlarni tortib olishning oldini olish uchun bu safar hech qanday "
            "lead so'ralmadi. Keyingi tekshiruvdan (odatda 15 daqiqadan keyin) "
            "boshlab FAQAT shu vaqtdan keyin yaratiladigan yangi lidlar tortiladi."
        )
        _save_status(result, company_id=company_id)
        return result

    if not page_id:
        result["errors"].append("META_PAGE_ID sozlanmagan -- lead sync o'tkazib yuborildi.")
        _save_status(result, company_id=company_id)
        return result

    forms = _discover_forms(page_id, access_token, ad_account_id, company, result, company_id=company_id)
    if not forms:
        if not result["errors"]:
            # Bu ENG KO'P uchraydigan "lead kelmayapti" sababi bo'lishi mumkin --
            # META_PAGE_ID noto'g'ri sahifaga ko'rsatayotgan bo'lishi ehtimoli
            # baland (masalan reklama boshqa Page/Instagram orqali yuritilsa).
            result["errors"].append(
                "Bu META_PAGE_ID uchun BIRORTA HAM Instant Form topilmadi. "
                "Agar target ishlayotgan bo'lsa-yu lead kelmasa, ehtimol META_PAGE_ID "
                "noto'g'ri sahifaga ko'rsatib turibdi yoki forma boshqa Page'da."
            )
        _add_permission_hint(result, access_token)
        _save_status(result, company_id=company_id)
        return result

    names = _NameMaps(company, access_token, ad_account_id, result)

    form_since_key = _form_since_key(company_id)
    form_cursors = kv_store.get_json(form_since_key, default=None) or {}
    if not isinstance(form_cursors, dict):
        form_cursors = {}
    new_cursor = int(time.time()) - _SYNC_OVERLAP_SECONDS

    all_new_ids: list = []
    session = get_session()
    try:
        for form in forms:
            form_id = form["id"]
            result["forms_checked"] += 1
            # Formaning o'z cursori (oldin xato bergan bo'lsa -- eski joyida);
            # yangi forma -- umumiy cursordan.
            form_since = form_cursors.get(form_id, since_unix)
            try:
                form_since = min(int(form_since), int(since_unix))
            except (TypeError, ValueError):
                form_since = since_unix
            try:
                leads = meta_api.get_leads(form_id, since=form_since, access_token=access_token,
                                           page_id=form.get("_page_id") or page_id)
            except meta_api.MetaAPIError as e:
                form_cursors[form_id] = form_since  # cursor joyida qoladi -- lidlar yo'qolmaydi
                safe_msg = _lead_error_text(e)
                result["errors"].append(f"Forma '{form.get('name', form_id)}' lidlarini olishda xatolik: {safe_msg}")
                result["form_diagnostics"].append({
                    "form_id": form_id, "form_name": form.get("name"),
                    "meta_leads_count": form.get("leads_count"), "db_leads_count": None,
                    "error": safe_msg,
                })
                continue

            try:
                created, merged = _ingest_raw_leads(session, leads, form_id=form_id, form_name=form.get("name"),
                                                    company_id=company_id, names=names)
                session.commit()
            except IntegrityError:
                # Juda kam holat (parallel yozuv -- masalan real-vaqt webhook
                # bir vaqtda yozdi). Shu forma bo'yicha hech narsa yo'qolmasin:
                # rollback, cursor joyida qoladi, keyingi safar qayta.
                session.rollback()
                logger.warning("Lead sync: IntegrityError (company_id=%s, form=%s) -- keyingi safar qayta", company_id, form_id)
                form_cursors[form_id] = form_since
                result["notices"].append(f"Forma '{form.get('name', form_id)}': parallel yozuv, keyingi tekshiruvda qayta o'qiladi.")
                continue
            form_cursors[form_id] = new_cursor
            result["new_leads"] += len(created)
            result["merged_leads"] = result.get("merged_leads", 0) + len(merged)

            # Yangi lead CRM'ga tushgach -- Meta'ga "Lead" CAPI hodisasi
            # (har biri o'z ichida xato-chidamli).
            _dispatch_capi(session, created + merged)
            all_new_ids.extend(l.id for l in created)

            db_count_for_form = session.query(Lead).filter_by(form_id=form_id).count()
            meta_count = form.get("leads_count")
            diag = {
                "form_id": form_id, "form_name": form.get("name"),
                "meta_leads_count": meta_count, "db_leads_count": db_count_for_form,
                "new_this_run": len(created),
            }
            if isinstance(meta_count, int) and meta_count > 0 and db_count_for_form == 0:
                diag["warning"] = "Meta'da bu formada lidlar bor, lekin bazada BITTASI HAM yo'q -- token/ruxsat yoki sync muammosi bo'lishi mumkin."
            result["form_diagnostics"].append(diag)
    finally:
        session.close()

    kv_store.set_json(since_key, new_cursor)
    live_ids = {f["id"] for f in forms}
    kv_store.set_json(form_since_key, {k: v for k, v in form_cursors.items() if k in live_ids})

    if all_new_ids:
        _notify(company_id, all_new_ids)
    _save_status(result, company_id=company_id)
    return result


# ---------------------------------------------------------------------------
# 2026-10 yordamchilar: barcha formalarni topish, lidni yozish (telefon
# bo'yicha birlashtirish bilan), real-vaqt webhook lidi.
# ---------------------------------------------------------------------------

# Qo'lda/Excel'dan kiritilgan lid shu muddat ichida bo'lsa va telefoni mos
# kelsa -- Meta'dan kelgan xuddi shu lid YANGI qator bo'lib qo'shilmaydi,
# mavjud qatorga Meta ma'lumoti (kampaniya, forma, meta_lead_id) biriktiriladi.
_MERGE_WINDOW_DAYS = 60


def _lead_error_text(e: Exception) -> str:
    err = getattr(e, "error", None) or (e.args[0] if getattr(e, "args", None) else None)
    friendly = meta_api.friendly_meta_error(err, step="lead") if isinstance(err, dict) else None
    return friendly or meta_api.safe_error_message(e)


def _add_permission_hint(result: dict, access_token) -> None:
    """Ruxsat xatosi bo'lsa -- tokenda QAYSI ruxsat yetishmayotganini aniq aytadi."""
    if not access_token or not any("ruxsat" in (x or "") for x in result.get("errors", [])):
        return
    granted = meta_api.get_granted_permissions(access_token)
    if not granted:
        return
    missing = [p for p in meta_api.LEAD_SCOPES if p not in granted]
    if missing:
        result["errors"].append(
            "Tokenda yetishmayotgan ruxsatlar: " + ", ".join(missing)
            + ". \"Hisoblarni ulash\" -> \"Facebook bilan qayta ulash\" bosing va oynadagi barcha belgilarni yoqilgan holda qoldiring."
        )


_AD_FORMS_TTL_SECONDS = 3600


def _ad_account_extra_forms(company_id, page_id, access_token, ad_account_id, known, result) -> list:
    """Reklamalardagi (boshqa sahifadagi) formalar -- 1 soat keshlanadi
    (reklama kreativlarini har 5 daqiqada so'rash ortiqcha og'ir)."""
    import time as _time
    key = f"lead_sync_ad_forms:{company_id}"
    cached = kv_store.get_json(key, default=None)
    if isinstance(cached, dict) and _time.time() - float(cached.get("ts") or 0) < _AD_FORMS_TTL_SECONDS:
        return [f for f in (cached.get("forms") or []) if f.get("id") not in known]
    try:
        ad_form_ids = meta_api.get_ad_account_lead_form_ids(access_token=access_token, ad_account_id=ad_account_id)
    except Exception as e:  # noqa: BLE001
        result["notices"].append(f"Reklamalardagi formalarni aniqlab bo'lmadi: {meta_api.safe_error_message(e)}")
        return []
    extra = []
    for fid in sorted(ad_form_ids - known):
        try:
            info = meta_api.get_lead_form_info(fid, access_token=access_token)
        except Exception:  # noqa: BLE001
            continue
        fpage = ((info.get("page") or {}).get("id")) or page_id
        extra.append({"id": fid, "name": info.get("name"), "leads_count": info.get("leads_count"),
                      "status": info.get("status"), "_page_id": fpage})
    kv_store.set_json(key, {"ts": _time.time(), "forms": extra})
    return extra


def _discover_forms(page_id, access_token, ad_account_id, company, result, *, company_id=None) -> list:
    """Sahifadagi BARCHA formalar + reklama hisobidagi reklamalar ishlatgan,
    lekin BOSHQA sahifada turgan formalar (`_page_id` bilan)."""
    forms: list = []
    try:
        forms = list(meta_api.get_lead_forms(page_id, access_token=access_token))
    except meta_api.MetaAPIError as e:
        result["errors"].append(f"Formalarni olishda xatolik: {_lead_error_text(e)}")
    except Exception as e:  # noqa: BLE001
        result["errors"].append(f"Formalarni olishda xatolik: {meta_api.safe_error_message(e)}")
    known = {f["id"] for f in forms}
    if (company is None or ad_account_id) and access_token:
        forms.extend(_ad_account_extra_forms(company_id, page_id, access_token, ad_account_id, known, result))
    return forms


class _NameMaps:
    """Kampaniya/adset/reklama NOMLARI -- faqat yangi lid bo'lganda (bir marta)
    Meta'dan olinadi (har sync'da og'ir so'rov yubormaslik uchun)."""

    def __init__(self, company, access_token, ad_account_id, result):
        self._args = (company, access_token, ad_account_id)
        self._result = result
        self._loaded = False
        self.campaign, self.adset, self.ad = {}, {}, {}

    def _load(self):
        self._loaded = True
        company, access_token, ad_account_id = self._args
        if company is not None and not ad_account_id:
            return
        try:
            structure = meta_api.get_account_structure(active_only=False, access_token=access_token, ad_account_id=ad_account_id)
        except Exception as e:  # noqa: BLE001
            if self._result is not None:
                self._result["notices"].append(
                    f"Kampaniya nomlarini olib bo'lmadi (lidlar baribir yozildi): {meta_api.safe_error_message(e)}")
            return
        self.campaign = {c["id"]: c.get("name", "") for c in structure.get("campaigns", [])}
        self.adset = {a["id"]: a.get("name", "") for a in structure.get("adsets", [])}
        self.ad = {a["id"]: a.get("name", "") for a in structure.get("ads", [])}

    def get(self, kind: str, obj_id):
        if not obj_id:
            return ""
        if not self._loaded:
            self._load()
        return getattr(self, kind).get(obj_id, "")


class _MergeIndex:
    """Shu kompaniyada yaqinda QO'LDA/IMPORT qilingan, Meta ID'si yo'q lidlar --
    telefonning oxirgi 9 raqami bo'yicha (yozilish formatidan qat'iy nazar:
    "+998 90 111-22-33" ham, "901112233" ham). Bir sync uchun bir marta yuklanadi."""

    def __init__(self, session, company_id):
        self._session, self._company_id = session, company_id
        self._by_key = None

    def _load(self):
        from phone_utils import phone_key9
        self._by_key = {}
        cutoff = dt.datetime.utcnow() - dt.timedelta(days=_MERGE_WINDOW_DAYS)
        rows = (
            self._session.query(Lead)
            .filter(Lead.company_id == self._company_id, Lead.meta_lead_id.is_(None),
                    Lead.source.in_(("manual", "import")), Lead.created_at >= cutoff)
            .order_by(Lead.created_at.asc())
            .all()
        )
        for r in rows:  # eng yangisi oxirida yoziladi -> ustun turadi
            for k in (phone_key9(r.phone), phone_key9(r.phone2)):
                if k and len(k) == 9:
                    self._by_key[k] = r

    def pop(self, phone, phone2):
        from phone_utils import phone_key9
        if self._by_key is None:
            self._load()
        for k in (phone_key9(phone), phone_key9(phone2)):
            if k and len(k) == 9 and k in self._by_key:
                target = self._by_key.pop(k)
                for kk in [x for x, v in self._by_key.items() if v is target]:
                    self._by_key.pop(kk, None)
                return target
        return None


def _ingest_raw_leads(session, raws, *, form_id, form_name, company_id, names) -> tuple:
    """Meta lidlarini bazaga yozadi (commit chaqiruvchida). Qaytaradi:
    (yangi yaratilganlar, mavjud qo'lda/import lidga birlashtirilganlar)."""
    ids = [str(r.get("id")) for r in raws if r.get("id")]
    existing_ids = set()
    # Dublikat tekshiruvi BUTUN baza bo'yicha (tenant filtrisiz): `meta_lead_id`
    # global unique -- boshqa kompaniyada bor lidni yozishga urinish
    # IntegrityError bilan butun sync'ni yiqitardi.
    with db.unscoped():
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            existing_ids.update(x for (x,) in session.query(Lead.meta_lead_id).filter(Lead.meta_lead_id.in_(chunk)))
    created, merged = [], []
    merge_index = _MergeIndex(session, company_id)
    for raw in raws:
        meta_lead_id = raw.get("id")
        if not meta_lead_id or str(meta_lead_id) in existing_ids:
            continue
        existing_ids.add(str(meta_lead_id))
        fd = _field_data_to_dict(raw.get("field_data"))
        name, phone, phone2, email = _extract_name_phone_email(fd)
        campaign_id, adset_id, ad_id = raw.get("campaign_id"), raw.get("adset_id"), raw.get("ad_id")
        created_time = raw.get("created_time")
        try:
            created_dt = dt.datetime.strptime(created_time[:19], "%Y-%m-%dT%H:%M:%S") if created_time else None
        except ValueError:
            created_dt = None
        meta_fields = dict(
            meta_lead_id=str(meta_lead_id),
            campaign_id=campaign_id, campaign_name=names.get("campaign", campaign_id),
            adset_id=adset_id, adset_name=names.get("adset", adset_id),
            ad_id=ad_id, ad_name=names.get("ad", ad_id),
            form_id=raw.get("form_id") or form_id, form_name=form_name,
            raw_field_data=json.dumps(fd, ensure_ascii=False),
            lead_created_time=created_dt,
        )
        target = merge_index.pop(phone, phone2)
        if target is not None:
            # Qo'lda/import kiritilgan o'sha odam -- yangi qator emas, Meta
            # ma'lumoti biriktiriladi (holat, menejer, izohlar saqlanadi).
            for k, v in meta_fields.items():
                if k == "lead_created_time" and target.lead_created_time:
                    continue
                setattr(target, k, v)
            target.full_name = target.full_name or name
            target.email = target.email or email
            merged.append(target)
            continue
        lead = Lead(
            source="meta", status="new", full_name=name, phone=phone, phone2=phone2, email=email,
            company_id=company_id, **meta_fields,
        )
        session.add(lead)
        created.append(lead)
    session.flush()
    return created, merged


def _dispatch_capi(session, leads) -> None:
    if not leads:
        return
    import meta_events
    for lead in leads:
        try:
            meta_events.dispatch_lead_event(session, lead)
        except Exception:
            logger.exception("Lead CAPI hodisasini yuborishda xatolik (lead_id=%s)", lead.id)


def _notify(company_id, lead_ids) -> None:
    try:
        import lead_notify
        lead_notify.notify_new_leads(company_id, lead_ids)
    except Exception:
        logger.exception("Yangi lid bildirishnomasida xato (company_id=%s)", company_id)


def ingest_webhook_lead(page_id: str, leadgen_id: str, form_id: "str | None" = None) -> dict:
    """REAL-VAQT: Meta `leadgen` webhook'i (forma to'ldirilgan zahoti) --
    shu sahifa ulangan kompaniyani topib, lidni ID bo'yicha o'qib, darhol
    CRM'ga yozadi. 15 daqiqalik polling zaxira bo'lib qoladi (webhook
    kelmay qolsa ham lid yo'qolmaydi)."""
    from db import Company
    session = get_session()
    try:
        with db.unscoped():
            c = session.query(Company).filter_by(meta_page_id=str(page_id), is_active=True).first()
            if c is None:
                return {"ok": False, "reason": "sahifa hech bir kompaniyaga ulanmagan"}
            company = _CompanyCreds(id=c.id, meta_page_id=c.meta_page_id,
                                    meta_access_token=c.get_meta_access_token(), meta_ad_account_id=c.meta_ad_account_id)
    finally:
        session.close()
    return _ingest_webhook_for_company(company, leadgen_id, form_id)


@db.company_scoped
def _ingest_webhook_for_company(company, leadgen_id, form_id) -> dict:
    token = company.get_meta_access_token()
    raw = meta_api.get_lead_by_id(leadgen_id, page_id=company.meta_page_id, access_token=token)
    fid = raw.get("form_id") or form_id
    form_name = None
    if fid:
        try:
            form_name = meta_api.get_lead_form_info(fid, access_token=token).get("name")
        except Exception:  # noqa: BLE001
            form_name = None
    names = _NameMaps(company, token, company.meta_ad_account_id, None)
    session = get_session()
    try:
        try:
            created, merged = _ingest_raw_leads(session, [raw], form_id=fid, form_name=form_name,
                                                company_id=company.id, names=names)
            session.commit()
        except IntegrityError:
            session.rollback()  # polling bir vaqtda yozdi -- lid baribir bazada
            return {"ok": True, "duplicate": True}
        _dispatch_capi(session, created + merged)
        new_ids = [l.id for l in created]
    finally:
        session.close()
    if new_ids:
        _notify(company.id, new_ids)
    return {"ok": True, "created": len(new_ids), "merged": len(merged)}


def _save_status(result: dict, *, company_id: "int | None" = None) -> None:
    """Natija + `last_success_at` (oxirgi XATOSIZ sync) + `consecutive_failures`
    (ketma-ket xatoli sync'lar soni -- Telegram ogohlantirishi shunga qaraydi)."""
    try:
        prev = kv_store.get_json(_status_key(company_id), default=None) or {}
        now = dt.datetime.utcnow().isoformat()
        failed = bool(result.get("errors"))
        kv_store.set_json(_status_key(company_id), {
            **result,
            "last_run_at": now,
            "last_success_at": prev.get("last_success_at") if failed else now,
            "consecutive_failures": (int(prev.get("consecutive_failures") or 0) + 1) if failed else 0,
        })
    except Exception:
        logger.exception("lead_sync_status'ni kv_store'ga yozishda xato (o'zi kritik emas)")


def get_last_status(company_id: "int | None" = None) -> "dict | None":
    return kv_store.get_json(_status_key(company_id), default=None)


class _CompanyCreds:
    """`sync_once(company=...)`ga uzatish uchun yengil obyekt (`ig_dm_sync.py`
    bilan bir xil naqsh) -- to'liq `db.Company` ORM qatori shart emas, faqat
    shu maydonlar kerak (session yopilgandan keyin ham ishlatish uchun
    detach qilingan)."""
    def __init__(self, id, meta_page_id, meta_access_token, meta_ad_account_id=None):
        self.id = id
        self.meta_page_id = meta_page_id
        self._meta_access_token_plain = meta_access_token
        self.meta_ad_account_id = meta_ad_account_id

    def get_meta_access_token(self) -> "str | None":
        return self._meta_access_token_plain


def sync_all_companies() -> dict:
    """2026-09, multi-tenant: `meta_page_id`+`meta_access_token` ulagan HAR
    BIR kompaniya bo'yicha aylanib, har birining Meta Lead Ads lidlarini
    ALOHIDA (o'z hisobi bilan) sinxronlaydi va O'Z `company_id`siga yozadi
    (`ig_dm_sync.sync_all_companies()` bilan bir xil naqsh).
    `scheduler.py`ning davriy lead-sync job'i endi shuni chaqiradi -- eski
    yagona-akkaunt `sync_once()` o'rniga. Bitta kompaniyaning
    sinxronizatsiyasi muvaffaqiyatsiz bo'lishi qolganlarini to'xtatmaydi."""
    from db import Company

    session = get_session()
    try:
        rows = (
            session.query(Company)
            .filter(Company.meta_page_id.isnot(None), Company.meta_access_token.isnot(None), Company.is_active.is_(True))
            .all()
        )
        companies = [
            {
                "id": c.id, "name": c.name, "meta_page_id": c.meta_page_id,
                "meta_access_token": c.get_meta_access_token(),
                "meta_ad_account_id": c.meta_ad_account_id,
            }
            for c in rows
        ]
    finally:
        session.close()

    per_company = {}
    for c in companies:
        fake_company = _CompanyCreds(
            id=c["id"], meta_page_id=c["meta_page_id"],
            meta_access_token=c["meta_access_token"], meta_ad_account_id=c["meta_ad_account_id"],
        )
        try:
            per_company[c["id"]] = sync_once(company=fake_company)
        except Exception as e:
            logger.exception("Lead sync: '%s' (id=%s) kompaniyasi uchun xato", c["name"], c["id"])
            # XAVFSIZLIK (2026-09 audit, item 11): `meta_api.safe_error_message()`
            # ishlatiladi -- bu yerga MetaAPIError'dan tashqari HAR QANDAY
            # xato (masalan `requests`ning o'zi ko'targan tarmoq/proxy xatosi,
            # matnida to'liq so'ralgan URL -- shu bilan birga `access_token`
            # ham -- ko'rsatiladigan) tushishi mumkin, va bu xabar
            # `analytics.html`da to'g'ridan-to'g'ri ko'rsatiladi.
            per_company[c["id"]] = {"errors": [f"Kutilmagan xato: {meta_api.safe_error_message(e)}"]}
            _save_status(dict(per_company[c["id"]]), company_id=c["id"])  # ketma-ket xatolar hisobi uchun
    return {"companies_synced": len(companies), "per_company": per_company}


def cleanup_backlog_leads() -> dict:
    """FOYDALANUVCHI ANIQ SO'ROVI bilan (2026-08) BIR MARTALIK ishga
    tushiriladigan tozalash -- (#190) tuzatilgandan keyingi BIRINCHI
    (buzilgan) sync butun tarixiy Meta lead arxivini "yangi" deb bazaga
    yozib yuborgan edi (yuqoridagi "FAQAT YANGI LIDLAR" CURSOR izohiga
    qarang). Bu funksiya O'SHA eski backlog'ni bazadan o'chiradi:

      - FAQAT `source == "meta"` bo'lgan va `created_at` `_BACKLOG_CUTOFF_KEY`
        chegarasidan OLDIN yozilgan lead'lar nomzod hisoblanadi -- tuzatishdan
        KEYIN (haqiqiy since-cursor bilan) kelgan yangi lidlarga HECH QACHON
        tegilmaydi, cleanup necha marta yoki qachon ishga tushirilishidan
        qat'iy nazar.
      - Agar lead'ga HAQIQIY SOTUV (`Sale`) biriktirilgan bo'lsa -- bu pul/KPI
        yozuvi, HECH QACHON avtomatik o'chirilmaydi, shunchaki o'tkazib
        yuboriladi ("kept_has_sale" hisoblanadi).
      - O'chirilayotgan lead'ning izohlari (`LeadNote`) birga o'chadi;
        unga bog'langan qo'ng'iroq yozuvlari (`CallRecord`) esa O'CHIRILMAYDI
        -- faqat `lead_id` NULL qilinadi (qo'ng'iroq tarixi/statistikasi
        saqlanib qoladi).

    Qaytaradi: {"deleted": N, "kept_has_sale": N, "notes_deleted": N,
    "calls_unlinked": N} yoki cursor hali o'rnatilmagan bo'lsa {"error": "..."}."""
    cutoff_unix = kv_store.get_json(_backlog_key(None), default=None)
    if cutoff_unix is None:
        return {
            "error": (
                "Hali birorta ham tuzatilgan lead-sync ishga tushmagan (cursor "
                "o'rnatilmagan) -- avval yangi kodni deploy qiling va kamida bir "
                "marta sync (avtomatik yoki /api/trigger/lead-sync) ishlashini "
                "kuting, keyin bu tozalashni ishga tushiring."
            )
        }
    cutoff_dt = dt.datetime.utcfromtimestamp(cutoff_unix)

    # 2026-09, MUHIM ko'p-kompaniyalilik tuzatishi: bu funksiya #190 bug'i
    # tuzatilgandan KEYINGI eski (bitta kompaniya davridagi) backlog'ni
    # tozalash uchun yozilgan edi -- hech qanday company_id filtri
    # bo'lmagani uchun, endi (5 ta kompaniya bilan) qayta ishga tushirilsa
    # BOSHQA kompaniyalarning HALI HAM haqiqiy/amaldagi Meta lead'larini
    # (agar ular ham shu cutoff'dan oldin yaratilgan bo'lsa) o'chirib
    # yuborishi mumkin edi. Endi FAQAT standart (birinchi, "Asosiy")
    # kompaniyaning lead'lari bilan cheklanadi -- bu funksiya asl
    # mo'ljallangan doirasidan chiqmaydi.
    session = get_session()
    try:
        stats = {"deleted": 0, "kept_has_sale": 0, "notes_deleted": 0, "calls_unlinked": 0}
        with db.scoped_as(db.get_default_company_id()):
            candidates = (
                session.query(Lead)
                .filter(Lead.source == "meta")
                .filter((Lead.created_at < cutoff_dt) | (Lead.created_at.is_(None)))
                .all()
            )
            stats = _delete_backlog_candidates(session, candidates, stats)
        session.commit()
        return stats
    finally:
        session.close()


def _delete_backlog_candidates(session, candidates, stats) -> dict:
    """`cleanup_backlog_leads()` yordamchisi -- chaqiruvchining tenant
    kontekstida (standart kompaniya) ishlaydi."""
    from db import Sale, LeadNote, CallRecord

    for lead in candidates:
        has_sale = session.query(Sale).filter_by(lead_id=lead.id).first() is not None
        if has_sale:
            stats["kept_has_sale"] += 1
            continue
        stats["notes_deleted"] += session.query(LeadNote).filter_by(lead_id=lead.id).delete()
        stats["calls_unlinked"] += (
            session.query(CallRecord).filter_by(lead_id=lead.id).update({"lead_id": None})
        )
        session.delete(lead)
        stats["deleted"] += 1
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(sync_once(), ensure_ascii=False, indent=2))
