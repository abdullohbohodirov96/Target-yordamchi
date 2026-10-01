"""test_creative_carousel_offline.py -- docs/PLAN.md, 3-bosqich (Kreativ
studiya): karusel postlar (6 uslub) va 9:16 xavfsiz zonasi. Tarmoqsiz,
vaqtinchalik SQLite; OpenAI ATAYLAB o'chirilgan (chaqirilsa -- test yiqiladi).
  1. Kartalar rejasi: ilgak -> afzalliklar -> CTA, 3..6 chegarasi.
  2. 6 ta uslubning har biri 1:1, 4:5, 9:16 da xatosiz render bo'ladi;
     9:16 da matn/logo/badge xavfsiz zonada (0.12..0.82).
  3. Manbasiz va tayyor kreativdan karusel -- OpenAI'siz, kvota o'zgarmaydi.
  4. HTTP: yaratish -> karusel sahifasi -> ZIP; B kompaniya A karuselini
     ko'ra olmaydi (404); ro'yxatda "Karusel i/n" belgisi.
  5. Galereyada 6 ta uslub real namuna rasmi bilan ko'rinadi.

Ishga tushirish:
    cd app && python3 scripts/test_creative_carousel_offline.py
"""

import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("REPLIX_TEST_DEFAULT_UNSCOPED", "1")  # test skripti bazani to'g'ridan-to'g'ri tayyorlaydi (db.py, fail-closed rejim)
os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("OPENAI_API_KEY", "test-dummy-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-dummy-token")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
_TMPDIR = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMPDIR, 'carousel.db')}"

import requests  # noqa: E402

_network_calls = []


def _offline(*a, **k):
    _network_calls.append(a[0] if a else k.get("url"))
    raise requests.ConnectionError("offline test")


for _name in ("get", "post", "put", "delete", "request"):
    setattr(requests, _name, _offline)
requests.Session.request = _offline  # type: ignore[assignment]

import app as app_module  # noqa: E402
import creative_carousel  # noqa: E402
import creative_studio  # noqa: E402
import db as db_module  # noqa: E402

app_module.app.config["TESTING"] = True
app_module.app.config["WTF_CSRF_ENABLED"] = False
creative_studio.CREATIVE_ROOT = Path(_TMPDIR) / "creatives"
creative_studio.BRAND_ROOT = Path(_TMPDIR) / "brand"
db_module.init_db()

PW = "parol12345"
failures = []


def check(name, cond, detail=""):
    print(("OK:   " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


SAMPLE = {"headline": "Qishki etiklar", "subheadline": "Charm, issiq", "cta_text": "Hoziroq yozing",
          "price_text": "399 000 so'm", "offer_text": "-30%", "phone_line": "Tel: +998 90 123 45 67",
          "features": ["Haqiqiy charm", "-30°C ga chidamli", "1 yil kafolat", "Bepul yetkazish"]}


def test_plan_cards():
    cards = creative_carousel.plan_cards(SAMPLE, 4)
    check("4 karta: ilgak + 2 afzallik + CTA", [c["kind"] for c in cards] == ["hook", "feature", "feature", "cta"], str(cards))
    check("6 karta chegarasi", len(creative_carousel.plan_cards(SAMPLE, 99)) == 6)
    check("kamida 3 karta", len(creative_carousel.plan_cards(SAMPLE, 1)) == 3)
    bare = creative_carousel.plan_cards({"headline": "X"}, 5)
    check("afzallik yo'q -- 3 karta (bo'sh karta yo'q)", len(bare) == 3 and bare[1]["text"] == "X", str(bare))


def test_all_styles_render_and_story_safe_zone():
    from PIL import Image
    base = Path(_TMPDIR) / "base.png"
    Image.new("RGB", (1024, 1536), (90, 110, 150)).save(base)
    check("6 ta uslub", len(creative_carousel.CAROUSEL_STYLES) == 6 and len({s["key"] for s in creative_carousel.CAROUSEL_STYLES}) == 6)
    for style in creative_carousel.CAROUSEL_STYLES:
        cards = creative_carousel.plan_cards(SAMPLE, 5)
        ok_render, ok_zone = True, True
        for aspect in ("1:1", "4:5", "9:16"):
            size = creative_studio._target_pixels_for_aspect(aspect)
            for i, card in enumerate(cards, start=1):
                layers = creative_carousel.card_layers(style, card, SAMPLE, i, len(cards), style["accents"], aspect)
                out = Path(_TMPDIR) / f"{style['key']}_{aspect.replace(':', 'x')}_{i}.png"
                try:
                    creative_studio.render_composite(base, layers, None, out, target_size=size)
                    with Image.open(out) as im:
                        ok_render &= im.size == size
                except Exception as e:  # noqa: BLE001
                    ok_render = False
                    print("   render xato:", style["key"], aspect, e)
                if aspect == "9:16":
                    for l in layers:
                        if l.get("type") != "panel" and not l.get("hidden"):
                            if l["y"] < creative_studio.STORY_SAFE_TOP - 0.001 or l["y"] + l["h"] > creative_studio.STORY_SAFE_BOTTOM + 0.001:
                                ok_zone = False
                                print("   zona tashqarisida:", style["key"], l["id"], l["y"], l["h"])
        check(f"uslub '{style['key']}': 1:1/4:5/9:16 xatosiz render", ok_render)
        check(f"uslub '{style['key']}': 9:16 da matn/logo xavfsiz zonada", ok_zone)


def test_adapt_layers_keeps_other_aspects():
    layers = [{"id": "h", "type": "text", "x": 0.1, "y": 0.9, "w": 0.8, "h": 0.05, "size_ratio": 0.05, "text": "x"}]
    check("1:1 -- o'zgarmaydi", creative_studio.adapt_layers_for_aspect(layers, "1:1") == layers)
    adapted = creative_studio.adapt_layers_for_aspect(layers, "9:16")[0]
    check("9:16 -- pastki CTA xavfsiz zonaga ko'tarildi", adapted["y"] + adapted["h"] <= creative_studio.STORY_SAFE_BOTTOM + 1e-6, str(adapted))
    check("kirish o'zgartirilmadi (sof funksiya)", layers[0]["y"] == 0.9)


def _seed():
    session = db_module.get_session()
    try:
        out = {}
        for key in ("A", "B"):
            c = db_module.Company(name=f"{key} MChJ", plan="unlimited", is_active=True,
                                  paid_until=db_module.dt.datetime.utcnow() + db_module.dt.timedelta(days=365))
            session.add(c)
            session.commit()
            m = db_module.Manager(username=f"{key.lower()}_admin", role="admin", company_id=c.id, full_name=key)
            m.set_password(PW)
            session.add(m)
            session.commit()
            out[key] = c.id
        return out
    finally:
        session.close()


def test_create_from_scratch_and_source_without_openai():
    ids = _seed()
    session = db_module.get_session()
    try:
        company = session.get(db_module.Company, ids["A"])
        before_quota = creative_studio.get_usage(session, company.id)
        _network_calls.clear()
        try:
            creative_carousel.create_carousel(session, company, None, style_key="dark_luxury", n_cards=4)
            check("bo'sh profil -- takroriy kartalar o'rniga aniq yo'riqnoma", False)
        except creative_studio.CreativeError as e:
            check("bo'sh profil -- takroriy kartalar o'rniga aniq yo'riqnoma", "afzallik" in str(e), str(e))
        import json as _json
        company.business_profile_answers = _json.dumps({
            "best_seller": "Qishki etiklar", "product_or_service": "Poyabzal",
            "extra_notes": "Haqiqiy charm, Bepul yetkazish, 1 yil kafolat", "price_range": "399 000 so'm",
        }, ensure_ascii=False)
        session.commit()
        cards = creative_carousel.create_carousel(session, company, None, style_key="dark_luxury", n_cards=4, aspect="9:16")
        texts_on_cards = [l.get("text") for c in cards for l in creative_studio.get_layers(c) if l.get("id") == "feature_text"]
        check("profil afzalliklari kartalarga tushdi", texts_on_cards == ["Haqiqiy charm", "Bepul yetkazish"], str(texts_on_cards))
        check("manbasiz: kartalar yaratildi va tayyor", len(cards) >= 3 and all(c.status == "ready" and c.final_storage_path for c in cards), str([(c.status, c.error_message) for c in cards]))
        info = creative_carousel.info(cards[0])
        check("guruh ma'lumoti (index/total/style)", info["index"] == 1 and info["total"] == len(cards) and info["style"] == "dark_luxury")
        check("9:16 o'lcham", (cards[0].width, cards[0].height) == (1080, 1920))

        src = creative_studio.create_from_template(session, company, None, "bold_sale", aspect="1:1")
        cards2 = creative_carousel.create_carousel(session, company, None, style_key="sticker_pop", n_cards=5, source_asset=src)
        check("tayyor kreativdan karusel", len(cards2) >= 3 and all(c.status == "ready" for c in cards2))
        try:
            creative_carousel.create_carousel(session, company, None, style_key="sticker_pop", source_asset=cards2[0])
            check("karta ichidan qayta karusel -- rad etiladi", False)
        except creative_studio.CreativeError:
            check("karta ichidan qayta karusel -- rad etiladi", True)
        check("rasm AI'si (images) chaqirilmadi", not [u for u in _network_calls if "images" in str(u)], str(_network_calls))
        check("AI kvota sarflanmadi", creative_studio.get_usage(session, company.id) == before_quota)
        group = creative_carousel.info(cards2[0])["group"]
        check("guruh bo'yicha topiladi (tartibda)", [creative_carousel.info(c)["index"] for c in creative_carousel.group_assets(session, company.id, group)] == list(range(1, len(cards2) + 1)))
        check("boshqa kompaniya guruhni ko'rmaydi", creative_carousel.group_assets(session, ids["B"], group) == [])
        check("noto'g'ri guruh formati -- bo'sh", creative_carousel.group_assets(session, company.id, "x' OR 1=1 --") == [])
    finally:
        session.close()
    return ids


def test_http_flow(ids):
    a = app_module.app.test_client()
    check("A login", a.post("/login", data={"username": "a_admin", "password": PW}).status_code == 302)
    r = a.get("/kreativ/shablonlar")
    html = r.get_data(as_text=True)
    check("galereyada 6 ta karusel uslubi real rasm bilan", r.status_code == 200 and html.count("creative_templates/carousel_") == 6)
    check("namuna rasmlari mavjud", all((Path(app_module.app.static_folder) / "creative_templates" / f"carousel_{s['key']}.png").exists() for s in creative_carousel.CAROUSEL_STYLES))
    r = a.post("/kreativ/karusel/yangi", data={"style": "minimal_white", "cards": "4", "aspect": "4:5"})
    check("yaratish -> karusel sahifasiga redirect", r.status_code == 302 and "/kreativ/karusel/" in r.headers.get("Location", ""), r.headers.get("Location"))
    url = r.headers.get("Location", "")
    r = a.get(url)
    check("karusel sahifasi 200", r.status_code == 200 and "ZIP" in r.get_data(as_text=True))
    z = a.get(url + ".zip")
    ok_zip = z.status_code == 200
    if ok_zip:
        names = zipfile.ZipFile(io.BytesIO(z.data)).namelist()
        ok_zip = names and names[0] == "karusel_01.png"
    check("ZIP: kartalar tartib bilan", bool(ok_zip))
    lst = a.get("/kreativ").get_data(as_text=True)
    check("ro'yxatda 'Karusel 1/n' belgisi", "Karusel 1/" in lst)
    b = app_module.app.test_client()
    b.post("/login", data={"username": "b_admin", "password": PW})
    check("B: A karuseli 404", b.get(url).status_code == 404)
    check("B: A ZIP 404", b.get(url + ".zip").status_code == 404)


def run_all():
    test_plan_cards()
    test_all_styles_render_and_story_safe_zone()
    test_adapt_layers_keeps_other_aspects()
    ids = test_create_from_scratch_and_source_without_openai()
    test_http_flow(ids)
    if failures:
        print(f"\n{len(failures)} ta XATO:")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("\nBARCHA TESTLAR O'TDI")


if __name__ == "__main__":
    run_all()
