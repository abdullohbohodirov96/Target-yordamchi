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
import datetime as dt

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
_SYNC_OVERLAP_SECONDS = 600


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


def _extract_name_phone_email(fd: dict) -> tuple[str | None, str | None, str | None]:
    name = _find_by_keys(fd, _NAME_KEYS)
    if not name:
        first = fd.get("first_name", "")
        last = fd.get("last_name", "")
        name = f"{first} {last}".strip() or None
    phone = _find_by_keys(fd, _PHONE_KEYS)
    email = _find_by_keys(fd, _EMAIL_KEYS)

    # Fallback: agar standart/keng tarqalgan kalitlar orasida topilmasa,
    # QOLGAN barcha maydonlarni skanerlab, telefon/ism'ga O'XSHAGANINI
    # taxmin qilamiz -- bu aynan Excel import'da ishlagan mantiq bilan bir xil
    # (localised savol matnidan generatsiya qilingan g'alati kalitlar uchun).
    if not phone or not name:
        for k, v in fd.items():
            if k in _NEVER_NAME_OR_PHONE_KEYS or not v:
                continue
            if not phone and _looks_phoneish(v):
                phone = v
                continue
            if not name and _looks_nameish(v):
                name = v

    if phone:
        normalized = normalize_phone(phone)
        if normalized:
            phone = normalized
    if isinstance(email, str) and "@" not in email:
        # Ba'zan email deb nomlangan maydonga aslida boshqa narsa tushadi --
        # shubhali bo'lsa, email sifatida saqlamaymiz (bo'sh qoldiramiz).
        email = None
    return name, phone, email


def sync_once(company=None) -> dict:
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
        kv_store.set_json(backlog_key, int(sync_started_at.timestamp()))

    since_unix = kv_store.get_json(since_key, default=None)
    if since_unix is None:
        # Cursor hali o'rnatilmagan (birinchi marta ishga tushish yoki qo'lda
        # tozalangan holat) -- ESKI TARIXIY lidlarni ommaviy tortib olishning
        # oldini olish uchun bu safar hech qanday lead so'ramaymiz, faqat
        # "shu vaqtdan keyingilarini kuzataman" degan boshlang'ich chegarani
        # qo'yamiz. Keyingi tekshiruvdan boshlab ENDI yaratiladigan lidlar
        # normal tortiladi.
        cursor = int(sync_started_at.timestamp()) - _SYNC_OVERLAP_SECONDS
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

    try:
        forms = meta_api.get_lead_forms(page_id, access_token=access_token)
    except meta_api.MetaAPIError as e:
        result["errors"].append(f"Formalarni olishda xatolik: {e}")
        _save_status(result, company_id=company_id)
        return result

    if not forms:
        # Bu ENG KO'P uchraydigan "lead kelmayapti" sababi bo'lishi mumkin --
        # META_PAGE_ID noto'g'ri sahifaga ko'rsatayotgan bo'lishi ehtimoli
        # baland (masalan reklama boshqa Page/Instagram orqali yuritilsa).
        # Bu holda davom etishning hojati yo'q -- tortib olinadigan lead yo'q.
        result["errors"].append(
            "Bu META_PAGE_ID uchun BIRORTA HAM Instant Form topilmadi. "
            "Agar target ishlayotgan bo'lsa-yu lead kelmasa, ehtimol META_PAGE_ID "
            "noto'g'ri sahifaga ko'rsatib turibdi yoki forma boshqa Page'da."
        )
        _save_status(result, company_id=company_id)
        return result

    # Kampaniya/adset/ad ID -> NOM xaritalari (dashboard/CRM'da "qaysi target,
    # qaysi video/reklamadan kelgan" to'liq ko'rinishi uchun -- ad_name ko'pincha
    # ishlatilgan video/kreativ nomiga mos qilib qo'yiladi).
    # 2026-09 multi-tenant: agar `company` berilgan bo'lsa-yu, u hali O'Z
    # reklama hisobini ulamagan bo'lsa (`meta_ad_account_id` yo'q), bu boyitish
    # ATAYLAB o'tkazib yuboriladi -- aks holda global (boshqa kompaniyaning)
    # hisob strukturasi noto'g'ri ishlatilib, nomlar aralashib ketishi mumkin
    # edi. Lead'ning o'zi baribir ID'lar bilan to'liq saqlanadi, faqat NOM
    # ustunlari bo'sh qoladi.
    campaign_name_by_id: dict[str, str] = {}
    adset_name_by_id: dict[str, str] = {}
    ad_name_by_id: dict[str, str] = {}
    if company is None or ad_account_id:
        try:
            structure = meta_api.get_account_structure(active_only=False, access_token=access_token, ad_account_id=ad_account_id)
            for c in structure.get("campaigns", []):
                campaign_name_by_id[c["id"]] = c.get("name", "")
            for a in structure.get("adsets", []):
                adset_name_by_id[a["id"]] = a.get("name", "")
            for a in structure.get("ads", []):
                ad_name_by_id[a["id"]] = a.get("name", "")
        except meta_api.MetaAPIError as e:
            result["errors"].append(f"Kampaniya nomlarini olishda xatolik (davom etamiz): {e}")

    session = get_session()
    try:
        for form in forms:
            form_id = form["id"]
            result["forms_checked"] += 1
            try:
                leads = meta_api.get_leads(form_id, since=since_unix, access_token=access_token, page_id=page_id)
            except meta_api.MetaAPIError as e:
                result["errors"].append(f"Forma '{form.get('name', form_id)}' lidlarini olishda xatolik: {e}")
                result["form_diagnostics"].append({
                    "form_id": form_id, "form_name": form.get("name"),
                    "meta_leads_count": form.get("leads_count"), "db_leads_count": None,
                    "error": str(e),
                })
                continue

            new_for_this_form = 0
            newly_created_leads = []  # 2026-09: commit'dan KEYIN "Lead" CAPI hodisasini yuborish uchun
            for raw in leads:
                meta_lead_id = raw.get("id")
                if not meta_lead_id:
                    continue
                existing = session.query(Lead).filter_by(meta_lead_id=meta_lead_id).first()
                if existing:
                    continue  # allaqachon bazada bor -- dublikat qilinmaydi

                fd = _field_data_to_dict(raw.get("field_data"))
                name, phone, email = _extract_name_phone_email(fd)
                campaign_id = raw.get("campaign_id")
                adset_id = raw.get("adset_id")
                ad_id = raw.get("ad_id")
                created_time = raw.get("created_time")
                try:
                    created_dt = dt.datetime.strptime(created_time[:19], "%Y-%m-%dT%H:%M:%S") if created_time else None
                except ValueError:
                    created_dt = None

                lead = Lead(
                    meta_lead_id=meta_lead_id,
                    campaign_id=campaign_id,
                    campaign_name=campaign_name_by_id.get(campaign_id, ""),
                    adset_id=adset_id,
                    adset_name=adset_name_by_id.get(adset_id, ""),
                    ad_id=ad_id,
                    ad_name=ad_name_by_id.get(ad_id, ""),
                    form_id=form_id,
                    form_name=form.get("name"),
                    source="meta",
                    full_name=name,
                    phone=phone,
                    email=email,
                    raw_field_data=json.dumps(fd, ensure_ascii=False),
                    status="new",
                    lead_created_time=created_dt,
                    # 2026-09 multi-tenant 3-bosqich: endi Meta ulanishi
                    # kompaniya bo'yicha bo'lgani uchun, yangi lead ANIQ
                    # O'SHA kompaniyaga (`sync_once(company=...)`ni chaqirgan
                    # kompaniya) yoziladi -- `company=None` chaqirilsa
                    # (eski, global CLI holati) standart kompaniyaga tushadi.
                    company_id=company_id,
                )
                session.add(lead)
                newly_created_leads.append(lead)
                result["new_leads"] += 1
                new_for_this_form += 1

            session.commit()

            # 2026-09, "production-ready Meta Ads + CAPI integration" so'rovi:
            # yangi lead CRM'ga tushgach, shu HAQIQATNI Meta'ga "Lead" CAPI
            # hodisasi sifatida qaytarib yuboramiz (algoritm o'xshash
            # odamlarni topishni shu signal orqali ham o'rganadi). Har bir
            # yuborish o'z ichida xato-chidamli (try/except) -- bitta
            # muvaffaqiyatsiz hodisa qolgan sinxronizatsiyani to'xtatmasligi
            # kerak.
            if newly_created_leads:
                import meta_events
                for lead in newly_created_leads:
                    try:
                        meta_events.dispatch_lead_event(session, lead)
                    except Exception:
                        logger.exception("Lead CAPI hodisasini yuborishda xatolik (lead_id=%s)", lead.id)

            db_count_for_form = session.query(Lead).filter_by(form_id=form_id).count()
            meta_count = form.get("leads_count")
            diag = {
                "form_id": form_id, "form_name": form.get("name"),
                "meta_leads_count": meta_count, "db_leads_count": db_count_for_form,
                "new_this_run": new_for_this_form,
            }
            # Meta "leads_count" -- shu forma umr bo'yi olgan lead soni; bizning
            # bazamizda esa faqat form_id TO'LDIRILGAN (shu tuzatishdan keyingi)
            # yozuvlar bor -- shuning uchun katta farq FAQAT ikkalasi ham
            # nolga yaqin bo'lmaganda ma'noli signal beradi.
            if isinstance(meta_count, int) and meta_count > 0 and db_count_for_form == 0:
                diag["warning"] = "Meta'da bu formada lidlar bor, lekin bazada BITTASI HAM yo'q -- token/ruxsat yoki sync muammosi bo'lishi mumkin."
            result["form_diagnostics"].append(diag)
    finally:
        session.close()

    # Cursor'ni oldinga suramiz -- keyingi tekshiruv endi shu safargi
    # boshlanish vaqtidan (kichik overlap bilan) keyingi lidlarnigina so'raydi.
    # Muvaffaqiyatsiz bo'lgan alohida formalar (yuqorida "continue" bo'lgan)
    # keyingi safar YANA shu (yangi) cursor bilan tekshiriladi -- agar ular
    # orasida chegaraga to'g'ri kelib qolgan lead bo'lsa, bu holatda
    # qo'lda `/api/trigger/lead-sync`ni qayta ishga tushirish kifoya.
    new_cursor = int(sync_started_at.timestamp()) - _SYNC_OVERLAP_SECONDS
    kv_store.set_json(since_key, new_cursor)

    _save_status(result, company_id=company_id)
    return result


def _save_status(result: dict, *, company_id: "int | None" = None) -> None:
    try:
        kv_store.set_json(_status_key(company_id), {
            **result,
            "last_run_at": dt.datetime.utcnow().isoformat(),
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
            per_company[c["id"]] = {"errors": [f"Kutilmagan xato: {e}"]}
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
    from db import Sale, LeadNote, CallRecord

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

    session = get_session()
    try:
        stats = {"deleted": 0, "kept_has_sale": 0, "notes_deleted": 0, "calls_unlinked": 0}
        candidates = (
            session.query(Lead)
            .filter(Lead.source == "meta")
            .filter((Lead.created_at < cutoff_dt) | (Lead.created_at.is_(None)))
            .all()
        )
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
        session.commit()
        return stats
    finally:
        session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(sync_once(), ensure_ascii=False, indent=2))
