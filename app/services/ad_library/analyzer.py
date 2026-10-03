"""AI Analyze — bitta raqobatchi reklamasini tahlil qiladi: Hook, Offer, CTA,
Pain point, Creative type, Lead magnet (+ qisqa xulosa va bizga g'oya).

- Faqat foydalanuvchi "AI Analyze" bosganda (avtomatik emas) -- token tejaladi.
- Natija reklama ID bo'yicha keshlanadi (`kv_store`, 30 kun): bir xil
  reklama qayta bosilsa, AI qayta chaqirilmaydi.
- Kompaniya bo'yicha kunlik limit: AD_LIBRARY_AI_DAILY_LIMIT (standart 40).
- Model: mavjud `orchestrator.call_light` (OPENAI_API_KEY)."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import time

import kv_store

from .provider import Ad

CACHE_TTL_SECONDS = 30 * 86400
FIELDS = ("hook", "offer", "cta", "pain_point", "creative_type", "lead_magnet", "summary", "idea")

SYSTEM_PROMPT = """Sen Meta (Facebook/Instagram) reklamalari bo'yicha tajribali performance-marketologsan.
Senga raqobatchining BITTA reklamasi beriladi (matn, sarlavha, tugma, format, qachondan beri ishlayotgani).
Uni tahlil qilib, FAQAT quyidagi kalitlar bilan JSON qaytar (boshqa hech narsa yozma):
{
  "hook": "birinchi 1-2 soniyada/qatorda e'tiborni tortadigan narsa -- aniq iqtibos yoki tavsif",
  "offer": "nima taklif qilinyapti: mahsulot, narx, chegirma, bonus, kafolat",
  "cta": "harakatga chaqiruv va u qayerga olib boradi (sayt, Direct, Telegram, forma)",
  "pain_point": "qaysi og'riq/ehtiyojga bosilyapti",
  "creative_type": "format va uslub: video/rasm/karusel; UGC, mahsulot ko'rsatish, before/after, ekspert va h.k.",
  "lead_magnet": "lid yig'ish uchun bepul/qimmatli narsa (sinov, konsultatsiya, katalog, chegirma kodi) yoki 'yo'q'",
  "summary": "1-2 gapda: reklama nega ishlashi (yoki ishlamasligi) mumkin",
  "idea": "biz uchun 1 ta aniq g'oya: shunga qarshi qanday reklama qilish mumkin"
}
Qoidalar: o'zbek tilida (lotin), qisqa va aniq yoz; reklamada bo'lmagan narsani to'qima --
ma'lumot yetarli bo'lmasa "aniq emas" deb yoz. Uzoq ishlayotgan reklama (14+ kun) odatda natija beradi."""


class AnalyzeError(Exception):
    code = "analyze_error"


class AnalyzeLimitReached(AnalyzeError):
    code = "limit"


def daily_limit() -> int:
    try:
        return max(0, int(os.environ.get("AD_LIBRARY_AI_DAILY_LIMIT", "40")))
    except ValueError:
        return 40


def _usage_key(company_id) -> str:
    return f"adlib_ai_usage:{company_id or 0}:{dt.date.today().isoformat()}"


def usage_today(company_id) -> int:
    return int(kv_store.get_json(_usage_key(company_id), 0) or 0)


def _ad_prompt(ad: Ad) -> str:
    lines = [
        f"Sahifa: {ad.page_name or '—'}",
        f"Format: {ad.media_type}" + (f" ({ad.variants} ta variant)" if ad.variants and ad.variants > 1 else ""),
        f"Platformalar: {', '.join(ad.platforms) or '—'}",
        f"Holat: {'faol' if ad.is_active else 'to‘xtagan'}; boshlangan: {ad.start_date or '—'}"
        + (f"; {ad.running_days} kundan beri" if ad.running_days is not None else ""),
        f"Sarlavha (headline): {ad.headline or '—'}",
        f"Tugma (CTA): {ad.cta_text or '—'}",
        f"Havola: {ad.link_caption or ad.link_url or '—'}",
        "Asosiy matn (primary text):",
        (ad.primary_text or "(matn yo'q — kreativ rasm/videoda)")[:2500],
    ]
    return "\n".join(lines)


def _parse(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        raise AnalyzeError("AI javobi tushunarsiz bo'ldi -- qayta urinib ko'ring")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise AnalyzeError("AI javobi tushunarsiz bo'ldi -- qayta urinib ko'ring") from e
    return {k: (str(data.get(k)).strip() if data.get(k) not in (None, "") else "aniq emas") for k in FIELDS}


def analyze_ad(ad: Ad, *, company_id=None, force: bool = False, llm=None) -> dict:
    """Qaytaradi: {"analysis": {...}, "cached": bool}. `llm` -- test uchun."""
    cache_key = f"adlib_ai:{ad.id}"
    if not force:
        hit = kv_store.get_json(cache_key)
        if isinstance(hit, dict) and time.time() - float(hit.get("ts", 0)) < CACHE_TTL_SECONDS:
            return {"analysis": hit["analysis"], "cached": True}
    used = usage_today(company_id)
    if used >= daily_limit():
        raise AnalyzeLimitReached(f"Bugungi AI tahlil limiti tugadi ({daily_limit()} ta). Ertaga qayta urinib ko'ring.")
    if llm is None:
        import orchestrator
        llm = orchestrator.call_light
    try:
        raw = llm(SYSTEM_PROMPT, _ad_prompt(ad), max_tokens=700)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "OPENAI_API_KEY" in msg:
            raise AnalyzeError("AI sozlanmagan (OPENAI_API_KEY)") from e
        raise AnalyzeError("AI xizmati javob bermadi -- birozdan keyin qayta urinib ko'ring") from e
    analysis = _parse(raw)
    kv_store.set_json(_usage_key(company_id), used + 1)
    kv_store.set_json(cache_key, {"ts": time.time(), "analysis": analysis})
    return {"analysis": analysis, "cached": False}
