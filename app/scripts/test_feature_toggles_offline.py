"""test_feature_toggles_offline.py — 2026-09, foydalanuvchi so'rovi:
"funksionalni ochirib turish mumkin bolsin" -- kompaniya admini Sozlamalar
sahifasidan (1) tarifiga kirgan istalgan bo'limni (masalan "Target") butun
kompaniya uchun o'chirib qo'ya olishi, va (2) AI-yordamchi vidjetini
o'chirib qo'ya olishi kerak.

Tekshiradi:
  - Admin /sozlamalar orqali "target" modulini o'chirsa -- has_module
    ADMIN uchun ham False qaytaradi (kompaniya darajasidagi cheklov,
    shaxsiy emas), sidebar/page-subnav'da "Target" ko'rinmay qoladi,
    /target route'iga kirishga urinish rad etiladi.
  - "settings" moduli HECH QACHON o'chirilishi mumkin emas (o'z-o'zini
    qulflab qo'yishning oldini olish) -- tampered so'rov bilan ham.
  - Qayta yoqilgandan keyin "target" yana ko'rinadi.
  - AI-yordamchini o'chirish: `company_ai_enabled` (context processor)
    False bo'lib qoladi (vidjet sahifadan yo'qoladi), /individual-tekshirish
    (Audio) sahifasi bunga bog'liq bo'lmagan holda ishlashda davom etadi
    (2026-09: AI qo'ng'iroq-tahlili butunlay olib tashlangan, shu sabab bu
    sahifa endi ai_features_disabled'ga umuman bog'liq emas).

Ishga tushirish:
    cd app && python3 scripts/test_feature_toggles_offline.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMPDIR = tempfile.mkdtemp()
_DB_PATH = os.path.join(_TMPDIR, "test_toggles.db")

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("OPENAI_API_KEY", "test-dummy-openai-key")
os.environ.setdefault("META_ACCESS_TOKEN", "test-dummy-token")
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_test_dummy")
os.environ.setdefault("META_PAGE_ID", "page_test_dummy")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
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


_session = db_module.get_session()
try:
    company1 = _session.query(db_module.Company).order_by(db_module.Company.id.asc()).first()
    company1.plan = "unlimited"  # target/individual_check/AI hammasi ochiq bo'lsin

    admin = db_module.Manager(username="toggle_admin", full_name="Admin", role="admin", company_id=company1.id)
    admin.set_password("parol123")
    _session.add(admin)
    _session.commit()
    company1_id = company1.id
finally:
    _session.close()

client = app_module.app.test_client()
client.post("/login", data={"username": "toggle_admin", "password": "parol123"})

# --- 1. Boshida "target" ko'rinadi ---
html_target_page = client.get("/target").status_code
check("boshida /target ochiladi (302 emas, redirect yo'q)", html_target_page == 200)

# --- 2. Admin "target"ni o'chiradi (boshqa hamma modulni yoqiq qoldirib) ---
remaining = [k for k in permissions.TOGGLEABLE_MODULE_KEYS if k != "target"]
resp = client.post("/sozlamalar/funksiyalar", data={"action": "set_disabled_modules", "enabled_modules": remaining}, follow_redirects=True)
check("set_disabled_modules so'rovi muvaffaqiyatli", resp.status_code == 200)

sidebar_html = client.get("/").get_data(as_text=True)
check("Target o'chirilgach sidebar'da 'Target' bo'limi yo'q", 'href="/target"' not in sidebar_html)

target_resp = client.get("/target", follow_redirects=False)
check("Target o'chirilgach /target endi ochilmaydi (redirect)", target_resp.status_code in (302, 303))

# --- 3. "settings" hech qachon o'chmaydi (tampered so'rov bilan ham) ---
tamper_resp = client.post("/sozlamalar/funksiyalar", data={"action": "set_disabled_modules", "enabled_modules": []}, follow_redirects=True)
settings_still_ok = client.get("/sozlamalar", follow_redirects=False).status_code == 200
check("'settings' tampered so'rov bilan ham o'chmaydi (o'zini qulflab qo'yish oldi olingan)", settings_still_ok)

# --- 4. Qayta yoqish ---
client.post("/sozlamalar/funksiyalar", data={"action": "set_disabled_modules", "enabled_modules": permissions.TOGGLEABLE_MODULE_KEYS}, follow_redirects=True)
target_resp2 = client.get("/target", follow_redirects=False)
check("Target qayta yoqilgach /target ochiladi", target_resp2.status_code == 200)

# --- 5. Company1'ning AI funksiyalarini (AI-yordamchi vidjeti)
# Sozlamalardan o'chirish -- vidjet yashirilishi kerak, lekin Audio
# sahifasi (2026-09: AI qo'ng'iroq-tahlili butunlay olib tashlangan) bunga
# bog'liq bo'lmagan holda ishlashda davom etishi kerak. ---
dash_before = client.get("/").get_data(as_text=True)
check("AI o'chirilmaguncha AI-yordamchi vidjeti sahifada bor", 'id="ai-assistant-root"' in dash_before)

client.post("/sozlamalar/ai", data={"action": "toggle_ai_features", "ai_features_disabled": "1"}, follow_redirects=True)

dash_disabled = client.get("/").get_data(as_text=True)
check("AI o'chirilgach AI-yordamchi vidjeti sahifadan yo'qoladi", 'id="ai-assistant-root"' not in dash_disabled)

ic_resp = client.get("/individual-tekshirish", follow_redirects=True)
check("AI-yordamchi o'chirilgan holda ham Audio sahifasi ochiladi (AI tahlilga bog'liq emas)", ic_resp.status_code == 200)

# --- 6. AI qayta yoqilgach vidjet qaytadi ---
client.post("/sozlamalar/ai", data={"action": "toggle_ai_features", "ai_features_disabled": "0"}, follow_redirects=True)
dash_after = client.get("/").get_data(as_text=True)
check("AI qayta yoqilgach AI-yordamchi vidjeti qaytadi", 'id="ai-assistant-root"' in dash_after)

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
else:
    print("ALL PASSED")
