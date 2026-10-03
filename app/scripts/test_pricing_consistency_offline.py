"""test_pricing_consistency_offline.py -- 2026-10-01 "narxlarni webda to'liq
to'g'irla": barcha sahifalarda narx BITTA manbadan (plans.py) va Payme
AYNAN yechadigan so'm summasi ham ko'rsatiladi.
  1. Landing/pricing/signup (uz/ru/en) -- har bir pullik tarif $ va so'm bilan.
  2. So'm summasi = payme_subscribe.usd_to_tiyin()/100 (kartadan yechiladigan).
  3. Tarif bandlarida tarjima kaliti ochiq qolmagan ("plans.feature_...").
  4. Saytda eski/boshqa narxlar ($50/$120/$250) yo'q; "--" o'rniga "—".
  5. To'lov sahifasida so'm summasi.

    cd app && python3 scripts/test_pricing_consistency_offline.py
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("REPLIX_TEST_DEFAULT_UNSCOPED", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-dummy-token")
os.environ["FLASK_SECRET_KEY"] = "test-secret-price"
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(tempfile.mkdtemp(), 'price.db')}"

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402
import lang  # noqa: E402
import payme_subscribe  # noqa: E402
import plans  # noqa: E402

db_module.init_db()
app_module.app.config["TESTING"] = True
app_module.app.config["WTF_CSRF_ENABLED"] = False
failures = []


def check(name, cond, detail=""):
    print(("OK:   " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def uzs(p):
    return f"{payme_subscribe.usd_to_tiyin(p) // 100:,}".replace(",", " ")


paid = [p for p in plans.PAID_PLAN_LIST if p.price_usd]
c = app_module.app.test_client()
for code in ("uz", "ru", "en"):
    for path in ("/", "/tariflar", "/signup"):
        r = c.get(f"{path}?lang={code}", follow_redirects=True)
        body = r.get_data(as_text=True)
        check(f"{path} [{code}] 200", r.status_code == 200, str(r.status_code))
        for p in paid:
            check(f"{path} [{code}] {p.key}: ${p.price_usd} va {uzs(p.price_usd)}",
                  f"${p.price_usd}" in body and uzs(p.price_usd) in body)
        check(f"{path} [{code}] tarjima kaliti ochiq emas", "plans.feature_" not in body and "igb." not in body)
        # Taqqoslash bo'limidagi "~$50–100/oy" (BOSHQA xizmatlar narxi) hisobga olinmaydi --
        # faqat tarif kartalaridagi narx.
        card_prices = set(re.findall(r'lp-plan-price">\s*<b>\$(\d+)</b>', body))
        check(f"{path} [{code}] tarif kartalarida faqat plans.py narxlari",
              card_prices <= {str(p.price_usd) for p in paid}, str(card_prices))

for code in ("uz", "ru", "en"):
    for p in plans.localized_plan_list(plans.PLAN_LIST, code):
        bad = [f for f in p.features if f.startswith("plans.")]
        check(f"{p.key} [{code}] barcha bandlar tarjima qilingan", not bad, str(bad))

dashes = [k for k, v in lang.TRANSLATIONS["uz"].items() if isinstance(v, str) and " -- " in v]
check("uz matnlarda ' -- ' qolmagan", not dashes, str(dashes[:5]))
check("og_description min narxdan", "{min_price}" not in lang.translate("shell.og_description", "uz")
      and f"${min(p.price_usd for p in paid)}" in lang.translate("shell.og_description", "uz"))

s = db_module.get_session()
try:
    with db_module.unscoped():
        comp = db_module.Company(name="Narx", plan="start", is_active=True)
        s.add(comp)
        s.commit()
        m = db_module.Manager(username="narx_admin", role="admin", company_id=comp.id)
        m.set_password("parol12345")
        s.add(m)
        s.commit()
finally:
    s.close()
c2 = app_module.app.test_client()
c2.post("/login", data={"username": "narx_admin", "password": "parol12345"})
body = c2.get("/tolov").get_data(as_text=True)
check("to'lov sahifasida so'm summasi", uzs(plans.PLANS["start"].price_usd) in body)

if failures:
    print(f"\n{len(failures)} ta XATO")
    sys.exit(1)
print("\nBARCHA TESTLAR O'TDI")
