"""test_leads_period_filter_and_roas_offline.py — 2026-09, foydalanuvchi
so'rovi (skrinshot bilan, uchta alohida ish):

  1. "Faqat yoqilganlarni ko'rsatish" filtri avval target'ning HOZIRGI
     (joriy) statusiga qarardi -- o'tgan oyni tanlasa-yu, o'sha davrdagi
     target'lar hozir pauzada bo'lsa, ro'yxat bo'sh chiqib qolardi. Endi
     TANLANGAN DAVRDA real delivery (Meta insights'da qator borligi)
     bo'lishiga qaraydi (subtitle matni ham shunga mos yangilangan --
     pastda `dashboard_data.get_kpis()` darajasidagi tekshiruv
     `test_target_status_and_smm_periods_offline.py`da, bu yerda faqat
     sahifa-matni regressiyasi tekshiriladi).
  2. CRM (`/leads`) endi Lead Analytics'dagi kabi to'liq kalendar-sana
     tanlagichga ega (tezkor preset'lar + aniq oraliq) -- avvalgi oddiy
     `<input type=date>` juft o'rniga.
  3. Lead Analytics jadvalida "+ ROAS" tugmasi -- bosilganda ROAS ustuni
     ko'rsatiladi/yashiriladi (backend `dashboard_data.py`da `roas`
     maydoni allaqachon hisoblanadi).

Ishga tushirish:
    cd app && python3 scripts/test_leads_period_filter_and_roas_offline.py
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
_DB_PATH = os.path.join(_TMPDIR, "test_leads_period_roas.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402
import dashboard_data  # noqa: E402
import meta_api  # noqa: E402

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
    company = db_module.Company(name="Leads Period/ROAS Test", is_active=True, plan="unlimited")
    session.add(company)
    session.commit()
    company_id = company.id

    admin = db_module.Manager(username="lp_admin", full_name="Admin", role="admin", company_id=company_id)
    admin.set_password("parol123")
    session.add(admin)
    session.commit()

    # Ikkita lead, ikki xil sanada -- period preset filtri ("today"/
    # "maximum") to'g'ri ishlashini tekshirish uchun.
    import datetime as dt
    l_old = db_module.Lead(
        company_id=company_id, full_name="Eski Lead", phone="+998900000010",
        campaign_id="camp_old", campaign_name="Eski kampaniya", status="new", source="meta",
        created_at=dt.datetime.utcnow() - dt.timedelta(days=200),
    )
    l_new = db_module.Lead(
        company_id=company_id, full_name="Yangi Lead", phone="+998900000011",
        campaign_id="camp_new", campaign_name="Yangi kampaniya", status="new", source="meta",
        created_at=dt.datetime.utcnow(),
    )
    session.add_all([l_old, l_new])
    session.commit()
finally:
    session.close()

client = app_module.app.test_client()
client.post("/login", data={"username": "lp_admin", "password": "parol123"})

# --- 1a. Standart holatda (period berilmagan) -- barcha lidlar ko'rinadi (eski xatti-harakat saqlangan) ---
r = client.get("/leads")
html = r.get_data(as_text=True)
check("/leads standart holatda 200 qaytaradi", r.status_code == 200)
check("Standart holatda ESKI lead ham ko'rinadi (filtr yo'q, 'maximum')", ">Eski Lead<" in html)
check("Standart holatda YANGI lead ham ko'rinadi", ">Yangi Lead<" in html)

# --- 1b. period=today bilan -- faqat bugungi lead ko'rinishi kerak ---
r_today = client.get("/leads?period=today")
html_today = r_today.get_data(as_text=True)
check("period=today bilan 200 qaytaradi", r_today.status_code == 200)
check("period=today bilan YANGI lead ko'rinadi", ">Yangi Lead<" in html_today)
check("period=today bilan ESKI lead (200 kun oldingi) ko'rinmaydi", ">Eski Lead<" not in html_today)

# --- 2. CRM sahifasida umumiy kalendar-sana tanlagich (Lead Analytics bilan bir xil komponent) bor ---
check("/leads sahifasida kalendar-sana tanlagich (cdp-leads) bor", 'id="cdp-leads"' in html)
check("/leads sahifasida tezkor preset tugmalari bor ('Bugun')", 'data-preset="today"' in html)
check("/leads sahifasida 'Maksimal' preset'i bor", 'data-preset="maximum"' in html)
# Eski oddiy <input type=date> juft endi Filtr panelida YO'Q (yashirin maydonlarga almashtirildi)
check("Filtr panelidagi eski qo'lda sana-kiritish maydoni olib tashlangan", '<label>Sana oralig' not in html)

# --- 3. Lead Analytics: "+ ROAS" tugmasi va ROAS ustuni ---
r_la = client.get("/lead-analytics")
html_la = r_la.get_data(as_text=True)
check("/lead-analytics 200 qaytaradi", r_la.status_code == 200)
check("'+ ROAS' tugmasi (id=laToggleRoas) bor", 'id="laToggleRoas"' in html_la)
check("ROAS ustun sarlavhasi jadvalda bor (standart holda yashirin)", 'data-key="roas"' in html_la)
check("ROAS ustuni standart holda 'hidden' bilan belgilangan (yashirin boshlanadi)", 'la-roas-col" data-key="roas" data-type="num" hidden' in html_la)

# --- 4. Subtitle matni endi "hozir YOQILGAN" demaydi, davr bo'yicha ekanini aytadi ---
check("Lead Analytics subtitle'ida endi 'hozir YOQILGAN' YO'Q (davr-asoslangan matn bilan almashtirilgan)", "hozir YOQILGAN" not in html_la)
check("Lead Analytics subtitle'ida 'tanlangan davrda yoqilgan' matni bor", "tanlangan davrda yoqilgan" in html_la)

r_target = client.get("/target")
html_target = r_target.get_data(as_text=True)
check("Target sahifasida ham endi 'hozir YOQILGAN' YO'Q", "hozir YOQILGAN" not in html_target)
check("Target sahifasida ham 'tanlangan davrda yoqilgan' matni bor", "tanlangan davrda yoqilgan" in html_target)

# --- 5. dashboard_data.get_kpis(): roas maydoni to'g'ri hisoblanadi ---
def fake_get_insights(level, date_preset, fields, access_token=None, ad_account_id=None, **kw):
    return [{"campaign_id": "cX", "campaign_name": "Test", "spend": 100.0, "impressions": 5, "reach": 4, "actions": []}]


def fake_get_account_structure(*a, **kw):
    return {"campaigns": [{"id": "cX", "name": "Test", "status": "ACTIVE", "effective_status": "ACTIVE", "objective": "OUTCOME_LEADS"}], "adsets": [], "ads": []}


real_get_insights = meta_api.get_insights
real_get_account_structure = meta_api.get_account_structure
meta_api.get_insights = fake_get_insights
meta_api.get_account_structure = fake_get_account_structure
try:
    result = dashboard_data.get_kpis(level="campaign", date_preset="last_30d", access_token="tok", ad_account_id="act_roas_test")
    row = result["rows"][0] if result["rows"] else {}
    # revenue=0 bo'lgani uchun roas=0.0 kutiladi (bo'lish-nolga emas, xato bermasligi kerak)
    check("roas maydoni natijada bor (xatosiz hisoblangan)", "roas" in row)
    check("Xarajat bor-u, sotuv yo'q holatda roas=0.0", row.get("roas") == 0.0)
    check("totals'da ham roas maydoni bor", "roas" in result["totals"])
finally:
    meta_api.get_insights = real_get_insights
    meta_api.get_account_structure = real_get_account_structure

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
else:
    print("ALL PASSED")
