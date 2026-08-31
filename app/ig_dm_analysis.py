"""
ig_dm_analysis.py — Instagram DM suhbatlariga DAVRIY (odatda har 2-3
soatda, `scheduler.job_ig_dm_analysis`) lid-sifat bahosi beradi (2026-08,
foydalanuvchi so'rovi: "ig chatlarni tahlilini ham qoshish kerak, lekin
byudjetni yo'lini top, qimmat bo'p ketmasin").

XARAJATNI NAZORAT QILISH -- IKKI QOIDA:
  1. FAQAT `IgDmConversation.message_count > ai_analyzed_message_count`
     bo'lgan suhbatlar tahlil qilinadi -- ya'ni oxirgi tahlildan beri
     KAMIDA bitta yangi xabar kelgan bo'lishi kerak. O'zgarmagan suhbat
     HAR safar qayta-qayta tahlil qilinib pul isrof qilinmaydi.
  2. Har bir tahlilga butun tarix EMAS, faqat oxirgi
     `ig_dm_analysis_lookback_messages` (business_rules.json, standart 30)
     xabar yuboriladi.

MODEL: `call_analysis.OPENAI_ANALYSIS_MODEL` (standart `gpt-4o-mini`) --
ATAYLAB xuddi qo'ng'iroq-tahlili bilan BIR XIL, arzon model. OpenAI so'rov
infratuzilmasi (retry/backoff, "kredit tugagan" aniqlash) ham
`call_analysis.py`dan QAYTA ISHLATILADI -- ikkinchi marta yozilmaydi
(bitta joyda tuzatilsa, ikkalasida ham ishlaydi)."""

import json
import logging
import os
import datetime as dt
from pathlib import Path

from call_analysis import (
    _openai_request,
    _extract_openai_error,
    _extract_responses_output_text,
    _is_quota_exhausted_response,
    OpenAICreditExhaustedError,
    OPENAI_ANALYSIS_MODEL,
)
from db import get_session, IgDmConversation, IgDmMessage

logger = logging.getLogger("ig_dm_analysis")

BASE_DIR = Path(__file__).resolve().parent
_business_rules = json.loads((BASE_DIR / "business_rules.json").read_text(encoding="utf-8"))
LOOKBACK_MESSAGES = _business_rules.get("ig_dm_analysis_lookback_messages", 30)

_SYSTEM_PROMPT = """Sen Instagram Direct (DM) yozishmalarini tahlil qiluvchi
yordamchisan. Senga BIZNES (Menejer) bilan MIJOZ orasidagi yozishma
beriladi. Vazifang:

1. `leadQuality` -- shu mijoz QANCHALIK haqiqiy xaridorga o'xshaydi:
   - "hot": aniq xarid/buyurtma niyati bor, narx/yetkazib berish/to'lov
     haqida so'rayapti, yoki xarid qilishga tayyorligini bildirgan.
   - "warm": qiziqish bor (mahsulot/xizmat haqida so'rayapti), lekin
     hali aniq xarid niyati yo'q yoki savoli hal qilinmagan.
   - "cold": shunchaki spam/reklama/aloqasi yo'q savol, yoki juda umumiy
     salomlashuvdan nariga o'tmagan yozishma.
2. `summary` -- 1-2 gapda: mijoz nima so'rayapti/xohlaydi (o'zbek tilida).
3. `reasons` -- bahoingga 1-3 ta qisqa, aniq sabab (o'zbek tilida).

FAQAT berilgan matnga tayanib xulosa chiqar -- hech narsani "to'qib
chiqarma". Agar yozishma juda qisqa/noaniq bo'lsa, "cold" deb baholab,
sababda buni aniq yoz."""

_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "leadQuality": {"type": "string", "enum": ["hot", "warm", "cold"]},
        "summary": {"type": "string"},
        "reasons": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["leadQuality", "summary", "reasons"],
    "additionalProperties": False,
}


def _render_conversation(messages: list) -> str:
    lines = []
    for m in messages:
        label = "Mijoz" if m.sender == "customer" else "Menejer"
        lines.append(f"{label}: {m.text or ''}")
    return "\n".join(lines)


def _parse_result_json(raw_text: str) -> dict:
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    data = json.loads(text)
    if "leadQuality" not in data or "summary" not in data:
        raise ValueError("Model javobida 'leadQuality'/'summary' maydonlari yo'q.")
    return data


def analyze_conversation(session, conv: IgDmConversation) -> dict:
    """Bitta `IgDmConversation`ni tahlil qiladi va natijani shu qatorga
    yozadi (session.commit() shu funksiya ichida bajariladi)."""
    messages = (
        session.query(IgDmMessage)
        .filter_by(conversation_id=conv.id)
        .order_by(IgDmMessage.sent_at.asc())
        .all()
    )
    if not messages:
        raise ValueError("Bu suhbatda hali xabar yo'q -- tahlil qilib bo'lmaydi.")

    recent = messages[-LOOKBACK_MESSAGES:]
    conversation_text = _render_conversation(recent)
    api_key = os.environ.get("OPENAI_API_KEY")

    resp = _openai_request(
        "POST", "https://api.openai.com/v1/responses",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        json_body={
            "model": OPENAI_ANALYSIS_MODEL,
            "temperature": 0,
            "input": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"Instagram DM yozishmasi (eng eskisi tepada):\n\n{conversation_text}"},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "ig_dm_analysis",
                    "schema": _JSON_SCHEMA,
                    "strict": True,
                },
            },
        },
        timeout=60,
    )
    if not resp.ok:
        if _is_quota_exhausted_response(resp):
            raise OpenAICreditExhaustedError(
                "OpenAI balansi/krediti tugagan -- Instagram DM tahlili vaqtincha ishlamaydi. "
                "Administrator OpenAI hisobini to'ldirishi kerak, keyin navbatdagi tsiklda avtomatik davom etadi."
            )
        err_msg = _extract_openai_error(resp)
        raise RuntimeError(f"OpenAI tahlil xatosi (model={OPENAI_ANALYSIS_MODEL}, HTTP {resp.status_code}): {err_msg}")

    raw_text = _extract_responses_output_text(resp.json())
    data = _parse_result_json(raw_text)

    conv.ai_lead_quality = data["leadQuality"]
    conv.ai_summary = data["summary"]
    conv.ai_reasons = json.dumps(data.get("reasons") or [], ensure_ascii=False)
    conv.ai_analyzed_message_count = len(messages)
    conv.ai_analyzed_at = dt.datetime.utcnow()
    conv.ai_model = OPENAI_ANALYSIS_MODEL
    conv.ai_error = None
    session.commit()
    return data


def analyze_pending_conversations(limit: int = 20) -> dict:
    """FAQAT oxirgi tahlildan beri yangi xabar kelgan suhbatlarni (eng
    yangisidan boshlab, `limit` tagacha) tahlil qiladi -- bitta chaqiruvda
    hammasi emas, shunda bitta katta OpenAI to'xtalishi butun navbatni
    ushlab qolmaydi (keyingi tsiklda davom etadi)."""
    result = {"analyzed": 0, "skipped_no_openai_key": False, "errors": []}
    if not os.environ.get("OPENAI_API_KEY"):
        result["skipped_no_openai_key"] = True
        return result

    session = get_session()
    try:
        pending = (
            session.query(IgDmConversation)
            .filter(IgDmConversation.message_count > IgDmConversation.ai_analyzed_message_count)
            .order_by(IgDmConversation.last_message_at.desc())
            .limit(limit)
            .all()
        )
        for conv in pending:
            try:
                analyze_conversation(session, conv)
                result["analyzed"] += 1
            except OpenAICreditExhaustedError as e:
                logger.error("IG DM tahlili: OpenAI krediti tugagan -- navbat to'xtatildi.")
                conv.ai_error = str(e)[:2000]
                session.commit()
                result["errors"].append(f"Suhbat #{conv.id}: {e}")
                break  # kredit tugagan -- qolgan suhbatlarni urinish ham behuda
            except Exception as e:
                logger.exception("IG DM suhbat #%s tahlilida kutilmagan xato", conv.id)
                conv.ai_error = str(e)[:2000]
                session.commit()
                result["errors"].append(f"Suhbat #{conv.id}: {e}")
    finally:
        session.close()
    return result
