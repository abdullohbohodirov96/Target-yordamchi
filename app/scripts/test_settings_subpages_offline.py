"""test_settings_subpages_offline.py — 2026-09, foydalanuvchi so'rovi:
"nastroyka joyini ... tepada bo'lim bo'lim qilib tashla, hamma narsani
bo'lim bo'yicha ... otdelniy ulash, ulash, ulash qilib" -- eski bitta
uzun "/sozlamalar" sahifasi (7 ta forma bitta joyda) endi FAQAT
kartochkali bosh sahifa, har bir bo'lim (Umumiy/CPL/Telegram/
Funksiyalar/AI/Javobsiz savollar) o'zining ALOHIDA sahifasiga (route)
ega.

Tekshiradi:
  - "/sozlamalar" (hub) GET -- kartochkalar bilan 200 qaytaradi, endi
    POST qabul qilmaydi (405).
  - Har bir yangi bo'lim sahifasi (GET) admin uchun 200 qaytaradi va
    o'ziga tegishli kontentni ko'rsatadi.
  - 5 ta admin-only bo'lim (CPL/Telegram/Funksiyalar/AI/Javobsiz
    savollar) oddiy menejer uchun (admin bo'lmagan, lekin "settings"
    moduliga ruxsati bor) dashboard'ga redirect qiladi ("faqat admin
    uchun" xabari bilan) -- "Umumiy" esa ochiladi.
  - Har bir bo'limning POST amali (`_handle_settings_post` orqali)
    to'g'ri ishlaydi va o'ziga qaytib redirect qiladi.

Ishga tushirish:
    cd app && python3 scripts/test_settings_subpages_offline.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("META_ACCESS_TOKEN", "test-dummy-token")
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_test_dummy")
os.environ.setdefault("META_PAGE_ID", "page_test_dummy")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

_TMPDIR = tempfile.mkdtemp()
_DB_PATH = os.path.join(_TMPDIR, "test_settings_subpages.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402
import permissions  # noqa: E402

app_module.app.config["TESTING"] = True
app_module.app.config["WTF_CSRF_ENABLED"] = False  # 2026-09, CSRF endi majburiy -- testlarda so'rovlar session-tashqarisida yasaladi
db_module.init_db()

failures = []


def check(name, cond):
    print(("OK  " if cond else "FAIL") + " " + name)
    if not cond:
        failures.append(name)


session = db_module.get_session()
try:
    company1 = session.query(db_module.Company).order_by(db_module.Company.id.asc()).first()
    company1.plan = "unlimited"

    admin = db_module.Manager(username="stg_admin", full_name="Admin", role="admin", company_id=company1.id)
    admin.set_password("parol123")
    session.add(admin)

    manager = db_module.Manager(
        username="stg_manager", full_name="Menejer", role="manager", company_id=company1.id,
        allowed_modules=permissions.serialize_allowed_modules(["dashboard", "settings"]),
    )
    manager.set_password("parol123")
    session.add(manager)

    unanswered = db_module.AssistantUnanswered(company_id=company1.id, manager_name="Admin", question="Test savol?")
    session.add(unanswered)
    session.commit()
    unanswered_id = unanswered.id
finally:
    session.close()

admin_client = app_module.app.test_client()
admin_client.post("/login", data={"username": "stg_admin", "password": "parol123"})

manager_client = app_module.app.test_client()
manager_client.post("/login", data={"username": "stg_manager", "password": "parol123"})

# --- 1. Hub sahifasi -- kartochkalar, endi POST qabul qilmaydi ---
hub_html = admin_client.get("/sozlamalar").get_data(as_text=True)
check("hub sahifasida 'Umumiy' kartochkasi bor", "Umumiy" in hub_html)
check("hub sahifasida CPL kartochkasi bor", "Target avtomatik o'chirish" in hub_html)
check("hub sahifasida Telegram kartochkasi bor", "Bildirishnomalar (Telegram)" in hub_html)
check("hub sahifasida javobsiz savollar kartochkasi bor", "javobsiz savollar" in hub_html)
hub_post = admin_client.post("/sozlamalar", data={"action": "set_min_sale", "min_sale_amount": "1000"})
check("hub endi POST qabul qilmaydi (405)", hub_post.status_code == 405)

# --- 2. Har bir bo'lim sahifasi admin uchun ochiladi ---
for ep, marker in [
    ("/sozlamalar/umumiy", "Minimal sotuv summasi"),
    ("/sozlamalar/cpl", "Maqsad/ideal CPL"),
    ("/sozlamalar/telegram", "userinfobot"),
    ("/sozlamalar/funksiyalar", "Funksiyalarni yoqish"),
    ("/sozlamalar/ai", "AI funksiyalari"),
    ("/sozlamalar/savollar", "Test savol?"),
]:
    r = admin_client.get(ep)
    check(f"{ep} admin uchun 200 qaytaradi", r.status_code == 200)
    check(f"{ep} kutilgan kontentni ko'rsatadi", marker in r.get_data(as_text=True))

# --- 3. Admin-only bo'limlar oddiy menejer uchun rad etiladi, Umumiy ochiladi ---
general_resp = manager_client.get("/sozlamalar/umumiy")
check("'Umumiy' menejer uchun ham ochiladi", general_resp.status_code == 200)

for ep in ("/sozlamalar/cpl", "/sozlamalar/telegram", "/sozlamalar/funksiyalar", "/sozlamalar/ai", "/sozlamalar/savollar"):
    r = manager_client.get(ep, follow_redirects=True)
    check(f"{ep} oddiy menejer uchun rad etiladi (dashboard'ga qaytariladi)", "faqat admin uchun" in r.get_data(as_text=True))

# --- 4. Har bir bo'limning POST amali to'g'ri ishlaydi ---
r = admin_client.post("/sozlamalar/umumiy", data={"action": "set_min_sale", "min_sale_amount": "50000"}, follow_redirects=True)
check("settings_general POST (set_min_sale) 200 qaytaradi", r.status_code == 200)
# Eslatma: flash matnidagi apostrof Jinja auto-escaping orqali "&#39;"ga
# aylanadi -- shu sabab tekshiruv apostrofsiz qism bilan solishtiradi.
check("settings_general POST muvaffaqiyat xabari", "Minimal sotuv summasi" in r.get_data(as_text=True) and "rnatildi" in r.get_data(as_text=True))

r = admin_client.post("/sozlamalar/cpl", data={
    "action": "set_cpl_rules", "target_cpa_usd": "5", "cpl_hard_kill_usd": "10",
    "cpl_hard_kill_min_spend_usd": "3", "cpl_hard_kill_zero_lead_usd": "8",
}, follow_redirects=True)
check("settings_cpl POST (set_cpl_rules) 200 qaytaradi", r.status_code == 200)
check("settings_cpl POST muvaffaqiyat xabari", "saqlandi" in r.get_data(as_text=True))

r = admin_client.post("/sozlamalar/telegram", data={"action": "set_telegram", "manager_id": "999999", "telegram_user_id": "12345"}, follow_redirects=True)
check("settings_telegram POST (noto'g'ri manager_id) 200 qaytaradi, xato ko'rsatiladi", r.status_code == 200 and "topilmadi" in r.get_data(as_text=True))

r = admin_client.post("/sozlamalar/funksiyalar", data={"action": "set_disabled_modules", "enabled_modules": permissions.TOGGLEABLE_MODULE_KEYS}, follow_redirects=True)
check("settings_modules POST (set_disabled_modules) 200 qaytaradi", r.status_code == 200)

r = admin_client.post("/sozlamalar/ai", data={"action": "toggle_ai_features", "ai_features_disabled": "1"}, follow_redirects=True)
check("settings_ai POST (toggle_ai_features) 200 qaytaradi", r.status_code == 200)
# Qayta yoqib qo'yamiz -- keyingi tekshiruvlarga ta'sir qilmasin
admin_client.post("/sozlamalar/ai", data={"action": "toggle_ai_features", "ai_features_disabled": "0"})

r = admin_client.post("/sozlamalar/savollar", data={"action": "resolve_unanswered", "question_id": str(unanswered_id)}, follow_redirects=True)
check("settings_unanswered POST (resolve_unanswered) 200 qaytaradi", r.status_code == 200)
check("savol hal qilingan deb belgilandi", "hal qilingan" in r.get_data(as_text=True))

# --- 5. Oddiy menejer admin-only bo'limga POST qilsa ham rad etiladi ---
r = manager_client.post("/sozlamalar/cpl", data={"action": "set_cpl_rules", "target_cpa_usd": "1"}, follow_redirects=True)
check("menejer CPL'ga POST qilsa rad etiladi (faqat admin)", "faqat admin" in r.get_data(as_text=True))

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
else:
    print("ALL PASSED")
