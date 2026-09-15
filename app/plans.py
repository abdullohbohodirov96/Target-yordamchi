"""plans.py — SaaS tarif (pricing tier) katalogi.

2026-09, foydalanuvchi so'rovi: ochiq (o'z-o'zidan) ro'yxatdan o'tish +
tariflar tizimi ("тарифы, чтобы выбирался тариф... как ты сам анализировал
и сам создал"). Narx va xususiyatlarni ANIQ shu so'rov asosida MEN
loyihalashtirdim:

  - Eng oddiy PULLIK tarif ($20/oy dan) foydalanuvchining aniq talabi bo'yicha
    boshlanadi, undan yuqorisi bosqichma-bosqich ko'proq imkoniyat + sog'lom
    foyda marjasi bilan o'sadi (OpenAI/Meta/Moi Zvonki xarajatlari past
    tarifda deyarli nolga yaqin bo'lgani uchun marja pastda ham katta).

2026-09 YANGILANDI, foydalanuvchi so'rovi ("narxlarni pasaytir, $20dan
boshlansin"): barcha 3 pullik tarif proportsional pasaytirildi (avval
$50/$120/$250, endi $20/$60/$150) -- tariflar orasidagi nisbat va
bosqichma-bosqich o'sish saqlanib qoldi.
  - "Sinov" (trial) ATAYLAB juda cheklangan: faqat Instagram ulanadi, faqat
    XOM natijalar (target xarajat/lead/CPL) ko'rinadi, HECH QANDAY
    AI-xarajat talab qiluvchi funksiya (AI qo'ng'iroq tahlili, ichki AI
    yordamchi) ishlamaydi -- foydalanuvchi so'rovi bilan bir xil:
    "просто, чтобы выводились результаты... без искусственного интеллекта
    и без каких-то трат". Bu SIZNING (platforma egasi) OpenAI xarajatingizni
    tekshirilmagan (pullamagan) hisoblardan ham himoya qiladi.

Mavjud `Company.plan` ustuni (trial|start|business|unlimited) O'ZGARTIRILMAYDI
-- bu 4 ta qiymat endi shu yerda TO'LIQ (narx + huquq + limit) ta'riflanadi,
`app.py`dagi `module_required()` va menejer-limit tekshiruvi shu yerdan
o'qiydi. Company #1 (platforma egasining o'z biznesi) hamon "unlimited"da
turadi (`db.ensure_default_company()`) -- shuning uchun bu gating ESKI
ishlashga ta'sir qilmaydi.

2026-09 YANA YANGILANDI, foydalanuvchi so'rovi ("lead analytics qo'shdik,
adalibrary qo'shdik... trailga, eng oddiy variantga qo'shmang... pro
darajadan chiqib kelaversin... uchtagacha ham raqobatchini belgilash
mumkin bo'lsin birinchi boshlang'ich tarifda... keyingisi o'n tagacha...
keyingisida cheksiz"):
  - "Lid tahlili" (Lead Analytics, `lead_analytics` moduli -- ilgari
    "target" ichida bo'lgani uchun sinovda ham ochiq edi) va
    "Raqobatchilar" (Ad Library kuzatuvi, `settings` moduli) ENDI
    ikkalasi ham "Sinov" (trial) tarifida YO'Q -- faqat pullik
    tariflardan (Boshlang'ich, $20 dan) boshlab ochiladi.
  - Raqobatchilar kuzatuvi endi sondan cheklangan (`competitor_limit`):
    Boshlang'ich -- 3 tagacha, Biznes -- 10 tagacha, Ekspert -- cheksiz.
    Bu DB xarajati (`CompetitorAd`/`CompetitorAnalysis` qatorlari va har
    {ROTATION_DAYS} kunda BITTA raqobatchiga ketadigan Meta Ad Library +
    AI-tahlil xarajati) tarifga mutanosib bo'lishi uchun.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Plan:
    key: str
    name: str
    price_usd: "int | None"       # None = pulsiz
    period_days: "int | None"     # trial uchun muddat (kun); pullik tariflarda oylik, None
    tagline: str
    modules: frozenset            # permissions.MODULE_KEYS'dan qaysi biri ochiq
    manager_limit: "int | None"   # None = cheksiz
    leads_limit: "int | None"     # CRM'dagi jami lidlar soni chegarasi; None = cheksiz
    competitor_limit: "int | None"  # Raqobatchilar (Ad Library) kuzatuv soni; None = cheksiz, 0 = modul yopiq
    ai_enabled: bool              # Ichki AI-yordamchi (real vaqtda savol-javob)
    can_connect_meta_ads: bool    # False bo'lsa -- connect-accounts sahifasida faqat Instagram maydoni ko'rinadi
    highlight: bool               # narxlar sahifasida "Eng ommabop" belgisi
    features: tuple                # marketing/taqqoslash jadvali uchun aniq bandlar


PLANS = {
    "trial": Plan(
        # 2026-09, foydalanuvchi so'rovi ("Trial muddati/limitlarini
        # o'zgartirish"): AVVAL 14 kundan 7 kunga qisqartirilgan edi, endi
        # (bosh sahifadagi yangi "ro'yxatdan o'tmaganlar uchun eslatma"
        # popup'i bilan birga so'ralgan) 7 kundan 5 kunga qisqartirildi. Bu
        # YAGONA joy -- `paid_until` shu qiymatdan hisoblanadi (app.py,
        # ro'yxatdan o'tishda), boshqa hech qayerda muddat qattiq
        # yozilmagan (faqat ko'rsatiladigan matnlar, ular ham shu songa
        # moslab yangilandi -- qarang `lang.py`).
        key="trial", name="Sinov", price_usd=None, period_days=5,
        tagline="5 kun bepul — Instagram'ni ulab, xom natijalarni ko'ring",
        modules=frozenset({"dashboard", "leads", "target", "analytics"}),
        manager_limit=1, leads_limit=100, competitor_limit=0,
        ai_enabled=False, can_connect_meta_ads=False, highlight=False,
        features=(
            "5 kun bepul, karta shart emas",
            "Faqat Instagram akkauntini ulash",
            "Target (Meta Ads) bo'yicha XOM natijalar: xarajat, lead, CPL",
            "Lidlar bazasi va asosiy CRM voronkasi (100 tagacha lid)",
            "1 ta admin hisob",
            "Ichki AI-yordamchi kiritilmagan",
            "Lid tahlili va Raqobatchilar (Ad Library) kiritilmagan",
        ),
    ),
    "start": Plan(
        key="start", name="Boshlang'ich", price_usd=20, period_days=None,
        tagline="Kichik jamoalar uchun to'liq CRM + target monitoring",
        modules=frozenset({"dashboard", "leads", "target", "analytics", "settings", "lead_analytics"}),
        manager_limit=2, leads_limit=1000, competitor_limit=3,
        ai_enabled=False, can_connect_meta_ads=True, highlight=False,
        features=(
            "Sinovdagi hammasi",
            "To'liq Meta Ads hisoblar (bir nechta kampaniya) ulash",
            "Meta Conversions API (CAPI) -- konversiya signalini qaytarish",
            "SMM hisobot va Instagram xabarlar",
            "Lid tahlili (CRM/voronka bo'yicha chuqur tahlil)",
            "Raqobatchilar kuzatuvi (Meta Ad Library) -- 3 tagacha",
            "Voronka, majburiy vazifalar, qo'shimcha maydonlar sozlamalari",
            "1 000 tagacha lid (CRM)",
            "2 tagacha menejer/admin hisob",
            "Email orqali qo'llab-quvvatlash",
        ),
    ),
    "business": Plan(
        key="business", name="Biznes", price_usd=60, period_days=None,
        tagline="O'sayotgan sotuv jamoalari uchun — AI bilan kuchaytirilgan",
        modules=frozenset({"dashboard", "leads", "target", "analytics", "settings", "individual_check", "lead_analytics"}),
        manager_limit=4, leads_limit=5000, competitor_limit=10,
        ai_enabled=True, can_connect_meta_ads=True, highlight=True,
        features=(
            "Boshlang'ichdagi hammasi",
            "Raqobatchilar kuzatuvi (Meta Ad Library) -- 10 tagacha",
            "Qo'ng'iroq audio nazorati (Individual tekshirish)",
            "Ichki AI-yordamchi (real vaqtda savol-javob va hisobot)",
            "5 000 tagacha lid (CRM)",
            "4 tagacha menejer/admin hisob",
            "Ustuvor (tezkor) qo'llab-quvvatlash",
        ),
    ),
    "unlimited": Plan(
        key="unlimited", name="Ekspert", price_usd=150, period_days=None,
        tagline="Yirik jamoalar va ko'p filiallar uchun — cheksiz",
        modules=frozenset({"dashboard", "leads", "target", "analytics", "settings", "individual_check", "lead_analytics"}),
        manager_limit=None, leads_limit=None, competitor_limit=None,
        ai_enabled=True, can_connect_meta_ads=True, highlight=False,
        features=(
            "Biznesdagi hammasi",
            "Cheksiz raqobatchilar kuzatuvi (Meta Ad Library)",
            "Cheksiz lidlar (CRM)",
            "Cheksiz menejer/admin hisoblar",
            "Cheksiz qo'ng'iroq audio arxivi",
            "Shaxsiy onboarding va maslahat",
            "24/7 ustuvor qo'llab-quvvatlash",
        ),
    ),
}

PLAN_ORDER = ["trial", "start", "business", "unlimited"]
PLAN_LIST = [PLANS[k] for k in PLAN_ORDER]
PAID_PLAN_LIST = [PLANS[k] for k in PLAN_ORDER if k != "trial"]


# ---------------------------------------------------------------------------
# 2026-09, foydalanuvchi so'rovi ("tariflarni ham imenno har bitta obshiy
# qil, funksiyalarni yozganday, plyus/gollichka -- agar yo'q bo'lsa x, bor
# bo'lsa gollichka"): tariflar sahifasi (`pricing.html`) va bosh sahifadagi
# tariflar bo'limi (`landing.html`) uchun TO'LIQ funksiya-taqqoslash jadvali.
# Har bir qator -- bitta aniq funksiya, har bir ustun -- tarif; qiymat
# `True`/`False` bo'lsa gollichka/X ikonkasi chiqadi, matn (str) bo'lsa
# aynan o'sha matn ko'rsatiladi (menejer soni, qo'llab-quvvatlash darajasi
# kabi "ha/yo'q" bo'lmagan qatorlar uchun).
#
# Qiymatlar YUQORIDAGI `PLANS` lug'atining o'zidan (modules/ai_enabled/
# can_connect_meta_ads/manager_limit/leads_limit) olinadi -- shu sabab bu
# ikkalasi hech qachon bir-biridan uzilib qolmaydi.
#
# CAPI (Meta Conversions API) alohida modul emas -- u `can_connect_meta_ads`
# yoqilgan har qanday tarifda ishlaydi (reklama hisobi ulanishi bilan Pixel
# avtomatik topiladi, ko'proq narsa talab qilinmaydi), shuning uchun bu
# qatorning qiymati ham o'sha bayroqdan olinadi.
# ---------------------------------------------------------------------------
def _has(module_key):
    return {p.key: module_key in p.modules for p in PLAN_LIST}


def _count_or_unlimited(value, unit):
    return "Cheksiz" if value is None else f"{value} tagacha {unit}"


def _competitor_limit_display(p: "Plan"):
    """Raqobatchilar (Ad Library) qatori uchun: `settings` moduli yopiq
    bo'lsa -- umuman kirish yo'q (X). Ochiq bo'lsa -- `competitor_limit`
    (None = cheksiz, son = shuncha tagacha)."""
    if "settings" not in p.modules:
        return False
    return _count_or_unlimited(p.competitor_limit, "raqobatchi")


FEATURE_MATRIX = [
    {"label": "CRM va lidlar bazasi",
     "values": {p.key: _count_or_unlimited(p.leads_limit, "lid") for p in PLAN_LIST}},
    {"label": "Instagram akkauntini ulash", "values": {p.key: True for p in PLAN_LIST}},
    {"label": "To'liq Meta Ads (Instagram + Facebook reklama) ulash",
     "values": {p.key: p.can_connect_meta_ads for p in PLAN_LIST}},
    {"label": "Meta Conversions API (CAPI)",
     "values": {p.key: p.can_connect_meta_ads for p in PLAN_LIST}},
    {"label": "SMM hisobot (obunachi, qamrov, postlar statistikasi)", "values": _has("target")},
    {"label": "Lid tahlili (CRM/voronka bo'yicha chuqur tahlil)", "values": _has("lead_analytics")},
    {"label": "Raqobatchilar kuzatuvi (Meta Ad Library)",
     "values": {p.key: _competitor_limit_display(p) for p in PLAN_LIST}},
    {"label": "Analitika va hisobotlar", "values": _has("analytics")},
    {"label": "Sozlamalar (voronka, vazifalar, qo'shimcha maydonlar)", "values": _has("settings")},
    {"label": "Qo'ng'iroq audio nazorati (Individual tekshirish)", "values": _has("individual_check")},
    {"label": "Ichki AI-yordamchi", "values": {p.key: p.ai_enabled for p in PLAN_LIST}},
    {"label": "Menejer/admin hisoblar soni",
     "values": {p.key: _count_or_unlimited(p.manager_limit, "hisob") for p in PLAN_LIST}},
    {"label": "Qo'llab-quvvatlash",
     "values": {
         "trial": "—", "start": "Email",
         "business": "Ustuvor (tezkor)", "unlimited": "24/7 + shaxsiy onboarding",
     }},
]


def get_plan(key: "str | None") -> Plan:
    return PLANS.get(key) or PLANS["trial"]


def modules_for_plan(key: "str | None") -> frozenset:
    return get_plan(key).modules


def manager_limit_for_plan(key: "str | None") -> "int | None":
    return get_plan(key).manager_limit


def leads_limit_for_plan(key: "str | None") -> "int | None":
    return get_plan(key).leads_limit


def competitor_limit_for_plan(key: "str | None") -> "int | None":
    """Raqobatchi (Ad Library) kuzatuv soni chegarasi. `None` -- cheksiz
    (yoki `settings` moduli yopiq bo'lsa, real cheklov ahamiyatsiz, chunki
    Raqobatchilar sahifasining o'ziga kirish yo'q)."""
    return get_plan(key).competitor_limit


def ai_enabled_for_plan(key: "str | None") -> bool:
    return get_plan(key).ai_enabled


def next_plan_up(key: "str | None") -> "Plan | None":
    """Joriy tarifdan bir pog'ona yuqoridagisini qaytaradi (upsell banner/
    "tarifni oshirish" tavsiyasi uchun) -- eng tepadagi (`unlimited`) uchun
    `None`."""
    plan = get_plan(key)
    try:
        idx = PLAN_ORDER.index(plan.key)
    except ValueError:
        idx = 0
    if idx + 1 >= len(PLAN_ORDER):
        return None
    return PLANS[PLAN_ORDER[idx + 1]]
