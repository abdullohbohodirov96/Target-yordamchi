"""
smm_analytics.py — `db.SmmSnapshot` / `db.SmmPost` yozuvlaridan "SMM
hisobot" sahifasi (`/smm`) uchun to'liq tahlilni yig'adi: obunachilar
o'sish grafigi, davr bo'yicha umumiy qamrov/like/comment, engagement rate
va eng yaxshi (top) postlar -- Instagram va Facebook uchun alohida.
"""

import datetime as dt

_TASHKENT_OFFSET = dt.timedelta(hours=5)


def _engagement(p) -> int:
    return (p.like_count or 0) + (p.comments_count or 0) + (p.shares_count or 0) + (p.saved_count or 0)


def _post_to_dict(p) -> dict:
    return {
        "external_id": p.external_id,
        "caption": (p.caption or "").strip()[:140],
        "permalink": p.permalink,
        "media_type": p.media_type,
        "posted_at": p.posted_at,
        "like_count": p.like_count or 0,
        "comments_count": p.comments_count or 0,
        "shares_count": p.shares_count or 0,
        "saved_count": p.saved_count or 0,
        "reach": p.reach,
        "impressions": p.impressions,
        "engagement": _engagement(p),
    }


def _build_platform_report(session, platform: str, days: int) -> dict:
    from db import SmmSnapshot, SmmPost

    since_date = (dt.datetime.utcnow() + _TASHKENT_OFFSET - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    snapshots = (
        session.query(SmmSnapshot)
        .filter(SmmSnapshot.platform == platform, SmmSnapshot.date >= since_date)
        .order_by(SmmSnapshot.date.asc())
        .all()
    )
    latest_followers = snapshots[-1].followers_count if snapshots else None
    earliest_followers = snapshots[0].followers_count if snapshots else None
    growth = (
        latest_followers - earliest_followers
        if (latest_followers is not None and earliest_followers is not None)
        else None
    )

    all_posts = (
        session.query(SmmPost)
        .filter(SmmPost.platform == platform)
        .order_by(SmmPost.posted_at.desc())
        .limit(50)
        .all()
    )
    cutoff = dt.datetime.utcnow() - dt.timedelta(days=days)
    period_posts = [p for p in all_posts if p.posted_at and p.posted_at >= cutoff]

    total_reach = sum(p.reach or 0 for p in period_posts)
    total_impressions = sum(p.impressions or 0 for p in period_posts)
    total_likes = sum(p.like_count or 0 for p in period_posts)
    total_comments = sum(p.comments_count or 0 for p in period_posts)
    total_shares = sum(p.shares_count or 0 for p in period_posts)
    total_saved = sum(p.saved_count or 0 for p in period_posts)
    total_engagement = total_likes + total_comments + total_shares + total_saved

    engagement_rate = None
    if latest_followers and period_posts:
        avg_engagement_per_post = total_engagement / len(period_posts)
        engagement_rate = round((avg_engagement_per_post / latest_followers) * 100, 2)

    top_posts = sorted(period_posts, key=_engagement, reverse=True)[:10]

    return {
        "has_snapshots": bool(snapshots),
        "has_posts": bool(all_posts),
        "followers_count": latest_followers,
        "media_count": snapshots[-1].media_count if snapshots else None,
        "growth": growth,
        "chart": [{"date": s.date, "followers": s.followers_count or 0} for s in snapshots],
        "posts_count_period": len(period_posts),
        "total_reach": total_reach,
        "total_impressions": total_impressions,
        "total_likes": total_likes,
        "total_comments": total_comments,
        "total_shares": total_shares,
        "total_saved": total_saved,
        "total_engagement": total_engagement,
        "engagement_rate": engagement_rate,
        "top_posts": [_post_to_dict(p) for p in top_posts],
        "recent_posts": [_post_to_dict(p) for p in all_posts[:25]],
    }


def build_smm_report(session, days: int = 30) -> dict:
    """To'liq SMM hisobotini qaytaradi: {"days": N, "platforms":
    {"instagram": {...}, "facebook": {...}}} -- har bir platforma bo'yicha
    yuqoridagi `_build_platform_report()` natijasi."""
    return {
        "days": days,
        "platforms": {
            "instagram": _build_platform_report(session, "instagram", days),
            "facebook": _build_platform_report(session, "facebook", days),
        },
    }
