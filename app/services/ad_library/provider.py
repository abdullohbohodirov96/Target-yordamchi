"""provider.py — Competitor Ads / Ad Library moduli: provider interfeysi.

Har bir manba (Meta rasmiy API, Ad Library veb-sahifasi, Apify va h.k.)
`AdLibraryProvider`ni amalga oshiradi va natijani BIR XIL `Ad` modeliga
keltiradi. Qolgan kod (route'lar, dashboard, AI tahlil) faqat shu interfeys
bilan ishlaydi -- shuning uchun provider keyinchalik almashtirilsa, boshqa
hech narsa o'zgarmaydi.

Tanlash (`.env` / Render Environment):
    AD_LIBRARY_PROVIDER = auto | web | apify | meta_api   (standart: auto)
  auto -- sozlangan providerlar navbat bilan: web -> apify -> meta_api;
          biri to'siq/xato bersa keyingisiga o'tiladi.

API kalitlar KODDA YO'Q -- har bir provider o'zi `os.environ`dan o'qiydi.
"""

from __future__ import annotations

import abc
import datetime as dt
import logging
import os
from dataclasses import asdict, dataclass, field

logger = logging.getLogger("ad_library")

DEFAULT_COUNTRY = "UZ"
MAX_LIMIT = 60


class AdLibraryError(Exception):
    """Foydalanuvchiga ko'rsatiladigan xato. `code` -- front-end uchun."""

    code = "provider_error"

    def __init__(self, message: str, *, provider: "str | None" = None):
        super().__init__(message)
        self.provider = provider


class ProviderNotConfigured(AdLibraryError):
    code = "not_configured"


class ProviderBlocked(AdLibraryError):
    """Manba vaqtincha javob bermadi (to'siq, captcha, timeout)."""

    code = "blocked"


@dataclass
class AdQuery:
    term: str = ""                 # kompaniya nomi / kalit so'z
    page_id: "str | None" = None   # Facebook Page ID (aniq sahifa)
    page_vanity: "str | None" = None  # facebook.com/<vanity> -- natijani shu sahifaga filtrlash
    country: str = DEFAULT_COUNTRY
    active_only: bool = True
    limit: int = 30
    kind: str = "name"             # name | page_id | page_url

    def describe(self) -> str:
        return self.page_id or self.page_vanity or self.term


@dataclass
class Ad:
    """Provider'dan qat'i nazar yagona reklama modeli."""

    id: str
    page_id: "str | None" = None
    page_name: "str | None" = None
    page_picture_url: "str | None" = None
    page_profile_uri: "str | None" = None
    page_like_count: "int | None" = None
    primary_text: "str | None" = None
    headline: "str | None" = None
    cta_text: "str | None" = None
    link_url: "str | None" = None
    link_caption: "str | None" = None
    platforms: list = field(default_factory=list)
    is_active: bool = True
    start_date: "str | None" = None   # ISO (YYYY-MM-DD)
    end_date: "str | None" = None
    media_type: str = "unknown"      # video | image | carousel | dynamic | text | unknown
    image_url: "str | None" = None
    video_url: "str | None" = None
    video_poster_url: "str | None" = None
    card_images: list = field(default_factory=list)
    ad_library_url: "str | None" = None
    variants: int = 1
    provider: str = ""

    @property
    def running_days(self) -> "int | None":
        if not self.start_date:
            return None
        try:
            start = dt.date.fromisoformat(self.start_date[:10])
        except ValueError:
            return None
        end = dt.date.today()
        if not self.is_active and self.end_date:
            try:
                end = dt.date.fromisoformat(self.end_date[:10])
            except ValueError:
                pass
        return max(0, (end - start).days)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["running_days"] = self.running_days
        return d


class AdLibraryProvider(abc.ABC):
    name = "base"
    label = "Base"

    @abc.abstractmethod
    def is_configured(self) -> bool:
        """Kalit/sozlama bormi (tarmoqqa chiqmasdan)."""

    @abc.abstractmethod
    def search(self, query: AdQuery) -> list[Ad]:
        """Reklamalarni qaytaradi; xatoda `AdLibraryError` (yoki voris) ko'taradi."""


def _registry() -> dict:
    from .providers.apify import ApifyProvider
    from .providers.meta_api import MetaApiProvider
    from .providers.web import WebProvider
    return {p.name: p for p in (WebProvider(), ApifyProvider(), MetaApiProvider())}


AUTO_ORDER = ("web", "apify", "meta_api")


def provider_chain(name: "str | None" = None) -> list[AdLibraryProvider]:
    """Tanlangan provider (yoki `auto` bo'lsa sozlanganlarining navbati)."""
    name = (name or os.environ.get("AD_LIBRARY_PROVIDER") or "auto").strip().lower()
    reg = _registry()
    if name != "auto":
        if name not in reg:
            raise ProviderNotConfigured(f"Noma'lum AD_LIBRARY_PROVIDER: {name}")
        return [reg[name]]
    return [reg[n] for n in AUTO_ORDER if reg[n].is_configured()]


def search_ads(query: AdQuery, provider: "str | None" = None) -> tuple[list[Ad], str]:
    """Navbatdagi providerlar orqali qidiradi. Qaytaradi: (reklamalar, provider nomi).
    Hammasi muvaffaqiyatsiz bo'lsa -- oxirgi xatoni ko'taradi."""
    chain = provider_chain(provider)
    if not chain:
        raise ProviderNotConfigured(
            "Hech qaysi Ad Library provider sozlanmagan (AD_LIBRARY_PROVIDER, ADLIB_SCRAPER, "
            "APIFY_TOKEN yoki META_ACCESS_TOKEN)."
        )
    query.limit = max(1, min(int(query.limit or 30), MAX_LIMIT))
    last_err: "AdLibraryError | None" = None
    for prov in chain:
        if not prov.is_configured():
            last_err = ProviderNotConfigured(f"{prov.label} sozlanmagan", provider=prov.name)
            continue
        try:
            ads = prov.search(query)
        except AdLibraryError as e:
            e.provider = e.provider or prov.name
            logger.warning("ad_library: %s provider xatosi: %s", prov.name, e)
            last_err = e
            continue
        except Exception as e:  # noqa: BLE001 -- kutilmagan xato keyingi providerni to'xtatmasin
            logger.exception("ad_library: %s provider kutilmagan xato", prov.name)
            last_err = AdLibraryError(str(e)[:200], provider=prov.name)
            continue
        for ad in ads:
            ad.provider = ad.provider or prov.name
        return _post_filter(ads, query), prov.name
    raise last_err or AdLibraryError("Qidiruv amalga oshmadi")


def _post_filter(ads: list[Ad], query: AdQuery) -> list[Ad]:
    """Page URL (vanity) bilan qidirilganda -- faqat o'sha sahifaning reklamalari
    (topilmasa, kalit so'z natijasi o'zgarishsiz qaytadi)."""
    if query.active_only:
        ads = [a for a in ads if a.is_active]
    if query.page_vanity:
        v = query.page_vanity.lower()
        exact = [a for a in ads if a.page_profile_uri and f"/{v}/" in a.page_profile_uri.lower().rstrip("/") + "/"]
        if exact:
            ads = exact
    return ads[: query.limit]
