"""
crypto_util.py — 2026-09, "production-ready Meta Ads + CAPI integration"
so'rovi asosida qo'shildi: bazadagi maxfiy tokenlarni (Meta access token,
CAPI System User token) DISKDA/BAZADA HECH QACHON ochiq matn (plaintext)
holida saqlamaslik uchun kichik, mustaqil shifrlash yordamchisi.

Ilgari bu loyihada HECH QANDAY shifrlash infratuzilmasi yo'q edi
(`Company.meta_access_token` to'g'ridan-to'g'ri ochiq matn ustunida
saqlanardi) -- bu funksiya shu bo'shliqni to'ldiradi, `cryptography`
kutubxonasining Fernet (AES-128-CBC + HMAC, симметрик, autentifikatsiya
qilingan shifrlash) sxemasidan foydalanib.

Kalit manbai:
  1. `TOKEN_ENCRYPTION_KEY` environment o'zgaruvchisi -- PRODUCTION uchun
     TAVSIYA ETILGAN yo'l. Qiymat `Fernet.generate_key()` natijasi bo'lishi
     kerak (44 belgili urlsafe-base64 satr) -- buni bir marta generatsiya
     qilib, Render environment variable'ga qo'shish kifoya (qarang:
     docs/META_INTEGRATION_SETUP.md).
  2. Agar (1) sozlanmagan bo'lsa (masalan lokal ishlab chiqish/test muhiti) --
     ALLAQACHON MAJBURIY bo'lgan `FLASK_SECRET_KEY`dan DETERMINISTIK ravishda
     (SHA-256 orqali) bitta Fernet kaliti hosil qilinadi. Bu YANGI majburiy
     environment o'zgaruvchi qo'shmasdan, offline testlar va lokal ishga
     tushirishni buzmasdan ishlashni ta'minlaydi -- lekin PRODUCTION'da
     alohida, faqat shu maqsad uchun ishlatiladigan `TOKEN_ENCRYPTION_KEY`
     sozlash KUCHLI tavsiya etiladi (agar `FLASK_SECRET_KEY` biror sabab
     bilan almashtirilsa, undan hosil qilingan kalit ham o'zgarib, ESKI
     shifrlangan tokenlar o'qib bo'lmay qoladi).

MUHIM: bu modul HECH QACHON xato/xatolik matnida haqiqiy kalit yoki
shifrlangan/ochilgan qiymatni logga yozmaydi -- faqat umumiy xabar.
"""

import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("crypto_util")

_fernet_instance: Fernet | None = None


def _derive_key_from_secret(secret: str) -> bytes:
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _get_fernet() -> Fernet:
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance

    explicit_key = os.environ.get("TOKEN_ENCRYPTION_KEY", "").strip()
    if explicit_key:
        try:
            _fernet_instance = Fernet(explicit_key.encode("utf-8"))
            return _fernet_instance
        except Exception:
            logger.error(
                "TOKEN_ENCRYPTION_KEY noto'g'ri formatda (Fernet.generate_key() "
                "natijasi bo'lishi kerak) -- FLASK_SECRET_KEY'dan hosil qilingan "
                "zaxira kalitga o'tildi."
            )

    fallback_secret = os.environ.get("FLASK_SECRET_KEY", "") or "replix-dev-fallback-key"
    _fernet_instance = Fernet(_derive_key_from_secret(fallback_secret))
    return _fernet_instance


def encrypt_token(raw: "str | None") -> "str | None":
    """`raw` bo'sh/None bo'lsa -- `None` qaytaradi (bazada NULL saqlash
    uchun). Aks holda shifrlangan (urlsafe-base64) satrni qaytaradi."""
    if not raw:
        return None
    return _get_fernet().encrypt(raw.encode("utf-8")).decode("utf-8")


def decrypt_token(ciphertext: "str | None") -> "str | None":
    """Shifrini ochib, asl qiymatni qaytaradi.

    MIGRATSIYA MULOHAZASI: bu funksiya qo'shilishidan OLDIN
    `Company.meta_access_token` OCHIQ MATNDA saqlanardi (production
    bazasida ALLAQACHON shunday qatorlar bor). Fernet formatiga mos
    kelmagan qiymat (`InvalidToken`) uchun `None` qaytarish, deploy
    paytida BARCHA eski (hali shifrlanmagan) tokenlarni "yo'qolgan" deb
    ko'rsatib, ishlab turgan Meta ulanishlarini butunlay buzardi. Shuning
    uchun `InvalidToken` holatida -- xavfsiz yechim sifatida -- qiymat
    ESKI (hali shifrlanmagan) ochiq matn token deb hisoblanadi va
    O'ZGARTIRILMAY qaytariladi (keyingi `set_...` chaqiruvida avtomatik
    shifrlangan holga o'tadi). Agar bu haqiqatan ham buzilgan/mos
    kelmaydigan qiymat bo'lsa -- Meta API'ning o'zi keyinroq oddiy
    "noto'g'ri token" xatosi bilan rad etadi, hech qanday maxfiylik
    xavfi tug'dirmaydi (faqat funksional xato, xavfsizlik emas)."""
    if not ciphertext:
        return None
    try:
        return _get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return ciphertext
    except Exception:
        logger.exception("Token shifrini ochishda kutilmagan xatolik.")
        return ciphertext
