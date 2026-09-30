"""test_tenant_isolation_a_vs_b_offline.py -- docs/PLAN.md, 1-bosqich:
"A kompaniya B'ning lidlari, kampaniyalari, menejerlari va tokenlarini
ko'rmasligi" + "har bir kompaniyaning Meta tokeni shifrlangan".

PRODUCTION rejimida (fail-closed, `REPLIX_TEST_DEFAULT_UNSCOPED`siz),
tarmoqsiz, vaqtinchalik SQLite:
  1. A va B kompaniyasi: har birida admin, menejer, lid, kampaniya
     qoralamasi, Meta access token, CAPI token, Moi Zvonki kaliti.
  2. A admini (va A menejeri) sifatida parametrsiz BARCHA GET sahifalar
     ochiladi -- hech birida B'ning markeri yo'q (lid/menejer/kampaniya
     nomi, token ochiq matni YOKI shifri).
  3. Hech bir sahifada hatto A'ning O'Z tokeni ochiq ko'rinmaydi.
  4. Ijobiy nazorat: A o'z lidi, menejeri va kampaniyasini ko'radi.
  5. Bazada barcha tokenlar shifrlangan; eski ochiq matnli token ishga
     tushishda shifrlanadi (idempotent); TOKEN_ENCRYPTION_KEY keyinroq
     qo'shilsa ham eski shifrlar o'qiladi (MultiFernet).

Ishga tushirish:
    cd app && python3 scripts/test_tenant_isolation_a_vs_b_offline.py
"""

import json
import os
import sys
import tempfile
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.pop("REPLIX_TEST_DEFAULT_UNSCOPED", None)  # ATAYLAB production rejimi
os.environ.pop("TENANT_SCOPE_MODE", None)
os.environ.pop("TOKEN_ENCRYPTION_KEY", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-dummy-token")
os.environ["FLASK_SECRET_KEY"] = "test-secret-a-vs-b"
_TMPDIR = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMPDIR, 'a_vs_b.db')}"

import requests  # noqa: E402


def _offline(*_a, **_k):
    raise requests.ConnectionError("offline test -- tarmoq ATAYLAB o'chirilgan")


for _name in ("get", "post", "put", "delete", "request"):
    setattr(requests, _name, _offline)
requests.Session.request = _offline  # type: ignore[assignment]

import app as app_module  # noqa: E402
import crypto_util  # noqa: E402
import db as db_module  # noqa: E402
import meta_api  # noqa: E402

meta_api._retry_sleep = lambda attempt: None
app_module.app.config["TESTING"] = True
app_module.app.config["WTF_CSRF_ENABLED"] = False
db_module.init_db()

PW = "parol12345"
FAR_FUTURE = dt.datetime.utcnow() + dt.timedelta(days=3650)
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("OK:   " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def _seed_company(session, key: str) -> dict:
    marker = f"ZZ{key}MARK"
    c = db_module.Company(name=f"Kompaniya {key}", plan="unlimited", is_active=True, paid_until=FAR_FUTURE,
                          meta_ad_account_id=f"act_{key}111", meta_page_id=f"page_{key}111",
                          meta_integration_status="connected", meta_capi_dataset_id=f"ds_{key}111")
    c.set_meta_access_token(f"EAA_{key}_SECRET_ACCESS_TOKEN")
    c.set_meta_capi_token(f"EAA_{key}_SECRET_CAPI_TOKEN")
    c.set_moizvonki_api_key(f"MZ_{key}_SECRET_KEY")
    session.add(c)
    session.commit()
    admin = db_module.Manager(username=f"{key.lower()}_admin", full_name=f"Admin {marker}", role="admin", company_id=c.id)
    admin.set_password(PW)
    mgr = db_module.Manager(username=f"{key.lower()}_manager", full_name=f"Menejer {marker}", role="manager", company_id=c.id,
                            allowed_modules=json.dumps(["dashboard", "leads", "target", "settings", "analytics"]))
    mgr.set_password(PW)
    session.add_all([admin, mgr])
    session.commit()
    db_module.seed_default_funnel_stages_for_company(c.id)
    session.add(db_module.Lead(company_id=c.id, full_name=f"Lid {marker}", phone=f"+99890{c.id}112233",
                               source="manual", status="new", assigned_manager_id=mgr.id))
    session.add(db_module.CampaignDraft(company_id=c.id, created_by_manager_id=admin.id, title=f"Kampaniya {marker}",
                                        source="MANUAL", status="draft", objective="MESSAGES",
                                        state_json=json.dumps({"campaign": {"name": f"Kampaniya {marker}"}})))
    session.commit()
    return {
        "id": c.id, "marker": marker,
        "secrets": [f"EAA_{key}_SECRET_ACCESS_TOKEN", f"EAA_{key}_SECRET_CAPI_TOKEN", f"MZ_{key}_SECRET_KEY"],
        "ciphertexts": [c.meta_access_token, c.meta_capi_access_token, c.moizvonki_api_key],
    }


def _crawl(client, label: str, forbidden: list[str]) -> dict:
    skip = ("logout", "static", "/api/trigger", "/api/cron", "oauth", "callback", "/connect-accounts/facebook")
    pages = {}
    for rule in sorted(app_module.app.url_map.iter_rules(), key=lambda r: r.rule):
        if "GET" not in rule.methods or rule.arguments or any(s in rule.rule for s in skip):
            continue
        r = client.get(rule.rule, follow_redirects=True)
        body = r.get_data().decode("utf-8", errors="ignore")  # eksport (xlsx/pdf) ham bo'lishi mumkin
        pages[rule.rule] = body
        leaked = [f for f in forbidden if f and f in body]
        if r.status_code >= 500 or leaked:
            check(f"{label}: GET {rule.rule}", False, f"status={r.status_code} leaked={leaked}")
    check(f"{label}: {len(pages)} ta sahifada B ma'lumoti/token yo'q", True)
    return pages


def test_a_cannot_see_b():
    session = db_module.get_session()
    try:
        with db_module.unscoped():
            a = _seed_company(session, "A")
            b = _seed_company(session, "B")
    finally:
        session.close()

    forbidden_for_a = [b["marker"], *b["secrets"], *b["ciphertexts"], *a["secrets"], *a["ciphertexts"]]

    for username in ("a_admin", "a_manager"):
        client = app_module.app.test_client()
        r = client.post("/login", data={"username": username, "password": PW})
        check(f"{username} login", r.status_code == 302, str(r.status_code))
        pages = _crawl(client, username, forbidden_for_a)
        if username == "a_admin":
            check("ijobiy nazorat: A admin o'z lidini ko'radi", a["marker"] in pages.get("/leads", ""))
            check("ijobiy nazorat: A admin o'z menejerini ko'radi", f"Menejer {a['marker']}" in pages.get("/managers", ""))
            check("ijobiy nazorat: A admin o'z kampaniyasini ko'radi", f"Kampaniya {a['marker']}" in pages.get("/avtopilot", ""))
        client.get("/logout")


def test_tokens_encrypted_at_rest():
    session = db_module.get_session()
    try:
        rows = session.query(db_module.Company).all()
        for c in rows:
            for col in db_module._SECRET_COLUMNS:
                raw = getattr(c, col)
                if raw:
                    check(f"company #{c.id}.{col} bazada shifrlangan", crypto_util.looks_like_fernet(raw) and crypto_util.is_encrypted(raw))
    finally:
        session.close()


def test_legacy_plaintext_encrypted_on_startup():
    session = db_module.get_session()
    try:
        c = db_module.Company(name="Eski", meta_access_token="EAA_LEGACY_PLAINTEXT", moizvonki_api_key="MZ_LEGACY")
        session.add(c)
        session.commit()
        cid = c.id
    finally:
        session.close()

    counts = db_module.encrypt_legacy_plaintext_secrets()
    check("eski ochiq token shifrlandi", counts["meta_access_token"] >= 1 and counts["moizvonki_api_key"] >= 1, str(counts))
    counts2 = db_module.encrypt_legacy_plaintext_secrets()
    check("qayta ishga tushirish -- hech narsa o'zgarmaydi (idempotent)", not any(counts2.values()), str(counts2))

    session = db_module.get_session()
    try:
        c = session.get(db_module.Company, cid)
        check("bazada endi shifr", crypto_util.looks_like_fernet(c.meta_access_token))
        check("getter asl tokenni qaytaradi", c.get_meta_access_token() == "EAA_LEGACY_PLAINTEXT")
        check("Moi Zvonki kaliti ham", c.get_moizvonki_api_key() == "MZ_LEGACY")
        old_cipher = c.meta_access_token
    finally:
        session.close()

    # TOKEN_ENCRYPTION_KEY keyinroq qo'shilsa -- eski shifr o'qilaveradi.
    from cryptography.fernet import Fernet
    os.environ["TOKEN_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    crypto_util._fernet_instance = None
    try:
        check("yangi kalit qo'shilgach eski shifr o'qiladi (MultiFernet)", crypto_util.decrypt_token(old_cipher) == "EAA_LEGACY_PLAINTEXT")
        new_cipher = crypto_util.encrypt_token("EAA_NEW")
        check("yangi qiymat yangi kalit bilan shifrlanadi", crypto_util.decrypt_token(new_cipher) == "EAA_NEW")
    finally:
        os.environ.pop("TOKEN_ENCRYPTION_KEY", None)
        crypto_util._fernet_instance = None


def run_all():
    test_a_cannot_see_b()
    test_tokens_encrypted_at_rest()
    test_legacy_plaintext_encrypted_on_startup()
    if failures:
        print(f"\n{len(failures)} ta XATO:")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("\nBARCHA TESTLAR O'TDI")


if __name__ == "__main__":
    run_all()
