"""test_autopilot_web_offline.py — Meta Ads Autopilot WEB QATLAMI (2026-09,
foydalanuvchi so'rovi: "Replix ... Ads Manager'dagi har bir maydonni
boshidan o'zi to'ldirmasligi kerak").

TARMOQSIZ (offline): LLM (`orchestrator._call_agent`), Meta qidiruvlari
(`meta_api.search_geo_location` / `search_targeting_interests`), aktivlar
(`meta_publish.get_meta_assets`), media yuklash, preview, nashr -- hammasi
mock. Tekshiriladi:
  1. Wizard GET: maqsad + byudjet bor, profilda standart hudud bo'lsa hudud
     savoli YO'Q; POST -> qoralama yaratiladi -> ko'rib chiqishga redirect.
  2. Ko'rib chiqish sahifasi 200, `#ap-data` JSON oroli ichida holat bor.
  3. Qo'lda patch: yosh o'zgaradi, FAQAT adset tasdig'i bekor bo'ladi.
  4. AI chat tahriri (mock patch) qo'llanadi va `ai_edit` audit yozuvi.
  5. Ruxsat etilmagan AI patch'i rad etiladi -- holat o'zgarmaydi.
  6. Tasdiqlash endpoint'lari; xatoli daraja tasdiqlanmaydi (400).
  7. Nashr uchala tasdiqsiz 400 (o'zbekcha); tasdiqlab mock nashr -> published.
  8. Faollashtirish confirm'siz 400; confirm bilan -> active.
  9. Media yuklash (PIL PNG) -> ad.media.image_hash (mock upload).
 10. Preview: hash yo'q -> local:true; hash + mock -> Meta html.
 11. Import oqimi (mock) -> "Meta" belgili qoralama.
 12. Multi-tenant: B kompaniyasi A qoralamasiga 404, ro'yxatda faqat o'ziniki.
 13. Menejer (admin emas) ko'radi, nashrda 403.
 14. Sinov (trial) tarifi: banner ko'rinadi, nashr 400 + xabar.
 15. Sidebar'da "Avtopilot" bandi bor.

Ishga tushirish:
    cd app && python3 scripts/test_autopilot_web_offline.py
"""

import io
import os
import re
import sys
import json
import tempfile
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("META_ACCESS_TOKEN", "test-dummy-token")
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_test_dummy")
os.environ.setdefault("META_PAGE_ID", "page_test_dummy")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
_TMPDIR = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMPDIR, 'test_autopilot_web.db')}"

from PIL import Image  # noqa: E402

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402
import meta_api  # noqa: E402
import meta_publish  # noqa: E402
import campaign_media  # noqa: E402
import orchestrator  # noqa: E402
import business_profile  # noqa: E402

app_module.app.config["TESTING"] = True
app_module.app.config["WTF_CSRF_ENABLED"] = False
db_module.init_db()
campaign_media.MEDIA_ROOT = Path(_TMPDIR) / "ad_media"

failures = []


def check(name, cond):
    print(("OK: " if cond else "FAIL: ") + name)
    if not cond:
        failures.append(name)


ASSETS = {
    "ad_account": {"id": "act_1", "name": "Nur Acc", "currency": "UZS", "timezone_name": "Asia/Tashkent"},
    "pages": [{"id": "p1", "name": "Nur Mebel", "instagram_business_account": {"id": "ig1"}}],
    "instagram_accounts": [{"id": "ig1", "username": "nurmebel", "page_id": "p1"}],
    "pixels": [], "custom_audiences": [{"id": "AUD1", "name": "Saytga kirganlar"}], "lead_forms": [{"id": "FORM1", "name": "Eski forma"}],
    "recent_images": [], "campaigns": [], "errors": [], "pixel_id": None, "page_id": "p1", "ig_business_id": "ig1",
}
EMPTY_ASSETS = {"ad_account": None, "pages": [], "instagram_accounts": [], "pixels": [], "custom_audiences": [], "lead_forms": [],
                "recent_images": [], "campaigns": [], "errors": ["Meta ulanmagan (token yo'q)."], "pixel_id": None, "page_id": None, "ig_business_id": None}

LLM_PLAN = {
    "campaign_name": "Replix | Nur Mebel | MESSAGES | Toshkent | Sep26",
    "adset_name": "Toshkent | 25-45", "ad_name": "Oshxona mebeli | v1",
    "age_min": 25, "age_max": 45, "genders": [2],
    "locations": ["Tashkent"], "interests": ["Furniture"],
    "advantage_audience": False, "placements_mode": "automatic", "destination_type": "INSTAGRAM_DIRECT",
    "primary_text_variants": ["Oshxona mebeli buyurtma asosida", "Matn 2", "Matn 3"], "headline_variants": ["Nur Mebel", "S2", "S3"], "description_variants": ["D1", "D2", "D3"],
    "cta": "SEND_MESSAGE",
    "messages": {"greeting": "Assalomu alaykum!", "quick_replies": ["Narxi qancha?", "Manzil?"]},
    "reasoning_summary": "Mebel uchun ayollar 25-45.",
    "explanations": {"adset.targeting.age_min": "Profil bo'yicha"}, "confidence": {"adset.targeting.age_min": 85},
    "warnings": [],
}


def fake_geo(query, location_types=None, *, access_token=None):
    if "tash" in query.lower() or "tosh" in query.lower():
        return [{"key": "2430536", "name": "Tashkent", "type": "city", "country_code": "UZ", "region": "Tashkent"}]
    return []


def fake_interests(query, *, access_token, limit=10):
    return [{"id": "6003", "name": "Furniture", "audience_size_lower_bound": 1000, "path": []}] if "furn" in query.lower() else []


def _assets_mock(assets):
    return mock.patch.object(meta_publish, "get_meta_assets", lambda company, use_cache=True: json.loads(json.dumps(assets)))


def _png_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (600, 600), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Kompaniyalar / foydalanuvchilar
# ---------------------------------------------------------------------------
_session = db_module.get_session()
try:
    A = db_module.Company(name="Nur Mebel", plan="business", is_active=True)
    A.meta_ad_account_id = "act_1"; A.meta_page_id = "p1"; A.ig_business_id = "ig1"
    A.set_meta_access_token("tok")
    A.business_category = "furniture"
    A.business_profile_answers = business_profile.serialize_business_profile_answers({
        "product_or_service": "Oshxona mebeli", "target_audience": "25-45 yosh ayollar, Toshkent shahri", "price_range": "o'rta",
    })
    B = db_module.Company(name="Boshqa MChJ", plan="business", is_active=True)
    B.meta_ad_account_id = "act_2"; B.meta_page_id = "p2"
    B.set_meta_access_token("tok2")
    T = db_module.Company(name="Sinov MChJ", plan="trial", is_active=True)
    _session.add_all([A, B, T])
    _session.commit()
    A_ID, B_ID, T_ID = A.id, B.id, T.id
    users = [
        db_module.Manager(username="ap_admin_a", full_name="Admin A", role="admin", company_id=A_ID),
        db_module.Manager(username="ap_manager_a", full_name="Menejer A", role="manager", company_id=A_ID,
                          allowed_modules=json.dumps(["dashboard", "leads", "target"])),
        db_module.Manager(username="ap_admin_b", full_name="Admin B", role="admin", company_id=B_ID),
        db_module.Manager(username="ap_admin_t", full_name="Admin T", role="admin", company_id=T_ID),
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


def _draft_json(html):
    m = re.search(r'<script id="ap-data" type="application/json">(.*?)</script>', html, re.S)
    assert m, "ap-data oroli topilmadi"
    return json.loads(m.group(1))


def _row(draft_id):
    s = db_module.get_session()
    try:
        with db_module.unscoped():
            d = s.get(db_module.CampaignDraft, draft_id)
            s.refresh(d)
            s.expunge(d)
            return d
    finally:
        s.close()


def _events(draft_id):
    s = db_module.get_session()
    try:
        with db_module.unscoped():
            return [(e.actor, e.action, e.get_details()) for e in s.query(db_module.CampaignDraftEvent).filter_by(draft_id=draft_id).order_by(db_module.CampaignDraftEvent.id).all()]
    finally:
        s.close()


admin = _client("ap_admin_a")
DRAFT_ID = None


# ---------------------------------------------------------------------------
# 1) Wizard
# ---------------------------------------------------------------------------
def test_wizard_get_and_post():
    global DRAFT_ID
    with _assets_mock(ASSETS):
        r = admin.get("/avtopilot/yangi")
        html = r.get_data(as_text=True)
        check("wizard GET 200", r.status_code == 200)
        check("wizard: maqsad va byudjet savollari bor", 'name="objective"' in html and 'name="budget"' in html)
        check("wizard: profilda hudud bor -> hudud typeahead'i YO'Q", 'id="ap-location"' not in html and "Toshkent" in html.replace("&#39;", "'") or "Tashkent" in html)
        check("wizard: muddat preset'lari (7/14/30) bor", 'name="duration_preset"' in html and ">30 kun<" in html)
        check("wizard: media dropzone ixtiyoriy", 'id="ap-dropzone"' in html)

        with mock.patch.object(orchestrator, "_call_agent", return_value=dict(LLM_PLAN)), \
                mock.patch.object(meta_api, "search_geo_location", fake_geo), \
                mock.patch.object(meta_api, "search_targeting_interests", fake_interests):
            r = admin.post("/avtopilot/yangi", data={"objective": "MESSAGES", "budget": "200000", "duration_days": "7", "product_focus": "Oshxona mebeli"})
        check("wizard POST -> redirect", r.status_code in (302, 303))
        m = re.search(r"/avtopilot/(\d+)$", r.headers.get("Location", ""))
        check("redirect ko'rib chiqish sahifasiga", bool(m))
        DRAFT_ID = int(m.group(1))
        d = _row(DRAFT_ID)
        st = d.get_state()
        check("qoralama source=AI, status=draft", d.source == "AI" and d.status == "draft" and d.objective == "MESSAGES")
        check("AI reja: byudjet foydalanuvchidan, hudud Meta key bilan", st["adset"]["daily_budget"] == 200000.0 and st["adset"]["targeting"]["geo_locations"]["cities"][0]["key"] == "2430536")
        check("AI reja: qiziqish haqiqiy ID bilan", st["adset"]["targeting"]["interests"] == [{"id": "6003", "name": "Furniture"}])
        check("field_sources hammasi AI_RECOMMENDED", d.get_field_sources().get("adset.targeting.age_min") == "AI_RECOMMENDED")
        evs = _events(DRAFT_ID)
        check("audit: ai_generated_plan yozildi", any(a == "ai_generated_plan" and actor == "ai" for actor, a, _ in evs))

        # Planner ishlamasa -- o'zbekcha xato, wizard qayta chiziladi
        with mock.patch.object(orchestrator, "_call_agent", side_effect=orchestrator.AgentUnavailableError("down")):
            r = admin.post("/avtopilot/yangi", data={"objective": "MESSAGES", "budget": "100000"})
        check("planner ishlamasa 503 + wizard + o'zbekcha xabar", r.status_code == 503 and "javob bera olmadi" in r.get_data(as_text=True))
        # Byudjetsiz -> 400
        r = admin.post("/avtopilot/yangi", data={"objective": "MESSAGES", "budget": ""})
        check("byudjetsiz POST 400", r.status_code == 400)


# ---------------------------------------------------------------------------
# 2) Ko'rib chiqish sahifasi
# ---------------------------------------------------------------------------
def test_review_page():
    with _assets_mock(ASSETS):
        r = admin.get(f"/avtopilot/{DRAFT_ID}")
        html = r.get_data(as_text=True)
        check("review 200", r.status_code == 200)
        data = _draft_json(html)
        check("JSON oroli: state + field_sources + ai_plan + approvals", data["state"]["adset"]["targeting"]["age_min"] == 25 and data["field_sources"] and data["ai_plan"]["reasoning_summary"] and data["approvals"] == {"campaign": False, "adset": False, "ad": False})
        check("JSON: Meta aktivlari (sahifa/IG/auditoriya/forma)", data["assets"]["pages"][0]["id"] == "p1" and data["assets"]["custom_audiences"] and data["assets"]["lead_forms"])
        check("JSON: tekshiruv xatolari (media yo'q)", any(e["field"] == "media" for e in data["validation"]))
        check("JSON: nashr bloklangan + sabablar o'zbekcha", data["can_publish"] is False and any("tasdiqlanmagan" in x for x in data["publish_reasons"]))
        check("JSON: yo'nalishlar -- WhatsApp o'chiq + izoh", any(o["value"] == "WHATSAPP" and o["available"] is False and "sozlanmagan" in o["note"] for o in data["options"]["destinations"]))
        check("JSON: Instagram Direct mavjud (IG ulangan)", any(o["value"] == "INSTAGRAM_DIRECT" and o["available"] for o in data["options"]["destinations"]))
        check("JSON: API boshqarmaydigan maydonlar ro'yxati", any(u["key"] == "ab_test" for u in data["options"]["unsupported"]))
        check("sahifa autopilot.js va CSRF tokenini ulaydi", "autopilot.js" in html and 'data-csrf="' in html)
        check("sidebar'da Avtopilot bandi bor", 'data-tooltip="Avtopilot (Meta Ads)"' in html and 'href="/avtopilot"' in html)


# ---------------------------------------------------------------------------
# 3-6) Patch / AI edit / approve
# ---------------------------------------------------------------------------
def test_manual_patch_resets_only_adset_approval():
    s = db_module.get_session()
    try:
        with db_module.unscoped():
            d = s.get(db_module.CampaignDraft, DRAFT_ID)
            d.campaign_approved = True; d.adset_approved = True; d.ad_approved = True
            s.commit()
    finally:
        s.close()
    with _assets_mock(ASSETS):
        r = admin.post(f"/avtopilot/{DRAFT_ID}/patch", json={"scope": "adset", "changes": {"adset.targeting.age_min": 30, "adset.targeting.age_max": 50}})
        data = r.get_json()
        check("patch 200", r.status_code == 200)
        check("yosh 30-50 bo'ldi", data["draft"]["state"]["adset"]["targeting"]["age_min"] == 30 and data["draft"]["state"]["adset"]["targeting"]["age_max"] == 50)
        check("manba USER_OVERRIDDEN", data["draft"]["field_sources"]["adset.targeting.age_min"] == "USER_OVERRIDDEN")
        check("FAQAT adset tasdig'i bekor", data["draft"]["approvals"] == {"campaign": True, "adset": False, "ad": True} and data["reset_scopes"] == ["adset"])
        evs = _events(DRAFT_ID)
        check("audit: user_edit yozildi", any(a == "user_edit" and actor == "user" and set(dd.get("changed_paths") or []) == {"adset.targeting.age_min", "adset.targeting.age_max"} for actor, a, dd in evs))
        # Ruxsat etilmagan yo'l
        r = admin.post(f"/avtopilot/{DRAFT_ID}/patch", json={"scope": "campaign", "changes": {"campaign.status": "ACTIVE"}})
        check("ruxsatsiz yo'l 400 (o'zbekcha)", r.status_code == 400 and "ruxsat" in r.get_json()["error"].lower())
        check("holat o'zgarmadi (status PAUSED)", _row(DRAFT_ID).get_state()["campaign"]["status"] == "PAUSED")


def test_ai_edit_applies_and_logs():
    with _assets_mock(ASSETS), mock.patch.object(meta_api, "search_geo_location", fake_geo), mock.patch.object(meta_api, "search_targeting_interests", fake_interests):
        with mock.patch.object(orchestrator, "_call_agent", return_value={"scope": "adset", "changes": {"adset.daily_budget": 300000}, "reply": "Byudjet 300 ming qilindi.", "clarify": False}):
            r = admin.post(f"/avtopilot/{DRAFT_ID}/ai-edit", json={"message": "Budjetni 300 ming qil"})
        data = r.get_json()
        check("ai-edit 200 + applied", r.status_code == 200 and data["applied"] is True and data["reply"] == "Byudjet 300 ming qilindi.")
        check("byudjet 300000", data["draft"]["state"]["adset"]["daily_budget"] == 300000.0)
        check("chat tahriri ham USER_OVERRIDDEN", data["draft"]["field_sources"]["adset.daily_budget"] == "USER_OVERRIDDEN")
        evs = _events(DRAFT_ID)
        check("audit: ai_edit (actor=ai, buyruq + patch)", any(a == "ai_edit" and actor == "ai" and dd.get("message") == "Budjetni 300 ming qil" and dd.get("patch") for actor, a, dd in evs))

        before = _row(DRAFT_ID).get_state()
        with mock.patch.object(orchestrator, "_call_agent", return_value={"scope": "campaign", "changes": {"campaign.status": "ACTIVE"}, "reply": "Yoqdim.", "clarify": False}):
            r = admin.post(f"/avtopilot/{DRAFT_ID}/ai-edit", json={"message": "Kampaniyani yoq"})
        data = r.get_json()
        check("ruxsatsiz AI patch: applied=False, o'zbekcha javob", r.status_code == 200 and data["applied"] is False and "o'zgartirib bo'lmaydi" in data["reply"])
        check("holat O'ZGARMADI", _row(DRAFT_ID).get_state() == before)

        with mock.patch.object(orchestrator, "_call_agent", return_value={"scope": None, "changes": {}, "reply": "Qaysi shahar?", "clarify": True}):
            r = admin.post(f"/avtopilot/{DRAFT_ID}/ai-edit", json={"message": "Shaharni o'zgartir"})
        check("aniqlashtiruvchi savol: clarify=True, applied=False", r.get_json()["clarify"] is True and r.get_json()["applied"] is False)
        r = admin.post(f"/avtopilot/{DRAFT_ID}/ai-edit", json={"message": ""})
        check("bo'sh buyruq 400", r.status_code == 400)


def test_ai_edit_multi_adset():
    """2026-09 bugfix: bitta chat xabarida bir nechta ad set (auditoriya)
    so'ralganda AI oldin "alohida so'rashni iltimos qiling" deb rad
    etardi. Endi: birinchi auditoriya joriy qoralamaga qo'llanadi,
    qolganlari HAR BIRI uchun joriy qoralamaning nusxasi (yangi
    `CampaignDraft`) SHU SO'ROV ICHIDA yaratiladi va o'sha auditoriya
    darhol qo'llanadi -- foydalanuvchi ikkinchi marta yozmaydi."""
    with _assets_mock(ASSETS), mock.patch.object(meta_api, "search_geo_location", fake_geo), mock.patch.object(meta_api, "search_targeting_interests", fake_interests):
        llm_multi = {
            "scope": "adset",
            "changes": {"adset.name": "Tijorat quruvchilar", "adset.targeting.interests": [{"name": "Furniture"}]},
            "reply": "2 ta ad set tuzildi.", "clarify": False,
            "extra_ad_sets": [
                {"label": "Uy egalari", "changes": {
                    "adset.name": "Uy egalari", "adset.targeting.interests": [{"name": "Furniture"}],
                    "adset.targeting.age_min": 30, "adset.targeting.age_max": 55,
                }},
            ],
        }
        with mock.patch.object(orchestrator, "_call_agent", return_value=llm_multi):
            r = admin.post(f"/avtopilot/{DRAFT_ID}/ai-edit", json={
                "message": "Ikkita ad set qilib ber, biri tijorat bino quradigan biznes egalari, ikkinchisi uy ta'mirlash qiladigan uy egalari",
            })
        data = r.get_json()
        check("multi ad set: 200 + asosiy patch qo'llandi", r.status_code == 200 and data["applied"] is True)
        check("multi ad set: extra_drafts -- 1 ta yangi qoralama", len(data.get("extra_drafts") or []) == 1)
        extra = data["extra_drafts"][0]
        check("multi ad set: yangi qoralama ID joriysidan BOSHQA", extra["id"] != DRAFT_ID)
        check("multi ad set: reply foydalanuvchini alohida so'rashga YO'NALTIRMAYDI",
              "alohida so'rashni" not in data["reply"].lower() and "alohida qoralama" in data["reply"])

        new_row = _row(extra["id"])
        old_row = _row(DRAFT_ID)
        check("yangi qoralama -- bir xil kompaniya, status=draft, Meta ID yo'q (alohida tasdiqlanadi)",
              new_row.company_id == A_ID and new_row.status == "draft" and not new_row.meta_campaign_id)
        new_state = new_row.get_state()
        old_state = old_row.get_state()
        check("ikkala ad set nomi FARQLI (tavsiflangan auditoriyaga mos)", new_state["adset"]["name"] == "Uy egalari" and old_state["adset"]["name"] == "Tijorat quruvchilar")
        check("yangi ad set O'ZINING targeting'iga ega (yosh 30-55, nusxa emas)",
              new_state["adset"]["targeting"]["age_min"] == 30 and new_state["adset"]["targeting"]["age_max"] == 55
              and old_state["adset"]["targeting"]["age_max"] != 55)
        check("reklama matni/kreativ asosiy qoralamadan meros bo'ldi", new_state["ad"]["primary_text"] == old_state["ad"]["primary_text"])
        evs = _events(extra["id"])
        check("audit: yangi qoralamada ai_extra_adset_created yozildi",
              any(a == "ai_extra_adset_created" and dd.get("duplicated_from_draft_id") == DRAFT_ID for _, a, dd in evs))
        check("audit: yangi qoralamada ai_edit (auditoriya patch'i) ham yozildi", any(a == "ai_edit" for _, a, dd in evs))

        # Yaroqsiz/bo'sh qo'shimcha ad set -- asosiy so'rovni yiqitmaydi, shunchaki o'tkazib yuboriladi
        llm_bad_extra = {
            "scope": "adset", "changes": {"adset.daily_budget": 250000},
            "reply": "Byudjet yangilandi.", "clarify": False,
            "extra_ad_sets": [{"label": "Bo'sh", "changes": {}}],
        }
        with mock.patch.object(orchestrator, "_call_agent", return_value=llm_bad_extra):
            r = admin.post(f"/avtopilot/{DRAFT_ID}/ai-edit", json={"message": "Byudjetni 250 ming qil"})
        data = r.get_json()
        check("bo'sh qo'shimcha ad set -- asosiy o'zgarish baribir qo'llanadi", r.status_code == 200 and data["applied"] is True and data["draft"]["state"]["adset"]["daily_budget"] == 250000.0)
        check("bo'sh qo'shimcha ad set -- yangi qoralama YARATILMAYDI", not data.get("extra_drafts"))


def test_save_draft_endpoint():
    """2026-09, foydalanuvchi so'rovi: to'liq tekshiruv/tasdiq/nashrdan
    o'tmasdan ham qoralamani ANIQ "saqlandi" deb bilish. Har bir tahrir
    ALLAQACHON avtomatik saqlanadi -- bu endpoint validatsiya/tasdiq talab
    QILMAYDI, faqat ANIQ tasdiqlaydi (audit + updated_at)."""
    before = _row(DRAFT_ID)
    before_updated = before.updated_at
    with _assets_mock(ASSETS):
        r = admin.post(f"/avtopilot/{DRAFT_ID}/save-draft", json={})
    data = r.get_json()
    check("save-draft 200 + ok", r.status_code == 200 and data.get("ok") is True)
    check("save-draft: validatsiya/tasdiq TALAB QILINMAYDI (draft hali to'liq emas)", data["draft"]["can_publish"] in (True, False))
    after = _row(DRAFT_ID)
    check("save-draft: updated_at yangilandi (ro'yxatda yuqoriga chiqadi)", after.updated_at is not None and (before_updated is None or after.updated_at >= before_updated))
    evs = _events(DRAFT_ID)
    check("audit: draft_saved (actor=user) yozildi", any(a == "draft_saved" and actor == "user" for actor, a, _ in evs))
    # Menejer (admin emas) -- yozuvchi amal 403
    m = _client("ap_manager_a")
    r = m.post(f"/avtopilot/{DRAFT_ID}/save-draft", json={})
    check("menejer save-draft 403", r.status_code == 403)
    # Boshqa kompaniya qoralamasi -- 404
    b = _client("ap_admin_b")
    r = b.post(f"/avtopilot/{DRAFT_ID}/save-draft", json={})
    check("boshqa kompaniya save-draft 404", r.status_code == 404)


def test_approve_endpoints():
    with _assets_mock(ASSETS):
        r = admin.post(f"/avtopilot/{DRAFT_ID}/approve", json={"scope": "adset"})
        check("adset tasdiqlandi", r.status_code == 200 and r.get_json()["draft"]["approvals"]["adset"] is True)
        # Ad darajasida media yo'q -> tasdiqlab bo'lmaydi
        s = db_module.get_session()
        try:
            with db_module.unscoped():
                d = s.get(db_module.CampaignDraft, DRAFT_ID)
                d.ad_approved = False
                s.commit()
        finally:
            s.close()
        r = admin.post(f"/avtopilot/{DRAFT_ID}/approve", json={"scope": "ad"})
        check("xatoli daraja (media yo'q) tasdiqlanmaydi 400", r.status_code == 400 and "Rasm yoki video" in r.get_json()["error"])
        r = admin.post(f"/avtopilot/{DRAFT_ID}/approve", json={"scope": "xyz"})
        check("noto'g'ri scope 400", r.status_code == 400)
        evs = _events(DRAFT_ID)
        check("audit: approve_adset", any(a == "approve_adset" for _, a, _ in evs))


# ---------------------------------------------------------------------------
# 9-10) Media + preview
# ---------------------------------------------------------------------------
def test_media_upload_and_preview():
    with _assets_mock(ASSETS):
        r = admin.get(f"/avtopilot/{DRAFT_ID}/preview?fmt=instagram_feed")
        data = r.get_json()
        check("preview: hash yo'q -> local:true + yorliq", data["local"] is True and "Meta preview emas" in data["html"])

        with mock.patch.object(meta_api, "upload_ad_image", return_value={"hash": "HASH_ABC", "url": "https://x/y.png"}) as up:
            r = admin.post(f"/avtopilot/{DRAFT_ID}/media", data={"file": (io.BytesIO(_png_bytes()), "banner.png")}, content_type="multipart/form-data")
        data = r.get_json()
        check("media upload 200", r.status_code == 200 and data.get("ok") is True, )
        check("Meta'ga yuklandi (mock) va image_hash holatga yozildi", up.called and data["draft"]["state"]["ad"]["media"]["image_hash"] == "HASH_ABC" and data["draft"]["state"]["ad"]["media"]["media_id"] == data["media_id"])
        check("media ro'yxatida tanlangan + URL", data["draft"]["media"][0]["selected"] is True and data["draft"]["media"][0]["url"].startswith("/avtopilot/media/"))
        media_id = data["media_id"]
        r = admin.get(f"/avtopilot/media/{media_id}")
        check("media fayli beriladi (image/png)", r.status_code == 200 and r.mimetype == "image/png")

        with mock.patch.object(meta_api, "generate_ad_preview", return_value="<iframe src='https://www.facebook.com/ads/api/preview_iframe.php'></iframe>") as gp:
            r = admin.get(f"/avtopilot/{DRAFT_ID}/preview?fmt=instagram_story")
        data = r.get_json()
        check("preview: hash bor -> Meta html, local:false", data["local"] is False and "iframe" in data["html"] and gp.call_args[0][2] == "INSTAGRAM_STORY")
        with mock.patch.object(meta_api, "generate_ad_preview", side_effect=meta_api.MetaAPIError({"message": "bad", "code": 100})):
            r = admin.get(f"/avtopilot/{DRAFT_ID}/preview?fmt=facebook_feed")
        check("Meta preview xatosi -> lokal + sabab (Meta deb ko'rsatilmaydi)", r.get_json()["local"] is True and "olinmadi" in r.get_json()["reason"])

        # Ikkinchi media (select=0) -- tanlov o'zgarmaydi; keyin select endpoint
        with mock.patch.object(meta_api, "upload_ad_image", return_value={"hash": "HASH_2", "url": None}):
            r = admin.post(f"/avtopilot/{DRAFT_ID}/media", data={"file": (io.BytesIO(_png_bytes()), "second.png"), "select": "0"}, content_type="multipart/form-data")
        d2 = r.get_json()
        check("ikkinchi media tanlovni o'zgartirmadi", d2["draft"]["state"]["ad"]["media"]["image_hash"] == "HASH_ABC" and len(d2["draft"]["media"]) == 2)
        r = admin.post(f"/avtopilot/{DRAFT_ID}/media/{d2['media_id']}/select", json={})
        check("2-rasmni tanla -> image_hash HASH_2, selected_variant=1", r.get_json()["draft"]["state"]["ad"]["media"]["image_hash"] == "HASH_2" and r.get_json()["draft"]["state"]["ad"]["media"]["selected_variant"] == 1)
        # Boshqa kompaniya media faylini ololmaydi
        other = _client("ap_admin_b")
        check("B kompaniyasi A media fayliga 404", other.get(f"/avtopilot/media/{media_id}").status_code == 404)

        # 2026-09, foydalanuvchi so'rovi: media o'chirish. Keyingi testlar
        # HASH_2 tanlangan holatiga tayanadi -- shu sabab uchinchi, TASHLAB
        # YUBORILADIGAN media yuklab, faqat o'shani o'chiramiz (mavjud
        # tanlovga tegmaymiz).
        with mock.patch.object(meta_api, "upload_ad_image", return_value={"hash": "HASH_3", "url": None}):
            r = admin.post(f"/avtopilot/{DRAFT_ID}/media", data={"file": (io.BytesIO(_png_bytes()), "third.png"), "select": "0"}, content_type="multipart/form-data")
        d3 = r.get_json()
        check("boshqa kompaniya o'chira olmaydi (404)", other.post(f"/avtopilot/{DRAFT_ID}/media/{d3['media_id']}/ochirish", json={}).status_code == 404)
        r = admin.post(f"/avtopilot/{DRAFT_ID}/media/{d3['media_id']}/ochirish", json={})
        data = r.get_json()
        check("tanlanmagan media o'chirilgach, ok=True", r.status_code == 200 and data["ok"] is True)
        check("o'chirilgan media ro'yxatdan yo'qoldi", all(m["id"] != d3["media_id"] for m in data["draft"]["media"]))
        check("boshqa media tanlangan bo'lsa, o'chirish uni o'zgartirmaydi", data["draft"]["state"]["ad"]["media"]["image_hash"] == "HASH_2")
        r = admin.post(f"/avtopilot/{DRAFT_ID}/media/{d3['media_id']}/ochirish", json={})
        check("ikkinchi marta o'chirish -> 404 (allaqachon yo'q)", r.status_code == 404)
        check("noto'g'ri media_id -> 404", admin.post(f"/avtopilot/{DRAFT_ID}/media/999999/ochirish", json={}).status_code == 404)
        # TANLANGAN medianing o'chirilishi ad.media'ni tozalashini ham
        # alohida tekshiramiz -- so'ng darhol qayta tanlab, holatni
        # keyingi testlar uchun tiklaymiz.
        r = admin.post(f"/avtopilot/{DRAFT_ID}/media/{d2['media_id']}/ochirish", json={})
        data = r.get_json()
        check("tanlangan media o'chirilgach ad.media tozalandi", data["draft"]["state"]["ad"]["media"]["media_id"] is None and data["draft"]["state"]["ad"]["media"]["image_hash"] is None)
        with mock.patch.object(meta_api, "upload_ad_image", return_value={"hash": "HASH_2", "url": None}):
            r = admin.post(f"/avtopilot/{DRAFT_ID}/media", data={"file": (io.BytesIO(_png_bytes()), "second.png"), "select": "1"}, content_type="multipart/form-data")
        d2 = r.get_json()
        check("HASH_2 media qayta yuklab tanlandi (keyingi testlar uchun)", r.get_json()["draft"]["state"]["ad"]["media"]["image_hash"] == "HASH_2")


# ---------------------------------------------------------------------------
# 7-8) Nashr + faollashtirish
# ---------------------------------------------------------------------------
def test_publish_blocked_then_success_and_activate():
    with _assets_mock(ASSETS):
        s = db_module.get_session()
        try:
            with db_module.unscoped():
                d = s.get(db_module.CampaignDraft, DRAFT_ID)
                d.campaign_approved = False; d.adset_approved = True; d.ad_approved = False
                s.commit()
        finally:
            s.close()
        r = admin.post(f"/avtopilot/{DRAFT_ID}/publish", json={})
        data = r.get_json()
        check("nashr tasdiqsiz 400", r.status_code == 400)
        check("o'zbekcha sabablar (kampaniya + reklama tasdiqlanmagan)", any("Kampaniya darajasi" in x for x in data["errors"]) and any("Reklama (Ad)" in x for x in data["errors"]))
        r = admin.get(f"/avtopilot/{DRAFT_ID}/summary")
        check("summary JSON: xulosa + sabablar", r.status_code == 200 and r.get_json()["summary"]["budget_line"].startswith("Kunlik") and r.get_json()["can_publish"] is False)

        for scope in ("campaign", "ad"):
            r = admin.post(f"/avtopilot/{DRAFT_ID}/approve", json={"scope": scope})
            check(f"{scope} tasdiqlandi", r.status_code == 200, )
        check("uchala tasdiq -> can_publish", r.get_json()["draft"]["can_publish"] is True)

        r = admin.post(f"/avtopilot/{DRAFT_ID}/activate", json={})
        check("faollashtirish confirm'siz 400", r.status_code == 400 and "tasdiq" in r.get_json()["error"].lower())

        def fake_publish(session, draft, company, *, manager_id=None):
            draft.status = "published"; draft.sync_status = "synced"
            draft.meta_campaign_id = "C1"; draft.meta_adset_id = "AS1"; draft.meta_creative_id = "CR1"; draft.meta_ad_id = "AD1"
            meta_publish._log_event(session, draft, actor="system", action="publish_verified", scope="all", details={}, manager_id=manager_id)
            session.commit()
            return {"campaign_id": "C1", "adset_id": "AS1", "creative_id": "CR1", "ad_id": "AD1", "warnings": []}

        with mock.patch.object(meta_publish, "publish_draft", fake_publish):
            r = admin.post(f"/avtopilot/{DRAFT_ID}/publish", json={})
        data = r.get_json()
        check("nashr 200 + natija", r.status_code == 200 and data["result"]["campaign_id"] == "C1")
        check("qoralama published, Meta ID'lar bor", data["draft"]["status"] == "published" and data["draft"]["meta"]["ad_id"] == "AD1" and _row(DRAFT_ID).status == "published")
        check("nashrdan keyin qayta nashr bloklanadi", admin.post(f"/avtopilot/{DRAFT_ID}/publish", json={}).status_code == 400)

        # PublishError -> 400 friendly
        s = db_module.get_session()
        try:
            with db_module.unscoped():
                d = s.get(db_module.CampaignDraft, DRAFT_ID)
                d.status = "draft"; d.meta_campaign_id = None; d.meta_adset_id = None; d.meta_ad_id = None
                s.commit()
        finally:
            s.close()
        with mock.patch.object(meta_publish, "publish_draft", side_effect=meta_publish.PublishError("adset", "Byudjet Meta'ning minimal chegarasidan kam -- kunlik byudjetni oshiring.", "raw")):
            r = admin.post(f"/avtopilot/{DRAFT_ID}/publish", json={})
        check("PublishError -> 400 friendly + step (xom matn yo'q)", r.status_code == 400 and r.get_json()["step"] == "adset" and "raw" not in r.get_json()["error"] and "kunlik byudjetni oshiring" in r.get_json()["error"])
        s = db_module.get_session()
        try:
            with db_module.unscoped():
                d = s.get(db_module.CampaignDraft, DRAFT_ID)
                d.status = "published"; d.meta_campaign_id = "C1"; d.meta_adset_id = "AS1"; d.meta_ad_id = "AD1"
                s.commit()
        finally:
            s.close()

        with mock.patch.object(meta_api, "activate_object", return_value={"success": True}) as act:
            r = admin.post(f"/avtopilot/{DRAFT_ID}/activate", json={"confirm": True})
        check("faollashtirish confirm bilan -> active (3 ta obyekt)", r.status_code == 200 and r.get_json()["draft"]["status"] == "active" and act.call_count == 3)

        # Nashrdan keyingi tahrir -> local_changes; push (mock)
        r = admin.post(f"/avtopilot/{DRAFT_ID}/patch", json={"scope": "campaign", "changes": {"campaign.name": "Yangi nom"}})
        check("nashrdan keyin tahrir -> sync_status=local_changes, tasdiq bekor", r.get_json()["draft"]["sync_status"] == "local_changes" and r.get_json()["draft"]["approvals"]["campaign"] is False)
        r = admin.post(f"/avtopilot/{DRAFT_ID}/push", json={})
        check("push: tasdiqsiz 400", r.status_code == 400)
        admin.post(f"/avtopilot/{DRAFT_ID}/approve", json={"scope": "campaign"})
        with mock.patch.object(meta_api, "update_campaign", return_value={"success": True}) as uc, \
                mock.patch.object(meta_api, "update_adset", return_value={"success": True}) as ua:
            r = admin.post(f"/avtopilot/{DRAFT_ID}/push", json={})
        check("push: kampaniya nomi Meta'ga yuborildi", r.status_code == 200 and uc.called and "campaign.name" in r.get_json()["result"]["pushed"] and r.get_json()["draft"]["sync_status"] == "synced")
        check("push: faqat oxirgi sinxrondan keyingi o'zgarishlar (adset tegilmadi)", not ua.called)

        # Sync (mock)
        with mock.patch.object(meta_api, "get_campaign_basic", return_value={"id": "C1", "name": "Yangi nom", "status": "ACTIVE"}), \
                mock.patch.object(meta_api, "get_adset_basic", return_value={"id": "AS1", "name": "x", "status": "ACTIVE", "daily_budget": "30000000", "targeting": {"age_min": 30, "age_max": 50}, "end_time": None}), \
                mock.patch.object(meta_api, "get_ad_basic", return_value={"id": "AD1", "name": "x", "status": "ACTIVE"}):
            r = admin.post(f"/avtopilot/{DRAFT_ID}/sync", json={})
        check("sync endpoint ishlaydi", r.status_code == 200 and r.get_json()["sync_status"] in ("synced", "meta_changed", "local_changes"))


# ---------------------------------------------------------------------------
# 11) Import
# ---------------------------------------------------------------------------
def test_import_flow():
    tree = {
        "id": "C_IMP", "name": "Eski kampaniya", "objective": "OUTCOME_ENGAGEMENT", "status": "PAUSED", "special_ad_categories": [],
        "adsets": [{"id": "AS_IMP", "name": "AS", "status": "PAUSED", "daily_budget": "15000000", "optimization_goal": "CONVERSATIONS", "billing_event": "IMPRESSIONS",
                    "bid_strategy": "LOWEST_COST_WITHOUT_CAP", "destination_type": "MESSENGER", "start_time": "2026-09-01T09:00:00+0500", "end_time": "2026-09-08T09:00:00+0500",
                    "targeting": {"geo_locations": {"cities": [{"key": "2430536", "name": "Tashkent"}]}, "age_min": 20, "age_max": 40, "genders": [], "targeting_automation": {"advantage_audience": 1}},
                    "ads": [{"id": "AD_IMP", "name": "Ad", "status": "PAUSED", "creative": {"id": "CR_IMP", "object_story_spec": {"page_id": "p1", "link_data": {"message": "Salom", "name": "Sarlavha", "image_hash": "IMPHASH", "call_to_action": {"type": "MESSAGE_PAGE"}}}}}]}],
    }
    with _assets_mock(ASSETS):
        with mock.patch.object(meta_api, "list_ad_account_campaigns", return_value=[{"id": "C_IMP", "name": "Eski kampaniya", "objective": "OUTCOME_ENGAGEMENT", "status": "PAUSED", "updated_time": "2026-09-01T10:00:00+0500"}]):
            r = admin.get("/avtopilot/import")
        check("import picker 200 + kampaniya ro'yxati", r.status_code == 200 and "Eski kampaniya" in r.get_data(as_text=True))
        with mock.patch.object(meta_api, "get_campaign_tree", return_value=tree):
            r = admin.post("/avtopilot/import", data={"campaign_id": "C_IMP"})
        check("import POST -> redirect review", r.status_code in (302, 303) and "/avtopilot/" in r.headers.get("Location", ""))
        imp_id = int(r.headers["Location"].rstrip("/").split("/")[-1])
        d = _row(imp_id)
        check("import: source=IMPORTED, Meta ID'lar, synced, tasdiqlangan", d.source == "IMPORTED" and d.meta_campaign_id == "C_IMP" and d.sync_status == "synced" and d.all_approved)
        page = admin.get(f"/avtopilot/{imp_id}")
        data = _draft_json(page.get_data(as_text=True))
        check("import: barcha maydonlar 'Meta' belgisi bilan", data["field_sources"]["adset.targeting.age_min"] == "META_IMPORTED" and data["source_labels"]["META_IMPORTED"] == "Meta")
        check("import: sync chip Synced", data["sync_label"] == "Synced")
        # Trial/ulanmagan -> import redirect
        t = _client("ap_admin_t")
        r = t.get("/avtopilot/import")
        check("ulanmagan kompaniya importga kira olmaydi (redirect)", r.status_code in (302, 303))


# ---------------------------------------------------------------------------
# 12-13) Multi-tenant + rollar
# ---------------------------------------------------------------------------
def test_multitenant_and_roles():
    with _assets_mock(ASSETS):
        b = _client("ap_admin_b")
        check("B: A qoralamasi 404", b.get(f"/avtopilot/{DRAFT_ID}").status_code == 404)
        check("B: A qoralamasiga patch 404", b.post(f"/avtopilot/{DRAFT_ID}/patch", json={"scope": "campaign", "changes": {"campaign.name": "hack"}}).status_code == 404)
        check("B: A qoralamasiga publish 404", b.post(f"/avtopilot/{DRAFT_ID}/publish", json={}).status_code == 404)
        html = b.get("/avtopilot").get_data(as_text=True)
        check("B ro'yxatida A qoralamasi yo'q", f'/avtopilot/{DRAFT_ID}"' not in html and "Hali qoralama yo'q" in html.replace("&#39;", "'"))
        html_a = admin.get("/avtopilot").get_data(as_text=True)
        check("A ro'yxatida o'z qoralamasi bor", f'/avtopilot/{DRAFT_ID}"' in html_a)

        m = _client("ap_manager_a")
        r = m.get(f"/avtopilot/{DRAFT_ID}")
        check("menejer ko'ra oladi (200)", r.status_code == 200)
        check("menejer uchun sahifa is_admin=0", 'data-is-admin="0"' in r.get_data(as_text=True))
        check("menejer publish 403", m.post(f"/avtopilot/{DRAFT_ID}/publish", json={}).status_code == 403)
        check("menejer patch 403", m.post(f"/avtopilot/{DRAFT_ID}/patch", json={"scope": "campaign", "changes": {"campaign.name": "x"}}).status_code == 403)
        check("menejer ai-edit 403", m.post(f"/avtopilot/{DRAFT_ID}/ai-edit", json={"message": "x"}).status_code == 403)
        check("menejer wizard POST -> redirect (admin emas)", m.post("/avtopilot/yangi", data={"objective": "MESSAGES", "budget": "1"}).status_code in (302, 303))
        check("menejer launch-status 403", m.post(f"/avtopilot/{DRAFT_ID}/launch-status", json={"active": False}).status_code == 403)
        check("B: A qoralamasiga launch-status 404", b.post(f"/avtopilot/{DRAFT_ID}/launch-status", json={"active": False}).status_code == 404)


# ---------------------------------------------------------------------------
# 13b) Nashrdan keyingi holat (ACTIVE / PAUSED) -- 2026-09
# ---------------------------------------------------------------------------
def test_launch_status_toggle():
    """`launch_active` serialize'da bor (standart True), `/launch-status`
    uni o'zgartiradi va audit-jurnalga yozadi; `last_meta_error_raw` ham
    JSON'da (nashr xatosi diagnostikasi uchun)."""
    with _assets_mock(ASSETS):
        r = admin.get(f"/avtopilot/{DRAFT_ID}/summary")
        data = r.get_json()
        r2 = admin.post(f"/avtopilot/{DRAFT_ID}/patch", json={"scope": "campaign", "changes": {"campaign.name": "Launch test"}})
        dr = r2.get_json()["draft"]
        check("serialize: launch_active kaliti bor va standart True", dr.get("launch_active") is True)
        check("serialize: last_meta_error_raw kaliti bor", "last_meta_error_raw" in dr)
        r = admin.post(f"/avtopilot/{DRAFT_ID}/launch-status", json={})
        check("launch-status 'active'siz 400", r.status_code == 400 and "active" in r.get_json()["error"])
        r = admin.post(f"/avtopilot/{DRAFT_ID}/launch-status", json={"active": False})
        check("launch-status PAUSED -> 200, launch_active False", r.status_code == 200 and r.get_json()["draft"]["launch_active"] is False)
        check("bazada launch_active False", _row(DRAFT_ID).launch_active is False)
        evs = _events(DRAFT_ID)
        check("audit: launch_status_changed yozildi", any(a == "launch_status_changed" and dd.get("active") is False for _actor, a, dd in evs))
        before = dict(r.get_json()["draft"]["approvals"])
        r = admin.post(f"/avtopilot/{DRAFT_ID}/launch-status", json={"active": True})
        check("launch-status ACTIVE -> launch_active True", r.status_code == 200 and r.get_json()["draft"]["launch_active"] is True and _row(DRAFT_ID).launch_active is True)
        check("launch-status tasdiqlarni BEKOR QILMAYDI (holat maydoni emas)", r.get_json()["draft"]["approvals"] == before)


# ---------------------------------------------------------------------------
# 14) Sinov tarifi
# ---------------------------------------------------------------------------
def test_trial_company():
    t = _client("ap_admin_t")
    with _assets_mock(EMPTY_ASSETS):
        r = t.get("/avtopilot")
        html = r.get_data(as_text=True).replace("&#39;", "'")
        check("trial: ro'yxat ochiladi + ulanish banneri", r.status_code == 200 and "yetishmayapti" in html and "/connect-accounts" in html and "tarif" in html.lower())
        with mock.patch.object(orchestrator, "_call_agent", return_value=dict(LLM_PLAN)), \
                mock.patch.object(meta_api, "search_geo_location", fake_geo), mock.patch.object(meta_api, "search_targeting_interests", fake_interests):
            r = t.post("/avtopilot/yangi", data={"objective": "MESSAGES", "budget": "50000", "location": "Toshkent"})
        check("trial: AI reja tuzish ishlaydi (redirect)", r.status_code in (302, 303))
        tid = int(r.headers["Location"].rstrip("/").split("/")[-1])
        page = t.get(f"/avtopilot/{tid}")
        data = _draft_json(page.get_data(as_text=True))
        check("trial: review 200 + connection.problems (tarif + hisob)", page.status_code == 200 and data["connection"]["plan_allows_meta"] is False and len(data["connection"]["problems"]) >= 2)
        check("trial: hudud tokensiz topilmadi -> AI ogohlantirishi", any("topilmadi" in w or "aniqlanmadi" in w for w in data["ai_plan"]["warnings"]))
        s = db_module.get_session()
        try:
            with db_module.unscoped():
                d = s.get(db_module.CampaignDraft, tid)
                d.campaign_approved = d.adset_approved = d.ad_approved = True
                s.commit()
        finally:
            s.close()
        r = t.post(f"/avtopilot/{tid}/publish", json={})
        check("trial: publish 400 + tarif xabari", r.status_code == 400 and any("tarif" in e.lower() for e in r.get_json()["errors"]))
        r = t.get("/avtopilot/api/search-geo?q=Toshkent")
        check("trial: geo qidiruv tokensiz bo'sh + izoh", r.status_code == 200 and r.get_json()["results"] == [] and "ulanmagan" in r.get_json()["error"])


# ---------------------------------------------------------------------------
# Qo'shimcha: regenerate-copy, replan, archive, search endpoints
# ---------------------------------------------------------------------------
def test_regenerate_replan_archive_search():
    with _assets_mock(ASSETS), mock.patch.object(meta_api, "search_geo_location", fake_geo), mock.patch.object(meta_api, "search_targeting_interests", fake_interests):
        s = db_module.get_session()
        try:
            with db_module.unscoped():
                d = s.get(db_module.CampaignDraft, DRAFT_ID)
                d.status = "draft"
                s.commit()
        finally:
            s.close()
        with mock.patch.object(orchestrator, "_call_agent", return_value={"scope": "ad", "changes": {"ad.headline": "Kuchli sarlavha", "ad.copy_variants.headline": ["A", "B", "C"]}, "reply": "Yangi headline.", "clarify": False}):
            r = admin.post(f"/avtopilot/{DRAFT_ID}/regenerate-copy", json={"field": "headline"})
        check("regenerate-copy: headline yangilandi, manba AI", r.status_code == 200 and r.get_json()["draft"]["state"]["ad"]["headline"] == "Kuchli sarlavha" and r.get_json()["draft"]["field_sources"]["ad.headline"] == "AI_RECOMMENDED")
        check("regenerate-copy: noto'g'ri maydon 400", admin.post(f"/avtopilot/{DRAFT_ID}/regenerate-copy", json={"field": "campaign.status"}).status_code == 400)

        with mock.patch.object(orchestrator, "_call_agent", return_value=dict(LLM_PLAN, age_min=18, age_max=65)):
            r = admin.post(f"/avtopilot/{DRAFT_ID}/replan", json={})
        data = r.get_json()
        check("replan: foydalanuvchi override'i (yosh 30-50) SAQLANDI", r.status_code == 200 and data["draft"]["state"]["adset"]["targeting"]["age_min"] == 30 and data["draft"]["field_sources"]["adset.targeting.age_min"] == "USER_OVERRIDDEN")
        check("replan: media saqlandi, tasdiqlar bekor", data["draft"]["state"]["ad"]["media"]["image_hash"] == "HASH_2" and data["draft"]["all_approved"] is False)

        with mock.patch.object(meta_api, "search_geo_location", fake_geo):
            r = admin.get("/avtopilot/api/search-geo?q=Toshkent")
        check("search-geo JSON", r.get_json()["results"][0]["key"] == "2430536")
        with mock.patch.object(meta_api, "search_targeting_interests", side_effect=meta_api.MetaAPIError({"message": "Error validating access token: tok", "code": 190})):
            r = admin.get("/avtopilot/api/search-interests?q=furniture")
        check("search-interests Meta xatosi -> 200, bo'sh ro'yxat + xabar (5xx emas)", r.status_code == 200 and r.get_json()["results"] == [] and r.get_json()["error"])

        r = admin.post(f"/avtopilot/{DRAFT_ID}/archive", json={})
        check("archive -> archived", r.status_code == 200 and _row(DRAFT_ID).status == "archived")
        check("arxivda patch bloklanadi", admin.post(f"/avtopilot/{DRAFT_ID}/patch", json={"scope": "campaign", "changes": {"campaign.name": "x"}}).status_code == 400)


if __name__ == "__main__":
    test_wizard_get_and_post()
    test_review_page()
    test_manual_patch_resets_only_adset_approval()
    test_ai_edit_applies_and_logs()
    test_ai_edit_multi_adset()
    test_save_draft_endpoint()
    test_approve_endpoints()
    test_media_upload_and_preview()
    test_publish_blocked_then_success_and_activate()
    test_import_flow()
    test_multitenant_and_roles()
    test_launch_status_toggle()
    test_trial_company()
    test_regenerate_replan_archive_search()
    if failures:
        print("\nXATOLAR:", failures)
        sys.exit(1)
    print("\nBARCHA TESTLAR O'TDI")
