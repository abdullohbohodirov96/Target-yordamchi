"""test_business_profile_offline.py — 2026-09, foydalanuvchi so'rovi (1-bosqich):
"registratsiya bo'limida va nastroykada... kompaniya haqida ma'lumotlarni
qo'shish mumkin bo'lsin keyinchalik to'liq tahlil uchun... drop down...
har bitta yo'nalish bo'yicha... qo'shimcha izoh... kamida beshta savol...
imenno kompaniya haqida to'liq tushunib olish uchun AI va shu bo'yicha
javob bersin har doim."

2026-09 QAYTA ISHLASH (2-bosqich so'rovi): "biznes ochayotganda... yaratish
bosgandan keyin, otdelno oyinda chiqib kelsin... kamida beshta savol...
nechta sku... nastroykaga qo'shimcha joy qo'shish kerak... kompaniya
ma'lumotlari... agar to'ldirilmagan bo'lsa... yordamchi sms chiqib
kelsin". Bu fayl ENDI quyidagilarni tekshiradi:

  1. `business_profile.py`ning sof funksiyalari: serialize/parse round-trip,
     bo'sh javoblar tashlab yuboriladi, `business_profile_summary_text()`
     to'ldirilmagan bo'lsa `None`, to'ldirilgan bo'lsa AI promptiga
     qo'shiladigan formatlangan matn qaytaradi, `sku_count` savoli
     ro'yxatda bor, `is_profile_filled()` to'g'ri ishlaydi.
  2. `/signup` ENDI biznes-profil maydonlarini UMUMAN so'ramaydi (faqat
     hisob yaratadi) va muvaffaqiyatli bo'lsa `/xush-kelibsiz/biznes-profili`ga
     yo'naltiradi (`connect_accounts`ga TO'G'RIDAN-TO'G'RI EMAS).
  3. `/xush-kelibsiz/biznes-profili` (onboarding, endpoint
     `onboarding_business_profile`) -- GET "Xush kelibsiz" sarlavhasi +
     dropdown + barcha savollarni (helper matni bilan) ko'rsatadi; POST
     profilni saqlaydi VA `/connect-accounts`ga yo'naltiradi (onboarding
     oqimi davom etadi); "O'tkazib yuborish" havolasi ham bor.
  4. `/sozlamalar/kompaniya-malumotlari` (endpoint `settings_business_profile`)
     -- xuddi shu forma, lekin Sozlamalar konteksti ("Kompaniya
     ma'lumotlari" sarlavha, orqaga havola, "O'tkazib yuborish" YO'Q);
     POST saqlagach O'ZIGA qaytadi (`connect_accounts`ga EMAS).
  5. MUHIM: ikkala route ham "Sinov" (trial) tarifidagi kompaniya uchun
     ham ISHLASHI kerak -- ular `@module_required("settings")` EMAS,
     faqat `@admin_required` bilan himoyalangan (aks holda yangi
     ro'yxatdan o'tgan -- har doim trial'dan boshlaydigan -- kompaniya bu
     qadamni UMUMAN o'tolmay qolardi).
  6. `/sozlamalar/umumiy` ENDI biznes-profil formasini KO'RSATMAYDI (bu
     bo'lim `settings_business_profile`ga ko'chirildi).
  7. `/sozlamalar` (hub) "Kompaniya ma'lumotlari" kartasini ko'rsatadi,
     profil bo'sh bo'lsa "to'ldirilmagan" belgisi bilan.
  8. Web-assistant tizim prompti (`app._web_assistant_system_prompt`)
     kompaniya profili to'ldirilgan bo'lsa uni o'z ichiga oladi, bo'sh
     bo'lsa -- oldingidek, profilsiz ishlaydi.
  9. `company_business_profile_nudge` context -- profil bo'sh bo'lsa admin
     uchun `True`, to'ldirilgan bo'lsa `False` (AI-yordamchi proaktiv
     taklif bubble'i shunga qarab ko'rsatiladi/yashiriladi).

Ishga tushirish:
    cd app && python3 scripts/test_business_profile_offline.py
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
_DB_PATH = os.path.join(_TMPDIR, "test_business_profile.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

import app as app_module  # noqa: E402
import business_profile  # noqa: E402
import db as db_module  # noqa: E402

app_module.app.config["TESTING"] = True
app_module.app.config["WTF_CSRF_ENABLED"] = False  # 2026-09, CSRF endi majburiy -- testlarda so'rovlar session-tashqarisida yasaladi
db_module.init_db()

failures = []


def check(name, cond):
    print(("OK  " if cond else "FAIL") + " " + name)
    if not cond:
        failures.append(name)


def _label_in_html(label, html):
    """Jinja `'` ni `&#39;`ga escape qiladi -- solishtirishda apostrofni
    e'tiborsiz qoldiramiz (xuddi boshqa testlardagi kabi)."""
    return label.replace("'", "") in html.replace("&#39;", "").replace("'", "")


# ---------------------------------------------------------------------------
# 1) business_profile.py sof funksiyalari
# ---------------------------------------------------------------------------

def test_serialize_parse_round_trip():
    answers = {"product_or_service": "Erkaklar oyoq kiyimi", "target_audience": "  ", "unknown_key": "e'tiborsiz"}
    raw = business_profile.serialize_business_profile_answers(answers)
    check("bo'sh/notanish kalitlar tashlab yuboriladi", raw is not None and "unknown_key" not in raw and "target_audience" not in raw)
    parsed = business_profile.parse_business_profile_answers(raw)
    check("saqlangan javob round-trip qaytadi", parsed.get("product_or_service") == "Erkaklar oyoq kiyimi")
    check("hech narsa to'ldirilmasa None qaytadi", business_profile.serialize_business_profile_answers({}) is None)
    check("None/bo'sh JSON dict sifatida parse qilinadi", business_profile.parse_business_profile_answers(None) == {})
    check("buzilgan JSON xato bermaydi, bo'sh dict qaytadi", business_profile.parse_business_profile_answers("{buzilgan") == {})


def test_sku_count_question_present():
    keys = [key for key, *_ in business_profile.BUSINESS_PROFILE_QUESTIONS]
    check("'nechta SKU' savoli ro'yxatda bor (sku_count)", "sku_count" in keys)
    check("har bir savolda 4 ta element bor (key, savol, misol, tushuntirish)",
          all(len(q) == 4 for q in business_profile.BUSINESS_PROFILE_QUESTIONS))
    check("har bir savolning tushuntirish matni bo'sh emas",
          all(q[3].strip() for q in business_profile.BUSINESS_PROFILE_QUESTIONS))


class _FakeCompany:
    def __init__(self, category=None, note=None, answers_json=None):
        self.business_category = category
        self.business_category_note = note
        self.business_profile_answers = answers_json


def test_summary_text_empty_vs_filled():
    check("company=None -> None", business_profile.business_profile_summary_text(None) is None)
    empty_company = _FakeCompany()
    check("hech narsa to'ldirilmagan kompaniya -> None", business_profile.business_profile_summary_text(empty_company) is None)

    filled_raw = business_profile.serialize_business_profile_answers({
        "product_or_service": "Erkaklar oyoq kiyimi, 30 dan ortiq model",
        "price_range": "150 000 - 500 000 so'm",
        "sku_count": "45 ta model",
    })
    filled_company = _FakeCompany(category="clothing_fashion", note="Asosan sport oyoq kiyimlari", answers_json=filled_raw)
    text = business_profile.business_profile_summary_text(filled_company)
    check("to'ldirilgan profil matn qaytaradi", text is not None)
    check("sarlavha bor", "KOMPANIYA BIZNES PROFILI" in text)
    check("kategoriya nomi bor", "Kiyim-kechak / moda" in text)
    check("izoh bor", "Asosan sport oyoq kiyimlari" in text)
    check("javoblar bor", "Erkaklar oyoq kiyimi, 30 dan ortiq model" in text)
    check("SKU javobi bor", "45 ta model" in text)
    check("faqat to'ldirilgan savol qatori qo'shiladi (bo'shlari yo'q)", "Asosiy raqobatchilaringiz" not in text)


def test_is_profile_filled():
    check("company=None -> False", business_profile.is_profile_filled(None) is False)
    check("hech narsa yo'q -> False", business_profile.is_profile_filled(_FakeCompany()) is False)
    check("faqat kategoriya bor -> True", business_profile.is_profile_filled(_FakeCompany(category="clothing_fashion")) is True)
    only_answers = business_profile.serialize_business_profile_answers({"sku_count": "10 ta"})
    check("faqat savol javobi bor -> True", business_profile.is_profile_filled(_FakeCompany(answers_json=only_answers)) is True)


test_serialize_parse_round_trip()
test_sku_count_question_present()
test_summary_text_empty_vs_filled()
test_is_profile_filled()


# ---------------------------------------------------------------------------
# 2) /signup -- ENDI biznes-profil so'ramaydi, alohida qadamga yo'naltiradi
# ---------------------------------------------------------------------------

def test_signup_no_longer_shows_business_profile_fields():
    with app_module.app.test_client() as client:
        html = client.get("/signup").get_data(as_text=True)
        check("signup sahifasida biznes-profil bo'limi ENDI YO'Q", "Kompaniya haqida qo'shimcha ma'lumot" not in html)
        check("signup sahifasida dropdown ENDI YO'Q", "Kiyim-kechak / moda" not in html)


def test_signup_post_redirects_to_onboarding_business_profile():
    with app_module.app.test_client() as client:
        data = {
            "company_name": "Fashion Point MChJ", "admin_username": "fp_admin",
            "admin_full_name": "", "email": "", "plan": "trial",
            "password": "parol123456", "password2": "parol123456",
        }
        r = client.post("/signup", data=data, follow_redirects=False)
        check("signup POST muvaffaqiyatli bo'lsa redirect (302/303) qaytaradi", r.status_code in (302, 303))
        check("signup ENDI to'g'ridan-to'g'ri connect_accounts'ga EMAS, biznes-profil qadamiga yo'naltiradi",
              "/xush-kelibsiz/biznes-profili" in r.headers.get("Location", ""))

        # onboarding qadamini kuzatib boramiz -- "Xush kelibsiz" sarlavhasi
        # va barcha savollar (helper matni bilan) ko'rinishi kerak, VA
        # "Sinov" (trial) tarifida bo'lsa ham (ENG MUHIM tuzatish -- avval
        # bu bo'lim `settings` moduliga bog'liq edi, trial'da esa yo'q).
        r2 = client.get("/xush-kelibsiz/biznes-profili")
        check("onboarding sahifasi TRIAL tarifdagi kompaniya uchun ham 200 qaytaradi", r2.status_code == 200)
        html2 = r2.get_data(as_text=True)
        check("onboarding sahifasida 'Xush kelibsiz' sarlavhasi bor", "Xush kelibsiz" in html2)
        check("onboarding sahifasida dropdown bor", "Kiyim-kechak / moda" in html2)
        check("onboarding sahifasida 'O'tkazib yuborish' havolasi bor", "tkazib yuborish" in html2)
        for key, label, _placeholder, helper in business_profile.BUSINESS_PROFILE_QUESTIONS:
            check(f"onboarding sahifasida savol bor: {key}", _label_in_html(label, html2))
            check(f"onboarding sahifasida tushuntirish matni bor: {key}", _label_in_html(helper, html2))


def test_onboarding_post_saves_and_redirects_to_connect_accounts():
    with app_module.app.test_client() as client:
        client.post("/signup", data={
            "company_name": "Onboarding Test MChJ", "admin_username": "onb_admin",
            "admin_full_name": "", "email": "", "plan": "trial",
            "password": "parol123456", "password2": "parol123456",
        }, follow_redirects=False)

        r = client.post("/xush-kelibsiz/biznes-profili", data={
            "business_category": "clothing_fashion",
            "business_category_note": "Asosan ayollar kiyimi",
            "bp_product_or_service": "Ayollar ko'ylaklari",
            "bp_price_range": "200 000 - 600 000 so'm",
            "bp_sku_count": "80 ta model",
        }, follow_redirects=False)
        check("onboarding POST redirect qaytaradi", r.status_code in (302, 303))
        check("onboarding POST'dan keyin /connect-accounts'ga yo'naltiradi", "/connect-accounts" in r.headers.get("Location", ""))

    session = db_module.get_session()
    try:
        with db_module.unscoped():
            company = session.query(db_module.Company).filter_by(name="Onboarding Test MChJ").first()
        check("kompaniya yaratilgan", company is not None)
        check("business_category saqlandi", company.business_category == "clothing_fashion")
        answers = business_profile.parse_business_profile_answers(company.business_profile_answers)
        check("savol javobi saqlandi", answers.get("product_or_service") == "Ayollar ko'ylaklari")
        check("SKU javobi saqlandi", answers.get("sku_count") == "80 ta model")
        check("is_profile_filled() endi True", business_profile.is_profile_filled(company) is True)
    finally:
        session.close()


def test_onboarding_skip_link_goes_straight_to_connect_accounts():
    """Profilni to'ldirmasdan "O'tkazib yuborish" bosilsa -- hech narsa
    yozilmasdan, to'g'ridan-to'g'ri connect_accounts'ga o'tishi kerak
    (bu shunchaki oddiy havola, alohida route emas)."""
    with app_module.app.test_client() as client:
        client.post("/signup", data={
            "company_name": "Skip Link MChJ", "admin_username": "skiplink_admin",
            "admin_full_name": "", "email": "", "plan": "trial",
            "password": "parol123456", "password2": "parol123456",
        }, follow_redirects=False)
        r = client.get("/xush-kelibsiz/biznes-profili")
        html = r.get_data(as_text=True)
        check("'O'tkazib yuborish' havolasi to'g'ridan-to'g'ri /connect-accounts'ga ishora qiladi",
              'href="/connect-accounts"' in html)


test_signup_no_longer_shows_business_profile_fields()
test_signup_post_redirects_to_onboarding_business_profile()
test_onboarding_post_saves_and_redirects_to_connect_accounts()
test_onboarding_skip_link_goes_straight_to_connect_accounts()


# ---------------------------------------------------------------------------
# 3) /sozlamalar/kompaniya-malumotlari -- ko'rsatish + tahrirlash
#    (endi TRIAL tarifida ham ishlaydi, /sozlamalar/umumiy'da ENDI yo'q)
# ---------------------------------------------------------------------------

session = db_module.get_session()
try:
    company2 = db_module.Company(name="Sozlama MChJ", plan="trial", is_active=True, source="admin_created")
    session.add(company2)
    session.commit()
    admin2 = db_module.Manager(username="stg_bp_admin", full_name="Admin", role="admin", company_id=company2.id)
    admin2.set_password("parol123")
    session.add(admin2)
    session.commit()
    company2_id = company2.id
finally:
    session.close()

admin2_client = app_module.app.test_client()
admin2_client.post("/login", data={"username": "stg_bp_admin", "password": "parol123"})


def test_settings_general_no_longer_shows_business_profile_form():
    html = admin2_client.get("/sozlamalar/umumiy").get_data(as_text=True)
    check("settings_general ENDI biznes-profil bo'limini KO'RSATMAYDI ('umumiy' sozlamalarga tegishli emas)",
          "Kompaniya biznes profili" not in html)


def test_settings_business_profile_shows_form_on_trial_plan():
    """MUHIM: bu -- company2 "trial" tarifda, `settings` moduli YO'Q --
    lekin sahifa BARIBIR ochilishi kerak (`@module_required("settings")`
    EMAS, faqat `@admin_required`)."""
    r = admin2_client.get("/sozlamalar/kompaniya-malumotlari")
    check("settings_business_profile TRIAL tarifda ham 200 qaytaradi", r.status_code == 200)
    html = r.get_data(as_text=True)
    check("sahifa sarlavhasi 'Kompaniya ma'lumotlari'", "Kompaniya ma&#39;lumotlari" in html or "Kompaniya ma'lumotlari" in html)
    check("dropdown ko'rsatiladi", "Kiyim-kechak / moda" in html)
    check("Sozlamalar sahifasiga orqaga havola bor", "Sozlamalar" in html)
    check("bu yerda 'O'tkazib yuborish' YO'Q (bu onboarding emas)", "tkazib yuborish" not in html)
    for key, label, _placeholder, helper in business_profile.BUSINESS_PROFILE_QUESTIONS:
        check(f"settings_business_profile savolni ko'rsatadi: {key}", _label_in_html(label, html))


def test_settings_business_profile_post_saves_and_prefills():
    r = admin2_client.post("/sozlamalar/kompaniya-malumotlari", data={
        "business_category": "beauty_salon",
        "business_category_note": "Sartaroshxona + manikyur",
        "bp_product_or_service": "Soch turmagi, manikyur, pedikyur",
        "bp_target_audience": "20-45 yosh ayollar",
        "bp_sku_count": "1 ta asosiy xizmat, 6 xil paket",
    }, follow_redirects=False)
    check("POST redirect qaytaradi", r.status_code in (302, 303))
    check("POST'dan keyin O'ZIGA qaytadi (connect_accounts'ga EMAS)", "/sozlamalar/kompaniya-malumotlari" in r.headers.get("Location", ""))

    session = db_module.get_session()
    try:
        with db_module.unscoped():
            company = session.get(db_module.Company, company2_id)
        check("business_category yangilandi", company.business_category == "beauty_salon")
        check("business_category_note yangilandi", company.business_category_note == "Sartaroshxona + manikyur")
        answers = business_profile.parse_business_profile_answers(company.business_profile_answers)
        check("javob saqlandi (product_or_service)", answers.get("product_or_service") == "Soch turmagi, manikyur, pedikyur")
        check("javob saqlandi (target_audience)", answers.get("target_audience") == "20-45 yosh ayollar")
        check("javob saqlandi (sku_count)", answers.get("sku_count") == "1 ta asosiy xizmat, 6 xil paket")
    finally:
        session.close()

    # Qayta ochilganda oldingi qiymatlar bilan oldindan to'ldirilgan bo'lishi kerak.
    html2 = admin2_client.get("/sozlamalar/kompaniya-malumotlari").get_data(as_text=True)
    check("qayta ochilganda tanlangan kategoriya 'selected' bo'lib qoladi", 'value="beauty_salon" selected' in html2)
    check("qayta ochilganda izoh maydonda qoladi", "Sartaroshxona + manikyur" in html2)
    check("qayta ochilganda javob maydonda qoladi", "Soch turmagi, manikyur, pedikyur" in html2)


test_settings_general_no_longer_shows_business_profile_form()
test_settings_business_profile_shows_form_on_trial_plan()
test_settings_business_profile_post_saves_and_prefills()


# ---------------------------------------------------------------------------
# 4) /sozlamalar (hub) -- "Kompaniya ma'lumotlari" kartasi + "to'ldirilmagan" belgisi
# ---------------------------------------------------------------------------

def test_settings_hub_shows_business_profile_card_with_badge():
    session = db_module.get_session()
    try:
        c = db_module.Company(name="Hub Badge MChJ", plan="start", is_active=True, source="admin_created")
        session.add(c)
        session.commit()
        m = db_module.Manager(username="hub_badge_admin", full_name="Admin", role="admin", company_id=c.id)
        m.set_password("parol123")
        session.add(m)
        session.commit()
        cid = c.id
    finally:
        session.close()

    client = app_module.app.test_client()
    client.post("/login", data={"username": "hub_badge_admin", "password": "parol123"})

    html = client.get("/sozlamalar").get_data(as_text=True)
    check("hub sahifasida 'Kompaniya ma'lumotlari' kartasi bor", "Kompaniya ma" in html)
    check("profil bo'sh bo'lganda 'to'ldirilmagan' belgisi ko'rinadi", "to'ldirilmagan" in html)

    client.post("/sozlamalar/kompaniya-malumotlari", data={"business_category": "beauty_salon"}, follow_redirects=False)

    session = db_module.get_session()
    try:
        with db_module.unscoped():
            c2 = session.get(db_module.Company, cid)
        check("profil endi to'ldirilgan", business_profile.is_profile_filled(c2) is True)
    finally:
        session.close()

    html2 = client.get("/sozlamalar").get_data(as_text=True)
    check("profil to'ldirilgach 'to'ldirilmagan' belgisi YO'QOLADI", "to'ldirilmagan" not in html2)


test_settings_hub_shows_business_profile_card_with_badge()


# ---------------------------------------------------------------------------
# 5) Web-assistant tizim prompti profilni hisobga oladi
# ---------------------------------------------------------------------------

def test_web_assistant_prompt_includes_profile_when_present():
    session = db_module.get_session()
    try:
        with db_module.unscoped():
            company = session.get(db_module.Company, company2_id)
        prompt_with_profile = app_module._web_assistant_system_prompt(company)
    finally:
        session.close()
    check("profilli kompaniya uchun prompt profil matnini o'z ichiga oladi", "KOMPANIYA BIZNES PROFILI" in prompt_with_profile)
    check("profilli kompaniya uchun prompt kategoriyani o'z ichiga oladi", "Go'zallik saloni" in prompt_with_profile)

    prompt_without_company = app_module._web_assistant_system_prompt(None)
    check("company=None bo'lsa prompt profilsiz, xatosiz ishlaydi", "KOMPANIYA BIZNES PROFILI" not in prompt_without_company)
    check("company=None bo'lsa ham bilim bazasi bor", "BILIM BAZASI" in prompt_without_company)


test_web_assistant_prompt_includes_profile_when_present()


# ---------------------------------------------------------------------------
# 6) AI-yordamchi proaktiv taklif bubble -- faqat profil bo'sh bo'lganda,
#    faqat admin uchun.
# ---------------------------------------------------------------------------

def test_ai_nudge_bubble_shown_only_when_profile_empty():
    session = db_module.get_session()
    try:
        c = db_module.Company(name="Nudge MChJ", plan="business", is_active=True, source="admin_created")
        session.add(c)
        session.commit()
        admin = db_module.Manager(username="nudge_admin", full_name="Admin", role="admin", company_id=c.id)
        admin.set_password("parol123")
        manager = db_module.Manager(username="nudge_manager", full_name="Menejer", role="manager", company_id=c.id)
        manager.set_password("parol123")
        session.add_all([admin, manager])
        session.commit()
    finally:
        session.close()

    admin_client = app_module.app.test_client()
    admin_client.post("/login", data={"username": "nudge_admin", "password": "parol123"})
    html = admin_client.get("/").get_data(as_text=True)
    check("profil bo'sh -- admin uchun bubble markup'i sahifada bor", 'id="ai-nudge-bubble"' in html)
    check("bubble matni bor", "hali tanishmadik" in html)

    manager_client = app_module.app.test_client()
    manager_client.post("/login", data={"username": "nudge_manager", "password": "parol123"})
    html_manager = manager_client.get("/").get_data(as_text=True)
    check("menejer uchun bubble YO'Q (faqat admin to'ldira oladi)", 'id="ai-nudge-bubble"' not in html_manager)

    admin_client.post("/sozlamalar/kompaniya-malumotlari", data={"business_category": "beauty_salon"}, follow_redirects=False)
    html_after = admin_client.get("/").get_data(as_text=True)
    check("profil to'ldirilgach bubble YO'QOLADI", 'id="ai-nudge-bubble"' not in html_after)


test_ai_nudge_bubble_shown_only_when_profile_empty()


print()
if failures:
    print(f"MUVAFFAQIYATSIZ: {len(failures)} ta test o'tmadi:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("BARCHA TESTLAR O'TDI (biznes-profil)")
