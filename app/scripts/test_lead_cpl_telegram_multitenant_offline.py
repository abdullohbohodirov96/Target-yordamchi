"""test_lead_cpl_telegram_multitenant_offline.py — 2026-09, foydalanuvchi
so'rovi ("yangi kompaniya ochilganda to'liq integratsiya ochilishi kerak --
targeting/xarajat ma'lumoti o'z guruhiga, lidlar o'z CRM'iga tushishi
kerak, target orqali ulangan reklama hisobidan lidlar o'sha kompaniyaga
tortilishi kerak"). Bu fayl TARMOQSIZ (offline) tekshiradi:

  1. `lead_sync.get_lead_forms`/`get_leads` (meta_api.py) endi to'g'ri
     `page_id`/`access_token` bilan chaqirilishini -- avvalgi bug (token
     har doim GLOBAL ENV'dan olinardi, `page_id` faqat URL yo'lida
     ishlatilardi) endi tuzatilganini.
  2. `lead_sync.sync_all_companies()` -- Meta Lead Ads ulagan HAR BIR
     kompaniyani O'Z tokeni/page_id'si bilan sinxronlaydi, yangi lead'lar
     TO'G'RI `company_id` bilan bazaga yoziladi (bir-biriga aralashmaydi).
  3. `orchestrator.enforce_cpl_hard_kill_all_companies()` -- har bir
     kompaniyani O'Z reklama hisobi/tokeni bilan tekshiradi va pauza qiladi.
  4. `scheduler.job_cpl_hard_kill()` -- pauza/xato xabarini FAQAT o'sha
     kompaniyaning O'Z Telegram guruhiga yuboradi; guruhi yo'q kompaniya
     uchun hech qayerga (platforma egasining umumiy guruhiga ham) yubormaydi.
  5. Yangi `/groupid` Telegram buyrug'i va "Akkauntlarni ulash" sahifasidagi
     Telegram guruh ID saqlash + test-ulanish endpoint'i.

Ishga tushirish:
    cd app && python3 scripts/test_lead_cpl_telegram_multitenant_offline.py
"""

import os
import sys
import tempfile
import datetime as dt
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ.setdefault("META_APP_ID", "test_app_id")
os.environ.setdefault("META_APP_SECRET", "test_app_secret")


def _fresh_modules(db_path):
    """Har bir test o'z ALOHIDA SQLite fayli va tegishli modullarning TOZA
    nusxasi bilan ishlaydi (`test_multitenant_sync_offline.py`dagi bilan bir
    xil naqsh -- bu modullar `db`dan sinf/funksiyalarni IMPORT VAQTIDA olib
    qo'yadi)."""
    for name in ("db", "kv_store", "lead_sync", "orchestrator", "scheduler", "meta_events", "app"):
        sys.modules.pop(name, None)
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ.pop("META_ACCESS_TOKEN", None)
    os.environ.pop("META_PAGE_ID", None)
    os.environ.pop("META_AD_ACCOUNT_ID", None)
    os.environ.pop("TELEGRAM_AGENTS_GROUP_ID", None)
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    import db as db_module
    db_module.init_db()
    import lead_sync
    return db_module, lead_sync


def _make_company(db_module, *, name, meta_page_id=None, meta_access_token=None,
                   meta_ad_account_id=None, telegram_group_id=None):
    session = db_module.get_session()
    try:
        c = db_module.Company(
            name=name, meta_page_id=meta_page_id, meta_access_token=meta_access_token,
            meta_ad_account_id=meta_ad_account_id, telegram_group_id=telegram_group_id,
            is_active=True,
        )
        session.add(c)
        session.commit()
        return c.id
    finally:
        session.close()


def test_lead_sync_all_companies_uses_own_token_and_writes_correct_company_id():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, lead_sync = _fresh_modules(os.path.join(tmp, "s1.db"))
        company_a = _make_company(db_module, name="Kompaniya A", meta_page_id="page_a", meta_access_token="tok_a")
        company_b = _make_company(db_module, name="Kompaniya B", meta_page_id="page_b", meta_access_token="tok_b", meta_ad_account_id="act_b")

        # Cursor'larni oldindan o'rnatamiz -- aks holda sync_once()ning
        # "birinchi ishga tushish" himoyasi bu safar HECH QANDAY lead
        # so'ramaydi (ataylab shunday, backlog muammosining oldini olish
        # uchun -- yuqoridagi lead_sync.py izohiga qarang).
        old_cursor = int(dt.datetime.utcnow().timestamp()) - 3600
        for cid in (company_a, company_b):
            import kv_store
            kv_store.set_json(lead_sync._since_key(cid), old_cursor)

        import meta_api

        forms_calls = []

        def fake_get_lead_forms(page_id, *, access_token=None):
            forms_calls.append((page_id, access_token))
            return [{"id": f"form_{page_id}", "name": f"Forma {page_id}", "leads_count": 1}]

        leads_calls = []

        def fake_get_leads(form_id, since=None, *, access_token=None, page_id=None):
            leads_calls.append((form_id, access_token, page_id))
            return [{
                "id": f"meta_lead_{form_id}", "created_time": dt.datetime.utcnow().isoformat(),
                "campaign_id": None, "adset_id": None, "ad_id": None, "form_id": form_id,
                "field_data": [
                    {"name": "full_name", "values": [f"Mijoz {form_id}"]},
                    {"name": "phone_number", "values": ["+998901234567"]},
                ],
            }]

        with mock.patch.object(meta_api, "get_lead_forms", side_effect=fake_get_lead_forms), \
             mock.patch.object(meta_api, "get_leads", side_effect=fake_get_leads), \
             mock.patch.object(meta_api, "get_account_structure", return_value={"campaigns": [], "adsets": [], "ads": []}), \
             mock.patch("meta_events.dispatch_lead_event"):
            overall = lead_sync.sync_all_companies()

        assert overall["companies_synced"] == 2, f"faqat Meta ulagan 2 ta kompaniya sinxronlanishi kerak edi: {overall}"
        assert overall["per_company"][company_a]["new_leads"] == 1
        assert overall["per_company"][company_b]["new_leads"] == 1

        # BUG FIX tekshiruvi: har bir chaqiruv AYNAN o'sha kompaniyaning
        # page_id/access_token juftligi bilan bo'lishi kerak (avval token
        # HAR DOIM global ENV'dan olinardi, page_id e'tiborga olinmasdi).
        forms_by_page = {p: t for p, t in forms_calls}
        assert forms_by_page["page_a"] == "tok_a"
        assert forms_by_page["page_b"] == "tok_b"
        for form_id, token, page_id in leads_calls:
            if page_id == "page_a":
                assert token == "tok_a"
            elif page_id == "page_b":
                assert token == "tok_b"

        session = db_module.get_session()
        with db_module.unscoped():
            lead_a = session.query(db_module.Lead).filter_by(meta_lead_id="meta_lead_form_page_a").first()
            lead_b = session.query(db_module.Lead).filter_by(meta_lead_id="meta_lead_form_page_b").first()
        session.close()
        assert lead_a is not None and lead_a.company_id == company_a, "Kompaniya A'ning lead'i O'Z company_id'si bilan yozilishi kerak"
        assert lead_b is not None and lead_b.company_id == company_b, "Kompaniya B'ning lead'i O'Z company_id'si bilan yozilishi kerak, A bilan aralashmasligi kerak"
    print("OK: lead_sync.sync_all_companies() har bir kompaniyani O'Z Meta tokeni/page_id'si bilan sinxronlaydi va lead'larni TO'G'RI company_id bilan yozadi (avvalgi get_lead_forms/get_leads token-e'tiborsizlik bug'i tuzatildi)")


def test_lead_sync_skips_campaign_enrichment_without_ad_account():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, lead_sync = _fresh_modules(os.path.join(tmp, "s2.db"))
        # Kompaniya HALI reklama hisobini ulamagan (faqat Page/lead forms bor).
        company_id = _make_company(db_module, name="Reklama hisobisiz kompaniya", meta_page_id="page_x", meta_access_token="tok_x")

        import kv_store
        old_cursor = int(dt.datetime.utcnow().timestamp()) - 3600
        kv_store.set_json(lead_sync._since_key(company_id), old_cursor)

        import meta_api
        structure_calls = []

        with mock.patch.object(meta_api, "get_lead_forms", return_value=[{"id": "form_x", "name": "Forma X", "leads_count": 0}]), \
             mock.patch.object(meta_api, "get_leads", return_value=[]), \
             mock.patch.object(meta_api, "get_account_structure", side_effect=lambda *a, **k: structure_calls.append((a, k)) or {"campaigns": [], "adsets": [], "ads": []}):
            session = db_module.get_session()
            try:
                with db_module.unscoped():
                    company = session.query(db_module.Company).get(company_id)
                fake = lead_sync._CompanyCreds(id=company.id, meta_page_id=company.meta_page_id, meta_access_token=company.get_meta_access_token(), meta_ad_account_id=company.meta_ad_account_id)
            finally:
                session.close()
            lead_sync.sync_once(company=fake)

        assert structure_calls == [], "reklama hisobi ulanmagan kompaniya uchun get_account_structure UMUMAN chaqirilmasligi kerak (boshqa kompaniyaning global hisobiga tushib qolmasligi uchun)"
    print("OK: lead_sync.sync_once() reklama hisobi ulanmagan kompaniya uchun kampaniya-nom boyitishni xavfsiz o'tkazib yuboradi (global hisobga fallback qilmaydi)")


def test_cpl_hard_kill_all_companies_uses_own_token_and_account():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, _ = _fresh_modules(os.path.join(tmp, "s3.db"))
        company_a = _make_company(db_module, name="Kompaniya A", meta_access_token="tok_a", meta_ad_account_id="act_a")
        company_b = _make_company(db_module, name="Kompaniya B", meta_access_token="tok_b", meta_ad_account_id="act_b")

        import orchestrator as orch

        kpi_calls = []

        def fake_get_kpis(level="campaign", date_preset="last_30d", active_only=False, *, access_token=None, ad_account_id=None):
            kpi_calls.append((access_token, ad_account_id))
            rows = {
                "act_a": [{"id": "ad_a", "name": "Ad A", "status": "ACTIVE", "spend": 10.0, "cpl": 5.0, "crm_leads_total": 1, "goal": "LEAD_GENERATION"}],
                "act_b": [{"id": "ad_b", "name": "Ad B", "status": "ACTIVE", "spend": 1.0, "cpl": 0.5, "crm_leads_total": 3, "goal": "LEAD_GENERATION"}],
            }
            return {"rows": rows.get(ad_account_id, [])}

        pause_calls = []

        def fake_execute_and_verify(object_id, expected_status, *, access_token=None):
            pause_calls.append((object_id, access_token))
            return {"status": expected_status, "verified": True}

        rules = {"cpl_hard_kill_usd": 1.5, "cpl_hard_kill_min_spend_usd": 3.0, "cpl_hard_kill_zero_lead_multiplier": 3.0, "protected_campaign_ids": []}

        with mock.patch.object(orch, "BUSINESS_RULES", rules), \
             mock.patch.object(orch.dashboard_data, "get_kpis", side_effect=fake_get_kpis), \
             mock.patch.object(orch.meta_api, "get_account_structure", return_value={"ads": []}), \
             mock.patch.object(orch, "_execute_and_verify_status", side_effect=fake_execute_and_verify):
            overall = orch.enforce_cpl_hard_kill_all_companies()

        assert overall["companies_checked"] == 2
        assert set(kpi_calls) == {("tok_a", "act_a"), ("tok_b", "act_b")}, f"har bir kompaniya O'Z tokeni/hisobi bilan tekshirilishi kerak: {kpi_calls}"
        assert pause_calls == [("ad_a", "tok_a")], f"faqat chegaradan oshgan Kompaniya A'ning reklamasi, O'Z tokeni bilan pauza qilinishi kerak: {pause_calls}"
        assert overall["per_company"][company_a]["paused"][0]["ad_id"] == "ad_a"
        assert overall["per_company"][company_b]["paused"] == []
    print("OK: orchestrator.enforce_cpl_hard_kill_all_companies() har bir kompaniyani O'Z reklama hisobi/tokeni bilan tekshiradi va pauza qiladi, boshqa kompaniyaga aralashmaydi")


def test_scheduler_cpl_hard_kill_routes_only_to_companys_own_telegram_group():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, _ = _fresh_modules(os.path.join(tmp, "s4.db"))
        company_a = _make_company(db_module, name="Kompaniya A", meta_access_token="tok_a", meta_ad_account_id="act_a", telegram_group_id="-1002222")
        company_b = _make_company(db_module, name="Kompaniya B", meta_access_token="tok_b", meta_ad_account_id="act_b", telegram_group_id=None)
        os.environ["TELEGRAM_AGENTS_GROUP_ID"] = "-1009999"  # platforma egasining UMUMIY guruhi -- bu yerga SIZIB CHIQMASLIGI kerak
        os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"

        sys.modules.pop("scheduler", None)
        import scheduler

        fake_overall = {
            "companies_checked": 2,
            "per_company": {
                company_a: {"checked": 1, "paused": [{"ad_id": "ad_a", "name": "Ad A", "reason": "CPL yuqori", "cpl": 5.0, "spend": 10.0}], "errors": []},
                company_b: {"checked": 1, "paused": [], "errors": ["Ad B (act_b): Meta xatosi"]},
            },
        }

        sent_messages = []

        def fake_tg_send(chat_id, text):
            sent_messages.append((chat_id, text))

        with mock.patch.object(scheduler.orchestrator, "enforce_cpl_hard_kill_all_companies", return_value=fake_overall), \
             mock.patch.object(scheduler, "_tg_send", side_effect=fake_tg_send):
            result = scheduler.job_cpl_hard_kill()

        assert result == fake_overall
        assert len(sent_messages) == 1, f"faqat Kompaniya A'ga (o'z guruhi sozlangan) xabar yuborilishi kerak edi: {sent_messages}"
        chat_id, text = sent_messages[0]
        assert chat_id == -1002222, "Kompaniya A'ning xabari O'Z telegram_group_id'iga borishi kerak"
        assert "Ad A" in text
        assert "Ad B" not in text, "Kompaniya B'ning ma'lumoti Kompaniya A'ning guruhiga sizib chiqmasligi kerak"
        assert -1009999 not in [c for c, _ in sent_messages], "platforma egasining umumiy guruhiga HECH NARSA yuborilmasligi kerak"
    print("OK: scheduler.job_cpl_hard_kill() har bir kompaniyaning pauza/xato xabarini FAQAT o'zining Telegram guruhiga yuboradi, guruhi yo'q kompaniya uchun hech qayerga (platforma egasiga ham) yubormaydi")


def test_groupid_command_echoes_group_chat_id():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, _ = _fresh_modules(os.path.join(tmp, "s5.db"))
        os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"
        sys.modules.pop("app", None)
        import app as app_module
        app_module.app.config["TESTING"] = True
        db_module.init_db()

        sent = []
        with mock.patch.object(app_module, "tg_send", side_effect=lambda cid, text: sent.append((cid, text))):
            app_module.handle_command(-1005555, "/groupid", [])

        assert len(sent) == 1
        chat_id, text = sent[0]
        assert chat_id == -1005555
        assert "-1005555" in text
        assert "Telegram guruh ID" in text
    print("OK: /groupid buyrug'i shu guruhning O'Z chat_id'sini qaytaradi (Akkauntlarni ulash sahifasiga joylashtirish uchun)")


def test_connect_accounts_saves_telegram_group_id_and_test_endpoint_reports_result():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, _ = _fresh_modules(os.path.join(tmp, "s6.db"))
        os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"
        sys.modules.pop("app", None)
        import app as app_module
        app_module.app.config["TESTING"] = True
        db_module.init_db()

        client = app_module.app.test_client()
        client.post("/signup", data={
            "company_name": "Guruh MChJ", "admin_username": "guruh_admin",
            "admin_full_name": "", "email": "", "plan": "business",
            "password": "parol123456", "password2": "parol123456",
        }, follow_redirects=True)

        r = client.post("/connect-accounts", data={"ig_business_id": "", "telegram_group_id": "-1007777"}, follow_redirects=True)
        assert r.status_code == 200

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                company = session.query(db_module.Company).filter_by(name="Guruh MChJ").first()
            assert company.telegram_group_id == "-1007777"
        finally:
            session.close()

        with mock.patch.object(app_module, "tg_send_checked", return_value={"ok": True, "error": None}) as mock_check:
            r2 = client.post("/connect-accounts/telegram/test", follow_redirects=True)
        assert r2.status_code == 200
        assert mock_check.call_args[0][0] == -1007777
    print("OK: 'Akkauntlarni ulash' sahifasi Telegram guruh ID'ni saqlaydi va test-ulanish endpoint'i saqlangan guruhga haqiqiy tekshiruv xabari yuboradi")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
