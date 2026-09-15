"""test_business_profile_offline.py — 2026-09, foydalanuvchi so'rovi:
"registratsiya bo'limida va nastroykada... kompaniya haqida ma'lumotlarni
qo'shish mumkin bo'lsin keyinchalik to'liq tahlil uchun... drop down...
har bitta yo'nalish bo'yicha... qo'shimcha izoh... kamida beshta savol...
imenno kompaniya haqida to'liq tushunib olish uchun AI va shu bo'yicha
javob bersin har doim."

Tekshiradi:
  - `business_profile.py`ning sof funksiyalari: serialize/parse round-trip,
    bo'sh javoblar tashlab yuboriladi, `business_profile_summary_text()`
    to'ldirilmagan bo'lsa `None`, to'ldirilgan bo'lsa AI promptiga
    qo'shiladigan formatlangan matn qaytaradi.
  - `/signup` sahifasi (GET) dropdown + 6 ta savolni ko'rsatadi; (POST)
    kiritilgan biznes-profilni yangi `Company`ga saqlaydi.
  - `/sozlamalar/umumiy` (GET) admin uchun biznes-profil formasini
    ko'rsatadi (mavjud qiymatlar bilan oldindan to'ldirilgan); (POST,
    action=set_business_profile) profilni yangilaydi/tahrirlaydi.
  - Web-assistant tizim prompti (`app._web_assistant_system_prompt`)
    kompaniya profili to'ldirilgan bo'lsa uni o'z ichiga oladi, bo'sh
    bo'lsa -- oldingidek, profilsiz ishlaydi.

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
    })
    filled_company = _FakeCompany(category="clothing_fashion", note="Asosan sport oyoq kiyimlari", answers_json=filled_raw)
    text = business_profile.business_profile_summary_text(filled_company)
    check("to'ldirilgan profil matn qaytaradi", text is not None)
    check("sarlavha bor", "KOMPANIYA BIZNES PROFILI" in text)
    check("kategoriya nomi bor", "Kiyim-kechak / moda" in text)
    check("izoh bor", "Asosan sport oyoq kiyimlari" in text)
    check("javoblar bor", "Erkaklar oyoq kiyimi, 30 dan ortiq model" in text)
    check("faqat to'ldirilgan savol qatori qo'shiladi (bo'shlari yo'q)", "Asosiy raqobatchilaringiz" not in text)


test_serialize_parse_round_trip()
test_summary_text_empty_vs_filled()


# ---------------------------------------------------------------------------
# 2) /signup -- dropdown + savollarni ko'rsatadi, POST saqlaydi
# ---------------------------------------------------------------------------

def test_signup_shows_business_profile_fields():
    with app_module.app.test_client() as client:
        html = client.get("/signup").get_data(as_text=True)
        check("signup sahifasida biznes-profil bo'limi bor", "Kompaniya haqida qo'shimcha ma'lumot" in html)
        check("signup sahifasida dropdown bor (Kiyim-kechak)", "Kiyim-kechak / moda" in html)
        for key, label, _ in business_profile.BUSINESS_PROFILE_QUESTIONS:
            check(f"signup sahifasida savol bor: {key}", _label_in_html(label, html))


def test_signup_post_saves_business_profile():
    with app_module.app.test_client() as client:
        data = {
            "company_name": "Fashion Point MChJ", "admin_username": "fp_admin",
            "admin_full_name": "", "email": "", "plan": "trial",
            "password": "parol123456", "password2": "parol123456",
            "business_category": "clothing_fashion",
            "business_category_note": "Asosan ayollar kiyimi",
            "bp_product_or_service": "Ayollar ko'ylaklari",
            "bp_price_range": "200 000 - 600 000 so'm",
        }
        r = client.post("/signup", data=data, follow_redirects=True)
        check("signup (biznes-profil bilan) muvaffaqiyatli", r.status_code == 200)

    session = db_module.get_session()
    try:
        with db_module.unscoped():
            company = session.query(db_module.Company).filter_by(name="Fashion Point MChJ").first()
        check("kompaniya yaratildi", company is not None)
        check("business_category saqlandi", company.business_category == "clothing_fashion")
        check("business_category_note saqlandi", company.business_category_note == "Asosan ayollar kiyimi")
        answers = business_profile.parse_business_profile_answers(company.business_profile_answers)
        check("savol javobi saqlandi", answers.get("product_or_service") == "Ayollar ko'ylaklari")
        summary = business_profile.business_profile_summary_text(company)
        check("saqlangan profildan summary quriladi", summary is not None and "Ayollar ko'ylaklari" in summary)
    finally:
        session.close()


def test_signup_post_without_business_profile_still_works():
    """Ixtiyoriy -- bo'sh qoldirilsa ham ro'yxatdan o'tish buzilmasligi kerak."""
    with app_module.app.test_client() as client:
        r = client.post("/signup", data={
            "company_name": "Profilsiz MChJ", "admin_username": "profilsiz_admin",
            "admin_full_name": "", "email": "", "plan": "trial",
            "password": "parol123456", "password2": "parol123456",
        }, follow_redirects=True)
        check("biznes-profilsiz signup ham ishlaydi", r.status_code == 200)

    session = db_module.get_session()
    try:
        with db_module.unscoped():
            company = session.query(db_module.Company).filter_by(name="Profilsiz MChJ").first()
        check("kompaniya yaratildi (profilsiz)", company is not None)
        check("business_category bo'sh qoldi", company.business_category is None)
        check("business_profile_answers bo'sh qoldi (NULL, '{}' emas)", company.business_profile_answers is None)
    finally:
        session.close()


test_signup_shows_business_profile_fields()
test_signup_post_saves_business_profile()
test_signup_post_without_business_profile_still_works()


# ---------------------------------------------------------------------------
# 3) /sozlamalar/umumiy -- ko'rsatish + tahrirlash
# ---------------------------------------------------------------------------

session = db_module.get_session()
try:
    company2 = db_module.Company(name="Sozlama MChJ", plan="start", is_active=True, source="admin_created")
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


def test_settings_general_shows_business_profile_form():
    html = admin2_client.get("/sozlamalar/umumiy").get_data(as_text=True)
    check("settings_general biznes-profil bo'limini ko'rsatadi", "Kompaniya biznes profili" in html)
    check("settings_general dropdown ko'rsatadi", "Kiyim-kechak / moda" in html)
    for key, label, _ in business_profile.BUSINESS_PROFILE_QUESTIONS:
        check(f"settings_general savolni ko'rsatadi: {key}", _label_in_html(label, html))


def test_settings_general_post_saves_and_prefills():
    r = admin2_client.post("/sozlamalar/umumiy", data={
        "action": "set_business_profile",
        "business_category": "beauty_salon",
        "business_category_note": "Sartaroshxona + manikyur",
        "bp_product_or_service": "Soch turmagi, manikyur, pedikyur",
        "bp_target_audience": "20-45 yosh ayollar",
    }, follow_redirects=True)
    check("set_business_profile POST 200 qaytaradi", r.status_code == 200)
    html = r.get_data(as_text=True)
    check("saqlash muvaffaqiyatli xabari ko'rsatiladi", "biznes profili saqlandi" in html)

    session = db_module.get_session()
    try:
        with db_module.unscoped():
            company = session.get(db_module.Company, company2_id)
        check("business_category yangilandi", company.business_category == "beauty_salon")
        check("business_category_note yangilandi", company.business_category_note == "Sartaroshxona + manikyur")
        answers = business_profile.parse_business_profile_answers(company.business_profile_answers)
        check("javob saqlandi (product_or_service)", answers.get("product_or_service") == "Soch turmagi, manikyur, pedikyur")
        check("javob saqlandi (target_audience)", answers.get("target_audience") == "20-45 yosh ayollar")
    finally:
        session.close()

    # Qayta ochilganda oldingi qiymatlar bilan oldindan to'ldirilgan bo'lishi kerak.
    html2 = admin2_client.get("/sozlamalar/umumiy").get_data(as_text=True)
    check("qayta ochilganda tanlangan kategoriya 'selected' bo'lib qoladi", 'value="beauty_salon" selected' in html2)
    check("qayta ochilganda izoh maydonda qoladi", "Sartaroshxona + manikyur" in html2)
    check("qayta ochilganda javob maydonda qoladi", "Soch turmagi, manikyur, pedikyur" in html2)


test_settings_general_shows_business_profile_form()
test_settings_general_post_saves_and_prefills()


# ---------------------------------------------------------------------------
# 4) Web-assistant tizim prompti profilni hisobga oladi
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


print()
if failures:
    print(f"MUVAFFAQIYATSIZ: {len(failures)} ta test o'tmadi:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("BARCHA TESTLAR O'TDI (biznes-profil)")
