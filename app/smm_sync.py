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
from db import get_session, SmmSnapshot, SmmPost

logger = logging.getLogger("smm_sync")

_TASHKENT_OFFSET = dt.timedelta(hours=5)


def is_configured() -> bool:
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


def _upsert_snapshot(session, platform: str, followers_count, media_count) -> None:
    today = _today_tashkent()
    row = session.query(SmmSnapshot).filter_by(platform=platform, date=today).first()
    if row is None:
        row = SmmSnapshot(platform=platform, date=today)
        session.add(row)
    if followers_count is not None:
        row.followers_count = followers_count
    if media_count is not None:
        row.media_count = media_count
    session.commit()


def _upsert_post(session, **fields) -> None:
    external_id = fields["external_id"]
    row = session.query(SmmPost).filter_by(external_id=external_id).first()
    if row is None:
        row = SmmPost(**fields)
        session.add(row)
    else:
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


def _sync_facebook(session, result: dict) -> None:
    try:
        profile = meta_api.get_facebook_page_profile()
    except meta_api.MetaAPIError as e:
        result["errors"].append(f"Facebook Page profilini olishda xatolik: {_friendly_meta_error(e)}")
        return

    followers = profile.get("fan_count")
    _upsert_snapshot(session, "facebook", followers_count=followers, media_count=None)
    result["facebook"]["followers_count"] = followers

    try:
        posts = meta_api.get_facebook_page_posts(limit=25)
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
            insights = meta_api.get_facebook_post_insights(post_id)
        except meta_api.MetaAPIError:
            pass  # ba'zi postlarda insights ruxsati bo'lmasligi mumkin -- shu bittasi bo'sh qoladi
        _upsert_post(
            session,
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


def _sync_instagram(session, result: dict) -> None:
    try:
        ig_id = meta_api.get_instagram_business_account_id()
    except meta_api.MetaAPIError as e:
        result["errors"].append(f"Instagram akkauntni aniqlashda xatolik: {_friendly_meta_error(e)}")
        return
    if not ig_id:
        result["notices"].append(
            "META_PAGE_ID'ga ulangan Instagram Business akkaunt topilmadi -- "
            "Instagram statistikasi o'tkazib yuborildi (Facebook statistikasi "
            "baribir yig'ilmoqda)."
        )
        return

    try:
        profile = meta_api.get_instagram_profile(ig_id)
    except meta_api.MetaAPIError as e:
        result["errors"].append(f"Instagram profilini olishda xatolik: {_friendly_meta_error(e)}")
        return

    _upsert_snapshot(
        session, "instagram",
        followers_count=profile.get("followers_count"),
        media_count=profile.get("media_count"),
    )
    result["instagram"]["followers_count"] = profile.get("followers_count")

    try:
        media_list = meta_api.get_instagram_media(ig_id, limit=25)
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


def sync_once() -> dict:
    """Bitta sinxronizatsiya tsiklini bajaradi (Facebook + Instagram).
    Qaytaradi: {"errors": [...], "notices": [...],
    "facebook": {"followers_count", "posts_synced"},
    "instagram": {"followers_count", "posts_synced"}}."""
    result = {
        "errors": [], "notices": [],
        "facebook": {"followers_count": None, "posts_synced": 0},
        "instagram": {"followers_count": None, "posts_synced": 0},
    }
    if not is_configured():
        result["errors"].append(
            "META_ACCESS_TOKEN yoki META_PAGE_ID sozlanmagan -- SMM sinxronizatsiya o'tkazib yuborildi."
        )
        _save_status(result)
        return result

    session = get_session()
    try:
        _sync_facebook(session, result)
        _sync_instagram(session, result)
    finally:
        session.close()

    _save_status(result)
    return result


def _save_status(result: dict) -> None:
    try:
        kv_store.set_json("smm_sync_status", {**result, "last_run_at": dt.datetime.utcnow().isoformat()})
    except Exception:
        logger.exception("smm_sync_status'ni kv_store'ga yozishda xato (o'zi kritik emas)")


def get_last_status() -> dict | None:
    return kv_store.get_json("smm_sync_status", default=None)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(sync_once(), ensure_ascii=False, indent=2))
