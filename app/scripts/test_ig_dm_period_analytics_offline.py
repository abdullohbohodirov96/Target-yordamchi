"""test_ig_dm_period_analytics_offline.py — 2026-09, foydalanuvchi so'rovi:
"Instagram DM tahlilini ko'rinadigan qilish". `/instagram-xabarlar` sahifasi
ilgari FAQAT "hozirgi holat"ni (real-time inbox: suhbatlar ro'yxati +
joriy hot/warm/cold hisobi) ko'rsatardi -- Lead Analytics CRM uchun bergan
DAVR bo'yicha tahlil (trend, taqsimot) IG DM uchun umuman yo'q edi.

Tekshiradi:
  1. `ig_dm_analytics.build_period_analytics()` -- yangi suhbat/mijoz
     xabari sonini, o'rtacha javob vaqtini, kunlik trendni va AI
     bahosining eng ko'p sabablarini TO'G'RI hisoblaydi.
  2. Davr chegarasidan TASHQARIDAGI xabarlar/suhbatlar hisobga
     OLINMAYDI (davr filtri chindan ishlashi).
  3. `/instagram-xabarlar?period=...` sahifasi 200 qaytaradi, yangi
     "Davr bo'yicha tahlil" bo'limi va kalendar-tanlagich ko'rinadi,
     ESKI "hozirgi holat" bo'limi ham o'zgarishsiz qoladi.

Ishga tushirish:
    cd app && python3 scripts/test_ig_dm_period_analytics_offline.py
"""
import os
import sys
import tempfile
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ.setdefault("META_ACCESS_TOKEN", "test-dummy-token")
os.environ.setdefault("META_PAGE_ID", "page_test_dummy")

_TMPDIR = tempfile.mkdtemp()
_DB_PATH = os.path.join(_TMPDIR, "test_ig_dm_period_analytics.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402
import ig_dm_analytics  # noqa: E402

app_module.app.config["TESTING"] = True
app_module.app.config["WTF_CSRF_ENABLED"] = False
db_module.init_db()

failures = []


def check(name, cond):
    print(("OK  " if cond else "FAIL") + " " + name)
    if not cond:
        failures.append(name)


session = db_module.get_session()
try:
    company = db_module.Company(name="DM Test", is_active=True, plan="unlimited")
    session.add(company)
    session.commit()
    company_id = company.id

    admin = db_module.Manager(username="dmp_admin", full_name="Admin", role="admin", company_id=company_id)
    admin.set_password("parol123")
    session.add(admin)
    session.commit()

    now = dt.datetime.utcnow()

    # Suhbat 1 -- DAVR ICHIDA boshlangan, javob berilgan, "hot" deb baholangan.
    conv1 = db_module.IgDmConversation(
        company_id=company_id, external_id="conv-1", customer_username="ali",
        ai_lead_quality="hot", ai_reasons='["Narx so\'radi", "Buyurtma bermoqchi"]',
    )
    session.add(conv1)
    session.commit()
    session.add_all([
        db_module.IgDmMessage(company_id=company_id, conversation_id=conv1.id, sender="customer",
                               text="Narxi qancha?", sent_at=now - dt.timedelta(days=2, hours=3)),
        db_module.IgDmMessage(company_id=company_id, conversation_id=conv1.id, sender="business",
                               text="500 ming so'm", sent_at=now - dt.timedelta(days=2, hours=3) + dt.timedelta(minutes=15)),
    ])

    # Suhbat 2 -- DAVR ICHIDA, "warm", javobsiz (business javob bermagan).
    conv2 = db_module.IgDmConversation(
        company_id=company_id, external_id="conv-2", customer_username="vali",
        ai_lead_quality="warm", ai_reasons='["Narx so\'radi"]',
    )
    session.add(conv2)
    session.commit()
    session.add(
        db_module.IgDmMessage(company_id=company_id, conversation_id=conv2.id, sender="customer",
                               text="Yetkazib berasizmi?", sent_at=now - dt.timedelta(days=1)),
    )

    # Suhbat 3 -- DAVR CHEGARASIDAN TASHQARIDA (90 kun oldin) -- hisobga
    # OLINMASLIGI kerak (davr filtri "last_7d" bo'lganda).
    conv3 = db_module.IgDmConversation(
        company_id=company_id, external_id="conv-3", customer_username="eski_mijoz",
        ai_lead_quality="cold", ai_reasons='["Eski so\'rov"]',
    )
    session.add(conv3)
    session.commit()
    session.add(
        db_module.IgDmMessage(company_id=company_id, conversation_id=conv3.id, sender="customer",
                               text="Eski xabar", sent_at=now - dt.timedelta(days=90)),
    )
    session.commit()
finally:
    session.close()

# --- 1-2. build_period_analytics() to'g'ri hisoblaydi, davr TASHQARISI kirmaydi ---
session = db_module.get_session()
try:
    with db_module.scoped_as(company_id):
        since = now - dt.timedelta(days=7)
        result = ig_dm_analytics.build_period_analytics(session, since, None)
finally:
    session.close()

check("davr ichida 2 ta suhbat faol deb hisoblanadi (conv3 chetda qoladi)", result["active_conversations"] == 2)
check("davr ichida 2 ta yangi suhbat (conv1, conv2)", result["new_conversations"] == 2)
check("davr ichida 2 ta mijoz xabari (conv1, conv2)", result["customer_messages"] == 2)
check("davr ichida 1 ta biznes xabari (conv1'ga javob)", result["business_messages"] == 1)
check("o'rtacha javob vaqti ~15 daqiqa", result["avg_response_minutes"] is not None and abs(result["avg_response_minutes"] - 15.0) < 0.5)
check("hot_count=1 (conv1)", result["hot_count"] == 1)
check("warm_count=1 (conv2)", result["warm_count"] == 1)
check("cold_count=0 (conv3 davr tashqarisida, hisoblanmaydi)", result["cold_count"] == 0)
check("eng ko'p sabab -- 'Narx so'radi' (ikkala suhbatda ham bor)", result["top_reasons"] and result["top_reasons"][0]["text"] == "Narx so'radi" and result["top_reasons"][0]["count"] == 2)
check("kunlik trendda kamida 2 kun bor", len(result["daily_trend"]) >= 2)

# Davr filtri UMUMAN qo'yilmasa (maximum) -- conv3 ham kirishi kerak.
session = db_module.get_session()
try:
    with db_module.scoped_as(company_id):
        result_all = ig_dm_analytics.build_period_analytics(session, None, None)
finally:
    session.close()
check("'maksimal' davrda barcha 3 ta suhbat kiradi", result_all["active_conversations"] == 3)
check("'maksimal' davrda cold_count=1 (conv3)", result_all["cold_count"] == 1)

# --- 3. Sahifa render qilinadi, yangi bo'lim ko'rinadi ---
client = app_module.app.test_client()
client.post("/login", data={"username": "dmp_admin", "password": "parol123"})

r = client.get("/instagram-xabarlar?period=last_7d")
check("sahifa 200 qaytaradi", r.status_code == 200)
html = r.get_data(as_text=True)
check("'Davr bo'yicha tahlil' bo'limi ko'rinadi", "Davr bo'yicha tahlil" in html)
check("kalendar-tanlagich ulangan (boshqa sahifalar bilan bir xil)", "la-dp" in html or "cdp" in html or "So'nggi 7 kun" in html)
check("eski 'Hozirgi holat' bo'limi ham saqlanib qolgan", "Hozirgi holat" in html)
check("davr ichidagi sabab matni ko'rinadi", "Narx so&#39;radi" in html or "Narx so'radi" in html)
check("kunlik trend grafik joyi bor", "chart-dm-trend" in html)

r90 = client.get("/instagram-xabarlar?period=maximum")
check("'maksimal' davrda sahifa 200 qaytaradi", r90.status_code == 200)

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
else:
    print("ALL PASSED")
