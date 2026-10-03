"""Providerlar uchun umumiy normalizatsiya: Ad Library'ning xom JSON tuzilmasi
(veb-sahifa va ko'pchilik Apify aktorlari bir xil tuzilmada beradi) -> `Ad`."""

from __future__ import annotations

from ..provider import Ad

_FORMAT_MAP = {
    "VIDEO": "video", "IMAGE": "image", "CAROUSEL": "carousel", "DCO": "dynamic",
    "DPA": "dynamic", "MULTI_IMAGES": "carousel", "MULTI_MEDIA": "carousel", "TEXT": "text",
}


def media_type_from(display_format: "str | None", *, has_video: bool, has_image: bool, cards: int) -> str:
    mt = _FORMAT_MAP.get((display_format or "").upper())
    if mt:
        return mt
    if has_video:
        return "video"
    if cards > 1:
        return "carousel"
    if has_image:
        return "image"
    return "unknown"


def _int(v) -> "int | None":
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def ad_from_adlib_dict(d: dict, provider: str) -> "Ad | None":
    """`adlib_scraper.normalize()` natijasi (dict) -> `Ad`."""
    if not d or not d.get("id"):
        return None
    cards = d.get("card_images") or []
    return Ad(
        id=str(d["id"]),
        page_id=d.get("page_id"),
        page_name=d.get("page_name"),
        page_picture_url=d.get("page_picture_url"),
        page_profile_uri=d.get("page_profile_uri"),
        page_like_count=_int(d.get("page_like_count")),
        primary_text=d.get("primary_text") or ((d.get("ad_creative_bodies") or [None])[0]),
        headline=d.get("headline") or ((d.get("ad_creative_link_titles") or [None])[0]),
        cta_text=d.get("cta_text"),
        link_url=d.get("link_url"),
        link_caption=d.get("link_caption"),
        platforms=[str(p).lower() for p in (d.get("platforms") or [])],
        is_active=bool(d.get("is_active", not d.get("ad_delivery_stop_time"))),
        start_date=(d.get("ad_delivery_start_time") or "")[:10] or None,
        end_date=(d.get("ad_delivery_stop_time") or "")[:10] or None,
        media_type=media_type_from(d.get("display_format"), has_video=bool(d.get("video_url")),
                                   has_image=bool(d.get("image_url") or d.get("preview_image_url")), cards=len(cards)),
        image_url=d.get("image_url") or (cards[0] if cards else None) or (None if d.get("video_url") else d.get("preview_image_url")),
        video_url=d.get("video_url"),
        video_poster_url=d.get("preview_image_url") if d.get("video_url") else None,
        card_images=cards,
        ad_library_url=d.get("ad_snapshot_url") or f"https://www.facebook.com/ads/library/?id={d['id']}",
        variants=_int(d.get("collation_count")) or 1,
        provider=provider,
    )
