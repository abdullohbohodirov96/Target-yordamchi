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

2026-08, IKKINCHI VERSIYA (arxitektura o'zgardi): dastlab audioni
to'g'ridan-to'g'ri (transkripsiya xizmatisiz) "tushunadigan" OpenAI'ning
audio-preview chat modellariga (`gpt-4o-audio-preview` va h.k.) yuborilar
edi -- lekin bu OpenAI akkaunti/tarif UCHUN BARCHA shunday model nomlari
404 (model_not_found/access) qaytardi (production loglarida tasdiqlandi --
bir nechta nomzod nom sinalgan bo'lsa ham hammasi rad etildi). Bu odatda
audio-preview modellar OpenAI tomonidan "Verify Organization" qilingan
akkauntlargagina ochiq bo'lgani uchun.

Shuning uchun ENDI IKKI BOSQICHLI yondashuv ishlatiladi (ancha keng
mavjud, kamroq cheklangan API'lar orqali):
  1) Audio -> matn: OpenAI'ning transkripsiya endpointi
     (`/v1/audio/transcriptions`, `whisper-1` -- bu KO'PDAN BERI hamma
     akkauntlarga ochiq, maxsus ruxsat talab qilmaydi).
  2) Matn -> tahlil: oddiy matnli chat completions (`OPENAI_MODEL`,
     standart `gpt-4o-mini`) -- bu ANIQ SHU akkauntda allaqachon
     `orchestrator.py` orqali muvaffaqiyatli ishlatilib turibdi (Telegram
     bot, kunlik hisobotlar va h.k.), demak ishonchli ishlaydi.
Kamchiligi: `whisper-1` gapiruvchilarni (diarization) ALOHIDA
ajratmaydi -- shuning uchun Manager/Mijoz ajratish endi 2-bosqichdagi matn
modeliga TAXMIN sifatida topshiriladi (suhbat mazmuniga qarab, masalan
savol beruvchi odatda Manager). Bu 100% aniq bo'lmasligi mumkin, lekin
baho/xulosa/status kabi asosiy natijalar buzilmaydi.

2026-08, UCHINCHI VERSIYA (ixtiyoriy yaxshilash qo'shildi): foydalanuvchi
OpenAI'ning `gpt-4o-transcribe-diarize` modelini taklif qildi -- bu HAQIQIY
diarizatsiya qiladi (kim qachon gapirganini vaqt bo'yicha ajratadi,
taxminsiz). Shuning uchun ENDI avval shu model bilan sinab ko'riladi
(`_try_diarized_transcription()`) -- muvaffaqiyatli bo'lsa, natija
to'g'ridan-to'g'ri "Manager: ...\\nMijoz: ..." formatiga aylantiriladi
(birinchi gapirgan tomon -- odatda qo'ng'iroqqa javob bergan -- Manager
deb belgilanadi). MUHIM: bu OpenAI account/tarif uchun MAVJUD BO'LMASLIGI
MUMKIN (xuddi audio-preview modellar kabi) -- shuning uchun BUTUNLAY
IXTIYORIY qatlam sifatida qo'shilgan: islamasa/404/xato bersa, JIM
(xatosiz) eski `whisper-1` orqali oddiy transkripsiya + matn modeliga
taxmin qildirish yo'liga qaytiladi. Hech qanday holatda butun tahlil shu
modelga BOG'LIQ emas.
"""

import os
import re
import json
import logging
import datetime as dt

import requests

logger = logging.getLogger("call_analysis")

# 2026-08, TRANSKRIPSIYA (audio -> matn) bosqichi uchun modellar. `whisper-1`
# birinchi -- bu ENG KENG mavjud (yillar davomida barcha OpenAI akkauntlariga
# ochiq bo'lgan), keyingi nomzodlar yangiroq/sifatliroq bo'lishi mumkin,
# lekin ba'zi akkauntlarda mavjud bo'lmasligi mumkin.
_TRANSCRIBE_MODEL_CANDIDATES = ["whisper-1", "gpt-4o-mini-transcribe", "gpt-4o-transcribe"]
_working_transcribe_config = None  # (model, include_language) -- bir marta ishlagan kombinatsiya shu yerda keshlanadi

# TAHLIL (matn -> JSON natija) bosqichi -- ANIQ SHU akkauntda allaqachon
# ishlatilib turgan oddiy matn modeli (orchestrator.py bilan bir xil
# standart qiymat, ataylab -- u yerda ishlagani uchun bu yerda ham
# ishlashi deyarli kafolatlangan).
OPENAI_ANALYSIS_MODEL = os.environ.get("OPENAI_ANALYSIS_MODEL", os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))

# 2026-08, foydalanuvchi tomonidan berilgan audio-tahlil prompti asosida --
# audio o'rniga TAYYOR TRANSKRIPSIYA matni beriladigan qilib moslashtirilgan
# (baholash mezonlari, JSON formati va boshqa barcha qoidalar SO'ZMA-SO'Z
# saqlangan, faqat kirish turi va gapiruvchi-ajratish qismi o'zgartirilgan).
TEXT_SYSTEM_PROMPT = """Sen professional savdo va mijoz bilan suhbatlarni tahlil qiluvchi AI assistentsan.

Senga qo'ng'iroq suhbatining TAYYOR TRANSKRIPSIYASI (matn ko'rinishida, avtomatik nutqni-matnga aylantirish xizmati orqali olingan) beriladi. Matn asosan o'zbek tilida bo'lishi mumkin, lekin ruscha, inglizcha yoki boshqa so'zlar aralashishi mumkin. Transkripsiyada gapiruvchilar ALOHIDA ko'rsatilmagan bo'lishi mumkin (faqat uzluksiz matn) — bunday holda kim qachon gapirganini SUHBAT MAZMUNIGA qarab (masalan, savol beruvchi/taklif qiluvchi odatda Manager, narx/mahsulot so'ragan odatda Mijoz) TAXMIN qilib ajratishga harakat qil.

VAZIFANG:

1. Berilgan transkripsiya matnini Manager/Mijoz bo'yicha imkon qadar ajratib qayta yoz.
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

"transcription" ichida berilgan matnni Manager/Mijoz yorliqlari bilan qayta formatlab to'liq yoz.

Format:

Manager: Assalomu alaykum…
Mijoz: Vaalaykum assalom…
Manager: …

So'zlarni o'zingdan qo'shma yoki o'zgartirma — faqat berilgan matnni qayta formatlash va tahlil qilish kerak.

Agar biror qism tushunarsiz/uzilgan bo'lsa:

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
• Transkripsiyani qisqartirma — berilgan matnning barcha mazmunini saqla.
• Overview qisqa bo'lsin.
• Berilgan transkripsiyada yo'q ma'lumotlarni o'zingdan qo'shib chiqarma."""

_REQUIRED_KEYS = ("overview", "score", "status", "color", "result", "transcription")


def is_configured() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def _guess_audio_format(url: str, content_type: str | None) -> str:
    ct = (content_type or "").lower()
    if "wav" in ct or url.lower().endswith(".wav"):
        return "wav"
    if "ogg" in ct or url.lower().endswith(".ogg"):
        return "ogg"
    if "m4a" in ct or url.lower().endswith(".m4a"):
        return "m4a"
    return "mp3"


def _detect_magic_format(data: bytes) -> str | None:
    """Faylning BOSHIDAGI "magic bytes"iga qarab formatni aniqlaydi.
    Hech narsa mos kelmasa `None` qaytaradi (ya'ni bu ehtimol umuman
    audio EMAS -- masalan xato sahifasi)."""
    head = data[:16]
    if head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return "mp3"
    if head[:4] == b"RIFF":
        return "wav"
    if head[:4] == b"OggS":
        return "ogg"
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return "m4a"
    return None


def _sniff_audio_format(data: bytes, content_type: str | None, url: str) -> str:
    """Fayl KENGAYTMASI/Content-Type'ga emas, fayl BOSHIDAGI "magic bytes"ga
    qarab haqiqiy formatni aniqlaydi. 2026-08: transkripsiya "gibberish"
    (ma'nosiz so'zlar) chiqarayotgani aniqlandi -- buning bir sababi format
    NOTO'G'RI taxmin qilinib, OpenAI'ga masalan WAV fayl "mp3" deb
    yuborilishi bo'lishi mumkin edi (bunda model xato dekodlangan audio
    "shovqinini" so'zlarga aylantirishga urinadi). Bu funksiya faylning
    o'zidan (headerdan) haqiqiy formatni o'qiydi -- ancha ishonchli."""
    return _detect_magic_format(data) or _guess_audio_format(url, content_type)


def _download_audio(url: str) -> tuple[bytes, str]:
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    data = resp.content
    content_type = resp.headers.get("Content-Type", "")

    # 2026-08: tahlil hali ham ma'nosiz ("gibberish") matn chiqarayotgani
    # haqida shikoyat kelgach -- bir ehtimol tekshirildi: balki `recording_url`
    # dan AUDIO EMAS, xato sahifasi (HTML/JSON) tushib qolayotgandir (havola
    # muddati o'tgan yoki noto'g'ri bo'lsa ham server ko'pincha HTTP 200
    # bilan "xato" sahifasini qaytaradi -- `raise_for_status()` buni
    # ushlamaydi). Bunday holda Whisper "audio" sifatida matnni dekodlashga
    # urinib, albatta ma'nosiz chiqindi beradi. Shuning uchun ENDI yuklab
    # olingan fayl HAQIQATDA audio ko'rinishiga ega ekanini aniq tekshiramiz.
    if len(data) < 500:
        raise RuntimeError(
            f"Yuklab olingan yozuv fayli juda kichik ({len(data)} bayt, Content-Type: "
            f"{content_type or 'nomaʼlum'}) -- bu haqiqiy audio emasligi mumkin "
            "(havola muddati o'tgan yoki yozuv hali tayyor bo'lmagan bo'lishi mumkin)."
        )
    if _detect_magic_format(data) is None:
        head = data[:60]
        looks_textual = bool(head) and all(32 <= b < 127 or b in (9, 10, 13) for b in head[:20])
        if looks_textual:
            preview = head.decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Yozuv havolasidan (recording_url) audio o'rniga matn/HTML qaytdi "
                f"(Content-Type: {content_type or 'nomaʼlum'}) -- boshlanishi: {preview!r}. "
                "Havola muddati o'tgan yoki noto'g'ri bo'lishi mumkin."
            )
        logger.warning(
            "Yozuv fayli tanish magic-byte'larga mos kelmadi (Content-Type: %s, boshi: %r) -- "
            "baribir yuborilmoqda.", content_type, data[:16],
        )

    audio_format = _sniff_audio_format(data, content_type, url)
    return data, audio_format


def _extract_openai_error(resp) -> str:
    """OpenAI xato javobidan ANIQ sabab matnini chiqarib olishga harakat
    qiladi (masalan "The model `...` does not exist or you do not have
    access to it.") -- shunda `ai_error` ustunida generik "404 Client
    Error" o'rniga aynan nima noto'g'riligi ko'rinadi."""
    try:
        body = resp.json()
        msg = (body.get("error") or {}).get("message")
        if msg:
            return msg
    except Exception:
        pass
    return (resp.text or "")[:300] or f"HTTP {resp.status_code}"


def _post_transcription(api_key: str, model: str, audio_bytes: bytes, audio_format: str, include_language: bool = True):
    data = {
        "model": model,
        "response_format": "text",
        # 2026-08: `language="uz"` bu endpoint uchun rad etilgani sababli
        # (pastga qarang), tilni "prompt" orqali bilvosita bildirishga
        # majburmiz. MUHIM: Whisper "prompt"ni TAVSIF sifatida emas, "audio
        # DAVOMI" sifatida talqin qiladi -- shuning uchun "bu o'zbekcha
        # suhbat" kabi TASHQI tavsif deyarli foydasiz, LEKIN haqiqiy
        # o'zbekcha DIALOG NAMUNASI berish tilni va uslubni kuchli tarzda
        # "tortadi" (OpenAI'ning o'z tavsiyasi). Shuning uchun bu yerda
        # aynan shu ko'rinishdagi namuna ishlatilgan.
        "prompt": (
            "Manager: Assalomu alaykum, xush kelibsiz, sizga qanday yordam bera olaman? "
            "Mijoz: Vaalaykum assalom, menga mahsulot narxi va yetkazib berish haqida "
            "ma'lumot kerak edi."
        ),
        # Hallyusinatsiya (ma'nosiz so'z to'qish) ehtimolini kamaytirish
        # uchun eng "ishonchli"/deterministik dekodlashni so'raymiz.
        "temperature": "0",
    }
    if include_language:
        # 2026-08: tilni ANIQ ko'rsatmasak, Whisper ba'zan noto'g'ri tilni
        # avtomatik aniqlab, natijada butunlay ma'nosiz ("gibberish") matn
        # chiqarib beradi -- shuning uchun avval `language="uz"` bilan
        # sinaladi. LEKIN production'da bu account/endpoint uchun aynan
        # "uz" qiymati HTTP 400 ("Language 'uz' is not supported.") bilan
        # rad etilishi aniqlandi -- shuning uchun bu parametr FAQAT birinchi
        # urinishda qo'shiladi, rad etilsa (`_transcribe_audio` pastda)
        # DARHOL shu parametrsiz qayta so'raladi.
        data["language"] = "uz"
    return requests.post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {api_key}"},
        files={"file": (f"audio.{audio_format}", audio_bytes, f"audio/{audio_format}")},
        data=data,
        # 2026-08: 180s'dan 90ga tushirildi -- endi bu fon oqimida (thread)
        # ishlaydi (gunicorn HTTP timeout'iga bog'liq emas), lekin har bir
        # urinish baribir CHEKLANGAN bo'lishi kerak (bir nechta model/til
        # kombinatsiyasi ketma-ket sinalganda, har biri 180s kutsa, bitta
        # qo'ng'iroq o'zi 10+ daqiqaga cho'zilib ketishi mumkin edi).
        timeout=90,
    )


def _transcribe_audio(audio_bytes: bytes, audio_format: str) -> str:
    """Audio -> matn (1-bosqich). 2026-08: foydalanuvchi transkripsiya hali
    ham noto'g'ri (ingliz/arab tilida "gibberish") chiqayotganini xabar
    qildi -- muammo shunda ediki, "language='uz'" biror model uchun rad
    etilishi bilanoq, HAMMA keyingi urinishlar (shu jumladan boshqa
    model nomzodlari) uchun ham darhol tilsiz (avtomatik aniqlashga
    tayanib) so'ralar edi -- garchi BOSHQA model "uz"ni qabul qilishi
    mumkin bo'lsa ham. Endi ikki BOSQICHLI qidiruv qilinadi: 1) barcha
    nomzodlarga `language="uz"` bilan (eng ishonchli -- avtomatik
    aniqlashga umuman ehtiyoj yo'q); shu birontasi ham qabul qilmasa,
    2) barcha nomzodlarga tilsiz (faqat dialog-namunali `prompt` hint
    bilan). Topilgan ishlaydigan (model, til-bormi) kombinatsiyasi
    keshlanadi -- keyingi chaqiruvda AVVAL o'sha yo'l sinaladi."""
    global _working_transcribe_config
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY sozlanmagan -- qo'ng'iroq tahlili ishlamaydi.")

    env_override = os.environ.get("OPENAI_TRANSCRIBE_MODEL")
    candidates = []
    for m in [env_override] + _TRANSCRIBE_MODEL_CANDIDATES:
        if m and m not in candidates:
            candidates.append(m)

    attempts = []

    def _attempt(model: str, include_language: bool):
        resp = _post_transcription(api_key, model, audio_bytes, audio_format, include_language=include_language)
        if resp.status_code == 400 and include_language and "language" in _extract_openai_error(resp).lower():
            return None  # bu kombinatsiya rad etildi -- chaqiruvchi keyingisiga o'tadi
        if resp.status_code == 404:
            err_msg = _extract_openai_error(resp)
            attempts.append(f"{model} (til={'ha' if include_language else 'yoʻq'}): {err_msg}")
            logger.warning("OpenAI transkripsiya modeli '%s' topilmadi (404): %s", model, err_msg)
            return None
        if not resp.ok:
            err_msg = _extract_openai_error(resp)
            raise RuntimeError(f"OpenAI transkripsiya xatosi (model={model}, HTTP {resp.status_code}): {err_msg}")
        text = (resp.text or "").strip()
        if not text:
            attempts.append(f"{model} (til={'ha' if include_language else 'yoʻq'}): bo'sh transkripsiya qaytardi")
            return None
        return text

    # Tezkor yo'l -- avval oxirgi marta ishlagan kombinatsiya bilan sinaymiz.
    if _working_transcribe_config:
        cached_model, cached_lang = _working_transcribe_config
        text = _attempt(cached_model, cached_lang)
        if text:
            return text
        _working_transcribe_config = None  # bu safar ishlamadi -- to'liq qidiruvga o'tamiz

    # 1-bosqich: HAR BIR nomzodda `language="uz"`ni majburlashga harakat --
    # bu autodetect xatosi ehtimolini butunlay yo'qotadi.
    for model in candidates:
        text = _attempt(model, True)
        if text:
            _working_transcribe_config = (model, True)
            return text

    # 2-bosqich: hech qaysi model "uz"ni qabul qilmadi -- tilsiz (faqat
    # dialog-namunali `prompt` orqali kontekst hint bilan) sinaymiz.
    for model in candidates:
        text = _attempt(model, False)
        if text:
            _working_transcribe_config = (model, False)
            return text

    raise RuntimeError(
        "OpenAI'da ishlaydigan transkripsiya kombinatsiyasi topilmadi. Sinalgan: "
        + "; ".join(attempts)
    )


def _post_diarized_transcription(api_key: str, model: str, audio_bytes: bytes, audio_format: str):
    return requests.post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {api_key}"},
        files={"file": (f"audio.{audio_format}", audio_bytes, f"audio/{audio_format}")},
        data={"model": model, "response_format": "diarized_json", "chunking_strategy": "auto"},
        timeout=90,
    )


def _build_labeled_transcript_from_segments(segments: list) -> str | None:
    """`diarized_json` javobidagi `segments` ro'yxatini (har biri
    {"speaker": "A"/"B"/..., "text": "..."}) "Manager: ...\\nMijoz: ...\\n"
    formatiga aylantiradi. TAXMIN: qo'ng'iroqqa BIRINCHI javob bergan
    (birinchi gapirgan) tomon -- odatda "Assalomu alaykum, ... firmasidan"
    kabi qabul qilish gapini aytadigan -- Manager deb belgilanadi, undan
    keyin paydo bo'lgan har qanday boshqa gapiruvchi Mijoz deb olinadi.
    Bu ko'pchilik chiquvchi/kiruvchi savdo qo'ng'iroqlari uchun to'g'ri
    (2 tomonlik suhbat), lekin 100% kafolat emas."""
    if not segments:
        return None
    speaker_order = []
    lines = []
    for seg in segments:
        raw_speaker = str(seg.get("speaker", "")).strip() or "?"
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        if raw_speaker not in speaker_order:
            speaker_order.append(raw_speaker)
        label = "Manager" if speaker_order.index(raw_speaker) == 0 else "Mijoz"
        lines.append(f"{label}: {text}")
    return "\n".join(lines) if lines else None


def _try_diarized_transcription(audio_bytes: bytes, audio_format: str) -> str | None:
    """IXTIYORIY qatlam -- HAQIQIY (taxminsiz) gapiruvchi ajratish uchun
    `gpt-4o-transcribe-diarize` bilan sinab ko'radi. Bu model bu OpenAI
    akkauntida MAVJUD BO'LMASLIGI MUMKIN (audio-preview modellar kabi) --
    shuning uchun har qanday xatoda (404, boshqa HTTP xato, tarmoq xatosi,
    kutilmagan javob formati) `None` qaytaradi va CHAQIRUVCHI FUNKSIYA
    (`analyze_call_record`) muqobil sifatida eski `_transcribe_audio()`
    yo'liga o'tadi -- bu qatlam butun tahlilni TO'XTATA OLMAYDI."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    model = os.environ.get("OPENAI_DIARIZE_MODEL", "gpt-4o-transcribe-diarize")
    try:
        resp = _post_diarized_transcription(api_key, model, audio_bytes, audio_format)
    except requests.RequestException as e:
        logger.warning("Diarizatsiya so'rovi (model=%s) tarmoq xatosi bilan tugadi: %s", model, e)
        return None
    if not resp.ok:
        err_msg = _extract_openai_error(resp)
        logger.warning(
            "Diarizatsiya modeli '%s' ishlamadi (HTTP %s): %s -- oddiy transkripsiya yo'liga o'tilmoqda.",
            model, resp.status_code, err_msg,
        )
        return None
    try:
        segments = resp.json().get("segments") or []
    except Exception:
        logger.warning("Diarizatsiya javobini JSON qilib o'qib bo'lmadi -- oddiy transkripsiya yo'liga o'tilmoqda.")
        return None
    return _build_labeled_transcript_from_segments(segments)


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


_TURN_RE = re.compile(
    r"(?m)^(Manager|Mijoz|Speaker\s*1|Speaker\s*2)\s*:\s*"
    r"(.*(?:\n(?!(?:Manager|Mijoz|Speaker\s*1|Speaker\s*2)\s*:).*)*)"
)


def parse_transcript_turns(text: str) -> list:
    """2026-08, foydalanuvchi so'rovi -- transkripsiyani "SMS suhbat"
    ko'rinishida (Manager bir tomonda, Mijoz boshqa tomonda, gap-bo'lib-gap
    pufakchalar bilan) chiqarish uchun, model qaytargan "Manager: ...\\n
    Mijoz: ...\\n" formatidagi matnni {"speaker", "raw_label", "text"}
    lug'atlar ro'yxatiga ajratadi. `app.py`dagi `_build_ai_analysis_view()`
    shu funksiyani chaqirib, natijani shablonga (`individual_check.html`)
    uzatadi."""
    if not text:
        return []
    turns = []
    for m in _TURN_RE.finditer(text):
        speaker_raw = m.group(1).strip()
        body = m.group(2).strip()
        if not body:
            continue
        low = speaker_raw.lower()
        if low.startswith("manager"):
            speaker = "manager"
        elif low.startswith("mijoz"):
            speaker = "mijoz"
        else:
            speaker = "unknown"
        turns.append({"speaker": speaker, "raw_label": speaker_raw, "text": body})
    if not turns:
        # Model kutilgan "Manager:/Mijoz:" formatiga amal qilmagan bo'lsa --
        # xom matnni bitta "unknown" bo'lak sifatida qaytaramiz (shablon
        # baribir buni ko'rsata oladi, faqat ikki tomonga ajratmasdan).
        turns = [{"speaker": "unknown", "raw_label": "", "text": text.strip()}]
    return turns


def _analyze_transcript(transcript_text: str) -> dict:
    """Matn -> tahlil (2-bosqich). Oddiy matnli chat completions -- shu
    akkauntda allaqachon ishonchli ishlab turgan model (`OPENAI_ANALYSIS_MODEL`,
    standart -- `orchestrator.py`dagi `OPENAI_MODEL` bilan bir xil)."""
    api_key = os.environ.get("OPENAI_API_KEY")
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        json={
            "model": OPENAI_ANALYSIS_MODEL,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": TEXT_SYSTEM_PROMPT},
                {"role": "user", "content": f"Transkripsiya matni:\n\n{transcript_text}"},
            ],
        },
        timeout=90,
    )
    if not resp.ok:
        err_msg = _extract_openai_error(resp)
        raise RuntimeError(f"OpenAI tahlil xatosi (model={OPENAI_ANALYSIS_MODEL}, HTTP {resp.status_code}): {err_msg}")
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    return _parse_json_response(content)


def analyze_call_record(session, call) -> dict:
    """Bitta `db.CallRecord` yozuvini tahlil qiladi va natijani SHU
    yozuvning `ai_*` ustunlariga saqlaydi (`session.commit()` chaqiruvchi
    tomonidan emas, shu yerning o'zida qilinadi). Xato bo'lsa -- `ai_error`ga
    yoziladi va `ai_analyzed_at` baribir belgilanadi (aks holda buzuq/
    yetib bo'lmaydigan yozuv har safar qayta-qayta urinib, behuda API
    xarajatiga olib kelaveradi -- `run_pending_analysis()` xatolik bilan
    tugagan yozuvlarni baribir qayta ko'rib chiqadi, quyida qarang)."""
    now = dt.datetime.utcnow()
    if not call.recording_url:
        raise ValueError("Bu qo'ng'iroqda yozuv (recording_url) yo'q.")
    try:
        audio_bytes, audio_format = _download_audio(call.recording_url)
        # Avval HAQIQIY diarizatsiya (taxminsiz gapiruvchi ajratish) bilan
        # sinab ko'ramiz; mavjud bo'lmasa/xato bersa (None qaytadi) --
        # oddiy transkripsiya + matn modeliga taxmin qildirish yo'liga
        # o'tamiz (eski, ishlab turgan yo'l).
        transcript_text = _try_diarized_transcription(audio_bytes, audio_format)
        if not transcript_text:
            transcript_text = _transcribe_audio(audio_bytes, audio_format)
        result = _analyze_transcript(transcript_text)
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
