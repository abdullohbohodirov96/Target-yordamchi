"""test_meta_integration_v2_offline.py — 2026-09, "production-ready Meta
Ads + Conversions API integration" so'rovi bo'yicha YANGI qatlamlarni
TARMOQSIZ (offline) tekshiradi: token shifrlash (Company metodlari
orqali), Advanced/Manual CAPI saqlash formasi (tekshiruvdan o'tmasa rad
etilishi), `meta_events.py` dispatch qatlami (MetaEventLog yozuvlari +
Meta xatosi chaqiruvchiga otilib ketmasligi), disconnect oqimi, va
Advanced/Manual CAPI ma'lumotlarining kompaniyalar orasida SIZIB
chiqmasligi (cross-tenant isolation).

Ishga tushirish:
    cd app && python3 scripts/test_meta_integration_v2_offline.py
"""

import os
import sys
import tempfile
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMPDIR = tempfile.mkdtemp()
_DB_PATH = os.path.join(_TMPDIR, "test_meta_v2.db")

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("META_ACCESS_TOKEN", "test-dummy-token")
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_test_dummy")
os.environ.setdefault("META_PAGE_ID", "page_test_dummy")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402
import meta_api  # noqa: E402
import meta_events  # noqa: E402

app_module.app.config["TESTING"] = True
db_module.init_db()

client = app_module.app.test_client()
failures = []


def check(name, cond):
    print(("OK  " if cond else "FAIL") + " " + name)
    if not cond:
        failures.append(name)


def _signup(company_name, admin_username, plan="business"):
    return client.post("/signup", data={
        "company_name": company_name, "admin_username": admin_username,
        "admin_full_name": "", "email": "", "plan": plan,
        "password": "parol123456", "password2": "parol123456",
    }, follow_redirects=True)


def _login(username):
    return client.post("/login", data={"username": username, "password": "parol123456"}, follow_redirects=True)


# --- 1. Token shifrlash round-trip (Company metodlari) ---
def test_token_encryption_roundtrip():
    session = db_module.get_session()
    try:
        c = db_module.Company(name="Shifrlash MChJ")
        c.set_meta_access_token("secret-oauth-token-xyz")
        c.set_meta_capi_token("secret-capi-token-xyz")
        assert c.meta_access_token != "secret-oauth-token-xyz"
        assert c.meta_capi_access_token != "secret-capi-token-xyz"
        assert c.get_meta_access_token() == "secret-oauth-token-xyz"
        assert c.get_meta_capi_token() == "secret-capi-token-xyz"
        # None/bo'sh qiymatlar bilan ham ishlashi kerak
        c.set_meta_access_token(None)
        assert c.meta_access_token is None
        assert c.get_meta_access_token() is None
        session.add(c)
        session.commit()
        c2 = session.get(db_module.Company, c.id)
        assert c2.get_meta_capi_token() == "secret-capi-token-xyz"
    finally:
        session.rollback()
        session.close()
    print("OK: Company.set_meta_access_token/get_meta_access_token va set_meta_capi_token/get_meta_capi_token to'g'ri shifrlaydi/ochadi")


# --- 2. Manual CAPI saqlash: tekshiruvdan o'tmasa rad etiladi ---
def test_manual_capi_rejects_invalid_credentials_before_saving():
    _signup("Manual Rad MChJ", "manual_rad_admin", plan="business")
    _login("manual_rad_admin")

    real_verify = meta_api.verify_dataset_credentials
    meta_api.verify_dataset_credentials = lambda dataset_id, token: (_ for _ in ()).throw(
        meta_api.MetaAPIError({"message": "Invalid parameter", "type": "OAuthException", "code": 100})
    )
    try:
        resp = client.post("/connect-accounts/meta/manual", data={
            "dataset_id": "not_a_real_dataset", "capi_access_token": "fake_token_123",
        }, follow_redirects=True)
        html = resp.get_data(as_text=True)
        check("noto'g'ri Dataset/token bo'lsa xato ko'rsatiladi", "Invalid parameter" in html)

        session = db_module.get_session()
        try:
            c = session.query(db_module.Company).filter_by(name="Manual Rad MChJ").first()
            check("tekshiruvdan o'tmagan qiymat SAQLANMAYDI", c.meta_capi_dataset_id is None and c.meta_capi_access_token is None)
        finally:
            session.close()
    finally:
        meta_api.verify_dataset_credentials = real_verify
        client.get("/logout")


# --- 3. Manual CAPI saqlash: to'g'ri bo'lsa shifrlanib saqlanadi, plaintext hech qachon HTML'da ko'rinmaydi ---
def test_manual_capi_accepts_and_encrypts_valid_credentials():
    _signup("Manual OK MChJ", "manual_ok_admin", plan="business")
    _login("manual_ok_admin")

    real_verify = meta_api.verify_dataset_credentials
    meta_api.verify_dataset_credentials = lambda dataset_id, token: {"id": dataset_id, "name": "Haqiqiy Dataset"}
    try:
        resp = client.post("/connect-accounts/meta/manual", data={
            "dataset_id": "1234567890", "capi_access_token": "SUPER-SECRET-SYSTEM-USER-TOKEN",
        }, follow_redirects=True)
        html = resp.get_data(as_text=True)
        check("muvaffaqiyatli saqlash xabari ko'rsatiladi", "saqlandi" in html)
        check("HAQIQIY token HTML sahifada UMUMAN ko'rinmaydi", "SUPER-SECRET-SYSTEM-USER-TOKEN" not in html)

        session = db_module.get_session()
        try:
            c = session.query(db_module.Company).filter_by(name="Manual OK MChJ").first()
            check("dataset_id saqlangan", c.meta_capi_dataset_id == "1234567890")
            check("token BAZADA shifrlangan holda (plaintext emas)", c.meta_capi_access_token != "SUPER-SECRET-SYSTEM-USER-TOKEN")
            check("get_meta_capi_token() to'g'ri ochadi", c.get_meta_capi_token() == "SUPER-SECRET-SYSTEM-USER-TOKEN")
        finally:
            session.close()

        settings_html = client.get("/connect-accounts").get_data(as_text=True)
        check("Sozlash sahifasida ham HAQIQIY token ko'rinmaydi", "SUPER-SECRET-SYSTEM-USER-TOKEN" not in settings_html)
        check("Sozlash sahifasida 'saqlangan' maskasi ko'rsatiladi", "saqlangan" in settings_html)
    finally:
        meta_api.verify_dataset_credentials = real_verify
        client.get("/logout")


# --- 4. meta_events dispatch: MetaEventLog yozadi, Meta xatosi otilib ketmaydi ---
def test_dispatch_writes_event_log_and_never_raises():
    session = db_module.get_session()
    try:
        company = db_module.Company(name="Dispatch MChJ", meta_pixel_id="pixel_disp")
        company.set_meta_access_token("disp-token")
        company.meta_integration_status = "connected"
        session.add(company)
        session.commit()
        lead = db_module.Lead(company_id=company.id, full_name="Test Lead", phone="+998901234567", status="new")
        session.add(lead)
        session.commit()
        lead_id, company_id = lead.id, company.id
    finally:
        session.close()

    # 4a. Muvaffaqiyatli yuborish -- "sent" statusli MetaEventLog yozuvi
    real_send = meta_api.send_conversion_event
    meta_api.send_conversion_event = lambda *a, **kw: {"events_received": 1, "fbtrace_id": "trace123"}
    try:
        session = db_module.get_session()
        try:
            lead = session.get(db_module.Lead, lead_id)
            meta_events.dispatch_lead_event(session, lead)
            logs = session.query(db_module.MetaEventLog).filter_by(lead_id=lead_id, event_name="Lead").all()
            check("muvaffaqiyatli dispatch MetaEventLog yozuvi yaratadi", len(logs) == 1)
            check("status='sent' bilan yoziladi", logs[0].status == "sent" if logs else False)
            check("hech qanday token MetaEventLog ustunlarida saqlanmaydi", not any(
                "disp-token" in str(getattr(logs[0], col.name, "")) for col in db_module.MetaEventLog.__table__.columns
            ) if logs else True)
        finally:
            session.close()
    finally:
        meta_api.send_conversion_event = real_send

    # 4b. Meta xatosi (masalan token muddati tugagan, kod 190) -- chaqiruvchiga OTILMAYDI, status reauth_required'ga o'tadi
    def _raise_expired(*a, **kw):
        raise meta_api.MetaAPIError({"message": "Error validating access token", "type": "OAuthException", "code": 190})

    meta_api.send_conversion_event = _raise_expired
    try:
        session = db_module.get_session()
        try:
            lead = session.get(db_module.Lead, lead_id)
            try:
                meta_events.dispatch_qualified_lead_event(session, lead)
                raised = False
            except Exception:
                raised = True
            check("Meta xatosi CRM oqimiga (chaqiruvchiga) otilib ketmaydi", not raised)

            c = session.get(db_module.Company, company_id)
            check("kod 190 kelganda status 'reauth_required'ga o'tadi", c.meta_integration_status == "reauth_required")

            failed_logs = session.query(db_module.MetaEventLog).filter_by(lead_id=lead_id, event_name="QualifiedLead").all()
            check("muvaffaqiyatsiz urinish ham MetaEventLog'ga yoziladi ('failed')", len(failed_logs) == 1 and failed_logs[0].status == "failed")
            check("xato xabari xavfsiz (tokensiz) shaklda saqlanadi", failed_logs[0].safe_error_message and "disp-token" not in failed_logs[0].safe_error_message)
        finally:
            session.close()
    finally:
        meta_api.send_conversion_event = real_send

    print("OK: meta_events.dispatch_* har doim MetaEventLog yozadi, tokenni hech qachon log'ga yozmaydi, va Meta xatosi CRM oqimini hech qachon buzmaydi (190-kod reauth_required'ni ishga tushiradi)")


# --- 5. Disconnect: barcha maydonlarni tozalaydi, keyingi dispatch jim o'tkazib yuboriladi ---
def test_disconnect_clears_fields_and_stops_future_dispatch():
    _signup("Disconnect MChJ", "disconnect_admin", plan="business")
    _login("disconnect_admin")

    session = db_module.get_session()
    try:
        c = session.query(db_module.Company).filter_by(name="Disconnect MChJ").first()
        company_id = c.id
        c.set_meta_access_token("to-be-revoked-token")
        c.meta_pixel_id = "pixel_to_revoke"
        c.meta_ad_account_id = "act_to_revoke"
        c.meta_integration_status = "connected"
        session.commit()
        lead = db_module.Lead(company_id=c.id, full_name="Disc Lead", phone="+998901112233", status="new")
        session.add(lead)
        session.commit()
        lead_id = lead.id
    finally:
        session.close()

    real_revoke = meta_api.oauth_revoke
    meta_api.oauth_revoke = lambda token: True
    try:
        resp = client.post("/connect-accounts/meta/disconnect", follow_redirects=True)
        check("disconnect muvaffaqiyatli xabar qaytaradi", "uzildi" in resp.get_data(as_text=True))
    finally:
        meta_api.oauth_revoke = real_revoke

    session = db_module.get_session()
    try:
        c = session.get(db_module.Company, company_id)
        check("meta_access_token tozalangan", c.meta_access_token is None)
        check("meta_pixel_id tozalangan", c.meta_pixel_id is None)
        check("meta_ad_account_id tozalangan", c.meta_ad_account_id is None)
        check("status='disconnected'ga qaytadi", c.meta_integration_status == "disconnected")
    finally:
        session.close()

    real_send = meta_api.send_conversion_event
    called = {"count": 0}
    def _track(*a, **kw):
        called["count"] += 1
        return {"events_received": 1}
    meta_api.send_conversion_event = _track
    try:
        session = db_module.get_session()
        try:
            lead = session.get(db_module.Lead, lead_id)
            meta_events.dispatch_lead_event(session, lead)
        finally:
            session.close()
        check("disconnect qilingan kompaniya uchun dispatch Meta'ga HECH QANDAY so'rov yubormaydi", called["count"] == 0)
    finally:
        meta_api.send_conversion_event = real_send
        client.get("/logout")

    print("OK: /connect-accounts/meta/disconnect barcha Meta maydonlarini tozalaydi va shundan keyingi dispatch jim (Meta'ga so'rovsiz) o'tkazib yuboriladi")


# --- 6. Cross-tenant: Company B hech qachon Company A'ning Advanced/Manual CAPI tokeniga ega bo'lolmaydi ---
def test_cross_tenant_manual_capi_isolation():
    real_verify = meta_api.verify_dataset_credentials
    meta_api.verify_dataset_credentials = lambda dataset_id, token: {"id": dataset_id, "name": "D"}
    try:
        _signup("Tenant A MChJ", "tenant_a_admin", plan="business")
        _login("tenant_a_admin")
        client.post("/connect-accounts/meta/manual", data={
            "dataset_id": "dataset_A", "capi_access_token": "TOKEN_BELONGS_TO_A",
        }, follow_redirects=True)
        client.get("/logout")

        _signup("Tenant B MChJ", "tenant_b_admin", plan="business")
        _login("tenant_b_admin")
        html = client.get("/connect-accounts").get_data(as_text=True)
        check("Company B sahifasida Company A tokeni HECH QACHON ko'rinmaydi", "TOKEN_BELONGS_TO_A" not in html)
        check("Company B sahifasida Company A dataset ID'si ko'rinmaydi", "dataset_A" not in html)

        session = db_module.get_session()
        try:
            cb = session.query(db_module.Company).filter_by(name="Tenant B MChJ").first()
            check("Company B'ning o'z manual CAPI maydonlari bo'sh", cb.meta_capi_dataset_id is None and cb.meta_capi_access_token is None)

            company_a = session.query(db_module.Company).filter_by(name="Tenant A MChJ").first()
            check("Company A'ning tokeni faqat O'ZINING qatorida qoladi", company_a.get_meta_capi_token() == "TOKEN_BELONGS_TO_A")
        finally:
            session.close()
    finally:
        meta_api.verify_dataset_credentials = real_verify
        client.get("/logout")

    print("OK: Advanced/Manual CAPI Dataset ID/token ikkinchi kompaniyaga HECH QACHON sizib chiqmaydi (cross-tenant izolyatsiya saqlanadi)")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    print(f"BARCHA TEKSHIRUVLAR O'TDI")


if __name__ == "__main__":
    run_all()
