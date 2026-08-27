"""competitor_sync.py — Meta Ad Library orqali admin qo'shgan
raqobatchilarning JORIY reklamalarini kuzatib boradi (2026-08, foydalanuvchi
so'rovi: "raqobatchilarni har kuni tahlil qilsin, ad library orqali").

Ishlash tartibi `smm_sync.py` bilan bir xil naqsh: har bir faol
`Competitor` uchun Ad Library'dan qidiruv qilinadi, topilgan har bir
reklama `external_id` (Meta'ning ad_archive_id'i) bo'yicha upsert qilinadi
(`CompetitorAd`) — shu orqali "yangi reklama chiqdimi" yoki "eskisi hali
ham ishlayaptimi" bilish mumkin bo'ladi.
"""

import datetime as dt
import logging

import meta_api
from db import get_session, Competitor, CompetitorAd

logger = logging.getLogger("competitor-sync")


def is_configured() -> bool:
    return bool(meta_api.ACCESS_TOKEN)


def _parse_dt(raw):
    if not raw:
        return None
    try:
        return dt.datetime.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        return None


def sync_once() -> dict:
    """Barcha faol raqobatchilarni bittama-bitta tekshiradi. Bitta
    raqobatchida Meta xato bersa (masalan qidiruv natija bermasa) qolganlar
    baribir davom etadi — bitta xato butun sinxronizatsiyani to'xtatmasligi
    kerak."""
    if not is_configured():
        return {"skipped": "META_ACCESS_TOKEN sozlanmagan"}

    session = get_session()
    stats = {"competitors": 0, "ads_found": 0, "ads_new": 0, "errors": []}
    try:
        competitors = session.query(Competitor).filter_by(is_active=True).all()
        for comp in competitors:
            stats["competitors"] += 1
            term = (comp.search_term or comp.name or "").strip()
            if not term:
                continue
            try:
                ads = meta_api.search_ad_library(term)
            except meta_api.MetaAPIError as e:
                logger.warning("Ad Library qidiruvida xatolik (%s): %s", comp.name, e)
                stats["errors"].append(f"{comp.name}: {e}")
                continue
            except Exception as e:
                logger.exception("Ad Library qidiruvida kutilmagan xatolik (%s)", comp.name)
                stats["errors"].append(f"{comp.name}: {e}")
                continue

            for ad in ads:
                external_id = ad.get("id")
                if not external_id:
                    continue
                stats["ads_found"] += 1
                existing = session.query(CompetitorAd).filter_by(external_id=external_id).first()
                bodies = ad.get("ad_creative_bodies") or []
                if existing is None:
                    existing = CompetitorAd(competitor_id=comp.id, external_id=external_id)
                    session.add(existing)
                    stats["ads_new"] += 1
                existing.page_name = ad.get("page_name")
                existing.body_text = bodies[0] if bodies else None
                existing.snapshot_url = ad.get("ad_snapshot_url")
                existing.is_active = not ad.get("ad_delivery_stop_time")
                existing.ad_started_at = _parse_dt(ad.get("ad_delivery_start_time"))
                existing.last_seen_at = dt.datetime.utcnow()
            session.commit()
    finally:
        session.close()
    return stats
