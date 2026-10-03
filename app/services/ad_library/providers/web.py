"""WebProvider — Meta Ad Library'ning ommaviy veb-sahifasini serverdagi
yashirin brauzer orqali o'qiydi (`adlib_scraper.py`). Token kerak emas;
O'zbekiston tijoriy reklamalari ham ko'rinadi.

Env: ADLIB_SCRAPER (Render'da standart yoqilgan, "0" -- o'chiq),
     ADLIB_PROXY (ixtiyoriy), ADLIB_CHROMIUM_PATH (ixtiyoriy)."""

from __future__ import annotations

import adlib_scraper

from ..provider import AdLibraryProvider, AdQuery, ProviderBlocked
from .common import ad_from_adlib_dict


class WebProvider(AdLibraryProvider):
    name = "web"
    label = "Ad Library (veb-sahifa)"

    def is_configured(self) -> bool:
        return adlib_scraper.is_enabled()

    def search(self, query: AdQuery):
        try:
            raw = adlib_scraper.search(
                query.term or query.page_vanity or "", country=query.country, limit=query.limit,
                page_id=query.page_id, active_only=query.active_only,
            )
        except adlib_scraper.AdLibraryBlocked as e:
            raise ProviderBlocked(f"Ad Library sahifasi javob bermadi: {e}", provider=self.name) from e
        return [a for a in (ad_from_adlib_dict(d, self.name) for d in raw) if a]
