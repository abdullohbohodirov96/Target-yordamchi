"""test_ai_assistant_reply_relay_offline.py — 2026-09, "superadmin AI-
suhbatlar paneli + javob-relesi" so'rovi: TARMOQSIZ (offline) tekshiradi:

  1. `AssistantUnanswered` qatori WEB yo'lida (`_log_unanswered_question`)
     `origin="web"` va `notify_chat_id`/`notify_message_id` bilan, TELEGRAM
     yo'lida (`_handle_company_free_text`) `origin="telegram"`, `chat_id`
     va SHU YERDA HAM `notify_chat_id`/`notify_message_id` bilan to'g'ri
     yaratilishini (ikkalasida ham platforma egasiga Telegram ogohlantirish
     ketishini).
  2. Webhook'dagi javob-relesi (`_try_handle_assistant_reply`) -- platforma
     egasi ogohlantirish xabariga REPLY qilsa:
       - `notify_message_id` VA `notify_chat_id` IKKALASI HAM mos kelgandagina
         "javob" deb hisoblanishi (faqat message_id -- boshqa chatdan kelgan
         xabar bilan chalkashmasligi -- XAVFSIZLIK/TO'G'RILIK chegarasi);
       - ALLAQACHON javob berilgan qator qayta moslashtirilmasligi;
       - WEB kelib chiqishi -- `web_chat_history:<manager_id>`ga to'g'ri
         yoziladi, BOSHQA menejerning tarixi tegilmasligi;
       - TELEGRAM kelib chiqishi -- ASL `chat_id`ga javob yuborilishi;
       - hech narsa mos kelmasa -- webhook ODATDAGI dispetcherga (mention/
         guruh/erkin-matn) o'zgarishsiz o'tishi.
  3. `/companies/ai-suhbatlar` (superadmin panel) -- FAQAT platforma
     egasiga ochiqligi.

Ishga tushirish:
    cd app && python3 scripts/test_ai_assistant_reply_relay_offline.py
"""

import os
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ.setdefault("META_APP_ID", "test_app_id")
os.environ.setdefault("META_APP_SECRET", "test_app_secret")


def _fresh_app(db_path, *, owner_group=-100777, owner_group_2=None):
    """Har bir test o'z ALOHIDA SQLite bazasi va `app` modulining TOZA
    nusxasi bilan ishlaydi (boshqa `*_offline.py` fayllardagi bilan bir
    xil naqsh)."""
    for name in (
        "db", "kv_store", "app", "budget_tracker", "orchestrator", "meta_api",
        "scheduler", "lead_sync", "meta_events", "monthly_report", "ig_dm_sync",
        "ig_dm_analysis", "dashboard_data",
    ):
        sys.modules.pop(name, None)
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"
    os.environ["TELEGRAM_AGENTS_GROUP_ID"] = str(owner_group)
    if owner_group_2 is not None:
        os.environ["TELEGRAM_REPORT_GROUP_ID"] = str(owner_group_2)
    else:
        os.environ.pop("TELEGRAM_REPORT_GROUP_ID", None)
    import db as db_module
    db_module.init_db()
    import app as app_module
    app_module.app.config["TESTING"] = True
    app_module.app.config["WTF_CSRF_ENABLED"] = False
    app_module._BOT_IDENTITY_CACHE.clear()
    return db_module, app_module


def _signup(client, *, company_name, admin_username, plan="business"):
    return client.post("/signup", data={
        "company_name": company_name, "admin_username": admin_username,
        "admin_full_name": "", "email": "", "plan": plan,
        "password": "parol123456", "password2": "parol123456",
    }, follow_redirects=True)


def _login(client, username, password="parol123456"):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=True)


# ---------------------------------------------------------------------------
# 1) AssistantUnanswered qatorining maydonlari -- WEB va TELEGRAM yo'llari
# ---------------------------------------------------------------------------

def test_web_unanswered_row_has_origin_and_tracked_notify_fields():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_app(os.path.join(tmp, "s1.db"))
        client = app_module.app.test_client()
        _signup(client, company_name="Web Savol MChJ", admin_username="web_savol_admin")

        with mock.patch.object(app_module.orchestrator, "classify_intent", return_value=("LIGHT", "")), \
             mock.patch.object(app_module.orchestrator, "execute_intent", return_value=None), \
             mock.patch.object(app_module.orchestrator, "call_light_chat", return_value="Bilmayman. [[UNANSWERED]]"), \
             mock.patch.object(app_module, "tg_send_tracked", return_value={"ok": True, "message_id": 4242, "error": None}) as mock_tracked:
            r = client.post("/api/assistant", json={"message": "Yer yuzida nechta chumoli bor?"})
            assert r.status_code == 200
        assert mock_tracked.call_args[0][0] == -100777

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                row = session.query(db_module.AssistantUnanswered).first()
            assert row is not None
            assert row.origin == "web"
            assert row.chat_id is None
            assert row.notify_chat_id == "-100777"
            assert row.notify_message_id == 4242
            assert row.manager_id is not None
            assert row.answered_at is None
        finally:
            session.close()
    print("OK: web-yo'lidagi javobsiz savol qatori origin='web' + notify_chat_id/notify_message_id bilan to'g'ri yaratiladi")


def test_telegram_unanswered_row_sends_alert_and_has_fields():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_app(os.path.join(tmp, "s2.db"))
        session = db_module.get_session()
        try:
            company = db_module.Company(name="Telegram Savol MChJ", is_active=True, plan="business", telegram_group_id="-300111")
            session.add(company)
            session.commit()
            company_id = company.id
        finally:
            session.close()

        with mock.patch.object(app_module.orchestrator, "call_light_chat", return_value="Bilmayman. [[UNANSWERED]]"), \
             mock.patch.object(app_module, "tg_send_tracked", return_value={"ok": True, "message_id": 5151, "error": None}) as mock_tracked, \
             mock.patch.object(app_module, "tg_send") as mock_send:
            session = db_module.get_session()
            try:
                with db_module.unscoped():
                    company_row = session.get(db_module.Company, company_id)
                app_module._handle_company_free_text(-300111, company_row, "Nima uchun narx qimmat?")
            finally:
                session.close()

        # Foydalanuvchining o'ziga oddiy javob (tg_send) HAM, platforma
        # egasiga ogohlantirish (tg_send_tracked) HAM ketishi kerak --
        # ILGARI (bu ish boshlanishidan oldin) Telegram yo'lida HECH QANDAY
        # ogohlantirish yuborilmasdi (faqat bazaga jim yozilardi).
        assert mock_send.call_args_list[0][0][0] == -300111
        assert mock_tracked.call_args[0][0] == -100777

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                row = session.query(db_module.AssistantUnanswered).first()
            assert row is not None
            assert row.origin == "telegram"
            assert row.chat_id == "-300111"
            assert row.notify_chat_id == "-100777"
            assert row.notify_message_id == 5151
        finally:
            session.close()
    print("OK: Telegram-yo'lidagi javobsiz savol ENDI platforma egasiga ogohlantirish yuboradi VA origin/chat_id/notify_* maydonlarini to'g'ri yozadi")


# ---------------------------------------------------------------------------
# 2) Javob-relesi -- webhook orqali
# ---------------------------------------------------------------------------

def _make_pending_row(db_module, **kwargs):
    session = db_module.get_session()
    try:
        row = db_module.AssistantUnanswered(
            question=kwargs.pop("question", "Test savol"),
            **kwargs,
        )
        session.add(row)
        session.commit()
        return row.id
    finally:
        session.close()


def test_reply_requires_both_message_id_and_chat_id_match():
    """Ikkinchi (haqiqiy) platforma egasi guruhidan xuddi shu raqamli
    xabarga REPLY qilinsa ham -- notify_chat_id mos kelmagani uchun bu
    javob deb HISOBLANMASLIGI, oddiy erkin-matn sifatida ishlanishi kerak."""
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_app(os.path.join(tmp, "s3.db"), owner_group=-100001, owner_group_2=-100002)
        row_id = _make_pending_row(
            db_module, origin="web", manager_id=None,
            notify_chat_id="-100001", notify_message_id=555,
        )
        client = app_module.app.test_client()

        with mock.patch.object(app_module, "handle_free_text") as mock_ht:
            r = client.post("/api/webhook", json={
                "message": {
                    "chat": {"id": -100002, "type": "private"},
                    "text": "Bu javob emas, oddiy xabar",
                    "reply_to_message": {"message_id": 555},
                    "from": {"id": 9, "first_name": "BoshqaOdam"},
                }
            })
        assert r.status_code == 200
        mock_ht.assert_called_once_with(-100002, "Bu javob emas, oddiy xabar")

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                row = session.get(db_module.AssistantUnanswered, row_id)
            assert row.answered_at is None, "notify_chat_id mos kelmasa qator HALI HAM javobsiz qolishi kerak"
        finally:
            session.close()
    print("OK: message_id mos kelsa ham notify_chat_id mos kelmasa -- javob deb hisoblanmaydi, oddiy dispetcherga o'tadi")


def test_reply_from_non_owner_chat_falls_through():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_app(os.path.join(tmp, "s4.db"), owner_group=-100003)
        row_id = _make_pending_row(
            db_module, origin="web", manager_id=None,
            notify_chat_id="-100003", notify_message_id=777,
        )
        client = app_module.app.test_client()

        with mock.patch.object(app_module, "handle_free_text") as mock_ht:
            r = client.post("/api/webhook", json={
                "message": {
                    "chat": {"id": -999999, "type": "private"},
                    "text": "Men bu qatorga aloqasi yo'q odamman",
                    "reply_to_message": {"message_id": 777},
                    "from": {"id": 42},
                }
            })
        assert r.status_code == 200
        mock_ht.assert_called_once()

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                row = session.get(db_module.AssistantUnanswered, row_id)
            assert row.answered_at is None
        finally:
            session.close()
    print("OK: platforma egasiga tegishli bo'lmagan chatdan kelgan reply -- javob-rele mexanizmini UMUMAN ishga tushirmaydi")


def test_already_answered_row_not_rematched():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_app(os.path.join(tmp, "s5.db"), owner_group=-100004)
        import datetime as _dt
        row_id = _make_pending_row(
            db_module, origin="web", manager_id=None,
            notify_chat_id="-100004", notify_message_id=888,
            answered_at=_dt.datetime.utcnow(), answered_by="Boshqa admin", answer_text="Eski javob",
        )
        client = app_module.app.test_client()

        with mock.patch.object(app_module, "handle_free_text") as mock_ht:
            r = client.post("/api/webhook", json={
                "message": {
                    "chat": {"id": -100004, "type": "private"},
                    "text": "Ikkinchi marta javob berishga urinish",
                    "reply_to_message": {"message_id": 888},
                    "from": {"id": 1, "first_name": "Owner"},
                }
            })
        assert r.status_code == 200
        mock_ht.assert_called_once_with(-100004, "Ikkinchi marta javob berishga urinish")

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                row = session.get(db_module.AssistantUnanswered, row_id)
            assert row.answer_text == "Eski javob", "allaqachon javob berilgan qator QAYTA yozilmasligi kerak"
        finally:
            session.close()
    print("OK: allaqachon javob berilgan (answered_at to'ldirilgan) qator ikkinchi REPLY bilan qayta moslashtirilmaydi")


def test_web_origin_reply_appends_to_correct_manager_only():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_app(os.path.join(tmp, "s6.db"), owner_group=-100005)
        client = app_module.app.test_client()
        _signup(client, company_name="Nishon MChJ", admin_username="nishon_admin")

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                target_manager = session.query(db_module.Manager).filter_by(username="nishon_admin").first()
                target_id = target_manager.id
            other_manager = db_module.Manager(company_id=target_manager.company_id, username="boshqa_admin", role="admin", full_name="Boshqa Admin")
            other_manager.set_password("parol123456")
            session.add(other_manager)
            session.commit()
            other_id = other_manager.id
        finally:
            session.close()

        app_module.kv_store.set_json(f"web_chat_history:{target_id}", [{"role": "user", "content": "Salom"}])
        app_module.kv_store.set_json(f"web_chat_history:{other_id}", [{"role": "user", "content": "Boshqa savol"}])

        row_id = _make_pending_row(
            db_module, origin="web", manager_id=target_id,
            notify_chat_id="-100005", notify_message_id=999,
            question="Narxlar qanday?",
        )

        with mock.patch.object(app_module, "tg_send") as mock_send:
            r = app_module.app.test_client().post("/api/webhook", json={
                "message": {
                    "chat": {"id": -100005, "type": "private"},
                    "text": "Narxlar 500 ming so'mdan boshlanadi",
                    "reply_to_message": {"message_id": 999},
                    "from": {"id": 7, "first_name": "Platforma egasi"},
                }
            })
        assert r.status_code == 200

        target_history = app_module.kv_store.get_json(f"web_chat_history:{target_id}", default=[])
        other_history = app_module.kv_store.get_json(f"web_chat_history:{other_id}", default=[])
        assert len(target_history) == 2, target_history
        assert "500 ming" in target_history[-1]["content"]
        assert target_history[-1]["role"] == "assistant"
        assert len(other_history) == 1, "BOSHQA menejerning web-suhbat tarixi TEGILMASLIGI kerak"

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                row = session.get(db_module.AssistantUnanswered, row_id)
            assert row.answered_at is not None
            assert row.answer_text == "Narxlar 500 ming so'mdan boshlanadi"
        finally:
            session.close()

        # Owner'ga "yuborildi" tasdiq xabari ketishi kerak.
        assert any(c[0][0] == -100005 and "yuborildi" in c[0][1] for c in mock_send.call_args_list)
    print("OK: WEB kelib chiqishidagi javob TO'G'RI manager'ning web_chat_history'siga qo'shiladi, BOSHQA menejernikiga tegmaydi")


def test_telegram_origin_reply_relays_to_original_chat():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_app(os.path.join(tmp, "s7.db"), owner_group=-100006)
        row_id = _make_pending_row(
            db_module, origin="telegram", chat_id="-400111",
            notify_chat_id="-100006", notify_message_id=1010,
            question="Ishlash vaqti qachon?",
        )

        with mock.patch.object(app_module, "tg_send") as mock_send:
            r = app_module.app.test_client().post("/api/webhook", json={
                "message": {
                    "chat": {"id": -100006, "type": "private"},
                    "text": "Dushanba-shanba, 9:00-18:00",
                    "reply_to_message": {"message_id": 1010},
                    "from": {"id": 7, "first_name": "Owner"},
                }
            })
        assert r.status_code == 200

        relay_calls = [c for c in mock_send.call_args_list if c[0][0] == -400111]
        assert len(relay_calls) == 1, mock_send.call_args_list
        assert "Dushanba-shanba" in relay_calls[0][0][1]
        assert "javob berdi" in relay_calls[0][0][1]

        confirm_calls = [c for c in mock_send.call_args_list if c[0][0] == -100006]
        assert len(confirm_calls) == 1 and "yuborildi" in confirm_calls[0][0][1]

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                row = session.get(db_module.AssistantUnanswered, row_id)
            assert row.answered_at is not None
        finally:
            session.close()
    print("OK: TELEGRAM kelib chiqishidagi javob ASL chat_id'ga to'g'ri yuboriladi, platforma egasiga tasdiq xabari ketadi")


def test_webhook_normal_flow_unaffected_without_reply():
    """Javob-rele qo'shilishi ODDIY (reply bo'lmagan) xabarlar oqimini
    umuman o'zgartirmasligi kerak -- mavjud guruh-gating xatti-harakati."""
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_app(os.path.join(tmp, "s8.db"), owner_group=-100007)

        def _fake_get(url, timeout=None):
            class _Resp:
                def json(self_inner):
                    if url.endswith("/getMe"):
                        return {"ok": True, "result": {"id": 999, "username": "targetolog_bot", "is_bot": True}}
                    return {"ok": False}
            return _Resp()

        with mock.patch("requests.get", side_effect=_fake_get), \
             mock.patch.object(app_module, "handle_free_text") as mock_ht:
            r = app_module.app.test_client().post("/api/webhook", json={
                "message": {"chat": {"id": -100007, "type": "private"}, "text": "hisobim qanday ketyapti", "from": {"id": 7}}
            })
        assert r.status_code == 200
        mock_ht.assert_called_once_with(-100007, "hisobim qanday ketyapti")
    print("OK: reply bo'lmagan oddiy xabar -- javob-rele tekshiruvi hech narsaga ta'sir qilmasdan odatdagi dispetcherga o'tadi")


# ---------------------------------------------------------------------------
# 3) Superadmin panel -- faqat platforma egasiga ochiq
# ---------------------------------------------------------------------------

def test_ai_conversations_panel_owner_only():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_app(os.path.join(tmp, "s9.db"))
        client = app_module.app.test_client()

        session = db_module.get_session()
        try:
            owner_username = "platform_owner_ai"
            m = db_module.Manager(company_id=1, username=owner_username, role="admin", full_name="Owner")
            m.set_password("parol123456")
            session.add(m)
            session.commit()
        finally:
            session.close()
        _login(client, owner_username)

        app_module.kv_store.set_json("web_chat_history:1", [{"role": "user", "content": "salom"}, {"role": "assistant", "content": "salom!"}])

        r = client.get("/companies/ai-suhbatlar")
        assert r.status_code == 200
        assert "AI-yordamchi suhbatlari" in r.get_data(as_text=True)
    print("OK: '/companies/ai-suhbatlar' platforma egasiga to'g'ri ochiladi")


def test_ai_conversations_panel_rejects_regular_admin():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_app(os.path.join(tmp, "s10.db"))
        client = app_module.app.test_client()
        _signup(client, company_name="Oddiy MChJ", admin_username="oddiy_ai_admin")

        r = client.get("/companies/ai-suhbatlar", follow_redirects=True)
        assert r.status_code == 200
        assert "faqat platforma egasi uchun" in r.get_data(as_text=True)
    print("OK: '/companies/ai-suhbatlar' oddiy mijoz-kompaniya admin'i uchun rad etiladi")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
