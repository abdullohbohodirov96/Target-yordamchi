"""test_meta_pixel_snippet_offline.py — 2026-09, foydalanuvchi so'rovi:
"meta pixel ulimiz hamma narsani sanga tashba beraman tog'rilavor" --
Meta Events Manager'dan olingan asosiy (base) Pixel kodini (ID:
2060099542047401) butun sayt uchun umumiy `base.html`ga qo'shdik (Meta
o'zi tavsiya qilgan "har bir sahifada" qoidasiga muvofiq).

Tekshiradi:
  - Pixel <script> kodi va <noscript> fallback rasmi TO'G'RI Pixel ID
    bilan bosh sahifada (login qilinmagan holatda ham) mavjudligini.
  - Xuddi shu kod ichki (login qilingan) sahifalarda ham mavjudligini
    (Meta'ning "har bir sahifada" tavsiyasiga muvofiq).
  - <script> ichidagi JS sintaksisi to'g'ri ekanini (`node --check`).

Ishga tushirish:
    cd app && python3 scripts/test_meta_pixel_snippet_offline.py
"""
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMPDIR = tempfile.mkdtemp()
_DB_PATH = os.path.join(_TMPDIR, "test_pixel.db")

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("META_ACCESS_TOKEN", "test-dummy-token")
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_test_dummy")
os.environ.setdefault("META_PAGE_ID", "page_test_dummy")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402

app_module.app.config["TESTING"] = True
db_module.init_db()

_PIXEL_ID = "2060099542047401"

_session = db_module.get_session()
try:
    admin = db_module.Manager(username="pixel_admin", full_name="Admin", role="admin", company_id=1)
    admin.set_password("parol123")
    _session.add(admin)
    _session.commit()
finally:
    _session.close()

client = app_module.app.test_client()
failures = []


def check(name, cond):
    print(("OK  " if cond else "FAIL") + " " + name)
    if not cond:
        failures.append(name)


def _extract_pixel_script(html):
    m = re.search(r"<script>\s*!function\(f,b,e,v,n,t,s\)(.*?)</script>", html, re.S)
    return m.group(0) if m else None


# --- 1. Bosh sahifa (login qilinmagan, ochiq) ---
html = client.get("/").get_data(as_text=True)
check("bosh sahifada Pixel <script> mavjud", f"fbq('init', '{_PIXEL_ID}')" in html)
check("bosh sahifada Pixel PageView kuzatuvi bor", "fbq('track', 'PageView')" in html)
check("bosh sahifada <noscript> fallback rasm mavjud", f"facebook.com/tr?id={_PIXEL_ID}" in html)

pixel_script = _extract_pixel_script(html)
check("Pixel <script> bloki topildi", pixel_script is not None)
if pixel_script:
    js_body = re.sub(r"^<script>|</script>$", "", pixel_script.strip())
    r = subprocess.run(["node", "--check", "-"], input=js_body, capture_output=True, text=True)
    check("Pixel <script> JS sintaksisi to'g'ri", r.returncode == 0)

# --- 2. Ichki (login qilingan) sahifa -- Meta "har bir sahifada" tavsiyasi ---
client.post("/login", data={"username": "pixel_admin", "password": "parol123"})
html_dashboard = client.get("/").get_data(as_text=True)
check("Dashboard'da (login qilingan) ham Pixel kodi bor", f"fbq('init', '{_PIXEL_ID}')" in html_dashboard)

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
else:
    print("ALL PASSED")
