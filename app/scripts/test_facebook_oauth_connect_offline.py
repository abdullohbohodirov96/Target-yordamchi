"""test_facebook_oauth_connect_offline.py — 2026-09, foydalanuvchi so'rovi:
"boshqa kompaniyalar bitta tugma bilan o'z Facebook/Instagram hisobini
ulasin". Haqiqiy Facebook'ga ULANMAYDI -- `meta_api.oauth_*` funksiyalarini
soxtalashtirib, `app.py`dagi OAuth route'larining HAQIQIY oqimini
(start -> callback -> [ixtiyoriy tanlov] -> Company'ga saqlash) tekshiradi.

2026-09 YANGILANISH ("production-ready Meta Ads + CAPI integration"
so'rovi): `meta_api.oauth_exchange_long_lived()` endi
`(token, expires_in)` juftini qaytaradi.

2026-09, JONLI BUG TUZATISHI (foydalanuvchi skrinshot bilan xabar berdi --
"avtomaticheskiy ulanib ketmayapti"): reklama scope so'ralganda
(ads/business tarif) HAR BIR darajada (sahifa, Business, reklama hisobi)
aynan bitta yoki nolta nomzod bo'lsa -- ENDI hech qanday tanlov ekransiz
to'liq avtomatik ulanadi; tanlov ekrani FAQAT haqiqiy noaniqlik (2+
Business yoki 2+ reklama hisobi) bo'lganda ko'rsatiladi. Bu fayl shu
xulqni aks ettiradi.

Ishga tushirish:
    cd app && python3 scripts/test_facebook_oauth_connect_offline.py
"""

import os
import re
import sys
import tempfile
import unittest.mock as mock

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
import lead_sync  # noqa: E402


class _SyncThread:
    """`threading.Thread`ning DETERMINISTIK o'rinbosari -- `.start()`
    target'ni FON OQIMIDA emas, DARHOL joriy oqimda chaqiradi. Fon
    oqimidagi haqiqiy vaqt kutish/race sharti bo'lmasin uchun testlarda
    ishlatiladi (`_run_initial_lead_sync`ning chaqirilganini tekshirish)."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)

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


def _stub_no_business_assets():
    """Ko'pchilik test uchun Business Portfolio topilmadi deb faraz
    qilamiz -- shunda oqim eski (fallback `/me/adaccounts`) hisoblar
    ro'yxatiga tushadi, HAQIQIY tarmoqqa chiqmaydi."""
    meta_api.oauth_list_businesses = lambda token: []
    meta_api.oauth_list_ad_accounts_for_business = lambda business_id, token: []
    meta_api.get_business_pixels = lambda business_id, token: []


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


def test_single_option_at_every_level_auto_connects():
    """2026-09, JONLI BUG TUZATISHI (foydalanuvchi shikoyati -- skrinshot
    bilan: "akkaunt ulashni boshlagan bosganda facebookda shunaqa narsa
    ochilib qolyapti... avtomaticheskiy ulanib ketmayapti", "avtomaticheskiy
    ulanishni to'g'irlagandik-ku, buzilib ketibdi"): ILGARI reklama scope
    so'ralganda (`include_ads=True`) tanlov ekrani HAR DOIM ko'rsatilardi --
    hatto atigi BITTA sahifa/Business/reklama hisobi/Pixel bo'lganda ham
    (bunday holatda haqiqatan tanlaydigan HECH NARSA yo'q edi). Endi: HAR
    BIR darajada (sahifa, Business, reklama hisobi) aynan bitta yoki nolta
    nomzod bo'lsa -- BARCHASI avtomatik saqlanadi, tanlov ekrani UMUMAN
    ko'rsatilmaydi. Tanlov ekrani FAQAT haqiqiy noaniqlik bo'lganda (2+
    Business yoki 2+ reklama hisobi) ko'rsatiladi."""
    meta_api.META_APP_ID, meta_api.META_APP_SECRET = "test_app_id", "test_app_secret"
    meta_api.oauth_exchange_code = lambda code, redirect_uri: "short_lived_token"
    meta_api.oauth_exchange_long_lived = lambda short_token: ("long_lived_token_abc", 5184000)
    meta_api.oauth_list_pages = lambda token: [
        {"id": "page_1", "name": "Mening Sahifam", "instagram_business_account": {"id": "ig_1", "username": "mening_do'konim"}},
    ]
    meta_api.oauth_list_ad_accounts = lambda token: [
        {"id": "act_111", "name": "Asosiy hisob"},
    ]
    meta_api.get_ad_account_pixels = lambda ad_account_id, access_token: (
        [{"id": "pixel_111", "name": "Asosiy Pixel"}] if ad_account_id == "act_111" else []
    )
    _stub_no_business_assets()
    try:
        with app_module.app.test_client() as client:
            _signup(client, company_name="Bitta Variant MChJ", admin_username="bitta_variant_admin", plan="business")
            start_resp = client.get("/connect-accounts/facebook/start")
            assert start_resp.status_code == 302
            state = _extract_state(start_resp.headers["Location"])

            cb_resp = client.get(f"/connect-accounts/facebook/callback?code=fake_code&state={state}", follow_redirects=True)
            html = cb_resp.get_data(as_text=True)
            assert "Qaysi hisobni ulaymiz" not in html, "Bitta variant bo'lsa, tanlov ekrani UMUMAN ko'rsatilmasligi kerak -- avtomatik ulanishi kerak"
            assert "avtomatik ulandi" in html

            company = app_module._current_company()
            session = db_module.get_session()
            try:
                c = session.get(db_module.Company, company.id)
                assert c.meta_access_token != "long_lived_token_abc", "token endi shifrlangan saqlanishi kerak"
                assert c.get_meta_access_token() == "long_lived_token_abc"
                assert c.meta_page_id == "page_1"
                assert c.ig_business_id == "ig_1"
                assert c.meta_ad_account_id == "act_111"
                assert c.meta_pixel_id == "pixel_111"
                assert c.meta_integration_status == "connected"
                assert c.meta_token_expires_at is not None
            finally:
                session.close()
    finally:
        meta_api.META_APP_ID, meta_api.META_APP_SECRET = "", ""
    print("OK: reklama scope so'ralganda ham, bitta variant bo'lsa tanlov ekransiz to'liq avtomatik ulanadi (token shifrlangan + status='connected' + muddat bilan saqlanadi)")


def test_ad_account_pixel_auto_detected_when_dataset_not_explicitly_chosen():
    """Bitta sahifa + bitta reklama hisobi (Business Portfolio'siz) --
    endi callback bosqichida O'ZI to'liq avtomatik ulanadi (tanlov ekrani
    ko'rsatilmaydi). Bu auto-connect yo'lida ham Dataset ANIQ tanlanmagan
    (Business Portfolio-darajasidagi Pixel ro'yxati yo'q), shuning uchun
    `_save_facebook_connection()`ning o'zidagi zaxira mantiq -- tanlangan
    reklama hisobiga biriktirilgan birinchi Pixel'ni avtomatik topib
    saqlash (foydalanuvchi so'rovi -- "capi ni hammasini avtomatik qil")
    -- ishga tushishi kerak."""
    meta_api.META_APP_ID, meta_api.META_APP_SECRET = "test_app_id", "test_app_secret"
    meta_api.oauth_exchange_code = lambda code, redirect_uri: "short_lived_token"
    meta_api.oauth_exchange_long_lived = lambda short_token: ("long_lived_token_pixel", None)
    meta_api.oauth_list_pages = lambda token: [
        {"id": "page_pixel", "name": "Pixel Sahifasi"},
    ]
    meta_api.oauth_list_ad_accounts = lambda token: [
        {"id": "act_pixel_test", "name": "Pixel Hisobi"},
    ]
    meta_api.get_ad_account_pixels = lambda ad_account_id, access_token: (
        [{"id": "pixel_999", "name": "Asosiy Pixel"}] if ad_account_id == "act_pixel_test" else []
    )
    _stub_no_business_assets()
    try:
        with app_module.app.test_client() as client:
            _signup(client, company_name="Pixel MChJ", admin_username="pixel_admin", plan="business")
            start_resp = client.get("/connect-accounts/facebook/start")
            state = _extract_state(start_resp.headers["Location"])
            cb_resp = client.get(f"/connect-accounts/facebook/callback?code=fake_code&state={state}", follow_redirects=True)
            assert "Qaysi hisobni ulaymiz" not in cb_resp.get_data(as_text=True), "Bitta variant bo'lsa tanlov ekrani ko'rsatilmasligi kerak"

            company = app_module._current_company()
            session = db_module.get_session()
            try:
                c = session.get(db_module.Company, company.id)
                assert c.meta_pixel_id == "pixel_999", f"Pixel avtomatik saqlanmadi: {c.meta_pixel_id!r}"
                assert c.meta_token_expires_at is None, "expires_in=None bo'lsa muddat ustuni ham bo'sh qolishi kerak"
            finally:
                session.close()

            settings_html = client.get("/sozlamalar").get_data(as_text=True)
            assert 'badge-color-good">ulangan' in settings_html, "Sozlamalar sahifasida CAPI 'ulangan' deb ko'rsatilishi kerak"
    finally:
        meta_api.META_APP_ID, meta_api.META_APP_SECRET = "", ""
    print("OK: tanlov formasida Dataset aniq tanlanmasa, reklama hisobiga biriktirilgan Pixel avtomatik topilib saqlanadi, va Sozlamalar sahifasi buni 'ulangan' deb ko'rsatadi")


def test_multiple_pages_require_explicit_choice_and_state_mismatch_is_rejected():
    meta_api.META_APP_ID, meta_api.META_APP_SECRET = "test_app_id", "test_app_secret"
    meta_api.oauth_exchange_code = lambda code, redirect_uri: "short_lived_token"
    meta_api.oauth_exchange_long_lived = lambda short_token: ("long_lived_token_xyz", 5184000)
    meta_api.oauth_list_pages = lambda token: [
        {"id": "page_a", "name": "Filial A", "instagram_business_account": {"id": "ig_a"}},
        {"id": "page_b", "name": "Filial B", "instagram_business_account": None},
    ]
    meta_api.oauth_list_ad_accounts = lambda token: [
        {"id": "act_a", "name": "Hisob A"},
        {"id": "act_b", "name": "Hisob B"},
    ]
    meta_api.get_ad_account_pixels = lambda ad_account_id, access_token: []
    _stub_no_business_assets()
    try:
        with app_module.app.test_client() as client:
            _signup(client, company_name="Ko'p Variant MChJ", admin_username="kop_variant_admin")

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


def test_business_and_dataset_selection_persisted():
    """Bitta Business Portfolio, unga tegishli bitta reklama hisobi va
    bitta Dataset (Pixel) topilganda -- ENDI callback bosqichida O'ZI
    to'liq avtomatik ulanadi (tanlov ekransiz), va tanlangan Business
    ID/nomi + unga tegishli Dataset to'g'ri saqlanishini tekshiradi
    (foydalanuvchi talabi: "select Business Portfolio -> Ad Account ->
    Pixel/Dataset")."""
    meta_api.META_APP_ID, meta_api.META_APP_SECRET = "test_app_id", "test_app_secret"
    meta_api.oauth_exchange_code = lambda code, redirect_uri: "short_lived_token"
    meta_api.oauth_exchange_long_lived = lambda short_token: ("long_lived_biz_token", 5184000)
    meta_api.oauth_list_pages = lambda token: [{"id": "page_biz", "name": "Biznes Sahifa"}]
    meta_api.oauth_list_ad_accounts = lambda token: []
    meta_api.oauth_list_businesses = lambda token: [{"id": "biz_1", "name": "Mening Businessim"}]
    meta_api.oauth_list_ad_accounts_for_business = lambda business_id, token: (
        [{"id": "act_biz_1", "name": "Biznes Hisobi"}] if business_id == "biz_1" else []
    )
    meta_api.get_business_pixels = lambda business_id, token: (
        [{"id": "pixel_biz_1", "name": "Biznes Dataset"}] if business_id == "biz_1" else []
    )
    try:
        with app_module.app.test_client() as client:
            _signup(client, company_name="Biznes Portfolio MChJ", admin_username="biz_admin", plan="business")
            start_resp = client.get("/connect-accounts/facebook/start")
            state = _extract_state(start_resp.headers["Location"])
            cb_resp = client.get(f"/connect-accounts/facebook/callback?code=fake_code&state={state}", follow_redirects=True)
            cb_html = cb_resp.get_data(as_text=True)
            assert "Qaysi hisobni ulaymiz" not in cb_html, "Har bir darajada bitta variant bo'lsa tanlov ekrani ko'rsatilmasligi kerak"
            assert "avtomatik ulandi" in cb_html

            company = app_module._current_company()
            session = db_module.get_session()
            try:
                c = session.get(db_module.Company, company.id)
                assert c.meta_business_id == "biz_1"
                assert c.meta_business_name == "Mening Businessim"
                assert c.meta_ad_account_id == "act_biz_1"
                assert c.meta_ad_account_name == "Biznes Hisobi"
                assert c.meta_pixel_id == "pixel_biz_1"
                assert c.meta_dataset_name == "Biznes Dataset"
            finally:
                session.close()
    finally:
        meta_api.META_APP_ID, meta_api.META_APP_SECRET = "", ""
    print("OK: Business Portfolio tanlanganda unga tegishli Ad Account+Dataset to'g'ri saqlanadi")


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


def test_successful_connection_triggers_immediate_background_lead_sync():
    """2026-09, kod tekshiruvida topilgan HAQIQIY bo'shliq (`lead_sync.py`
    hujjatlashtirilgan "since-cursor" naqshi): birinchi sinxronizatsiya
    ishga tushishida kursor faqat "hozir"ga o'rnatiladi, HECH NARSA
    tortib olinmaydi -- shuning uchun ulanish bilan birinchi cron
    ishlashi (eng ko'pi bilan 15 daqiqa) orasida kelgan har qanday lead
    umuman kuzatilmay qolib ketishi mumkin edi. Endi
    `_save_facebook_connection()` muvaffaqiyatli yakunlangach DARHOL
    (fon oqimida) `lead_sync.sync_once(company=...)` chaqiriladi -- shu
    tufayli bu oyna yopiladi."""
    meta_api.META_APP_ID, meta_api.META_APP_SECRET = "test_app_id", "test_app_secret"
    meta_api.oauth_exchange_code = lambda code, redirect_uri: "short_lived_token"
    meta_api.oauth_exchange_long_lived = lambda short_token: ("long_lived_sync_token", None)
    meta_api.oauth_list_pages = lambda token: [{"id": "page_sync", "name": "Sync Sahifasi"}]
    meta_api.oauth_list_ad_accounts = lambda token: []
    meta_api.get_ad_account_pixels = lambda ad_account_id, access_token: []
    _stub_no_business_assets()

    calls = []

    def _fake_sync_once(company=None):
        calls.append(company.id if company else None)
        return {"created": 0, "updated": 0}

    try:
        with app_module.app.test_client() as client, \
             mock.patch.object(app_module, "threading") as mock_threading, \
             mock.patch.object(lead_sync, "sync_once", side_effect=_fake_sync_once):
            mock_threading.Thread = _SyncThread
            _signup(client, company_name="Sync MChJ", admin_username="sync_admin", plan="start")
            start_resp = client.get("/connect-accounts/facebook/start")
            state = _extract_state(start_resp.headers["Location"])
            client.get(f"/connect-accounts/facebook/callback?code=fake_code&state={state}", follow_redirects=True)

            company = app_module._current_company()
            assert calls == [company.id], f"Ulanishdan darhol keyin lead_sync.sync_once() aynan bitta marta, shu kompaniya uchun chaqirilishi kerak edi: {calls!r}"
    finally:
        meta_api.META_APP_ID, meta_api.META_APP_SECRET = "", ""
    print("OK: Facebook muvaffaqiyatli ulangach, cron kutmasdan DARHOL fon oqimida lead_sync.sync_once() ishga tushadi -- ulanish bilan birinchi cron orasidagi lead yo'qolish oynasi yopiladi")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
