"""test_ai_assistant_and_landing_admin_offline.py — 2026-09, foydalanuvchi
so'rovi: "AI ga savol bersayam jovob beromasa ai manga habar bersin ...
hamasini odmalar nimani soravtkaniyam korinsin". TARMOQSIZ (offline)
tekshiradi:

  1. Web AI-yordamchisi ([[UNANSWERED]] belgisi bilan) javob topa
     olmaganda -- `AssistantUnanswered`ga yozadi VA platforma egasining
     standart Telegram nishoniga DARHOL xabar yuboradi (ilgari faqat
     "Sozlamalar" sahifasidagi ro'yxatda passiv ko'rinardi).
  2. `/companies/murojaatlar` -- replix.uz landing formasi orqali kelgan
     murojaatlarni ko'rsatuvchi yangi sahifa -- FAQAT platforma egasiga
     ochiq, boshqa (mijoz-kompaniya) admin uchun EMAS.

Ishga tushirish:
    cd app && python3 scripts/test_ai_assistant_and_landing_admin_offline.py
"""

import os
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")


def _fresh_modules(db_path):
    for name in ("db", "kv_store", "app", "budget_tracker", "orchestrator", "meta_api", "scheduler", "lead_sync", "meta_events", "monthly_report", "ig_dm_sync", "dashboard_data"):
        sys.modules.pop(name, None)
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ.pop("TELEGRAM_AGENTS_GROUP_ID", None)
    os.environ.pop("TELEGRAM_REPORT_GROUP_ID", None)
    import db as db_module
    db_module.init_db()
    import app as app_module
    app_module.app.config["TESTING"] = True
    app_module.app.config["WTF_CSRF_ENABLED"] = False  # 2026-09, CSRF endi majburiy -- testlarda so'rovlar session-tashqarisida yasaladi
    return db_module, app_module


def _signup(client, *, company_name, admin_username, plan="business"):
    return client.post("/signup", data={
        "company_name": company_name, "admin_username": admin_username,
        "admin_full_name": "", "email": "", "plan": plan,
        "password": "parol123456", "password2": "parol123456",
    }, follow_redirects=True)


def test_unanswered_web_question_notifies_owner_telegram_immediately():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_modules(os.path.join(tmp, "s1.db"))
        os.environ["TELEGRAM_AGENTS_GROUP_ID"] = "-100777111"
        client = app_module.app.test_client()
        _signup(client, company_name="Savol MChJ", admin_username="savol_admin")

        sent = []

        def _fake_tracked(cid, text):
            sent.append((cid, text))
            return {"ok": True, "message_id": 111, "error": None}

        # 2026-09, "javob-relesi" ishi: BIRINCHI (va bu testda yagona)
        # nishonga endi `tg_send_tracked()` orqali yuboriladi (`tg_send`
        # EMAS) -- shu orqali `notify_message_id` qatorga saqlanadi.
        with mock.patch.object(app_module.orchestrator, "classify_intent", return_value=("LIGHT", "")), \
             mock.patch.object(app_module.orchestrator, "execute_intent", return_value=None), \
             mock.patch.object(app_module.orchestrator, "call_light_chat", return_value="Menda bu bo'yicha aniq ma'lumot yo'q. [[UNANSWERED]]"), \
             mock.patch.object(app_module, "tg_send_tracked", side_effect=_fake_tracked):
            r = client.post("/api/assistant", json={"message": "Yer yuzida nechta chumoli bor?"})
            assert r.status_code == 200
            body = r.get_json()
            assert "[[UNANSWERED]]" not in body["reply"], "Foydalanuvchiga ko'rinadigan javobda yashirin belgi QOLMASLIGI kerak"

        assert len(sent) == 1, f"Aynan bitta Telegram xabari yuborilishi kerak edi: {sent!r}"
        assert sent[0][0] == -100777111
        assert "Savol MChJ" in sent[0][1]
        assert "Yer yuzida nechta chumoli bor?" in sent[0][1]
        assert "savol_admin" in sent[0][1] or "AI-yordamchi" in sent[0][1]

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                row = session.query(db_module.AssistantUnanswered).first()
            assert row is not None and "chumoli" in row.question
            assert row.origin == "web"
            assert row.notify_chat_id == "-100777111"
            assert row.notify_message_id == 111
        finally:
            session.close()
    print("OK: AI-yordamchi javob topolmaganda, savol AssistantUnanswered'ga yoziladi VA platforma egasiga DARHOL Telegram xabari boradi")


def test_answered_web_question_does_not_notify():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_modules(os.path.join(tmp, "s2.db"))
        os.environ["TELEGRAM_AGENTS_GROUP_ID"] = "-100777222"
        client = app_module.app.test_client()
        _signup(client, company_name="Javob MChJ", admin_username="javob_admin")

        sent = []
        with mock.patch.object(app_module.orchestrator, "classify_intent", return_value=("LIGHT", "")), \
             mock.patch.object(app_module.orchestrator, "execute_intent", return_value=None), \
             mock.patch.object(app_module.orchestrator, "call_light_chat", return_value="Bugun 12 ta lead keldi."), \
             mock.patch.object(app_module, "tg_send", side_effect=lambda cid, text: sent.append((cid, text))):
            client.post("/api/assistant", json={"message": "Bugun nechta lead keldi?"})

        assert sent == [], "Oddiy (javob topilgan) savol uchun Telegram xabari YUBORILMASLIGI kerak"

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                count = session.query(db_module.AssistantUnanswered).count()
            assert count == 0
        finally:
            session.close()
    print("OK: AI-yordamchi javob TOPGANDA hech qanday Telegram xabari yuborilmaydi, AssistantUnanswered'ga yozilmaydi")


def test_landing_contact_submissions_page_owner_only():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_modules(os.path.join(tmp, "s3.db"))
        client = app_module.app.test_client()

        # Platforma egasi -- Company #1 (default kompaniya, `db.init_db()`
        # avtomatik yaratadi, lekin unga admin qo'shmaydi) uchun admin
        # hisobini qo'lda yaratamiz.
        session = db_module.get_session()
        try:
            owner_username = "platform_owner_test"
            m = db_module.Manager(company_id=1, username=owner_username, role="admin", full_name="Owner")
            m.set_password("parol123456")
            session.add(m)
            session.commit()
        finally:
            session.close()
        client.post("/login", data={"username": owner_username, "password": "parol123456"}, follow_redirects=True)

        app_module.kv_store.set_json("landing_contact_submissions", [
            {"name": "Sayt Mijozi", "phone": "+998901112233", "message": "Narxi qancha?", "created_at": "2026-09-10T08:00:00"},
        ])

        r = client.get("/companies/murojaatlar")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "Sayt Mijozi" in html
        assert "+998901112233" in html
    print("OK: '/companies/murojaatlar' platforma egasiga sayt murojaatlarini to'g'ri ko'rsatadi")


def test_landing_contact_submissions_page_rejects_regular_company_admin():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_modules(os.path.join(tmp, "s4.db"))
        client = app_module.app.test_client()
        _signup(client, company_name="Oddiy Mijoz MChJ", admin_username="oddiy_admin")

        r = client.get("/companies/murojaatlar", follow_redirects=True)
        assert r.status_code == 200
        assert "faqat platforma egasi uchun" in r.get_data(as_text=True)
    print("OK: '/companies/murojaatlar' oddiy mijoz-kompaniya admin'i uchun rad etiladi (faqat platforma egasi)")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
