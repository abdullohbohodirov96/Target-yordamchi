"""adlib_scraper.py — Meta Ad Library'ning OMMAVIY veb-sahifasini serverdagi
yashirin (headless) brauzer orqali o'qib, raqobatchi reklamalarini olish
(2026-10, foydalanuvchi so'rovi: "dunyabunya qidirsa malumoti ad librarydan
olib backda ishlab manga frontda chiqaradigan qilsa").

NEGA KERAK: rasmiy Ad Library API (`ads_archive`) O'zbekistondagi oddiy
(tijoriy) reklamalarni BERMAYDI -- faqat siyosiy va Yevropadagilarni. Veb-
sahifada esa hammasi ko'rinadi (video, matn, boshlangan sana, tugma).

XAVFSIZLIK/RESURS (egasi bilan kelishilgan):
  - Standart O'CHIQ: faqat `ADLIB_SCRAPER=1` bo'lsa ishlaydi.
  - Bir vaqtda faqat BITTA brauzer (lock), har so'rov ko'pi bilan ~40 s,
    rasm/video/shrift yuklanmaydi (xotira tejash), so'rovdan keyin brauzer
    darhol yopiladi.
  - Natija 6 soat keshlanadi -- bir xil qidiruv brauzerni qayta ochmaydi.
  - Meta to'siq/captcha qo'ysa `AdLibraryBlocked` -- chaqiruvchi rasmiy API'ga
    yoki "Ad Library'da ochish" tugmasiga qaytadi.

Natija `meta_api.search_ad_library` bilan BIR XIL formatda (id, page_id,
page_name, ad_creative_bodies, ad_snapshot_url, ad_delivery_start_time,
ad_delivery_stop_time) + qo'shimcha maydonlar (display_format, video_url,
preview_image_url, cta_text, link_url, platforms, page_picture_url,
page_like_count)."""

import datetime as dt
import json
import logging
import os
import threading
import time
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 6 * 3600
CACHE_MAX_ENTRIES = 64
PAGE_TIMEOUT_MS = 40_000
LOCK_WAIT_SECONDS = 45
MAX_SCROLLS = 2
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
_RESULTS_KEY = '"search_results_connection":'

_lock = threading.Lock()
_cache: dict = {}


class AdLibraryBlocked(Exception):
    """Meta captcha/to'siq qo'ydi yoki sahifa o'qilmadi."""


def is_enabled() -> bool:
    return os.environ.get("ADLIB_SCRAPER", "").strip().lower() in ("1", "true", "yes")


def library_url(terms: str, country: str = "UZ", active_only: bool = True) -> str:
    return "https://www.facebook.com/ads/library/?" + urlencode({
        "active_status": "active" if active_only else "all", "ad_type": "all", "country": country,
        "q": terms, "search_type": "keyword_unordered", "media_type": "all",
    })


def _iso(epoch) -> "str | None":
    try:
        return dt.datetime.utcfromtimestamp(int(epoch)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _text(v) -> str:
    if isinstance(v, dict):
        v = v.get("text")
    v = (v or "").strip() if isinstance(v, str) else ""
    return "" if v.startswith("{{") and v.endswith("}}") else v


def normalize(node: dict) -> "dict | None":
    """Sahifa JSON'idagi bitta `collated_results` elementi -> ads_archive formati."""
    ad_id = node.get("ad_archive_id")
    if not ad_id:
        return None
    s = node.get("snapshot") or {}
    cards = s.get("cards") or []
    videos = s.get("videos") or []
    images = s.get("images") or []
    body = _text(s.get("body")) or next((_text(c.get("body")) for c in cards if _text(c.get("body"))), "")
    title = _text(s.get("title")) or next((_text(c.get("title")) for c in cards if _text(c.get("title"))), "")
    video = videos[0] if videos else next((c for c in cards if c.get("video_sd_url") or c.get("video_hd_url")), None)
    preview = None
    if video:
        preview = video.get("video_preview_image_url")
    if not preview and images:
        preview = images[0].get("resized_image_url") or images[0].get("original_image_url")
    if not preview:
        preview = next((c.get("resized_image_url") or c.get("video_preview_image_url") for c in cards
                        if c.get("resized_image_url") or c.get("video_preview_image_url")), None)
    is_active = bool(node.get("is_active"))
    return {
        "id": str(ad_id),
        "page_id": str(node.get("page_id") or s.get("page_id") or "") or None,
        "page_name": node.get("page_name") or s.get("page_name"),
        "ad_creative_bodies": [body] if body else ([title] if title else []),
        "ad_creative_link_titles": [title] if title else [],
        "ad_snapshot_url": f"https://www.facebook.com/ads/library/?id={ad_id}",
        "ad_delivery_start_time": _iso(node.get("start_date")),
        "ad_delivery_stop_time": None if is_active else _iso(node.get("end_date")),
        "display_format": s.get("display_format"),
        "video_url": (video or {}).get("video_sd_url") or (video or {}).get("video_hd_url"),
        "preview_image_url": preview,
        "cta_text": s.get("cta_text"),
        "link_url": s.get("link_url"),
        "platforms": node.get("publisher_platform") or [],
        "page_picture_url": s.get("page_profile_picture_url"),
        "page_like_count": s.get("page_like_count"),
        "collation_count": node.get("collation_count") or 1,
    }


def parse_results(text: str) -> list[dict]:
    """HTML yoki GraphQL javobidagi barcha `search_results_connection`
    bloklaridan reklamalarni ajratadi (takrorlarsiz)."""
    if '"xfb_ad_library_is_captcha_required":true' in text:
        raise AdLibraryBlocked("captcha")
    out, seen = [], set()
    decoder = json.JSONDecoder()
    start = 0
    while True:
        i = text.find(_RESULTS_KEY, start)
        if i < 0:
            break
        start = i + len(_RESULTS_KEY)
        try:
            obj, _ = decoder.raw_decode(text[start:])
        except ValueError:
            continue
        for edge in (obj or {}).get("edges") or []:
            for node in ((edge or {}).get("node") or {}).get("collated_results") or []:
                ad = normalize(node)
                if ad and ad["id"] not in seen:
                    seen.add(ad["id"])
                    out.append(ad)
    return out


def _fetch(terms: str, country: str, limit: int) -> list[dict]:
    from playwright.sync_api import sync_playwright  # faqat yoqilganda kerak

    launch_kwargs = {"args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]}
    exe = os.environ.get("ADLIB_CHROMIUM_PATH", "").strip()
    if exe:
        launch_kwargs["executable_path"] = exe
    if os.environ.get("ADLIB_IGNORE_HTTPS_ERRORS"):  # faqat sinov muhiti (MITM proksi)
        launch_kwargs["args"].append("--ignore-certificate-errors")
    proxy = os.environ.get("ADLIB_PROXY", "").strip()
    if proxy:
        launch_kwargs["proxy"] = {"server": proxy}
    extra_chunks: list[str] = []

    def on_response(resp):
        if "/api/graphql" in resp.url:
            try:
                body = resp.text()
            except Exception:  # noqa: BLE001
                return
            if _RESULTS_KEY in body:
                extra_chunks.append(body)

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
        try:
            page = browser.new_page(user_agent=USER_AGENT, locale="en-US",
                                    ignore_https_errors=bool(os.environ.get("ADLIB_IGNORE_HTTPS_ERRORS")))
            page.route("**/*", lambda r: r.abort() if r.request.resource_type in ("image", "media", "font", "stylesheet") else r.continue_())
            page.on("response", on_response)
            page.goto(library_url(terms, country), wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            # Birinchi javob ko'pincha bot-tekshiruv sahifasi: u o'zi POST qilib
            # sahifani qayta yuklaydi -- natija bloki paydo bo'lguncha kutamiz.
            html = ""
            deadline = time.time() + 20
            while time.time() < deadline:
                page.wait_for_timeout(1000)
                try:
                    html = page.content()
                except Exception:  # noqa: BLE001 -- navigatsiya paytida
                    continue
                if _RESULTS_KEY in html or '"xfb_ad_library_is_captcha_required":true' in html:
                    break
            if _RESULTS_KEY not in html:
                raise AdLibraryBlocked("natija sahifasi yuklanmadi")
            ads = parse_results(html)
            scrolls = 0
            while len(ads) < limit and scrolls < MAX_SCROLLS:
                scrolls += 1
                page.mouse.wheel(0, 20000)
                page.wait_for_timeout(2000)
                for chunk in extra_chunks:
                    for ad in parse_results(chunk):
                        if all(ad["id"] != a["id"] for a in ads):
                            ads.append(ad)
                extra_chunks.clear()
            if not ads and "/login" in page.url:
                raise AdLibraryBlocked("login talab qilindi")
            return ads[:limit]
        finally:
            browser.close()


def search(terms: str, country: str = "UZ", limit: int = 30) -> list[dict]:
    """Keshli qidiruv. Xato/to'siqda `AdLibraryBlocked` ko'taradi."""
    terms = (terms or "").strip()
    if not terms:
        return []
    key = (terms.lower(), country, limit)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    if not _lock.acquire(timeout=LOCK_WAIT_SECONDS):
        raise AdLibraryBlocked("band (boshqa qidiruv ketmoqda)")
    try:
        hit = _cache.get(key)  # kutish paytida boshqa so'rov to'ldirgan bo'lishi mumkin
        if hit and time.time() - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
        try:
            ads = _fetch(terms, country, limit)
        except AdLibraryBlocked:
            raise
        except Exception as e:  # noqa: BLE001 -- brauzer/tarmoq xatosi
            logger.warning("adlib_scraper: %s uchun o'qib bo'lmadi: %s", terms, e)
            raise AdLibraryBlocked(str(e)[:200]) from e
        if len(_cache) >= CACHE_MAX_ENTRIES:
            _cache.pop(min(_cache, key=lambda k: _cache[k][0]), None)
        _cache[key] = (time.time(), ads)
        return ads
    finally:
        _lock.release()
