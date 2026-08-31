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
        "thumbnail_url": p.thumbnail_url,
        "posted_at": p.posted_at,
        "like_count": p.like_count or 0,
        "comments_count": p.comments_count or 0,
        # MUHIM BUG FIX (2026-08): avval `or 0` bilan None'ni "0"ga
        # aylantirardi -- endi reach/follows kabi xom (None-able) qiymat
        # o'tadi, shablon "—" (ma'lumot yo'q) bilan haqiqiy "0"ni ANIQ
        # ajratib ko'rsatadi.
        "shares_count": p.shares_count,
        "saved_count": p.saved_count,
        "follows_count": p.follows_count,
        "reach": p.reach,
        "impressions": p.impressions,
        "engagement": _engagement(p),
    }


def _build_platform_report(session, platform: str, days: int) -> dict:
    from db import SmmSnapshot, SmmPost

    now_tashkent = dt.datetime.utcnow() + _TASHKENT_OFFSET
    since_date = (now_tashkent - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    snapshots = (
        session.query(SmmSnapshot)
        .filter(SmmSnapshot.platform == platform, SmmSnapshot.date >= since_date)
        .order_by(SmmSnapshot.date.asc())
        .all()
    )
    latest_followers = snapshots[-1].followers_count if snapshots else None
    earliest_followers = snapshots[0].followers_count if snapshots else None
    # MUHIM (2026-08, foydalanuvchi topgan chalkashlik -- "O'sish (30 kun):
    # +0" deb ko'rsatilgan, aslida o'sish YO'Q emas, shunchaki hali ikkinchi
    # solishtirish nuqtasi (snapshot) yig'ilmagan edi): agar shu davrda
    # FAQAT bitta (yoki nolta) snapshot bo'lsa, "0" o'rniga `None` qaytaramiz
    # -- shablon buni "—" ("yetarli ma'lumot yo'q") deb ko'rsatadi.
    growth = (
        latest_followers - earliest_followers
        if (len(snapshots) >= 2 and latest_followers is not None and earliest_followers is not None)
        else None
    )

    # "Bu oy qancha obunachi keldi" -- foydalanuvchi aniq shuni so'radi.
    # Tepadagi `days` (7/30/90 kunlik) tanlovdan MUSTAQIL, doim JORIY
    # TAQVIM OYI bo'yicha hisoblanadi -- foydalanuvchi 7 kunlik ko'rinishni
    # tanlagan bo'lsa ham, "bu oy" ko'rsatkichi to'g'ri chiqishi kerak.
    month_start_date = now_tashkent.strftime("%Y-%m-01")
    all_snapshots = (
        session.query(SmmSnapshot)
        .filter(SmmSnapshot.platform == platform)
        .order_by(SmmSnapshot.date.asc())
        .all()
    )
    month_snapshots = [s for s in all_snapshots if s.date >= month_start_date]
    prior_snapshots = [s for s in all_snapshots if s.date < month_start_date]
    growth_month = None
    if month_snapshots:
        latest_month_followers = month_snapshots[-1].followers_count
        if len(month_snapshots) >= 2:
            baseline_followers = month_snapshots[0].followers_count
        elif prior_snapshots:
            # Oy boshida hali snapshot bo'lmasa, oydan OLDINGI eng yaqin
            # kunlik qiymat bilan solishtiramiz (baribir taxminiy emas,
            # haqiqiy kunlik yozuv asosida).
            baseline_followers = prior_snapshots[-1].followers_count
        else:
            baseline_followers = None
        if baseline_followers is not None and latest_month_followers is not None:
            growth_month = latest_month_followers - baseline_followers

    all_posts = (
        session.query(SmmPost)
        .filter(SmmPost.platform == platform)
        .order_by(SmmPost.posted_at.desc())
        .limit(50)
        .all()
    )
    cutoff = dt.datetime.utcnow() - dt.timedelta(days=days)
    period_posts = [p for p in all_posts if p.posted_at and p.posted_at >= cutoff]

    # MUHIM (2026-08, foydalanuvchi so'rovi: "nimadir noto'g'ri bo'lsa nima
    # uchunligini tushuntiring" -- "Umumiy qamrov"/"Umumiy ko'rishlar" har
    # doim "0" ko'rsatib, xuddi hech narsa ishlamayotgandek ko'rinardi,
    # aslida Meta HAR BIR post uchun insights so'ralganda ba'zan ruxsat/
    # cheklov tufayli hech qanday qiymat qaytarmaydi -- `smm_sync.py`da bu
    # holat `reach=None`/`impressions=None` sifatida saqlanadi). Endi "hech
    # birida ma'lumot yo'q" (None) bilan "haqiqatan 0" ni ANIQ ajratamiz --
    # birinchisida shablon "—" + tushuntirish ko'rsatadi, ikkinchisida esa
    # haqiqiy "0" qoladi. Qisman (ba'zi postlarda bor, ba'zilarida yo'q)
    # holat uchun ham nechta post "ko'rinmasligi"ni alohida hisoblaymiz.
    reach_values = [p.reach for p in period_posts if p.reach is not None]
    impressions_values = [p.impressions for p in period_posts if p.impressions is not None]
    total_reach = sum(reach_values) if reach_values else None
    total_impressions = sum(impressions_values) if impressions_values else None
    reach_missing_count = len(period_posts) - len(reach_values)
    impressions_missing_count = len(period_posts) - len(impressions_values)
    total_likes = sum(p.like_count or 0 for p in period_posts)
    total_comments = sum(p.comments_count or 0 for p in period_posts)

    # MUHIM BUG FIX (2026-08, foydalanuvchi shikoyati: "smm haliyam notori
    # ishlayapti"): avval `sum(p.shares_count or 0 ...)` edi -- bu Instagram
    # insights so'rovi BUTUNLAY muvaffaqiyatsiz bo'lib `shares_count`/
    # `saved_count` HAMMASI `None` (ma'lumot yo'q) bo'lganda ham "0" (ya'ni
    # "tasdiqlangan nol repost/saqlash") ko'rsatardi -- reach/follows uchun
    # ALLAQACHON qo'llanilgan "None = ma'lumot yo'q, 0 = tasdiqlangan nol"
    # qoidasi endi bularga ham qo'llanildi.
    shares_values = [p.shares_count for p in period_posts if p.shares_count is not None]
    total_shares = sum(shares_values) if shares_values else None
    shares_missing_count = len(period_posts) - len(shares_values)

    saved_values = [p.saved_count for p in period_posts if p.saved_count is not None]
    total_saved = sum(saved_values) if saved_values else None
    saved_missing_count = len(period_posts) - len(saved_values)

    # "Jami faollik" -- tarkibiy (composite) ko'rsatkich, aniq bitta
    # metrikaning o'zi emas -- shuning uchun bu yerda `None` qismlar 0
    # sifatida hisoblanadi (aks holda insights vaqtincha ishlamay qolganda
    # butun "jami faollik" karta ham "—" bo'lib qolib, hali ISHLAYOTGAN
    # like/comment ma'lumotini ham yashirib qo'yardi).
    total_engagement = total_likes + total_comments + (total_shares or 0) + (total_saved or 0)

    # 2026-08 (item 6, foydalanuvchi so'rovi: "nechta obunachi qo'shildi
    # videodan aniq korsinsin"): `follows_count` HAR BIR post uchun Meta
    # tomonidan berilmasligi mumkin (masalan REELS turi uchun umuman
    # berilmaydi, Facebook'da esa bu ko'rsatkich UMUMAN yo'q) -- shuning
    # uchun reach/impressions'dagi kabi "haqiqiy 0" bilan "ma'lumot yo'q"ni
    # aniq ajratamiz (None -- shablon "—" ko'rsatadi).
    follows_values = [p.follows_count for p in period_posts if p.follows_count is not None]
    total_follows = sum(follows_values) if follows_values else None
    follows_missing_count = len(period_posts) - len(follows_values)

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
        "growth_month": growth_month,
        "chart": [{"date": s.date, "followers": s.followers_count or 0} for s in snapshots],
        "posts_count_period": len(period_posts),
        "total_reach": total_reach,
        "total_impressions": total_impressions,
        "reach_missing_count": reach_missing_count,
        "impressions_missing_count": impressions_missing_count,
        "posts_in_period_count": len(period_posts),
        "total_likes": total_likes,
        "total_comments": total_comments,
        "total_shares": total_shares,
        "shares_missing_count": shares_missing_count,
        "total_saved": total_saved,
        "saved_missing_count": saved_missing_count,
        "total_follows": total_follows,
        "follows_missing_count": follows_missing_count,
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
