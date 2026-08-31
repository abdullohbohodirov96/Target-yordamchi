"""test_subscription_enforcement_offline.py — `app.py`ga qo'shilgan
obuna/to'lov tekshiruvi (`_enforce_subscription`, `/companies`,
`/companies/<id>/edit`) uchun TARMOQSIZ (offline) tekshiruv.

Flask'ning o'z `test_client()`'i orqali HAQIQIY HTTP so'rov oqimini
(login -> sessiya cookie -> keyingi so'rovlar) sinaydi, vaqtinchalik
faylga asoslangan SQLite bazasi bilan (Meta/Anthropic API'ga umuman
chaqiruv qilinmaydi -- soxta ENV kalitlar bilan faqat import vaqtidagi
tekshiruvni o'tkazish uchun).

Ishga tushirish:
    cd app && python3 scripts/test_subscription_enforcement_offline.py
"""

import os
import sys
import tempfile
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMPDIR = tempfile.mkdtemp()
_DB_PATH = os.path.join(_TMPDIR, "test_subscription.db")

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("META_ACCESS_TOKEN", "test-dummy-token")
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_test_dummy")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402

app_module.app.config["TESTING"] = True
db_module.init_db()

# --- Fixture: 2-kompaniya, har birida bitta admin ------------------------
_session = db_module.get_session()
try:
    company1 = _session.query(db_module.Company).order_by(db_module.Company.id.asc()).first()
    assert company1 is not None and company1.id == 1, "ensure_default_company() Company #1'ni yaratmagan"

    admin1 = db_module.Manager(username="owner_admin", full_name="Egasi", role="admin", company_id=1)
    admin1.set_password("parol123")
    _session.add(admin1)

    company2 = db_module.Company(
        name="Mijoz MChJ", plan="start", is_active=True,
        paid_until=dt.datetime.utcnow() - dt.timedelta(days=1),  # KECHA tugagan -- to'lovsiz
    )
    _session.add(company2)
    _session.commit()

    admin2 = db_module.Manager(username="mijoz_admin", full_name="Mijoz admin", role="admin", company_id=company2.id)
    admin2.set_password("parol123")
    _session.add(admin2)

    company3 = db_module.Company(
        name="Yaxshi mijoz MChJ", plan="business", is_active=True,
        paid_until=dt.datetime.utcnow() + dt.timedelta(days=20),  # HALI TUGAMAGAN -- to'lov qilingan
    )
    _session.add(company3)
    _session.commit()

    admin3 = db_module.Manager(username="yaxshi_admin", full_name="Yaxshi mijoz", role="admin", company_id=company3.id)
    admin3.set_password("parol123")
    _session.add(admin3)
    _session.commit()

    COMPANY2_ID = company2.id
    COMPANY3_ID = company3.id
finally:
    _session.close()


def _login(client, username, password="parol123"):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=False)


def test_platform_owner_company1_not_blocked():
    with app_module.app.test_client() as client:
        r = _login(client, "owner_admin")
        assert r.status_code == 302 and r.headers["Location"].endswith("/"), f"login muvaffaqiyatsiz: {r.status_code} {r.headers.get('Location')}"
        r2 = client.get("/", follow_redirects=False)
        assert r2.status_code == 200, f"Company #1 (paid_until=NULL) hech qachon bloklanmasligi kerak, olindi: {r2.status_code} {r2.headers.get('Location')}"
    print("OK: platforma egasi (Company #1, paid_until=NULL) hech qachon obuna-tekshiruvi bilan bloklanmaydi")


def test_unpaid_company_blocked_on_every_page():
    with app_module.app.test_client() as client:
        r = _login(client, "mijoz_admin")
        assert r.status_code == 302
        r2 = client.get("/", follow_redirects=False)
        assert r2.status_code == 302 and "/obuna-tugagan" in r2.headers["Location"], (
            f"to'lov muddati o'tgan kompaniya bloklanishi kerak edi, olindi: {r2.status_code} {r2.headers.get('Location')}"
        )
        # Boshqa har qanday sahifa ham xuddi shunday bloklanishi kerak.
        r3 = client.get("/target", follow_redirects=False)
        assert r3.status_code == 302 and "/obuna-tugagan" in r3.headers["Location"]
        # /obuna-tugagan sahifasining o'zi ochilishi (qayta-qayta redirect qilib yubormasligi) kerak.
        r4 = client.get("/obuna-tugagan", follow_redirects=False)
        assert r4.status_code == 200, f"/obuna-tugagan sahifasi o'zi 200 qaytarishi kerak, olindi: {r4.status_code}"
    print("OK: to'lov muddati o'tgan (paid_until kechagi kun) kompaniyaning admini HAR QANDAY sahifada /obuna-tugagan'ga yo'naltiriladi")


def test_paid_company_not_blocked():
    with app_module.app.test_client() as client:
        r = _login(client, "yaxshi_admin")
        assert r.status_code == 302
        r2 = client.get("/", follow_redirects=False)
        assert r2.status_code == 200, f"muddati hali tugamagan (20 kun qoldi) kompaniya bloklanmasligi kerak, olindi: {r2.status_code} {r2.headers.get('Location')}"
    print("OK: to'lov muddati hali tugamagan kompaniya bloklanmaydi")


def test_only_platform_owner_sees_companies_page():
    with app_module.app.test_client() as client:
        _login(client, "owner_admin")
        r = client.get("/companies", follow_redirects=False)
        assert r.status_code == 200, f"platforma egasi /companies'ni ko'rishi kerak, olindi: {r.status_code}"

    with app_module.app.test_client() as client:
        _login(client, "yaxshi_admin")
        r = client.get("/companies", follow_redirects=False)
        assert r.status_code == 302 and r.headers["Location"].endswith("/"), (
            "boshqa kompaniya admini /companies'ga kira olmasligi kerak (faqat platforma egasi uchun)"
        )
    print("OK: /companies FAQAT platforma egasi (Company #1 admin)ga ochiq, boshqa kompaniya admini kira olmaydi")


def test_extend_30_unblocks_company():
    # Platforma egasi to'lov kelganda "+30 kunga uzaytirish" tugmasini
    # bosadi -- shundan keyin o'sha kompaniyaning o'zi darhol qayta
    # kira oladigan bo'lishi kerak (yangi login qilmasdan ham, chunki
    # `_enforce_subscription` har so'rovda bazadan QAYTA o'qiydi).
    with app_module.app.test_client() as owner_client:
        _login(owner_client, "owner_admin")
        r = owner_client.post(f"/companies/{COMPANY2_ID}/edit", data={"action": "extend_30"}, follow_redirects=False)
        assert r.status_code == 302

    with app_module.app.test_client() as client:
        _login(client, "mijoz_admin")
        r2 = client.get("/", follow_redirects=False)
        assert r2.status_code == 200, f"+30 kun uzaytirilgandan keyin darhol kirish ochilishi kerak, olindi: {r2.status_code} {r2.headers.get('Location')}"
    print("OK: '+30 kunga uzaytirish' amali kompaniyani darhol (qayta login qilmasdan) qayta faollashtiradi")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
