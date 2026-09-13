"""test_payme_subscribe_offline.py — 2026-09, foydalanuvchi so'rovi:
"tolov avtomatik otishi uchun ... tolov kirganda paymega ruhsat olish
keremi ... tolov avtomatik paymega otib tolov qilinib kegn tolov
otkanini tekshriib boladigan qilish".

Payme SUBSCRIBE API orqali OYLIK obuna to'lovini mijoz kartasidan
AVTOMATIK yechib olish -- TARMOQSIZ (Payme'ga haqiqiy so'rov yubormasdan,
`requests.post`ni almashtirib) quyidagilarni tekshiradi:

  1. `payme_subscribe.py` past darajadagi JSON-RPC mijozi -- to'g'ri
     metod/parametr/`X-Auth` sarlavhasi bilan chaqiradi, Payme xato
     qaytarsa `PaymeSubscribeError` ko'taradi, ulanmagan (ENV yo'q)
     holatda TARMOQQA chiqmasdan darhol xato beradi.
  2. `/tolov/karta/*` route'lari -- kartani bog'lash (SMS tasdiqlash
     kerak bo'lgan/bo'lmagan holatlar), kod bilan tasdiqlash, o'chirish,
     avtoto'lovni yoqish/o'chirish -- HAMMASI faqat admin uchun va faqat
     JORIY kompaniyaga tegishli.
  3. `scheduler.job_payme_autopay()` -- muddati tugayotgan/tugagan
     kompaniyalarni topib, avtomatik to'laydi, `paid_until`ni 30 kunga
     uzaytiradi, Telegram'ga xabar beradi; MUHIM -- bir xil davr uchun
     IKKI MARTA pul yechib olinmaydi (idempotentlik), muvaffaqiyatsiz
     to'lov `paid_until`ni O'ZGARTIRMAYDI, va Payme hali sozlanmagan
     bo'lsa funksiya HECH NARSAGA tegmasdan darhol chiqadi.

Ishga tushirish:
    cd app && python3 scripts/test_payme_subscribe_offline.py
"""

import os
import sys
import tempfile
import datetime as dt
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")


def _fresh_modules(db_path, *, payme_merchant_id="", payme_test_key="", usd_to_uzs=None):
    """Har bir stsenariy (Payme sozlangan/sozlanmagan, boshqa kurs) uchun
    modullarni QAYTA yuklaydi -- `payme_subscribe.py` ENV o'zgaruvchilarini
    MODUL DARAJASIDA (import paytida) o'qiydi, shuning uchun ENV o'zgargach
    modulni qayta import qilmasdan o'zgarish ko'rinmaydi (loyihaning boshqa
    offline testlarida ham xuddi shu naqsh ishlatiladi)."""
    for name in ("db", "app", "scheduler", "payme_subscribe", "kv_store", "orchestrator", "budget_tracker"):
        sys.modules.pop(name, None)
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["PAYME_MERCHANT_ID"] = payme_merchant_id
    os.environ["PAYME_TEST_KEY"] = payme_test_key
    os.environ["PAYME_TEST_MODE"] = "true"
    os.environ.pop("PAYME_KEY", None)
    if usd_to_uzs is not None:
        os.environ["PAYME_USD_TO_UZS_RATE"] = str(usd_to_uzs)
    else:
        os.environ.pop("PAYME_USD_TO_UZS_RATE", None)
    os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"

    import db as db_module
    db_module.init_db()
    import payme_subscribe as payme_module
    import app as app_module
    app_module.app.config["TESTING"] = True
    import scheduler as scheduler_module
    return db_module, payme_module, app_module, scheduler_module


def _make_company(db_module, *, name, plan="start", paid_until=None, card_token=None, telegram_group_id=None, autopay=True):
    session = db_module.get_session()
    try:
        c = db_module.Company(name=name, is_active=True, plan=plan, paid_until=paid_until, telegram_group_id=telegram_group_id)
        c.payme_autopay_enabled = autopay
        session.add(c)
        session.commit()
        if card_token:
            c.set_payme_card_token(card_token)
            c.payme_card_masked = "860006******6311"
            session.commit()
        cid = c.id
        m = db_module.Manager(username=f"{name}_admin", full_name="A", role="admin", company_id=cid)
        m.set_password("parol123")
        session.add(m)
        session.commit()
        return cid, m.username
    finally:
        session.close()


def _login(client, username, password="parol123"):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=False)


def _fake_response(payload):
    resp = mock.Mock()
    resp.json.return_value = payload
    return resp


# ---------------------------------------------------------------------------
# 1) payme_subscribe.py -- past darajadagi JSON-RPC mijozi
# ---------------------------------------------------------------------------

def test_usd_to_tiyin_uses_configured_rate():
    _TMPDIR = tempfile.mkdtemp()
    _, payme_module, _, _ = _fresh_modules(
        os.path.join(_TMPDIR, "t1.db"), payme_merchant_id="M1", payme_test_key="K1", usd_to_uzs=12000,
    )
    assert payme_module.usd_to_tiyin(50) == 50 * 12000 * 100
    print("OK: usd_to_tiyin() PAYME_USD_TO_UZS_RATE'dan to'g'ri hisoblaydi")


def test_create_card_calls_correct_method_with_auth_header():
    _TMPDIR = tempfile.mkdtemp()
    _, payme_module, _, _ = _fresh_modules(
        os.path.join(_TMPDIR, "t2.db"), payme_merchant_id="MERCH1", payme_test_key="SECRETKEY1",
    )
    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append((url, json, headers))
        return _fake_response({"result": {"card": {"number": "860006******6311", "token": "TOK123", "verify": True, "recurrent": False}}})

    with mock.patch.object(payme_module.requests, "post", side_effect=fake_post):
        result = payme_module.create_card("8600123412341234", "1229")

    assert result == {"token": "TOK123", "masked": "860006******6311", "verify_needed": True, "recurrent": False}
    assert len(calls) == 1
    url, body, headers = calls[0]
    assert url == payme_module._TEST_URL
    assert body["method"] == "cards.create"
    assert body["params"] == {"card": {"number": "8600123412341234", "expire": "1229"}, "save": True}
    assert headers["X-Auth"] == "MERCH1:SECRETKEY1"
    print("OK: create_card() to'g'ri method/parametr/X-Auth sarlavhasi bilan cards.create chaqiradi")


def test_call_raises_payme_subscribe_error_on_json_rpc_error():
    _TMPDIR = tempfile.mkdtemp()
    _, payme_module, _, _ = _fresh_modules(
        os.path.join(_TMPDIR, "t3.db"), payme_merchant_id="M", payme_test_key="K",
    )

    def fake_post(*a, **kw):
        return _fake_response({"error": {"code": -31630, "message": {"uz": "Karta rad etildi"}}})

    with mock.patch.object(payme_module.requests, "post", side_effect=fake_post):
        try:
            payme_module.pay_receipt("receipt1", "TOK123")
            assert False, "PaymeSubscribeError kutilgan edi"
        except payme_module.PaymeSubscribeError as e:
            assert "Karta rad etildi" in str(e)
            assert e.code == -31630
    print("OK: Payme JSON-RPC xato qaytarsa PaymeSubscribeError (kod+xabar bilan) ko'taradi")


def test_not_configured_fails_fast_without_network_call():
    _TMPDIR = tempfile.mkdtemp()
    _, payme_module, _, _ = _fresh_modules(
        os.path.join(_TMPDIR, "t4.db"), payme_merchant_id="", payme_test_key="",
    )
    assert payme_module.is_configured() is False
    with mock.patch.object(payme_module.requests, "post", side_effect=AssertionError("tarmoqqa chiqmasligi kerak edi")):
        try:
            payme_module.create_card("8600123412341234", "1229")
            assert False, "PaymeSubscribeError kutilgan edi"
        except payme_module.PaymeSubscribeError as e:
            assert "ulanmagan" in str(e).lower()
    print("OK: PAYME_MERCHANT_ID/KEY yo'q bo'lsa darhol xato -- tarmoqqa chiqishga urinmaydi")


def test_verify_card_and_pay_receipt_success():
    _TMPDIR = tempfile.mkdtemp()
    _, payme_module, _, _ = _fresh_modules(
        os.path.join(_TMPDIR, "t5.db"), payme_merchant_id="M", payme_test_key="K",
    )
    with mock.patch.object(payme_module.requests, "post", side_effect=lambda *a, **kw: _fake_response(
        {"result": {"card": {"number": "860006******6311", "token": "TOK-FULL", "recurrent": True}}}
    )):
        verified = payme_module.verify_card("TOK-PENDING", "666666")
    assert verified == {"token": "TOK-FULL", "masked": "860006******6311", "recurrent": True}

    with mock.patch.object(payme_module.requests, "post", side_effect=lambda *a, **kw: _fake_response(
        {"result": {"receipt": {"_id": "R1", "state": 4, "pay_time": 1234567890}}}
    )):
        paid = payme_module.pay_receipt("R1", "TOK-FULL")
    assert paid == {"id": "R1", "state": 4, "pay_time": 1234567890}
    print("OK: verify_card()/pay_receipt() muvaffaqiyatli javobni to'g'ri o'qiydi")


# ---------------------------------------------------------------------------
# 2) /tolov/karta/* route'lari
# ---------------------------------------------------------------------------

def test_bind_card_needing_sms_stores_pending_token_and_sends_code():
    _TMPDIR = tempfile.mkdtemp()
    db_module, payme_module, app_module, _ = _fresh_modules(
        os.path.join(_TMPDIR, "t6.db"), payme_merchant_id="M", payme_test_key="K",
    )
    cid, username = _make_company(db_module, name="Karta A")

    with mock.patch.object(payme_module, "create_card", return_value={"token": "TOK-PEND-1", "masked": "8600******1111", "verify_needed": True, "recurrent": False}), \
         mock.patch.object(payme_module, "get_verify_code", return_value={"sent": True}) as m_verify_code:
        with app_module.app.test_client() as client:
            _login(client, username)
            r = client.post("/tolov/karta/boglash", data={"card_number": "8600 1234 1234 1111", "card_expire": "12/29"}, follow_redirects=True)
            assert r.status_code == 200
            html = r.get_data(as_text=True)
            assert "SMS" in html

    assert m_verify_code.call_args[0][0] == "TOK-PEND-1"
    session = db_module.get_session()
    try:
        with db_module.unscoped():
            c = session.query(db_module.Company).filter_by(id=cid).first()
        assert c.get_payme_card_pending_token() == "TOK-PEND-1"
        assert c.payme_card_token is None, "SMS tasdiqlanmagunicha token DOIMIY sifatida saqlanmasligi kerak"
    finally:
        session.close()
    print("OK: SMS tasdiqlash talab qilinganda token 'pending' sifatida saqlanadi va kod yuboriladi")


def test_verify_code_completes_binding():
    _TMPDIR = tempfile.mkdtemp()
    db_module, payme_module, app_module, _ = _fresh_modules(
        os.path.join(_TMPDIR, "t7.db"), payme_merchant_id="M", payme_test_key="K",
    )
    cid, username = _make_company(db_module, name="Karta B")

    session = db_module.get_session()
    try:
        c = session.get(db_module.Company, cid)
        c.set_payme_card_pending_token("TOK-PEND-2")
        session.commit()
    finally:
        session.close()

    with mock.patch.object(payme_module, "verify_card", return_value={"token": "TOK-FULL-2", "masked": "8600******2222", "recurrent": True}):
        with app_module.app.test_client() as client:
            _login(client, username)
            r = client.post("/tolov/karta/tasdiqlash", data={"code": "666666"}, follow_redirects=True)
            assert r.status_code == 200
            html = r.get_data(as_text=True)
            assert "8600******2222" in html

    session = db_module.get_session()
    try:
        with db_module.unscoped():
            c = session.query(db_module.Company).filter_by(id=cid).first()
        assert c.get_payme_card_token() == "TOK-FULL-2"
        assert c.payme_card_masked == "8600******2222"
        assert c.get_payme_card_pending_token() is None
    finally:
        session.close()
    print("OK: to'g'ri SMS kod bilan karta bog'lanishi yakunlanadi (token + maskalangan raqam saqlanadi)")


def test_remove_card_clears_fields_and_disables_autopay_display():
    _TMPDIR = tempfile.mkdtemp()
    db_module, payme_module, app_module, _ = _fresh_modules(
        os.path.join(_TMPDIR, "t8.db"), payme_merchant_id="M", payme_test_key="K",
    )
    cid, username = _make_company(db_module, name="Karta C", card_token="TOK-EXISTING")

    with mock.patch.object(payme_module, "remove_card", return_value={"success": True}) as m_remove:
        with app_module.app.test_client() as client:
            _login(client, username)
            r = client.post("/tolov/karta/ochirish", data={}, follow_redirects=True)
            assert r.status_code == 200
    assert m_remove.call_args[0][0] == "TOK-EXISTING"

    session = db_module.get_session()
    try:
        with db_module.unscoped():
            c = session.query(db_module.Company).filter_by(id=cid).first()
        assert c.payme_card_token is None and c.payme_card_masked is None
    finally:
        session.close()
    print("OK: kartani o'chirish Payme'dan token'ni o'chiradi va bazadagi maydonlarni tozalaydi")


def test_autopay_toggle_persists_flag_and_is_isolated_per_company():
    _TMPDIR = tempfile.mkdtemp()
    db_module, payme_module, app_module, _ = _fresh_modules(
        os.path.join(_TMPDIR, "t9.db"), payme_merchant_id="M", payme_test_key="K",
    )
    cid_a, user_a = _make_company(db_module, name="Toggle A", card_token="TOK-A")
    cid_b, user_b = _make_company(db_module, name="Toggle B", card_token="TOK-B")

    with app_module.app.test_client() as client:
        _login(client, user_a)
        client.post("/tolov/avtotolov", data={"enabled": "0"}, follow_redirects=True)

    session = db_module.get_session()
    try:
        with db_module.unscoped():
            a = session.query(db_module.Company).filter_by(id=cid_a).first()
            b = session.query(db_module.Company).filter_by(id=cid_b).first()
        assert a.payme_autopay_enabled is False, "A kompaniyasi o'chirilgan bo'lishi kerak"
        assert b.payme_autopay_enabled is True, "B kompaniyasi B O'ZGARMASLIGI kerak (multi-tenant izolyatsiya)"
    finally:
        session.close()
    print("OK: avtoto'lov yoqish/o'chirish FAQAT joriy kompaniyaga ta'sir qiladi, boshqasiga tegmaydi")


# ---------------------------------------------------------------------------
# 3) scheduler.job_payme_autopay()
# ---------------------------------------------------------------------------

def test_autopay_job_charges_expiring_company_and_extends_paid_until():
    _TMPDIR = tempfile.mkdtemp()
    db_module, payme_module, app_module, scheduler_module = _fresh_modules(
        os.path.join(_TMPDIR, "t10.db"), payme_merchant_id="M", payme_test_key="K",
    )
    paid_until = dt.datetime.utcnow() + dt.timedelta(hours=6)
    cid, _ = _make_company(db_module, name="Autopay Charge", plan="start", paid_until=paid_until, card_token="TOK-CHARGE", telegram_group_id="-100555")

    with mock.patch.object(payme_module, "create_receipt", return_value={"id": "R-CHARGE", "state": 1}) as m_create, \
         mock.patch.object(payme_module, "pay_receipt", return_value={"id": "R-CHARGE", "state": 4, "pay_time": 1700000000}) as m_pay, \
         mock.patch.object(scheduler_module, "_tg_send", return_value={"ok": True, "error": None}) as m_tg:
        results = scheduler_module.job_payme_autopay()

    assert results[cid] == "to'landi"
    assert m_create.call_count == 1 and m_pay.call_count == 1
    assert m_pay.call_args[0][1] == "TOK-CHARGE"
    assert m_tg.call_count == 1
    assert "muvaffaqiyatli" in m_tg.call_args[0][1]

    session = db_module.get_session()
    try:
        with db_module.unscoped():
            c = session.query(db_module.Company).filter_by(id=cid).first()
            receipt = session.query(db_module.PaymeReceipt).filter_by(company_id=cid).first()
        assert c.paid_until > paid_until + dt.timedelta(days=29)
        assert receipt.status == "paid"
        assert receipt.payme_receipt_id == "R-CHARGE"
    finally:
        session.close()
    print("OK: muddati tugayotgan kompaniya avtomatik to'lanadi, paid_until 30 kunga uzaytiriladi, Telegram'ga xabar boradi")


def test_autopay_job_does_not_double_charge_same_billing_period():
    _TMPDIR = tempfile.mkdtemp()
    db_module, payme_module, app_module, scheduler_module = _fresh_modules(
        os.path.join(_TMPDIR, "t11.db"), payme_merchant_id="M", payme_test_key="K",
    )
    paid_until = dt.datetime.utcnow() + dt.timedelta(hours=6)
    cid, _ = _make_company(db_module, name="No Double Charge", plan="start", paid_until=paid_until, card_token="TOK-ONCE")

    # Boshqa (masalan parallel/qayta-deploy) urinishdan qolgan "pending"
    # receipt ALLAQACHON shu davr uchun mavjud -- job YANGI so'rov
    # yubormasligi kerak.
    session = db_module.get_session()
    try:
        with db_module.unscoped():
            session.add(db_module.PaymeReceipt(
                company_id=cid, plan_key="start", amount_tiyin=1000000,
                billing_period_start=paid_until, status="pending",
            ))
            session.commit()
    finally:
        session.close()

    with mock.patch.object(payme_module, "create_receipt") as m_create, \
         mock.patch.object(payme_module, "pay_receipt") as m_pay:
        results = scheduler_module.job_payme_autopay()

    assert m_create.call_count == 0 and m_pay.call_count == 0, "bir xil davr uchun IKKINCHI marta so'rov yuborilmasligi kerak"
    assert "allaqachon urinilgan" in results[cid]
    print("OK: bir xil billing_period_start uchun ikkinchi marta pul so'ralmaydi (idempotentlik)")


def test_autopay_job_failed_charge_does_not_extend_and_allows_retry():
    _TMPDIR = tempfile.mkdtemp()
    db_module, payme_module, app_module, scheduler_module = _fresh_modules(
        os.path.join(_TMPDIR, "t12.db"), payme_merchant_id="M", payme_test_key="K",
    )
    paid_until = dt.datetime.utcnow() - dt.timedelta(hours=2)  # kecha tugagan, hali GRACE ichida
    cid, _ = _make_company(db_module, name="Autopay Fail", plan="start", paid_until=paid_until, card_token="TOK-FAIL", telegram_group_id="-100777")

    with mock.patch.object(payme_module, "create_receipt", return_value={"id": "R-FAIL", "state": 1}), \
         mock.patch.object(payme_module, "pay_receipt", side_effect=payme_module.PaymeSubscribeError("Mablag' yetarli emas", code=-31001)), \
         mock.patch.object(scheduler_module, "_tg_send", return_value={"ok": True, "error": None}) as m_tg:
        results = scheduler_module.job_payme_autopay()

    assert "to'lanmadi" in results[cid]
    assert m_tg.call_count == 1
    assert "AMALGA OSHMADI" in m_tg.call_args[0][1]

    session = db_module.get_session()
    try:
        with db_module.unscoped():
            c = session.query(db_module.Company).filter_by(id=cid).first()
            receipt = session.query(db_module.PaymeReceipt).filter_by(company_id=cid).first()
        assert c.paid_until == paid_until, "muvaffaqiyatsiz to'lov paid_until'ni O'ZGARTIRMASLIGI kerak"
        assert receipt.status == "failed"
        assert "Mablag'" in receipt.error_message
    finally:
        session.close()
    print("OK: rad etilgan to'lov paid_until'ni o'zgartirmaydi, xato Telegram'ga yuboriladi, keyingi kunga qayta urinish ochiq qoladi")


def test_autopay_job_skips_trial_and_no_card_and_default_company():
    _TMPDIR = tempfile.mkdtemp()
    db_module, payme_module, app_module, scheduler_module = _fresh_modules(
        os.path.join(_TMPDIR, "t13.db"), payme_merchant_id="M", payme_test_key="K",
    )
    soon = dt.datetime.utcnow() + dt.timedelta(hours=1)
    _make_company(db_module, name="Trial Co", plan="trial", paid_until=soon, card_token="TOK-TRIAL")
    _make_company(db_module, name="No Card Co", plan="start", paid_until=soon, card_token=None)
    # Company #1 (default/platforma egasi) -- ensure_default_company() allaqachon yaratgan, unga tegilmaydi.

    with mock.patch.object(payme_module, "create_receipt") as m_create, \
         mock.patch.object(payme_module, "pay_receipt") as m_pay:
        results = scheduler_module.job_payme_autopay()

    assert m_create.call_count == 0 and m_pay.call_count == 0
    assert results == {}, f"hech kim to'lanmasligi kerak edi, lekin: {results}"
    print("OK: sinov tarifidagi, kartasiz va platforma egasining o'z kompaniyasi avtoto'lovga UMUMAN kirmaydi")


def test_autopay_job_noop_when_payme_not_configured():
    _TMPDIR = tempfile.mkdtemp()
    db_module, payme_module, app_module, scheduler_module = _fresh_modules(
        os.path.join(_TMPDIR, "t14.db"), payme_merchant_id="", payme_test_key="",
    )
    soon = dt.datetime.utcnow() + dt.timedelta(hours=1)
    cid, _ = _make_company(db_module, name="Unconfigured Co", plan="start", paid_until=soon, card_token="TOK-X")

    with mock.patch.object(payme_module.requests, "post", side_effect=AssertionError("tarmoqqa chiqmasligi kerak edi")):
        results = scheduler_module.job_payme_autopay()

    session = db_module.get_session()
    try:
        with db_module.unscoped():
            count = session.query(db_module.PaymeReceipt).filter_by(company_id=cid).count()
        assert count == 0
    finally:
        session.close()
    assert "sozlanmagan" in list(results.values())[0]
    print("OK: Payme hali sozlanmagan bo'lsa job hech narsaga tegmasdan darhol chiqadi (xavfsiz standart holat)")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
