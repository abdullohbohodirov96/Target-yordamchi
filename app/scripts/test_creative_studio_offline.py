"""test_creative_studio_offline.py — Kreativ studiya (2026-09, AI
rasm-generatsiya) yadrosi, `creative_studio.py` + `creative_templates.py`
+ `plans.image_generation_monthly_limit` + `db.CreativeAsset`/
`CompanyBrandKit`/`ImageGenerationUsage`:

  1. `CREATIVE_TEMPLATES` -- aniq 20 ta, majburiy kalitlar, unique key'lar,
     qatlam sxemasi (0..1 koordinatalar, ma'lum type'lar).
  2. `missing_questions()` -- bo'sh ctx'da hamma savol (focus MAJBURIY),
     to'liq ctx'da bo'sh ro'yxat.
  3. `check_quota()` -- trial (0) darhol xato, start (10) 10-chidan keyin,
     unlimited hech qachon.
  4. `generate_base_image()` -- OpenAI HTTP mock: muvaffaqiyat -> ready +
     final PNG diskda + kvota +1; OpenAI xato -> failed + o'zbekcha xabar,
     kvota O'ZGARMAYDI; brif to'liq bo'lmasa -> CreativeError, OpenAI
     UMUMAN chaqirilmaydi; kredit tugagan (429) -> friendly xabar.
  5. `render_composite()` -- logotipli/logotipsiz, chiqish o'lchami aniq.
  6. `set_layers()` -- qayta chizadi, OpenAI chaqirmaydi; noto'g'ri
     qatlam rad etiladi.
  7. `create_from_template()` -- OpenAI chaqirilmaydi, status darhol ready.
  8. `export_pdf()` -- fayl mavjud, "%PDF" magic bytes.
  9. Multi-tenant: B kompaniya A rasmlarini/brend kitini ko'rmaydi
     (`db.scoped_as`), yangi modellar `_COMPANY_SCOPED_MODELS`da.
 10. `plans` -- limitlar va FEATURE_MATRIX qatori.

HAQIQIY tarmoqqa HECH QACHON chiqmaydi (`creative_studio._openai_request`
mock qilinadi).

Ishga tushirish:
    cd app && python3 scripts/test_creative_studio_offline.py
"""
import io
import os
import sys
import json
import base64
import tempfile
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("META_ACCESS_TOKEN", "test-dummy-token")
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_test_dummy")
os.environ.setdefault("META_PAGE_ID", "page_test_dummy")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ["OPENAI_API_KEY"] = "sk-test-dummy"

_TMPDIR = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMPDIR, 'test_creative_studio.db')}"

from PIL import Image  # noqa: E402

import db as db_module  # noqa: E402
import plans  # noqa: E402
import creative_templates  # noqa: E402
import creative_studio  # noqa: E402

db_module.init_db()
creative_studio.CREATIVE_ROOT = Path(_TMPDIR) / "creative_studio"
creative_studio.BRAND_ROOT = Path(_TMPDIR) / "brand_kit"

failures = []


def check(name, cond):
    print(("OK  " if cond else "FAIL") + " " + name)
    if not cond:
        failures.append(name)


def _png_bytes(w=2, h=2, color=(120, 40, 200)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _rgba_png_bytes(w=64, h=32):
    buf = io.BytesIO()
    Image.new("RGBA", (w, h), (255, 0, 0, 128)).save(buf, format="PNG")
    return buf.getvalue()


class _FakeResp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def _ok_openai_response():
    return _FakeResp(200, {"created": 1700000000, "data": [{"b64_json": base64.b64encode(_png_bytes(2, 2)).decode()}]})


def _company(session, name, plan="business", *, full_profile=True):
    c = db_module.Company(name=name, plan=plan, is_active=True, business_category="clothing_fashion")
    if full_profile:
        c.business_profile_answers = json.dumps({
            "product_or_service": "Erkaklar oyoq kiyimi, 30 dan ortiq model",
            "target_audience": "25-45 yosh erkaklar, Toshkent",
            "price_range": "300 000 - 700 000 so'm",
            "best_seller": "Klassik charm tufli",
            "extra_notes": "Bepul yetkazib berish, 1 yil kafolat, chinakam charm",
        }, ensure_ascii=False)
    session.add(c)
    session.commit()
    return c


# ---------------------------------------------------------------------------
def test_templates():
    tpls = creative_templates.CREATIVE_TEMPLATES
    check("aniq 20 ta shablon", len(tpls) == 20)
    keys = [t["key"] for t in tpls]
    check("shablon key'lari unique", len(set(keys)) == 20)
    required = {"key", "name", "category", "description", "style_prompt", "background", "aspect_default", "layers"}
    check("har bir shablonda majburiy kalitlar", all(required <= set(t.keys()) for t in tpls))
    check("style_prompt'da 'no text' bor", all("no text" in t["style_prompt"] for t in tpls))
    check("aspect_default ruxsat etilgan", all(t["aspect_default"] in creative_studio.ASPECTS for t in tpls))
    check("story_fullbleed 9:16", creative_templates.get_template("story_fullbleed")["aspect_default"] == "9:16")
    ok_layers = True
    for t in tpls:
        for layer in t["layers"]:
            if layer["type"] not in creative_studio.LAYER_TYPES:
                ok_layers = False
            for k in ("x", "y", "w", "h"):
                if not (0.0 <= float(layer[k]) <= 1.0):
                    ok_layers = False
            if layer["type"] in ("text", "badge") and not {"align", "font", "size_ratio", "color", "text"} <= set(layer):
                ok_layers = False
        has_headline = any(l.get("id") == "headline" for l in t["layers"])
        has_logo = any(l.get("type") == "logo" for l in t["layers"])
        if not (has_headline and has_logo):
            ok_layers = False
    check("qatlamlar sxemaga mos (0..1, type, headline+logo bor)", ok_layers)
    check("get_template noma'lum -> None", creative_templates.get_template("yoq_shablon") is None)
    check("style_prompt'lar takrorlanmas", len({t["style_prompt"] for t in tpls}) == 20)
    for t in tpls:
        if not (Path(__file__).resolve().parent.parent / "static" / "creative_templates" / f"{t['key']}.png").exists():
            check(f"preview PNG mavjud: {t['key']}", False)
            break
    else:
        check("20 ta preview PNG static/creative_templates/ da", True)


def test_missing_questions_and_placeholders():
    import company_context
    empty_ctx = company_context.build_company_context(None)
    q = creative_studio.missing_questions(empty_ctx, {})
    check("bo'sh ctx -> barcha savollar", [x["key"] for x in q] == [k for k, *_ in creative_studio.CREATIVE_BRIEF_QUESTIONS])
    check("focus majburiy, qolganlari ixtiyoriy", q[0]["required"] is True and all(not x["required"] for x in q[1:]))
    session = db_module.get_session()
    try:
        c = _company(session, "Full Co")
        full_ctx = company_context.build_company_context(c, session)
        check("to'liq ctx -> bo'sh ro'yxat", creative_studio.missing_questions(full_ctx, {}) == [])
        check("bo'sh ctx + focus javobi -> bo'sh ro'yxat", creative_studio.missing_questions(empty_ctx, {"focus": "Tufli"}) == [])
        q2 = creative_studio.missing_questions(empty_ctx, {"offer_text": "", "cta_preference": ""})
        check("ko'rib chiqilgan ixtiyoriy savollar qayta so'ralmaydi", [x["key"] for x in q2] == ["focus", "style_notes"])

        vals = creative_studio.placeholder_values(full_ctx, {"offer_text": "-30% chegirma", "cta_preference": "Buyurtma bering"}, None)
        check("headline best_seller'dan", vals["headline"] == "Klassik charm tufli")
        check("subheadline offer'dan", vals["subheadline"] == "-30% chegirma")
        check("cta brifdan", vals["cta_text"] == "Buyurtma bering")
        check("feature'lar extra_notes'dan", vals["feature_1"] == "Bepul yetkazib berish" and vals["feature_3"] == "chinakam charm" and vals["feature_4"] == "")
        vals2 = creative_studio.placeholder_values(full_ctx, {"offer_text": "yo'q"}, creative_templates.get_template("bold_sale"))
        check("'yo'q' -> offer_text standart AKSIYA, cta shablondan", vals2["offer_text"] == "AKSIYA" and vals2["cta_text"] == "Hoziroq xarid qiling")
        prompt = creative_studio.build_image_prompt(full_ctx, {"focus": "Qishki etik", "style_notes": "qor fon"}, creative_templates.get_template("luxury_dark"))
        check("prompt: no text + focus + style", "no text" in prompt and "Qishki etik" in prompt and "qor fon" in prompt and "luxurious" in prompt)
        check("_size_for_aspect", creative_studio._size_for_aspect("1:1") == "1024x1024" and creative_studio._size_for_aspect("9:16") == "1024x1536" and creative_studio._size_for_aspect("x") == "1024x1024")
        check("_target_pixels_for_aspect", creative_studio._target_pixels_for_aspect("4:5") == (1080, 1350) and creative_studio._target_pixels_for_aspect("9:16") == (1080, 1920))
    finally:
        session.close()


def test_quota():
    session = db_module.get_session()
    try:
        c = _company(session, "Quota Co", plan="start")
        try:
            creative_studio.check_quota(c, plans.PLANS["trial"], session)
            check("trial (0) darhol QuotaExceededError", False)
        except creative_studio.QuotaExceededError as e:
            check("trial (0) darhol QuotaExceededError", "tarif" in str(e).lower())
        check("boshlang'ich ishlatish 0", creative_studio.get_usage(session, c.id) == 0)
        for _ in range(10):
            creative_studio.check_quota(c, plans.PLANS["start"], session)
            creative_studio.increment_usage(session, c.id)
        check("10 marta ishlatildi", creative_studio.get_usage(session, c.id) == 10)
        try:
            creative_studio.check_quota(c, plans.PLANS["start"], session)
            check("start (10) 10-chidan keyin xato", False)
        except creative_studio.QuotaExceededError as e:
            check("start (10) 10-chidan keyin xato", "10/10" in str(e))
        check("QuotaExceededError -- CreativeError'ning avlodi", issubclass(creative_studio.QuotaExceededError, creative_studio.CreativeError))
        try:
            creative_studio.check_quota(c, plans.PLANS["unlimited"], session)
            creative_studio.check_quota(c, plans.PLANS["unlimited"], session, need=1000)
            check("unlimited hech qachon xato bermaydi", True)
        except creative_studio.QuotaExceededError:
            check("unlimited hech qachon xato bermaydi", False)
        st = creative_studio.quota_status(session, c, plans.PLANS["business"])
        check("quota_status", st["used"] == 10 and st["limit"] == 30 and st["remaining"] == 20 and st["enabled"])
        with db_module.unscoped():
            rows = session.query(db_module.ImageGenerationUsage).filter_by(company_id=c.id).all()
        check("bitta (company, period) qatori", len(rows) == 1 and rows[0].period_key == creative_studio.current_period_key())
    finally:
        session.close()


def test_generate_flow():
    session = db_module.get_session()
    try:
        c = _company(session, "Gen Co", plan="business")
        plan = plans.PLANS["business"]

        # --- brif to'liq bo'lmagan kompaniya: OpenAI umuman chaqirilmaydi
        c_empty = _company(session, "Empty Co", plan="business", full_profile=False)
        a0 = creative_studio.create_draft_asset(session, c_empty, None)
        check("draft: collecting_brief + missing_fields", a0.status == "collecting_brief" and "focus" in json.loads(a0.missing_fields_json))
        with mock.patch.object(creative_studio, "_openai_request", return_value=_ok_openai_response()) as req:
            try:
                creative_studio.generate_base_image(session, a0, c_empty, plan)
                check("brif to'liq emas -> CreativeError", False)
            except creative_studio.CreativeError as e:
                check("brif to'liq emas -> CreativeError", "savol" in str(e))
            check("brif to'liq emas -> OpenAI chaqirilmagan", req.call_count == 0)
            check("brif to'liq emas -> status o'zgarmagan (failed emas)", a0.status == "collecting_brief")
            creative_studio.submit_brief_answer(session, a0, "focus", "Qishki etiklar")
            check("javobdan keyin missing bo'sh", json.loads(a0.missing_fields_json) == [])
            creative_studio.generate_base_image(session, a0, c_empty, plan)
            check("javobdan keyin generatsiya o'tdi", a0.status == "ready" and req.call_count == 1)
        try:
            creative_studio.submit_brief_answer(session, a0, "hacker_key", "x")
            check("noma'lum brif kaliti rad etiladi", False)
        except creative_studio.CreativeError:
            check("noma'lum brif kaliti rad etiladi", True)

        # --- muvaffaqiyatli generatsiya (shablon bilan)
        a = creative_studio.create_draft_asset(session, c, None, template_key="bold_sale", aspect="4:5")
        creative_studio.submit_brief_answer(session, a, "offer_text", "-40% chegirma")
        before = creative_studio.get_usage(session, c.id)
        with mock.patch.object(creative_studio, "_openai_request", return_value=_ok_openai_response()) as req:
            creative_studio.generate_base_image(session, a, c, plan)
        check("OpenAI bir marta chaqirildi", req.call_count == 1)
        body = req.call_args.kwargs["json_body"]
        check("so'rov: model/size/n/prompt", body["model"] == creative_studio.OPENAI_IMAGE_MODEL and body["size"] == "1024x1536" and body["n"] == 1 and "no text" in body["prompt"])
        check("Authorization sarlavhasi", req.call_args.kwargs["headers"]["Authorization"] == "Bearer sk-test-dummy")
        check("status ready", a.status == "ready" and a.error_message is None)
        check("prompt_used saqlangan", a.prompt_used and "vibrant" in a.prompt_used)
        final = creative_studio.export_png_path(a)
        check("final PNG diskda", final.exists() and str(final).startswith(str(creative_studio.CREATIVE_ROOT / str(c.id) / str(a.id))))
        with Image.open(final) as img:
            check("final PNG 1080x1350 (4:5)", img.format == "PNG" and img.size == (1080, 1350))
        check("width/height", a.width == 1080 and a.height == 1350)
        base = creative_studio.CREATIVE_ROOT / a.base_image_storage_path
        check("base.png diskda", base.exists())
        check("kvota +1", creative_studio.get_usage(session, c.id) == before + 1)
        layers = creative_studio.get_layers(a)
        check("layers shablondan, placeholder'lar almashtirilgan", any(l["id"] == "headline" and l["text"] == "Klassik charm tufli" for l in layers) and not any("{{" in (l.get("text") or "") for l in layers))
        check("offer badge brifdan", any(l["id"] == "offer_badge" and l["text"] == "-40% chegirma" for l in layers))
        check("title avtomatik", a.title == "Klassik charm tufli")
        check("asset_image_bytes PNG", creative_studio.asset_image_bytes(a)[:8] == b"\x89PNG\r\n\x1a\n")

        # --- set_layers: qayta chizadi, OpenAI chaqirmaydi
        edited = [l for l in layers if l["id"] != "subheadline"]
        for l in edited:
            if l["id"] == "headline":
                l["text"] = "O'zgartirilgan sarlavha"
                l["x"] = 0.2
        old_mtime = final.stat().st_mtime_ns
        with mock.patch.object(creative_studio, "_openai_request") as req2:
            creative_studio.set_layers(session, a, edited)
        check("set_layers OpenAI chaqirmaydi", req2.call_count == 0)
        check("set_layers saqlandi", any(l["id"] == "headline" and l["text"] == "O'zgartirilgan sarlavha" and l["x"] == 0.2 for l in a.get_layers()) and not any(l["id"] == "subheadline" for l in a.get_layers()))
        check("set_layers qayta chizdi", final.exists() and final.stat().st_mtime_ns >= old_mtime)
        check("kvota o'zgarmadi (tahrir)", creative_studio.get_usage(session, c.id) == before + 1)
        for bad in ([{"type": "script", "x": 0, "y": 0, "w": 1, "h": 1}], "notalist", [{"type": "text", "color": "red", "x": 0, "y": 0, "w": 1, "h": 1}]):
            try:
                creative_studio.set_layers(session, a, bad)
                check("noto'g'ri qatlam rad etiladi", False)
                break
            except creative_studio.CreativeError:
                pass
        else:
            check("noto'g'ri qatlam rad etiladi", True)
        clean = creative_studio.sanitize_layers([{"type": "text", "x": 5, "y": -1, "w": 0.5, "h": 0.1, "text": "a" * 500, "size_ratio": 9}])
        check("sanitize: koordinata/hajm chegaralanadi", clean[0]["x"] == 1.0 and clean[0]["y"] == 0.0 and len(clean[0]["text"]) == 300 and clean[0]["size_ratio"] == 0.5)

        # --- regenerate: layers saqlanadi, kvota yana sarflanadi
        with mock.patch.object(creative_studio, "_openai_request", return_value=_ok_openai_response()) as req3:
            creative_studio.regenerate_base_image(session, a, c, plan)
        check("regenerate: OpenAI qayta chaqirildi, kvota +1", req3.call_count == 1 and creative_studio.get_usage(session, c.id) == before + 2)
        check("regenerate: tahrirlangan layers saqlangan", any(l["id"] == "headline" and l["text"] == "O'zgartirilgan sarlavha" for l in a.get_layers()))

        # --- OpenAI xato (HTTP 500): failed + o'zbekcha xabar, kvota o'zgarmaydi
        b = creative_studio.create_draft_asset(session, c, None)
        used = creative_studio.get_usage(session, c.id)
        with mock.patch.object(creative_studio, "_openai_request", return_value=_FakeResp(500, {"error": {"message": "internal server error RAW"}})):
            try:
                creative_studio.generate_base_image(session, b, c, plan)
                check("OpenAI xato -> CreativeError", False)
            except creative_studio.CreativeError as e:
                check("OpenAI xato -> CreativeError", "RAW" not in str(e) and "qayta" in str(e))
        check("OpenAI xato -> status failed + o'zbekcha error_message", b.status == "failed" and b.error_message and "RAW" not in b.error_message)
        check("OpenAI xato -> kvota O'ZGARMADI", creative_studio.get_usage(session, c.id) == used)
        check("OpenAI xato -> final yo'q", b.final_storage_path is None)

        # --- kredit tugagan (429 insufficient_quota)
        b2 = creative_studio.create_draft_asset(session, c, None)
        with mock.patch.object(creative_studio, "_openai_request", return_value=_FakeResp(429, {"error": {"code": "insufficient_quota", "message": "You exceeded your current quota"}})):
            try:
                creative_studio.generate_base_image(session, b2, c, plan)
                check("kredit tugagan -> friendly xabar", False)
            except creative_studio.CreativeError as e:
                check("kredit tugagan -> friendly xabar", "balans" in str(e) and "quota" not in str(e).lower())
        check("kredit tugagan -> kvota o'zgarmadi", creative_studio.get_usage(session, c.id) == used)

        # --- tarmoq xatosi
        import requests
        b3 = creative_studio.create_draft_asset(session, c, None)
        with mock.patch.object(creative_studio, "_openai_request", side_effect=requests.exceptions.ConnectionError("boom sk-secret")):
            try:
                creative_studio.generate_base_image(session, b3, c, plan)
                check("tarmoq xatosi -> CreativeError", False)
            except creative_studio.CreativeError as e:
                check("tarmoq xatosi -> CreativeError (xom matn yo'q)", "sk-secret" not in str(e))
        check("tarmoq xatosi -> failed", b3.status == "failed")

        # --- failed asset qayta generatsiya qilinishi mumkin
        with mock.patch.object(creative_studio, "_openai_request", return_value=_ok_openai_response()):
            creative_studio.generate_base_image(session, b3, c, plan)
        check("failed -> qayta urinish ready", b3.status == "ready" and b3.error_message is None)

        # --- OPENAI_API_KEY yo'q
        b4 = creative_studio.create_draft_asset(session, c, None)
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}), mock.patch.object(creative_studio, "_openai_request") as req5:
            try:
                creative_studio.generate_base_image(session, b4, c, plan)
                check("OPENAI_API_KEY yo'q -> CreativeError", False)
            except creative_studio.CreativeError as e:
                check("OPENAI_API_KEY yo'q -> CreativeError", "OPENAI_API_KEY" in str(e) and req5.call_count == 0)

        # --- kvota tugagan kompaniya: OpenAI chaqirilmaydi
        c_trial = _company(session, "Trial Co", plan="trial")
        t = creative_studio.create_draft_asset(session, c_trial, None)
        with mock.patch.object(creative_studio, "_openai_request") as req6:
            try:
                creative_studio.generate_base_image(session, t, c_trial, plans.PLANS["trial"])
                check("trial -> QuotaExceededError", False)
            except creative_studio.QuotaExceededError:
                check("trial -> QuotaExceededError, OpenAI chaqirilmagan", req6.call_count == 0)
        check("trial -> asset holati o'zgarmaydi (qayta urinish mumkin), generating emas", t.status == "collecting_brief" and t.error_message is None)

        # noto'g'ri shablon/aspekt
        for kwargs in ({"template_key": "yoq"}, {"aspect": "16:9"}):
            try:
                creative_studio.create_draft_asset(session, c, None, **kwargs)
                check(f"create_draft_asset noto'g'ri {kwargs} rad etiladi", False)
            except creative_studio.CreativeError:
                check(f"create_draft_asset noto'g'ri {kwargs} rad etiladi", True)
    finally:
        session.close()


def test_render_composite_and_brand_kit():
    session = db_module.get_session()
    try:
        c = _company(session, "Brand Co")
        base_path = Path(_TMPDIR) / "base_test.png"
        base_path.write_bytes(_png_bytes(1024, 1536, (30, 90, 200)))
        layers = creative_studio.resolve_layers(creative_templates.get_template("minimal_clean")["layers"], {
            "headline": "O'zbekcha sarlavha: so'm, ko'ring, g'isht",
            "subheadline": "Tavsif matni juda uzun bo'lsa ham qatorlarga bo'linib, ramkaga sig'ishi kerak " * 3,
            "cta_text": "Xarid qiling",
        })
        out1 = Path(_TMPDIR) / "out_nologo.png"
        creative_studio.render_composite(base_path, layers, None, out1, target_size=(1080, 1080))
        with Image.open(out1) as img:
            check("logotipsiz render: 1080x1080 PNG", img.size == (1080, 1080) and img.format == "PNG")
            # Matn chizilganmi -- pastki chap zonada fon rangidan farqli piksellar bor
            px = img.convert("RGB").crop((90, 780, 990, 900)).getcolors(200000)
            check("matn haqiqatan chizilgan", px and len(px) > 5)

        # brend kit: logotip (RGBA) + ranglar
        kit = creative_studio.save_brand_logo(session, c.id, _rgba_png_bytes(), "logo.png", "image/png")
        lp = creative_studio.brand_logo_file_path(kit)
        check("logotip saqlandi", lp and lp.exists() and str(lp).endswith("logo.png") and kit.logo_content_type == "image/png")
        kit = creative_studio.save_brand_colors(session, c.id, "#ff8800", "abc")
        check("ranglar normallashtirildi", kit.primary_color == "#FF8800" and kit.secondary_color == "#AABBCC")
        try:
            creative_studio.save_brand_colors(session, c.id, "qizil", None)
            check("noto'g'ri rang rad etiladi", False)
        except creative_studio.CreativeError:
            check("noto'g'ri rang rad etiladi", True)
        check("get_brand_kit bitta qator", creative_studio.get_brand_kit(session, c.id).id == kit.id)
        for fname, ct, data in (("virus.exe", "application/octet-stream", b"MZ.."), ("x.png", "image/png", b"notpng"), ("", "image/png", b"")):
            try:
                creative_studio.save_brand_logo(session, c.id, data, fname, ct)
                check(f"logotip rad etiladi: {fname!r}", False)
            except creative_studio.CreativeError:
                check(f"logotip rad etiladi: {fname!r}", True)
        with mock.patch.object(creative_studio, "MAX_LOGO_BYTES", 10):
            try:
                creative_studio.save_brand_logo(session, c.id, _rgba_png_bytes(), "big.png", "image/png")
                check("logotip hajm chegarasi", False)
            except creative_studio.CreativeError as e:
                check("logotip hajm chegarasi", "katta" in str(e))
        # JPG logotip eski PNG'ni almashtiradi
        jpg = io.BytesIO()
        Image.new("RGB", (40, 40), (0, 0, 0)).save(jpg, format="JPEG")
        kit = creative_studio.save_brand_logo(session, c.id, jpg.getvalue(), "logo.jpg", None)
        check("JPG logotip -> eski PNG o'chirildi", kit.logo_storage_path.endswith("logo.jpg") and not (creative_studio.BRAND_ROOT / str(c.id) / "logo.png").exists())
        creative_studio.save_brand_logo(session, c.id, _rgba_png_bytes(), "logo.png", "image/png")
        kit = creative_studio.get_brand_kit(session, c.id)

        out2 = Path(_TMPDIR) / "out_logo.png"
        creative_studio.render_composite(base_path, layers, kit, out2, target_size=(1080, 1920))
        with Image.open(out2) as img:
            check("logotipli render: 1080x1920", img.size == (1080, 1920))
            # logo zonasi (x=0.80..0.94, y=0.06..0.15) -- qizil aralashgan piksellar
            region = img.convert("RGB").crop((int(0.80 * 1080), int(0.06 * 1920), int(0.94 * 1080), int(0.15 * 1920)))
            rw, rh = region.size
            reds = [1 for yy in range(0, rh, 2) for xx in range(0, rw, 2) if (lambda p: p[0] > p[2] + 40)(region.getpixel((xx, yy)))]
            check("logotip haqiqatan chizilgan (shaffoflik bilan)", len(reds) > 100)
        # logotip yo'li bor, lekin fayl o'chirilgan -> xato bermaydi
        lp.unlink()
        creative_studio.render_composite(base_path, layers, kit, out2, target_size=(500, 500))
        check("logotip fayli yo'q -> o'tkazib yuboriladi", out2.exists())
        # panel/badge/gradient bilan (story shablon)
        st_layers = creative_studio.resolve_layers(creative_templates.get_template("story_fullbleed")["layers"], {"headline": "A", "subheadline": "B", "cta_text": "C"})
        out3 = Path(_TMPDIR) / "out_story.png"
        creative_studio.render_composite(base_path, st_layers, None, out3, target_size=(1080, 1920))
        with Image.open(out3) as img:
            top = img.convert("RGB").getpixel((540, 300))
            bottom = img.convert("RGB").getpixel((540, 1880))
            check("gradient overlay: pastki qism qorong'iroq", sum(bottom) < sum(top))
        check("background_image gradient/solid", creative_studio.background_image({"type": "gradient", "colors": ["#000000", "#FFFFFF"], "direction": "diagonal"}, (50, 50)).size == (50, 50) and creative_studio.background_image({"type": "solid", "colors": ["#123456"]}, (10, 20)).getpixel((0, 0)) == (0x12, 0x34, 0x56))
        hidden = creative_studio.resolve_layers([{"id": "price_badge", "type": "badge", "x": 0, "y": 0, "w": 0.3, "h": 0.1, "text": "{{price_text}}", "bg_color": "#000000", "color": "#FFFFFF", "size_ratio": 0.03, "font": "bold", "align": "center"}], {"price_text": ""})
        check("bo'sh placeholder -> hidden", hidden[0]["hidden"] is True and hidden[0]["text"] == "")
    finally:
        session.close()


def test_create_from_template_and_export():
    session = db_module.get_session()
    try:
        c = _company(session, "Tpl Co", plan="trial")  # trial: OpenAI kvotasi 0, lekin shablon ishlaydi
        with mock.patch.object(creative_studio, "_openai_request") as req:
            a = creative_studio.create_from_template(session, c, None, "luxury_dark")
        check("shablon: OpenAI chaqirilmagan", req.call_count == 0)
        check("shablon: status darhol ready, kind=template", a.status == "ready" and a.kind == "template" and a.template_key == "luxury_dark")
        check("shablon: kvota sarflanmagan", creative_studio.get_usage(session, c.id) == 0)
        with Image.open(creative_studio.export_png_path(a)) as img:
            check("shablon: final 1080x1080", img.size == (1080, 1080))
        check("shablon: layers profil matni bilan", any(l["id"] == "headline" and l["text"] == "Klassik charm tufli" for l in a.get_layers()))

        with mock.patch.object(creative_studio, "_openai_request") as req:
            b = creative_studio.create_from_template(session, c, None, "story_fullbleed", product_image_bytes=_png_bytes(300, 200, (0, 255, 0)))
        check("mahsulot rasmi bilan shablon: 9:16, OpenAI yo'q", b.status == "ready" and b.aspect == "9:16" and b.width == 1080 and b.height == 1920 and req.call_count == 0)
        with Image.open(creative_studio.CREATIVE_ROOT / b.base_image_storage_path) as img:
            check("mahsulot rasmi fon sifatida (cover)", img.size == (1080, 1920) and img.getpixel((540, 600)) == (0, 255, 0))
        for bad in ({"template_key": "yoq"}, {"template_key": "luxury_dark", "product_image_bytes": b"notimage"}, {"template_key": "luxury_dark", "aspect": "3:2"}):
            try:
                creative_studio.create_from_template(session, c, None, bad.pop("template_key"), **bad)
                check("create_from_template noto'g'ri kirish rad etiladi", False)
                break
            except creative_studio.CreativeError:
                pass
        else:
            check("create_from_template noto'g'ri kirish rad etiladi", True)

        pdf_path = Path(_TMPDIR) / "export" / "a.pdf"
        creative_studio.export_pdf(a, pdf_path)
        data = pdf_path.read_bytes()
        check("export_pdf: fayl mavjud, %PDF magic", pdf_path.exists() and len(data) > 1000 and data[:4] == b"%PDF")
        try:
            creative_studio.export_pdf(db_module.CreativeAsset(company_id=c.id), Path(_TMPDIR) / "x.pdf")
            check("tayyor bo'lmagan asset eksport qilinmaydi", False)
        except creative_studio.CreativeError:
            check("tayyor bo'lmagan asset eksport qilinmaydi", True)
        lst = creative_studio.list_assets(session, c.id)
        check("list_assets yangi->eski", [x.id for x in lst] == [b.id, a.id])
        d = creative_studio._asset_dir(b)
        creative_studio.delete_asset(session, b)
        check("delete_asset qator+papka", not d.exists() and len(creative_studio.list_assets(session, c.id)) == 1)
    finally:
        session.close()


def test_multitenant_isolation():
    session = db_module.get_session()
    try:
        a_co = _company(session, "Tenant A")
        b_co = _company(session, "Tenant B")
        with mock.patch.object(creative_studio, "_openai_request"):
            asset_a = creative_studio.create_from_template(session, a_co, None, "minimal_clean")
        creative_studio.save_brand_logo(session, a_co.id, _rgba_png_bytes(), "logo.png", "image/png")
        creative_studio.increment_usage(session, a_co.id)
        a_id, b_id, asset_id = a_co.id, b_co.id, asset_a.id
        session.close()
        session = db_module.get_session()  # yangi sessiya -- identity map'da eski obyekt qolmasin
        with db_module.scoped_as(b_id):
            check("B kompaniya A rasmini ko'rmaydi (query)", session.query(db_module.CreativeAsset).filter_by(id=asset_id).first() is None)
            check("B kompaniya A rasmini ko'rmaydi (get)", session.get(db_module.CreativeAsset, asset_id) is None)
            check("B kompaniya A brend kitini ko'rmaydi", session.query(db_module.CompanyBrandKit).count() == 0)
            check("B kompaniya A kvota qatorini ko'rmaydi", session.query(db_module.ImageGenerationUsage).count() == 0)
            check("B uchun get_usage 0, A uchun 1", creative_studio.get_usage(session, b_id) == 0 and creative_studio.get_usage(session, a_id) == 1)
            check("B uchun get_brand_kit None", creative_studio.get_brand_kit(session, b_id) is None)
        with db_module.scoped_as(a_id):
            check("A o'zinikini ko'radi", session.get(db_module.CreativeAsset, asset_id) is not None and creative_studio.get_brand_kit(session, a_id) is not None)
        check("yangi modellar tenant ro'yxatida", all(m in db_module._COMPANY_SCOPED_MODELS for m in (db_module.CompanyBrandKit, db_module.CreativeAsset, db_module.ImageGenerationUsage)))
        check("CampaignDraftMedia.creative_asset_id ustuni", hasattr(db_module.CampaignDraftMedia, "creative_asset_id"))
        # delete_company_cascade yangi jadvallarni ham tozalaydi
        with db_module.unscoped():
            counts = db_module.delete_company_cascade(session, a_id)
            session.commit()
            check("cascade: creative jadvallari o'chirildi", counts.get("creative_assets") == 1 and counts.get("company_brand_kits") == 1 and counts.get("image_generation_usage") == 1)
    finally:
        session.close()


def test_plans():
    check("plans: trial=0, start=10, business=30, unlimited=None", (
        plans.PLANS["trial"].image_generation_monthly_limit == 0 and plans.PLANS["start"].image_generation_monthly_limit == 10
        and plans.PLANS["business"].image_generation_monthly_limit == 30 and plans.PLANS["unlimited"].image_generation_monthly_limit is None
    ))
    row = next((r for r in plans.FEATURE_MATRIX if "Kreativ studiya" in r["label"]), None)
    check("FEATURE_MATRIX qatori", row is not None and row["values"]["trial"] is False and row["values"]["start"] == "10 tagacha rasm/oyda" and row["values"]["unlimited"] == "Cheksiz")
    check("image_generation_limit_for_plan", plans.image_generation_limit_for_plan("business") == 30 and plans.image_generation_limit_for_plan(None) == 0)


test_templates()
test_missing_questions_and_placeholders()
test_quota()
test_generate_flow()
test_render_composite_and_brand_kit()
test_create_from_template_and_export()
test_multitenant_isolation()
test_plans()

print()
if failures:
    print(f"MUVAFFAQIYATSIZ: {len(failures)} ta test o'tmadi:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("BARCHA TESTLAR O'TDI (creative_studio)")
