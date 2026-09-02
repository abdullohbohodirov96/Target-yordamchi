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
  - Xatoliklarni qayta urinish (retry/backoff) mexanizmini kuchaytiring.
  - Rate limit (Meta har soatlik so'rov limiti bor) monitoringini qo'shing.
  - Har bir yozish amalini (pause/budget) alohida audit-log'ga yozing.
"""

import os
import re
import json
import time
import hashlib
import concurrent.futures
import requests

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


def _get(path: str, params: dict | None = None, token: str | None = None) -> dict:
    params = {
        k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
        for k, v in (params or {}).items()
    }
    params["access_token"] = token or ACCESS_TOKEN
    r = requests.get(f"{GRAPH_URL}/{path}", params=params, timeout=30)
    data = r.json()
    if "error" in data:
        raise MetaAPIError(data["error"])
    return data


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
    r = requests.post(f"{GRAPH_URL}/{path}", data=payload, timeout=30)
    result = r.json()
    if isinstance(result, dict) and "error" in result:
        raise MetaAPIError(result["error"])
    return result


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
) -> dict | None:
    """Bitta hodisani (masalan "QualifiedLead" yoki "Purchase") Meta
    Conversions API'ga yuboradi.

    `pixel_id`/`access_token` -- 2026-09 multi-tenant: berilsa, O'SHA
    kompaniyaning o'z Pixel'i/tokeni ishlatiladi. Ikkalasi ham berilmasa --
    eski global ENV (`PIXEL_ID`/`ACCESS_TOKEN`) ishlatiladi.

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

    if not user_data:
        return None  # moslashtiradigan hech narsa yo'q -- yuborishning ma'nosi yo'q

    event = {
        "event_name": event_name,
        "event_time": int(time.time()),
        "action_source": "system_generated",
        "user_data": user_data,
    }
    if event_id:
        event["event_id"] = event_id
    if value is not None:
        event["custom_data"] = {"value": round(float(value), 2), "currency": currency}

    return _post(f"{resolved_pixel_id}/events", {"data": [event]}, token=resolved_token)


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
_page_token_cache: dict[str, str] = {}


def _get_page_access_token(page_id: str | None = None, user_access_token: str | None = None) -> str:
    """`page_id`/`user_access_token` berilsa -- O'SHA (kompaniyaning o'zi
    ulagan) Page/token uchun Page Access Token oladi. Ikkalasi ham
    berilmasa -- eski global (ENV) `PAGE_ID`/`ACCESS_TOKEN` ishlatiladi
    (orqaga moslik: CLI skript yoki hali company-parametrsiz chaqiruvlar)."""
    resolved_page_id = page_id or PAGE_ID
    resolved_user_token = user_access_token or ACCESS_TOKEN
    if resolved_page_id in _page_token_cache:
        return _page_token_cache[resolved_page_id]
    if not resolved_page_id:
        raise MetaAPIError({"message": "Page ID sozlanmagan -- Page Access Token olib bo'lmaydi."})
    r = requests.get(
        f"{GRAPH_URL}/{resolved_page_id}",
        params={"fields": "access_token", "access_token": resolved_user_token},
        timeout=30,
    )
    data = r.json()
    if "error" in data:
        raise MetaAPIError(data["error"])
    token = data.get("access_token")
    if not token:
        raise MetaAPIError({
            "message": (
                "Page Access Token olinmadi -- ulangan token shu Page'ga "
                "(Business Manager -> Sahifalar) administrator/muharrir sifatida "
                "ulanganini tekshiring."
            )
        })
    _page_token_cache[resolved_page_id] = token
    return token


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
) -> list[dict]:
    """`get_insights()` bilan bir xil, lekin video/engagement metrikalarini ham
    qo'shib qaytaradi. Foydalanuvchi "video necha % odam ko'rgan", "hook rate
    qancha", yoki aniq bir kun/oraliq ("20 iyul", "1-10 avgust") so'raganda
    ishlatiladi (orchestrator.answer_data_question)."""
    return get_insights(
        level=level, date_preset=date_preset, breakdowns=breakdowns,
        fields=FULL_REPORTING_FIELDS, time_range=time_range,
    )


def get_active_ads(adset_id: str | None = None) -> list[dict]:
    path = f"{adset_id}/ads" if adset_id else f"{AD_ACCOUNT_ID}/ads"
    data = _get(path, {"fields": "id,name,status,adset_id,campaign_id", "limit": 200})
    return data.get("data", [])


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
        campaigns_future = pool.submit(_get, f"{acct}/campaigns", campaign_params, access_token)
        adsets_future = pool.submit(_get, f"{acct}/adsets", adset_params, access_token)
        ads_future = pool.submit(_get, f"{acct}/ads", ad_params, access_token)
        campaigns = campaigns_future.result().get("data", [])
        adsets = adsets_future.result().get("data", [])
        ads = ads_future.result().get("data", [])
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
         qaytarib qo'shishi mumkin."""
    from urllib.parse import urlencode

    scopes = ["pages_show_list", "pages_read_engagement", "instagram_basic"]
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


def oauth_exchange_long_lived(short_token: str) -> str:
    """QISQA muddatli (~1-2 soatlik) tokenni ~60 kunlik UZOQ muddatli
    tokenga almashtiradi -- shu token Company.meta_access_token'da
    saqlanadi (xuddi qo'lda kiritilgan token kabi)."""
    r = requests.get(f"{GRAPH_URL}/oauth/access_token", params={
        "grant_type": "fb_exchange_token", "client_id": META_APP_ID,
        "client_secret": META_APP_SECRET, "fb_exchange_token": short_token,
    }, timeout=30)
    data = r.json()
    if "error" in data:
        raise MetaAPIError(data["error"])
    return data["access_token"]


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


def get_object_status(object_id: str) -> dict:
    """Ad/AdSet/Campaign'ning joriy holatini (status) qaytaradi. pause_object()/
    activate_object() dan keyin haqiqatan o'zgarganini TASDIQLASH uchun ishlatiladi
    — Meta ba'zan {"success": true} qaytarsa ham, holat kutilganidek o'zgarmagan
    bo'lishi mumkin (masalan yuqori darajadagi kampaniya/adset o'chiq bo'lsa)."""
    return _get(object_id, {"fields": "id,name,status,effective_status"})


def get_adset_details(adset_id: str) -> dict:
    """Bitta adset'ning to'liq sozlamalarini (targeting, byudjet va h.k.) qaytaradi.
    Targetolog `account_structure`dan kerakli adset'ni nom bo'yicha topgach, aynan
    o'sha bitta adset uchun bu funksiya chaqiriladi — barcha adsetlarning
    targeting'ini birdaniga yubormaslik uchun (token limitidan oshib ketmasligi uchun)."""
    return _get(adset_id, {"fields": "id,name,status,campaign_id,daily_budget,targeting,optimization_goal"})


# ---------------------------------------------------------------------------
# ON/OFF VA BYUDJET BOSHQARUVI
# ---------------------------------------------------------------------------

def set_status(object_id: str, status: str) -> dict:
    """Ad/AdSet/Campaign holatini o'rnatadi (ACTIVE / PAUSED / ARCHIVED).
    pause_object/activate_object shu funksiyaning qulay wrapperlari."""
    return _post(object_id, {"status": status})


def pause_object(object_id: str) -> dict:
    """Ad, AdSet yoki Campaign'ni pauza qiladi."""
    return set_status(object_id, "PAUSED")


def activate_object(object_id: str) -> dict:
    """Ad, AdSet yoki Campaign'ni qayta ishga tushiradi."""
    return set_status(object_id, "ACTIVE")


def archive_object(object_id: str) -> dict:
    """Kerak bo'lmay qolgan (uzoq vaqt pauzada, kelajakda ishlatilmaydigan)
    kampaniya/adset'ni arxivlaydi — o'chirib tashlash (DELETED) emas, shuning
    uchun kerak bo'lsa Ads Manager'da qaytarib bo'ladi, lekin ro'yxatlarni
    "toza" qiladi."""
    return set_status(object_id, "ARCHIVED")


def update_daily_budget(adset_id: str, new_daily_budget_cents: int) -> dict:
    """Byudjet Meta API'da eng kichik valyuta birligida (masalan tiyin/cent)
    beriladi. Masalan $10.00 -> 1000."""
    return _post(adset_id, {"daily_budget": new_daily_budget_cents})


def adjust_budget_by_percent(adset_id: str, current_daily_budget_cents: int, percent: float) -> dict:
    """4.4-bo'lim qoidasiga ko'ra: bir martada 10-20% oralig'ida o'zgartirish
    tavsiya etiladi. `percent` musbat (oshirish) yoki manfiy (kamaytirish)."""
    new_budget = int(current_daily_budget_cents * (1 + percent / 100))
    return update_daily_budget(adset_id, new_budget)


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


def set_location_current_city_only(adset_id: str, city_key: str) -> dict:
    """Ad Set targeting'ini faqat joriy shaharga cheklaydi va avtokengaytirishni
    o'chiradi ("Reach more people likely to respond" -> off)."""
    targeting = {
        "geo_locations": {
            "cities": [{"key": city_key, "radius": 0, "distance_unit": "kilometer"}],
            "location_types": ["home"],  # faqat shu shaharda yashovchilar
        },
        "targeting_automation": {"advantage_audience": 0},  # auto-expansion off
    }
    return _post(adset_id, {"targeting": _sanitize_targeting_for_write(targeting)})


def update_targeting(adset_id: str, targeting: dict) -> dict:
    """Ad Set auditoriyasini to'liq yangi targeting spec bilan almashtiradi.
    Yozishdan oldin avtomatik ravishda xavfsizlashtiriladi (`_sanitize_targeting_for_write`)."""
    return _post(adset_id, {"targeting": _sanitize_targeting_for_write(targeting)})


def search_geo_location(query: str, location_types: list[str] | None = None) -> list[dict]:
    """Erkin matndagi joy nomini (masalan 'Chirchiq', 'Zangiota tumani') Meta'ning
    rasmiy geo-target kaliti va turiga bog'laydi. Bir nechta nomzod qaytishi mumkin
    (bir xil nomli joylar turli davlatlarda bo'lishi mumkin) — Targetolog davlat/
    kontekstga qarab eng mosini tanlashi kerak. Natija elementlari odatda:
    {"key": "...", "name": "...", "type": "city"|"region"|"country"|..., "country_code": "UZ", ...}
    Bu funksiyasiz shahar/tuman nomlarini exclude/include qilib bo'lmaydi — Meta
    faqat raqamli `key` bilan ishlaydi, nom bilan emas."""
    params = {"type": "adgeolocation", "q": query}
    if location_types:
        params["location_types"] = location_types
    data = _get("search", params)
    return data.get("data", [])


# ---------------------------------------------------------------------------
# YANGI KAMPANIYA/ADSET/AD YARATISH (targetni "o'zi to'liq yoqishi" uchun)
# ---------------------------------------------------------------------------

def create_campaign(
    name: str,
    objective: str = "OUTCOME_LEADS",   # OUTCOME_LEADS | OUTCOME_SALES | OUTCOME_ENGAGEMENT | OUTCOME_TRAFFIC
    status: str = "PAUSED",
    special_ad_categories: list | None = None,
) -> dict:
    return _post(f"{AD_ACCOUNT_ID}/campaigns", {
        "name": name,
        "objective": objective,
        "status": status,
        "special_ad_categories": special_ad_categories or [],
    })


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
) -> dict:
    """Bo'lim 4.2-4.3 qoidalariga mos targeting spec bilan yangi Ad Set yaratadi.

    `targeting` namunasi (broad, faqat yosh/jins/hudud — 4.2-bo'lim tavsiyasiga ko'ra):
    {
        "geo_locations": {"cities": [{"key": "2430536", "radius": 0, "distance_unit": "kilometer"}]},
        "age_min": 18, "age_max": 65,
        "targeting_automation": {"advantage_audience": 1}
    }
    """
    payload = {
        "name": name,
        "campaign_id": campaign_id,
        "daily_budget": daily_budget_cents,
        "targeting": targeting,
        "optimization_goal": optimization_goal,
        "billing_event": billing_event,
        "bid_strategy": bid_strategy,
        "status": status,
    }
    if promoted_object:
        payload["promoted_object"] = promoted_object
    return _post(f"{AD_ACCOUNT_ID}/adsets", payload)


def create_ad(adset_id: str, name: str, creative_id: str, status: str = "PAUSED") -> dict:
    """Mavjud creative_id'dan foydalanib reklama yaratadi. AI video/rasm generatsiya
    qila olmaydi — creative_id avvaldan Ads Manager'da yuklangan bo'lishi kerak."""
    return _post(f"{AD_ACCOUNT_ID}/ads", {
        "name": name,
        "adset_id": adset_id,
        "creative": {"creative_id": creative_id},
        "status": status,
    })


# ---------------------------------------------------------------------------
# A/B TEST (Meta'ning native "copies" funksiyasi orqali)
# ---------------------------------------------------------------------------

def copy_adset(adset_id: str, rename_suffix: str = " - B variant", status_option: str = "PAUSED") -> dict:
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
    })


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

def get_ad_creative_details(ad_id: str) -> dict:
    """Reklamaning joriy kreativini (matn + rasm/video) qaytaradi.
    `replace_creative` uchun MUHIM: yangi creative yaratishdan oldin joriy
    `object_story_spec`ning AYNAN NUSXASIDAN boshlash kerak (noldan qurish
    EMAS) -- aks holda rasm/video yo'qolib ketishi yoki Meta "invalid
    creative" xatosi berishi mumkin."""
    data = _get(ad_id, {"fields": "id,name,adset_id,creative{id,object_story_spec,image_hash,video_id}"})
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


def create_ad_creative_with_new_copy(
    page_id: str,
    base_story_spec: dict,
    primary_text: str,
    headline: str | None = None,
    name: str | None = None,
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
    return _post(f"{AD_ACCOUNT_ID}/adcreatives", payload)


def update_ad_creative(ad_id: str, creative_id: str) -> dict:
    """Mavjud reklamaga YANGI creative'ni biriktiradi (eskisi endi
    ko'rsatilmaydi, lekin arxivda saqlanib qoladi). Reklamaning o'zi (ad_id,
    demak statistika tarixi) o'zgarmaydi."""
    return _post(ad_id, {"creative": {"creative_id": creative_id}})


# ---------------------------------------------------------------------------
# INSTANT FORMS / LEAD ADS (4.9-bo'lim)
# ---------------------------------------------------------------------------

def create_lead_form(page_id: str, form_config: dict) -> dict:
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
    """
    return _post(f"{page_id}/leadgen_forms", form_config, token=_get_page_access_token())


def get_leads(form_id: str, since: str | None = None) -> list[dict]:
    """Formadan tushgan lidlarni qaytaradi (leads_retrieval permission talab
    qilinadi). CRM lead-sync job'i (`lead_sync.py`) shu funksiyani har bir
    forma uchun muntazam chaqirib, yangi lidlarni bazaga yozadi -- shuning
    uchun attributsiya uchun kerakli maydonlar ham so'raladi (`ad_id`,
    `adset_id`, `campaign_id`), dashboard'da "qaysi kampaniyadan nechta lead
    kelgani"ni ko'rsatish uchun.

    `since` berilsa (ISO sana yoki unix timestamp), faqat shu sanadan keyingi
    lidlar so'raladi -- har safar BARCHA tarixni qayta o'qimaslik uchun."""
    params = {
        "fields": "id,created_time,ad_id,adset_id,campaign_id,form_id,field_data",
        "limit": 100,
    }
    if since:
        params["filtering"] = [{"field": "time_created", "operator": "GREATER_THAN", "value": since}]
    data = _get(f"{form_id}/leads", params, token=_get_page_access_token())
    leads = list(data.get("data", []))
    # Sahifalash (pagination) -- forma bo'yicha 100 dan ko'p yangi lead
    # bo'lishi kamdan-kam, lekin xavfsizlik uchun keyingi sahifalarni ham olamiz.
    while data.get("paging", {}).get("next"):
        next_url = data["paging"]["next"]
        r = requests.get(next_url, timeout=30)
        data = r.json()
        if "error" in data:
            break
        leads.extend(data.get("data", []))
    return leads


def get_lead_forms(page_id: str) -> list[dict]:
    """Sahifaga (Page) tegishli BARCHA Instant Form (Lead Ads) formalarini
    qaytaradi -- CRM lead-sync job'i har bir forma bo'yicha `get_leads()`ni
    alohida chaqiradi (Meta API'da "hisobdagi barcha lidlar" degan yagona
    endpoint yo'q, forma orqali so'raladi)."""
    data = _get(f"{page_id}/leadgen_forms", {"fields": "id,name,status,leads_count", "limit": 200}, token=_get_page_access_token())
    return data.get("data", [])


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
    data = _get("ads_archive", {
        "search_terms": search_terms,
        "ad_reached_countries": list(countries),
        "ad_active_status": "ALL",
        "ad_type": "ALL",
        "limit": limit,
        "fields": "id,ad_snapshot_url,page_name,ad_creative_bodies,ad_creative_link_titles,ad_delivery_start_time,ad_delivery_stop_time",
    })
    return data.get("data", [])


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

def get_instagram_conversations(limit: int = 50, *, page_id: str | None = None, access_token: str | None = None) -> list[dict]:
    """Page'ga (Instagram Business akkauntiga) kelgan DM suhbatlarning
    ro'yxatini qaytaradi (eng oxirgi yangilangandan boshlab).
    Ishtirokchilarning IGSID/username'i shu yerda keladi, lekin xabarlar
    matni EMAS -- ular alohida `get_instagram_conversation_messages()`
    orqali so'raladi (Meta shunday ikki bosqichli ishlaydi)."""
    resolved_page_id = page_id or PAGE_ID
    data = _get(f"{resolved_page_id}/conversations", {
        "platform": "instagram",
        "fields": "id,updated_time,participants",
        "limit": limit,
    }, token=_get_page_access_token(page_id, access_token))
    return data.get("data", [])


def get_instagram_conversation_messages(conversation_id: str, limit: int = 40, *, page_id: str | None = None, access_token: str | None = None) -> list[dict]:
    """Bitta suhbatning so'nggi xabarlarini (eng yangisi birinchi) qaytaradi:
    har birida `id`, `message` (matn), `created_time`, `from` (yuboruvchi
    IGSID/ism) bor."""
    data = _get(conversation_id, {
        "fields": f"messages.limit({limit}){{id,message,created_time,from,to}}",
    }, token=_get_page_access_token(page_id, access_token))
    return ((data.get("messages") or {}).get("data")) or []
