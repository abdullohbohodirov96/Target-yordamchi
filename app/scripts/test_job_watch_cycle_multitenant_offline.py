"""test_job_watch_cycle_multitenant_offline.py -- 2026-09, foydalanuvchi
so'rovi: "barchada bu narsa bo'lsin, lekin ulanayotganda, ya'ni dostuplar
olinsin, yoqsin o'zi odam. agar yoqsa, o'zi o'chirib pauzalarni berib
yursin. agar yoqilmasa, yoqmasin o'zi. va shuni to'liq to'g'irlab, bir
ikkita xatolar ham chiqyapti, o'chirmayapti vaqtida."

Bu fayl uchta narsani tekshiradi:
  1. `Company.is_auto_watch_enabled()` -- NULL bo'lsa platforma egasi
     (id=1) uchun True, boshqa HAR BIR kompaniya uchun False (opt-in).
  2. `scheduler.job_watch_cycle()` -- endi HAR BIR (Meta ulagan, guruhini
     sozlagan VA funksiyani o'zi yoqqan) kompaniya uchun ALOHIDA, O'Z Meta
     hisobi bilan ishlaydi; yoqmagan kompaniya butunlay o'tkazib
     yuboriladi; bitta kompaniyaning xatosi boshqasini to'xtatmaydi.
  3. `scheduler.job_standing_tasks()` -- soatlik jadval (on/off) vazifasi
     endi o'zining kompaniyasiga tegishli Meta token bilan bajariladi,
     global (platforma egasi) token bilan EMAS -- foydalanuvchi
     shikoyatining ("xatolar chiqyapti, o'chirmayapti vaqtida") aynan
     ehtimoliy sababi edi.

Ishga tushirish:
    cd app && python3 scripts/test_job_watch_cycle_multitenant_offline.py
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
    import scheduler as scheduler_module
    return db_module, scheduler_module


def _make_company(db_module, *, name, telegram_group_id=None, meta_ad_account_id=None,
                   meta_access_token=None, auto_watch_enabled=None, is_active=True):
    session = db_module.get_session()
    try:
        c = db_module.Company(
            name=name, telegram_group_id=telegram_group_id, is_active=is_active,
            meta_ad_account_id=meta_ad_account_id, auto_watch_enabled=auto_watch_enabled,
        )
        if meta_access_token:
            c.set_meta_access_token(meta_access_token)
        session.add(c)
        session.commit()
        return c.id
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 1) Company.is_auto_watch_enabled() -- NULL-holat semantikasi
# ---------------------------------------------------------------------------

def _force_null_auto_watch(db_module, company_id):
    """`_migrate_add_missing_columns()` orqali ESKI (migratsiyadan oldingi)
    qatorlarga bu ustun HAR DOIM NULL bo'lib qo'shiladi -- yangi ORM orqali
    yaratilgan qator esa modeldagi `default=False`ni oladi. Shu farqni aniq
    simulyatsiya qilish uchun qatorni qo'lda NULL'ga qaytaramiz (xuddi
    "eski, hali bu funksiyani bilmaydigan" qatordek)."""
    session = db_module.get_session()
    try:
        with db_module.unscoped():
            c = session.get(db_module.Company, company_id)
            c.auto_watch_enabled = None
            session.commit()
    finally:
        session.close()


def test_owner_defaults_to_enabled_when_null():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, _sched = _fresh_modules(os.path.join(tmp, "a1.db"))
        owner_id = db_module.get_default_company_id()
        _force_null_auto_watch(db_module, owner_id)
        session = db_module.get_session()
        try:
            owner = session.get(db_module.Company, owner_id)
            assert owner.auto_watch_enabled is None
            assert owner.is_auto_watch_enabled() is True, (
                "platforma egasi (id=1) uchun NULL (eski, migratsiya qilingan qator) "
                "orqaga moslik sifatida True bo'lishi kerak"
            )
        finally:
            session.close()
    print("OK: Company.is_auto_watch_enabled() -- platforma egasi uchun NULL = True (orqaga moslik)")


def test_other_company_defaults_to_disabled_when_null():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, _sched = _fresh_modules(os.path.join(tmp, "a2.db"))
        company_a = _make_company(db_module, name="Kompaniya A")
        _force_null_auto_watch(db_module, company_a)
        session = db_module.get_session()
        try:
            with db_module.unscoped():
                c = session.get(db_module.Company, company_a)
            assert c.auto_watch_enabled is None
            assert c.is_auto_watch_enabled() is False, (
                "yangi (boshqa) kompaniya uchun NULL = False bo'lishi kerak -- opt-in, admin o'zi yoqishi kerak"
            )
        finally:
            session.close()
    print("OK: Company.is_auto_watch_enabled() -- boshqa har bir kompaniya uchun NULL = False (opt-in, standart o'chiq)")


def test_fresh_company_created_via_orm_defaults_to_disabled():
    """Yangi (ORM orqali, `_make_company` kabi) yaratilgan kompaniya -- eski
    migratsiya qilingan qatordan farqli o'laroq -- modeldagi `default=False`ni
    oladi (NULL emas), lekin natija baribir "o'chiq" bo'lishi kerak."""
    with tempfile.TemporaryDirectory() as tmp:
        db_module, _sched = _fresh_modules(os.path.join(tmp, "a4.db"))
        company_a = _make_company(db_module, name="Yangi kompaniya")
        session = db_module.get_session()
        try:
            with db_module.unscoped():
                c = session.get(db_module.Company, company_a)
            assert c.is_auto_watch_enabled() is False
        finally:
            session.close()
    print("OK: yangi ro'yxatdan o'tgan kompaniya ham standart holatda o'chiq (opt-in)")


def test_explicit_true_false_are_respected():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, _sched = _fresh_modules(os.path.join(tmp, "a3.db"))
        company_on = _make_company(db_module, name="Yoqilgan", auto_watch_enabled=True)
        company_off = _make_company(db_module, name="O'chirilgan", auto_watch_enabled=False)
        session = db_module.get_session()
        try:
            with db_module.unscoped():
                c_on = session.get(db_module.Company, company_on)
                c_off = session.get(db_module.Company, company_off)
            assert c_on.is_auto_watch_enabled() is True
            assert c_off.is_auto_watch_enabled() is False
        finally:
            session.close()
    print("OK: admin aniq yoqqan/o'chirgan qiymat har doim to'g'ridan-to'g'ri hurmat qilinadi")


# ---------------------------------------------------------------------------
# 2) scheduler.job_watch_cycle() -- ko'p-kompaniya, opt-in, izolyatsiya
# ---------------------------------------------------------------------------

def test_job_watch_cycle_skips_company_that_has_not_opted_in():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, scheduler_module = _fresh_modules(os.path.join(tmp, "b1.db"))
        import orchestrator
        _make_company(
            db_module, name="Yoqmagan kompaniya", telegram_group_id="-6001",
            meta_ad_account_id="act_b", meta_access_token="tok_b",
            auto_watch_enabled=False,
        )

        calls = []

        def fake_run_daily_cron_report(dry_run=False, company=None):
            calls.append(company.id if company else "owner")
            return None

        with mock.patch.object(orchestrator, "run_daily_cron_report", side_effect=fake_run_daily_cron_report), \
             mock.patch.object(scheduler_module, "_tg_send", return_value={"ok": True, "error": None}):
            results = scheduler_module.job_watch_cycle()

        assert calls == ["owner"], f"yoqmagan kompaniya uchun umuman chaqirilmasligi kerak edi: {calls}"
        assert "diqqatga loyiq narsa yo'q" in results["owner"]
    print("OK: job_watch_cycle() -- auto_watch_enabled=False bo'lgan kompaniya butunlay o'tkazib yuboriladi")


def test_job_watch_cycle_runs_opted_in_company_with_own_credentials_and_own_group():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, scheduler_module = _fresh_modules(os.path.join(tmp, "b2.db"))
        import orchestrator
        company_a = _make_company(
            db_module, name="Yoqqan kompaniya", telegram_group_id="-6002",
            meta_ad_account_id="act_a", meta_access_token="tok_a",
            auto_watch_enabled=True,
        )

        seen_companies = []

        def fake_run_daily_cron_report(dry_run=False, company=None):
            if company is None:
                return None  # owner -- diqqatga loyiq narsa yo'q
            seen_companies.append({
                "id": company.id,
                "token": company.get_meta_access_token(),
                "ad_account_id": company.meta_ad_account_id,
            })
            return f"'{company.name}' uchun audit natijasi"

        sent = []

        def fake_tg_send(chat_id, text):
            sent.append((chat_id, text))
            return {"ok": True, "error": None}

        with mock.patch.object(orchestrator, "run_daily_cron_report", side_effect=fake_run_daily_cron_report), \
             mock.patch.object(scheduler_module, "_tg_send", side_effect=fake_tg_send):
            results = scheduler_module.job_watch_cycle()

        assert len(seen_companies) == 1
        assert seen_companies[0]["id"] == company_a
        assert seen_companies[0]["token"] == "tok_a"
        assert seen_companies[0]["ad_account_id"] == "act_a"

        company_sent = [t for cid, t in sent if cid == -6002]
        assert len(company_sent) == 1 and "Yoqqan kompaniya" in company_sent[0]
        assert "yuborildi" in results[company_a]
    print("OK: job_watch_cycle() -- yoqqan kompaniya O'Z Meta hisobi bilan tekshiriladi va natija FAQAT O'Z Telegram guruhiga boradi")


def test_job_watch_cycle_one_companys_failure_does_not_block_another():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, scheduler_module = _fresh_modules(os.path.join(tmp, "b3.db"))
        import orchestrator
        company_bad = _make_company(
            db_module, name="Xato kompaniya", telegram_group_id="-6003",
            meta_ad_account_id="act_bad", meta_access_token="tok_bad",
            auto_watch_enabled=True,
        )
        company_good = _make_company(
            db_module, name="Yaxshi kompaniya", telegram_group_id="-6004",
            meta_ad_account_id="act_good", meta_access_token="tok_good",
            auto_watch_enabled=True,
        )

        def fake_run_daily_cron_report(dry_run=False, company=None):
            if company is None:
                return None
            if company.id == company_bad:
                raise RuntimeError(f"https://graph.facebook.com/x?access_token={company.get_meta_access_token()}")
            return "hammasi joyida emas -- diqqat kerak"

        sent = []

        def fake_tg_send(chat_id, text):
            sent.append((chat_id, text))
            return {"ok": True, "error": None}

        with mock.patch.object(orchestrator, "run_daily_cron_report", side_effect=fake_run_daily_cron_report), \
             mock.patch.object(scheduler_module, "_tg_send", side_effect=fake_tg_send):
            results = scheduler_module.job_watch_cycle()

        assert "xato" in results[company_bad]
        assert "yuborildi" in results[company_good]
        bad_texts = "".join(t for cid, t in sent if cid == -6003)
        assert "tok_bad" not in bad_texts and "access_token=" not in bad_texts, (
            f"xato xabarida xom (token-tashuvchi) matn ko'rinmasligi kerak (safe_error_message): {bad_texts}"
        )
        good_texts = [t for cid, t in sent if cid == -6004]
        assert good_texts and "hammasi joyida emas" in good_texts[0]
    print("OK: job_watch_cycle() -- bitta kompaniyaning xatosi boshqasini to'xtatmaydi, xato xabari xavfsiz (token sizmaydi)")


def test_job_watch_cycle_ignores_company_without_telegram_group():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, scheduler_module = _fresh_modules(os.path.join(tmp, "b4.db"))
        import orchestrator
        _make_company(
            db_module, name="Guruhsiz kompaniya", telegram_group_id=None,
            meta_ad_account_id="act_c", meta_access_token="tok_c",
            auto_watch_enabled=True,
        )

        calls = []

        def fake_run_daily_cron_report(dry_run=False, company=None):
            calls.append(company.id if company else "owner")
            return None

        with mock.patch.object(orchestrator, "run_daily_cron_report", side_effect=fake_run_daily_cron_report), \
             mock.patch.object(scheduler_module, "_tg_send", return_value={"ok": True, "error": None}):
            scheduler_module.job_watch_cycle()

        assert calls == ["owner"], (
            f"telegram_group_id sozlanmagan kompaniya uchun chaqirilmasligi kerak (natijani qayerga yuborishni bilmaydi): {calls}"
        )
    print("OK: job_watch_cycle() -- Telegram guruhini hali sozlamagan kompaniya (auto_watch yoqqan bo'lsa ham) o'tkazib yuboriladi")


# ---------------------------------------------------------------------------
# 3) scheduler.job_standing_tasks() -- har bir vazifa O'Z kompaniya tokeni bilan
# ---------------------------------------------------------------------------

def test_job_standing_tasks_uses_each_companys_own_token_not_global():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, scheduler_module = _fresh_modules(os.path.join(tmp, "c1.db"))
        import meta_api
        os.environ["ACCESS_TOKEN"] = "GLOBAL_OWNER_TOKEN"

        company_a = _make_company(
            db_module, name="Kompaniya A", telegram_group_id="-7001", meta_access_token="tok_company_a",
        )

        session = db_module.get_session()
        try:
            session.add(db_module.StandingTask(
                company_id=company_a, chat_id="-7001", object_id="camp_a",
                object_name="Kompaniya A targeti", on_time="00:00", off_time="00:00",
                is_active=True, last_desired_state="off",
            ))
            session.commit()
        finally:
            session.close()

        seen_tokens = []

        def fake_activate_object(object_id, access_token=None):
            seen_tokens.append(access_token)

        with mock.patch.object(meta_api, "activate_object", side_effect=fake_activate_object), \
             mock.patch.object(scheduler_module, "_tg_send", return_value={"ok": True, "error": None}):
            scheduler_module.job_standing_tasks()

        assert seen_tokens == ["tok_company_a"], (
            f"Kompaniya A'ning vazifasi O'ZINING tokeni bilan bajarilishi kerak edi, "
            f"global ENV token bilan EMAS: {seen_tokens}"
        )
    print("OK: job_standing_tasks() -- har bir vazifa O'Z kompaniyasining Meta tokeni bilan bajariladi (avval doim global ENV token ishlatilardi)")


def test_job_standing_tasks_error_is_recorded_and_safe():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, scheduler_module = _fresh_modules(os.path.join(tmp, "c2.db"))
        import meta_api
        company_a = _make_company(
            db_module, name="Kompaniya A", telegram_group_id="-7002", meta_access_token="tok_company_a",
        )
        session = db_module.get_session()
        try:
            task = db_module.StandingTask(
                company_id=company_a, chat_id="-7002", object_id="camp_a",
                object_name="Kompaniya A targeti", on_time="00:00", off_time="00:00",
                is_active=True, last_desired_state="off",
            )
            session.add(task)
            session.commit()
            task_id = task.id
        finally:
            session.close()

        def fake_activate_object(object_id, access_token=None):
            raise meta_api.MetaAPIError({"message": "Obyekt topilmadi"})

        with mock.patch.object(meta_api, "activate_object", side_effect=fake_activate_object), \
             mock.patch.object(scheduler_module, "_tg_send", return_value={"ok": True, "error": None}):
            result = scheduler_module.job_standing_tasks()

        assert "xato=1" in result
        session = db_module.get_session()
        try:
            with db_module.unscoped():
                t = session.get(db_module.StandingTask, task_id)
            assert t.last_error, "xato Company vazifasiga yozilishi kerak (keyingi tekshiruvda ko'rinishi uchun)"
            assert t.last_desired_state != "on", "xato bo'lganda holat 'muvaffaqiyatli o'zgardi' deb belgilanmasligi kerak"
        finally:
            session.close()
    print("OK: job_standing_tasks() -- xato bo'lsa vazifaga yoziladi (keyingi safar ham qayta urinilaveradi), holat noto'g'ri 'muvaffaqiyatli' deb belgilanmaydi")


# ---------------------------------------------------------------------------
# 4) job_watch_cycle() -- token muddati tugagan xato (kod 190) endi ASOSIY
#    (CAPI'siz) Meta Ads yo'lida ham aniqlanadi -- 2026-09, Item J xavfsizlik
#    auditi, 🟠 YUQORI 10-band ("Token muddati tugashi faqat CAPI orqali
#    aniqlanadi"). Ilgari bu yerda MetaAPIError kod=190 bo'lsa ham xuddi
#    boshqa har qanday xatodek umumiy "⚠️ ... xatolik" xabari yuborilardi,
#    `meta_integration_status` esa hech qachon "reauth_required"ga
#    o'zgarmasdi -- foydalanuvchi `/connect-accounts` sahifasidagi "qayta
#    ulash" bannerini hech qachon ko'rmasdi.
# ---------------------------------------------------------------------------

def test_job_watch_cycle_marks_company_reauth_required_on_token_expired():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, scheduler_module = _fresh_modules(os.path.join(tmp, "d1.db"))
        import orchestrator
        import meta_api
        company_a = _make_company(
            db_module, name="Tokeni eskirgan kompaniya", telegram_group_id="-6005",
            meta_ad_account_id="act_expired", meta_access_token="tok_expired",
            auto_watch_enabled=True,
        )

        def fake_run_daily_cron_report(dry_run=False, company=None):
            if company is None:
                return None
            raise meta_api.MetaAPIError({"code": 190, "message": "Error validating access token"})

        sent = []

        def fake_tg_send(chat_id, text):
            sent.append((chat_id, text))
            return {"ok": True, "error": None}

        with mock.patch.object(orchestrator, "run_daily_cron_report", side_effect=fake_run_daily_cron_report), \
             mock.patch.object(scheduler_module, "_tg_send", side_effect=fake_tg_send):
            results = scheduler_module.job_watch_cycle()

        assert "xato" in results[company_a]
        session = db_module.get_session()
        try:
            with db_module.unscoped():
                c = session.get(db_module.Company, company_a)
            assert c.meta_integration_status == "reauth_required", (
                "kod 190 (token eskirgan) MetaAPIError kelganda kompaniya "
                "'reauth_required' deb belgilanishi kerak edi"
            )
        finally:
            session.close()
        company_texts = [t for cid, t in sent if cid == -6005]
        assert company_texts and "qayta ulan" in company_texts[0].lower(), (
            f"foydalanuvchiga umumiy xato emas, ANIQ 'qayta ulaning' xabari ko'rsatilishi kerak: {company_texts}"
        )
    print("OK: job_watch_cycle() -- kompaniya uchun MetaAPIError(kod=190) kelsa, meta_integration_status='reauth_required' bo'ladi va aniq 'qayta ulaning' xabari yuboriladi")


def test_job_watch_cycle_does_not_mark_reauth_required_on_other_errors():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, scheduler_module = _fresh_modules(os.path.join(tmp, "d2.db"))
        import orchestrator
        import meta_api
        company_a = _make_company(
            db_module, name="Vaqtinchalik xato kompaniya", telegram_group_id="-6006",
            meta_ad_account_id="act_x", meta_access_token="tok_x",
            auto_watch_enabled=True,
        )

        def fake_run_daily_cron_report(dry_run=False, company=None):
            if company is None:
                return None
            raise meta_api.MetaAPIError({"code": 1, "message": "Unknown error"})

        with mock.patch.object(orchestrator, "run_daily_cron_report", side_effect=fake_run_daily_cron_report), \
             mock.patch.object(scheduler_module, "_tg_send", return_value={"ok": True, "error": None}):
            scheduler_module.job_watch_cycle()

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                c = session.get(db_module.Company, company_a)
            assert c.meta_integration_status != "reauth_required", (
                "token eskirishi bilan bog'liq BO'LMAGAN (kod != 190) MetaAPIError "
                "kompaniyani noto'g'ri ravishda 'reauth_required' deb belgilamasligi kerak"
            )
        finally:
            session.close()
    print("OK: job_watch_cycle() -- kod 190 BO'LMAGAN MetaAPIError kompaniyani 'reauth_required' deb noto'g'ri belgilamaydi")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
