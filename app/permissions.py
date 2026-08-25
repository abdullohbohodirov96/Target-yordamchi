"""
permissions.py — CRM bo'limlariga (modullariga) kirish huquqlarini boshqarish.

Yangi hisob ochilganda (yoki mavjudini tahrirlashda) admin har bir MENEJER
uchun qaysi bo'limlar ochiq bo'lishini belgilay oladi -- masalan bitta
menejerga faqat "Lidlar" ko'rinsin, ikkinchisiga "Analitika" ham ochiq
bo'lsin. ADMIN roli uchun bu ro'yxat E'TIBORSIZ -- adminda HAR DOIM
hammasi ochiq.

MUHIM: "Individual tekshirish" (menejerlarning haqiqiy qo'ng'iroq
faoliyatini tekshirish bo'limi) bu ro'yxatda ATAYLAB YO'Q -- u hech qachon
menejerga berilmaydigan, qattiq admin-only bo'lim (`app.py`da alohida
`@admin_required` bilan himoyalangan, `module_required()` orqali emas).
"""

import json

# (key, ko'rinadigan nom) -- tartib shu yerda ko'rsatilgan tartibda,
# manager_edit.html'dagi checkbox ro'yxati va navbar shu tartibni ishlatadi.
MODULES = [
    ("dashboard", "Dashboard va Target (umumiy ko'rinish + reklama xarajat statistikasi)"),
    ("leads", "Lidlar"),
    ("analytics", "Analitika (hisobotlar)"),
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
