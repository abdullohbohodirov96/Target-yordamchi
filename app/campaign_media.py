"""campaign_media.py — Meta Ads Autopilot: qoralama uchun rasm/video
saqlash va Meta'ga yuklash (2026-09, foydalanuvchi so'rovi: "Replix ...
Ads Manager'dagi har bir maydonni boshidan o'zi to'ldirmasligi kerak" --
foydalanuvchi faqat rasm/videoni yuklaydi, `image_hash`/`video_id`ni
tizim o'zi oladi).

Fayllar `MEDIA_ROOT/<company_id>/<draft_id>/<uuid>_<xavfsiz nom>` ostida
saqlanadi (`CampaignDraftMedia.storage_path` -- MEDIA_ROOT'ga nisbatan).
Meta'ga yuklash (`ensure_uploaded_to_meta`) IDEMPOTENT: hash/video_id
allaqachon bo'lsa qayta yuklanmaydi -- nashr pipeline'i qayta urinilganda
bitta rasm ikki marta hisobga tushmaydi.
"""

import io
import os
import re
import json
import uuid
import logging
from pathlib import Path

import db
import meta_api

logger = logging.getLogger("campaign_media")

BASE_DIR = Path(__file__).parent
# Testlar/deploy bu qiymatni o'zgartirishi mumkin -- funksiyalar uni
# CHAQIRUV paytida o'qiydi (modul yuklanganda qotib qolmaydi).
MEDIA_ROOT = BASE_DIR / "uploads" / "ad_media"

ALLOWED_IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
ALLOWED_VIDEO_TYPES = {"video/mp4": ".mp4", "video/quicktime": ".mov", "video/x-m4v": ".m4v", "video/webm": ".webm"}
MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_VIDEO_BYTES = 50 * 1024 * 1024

_EXT_TO_TYPE = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif",
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".m4v": "video/x-m4v", ".webm": "video/webm",
}


class MediaError(Exception):
    """Foydalanuvchiga ko'rsatiladigan o'zbekcha xato (noto'g'ri fayl turi,
    hajm chegarasi va h.k.)."""


def _safe_filename(name: str) -> str:
    base = os.path.basename(name or "fayl")
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "fayl"
    return base[:80]


def _detect_content_type(filename: str, content_type: "str | None") -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in ALLOWED_IMAGE_TYPES or ct in ALLOWED_VIDEO_TYPES:
        return ct
    ext = os.path.splitext(filename or "")[1].lower()
    return _EXT_TO_TYPE.get(ext, ct or "application/octet-stream")


def _read_bytes(file_storage_or_bytes) -> bytes:
    if isinstance(file_storage_or_bytes, (bytes, bytearray)):
        return bytes(file_storage_or_bytes)
    stream = getattr(file_storage_or_bytes, "stream", None) or file_storage_or_bytes
    if hasattr(stream, "read"):
        data = stream.read()
        return data if isinstance(data, bytes) else bytes(data)
    raise MediaError("Fayl o'qib bo'lmadi.")


def media_file_path(media_row) -> Path:
    """`CampaignDraftMedia` qatorining diskdagi to'liq yo'li."""
    return Path(MEDIA_ROOT) / (media_row.storage_path or "")


def save_uploaded_media(session, company_id: int, draft_id: int, file_storage_or_bytes, filename: str, content_type: "str | None") -> "db.CampaignDraftMedia":
    """Yuklangan faylni tekshiradi (tur/hajm), diskka yozadi, rasm bo'lsa
    PIL orqali o'lchamini o'qiydi va `CampaignDraftMedia` qatorini yaratadi
    (`session.add` + `commit`). Noto'g'ri tur/hajm -> `MediaError` (o'zbekcha)."""
    filename = _safe_filename(filename)
    ct = _detect_content_type(filename, content_type)
    if ct in ALLOWED_IMAGE_TYPES:
        kind = "image"
        limit = MAX_IMAGE_BYTES
    elif ct in ALLOWED_VIDEO_TYPES:
        kind = "video"
        limit = MAX_VIDEO_BYTES
    else:
        raise MediaError("Faqat rasm (JPG, PNG, WEBP, GIF) yoki video (MP4, MOV, WEBM) yuklash mumkin.")
    data = _read_bytes(file_storage_or_bytes)
    if not data:
        raise MediaError("Fayl bo'sh.")
    if len(data) > limit:
        raise MediaError(f"Fayl juda katta -- {'rasm' if kind == 'image' else 'video'} uchun chegara {limit // (1024 * 1024)} MB.")

    width = height = None
    if kind == "image":
        try:
            from PIL import Image
            with Image.open(io.BytesIO(data)) as img:
                width, height = img.size
        except Exception as e:  # noqa: BLE001 -- PIL o'qiy olmasa: buzilgan/soxta rasm
            raise MediaError("Rasm faylini o'qib bo'lmadi -- fayl buzilgan yoki rasm emas.") from e

    rel_dir = Path(str(company_id)) / str(draft_id)
    abs_dir = Path(MEDIA_ROOT) / rel_dir
    abs_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}_{filename}"
    (abs_dir / stored_name).write_bytes(data)

    row = db.CampaignDraftMedia(
        company_id=company_id, draft_id=draft_id, kind=kind, filename=filename,
        storage_path=str(rel_dir / stored_name), content_type=ct, size_bytes=len(data),
        width=width, height=height, upload_status="pending",
    )
    session.add(row)
    session.commit()
    return row


def _log_event(session, media_row, action: str, details: dict) -> None:
    try:
        session.add(db.CampaignDraftEvent(
            company_id=media_row.company_id, draft_id=media_row.draft_id, actor="system",
            action=action, scope="ad", details_json=json.dumps(details, ensure_ascii=False),
        ))
    except Exception as e:  # noqa: BLE001 -- audit-jurnal asosiy oqimni to'xtatmasin
        logger.warning("campaign_media: event yozilmadi: %s", e)


def ensure_uploaded_to_meta(session, media_row, company) -> "db.CampaignDraftMedia":
    """Media hali Meta'ga yuklanmagan bo'lsa yuklaydi (rasm -> adimages
    hash, video -> advideos id) va qatorni yangilaydi. IDEMPOTENT: hash/id
    allaqachon bo'lsa hech narsa qilmaydi. Xato bo'lsa `upload_status=
    "failed"` + `upload_error` yoziladi va `MetaAPIError` qayta ko'tariladi
    (nashr pipeline'i buni friendly xatoga aylantiradi)."""
    if media_row.kind == "image" and media_row.meta_image_hash:
        return media_row
    if media_row.kind == "video" and media_row.meta_video_id:
        return media_row
    token = company.get_meta_access_token() if hasattr(company, "get_meta_access_token") else None
    ad_account_id = getattr(company, "meta_ad_account_id", None)
    if not token or not ad_account_id:
        raise meta_api.MetaAPIError({"message": "Meta reklama hisobi ulanmagan -- media yuklab bo'lmaydi."})
    path = media_file_path(media_row)
    try:
        data = path.read_bytes()
    except OSError as e:
        media_row.upload_status = "failed"
        media_row.upload_error = "Fayl diskda topilmadi -- qayta yuklang."
        session.commit()
        raise meta_api.MetaAPIError({"message": media_row.upload_error}) from e
    try:
        if media_row.kind == "image":
            result = meta_api.upload_ad_image(ad_account_id, media_row.filename or "image.jpg", data, access_token=token)
            media_row.meta_image_hash = result["hash"]
        else:
            result = meta_api.upload_ad_video(ad_account_id, media_row.filename or "video.mp4", data, access_token=token)
            media_row.meta_video_id = result["id"]
        media_row.upload_status = "uploaded"
        media_row.upload_error = None
        _log_event(session, media_row, "media_uploaded", {"media_id": media_row.id, "kind": media_row.kind, "hash": media_row.meta_image_hash, "video_id": media_row.meta_video_id})
        session.commit()
        return media_row
    except meta_api.MetaAPIError as e:
        media_row.upload_status = "failed"
        media_row.upload_error = meta_api.safe_error_message(e)
        _log_event(session, media_row, "meta_error", {"media_id": media_row.id, "step": "upload_media", "error": media_row.upload_error})
        session.commit()
        raise
    except Exception as e:  # noqa: BLE001 -- tarmoq/proxy xatosi: token sizmasin (safe_error_message)
        media_row.upload_status = "failed"
        media_row.upload_error = meta_api.safe_error_message(e)
        _log_event(session, media_row, "meta_error", {"media_id": media_row.id, "step": "upload_media", "error": media_row.upload_error})
        session.commit()
        raise meta_api.MetaAPIError({"message": media_row.upload_error}) from e
