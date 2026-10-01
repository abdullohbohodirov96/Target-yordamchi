"""ig_benchmark.py -- Instagram raqobatchi statistikasi (2026-10-01,
foydalanuvchi so'rovi: "username yozasiz, u oxirgi 12-30 ta postni oladi,
o'rtacha like/komment/ko'rishlarni hisoblab jadval qilib ko'rsatadi").

Ma'lumot Meta'ning RASMIY Business Discovery API'sidan olinadi -- kompaniya
o'zining ulangan Instagram Business akkaunti nomidan so'raydi. Shuning uchun:
  - faqat OCHIQ Business/Creator akkauntlar ko'rinadi (shaxsiy akkaunt yo'q);
  - Instagram qoidalariga mos, bloklanish xavfi yo'q.
Natija 6 soat keshlanadi (bitta akkauntni qayta-qayta so'rab Meta limitini
yemaslik uchun). Bazaga yozilmaydi -- migratsiya kerak emas.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import statistics
import time

import db
import kv_store
import meta_api

logger = logging.getLogger("ig-benchmark")

CACHE_TTL_SECONDS = 6 * 3600
MIN_POSTS, MAX_POSTS, DEFAULT_POSTS = 12, 30, 20
MAX_USERNAMES = 5
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


class BenchmarkError(Exception):
    """Foydalanuvchiga ko'rsatsa bo'ladigan (tozalangan) xato."""


def parse_usernames(raw: str) -> list[str]:
    """"@a, b  https://instagram.com/c/" -> ["a", "b", "c"] (takrorsiz, max 5)."""
    out: list[str] = []
    for part in re.split(r"[\s,;]+", raw or ""):
        part = part.strip()
        if not part:
            continue
        m = re.search(r"instagram\.com/([^/?#]+)", part)
        if m:
            part = m.group(1)
        part = part.lstrip("@").strip("/").lower()
        if _USERNAME_RE.match(part) and part not in out:
            out.append(part)
    return out[:MAX_USERNAMES]


def clamp_limit(raw) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_POSTS
    return max(MIN_POSTS, min(MAX_POSTS, n))


def _avg(values: list) -> "float | None":
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def _median(values: list) -> "float | None":
    vals = [v for v in values if v is not None]
    return round(statistics.median(vals), 1) if vals else None


def _post_kind(m: dict) -> str:
    if (m.get("media_product_type") or "").upper() == "REELS":
        return "Reels"
    return {"VIDEO": "Video", "CAROUSEL_ALBUM": "Karusel", "IMAGE": "Rasm"}.get((m.get("media_type") or "").upper(), "Post")


def compute_stats(profile: dict, limit: int) -> dict:
    """Business Discovery javobidan jadval uchun tayyor statistika."""
    media = ((profile.get("media") or {}).get("data") or [])[:limit]
    followers = profile.get("followers_count") or 0
    posts = []
    for m in media:
        likes = m.get("like_count")  # akkaunt like'larni yashirgan bo'lsa -- yo'q
        comments = m.get("comments_count") or 0
        views = m.get("view_count")
        ts = m.get("timestamp")
        try:
            when = dt.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S") if ts else None
        except ValueError:
            when = None
        engagement = (likes or 0) + comments
        posts.append({
            "kind": _post_kind(m),
            "caption": (m.get("caption") or "").strip().replace("\n", " ")[:90],
            "permalink": m.get("permalink"),
            "timestamp": when,
            "likes": likes,
            "comments": comments,
            "views": views,
            "engagement": engagement,
            "er": round(engagement / followers * 100, 2) if followers else None,
        })

    dates = sorted(p["timestamp"] for p in posts if p["timestamp"])
    posts_per_week = None
    if len(dates) >= 2:
        span_days = max((dates[-1] - dates[0]).total_seconds() / 86400, 1)
        posts_per_week = round(len(dates) / span_days * 7, 1)

    avg_likes = _avg([p["likes"] for p in posts])
    avg_comments = _avg([p["comments"] for p in posts])
    avg_eng = _avg([p["engagement"] for p in posts])
    video_posts = [p for p in posts if p["kind"] in ("Reels", "Video")]
    kinds: dict = {}
    for p in posts:
        kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
    top = sorted(posts, key=lambda p: p["engagement"], reverse=True)[:3]
    return {
        "username": profile.get("username"),
        "name": profile.get("name") or profile.get("username"),
        "biography": (profile.get("biography") or "")[:200],
        "picture": profile.get("profile_picture_url"),
        "followers": followers,
        "following": profile.get("follows_count"),
        "media_count": profile.get("media_count"),
        "posts_analyzed": len(posts),
        "avg_likes": avg_likes,
        "median_likes": _median([p["likes"] for p in posts]),
        "avg_comments": avg_comments,
        "avg_views": _avg([p["views"] for p in video_posts]),
        "video_count": len(video_posts),
        "avg_engagement": avg_eng,
        "engagement_rate": round(avg_eng / followers * 100, 2) if (followers and avg_eng is not None) else None,
        "posts_per_week": posts_per_week,
        "likes_hidden": bool(posts) and all(p["likes"] is None for p in posts),
        "kinds": kinds,
        "top_posts": top,
        "posts": posts,
    }


def _own_ig_account(company) -> "tuple[str, str | None, str | None]":
    """Kompaniyaning O'Z ulangan IG Business akkaunti (so'rov shu nomidan)."""
    if company is None:
        raise BenchmarkError("Kompaniya topilmadi.")
    token = company.get_meta_access_token()
    page_id = company.meta_page_id
    if not token or not page_id:
        raise BenchmarkError(
            "Avval Facebook sahifangiz va Instagram Business akkauntingizni ulang "
            "(Sozlamalar -> Hisoblarni ulash). Raqobatchilar shu akkaunt orqali ko'riladi."
        )
    ig_id = company.ig_business_id
    if not ig_id:
        try:
            ig_id = meta_api.get_instagram_business_account_id(page_id=page_id, access_token=token)
        except Exception as e:  # noqa: BLE001
            raise BenchmarkError(f"Instagram akkauntni aniqlab bo'lmadi: {meta_api.safe_error_message(e)}")
    if not ig_id:
        raise BenchmarkError(
            "Facebook sahifangizga Instagram Business akkaunt ulanmagan. Instagram'da "
            "professional (Business/Creator) akkauntga o'tib, uni sahifaga bog'lang."
        )
    return ig_id, page_id, token


def _friendly_error(username: str, e: Exception) -> str:
    msg = meta_api.safe_error_message(e)
    low = str(getattr(e, "args", [""])[0]).lower() + " " + msg.lower()
    if "cannot be found" in low or "does not exist" in low or "invalid user id" in low or "110" in low:
        return f"@{username} topilmadi yoki u Business/Creator akkaunt emas (shaxsiy akkauntlar ko'rinmaydi)."
    return f"@{username}: {msg}"


def fetch_stats(company, username: str, limit: int = DEFAULT_POSTS, *, force: bool = False) -> dict:
    """Bitta akkaunt statistikasi (kesh bilan). Xatoda BenchmarkError."""
    limit = clamp_limit(limit)
    ig_id, page_id, token = _own_ig_account(company)
    cache_key = f"ig_benchmark:{username}:{limit}:{company.id}"
    if not force:
        try:
            cached = kv_store.get_json(cache_key, default=None)
        except Exception:  # noqa: BLE001
            cached = None
        if isinstance(cached, dict) and time.time() - float(cached.get("_at") or 0) < CACHE_TTL_SECONDS:
            return _revive(cached["stats"])

    try:
        try:
            profile = meta_api.get_instagram_business_discovery(ig_id, username, limit, page_id=page_id, access_token=token)
        except meta_api.MetaAPIError as e:
            # `view_count` maydoni qo'llab-quvvatlanmasa -- ko'rishlarsiz qayta.
            if "view_count" in str(e).lower() or "nonexisting field" in str(e).lower():
                profile = meta_api.get_instagram_business_discovery(
                    ig_id, username, limit, page_id=page_id, access_token=token, with_views=False,
                )
            else:
                raise
    except Exception as e:  # noqa: BLE001
        logger.warning("IG benchmark xatosi (@%s, company=%s): %s", username, company.id, type(e).__name__)
        raise BenchmarkError(_friendly_error(username, e))
    if not profile:
        raise BenchmarkError(f"@{username} topilmadi yoki u Business/Creator akkaunt emas.")

    stats = compute_stats(profile, limit)
    stats["fetched_at"] = dt.datetime.utcnow()
    try:
        kv_store.set_json(cache_key, {"_at": time.time(), "stats": _freeze(stats)})
    except Exception:  # noqa: BLE001
        logger.exception("IG benchmark keshini saqlab bo'lmadi")
    return stats


def _freeze(stats: dict) -> dict:
    """datetime -> ISO (kv JSON uchun)."""
    def conv(v):
        return v.isoformat() if isinstance(v, dt.datetime) else v
    out = {k: conv(v) for k, v in stats.items() if k not in ("posts", "top_posts")}
    out["posts"] = [{k: conv(v) for k, v in p.items()} for p in stats["posts"]]
    out["top_posts"] = [{k: conv(v) for k, v in p.items()} for p in stats["top_posts"]]
    return out


def _revive(stats: dict) -> dict:
    def conv(v):
        if isinstance(v, str) and len(v) >= 19 and v[4] == "-" and v[10] == "T":
            try:
                return dt.datetime.fromisoformat(v)
            except ValueError:
                return v
        return v
    out = {k: conv(v) for k, v in stats.items() if k not in ("posts", "top_posts")}
    out["posts"] = [{k: conv(v) for k, v in p.items()} for p in stats.get("posts", [])]
    out["top_posts"] = [{k: conv(v) for k, v in p.items()} for p in stats.get("top_posts", [])]
    return out


def compare(company, usernames: list[str], limit: int = DEFAULT_POSTS, *, force: bool = False) -> dict:
    """Bir nechta akkaunt -> {"results": [stats...], "errors": [...]}."""
    results, errors = [], []
    for u in usernames[:MAX_USERNAMES]:
        try:
            results.append(fetch_stats(company, u, limit, force=force))
        except BenchmarkError as e:
            errors.append(str(e))
    best = {}
    for key in ("avg_likes", "avg_comments", "avg_views", "engagement_rate", "posts_per_week"):
        vals = [r[key] for r in results if r.get(key) is not None]
        if len(vals) >= 2:
            best[key] = max(vals)
    return {"results": results, "errors": errors, "best": best}
