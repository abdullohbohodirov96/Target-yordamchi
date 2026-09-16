"""
meta_api.py — Meta Marketing API (Facebook Graph API) bilan ishlash uchun
yengil wrapper. Tashqi og'ir SDK talab qilinmaydi, faqat `requests`.

KERAKLI RUXSATLAR (Meta tomonida):
- System User Access Token (Business Manager -> System Users), quyidagi
  permission'lar bilan: ads_management, ads_read, leads_retrieval (agar
  lead ma'lumotlarini olish kerak bo'lsa), pages_read_engagement (agar
  Instant Form yaratish/Page bilan ishlash kerak bo'lsa).
- Token shu Business Manager ostidagi O'ZINGIZNING reklama kabinetingiz va
  sahifangiz uchun to'liq ishlaydi — bu holatda Meta App Review shart emas.
  Agar boshqa birovning Page/Ad Account'iga ulanish kerak bo'lsa, Meta
  tomonidan qo'shimcha tekshiruv (App Review) talab qilinishi mumkin.
- Token muddati: uzoq muddatli System User token amalda muddatsiz ishlaydi
  (agar qo'lda bekor qilinmasa).

ESLATMA: Bu MVP kodi. Ishlab chiqarishga (production) chiqarishdan oldin:
  - ~~Xatoliklarni qayta urinish (retry/backoff) mexanizmini kuchaytiring.~~
    TUZATILDI (2026-09) -- `_get`/`_post` endi tarmoq xatosi/Meta'ning
    vaqtinchalik xatolarida avtomatik qayta uriladi (pastga, `_get`/`_post`
    ta'rifidan oldingi izohga qarang -- YOZUV uchun ATAYLAB ehtiyotkorroq).
  - Rate limit (Meta har soatlik so'rov limiti bor) monitoringini qo'shing.
  - Har bir yozish amalini (pause/budget) alohida audit-log'ga yozing.
"""

import os
import re
import json
import time
import hmac
import random
import logging
import hashlib
import calendar
import datetime as dt
import concurrent.futures
import requests

logger = logging.getLogger("meta_api")

GRAPH_API_VERSION = "v21.0"
GRAPH_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN", "")
AD_ACCOUNT_ID = os.environ.get("META_AD_ACCOUNT_ID", "")  # format: act_1234567890
PAGE_ID = os.environ.get("META_PAGE_ID", "")  # Facebook Page ID (ad creative uchun)
PIXEL_ID = os.environ.get("META_PIXEL_ID", "")  # Conversions API (CAPI) uchun -- ixtiyoriy

# 2026-09, foydalanuvchi so'rovi: "boshqa kompaniyalar bitta tugma bilan
# o'z Facebook/Instagram hisobini ulasin" -- "Facebook Login for Business"
# OAuth ilovasi uchun (developers.facebook.com'da yaratiladi). Bular BUTUN
# tizim uchun BITTA ilova (App ID/Secret) -- har bir KOMPANIYA esa OAuth
# orqali O'Z token/sahifa/reklama hisobini oladi (pastga, "OAuth" bo'limiga
# qarang). Sozlanmagan bo'lsa (bo'sh), "Connect with Facebook" tugmasi
# `app.py`da ko'rsatilmaydi -- qo'lda token kiritish (eski usul) ishlayveradi.
META_APP_ID = os.environ.get("META_APP_ID", "")
META_APP_SECRET = os.environ.get("META_APP_SECRET", "")

# 2026-09, foydalanuvchi so'rovi (item 9 -- webhook arxitekturasi): Meta App
# Dashboard'da "Webhooks" bo'limida Callback URL bilan birga QO'LDA
# kiritiladigan ixtiyoriy matn (Meta'ning O'ZI TANLAMAYDI -- BIZ o'ylab
# topamiz va ikkala tomonga -- shu yerga ENV sifatida, va Meta Dashboard'ga
# -- bir xil qiymatni kiritamiz). Verifikatsiya handshake'ida
# (`GET /webhooks/instagram?hub.verify_token=...`) solishtirish uchun
# ishlatiladi -- `app.py`dagi route'ga qarang.
META_WEBHOOK_VERIFY_TOKEN = os.environ.get("META_WEBHOOK_VERIFY_TOKEN", "")


class MetaAPIError(Exception):
    pass


def safe_error_message(e: Exception) -> str:
    """XAVFSIZLIK TUZATISHI (2026-09, foydalanuvchi so'rovi: "webni to'liq
    tekshirib chiq, xato forntlarini" -- to'liq audit paytida topilgan
    JIDDIY muammo): `app.py`da bir nechta joyda (masalan `target_page()`)
    `except Exception as e: ... {"error": str(e)}` qilingan, va bu matn
    to'g'ridan-to'g'ri HTML'ga (`{{ data.error }}`) chiqarilgan edi.

    Muammo: agar `e` Meta serveriga ULANISHNING O'ZI (proxy/tarmoq xatosi,
    `requests.exceptions.ProxyError`/`ConnectionError`/`Timeout`) bo'lsa,
    Python'ning `requests` kutubxonasi bunday xatoning matnida SO'ROV
    QILINGAN TO'LIQ URL'ni ko'rsatadi -- bu URL esa `access_token=...`
    parametrini OCHIQ HOLDA o'z ichiga oladi! Ya'ni tarmoq bir zumga
    uzilib qolsa, foydalanuvchining ekraniga (Target sahifasidagi qizil
    xato banneriga) HAQIQIY Meta access token'i chiqib qolar edi --
    skrinshot orqali osongina sizib chiqishi mumkin bo'lgan xavfsizlik
    kamchiligi.

    Bu funksiya shu muammoni tuzatadi: FAQAT Meta'ning o'zi qaytargan,
    toza JSON xato xabarini (`MetaAPIError(data["error"])`, tarkibida URL/
    token bo'lmaydi) foydalanuvchiga ko'rsatishga ruxsat beradi; boshqa
    HAR QANDAY (tarmoq/proxy/timeout va h.k.) xato uchun umumiy, xavfsiz
    o'zbekcha xabar qaytaradi. To'liq texnik tafsilot baribir
    `logger.exception(...)` orqali serverga (foydalanuvchiga ko'rinmaydigan
    joyga) yoziladi -- diagnostika uchun yo'qolmaydi, faqat ekranga
    chiqmaydi."""
    if isinstance(e, MetaAPIError) and e.args and isinstance(e.args[0], dict):
        msg = e.args[0].get("message")
        if msg:
            return str(msg)
    return "Meta bilan bog'lanishda vaqtinchalik xatolik yuz berdi (tarmoq muammosi bo'lishi mumkin). Birozdan keyin sahifani yangilab ko'ring."


# 2026-09, Item J xavfsizlik auditi (🟠 YUQORI, 8-band): "Meta API va Claude
# chaqiruvlarida retry/backoff yo'q" -- Claude tomoni ANIQLANDI: `anthropic`
# Python SDK'si (`orchestrator.py`dagi `anthropic.Anthropic(...)` mijozi)
# ULANISH xatosi/429/5xx uchun O'ZI, standart bo'yicha (`max_retries=2`,
# eksponensial kechikish bilan) qayta urinadi -- bu yerda qo'shimcha kod
# kerak emas edi. Meta (Graph API) tomoni esa HAQIQATAN HAM qayta
# urinishsiz edi -- pastdagi `_MAX_ATTEMPTS`/`_retry_sleep` shuni tuzatadi.
#
# MUHIM ASIMMETRIYA (pul bilan bog'liq xavfsizlik uchun ATAYLAB): `_get`
# (O'QISH, ta'sirsiz) tarmoq xatosida VA Meta'ning "vaqtinchalik" deb
# belgilagan xatosida (`is_transient`/reyting-cheklov kodlari) ham qayta
# uriniladi. `_post` (YOZUV -- byudjet, pauza va h.k.) esa FAQAT sof
# TARMOQ xatosida (ulanish/timeout -- ya'ni so'rov Meta serveriga
# YETIB BORMAGAN bo'lishi ehtimoli katta) qayta uriniladi; agar Meta
# JAVOB QAYTARGAN bo'lsa (hatto "vaqtinchalik" xato bilan ham) -- ENDI
# QAYTA URINILMAYDI, chunki yozuv allaqachon qisman bajarilgan bo'lishi
# mumkin va qayta yuborish uni IKKILANTIRIB YUBORISHI mumkin (masalan
# byudjet ikki marta o'zgarishi). Bu qatlamning ustidagi chaqiruvchilar
# (`orchestrator.py`) allaqachon har bir yozuvdan keyin QAYTA O'QIB
# TEKSHIRADI (`_execute_and_verify_status`) -- shu combo (tarmoqda
# ehtiyotkor qayta urinish + natijani tekshirish) xavfsiz.
_MAX_ATTEMPTS = 3
_RETRY_BASE_DELAY = 1.0  # soniya -- 1-qayta urinish ~1s, 2-qayta urinish ~2s kutadi
_RETRYABLE_NETWORK_ERRORS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
)
_TRANSIENT_META_ERROR_CODES = {4, 17, 32, 613}  # Meta hujjati: rate-limit turlari


def _retry_sleep(attempt: int) -> None:
    time.sleep(_RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.4))


def _is_transient_meta_error(data: dict) -> bool:
    err = data.get("error") if isinstance(data, dict) else None
    if not isinstance(err, dict):
        return False
    if err.get("is_transient"):
        return True
    return err.get("code") in _TRANSIENT_META_ERROR_CODES


def _get(path: str, params: dict | None = None, token: str | None = None) -> dict:
    params = {
        k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
        for k, v in (params or {}).items()
    }
    params["access_token"] = token or ACCESS_TOKEN
    url = f"{GRAPH_URL}/{path}"
    for attempt in range(_MAX_ATTEMPTS):
        try:
            r = requests.get(url, params=params, timeout=30)
            data = r.json()
        except _RETRYABLE_NETWORK_ERRORS:
            if attempt < _MAX_ATTEMPTS - 1:
                _retry_sleep(attempt)
                continue
            raise
        if "error" in data:
            if _is_transient_meta_error(data) and attempt < _MAX_ATTEMPTS - 1:
                _retry_sleep(attempt)
                continue
            raise MetaAPIError(data["error"])
        return data
    raise MetaAPIError({"message": "Meta bilan bog'lanib bo'lmadi (qayta urinishlar tugadi)."})


def _post(path: str, data: dict, token: str | None = None) -> dict:
    # Graph API forma-encoded POST so'rovlarida object/array parametrlar
    # (targeting, creative, rename_options va h.k.) JSON-string ko'rinishida
    # yuborilishi kerak — shuning uchun dict/list qiymatlarni avtomatik
    # json.dumps() qilamiz.
    payload = {
        k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
        for k, v in data.items()
    }
    payload["access_token"] = token or ACCESS_TOKEN
    url = f"{GRAPH_URL}/{path}"
    for attempt in range(_MAX_ATTEMPTS):
        try:
            r = requests.post(url, data=payload, timeout=30)
            result = r.json()
        except _RETRYABLE_NETWORK_ERRORS:
            # Faqat SOF tarmoq xatosida qayta urinamiz (izohga qarang, yuqorida)
            # -- Meta javob qaytargan har qanday holatda (hatto xato bilan ham)
            # darhol to'xtaymiz, IKKILANTIRIB YUBORISH xavfini olmaslik uchun.
            if attempt < _MAX_ATTEMPTS - 1:
                _retry_sleep(attempt)
                continue
            raise
        if isinstance(result, dict) and "error" in result:
            raise MetaAPIError(result["error"])
        return result
    raise MetaAPIError({"message": "Meta bilan bog'lanib bo'lmadi (qayta urinishlar tugadi)."})


def _post_multipart(path: str, data: dict, files: dict, token: str | None = None) -> dict:
    """2026-09, Meta Ads Autopilot: fayl (rasm/video) yuklash uchun
    `multipart/form-data` POST -- `adimages`/`advideos` endpoint'lari.
    Xato ishlovi `_post()` bilan bir xil (yozuv -- faqat SOF tarmoq xatosida
    qayta uriniladi; Meta javob qaytargan bo'lsa darhol to'xtaydi, aks holda
    bitta rasm ikki marta yuklanib ketishi mumkin). `files` -- `requests`
    formatida: {"filename": (name, bytes, content_type)}."""
    payload = {
        k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
        for k, v in (data or {}).items()
    }
    payload["access_token"] = token or ACCESS_TOKEN
    url = f"{GRAPH_URL}/{path}"
    for attempt in range(_MAX_ATTEMPTS):
        try:
            r = requests.post(url, data=payload, files=files, timeout=120)
            result = r.json()
        except _RETRYABLE_NETWORK_ERRORS:
            if attempt < _MAX_ATTEMPTS - 1:
                _retry_sleep(attempt)
                continue
            raise
        if isinstance(result, dict) and "error" in result:
            raise MetaAPIError(result["error"])
        return result
    raise MetaAPIError({"message": "Meta bilan bog'lanib bo'lmadi (qayta urinishlar tugadi)."})


def _get_all_pages(path: str, params: dict | None = None, token: str | None = None) -> list[dict]:
    """2026-09, Item J xavfsizlik auditi (🟠 YUQORI, 9-band: "200 tadan
    ortiq obyektli hisoblar uchun pagination yo'q"). `_get()` bilan bir xil,
    lekin Meta'ning `paging.next` havolasini OXIRIGACHA ergashadi -- bitta
    so'rov limitidan (odatda 100-200) ko'p obyekt bo'lgan hisoblarda natija
    JIM RAVISHDA KESILIB QOLMASLIGI uchun. Ayniqsa `get_account_structure()`
    uchun MUHIM: shu ro'yxat asosida `object_id` tasdiqlanadi (ijro
    xavfsizligi, 2026-09 avvalroq tuzatilgan) -- kesilgan ro'yxat haqiqiy
    obyektni "topilmadi" deb ko'rsatib, amalni asossiz bloklashi mumkin edi.

    Har bir keyingi sahifa ham tarmoq xatosida qayta uriladi -- bu O'QISH,
    ta'sirsiz, shuning uchun retry to'liq xavfsiz (yozuv uchun ehtiyotkor
    asimmetriyaga bu yerda ehtiyoj yo'q)."""
    data = _get(path, params, token=token)
    items = list(data.get("data", []))
    next_url = data.get("paging", {}).get("next")
    while next_url:
        page = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                r = requests.get(next_url, timeout=30)
                page = r.json()
                break
            except _RETRYABLE_NETWORK_ERRORS:
                if attempt < _MAX_ATTEMPTS - 1:
                    _retry_sleep(attempt)
                    continue
                raise
        if isinstance(page, dict) and "error" in page:
            raise MetaAPIError(page["error"])
        items.extend((page or {}).get("data", []))
        next_url = (page or {}).get("paging", {}).get("next")
    return items


# ---------------------------------------------------------------------------
# Conversions API (CAPI) -- CRM'dagi lead-sifat/sotuv signalini Meta'ga qayta
# yuborish (2026-08, NotebookLM orqali o'rganilgan "Vena AI" konsepsiyasi
# asosida qo'shildi -- bilim bazasi 4.6/4.10-bo'limlarida ilgaridan tavsiya
# qilingan edi, lekin hech qachon amalga oshirilmagan edi). G'oya: sotuvchi
# CRM'da lidni "sifatli" yoki "sotib oldi" deb belgilaganda, shu hodisa
# darhol Meta'ga signal sifatida yuboriladi -- algoritm shunga o'xshagan
# odamlarni auksionda qidirishni o'rganadi (ayniqsa "Maximize number of
# qualified leads" maqsadi bilan birga ishlaganda samarali).
#
# Sozlash: Render environment variable'larga META_PIXEL_ID qo'shing (Meta
# Events Manager -> Data Sources -> Pixel). Bu sozlanmagan bo'lsa,
# send_conversion_event() jim ravishda hech narsa qilmaydi (xato tashlamaydi)
# -- CRM'ning asosiy oqimi (lead saqlash, sotuv qo'shish) CAPI ulanmagan
# taqdirda ham hech qachon buzilmasligi kerak.
# ---------------------------------------------------------------------------

def is_capi_configured(*, pixel_id: str | None = None, access_token: str | None = None) -> bool:
    """2026-09, multi-tenant: `pixel_id`/`access_token` berilsa -- O'SHA
    kompaniyaning o'z Pixel'i/tokeni tekshiriladi. Ikkalasi ham berilmasa --
    eski global ENV (`PIXEL_ID`/`ACCESS_TOKEN`) tekshiriladi (orqaga
    moslik -- CLI yoki hali company-parametrsiz chaqiruvlar uchun)."""
    return bool((access_token or ACCESS_TOKEN) and (pixel_id or PIXEL_ID))


def _hash_sha256(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


def send_conversion_event(
    event_name: str,
    *,
    phone: str | None = None,
    email: str | None = None,
    lead_id: str | None = None,
    event_id: str | None = None,
    value: float | None = None,
    currency: str = "UZS",
    pixel_id: str | None = None,
    access_token: str | None = None,
    test_event_code: str | None = None,
    action_source: str = "system_generated",
    fbp: str | None = None,
    fbc: str | None = None,
    client_ip_address: str | None = None,
    client_user_agent: str | None = None,
    external_id: str | None = None,
    event_source_url: str | None = None,
) -> dict | None:
    """Bitta hodisani (masalan "QualifiedLead" yoki "Purchase") Meta
    Conversions API'ga yuboradi.

    `pixel_id`/`access_token` -- 2026-09 multi-tenant: berilsa, O'SHA
    kompaniyaning o'z Pixel'i/tokeni ishlatiladi. Ikkalasi ham berilmasa --
    eski global ENV (`PIXEL_ID`/`ACCESS_TOKEN`) ishlatiladi.

    `test_event_code` -- Meta Events Manager -> "Test events" bo'limida
    ko'rsatiladigan vaqtinchalik kod. Berilsa, hodisa Meta'ning HAQIQIY
    statistikasiga (real reklama optimizatsiyasiga) ta'sir qilmaydi, lekin
    Events Manager'da darhol "Server" manbai bilan ko'rinadi -- aynan
    "Test connection" tugmasi shuni ishlatadi (Meta CAPI'ning rasmiy test
    mexanizmi -- `data` bilan bir qatorda POST tanasiga qo'shiladigan
    alohida maydon, docs/META_INTEGRATION_SETUP.md'da havola berilgan).

    - `phone`/`email` -- mijozning CRM'dagi kontakti (SHA-256 bilan xeshlanadi,
      xom holda hech qachon Meta'ga yuborilmaydi -- bu Meta'ning o'zi talab
      qiladigan standart usul).
    - `lead_id` -- agar mavjud bo'lsa, Meta'ning o'z Instant Form leadgen ID'si
      (`Lead.meta_lead_id`) -- eng aniq moslashtirish usuli, chunki bu lead
      allaqachon Meta tomonida bor va reklama bilan bevosita bog'langan.
    - `event_id` -- dublikatni oldini olish uchun barqaror kalit (masalan
      f"lead-{lead.id}-qualified") -- bir xil hodisa qayta yuborilib qolsa
      ham Meta ikkalanini bittaga hisoblaydi.
    - `value`/`currency` -- pul summasi bilan bog'liq hodisalar uchun
      (masalan sotuv summasi).
    - `action_source` -- Meta'ning rasmiy hujjatidagi qiymatlardan biri
      ("website", "email", "phone_call", "system_generated" va h.k.).
      CRM-ichida (qo'ng'iroq/status o'zgarishi) yaratilgan hodisalar uchun
      standart qiymat "system_generated" -- Meta'ning o'z hujjatida server
      tomonidan, brauzer/qo'ng'iroqsiz yuborilgan hodisalar uchun aynan shu
      qiymat tavsiya etiladi. Sayt formasidan kelgan lead uchun chaqiruvchi
      "website" uzatishi mumkin (pastga qarang -- `fbp`/`fbc` bilan birga).
    - `fbp`/`fbc` -- brauzer Pixel cookie'lari (`_fbp`/`_fbc`), sayt orqali
      kelgan lead'larda Pixel+CAPI DEDUPLIKATSIYA uchun (xeshlanmaydi --
      Meta hujjatiga ko'ra bular ochiq holda yuboriladi).
    - `client_ip_address`/`client_user_agent` -- moslashtirish sifatini
      oshiradi (xeshlanmaydi).
    - `external_id` -- CRM'dagi ichki lead ID (xeshlanadi -- Meta'ning
      moslashtirish kaliti sifatida hujjatlashtirilgan).
    - `event_source_url` -- sayt orqali kelgan lead uchun landing sahifa
      manzili (`Lead.landing_url`).

    META_PIXEL_ID sozlanmagan yoki moslashtiradigan hech qanday kontakt
    berilmagan bo'lsa -- `None` qaytaradi, xato tashlamaydi.
    """
    resolved_pixel_id = pixel_id or PIXEL_ID
    resolved_token = access_token or ACCESS_TOKEN
    if not is_capi_configured(pixel_id=resolved_pixel_id, access_token=resolved_token):
        return None

    user_data: dict = {}
    if phone:
        digits = re.sub(r"\D", "", phone)
        if digits:
            user_data["ph"] = [_hash_sha256(digits)]
    if email and "@" in email:
        user_data["em"] = [_hash_sha256(email)]
    if lead_id:
        user_data["lead_id"] = str(lead_id)
    if external_id:
        user_data["external_id"] = [_hash_sha256(str(external_id))]
    if fbp:
        user_data["fbp"] = fbp
    if fbc:
        user_data["fbc"] = fbc
    if client_ip_address:
        user_data["client_ip_address"] = client_ip_address
    if client_user_agent:
        user_data["client_user_agent"] = client_user_agent

    if not user_data:
        return None  # moslashtiradigan hech narsa yo'q -- yuborishning ma'nosi yo'q

    event = {
        "event_name": event_name,
        "event_time": int(time.time()),
        "action_source": action_source,
        "user_data": user_data,
    }
    if event_source_url:
        event["event_source_url"] = event_source_url
    if event_id:
        event["event_id"] = event_id
    if value is not None:
        event["custom_data"] = {"value": round(float(value), 2), "currency": currency}

    payload: dict = {"data": [event]}
    if test_event_code:
        payload["test_event_code"] = test_event_code

    return _post(f"{resolved_pixel_id}/events", payload, token=resolved_token)


# ---------------------------------------------------------------------------
# Page Access Token -- Instant Form (Lead Ads) bilan bog'liq endpointlar
# (leadgen_forms yaratish/o'qish, forma leadlarini o'qish) Facebook
# tomonidan MAJBURIY ravishda alohida "Page Access Token" talab qiladi --
# oddiy System User/foydalanuvchi token bilan chaqirilsa "(#190) This
# method must be called with a Page Access Token" xatosi qaytadi (aynan
# shu xato "Lead-sync holati" kartochkasida ko'ringan).
#
# YECHIM: Render'da YANGI environment variable/token QO'SHISH SHART EMAS --
# allaqachon sozlangan META_ACCESS_TOKEN shu Page'ga administrator/
# muharrir sifatida ulangan bo'lsa (Business Manager -> Sahifalar), Page
# Access Token'ni O'ZI so'rab, keshlab, keyingi barcha Page-darajasidagi
# chaqiruvlarda ishlatadi (`GET /{page-id}?fields=access_token`).
# ---------------------------------------------------------------------------

# 2026-09, multi-tenant: HAR BIR kompaniyaning O'Z Page'i uchun alohida
# Page Access Token kerak -- keshni endi `page_id` bo'yicha (avval "har
# doim bitta" deb faraz qilingan yagona "token" kaliti emas) saqlaymiz.
#
# 2026-09, Item J xavfsizlik auditi (🟠 YUQORI, 14-band: "Page-token kesh
# hech qachon tozalanmaydi"): `invalidate_page_token_cache()` FAQAT o'zimiz
# bilgan bitta holatni (foydalanuvchi "qayta ulash" tugmasini bosgan payt)
# yopadi -- agar Page ruxsati Meta tomonida BOSHQA sabab bilan (masalan
# administrator Business Manager'dan olib tashlagan, yoki token muddati
# tugagan) o'zgarsa, eski token PROCESS QAYTA ISHGA TUSHMAGUNCHA cheksiz
# keshda qolardi. Endi har bir yozuv `(token, fetched_at)` bilan birga
# saqlanadi va `_PAGE_TOKEN_TTL_SECONDS`dan eskirgan bo'lsa avtomatik qayta
# so'raladi -- bu Render'da OYLAB uzluksiz ishlaydigan process uchun MUHIM
# (foydalanuvchi hech qachon qayta ulanmasa ham, kesh o'zi vaqti-vaqti bilan
# yangilanadi).
_page_token_cache: "dict[str, tuple[str, float]]" = {}
_PAGE_TOKEN_TTL_SECONDS = 12 * 60 * 60  # 12 soat


def _get_page_access_token(page_id: str | None = None, user_access_token: str | None = None) -> str:
    """`page_id`/`user_access_token` berilsa -- O'SHA (kompaniyaning o'zi
    ulagan) Page/token uchun Page Access Token oladi. Ikkalasi ham
    berilmasa -- eski global (ENV) `PAGE_ID`/`ACCESS_TOKEN` ishlatiladi
    (orqaga moslik: CLI skript yoki hali company-parametrsiz chaqiruvlar)."""
    resolved_page_id = page_id or PAGE_ID
    resolved_user_token = user_access_token or ACCESS_TOKEN
    cached = _page_token_cache.get(resolved_page_id)
    if cached is not None:
        token, fetched_at = cached
        if time.monotonic() - fetched_at < _PAGE_TOKEN_TTL_SECONDS:
            return token
        del _page_token_cache[resolved_page_id]  # TTL tugagan -- qayta so'raladi
    if not resolved_page_id:
        raise MetaAPIError({"message": "Page ID sozlanmagan -- Page Access Token olib bo'lmaydi."})
    # 2026-09: endi umumiy `_get()` orqali -- shu bilan tarmoq xatosi/Meta'ning
    # vaqtinchalik xatolarida avtomatik qayta urinish ham qo'llanadi (O'QISH,
    # ta'sirsiz -- retry uchun xavfsiz).
    data = _get(resolved_page_id, params={"fields": "access_token"}, token=resolved_user_token)
    token = data.get("access_token")
    if not token:
        raise MetaAPIError({
            "message": (
                "Page Access Token olinmadi -- ulangan token shu Page'ga "
                "(Business Manager -> Sahifalar) administrator/muharrir sifatida "
                "ulanganini tekshiring."
            )
        })
    _page_token_cache[resolved_page_id] = (token, time.monotonic())
    return token


def invalidate_page_token_cache(page_id: "str | None") -> None:
    """2026-09, JONLI XATO TOPILDI: kompaniya "Facebook bilan qayta ulash"
    orqali YANGI (kengroq scope'li, masalan `instagram_manage_messages`
    qo'shilgan) foydalanuvchi tokeniga o'tsa ham, `_get_page_access_token()`
    yuqoridagi `_page_token_cache`da ESKI (torroq ruxsat bilan olingan)
    Page Access Tokenni CHEKSIZ (process qayta ishga tushmaguncha) saqlab
    qolardi -- shuning uchun Instagram DM ("#230 Requires
    instagram_manage_messages") xatosi qayta ulanishdan KEYIN ham davom
    etardi (foydalanuvchi buni bir necha marta qayta ulanib ham hal qila
    olmadi). Bu funksiya `_save_facebook_connection()`dan (app.py) HAR
    safar chaqiriladi -- shu Page uchun eski keshni olib tashlaydi, keyingi
    chaqiruv YANGI foydalanuvchi tokenidan yangi Page Access Token oladi."""
    if page_id and page_id in _page_token_cache:
        del _page_token_cache[page_id]


# ---------------------------------------------------------------------------
# INSIGHTS (tahlil uchun ma'lumot olish)
# ---------------------------------------------------------------------------

DEFAULT_FIELDS = [
    "campaign_name", "adset_name", "ad_name",
    "spend", "cpm", "ctr", "cpc",
    "actions", "action_values", "cost_per_action_type",
    "reach", "frequency", "impressions",
]

# Video/kreativ engagement metrikalari — "video ko'rganlar soni", "necha foizi
# birinchi 15 soniyani ko'rdi" kabi savollarga javob berish uchun (4.12-bo'lim:
# Hook rate / Hold rate tashxisi shu metrikalarga asoslanadi).
VIDEO_FIELDS = [
    "video_play_actions",              # umumiy video play soni
    "video_avg_time_watched_actions",   # o'rtacha ko'rish davomiyligi (soniya)
    "video_p25_watched_actions",        # 25% ko'rganlar (taxminan Hook natijasi)
    "video_p50_watched_actions",
    "video_p75_watched_actions",
    "video_p95_watched_actions",
    "video_p100_watched_actions",       # oxirigacha ko'rganlar
    "video_thruplay_watched_actions",   # 15 soniya (yoki oxirigacha, qisqaroq bo'lsa) ko'rganlar — "Hold rate" uchun asosiy metrika
    "video_30_sec_watched_actions",
]

FULL_REPORTING_FIELDS = DEFAULT_FIELDS + VIDEO_FIELDS


def get_insights(
    level: str = "ad",              # "campaign" | "adset" | "ad"
    date_preset: str = "last_7d",
    breakdowns: list[str] | None = None,   # masalan ["region"]
    fields: list[str] | None = None,
    time_range: dict | None = None,   # {"since": "YYYY-MM-DD", "until": "YYYY-MM-DD"}
    time_increment: int | str | None = None,   # 1 = har kun uchun alohida qator
    *,
    access_token: str | None = None,
    ad_account_id: str | None = None,
) -> list[dict]:
    """Kampaniya/adset/ad darajasidagi statistikani qaytaradi.

    `breakdowns=["region"]` bersangiz — lidlar/xarajat qaysi hududdan
    kelayotganini ko'rish mumkin (4.11-bo'lim: hudud muammosini aniqlash uchun).

    `time_range` berilsa (masalan foydalanuvchi aniq bir kun yoki oraliq
    so'raganda -- "20 iyul", "1-10 avgust"), u `date_preset`dan USTUN turadi
    va aynan o'sha sanalar oralig'idagi ma'lumot qaytariladi.

    `time_increment=1` bersangiz, natija BIR QATOR o'rniga HAR KUN uchun
    alohida qator (`date_start`/`date_stop` maydonlari bilan) qaytaradi --
    oylik hisobotdagi "kunlik jadval" uchun ishlatiladi (monthly_report.py).

    `access_token`/`ad_account_id` -- 2026-09, multi-tenant to'g'rilash
    (foydalanuvchi shikoyati: "targeting ma'lumotlari boshqa loyihadan
    chiqib qolyapti"): BERILSA, shu ANIQ kompaniyaning O'Z Meta hisobidan
    so'raladi; BERILMASA (None), eski xatti-harakat -- global ENV
    o'zgaruvchilar (`ACCESS_TOKEN`/`AD_ACCOUNT_ID`, sizning o'z biznesingiz
    -- Company #1) ishlatiladi. Chaqiruvchi (`dashboard_data.py`) HECH
    QACHON "ulanmagan" kompaniya uchun bu ikkalasini bo'sh qoldirib
    chaqirmasligi kerak -- aks holda global (boshqa kompaniyaning) hisob
    ma'lumoti qaytib, xuddi shu leak yana takrorlanadi."""
    params = {
        "level": level,
        "fields": ",".join(fields or DEFAULT_FIELDS),
        "limit": 200,
    }
    if time_range:
        params["time_range"] = time_range  # _get avtomatik JSON'ga o'giradi
    else:
        params["date_preset"] = date_preset
    if breakdowns:
        params["breakdowns"] = ",".join(breakdowns)
    if time_increment:
        params["time_increment"] = time_increment
    data = _get(f"{ad_account_id or AD_ACCOUNT_ID}/insights", params, token=access_token)
    return data.get("data", [])


def get_campaign_insights(
    campaign_id: str,
    date_preset: str = "last_7d",
    fields: list[str] | None = None,
    time_range: dict | None = None,
    time_increment: int | str | None = None,
    *,
    access_token: str | None = None,
) -> list[dict]:
    """`get_insights()`dan farqli -- butun reklama hisobi emas, FAQAT bitta
    ANIQ kampaniyaning statistikasi (`{campaign_id}/insights` endpoint'i,
    `get_campaign_basic()` kabi to'g'ridan-to'g'ri obyekt ID'siga so'rov).
    2026-09, Target Analizi (`target_analysis.py`) uchun -- bitta live
    kampaniyani diagnostika qilishda butun hisobni o'qib, natijani mahalliy
    filtrlash shart emas."""
    params = {"fields": ",".join(fields or DEFAULT_FIELDS), "limit": 200}
    if time_range:
        params["time_range"] = time_range
    else:
        params["date_preset"] = date_preset
    if time_increment:
        params["time_increment"] = time_increment
    data = _get(f"{campaign_id}/insights", params, token=access_token)
    return data.get("data", [])


def get_account_spend(since: str, until: str, *, access_token: str | None = None, ad_account_id: str | None = None) -> float:
    """Berilgan sana oralig'ida (YYYY-MM-DD, ikkalasi ham kiritiladi) butun
    hisobning (barcha kampaniyalar) umumiy xarajatini qaytaradi. Byudjet
    balansini kuzatish (budget_tracker.py) uchun ishlatiladi."""
    params = {
        "level": "account",
        "time_range": {"since": since, "until": until},  # _get avtomatik JSON'ga o'giradi
        "fields": "spend",
    }
    data = _get(f"{ad_account_id or AD_ACCOUNT_ID}/insights", params, token=access_token)
    rows = data.get("data", [])
    return sum(float(r.get("spend", 0)) for r in rows)


def get_account_daily_spend_avg(days: int = 3, *, access_token: str | None = None, ad_account_id: str | None = None) -> float:
    """So'nggi N kunlik o'rtacha KUNLIK xarajatni qaytaradi (byudjet necha
    kunga/qachon tugashini hisoblash uchun burn-rate)."""
    params = {
        "level": "account",
        "date_preset": f"last_{days}d",
        "fields": "spend",
    }
    data = _get(f"{ad_account_id or AD_ACCOUNT_ID}/insights", params, token=access_token)
    rows = data.get("data", [])
    total = sum(float(r.get("spend", 0)) for r in rows)
    return total / days if days > 0 else 0.0


def get_full_report(
    level: str = "ad",
    date_preset: str = "last_7d",
    breakdowns: list[str] | None = None,
    time_range: dict | None = None,
    *,
    access_token: str | None = None,
    ad_account_id: str | None = None,
) -> list[dict]:
    """`get_insights()` bilan bir xil, lekin video/engagement metrikalarini ham
    qo'shib qaytaradi. Foydalanuvchi "video necha % odam ko'rgan", "hook rate
    qancha", yoki aniq bir kun/oraliq ("20 iyul", "1-10 avgust") so'raganda
    ishlatiladi (orchestrator.answer_data_question).

    `access_token`/`ad_account_id` -- 2026-09, multi-tenant (`get_insights()`
    bilan bir xil naqsh): BERILSA shu ANIQ kompaniyaning hisobidan so'raladi,
    BERILMASA eski global (ENV) xatti-harakat."""
    return get_insights(
        level=level, date_preset=date_preset, breakdowns=breakdowns,
        fields=FULL_REPORTING_FIELDS, time_range=time_range,
        access_token=access_token, ad_account_id=ad_account_id,
    )


def get_active_ads(adset_id: str | None = None) -> list[dict]:
    path = f"{adset_id}/ads" if adset_id else f"{AD_ACCOUNT_ID}/ads"
    return _get_all_pages(path, {"fields": "id,name,status,adset_id,campaign_id", "limit": 200})


def get_account_structure(active_only: bool = True, *, access_token: str | None = None, ad_account_id: str | None = None) -> dict:
    """Kampaniya -> Adset -> Ad daraxtini FAQAT NOM va ID bilan qaytaradi (yengil).

    Bu funksiya juda muhim: foydalanuvchi Telegramda "AB | Traffic | IG" kabi
    o'ziga tanish NOM bilan buyruq beradi (hech kim Meta ID'ni yodlab yurmaydi).
    Targetolog action yaratishdan oldin shu ro'yxatdan mos nomni topib, haqiqiy
    `id`ni ishlatishi kerak — aks holda action bajarilmaydi.

    MUHIM: bu yerda ataylab `targeting` maydoni SO'RALMAYDI — ko'p sonli
    kampaniya/adset bo'lgan hisoblarda to'liq targeting'larni qo'shib yuborish
    Claude'ning kontekst limitidan (200k token) oshib ketishiga sabab bo'lgan.
    Bitta adset'ning to'liq targeting'i kerak bo'lsa, `get_adset_details()`ni
    faqat O'SHA BITTA adset uchun alohida chaqiring.

    `active_only=True` bo'lsa, arxivlangan/o'chirilgan (ARCHIVED/DELETED)
    obyektlar chiqarib tashlanadi — bu ham hajmni sezilarli kamaytiradi."""
    status_filter = {"effective_status": ["ACTIVE", "PAUSED"]} if active_only else None

    # MUHIM (2026-09, foydalanuvchi shikoyati: "targeting'da o'chirilgan
    # targetlar ko'rsatilyapti, pul sarfi noto'g'ri"): oldin faqat `status`
    # (obyektning O'ZINING yoqilgan/o'chirilganligi) so'ralardi. Lekin Meta'da
    # bitta ad o'zi "ACTIVE" bo'lsa ham, uning ustidagi adset yoki kampaniya
    # PAUSED bo'lsa, u AMALDA reklama ko'rsatmaydi -- shuni bilish uchun
    # `effective_status` kerak (butun ierarxiyani hisobga oladi: ACTIVE,
    # PAUSED, CAMPAIGN_PAUSED, ADSET_PAUSED, ARCHIVED va h.k.). Endi ikkalasi
    # ham so'raladi, `dashboard_data.py` effective_status'ni ustun qo'yadi.
    campaign_params = {"fields": "id,name,status,effective_status,objective", "limit": 100}
    adset_params = {"fields": "id,name,status,effective_status,campaign_id,optimization_goal", "limit": 200}
    ad_params = {"fields": "id,name,status,effective_status,adset_id,campaign_id", "limit": 200}
    if status_filter:
        campaign_params["filtering"] = [{"field": "effective_status", "operator": "IN", "value": status_filter["effective_status"]}]
        adset_params["filtering"] = campaign_params["filtering"]
        ad_params["filtering"] = campaign_params["filtering"]

    # Uch chaqiruv ham bir-biriga bog'liq emas -- ketma-ket emas, parallel
    # (bir vaqtda) yuborib, umumiy kutish vaqtini ~3 baravar qisqartiramiz
    # (Vercel'ning 60 soniyalik funksiya limitiga urilib qolish xavfini
    # kamaytirish uchun muhim -- bu funksiya deyarli har bir amaliy buyruq
    # oldidan chaqiriladi).
    acct = ad_account_id or AD_ACCOUNT_ID
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        campaigns_future = pool.submit(_get_all_pages, f"{acct}/campaigns", campaign_params, access_token)
        adsets_future = pool.submit(_get_all_pages, f"{acct}/adsets", adset_params, access_token)
        ads_future = pool.submit(_get_all_pages, f"{acct}/ads", ad_params, access_token)
        campaigns = campaigns_future.result()
        adsets = adsets_future.result()
        ads = ads_future.result()
    return {"campaigns": campaigns, "adsets": adsets, "ads": ads}


# ---------------------------------------------------------------------------
# OAuth ("Facebook Login for Business") -- 2026-09, foydalanuvchi so'rovi:
# "boshqa kompaniyalar BITTA TUGMA bilan o'z Facebook'iga kirib, reklama
# hisobini ulasin" (avval FAQAT qo'lda token/ID kiritish bor edi).
#
# MUHIM CHEKLOV (foydalanuvchiga aniq aytilishi kerak): bu ILOVA darajasida
# ishlaydi -- `META_APP_ID`/`META_APP_SECRET` Meta uchun Developers
# (developers.facebook.com) saytida ro'yxatdan o'tkazilgan "Facebook Login
# for Business" mahsulotli ilova bo'lishi kerak, OAuth Redirect URI'si
# ushbu saytning `/connect-accounts/facebook/callback` manziliga
# sozlangan bo'lishi kerak. Ilova "Development Mode"da bo'lsa, faqat O'SHA
# ilovaga administrator/tester sifatida qo'shilgan Facebook hisoblar orqali
# ulanish ishlaydi -- BOSHQA (haqiqiy, uchinchi tomon) kompaniyalar uchun
# `ads_management`/`ads_read` kabi cheklangan ruxsatlar Meta App Review'dan
# o'tishi shart (odatda bir necha kun-hafta). App Review'gacha bo'lgan
# davrda tugma baribir ishlaydi -- lekin faqat Page/Instagram ulash uchun
# (agar shu ruxsatlar review talab qilmasa) yoki ilovaga qo'shilgan test
# foydalanuvchilar uchun reklama hisobi bilan ham.
# ---------------------------------------------------------------------------

def oauth_configured() -> bool:
    return bool(META_APP_ID and META_APP_SECRET)


def oauth_dialog_url(redirect_uri: str, state: str, include_ads_scope: bool) -> str:
    """Foydalanuvchini Facebook'ning o'zining "ruxsat berish" oynasiga
    yo'naltirish uchun URL. `include_ads_scope=True` bo'lsa (kompaniya
    tarifi reklama hisobini ulashga ruxsat bersa), `ads_management`/
    `ads_read` ham so'raladi -- bular Meta tomonidan cheklangan (App Review
    talab qiladigan) ruxsatlar.

    BUG FIX (2026-09, foydalanuvchi sinovda ketma-ket ikkita "Invalid
    Scopes" xatosini oldi):
      1. `read_insights` -- Meta'ning ESKI, allaqachon Login dialogidan
         OLIB TASHLANGAN ruxsati (yillar oldin `manage_pages`/
         `publish_actions` bilan bir qatorda bekor qilingan) -- so'ralsa,
         Facebook OAuth so'rovining O'ZINI butunlay rad etadi. Olib
         tashlandi.
      2. O'rniga qo'shilgan `instagram_manage_insights` HAM jonli sinovda
         "Invalid Scopes" bilan rad etildi -- sabab: bu ruxsat App
         Dashboard'da "App Review -> Permissions and Features" bo'limida
         ALOHIDA so'ralmaguncha (hatto faqat testerlar uchun ham),
         Login dialogiga umuman qo'shib bo'lmaydi (`pages_show_list`/
         `pages_read_engagement`/`instagram_basic`dan farqli -- ular
         standart, avtomatik ruxsat etilgan). Shuning uchun HOZIRCHA
         scope ro'yxatidan OLIB TASHLANDI -- asosiy "ulash" oqimi
         (sahifa/Instagram/reklama hisobini bog'lash) bunga muhtoj emas.
         Instagram statistikasi (`get_instagram_media_insights`) shu
         ruxsatsiz ishlamaydi, lekin bu XATO EMAS -- `smm_sync.py`
         allaqachon bunday holatni yumshoq tutadi (aniq xabar bilan
         "olinmadi" deb ko'rsatadi, sinxronizatsiyani yiqitmaydi).
         `instagram_manage_insights` kerak bo'lsa, foydalanuvchi App
         Dashboard -> App Review -> Permissions and Features'da shu
         ruxsatni so'rab (testerlar uchun review shart emas, faqat
         "Request" bosish kifoya bo'lishi kerak), keyin bu ro'yxatga
         qaytarib qo'shishi mumkin.

    BUG FIX (2026-09, foydalanuvchi so'rovi -- Instagram xabarlarga javob
    yozganda "(#230) Requires instagram_manage_messages permission"):
    `ig_dm_sync.py`/`meta_api.py`dagi Instagram DM funksiyalari ALLAQACHON
    `instagram_manage_messages` ruxsatini talab qilar edi (izohlarda ham
    aniq yozilgan), LEKIN bu ruxsat shu OAuth "ulash" oynasining scope
    ro'yxatiga HECH QACHON qo'shilmagan edi -- ya'ni Facebook hech qachon
    bu ruxsatni SO'RAMAGAN, shuning uchun "Facebook bilan qayta ulash"
    tugmasi necha marta bosilmasin, bu ruxsat HECH QACHON berilmasdi
    (so'ralmagan narsa berilmaydi). Endi qo'shildi.

    OGOHLANTIRISH (yuqoridagi `instagram_manage_insights` saboqiga qarang):
    agar Facebook bu ruxsatni ham "Invalid Scopes" bilan rad etsa (ya'ni
    OAuth oynasining O'ZI ochilmay, xato qaytarsa) -- bu ruxsat App
    Dashboard'da avval ALOHIDA "yoqilishi" (Instagram -> Instagram API
    setup / "Instagram Messaging" mahsuloti qo'shilishi va akkaunt
    Instagram Tester sifatida qo'shilishi) kerakligini bildiradi, KOD
    XATOSI EMAS. Shunday bo'lsa, bu qatorni vaqtincha qaytarib olib
    tashlash kerak bo'ladi (aks holda ads/pages ulanishi ham buziladi)."""
    from urllib.parse import urlencode

    scopes = ["pages_show_list", "pages_read_engagement", "instagram_basic", "instagram_manage_messages"]
    if include_ads_scope:
        scopes += ["ads_management", "ads_read", "business_management"]
    params = {
        "client_id": META_APP_ID,
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": ",".join(scopes),
        "response_type": "code",
        # BUG FIX (2026-09, jonli sinovda topilgan): foydalanuvchi OAuth
        # oynasidan MUVAFFAQIYATLI o'tgandan keyin ham SMM hisobotda
        # "(#10) This endpoint requires the 'pages_read_engagement'
        # permission" xatosi chiqishda davom etdi. Sabab -- Facebook shu
        # ilova (App) uchun foydalanuvchidan OLDINROQ (bu segmentdagi ikkita
        # "Invalid Scopes" xatosi paytida) bir marta ruxsat so'ragan edi;
        # keyinchalik scope ro'yxati kengaytirilganda (masalan,
        # `pages_read_engagement` qo'shilganda), Facebook ODATDA foydalanuvchi
        # ILGARI bir marta ilovaga ruxsat bergan bo'lsa, YANGI qo'shilgan
        # ruxsat(lar) uchun QAYTA so'ramaydi -- shunchaki eski (torroq)
        # ruxsat to'plami bilan davom etadi, hatto foydalanuvchi "qayta
        # ulasa" ham. Rasmiy yechim -- `auth_type=rerequest`: bu Facebook'ga
        # foydalanuvchidan SO'RALGAN barcha ruxsatlarni (eski+yangi)
        # albatta QAYTADAN ko'rsatib so'rashni majburlaydi.
        "auth_type": "rerequest",
    }
    return f"https://www.facebook.com/{GRAPH_API_VERSION}/dialog/oauth?{urlencode(params)}"


def oauth_exchange_code(code: str, redirect_uri: str) -> str:
    """OAuth `code`ni QISQA muddatli foydalanuvchi access token'iga
    almashtiradi. `_get()` yordamchisi ATAYLAB ishlatilmaydi -- u har doim
    `access_token` parametrini (global `ACCESS_TOKEN`) qo'shib yuboradi,
    bu yerda esa client_id/client_secret autentifikatsiya qiladi."""
    r = requests.get(f"{GRAPH_URL}/oauth/access_token", params={
        "client_id": META_APP_ID, "client_secret": META_APP_SECRET,
        "redirect_uri": redirect_uri, "code": code,
    }, timeout=30)
    data = r.json()
    if "error" in data:
        raise MetaAPIError(data["error"])
    return data["access_token"]


def oauth_exchange_long_lived(short_token: str) -> tuple[str, "int | None"]:
    """QISQA muddatli (~1-2 soatlik) tokenni ~60 kunlik UZOQ muddatli
    tokenga almashtiradi -- shu token Company.meta_access_token'da
    saqlanadi (xuddi qo'lda kiritilgan token kabi).

    2026-09 TUZATISH ("production-ready ... token muddati" so'rovi):
    ILGARI Meta qaytargan `expires_in` (soniyada, odatda ~5184000 = 60
    kun) BUTUNLAY TASHLAB YUBORILARDI -- token qachon tugashi HECH QAYERDA
    saqlanmagan, shuning uchun u jimgina ishlamay qolganda foydalanuvchiga
    HECH QANDAY signal ko'rsatib bo'lmasdi. Endi `(token, expires_in)`
    juftligi qaytariladi -- chaqiruvchi (`app.py`) buni
    `Company.meta_token_expires_at`ga yozadi. Meta ba'zan `expires_in`ni
    umuman qaytarmasligi mumkin (masalan muddatsiz token uchun) -- bunday
    holda `None` qaytariladi."""
    r = requests.get(f"{GRAPH_URL}/oauth/access_token", params={
        "grant_type": "fb_exchange_token", "client_id": META_APP_ID,
        "client_secret": META_APP_SECRET, "fb_exchange_token": short_token,
    }, timeout=30)
    data = r.json()
    if "error" in data:
        raise MetaAPIError(data["error"])
    expires_in = data.get("expires_in")
    try:
        expires_in = int(expires_in) if expires_in is not None else None
    except (TypeError, ValueError):
        expires_in = None
    return data["access_token"], expires_in


def oauth_list_pages(user_token: str) -> list[dict]:
    """Foydalanuvchi administratori bo'lgan barcha Facebook Page'lar
    ro'yxatini (va ularga ulangan Instagram Business akkauntni, bo'lsa)
    qaytaradi."""
    data = _get("me/accounts", {
        "fields": "id,name,instagram_business_account{id,username}",
        "limit": 200,
    }, token=user_token)
    return data.get("data", [])


def oauth_list_ad_accounts(user_token: str) -> list[dict]:
    """Foydalanuvchi kirishi bor barcha reklama hisoblari ro'yxatini
    qaytaradi (`id` allaqachon "act_..." formatida keladi)."""
    data = _get("me/adaccounts", {
        "fields": "id,name,account_status,currency",
        "limit": 200,
    }, token=user_token)
    return data.get("data", [])


def get_ad_account_pixels(ad_account_id: str, access_token: str) -> list[dict]:
    """2026-09, foydalanuvchi so'rovi ("capi nima bo'lsa hammasini avtomatik
    tugma orqali qiladigan qil"): reklama hisobiga BIRIKTIRILGAN Meta
    Pixel(lar) ro'yxatini qaytaradi (`GET /act_.../adspixels`). Bu orqali
    Conversions API (CAPI) sozlashda foydalanuvchidan Pixel ID'ni Meta
    Events Manager'dan qidirib, qo'lda topib kiritishi SHART emas -- reklama
    hisobi OAuth orqali ulangan payt avtomatik topiladi (qarang: app.py
    `_save_facebook_connection`). Xato yoki ruxsat yetishmasa (masalan
    hisobda hali Pixel yaratilmagan bo'lsa) -- bo'sh ro'yxat qaytaradi,
    xato tashlamaydi, chunki bu CAPI -- IXTIYORIY, asosiy ulanish oqimini
    hech qachon to'xtatmasligi kerak."""
    try:
        data = _get(f"{ad_account_id}/adspixels", {"fields": "id,name"}, token=access_token)
        return data.get("data", [])
    except MetaAPIError:
        return []


def verify_dataset_credentials(dataset_id: str, access_token: str) -> dict:
    """Advanced/Manual sozlash formasi ("Dataset ID + CAPI Access Token")
    SAQLASHDAN OLDIN chaqiradi -- Meta'ning o'ziga haqiqiy so'rov yuborib,
    berilgan Dataset ID shu token bilan HAQIQATAN ko'rinishini tekshiradi.
    Muvaffaqiyatsiz bo'lsa `MetaAPIError` ko'taradi (chaqiruvchi
    `safe_error_message()` bilan foydalanuvchiga xavfsiz xabar ko'rsatadi).
    """
    return _get(dataset_id, {"fields": "id,name"}, token=access_token)


# ---------------------------------------------------------------------------
# Business Portfolio / Dataset tanlash -- 2026-09, "production-ready Meta Ads
# + CAPI integration" so'rovi: ilgari ulanish oqimi FAQAT Page + Ad Account
# so'rardi, Pixel esa AVTOMATIK (birinchisi) olinardi -- foydalanuvchiga hech
# qanday tanlov ko'rsatilmasdi. Endi aniq uch bosqichli tanlov: Business
# Portfolio -> Ad Account -> Dataset/Pixel (rasmiy Graph API obyekt modeliga
# mos: /me/businesses, /{business}/owned_ad_accounts+client_ad_accounts,
# /{business}/owned_pixels+client_pixels).
# ---------------------------------------------------------------------------

def oauth_list_businesses(user_token: str) -> list[dict]:
    """Foydalanuvchi a'zo bo'lgan Business Portfolio (Business Manager)
    ro'yxatini qaytaradi. Ko'p kichik reklamachida Business Manager umuman
    bo'lmasligi mumkin -- bo'sh ro'yxat ODATIY holat, xato emas."""
    try:
        data = _get("me/businesses", {"fields": "id,name", "limit": 200}, token=user_token)
        return data.get("data", [])
    except MetaAPIError:
        return []


def oauth_list_ad_accounts_for_business(business_id: str, user_token: str) -> list[dict]:
    """Berilgan Business Portfolio'ga tegishli reklama hisoblari --
    o'zining (`owned_ad_accounts`) va unga ulashilgan (`client_ad_accounts`,
    agentlik holati) ikkalasini ham birlashtirib qaytaradi. Xato bo'lsa
    (masalan ruxsat yetishmasa) bo'sh ro'yxat -- `oauth_list_ad_accounts()`
    (`/me/adaccounts`) ga qaytish chaqiruvchi tomonda amalga oshiriladi."""
    accounts: dict[str, dict] = {}
    for edge in ("owned_ad_accounts", "client_ad_accounts"):
        try:
            data = _get(f"{business_id}/{edge}", {"fields": "id,name,account_status,currency", "limit": 200}, token=user_token)
            for a in data.get("data", []):
                accounts[a["id"]] = a
        except MetaAPIError:
            continue
    return list(accounts.values())


def get_business_pixels(business_id: str, user_token: str) -> list[dict]:
    """Berilgan Business Portfolio'ning o'z (`owned_pixels`) va unga
    ulashilgan (`client_pixels`) Pixel/Dataset'lari ro'yxati."""
    pixels: dict[str, dict] = {}
    for edge in ("owned_pixels", "client_pixels"):
        try:
            data = _get(f"{business_id}/{edge}", {"fields": "id,name", "limit": 200}, token=user_token)
            for p in data.get("data", []):
                pixels[p["id"]] = p
        except MetaAPIError:
            continue
    return list(pixels.values())


def oauth_revoke(user_token: str) -> bool:
    """Disconnect paytida -- Meta'ning o'z "barcha ruxsatlarni bekor
    qilish" endpoint'i (`DELETE /me/permissions`). Best-effort: token
    allaqachon eskirgan/bekor qilingan bo'lsa ham xato tashlamaydi (chunki
    disconnect BARIBIR mahalliy bazadan tozalanishi kerak) -- shunchaki
    True/False qaytaradi, chaqiruvchi buni faqat log/UX uchun ishlatadi."""
    try:
        r = requests.delete(f"{GRAPH_URL}/me/permissions", params={"access_token": user_token}, timeout=15)
        data = r.json()
        return bool(data.get("success"))
    except Exception:
        return False


def is_token_expired_error(e: Exception) -> bool:
    """`MetaAPIError`ning Meta error kodi 190 (OAuthException -- token
    muddati o'tgan/bekor qilingan/parol o'zgargan) ekanini tekshiradi.
    Rasmiy Meta hujjatida shu kod aynan "token endi yaroqsiz, foydalanuvchi
    qayta login qilishi kerak" degani -- boshqa har qanday xato (tarmoq,
    rate limit, noto'g'ri parametr) bilan aralashtirmaslik uchun aniq
    kod bo'yicha tekshiriladi, taxmin qilinmaydi."""
    if not isinstance(e, MetaAPIError) or not e.args or not isinstance(e.args[0], dict):
        return False
    return e.args[0].get("code") == 190


def get_object_status(object_id: str, *, access_token: str | None = None) -> dict:
    """Ad/AdSet/Campaign'ning joriy holatini (status) qaytaradi. pause_object()/
    activate_object() dan keyin haqiqatan o'zgarganini TASDIQLASH uchun ishlatiladi
    — Meta ba'zan {"success": true} qaytarsa ham, holat kutilganidek o'zgarmagan
    bo'lishi mumkin (masalan yuqori darajadagi kampaniya/adset o'chiq bo'lsa).

    2026-09 multi-tenant: `access_token` berilsa, O'SHA (kompaniyaning o'zi
    ulagan) token bilan so'raladi -- berilmasa eski global ACCESS_TOKEN
    ishlatiladi (`_get`ning o'zidagi fallback orqali)."""
    return _get(object_id, {"fields": "id,name,status,effective_status"}, access_token)


def get_adset_details(adset_id: str, *, access_token: str | None = None) -> dict:
    """Bitta adset'ning to'liq sozlamalarini (targeting, byudjet va h.k.) qaytaradi.
    Targetolog `account_structure`dan kerakli adset'ni nom bo'yicha topgach, aynan
    o'sha bitta adset uchun bu funksiya chaqiriladi — barcha adsetlarning
    targeting'ini birdaniga yubormaslik uchun (token limitidan oshib ketmasligi uchun).

    2026-09 multi-tenant (job_watch_cycle ko'p-kompaniyaga kengaytirildi):
    `access_token` berilsa, O'SHA kompaniyaning o'z token'i bilan so'raladi."""
    return _get(adset_id, {"fields": "id,name,status,campaign_id,daily_budget,targeting,optimization_goal"}, access_token)


# ---------------------------------------------------------------------------
# ON/OFF VA BYUDJET BOSHQARUVI
# ---------------------------------------------------------------------------

def set_status(object_id: str, status: str, *, access_token: str | None = None) -> dict:
    """Ad/AdSet/Campaign holatini o'rnatadi (ACTIVE / PAUSED / ARCHIVED).
    pause_object/activate_object shu funksiyaning qulay wrapperlari.

    2026-09 multi-tenant: `access_token` berilsa, O'SHA (kompaniyaning o'zi
    ulagan) token bilan yuboriladi -- berilmasa eski global ACCESS_TOKEN
    (`_post`ning o'zidagi fallback orqali)."""
    return _post(object_id, {"status": status}, access_token)


def pause_object(object_id: str, *, access_token: str | None = None) -> dict:
    """Ad, AdSet yoki Campaign'ni pauza qiladi."""
    return set_status(object_id, "PAUSED", access_token=access_token)


def activate_object(object_id: str, *, access_token: str | None = None) -> dict:
    """Ad, AdSet yoki Campaign'ni qayta ishga tushiradi."""
    return set_status(object_id, "ACTIVE", access_token=access_token)


def archive_object(object_id: str, *, access_token: str | None = None) -> dict:
    """Kerak bo'lmay qolgan (uzoq vaqt pauzada, kelajakda ishlatilmaydigan)
    kampaniya/adset'ni arxivlaydi — o'chirib tashlash (DELETED) emas, shuning
    uchun kerak bo'lsa Ads Manager'da qaytarib bo'ladi, lekin ro'yxatlarni
    "toza" qiladi."""
    return set_status(object_id, "ARCHIVED", access_token=access_token)


def update_daily_budget(adset_id: str, new_daily_budget_cents: int, *, access_token: str | None = None) -> dict:
    """Byudjet Meta API'da eng kichik valyuta birligida (masalan tiyin/cent)
    beriladi. Masalan $10.00 -> 1000."""
    return _post(adset_id, {"daily_budget": new_daily_budget_cents}, access_token)


def adjust_budget_by_percent(adset_id: str, current_daily_budget_cents: int, percent: float, *, access_token: str | None = None) -> dict:
    """4.4-bo'lim qoidasiga ko'ra: bir martada 10-20% oralig'ida o'zgartirish
    tavsiya etiladi. `percent` musbat (oshirish) yoki manfiy (kamaytirish)."""
    new_budget = int(current_daily_budget_cents * (1 + percent / 100))
    return update_daily_budget(adset_id, new_budget, access_token=access_token)


# ---------------------------------------------------------------------------
# AUDITORIYA / HUDUD SOZLAMALARI (4.11-bo'lim)
# ---------------------------------------------------------------------------

def _sanitize_targeting_for_write(targeting: dict) -> dict:
    """Meta Graph API'dan GET orqali o'qilgan targeting obyektida ba'zan
    yozib bo'lmaydigan/normalizatsiya qilinmaydigan qiymatlar uchraydi
    (masalan `targeting_automation.individual_setting` ichida kutilmagan
    kalit/qiymat, "Normalization does not allow the value ..." xatosi).
    Bunday obyektni o'zgarishsiz qaytarib yuborish Meta'dan "Invalid
    parameter" xatosiga olib keladi.

    Bu funksiya `targeting_automation.individual_setting`da FAQAT ma'lum,
    xavfsiz deb bilingan kalitlarni (age/gender/geo, qiymati 0 yoki 1)
    qoldiradi, qolganini olib tashlaydi. Original `targeting` obyekti
    o'zgartirilmaydi (nusxa qaytariladi)."""
    targeting = dict(targeting)
    automation = targeting.get("targeting_automation")
    if isinstance(automation, dict) and isinstance(automation.get("individual_setting"), dict):
        automation = dict(automation)
        safe_individual = {
            k: v for k, v in automation["individual_setting"].items()
            if k in ("age", "gender", "geo") and v in (0, 1)
        }
        if safe_individual:
            automation["individual_setting"] = safe_individual
        else:
            automation.pop("individual_setting", None)
        targeting["targeting_automation"] = automation
    return targeting


def set_location_current_city_only(adset_id: str, city_key: str, *, access_token: str | None = None) -> dict:
    """Ad Set targeting'ini faqat joriy shaharga cheklaydi va avtokengaytirishni
    o'chiradi ("Reach more people likely to respond" -> off)."""
    targeting = {
        "geo_locations": {
            "cities": [{"key": city_key, "radius": 0, "distance_unit": "kilometer"}],
            "location_types": ["home"],  # faqat shu shaharda yashovchilar
        },
        "targeting_automation": {"advantage_audience": 0},  # auto-expansion off
    }
    return _post(adset_id, {"targeting": _sanitize_targeting_for_write(targeting)}, access_token)


def update_targeting(adset_id: str, targeting: dict, *, access_token: str | None = None) -> dict:
    """Ad Set auditoriyasini to'liq yangi targeting spec bilan almashtiradi.
    Yozishdan oldin avtomatik ravishda xavfsizlashtiriladi (`_sanitize_targeting_for_write`)."""
    return _post(adset_id, {"targeting": _sanitize_targeting_for_write(targeting)}, access_token)


def search_geo_location(query: str, location_types: list[str] | None = None, *, access_token: str | None = None) -> list[dict]:
    """Erkin matndagi joy nomini (masalan 'Chirchiq', 'Zangiota tumani') Meta'ning
    rasmiy geo-target kaliti va turiga bog'laydi. Bir nechta nomzod qaytishi mumkin
    (bir xil nomli joylar turli davlatlarda bo'lishi mumkin) — Targetolog davlat/
    kontekstga qarab eng mosini tanlashi kerak. Natija elementlari:
    {"key": "...", "name": "...", "type": "city"|"region"|"country"|..., "country_code": "UZ", "region": "..."}
    Bu funksiyasiz shahar/tuman nomlarini exclude/include qilib bo'lmaydi — Meta
    faqat raqamli `key` bilan ishlaydi, nom bilan emas.

    2026-09, Meta Ads Autopilot: `access_token` kwarg qo'shildi -- har bir
    kompaniya O'Z tokeni bilan qidiradi (berilmasa eski global token)."""
    params = {"type": "adgeolocation", "q": query}
    if location_types:
        params["location_types"] = location_types
    data = _get("search", params, token=access_token)
    out = []
    for item in data.get("data", []):
        # Meta qaytargan barcha maydonlar saqlanadi (orchestrator LLM'ga
        # to'liq nomzodni ko'rsatadi), lekin 5 ta asosiy kalit HAR DOIM bor.
        row = dict(item)
        for key in ("key", "name", "type", "country_code", "region"):
            row.setdefault(key, None)
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# YANGI KAMPANIYA/ADSET/AD YARATISH (targetni "o'zi to'liq yoqishi" uchun)
# ---------------------------------------------------------------------------

def create_campaign(
    name: str,
    objective: str = "OUTCOME_LEADS",   # OUTCOME_LEADS | OUTCOME_SALES | OUTCOME_ENGAGEMENT | OUTCOME_TRAFFIC
    status: str = "PAUSED",
    special_ad_categories: list | None = None,
    *,
    access_token: str | None = None,
    ad_account_id: str | None = None,
) -> dict:
    return _post(f"{ad_account_id or AD_ACCOUNT_ID}/campaigns", {
        "name": name,
        "objective": objective,
        "status": status,
        "special_ad_categories": special_ad_categories or [],
    }, access_token)


def create_adset(
    campaign_id: str,
    name: str,
    daily_budget_cents: int,
    targeting: dict,
    optimization_goal: str = "OFFSITE_CONVERSIONS",
    billing_event: str = "IMPRESSIONS",
    bid_strategy: str = "LOWEST_COST_WITHOUT_CAP",
    status: str = "PAUSED",
    promoted_object: dict | None = None,
    *,
    access_token: str | None = None,
    ad_account_id: str | None = None,
    lifetime_budget_cents: int | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    destination_type: str | None = None,
) -> dict:
    """Bo'lim 4.2-4.3 qoidalariga mos targeting spec bilan yangi Ad Set yaratadi.

    `targeting` namunasi (broad, faqat yosh/jins/hudud — 4.2-bo'lim tavsiyasiga ko'ra):
    {
        "geo_locations": {"cities": [{"key": "2430536", "radius": 0, "distance_unit": "kilometer"}]},
        "age_min": 18, "age_max": 65,
        "targeting_automation": {"advantage_audience": 1}
    }

    2026-09, Meta Ads Autopilot -- ixtiyoriy kwarg'lar qo'shildi (eski
    chaqiruvlar o'zgarmaydi): `lifetime_budget_cents` berilsa `daily_budget`
    O'RNIGA umumiy byudjet yuboriladi (Meta ikkalasini birga qabul
    qilmaydi); `start_time`/`end_time` (ISO); `destination_type` (MESSENGER/
    INSTAGRAM_DIRECT/WHATSAPP/WEBSITE/ON_AD/PHONE_CALL) -- Meta v21 hujjati
    bo'yicha; rad etilsa friendly xato ko'rsatiladi.
    """
    payload = {
        "name": name,
        "campaign_id": campaign_id,
        "targeting": targeting,
        "optimization_goal": optimization_goal,
        "billing_event": billing_event,
        "bid_strategy": bid_strategy,
        "status": status,
    }
    if lifetime_budget_cents:
        payload["lifetime_budget"] = lifetime_budget_cents
    else:
        payload["daily_budget"] = daily_budget_cents
    if start_time:
        payload["start_time"] = start_time
    if end_time:
        payload["end_time"] = end_time
    if destination_type:
        payload["destination_type"] = destination_type
    if promoted_object:
        payload["promoted_object"] = promoted_object
    return _post(f"{ad_account_id or AD_ACCOUNT_ID}/adsets", payload, access_token)


def create_ad(
    adset_id: str, name: str, creative_id: str, status: str = "PAUSED",
    *, access_token: str | None = None, ad_account_id: str | None = None,
) -> dict:
    """Mavjud creative_id'dan foydalanib reklama yaratadi. AI video/rasm generatsiya
    qila olmaydi — creative_id avvaldan Ads Manager'da yuklangan bo'lishi kerak."""
    return _post(f"{ad_account_id or AD_ACCOUNT_ID}/ads", {
        "name": name,
        "adset_id": adset_id,
        "creative": {"creative_id": creative_id},
        "status": status,
    }, access_token)


# ---------------------------------------------------------------------------
# A/B TEST (Meta'ning native "copies" funksiyasi orqali)
# ---------------------------------------------------------------------------

def copy_adset(
    adset_id: str, rename_suffix: str = " - B variant", status_option: str = "PAUSED",
    *, access_token: str | None = None,
) -> dict:
    """Ad Set'ni nusxalaydi — A/B test uchun B variantini yaratish uchun ishlatiladi.
    Nusxalangach, `update_targeting()` yoki yangi creative bilan `create_ad()`
    orqali B variantda faqat BITTA o'zgaruvchini (masalan auditoriya turi yoki
    kreativ) farqlantiring — qolgan hammasi bir xil bo'lishi kerak (toza test)."""
    return _post(f"{adset_id}/copies", {
        "rename_options": {
            "rename_suffix": rename_suffix,
            "rename_strategy": "ONLY_TOP_LEVEL_RENAME",
        },
        "status_option": status_option,
    }, access_token)


# ---------------------------------------------------------------------------
# KREATIV MATNINI ALMASHTIRISH (replace_creative -- matn-avtonom variant, 2026-08)
#
# AdCreative'lar Meta'da IMMUTABLE -- mavjudini "tahrirlab" bo'lmaydi. Shuning
# uchun "reklama matnini yangilash" aslida: (1) joriy kreativning to'liq
# object_story_spec'ini (rasm/video shu ichida -- image_hash/video_id) o'qib
# olish, (2) FAQAT matn maydonini (message/name) yangi qiymat bilan
# almashtirib, YANGI AdCreative yaratish, (3) reklamaga o'sha yangi creative_id
# ni biriktirish. Rasm/video O'ZGARMAYDI -- shuning uchun bu AI hali rasm/video
# generatsiya qila olmasa ham, mavjud vizual bilan matnni avtonom yangilashga
# yetarli.
# ---------------------------------------------------------------------------

def get_ad_creative_details(ad_id: str, *, access_token: str | None = None) -> dict:
    """Reklamaning joriy kreativini (matn + rasm/video) qaytaradi.
    `replace_creative` uchun MUHIM: yangi creative yaratishdan oldin joriy
    `object_story_spec`ning AYNAN NUSXASIDAN boshlash kerak (noldan qurish
    EMAS) -- aks holda rasm/video yo'qolib ketishi yoki Meta "invalid
    creative" xatosi berishi mumkin."""
    data = _get(ad_id, {"fields": "id,name,adset_id,creative{id,object_story_spec,image_hash,video_id}"}, access_token)
    creative = data.get("creative", {}) or {}
    return {
        "ad_id": data.get("id"),
        "ad_name": data.get("name"),
        "adset_id": data.get("adset_id"),
        "creative_id": creative.get("id"),
        "object_story_spec": creative.get("object_story_spec", {}),
        "image_hash": creative.get("image_hash"),
        "video_id": creative.get("video_id"),
    }


def extract_ad_copy_text(object_story_spec: dict) -> str:
    """`object_story_spec`dan (yuqoridagi `get_ad_creative_details()`
    qaytargan) o'qiladigan reklama matnini (sarlavha + asosiy matn)
    ajratib oladi -- 2026-09, foydalanuvchi so'rovi ("sifatli chiqqan
    reklamaga copyni ulash"): DM tahlilida qaysi reklama yaxshi natija
    berayotganini ko'rsatganda, menejer o'sha matnni o'qib, Meta Ads
    Manager'da qo'lda nusxalay olishi uchun. `video_data`/`link_data`/
    `photo_data`dan qaysi biri bo'lsa, o'shandan (Meta reklama turiga
    qarab qaysi biri to'ldirilgani farq qiladi)."""
    spec = object_story_spec or {}
    for key in ("video_data", "link_data", "photo_data"):
        block = spec.get(key)
        if not block:
            continue
        headline = (block.get("title") or block.get("name") or "").strip()
        body = (block.get("message") or "").strip()
        parts = [p for p in (headline, body) if p]
        if parts:
            return "\n".join(parts)
    return ""


def create_ad_creative_with_new_copy(
    page_id: str,
    base_story_spec: dict,
    primary_text: str,
    headline: str | None = None,
    name: str | None = None,
    *,
    access_token: str | None = None,
    ad_account_id: str | None = None,
) -> dict:
    """Mavjud kreativning `object_story_spec`idan (rasm/video O'ZGARMAYDI)
    chuqur nusxa olib, FAQAT matn maydonlarini (`link_data.message`/`name`
    yoki `video_data.message`/`title`) yangi qiymat bilan almashtirib, YANGI
    AdCreative yaratadi. Reklamaga biriktirish uchun keyin
    `update_ad_creative()` chaqiriladi."""
    story_spec = json.loads(json.dumps(base_story_spec or {}))  # chuqur nusxa
    story_spec["page_id"] = story_spec.get("page_id") or page_id

    if "video_data" in story_spec:
        story_spec["video_data"]["message"] = primary_text
        if headline:
            story_spec["video_data"]["title"] = headline
    else:
        # link_data bo'lmasa ham (masalan photo_data), matn saqlanib qolishi
        # uchun eng keng tarqalgan holatga -- link_data'ga -- tushamiz.
        story_spec.setdefault("link_data", {})["message"] = primary_text
        if headline:
            story_spec["link_data"]["name"] = headline

    payload = {
        "name": name or "Target Master — yangilangan matn",
        "object_story_spec": story_spec,
    }
    return _post(f"{ad_account_id or AD_ACCOUNT_ID}/adcreatives", payload, access_token)


def update_ad_creative(ad_id: str, creative_id: str, *, access_token: str | None = None) -> dict:
    """Mavjud reklamaga YANGI creative'ni biriktiradi (eskisi endi
    ko'rsatilmaydi, lekin arxivda saqlanib qoladi). Reklamaning o'zi (ad_id,
    demak statistika tarixi) o'zgarmaydi."""
    return _post(ad_id, {"creative": {"creative_id": creative_id}}, access_token)


# ---------------------------------------------------------------------------
# INSTANT FORMS / LEAD ADS (4.9-bo'lim)
# ---------------------------------------------------------------------------

def create_lead_form(page_id: str, form_config: dict, *, access_token: str | None = None) -> dict:
    """Instant Form (Lead Ads) yaratadi.

    form_config namunasi:
    {
        "name": "Kurs uchun lid formasi",
        "intro": {"headline": "IELTS 7+ bo'lishni xohlaysizmi?", "description": "..."},
        "questions": [
            {"type": "FULL_NAME"},
            {"type": "PHONE"},
            {"type": "CUSTOM", "key": "hudud", "label": "Qaysi shahardansiz?"},
        ],
        "privacy_policy": {"url": "https://example.com/privacy"},
        "thank_you_page": {"title": "Rahmat!", "body": "Tez orada bog'lanamiz."},
    }

    2026-09, Meta Ads Autopilot: `access_token` (kompaniyaning foydalanuvchi
    tokeni) berilsa, shu Page uchun Page Access Token AYNAN shu tokendan
    olinadi (multi-tenant); berilmasa eski global yo'l.
    """
    page_token = _get_page_access_token(page_id=page_id, user_access_token=access_token) if access_token else _get_page_access_token()
    return _post(f"{page_id}/leadgen_forms", form_config, token=page_token)


def get_leads(form_id: str, since: str | None = None, *, access_token: str | None = None, page_id: str | None = None) -> list[dict]:
    """Formadan tushgan lidlarni qaytaradi (leads_retrieval permission talab
    qilinadi). CRM lead-sync job'i (`lead_sync.py`) shu funksiyani har bir
    forma uchun muntazam chaqirib, yangi lidlarni bazaga yozadi -- shuning
    uchun attributsiya uchun kerakli maydonlar ham so'raladi (`ad_id`,
    `adset_id`, `campaign_id`), dashboard'da "qaysi kampaniyadan nechta lead
    kelgani"ni ko'rsatish uchun.

    `since` berilsa (ISO sana yoki unix timestamp), faqat shu sanadan keyingi
    lidlar so'raladi -- har safar BARCHA tarixni qayta o'qimaslik uchun.

    2026-09 BUG FIX (multi-tenant): avval bu funksiya `_get_page_access_token()`ni
    HECH QANDAY argumentsiz chaqirar edi -- ya'ni qaysi kompaniya uchun
    ishlatilayotganidan qat'iy nazar, token doim GLOBAL (ENV) Page/tokendan
    olinardi. Endi `access_token`/`page_id` berilsa, O'SHA kompaniyaning O'Z
    Page Access Token'i olinadi (`_get_page_access_token(page_id=..., user_access_token=...)`)."""
    params = {
        "fields": "id,created_time,ad_id,adset_id,campaign_id,form_id,field_data",
        "limit": 100,
    }
    if since:
        params["filtering"] = [{"field": "time_created", "operator": "GREATER_THAN", "value": since}]
    page_token = _get_page_access_token(page_id=page_id, user_access_token=access_token)
    # 2026-09: endi `_get_all_pages()` orqali -- avvalgi qo'lda yozilgan
    # sahifalash tsikli xatoda LIDLARNI JIM RAVISHDA (hech qanday xatosiz)
    # tashlab yuborardi ("if 'error' in data: break"); endi xato chaqiruvchiga
    # (`lead_sync.py`, bu allaqachon `MetaAPIError`ni to'g'ri tutadi va
    # forma bo'yicha xato hisobotiga yozadi) ko'tariladi -- YO'QOLGAN
    # LIDLAR ENDI SEZILMAY QOLMAYDI. Qayta urinish (tarmoq xatosida) ham
    # avtomatik qo'llanadi.
    return _get_all_pages(f"{form_id}/leads", params, token=page_token)


def get_lead_forms(page_id: str, *, access_token: str | None = None) -> list[dict]:
    """Sahifaga (Page) tegishli BARCHA Instant Form (Lead Ads) formalarini
    qaytaradi -- CRM lead-sync job'i har bir forma bo'yicha `get_leads()`ni
    alohida chaqiradi (Meta API'da "hisobdagi barcha lidlar" degan yagona
    endpoint yo'q, forma orqali so'raladi).

    2026-09 BUG FIX (multi-tenant): avval `page_id` faqat URL yo'lida
    ishlatilib, token esa HAR DOIM global `_get_page_access_token()`dan
    (argumentsiz) olinardi -- ya'ni boshqa kompaniyaning Page ID'si bilan
    chaqirilsa ham, token global kompaniyanikidan olinardi. Endi `access_token`
    berilsa, aynan shu `page_id` + `access_token` juftligi uchun Page Access
    Token olinadi."""
    page_token = _get_page_access_token(page_id=page_id, user_access_token=access_token)
    return _get_all_pages(f"{page_id}/leadgen_forms", {"fields": "id,name,status,leads_count", "limit": 200}, token=page_token)


# ---------------------------------------------------------------------------
# SMM (ORGANIK) STATISTIKA -- Instagram Business va Facebook Page uchun
# obunachilar/post statistikasi (`smm_sync.py` ishlatadi). QO'SHIMCHA
# environment variable KERAK EMAS -- allaqachon sozlangan META_ACCESS_TOKEN
# va META_PAGE_ID yetarli (Page Access Token orqali, xuddi Instant Form
# funksiyalari kabi). Instagram Business akkaunt shu Page'ga ulangan
# bo'lishi kerak (Meta Business Suite -> Sozlamalar -> Bog'langan hisoblar).
# ---------------------------------------------------------------------------

def search_ad_library(search_terms: str, countries: tuple[str, ...] = ("UZ",), limit: int = 30) -> list[dict]:
    """Meta Ad Library (`ads_archive`) orqali biror brend/sahifa nomi
    bo'yicha HOZIR yoki YAQINDA ishlagan reklamalarni qaytaradi (2026-08,
    raqobatchi tahlili uchun qo'shildi).

    MUHIM: bu OMMAVIY (public) endpoint -- reklama BERUVCHIning o'zi
    bo'lish shart emas, oddiy `META_ACCESS_TOKEN` (mavjud, boshqa hech
    narsa sozlash shart emas) yetarli. Faqat "search_terms" (brend nomi)
    bo'yicha qidiradi -- aniq Page ID emas, chunki bizda faqat veb-sayt
    domenlari bor, Page ID emas.
    """
    # `page_id` -- 2026-09, foydalanuvchi so'rovi ("aniq brendni topish
    # uchun logo va obunachi soni ko'rinsin, fayk akkauntlar ko'p"):
    # natijadagi sahifaning HAQIQIY logotipi/obunachilar sonini olish
    # uchun keyinroq `get_page_public_profile(page_id)` alohida so'raladi
    # (ads_archive'ning o'zi bu ma'lumotni bermaydi) -- shuning uchun
    # page_id shu yerda so'ralishi shart.
    data = _get("ads_archive", {
        "search_terms": search_terms,
        "ad_reached_countries": list(countries),
        "ad_active_status": "ALL",
        "ad_type": "ALL",
        "limit": limit,
        "fields": "id,ad_snapshot_url,page_id,page_name,ad_creative_bodies,ad_creative_link_titles,ad_delivery_start_time,ad_delivery_stop_time",
    })
    return data.get("data", [])


def get_page_public_profile(page_id: str) -> dict:
    """Bitta Facebook sahifaning OMMAVIY profil ma'lumotini (logotip rasmi
    + obunachilar soni) qaytaradi -- 2026-09, foydalanuvchi so'rovi:
    "brendlarni aniq logo, nechta obunachisi hammasi ko'rinsin ad
    library'ga o'xshab ... fayk akkauntlar juda ko'p, aniq brendni
    topish uchun kerak". `search_ad_library()` kabi -- bular sahifaning
    O'ZI alohida ruxsat berishi SHART bo'lmagan, umumiy `picture`/
    `fan_count` OMMAVIY maydonlari (standart `META_ACCESS_TOKEN`
    yetarli), lekin `ads_archive` javobida yo'q -- har bir sahifa uchun
    ALOHIDA so'rov kerak.

    Xato bo'lsa (masalan sahifa cheklangan/o'chirilgan, yoki vaqtinchalik
    tarmoq xatosi) BO'SH dict qaytaradi -- BITTA sahifaning
    muvaffaqiyatsizligi butun qidiruv natijasini to'xtatmasligi kerak
    (chaqiruvchi tomon shu sababli buni try/except bilan o'rab chaqiradi)."""
    if not page_id:
        return {}
    try:
        data = _get(page_id, {"fields": "picture{url},fan_count"})
    except MetaAPIError:
        return {}
    picture = ((data.get("picture") or {}).get("data")) or {}
    return {"picture_url": picture.get("url"), "fan_count": data.get("fan_count")}


def get_instagram_business_account_id(*, page_id: str | None = None, access_token: str | None = None) -> str | None:
    """`page_id`ga ulangan Instagram Business akkaunt ID'sini qaytaradi
    (agar ulanmagan bo'lsa -- None). `page_id`/`access_token` berilmasa --
    eski global (ENV) `PAGE_ID`/`ACCESS_TOKEN` (orqaga moslik). 2026-09,
    multi-tenant: `smm_sync.py`/`ig_dm_sync.py` endi HAR BIR kompaniyaning
    O'Z page_id/token'ini shu yerga uzatadi."""
    resolved_page_id = page_id or PAGE_ID
    if not resolved_page_id:
        return None
    data = _get(resolved_page_id, {"fields": "instagram_business_account"}, token=_get_page_access_token(page_id, access_token))
    ig = data.get("instagram_business_account")
    return ig.get("id") if ig else None


def get_facebook_page_profile(*, page_id: str | None = None, access_token: str | None = None) -> dict:
    """Sahifaning joriy obunachilar (fan_count) sonini qaytaradi."""
    resolved_page_id = page_id or PAGE_ID
    return _get(resolved_page_id, {"fields": "fan_count,name"}, token=_get_page_access_token(page_id, access_token))


def get_facebook_page_posts(limit: int = 25, *, page_id: str | None = None, access_token: str | None = None) -> list[dict]:
    """Sahifadagi so'nggi postlarni like/comment/share soni bilan birga
    qaytaradi. Qamrov/ko'rishlar (impressions) alohida `get_facebook_post_insights()`
    orqali so'raladi -- bitta so'rovga qo'shib yuborilsa, insights ruxsati
    yo'q hollarda BUTUN /posts so'rovi xato qaytarib qo'yishi mumkin."""
    # 2026-08, foydalanuvchi so'rovi ("eng faol postlar qaysiligini
    # bilmayman, videomi yo'qmi ko'rsatsin, video boshini/coverini
    # qo'ysa bo'ladimi"): `attachments{media_type,type}` -- post turini
    # aniqlash uchun; `full_picture` -- Meta HAR QANDAY post (rasm YOKI
    # video) uchun avtomatik generatsiya qiladigan muqova/preview rasm
    # URL'i, shu orqali "Eng faol postlar" jadvalida haqiqiy kichik
    # rasm (thumbnail) ko'rsatish mumkin bo'ladi.
    fields = (
        "id,message,created_time,permalink_url,full_picture,"
        "likes.summary(true).limit(0),comments.summary(true).limit(0),shares,"
        "attachments{media_type,type}"
    )
    resolved_page_id = page_id or PAGE_ID
    data = _get(f"{resolved_page_id}/posts", {"fields": fields, "limit": limit}, token=_get_page_access_token(page_id, access_token))
    return data.get("data", [])


def get_facebook_post_insights(post_id: str, *, page_id: str | None = None, access_token: str | None = None) -> dict:
    """Bitta Facebook post uchun qamrov (impressions) va faollashgan
    foydalanuvchilar sonini qaytaradi -- {"post_impressions": N,
    "post_engaged_users": N} ko'rinishida (mavjud bo'lmasa qiymat None)."""
    data = _get(f"{post_id}/insights", {"metric": "post_impressions,post_engaged_users"}, token=_get_page_access_token(page_id, access_token))
    out = {}
    for item in data.get("data", []):
        values = item.get("values") or []
        out[item.get("name")] = values[0].get("value") if values else None
    return out


def get_instagram_profile(ig_user_id: str, *, page_id: str | None = None, access_token: str | None = None) -> dict:
    """Instagram Business akkauntining joriy obunachilar/post sonini qaytaradi."""
    return _get(ig_user_id, {"fields": "followers_count,media_count,username"}, token=_get_page_access_token(page_id, access_token))


def get_instagram_media(ig_user_id: str, limit: int = 25, *, page_id: str | None = None, access_token: str | None = None) -> list[dict]:
    """So'nggi Instagram postlarini (like/comment soni bilan) qaytaradi.
    Har bir media'ning qamrovi (reach) alohida `get_instagram_media_insights()`
    orqali so'raladi (Meta buni asosiy `/media` so'rovida bermaydi)."""
    # `thumbnail_url` -- FAQAT video/reels turidagi media uchun mavjud
    # (Meta shunday cheklaydi); rasm/albom uchun `media_url`ning o'zi
    # muqova sifatida ishlatiladi (2026-08, foydalanuvchi so'rovi: "eng
    # faol postlar" jadvalida qaysi post ekanini bilish uchun kichik
    # rasm/video muqovasi ko'rsatilsin).
    # `media_product_type` -- Meta'ning INSIGHTS METRIKALARINI TANLASH uchun
    # ishlatadigan HAQIQIY maydoni (FEED | REELS | STORY | AD). 2026-08
    # (item 6, foydalanuvchi shikoyati -- SMM ma'lumotlari "notori"):
    # avval bu maydon UMUMAN so'ralmas edi, kod noto'g'ri ravishda
    # `media_type` (IMAGE/VIDEO/CAROUSEL_ALBUM)ga qarab metrika tanlardi --
    # bu ikkisi BOSHQA-BOSHQA narsa (masalan oddiy FEED'ga joylangan VIDEO
    # bilan REELS'ga joylangan VIDEO uchun Meta turli metrikalarni
    # qo'llab-quvvatlaydi). Pastdagi `get_instagram_media_insights()` endi
    # to'g'ri `media_product_type`ga qarab ishlaydi.
    fields = "id,caption,timestamp,permalink,media_type,media_product_type,media_url,thumbnail_url,like_count,comments_count"
    data = _get(f"{ig_user_id}/media", {"fields": fields, "limit": limit}, token=_get_page_access_token(page_id, access_token))
    return data.get("data", [])


def get_instagram_media_insights(
    media_id: str, media_type: str = "IMAGE", media_product_type: str | None = None,
    *, page_id: str | None = None, access_token: str | None = None,
) -> dict:
    """Bitta Instagram post/media uchun qamrov (reach), ko'rishlar (views),
    saqlanganlar (saved), repost (shares) va postdan qo'shilgan yangi
    obunachilar (follows) sonini qaytaradi.

    MUHIM TUZATISH (2026-08, item 6 -- foydalanuvchi shikoyati: "SMM
    hisobotdagi malumotla notori, nechta like coment repost va nechta
    obunachi qo'shildi videodan aniq korsinsin"): bu funksiya AVVAL
    Meta tomonidan 2025-yil aprelidan BEKOR QILINGAN metrikalarni
    ("plays" -- video/reel uchun, "impressions" -- 2024-yil iyuldan
    keyin joylangan HAR QANDAY media uchun) so'rar edi. Meta Graph
    API'da ro'yxatdagi metrikalardan BITTASI ham noto'g'ri/bekor
    qilingan bo'lsa, BUTUN so'rov xato qaytaradi (qisman natija emas) --
    demak deyarli HAR BIR (ayniqsa 2024-yil iyuldan keyingi, ya'ni
    amalda HAMMA joriy) post uchun bu chaqiruv butunlay muvaffaqiyatsiz
    bo'lib, `except MetaAPIError: pass` orqali JIM yutilib ketgan, natijada
    reach/saqlangan/qamrov ko'rsatkichlari deyarli hech qachon
    to'lmagan. Bundan tashqari "shares" (repost) va "follows" (postdan
    kelgan yangi obunachi) metrikalari UMUMAN so'ralmagan edi.

    Metrikalar Meta'ning HAQIQIY qo'llab-quvvatlash jadvaliga mos ravishda
    `media_product_type`ga (media_type'ga EMAS) qarab tanlanadi:
      - REELS: reach, saved, shares, total_interactions, views ("follows"
        REELS uchun Meta tomonidan berilmaydi -- bu Meta'ning o'zining
        cheklovi, kod xatosi emas).
      - STORY: reach, shares, follows, total_interactions, views ("saved"
        tushunchasi Story uchun mavjud emas).
      - FEED (standart/boshqa hollarda ham shu): reach, saved, shares,
        follows, total_interactions, views.
    "impressions" ENDI hech qanday holatda so'ralmaydi (bekor qilingan);
    o'rniga zamonaviy "views" metrikasi ishlatiladi -- chaqiruvchi
    (`smm_sync.py`) buni bazaning `impressions` ustuniga yozadi.

    Chaqiruvchi (`smm_sync.py`) bu funksiyani har bir media uchun ALOHIDA,
    xatoni tutib chaqiradi -- bitta postning insights so'rovi
    muvaffaqiyatsiz bo'lsa ham, qolgan postlar sinxronlanishda davom
    etadi."""
    if media_product_type == "REELS":
        metrics = "reach,saved,shares,total_interactions,views"
    elif media_product_type == "STORY":
        metrics = "reach,shares,follows,total_interactions,views"
    else:  # FEED, "AD", yoki noma'lum/berilmagan -- eng keng tarqalgan holat
        metrics = "reach,saved,shares,follows,total_interactions,views"
    data = _get(f"{media_id}/insights", {"metric": metrics}, token=_get_page_access_token(page_id, access_token))
    out = {}
    for item in data.get("data", []):
        values = item.get("values") or []
        out[item.get("name")] = values[0].get("value") if values else None
    return out


# ---------------------------------------------------------------------------
# INSTAGRAM DIRECT (DM) -- 2026-08, foydalanuvchi so'rovi ("ig chatlarni
# tahlilini ham qoshish kerak"). Meta Instagram Messaging API orqali
# ishlaydi -- ODDIY SMM/Instant-Form funksiyalari kabi, ALLAQACHON sozlangan
# META_ACCESS_TOKEN + META_PAGE_ID yetarli (Page Access Token orqali).
#
# MUHIM (Meta ruxsat nozikligi -- foydalanuvchiga aniq tushuntirish kerak):
# xabarlarni O'QISH uchun `instagram_manage_messages` permission kerak.
# Bu ODDIY reklama/lead ruxsatlaridan (ads_management va h.k.) FARQLI
# o'laroq, System User token BIZNES-MENEJERNING O'Z Instagram akkaunti
# uchun bo'lsa ham, ba'zan alohida yoqilishi kerak bo'lishi mumkin:
#   - O'Z akkauntingiz uchun (bugungi holat -- yagona kompaniya): odatda
#     Meta App Dashboard -> App Roles -> "Instagram Testers" bo'limiga
#     o'zingizning Instagram Business akkauntingizni tester sifatida
#     qo'shish YETARLI -- to'liq ommaviy App Review SHART EMAS.
#   - Agar KELAJAKDA bu CRM boshqa mijozlarning (boshqa Business Manager
#     ostidagi) Instagram akkauntlariga ham ulanadigan bo'lsa (multi-tenant,
#     har bir mijoz o'z akkauntini ulaydi) -- O'SHANDA Meta'ning to'liq App
#     Review (Business Verification + screencast namoyishi) TALAB QILINADI.
# Bu funksiya ruxsat yo'qligida oddiy `MetaAPIError` ko'taradi (masalan
# "(#10) permission denied") -- chaqiruvchi (`ig_dm_sync.py`) buni tutib,
# aniq xabar bilan jim to'xtaydi (butun sinxronizatsiya jarayonini
# to'xtatmaydi).
# ---------------------------------------------------------------------------

def _is_reduce_data_error(e: "MetaAPIError") -> bool:
    """2026-09, JONLI XATO: `instagram_manage_messages` tuzatilgandan
    KEYIN chiqqan YANGI (BOSHQA) Meta xatosi -- "Please reduce the amount
    of data you're asking for, then retry your request". Bu ruxsat bilan
    ALOQASI YO'Q -- Meta so'ralgan `limit`/`fields` hajmi juda katta deb
    hisoblaganda (masalan, ko'p yillik DM tarixi bo'lgan faol akkaunt)
    qaytaradi. Meta'ning O'ZI aytganidek -- yechim shunchaki kamroq
    so'rash va qayta urinish."""
    meta_err = e.args[0] if e.args else {}
    message = meta_err.get("message", "") if isinstance(meta_err, dict) else str(e)
    return "reduce the amount of data" in message.lower()


def _is_timeout_error(e: "MetaAPIError") -> bool:
    """2026-09, JONLI XATO (foydalanuvchi topdi): "reduce the amount of
    data"dan ALOHIDA -- Meta ba'zan (odatda og'ir/uzoq NESTED so'rovlarda,
    masalan bitta suhbat obyekti ICHIDA `messages.limit(N){...}` orqali
    xabarlarni so'rashda) JSON xato tanasida to'g'ridan-to'g'ri
    `{"error": {"message": "Timeout", ...}}` qaytaradi -- bu ham
    `limit`ni kamaytirib qayta urinish bilan tuzatiladi (kamroq ma'lumot
    = serverda tezroq javob)."""
    meta_err = e.args[0] if e.args else {}
    message = meta_err.get("message", "") if isinstance(meta_err, dict) else str(e)
    return "timeout" in message.lower()


def _log_meta_error(endpoint: str, e: "MetaAPIError", *, elapsed: float, attempt: int) -> None:
    """Diagnostika logi -- 2026-09, foydalanuvchi so'rovi: "qaysi IG
    endpoint xato berdi, Meta error code, error_subcode, message, elapsed
    time". XAVFSIZLIK: bu yerga HECH QACHON `access_token`/to'liq so'rov
    URL'i (query string) chiqarilmaydi -- faqat Meta'ning JSON xato
    tanasidan olingan (token'siz) maydonlar."""
    meta_err = e.args[0] if e.args else {}
    if isinstance(meta_err, dict):
        code = meta_err.get("code")
        subcode = meta_err.get("error_subcode")
        message = meta_err.get("message", str(e))
    else:
        code, subcode, message = None, None, str(e)
    logger.warning(
        "Meta Graph API xatosi: endpoint=%s attempt=%s code=%s error_subcode=%s "
        "message=%r elapsed_ms=%d",
        endpoint, attempt, code, subcode, message, round(elapsed * 1000),
    )


# 2026-09 TUZATISH: qayta urinish endi BIR MARTA emas, shu qiymatga
# yetguncha DAVOM ETADI (pastga qarang) -- juda faol/uzoq tarixli
# akkauntlarda bitta yarmiga tushirish yetarli bo'lmagani jonli saytda
# kuzatilgan (foydalanuvchi xabari, 2026-09-11).
_MIN_LIMIT = 3


def get_instagram_conversations(
    limit: int = 5, *, page_id: str | None = None, access_token: str | None = None,
    after: str | None = None,
) -> tuple[list[dict], "str | None"]:
    """Page'ga (Instagram Business akkauntiga) kelgan DM suhbatlarning
    ro'yxatini qaytaradi: `(items, next_cursor)`. `items`da FAQAT `id` va
    `updated_time` bor -- ishtirokchilar (`participants`) VA xabarlar
    ALOHIDA, faqat kerak bo'lganda so'raladi (pastga qarang).

    2026-09 TUZATISH (foydalanuvchi topgan JONLI XATO -- oxirgi fix'dan
    KEYIN ham "Please reduce the amount of data" davom etdi): muammo
    aslida shu funksiyaning O'ZIDA edi -- bitta so'rovda `participants`ni
    ham (nested, potentsial og'ir) so'rash VA `since` filtri (Meta'ning
    o'zi buni qanchalik "arzon" hisoblashi noaniq) birgalikda Meta'ni
    "juda ko'p ma'lumot" deb hisoblashga majbur qilgan bo'lishi mumkin.
    Endi bu so'rov IMKON QADAR YENGIL:
      - `since` UMUMAN Meta'ga YUBORILMAYDI (pastga, `ig_dm_sync.py`dagi
        izohga qarang -- filtrlash endi FAQAT LOKAL, olingan
        `updated_time` bo'yicha).
      - `fields` FAQAT `id,updated_time` -- `participants` bu yerda
        SO'RALMAYDI (`get_instagram_conversation_participants()` orqali,
        har bir suhbat uchun ALOHIDA, faqat kerak bo'lganda).
      - Standart `limit` 5 (avval 10/50 edi).

    Agar shunga qaramay Meta "reduce the amount of data..." yoki
    "Timeout" bilan rad etsa -- BITTA qo'shimcha DIAGNOSTIK urinish
    qilinadi: `fields=id` (hatto `updated_time`siz), `limit=1` -- eng
    kichik mumkin bo'lgan so'rov. Agar SHU HAM rad etilsa, demak muammo
    endi `limit`/`fields` hajmida emas (masalan token/ruxsat/tarmoq) --
    xato ko'tariladi, `error_stage="conversations_list_minimal"` bilan
    belgilanadi (`e.stage` atributi orqali, `ig_dm_sync.py` shuni o'qib
    natijaga yozadi).

    `after` -- cursor-based pagination (`paging.cursors.after`) uchun;
    berilsa shu sahifadan boshlab so'raladi. ESLATMA: hozircha
    `ig_dm_sync.sync_once()` FAQAT BITTA sahifani (eng yangi `limit`
    ta suhbatni) so'raydi -- avtomatik keyingi sahifalarga o'tmaydi
    (eski suhbatlar kerak bo'lmagani uchun; `next_cursor` shunchaki
    KELAJAKDA to'liq/chuqur sinxronizatsiya kerak bo'lsa ishlatilishi
    uchun qaytariladi)."""
    resolved_page_id = page_id or PAGE_ID
    token = _get_page_access_token(page_id, access_token)
    endpoint = f"{resolved_page_id}/conversations"
    params = {"platform": "instagram", "fields": "id,updated_time", "limit": limit}
    if after:
        params["after"] = after

    attempt = 1
    started = time.monotonic()
    try:
        data = _get(endpoint, params, token=token)
    except MetaAPIError as e:
        _log_meta_error(endpoint, e, elapsed=time.monotonic() - started, attempt=attempt)
        if not (_is_reduce_data_error(e) or _is_timeout_error(e)):
            raise
        # Diagnostik ZAXIRA urinish -- eng kichik mumkin bo'lgan so'rov.
        minimal_params = {"platform": "instagram", "fields": "id", "limit": 1}
        attempt += 1
        started = time.monotonic()
        try:
            data = _get(endpoint, minimal_params, token=token)
        except MetaAPIError as e2:
            _log_meta_error(endpoint, e2, elapsed=time.monotonic() - started, attempt=attempt)
            e2.stage = "conversations_list_minimal"
            raise
    items = data.get("data", [])
    next_cursor = ((data.get("paging") or {}).get("cursors") or {}).get("after")
    return items, next_cursor


def get_instagram_conversation_participants(
    conversation_id: str, *, page_id: str | None = None, access_token: str | None = None,
) -> dict:
    """2026-09, foydalanuvchi so'rovi: suhbatlar RO'YXATI so'rovi endi
    `participants`ni o'z ichiga OLMAYDI (yuqoridagi
    `get_instagram_conversations()`ga qarang) -- shu maydon endi HAR BIR
    suhbat uchun ALOHIDA, ENG YENGIL mumkin bo'lgan so'rov bilan olinadi:
    `GET /{conversation_id}?fields=participants,updated_time`. Faqat
    HALI mijozi (`customer_ig_id`) bazada noma'lum bo'lgan (yangi)
    suhbatlar uchun chaqiriladi -- allaqachon ma'lum bo'lsa, bu so'rov
    UMUMAN qilinmaydi (`ig_dm_sync.py`dagi chaqiruvchiga qarang)."""
    token = _get_page_access_token(page_id, access_token)
    endpoint = conversation_id
    started = time.monotonic()
    try:
        return _get(endpoint, {"fields": "participants,updated_time"}, token=token)
    except MetaAPIError as e:
        _log_meta_error(endpoint, e, elapsed=time.monotonic() - started, attempt=1)
        raise


def get_instagram_conversation_messages(conversation_id: str, limit: int = 10, *, page_id: str | None = None, access_token: str | None = None) -> list[dict]:
    """Bitta suhbatning so'nggi xabarlarini (eng yangisi birinchi) qaytaradi:
    har birida `id`, `message` (matn), `created_time`, `from` (yuboruvchi
    IGSID/ism) bor.

    2026-09 TUZATISH (foydalanuvchi topgan JONLI XATO -- "Yangilash"
    tugmasi bosilganda UI'da "Meta error: Timeout" chiqardi): avval bu
    funksiya suhbat obyektining O'ZINI NESTED `fields=messages.limit(N)
    {...}` so'rovi bilan so'rardi (`GET /{conversation_id}?fields=
    messages.limit(40){...}`) -- Meta'da bunday ICHKI-EDGE expansion
    TO'G'RIDAN-TO'G'RI `/{conversation_id}/messages` edge'iga qaraganda
    SEKINROQ/OG'IRROQ ishlanadi va faol suhbatlarda server-tomon
    "Timeout" xatosi bilan tugashi kuzatilgan. Endi to'g'ridan-to'g'ri
    xabarlar edge'i ishlatiladi (`GET /{conversation_id}/messages
    ?fields=id,message,created_time,from,to&limit=N`) -- standart limit
    ham 40'dan 10'ga tushirildi.

    Ikki xil xato holatida ham (Meta "reduce the amount of data..." YOKI
    "Timeout" qaytarsa) `limit` HAR SAFAR yarmiga tushirilib (10 -> 5 ->
    3), minimal qiymatga (`_MIN_LIMIT`) yetguncha QAYTA-QAYTA avtomatik
    uriniladi -- `get_instagram_conversations()`dagi bilan bir xil naqsh.
    Har bir muvaffaqiyatsiz urinish diagnostika uchun logga yoziladi
    (`_log_meta_error` -- access_token HECH QACHON logga chiqmaydi)."""
    token = _get_page_access_token(page_id, access_token)
    current_limit = limit
    endpoint = f"{conversation_id}/messages"
    attempt = 0
    while True:
        attempt += 1
        started = time.monotonic()
        try:
            data = _get(endpoint, {
                "fields": "id,message,created_time,from,to",
                "limit": current_limit,
            }, token=token)
            return data.get("data", [])
        except MetaAPIError as e:
            _log_meta_error(endpoint, e, elapsed=time.monotonic() - started, attempt=attempt)
            if (_is_reduce_data_error(e) or _is_timeout_error(e)) and current_limit > _MIN_LIMIT:
                current_limit = max(_MIN_LIMIT, current_limit // 2)
                continue
            raise


def send_instagram_message(recipient_ig_id: str, text: str, *, page_id: str | None = None, access_token: str | None = None) -> dict:
    """Instagram Direct'ga menejer ilova ICHIDAN yozgan javobni yuboradi
    (2026-09, foydalanuvchi so'rovi: "habarlar joyida ... manager jovob
    berolidigan qilishim kerak" -- avval bu modul FAQAT o'qir edi). Meta'ning
    "Send API"si -- Messenger bilan bir xil endpoint (`/{page_id}/messages`),
    IGSID orqali qabul qiluvchi avtomatik Instagram deb aniqlanadi, alohida
    `platform` parametri shart emas.

    MUHIM: Meta "24 soatlik javob berish oynasi" siyosatini qo'llaydi --
    mijoz OXIRGI xabar yuborganidan 24 soatdan ko'p vaqt o'tgan bo'lsa,
    oddiy matn javobi RAD ETILADI. BU KOD XATOSI EMAS -- Meta'ning barcha
    Instagram/Messenger biznes akkauntlari uchun majburiy (spam'dan himoya)
    siyosati; chaqiruvchi (`app.py`) bu xatoni `ig_dm_sync.friendly_send_error()`
    orqali tushunarli qilib ko'rsatadi."""
    resolved_page_id = page_id or PAGE_ID
    payload = {
        "recipient": {"id": recipient_ig_id},
        "message": {"text": text},
        "messaging_type": "RESPONSE",
    }
    return _post(f"{resolved_page_id}/messages", payload, token=_get_page_access_token(page_id, access_token))


# ---------------------------------------------------------------------------
# Webhook (real-time) -- 2026-09, foydalanuvchi so'rovi (item 9): "yangi
# Instagram DM kelganda Meta webhook orqali DBga yozilsin. Polling/Yangilash
# faqat fallback va initial sync bo'lsin." QAT'IY TALAB QILINADIGAN QO'LDA
# QADAM (Meta App Dashboard -> Webhooks -> Instagram bo'limida): Callback
# URL ("https://<domen>/webhooks/instagram") va Verify Token (yuqoridagi
# `META_WEBHOOK_VERIFY_TOKEN` bilan BIR XIL qiymat) kiritilishi va
# "messages" maydoni obuna qilinishi kerak -- bu Meta'ning O'ZI talab
# qiladigan, kod orqali AVTOMATLASHTIRIB BO'LMAYDIGAN qadam. Quyidagi
# funksiya esa AVTOMATLASHTIRISH MUMKIN bo'lgan ikkinchi qadamni bajaradi:
# har bir Page'ni ilovaning webhook'iga OBUNA QILISH (`subscribed_apps`).
# ---------------------------------------------------------------------------

def subscribe_page_to_messaging_webhook(page_id: str, access_token: str) -> dict:
    """`POST /{page_id}/subscribed_apps?subscribed_fields=messages` --
    shu Page'ni ilovaning (App Dashboard'da sozlangan) webhook'iga
    "messages" hodisalari uchun obuna qiladi. Kompaniya Facebook orqali
    (qayta) ulanganda `app.py`dagi `_save_facebook_connection()`dan
    BEST-EFFORT (xato bo'lsa asosiy ulanishni TO'XTATMAYDIGAN) tarzda
    chaqiriladi -- App-darajasidagi Callback URL/Verify Token sozlamasi
    (yuqoridagi izohga qarang) BU FUNKSIYADAN OLDIN, Meta App
    Dashboard'da QO'LDA bir marta qilingan bo'lishi SHART, aks holda
    Meta bu so'rovni "App-level webhook sozlanmagan" xatosi bilan rad
    etadi."""
    return _post(
        f"{page_id}/subscribed_apps",
        {"subscribed_fields": "messages"},
        token=access_token,
    )


def verify_webhook_signature(payload_body: bytes, signature_header: "str | None") -> bool:
    """Meta'dan kelgan webhook POST so'rovining haqiqiyligini tekshiradi:
    `X-Hub-Signature-256` header'i (`"sha256=<hex>"` formatida) `payload_
    body`ning `META_APP_SECRET` kaliti bilan hisoblangan HMAC-SHA256
    imzosiga TENG bo'lishi kerak. Vaqt-hujumidan (timing attack) himoya
    uchun `hmac.compare_digest` ishlatiladi (oddiy `==` EMAS). `app.py`
    ushbu tekshiruvdan O'TMAGAN so'rovlarni RAD ETADI (401) -- aks holda
    ISTALGAN kishi soxta "yangi xabar" yuborib, bazaga yolg'on yozuv
    kiritishi mumkin edi."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    if not META_APP_SECRET:
        return False
    expected = hmac.new(META_APP_SECRET.encode("utf-8"), payload_body, hashlib.sha256).hexdigest()
    provided = signature_header.split("=", 1)[1]
    return hmac.compare_digest(expected, provided)


# ---------------------------------------------------------------------------
# META ADS AUTOPILOT (2026-09, foydalanuvchi so'rovi: "Replix ... Ads
# Manager'dagi har bir maydonni boshidan o'zi to'ldirmasligi kerak").
# Kampaniya qoralamasini (`campaign_draft.py`) Meta'ga chiqarish
# (`meta_publish.py`) uchun kerak bo'lgan QO'SHIMCHA endpoint'lar. Hammasi
# `access_token` (kompaniyaning O'Z tokeni) bilan ishlaydi -- global ENV
# tokeniga tayanmaydi. Meta v21 hujjati bo'yicha yozilgan; rad etilsa
# `meta_publish.friendly_publish_error()` foydalanuvchiga tushunarli xato
# ko'rsatadi (xom API matni ekranga chiqmaydi).
# ---------------------------------------------------------------------------

_MAX_VIDEO_UPLOAD_BYTES = 50 * 1024 * 1024  # bitta so'rovli yuklash chegarasi (chunked yuklash amalga oshirilmagan)

AD_PREVIEW_FORMATS = {
    "facebook_feed": "DESKTOP_FEED_STANDARD",
    "facebook_mobile": "MOBILE_FEED_STANDARD",
    "instagram_feed": "INSTAGRAM_STANDARD",
    "instagram_story": "INSTAGRAM_STORY",
    "instagram_reels": "INSTAGRAM_REELS",
}


def get_ad_account_info(ad_account_id: str, *, access_token: str) -> dict:
    """Reklama hisobining valyutasi/vaqt zonasi/holati -- byudjetni to'g'ri
    kichik birlikka o'tkazish (`campaign_draft.to_minor_units`) va UI'da
    ko'rsatish uchun."""
    data = _get(ad_account_id, {"fields": "name,currency,timezone_name,account_status,spend_cap,amount_spent"}, token=access_token)
    return {
        "id": data.get("id") or ad_account_id,
        "name": data.get("name"),
        "currency": data.get("currency"),
        "timezone_name": data.get("timezone_name"),
        "account_status": data.get("account_status"),
        "spend_cap": data.get("spend_cap"),
        "amount_spent": data.get("amount_spent"),
    }


def list_custom_audiences(ad_account_id: str, *, access_token: str, limit: int = 100) -> list[dict]:
    """Reklama hisobidagi Custom/Lookalike auditoriyalar -- qoralamada
    tanlash uchun (egalik tekshiruvi ham shu ro'yxat bo'yicha)."""
    data = _get(f"{ad_account_id}/customaudiences", {"fields": "id,name,subtype,approximate_count_lower_bound", "limit": limit}, token=access_token)
    return [
        {"id": a.get("id"), "name": a.get("name"), "subtype": a.get("subtype"),
         "approximate_count_lower_bound": a.get("approximate_count_lower_bound")}
        for a in data.get("data", [])
    ]


def list_ad_images(ad_account_id: str, *, access_token: str, limit: int = 50) -> list[dict]:
    """Hisobga ilgari yuklangan rasmlar (hash bilan) -- qayta yuklamasdan
    tanlash uchun."""
    data = _get(f"{ad_account_id}/adimages", {"fields": "hash,name,url,width,height,created_time", "limit": limit}, token=access_token)
    return data.get("data", [])


def upload_ad_image(ad_account_id: str, filename: str, file_bytes: bytes, *, access_token: str) -> dict:
    """Rasmni `act_x/adimages`ga yuklaydi, {"hash","url"} qaytaradi.
    Meta javobi: {"images": {"<filename>": {"hash": ..., "url": ...}}}."""
    result = _post_multipart(f"{ad_account_id}/adimages", {}, {"filename": (filename, file_bytes, "application/octet-stream")}, token=access_token)
    images = result.get("images") if isinstance(result, dict) else None
    if not isinstance(images, dict) or not images:
        raise MetaAPIError({"message": "Rasm yuklandi, lekin Meta hash qaytarmadi."})
    entry = images.get(filename) or next(iter(images.values()))
    if not isinstance(entry, dict) or not entry.get("hash"):
        raise MetaAPIError({"message": "Rasm yuklandi, lekin Meta hash qaytarmadi."})
    return {"hash": entry["hash"], "url": entry.get("url")}


def upload_ad_video(ad_account_id: str, filename: str, file_bytes: bytes, *, access_token: str) -> dict:
    """Videoni `act_x/advideos`ga BITTA so'rov bilan (`source`) yuklaydi va
    {"id"} qaytaradi. Bo'laklab (chunked/resumable) yuklash AMALGA
    OSHIRILMAGAN -- 50 MB dan katta fayl uchun friendly xato."""
    if len(file_bytes) > _MAX_VIDEO_UPLOAD_BYTES:
        raise MetaAPIError({"message": "Video 50 MB dan katta -- hozircha faqat 50 MB gacha video yuklash mumkin. Faylni siqib qayta yuklang."})
    result = _post_multipart(f"{ad_account_id}/advideos", {"name": filename}, {"source": (filename, file_bytes, "application/octet-stream")}, token=access_token)
    if not isinstance(result, dict) or not result.get("id"):
        raise MetaAPIError({"message": "Video yuklandi, lekin Meta ID qaytarmadi."})
    return {"id": str(result["id"])}


def search_targeting_interests(query: str, *, access_token: str, limit: int = 10) -> list[dict]:
    """Qiziqish (interest) NOMINI Meta'ning haqiqiy targeting ID'siga
    bog'laydi (`GET /search?type=adinterest`). AI hech qachon ID
    o'ylab topmaydi -- faqat shu natijadan tanlanadi."""
    data = _get("search", {"type": "adinterest", "q": query, "limit": limit}, token=access_token)
    return [
        {"id": i.get("id"), "name": i.get("name"), "audience_size_lower_bound": i.get("audience_size_lower_bound"), "path": i.get("path")}
        for i in data.get("data", []) if i.get("id")
    ]


def generate_ad_preview(ad_account_id: str, object_story_spec: dict, ad_format: str, *, access_token: str) -> str:
    """Kreativ hali yaratilmagan bo'lsa ham reklama ko'rinishini (iframe
    HTML) qaytaradi -- `act_x/generatepreviews`. `ad_format` --
    `AD_PREVIEW_FORMATS` qiymatlaridan. Meta v21 hujjati bo'yicha; rad
    etilsa friendly xato ko'rsatiladi."""
    data = _get(f"{ad_account_id}/generatepreviews", {
        "creative": {"object_story_spec": object_story_spec},
        "ad_format": ad_format,
    }, token=access_token)
    items = data.get("data") or []
    if not items or not isinstance(items[0], dict):
        raise MetaAPIError({"message": "Meta preview qaytarmadi."})
    return items[0].get("body") or ""


def create_ad_creative(ad_account_id: str, name: str, object_story_spec: dict, *, access_token: str) -> dict:
    """Yangi AdCreative (`act_x/adcreatives`) -- {"id"} qaytaradi.
    `object_story_spec` `campaign_draft.to_meta_creative_spec()`dan keladi."""
    result = _post(f"{ad_account_id}/adcreatives", {"name": name, "object_story_spec": object_story_spec}, token=access_token)
    return {"id": str(result.get("id"))} if result.get("id") else result


def get_campaign_tree(campaign_id: str, *, access_token: str) -> dict:
    """Import uchun: kampaniya + uning adset'lari + reklamalari (kreativ
    spec bilan) BITTA so'rovda (nested fields)."""
    fields = (
        "id,name,objective,status,special_ad_categories,daily_budget,lifetime_budget,bid_strategy,buying_type,"
        "adsets.limit(25){id,name,status,daily_budget,lifetime_budget,optimization_goal,billing_event,bid_strategy,"
        "destination_type,promoted_object,targeting,start_time,end_time,"
        "ads.limit(25){id,name,status,creative{id,object_story_spec}}}"
    )
    data = _get(campaign_id, {"fields": fields}, token=access_token)
    adsets = data.get("adsets")
    if isinstance(adsets, dict):
        data["adsets"] = adsets.get("data", [])
    for adset in data.get("adsets") or []:
        ads = adset.get("ads")
        if isinstance(ads, dict):
            adset["ads"] = ads.get("data", [])
    return data


def get_campaign_basic(campaign_id: str, *, access_token: str) -> dict:
    """Nashrdan keyingi tekshiruv/sinxronizatsiya uchun qisqa holat."""
    return _get(campaign_id, {"fields": "id,name,status,effective_status,objective,updated_time"}, token=access_token)


def get_adset_basic(adset_id: str, *, access_token: str) -> dict:
    return _get(adset_id, {"fields": "id,name,status,effective_status,daily_budget,lifetime_budget,targeting,start_time,end_time,optimization_goal,updated_time"}, token=access_token)


def get_ad_basic(ad_id: str, *, access_token: str) -> dict:
    return _get(ad_id, {"fields": "id,name,status,effective_status,creative{id},updated_time"}, token=access_token)


_UPDATABLE_FIELDS = {"name", "status", "daily_budget", "lifetime_budget", "end_time", "targeting", "bid_strategy"}


def _update_object(object_id: str, fields: dict, *, access_token: str, allowed: set) -> dict:
    payload = {k: v for k, v in (fields or {}).items() if k in allowed}
    if not payload:
        raise MetaAPIError({"message": "Yangilash uchun ruxsat etilgan maydon yo'q."})
    if "targeting" in payload and isinstance(payload["targeting"], dict):
        payload["targeting"] = _sanitize_targeting_for_write(payload["targeting"])
    return _post(object_id, payload, token=access_token)


def update_campaign(campaign_id: str, fields: dict, *, access_token: str) -> dict:
    """Kampaniyaning FAQAT ruxsat etilgan maydonlarini (name, status,
    daily/lifetime_budget, bid_strategy) yangilaydi."""
    return _update_object(campaign_id, fields, access_token=access_token, allowed={"name", "status", "daily_budget", "lifetime_budget", "bid_strategy"})


def update_adset(adset_id: str, fields: dict, *, access_token: str) -> dict:
    """Ad Set'ning ruxsat etilgan maydonlari: name, status, daily_budget,
    lifetime_budget, end_time, targeting, bid_strategy."""
    return _update_object(adset_id, fields, access_token=access_token, allowed=_UPDATABLE_FIELDS)


def update_ad(ad_id: str, fields: dict, *, access_token: str) -> dict:
    """Reklamaning ruxsat etilgan maydonlari: name, status (kreativ
    `update_ad_creative()` orqali -- kreativlar immutable)."""
    return _update_object(ad_id, fields, access_token=access_token, allowed={"name", "status"})


def list_ad_account_campaigns(ad_account_id: str, *, access_token: str, limit: int = 50) -> list[dict]:
    """Import tanlovi uchun hisobdagi kampaniyalar ro'yxati."""
    data = _get(f"{ad_account_id}/campaigns", {"fields": "id,name,objective,status,updated_time", "limit": limit}, token=access_token)
    return data.get("data", [])
