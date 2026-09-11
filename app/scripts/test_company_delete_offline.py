"""test_company_delete_offline.py — 2026-09, foydalanuvchi so'rovi:
"kompaniyalarni o'chirib tashlash chiqar" -- `/companies` ro'yxatida
ilgari faqat "Yoqish/To'xtatish" (kirishni yopadi, ma'lumot qoladi) bor
edi. Bu fayl YANGI `/companies/<id>/delete` marshrutini tekshiradi:

  - Kompaniya NOMI aniq (harfma-harf) kiritilmasa -- HECH NARSA
    o'chirilmaydi, xato bilan qaytariladi (tasodifiy bosishdan himoya).
  - To'g'ri nom kiritilsa -- kompaniya VA unga tegishli barcha
    ma'lumot (menejerlar, leadlar) o'chadi.
  - Boshqa kompaniyaning ma'lumoti TEGINILMAY qoladi (izolyatsiya).
  - Asosiy (id=1, platforma egasining o'zi) kompaniyani o'chirib
    bo'lmaydi.

Ishga tushirish:
    cd app && python3 scripts/test_company_delete_offline.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMPDIR = tempfile.mkdtemp()
_DB_PATH = os.path.join(_TMPDIR, "test_company_delete.db")

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
    owner = db_module.Manager(username="owner_del", full_name="Owner", role="admin", company_id=1)
    owner.set_password("parol123")
    _session.add(owner)
    _session.commit()
finally:
    _session.close()


def _login(client):
    return client.post("/login", data={"username": "owner_del", "password": "parol123"}, follow_redirects=False)


def _make_company(name):
    session = db_module.get_session()
    try:
        c = db_module.Company(name=name, is_active=True)
        session.add(c)
        session.commit()
        cid = c.id
        m = db_module.Manager(username=f"{name}_admin", full_name="A", role="admin", company_id=cid)
        m.set_password("x")
        session.add(m)
        with db_module.unscoped():
            lead = db_module.Lead(company_id=cid, full_name="Test Lead", phone="+998900000000", status="new")
            session.add(lead)
        session.commit()
        return cid
    finally:
        session.close()


def test_wrong_name_blocks_deletion():
    cid = _make_company("O'chiriladigan A")
    with app_module.app.test_client() as client:
        _login(client)
        r = client.post(f"/companies/{cid}/delete", data={"confirm_name": "notog'ri nom"}, follow_redirects=True)
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "mos kelmadi" in html
    session = db_module.get_session()
    try:
        with db_module.unscoped():
            assert session.get(db_module.Company, cid) is not None, "noto'g'ri nom bilan kompaniya O'CHIRILMASLIGI kerak"
    finally:
        session.close()
    print("OK: noto'g'ri kompaniya nomi bilan o'chirish rad etiladi, hech narsa o'chmaydi")


def test_correct_name_deletes_everything_and_isolates_others():
    cid_victim = _make_company("O'chiriladigan B")
    cid_other = _make_company("Tegilmaydigan C")
    with app_module.app.test_client() as client:
        _login(client)
        r = client.post(f"/companies/{cid_victim}/delete", data={"confirm_name": "O'chiriladigan B"}, follow_redirects=True)
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "butunlay" in html and "chirildi" in html  # Jinja apostrofni &#39; qilib escape qiladi

    session = db_module.get_session()
    try:
        with db_module.unscoped():
            assert session.get(db_module.Company, cid_victim) is None, "kompaniyaning o'zi o'chirilishi kerak"
            assert session.query(db_module.Manager).filter_by(company_id=cid_victim).count() == 0
            assert session.query(db_module.Lead).filter_by(company_id=cid_victim).count() == 0
            # Boshqa kompaniya butunlay tegilmagan bo'lishi kerak.
            assert session.get(db_module.Company, cid_other) is not None
            assert session.query(db_module.Manager).filter_by(company_id=cid_other).count() == 1
            assert session.query(db_module.Lead).filter_by(company_id=cid_other).count() == 1
    finally:
        session.close()
    print("OK: to'g'ri nom bilan kompaniya VA barcha ma'lumoti o'chadi, boshqa kompaniya tegilmay qoladi")


def test_cannot_delete_own_company():
    with app_module.app.test_client() as client:
        _login(client)
        r = client.post("/companies/1/delete", data={"confirm_name": "Asosiy kompaniya"}, follow_redirects=True)
        assert r.status_code == 200
    session = db_module.get_session()
    try:
        with db_module.unscoped():
            assert session.get(db_module.Company, 1) is not None, "platforma egasining o'z kompaniyasi o'chirilmasligi kerak"
    finally:
        session.close()
    print("OK: platforma egasi o'zining (id=1) kompaniyasini o'chira olmaydi")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
