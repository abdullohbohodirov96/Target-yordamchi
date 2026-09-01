"""test_facebook_oauth_connect_offline.py — 2026-09, foydalanuvchi so'rovi:
"boshqa kompaniyalar bitta tugma bilan o'z Facebook/Instagram hisobini
ulasin". Haqiqiy Facebook'ga ULANMAYDI -- `meta_api.oauth_*` funksiyalarini
soxtalashtirib, `app.py`dagi OAuth route'larining HAQIQIY oqimini
(start -> callback -> [ixtiyoriy tanlov] -> Company'ga saqlash) tekshiradi.

Ishga tushirish:
    cd app && python3 scripts/test_facebook_oauth_connect_offline.py
"""

import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMPDIR = tempfile.mkdtemp()
_DB_PATH = os.path.join(_TMPDIR, "test_fb_oauth.db")

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("META_ACCESS_TOKEN", "test-dummy-token")
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_test_dummy")
os.environ.setdefault("META_PAGE_ID", "page_test_dummy")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402
import meta_api  # noqa: E402

app_module.app.config["TESTING"] = True
db_module.init_db()


def _signup(client, *, company_name, admin_username, plan="business"):
    return client.post("/signup", data={
        "company_name": company_name, "admin_username": admin_username,
        "admin_full_name": "", "email": "", "plan": plan,
        "password": "parol123456", "password2": "parol123456",
    }, follow_redirects=True)


def _extract_state(location: str) -> str:
    m = re.search(r"[?&]state=([^&]+)", location)
    assert m, f"OAuth dialog URL'da state topilmadi: {location}"
    return m.group(1)


def test_oauth_not_configured_falls_back_to_manual_form_only():
    real_app_id, real_app_secret = meta_api.META_APP_ID, meta_api.META_APP_SECRET
    meta_api.META_APP_ID, meta_api.META_APP_SECRET = "", ""
    try:
        with app_module.app.test_client() as client:
            _signup(client, company_name="Sozlanmagan MChJ", admin_username="sozlanmagan_admin")
            html = client.get("/connect-accounts").get_data(as_text=True)
            assert "Bitta tugma bilan ulash" not in html, "App ID/Secret yo'q bo'lsa, OAuth tugmasi UMUMAN ko'rsatilmasligi kerak"
            r = client.get("/connect-accounts/facebook/start", follow_redirects=True)
            assert "sozlanmagan" in r.get_data(as_text=True)
    finally:
        meta_api.META_APP_ID, meta_api.META_APP_SECRET = real_app_id, real_app_secret
    print("OK: META_APP_ID/SECRET sozlanmagan bo'lsa, OAuth tugmasi yashirin -- eski qo'lda-token forma yagona yo'l bo'lib qoladi")


def test_single_page_and_account_auto_selected_and_saved():
    meta_api.META_APP_ID, meta_api.META_APP_SECRET = "test_app_id", "test_app_secret"
    meta_api.oauth_exchange_code = lambda code, redirect_uri: "short_lived_token"
    meta_api.oauth_exchange_long_lived = lambda short_token: "long_lived_token_abc"
    meta_api.oauth_list_pages = lambda token: [
        {"id": "page_1", "name": "Mening Sahifam", "instagram_business_account": {"id": "ig_1", "username": "mening_do'konim"}},
    ]
    meta_api.oauth_list_ad_accounts = lambda token: [
        {"id": "act_111", "name": "Asosiy hisob"},
    ]
    meta_api.get_ad_account_pixels = lambda ad_account_id, access_token: []
    try:
        with app_module.app.test_client() as client:
            _signup(client, company_name="Bitta Variant MChJ", admin_username="bitta_variant_admin", plan="business")
            start_resp = client.get("/connect-accounts/facebook/start")
            assert start_resp.status_code == 302
            state = _extract_state(start_resp.headers["Location"])

            cb_resp = client.get(f"/connect-accounts/facebook/callback?code=fake_code&state={state}", follow_redirects=True)
            html = cb_resp.get_data(as_text=True)
            assert "muvaffaqiyatli ulandi" in html

            company = app_module._current_company()
            session = db_module.get_session()
            try:
                c = session.get(db_module.Company, company.id)
                assert c.meta_access_token == "long_lived_token_abc"
                assert c.meta_page_id == "page_1"
                assert c.ig_business_id == "ig_1"
                assert c.meta_ad_account_id == "act_111"
            finally:
                session.close()
    finally:
        meta_api.META_APP_ID, meta_api.META_APP_SECRET = "", ""
    print("OK: bitta sahifa/hisob topilsa, avtomatik tanlanadi va Company'ning meta_access_token/meta_page_id/ig_business_id/meta_ad_account_id maydonlariga to'g'ri saqlanadi")


def test_ad_account_pixel_auto_detected_and_saved_for_capi():
    """2026-09, foydalanuvchi so'rovi ("capi ni ... hammasini avtomatik
    qil"): reklama hisobi ulanganda, unga biriktirilgan Meta Pixel
    Company.meta_pixel_id'ga AVTOMATIK saqlanishi kerak -- foydalanuvchi
    Render'ga qo'lda META_PIXEL_ID qo'shishi shart bo'lmasin."""
    meta_api.META_APP_ID, meta_api.META_APP_SECRET = "test_app_id", "test_app_secret"
    meta_api.oauth_exchange_code = lambda code, redirect_uri: "short_lived_token"
    meta_api.oauth_exchange_long_lived = lambda short_token: "long_lived_token_pixel"
    meta_api.oauth_list_pages = lambda token: [
        {"id": "page_pixel", "name": "Pixel Sahifasi"},
    ]
    meta_api.oauth_list_ad_accounts = lambda token: [
        {"id": "act_pixel_test", "name": "Pixel Hisobi"},
    ]
    meta_api.get_ad_account_pixels = lambda ad_account_id, access_token: (
        [{"id": "pixel_999", "name": "Asosiy Pixel"}] if ad_account_id == "act_pixel_test" else []
    )
    try:
        with app_module.app.test_client() as client:
            _signup(client, company_name="Pixel MChJ", admin_username="pixel_admin", plan="business")
            start_resp = client.get("/connect-accounts/facebook/start")
            state = _extract_state(start_resp.headers["Location"])
            client.get(f"/connect-accounts/facebook/callback?code=fake_code&state={state}", follow_redirects=True)

            company = app_module._current_company()
            session = db_module.get_session()
            try:
                c = session.get(db_module.Company, company.id)
                assert c.meta_pixel_id == "pixel_999", f"Pixel avtomatik saqlanmadi: {c.meta_pixel_id!r}"
            finally:
                session.close()

            settings_html = client.get("/sozlamalar").get_data(as_text=True)
            assert 'badge-color-good">ulangan' in settings_html, "Sozlamalar sahifasida CAPI 'ulangan' deb ko'rsatilishi kerak"
    finally:
        meta_api.META_APP_ID, meta_api.META_APP_SECRET = "", ""
    print("OK: reklama hisobi ulanganda unga biriktirilgan Meta Pixel avtomatik topilib Company.meta_pixel_id'ga saqlanadi, va Sozlamalar sahifasi buni 'ulangan' deb ko'rsatadi")


def test_multiple_pages_require_explicit_choice_and_state_mismatch_is_rejected():
    meta_api.META_APP_ID, meta_api.META_APP_SECRET = "test_app_id", "test_app_secret"
    meta_api.oauth_exchange_code = lambda code, redirect_uri: "short_lived_token"
    meta_api.oauth_exchange_long_lived = lambda short_token: "long_lived_token_xyz"
    meta_api.oauth_list_pages = lambda token: [
        {"id": "page_a", "name": "Filial A", "instagram_business_account": {"id": "ig_a"}},
        {"id": "page_b", "name": "Filial B", "instagram_business_account": None},
    ]
    meta_api.oauth_list_ad_accounts = lambda token: [
        {"id": "act_a", "name": "Hisob A"},
        {"id": "act_b", "name": "Hisob B"},
    ]
    meta_api.get_ad_account_pixels = lambda ad_account_id, access_token: []
    try:
        with app_module.app.test_client() as client:
            _signup(client, company_name="Ko'p Variant MChJ", admin_username="kop_variant_admin", plan="business")

            # Noto'g'ri (mos kelmagan) state -- rad etilishi kerak, HECH
            # QANDAY tanlov saqlanmasligi kerak.
            bad_resp = client.get("/connect-accounts/facebook/callback?code=fake_code&state=notogri_state", follow_redirects=True)
            assert "mos kelmadi" in bad_resp.get_data(as_text=True)

            start_resp = client.get("/connect-accounts/facebook/start")
            state = _extract_state(start_resp.headers["Location"])
            cb_resp = client.get(f"/connect-accounts/facebook/callback?code=fake_code&state={state}", follow_redirects=True)
            html = cb_resp.get_data(as_text=True)
            assert "Qaysi hisobni ulaymiz" in html, "Bir nechta sahifa/hisob bo'lsa, tanlov sahifasi ko'rsatilishi kerak"
            assert "Filial A" in html and "Filial B" in html

            choose_resp = client.post("/connect-accounts/facebook/choose", data={
                "page_id": "page_b", "ad_account_id": "act_a",
            }, follow_redirects=True)
            assert "muvaffaqiyatli ulandi" in choose_resp.get_data(as_text=True)

            company = app_module._current_company()
            session = db_module.get_session()
            try:
                c = session.get(db_module.Company, company.id)
                assert c.meta_page_id == "page_b"
                assert c.ig_business_id is None, "Filial B'ning Instagram akkaunti yo'q edi -- None saqlanishi kerak"
                assert c.meta_ad_account_id == "act_a"
            finally:
                session.close()
    finally:
        meta_api.META_APP_ID, meta_api.META_APP_SECRET = "", ""
    print("OK: bir nechta sahifa/hisob bo'lganda admin aniq tanlaydi (va noto'g'ri OAuth state avtomatik rad etiladi)")


def test_oauth_dialog_url_forces_rerequest_for_new_permissions():
    """BUG FIX (2026-09, jonli sinovda topilgan): foydalanuvchi Facebook
    orqali muvaffaqiyatli ulangandan keyin ham SMM hisobotda "(#10) This
    endpoint requires the 'pages_read_engagement' permission" xatosi
    davom etardi -- sababi, foydalanuvchi bu ilovaga ILGARI (scope
    ro'yxati kengaytirilishidan OLDIN) bir marta ruxsat bergan edi, va
    Facebook standart holatda ilgari ruxsat berilgan foydalanuvchidan
    YANGI qo'shilgan scope'lar uchun QAYTA so'ramaydi. `auth_type=rerequest`
    shuni majburlaydi."""
    real_app_id = meta_api.META_APP_ID
    meta_api.META_APP_ID = "test_app_id"
    try:
        url = meta_api.oauth_dialog_url("https://example.com/callback", "somestate", False)
        assert "auth_type=rerequest" in url, f"OAuth URL'da auth_type=rerequest yo'q: {url}"
        assert "pages_read_engagement" in url
    finally:
        meta_api.META_APP_ID = real_app_id
    print("OK: OAuth dialog URL har doim auth_type=rerequest bilan -- ilgari ulangan foydalanuvchidan ham yangi ruxsatlar qayta so'raladi")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
