"""target_analysis.py — Meta Ads Autopilot: "Target Analizi" -- import
qilingan / jonli (Meta'da allaqachon ishlab turgan) kampaniyani AMALDAGI
Meta statistikasi + CRM (Replix'ning o'z lid sifati ma'lumoti) asosida,
o'rnatilgan Meta Ads yaxshi amaliyotlariga (best practices) solishtirib
diagnostika qiladi (2026-09, foydalanuvchi so'rovi: "targetolog agentimizni
yuz foiz ishlaydigan qilish" -- Meta'dan import qilingan kampaniya uchun AI
diagnostika + tasdiqlab-qo'llash oqimi).

QUVUR -- `ai_campaign_planner.chat_edit()` bilan BIR XIL zanjir, faqat
kirish nuqtasi boshqa (foydalanuvchi buyrug'i o'rniga -- haqiqiy raqamlar):
    LLM -> taklif qilingan o'zgarishlar -> campaign_draft.is_allowed_path()
    bilan tekshiruv -> campaign_draft.apply_patch() -> meta_publish.
    push_updates_to_meta().

MUHIM FARQ -- bu yerda AI o'z tashabbusi bilan (foydalanuvchining ANIQ
buyrug'isiz) o'zgarish taklif qiladi va bu HAQIQIY pul sarfiga (jonli
kampaniya) ta'sir qiladi, shuning uchun:
  - HAR BIR taklif ALOHIDA-ALOHIDA, ANIQ tasdiqlanmasa (checkbox + "Tanlan-
    ganlarni qo'llash"), HECH QACHON qo'llanmaydi -- avtomatik qo'llash yo'q
    (`chat_edit()` foydalanuvchi xabariga darhol javob sifatida qo'llanadi,
    bu yerda esa taklif va qo'llash ikki ALOHIDA marshrut/bosqich).
  - Diagnostika (`run_diagnosis`) HECH NARSANI o'zgartirmaydi/saqlamaydi --
    faqat o'qiydi va taklif qaytaradi; qo'llash (app.py'dagi
    `/target-analiz/qollash`) alohida, aniq foydalanuvchi tasdig'i bilan.
  - HAR BIR raqam (CPL, chastota, xarajat, CRM yutuq foizi) FAQAT haqiqiy
    Meta/CRM so'rovidan keladi -- LLM'ga "sanoat benchmarki" kabi uydirma
    son ishlatishga PROMPT darajasida ta'qiq qo'yilgan (pastga qarang).
"""

import json
import logging
import datetime as dt
from collections import defaultdict

from sqlalchemy import func

import campaign_draft
import meta_api
import dashboard_data
import ai_campaign_planner
import company_context as company_context_module
from db import Lead, FunnelStage

logger = logging.getLogger("target_analysis")


class AnalysisUnavailableError(Exception):
    """Target Analizi hozir ishlamadi (LLM/Meta xatosi yoki kampaniya hali
    Meta'ga chiqarilmagan). Xabar o'zbekcha, foydalanuvchiga to'g'ridan-
    to'g'ri ko'rsatiladi."""


_ANALYSIS_UNAVAILABLE_MSG = (
    "Target Analizi hozir javob bera olmadi. Birozdan keyin qayta urinib ko'ring."
)


def _llm(system_prompt: str, user_content: str) -> dict:
    """`orchestrator._call_agent` uchun yupqa o'ram -- `ai_campaign_planner.
    _llm()` bilan bir xil naqsh (import chaqiruv paytida emas, funksiya
    ichida -- `ANTHROPIC_API_KEY` modul importida talab qilinmasin; testlar
    `orchestrator._call_agent`ni mock qiladi)."""
    import orchestrator
    try:
        return orchestrator._call_agent(system_prompt, user_content)
    except (orchestrator.TargetologFormatError, orchestrator.AgentUnavailableError) as e:
        logger.warning("target_analysis: LLM javob bermadi (%s)", type(e).__name__)
        raise AnalysisUnavailableError(_ANALYSIS_UNAVAILABLE_MSG) from e


# ---------------------------------------------------------------------------
# MA'LUMOT YETARLILIGI CHEGARASI
#
# Nega aynan shu sonlar (texnik audit uchun izoh): Meta'ning o'zi auction
# "o'rganish" (learning) bosqichidan barqaror chiqish uchun taxminan 7 kunlik
# oynada ~50 ta optimallashtirish hodisasi (masalan lid) kerakligini aytadi
# -- bu IDEAL holat; diagnostika uchun shuncha talab qilish aksariyat kichik
# byudjetli mahalliy kampaniyalarni "hech qachon tahlil qilib bo'lmaydi"
# holatiga tushirib qo'yar edi. Shuning uchun bu yerda ancha PASTROQ, lekin
# "shovqin"dan (bir kunlik tasodifiy tebranishdan) ajratib bo'ladigan ikkita
# ODDIY, valyutadan MUSTAQIL shart tanlandi -- ikkalasi ham FAQAT haqiqiy
# fetch qilingan Meta raqamlariga tayanadi, hech qanday "sanoat benchmarki"
# o'ylab topilmaydi:
#   1) MIN_DAYS_WITH_SPEND -- so'nggi 30 kunda kamida shuncha KUN reklama
#      ko'rsatilgan bo'lishi kerak (auction odatda birinchi 1-2 kunda
#      beqaror narx ko'rsatadi -- bitta-ikkita kunlik statistika asosida
#      xulosa chiqarish yanglish bo'ladi).
#   2) MIN_SPEND_TO_DAILY_BUDGET_RATIO -- kampaniya so'nggi 30 kunda O'ZINING
#      JORIY kunlik byudjetining kamida shuncha barobarini sarflagan bo'lishi
#      kerak (masalan kecha yaratilgan yoki byudjeti yaqinda keskin
#      oshirilgan kampaniya uchun "hali erta" deb JIM turamiz, uydirma
#      xulosa chiqarmaymiz).
MIN_DAYS_WITH_SPEND = 3
MIN_SPEND_TO_DAILY_BUDGET_RATIO = 3.0

PERFORMANCE_FIELDS = ["spend", "impressions", "reach", "frequency", "cpm", "ctr", "cpc", "actions"]
_DAILY_FIELDS = ["spend", "frequency", "cpm", "ctr", "actions"]

SEVERITY_VALUES = {"past", "o'rtacha", "yuqori"}
_SEVERITY_DEFAULT = "o'rtacha"


# ---------------------------------------------------------------------------
# META STATISTIKASI
# ---------------------------------------------------------------------------

def _sum_rows(rows: list[dict]) -> tuple[float, int, int, list[dict]]:
    spend = sum(float(r.get("spend") or 0) for r in rows)
    impressions = sum(int(float(r.get("impressions") or 0)) for r in rows)
    reach_vals = [int(float(r.get("reach") or 0)) for r in rows if r.get("reach")]
    reach = max(reach_vals) if reach_vals else 0
    actions: list[dict] = []
    for r in rows:
        actions.extend(r.get("actions") or [])
    return spend, impressions, reach, actions


def fetch_performance(company, meta_campaign_id: str, goal: str) -> dict:
    """Kampaniyaning so'nggi 7/30 kunlik ko'rsatkichlari + 30 kunlik kunlik
    yoyilma (necha kun reklama ko'rsatilgani va chastota trendini ko'rish
    uchun). Tarmoq/Meta xatosida HECH QACHON exception otmaydi -- `{"error":
    ...}` qaytaradi (chaqiruvchi buni "ma'lumot olinmadi" deb ko'rsatadi,
    sahifa yiqilmaydi -- shu faylning boshqa funksiyalari bilan bir xil
    "hech qachon buzilmang" falsafasi)."""
    token = company.get_meta_access_token() if hasattr(company, "get_meta_access_token") else None
    if not token:
        return {"error": "Meta ulanmagan."}
    try:
        rows_7d = meta_api.get_campaign_insights(meta_campaign_id, date_preset="last_7d", fields=PERFORMANCE_FIELDS, access_token=token)
        rows_30d = meta_api.get_campaign_insights(meta_campaign_id, date_preset="last_30d", fields=PERFORMANCE_FIELDS, access_token=token)
        daily_30d = meta_api.get_campaign_insights(meta_campaign_id, date_preset="last_30d", fields=_DAILY_FIELDS, time_increment=1, access_token=token)
    except Exception as e:  # noqa: BLE001 -- Meta/tarmoq xatosi diagnostikani to'xtatadi, lekin sahifani yiqitmaydi
        logger.warning("target_analysis.fetch_performance(%s): %s", meta_campaign_id, meta_api.safe_error_message(e))
        return {"error": meta_api.safe_error_message(e)}

    def _window(rows: list[dict]) -> dict:
        spend, impressions, reach, actions = _sum_rows(rows)
        results, result_label = dashboard_data._resolve_meta_result(goal, actions, reach, impressions)
        # Nisbat maydonlari (ctr/cpm/frequency) -- Meta o'zi to'g'ri
        # hisoblab bergan qiymat ISHLATILADI (bir nechta qator kelsa --
        # kutilmagan holat -- birinchisidan; o'zimiz QAYTA hisoblamaymiz,
        # chunki masalan chastota reach/impressions nisbati -- bir necha
        # qatorni yig'ib to'g'ri chiqmaydi).
        first = rows[0] if rows else {}
        cost_per_result = (spend / results) if results else None
        return {
            "spend": round(spend, 2), "impressions": impressions, "reach": reach,
            "results": results, "result_label": result_label,
            "cpm": round(float(first.get("cpm") or 0), 2), "ctr": round(float(first.get("ctr") or 0), 3),
            "frequency": round(float(first.get("frequency") or 0), 2),
            "cost_per_result": round(cost_per_result, 2) if cost_per_result else None,
        }

    days_with_spend = sum(1 for r in daily_30d if float(r.get("spend") or 0) > 0)
    # Trend -- so'nggi 7 kun bilan undan oldingi 7 kunni solishtiramiz
    # (chastota ko'tarilyaptimi) -- HAQIQIY fetch qilingan kunlik
    # qatorlardan, hech qanday benchmark o'ylab topilmaydi.
    sorted_daily = sorted(daily_30d, key=lambda r: r.get("date_start") or "")
    recent7 = sorted_daily[-7:] if len(sorted_daily) >= 7 else sorted_daily
    prior7 = sorted_daily[-14:-7] if len(sorted_daily) >= 14 else []
    recent_freq = max([float(r.get("frequency") or 0) for r in recent7], default=0.0)
    prior_freq = max([float(r.get("frequency") or 0) for r in prior7], default=0.0) if prior7 else None

    return {
        "error": None,
        "last_7d": _window(rows_7d),
        "last_30d": _window(rows_30d),
        "days_with_spend_30d": days_with_spend,
        "max_frequency_recent_7d": round(recent_freq, 2),
        "max_frequency_prior_7d": round(prior_freq, 2) if prior_freq is not None else None,
        "goal": goal,
        "goal_label": dashboard_data.GOAL_LABELS.get(goal, goal),
    }


def check_data_sufficiency(performance: dict, state: dict) -> "str | None":
    """`None` = yetarli ma'lumot bor, diagnostika davom etadi. Aks holda --
    foydalanuvchiga to'g'ridan-to'g'ri ko'rsatiladigan o'zbekcha sabab."""
    days = performance.get("days_with_spend_30d") or 0
    if days < MIN_DAYS_WITH_SPEND:
        return (
            f"Hali yetarli ma'lumot yo'q -- so'nggi 30 kunda atigi {days} kun reklama ko'rsatilgan. "
            f"Ishonchli xulosa chiqarish uchun kamida {MIN_DAYS_WITH_SPEND} kun reklama ko'rsatilgan bo'lishi kerak."
        )
    adset = state.get("adset") or {}
    daily_budget = adset.get("daily_budget")
    if not daily_budget and (adset.get("budget_type") == "lifetime") and adset.get("lifetime_budget"):
        duration = adset.get("duration_days") or 1
        daily_budget = float(adset["lifetime_budget"]) / max(int(duration), 1)
    spend_30d = (performance.get("last_30d") or {}).get("spend") or 0.0
    if daily_budget and spend_30d < MIN_SPEND_TO_DAILY_BUDGET_RATIO * float(daily_budget):
        return (
            f"Hali yetarli ma'lumot yo'q -- so'nggi 30 kunda sarflangan summa ({spend_30d:,.0f}) "
            f"joriy kunlik byudjetning {MIN_SPEND_TO_DAILY_BUDGET_RATIO:.0f} barobariga ham yetmaydi. "
            f"Kampaniya hali yangi ishga tushgan yoki byudjet yaqinda o'zgargan bo'lishi mumkin -- "
            f"bir necha kundan keyin qayta urinib ko'ring."
        )
    return None


# ---------------------------------------------------------------------------
# CRM (LID SIFATI) -- Replix'ning o'z farqlovchi ma'lumoti, Meta buni bilmaydi
# ---------------------------------------------------------------------------

def fetch_crm_summary(session, meta_campaign_id: str, *, since_days: int = 30) -> dict:
    """Shu kampaniyadan (Meta campaign ID orqali, `dashboard_data.py`dagi
    bilan bir xil konvensiya -- Meta campaign ID global yagona, alohida
    `company_id` filtri qo'yilmaydi) kelgan lidlarning CRM holatini
    (yutilgan/yo'qotilgan/kutilayotgan) qaytaradi. Lid umuman topilmasa
    `{"total": 0}` -- LLM buni "bu kampaniya uchun CRM ma'lumoti yo'q"
    deb to'g'ri talqin qiladi (masalan Xabar/Qo'ng'iroq maqsadli kampaniya)."""
    since = dt.datetime.utcnow() - dt.timedelta(days=since_days)
    effective_created = func.coalesce(Lead.lead_created_time, Lead.created_at)
    leads = (
        session.query(Lead)
        .filter(Lead.campaign_id == meta_campaign_id, effective_created >= since)
        .all()
    )
    if not leads:
        return {"total": 0}
    category_by_key = {fs.key: fs.category for fs in session.query(FunnelStage).all()}
    counts: dict = defaultdict(int)
    for lead in leads:
        category = category_by_key.get(lead.status, "active")
        counts[category] += 1
    won = counts.get("sold", 0)
    lost = counts.get("unqualified", 0)
    decided = won + lost
    return {
        "total": len(leads), "active": counts.get("active", 0), "qualified": counts.get("qualified", 0),
        "won": won, "lost": lost,
        "win_rate_of_decided_percent": round(won / decided * 100, 1) if decided else None,
        "window_days": since_days,
    }


# ---------------------------------------------------------------------------
# LLM PROMPT -- o'rnatilgan Meta Ads yaxshi amaliyotlari (Meta hujjatidan
# so'zma-so'z ko'chirma EMAS -- sohada keng tan olingan, barqaror bilim;
# foydalanuvchiga ham shunday, "o'rnatilgan amaliyotlar" deb taqdim etiladi)
# ---------------------------------------------------------------------------
_BEST_PRACTICES_REFERENCE = """O'RNATILGAN META ADS YAXSHI AMALIYOTLARI (bu Meta'ning rasmiy hujjatidan so'zma-so'z ko'chirma EMAS -- reklama sohasida keng tan olingan, barqaror bilim, foydalanuvchiga ham shunday -- "o'rnatilgan amaliyotlar" deb -- taqdim et, Meta'ning rasmiy tavsiyasi sifatida emas):

AUDITORIYA / CPL:
- Keng (broad) yoki Advantage+ auditoriya odatda tor, bir nechta qiziqishni ustma-ust qo'ygan auditoriyadan ko'ra arzonroq va barqarorroq natija beradi.
- Juda tor/kichik auditoriya odatda yuqori CPL va past yetkazishga olib keladi.

O'RGANISH BOSQICHI (LEARNING) VA BARQARORLIK:
- Meta auction taxminan 7 kunlik oynada ~50 ta optimallashtirish hodisasi (masalan lid) to'plangach "o'rganish" bosqichidan barqaror chiqadi.
- Tez-tez tahrirlash (ayniqsa byudjetni 20%dan ko'proqqa o'zgartirish, yoki auditoriya/kreativni almashtirish) o'rganish bosqichini QAYTA ishga tushiradi va narxni vaqtincha ko'taradi -- agar kampaniya YAQINDA tahrirlangan bo'lsa va CPL yomon ko'rinsa, sabab strukturaviy emas, balki shu bo'lishi mumkin.

CHASTOTA (FREQUENCY):
- Sovuq auditoriya uchun qisqa muddatda chastota ~3-4dan yuqoriga chiqishi odatda reklama charchashi (ad fatigue) belgisi -- CTR pasayadi, CPL ko'tariladi. Yechim -- ko'proq byudjet EMAS, balki yangi kreativ yoki auditoriyani kengaytirish.

KREATIV:
- Bir nechta (3-6 ta) kreativ variant tizimga ko'proq sinov imkonini beradi.
- Uzoq vaqt (bir necha hafta) o'zgarmagan, yuqori chastotali kreativ -- CPL ko'tarilishining odatiy va oson aniqlanadigan sababi.
- Lid reklamalarida reklamaning va'dasi (kreativ/matn) lid formasi so'ragan narsaga MOS bo'lishi kerak -- mos kelmasa, ham konversiya darajasi, ham lid sifati pasayadi.

LID FORMA DIZAYNI:
- Kamroq savol -- odatda yuqoriroq forma to'ldirish darajasi va arzonroq CPL, lekin lid sifati pasayishi mumkin.
- 1-2 ta to'g'ri tanlangan malaka savoli (byudjet/maqsad/muddat) qo'shish -- kompaniyaning haqiqiy muammosi "juda kam lid" emas, "sifatsiz lid" bo'lganda, ongli ravishda ozroq hajm evaziga sifatni oshirishning standart usuli.

BYUDJET / JOYLASHUV:
- Aniq, dalillangan sabab bo'lmasa, avtomatik joylashuvni (placements) yoqiq qoldirish yaxshiroq -- qo'lda cheklash odatda auksionni toraytirib, narxni oshiradi.
- Kunlik byudjet maqsadli natija narxiga nisbatan juda kichik bo'lsa (kuniga ~1 ta natijani ham qoplay olmasa), kampaniya o'rganish bosqichidan chiqishda qiynaladi.

REKLAMA MOSLIK DIAGNOSTIKASI (Ad Relevance Diagnostics):
- Meta reklama yetarli ko'rsatishga ega bo'lgach, uni SHU AUDITORIYA uchun raqobatlashayotgan boshqa reklamalar bilan solishtirib uchta nisbiy reyting beradi: Sifat (Quality), O'zaro aloqa darajasi (Engagement) va Konversiya darajasi (Conversion). O'rtachadan past Sifat -- odatda past sifatli/relevant kreativ yoki yomon post-click/forma tajribasi belgisi; past O'zaro aloqa -- zaif kreativ ilgak (hook); past Konversiya -- reklama va'dasi bilan taklif/auditoriya/forma o'rtasidagi nomuvofiqlik. (Bu reytinglar Replix'ning hozirgi Meta so'rovlarida hali olinmaydi -- kelajakda qo'shish mumkin.)

REPLIX'NING O'Z CRM FARQI (bu Meta'da yo'q, faqat Replix'da bor):
- Meta-tomonidagi CPL yaxshi ko'rinsa-yu, CRM-tomonidagi yutuq darajasi (win rate) past bo'lsa -- odatda sabab targeting biznesga mos emasligi, reklama va'dasi taklifdan ortiqcha va'da berishi, yoki forma juda kam/hech qanday malaka savolisiz sifatsiz lidlarni ham o'tkazib yuborishi.
- CRM-tomonidagi yutuq darajasi yaxshi-yu, CPL/hajm shikoyat bo'lsa -- yechim forma emas, Meta tomonida (targeting/kreativ/byudjet)."""

_DIAGNOSIS_PATHS = [
    "adset.daily_budget", "adset.lifetime_budget", "adset.bid_strategy",
    "adset.targeting.age_min", "adset.targeting.age_max", "adset.targeting.genders",
    "adset.targeting.geo_locations.cities", "adset.targeting.geo_locations.regions", "adset.targeting.geo_locations.countries",
    "adset.targeting.interests", "adset.targeting.behaviors", "adset.targeting.advantage_audience",
    "adset.targeting.placements.mode", "adset.targeting.placements.publisher_platforms",
    "adset.targeting.placements.facebook_positions", "adset.targeting.placements.instagram_positions",
    "ad.primary_text", "ad.headline", "ad.description", "ad.cta",
    "ad.copy_variants.primary_text", "ad.copy_variants.headline", "ad.copy_variants.description",
    "ad.lead_form.new_form.questions",
]
_DIAGNOSIS_PATHS = [p for p in _DIAGNOSIS_PATHS if campaign_draft.is_allowed_path(p)]  # himoya: allowlist o'zgarsa ham sinxron

_SYSTEM_TEMPLATE = """Sen Replix platformasining "Target Analizi" -- jonli (Meta'da allaqachon ishlab turgan) reklama kampaniyasini diagnostika qiluvchi AI agentisan.

Vazifang: senga berilgan HAQIQIY Meta statistikasi + CRM (lid sifati) ma'lumoti + kampaniyaning joriy sozlamalarini pastdagi o'rnatilgan yaxshi amaliyotlarga solishtirib, ANIQ muammolarni DALIL bilan ko'rsatish va ANIQ, qo'llash mumkin bo'lgan o'zgarishlarni taklif qilish.

{best_practices}

QAT'IY QOIDALAR:
- FAQAT JSON qaytar (hech qanday izoh, matn, ``` belgisiz).
- HAR BIR "issue"ning "evidence" maydoni FAQAT senga berilgan HAQIQIY raqamlarga tayanishi kerak -- HECH QANDAY sonni (masalan "sanoat o'rtachasi shuncha foiz") O'YLAB TOPMA. Aniq son bo'lmasa, sifat jihatidan yoz (masalan "chastota yuqori va oshib bormoqda").
- Har bir "changes" elementining "path" maydoni albatta quyidagi ro'yxatdan bo'lishi kerak (boshqa har qanday yo'l RAD ETILADI, serverda qayta tekshiriladi):
{allowed}
- "current_value" -- kampaniyaning HOZIRGI qiymati, "proposed_value" -- taklif qilinayotgan yangi qiymat, AYNAN SHU YO'L kutgan formatda (masalan interests/geo_locations.cities/regions uchun -- [{{"name": "..."}}] NOM bilan, key/id EMAS -- tizim key/id'ni Meta orqali o'zi topadi, sen hech qachon o'ylab topma).
- "ad.lead_form.new_form.questions"ni FAQAT joriy holatda "ad.lead_form.mode" == "new" bo'lsagina taklif qil (mode == "existing" bo'lsa bu maydonni o'zgartirish Meta'dagi MAVJUD formaga hech qanday ta'sir qilmaydi -- buning o'rniga forma bilan bog'liq muammoni "issues"da tasvirlab qo'y, "changes"ga qo'shma).
- Agar biror aniq, dalilga asoslangan taklif topolmasang, "changes"ni bo'sh qoldir -- har safar kamida bitta o'zgarish o'ylab topishga MAJBUR EMASSAN.
- Matnlar o'zbek tilida (lotin).

QAYTARILADIGAN JSON SXEMASI:
{{
  "summary": str,
  "issues": [{{"issue": str, "evidence": str, "severity": "past" | "o'rtacha" | "yuqori"}}],
  "changes": [{{"path": str, "current_value": <har qanday>, "proposed_value": <har qanday>, "why": str}}]
}}"""


def _user_content(ctx: dict, state: dict, performance: dict, crm: dict) -> str:
    lines = [
        company_context_module.company_context_prompt_block(ctx or {}),
        "",
        "# KAMPANIYANING JORIY SOZLAMALARI (ixcham JSON)",
        campaign_draft.compact_state_for_prompt(state),
        "",
        "# META'DAN OLINGAN HAQIQIY STATISTIKA (so'nggi 7/30 kun)",
        json.dumps(performance, ensure_ascii=False),
        "",
        "# CRM (Replix) LID SIFATI MA'LUMOTI (so'nggi 30 kun)",
        json.dumps(crm, ensure_ascii=False),
        "",
        "Yuqoridagi sxema bo'yicha FAQAT JSON qaytar.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ASOSIY KIRISH NUQTASI
# ---------------------------------------------------------------------------

def run_diagnosis(session, draft, company, ctx: dict, *, resolve_geo, resolve_interests) -> dict:
    """Ma'lumot yetarliligini tekshiradi; yetarli bo'lsa Meta + CRM
    ma'lumotini yig'ib LLM'ga yuboradi va natijadagi HAR BIR taklif
    qilingan `path`ni `campaign_draft.is_allowed_path()` bilan QAYTA
    tekshiradi (LLM chiqishi HECH QACHON ishonchli manba emas).

    HECH NARSANI saqlamaydi/o'zgartirmaydi -- faqat o'qiydi. Qaytaradi:
    {"sufficient": bool, "reason": str|None, "performance": dict|None,
     "crm": dict|None, "summary": str|None, "issues": [...], "changes": [...],
     "dropped_paths": [...], "warnings": [...], "generated_at": iso}.

    LLM butunlay ishlamasa (ikkala provayder ham) -- `AnalysisUnavailableError`.
    Kampaniya hali Meta'ga chiqarilmagan bo'lsa ham xuddi shunday (bu
    funksiya faqat jonli/import qilingan kampaniyalar uchun ma'no
    beradi -- gate chaqiruvchida ham, bu yerda ham qo'yilgan)."""
    state = draft.get_state()
    meta_campaign_id = draft.meta_campaign_id
    generated_at = dt.datetime.utcnow().replace(microsecond=0).isoformat()
    if not meta_campaign_id:
        raise AnalysisUnavailableError(
            "Target Analizi faqat Meta'ga chiqarilgan (jonli) kampaniyalar uchun ishlaydi -- "
            "bu qoralama hali nashr qilinmagan."
        )

    goal = (state.get("adset") or {}).get("optimization_goal") or ""
    performance = fetch_performance(company, meta_campaign_id, goal)
    if performance.get("error"):
        return {
            "sufficient": False, "reason": "Meta'dan statistika olinmadi: " + performance["error"],
            "performance": None, "crm": None, "summary": None, "issues": [], "changes": [],
            "dropped_paths": [], "warnings": [], "generated_at": generated_at,
        }

    crm = fetch_crm_summary(session, meta_campaign_id)
    insufficient_reason = check_data_sufficiency(performance, state)
    if insufficient_reason:
        return {
            "sufficient": False, "reason": insufficient_reason,
            "performance": performance, "crm": crm, "summary": None, "issues": [], "changes": [],
            "dropped_paths": [], "warnings": [], "generated_at": generated_at,
        }

    system_prompt = _SYSTEM_TEMPLATE.format(
        best_practices=_BEST_PRACTICES_REFERENCE,
        allowed="\n".join(f"- {p}: {campaign_draft.PATH_TYPES[p]}" for p in _DIAGNOSIS_PATHS),
    )
    user_content = _user_content(ctx, state, performance, crm)
    raw = _llm(system_prompt, user_content)
    if not isinstance(raw, dict):
        raise AnalysisUnavailableError(_ANALYSIS_UNAVAILABLE_MSG)

    issues: list[dict] = []
    for it in (raw.get("issues") or [])[:12]:
        if not isinstance(it, dict):
            continue
        issue_text = str(it.get("issue") or "").strip()
        if not issue_text:
            continue
        severity = str(it.get("severity") or _SEVERITY_DEFAULT).strip()
        if severity not in SEVERITY_VALUES:
            severity = _SEVERITY_DEFAULT
        issues.append({"issue": issue_text[:500], "evidence": str(it.get("evidence") or "").strip()[:500], "severity": severity})

    # --- Taklif qilingan o'zgarishlar: 1) allowlist, 2) nom -> Meta key/id
    # (chat_edit bilan bir xil mexanizm -- LLM hech qachon ID o'ylab topmaydi),
    # 3) tip tekshiruvi (coerce_value). Har bosqichda rad etilgan yo'l
    # `dropped_paths`ga tushadi -- jimgina yo'qolmaydi, javobda ko'rinadi.
    dropped_paths: list[str] = []
    candidates: list[dict] = []
    lead_form_mode = ((state.get("ad") or {}).get("lead_form") or {}).get("mode")
    for ch in (raw.get("changes") or [])[:12]:
        if not isinstance(ch, dict):
            continue
        path = str(ch.get("path") or "").strip()
        if not campaign_draft.is_allowed_path(path) or path not in _DIAGNOSIS_PATHS:
            if path:
                dropped_paths.append(path)
            continue
        if path == "ad.lead_form.new_form.questions" and lead_form_mode != "new":
            dropped_paths.append(path)
            continue
        candidates.append({"path": path, "proposed_value": ch.get("proposed_value"), "why": str(ch.get("why") or "").strip()[:500]})

    warnings: list[str] = []
    changes_dict = {c["path"]: c["proposed_value"] for c in candidates}
    resolved_dict = ai_campaign_planner._resolve_named_items_in_changes(changes_dict, state, resolve_geo, resolve_interests, warnings)

    changes: list[dict] = []
    for c in candidates:
        path = c["path"]
        value = resolved_dict.get(path, c["proposed_value"])
        try:
            coerced = campaign_draft.coerce_value(path, value)
        except campaign_draft.DraftPatchError as e:
            logger.info("target_analysis: taklif rad etildi (%s): %s", path, e)
            dropped_paths.append(path)
            continue
        changes.append({
            "path": path, "current_value": campaign_draft.get_path(state, path),
            "proposed_value": coerced, "why": c["why"],
        })

    return {
        "sufficient": True, "reason": None,
        "performance": performance, "crm": crm,
        "summary": str(raw.get("summary") or "").strip()[:1000],
        "issues": issues, "changes": changes, "dropped_paths": dropped_paths, "warnings": warnings,
        "generated_at": generated_at,
    }
