"""test_multitenant_e2e_offline.py — multi-tenant 2-bosqichning HAQIQIY
Flask marshrutlar orqali (test_client, tarmoqsiz) ishlashini tekshiradi.

`test_tenant_scoping_offline.py` markaziy filtr mexanizmini (db.py ichida)
sinaydi; bu fayl esa foydalanuvchi ko'radigan OQIMNI sinaydi:
  1. Platforma egasi `/companies`da yangi kompaniya yaratadi -> DARHOL
     o'sha kompaniyaning o'z admin hisobi (`Manager`, role=admin) va
     standart voronka bosqichlari (`FunnelStage`) paydo bo'ladi (2026-09,
     foydalanuvchi so'rovi: "kompaniya yaratgandan keyin unga admin ham
     yaratilsin, srazu u ozi managerlarini ochvoladi").
  2. Yangi kompaniyaning admin hisobi bilan kirib bo'ladi, va u FAQAT
     o'z kompaniyasining lidlarini/menejerlarini ko'radi -- boshqa
     kompaniyaning ma'lumoti UMUMAN ko'rinmaydi.
  3. Ikkinchi kompaniya birinchi kompaniyanikidan FARQLI standart voronka
     bosqichlariga ega bo'ladi (har biri o'zining nusxasini oladi).
  4. Ikkinchi kompaniya birinchi kompaniyaning admin username'i bilan
     yangi menejer qo'shishga urinsa -- 500/IntegrityError EMAS, chiroyli
     "band" xabari bilan rad etiladi (chunki username ATAYLAB global
     unique).

Ishga tushirish:
    cd app && python3 scripts/test_multitenant_e2e_offline.py
"""

import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMPDIR = tempfile.mkdtemp()
_DB_PATH = os.path.join(_TMPDIR, "test_e2e.db")

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("META_ACCESS_TOKEN", "test-dummy-token")
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_test_dummy")
os.environ.setdefault("META_PAGE_ID", "page_test_dummy")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402

app_module.app.config["TESTING"] = True
db_module.init_db()

# --- Fixture: Company #1 (platforma egasi) uchun admin hisob.
_session = db_module.get_session()
try:
    company1 = _session.query(db_module.Company).order_by(db_module.Company.id.asc()).first()
    assert company1 is not None and company1.id == 1
    owner = db_module.Manager(username="platform_owner", full_name="Platforma egasi", role="admin", company_id=1)
    owner.set_password("parol123")
    _session.add(owner)
    _session.commit()
finally:
    _session.close()


def _login(client, username, password="parol123"):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=False)


def test_company_creation_auto_creates_admin_and_funnel_stages():
    with app_module.app.test_client() as client:
        r = _login(client, "platform_owner")
        assert r.status_code == 302, f"login muvaffaqiyatsiz: {r.status_code}"

        r2 = client.post("/companies", data={"name": "Ikkinchi Mijoz", "email": "ikkinchi@mijoz.uz", "plan": "trial"}, follow_redirects=True)
        assert r2.status_code == 200, f"kompaniya yaratish 200 qaytarishi kerak, olindi: {r2.status_code}"
        html = r2.get_data(as_text=True)
        assert "Ikkinchi Mijoz" in html, "yangi kompaniya ro'yxatda ko'rinishi kerak"

        m = re.search(r'login: &#34;([^&"]+)&#34;, parol: &#34;([^&"]+)&#34;', html)
        if not m:
            m = re.search(r'login: "([^"]+)", parol: "([^"]+)"', html)
        assert m, f"flash xabarida avtomatik yaratilgan admin login/parol ko'rinishi kerak edi -- topilmadi. HTML parchasi: {html[html.find('flash'):html.find('flash')+300]}"
        new_username, new_password = m.group(1), m.group(2)

    session = db_module.get_session()
    try:
        with db_module.unscoped():
            company2 = session.query(db_module.Company).filter_by(name="Ikkinchi Mijoz").first()
            assert company2 is not None
            company2_id = company2.id

            new_admin = session.query(db_module.Manager).filter_by(username=new_username).first()
            assert new_admin is not None, "avtomatik admin bazada topilishi kerak"
            assert new_admin.role == "admin"
            assert new_admin.company_id == company2_id, "avtomatik admin YANGI kompaniyaga bog'langan bo'lishi kerak"
            assert new_admin.check_password(new_password), "flash'da ko'rsatilgan parol haqiqatda ishlashi kerak"

        db_module.set_current_company_id(company2_id)
        try:
            stages = session.query(db_module.FunnelStage).all()
            assert len(stages) == 5, f"yangi kompaniya UZINING 5 ta standart voronka bosqichini olishi kerak, olindi: {len(stages)}"
        finally:
            db_module.set_current_company_id(None)
    finally:
        session.close()
    print("OK: yangi kompaniya yaratilganda DARHOL o'z admin hisobi (haqiqiy ishlaydigan parol bilan) va 5 ta standart voronka bosqichi paydo bo'ladi")
    return new_username, new_password, company2_id


def test_new_company_admin_isolated_from_company1(new_username, new_password, company2_id):
    # Company #1'ga bitta lead qo'shamiz.
    session = db_module.get_session()
    try:
        db_module.set_current_company_id(1)
        try:
            session.add(db_module.Lead(full_name="Company1 lead", company_id=1, status="new"))
            session.commit()
        finally:
            db_module.set_current_company_id(None)
    finally:
        session.close()

    with app_module.app.test_client() as client:
        r = _login(client, new_username, new_password)
        assert r.status_code == 302, f"yangi admin login muvaffaqiyatsiz bo'lishi kerak emas edi: {r.status_code}"

        r2 = client.get("/leads", follow_redirects=True)
        assert r2.status_code == 200
        html = r2.get_data(as_text=True)
        assert "Company1 lead" not in html, "yangi kompaniya admin'i BOSHQA kompaniyaning lidini ko'rmasligi kerak"

        r3 = client.get("/managers", follow_redirects=True)
        assert r3.status_code == 200
        html3 = r3.get_data(as_text=True)
        assert "platform_owner" not in html3, "yangi kompaniya admin'i BOSHQA kompaniyaning menejerini ko'rmasligi kerak"
    print("OK: yangi kompaniyaning admin'i faqat o'z lidlarini/menejerlarini ko'radi -- Company #1'ning ma'lumoti UMUMAN ko'rinmaydi")


def test_cross_company_username_conflict_shows_friendly_error(new_username, new_password):
    with app_module.app.test_client() as client:
        r = _login(client, new_username, new_password)
        assert r.status_code == 302

        # `platform_owner` -- BOSHQA (Company #1) kompaniyada band username.
        # Bu yerdagi filtr Company #2'ga scoped bo'lgani uchun oddiy tekshiruv
        # buni "bo'sh" deb topib, DB darajasidagi unique cheklovga urilib xom
        # IntegrityError/500 chiqarishi mumkin edi -- `db.unscoped()` buni oldini olishi kerak.
        r2 = client.post("/managers", data={
            "username": "platform_owner", "password": "boshqaparol1", "full_name": "Klon", "role": "manager",
        }, follow_redirects=True)
        assert r2.status_code == 200, f"500 EMAS, chiroyli 200 sahifa bilan rad etilishi kerak, olindi: {r2.status_code}"
        html = r2.get_data(as_text=True)
        assert "band" in html.lower(), "'username band' xabari ko'rsatilishi kerak"
    print("OK: boshqa kompaniyada band bo'lgan username bilan menejer qo'shishga urinish 500 EMAS, chiroyli xabar bilan rad etiladi")


def run_all():
    new_username, new_password, company2_id = test_company_creation_auto_creates_admin_and_funnel_stages()
    test_new_company_admin_isolated_from_company1(new_username, new_password, company2_id)
    test_cross_company_username_conflict_shows_friendly_error(new_username, new_password)
    print("\nBARCHA TESTLAR O'TDI (3 ta)")


if __name__ == "__main__":
    run_all()
