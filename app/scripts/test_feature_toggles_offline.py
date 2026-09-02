"""test_feature_toggles_offline.py — 2026-09, foydalanuvchi so'rovi:
"funksionalni ochirib turish mumkin bolsin misol audio tahlilini ochirib
turish mumkin bolsin" -- kompaniya admini Sozlamalar sahifasidan (1)
tarifiga kirgan istalgan bo'limni (masalan "Target") butun kompaniya
uchun o'chirib qo'ya olishi, va (2) AI funksiyalarini (qo'ng'iroq tahlili
+ AI-yordamchi) BITTA tugma bilan o'chirib qo'ya olishi kerak.

Tekshiradi:
  - Admin /sozlamalar orqali "target" modulini o'chirsa -- has_module
    ADMIN uchun ham False qaytaradi (kompaniya darajasidagi cheklov,
    shaxsiy emas), sidebar/page-subnav'da "Target" ko'rinmay qoladi,
    /target route'iga kirishga urinish rad etiladi.
  - "settings" moduli HECH QACHON o'chirilishi mumkin emas (o'z-o'zini
    qulflab qo'yishning oldini olish) -- tampered so'rov bilan ham.
  - Qayta yoqilgandan keyin "target" yana ko'rinadi.
  - AI funksiyalarini o'chirish: `company_ai_enabled` (context processor)
    False bo'lib qoladi, /individual-tekshirish sahifasida "AI analiz"
    tab'i "o'chirilgan" deb ko'rsatiladi va ?tab=ai so'ralsa ham xom
    qo'ng'iroqlar tab'iga qaytariladi.
  - `call_analysis.run_pending_analysis` AI o'chirilgan kompaniyaning
    qo'ng'iroqlarini o'TKAZIB YUBORADI (fon vazifasida ham xarajat
    to'xtashi kerak, faqat sahifada yashirilmasin).

Ishga tushirish:
    cd app && python3 scripts/test_feature_toggles_offline.py
"""
import os
import sys
import tempfile
import datetime as dt

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
import call_analysis  # noqa: E402
import permissions  # noqa: E402

app_module.app.config["TESTING"] = True
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

    company2 = db_module.Company(name="Boshqa kompaniya", plan="unlimited", ai_features_disabled=True)
    _session.add(company2)
    _session.commit()

    # Company1 (AI yoqiq) va Company2 (AI o'chirilgan)ga bittadan "haqiqiy" qo'ng'iroq
    call_ok = db_module.CallRecord(
        company_id=company1.id, recording_url="https://example.com/rec1.mp3",
        duration_seconds=120, started_at=dt.datetime.utcnow(),
    )
    call_disabled_company = db_module.CallRecord(
        company_id=company2.id, recording_url="https://example.com/rec2.mp3",
        duration_seconds=120, started_at=dt.datetime.utcnow(),
    )
    _session.add_all([call_ok, call_disabled_company])
    _session.commit()
    company1_id, call_ok_id, call_disabled_id = company1.id, call_ok.id, call_disabled_company.id
finally:
    _session.close()

client = app_module.app.test_client()
client.post("/login", data={"username": "toggle_admin", "password": "parol123"})

# --- 1. Boshida "target" ko'rinadi ---
html_target_page = client.get("/target").status_code
check("boshida /target ochiladi (302 emas, redirect yo'q)", html_target_page == 200)

# --- 2. Admin "target"ni o'chiradi (boshqa hamma modulni yoqiq qoldirib) ---
remaining = [k for k in permissions.TOGGLEABLE_MODULE_KEYS if k != "target"]
resp = client.post("/sozlamalar", data={"action": "set_disabled_modules", "enabled_modules": remaining}, follow_redirects=True)
check("set_disabled_modules so'rovi muvaffaqiyatli", resp.status_code == 200)

sidebar_html = client.get("/").get_data(as_text=True)
check("Target o'chirilgach sidebar'da 'Target' bo'limi yo'q", 'href="/target"' not in sidebar_html)

target_resp = client.get("/target", follow_redirects=False)
check("Target o'chirilgach /target endi ochilmaydi (redirect)", target_resp.status_code in (302, 303))

# --- 3. "settings" hech qachon o'chmaydi (tampered so'rov bilan ham) ---
tamper_resp = client.post("/sozlamalar", data={"action": "set_disabled_modules", "enabled_modules": []}, follow_redirects=True)
settings_still_ok = client.get("/sozlamalar", follow_redirects=False).status_code == 200
check("'settings' tampered so'rov bilan ham o'chmaydi (o'zini qulflab qo'yish oldi olingan)", settings_still_ok)

# --- 4. Qayta yoqish ---
client.post("/sozlamalar", data={"action": "set_disabled_modules", "enabled_modules": permissions.TOGGLEABLE_MODULE_KEYS}, follow_redirects=True)
target_resp2 = client.get("/target", follow_redirects=False)
check("Target qayta yoqilgach /target ochiladi", target_resp2.status_code == 200)

# --- 5. Fon vazifasi (scheduler) AI O'CHIRILGAN kompaniyani o'tkazib
# yuboradi -- BU YERDA (company1 hali AI-yoqiq paytida) tekshiriladi,
# chunki keyingi qadam company1ning o'zining AI'sini o'chiradi. ---
_orig_analyze = call_analysis.analyze_call_record


def _fake_analyze(session, call):
    call.ai_analyzed_at = dt.datetime.utcnow()
    call.ai_score = 8
    session.commit()


call_analysis.analyze_call_record = _fake_analyze
run_session = db_module.get_session()
try:
    result = call_analysis.run_pending_analysis(run_session, limit=10)
finally:
    call_analysis.analyze_call_record = _orig_analyze
    run_session.close()

verify_session = db_module.get_session()
try:
    refreshed_ok = verify_session.get(db_module.CallRecord, call_ok_id)
    refreshed_disabled = verify_session.get(db_module.CallRecord, call_disabled_id)
    check("AI yoqiq kompaniyaning qo'ng'irog'i tahlil qilindi", refreshed_ok.ai_analyzed_at is not None)
    check("AI o'chirilgan kompaniyaning qo'ng'irog'i O'TKAZIB YUBORILDI (tahlil qilinmadi)", refreshed_disabled.ai_analyzed_at is None)
finally:
    verify_session.close()

# --- 6. Endi company1'ning O'ZINING AI funksiyalarini Sozlamalardan
# o'chirish -- AI-yordamchi vidjeti va "AI analiz" tab'i yashirilishi
# kerak. ---
dash_before = client.get("/").get_data(as_text=True)
check("AI o'chirilmaguncha AI-yordamchi vidjeti sahifada bor", 'id="ai-assistant-root"' in dash_before)

client.post("/sozlamalar", data={"action": "toggle_ai_features", "ai_features_disabled": "1"}, follow_redirects=True)

dash_disabled = client.get("/").get_data(as_text=True)
check("AI o'chirilgach AI-yordamchi vidjeti sahifadan yo'qoladi", 'id="ai-assistant-root"' not in dash_disabled)

ic_html = client.get("/individual-tekshirish?tab=ai", follow_redirects=True).get_data(as_text=True)
check("AI o'chirilgach ?tab=ai so'ralsa ham 'calls' tab'iga qaytariladi (redirect yo'q, ichkarida)", "o'chirilgan" in ic_html)

# --- 7. AI qayta yoqilgach vidjet qaytadi ---
client.post("/sozlamalar", data={"action": "toggle_ai_features", "ai_features_disabled": "0"}, follow_redirects=True)
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
