"""test_kpi_config_offline.py -- PLAN 5-bosqich: KPI/bonus qoidalari har
kompaniyaga alohida sozlanadi.
  1. Standart sozlamada natija ESKI (Dunyabunya) formulalar bilan AYNAN bir xil.
  2. Boshqa qoidalar (oklad, bosqichlar, foiz) to'g'ri qo'llanadi.
  3. Noto'g'ri kiritish (manfiy, matn, bo'sh) yiqitmaydi -- standartga tushadi.
  4. Sozlamalar sahifasi: saqlash/standartga qaytarish; A ning sozlamasi B ga ta'sir qilmaydi.

    cd app && python3 scripts/test_kpi_config_offline.py
"""
import math
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("REPLIX_TEST_DEFAULT_UNSCOPED", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-dummy-token")
os.environ["FLASK_SECRET_KEY"] = "test-secret-kpi"
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(tempfile.mkdtemp(), 'kpi.db')}"

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402
import kpi_bonus as K  # noqa: E402

db_module.init_db()
app_module.app.config["TESTING"] = True
app_module.app.config["WTF_CSRF_ENABLED"] = False
failures = []


def check(name, cond, detail=""):
    print(("OK:   " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# --- eski (2026-09) formulalar, solishtirish uchun ---
def old_rate(n, f=1.0):
    if f <= 0:
        return 0
    t1, t2, t3 = round(75 * f), round(150 * f), round(300 * f)
    if n < max(t1, 1):
        return 0
    if n < t2:
        return 10_000
    if n < t3:
        return 15_000
    return 20_000


def old_turnover(t, f=1.0):
    if f <= 0:
        return 0.0
    b1, b2, b3, b4 = (x * f for x in (75e6, 150e6, 300e6, 400e6))
    if t < b1:
        return 0.0
    if t < b2:
        return 500_000.0
    if t < b3:
        return 1_000_000.0
    if t <= b4:
        return 2_000_000.0
    return 2_000_000.0 + math.ceil((t - b4) / (100e6 * f)) * 500_000.0


D = K.normalize_kpi_config(None)
bad = []
for f in (1.0, 0.5, 0.37):
    for n in list(range(0, 320)) :
        if K.progressive_rate_for_count(n, f, D) != old_rate(n, f):
            bad.append(("rate", n, f))
    for t in [0, 74_999_999, 75e6, 120e6, 150e6, 299e6, 300e6, 400e6, 400e6 + 1, 455e6, 500e6, 501e6, 1.234e9]:
        if abs(K.turnover_bonus_for_amount(t, f, D) - old_turnover(t, f)) > 1e-6:
            bad.append(("turn", t, f))
check("standart: bosqichli bonuslar eski formula bilan aynan bir xil", not bad, str(bad[:5]))
check("standart: 1-xarid 10 000 + 0.5%", K.activation_bonus_for_sale(1, 1_000_000, None, D) == 15_000)
check("standart: 2-xarid 15 kun ichida 20 000 + 0.5%", K.activation_bonus_for_sale(2, 1_000_000, 10, D) == 25_000)
check("standart: 2-xarid 16-kunda 0", K.activation_bonus_for_sale(2, 1_000_000, 16, D) == 0)
r = K.compute_manager_report([{"sale_number": 1, "amount": 1_000_000, "sold_at": None, "days_since_first_sale": None, "lead_id": 1}] * 80, 2026, 9, cfg=D)
check("standart hisobot: oklad 4 mln, 80 sotuv -> 10 000/sotuv", r["oklad"] == 4_000_000 and r["progressive_rate"] == 10_000 and r["bonus_b"] == 800_000)
check("standart hisobot: oborot 80 mln -> 500 000", r["bonus_c"] == 500_000)
check("standart: bosqich nomlari avvalgidek", [t["label"] for t in r["progressive_tiers"]] == ["75 - 149 ta", "150 - 299 ta", "300+ ta"]
      and [t["label"] for t in r["turnover_tiers"]] == ["75 mln - 150 mln", "150 mln - 300 mln", "300 mln - 400 mln"], str([t["label"] for t in r["turnover_tiers"]]))

C = K.normalize_kpi_config({"salary_fixed": 3_000_000, "activation_percent": 1, "activation_first_fixed": 0,
                            "progressive_tiers": [[10, 5000], [20, 7000]], "turnover_tiers": [[10e6, 100_000]],
                            "turnover_top": 0, "survival_min_sales": 10})
r = K.compute_manager_report([{"sale_number": 1, "amount": 1_000_000, "sold_at": None, "days_since_first_sale": None, "lead_id": 1}] * 12, 2026, 9, cfg=C)
check("maxsus: oklad 3 mln", r["oklad"] == 3_000_000)
check("maxsus: 12 sotuv -> 5000/sotuv", r["progressive_rate"] == 5000 and r["bonus_b"] == 60_000)
check("maxsus: 1-xarid 1% fiksiz", r["bonus_a"] == 120_000)
check("maxsus: oborot 12 mln -> 100 000, yuqori bosqichsiz", r["bonus_c"] == 100_000)
check("maxsus: reja 10 bajarildi", r["survival_ok"])

E = K.normalize_kpi_config({"salary_fixed": -5, "activation_percent": "abc", "progressive_tiers": [["x", 1], [0, 5]], "turnover_tiers": []})
check("noto'g'ri qiymatlar standartga tushadi", E["salary_fixed"] == K.SALARY_FIXED and E["activation_percent"] == 0.5)
check("noto'g'ri bosqichlar tashlanadi", E["progressive_tiers"] == [])
r = K.compute_manager_report([], 2026, 9, cfg=E)
check("bosqichlarsiz ham yiqilmaydi", r["bonus_b"] == 0 and r["bonus_c"] == 0)

s = db_module.get_session()
try:
    with db_module.unscoped():
        a = s.get(db_module.Company, db_module.get_default_company_id())
        b = db_module.Company(name="B", plan="unlimited", is_active=True)
        s.add(b)
        s.commit()
        for comp, u in ((a, "a_admin"), (b, "b_admin")):
            m = db_module.Manager(username=u, role="admin", company_id=comp.id)
            m.set_password("parol12345")
            s.add(m)
        s.commit()
        a_id, b_id = a.id, b.id
finally:
    s.close()
c = app_module.app.test_client()
c.post("/login", data={"username": "b_admin", "password": "parol12345"})
r = c.get("/sozlamalar/umumiy")
check("sozlamalar sahifasi 200 va KPI formasi bor", r.status_code == 200 and "set_kpi_config" in r.get_data(as_text=True))
c.post("/sozlamalar/umumiy", data={"action": "set_kpi_config", "salary_fixed": "3500000", "activation_percent": "1",
                                   "progressive_tiers": "50 = 8 000\n100=12000\nnoto'g'ri", "turnover_tiers": "50 000 000 = 300000"})
with db_module.scoped_as(b_id):
    cb = K.get_kpi_config(b_id)
check("B: saqlandi", cb["salary_fixed"] == 3_500_000 and cb["progressive_tiers"] == [[50, 8000], [100, 12000]]
      and cb["turnover_tiers"] == [[50_000_000, 300_000]], str(cb))
check("A ga ta'sir qilmadi", K.get_kpi_config(a_id)["salary_fixed"] == K.SALARY_FIXED)
c.post("/sozlamalar/umumiy", data={"action": "set_kpi_config", "reset": "1"})
check("standartga qaytarildi", K.get_kpi_config(b_id)["salary_fixed"] == K.SALARY_FIXED)
s = db_module.get_session()
try:
    with db_module.unscoped():
        mgr = db_module.Manager(username="b_mgr", role="manager", company_id=b_id, full_name="B Menejer")
        mgr.set_password("parol12345")
        s.add(mgr)
        s.commit()
        lead = db_module.Lead(company_id=b_id, full_name="Mijoz", phone="+998901112233", source="manual", status="sold", assigned_manager_id=mgr.id)
        s.add(lead)
        s.commit()
        import datetime as _dt
        s.add(db_module.Sale(company_id=b_id, lead_id=lead.id, manager_id=mgr.id, amount=2_000_000, sale_number=1, sold_at=_dt.datetime.utcnow()))
        s.commit()
except Exception as e:  # Sale maydonlari farq qilsa ham sahifa testi davom etsin
    print("  (sotuv yaratilmadi:", type(e).__name__, e, ")")
finally:
    s.close()
for cfg_post in ({"action": "set_kpi_config", "progressive_tiers": "", "turnover_tiers": "", "turnover_top": "0"}, {"action": "set_kpi_config", "reset": "1"}):
    c.post("/sozlamalar/umumiy", data=cfg_post)
    r = c.get("/analitika")
    check(f"Analitika sahifasi 200 ({'standart' if cfg_post.get('reset') else 'bosqichsiz'})", r.status_code == 200, str(r.status_code))

if failures:
    print(f"\n{len(failures)} ta XATO")
    sys.exit(1)
print("\nBARCHA TESTLAR O'TDI")
