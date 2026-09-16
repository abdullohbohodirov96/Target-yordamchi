"""test_storage_backend_offline.py — `storage_backend.py` (2026-09,
Cloudflare R2 doimiy saqlash qatlami):

  1. `enabled()` -- to'rtta muhit o'zgaruvchisi HAMMASI bo'lmasa `False`
     (qisman kombinatsiyalarda ham).
  2. R2 O'CHIQ holatda `upload_file`/`download_file`/`delete_object`/
     `delete_prefix` -- tarmoqqa chiqmasdan (`boto3.client` chaqirilmasdan)
     `False`/no-op.
  3. R2 YOQILGAN (soxta kalitlar) holatda -- `boto3.client` mock qilinib:
     muvaffaqiyatli chaqiruv, xato ko'tarilganda ushlab `False` qaytarish,
     to'g'ri bucket/key bilan chaqirilishi.
  4. `ensure_local()` -- lokal fayl bor/yo'q x R2 yoqilgan/o'chiq --
     to'rtta kombinatsiya.

Ishga tushirish:
    cd app && python3 scripts/test_storage_backend_offline.py
"""
import os
import sys
import tempfile
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# R2 muhit o'zgaruvchilarini boshida TOZA holatga keltiramiz (boshqa
# testlar/CI muhitidan sizib qolmasin).
for _k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME", "R2_ENDPOINT_URL"):
    os.environ.pop(_k, None)

import storage_backend  # noqa: E402

_TMPDIR = Path(tempfile.mkdtemp())

failures = []


def check(name, cond):
    print(("OK  " if cond else "FAIL") + " " + name)
    if not cond:
        failures.append(name)


def _clear_r2_env():
    for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME", "R2_ENDPOINT_URL"):
        os.environ.pop(k, None)


def _set_full_r2_env():
    os.environ["R2_ACCOUNT_ID"] = "acc123"
    os.environ["R2_ACCESS_KEY_ID"] = "AKIA_TEST"
    os.environ["R2_SECRET_ACCESS_KEY"] = "secret_test"
    os.environ["R2_BUCKET_NAME"] = "test-bucket"


def test_enabled_combinations():
    _clear_r2_env()
    try:
        check("hech biri yo'q -> False", storage_backend.enabled() is False)

        os.environ["R2_ACCOUNT_ID"] = "a"
        check("faqat 1/4 -> False", storage_backend.enabled() is False)

        os.environ["R2_ACCESS_KEY_ID"] = "b"
        os.environ["R2_SECRET_ACCESS_KEY"] = "c"
        check("3/4 (bucket yo'q) -> False", storage_backend.enabled() is False)

        os.environ["R2_BUCKET_NAME"] = "d"
        check("4/4 -> True", storage_backend.enabled() is True)
    finally:
        _clear_r2_env()


def test_disabled_no_network():
    _clear_r2_env()
    try:
        with mock.patch("boto3.client") as bc:
            local = _TMPDIR / "somefile.png"
            local.write_bytes(b"x")
            check("upload_file False (o'chiq)", storage_backend.upload_file(local, "k") is False)
            check("download_file False (o'chiq)", storage_backend.download_file("k", _TMPDIR / "out.png") is False)
            check("delete_object False (o'chiq)", storage_backend.delete_object("k") is False)
            check("delete_prefix False (o'chiq)", storage_backend.delete_prefix("p/") is False)
            check("boto3.client HECH chaqirilmadi (o'chiq)", bc.call_count == 0)
    finally:
        _clear_r2_env()


def test_enabled_upload_download_delete_mocked():
    _set_full_r2_env()
    try:
        # --- upload_file muvaffaqiyatli ---
        fake_client = mock.MagicMock()
        with mock.patch("boto3.client", return_value=fake_client) as bc:
            local = _TMPDIR / "logo.png"
            local.write_bytes(b"PNGDATA")
            ok = storage_backend.upload_file(local, "brand_kit/1/logo.png")
            check("upload_file True (yoqilgan)", ok is True)
            check("boto3.client chaqirildi (endpoint bilan)", bc.call_count == 1)
            args, kwargs = fake_client.upload_file.call_args
            check("upload_file to'g'ri path/bucket/key bilan chaqirildi", args[0] == str(local) and args[1] == "test-bucket" and args[2] == "brand_kit/1/logo.png")

        # --- upload_file xato -> False, ko'tarilmaydi ---
        fake_client2 = mock.MagicMock()
        fake_client2.upload_file.side_effect = Exception("network down")
        with mock.patch("boto3.client", return_value=fake_client2):
            ok = storage_backend.upload_file(local, "brand_kit/1/logo.png")
            check("upload_file xato -> False, ko'tarilmaydi", ok is False)

        # --- download_file muvaffaqiyatli ---
        def _fake_download_file(bucket, key, dest, *a, **kw):
            Path(dest).write_bytes(b"DOWNLOADED")

        fake_client3 = mock.MagicMock()
        fake_client3.download_file.side_effect = _fake_download_file
        dest = _TMPDIR / "sub" / "downloaded.png"
        with mock.patch("boto3.client", return_value=fake_client3):
            ok = storage_backend.download_file("brand_kit/1/logo.png", dest)
            check("download_file True (yoqilgan)", ok is True)
            check("download_file fayl haqiqatan yozildi", dest.exists() and dest.read_bytes() == b"DOWNLOADED")
            args, kwargs = fake_client3.download_file.call_args
            check("download_file to'g'ri bucket/key/path bilan chaqirildi", args[0] == "test-bucket" and args[1] == "brand_kit/1/logo.png" and args[2] == str(dest))

        # --- download_file "topilmadi" (ClientError) -> False, jim ---
        import botocore.exceptions
        fake_client4 = mock.MagicMock()
        fake_client4.download_file.side_effect = botocore.exceptions.ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "GetObject"
        )
        missing_dest = _TMPDIR / "missing.png"
        with mock.patch("boto3.client", return_value=fake_client4):
            ok = storage_backend.download_file("brand_kit/1/nope.png", missing_dest)
            check("download_file topilmasa -> False, ko'tarilmaydi", ok is False)
            check("download_file topilmasa -> fayl yaratilmagan", not missing_dest.exists())

        # --- delete_object ---
        fake_client5 = mock.MagicMock()
        with mock.patch("boto3.client", return_value=fake_client5):
            ok = storage_backend.delete_object("ad_media/1/2/x.jpg")
            check("delete_object True", ok is True)
            check("delete_object to'g'ri bucket/key bilan chaqirildi", fake_client5.delete_object.call_args[1] == {"Bucket": "test-bucket", "Key": "ad_media/1/2/x.jpg"})

        fake_client6 = mock.MagicMock()
        fake_client6.delete_object.side_effect = Exception("boom")
        with mock.patch("boto3.client", return_value=fake_client6):
            ok = storage_backend.delete_object("x")
            check("delete_object xato -> False", ok is False)

        # --- delete_prefix ---
        fake_client7 = mock.MagicMock()
        paginator = mock.MagicMock()
        paginator.paginate.return_value = [
            {"Contents": [{"Key": "creative_studio/1/2/base.png"}, {"Key": "creative_studio/1/2/final.png"}]}
        ]
        fake_client7.get_paginator.return_value = paginator
        with mock.patch("boto3.client", return_value=fake_client7):
            ok = storage_backend.delete_prefix("creative_studio/1/2/")
            check("delete_prefix True", ok is True)
            check("delete_prefix ommaviy o'chirish chaqirildi", fake_client7.delete_objects.call_count == 1)
            del_kwargs = fake_client7.delete_objects.call_args[1]
            check("delete_prefix to'g'ri kalitlar bilan", {o["Key"] for o in del_kwargs["Delete"]["Objects"]} == {"creative_studio/1/2/base.png", "creative_studio/1/2/final.png"})

        # --- delete_prefix xato -> False ---
        fake_client8 = mock.MagicMock()
        fake_client8.get_paginator.side_effect = Exception("boom")
        with mock.patch("boto3.client", return_value=fake_client8):
            ok = storage_backend.delete_prefix("x/")
            check("delete_prefix xato -> False", ok is False)
    finally:
        _clear_r2_env()


def test_ensure_local_combinations():
    root = _TMPDIR / "ensure_root"
    root.mkdir(parents=True, exist_ok=True)

    # 1) fayl mavjud + R2 o'chiq -> shu yo'l, download urinilmaydi
    _clear_r2_env()
    existing_rel = "a/exists.png"
    (root / "a").mkdir(parents=True, exist_ok=True)
    (root / existing_rel).write_bytes(b"here")
    with mock.patch("boto3.client") as bc:
        p = storage_backend.ensure_local(root, existing_rel, key_prefix="ad_media")
        check("ensure_local: mavjud+o'chiq -> to'g'ri Path", p == root / existing_rel)
        check("ensure_local: mavjud+o'chiq -> download urinilmagan", bc.call_count == 0)

    # 2) fayl mavjud + R2 yoqilgan -> shu yo'l, download urinilmaydi (tezkor yo'l)
    _set_full_r2_env()
    try:
        with mock.patch("boto3.client") as bc:
            p = storage_backend.ensure_local(root, existing_rel, key_prefix="ad_media")
            check("ensure_local: mavjud+yoqilgan -> to'g'ri Path", p == root / existing_rel)
            check("ensure_local: mavjud+yoqilgan -> download urinilmagan", bc.call_count == 0)
    finally:
        _clear_r2_env()

    # 3) fayl yo'q + R2 o'chiq -> shu yo'l qaytadi, download urinilmaydi
    missing_rel = "b/missing.png"
    _clear_r2_env()
    with mock.patch("boto3.client") as bc:
        p = storage_backend.ensure_local(root, missing_rel, key_prefix="ad_media")
        check("ensure_local: yo'q+o'chiq -> to'g'ri Path (mavjud emas)", p == root / missing_rel and not p.exists())
        check("ensure_local: yo'q+o'chiq -> download urinilmagan", bc.call_count == 0)

    # 4) fayl yo'q + R2 yoqilgan + download muvaffaqiyatli
    _set_full_r2_env()
    try:
        def _fake_download_file(bucket, key, dest, *a, **kw):
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_bytes(b"FROM_R2")

        fake_client = mock.MagicMock()
        fake_client.download_file.side_effect = _fake_download_file
        with mock.patch("boto3.client", return_value=fake_client):
            p = storage_backend.ensure_local(root, missing_rel, key_prefix="ad_media")
            check("ensure_local: yo'q+yoqilgan+muvaffaqiyat -> Path", p == root / missing_rel)
            check("ensure_local: yo'q+yoqilgan+muvaffaqiyat -> fayl yaratildi", p.exists() and p.read_bytes() == b"FROM_R2")
            args, kwargs = fake_client.download_file.call_args
            check("ensure_local: to'g'ri key (prefix/rel_path)", args[1] == f"ad_media/{missing_rel}")

        # 5) fayl yo'q + R2 yoqilgan + download muvaffaqiyatsiz -> Path qaytadi, .exists() False
        missing_rel2 = "c/still_missing.png"
        fake_client2 = mock.MagicMock()
        fake_client2.download_file.side_effect = Exception("network fail")
        with mock.patch("boto3.client", return_value=fake_client2):
            p2 = storage_backend.ensure_local(root, missing_rel2, key_prefix="ad_media")
            check("ensure_local: yo'q+yoqilgan+xato -> Path baribir qaytadi", p2 == root / missing_rel2)
            check("ensure_local: yo'q+yoqilgan+xato -> fayl yaratilmagan", not p2.exists())
    finally:
        _clear_r2_env()


test_enabled_combinations()
test_disabled_no_network()
test_enabled_upload_download_delete_mocked()
test_ensure_local_combinations()

print()
if failures:
    print(f"MUVAFFAQIYATSIZ: {len(failures)} ta test o'tmadi:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("BARCHA TESTLAR O'TDI (storage_backend)")
