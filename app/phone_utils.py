"""
phone_utils.py — telefon raqamlarini normallashtirish/taqqoslash uchun
umumiy yordamchi funksiyalar. `app.py`dagi Excel import qismi va
`call_sync.py` (Mening qo'ng'iroqlarim integratsiyasi) ikkalasi ham shu
mantiqni ishlatadi -- lead va qo'ng'iroq yozuvi bir xil odamga tegishli
ekanini aniqlash uchun raqamlar bir xil "kalit"ga tushishi kerak."""

import re


def clean_phone_raw(raw):
    if raw is None:
        return None
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)
    s = str(raw).strip()
    if not s:
        return None
    if re.fullmatch(r"\d+\.0", s):
        s = s[:-2]
    if s.lower().startswith("p:"):
        s = s[2:]
    return s


def normalize_phone(raw):
    """Turli formatdagi telefon qiymatini imkon qadar "+998XXXXXXXXX"
    ko'rinishiga keltiradi. Aniqlab bo'lmasa, faqat raqamlarni qoldirib
    qaytaradi (yo'qotmaslik uchun)."""
    s = clean_phone_raw(raw)
    if not s:
        return None
    has_plus = s.strip().startswith("+")
    digits = re.sub(r"\D", "", s)
    if len(digits) < 7:
        return None
    if has_plus:
        return "+" + digits
    if digits.startswith("998") and len(digits) >= 12:
        return "+" + digits[:12]
    if len(digits) == 9:
        return "+998" + digits
    if len(digits) in (12, 13) and digits.startswith("998"):
        return "+" + digits
    return digits


def phone_key9(value) -> str | None:
    """Solishtirish uchun "kalit" -- raqamning oxirgi 9 ta xonasi (Uzbek
    mobil raqamlar uchun bu doim shahar/operator kodi + abonent raqami,
    "+998"/"998"/prefikssiz yozilishidan qat'iy nazar bir xil bo'ladi)."""
    s = clean_phone_raw(value)
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    return digits[-9:] if len(digits) >= 9 else (digits or None)
