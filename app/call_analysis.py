"""call_analysis.py — qo'ng'iroq yozuvlarini (Moi Zvonki `recording_url`) AI
yordamida TAHLIL qiladi: to'liq transkripsiya (Manager/Mijoz ajratilgan
holda), mijoz so'rovi, menejer xatolari, ijobiy tomonlar, savdo natijasi,
va 1-10 baho (rang bilan: qizil/sariq/yashil).

=====================================================================
TARIX (arxitektura qanday o'zgarib kelgani -- keyingi safar kimdir shu
kodni o'qisa, "nega bunday" degan savolga javob bo'lsin deb saqlanmoqda)
=====================================================================
V1: audio to'g'ridan-to'g'ri OpenAI audio-preview chat modellariga
    yuborilardi -- BARCHA shunday model nomlari bu akkaunt uchun 404
    qaytardi (org verification talab qilinadi shekilli).
V2: ikki bosqichli: whisper-1 orqali transkripsiya, keyin oddiy matn
    modeli orqali tahlil. Ishladi, lekin sifat past edi (gibberish,
    "language=uz" 400 xatosi, atamalar noto'g'ri tanilishi va h.k.)
V3: ixtiyoriy gpt-4o-transcribe-diarize qatlami qo'shildi (haqiqiy
    diarizatsiya, taxminsiz).
V4 (2026-08, HOZIRGI, foydalanuvchining to'liq audit-va-qayta-qurish
    so'rovi asosida): quyidagilar qo'shildi/o'zgardi:
  - Transkripsiya modeli ustuvorligi: `gpt-4o-transcribe` ENDI BIRINCHI
    nomzod (avval whisper-1 birinchi edi) -- sifatliroq deb hisoblanadi,
    lekin whisper-1 baribir oxirgi fallback sifatida qoladi (eng keng
    mavjud).
  - Markazlashtirilgan domen lug'ati (`call_glossary.py`) -- endi
    "prompt" hint SHU MODULDAN olinadi, alohida hardcoded matn emas.
  - Audio metadata (kodek, sample rate, davomiylik, kanallar soni)
    ffprobe orqali aniqlanadi -- LEKIN ffprobe/ffmpeg Render'ning
    joriy "python" runtime'ida (Docker emas) KAFOLATLANGAN mavjud EMAS.
    Shuning uchun BUTUNLAY `shutil.which()` bilan himoyalangan: mavjud
    bo'lmasa, bu qadam JIM o'tkazib yuboriladi (butun tahlil to'xtamaydi).
  - Stereo (2 kanalli) yozuvlar uchun -- agar ffmpeg mavjud bo'lsa VA
    yozuv 2 kanalli bo'lsa -- kanallar ALOHIDA-ALOHIDA transkripsiya
    qilinadi (kanal 0 = odatda operator/Manager liniyasi, kanal 1 =
    mijoz liniyasi -- bu YOZUV TIZIMINING konvensiyasi, `CALL_OPERATOR_CHANNEL`
    orqali sozlanadi) va vaqt tamg'asi bo'yicha xronologik birlashtiriladi.
    Bu DIARIZATSIYADAN KO'RA ISHONCHLIROQ (taxmin emas, jismoniy kanal).
    Agar biror qadam ishlamasa -- JIM oddiy (mono) yo'lga qaytiladi.
  - Xom (`ai_raw_transcription`) va normallashtirilgan (`ai_transcription`)
    transkripsiya BAZADA ALOHIDA saqlanadi -- xom versiya HECH QACHON AI
    tomonidan "tozalanmaydi" (aynan ASR natijasi).
  - Tahlil bosqichi endi OpenAI Responses API (`/v1/responses`) orqali,
    QAT'IY JSON Schema (Structured Outputs) bilan ishlaydi -- erkin
    matnni parslash o'rniga. Model: `OPENAI_ANALYSIS_MODEL` (standart --
    `gpt-4o-mini`, ANIQ shu akkauntda ishlab turgan model). ESLATMA:
    foydalanuvchi taklif qilgan "gpt-5.6-terra" HAQIQIY OpenAI modeli
    EMAS (bunday model mavjud emas) -- shuning uchun ataylab ishlatilmadi,
    o'rniga mavjud/tasdiqlangan model ishlatildi (bu holat foydalanuvchiga
    yakuniy hisobotda ALOHIDA aytiladi).
  - Tahlil natijasi kengaytirildi: mijoz so'rovi (mahsulot/brend/miqdor/
    o'lcham/parametrlar), menejer xatolari, ijobiy tomonlar, savdo
    natijasi (sotildi/yo'qotildi/kutilmoqda/noma'lum), qayta bog'lanish
    kerakmi, tavsiya etilgan javob.
  - Status/rang HAMON server kodida score'dan DETERMINISTIK hisoblanadi
    (modelga ishonilmaydi) -- bu V2'dan buyon shunday edi, o'zgarmadi.
  - Jarayon bosqichlari (`ai_stage`): uploaded/processing_audio/
    transcribing/analyzing/completed/failed -- agar transkripsiya
    muvaffaqiyatli, lekin TAHLIL xato bergan bo'lsa, keyingi urinishda
    AUDIO QAYTA YUKLAB OLINMAYDI/QAYTA TRANSKRIPSIYA QILINMAYDI -- mavjud
    xom transkripsiyadan to'g'ridan-to'g'ri tahlil qilinadi.
  - Vaqtinchalik (tarmoq/5xx) xatolarda avtomatik qayta urinish
    qo'shildi -- 4xx (validatsiya/autentifikatsiya) xatolarida ASLO
    qayta urinilmaydi.
  - Batafsil log: qaysi model ishlatilgani, audio davomiyligi/formati/
    kanallar soni, transkripsiya/tahlil davomiyligi (soniyada), xatolar --
    API KALITI HECH QACHON log qilinmaydi.

MUHIM CHEKLOV (foydalanuvchi talabi bilan): faqat OpenAI ishlatiladi.
Boshqa transkripsiya provayderi (AssemblyAI, Deepgram, Google Speech,
Azure Speech va h.k.) QO'SHILMAGAN va qo'shilmasligi kerak, agar
foydalanuvchi ANIQ so'ramasa.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import datetime as dt

import requests

import call_glossary

logger = logging.getLogger("call_analysis")

# ---------------------------------------------------------------------------
# Model konfiguratsiyasi
# ---------------------------------------------------------------------------

# 2026-08 V4: `gpt-4o-transcribe` ENDI BIRINCHI nomzod (foydalanuvchi
# so'rovi bilan sifat uchun ustuvor qilindi). `whisper-1` OXIRIDA --
# eng keng mavjud fallback sifatida saqlanadi. `OPENAI_TRANSCRIBE_MODEL`
# environment variable orqali bu ro'yxatning BOSHIGA qo'shimcha nomzod
# qo'shish mumkin (masalan test uchun).
_TRANSCRIBE_MODEL_CANDIDATES = ["gpt-4o-transcribe", "gpt-4o-mini-transcribe", "whisper-1"]
_working_transcribe_config = None  # (model, include_language) -- keshlangan ishlaydigan kombinatsiya

# Stereo-kanal ajratishda har bir kanalni ALOHIDA transkripsiya qilish
# uchun vaqt tamg'asi (segments) KERAK -- bunga faqat `whisper-1`ning
# `verbose_json` formati javob beradi (`gpt-4o-transcribe`/`-mini`
# `verbose_json`/timestamp granularity'ni QO'LLAB-QUVVATLAMAYDI, OpenAI
# hujjatlariga ko'ra) -- shuning uchun bu FAQAT shu tor vazifa uchun
# whisper-1 ATAYLAB ishlatiladi, umumiy ustuvorlikka zid emas.
_CHANNEL_TIMESTAMP_MODEL = "whisper-1"

OPENAI_ANALYSIS_MODEL = os.environ.get("OPENAI_ANALYSIS_MODEL", os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))

# Qaysi jismoniy audio kanal operator (Manager) liniyasi deb hisoblanadi.
# Ko'pchilik IP-telefoniya/qo'ng'iroq yozish tizimlarida kanal 0 (chap) --
# operator, kanal 1 (o'ng) -- mijoz, lekin bu YOZUV TIZIMIGA BOG'LIQ
# konvensiya -- boshqacha bo'lsa shu environment variable orqali "1"ga
# o'zgartirilsin.
CHANNEL_OPERATOR_INDEX = int(os.environ.get("CALL_OPERATOR_CHANNEL", "0") or "0")

# Vaqtinchalik (tarmoq/5xx) xatolarda necha marta qayta urinish.
_MAX_RETRIES = 2
_TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}

_REQUIRED_LEGACY_KEYS = ("overview", "score")


def is_configured() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


# ---------------------------------------------------------------------------
# Tarmoq qatlami -- qayta urinish (retry) bilan
# ---------------------------------------------------------------------------

def _openai_request(method: str, url: str, *, headers: dict, json_body=None, data=None, files=None, timeout: int = 90):
    """Barcha OpenAI so'rovlari SHU orqali yuboriladi -- vaqtinchalik
    (tarmoq uzilishi, timeout, HTTP 429/5xx) xatolarda avtomatik qayta
    urinadi (`_MAX_RETRIES` marta, ortib boruvchi kutish bilan).
    VALIDATSIYA/AUTENTIFIKATSIYA xatolari (400/401/403/404/422) ASLO
    qayta urinilmaydi -- ular takrorlansa ham natija o'zgarmaydi, faqat
    vaqt behuda ketadi."""
    last_exc = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = requests.request(
                method, url, headers=headers, json=json_body, data=data, files=files, timeout=timeout,
            )
        except requests.RequestException as e:
            last_exc = e
            if attempt < _MAX_RETRIES:
                wait = 1.5 * (attempt + 1)
                logger.warning(
                    "OpenAI so'rovi tarmoq xatosi (%s, urinish %s/%s): %s -- %.1fs kutib qayta uriniladi",
                    url, attempt + 1, _MAX_RETRIES + 1, e, wait,
                )
                time.sleep(wait)
                continue
            raise
        if resp.status_code in _TRANSIENT_STATUS_CODES and attempt < _MAX_RETRIES:
            wait = 1.5 * (attempt + 1)
            logger.warning(
                "OpenAI vaqtinchalik xatosi (HTTP %s, %s, urinish %s/%s) -- %.1fs kutib qayta uriniladi",
                resp.status_code, url, attempt + 1, _MAX_RETRIES + 1, wait,
            )
            time.sleep(wait)
            continue
        return resp
    raise last_exc


def _extract_openai_error(resp) -> str:
    try:
        body = resp.json()
        msg = (body.get("error") or {}).get("message")
        if msg:
            return msg
    except Exception:
        pass
    return (resp.text or "")[:300] or f"HTTP {resp.status_code}"


# ---------------------------------------------------------------------------
# Audio yuklab olish va formatni aniqlash
# ---------------------------------------------------------------------------

def _guess_audio_format(url: str, content_type: "str | None") -> str:
    ct = (content_type or "").lower()
    if "wav" in ct or url.lower().endswith(".wav"):
        return "wav"
    if "ogg" in ct or url.lower().endswith(".ogg"):
        return "ogg"
    if "m4a" in ct or url.lower().endswith(".m4a"):
        return "m4a"
    return "mp3"


def _detect_magic_format(data: bytes) -> "str | None":
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


def _sniff_audio_format(data: bytes, content_type: "str | None", url: str) -> str:
    return _detect_magic_format(data) or _guess_audio_format(url, content_type)


def _download_audio(url: str) -> "tuple[bytes, str]":
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    data = resp.content
    content_type = resp.headers.get("Content-Type", "")

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


# ---------------------------------------------------------------------------
# Audio metadata (ffprobe) -- IXTIYORIY, faqat mavjud bo'lsa ishlaydi
# ---------------------------------------------------------------------------

def _ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def probe_audio_metadata(audio_bytes: bytes, audio_format: str) -> "dict | None":
    """ffprobe orqali audio metadata (kodek, sample rate, davomiylik,
    kanallar soni, bitrate) ni o'qiydi. Render'ning joriy "python"
    runtime'ida ffmpeg/ffprobe O'RNATILMAGAN bo'lishi mumkin -- shuning
    uchun `shutil.which()` bilan himoyalangan, mavjud bo'lmasa `None`
    qaytaradi (xato tashlamaydi, butun tahlilni to'xtatmaydi). Original
    fayl HECH QACHON o'zgartirilmaydi -- faqat vaqtinchalik nusxa
    o'qiladi va so'ng o'chiriladi."""
    if not _ffprobe_available():
        logger.info("ffprobe topilmadi -- audio metadata aniqlash o'tkazib yuborildi.")
        return None
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=f".{audio_format}", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_streams", "-show_format",
                "-of", "json", tmp_path,
            ],
            capture_output=True, text=True, timeout=20,
        )
        if proc.returncode != 0:
            logger.warning("ffprobe xato bilan tugadi (%s): %s", proc.returncode, (proc.stderr or "")[:300])
            return None
        info = json.loads(proc.stdout or "{}")
        streams = info.get("streams") or []
        audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
        fmt = info.get("format") or {}
        if not audio_streams:
            return {"channels": None, "codec": None, "duration_sec": None, "sample_rate": None}
        a = audio_streams[0]
        duration = fmt.get("duration") or a.get("duration")
        return {
            "channels": int(a["channels"]) if a.get("channels") is not None else None,
            "codec": a.get("codec_name"),
            "duration_sec": float(duration) if duration is not None else None,
            "sample_rate": int(a["sample_rate"]) if a.get("sample_rate") is not None else None,
        }
    except Exception as e:
        logger.warning("Audio metadata (ffprobe) aniqlashda xato: %s", e)
        return None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Stereo (2-kanalli) yozuvlarni ALOHIDA kanal bo'yicha qayta ishlash
# ---------------------------------------------------------------------------

def split_stereo_channels(audio_bytes: bytes, audio_format: str) -> "tuple[bytes, bytes] | None":
    """2 kanalli audio faylni IKKI alohida mono WAV faylga ajratadi
    (ffmpeg orqali). Muvaffaqiyatsiz bo'lsa (ffmpeg yo'q, fayl mono,
    xato) -- `None` qaytaradi, chaqiruvchi oddiy (mono/diarizatsiya)
    yo'lga qaytadi. Original bayt-massiv HECH QACHON o'zgartirilmaydi --
    faqat vaqtinchalik nusxalar bilan ishlanadi."""
    if not _ffmpeg_available():
        return None
    src_path = left_path = right_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=f".{audio_format}", delete=False) as src:
            src.write(audio_bytes)
            src_path = src.name
        left_fd, left_path = tempfile.mkstemp(suffix=".wav")
        right_fd, right_path = tempfile.mkstemp(suffix=".wav")
        os.close(left_fd)
        os.close(right_fd)
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-i", src_path,
                "-filter_complex", "[0:a]channelsplit=channel_layout=stereo[left][right]",
                "-map", "[left]", left_path,
                "-map", "[right]", right_path,
            ],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            logger.warning("ffmpeg kanal ajratishda xato: %s", (proc.stderr or "")[:400])
            return None
        with open(left_path, "rb") as f:
            left_bytes = f.read()
        with open(right_path, "rb") as f:
            right_bytes = f.read()
        if not left_bytes or not right_bytes:
            return None
        return left_bytes, right_bytes
    except Exception as e:
        logger.warning("Stereo kanal ajratishda kutilmagan xato: %s", e)
        return None
    finally:
        for p in (src_path, left_path, right_path):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass


def _transcribe_channel_with_timestamps(api_key: str, channel_bytes: bytes) -> "list | None":
    """Bitta kanalni `whisper-1` + `verbose_json` bilan transkripsiya
    qiladi -- natija `segments` ro'yxati (har biri `start`/`end`/`text`
    bilan), keyinchalik ikki kanalni VAQT bo'yicha xronologik
    birlashtirish uchun kerak. Xato bo'lsa `None` qaytaradi."""
    try:
        resp = _openai_request(
            "POST", "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": ("channel.wav", channel_bytes, "audio/wav")},
            data={"model": _CHANNEL_TIMESTAMP_MODEL, "response_format": "verbose_json", "temperature": "0"},
            timeout=90,
        )
    except requests.RequestException as e:
        logger.warning("Kanal transkripsiyasi tarmoq xatosi: %s", e)
        return None
    if not resp.ok:
        logger.warning("Kanal transkripsiyasi xato (HTTP %s): %s", resp.status_code, _extract_openai_error(resp))
        return None
    try:
        return resp.json().get("segments") or []
    except Exception:
        return None


def try_stereo_channel_transcription(audio_bytes: bytes, audio_format: str, channels_hint: "int | None") -> "str | None":
    """Agar yozuv 2 kanalli bo'lsa VA ffmpeg mavjud bo'lsa -- har bir
    kanalni ALOHIDA transkripsiya qilib, vaqt tamg'asi bo'yicha
    xronologik "Manager: ...\\nMijoz: ...\\n" matniga birlashtiradi.
    Bu DIARIZATSIYADAN (taxmindan) ko'ra ISHONCHLIROQ -- chunki
    gapiruvchi jismoniy kanal bilan aniqlanadi, kontent bilan taxmin
    qilinmaydi. Har qanday bosqichda muammo chiqsa -- `None` qaytadi,
    chaqiruvchi mono yo'lga (diarizatsiya/oddiy transkripsiya) o'tadi."""
    if channels_hint is not None and channels_hint != 2:
        return None
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    split = split_stereo_channels(audio_bytes, audio_format)
    if not split:
        return None
    left_bytes, right_bytes = split
    left_segments = _transcribe_channel_with_timestamps(api_key, left_bytes)
    right_segments = _transcribe_channel_with_timestamps(api_key, right_bytes)
    if not left_segments and not right_segments:
        return None

    operator_is_left = CHANNEL_OPERATOR_INDEX == 0
    left_label = "Manager" if operator_is_left else "Mijoz"
    right_label = "Mijoz" if operator_is_left else "Manager"

    merged = []
    for seg in (left_segments or []):
        text = (seg.get("text") or "").strip()
        if text:
            merged.append((seg.get("start", 0.0), left_label, text))
    for seg in (right_segments or []):
        text = (seg.get("text") or "").strip()
        if text:
            merged.append((seg.get("start", 0.0), right_label, text))
    if not merged:
        return None
    merged.sort(key=lambda t: t[0])
    lines = [f"{label}: {text}" for _start, label, text in merged]
    logger.info(
        "Stereo kanal transkripsiyasi muvaffaqiyatli: %s bo'lak (chap=%s, o'ng=%s)",
        len(lines), len(left_segments or []), len(right_segments or []),
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Mono transkripsiya (oddiy, kanal ajratilmagan yozuvlar uchun)
# ---------------------------------------------------------------------------

def _post_transcription(api_key: str, model: str, audio_bytes: bytes, audio_format: str, include_language: bool = True):
    data = {
        "model": model,
        "response_format": "text",
        # 2026-08 V4: hint matni endi `call_glossary.py`dan olinadi --
        # markazlashtirilgan, kengaytiriladigan lug'at (dialog namunasi +
        # sohaga oid atamalar ro'yxati). Whisper "prompt"ni tavsif emas,
        # "audio davomi" sifatida talqin qiladi -- shuning uchun haqiqiy
        # dialog namunasi bilan boshlanadi.
        "prompt": call_glossary.build_transcription_prompt(),
        "temperature": "0",
    }
    if include_language:
        data["language"] = "uz"
    return _openai_request(
        "POST", "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {api_key}"},
        files={"file": (f"audio.{audio_format}", audio_bytes, f"audio/{audio_format}")},
        data=data,
        timeout=90,
    )


def _transcribe_audio(audio_bytes: bytes, audio_format: str) -> "tuple[str, str]":
    """Audio -> matn (mono yo'l). `(matn, ishlatilgan_model)` qaytaradi.
    Ikki bosqichli qidiruv: 1) barcha nomzodlarga `language='uz'` bilan;
    2) hech qaysi biri qabul qilmasa -- tilsiz (avtomatik aniqlash +
    dialog-namunali prompt hint bilan). Ishlagan kombinatsiya keshlanadi."""
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
            return None
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

    if _working_transcribe_config:
        cached_model, cached_lang = _working_transcribe_config
        text = _attempt(cached_model, cached_lang)
        if text:
            return text, cached_model
        _working_transcribe_config = None

    for model in candidates:
        text = _attempt(model, True)
        if text:
            _working_transcribe_config = (model, True)
            return text, model

    for model in candidates:
        text = _attempt(model, False)
        if text:
            _working_transcribe_config = (model, False)
            return text, model

    raise RuntimeError(
        "OpenAI'da ishlaydigan transkripsiya kombinatsiyasi topilmadi. Sinalgan: "
        + "; ".join(attempts)
    )


# ---------------------------------------------------------------------------
# Diarizatsiya (ixtiyoriy qatlam, mono yozuvlar uchun)
# ---------------------------------------------------------------------------

def _post_diarized_transcription(api_key: str, model: str, audio_bytes: bytes, audio_format: str):
    return _openai_request(
        "POST", "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {api_key}"},
        files={"file": (f"audio.{audio_format}", audio_bytes, f"audio/{audio_format}")},
        data={"model": model, "response_format": "diarized_json", "chunking_strategy": "auto"},
        timeout=90,
    )


def _build_labeled_transcript_from_segments(segments: list) -> "tuple[str, bool] | None":
    """`diarized_json`dagi `segments`ni "Manager:/Mijoz:" formatiga
    aylantiradi. Ikkinchi element -- `confident: bool` -- agar aniq
    ikkita gapiruvchi topilmasa yoki taxmin ISHONCHSIZ bo'lsa `False`
    (bunday holda "Speaker A/B" saqlanadi, Manager/Mijoz deb NOTO'G'RI
    ishonch bilan belgilanmaydi -- foydalanuvchi ANIQ shuni so'ragan)."""
    if not segments:
        return None
    speaker_order = []
    for seg in segments:
        raw_speaker = str(seg.get("speaker", "")).strip() or "?"
        if raw_speaker not in speaker_order:
            speaker_order.append(raw_speaker)

    # Ishonch mezoni: aynan 2 ta gapiruvchi bo'lsa VA ikkalasi ham yetarlicha
    # gapirsa (juda qisqa "aralashib qolgan" segment emas) -- Manager/Mijoz
    # deb belgilashga ishonamiz. Aks holda "Speaker 1/Speaker 2" saqlanadi.
    confident = len(speaker_order) == 2

    lines = []
    for seg in segments:
        raw_speaker = str(seg.get("speaker", "")).strip() or "?"
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        if confident:
            idx = speaker_order.index(raw_speaker)
            label = "Manager" if idx == 0 else "Mijoz"
        else:
            idx = speaker_order.index(raw_speaker)
            label = f"Speaker {idx + 1}"
        lines.append(f"{label}: {text}")
    return ("\n".join(lines), confident) if lines else None


def _try_diarized_transcription(audio_bytes: bytes, audio_format: str) -> "tuple[str, str] | None":
    """Muvaffaqiyatli bo'lsa `(labeled_text, raw_json_str)` qaytaradi --
    `raw_json_str` faqat DEBUG maqsadida (`ai_diarized_json` ustuniga)
    saqlanadi, tahlil mantiqiga ta'sir qilmaydi."""
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
        raw_json = resp.json()
        segments = raw_json.get("segments") or []
    except Exception:
        logger.warning("Diarizatsiya javobini JSON qilib o'qib bo'lmadi -- oddiy transkripsiya yo'liga o'tilmoqda.")
        return None
    built = _build_labeled_transcript_from_segments(segments)
    if not built:
        return None
    text, _confident = built
    try:
        raw_json_str = json.dumps(raw_json, ensure_ascii=False)[:20000]
    except Exception:
        raw_json_str = ""
    return text, raw_json_str


# ---------------------------------------------------------------------------
# Transkripsiya matnini "gap-bo'lib-gap" bo'laklarga ajratish (UI uchun)
# ---------------------------------------------------------------------------

_TURN_RE = re.compile(
    r"(?m)^(Manager|Mijoz|Speaker\s*1|Speaker\s*2)\s*:\s*"
    r"(.*(?:\n(?!(?:Manager|Mijoz|Speaker\s*1|Speaker\s*2)\s*:).*)*)"
)


def parse_transcript_turns(text: str) -> list:
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
        turns = [{"speaker": "unknown", "raw_label": "", "text": text.strip()}]
    return turns


def _turns_to_labeled_text(turns: list) -> str:
    """Structured Output'dan kelgan `[{"speaker","text"}, ...]`ni
    "Manager: ...\\nMijoz: ...\\n" matniga aylantiradi (deterministik,
    server kodida -- modelning o'z formatlashiga ishonilmaydi)."""
    lines = []
    for t in turns or []:
        speaker = (t.get("speaker") or "unknown").strip().lower()
        text = (t.get("text") or "").strip()
        if not text:
            continue
        label = {"manager": "Manager", "mijoz": "Mijoz"}.get(speaker, "Speaker")
        lines.append(f"{label}: {text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tahlil bosqichi -- OpenAI Responses API + Structured Outputs (JSON Schema)
# ---------------------------------------------------------------------------

_ANALYSIS_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "overview", "score", "normalizedTranscript", "customerRequest",
        "operatorMistakes", "positivePoints", "saleResult",
        "callbackRequired", "recommendedResponse",
    ],
    "properties": {
        "overview": {"type": "string", "description": "2-3 gapdan iborat juda qisqa xulosa."},
        "score": {"type": "integer", "description": "Suhbat sifatiga 1 dan 10 gacha baho."},
        "normalizedTranscript": {
            "type": "array",
            "description": "Suhbat, gap-bo'lib-gap, gapiruvchi bilan.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["speaker", "text"],
                "properties": {
                    "speaker": {"type": "string", "enum": ["manager", "mijoz", "unknown"]},
                    "text": {"type": "string"},
                },
            },
        },
        "customerRequest": {
            "type": "object",
            "additionalProperties": False,
            "required": ["product", "brand", "quantity", "measurement", "parameters"],
            "properties": {
                "product": {"type": ["string", "null"]},
                "brand": {"type": ["string", "null"]},
                "quantity": {"type": ["string", "null"]},
                "measurement": {"type": ["string", "null"]},
                "parameters": {"type": ["string", "null"]},
            },
        },
        "operatorMistakes": {"type": "array", "items": {"type": "string"}},
        "positivePoints": {"type": "array", "items": {"type": "string"}},
        "saleResult": {"type": "string", "enum": ["sold", "lost", "pending", "unknown"]},
        "callbackRequired": {"type": "boolean"},
        "recommendedResponse": {"type": "string"},
    },
}

_ANALYSIS_SYSTEM_PROMPT = f"""Sen professional savdo va mijoz bilan suhbatlarni tahlil qiluvchi AI assistentsan.

Senga qo'ng'iroq suhbatining TAYYOR TRANSKRIPSIYASI (matn ko'rinishida, avtomatik nutqni-matnga aylantirish xizmati orqali olingan) beriladi. Matn asosan o'zbek tilida bo'lishi mumkin, lekin ruscha, inglizcha yoki boshqa so'zlar aralashishi mumkin.

QAT'IY QOIDALAR (buzilmasin):

1. Transkripsiyada AYTILGAN so'zlarni HECH QACHON "to'g'ri" yoki "adabiy" o'zbek tiliga o'zgartirma yoki tarjima qilma. Masalan agar transkripsiyada "120 plotnost, 8 santimetrlisidan kerak" deyilgan bo'lsa -- buni SHU KO'RINISHIDA saqla, "120 zichlik, 8 santimetrlik" kabi "tuzatilgan" versiyaga almashtirma.
2. Transkripsiyada YO'Q ma'lumotni o'zingdan qo'shib chiqarma yoki umumiy bilimingdan foydalanib "to'ldirma" (masalan "8 sm bazalt albatta mavjud" kabi tasdiqni faqat MATNDA aniq aytilgan bo'lsagina yoz).
3. Noaniq/eshitilmagan/uzilgan joylarni "[noaniq]" deb belgila -- o'zingdan taxmin qilib to'ldirma.
4. Mahsulot/brend nomini ANIQ bilmasang yoki noaniq eshitilgan bo'lsa -- "eng yaqin" nomga zo'rma-zo'raki moslashtirma, transkripsiyada qanday kelgan bo'lsa saqla yoki "[noaniq]" deb belgila.
5. Gapiruvchilarni imkon qadar "manager"/"mijoz" deb ajrat; aniqlab bo'lmasa "unknown" qoldir -- taxmin bilan noto'g'ri belgilashdan ko'ra "unknown" afzal.

{call_glossary.build_analysis_glossary_note()}

BAHOLASH (1-10, faqat butun son):
1-3 = suhbat yomon o'tgan; 4-6 = o'rtacha, yaxshilash kerak; 7-10 = yaxshi suhbat.
E'tibor ber: menejer mijozni tushundimi, to'g'ri savollar berdimi, ehtiyojni aniqladimi, aniq/tushunarli javob berdimi, professional gaplashdimi, suhbatni keyingi qadamga olib bordimi, mijozning savollari javobsiz qolmadimi.

MIJOZ SO'ROVI (customerRequest): mijoz nima so'ragani -- mahsulot, brend, miqdor, o'lcham (masalan santimetr/millimetr/kvadrat/kub), boshqa parametrlar. Aniq aytilmagan maydonlarni `null` qoldir -- o'zingdan to'ldirma.

MENEJER XATOLARI / IJOBIY TOMONLAR: aniq, transkripsiyaga asoslangan, qisqa bandlar ro'yxati (bo'sh bo'lishi ham mumkin).

SAVDO NATIJASI (saleResult): "sold" (sotildi/buyurtma berildi), "lost" (rad etildi/qiziqmadi), "pending" (hali hal bo'lmagan, o'ylab ko'radi), "unknown" (transkripsiyadan aniqlab bo'lmaydi).

CALLBACK (callbackRequired): mijozga qayta qo'ng'iroq/aloqa qilish kerakmi (masalan "keyinroq qo'ng'iroq qilaman" deyilgan bo'lsa).

TAVSIYA (recommendedResponse): menejer/kompaniya keyingi safar qanday harakat qilishi kerakligi bo'yicha qisqa, amaliy tavsiya.

MUHIM: JSON'dan tashqarida hech qanday matn yozma, markdown ishlatma."""


def _extract_responses_output_text(data: dict) -> str:
    """OpenAI Responses API javobidan (`/v1/responses`) matnli JSON'ni
    chiqarib oladi. Struktura: `output` -- xabarlar ro'yxati, har birida
    `content` -- bo'laklar ro'yxati, `type == "output_text"` bo'lgani
    haqiqiy matnni saqlaydi."""
    for item in data.get("output") or []:
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if part.get("type") in ("output_text", "text") and part.get("text"):
                return part["text"]
    # Ba'zi SDK/versiyalarda qulaylik uchun to'g'ridan-to'g'ri maydon bo'lishi mumkin.
    if data.get("output_text"):
        return data["output_text"]
    raise ValueError("OpenAI Responses API javobidan matn topilmadi.")


def _parse_analysis_json(raw_text: str) -> dict:
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    data = json.loads(text)

    missing = [k for k in _REQUIRED_LEGACY_KEYS if k not in data]
    if missing:
        raise ValueError(f"Model javobida quyidagi maydonlar yo'q: {missing}")

    # Status/rang HAMISHA score'dan server kodida DETERMINISTIK
    # hisoblanadi -- modelning o'z bahosiga ishonilmaydi.
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
    data.setdefault("normalizedTranscript", [])
    data.setdefault("customerRequest", {})
    data.setdefault("operatorMistakes", [])
    data.setdefault("positivePoints", [])
    data.setdefault("saleResult", "unknown")
    data.setdefault("callbackRequired", False)
    data.setdefault("recommendedResponse", "")
    return data


_SALE_RESULT_LABELS_UZ = {
    "sold": "Sotildi",
    "lost": "Yo'qotildi",
    "pending": "Kutilmoqda",
    "unknown": "Noma'lum",
}


def _build_result_summary(data: dict) -> str:
    """`ai_result` ustuni (jadvalda "Natija" sifatida ko'rsatiladi) uchun
    qisqa, server tomonidan yig'ilgan matn -- modeldan alohida erkin matn
    so'rash o'rniga, allaqachon structured maydonlardan deterministik
    quriladi."""
    sale = _SALE_RESULT_LABELS_UZ.get(data.get("saleResult"), "Noma'lum")
    parts = [f"Natija: {sale}."]
    if data.get("callbackRequired"):
        parts.append("Mijozga qayta bog'lanish kerak.")
    if data.get("recommendedResponse"):
        parts.append(f"Tavsiya: {data['recommendedResponse']}")
    return " ".join(parts)


def _analyze_transcript(transcript_text: str) -> dict:
    """Matn -> tahlil. OpenAI Responses API + Structured Outputs (qat'iy
    JSON Schema) orqali -- erkin matnni qo'lda parslash EMAS."""
    api_key = os.environ.get("OPENAI_API_KEY")
    resp = _openai_request(
        "POST", "https://api.openai.com/v1/responses",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        json_body={
            "model": OPENAI_ANALYSIS_MODEL,
            "temperature": 0,
            "input": [
                {"role": "system", "content": _ANALYSIS_SYSTEM_PROMPT},
                {"role": "user", "content": f"Transkripsiya matni:\n\n{transcript_text}"},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "call_analysis",
                    "schema": _ANALYSIS_JSON_SCHEMA,
                    "strict": True,
                },
            },
        },
        timeout=90,
    )
    if not resp.ok:
        err_msg = _extract_openai_error(resp)
        raise RuntimeError(f"OpenAI tahlil xatosi (model={OPENAI_ANALYSIS_MODEL}, HTTP {resp.status_code}): {err_msg}")
    raw_text = _extract_responses_output_text(resp.json())
    return _parse_analysis_json(raw_text)


# ---------------------------------------------------------------------------
# Asosiy orkestratsiya -- bitta yozuvni tahlil qilish
# ---------------------------------------------------------------------------

def analyze_call_record(session, call) -> dict:
    """Bitta `db.CallRecord` yozuvini tahlil qiladi. Bosqichlar
    (`call.ai_stage`): processing_audio -> transcribing -> analyzing ->
    completed (yoki xato bo'lsa -- failed). Agar OLDINGI urinishda
    transkripsiya MUVAFFAQIYATLI bo'lib, faqat TAHLIL bosqichi xato
    bergan bo'lsa (`ai_raw_transcription` mavjud VA `ai_error` mavjud) --
    audio QAYTA yuklab olinmaydi/QAYTA transkripsiya qilinmaydi, to'g'ridan
    -to'g'ri mavjud xom transkripsiyadan tahlil qilinadi."""
    now = dt.datetime.utcnow()
    if not call.recording_url:
        raise ValueError("Bu qo'ng'iroqda yozuv (recording_url) yo'q.")

    resume_from_transcript = bool(call.ai_raw_transcription) and bool(call.ai_error)

    try:
        if resume_from_transcript:
            logger.info(
                "Qo'ng'iroq #%s: oldingi transkripsiya mavjud, faqat TAHLIL qayta urinilmoqda "
                "(audio qayta yuklanmaydi).", call.id,
            )
            transcript_text = call.ai_raw_transcription
            call.ai_stage = "analyzing"
            session.commit()
        else:
            call.ai_stage = "processing_audio"
            session.commit()
            t0 = time.monotonic()
            audio_bytes, audio_format = _download_audio(call.recording_url)

            metadata = probe_audio_metadata(audio_bytes, audio_format)
            channels = metadata.get("channels") if metadata else None
            if metadata:
                call.ai_audio_channels = metadata.get("channels")
                call.ai_audio_codec = metadata.get("codec")
                call.ai_audio_duration_sec = metadata.get("duration_sec")
                session.commit()
                duration_label = (
                    f"{metadata['duration_sec']:.1f}s" if metadata.get("duration_sec") is not None else "noma'lum"
                )
                logger.info(
                    "Qo'ng'iroq #%s audio metadata: kodek=%s, kanal=%s, davomiylik=%s",
                    call.id, metadata.get("codec"), metadata.get("channels"), duration_label,
                )

            call.ai_stage = "transcribing"
            session.commit()

            transcript_text = None
            model_used = None
            diarized_raw_json = None

            # 1) 2-kanalli (stereo) bo'lsa -- kanal bo'yicha ALOHIDA
            #    transkripsiya (eng ishonchli, taxminsiz gapiruvchi ajratish).
            if channels == 2:
                transcript_text = try_stereo_channel_transcription(audio_bytes, audio_format, channels)
                if transcript_text:
                    model_used = f"{_CHANNEL_TIMESTAMP_MODEL} (stereo-split)"

            # 2) Mono/aralash -- avval HAQIQIY diarizatsiya bilan sinaladi.
            if not transcript_text:
                diarize_result = _try_diarized_transcription(audio_bytes, audio_format)
                if diarize_result:
                    transcript_text, diarized_raw_json = diarize_result
                    model_used = os.environ.get("OPENAI_DIARIZE_MODEL", "gpt-4o-transcribe-diarize")

            # 3) Hech biri ishlamasa -- oddiy transkripsiya + matn modeliga taxmin.
            if not transcript_text:
                transcript_text, model_used = _transcribe_audio(audio_bytes, audio_format)

            if diarized_raw_json:
                call.ai_diarized_json = diarized_raw_json

            transcribe_elapsed = time.monotonic() - t0
            logger.info(
                "Qo'ng'iroq #%s transkripsiya tugadi: model=%s, %.1fs, %s belgi",
                call.id, model_used, transcribe_elapsed, len(transcript_text or ""),
            )

            call.ai_raw_transcription = transcript_text
            call.ai_model_transcribe = model_used
            call.ai_stage = "analyzing"
            session.commit()

        t1 = time.monotonic()
        result = _analyze_transcript(transcript_text)
        analyze_elapsed = time.monotonic() - t1
        logger.info(
            "Qo'ng'iroq #%s tahlil tugadi: model=%s, %.1fs, baho=%s",
            call.id, OPENAI_ANALYSIS_MODEL, analyze_elapsed, result.get("score"),
        )

        normalized_text = _turns_to_labeled_text(result["normalizedTranscript"])
        call.ai_overview = result["overview"]
        call.ai_score = result["score"]
        call.ai_status = result["status"]
        call.ai_color = result["color"]
        call.ai_result = _build_result_summary(result)
        call.ai_transcription = normalized_text or transcript_text
        call.ai_customer_request = json.dumps(result["customerRequest"], ensure_ascii=False)
        call.ai_operator_mistakes = json.dumps(result["operatorMistakes"], ensure_ascii=False)
        call.ai_positive_points = json.dumps(result["positivePoints"], ensure_ascii=False)
        call.ai_sale_result = result["saleResult"]
        call.ai_callback_required = bool(result["callbackRequired"])
        call.ai_recommended_response = result["recommendedResponse"]
        call.ai_model_analysis = OPENAI_ANALYSIS_MODEL
        call.ai_error = None
        call.ai_stage = "completed"
        call.ai_analyzed_at = now
        session.commit()
        return result
    except Exception as e:
        logger.exception("Qo'ng'iroq #%s tahlilida xato", call.id)
        call.ai_error = f"{type(e).__name__}: {e}"[:2000]
        call.ai_stage = "failed"
        call.ai_analyzed_at = now
        session.commit()
        raise


def run_pending_analysis(session, limit: int = 10) -> dict:
    """Hali tahlil qilinmagan, YOZUVI BOR va "haqiqiy" (min_real_talk_seconds
    chegarasidan uzun) qo'ng'iroqlarni topib, birma-bir tahlil qiladi.
    Hali umuman urinilmagan yozuvlarga USTUNLIK beriladi, qolgan joy
    bo'lsagina xatolik bilan tugaganlar qayta sinaladi (bu safar, agar
    transkripsiya avval muvaffaqiyatli bo'lgan bo'lsa, faqat tahlil
    qayta sinaladi -- `analyze_call_record` ichidagi mantiq bilan)."""
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
            failed += 1

    return {
        "analyzed": analyzed,
        "failed": failed,
        "remaining": never_tried_q.count(),
        "retry_remaining": retry_q.count(),
    }
