"""test_carousel_ad_offline.py -- PLAN 4-bosqich oxirgi bandi: Kreativ
studiya karuselini Avtopilot orqali Meta KARUSEL reklamasi sifatida chiqarish.
  1. Qoralama sxemasi `ad.media.carousel`ni qabul qiladi (begona kalitlar
     tashlanadi, rasmsiz karta rad etiladi).
  2. Kreativ spec: `link_data.child_attachments` (har karta rasm/sarlavha/CTA).
  3. Tekshiruv: 1 ta karta -- xato, 2-10 -- yaxshi.
  4. Nashr: yuklanmagan kartalar Meta'ga yuklanadi; "Darhol yoqilsin"
     tanlangan bo'lsa ham hammasi PAUSED (egasi qarori).
  5. Karusel sahifasida tugma; Avtopilot ustasida karusel ko'rinadi.

    cd app && python3 scripts/test_carousel_ad_offline.py
"""
import json
import os
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("REPLIX_TEST_DEFAULT_UNSCOPED", "1")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-dummy-token")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-carousel-ad")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(tempfile.mkdtemp(), 'carad.db')}"

import campaign_draft as cd  # noqa: E402
import campaign_media  # noqa: E402
import db as db_module  # noqa: E402
import meta_api  # noqa: E402
import meta_publish  # noqa: E402

db_module.init_db()
failures = []


def check(name, cond, detail=""):
    print(("OK:   " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


ASSETS = {
    "ad_account": {"id": "act_1", "name": "Acc", "currency": "UZS", "timezone_name": "Asia/Tashkent"},
    "pages": [{"id": "PAGE1", "name": "Nur", "instagram_business_account": {"id": "IG1"}}],
    "instagram_accounts": [{"id": "IG1"}], "pixels": [{"id": "PX1"}], "custom_audiences": [],
    "lead_forms": [], "recent_images": [], "campaigns": [], "errors": [],
    "pixel_id": "PX1", "page_id": "PAGE1", "ig_business_id": "IG1",
}


def _state(cards):
    s = cd.new_empty_state("TRAFFIC")
    s["campaign"]["name"] = "Replix | karusel"
    s["adset"]["name"] = "Toshkent"
    s["adset"]["daily_budget"] = 200000
    s["adset"]["duration_days"] = 7
    s["adset"]["start_time"] = "2026-10-05T09:00:00"
    s["adset"]["end_time"] = "2026-10-12T09:00:00"
    s["adset"]["targeting"]["age_min"] = 25
    s["adset"]["targeting"]["age_max"] = 45
    s["adset"]["targeting"]["geo_locations"]["cities"] = [{"key": "2430536", "name": "Tashkent", "radius": 0, "distance_unit": "kilometer"}]
    s["ad"]["name"] = "Karusel reklama"
    s["ad"]["page_id"] = "PAGE1"
    s["ad"]["instagram_actor_id"] = "IG1"
    s["ad"]["primary_text"] = "Yangi kolleksiya"
    s["ad"]["headline"] = "Nur Mebel"
    s["ad"]["link_url"] = "https://nur.uz"
    s["ad"]["media"]["image_hash"] = cards[0].get("image_hash") or "HASH_FIRST"
    s["ad"]["media"]["carousel"] = cards
    return s


def test_schema_and_spec():
    s = cd.new_empty_state("TRAFFIC")
    new, _, _ = cd.apply_patch(s, {"scope": "ad", "changes": {"ad.media": {
        "media_id": 1, "image_hash": "H1", "video_id": None, "selected_variant": None,
        "carousel": [{"media_id": 1, "image_hash": "H1", "headline": "Birinchi karta juda uzun sarlavha bilan, 40 belgidan oshadi", "evil": "x"},
                     {"media_id": 2, "headline": "Ikkinchi"}],
    }}}, source="USER_OVERRIDDEN", field_sources={})
    car = new["ad"]["media"]["carousel"]
    check("sxema: karusel saqlandi", len(car) == 2 and car[0]["image_hash"] == "H1")
    check("sxema: begona kalit tashlandi", "evil" not in car[0])
    check("sxema: sarlavha 40 belgigacha", len(car[0]["headline"]) <= 40)
    try:
        cd.apply_patch(s, {"scope": "ad", "changes": {"ad.media": {"carousel": [{"headline": "rasmsiz"}]}}}, source="USER_OVERRIDDEN", field_sources={})
        check("sxema: rasmsiz karta rad etiladi", False)
    except cd.DraftPatchError:
        check("sxema: rasmsiz karta rad etiladi", True)

    st = _state([{"media_id": 1, "image_hash": "H1", "headline": "A"}, {"media_id": 2, "image_hash": "H2", "headline": "B"}, {"media_id": 3, "image_hash": "H3", "headline": "C"}])
    spec = cd.to_meta_creative_spec(st, page_id="PAGE1", instagram_actor_id="IG1", image_hash="H1")
    ld = spec.get("link_data") or {}
    kids = ld.get("child_attachments") or []
    check("spec: link_data.child_attachments 3 ta", len(kids) == 3, json.dumps(spec)[:300])
    check("spec: har karta o'z rasmi va sarlavhasi", [k["image_hash"] for k in kids] == ["H1", "H2", "H3"] and kids[1]["name"] == "B")
    check("spec: har kartada havola va CTA", all(k.get("link") == "https://nur.uz" and k.get("call_to_action") for k in kids))
    check("spec: umumiy matn", ld.get("message") == "Yangi kolleksiya" and "image_hash" not in ld)

    one = _state([{"media_id": 1, "image_hash": "H1"}])
    errs = [e["message"] for e in cd.validate_state(one, company=None, meta_assets=ASSETS)]
    check("tekshiruv: 1 karta -- xato", any("Karusel" in e for e in errs), str(errs))
    errs = [e["message"] for e in cd.validate_state(st, company=None, meta_assets=ASSETS)]
    check("tekshiruv: 3 karta -- karusel xatosi yo'q", not any("Karusel" in e for e in errs), str(errs))


class FakeMeta:
    def __init__(self):
        self.calls = []

    def create_campaign(self, name, objective, status, cats, *, access_token, ad_account_id):
        self.calls.append(("campaign", status)); return {"id": "C1"}

    def create_adset(self, campaign_id, name, daily, targeting, opt, billing, bid, status, promoted, **kw):
        self.calls.append(("adset", status)); return {"id": "AS1"}

    def create_ad_creative(self, ad_account_id, name, spec, *, access_token):
        self.calls.append(("creative", spec)); return {"id": "CR1"}

    def create_ad(self, adset_id, name, creative_id, status, *, access_token, ad_account_id):
        self.calls.append(("ad", status)); return {"id": "AD1"}

    def basic(self, oid, *, access_token):
        return {"id": oid, "name": "x", "status": "PAUSED"}


def test_publish_carousel():
    session = db_module.get_session()
    try:
        c = db_module.Company(name="Nur", plan="business", is_active=True, meta_ad_account_id="act_1", meta_page_id="PAGE1", ig_business_id="IG1", meta_pixel_id="PX1")
        c.set_meta_access_token("tok_A")
        session.add(c)
        session.commit()
        d = db_module.CampaignDraft(company_id=c.id, title="K", objective="TRAFFIC", campaign_approved=True, adset_approved=True, ad_approved=True)
        session.add(d)
        session.commit()
        rows = []
        for i in range(3):
            r = db_module.CampaignDraftMedia(company_id=c.id, draft_id=d.id, kind="image", filename=f"k{i}.png", storage_path=f"x/{i}.png",
                                             meta_image_hash=("H0" if i == 0 else None), upload_status=("uploaded" if i == 0 else "pending"))
            session.add(r)
            rows.append(r)
        session.commit()
        cards = [{"media_id": r.id, "image_hash": r.meta_image_hash, "headline": f"Karta {i + 1}"} for i, r in enumerate(rows)]
        st = _state(cards)
        st["ad"]["media"]["media_id"] = rows[0].id
        st["ad"]["media"]["image_hash"] = "H0"
        d.set_state(st)
        d.launch_active = True  # egasi "Darhol yoqilsin" tanlagan bo'lsa ham
        session.commit()

        def fake_upload(sess, row, company):
            row.meta_image_hash = f"UP{row.id}"
            row.upload_status = "uploaded"
            sess.commit()
            return row

        fm = FakeMeta()
        with mock.patch.object(meta_api, "create_campaign", fm.create_campaign), \
             mock.patch.object(meta_api, "create_adset", fm.create_adset), \
             mock.patch.object(meta_api, "create_ad_creative", fm.create_ad_creative), \
             mock.patch.object(meta_api, "create_ad", fm.create_ad), \
             mock.patch.object(meta_api, "get_campaign_basic", fm.basic), \
             mock.patch.object(meta_api, "get_adset_basic", fm.basic), \
             mock.patch.object(meta_api, "get_ad_basic", fm.basic), \
             mock.patch.object(campaign_media, "ensure_uploaded_to_meta", side_effect=fake_upload) as up, \
             mock.patch.object(meta_publish, "get_meta_assets", lambda company, use_cache=True: json.loads(json.dumps(ASSETS))):
            meta_publish.publish_draft(session, d, c, manager_id=None)
        statuses = [s for k, s in fm.calls if k in ("campaign", "adset", "ad")]
        check("nashr: hammasi PAUSED (Darhol yoqilsin tanlangan bo'lsa ham)", statuses == ["PAUSED", "PAUSED", "PAUSED"], str(statuses))
        check("nashr: yuklanmagan 2 karta Meta'ga yuklandi", up.call_count == 2, str(up.call_count))
        spec = next(s for k, s in fm.calls if k == "creative")
        hashes = [k["image_hash"] for k in spec["link_data"]["child_attachments"]]
        check("nashr: kreativda 3 karta, yangi hash'lar bilan", hashes == ["H0", f"UP{rows[1].id}", f"UP{rows[2].id}"], str(hashes))
        check("nashr: qoralama 'published'", d.status == "published", d.status)
        saved = d.get_state()["ad"]["media"]["carousel"]
        check("nashr: hash'lar qoralamada saqlandi (qayta urinishda qayta yuklanmaydi)", all(c_.get("image_hash") for c_ in saved))
    finally:
        session.close()


def test_pages():
    import app as app_module
    app_module.app.config["TESTING"] = True
    app_module.app.config["WTF_CSRF_ENABLED"] = False
    s = db_module.get_session()
    try:
        c = db_module.Company(name="Karusel Co", plan="business", is_active=True)
        s.add(c)
        s.commit()
        m = db_module.Manager(username="car_admin", role="admin", company_id=c.id)
        m.set_password("parol12345")
        s.add(m)
        s.commit()
        assets = []
        for i in range(3):
            a = db_module.CreativeAsset(company_id=c.id, kind="template", status="ready", aspect="1:1", title=f"K — karusel {i + 1}/3",
                                        final_storage_path=f"x/{i}.png")
            a.set_brief_answers({"carousel_group": "abc123abc123", "carousel_index": i + 1, "carousel_total": 3, "carousel_style": "gradient_bold"})
            s.add(a)
            assets.append(a)
        s.commit()
    finally:
        s.close()
    cl = app_module.app.test_client()
    cl.post("/login", data={"username": "car_admin", "password": "parol12345"})
    body = cl.get("/kreativ/karusel/abc123abc123").get_data(as_text=True)
    check("karusel sahifasida 'Avtopilot orqali reklama' tugmasi", "carousel_group=abc123abc123" in body)
    r = cl.get("/avtopilot/yangi?carousel_group=abc123abc123")
    body = r.get_data(as_text=True)
    check("Avtopilot ustasi karuselni ko'rsatadi", r.status_code == 200 and 'name="carousel_group"' in body and "3 karta" in body, str(r.status_code))
    # Ustaning biriktirish funksiyasi: har karta media qatori + ad.media.carousel.
    s = db_module.get_session()
    try:
        comp = s.query(db_module.Company).filter_by(name="Karusel Co").one()
        draft = db_module.CampaignDraft(company_id=comp.id, title="K", objective="TRAFFIC")
        draft.set_state(cd.new_empty_state("TRAFFIC"))
        s.add(draft)
        s.commit()
        rows = s.query(db_module.CreativeAsset).filter_by(company_id=comp.id).order_by(db_module.CreativeAsset.id).all()
        counter = {"n": 0}

        def fake_media(sess, dr, asset, manager_id):
            counter["n"] += 1
            r_ = db_module.CampaignDraftMedia(company_id=dr.company_id, draft_id=dr.id, kind="image", filename=f"c{asset.id}.png",
                                              storage_path="x", meta_image_hash=f"HH{asset.id}", upload_status="uploaded")
            sess.add(r_)
            sess.commit()
            return r_

        import autopilot_web
        with app_module.app.test_request_context(), \
             mock.patch.object(autopilot_web, "media_from_creative_asset", side_effect=fake_media), \
             mock.patch.object(autopilot_web, "try_upload_to_meta", return_value=None), \
             mock.patch.object(app_module, "_autopilot_manager_id", return_value=None):
            err = app_module._autopilot_attach_carousel(s, draft, rows, comp, ASSETS)
        s.refresh(draft)
        car = draft.get_state()["ad"]["media"].get("carousel") or []
        check("biriktirish: xatosiz", err is None, str(err))
        check("biriktirish: 3 karta ad.media.carousel'da", len(car) == 3 and all(c_["image_hash"] for c_ in car), str(car))
        check("biriktirish: asosiy rasm = 1-karta", draft.get_state()["ad"]["media"]["image_hash"] == car[0]["image_hash"])
    finally:
        s.close()
    r = cl.get("/avtopilot/yangi?carousel_group=zzzzzzzzzzzz")
    check("begona/noto'g'ri guruh e'tiborsiz", r.status_code == 200 and 'name="carousel_group"' not in r.get_data(as_text=True))


test_schema_and_spec()
test_publish_carousel()
test_pages()
if failures:
    print(f"\n{len(failures)} ta XATO")
    sys.exit(1)
print("\nBARCHA TESTLAR O'TDI")
