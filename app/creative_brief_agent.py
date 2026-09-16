"""creative_brief_agent.py — Kreativ studiya: AI BRIF AGENTI (2026-09,
haqiqiy foydalanuvchi fikri: "armatura" deb yozganida statik "Rasm qanday
ko'rinishda bo'lsin?" kabi umumiy savol emas, "qaysi diametr, narxi
qancha" kabi AYNAN shu mahsulotga xos savol kutilgan edi -- "savollar bir
xil shablon bo'lmasin, ya ishlasin bu yerda, agent ishlasin").

`creative_studio.CREATIVE_BRIEF_QUESTIONS` (5 ta FIKSAL savol, bittada)
ENDI faqat ZAXIRA/orqaga moslik uchun qoladi (o'chirilmagan -- pastga
qarang). Bu modul o'rniga navbatma-navbat, LLM'ga tayangan suhbat
yuritadi: har javobdan keyin foydalanuvchi AYTGAN faktlarga qarab
KEYINGI eng foydali (va faqat shu reklama uchun kerakli) savolni tanlaydi
-- `ai_campaign_planner.py`dagi naqsh bilan bir xil (`_llm()` yupqa o'ram,
`orchestrator._call_agent`, Anthropic asosiy + OpenAI zaxira).

OQIM: `next_step(ctx, conversation)` HAR safar chaqiriladi (bir marta bir
savol/javob -- server holatsiz, `creative_studio.start_brief`/
`answer_brief` suhbatni `CreativeAsset.brief_conversation_json`da
saqlaydi). Qaytaradi:
  {"done": False, "question": "...", "placeholder": "...", "brief": None}
  {"done": True,  "question": None, "placeholder": None, "brief": {...}}
`brief` -- ESKI tekis lug'at shakli (`focus`, `offer_text`,
`cta_preference`, `style_notes`, `phone`) -- pastki oqim (`_run_generation`,
`fallback_placeholder_values`, AI kopirayter) buni ALLAQACHON shunday
kutadi, shuning uchun ULARGA HECH QANDAY o'zgartirish kerak emas.

QATTIQ XAVFSIZLIK TO'RI (LLM "aqli" bilan chetlab o'tib bo'lmaydi -- sof
Python, HAR DOIM ishlaydi): `creative_studio.missing_questions()`dagi
BILAN AYNAN BIR XIL ikkita majburiy shart -- 'focus' (mahsulot profilda
yo'q bo'lsa) va 'phone' (kompaniya telefoni yo'q bo'lsa) -- LLM "done"
desa ham, shu ikkitasi bo'sh ekan, tizim buni RAD ETADI va o'sha maydon
uchun ESKI, statik savol matnini (`creative_studio._BRIEF_BY_KEY`)
qaytaradi. Ilova HECH QACHON telefon/mahsulot fokusisiz rasm
generatsiya qila olmaydi -- xuddi eski oqimdagidek.

LLM ISHLAMASA (ikkala provayder ham) -- suhbat HECH QACHON tiqilib
qolmaydi: `_fallback_step()` transkriptdan oddiy evristika bilan (birinchi
javob -- focus, telefonga o'xshagan javob -- phone) yakuniy brif tuzadi
yoki majburiy maydon uchun ESKI statik savolni beradi."""

import logging

import company_context as company_context_module
import creative_studio

logger = logging.getLogger("creative_brief_agent")


class BriefAgentUnavailableError(Exception):
    """`orchestrator._call_agent` ikkala provayder bilan ham ishlamaganda
    `_llm()` ichida ko'tariladi. `next_step()` buni HAR DOIM ICHKARIDA
    ushlaydi (`_fallback_step()`ga o'tadi) -- suhbat oqimi tashqariga bu
    xatoni odatda chiqarmaydi (foydalanuvchi hech qachon "AI ishlamayapti"
    devor bilan to'xtab qolmasligi kerak)."""


_UNAVAILABLE_MSG = (
    "AI brif agenti hozir javob bera olmadi -- oddiy savollar bilan davom etamiz."
)

_BRIEF_KEYS = ("focus", "offer_text", "cta_preference", "style_notes", "phone")
_MAX_AGENT_QUESTIONS = 5


def _llm(system_prompt: str, user_content: str) -> dict:
    """`orchestrator._call_agent` uchun yupqa o'ram -- import chaqiruv
    paytida emas (modul import qilinganda `ANTHROPIC_API_KEY` talab
    qilinmasin uchun; testlar `orchestrator._call_agent`ni mock qiladi),
    xuddi `ai_campaign_planner._llm()` kabi."""
    import orchestrator
    try:
        return orchestrator._call_agent(system_prompt, user_content)
    except (orchestrator.TargetologFormatError, orchestrator.AgentUnavailableError) as e:
        logger.warning("creative_brief_agent: LLM javob bermadi (%s)", type(e).__name__)
        raise BriefAgentUnavailableError(_UNAVAILABLE_MSG) from e


# ---------------------------------------------------------------------------
# PROMPT
# ---------------------------------------------------------------------------
_BRIEF_AGENT_SYSTEM = """Sen Replix "Kreativ studiya" bo'limidagi AI BRIF AGENTISAN -- Facebook/Instagram reklama
rasmi uchun kerakli ma'lumotni foydalanuvchi bilan TABIIY SUHBAT orqali yig'asan (O'zbekiston bozoridagi kichik
biznes uchun). Senga kompaniya konteksti (nomi, sohasi, mahsulot/xizmat, eng ko'p sotiladigani, narx segmenti,
mavjud telefon) VA hozirgacha bo'lgan suhbat (sening savollaring + foydalanuvchi javoblari) beriladi.

VAZIFANG: HAR SAFAR bitta, AYNAN foydalanuvchi AYTGAN narsaga qarab tanlangan, ANIQ savol ber -- umumiy shablon
savol EMAS. MISOL (aynan shunday ishla): foydalanuvchi mahsulot sifatida "armatura" desa, keyingi savol
"Qaysi diametr/turdagi armatura (masalan 10mm, 12mm), qaysi GOST standarti va narxi qancha?" kabi ANIQ va
MAHSULOTGA XOS bo'lsin -- "Yana ma'lumot bering" yoki "Tafsilotlarni yozing" kabi umumiy savol HECH QACHON BERMA.

QOIDALAR:
- Har bir javobdan keyin foydalanuvchi AYTGAN faktlarga tayanib KEYINGI eng foydali savolni tanla (narx, o'lcham/
  model, aksiya, chaqiriq matni, telefon, uslub/muhit -- lekin faqat SHU reklama uchun HALI noaniq va MUHIM
  bo'lgan narsani so'ra, hammasini birdan so'ramaysan).
- HECH QACHON fakt (narx, kafolat, texnik xususiyat, va'da) O'YLAB TOPMA -- faqat foydalanuvchi aytgan narsani
  qayta ishlat.
- Odatda 2-4 savoldan keyin, ko'pi bilan 5 tadan keyin YETARLI ma'lumot yig'ilgan bo'lishi kerak -- shunda to'xta
  (ortiqcha savol berib foydalanuvchini charchatma).
- Kompaniya telefoni kontekstda "Kompaniya telefoni" sifatida ko'rsatilmagan bo'lsa VA suhbatda ham berilmagan
  bo'lsa -- albatta bir marta telefon raqamini so'ra (reklama rasmida ko'rsatiladi).
- FAQAT JSON qaytar (hech qanday izoh, matn, ``` belgisiz), ikkita mumkin bo'lgan shakl:
  Hali savol bor: {"done": false, "question": "...", "placeholder": "..."}
  Yetarli ma'lumot: {"done": true, "brief": {"focus": "...", "offer_text": "...", "cta_preference": "...",
  "style_notes": "...", "phone": "..."}}
- "brief" ichida 5 ta kalit HAR DOIM bo'lishi shart (focus, offer_text, cta_preference, style_notes, phone) --
  foydalanuvchi tegmagan mavzu uchun bo'sh satr "" yoz, kalitni tashlab ketma.
- Til -- o'zbekcha (lotin). Savol qisqa, bitta gap, tabiiy va do'stona ohangda; kerak bo'lsa "placeholder"da
  qisqa misol ber."""


def _brief_user_content(ctx: dict, conversation: list) -> str:
    block = company_context_module.company_context_prompt_block(ctx or {})
    lines = [block, "", "# SUHBAT (hozirgacha)"]
    turns = [t for t in (conversation or []) if isinstance(t, dict)]
    if not turns:
        lines.append("(hali savol berilmagan -- BIRINCHI, kompaniya kontekstiga mos savolni ber)")
    for turn in turns:
        role = "Agent (sen)" if turn.get("role") == "agent" else "Foydalanuvchi"
        lines.append(f"- {role}: {turn.get('text') or ''}")
    lines.append("")
    lines.append("Yuqoridagi qoidalar bo'yicha FAQAT JSON qaytar.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# QATTIQ MAJBURIY MAYDONLAR (`creative_studio.missing_questions()` bilan
# AYNAN BIR XIL shartlar -- ikkalasi ham shu mantiqni ishlatadi, shuning
# uchun bu yerda TAKRORLANMAYDI, chaqiriladi).
# ---------------------------------------------------------------------------

def _focus_missing(ctx: dict, brief: dict) -> bool:
    ctx = ctx or {}
    product_missing = "product_or_service" in (ctx.get("missing_fields") or []) or not (ctx.get("profile_answers") or {}).get("product_or_service")
    return product_missing and not str((brief or {}).get("focus") or "").strip()


def _phone_missing(ctx: dict, brief: dict) -> bool:
    return not creative_studio.phone_known(ctx) and not str((brief or {}).get("phone") or "").strip()


def _hard_question(key: str) -> dict:
    """`creative_studio._BRIEF_BY_KEY`dagi ESKI, statik savol matnini
    qaytaradi -- Uzbek matn IKKI JOYDA yozilmasin."""
    _, question, placeholder, _ = creative_studio._BRIEF_BY_KEY[key]
    return {"done": False, "question": question, "placeholder": placeholder, "brief": None}


def _normalize_brief(raw: dict) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for key in _BRIEF_KEYS:
        v = raw.get(key)
        out[key] = " ".join(str(v).split())[:500] if isinstance(v, str) else ""
    if out["phone"]:
        try:
            out["phone"] = creative_studio.normalize_phone(out["phone"])
        except creative_studio.CreativeError:
            pass  # formatga tushmasa ham xom matnni saqlaymiz -- qattiq to'r faqat "bo'sh emasligini" tekshiradi
    return out


def _best_effort_brief(ctx: dict, conversation: list) -> dict:
    """LLM UMUMAN ishlamasa (ikkala provayder ham) -- eng oddiy, xavfsiz
    evristika: birinchi foydalanuvchi javobi -- focus, telefonga o'xshagan
    birinchi javob -- phone, qolganlari -- style_notes."""
    user_answers = [str(t.get("text") or "").strip() for t in (conversation or [])
                     if isinstance(t, dict) and t.get("role") == "user" and str(t.get("text") or "").strip()]
    brief = {k: "" for k in _BRIEF_KEYS}
    if user_answers:
        brief["focus"] = user_answers[0][:500]
    for ans in user_answers:
        try:
            brief["phone"] = creative_studio.normalize_phone(ans)
            break
        except creative_studio.CreativeError:
            continue
    if len(user_answers) > 1:
        brief["style_notes"] = " ".join(user_answers[1:])[:500]
    return brief


def _fallback_step(ctx: dict, conversation: list) -> dict:
    """LLM ishlamadi -- ESKI deterministik xatti-harakat: majburiy maydon
    hali yo'q bo'lsa -- shu maydon uchun statik savol; ikkalasi ham
    (suhbatdan) topilsa -- darhol "done" (generatsiya HECH QACHON
    to'xtab qolmasin)."""
    brief = _best_effort_brief(ctx, conversation)
    if _focus_missing(ctx, brief):
        return _hard_question("focus")
    if _phone_missing(ctx, brief):
        return _hard_question("phone")
    return {"done": True, "question": None, "placeholder": None, "brief": brief}


def next_step(ctx: dict, conversation: list) -> dict:
    """Suhbatning KEYINGI qadamini oladi. `conversation` -- `[{"role":
    "agent"|"user", "text": str}, ...]` (bo'sh -- birinchi savol so'raladi).
    Qaytadi: {"done", "question", "placeholder", "brief"} (yuqoridagi
    modul docstringiga qarang). HECH QACHON istisno ko'tarmaydi -- LLM
    ishlamasa ham (`_fallback_step`) ichkarida hal qiladi."""
    ctx = ctx or {}
    conversation = conversation or []
    agent_count = sum(1 for t in conversation if isinstance(t, dict) and t.get("role") == "agent")
    at_cap = agent_count >= _MAX_AGENT_QUESTIONS

    try:
        raw = _llm(_BRIEF_AGENT_SYSTEM, _brief_user_content(ctx, conversation))
    except BriefAgentUnavailableError:
        return _fallback_step(ctx, conversation)

    if not isinstance(raw, dict):
        logger.warning("creative_brief_agent: LLM JSON o'rniga %s qaytardi", type(raw).__name__)
        return _fallback_step(ctx, conversation)

    llm_done = bool(raw.get("done"))
    if llm_done or at_cap:
        # Savollar chegarasiga yetilgan bo'lsa -- LLM nima desa ham
        # (hattoki "done: false" bo'lsa ham) MAJBURIY yakunlaymiz, aks
        # holda cheksiz savol-javob tsikli bo'lishi mumkin. Quyidagi
        # qattiq maydon tekshiruvi baribir ishlaydi (kerak bo'lsa BITTA
        # qo'shimcha savolga ruxsat beradi -- majburiy maydonsiz
        # generatsiya HECH QACHON mumkin emas).
        if llm_done and isinstance(raw.get("brief"), dict):
            brief = _normalize_brief(raw["brief"])
        else:
            brief = _best_effort_brief(ctx, conversation)
        if _focus_missing(ctx, brief):
            return _hard_question("focus")
        if _phone_missing(ctx, brief):
            return _hard_question("phone")
        return {"done": True, "question": None, "placeholder": None, "brief": brief}

    question = str(raw.get("question") or "").strip()
    if not question:
        logger.warning("creative_brief_agent: LLM 'done: false' lekin savol bermadi")
        return _fallback_step(ctx, conversation)
    placeholder = raw.get("placeholder")
    return {
        "done": False,
        "question": question[:300],
        "placeholder": placeholder[:120] if isinstance(placeholder, str) and placeholder.strip() else None,
        "brief": None,
    }
