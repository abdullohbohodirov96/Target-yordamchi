"""test_company_context_offline.py — Meta Ads Autopilot (2026-09,
foydalanuvchi so'rovi: "Replix ... Ads Manager'dagi har bir maydonni
boshidan o'zi to'ldirmasligi kerak"): `company_context.py` --
`build_company_context()` va `company_context_prompt_block()`:

  1. Profilsiz kompaniya -> barcha `missing_fields`, default_location None.
  2. Profilli kompaniya -> yo'nalish/javoblar kontekstda, "Toshkent
     shahri" javobidan standart hudud "Tashkent" ajratiladi, missing_fields
     faqat haqiqatan bo'shlar.
  3. Prompt bloki sarlavha + yo'nalish + javoblar + aktivlarni o'z ichiga oladi.
  4. So'nggi natijalar (Lead soni) sessiya berilsa hisoblanadi, xato bo'lsa
     kontekst baribir qaytadi.

Ishga tushirish:
    cd app && python3 scripts/test_company_context_offline.py
"""
import os
import sys
import json
import tempfile
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("META_ACCESS_TOKEN", "test-dummy-token")
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_test_dummy")
os.environ.setdefault("META_PAGE_ID", "page_test_dummy")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

_TMPDIR = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMPDIR, 'test_company_context.db')}"

import db as db_module  # noqa: E402
import company_context  # noqa: E402

db_module.init_db()

failures = []


def check(name, cond):
    print(("OK  " if cond else "FAIL") + " " + name)
    if not cond:
        failures.append(name)


def test_empty_profile():
    session = db_module.get_session()
    try:
        c = db_module.Company(name="Bo'sh MChJ", plan="trial", is_active=True)
        session.add(c)
        session.commit()
        ctx = company_context.build_company_context(c, session)
    finally:
        session.close()
    check("company_name", ctx["company_name"] == "Bo'sh MChJ")
    check("business_category bo'sh", ctx["business_category"] == {"key": None, "label": None})
    check("missing_fields hammasi", set(ctx["missing_fields"]) == {"business_category", "product_or_service", "target_audience", "price_range", "location"})
    check("default_location None", ctx["default_location"] is None)
    check("meta_assets bool'lar False", not ctx["meta_assets"]["has_page"] and not ctx["meta_assets"]["has_ad_account"])
    check("profile_summary_text None", ctx["profile_summary_text"] is None)
    block = company_context.company_context_prompt_block(ctx)
    check("prompt sarlavha", block.startswith("# KOMPANIYA KONTEKSTI (HAR DOIM shundan kelib chiq, umumiy javob berma)"))
    check("prompt to'ldirilmagan ro'yxati", "TO'LDIRILMAGAN" in block)
    ctx_none = company_context.build_company_context(None)
    check("None kompaniya -> bo'sh kontekst", ctx_none["company_id"] is None and ctx_none["missing_fields"])


def test_filled_profile_and_performance():
    session = db_module.get_session()
    try:
        c = db_module.Company(
            name="Nur Mebel", plan="business", is_active=True,
            business_category="furniture_interior", business_category_note="Oshxona mebeli",
            business_profile_answers=json.dumps({
                "product_or_service": "Oshxona mebeli, buyurtma asosida",
                "target_audience": "25-45 yosh, ko'proq ayollar, Toshkent shahri",
                "price_range": "3 mln - 15 mln so'm",
                "best_seller": "Burchak oshxona",
            }, ensure_ascii=False),
            meta_ad_account_id="act_1", meta_page_id="PAGE1", ig_business_id="IG1", meta_pixel_id="PX1",
        )
        c.set_meta_access_token("tok")
        session.add(c)
        session.commit()
        now = dt.datetime.utcnow()
        with db_module.scoped_as(c.id):
            for i in range(5):
                session.add(db_module.Lead(company_id=c.id, full_name=f"L{i}", phone=f"+99890000000{i}", ad_name="Video A" if i < 3 else "Rasm B", created_at=now - dt.timedelta(days=2)))
            session.add(db_module.Lead(company_id=c.id, full_name="Eski", phone="+998900000099", ad_name="Eski", created_at=now - dt.timedelta(days=90)))
            session.commit()
        ctx = company_context.build_company_context(c, session)
    finally:
        session.close()
    check("category label", ctx["business_category"] == {"key": "furniture_interior", "label": "Mebel / interyer"})
    check("default_location Tashkent", ctx["default_location"] == "Tashkent")
    check("missing_fields faqat bo'shlar yo'q", ctx["missing_fields"] == [])
    check("profile_answers barcha kalitlar", set(ctx["profile_answers"].keys()) >= {"product_or_service", "target_audience", "sku_count", "price_range", "best_seller", "competitors", "extra_notes"})
    check("meta_assets ids", ctx["meta_assets"]["page_id"] == "PAGE1" and ctx["meta_assets"]["has_instagram"] and ctx["meta_assets"]["has_pixel"])
    perf = ctx["recent_performance"]
    check("leads_last_30d = 5", perf["leads_last_30d"] == 5)
    check("top_ads birinchi Video A (3)", perf["top_ads"] and perf["top_ads"][0] == {"ad_name": "Video A", "leads": 3})
    block = company_context.company_context_prompt_block(ctx)
    check("prompt yo'nalish", "Mebel / interyer" in block and "Oshxona mebeli" in block)
    check("prompt javoblar", "3 mln - 15 mln" in block and "Toshkent shahri" in block)
    check("prompt standart hudud", "Standart hudud (profil bo'yicha): Tashkent" in block)
    check("prompt aktivlar", "reklama hisobi ULANGAN" in block and "Instagram ULANGAN" in block)
    check("prompt natijalar", "5 ta lead" in block and "Video A (3)" in block)
    check("prompt to'ldirilmagan yo'q", "TO'LDIRILMAGAN" not in block)


def test_location_extraction():
    ex = company_context.extract_default_location
    check("Toshkent shahri", ex("20-40 yosh, Toshkent shahri") == "Tashkent")
    check("kirill Самарканд", ex("Самарканд ва Бухоро") == "Samarkand")
    check("birinchi uchragani", ex("Chirchiq va Toshkent") == "Chirchiq")
    check("topilmasa None", ex("18-25 yosh, talabalar") is None)
    check("bo'sh None", ex(None) is None)


def test_performance_error_tolerated():
    class Broken:
        def query(self, *a, **k):
            raise RuntimeError("db down")
    session = db_module.get_session()
    try:
        c = db_module.Company(name="X", plan="trial", is_active=True)
        session.add(c)
        session.commit()
        ctx = company_context.build_company_context(c, Broken())
    finally:
        session.close()
    check("natija xatosi kontekstni buzmaydi", ctx["recent_performance"] == {"leads_last_30d": None, "top_ads": []} and ctx["company_name"] == "X")


test_empty_profile()
test_filled_profile_and_performance()
test_location_extraction()
test_performance_error_tolerated()

print()
if failures:
    print(f"MUVAFFAQIYATSIZ: {len(failures)} ta test o'tmadi:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("BARCHA TESTLAR O'TDI (company_context)")
