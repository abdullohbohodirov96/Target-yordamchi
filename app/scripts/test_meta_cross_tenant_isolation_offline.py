"""test_meta_cross_tenant_isolation_offline.py — 2026-09, foydalanuvchi
shikoyati: "targeting ma'lumotlari boshqa loyihadan chiqib qolyapti,
masalan boshqa loyihalardagi ma'lumotlar bu loyihaga chiqib qoladi".

HAQIQIY topilgan ikkita sabab:
  1. `meta_api.py` HAR DOIM global ENV o'zgaruvchilardan (`META_ACCESS_TOKEN`/
     `META_AD_ACCOUNT_ID`) foydalanardi -- yangi (hali hech narsa ulamagan)
     kompaniya ham platforma egasining HAQIQIY reklama hisobini ko'rardi.
  2. `dashboard_data.get_kpis()`ning keshi (`_kpi_cache`) `ad_account_id`ni
     kesh kalitiga umuman qo'shmasdi -- ikki turli kompaniya 120 soniya
     ichida Target sahifasini ochsa, ikkinchisi BIRINCHISINING keshlangan
     natijasini ko'rardi.

Bu fayl ikkalasini ham tekshiradi:
  - Meta hisobini ulamagan kompaniya HECH QANDAY Meta so'rovisiz "ulanmagan"
    holatini ko'radi (boshqa hech kimning ma'lumoti emas).
  - Ikki xil kompaniya, ikkalasi ham O'Z hisobini ulagan bo'lsa, bir-
    birining (hatto keshlash oynasi ichida ham) ma'lumotini ASLO ko'rmaydi.

Ishga tushirish:
    cd app && python3 scripts/test_meta_cross_tenant_isolation_offline.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMPDIR = tempfile.mkdtemp()
_DB_PATH = os.path.join(_TMPDIR, "test_meta_isolation.db")

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("META_ACCESS_TOKEN", "owner-real-token")
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_owner_real_account")
os.environ.setdefault("META_PAGE_ID", "page_test_dummy")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402
import dashboard_data  # noqa: E402

app_module.app.config["TESTING"] = True
db_module.init_db()


def _signup(client, *, company_name, admin_username, plan="business"):
    return client.post("/signup", data={
        "company_name": company_name, "admin_username": admin_username,
        "admin_full_name": "", "email": "", "plan": plan,
        "password": "parol123456", "password2": "parol123456",
    }, follow_redirects=True)


def _connect_meta(client, *, ad_account_id, access_token):
    return client.post("/connect-accounts", data={
        "ig_business_id": "", "meta_page_id": "", "meta_ad_account_id": ad_account_id,
        "meta_access_token": access_token,
    }, follow_redirects=True)


def test_unconnected_company_gets_no_meta_call_at_all():
    calls = []
    real_get_kpis = app_module.get_kpis

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real_get_kpis(*args, **kwargs)

    app_module.get_kpis = spy
    try:
        with app_module.app.test_client() as client:
            _signup(client, company_name="Ulanmagan MChJ", admin_username="ulanmagan_admin")
            r = client.get("/target")
            assert r.status_code == 200
            html = r.get_data(as_text=True)
            assert "hisobingiz hali ulanmagan" in html
        assert calls == [], "hisob ulanmagan kompaniya uchun get_kpis() UMUMAN chaqirilmasligi kerak"
    finally:
        app_module.get_kpis = real_get_kpis
    print("OK: Meta hisobi ulanmagan kompaniya uchun HECH QANDAY Meta so'rovi yuborilmaydi (boshqa hisob ma'lumoti sizib chiqmaydi)")


def test_two_companies_own_accounts_never_cross_even_within_cache_window():
    def fake_get_insights(level, date_preset, fields, access_token=None, ad_account_id=None, **kw):
        # Har bir "kompaniya"ning o'z ad_account_id'siga qarab ALOHIDA,
        # bir-biridan farqli soxta natija qaytaradi.
        spend = {"act_company_a": 111.0, "act_company_b": 222.0}.get(ad_account_id, 0.0)
        return [{"campaign_id": "c1", "campaign_name": "Test", "spend": spend, "impressions": 100, "reach": 90, "actions": []}]

    import meta_api
    real_get_insights = meta_api.get_insights
    real_get_account_structure = meta_api.get_account_structure
    def fake_get_account_structure(*a, **kw):
        return {"campaigns": [{"id": "c1", "name": "Test", "status": "ACTIVE", "objective": "OUTCOME_LEADS"}], "adsets": [], "ads": []}

    meta_api.get_insights = fake_get_insights
    meta_api.get_account_structure = fake_get_account_structure
    try:
        with app_module.app.test_client() as client_a:
            _signup(client_a, company_name="Kompaniya A", admin_username="kompaniya_a_admin")
            _connect_meta(client_a, ad_account_id="act_company_a", access_token="token_a")
            html_a = client_a.get("/target").get_data(as_text=True)
            assert "111.00" in html_a, "Kompaniya A o'z ($111) xarajatini ko'rishi kerak"
            assert "222.00" not in html_a, "Kompaniya A Kompaniya B'ning ma'lumotini ASLO ko'rmasligi kerak"

        # Darhol (kesh TTL -- 120s -- ichida) ikkinchi kompaniya so'raydi.
        with app_module.app.test_client() as client_b:
            _signup(client_b, company_name="Kompaniya B", admin_username="kompaniya_b_admin")
            _connect_meta(client_b, ad_account_id="act_company_b", access_token="token_b")
            html_b = client_b.get("/target").get_data(as_text=True)
            assert "222.00" in html_b, "Kompaniya B o'z ($222) xarajatini ko'rishi kerak"
            assert "111.00" not in html_b, "Kompaniya B Kompaniya A'ning (keshlangan!) ma'lumotini ko'rmasligi kerak"
    finally:
        meta_api.get_insights = real_get_insights
        meta_api.get_account_structure = real_get_account_structure
    print("OK: ikki kompaniya BIR XIL 120s kesh oynasida ham bir-birining Meta ma'lumotini ASLO ko'rmaydi")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
