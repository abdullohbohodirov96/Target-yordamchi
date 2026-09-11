"""test_settings_and_standing_tasks_multitenant_offline.py — 2026-09,
foydalanuvchi so'rovi: "capi to'liq ishlayotgan bo'lsa endi, asosiy muammo
ikkita-uchta kompaniya ochilsa ular orasida ma'lumotlar aralashib
birbiriga o'tib ketvoti shuni to'g'rila, yechim qil, tg hisobot aralash,
target web ma'lumotlar aralash bo'p ketvoti shuni to'liq to'g'irlash
kerak".

Chuqurroq audit natijasida topilgan, ILGARI TUZATILMAGAN ikkita YANGI
cross-tenant "aralashish" manbai (avvalgi sessiyada scheduler.py'dagi
kunlik hisobot/eslatma/raqobatchi tahlili allaqachon tuzatilgan edi --
bu fayl ULARNI emas, QUYIDAGI YANGI ikkitasini tekshiradi):

  1. CPL/target chegaralari (`orchestrator.get_business_rule`/
     `set_business_rule`) va KPI sozlamalari (`kpi_bonus.get_min_sale_
     amount`/`set_min_sale_amount`, `get_usd_to_uzs_rate`) FAQAT bitta
     GLOBAL kv_store kaliti bilan saqlanardi -- QAYSI kompaniyaning
     admini "Sozlamalar"dan o'zgartirsa ham, bu HAMMA kompaniyaning CPL
     hard-kill tekshiruvi (haqiqiy reklamalarni pauza qilish!) VA
     dashboard/KPI/ROI hisobiga ham ta'sir qilardi. Bu -- aynan "target
     web ma'lumotlar aralash" va CPL orqali "tg hisobot aralash"
     shikoyatiga mos keladi.
  2. Telegram `/vazifalar` buyrug'i BARCHA kompaniyalarning faol doimiy
     (kampaniya yoqish/o'chirish + qo'shimcha hisobot) vazifalarini HAR
     QANDAY kompaniyaning chatiga ko'rsatardi, `/vazifa_off` esa ID'ni
     bilgan/taxmin qilgan har kim boshqa kompaniyaning vazifasini bekor
     qila olardi -- aynan "tg hisobot aralash" shikoyatiga mos keladi.

Ishga tushirish:
    cd app && python3 scripts/test_settings_and_standing_tasks_multitenant_offline.py
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


def _fresh_modules(db_path):
    for name in (
        "db", "kv_store", "app", "orchestrator", "kpi_bonus", "call_analytics",
        "scheduler", "lead_sync", "meta_events", "meta_api", "monthly_report",
        "ig_dm_sync", "ig_dm_analysis", "dashboard_data", "budget_tracker",
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


def _make_company(db_module, *, name, telegram_group_id=None):
    session = db_module.get_session()
    try:
        c = db_module.Company(name=name, telegram_group_id=telegram_group_id, is_active=True)
        session.add(c)
        session.commit()
        return c.id
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 1) CPL/target va KPI sozlamalari -- har bir kompaniya uchun ALOHIDA
# ---------------------------------------------------------------------------

def test_cpl_business_rule_is_isolated_per_company():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_modules(os.path.join(tmp, "b1.db"))
        import orchestrator
        owner_id = db_module.get_default_company_id()
        company_a = _make_company(db_module, name="Kompaniya A")

        # Standart (fayldan) qiymat -- ikkalasi ham hali sozlamagan.
        default_val = orchestrator.get_business_rule("cpl_hard_kill_usd")

        # Faqat platforma egasi CPL chegarasini o'zgartiradi.
        orchestrator.set_business_rule("cpl_hard_kill_usd", 9.99, company_id=owner_id)

        owner_val = orchestrator.get_business_rule("cpl_hard_kill_usd", company_id=owner_id)
        company_a_val = orchestrator.get_business_rule("cpl_hard_kill_usd", company_id=company_a)

        assert owner_val == 9.99, f"platforma egasining o'zi o'rnatgan qiymati qaytishi kerak: {owner_val}"
        assert company_a_val == default_val, (
            f"Kompaniya A hali o'z CPL chegarasini sozlamagan -- platforma egasining "
            f"9.99'ini emas, standart qiymatni ({default_val}) ko'rishi kerak, oldi: {company_a_val}"
        )

        # Endi Kompaniya A ham O'ZINING chegarasini sozlaydi -- ikkalasi ham
        # bir-biriga sira ta'sir qilmasligi kerak.
        orchestrator.set_business_rule("cpl_hard_kill_usd", 3.33, company_id=company_a)
        assert orchestrator.get_business_rule("cpl_hard_kill_usd", company_id=owner_id) == 9.99, \
            "Kompaniya A o'z chegarasini o'zgartirgani platforma egasining qiymatiga tegmasligi kerak"
        assert orchestrator.get_business_rule("cpl_hard_kill_usd", company_id=company_a) == 3.33
    print("OK: orchestrator.get_business_rule/set_business_rule endi HAR BIR kompaniya uchun ALOHIDA (bitta kompaniyaning CPL sozlamasi boshqasiga sira ta'sir qilmaydi)")


def test_enforce_cpl_hard_kill_uses_each_companys_own_threshold():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_modules(os.path.join(tmp, "b2.db"))
        import orchestrator
        import dashboard_data
        owner_id = db_module.get_default_company_id()
        company_a = _make_company(db_module, name="Kompaniya A")

        # Platforma egasi CPL chegarasini $1 ga qattiq qo'yadi (deyarli
        # hammasi pauza qilinadi), Kompaniya A esa $100 ga (deyarli hech
        # narsa pauza qilinmaydi) -- ikkalasi HAM $2 CPL bilan reklama
        # yuritmoqda.
        orchestrator.set_business_rule("cpl_hard_kill_usd", 1.0, company_id=owner_id)
        orchestrator.set_business_rule("cpl_hard_kill_min_spend_usd", 0.0, company_id=owner_id)
        orchestrator.set_business_rule("cpl_hard_kill_usd", 100.0, company_id=company_a)
        orchestrator.set_business_rule("cpl_hard_kill_min_spend_usd", 0.0, company_id=company_a)

        fake_ad = {
            "id": "ad1", "name": "Reklama 1", "status": "ACTIVE",
            "spend": 10.0, "cpl": 2.0, "goal": "LEAD_GENERATION",
            "crm_leads_total": 5, "meta_result": 5, "meta_leads": 5,
        }

        def fake_get_kpis(*args, **kwargs):
            return {"error": None, "rows": [dict(fake_ad)]}

        paused_ids = []

        def fake_set_status(object_id, expected_status, access_token=None):
            paused_ids.append((object_id, expected_status))

        def fake_get_object_status(object_id, access_token=None):
            return {"status": "PAUSED"}

        owner_creds = orchestrator._CplCompanyCreds(id=owner_id, name="Egasi", meta_access_token="tok_o", meta_ad_account_id="act_o")
        company_a_creds = orchestrator._CplCompanyCreds(id=company_a, name="Kompaniya A", meta_access_token="tok_a", meta_ad_account_id="act_a")

        with mock.patch.object(dashboard_data, "get_kpis", side_effect=fake_get_kpis), \
             mock.patch("meta_api.set_status", side_effect=fake_set_status), \
             mock.patch("meta_api.get_object_status", side_effect=fake_get_object_status), \
             mock.patch("meta_api.get_account_structure", return_value={"campaigns": [], "adsets": [], "ads": []}):
            result_owner = orchestrator.enforce_cpl_hard_kill(company=owner_creds)
            result_a = orchestrator.enforce_cpl_hard_kill(company=company_a_creds)

        assert len(result_owner["paused"]) == 1, f"platforma egasi $1 chegara bilan $2 CPL'ni pauza qilishi kerak: {result_owner}"
        assert len(result_a["paused"]) == 0, f"Kompaniya A $100 chegara bilan $2 CPL'ni pauza QILMASLIGI kerak (avval egasining $1 chegarasi ishlatilardi): {result_a}"
    print("OK: enforce_cpl_hard_kill() endi har bir kompaniyaning O'Z Sozlamalardagi CPL chegarasini ishlatadi (avval BARCHA kompaniyaga bitta global chegara qo'llanardi)")


def test_min_sale_amount_is_isolated_per_company():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_modules(os.path.join(tmp, "b3.db"))
        import kpi_bonus
        owner_id = db_module.get_default_company_id()
        company_a = _make_company(db_module, name="Kompaniya A")

        kpi_bonus.set_min_sale_amount(1_000_000, company_id=owner_id)
        assert kpi_bonus.get_min_sale_amount(company_id=owner_id) == 1_000_000
        assert kpi_bonus.get_min_sale_amount(company_id=company_a) == kpi_bonus.MIN_SALE_AMOUNT, (
            "Kompaniya A hali sozlamagan -- platforma egasining 1 000 000'ini emas, "
            "standart qiymatni ko'rishi kerak"
        )
    print("OK: kpi_bonus.get_min_sale_amount/set_min_sale_amount endi HAR BIR kompaniya uchun ALOHIDA")


# ---------------------------------------------------------------------------
# 2) /vazifalar va /vazifa_off -- faqat O'Z kompaniyasining vazifalari
# ---------------------------------------------------------------------------

def test_vazifalar_command_only_shows_own_companys_tasks():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_modules(os.path.join(tmp, "v1.db"))
        company_a = _make_company(db_module, name="Kompaniya A", telegram_group_id="-3001")
        company_b = _make_company(db_module, name="Kompaniya B", telegram_group_id="-3002")

        session = db_module.get_session()
        try:
            session.add(db_module.StandingTask(
                company_id=company_a, chat_id="-3001", object_id="camp_a", object_name="Kompaniya A targeti",
                on_time="09:00", off_time="22:00", is_active=True,
            ))
            session.add(db_module.StandingTask(
                company_id=company_b, chat_id="-3002", object_id="camp_b", object_name="Kompaniya B targeti",
                on_time="10:00", off_time="21:00", is_active=True,
            ))
            session.commit()
        finally:
            session.close()

        sent = []
        with mock.patch.object(app_module, "tg_send", side_effect=lambda cid, text: sent.append((cid, text))):
            app_module.handle_command(-3001, "/vazifalar", [])
            app_module.handle_command(-3002, "/vazifalar", [])

        text_a = next(t for c, t in sent if c == -3001)
        text_b = next(t for c, t in sent if c == -3002)
        assert "Kompaniya A targeti" in text_a and "Kompaniya B targeti" not in text_a, \
            f"Kompaniya A guruhi FAQAT o'z targetini ko'rishi kerak: {text_a}"
        assert "Kompaniya B targeti" in text_b and "Kompaniya A targeti" not in text_b, \
            f"Kompaniya B guruhi FAQAT o'z targetini ko'rishi kerak: {text_b}"
    print("OK: /vazifalar endi FAQAT so'ragan chatning O'Z kompaniyasiga tegishli vazifalarni ko'rsatadi (avval BARCHA kompaniyaning vazifalari har qanday chatga ko'rinardi)")


def test_vazifa_off_cannot_deactivate_another_companys_task():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_modules(os.path.join(tmp, "v2.db"))
        company_a = _make_company(db_module, name="Kompaniya A", telegram_group_id="-4001")
        company_b = _make_company(db_module, name="Kompaniya B", telegram_group_id="-4002")

        session = db_module.get_session()
        try:
            task_a = db_module.StandingTask(
                company_id=company_a, chat_id="-4001", object_id="camp_a", object_name="Kompaniya A targeti",
                on_time="09:00", off_time="22:00", is_active=True,
            )
            session.add(task_a)
            session.commit()
            task_a_id = task_a.id
        finally:
            session.close()

        sent = []
        with mock.patch.object(app_module, "tg_send", side_effect=lambda cid, text: sent.append((cid, text))):
            # Kompaniya B, Kompaniya A'ning vazifa ID'sini TAXMIN qilib
            # bekor qilishga urinadi.
            app_module.handle_command(-4002, "/vazifa_off", [f"T{task_a_id}"])

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                task_a_after = session.get(db_module.StandingTask, task_a_id)
            assert task_a_after.is_active, "Kompaniya B, Kompaniya A'ning vazifasini bekor qila OLMASLIGI kerak"
        finally:
            session.close()
        assert any("topilmadi" in t for _, t in sent), f"Kompaniya B'ga 'topilmadi' javobi kelishi kerak: {sent}"
    print("OK: /vazifa_off endi boshqa kompaniyaning vazifasini ID bo'yicha bekor qilishga yo'l qo'ymaydi (egalik tekshiruvi qo'shildi)")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
