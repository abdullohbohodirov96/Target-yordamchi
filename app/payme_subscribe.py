"""payme_subscribe.py -- Payme SUBSCRIBE API mijozi.

2026-09, foydalanuvchi so'rovi: "tolov avtomatik otishi uchun ... tolov
otkanini tekshirib boladigan qilish" -- ya'ni oylik obuna to'lovini mijozning
KARTASIDAN AVTOMATIK yechib olish (mijoz har oy qaytadan to'lamasin).

BU -- Payme MERCHANT API (checkout-havola + kiruvchi webhook) DAN BOSHQA,
FARQLI oqim: Merchant API'da Payme BIZNING serverimizga so'rov yuboradi
(masalan kelajakda checkout-orqali-birmarta-to'lov kerak bo'lsa, buni ALOHIDA
`/webhooks/payme` endpoint sifatida qo'shish mumkin). Subscribe API'da esa,
aksincha, BIZNING serverimiz Payme'ga JSON-RPC so'rov yuboradi -- hech qanday
kiruvchi webhook shart emas, chunki har bir chaqiruv (`receipts.pay` va h.k.)
darhol javob bilan qaytadi.

OQIM:
  1. `create_card(pan, expire)` -- mijozning kartasini TOKENLASHTIRADI.
     Javobda `verify_needed=True` bo'lsa, karta SMS orqali tasdiqlanishi
     kerak (odatiy holat).
  2. `get_verify_code(token)` -- kartaga bog'liq telefon raqamiga SMS kod
     yuboradi.
  3. `verify_card(token, code)` -- SMS kodni tasdiqlaydi -- token ENDI
     ko'p martalik (recurrent) to'lovlar uchun tayyor va DOIMIY saqlanadi
     (`Company.set_payme_card_token()`, shifrlangan holda).
  4. Har oy (`scheduler.job_payme_autopay()`): `create_receipt(...)` --
     "hisob-faktura" yaratadi, keyin `pay_receipt(receipt_id, token)` --
     saqlangan token bilan shu hisobni AVTOMATIK to'laydi.
  5. `remove_card(token)` -- mijoz kartasini uzmoqchi (avtoto'lovni
     o'chirmoqchi) bo'lsa.

MUHIM (PCI-DSS eslatma, MUHOKAMA QILINMAGAN OCHIQ SAVOL): quyidagi
`create_card()` xom karta raqamini BIZNING backend serverimiz orqali
Payme'ga yuboradi (JSON-RPC `cards.create`, parametr sifatida). Bu eng
sodda/tezkor yo'l va Payme'ning rasmiy hujjatida aynan shu parametrlar
(`card.number`, `card.expire`) ko'rsatilgan -- LEKIN Payme, shu bilan bir
qatorda, karta kiritish FORMASI uchun alohida talablar ham qo'yadi ("name"
va "action" atributlarisiz forma, Payme logotipi va shartlar havolasi) --
bu odatda karta raqami BROWSER'dan TO'G'RIDAN-TO'G'RI (bizning serverimizga
tegmasdan) Payme'ning o'z JS-widget'iga ketishi kerakligini bildiradi.
Payme rasman ulanish tasdiqlangach, ularning texnik integratsiya bo'limi
bilan ANIQ qaysi usul (server orqali to'g'ridan-to'g'ri, yoki rasmiy
JS-checkout widget) tavsiya etilishini albatta tasdiqlab oling -- men buni
ochiq hujjatlardan to'liq aniqlay olmadim. Xavfsiz tomondan xato qilish
uchun, kod QAYERDA BO'LMASIN xom karta raqamini SAQLAMAYDI (faqat darhol
Payme'ga jo'natadi va javobdagi TOKENni saqlaydi).
"""

import os
import base64
import logging

import requests

logger = logging.getLogger("payme_subscribe")

PAYME_MERCHANT_ID = os.environ.get("PAYME_MERCHANT_ID", "").strip()
PAYME_KEY = os.environ.get("PAYME_KEY", "").strip()  # PROD (haqiqiy) kalit
PAYME_TEST_KEY = os.environ.get("PAYME_TEST_KEY", "").strip()  # sinov (test) kalit
PAYME_TEST_MODE = os.environ.get("PAYME_TEST_MODE", "true").strip().lower() in ("1", "true", "yes", "on")
# `plans.py`dagi narxlar USD'da, Payme esa FAQAT so'm (tiyin) bilan ishlaydi --
# platforma egasi shu kursni ENV orqali sozlaydi (masalan haqiqiy bank kursiga
# yaqin qiymatga). Standart qiymat -- taxminiy, ishga tushirishdan oldin
# albatta joriy kursga moslab o'rnating.
PAYME_USD_TO_UZS_RATE = float(os.environ.get("PAYME_USD_TO_UZS_RATE", "12700") or "12700")

_PROD_URL = "https://checkout.paycom.uz/api"
_TEST_URL = "https://checkout.test.paycom.uz/api"

_REQUEST_TIMEOUT_SECONDS = 20


class PaymeSubscribeError(Exception):
    """Payme JSON-RPC xato qaytarganda (yoki tarmoq/ulanish xatosida)
    ko'tariladi. `code` -- Payme'ning o'z xato kodi (masalan karta rad
    etilgan, mablag' yetarli emas va h.k.), agar mavjud bo'lsa."""

    def __init__(self, message: str, *, code=None, raw: "dict | None" = None):
        super().__init__(message)
        self.code = code
        self.raw = raw


def is_configured() -> bool:
    return bool(PAYME_MERCHANT_ID and (PAYME_TEST_KEY if PAYME_TEST_MODE else PAYME_KEY))


def _active_key() -> str:
    return PAYME_TEST_KEY if PAYME_TEST_MODE else PAYME_KEY


def _base_url() -> str:
    return _TEST_URL if PAYME_TEST_MODE else _PROD_URL


def usd_to_tiyin(price_usd: "int | float") -> int:
    """Dollar narxini Payme kutgan "tiyin" (1 so'm = 100 tiyin) birligiga
    o'tkazadi -- oylik obuna narxi USD'da (`plans.py`) saqlanadi."""
    uzs = round(price_usd * PAYME_USD_TO_UZS_RATE)
    return uzs * 100


def _call(method: str, params: dict) -> dict:
    if not is_configured():
        raise PaymeSubscribeError(
            "Payme ulanmagan -- PAYME_MERCHANT_ID va PAYME_KEY/PAYME_TEST_KEY "
            "ENV o'zgaruvchilari o'rnatilmagan."
        )
    try:
        resp = requests.post(
            _base_url(),
            json={"id": 1, "method": method, "params": params},
            headers={
                "X-Auth": f"{PAYME_MERCHANT_ID}:{_active_key()}",
                "Content-Type": "application/json",
            },
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        data = resp.json()
    except Exception as e:
        logger.exception("Payme Subscribe API so'rovida tarmoq xatosi (%s)", method)
        raise PaymeSubscribeError(f"Payme'ga ulanishda xato: {e}") from e

    error = data.get("error")
    if error:
        message = error.get("message")
        if isinstance(message, dict):
            message = message.get("uz") or message.get("ru") or message.get("en") or str(message)
        raise PaymeSubscribeError(message or f"Payme xatosi ({method})", code=error.get("code"), raw=data)

    return data.get("result") or {}


def create_card(pan: str, expire: str, *, save: bool = True) -> dict:
    """Kartani tokenlashtiradi. `pan` -- karta raqami (bo'shliqsiz),
    `expire` -- "MMYY" formatida amal qilish muddati (masalan "1229").
    Qaytaradi: {"token", "masked", "verify_needed", "recurrent"}."""
    result = _call("cards.create", {"card": {"number": pan, "expire": expire}, "save": save})
    card = result.get("card") or {}
    return {
        "token": card.get("token"),
        "masked": card.get("number"),
        "verify_needed": bool(card.get("verify")),
        "recurrent": bool(card.get("recurrent")),
    }


def get_verify_code(token: str) -> dict:
    """Kartaga bog'langan telefon raqamiga SMS tasdiqlash kodini yuboradi."""
    return _call("cards.get_verify_code", {"token": token})


def verify_card(token: str, code: str) -> dict:
    """SMS kodni tasdiqlaydi -- muvaffaqiyatli bo'lsa token ENDI ko'p
    martalik (recurrent) avtomatik to'lovlar uchun tayyor."""
    result = _call("cards.verify", {"token": token, "code": code})
    card = result.get("card") or {}
    return {
        "token": card.get("token") or token,
        "masked": card.get("number"),
        "recurrent": bool(card.get("recurrent", True)),
    }


def remove_card(token: str) -> dict:
    return _call("cards.remove", {"token": token})


def create_receipt(amount_tiyin: int, order_id: str, description: str) -> dict:
    """Bitta oylik to'lov uchun "hisob-faktura" (receipt) yaratadi.
    Qaytaradi: {"id", "state"}."""
    result = _call("receipts.create", {
        "amount": amount_tiyin,
        "account": {"order_id": order_id},
        "description": description,
    })
    receipt = result.get("receipt") or {}
    return {"id": receipt.get("_id"), "state": receipt.get("state")}


def pay_receipt(receipt_id: str, token: str) -> dict:
    """Saqlangan (recurrent) token bilan berilgan hisobni AVTOMATIK to'laydi.
    Muvaffaqiyatsiz bo'lsa `PaymeSubscribeError` ko'taradi (masalan karta
    muddati o'tgan, mablag' yetarli emas). Muvaffaqiyat -- `pay_time`
    musbat (0 emas) qiymat bilan qaytadi."""
    result = _call("receipts.pay", {"id": receipt_id, "token": token})
    receipt = result.get("receipt") or {}
    return {
        "id": receipt.get("_id") or receipt_id,
        "state": receipt.get("state"),
        "pay_time": receipt.get("pay_time"),
    }


def check_receipt(receipt_id: str) -> dict:
    result = _call("receipts.check", {"id": receipt_id})
    return {"state": result.get("state")}
