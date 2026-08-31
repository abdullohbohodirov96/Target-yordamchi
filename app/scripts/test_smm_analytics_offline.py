"""test_smm_analytics_offline.py — `smm_analytics.py` uchun TARMOQSIZ
(offline) tekshiruv. Haqiqiy Meta API'ga ULANMAYDI -- vaqtinchalik SQLite
bazasiga to'g'ridan-to'g'ri `SmmPost`/`SmmSnapshot` qatorlarini yozib,
`build_smm_report()`ning HAQIQIY agregatsiya mantig'ini tekshiradi.

Bu 2026-08 (item 6) tuzatishini qamrab oladi: Instagram "shares"/"follows"
metrikalari endi haqiqiy qiymat bilan keladi (avval "shares" doim 0,
"follows" umuman yo'q edi), va "ma'lumot yo'q" (None, masalan Reels uchun
follows) bilan "haqiqatan 0" ANIQ ajratiladi -- shablon buni "—" deb
ko'rsatishi kerak, "0" emas.

Ishga tushirish:
    cd app && python3 scripts/test_smm_analytics_offline.py
"""

import os
import sys
import tempfile
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_db_module(db_path):
    if "db" in sys.modules:
        del sys.modules["db"]
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    import db as db_module
    return db_module


def test_instagram_feed_post_shows_real_shares_and_follows():
    with tempfile.TemporaryDirectory() as tmp:
        db_module = _fresh_db_module(os.path.join(tmp, "t1.db"))
        db_module.init_db()
        import smm_analytics

        session = db_module.get_session()
        try:
            now = dt.datetime.utcnow()
            session.add(db_module.SmmPost(
                platform="instagram", external_id="ig_feed_1", media_type="IMAGE",
                posted_at=now, like_count=100, comments_count=10,
                shares_count=7, saved_count=15, follows_count=3,
                reach=2000, impressions=2500,
            ))
            session.commit()

            report = smm_analytics.build_smm_report(session, days=30)
            ig = report["platforms"]["instagram"]
            assert ig["total_shares"] == 7, f"kutilgan 7 ta share, olindi: {ig['total_shares']}"
            assert ig["total_saved"] == 15
            assert ig["total_follows"] == 3, f"kutilgan 3 ta yangi obunachi, olindi: {ig['total_follows']}"
            assert ig["follows_missing_count"] == 0
            post_dict = ig["top_posts"][0]
            assert post_dict["follows_count"] == 3
        finally:
            session.close()
    print("OK: Instagram FEED post uchun shares/follows haqiqiy qiymat bilan agregatsiya qilinadi (avval shares doim 0, follows umuman yo'q edi)")


def test_instagram_reels_missing_follows_shown_as_none_not_zero():
    # REELS uchun Meta "follows" metrikasini UMUMAN bermaydi -- bu holni
    # `smm_sync.py` `follows_count=None` deb saqlaydi (0 EMAS). Bu test
    # shuni tasdiqlaydi: aralash (bitta FEED bilan follows, bitta REELS
    # follows'siz) holatda ham umumiy son FAQAT mavjud qiymatlardan
    # hisoblanadi va "qancha postda yo'qligi" alohida hisoblanadi.
    with tempfile.TemporaryDirectory() as tmp:
        db_module = _fresh_db_module(os.path.join(tmp, "t2.db"))
        db_module.init_db()
        import smm_analytics

        session = db_module.get_session()
        try:
            now = dt.datetime.utcnow()
            session.add(db_module.SmmPost(
                platform="instagram", external_id="ig_feed_2", media_type="IMAGE",
                posted_at=now, like_count=50, comments_count=5,
                shares_count=2, saved_count=8, follows_count=4,
                reach=1000, impressions=1200,
            ))
            session.add(db_module.SmmPost(
                platform="instagram", external_id="ig_reel_1", media_type="REEL",
                posted_at=now, like_count=500, comments_count=40,
                shares_count=20, saved_count=60, follows_count=None,  # REELS -- Meta bermaydi
                reach=9000, impressions=15000,
            ))
            session.commit()

            report = smm_analytics.build_smm_report(session, days=30)
            ig = report["platforms"]["instagram"]
            assert ig["total_follows"] == 4, f"faqat mavjud (FEED) qiymatdan hisoblanishi kerak, olindi: {ig['total_follows']}"
            assert ig["follows_missing_count"] == 1, "REELS posti 'follows ma'lumoti yo'q' deb hisoblanishi kerak"

            by_id = {p["external_id"]: p for p in ig["top_posts"]}
            assert by_id["ig_reel_1"]["follows_count"] is None, "REELS uchun follows_count None bo'lishi kerak (0 emas)"
            assert by_id["ig_feed_2"]["follows_count"] == 4
        finally:
            session.close()
    print("OK: REELS uchun follows=None ('ma'lumot yo'q') haqiqiy 0'dan TO'G'RI ajratiladi, umumiy son faqat mavjud qiymatlardan hisoblanadi")


def test_facebook_has_no_follows_or_saved_concept():
    # Facebook'da post darajasida "yangi obunachi" va "saqlangan" tushunchasi
    # UMUMAN yo'q -- ikkalasi ham har doim None bo'lishi, va umumiy
    # ko'rsatkich ham shunga mos "ma'lumot yo'q" (None) bo'lishi kerak,
    # "0" emas (aks holda "hech kim ulashmadi" deb noto'g'ri o'qiladi).
    with tempfile.TemporaryDirectory() as tmp:
        db_module = _fresh_db_module(os.path.join(tmp, "t3.db"))
        db_module.init_db()
        import smm_analytics

        session = db_module.get_session()
        try:
            now = dt.datetime.utcnow()
            session.add(db_module.SmmPost(
                platform="facebook", external_id="fb_post_1", media_type="STATUS",
                posted_at=now, like_count=20, comments_count=3,
                shares_count=5, saved_count=None, follows_count=None,
                reach=800, impressions=800,
            ))
            session.commit()

            report = smm_analytics.build_smm_report(session, days=30)
            fb = report["platforms"]["facebook"]
            assert fb["total_shares"] == 5, "Facebook share -- haqiqiy /posts maydoni, bu HAR DOIM ishlashi kerak"
            assert fb["total_follows"] is None, "Facebook uchun follows umuman mavjud emas -- None bo'lishi kerak"
            assert fb["total_saved"] == 0, "saved_count None bo'lganda yig'indi 0 (Facebook uchun bu ustun ishlatilmaydi)"
        finally:
            session.close()
    print("OK: Facebook uchun 'yangi obunachi'/'saqlangan' None sifatida to'g'ri ishlanadi (mavjud bo'lmagan metrika sifatida)")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
