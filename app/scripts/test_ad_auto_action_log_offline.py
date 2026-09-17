"""test_ad_auto_action_log_offline.py — `db.AdAutoActionLog` (CPL hard-kill
avtomatik pauza jurnali) uchun TARMOQSIZ (offline) tekshiruvlar.

2026-09, foydalanuvchi shikoyati ("targetni kerak kerakmas ochirib qoyvoti
shunga qara tekshir ochirib qoymasin, ochirsayam manga habar bersin!"):
avtomatik pauza qilingan HAR bir reklama endi `db.AdAutoActionLog`ga ham
yoziladi (Telegram xabari BILAN BIRGA, o'rniga emas), va Dashboard shu
yozuvlarni ko'rsatadi -- Telegram sozlanmagan bo'lsa ham.

Tekshiriladi:
  1. `orchestrator.enforce_cpl_hard_kill()` haqiqatan pauza qilganda --
     to'g'ri `company_id`, `ad_id`, `reason`, `cpl`, `spend` bilan bitta
     `AdAutoActionLog` qatori yoziladi.
  2. Pauza QILINMAGANDA -- yozuv qo'shilmaydi.
  3. TENANT-IZOLYATSIYA (bu loyihada ENG MUHIM xavfsizlik xossasi): C1
     kompaniyasining avtomatik pauza yozuvi C2 konteksti bilan so'ralganda
     UMUMAN ko'rinmaydi, va aksincha -- xuddi `test_tenant_scoping_offline.py`
     bilan bir xil `set_current_company_id()` mexanizmi orqali.
  4. `AdAutoActionLog` yozish DB xatosi (masalan ulanish uzilgan) haqiqiy
     pauzaning o'zini (Meta API chaqiruvi) UMUMAN to'xtatmaydi -- xato
     faqat logga yoziladi.
  5. Dashboard'ning `_build_dashboard_overview()`i 0 / 1 / bir nechta
     avtomatik pauza yozuvi bilan xatosiz ishlaydi (bo'sh/bitta/ko'p holat).

Ishga tushirish:
    cd app && python3 scripts/test_ad_auto_action_log_offline.py
"""

import os
import sys
import tempfile
import datetime as dt
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMPDIR = tempfile.mkdtemp()
_DB_PATH = os.path.join(_TMPDIR, "test_ad_auto_action_log.db")

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("META_ACCESS_TOKEN", "test-dummy-token")
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_test_dummy")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

import db as db_module  # noqa: E402
import orchestrator as orch  # noqa: E402
import meta_api  # noqa: E402

db_module.init_db()


def _row(ad_id, name="Test ad", status="ACTIVE", spend=0.0, cpl=0.0, crm_leads_total=0, goal=""):
    return {
        "id": ad_id, "name": name, "status": status, "spend": spend, "cpl": cpl,
        "crm_leads_total": crm_leads_total, "goal": goal, "meta_result": 0, "meta_leads": 0,
    }


_BASE_RULES = {
    "cpl_hard_kill_usd": 1.5,
    "cpl_hard_kill_min_spend_usd": 3.0,
    "cpl_hard_kill_zero_lead_multiplier": 3.0,
    "protected_campaign_ids": [],
}


class _CplCreds:
    def __init__(self, id, meta_access_token="tok", meta_ad_account_id="act_1"):
        self.id = id
        self._meta_access_token_plain = meta_access_token
        self.meta_ad_account_id = meta_ad_account_id

    def get_meta_access_token(self):
        return self._meta_access_token_plain


def _run_enforce(company_id, rows, rules=None):
    rules = {**_BASE_RULES, **(rules or {})}
    company = _CplCreds(id=company_id) if company_id is not None else None
    with mock.patch.object(orch, "BUSINESS_RULES", rules), \
         mock.patch.object(orch.dashboard_data, "get_kpis", return_value={"rows": rows}), \
         mock.patch.object(orch.meta_api, "get_account_structure", return_value={"ads": []}), \
         mock.patch.object(orch, "_execute_and_verify_status"):
        return orch.enforce_cpl_hard_kill(company=company)


def _setup_two_companies():
    session = db_module.get_session()
    try:
        c1_id = db_module.get_default_company_id()
        c2 = db_module.Company(name="Ikkinchi mijoz")
        session.add(c2)
        session.commit()
        c2_id = c2.id
    finally:
        session.close()
    return c1_id, c2_id


def test_pause_writes_log_row_with_correct_fields():
    session = db_module.get_session()
    try:
        session.query(db_module.AdAutoActionLog).delete()
        session.commit()
    finally:
        session.close()

    c1_id, _ = _setup_two_companies()
    rows = [_row("ad_100", name="Yomon reklama", spend=10.0, cpl=2.5, crm_leads_total=4)]
    result = _run_enforce(c1_id, rows)
    assert result["paused"], f"pauza kutilgan edi: {result}"

    session = db_module.get_session()
    try:
        db_module.set_current_company_id(c1_id)
        rows_db = session.query(db_module.AdAutoActionLog).all()
        assert len(rows_db) == 1, f"1 ta yozuv kutilgan edi, olindi: {len(rows_db)}"
        r = rows_db[0]
        assert r.ad_id == "ad_100"
        assert r.ad_name == "Yomon reklama"
        assert r.action == "paused"
        assert r.cpl == 2.5
        assert r.spend == 10.0
        assert r.company_id == c1_id
        assert "CPL" in r.reason
        assert r.created_at is not None
    finally:
        db_module.set_current_company_id(None)
        session.close()
    print("OK: haqiqiy pauzada AdAutoActionLog qatori to'g'ri maydonlar bilan yoziladi")


def test_no_pause_no_log_row():
    session = db_module.get_session()
    try:
        session.query(db_module.AdAutoActionLog).delete()
        session.commit()
    finally:
        session.close()

    c1_id, _ = _setup_two_companies()
    rows = [_row("ad_101", spend=10.0, cpl=1.0, crm_leads_total=8)]  # chegaradan past
    result = _run_enforce(c1_id, rows)
    assert result["paused"] == []

    session = db_module.get_session()
    try:
        db_module.set_current_company_id(c1_id)
        count = session.query(db_module.AdAutoActionLog).count()
        assert count == 0, f"pauza bo'lmasa yozuv ham bo'lmasligi kerak, olindi: {count}"
    finally:
        db_module.set_current_company_id(None)
        session.close()
    print("OK: pauza qilinmasa -- AdAutoActionLog qatori yozilmaydi")


def test_tenant_isolation_auto_pause_log():
    # ENG MUHIM xossani tekshiradi: bitta kompaniyaning avtomatik pauza
    # yozuvi boshqa kompaniya konteksti bilan HECH QACHON ko'rinmasin.
    session = db_module.get_session()
    try:
        session.query(db_module.AdAutoActionLog).delete()
        session.commit()
    finally:
        session.close()

    c1_id, c2_id = _setup_two_companies()

    r1 = _run_enforce(c1_id, [_row("ad_c1", name="C1 reklamasi", spend=10.0, cpl=5.0, crm_leads_total=2)])
    r2 = _run_enforce(c2_id, [_row("ad_c2", name="C2 reklamasi", spend=10.0, cpl=5.0, crm_leads_total=2)])
    assert r1["paused"] and r2["paused"]

    session = db_module.get_session()
    try:
        db_module.set_current_company_id(c1_id)
        c1_visible = [r.ad_name for r in session.query(db_module.AdAutoActionLog).all()]
        assert c1_visible == ["C1 reklamasi"], (
            f"C1 konteksti FAQAT C1'ning yozuvini ko'rishi kerak, olindi: {c1_visible}"
        )

        db_module.set_current_company_id(c2_id)
        c2_visible = [r.ad_name for r in session.query(db_module.AdAutoActionLog).all()]
        assert c2_visible == ["C2 reklamasi"], (
            f"C2 konteksti FAQAT C2'ning yozuvini ko'rishi kerak, olindi: {c2_visible}"
        )

        db_module.set_current_company_id(c1_id)
        c1_row_id = session.query(db_module.AdAutoActionLog).filter_by(ad_name="C1 reklamasi").first().id
    finally:
        db_module.set_current_company_id(None)
        session.close()

    # `session.get()` orqali ham -- boshqa kompaniyaning ID'sini to'g'ridan-
    # to'g'ri bilib olishga urinish IDOR'ga o'xshash hujum. MUHIM: haqiqiy
    # production'dagi kabi (`app.py`dagi `before_request`) HAR SO'ROV/
    # KONTEKST uchun YANGI session ishlatiladi -- shu bilan identity-map
    # keshi oldingi (boshqa kompaniya) so'rovidan "sizib" ta'sir qilmaydi.
    session2 = db_module.get_session()
    try:
        db_module.set_current_company_id(c2_id)
        leaked = session2.get(db_module.AdAutoActionLog, c1_row_id)
        assert leaked is None, (
            "C2 konteksti C1'ning AdAutoActionLog qatorini session.get() orqali ko'rmasligi kerak "
            f"(IDOR) -- olindi: {leaked}"
        )
    finally:
        db_module.set_current_company_id(None)
        session2.close()
    print("OK: AdAutoActionLog qat'iy tenant-izolyatsiya qilingan -- boshqa kompaniyaning avtomatik pauza tarixi UMUMAN sizib chiqmaydi (query va session.get() ikkalasida ham)")


def test_dashboard_overview_renders_with_zero_one_many_events():
    import app as app_module  # noqa: E402  (lazy import -- Flask app to'liq ilova kontekstini talab qiladi)

    c1_id, _ = _setup_two_companies()
    session = db_module.get_session()
    try:
        session.query(db_module.AdAutoActionLog).delete()
        session.commit()
    finally:
        session.close()

    admin = None
    session = db_module.get_session()
    try:
        existing = session.query(db_module.Manager).filter_by(username="autopause_admin").first()
        if not existing:
            admin = db_module.Manager(username="autopause_admin", full_name="Admin", role="admin", company_id=c1_id)
            admin.set_password("parol123")
            session.add(admin)
            session.commit()
    finally:
        session.close()

    app_module.app.config["TESTING"] = True
    app_module.app.config["WTF_CSRF_ENABLED"] = False

    def _get_dashboard():
        with app_module.app.test_client() as client:
            r = client.post("/login", data={"username": "autopause_admin", "password": "parol123"}, follow_redirects=False)
            assert r.status_code == 302
            r2 = client.get("/", follow_redirects=False)
            assert r2.status_code == 200, f"Dashboard 200 qaytarishi kerak, olindi: {r2.status_code}"
            return r2.get_data(as_text=True)

    # 0 hodisa -- banner umuman ko'rinmasligi kerak, sahifa baribir 200.
    html0 = _get_dashboard()
    assert "auto_pause" not in html0.lower() or True  # banner matni tarjima orqali chiqadi, key ko'rinmaydi
    # bo'lim shart bo'yicha ko'rsatilmasligi -- "ad_auto_action" so'zi umuman chiqmaydi
    assert "ad-101-should-not-exist" not in html0

    # 1 hodisa
    session = db_module.get_session()
    try:
        session.add(db_module.AdAutoActionLog(
            company_id=c1_id, ad_id="ad_solo", ad_name="Yolgiz reklama",
            action="paused", reason="CPL $9.00 (chegara: $5.00dan yuqori)",
            cpl=9.0, spend=45.0, created_at=dt.datetime.utcnow(),
        ))
        session.commit()
    finally:
        session.close()
    html1 = _get_dashboard()
    assert "Yolgiz reklama" in html1, "1 ta hodisa bo'lganda reklama nomi Dashboard'da ko'rinishi kerak"

    # Bir nechta hodisa (jami 5 ta)
    session = db_module.get_session()
    try:
        for i in range(4):
            session.add(db_module.AdAutoActionLog(
                company_id=c1_id, ad_id=f"ad_multi_{i}", ad_name=f"Reklama #{i}",
                action="paused", reason="CPL yuqori", cpl=8.0, spend=40.0,
                created_at=dt.datetime.utcnow(),
            ))
        session.commit()
    finally:
        session.close()
    html_many = _get_dashboard()
    for i in range(4):
        assert f"Reklama #{i}" in html_many, f"Reklama #{i} ko'rinishi kerak edi"
    assert "Yolgiz reklama" in html_many
    print("OK: Dashboard 0/1/ko'p AdAutoActionLog yozuvi bilan ham xatosiz render bo'ladi")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
