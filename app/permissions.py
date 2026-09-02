"""
permissions.py — CRM bo'limlariga (modullariga) kirish huquqlarini boshqarish.

Yangi hisob ochilganda (yoki mavjudini tahrirlashda) admin har bir MENEJER
uchun qaysi bo'limlar ochiq bo'lishini belgilay oladi -- masalan bitta
menejerga faqat "Lidlar" ko'rinsin, ikkinchisiga "Analitika" ham ochiq
bo'lsin. ADMIN roli uchun bu ro'yxat E'TIBORSIZ -- adminda HAR DOIM
hammasi ochiq.

MUHIM (2026-08, foydalanuvchi so'rovi bilan kengaytirildi): avval faqat 3 ta
bo'lim (dashboard/leads/analytics) toggle qilinar edi, qolgan hamma narsa
(Target, Individual tekshirish, SMM, Sozlamalar) qattiq admin-only edi --
"ko'p bo'limda menejerga dostup berib bo'lmayapti" shikoyati shundan edi.
Endi deyarli HAR BIR bo'lim shu ro'yxatda -- admin xohlagan menejerga
xohlagan bo'limni yoqib/o'chirib bera oladi. FAQAT "Menejerlar" (hisob
boshqaruvi -- boshqa xodimlarning login/maosh/rolini o'zgartirish) bu
ro'yxatda ATAYLAB YO'Q va har doim qat'iy admin-only qoladi -- bu boshqa
turdagi (hisob xavfsizligi) huquq, oddiy "bo'limga dostup" emas.
"""

import json

# (key, ko'rinadigan nom) -- tartib shu yerda ko'rsatilgan tartibda,
# manager_edit.html'dagi checkbox ro'yxati va navbar shu tartibni ishlatadi.
MODULES = [
    ("dashboard", "Dashboard (umumiy ko'rinish)"),
    ("leads", "Lidlar"),
    ("analytics", "Analitika (hisobotlar)"),
    ("target", "Target (Meta Ads + SMM hisobot + Instagram xabarlar)"),
    ("individual_check", "Individual tekshirish (qo'ng'iroq nazorati)"),
    ("settings", "Sozlamalar (voronka, kvalifikatsiya savollari, doimiy vazifalar)"),
]

MODULE_KEYS = {key for key, _ in MODULES}

# "Sozlamalar" ATAYLAB kompaniya-darajasidagi yoqish/o'chirish ro'yxatiga
# KIRITILMAYDI -- aks holda admin uni o'chirib qo'ysa, `settings_hub`
# routening o'zi `module_required("settings")` bilan yopilgani uchun
# hech kim (admin ham) uni QAYTA YOQA OLMAY QOLARDI (o'zini-o'zi
# qulflab qo'yish). Bu FAQAT "butun kompaniya uchun o'chirish" ro'yxati
# uchun cheklov -- menejerga alohida ruxsat berish (`allowed_modules`)
# hamon "settings"ni o'z ichiga oladi.
TOGGLEABLE_MODULE_KEYS = [key for key, _ in MODULES if key != "settings"]

# Yangi menejer hisobi standart holatda qaysi bo'limlarni ko'radi (admin
# checkbox orqali keyinroq o'zgartirishi mumkin) -- "Lidlar" har doim
# beriladi, chunki menejerning asosiy ishi shu.
DEFAULT_MANAGER_MODULES = ["leads"]


def parse_allowed_modules(raw_json: str | None) -> list[str]:
    if not raw_json:
        return list(DEFAULT_MANAGER_MODULES)
    try:
        parsed = json.loads(raw_json)
    except (TypeError, ValueError):
        return list(DEFAULT_MANAGER_MODULES)
    if not isinstance(parsed, list):
        return list(DEFAULT_MANAGER_MODULES)
    return [m for m in parsed if m in MODULE_KEYS]


def serialize_allowed_modules(modules: list[str]) -> str:
    clean = [m for m in modules if m in MODULE_KEYS]
    return json.dumps(clean, ensure_ascii=False)


def parse_disabled_modules(raw_json: str | None) -> list[str]:
    """`Company.disabled_modules` uchun -- bo'sh/None = hech narsa qo'lda
    o'chirilmagan."""
    if not raw_json:
        return []
    try:
        parsed = json.loads(raw_json)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [m for m in parsed if m in MODULE_KEYS]


def serialize_disabled_modules(modules: list[str]) -> str:
    clean = [m for m in modules if m in MODULE_KEYS]
    return json.dumps(clean, ensure_ascii=False)


def _company_disabled_set(user) -> set:
    """`user.company_id` orqali JORIY SO'ROV davomida kompaniyaning
    `disabled_modules`ini BIR MARTA o'qib, `flask.g`da keshlaydi.

    MUHIM: bu `app.py`dagi `_current_company()` keshidan ATAYLAB
    MUSTAQIL -- `has_module` ba'zan (masalan `module_required`
    dekoratorida) `_current_company()` chaqirilishidan OLDIN ishga
    tushadi, shuning uchun o'zining alohida (lekin xuddi shunday
    so'rov-davomida-bir-marta) keshiga ega."""
    company_id = getattr(user, "company_id", None)
    if company_id is None:
        return set()
    try:
        from flask import g
    except ImportError:
        return set()
    cache_attr = f"_perm_disabled_modules_{company_id}"
    if not hasattr(g, cache_attr):
        raw = None
        try:
            import db as db_module
            session = db_module.get_session()
            try:
                with db_module.unscoped():
                    company = session.get(db_module.Company, company_id)
                raw = company.disabled_modules if company is not None else None
            finally:
                session.close()
        except Exception:
            raw = None
        setattr(g, cache_attr, parse_disabled_modules(raw))
    return set(getattr(g, cache_attr))


def has_module(user, key: str) -> bool:
    """`user` -- Flask-Login `current_user` (ManagerUser) yoki `None`.

    Tekshiruv tartibi:
      1. Kompaniya darajasida ADMIN qo'lda o'chirib qo'yganmi
         (`Company.disabled_modules`) -- bo'lsa, ADMIN uchun ham,
         MENEJER uchun ham YOPIQ (bu "butun kompaniya uchun funksiyani
         o'chirish", shaxsiy ruxsat emas).
      2. Admin uchun (agar (1)da o'chirilmagan bo'lsa) har doim True.
      3. Menejer uchun `user.allowed_modules` ro'yxatiga qarab."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if key in _company_disabled_set(user):
        return False
    if getattr(user, "role", None) == "admin":
        return True
    allowed = getattr(user, "allowed_modules", None) or []
    return key in allowed
