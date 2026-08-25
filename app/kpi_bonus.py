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
     shunchaki "keyingi oddiy sotuv" hisoblanadi. BU FORMULA proratsiyaga
     BOG'LIQ EMAS -- bitta savdo bo'yicha qoida, oy uzunligidan qat'iy nazar.
  3. Oylik jami sotuvlar soniga qarab progressiv ("Svex") bonus (B) -- OY
     YAKUNIDAGI umumiy sondan kelib chiqib, HAR BIR sotuv uchun bitta stavka
     qo'llanadi (hujjatda marjinal/bosqichma-bosqich emas, "erishilgan
     daraja" uslubida yozilgan):
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
     chiqarib tashlaydi.

PRORATSIYA (2026-08, foydalanuvchi so'rovi bo'yicha qo'shildi): agar menejer
oyning O'RTASIDA ishga kirgan bo'lsa (`Manager.hire_date`), unga to'liq oylik
reja (75 sotuv, 75/150/300/400mln bosqichlari) adolatsiz bo'ladi. Shuning
uchun `compute_prorate_factor()` shu oyda NECHA KUN ishlagani / OYNING JAMI
KUNI nisbatini hisoblaydi (taqvim kunlari bo'yicha, oddiylik uchun -- dam
olish kunlari hisobga olinmagan), va bu koeffitsient SOTUV SONI/OBOROT
CHEGARALARIGA qo'llaniladi. FAQAT sale-boshiga bonus (A) proratsiyaga
tegishli EMAS -- bu bitta tranzaksiya qoidasi, oy uzunligiga bog'liq emas.
Agar `hire_date` bo'sh bo'lsa -- koeffitsient har doim 1.0 (to'liq oy).

Bu modul FAQAT hisoblaydi -- ma'lumotni `db.Sale`/`db.Lead`dan o'qish
`app.py`dagi route tomonidan qilinadi (`_build_manager_kpi_report`).
"""

import calendar
import datetime as dt
import math

SALARY_FIXED = 4_000_000.0
MIN_SALE_AMOUNT = 500_000.0          # "minimal chek qoidasi"
REPEAT_WINDOW_DAYS = 15              # "qayta xarid" bonusi uchun oyna
SURVIVAL_MIN_SALES = 75              # "o'lim chizig'i" -- 25 ish kunida (to'liq oy)
SURVIVAL_WORKDAYS = 25
DAILY_CALLS_NORM = 60                # kunlik faollik normasi (>1 daqiqalik qo'ng'iroq)
DAILY_REQUESTS_NORM = 6              # kunlik reja (zayavka)
DAILY_SALES_NORM = 3                 # shulardan kamida 3 tasi real sotuv

_PROGRESSIVE_TIERS = [
    ("75 - 149 ta", 75, 149, 10_000),
    ("150 - 299 ta", 150, 299, 15_000),
    ("300+ ta", 300, None, 20_000),
]
_TURNOVER_TIERS = [
    ("75 mln - 150 mln", 75_000_000, 150_000_000, 500_000),
    ("150 mln - 300 mln", 150_000_000, 300_000_000, 1_000_000),
    ("300 mln - 400 mln", 300_000_000, 400_000_000, 2_000_000),
]


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


def compute_prorate_factor(year: int, month: int, hire_date=None) -> tuple[float, int, int]:
    """Qaytaradi: (factor 0..1, shu oyda ishlagan kunlar, oyning jami kuni).
    `hire_date` -- `datetime`/`date` yoki None (None -- to'liq oy ishlagan)."""
    days_in_month = calendar.monthrange(year, month)[1]
    month_start = dt.date(year, month, 1)
    month_end = dt.date(year, month, days_in_month)
    if hire_date is None:
        return 1.0, days_in_month, days_in_month
    hd = hire_date.date() if isinstance(hire_date, dt.datetime) else hire_date
    if hd <= month_start:
        return 1.0, days_in_month, days_in_month
    if hd > month_end:
        return 0.0, 0, days_in_month
    work_days = (month_end - hd).days + 1
    return work_days / days_in_month, work_days, days_in_month


def _scaled_progressive_tiers(factor: float) -> list[dict]:
    tiers = []
    for label, lo, hi, rate in _PROGRESSIVE_TIERS:
        tiers.append({
            "label": label,
            "min": round(lo * factor),
            "max": (round(hi * factor) if hi is not None else None),
            "rate": rate,
        })
    return tiers


def _scaled_turnover_tiers(factor: float) -> list[dict]:
    tiers = []
    for label, lo, hi, bonus in _TURNOVER_TIERS:
        tiers.append({
            "label": label,
            "min": lo * factor,
            "max": hi * factor,
            "bonus": bonus,
        })
    return tiers


def progressive_rate_for_count(total_sales_count: int, factor: float = 1.0) -> int:
    """Oylik jami sotuvlar soniga qarab, HAR BIR sotuv uchun bonus stavkasi.
    `factor` -- proratsiya koeffitsienti (0..1), chegaralarni shu nisbatda
    kamaytiradi (masalan yarim oy ishlagan menejer uchun 75 o'rniga ~38)."""
    if factor <= 0:
        return 0
    t1 = round(75 * factor)
    t2 = round(150 * factor)
    t3 = round(300 * factor)
    if total_sales_count < max(t1, 1):
        return 0
    if total_sales_count < t2:
        return 10_000
    if total_sales_count < t3:
        return 15_000
    return 20_000


def turnover_bonus_for_amount(total_turnover: float, factor: float = 1.0) -> float:
    """Oylik umumiy oborot bo'yicha pog'onali FIKS bonus (proratsiyalangan)."""
    if factor <= 0:
        return 0.0
    b1 = 75_000_000 * factor
    b2 = 150_000_000 * factor
    b3 = 300_000_000 * factor
    b4 = 400_000_000 * factor
    if total_turnover < b1:
        return 0.0
    if total_turnover < b2:
        return 500_000.0
    if total_turnover < b3:
        return 1_000_000.0
    if total_turnover <= b4:
        return 2_000_000.0
    extra_steps = math.ceil((total_turnover - b4) / (100_000_000 * factor))
    return 2_000_000.0 + extra_steps * 500_000.0


def _next_turnover_milestone(turnover: float, factor: float) -> tuple[float, float] | None:
    """Oborot bo'yicha KEYINGI bonus bosqichi (pog'ona) qiymatini va shu
    bosqichga yetganda olinadigan bonus (C)ni qaytaradi -- 400mlndan
    yuqorida ham (cheksiz, har 100mln uchun +500ming) ishlaydi, shuning
    uchun har doim "keyingi qadam" bo'ladi (agar factor>0 bo'lsa)."""
    if factor <= 0:
        return None
    b1 = 75_000_000 * factor
    b2 = 150_000_000 * factor
    b3 = 300_000_000 * factor
    b4 = 400_000_000 * factor
    step = 100_000_000 * factor
    if turnover < b1:
        milestone = b1
    elif turnover < b2:
        milestone = b2
    elif turnover < b3:
        milestone = b3
    elif turnover < b4:
        milestone = b4
    else:
        steps_done = math.floor((turnover - b4) / step) if step else 0
        milestone = b4 + (steps_done + 1) * step
    return milestone, turnover_bonus_for_amount(milestone, factor)


def month_bounds(year: int, month: int) -> tuple[dt.datetime, dt.datetime]:
    """[oy boshi, keyingi oy boshi) -- yarim ochiq oraliq sifatida qaytaradi."""
    start = dt.datetime(year, month, 1)
    if month == 12:
        end = dt.datetime(year + 1, 1, 1)
    else:
        end = dt.datetime(year, month + 1, 1)
    return start, end


def compute_manager_report(valid_sales: list[dict], year: int, month: int, hire_date=None) -> dict:
    """`valid_sales` -- shu menejerning shu oydagi, minimal chek shartidan
    o'tgan va QAYTARILMAGAN sotuvlari, har biri:
      {"sale_number": int, "amount": float, "sold_at": datetime,
       "days_since_first_sale": float|None, "lead_id": int}
    (kunlar/tartib butun LEAD tarixidan hisoblab kelinadi, faqat shu oyga
    tegishlilari shu ro'yxatda bo'ladi).

    Qaytaradi: oklad/bonus_a/bonus_b/bonus_c/jami, sales_count/turnover,
    proratsiya ma'lumoti (factor/work_days/days_in_month), UI uchun tier
    ro'yxatlari (is_current bilan) va kunlik taqsimot."""
    factor, work_days, days_in_month = compute_prorate_factor(year, month, hire_date)

    sales_count = len(valid_sales)
    turnover = sum(s["amount"] for s in valid_sales)

    bonus_a = sum(
        activation_bonus_for_sale(s["sale_number"], s["amount"], s.get("days_since_first_sale"))
        for s in valid_sales
    )
    rate = progressive_rate_for_count(sales_count, factor)
    bonus_b = rate * sales_count
    bonus_c = turnover_bonus_for_amount(turnover, factor)

    daily: dict[str, dict] = {}
    for s in valid_sales:
        day = s["sold_at"].strftime("%Y-%m-%d") if s.get("sold_at") else "noma'lum"
        d = daily.setdefault(day, {"sales_count": 0, "turnover": 0.0})
        d["sales_count"] += 1
        d["turnover"] += s["amount"]

    oklad = SALARY_FIXED
    jami = oklad + bonus_a + bonus_b + bonus_c

    survival_min = round(SURVIVAL_MIN_SALES * factor)
    progressive_tiers = _scaled_progressive_tiers(factor)
    for t in progressive_tiers:
        t["is_current"] = sales_count >= t["min"] and (t["max"] is None or sales_count <= t["max"])
    turnover_tiers = _scaled_turnover_tiers(factor)
    for t in turnover_tiers:
        t["is_current"] = turnover >= t["min"] and turnover < t["max"]

    daily_turnover_target = round((400_000_000 * factor) / days_in_month) if days_in_month else 0

    # UI uchun "keyingi bosqichgacha qoldi" ko'rsatkichlari (kartochkalardagi
    # "yetishi uchun qoldi"/"bonusgacha yetmaydi" maslahat matnlari shundan).
    next_progressive_tier = next((t for t in progressive_tiers if not t["is_current"] and sales_count < t["min"]), None)
    sales_to_next_tier = (next_progressive_tier["min"] - sales_count) if next_progressive_tier else 0
    next_turnover_tier = next((t for t in turnover_tiers if not t["is_current"] and turnover < t["min"]), None)
    turnover_to_next_tier = (next_turnover_tier["min"] - turnover) if next_turnover_tier else 0
    sales_to_survival = max(0, survival_min - sales_count) if survival_min else 0

    # --- Dashboard uchun: "hozirgi oladigan pul" va "keyingi qadamga borsa
    # nechpul oladi" ko'rsatkichlari. Svex (B) bonusi uchun keyingi bosqich
    # STAVKASI o'zgarganda (75/150/300 chegarasi) TO'LIQ hisoblanadi (rate x
    # shu bosqich boshlanish soni); oborot (C) bonusi uchun keyingi pog'ona
    # 400mlndan yuqorida ham cheksiz davom etadi. Bonus (A) va boshqa
    # komponentlar joriy holatda QOLDIRILADI (taxminiy proyeksiya --
    # kelajakdagi sotuvlar summasi noma'lum).
    bonus_total = round(bonus_a + bonus_b + bonus_c)

    if next_progressive_tier:
        projected_bonus_b_at_next_sales_tier = next_progressive_tier["rate"] * next_progressive_tier["min"]
        projected_total_at_next_sales_tier = round(oklad + bonus_a + projected_bonus_b_at_next_sales_tier + bonus_c)
    else:
        projected_bonus_b_at_next_sales_tier = None
        projected_total_at_next_sales_tier = None

    milestone = _next_turnover_milestone(turnover, factor)
    if milestone:
        next_turnover_milestone_amount, next_turnover_milestone_bonus = milestone
        turnover_to_next_milestone = max(0.0, next_turnover_milestone_amount - turnover)
        projected_total_at_next_turnover_milestone = round(oklad + bonus_a + bonus_b + next_turnover_milestone_bonus)
    else:
        next_turnover_milestone_amount = None
        next_turnover_milestone_bonus = None
        turnover_to_next_milestone = None
        projected_total_at_next_turnover_milestone = None

    return {
        "oklad": oklad,
        "bonus_a": round(bonus_a),
        "bonus_b": bonus_b,
        "bonus_c": bonus_c,
        "jami": round(jami),
        "sales_count": sales_count,
        "turnover": turnover,
        "progressive_rate": rate,
        "survival_ok": sales_count >= survival_min if survival_min else True,
        "survival_min": survival_min,
        "daily": daily,
        "prorate_factor": round(factor, 3),
        "work_days": work_days,
        "days_in_month": days_in_month,
        "is_prorated": factor < 1.0,
        "progressive_tiers": progressive_tiers,
        "turnover_tiers": turnover_tiers,
        "turnover_bonus_start": round(75_000_000 * factor),
        "daily_turnover_target": daily_turnover_target,
        "next_progressive_tier": next_progressive_tier,
        "sales_to_next_tier": sales_to_next_tier,
        "next_turnover_tier": next_turnover_tier,
        "turnover_to_next_tier": turnover_to_next_tier,
        "sales_to_survival": sales_to_survival,
        "bonus_total": bonus_total,
        "projected_bonus_b_at_next_sales_tier": (
            round(projected_bonus_b_at_next_sales_tier) if projected_bonus_b_at_next_sales_tier is not None else None
        ),
        "projected_total_at_next_sales_tier": projected_total_at_next_sales_tier,
        "next_turnover_milestone_amount": next_turnover_milestone_amount,
        "next_turnover_milestone_bonus": next_turnover_milestone_bonus,
        "turnover_to_next_milestone": turnover_to_next_milestone,
        "projected_total_at_next_turnover_milestone": projected_total_at_next_turnover_milestone,
    }
