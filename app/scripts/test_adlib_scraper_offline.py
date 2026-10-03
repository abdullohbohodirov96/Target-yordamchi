"""test_adlib_scraper_offline.py — Ad Library veb-sahifasidan o'qish
(`adlib_scraper.py`) va uning ulanishi, TARMOQSIZ va BRAUZERSIZ:

  1. `parse_results`: sahifa JSON'idan VIDEO/IMAGE/CAROUSEL/DCO reklamalar,
     matn (body.text / karta), video, preview rasm, sana, faol/to'xtagan,
     takrorlar olib tashlanadi; captcha -> AdLibraryBlocked.
  2. `search`: kesh (ikkinchi chaqiruv brauzer ochmaydi), xato -> Blocked.
  3. `is_enabled` standart O'CHIQ.
  4. `meta_api.search_ad_library`: yoqilganda scraper ishlatiladi, to'siqda
     rasmiy API'ga qaytadi, o'chiq bo'lsa scraper umuman chaqirilmaydi.
  5. Raqobatchilar sahifasi: video kartochka (<video poster>), "N kun",
     live-search scraper yoqilganda brauzer ochmaydi.
"""
import datetime as dt
import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(tempfile.mkdtemp(), 'adlib.db')}"
os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy")
os.environ.setdefault("META_ACCESS_TOKEN", "test-dummy-token")
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_test_dummy")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ.pop("ADLIB_SCRAPER", None)

import adlib_scraper as S  # noqa: E402

FAILED = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILED.append(name)


START = int(dt.datetime(2026, 8, 21, 7).timestamp())


def node(ad_id, fmt, active=True, **snap):
    base = {"page_name": "Texnomart", "page_id": "525", "display_format": fmt, "body": None, "title": None,
            "cards": [], "videos": [], "images": [], "cta_text": "Shop now", "link_url": "https://texnomart.uz",
            "page_profile_picture_url": "https://cdn/p.jpg", "page_like_count": 58913}
    base.update(snap)
    return {"ad_archive_id": ad_id, "is_active": active, "page_id": "525", "page_name": "Texnomart",
            "start_date": START, "end_date": START + 86400 * 5, "publisher_platform": ["FACEBOOK", "INSTAGRAM"],
            "collation_count": 2, "snapshot": base}


payload = {"count": 4, "edges": [
    {"node": {"collated_results": [
        node("1", "VIDEO", videos=[{"video_sd_url": "https://v/sd.mp4", "video_hd_url": "https://v/hd.mp4", "video_preview_image_url": "https://v/prev.jpg"}]),
        node("2", "IMAGE", body={"text": "Muddatli to'lov 0%! 1 290 000 so'm"}, images=[{"resized_image_url": "https://i/r.jpg"}]),
    ]}},
    {"node": {"collated_results": [
        node("3", "CAROUSEL", active=False, body={"text": "{{product.name}}"},
             cards=[{"body": "Karta matni", "title": "Karta", "resized_image_url": "https://c/1.jpg"}]),
        node("1", "VIDEO"),  # takror
    ]}},
]}
html = '<html><script>{"x":1,"ad_library_main":{"search_results_connection":' + json.dumps(payload) + '}}</script></html>'
ads = S.parse_results(html)
by = {a["id"]: a for a in ads}
check("3 ta noyob reklama (takror olib tashlandi)", len(ads) == 3)
check("VIDEO: video_url (sd) va preview", by["1"]["video_url"] == "https://v/sd.mp4" and by["1"]["preview_image_url"] == "https://v/prev.jpg")
check("IMAGE: matn body.text dan", by["2"]["ad_creative_bodies"] == ["Muddatli to'lov 0%! 1 290 000 so'm"])
check("IMAGE: preview rasm", by["2"]["preview_image_url"] == "https://i/r.jpg" and by["2"]["video_url"] is None)
check("CAROUSEL: {{placeholder}} tashlanib, karta matni olinadi", by["3"]["ad_creative_bodies"] == ["Karta matni"])
check("CAROUSEL: karta rasmi preview", by["3"]["preview_image_url"] == "https://c/1.jpg")
check("sana ISO formatda", by["1"]["ad_delivery_start_time"].startswith("2026-08-21"))
check("faol -> stop_time None; to'xtagan -> stop_time bor", by["1"]["ad_delivery_stop_time"] is None and by["3"]["ad_delivery_stop_time"])
check("snapshot_url Ad Library ?id=", by["2"]["ad_snapshot_url"] == "https://www.facebook.com/ads/library/?id=2")
check("sahifa rasmi va obunachi", by["1"]["page_picture_url"] == "https://cdn/p.jpg" and by["1"]["page_like_count"] == 58913)
try:
    S.parse_results('{"xfb_ad_library_is_captcha_required":true}')
    check("captcha -> AdLibraryBlocked", False)
except S.AdLibraryBlocked:
    check("captcha -> AdLibraryBlocked", True)
check("natijasiz sahifa -> bo'sh ro'yxat", S.parse_results("<html>No ads</html>") == [])

# 2. kesh / xato
check("standart holatda o'chiq", S.is_enabled() is False)
S._cache.clear()
with mock.patch.object(S, "_fetch", return_value=ads) as f:
    r1 = S.search("Texnomart")
    r2 = S.search("texnomart ")
check("kesh: ikkinchi qidiruv brauzer ochmaydi", f.call_count == 1 and r1 == r2 == ads)
S._cache.clear()
with mock.patch.object(S, "_fetch", side_effect=RuntimeError("Timeout 40000ms")):
    try:
        S.search("x")
        check("brauzer xatosi -> AdLibraryBlocked", False)
    except S.AdLibraryBlocked:
        check("brauzer xatosi -> AdLibraryBlocked", True)
check("lock bo'shatildi", S._lock.acquire(blocking=False) and (S._lock.release() or True))

# 4. meta_api ulanishi
import meta_api  # noqa: E402

with mock.patch.object(S, "search", return_value=ads) as sc, mock.patch.object(meta_api, "_get") as g:
    meta_api.search_ad_library("texnomart")
    check("o'chiq -> scraper chaqirilmaydi, rasmiy API", sc.call_count == 0 and g.call_count == 1)
os.environ["ADLIB_SCRAPER"] = "1"
with mock.patch.object(S, "search", return_value=ads) as sc, mock.patch.object(meta_api, "_get") as g:
    res = meta_api.search_ad_library("texnomart")
    check("yoqilgan -> scraper natijasi, rasmiy API chaqirilmaydi", res == ads and g.call_count == 0)
with mock.patch.object(S, "search", side_effect=S.AdLibraryBlocked("captcha")), \
     mock.patch.object(meta_api, "_get", return_value={"data": [{"id": "api1"}]}) as g:
    res = meta_api.search_ad_library("texnomart")
    check("to'siq -> rasmiy API'ga qaytadi", res == [{"id": "api1"}] and g.call_count == 1)

# 5. Sahifa
import app as app_module  # noqa: E402
import db  # noqa: E402

app_module.app.config["TESTING"] = True
app_module.app.config["WTF_CSRF_ENABLED"] = False
db.init_db()
s = db.get_session()
with db.unscoped():
    co = db.Company(name="AdLib Co", is_active=True, plan="unlimited")
    s.add(co)
    s.commit()
    m = db.Manager(username="adlib_admin", full_name="A", role="admin", company_id=co.id, is_active=True)
    m.set_password("parol12345")
    s.add(m)
    s.commit()
s.close()
client = app_module.app.test_client()
client.post("/login", data={"username": "adlib_admin", "password": "parol12345"})
with mock.patch.object(S, "search", return_value=ads), mock.patch.object(meta_api, "get_page_public_profile", return_value={}) as prof:
    page = client.get("/settings/competitors?q=texnomart").get_data(as_text=True)
check("video kartochka: <video poster=preview src=video>", 'poster="https://v/prev.jpg"' in page and 'src="https://v/sd.mp4"' in page)
check("rasm kartochka", 'src="https://i/r.jpg"' in page)
check("'N kun' yozuvi (qachondan beri)", "21.08.2026 dan beri" in page)
check("sahifa rasmi scraper'dan -> profil API chaqirilmaydi", prof.call_count == 0 and "https://cdn/p.jpg" in page)
with mock.patch.object(S, "search", side_effect=AssertionError("brauzer ochildi")) as sc:
    r = client.get("/settings/competitors/live-search?q=texno")
check("live-search scraper yoqilganda brauzer ochmaydi", r.get_json() == {"results": []} and sc.call_count == 0)
os.environ.pop("ADLIB_SCRAPER", None)

print()
if FAILED:
    print(f"{len(FAILED)} ta test YIQILDI: {FAILED}")
    sys.exit(1)
print("Hammasi o'tdi.")
