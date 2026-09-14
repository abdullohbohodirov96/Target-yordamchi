"""test_target_status_and_smm_periods_offline.py — 2026-09, foydalanuvchi
shikoyati: "targeting'da o'chirilgan targetlar yoqilgandek ko'rsatilyapti,
pul sarfi noto'g'ri" + "SMM'da bugun/hafta/oy/yil bo'yicha ko'rish kerak".

Ikki ALOHIDA (lekin bir xatoni ikki joyda tug'diradigan) muammoni
tekshiradi:

  1. `dashboard_data.get_kpis(active_only=True)` ilgari faqat Meta'ning
     `status` maydoniga (obyektning O'ZINING yoqiq/o'chiqligi) qarardi --
     bitta ad o'zi "ACTIVE" bo'lsa ham, uning ustidagi kampaniyasi PAUSED
     bo'lsa, u AMALDA hech narsa ko'rsatmaydi, lekin baribir "yoqilgan"
     ro'yxatida chiqardi. `effective_status` (butun ierarxiyani hisobga
     oladi) endi ustun qo'yiladi.
  2. `smm_analytics.resolve_period()`/`build_smm_report()` endi taqvim
     davrlarini (bugun/shu hafta/o'tgan hafta/shu oy/o'tgan oy/shu yil)
     qo'llab-quvvatlaydi -- eski "so'nggi N kun" aylanma oynasi o'rniga.

Ishga tushirish:
    cd app && python3 scripts/test_target_status_and_smm_periods_offline.py
"""

import os
import sys
import tempfile
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMPDIR = tempfile.mkdtemp()
_DB_PATH = os.path.join(_TMPDIR, "test_status_smm.db")

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("META_ACCESS_TOKEN", "test-dummy-token")
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_test_dummy")
os.environ.setdefault("META_PAGE_ID", "page_test_dummy")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

import db as db_module  # noqa: E402
import dashboard_data  # noqa: E402
import meta_api  # noqa: E402
import smm_analytics  # noqa: E402

db_module.init_db()

_TASHKENT_OFFSET = dt.timedelta(hours=5)


def test_status_dot_uses_effective_status_not_raw_status():
    # 2026-09 YANGILANISH: "faqat yoqilgan" (active_only) FILTRI endi
    # effective_status'ga EMAS, tanlangan DAVRDA haqiqiy delivery (Meta
    # insights'da xarajat/ko'rsatish qatori qaytganligi) bo'lishiga qaraydi
    # -- pastdagi `test_active_only_filters_by_period_delivery_not_current_status`
    # shuni tekshiradi. LEKIN `effective_status` hali ham boshqa ikkita
    # narsa uchun ishlatiladi: (a) har bir qatordagi yashil/kulrang
    # "status-dot" belgisi (`r.status`, hozirgi HAQIQIY holatni ko'rsatadi,
    # ota-kampaniyasi pauza bo'lsa ham obyekt "ACTIVE" deb yozilgan
    # bo'lishi mumkin edi), (b) `active_only=False` (ya'ni "Hammasini
    # ko'rsatish") holatida ham status-dot to'g'ri chizilishi. Shu ikkalasi
    # hali effective_status'ga tayanishini tekshiramiz.
    def fake_get_insights(level, date_preset, fields, access_token=None, ad_account_id=None, **kw):
        return [
            {"campaign_id": "c1", "campaign_name": "Chin holda o'chiq", "spend": 50.0, "impressions": 10, "reach": 8, "actions": []},
            {"campaign_id": "c2", "campaign_name": "Chin holda yoqilgan", "spend": 75.0, "impressions": 20, "reach": 15, "actions": []},
        ]

    def fake_get_account_structure(*a, **kw):
        return {
            "campaigns": [
                {"id": "c1", "name": "Chin holda o'chiq", "status": "ACTIVE", "effective_status": "CAMPAIGN_PAUSED", "objective": "OUTCOME_LEADS"},
                {"id": "c2", "name": "Chin holda yoqilgan", "status": "PAUSED", "effective_status": "ACTIVE", "objective": "OUTCOME_LEADS"},
            ],
            "adsets": [], "ads": [],
        }

    real_get_insights = meta_api.get_insights
    real_get_account_structure = meta_api.get_account_structure
    meta_api.get_insights = fake_get_insights
    meta_api.get_account_structure = fake_get_account_structure
    try:
        result = dashboard_data.get_kpis(
            level="campaign", date_preset="last_30d", active_only=False,
            access_token="tok", ad_account_id="act_unique_for_this_test",
        )
        status_by_id = {r["id"]: r["status"] for r in result["rows"]}
        assert status_by_id.get("c1") == "CAMPAIGN_PAUSED", "c1 uchun status-dot effective_status'ga (ota-kampaniyasi pauza) asoslanishi kerak, o'zining xom 'ACTIVE' maydoniga emas"
        assert status_by_id.get("c2") == "ACTIVE", "c2 uchun status-dot effective_status'ga (haqiqatan yoqilgan) asoslanishi kerak, o'zining xom 'PAUSED' maydoniga emas"
    finally:
        meta_api.get_insights = real_get_insights
        meta_api.get_account_structure = real_get_account_structure
    print("OK: har bir qatorning status-dot/status maydoni hali ham effective_status'ga (haqiqiy ierarxiyaga) qaraydi, obyektning o'z xom status'iga emas")


def test_active_only_filters_by_period_delivery_not_current_status():
    # 2026-09, foydalanuvchi shikoyati (skrinshot bilan): "faqat yoqilgan"
    # filtri avval OBYEKTNING HOZIRGI (joriy, bugungi) statusiga qarardi --
    # foydalanuvchi o'tgan oyni (masalan avgustni) tanlasa-yu, o'sha davrdagi
    # barcha target'lar hozir (sentyabrda) allaqachon pauza/tugatilgan
    # bo'lsa, ro'yxat BUTUNLAY BO'SH chiqib qolardi, garchi o'sha davrda
    # real lidlar/xarajat bo'lgan bo'lsa ham. Endi filtr Meta'dan TANLANGAN
    # DAVR uchun qaytgan insight-qatorlar asosida ishlaydi (`meta_by_id`da
    # borligi = "shu davrda xarajat/ko'rsatish bo'lgan, demak ishlagan"
    # degani) -- xuddi Meta Ads Manager'da davr tanlanganda bo'lgani kabi,
    # joriy effective_status'dan qat'i nazar.
    def fake_get_insights(level, date_preset, fields, access_token=None, ad_account_id=None, **kw):
        return [
            {"campaign_id": "c1", "campaign_name": "Avgustda ishlagan, hozir pauza", "spend": 50.0, "impressions": 10, "reach": 8, "actions": []},
            {"campaign_id": "c2", "campaign_name": "Hozir ham yoqilgan", "spend": 75.0, "impressions": 20, "reach": 15, "actions": []},
        ]

    def fake_get_account_structure(*a, **kw):
        # Ikkalasi ham HOZIR (joriy holatda) pauzada/tugatilgan -- lekin
        # tanlangan davrda ikkalasi ham real xarajat qilgan (yuqorida).
        return {
            "campaigns": [
                {"id": "c1", "name": "Avgustda ishlagan, hozir pauza", "status": "PAUSED", "effective_status": "PAUSED", "objective": "OUTCOME_LEADS"},
                {"id": "c2", "name": "Hozir ham yoqilgan", "status": "PAUSED", "effective_status": "PAUSED", "objective": "OUTCOME_LEADS"},
            ],
            "adsets": [], "ads": [],
        }

    real_get_insights = meta_api.get_insights
    real_get_account_structure = meta_api.get_account_structure
    meta_api.get_insights = fake_get_insights
    meta_api.get_account_structure = fake_get_account_structure
    try:
        result = dashboard_data.get_kpis(
            level="campaign", date_preset="custom", date_from="2026-08-01", date_to="2026-08-31",
            active_only=True, access_token="tok", ad_account_id="act_unique_for_this_test_2",
        )
        ids = {r["id"] for r in result["rows"]}
        assert "c1" in ids, "c1 tanlangan davrda (avgust) real xarajat qilgan -- HOZIRGI statusi pauza bo'lsa ham 'faqat yoqilgan' ro'yxatida chiqishi kerak"
        assert "c2" in ids, "c2 ham tanlangan davrda real xarajat qilgan -- ro'yxatda chiqishi kerak"
        # Pul sarfi (totals.spend) ham ikkalasining yig'indisi bo'lishi kerak --
        # joriy status'ga qarab birortasi ham chiqarib tashlanmasligi kerak.
        assert result["totals"]["spend"] == 125.0, f"kutilgan $125 (c1+c2), olindi: {result['totals']['spend']}"
    finally:
        meta_api.get_insights = real_get_insights
        meta_api.get_account_structure = real_get_account_structure
    print("OK: 'faqat yoqilgan' filtri endi TANLANGAN DAVRDA real delivery bo'lganligiga qaraydi, obyektning HOZIRGI statusiga emas")


def test_resolve_period_calendar_math_is_internally_consistent():
    """Kunlar oralig'i qaysi haftaning/oyning qaysi kunida ishga tushirilishidan
    QAT'IY NAZAR to'g'ri bo'lishi kerak bo'lgan invariantlar -- shuning uchun
    aniq offsetlarga emas, faqat matematik munosabatlarga tayanadi."""
    now_tashkent = dt.datetime.utcnow() + _TASHKENT_OFFSET
    today = now_tashkent.date()

    s, e, _ = smm_analytics.resolve_period("today")
    assert s == e == today.isoformat()

    s_tw, e_tw, _ = smm_analytics.resolve_period("this_week")
    assert e_tw == today.isoformat()
    start_tw_date = dt.date.fromisoformat(s_tw)
    assert (today - start_tw_date).days == today.weekday(), "Shu hafta Dushanba kunidan boshlanishi kerak"

    s_lw, e_lw, _ = smm_analytics.resolve_period("last_week")
    end_lw_date = dt.date.fromisoformat(e_lw)
    start_lw_date = dt.date.fromisoformat(s_lw)
    assert end_lw_date + dt.timedelta(days=1) == start_tw_date, "O'tgan hafta shu haftaning boshlanishidan bir kun oldin tugashi kerak"
    assert (end_lw_date - start_lw_date).days == 6, "O'tgan hafta aynan 7 kun bo'lishi kerak"

    s_tm, e_tm, _ = smm_analytics.resolve_period("this_month")
    assert e_tm == today.isoformat()
    assert s_tm.endswith("-01") and s_tm[:7] == today.isoformat()[:7]

    s_lm, e_lm, _ = smm_analytics.resolve_period("last_month")
    start_tm_date = dt.date.fromisoformat(s_tm)
    end_lm_date = dt.date.fromisoformat(e_lm)
    start_lm_date = dt.date.fromisoformat(s_lm)
    assert end_lm_date + dt.timedelta(days=1) == start_tm_date, "O'tgan oy shu oyning boshlanishidan bir kun oldin tugashi kerak"
    assert start_lm_date.day == 1

    s_ty, e_ty, _ = smm_analytics.resolve_period("this_year")
    assert e_ty == today.isoformat()
    assert s_ty == f"{today.year}-01-01"
    print("OK: SMM taqvim-presetlari (bugun/hafta/o'tgan hafta/oy/o'tgan oy/yil) har doim -- qaysi kunda ishga tushirilishidan qat'iy nazar -- to'g'ri hisoblanadi")


def test_build_smm_report_buckets_posts_by_calendar_preset_not_rolling_window():
    with tempfile.TemporaryDirectory() as tmp:
        if "db" in sys.modules:
            del sys.modules["db"]
        os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(tmp, 't.db')}"
        import db as fresh_db
        fresh_db.init_db()
        import importlib
        import smm_analytics as fresh_smm
        importlib.reload(fresh_smm)

        session = fresh_db.get_session()
        try:
            now = dt.datetime.utcnow()
            # "Hozir" joylashtirilgan post -- bugun/shu hafta/shu oy/shu
            # yil HAMMASIGA kirishi kerak (qaysi kunda ishga tushirilishidan
            # qat'iy nazar).
            session.add(fresh_db.SmmPost(
                platform="instagram", external_id="post_now", media_type="IMAGE",
                posted_at=now, like_count=10, comments_count=1,
                shares_count=0, saved_count=0, follows_count=0,
                reach=100, impressions=120,
            ))
            # O'tgan haftaning ANIQ tugash chegarasida (resolve_period'ning
            # o'zidan olingan, taxminiy offset emas) joylashtirilgan post --
            # "o'tgan hafta"da chiqishi, "shu hafta"da chiqmasligi kerak.
            _, end_lw, _ = fresh_smm.resolve_period("last_week")
            end_lw_noon_utc = dt.datetime.strptime(end_lw, "%Y-%m-%d") + dt.timedelta(hours=12) - _TASHKENT_OFFSET
            session.add(fresh_db.SmmPost(
                platform="instagram", external_id="post_last_week", media_type="IMAGE",
                posted_at=end_lw_noon_utc, like_count=5, comments_count=0,
                shares_count=0, saved_count=0, follows_count=0,
                reach=50, impressions=60,
            ))
            # Juda uzoq o'tmish (400+ kun oldin) -- hech qaysi taqvim
            # preset'ga (shu jumladan "shu yil"ga, agar bugun yil boshiga
            # yaqin bo'lmasa) kirmasligi kerak.
            far_past = now - dt.timedelta(days=400)
            session.add(fresh_db.SmmPost(
                platform="instagram", external_id="post_far_past", media_type="IMAGE",
                posted_at=far_past, like_count=1, comments_count=0,
                shares_count=0, saved_count=0, follows_count=0,
                reach=10, impressions=10,
            ))
            session.commit()

            report_today = fresh_smm.build_smm_report(session, preset="today")
            today_ids = {p["external_id"] for p in report_today["platforms"]["instagram"]["recent_posts"] if p["external_id"] in ("post_now",) or True}
            # recent_posts ro'yxati HAMMA (oxirgi 25 ta) postni ko'rsatadi --
            # davr bo'yicha filtrlangani `posts_count_period`, shuning uchun
            # buni tekshiramiz, `recent_posts`ni emas.
            assert report_today["platforms"]["instagram"]["posts_count_period"] == 1, \
                "'Bugun' faqat 'hozir' joylashtirilgan postni hisoblashi kerak"

            report_lw = fresh_smm.build_smm_report(session, preset="last_week")
            top_ids_lw = {p["external_id"] for p in report_lw["platforms"]["instagram"]["top_posts"]}
            assert "post_last_week" in top_ids_lw, "O'tgan hafta chegarasidagi post 'o'tgan hafta' hisobotida chiqishi kerak"
            assert "post_now" not in top_ids_lw, "'Hozirgi' post 'o'tgan hafta' hisobotida ASLO chiqmasligi kerak"

            report_this_week = fresh_smm.build_smm_report(session, preset="this_week")
            top_ids_tw = {p["external_id"] for p in report_this_week["platforms"]["instagram"]["top_posts"]}
            assert "post_last_week" not in top_ids_tw, "O'tgan haftaning posti 'shu hafta' hisobotiga SIZIB chiqmasligi kerak"

            report_year = fresh_smm.build_smm_report(session, preset="this_year")
            top_ids_year = {p["external_id"] for p in report_year["platforms"]["instagram"]["top_posts"]}
            assert "post_far_past" not in top_ids_year, "400+ kun oldingi post 'shu yil' hisobotiga kirmasligi kerak"
        finally:
            session.close()
    print("OK: SMM hisoboti endi ANIQ taqvim davrlari (bugun/hafta/oy/yil) bo'yicha to'g'ri chegaralanadi, eski 'aylanma oyna' emas")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
