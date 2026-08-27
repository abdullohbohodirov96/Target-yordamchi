"""
call_analysis.py — qo'ng'iroq yozuvlarini (Moi Zvonki `recording_url`) AI
yordamida TAHLIL qiladi: to'liq transkripsiya (Manager/Mijoz ajratilgan
holda), suhbat qanday o'tgani haqida qisqa xulosa, va 1-10 baho (rang
bilan: qizil/sariq/yashil).

2026-08, foydalanuvchi so'rovi bilan qo'shildi -- foydalanuvchi tayyor
audio-tahlil promptini berdi, tanlovi bo'yicha bu AVTOMATIK ishlaydi
(har bir yangi "haqiqiy" -- ya'ni `call_analytics.get_min_real_talk_seconds()`
chegarasidan uzunroq -- qo'ng'iroq yozuvi fon jarayonda o'zi tahlil
qilinadi, `scheduler.py`dagi davriy vazifa orqali).

NEGA OpenAI (Claude EMAS): audio faylni to'g'ridan-to'g'ri (transkripsiya
xizmatisiz) tushunadigan modellar hozircha faqat OpenAI'ning audio-preview
oilasida (`gpt-4o-mini-audio-preview` va h.k.) -- Anthropic Messages API
hali audio kirishni qo'llab-quvvatlamaydi. Shuning uchun bu modul
`orchestrator.py`dagi `OPENAI_API_KEY`dan alohida emas, xuddi shu kalitdan
foydalanadi (allaqachon sozlangan bo'lishi kerak).
"""

import os
import json
import base64
import logging
import datetime as dt

import requests

logger = logging.getLogger("call_analysis")

# 2026-08, foydalanuvchi tomonidan berilgan audio-tahlil prompti -- SO'ZMA-SO'Z
# saqlanadi (o'zgartirilmagan), faqat shu modelga system prompt sifatida
# yuboriladi.
SYSTEM_PROMPT = """Sen professional savdo va mijoz bilan suhbatlarni tahlil qiluvchi AI assistentsan.

Senga audio suhbat beriladi. Audio asosan o'zbek tilida bo'lishi mumkin, lekin ruscha, inglizcha yoki boshqa so'zlar aralashishi mumkin.

VAZIFANG:

1. Audioni to'liq va aniq transkripsiya qil.
2. Gapiruvchilarni imkon qadar ajrat:
 • Manager
 • Mijoz
3. Suhbatning mazmunini tushun.
4. Suhbat qanday o'tganini juda qisqa va mazmunli tarzda yoz.
5. Suhbat sifatiga 1 dan 10 gacha umumiy baho ber.
6. Natijani faqat quyidagi JSON formatida qaytar.

BAHOLASH:

1-3 = qizil — suhbat yomon o'tgan
4-6 = sariq — o'rtacha, yaxshilash kerak
7-10 = yashil — yaxshi suhbat

Baholashda quyidagilarga e'tibor ber:

• Manager mijozni tushundimi?
• To'g'ri savollar berdimi?
• Mijozning ehtiyojini aniqladimi?
• Aniq va tushunarli javob berdimi?
• Mijoz bilan professional gaplashdimi?
• Suhbatni keyingi qadamga olib bordimi?
• Mijozda qiziqish yoki sotib olish ehtimoli paydo bo'ldimi?
• Keraksiz uzun yoki mavzudan tashqari gaplar bo'ldimi?
• Mijozning savollari javobsiz qolmadimi?

OVERVIEW QOIDASI:

"overview" maksimal 2-3 ta qisqa gapdan iborat bo'lsin.

Faqat eng muhim narsalarni ayt:

• mijoz nima uchun murojaat qildi;
• suhbat qanday o'tdi;
• qanday natija bilan tugadi.

Masalan:

"Mijoz mahsulot narxi va yetkazib berish bo'yicha murojaat qildi. Manager savollarga javob berdi, lekin mijoz ehtiyojini to'liq aniqlamadi. Suhbat aniq keyingi qadamsiz tugadi."

TRANSKRIPSIYA:

"transcription" ichida suhbatni to'liq yoz.

Format:

Manager: Assalomu alaykum…
Mijoz: Vaalaykum assalom…
Manager: …

So'zlarni o'zingdan qo'shma.

Agar biror qism tushunarsiz bo'lsa:

[tushunarsiz]

deb yoz.

Agar gapiruvchini aniqlab bo'lmasa:

Speaker 1:
Speaker 2:

formatidan foydalan.

JSON OUTPUT:

{
"overview": "Suhbatning juda qisqa mazmunli xulosasi.",
"score": 8,
"status": "good",
"color": "green",
"result": "Mijoz qiziqish bildirdi va keyingi aloqa uchun kelishildi.",
"transcription": "Manager: …\\nMijoz: …"
}

STATUS VA COLOR:

Agar score 1-3 bo'lsa:
"status": "bad"
"color": "red"

Agar score 4-6 bo'lsa:
"status": "average"
"color": "yellow"

Agar score 7-10 bo'lsa:
"status": "good"
"color": "green"

MUHIM:

• JSON'dan tashqarida hech qanday matn yozma.
• Markdown ishlatma.
• Score faqat butun son bo'lsin.
• Transkripsiyani qisqartirma.
• Overview qisqa bo'lsin.
• Audio uzun bo'lsa ham to'liq suhbatni transkripsiya qil.
• Audio ichidagi ma'lumotlarni taxmin qilma."""

OPENAI_AUDIO_MODEL = os.environ.get("OPENAI_AUDIO_MODEL", "gpt-4o-audio-preview")
# 2026-08: production'da `gpt-4o-mini-audio-preview` OpenAI'dan 404
# (model_not_found) qaytardi -- bu OpenAI akkaunti/tarif uchun mavjud
# bo'lmagan model nomi ekan. Aniq qaysi nom to'g'ri ishlashini bu yerdan
# tekshirib bo'lmagani uchun, ENDI bir nechta nomzod nom ketma-ket
# sinaladi (birinchi ishlagani keyingi chaqiruvlar uchun keshlanadi) --
# shunday qilib qaysi model haqiqatda mavjudligidan qat'i nazar ishlaydi,
# va agar birontasi ham ishlamasa xatolikda OpenAI'ning O'ZI aytgan aniq
# sababi ko'rsatiladi (endi generik "404 Client Error" emas).
_FALLBACK_AUDIO_MODELS = [
    "gpt-4o-audio-preview",
    "gpt-4o-mini-audio-preview",
    "gpt-4o-audio-preview-2024-12-17",
    "gpt-4o-mini-audio-preview-2024-12-17",
    "gpt-4o-audio-preview-2024-10-01",
]
_REQUIRED_KEYS = ("overview", "score", "status", "color", "result", "transcription")
_working_model = None  # bir marta muvaffaqiyatli ishlagan model shu yerda keshlanadi


def is_configured() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def _guess_audio_format(url: str, content_type: str | None) -> str:
    """OpenAI'ning `input_audio` maydoni faqat "mp3" yoki "wav" formatini
    qabul qiladi -- Moi Zvonki odatda mp3 qaytaradi, lekin har ehtimolga
    qarshi content-type/URL kengaytmasidan aniqlashga harakat qilamiz,
    aniqlab bo'lmasa "mp3" standart sifatida ishlatiladi."""
    ct = (content_type or "").lower()
    if "wav" in ct or url.lower().endswith(".wav"):
        return "wav"
    return "mp3"


def _download_audio(url: str) -> tuple[bytes, str]:
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    audio_format = _guess_audio_format(url, resp.headers.get("Content-Type"))
    return resp.content, audio_format


def _parse_json_response(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    data = json.loads(text)
    missing = [k for k in _REQUIRED_KEYS if k not in data]
    if missing:
        raise ValueError(f"Model javobida quyidagi maydonlar yo'q: {missing}")
    # MUHIM: modelga ishonib emas, score'dan status/color'ni o'zimiz ham
    # QAYTA hisoblaymiz -- promptga qat'iy amal qilmasdan status="good" lekin
    # score=3 kabi nomuvofiqlik chiqib qolmasligi uchun (Telegram audit
    # tajribasida ko'p marta uchragan xato turi -- "modelga ishonib emas,
    # xavfsiz tomondan tekshir" qoidasi shu yerda ham qo'llanildi).
    try:
        score = max(1, min(10, int(round(float(data["score"])))))
    except (TypeError, ValueError):
        score = 5
    if score <= 3:
        status, color = "bad", "red"
    elif score <= 6:
        status, color = "average", "yellow"
    else:
        status, color = "good", "green"
    data["score"] = score
    data["status"] = status
    data["color"] = color
    return data


def _extract_openai_error(resp) -> str:
    """OpenAI xato javobidan ANIQ sabab matnini chiqarib olishga harakat
    qiladi (masalan "The model `gpt-4o-mini-audio-preview` does not exist
    or you do not have access to it.") -- shunda `ai_error` ustunida
    generik "404 Client Error" o'rniga aynan nima noto'g'riligi ko'rinadi."""
    try:
        body = resp.json()
        msg = (body.get("error") or {}).get("message")
        if msg:
            return msg
    except Exception:
        pass
    return (resp.text or "")[:300] or f"HTTP {resp.status_code}"


def _post_chat_completion(api_key: str, model: str, b64_audio: str, audio_format: str):
    return requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "modalities": ["text"],
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Quyidagi qo'ng'iroq yozuvini tahlil qil."},
                        {"type": "input_audio", "input_audio": {"data": b64_audio, "format": audio_format}},
                    ],
                },
            ],
        },
        # Audio tahlili odatiy matn so'rovidan SEZILARLI sekinroq (audioni
        # o'zi "eshitib" chiqishi kerak) -- shuning uchun `orchestrator.py`dagi
        # 55s o'rniga ancha uzunroq (bu fon jarayonda ishlaydi, foydalanuvchi
        # brauzerda kutib turmaydi, faqat "hoziroq tahlil qilish" tugmasi
        # bosilganda cheklangan sondagi qo'ng'iroq uchun ishlatiladi).
        timeout=180,
    )


def _call_openai_audio(audio_bytes: bytes, audio_format: str) -> dict:
    global _working_model
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY sozlanmagan -- qo'ng'iroq audio-tahlili ishlamaydi.")
    b64_audio = base64.b64encode(audio_bytes).decode("ascii")

    candidates = []
    if _working_model:
        candidates.append(_working_model)
    for m in [OPENAI_AUDIO_MODEL] + _FALLBACK_AUDIO_MODELS:
        if m and m not in candidates:
            candidates.append(m)

    attempts = []
    for model in candidates:
        resp = _post_chat_completion(api_key, model, b64_audio, audio_format)
        if resp.status_code == 404:
            # Ehtimol model nomi bu OpenAI akkaunti uchun mavjud emas --
            # keyingi nomzodni sinaymiz (auth/rate-limit/boshqa xatolarda
            # ESA darhol to'xtaymiz, chunki model almashtirish yordam
            # bermaydi).
            err_msg = _extract_openai_error(resp)
            attempts.append(f"{model}: {err_msg}")
            logger.warning("OpenAI audio modeli '%s' topilmadi (404): %s", model, err_msg)
            continue
        if not resp.ok:
            err_msg = _extract_openai_error(resp)
            raise RuntimeError(f"OpenAI xatosi (model={model}, HTTP {resp.status_code}): {err_msg}")
        _working_model = model
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return _parse_json_response(content)

    raise RuntimeError(
        "OpenAI'da ishlaydigan audio-tahlil modeli topilmadi. Sinalgan modellar: "
        + "; ".join(attempts)
        + ". OPENAI_AUDIO_MODEL environment variable'ga akkauntingizda mavjud "
        "bo'lgan aniq model nomini qo'shing (OpenAI hisobingizdagi Models "
        "ro'yxatidan tekshiring)."
    )


def analyze_call_record(session, call) -> dict:
    """Bitta `db.CallRecord` yozuvini tahlil qiladi va natijani SHU
    yozuvning `ai_*` ustunlariga saqlaydi (`session.commit()` chaqiruvchi
    tomonidan emas, shu yerning o'zida qilinadi). Xato bo'lsa -- `ai_error`ga
    yoziladi va `ai_analyzed_at` baribir belgilanadi (aks holda buzuq/
    yetib bo'lmaydigan yozuv har safar qayta-qayta urinib, behuda API
    xarajatiga olib kelaveradi)."""
    now = dt.datetime.utcnow()
    if not call.recording_url:
        raise ValueError("Bu qo'ng'iroqda yozuv (recording_url) yo'q.")
    try:
        audio_bytes, audio_format = _download_audio(call.recording_url)
        result = _call_openai_audio(audio_bytes, audio_format)
        call.ai_overview = result["overview"]
        call.ai_score = result["score"]
        call.ai_status = result["status"]
        call.ai_color = result["color"]
        call.ai_result = result["result"]
        call.ai_transcription = result["transcription"]
        call.ai_error = None
        call.ai_analyzed_at = now
        session.commit()
        return result
    except Exception as e:
        logger.exception("Qo'ng'iroq #%s tahlilida xato", call.id)
        call.ai_error = f"{type(e).__name__}: {e}"[:2000]
        call.ai_analyzed_at = now
        session.commit()
        raise


def run_pending_analysis(session, limit: int = 10) -> dict:
    """Hali tahlil qilinmagan, YOZUVI BOR va "haqiqiy" (min_real_talk_seconds
    chegarasidan uzun) qo'ng'iroqlarni topib, birma-bir tahlil qiladi.
    `scheduler.py`dagi davriy vazifa VA admin'ning "hoziroq tahlil qilish"
    tugmasi ikkalasi ham shu funksiyani chaqiradi (turli `limit` bilan).

    2026-08: avval tahlil urinib, XATOLIK bilan tugagan yozuvlar
    (`ai_error IS NOT NULL`) ham qayta ko'rib chiqiladi -- aks holda
    tizimli xato (masalan noto'g'ri model nomi) tuzatilgandan keyin ham
    o'sha yozuvlar abadiy "xatolik"da qolib qolar edi. Hali umuman
    urinilmagan yozuvlarga USTUNLIK beriladi, qolgan joy bo'lsagina
    xatolik bilan tugaganlar qayta sinaladi.

    Qaytaradi: {"analyzed": N, "failed": N, "remaining": N, "retry_remaining": N}."""
    import call_analytics
    from db import CallRecord

    if not is_configured():
        return {"analyzed": 0, "failed": 0, "remaining": 0, "retry_remaining": 0, "error": "OPENAI_API_KEY sozlanmagan"}

    min_seconds = call_analytics.get_min_real_talk_seconds()
    base_filter = (
        CallRecord.recording_url.isnot(None),
        CallRecord.duration_seconds >= min_seconds,
    )
    never_tried_q = session.query(CallRecord).filter(*base_filter, CallRecord.ai_analyzed_at.is_(None))
    retry_q = session.query(CallRecord).filter(*base_filter, CallRecord.ai_error.isnot(None))

    never_tried = never_tried_q.order_by(CallRecord.started_at.desc()).limit(limit).all()
    remaining_slots = max(0, limit - len(never_tried))
    to_retry = (
        retry_q.order_by(CallRecord.started_at.desc()).limit(remaining_slots).all()
        if remaining_slots else []
    )

    analyzed, failed = 0, 0
    for call in never_tried + to_retry:
        try:
            analyze_call_record(session, call)
            analyzed += 1
        except Exception:
            failed += 1  # xato allaqachon analyze_call_record ichida log qilindi/saqlandi

    return {
        "analyzed": analyzed,
        "failed": failed,
        "remaining": never_tried_q.count(),
        "retry_remaining": retry_q.count(),
    }
