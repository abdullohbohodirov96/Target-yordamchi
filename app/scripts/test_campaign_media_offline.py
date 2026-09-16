"""test_campaign_media_offline.py — Meta Ads Autopilot (2026-09,
foydalanuvchi so'rovi: "Replix ... Ads Manager'dagi har bir maydonni
boshidan o'zi to'ldirmasligi kerak"): `campaign_media.py`:

  1. `save_uploaded_media()` -- PIL bilan yaratilgan rasm -> qator
     width/height bilan, fayl MEDIA_ROOT/<company>/<draft>/ ostida.
  2. `.exe` (va noto'g'ri content-type) rad etiladi; hajm chegarasi.
  3. `ensure_uploaded_to_meta()` idempotent -- `upload_ad_image` ikki
     chaqiriqda BIR MARTA; xato bo'lsa `failed` + `upload_error` yoziladi
     va CampaignDraftEvent'ga `meta_error` tushadi.

Ishga tushirish:
    cd app && python3 scripts/test_campaign_media_offline.py
"""
import io
import os
import sys
import tempfile
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("META_ACCESS_TOKEN", "test-dummy-token")
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_test_dummy")
os.environ.setdefault("META_PAGE_ID", "page_test_dummy")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

_TMPDIR = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMPDIR, 'test_campaign_media.db')}"

from PIL import Image  # noqa: E402

import db as db_module  # noqa: E402
import meta_api  # noqa: E402
import campaign_media  # noqa: E402

db_module.init_db()
campaign_media.MEDIA_ROOT = Path(_TMPDIR) / "ad_media"

failures = []


def check(name, cond):
    print(("OK  " if cond else "FAIL") + " " + name)
    if not cond:
        failures.append(name)


def _png_bytes(w=640, h=480):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _setup():
    session = db_module.get_session()
    c = db_module.Company(name="Media Co", plan="business", is_active=True, meta_ad_account_id="act_77", meta_page_id="PAGE1")
    c.set_meta_access_token("tok_media")
    session.add(c)
    session.commit()
    d = db_module.CampaignDraft(company_id=c.id, title="T", objective="MESSAGES")
    session.add(d)
    session.commit()
    return session, c, d


def test_save_image_and_reject():
    session, c, d = _setup()
    try:
        row = campaign_media.save_uploaded_media(session, c.id, d.id, _png_bytes(), "banner.png", "image/png")
        check("rasm qatori yaratildi", row.id and row.kind == "image" and row.upload_status == "pending")
        check("width/height", row.width == 640 and row.height == 480)
        check("size_bytes", row.size_bytes == len(_png_bytes()))
        path = campaign_media.media_file_path(row)
        check("fayl diskda", path.exists() and str(path).startswith(str(campaign_media.MEDIA_ROOT / str(c.id) / str(d.id))))
        check("storage_path nisbiy", not os.path.isabs(row.storage_path))

        # FileStorage-o'xshash obyekt
        class FS:
            def __init__(self, data):
                self.stream = io.BytesIO(data)
        row2 = campaign_media.save_uploaded_media(session, c.id, d.id, FS(_png_bytes(10, 20)), "../../evil name.PNG", None)
        check("stream + xavfsiz nom + ext'dan tur", row2.width == 10 and row2.filename == "evil_name.PNG" and row2.content_type == "image/png")

        for fname, ct in (("virus.exe", "application/octet-stream"), ("doc.pdf", "application/pdf")):
            try:
                campaign_media.save_uploaded_media(session, c.id, d.id, b"MZ...", fname, ct)
                check(f"{fname} rad etiladi", False)
            except campaign_media.MediaError as e:
                check(f"{fname} rad etiladi", "Faqat rasm" in str(e))
        try:
            campaign_media.save_uploaded_media(session, c.id, d.id, b"notanimage", "x.png", "image/png")
            check("buzilgan rasm rad etiladi", False)
        except campaign_media.MediaError:
            check("buzilgan rasm rad etiladi", True)
        with mock.patch.object(campaign_media, "MAX_IMAGE_BYTES", 100):
            try:
                campaign_media.save_uploaded_media(session, c.id, d.id, _png_bytes(), "big.png", "image/png")
                check("hajm chegarasi", False)
            except campaign_media.MediaError as e:
                check("hajm chegarasi", "juda katta" in str(e))
        try:
            campaign_media.save_uploaded_media(session, c.id, d.id, b"", "empty.png", "image/png")
            check("bo'sh fayl rad etiladi", False)
        except campaign_media.MediaError:
            check("bo'sh fayl rad etiladi", True)
    finally:
        session.close()


def test_ensure_uploaded_idempotent_and_failure():
    session, c, d = _setup()
    try:
        row = campaign_media.save_uploaded_media(session, c.id, d.id, _png_bytes(), "a.png", "image/png")
        with mock.patch.object(meta_api, "upload_ad_image", return_value={"hash": "HASH1", "url": "u"}) as up:
            campaign_media.ensure_uploaded_to_meta(session, row, c)
            campaign_media.ensure_uploaded_to_meta(session, row, c)
        check("upload_ad_image bir marta", up.call_count == 1)
        check("chaqiruv kompaniya tokeni/hisobi bilan", up.call_args[0][0] == "act_77" and up.call_args[1]["access_token"] == "tok_media")
        check("hash saqlandi, status uploaded", row.meta_image_hash == "HASH1" and row.upload_status == "uploaded")
        with db_module.scoped_as(c.id):
            ev = session.query(db_module.CampaignDraftEvent).filter_by(draft_id=d.id, action="media_uploaded").all()
        check("media_uploaded event", len(ev) == 1)

        row2 = campaign_media.save_uploaded_media(session, c.id, d.id, _png_bytes(), "b.png", "image/png")
        with mock.patch.object(meta_api, "upload_ad_image", side_effect=meta_api.MetaAPIError({"message": "Invalid image", "code": 100})):
            try:
                campaign_media.ensure_uploaded_to_meta(session, row2, c)
                check("xato qayta ko'tariladi", False)
            except meta_api.MetaAPIError:
                check("xato qayta ko'tariladi", True)
        check("failed + upload_error", row2.upload_status == "failed" and row2.upload_error == "Invalid image")
        with db_module.scoped_as(c.id):
            ev = session.query(db_module.CampaignDraftEvent).filter_by(draft_id=d.id, action="meta_error").all()
        check("meta_error event", len(ev) == 1 and ev[0].get_details()["step"] == "upload_media")

        # tarmoq xatosi -> token sizmaydi
        import requests
        row3 = campaign_media.save_uploaded_media(session, c.id, d.id, _png_bytes(), "c.png", "image/png")
        with mock.patch.object(meta_api, "upload_ad_image", side_effect=requests.exceptions.ConnectionError("https://x?access_token=tok_media")):
            try:
                campaign_media.ensure_uploaded_to_meta(session, row3, c)
            except meta_api.MetaAPIError:
                pass
        check("tarmoq xatosida token sizmaydi", "tok_media" not in (row3.upload_error or "") and row3.upload_status == "failed")

        # video
        buf = b"\x00" * 1000
        vrow = campaign_media.save_uploaded_media(session, c.id, d.id, buf, "clip.mp4", "video/mp4")
        check("video qatori", vrow.kind == "video" and vrow.width is None)
        check("video size_bytes to'g'ri", vrow.size_bytes == len(buf))

        # 2026-09: video limit 300 MB'ga oshirildi, endi bo'lak-bo'lak
        # diskka yoziladi (_stream_video_to_disk) -- xotiraga to'liq
        # yig'ilmasligini va limitni to'g'ri qo'llashini tekshiramiz.
        check("MAX_VIDEO_BYTES = 300 MB", campaign_media.MAX_VIDEO_BYTES == 300 * 1024 * 1024)

        class _FakeStream:
            """.stream.read(n) orqali bo'lak-bo'lak beradigan soxta yuklama."""
            def __init__(self, data, chunk=64):
                self._chunks = [data[i:i + chunk] for i in range(0, len(data), chunk)] or [b""]
                self.stream = self

            def read(self, n=-1):
                if not self._chunks:
                    return b""
                return self._chunks.pop(0)

        with mock.patch.object(campaign_media, "MAX_VIDEO_BYTES", 500):
            try:
                campaign_media.save_uploaded_media(session, c.id, d.id, _FakeStream(b"\x01" * 2000), "big.mp4", "video/mp4")
                check("video hajm chegarasi (stream)", False)
            except campaign_media.MediaError as e:
                check("video hajm chegarasi (stream)", "juda katta" in str(e))
            leftover = [p for p in (campaign_media.MEDIA_ROOT / str(c.id) / str(d.id)).glob("*big.mp4")]
            check("limitdan oshgan chala fayl o'chirilgan", not leftover)

        vrow2 = campaign_media.save_uploaded_media(session, c.id, d.id, _FakeStream(b"\x02" * 5000, chunk=777), "ok.mp4", "video/mp4")
        check("stream orqali video to'g'ri saqlandi", vrow2.size_bytes == 5000 and campaign_media.media_file_path(vrow2).stat().st_size == 5000)
        with mock.patch.object(meta_api, "upload_ad_video", return_value={"id": "VID9"}) as upv:
            campaign_media.ensure_uploaded_to_meta(session, vrow, c)
            campaign_media.ensure_uploaded_to_meta(session, vrow, c)
        check("video bir marta yuklanadi, id saqlanadi", upv.call_count == 1 and vrow.meta_video_id == "VID9")

        # Meta ulanmagan kompaniya
        c2 = db_module.Company(name="No Meta", plan="trial", is_active=True)
        session.add(c2)
        session.commit()
        row4 = campaign_media.save_uploaded_media(session, c2.id, d.id, _png_bytes(), "d.png", "image/png")
        try:
            campaign_media.ensure_uploaded_to_meta(session, row4, c2)
            check("Meta ulanmagan -> xato", False)
        except meta_api.MetaAPIError as e:
            check("Meta ulanmagan -> xato", "ulanmagan" in meta_api.safe_error_message(e))
    finally:
        session.close()


test_save_image_and_reject()
test_ensure_uploaded_idempotent_and_failure()

print()
if failures:
    print(f"MUVAFFAQIYATSIZ: {len(failures)} ta test o'tmadi:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("BARCHA TESTLAR O'TDI (campaign_media)")
