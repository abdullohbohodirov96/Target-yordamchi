"""
orchestrator.py — Targetolog + Marketolog ikki agentli tsiklni boshqaradi.

Oqim:
  1. Meta Marketing API'dan so'nggi ma'lumotlarni (insights + region breakdown) ol.
  2. Targetolog agent (Claude) shu ma'lumotni tahlil qilib, action_plan (JSON) beradi.
  3. Marketolog agent (Claude) action_plan'ni biznes qoidalariga solishtirib
     tekshiradi, har bir action uchun approved/rejected/edited qaror chiqaradi.
  4. Faqat tasdiqlangan action'lar meta_api.py orqali haqiqiy hisobda bajariladi.
  5. Har bir tsikl natijasi logs/run_<timestamp>.json fayliga yoziladi va
     Telegram uchun inson o'qiydigan hisobot qaytariladi.

ISHGA TUSHIRISH:
    pip install anthropic requests
    export ANTHROPIC_API_KEY=...
    export META_ACCESS_TOKEN=...
    export META_AD_ACCOUNT_ID=act_...
    python orchestrator.py          # bitta marta tahlil tsiklini ishga tushiradi
"""

import os
import re
import json
import logging
import concurrent.futures
from pathlib import Path
from datetime import datetime, timedelta

import anthropic
import requests

import meta_api
import budget_tracker
import kv_store
import monthly_report
import db
import dashboard_data

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("orchestrator")

BASE_DIR = Path(__file__).parent
AGENTS_DIR = BASE_DIR / "agents"

# MUHIM (Vercel/serverless muhitlar uchun): loyiha papkasi (BASE_DIR) serverless
# funksiyada FAQAT O'QISH uchun ochiq — yozish (mkdir/write) imkonsiz bo'lishi
# mumkin ("Read-only file system" xatosi, bu butun modulni import qilishda
# ko'tarilib, HAR BIR so'rovni "FUNCTION_INVOCATION_FAILED" bilan buzadi).
# Shuning uchun avval BASE_DIR/logs'ga yozishga urinamiz (VPS/mahalliy uchun —
# haqiqiy, doimiy log), muvaffaqiyatsiz bo'lsa /tmp'ga qaytamiz (Vercel'da
# yagona yoziladigan joy — instance ichida vaqtinchalik, lekin dastur
# yiqilib qolmaydi).
import tempfile as _tempfile

LOGS_DIR = BASE_DIR / "logs"
try:
    LOGS_DIR.mkdir(exist_ok=True)
except OSError:
    LOGS_DIR = Path(_tempfile.gettempdir()) / "target_master_logs"
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

KNOWLEDGE_BASE = (BASE_DIR / "target_master_agent.md").read_text(encoding="utf-8")
TARGETOLOG_ROLE = (AGENTS_DIR / "targetolog_system_prompt.md").read_text(encoding="utf-8")
MARKETOLOG_ROLE = (AGENTS_DIR / "marketolog_system_prompt.md").read_text(encoding="utf-8")
ACTION_SCHEMA = (AGENTS_DIR / "action_schema.md").read_text(encoding="utf-8")
BUSINESS_RULES = json.loads((BASE_DIR / "business_rules.json").read_text(encoding="utf-8"))

TARGETOLOG_SYSTEM = f"{TARGETOLOG_ROLE}\n\n---\n\n# BILIM BAZASI\n\n{KNOWLEDGE_BASE}\n\n---\n\n{ACTION_SCHEMA}"
MARKETOLOG_SYSTEM = f"{MARKETOLOG_ROLE}\n\n---\n\n{ACTION_SCHEMA}"

# MODEL TANLASH STRATEGIYASI (xarajatni balanslash uchun -- ataylab qilingan qaror):
#   - MODEL (Sonnet) -- FAQAT HAQIQIY vazifa/qaror yaratish uchun: Targetolog
#     action_plan tuzganda (yangi kampaniya, byudjet/auditoriya o'zgarishi,
#     murakkab tashxis) va Marketolog tekshiruvida. Bu joylarda chuqur
#     mulohaza va bilim bazasiga tayanish kerak -- shuning uchun Anthropic
#     ishlatiladi va FAQAT shu yerda ishlatiladi.
#   - Boshqa HAMMA narsa -- intent aniqlash, oddiy metrika savoliga real
#     raqamlar bilan javob berish (`answer_data_question`), davr/sana
#     aniqlash (`_resolve_query_period`), byudjet xabarini tushunish, kunlik
#     hisobotlar va oddiy erkin suhbat -- FAQAT OpenAI orqali ishlaydi
#     (`call_light`/`call_light_chat`). Bular "vazifa yaratish" emas, faqat
#     o'qish/tushuntirish -- Anthropic API xarajatini bu yerda umuman
#     sarflamaslik uchun Claude'ga fallback ATAYLAB OLIB TASHLANGAN: agar
#     `OPENAI_API_KEY` sozlanmagan yoki OpenAI so'rovi xato bersa, funksiya
#     xato qaytaradi (Claude Haiku'ga sirli tushib qolmaydi) -- chaqiruvchi
#     joy buni ushlab, foydalanuvchiga tushunarli xabar ko'rsatadi.
# 2026-08, foydalanuvchi so'rovi ("AI tokenlarni ko'p ichvoyapti, ekonom
# ishlasin"): tahlil paytida aniqlandi -- `claude-sonnet-4-5` (versiya
# raqamisiz "alias") Anthropic'ning rasmiy narx sahifasida ENDI asosiy
# API'da ko'rsatilmaydi (faqat Bedrock/Google Cloud'da qoldirilgan),
# o'rniga `claude-sonnet-5` YANGI standart model bo'lgan -- HAM arzonroq
# ($2/$10 har MTok, avvalgisi $3/$15 edi -- kirish/chiqish narxi ~33%
# past), HAM yangiroq avlod (sifat pasaymaydi, aksincha). Shuning uchun
# Targetolog/Marketolog OG'IR chaqiruvlari endi shu modelga o'tkazildi --
# bu HECH QANDAY sifat yo'qotmasdan (aksincha) darhol ~33% xarajat
# tejaydi, kod/mantiqda boshqa hech narsa o'zgarmaydi.
MODEL = "claude-sonnet-5"
LIGHT_MODEL = "claude-haiku-4-5-20251001"  # endi ishlatilmaydi -- moslik uchun saqlangan
INTENT_MODEL = LIGHT_MODEL  # eski nom -- moslik uchun saqlangan
client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# OpenAI -- YENGIL so'rovlarning YAGONA manbasi (intent aniqlash, metrika
# savoliga javob, davr/sana aniqlash, oddiy suhbat, byudjet xabarini
# tushunish, kunlik hisobotlar). Fallback yo'q -- OPENAI_API_KEY majburiy
# sozlanishi kerak, aks holda shu yo'nalishdagi so'rovlar xato beradi.
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

# OG'IR vazifalar (Targetolog/Marketolog action_plan) uchun ZAXIRA yo'l.
# 2026-08, foydalanuvchi so'rovi: yuqoridagi "ATAYLAB fallback yo'q" qarori
# amalda Anthropic hisobida balans tugab qolganda BUTUN botni ishlamay
# qo'yardi (xom "credit balance is too low" xatosi to'g'ridan-to'g'ri
# foydalanuvchiga ko'rinardi) -- bu yengil so'rovlar uchun maqsadli
# xarajat tejashdan farqli, kutilmagan uzilish edi. Endi: ANTHROPIC hamon
# BIRINCHI navbatda ishlatiladi (sifati yaxshiroq, bilim bazasiga
# chuqurroq tayanadi), lekin `anthropic.APIError` (balans tugashi, rate
# limit, xizmat vaqtincha ishlamasligi va h.k.) ko'tarilsa, xuddi shu
# so'rov OpenAI'ga (ATAYLAB yengil `OPENAI_MODEL` emas, kuchliroq
# `OPENAI_FALLBACK_MODEL` -- standart gpt-4o) qayta yuboriladi, shunda bot
# butunlay to'xtab qolmaydi.
OPENAI_FALLBACK_MODEL = os.environ.get("OPENAI_FALLBACK_MODEL", "gpt-4o")

_ANTHROPIC_FALLBACK_MESSAGE = (
    "Kechirasiz, hozir AI xizmatida vaqtinchalik uzilish bor — balans "
    "tugagan yoki xizmat javob bermayotgan bo'lishi mumkin. Administrator "
    "hisobni tekshirib to'ldirsin, birozdan so'ng qayta urinib ko'ring \U0001F64F"
)


class AgentUnavailableError(Exception):
    """Anthropic HAM, OpenAI zaxira yo'li HAM ishlamasa (masalan ikkalasining
    ham balansi tugagan yoki kaliti yo'q) ko'tariladi. Xabar matni ODAM
    O'QIYDIGAN va muloyim -- chaqiruvchi joylar buni to'g'ridan-to'g'ri
    foydalanuvchiga ko'rsatishi mumkin, xom texnik xato emas."""


def friendly_error_message(e: Exception) -> str:
    """Telegram yoki veb AI-yordamchi orqali foydalanuvchiga ko'rsatish
    uchun XAVFSIZ matn qaytaradi. Anthropic/OpenAI'dan kelgan xom texnik
    xatolarni (masalan "credit balance is too low", HTTP status kodi,
    Python traceback matni) HECH QACHON to'g'ridan-to'g'ri ko'rsatmaydi --
    ular uchun muloyim umumiy xabar beradi. Boshqa (masalan MetaAPIError,
    TargetologFormatError) xatolar allaqachon odam o'qiydigan bo'lgani
    uchun o'zgarishsiz qaytariladi."""
    if isinstance(e, AgentUnavailableError):
        return str(e)
    if isinstance(e, anthropic.APIError):
        return _ANTHROPIC_FALLBACK_MESSAGE
    lowered = str(e).lower()
    if (
        isinstance(e, requests.exceptions.RequestException)
        or "openai_api_key" in lowered
        or "credit" in lowered
        or "balance" in lowered
        or "quota" in lowered
    ):
        return (
            "Kechirasiz, hozir AI xizmatida vaqtinchalik muammo bor — "
            "balans tugagan yoki xizmat javob bermayapti. Administrator "
            "tekshirib qo'ysin, birozdan so'ng qayta urinib ko'ring \U0001F64F"
        )
    return f"⚠️ Xatolik: {e}"


def call_light(system_prompt: str, user_content: str, max_tokens: int = 500) -> str:
    """Bitta-turli (single-turn) YENGIL chaqiruv: intent aniqlash, byudjet
    xabarini tushunish, metrika savoliga javob berish, kunlik hisobotlar --
    shularning barchasi FAQAT OpenAI orqali ishlaydi (Anthropic API
    xarajatini yengil/oddiy so'rovlarda sarflamaslik uchun, ataylab qilingan
    qaror). `OPENAI_API_KEY` sozlanmagan yoki OpenAI so'rovi xato bersa --
    Claude'ga tushib qolinmaydi, xato yuqoriga uzatiladi (chaqiruvchi
    funksiya buni ushlab, foydalanuvchiga tushunarli xabar ko'rsatadi)."""
    openai_key = os.environ.get("OPENAI_API_KEY")
    if not openai_key:
        raise RuntimeError(
            "OPENAI_API_KEY sozlanmagan -- yengil so'rovlar (metrika/intent/"
            "byudjet/hisobot) faqat OpenAI orqali ishlaydi, Anthropic'ga "
            "tushib qolinmaydi."
        )
    return _call_openai(openai_key, system_prompt, [{"role": "user", "content": user_content}], max_tokens)


def call_light_chat(system_prompt: str, messages: list[dict], max_tokens: int = 1000) -> str:
    """`call_light()`ga o'xshaydi, lekin ko'p-turli (multi-turn) suhbat
    tarixi (`messages`, {"role", "content"} ro'yxati) bilan ishlaydi --
    erkin/umumiy suhbat rejimida ishlatiladi. FAQAT OpenAI -- Claude'ga
    fallback yo'q (Anthropic API faqat haqiqiy qaror/tavsiya beradigan
    vazifalar -- Targetolog/Marketolog action_plan -- uchun saqlanadi)."""
    openai_key = os.environ.get("OPENAI_API_KEY")
    if not openai_key:
        raise RuntimeError(
            "OPENAI_API_KEY sozlanmagan -- erkin suhbat ham faqat OpenAI "
            "orqali ishlaydi."
        )
    return _call_openai(openai_key, system_prompt, messages, max_tokens)



def _call_openai(api_key: str, system_prompt: str, messages: list[dict], max_tokens: int) -> str:
    full_messages = [{"role": "system", "content": system_prompt}] + list(messages)
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        json={"model": OPENAI_MODEL, "temperature": 0, "max_tokens": max_tokens, "messages": full_messages},
        # MUHIM: avval 20 soniya edi -- OpenAI ba'zan shundan sekinroq javob
        # berib, keraksiz "Read timed out" xatosiga olib kelardi (Claude'ga
        # fallback endi yo'qligi uchun bu to'g'ridan-to'g'ri foydalanuvchiga
        # ko'rinadi). 55ga oshirildi -- bu Vercel funksiyasining o'zi 60
        # soniyada MAJBURIY to'xtaydigan chegarasidan atigi 5 soniya kam
        # (Telegram xabar yuborish/o'chirish uchun ozgina joy qoldirish
        # uchun). Butunlay chegarasiz qilib bo'lmaydi -- Vercel baribir 60
        # soniyada funksiyani o'zi majburan to'xtatadi (504), shuning uchun
        # 55 -- amalda erishsa bo'ladigan ENG UZUN vaqt.
        timeout=55,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]

# Har bir action_plan tipi -> uni haqiqiy hisobda bajaradigan funksiya
# Har bir action_type uchun ODDIY, ODAM O'QIYDIGAN fe'l -- guruhga
# yuboriladigan hisobotda "N ta o'zgarish qildim" deyish o'rniga, ANIQ
# qaysi target'ga NIMA qilinganini ko'rsatish uchun (foydalanuvchi aniq
# shuni so'radi: "guruhga tog'rilangan qilingan narsalarni yozsin").
_ACTION_FRIENDLY_VERB = {
    "pause_ad": "to'xtatdim",
    "resume_ad": "qayta ishga tushirdim",
    "archive_campaign": "arxivladim",
    "increase_budget": "byudjetini oshirdim",
    "decrease_budget": "byudjetini kamaytirdim",
    "fix_region_targeting": "hudud sozlamasini tuzatdim",
    "adjust_audience": "auditoriyasini o'zgartirdim",
    "launch_campaign": "yangi kampaniya sifatida yaratdim",
    "start_ab_test": "A/B testni boshladim",
    "conclude_ab_test": "A/B testni yakunladim (g'olibni tanladim)",
    "schedule_on_off": "kunlik avtomatik yoqish/o'chirish jadvalini qo'ydim",
    "schedule_report": "qo'shimcha doimiy hisobot vaqtini qo'shdim",
    "cancel_standing_task": "doimiy vazifani bekor qildim",
    "replace_creative": "reklama matnini yangiladim",
}

ACTION_EXECUTORS = {
    "pause_ad": lambda a: _execute_and_verify_status(a["object_id"], "PAUSED"),
    "resume_ad": lambda a: _execute_and_verify_status(a["object_id"], "ACTIVE"),
    "archive_campaign": lambda a: _execute_and_verify_status(a["object_id"], "ARCHIVED"),
    "increase_budget": lambda a: _execute_adjust_budget(a, "increase"),
    "decrease_budget": lambda a: _execute_adjust_budget(a, "decrease"),
    "fix_region_targeting": lambda a: _execute_fix_region(a),
    "adjust_audience": lambda a: _execute_adjust_audience(a),
    "launch_campaign": lambda a: _execute_launch_campaign(a),
    "start_ab_test": lambda a: _execute_ab_test(a),
    "conclude_ab_test": lambda a: (
        meta_api.pause_object(a["params"]["losing_adset_id"])
        if a.get("params", {}).get("losing_adset_id")
        else {"status": "no_loser_specified"}
    ),
    "replace_creative": lambda a: _execute_replace_creative(a),
    # create_instant_form MVP bosqichida avtomatik ijro etilmaydi -- forma
    # yaratish odatda bir martalik/kamdan-kam va tasdiqlash talab qiladigan
    # qadam, shuning uchun faqat taklif sifatida odamga (Telegram orqali)
    # ko'rsatiladi. replace_creative esa (2026-08, foydalanuvchi so'rovi bilan)
    # ENDI AVTOMATIK ijro etiladi -- mavjud rasm/video SAQLANIB, faqat matn
    # (`final_primary_text`/`final_headline`) yangilanadi va yangi creative
    # to'g'ridan-to'g'ri reklamaga biriktiriladi (pastga, `_execute_replace_creative`
    # ga qarang). Butunlay YANGI vizual kerak bo'lgan holatlar hali ham odam
    # dizaynerga TZ sifatida `summary`da chiqariladi -- bu action turi orqali emas.
}


def _execute_and_verify_status(object_id: str, expected_status: str) -> dict:
    """pause_ad/resume_ad uchun: Meta'ga status o'zgartirish so'rovini yuboradi,
    KEYIN qayta o'qib haqiqatan o'zgarganini tekshiradi. Meta ba'zan
    {"success": true} qaytaradi-yu, holat aslida o'zgarmagan bo'lishi mumkin
    (masalan yuqori darajadagi adset/kampaniya o'chiq bo'lsa) — bu holda
    "bajarildi" deb yolg'on hisobot berilmasligi uchun xato ko'taramiz."""
    meta_api.set_status(object_id, expected_status)

    info = meta_api.get_object_status(object_id)
    actual_status = info.get("status")
    if actual_status != expected_status:
        raise meta_api.MetaAPIError({
            "message": (
                f"Meta so'rovni qabul qildi, lekin qayta tekshirganda holat "
                f"hali ham '{actual_status}' (kutilgan: '{expected_status}'). "
                "Ehtimol yuqori darajadagi kampaniya/adset boshqa holatda "
                "(masalan o'zi PAUSED). Ads Manager'da qo'lda tekshiring."
            ),
            "expected_status": expected_status,
            "actual_status": actual_status,
        })
    return {"status": actual_status, "verified": True}


def _require(action: dict, *path: str):
    """`action["params"]["audience_change"]["city_key"]` kabi chuqur maydonlarga
    XAVFSIZ kirish uchun yordamchi. Agar Targetolog kutilgan strukturani
    bermagan bo'lsa (masalan schema'ga to'liq amal qilmasa), Python'ning xom
    KeyError'i o'rniga aniq, tushunarli MetaAPIError ko'taradi — shu tufayli
    butun so'rov "kutilmagan xatolik" bilan buzilib qolmaydi, foydalanuvchi
    Telegram'da aniq nima yetishmayotganini ko'radi."""
    node = action
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise meta_api.MetaAPIError({
                "message": (
                    f"Targetolog action'ida kerakli maydon topilmadi: "
                    f"{'.'.join(path)}. Model action_schema'ga to'liq amal "
                    "qilmagan bo'lishi mumkin. Qaytadan urinib ko'ring yoki "
                    "buyruqni boshqacharoq/aniqroq yozing."
                ),
                "missing_field": ".".join(path),
                "action_received": action,
            })
        node = node[key]
    return node


def _require_any(action: dict, *paths: tuple) -> object:
    """`_require()`ga o'xshaydi, lekin bir nechta mumkin bo'lgan joylashuvni
    sinab ko'radi va birinchi topilganini qaytaradi. MUHIM: Targetolog ba'zan
    `params.audience_change.targeting` o'rniga to'g'ridan-to'g'ri
    `params.targeting` deb yozib qo'yadi (schema'ga qat'iy amal qilmaydi) —
    bu real, tez-tez uchraydigan holat, shuning uchun kodni PROMPT'ga emas,
    shu moslashuvchanlikka tayanamiz."""
    for path in paths:
        node = action
        found = True
        for key in path:
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                found = False
                break
        if found:
            return node
    # MUHIM (2026-08, foydalanuvchi Telegram loglarida takror ko'rgan xato):
    # oldin bu xabar faqat "qaysi joylashuvlar sinab ko'rildi"ni ko'rsatardi,
    # lekin model HAQIQATDA nima yuborganini (masalan `params` ichida qanday
    # kalitlar bor edi) ko'rsatmasdi -- shuning uchun har safar server logini
    # ochib `action_received`ni qo'lda o'qish kerak edi. Endi xabarning o'zida
    # haqiqiy `params` kalitlari ham ko'rsatiladi -- shu orqali muammo
    # server logisiz ham darhol tushunarli bo'ladi.
    params_obj = action.get("params")
    received_keys = sorted(params_obj.keys()) if isinstance(params_obj, dict) else []
    received_keys_display = ", ".join(received_keys) if received_keys else "(bo'sh)"
    raise meta_api.MetaAPIError({
        "message": (
            "Targetolog action'ida kerakli maydon topilmadi (sinab ko'rilgan "
            f"joylashuvlar: {[' > '.join(p) for p in paths]}). Model 'params' "
            "ichiga boshqa nom/joylashuv bilan yozgan bo'lishi mumkin -- "
            f"haqiqatda kelgan 'params' kalitlari: {received_keys_display}"
            ". Qaytadan urinib ko'ring yoki buyruqni boshqacharoq/aniqroq yozing."
        ),
        "tried_paths": [".".join(p) for p in paths],
        "received_params_keys": received_keys,
        "action_received": action,
    })


def _execute_fix_region(action: dict) -> dict:
    """4.11-bo'lim: 'faqat joriy shahar' sozlamasini qo'llaydi va qayta o'qib
    tasdiqlaydi."""
    adset_id = _require(action, "object_id")
    city_key = _require_any(
        action,
        ("params", "audience_change", "city_key"),
        ("params", "city_key"),
    )
    meta_api.set_location_current_city_only(adset_id, city_key)
    verified = meta_api.get_adset_details(adset_id)
    return {"verified": True, "current_targeting": verified.get("targeting", {})}


# `targeting` obyektida odatda uchraydigan kalitlar -- model uni
# `params.audience_change.targeting`/`params.targeting` ichiga emas,
# TO'G'RIDAN-TO'G'RI `params`ning o'ziga yozib qo'ysa ham (Telegram
# loglarida bir necha marta takrorlangan xato) shu orqali aniqlaymiz.
_TARGETING_LIKE_KEYS = {
    "geo_locations", "excluded_geo_locations", "age_min", "age_max",
    "genders", "interests", "flexible_spec", "locales",
    "publisher_platforms", "device_platforms", "custom_audiences",
    "excluded_custom_audiences",
}


def _execute_adjust_audience(action: dict) -> dict:
    """`adjust_audience` (masalan hudud exclude qilish): targeting'ni yangilaydi,
    KEYIN adset'ni qayta o'qib, so'ralgan o'zgarish (masalan excluded_geo_locations)
    haqiqatan saqlanganini tasdiqlaydi. Tasdiqlanmasa — bajarilgan deb ko'rsatilmaydi,
    xato sifatida qaytariladi (foydalanuvchi buni Telegram'da ❌ bilan ko'radi).

    MUHIM (2026-08, Telegram loglarida ko'p marta takrorlangan xato --
    "kerakli maydon topilmadi: params > audience_change > targeting / params
    > targeting"): model targeting maydonlarini ba'zan ikkalasiga ham
    joylamasdan, TO'G'RIDAN-TO'G'RI `params`ning o'ziga yozib qo'yadi
    (masalan `{"params": {"excluded_geo_locations": [...]}}`). Shuning uchun
    ikkita "rasmiy" joylashuvdan tashqari, UCHINCHI, kengroq fallback ham
    qo'shildi: agar `params`ning o'zida targeting'ga xos kalit(lar) bo'lsa,
    o'sha `params`ning o'zini targeting sifatida ishlatamiz."""
    adset_id = _require(action, "object_id")
    params = action.get("params") if isinstance(action.get("params"), dict) else {}
    audience_change = params.get("audience_change") if isinstance(params.get("audience_change"), dict) else {}

    if isinstance(audience_change.get("targeting"), dict):
        new_targeting = audience_change["targeting"]
    elif isinstance(params.get("targeting"), dict):
        new_targeting = params["targeting"]
    elif _TARGETING_LIKE_KEYS & set(params.keys()):
        new_targeting = {k: v for k, v in params.items() if k != "audience_change"}
    else:
        # Hech biri topilmadi -- aniq, batafsil (received_params_keys bilan)
        # xato uchun _require_any'ning o'ziga qaytamiz.
        new_targeting = _require_any(
            action,
            ("params", "audience_change", "targeting"),
            ("params", "targeting"),
        )
    meta_api.update_targeting(adset_id, new_targeting)

    verified = meta_api.get_adset_details(adset_id)
    actual_targeting = verified.get("targeting", {})

    expected_excluded = new_targeting.get("excluded_geo_locations")
    if expected_excluded:
        actual_excluded = actual_targeting.get("excluded_geo_locations")
        if not actual_excluded:
            raise meta_api.MetaAPIError({
                "message": (
                    "Meta so'rovni qabul qildi, lekin qayta tekshirganda "
                    "excluded_geo_locations bo'sh chiqdi — o'zgarish amalda "
                    "saqlanmagan. Ads Manager'da qo'lda tekshiring."
                ),
                "expected_excluded_geo_locations": expected_excluded,
                "actual_targeting": actual_targeting,
            })
    return {"verified": True, "current_targeting": actual_targeting}


def _execute_adjust_budget(action: dict, direction: str) -> dict:
    """`increase_budget`/`decrease_budget` uchun.

    MUHIM (2026-08, foydalanuvchi Telegram loglarida ko'rgan xato --
    `KeyError: 'current_daily_budget_cents'`): oldin bu joriy kunlik
    byudjetni TO'G'RIDAN-TO'G'RI Targetolog'ning JSON javobidan olardi --
    agar model shu maydonni schema'ga to'liq amal qilmasdan qoldirib
    ketsa (yoki eski/noto'g'ri qiymat yozsa), yo xom KeyError chiqardi,
    yo noto'g'ri bazadan foiz hisoblanardi. Endi HAQIQIY joriy byudjet
    modelning taxminiga ishonmasdan, to'g'ridan-to'g'ri Meta'dan
    (`get_adset_details`) o'qib olinadi -- keyin natija qayta o'qib
    tasdiqlanadi (boshqa executor'lardagi kabi verify-after-write)."""
    adset_id = _require(action, "object_id")
    percent = abs(float(_require(action, "params", "percent")))
    if direction == "decrease":
        percent = -percent

    current = meta_api.get_adset_details(adset_id)
    current_budget = current.get("daily_budget")
    if not current_budget:
        raise meta_api.MetaAPIError({
            "message": (
                "Bu reklama guruhining o'zida kunlik byudjet topilmadi -- "
                "ehtimol byudjet KAMPANIYA darajasida sozlangan (Campaign "
                "Budget Optimization / CBO). Hozircha faqat reklama guruhi "
                "darajasidagi byudjetni o'zgartira olaman -- Ads Manager'da "
                "qo'lda tekshiring."
            ),
            "adset_id": adset_id,
        })
    current_budget = int(current_budget)

    meta_api.adjust_budget_by_percent(adset_id, current_budget, percent)

    verified = meta_api.get_adset_details(adset_id)
    new_budget = verified.get("daily_budget")
    expected_budget = int(current_budget * (1 + percent / 100))
    # Meta ba'zan kichik yaxlitlash farqi bilan qaytarishi mumkin -- shuning
    # uchun kutilgan qiymatdan 5% (yoki kamida 1 tiyin) ichidagi farqni ham
    # "tasdiqlangan" deb hisoblaymiz.
    tolerance = max(1, abs(expected_budget) * 0.05)
    if not new_budget or abs(int(new_budget) - expected_budget) > tolerance:
        raise meta_api.MetaAPIError({
            "message": (
                f"Meta so'rovni qabul qildi, lekin qayta tekshirganda byudjet "
                f"kutilganidek o'zgarmagan (eski: {current_budget}, kutilgan: "
                f"~{expected_budget}, hozirgi: {new_budget}). Ads Manager'da "
                "qo'lda tekshiring."
            ),
            "old_budget_cents": current_budget, "expected_budget_cents": expected_budget,
            "actual_budget_cents": new_budget,
        })
    return {"verified": True, "old_budget_cents": current_budget, "new_budget_cents": int(new_budget)}


def _execute_launch_campaign(action: dict) -> dict:
    """8-band (targetolog prompt): to'liq yangi campaign -> adset -> (ad) yaratadi."""
    params = action["params"]
    campaign = meta_api.create_campaign(**params["campaign"])
    campaign_id = campaign["id"]

    adset_params = dict(params["adset"])
    adset_params["campaign_id"] = campaign_id
    adset = meta_api.create_adset(**adset_params)

    result = {"campaign": campaign, "adset": adset}

    ad_spec = params.get("ad")
    if ad_spec and ad_spec.get("creative_id"):
        ad = meta_api.create_ad(
            adset_id=adset["id"],
            name=ad_spec.get("name", action.get("object_name", "Target Master ad")),
            creative_id=ad_spec["creative_id"],
            status=ad_spec.get("status", "PAUSED"),
        )
        result["ad"] = ad
    else:
        result["note"] = "creative_id berilmagan — reklama hali yaratilmadi, foydalanuvchi creative_id yuborishi kerak."
    return result


def _execute_ab_test(action: dict) -> dict:
    """9-band (targetolog prompt): mavjud adset'ni nusxalab, B variantni yaratadi,
    faqat bitta o'zgaruvchini (auditoriya YOKI kreativ) farqlantiradi."""
    params = action["params"]
    copy_result = meta_api.copy_adset(
        action["object_id"], rename_suffix=params.get("rename_suffix", " - B variant")
    )
    b_adset_id = copy_result.get("adset_id") or copy_result.get("id")

    variant_b = params.get("variant_b", {})
    if variant_b.get("targeting"):
        meta_api.update_targeting(b_adset_id, variant_b["targeting"])
    if variant_b.get("creative_id"):
        meta_api.create_ad(
            adset_id=b_adset_id,
            name=f"{action.get('object_name', 'Test')} - B",
            creative_id=variant_b["creative_id"],
            status="ACTIVE",
        )

    meta_api.activate_object(action["object_id"])
    meta_api.activate_object(b_adset_id)
    return {
        "variant_a_adset_id": action["object_id"],
        "variant_b_adset_id": b_adset_id,
        "test_duration_days": params.get("test_duration_days", 7),
        "decision_metric": params.get("decision_metric", "CPA"),
    }


def _execute_replace_creative(action: dict) -> dict:
    """4-band (targetolog prompt), 2026-08 matn-avtonom variant: mavjud
    rasm/video'ni SAQLAB, faqat reklama matnini (`final_primary_text`/
    `final_headline`) yangilab, yangi creative'ni reklamaga biriktiradi va
    qayta o'qib tasdiqlaydi. AI hali rasm/video generatsiya qila olmaydi --
    shuning uchun Targetolog butunlay YANGI vizual kerak deb hisoblasa, bu
    action turini UMUMAN chiqarmasligi, o'rniga `summary`da odam dizaynerga
    TZ yozishi kerak (bu funksiya faqat matn-almashtirish yo'lini bajaradi)."""
    ad_id = _require(action, "object_id")
    final_primary_text = _require_any(
        action,
        ("params", "creative_brief", "final_primary_text"),
        ("params", "final_primary_text"),
    )
    params = action.get("params") or {}
    brief = params.get("creative_brief") or {}
    final_headline = brief.get("final_headline") or params.get("final_headline")

    current = meta_api.get_ad_creative_details(ad_id)
    if not current.get("object_story_spec"):
        raise meta_api.MetaAPIError({
            "message": (
                "Reklamaning joriy kreativida object_story_spec topilmadi -- "
                "matnni avtomatik almashtirib bo'lmaydi. Ads Manager'da qo'lda "
                "tekshiring yoki yangi vizual bilan qo'lda yarating."
            ),
            "ad_id": ad_id,
        })

    new_creative = meta_api.create_ad_creative_with_new_copy(
        page_id=meta_api.PAGE_ID,
        base_story_spec=current["object_story_spec"],
        primary_text=final_primary_text,
        headline=final_headline,
        name=action.get("object_name"),
    )
    new_creative_id = new_creative.get("id")
    if not new_creative_id:
        raise meta_api.MetaAPIError({
            "message": "Yangi kreativ yaratildi, lekin ID qaytmadi -- reklamaga biriktirib bo'lmadi.",
            "response": new_creative,
        })

    meta_api.update_ad_creative(ad_id, new_creative_id)

    verified = meta_api.get_ad_creative_details(ad_id)
    if verified.get("creative_id") != new_creative_id:
        raise meta_api.MetaAPIError({
            "message": (
                "Meta so'rovni qabul qildi, lekin qayta tekshirganda reklama "
                "hali eski kreativni ko'rsatyapti -- o'zgarish saqlanmagan bo'lishi mumkin."
            ),
            "expected_creative_id": new_creative_id,
            "actual_creative_id": verified.get("creative_id"),
        })
    return {"verified": True, "new_creative_id": new_creative_id}


AUTO_EXECUTABLE_TYPES = set(ACTION_EXECUTORS.keys())

# schedule_on_off/schedule_report/cancel_standing_task -- boshqa action'lardan
# farqli, Meta'ga to'g'ridan-to'g'ri murojaat qilmaydi (shuning uchun ACTION_EXECUTORS
# ichida emas) -- faqat bazada "vazifa" yozadi/bekor qiladi va `chat_id` talab qiladi
# (qaysi Telegram guruh so'ragani). `_finish_pipeline` bularni alohida ishlaydi.
SCHEDULING_ACTION_TYPES = {"schedule_on_off", "schedule_report", "cancel_standing_task"}

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _parse_hhmm(value, field_name: str) -> str:
    v = (value or "").strip() if isinstance(value, str) else ""
    if not _HHMM_RE.match(v):
        raise meta_api.MetaAPIError({
            "message": (
                f"{field_name} noto'g'ri yoki bo'sh: {value!r}. 'HH:MM' "
                "(24 soatlik, Toshkent vaqti, masalan '22:00') formatida "
                "bo'lishi kerak."
            ),
        })
    return v


def _execute_schedule_on_off(action: dict, chat_id: int | None) -> dict:
    """`schedule_on_off`: Meta'da HECH NARSA DARHOL o'zgarmaydi -- faqat
    `db.StandingTask` yozuvini yaratadi/yangilaydi. Haqiqiy yoqish/o'chirish
    `scheduler.py`dagi `job_standing_tasks` orqali, har ~5 daqiqada joriy
    Toshkent vaqtiga qarab avtomatik bajariladi -- foydalanuvchi buyruqni
    faqat BIR MARTA beradi, keyin agentning o'zi kuzatib turadi."""
    if chat_id is None:
        raise meta_api.MetaAPIError({
            "message": "Doimiy vazifa yaratish uchun chat aniqlanmadi -- buyruqni Telegram guruh/chatidan qayta yuboring.",
        })
    object_id = _require(action, "object_id")
    params = action.get("params") or {}
    on_time = _parse_hhmm(params.get("on_time"), "on_time")
    off_time = _parse_hhmm(params.get("off_time"), "off_time")

    session = db.get_session()
    try:
        existing = session.query(db.StandingTask).filter_by(
            chat_id=str(chat_id), object_id=object_id, is_active=True
        ).first()
        if existing:
            existing.on_time = on_time
            existing.off_time = off_time
            existing.object_name = action.get("object_name") or existing.object_name
            existing.created_by_text = action.get("reason", "")
            existing.last_desired_state = None  # keyingi tekshiruvda darhol qayta baholansin
            task_id = existing.id
        else:
            task = db.StandingTask(
                chat_id=str(chat_id), object_id=object_id,
                object_name=action.get("object_name"),
                on_time=on_time, off_time=off_time,
                created_by_text=action.get("reason", ""),
            )
            session.add(task)
            session.flush()
            task_id = task.id
        session.commit()
    finally:
        session.close()
    return {"standing_task_id": task_id, "on_time": on_time, "off_time": off_time, "verified": True}


def _execute_schedule_report(action: dict, chat_id: int | None) -> dict:
    """`schedule_report`: qo'shimcha (asosiy 09:00dan tashqari) doimiy hisobot
    vaqtini `db.StandingReport`ga yozadi -- `scheduler.py`dagi
    `job_standing_reports` shu vaqtda har kuni avtomatik hisobot yuboradi."""
    if chat_id is None:
        raise meta_api.MetaAPIError({
            "message": "Doimiy hisobot vazifasini yaratish uchun chat aniqlanmadi -- buyruqni Telegram guruh/chatidan qayta yuboring.",
        })
    params = action.get("params") or {}
    time_hhmm = _parse_hhmm(params.get("time"), "time")

    session = db.get_session()
    try:
        existing = session.query(db.StandingReport).filter_by(
            chat_id=str(chat_id), time_hhmm=time_hhmm, is_active=True
        ).first()
        if existing:
            report_id = existing.id
        else:
            report = db.StandingReport(chat_id=str(chat_id), time_hhmm=time_hhmm, label=params.get("label"))
            session.add(report)
            session.flush()
            report_id = report.id
        session.commit()
    finally:
        session.close()
    return {"standing_report_id": report_id, "time": time_hhmm, "verified": True}


def _execute_cancel_standing_task(action: dict, chat_id: int | None) -> dict:
    """`cancel_standing_task`: berilgan `object_id` uchun barcha faol
    `schedule_on_off` vazifalarini bekor qiladi (nofaol qiladi)."""
    object_id = _require(action, "object_id")
    session = db.get_session()
    try:
        rows = session.query(db.StandingTask).filter_by(object_id=object_id, is_active=True).all()
        for r in rows:
            r.is_active = False
        session.commit()
        count = len(rows)
    finally:
        session.close()
    if count == 0:
        raise meta_api.MetaAPIError({"message": "Bu target uchun faol doimiy (avtomatik yoqish/o'chirish) vazifa topilmadi."})
    return {"cancelled": count, "verified": True}


# ---------------------------------------------------------------------------
# YENGIL (OpenAI) YO'L -- oddiy, bitta qadamli ACTION buyruqlari uchun
# ---------------------------------------------------------------------------
# MUHIM (foydalanuvchi so'ragan xarajat optimallashtirish): avval HAR BIR
# ACTION turi (hatto oddiy "X'ni to'xtat"/"X'ni yoq" kabi hech qanday
# mulohaza talab qilmaydigan buyruqlar ham) Targetolog+Marketolog (Claude,
# ikkalasi ham) orqali o'tar edi. Bu haqiqiy MUHOKAMA/tashxis talab qiladigan
# buyruqlar uchun (byudjet/auditoriya/kreativ qarorlari, yangi kampaniya)
# to'g'ri, lekin oddiy to'g'ridan-to'g'ri buyruq uchun ortiqcha xarajat edi.
# Endi bunday oddiy buyruqlar OLDIN shu YENGIL (faqat OpenAI, target nomini
# hisob strukturasiga solishtirish esa oddiy Python orqali) yo'l bilan
# sinab ko'riladi -- muvaffaqiyatli bo'lsa Claude UMUMAN chaqirilmaydi.
# Har qanday noaniqlik (COMPLEX deb topilsa, target topilmasa/bir nechta
# nomzod bo'lsa, vaqt formati xato bo'lsa, OpenAI o'zi xato bersa) -- `None`
# qaytariladi va chaqiruvchi (`execute_intent`) avvalgidek to'liq Claude
# asosidagi `_run_pipeline_command`ga tushadi, ya'ni hech narsa "taxmin
# qilib" bajarilmaydi -- faqat aniq holatlarda xarajat tejaladi.
_SIMPLE_ACTION_PROMPT = (
    "Foydalanuvchi Telegram orqali AMALIY buyruq berdi (allaqachon ACTION turi "
    "deb aniqlangan). Vazifangiz: bu ODDIY, TO'G'RIDAN-TO'G'RI, hech qanday "
    "tahlil/mulohaza talab qilmaydigan buyruqmi, yoki chuqurroq tashxis/qaror "
    "(auditoriya/byudjet/kreativ tahlili, murakkab yangi kampaniya) talab "
    "qiladimi -- shuni aniqlang.\n\n"
    "Faqat JSON qaytaring:\n"
    '{"verdict": "PAUSE|RESUME|SCHEDULE_ON_OFF|SCHEDULE_REPORT|CANCEL_SCHEDULE|COMPLEX", '
    '"target_name": "<foydalanuvchi aytgan kampaniya/adset/ad nomi, xabardan AYNAN '
    'olingan, aks holda null>", "on_time": "HH:MM yoki null", "off_time": "HH:MM '
    'yoki null", "report_time": "HH:MM yoki null"}\n\n'
    "Qoidalar:\n"
    "- PAUSE -- foydalanuvchi ANIQ bitta target nomini aytib, uni TO'G'RIDAN-TO'G'RI "
    "to'xtatish/o'chirishni buyurgan (masalan \"X'ni to'xtat\", \"X'ni o'chir\"), "
    "SABAB sifatida ishlash sifati/narx haqida shikoyat qilMAGAN holatda.\n"
    "- RESUME -- xuddi shunday, lekin qayta ishga tushirish/yoqish uchun.\n"
    "- SCHEDULE_ON_OFF -- \"har kuni soat X dan Y gacha yoqib/o'chirib tur\" kabi "
    "DOIMIY vaqt jadvali so'ralganda -- on_time/off_time'ni ANIQ 'HH:MM' (24 "
    "soatlik) formatda chiqaring (agar aniq soat aytilmagan bo'lsa -- COMPLEX).\n"
    "- SCHEDULE_REPORT -- \"har kuni soat X da (qo'shimcha) hisobot ber\" kabi -- "
    "report_time'ni 'HH:MM' formatda chiqaring, target_name kerak emas (null).\n"
    "- CANCEL_SCHEDULE -- \"X uchun avtomatik yoqib-o'chirishni bekor qil\" kabi.\n"
    "- COMPLEX -- QOLGAN HAMMA HOLAT: yangi kampaniya/target yaratish, byudjet "
    "o'zgartirish, auditoriya/hudud o'zgartirish, kreativ muammosi, A/B test, "
    "yoki buyruqda ISHLASH SIFATI/NATIJA haqida SABAB/SHIKOYAT bo'lsa (masalan "
    "\"kam lead beryapti\", \"narxi qimmat chiqdi\", \"yaxshi ishlamayapti\", "
    "\"tuzat\") -- bunday holatda oddiy pause/resume EMAS, mutaxassis tahlili "
    "kerak (avval boshqa yechim ko'rib chiqilishi kerak bo'lishi mumkin), hatto "
    "foydalanuvchi \"to'xtat\" so'zini ishlatgan bo'lsa ham.\n"
    "- Agar target nomi/vaqt aniq bo'lmasa yoki bir nechta target'ga tegishli "
    "bo'lishi mumkin bo'lsa -- COMPLEX qaytaring (mutaxassisga yuboriladi, u "
    "aniqlashtiruvchi savol beradi).\n"
    "Faqat JSON qaytaring, boshqa matn yo'q."
)


def _find_target_by_name(structure: dict, name_query: str | None) -> dict | None:
    """`name_query`ni hisob strukturasidagi campaign/adset/ad nomlariga
    solishtiradi (LLM'siz, oddiy Python). Avval ANIQ (case-insensitive) mos
    kelish tekshiriladi; topilmasa qisman (ikki tomonlama containment) mos
    kelish. BIRDAN ORTIQ yoki NOLTA nomzod topilsa -- `None` qaytaradi
    (chaqiruvchi Claude yo'liga tushadi, noaniq holatda hech narsa taxmin
    qilib bajarilmasligi uchun)."""
    if not name_query or not name_query.strip():
        return None
    q = name_query.strip().lower()
    all_objects = []
    for level_key, level in (("ads", "ad"), ("adsets", "adset"), ("campaigns", "campaign")):
        for o in structure.get(level_key, []) or []:
            name = o.get("name") or ""
            if name:
                all_objects.append({"id": o.get("id"), "name": name, "level": level})

    exact = [o for o in all_objects if o["name"].strip().lower() == q]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None

    partial = [o for o in all_objects if q in o["name"].lower() or o["name"].lower() in q]
    if len(partial) == 1:
        return partial[0]
    return None


def _execute_simple_action(user_text: str, chat_id: int | None) -> str | None:
    """ACTION verdikti uchun ARZON (faqat OpenAI) yo'l -- yuqoridagi izohga
    qarang. `None` qaytarilsa, chaqiruvchi to'liq Claude pipeline'ga tushadi."""
    try:
        raw = call_light(_SIMPLE_ACTION_PROMPT, user_text, max_tokens=200).strip()
    except Exception:
        return None  # OPENAI_API_KEY yo'q/xato -- Claude yo'liga (funksional zaxira)
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None

    verdict = str(parsed.get("verdict", "")).strip().upper()
    if verdict not in ("PAUSE", "RESUME", "SCHEDULE_ON_OFF", "SCHEDULE_REPORT", "CANCEL_SCHEDULE"):
        return None  # COMPLEX yoki noma'lum javob -- Claude (Targetolog) yo'liga

    if verdict == "SCHEDULE_REPORT":
        try:
            time_hhmm = _parse_hhmm(parsed.get("report_time"), "report_time")
        except meta_api.MetaAPIError:
            return None
        try:
            _execute_schedule_report({"params": {"time": time_hhmm}}, chat_id)
        except meta_api.MetaAPIError as e:
            return f"⚠️ {e}"
        logger.info("Yengil yo'l: schedule_report time=%s chat_id=%s", time_hhmm, chat_id)
        return f"✅ Qabul qildim — har kuni soat {time_hhmm} da qo'shimcha hisobot yuboraman."

    # Qolgan to'rttasi (PAUSE/RESUME/SCHEDULE_ON_OFF/CANCEL_SCHEDULE) -- barchasi
    # bitta aniq target'ga bog'liq, shuning uchun avval nomini hisob
    # strukturasiga solishtirib topamiz (LLM ishlatmasdan).
    try:
        structure = meta_api.get_account_structure()
    except meta_api.MetaAPIError:
        return None
    target = _find_target_by_name(structure, parsed.get("target_name"))
    if target is None:
        return None  # topilmadi yoki noaniq -- Targetolog aniqroq so'rasin

    if verdict == "PAUSE":
        try:
            _execute_and_verify_status(target["id"], "PAUSED")
        except meta_api.MetaAPIError as e:
            return f"⚠️ {target['name']}: {e}"
        logger.info("Yengil yo'l: pause object_id=%s chat_id=%s", target["id"], chat_id)
        return f"✅ {target['name']}: to'xtatdim."

    if verdict == "RESUME":
        try:
            _execute_and_verify_status(target["id"], "ACTIVE")
        except meta_api.MetaAPIError as e:
            return f"⚠️ {target['name']}: {e}"
        logger.info("Yengil yo'l: resume object_id=%s chat_id=%s", target["id"], chat_id)
        return f"✅ {target['name']}: qayta ishga tushirdim."

    if verdict == "SCHEDULE_ON_OFF":
        try:
            on_time = _parse_hhmm(parsed.get("on_time"), "on_time")
            off_time = _parse_hhmm(parsed.get("off_time"), "off_time")
        except meta_api.MetaAPIError:
            return None
        try:
            _execute_schedule_on_off(
                {"object_id": target["id"], "object_name": target["name"],
                 "params": {"on_time": on_time, "off_time": off_time}},
                chat_id,
            )
        except meta_api.MetaAPIError as e:
            return f"⚠️ {e}"
        logger.info("Yengil yo'l: schedule_on_off object_id=%s chat_id=%s", target["id"], chat_id)
        return (
            f"✅ {target['name']}: har kuni {on_time} da yoqiladi, {off_time} da "
            "o'chadi — endi o'zim kuzatib boraman."
        )

    if verdict == "CANCEL_SCHEDULE":
        try:
            _execute_cancel_standing_task({"object_id": target["id"]}, chat_id)
        except meta_api.MetaAPIError as e:
            return f"⚠️ {target['name']}: {e}"
        logger.info("Yengil yo'l: cancel_standing_task object_id=%s chat_id=%s", target["id"], chat_id)
        return f"✅ {target['name']}: avtomatik yoqish/o'chirish jadvali bekor qilindi."

    return None


# "Rejalashtirilgan/pauzadagi/hali yoqilmagan" target so'rovlarini aniqlash
# uchun -- bunday savolda PAUSED holatidagi kampaniya/adset/ad'lar
# (`meta_api.get_account_structure`dan, HAQIQIY status maydoni bo'yicha,
# LLM'siz -- oddiy filtrlash orqali) ro'yxati javobga qo'shib beriladi.
_PLANNED_KEYWORDS = re.compile(
    r"rejalashtirilgan|pauzada|to'xtatilgan|hali yoqilmagan|tayyor turgan|"
    r"tayyor holatda",
    re.IGNORECASE,
)

# MUHIM (bug fix): "27 iyul" kabi ANIQ sana so'ralganda, avvalgi versiya bu
# sanani (since/until) OpenAI (call_light) orqali JSON qilib chiqartirardi --
# lekin bu NOANIQ chiqib qoldi: foydalanuvchi "27 iyul" so'raganda bot
# "Xarajat: $28.56" deb ko'rsatdi, aslida Meta Ads Manager'da o'sha kunlik
# HAQIQIY xarajat atigi $14.75 edi (deyarli ANIQ 2 barobar ko'p -- demak
# LLM since/until'ni BIR KUN o'rniga IKKI KUNLIK oraliq qilib noto'g'ri
# chiqargan bo'lishi kerak). Bu ANIQ sana/oy nomi bilan bog'liq sanalarni endi
# butunlay DETERMINISTIK (regex + Python sana arifmetikasi) orqali hisoblaymiz
# -- LLM umuman chaqirilmaydi, shuning uchun bunday xato butunlay yo'qoladi.
# LLM (call_light) faqat matn hech qanday aniq sanaga TO'G'RI KELMAGANDA
# (masalan "oxirgi hafta", "shu haftada" kabi kamdan-kam iboralar uchun)
# zaxira sifatida ishlatiladi.
_QP_MONTH_NAMES = "|".join(monthly_report._UZ_MONTHS.keys())
# MUHIM: \w* bilan -- "bugun" so'zi "bugungi", "bugundan" kabi qo'shimchali
# shaklda ham kelishi mumkin, oddiy \bbugun\b bunday hollarda mos kelmay
# qolib, keraksiz LLM chaqiruviga (va potentsial xatoga) olib kelardi.
_QP_TODAY_PATTERN = re.compile(r"\bbugun\w*\b|\bhozir\w*\b", re.IGNORECASE)
_QP_YESTERDAY_PATTERN = re.compile(r"\bkecha\w*\b", re.IGNORECASE)
# "1-10 avgust" / "1 - 10 avgust" kabi BIR OY ICHIDAGI kun oralig'i
_QP_DAY_RANGE_PATTERN = re.compile(
    r"\b(\d{1,2})\s*[-–]\s*(\d{1,2})\s*(" + _QP_MONTH_NAMES + r")\b",
    re.IGNORECASE,
)


def _qp_find_day_near_month(text_lower: str, month_name: str) -> int | None:
    """`month_name` yoniga yopishgan 1-2 xonali kun raqamini topadi -- raqam
    oy nomidan OLDIN yoki KEYIN, oralig'ida faqat bo'shliq/chiziqcha bilan
    kelishi mumkin (masalan "27 iyul", "iyul 27", "27-iyul")."""
    m = re.search(r"\b(\d{1,2})\b[\s\-]*" + re.escape(month_name), text_lower)
    if m:
        return int(m.group(1))
    m = re.search(re.escape(month_name) + r"[\s\-]*\b(\d{1,2})\b", text_lower)
    if m:
        return int(m.group(1))
    return None


def _qp_parse_explicit_date(user_text: str, today) -> "date | None":
    """Xabarda ANIQ BITTA sana (oy nomi + kun raqami) bormi -- bo'lsa shu
    sanani (yil ko'rsatilmagan bo'lsa joriy yildan, kelajakka chiqib
    qolmasligi uchun kerak bo'lsa o'tgan yildan) qaytaradi, aks holda None."""
    from datetime import date as _date
    text_lower = (user_text or "").lower()
    for month_name, month_num in monthly_report._UZ_MONTHS.items():
        if month_name not in text_lower:
            continue
        day = _qp_find_day_near_month(text_lower, month_name)
        if day is None or not (1 <= day <= 31):
            continue
        # MUHIM: OY nomiga qarab yil tanlanadi (kun raqamiga qarab emas) --
        # `_qp_parse_day_range`dagi bir xil bug-fix bilan bir xil mantiq,
        # aks holda joriy oy ichidagi (bugundan bir necha kun keyingi)
        # sana so'ralganda butun yil xato ravishda o'tgan yilga surilib
        # ketardi.
        year = today.year
        if month_num > today.month:
            year -= 1
        try:
            candidate = _date(year, month_num, day)
        except ValueError:
            continue
        return candidate
    return None


def _qp_parse_day_range(user_text: str, today) -> "tuple[date, date] | None":
    """Xabarda "1-10 avgust" kabi BIR OY ICHIDAGI kun oralig'i bormi --
    bo'lsa (since, until) juftligini qaytaradi, aks holda None."""
    from datetime import date as _date
    text_lower = (user_text or "").lower()
    m = _QP_DAY_RANGE_PATTERN.search(text_lower)
    if not m:
        return None
    day1, day2, month_name = int(m.group(1)), int(m.group(2)), m.group(3)
    month_num = monthly_report._UZ_MONTHS.get(month_name)
    if not month_num:
        return None
    if day1 > day2:
        day1, day2 = day2, day1
    # MUHIM (bug fix): bu yerda ANIQ sana emas, OY nomiga qarab yil
    # tanlanadi (monthly_report.resolve_monthly_period bilan bir xil
    # mantiq) -- aks holda masalan bugun 01.08.2026 bo'lganda "1-10
    # avgust" so'ralsa, "10-avgust > bugun" tekshiruvi false-positive berib,
    # butun oraliqni xato ravishda O'TGAN YILGA (2025) surib yuborardi,
    # holbuki foydalanuvchi aniq JORIY oyni so'rayotgan edi.
    year = today.year
    if month_num > today.month:
        year -= 1
    try:
        since = _date(year, month_num, day1)
        until = _date(year, month_num, day2)
    except ValueError:
        return None
    return since, until


class TargetologFormatError(Exception):
    """Model kutilgan JSON o'rniga erkin matn qaytarganda ko'tariladi (masalan,
    unga kerakli ma'lumot — kampaniya/adset ID — yetishmasa)."""
    def __init__(self, raw_text: str):
        self.raw_text = raw_text
        super().__init__("Model JSON formatda javob bermadi")


def _call_agent(system_prompt: str, user_content: str) -> dict:
    # MUHIM (token/xarajatni kamaytirish uchun): TARGETOLOG_SYSTEM/MARKETOLOG_SYSTEM
    # bilim bazasi bilan birga juda katta (~3-4 ming token) va HAR BIR chaqiruvda
    # bir xil — shuning uchun uni "cache_control" bilan belgilaymiz. Anthropic API
    # shu bloqni keshlab, keyingi 5 daqiqa ichidagi chaqiruvlarda uni ~10% narxda
    # qayta ishlatadi (to'liq narx emas). Bitta foydalanuvchi buyrug'i uchun bir
    # nechta chaqiruv (masalan geo-lookup ikkinchi bosqichi) bo'lsa ham, faqat
    # birinchisi to'liq narxda hisoblanadi.
    try:
        response = client.messages.create(
            model=MODEL,
            # MUHIM: 2500 token bilan ba'zan (ayniqsa ikki bosqichli aniqlashtirish
            # so'rovida, xabar kattaroq bo'lganda) javob o'rtada kesilib qolib,
            # JSON buzilib, "Targetolog JSON qaytarmadi" xatosiga olib kelardi.
            # 4000ga oshirildi — bu MAX chegara, real xarajat qancha token
            # ishlatilganiga bog'liq (kesilib ketmasa, ko'pincha ancha kamroq
            # ishlatiladi), shuning uchun xarajatni sezilarli oshirmaydi, lekin
            # muvaffaqiyatsiz/qayta urinishlarni oldini oladi.
            max_tokens=4000,
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_content}],
        )
        text = response.content[0].text
    except anthropic.APIError as e:
        # Anthropic balansi tugagan/rate-limit/xizmat vaqtincha ishlamayapti
        # va h.k. -- botni butunlay to'xtatib qo'ymaslik uchun OpenAI'ga
        # (kuchliroq OPENAI_FALLBACK_MODEL bilan) qayta so'raymiz.
        logger.warning("Anthropic ishlamadi (%s: %s), OpenAI zaxira yo'liga o'tyapmiz", type(e).__name__, e)
        text = _call_agent_openai_fallback(system_prompt, user_content)
    # Model ba'zan JSON'ni ```json ... ``` bloki ichida qaytarishi mumkin
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise TargetologFormatError(text) from e


def _call_agent_openai_fallback(system_prompt: str, user_content: str) -> str:
    """`_call_agent()` uchun ZAXIRA yo'l -- Anthropic ishlamay qolganda
    ishga tushadi. Xuddi shu tizim prompti (bilim bazasi bilan) va
    foydalanuvchi xabarini OpenAI'ga yuboradi. Agar OPENAI_API_KEY
    sozlanmagan bo'lsa yoki OpenAI so'rovi ham xato bersa -- xom texnik
    xato o'rniga `AgentUnavailableError` (muloyim, odam o'qiydigan xabar
    bilan) ko'taradi."""
    openai_key = os.environ.get("OPENAI_API_KEY")
    if not openai_key:
        raise AgentUnavailableError(_ANTHROPIC_FALLBACK_MESSAGE)
    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {openai_key}"},
            json={
                "model": OPENAI_FALLBACK_MODEL,
                "temperature": 0,
                "max_tokens": 4000,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            },
            timeout=55,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.warning("OpenAI zaxira yo'li ham ishlamadi: %s", e)
        raise AgentUnavailableError(_ANTHROPIC_FALLBACK_MESSAGE) from e


SNAPSHOT_KV_KEY = "orchestrator_daily_snapshot"


def _crm_leads_count_today() -> int:
    """CRM bazamizdagi (Meta emas) HAQIQIY, bugun (Toshkent vaqti) tushgan
    lead yozuvlari soni. 2026-08, foydalanuvchi topgan xato: Telegram audit
    xabari "bugun 29 ta mijoz keldi" deb yozgan, aslida CRM'da bugun bor-yo'g'i
    4 ta lead bor edi -- sabab: Targetologga hech qachon haqiqiy CRM lead
    sonini bermasdik, u Meta'ning campaign-darajasidagi "natija" sonini
    (bu xabar/qo'ng'iroq boshlash kabi tugallanmagan harakatlarni ham
    o'z ichiga olishi mumkin) "mijoz" deb noto'g'ri talqin qilgan bo'lishi
    mumkin edi. Endi aniq, tekshirilgan CRM soni beriladi."""
    date_bounds = dashboard_data._date_preset_bounds_utc("today")
    if not date_bounds:
        return 0
    start_utc, end_utc = date_bounds
    session = db.get_session()
    try:
        return session.query(db.Lead).filter(
            db.Lead.created_at >= start_utc, db.Lead.created_at < end_utc
        ).count()
    finally:
        session.close()


def gather_data() -> dict:
    """Meta API'dan tahlil uchun kerakli barcha ma'lumotni yig'adi.

    Shuningdek, KECHAGI (oldingi chaqiruvdagi) kampaniya darajasidagi
    statistikani ham qo'shib beradi ("previous_snapshot") — shu orqali
    Targetolog "kecha CPA $9 edi, undan oldin $14 edi" kabi HAQIQIY
    solishtirishga asoslanib xulosa chiqara oladi, taxmin qilmaydi. Joriy
    holat esa ertangi solishtirish uchun KV'ga saqlanadi.

    MUHIM (2026-08, foydalanuvchi topgan xato -- "bugun 54$ sarflandi, 29 ta
    mijoz keldi" deb hisobot berilgan, aslida bugun hali 4 ta lead bor edi):
    oldin bu funksiya HECH QACHON haqiqiy "bugun" (date_preset="today")
    ma'lumotini bermasdi -- faqat `last_7d` va `yesterday` bor edi (o'zgaruvchi
    nomi "campaign_insights_today" bo'lsa ham, aslida "yesterday" so'rovi edi!).
    Natijada Targetolog "bugun" so'zini ishlatib, aslida 7-kunlik yoki kechagi
    jamlangan raqamni tasvirlab yozardi. Endi haqiqiy "bugun" Meta ma'lumoti
    VA haqiqiy CRM lead soni alohida, aniq nomlangan blokda beriladi -- va
    pastdagi `comparison_instruction` so'zi bilan model "bugun" so'zini FAQAT
    shu blokka asoslanib ishlatishi kerakligi aniq ta'kidlanadi."""
    account_structure = meta_api.get_account_structure()
    ad_insights = meta_api.get_insights(level="ad", date_preset="last_7d")
    region_breakdown = meta_api.get_insights(
        level="ad", date_preset="last_7d", breakdowns=["region"]
    )
    yesterday_campaign_insights = meta_api.get_insights(level="campaign", date_preset="yesterday")
    today_campaign_insights = meta_api.get_insights(level="campaign", date_preset="today")
    today_crm_leads = _crm_leads_count_today()

    # MUHIM: bu funksiya endi FAQAT kunlik cron'dan emas, tez-tez (masalan
    # har 30-60 daqiqada) ishlaydigan "kuzatuv" cron'idan ham chaqirilishi
    # mumkin. Agar har chaqiruvda snapshot'ni qayta yozsak, "kecha bilan
    # solishtirish" buzilib, "bir necha soat oldin bilan solishtirish"ga
    # aylanib qolardi. Shuning uchun snapshot FAQAT kunda BIR MARTA (sana
    # o'zgarganda) yangilanadi -- shu kunning ichidagi barcha keyingi
    # chaqiruvlar (kuzatuv cron ham, /analyze ham) hammasi bir xil "kecha"
    # ma'lumotini ko'radi.
    previous_snapshot = kv_store.get_json(SNAPSHOT_KV_KEY, default=None)
    today_str = datetime.utcnow().date().isoformat()
    if previous_snapshot is None or previous_snapshot.get("date") != today_str:
        kv_store.set_json(SNAPSHOT_KV_KEY, {
            "date": today_str,
            "campaign_insights": yesterday_campaign_insights,
        })

    return {
        "account_structure": account_structure,
        "ad_insights": ad_insights,
        "region_breakdown": region_breakdown,
        "business_rules": BUSINESS_RULES,
        "generated_at": datetime.utcnow().isoformat(),
        "today_insights": {
            "meta_campaign_data_today": today_campaign_insights,
            "real_crm_lead_count_today": today_crm_leads,
            "note": (
                "MUHIM: 'bugun'/'today' so'zini summary'da ishlatganda FAQAT "
                "shu 'today_insights' blokidagi ma'lumotga asoslaning, boshqa "
                "hech qanday blokka emas. 'real_crm_lead_count_today' -- "
                "bizning CRM bazamizdagi TASDIQLANGAN, haqiqiy lead yozuvlari "
                "soni (bugun, Toshkent vaqti bilan). 'mijoz keldi'/'lid keldi' "
                "deganda ANIQ shu sonni ayting -- Meta'ning campaign-darajasidagi "
                "'natija' (masalan xabar boshlash, qo'ng'iroq urinishi kabi "
                "tugallanmagan harakatlarni ham qo'shib yuborishi mumkin) sonini "
                "'mijoz' deb atamang, ular boshqa-boshqa narsa."
            ),
        },
        "yesterday_campaign_insights": yesterday_campaign_insights,
        "previous_day_snapshot_for_comparison": previous_snapshot,
        "comparison_instruction": (
            "'previous_day_snapshot_for_comparison' — 1-2 kun oldin saqlangan "
            "kunlik holat (agar mavjud bo'lsa). Buni 'yesterday_campaign_insights' "
            "bilan solishtiring va IKKALASINI HAM 'kecha'/'oldingi kun(lar)' deb "
            "ayting (masalan 'kecha CPA $14 edi, undan oldingi kun $9 edi — 55% "
            "oshdi'). DIQQAT: bu ikkalasi ham 'BUGUN' EMAS — 'bugun' so'zini "
            "FAQAT yuqoridagi alohida 'today_insights' blokiga asoslanib "
            "ishlating, hech qachon 'yesterday_campaign_insights' yoki "
            "'previous_day_snapshot_for_comparison'ni 'bugun' deb atamang."
        ),
    }


def _format_json_error(e: "TargetologFormatError", stage: str = "Targetolog") -> str:
    """Xato yuz berganda foydalanuvchiga ODDIY, QISQA matn ko'rsatiladi —
    xom JSON/model javobi hech qachon ko'rsatilmaydi (bu "kod"dek ko'rinib,
    tushunarsiz bo'ladi). To'liq texnik tafsilot faqat server logiga yoziladi
    (debug uchun), Telegram'ga chiqmaydi."""
    logger.error("%s JSON qaytarmadi. Xom javob: %s", stage, e.raw_text[:1000])
    return (
        "⚠️ Buni to'liq bajara olmadim — kerakli ma'lumot yetarli emas edi "
        "(masalan aniq qaysi reklama/kampaniya haqida ekani noaniq bo'ldi) "
        "yoki so'rov juda murakkab bo'ldi.\n\n"
        "Iltimos, aniqroq yozib qayta yuboring (masalan kampaniya nomini "
        "to'liq ko'rsating)."
    )


_EMPTY_STATS = {"succeeded": 0, "failed": 0, "skipped": 0, "manual_suggestions": 0}


def _run_pipeline(targetolog_user_message: str, dry_run: bool = False, chat_id: int | None = None) -> tuple[str, dict]:
    """Targetolog -> Marketolog -> ijro zanjirining umumiy o'zagi. Buni ham
    to'liq hisob tahlili (`run_analysis_cycle`), ham Telegram'dagi erkin
    buyruqlar (`handle_chat_command`) chaqiradi — ikkalasi ham xuddi shu
    ikki bosqichli nazoratdan o'tadi. `(matn, statistika)` qaytaradi —
    statistika kunlik cron hisobotida "diqqatga loyiqmi" degan qarorni
    matnni regex bilan tahlil qilmasdan, to'g'ridan-to'g'ri aniqlash uchun.
    `chat_id` -- faqat `schedule_on_off`/`schedule_report`/`cancel_standing_task`
    action'lari uchun kerak (qaysi Telegram chatdan buyruq kelgani), boshqa
    action turlariga ta'sir qilmaydi."""
    logger.info("Targetolog agentga so'rov yuborilmoqda...")
    try:
        targetolog_plan = _call_agent(TARGETOLOG_SYSTEM, targetolog_user_message)
    except TargetologFormatError as e:
        return _format_json_error(e, "Targetolog"), dict(_EMPTY_STATS)
    return _finish_pipeline(targetolog_plan, dry_run, chat_id)


def _finish_pipeline(targetolog_plan: dict, dry_run: bool = False, chat_id: int | None = None) -> tuple[str, dict]:
    """Targetolog allaqachon tuzgan action_plan'ni Marketolog'ga tekshirtiradi
    va tasdiqlangan action'larni ijro etadi. `_run_pipeline` va geo-lookup
    ikki bosqichli oqimi (`_run_pipeline_command`) ikkalasi ham shu yerga kelib
    tugaydi.

    `business_rules.json` dagi `skip_marketolog: true` bo'lsa, Marketolog
    bosqichi butunlay o'tkazib yuboriladi — Targetolog taklif qilgan HAMMA
    action to'g'ridan-to'g'ri ijroga yuboriladi (tezroq, lekin ikkinchi nazorat
    qatlamisiz).

    2026-08, foydalanuvchi so'rovi ("AI tokenlarni ko'p ichvoyapti, ekonom
    ishlasin"): tahlil qildik -- `job_watch_cycle` HAR SOATDA (kuniga 24
    marta) `_run_pipeline` orqali Targetolog'ni chaqiradi, va avval bu safar
    Targetolog nima topmasin (hatto HAMMA taklifi shunchaki "no_action"
    bo'lsa ham) Marketolog HAR DOIM to'liq qayta tekshirib chiqardi -- bu
    kuniga 24 ta QO'SHIMCHA, HECH NARSANI TASDIQLAMAYDIGAN Claude
    chaqiruvini anglatardi (Marketolog'ning vazifasi -- TAKLIF QILINGAN
    o'zgarishni tekshirish; agar hech qanday haqiqiy o'zgarish taklif
    qilinmagan bo'lsa, tekshirishning o'zi ma'nosiz). Endi -- agar
    Targetolog'ning BARCHA action'lari `type == "no_action"` bo'lsa,
    Marketolog chaqiruvi ATLAB o'tkazib yuboriladi (xuddi `skip_marketolog`
    kabi, lekin FAQAT shu holatda -- haqiqiy o'zgarish taklif qilinsa,
    Marketolog ODATDAGIDEK to'liq ishlaydi, ikkinchi nazorat qatlami
    SAQLANADI)."""
    skip_marketolog = bool(BUSINESS_RULES.get("skip_marketolog"))
    proposed_actions = targetolog_plan.get("actions") or []
    all_no_action = bool(proposed_actions) and all(a.get("type") == "no_action" for a in proposed_actions)

    if skip_marketolog or all_no_action:
        if skip_marketolog:
            reason = "business_rules.json: skip_marketolog=true"
            logger.info("skip_marketolog=true — Marketolog bosqichi o'tkazib yuborildi.")
        else:
            reason = "barcha takliflar 'no_action' — tekshirishga hech narsa yo'q"
            logger.info(
                "Targetolog'ning barcha takliflari 'no_action' — Marketolog chaqiruvi "
                "(Claude, ~%s belgi tizim prompti) tejash uchun o'tkazib yuborildi.",
                len(MARKETOLOG_SYSTEM),
            )
        marketolog_review = {
            "review_summary": f"(Marketolog o'tkazib yuborildi — {reason})",
            "decisions": [
                {"action_index": i, "type": a["type"], "decision": "approved", "comment": "auto (marketolog skipped)"}
                for i, a in enumerate(proposed_actions)
            ],
        }
    else:
        logger.info("Marketolog agent tekshirmoqda...")
        try:
            marketolog_review = _call_agent(
                MARKETOLOG_SYSTEM,
                "Targetolog taklif qilgan action_plan:\n\n"
                f"{json.dumps(targetolog_plan, ensure_ascii=False, indent=2)}\n\n"
                "Biznes qoidalari:\n"
                f"{json.dumps(BUSINESS_RULES, ensure_ascii=False, indent=2)}",
            )
        except TargetologFormatError as e:
            logger.error("Marketolog JSON qaytarmadi. Xom javob: %s", e.raw_text[:1000])
            text = (
                "⚠️ Ichki tekshiruvda xatolik chiqdi, qaytadan urinib ko'ring.\n\n"
                f"{targetolog_plan.get('summary', '')}"
            )
            return text, dict(_EMPTY_STATS)

    succeeded, failed, skipped = [], [], []
    if not dry_run:
        for decision in marketolog_review.get("decisions", []):
            idx = decision["action_index"]
            action = targetolog_plan["actions"][idx]
            action_type = action["type"]

            if action_type == "no_action":
                # "Hech narsa qilmaslik" — bu XATO yoki KUTILMAGAN holat emas,
                # aksincha hammasi joyida degani. Statistikaga (succeeded/
                # failed/skipped) kirmaydi — aks holda kunlik cron "hammasi
                # yaxshi bo'lsa xabar yubormaslik" mantig'i ishlamay qolardi.
                continue

            if decision["decision"] not in ("approved", "approved_with_edit"):
                skipped.append({"action": action, "decision": decision})
                continue

            if action_type not in AUTO_EXECUTABLE_TYPES and action_type not in SCHEDULING_ACTION_TYPES:
                # create_instant_form -- inson tasdig'i kerak (replace_creative
                # 2026-08dan AUTO_EXECUTABLE_TYPES ichida, shuning uchun bu
                # yerga endi tushmaydi).
                skipped.append({"action": action, "decision": decision, "reason": "manual_step_required"})
                continue

            final_action = dict(action)
            if decision.get("final_params"):
                final_action["params"] = {**final_action.get("params", {}), **decision["final_params"]}

            try:
                if action_type == "schedule_on_off":
                    result = _execute_schedule_on_off(final_action, chat_id)
                elif action_type == "schedule_report":
                    result = _execute_schedule_report(final_action, chat_id)
                elif action_type == "cancel_standing_task":
                    result = _execute_cancel_standing_task(final_action, chat_id)
                else:
                    result = ACTION_EXECUTORS[action_type](final_action)
                succeeded.append({"action": final_action, "result": result})
            except meta_api.MetaAPIError as e:
                logger.exception("Action bajarishda Meta API xatoligi: %s", action_type)
                # Meta xatosi odatda {"message": "...", "type": "...", "code": ...}
                # ko'rinishidagi dict bo'ladi -- foydalanuvchiga faqat qisqa
                # `message` qismini ko'rsatamiz (2026-08, foydalanuvchi so'rovi:
                # "aniq muammo xatosini yozsin" -- xatolik xabari endi shunchaki
                # "xatolik chiqdi" emas, Meta NEGA rad etganini aytadi).
                meta_err = e.args[0] if e.args else {}
                short_error = meta_err.get("message", str(e)) if isinstance(meta_err, dict) else str(e)
                failed.append({"action": final_action, "error": short_error})
            except Exception as e:
                # MUHIM: MetaAPIError'dan tashqari HAR QANDAY xato (masalan
                # Targetolog kutilgan schema'ga to'liq amal qilmasa — KeyError/
                # TypeError) ham shu yerda tutiladi. Aks holda bitta action'dagi
                # kichik nuqson butun so'rovni "kutilmagan xatolik" bilan
                # buzib, foydalanuvchiga hech narsa tushunarli bo'lmagan xabar
                # ko'rsatib qo'yardi.
                logger.exception("Action bajarishda kutilmagan xato: %s", action_type)
                failed.append({"action": final_action, "error": f"{type(e).__name__}: {e}"})

    run_log = {
        "timestamp": datetime.utcnow().isoformat(),
        "targetolog_plan": targetolog_plan,
        "marketolog_review": marketolog_review,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "dry_run": dry_run,
    }
    log_path = LOGS_DIR / f"run_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
    log_path.write_text(json.dumps(run_log, ensure_ascii=False, indent=2), encoding="utf-8")

    # ODDIY, QISQA hisobot — texnik hisob-kitob emas, oddiy odamga
    # tushunarli xabar. Targetolog'ning o'z xulosasi (`summary`) allaqachon
    # oddiy tilda yozilgan bo'lishi kerak (system prompt shuni talab qiladi);
    # bu yerda faqat qisqa amaliy qo'shimcha qilinadi.
    # replace_creative endi (2026-08) AVTOMATIK ijro etiladi -- shuning uchun
    # bu ro'yxatda faqat create_instant_form qoladi (u hali odam tasdig'ini
    # kutadi). Yangi vizual kerak bo'lgan (matn-almashtirish yetarli bo'lmagan)
    # holatlar Targetolog tomonidan alohida action sifatida emas, `summary`
    # ichida odam dizaynerga TZ sifatida chiqariladi.
    creative_or_form_actions = [
        a for a in targetolog_plan.get("actions", [])
        if a["type"] in ("create_instant_form",)
    ]

    report_lines = [targetolog_plan.get("summary", "").strip()]

    if succeeded:
        # MUHIM (foydalanuvchi so'ragan aniqlik, KEYIN qisqartirilgan): avval
        # bu yerda har bir action'ning TO'LIQ, xom `reason` maydoni ham
        # qo'shib yuborilgan edi -- lekin `reason` audit/texnik maqsad uchun
        # (action_schema.md: "bu foydalanuvchiga ko'rsatilmasligi ham
        # mumkin"), ko'pincha bir necha jumlali texnik tushuntirish bo'ladi
        # (masalan "targeting_automation.advantage_audience=1 bo'lganligi
        # sababli..."). Buni Telegram xabariga xom holda qo'shish natijada
        # o'ta uzun, texnik xabar berardi -- foydalanuvchi buni skrinshot
        # bilan ko'rsatib, "qisqa va aniq, ma'nosini yo'qotmasdan" deb
        # so'radi. Endi faqat target nomi + oddiy tildagi qisqa fe'l
        # ko'rsatiladi ("nima qilindi" yetarli) -- "nega" degan texnik
        # asos endi faqat `summary`da (Targetolog o'zi 2-3 gapda, oddiy
        # tilda beradi) va server logidagi run_*.json faylida qoladi.
        report_lines.append(f"\n✅ {len(succeeded)} ta o'zgarish qildim:")
        for s in succeeded[:10]:
            action = s["action"]
            name = action.get("object_name", action.get("object_id", "?"))
            verb = _ACTION_FRIENDLY_VERB.get(action["type"], action["type"])
            report_lines.append(f"   🔧 {name}: {verb}")
        if len(succeeded) > 10:
            report_lines.append(f"   ...va yana {len(succeeded) - 10} tasi.")
    if failed:
        # 2026-08, foydalanuvchi so'rovi ("aniq muammo xatosini yozsin" --
        # avval faqat nomlar ro'yxati ko'rsatilardi, "3 tasida xatolik
        # chiqdi" deb, SABABSIZ -- endi har birining QISQA sababi ham
        # ko'rsatiladi, shunda admin muammoni tushunib to'g'irlay oladi.
        report_lines.append(f"\n⚠️ {len(failed)} tasida xatolik chiqdi -- hisobda hech narsa o'zgarmadi:")
        for f in failed[:5]:
            name = f["action"].get("object_name", f["action"].get("object_id", "?"))
            reason = (f.get("error") or "noma'lum xato")[:150]
            report_lines.append(f"   ❌ {name}: {reason}")
        if len(failed) > 5:
            report_lines.append(f"   ...va yana {len(failed) - 5} tasi (batafsili server logida).")
    if creative_or_form_actions:
        names = ", ".join(a.get("object_name", "?") for a in creative_or_form_actions[:5])
        report_lines.append(f"\n🎨 Bularga sizning tasdig'ingiz kerak: {names}.")

    text = "\n".join(line for line in report_lines if line).strip()
    stats = {
        "succeeded": len(succeeded),
        "failed": len(failed),
        "skipped": len(skipped),
        "manual_suggestions": len(creative_or_form_actions),
    }
    return text, stats


def run_analysis_cycle_with_stats(dry_run: bool = False, chat_id: int | None = None) -> tuple[str, dict]:
    """`run_analysis_cycle()` bilan bir xil, lekin matn bilan birga aniq
    statistikani (`{"succeeded", "failed", "skipped", "manual_suggestions"}`)
    ham qaytaradi — matnni regex bilan "tahlil qilish" shart emas."""
    data = gather_data()
    data_json = json.dumps(data, ensure_ascii=False, indent=2)
    return _run_pipeline(
        f"Quyidagi ma'lumotlar asosida to'liq hisobni tahlil qilib action_plan tuzing:\n\n{data_json}",
        dry_run=dry_run,
        chat_id=chat_id,
    )


def run_analysis_cycle(dry_run: bool = False, chat_id: int | None = None) -> str:
    """To'liq hisobni tahlil qiladi (barcha kampaniya/adset/ad + region breakdown).
    Telegram bot `/analyze` buyrug'i shu funksiyani chaqiradi."""
    text, _stats = run_analysis_cycle_with_stats(dry_run=dry_run, chat_id=chat_id)
    return text


def run_daily_cron_report(dry_run: bool = False) -> str | None:
    """VERCEL CRON UCHUN: `run_analysis_cycle()` bilan bir xil to'liq tahlilni
    ishga tushiradi, lekin foydalanuvchiga faqat DIQQATGA LOYIQ narsa bo'lsa
    (biror action bajarildi/xato berdi/qo'lda ko'rib chiqish kerak bo'lsa)
    xabar qaytaradi. Agar hisobda hech narsa o'zgarmagan va hammasi joyida
    bo'lsa — `None` qaytaradi, ya'ni kunlik "hammasi joyida" degan bo'sh
    xabar bilan bezovta qilinmaydi."""
    text, stats = run_analysis_cycle_with_stats(dry_run=dry_run)

    if not any(stats.values()):
        return None
    return text


def handle_budget_message(user_text: str, chat_id: int) -> str:
    """Foydalanuvchi byudjet/pul haqida yozganda chaqiriladi (masalan 'bugun
    500$ tushdi', 'qancha qoldi', 'qachon tugaydi'). Arzon model (LIGHT_MODEL)
    bilan bu deposit xabarimi yoki savolmi va agar deposit bo'lsa qancha
    summa ekanini aniqlaydi, keyin haqiqiy hisob-kitobni `budget_tracker.py`
    (Meta'dan olingan REAL xarajat asosida) bajaradi — model o'zi raqam
    o'ylab topmaydi, faqat matnni tushunadi."""
    text = call_light(
        "Foydalanuvchi reklama byudjeti/puli haqida yozmoqda. Faqat JSON "
        'qaytar: {"type": "deposit" yoki "query", "amount": <deposit bo\'lsa '
        "dollar miqdori (raqam), aks holda null>}. Masalan: "
        "'bugun 500$ tushdi' -> {\"type\":\"deposit\",\"amount\":500}. "
        "'gruppaga 200 dollar tashladim' -> {\"type\":\"deposit\",\"amount\":200}. "
        "'qancha qoldi', 'qachon tugaydi', '$100 qolganda ayt' -> "
        '{"type":"query","amount":null}. Faqat JSON qaytar, boshqa matn yo\'q.',
        user_text,
        max_tokens=60,
    ).strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = {"type": "query", "amount": None}

    if parsed.get("type") == "deposit" and parsed.get("amount"):
        amount = float(parsed["amount"])
        status = budget_tracker.record_deposit(amount, chat_id)
        header = f"✅ ${amount:.0f} balansga qo'shildi.\n\n"
        return header + budget_tracker.format_status_message(status)

    budget_tracker.set_notify_chat_id(chat_id)
    status = budget_tracker.get_status()
    return budget_tracker.format_status_message(status)


def classify_intent(
    user_text: str, recent_history: list[dict] | None = None
) -> tuple[str, str]:
    """Foydalanuvchi Telegram'da erkin matn yozganda chaqiriladi. Bu matn
    haqiqiy amaliy buyruqmi (masalan 'yangi target yoq', 'X reklamani to'xtat',
    'abtest boshla') yoki oddiy savolmi -- shuni ARZON model (Haiku) bilan tez
    aniqlaydi. Og'ir ishning o'zini BAJARMAYDI -- buni ataylab `execute_intent()`
    ga ajratib qo'ydik, chunki Vercel webhook OG'IR (ACTION/ANALYSIS) va
    YENGIL (BUDGET/METRIC/GENERAL) turlarni turlicha ishlatishi kerak (og'irini
    fon so'rovga uzatib, Vercel'ning 60 soniyalik timeout'idan qochish uchun).

    `recent_history` -- suhbatning so'nggi xabarlari (agar Targetolog oldin
    "byudjetingiz qancha?" deb so'ragan bo'lsa, keyingi "50000" degan javob
    shu kontekst bilan to'g'ri bog'lanishi uchun).

    Qaytaradi: `(verdict, history_text)` -- `history_text` ham qaytariladi,
    chunki `execute_intent()` uni qayta hisoblamasligi kerak.
    """
    history_text = ""
    if recent_history:
        history_text = "\n\nSo'nggi suhbat konteksti:\n" + "\n".join(
            f"{m['role']}: {m['content']}" for m in recent_history[-6:]
        )

    verdict = call_light(
        (
            "Foydalanuvchi xabari qaysi turga kiradi? Faqat bitta so'z bilan javob ber:\n"
            "BUDGET -- agar reklama HISOB BALANSI/PULI haqida bo'lsa: hisobga pul "
            "tushirilgani haqida xabar (masalan 'bugun 500$ tushdi', 'gruppaga 200 "
            "dollar tashladim'), yoki shu pul qancha qolgani/qachon tugashi haqidagi "
            "savol (masalan 'qancha qoldi', 'necha kunga yetadi', 'qachon tugaydi', "
            "'100$ qolganda ayt'). Bu ADS ACCOUNT balansi haqida, aniq bitta ad'ning "
            "CPA/CTR kabi ijro ko'rsatkichi haqida EMAS (u METRIC).\n"
            "ANALYSIS -- agar foydalanuvchi BUTUN hisobni yoki bir nechta kampaniyani "
            "KENG QAMROVLI tahlil qilishni so'rasa (masalan: 'hisobimni tahlil qil', "
            "'targetni to'liq tekshir', 'nima muammo bor', 'umumiy holatni ko'rsat') -- "
            "bitta aniq obyektga qaratilgan tor savol EMAS, balki to'liq audit so'ralganda.\n"
            "ACTION -- agar amaliy buyruq bo'lsa: yangi target/kampaniya yoqish, mavjud "
            "reklamani to'xtatish/yoqish, byudjet o'zgartirish, abtest boshlash, auditoriya/"
            "hudud o'zgartirish (masalan biror viloyat/shaharni QO'SHISH yoki OLIB TASHLASH/"
            "EXCLUDE qilish, \"faqat X qolsin\", \"Y'ni chiqarib tashla\"), yoki shu buyruqqa "
            "javoban berilgan qo'shimcha ma'lumot (byudjet raqami, shahar nomi). Foydalanuvchi "
            "kampaniya/adset nomini o'z uslubida yozishi mumkin (masalan \"AB | Traffic | IG\", "
            "qisqartmalar, \" | \" bilan ajratilgan nomlar) -- bu ham ACTION, GENERAL emas. "
            "Bunga MUHIM MISOL: foydalanuvchi biror target/kampaniyaning natijasi/lead soni "
            "KAM/PAST deb shikoyat qilsa va uni YAXSHILASH/KO'PAYTIRISHNI so'rasa (masalan "
            "\"bu targetda lead kam, ko'paytir\", \"X kam ishlayapti, tuzat\") -- bu ham ACTION "
            "(Targetolog o'zi choralar ko'rishi kerak), METRIC (faqat raqam ko'rsatish) EMAS.\n"
            "METRIC -- agar haqiqiy hisobdagi JORIY raqam/statistika so'ralayotgan bo'lsa. "
            "Bunga ikki xil so'rov kiradi: (1) ANIQ bitta ko'rsatkich (masalan 'video necha "
            "kishi ko'rgan', 'CPA qancha', 'necha % odam 15 soniyani ko'rgan'), VA (2) "
            "aniq ko'rsatkich nomi aytilmagan, lekin foydalanuvchi hisobning JORIY holati/"
            "raqamlarini so'rayotgan umumiy so'rovlar -- masalan 'target ma'lumot ber', "
            "'bugungi ma'lumotlarni ber', 'hisobot ber', 'statistika ko'rsat', 'necha lead "
            "keldi', 'bugun qanday ketyapti' kabi. Bunday umumiy so'rovlarda ham javob "
            "REAL Meta ma'lumotidan (lead soni, xarajat, CPL va h.k.) tuzilishi kerak -- "
            "GENERAL emas, chunki foydalanuvchi maslahat emas, HAQIQIY raqam kutmoqda.\n"
            "(3) foydalanuvchi ANIQ bir kun/sana yoki oraliq haqida so'raganda (masalan '20 iyulni bergin', '1-10 avgust qancha ketdi') -- bu ham METRIC, javob shu ANIQ davr uchun bo'lishi kerak, standart 7 kun EMAS. VA (4) foydalanuvchi 'rejalashtirilgan', 'tayyor turgan', 'pauzadagi', 'hali yoqilmagan', 'to'xtatilgan' targetlar/kampaniyalar haqida so'raganda (masalan 'rejalashtirilgan targetlar bormi', 'qaysi target pauzada') -- bu ham METRIC (hisob tuzilmasi/status ma'lumotidan javob beriladi), ACTION yoki GENERAL EMAS.\n"
            "GENERAL -- FAQAT hisobning joriy holati/raqamlari SO'RALMAGAN, sof bilim/"
            "maslahat savoli bo'lsa (masalan 'CBO nima', 'byudjetni qachon oshirish kerak', "
            "'yaxshi kreativ qanday bo'ladi'). Agar xabarda 'ma'lumot', 'hisobot', 'statistika', "
            "'bugungi holat' kabi so'zlar hisobga nisbatan ishlatilgan bo'lsa -- bu GENERAL "
            "EMAS, METRIC (yuqoriga qarang)."
        ),
        f"{history_text}\n\nYangi xabar: {user_text}",
        max_tokens=20,
    ).strip().upper()
    return verdict, history_text


def is_heavy_intent(verdict: str) -> bool:
    """ACTION va ANALYSIS -- Meta API'dan bir necha marta o'qish + Claude
    Sonnet chaqiruv(lar)i + ijro/tekshirish zanjirini talab qiladi, ba'zan
    bir necha o'n soniya davom etadi. Vercel'ning 60 soniyalik funksiya
    limitiga urilib qolmasligi uchun webhook bularni FON (background)
    so'rovga uzatadi; BUDGET/METRIC/GENERAL yengil va darhol bajariladi."""
    return "ACTION" in verdict or "ANALYSIS" in verdict


def execute_intent(
    verdict: str, user_text: str, history_text: str = "", chat_id: int | None = None
) -> str | None:
    """`classify_intent()` aniqlagan turga qarab tegishli ishni bajaradi va
    natija matnini (yoki oddiy savol bo'lsa `None`) qaytaradi."""
    if "BUDGET" in verdict:
        return handle_budget_message(user_text, chat_id) if chat_id is not None else None
    if "ANALYSIS" in verdict:
        return run_analysis_cycle(dry_run=False, chat_id=chat_id)
    if "ACTION" in verdict:
        # Avval ARZON (faqat OpenAI) yo'lni sinab ko'ramiz -- faqat oddiy,
        # bitta qadamli, mulohaza talab qilmaydigan buyruqlar uchun ishlaydi
        # (pause/resume/schedule_on_off/schedule_report/cancel_standing_task).
        # Har qanday noaniqlik bo'lsa `None` qaytadi -- shunda avvalgidek
        # to'liq Claude (Targetolog+Marketolog) pipeline'iga tushamiz.
        try:
            light_result = _execute_simple_action(user_text, chat_id)
        except Exception:
            logger.exception("Yengil buyruq yo'lida kutilmagan xato -- Claude yo'liga o'tildi.")
            light_result = None
        if light_result is not None:
            return light_result
        return _run_pipeline_command(user_text, history_text, chat_id)
    if "METRIC" in verdict:
        return answer_data_question(user_text, history_text)
    return None


def handle_chat_command(
    user_text: str, recent_history: list[dict] | None = None, chat_id: int | None = None
) -> str | None:
    """Eski nom, moslik uchun saqlangan: `classify_intent()` + `execute_intent()`ni
    ketma-ket, BITTA chaqiruv ichida (fon so'rovga ajratmasdan) bajaradi.
    VPS/mahalliy rejim (`telegram_bot.py`, uzoq-polling) shuni ishlatadi --
    u yerda Vercel'ning 60 soniyalik cheklovi yo'q, shuning uchun fon
    so'rovga ehtiyoj ham yo'q. Vercel webhook (`api/index.py`) endi
    `classify_intent`/`is_heavy_intent`/`execute_intent`ni to'g'ridan-to'g'ri,
    alohida-alohida ishlatadi."""
    verdict, history_text = classify_intent(user_text, recent_history)
    return execute_intent(verdict, user_text, history_text, chat_id)


def _run_pipeline_command(user_text: str, history_text: str, chat_id: int | None = None) -> str:
    # MUHIM: foydalanuvchi kampaniya/adset'ni ko'pincha NOM bilan ataydi
    # (masalan "AB | Traffic | IG"), Meta ID bilan emas. Shuning uchun har bir
    # amaliy buyruqdan oldin joriy hisob strukturasini (nom + haqiqiy ID)
    # Targetologga beramiz — aks holda u ID'ni bila olmay, action_plan o'rniga
    # oddiy matnli tavsiya yozib qo'yadi (bajarilmagan bo'lib qoladi).
    # Ikkalasi ham bir-biriga bog'liq emas -- parallel (bir vaqtda) so'rab,
    # ketma-ket kutishning o'rniga umumiy kutish vaqtini taxminan yarmiga
    # tushiramiz (Vercel'ning 60 soniyalik funksiya limitiga urilib qolish
    # xavfini kamaytirish uchun muhim).
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        structure_future = pool.submit(meta_api.get_account_structure)
        insights_future = pool.submit(meta_api.get_insights, level="campaign", date_preset="last_7d")

        try:
            account_structure = structure_future.result()
            structure_json = json.dumps(account_structure, ensure_ascii=False, indent=2)
        except meta_api.MetaAPIError as e:
            return f"⚠️ Meta hisobi bilan bog'lanib bo'lmadi: {e}"

        try:
            campaign_insights = insights_future.result()
            insights_json = json.dumps(campaign_insights, ensure_ascii=False, indent=2)
        except meta_api.MetaAPIError as e:
            insights_json = f"(statistika olinmadi: {e})"

    message = (
        "Foydalanuvchi Telegram orqali quyidagi amaliy buyruqni berdi (kerak bo'lsa "
        f"suhbat konteksti bilan birga):{history_text}\n\n"
        f"Yangi xabar: \"{user_text}\"\n\n"
        "Joriy hisobdagi kampaniya/adset/ad nomlari va ID'lari (targeting "
        f"tafsilotlarisiz — kerak bo'lsa alohida so'rang):\n{structure_json}\n\n"
        f"So'nggi 7 kunlik kampaniya darajasidagi statistika (CPM/CTR/CPC/spend/"
        f"reach/frequency/actions):\n{insights_json}\n\n"
        "Agar buyruqda hudud/shahar/tuman nomi (masalan \"Chirchiq\", \"Zangiota\") "
        "qo'shish yoki chiqarib tashlash (exclude) kerak bo'lsa-yu, lekin sizda "
        "ularning Meta rasmiy geo-target kaliti (key) yo'q bo'lsa — `no_action` "
        "qaytarib, `actions[0].params.geo_lookup_needed` ro'yxatida shu joy "
        "nomlarini bering.\n"
        "Agar `adjust_audience` uchun biror adset'ning JORIY to'liq targeting'ini "
        "bilish kerak bo'lsa — `no_action` qaytarib, `actions[0].params."
        "adset_details_needed` ro'yxatida o'sha adset'ning (account_structure'dan "
        "topilgan) ID'sini bering.\n"
        "Ikkalasini ham bir vaqtda so'rashingiz mumkin — sizga natijalar birga "
        "qaytariladi va qayta so'ralasiz.\n"
        "Agar buyruqdagi nomga mos kampaniya/adset topilmasa YOKI yangi targeting "
        "uchun ma'lumot (soha, maqsad, byudjet, hudud) yetarli bo'lmasa — `no_action` "
        "qaytarib aniq nima yetishmayotganini `summary`da so'rang. Aks holda to'liq "
        "action_plan tuzing (haqiqiy ID va to'liq targeting obyekti bilan)."
    )

    try:
        targetolog_plan = _call_agent(TARGETOLOG_SYSTEM, message)
    except TargetologFormatError as e:
        return _format_json_error(e, "Targetolog")

    # Ikkinchi bosqich: agar Targetolog hudud kaliti va/yoki adset'ning to'liq
    # targeting'ini so'ragan bo'lsa, ularni haqiqatan Meta'dan olib, qayta so'raymiz.
    first_action = (targetolog_plan.get("actions") or [{}])[0]
    params = first_action.get("params") or {}
    geo_lookup_needed = params.get("geo_lookup_needed")
    adset_details_needed = params.get("adset_details_needed")

    if first_action.get("type") == "no_action" and (geo_lookup_needed or adset_details_needed):
        extra_parts = []

        if geo_lookup_needed:
            geo_candidates = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(geo_lookup_needed))) as pool:
                futures = {pool.submit(meta_api.search_geo_location, place): place for place in geo_lookup_needed}
                for future in concurrent.futures.as_completed(futures):
                    place = futures[future]
                    try:
                        geo_candidates[place] = future.result()
                    except meta_api.MetaAPIError as e:
                        geo_candidates[place] = {"error": str(e)}
            extra_parts.append(
                "Hudud nomlari uchun Meta'dan topilgan rasmiy geo-target "
                f"nomzodlari:\n{json.dumps(geo_candidates, ensure_ascii=False, indent=2)}"
            )

        if adset_details_needed:
            adset_details = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(adset_details_needed))) as pool:
                futures = {pool.submit(meta_api.get_adset_details, aid): aid for aid in adset_details_needed}
                for future in concurrent.futures.as_completed(futures):
                    adset_id = futures[future]
                    try:
                        adset_details[adset_id] = future.result()
                    except meta_api.MetaAPIError as e:
                        adset_details[adset_id] = {"error": str(e)}
            extra_parts.append(
                "So'ralgan adset(lar)ning to'liq joriy sozlamalari:\n"
                f"{json.dumps(adset_details, ensure_ascii=False, indent=2)}"
            )

        followup_message = (
            message + "\n\n---\n\n" + "\n\n".join(extra_parts) + "\n\n"
            "Endi shu ma'lumotlar bilan to'liq action_plan tuzing. Agar hali ham "
            "biror narsa yetishmasa, `no_action` qaytarib buni ochiq ayting."
        )
        try:
            targetolog_plan = _call_agent(TARGETOLOG_SYSTEM, followup_message)
        except TargetologFormatError as e:
            return _format_json_error(e, "Targetolog (aniqlashtirish)")

    text, _stats = _finish_pipeline(targetolog_plan, dry_run=False, chat_id=chat_id)
    return text


def _resolve_query_period(user_text: str) -> tuple[dict, str]:
    """Foydalanuvchi so'ragan davrni aniqlaydi -- "bugun", "20 iyul", "1-10
    avgust" kabi ANIQ sana/oraliq aytilgan bo'lsa, arzon model orqali shuni
    ANIQ time_range (since/until)ga o'giradi. Hech qanday davr aytilmagan
    bo'lsa, standart so'nggi 7 kunga tushadi.

    Qaytaradi: `(meta_api.get_insights ga beriladigan kwargs, odam o'qiydigan
    davr nomi)` -- masalan `({"date_preset": "today"}, "bugungi kun")` yoki
    `({"time_range": {"since": "2026-07-20", "until": "2026-07-20"}}, "20.07.2026")`."""
    tashkent_today = (datetime.utcnow() + timedelta(hours=5)).date()

    # 1) DETERMINISTIK yo'l -- eng ko'p uchraydigan holatlar (bugun/kecha,
    # aniq bitta sana, kun oralig'i) LLM'ga umuman murojaat qilmasdan aniq
    # hisoblanadi, shuning uchun "27 iyul"da xato (2 barobar ko'p) xarajat
    # chiqishi kabi muammolar butunlay oldi olinadi.
    if _QP_TODAY_PATTERN.search(user_text or ""):
        return {"date_preset": "today"}, "bugungi kun"

    if _QP_YESTERDAY_PATTERN.search(user_text or ""):
        y = tashkent_today - timedelta(days=1)
        return {"time_range": {"since": y.isoformat(), "until": y.isoformat()}}, y.strftime("%d.%m.%Y")

    day_range = _qp_parse_day_range(user_text, tashkent_today)
    if day_range:
        since_d, until_d = day_range
        label = f"{since_d.strftime('%d.%m')}–{until_d.strftime('%d.%m.%Y')}"
        return {"time_range": {"since": since_d.isoformat(), "until": until_d.isoformat()}}, label

    explicit_date = _qp_parse_explicit_date(user_text, tashkent_today)
    if explicit_date:
        iso = explicit_date.isoformat()
        return {"time_range": {"since": iso, "until": iso}}, explicit_date.strftime("%d.%m.%Y")

    # 2) Zaxira: yuqoridagi aniq naqshlarga to'g'ri kelmagan boshqa turdagi
    # so'rovlar uchun (masalan "oxirgi hafta") arzon model orqali aniqlanadi.
    today_iso = tashkent_today.isoformat()
    extraction = call_light(
        f"Bugungi sana: {today_iso} (YYYY-MM-DD). Foydalanuvchi xabaridan aniq QAYSI "
        "SANA yoki DAVR haqida so'rayotganini aniqla. Faqat JSON qaytar: "
        '{"since": "YYYY-MM-DD" yoki null, "until": "YYYY-MM-DD" yoki null, '
        '"label": "odam o\'qiydigan qisqa nom (masalan \'20.07.2026\' yoki \'bugungi kun\')"}'
        '. Agar xabarda "bugun"/"hozir" bo\'lsa: since=until=bugungi sana. Agar aniq bitta '
        'sana aytilgan bo\'lsa (masalan "20 iyul"), yil ko\'rsatilmagan bo\'lsa joriy yildan '
        'hisobla (agar shu sana kelajakda chiqib qolsa, o\'tgan yildan ol); since=until=o\'sha '
        'sana. Agar oraliq aytilgan bo\'lsa ("1-10 avgust"), since/until shunga mos. Agar '
        'hech qanday aniq sana/davr aytilmagan bo\'lsa, since=null, until=null, '
        'label="so\'nggi 7 kun" qaytar. Faqat JSON, boshqa matn yo\'q.',
        user_text,
        max_tokens=80,
    )
    text = extraction.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = {}

    since = parsed.get("since")
    until = parsed.get("until")
    label = parsed.get("label") or "so'nggi 7 kun"
    if since and until:
        return {"time_range": {"since": since, "until": until}}, label
    return {"date_preset": "last_7d"}, label


def _admin_report_header(period_label: str, hisobot_vaqti: str, subtitle: str) -> str:
    return (
        "\U0001F4CA ADMIN TARGET HISOBOTI\n\n"
        f"\U0001F4C5 Davr: {period_label}\n"
        f"\U0001F553 Hisobot vaqti: {hisobot_vaqti}\n"
        f"\U0001F4DD {subtitle}\n"
        "\U0001F7E2 Real Data\n\n"
    )


def build_admin_report(
    period_label: str,
    hisobot_vaqti: str,
    subtitle: str = "Joriy holat",
    insight_kwargs: dict | None = None,
) -> str:
    """"ADMIN TARGET HISOBOTI" qat'iy formatidagi hisobotni quradi (kunlik
    09:00 cron VA oddiy "ma'lumot/hisobot ber" so'rovlari -- IKKALASI HAM shu
    bir xil ko'rinishda javob berishi uchun). Sarlavha/sana/vaqt qismini biz
    o'zimiz (deterministik) yozamiz.

    MUHIM (bug fix, ikkinchi marta): bu funksiya AVVAL raqamlarni OpenAI'ga
    hisoblatgan edi -- va bu ikki marta xato chiqargan: (1) bitta leadni 3
    marta hisoblab "Leadlar: 3" deb chiqargan, (2) HAR BIR kampaniyaga
    majburan "lead" deb yorliq yopishtirgan, shuning uchun Traffic/SMS/
    boshqa maqsadli kampaniyalar (masalan "AB | Traffic | IG", $110.30
    xarajat) doim "0 lead" bo'lib noto'g'ri ko'rsatilgan (foydalanuvchi
    skrinshot bilan ko'rsatgan). Endi bu funksiya `monthly_report.py`dagi
    BIR XIL ishonchli, 100% DETERMINISTIK (LLM'siz) dvigatel bilan ishlaydi
    -- har bir kampaniyaning HAQIQIY maqsadi (Lead/Xabar/Profil tashrif/
    Sotuv/Noma'lum) nomi/`objective`sidan aniqlanadi, mos natija turi
    ko'rsatiladi -- boshqa turdagi kampaniyaga "lead" yorlig'i yopishtirilmaydi."""
    insight_kwargs = insight_kwargs or {"date_preset": "today"}
    header = _admin_report_header(period_label, hisobot_vaqti, subtitle)
    try:
        account_rows = meta_api.get_insights(level="account", fields=meta_api.DEFAULT_FIELDS, **insight_kwargs)
        campaigns, _totals = monthly_report.compute_campaigns_and_totals(**insight_kwargs)
    except meta_api.MetaAPIError as e:
        return header + f"\u26A0\uFE0F Meta API'dan ma'lumot olishda xatolik: {e}"

    account_row = account_rows[0] if account_rows else {}
    spend = monthly_report._safe_float(account_row.get("spend"))
    actions = account_row.get("actions") or []
    leads = monthly_report._first_matching_action_value(actions, monthly_report.LEAD_ACTION_PRIORITY, "lead")
    messages = monthly_report._first_matching_action_value(actions, monthly_report.MESSAGE_ACTION_PRIORITY, "messag")
    results = leads + messages
    cpl = (spend / results) if results else None
    impressions = int(monthly_report._safe_float(account_row.get("impressions")))
    reach = int(monthly_report._safe_float(account_row.get("reach")))
    ctr = monthly_report._safe_float(account_row.get("ctr"))
    cpc = monthly_report._safe_float(account_row.get("cpc"))
    cpm = monthly_report._safe_float(account_row.get("cpm"))
    frequency = monthly_report._safe_float(account_row.get("frequency"))

    body_lines = [
        f"\U0001F4B0 Xarajat: {monthly_report._fmt_money(spend)}",
        f"\U0001F4E9 Leadlar: {leads}",
        f"\U0001F4AC Xabarlar: {messages}",
        f"\U0001F3AF CPL: {monthly_report._fmt_money(cpl)}",
        f"\U0001F4C8 CTR: {ctr:.2f}%",
        f"\U0001F4F1 CPC: {monthly_report._fmt_money(cpc)}",
        f"\U0001F4F6 CPM: {monthly_report._fmt_money(cpm)}",
        f"\U0001F441 Impressions: {monthly_report._fmt_int(impressions)}",
        f"\U0001F4CD Reach: {monthly_report._fmt_int(reach)}",
        f"\U0001F504 Frequency: {frequency:.2f}",
        "",
        "\U0001F4CB Har bir target natijasi:",
        "",
    ]
    if campaigns:
        blocks = []
        for c in campaigns:
            cpl_str = monthly_report._fmt_money(c["cpl"])
            # MUHIM: "CPL" atamasi faqat Lead uchun to'g'ri -- Traffic/Sotuv/
            # boshqa yo'nalishlarda "narxi bitta lead uchun" degani NOTO'G'RI
            # bo'lar edi, shuning uchun bunday hollarda umumiy "Narx" so'zi
            # ishlatiladi (masalan "Narx: $0.48 (bitta profil tashrif uchun)").
            price_label = "CPL" if c["direction"] == "Lead" else "Narx"
            blocks.append(
                f"\U0001F539 {c['name']} ({c['direction']})\n"
                f"   \U0001F4B0 {monthly_report._fmt_money(c['spend'])} | "
                f"\U0001F4CA {c['results']} {c['direction'].lower()} | "
                f"\U0001F3AF {price_label} {cpl_str}"
            )
        body_lines.append("\n\n".join(blocks))
    else:
        body_lines.append("(faol kampaniya topilmadi)")

    return header + "\n".join(body_lines)




# MUHIM (bug fix): Vercel bir funksiya chaqiruvini `maxDuration` chegarasida
# QATTIQ o'ldiradi (FUNCTION_INVOCATION_TIMEOUT/504) -- bu HAQIQIY process
# SIGKILL kabi ishlaydi, shuning uchun `process_action()` ichidagi oddiy
# try/except uni HECH QACHON tuta olmaydi (kod umuman davom etmaydi, hech
# qanday except bloki ishga tushmaydi). Natijada foydalanuvchi "Qabul
# qildim, ishlab chiqyapman..." xabaridan keyin ABADIY hech narsa
# olmasdi -- xatolik ham, natija ham (foydalanuvchi buni skrinshot bilan
# ko'rsatdi: Vercel logida 504 bor, lekin Telegram'da hech narsa yo'q).
#
# Yechim: fon ishi BOSHLANISHIDAN OLDIN chat_id KV'ga "kutilyapti" deb
# belgilanadi (`mark_action_pending`), ishi TUGAGANDA (muvaffaqiyatli YOKI
# except orqali ushlangan xato bilan) belgi OLIB TASHLANADI
# (`clear_action_pending`). Alohida, tez-tez ishlaydigan cron
# (`/api/cron/pending-check`) belgisi hali ham turgan (ya'ni process_action
# hech qachon tugamagan -- SIGKILL bo'lgan) yozuvlarni topib, foydalanuvchiga
# "bu buyruq vaqt tugashi sababli bekor bo'ldi" deb ALOHIDA xabar beradi --
# shu orqali "bot hech narsa demadi" holati BUTUNLAY yo'qoladi, hatto
# maxDuration limitiga urilib qolgan taqdirda ham.
_PENDING_ACTIONS_KV_KEY = "pending_actions"
_PENDING_ACTION_TIMEOUT_SECONDS = 90  # vercel.json maxDuration=300 dan kichikroq marj bilan


def mark_action_pending(chat_id: int, user_text: str) -> None:
    """Fon ishi (`/api/process-action`) yuborilishidan OLDIN chaqiriladi."""
    pending = kv_store.get_json(_PENDING_ACTIONS_KV_KEY, default={})
    pending[str(chat_id)] = {
        "started_at": datetime.utcnow().isoformat(),
        "user_text": (user_text or "")[:200],
    }
    kv_store.set_json(_PENDING_ACTIONS_KV_KEY, pending)


def clear_action_pending(chat_id: int) -> None:
    """Fon ishi TUGAGANDA (muvaffaqiyatli yoki except orqali ushlangan
    xato bilan) chaqiriladi -- belgi endi kerak emas."""
    pending = kv_store.get_json(_PENDING_ACTIONS_KV_KEY, default={})
    if str(chat_id) in pending:
        del pending[str(chat_id)]
        kv_store.set_json(_PENDING_ACTIONS_KV_KEY, pending)


def check_stale_pending_actions(timeout_seconds: int = _PENDING_ACTION_TIMEOUT_SECONDS) -> list[tuple[int, str]]:
    """`/api/cron/pending-check` tomonidan tez-tez (masalan har 1-2 daqiqada)
    chaqiriladi -- HECH QANDAY LLM chaqiruvisiz, faqat KV o'qish/yozish,
    shuning uchun deyarli bepul va tez. `started_at`dan beri `timeout_seconds`
    dan ko'p vaqt o'tgan (demak `process_action` hech qachon tugamagan --
    Vercel tomonidan SIGKILL qilingan) yozuvlarni topib, ularni KV'dan olib
    tashlaydi va `(chat_id, foydalanuvchiga_yuboriladigan_xabar)` ro'yxatini
    qaytaradi."""
    pending = kv_store.get_json(_PENDING_ACTIONS_KV_KEY, default={})
    if not pending:
        return []
    now = datetime.utcnow()
    stale: list[tuple[int, str]] = []
    remaining = {}
    for chat_id_str, info in pending.items():
        started_raw = info.get("started_at") if isinstance(info, dict) else None
        try:
            started = datetime.fromisoformat(started_raw) if started_raw else None
        except ValueError:
            started = None
        if started is None:
            continue  # buzilgan yozuv -- e'tiborsiz qoldiramiz (o'chirib tashlanadi)
        elapsed = (now - started).total_seconds()
        if elapsed >= timeout_seconds:
            user_text = (info.get("user_text") or "").strip() if isinstance(info, dict) else ""
            text = (
                "\u26a0\ufe0f Oldingi buyrug'ingizni bajarish juda uzoq davom etib, "
                "server vaqti tugagani sababli BEKOR bo'ldi"
                + (f":\n\u201c{user_text}\u201d" if user_text else ".")
                + "\n\nIltimos, kichikroq/aniqroq qilib qaytadan yuboring (masalan bitta "
                "kampaniya uchun alohida-alohida so'rang)."
            )
            try:
                stale.append((int(chat_id_str), text))
            except ValueError:
                pass
        else:
            remaining[chat_id_str] = info
    if len(remaining) != len(pending):
        kv_store.set_json(_PENDING_ACTIONS_KV_KEY, remaining)
    return stale


def _current_tashkent_time() -> tuple[str, str]:
    now = datetime.utcnow() + timedelta(hours=5)  # O'zbekiston vaqti (UTC+5)
    return now.strftime("%d.%m.%Y"), now.strftime("%H:%M")


def answer_data_question(user_text: str, history_text: str = "") -> str:
    """Foydalanuvchi hisobdagi aniq metrika/raqamni, umumiy joriy holatni,
    yoki REJALASHTIRILGAN/PAUZADAGI (hali yoqilmagan/o'chirilgan) targetlar
    haqida so'raganda (masalan: 'CPA qancha', 'bugungi ma'lumotlarni ber',
    '20 iyulni bergin', 'rejalashtirilgan targetlar bormi') chaqiriladi.

    Foydalanuvchining aniq talabiga ko'ra: javob HAR DOIM kunlik 09:00
    "ADMIN TARGET HISOBOTI" bilan BIR XIL qat'iy formatda beriladi
    (`build_admin_report`), farqi faqat davr (`_resolve_query_period`
    orqali aniqlanadi) va sarlavhadagi vaqt/izoh. Agar savol aynan
    rejalashtirilgan/pauzadagi targetlar haqida bo'lsa, pastiga
    `account_structure`dan (HAQIQIY `status` maydoni, LLM'siz oddiy
    filtrlash orqali) PAUSED ro'yxati ham qo'shiladi."""
    insight_kwargs, period_label = _resolve_query_period(user_text)
    _, hisobot_vaqti = _current_tashkent_time()
    report = build_admin_report(period_label, hisobot_vaqti, "So'ralgan ma'lumot", insight_kwargs)

    if _PLANNED_KEYWORDS.search(user_text):
        try:
            structure = meta_api.get_account_structure(active_only=False)
        except meta_api.MetaAPIError as e:
            report += f"\n\n\u26A0\uFE0F Hisob tuzilmasini olishda xatolik: {e}"
        else:
            paused_names = []
            for obj_type in ("campaigns", "adsets", "ads"):
                for obj in structure.get(obj_type, []):
                    if str(obj.get("status", "")).upper() == "PAUSED":
                        paused_names.append(f"{obj.get('name', obj.get('id'))} ({obj_type[:-1]})")
            if paused_names:
                report += "\n\n\u23F8 Rejalashtirilgan/pauzadagi targetlar:\n" + "\n".join(
                    f"- {name}" for name in paused_names
                )
            else:
                report += "\n\n\u23F8 Hozircha rejalashtirilgan/pauzadagi target yo'q."

    return report


if __name__ == "__main__":
    print(run_analysis_cycle(dry_run=False))
