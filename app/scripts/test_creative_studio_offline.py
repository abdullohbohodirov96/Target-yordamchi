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
 11. (2026-09) Logotip fonini avtomatik kesish: oq/qora fonli logotip ->
     `logo_clean.png` (burchak alpha=0), shaffof PNG -> tegilmaydi (skip
     marker), eski logotip birinchi o'qishda o'zi tozalanadi (self-heal).
 12. (2026-09) AI kopirayter: telefon savoli (`ctx['phone']` bo'sh bo'lsa
     MAJBURIY), `generate_ad_copy` mock -- muvaffaqiyat/xato/buzuq JSON,
     CTA endi hech qachon umumiy "Batafsil" emas, xato bo'lsa "Bog'laning".
 13. (2026-09) "dizayn oddiy, har safar bir xil" shikoyati: AI kopirayter
     `price_text`/`features` -- faqat asoslangan bo'lsa qaytaradi (hech
     qachon o'ylab topmaydi), qisman/buzuq JSON baribir parse qilinadi;
     `select_default_layout()` -- bo'sh narx/xususiyatda bazaviy variant
     (bo'sh element yo'q), to'liq ma'lumotda BOY variant, bir xil asset id
     -- bir xil natija (barqaror), turli asset id -- haqiqiy xilma-xillik
     (bazaviy holatda ham); har bir variantda logotip+telefon+headline bor;
     yangi variant `render_composite()` orqali xatosiz to'liq render bo'ladi.

HAQIQIY tarmoqqa HECH QACHON chiqmaydi (`creative_studio._openai_request`
va AI kopirayter `creative_studio._request_ad_copy` mock qilinadi).

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

import orchestrator  # noqa: E402
import db as db_module  # noqa: E402
import plans  # noqa: E402
import creative_templates  # noqa: E402
import creative_studio  # noqa: E402

db_module.init_db()
creative_studio.CREATIVE_ROOT = Path(_TMPDIR) / "creative_studio"
creative_studio.BRAND_ROOT = Path(_TMPDIR) / "brand_kit"

failures = []

# AI kopirayter (chat/completions) butun test davomida OFFLINE: standart
# holatda "tarmoq xatosi" -> zaxira matn. Alohida testlar ichida boshqacha
# mock qilinadi (`test_ai_copywriter_and_phone`).
_COPY_OFFLINE = mock.patch.object(creative_studio, "_request_ad_copy", side_effect=RuntimeError("offline"))
_COPY_OFFLINE.start()


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


def _company(session, name, plan="business", *, full_profile=True, phone="+998 90 000 00 00"):
    c = db_module.Company(name=name, plan=plan, is_active=True, business_category="clothing_fashion", phone=phone)
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
    check("bo'sh ctx -> barcha savollar (majburiylar oldinda)", [x["key"] for x in q] == ["focus", "phone", "offer_text", "cta_preference", "style_notes"])
    check("focus + phone majburiy, qolganlari ixtiyoriy", q[0]["required"] is True and q[1]["required"] is True and all(not x["required"] for x in q[2:]))
    session = db_module.get_session()
    try:
        c = _company(session, "Full Co")
        full_ctx = company_context.build_company_context(c, session)
        check("to'liq ctx (mahsulot + telefon) -> bo'sh ro'yxat", creative_studio.missing_questions(full_ctx, {}) == [])
        check("bo'sh ctx + focus + phone javobi -> bo'sh ro'yxat", creative_studio.missing_questions(empty_ctx, {"focus": "Tufli", "phone": "+998901234567"}) == [])
        q2 = creative_studio.missing_questions(empty_ctx, {"offer_text": "", "cta_preference": "", "phone": "+998901234567"})
        check("ko'rib chiqilgan ixtiyoriy savollar qayta so'ralmaydi", [x["key"] for x in q2] == ["focus", "style_notes"])
        # Telefon: profilda yo'q -> majburiy; bor -> umuman ko'rsatilmaydi
        c_nophone = _company(session, "No Phone Co", phone=None)
        np_ctx = company_context.build_company_context(c_nophone, session)
        qn = creative_studio.missing_questions(np_ctx, {})
        check("profilda telefon yo'q -> phone MAJBURIY (mahsulot bor bo'lsa ham)", [x["key"] for x in qn if x["required"]] == ["phone"])
        check("majburiy bilan birga ixtiyoriylar ham bir martada", [x["key"] for x in qn] == ["phone", "focus", "offer_text", "cta_preference", "style_notes"] or [x["key"] for x in qn][0] == "phone" and len(qn) == 5)
        check("telefon javobidan keyin bo'sh", creative_studio.missing_questions(np_ctx, {"phone": "+998 91 111 22 33"}) == [])
        check("visible_brief_questions: telefon bor -> savol yo'q", "phone" not in [k for k, *_ in creative_studio.visible_brief_questions(full_ctx)] and "phone" in [k for k, *_ in creative_studio.visible_brief_questions(np_ctx)])
        for bad in ("", "abc", "12 34"):
            try:
                creative_studio.normalize_phone(bad)
                check(f"normalize_phone rad etadi: {bad!r}", False)
            except creative_studio.CreativeError:
                check(f"normalize_phone rad etadi: {bad!r}", True)
        check("normalize_phone tozalaydi", creative_studio.normalize_phone(" +998 (90) 123-45-67 tel ") == "+998 (90) 123-45-67")

        vals = creative_studio.placeholder_values(full_ctx, {"offer_text": "-30% chegirma", "cta_preference": "Buyurtma bering"}, None)
        check("headline best_seller'dan", vals["headline"] == "Klassik charm tufli")
        check("subheadline offer'dan", vals["subheadline"] == "-30% chegirma")
        check("cta brifdan", vals["cta_text"] == "Buyurtma bering")
        check("feature'lar extra_notes'dan", vals["feature_1"] == "Bepul yetkazib berish" and vals["feature_3"] == "chinakam charm" and vals["feature_4"] == "")
        vals2 = creative_studio.placeholder_values(full_ctx, {"offer_text": "yo'q"}, creative_templates.get_template("bold_sale"))
        check("'yo'q' -> offer_text standart AKSIYA, cta shablondan", vals2["offer_text"] == "AKSIYA" and vals2["cta_text"] == "Hoziroq xarid qiling")
        check("phone/phone_line profildan", vals["phone"] == "+998 90 000 00 00" and vals["phone_line"] == "Tel: +998 90 000 00 00")
        check("shablonsiz CTA zaxirasi 'Batafsil' EMAS", creative_studio.placeholder_values(full_ctx, {}, None)["cta_text"] == creative_studio.FALLBACK_CTA != "Batafsil")
        check("hech bir shablon default_cta 'Batafsil' emas", all(t.get("default_cta") != "Batafsil" for t in creative_templates.CREATIVE_TEMPLATES))
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

        # Shablonsiz (standart qatlamlar) -- telefon qatlami profildan
        a_def = creative_studio.create_draft_asset(session, c, None)
        with mock.patch.object(creative_studio, "_openai_request", return_value=_ok_openai_response()):
            creative_studio.generate_base_image(session, a_def, c, plan)
        check("standart qatlamlarda telefon qatlami (profil raqami)", any(l["id"] == "phone" and l["text"] == "Tel: +998 90 000 00 00" and not l.get("hidden") for l in a_def.get_layers()))
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


# ---------------------------------------------------------------------------
# 11) Logotip fonini avtomatik kesish (2026-09)
# ---------------------------------------------------------------------------
def _wordmark_png(bg, fg, fmt="PNG", size=(400, 140)):
    from PIL import ImageDraw, ImageFont
    im = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(im)
    d.text((20, 30), "BREND", font=ImageFont.truetype(str(creative_studio.FONT_BOLD), 72), fill=fg)
    buf = io.BytesIO()
    im.save(buf, format=fmt)
    return buf.getvalue()


def test_logo_background_removal():
    session = db_module.get_session()
    try:
        c = _company(session, "Logo BG Co")
        d = creative_studio.BRAND_ROOT / str(c.id)
        for name, data, fname, ct in (("oq fon PNG", _wordmark_png((255, 255, 255), (20, 20, 20)), "logo.png", "image/png"),
                                      ("qora fon PNG", _wordmark_png((0, 0, 0), (245, 245, 245)), "logo.png", "image/png"),
                                      ("oq fon JPEG", _wordmark_png((255, 255, 255), (200, 30, 30), fmt="JPEG"), "logo.jpg", "image/jpeg")):
            kit = creative_studio.save_brand_logo(session, c.id, data, fname, ct)
            used = creative_studio.brand_logo_file_path(kit)
            clean = d / creative_studio.LOGO_CLEAN_NAME
            check(f"{name}: logo_clean.png yaratildi va render shu faylni oladi", clean.exists() and used == clean and not (d / creative_studio.LOGO_CLEAN_SKIP).exists())
            with Image.open(clean) as im:
                im = im.convert("RGBA")
                corners = [im.getpixel(p)[3] for p in ((0, 0), (im.width - 1, 0), (0, im.height - 1), (im.width - 1, im.height - 1))]
                center_alpha = im.getchannel("A").getbbox()
                check(f"{name}: burchaklar shaffof (alpha=0), belgi saqlangan", all(a == 0 for a in corners) and center_alpha is not None)
                # Harflar (fon rangidan uzoq) to'liq ko'rinadi
                hist = im.getchannel("A").histogram()
                check(f"{name}: matn piksellari to'liq alpha (255)", hist[255] > 500)
            check(f"{name}: asl fayl ham saqlanadi", creative_studio.brand_logo_original_path(kit).exists() and creative_studio.brand_logo_original_path(kit).name == fname)
        # Allaqachon shaffof PNG -- tegilmaydi, skip marker
        kit = creative_studio.save_brand_logo(session, c.id, _rgba_png_bytes(), "logo.png", "image/png")
        check("shaffof PNG -> tozalanmaydi (skip marker), asl fayl ishlatiladi",
              not (d / creative_studio.LOGO_CLEAN_NAME).exists() and (d / creative_studio.LOGO_CLEAN_SKIP).exists()
              and creative_studio.brand_logo_file_path(kit).name == "logo.png")
        # remove_logo_background: gradient/foto (chekka bir xil emas) -> tegilmaydi
        grad = creative_studio.background_image({"type": "gradient", "colors": ["#000000", "#FFFFFF"], "direction": "horizontal"}, (200, 100))
        _out, changed = creative_studio.remove_logo_background(grad)
        check("chekkasi bir xil bo'lmagan rasm (gradient) -> tegilmaydi", changed is False)
        # Bir rangli rasm (hamma narsa fon) -> xavfsiz: tegilmaydi
        _out, changed = creative_studio.remove_logo_background(Image.new("RGB", (50, 50), (255, 255, 255)))
        check("bir rangli rasm -> tegilmaydi", changed is False)
        # Och-kulrang detal (fon rangiga yaqin, lekin T1 dan uzoq) saqlanadi
        from PIL import ImageDraw
        im = Image.new("RGB", (300, 120), (255, 255, 255))
        ImageDraw.Draw(im).rectangle((20, 40, 280, 80), fill=(190, 190, 190))
        out, changed = creative_studio.remove_logo_background(im)
        check("fon rangiga yaqinroq (kulrang) detal saqlanadi", changed and out.getpixel((150, 60))[3] == 255 and out.getpixel((5, 5))[3] == 0)

        # Self-heal: ESKI yuklangan (tozalanmagan) logotip -- birinchi o'qishda o'zi tozalanadi
        for f in d.glob("logo_clean.*"):
            f.unlink()
        (d / "logo.png").write_bytes(_wordmark_png((255, 255, 255), (0, 0, 0)))
        kit = creative_studio.get_brand_kit(session, c.id)
        used = creative_studio.brand_logo_file_path(kit)
        check("eski logotip self-heal: birinchi o'qishda logo_clean.png yaratildi", used.name == creative_studio.LOGO_CLEAN_NAME and used.exists())
        with Image.open(used) as im:
            check("self-heal natijasi shaffof", im.convert("RGBA").getpixel((0, 0))[3] == 0)
        # Render: logotip zonasida fon rangi (oq quti) YO'Q, faqat harflar
        base_path = Path(_TMPDIR) / "base_logo_bg.png"
        base_path.write_bytes(_png_bytes(600, 600, (40, 120, 60)))
        out_path = Path(_TMPDIR) / "out_logo_bg.png"
        creative_studio.render_composite(base_path, [{"id": "logo", "type": "logo", "x": 0.6, "y": 0.05, "w": 0.35, "h": 0.15, "align": "right"}], kit, out_path, target_size=(600, 600))
        with Image.open(out_path) as im:
            region = im.convert("RGB").crop((360, 30, 570, 120))
            whites = sum(1 for p in region.getdata() if p[0] > 240 and p[1] > 240 and p[2] > 240)
            blacks = sum(1 for p in region.getdata() if p[0] < 40 and p[1] < 40 and p[2] < 40)
            check("renderda oq quti yo'q, qora harflar bor", whites < 30 and blacks > 100)
        # Yangi logotip yuklansa eski clean/skip o'chadi
        creative_studio.save_brand_logo(session, c.id, _rgba_png_bytes(), "logo.png", "image/png")
        check("yangi yuklashda eski logo_clean.png o'chirildi", not (d / creative_studio.LOGO_CLEAN_NAME).exists())
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 12) AI kopirayter + telefon (2026-09)
# ---------------------------------------------------------------------------
def _copy_resp(content, status=200):
    return _FakeResp(status, {"choices": [{"message": {"content": content}}]})


def test_ai_copywriter_and_phone():
    import company_context
    session = db_module.get_session()
    try:
        c = _company(session, "Armatura Co", full_profile=False, phone=None)
        ctx = company_context.build_company_context(c, session)
        raw = {"focus": "armatura sotishimiz kerak", "offer_text": "yo'q", "phone": "+998 90 123 45 67"}
        good = json.dumps({"headline": "Sifatli armatura — zavod narxida", "subheadline": "Toshkent bo'ylab yetkazib beramiz", "cta_text": "Narxini bilib oling"}, ensure_ascii=False)
        with mock.patch.object(creative_studio, "_request_ad_copy", return_value=_copy_resp(good)) as req:
            vals = creative_studio.placeholder_values(ctx, raw, None)
        check("AI matn: chat/completions bir marta, JSON rejimi, arzon model", req.call_count == 1 and req.call_args.args[0]["response_format"] == {"type": "json_object"} and req.call_args.args[0]["model"] == creative_studio.OPENAI_TEXT_MODEL and req.call_args.args[0]["max_tokens"] <= 300)
        check("AI matn: promptda xom javob + telefon + kompaniya", "armatura sotishimiz kerak" in req.call_args.args[0]["messages"][1]["content"] and "+998 90 123 45 67" in req.call_args.args[0]["messages"][1]["content"] and "Armatura Co" in req.call_args.args[0]["messages"][1]["content"])
        check("AI matn: sarlavha xom javob EMAS, AI natijasi", vals["headline"] == "Sifatli armatura — zavod narxida" and vals["subheadline"] == "Toshkent bo'ylab yetkazib beramiz")
        check("AI matn: CTA kontekstga mos (Batafsil emas)", vals["cta_text"] == "Narxini bilib oling")
        check("AI matn: telefon qiymatlari brifdan", vals["phone"] == "+998 90 123 45 67" and vals["phone_line"] == "Tel: +998 90 123 45 67")
        # cta_preference bo'lsa -- AI CTA'si emas, mijozniki
        with mock.patch.object(creative_studio, "_request_ad_copy", return_value=_copy_resp(good)):
            vals_pref = creative_studio.placeholder_values(ctx, dict(raw, cta_preference="Qo'ng'iroq qiling"), None)
        check("cta_preference AI'dan ustun", vals_pref["cta_text"] == "Qo'ng'iroq qiling" and vals_pref["headline"] == "Sifatli armatura — zavod narxida")
        # ``` bilan o'ralgan JSON ham o'qiladi
        with mock.patch.object(creative_studio, "_request_ad_copy", return_value=_copy_resp("```json\n" + good + "\n```")):
            check("```json``` o'rami tozalanadi", creative_studio.placeholder_values(ctx, raw, None)["headline"] == "Sifatli armatura — zavod narxida")
        # Xato holatlari -> zaxira (xom, lekin crash yo'q), CTA "Bog'laning"
        for label, patch_kwargs in (("tarmoq xatosi", {"side_effect": RuntimeError("boom")}),
                                    ("HTTP 500", {"return_value": _FakeResp(500, {"error": {"message": "RAW"}})}),
                                    ("kredit tugagan 429", {"return_value": _FakeResp(429, {"error": {"code": "insufficient_quota", "message": "quota"}})}),
                                    ("buzuq JSON", {"return_value": _copy_resp("bu json emas")}),
                                    ("bo'sh JSON", {"return_value": _copy_resp("{}")})):
            with mock.patch.object(creative_studio, "_request_ad_copy", **patch_kwargs):
                try:
                    v = creative_studio.placeholder_values(ctx, raw, None)
                    check(f"AI matn {label} -> zaxira, crash yo'q, CTA='Bog\'laning'", v["headline"] == "armatura sotishimiz kerak" and v["cta_text"] == "Bog'laning")
                except Exception as e:  # noqa: BLE001
                    check(f"AI matn {label} -> zaxira, crash yo'q", False)
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}), mock.patch.object(creative_studio, "_request_ad_copy") as req:
            creative_studio.placeholder_values(ctx, raw, None)
            check("OPENAI_API_KEY yo'q -> AI matn chaqirilmaydi", req.call_count == 0)
        with mock.patch.object(creative_studio, "_request_ad_copy") as req:
            creative_studio.placeholder_values(ctx, raw, None, use_ai=False)
            check("use_ai=False -> chaqirilmaydi (shablon rejimi)", req.call_count == 0)
        # Juda uzun AI matn qisqartiriladi
        with mock.patch.object(creative_studio, "_request_ad_copy", return_value=_copy_resp(json.dumps({"headline": "x" * 200, "cta_text": "y" * 100}))):
            v = creative_studio.placeholder_values(ctx, raw, None)
            check("AI matn uzunligi chegaralanadi", len(v["headline"]) <= 48 and len(v["cta_text"]) <= 32)

        # To'liq oqim: telefon so'raladi, javob profilga yoziladi, generatsiyada AI matn qatlamga tushadi
        a = creative_studio.create_draft_asset(session, c, None)
        missing = json.loads(a.missing_fields_json)
        check("draft: focus + phone majburiy (profil bo'sh)", "focus" in missing and "phone" in missing)
        creative_studio.submit_brief_answer(session, a, "focus", "armatura sotishimiz kerak")
        try:
            creative_studio.submit_brief_answer(session, a, "phone", "raqam yo'q")
            check("noto'g'ri telefon rad etiladi", False)
        except creative_studio.CreativeError:
            check("noto'g'ri telefon rad etiladi", True)
        creative_studio.submit_brief_answer(session, a, "phone", "+998 90 123 45 67")
        check("telefon javobi profilga ham yozildi (bo'sh edi)", c.phone == "+998 90 123 45 67")
        check("telefondan keyin missing bo'sh", json.loads(a.missing_fields_json) == [])
        with mock.patch.object(creative_studio, "_openai_request", return_value=_ok_openai_response()) as img_req, \
                mock.patch.object(creative_studio, "_request_ad_copy", return_value=_copy_resp(good)) as copy_req:
            creative_studio.generate_base_image(session, a, c, plans.PLANS["business"])
        check("generatsiya: rasm 1 marta, AI matn 1 marta", img_req.call_count == 1 and copy_req.call_count == 1)
        layers = {l["id"]: l for l in a.get_layers()}
        check("qatlamlar: AI sarlavha, AI CTA, telefon", layers["headline"]["text"] == "Sifatli armatura — zavod narxida" and layers["cta_badge"]["text"] == "Narxini bilib oling" and layers["phone"]["text"] == "Tel: +998 90 123 45 67")
        check("title AI sarlavhadan", a.title == "Sifatli armatura — zavod narxida")
        # Ikkinchi kompaniya: telefon profilda bor -> so'ralmaydi, ustidan yozilmaydi
        c2 = _company(session, "Phone Co", full_profile=False)
        a2 = creative_studio.create_draft_asset(session, c2, None)
        check("profilda telefon bor -> so'ralmaydi", "phone" not in json.loads(a2.missing_fields_json) and "focus" in json.loads(a2.missing_fields_json))
        creative_studio.submit_brief_answer(session, a2, "phone", "+998 93 999 99 99")
        check("mavjud profil telefoni ustidan yozilmaydi", c2.phone == "+998 90 000 00 00")
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 13) (2026-09) Foydalanuvchi shikoyati skrinshot bilan: "dizayn juda oddiy,
#     har safar bir xil, narx/xususiyat to'liq ko'rsatilmayapti" -- AI
#     kopirayter ENDI price_text/features ham qaytaradi (faqat asoslangan
#     bo'lsa), va shablonsiz yo'l `select_default_layout()` orqali BOY,
#     XILMA-XIL kompozitsiya tanlaydi.
# ---------------------------------------------------------------------------
def test_price_features_and_default_layout_variants():
    import company_context
    session = db_module.get_session()
    try:
        c = _company(session, "Sement Co", full_profile=True)
        ctx = company_context.build_company_context(c, session)
        raw = {"focus": "Sement", "offer_text": "yo'q"}

        # --- narx/xususiyat REAL ma'lumotga asoslangan bo'lsa qaytadi
        good_full = json.dumps({
            "headline": "Sifatli sement", "subheadline": "Ishonchli yetkazib berish", "cta_text": "Buyurtma bering",
            "price_text": "29.000 so'mdan boshlab",
            "features": ["Yuqori sifat", "Tez yetkazib berish", "Rasmiy kafolat"],
        }, ensure_ascii=False)
        with mock.patch.object(creative_studio, "_request_ad_copy", return_value=_copy_resp(good_full)):
            vals = creative_studio.placeholder_values(ctx, raw, None)
        check("AI narx: asoslangan bo'lsa qaytadi", vals["price_text"] == "29.000 so'mdan boshlab")
        check("AI xususiyatlar: asoslangan bo'lsa qaytadi (3 ta)",
              vals["feature_1"] == "Yuqori sifat" and vals["feature_2"] == "Tez yetkazib berish" and vals["feature_3"] == "Rasmiy kafolat")
        check("AI xususiyatlar zaxirani TO'LIQ almashtiradi (feature_4 bo'sh)", vals["feature_4"] == "")

        # --- hech narsaga asoslanmasa -- BO'SH (o'ylab topilmaydi)
        c_empty = _company(session, "Bosh Co", full_profile=False, phone=None)
        ctx_empty = company_context.build_company_context(c_empty, session)
        good_empty = json.dumps({
            "headline": "Sifatli xizmat", "subheadline": "Ishonchli hamkor", "cta_text": "Bog'laning",
            "price_text": "", "features": [],
        }, ensure_ascii=False)
        with mock.patch.object(creative_studio, "_request_ad_copy", return_value=_copy_resp(good_empty)):
            vals_empty = creative_studio.placeholder_values(ctx_empty, {"focus": "Xizmat"}, None)
        check("AI narx ma'lumoti yo'q -> bo'sh qaytadi (o'ylab topmaydi)", vals_empty["price_text"] == "")
        check("AI xususiyat asosi yo'q -> bo'sh qaytadi (zaxira ham bo'sh)", vals_empty["feature_1"] == "")

        # --- qisman/buzuq JSON -- mavjud to'g'ri maydonlar baribir parse qilinadi
        partial = json.dumps({
            "headline": "Yaxshi taklif", "price_text": "50.000 so'mdan",
            "features": ["a" * 40, 123, "Tez"],  # 2-element noto'g'ri tur -- tashlab yuboriladi
        }, ensure_ascii=False)
        with mock.patch.object(creative_studio, "_request_ad_copy", return_value=_copy_resp(partial)):
            vals_partial = creative_studio.placeholder_values(ctx, raw, None)
        check("qisman JSON: mavjud headline/price_text parse qilindi", vals_partial["headline"] == "Yaxshi taklif" and vals_partial["price_text"] == "50.000 so'mdan")
        check("qisman JSON: features ichidagi noto'g'ri element tashlab yuboriladi, qolgani saqlanadi",
              vals_partial["feature_1"] == ("a" * 40)[:28] and vals_partial["feature_2"] == "Tez")

        # -----------------------------------------------------------------
        # select_default_layout()
        # -----------------------------------------------------------------
        minimal_values = {
            "headline": "Sifatli sement", "subheadline": "Ishonchli yetkazib berish", "cta_text": "Buyurtma bering",
            "offer_text": "AKSIYA", "price_text": "", "brand_name": "Dunyo Bunyo", "quote_text": "",
            "phone": "+998901234567", "phone_line": "Tel: +998901234567",
            "feature_1": "", "feature_2": "", "feature_3": "", "feature_4": "",
        }
        layers_minimal = creative_studio.select_default_layout(minimal_values, 1)
        has_headline = any(l["id"] == "headline" for l in layers_minimal)
        has_logo = any(l["type"] == "logo" for l in layers_minimal)
        no_price_ref = not any("{{price_text}}" in (l.get("text") or "") for l in layers_minimal)
        no_feature_ref = not any("{{feature_" in (l.get("text") or "") for l in layers_minimal)
        check("select_default_layout: narx/xususiyat yo'q -> bazaviy variant (bo'sh element ko'rsatilmaydi)",
              has_headline and has_logo and no_price_ref and no_feature_ref)

        full_values = dict(
            minimal_values, price_text="29.000 so'mdan boshlab", offer_text="-10% chegirma",
            feature_1="Yuqori sifat", feature_2="Tez yetkazib berish", feature_3="Rasmiy kafolat",
            quote_text="Mijozlarimiz doim mamnun",
        )
        layers_full = creative_studio.select_default_layout(full_values, 1)
        has_price_ref = any("{{price_text}}" in (l.get("text") or "") for l in layers_full)
        has_feature_ref = any("{{feature_1}}" in (l.get("text") or "") for l in layers_full)
        check("select_default_layout: to'liq ma'lumotda -- narx+xususiyat ko'rsatadigan BOY variant tanlanadi",
              has_price_ref and has_feature_ref)

        # determinizm: bir xil values + bir xil seed (asset.id) -> bir xil natija
        again1 = creative_studio.select_default_layout(full_values, 42)
        again2 = creative_studio.select_default_layout(full_values, 42)
        check("select_default_layout: bir xil asset id -> bir xil variant (barqaror)", again1 == again2)

        # xilma-xillik: turli asset id -> BIR NECHTA xil variant (to'liq ma'lumot -- 2 ta BOY variant teng)
        seen_full = {tuple(l["id"] for l in creative_studio.select_default_layout(full_values, i)) for i in range(30)}
        check("select_default_layout: to'liq ma'lumotda ham turli asset id -> haqiqiy xilma-xillik", len(seen_full) > 1)

        # xilma-xillik BAZAVIY holatda ham -- aynan foydalanuvchi shikoyati
        # ("har safar bir xil dizayn") shu yerda TO'G'RIDAN-TO'G'RI tekshiriladi.
        seen_min = {tuple(l["id"] for l in creative_studio.select_default_layout(minimal_values, i)) for i in range(30)}
        check("select_default_layout: bazaviy holatda ham xilma-xillik (eski 'doim bir xil' muammo yo'q)", len(seen_min) > 1)

        # har bir variantda logotip VA telefon qatlami bor (regressiya himoyasi)
        all_variant_keys, _b, _r, _c = zip(*creative_studio._LAYOUT_VARIANTS)
        check("kamida 5-6 ta original variant mavjud", len(creative_studio._LAYOUT_VARIANTS) >= 5)
        for key, build, richness, cond in creative_studio._LAYOUT_VARIANTS:
            layers = build(full_values)
            ok = any(l["id"] == "logo" for l in layers) and any(l["id"] == "phone" for l in layers) and any(l["id"] == "headline" for l in layers)
            check(f"variant '{key}': logotip+telefon+headline mavjud", ok)

        # -----------------------------------------------------------------
        # render_composite() -- yangi (boy) variant to'liq render zanjiri
        # -----------------------------------------------------------------
        resolved = creative_studio.resolve_layers(layers_full, full_values)
        with tempfile.TemporaryDirectory() as td:
            base_path = Path(td) / "base.png"
            Image.new("RGB", (1080, 1080), (180, 190, 200)).save(base_path, format="PNG")
            out_path = Path(td) / "final.png"
            creative_studio.render_composite(base_path, resolved, None, out_path, target_size=(1080, 1080))
            check("render_composite: yangi variant xatosiz render qilindi (fayl bor)", out_path.exists() and out_path.stat().st_size > 3000)
            with Image.open(out_path) as img:
                check("render_composite: chiqish o'lchami/format to'g'ri", img.format == "PNG" and img.size == (1080, 1080))
    finally:
        session.close()


def test_brief_agent_flow():
    """2026-09, foydalanuvchi fikri ("savollar bir xil shablon bo'lmasin,
    agent ishlasin"): `creative_studio.start_brief`/`answer_brief`
    (LLM -- `orchestrator._call_agent` mock) -- suhbat saqlanadi, "done"
    bo'lgach `brief_answers_json` ESKI tekis shaklda to'ladi (telefon
    profilga backfill bilan birga) va pastki oqim (`generate_base_image`)
    HECH QANDAY o'zgarishsiz ishlaydi (flat-dict shartnoma saqlangan)."""
    import company_context
    session = db_module.get_session()
    try:
        c = _company(session, "Armatura Co", plan="business", full_profile=False, phone=None)
        ctx = company_context.build_company_context(c, session)
        asset = creative_studio.create_draft_asset(session, c, None)
        check("yangi asset: suhbat hali bo'sh", asset.get_brief_conversation() == [])

        with mock.patch.object(orchestrator, "_call_agent", return_value={"done": False, "question": "Qaysi mahsulotga urg'u berilsin?", "placeholder": "Masalan: armatura"}) as m:
            step = creative_studio.start_brief(session, asset, ctx)
        conv = asset.get_brief_conversation()
        check("start_brief: birinchi savol saqlandi", step["done"] is False and len(conv) == 1 and conv[0]["role"] == "agent" and "mahsulotga" in conv[0]["text"])
        with mock.patch.object(orchestrator, "_call_agent") as m2:
            creative_studio.start_brief(session, asset, ctx)
        check("start_brief idempotent -- ikkinchi chaqiruvda LLM chaqirilmadi", m2.call_count == 0)

        with mock.patch.object(orchestrator, "_call_agent", return_value={"done": False, "question": "Qaysi diametr/turdagi armatura, narxi qancha?", "placeholder": None}):
            step2 = creative_studio.answer_brief(session, asset, "Armatura sotamiz", ctx=ctx)
        conv2 = asset.get_brief_conversation()
        check("answer_brief: user+agent burilish qo'shildi (2 -> 4)", len(conv2) == 3 and conv2[1]["role"] == "user" and conv2[1]["text"] == "Armatura sotamiz")
        check("hali done emas", step2["done"] is False)
        check("brif hali to'liq emas -- missing_fields o'zgarmagan", "focus" in json.loads(asset.missing_fields_json))

        brief = {"focus": "Armatura 12mm, GOST 5781", "offer_text": "-10% chegirma", "cta_preference": "", "style_notes": "", "phone": "+998 90 123 45 67"}
        with mock.patch.object(orchestrator, "_call_agent", return_value={"done": True, "brief": brief}):
            step3 = creative_studio.answer_brief(session, asset, "12mm, GOST 5781, -10% chegirma, +998901234567", ctx=ctx)
        check("done bo'lgach step3 done=True", step3["done"] is True)
        answers = asset.get_brief_answers()
        check("brief_answers_json ESKI tekis shaklda, 5 ta kalit", set(answers.keys()) == {"focus", "offer_text", "cta_preference", "style_notes", "phone"})
        check("focus/phone to'g'ri o'tdi", answers["focus"] == "Armatura 12mm, GOST 5781" and answers["phone"] == "+998 90 123 45 67")
        check("missing_fields_json bo'sh (generatsiyaga tayyor)", json.loads(asset.missing_fields_json) == [])
        check("telefon kompaniya profiliga backfill qilindi", c.phone == "+998 90 123 45 67")
        check("yopilish burilishi qo'shildi", asset.get_brief_conversation()[-1]["role"] == "agent")

        # Pastki oqim (generatsiya) O'ZGARISHSIZ ishlaydi -- flat-dict shartnoma saqlangan
        plan = plans.PLANS["business"]
        with mock.patch.object(creative_studio, "_openai_request", return_value=_ok_openai_response()) as req:
            creative_studio.generate_base_image(session, asset, c, plan)
        check("AI suhbatdan keyin ham generatsiya muvaffaqiyatli", asset.status == "ready" and req.call_count == 1)
        check("headline AI suhbat brifidan (fallback, AI kopirayter offline)", any("Armatura" in (l.get("text") or "") for l in asset.get_layers()))

        # Xavfsizlik to'ri: telefon yo'q kompaniya -- LLM "done" desa ham rad etiladi
        c2 = _company(session, "No Phone Armatura", plan="business", full_profile=False, phone=None)
        ctx2 = company_context.build_company_context(c2, session)
        asset2 = creative_studio.create_draft_asset(session, c2, None)
        with mock.patch.object(orchestrator, "_call_agent", return_value={"done": False, "question": "Mahsulot?", "placeholder": None}):
            creative_studio.start_brief(session, asset2, ctx2)
        with mock.patch.object(orchestrator, "_call_agent", return_value={"done": True, "brief": {"focus": "Armatura", "offer_text": "", "cta_preference": "", "style_notes": "", "phone": ""}}):
            step4 = creative_studio.answer_brief(session, asset2, "Armatura", ctx=ctx2)
        check("qattiq to'r: telefonsiz 'done' rad etildi (web-agent darajasida ham)", step4["done"] is False and "telefon" in step4["question"].lower())
        check("qattiq to'r ishlaganda brief_answers_json TEGILMAYDI", asset2.get_brief_answers() == {})

        # LLM UMUMAN ishlamasa -- suhbat qulflanib qolmaydi
        c3 = _company(session, "LLM Down Co", plan="business", full_profile=False, phone=None)
        ctx3 = company_context.build_company_context(c3, session)
        asset3 = creative_studio.create_draft_asset(session, c3, None)
        with mock.patch.object(orchestrator, "_call_agent", side_effect=orchestrator.AgentUnavailableError("down")):
            step5 = creative_studio.start_brief(session, asset3, ctx3)
        check("LLM ishlamasa ham start_brief ishlaydi (ESKI statik savol)", step5["done"] is False and step5["question"] == creative_studio._BRIEF_BY_KEY["focus"][1])
    finally:
        session.close()


def test_chat_edit_layers():
    """2026-09, foydalanuvchi so'rovi: "logotipni kattaroq qil" kabi
    buyruqni OpenAI'ni QAYTA chaqirmasdan qo'llash (kvota sarflanmaydi,
    faqat Pillow qayta chizadi). LLM -- `orchestrator._call_agent` mock."""
    import company_context
    session = db_module.get_session()
    try:
        c = _company(session, "Layer Chat Co", plan="business")
        plan = plans.PLANS["business"]
        a = creative_studio.create_draft_asset(session, c, None, template_key="bold_sale", aspect="1:1")
        creative_studio.submit_brief_answer(session, a, "focus", "Test mahsulot")
        with mock.patch.object(creative_studio, "_openai_request", return_value=_ok_openai_response()):
            creative_studio.generate_base_image(session, a, c, plan)
        check("chat_edit_layers testi uchun asset tayyor (ready)", a.status == "ready")
        final = creative_studio.export_png_path(a)
        before_bytes = final.read_bytes()
        logo_id = next(l["id"] for l in a.get_layers() if l["type"] == "logo")
        headline_id = next(l["id"] for l in a.get_layers() if l["id"] == "headline")
        ctx = company_context.build_company_context(c, session)

        # Happy path: valid o'zgarishlar (logotip hajmi + ko'rinadigan matn
        # rangi -- piksellar aniq o'zgarishi uchun) + rad etilishi kerak
        # bo'lganlar (strukturaviy field, noma'lum layer_id) aralash.
        resp = {
            "changes": [
                {"layer_id": logo_id, "field": "w", "value": 0.4},
                {"layer_id": logo_id, "field": "h", "value": 0.2},
                {"layer_id": logo_id, "field": "type", "value": "text"},    # strukturaviy -- rad etilishi kerak
                {"layer_id": "yoq_qatlam", "field": "text", "value": "x"},  # noma'lum layer_id -- rad etilishi kerak
                {"layer_id": headline_id, "field": "color", "value": "#00FF00"},
            ],
            "reply": "Logotip kattalashtirildi.",
        }
        with mock.patch.object(orchestrator, "_call_agent", return_value=resp) as m, \
             mock.patch.object(creative_studio, "check_quota") as qm, \
             mock.patch.object(creative_studio, "increment_usage") as um, \
             mock.patch.object(creative_studio, "_openai_request") as img_req:
            asset2, reply = creative_studio.chat_edit_layers(session, a, ctx, "logotipni kattaroq qil")
        check("chat_edit_layers: LLM 1 marta chaqirildi", m.call_count == 1)
        check("chat_edit_layers: reply qaytdi", reply == "Logotip kattalashtirildi.")
        new_logo = next(l for l in a.get_layers() if l["id"] == logo_id)
        new_headline = next(l for l in a.get_layers() if l["id"] == headline_id)
        check("ruxsat etilgan fieldlar (w/h) qo'llandi", abs(new_logo["w"] - 0.4) < 0.001 and abs(new_logo["h"] - 0.2) < 0.001)
        check("ruxsat etilgan rang o'zgarishi qo'llandi", new_headline["color"] == "#00FF00")
        check("strukturaviy field (type) O'ZGARMADI", new_logo["type"] == "logo")
        check("asset qayta render qilindi (final fayl o'zgardi)", final.read_bytes() != before_bytes)
        check("OpenAI rasm-generatsiyasi CHAQIRILMADI (kvota sarflanmaydi)", img_req.call_count == 0 and qm.call_count == 0 and um.call_count == 0)

        # LLM ishlamasa -- qatlamlar TEGILMAYDI, friendly xato
        snapshot = json.loads(json.dumps(a.get_layers()))
        with mock.patch.object(orchestrator, "_call_agent", side_effect=orchestrator.AgentUnavailableError("down")):
            try:
                creative_studio.chat_edit_layers(session, a, ctx, "fonni qora qil")
                check("LLM ishlamasa -> CreativeError", False)
            except creative_studio.CreativeError as e:
                check("LLM ishlamasa -> friendly xato (xom matn yo'q)", "down" not in str(e))
        check("LLM ishlamasa -> qatlamlar BAYT-BAYTIGA o'zgarmadi", a.get_layers() == snapshot)

        # Barcha o'zgarishlar rad etilsa (noma'lum layer_id) -> CreativeError, qatlamlar o'zgarmaydi
        with mock.patch.object(orchestrator, "_call_agent", return_value={"changes": [{"layer_id": "yoq", "field": "text", "value": "x"}], "reply": "Mos qatlam topilmadi."}):
            try:
                creative_studio.chat_edit_layers(session, a, ctx, "nimadir noaniq narsani o'zgartir")
                check("hech narsa qo'llanmasa -> CreativeError", False)
            except creative_studio.CreativeError as e:
                check("hech narsa qo'llanmasa -> reply xato sifatida", "topilmadi" in str(e).lower())
        check("hech narsa qo'llanmasa -> qatlamlar o'zgarmadi", a.get_layers() == snapshot)

        # Bo'sh buyruq
        try:
            creative_studio.chat_edit_layers(session, a, ctx, "   ")
            check("bo'sh buyruq -> CreativeError", False)
        except creative_studio.CreativeError:
            check("bo'sh buyruq -> CreativeError", True)
    finally:
        session.close()


def test_r2_storage_backend_wired():
    """2026-09, R2 doimiy saqlash: logotip/kreativ base+final rasmlari
    `storage_backend.upload_file`ga to'g'ri key bilan uzatilishi,
    `brand_logo_file_path`/`brand_logo_original_path`/`export_png_path`/
    `asset_base_image_path` esa `storage_backend.ensure_local` orqali
    o'tishi (R2 o'chiq bo'lganda -- bugungidek, download urinmasdan)."""
    session = db_module.get_session()
    try:
        c = _company(session, "R2 Co", plan="trial")

        # --- brend logotip: yuklashda R2'ga yuklanadi ---
        with mock.patch.object(creative_studio.storage_backend, "upload_file", return_value=True) as up:
            kit = creative_studio.save_brand_logo(session, c.id, _rgba_png_bytes(), "logo.png", "image/png")
        check("save_brand_logo -> storage_backend.upload_file chaqirildi", up.call_count == 1)
        up_args = up.call_args[0]
        check("logotip -> to'g'ri R2 kaliti", up_args[1] == f"brand_kit/{kit.logo_storage_path}")
        check("logotip -> lokal fayl bilan chaqirildi", up_args[0].exists() and up_args[0].name == Path(kit.logo_storage_path).name)

        # brand_logo_file_path/original_path -- R2 o'chiqda bugungidek
        # (download urinmasdan, mavjud lokal faylni qaytaradi).
        with mock.patch.object(creative_studio.storage_backend, "download_file") as dl:
            p1 = creative_studio.brand_logo_file_path(kit)
            p2 = creative_studio.brand_logo_original_path(kit)
            check("brand_logo_file_path R2 o'chiqda download urinmaydi", dl.call_count == 0)
            check("brand_logo_original_path R2 o'chiqda to'g'ri fayl", p2.exists())
            check("brand_logo_file_path natija bor (logo_clean yoki asl)", p1 is not None and p1.exists())

        # Fayl "yo'qolgan" (yangi deploy simulyatsiyasi) + R2 yoqilgan ->
        # ensure_local orqali download'ga urinishi kerak.
        original = creative_studio.BRAND_ROOT / kit.logo_storage_path
        for f in original.parent.glob("logo_clean.*"):
            f.unlink()
        original.unlink()
        with mock.patch.object(creative_studio.storage_backend, "enabled", return_value=True), \
             mock.patch.object(creative_studio.storage_backend, "download_file", return_value=False) as dl2:
            creative_studio.brand_logo_original_path(kit)
            check("brand_logo_original_path fayl yo'qolganda+R2 yoqilganda download urinadi", dl2.call_count == 1)
            check("download to'g'ri key bilan", dl2.call_args[0][0] == f"brand_kit/{kit.logo_storage_path}")
        # Faylni tiklab qo'yamiz (keyingi testlar buzilmasin).
        creative_studio.save_brand_logo(session, c.id, _rgba_png_bytes(), "logo.png", "image/png")

        # --- shablondan yaratilgan kreativ: base.png R2'ga yuklanadi ---
        with mock.patch.object(creative_studio.storage_backend, "upload_file", return_value=True) as up2, \
             mock.patch.object(creative_studio, "_openai_request") as req:
            asset = creative_studio.create_from_template(session, c, None, "luxury_dark")
        check("create_from_template: OpenAI chaqirilmagan", req.call_count == 0)
        keys_uploaded = [call.args[1] for call in up2.call_args_list]
        check("base.png R2'ga yuklandi", f"creative_studio/{asset.base_image_storage_path}" in keys_uploaded)
        check("final.png R2'ga yuklandi", f"creative_studio/{asset.final_storage_path}" in keys_uploaded)

        # export_png_path -- R2 o'chiqda download urinmasdan mavjud faylni qaytaradi.
        with mock.patch.object(creative_studio.storage_backend, "download_file") as dl3:
            out = creative_studio.export_png_path(asset)
            check("export_png_path R2 o'chiqda download urinmaydi", dl3.call_count == 0 and out.exists())

        # asset_base_image_path -- xuddi shunday, va bo'sh asset -> None.
        check("asset_base_image_path to'g'ri yo'l qaytaradi", creative_studio.asset_base_image_path(asset) == creative_studio.CREATIVE_ROOT / asset.base_image_storage_path)
        check("asset_base_image_path bo'sh asset -> None", creative_studio.asset_base_image_path(None) is None)

        # final.png "yo'qolgan" + R2 yoqilgan -> export_png_path ensure_local orqali urinadi.
        final_path = creative_studio.CREATIVE_ROOT / asset.final_storage_path
        final_path.unlink()
        with mock.patch.object(creative_studio.storage_backend, "enabled", return_value=True), \
             mock.patch.object(creative_studio.storage_backend, "download_file", return_value=False) as dl4:
            try:
                creative_studio.export_png_path(asset)
                check("export_png_path noto'g'ri holatda xato berishi kerak edi", False)
            except creative_studio.CreativeError:
                check("final.png yo'qolganda+R2 yoqilganda download urinadi", dl4.call_count == 1)
                check("download to'g'ri key bilan (final.png)", dl4.call_args[0][0] == f"creative_studio/{asset.final_storage_path}")

        # delete_asset -- R2 prefiksini ham o'chirishga urinishi kerak.
        with mock.patch.object(creative_studio.storage_backend, "delete_prefix", return_value=True) as delp:
            creative_studio.delete_asset(session, asset)
        check("delete_asset -> storage_backend.delete_prefix chaqirildi", delp.call_count == 1)
        check("delete_prefix to'g'ri prefiks bilan", delp.call_args[0][0] == f"creative_studio/{c.id}/{asset.id}/")
    finally:
        session.close()


test_templates()
test_missing_questions_and_placeholders()
test_quota()
test_generate_flow()
test_render_composite_and_brand_kit()
test_create_from_template_and_export()
test_multitenant_isolation()
test_plans()
test_logo_background_removal()
test_ai_copywriter_and_phone()
test_price_features_and_default_layout_variants()
test_brief_agent_flow()
test_chat_edit_layers()
test_r2_storage_backend_wired()

print()
if failures:
    print(f"MUVAFFAQIYATSIZ: {len(failures)} ta test o'tmadi:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("BARCHA TESTLAR O'TDI (creative_studio)")
