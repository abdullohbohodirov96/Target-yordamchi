"""test_tenant_mixing_fixes_offline.py -- 2026-10-01 "kompaniyalar
aralashib ketmayaptimi" auditi tuzatishlari (tarmoqsiz, vaqtinchalik SQLite):
  1. B kompaniyasi CAPI ma'lumotisiz -- egasining ENV Pixel'iga TUSHMAYDI.
  2. Page token keshi kalitida token xeshi bor (A va B aralashmaydi).
  3. Bir xil Facebook sahifa / reklama hisobi / Telegram guruhini ikkinchi
     kompaniya ulay olmaydi.
  4. Qo'ng'iroq faqat O'Z kompaniyasining lidiga bog'lanadi.
  5. `_is_platform_owner` -- standart kompaniya admini.

Ishga tushirish:
    cd app && python3 scripts/test_tenant_mixing_fixes_offline.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.pop("REPLIX_TEST_DEFAULT_UNSCOPED", None)
os.environ.pop("TENANT_SCOPE_MODE", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-dummy-token")
os.environ["FLASK_SECRET_KEY"] = "test-secret-mixing"
_TMPDIR = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMPDIR, 'mixing.db')}"

import requests  # noqa: E402


def _offline(*_a, **_k):
    raise requests.ConnectionError("offline test")


for _name in ("get", "post", "put", "delete", "request"):
    setattr(requests, _name, _offline)
requests.Session.request = _offline  # type: ignore[assignment]

import app as app_module  # noqa: E402
import call_sync  # noqa: E402
import db as db_module  # noqa: E402
import meta_api  # noqa: E402
import meta_events  # noqa: E402

db_module.init_db()
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("OK:   " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def _seed():
    s = db_module.get_session()
    try:
        with db_module.unscoped():
            owner = s.get(db_module.Company, db_module.get_default_company_id())  # platforma egasi
            owner.meta_page_id, owner.telegram_group_id = "page_OWNER", "-100111"
            b = db_module.Company(name="B", plan="unlimited", is_active=True)
            s.add(b)
            s.commit()
            s.add(db_module.Lead(company_id=owner.id, full_name="Owner lid", phone="+998901234567", source="manual", status="new"))
            s.add(db_module.Lead(company_id=b.id, full_name="B lid", phone="+998901234567", source="manual", status="new"))
            mgr = db_module.Manager(username="b_mgr", full_name="B mgr", role="manager", company_id=b.id, phone_number="+998935550000")
            mgr.set_password("parol12345")
            s.add(mgr)
            s.commit()
            return owner.id, b.id
    finally:
        s.close()


def test_capi_no_owner_fallback(owner_id, b_id):
    meta_api.PIXEL_ID, meta_api.ACCESS_TOKEN = "OWNER_PIXEL", "OWNER_TOKEN"
    s = db_module.get_session()
    try:
        with db_module.unscoped():
            b = s.get(db_module.Company, b_id)
            owner = s.get(db_module.Company, owner_id)
            check("B: CAPI ENV (egasi) Pixel'iga tushmaydi", meta_events._resolve_capi_credentials(b) == (None, None))
            check("B: capi_credentials_configured False", meta_events.capi_credentials_configured(b) is False)
            check("Egasi: ENV zaxirasi ishlaydi", meta_events._resolve_capi_credentials(owner) == ("OWNER_TOKEN", "OWNER_PIXEL"))
    finally:
        s.close()


def test_page_cache_key():
    a = meta_api._page_cache_key("p1", "TOKEN_A")
    b = meta_api._page_cache_key("p1", "TOKEN_B")
    check("page token kesh kaliti token bo'yicha farqlanadi", a != b and a.startswith("p1|"))
    check("kesh kalitida ochiq token yo'q", "TOKEN_A" not in a)


def test_identifier_conflict(owner_id, b_id):
    with app_module.app.app_context():
        check("B egasining sahifasini ulay olmaydi", bool(app_module._identifier_conflict(b_id, page_id="page_OWNER")))
        check("B egasining guruhini ulay olmaydi", bool(app_module._identifier_conflict(b_id, telegram_group_id="-100111")))
        check("egasi o'zinikini qayta saqlay oladi", app_module._identifier_conflict(owner_id, page_id="page_OWNER") is None)
        check("yangi sahifa -- ruxsat", app_module._identifier_conflict(b_id, page_id="page_NEW") is None)


def test_call_links_own_company_lead(b_id):
    s = db_module.get_session()
    try:
        with db_module.unscoped():
            b = s.get(db_module.Company, b_id)
            b_lead = s.query(db_module.Lead).filter_by(company_id=b_id).one()
    finally:
        s.close()
    raw = {"db_call_id": "c1", "src_number": "+998935550000", "client_number": "+998901234567",
           "direction": 1, "duration": 30, "start_time": 1700000000}
    orig_fetch, orig_conf, orig_creds = call_sync._fetch_calls, call_sync.is_configured, call_sync._resolve_credentials
    call_sync._fetch_calls = lambda *a, **k: [raw]
    call_sync.is_configured = lambda company=None: True
    call_sync._resolve_credentials = lambda company=None: ("addr", "key", "user")
    try:
        with db_module.scoped_as(b_id):
            res = call_sync.sync_once(company=b)
        check("sync_once: 1 ta yangi qo'ng'iroq", res.get("new_calls") == 1, str(res))
    finally:
        call_sync._fetch_calls, call_sync.is_configured, call_sync._resolve_credentials = orig_fetch, orig_conf, orig_creds
    s = db_module.get_session()
    try:
        with db_module.unscoped():
            recs = s.query(db_module.CallRecord).all()
            if recs:
                check("qo'ng'iroq B lidiga bog'landi (egasinikiga emas)", all(r.lead_id == b_lead.id for r in recs),
                      str([(r.lead_id, r.company_id) for r in recs]))
    finally:
        s.close()
    src = open(call_sync.__file__, encoding="utf-8").read()
    check("call_sync: lid qidiruvi company_id bilan cheklangan", "Lead.company_id == company_id" in src)


def test_platform_owner_static():
    src = open(app_module.__file__, encoding="utf-8").read()
    check("_is_platform_owner standart kompaniyaga tayanadi",
          'getattr(current_user, "company_id", None) == db.get_default_company_id()' in src)


def run_all():
    owner_id, b_id = _seed()
    test_capi_no_owner_fallback(owner_id, b_id)
    test_page_cache_key()
    test_identifier_conflict(owner_id, b_id)
    test_call_links_own_company_lead(b_id)
    test_platform_owner_static()
    if failures:
        print(f"\n{len(failures)} ta XATO:")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("\nBARCHA TESTLAR O'TDI")


if __name__ == "__main__":
    run_all()
