"""test_ai_campaign_planner_offline.py — Meta Ads Autopilot (2026-09,
foydalanuvchi so'rovi: "Replix ... Ads Manager'dagi har bir maydonni
boshidan o'zi to'ldirmasligi kerak"): `ai_campaign_planner.py` --
LLM (`orchestrator._call_agent`) va Meta qidiruvlari MOCK qilinadi:

  1. `plan_campaign()`: LLM 2 ta hudud (bittasi uydirma) va 3 ta qiziqish
     (bittasi topilmaydigan) qaytaradi -> holat quriladi, uydirmalar
     tashlab yuboriladi + ogohlantirish, `meta_objective` LLM noto'g'ri
     aytsa ham `OBJECTIVE_META`dan, byudjet javobdan, barcha manbalar
     AI_RECOMMENDED.
  2. `missing_questions()`: byudjet so'raladi, hudud kontekstda bo'lsa
     so'ralmaydi.
  3. `chat_edit()`: ruxsat etilgan patch o'tadi; ruxsat etilmagan yo'l ->
     patch None + javob; nom bilan kelgan shahar key'ga aylanadi.
  4. `replan_preserving_overrides()` USER_OVERRIDDEN qiymatni saqlaydi.
  5. LLM ishlamasa -> `PlannerUnavailableError`.

Ishga tushirish:
    cd app && python3 scripts/test_ai_campaign_planner_offline.py
"""
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("META_ACCESS_TOKEN", "test-dummy-token")
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_test_dummy")
os.environ.setdefault("META_PAGE_ID", "page_test_dummy")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import orchestrator  # noqa: E402
import campaign_draft as cd  # noqa: E402
import ai_campaign_planner as planner  # noqa: E402

failures = []


def check(name, cond):
    print(("OK  " if cond else "FAIL") + " " + name)
    if not cond:
        failures.append(name)


CTX = {
    "company_id": 1, "company_name": "Nur Mebel", "plan": "business",
    "business_category": {"key": "furniture_interior", "label": "Mebel / interyer"}, "business_category_note": "",
    "profile_answers": {"product_or_service": "Oshxona mebeli", "target_audience": "25-45 yosh, Toshkent"},
    "profile_summary_text": "...",
    "meta_assets": {"ad_account_id": "act_1", "page_id": "PAGE1", "ig_business_id": "IG1", "pixel_id": None, "business_id": None,
                    "has_ad_account": True, "has_page": True, "has_instagram": True, "has_pixel": False, "has_business": False},
    "default_location": "Tashkent",
    "recent_performance": {"leads_last_30d": 5, "top_ads": []},
    "missing_fields": [],
}
GEO_DB = {"tashkent": {"key": "2430536", "name": "Tashkent", "type": "city"}, "chirchiq": {"key": "2430600", "name": "Chirchiq", "type": "city"},
          "uzbekistan": {"key": "UZ", "name": "Uzbekistan", "type": "country"}}
INTEREST_DB = {"furniture": [{"id": "6003", "name": "Furniture"}], "interior design": [{"id": "6004", "name": "Interior design"}]}


def fake_geo(name):
    return GEO_DB.get(name.lower())


def fake_interests(name):
    return INTEREST_DB.get(name.lower(), [])


LLM_PLAN = {
    "campaign_name": "Replix | Nur Mebel | MESSAGES | Toshkent | Sep26",
    "adset_name": "Toshkent | 25-45", "ad_name": "Oshxona mebeli | v1",
    "meta_objective": "OUTCOME_SALES",  # ATAYLAB noto'g'ri -- tizim e'tiborsiz qoldirishi kerak
    "optimization_goal": "LINK_CLICKS",
    "age_min": 10, "age_max": 80, "genders": [2],
    "locations": ["Tashkent", "Atlantis"],
    "interests": ["Furniture", "Interior design", "Unicorn riding"],
    "advantage_audience": False, "placements_mode": "automatic",
    "destination_type": "INSTAGRAM_DIRECT",
    "primary_text_variants": ["Matn 1", "Matn 2", "Matn 3"], "headline_variants": ["S1", "S2", "S3"], "description_variants": ["D1", "D2", "D3"],
    "cta": "SEND_MESSAGE",
    "messages": {"greeting": "Assalomu alaykum!", "quick_replies": ["Narxi qancha?", "Manzil?", "Muddat?", "Kafolat?", "Yetkazish?"]},
    "reasoning_summary": "Mebel uchun ayollar 25-45.",
    "explanations": {"adset.targeting.age_min": "Profil bo'yicha"}, "confidence": {"adset.targeting.age_min": 85},
    "warnings": [],
}


def test_plan_campaign():
    with mock.patch.object(orchestrator, "_call_agent", return_value=dict(LLM_PLAN)) as m:
        res = planner.plan_campaign(CTX, {"objective": "messages", "budget": "200 000", "duration_days": 10},
                                    meta_assets={"ad_account": {"currency": "UZS"}, "pages": [{"id": "PAGE1"}]},
                                    resolve_geo=fake_geo, resolve_interests=fake_interests)
    check("LLM bir marta chaqirildi", m.call_count == 1)
    check("prompt kompaniya kontekstini o'z ichiga oladi", "KOMPANIYA KONTEKSTI" in m.call_args[0][1] and "Nur Mebel" in m.call_args[0][1])
    s = res["state"]
    t = s["adset"]["targeting"]
    check("meta_objective OBJECTIVE_META'dan (LLM'ni e'tiborsiz)", s["campaign"]["meta_objective"] == "OUTCOME_ENGAGEMENT")
    check("optimization_goal CONVERSATIONS", s["adset"]["optimization_goal"] == "CONVERSATIONS")
    check("byudjet javobdan 200000", s["adset"]["daily_budget"] == 200000.0 and s["adset"]["currency"] == "UZS")
    check("muddat 10 kun, start/end bor", s["adset"]["duration_days"] == 10 and s["adset"]["start_time"] and s["adset"]["end_time"])
    check("uydirma hudud tashlandi", [c["key"] for c in t["geo_locations"]["cities"]] == ["2430536"])
    check("uydirma hudud ogohlantirish", any("Atlantis" in w for w in res["plan"]["warnings"]))
    check("yaroqsiz qiziqish tashlandi", [i["id"] for i in t["interests"]] == ["6003", "6004"])
    check("qiziqish ogohlantirish", any("Unicorn" in w for w in res["plan"]["warnings"]))
    check("yosh clamp 13-65", t["age_min"] == 13 and t["age_max"] == 65)
    check("genders [2]", t["genders"] == [2])
    check("destination INSTAGRAM_DIRECT (IG bor)", s["adset"]["destination_type"] == "INSTAGRAM_DIRECT")
    check("page/ig aktivlardan", s["ad"]["page_id"] == "PAGE1" and s["ad"]["instagram_actor_id"] == "IG1")
    check("copy variants 3 ta, birinchisi tanlangan", len(s["ad"]["copy_variants"]["primary_text"]) == 3 and s["ad"]["primary_text"] == "Matn 1")
    check("quick_replies 5 ta", len(s["ad"]["messages"]["quick_replies"]) == 5)
    check("cta SEND_MESSAGE", s["ad"]["cta"] == "SEND_MESSAGE")
    check("field_sources hammasi AI_RECOMMENDED", res["field_sources"] and set(res["field_sources"].values()) == {"AI_RECOMMENDED"})
    check("plan explanations/confidence", res["plan"]["explanations"]["adset.targeting.age_min"] == "Profil bo'yicha" and res["plan"]["confidence"]["adset.targeting.age_min"] == 85)
    check("holat validate_state'dan o'tadi (media'dan tashqari)", [e["field"] for e in cd.validate_state(s, company=type("C", (), {"ig_business_id": "IG1", "meta_pixel_id": None})())] == ["media"])

    # LEADS: forma savollari + PHONE majburiy
    leads_plan = dict(LLM_PLAN, lead_form={"name": "Forma", "questions": [{"type": "CUSTOM", "label": "Obyekt hajmi?"}], "intro_headline": "x"})
    with mock.patch.object(orchestrator, "_call_agent", return_value=leads_plan):
        res = planner.plan_campaign(CTX, {"objective": "LEADS", "budget": 100000}, meta_assets=None, resolve_geo=fake_geo, resolve_interests=fake_interests)
    qs = res["state"]["ad"]["lead_form"]["new_form"]["questions"]
    check("LEADS: PHONE va FULL_NAME avtomatik qo'shildi", [q["type"] for q in qs][:2] == ["FULL_NAME", "PHONE"] and qs[-1]["type"] == "CUSTOM")
    check("LEADS: privacy ogohlantirish", any("privacy_url" in w for w in res["plan"]["warnings"]))
    check("LEADS: muddat standart 7", res["state"]["adset"]["duration_days"] == 7)

    # Hech bir qiziqish topilmasa -> advantage_audience True
    with mock.patch.object(orchestrator, "_call_agent", return_value=dict(LLM_PLAN, interests=["Nothing"], advantage_audience=False)):
        res = planner.plan_campaign(CTX, {"objective": "MESSAGES", "budget": 1}, meta_assets=None, resolve_geo=fake_geo, resolve_interests=fake_interests)
    check("qiziqish topilmasa advantage_audience True", res["state"]["adset"]["targeting"]["advantage_audience"] is True)


def test_missing_questions():
    q = planner.missing_questions(CTX, {"objective": "MESSAGES"})
    check("byudjet so'raladi", [x["key"] for x in q] == ["budget"])
    q = planner.missing_questions(dict(CTX, default_location=None), {"objective": "MESSAGES", "budget": 100})
    check("hudud kontekstsiz so'raladi", [x["key"] for x in q] == ["locations"])
    q = planner.missing_questions(dict(CTX, default_location=None), {})
    check("hammasi bo'sh -> objective, budget, locations", [x["key"] for x in q] == ["objective", "budget", "locations"])
    q = planner.missing_questions(CTX, {"objective": "MESSAGES", "budget": 100})
    check("muddat hech qachon so'ralmaydi", q == [])


def _state():
    s = cd.new_empty_state("MESSAGES")
    s["adset"]["targeting"]["geo_locations"]["cities"] = [{"key": "2430536", "name": "Tashkent", "radius": 0, "distance_unit": "kilometer"}]
    return s


def test_chat_edit():
    with mock.patch.object(orchestrator, "_call_agent", return_value={"scope": "adset", "changes": {"targeting.age_min": 30, "targeting.age_max": 50}, "reply": "Yosh 30-50.", "clarify": False}):
        res = planner.chat_edit(CTX, _state(), "Yoshni 30-50 qil", resolve_geo=fake_geo, resolve_interests=fake_interests)
    check("ruxsat etilgan patch", res["patch"] == {"scope": "adset", "changes": {"adset.targeting.age_min": 30, "adset.targeting.age_max": 50}})
    check("reply", res["reply"] == "Yosh 30-50." and res["clarify"] is False)
    new, changed, _ = cd.apply_patch(_state(), res["patch"], source="USER_OVERRIDDEN", field_sources={})
    check("patch apply_patch'dan o'tadi", new["adset"]["targeting"]["age_min"] == 30)

    with mock.patch.object(orchestrator, "_call_agent", return_value={"scope": "campaign", "changes": {"campaign.status": "ACTIVE"}, "reply": "Yoqdim.", "clarify": False}):
        res = planner.chat_edit(CTX, _state(), "Kampaniyani yoq", resolve_geo=fake_geo, resolve_interests=fake_interests)
    check("ruxsat etilmagan yo'l -> patch None", res["patch"] is None and "campaign.status" in res["reply"])

    with mock.patch.object(orchestrator, "_call_agent", return_value={"scope": "adset", "changes": {"adset.targeting.geo_locations.cities": [{"name": "Tashkent"}, {"name": "Chirchiq"}, {"name": "Narnia"}]}, "reply": "Qo'shildi.", "clarify": False}):
        res = planner.chat_edit(CTX, _state(), "Toshkent va Chirchiqni qo'sh", resolve_geo=fake_geo, resolve_interests=fake_interests)
    cities = res["patch"]["changes"]["adset.targeting.geo_locations.cities"]
    check("nomlar key'ga aylandi (mavjud Tashkent key'i saqlanadi)", [c["key"] for c in cities] == ["2430536", "2430600"])
    check("topilmagan shahar ogohlantirish", any("Narnia" in w for w in res["warnings"]))

    with mock.patch.object(orchestrator, "_call_agent", return_value={"scope": "adset", "changes": {"adset.targeting.interests": [], "adset.targeting.advantage_audience": True}, "reply": "Broad.", "clarify": False}):
        res = planner.chat_edit(CTX, _state(), "Buni broad qil", resolve_geo=fake_geo, resolve_interests=fake_interests)
    check("broad patch", res["patch"]["changes"] == {"adset.targeting.interests": [], "adset.targeting.advantage_audience": True})

    with mock.patch.object(orchestrator, "_call_agent", return_value={"scope": "ad", "changes": {"ad.lead_form.new_form.questions": {"$append": {"type": "CUSTOM", "label": "Obyekt hajmi?"}}}, "reply": "Savol qo'shildi.", "clarify": False}):
        res = planner.chat_edit(CTX, cd.new_empty_state("LEADS"), "Lead formga obyekt hajmi degan savol qo'sh", resolve_geo=fake_geo, resolve_interests=fake_interests)
    check("$append patch o'tadi", "$append" in res["patch"]["changes"]["ad.lead_form.new_form.questions"])

    with mock.patch.object(orchestrator, "_call_agent", return_value={"scope": None, "changes": {}, "reply": "Qaysi shahar?", "clarify": True}):
        res = planner.chat_edit(CTX, _state(), "shaharni o'zgartir", resolve_geo=fake_geo, resolve_interests=fake_interests)
    check("clarify -> patch None", res["patch"] is None and res["clarify"] is True)

    with mock.patch.object(orchestrator, "_call_agent", return_value={"scope": "adset", "changes": {"adset.targeting.age_min": "yigirma"}, "reply": "x", "clarify": False}):
        res = planner.chat_edit(CTX, _state(), "yoshni yigirma qil", resolve_geo=fake_geo, resolve_interests=fake_interests)
    check("noto'g'ri tip -> patch None", res["patch"] is None and "qo'llab bo'lmadi" in res["reply"])


def test_replan_preserving_overrides():
    old = _state()
    old["adset"]["targeting"]["age_min"] = 30
    old["adset"]["daily_budget"] = 500000
    new_plan = _state()
    new_plan["adset"]["targeting"]["age_min"] = 18
    new_plan["adset"]["daily_budget"] = 100000
    new_plan["ad"]["headline"] = "Yangi"
    merged = planner.replan_preserving_overrides(old, {"adset.targeting.age_min": "USER_OVERRIDDEN", "adset.daily_budget": "AI_RECOMMENDED"}, new_plan)
    check("USER_OVERRIDDEN saqlanadi", merged["adset"]["targeting"]["age_min"] == 30)
    check("AI_RECOMMENDED yangilanadi", merged["adset"]["daily_budget"] == 100000)
    check("yangi maydonlar qoladi", merged["ad"]["headline"] == "Yangi")
    check("new_plan o'zgarmaydi", new_plan["adset"]["targeting"]["age_min"] == 18)


def test_llm_failure():
    with mock.patch.object(orchestrator, "_call_agent", side_effect=orchestrator.TargetologFormatError("erkin matn")):
        try:
            planner.plan_campaign(CTX, {"objective": "MESSAGES", "budget": 1000}, meta_assets=None, resolve_geo=fake_geo, resolve_interests=fake_interests)
            check("FormatError -> PlannerUnavailableError", False)
        except planner.PlannerUnavailableError as e:
            check("FormatError -> PlannerUnavailableError", "AI rejalashtiruvchi" in str(e))
    with mock.patch.object(orchestrator, "_call_agent", side_effect=orchestrator.AgentUnavailableError("down")):
        try:
            planner.chat_edit(CTX, _state(), "Yoshni 25-50 qil", resolve_geo=fake_geo, resolve_interests=fake_interests)
            check("AgentUnavailable -> PlannerUnavailableError (chat)", False)
        except planner.PlannerUnavailableError:
            check("AgentUnavailable -> PlannerUnavailableError (chat)", True)
    try:
        planner.plan_campaign(CTX, {"objective": "MESSAGES"}, meta_assets=None, resolve_geo=fake_geo, resolve_interests=fake_interests)
        check("byudjetsiz -> DraftPatchError", False)
    except cd.DraftPatchError:
        check("byudjetsiz -> DraftPatchError", True)


test_plan_campaign()
test_missing_questions()
test_chat_edit()
test_replan_preserving_overrides()
test_llm_failure()

print()
if failures:
    print(f"MUVAFFAQIYATSIZ: {len(failures)} ta test o'tmadi:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("BARCHA TESTLAR O'TDI (ai_campaign_planner)")
