"""Foydalanuvchi kiritgan qatorni `AdQuery`ga aylantirish: kompaniya nomi,
Facebook Page URL, Page ID yoki Ad Library havolasi."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from .provider import DEFAULT_COUNTRY, AdQuery

_NUMERIC_ID = re.compile(r"^\d{5,20}$")
# facebook.com'dagi sahifa emas, xizmat yo'llari -- vanity sifatida qabul qilinmaydi.
_RESERVED = {"ads", "profile.php", "pages", "people", "pg", "groups", "watch", "events",
             "marketplace", "login", "share", "sharer", "story.php", "permalink.php", "photo", "photo.php"}


def parse_query(raw: str, *, country: str = DEFAULT_COUNTRY, active_only: bool = True, limit: int = 30) -> AdQuery:
    text = (raw or "").strip()
    country = (country or DEFAULT_COUNTRY).strip().upper()
    if country != "ALL" and not re.fullmatch(r"[A-Z]{2}", country):
        country = DEFAULT_COUNTRY
    q = AdQuery(country=country, active_only=active_only, limit=limit)
    if not text:
        return q
    if _NUMERIC_ID.match(text):
        q.page_id, q.kind = text, "page_id"
        return q
    candidate = text if "://" in text else ("https://" + text if re.match(r"^(www\.|m\.|web\.)?(facebook|fb|instagram)\.com/", text, re.I) else "")
    if candidate:
        u = urlparse(candidate)
        host = (u.netloc or "").lower()
        params = parse_qs(u.query)
        parts = [p for p in u.path.split("/") if p]
        if "facebook.com" in host or host.endswith("fb.com"):
            # Ad Library havolasi: ?view_all_page_id=... yoki ?q=...
            if params.get("view_all_page_id"):
                q.page_id, q.kind = params["view_all_page_id"][0], "page_id"
                return q
            if params.get("id") and _NUMERIC_ID.match(params["id"][0]):
                q.page_id, q.kind = params["id"][0], "page_id"  # profile.php?id=...
                return q
            if params.get("q") and parts[:2] == ["ads", "library"]:
                q.term, q.kind = params["q"][0], "name"
                return q
            # /people/Nom/123..., /pages/Nom/123...
            for p in reversed(parts):
                if _NUMERIC_ID.match(p):
                    q.page_id, q.kind = p, "page_id"
                    return q
            # pg/<vanity>/... yoki to'g'ridan-to'g'ri <vanity>
            if parts and parts[0] == "pg" and len(parts) > 1:
                parts = parts[1:]
            if parts and parts[0].lower() not in _RESERVED:
                vanity = parts[0]
                q.page_vanity, q.term, q.kind = vanity, _vanity_to_term(vanity), "page_url"
                return q
        if "instagram.com" in host and parts:
            q.term, q.kind = _vanity_to_term(parts[0]), "name"
            return q
    q.term, q.kind = text[:120], "name"
    return q


def _vanity_to_term(vanity: str) -> str:
    """`texnomart.uz` -> `texnomart`, `arboss_official` -> `arboss official`."""
    v = re.sub(r"\.(uz|com|ru|official)$", "", vanity, flags=re.I)
    return re.sub(r"[._-]+", " ", v).strip() or vanity
