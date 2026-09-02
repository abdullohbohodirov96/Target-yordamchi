"""test_landing_contact_form_offline.py — 2026-09, foydalanuvchi so'rovi:
"web ozida ushatta pasida forma qilib qoy malumotlarini qabul qilish uchun
va aloqa uchun nomerlar ham qoshib qoy ... formani tgda manga habar kesin".

Tekshiradi:
  - Bosh sahifada forma, telefon va Telegram havolalari ko'rinishini.
  - Formani to'ldirib yuborilganda kv_store'ga saqlanishini (Telegram
    sozlanmagan bo'lsa ham -- HECH QACHON yo'qolmasligi kerak).
  - Bo'sh ism/telefon bilan yuborilsa xatolik ko'rsatilishini.
  - LANDING_CONTACT_TELEGRAM_CHAT_ID sozlangan bo'lsa, scheduler._tg_send
    chaqirilishini (soxta funksiya bilan).

Ishga tushirish:
    cd app && python3 scripts/test_landing_contact_form_offline.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMPDIR = tempfile.mkdtemp()
_DB_PATH = os.path.join(_TMPDIR, "test_contact.db")

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("META_ACCESS_TOKEN", "test-dummy-token")
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_test_dummy")
os.environ.setdefault("META_PAGE_ID", "page_test_dummy")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402
import kv_store  # noqa: E402

app_module.app.config["TESTING"] = True
db_module.init_db()

client = app_module.app.test_client()
failures = []


def check(name, cond):
    print(("OK  " if cond else "FAIL") + " " + name)
    if not cond:
        failures.append(name)


html = client.get("/").get_data(as_text=True)
check("landing sahifada forma bor", 'action="/aloqa"' in html)
check("telefon havolasi bor", "tel:+998509999733" in html)
check("telegram havolasi bor", "t.me/abdulloh_mrktlg" in html)

r = client.post("/aloqa", data={"name": "Ali Valiyev", "phone": "+998901112233", "message": "Narxlar haqida"}, follow_redirects=True)
check("muvaffaqiyatli yuborilgach 200", r.status_code == 200)
check("muvaffaqiyat xabari ko'rsatildi", "qabul qilindi" in r.get_data(as_text=True))

subs = kv_store.get_json("landing_contact_submissions", default=[])
check("murojaat kv_store'ga saqlandi", len(subs) == 1 and subs[0]["name"] == "Ali Valiyev")

r_empty = client.post("/aloqa", data={"name": "", "phone": ""}, follow_redirects=True)
check("bo'sh ism/telefon rad etiladi", "kiriting" in r_empty.get_data(as_text=True))
check("bo'sh forma kv_store'ga qo'shilmadi", len(kv_store.get_json("landing_contact_submissions", default=[])) == 1)

# Telegram yo'naltirish -- soxta _tg_send bilan
sent = {}


def _fake_tg_send(chat_id, text):
    sent["chat_id"] = chat_id
    sent["text"] = text
    return {"ok": True}


import scheduler  # noqa: E402
real_tg_send = scheduler._tg_send
scheduler._tg_send = _fake_tg_send
real_chat_id_env = app_module.LANDING_CONTACT_TELEGRAM_CHAT_ID
app_module.LANDING_CONTACT_TELEGRAM_CHAT_ID = "-100123456"
try:
    client.post("/aloqa", data={"name": "Bek Turayev", "phone": "+998907778899", "message": ""}, follow_redirects=True)
    check("Telegram sozlangan bo'lsa _tg_send chaqiriladi", sent.get("chat_id") == -100123456)
    check("Telegram xabarida ism/telefon bor", "Bek Turayev" in sent.get("text", "") and "+998907778899" in sent.get("text", ""))
finally:
    scheduler._tg_send = real_tg_send
    app_module.LANDING_CONTACT_TELEGRAM_CHAT_ID = real_chat_id_env

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
else:
    print("ALL PASSED")
