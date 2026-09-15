"""ai_campaign_planner.py — Meta Ads Autopilot: AI REJALASHTIRUVCHI va
CHAT-TAHRIRLOVCHI (2026-09, foydalanuvchi so'rovi: "Replix ... Ads
Manager'dagi har bir maydonni boshidan o'zi to'ldirmasligi kerak").

Ikki vazifa:
  1. `plan_campaign()` -- kompaniya konteksti (`company_context`) + bir
     nechta wizard javobi (maqsad, byudjet, hudud, muddat) asosida LLM'dan
     TO'LIQ reja oladi, so'ng DETERMINISTIK post-process qiladi:
     `meta_objective`/`optimization_goal` HECH QACHON LLM'dan olinmaydi
     (`campaign_draft.OBJECTIVE_META`), hudud/qiziqish NOMLARI faqat Meta
     qidiruv callback'lari orqali haqiqiy key/id'ga aylanadi (topilmasa --
     tashlab yuboriladi + ogohlantirish, uydirma ID YO'Q), yosh
     chegaralanadi, byudjet javobdan, sahifa/IG kompaniya aktivlaridan.
  2. `chat_edit()` -- "Yoshni 25-50 qil", "Faqat Toshkent qil" kabi
     buyruqni STRUKTURALI patch'ga ({"scope","changes"}) aylantiradi; har
     bir yo'l `campaign_draft.is_allowed_path()` bilan tekshiriladi, nomlar
     callback orqali id'ga aylanadi. LLM chiqishi HECH QACHON to'g'ridan-
     to'g'ri Meta/bazaga tushmaydi -- faqat `campaign_draft.apply_patch` +
     `validate_state` orqali.

LLM chaqiruvi -- `orchestrator._call_agent` (Anthropic, OpenAI zaxira
yo'li bilan). Ikkala provayder ham ishlamasa yoki JSON kelmasa --
`PlannerUnavailableError` (o'zbekcha) ko'tariladi; UMUMIY "shablon" reja
JIMGINA tuzilmaydi (foydalanuvchi buni kutmaydi).
"""

import copy
import logging
import datetime as dt

import campaign_draft
import company_context as company_context_module

logger = logging.getLogger("ai_campaign_planner")


class PlannerUnavailableError(Exception):
    """AI rejalashtiruvchi ishlamadi (provayder xatosi yoki JSON bo'lmagan
    javob). Xabar o'zbekcha, foydalanuvchiga to'g'ridan-to'g'ri ko'rsatiladi."""


_PLANNER_UNAVAILABLE_MSG = (
    "AI rejalashtiruvchi hozir javob bera olmadi. Birozdan keyin qayta urinib "
    "ko'ring yoki kampaniyani qo'lda to'ldiring."
)


def _llm(system_prompt: str, user_content: str) -> dict:
    """`orchestrator._call_agent` uchun yupqa o'ram -- import chaqiruv
    paytida (modul importida `ANTHROPIC_API_KEY` talab qilinmasligi uchun;
    testlar `orchestrator._call_agent`ni mock qiladi)."""
    import orchestrator
    try:
        return orchestrator._call_agent(system_prompt, user_content)
    except (orchestrator.TargetologFormatError, orchestrator.AgentUnavailableError) as e:
        logger.warning("AI planner: LLM javob bermadi (%s)", type(e).__name__)
        raise PlannerUnavailableError(_PLANNER_UNAVAILABLE_MSG) from e


# ---------------------------------------------------------------------------
# WIZARD SAVOLLARI -- planner qaysilari HALI kerakligini o'zi hal qiladi
# (`missing_questions`). Maqsad va byudjet HAR DOIM so'raladi (byudjetni
# taxmin qilib bo'lmaydi); hudud -- faqat profilda standart hudud bo'lmasa;
# muddat -- so'ralmaydi (standart 7 kun); qolganlari ixtiyoriy.
# ---------------------------------------------------------------------------
WIZARD_QUESTIONS = [
    {"key": "objective", "question": "Reklamadan asosiy maqsadingiz nima?",
     "options": [{"value": o, "label": campaign_draft.OBJECTIVE_LABELS[o]} for o in campaign_draft.OBJECTIVES],
     "why": "Maqsad Meta'dagi kampaniya turini, optimallashtirish va tugmani belgilaydi."},
    {"key": "budget", "question": "Kunlik byudjet qancha (reklama hisobi valyutasida)?",
     "why": "Byudjetni AI taxmin qila olmaydi -- bu sizning qaroringiz."},
    {"key": "locations", "question": "Qaysi shahar/hududda ko'rsatilsin?",
     "why": "Profilda hudud ko'rsatilmagan -- reklama qayerda chiqishini bilish shart."},
    {"key": "duration_days", "question": "Necha kun davom etsin?", "why": "Standart 7 kun."},
    {"key": "product_focus", "question": "Aynan qaysi mahsulot/xizmatni reklama qilamiz?", "why": "Ixtiyoriy -- bo'sh bo'lsa profil bo'yicha eng ko'p sotiladigani olinadi."},
    {"key": "media", "question": "Rasm yoki video yuklang", "why": "Reklama uchun vizual shart (AI rasm yaratmaydi)."},
]
_WIZARD_BY_KEY = {q["key"]: q for q in WIZARD_QUESTIONS}
DEFAULT_DURATION_DAYS = 7


def missing_questions(ctx: dict, answers: "dict | None") -> list[dict]:
    """Reja tuzishdan oldin HALI so'ralishi kerak bo'lgan savollar.
    Maqsad va byudjet -- har doim (javob bo'lmasa); hudud -- faqat
    kontekstda standart hudud bo'lmasa va javobda ham bo'lmasa; muddat va
    boshqalar -- hech qachon so'ralmaydi (standart/ixtiyoriy)."""
    answers = answers or {}
    out: list[dict] = []
    objective = str(answers.get("objective") or "").upper()
    if objective not in campaign_draft.OBJECTIVE_META:
        out.append(_WIZARD_BY_KEY["objective"])
    budget = answers.get("budget")
    try:
        budget_ok = budget is not None and float(str(budget).replace(" ", "").replace(",", ".")) > 0
    except ValueError:
        budget_ok = False
    if not budget_ok:
        out.append(_WIZARD_BY_KEY["budget"])
    has_location = bool(answers.get("locations") or answers.get("location") or (ctx or {}).get("default_location"))
    if not has_location:
        out.append(_WIZARD_BY_KEY["locations"])
    return out


# ---------------------------------------------------------------------------
# REJALASHTIRUVCHI PROMPT
# ---------------------------------------------------------------------------
_PLANNER_SYSTEM = """Sen Replix platformasining Meta Ads (Facebook/Instagram) rejalashtiruvchi agentisan.
Vazifang: kompaniya konteksti va foydalanuvchi javoblari asosida TO'LIQ reklama rejasini tuzish.
Foydalanuvchi Ads Manager'dagi maydonlarni o'zi to'ldirmaydi -- SEN to'ldirasan, u faqat tasdiqlaydi/tuzatadi.

QAT'IY QOIDALAR:
- FAQAT JSON qaytar (hech qanday izoh, matn, ``` belgisiz).
- Kompaniya kontekstidan kelib chiq: matnlar aynan shu biznes, shu mahsulot, shu narx segmenti uchun. Umumiy shablon YOZMA.
- Meta ID/key'larni O'YLAB TOPMA -- hudud va qiziqishlarni faqat NOM bilan yoz, tizim ularni Meta qidiruvi orqali o'zi aniqlaydi.
- meta_objective / optimization_goal / billing_event -- SEN TANLAMAYSAN (tizim maqsaddan o'zi oladi).
- Matnlar o'zbek tilida (lotin), agar kontekstda boshqa til ko'rsatilmagan bo'lsa. Ohang -- biznesga mos (premium/ekonom).
- Har bir asosiy qaror uchun QISQA o'zbekcha tushuntirish (explanations) va ishonch (confidence 0-100).
- Qiziqishlar: ko'pi bilan 5 ta NOM; agar keng auditoriya (broad) to'g'riroq bo'lsa -- bo'sh ro'yxat + advantage_audience=true.
- MESSAGES maqsadida: messages.greeting (salomlashuv) va 4-5 ta quick_replies -- aynan shu biznesga mos savollar ("Narxi qancha?", "Manzilingiz qayerda?" kabi, lekin nishga moslab).
- LEADS maqsadida: lead_form -- faqat KVALIFIKATSIYA savollari (3-5 ta, type: FULL_NAME|PHONE|EMAIL|CUSTOM); PHONE doim bo'lsin.
- Kampaniya nomi formati: "Replix | <Kompaniya> | <Maqsad> | <Shahar> | <OyYY>" (masalan "Replix | Nur Mebel | MESSAGES | Toshkent | Sep26").

QAYTARILADIGAN JSON SXEMASI:
{
  "campaign_name": str, "adset_name": str, "ad_name": str,
  "age_min": int, "age_max": int, "genders": [] | [1] | [2],
  "locations": [str],            // hudud nomlari (shahar/viloyat/davlat), tizim aniqlaydi
  "interests": [str],            // 0-5 ta qiziqish NOMI (ingliz tilida yozsang Meta qidiruvi aniqroq topadi)
  "advantage_audience": bool,
  "placements_mode": "automatic" | "manual", "publisher_platforms": [str],
  "destination_type": str | null, // MESSAGES uchun: MESSENGER | INSTAGRAM_DIRECT | WHATSAPP
  "primary_text_variants": [str, str, str], "headline_variants": [str, str, str], "description_variants": [str, str, str],
  "cta": str,                    // LEARN_MORE|SEND_MESSAGE|SIGN_UP|SHOP_NOW|CONTACT_US|GET_QUOTE|CALL_NOW|ORDER_NOW|APPLY_NOW|SUBSCRIBE|WHATSAPP_MESSAGE|GET_OFFER
  "link_url": str | null,
  "messages": {"greeting": str, "quick_replies": [str]},
  "lead_form": {"name": str, "intro_headline": str, "intro_description": str, "questions": [{"type": str, "label": str}], "thank_you_title": str, "thank_you_body": str} | null,
  "reasoning_summary": str,
  "explanations": {"adset.targeting.age_min": str, "adset.targeting.interests": str, "ad.primary_text": str, ...},
  "confidence": {"adset.targeting.age_min": int, ...},
  "warnings": [str]
}"""


def _planner_user_content(ctx: dict, answers: dict, meta_assets: "dict | None") -> str:
    block = company_context_module.company_context_prompt_block(ctx)
    objective = str(answers.get("objective") or "").upper()
    locs = answers.get("locations") or ([answers["location"]] if answers.get("location") else []) or ([ctx.get("default_location")] if ctx.get("default_location") else [])
    currency = answers.get("currency") or ((meta_assets or {}).get("ad_account") or {}).get("currency") or "UZS"
    loc_text = ", ".join(str(l) for l in locs) if locs else "ko'rsatilmagan"
    assets = ctx.get("meta_assets") or {}
    yes_no = lambda flag: "ha" if flag else "yo'q"  # noqa: E731
    lines = [
        block,
        "",
        "# FOYDALANUVCHI JAVOBLARI",
        f"- Maqsad: {objective} ({campaign_draft.OBJECTIVE_LABELS.get(objective, objective)})",
        f"- Kunlik byudjet: {answers.get('budget')} {currency}",
        f"- Hudud(lar): {loc_text}",
        f"- Muddat: {answers.get('duration_days') or DEFAULT_DURATION_DAYS} kun",
        f"- Mahsulot fokusi: {answers.get('product_focus') or '(profil bo`yicha)'}",
        f"- Media: {answers.get('media') or 'rasm (keyin yuklanadi)'}",
        f"- Sahifa ulangan: {yes_no(assets.get('has_page'))}; Instagram ulangan: {yes_no(assets.get('has_instagram'))}; Pixel: {'bor' if assets.get('has_pixel') else 'yo`q'}",
        f"- Bugungi sana: {dt.date.today().isoformat()}",
        "",
        "Yuqoridagi sxema bo'yicha FAQAT JSON qaytar.",
    ]
    return "\n".join(lines)


def _first_str_list(value, limit: int) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = []
    for v in value:
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
        if len(out) >= limit:
            break
    return out


def _resolve_locations(names: list[str], resolve_geo, warnings: list[str]) -> dict:
    """Hudud nomlarini `resolve_geo(name)` callback'i orqali haqiqiy Meta
    geo obyektlariga aylantiradi. Topilmagani TASHLAB yuboriladi (uydirma
    key yo'q) va ogohlantirish yoziladi."""
    geo = {"countries": [], "cities": [], "regions": []}
    seen: set = set()
    for name in names:
        try:
            hit = resolve_geo(name) if resolve_geo else None
        except Exception as e:  # noqa: BLE001 -- Meta qidiruv xatosi rejani to'xtatmasin, log + ogohlantirish
            logger.warning("resolve_geo('%s') xato: %s", name, e)
            hit = None
        if not hit or not hit.get("key"):
            warnings.append(f"'{name}' hududi Meta'da topilmadi -- ro'yxatdan chiqarildi. Qo'lda qayta tanlang.")
            continue
        key = str(hit["key"])
        if key in seen:
            continue
        seen.add(key)
        loc_type = str(hit.get("type") or "city").lower()
        if loc_type == "country":
            geo["countries"].append(key.upper())
        elif loc_type in ("region", "state", "province"):
            geo["regions"].append({"key": key, "name": hit.get("name") or name})
        else:
            geo["cities"].append({"key": key, "name": hit.get("name") or name, "radius": 0, "distance_unit": "kilometer"})
    return geo


def _resolve_interest_names(names: list[str], resolve_interests, warnings: list[str]) -> list[dict]:
    out: list[dict] = []
    seen: set = set()
    for name in names[:5]:
        try:
            hits = resolve_interests(name) if resolve_interests else []
        except Exception as e:  # noqa: BLE001
            logger.warning("resolve_interests('%s') xato: %s", name, e)
            hits = []
        hit = next((h for h in (hits or []) if isinstance(h, dict) and h.get("id")), None)
        if not hit:
            warnings.append(f"'{name}' qiziqishi Meta'da topilmadi -- tashlab yuborildi.")
            continue
        if str(hit["id"]) in seen:
            continue
        seen.add(str(hit["id"]))
        out.append({"id": str(hit["id"]), "name": hit.get("name") or name})
    return out


def _clamp_age(value, default: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(13, min(65, v))


def _parse_budget(value) -> "float | None":
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def plan_campaign(ctx: dict, answers: dict, *, meta_assets: "dict | None", resolve_geo, resolve_interests) -> dict:
    """LLM'dan reja oladi va DETERMINISTIK post-process qilib kanonik holat
    quradi. Qaytaradi: {"state", "field_sources" (hammasi AI_RECOMMENDED),
    "plan": {"reasoning_summary","explanations","confidence","questions","warnings"}}.

    `resolve_geo(name) -> {"key","name","type"} | None`,
    `resolve_interests(name) -> [{"id","name"}]` -- chaqiruvchi (web qatlam)
    `meta_api.search_geo_location`/`search_targeting_interests`ni kompaniya
    tokeni bilan o'rab beradi. LLM ishlamasa `PlannerUnavailableError`."""
    answers = answers or {}
    ctx = ctx or {}
    objective = str(answers.get("objective") or "").upper()
    if objective not in campaign_draft.OBJECTIVE_META:
        raise campaign_draft.DraftPatchError("Kampaniya maqsadi tanlanmagan.")
    budget = _parse_budget(answers.get("budget"))
    if budget is None or budget <= 0:
        raise campaign_draft.DraftPatchError("Kunlik byudjet kiritilmagan.")

    raw = _llm(_PLANNER_SYSTEM, _planner_user_content(ctx, answers, meta_assets))
    if not isinstance(raw, dict):
        raise PlannerUnavailableError(_PLANNER_UNAVAILABLE_MSG)

    warnings: list[str] = [w for w in _first_str_list(raw.get("warnings"), 10)]
    state = campaign_draft.new_empty_state(objective)
    meta = campaign_draft.OBJECTIVE_META[objective]
    company_name = ctx.get("company_name") or "Kompaniya"

    # --- Nomlar
    loc_names = _first_str_list(answers.get("locations"), 10) or ([str(answers["location"])] if answers.get("location") else []) \
        or ([ctx["default_location"]] if ctx.get("default_location") else [])
    llm_locs = _first_str_list(raw.get("locations"), 10)
    all_loc_names = list(dict.fromkeys(loc_names + llm_locs)) if loc_names else llm_locs
    city_label = (all_loc_names[0] if all_loc_names else "UZ")
    month_tag = dt.date.today().strftime("%b%y")
    default_campaign_name = f"Replix | {company_name} | {objective} | {city_label} | {month_tag}"
    state["campaign"]["name"] = (str(raw.get("campaign_name") or "").strip() or default_campaign_name)[:255]
    state["adset"]["name"] = (str(raw.get("adset_name") or "").strip() or f"{city_label} | {objective} | asosiy")[:255]
    state["ad"]["name"] = (str(raw.get("ad_name") or "").strip() or f"{objective} | reklama 1")[:255]

    # --- Byudjet / muddat / valyuta
    account = (meta_assets or {}).get("ad_account") or {}
    currency = (answers.get("currency") or account.get("currency") or "UZS").upper()
    state["adset"]["currency"] = currency
    state["adset"]["budget_type"] = "daily"
    state["adset"]["daily_budget"] = budget
    try:
        duration = int(answers.get("duration_days") or DEFAULT_DURATION_DAYS)
    except (TypeError, ValueError):
        duration = DEFAULT_DURATION_DAYS
    duration = max(1, duration)
    start = (dt.datetime.utcnow() + dt.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    state["adset"]["duration_days"] = duration
    state["adset"]["start_time"] = start.isoformat()
    state["adset"]["end_time"] = (start + dt.timedelta(days=duration)).isoformat()

    # --- Yo'nalish (destination) -- faqat ruxsat etilganlardan
    dest = str(raw.get("destination_type") or "").upper() or None
    if meta["destination_types"]:
        if dest not in meta["destination_types"]:
            dest = meta["destination_types"][0]
        if objective == "MESSAGES":
            has_ig = bool((ctx.get("meta_assets") or {}).get("has_instagram"))
            if dest == "INSTAGRAM_DIRECT" and not has_ig:
                dest = "MESSENGER"
                warnings.append("Instagram akkaunt ulanmagan -- xabarlar Messenger'ga yo'naltirildi.")
            elif dest == "MESSENGER" and has_ig and not raw.get("destination_type"):
                dest = "INSTAGRAM_DIRECT"
        state["adset"]["destination_type"] = dest
    else:
        state["adset"]["destination_type"] = None

    # --- Targeting
    t = state["adset"]["targeting"]
    t["age_min"] = _clamp_age(raw.get("age_min"), 18)
    t["age_max"] = _clamp_age(raw.get("age_max"), 65)
    if t["age_min"] > t["age_max"]:
        t["age_min"], t["age_max"] = t["age_max"], t["age_min"]
    genders = raw.get("genders") or []
    t["genders"] = [g for g in genders if g in (1, 2)] if isinstance(genders, list) else []
    t["geo_locations"] = _resolve_locations(all_loc_names, resolve_geo, warnings)
    if not (t["geo_locations"]["cities"] or t["geo_locations"]["regions"] or t["geo_locations"]["countries"]):
        warnings.append("Hech bir hudud aniqlanmadi -- hududni qo'lda tanlang.")
    t["interests"] = _resolve_interest_names(_first_str_list(raw.get("interests"), 5), resolve_interests, warnings)
    adv = raw.get("advantage_audience")
    t["advantage_audience"] = bool(adv) if isinstance(adv, bool) else (not t["interests"])
    if not t["interests"]:
        t["advantage_audience"] = True
    mode = str(raw.get("placements_mode") or "automatic").lower()
    t["placements"]["mode"] = "manual" if mode == "manual" else "automatic"
    if t["placements"]["mode"] == "manual":
        plats = [p for p in _first_str_list(raw.get("publisher_platforms"), 4) if p in campaign_draft.PLACEMENT_OPTIONS["publisher_platforms"]]
        if not plats:
            t["placements"]["mode"] = "automatic"
        t["placements"]["publisher_platforms"] = plats

    # --- Ad (kreativ matnlar)
    ad = state["ad"]
    assets = ctx.get("meta_assets") or {}
    pages = (meta_assets or {}).get("pages") or []
    ad["page_id"] = assets.get("page_id") or (str(pages[0]["id"]) if pages and pages[0].get("id") else None)
    ad["instagram_actor_id"] = assets.get("ig_business_id") or None
    pt = _first_str_list(raw.get("primary_text_variants"), 3)
    hl = _first_str_list(raw.get("headline_variants"), 3)
    ds = _first_str_list(raw.get("description_variants"), 3)
    ad["copy_variants"] = {"primary_text": pt, "headline": hl, "description": ds}
    ad["primary_text"] = (pt[0] if pt else "")[:1000]
    ad["headline"] = (hl[0] if hl else "")[:125]
    ad["description"] = (ds[0] if ds else "")[:255]
    cta = str(raw.get("cta") or "").upper()
    if cta == "MESSAGE_PAGE":
        cta = "SEND_MESSAGE"
    ad["cta"] = cta if cta in campaign_draft.CTA_TYPES else meta["default_cta"]
    link = raw.get("link_url")
    ad["link_url"] = str(link).strip() if isinstance(link, str) and link.strip().startswith("http") else None
    if objective == "CALLS":
        # Click-to-call: CTA CALL_NOW `link` = "tel:+998..." (Meta v21 hujjati bo'yicha)
        phone = (ctx.get("phone") or "").replace(" ", "")
        ad["link_url"] = f"tel:{phone}" if phone else None
        if not phone:
            warnings.append("Qo'ng'iroq maqsadi uchun kompaniya telefon raqami kerak -- Sozlamalar'da kiriting yoki reklamada qo'lda yozing.")
    msgs = raw.get("messages") if isinstance(raw.get("messages"), dict) else {}
    ad["messages"] = {
        "greeting": str(msgs.get("greeting") or "").strip()[:500],
        "quick_replies": _first_str_list(msgs.get("quick_replies"), 5),
    }
    if objective == "MESSAGES" and not ad["messages"]["greeting"]:
        ad["messages"]["greeting"] = f"Assalomu alaykum! {company_name}ga xush kelibsiz. Sizga qanday yordam bera olamiz?"
    lf = raw.get("lead_form") if isinstance(raw.get("lead_form"), dict) else None
    if objective == "LEADS":
        questions = []
        for q in (lf or {}).get("questions") or []:
            if not isinstance(q, dict):
                continue
            qtype = str(q.get("type") or "CUSTOM").upper()
            if qtype not in campaign_draft.LEAD_QUESTION_TYPES:
                continue
            if qtype == "CUSTOM":
                label = str(q.get("label") or "").strip()
                if not label:
                    continue
                questions.append({"type": "CUSTOM", "label": label, "key": campaign_draft._slug(label)})
            else:
                questions.append({"type": qtype})
        if not any(q["type"] in ("PHONE", "EMAIL") for q in questions):
            questions.insert(0, {"type": "PHONE"})
        if not any(q["type"] == "FULL_NAME" for q in questions):
            questions.insert(0, {"type": "FULL_NAME"})
        ad["lead_form"] = {
            "mode": "new", "existing_form_id": None,
            "new_form": {
                "name": str((lf or {}).get("name") or f"{company_name} — lead forma")[:100],
                "intro_headline": str((lf or {}).get("intro_headline") or "")[:60],
                "intro_description": str((lf or {}).get("intro_description") or "")[:300],
                "questions": questions[:6],
                "privacy_url": "",
                "thank_you_title": str((lf or {}).get("thank_you_title") or "Rahmat!")[:60],
                "thank_you_body": str((lf or {}).get("thank_you_body") or "Tez orada siz bilan bog'lanamiz.")[:300],
            },
        }
        warnings.append("Instant Form uchun maxfiylik siyosati havolasini (privacy_url) kiriting -- Meta buni talab qiladi.")
    if objective == "SALES" and not assets.get("has_pixel"):
        warnings.append("Sotuv maqsadi uchun Pixel kerak -- reklama hisobida Pixel tanlanmagan.")

    campaign_draft.apply_objective_defaults(state)

    # --- Manbalar: hamma AI tomonidan tavsiya qilingan
    field_sources = {p: "AI_RECOMMENDED" for p in campaign_draft.ALLOWED_PATHS if campaign_draft.PATH_TYPES[p] != "dict"}

    explanations = raw.get("explanations") if isinstance(raw.get("explanations"), dict) else {}
    confidence = raw.get("confidence") if isinstance(raw.get("confidence"), dict) else {}
    plan = {
        "reasoning_summary": str(raw.get("reasoning_summary") or "").strip(),
        "explanations": {str(k): str(v) for k, v in explanations.items() if isinstance(v, str)},
        "confidence": {str(k): max(0, min(100, int(v))) for k, v in confidence.items() if isinstance(v, (int, float))},
        "questions": [],
        "warnings": warnings,
        "generated_at": dt.datetime.utcnow().replace(microsecond=0).isoformat(),
    }
    return {"state": state, "field_sources": field_sources, "plan": plan}


def replan_preserving_overrides(old_state: dict, old_sources: dict, new_plan_state: dict) -> dict:
    """Foydalanuvchi qo'lda o'zgartirgan (USER_OVERRIDDEN) har bir yo'l
    uchun ESKI qiymat saqlanadi -- yangi AI rejasi uni jimgina qayta
    yozmaydi (foydalanuvchi override siyosati). Qaytaradi: yangi holatning
    o'zgartirilgan nusxasi."""
    merged = copy.deepcopy(new_plan_state)
    for path, source in (old_sources or {}).items():
        if source != "USER_OVERRIDDEN" or not campaign_draft.is_allowed_path(path):
            continue
        old_value = campaign_draft.get_path(old_state, path)
        campaign_draft.set_path(merged, path, copy.deepcopy(old_value))
    if "objective" in (old_sources or {}) and old_sources["objective"] == "USER_OVERRIDDEN":
        campaign_draft.apply_objective_defaults(merged)
    return merged


# ---------------------------------------------------------------------------
# CHAT-TAHRIRLOVCHI
# ---------------------------------------------------------------------------
_CHAT_SYSTEM = """Sen Replix Meta Ads qoralamasining CHAT-TAHRIRLOVCHISISAN. Foydalanuvchi oddiy tilda buyruq beradi,
sen uni STRUKTURALI patch'ga aylantirasan. FAQAT JSON qaytar:
{"scope": "campaign" | "adset" | "ad" | "objective", "changes": {"<yo'l>": <qiymat>}, "reply": "<qisqa o'zbekcha javob>", "clarify": false}
Agar buyruq tushunarsiz/ikki xil ma'noli bo'lsa: {"scope": null, "changes": {}, "reply": "<aniqlashtiruvchi savol>", "clarify": true}

RUXSAT ETILGAN YO'LLAR (faqat shular; boshqa yo'l yozsang rad etiladi):
{allowed}

QOIDALAR:
- Yo'llar TO'LIQ yoziladi (masalan "adset.targeting.age_min"), scope -- yo'lning birinchi bo'lagi.
- Bitta patch faqat BITTA scope'ga tegishli. Ikki scope kerak bo'lsa, eng muhimini qil va reply'da ikkinchisini alohida so'rashni ayt.
- Hudud/qiziqish uchun Meta key/id'ni O'YLAB TOPMA: "adset.targeting.geo_locations.cities" uchun [{"name": "Toshkent"}] deb NOM yoz (tizim key'ni o'zi topadi); qo'shish uchun mavjud ro'yxatga yangi nomni qo'shib to'liq ro'yxatni qaytar; "faqat X" -- ro'yxatda faqat X.
- Qiziqishlar: "adset.targeting.interests": [{"name": "..."}] (nom bilan). "broad qil" -> interests: [] va advantage_audience: true.
- Yosh: age_min va age_max ikkalasini ham bir patch'da ber. Byudjet: "adset.daily_budget" -- son (masalan "200 ming" -> 200000).
- Muddat: "adset.duration_days" (son). Tizim start/end sanani o'zi hisoblaydi.
- Platforma olib tashlash ("Instagramni olib tashla"): placements.mode = "manual" va publisher_platforms'dan chiqar (joriy holatga qara).
- CTA: "ad.cta" (SEND_MESSAGE, LEARN_MORE, SIGN_UP, SHOP_NOW, CALL_NOW, ...). "N-rasmni/variantni tanla" -> "ad.media.selected_variant": N-1 (0 dan boshlanadi) yoki matn varianti bo'lsa "ad.primary_text"/"ad.headline"ga o'sha variant matnini qo'y.
- "Headline'ni kuchliroq qil" -> "ad.headline"ga yangi, kuchliroq matn yoz (kontekstga mos).
- Lead formga savol qo'shish -> "ad.lead_form.new_form.questions": {"$append": {"type": "CUSTOM", "label": "..."}}.
- Quick reply qo'shish/o'chirish -> "ad.messages.quick_replies" to'liq ro'yxat yoki {"$append": "..."} / {"$remove_index": i}.
- reply -- 1-2 jumla, o'zbekcha, nima o'zgarganini ayt.

MISOLLAR:
"Yoshni 25-50 qil" -> {"scope":"adset","changes":{"adset.targeting.age_min":25,"adset.targeting.age_max":50},"reply":"Yosh oralig'i 25-50 qilindi.","clarify":false}
"Faqat Toshkent qil" -> {"scope":"adset","changes":{"adset.targeting.geo_locations.cities":[{"name":"Tashkent"}],"adset.targeting.geo_locations.regions":[],"adset.targeting.geo_locations.countries":[]},"reply":"Hudud faqat Toshkent qilindi.","clarify":false}
"Toshkent va Chirchiqni qo'sh" -> cities: joriy ro'yxat + [{"name":"Tashkent"},{"name":"Chirchiq"}]
"Instagramni olib tashla" -> {"scope":"adset","changes":{"adset.targeting.placements.mode":"manual","adset.targeting.placements.publisher_platforms":["facebook","messenger"]},...}
"Buni broad qil" -> {"scope":"adset","changes":{"adset.targeting.interests":[],"adset.targeting.advantage_audience":true},...}
"Budjetni 200 ming qil" -> {"scope":"adset","changes":{"adset.daily_budget":200000},...}
"Buni 10 kunga qil" -> {"scope":"adset","changes":{"adset.duration_days":10},...}
"CTA'ni Send Message qil" -> {"scope":"ad","changes":{"ad.cta":"SEND_MESSAGE"},...}
"3-rasmni tanla" -> {"scope":"ad","changes":{"ad.media.selected_variant":2},...}
"Lead formga obyekt hajmi degan savol qo'sh" -> {"scope":"ad","changes":{"ad.lead_form.new_form.questions":{"$append":{"type":"CUSTOM","label":"Obyekt hajmi qancha?"}}},...}
"""


def _chat_system_prompt() -> str:
    allowed = "\n".join(f"- {p}: {campaign_draft.PATH_TYPES[p]}" for p in sorted(campaign_draft.ALLOWED_PATHS))
    return _CHAT_SYSTEM.replace("{allowed}", allowed)


def _resolve_named_items_in_changes(changes: dict, state: dict, resolve_geo, resolve_interests, warnings: list[str]) -> dict:
    """Chat patch'idagi NOM bilan berilgan hudud/qiziqishlarni haqiqiy Meta
    key/id'ga aylantiradi. Allaqachon key/id bo'lsa (masalan joriy
    holatdan nusxalangan) o'zgarishsiz qoladi; topilmagani tashlab
    yuboriladi + ogohlantirish."""
    out = dict(changes)
    current_geo = ((state.get("adset") or {}).get("targeting") or {}).get("geo_locations") or {}
    for key in ("adset.targeting.geo_locations.cities", "adset.targeting.geo_locations.regions"):
        if key not in out or not isinstance(out[key], list):
            continue
        resolved = []
        for item in out[key]:
            if not isinstance(item, dict):
                if isinstance(item, str):
                    item = {"name": item}
                else:
                    continue
            if item.get("key"):
                resolved.append(item)
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            # Joriy holatda shu nomli joy allaqachon bo'lsa -- key'ini olamiz
            existing = next((c for c in (current_geo.get("cities") or []) + (current_geo.get("regions") or [])
                             if (c.get("name") or "").lower() == name.lower()), None)
            if existing:
                resolved.append(existing)
                continue
            geo = _resolve_locations([name], resolve_geo, warnings)
            if geo["cities"] and key.endswith("cities"):
                resolved.append(geo["cities"][0])
            elif geo["regions"]:
                if key.endswith("regions"):
                    resolved.append(geo["regions"][0])
                else:
                    out.setdefault("adset.targeting.geo_locations.regions", list(current_geo.get("regions") or [])).append(geo["regions"][0])
            elif geo["cities"]:
                out.setdefault("adset.targeting.geo_locations.cities", list(current_geo.get("cities") or [])).append(geo["cities"][0])
            elif geo["countries"]:
                out.setdefault("adset.targeting.geo_locations.countries", list(current_geo.get("countries") or [])).append(geo["countries"][0])
        out[key] = resolved
    if "adset.targeting.geo_locations" in out and isinstance(out["adset.targeting.geo_locations"], dict):
        nested = out.pop("adset.targeting.geo_locations")
        for sub in ("cities", "regions", "countries"):
            if sub in nested:
                out[f"adset.targeting.geo_locations.{sub}"] = nested[sub]
        return _resolve_named_items_in_changes(out, state, resolve_geo, resolve_interests, warnings)
    for key in ("adset.targeting.interests", "adset.targeting.behaviors"):
        if key not in out or not isinstance(out[key], list):
            continue
        resolved = []
        names = []
        for item in out[key]:
            if isinstance(item, dict) and item.get("id"):
                resolved.append({"id": str(item["id"]), "name": item.get("name") or ""})
            elif isinstance(item, dict) and item.get("name"):
                names.append(str(item["name"]))
            elif isinstance(item, str):
                names.append(item)
        if names and key.endswith("interests"):
            resolved.extend(_resolve_interest_names(names, resolve_interests, warnings))
        out[key] = resolved
    return out


def chat_edit(ctx: dict, state: dict, message: str, *, resolve_geo, resolve_interests) -> dict:
    """Chat buyrug'ini patch'ga aylantiradi. Qaytaradi:
    {"patch": {"scope","changes"} | None, "reply": str, "clarify": bool,
     "warnings": [str]}. `patch` -- HALI QO'LLANMAGAN; chaqiruvchi uni
    `campaign_draft.apply_patch(..., source="USER_OVERRIDDEN")` +
    `validate_state()` orqali o'tkazadi (chat orqali o'zgartirish ham
    foydalanuvchining ANIQ qarori, shuning uchun USER_OVERRIDDEN)."""
    message = (message or "").strip()
    if not message:
        return {"patch": None, "reply": "Buyruq bo'sh -- nimani o'zgartiray?", "clarify": True, "warnings": []}
    user_content = "\n".join([
        company_context_module.company_context_prompt_block(ctx or {}),
        "",
        "# JORIY QORALAMA HOLATI (ixcham JSON)",
        campaign_draft.compact_state_for_prompt(state),
        "",
        "# FOYDALANUVCHI BUYRUG'I",
        message,
        "",
        "FAQAT JSON qaytar.",
    ])
    raw = _llm(_chat_system_prompt(), user_content)
    if not isinstance(raw, dict):
        raise PlannerUnavailableError(_PLANNER_UNAVAILABLE_MSG)
    reply = str(raw.get("reply") or "").strip()
    clarify = bool(raw.get("clarify"))
    changes = raw.get("changes") if isinstance(raw.get("changes"), dict) else {}
    scope = str(raw.get("scope") or "").strip().lower() or None
    if clarify or not changes:
        return {"patch": None, "reply": reply or "Buyruqni aniqroq yozing.", "clarify": True, "warnings": []}

    warnings: list[str] = []
    # Yo'llarni to'liq ko'rinishga keltirib allowlist bilan tekshiramiz
    full_changes: dict = {}
    rejected: list[str] = []
    for key, value in changes.items():
        full = campaign_draft._resolve_full_path(scope or campaign_draft.scope_of_path(str(key)), str(key))
        if not campaign_draft.is_allowed_path(full):
            rejected.append(str(key))
            continue
        full_changes[full] = value
    if rejected and not full_changes:
        return {
            "patch": None,
            "reply": "Bu maydonni chat orqali o'zgartirib bo'lmaydi: " + ", ".join(rejected) + ". Ruxsat etilgan maydonlarni formadan o'zgartiring.",
            "clarify": False, "warnings": [],
        }
    if rejected:
        warnings.append("Ruxsat etilmagan maydonlar tashlab yuborildi: " + ", ".join(rejected))
    scopes = {campaign_draft.scope_of_path(p) for p in full_changes}
    if len(scopes) > 1:
        # Bitta patch -- bitta daraja: eng ko'p o'zgarishli scope qoladi
        main_scope = max(scopes, key=lambda s: sum(1 for p in full_changes if campaign_draft.scope_of_path(p) == s))
        dropped = [p for p in full_changes if campaign_draft.scope_of_path(p) != main_scope]
        for p in dropped:
            full_changes.pop(p)
        warnings.append("Bir vaqtda faqat bitta daraja o'zgartiriladi; alohida so'rang: " + ", ".join(dropped))
        scope = main_scope
    else:
        scope = scopes.pop()
    full_changes = _resolve_named_items_in_changes(full_changes, state, resolve_geo, resolve_interests, warnings)
    # Tip tekshiruvi -- xato bo'lsa patch qaytarilmaydi (chaqiruvchi apply_patch'da ham tekshiradi)
    try:
        for path, value in full_changes.items():
            if isinstance(value, dict) and ("$append" in value or "$remove_index" in value):
                continue
            campaign_draft.coerce_value(path, value)
    except campaign_draft.DraftPatchError as e:
        return {"patch": None, "reply": f"Buyruqni qo'llab bo'lmadi: {e}", "clarify": False, "warnings": warnings}
    return {
        "patch": {"scope": scope, "changes": full_changes},
        "reply": reply or "O'zgartirildi.",
        "clarify": False,
        "warnings": warnings,
    }
