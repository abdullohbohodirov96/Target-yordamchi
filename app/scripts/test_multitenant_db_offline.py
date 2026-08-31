"""test_multitenant_db_offline.py — `db.py`ga qo'shilgan multi-tenant
1-bosqich (`Company` jadvali, `company_id` ustunlari, `ensure_default_company()`)
uchun TARMOQSIZ (offline) tekshiruv. Haqiqiy Postgres'ga ULANMAYDI -- vaqtinchalik
faylga asoslangan SQLite baza ishlatiladi (SQLAlchemy modellari bir xil,
`ensure_default_company()`ning o'zi PostgreSQL'ga xos hech narsa qilmaydi).

MUHIM: `_migrate_widen_columns()` (`call_records.ai_sale_result` ustuni
turini kengaytirish) SQLite'da "ALTER COLUMN ... TYPE" sintaksisini
qo'llab-quvvatlamagani uchun xato logi chiqaradi -- bu KUTILGAN va zararsiz
(bu funksiya faqat Postgres uchun mo'ljallangan, xato try/except bilan
ushlanadi, dastur davom etadi) -- shu test ham buni tasdiqlaydi.

Ishga tushirish:
    cd app && python3 scripts/test_multitenant_db_offline.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_db_module(db_path, env_overrides=None):
    """Har bir test o'z ALOHIDA SQLite fayli va `db` modulining TOZA
    nusxasi bilan ishlaydi -- `engine`/`SessionLocal` modul yuklanganda
    DATABASE_URL'ga bog'lanib qolgani uchun (import vaqtida global
    o'zgaruvchi sifatida yaratiladi), testlar orasida holat sizib
    o'tmasligi uchun `sys.modules`dan `db`ni har safar olib tashlab,
    qayta import qilamiz."""
    for mod_name in ("db",):
        if mod_name in sys.modules:
            del sys.modules[mod_name]
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    for k, v in (env_overrides or {}).items():
        os.environ[k] = v
    import db as db_module
    return db_module


def test_first_init_creates_default_company_from_env():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test1.db")
        db_module = _fresh_db_module(db_path, {
            "META_ACCESS_TOKEN": "tok_abc123",
            "META_AD_ACCOUNT_ID": "act_555",
            "TELEGRAM_AGENTS_GROUP_ID": "-1009999",
        })
        db_module.init_db()
        session = db_module.get_session()
        try:
            companies = session.query(db_module.Company).all()
            assert len(companies) == 1, f"aynan 1 ta standart kompaniya kutilgan edi, {len(companies)} ta topildi"
            c = companies[0]
            assert c.plan == "unlimited", f"standart kompaniya 'unlimited' tarifda bo'lishi kerak, olindi: {c.plan}"
            assert c.meta_access_token == "tok_abc123"
            assert c.meta_ad_account_id == "act_555"
            assert c.telegram_group_id == "-1009999"
        finally:
            session.close()
    print("OK: birinchi init_db() ENV o'zgaruvchilaridan standart kompaniya (Meta/Telegram ma'lumotlari bilan) yaratadi")


def test_existing_rows_backfilled_to_default_company():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test2.db")
        db_module = _fresh_db_module(db_path)
        db_module.init_db()

        # Standart voronka bosqichlari (`seed_default_funnel_stages`)
        # ALLAQACHON `ensure_default_company()` orqali backfill qilingan
        # bo'lishi kerak -- bitta init_db() chaqiruvida ikkalasi ham ishlaydi.
        session = db_module.get_session()
        try:
            stages = session.query(db_module.FunnelStage).all()
            assert len(stages) == 5
            assert all(s.company_id is not None for s in stages), "seed qilingan voronka bosqichlari company_id'siz qolib ketdi"
        finally:
            session.close()
    print("OK: bitta init_db() chaqiruvida yangi seed qilingan qatorlar HAM darhol company_id oladi")


def test_manually_inserted_null_row_backfilled_on_next_init():
    # Production'dagi haqiqiy holatni simulyatsiya qiladi: `company_id`
    # ustuni yangi qo'shilganda, ALLAQACHON mavjud (eski) qatorlar avval
    # NULL bo'ladi -- keyingi ishga tushirishda (deploy/restart) ular
    # avtomatik standart kompaniyaga biriktirilishi SHART.
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test3.db")
        db_module = _fresh_db_module(db_path)
        db_module.init_db()

        session = db_module.get_session()
        try:
            old_manager = db_module.Manager(username="eski_admin", password_hash="x", company_id=None)
            session.add(old_manager)
            session.commit()
            manager_id = old_manager.id
            assert old_manager.company_id is None
        finally:
            session.close()

        # Ilovaning KEYINGI ishga tushishini simulyatsiya qiladi (masalan
        # Render'da qayta deploy) -- BIR XIL bazaga qarshi.
        db_module.init_db()

        session = db_module.get_session()
        try:
            refreshed = session.query(db_module.Manager).filter_by(id=manager_id).first()
            assert refreshed.company_id is not None, "eski (company_id=NULL) qator keyingi init_db()da backfill qilinmadi"
            default_company = session.query(db_module.Company).first()
            assert refreshed.company_id == default_company.id
        finally:
            session.close()
    print("OK: company_id=NULL bo'lgan eski qator KEYINGI ishga tushirishda standart kompaniyaga avtomatik biriktiriladi")


def test_second_init_does_not_create_duplicate_company():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test4.db")
        db_module = _fresh_db_module(db_path)
        db_module.init_db()
        db_module.init_db()
        db_module.init_db()

        session = db_module.get_session()
        try:
            companies = session.query(db_module.Company).all()
            assert len(companies) == 1, f"init_db() bir necha marta chaqirilsa ham FAQAT 1 ta standart kompaniya bo'lishi kerak, {len(companies)} ta topildi"
        finally:
            session.close()
    print("OK: init_db() bir necha marta chaqirilsa ham ikkinchi/uchinchi 'standart kompaniya' YARATILMAYDI")


def test_company_password_helpers():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test5.db")
        db_module = _fresh_db_module(db_path)
        db_module.init_db()
        c = db_module.Company(name="Test MChJ", email="test@example.com")
        c.set_password("mening-parolim-123")
        assert c.password_hash and c.password_hash != "mening-parolim-123"
        assert c.check_password("mening-parolim-123") is True
        assert c.check_password("notogri-parol") is False

        c_no_password = db_module.Company(name="Parolsiz kompaniya")
        assert c_no_password.check_password("hech-narsa") is False, "password_hash yo'q bo'lsa check_password xato ko'tarmasdan False qaytarishi kerak"
    print("OK: Company.set_password/check_password Manager'dagi bilan bir xil xavfsiz naqshda ishlaydi")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
