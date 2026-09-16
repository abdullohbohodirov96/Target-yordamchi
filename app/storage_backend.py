"""storage_backend.py — Cloudflare R2 (S3-mos) orqali DOIMIY saqlash
qatlami (2026-09, Render "starter" tarifi muammosi: disk EFEMER -- har
deploy'da `uploads/` papkasi TOZALANADI, shu sabab foydalanuvchilar
yuklagan brend logotipi va kampaniya media fayllari yo'qolib qolardi).

ARXITEKTURA -- "LOKAL DISK BIRINCHI, R2 -- ZAXIRA":
  Kod HECH QACHON to'g'ridan-to'g'ri R2'dan o'qimaydi/ishlamaydi. Lokal
  disk avvalgidek TEZKOR yo'l bo'lib qoladi -- barcha mavjud PIL/atomik
  yozish/`.glob()`/`send_file()` kodi o'zgarishsiz ishlaydi:
    1. Fayl DISKKA yozilgandan KEYIN (muvaffaqiyatli), uning nusxasi R2'ga
       ham yuklanadi (`upload_file`) -- "eng yaxshi urinish": xato bo'lsa
       faqat `log.warning`, HECH QACHON asosiy oqimni to'xtatmaydi.
    2. Fayl o'qilishidan OLDIN, `ensure_local()` chaqiriladi: fayl diskda
       bor bo'lsa -- hech narsa qilmaydi (tezkor); yo'q bo'lsa (masalan
       yangi deploy'dan keyin) VA R2 sozlangan bo'lsa -- R2'dan diskka
       tortib oladi ("self-heal"). R2 sozlanmagan yoki u yerda ham yo'q
       bo'lsa -- yo'l shunchaki qaytariladi, chaqiruvchi o'zining odatiy
       `.exists()` tekshiruvini qiladi (xatti-harakat BUGUNGIDEK qoladi).
    3. O'chirishda R2'dagi nusxa ham (eng yaxshi urinishda) o'chiriladi.

XAVFSIZ NO-OP: quyidagi TO'RTTA muhit o'zgaruvchisi (`R2_ACCOUNT_ID`,
`R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME`) HAMMASI
sozlanmagunicha `enabled()` -- `False`, va bu modul hech qanday tarmoq
so'rovi yubormaydi -- barcha funksiyalar darhol `False`/no-op qaytaradi.
Demak bu fayl production'ga TUSHGANDA ham (hozirgi holat -- R2
sozlanmagan) mavjud xatti-harakatni ZARRACHA o'zgartirmaydi. Foydalanuvchi
R2 hisobini ulab shu 4 ta o'zgaruvchini Render Dashboard'ga qo'yishi bilan
saqlash avtomatik doimiy bo'lib qoladi -- kod o'zgartirish shart emas.
"""

import logging
from pathlib import Path

logger = logging.getLogger("storage_backend")


def enabled() -> bool:
    """R2 uchun kerakli TO'RTTA muhit o'zgaruvchisi hammasi sozlanganmi.

    Testlar/deploy bu holatni istalgan vaqtda o'zgartirishi mumkin --
    shuning uchun bu funksiya muhit o'zgaruvchilarini modul yuklanganda
    emas, HAR CHAQIRUVDA (`os.environ.get`) o'qiydi, xuddi
    `campaign_media.MEDIA_ROOT` kabi."""
    import os
    return bool(
        os.environ.get("R2_ACCOUNT_ID")
        and os.environ.get("R2_ACCESS_KEY_ID")
        and os.environ.get("R2_SECRET_ACCESS_KEY")
        and os.environ.get("R2_BUCKET_NAME")
    )


def _bucket_name() -> "str | None":
    import os
    return os.environ.get("R2_BUCKET_NAME")


def _build_client():
    """Yangi boto3 S3 mijozini yaratadi (R2 endpoint bilan). Bu -- issiq
    yo'l EMAS (yuklash/tushirish tarmoq bilan chegaralangan), shuning
    uchun soddalik uchun HAR CHAQIRUVDA qayta yaratiladi -- kesh yo'q,
    demak kalitlar o'zgarsa (masalan testda) darhol qo'llaniladi."""
    import os
    import boto3
    account_id = os.environ.get("R2_ACCOUNT_ID")
    endpoint_url = os.environ.get("R2_ENDPOINT_URL") or f"https://{account_id}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=os.environ.get("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )


def upload_file(local_path: Path, key: str) -> bool:
    """`local_path`dagi faylni R2'ga `key` ostida yuklaydi (eng yaxshi
    urinish). R2 sozlanmagan, fayl topilmagan yoki har qanday
    tarmoq/autentifikatsiya xatosi bo'lsa -- `False` (HECH QACHON
    ko'tarmaydi, faqat `log.warning`)."""
    if not enabled():
        return False
    try:
        client = _build_client()
        client.upload_file(str(local_path), _bucket_name(), key)
        return True
    except Exception as e:  # noqa: BLE001 -- R2 ishlamasa asosiy oqim to'xtamasin
        logger.warning("storage_backend: R2'ga yuklab bo'lmadi (%s -> %s): %s", local_path, key, e)
        return False


def download_file(key: str, local_path: Path) -> bool:
    """R2'dagi `key`ni `local_path`ga tushiradi (ota papkalarni yaratib).
    Faqat HAQIQATAN muvaffaqiyatli tushirilganda `True` qaytaradi (fayl
    R2'da topilmasa yoki har qanday xato bo'lsa -- `False`, HECH QACHON
    ko'tarmaydi)."""
    if not enabled():
        return False
    try:
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        client = _build_client()
        client.download_file(_bucket_name(), key, str(local_path))
        return local_path.exists() and local_path.stat().st_size > 0
    except Exception as e:  # noqa: BLE001 -- topilmasa/tarmoq xatosi -- jim False
        logger.warning("storage_backend: R2'dan tushirib bo'lmadi (%s): %s", key, e)
        return False


def delete_object(key: str) -> bool:
    """R2'dagi `key`ni o'chiradi (eng yaxshi urinish). Topilmagan holat ham
    "muvaffaqiyat" hisoblanadi (S3-mos API'da `delete_object` allaqachon
    shunday ishlaydi -- 404 emas, jim muvaffaqiyat qaytaradi)."""
    if not enabled():
        return False
    try:
        client = _build_client()
        client.delete_object(Bucket=_bucket_name(), Key=key)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("storage_backend: R2'dan o'chirib bo'lmadi (%s): %s", key, e)
        return False


def delete_prefix(prefix: str) -> bool:
    """`prefix` ostidagi BARCHA R2 obyektlarini o'chiradi (butun kreativ
    papkasi o'chirilganda ishlatiladi). Ro'yxat + ommaviy o'chirish; eng
    yaxshi urinish -- xato bo'lsa `False`, HECH QACHON ko'tarmaydi."""
    if not enabled():
        return False
    try:
        client = _build_client()
        bucket = _bucket_name()
        paginator = client.get_paginator("list_objects_v2")
        ok = True
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            keys = [{"Key": obj["Key"]} for obj in page.get("Contents") or []]
            if keys:
                client.delete_objects(Bucket=bucket, Delete={"Objects": keys})
        return ok
    except Exception as e:  # noqa: BLE001
        logger.warning("storage_backend: R2 prefiksini o'chirib bo'lmadi (%s): %s", prefix, e)
        return False


def ensure_local(root: Path, rel_path: str, *, key_prefix: str) -> Path:
    """`Path(root) / rel_path`ni qaytaradi. Fayl lokal diskda YO'Q bo'lsa
    VA R2 sozlangan bo'lsa -- avval R2'dan (`key = f"{key_prefix}/{rel_path}"`)
    o'sha aniq yo'lga tushirishga urinadi. Yuklab olish muvaffaqiyatli
    bo'ldimi yo'qmi -- BARIBIR shu `Path` qaytariladi (chaqiruvchi
    allaqachon o'zining `.exists()` tekshiruvini qiladi -- bu funksiya
    fayl HAQIQATAN hech qayerda bo'lmagan holatdagi bugungi xatti-harakatni
    ANIQ saqlaydi)."""
    path = Path(root) / (rel_path or "")
    if path.exists():
        return path
    if not enabled():
        return path
    key = f"{key_prefix}/{rel_path}"
    download_file(key, path)
    return path
