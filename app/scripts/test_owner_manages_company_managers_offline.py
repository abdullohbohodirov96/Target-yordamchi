"""test_owner_manages_company_managers_offline.py — 2026-09, foydalanuvchi
shikoyati: "kompaniyaga kirganda manager qoshib bomayaptiyu baribir shuni
tog'rila". Sabab: `Manager` tenant-filtrga tushgandan keyin (multi-tenant
2-bosqich), platforma egasi `/managers`da ENDI faqat O'ZINING kompaniyasini
(Company #1) ko'rar/boshqarar edi -- boshqa (masalan yangi yaratilgan)
kompaniyaga menejer qo'shishning YAGONA yo'li o'sha kompaniyaning avtomatik
yaratilgan admin hisobi bilan ALOHIDA chiqib-kirish edi.

Bu fayl `/companies/<id>/managers` (yangi marshrut, `db.scoped_as()` orqali)
platforma egasiga CHIQIB-KIRMASDAN boshqa kompaniyaning menejerlarini
ko'rish/qo'shish/tahrirlash imkonini berishini tekshiradi, VA `/managers`
o'zi hamon faqat joriy foydalanuvchining o'z kompaniyasi bilan ishlashini
(izolyatsiya buzilmaganini) tasdiqlaydi.

Ishga tushirish:
    cd app && python3 scripts/test_owner_manages_company_managers_offline.py
"""

import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMPDIR = tempfile.mkdtemp()
_DB_PATH = os.path.join(_TMPDIR, "test_owner_mgr.db")

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

_session = db_module.get_session()
try:
    owner = db_module.Manager(username="owner_x", full_name="Owner", role="admin", company_id=1)
    owner.set_password("parol123")
    _session.add(owner)
    _session.commit()
finally:
    _session.close()


def _login(client, username="owner_x", password="parol123"):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=False)


def _create_company(client, name):
    r = client.post("/companies", data={"name": name, "email": "", "plan": "trial"}, follow_redirects=True)
    html = r.get_data(as_text=True)
    m = re.search(r'login: &#34;([^&"]+)&#34;, parol: &#34;([^&"]+)&#34;', html)
    return m.group(1), m.group(2)


def test_owner_can_add_manager_to_other_company_without_logging_out():
    with app_module.app.test_client() as client:
        _login(client)
        admin_username, _ = _create_company(client, "Mijoz X")

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                admin_row = session.query(db_module.Manager).filter_by(username=admin_username).first()
                assert admin_row is not None
                company_id = admin_row.company_id
        finally:
            session.close()

        # Owner logged in as THEMSELVES (company #1) -- never logged out.
        r_page = client.get(f"/companies/{company_id}/managers")
        assert r_page.status_code == 200
        html_page = r_page.get_data(as_text=True)
        assert admin_username in html_page, "shu kompaniyaning avtomatik admin'i ro'yxatda ko'rinishi kerak"

        r_post = client.post(f"/companies/{company_id}/managers", data={
            "username": "yangi_menejer_x", "password": "parol456", "full_name": "Yangi Menejer",
            "role": "manager", "allowed_modules": ["leads"],
        }, follow_redirects=True)
        assert r_post.status_code == 200
        html_post = r_post.get_data(as_text=True)
        assert "yangi_menejer_x qo&#39;shildi" in html_post or "yangi_menejer_x qo'shildi" in html_post
        assert "yangi_menejer_x" in html_post

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                new_m = session.query(db_module.Manager).filter_by(username="yangi_menejer_x").first()
                assert new_m is not None
                assert new_m.company_id == company_id, "yangi menejer AYNAN shu (target) kompaniyaga bog'lanishi kerak"
        finally:
            session.close()
    print("OK: platforma egasi chiqib-kirmasdan, /companies/<id>/managers orqali boshqa kompaniyaga menejer qo'sha oladi")


def test_managers_page_still_isolated_to_own_company():
    with app_module.app.test_client() as client:
        _login(client)
        r = client.get("/managers")
        html = r.get_data(as_text=True)
        assert "yangi_menejer_x" not in html, "/managers hamon FAQAT egasining o'z kompaniyasini ko'rsatishi kerak"
    print("OK: /managers o'zi hamon izolyatsiyalangan -- boshqa kompaniyaning menejeri unda ko'rinmaydi")


def test_owner_can_edit_manager_of_other_company():
    with app_module.app.test_client() as client:
        _login(client)
        session = db_module.get_session()
        try:
            with db_module.unscoped():
                m = session.query(db_module.Manager).filter_by(username="yangi_menejer_x").first()
                m_id = m.id
        finally:
            session.close()

        r = client.get(f"/managers/{m_id}/edit")
        assert r.status_code == 200, "platforma egasi boshqa kompaniyaning menejerini tahrirlash sahifasini ochа olishi kerak"
        html = r.get_data(as_text=True)
        assert "yangi_menejer_x" in html

        r2 = client.post(f"/managers/{m_id}/edit", data={
            "username": "yangi_menejer_x", "full_name": "Yangilangan Ism", "role": "manager",
            "password": "", "is_active": "on",
        }, follow_redirects=True)
        assert r2.status_code == 200
        html2 = r2.get_data(as_text=True)
        assert "yangilandi" in html2.lower()
    print("OK: platforma egasi boshqa kompaniyaning menejerini muvaffaqiyatli tahrirlay oladi")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
