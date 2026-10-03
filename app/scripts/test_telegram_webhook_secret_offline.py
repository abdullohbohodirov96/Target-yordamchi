"""test_telegram_webhook_secret_offline.py -- docs/PLAN.md, 1-bosqich:
Telegram webhook secret va bot kirish huquqlari (tarmoqsiz).

  1. `TELEGRAM_WEBHOOK_SECRET` yo'q bo'lsa ham ilova barqaror secret hosil
     qiladi va Telegram'dagi MAVJUD webhook URL'iga (o'zgartirmasdan) qo'shadi.
  2. Secret yoqilgach: secret'siz yoki noto'g'ri secret'li so'rov -> 403,
     to'g'ri secret -> 200.
  3. Telegram tasdiqlamasa (tarmoq xatosi) -- bot to'xtamaydi (eski rejim).
  4. Soxta so'rov platforma egasining chat ID'si bilan /pause yubora olmaydi.
  5. Egasi bo'lmagan chat /pause, /resume, /analyze, /status ishlata olmaydi.

Ishga tushirish:
    cd app && python3 scripts/test_telegram_webhook_secret_offline.py
"""

import os
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.pop("REPLIX_TEST_DEFAULT_UNSCOPED", None)
os.environ.pop("TELEGRAM_WEBHOOK_SECRET", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ["TELEGRAM_BOT_TOKEN"] = "123:test-token"
os.environ["FLASK_SECRET_KEY"] = "test-secret-tg"
os.environ["TELEGRAM_AGENTS_GROUP_ID"] = "-100555"
_TMPDIR = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMPDIR, 'tg_secret.db')}"

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402
import meta_api  # noqa: E402

app_module.app.config["TESTING"] = True
db_module.init_db()

OWNER_CHAT = -100555
failures: list[str] = []


def check(name, cond, detail=""):
    print(("OK:   " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


class _Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


_UPD = [0]


def _update(chat_id, text):
    _UPD[0] += 1
    return {"update_id": _UPD[0], "message": {"message_id": 1, "chat": {"id": chat_id, "type": "group"}, "text": text}}


def run_all():
    client = app_module.app.test_client()
    secret = app_module._derived_telegram_webhook_secret()
    check("hosil qilingan secret barqaror (restartda bir xil)", secret == app_module._derived_telegram_webhook_secret() and len(secret) == 64)

    # 3. Telegram tasdiqlamasa -- bot ishlashda davom etadi.
    with mock.patch.object(app_module.requests, "get", side_effect=OSError("offline")):
        ok = app_module._ensure_telegram_webhook_secret()
    check("tarmoq xatosida secret yoqilmaydi", ok is False and not app_module._TG_AUTO_SECRET["active"])
    with mock.patch.object(app_module, "tg_send"):
        r = client.post("/api/webhook", json=_update(777, "/start"))
    check("secret yoqilmaguncha bot to'xtamaydi (200)", r.status_code == 200, str(r.status_code))

    # 1. Mavjud URL'ga secret qo'shiladi.
    calls = {}

    def fake_post(url, json=None, timeout=None):
        calls["url"], calls["json"] = url, json
        return _Resp({"ok": True, "result": True})

    with mock.patch.object(app_module.requests, "get", return_value=_Resp({"ok": True, "result": {"url": "https://replix.uz/api/webhook", "allowed_updates": ["message"]}})), \
         mock.patch.object(app_module.requests, "post", side_effect=fake_post):
        ok = app_module._ensure_telegram_webhook_secret()
    check("setWebhook muvaffaqiyatli -> himoya yoqildi", ok and app_module._TG_AUTO_SECRET["active"])
    check("webhook URL o'zgarmadi", calls.get("json", {}).get("url") == "https://replix.uz/api/webhook", str(calls))
    check("allowed_updates saqlandi", calls.get("json", {}).get("allowed_updates") == ["message"])
    check("secret_token yuborildi", calls.get("json", {}).get("secret_token") == secret)

    # 2 + 4. Soxta so'rovlar.
    with mock.patch.object(meta_api, "pause_object") as pause, mock.patch.object(app_module, "tg_send"):
        r = client.post("/api/webhook", json=_update(OWNER_CHAT, "/pause 12345"))
        check("secret'siz soxta /pause -> 403", r.status_code == 403, str(r.status_code))
        r = client.post("/api/webhook", json=_update(OWNER_CHAT, "/pause 12345"), headers={"X-Telegram-Bot-Api-Secret-Token": "notogri"})
        check("noto'g'ri secret -> 403", r.status_code == 403, str(r.status_code))
        check("soxta so'rov reklamani to'xtatmadi", pause.call_count == 0)
        r = client.post("/api/webhook", json=_update(OWNER_CHAT, "/pause 12345"), headers={"X-Telegram-Bot-Api-Secret-Token": secret})
        check("to'g'ri secret + egasi chati -> 200 va /pause bajariladi", r.status_code == 200 and pause.call_count == 1, f"{r.status_code} {pause.call_count}")

    # 5. Egasi bo'lmagan chat -- egasi buyruqlari yopiq.
    sent = []
    with mock.patch.object(meta_api, "pause_object") as pause, mock.patch.object(meta_api, "activate_object") as act, \
         mock.patch.object(app_module, "tg_send", side_effect=lambda c, t, *a, **k: sent.append(t)):
        for cmd in ("/pause 1", "/resume 1", "/analyze", "/status"):
            client.post("/api/webhook", json=_update(-100999, cmd), headers={"X-Telegram-Bot-Api-Secret-Token": secret})
        check("begona chat /pause /resume bajarmaydi", pause.call_count == 0 and act.call_count == 0)
        check("begona chatga rad javobi yuborildi", len(sent) == 4 and all(t == app_module._NOT_OWNER_TEXT for t in sent), str(sent)[:200])

    # ENV'dagi aniq secret ustun turadi.
    with mock.patch.object(app_module, "TELEGRAM_WEBHOOK_SECRET", "env-secret"):
        r = client.post("/api/webhook", json=_update(777, "salom"), headers={"X-Telegram-Bot-Api-Secret-Token": secret})
        check("ENV secret o'rnatilgan bo'lsa -- faqat o'sha qabul qilinadi", r.status_code == 403, str(r.status_code))

    if failures:
        print(f"\n{len(failures)} ta XATO:")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("\nBARCHA TESTLAR O'TDI")


if __name__ == "__main__":
    run_all()
