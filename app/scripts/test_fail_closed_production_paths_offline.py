"""test_fail_closed_production_paths_offline.py -- "yopiq holatda yiqiladigan"
(fail-closed) tenant rejimida HAQIQIY ishlash yo'llari buzilmaganini
tekshiradi (docs/PLAN.md, 1-bosqich).

Boshqa testlardan farqli o'laroq bu fayl `REPLIX_TEST_DEFAULT_UNSCOPED`
ISHLATMAYDI -- ya'ni production'dagidek: kontekstsiz tenant-so'rov darhol
`TenantScopeError`. Tekshiriladi (hammasi tarmoqsiz, vaqtinchalik SQLite):
  1. Login (POST /login) va Flask-Login `load_user` -- kontekstsiz ishlaydi.
  2. Kirgan admin sifatida parametrsiz BARCHA GET sahifalar -- 500 yo'q,
     `TenantScopeError` yo'q.
  3. Anonim sahifalar va ro'yxatdan o'tish (POST /signup).
  4. Telegram webhook (ro'yxatdan o'tgan va yot chat), kiruvchi lid webhook.
  5. `scheduler.JOBS`dagi HAR BIR fon vazifasi (tarmoq o'chiq -- tashqi
     xatolar kutiladi, lekin `TenantScopeError` bo'lmasligi SHART).
  6. Yot Telegram chat hech bir kompaniyaning ma'lumotini ololmaydi.

Ishga tushirish:
    cd app && python3 scripts/test_fail_closed_production_paths_offline.py
"""

import json
import os
import sys
import tempfile
import datetime as dt
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.pop("REPLIX_TEST_DEFAULT_UNSCOPED", None)  # ATAYLAB production rejimi
os.environ.pop("TENANT_SCOPE_MODE", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.pop("OPENAI_API_KEY", None)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-dummy-token")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ.pop("TELEGRAM_WEBHOOK_SECRET", None)
_TMPDIR = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMPDIR, 'fail_closed.db')}"

import requests  # noqa: E402


def _offline(*_a, **_k):
    raise requests.ConnectionError("offline test -- tarmoq ATAYLAB o'chirilgan")


for _name in ("get", "post", "put", "delete", "request"):
    setattr(requests, _name, _offline)
requests.Session.request = _offline  # type: ignore[assignment]

import db as db_module  # noqa: E402

# Har bir TenantScopeError'ni (hatto route ichida `except Exception` bilan
# yutilgan bo'lsa ham) qayd qilamiz.
_scope_errors: list[str] = []
_orig_init = db_module.TenantScopeError.__init__


def _recording_init(self, *args, **kwargs):
    _scope_errors.append(str(args[0]) if args else "TenantScopeError")
    _orig_init(self, *args, **kwargs)


db_module.TenantScopeError.__init__ = _recording_init  # type: ignore[method-assign]

import app as app_module  # noqa: E402
import meta_api  # noqa: E402
import scheduler  # noqa: E402

meta_api._retry_sleep = lambda attempt: None
app_module.app.config["TESTING"] = True
app_module.app.config["WTF_CSRF_ENABLED"] = False
db_module.init_db()

PW = "parol12345"
A_GROUP = "-1001234"
UNKNOWN_CHAT = 777000111
FAR_FUTURE = dt.datetime.utcnow() + dt.timedelta(days=3650)

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("OK:   " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def _seed() -> dict:
    session = db_module.get_session()
    try:
        with db_module.unscoped():
            owner_id = db_module.get_default_company_id()
            owner = session.get(db_module.Company, owner_id)
            owner.paid_until = FAR_FUTURE
            owner.plan = "unlimited"
            a = db_module.Company(
                name="A MChJ", plan="unlimited", is_active=True, paid_until=FAR_FUTURE,
                telegram_group_id=A_GROUP, inbound_lead_token="tok-a-123",
                webhook_out_url="https://example.invalid/hook",
            )
            session.add(a)
            session.commit()
            admin = db_module.Manager(username="a_admin", full_name="A admin", role="admin", company_id=a.id)
            admin.set_password(PW)
            session.add(admin)
            session.commit()
            db_module.seed_default_funnel_stages_for_company(a.id)
            session.add(db_module.Lead(company_id=a.id, full_name="A lead", phone="+998901112233", source="manual", status="new"))
            session.add(db_module.StandingTask(company_id=a.id, chat_id=A_GROUP, object_id="123", object_name="A kampaniya",
                                               on_time="00:00", off_time="00:01", is_active=True))
            session.add(db_module.StandingReport(company_id=a.id, chat_id=A_GROUP, time_hhmm="00:00", is_active=True))
            session.commit()
            return {"owner_id": owner_id, "a_id": a.id}
    finally:
        session.close()


def _tg_update(chat_id, text, chat_type="group"):
    return {"update_id": 1, "message": {"message_id": 1, "chat": {"id": chat_id, "type": chat_type},
                                         "from": {"id": 5}, "text": text}}


def run_all():
    ids = _seed()
    sent: list = []

    with mock.patch.object(app_module, "tg_send", side_effect=lambda chat_id, text, *a, **k: sent.append((chat_id, text))):
        client = app_module.app.test_client()

        # 3. Anonim sahifalar.
        for path in ("/", "/login", "/signup", "/tariflar", "/maxfiylik-siyosati", "/robots.txt", "/sitemap.xml"):
            r = client.get(path)
            check(f"anonim GET {path} -> {r.status_code}", r.status_code < 500)

        # 1. Login + load_user.
        r = client.post("/login", data={"username": "a_admin", "password": PW})
        check("POST /login (fail-closed) -> redirect", r.status_code == 302, str(r.status_code))
        r = client.get("/leads")
        check("login'dan keyin /leads ochiladi", r.status_code == 200 and "A lead" in r.get_data(as_text=True), str(r.status_code))

        # 2. Barcha parametrsiz GET sahifalar.
        skip = ("logout", "static", "/api/trigger", "/api/cron", "oauth", "callback", "/connect-accounts/facebook")
        errors_before = len(_scope_errors)
        crawled = 0
        for rule in sorted(app_module.app.url_map.iter_rules(), key=lambda r: r.rule):
            if "GET" not in rule.methods or rule.arguments or any(s in rule.rule for s in skip):
                continue
            r = client.get(rule.rule)
            crawled += 1
            if r.status_code >= 500:
                check(f"GET {rule.rule} (A admin)", False, str(r.status_code))
        check(f"{crawled} ta sahifa -- TenantScopeError yo'q", len(_scope_errors) == errors_before, "; ".join(_scope_errors[errors_before:])[:500])
        client.get("/logout")

        # Ro'yxatdan o'tish (yangi kompaniya).
        r = client.post("/signup", data={
            "company_name": "Yangi MChJ", "admin_username": "yangi_admin", "admin_full_name": "Yangi",
            "email": "yangi@test.uz", "password": PW, "password2": PW, "plan": "trial",
        })
        check("POST /signup -> redirect", r.status_code == 302, str(r.status_code))
        client.get("/logout")

        # 4. Telegram webhook -- ro'yxatdan o'tgan guruh.
        sent.clear()
        r = client.post("/api/webhook", json=_tg_update(int(A_GROUP), "/vazifalar"))
        check("Telegram /vazifalar (A guruhi) -> 200", r.status_code == 200)
        text = "\n".join(t for _c, t in sent)
        check("A guruhi o'z vazifasini ko'radi", "A kampaniya" in text, text[:200])
        check("Telegram xatoliksiz", "Kutilmagan ichki xatolik" not in text, text[:200])

        # 6. Yot chat -- hech narsa ko'rmaydi.
        sent.clear()
        client.post("/api/webhook", json=_tg_update(UNKNOWN_CHAT, "/vazifalar", "private"))
        text = "\n".join(t for _c, t in sent)
        check("yot chat A vazifasini KO'RMAYDI", "A kampaniya" not in text, text[:200])
        sent.clear()
        client.post("/api/webhook", json=_tg_update(UNKNOWN_CHAT, "salom", "private"))
        text = "\n".join(t for _c, t in sent)
        check("yot chat erkin matn -- ichki xatoliksiz", "Kutilmagan ichki xatolik" not in text, text[:200])

        # Kiruvchi lid webhook.
        r = client.post("/api/webhook/leads/tok-a-123", json={"name": "Webhook lid", "phone": "+998901234567"})
        check("kiruvchi lid webhook -> 200", r.status_code == 200, r.get_data(as_text=True)[:200])
        session = db_module.get_session()
        try:
            with db_module.scoped_as(ids["a_id"]):
                found = session.query(db_module.Lead).filter_by(full_name="Webhook lid").first()
            check("webhook lidi A kompaniyasiga yozildi", found is not None and found.company_id == ids["a_id"])
        finally:
            session.close()

    # 5. Barcha fon vazifalari.
    with mock.patch.object(scheduler, "_tg_send", return_value={"ok": True, "error": None}):
        for name, job in scheduler.JOBS.items():
            before = len(_scope_errors)
            try:
                job()
            except db_module.TenantScopeError:
                pass
            except Exception as e:  # tarmoq o'chiq -- tashqi xatolar kutiladi
                print(f"      ({name}: tashqi xato kutilgan -- {type(e).__name__})")
            check(f"fon vazifasi '{name}' -- TenantScopeError yo'q", len(_scope_errors) == before,
                  "; ".join(_scope_errors[before:])[:300])

    check("kontekst testdan keyin bo'sh", db_module.get_current_company_id() is None)

    if failures:
        print(f"\n{len(failures)} ta XATO:")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("\nBARCHA TESTLAR O'TDI")


if __name__ == "__main__":
    run_all()
