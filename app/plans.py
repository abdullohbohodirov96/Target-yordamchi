"""plans.py — SaaS tarif (pricing tier) katalogi.

2026-09, foydalanuvchi so'rovi: ochiq (o'z-o'zidan) ro'yxatdan o'tish +
tariflar tizimi ("тарифы, чтобы выбирался тариф... как ты сам анализировал
и сам создал"). Narx va xususiyatlarni ANIQ shu so'rov asosida MEN
loyihalashtirdim:

  - Eng oddiy PULLIK tarif ($50/oy dan) foydalanuvchining aniq talabi bo'yicha
    boshlanadi, undan yuqorisi bosqichma-bosqich ko'proq imkoniyat + sog'lom
    foyda marjasi bilan o'sadi (OpenAI/Meta/Moi Zvonki xarajatlari past
    tarifda deyarli nolga yaqin bo'lgani uchun marja pastda ham katta).
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
    ai_enabled: bool              # AI qo'ng'iroq tahlili + ichki AI-yordamchi
    can_connect_meta_ads: bool    # False bo'lsa -- connect-accounts sahifasida faqat Instagram maydoni ko'rinadi
    highlight: bool               # narxlar sahifasida "Eng ommabop" belgisi
    features: tuple                # marketing/taqqoslash jadvali uchun aniq bandlar


PLANS = {
    "trial": Plan(
        key="trial", name="Sinov", price_usd=None, period_days=14,
        tagline="14 kun bepul — Instagram'ni ulab, xom natijalarni ko'ring",
        modules=frozenset({"dashboard", "leads", "target", "analytics"}),
        manager_limit=1, ai_enabled=False, can_connect_meta_ads=False, highlight=False,
        features=(
            "14 kun bepul, karta shart emas",
            "Faqat Instagram akkauntini ulash",
            "Target (Meta Ads) bo'yicha XOM natijalar: xarajat, lead, CPL",
            "Lidlar bazasi va asosiy CRM voronkasi",
            "1 ta admin hisob",
            "AI tahlil va qo'ng'iroq nazorati kiritilmagan",
        ),
    ),
    "start": Plan(
        key="start", name="Boshlang'ich", price_usd=50, period_days=None,
        tagline="Kichik jamoalar uchun to'liq CRM + target monitoring",
        modules=frozenset({"dashboard", "leads", "target", "analytics", "settings"}),
        manager_limit=3, ai_enabled=False, can_connect_meta_ads=True, highlight=False,
        features=(
            "Sinovdagi hammasi",
            "To'liq Meta Ads hisoblar (bir nechta kampaniya) ulash",
            "SMM hisobot va Instagram xabarlar",
            "Voronka, majburiy vazifalar, qo'shimcha maydonlar sozlamalari",
            "3 tagacha menejer/admin hisob",
            "Email orqali qo'llab-quvvatlash",
        ),
    ),
    "business": Plan(
        key="business", name="Biznes", price_usd=120, period_days=None,
        tagline="O'sayotgan sotuv jamoalari uchun — AI bilan kuchaytirilgan",
        modules=frozenset({"dashboard", "leads", "target", "analytics", "settings", "individual_check"}),
        manager_limit=8, ai_enabled=True, can_connect_meta_ads=True, highlight=True,
        features=(
            "Boshlang'ichdagi hammasi",
            "AI qo'ng'iroq tahlili (Individual tekshirish)",
            "Ichki AI-yordamchi (real vaqtda savol-javob va hisobot)",
            "8 tagacha menejer/admin hisob",
            "Ustuvor (tezkor) qo'llab-quvvatlash",
        ),
    ),
    "unlimited": Plan(
        key="unlimited", name="Ekspert", price_usd=250, period_days=None,
        tagline="Yirik jamoalar va ko'p filiallar uchun — cheksiz",
        modules=frozenset({"dashboard", "leads", "target", "analytics", "settings", "individual_check"}),
        manager_limit=None, ai_enabled=True, can_connect_meta_ads=True, highlight=False,
        features=(
            "Biznesdagi hammasi",
            "Cheksiz menejer/admin hisoblar",
            "Cheksiz AI qo'ng'iroq tahlili",
            "Shaxsiy onboarding va maslahat",
            "24/7 ustuvor qo'llab-quvvatlash",
        ),
    ),
}

PLAN_ORDER = ["trial", "start", "business", "unlimited"]
PLAN_LIST = [PLANS[k] for k in PLAN_ORDER]
PAID_PLAN_LIST = [PLANS[k] for k in PLAN_ORDER if k != "trial"]


def get_plan(key: "str | None") -> Plan:
    return PLANS.get(key) or PLANS["trial"]


def modules_for_plan(key: "str | None") -> frozenset:
    return get_plan(key).modules


def manager_limit_for_plan(key: "str | None") -> "int | None":
    return get_plan(key).manager_limit


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
