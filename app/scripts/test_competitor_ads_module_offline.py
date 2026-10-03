"""test_competitor_ads_module_offline.py — Competitor Ads / Ad Library moduli
(services/ad_library + /raqobatchilar/reklamalar + /api/ad-library/*),
tarmoqsiz va brauzersiz:

  1. parse_query: nom, Page ID, profile.php?id=, /people/.../ID, vanity URL,
     Ad Library havolasi (view_all_page_id / q), instagram.com, noto'g'ri mamlakat.
  2. provider_chain/search_ads: AD_LIBRARY_PROVIDER tanlovi, auto navbat,
     xatoda keyingi providerga o'tish, hammasi xato -> oxirgi xato, sozlanmagan.
  3. Normalizatsiya: web (adlib dict) -> Ad (media_type, matn, sarlavha, CTA,
     platforma, sana, running_days); vanity filtri.
  4. Apify provider: HTTP mock -> Ad; 401/402 -> tushunarli xato; token yo'q -> sozlanmagan.
  5. Meta API provider: page_id -> search_page_ids; xato -> AdLibraryError.
  6. Analyzer: JSON tahlil (6 maydon), kesh (2-chaqiruvda AI yo'q), kunlik
     limit, AI xato / buzuq javob.
  7. API: qidiruv (ok/xato/qisqa so'rov), AI tahlil faqat server ko'rgan
     reklamaga (boshqa kompaniya ID'si -> 404), sahifa va menyu.
"""
import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(tempfile.mkdtemp(), 'cads.db')}"
for k, v in (("ANTHROPIC_API_KEY", "test-dummy"), ("META_ACCESS_TOKEN", "test-dummy-token"),
             ("META_AD_ACCOUNT_ID", "act_test"), ("FLASK_SECRET_KEY", "test-secret")):
    os.environ.setdefault(k, v)
for k in ("ADLIB_SCRAPER", "APIFY_TOKEN", "AD_LIBRARY_PROVIDER", "RENDER", "AD_LIBRARY_AI_DAILY_LIMIT"):
    os.environ.pop(k, None)

import requests  # noqa: E402

import adlib_scraper  # noqa: E402
import meta_api  # noqa: E402
from services import ad_library as AL  # noqa: E402
from services.ad_library import analyzer, store  # noqa: E402
from services.ad_library.providers import apify as apify_mod  # noqa: E402

FAILED = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILED.append(name)


# 1. parse_query
P = AL.parse_query
q = P("Texnomart"); check("nom -> term", q.kind == "name" and q.term == "Texnomart" and q.country == "UZ")
q = P("525570110851332"); check("raqam -> page_id", q.kind == "page_id" and q.page_id == "525570110851332")
q = P("https://www.facebook.com/profile.php?id=61574899049202"); check("profile.php?id -> page_id", q.page_id == "61574899049202")
q = P("facebook.com/people/Arboss/100089123456789/"); check("/people/.../ID -> page_id", q.page_id == "100089123456789")
q = P("https://facebook.com/texnomart.uz"); check("vanity URL -> page_url + term", q.kind == "page_url" and q.page_vanity == "texnomart.uz" and q.term == "texnomart")
q = P("https://www.facebook.com/ads/library/?view_all_page_id=525570110851332&search_type=page"); check("Ad Library page havolasi", q.page_id == "525570110851332")
q = P("https://www.facebook.com/ads/library/?q=arboss&country=UZ"); check("Ad Library q havolasi", q.term == "arboss" and not q.page_id)
q = P("https://instagram.com/dunyabunya_uz/"); check("instagram -> term", q.term == "dunyabunya uz")
q = P("x", country="'; DROP"); check("noto'g'ri mamlakat -> UZ", q.country == "UZ")
q = P("arboss", country="all"); check("ALL mamlakat", q.country == "ALL")
check("bo'sh -> bo'sh so'rov", P("  ").term == "" and P("  ").page_id is None)

# 3. normalizatsiya
RAW = [
    {"id": "1", "page_id": "525", "page_name": "Texnomart", "display_format": "VIDEO", "video_url": "https://v/sd.mp4",
     "preview_image_url": "https://v/p.jpg", "primary_text": "Muddatli to'lov 0%", "headline": "Pura 90",
     "cta_text": "Shop now", "platforms": ["FACEBOOK", "INSTAGRAM"], "is_active": True,
     "ad_delivery_start_time": "2026-08-21T07:00:00+0000", "page_profile_uri": "https://www.facebook.com/texnomart.uz/",
     "collation_count": 4, "page_like_count": "58913"},
    {"id": "2", "page_id": "999", "page_name": "Boshqa", "display_format": "CAROUSEL", "card_images": ["https://c/1.jpg", "https://c/2.jpg"],
     "primary_text": "Karusel", "is_active": True, "ad_delivery_start_time": "2026-09-30T00:00:00+0000",
     "page_profile_uri": "https://www.facebook.com/texnomart.uzbekistan/"},
    {"id": "3", "page_id": "525", "page_name": "Texnomart", "display_format": "IMAGE", "image_url": "https://i/1.jpg",
     "is_active": False, "ad_delivery_start_time": "2026-07-01T00:00:00+0000", "ad_delivery_stop_time": "2026-07-11T00:00:00+0000",
     "page_profile_uri": "https://www.facebook.com/texnomart.uz/"},
]
os.environ["ADLIB_SCRAPER"] = "1"
with mock.patch.object(adlib_scraper, "search", return_value=RAW) as s:
    ads, prov = AL.search_ads(P("https://facebook.com/texnomart.uz"))
    check("web provider tanlandi", prov == "web" and s.call_args.kwargs.get("page_id") is None)
    check("vanity filtri: faqat texnomart.uz sahifasi, faqat faol", [a.id for a in ads] == ["1"])
    a = ads[0]
    check("video: media_type/video/poster", a.media_type == "video" and a.video_url == "https://v/sd.mp4" and a.video_poster_url == "https://v/p.jpg")
    check("matn/sarlavha/CTA/platforma", a.primary_text.startswith("Muddatli") and a.headline == "Pura 90" and a.cta_text == "Shop now" and a.platforms == ["facebook", "instagram"])
    check("sana va variantlar, obunachi int", a.start_date == "2026-08-21" and a.variants == 4 and a.page_like_count == 58913)
    check("running_days hisoblanadi", isinstance(a.to_dict()["running_days"], int))
    ads, _ = AL.search_ads(P("texnomart", active_only=False))
    by = {x.id: x for x in ads}
    check("karusel: birinchi karta rasmi", by["2"].media_type == "carousel" and by["2"].image_url == "https://c/1.jpg")
    check("to'xtagan: running_days = start..end (10)", by["3"].running_days == 10 and not by["3"].is_active)
with mock.patch.object(adlib_scraper, "search", return_value=RAW) as s:
    AL.search_ads(P("525570110851332"))
    check("page_id web provider'ga uzatiladi", s.call_args.kwargs.get("page_id") == "525570110851332")

# 2. provider tanlash / fallback
with mock.patch.object(adlib_scraper, "search", side_effect=adlib_scraper.AdLibraryBlocked("captcha")), \
     mock.patch.object(meta_api, "_get", return_value={"data": [{"id": "m1", "page_name": "X", "ad_creative_bodies": ["t"],
                                                                 "publisher_platforms": ["facebook"], "ad_delivery_start_time": "2026-09-01T00:00:00+0000"}]}) as g:
    ads, prov = AL.search_ads(P("arboss"))
    check("web to'siq -> meta_api'ga o'tadi", prov == "meta_api" and ads[0].id == "m1" and ads[0].primary_text == "t")
    check("meta_api: search_terms", g.call_args.args[1].get("search_terms") == "arboss")
with mock.patch.object(adlib_scraper, "search", side_effect=adlib_scraper.AdLibraryBlocked("captcha")), \
     mock.patch.object(meta_api, "_get", side_effect=meta_api.MetaAPIError("perm")):
    try:
        AL.search_ads(P("arboss")); check("hammasi xato -> AdLibraryError", False)
    except AL.AdLibraryError as e:
        check("hammasi xato -> oxirgi (meta_api) xatosi", e.provider == "meta_api")
os.environ["AD_LIBRARY_PROVIDER"] = "meta_api"
with mock.patch.object(adlib_scraper, "search") as s, mock.patch.object(meta_api, "_get", return_value={"data": []}) as g:
    AL.search_ads(P("525570110851332"))
    check("AD_LIBRARY_PROVIDER=meta_api -> faqat rasmiy API, page_id -> search_page_ids",
          s.call_count == 0 and g.call_args.args[1].get("search_page_ids") == ["525570110851332"])
os.environ["AD_LIBRARY_PROVIDER"] = "nimadir"
try:
    AL.search_ads(P("x")); check("noma'lum provider -> xato", False)
except AL.ProviderNotConfigured:
    check("noma'lum provider -> ProviderNotConfigured", True)
os.environ.pop("AD_LIBRARY_PROVIDER")
os.environ.pop("ADLIB_SCRAPER")
with mock.patch.object(meta_api, "ACCESS_TOKEN", ""), mock.patch.object(meta_api, "AD_LIBRARY_TOKEN", ""):
    try:
        AL.search_ads(P("x")); check("hech narsa sozlanmagan -> xato", False)
    except AL.ProviderNotConfigured:
        check("hech narsa sozlanmagan -> ProviderNotConfigured", True)

# 4. Apify
apify = apify_mod.ApifyProvider()
check("Apify: token yo'q -> sozlanmagan", not apify.is_configured())
os.environ["APIFY_TOKEN"] = "apify_test_token"
node = {"ad_archive_id": "a1", "is_active": True, "page_id": "5", "page_name": "Arboss", "start_date": 1787295600,
        "publisher_platform": ["INSTAGRAM"], "snapshot": {"display_format": "IMAGE", "body": {"text": "Chegirma 20%"},
        "images": [{"resized_image_url": "https://i/a.jpg"}], "cta_text": "Learn more", "cards": [], "videos": []}}
resp = mock.Mock(status_code=200); resp.json.return_value = [node]
with mock.patch.object(requests, "post", return_value=resp) as post:
    out = apify.search(P("arboss"))
    sent = post.call_args
    check("Apify: Ad Library URL va token paramda (kodda emas)", "arboss" in sent.kwargs["json"]["urls"][0]["url"] and sent.kwargs["params"]["token"] == "apify_test_token")
    check("Apify: natija normalizatsiya", out and out[0].id == "a1" and out[0].media_type == "image" and out[0].primary_text == "Chegirma 20%")
for code, word in ((401, "token"), (402, "mablag")):
    r = mock.Mock(status_code=code)
    with mock.patch.object(requests, "post", return_value=r):
        try:
            apify.search(P("arboss")); check(f"Apify {code} -> xato", False)
        except AL.AdLibraryError as e:
            check(f"Apify {code} -> tushunarli xato", word in str(e))
os.environ.pop("APIFY_TOKEN")

# 6. Analyzer
import db  # noqa: E402
db.init_db()
ad = AL.Ad(id="an1", page_name="Arboss", primary_text="Bepul o'lchov! 20% chegirma", headline="Shkaf", cta_text="Send message",
           media_type="video", start_date="2026-09-01", platforms=["instagram"])
GOOD = json.dumps({"hook": "Bepul o'lchov", "offer": "20% chegirma", "cta": "Direct", "pain_point": "Joy yetishmasligi",
                   "creative_type": "Video", "lead_magnet": "Bepul o'lchov", "summary": "Kuchli", "idea": "Kafolat bering"})
llm = mock.Mock(return_value="Mana: " + GOOD)
r1 = analyzer.analyze_ad(ad, company_id=7, llm=llm)
check("AI: 6 maydon + xulosa", r1["analysis"]["hook"] == "Bepul o'lchov" and r1["analysis"]["lead_magnet"] == "Bepul o'lchov" and not r1["cached"])
check("AI prompt reklama ma'lumotini o'z ichiga oladi", "20% chegirma" in llm.call_args.args[1] and "Send message" in llm.call_args.args[1])
r2 = analyzer.analyze_ad(ad, company_id=7, llm=llm)
check("AI: kesh -- 2-chaqiruvda AI yo'q", r2["cached"] and llm.call_count == 1)
check("AI: kunlik hisob", analyzer.usage_today(7) == 1)
os.environ["AD_LIBRARY_AI_DAILY_LIMIT"] = "1"
try:
    analyzer.analyze_ad(AL.Ad(id="an2"), company_id=7, llm=llm); check("limit -> xato", False)
except analyzer.AnalyzeLimitReached:
    check("AI: kunlik limit", True)
os.environ.pop("AD_LIBRARY_AI_DAILY_LIMIT")
try:
    analyzer.analyze_ad(AL.Ad(id="an3"), company_id=8, llm=mock.Mock(return_value="kechirasiz")); check("buzuq javob -> xato", False)
except analyzer.AnalyzeError:
    check("AI: buzuq javob -> AnalyzeError, hisob oshmaydi", analyzer.usage_today(8) == 0)
try:
    analyzer.analyze_ad(AL.Ad(id="an4"), company_id=8, llm=mock.Mock(side_effect=RuntimeError("OPENAI_API_KEY sozlanmagan")))
except analyzer.AnalyzeError as e:
    check("AI: kalit yo'q -> tushunarli xato", "OPENAI_API_KEY" in str(e))

# 7. API va sahifa
import app as app_module  # noqa: E402

app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
s = db.get_session()
with db.unscoped():
    ca = db.Company(name="CAds A", is_active=True, plan="unlimited"); cb = db.Company(name="CAds B", is_active=True, plan="unlimited")
    s.add_all([ca, cb]); s.commit()
    for u, c in (("cads_a", ca.id), ("cads_b", cb.id)):
        m = db.Manager(username=u, full_name=u, role="admin", company_id=c, is_active=True); m.set_password("parol12345"); s.add(m)
    s.commit()
s.close()
A = app_module.app.test_client(); A.post("/login", data={"username": "cads_a", "password": "parol12345"})
B = app_module.app.test_client(); B.post("/login", data={"username": "cads_b", "password": "parol12345"})

page = A.get("/raqobatchilar/reklamalar")
html = page.get_data(as_text=True)
check("sahifa ochiladi va menyuda tab", page.status_code == 200 and "cadsForm" in html and "/raqobatchilar/reklamalar" in html)
check("sahifada i18n JSON (JS uchun)", '"ai_btn"' in html and "Hook" in html)
check("qisqa so'rov -> 400", A.get("/api/ad-library/search?q=a").status_code == 400)
os.environ["ADLIB_SCRAPER"] = "1"
with mock.patch.object(adlib_scraper, "search", return_value=RAW):
    r = A.get("/api/ad-library/search?q=texnomart&active=0")
    d = r.get_json()
    check("API qidiruv: ok, 3 reklama, provider=web", r.status_code == 200 and d["ok"] and len(d["ads"]) == 3 and d["provider"] == "web")
    check("API: Ad Library zaxira havolasi", d["ad_library_url"].startswith("https://www.facebook.com/ads/library/?"))
with mock.patch.object(adlib_scraper, "search", side_effect=adlib_scraper.AdLibraryBlocked("captcha")), \
     mock.patch.object(meta_api, "_get", side_effect=meta_api.MetaAPIError("perm")):
    r = A.get("/api/ad-library/search?q=texnomart")
    d = r.get_json()
    check("API xato: 502 + error + zaxira havola", r.status_code == 502 and not d["ok"] and d["error"] and d["ad_library_url"])
with mock.patch("orchestrator.call_light", return_value=GOOD) as cl:
    r = A.post("/api/ad-library/analyze", json={"ad_id": "1"})
    d = r.get_json()
    check("AI API: A o'z qidiruvidagi reklamani tahlil qiladi", r.status_code == 200 and d["ok"] and d["analysis"]["offer"] == "20% chegirma")
    r = B.post("/api/ad-library/analyze", json={"ad_id": "1"})
    check("AI API: B kompaniya A qidiruvidagi reklamani tahlil qila olmaydi (404)", r.status_code == 404)
    r = A.post("/api/ad-library/analyze", json={"ad_id": "yoq"})
    check("AI API: noma'lum reklama -> 404", r.status_code == 404)
os.environ.pop("ADLIB_SCRAPER")
anon = app_module.app.test_client()
check("kirmagan foydalanuvchi API'ga kira olmaydi", anon.get("/api/ad-library/search?q=texnomart").status_code in (302, 401))

print()
if FAILED:
    print(f"{len(FAILED)} ta test YIQILDI: {FAILED}")
    sys.exit(1)
print("Hammasi o'tdi.")
