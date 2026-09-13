"""call_analysis.py — 2026-09, foydalanuvchi ANIQ so'rovi bilan ("audio
tahlil qilishni o'chirib tashla to'liq va audiolar kelishi lekin
bo'laversin, qovursin"): AI qo'ng'iroq-tahlili (transkripsiya + 1-10
rubrika bahosi) konveyeri BUTUNLAY OLIB TASHLANDI. Moi Zvonki qo'ng'iroq
yozuvlarining o'zi (sinxronizatsiya, ro'yxat, pleer) BUTUNLAY TEGILMAGAN --
qarang `call_sync.py`/`call_analytics.py` va `app.py`dagi
`individual_check()`/`individual_check_audio_proxy()`.

Bu fayl ENDI ikki narsa uchun qoladi:

  1. Umumiy OpenAI so'rov infratuzilmasi (`_openai_request`,
     `_extract_openai_error`, `_extract_responses_output_text`,
     `_is_quota_exhausted_response`, `OpenAICreditExhaustedError`,
     `OPENAI_ANALYSIS_MODEL`) -- bular BOSHQA, HALI FAOL funksiya
     tomonidan qayta ishlatiladi: Instagram DM lid-sifat bahosi
     (`ig_dm_analysis.py`, `from call_analysis import ...`). Shu sabab
     BU QISM O'CHIRILMAYDI.
  2. Audio format/metadata yordamchi funksiyalari (`_detect_magic_format`,
     `probe_audio_metadata`, `ffmpeg_available`/`ffprobe_available`) --
     bular admin panelda QO'LDA audio yuklash (`individual_check_upload_audio`
     `app.py`da) uchun hali kerak (fayl formatini/davomiyligini aniqlash,
     AI TAHLIL EMAS).

O'chirilgan qism (transkripsiya, diarizatsiya, rubrika-bahosi,
`analyze_call_record()`, `run_pending_analysis()`, `scheduler.py`dagi
`job_call_analysis()` cron'i -- allaqachon o'chiq edi) ushbu git tarixida
saqlanib qoladi, kerak bo'lsa qayta tiklash mumkin. Birga o'chirilgan
yordamchi modullar: `call_glossary.py`, `call_quality.py` (faqat shu
konveyer tomonidan ishlatilardi, boshqa hech kim ulardan foydalanmasdi).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
import json

import requests

logger = logging.getLogger("call_analysis")

# ---------------------------------------------------------------------------
# Model konfiguratsiyasi -- FAQAT Instagram DM tahlili (`ig_dm_analysis.py`)
# uchun qayta ishlatiladi.
# ---------------------------------------------------------------------------

OPENAI_ANALYSIS_MODEL = os.environ.get("OPENAI_ANALYSIS_MODEL", "gpt-4o-mini")

# Vaqtinchalik (tarmoq/5xx) xatolarda necha marta qayta urinish.
_MAX_RETRIES = 2
_TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}


def is_configured() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


# ---------------------------------------------------------------------------
# Tarmoq qatlami -- qayta urinish (retry) bilan. `ig_dm_analysis.py` ham
# shu funksiyani ishlatadi -- BITTA joyda tuzatilsa, ikkalasida ham
# ishlaydi.
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
        if resp.status_code == 429 and _is_quota_exhausted_response(resp):
            # Kredit tugagan -- bu DOIMIY holat, qayta urinish faqat
            # vaqtni behuda sarflaydi. Darhol qaytariladi, chaqiruvchi
            # `OpenAICreditExhaustedError` ko'tarishi kerak.
            logger.error(
                "OpenAI kredit/balans tugagan (HTTP 429, %s) -- qayta urinilmaydi: %s",
                url, _extract_openai_error(resp),
            )
            return resp
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


class OpenAICreditExhaustedError(RuntimeError):
    """OpenAI hisobida kredit/balans qolmagan (HTTP 429, lekin oddiy
    tezlik-chegarasi EMAS -- `error.code == "insufficient_quota"` yoki
    xabar matnida shunga mos belgilar). Qayta urinish FOYDASIZ."""


_QUOTA_EXHAUSTED_MARKERS = (
    "insufficient_quota", "exceeded your current quota", "no credits remaining",
    "you have no credits", "billing", "quota exceeded",
)


def _is_quota_exhausted_response(resp) -> bool:
    if getattr(resp, "status_code", None) != 429:
        return False
    try:
        body = resp.json()
        code = ((body.get("error") or {}).get("code") or "").lower()
        if code == "insufficient_quota":
            return True
        msg = ((body.get("error") or {}).get("message") or "").lower()
    except Exception:
        msg = (resp.text or "").lower()
    return any(marker in msg for marker in _QUOTA_EXHAUSTED_MARKERS)


# ---------------------------------------------------------------------------
# Audio format/metadata yordamchilari -- qo'lda audio yuklash uchun
# (`app.py`, `individual_check_upload_audio`). AI TAHLIL BILAN BOG'LIQ EMAS.
# ---------------------------------------------------------------------------

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


def _ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


# Ochiq (public) nom bilan ham eksport qilinadi -- `/api/health`da
# ffmpeg/ffprobe HAQIQATDA mavjudligini ko'rsatish uchun.
def ffmpeg_available() -> bool:
    return _ffmpeg_available()


def ffprobe_available() -> bool:
    return _ffprobe_available()


def log_model_config() -> None:
    """Ilova ishga tushganda LOGGA (hech qachon API kalitlarni EMAS) qaysi
    umumiy OpenAI modeli (Instagram DM tahlili uchun) ishlatilishini va
    audio vositalari (ffmpeg/ffprobe, qo'lda yuklangan audio uchun)
    mavjudligini yozadi."""
    logger.info("Shared OpenAI model (Instagram DM sifat bahosi uchun): %s", OPENAI_ANALYSIS_MODEL)
    logger.info("Audio tooling (qo'lda yuklangan audio uchun): ffmpeg_available=%s, ffprobe_available=%s", ffmpeg_available(), ffprobe_available())


def probe_audio_metadata(audio_bytes: bytes, audio_format: str) -> "dict | None":
    """ffprobe orqali audio metadata (kodek, sample rate, davomiylik,
    kanallar soni, bitrate) ni o'qiydi. Render'ning joriy "python"
    runtime'ida ffmpeg/ffprobe O'RNATILMAGAN bo'lishi mumkin -- shuning
    uchun `shutil.which()` bilan himoyalangan, mavjud bo'lmasa `None`
    qaytaradi (xato tashlamaydi). Original fayl HECH QACHON
    o'zgartirilmaydi -- faqat vaqtinchalik nusxa o'qiladi va so'ng
    o'chiriladi."""
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
