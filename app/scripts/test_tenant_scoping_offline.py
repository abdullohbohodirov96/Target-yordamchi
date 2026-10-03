"""test_tenant_scoping_offline.py — `db.py`dagi multi-tenant mexanizmini
(`set_current_company_id()` + `with_loader_criteria` + `do_orm_execute`
hodisasi) TARMOQSIZ tekshiradi. Haqiqiy Postgres'ga ULANMAYDI --
vaqtinchalik SQLite baza.

Bu MARKAZIY mexanizm -- ilovadagi 70+ so'rov joyi (leads, sales, managers,
calls, competitors, ...) qo'lda emas, aynan shu BITTA filtr orqali
kompaniya bo'yicha ajratiladi, shuning uchun bu fayl ALOHIDA, puxta
tekshiriladi:
  1. `company_id` o'rnatilgan bo'lsa -- `session.query()`, `session.get()`
     va lazy-load HAMMASI faqat o'sha kompaniyaning qatorlarini qaytaradi.
  2. (2026-09-30, docs/PLAN.md 1-bosqich -- "yopiq holatda yiqiladigan")
     kontekst o'rnatilmagan bo'lsa -- tenant jadvaliga so'rov
     `TenantScopeError` bilan RAD ETILADI. Filtrsiz qidiruv (login) FAQAT
     `db.unscoped()` orqali.
  3. UPDATE/DELETE so'rovlari ham joriy kompaniya bilan cheklanadi --
     boshqa kompaniyaning qatorini o'zgartirib/o'chirib bo'lmaydi.
  4. Kontekst har doim BOSHIDA tozalanib, keyin qayta o'rnatilishi kerak
     (oldingi so'rovdan "sizib qolgan" qiymat keyingisini buzmasin).
  5. `TENANT_SCOPE_MODE=warn` -- favqulodda zaxira: rad etmaydi, log yozadi.

Ishga tushirish:
    cd app && python3 scripts/test_tenant_scoping_offline.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_db_module(db_path):
    if "db" in sys.modules:
        del sys.modules["db"]
    os.environ.pop("REPLIX_TEST_DEFAULT_UNSCOPED", None)  # bu fayl ATAYLAB qat'iy rejimda
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    import db as db_module
    return db_module


def _seed_two_companies(db_module, *, leads=False, manager_for_c2=None):
    """Ikki kompaniya (standart + "Ikkinchi mijoz") va ixtiyoriy lid/menejer.
    Tayyorlash -- ATAYLAB `unscoped()` ichida (migratsiya kabi)."""
    session = db_module.get_session()
    try:
        with db_module.unscoped():
            c2 = db_module.Company(name="Ikkinchi mijoz")
            session.add(c2)
            session.commit()
            c1_id = db_module.get_default_company_id()
            c2_id = c2.id
            if leads:
                session.add(db_module.Lead(full_name="C1 lead", company_id=c1_id))
                session.add(db_module.Lead(full_name="C2 lead", company_id=c2_id))
            m2_id = None
            if manager_for_c2:
                m2 = db_module.Manager(username=manager_for_c2, password_hash="x", company_id=c2_id)
                session.add(m2)
                session.commit()
                m2_id = m2.id
            session.commit()
    finally:
        session.close()
    return c1_id, c2_id, m2_id


def test_query_filtered_by_current_company():
    with tempfile.TemporaryDirectory() as tmp:
        db_module = _fresh_db_module(os.path.join(tmp, "t1.db"))
        db_module.init_db()
        c1_id, c2_id, _ = _seed_two_companies(db_module, leads=True)

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
    # MUHIM: session.get() HAM filtrlanishi kerak -- aks holda boshqa
    # kompaniyaning ID'sini URL'da qo'lda kiritib ko'rish orqali (masalan
    # /managers/<id>/edit) uning ma'lumotini ko'rish mumkin bo'lib qolardi.
    with tempfile.TemporaryDirectory() as tmp:
        db_module = _fresh_db_module(os.path.join(tmp, "t2.db"))
        db_module.init_db()
        c1_id, c2_id, m2_id = _seed_two_companies(db_module, manager_for_c2="boshqa_kompaniya_admin")

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
    print("OK: session.get() ham kompaniya bo'yicha to'g'ri filtrlanadi")


def test_missing_context_is_rejected_fail_closed():
    # ASOSIY yangi qoida: kontekst yo'q -> tenant jadvaliga so'rov RAD ETILADI
    # (ilgari JIM-JIT barcha kompaniyalarning ma'lumoti qaytarilardi).
    with tempfile.TemporaryDirectory() as tmp:
        db_module = _fresh_db_module(os.path.join(tmp, "t3.db"))
        db_module.init_db()
        _seed_two_companies(db_module, leads=True, manager_for_c2="c2_admin")

        session = db_module.get_session()
        try:
            db_module.set_current_company_id(None)
            for label, run in [
                ("query(Lead)", lambda: session.query(db_module.Lead).all()),
                ("query(Manager) by username", lambda: session.query(db_module.Manager).filter_by(username="c2_admin").first()),
                ("get(Manager)", lambda: session.get(db_module.Manager, 1)),
                ("query(Lead).delete()", lambda: session.query(db_module.Lead).delete()),
                ("query(Lead).update()", lambda: session.query(db_module.Lead).update({"full_name": "x"})),
            ]:
                try:
                    run()
                except db_module.TenantScopeError:
                    session.rollback()
                    continue
                raise AssertionError(f"{label}: kontekstsiz so'rov RAD ETILISHI kerak edi")

            # Tenant bo'lmagan jadval (Company) -- kontekstsiz ham ishlaydi.
            assert session.query(db_module.Company).count() == 2
        finally:
            session.close()

        # Hech narsa o'chmagan/o'zgarmaganini tekshiramiz.
        session = db_module.get_session()
        try:
            with db_module.unscoped():
                names = sorted(l.full_name for l in session.query(db_module.Lead).all())
            assert names == ["C1 lead", "C2 lead"], names
        finally:
            session.close()
    print("OK: kontekstsiz tenant-so'rov (SELECT/get/UPDATE/DELETE) TenantScopeError bilan rad etiladi, Company jadvali esa ochiq")


def test_unscoped_allows_global_lookup_for_login():
    # `/login` -- foydalanuvchi HALI aniqlanmagan, Manager'ni username
    # bo'yicha GLOBAL topish kerak -- endi FAQAT ataylab `unscoped()` orqali.
    with tempfile.TemporaryDirectory() as tmp:
        db_module = _fresh_db_module(os.path.join(tmp, "t4.db"))
        db_module.init_db()
        _seed_two_companies(db_module, manager_for_c2="c2_admin")

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                assert db_module.is_unscoped()
                found = session.query(db_module.Manager).filter_by(username="c2_admin").first()
            assert found is not None, "unscoped() ichida login GLOBAL qidira olishi kerak"
            assert not db_module.is_unscoped(), "unscoped() blokidan keyin kontekst qaytishi kerak"
        finally:
            session.close()
    print("OK: unscoped() bloki ichida (login) filtr ataylab o'chiq, blokdan keyin yana yopiq")


def test_update_and_delete_limited_to_current_company():
    with tempfile.TemporaryDirectory() as tmp:
        db_module = _fresh_db_module(os.path.join(tmp, "t5.db"))
        db_module.init_db()
        c1_id, c2_id, _ = _seed_two_companies(db_module, leads=True)

        session = db_module.get_session()
        try:
            with db_module.scoped_as(c1_id):
                updated = session.query(db_module.Lead).update({"status": "won"}, synchronize_session=False)
                session.commit()
                assert updated == 1, f"UPDATE faqat C1 lidiga tegishi kerak, tegdi: {updated}"
                deleted = session.query(db_module.Lead).delete(synchronize_session=False)
                session.commit()
                assert deleted == 1, f"DELETE faqat C1 lidini o'chirishi kerak, o'chirdi: {deleted}"
        finally:
            session.close()

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                rows = [(l.full_name, l.status) for l in session.query(db_module.Lead).all()]
            assert len(rows) == 1 and rows[0][0] == "C2 lead", rows
            assert rows[0][1] != "won", f"C2 lidi o'zgarmasligi kerak edi: {rows}"
        finally:
            session.close()
    print("OK: UPDATE/DELETE faqat joriy kompaniya qatorlariga ta'sir qiladi -- C2 lidi butun qoldi")


def test_stale_context_from_previous_request_does_not_leak():
    # `app.py`dagi before_request avval kontekstni tozalab, keyin haqiqiy
    # qiymatni qo'yadi -- oldingi (stale) qiymat keyingi so'rovni buzmasin.
    with tempfile.TemporaryDirectory() as tmp:
        db_module = _fresh_db_module(os.path.join(tmp, "t6.db"))
        db_module.init_db()
        c1_id, c2_id, _ = _seed_two_companies(db_module)
        session = db_module.get_session()
        try:
            with db_module.unscoped():
                session.add(db_module.Lead(full_name="C1 lead", company_id=c1_id))
                session.commit()
        finally:
            session.close()

        # "So'rov 1" -- C2 konteksti bilan ishlaydi.
        token1 = db_module.push_company_context(None)
        db_module.set_current_company_id(c2_id)
        db_module.pop_company_context(token1)
        assert db_module.get_current_company_id() is None, "teardown'dan keyin kontekst tozalanishi kerak"

        # "So'rov 2" -- xuddi shu thread'da, C1 admini.
        token2 = db_module.push_company_context(None)
        db_module.set_current_company_id(c1_id)
        session2 = db_module.get_session()
        try:
            names = [l.full_name for l in session2.query(db_module.Lead).all()]
            assert names == ["C1 lead"], f"to'g'ri qayta o'rnatilgan kontekst bilan C1 lidi ko'rinishi kerak, olindi: {names}"
        finally:
            db_module.pop_company_context(token2)
            session2.close()
    print("OK: har so'rov boshida kontekst to'g'ri qayta o'rnatiladi, oldingi so'rovdan hech narsa 'sizib qolmaydi'")


def test_warn_mode_is_emergency_fallback():
    with tempfile.TemporaryDirectory() as tmp:
        db_module = _fresh_db_module(os.path.join(tmp, "t7.db"))
        db_module.init_db()
        _seed_two_companies(db_module, leads=True)
        os.environ["TENANT_SCOPE_MODE"] = "warn"
        session = db_module.get_session()
        try:
            db_module.set_current_company_id(None)
            names = sorted(l.full_name for l in session.query(db_module.Lead).all())
            assert names == ["C1 lead", "C2 lead"], names
        finally:
            os.environ.pop("TENANT_SCOPE_MODE", None)
            session.close()
    print("OK: TENANT_SCOPE_MODE=warn -- favqulodda zaxira (rad etmaydi, faqat log)")


def test_company_scoped_decorator_uses_company_argument():
    with tempfile.TemporaryDirectory() as tmp:
        db_module = _fresh_db_module(os.path.join(tmp, "t8.db"))
        db_module.init_db()
        c1_id, c2_id, _ = _seed_two_companies(db_module, leads=True)

        @db_module.company_scoped
        def lead_names(company=None):
            session = db_module.get_session()
            try:
                return [l.full_name for l in session.query(db_module.Lead).all()]
            finally:
                session.close()

        class _C:
            def __init__(self, id):
                self.id = id

        assert lead_names(company=_C(c2_id)) == ["C2 lead"]
        assert lead_names() == ["C1 lead"], "company berilmasa -- standart (platforma egasi) kompaniya"
        assert db_module.get_current_company_id() is None, "dekoratordan keyin kontekst tiklanishi kerak"
    print("OK: @company_scoped -- fon vazifasi company= bo'yicha to'g'ri kontekstda ishlaydi")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
