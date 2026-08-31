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


def has_module(user, key: str) -> bool:
    """`user` -- Flask-Login `current_user` (ManagerUser) yoki `None`.
    Admin uchun har doim True. Menejer uchun `user.allowed_modules`
    ro'yxatiga qarab tekshiradi."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "role", None) == "admin":
        return True
    allowed = getattr(user, "allowed_modules", None) or []
    return key in allowed
