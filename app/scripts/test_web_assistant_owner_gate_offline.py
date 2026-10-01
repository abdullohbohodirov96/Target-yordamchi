"""test_web_assistant_owner_gate_offline.py -- XAVFSIZLIK (2026-10-01 audit):
veb AI-yordamchi (/api/assistant) orqali BOSHQA kompaniya admini/menejeri
platforma egasining global Meta hisobini boshqara olmasligi va uning
ma'lumotini ko'ra olmasligi kerak.
  1. B kompaniya admini "to'xtat" yozsa -- orchestrator.classify_intent /
     execute_intent UMUMAN chaqirilmaydi, javob B'ning o'z ma'lumotlari bilan
     (call_light_chat, B snapshot).
  2. B menejeri "bugun necha lead" -- egasining metrikasi emas, B'niki.
  3. Platforma egasi (standart kompaniya admini) -- eski yo'l (intentlar) ishlaydi.

Ishga tushirish:
    cd app && python3 scripts/test_web_assistant_owner_gate_offline.py
"""
import os
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("REPLIX_TEST_DEFAULT_UNSCOPED", "1")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("OPENAI_API_KEY", "test-dummy-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-dummy-token")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(tempfile.mkdtemp(), 'gate.db')}"

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402
import orchestrator  # noqa: E402

app_module.app.config["TESTING"] = True
app_module.app.config["WTF_CSRF_ENABLED"] = False
db_module.init_db()
PW = "parol12345"
failures = []


def check(name, cond, detail=""):
    print(("OK:   " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def _seed():
    s = db_module.get_session()
    try:
        far = db_module.dt.datetime.utcnow() + db_module.dt.timedelta(days=365)
        owner_c = s.get(db_module.Company, db_module.get_default_company_id())
        owner_c.plan, owner_c.paid_until = "unlimited", far
        b = db_module.Company(name="BetaCo", plan="business", is_active=True, paid_until=far)
        s.add(b)
        s.commit()
        for uname, role, cid in (("owner_admin", "admin", owner_c.id), ("b_admin", "admin", b.id), ("b_mgr", "manager", b.id)):
            m = db_module.Manager(username=uname, role=role, company_id=cid, full_name=uname)
            m.set_password(PW)
            s.add(m)
        s.commit()
        return b.id
    finally:
        s.close()


def run_all():
    b_id = _seed()
    for uname in ("b_admin", "b_mgr"):
        c = app_module.app.test_client()
        c.post("/login", data={"username": uname, "password": PW})
        with mock.patch.object(orchestrator, "classify_intent") as ci, \
                mock.patch.object(orchestrator, "execute_intent") as ei, \
                mock.patch.object(app_module, "_company_ai_snapshot", return_value="B_SNAPSHOT") as snap, \
                mock.patch.object(orchestrator, "call_light_chat", return_value="B javobi") as chat:
            r = c.post("/api/assistant", json={"message": "AB | Traffic | IG ni to'xtat"})
            check(f"{uname}: 200 va javob", r.status_code == 200 and r.get_json()["reply"] == "B javobi", str(r.get_json()))
            check(f"{uname}: classify/execute_intent CHAQIRILMADI (egasi hisobiga tegilmaydi)", not ci.called and not ei.called)
            check(f"{uname}: B'ning o'z ma'lumoti ishlatildi", snap.call_args[0][0] == b_id and "B_SNAPSHOT" in chat.call_args[0][0])
    c = app_module.app.test_client()
    c.post("/login", data={"username": "owner_admin", "password": PW})
    with mock.patch.object(orchestrator, "classify_intent", return_value=("METRIC", "")) as ci, \
            mock.patch.object(orchestrator, "execute_intent", return_value="egasi metrikasi") as ei:
        r = c.post("/api/assistant", json={"message": "bugun necha lead"})
        check("egasi: intent yo'li ishlaydi", ci.called and ei.called and r.get_json()["reply"] == "egasi metrikasi")
    if failures:
        print(f"\n{len(failures)} ta XATO")
        sys.exit(1)
    print("\nBARCHA TESTLAR O'TDI")


if __name__ == "__main__":
    run_all()
