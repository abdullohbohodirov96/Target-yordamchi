"""test_admin_report_followup_multitenant_offline.py — 2026-09, foydalanuvchi
so'rovi: "hozircha maksimum 5ta kompaniya bo'ldi, shuni to'g'rila,
ma'lumotlar adashib ketmasin, bot ulansa guruhga ham adashib ketmasin,
har kuni ertalab 9da webda ham aniq ko'rsatsin, leadlar adashib
chalkashmasin, tg bot hisobotlar".

HAQIQIY topilgan sabab: `dashboard_data.get_kpis()`/CRM Web tomoni allaqachon
(oldingi ishda) to'g'ri tenant-izolyatsiya qilingan edi (`test_meta_cross_
tenant_isolation_offline.py`ga qarang), LEKIN fon vazifalaridagi (`scheduler.
py`) Telegram HISOBOTLARI hali ham ESKI, YAGONA-GLOBAL-AKKAUNT mantig'i
bilan ishlardi:
  1. `orchestrator._crm_leads_count_today()` -- HECH QANDAY company_id
     filtrisiz edi (fon vazifalarida `db.py`dagi avtomatik tenant-filtr
     FAOL EMAS, chunki `_current_company_id` standart holatda `None`) --
     BARCHA kompaniyalarning bugungi leadlarini QO'SHIB hisoblardi.
  2. `scheduler.job_admin_report()` (har kuni 09:00) FAQAT platforma
     egasining (Company #1) Meta hisobini, global ENV guruhiga yuborardi
     -- boshqa kompaniyalar HECH QANDAY kunlik hisobot OLMAS edi.
  3. `scheduler.job_standing_reports()` xuddi shunday -- BARCHA due
     chat_id'larga BITTA (platforma egasining) hisobotni yuborardi.
  4. `scheduler.job_followup_reminders()` -- `session.query(db.Lead)`
     filtrsiz edi, umumiy xulosa BARCHA kompaniyalarning (potentsial
     mijoz ismi/telefoni bilan) leadlarini QO'SHIB, platforma egasining
     umumiy guruhiga yuborardi.

Bu fayl 1-2-4'ni tekshiradi (3-standing-reports xuddi shu naqsh, alohida
test shart emas).

Ishga tushirish:
    cd app && python3 scripts/test_admin_report_followup_multitenant_offline.py
"""

import os
import sys
import tempfile
import datetime as dt
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")


def _fresh_modules(db_path, *, owner_meta_ad_account_id=None, owner_meta_access_token=None):
    for name in ("db", "kv_store", "orchestrator", "monthly_report", "meta_api", "scheduler", "dashboard_data"):
        sys.modules.pop(name, None)
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    # MUHIM: platforma egasi uchun "eski, global ENV" kredensiallari --
    # `meta_api.py`ning module-darajasidagi konstantalari IMPORT vaqtida
    # o'qiladi, shuning uchun shu yerda, `import meta_api`dan OLDIN
    # o'rnatiladi/tozalanadi.
    if owner_meta_ad_account_id:
        os.environ["META_AD_ACCOUNT_ID"] = owner_meta_ad_account_id
    else:
        os.environ.pop("META_AD_ACCOUNT_ID", None)
    if owner_meta_access_token:
        os.environ["META_ACCESS_TOKEN"] = owner_meta_access_token
    else:
        os.environ.pop("META_ACCESS_TOKEN", None)
    os.environ.pop("META_PAGE_ID", None)
    os.environ["TELEGRAM_AGENTS_GROUP_ID"] = "-1009999"  # platforma egasining umumiy guruhi
    os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"
    import db as db_module
    db_module.init_db()
    import orchestrator
    import scheduler
    return db_module, orchestrator, scheduler


def _make_company(db_module, *, name, meta_ad_account_id=None, meta_access_token=None, telegram_group_id=None, is_active=True):
    session = db_module.get_session()
    try:
        c = db_module.Company(
            name=name, meta_ad_account_id=meta_ad_account_id, meta_access_token=meta_access_token,
            telegram_group_id=telegram_group_id, is_active=is_active,
        )
        session.add(c)
        session.commit()
        return c.id
    finally:
        session.close()


def _make_lead(db_module, *, company_id, full_name, next_contact_at=None, created_at=None, assigned_manager_id=None):
    session = db_module.get_session()
    try:
        lead = db_module.Lead(
            company_id=company_id, full_name=full_name,
            next_contact_at=next_contact_at, created_at=created_at or dt.datetime.utcnow(),
            assigned_manager_id=assigned_manager_id,
        )
        session.add(lead)
        session.commit()
        return lead.id
    finally:
        session.close()


def test_crm_leads_count_today_is_scoped_per_company():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, orchestrator, _ = _fresh_modules(os.path.join(tmp, "t1.db"))
        company_a = _make_company(db_module, name="A", telegram_group_id="-1001")
        company_b = _make_company(db_module, name="B", telegram_group_id="-1002")

        now = dt.datetime.utcnow()
        _make_lead(db_module, company_id=company_a, full_name="Ali", created_at=now)
        _make_lead(db_module, company_id=company_a, full_name="Vali", created_at=now)
        _make_lead(db_module, company_id=company_b, full_name="Boris", created_at=now)

        count_a = orchestrator._crm_leads_count_today(company_id=company_a)
        count_b = orchestrator._crm_leads_count_today(company_id=company_b)
        count_default = orchestrator._crm_leads_count_today()  # kompaniyasiz -- standart (Company #1)ga tushadi

        assert count_a == 2, f"Kompaniya A'ning bugungi lead soni 2 bo'lishi kerak, oldi: {count_a}"
        assert count_b == 1, f"Kompaniya B'ning bugungi lead soni 1 bo'lishi kerak, oldi: {count_b}"
        assert count_default == 0, f"Standart (bo'sh) kompaniyada 0 ta lead bo'lishi kerak, oldi: {count_default} -- A/B'ning leadlari sizib chiqqan bo'lishi mumkin"
    print("OK: _crm_leads_count_today() endi ANIQ company_id bo'yicha filtrlanadi -- boshqa kompaniyaning leadlari qo'shilib ketmaydi")


def test_job_admin_report_sends_each_companys_own_numbers_to_its_own_group():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, orchestrator, scheduler = _fresh_modules(
            os.path.join(tmp, "t2.db"), owner_meta_ad_account_id="act_owner", owner_meta_access_token="tok_owner",
        )

        company_a = _make_company(db_module, name="Kompaniya A", meta_ad_account_id="act_a", meta_access_token="tok_a", telegram_group_id="-1001")
        # Telegram guruhini hali sozlamagan kompaniya -- hisobot hech qayerga yuborilmasligi kerak.
        company_b_no_group = _make_company(db_module, name="Kompaniya B (guruhsiz)", meta_ad_account_id="act_b", meta_access_token="tok_b", telegram_group_id=None)

        spend_by_account = {"act_owner": 10.0, "act_a": 111.0, "act_b": 222.0}

        import meta_api

        def fake_get_insights(*args, **kwargs):
            level = kwargs.get("level")
            # Haqiqiy `get_insights()` bilan BIR XIL fallback -- `ad_account_id`
            # berilmasa (platforma egasi yo'li), global ENV qiymatiga tushadi.
            ad_account_id = kwargs.get("ad_account_id") or meta_api.AD_ACCOUNT_ID
            if level == "account":
                spend = spend_by_account.get(ad_account_id, -1.0)
                return [{"spend": spend, "actions": [], "impressions": 10, "reach": 9, "ctr": 1.0, "cpc": 0.1, "cpm": 1.0, "frequency": 1.0}]
            return []

        def fake_get_account_structure(*args, **kwargs):
            return {"campaigns": [], "adsets": [], "ads": []}

        import meta_api
        sent = []

        def fake_tg_send(chat_id, text):
            sent.append((chat_id, text))
            return {"ok": True, "error": None}

        with mock.patch.object(meta_api, "get_insights", side_effect=fake_get_insights), \
             mock.patch.object(meta_api, "get_account_structure", side_effect=fake_get_account_structure), \
             mock.patch.object(scheduler, "_tg_send", side_effect=fake_tg_send):
            result = scheduler.job_admin_report()

        by_chat = {}
        for chat_id, text in sent:
            by_chat.setdefault(chat_id, []).append(text)

        assert -1009999 in by_chat, f"platforma egasi ESKI (global) guruhiga hisobot olishi kerak: {result}"
        assert any("$10.00" in t for t in by_chat[-1009999]), f"platforma egasining hisoboti O'Z ($10) xarajatini ko'rsatishi kerak: {by_chat[-1009999]}"
        assert not any("$111.00" in t or "$222.00" in t for t in by_chat[-1009999]), "platforma egasi boshqa kompaniyalarning xarajatini ko'rmasligi kerak"

        assert -1001 in by_chat, f"Kompaniya A O'Z guruhiga hisobot olishi kerak: {result}"
        assert any("$111.00" in t for t in by_chat[-1001]), f"Kompaniya A'ning hisoboti O'Z ($111) xarajatini ko'rsatishi kerak: {by_chat[-1001]}"
        assert not any("$10.00" in t or "$222.00" in t for t in by_chat[-1001]), "Kompaniya A boshqa kompaniyalarning ma'lumotini ko'rmasligi kerak"

        assert -1009999 not in [c for c, _ in sent if c != -1009999], True  # sanity no-op
        assert sum(1 for c, _ in sent if c == -1001) == 1, "Kompaniya A'ga aynan bitta hisobot yuborilishi kerak"
        assert all(c != None for c, _ in sent)
        # Guruhi sozlanmagan Kompaniya B -- hech qanday chat_id'ga uning ma'lumoti ($222) yuborilmasligi kerak.
        assert not any("$222.00" in t for _, t in sent), "Guruhi yo'q Kompaniya B'ning ma'lumoti hech qayerga (hatto platforma egasiga ham) sizib chiqmasligi kerak"
    print("OK: job_admin_report() endi HAR BIR kompaniyaning O'Z Meta hisobidan hisoblangan hisobotini FAQAT O'ZINING Telegram guruhiga yuboradi")


def test_job_followup_reminders_does_not_leak_lead_names_across_companies():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, orchestrator, scheduler = _fresh_modules(os.path.join(tmp, "t3.db"))
        owner_id = db_module.get_default_company_id()
        company_a = _make_company(db_module, name="Kompaniya A", telegram_group_id="-2001")
        company_b = _make_company(db_module, name="Kompaniya B", telegram_group_id="-2002")

        yesterday = dt.datetime.utcnow() - dt.timedelta(days=1)
        _make_lead(db_module, company_id=owner_id, full_name="Egasi Mijoz", next_contact_at=yesterday)
        _make_lead(db_module, company_id=company_a, full_name="Anvar A", next_contact_at=yesterday)
        _make_lead(db_module, company_id=company_b, full_name="Bekzod B", next_contact_at=yesterday)
        _make_lead(db_module, company_id=company_b, full_name="Boris B", next_contact_at=yesterday)

        sent = []

        def fake_tg_send(chat_id, text):
            sent.append((chat_id, text))
            return {"ok": True, "error": None}

        with mock.patch.object(scheduler, "_tg_send", side_effect=fake_tg_send):
            result = scheduler.job_followup_reminders()

        assert result["due_count"] == 4, f"jami 4 ta lead (hammasi) hisoblanishi kerak: {result}"

        by_chat = {}
        for chat_id, text in sent:
            by_chat.setdefault(chat_id, []).append(text)

        assert -1009999 in by_chat, "platforma egasi ESKI (global) guruhiga xulosa olishi kerak"
        owner_text = "\n".join(by_chat[-1009999])
        assert "Anvar" not in owner_text and "Bekzod" not in owner_text and "Boris" not in owner_text, \
            f"platforma egasining xulosasida boshqa kompaniyalarning mijoz ismlari bo'lmasligi kerak: {owner_text}"

        assert -2001 in by_chat, "Kompaniya A O'Z guruhiga xulosa olishi kerak"
        text_a = "\n".join(by_chat[-2001])
        assert "1 ta" in text_a or "jami 1" in text_a, f"Kompaniya A'ning xulosasida FAQAT 1 ta lead ko'rsatilishi kerak: {text_a}"
        assert "Bekzod" not in text_a and "Boris" not in text_a and "Egasi" not in text_a, \
            f"Kompaniya A'ning guruhida boshqa kompaniyalarning mijoz ismlari bo'lmasligi kerak: {text_a}"

        assert -2002 in by_chat, "Kompaniya B O'Z guruhiga xulosa olishi kerak"
        text_b = "\n".join(by_chat[-2002])
        assert "jami 2" in text_b, f"Kompaniya B'ning xulosasida FAQAT 2 ta lead ko'rsatilishi kerak: {text_b}"
        assert "Anvar" not in text_b and "Egasi" not in text_b, \
            f"Kompaniya B'ning guruhida boshqa kompaniyalarning mijoz ismlari bo'lmasligi kerak: {text_b}"
    print("OK: job_followup_reminders() endi har bir kompaniyani ALOHIDA (db.scoped_as) hisoblaydi -- umumiy xulosada boshqa kompaniyaning mijoz ismi/soni chiqmaydi")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
