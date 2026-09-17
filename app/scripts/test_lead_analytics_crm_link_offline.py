"""test_lead_analytics_crm_link_offline.py — 2026-09, foydalanuvchi so'rovi:

  1. "Lead Analytics" endi sidebar'da ALOHIDA, mustaqil band (avval
     Marketing guruhi ichida, Target/SMM bilan bir tab qatorida edi).
  2. Kalendar-sana tanlagich: bitta sana tanlaganda menyu YOPILIB
     QOLMASLIGI kerak (ikkinchi sanani ham tanlash uchun) -- avvalgi
     bug: `render()` bosilgan katakchani DOM'dan uzardi, shu sabab
     tashqi "click-outside" tinglagichi menyuni yopib qo'yardi.
  3. Lead Analytics jadvalidagi target(kampaniya)larni checkbox bilan
     tanlab, "CRM'ga o'tish" tugmasi bosilsa -- CRM (`/leads`) FAQAT shu
     target(lar)dan, joriy davrda kelgan lidlarni ko'rsatishi kerak.
  4. Lead Analytics'da ham Target sahifasidagi kabi "Faqat yoqilganlarni
     ko'rsatish / Hammasini ko'rsatish" almashtirgichi bo'lishi kerak
     (`active_only`/`show_all`).
  5. ACT/WON/LOSS/SPEND/LEADS/CONV.RATE/DEAL TIME kabi inglizcha
     ustun/yorliq nomlari o'zbekchaga o'girilgan bo'lishi kerak.

Ishga tushirish:
    cd app && python3 scripts/test_lead_analytics_crm_link_offline.py
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
_DB_PATH = os.path.join(_TMPDIR, "test_la_crm_link.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402

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
    company = db_module.Company(name="LA CRM Link Test", is_active=True, plan="unlimited")
    session.add(company)
    session.commit()
    company_id = company.id

    admin = db_module.Manager(username="la_admin", full_name="Admin", role="admin", company_id=company_id)
    admin.set_password("parol123")
    session.add(admin)
    session.commit()

    # Ikki xil kampaniya'dan lidlar -- filtr FAQAT tanlangan campaign_id'ga
    # tegishlilarini qaytarishi kerakligini tekshirish uchun.
    l1 = db_module.Lead(company_id=company_id, full_name="Lead A", phone="+998900000001",
                         campaign_id="camp_1", campaign_name="Kampaniya 1", status="new", source="meta")
    l2 = db_module.Lead(company_id=company_id, full_name="Lead B", phone="+998900000002",
                         campaign_id="camp_2", campaign_name="Kampaniya 2", status="new", source="meta")
    session.add_all([l1, l2])
    session.commit()
finally:
    session.close()

client = app_module.app.test_client()
client.post("/login", data={"username": "la_admin", "password": "parol123"})

# --- 1. Lead Analytics endi sidebar'da alohida, Marketing subnav'ida YO'Q ---
r = client.get("/target")
html = r.get_data(as_text=True)
check("Target sahifasi 200 qaytaradi", r.status_code == 200)
check("Marketing subnav'ida 'Lead Analytics' YO'Q (endi alohida sidebar band)", "Lead Analytics" not in html.split('page-subnav')[1].split('</nav>')[0] if 'page-subnav' in html else True)

r = client.get("/lead-analytics")
# 3-bosqich, ko'p tillilik: matnlar endi t() orqali chiqadi, Jinja
# autoescape ' belgisini &#39;ga aylantiradi -- literal solishtirish
# uchun normalizatsiya (avvalgi bosqichlardagi bilan bir xil yondashuv).
html = r.get_data(as_text=True).replace("&#39;", "'")
check("/lead-analytics 200 qaytaradi", r.status_code == 200)
check("Sidebar'da 'Lead Analytics' mustaqil nav-item sifatida bor", 'data-tooltip="Lead Analytics"' in html)

# --- 2. Kalendar-sana tanlagich JS: stopPropagation regressiya nazorati ---
for fname in ("_calendar_date_picker.html", "_lead_analytics_date_picker.html"):
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", fname)
    src = open(path, encoding="utf-8").read()
    day_cell_block = src.split("la-dp-cal-day[data-date]").pop()
    check(f"{fname}: sana katakchasi bosilganda stopPropagation() chaqiriladi (menyu erta yopilmasligi uchun)",
          "e.stopPropagation()" in day_cell_block.split("});")[0])

# --- 3. checkbox + "CRM'ga o'tish" tugmasi HTML'da bor ---
check("Har bir qatorda checkbox (laRowSel) bor", 'class="laRowSel"' in html or "Ma'lumot yo'q" in html)
check("'CRM'ga o'tish' tugmasi bor", "CRM'ga o'tish" in html)
check("'Hammasini tanlash' checkbox bor", 'id="laSelectAll"' in html)

# --- 4. active_only/show_all almashtirgichi ---
check("'Hammasini ko'rsatish' yoki 'Faqat yoqilganlarni ko'rsatish' tugmasi bor", "ko'rsatish" in html and "toggle-all-btn" in html)
r_show_all = client.get("/lead-analytics?show_all=1")
check("show_all=1 bilan ham 200 qaytaradi", r_show_all.status_code == 200)

# --- 5. Inglizcha yorliqlar o'zbekchaga o'girilgan ---
for bad_label in (">ACT<", ">WON<", ">LOSS<", ">SPEND<", ">LEADS<", ">CONV.RATE<", ">DEAL TIME<", ">COST/WON<", "Campaigns<", "Ad sets<", "Ads<"):
    check(f"Jadval sarlavhasida endi '{bad_label.strip('<>')}' (inglizcha) YO'Q", bad_label not in html)
for good_label in ("Bog'landi", "Yutildi", "Yo'qotildi", "Xarajat", "Lidlar", "Konversiya", "Yopilish vaqti", "Yutuv narxi", "Kampaniyalar", "Reklama guruhlari", "Reklamalar"):
    check(f"O'zbekcha yorliq '{good_label}' bor", good_label in html)

# --- 6. CRM (`/leads`) campaign_id bo'yicha to'g'ri filtrlaydi ---
# DIQQAT: oddiy "Lead A" in html tekshiruvi noto'g'ri natija beradi --
# "Lead A" satri sidebar'dagi "Lead Analytics" so'zining ICHIDA HAM
# uchraydi (substring). Shuning uchun aniq `>Lead A<` (jadval katakchasi
# ichida, teglar bilan chegaralangan) qidiriladi.
r_all = client.get("/leads")
html_all = r_all.get_data(as_text=True)
check("Filtrsiz CRM'da ikkala lead ham bor", ">Lead A<" in html_all and ">Lead B<" in html_all)

r_filtered = client.get("/leads?campaign_id=camp_1")
html_filtered = r_filtered.get_data(as_text=True)
check("campaign_id=camp_1 bilan FAQAT Lead A ko'rinadi", ">Lead A<" in html_filtered and ">Lead B<" not in html_filtered)
check("Filtr paneli 'Target (kampaniya)' bo'limini ko'rsatadi", "Target (kampaniya)" in html_filtered)
check("Filtr badge soni to'g'ri (1ta campaign_id)", 'badge-color-blue" style="margin-left:2px">1<' in html_filtered)

r_filtered2 = client.get("/leads?campaign_id=camp_2")
html_filtered2 = r_filtered2.get_data(as_text=True)
check("campaign_id=camp_2 bilan FAQAT Lead B ko'rinadi", ">Lead B<" in html_filtered2 and ">Lead A<" not in html_filtered2)

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
else:
    print("ALL PASSED")
