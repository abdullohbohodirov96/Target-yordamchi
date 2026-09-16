"""test_target_analysis_offline.py — "Target Analizi" (2026-09, foydalanuvchi
so'rovi: "targetolog agentimizni yuz foiz ishlaydigan qilish" -- Meta'dan
import qilingan/jonli kampaniyani AI diagnostika qilib, tasdiqlangan
o'zgarishlarnigina jonli Meta kampaniyasiga qo'llash).

TARMOQSIZ (offline): Meta (`meta_api.get_campaign_insights`), LLM
(`orchestrator._call_agent`), Meta'ga yuborish (`meta_publish.
push_updates_to_meta`) -- hammasi mock. Tekshiriladi:
  1. Ma'lumot yetarlilik chegarasi: kam kun/kam sarf -> LLM UMUMAN
     chaqirilmasdan "hali yetarli ma'lumot yo'q" natija (call_count == 0).
  2. `is_allowed_path()`dan o'tmagan taklif jimgina tashlab yuboriladi
     (`dropped_paths`da ko'rinadi), javob buzilmaydi.
  3. Nom bilan kelgan qiziqish (interest) Meta ID'ga aylanadi (chat_edit
     bilan bir xil mexanizm).
  4. Faqat TANLANGAN (approved) o'zgarishlar qo'llanadi -- `apply_patch`/
     `push_updates_to_meta` AYNAN shu yo'llarni oladi, boshqasini emas.
  5. LLM ishlamasa -- 503 + o'zbekcha xabar, sahifa yiqilmaydi.
  6. Boshqa kompaniyaning qoralamasini diagnostika/qo'llash qila olmaydi (404).

Ishga tushirish:
    cd app && python3 scripts/test_target_analysis_offline.py
"""

import os
import sys
import json
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("META_ACCESS_TOKEN", "test-dummy-token")
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_test_dummy")
os.environ.setdefault("META_PAGE_ID", "page_test_dummy")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
_TMPDIR = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMPDIR, 'test_target_analysis.db')}"

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402
import meta_api  # noqa: E402
import meta_publish  # noqa: E402
import campaign_draft as cd  # noqa: E402
import orchestrator  # noqa: E402
import target_analysis as ta  # noqa: E402

app_module.app.config["TESTING"] = True
app_module.app.config["WTF_CSRF_ENABLED"] = False
db_module.init_db()

failures = []


def check(name, cond):
    print(("OK: " if cond else "FAIL: ") + name)
    if not cond:
        failures.append(name)


ASSETS = {
    "ad_account": {"id": "act_1", "name": "Nur Acc", "currency": "UZS", "timezone_name": "Asia/Tashkent"},
    "pages": [{"id": "p1", "name": "Nur Mebel", "instagram_business_account": {"id": "ig1"}}],
    "instagram_accounts": [{"id": "ig1", "username": "nurmebel", "page_id": "p1"}],
    "pixels": [], "custom_audiences": [], "lead_forms": [],
    "recent_images": [], "campaigns": [], "errors": [], "pixel_id": None, "page_id": "p1", "ig_business_id": "ig1",
}


def _assets_mock():
    return mock.patch.object(meta_publish, "get_meta_assets", lambda company, use_cache=True: json.loads(json.dumps(ASSETS)))


def fake_interests(query, *, access_token, limit=10):
    return [{"id": "6003", "name": "Furniture", "audience_size_lower_bound": 1000, "path": []}] if "furn" in query.lower() else []


def fake_geo(query, location_types=None, *, access_token=None):
    return []


# ---------------------------------------------------------------------------
# Kompaniyalar / foydalanuvchilar / import qilingan (jonli) qoralama
# ---------------------------------------------------------------------------
_session = db_module.get_session()
try:
    A = db_module.Company(name="Nur Mebel", plan="business", is_active=True)
    A.meta_ad_account_id = "act_1"; A.meta_page_id = "p1"; A.ig_business_id = "ig1"
    A.set_meta_access_token("tok")
    B = db_module.Company(name="Boshqa MChJ", plan="business", is_active=True)
    B.meta_ad_account_id = "act_2"; B.meta_page_id = "p2"
    B.set_meta_access_token("tok2")
    _session.add_all([A, B])
    _session.commit()
    A_ID, B_ID = A.id, B.id
    users = [
        db_module.Manager(username="ta_admin_a", full_name="Admin A", role="admin", company_id=A_ID),
        db_module.Manager(username="ta_admin_b", full_name="Admin B", role="admin", company_id=B_ID),
    ]
    for u in users:
        u.set_password("parol123")
    _session.add_all(users)
    _session.commit()
finally:
    _session.close()

db_module.seed_default_funnel_stages_for_company(A_ID)


def _new_state():
    s = cd.new_empty_state("LEADS")
    s["campaign"]["name"] = "Nur Mebel | Lidlar"
    s["adset"]["name"] = "Toshkent"
    s["adset"]["daily_budget"] = 100000.0
    s["adset"]["currency"] = "UZS"
    s["adset"]["targeting"]["geo_locations"]["cities"] = [{"key": "2430536", "name": "Tashkent", "radius": 0, "distance_unit": "kilometer"}]
    s["ad"]["name"] = "Reklama 1"
    s["ad"]["page_id"] = "p1"
    s["ad"]["primary_text"] = "Sifatli mebel -- buyurtma asosida."
    s["ad"]["media"] = {"media_id": None, "image_hash": "HASH1", "video_id": None, "selected_variant": None}
    s["ad"]["lead_form"] = {"mode": "existing", "existing_form_id": "FORM1", "new_form": s["ad"]["lead_form"]["new_form"]}
    return s


def _make_draft(company_id, meta_campaign_id="C_LIVE"):
    s = _session2 = db_module.get_session()
    try:
        with db_module.scoped_as(company_id):
            d = db_module.CampaignDraft(
                company_id=company_id, title="Nur Mebel | Lidlar", source="IMPORTED",
                status="active", objective="LEADS",
                campaign_approved=True, adset_approved=True, ad_approved=True,
                meta_campaign_id=meta_campaign_id, meta_adset_id="AS_LIVE", meta_ad_id="AD_LIVE", meta_creative_id="CR_LIVE",
                sync_status="synced",
            )
            d.set_state(_new_state())
            d.set_field_sources({p: "META_IMPORTED" for p in cd.ALLOWED_PATHS if cd.PATH_TYPES[p] != "dict"})
            s.add(d)
            s.commit()
            did = d.id
    finally:
        s.close()
    return did


DRAFT_ID = _make_draft(A_ID)


def _client(username):
    c = app_module.app.test_client()
    r = c.post("/login", data={"username": username, "password": "parol123"})
    assert r.status_code in (302, 303), r.status_code
    return c


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
            return [(e.actor, e.action) for e in s.query(db_module.CampaignDraftEvent).filter_by(draft_id=draft_id).order_by(db_module.CampaignDraftEvent.id).all()]
    finally:
        s.close()


admin = _client("ta_admin_a")


# ---------------------------------------------------------------------------
# Meta insights fabrikasi
# ---------------------------------------------------------------------------
def _row_insight(spend, impressions=10000, reach=8000, frequency=1.2, cpm=None, ctr=2.0, leads=0, date=None):
    row = {"spend": str(spend), "impressions": str(impressions), "reach": str(reach), "frequency": str(frequency),
           "cpm": str(cpm if cpm is not None else (spend / impressions * 1000 if impressions else 0)), "ctr": str(ctr)}
    if leads:
        row["actions"] = [{"action_type": "lead", "value": str(leads)}]
    if date:
        row["date_start"] = date
        row["date_stop"] = date
    return row


def _daily_rows(n_days, spend_per_day, leads_per_day=0, start="2026-08-20"):
    import datetime as dt
    base = dt.date.fromisoformat(start)
    out = []
    for i in range(n_days):
        d = (base + dt.timedelta(days=i)).isoformat()
        out.append(_row_insight(spend_per_day, leads=leads_per_day, date=d))
    return out


def _fake_get_campaign_insights_factory(daily_rows, row_7d, row_30d):
    def fn(campaign_id, date_preset="last_7d", fields=None, time_range=None, time_increment=None, access_token=None):
        if time_increment:
            return daily_rows
        if date_preset == "last_7d":
            return [row_7d]
        return [row_30d]
    return fn


# ---------------------------------------------------------------------------
# 1) Ma'lumot yetarlilik chegarasi -- LLM UMUMAN chaqirilmaydi
# ---------------------------------------------------------------------------
def test_data_sufficiency_gate():
    # 1a) juda kam kun (atigi 2 kun sarf bo'lgan)
    daily = _daily_rows(2, 50000)
    fake_fn = _fake_get_campaign_insights_factory(daily, _row_insight(100000, leads=1), _row_insight(150000, leads=2))
    with _assets_mock(), mock.patch.object(meta_api, "get_campaign_insights", fake_fn), \
            mock.patch.object(orchestrator, "_call_agent") as llm:
        r = admin.post(f"/avtopilot/{DRAFT_ID}/target-analiz", json={})
    data = r.get_json()
    check("kam kun: 200 + sufficient=False", r.status_code == 200 and data["result"]["sufficient"] is False)
    check("kam kun: sabab o'zbekcha, kun sonini aytadi", "yetarli ma'lumot yo'q" in data["result"]["reason"] and "2 kun" in data["result"]["reason"])
    check("kam kun: LLM UMUMAN chaqirilmadi", llm.call_count == 0)

    # 1b) kunlar yetarli, lekin sarf joriy kunlik byudjetning 3 barobariga yetmaydi (daily_budget=100000)
    daily2 = _daily_rows(10, 20000)  # jami 200000 < 3*100000
    fake_fn2 = _fake_get_campaign_insights_factory(daily2, _row_insight(100000, leads=1), _row_insight(200000, leads=2))
    with _assets_mock(), mock.patch.object(meta_api, "get_campaign_insights", fake_fn2), \
            mock.patch.object(orchestrator, "_call_agent") as llm2:
        r = admin.post(f"/avtopilot/{DRAFT_ID}/target-analiz", json={})
    data = r.get_json()
    check("kam sarf: sufficient=False, LLM chaqirilmadi", data["result"]["sufficient"] is False and llm2.call_count == 0)
    check("kam sarf: sabab byudjet nisbatini aytadi", "kunlik byudjetning" in data["result"]["reason"])

    # 1c) Meta o'zi xato bersa -- LLM chaqirilmaydi, friendly xabar
    with _assets_mock(), mock.patch.object(meta_api, "get_campaign_insights", side_effect=meta_api.MetaAPIError({"message": "Error validating access token", "code": 190})), \
            mock.patch.object(orchestrator, "_call_agent") as llm3:
        r = admin.post(f"/avtopilot/{DRAFT_ID}/target-analiz", json={})
    data = r.get_json()
    check("Meta xatosi: sufficient=False + sabab, LLM chaqirilmadi, 200 (sahifa yiqilmadi)", r.status_code == 200 and data["result"]["sufficient"] is False and "Meta" in data["result"]["reason"] and llm3.call_count == 0)

    # Meta ulanmagan kampaniya (meta_campaign_id yo'q) -- 400, LLM chaqirilmaydi
    fresh_id = _make_draft(A_ID, meta_campaign_id=None)
    s = db_module.get_session()
    try:
        with db_module.unscoped():
            d = s.get(db_module.CampaignDraft, fresh_id)
            d.meta_campaign_id = None
            s.commit()
    finally:
        s.close()
    with mock.patch.object(orchestrator, "_call_agent") as llm4:
        r = admin.post(f"/avtopilot/{fresh_id}/target-analiz", json={})
    check("Meta'ga chiqarilmagan qoralama -- 400, LLM chaqirilmadi", r.status_code == 400 and llm4.call_count == 0)


# ---------------------------------------------------------------------------
# 2-3) Ruxsat etilmagan yo'l tashlanadi + nom -> Meta ID
# ---------------------------------------------------------------------------
LLM_DIAGNOSIS = {
    "summary": "Chastota yuqori va CPL o'sib bormoqda.",
    "issues": [
        {"issue": "Chastota yuqori", "evidence": "So'nggi 7 kunda chastota 4.1 ga yetgan.", "severity": "yuqori"},
        {"issue": "Noma'lum severity", "evidence": "x", "severity": "juda_yomon"},
    ],
    "changes": [
        {"path": "adset.daily_budget", "current_value": 100000, "proposed_value": 150000, "why": "Byudjetni oshirish o'rniga auditoriyani kengaytirish maqsadga muvofiq, lekin bu yerda sinov uchun budjet ham o'zgartiriladi."},
        {"path": "adset.targeting.interests", "current_value": [], "proposed_value": [{"name": "Furniture"}], "why": "Auditoriyani biroz aniqlashtirish."},
        {"path": "campaign.status", "current_value": "ACTIVE", "proposed_value": "PAUSED", "why": "Ruxsat etilmagan yo'l -- rad etilishi kerak."},
        {"path": "ad.page_id", "current_value": "p1", "proposed_value": "p2", "why": "is_allowed_path'dan o'tadi, lekin diagnostika ro'yxatida yo'q -- rad etilishi kerak."},
    ],
}


def _sufficient_fake_insights():
    daily = _daily_rows(10, 200000, leads_per_day=1)  # 10 kun, jami 2,000,000 >= 3*100000
    return _fake_get_campaign_insights_factory(daily, _row_insight(700000, leads=4), _row_insight(2000000, leads=20))


def test_invalid_path_dropped_and_names_resolved():
    with _assets_mock(), mock.patch.object(meta_api, "get_campaign_insights", _sufficient_fake_insights()), \
            mock.patch.object(orchestrator, "_call_agent", return_value=dict(LLM_DIAGNOSIS)) as llm, \
            mock.patch.object(meta_api, "search_targeting_interests", fake_interests), \
            mock.patch.object(meta_api, "search_geo_location", fake_geo):
        r = admin.post(f"/avtopilot/{DRAFT_ID}/target-analiz", json={})
    data = r.get_json()["result"]
    check("LLM bir marta chaqirildi (ma'lumot yetarli)", llm.call_count == 1)
    check("sufficient=True", data["sufficient"] is True)
    check("faqat 2 ta ruxsat etilgan taklif qoldi", len(data["changes"]) == 2)
    paths = [c["path"] for c in data["changes"]]
    check("ruxsat etilgan yo'llar to'g'ri", set(paths) == {"adset.daily_budget", "adset.targeting.interests"})
    check("ruxsatsiz yo'llar dropped_paths'da", "campaign.status" in data["dropped_paths"] and "ad.page_id" in data["dropped_paths"])
    interests_change = next(c for c in data["changes"] if c["path"] == "adset.targeting.interests")
    check("nom -> Meta ID (chat_edit bilan bir xil mexanizm)", interests_change["proposed_value"] == [{"id": "6003", "name": "Furniture"}])
    budget_change = next(c for c in data["changes"] if c["path"] == "adset.daily_budget")
    check("current_value joriy holatdan olingan", budget_change["current_value"] == 100000.0 and budget_change["proposed_value"] == 150000.0)
    check("noma'lum severity standartga tushadi", any(i["severity"] == "o'rtacha" for i in data["issues"]))
    check("javobda hech qanday uydirma raqam qo'shilmagan -- faqat fetch qilingan performance/crm qaytadi", data["performance"]["last_30d"]["spend"] == 2000000.0)


# ---------------------------------------------------------------------------
# 4) Faqat TANLANGAN o'zgarishlar qo'llanadi
# ---------------------------------------------------------------------------
def test_apply_only_approved_changes():
    before = _row(DRAFT_ID).get_state()
    check("qo'llashdan oldin byudjet hali eski", before["adset"]["daily_budget"] == 100000.0)

    # Faqat budjet o'zgarishini tasdiqlaymiz (interests'ni EMAS)
    payload = {"changes": [
        {"path": "adset.daily_budget", "value": 150000},
        {"path": "campaign.status", "value": "PAUSED"},  # ruxsat etilmagan -- serverda qayta tekshiriladi
    ]}
    with _assets_mock(), mock.patch.object(meta_publish, "push_updates_to_meta") as push:
        push.return_value = {"pushed": ["adset.daily_budget"], "skipped": []}
        r = admin.post(f"/avtopilot/{DRAFT_ID}/target-analiz/qollash", json=payload)
    data = r.get_json()
    check("qo'llash 200", r.status_code == 200 and data["ok"] is True)
    check("faqat ruxsat etilgan yo'l qo'llandi", data["changed_paths"] == ["adset.daily_budget"])
    check("ruxsatsiz yo'l skipped'da (holat o'zgarmadi)", data["skipped"] == ["campaign.status"])
    check("push_updates_to_meta AYNAN shu yo'l bilan chaqirildi (boshqasi emas)", push.call_args[0][3] == ["adset.daily_budget"])
    after = _row(DRAFT_ID).get_state()
    check("byudjet DB'da yangilandi", after["adset"]["daily_budget"] == 150000.0)
    check("interests O'ZGARMADI (tanlanmagan edi)", after["adset"]["targeting"]["interests"] == [])
    check("campaign.status allowlist'da YO'Q -- strukturaviy himoyalangan", cd.is_allowed_path("campaign.status") is False)
    check("manba USER_OVERRIDDEN (tasdiqlangan taklif -- foydalanuvchi qarori)", _row(DRAFT_ID).get_field_sources().get("adset.daily_budget") == "USER_OVERRIDDEN")

    evs = _events(DRAFT_ID)
    check("audit: target_analysis_run va target_analysis_applied yozildi", any(a == "target_analysis_run" for _, a in evs) and any(a == "target_analysis_applied" for _, a in evs))

    # Bo'sh tanlov -- 400
    r = admin.post(f"/avtopilot/{DRAFT_ID}/target-analiz/qollash", json={"changes": []})
    check("bo'sh tanlov 400", r.status_code == 400)
    # Faqat ruxsatsiz yo'llar -- 400 (hech narsa qo'llab bo'lmaydi)
    r = admin.post(f"/avtopilot/{DRAFT_ID}/target-analiz/qollash", json={"changes": [{"path": "campaign.status", "value": "PAUSED"}]})
    check("faqat ruxsatsiz yo'llar -- 400", r.status_code == 400)

    # Push Meta'da xato bersa -- 400 friendly, lekin lokal o'zgarish ALLAQACHON saqlangan
    with _assets_mock(), mock.patch.object(meta_publish, "push_updates_to_meta", side_effect=meta_publish.PublishError("push", "Meta so'rovlar chegarasi -- birozdan keyin qayta urinib ko'ring.", "raw")):
        r = admin.post(f"/avtopilot/{DRAFT_ID}/target-analiz/qollash", json={"changes": [{"path": "adset.daily_budget", "value": 180000}]})
    check("Meta push xatosi -- 400 friendly (xom matn yo'q)", r.status_code == 400 and "raw" not in r.get_json()["error"] and "qayta urinib" in r.get_json()["error"])
    check("lokal o'zgarish baribir saqlangan (apply_patch push'dan OLDIN commit qiladi)", _row(DRAFT_ID).get_state()["adset"]["daily_budget"] == 180000.0)


# ---------------------------------------------------------------------------
# 5) LLM ishlamasa -- 503, sahifa yiqilmaydi
# ---------------------------------------------------------------------------
def test_llm_failure_returns_friendly_error():
    with _assets_mock(), mock.patch.object(meta_api, "get_campaign_insights", _sufficient_fake_insights()), \
            mock.patch.object(orchestrator, "_call_agent", side_effect=orchestrator.AgentUnavailableError("down")):
        r = admin.post(f"/avtopilot/{DRAFT_ID}/target-analiz", json={})
    check("LLM ishlamasa 503 + o'zbekcha xabar", r.status_code == 503 and "javob bera olmadi" in r.get_json()["error"])

    with _assets_mock(), mock.patch.object(meta_api, "get_campaign_insights", _sufficient_fake_insights()), \
            mock.patch.object(orchestrator, "_call_agent", side_effect=orchestrator.TargetologFormatError("erkin matn")):
        r = admin.post(f"/avtopilot/{DRAFT_ID}/target-analiz", json={})
    check("format xatosi ham 503 + o'zbekcha xabar (sahifa yiqilmadi)", r.status_code == 503 and "javob bera olmadi" in r.get_json()["error"])


# ---------------------------------------------------------------------------
# 6) Boshqa kompaniya (cross-tenant)
# ---------------------------------------------------------------------------
def test_cross_tenant_blocked():
    b = _client("ta_admin_b")
    r = b.post(f"/avtopilot/{DRAFT_ID}/target-analiz", json={})
    check("B: A qoralamasini diagnostika qila olmaydi (404)", r.status_code == 404)
    r = b.post(f"/avtopilot/{DRAFT_ID}/target-analiz/qollash", json={"changes": [{"path": "adset.daily_budget", "value": 1}]})
    check("B: A qoralamasiga o'zgarish qo'llay olmaydi (404)", r.status_code == 404)


if __name__ == "__main__":
    test_data_sufficiency_gate()
    test_invalid_path_dropped_and_names_resolved()
    test_apply_only_approved_changes()
    test_llm_failure_returns_friendly_error()
    test_cross_tenant_blocked()
    if failures:
        print("\nXATOLAR:", failures)
        sys.exit(1)
    print("\nBARCHA TESTLAR O'TDI (target_analysis)")
