"""MetaApiProvider — Meta'ning rasmiy Ad Library API'si (`ads_archive`).

Cheklov: O'zbekistondagi oddiy (tijoriy) reklamalarni BERMAYDI -- faqat
siyosiy/ijtimoiy va Yevropada ko'rsatilganlar. Shuning uchun `auto`
navbatida oxirgi o'rinda.

Env: META_AD_LIBRARY_TOKEN (shaxsi tasdiqlangan odamning user tokeni) yoki
     META_ACCESS_TOKEN."""

from __future__ import annotations

import meta_api

from ..provider import Ad, AdLibraryError, AdLibraryProvider, AdQuery

FIELDS = ("id,ad_snapshot_url,page_id,page_name,ad_creative_bodies,ad_creative_link_titles,"
          "ad_creative_link_captions,ad_delivery_start_time,ad_delivery_stop_time,publisher_platforms")


class MetaApiProvider(AdLibraryProvider):
    name = "meta_api"
    label = "Meta Ad Library API"

    def is_configured(self) -> bool:
        return bool(meta_api.AD_LIBRARY_TOKEN or meta_api.ACCESS_TOKEN)

    def search(self, query: AdQuery):
        params = {
            "ad_reached_countries": [query.country] if query.country and query.country != "ALL" else ["UZ"],
            "ad_active_status": "ACTIVE" if query.active_only else "ALL",
            "ad_type": "ALL",
            "limit": query.limit,
            "fields": FIELDS,
        }
        if query.page_id:
            params["search_page_ids"] = [query.page_id]
        else:
            params["search_terms"] = query.term or query.page_vanity or ""
        try:
            data = meta_api._get("ads_archive", params, token=meta_api.AD_LIBRARY_TOKEN or None)
        except meta_api.MetaAPIError as e:
            raise AdLibraryError(meta_api.safe_error_message(e), provider=self.name) from e
        out = []
        for d in data.get("data", []):
            bodies = d.get("ad_creative_bodies") or []
            titles = d.get("ad_creative_link_titles") or []
            captions = d.get("ad_creative_link_captions") or []
            out.append(Ad(
                id=str(d.get("id")), page_id=d.get("page_id"), page_name=d.get("page_name"),
                primary_text=bodies[0] if bodies else None, headline=titles[0] if titles else None,
                link_caption=captions[0] if captions else None,
                platforms=[str(p).lower() for p in (d.get("publisher_platforms") or [])],
                is_active=not d.get("ad_delivery_stop_time"),
                start_date=(d.get("ad_delivery_start_time") or "")[:10] or None,
                end_date=(d.get("ad_delivery_stop_time") or "")[:10] or None,
                media_type="unknown", ad_library_url=d.get("ad_snapshot_url"), provider=self.name,
            ))
        return out
