"""test_call_sync_multitenant_offline.py -- 2026-09, Item J xavfsizlik/
ishonchlilik auditi (🔴 KRITIK, 7-band: "Moy Zvonki call-sync hardcoded to
one company"). Ilgari `call_sync.py` BUTUNLAY global environment
o'zgaruvchilari (MOIZVONKI_API_ADDRESS/API_KEY/USER_NAME) orqali ishlardi
-- boshqa hech qanday kompaniya o'z Moi Zvonki hisobini ulay olmasdi, va
ulasa ham (chunki bu maydonlar umuman yo'q edi) BARCHA qo'ng'iroq yozuvi
majburan `company_id=1`ga (`db.get_default_company_id()`) yozilardi.

Bu fayl tekshiradi:
  1. `sync_once(company=...)` -- kompaniyaning O'Z Moi Zvonki hisobi bilan
     so'raladi, FAQAT o'z menejerlariga biriktiriladi, va yaratilgan
     yozuvlar O'SHA kompaniyaning `company_id`siga yoziladi -- boshqa
     kompaniyaning bir xil telefon raqamli menejeriga ADASHIB
     biriktirilmaydi (kross-tenant izolyatsiya).
  2. `sync_all_companies()` -- platforma egasi (global ENV) VA har bir
     boshqa (o'z hisobini ulagan) kompaniya uchun ALOHIDA sinxronlaydi;
     ulamagan kompaniya o'tkazib yuboriladi.
  3. `reconcile_existing_records()` -- har bir yozuvni FAQAT o'z
     kompaniyasi menejerlariga solishtiradi (kross-tenant qayta
     biriktirmaydi).
  4. `Company.is_moizvonki_configured()` / `call_sync.is_configured_for()`
     -- to'g'ri holatni qaytaradi.

Ishga tushirish:
    cd app && python3 scripts/test_call_sync_multitenant_offline.py
"""

import os
import sys
import datetime as dt
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")


def _fresh_modules(db_path, *, moizvonki_address="", moizvonki_key="", moizvonki_user=""):
    for name in ("db", "call_sync", "kv_store"):
        sys.modules.pop(name, None)
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["MOIZVONKI_API_ADDRESS"] = moizvonki_address
    os.environ["MOIZVONKI_API_KEY"] = moizvonki_key
    os.environ["MOIZVONKI_USER_NAME"] = moizvonki_user
    import db as db_module
    db_module.init_db()
    import call_sync as call_sync_module
    return db_module, call_sync_module


def _make_company(db_module, *, name, moizvonki_api_address=None, moizvonki_user_name=None, moizvonki_api_key=None):
    session = db_module.get_session()
    try:
        c = db_module.Company(name=name, is_active=True)
        c.moizvonki_api_address = moizvonki_api_address
        c.moizvonki_user_name = moizvonki_user_name
        if moizvonki_api_key:
            c.set_moizvonki_api_key(moizvonki_api_key)
        session.add(c)
        session.commit()
        return c.id
    finally:
        session.close()


def _make_manager(db_module, *, company_id, phone_number=None, moizvonki_login=None, full_name="Menejer"):
    session = db_module.get_session()
    try:
        with db_module.unscoped():
            m = db_module.Manager(
                company_id=company_id, username=f"m{company_id}_{phone_number or moizvonki_login}",
                full_name=full_name, role="manager", phone_number=phone_number, moizvonki_login=moizvonki_login,
            )
            m.set_password("parol123")
            session.add(m)
            session.commit()
            return m.id
    finally:
        session.close()


def _fake_raw_call(*, src_number=None, client_number="998901112233", user_account=None, external_id="call-1"):
    return {
        "db_call_id": external_id,
        "src_number": src_number,
        "client_number": client_number,
        "user_account": user_account,
        "direction": 1,
        "duration": 42,
        "start_time": int(dt.datetime.utcnow().timestamp()),
        "recording": None,
    }


# ---------------------------------------------------------------------------
# 1) sync_once(company=...) -- o'z hisobi, o'z menejeri, kross-tenant emas
# ---------------------------------------------------------------------------

def test_sync_once_writes_to_own_company_and_matches_own_manager_only():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, call_sync_module = _fresh_modules(os.path.join(tmp, "a1.db"))

        company_a = _make_company(
            db_module, name="Kompaniya A", moizvonki_api_address="https://a.moizvonki.ru",
            moizvonki_user_name="admin-a@x.uz", moizvonki_api_key="key-a",
        )
        company_b = _make_company(
            db_module, name="Kompaniya B", moizvonki_api_address="https://b.moizvonki.ru",
            moizvonki_user_name="admin-b@x.uz", moizvonki_api_key="key-b",
        )
        # IKKALA kompaniyada ham XUDDI SHU telefon raqamli menejer bor --
        # bu tasodifiy kross-tenant moslikni simulyatsiya qiladi.
        manager_a = _make_manager(db_module, company_id=company_a, phone_number="+998901234567")
        manager_b = _make_manager(db_module, company_id=company_b, phone_number="+998901234567")

        seen_creds = []

        def fake_fetch_calls(since, *, api_address, api_key, user_name):
            seen_creds.append((api_address, api_key, user_name))
            return [_fake_raw_call(src_number="+998901234567", external_id="call-a-1")]

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                company_a_row = session.get(db_module.Company, company_a)
        finally:
            session.close()

        with mock.patch.object(call_sync_module, "_fetch_calls", side_effect=fake_fetch_calls):
            result = call_sync_module.sync_once(company=company_a_row)

        assert result["new_calls"] == 1, result
        assert seen_creds == [("https://a.moizvonki.ru", "key-a", "admin-a@x.uz")], (
            "sync_once(company=A) A'ning O'Z Moi Zvonki hisobi bilan chaqirilishi kerak edi"
        )

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                record = session.query(db_module.CallRecord).filter_by(external_id="call-a-1").first()
                assert record is not None
                assert record.company_id == company_a, "yozuv A kompaniyasiga yozilishi kerak edi"
                assert record.manager_id == manager_a, (
                    "menejer A kompaniyasining O'Zi bo'lishi kerak edi, B kompaniyasining bir xil "
                    "raqamli menejeriga ADASHIB biriktirilmasligi kerak"
                )
        finally:
            session.close()
    print("OK: sync_once(company=A) -- A'ning O'Z Moi Zvonki hisobi bilan so'raladi, yozuv A'ga yoziladi, manager_id ADASHMAY A'ning menejeriga bog'lanadi (bir xil raqamli B'ning menejeriga EMAS)")


def test_sync_once_without_company_uses_legacy_global_env_behaviour():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, call_sync_module = _fresh_modules(
            os.path.join(tmp, "a2.db"),
            moizvonki_address="https://owner.moizvonki.ru", moizvonki_key="owner-key", moizvonki_user="owner@x.uz",
        )
        owner_id = db_module.get_default_company_id()
        manager_owner = _make_manager(db_module, company_id=owner_id, phone_number="+998900000001")

        seen_creds = []

        def fake_fetch_calls(since, *, api_address, api_key, user_name):
            seen_creds.append((api_address, api_key, user_name))
            return [_fake_raw_call(src_number="+998900000001", external_id="call-owner-1")]

        with mock.patch.object(call_sync_module, "_fetch_calls", side_effect=fake_fetch_calls):
            result = call_sync_module.sync_once()  # company=None -- eski xatti-harakat

        assert result["new_calls"] == 1
        assert seen_creds == [("https://owner.moizvonki.ru", "owner-key", "owner@x.uz")]

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                record = session.query(db_module.CallRecord).filter_by(external_id="call-owner-1").first()
                assert record.company_id == owner_id
                assert record.manager_id == manager_owner
        finally:
            session.close()
    print("OK: sync_once() (company=None) -- eski global ENV xatti-harakati orqaga moslik uchun o'zgarmagan")


# ---------------------------------------------------------------------------
# 2) sync_all_companies() -- egasi + har bir ulagan kompaniya, ulamagan o'tkazib yuboriladi
# ---------------------------------------------------------------------------

def test_sync_all_companies_covers_owner_and_opted_in_companies_only():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, call_sync_module = _fresh_modules(
            os.path.join(tmp, "b1.db"),
            moizvonki_address="https://owner.moizvonki.ru", moizvonki_key="owner-key", moizvonki_user="owner@x.uz",
        )
        owner_id = db_module.get_default_company_id()
        company_configured = _make_company(
            db_module, name="Ulagan", moizvonki_api_address="https://c.moizvonki.ru",
            moizvonki_user_name="admin-c@x.uz", moizvonki_api_key="key-c",
        )
        _company_unconfigured = _make_company(db_module, name="Ulamagan")

        calls = []

        def fake_sync_once(since=None, company=None):
            calls.append(company.id if company else "owner")
            return {"configured": True, "new_calls": 0, "skipped_unmatched": 0, "errors": []}

        with mock.patch.object(call_sync_module, "sync_once", side_effect=fake_sync_once):
            result = call_sync_module.sync_all_companies()

        assert calls[0] == "owner", "birinchi navbatda platforma egasi (global ENV) sinxronlanishi kerak"
        assert company_configured in calls, "o'z hisobini ulagan kompaniya sinxronlanishi kerak edi"
        assert _company_unconfigured not in calls, "ulamagan kompaniya butunlay o'tkazib yuborilishi kerak"
        assert result["companies_synced"] == 1
        assert owner_id not in [c for c in calls if c != "owner"], "egasi ikki marta (ham 'owner', ham o'z ID'si bilan) sinxronlanmasligi kerak"
    print("OK: sync_all_companies() -- platforma egasi (global ENV) + FAQAT o'z hisobini ulagan kompaniyalar, ulamaganlar o'tkazib yuboriladi")


# ---------------------------------------------------------------------------
# 3) reconcile_existing_records() -- har bir yozuv FAQAT o'z kompaniyasi menejerlariga
# ---------------------------------------------------------------------------

def test_reconcile_does_not_cross_assign_manager_across_companies():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, call_sync_module = _fresh_modules(os.path.join(tmp, "c1.db"))
        company_a = _make_company(db_module, name="A")
        company_b = _make_company(db_module, name="B")
        manager_a = _make_manager(db_module, company_id=company_a, phone_number="+998911112233")
        # B'da BIR XIL raqamli menejer YO'Q -- faqat A'da bor.

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                # B kompaniyasiga tegishli yozuv, lekin A'ning menejeriga TASODIFAN
                # (eski, buzuq mantiqdan qolgan) biriktirilgan holatni simulyatsiya qilamiz.
                rec = db_module.CallRecord(
                    company_id=company_b, external_id="rec-b-1", manager_id=manager_a,
                    manager_phone_number="+998911112233", phone_number="+998900000000",
                    direction="incoming", duration_seconds=10, raw_data="{}",
                )
                session.add(rec)
                session.commit()
                rec_id = rec.id
        finally:
            session.close()

        stats = call_sync_module.reconcile_existing_records()
        assert stats["deleted"] == 1 and stats["kept"] == 0, (
            f"B kompaniyasining yozuvi A'ning menejeriga mos kelgani uchun EMAS, B'DA "
            f"bunday menejer topilmagani uchun o'chirilishi kerak edi: {stats}"
        )
        session = db_module.get_session()
        try:
            with db_module.unscoped():
                assert session.get(db_module.CallRecord, rec_id) is None
        finally:
            session.close()
    print("OK: reconcile_existing_records() -- yozuv boshqa kompaniyaning menejeriga mos kelgani uchun SAQLANIB QOLMAYDI (kross-tenant qayta biriktirish yo'q), o'z kompaniyasida mos topilmasa o'chiriladi")


def test_reconcile_reassigns_within_same_company():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, call_sync_module = _fresh_modules(os.path.join(tmp, "c2.db"))
        company_a = _make_company(db_module, name="A")
        manager_a_new = _make_manager(db_module, company_id=company_a, phone_number="+998911112233")

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                rec = db_module.CallRecord(
                    company_id=company_a, external_id="rec-a-1", manager_id=None,
                    manager_phone_number="+998911112233", phone_number="+998900000000",
                    direction="incoming", duration_seconds=10, raw_data="{}",
                )
                session.add(rec)
                session.commit()
                rec_id = rec.id
        finally:
            session.close()

        stats = call_sync_module.reconcile_existing_records()
        assert stats["reassigned"] == 1 and stats["kept"] == 1 and stats["deleted"] == 0, stats
        session = db_module.get_session()
        try:
            with db_module.unscoped():
                rec = session.get(db_module.CallRecord, rec_id)
                assert rec.manager_id == manager_a_new
        finally:
            session.close()
    print("OK: reconcile_existing_records() -- O'Z kompaniyasi ichida mos menejer topilsa TO'G'RI biriktiriladi")


# ---------------------------------------------------------------------------
# 4) is_moizvonki_configured() / is_configured_for()
# ---------------------------------------------------------------------------

def test_is_configured_for_owner_falls_back_to_global_env():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, call_sync_module = _fresh_modules(
            os.path.join(tmp, "d1.db"),
            moizvonki_address="https://owner.moizvonki.ru", moizvonki_key="owner-key", moizvonki_user="owner@x.uz",
        )
        owner_id = db_module.get_default_company_id()
        session = db_module.get_session()
        try:
            with db_module.unscoped():
                owner = session.get(db_module.Company, owner_id)
                assert owner.is_moizvonki_configured() is False, "egasi o'z ustunlarini to'ldirmagan"
                assert call_sync_module.is_configured_for(owner) is True, (
                    "egasi uchun global ENV orqaga moslik sifatida 'ulangan' deb ko'rsatilishi kerak"
                )
        finally:
            session.close()
    print("OK: is_configured_for() -- platforma egasi uchun global ENV orqaga moslik sifatida ishlaydi")


def test_is_configured_for_other_company_requires_own_connection():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, call_sync_module = _fresh_modules(
            os.path.join(tmp, "d2.db"),
            moizvonki_address="https://owner.moizvonki.ru", moizvonki_key="owner-key", moizvonki_user="owner@x.uz",
        )
        company_a = _make_company(db_module, name="A")
        session = db_module.get_session()
        try:
            with db_module.unscoped():
                a = session.get(db_module.Company, company_a)
                assert call_sync_module.is_configured_for(a) is False, (
                    "boshqa kompaniya o'z hisobini ulamagan bo'lsa -- egasining global ENV'i UNGA tegishli EMAS"
                )
                a.moizvonki_api_address = "https://a.moizvonki.ru"
                a.moizvonki_user_name = "admin-a@x.uz"
                a.set_moizvonki_api_key("key-a")
                session.commit()
                assert call_sync_module.is_configured_for(a) is True
        finally:
            session.close()
    print("OK: is_configured_for() -- boshqa kompaniya FAQAT O'Zi ulagandan keyin 'ulangan' deb ko'rsatiladi (egasining global ENV'idan foydalana olmaydi)")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
