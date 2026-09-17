"""test_competitor_ad_library_offline.py — 2026-09, foydalanuvchi so'rovi:
"va qo'shimcha misol ad library qo'shsak bo'lar ekan raqobatchilar qo'shish
mumkin bo'lsin, har 2 kunda bir raqobatchilani analiz qilsin va tahlilni
bot orqali yuborsin ad library interfeysini ulab qo'yish kerak bizaga ham
bo'lim yaratib u yerda yulduzcha orqali bizneslani akkauntlani saqlash va
nimaga targte yoqkan toqi ko'rish mummkin bo'lsin va oson bo'sin".

Tekshiradi:
  1. `/settings/competitors?q=...` -- jonli Ad Library qidiruvi (meta_api
     mock qilingan), natijalar sahifada ko'rinadi, allaqachon kuzatuvda
     bo'lgan sahifa "★ Kuzatuvda" deb belgilanadi.
  2. "star_add" POST -- qidiruv natijasidan "☆ Kuzatuvga qo'sh" bosilsa
     yangi `Competitor` yaratiladi; xuddi shu nom qaytadan qo'shilsa
     (yoki oddiy "add" orqali) DUBLIKAT yaratilmaydi.
  3. `/settings/competitors/<id>` tafsilot sahifasi -- hali tahlil
     qilinmagan holatda ochiladi, "Hozir tekshir" POST'i tahlil yaratadi
     va `Competitor.last_analyzed_at`ni yangilaydi.
  4. `competitor_analytics.analyze_due_competitor()` -- 2 kunlik AYLANMA
     (rotation) tanlov: hali tahlil qilinmagan raqobatchi birinchi
     tanlanadi; shu ondan keyin (hali 2 kun o'tmagan) qayta chaqirilsa --
     hech narsa tanlamaydi (navbat kelmagan).
  5. Ikki xil kompaniya BIR XIL nomli raqobatchini alohida-alohida
     qo'sha olishi (dedup FAQAT o'z kompaniyasi ichida ishlashi) kerak.
  6. Tafsilot sahifasi (`/settings/competitors/<id>`) boshqa kompaniyaning
     raqobatchi ID'sini to'g'ridan-to'g'ri URL orqali ochishga urinsa 404
     qaytarishi kerak (multi-tenant sizib chiqishga qarshi).

Ishga tushirish:
    cd app && python3 scripts/test_competitor_ad_library_offline.py
"""
import os
import sys
import tempfile
import datetime as dt
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("META_ACCESS_TOKEN", "test-dummy-token")
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_test_dummy")
os.environ.setdefault("META_PAGE_ID", "page_test_dummy")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

_TMPDIR = tempfile.mkdtemp()
_DB_PATH = os.path.join(_TMPDIR, "test_competitor_ad_library.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402
import meta_api  # noqa: E402
import competitor_analytics  # noqa: E402

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
    company_a = db_module.Company(name="Kompaniya A", is_active=True, plan="unlimited")
    company_b = db_module.Company(name="Kompaniya B", is_active=True, plan="unlimited")
    session.add_all([company_a, company_b])
    session.commit()
    company_a_id, company_b_id = company_a.id, company_b.id

    admin_a = db_module.Manager(username="cad_admin_a", full_name="Admin A", role="admin", company_id=company_a_id)
    admin_a.set_password("parol123")
    session.add(admin_a)

    admin_b = db_module.Manager(username="cad_admin_b", full_name="Admin B", role="admin", company_id=company_b_id)
    admin_b.set_password("parol123")
    session.add(admin_b)
    session.commit()
finally:
    session.close()

client_a = app_module.app.test_client()
client_a.post("/login", data={"username": "cad_admin_a", "password": "parol123"})

client_b = app_module.app.test_client()
client_b.post("/login", data={"username": "cad_admin_b", "password": "parol123"})

FAKE_AD_LIBRARY_RESULTS = [
    {
        "id": "ext-search-1", "page_name": "Arboss", "ad_snapshot_url": "https://facebook.com/ads/1",
        "ad_creative_bodies": ["30% chegirma -- faqat shu hafta!"],
        "ad_delivery_start_time": "2026-09-01T00:00:00+0000", "ad_delivery_stop_time": None,
    },
    {
        "id": "ext-search-2", "page_name": "Arboss", "ad_snapshot_url": "https://facebook.com/ads/2",
        "ad_creative_bodies": ["Yangi kolleksiya keldi"],
        "ad_delivery_start_time": "2026-09-05T00:00:00+0000", "ad_delivery_stop_time": None,
    },
]

# --- 1-2. Jonli qidiruv + yulduzcha orqali qo'shish ---
with mock.patch.object(meta_api, "search_ad_library", return_value=FAKE_AD_LIBRARY_RESULTS):
    r = client_a.get("/settings/competitors?q=Arboss")
    html = r.get_data(as_text=True)
    # 2026-09, ko'p tillilik: bu matn endi `t()` orqali chiqadi va Jinja
    # autoescape apostrofni `&#39;` qilib yozadi (brauzerda bir xil
    # ko'rinadi) -- solishtirishdan oldin qaytariladi.
    html_norm = html.replace("&#39;", "'")
    check("qidiruv sahifasi 200 qaytaradi", r.status_code == 200)
    check("qidiruv natijasida sahifa nomi ko'rinadi", "Arboss" in html)
    check("qidiruv natijasida reklama matni ko'rinadi", "chegirma" in html)
    check("duplikatsiz -- bitta page_name uchun bitta qator", html_norm.count("☆ Kuzatuvga qo'sh") == 1)
    check("Ad Library'ga o'xshab IKKALA namuna reklama ham ko'rinadi (2 variant)", "chegirma" in html and "Yangi kolleksiya" in html)

    r = client_a.post("/settings/competitors", data={
        "action": "star_add", "name": "Arboss", "search_term": "Arboss", "q": "Arboss",
    }, follow_redirects=True)
    check("yulduzcha orqali qo'shish 200 qaytaradi", r.status_code == 200)
    check("yulduzcha orqali qo'shilgani xabar beriladi", "ro&#39;yxatiga qo&#39;shildi" in r.get_data(as_text=True) or "qo'shildi" in r.get_data(as_text=True))

    session = db_module.get_session()
    try:
        with db_module.scoped_as(company_a_id):
            comps = session.query(db_module.Competitor).filter_by(name="Arboss").all()
        check("Kompaniya A'da aynan bitta 'Arboss' yaratildi", len(comps) == 1)
        competitor_id = comps[0].id
    finally:
        session.close()

    # Qayta bosilsa -- dublikat yaratilmasligi kerak.
    r = client_a.post("/settings/competitors", data={
        "action": "star_add", "name": "Arboss", "search_term": "Arboss", "q": "Arboss",
    }, follow_redirects=True)
    check("qayta bosilganda 'allaqachon bor' xabari", "allaqachon" in r.get_data(as_text=True))
    session = db_module.get_session()
    try:
        with db_module.scoped_as(company_a_id):
            comps_after = session.query(db_module.Competitor).filter_by(name="Arboss").count()
        check("qayta bosilgandan keyin ham FAQAT bitta 'Arboss' bor (dublikat yo'q)", comps_after == 1)
    finally:
        session.close()

    # Qidiruvda endi "★ Kuzatuvda" ko'rsatilishi kerak.
    r = client_a.get("/settings/competitors?q=Arboss")
    check("kuzatuvga qo'shilgandan keyin qidiruv natijasida '★ Kuzatuvda' belgisi bor", "Kuzatuvda" in r.get_data(as_text=True))

# --- 5. Ikki kompaniya bir xil nomni alohida qo'sha oladi ---
r = client_b.post("/settings/competitors", data={"action": "add", "name": "Arboss"}, follow_redirects=True)
check("Kompaniya B ham 'Arboss' nomini qo'sha oladi (dedup faqat o'z kompaniyasida)", "qo'shildi" in r.get_data(as_text=True) or "shildi" in r.get_data(as_text=True))
session = db_module.get_session()
try:
    with db_module.unscoped():
        total_arboss = session.query(db_module.Competitor).filter_by(name="Arboss").count()
    check("bazada jami 2 ta 'Arboss' bor -- ikkala kompaniyaga alohida", total_arboss == 2)
finally:
    session.close()

# --- 3. Tafsilot sahifasi + "Hozir tekshir" ---
r = client_a.get(f"/settings/competitors/{competitor_id}")
check("tafsilot sahifasi 200 qaytaradi", r.status_code == 200)
check("hali tahlil qilinmagan holat ko'rsatiladi", "hali tahlil qilinmagan" in r.get_data(as_text=True))

with mock.patch.object(meta_api, "search_ad_library", return_value=FAKE_AD_LIBRARY_RESULTS):
    r = client_a.post(f"/settings/competitors/{competitor_id}/analyze-now", follow_redirects=True)
    check("'Hozir tekshir' 200 qaytaradi", r.status_code == 200)
    check("'Hozir tekshir' muvaffaqiyat xabari", "yangilandi" in r.get_data(as_text=True))
    check("tafsilot sahifasida reklama matni endi ko'rinadi", "chegirma" in r.get_data(as_text=True).lower() or "Yangi kolleksiya" in r.get_data(as_text=True))

session = db_module.get_session()
try:
    with db_module.scoped_as(company_a_id):
        comp = session.get(db_module.Competitor, competitor_id)
        check("'Hozir tekshir' Competitor.last_analyzed_at'ni yangiladi", comp.last_analyzed_at is not None)
        analyses = session.query(db_module.CompetitorAnalysis).filter_by(competitor_id=competitor_id).all()
        check("CompetitorAnalysis yozuvi yaratildi", len(analyses) == 1)
        check("CompetitorAnalysis.ads_analyzed_count to'g'ri", analyses[0].ads_analyzed_count == 2)
finally:
    session.close()

# --- 4. Aylanma (rotation) tanlov mantig'i -- ALOHIDA (yangi) kompaniyada,
# oldingi bosqichlarning (Kompaniya B'ga qo'shilgan "Arboss") navbatga
# aralashmasligi uchun.
session = db_module.get_session()
try:
    company_c = db_module.Company(name="Kompaniya C", is_active=True, plan="unlimited")
    session.add(company_c)
    session.commit()
    company_c_id = company_c.id
    with db_module.scoped_as(company_c_id):
        never_analyzed = db_module.Competitor(name="Hech qachon tekshirilmagan", company_id=company_c_id, is_active=True)
        recently_analyzed = db_module.Competitor(
            name="Yaqinda tekshirilgan", company_id=company_c_id, is_active=True,
            last_analyzed_at=dt.datetime.utcnow() - dt.timedelta(hours=3),
        )
        session.add_all([never_analyzed, recently_analyzed])
        session.commit()
        never_id, recent_id = never_analyzed.id, recently_analyzed.id
finally:
    session.close()

with mock.patch.object(meta_api, "search_ad_library", return_value=[]):
    with db_module.scoped_as(company_c_id):
        result = competitor_analytics.analyze_due_competitor()
    check("navbatdagi tanlov -- hali tekshirilmagan raqobatchi tanlanadi", result is not None and result["competitor_id"] == never_id)

    # Yaqinda (2 kundan kam oldin) tekshirilgan raqobatchi -- hozircha
    # NAVBATGA tushmasligi kerak, faqat "hech qachon tekshirilmagan" bor
    # edi va endi u ham "hozirgina tekshirilgan" holatga o'tdi -- demak
    # ikkinchi chaqiruvda HECH KIM navbatda bo'lmasligi kerak.
    with db_module.scoped_as(company_c_id):
        result2 = competitor_analytics.analyze_due_competitor()
    check("ikkinchi chaqiruvda navbat kelmagan -- None qaytadi", result2 is None)

# --- 6. Tafsilot sahifasi boshqa kompaniyaning raqobatchisini SIZDIRMAYDI
# (Competitor.id topib olib to'g'ridan-to'g'ri URL orqali kirishga urinish).
r = client_b.get(f"/settings/competitors/{competitor_id}")  # competitor_id -- Kompaniya A'ning "Arboss"si
check("Kompaniya B, Kompaniya A'ning raqobatchi ID'sini to'g'ridan-to'g'ri ochsa 404 oladi", r.status_code == 404)

# --- 7. 2026-09, foydalanuvchi so'rovi: qidiruv natijasi Ad Library'ga
# o'xshab (a) bitta sahifa uchun ko'pi bilan 2 ta namuna reklama bilan
# cheklansin, (b) "hozir reklamasi bor/yo'q" holati BARCHA topilgan
# reklamalar (faqat ko'rsatiladigan 2 tasi emas) asosida aniqlansin, (c)
# sahifaning haqiqiy logotipi/obunachilar soni ko'rinsin (fayk
# akkauntlardan ajratish uchun), (d) bitta sahifaning profili
# olinmasa ham (masalan Meta xatosi) butun qidiruv ishlashda davom etsin.
FAKE_MULTI_AD_RESULTS = [
    {"id": "m1", "page_id": "pgid-1", "page_name": "Multibrand", "ad_snapshot_url": "https://facebook.com/ads/m1",
     "ad_creative_bodies": ["Birinchi aksiya"], "ad_delivery_stop_time": "2026-08-01T00:00:00+0000"},
    {"id": "m2", "page_id": "pgid-1", "page_name": "Multibrand", "ad_snapshot_url": "https://facebook.com/ads/m2",
     "ad_creative_bodies": ["Ikkinchi aksiya"], "ad_delivery_stop_time": "2026-08-05T00:00:00+0000"},
    # Uchinchi reklama -- HALI TUGAMAGAN (stop_time yo'q) -- ko'rsatiladigan
    # 2 tadan tashqarida qolsa ham, "hozir reklamasi bor" holatiga ta'sir
    # qilishi kerak.
    {"id": "m3", "page_id": "pgid-1", "page_name": "Multibrand", "ad_snapshot_url": "https://facebook.com/ads/m3",
     "ad_creative_bodies": ["Uchinchi, hali faol aksiya"], "ad_delivery_stop_time": None},
]
with mock.patch.object(meta_api, "search_ad_library", return_value=FAKE_MULTI_AD_RESULTS), \
     mock.patch.object(meta_api, "get_page_public_profile", return_value={"picture_url": "https://example.com/logo.png", "fan_count": 12400}):
    r = client_a.get("/settings/competitors?q=Multibrand")
    html = r.get_data(as_text=True)
    check("ko'pi bilan 2 ta namuna reklama ko'rsatiladi (3-si emas)", "Birinchi aksiya" in html and "Ikkinchi aksiya" in html and "Uchinchi, hali faol aksiya" not in html)
    check("faol reklama SONI -- ko'rsatilmagan 3-reklama ham hisobga olindi (1 ta faol)", "Hozir 1 ta reklama yoqilgan" in html)
    check("sahifa logotipi (picture_url) <img> sifatida chiqadi", 'src="https://example.com/logo.png"' in html)
    check("obunachilar soni ko'rinadi", "12 400 obunachi" in html)

# Barcha ad'lar TUGAGAN (stop_time bor) bo'lsa -- "hozir reklamasi yo'q".
FAKE_INACTIVE_RESULTS = [
    {"id": "i1", "page_id": "pgid-2", "page_name": "Nofaol Brend", "ad_snapshot_url": None,
     "ad_creative_bodies": ["Eski aksiya"], "ad_delivery_stop_time": "2026-01-01T00:00:00+0000"},
]
with mock.patch.object(meta_api, "search_ad_library", return_value=FAKE_INACTIVE_RESULTS), \
     mock.patch.object(meta_api, "get_page_public_profile", side_effect=meta_api.MetaAPIError({"message": "vaqtinchalik xato"})):
    r = client_a.get("/settings/competitors?q=Nofaol")
    html = r.get_data(as_text=True)
    check("barcha reklamalar tugagan bo'lsa 'hozir reklamasi yo'q'", "Hozir reklamasi yo'q" in html.replace("&#39;", "'"))
    check("profil olishda xato bo'lsa ham sahifa 200 qaytaradi (butun qidiruv to'xtamaydi)", r.status_code == 200)
    check("profil olinmasa harf-avatar'ga qaytadi (rasm yo'q)", 'cp-result-avatar-img' not in html)

# --- 8. 2026-09, foydalanuvchi so'rovi ("qidirilish -- yozishga qarab
# brandlar chiqib kelsin, logo/obunachi soni ko'rinib tursin"): yozayotganda
# ishlaydigan jonli taklif JSON endpointi (`/settings/competitors/live-search`).
with mock.patch.object(meta_api, "search_ad_library", return_value=FAKE_MULTI_AD_RESULTS), \
     mock.patch.object(meta_api, "get_page_public_profile", return_value={"picture_url": "https://example.com/logo.png", "fan_count": 12400}):
    r = client_a.get("/settings/competitors/live-search?q=Multibrand")
    check("live-search 200 qaytaradi", r.status_code == 200)
    payload = r.get_json()
    check("live-search JSON tuzilishi to'g'ri (results ro'yxati)", isinstance(payload, dict) and isinstance(payload.get("results"), list))
    results = payload.get("results") or []
    check("live-search kamida bitta natija qaytaradi", len(results) >= 1)
    if results:
        first = results[0]
        check("live-search natijasida page_name bor", first.get("page_name") == "Multibrand")
        check("live-search natijasida active_count to'g'ri hisoblangan (1 ta faol)", first.get("active_count") == 1)
        check("live-search natijasida obunachilar soni ko'rinadi", first.get("fan_count_display") == "12 400 obunachi")
        check("live-search natijasida reklama namunalari YO'Q (yengil panel)", "ads" not in first)

r = client_a.get("/settings/competitors/live-search?q=a")
check("live-search 2 harfdan qisqa so'rovda bo'sh natija qaytaradi (ortiqcha so'rovlarni oldini olish)", r.get_json() == {"results": []})

with mock.patch.object(meta_api, "search_ad_library", return_value=[]):
    r = client_a.get("/settings/competitors/live-search?q=Hechnarsa")
    check("live-search natija topilmasa bo'sh ro'yxat qaytaradi, xato emas", r.status_code == 200 and r.get_json() == {"results": []})

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
else:
    print("ALL PASSED")
