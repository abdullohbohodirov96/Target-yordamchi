"""test_smm_sync_offline.py — `smm_sync.py` uchun TARMOQSIZ (offline)
tekshiruv. Haqiqiy Meta API'ga ULANMAYDI -- `meta_api` funksiyalari mock
qilinadi.

2026-08, foydalanuvchi shikoyati asosida qo'shildi: "smm haliyam notori
ishlavoti, videodan nechta obunachi keganini koromayapti". Ildiz sabab
ikkita edi:
  1. Instagram media-insights so'rovi (`get_instagram_media_insights`)
     muvaffaqiyatsiz bo'lganda xato JIM yutilardi (`except: pass`) --
     HAQIQIY sababi (masalan aniq qaysi ruxsat yetishmayotgani) hech
     qayerda ko'rinmasdi. Endi bu xato aniq matn bilan `result["errors"]`ga
     qo'shiladi (shu orqali /smm sahifasidagi "Sinxronizatsiya holati"
     paneliga chiqadi).
  2. `shares_count=insights.get("shares", 0) or 0` -- bu insights so'rovi
     BUTUNLAY ishlamaganda ham (bo'sh {} qaytganda) "0" (soxta
     "tasdiqlangan nol repost") yozib qo'yardi. Endi `insights.get("shares")`
     -- chinakam None qoladi.

Ishga tushirish:
    cd app && python3 scripts/test_smm_sync_offline.py
"""

import os
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_modules(db_path):
    for name in ("db", "kv_store", "smm_sync"):
        sys.modules.pop(name, None)
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["META_ACCESS_TOKEN"] = "tok_test"
    os.environ["META_PAGE_ID"] = "page_test"
    import db as db_module
    db_module.init_db()
    import smm_sync
    return db_module, smm_sync


def _media(media_id, media_type="REEL", media_product_type="REELS"):
    return {
        "id": media_id, "caption": "test", "timestamp": "2026-08-30T10:00:00+0000",
        "permalink": "https://instagram.com/p/x", "media_type": media_type,
        "media_product_type": media_product_type, "media_url": "https://x/img.jpg",
        "like_count": 100, "comments_count": 5,
    }


def test_insights_failure_stores_none_not_false_zero_and_surfaces_error():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, smm_sync = _fresh_modules(os.path.join(tmp, "t1.db"))

        with mock.patch.object(smm_sync.meta_api, "get_instagram_business_account_id", return_value="IG_ID"), \
             mock.patch.object(smm_sync.meta_api, "get_instagram_profile", return_value={"followers_count": 1000, "media_count": 10}), \
             mock.patch.object(smm_sync.meta_api, "get_instagram_media", return_value=[_media("m1")]), \
             mock.patch.object(smm_sync.meta_api, "get_instagram_media_insights",
                                side_effect=smm_sync.meta_api.MetaAPIError({"message": "(#10) permission denied", "code": 10})), \
             mock.patch.object(smm_sync, "_sync_facebook", return_value=None):
            result = smm_sync.sync_once()

        assert result["instagram"]["posts_synced"] == 1
        assert any("statistikasi" in e and "permission denied" in e for e in result["errors"]), (
            f"insights xatosi aniq matn bilan errors'ga chiqishi kerak edi: {result['errors']}"
        )

        session = db_module.get_session()
        post = session.query(db_module.SmmPost).filter_by(external_id="m1").first()
        assert post.shares_count is None, f"insights butunlay ishlamagan bo'lsa shares_count None bo'lishi kerak (soxta 0 emas), olindi: {post.shares_count}"
        assert post.reach is None
        assert post.follows_count is None
        session.close()
    print("OK: Instagram insights so'rovi xato qaytarganda HAQIQIY sabab errors'ga chiqadi va shares_count soxta '0' emas, chinakam None bo'lib qoladi")


def test_insights_success_stores_real_values():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, smm_sync = _fresh_modules(os.path.join(tmp, "t2.db"))

        with mock.patch.object(smm_sync.meta_api, "get_instagram_business_account_id", return_value="IG_ID"), \
             mock.patch.object(smm_sync.meta_api, "get_instagram_profile", return_value={"followers_count": 1000, "media_count": 10}), \
             mock.patch.object(smm_sync.meta_api, "get_instagram_media", return_value=[_media("m2", media_type="IMAGE", media_product_type="FEED")]), \
             mock.patch.object(smm_sync.meta_api, "get_instagram_media_insights",
                                return_value={"reach": 5000, "views": 6000, "shares": 3, "saved": 12, "follows": 2}), \
             mock.patch.object(smm_sync, "_sync_facebook", return_value=None):
            result = smm_sync.sync_once()

        assert result["errors"] == []
        session = db_module.get_session()
        post = session.query(db_module.SmmPost).filter_by(external_id="m2").first()
        assert post.shares_count == 3
        assert post.saved_count == 12
        assert post.follows_count == 2
        assert post.reach == 5000
        assert post.impressions == 6000
        session.close()
    print("OK: insights so'rovi muvaffaqiyatli bo'lganda haqiqiy qiymatlar (0 bo'lsa ham) to'g'ri saqlanadi")


def test_insights_success_with_real_zero_shares_stays_zero():
    # Insights so'rovi ISHLAGAN, lekin Meta haqiqatan "shares: 0" qaytargan
    # holat -- bu HAQIQIY tasdiqlangan nol, None BO'LMASLIGI kerak.
    with tempfile.TemporaryDirectory() as tmp:
        db_module, smm_sync = _fresh_modules(os.path.join(tmp, "t3.db"))

        with mock.patch.object(smm_sync.meta_api, "get_instagram_business_account_id", return_value="IG_ID"), \
             mock.patch.object(smm_sync.meta_api, "get_instagram_profile", return_value={"followers_count": 1000, "media_count": 10}), \
             mock.patch.object(smm_sync.meta_api, "get_instagram_media", return_value=[_media("m3", media_type="IMAGE", media_product_type="FEED")]), \
             mock.patch.object(smm_sync.meta_api, "get_instagram_media_insights",
                                return_value={"reach": 100, "views": 120, "shares": 0, "saved": 0, "follows": 0}), \
             mock.patch.object(smm_sync, "_sync_facebook", return_value=None):
            result = smm_sync.sync_once()

        session = db_module.get_session()
        post = session.query(db_module.SmmPost).filter_by(external_id="m3").first()
        assert post.shares_count == 0, f"haqiqiy tasdiqlangan nol bo'lishi kerak, None emas: {post.shares_count}"
        assert post.saved_count == 0
        assert post.follows_count == 0
        session.close()
    print("OK: Meta haqiqatan '0' qaytarganda (tasdiqlangan nol) qiymat None'ga aylanib qolmaydi")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
