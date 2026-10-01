"""test_telegram_hardening_offline.py -- 2026-10-01 Telegram bot auditi:
  1. Bir xil update_id ikki marta kelsa -- bir marta ishlanadi.
  2. edited_message umuman ishlanmaydi.
  3. Egasi kompaniyasining ODDIY menejeri yoki o'chirilgan admini
     egasi buyruqlarini (/pause) ishlata olmaydi; faol admin -- oladi.
  4. Ulash havolasi: noto'g'ri chat turida bosilsa token yonmaydi;
     created_at yo'q token -- muddati o'tgan; o'chirilgan xodimga ulanmaydi;
     shu chat boshqa xodimda bo'lsa -- o'sha bog'lanish uziladi.

    cd app && python3 scripts/test_telegram_hardening_offline.py
"""
import datetime as dt
import os
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("REPLIX_TEST_DEFAULT_UNSCOPED", None)
os.environ.pop("TELEGRAM_WEBHOOK_SECRET", None)
os.environ.pop("TELEGRAM_AGENTS_GROUP_ID", None)
os.environ.pop("TELEGRAM_REPORT_GROUP_ID", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ["TELEGRAM_BOT_TOKEN"] = "123:test-token"
os.environ["FLASK_SECRET_KEY"] = "test-secret-tg-hard"
_TMPDIR = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMPDIR, 'tg_hard.db')}"

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402
import kv_store  # noqa: E402
import meta_api  # noqa: E402

db_module.init_db()
app_module.app.config["TESTING"] = True
failures = []


def check(name, cond, detail=""):
    print(("OK:   " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


_UPD = [100]


def _upd(chat_id, text, *, edited=False, update_id=None, chat_type="private"):
    if update_id is None:
        _UPD[0] += 1
        update_id = _UPD[0]
    msg = {"message_id": 1, "chat": {"id": chat_id, "type": chat_type}, "text": text}
    return {"update_id": update_id, ("edited_message" if edited else "message"): msg}


def _seed():
    s = db_module.get_session()
    try:
        with db_module.unscoped():
            owner_id = db_module.get_default_company_id()
            other = db_module.Company(name="Boshqa", plan="unlimited", is_active=True)
            s.add(other)
            s.commit()
            rows = {
                "admin": db_module.Manager(username="o_admin", role="admin", company_id=owner_id, telegram_user_id="501"),
                "mgr": db_module.Manager(username="o_mgr", role="manager", company_id=owner_id, telegram_user_id="502"),
                "off": db_module.Manager(username="o_off", role="admin", company_id=owner_id, telegram_user_id="503", is_active=False),
                "b_mgr": db_module.Manager(username="b_mgr", role="manager", company_id=other.id, telegram_user_id="777"),
                "b_new": db_module.Manager(username="b_new", role="manager", company_id=other.id),
                "b_off": db_module.Manager(username="b_off", role="manager", company_id=other.id, is_active=False),
            }
            for m in rows.values():
                m.set_password("parol12345")
                s.add(m)
            s.commit()
            return other.id, {k: m.id for k, m in rows.items()}
    finally:
        s.close()


def run_all():
    other_id, ids = _seed()
    client = app_module.app.test_client()

    check("faol admin -- egasi", app_module._is_owner_telegram_chat(501))
    check("oddiy menejer -- egasi EMAS", not app_module._is_owner_telegram_chat(502))
    check("o'chirilgan admin -- egasi EMAS", not app_module._is_owner_telegram_chat(503))

    with mock.patch.object(meta_api, "pause_object") as pause, mock.patch.object(app_module, "tg_send"):
        client.post("/api/webhook", json=_upd(501, "/pause 1", update_id=9001))
        client.post("/api/webhook", json=_upd(501, "/pause 1", update_id=9001))
        check("bir xil update_id -- bir marta bajarildi", pause.call_count == 1, str(pause.call_count))
        client.post("/api/webhook", json=_upd(501, "/pause 1", edited=True))
        check("edited_message ishlanmadi", pause.call_count == 1, str(pause.call_count))
        client.post("/api/webhook", json=_upd(502, "/pause 1"))
        check("oddiy menejer /pause qila olmaydi", pause.call_count == 1, str(pause.call_count))

    now = dt.datetime.utcnow().isoformat()
    kv_store.set_json("tg_link_token:T1", {"kind": "personal", "company_id": other_id, "manager_id": ids["b_new"], "created_at": now})
    r = app_module._consume_telegram_link_token("T1", -100123, "group")
    check("shaxsiy havola guruhda -- ogohlantirish", r and "shaxsiy" in r)
    check("... va token yonmadi", kv_store.get_json("tg_link_token:T1") is not None)

    kv_store.set_json("tg_link_token:T2", {"kind": "personal", "company_id": other_id, "manager_id": ids["b_new"]})
    check("created_at yo'q token -- rad", app_module._consume_telegram_link_token("T2", 888, "private") is None)

    kv_store.set_json("tg_link_token:T3", {"kind": "personal", "company_id": other_id, "manager_id": ids["b_off"], "created_at": now})
    check("o'chirilgan xodimga ulanmaydi", app_module._consume_telegram_link_token("T3", 889, "private") is None)

    app_module.save_history(777, [{"role": "user", "content": "eski"}])
    r = app_module._consume_telegram_link_token("T1", 777, "private")
    check("to'g'ri chatda ulanadi", r and "Muvaffaqiyatli" in r, str(r))
    s = db_module.get_session()
    try:
        with db_module.unscoped():
            old = s.get(db_module.Manager, ids["b_mgr"])
            new = s.get(db_module.Manager, ids["b_new"])
            check("chat yangi xodimga bog'landi", new.telegram_user_id == "777")
            check("eski xodimdagi bog'lanish uzildi", old.telegram_user_id is None)
    finally:
        s.close()
    check("ulanganda suhbat tarixi tozalandi", app_module.get_history(777) == [])

    # tg_send: guruh supergroup'ga ko'chsa -- ID yangilanadi va qayta yuboriladi.
    s = db_module.get_session()
    try:
        with db_module.unscoped():
            c = s.get(db_module.Company, other_id)
            c.telegram_group_id = "-1001"
            s.commit()
    finally:
        s.close()

    class _R:
        def __init__(self, body):
            self._b = body

        def json(self):
            return self._b

    posted = []

    def fake_post(url, json=None, timeout=None):
        posted.append(json)
        if json["chat_id"] == -1001:
            return _R({"ok": False, "error_code": 400, "parameters": {"migrate_to_chat_id": -100777}})
        return _R({"ok": True})

    with mock.patch("requests.post", side_effect=fake_post):
        app_module.tg_send(-1001, "salom")
        app_module.tg_send(5, "")
    s = db_module.get_session()
    try:
        with db_module.unscoped():
            check("migrate_to_chat_id: guruh ID yangilandi", s.get(db_module.Company, other_id).telegram_group_id == "-100777")
    finally:
        s.close()
    check("migrate: yangi ID'ga qayta yuborildi", any(p["chat_id"] == -100777 for p in posted), str(posted))
    check("bo'sh matn ham jim qolmaydi", any(p["chat_id"] == 5 and p["text"] for p in posted))

    with mock.patch.object(app_module, "tg_send") as sent:
        client.post("/api/webhook", json=_upd(501, "/pause"))
        check("/pause ID'siz -- yo'riqnoma", sent.call_args and "Foydalanish" in sent.call_args[0][1])

    with mock.patch.object(app_module, "TG_AI_DAILY_LIMIT_PER_CHAT", 2):
        res = [app_module._tg_ai_quota_ok(4242) for _ in range(3)]
    check("kunlik AI limiti: 2 ta ruxsat, 3-si rad", res == [True, True, False], str(res))

    if failures:
        print(f"\n{len(failures)} ta XATO")
        sys.exit(1)
    print("\nBARCHA TESTLAR O'TDI")


if __name__ == "__main__":
    run_all()
