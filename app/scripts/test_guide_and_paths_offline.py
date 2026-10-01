"""test_guide_and_paths_offline.py -- PLAN 7-bosqich: Qo'llanma (/qollanma)
va bosh sahifadagi "Kim uchun" (biznes egasi / targetolog) bo'limi.
  1. /qollanma mehmonga ham ochiq, 3 tilda 10 bo'lim, tarjima kaliti ochiq emas.
  2. Kirgan foydalanuvchiga har bo'limda ilovadagi sahifaga "Ochish" havolasi
     (barcha endpoint'lar mavjud).
  3. Obunasi tugagan kompaniya ham qo'llanmani ochadi.
  4. Bosh sahifada ikki yo'l, qo'llanma havolasi, sinov kunlari plans.py'dan.

    cd app && python3 scripts/test_guide_and_paths_offline.py
"""
import datetime as dt
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("REPLIX_TEST_DEFAULT_UNSCOPED", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-dummy-token")
os.environ["FLASK_SECRET_KEY"] = "test-secret-guide"
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(tempfile.mkdtemp(), 'guide.db')}"

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402
import guide_content  # noqa: E402
import plans  # noqa: E402

db_module.init_db()
app_module.app.config["TESTING"] = True
app_module.app.config["WTF_CSRF_ENABLED"] = False
failures = []


def check(name, cond, detail=""):
    print(("OK:   " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


trial = plans.PLANS["trial"].period_days
c = app_module.app.test_client()
for code in ("uz", "ru", "en"):
    r = c.get(f"/qollanma?lang={code}")
    body = r.get_data(as_text=True)
    texts, secs = guide_content.sections_for(code, trial)
    check(f"/qollanma [{code}] mehmonga 200", r.status_code == 200, str(r.status_code))
    check(f"/qollanma [{code}] 10 bo'lim", body.count('class="card gd-sec"') == 10, str(body.count('class="card gd-sec"')))
    check(f"/qollanma [{code}] sinov kunlari plans.py'dan", f"{trial}" in secs[0]["steps"][0] and "{trial_days}" not in body)
    check(f"/qollanma [{code}] mehmonga 'Ochish' yo'q", texts["open"] + " →" not in body)
    for key in texts:
        if isinstance(texts[key], tuple):
            check(f"[{code}] {key} bo'sh emas", texts[key][0] and all(texts[key][1]))

    r = c.get(f"/?lang={code}")
    body = r.get_data(as_text=True)
    check(f"/ [{code}] 'Kim uchun' bo'limi", 'id="kim-uchun"' in body and body.count('class="lp-path"') == 2)
    check(f"/ [{code}] qo'llanma havolasi", "/qollanma" in body)
    check(f"/ [{code}] tarjima kaliti ochiq emas", "paths." not in body and "guide.card" not in body)

with app_module.app.test_request_context():
    missing = []
    for sec in guide_content.SECTIONS:
        try:
            app_module.url_for(sec["endpoint"])
        except Exception as e:  # noqa: BLE001
            missing.append((sec["endpoint"], type(e).__name__))
check("barcha bo'lim endpoint'lari mavjud", not missing, str(missing))

s = db_module.get_session()
try:
    with db_module.unscoped():
        comp = db_module.Company(name="Eskirgan", plan="start", is_active=True, paid_until=dt.datetime.utcnow() - dt.timedelta(days=3))
        s.add(comp)
        s.commit()
        m = db_module.Manager(username="old_admin", role="admin", company_id=comp.id)
        m.set_password("parol12345")
        s.add(m)
        s.commit()
finally:
    s.close()
c2 = app_module.app.test_client()
c2.post("/login", data={"username": "old_admin", "password": "parol12345"})
r = c2.get("/qollanma")
body = r.get_data(as_text=True)
check("obunasi tugagan kompaniya ham qo'llanmani ochadi", r.status_code == 200, str(r.status_code))
check("kirgan foydalanuvchiga 'Ochish' havolalari", body.count("Ochish →") >= 8, str(body.count("Ochish →")))

if failures:
    print(f"\n{len(failures)} ta XATO")
    sys.exit(1)
print("\nBARCHA TESTLAR O'TDI")
