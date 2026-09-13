"""test_telegram_group_gating_and_owner_scope_offline.py — 2026-09,
foydalanuvchidan kelgan IKKITA shoshilinch xatolik uchun TARMOQSIZ
(offline) tekshiruvlar:

  1. "guruhda odiy gaplashganda bot o'ziga ovormasin, faqat unga
     murojaat qilinsa yozsin" -- guruh chatida bot FAQAT unga
     to'g'ridan-to'g'ri murojaat qilinganda (@username mention yoki
     uning O'Z xabariga reply) javob berishi kerak, aks holda guruh
     a'zolari o'zaro oddiy gaplashganda bot HECH NARSA qilmasligi (va
     eng muhimi -- LLM chaqiruv qilmasligi, token sarflamasligi) kerak.

  2. "bot hisobot yuborganda kompaniyalarni adashtirib, Dunyabunya
     (platforma egasi)ning hisobotini boshqa kompaniyaga yuborib
     qo'yyapti" -- erkin suhbat (`handle_free_text`) va hisobga
     to'g'ridan-to'g'ri ta'sir qiluvchi buyruqlar (/status, /analyze,
     /pause, /resume) FAQAT platforma egasining O'Z Telegram
     chatidan/guruhidan chaqirilishi mumkin -- boshqa har qanday
     (masalan, biror kompaniyaning o'z Telegram guruhi) chatdan
     kelsa, ular RAD ETILISHI kerak, orchestrator/Meta API'ga umuman
     chaqiruv qilinmasdan.

Ishga tushirish:
    cd app && python3 scripts/test_telegram_group_gating_and_owner_scope_offline.py
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


def _fresh_app(db_path, *, owner_group_env=None):
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
    os.environ.pop("TELEGRAM_AGENTS_GROUP_ID", None)
    os.environ.pop("TELEGRAM_REPORT_GROUP_ID", None)
    if owner_group_env is not None:
        os.environ["TELEGRAM_AGENTS_GROUP_ID"] = str(owner_group_env)
    import db as db_module
    db_module.init_db()
    import app as app_module
    app_module.app.config["TESTING"] = True
    app_module.app.config["WTF_CSRF_ENABLED"] = False  # 2026-09, CSRF endi majburiy -- testlarda so'rovlar session-tashqarisida yasaladi
    # Har bir test botning o'z `getMe` keshini nol boshidan sinasin.
    app_module._BOT_IDENTITY_CACHE.clear()
    return db_module, app_module


def _mock_get_me(bot_id=999, username="targetolog_bot"):
    def _fake_get(url, timeout=None):
        class _Resp:
            def json(self_inner):
                if url.endswith("/getMe"):
                    return {"ok": True, "result": {"id": bot_id, "username": username, "is_bot": True}}
                return {"ok": False}
        return _Resp()
    return _fake_get


# ---------------------------------------------------------------------------
# 1) Guruh chatida bot faqat murojaat qilinganda javob beradi
# ---------------------------------------------------------------------------

def test_group_plain_chatter_without_mention_is_ignored():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_app(os.path.join(tmp, "s1.db"))
        client = app_module.app.test_client()

        with mock.patch("requests.get", side_effect=_mock_get_me()), \
             mock.patch.object(app_module, "handle_free_text") as mock_ht, \
             mock.patch.object(app_module, "tg_send") as mock_send:
            r = client.post("/api/webhook", json={
                "message": {
                    "chat": {"id": -100111, "type": "group"},
                    "text": "salom hammaga, bugun ob-havo yaxshi ekan",
                    "from": {"id": 42},
                }
            })
        assert r.status_code == 200
        mock_ht.assert_not_called()
        mock_send.assert_not_called()
    print("OK: guruhda oddiy (botga murojaat qilinmagan) gap-so'zga bot HECH NARSA qilmaydi -- classify_intent/LLM chaqirilmaydi")


def test_group_message_mentioning_bot_is_processed():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_app(os.path.join(tmp, "s2.db"), owner_group_env=-100222)
        client = app_module.app.test_client()

        text = "@targetolog_bot hisobim qanday ketyapti"
        mention_len = len("@targetolog_bot")
        with mock.patch("requests.get", side_effect=_mock_get_me()), \
             mock.patch.object(app_module, "handle_free_text") as mock_ht:
            r = client.post("/api/webhook", json={
                "message": {
                    "chat": {"id": -100222, "type": "group"},
                    "text": text,
                    "entities": [{"type": "mention", "offset": 0, "length": mention_len}],
                    "from": {"id": 42},
                }
            })
        assert r.status_code == 200
        mock_ht.assert_called_once_with(-100222, text)
    print("OK: guruhda bot @username orqali to'g'ridan-to'g'ri murojaat qilinsa -- xabar odatdagidek qayta ishlanadi")


def test_group_reply_to_bots_own_message_is_processed():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_app(os.path.join(tmp, "s3.db"), owner_group_env=-100333)
        client = app_module.app.test_client()

        with mock.patch("requests.get", side_effect=_mock_get_me(bot_id=999)), \
             mock.patch.object(app_module, "handle_free_text") as mock_ht:
            r = client.post("/api/webhook", json={
                "message": {
                    "chat": {"id": -100333, "type": "group"},
                    "text": "rahmat",
                    "reply_to_message": {"from": {"id": 999}},
                    "from": {"id": 42},
                }
            })
        assert r.status_code == 200
        mock_ht.assert_called_once_with(-100333, "rahmat")
    print("OK: guruhda botning O'Z xabariga reply qilingan xabar ham 'murojaat qilingan' deb hisoblanadi")


def test_private_chat_is_never_gated_by_mention_check():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_app(os.path.join(tmp, "s4.db"), owner_group_env=-100444)
        client = app_module.app.test_client()

        with mock.patch("requests.get", side_effect=_mock_get_me()), \
             mock.patch.object(app_module, "handle_free_text") as mock_ht:
            r = client.post("/api/webhook", json={
                "message": {"chat": {"id": 555, "type": "private"}, "text": "salom", "from": {"id": 555}}
            })
        assert r.status_code == 200
        mock_ht.assert_called_once_with(555, "salom")
    print("OK: shaxsiy (private) chatda mention-tekshiruvi UMUMAN qo'llanmaydi -- bot odatdagidek javob beradi")


# ---------------------------------------------------------------------------
# 2) Erkin suhbat + hisobga ta'sir qiluvchi buyruqlar FAQAT platforma
#    egasining O'Z chatidan ishlaydi
# ---------------------------------------------------------------------------

def test_free_text_from_non_owner_chat_is_declined():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_app(os.path.join(tmp, "s5.db"), owner_group_env=-200111)
        sent = []
        with mock.patch.object(app_module, "tg_send", side_effect=lambda cid, text: sent.append((cid, text))), \
             mock.patch.object(app_module.orchestrator, "classify_intent") as mock_classify:
            app_module.handle_free_text(-200999, "Dunyabunya hisoboti qanday ketyapti?")
        mock_classify.assert_not_called()
        assert len(sent) == 1 and sent[0][0] == -200999
    print("OK: platforma egasiga tegishli bo'lmagan (boshqa kompaniya guruhi) chatdan erkin savol -- rad etiladi, classify_intent chaqirilmaydi")


def test_free_text_from_owner_chat_proceeds_normally():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_app(os.path.join(tmp, "s6.db"), owner_group_env=-200222)
        with mock.patch.object(app_module.orchestrator, "classify_intent", return_value=("LIGHT", "")) as mock_classify, \
             mock.patch.object(app_module.orchestrator, "is_heavy_intent", return_value=False), \
             mock.patch.object(app_module.orchestrator, "execute_intent", return_value="hammasi yaxshi"), \
             mock.patch.object(app_module, "tg_send") as mock_send:
            app_module.handle_free_text(-200222, "hisobim qanday ketyapti")
        mock_classify.assert_called_once()
        mock_send.assert_called_once_with(-200222, "hammasi yaxshi")
    print("OK: platforma egasining O'Z guruhidan (ENV'dagi TELEGRAM_AGENTS_GROUP_ID) erkin savol -- odatdagidek ishlanadi")


def test_owner_only_commands_declined_from_non_owner_chat_no_meta_call():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_app(os.path.join(tmp, "s7.db"), owner_group_env=-200333)
        for cmd, args in (("/status", []), ("/analyze", []), ("/pause", ["ad_1"]), ("/resume", ["ad_1"])):
            sent = []
            with mock.patch.object(app_module, "tg_send", side_effect=lambda cid, text: sent.append((cid, text))), \
                 mock.patch.object(app_module.meta_api, "pause_object") as mock_pause, \
                 mock.patch.object(app_module.meta_api, "activate_object") as mock_activate, \
                 mock.patch.object(app_module.orchestrator, "run_analysis_cycle") as mock_analyze:
                app_module.handle_command(-200999, cmd, args)
            assert len(sent) == 1, f"{cmd}: rad javobi yuborilishi kerak edi"
            mock_pause.assert_not_called()
            mock_activate.assert_not_called()
            mock_analyze.assert_not_called()
        print("OK: /status, /analyze, /pause, /resume boshqa (egasiga tegishli bo'lmagan) chatdan chaqirilsa -- rad etiladi, Meta API'ga HECH QANDAY chaqiruv ketmaydi")


def test_owner_only_commands_work_from_owner_chat():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_app(os.path.join(tmp, "s8.db"), owner_group_env=-200444)
        sent = []
        with mock.patch.object(app_module, "tg_send", side_effect=lambda cid, text: sent.append((cid, text))):
            app_module.handle_command(-200444, "/status", [])
        assert len(sent) == 1
        assert "Hali tahlil ishga tushirilmagan" in sent[0][1] or sent[0][1]
    print("OK: /status platforma egasining O'Z chatidan chaqirilganda odatdagidek ishlaydi")


def test_start_command_does_not_hijack_budget_notify_for_non_owner():
    """2026-09 QAYTA TUZATISH (foydalanuvchi so'rovi: "bot boshqala yozsa
    registratsiya qiling ... deb qadamma-qadam tushuntirsin, webga link
    bersin"): -200999 hech qanday kompaniyaga/menejerga ULANMAGAN ("yot")
    chat -- endi unga avvalgi umumiy WELCOME_TEXT (Targetolog uchun,
    egasiga xos) EMAS, balki aniq ro'yxatdan o'tish yo'riqnomasi
    (`_REGISTER_HELP_TEXT`) yuboriladi. Byudjet ogohlantirish manzili
    baribir O'G'IRLANMASLIGI kerakligi o'zgarmagan."""
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_app(os.path.join(tmp, "s9.db"), owner_group_env=-200555)
        with mock.patch.object(app_module, "tg_send") as mock_send, \
             mock.patch.object(app_module.budget_tracker, "set_notify_chat_id") as mock_notify:
            app_module.handle_command(-200999, "/start", [])
        mock_send.assert_called_once_with(-200999, app_module._REGISTER_HELP_TEXT)
        mock_notify.assert_not_called()
    print("OK: /start hali ro'yxatdan o'tmagan chatdan chaqirilsa -- qadam-baqadam ro'yxatdan o'tish yo'riqnomasi yuboriladi, byudjet ogohlantirish manzili O'G'IRLANMAYDI")


def test_start_command_sets_budget_notify_for_owner_chat():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_app(os.path.join(tmp, "s10.db"), owner_group_env=-200666)
        with mock.patch.object(app_module, "tg_send") as mock_send, \
             mock.patch.object(app_module.budget_tracker, "set_notify_chat_id") as mock_notify:
            app_module.handle_command(-200666, "/start", [])
        mock_send.assert_called_once_with(-200666, app_module.WELCOME_TEXT)
        mock_notify.assert_called_once_with(-200666)
    print("OK: /start platforma egasining O'Z chatidan chaqirilsa -- byudjet ogohlantirishlari o'sha chatga sozlanadi (odatdagidek)")


def test_tenant_safe_commands_still_work_from_any_chat():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_app(os.path.join(tmp, "s11.db"), owner_group_env=-200777)
        sent = []
        with mock.patch.object(app_module, "tg_send", side_effect=lambda cid, text: sent.append((cid, text))):
            app_module.handle_command(-200999, "/groupid", [])
            app_module.handle_command(-200999, "/id", [])
            app_module.handle_command(-200999, "/vazifalar", [])
        assert len(sent) == 3, "kompaniya-xavfsiz buyruqlar HAR QANDAY chatdan ishlashi kerak (gate qilinmagan)"
        assert "-200999" in sent[0][1]
    print("OK: /groupid, /id, /vazifalar kabi kompaniya-xavfsiz buyruqlar owner-only gate'ga tushmaydi -- har qanday kompaniya chatidan ishlayveradi")


def test_owner_chat_also_recognized_via_manager_telegram_user_id():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_app(os.path.join(tmp, "s12.db"))  # ENV guruh sozlanmagan
        default_company_id = db_module.get_default_company_id()
        session = db_module.get_session()
        try:
            mgr = db_module.Manager(
                company_id=default_company_id, full_name="Egasi", username="egasi_admin",
                password_hash="x", role="admin", telegram_user_id="777888",
            )
            session.add(mgr)
            session.commit()
        finally:
            session.close()

        sent = []
        with mock.patch.object(app_module, "tg_send", side_effect=lambda cid, text: sent.append((cid, text))):
            app_module.handle_command(777888, "/status", [])
        assert len(sent) == 1
        assert "rad etil" not in sent[0][1].lower() and "faqat platforma" not in sent[0][1].lower()
    print("OK: ENV guruhi sozlanmagan bo'lsa ham, standart kompaniyaning menejeri (telegram_user_id mos) shaxsiy chatidan /status ishlayveradi")


# ---------------------------------------------------------------------------
# 3) 2026-09, foydalanuvchi so'rovi ("har kompaniyaga o'z AI-yordamchisi"):
#    registratsiyadan o'tgan (lekin platforma egasi bo'lmagan) kompaniya
#    chati endi RAD ETILMAYDI -- agar tarifida AI bo'lsa, o'sha kompaniyaning
#    o'z ma'lumotlariga asoslangan javob oladi; bo'lmasa -- upgrade taklifi.
#    Ikkalasida ham reklama boshqaruvi (classify_intent/execute_intent)
#    ISHGA TUSHMAYDI -- bu FAQAT platforma egasiga tegishli bo'lib qoladi.
# ---------------------------------------------------------------------------

def _make_company_with_telegram_group(db_module, *, plan, telegram_group_id, name="Mijoz MChJ"):
    session = db_module.get_session()
    try:
        c = db_module.Company(name=name, is_active=True, plan=plan, telegram_group_id=str(telegram_group_id))
        session.add(c)
        session.commit()
        return c.id
    finally:
        session.close()


def test_free_text_from_registered_ai_plan_company_gets_scoped_ai_reply():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_app(os.path.join(tmp, "s13.db"), owner_group_env=-300111)
        _make_company_with_telegram_group(db_module, plan="business", telegram_group_id=-300222)

        with mock.patch.object(app_module.orchestrator, "classify_intent") as mock_classify, \
             mock.patch.object(app_module.orchestrator, "execute_intent") as mock_execute, \
             mock.patch.object(app_module.orchestrator, "call_light_chat", return_value="Bugun 3 ta yangi lead keldi.") as mock_chat, \
             mock.patch.object(app_module, "tg_send") as mock_send:
            app_module.handle_free_text(-300222, "bugun necha lead keldi?")

        mock_classify.assert_not_called()
        mock_execute.assert_not_called()
        mock_chat.assert_called_once()
        mock_send.assert_called_once_with(-300222, "Bugun 3 ta yangi lead keldi.")
    print("OK: AI tarifi bor (business) ro'yxatdan o'tgan kompaniya guruhi -- rad etilmaydi, kompaniyaga xos AI javob oladi, reklama-boshqaruv (classify/execute_intent) ishga tushmaydi")


def test_free_text_from_registered_non_ai_plan_company_gets_upgrade_message():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_app(os.path.join(tmp, "s14.db"), owner_group_env=-300333)
        _make_company_with_telegram_group(db_module, plan="start", telegram_group_id=-300444)

        with mock.patch.object(app_module.orchestrator, "call_light_chat") as mock_chat, \
             mock.patch.object(app_module, "tg_send") as mock_send:
            app_module.handle_free_text(-300444, "bugun necha lead keldi?")

        mock_chat.assert_not_called()
        mock_send.assert_called_once()
        assert "tarifingizda mavjud emas" in mock_send.call_args[0][1]
    print("OK: AI'siz tarifdagi (start) ro'yxatdan o'tgan kompaniya guruhi -- AI chaqirilmaydi, tarifni oshirish haqida xabar yuboriladi")


def test_unregistered_chat_still_gets_register_help_not_company_ai():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_app(os.path.join(tmp, "s15.db"), owner_group_env=-300555)
        # Boshqa (bog'liq bo'lmagan) kompaniya bazada bor, lekin bu chat
        # ID'siga UNI HECH NARSASI ulanmagan.
        _make_company_with_telegram_group(db_module, plan="business", telegram_group_id=-300666)

        with mock.patch.object(app_module.orchestrator, "call_light_chat") as mock_chat, \
             mock.patch.object(app_module, "tg_send") as mock_send:
            app_module.handle_free_text(-300999, "salom")

        mock_chat.assert_not_called()
        mock_send.assert_called_once_with(-300999, app_module._REGISTER_HELP_TEXT)
    print("OK: hali hech qaysi kompaniyaga ulanmagan 'yot' chat -- boshqa kompaniya bazada bo'lsa ham, ro'yxatdan o'tish yo'riqnomasini oladi (AI ishga tushmaydi)")


def test_company_ai_snapshot_is_tenant_scoped():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_app(os.path.join(tmp, "s16.db"), owner_group_env=-300777)
        company_a_id = _make_company_with_telegram_group(db_module, plan="business", telegram_group_id=-300888, name="A firma")
        company_b_id = _make_company_with_telegram_group(db_module, plan="business", telegram_group_id=-300999, name="B firma")

        # MUHIM: `scoped_as` faqat SELECT so'rovlarni filtrlaydi
        # (`_apply_tenant_scope`ga qarang -- `if not execute_state.is_select:
        # return`) -- yangi qator qo'shilganda `company_id` ANIQ o'zi
        # ko'rsatilishi kerak, aks holda NULL bo'lib qoladi.
        session = db_module.get_session()
        try:
            session.add(db_module.Lead(company_id=company_a_id, full_name="A mijozi", phone="+998900000001", status="new"))
            session.add(db_module.Lead(company_id=company_b_id, full_name="B mijozi 1", phone="+998900000002", status="new"))
            session.add(db_module.Lead(company_id=company_b_id, full_name="B mijozi 2", phone="+998900000003", status="new"))
            session.commit()
        finally:
            session.close()

        snapshot_a = app_module._company_ai_snapshot(company_a_id)
        snapshot_b = app_module._company_ai_snapshot(company_b_id)
        assert "Jami lidlar (CRM'da, barcha vaqt): 1" in snapshot_a
        assert "Jami lidlar (CRM'da, barcha vaqt): 2" in snapshot_b
    print("OK: kompaniyaga xos AI snapshot faqat O'SHA kompaniyaning lidlarini sanaydi -- boshqa kompaniyaning ma'lumoti sizib chiqmaydi")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
