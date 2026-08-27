"""call_glossary.py — qo'ng'iroqlar tahlili uchun MARKAZLASHTIRILGAN domen
lug'ati (qurilish-savdo terminologiyasi).

Nega alohida fayl: foydalanuvchi aniq so'radi -- "bitta hardcoded prompt"
emas, "markazlashtirilgan, qayta ishlatiladigan, oson kengaytiriladigan"
konfiguratsiya bo'lishi kerak. Shu fayl transkripsiya bosqichi (Whisper/
gpt-4o-transcribe'ning "prompt" hint'i) VA tahlil bosqichi (matn modeliga
system prompt) uchun BITTA manbadan foydalanadi.

Yangi atama qo'shish uchun: pastdagi `GLOSSARY_TERMS` ro'yxatiga
qo'shing -- boshqa hech narsani o'zgartirish shart emas, ikkala bosqich
ham avtomatik yangi ro'yxatni ishlatadi.

2026-08: mahsulot katalogi (Product jadvali) hozircha bazada YO'Q
(`db.py`da tekshirildi) -- shuning uchun bu yerda faqat STATIK ro'yxat.
Kelajakda katalog jadvali qo'shilsa, `build_glossary_hint()`ga
`extra_terms=` orqali dinamik nomlarni qo'shish mumkin (funksiya buni
allaqachon qo'llab-quvvatlaydi) -- masalan:

    from db import Product
    product_names = [p.name for p in session.query(Product).all()]
    hint = call_glossary.build_glossary_hint(extra_terms=product_names)
"""

from __future__ import annotations

# Foydalanuvchi bergan qurilish-savdo terminologiyasi (so'zma-so'z).
GLOSSARY_TERMS = [
    "dunyabunya",
    "bazalt",
    "penopleks",
    "gipsokarton",
    "profil",
    "plotnost",
    "zichlik",
    "santimetr",
    "millimetr",
    "kvadrat",
    "kvadrat metr",
    "kub",
    "dona",
    "pachka",
    "Vetonit",
    "Somafix",
    "TYTAN",
    "Demir",
    "smesitel",
    "rakovina",
    "kafel",
    "sement",
    "dostavka",
    "filial",
    "narx",
    "omborda",
    "mavjud",
    "buyurtma",
]

# Whisper/gpt-4o-transcribe'ning "prompt" maydoni cheklangan uzunlikka ega
# (taxminan 224 token) -- shuning uchun hint matnini shu son atama bilan
# cheklaymiz (bizning ro'yxatimiz buncha uzun emas, lekin kelajakda katalog
# nomlari qo'shilsa cheklov himoya qiladi).
_DEFAULT_MAX_TERMS = 60


def build_glossary_hint(extra_terms: "list[str] | None" = None, max_terms: int = _DEFAULT_MAX_TERMS) -> str:
    """Atamalar ro'yxatidan vergul bilan ajratilgan qisqa matn yasaydi --
    bu matn ham transkripsiya "prompt"iga, ham tahlil system promptiga
    hint sifatida qo'shiladi. `extra_terms` -- kelajakda mahsulot katalogi
    qo'shilganda dinamik nomlar uchun (hozircha ishlatilmaydi)."""
    terms: list[str] = list(GLOSSARY_TERMS)
    if extra_terms:
        for t in extra_terms:
            t = (t or "").strip()
            if t and t not in terms:
                terms.append(t)
    terms = terms[:max_terms]
    return ", ".join(terms)


# Transkripsiya bosqichi uchun -- haqiqiy o'zbekcha Manager/Mijoz dialog
# NAMUNASI (Whisper "prompt"ni tavsif emas, "audio davomi" sifatida
# talqin qiladi -- shuning uchun bu haqiqiy dialog ko'rinishida bo'lishi
# kerak, "bu o'zbekcha suhbat" kabi meta-tavsif emas).
DIALOGUE_SAMPLE = (
    "Manager: Assalomu alaykum, xush kelibsiz, sizga qanday yordam bera olaman? "
    "Mijoz: Vaalaykum assalom, menga mahsulot narxi va yetkazib berish haqida "
    "ma'lumot kerak edi."
)


def build_transcription_prompt(extra_terms: "list[str] | None" = None) -> str:
    """Transkripsiya so'rovining "prompt" maydoni uchun to'liq matn:
    dialog namunasi + lug'at atamalari ro'yxati. MUHIM: bu ro'yxat modelni
    so'zlarni "tuzatishga" undamaydi -- faqat qanday atamalar uchrashi
    mumkinligini "eslatib qo'yadi" (Whisper prompt'ning tabiati shunday)."""
    hint = build_glossary_hint(extra_terms=extra_terms)
    return f"{DIALOGUE_SAMPLE} Suhbatda quyidagi atamalar uchrashi mumkin: {hint}."


def build_analysis_glossary_note(extra_terms: "list[str] | None" = None) -> str:
    """Tahlil (matn -> JSON) bosqichi uchun -- system promptga qo'shiladigan
    alohida paragraf. MUHIM FARQ: bu yerda modelga ANIQ aytiladiki, bu
    ro'yxat faqat TANISH atamalarni bilib olish uchun -- transkripsiyadagi
    so'zlarni bu ro'yxatga "moslashtirib tuzatish" TAQIQLANADI."""
    hint = build_glossary_hint(extra_terms=extra_terms)
    return (
        "Sohaga oid atamalar ro'yxati (faqat tanish bo'lishi uchun, "
        f"TUZATISH uchun EMAS): {hint}. Agar transkripsiyada shu ro'yxatdagi "
        "so'zlarga o'xshash, lekin aniq bir xil bo'lmagan so'z uchrasa -- "
        "uni ZO'RAKI shu ro'yxatdagi so'zga o'zgartirma, transkripsiyada "
        "qanday kelgan bo'lsa shundayligicha qoldir."
    )
