"""test_billing_trial_and_reminders_offline.py -- 2026-09, foydalanuvchi
so'rovi (Item D, "Trial muddati/limitlarini o'zgartirish" +
"To'lov jarayonini yaxshilash"):

  1. Sinov (trial) muddati 14 kundan 7 kunga qisqartirildi
     (`plans.py: PLANS["trial"].period_days`) -- ochiq ro'yxatdan o'tish
     (`/signup`) VA platforma egasi qo'lda yaratadigan (`/companies`)
     kompaniya, IKKALASI ham shu YAGONA qiymatdan hisoblaydi.
  2. `scheduler.job_trial_expiry_warning()` -- sinov muddati tugashiga
     3 kun (yoki kamroq) qolgan kompaniyalarga BIR MARTA Telegram
     ogohlantirishi (avval bunday ogohlantirish UMUMAN yo'q edi).
  3. `scheduler.job_payme_autopay()` -- GRACE davrining SO'NGGI kunida
     oddiy ogohlantirishdan FARQLI, aniqroq "obuna to'xtatiladi" xabari.

Ishga tushirish:
    cd app && python3 scripts/test_billing_trial_and_reminders_offline.py
"""

import os
import sys
import datetime as dt
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ.setdefault("META_APP_ID", "test_app_id")
os.environ.setdefault("META_APP_SECRET", "test_app_secret")


def _fresh_modules(db_path, *, payme_merchant_id="", payme_test_key=""):
    for name in (
        "db", "app", "scheduler", "payme_subscribe", "kv_store", "orchestrator",
        "budget_tracker", "plans", "meta_api",
    ):
        sys.modules.pop(name, None)
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"
    os.environ["PAYME_MERCHANT_ID"] = payme_merchant_id
    os.environ["PAYME_TEST_KEY"] = payme_test_key
    os.environ["PAYME_TEST_MODE"] = "true"
    os.environ.pop("PAYME_KEY", None)

    import db as db_module
    db_module.init_db()
    import app as app_module
    app_module.app.config["TESTING"] = True
    import scheduler as scheduler_module
    import plans as plans_module
    return db_module, app_module, scheduler_module, plans_module


def _make_company(db_module, *, name, plan="trial", paid_until=None, telegram_group_id=None):
    session = db_module.get_session()
    try:
        c = db_module.Company(name=name, is_active=True, plan=plan, paid_until=paid_until, telegram_group_id=telegram_group_id)
        session.add(c)
        session.commit()
        return c.id
    finally:
        session.close()


def _add_admin(db_module, company_id, *, telegram_user_id=None, username=None):
    session = db_module.get_session()
    try:
        m = db_module.Manager(
            username=username or f"admin{company_id}", full_name="Admin", role="admin",
            company_id=company_id, telegram_user_id=telegram_user_id,
        )
        m.set_password("parol123")
        session.add(m)
        session.commit()
        return m.id
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 1) Trial muddati -- 14 kundan 7 kunga
# ---------------------------------------------------------------------------

def test_trial_plan_period_is_seven_days():
    with tempfile.TemporaryDirectory() as tmp:
        _db, _app, _sched, plans_module = _fresh_modules(os.path.join(tmp, "p1.db"))
        assert plans_module.PLANS["trial"].period_days == 7, (
            "Sinov tarifi endi 7 kun bo'lishi kerak (foydalanuvchi so'rovi bo'yicha, avval 14 kun edi)"
        )
    print("OK: plans.py -- sinov tarifi 7 kunga qisqartirildi")


def test_signup_sets_paid_until_seven_days_out():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module, _sched, _plans = _fresh_modules(os.path.join(tmp, "p2.db"))
        client = app_module.app.test_client()
        before = dt.datetime.utcnow()
        r = client.post("/signup", data={
            "company_name": "Yangi Kompaniya", "admin_username": "yangi_admin",
            "admin_full_name": "", "email": "", "plan": "trial",
            "password": "parol123456", "password2": "parol123456",
        }, follow_redirects=True)
        assert r.status_code == 200

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                c = session.query(db_module.Company).filter_by(name="Yangi Kompaniya").first()
            assert c is not None
            delta = c.paid_until - before
            assert dt.timedelta(days=6, hours=23) < delta < dt.timedelta(days=7, hours=1), (
                f"paid_until ~7 kundan keyin bo'lishi kerak, oldi: {delta}"
            )
        finally:
            session.close()
    print("OK: /signup orqali ochilgan sinov kompaniyasi paid_until'i ~7 kundan keyin")


def test_admin_created_company_respects_selected_plans_period():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module, _sched, plans_module = _fresh_modules(os.path.join(tmp, "p3.db"))
        session = db_module.get_session()
        try:
            owner = db_module.Manager(username="owner_x", full_name="Owner", role="admin", company_id=1)
            owner.set_password("parol123")
            session.add(owner)
            session.commit()
        finally:
            session.close()

        client = app_module.app.test_client()
        client.post("/login", data={"username": "owner_x", "password": "parol123"})
        before = dt.datetime.utcnow()

        # 2026-09 TUZATISH: ilgari bu yer TANLANGAN tarifdan qat'iy nazar
        # HAR DOIM 14 kunga qattiq yozilgan edi -- endi "biznes" (pullik)
        # tanlansa qisqa to'lov-muhlati (_SIGNUP_GRACE_DAYS), "sinov"
        # tanlansa esa plans.py'dagi haqiqiy sinov muddati qo'llanishi kerak.
        r = client.post("/companies", data={"name": "Pullik Kompaniya", "email": "", "plan": "business"}, follow_redirects=True)
        assert r.status_code == 200

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                c = session.query(db_module.Company).filter_by(name="Pullik Kompaniya").first()
            assert c is not None
            delta = c.paid_until - before
            assert delta < dt.timedelta(days=6), (
                f"pullik tarif uchun QISQA to'lov-muhlati (eski 14 kunlik sinov muddati EMAS) bo'lishi kerak: {delta}"
            )
        finally:
            session.close()
    print("OK: /companies (admin qo'lda yaratish) endi 14 kunga qattiq yozilmagan -- tanlangan tarifga mos muddat qo'llaniladi")


# ---------------------------------------------------------------------------
# 2) scheduler.job_trial_expiry_warning()
# ---------------------------------------------------------------------------

def test_trial_warning_sent_to_admins_personal_chat():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, _app, scheduler_module, _plans = _fresh_modules(os.path.join(tmp, "w1.db"))
        paid_until = dt.datetime.utcnow() + dt.timedelta(days=2)
        cid = _make_company(db_module, name="Tez tugaydigan", plan="trial", paid_until=paid_until, telegram_group_id="-8001")
        _add_admin(db_module, cid, telegram_user_id="555111")

        with mock.patch.object(scheduler_module, "_tg_send", return_value={"ok": True, "error": None}) as m_tg:
            results = scheduler_module.job_trial_expiry_warning()

        assert "yuborildi" in results[cid]
        assert m_tg.call_count == 1
        sent_chat_id, sent_text = m_tg.call_args[0]
        assert sent_chat_id == 555111, "admin botga ulangan bo'lsa, shaxsiy chatiga borishi kerak (guruhga emas)"
        assert "tugaydi" in sent_text and "tariflar" in sent_text
    print("OK: job_trial_expiry_warning() -- muddat tugashiga 2 kun qolgan kompaniyaga, admin shaxsiy chatiga ogohlantirish yuboradi")


def test_trial_warning_falls_back_to_group_when_no_admin_telegram():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, _app, scheduler_module, _plans = _fresh_modules(os.path.join(tmp, "w2.db"))
        paid_until = dt.datetime.utcnow() + dt.timedelta(days=1)
        cid = _make_company(db_module, name="Guruh orqali", plan="trial", paid_until=paid_until, telegram_group_id="-8002")
        _add_admin(db_module, cid, telegram_user_id=None)

        with mock.patch.object(scheduler_module, "_tg_send", return_value={"ok": True, "error": None}) as m_tg:
            results = scheduler_module.job_trial_expiry_warning()

        assert "yuborildi" in results[cid]
        assert m_tg.call_args[0][0] == -8002
    print("OK: job_trial_expiry_warning() -- adminning shaxsiy Telegram'i yo'q bo'lsa, kompaniya guruhiga tushadi")


def test_trial_warning_not_sent_twice_for_same_deadline():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, _app, scheduler_module, _plans = _fresh_modules(os.path.join(tmp, "w3.db"))
        paid_until = dt.datetime.utcnow() + dt.timedelta(days=1)
        cid = _make_company(db_module, name="Ikki marta emas", plan="trial", paid_until=paid_until, telegram_group_id="-8003")

        with mock.patch.object(scheduler_module, "_tg_send", return_value={"ok": True, "error": None}) as m_tg:
            r1 = scheduler_module.job_trial_expiry_warning()
            r2 = scheduler_module.job_trial_expiry_warning()

        assert "yuborildi" in r1[cid]
        assert "allaqachon" in r2[cid]
        assert m_tg.call_count == 1, "xuddi shu paid_until uchun IKKINCHI marta yuborilmasligi kerak"
    print("OK: job_trial_expiry_warning() -- bir xil muddat uchun ikkinchi marta qayta yubormaydi (idempotentlik)")


def test_trial_warning_skips_company_far_from_expiry():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, _app, scheduler_module, _plans = _fresh_modules(os.path.join(tmp, "w4.db"))
        paid_until = dt.datetime.utcnow() + dt.timedelta(days=6)  # hali 3 kunlik oynadan uzoq
        cid = _make_company(db_module, name="Hali erta", plan="trial", paid_until=paid_until, telegram_group_id="-8004")

        with mock.patch.object(scheduler_module, "_tg_send", return_value={"ok": True, "error": None}) as m_tg:
            scheduler_module.job_trial_expiry_warning()

        assert m_tg.call_count == 0
    print("OK: job_trial_expiry_warning() -- muddat tugashiga hali ko'p qolgan kompaniyaga tegmaydi")


def test_trial_warning_skips_paid_plans():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, _app, scheduler_module, _plans = _fresh_modules(os.path.join(tmp, "w5.db"))
        paid_until = dt.datetime.utcnow() + dt.timedelta(days=1)
        _make_company(db_module, name="Pullik", plan="business", paid_until=paid_until, telegram_group_id="-8005")

        with mock.patch.object(scheduler_module, "_tg_send", return_value={"ok": True, "error": None}) as m_tg:
            scheduler_module.job_trial_expiry_warning()

        assert m_tg.call_count == 0, "bu FAQAT sinov (trial) tarifiga tegishli, pullik tarifga (masalan to'lov kechikkan) tegmasligi kerak"
    print("OK: job_trial_expiry_warning() -- pullik tarifdagi kompaniyalarga tegmaydi (bu Payme autopay'ning ishi)")


# ---------------------------------------------------------------------------
# 3) scheduler.job_payme_autopay() -- yakuniy ogohlantirish
# ---------------------------------------------------------------------------

def test_payme_autopay_final_grace_day_sends_escalated_message():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module, scheduler_module, _plans = _fresh_modules(
            os.path.join(tmp, "f1.db"), payme_merchant_id="M", payme_test_key="K",
        )
        import payme_subscribe as payme_module
        # GRACE = 3 kun -- so'rov oynasi `paid_until >= now - 3 kun` bilan
        # cheklangan (3 kundan OSHIQ kechikkan kompaniya butunlay ro'yxatdan
        # tushadi), "yakuniy urinish" esa `paid_until <= now - 2 kun`da
        # boshlanadi -- shu ikkalasi orasidagi 2.5 kun oldingi muddat ham
        # so'rovga tushadi, ham "so'nggi urinish" shartini qanoatlantiradi.
        paid_until = dt.datetime.utcnow() - dt.timedelta(days=2, hours=12)
        session = db_module.get_session()
        try:
            c = db_module.Company(name="Oxirgi urinish", is_active=True, plan="start", paid_until=paid_until, telegram_group_id="-9001")
            c.payme_autopay_enabled = True
            session.add(c)
            session.commit()
            c.set_payme_card_token("TOK-LAST")
            session.commit()
            cid = c.id
        finally:
            session.close()

        with mock.patch.object(payme_module, "create_receipt", return_value={"id": "R-LAST", "state": 1}), \
             mock.patch.object(payme_module, "pay_receipt", side_effect=payme_module.PaymeSubscribeError("Karta bloklangan", code=-31001)), \
             mock.patch.object(scheduler_module, "_tg_send", return_value={"ok": True, "error": None}) as m_tg:
            results = scheduler_module.job_payme_autopay()

        assert "to'lanmadi" in results[cid]
        assert m_tg.call_count == 1
        text = m_tg.call_args[0][1]
        assert "SO'NGGI urinish" in text and "TO'XTATILADI" in text, (
            f"GRACE davrining so'nggi kunida oddiy xabardan FARQLI, aniqroq yakuniy xabar kutilgan edi: {text}"
        )
    print("OK: job_payme_autopay() -- GRACE davrining so'nggi kunidagi muvaffaqiyatsiz urinishda aniqroq 'obuna to'xtatiladi' xabari yuboriladi")


def test_payme_autopay_early_grace_day_sends_regular_message():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module, scheduler_module, _plans = _fresh_modules(
            os.path.join(tmp, "f2.db"), payme_merchant_id="M", payme_test_key="K",
        )
        import payme_subscribe as payme_module
        paid_until = dt.datetime.utcnow() - dt.timedelta(hours=2)  # endigina tugagan
        session = db_module.get_session()
        try:
            c = db_module.Company(name="Birinchi urinish", is_active=True, plan="start", paid_until=paid_until, telegram_group_id="-9002")
            c.payme_autopay_enabled = True
            session.add(c)
            session.commit()
            c.set_payme_card_token("TOK-FIRST")
            session.commit()
            cid = c.id
        finally:
            session.close()

        with mock.patch.object(payme_module, "create_receipt", return_value={"id": "R-FIRST", "state": 1}), \
             mock.patch.object(payme_module, "pay_receipt", side_effect=payme_module.PaymeSubscribeError("Mablag' yetarli emas", code=-31001)), \
             mock.patch.object(scheduler_module, "_tg_send", return_value={"ok": True, "error": None}) as m_tg:
            scheduler_module.job_payme_autopay()

        text = m_tg.call_args[0][1]
        assert "SO'NGGI urinish" not in text, "GRACE davri endi boshlangan -- yakuniy xabar HALI kerak emas"
        assert "AMALGA OSHMADI" in text
    print("OK: job_payme_autopay() -- GRACE davrining boshida oddiy (yakuniy bo'lmagan) ogohlantirish yuboriladi")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
