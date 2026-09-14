"""tz_utils.py — 2026-09, Item J auditi (🟡 O'RTA, 20/21-band: "Vaqt zonasi
hisob-kitobi markazlashmagan" / "Sinxronizatsiya modullari orasida katta
takrorlanish"): ILGARI "hozirgi mahalliy (Toshkent) vaqt"ni hisoblash HAR
BIR faylda ALOHIDA, qo'lda takrorlangan edi --

    _TASHKENT_OFFSET = dt.timedelta(hours=5)
    now_tashkent = dt.datetime.utcnow() + _TASHKENT_OFFSET

xuddi shu 2 qator `call_analytics.py`, `dashboard_data.py`,
`ig_dm_analytics.py`, `monthly_report.py`, `orchestrator.py`,
`smm_analytics.py`, `smm_sync.py` fayllarida 7 MARTA mustaqil ravishda
qayta yozilgan edi. Muammo shunchaki "kod takrorlanishi" emas edi -- bu
QATTIQ YOZILGAN `+5 soat` siljish, `scheduler.py`dagi haqiqiy `TIMEZONE`
environment o'zgaruvchisidan (standart "Asia/Tashkent", lekin nazariy
jihatdan boshqa qiymatga o'zgartirilishi mumkin) MUSTAQIL edi. Agar/qachon
`TIMEZONE` o'zgartirilsa, FAQAT cron-jadval (`CronTrigger(timezone=...)`)
yangi zonaga o'tardi -- dashboard/hisobot/"bugun" hisob-kitoblarining
BARCHASI sezmasdan eski, noto'g'ri `+5 soat`da qolib ketardi (aynan shu
klassdagi xato loyihada ILGARI bir marta haqiqatan ham yuz bergan --
"har kuni 9da" xabari aslida 14:00da kelib turgan holat, qarang
`scheduler.py`dagi `CronTrigger`ga oid izoh).

Endi BITTA manba: bu modul `TIMEZONE` environment o'zgaruvchisini
(`scheduler.TIMEZONE` bilan BIR XIL manba/standart qiymat) `zoneinfo`
(Python standart kutubxonasi, DST-xavfsiz, haqiqiy IANA ma'lumotlar bazasi)
orqali o'qiydi. `python:3.11-slim` (Docker) bazasida OS darajasidagi
tzdata ALWAYS mavjud emas -- shu sabab `requirements.txt`ga `tzdata`
(sof-Python IANA ma'lumotlar paketi) qo'shildi, `zoneinfo` avtomatik shu
paketga zaxira sifatida murojaat qiladi (CPython hujjatida tavsiya
etilgan naqsh).

Bazadagi BARCHA `DateTime` ustunlar UTC-NAIVE konventsiyasida saqlanadi
(loyihaning boshidan buyon shunday) -- shuning uchun bu yerdagi funksiyalar
ham doim NAIVE datetime qaytaradi/qabul qiladi (tzinfo=None), faqat
QIYMATNING O'ZI mahalliy yoki UTC ekanini funksiya nomi bildiradi. Bu
eski `+/- timedelta(hours=5)` naqshi bilan chaqiruvchi kod uchun BIR XIL
"ko'rinishda" ishlaydi (drop-in almashtirish), lekin markazlashgan va
(agar kelajakda `TIMEZONE` chindan o'zgartirilsa) DST-xavfsiz."""

import datetime as dt
import os
from zoneinfo import ZoneInfo

# MUHIM: bu qiymat `scheduler.py`dagi `TIMEZONE`ning AYNAN o'zi (bir xil
# environment o'zgaruvchisi, bir xil standart) -- ikkalasi ham har doim
# BIR XIL zonaga ishora qilishi kerak (cron-jadval VA "bugun" hisob-
# kitoblari sinxron bo'lishi uchun). `scheduler.py`ni bu yerdan import
# QILMAYMIZ (aylanma import xavfi -- ko'p fayl `scheduler.py`ni import
# qiladi), shuning uchun bir xil `os.environ.get(...)` chaqiruvi ataylab
# takrorlangan (bu takrorlanish xavfsiz -- ikkalasi ham bir xil ENV'dan
# o'qiydi, natija HAR DOIM bir xil bo'ladi).
TIMEZONE_NAME = os.environ.get("TIMEZONE", "Asia/Tashkent")
LOCAL_TZ = ZoneInfo(TIMEZONE_NAME)


def now_local() -> dt.datetime:
    """Joriy vaqt, MAHALLIY (masalan Toshkent) vaqt zonasida hisoblangan,
    lekin NAIVE (tzinfo=None) qaytariladi -- eski
    `dt.datetime.utcnow() + timedelta(hours=5)` bilan bir xil natija
    beradi, faqat markazlashgan va TIMEZONE o'zgarsa avtomatik moslashadi."""
    return dt.datetime.now(LOCAL_TZ).replace(tzinfo=None)


def today_local() -> "dt.date":
    """Bugungi SANA, mahalliy vaqt zonasida."""
    return now_local().date()


def to_local(utc_naive: dt.datetime) -> dt.datetime:
    """UTC-naive datetime'ni mahalliy-naive datetime'ga o'giradi (masalan
    bazadan o'qilgan `created_at`ni foydalanuvchiga ko'rsatish uchun)."""
    return utc_naive.replace(tzinfo=dt.timezone.utc).astimezone(LOCAL_TZ).replace(tzinfo=None)


def to_utc(local_naive: dt.datetime) -> dt.datetime:
    """Mahalliy-naive datetime'ni UTC-naive datetime'ga o'giradi (masalan
    mahalliy "bugun 00:00" chegarasini bazadagi UTC-naive `created_at`
    bilan solishtirish uchun)."""
    return local_naive.replace(tzinfo=LOCAL_TZ).astimezone(dt.timezone.utc).replace(tzinfo=None)
