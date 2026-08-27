"""
dashboard_data.py — bitta web sahifada ko'rsatiladigan barcha KPI'larni
tayyorlaydi: har bir kampaniya/adset/ad uchun xarajat/ko'rishlar (Meta'dan)
+ lead soni/sifat taqsimoti/sotuvlar (bizning CRM bazamizdan).

Meta o'zi "sifatli lead" yoki "sotuv" tushunchasini bilmaydi -- shuning
uchun bularni FAQAT bizning `leads` jadvalimizdan hisoblaymiz, Meta esa
faqat xarajat/ko'rish/klik kabi reklama darajasidagi raqamlarni beradi.

Ustunlar nomi (CAMPAIGN/SPEND/LEADS/CPL/ACTIVE/WON/LOST/QUAL/COST-WON)
foydalanuvchi ko'rsatgan referens dashboard bilan bir xil ma'noda:
  ACTIVE = hali hal bo'lmagan lidlar (status: new yoki contacted)
  QUAL   = sifatli deb belgilangan, lekin hali sotilmagan (status: qualified)
  WON    = sotilgan (status: sold)
  LOST   = sifatsiz deb belgilangan (status: unqualified)
Bu to'rttasi BIR-BIRINI YOPMAYDI (har bir lead faqat bitta guruhda).

MUHIM (2026-08): Hamma target Instant Form (Lead) uchun ishlamaydi -- ba'zi
kampaniya/adset/ad Qo'ng'iroq (Calls), Xabar (Messages), Profilga o'tish,
Havola bosish (Traffic), Konversiya (Sales/Pixel) yoki oddiy Ko'rsatish
(Awareness/Reach) uchun optimallashtirilgan bo'lishi mumkin. Bunday
target'larda CRM'dagi "Leads" ustuni HAQIQATDA 0 bo'ladi -- chunki CRM faqat
Instant Form orqali kelgan lidlarni avtomatik tortib oladi (`lead_sync.py`),
Qo'ng'iroq/Xabar/Profil natijalarini emas. Bu XATO EMAS, balki shu target
boshqa turdagi natija uchun ishlayotganini bildiradi.

Shuning uchun har bir qator uchun Meta'ning `optimization_goal` (adset/ad
darajasida, aniqroq) yoki `objective` (campaign darajasida, taxminiy)
maydoniga qarab TO'G'RI natija turini (`goal`/`goal_label`) va o'sha turga
mos "Meta natija" sonini (`meta_result`/`result_label`) hisoblaymiz --
`actions` massividagi mos `action_type`lardan yig'ib, yoki Reach/Impressions
kabi to'g'ridan-to'g'ri maydonlardan olib. Bu "Leads" ustunidan MUSTAQIL --
"Leads" ustuni doim faqat bizning CRM bazamizdagi haqiqiy lead yozuvlarini
ko'rsatadi, "Meta natija" esa target qanday sozlangan bo'lsa o'sha natijani.
"""

import datetime as dt
from collections import defaultdict

import meta_api
import kpi_bonus
from db import get_session, Lead, FunnelStage

_TASHKENT_OFFSET = dt.timedelta(hours=5)


def _date_preset_bounds_utc(date_preset: str) -> tuple[dt.datetime, dt.datetime] | None:
    """`date_preset` ("today", "yesterday", "last_7d", "last_30d", "last_90d"
    va h.k. -- Meta'ning o'zi ishlatadigan nomlar, `target_page()`dagi davr
    tugmalaridan keladi) uchun Toshkent vaqtida mos KUN ORALIG'INI hisoblab,
    UTC'ga o'tkazib qaytaradi -- CRM'dagi `Lead.created_at` (UTC, naive)
    bilan solishtirish uchun.

    MUHIM (2026-08, foydalanuvchi topgan xato): oldin CRM lead soni sana
    bo'yicha UMUMAN filtrlanmasdi -- "Bugun" davrini tanlasa ham, dashboard
    doim BARCHA vaqtdagi lead sonini ko'rsatardi (masalan bugun 1 ta lead
    kelgan bo'lsa ham, "7" deb ko'rsatgan -- bu shu funksiya yo'qligidan).
    Endi Meta'ning xarajat/ko'rsatish statistikasi qaysi davr uchun
    so'ralayotgan bo'lsa, CRM lead soni ham AYNAN o'sha davr uchun
    hisoblanadi.

    Noma'lum/qo'llab-quvvatlanmaydigan preset uchun `None` qaytaradi (filtr
    qo'yilmaydi -- "shu paytgacha jamlangan hammasi" ma'nosida, eski xatti-
    harakat saqlanib qoladi)."""
    now_tashkent = dt.datetime.utcnow() + _TASHKENT_OFFSET
    today_start_tashkent = now_tashkent.replace(hour=0, minute=0, second=0, microsecond=0)

    if date_preset == "today":
        start_tashkent = today_start_tashkent
        end_tashkent = today_start_tashkent + dt.timedelta(days=1)
    elif date_preset == "yesterday":
        start_tashkent = today_start_tashkent - dt.timedelta(days=1)
        end_tashkent = today_start_tashkent
    elif date_preset.startswith("last_") and date_preset.endswith("d"):
        try:
            days = int(date_preset[len("last_"):-1])
        except ValueError:
            return None
        # Meta'ning "last_Nd" preseti bugungi kunni ham o'z ichiga oladi
        # (aylanma N-kunlik oyna) -- shuning uchun tugash chegarasi ham
        # "ertaga boshlanishi"gacha.
        start_tashkent = today_start_tashkent - dt.timedelta(days=days)
        end_tashkent = today_start_tashkent + dt.timedelta(days=1)
    else:
        return None

    return start_tashkent - _TASHKENT_OFFSET, end_tashkent - _TASHKENT_OFFSET


def custom_range_bounds_utc(date_from: str, date_to: str) -> tuple[dt.datetime, dt.datetime] | None:
    """Foydalanuvchi ANIQ ikkita sanani (masalan Dashboard'dagi "sana
    oralig'i" formasidan, `YYYY-MM-DD`, Toshkent vaqti) tanlaganda ishlatiladi
    -- Meta Ads Manager'dagi "custom range" tanlovi kabi. `_date_preset_bounds_utc`
    tayyor preset'lar (bugun/kecha/last_Nd) uchun, bu esa ixtiyoriy ikkita
    sana uchun. `date_to` kuni HAM TO'LIQ hisobga olinadi (o'sha kunning
    oxirigacha, ya'ni keyingi kun boshigacha)."""
    try:
        start_date = dt.datetime.strptime((date_from or "").strip(), "%Y-%m-%d")
        end_date = dt.datetime.strptime((date_to or "").strip(), "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    start_tashkent = start_date
    end_tashkent = end_date + dt.timedelta(days=1)
    return start_tashkent - _TASHKENT_OFFSET, end_tashkent - _TASHKENT_OFFSET


LEVELS = {
    "campaign": {"id_field": "campaign_id", "name_field": "campaign_name", "lead_attr": "campaign_id"},
    "adset": {"id_field": "adset_id", "name_field": "adset_name", "lead_attr": "adset_id"},
    "ad": {"id_field": "ad_id", "name_field": "ad_name", "lead_attr": "ad_id"},
}

# ---------------------------------------------------------------------------
# Meta natija turlari — "optimization_goal" (adset/ad, aniq) yoki "objective"
# (campaign, taxminiy) qiymatini o'zbekcha tushunarli nomga va shu turga mos
# `actions` ichidagi `action_type`larga bog'laydi.
# ---------------------------------------------------------------------------

GOAL_LABELS = {
    "LEAD_GENERATION": "Lidlar (Instant Forma)",
    "QUALITY_LEAD": "Sifatli lidlar (Instant Forma)",
    "CONVERSATIONS": "Xabarlar (Messenger/WhatsApp)",
    "QUALITY_CALL": "Qo'ng'iroqlar",
    "LINK_CLICKS": "Havola bosishlar (Traffic)",
    "LANDING_PAGE_VIEWS": "Sahifa ko'rishlar",
    "OFFSITE_CONVERSIONS": "Konversiyalar (sayt/pixel)",
    "APP_INSTALLS": "Ilova o'rnatishlar",
    "PROFILE_VISIT": "Profilga o'tishlar",
    "REACH": "Qamrov (Reach)",
    "IMPRESSIONS": "Ko'rsatishlar (Impressions)",
    "THRUPLAY": "Video to'liq ko'rishlar",
    "POST_ENGAGEMENT": "Post bilan o'zaro aloqa",
    "PAGE_LIKES": "Sahifa yoqtirishlar",
    "VALUE": "Qiymat (sotuv/ROAS)",
    "": "Noma'lum turi",
}

# Har bir goal uchun `actions` massividan qidiriladigan action_type'lar
# ro'yxati (Meta'ning standart nomlari). Bir nechtasi berilgan -- birinchi
# topilgani emas, HAMMASI qo'shiladi (ba'zan bir xil harakat 2 xil nom bilan
# qaytishi mumkin, lekin amalda faqat bittasi bo'ladi).
GOAL_RESULT_ACTION_TYPES = {
    "LEAD_GENERATION": ["lead", "leadgen_grouped", "onsite_conversion.lead_grouped"],
    "QUALITY_LEAD": ["lead", "leadgen_grouped", "onsite_conversion.lead_grouped"],
    "CONVERSATIONS": [
        "onsite_conversion.messaging_conversation_started_7d",
        "onsite_conversion.messaging_first_reply",
        "onsite_conversion.total_messaging_connection",
    ],
    "QUALITY_CALL": ["onsite_conversion.total_call", "onsite_conversion.call_confirm", "call_confirm"],
    "LINK_CLICKS": ["link_click"],
    "LANDING_PAGE_VIEWS": ["landing_page_view"],
    "OFFSITE_CONVERSIONS": [
        "offsite_conversion.fb_pixel_purchase", "purchase", "omni_purchase",
        "offsite_conversion.fb_pixel_lead", "offsite_conversion.fb_pixel_complete_registration",
    ],
    "APP_INSTALLS": ["mobile_app_install", "omni_app_install"],
    "PROFILE_VISIT": ["ig_profile_visit", "onsite_conversion.total_messaging_connection"],
    "POST_ENGAGEMENT": ["post_engagement"],
    "PAGE_LIKES": ["like"],
}

# Campaign darajasida faqat kengroq `objective` bor (bitta campaign ichida
# bir nechta adset turli optimization_goal bilan bo'lishi mumkin) -- shuning
# uchun campaign qatorlari uchun TAXMINIY (eng odatiy) goal'ga moslaymiz.
OBJECTIVE_TO_TYPICAL_GOAL = {
    "OUTCOME_LEADS": "LEAD_GENERATION",
    "OUTCOME_ENGAGEMENT": "POST_ENGAGEMENT",
    "OUTCOME_TRAFFIC": "LINK_CLICKS",
    "OUTCOME_SALES": "OFFSITE_CONVERSIONS",
    "OUTCOME_AWARENESS": "REACH",
    "OUTCOME_APP_PROMOTION": "APP_INSTALLS",
    "OUTCOME_MESSAGES": "CONVERSATIONS",
    # Eski (2022 yilgacha) objective nomlari -- ba'zi eski kampaniyalarda hali ham uchraydi:
    "LEAD_GENERATION": "LEAD_GENERATION",
    "LINK_CLICKS": "LINK_CLICKS",
    "CONVERSIONS": "OFFSITE_CONVERSIONS",
    "REACH": "REACH",
    "BRAND_AWARENESS": "REACH",
    "APP_INSTALLS": "APP_INSTALLS",
    "MESSAGES": "CONVERSATIONS",
    "POST_ENGAGEMENT": "POST_ENGAGEMENT",
    "VIDEO_VIEWS": "REACH",
}


def _extract_action_count(actions: list[dict] | None, action_types: list[str]) -> int:
    total = 0
    found = False
    for a in (actions or []):
        if a.get("action_type") in action_types:
            try:
                total += int(float(a.get("value", 0)))
                found = True
            except (TypeError, ValueError):
                continue
    return total if found else 0


def _extract_lead_count_from_actions(actions: list[dict] | None) -> int:
    return _extract_action_count(actions, GOAL_RESULT_ACTION_TYPES["LEAD_GENERATION"])


def _resolve_meta_result(goal: str, actions: list[dict] | None, reach: int, impressions: int) -> tuple[int, str]:
    """Target qanday natija uchun sozlangan bo'lsa, o'sha natijaning haqiqiy
    sonini va o'zbekcha nomini qaytaradi. Aniq mos kelmasa, oxirgi chora
    sifatida ko'rsatishlar sonini (Impressions) belgi bilan qaytaradi --
    hech qachon "0, sabab noma'lum" holatida qoldirmaslik uchun."""
    if goal == "REACH":
        return reach or 0, GOAL_LABELS["REACH"]
    if goal == "IMPRESSIONS":
        return impressions or 0, GOAL_LABELS["IMPRESSIONS"]

    action_types = GOAL_RESULT_ACTION_TYPES.get(goal)
    if action_types:
        count = _extract_action_count(actions, action_types)
        if count:
            return count, GOAL_LABELS.get(goal, goal)
        # Goal ma'lum, lekin shu turdagi harakat topilmadi (masalan hali
        # hech kim qo'ng'iroq qilmagan) -- 0 to'g'ri qiymat, lekin nomi
        # o'sha goal'niki bo'lib qolishi kerak (Leads emas!).
        return 0, GOAL_LABELS.get(goal, goal)

    # Goal umuman noma'lum/bo'sh -- Lead sifatida tekshirib ko'ramiz (eski
    # ma'lumotlar yoki API'dan optimization_goal kelmagan holatlar uchun),
    # topilmasa ko'rsatishlar sonini qaytaramiz.
    lead_fallback = _extract_lead_count_from_actions(actions)
    if lead_fallback:
        return lead_fallback, GOAL_LABELS["LEAD_GENERATION"]
    return impressions or 0, GOAL_LABELS["IMPRESSIONS"]


def get_kpis(level: str = "campaign", date_preset: str = "last_30d", active_only: bool = False) -> dict:
    """Qaytaradi: {"rows": [...], "totals": {...}, "goal_breakdown": [...],
    "generated_at": ISO, "level": level}

    Har bir qatorda: id, name, status, spend, impressions, reach, meta_leads,
    crm_leads_total, active, qualified, unqualified, sold, revenue, cpl,
    cost_per_won, avg_check, roi_percent, goal, goal_label, meta_result,
    result_label."""
    cfg = LEVELS.get(level, LEVELS["campaign"])
    id_field, name_field = cfg["id_field"], cfg["name_field"]

    try:
        insight_rows = meta_api.get_insights(
            level=level,
            date_preset=date_preset,
            fields=[id_field, name_field, "spend", "impressions", "reach", "actions"],
        )
    except meta_api.MetaAPIError as e:
        return {"error": str(e), "rows": [], "totals": {}, "goal_breakdown": [], "generated_at": dt.datetime.utcnow().isoformat(), "level": level}

    status_by_id = {}
    goal_by_id = {}
    child_count_by_id = {}
    try:
        structure = meta_api.get_account_structure(active_only=False)
        key = {"campaign": "campaigns", "adset": "adsets", "ad": "ads"}[level]
        status_by_id = {o["id"]: o.get("status", "") for o in structure.get(key, [])}

        if level == "campaign":
            for c in structure.get("campaigns", []):
                goal_by_id[c["id"]] = OBJECTIVE_TO_TYPICAL_GOAL.get(c.get("objective", ""), "")
            # MUHIM (2026-08, foydalanuvchi topgan chalkashlik: "3 ta target
            # yoqilgan, lekin Campaigns bo'limi faqat 1 tasini ko'rsatvoti"):
            # bu ODATDA XATO EMAS -- Meta'da bitta KAMPANIYA ichida bir nechta
            # REKLAMA GURUHI (ad set) bo'lishi juda oddiy holat (masalan har
            # bir mahsulot/sana uchun alohida ad set, hammasi bitta kampaniya
            # ichida). "Campaigns" bo'limi har doim bitta kampaniya uchun
            # BITTA qator ko'rsatadi -- foydalanuvchi "target" deb atagan
            # narsalar ko'pincha aslida shu kampaniyaning ICHIDAGI ad set'lar
            # bo'ladi, ular "Ad sets" bo'limida ko'rinadi. Shuni tepada aniq
            # ko'rsatish uchun har bir kampaniyaning nechta ad set'i borligini
            # hisoblab, qatorga qo'shamiz (shablon shuni yozadi).
            adsets_per_campaign = defaultdict(int)
            for a in structure.get("adsets", []):
                if a.get("campaign_id"):
                    adsets_per_campaign[a["campaign_id"]] += 1
            child_count_by_id = dict(adsets_per_campaign)
        elif level == "adset":
            for a in structure.get("adsets", []):
                goal_by_id[a["id"]] = a.get("optimization_goal", "") or ""
        else:  # ad -- goal aslida adset'niki, ad -> adset_id -> optimization_goal orqali topiladi
            goal_by_adset = {a["id"]: (a.get("optimization_goal", "") or "") for a in structure.get("adsets", [])}
            for ad in structure.get("ads", []):
                goal_by_id[ad["id"]] = goal_by_adset.get(ad.get("adset_id"), "")
    except meta_api.MetaAPIError:
        pass

    meta_by_id = {}
    for row in insight_rows:
        oid = row.get(id_field)
        if not oid:
            continue
        actions = row.get("actions")
        impressions = int(row.get("impressions", 0) or 0)
        reach = int(row.get("reach", 0) or 0)
        goal = goal_by_id.get(oid, "")
        meta_result, result_label = _resolve_meta_result(goal, actions, reach, impressions)
        meta_by_id[oid] = {
            "id": oid,
            "name": row.get(name_field, ""),
            "status": status_by_id.get(oid, ""),
            "spend": float(row.get("spend", 0) or 0),
            "impressions": impressions,
            "reach": reach,
            "meta_leads": _extract_lead_count_from_actions(actions),
            "goal": goal,
            "goal_label": GOAL_LABELS.get(goal, GOAL_LABELS[""]),
            "meta_result": meta_result,
            "result_label": result_label,
            "child_adset_count": child_count_by_id.get(oid) if level == "campaign" else None,
        }

    # CRM tomonidagi lead statistikasi -- tanlangan `date_preset` bilan BIR
    # XIL davr uchun (yuqoridagi Meta xarajat/ko'rsatish so'rovi ham shu
    # davr uchun edi). Preset tanib bo'lmasa (masalan kelajakda yangi
    # preset qo'shilsa), eski xatti-harakat -- filtrisiz, hammasi -- saqlanadi.
    date_bounds = _date_preset_bounds_utc(date_preset)
    session = get_session()
    try:
        lead_query = session.query(Lead)
        if date_bounds:
            start_utc, end_utc = date_bounds
            lead_query = lead_query.filter(Lead.created_at >= start_utc, Lead.created_at < end_utc)
        leads = lead_query.all()
        # voronka bosqichi (key) -> kategoriya (active/qualified/unqualified/sold)
        # xaritasi -- admin bosqichlarni o'zgartirgan/qo'shgan bo'lsa ham, dashboard
        # to'g'ri kategoriyaga hisoblaydi (custom_fields_settings/funnel_settings'da
        # belgilangan `category` orqali).
        category_by_key = {fs.key: fs.category for fs in session.query(FunnelStage).all()}
    finally:
        session.close()

    lead_attr = cfg["lead_attr"]
    crm_by_id = defaultdict(lambda: {
        "crm_leads_total": 0, "active": 0, "qualified": 0,
        "unqualified": 0, "sold": 0, "revenue": 0.0,
    })
    for lead in leads:
        oid = getattr(lead, lead_attr, None) or "unknown"
        bucket = crm_by_id[oid]
        bucket["crm_leads_total"] += 1
        category = category_by_key.get(lead.status, "active")  # noma'lum/eski status -> "active" deb hisoblanadi
        if category == "active":
            bucket["active"] += 1
        elif category == "qualified":
            bucket["qualified"] += 1
        elif category == "unqualified":
            bucket["unqualified"] += 1
        elif category == "sold":
            bucket["sold"] += 1
            bucket["revenue"] += (lead.sale_amount or 0.0)

    all_ids = set(meta_by_id) | set(crm_by_id)
    if active_only:
        # Faqat hozir yoqilgan (ACTIVE) target'larni ko'rsatish -- pauzadagi/
        # arxivlangan kampaniyalar va Meta'da umuman topilmagan (masalan qo'lda
        # qo'shilgan) lead guruhlari ro'yxatdan chiqariladi. Foydalanuvchi
        # dashboard'da "Hammasini ko'rsatish" havolasi orqali bularni ham
        # ko'ra oladi (active_only=False holatga qaytadi).
        all_ids = {oid for oid in all_ids if meta_by_id.get(oid, {}).get("status") == "ACTIVE"}
    rows = []
    totals = {
        "spend": 0.0, "impressions": 0, "reach": 0, "meta_leads": 0, "crm_leads_total": 0,
        "active": 0, "qualified": 0, "unqualified": 0, "sold": 0, "revenue": 0.0,
        "_effective_leads": 0,
    }
    goal_totals = defaultdict(lambda: {"count": 0, "spend": 0.0, "meta_result": 0, "result_label": ""})
    # MUHIM (2026-08, foydalanuvchi topgan xato): `revenue` (sotuv summasi)
    # CRM'da SO'MDA saqlanadi, `spend` (reklama xarajati) esa Meta'dan
    # to'g'ridan-to'g'ri DOLLARDA keladi. Ikkalasini valyutasiga qaramasdan
    # to'g'ridan-to'g'ri ayirib/bo'lish ROI'ni yuzlab million foizga
    # "buzib" ko'rsatardi (masalan $1500000 "daromad" aslida 1 500 000 so'm
    # -- atigi ~$120 edi). Endi ROI hisoblashdan OLDIN daromad dollarga
    # o'tkaziladi (kurs: Sozlamalar -> Umumiy'dan tahrirlanadi).
    usd_to_uzs_rate = kpi_bonus.get_usd_to_uzs_rate()
    for oid in all_ids:
        meta_part = meta_by_id.get(oid, {
            "id": oid, "name": "(noma'lum)", "status": "",
            "spend": 0.0, "impressions": 0, "reach": 0, "meta_leads": 0,
            "goal": "", "goal_label": GOAL_LABELS[""], "meta_result": 0, "result_label": GOAL_LABELS[""],
        })
        crm_part = crm_by_id.get(oid, {
            "crm_leads_total": 0, "active": 0, "qualified": 0,
            "unqualified": 0, "sold": 0, "revenue": 0.0,
        })
        row = {**meta_part, **crm_part}
        # CPL (lid narxi) -- ODATDA CRM'dagi HAQIQIY lead yozuvlari soniga
        # asoslanadi. LEKIN Lead-generatsiya turidagi target uchun ba'zan
        # Meta'da lead kelgani aniq (`meta_result`/`meta_leads` > 0), lekin
        # CRM sinxronizatsiyasi hali ulgurmagan yoki campaign_id mos
        # kelmagan bo'lishi mumkin -- bunday holda CPL "$0.00" ko'rsatib,
        # "narx yo'q" degandek noto'g'ri taassurot qoldiradi. Shuning uchun
        # CRM'da 0 bo'lsa-yu, lekin bu Lead-turi target bo'lsa, Meta'ning
        # o'zi hisoblagan lead sonidan (meta_result yoki meta_leads,
        # qaysi biri kattaroq bo'lsa) foydalanamiz.
        effective_leads = row["crm_leads_total"]
        if not effective_leads and row.get("goal") in ("LEAD_GENERATION", "QUALITY_LEAD"):
            effective_leads = max(row.get("meta_result") or 0, row.get("meta_leads") or 0)
        row["cpl"] = (row["spend"] / effective_leads) if effective_leads else 0.0
        row["cost_per_won"] = (row["spend"] / row["sold"]) if row["sold"] else 0.0
        row["avg_check"] = (row["revenue"] / row["sold"]) if row["sold"] else 0.0
        revenue_usd = row["revenue"] / usd_to_uzs_rate
        row["revenue_usd"] = revenue_usd
        row["roi_percent"] = ((revenue_usd - row["spend"]) / row["spend"] * 100.0) if row["spend"] else 0.0
        row["cost_per_result"] = (row["spend"] / row["meta_result"]) if row["meta_result"] else 0.0
        rows.append(row)

        for k in ("spend", "impressions", "reach", "meta_leads", "crm_leads_total",
                   "active", "qualified", "unqualified", "sold", "revenue"):
            totals[k] += row[k]
        totals["_effective_leads"] += effective_leads

        gb = goal_totals[row["goal_label"]]
        gb["count"] += 1
        gb["spend"] += row["spend"]
        gb["meta_result"] += row["meta_result"]
        gb["result_label"] = row["result_label"]

    rows.sort(key=lambda r: r["spend"], reverse=True)
    totals["cpl"] = (totals["spend"] / totals["_effective_leads"]) if totals["_effective_leads"] else 0.0
    del totals["_effective_leads"]
    totals["cost_per_won"] = (totals["spend"] / totals["sold"]) if totals["sold"] else 0.0
    totals["avg_check"] = (totals["revenue"] / totals["sold"]) if totals["sold"] else 0.0
    totals["revenue_usd"] = totals["revenue"] / usd_to_uzs_rate
    totals["roi_percent"] = ((totals["revenue_usd"] - totals["spend"]) / totals["spend"] * 100.0) if totals["spend"] else 0.0
    totals["usd_to_uzs_rate"] = usd_to_uzs_rate

    goal_breakdown = []
    for label, g in goal_totals.items():
        goal_breakdown.append({
            "goal_label": label,
            "count": g["count"],
            "spend": g["spend"],
            "meta_result": g["meta_result"],
            "result_label": g["result_label"],
            "cost_per_result": (g["spend"] / g["meta_result"]) if g["meta_result"] else 0.0,
        })
    goal_breakdown.sort(key=lambda g: g["spend"], reverse=True)

    return {
        "rows": rows, "totals": totals, "goal_breakdown": goal_breakdown,
        "generated_at": dt.datetime.utcnow().isoformat(), "level": level,
    }


# Eski nom bilan moslik (app.py hali shu nomni chaqirishi mumkin bo'lsa) --
# yangi kodda to'g'ridan-to'g'ri get_kpis() ishlatiladi.
def get_campaign_kpis(date_preset: str = "last_30d") -> dict:
    data = get_kpis(level="campaign", date_preset=date_preset)
    data["campaigns"] = data.pop("rows")
    return data
