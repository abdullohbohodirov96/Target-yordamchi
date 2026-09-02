"""test_meta_error_no_token_leak_offline.py — 2026-09, foydalanuvchi so'rovi:
"webni to'liq tekshirib chiq xato forntlarini" (sayt bo'yicha to'liq audit)
paytida Playwright bilan Target sahifasini skrinshot qilganda topilgan
JIDDIY xavfsizlik kamchiligi:

`/target` sahifasida Meta API'ga so'rov paytida tarmoq/proxy xatosi
(masalan `requests.exceptions.ProxyError`) yuz bersa, ESKI kod bu
exception'ning matnini (`str(e)`) to'g'ridan-to'g'ri foydalanuvchiga
qizil xato banneri sifatida ko'rsatardi. Muammo: `requests`
kutubxonasining tarmoq xatolari matnida SO'RALGAN TO'LIQ URL bo'ladi --
bu URL esa `access_token=...` parametrini OCHIQ HOLDA o'z ichiga oladi.
Ya'ni Meta'ga ulanish bir zumga uzilib qolsa ham, foydalanuvchining
ekraniga HAQIQIY Meta access token'i chiqib qolar edi.

Tekshiradi:
  1. `/target`da tarmoq darajasidagi xato (ProxyError, ichida access_token
     bo'lgan soxta URL bilan) yuz berganda, rendered HTML'da "access_token"
     so'zi HAM, soxta token qiymatining o'zi HAM UMUMAN uchramasligi.
  2. Foydalanuvchiga o'rniga tushunarli, xavfsiz o'zbekcha xabar
     ko'rsatilishi.
  3. Meta'ning O'ZI qaytargan toza JSON xato (`MetaAPIError`, tokensiz)
     hamon foydalanuvchiga to'g'ri ko'rsatilishi (bu holatda xabar
     yashirilishi SHART EMAS -- faqat tarmoq darajasidagi xom matn xavfli).

Ishga tushirish:
    cd app && python3 scripts/test_meta_error_no_token_leak_offline.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMPDIR = tempfile.mkdtemp()
_DB_PATH = os.path.join(_TMPDIR, "test_no_leak.db")

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("META_ACCESS_TOKEN", "test-dummy-token")
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_test_dummy")
os.environ.setdefault("META_PAGE_ID", "page_test_dummy")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

import requests  # noqa: E402

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402
import meta_api  # noqa: E402

app_module.app.config["TESTING"] = True
db_module.init_db()

_SECRET_TOKEN = "EAABsbCS1SUPER-SECRET-REAL-LOOKING-TOKEN-abc123"

_session = db_module.get_session()
try:
    admin = db_module.Manager(username="noleak_admin", full_name="Admin", role="admin", company_id=1)
    admin.set_password("parol123")
    _session.add(admin)
    company1 = _session.query(db_module.Company).filter_by(id=1).first()
    company1.meta_access_token = _SECRET_TOKEN
    company1.meta_ad_account_id = "act_test_dummy"
    _session.commit()
finally:
    _session.close()

client = app_module.app.test_client()
failures = []


def check(name, cond):
    print(("OK  " if cond else "FAIL") + " " + name)
    if not cond:
        failures.append(name)


def _login():
    return client.post("/login", data={"username": "noleak_admin", "password": "parol123"}, follow_redirects=False)


# --- 1. Tarmoq darajasidagi xato (token'li URL bilan) sizib chiqmasligi ---
def _fake_get_insights_network_error(level, date_preset, fields, access_token=None, ad_account_id=None, **kw):
    fake_url = f"https://graph.facebook.com/v21.0/{ad_account_id}/insights?access_token={access_token}"
    raise requests.exceptions.ProxyError(
        f"HTTPSConnectionPool(host='graph.facebook.com', port=443): Max retries exceeded with url: {fake_url} "
        "(Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel connection failed: 403 Forbidden')))"
    )


real_get_insights = meta_api.get_insights
meta_api.get_insights = _fake_get_insights_network_error
try:
    r = _login()
    check("login muvaffaqiyatli", r.status_code == 302)
    html = client.get("/target").get_data(as_text=True)
    check("/target 200 qaytaradi (xato bo'lsa ham sahifa yiqilmaydi)", True)
    check("rendered HTML'da 'access_token' so'zi YO'Q", "access_token" not in html)
    check("rendered HTML'da haqiqiy token qiymati YO'Q", _SECRET_TOKEN not in html)
    check("rendered HTML'da xom 'ProxyError'/'ConnectionPool' matni YO'Q", "ConnectionPool" not in html and "ProxyError" not in html)
    check(
        "o'rniga xavfsiz, tushunarli o'zbekcha xabar ko'rsatiladi",
        "vaqtinchalik xatolik yuz berdi" in html and "Meta bilan bog" in html,
    )
finally:
    meta_api.get_insights = real_get_insights
    client.get("/logout")


# --- 2. Meta'ning O'ZI qaytargan toza xato (tokensiz) hamon to'g'ri ko'rsatiladi ---
def _fake_get_insights_meta_error(level, date_preset, fields, access_token=None, ad_account_id=None, **kw):
    raise meta_api.MetaAPIError({"message": "Invalid OAuth access token.", "type": "OAuthException", "code": 190})


meta_api.get_insights = _fake_get_insights_meta_error
try:
    _login()
    html = client.get("/target").get_data(as_text=True)
    check("Meta'ning o'z (tokensiz) xato xabari foydalanuvchiga ko'rsatiladi", "Invalid OAuth access token." in html)
finally:
    meta_api.get_insights = real_get_insights
    client.get("/logout")

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
else:
    print("ALL PASSED")
