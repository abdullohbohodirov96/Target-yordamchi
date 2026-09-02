"""
smm_sync.py — Instagram Business va Facebook Page uchun "organik" (pullik
reklamadan tashqari) SMM statistikasini muntazam Meta Graph API'dan tortib,
Postgres'ga saqlaydi -- "SMM hisobot" sahifasi (`smm_analytics.py` +
`/smm` route) shu ma'lumotdan foydalanadi.

QO'SHIMCHA ENVIRONMENT VARIABLE KERAK EMAS -- allaqachon Meta Ads uchun
sozlangan `META_ACCESS_TOKEN` va `META_PAGE_ID` yetarli (Page Access Token
orqali, `meta_api.py`dagi Instant Form funksiyalari kabi). Instagram
statistikasi ishlashi uchun Instagram Business akkaunt shu Facebook
Page'ga ulangan bo'lishi kerak (Meta Business Suite -> Sozlamalar ->
Bog'langan hisoblar) -- ulanmagan bo'lsa, Facebook qismi baribir ishlayveradi,
Instagram qismi "notices"da aniq ko'rsatiladi.

2026-09, multi-tenant (foydalanuvchi so'rovi: "boshqa kompaniyalar bitta
tugma bilan Facebook/Instagram'ga ulansin, keyin ularniki HAM ishlasin"):
`sync_once(company=None)` endi ixtiyoriy `company` (`db.Company` qatori)
qabul qiladi -- berilsa, O'SHA kompaniyaning O'Z `meta_page_id`/
`meta_access_token`'i bilan sinxronlanadi va yozuvlar shu `company_id` bilan
saqlanadi. `sync_all_companies()` -- `meta_page_id`+`meta_access_token`
ulagan HAR BIR kompaniya bo'yicha aylanib, har birini ALOHIDA sinxronlaydi
(`scheduler.py` endi shuni chaqiradi, eski global-yagona `sync_once()`
o'rniga). Company #1 (platforma egasi) ham shu ro'yxatga ODDIY tarzda
kiradi -- chunki uning ham `meta_page_id`/`meta_access_token`si
`db.ensure_default_company()` orqali bootstrap vaqtida ENV'dan o'z
ustuniga nusxalangan (alohida holat sifatida ishlanmaydi).

SAQLASH MANTIG'I:
  - `SmmSnapshot` -- HAR KUNI (Toshkent kuni bo'yicha) bitta qator: shu
    kundagi JORIY obunachilar sonini "suratga oladi". Meta o'zi tarixiy
    obunachilar sonini bermaydi, shuning uchun o'sish grafigini chizish
    uchun buni o'zimiz kunma-kun to'plashimiz kerak. Bir kunda necha marta
    sync ishga tushsa ham, o'sha kunning qatori YANGILANADI (upsert),
    ikkilanmaydi.
  - `SmmPost` -- so'nggi ~25 ta post/media uchun ENG OXIRGI statistika
    (`external_id` bo'yicha upsert) -- like/comment/qamrov vaqt o'tishi
    bilan o'zgarib turadi, shuning uchun har safar qayta yoziladi.

Natija har doim `kv_store`ga ("smm_sync_status" kaliti) yoziladi -- admin
buni "SMM hisobot" sahifasida ko'ra oladi.
"""

import json
import logging
import datetime as dt

import meta_api
import kv_store
import db
from db import get_session, SmmSnapshot, SmmPost

logger = logging.getLogger("smm_sync")

_TASHKENT_OFFSET = dt.timedelta(hours=5)


def is_configured(company=None) -> bool:
    """`company` berilsa -- O'SHA kompaniyaning O'Z ulanishini tekshiradi
    (2026-09, multi-tenant). Berilmasa -- eski global (ENV) tekshiruv
    (CLI/skript uchun orqaga moslik)."""
    if company is not None:
        return bool(getattr(company, "meta_access_token", None) and getattr(company, "meta_page_id", None))
    return bool(meta_api.ACCESS_TOKEN and meta_api.PAGE_ID)


def _today_tashkent() -> str:
    return (dt.datetime.utcnow() + _TASHKENT_OFFSET).strftime("%Y-%m-%d")


def _parse_dt(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        # Meta odatda "2026-08-20T10:00:00+0000" formatida qaytaradi.
        return dt.datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def _upsert_snapshot(session, platform: str, followers_count, media_count, *, company_id: int) -> None:
    today = _today_tashkent()
    # MUHIM (2026-09, multi-tenant): shu KOMPANIYAGA tegishli qatorni
    # qidiramiz -- `company_id` filtrisiz bir xil `(platform, date)`
    # kombinatsiyasi boshqa kompaniyaning qatorini "topib", uning
    # ma'lumotini bosib yozib yuborishi mumkin edi.
    row = session.query(SmmSnapshot).filter_by(platform=platform, date=today, company_id=company_id).first()
    if row is None:
        row = SmmSnapshot(platform=platform, date=today, company_id=company_id)
        session.add(row)
    if followers_count is not None:
        row.followers_count = followers_count
    if media_count is not None:
        row.media_count = media_count
    session.commit()


def _upsert_post(session, *, company_id: int, **fields) -> None:
    external_id = fields["external_id"]
    # MUHIM (2026-09, multi-tenant): `external_id` Meta tomonidan GLOBAL
    # noyob (bitta media/post ID ikki xil kompaniyaga tegishli bo'la
    # olmaydi), shuning uchun bu yerda qo'shimcha `company_id` filtri SHART
    # EMAS -- lekin YANGI qator yaratilganda albatta TO'G'RI `company_id`
    # bilan yoziladi (aks holda hammasi `get_default_company_id()`ga
    # tushib, boshqa kompaniyalarning postlari platforma egasiga "sizib"
    # ko'rinardi).
    row = session.query(SmmPost).filter_by(external_id=external_id).first()
    if row is None:
        row = SmmPost(company_id=company_id, **fields)
        session.add(row)
    else:
        row.company_id = company_id
        for k, v in fields.items():
            if k == "external_id":
                continue
            setattr(row, k, v)
    session.commit()


def _friendly_meta_error(e: meta_api.MetaAPIError) -> str:
    """`MetaAPIError`ning xom Meta xatolik dict'ini ("message"/"code"/"type")
    o'qib bo'lmaydigan Python exception matni ({'message': '...', 'code': 10}
    kabi) o'rniga tushunarli matnga aylantiradi. 2026-08, foydalanuvchi
    ko'rgan aniq holat: `(#10) This endpoint requires the
    'pages_read_engagement' permission or the 'Page Public Content Access'
    feature` -- bu KOD MUAMMOSI EMAS, Meta Business Manager/App Review
    tomonida sozlanishi kerak bo'lgan ruxsat -- shuning uchun bu holatga
    ALOHIDA, aniq harakat ko'rsatmasi beriladi."""
    meta_err = e.args[0] if e.args else {}
    message = meta_err.get("message", str(e)) if isinstance(meta_err, dict) else str(e)
    code = meta_err.get("code") if isinstance(meta_err, dict) else None
    if code == 10 or "pages_read_engagement" in message or "Page Public Content Access" in message:
        return (
            "Facebook postlarini o'qish uchun ruxsat yetarli emas (Meta xatosi: "
            f"\"{message}\"). BU KODDAGI XATO EMAS -- Meta Business Manager'da "
            "App Review orqali 'Page Public Content Access' funksiyasini "
            "yoqish yoki tokenga 'pages_read_engagement' ruxsatini qo'shish "
            "kerak (Meta Developer Console -> App -> Permissions and Features)."
        )
    return message


def _facebook_media_type(post: dict) -> str:
    """Facebook post'ining `attachments` maydonidan haqiqiy media turini
    aniqlaydi (2026-08, foydalanuvchi so'rovi -- avval bu HAR DOIM "STATUS"
    deb belgilanardi, chunki `attachments` maydoni umuman so'ralmasdi, shu
    sabab "Eng faol postlar" jadvalida video/rasm/oddiy matn postlarini
    farqlab bo'lmasdi). Meta'ning `attachments.data[0].media_type` odatda
    "photo"/"video"/"album"/"link" qiymatlaridan birini beradi -- ularni
    Instagram tomonida ishlatiladigan IMAGE/VIDEO/CAROUSEL_ALBUM/LINK
    nomlariga moslashtiramiz, shunda shablon ikkalasini ham bir xil
    belgi/ikonka bilan ko'rsata oladi."""
    attachments = ((post.get("attachments") or {}).get("data")) or []
    if not attachments:
        return "STATUS"
    raw = (attachments[0].get("media_type") or attachments[0].get("type") or "").lower()
    return {
        "photo": "IMAGE",
        "video_inline": "VIDEO", "video_autoplay": "VIDEO", "video": "VIDEO",
        "album": "CAROUSEL_ALBUM",
        "link": "LINK", "share": "LINK",
    }.get(raw, "STATUS")


def _sync_facebook(session, result: dict, *, page_id: str | None, access_token: str | None, company_id: int) -> None:
    try:
        profile = meta_api.get_facebook_page_profile(page_id=page_id, access_token=access_token)
    except meta_api.MetaAPIError as e:
        result["errors"].append(f"Facebook Page profilini olishda xatolik: {_friendly_meta_error(e)}")
        return

    followers = profile.get("fan_count")
    _upsert_snapshot(session, "facebook", followers_count=followers, media_count=None, company_id=company_id)
    result["facebook"]["followers_count"] = followers

    try:
        posts = meta_api.get_facebook_page_posts(limit=25, page_id=page_id, access_token=access_token)
    except meta_api.MetaAPIError as e:
        result["errors"].append(f"Facebook postlarini olishda xatolik: {_friendly_meta_error(e)}")
        return

    synced = 0
    for p in posts:
        post_id = p.get("id")
        if not post_id:
            continue
        insights = {}
        try:
            insights = meta_api.get_facebook_post_insights(post_id, page_id=page_id, access_token=access_token)
        except meta_api.MetaAPIError:
            pass  # ba'zi postlarda insights ruxsati bo'lmasligi mumkin -- shu bittasi bo'sh qoladi
        _upsert_post(
            session,
            company_id=company_id,
            platform="facebook",
            external_id=post_id,
            caption=p.get("message"),
            permalink=p.get("permalink_url"),
            media_type=_facebook_media_type(p),
            thumbnail_url=p.get("full_picture"),
            posted_at=_parse_dt(p.get("created_time")),
            like_count=((p.get("likes") or {}).get("summary") or {}).get("total_count", 0),
            comments_count=((p.get("comments") or {}).get("summary") or {}).get("total_count", 0),
            # Facebook'da "repost" (share) haqiqiy, to'g'ridan-to'g'ri
            # `/posts` maydoni -- insights'ga bog'liq emas, shuning uchun
            # bu HAR DOIM to'g'ri ishlagan (item 6 tuzatishi Instagram
            # tomoniga tegishli edi, bu yerga emas).
            shares_count=(p.get("shares") or {}).get("count", 0),
            # "Saqlangan" (bookmark/save) -- Instagram'ga xos tushuncha,
            # Facebook Page post'ida bunday metrika yo'q. 0 emas None --
            # "haqiqatan nol" bilan "bu platformada mavjud emas"ni
            # aralashtirib yubormaslik uchun (reach/impressions'dagi kabi).
            saved_count=None,
            reach=insights.get("post_impressions"),
            impressions=insights.get("post_impressions"),
            raw_data=json.dumps(p, ensure_ascii=False),
        )
        synced += 1
    result["facebook"]["posts_synced"] = synced


def _sync_instagram(session, result: dict, *, page_id: str | None, access_token: str | None, company_id: int) -> None:
    try:
        ig_id = meta_api.get_instagram_business_account_id(page_id=page_id, access_token=access_token)
    except meta_api.MetaAPIError as e:
        result["errors"].append(f"Instagram akkauntni aniqlashda xatolik: {_friendly_meta_error(e)}")
        return
    if not ig_id:
        result["notices"].append(
            "Sahifangizga ulangan Instagram Business akkaunt topilmadi -- "
            "Instagram statistikasi o'tkazib yuborildi (Facebook statistikasi "
            "baribir yig'ilmoqda)."
        )
        return

    try:
        profile = meta_api.get_instagram_profile(ig_id, page_id=page_id, access_token=access_token)
    except meta_api.MetaAPIError as e:
        result["errors"].append(f"Instagram profilini olishda xatolik: {_friendly_meta_error(e)}")
        return

    _upsert_snapshot(
        session, "instagram",
        followers_count=profile.get("followers_count"),
        media_count=profile.get("media_count"),
        company_id=company_id,
    )
    result["instagram"]["followers_count"] = profile.get("followers_count")

    try:
        media_list = meta_api.get_instagram_media(ig_id, limit=25, page_id=page_id, access_token=access_token)
    except meta_api.MetaAPIError as e:
        result["errors"].append(f"Instagram postlarini olishda xatolik: {_friendly_meta_error(e)}")
        return

    synced = 0
    insights_error_count = 0
    insights_error_sample = None
    for m in media_list:
        media_id = m.get("id")
        if not media_id:
            continue
        insights = {}
        try:
            insights = meta_api.get_instagram_media_insights(
                media_id,
                media_type=m.get("media_type", "IMAGE"),
                media_product_type=m.get("media_product_type"),
                page_id=page_id, access_token=access_token,
            )
        except meta_api.MetaAPIError as e:
            # MUHIM (2026-08, foydalanuvchi shikoyati: "smm haliyam notori
            # ishlavoti, videodan nechta obunachi kelganini ko'rsatmayapti"):
            # AVVAL bu yerda xato JIM yutilardi (`pass`) -- reach/views/
            # shares/saved/follows HAMMASI doim None qolib, foydalanuvchiga
            # "Meta bermadi (ruxsat/API cheklovi)" degan UMUMIY, tekshirib
            # bo'lmaydigan xabar ko'rsatilardi, HAQIQIY sabab (masalan qaysi
            # aniq ruxsat yetishmayotgani) HECH QAYERDA ko'rinmasdi. Endi bu
            # xato ushlanib, sync_status'ga (va shu orqali /smm sahifasidagi
            # "Sinxronizatsiya holati" paneliga) chiqariladi -- shunda
            # foydalanuvchi ANIQ Meta xato matnini ko'rib, kerak bo'lsa
            # menga yuborishi yoki Meta Business Manager'da tegishli
            # ruxsatni yoqishi mumkin.
            insights_error_count += 1
            if insights_error_sample is None:
                insights_error_sample = _friendly_meta_error(e)
        _upsert_post(
            session,
            company_id=company_id,
            platform="instagram",
            external_id=media_id,
            caption=m.get("caption"),
            permalink=m.get("permalink"),
            media_type=m.get("media_type"),
            # `thumbnail_url` faqat VIDEO/REEL turida keladi -- IMAGE/
            # CAROUSEL_ALBUM uchun `media_url`ning o'zi muqova bo'ladi.
            thumbnail_url=m.get("thumbnail_url") or m.get("media_url"),
            posted_at=_parse_dt(m.get("timestamp")),
            like_count=m.get("like_count", 0),
            comments_count=m.get("comments_count", 0),
            # MUHIM BUG FIX (2026-08): avval `insights.get("shares", 0) or 0`
            # edi -- bu insights so'rovi BUTUNLAY MUVAFFAQIYATSIZ bo'lganda
            # ham (yuqoridagi except -- `insights` bo'sh {} qoladi) "0"
            # (ya'ni "aniq bilamiz -- repost yo'q") deb yozib qo'yardi,
            # aslida bu holatda biz HECH NARSANI bilmaymiz. Endi reach/
            # follows/saved bilan BIR XIL qoidaga bo'ysunadi: `None` =
            # "ma'lumot yo'q/olinmadi", haqiqiy `0` = "tasdiqlangan nol".
            shares_count=insights.get("shares"),
            saved_count=insights.get("saved"),
            # 2026-08 (item 6 tuzatishi): "follows" -- FAQAT FEED/STORY turi
            # uchun Meta beradi, REELS uchun None qoladi (Meta'ning o'z
            # cheklovi -- shablon buni "—" deb ko'rsatadi, "0" emas).
            follows_count=insights.get("follows"),
            reach=insights.get("reach"),
            # 2026-08: bekor qilingan "impressions"/"plays" o'rniga "views".
            impressions=insights.get("views"),
            raw_data=json.dumps(m, ensure_ascii=False),
        )
        synced += 1
    result["instagram"]["posts_synced"] = synced
    if insights_error_count:
        result["errors"].append(
            f"{insights_error_count}/{synced} ta Instagram postining statistikasi (qamrov/ko'rish/share/"
            f"saqlangan/obunachi) olinmadi -- Meta xatosi: \"{insights_error_sample}\". Shu postlar uchun "
            "eski/bo'sh qiymat qoladi, keyingi sinxronizatsiyada avtomatik qayta uriniladi."
        )


def sync_once(company=None) -> dict:
    """Bitta sinxronizatsiya tsiklini bajaradi (Facebook + Instagram).
    `company` berilsa (`db.Company` qatori) -- O'SHA kompaniyaning O'Z
    `meta_page_id`/`meta_access_token`i bilan, natija shu kompaniyaning
    `company_id`si bilan saqlanadi. Berilmasa -- eski global (ENV) xatti-
    harakat (default kompaniyaga yoziladi).

    Qaytaradi: {"errors": [...], "notices": [...],
    "facebook": {"followers_count", "posts_synced"},
    "instagram": {"followers_count", "posts_synced"}}."""
    result = {
        "errors": [], "notices": [],
        "facebook": {"followers_count": None, "posts_synced": 0},
        "instagram": {"followers_count": None, "posts_synced": 0},
    }
    if not is_configured(company):
        result["errors"].append(
            "Meta hisobi sozlanmagan (Page/token yo'q) -- SMM sinxronizatsiya o'tkazib yuborildi."
        )
        _save_status(result, company_id=company.id if company else None)
        return result

    page_id = company.meta_page_id if company else None
    access_token = company.meta_access_token if company else None
    company_id = company.id if company else db.get_default_company_id()

    session = get_session()
    try:
        _sync_facebook(session, result, page_id=page_id, access_token=access_token, company_id=company_id)
        _sync_instagram(session, result, page_id=page_id, access_token=access_token, company_id=company_id)
    finally:
        session.close()

    _save_status(result, company_id=company_id)
    return result


def sync_all_companies() -> dict:
    """2026-09, multi-tenant: `meta_page_id`+`meta_access_token` ulagan
    HAR BIR kompaniya bo'yicha aylanib, har birini ALOHIDA (o'z hisobi
    bilan) sinxronlaydi. `scheduler.py`ning davriy SMM job'i endi shuni
    chaqiradi -- eski yagona-akkaunt `sync_once()` o'rniga. Bitta
    kompaniyaning sinxronizatsiyasi muvaffaqiyatsiz bo'lishi qolganlarini
    to'xtatmaydi."""
    from db import Company

    session = get_session()
    try:
        rows = (
            session.query(Company)
            .filter(Company.meta_page_id.isnot(None), Company.meta_access_token.isnot(None), Company.is_active.is_(True))
            .all()
        )
        # Session yopilgach ham ishlatish uchun -- ORM obyektlarini emas,
        # oddiy qiymatlarni chiqarib olamiz.
        companies = [{"id": c.id, "name": c.name, "meta_page_id": c.meta_page_id, "meta_access_token": c.get_meta_access_token()} for c in rows]
    finally:
        session.close()

    per_company = {}
    for c in companies:
        fake_company = _CompanyCreds(id=c["id"], meta_page_id=c["meta_page_id"], meta_access_token=c["meta_access_token"])
        try:
            per_company[c["id"]] = sync_once(company=fake_company)
        except Exception as e:
            logger.exception("SMM sync: '%s' (id=%s) kompaniyasi uchun xato", c["name"], c["id"])
            per_company[c["id"]] = {"errors": [f"Kutilmagan xato: {e}"]}
    return {"companies_synced": len(companies), "per_company": per_company}


class _CompanyCreds:
    """`sync_once(company=...)`ga uzatish uchun yengil obyekt -- to'liq
    `db.Company` ORM qatori shart emas, faqat shu uchta maydon kerak
    (session yopilgandan keyin ham ishlatish uchun detach qilingan)."""
    def __init__(self, id, meta_page_id, meta_access_token):
        self.id = id
        self.meta_page_id = meta_page_id
        self.meta_access_token = meta_access_token


def _status_key(company_id: int | None) -> str:
    return "smm_sync_status" if company_id is None else f"smm_sync_status:{company_id}"


def _save_status(result: dict, *, company_id: int | None = None) -> None:
    try:
        kv_store.set_json(_status_key(company_id), {**result, "last_run_at": dt.datetime.utcnow().isoformat()})
    except Exception:
        logger.exception("smm_sync_status'ni kv_store'ga yozishda xato (o'zi kritik emas)")


def get_last_status(company_id: int | None = None) -> dict | None:
    return kv_store.get_json(_status_key(company_id), default=None)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(sync_all_companies(), ensure_ascii=False, indent=2))
