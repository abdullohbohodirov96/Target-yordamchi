"""test_campaign_draft_offline.py — Meta Ads Autopilot (2026-09, foydalanuvchi
so'rovi: "Replix ... Ads Manager'dagi har bir maydonni boshidan o'zi
to'ldirmasligi kerak"): `campaign_draft.py`ning SOF (tarmoqsiz, bazasiz)
funksiyalarini tekshiradi:

  1. `new_empty_state()` -- to'liq sxema, maqsaddan hosila maydonlar.
  2. `apply_patch()` -- allowlist (campaign.status / adset.targeting.evil
     RAD etiladi), tip coercion, $append/$remove_index, manba (source)
     yozuvi.
  3. `approvals_after_change()` -- adset o'zgarsa FAQAT adset tasdig'i
     bekor; objective o'zgarsa uchalasi.
  4. `validate_state()` -- yosh teskari, hudud yo'q, media yo'q, SALES
     Pixel'siz, LEADS forma telefon/email/privacy'siz, MESSAGES
     INSTAGRAM_DIRECT IG'siz, begona page_id.
  5. `to_meta_targeting()` -- flexible_spec faqat qiziqish bo'lsa,
     placements faqat manual bo'lsa.
  6. `to_meta_creative_spec()` -- LEADS (lead_gen_form_id), MESSAGES
     (page_welcome_message JSON, <=4 ice breaker).
  7. Valyuta offset'lari.

Ishga tushirish:
    cd app && python3 scripts/test_campaign_draft_offline.py
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import campaign_draft as cd  # noqa: E402

failures = []


def check(name, cond):
    print(("OK  " if cond else "FAIL") + " " + name)
    if not cond:
        failures.append(name)


class FakeDraft:
    def __init__(self):
        self.campaign_approved = True
        self.adset_approved = True
        self.ad_approved = True


def _filled_state(objective="MESSAGES"):
    s = cd.new_empty_state(objective)
    s["campaign"]["name"] = "Replix | Test | " + objective
    s["adset"]["name"] = "Toshkent"
    s["adset"]["daily_budget"] = 200000
    s["adset"]["targeting"]["geo_locations"]["cities"] = [{"key": "2430536", "name": "Tashkent", "radius": 0, "distance_unit": "kilometer"}]
    s["ad"]["name"] = "Reklama 1"
    s["ad"]["page_id"] = "PAGE1"
    s["ad"]["media"]["image_hash"] = "abc123"
    s["ad"]["primary_text"] = "Salom"
    s["ad"]["headline"] = "Sarlavha"
    s["ad"]["messages"]["greeting"] = "Assalomu alaykum!"
    return s


def test_new_empty_state():
    s = cd.new_empty_state("LEADS")
    check("version=1", s["version"] == 1)
    check("meta_objective LEADS -> OUTCOME_LEADS", s["campaign"]["meta_objective"] == "OUTCOME_LEADS")
    check("optimization_goal LEAD_GENERATION", s["adset"]["optimization_goal"] == "LEAD_GENERATION")
    check("destination ON_AD", s["adset"]["destination_type"] == "ON_AD")
    check("cta SIGN_UP", s["ad"]["cta"] == "SIGN_UP")
    check("campaign status PAUSED", s["campaign"]["status"] == "PAUSED")
    for key in ("campaign", "adset", "ad"):
        check(f"{key} bloki bor", key in s)
    try:
        cd.new_empty_state("BOGUS")
        check("noma'lum maqsad rad etiladi", False)
    except cd.DraftPatchError:
        check("noma'lum maqsad rad etiladi", True)


def test_apply_patch_allowlist_and_coercion():
    s = _filled_state()
    for bad in ("campaign.status", "adset.targeting.evil", "campaign.meta_objective", "version", "adset.billing_event"):
        try:
            cd.apply_patch(s, {"scope": bad.split(".")[0] if "." in bad else "campaign", "changes": {bad: "X"}}, source="USER_OVERRIDDEN", field_sources={})
            check(f"{bad} rad etiladi", False)
        except cd.DraftPatchError:
            check(f"{bad} rad etiladi", True)
    check("is_allowed_path(adset.targeting.age_min)", cd.is_allowed_path("adset.targeting.age_min"))
    check("is_allowed_path(campaign.status) False", not cd.is_allowed_path("campaign.status"))

    new, changed, sources = cd.apply_patch(
        s, {"scope": "adset", "changes": {"targeting.age_min": "25", "targeting.age_max": 50, "targeting.genders": ["2"]}},
        source="USER_OVERRIDDEN", field_sources={"adset.targeting.age_min": "AI_RECOMMENDED"},
    )
    check("age_min coerced to int", new["adset"]["targeting"]["age_min"] == 25)
    check("age_max 50", new["adset"]["targeting"]["age_max"] == 50)
    check("genders coerced to ints", new["adset"]["targeting"]["genders"] == [2])
    check("changed paths full", set(changed) == {"adset.targeting.age_min", "adset.targeting.age_max", "adset.targeting.genders"})
    check("source USER_OVERRIDDEN", sources["adset.targeting.age_min"] == "USER_OVERRIDDEN")
    check("original state untouched", s["adset"]["targeting"]["age_min"] == 18)

    # scope mismatch
    try:
        cd.apply_patch(s, {"scope": "campaign", "changes": {"adset.name": "x"}}, source="USER_OVERRIDDEN", field_sources={})
        check("scope mismatch rad etiladi", False)
    except cd.DraftPatchError:
        check("scope mismatch rad etiladi", True)

    # enum coercion
    new, _, _ = cd.apply_patch(s, {"scope": "ad", "changes": {"cta": "message_page"}}, source="AI_RECOMMENDED", field_sources={})
    check("cta MESSAGE_PAGE -> SEND_MESSAGE", new["ad"]["cta"] == "SEND_MESSAGE")
    try:
        cd.apply_patch(s, {"scope": "ad", "changes": {"cta": "FLY_NOW"}}, source="AI_RECOMMENDED", field_sources={})
        check("noma'lum CTA rad etiladi", False)
    except cd.DraftPatchError:
        check("noma'lum CTA rad etiladi", True)

    # list ops
    s2 = cd.new_empty_state("LEADS")
    new, _, _ = cd.apply_patch(s2, {"scope": "ad", "changes": {"lead_form.new_form.questions": {"$append": {"type": "CUSTOM", "label": "Obyekt hajmi qancha?"}}}}, source="USER_OVERRIDDEN", field_sources={})
    qs = new["ad"]["lead_form"]["new_form"]["questions"]
    check("$append savol qo'shdi", len(qs) == 1 and qs[0]["type"] == "CUSTOM" and qs[0]["key"])
    new, _, _ = cd.apply_patch(new, {"scope": "ad", "changes": {"ad.lead_form.new_form.questions": {"$append": {"type": "PHONE"}}}}, source="USER_OVERRIDDEN", field_sources={})
    check("$append ikkinchi savol", len(new["ad"]["lead_form"]["new_form"]["questions"]) == 2)
    new, _, _ = cd.apply_patch(new, {"scope": "ad", "changes": {"ad.lead_form.new_form.questions": {"$remove_index": 0}}}, source="USER_OVERRIDDEN", field_sources={})
    check("$remove_index olib tashladi", [q["type"] for q in new["ad"]["lead_form"]["new_form"]["questions"]] == ["PHONE"])
    new, _, _ = cd.apply_patch(new, {"scope": "ad", "changes": {"messages.quick_replies": {"$append": "Narxi qancha?"}}}, source="USER_OVERRIDDEN", field_sources={})
    check("quick_replies $append", new["ad"]["messages"]["quick_replies"] == ["Narxi qancha?"])

    # objective change re-derives
    new, changed, _ = cd.apply_patch(s, {"scope": "objective", "changes": {"objective": "SALES"}}, source="USER_OVERRIDDEN", field_sources={})
    check("objective o'zgardi", new["objective"] == "SALES" and new["campaign"]["meta_objective"] == "OUTCOME_SALES")
    check("optimization OFFSITE_CONVERSIONS", new["adset"]["optimization_goal"] == "OFFSITE_CONVERSIONS")
    check("destination WEBSITE", new["adset"]["destination_type"] == "WEBSITE")

    # city without key rejected
    try:
        cd.apply_patch(s, {"scope": "adset", "changes": {"targeting.geo_locations.cities": [{"name": "Toshkent"}]}}, source="USER_OVERRIDDEN", field_sources={})
        check("key'siz shahar rad etiladi", False)
    except cd.DraftPatchError:
        check("key'siz shahar rad etiladi", True)


def test_approvals_after_change():
    d = FakeDraft()
    reset = cd.approvals_after_change(d, ["adset.targeting.age_min"])
    check("adset o'zgarsa faqat adset reset", reset == ["adset"] and d.campaign_approved and not d.adset_approved and d.ad_approved)
    d = FakeDraft()
    reset = cd.approvals_after_change(d, ["objective"])
    check("objective -> uchalasi reset", set(reset) == {"campaign", "adset", "ad"} and not (d.campaign_approved or d.adset_approved or d.ad_approved))
    d = FakeDraft()
    reset = cd.approvals_after_change(d, ["ad.headline", "campaign.name"])
    check("ad+campaign reset, adset qoladi", set(reset) == {"ad", "campaign"} and d.adset_approved)
    d = FakeDraft()
    d.ad_approved = False
    reset = cd.approvals_after_change(d, ["ad.headline"])
    check("allaqachon bekor bo'lgan reset ro'yxatga tushmaydi", reset == [])


def test_validate_state():
    ok = _filled_state()
    check("to'ldirilgan MESSAGES holati xatosiz", cd.validate_state(ok) == [])

    s = _filled_state()
    s["adset"]["targeting"]["age_min"] = 50
    s["adset"]["targeting"]["age_max"] = 30
    check("yosh teskari", any("Minimal yosh" in e["message"] for e in cd.validate_state(s)))

    s = _filled_state()
    s["adset"]["targeting"]["geo_locations"] = {"countries": [], "cities": [], "regions": []}
    check("hudud yo'q", any(e["field"] == "geo_locations" for e in cd.validate_state(s)))

    s = _filled_state()
    s["ad"]["media"] = {"media_id": None, "image_hash": None, "video_id": None, "selected_variant": None}
    check("media yo'q", any(e["field"] == "media" for e in cd.validate_state(s)))

    s = _filled_state("SALES")
    s["ad"]["link_url"] = "https://example.uz"
    errs = cd.validate_state(s, company=type("C", (), {"meta_pixel_id": None, "ig_business_id": "IG"})())
    check("SALES Pixel'siz", any("Pixel" in e["message"] for e in errs))
    errs = cd.validate_state(s, company=type("C", (), {"meta_pixel_id": "PX1", "ig_business_id": "IG"})())
    check("SALES Pixel bilan OK", not any("Pixel" in e["message"] for e in errs))

    s = _filled_state("LEADS")
    s["ad"]["lead_form"]["new_form"] = {"name": "Forma", "intro_headline": "", "intro_description": "", "questions": [{"type": "FULL_NAME"}, {"type": "CUSTOM", "label": "Hajmi?", "key": "hajmi"}], "privacy_url": "", "thank_you_title": "", "thank_you_body": ""}
    errs = cd.validate_state(s)
    check("LEADS telefon/email yo'q", any("telefon yoki email" in e["message"] for e in errs))
    check("LEADS privacy yo'q", any("privacy_url" in e["message"] or "Maxfiylik" in e["message"] for e in errs))
    s["ad"]["lead_form"]["new_form"]["questions"].append({"type": "PHONE"})
    s["ad"]["lead_form"]["new_form"]["privacy_url"] = "https://example.uz/privacy"
    check("LEADS forma to'g'ri -> xatosiz", cd.validate_state(s) == [])

    s = _filled_state("MESSAGES")
    s["adset"]["destination_type"] = "INSTAGRAM_DIRECT"
    errs = cd.validate_state(s, company=type("C", (), {"meta_pixel_id": None, "ig_business_id": None})())
    check("MESSAGES INSTAGRAM_DIRECT IG'siz", any("Instagram" in e["message"] for e in errs))
    errs = cd.validate_state(s, company=type("C", (), {"meta_pixel_id": None, "ig_business_id": "IG1"})())
    check("MESSAGES INSTAGRAM_DIRECT IG bilan OK", errs == [])

    s = _filled_state()
    errs = cd.validate_state(s, meta_assets={"pages": [{"id": "OTHER"}], "instagram_accounts": [{"id": "IG1"}], "custom_audiences": []})
    check("begona page_id", any("sahifa kompaniyaga ulanmagan" in e["message"] for e in errs))
    s["adset"]["targeting"]["custom_audiences"] = [{"id": "AUD9", "name": "Eski"}]
    errs = cd.validate_state(s, meta_assets={"pages": [{"id": "PAGE1"}], "instagram_accounts": [], "custom_audiences": [{"id": "AUD1"}]})
    check("begona custom audience", any("Auditoriya" in e["message"] for e in errs))

    s = _filled_state()
    s["adset"]["targeting"]["placements"] = {"mode": "manual", "publisher_platforms": ["tiktok"], "facebook_positions": [], "instagram_positions": []}
    check("manual placements noto'g'ri platforma", any("Platforma noto'g'ri" in e["message"] for e in cd.validate_state(s)))


def test_to_meta_targeting():
    s = _filled_state()
    t = cd.to_meta_targeting(s)
    check("cities key bilan", t["geo_locations"]["cities"][0]["key"] == "2430536")
    check("flexible_spec yo'q (qiziqish yo'q)", "flexible_spec" not in t)
    check("publisher_platforms yo'q (automatic)", "publisher_platforms" not in t)
    check("advantage_audience 1", t["targeting_automation"]["advantage_audience"] == 1)
    s["adset"]["targeting"]["interests"] = [{"id": "6003", "name": "Furniture"}]
    s["adset"]["targeting"]["placements"] = {"mode": "manual", "publisher_platforms": ["facebook"], "facebook_positions": ["feed"], "instagram_positions": []}
    s["adset"]["targeting"]["custom_audiences"] = [{"id": "AUD1", "name": "x"}]
    s["adset"]["targeting"]["advantage_audience"] = False
    t = cd.to_meta_targeting(s)
    check("flexible_spec interests", t["flexible_spec"] == [{"interests": [{"id": "6003", "name": "Furniture"}]}])
    check("manual placements yuboriladi", t["publisher_platforms"] == ["facebook"] and t["facebook_positions"] == ["feed"] and "instagram_positions" not in t)
    check("custom_audiences faqat id", t["custom_audiences"] == [{"id": "AUD1"}])
    check("advantage_audience 0", t["targeting_automation"]["advantage_audience"] == 0)


def test_to_meta_creative_spec():
    s = _filled_state("LEADS")
    spec = cd.to_meta_creative_spec(s, page_id="PAGE1", instagram_actor_id="IG1", image_hash="abc123", lead_form_id="FORM9")
    ld = spec["link_data"]
    check("LEADS: link_data image_hash", ld["image_hash"] == "abc123")
    check("LEADS: CTA SIGN_UP + lead_gen_form_id", ld["call_to_action"] == {"type": "SIGN_UP", "value": {"lead_gen_form_id": "FORM9"}})
    check("instagram_actor_id", spec["instagram_actor_id"] == "IG1" and spec["page_id"] == "PAGE1")
    check("headline -> name", ld["name"] == "Sarlavha" and ld["message"] == "Salom")

    s = _filled_state("MESSAGES")
    s["ad"]["messages"]["quick_replies"] = ["1", "2", "3", "4", "5", "6"]
    spec = cd.to_meta_creative_spec(s, page_id="PAGE1", image_hash="abc123")
    ld = spec["link_data"]
    check("MESSAGES: CTA MESSAGE_PAGE", ld["call_to_action"]["type"] == "MESSAGE_PAGE")
    check("MESSAGES: app_destination MESSENGER", ld["call_to_action"]["value"] == {"app_destination": "MESSENGER"})
    pwm = json.loads(ld["page_welcome_message"])
    check("page_welcome_message JSON parse", pwm["type"] == "VISUAL_EDITOR" and pwm["landing_screen_type"] == "welcome_message")
    check("<=4 ice breakers", len(pwm["text_format"]["message"]["ice_breakers"]) == 4)
    check("greeting text", pwm["text_format"]["message"]["text"] == "Assalomu alaykum!")

    s["ad"]["media"]["video_id"] = "VID1"
    spec = cd.to_meta_creative_spec(s, page_id="PAGE1", image_hash="abc123", video_id="VID1")
    check("video -> video_data", "video_data" in spec and spec["video_data"]["video_id"] == "VID1" and "link_data" not in spec)


def test_currency():
    check("UZS 200000 -> 20000000", cd.to_minor_units(200000, "UZS") == 20000000)
    check("JPY 1000 -> 1000", cd.to_minor_units(1000, "JPY") == 1000)
    check("USD 10.5 -> 1050", cd.to_minor_units(10.5, "usd") == 1050)
    check("from_minor UZS", cd.from_minor_units(20000000, "UZS") == 200000.0)
    check("from_minor KRW", cd.from_minor_units("5000", "KRW") == 5000.0)


def test_summary_and_unsupported():
    s = _filled_state()
    s["adset"]["duration_days"] = 7
    summ = cd.summary_for_review(s, {"company_name": "Test"})
    check("summary budget line", "200 000 UZS" in summ["budget_line"])
    check("summary estimated total", summ["estimated_total_budget"] == 1400000)
    check("summary locations", summ["locations"] == ["Tashkent"])
    check("summary warnings bo'sh", summ["warnings"] == [])
    check("META_UNSUPPORTED_UI_FIELDS label", all(f["note"] == "Meta API orqali boshqarilmaydi" for f in cd.META_UNSUPPORTED_UI_FIELDS))
    cfg = cd.lead_form_config_from_state(_filled_state("LEADS"))
    check("lead_form_config privacy_policy kaliti", "privacy_policy" in cfg and "questions" in cfg)


def test_lead_form_question_types_and_multiple_choice():
    # 2026-09, foydalanuvchi so'rovi ("ads menejerda instant forum
    # yaratayotganingda to'liq hali bor... multiplay choice bor va
    # boshqalar... shularni hammasini to'liq qil"): kengaytirilgan savol
    # turlari ro'yxati va CUSTOM savolga "options" (bir nechta variant).
    check("kengaytirilgan ro'yxatda CITY/COMPANY_NAME bor", {"CITY", "COMPANY_NAME", "WORK_EMAIL"} <= cd.LEAD_QUESTION_TYPES)
    check("LEAD_QUESTION_TYPE_LABELS hamma turni qamraydi", set(cd.LEAD_QUESTION_TYPES) == set(cd.LEAD_QUESTION_TYPE_LABELS))

    s2 = cd.new_empty_state("LEADS")
    new, _, _ = cd.apply_patch(s2, {"scope": "ad", "changes": {"lead_form.new_form.questions": {"$append": {"type": "CITY"}}}}, source="USER_OVERRIDDEN", field_sources={})
    check("yangi standart tur (CITY) qabul qilinadi", new["ad"]["lead_form"]["new_form"]["questions"][-1]["type"] == "CITY")

    try:
        cd.apply_patch(s2, {"scope": "ad", "changes": {"lead_form.new_form.questions": {"$append": {"type": "SHOE_SIZE"}}}}, source="USER_OVERRIDDEN", field_sources={})
        check("noma'lum savol turi rad etiladi", False)
    except cd.DraftPatchError:
        check("noma'lum savol turi rad etiladi", True)

    # CUSTOM + options -> multiple choice savol
    new, _, _ = cd.apply_patch(s2, {"scope": "ad", "changes": {"lead_form.new_form.questions": {"$append": {"type": "CUSTOM", "label": "Qaysi hajm kerak?", "options": ["Kichik", " O'rta ", "Katta", "  "]}}}}, source="USER_OVERRIDDEN", field_sources={})
    q = new["ad"]["lead_form"]["new_form"]["questions"][-1]
    # 2026-09 bugfix: bo'sh variant tahrir paytida SAQLANADI (uzunlik 4), faqat
    # matn strip qilinadi -- filtrlash nashr-oldi validatsiyasida.
    check("multiple-choice savolda options saqlandi (bo'sh qator ham -- tahrir paytida filtrlanmaydi)", q.get("options") == ["Kichik", "O'rta", "Katta", ""])

    # 2026-09 REGRESSIYA ("+ Variant qo'shish ishlamayapti"): UI har bosishda
    # `options: ["", ""]` bilan darhol patch yuboradi va serverdan qaytgan
    # holat lokalni to'liq almashtiradi. Bo'sh variantlar shu yerda
    # filtrlansa -- foydalanuvchi yozib ulgurmasdan qator yo'qolardi.
    new_blank, _, _ = cd.apply_patch(s2, {"scope": "ad", "changes": {"lead_form.new_form.questions": [{"type": "PHONE"}, {"type": "CUSTOM", "label": "Qaysi xizmat?", "options": ["", ""]}]}}, source="USER_OVERRIDDEN", field_sources={})
    q_blank = new_blank["ad"]["lead_form"]["new_form"]["questions"][-1]
    check("REGRESSIYA: ikkita bo'sh variant patch'dan keyin 2 ta bo'lib qoladi (0 ga tushmaydi)", q_blank.get("options") == ["", ""])
    # ... lekin shu holatda nashr qilib bo'lmaydi -- validatsiya "kamida 2 ta variant" deydi
    s_blank = _filled_state("LEADS")
    s_blank["ad"]["lead_form"]["new_form"] = {
        "name": "Forma", "intro_headline": "", "intro_description": "",
        "questions": new_blank["ad"]["lead_form"]["new_form"]["questions"],
        "privacy_url": "https://example.uz/privacy", "thank_you_title": "", "thank_you_body": "",
    }
    errs_blank = cd.validate_state(s_blank)
    check("REGRESSIYA: bo'sh variantlar bilan nashr-oldi validatsiya 'kamida 2 ta variant' deydi", any("kamida 2 ta variant" in e["message"] for e in errs_blank))
    s_blank["ad"]["lead_form"]["new_form"]["questions"][-1]["options"] = ["Ta'mirlash", ""]
    check("REGRESSIYA: 1 ta to'ldirilgan + 1 bo'sh -- hali ham xato", any("kamida 2 ta variant" in e["message"] for e in cd.validate_state(s_blank)))
    s_blank["ad"]["lead_form"]["new_form"]["questions"][-1]["options"] = ["Ta'mirlash", "Sotib olish", ""]
    check("REGRESSIYA: 2 ta to'ldirilgan + 1 bo'sh -- xatosiz (bo'sh e'tiborsiz)", not any("variant" in e["message"] for e in cd.validate_state(s_blank)))
    cfg_blank = cd.lead_form_config_from_state(s_blank)
    q_cfg = next(qq for qq in cfg_blank["questions"] if qq["type"] == "CUSTOM")
    check("REGRESSIYA: Meta config'ga bo'sh variant KETMAYDI", [o["value"] for o in q_cfg["options"]] == ["Ta'mirlash", "Sotib olish"])
    # 12 tadan ko'p variant rad etiladi
    try:
        cd.apply_patch(s2, {"scope": "ad", "changes": {"lead_form.new_form.questions": {"$append": {"type": "CUSTOM", "label": "Ko'p", "options": [str(i) for i in range(13)]}}}}, source="USER_OVERRIDDEN", field_sources={})
        check("13 ta variant rad etiladi", False)
    except cd.DraftPatchError as e:
        check("13 ta variant rad etiladi", "12 ta" in str(e))
    # EMAIL turi hali ham QABUL qilinadi (Meta'dan import qilingan eski forma
    # buzilmasin) -- faqat UI'da yangi qo'shishda taklif qilinmaydi
    new_email, _, _ = cd.apply_patch(s2, {"scope": "ad", "changes": {"lead_form.new_form.questions": {"$append": {"type": "EMAIL"}}}}, source="USER_OVERRIDDEN", field_sources={})
    check("EMAIL turi backend'da hali ham qabul qilinadi (import uchun)", new_email["ad"]["lead_form"]["new_form"]["questions"][-1]["type"] == "EMAIL")
    check("EMAIL yorlig'i saqlangan", cd.LEAD_QUESTION_TYPE_LABELS.get("EMAIL") == "Email")
    # PHONE yolg'iz (EMAIL'siz) validatsiyadan o'tadi
    s_ph = _filled_state("LEADS")
    s_ph["ad"]["lead_form"]["new_form"] = {"name": "F", "intro_headline": "", "intro_description": "", "questions": [{"type": "FULL_NAME"}, {"type": "PHONE"}], "privacy_url": "https://example.uz/p", "thank_you_title": "", "thank_you_body": ""}
    check("PHONE yolg'iz (EMAIL'siz) LEADS forma xatosiz", cd.validate_state(s_ph) == [])

    # options'siz CUSTOM -- hali ham erkin matnli savol (eski xatti-harakat)
    new2, _, _ = cd.apply_patch(s2, {"scope": "ad", "changes": {"lead_form.new_form.questions": {"$append": {"type": "CUSTOM", "label": "Izoh"}}}}, source="USER_OVERRIDDEN", field_sources={})
    check("options'siz CUSTOM hali ham erkin matn (kalit yo'q)", "options" not in new2["ad"]["lead_form"]["new_form"]["questions"][-1])

    # validate_state: kamida 2 ta variant kerak
    s = _filled_state("LEADS")
    s["ad"]["lead_form"]["new_form"] = {
        "name": "Forma", "intro_headline": "", "intro_description": "",
        "questions": [{"type": "PHONE"}, {"type": "CUSTOM", "label": "Hajmi?", "options": ["Kichik"]}],
        "privacy_url": "https://example.uz/privacy", "thank_you_title": "", "thank_you_body": "",
    }
    errs = cd.validate_state(s)
    check("1 ta variant bilan xato beradi", any("kamida 2 ta variant" in e["message"] for e in errs))
    s["ad"]["lead_form"]["new_form"]["questions"][1]["options"] = ["Kichik", "Katta"]
    check("2 ta variant bilan xatosiz", cd.validate_state(s) == [])

    # lead_form_config_from_state -> Meta options={key,value} shakliga o'giradi
    cfg = cd.lead_form_config_from_state(s)
    custom_q = next(q for q in cfg["questions"] if q["type"] == "CUSTOM")
    check("Meta config'da options key/value juftliklari", custom_q["options"] == [{"key": "kichik", "value": "Kichik"}, {"key": "katta", "value": "Katta"}])

    # options'siz CUSTOM -- Meta config'da "options" kaliti umuman yo'q
    s["ad"]["lead_form"]["new_form"]["questions"][1] = {"type": "CUSTOM", "label": "Izoh"}
    cfg2 = cd.lead_form_config_from_state(s)
    custom_q2 = next(q for q in cfg2["questions"] if q["type"] == "CUSTOM")
    check("options'siz CUSTOM Meta config'da 'options' kaliti yo'q", "options" not in custom_q2)


test_new_empty_state()
test_apply_patch_allowlist_and_coercion()
test_approvals_after_change()
test_validate_state()
test_to_meta_targeting()
test_to_meta_creative_spec()
test_currency()
test_summary_and_unsupported()
test_lead_form_question_types_and_multiple_choice()

print()
if failures:
    print(f"MUVAFFAQIYATSIZ: {len(failures)} ta test o'tmadi:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("BARCHA TESTLAR O'TDI (campaign_draft)")
