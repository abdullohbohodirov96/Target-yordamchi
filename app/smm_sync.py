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


def _sync_facebook(session, result: dict) -> None:
    try:
        profile = meta_api.get_facebook_page_profile()
    except meta_api.MetaAPIError as e:
        result["errors"].append(f"Facebook Page profilini olishda xatolik: {e}")
        return

    followers = profile.get("fan_count")
    _upsert_snapshot(session, "facebook", followers_count=followers, media_count=None)
    result["facebook"]["followers_count"] = followers

    try:
        posts = meta_api.get_facebook_page_posts(limit=25)
    except meta_api.MetaAPIError as e:
        result["errors"].append(f"Facebook postlarini olishda xatolik: {e}")
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
            media_type="STATUS",
            posted_at=_parse_dt(p.get("created_time")),
            like_count=((p.get("likes") or {}).get("summary") or {}).get("total_count", 0),
            comments_count=((p.get("comments") or {}).get("summary") or {}).get("total_count", 0),
            shares_count=(p.get("shares") or {}).get("count", 0),
            saved_count=0,
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
        result["errors"].append(f"Instagram akkauntni aniqlashda xatolik: {e}")
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
        result["errors"].append(f"Instagram profilini olishda xatolik: {e}")
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
        result["errors"].append(f"Instagram postlarini olishda xatolik: {e}")
        return

    synced = 0
    for m in media_list:
        media_id = m.get("id")
        if not media_id:
            continue
        insights = {}
        try:
            insights = meta_api.get_instagram_media_insights(media_id, media_type=m.get("media_type", "IMAGE"))
        except meta_api.MetaAPIError:
            pass
        _upsert_post(
            session,
            platform="instagram",
            external_id=media_id,
            caption=m.get("caption"),
            permalink=m.get("permalink"),
            media_type=m.get("media_type"),
            posted_at=_parse_dt(m.get("timestamp")),
            like_count=m.get("like_count", 0),
            comments_count=m.get("comments_count", 0),
            shares_count=0,
            saved_count=insights.get("saved"),
            reach=insights.get("reach"),
            impressions=insights.get("impressions") or insights.get("plays"),
            raw_data=json.dumps(m, ensure_ascii=False),
        )
        synced += 1
    result["instagram"]["posts_synced"] = synced


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
