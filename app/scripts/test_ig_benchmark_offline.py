"""test_ig_benchmark_offline.py -- Instagram raqobatchi statistikasi
(ig_benchmark.py + /raqobatchilar/instagram), tarmoqsiz:
  1. Username tahlili (@, havola, takror, noto'g'ri belgilar).
  2. O'rtacha like/komment/ko'rish (faqat video), ER, post/hafta, eng yaxshi.
  3. view_count qo'llab-quvvatlanmasa -- ko'rishlarsiz qayta so'raladi.
  4. Topilmagan akkaunt -- tushunarli xato, sahifa yiqilmaydi.
  5. Kesh: ikkinchi so'rov Meta'ga bormaydi; refresh=1 -- boradi.
  6. Meta ulanmagan kompaniya -- "avval ulang" xabari; boshqa kompaniya
     tokeni ishlatilmaydi (so'rov faqat O'Z IG akkaunti nomidan).

    cd app && python3 scripts/test_ig_benchmark_offline.py
"""
import datetime as dt
import os
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("REPLIX_TEST_DEFAULT_UNSCOPED", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-dummy-token")
os.environ["FLASK_SECRET_KEY"] = "test-secret-igb"
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(tempfile.mkdtemp(), 'igb.db')}"

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402
import ig_benchmark  # noqa: E402
import meta_api  # noqa: E402

db_module.init_db()
app_module.app.config["TESTING"] = True
app_module.app.config["WTF_CSRF_ENABLED"] = False
failures = []


def check(name, cond, detail=""):
    print(("OK:   " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def _media(i, likes, comments, kind="IMAGE", product="FEED", views=None, days_ago=0):
    m = {"id": f"m{i}", "caption": f"Post {i}", "like_count": likes, "comments_count": comments,
         "media_type": kind, "media_product_type": product, "permalink": f"https://instagram.com/p/{i}",
         "timestamp": (dt.datetime(2026, 9, 30) - dt.timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S+0000")}
    if views is not None:
        m["view_count"] = views
    return m


PROFILES = {
    "dunyabunya": {"username": "dunyabunya", "name": "Dunyabunya", "followers_count": 10000, "media_count": 300,
                   "media": {"data": [_media(1, 100, 10, days_ago=0), _media(2, 200, 30, "VIDEO", "REELS", views=5000, days_ago=7),
                                      _media(3, 300, 20, days_ago=14)]}},
    "buxoro_maktabi": {"username": "buxoro_maktabi", "name": "Buxoro Maktabi", "followers_count": 2000, "media_count": 50,
                       "media": {"data": [_media(4, 50, 5, days_ago=0), _media(5, 70, 15, days_ago=3)]}},
}
calls = []


def fake_bd(ig_user_id, username, limit=30, *, page_id=None, access_token=None, with_views=True):
    calls.append({"ig": ig_user_id, "u": username, "token": access_token, "views": with_views})
    if username not in PROFILES:
        raise meta_api.MetaAPIError({"message": "Invalid user id", "code": 110})
    return PROFILES[username]


def test_parse():
    check("parse: @, havola, takror", ig_benchmark.parse_usernames("@Dunyabunya, https://www.instagram.com/buxoro_maktabi/ dunyabunya") == ["dunyabunya", "buxoro_maktabi"])
    check("parse: noto'g'ri belgili tashlanadi", ig_benchmark.parse_usernames("ab!c x<y>") == [])
    check("parse: ko'pi bilan 5", len(ig_benchmark.parse_usernames("a b c d e f g")) == 5)
    check("limit 12..30", ig_benchmark.clamp_limit(5) == 12 and ig_benchmark.clamp_limit(99) == 30 and ig_benchmark.clamp_limit("x") == 20)


def test_stats():
    s = ig_benchmark.compute_stats(PROFILES["dunyabunya"], 20)
    check("o'rtacha like", s["avg_likes"] == 200.0, str(s["avg_likes"]))
    check("o'rtacha komment", s["avg_comments"] == 20.0, str(s["avg_comments"]))
    check("o'rtacha ko'rish faqat videodan", s["avg_views"] == 5000.0 and s["video_count"] == 1, str(s["avg_views"]))
    check("ER = (like+komment)/obunachi", s["engagement_rate"] == 2.2, str(s["engagement_rate"]))
    check("post/hafta", s["posts_per_week"] == 1.5, str(s["posts_per_week"]))
    check("eng faol post birinchi", s["top_posts"][0]["likes"] == 300)
    check("kontent turi", s["kinds"] == {"Rasm": 2, "Reels": 1}, str(s["kinds"]))
    hidden = ig_benchmark.compute_stats({"followers_count": 10, "media": {"data": [{"comments_count": 2}]}}, 12)
    check("like yashirilgan -- belgilanadi, yiqilmaydi", hidden["likes_hidden"] and hidden["avg_likes"] is None)
    empty = ig_benchmark.compute_stats({"username": "x"}, 12)
    check("postsiz akkaunt -- yiqilmaydi", empty["posts_analyzed"] == 0 and empty["engagement_rate"] is None)


def _seed():
    s = db_module.get_session()
    try:
        with db_module.unscoped():
            a = s.get(db_module.Company, db_module.get_default_company_id())
            a.meta_page_id, a.ig_business_id = "page_A", "ig_A"
            a.set_meta_access_token("TOKEN_A")
            a.plan, a.is_active = "unlimited", True
            b = db_module.Company(name="B", plan="unlimited", is_active=True)
            s.add(b)
            s.commit()
            for comp, uname in ((a, "a_admin"), (b, "b_admin")):
                m = db_module.Manager(username=uname, role="admin", company_id=comp.id)
                m.set_password("parol12345")
                s.add(m)
            s.commit()
    finally:
        s.close()


def test_page():
    _seed()
    store = {}
    with mock.patch.object(meta_api, "get_instagram_business_discovery", side_effect=fake_bd), \
         mock.patch.object(ig_benchmark.kv_store, "get_json", side_effect=lambda k, default=None: store.get(k, default)), \
         mock.patch.object(ig_benchmark.kv_store, "set_json", side_effect=lambda k, v: store.__setitem__(k, v)):
        c = app_module.app.test_client()
        c.post("/login", data={"username": "a_admin", "password": "parol12345"})
        r = c.get("/raqobatchilar/instagram")
        check("bo'sh sahifa 200", r.status_code == 200, str(r.status_code))
        r = c.get("/raqobatchilar/instagram?u=dunyabunya,buxoro_maktabi,yoq_akkaunt&n=20")
        body = r.get_data(as_text=True)
        check("tahlil sahifasi 200", r.status_code == 200, str(r.status_code))
        check("solishtirish jadvali bor", "@dunyabunya" in body and "@buxoro_maktabi" in body)
        check("o'rtacha like ko'rsatildi", "200" in body)
        check("Ad Library havolasi", "facebook.com/ads/library" in body)
        check("topilmagan akkaunt -- tushunarli xato", "yoq_akkaunt topilmadi" in body, body[body.find("⚠️"):body.find("⚠️") + 200])
        check("so'rov O'Z IG akkaunti va tokeni bilan", all(x["ig"] == "ig_A" and x["token"] == "TOKEN_A" for x in calls), str(calls))
        n = len(calls)
        c.get("/raqobatchilar/instagram?u=dunyabunya&n=20")
        check("kesh: qayta so'rov Meta'ga bormaydi", len(calls) == n, f"{n} -> {len(calls)}")
        c.get("/raqobatchilar/instagram?u=dunyabunya&n=20&refresh=1")
        check("refresh=1 -- qayta so'raladi", len(calls) == n + 1)
        c.get("/logout")

        calls.clear()
        c.post("/login", data={"username": "b_admin", "password": "parol12345"})
        r = c.get("/raqobatchilar/instagram?u=dunyabunya")
        body = r.get_data(as_text=True)
        check("Meta ulanmagan kompaniya -- 'avval ulang'", r.status_code == 200 and "Hisoblarni ulash" in body)
        check("B uchun A tokeni ishlatilmadi (so'rov yo'q)", calls == [], str(calls))
        check("B A'ning keshini ko'rmaydi", "Dunyabunya</strong>" not in body)


def test_view_fallback():
    seq = []

    def bd(ig, u, limit=30, *, page_id=None, access_token=None, with_views=True):
        seq.append(with_views)
        if with_views:
            raise meta_api.MetaAPIError({"message": "(#100) Tried accessing nonexisting field (view_count)"})
        return PROFILES["dunyabunya"]

    company = mock.Mock(id=999, meta_page_id="p", ig_business_id="ig", get_meta_access_token=lambda: "T")
    with mock.patch.object(meta_api, "get_instagram_business_discovery", side_effect=bd), \
         mock.patch.object(ig_benchmark.kv_store, "get_json", return_value=None), \
         mock.patch.object(ig_benchmark.kv_store, "set_json"):
        s = ig_benchmark.fetch_stats(company, "dunyabunya", 20)
    check("view_count yo'q -- ko'rishlarsiz qayta so'raldi", seq == [True, False] and s["avg_likes"] == 200.0, str(seq))


test_parse()
test_stats()
test_view_fallback()
test_page()
if failures:
    print(f"\n{len(failures)} ta XATO")
    sys.exit(1)
print("\nBARCHA TESTLAR O'TDI")
