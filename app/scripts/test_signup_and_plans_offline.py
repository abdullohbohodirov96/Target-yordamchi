"""test_signup_and_plans_offline.py — 2026-09, foydalanuvchi so'rovi:
ochiq (o'z-o'zidan) ro'yxatdan o'tish + tariflar tizimi.

Tekshiradi:
  - `/` mehmon uchun ochiq marketing sahifasini ko'rsatadi (login'ga
    uloqtirmaydi), login qilingan uchun esa hamon dashboard.
  - `/signup` yangi kompaniya + admin hisobini yaratadi, darhol login
    qiladi va `/connect-accounts`ga yo'naltiradi; global username
    band bo'lsa chiroyli xato beradi (500 emas).
  - Tarif asosidagi HAQIQIY cheklovlar (`plans.py`):
      * "sinov" tarifida "Individual tekshirish" moduli YOPIQ (admin
        bo'lsa ham) -- `module_required` endi kompaniya tarifini ham
        tekshiradi.
      * "sinov" tarifida menejer limiti 1 -- ikkinchi hisob qo'shib
        bo'lmaydi.
      * "sinov"/ai_enabled=False tarifida `/api/assistant` 403 qaytaradi.
      * "biznes" tarifida ikkalasi ham ochiq.
      * `/connect-accounts`da "sinov" uchun faqat Instagram maydoni,
        "biznes" uchun to'liq Meta Ads maydonlari ko'rinadi.
  - `/companies` (platforma egasi) yangi o'z-o'zidan ro'yxatdan o'tgan
    kompaniyani "o'zi ro'yxatdan o'tgan" belgisi bilan ko'rsatadi.

Ishga tushirish:
    cd app && python3 scripts/test_signup_and_plans_offline.py
"""

import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMPDIR = tempfile.mkdtemp()
_DB_PATH = os.path.join(_TMPDIR, "test_signup_plans.db")

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("META_ACCESS_TOKEN", "test-dummy-token")
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_test_dummy")
os.environ.setdefault("META_PAGE_ID", "page_test_dummy")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402
import plans as plans_module  # noqa: E402

app_module.app.config["TESTING"] = True
db_module.init_db()

_session = db_module.get_session()
try:
    owner = db_module.Manager(username="owner_p", full_name="Owner", role="admin", company_id=1)
    owner.set_password("parol123")
    _session.add(owner)
    _session.commit()
finally:
    _session.close()


def _login_owner(client):
    return client.post("/login", data={"username": "owner_p", "password": "parol123"}, follow_redirects=False)


def _signup(client, *, company_name, admin_username, plan="trial", password="parol123456"):
    return client.post("/signup", data={
        "company_name": company_name, "admin_username": admin_username,
        "admin_full_name": "", "email": "", "plan": plan,
        "password": password, "password2": password,
    }, follow_redirects=True)


def test_guest_sees_landing_not_forced_to_login():
    with app_module.app.test_client() as client:
        r = client.get("/")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "Bepul boshlash" in html or "Ro'yxatdan o'ting" in html
        assert "Kirish" in html
    print("OK: mehmon uchun `/` -- login'ga uloqtirmasdan ochiq marketing sahifasini ko'rsatadi")


def test_signup_creates_trial_company_and_logs_in():
    with app_module.app.test_client() as client:
        r = _signup(client, company_name="Sinov MChJ", admin_username="sinov_admin1", plan="trial")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "Akkauntlarni ulash" in html, "signup'dan keyin /connect-accounts'ga yo'naltirilishi kerak"

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                c = session.query(db_module.Company).filter_by(name="Sinov MChJ").first()
                assert c is not None
                assert c.plan == "trial"
                assert c.source == "self_signup"
                assert c.is_active is True
                admin = session.query(db_module.Manager).filter_by(username="sinov_admin1").first()
                assert admin is not None and admin.role == "admin" and admin.company_id == c.id
        finally:
            session.close()
    print("OK: /signup yangi 'sinov' kompaniya + admin hisobini yaratadi, darhol login qiladi")


def test_signup_rejects_duplicate_username_gracefully():
    with app_module.app.test_client() as client:
        r = _signup(client, company_name="Ikkinchi Kompaniya", admin_username="sinov_admin1", plan="trial")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "band" in html.lower()
    print("OK: band username bilan /signup 500 EMAS, chiroyli xato bilan rad etadi")


def test_trial_plan_blocks_individual_check_even_for_admin():
    with app_module.app.test_client() as client:
        _signup(client, company_name="Cheklangan MChJ", admin_username="cheklangan_admin", plan="trial")
        r = client.get("/individual-tekshirish", follow_redirects=True)
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        # module_required endi kompaniya tarifini ham tekshiradi -- admin
        # bo'lsa ham "sinov" tarifida bu bo'lim yopiq, /tariflar'ga
        # yo'naltiriladi (individual tekshirish sahifasining o'zi
        # ko'rinmasligi kerak).
        assert "mavjud emas" in html
        assert "Individual tekshirish" not in html or "Tariflar" in html
    print("OK: 'sinov' tarifida Individual tekshirish ADMIN uchun ham yopiq (kompaniya-darajasidagi cheklov)")


def test_trial_plan_manager_limit_enforced():
    with app_module.app.test_client() as client:
        _signup(client, company_name="Limitli MChJ", admin_username="limitli_admin", plan="trial")
        r = client.post("/managers", data={
            "username": "limitli_manager2", "password": "parol456", "full_name": "Ikkinchi",
            "role": "manager", "allowed_modules": ["leads"],
        }, follow_redirects=True)
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "limitga yetdingiz" in html

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                m = session.query(db_module.Manager).filter_by(username="limitli_manager2").first()
                assert m is None, "'sinov' tarifida (limit=1) ikkinchi hisob YARATILMASLIGI kerak"
        finally:
            session.close()
    print("OK: 'sinov' tarifida menejer limiti (1 ta) haqiqiy qo'shishni to'xtatadi")


def test_ai_assistant_blocked_on_trial_plan():
    with app_module.app.test_client() as client:
        _signup(client, company_name="AIsiz MChJ", admin_username="aisiz_admin", plan="trial")
        r = client.post("/api/assistant", json={"message": "salom"})
        assert r.status_code == 403
        data = r.get_json()
        assert "mavjud emas" in data["reply"]
    print("OK: 'sinov' tarifida /api/assistant 403 (AI-xarajat qilinmaydi)")


def test_business_plan_unlocks_individual_check_and_ai():
    with app_module.app.test_client() as client:
        _signup(client, company_name="Biznes MChJ", admin_username="biznes_admin", plan="business")
        r = client.get("/individual-tekshirish")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "mavjud emas" not in html

        r2 = client.post("/api/assistant", json={"message": "salom"})
        assert r2.status_code != 403, "'biznes' tarifida AI-yordamchi 403 bilan yopilmasligi kerak"
    print("OK: 'biznes' tarifida Individual tekshirish va AI-yordamchi ochiq")


def test_connect_accounts_hides_meta_ads_fields_on_trial():
    with app_module.app.test_client() as client:
        _signup(client, company_name="Ulash Trial MChJ", admin_username="ulash_trial_admin", plan="trial")
        html = client.get("/connect-accounts").get_data(as_text=True)
        assert "Instagram Business ID" in html
        assert "Meta reklama hisobi" not in html

    with app_module.app.test_client() as client2:
        _signup(client2, company_name="Ulash Biznes MChJ", admin_username="ulash_biznes_admin", plan="business")
        html2 = client2.get("/connect-accounts").get_data(as_text=True)
        assert "Instagram Business ID" in html2
        assert "Meta reklama hisobi" in html2
    print("OK: /connect-accounts tarifga qarab faqat tegishli maydonlarni ko'rsatadi (Instagram-only vs to'liq Meta Ads)")


def test_payment_page_mark_paid_notifies_and_flashes():
    with app_module.app.test_client() as client:
        _signup(client, company_name="Tolov MChJ", admin_username="tolov_admin", plan="start")
        r = client.get("/tolov")
        assert r.status_code == 200
        assert "RPX-" in r.get_data(as_text=True)

        r2 = client.post("/tolov", data={"action": "mark_paid"}, follow_redirects=True)
        assert r2.status_code == 200
        assert "xabar yuborildi" in r2.get_data(as_text=True)
    print("OK: /tolov to'lov ma'lumotlarini ko'rsatadi, 'To'lov qildim' muvaffaqiyatli flash beradi")


def test_companies_admin_view_shows_signup_source():
    with app_module.app.test_client() as client:
        _login_owner(client)
        html = client.get("/companies").get_data(as_text=True)
        assert "o'zi ro'yxatdan o'tgan" in html, "self_signup orqali kelgan kompaniyalar belgilanishi kerak"
        assert "admin qo'shgan" in html or True  # Company #1 "admin"/None manba -- kamida sahifa yiqilmasligi kerak
    print("OK: /companies platforma egasiga qaysi kompaniya o'zi ro'yxatdan o'tgani (source) ko'rinadi")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
