"""
kpi_bonus.py — "DUNYABUNYA: Sotuv menejerlari uchun KPI va bonus tizimi"
hujjatidagi qoidalarni (2026-08-11 dan kuchga kirgan) dasturiy hisoblaydi.

MANBA: foydalanuvchi yuklagan "KPI_Bonus_Tizimi_Dunyo_Bunyod.docx". Qoidalar
qisqacha:

  1. Fiks oylik (oklad): 4 000 000 so'm/oy.
  2. "Mijozni faollashtirish" bonusi (A) -- bitta MIJOZ (lead)ning FAQAT
     birinchi va (agar 15 kun ichida bo'lsa) ikkinchi xaridiga beriladi:
       - 1-xarid:  10 000 so'm + xarid summasining 0.5%
       - 2-xarid (1-xariddan 15 kun ICHIDA bo'lsa): 20 000 so'm + 0.5%
       - 3-xarid va undan keyingilari: bu bonusga kirmaydi (0)
     Agar 2-xarid 15 kundan KECHROQ bo'lsa, u ham bonusga kirmaydi (0) --
     shunchaki "keyingi oddiy sotuv" hisoblanadi.
  3. Oylik jami sotuvlar soniga qarab progressiv ("Svex") bonus (B) -- ORQAGA
     QARAB emas, OY YAKUNIDAGI umumiy sondan kelib chiqib, HAR BIR sotuv uchun
     bitta stavka qo'llanadi (hujjatda marjinal/bosqichma-bosqich emas, balki
     "erishilgan daraja" uslubida yozilgan):
       75-149 ta:  10 000 so'm/sotuv
       150-299 ta: 15 000 so'm/sotuv
       300+ ta:    20 000 so'm/sotuv
       (75 tadan kam bo'lsa -- bonus yo'q)
  4. Oylik umumiy oborot (barcha sotuvlar summasi) bo'yicha POG'ONALI FIKS
     bonus (C):
       75mln-150mln:  500 000 so'm
       150mln-300mln: 1 000 000 so'm
       300mln-400mln: 2 000 000 so'm
       400mln dan yuqori: 2 000 000 + har qo'shimcha 100mln uchun 500 000
         (masalan 400-500mln = 2 500 000, 500-600mln = 3 000 000, ...)
  5. Minimal chek qoidasi: 500 000 so'mdan KICHIK "sotuv" umuman hisobga
     olinmaydi (na kvotaga, na bonusga) -- mayda cheklar bilan reja
     "bajarish"ning oldini olish uchun.
  6. "O'lim chizig'i": 25 ish kunida kamida 75 ta REAL sotuv -- bajarilmasa
     oylik KAMAYTIRILMAYDI, lekin ishdan bo'shatish sharti hisoblanadi (CRM
     buni faqat OGOHLANTIRISH sifatida ko'rsatadi, avtomatik hech narsa
     qilmaydi).
  7. Qaytarilgan (vozvrat) sotuvlar -- KPI/bonus hisobidan BUTUNLAY chiqarib
     tashlanadi (`Sale.is_returned=True`). Hujjatda "shu oy bonusi KEYINGI
     oylikdan chegiriladi" deyilgan -- bu CRM hozircha oyroaro avtomatik
     chegirma YURITMAYDI (bu alohida, murakkabroq buxgalteriya funksiyasi),
     faqat qaytarilgan sotuvni joriy va barcha kelajakdagi hisoblardan
     chiqarib tashlaydi. Agar bonus AVVAL to'langan bo'lsa, buni qo'lda
     hisobga olish kerak -- pastda `report["korsatma"]` ichida eslatiladi.

Bu modul FAQAT hisoblaydi -- ma'lumotni `db.Sale`/`db.Lead`dan o'qish
`app.py`dagi route tomonidan qilinadi (`build_manager_month_report`).
"""

import calendar
import datetime as dt
import math

SALARY_FIXED = 4_000_000.0
MIN_SALE_AMOUNT = 500_000.0          # "minimal chek qoidasi"
REPEAT_WINDOW_DAYS = 15              # "qayta xarid" bonusi uchun oyna
SURVIVAL_MIN_SALES = 75              # "o'lim chizig'i" -- 25 ish kunida
SURVIVAL_WORKDAYS = 25
DAILY_CALLS_NORM = 60                # kunlik faollik normasi (>1 daqiqalik qo'ng'iroq)
DAILY_REQUESTS_NORM = 6              # kunlik reja (zayavka)
DAILY_SALES_NORM = 3                 # shulardan kamida 3 tasi real sotuv


def activation_bonus_for_sale(sale_number: int, amount: float, days_since_first_sale: float | None) -> float:
    """Bitta sotuv uchun "mijozni faollashtirish" bonusini (A) hisoblaydi.
    `days_since_first_sale` faqat sale_number==2 uchun ma'noli (1-sotuvdan
    necha kun o'tgani)."""
    if sale_number == 1:
        return 10_000.0 + amount * 0.005
    if sale_number == 2:
        if days_since_first_sale is not None and days_since_first_sale <= REPEAT_WINDOW_DAYS:
            return 20_000.0 + amount * 0.005
        return 0.0
    return 0.0


def progressive_rate_for_count(total_sales_count: int) -> int:
    """Oylik jami sotuvlar soniga qarab, HAR BIR sotuv uchun bonus stavkasi."""
    if total_sales_count < 75:
        return 0
    if total_sales_count < 150:
        return 10_000
    if total_sales_count < 300:
        return 15_000
    return 20_000


def turnover_bonus_for_amount(total_turnover: float) -> float:
    """Oylik umumiy oborot bo'yicha pog'onali FIKS bonus."""
    if total_turnover < 75_000_000:
        return 0.0
    if total_turnover < 150_000_000:
        return 500_000.0
    if total_turnover < 300_000_000:
        return 1_000_000.0
    if total_turnover <= 400_000_000:
        return 2_000_000.0
    extra_steps = math.ceil((total_turnover - 400_000_000) / 100_000_000)
    return 2_000_000.0 + extra_steps * 500_000.0


def month_bounds(year: int, month: int) -> tuple[dt.datetime, dt.datetime]:
    """[oy boshi, keyingi oy boshi) -- yarim ochiq oraliq sifatida qaytaradi."""
    start = dt.datetime(year, month, 1)
    if month == 12:
        end = dt.datetime(year + 1, 1, 1)
    else:
        end = dt.datetime(year, month + 1, 1)
    return start, end


def compute_manager_report(valid_sales: list[dict]) -> dict:
    """`valid_sales` -- shu menejerning shu oydagi, minimal chek shartidan
    o'tgan va QAYTARILMAGAN sotuvlari, har biri:
      {"sale_number": int, "amount": float, "sold_at": datetime,
       "days_since_first_sale": float|None, "lead_id": int}
    (kunlar/tartib butun LEAD tarixidan hisoblab kelinadi, faqat shu oyga
    tegishlilari shu ro'yxatda bo'ladi).

    Qaytaradi: {"oklad", "bonus_a", "bonus_b", "bonus_c", "jami",
    "sales_count", "turnover", "progressive_rate", "survival_ok",
    "daily": {gun -> {"sales_count", "turnover"}}}"""
    sales_count = len(valid_sales)
    turnover = sum(s["amount"] for s in valid_sales)

    bonus_a = sum(
        activation_bonus_for_sale(s["sale_number"], s["amount"], s.get("days_since_first_sale"))
        for s in valid_sales
    )
    rate = progressive_rate_for_count(sales_count)
    bonus_b = rate * sales_count
    bonus_c = turnover_bonus_for_amount(turnover)

    daily: dict[str, dict] = {}
    for s in valid_sales:
        day = s["sold_at"].strftime("%Y-%m-%d") if s.get("sold_at") else "noma'lum"
        d = daily.setdefault(day, {"sales_count": 0, "turnover": 0.0})
        d["sales_count"] += 1
        d["turnover"] += s["amount"]

    oklad = SALARY_FIXED
    jami = oklad + bonus_a + bonus_b + bonus_c

    return {
        "oklad": oklad,
        "bonus_a": round(bonus_a),
        "bonus_b": bonus_b,
        "bonus_c": bonus_c,
        "jami": round(jami),
        "sales_count": sales_count,
        "turnover": turnover,
        "progressive_rate": rate,
        "survival_ok": sales_count >= SURVIVAL_MIN_SALES,
        "survival_min": SURVIVAL_MIN_SALES,
        "daily": daily,
    }
