"""test_creative_web_offline.py — Kreativ studiya WEB QATLAMI (2026-09,
foydalanuvchi so'rovi: "AI reklama rasmini yaratsin -- avval savol-javob,
keyin generatsiya; 20 ta shablon; brauzerda tahrirlash; PNG/PDF; Avtopilot
media bosqichida tanlash").

TARMOQSIZ (offline): OpenAI (`creative_studio._openai_request`), Meta
aktivlari (`meta_publish.get_meta_assets`) -- mock. Tekshiriladi:
  1. /kreativ login talab qiladi; faqat o'z kompaniyasining kreativlari.
  2. /kreativ/yangi POST (AI) -> collecting_brief + redirect; template_key
     saqlanadi; shablon rejimi (OpenAI'siz) -> darhol ready.
  3. /brief -- javob saqlanadi, majburiy savoldan keyin missing bo'sh.
  4. /generate (OpenAI mock) -> ready + image_url; rasm.png beriladi.
  5. Sinov (trial, limit=0): /generate friendly 400, OpenAI chaqirilmaydi.
  6. /layers POST -> saqlanadi, final rasm qayta chiziladi (fayl o'zgaradi).
  7. eksport.png / eksport.pdf -- Content-Type + hajm > 0.
  8. Multi-tenant: B kompaniyasi A kreativiga 404 (barcha marshrutlar).
  9. /avtopilot/<id>/media/from-kreativ -> CampaignDraftMedia +
     creative_asset_id; boshqa kompaniya asset'i 404; admin emas 403.
 10. /sozlamalar/brend GET/POST -- logo + ranglar saqlanadi va qaytadi.
 11. Sidebar'da "Kreativ studiya" bandi; Avtopilot sahifasida galereya URL'i.
 12. (2026-09) Telefon savoli web oqimida (profilda yo'q -> majburiy,
     javob profilga yoziladi); /kreativ sahifasida INLINE shablonlar
     (bir bosishda "Shundan boshlash"); muharrir JSON'ida `target`.
 13. (2026-09) POST /kreativ/<id>/target-yarat ("Targetga ochish"): AI
     planner mock -> yangi CampaignDraft + kreativ media sifatida
     biriktirilgan + redirect /avtopilot/<id>; maqsad/byudjet yetishmasa
     -> wizard'ga (creative_asset_id bilan) yo'naltiradi, qoralama
     YARATILMAYDI; wizard POST creative_asset_id bilan ham biriktiradi;
     tayyor bo'lmagan kreativ / boshqa kompaniya / menejer -> xato.

Ishga tushirish:
    cd app && python3 scripts/test_creative_web_offline.py
"""

import io
import os
import re
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
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMPDIR, 'test_creative_web.db')}"

from PIL import Image  # noqa: E402

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402
import meta_publish  # noqa: E402
import campaign_draft  # noqa: E402
import campaign_media  # noqa: E402
import creative_studio  # noqa: E402
import business_profile  # noqa: E402
import orchestrator  # noqa: E402
import meta_api  # noqa: E402
import ai_campaign_planner  # noqa: E402

app_module.app.config["TESTING"] = True
app_module.app.config["WTF_CSRF_ENABLED"] = False
db_module.init_db()
campaign_media.MEDIA_ROOT = Path(_TMPDIR) / "ad_media"
creative_studio.CREATIVE_ROOT = Path(_TMPDIR) / "creative_studio"
creative_studio.BRAND_ROOT = Path(_TMPDIR) / "brand_kit"

failures = []

# AI kopirayter (chat/completions) OFFLINE -- zaxira matn ishlatiladi.
mock.patch.object(creative_studio, "_request_ad_copy", side_effect=RuntimeError("offline")).start()


def check(name, cond):
    print(("OK: " if cond else "FAIL: ") + name)
    if not cond:
        failures.append(name)


def _png_bytes(w=64, h=64, color=(200, 30, 30)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


class _FakeResp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def _ok_openai_response():
    return _FakeResp(200, {"created": 1700000000, "data": [{"b64_json": base64.b64encode(_png_bytes(256, 256, (30, 60, 200))).decode()}]})


EMPTY_ASSETS = {"ad_account": None, "pages": [], "instagram_accounts": [], "pixels": [], "custom_audiences": [], "lead_forms": [],
                "recent_images": [], "campaigns": [], "errors": [], "pixel_id": None, "page_id": None, "ig_business_id": None}


def _assets_mock():
    return mock.patch.object(meta_publish, "get_meta_assets", lambda company, use_cache=True: json.loads(json.dumps(EMPTY_ASSETS)))


# ---------------------------------------------------------------------------
# Kompaniyalar / foydalanuvchilar
# ---------------------------------------------------------------------------
_session = db_module.get_session()
try:
    # A: mahsulot maydoni BO'SH -> 'focus' savoli majburiy (brif oqimi ishga tushadi)
    A = db_module.Company(name="Nur Mebel", plan="business", is_active=True, business_category="furniture")
    A.business_profile_answers = business_profile.serialize_business_profile_answers({
        "target_audience": "25-45 yosh ayollar, Toshkent shahri", "price_range": "o'rta",
    })
    B = db_module.Company(name="Boshqa MChJ", plan="business", is_active=True)
    T = db_module.Company(name="Sinov MChJ", plan="trial", is_active=True, business_category="clothing_fashion", phone="+998 71 200 00 00")
    T.business_profile_answers = business_profile.serialize_business_profile_answers({"product_or_service": "Ayollar kiyimi"})
    _session.add_all([A, B, T])
    _session.commit()
    A_ID, B_ID, T_ID = A.id, B.id, T.id
    users = [
        db_module.Manager(username="cs_admin_a", full_name="Admin A", role="admin", company_id=A_ID),
        db_module.Manager(username="cs_manager_a", full_name="Menejer A", role="manager", company_id=A_ID,
                          allowed_modules=json.dumps(["dashboard", "leads", "target"])),
        db_module.Manager(username="cs_admin_b", full_name="Admin B", role="admin", company_id=B_ID),
        db_module.Manager(username="cs_admin_t", full_name="Admin T", role="admin", company_id=T_ID),
    ]
    for u in users:
        u.set_password("parol123")
    _session.add_all(users)
    _session.commit()
finally:
    _session.close()


def _client(username):
    c = app_module.app.test_client()
    r = c.post("/login", data={"username": username, "password": "parol123"})
    assert r.status_code in (302, 303), r.status_code
    return c


def _asset_json(html):
    m = re.search(r'<script id="cs-data" type="application/json">(.*?)</script>', html, re.S)
    assert m, "cs-data oroli topilmadi"
    return json.loads(m.group(1))


def _row(model, row_id):
    s = db_module.get_session()
    try:
        with db_module.unscoped():
            d = s.get(model, row_id)
            s.refresh(d)
            s.expunge(d)
            return d
    finally:
        s.close()


admin = _client("cs_admin_a")
ASSET_ID = None
TPL_ASSET_ID = None


# ---------------------------------------------------------------------------
# 1) Login + galereya
# ---------------------------------------------------------------------------
def test_login_required_and_empty_gallery():
    anon = app_module.app.test_client()
    r = anon.get("/kreativ")
    check("anonim /kreativ -> login redirect", r.status_code in (302, 303) and "login" in r.headers.get("Location", ""))
    r = admin.get("/kreativ")
    html = r.get_data(as_text=True).replace("&#39;", "'")
    check("galereya 200 + bo'sh holat tushuntirishi", r.status_code == 200 and "Hali kreativ yo'q" in html)
    check("kvota ko'rsatkichi (business: 0/30)", "0/30 rasm" in html)
    check("sidebar'da Kreativ studiya bandi", 'data-tooltip="Kreativ studiya (AI rasm)"' in html and 'href="/kreativ"' in html)
    check("/kreativ: INLINE shablonlar bo'limi (20 ta karta, 8 tasi ochiq, 'Shundan boshlash')", 'id="cs-inline-tpl"' in html and html.count('data-template-key="') == 20 and html.count('class="cs-inline-tpl-card cs-inline-tpl-more" data-template-key=') == 12 and 'name="template_key" value="bold_sale"' in html and 'id="cs-inline-tpl-toggle"' in html)
    check("/kreativ: inline shablon preview PNG + to'liq galereyaga havola", "/static/creative_templates/warm_food.png" in html and 'href="/kreativ/shablonlar"' in html)
    r = admin.get("/kreativ/shablonlar")
    html = r.get_data(as_text=True)
    check("shablonlar galereyasi 200, 20 ta karta", r.status_code == 200 and html.count('class="cs-tpl-card"') == 20)
    check("shablon preview PNG havolalari", "/static/creative_templates/bold_sale.png" in html and "Shundan boshlash" in html)


# ---------------------------------------------------------------------------
# 2) Yangi kreativ
# ---------------------------------------------------------------------------
def test_new_ai_and_template():
    global ASSET_ID, TPL_ASSET_ID
    r = admin.get("/kreativ/yangi")
    html = r.get_data(as_text=True)
    check("yangi GET 200: ikkita karta", r.status_code == 200 and 'id="cs-start-ai"' in html and 'id="cs-start-tpl"' in html)
    r = admin.post("/kreativ/yangi", data={"mode": "ai", "aspect": "1:1"})
    check("AI POST -> redirect /kreativ/<id>", r.status_code in (302, 303))
    m = re.search(r"/kreativ/(\d+)$", r.headers.get("Location", ""))
    check("redirect muharrirga", bool(m))
    ASSET_ID = int(m.group(1))
    a = _row(db_module.CreativeAsset, ASSET_ID)
    check("status=collecting_brief, kind=ai_generated, template yo'q", a.status == "collecting_brief" and a.kind == "ai_generated" and a.template_key is None and a.company_id == A_ID)
    check("missing_fields: focus + phone majburiy (profilda telefon yo'q)", "focus" in json.loads(a.missing_fields_json) and "phone" in json.loads(a.missing_fields_json))

    # Shablon bilan AI rejimi -> template_key saqlanadi
    r = admin.post("/kreativ/yangi", data={"mode": "ai", "template_key": "bold_sale", "aspect": ""})
    aid2 = int(r.headers["Location"].rstrip("/").split("/")[-1])
    a2 = _row(db_module.CreativeAsset, aid2)
    check("AI + shablon: template_key=bold_sale, aspekt shablondan (1:1)", a2.template_key == "bold_sale" and a2.aspect == "1:1")
    r = admin.post("/kreativ/yangi", data={"mode": "ai", "template_key": "yoq_shablon"})
    check("noma'lum shablon -> 400 (o'zbekcha)", r.status_code == 400 and "topilmadi" in r.get_data(as_text=True))

    # Shablon rejimi (OpenAI'siz) -> darhol ready, mahsulot fotosi bilan
    with mock.patch.object(creative_studio, "_openai_request") as req:
        r = admin.post("/kreativ/yangi", data={"mode": "template", "template_key": "warm_food", "aspect": "4:5",
                                               "product_image": (io.BytesIO(_png_bytes(300, 300, (10, 120, 10))), "taom.png")},
                       content_type="multipart/form-data")
    check("shablon POST -> redirect, OpenAI chaqirilmagan", r.status_code in (302, 303) and req.call_count == 0)
    TPL_ASSET_ID = int(r.headers["Location"].rstrip("/").split("/")[-1])
    t = _row(db_module.CreativeAsset, TPL_ASSET_ID)
    check("shablon asset: ready, kind=template, 4:5, final PNG bor", t.status == "ready" and t.kind == "template" and t.aspect == "4:5" and t.final_storage_path and (creative_studio.CREATIVE_ROOT / t.final_storage_path).exists())
    page = admin.get(f"/kreativ/{TPL_ASSET_ID}")
    data = _asset_json(page.get_data(as_text=True))
    check("shablon muharrir JSON: is_ready, image_url, base_image_url, layers", data["is_ready"] and data["image_url"].startswith(f"/kreativ/{TPL_ASSET_ID}/rasm.png?v=") and data["base_image_url"] and len(data["layers"]) >= 4)
    check("kvota shablondan keyin ham 0 (sarflanmadi)", data["quota"]["used"] == 0)


# ---------------------------------------------------------------------------
# 3) Brif
# ---------------------------------------------------------------------------
def test_brief_flow():
    page = admin.get(f"/kreativ/{ASSET_ID}")
    html = page.get_data(as_text=True)
    check("muharrir 200 + creative_studio.js + CSRF", page.status_code == 200 and "creative_studio.js" in html and 'data-csrf="' in html)
    data = _asset_json(html)
    check("JSON: collecting_brief, 5 ta savol (telefon bilan), focus+phone majburiy", data["status"] == "collecting_brief" and len(data["questions"]) == 5 and [q["key"] for q in data["missing_questions"]] == ["focus", "phone"])
    check("JSON: muharrir 'target' bo'limi (admin, maqsadlar)", data["target"]["can_create"] is True and any(o["value"] == "MESSAGES" for o in data["target"]["objectives"]) and data["urls"]["target_create"] == f"/kreativ/{ASSET_ID}/target-yarat")
    check("JSON: kvota business 0/30, can_generate", data["quota"]["limit"] == 30 and data["quota"]["can_generate"] is True)
    r = admin.post(f"/kreativ/{ASSET_ID}/brief", json={"key": "offer_text", "value": "-20% chegirma"})
    d = r.get_json()
    check("ixtiyoriy javob saqlandi, focus/phone hali majburiy", r.status_code == 200 and d["asset"]["brief_answers"]["offer_text"] == "-20% chegirma" and [q["key"] for q in d["asset"]["missing_questions"]] == ["focus", "phone"])
    with mock.patch.object(creative_studio, "_openai_request") as req:
        r = admin.post(f"/kreativ/{ASSET_ID}/generate", json={})
    check("brif to'liq emas -> 400 + o'zbekcha + OpenAI chaqirilmagan", r.status_code == 400 and "savol" in r.get_json()["error"] and req.call_count == 0)
    check("brif to'liq emas -> status o'zgarmadi", r.get_json()["asset"]["status"] == "collecting_brief")
    r = admin.post(f"/kreativ/{ASSET_ID}/brief", json={"key": "focus", "value": "Oshxona mebeli, yangi kolleksiya"})
    d = r.get_json()
    check("focus javobidan keyin faqat phone majburiy", r.status_code == 200 and [q["key"] for q in d["asset"]["missing_questions"]] == ["phone"] and d["asset"]["brief_answers"]["focus"].startswith("Oshxona"))
    r = admin.post(f"/kreativ/{ASSET_ID}/brief", json={"key": "phone", "value": "raqam"})
    check("noto'g'ri telefon -> 400 (o'zbekcha)", r.status_code == 400 and "Telefon" in r.get_json()["error"])
    r = admin.post(f"/kreativ/{ASSET_ID}/brief", json={"key": "phone", "value": "+998 90 123 45 67"})
    d = r.get_json()
    check("telefon javobidan keyin missing_questions bo'sh", r.status_code == 200 and d["asset"]["missing_questions"] == [] and d["asset"]["brief_answers"]["phone"] == "+998 90 123 45 67")
    s_ = db_module.get_session()
    try:
        with db_module.unscoped():
            check("telefon kompaniya profiliga yozildi", s_.get(db_module.Company, A_ID).phone == "+998 90 123 45 67")
    finally:
        s_.close()
    page = admin.get(f"/kreativ/{ASSET_ID}")
    check("profilda telefon paydo bo'lgach savol 4 taga tushadi (phone yashirin)", len(_asset_json(page.get_data(as_text=True))["questions"]) == 4)
    r = admin.post(f"/kreativ/{ASSET_ID}/brief", json={"key": "xyz", "value": "a"})
    check("noma'lum savol 400", r.status_code == 400)


# ---------------------------------------------------------------------------
# 4-5) Generatsiya + kvota
# ---------------------------------------------------------------------------
def test_generate_and_quota():
    with mock.patch.object(creative_studio, "_openai_request", return_value=_ok_openai_response()) as req:
        r = admin.post(f"/kreativ/{ASSET_ID}/generate", json={})
    d = r.get_json()
    check("generate 200, OpenAI 1 marta", r.status_code == 200 and d.get("ok") and req.call_count == 1)
    check("status=ready + image_url + layers", d["asset"]["status"] == "ready" and d["asset"]["image_url"] and d["asset"]["is_ready"] and len(d["asset"]["layers"]) >= 3)
    check("kvota 1/30", d["asset"]["quota"]["used"] == 1 and d["asset"]["quota"]["remaining"] == 29)
    check("headline brifdan (Oshxona mebeli) -- AI kopirayter offline, zaxira", any("Oshxona" in (l.get("text") or "") for l in d["asset"]["layers"]))
    check("telefon qatlami standart qatlamlarda", any(l["id"] == "phone" and l["text"] == "Tel: +998 90 123 45 67" for l in d["asset"]["layers"]))
    check("CTA 'Batafsil' emas", not any((l.get("text") or "") == "Batafsil" for l in d["asset"]["layers"]))
    r = admin.get(f"/kreativ/{ASSET_ID}/rasm.png")
    check("rasm.png 200 image/png", r.status_code == 200 and r.mimetype == "image/png" and len(r.data) > 100)
    r = admin.get(f"/kreativ/{ASSET_ID}/fon.png")
    check("fon.png 200 image/png", r.status_code == 200 and r.mimetype == "image/png")
    with Image.open(io.BytesIO(admin.get(f"/kreativ/{ASSET_ID}/rasm.png").data)) as img:
        check("final 1080x1080 (1:1)", img.size == (1080, 1080))

    # OpenAI xatosi -> failed + friendly
    with mock.patch.object(creative_studio, "_openai_request", return_value=_FakeResp(500, {"error": {"message": "internal RAW"}})):
        r = admin.post(f"/kreativ/{ASSET_ID}/regenerate", json={})
    check("OpenAI 500 -> 400 friendly (xom matn yo'q) + status failed", r.status_code == 400 and "RAW" not in r.get_json()["error"] and r.get_json()["asset"]["status"] == "failed")
    with mock.patch.object(creative_studio, "_openai_request", return_value=_ok_openai_response()):
        r = admin.post(f"/kreativ/{ASSET_ID}/regenerate", json={})
    check("regenerate -> yana ready, kvota 2", r.status_code == 200 and r.get_json()["asset"]["status"] == "ready" and r.get_json()["asset"]["quota"]["used"] == 2)

    # Trial: limit 0 -> friendly, OpenAI chaqirilmaydi
    t = _client("cs_admin_t")
    r = t.post("/kreativ/yangi", data={"mode": "ai"})
    tid = int(r.headers["Location"].rstrip("/").split("/")[-1])
    page = t.get(f"/kreativ/{tid}")
    data = _asset_json(page.get_data(as_text=True))
    check("trial: can_generate False, label tarif haqida", data["quota"]["can_generate"] is False and "Sinov" in data["quota"]["label"])
    check("trial: profilda mahsulot bor -> majburiy savol yo'q", data["missing_questions"] == [])
    with mock.patch.object(creative_studio, "_openai_request") as req:
        r = t.post(f"/kreativ/{tid}/generate", json={})
    d = r.get_json()
    check("trial generate -> 400 (500 emas) + quota_exceeded + o'zbekcha", r.status_code == 400 and d.get("quota_exceeded") is True and "tarif" in d["error"].lower())
    check("trial: OpenAI UMUMAN chaqirilmagan", req.call_count == 0)
    # Trial shablon rejimi ishlaydi (kvota sarflamaydi)
    r = t.post("/kreativ/yangi", data={"mode": "template", "template_key": "minimal_clean"})
    check("trial: shablondan yaratish ishlaydi", r.status_code in (302, 303) and _row(db_module.CreativeAsset, int(r.headers["Location"].rstrip("/").split("/")[-1])).status == "ready")


# ---------------------------------------------------------------------------
# 6-7) Qatlamlar + eksport
# ---------------------------------------------------------------------------
def test_layers_and_export():
    a = _row(db_module.CreativeAsset, ASSET_ID)
    final = creative_studio.CREATIVE_ROOT / a.final_storage_path
    before = final.read_bytes()
    layers = a.get_layers()
    for l in layers:
        if l["id"] == "headline":
            l["text"] = "YANGI SARLAVHA TEST"; l["x"] = 0.1; l["y"] = 0.2
    layers.append({"id": "extra", "type": "badge", "x": 0.5, "y": 0.5, "w": 0.3, "h": 0.08, "text": "Sinov", "bg_color": "#ff0000", "color": "#fff", "size_ratio": 0.03})
    r = admin.post(f"/kreativ/{ASSET_ID}/layers", json={"layers": layers})
    d = r.get_json()
    check("layers POST 200", r.status_code == 200 and d.get("ok"))
    saved = {l["id"]: l for l in d["asset"]["layers"]}
    check("yangi matn/pozitsiya saqlandi (tozalangan)", saved["headline"]["text"] == "YANGI SARLAVHA TEST" and saved["headline"]["y"] == 0.2 and saved["extra"]["bg_color"] == "#FF0000" and saved["extra"]["color"] == "#FFFFFF")
    after = final.read_bytes()
    check("final rasm qayta chizildi (fayl o'zgardi)", after != before and len(after) > 0)
    check("image_url versiyasi bor", "?v=" in d["asset"]["image_url"])
    check("kvota o'zgarmadi (render OpenAI'siz)", d["asset"]["quota"]["used"] == 2)
    r = admin.post(f"/kreativ/{ASSET_ID}/layers", json={"layers": [{"type": "hack"}]})
    check("noto'g'ri qatlam turi 400", r.status_code == 400 and "noma'lum" in r.get_json()["error"])
    r = admin.post(f"/kreativ/{ASSET_ID}/layers", json={"foo": 1})
    check("qatlamlarsiz body 400", r.status_code == 400)

    r = admin.get(f"/kreativ/{ASSET_ID}/eksport.png")
    check("eksport.png: image/png, attachment, hajm > 0", r.status_code == 200 and r.mimetype == "image/png" and "attachment" in r.headers.get("Content-Disposition", "") and len(r.data) > 1000)
    r = admin.get(f"/kreativ/{ASSET_ID}/eksport.pdf")
    check("eksport.pdf: application/pdf, attachment, hajm > 0", r.status_code == 200 and r.mimetype == "application/pdf" and "attachment" in r.headers.get("Content-Disposition", "") and r.data[:4] == b"%PDF")


# ---------------------------------------------------------------------------
# 8) Multi-tenant
# ---------------------------------------------------------------------------
def test_multitenant():
    b = _client("cs_admin_b")
    for path, method in [(f"/kreativ/{ASSET_ID}", "GET"), (f"/kreativ/{ASSET_ID}/rasm.png", "GET"), (f"/kreativ/{ASSET_ID}/eksport.png", "GET"),
                         (f"/kreativ/{ASSET_ID}/eksport.pdf", "GET"), (f"/kreativ/{ASSET_ID}/brief", "POST"), (f"/kreativ/{ASSET_ID}/generate", "POST"),
                         (f"/kreativ/{ASSET_ID}/layers", "POST"), (f"/kreativ/{ASSET_ID}/ochirish", "POST"), (f"/kreativ/{ASSET_ID}/target-yarat", "POST")]:
        r = b.open(path, method=method, json={} if method == "POST" else None)
        check(f"B: A kreativi {method} {path.split(str(ASSET_ID))[-1] or '/'} -> 404", r.status_code == 404)
    html = b.get("/kreativ").get_data(as_text=True).replace("&#39;", "'")
    check("B galereyasida A kreativi yo'q", f'/kreativ/{ASSET_ID}"' not in html and "Hali kreativ yo'q" in html)
    html_a = admin.get("/kreativ").get_data(as_text=True)
    check("A galereyasida o'z kreativlari bor (thumbnail bilan)", f'/kreativ/{ASSET_ID}"' in html_a and f"/kreativ/{ASSET_ID}/rasm.png?v=" in html_a)
    check("A kreativi hali mavjud (B o'chira olmadi)", _row(db_module.CreativeAsset, ASSET_ID) is not None)
    m = _client("cs_manager_a")
    r = m.get(f"/kreativ/{ASSET_ID}")
    check("menejer (target moduli) o'z kompaniyasi kreativini ko'radi", r.status_code == 200)


# ---------------------------------------------------------------------------
# 9) Avtopilot integratsiyasi
# ---------------------------------------------------------------------------
def test_autopilot_from_creative():
    s = db_module.get_session()
    try:
        with db_module.unscoped():
            draft = db_module.CampaignDraft(company_id=A_ID, title="Test kampaniya", source="AI", status="draft", objective="MESSAGES", sync_status="local")
            draft.set_state(campaign_draft.new_empty_state("MESSAGES"))
            draft.set_field_sources({})
            draft.set_ai_plan({})
            s.add(draft)
            s.commit()
            draft_id = draft.id
    finally:
        s.close()
    with _assets_mock():
        page = admin.get(f"/avtopilot/{draft_id}")
        check("Avtopilot sahifasida Kreativ studiya URL'i", page.status_code == 200 and 'data-creative-url="/kreativ"' in page.get_data(as_text=True))
        html = admin.get(f"/kreativ?from_autopilot={draft_id}&aspect=1:1").get_data(as_text=True)
        check("galereya from_autopilot: 'Bu reklamada ishlatish' + qaytish havolasi", "Bu reklamada ishlatish" in html and f'href="/avtopilot/{draft_id}"' in html and f'data-from-autopilot="{draft_id}"' in html)
        page = admin.get(f"/kreativ/{ASSET_ID}?from_autopilot={draft_id}")
        data = _asset_json(page.get_data(as_text=True))
        check("muharrir JSON: from_autopilot + autopilot URL", data["from_autopilot"] == draft_id and data["urls"]["autopilot"] == f"/avtopilot/{draft_id}")

        r = admin.post(f"/avtopilot/{draft_id}/media/from-kreativ", json={"creative_asset_id": ASSET_ID})
        d = r.get_json()
        check("from-kreativ 200 + draft JSON", r.status_code == 200 and d.get("ok") and d["media_id"] and d["draft"]["id"] == draft_id)
        media = _row(db_module.CampaignDraftMedia, d["media_id"])
        check("CampaignDraftMedia: creative_asset_id bog'langan, image, PNG", media.creative_asset_id == ASSET_ID and media.kind == "image" and media.content_type == "image/png" and media.filename == f"kreativ_{ASSET_ID}.png" and media.width == 1080)
        check("ad.media tanlangan (media_id)", d["draft"]["state"]["ad"]["media"]["media_id"] == media.id and d["draft"]["media"][0]["selected"] is True)
        check("Meta ulanmagan -> upload_error izohi (500 emas)", d["upload_error"] and "ulanmagan" in d["upload_error"])
        check("audit: media_selected (creative_studio)", any(e["action"] == "media_selected" and (e["details"] or {}).get("creative_asset_id") == ASSET_ID for e in d["draft"]["events"]))
        check("media fayli diskda", campaign_media.media_file_path(media).exists())

        # Tayyor bo'lmagan asset -> 400
        s = db_module.get_session()
        try:
            with db_module.unscoped():
                na = db_module.CreativeAsset(company_id=A_ID, status="collecting_brief", kind="ai_generated", aspect="1:1")
                s.add(na); s.commit(); na_id = na.id
        finally:
            s.close()
        r = admin.post(f"/avtopilot/{draft_id}/media/from-kreativ", json={"creative_asset_id": na_id})
        check("tayyor bo'lmagan kreativ -> 400 (o'zbekcha)", r.status_code == 400 and "tayyor emas" in r.get_json()["error"])
        r = admin.post(f"/avtopilot/{draft_id}/media/from-kreativ", json={"creative_asset_id": "x"})
        check("noto'g'ri id -> 400", r.status_code == 400)

        # Boshqa kompaniya asset'i (T kompaniyasining shablon rasmi) -> 404
        s = db_module.get_session()
        try:
            with db_module.unscoped():
                other = s.query(db_module.CreativeAsset).filter(db_module.CreativeAsset.company_id == T_ID, db_module.CreativeAsset.status == "ready").first()
                other_id = other.id
        finally:
            s.close()
        r = admin.post(f"/avtopilot/{draft_id}/media/from-kreativ", json={"creative_asset_id": other_id})
        check("boshqa kompaniya kreativi -> 404", r.status_code == 404)
        b = _client("cs_admin_b")
        check("B: A qoralamasiga from-kreativ 404", b.post(f"/avtopilot/{draft_id}/media/from-kreativ", json={"creative_asset_id": ASSET_ID}).status_code == 404)
        m = _client("cs_manager_a")
        check("menejer: from-kreativ 403", m.post(f"/avtopilot/{draft_id}/media/from-kreativ", json={"creative_asset_id": ASSET_ID}).status_code == 403)


# ---------------------------------------------------------------------------
# 10) Brend kit
# ---------------------------------------------------------------------------
def test_brand_kit():
    r = admin.get("/sozlamalar/brend")
    html = r.get_data(as_text=True)
    check("brend GET 200, logotip yo'q", r.status_code == 200 and "Logotip yuklanmagan" in html and 'name="primary_color"' in html)
    check("brend logo fayli yo'q -> 404", admin.get("/sozlamalar/brend/logo").status_code == 404)
    r = admin.post("/sozlamalar/brend", data={"logo": (io.BytesIO(_png_bytes(80, 40, (0, 0, 0))), "logo.png"), "primary_color": "#0b63f5", "secondary_color": "#7c22f0"},
                   content_type="multipart/form-data")
    check("brend POST -> redirect", r.status_code in (302, 303))
    r = admin.get("/sozlamalar/brend")
    html = r.get_data(as_text=True)
    check("keyingi GET: ranglar qaytdi", 'value="#0B63F5"' in html and 'value="#7C22F0"' in html)
    check("keyingi GET: logotip preview", "/sozlamalar/brend/logo?v=" in html)
    r = admin.get("/sozlamalar/brend/logo")
    check("logo fayli image/png", r.status_code == 200 and r.mimetype == "image/png")
    check("B kompaniyasi A logotipini ololmaydi", _client("cs_admin_b").get("/sozlamalar/brend/logo").status_code == 404)
    r = admin.post("/sozlamalar/brend", data={"logo": (io.BytesIO(b"not an image"), "logo.png"), "primary_color": "#000000", "secondary_color": ""}, content_type="multipart/form-data")
    check("buzilgan logotip -> redirect + saqlanmadi", r.status_code in (302, 303) and creative_studio.get_brand_kit(db_module.get_session(), A_ID).primary_color == "#0B63F5")
    m = _client("cs_manager_a")
    r = m.post("/sozlamalar/brend", data={"primary_color": "#111111", "secondary_color": "#222222"})
    check("menejer brend kitni o'zgartira olmaydi", r.status_code in (302, 303) and creative_studio.get_brand_kit(db_module.get_session(), A_ID).primary_color == "#0B63F5")
    # Logotip bilan qayta saqlash -> muharrir JSON brand.has_logo + logo_url
    r = admin.post(f"/kreativ/{ASSET_ID}/layers", json={"layers": _row(db_module.CreativeAsset, ASSET_ID).get_layers()})
    check("muharrir JSON: brand.has_logo + logo_url", r.get_json()["asset"]["brand"]["has_logo"] is True and r.get_json()["asset"]["brand"]["logo_url"] == "/sozlamalar/brend/logo")
    html = admin.get("/sozlamalar").get_data(as_text=True)
    check("Sozlamalar hub'ida Brend kit kartasi", 'href="/sozlamalar/brend"' in html)


# ---------------------------------------------------------------------------
# 13) "Targetga ochish" -- kreativdan Avtopilot qoralamasi (2026-09)
# ---------------------------------------------------------------------------
LLM_PLAN = {
    "campaign_name": "Replix | Nur Mebel | MESSAGES | Toshkent | Sep26", "adset_name": "Toshkent | 25-45", "ad_name": "Oshxona mebeli | v1",
    "age_min": 25, "age_max": 45, "genders": [2], "locations": ["Tashkent"], "interests": [],
    "advantage_audience": True, "placements_mode": "automatic", "destination_type": "INSTAGRAM_DIRECT",
    "primary_text_variants": ["Oshxona mebeli buyurtma asosida", "Matn 2", "Matn 3"], "headline_variants": ["Nur Mebel", "S2", "S3"], "description_variants": ["D1", "D2", "D3"],
    "cta": "SEND_MESSAGE", "messages": {"greeting": "Assalomu alaykum!", "quick_replies": ["Narxi qancha?"]},
    "reasoning_summary": "Mebel uchun ayollar 25-45.", "explanations": {}, "confidence": {}, "warnings": [],
}


def _fake_geo(query, location_types=None, *, access_token=None):
    return [{"key": "2430536", "name": "Tashkent", "type": "city", "country_code": "UZ", "region": "Tashkent"}]


def _planner_mock():
    return mock.patch.object(orchestrator, "_call_agent", return_value=json.loads(json.dumps(LLM_PLAN)))


def test_target_create():
    with _assets_mock():
        # Muharrir sahifasi: tayyor kreativda target-yarat URL + JS bo'limi
        html = admin.get(f"/kreativ/{ASSET_ID}").get_data(as_text=True)
        # A profili: "Toshkent shahri" auditoriyadan standart hudud chiqadi -> hudud so'ralmaydi
        check("muharrir: target_create URL + needs_location=False (profildan Toshkent)", f"/kreativ/{ASSET_ID}/target-yarat" in html and _asset_json(html)["target"]["needs_location"] is False and _asset_json(html)["target"]["default_location"])
        check("JS: 'Targetga ochish' bo'limi mavjud", "Targetga ochish" in Path(__file__).resolve().parent.parent.joinpath("static", "creative_studio.js").read_text(encoding="utf-8"))

        # a) Maqsad/byudjet berilmagan -> qoralama YARATILMAYDI, wizard'ga (kreativ bilan)
        s_ = db_module.get_session()
        try:
            with db_module.unscoped():
                before = s_.query(db_module.CampaignDraft).filter_by(company_id=A_ID).count()
        finally:
            s_.close()
        with _planner_mock() as llm:
            r = admin.post(f"/kreativ/{ASSET_ID}/target-yarat", data={})
        loc = r.headers.get("Location", "")
        check("ma'lumot yetishmasa -> 302 wizard'ga creative_asset_id bilan, planner chaqirilmagan", r.status_code in (302, 303) and loc.startswith("/avtopilot/yangi") and f"creative_asset_id={ASSET_ID}" in loc and "product_focus=" in loc and llm.call_count == 0)
        s_ = db_module.get_session()
        try:
            with db_module.unscoped():
                check("yetishmasa qoralama yaratilmadi", s_.query(db_module.CampaignDraft).filter_by(company_id=A_ID).count() == before)
        finally:
            s_.close()
        r = admin.post(f"/kreativ/{ASSET_ID}/target-yarat", json={"objective": "MESSAGES"})
        d = r.get_json()
        check("JSON: needs_input + missing (byudjet) + redirect", r.status_code == 200 and d["needs_input"] is True and {q["key"] for q in d["missing"]} == {"budget"} and "creative_asset_id" in d["redirect"])
        # Wizard GET: kreativ banneri, media bosqichi yashirin, maydonlar oldindan
        html = admin.get(loc).get_data(as_text=True)
        check("wizard: kreativ banneri + hidden creative_asset_id + media bosqichi yashirin", 'id="ap-creative-banner"' in html and f'name="creative_asset_id" value="{ASSET_ID}"' in html and f"/kreativ/{ASSET_ID}/rasm.png" in html and "Oshxona mebeli" in html)

        # b) Hammasi berilgan -> reja + qoralama + kreativ media + redirect
        with _planner_mock() as llm, mock.patch.object(meta_api, "search_geo_location", _fake_geo):
            r = admin.post(f"/kreativ/{ASSET_ID}/target-yarat", data={"objective": "MESSAGES", "budget": "150000", "location": "Toshkent"})
        m = re.search(r"/avtopilot/(\d+)$", r.headers.get("Location", ""))
        check("to'liq -> 302 /avtopilot/<id>, planner 1 marta", r.status_code in (302, 303) and bool(m) and llm.call_count == 1)
        draft_id = int(m.group(1))
        s_ = db_module.get_session()
        try:
            with db_module.unscoped():
                draft = s_.get(db_module.CampaignDraft, draft_id)
                media = s_.query(db_module.CampaignDraftMedia).filter_by(draft_id=draft_id).all()
                st = draft.get_state()
                check("yangi qoralama: A kompaniyasi, AI, MESSAGES, byudjet 150000", draft.company_id == A_ID and draft.source == "AI" and draft.objective == "MESSAGES" and st["adset"]["daily_budget"] == 150000.0)
                check("planner'ga product_focus kreativ brifidan ketdi", "Oshxona mebeli" in llm.call_args.args[1])
                check("kreativ media sifatida biriktirildi (creative_asset_id)", len(media) == 1 and media[0].creative_asset_id == ASSET_ID and media[0].kind == "image")
                check("ad.media tanlangan", (st["ad"].get("media") or {}).get("media_id") == media[0].id)
                evs = s_.query(db_module.CampaignDraftEvent).filter_by(draft_id=draft_id).all()
                check("audit: ai_generated_plan + media_selected (creative_studio)", any(e.action == "ai_generated_plan" for e in evs) and any(e.action == "media_selected" and "creative_studio" in (e.details_json or "") for e in evs))
        finally:
            s_.close()
        page = admin.get(f"/avtopilot/{draft_id}")
        check("ko'rib chiqish sahifasi ochiladi (200)", page.status_code == 200)
        # JSON varianti
        with _planner_mock(), mock.patch.object(meta_api, "search_geo_location", _fake_geo):
            r = admin.post(f"/kreativ/{ASSET_ID}/target-yarat", json={"objective": "LEADS", "budget": "90000", "location": "Toshkent"})
        d = r.get_json()
        check("JSON varianti: ok + draft_id + redirect", r.status_code == 200 and d["ok"] and d["redirect"] == f"/avtopilot/{d['draft_id']}")

        # c) Wizard POST creative_asset_id bilan -> biriktiradi (media faylsiz)
        with _planner_mock(), mock.patch.object(meta_api, "search_geo_location", _fake_geo):
            r = admin.post("/avtopilot/yangi", data={"objective": "MESSAGES", "budget": "120000", "location": "Toshkent", "creative_asset_id": str(ASSET_ID)})
        m = re.search(r"/avtopilot/(\d+)$", r.headers.get("Location", ""))
        check("wizard POST (creative_asset_id) -> redirect", r.status_code in (302, 303) and bool(m))
        s_ = db_module.get_session()
        try:
            with db_module.unscoped():
                media = s_.query(db_module.CampaignDraftMedia).filter_by(draft_id=int(m.group(1))).all()
                check("wizard: kreativ biriktirildi", len(media) == 1 and media[0].creative_asset_id == ASSET_ID)
        finally:
            s_.close()

        # d) Xato holatlari
        with _planner_mock(), mock.patch.object(meta_api, "search_geo_location", _fake_geo):
            r = admin.post(f"/kreativ/{ASSET_ID}/target-yarat", json={"objective": "MESSAGES", "budget": "-5", "location": "Toshkent"})
        check("noto'g'ri byudjet -> needs_input (crash yo'q)", r.status_code == 200 and r.get_json().get("needs_input") is True)
        with mock.patch.object(orchestrator, "_call_agent", side_effect=orchestrator.AgentUnavailableError("down")), mock.patch.object(meta_api, "search_geo_location", _fake_geo):
            r = admin.post(f"/kreativ/{ASSET_ID}/target-yarat", json={"objective": "MESSAGES", "budget": "100000", "location": "Toshkent"})
        check("planner ishlamasa -> 503 o'zbekcha (crash yo'q)", r.status_code == 503 and "javob bera olmadi" in r.get_json()["error"])
        with mock.patch.object(orchestrator, "_call_agent", side_effect=orchestrator.AgentUnavailableError("down")), mock.patch.object(meta_api, "search_geo_location", _fake_geo):
            r = admin.post(f"/kreativ/{ASSET_ID}/target-yarat", data={"objective": "MESSAGES", "budget": "100000", "location": "Toshkent"})
        check("planner ishlamasa (forma) -> muharrirga qaytadi", r.status_code in (302, 303) and r.headers["Location"].endswith(f"/kreativ/{ASSET_ID}"))
        # Tayyor bo'lmagan kreativ
        s_ = db_module.get_session()
        try:
            with db_module.unscoped():
                na = db_module.CreativeAsset(company_id=A_ID, status="collecting_brief", kind="ai_generated", aspect="1:1")
                s_.add(na); s_.commit(); na_id = na.id
        finally:
            s_.close()
        r = admin.post(f"/kreativ/{na_id}/target-yarat", json={"objective": "MESSAGES", "budget": "100000", "location": "Toshkent"})
        check("tayyor bo'lmagan kreativ -> 400", r.status_code == 400 and "yaratib" in r.get_json()["error"])
        check("wizard: tayyor bo'lmagan creative_asset_id e'tiborsiz (banner yo'q)", 'id="ap-creative-banner"' not in admin.get(f"/avtopilot/yangi?creative_asset_id={na_id}").get_data(as_text=True))
        m_ = _client("cs_manager_a")
        r = m_.post(f"/kreativ/{ASSET_ID}/target-yarat", json={"objective": "MESSAGES", "budget": "100000", "location": "Toshkent"})
        check("menejer -> 403", r.status_code == 403)
        page = m_.get(f"/kreativ/{ASSET_ID}")
        check("menejer JSON: target.can_create False", _asset_json(page.get_data(as_text=True))["target"]["can_create"] is False)
        b = _client("cs_admin_b")
        check("B: A kreativi target-yarat 404", b.post(f"/kreativ/{ASSET_ID}/target-yarat", json={"objective": "MESSAGES", "budget": "1"}).status_code == 404)
        check("B: wizard A kreativini ko'rmaydi", 'id="ap-creative-banner"' not in b.get(f"/avtopilot/yangi?creative_asset_id={ASSET_ID}").get_data(as_text=True))


def test_delete():
    r = admin.post(f"/kreativ/{TPL_ASSET_ID}/ochirish", json={})
    check("ochirish JSON 200", r.status_code == 200 and r.get_json()["ok"])
    s = db_module.get_session()
    try:
        with db_module.unscoped():
            check("qator o'chdi", s.get(db_module.CreativeAsset, TPL_ASSET_ID) is None)
    finally:
        s.close()
    check("papka o'chdi", not (creative_studio.CREATIVE_ROOT / str(A_ID) / str(TPL_ASSET_ID)).exists())


if __name__ == "__main__":
    test_login_required_and_empty_gallery()
    test_new_ai_and_template()
    test_brief_flow()
    test_generate_and_quota()
    test_layers_and_export()
    test_multitenant()
    test_autopilot_from_creative()
    test_brand_kit()
    test_target_create()
    test_delete()
    if failures:
        print("\nXATOLAR:", failures)
        sys.exit(1)
    print("\nBARCHA TESTLAR O'TDI")
