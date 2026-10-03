"""ApifyProvider — Apify'dagi "Facebook Ad Library" aktori orqali (pullik
tashqi xizmat; ular brauzer va proksilarni o'zlari boshqaradi).

Env:
  APIFY_TOKEN                -- majburiy (Apify → Settings → API & Integrations)
  APIFY_AD_LIBRARY_ACTOR     -- ixtiyoriy, standart "curious_coder~facebook-ads-library-scraper"
  APIFY_TIMEOUT_SECONDS      -- ixtiyoriy, standart 90

Aktor Ad Library URL'ini qabul qilib, sahifadagi bilan BIR XIL xom JSON
(ad_archive_id, snapshot, start_date, ...) qaytaradi -- shuning uchun
`adlib_scraper.normalize` qayta ishlatiladi. Boshqa aktor tanlansa va
tuzilma farq qilsa, faqat shu fayl moslashtiriladi."""

from __future__ import annotations

import os

import requests

import adlib_scraper

from ..provider import AdLibraryError, AdLibraryProvider, AdQuery, ProviderBlocked
from .common import ad_from_adlib_dict

DEFAULT_ACTOR = "curious_coder~facebook-ads-library-scraper"


class ApifyProvider(AdLibraryProvider):
    name = "apify"
    label = "Apify"

    def _token(self) -> str:
        return os.environ.get("APIFY_TOKEN", "").strip()

    def is_configured(self) -> bool:
        return bool(self._token())

    def search(self, query: AdQuery):
        actor = os.environ.get("APIFY_AD_LIBRARY_ACTOR", "").strip() or DEFAULT_ACTOR
        timeout = int(os.environ.get("APIFY_TIMEOUT_SECONDS", "90") or 90)
        url = adlib_scraper.library_url(query.term or query.page_vanity or "", query.country,
                                        active_only=query.active_only, page_id=query.page_id)
        try:
            resp = requests.post(
                f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items",
                params={"token": self._token(), "timeout": timeout},
                json={"urls": [{"url": url}], "count": query.limit, "scrapeAdDetails": False},
                timeout=timeout + 15,
            )
        except requests.RequestException as e:
            raise ProviderBlocked("Apify bilan aloqa bo'lmadi", provider=self.name) from e
        if resp.status_code in (401, 403):
            raise AdLibraryError("Apify tokeni noto'g'ri yoki ruxsat yo'q (APIFY_TOKEN)", provider=self.name)
        if resp.status_code == 402:
            raise AdLibraryError("Apify hisobida mablag' tugagan", provider=self.name)
        if resp.status_code >= 400:
            raise ProviderBlocked(f"Apify xatosi (HTTP {resp.status_code})", provider=self.name)
        try:
            items = resp.json()
        except ValueError as e:
            raise ProviderBlocked("Apify javobi o'qilmadi", provider=self.name) from e
        out = []
        for item in items if isinstance(items, list) else []:
            node = item.get("collated_results", [item])[0] if isinstance(item, dict) else None
            d = adlib_scraper.normalize(node) if isinstance(node, dict) else None
            ad = ad_from_adlib_dict(d, self.name) if d else None
            if ad:
                out.append(ad)
        return out
