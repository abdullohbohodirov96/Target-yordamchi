"""test_marketplace_webhooks_offline.py — 2026-09, foydalanuvchi so'rovi:
"pasidan side br tegida marketplace qoshishimiz kerak va ulidigan app
service lani ushattan ulidigan qilishmiz kerak misol FB IG, crm lar
harhil va boshqalar toli tg bot guruh ulash ushan... qara botta hamma
crmlarni ulash mumkin bolsin kop ontegratsiyalarni qilish mumkin bolsin
aniq ishlasin".

Har bir CRM uchun alohida (OAuth) integratsiya real hisob/API kalitlarisiz
tekshirib bo'lmaydi -- shuning uchun UNIVERSAL WEBHOOK yondashuvi
tanlandi (`integrations.py`, `/marketplace`, `/api/webhook/leads/<token>`).
Bu fayl shu mexanizmning ikkala tomonini ("kiruvchi" va "chiquvchi") HAM
to'g'ri ishlashini, HAM ko'p-kompaniyali (multi-tenant) izolyatsiyasini
tekshiradi:

  1. Kiruvchi: tashqi tizim kompaniyaning shaxsiy token'i bilan POST
     qilsa, to'g'ri kompaniyaga Lead sifatida tushadi; noto'g'ri token,
     bo'sh payload rad etiladi; ikkita kompaniya bir-birining lead'ini
     ko'rmaydi/olmaydi.
  2. Chiquvchi: yangi lead kompaniyaning o'z webhook URL'iga yuboriladi
     (muvaffaqiyatli/xato holatlar), birinchi marta sozlanganda ESKI
     lead'lar "backlog" sifatida o'tkazib yuboriladi (faqat keyingi
     yangilari yuboriladi), va ikkita kompaniyaning webhook'lari
     bir-biriga aralashmaydi.

Ishga tushirish:
    cd app && python3 scripts/test_marketplace_webhooks_offline.py
"""

import os
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("OPENAI_API_KEY", "test-dummy-openai-key")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ.setdefault("META_APP_ID", "test_app_id")
os.environ.setdefault("META_APP_SECRET", "test_app_secret")


def _fresh_modules(db_path):
    for name in (
        "db", "kv_store", "app", "orchestrator", "kpi_bonus", "call_analytics",
        "scheduler", "lead_sync", "meta_events", "meta_api", "monthly_report",
        "ig_dm_sync", "ig_dm_analysis", "dashboard_data", "budget_tracker",
        "integrations",
    ):
        sys.modules.pop(name, None)
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"
    os.environ.pop("TELEGRAM_AGENTS_GROUP_ID", None)
    os.environ.pop("TELEGRAM_REPORT_GROUP_ID", None)
    import db as db_module
    db_module.init_db()
    import app as app_module
    app_module.app.config["TESTING"] = True
    return db_module, app_module


def _make_company(db_module, *, name):
    session = db_module.get_session()
    try:
        c = db_module.Company(name=name, source="admin_created", plan="business", is_active=True)
        session.add(c)
        session.commit()
        return c.id
    finally:
        session.close()


def _login_admin(app_module, db_module, company_id, username):
    session = db_module.get_session()
    try:
        m = db_module.Manager(username=username, full_name="Admin", role="admin", company_id=company_id)
        m.set_password("parol123")
        session.add(m)
        session.commit()
    finally:
        session.close()
    client = app_module.app.test_client()
    client.post("/login", data={"username": username, "password": "parol123"}, follow_redirects=False)
    return client


# ---------------------------------------------------------------------------
# 1) Kiruvchi webhook -- /api/webhook/leads/<token>
# ---------------------------------------------------------------------------

def test_inbound_webhook_creates_lead_in_right_company():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_modules(os.path.join(tmp, "in1.db"))
        company_a = _make_company(db_module, name="Kompaniya A")
        import integrations

        session = db_module.get_session()
        try:
            c = session.get(db_module.Company, company_a)
            token = integrations.ensure_inbound_token(c)
            session.commit()
        finally:
            session.close()

        client = app_module.app.test_client()
        r = client.post(
            f"/api/webhook/leads/{token}",
            json={"name": "Alisher", "phone_number": "+998901234567", "note": "saytdan"},
        )
        assert r.status_code == 200, r.get_data(as_text=True)
        data = r.get_json()
        assert data["ok"] is True

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                lead = session.get(db_module.Lead, data["lead_id"])
            assert lead.company_id == company_a
            assert lead.full_name == "Alisher"
            assert lead.phone == "+998901234567"
            assert lead.source == "webhook"
        finally:
            session.close()
    print("OK: kiruvchi webhook (/api/webhook/leads/<token>) to'g'ri kompaniyaga Lead yaratadi, turli maydon nomlarini (name/phone_number) tanib oladi")


def test_inbound_webhook_rejects_bad_token_and_empty_payload():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_modules(os.path.join(tmp, "in2.db"))
        company_a = _make_company(db_module, name="Kompaniya A")
        import integrations
        session = db_module.get_session()
        try:
            c = session.get(db_module.Company, company_a)
            token = integrations.ensure_inbound_token(c)
            session.commit()
        finally:
            session.close()

        client = app_module.app.test_client()
        r_bad = client.post("/api/webhook/leads/mutlaqo-notogri-token", json={"name": "X", "phone": "123"})
        assert r_bad.status_code == 404

        r_empty = client.post(f"/api/webhook/leads/{token}", json={"note": "faqat izoh, ism/telefon yo'q"})
        assert r_empty.status_code == 400
    print("OK: kiruvchi webhook noto'g'ri token (404) va ism/telefonsiz payload'ni (400) rad etadi")


def test_inbound_webhook_cross_company_isolation():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_modules(os.path.join(tmp, "in3.db"))
        company_a = _make_company(db_module, name="Kompaniya A")
        company_b = _make_company(db_module, name="Kompaniya B")
        import integrations
        session = db_module.get_session()
        try:
            ca = session.get(db_module.Company, company_a)
            cb = session.get(db_module.Company, company_b)
            token_a = integrations.ensure_inbound_token(ca)
            token_b = integrations.ensure_inbound_token(cb)
            assert token_a != token_b
            session.commit()
        finally:
            session.close()

        client = app_module.app.test_client()
        client.post(f"/api/webhook/leads/{token_a}", json={"full_name": "A dan", "phone": "111"})
        client.post(f"/api/webhook/leads/{token_b}", json={"full_name": "B dan", "phone": "222"})

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                leads_a = session.query(db_module.Lead).filter_by(company_id=company_a).all()
                leads_b = session.query(db_module.Lead).filter_by(company_id=company_b).all()
            assert len(leads_a) == 1 and leads_a[0].full_name == "A dan"
            assert len(leads_b) == 1 and leads_b[0].full_name == "B dan"
        finally:
            session.close()
    print("OK: ikkita kompaniyaning kiruvchi webhook manzillari bir-biridan mustaqil (lead aralashmaydi)")


# ---------------------------------------------------------------------------
# 2) Chiquvchi webhook -- integrations.dispatch_pending_webhooks
# ---------------------------------------------------------------------------

def test_outgoing_webhook_delivers_new_lead_and_records_status():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_modules(os.path.join(tmp, "out1.db"))
        company_a = _make_company(db_module, name="Kompaniya A")
        import integrations

        session = db_module.get_session()
        try:
            c = session.get(db_module.Company, company_a)
            c.webhook_out_url = "https://example.com/hook-a"
            c.webhook_out_secret = "shh"
            session.commit()
            lead = db_module.Lead(company_id=company_a, full_name="Yangi Mijoz", phone="333", source="manual", status="new")
            session.add(lead)
            session.commit()
            lead_id = lead.id
        finally:
            session.close()

        fake_resp = mock.Mock(status_code=200, text="ok")
        with mock.patch("integrations.requests.post", return_value=fake_resp) as m_post:
            session = db_module.get_session()
            try:
                result = integrations.dispatch_pending_webhooks(session)
            finally:
                session.close()
        assert result["sent"] == 1 and result["failed"] == 0
        assert m_post.call_args.kwargs["json"]["phone"] == "333"
        assert m_post.call_args.kwargs["headers"]["X-Replix-Secret"] == "shh"

        session = db_module.get_session()
        try:
            lead = session.get(db_module.Lead, lead_id)
            c = session.get(db_module.Company, company_a)
            assert lead.webhook_delivered_at is not None
            assert c.webhook_out_last_status == "ok"
        finally:
            session.close()
    print("OK: chiquvchi webhook yangi lead'ni yuboradi, maxfiy kalitni sarlavhada jo'natadi, muvaffaqiyat holatini yozadi")


def test_outgoing_webhook_records_failure_without_crashing():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_modules(os.path.join(tmp, "out2.db"))
        company_a = _make_company(db_module, name="Kompaniya A")
        import integrations

        session = db_module.get_session()
        try:
            c = session.get(db_module.Company, company_a)
            c.webhook_out_url = "https://example.com/hook-fail"
            session.commit()
            lead = db_module.Lead(company_id=company_a, full_name="Muvaffaqiyatsiz", phone="444", source="manual", status="new")
            session.add(lead)
            session.commit()
            lead_id = lead.id
        finally:
            session.close()

        fake_resp = mock.Mock(status_code=500, text="server error")
        with mock.patch("integrations.requests.post", return_value=fake_resp):
            session = db_module.get_session()
            try:
                result = integrations.dispatch_pending_webhooks(session)
            finally:
                session.close()
        assert result["sent"] == 0 and result["failed"] == 1

        session = db_module.get_session()
        try:
            lead = session.get(db_module.Lead, lead_id)
            c = session.get(db_module.Company, company_a)
            assert lead.webhook_delivered_at is None
            assert lead.webhook_delivery_error and "500" in lead.webhook_delivery_error
            assert c.webhook_out_last_status == "error"
        finally:
            session.close()
    print("OK: chiquvchi webhook xato holatida qulamaydi, xatoni lead/kompaniyada yozib qo'yadi (qayta avtomatik urinilmaydi)")


def test_outgoing_webhook_skips_old_backlog_when_first_configured():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_modules(os.path.join(tmp, "out3.db"))
        company_a = _make_company(db_module, name="Kompaniya A")
        client = _login_admin(app_module, db_module, company_a, "mp_admin1")

        session = db_module.get_session()
        try:
            old_lead = db_module.Lead(company_id=company_a, full_name="Eski lead", phone="555", source="manual", status="new")
            session.add(old_lead)
            session.commit()
            old_lead_id = old_lead.id
        finally:
            session.close()

        # Admin Marketplace'dan BIRINCHI marta webhook URL'ni sozlaydi.
        r = client.post("/marketplace", data={"action": "set_outgoing", "webhook_out_url": "https://example.com/hook-b"}, follow_redirects=True)
        assert r.status_code == 200

        session = db_module.get_session()
        try:
            old_lead = session.get(db_module.Lead, old_lead_id)
            assert old_lead.webhook_delivered_at is not None, "URL BIRINCHI marta sozlanganda ESKI lead 'allaqachon yuborilgan' deb belgilanishi kerak (backlog o'tkazib yuboriladi)"

            new_lead = db_module.Lead(company_id=company_a, full_name="Yangi lead", phone="666", source="manual", status="new")
            session.add(new_lead)
            session.commit()
            new_lead_id = new_lead.id
        finally:
            session.close()

        import integrations
        fake_resp = mock.Mock(status_code=200, text="ok")
        with mock.patch("integrations.requests.post", return_value=fake_resp) as m_post:
            session = db_module.get_session()
            try:
                result = integrations.dispatch_pending_webhooks(session)
            finally:
                session.close()
        assert result["sent"] == 1, "faqat URL sozlangandan KEYIN yaratilgan lead yuborilishi kerak"
        assert m_post.call_args.kwargs["json"]["full_name"] == "Yangi lead"

        session = db_module.get_session()
        try:
            new_lead = session.get(db_module.Lead, new_lead_id)
            assert new_lead.webhook_delivered_at is not None
        finally:
            session.close()
    print("OK: webhook URL birinchi marta sozlanganda ESKI lead'lar bir zumda yuborilib ketmaydi, faqat KEYINGI yangi lead'lar yuboriladi")


def test_outgoing_webhooks_are_isolated_per_company():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_modules(os.path.join(tmp, "out4.db"))
        company_a = _make_company(db_module, name="Kompaniya A")
        company_b = _make_company(db_module, name="Kompaniya B")
        import integrations

        session = db_module.get_session()
        try:
            ca = session.get(db_module.Company, company_a)
            cb = session.get(db_module.Company, company_b)
            ca.webhook_out_url = "https://example.com/hook-a"
            cb.webhook_out_url = "https://example.com/hook-b"
            session.add(db_module.Lead(company_id=company_a, full_name="A lead", phone="1", source="manual", status="new"))
            session.add(db_module.Lead(company_id=company_b, full_name="B lead", phone="2", source="manual", status="new"))
            session.commit()
        finally:
            session.close()

        fake_resp = mock.Mock(status_code=200, text="ok")
        calls = []

        def fake_post(url, json=None, headers=None, timeout=None):
            calls.append((url, json.get("full_name")))
            return fake_resp

        with mock.patch("integrations.requests.post", side_effect=fake_post):
            session = db_module.get_session()
            try:
                result = integrations.dispatch_pending_webhooks(session)
            finally:
                session.close()

        assert result["companies"] == 2 and result["sent"] == 2
        sent_map = dict(calls)
        assert sent_map["https://example.com/hook-a"] == "A lead"
        assert sent_map["https://example.com/hook-b"] == "B lead"
    print("OK: ikkita kompaniyaning chiquvchi webhook'lari bir-biriga aralashmaydi (har biri O'Z URL'iga, O'Z lead'ini yuboradi)")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
