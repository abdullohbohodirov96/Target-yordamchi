"""business_profile.py — 2026-09, foydalanuvchi so'rovi: "registratsiya
bo'limida va nastroykada... kompaniya haqida ma'lumotlarni qo'shish mumkin
bo'lsin keyinchalik to'liq tahlil uchun... drop down... har bitta yo'nalish
bo'yicha... qo'shimcha izoh... kamida beshta savol... imenno kompaniya
haqida to'liq tushunib olish uchun AI va shu bo'yicha javob bersin har
doim."

2026-09 QAYTA ISHLASH (foydalanuvchi, ikkinchi so'rov: "biznes
ochayotganda... yaratish bosgandan keyin, otdelno oyinda chiqib kelsin...
kamida beshta savol... nechta sku... nastroykaga qo'shimcha joy qo'shish
kerak... kompaniya ma'lumotlari... agar to'ldirilmagan bo'lsa... yordamchi
sms chiqib kelsin"): endi bu profil (1) ro'yxatdan o'tishning O'ZIDA EMAS
(u yerda faqat hisob yaratiladi), balki hisob yaratilgandan KEYIN alohida
qadam sifatida (`/xush-kelibsiz/biznes-profili`) so'raladi, (2) Sozlamalar
bo'limida O'ZINING alohida kartasiga ega ("Kompaniya ma'lumotlari",
`/sozlamalar/kompaniya-malumotlari` -- endi "settings" tarif-moduliga
BOG'LIQ EMAS, aks holda "Sinov" tarifidagi yangi kompaniyalar buni
UMUMAN to'ldira olmasdi), (3) to'ldirilmagan bo'lsa, AI-yordamchi vidjeti
(`app.py`dagi `_ai_widget_nudge_text()`) proaktiv taklif ko'rsatadi.

Bu modul TO'RTTA narsani beradi:
  1. `BUSINESS_CATEGORIES` — biznes yo'nalishi dropdown ro'yxati (keng
     qamrovli, O'zbekistondagi tipik SMB'lar bo'yicha).
  2. `BUSINESS_PROFILE_QUESTIONS` — kompaniya haqida AI to'liq tushunishi
     uchun kerakli savollar (key, savol matni, misol/placeholder,
     PASTDAGI tushuntirish matni -- 4 ta element, foydalanuvchi
     "aniq savol bo'lsin... pastda tushuntirish bo'lsin" dedi).
  3. `business_profile_summary_text(company)` — Company obyektidan
     Targetolog/Marketolog agent (`orchestrator.py`) VA web/Telegram
     AI-yordamchisi (`app.py`) promptiga qo'shiladigan tayyor matn bo'lagi.
  4. `is_profile_filled(company)` — profil HECH BO'LMAGANDA bitta narsa
     (yo'nalish yoki savollardan biri) bilan to'ldirilganmi -- AI-yordamchi
     vidjetining proaktiv eslatmasi shu asosida ko'rsatiladi/yashiriladi.

Ma'lumotning o'zi `db.py`dagi `Company.business_category` /
`business_category_note` / `business_profile_answers` (JSON) ustunlarida
saqlanadi. To'ldirish ATAYLAB HAMON IXTIYORIY (bo'sh qoldirsa AI profilsiz,
oddiy ishlayveradi) -- lekin endi ko'proq ko'rinadigan/qulay joyda so'raladi."""

import json

# 2026-09: keng qamrovli, lekin cheksiz emas -- O'zbekistondagi tipik kichik/
# o'rta biznes turlari asosida tanlangan. Har doim oxirida "Boshqa" bor --
# ro'yxatda yo'q yo'nalish uchun (bunda `business_category_note` majburiy
# emas, lekin foydalanuvchi izohda aniqlashtirishi tavsiya etiladi).
BUSINESS_CATEGORIES = [
    ("phone_electronics", "Telefon / elektronika do'koni"),
    ("household_appliances", "Maishiy texnika"),
    ("clothing_fashion", "Kiyim-kechak / moda"),
    ("beauty_salon", "Go'zallik saloni / kosmetologiya"),
    ("construction_materials", "Qurilish materiallari"),
    ("furniture_interior", "Mebel / interyer"),
    ("real_estate", "Ko'chmas mulk"),
    ("auto_spare_parts", "Avtomobil / ehtiyot qismlar"),
    ("restaurant_food", "Restoran / kafe / oziq-ovqat"),
    ("education_courses", "Ta'lim markazi / kurslar"),
    ("medical_clinic", "Tibbiyot / klinika / salomatlik"),
    ("fitness_sport", "Fitnes / sport"),
    ("logistics_delivery", "Logistika / yetkazib berish"),
    ("agency_smm", "Marketing agentligi / SMM / IT xizmatlar"),
    ("online_shop_ecommerce", "Onlayn do'kon / e-commerce (ko'p toifali)"),
    ("wholesale_b2b", "Ulgurji savdo / B2B"),
    ("event_wedding", "To'ylar / tadbirlar tashkiloti"),
    ("legal_finance", "Yuridik / moliyaviy xizmatlar"),
    ("kids_toys", "Bolalar mahsulotlari / o'yinchoqlar"),
    ("jewelry_watches", "Zargarlik buyumlari / soatlar"),
    ("agriculture", "Qishloq xo'jaligi"),
    ("manufacturing", "Ishlab chiqarish / zavod"),
    ("travel_tourism", "Turizm / sayohat"),
    ("other", "Boshqa"),
]

# 2026-09, foydalanuvchi so'rovi: "ikkita-uchta savollar... kamida beshta
# savol bo'lsin, imenno kompaniya haqida to'liq tushunib olish uchun ai".
# 2026-09 QAYTA ISHLASH: "nechta sku" savoli qo'shildi (7 ta savol endi) --
# Targetolog agentiga eng ko'p kerak bo'ladigan narsalar (nima sotiladi,
# kimga, qancha narxda, nechta turdagi mahsulot, eng ko'p so'raladigani,
# raqobat, erkin qo'shimcha) atayin shu tartibda tanlangan. Har bir savol
# ENDI 4 ta elementdan iborat: (key, savol, misol/placeholder, PASTDA
# ko'rsatiladigan tushuntirish -- "aniq savol bo'lsin... pastda
# tushuntirish bo'lsin nimagaligini va nima haqida yozish kerakligini").
BUSINESS_PROFILE_QUESTIONS = [
    ("product_or_service", "Nima soting yoki qanday xizmat ko'rsatasiz?",
     "Masalan: erkaklar oyoq kiyimi, 30 dan ortiq model, o'rtacha narxda",
     "AI reklama matni va javoblarni shu mahsulot/xizmatga moslab yozadi -- qancha aniqroq yozsangiz, shuncha aniqroq javob beradi."),
    ("target_audience", "Mijozlaringiz odatda kimlar (yosh, jins, hudud)?",
     "Masalan: 20-40 yosh, ko'proq ayollar, Toshkent shahri",
     "Reklama auditoriyasini va murojaat ohangini shu mijoz portretiga qarab tanlashga yordam beradi."),
    ("sku_count", "Nechta xil mahsulot turi (SKU/model)ingiz bor?",
     "Masalan: 45 ta model, yoki \"1 ta xizmat turi\"",
     "Assortiment kattaligi -- keng assortimentda AI umumiy reklama g'oyalarini, tor assortimentda esa aniq mahsulot bo'yicha chuqur tahlilni taklif qiladi."),
    ("price_range", "O'rtacha chek/narx oralig'ingiz qancha?",
     "Masalan: 150 000 - 500 000 so'm",
     "Narx segmentini bilish AI'ga byudjet/ROI tahlilida va reklama uslubini (ekonom yoki premium) tanlashda yordam beradi."),
    ("best_seller", "Eng ko'p sotiladigan yoki so'raladigan mahsulot/xizmatingiz qaysi?",
     "Masalan: klassik model, u eng ko'p buyurtma qilinadi",
     "Yangi reklama g'oyalari va nimani ko'proq targetga qo'yish kerakligi bo'yicha maslahatlar shu javob asosida beriladi."),
    ("competitors", "Asosiy raqobatchilaringiz kimlar (bilsangiz)?",
     "Masalan: shahardagi 2-3 ta do'kon nomi yoki brend",
     "AI sizni raqobatchilardan ajratib turadigan afzalliklarni topib, reklama matnida shu farqni ta'kidlashga harakat qiladi."),
    ("extra_notes", "AI yana nimani bilishi kerak (aksiya, afzallik, o'ziga xos jihat)?",
     "Masalan: bepul yetkazib berish, 1 yillik kafolat",
     "Yuqoridagi savollarga sig'magan, lekin mijozga muhim bo'lgan har qanday qo'shimcha ma'lumot -- shu yerga yozing."),
]

_QUESTION_KEYS = {key for key, *_ in BUSINESS_PROFILE_QUESTIONS}
_CATEGORY_LABELS = dict(BUSINESS_CATEGORIES)


def category_label(key: "str | None") -> "str | None":
    return _CATEGORY_LABELS.get(key)


def parse_business_profile_answers(raw_json: "str | None") -> dict:
    if not raw_json:
        return {}
    try:
        parsed = json.loads(raw_json)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {k: v for k, v in parsed.items() if k in _QUESTION_KEYS and isinstance(v, str)}


def serialize_business_profile_answers(answers: dict) -> "str | None":
    """Bo'sh javoblarni tashlab yuboradi. Hech qanday javob qolmasa --
    `None` (ustunni bo'sh/NULL qoldirish uchun, "{}" emas)."""
    clean = {
        k: v.strip() for k, v in (answers or {}).items()
        if k in _QUESTION_KEYS and isinstance(v, str) and v.strip()
    }
    if not clean:
        return None
    return json.dumps(clean, ensure_ascii=False)


def business_profile_summary_text(company) -> "str | None":
    """`company` (`db.Company` yoki `None`)dan Targetolog/Marketolog agent
    (`orchestrator.py`) VA web/Telegram AI-yordamchisi (`app.py`) promptiga
    qo'shiladigan tayyor matn bo'lagini quradi. Hech narsa to'ldirilmagan
    bo'lsa -- `None` qaytaradi (chaqiruvchi bu holda promptga HECH NARSA
    qo'shmaydi -- profilsiz kompaniyalar uchun xatti-harakat o'zgarmaydi)."""
    if company is None:
        return None
    category = category_label(getattr(company, "business_category", None))
    note = (getattr(company, "business_category_note", None) or "").strip()
    answers = parse_business_profile_answers(getattr(company, "business_profile_answers", None))

    lines = []
    if category:
        lines.append(f"- Biznes yo'nalishi: {category}" + (f" — {note}" if note else ""))
    elif note:
        lines.append(f"- Qo'shimcha izoh: {note}")
    for key, label, *_ in BUSINESS_PROFILE_QUESTIONS:
        value = (answers.get(key) or "").strip()
        if value:
            lines.append(f"- {label} {value}")

    if not lines:
        return None
    return (
        "# KOMPANIYA BIZNES PROFILI (admin o'zi kiritgan, HAR DOIM shuni hisobga ol)\n"
        + "\n".join(lines)
    )


def is_profile_filled(company) -> bool:
    """`company`ning biznes profilida HECH BO'LMAGANDA bitta narsa
    (yo'nalish YOKI savollardan biri) to'ldirilganmi -- AI-yordamchi
    vidjetining proaktiv eslatmasini ko'rsatish/yashirish uchun (2026-09,
    foydalanuvchi so'rovi: "agar to'ldirilmagan bo'lsa... yordamchi
    tanishtiring degan sms chiqarsin")."""
    if company is None:
        return False
    if getattr(company, "business_category", None):
        return True
    answers = parse_business_profile_answers(getattr(company, "business_profile_answers", None))
    return bool(answers)
