"""test_impersonation_offline.py — 2026-09, "CEO dashboard + impersonatsiya"
ishi: platforma egasi (Company #1 admin) biror mijoz-kompaniya nomidan
TO'LIQ kirib (`/companies/<id>/impersonate`), o'sha kompaniyaning admin
hisobi qanday ko'rayotganini o'z ko'zi bilan ko'ra olishini, va bu
seansning har bir tomoni (kirish/chiqish, ichma-ich taqiq, o'zining
kompaniyasiga taqiq, admin bo'lmagan kompaniyada xatolik, audit-log
yozilishi/yopilishi, logout orqali ham yopilishi) to'g'ri ishlashini
tekshiradi.

Ishga tushirish:
    cd app && python3 scripts/test_impersonation_offline.py
"""

import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMPDIR = tempfile.mkdtemp()
_DB_PATH = os.path.join(_TMPDIR, "test_impersonation.db")

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("META_ACCESS_TOKEN", "test-dummy-token")
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_test_dummy")
os.environ.setdefault("META_PAGE_ID", "page_test_dummy")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402

app_module.app.config["TESTING"] = True
app_module.app.config["WTF_CSRF_ENABLED"] = False  # 2026-09, CSRF endi majburiy -- testlarda so'rovlar session-tashqarisida yasaladi
db_module.init_db()

_session = db_module.get_session()
try:
    owner = db_module.Manager(username="owner_imp", full_name="Owner", role="admin", company_id=1)
    owner.set_password("parol123")
    _session.add(owner)
    _session.commit()
finally:
    _session.close()


def _login(client, username="owner_imp", password="parol123"):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=False)


def _create_company(client, name, plan="business"):
    r = client.post("/companies", data={"name": name, "email": "", "plan": plan}, follow_redirects=True)
    html = r.get_data(as_text=True)
    m = re.search(r'login: &#34;([^&"]+)&#34;, parol: &#34;([^&"]+)&#34;', html)
    admin_username = m.group(1)
    session = db_module.get_session()
    try:
        with db_module.unscoped():
            admin_row = session.query(db_module.Manager).filter_by(username=admin_username).first()
            company_id = admin_row.company_id
    finally:
        session.close()
    return company_id, admin_username


def test_impersonate_switches_identity_and_tenant_scope():
    with app_module.app.test_client() as client:
        _login(client)
        company_id, admin_username = _create_company(client, "Impersonatsiya MChJ 1")

        r = client.post(f"/companies/{company_id}/impersonate", follow_redirects=True)
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "Impersonatsiya MChJ 1" in html, "banner/flash kompaniya nomini ko'rsatishi kerak"

        # Dashboard endi TARGET kompaniyaning nomidan ko'rinishi kerak --
        # boshqa admin'ga tegishli manager qo'shib ko'ramiz (tenant scope
        # to'g'ri o'rnatilganini tasdiqlash uchun).
        r_mgr = client.get("/managers")
        assert r_mgr.status_code == 200
        html_mgr = r_mgr.get_data(as_text=True)
        assert admin_username in html_mgr, "impersonatsiya paytida /managers TARGET kompaniyaning o'zini ko'rsatishi kerak"

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                logs = (
                    session.query(db_module.ImpersonationLog)
                    .filter_by(target_company_id=company_id)
                    .order_by(db_module.ImpersonationLog.id.desc())
                    .all()
                )
                assert len(logs) == 1
                assert logs[0].ended_at is None, "audit-yozuv hali ochiq (impersonatsiya davom etayotgani uchun) bo'lishi kerak"
                assert logs[0].owner_username == "owner_imp"
        finally:
            session.close()

        # Chiqish -- owner o'ziga qaytadi.
        r_exit = client.post("/impersonate/exit", follow_redirects=True)
        assert r_exit.status_code == 200
        r_companies = client.get("/companies")
        assert r_companies.status_code == 200, "chiqishdan keyin platforma egasining o'z huquqi (`/companies`) tiklanishi kerak"

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                log_row = session.query(db_module.ImpersonationLog).filter_by(target_company_id=company_id).first()
                assert log_row.ended_at is not None
                assert log_row.ended_reason == "exit"
        finally:
            session.close()
    print("OK: impersonatsiya current_user/tenant scope'ni to'g'ri almashtiradi, audit-log yoziladi va chiqishda to'g'ri yopiladi")


def test_platform_owner_routes_blocked_while_impersonating():
    with app_module.app.test_client() as client:
        _login(client)
        company_id, _ = _create_company(client, "Impersonatsiya MChJ 2")
        client.post(f"/companies/{company_id}/impersonate", follow_redirects=True)

        r = client.get("/companies", follow_redirects=False)
        assert r.status_code in (302, 303), "impersonatsiya paytida `/companies` (platform_owner_required) qayta yo'naltirishi kerak"

        client.post("/impersonate/exit", follow_redirects=True)
    print("OK: impersonatsiya paytida platform_owner_required marshrutlar (masalan /companies) yopiq bo'ladi")


def test_cannot_impersonate_own_company():
    with app_module.app.test_client() as client:
        _login(client)
        r = client.post("/companies/1/impersonate", follow_redirects=True)
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "impersonatsiya qilib bo&#39;lmaydi" in html or "impersonatsiya qilib bo'lmaydi" in html
    print("OK: o'z kompaniyasi nomidan impersonatsiya qilib bo'lmaydi")


def test_cannot_nest_impersonation():
    with app_module.app.test_client() as client:
        _login(client)
        company_a, _ = _create_company(client, "Impersonatsiya MChJ 3A")
        company_b, _ = _create_company(client, "Impersonatsiya MChJ 3B")

        client.post(f"/companies/{company_a}/impersonate", follow_redirects=True)
        r2 = client.post(f"/companies/{company_b}/impersonate", follow_redirects=True)
        html2 = r2.get_data(as_text=True)
        # 2026-09 eslatma: `company_impersonate`ning ICHIDAGI "Avval joriy
        # impersonatsiyadan chiqing" tekshiruvi bu yo'l bilan hech qachon
        # yetib bo'lmaydi -- chunki impersonatsiya paytida current_user
        # ENDI platforma egasi emas (`_is_platform_owner()` False qaytaradi,
        # target kompaniyaning company_id'si != 1 bo'lgani uchun), shuning
        # uchun marshrutning ustidagi `@platform_owner_required` allaqachon
        # avvalroq to'xtatadi. Natija baribir bir xil -- ichma-ich
        # impersonatsiya IMKONSIZ -- shuning uchun bu testda ANIQ shu
        # (haqiqiy) xabarni tekshiramiz.
        assert "faqat platforma egasi uchun" in html2

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                log_b = session.query(db_module.ImpersonationLog).filter_by(target_company_id=company_b).first()
                assert log_b is None, "ichma-ich urinishda ikkinchi audit-yozuv yaratilmasligi kerak"
        finally:
            session.close()

        client.post("/impersonate/exit", follow_redirects=True)
    print("OK: ichma-ich (nested) impersonatsiya taqiqlanadi")


def test_impersonate_company_without_active_admin_fails_gracefully():
    with app_module.app.test_client() as client:
        _login(client)
        company_id, admin_username = _create_company(client, "Adminsiz MChJ")

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                admin_row = session.query(db_module.Manager).filter_by(username=admin_username).first()
                admin_row.is_active = False
                session.commit()
        finally:
            session.close()

        r = client.post(f"/companies/{company_id}/impersonate", follow_redirects=True)
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "faol admin hisob topilmadi" in html

        # Owner o'zi hamon o'zi -- impersonatsiya boshlanmagan.
        assert client.get("/companies").status_code == 200
    print("OK: faol admin'i yo'q kompaniyani impersonatsiya qilish toza xatolik bilan rad etiladi")


def test_logout_while_impersonating_closes_audit_log():
    with app_module.app.test_client() as client:
        _login(client)
        company_id, _ = _create_company(client, "Impersonatsiya MChJ 4")
        client.post(f"/companies/{company_id}/impersonate", follow_redirects=True)

        client.get("/logout", follow_redirects=True)

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                log_row = (
                    session.query(db_module.ImpersonationLog)
                    .filter_by(target_company_id=company_id)
                    .order_by(db_module.ImpersonationLog.id.desc())
                    .first()
                )
                assert log_row.ended_at is not None
                assert log_row.ended_reason == "logout"
        finally:
            session.close()

        # Endi tizimga kirilmagan -- himoyalangan sahifa login'ga
        # qaytarishi kerak.
        r = client.get("/companies", follow_redirects=False)
        assert r.status_code in (302, 303)
    print("OK: oddiy /logout ham (chiqish tugmasidan emas) impersonatsiya audit-yozuvini to'g'ri yopadi")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
