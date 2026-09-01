"""test_seo_landing_offline.py — 2026-09, SEO/AEO tuzatishni tekshirish
(foydalanuvchi so'rovi: "web toliq malumot berish kerak ai qoganlar
qidirganda ideal chiqishi uchun kriteriyalarin boyicha yoz ozin").

Tekshiradi:
  - Anonim foydalanuvchi uchun "/" JSON-LD bloklari (Organization,
    WebSite, SoftwareApplication, FAQPage) to'g'ri, valid JSON ekanini.
  - "Replix nima?" FAQ bloki ko'rinadigan HTML'da borligini.
  - /login va /signup sahifalarida <meta name="robots" content="noindex">
    borligini.
  - /sitemap.xml endi /login va /signup'ni o'z ichiga olmasligini.

Ishga tushirish:
    cd app && python3 scripts/test_seo_landing_offline.py
"""
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMPDIR = tempfile.mkdtemp()
_DB_PATH = os.path.join(_TMPDIR, "test_seo.db")

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

client = app_module.app.test_client()

failures = []


def check(name, cond):
    print(("OK  " if cond else "FAIL") + " " + name)
    if not cond:
        failures.append(name)


r = client.get("/")
check("/ status 200", r.status_code == 200)
body = r.get_data(as_text=True)

blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', body, re.S)
check("4 JSON-LD blocks present", len(blocks) == 4)
types = []
for b in blocks:
    try:
        parsed = json.loads(b)
        types.append(parsed.get("@type"))
    except Exception as e:
        check(f"JSON-LD block valid ({b[:40]!r})", False)
check("JSON-LD types = Organization/WebSite/SoftwareApplication/FAQPage",
      types == ["Organization", "WebSite", "SoftwareApplication", "FAQPage"])

check("Organization has disambiguatingDescription",
      "disambiguatingDescription" in blocks[0])
check("FAQPage has 5 questions",
      json.loads(blocks[3])["mainEntity"].__len__() == 5 if len(blocks) == 4 else False)

check("visible FAQ section (lp-faq) present", "lp-faq" in body)
check("visible 'Replix nima?' text present", "Replix nima?" in body)
check("visible disambiguation text present ('Replix.ai' mentioned)", "Replix.ai" in body)

r_login = client.get("/login")
check("/login has noindex meta", 'name="robots" content="noindex' in r_login.get_data(as_text=True))

r_signup = client.get("/signup")
check("/signup has noindex meta", 'name="robots" content="noindex' in r_signup.get_data(as_text=True))

r_sitemap = client.get("/sitemap.xml")
sitemap_body = r_sitemap.get_data(as_text=True)
check("sitemap excludes /login", "<loc>http://localhost/login</loc>" not in sitemap_body)
check("sitemap excludes /signup", "<loc>http://localhost/signup</loc>" not in sitemap_body)
check("sitemap still includes / and /tariflar",
      "<loc>http://localhost/</loc>" in sitemap_body and "<loc>http://localhost/tariflar</loc>" in sitemap_body)

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
else:
    print("ALL PASSED")
