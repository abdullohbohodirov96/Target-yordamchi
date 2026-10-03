"""Competitor Ads / Ad Library moduli.

Tuzilma:
  provider.py          -- Ad / AdQuery modeli, AdLibraryProvider interfeysi, tanlash (AD_LIBRARY_PROVIDER)
  providers/web.py     -- Ad Library veb-sahifasi (adlib_scraper, token kerak emas)
  providers/apify.py   -- Apify aktori (APIFY_TOKEN)
  providers/meta_api.py-- rasmiy ads_archive API (O'zbekiston tijoriy reklamalari yo'q)
  query.py             -- nom / Page URL / Page ID ni ajratish
  analyzer.py          -- "AI Analyze" (Hook, Offer, CTA, Pain point, Creative type, Lead magnet)
  store.py             -- so'nggi qidiruv natijalari (AI tahlil faqat server ko'rgan reklamaga)
"""

from .provider import (  # noqa: F401
    Ad, AdLibraryError, AdLibraryProvider, AdQuery, ProviderBlocked, ProviderNotConfigured,
    provider_chain, search_ads,
)
from .query import parse_query  # noqa: F401
