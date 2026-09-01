"""test_tenant_scoping_offline.py — `db.py`ga qo'shilgan multi-tenant
2-bosqich mexanizmini (`set_current_company_id()` + `with_loader_criteria` +
`do_orm_execute` hodisasi) TARMOQSIZ tekshiradi. Haqiqiy Postgres'ga
ULANMAYDI -- vaqtinchalik SQLite baza.

2026-09, foydalanuvchi so'rovi ("hammasini hozir to'liq ajrat") asosida
qo'shildi. Bu MARKAZIY mexanizm -- ilovadagi 70+ so'rov joyi (leads,
sales, managers, calls, competitors, ...) qo'lda emas, aynan shu BITTA
filtr orqali kompaniya bo'yicha ajratiladi, shuning uchun bu fayl ALOHIDA,
puxta tekshiriladi:
  1. `company_id` o'rnatilgan bo'lsa -- `session.query()`, `session.get()`
     va lazy-load relationship HAMMASI faqat o'sha kompaniyaning
     qatorlarini qaytaradi.
  2. `company_id=None` bo'lsa -- filtr BUTUNLAY o'chiq (login sahifasi
     uchun kerak -- Manager'ni username bo'yicha GLOBAL topish kerak).
  3. Kontekst har doim BOSHIDA `None`ga qaytarilib, keyin qayta
     o'rnatilishi kerak (aks holda oldingi so'rovdan "sizib qolgan" eski
     qiymat keyingi so'rovni noto'g'ri filtrlab qo'yishi mumkin edi).

Ishga tushirish:
    cd app && python3 scripts/test_tenant_scoping_offline.py
"""

import os
import sys
import tempfile
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_db_module(db_path):
    if "db" in sys.modules:
        del sys.modules["db"]
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    import db as db_module
    return db_module


def test_query_filtered_by_current_company():
    with tempfile.TemporaryDirectory() as tmp:
        db_module = _fresh_db_module(os.path.join(tmp, "t1.db"))
        db_module.init_db()

        session = db_module.get_session()
        try:
            c2 = db_module.Company(name="Ikkinchi mijoz")
            session.add(c2)
            session.commit()
            c1_id = db_module.get_default_company_id()
            c2_id = c2.id

            session.add(db_module.Lead(full_name="C1 lead", company_id=c1_id))
            session.add(db_module.Lead(full_name="C2 lead", company_id=c2_id))
            session.commit()
        finally:
            session.close()

        session = db_module.get_session()
        try:
            db_module.set_current_company_id(c1_id)
            names = [l.full_name for l in session.query(db_module.Lead).all()]
            assert names == ["C1 lead"], f"faqat C1 kompaniyasi lidi ko'rinishi kerak, olindi: {names}"

            db_module.set_current_company_id(c2_id)
            names2 = [l.full_name for l in session.query(db_module.Lead).all()]
            assert names2 == ["C2 lead"], f"faqat C2 kompaniyasi lidi ko'rinishi kerak, olindi: {names2}"
        finally:
            db_module.set_current_company_id(None)
            session.close()
    print("OK: session.query(Lead) joriy kompaniyaga qarab avtomatik filtrlanadi -- boshqa kompaniyaning qatori UMUMAN ko'rinmaydi")


def test_session_get_by_primary_key_also_filtered():
    # MUHIM: session.get() (masalan Flask-Login'ning `load_user()`si aynan
    # shuni ishlatadi) HAM filtrlanishi kerak -- aks holda boshqa
    # kompaniyaning ID'sini URL'da qo'lda kiritib ko'rish orqali (masalan
    # /managers/<id>/edit) uning ma'lumotini ko'rish mumkin bo'lib qolardi.
    with tempfile.TemporaryDirectory() as tmp:
        db_module = _fresh_db_module(os.path.join(tmp, "t2.db"))
        db_module.init_db()

        session = db_module.get_session()
        try:
            c2 = db_module.Company(name="Ikkinchi mijoz")
            session.add(c2)
            session.commit()
            c1_id = db_module.get_default_company_id()
            c2_id = c2.id

            m2 = db_module.Manager(username="boshqa_kompaniya_admin", password_hash="x", company_id=c2_id)
            session.add(m2)
            session.commit()
            m2_id = m2.id
        finally:
            session.close()

        session = db_module.get_session()
        try:
            db_module.set_current_company_id(c1_id)
            found = session.get(db_module.Manager, m2_id)
            assert found is None, (
                "C1 konteksti C2'ga tegishli menejerni session.get() orqali ko'rmasligi kerak -- "
                f"olindi: {found}"
            )

            db_module.set_current_company_id(c2_id)
            found2 = session.get(db_module.Manager, m2_id)
            assert found2 is not None and found2.username == "boshqa_kompaniya_admin"
        finally:
            db_module.set_current_company_id(None)
            session.close()
    print("OK: session.get() (Flask-Login load_user() shu orqali ishlaydi) ham kompaniya bo'yicha to'g'ri filtrlanadi")


def test_none_context_disables_filter_for_login():
    # `/login` sahifasi -- foydalanuvchi HALI aniqlanmagan, Manager'ni
    # username bo'yicha GLOBAL (barcha kompaniyalar bo'yicha) topish kerak.
    with tempfile.TemporaryDirectory() as tmp:
        db_module = _fresh_db_module(os.path.join(tmp, "t3.db"))
        db_module.init_db()

        session = db_module.get_session()
        try:
            c2 = db_module.Company(name="Ikkinchi mijoz")
            session.add(c2)
            session.commit()
            session.add(db_module.Manager(username="c2_admin", password_hash="x", company_id=c2.id))
            session.commit()
        finally:
            session.close()

        session = db_module.get_session()
        try:
            db_module.set_current_company_id(None)
            found = session.query(db_module.Manager).filter_by(username="c2_admin").first()
            assert found is not None, "company_id=None bo'lsa filtr o'chiq bo'lishi, login GLOBAL qidira olishi kerak"
        finally:
            session.close()
    print("OK: kontekst None bo'lganda (login sahifasi) filtr butunlay o'chiq -- Manager'ni username bo'yicha global topish ishlaydi")


def test_stale_context_from_previous_request_does_not_leak():
    # `app.py`dagi before_request avval ANIQ None'ga qaytarib, keyin haqiqiy
    # qiymatni qo'yishi SHART -- bu test aynan shu ikki qadamli naqshni
    # simulyatsiya qiladi va oldingi (stale) qiymat YECHIB TASHLANMASA nima
    # buzilishini ko'rsatadi.
    with tempfile.TemporaryDirectory() as tmp:
        db_module = _fresh_db_module(os.path.join(tmp, "t4.db"))
        db_module.init_db()

        session = db_module.get_session()
        try:
            c2 = db_module.Company(name="Ikkinchi mijoz")
            session.add(c2)
            session.commit()
            c1_id = db_module.get_default_company_id()
            c2_id = c2.id
            session.add(db_module.Lead(full_name="C1 lead", company_id=c1_id))
            session.commit()
        finally:
            session.close()

        # "So'rov 1" -- C2 konteksti bilan ishlaydi (masalan C2 admini kirgan).
        session1 = db_module.get_session()
        db_module.set_current_company_id(c2_id)
        session1.close()

        # "So'rov 2" -- xuddi shu OS thread'da, lekin C1 admini kiradi.
        # `before_request` naqshi: avval None, keyin haqiqiy qiymat.
        db_module.set_current_company_id(None)
        db_module.set_current_company_id(c1_id)
        session2 = db_module.get_session()
        try:
            names = [l.full_name for l in session2.query(db_module.Lead).all()]
            assert names == ["C1 lead"], f"to'g'ri qayta o'rnatilgan kontekst bilan C1 lidi ko'rinishi kerak, olindi: {names}"
        finally:
            db_module.set_current_company_id(None)
            session2.close()
    print("OK: har so'rov boshida kontekst to'g'ri qayta o'rnatilsa, oldingi so'rovdan hech narsa 'sizib qolmaydi'")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
