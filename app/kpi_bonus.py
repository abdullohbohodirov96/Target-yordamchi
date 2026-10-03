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

import db
import kv_store

SALARY_FIXED = 4_000_000.0
MIN_SALE_AMOUNT = 500_000.0          # "minimal chek qoidasi" -- standart (admin o'zgartirmagan bo'lsa shu ishlatiladi)
_MIN_SALE_AMOUNT_KEY = "kpi_min_sale_amount"


def _scoped_key(base_key: str, company_id: "int | None") -> str:
    """2026-09, MUHIM ko'p-kompaniyalilik tuzatishi (foydalanuvchi so'rovi:
    "ikkita-uchta kompaniya ochilsa ular orasida ma'lumotlar aralashib
    ketvoti"): bu qiymatlar avval FAQAT bitta GLOBAL kv_store kaliti bilan
    saqlanardi -- ya'ni QAYSI kompaniyaning admini "Sozlamalar"da
    o'zgartirsa ham, HAMMA kompaniyaning KPI/ROI hisobi (va CPL hard-kill
    chegaralari) sezilmasdan o'zgarib ketardi. Endi standart (birinchi,
    "Asosiy") kompaniya uchun ESKI, suffikssiz kalit ishlatiladi (mavjud
    sozlamalar yo'qolmasligi uchun), boshqa HAR BIR kompaniya uchun esa
    o'ziga alohida (`base_key:company_id`) kalit -- birining sozlamasi
    ikkinchisiga sira ta'sir qilmaydi."""
    if company_id is None:
        # Aniq berilmasa -- joriy so'rov/fon vazifasining kompaniyasi (audit B13:
        # ilgari jimgina egasining kursi/sozlamasi olinardi).
        company_id = db.get_current_company_id()
    if company_id is None:
        return base_key
    try:
        if company_id == db.get_default_company_id():
            return base_key
    except Exception:
        pass
    return f"{base_key}:{company_id}"


def get_min_sale_amount(company_id: "int | None" = None) -> float:
    """Admin "Sozlamalar" sahifasida o'zgartirishi mumkin bo'lgan minimal
    chek qiymatini qaytaradi (kv_store'da, HAR BIR kompaniya UCHUN ALOHIDA
    saqlanadi) -- o'zgartirilmagan bo'lsa standart `MIN_SALE_AMOUNT`
    (500 000 so'm) qaytadi."""
    value = kv_store.get_json(_scoped_key(_MIN_SALE_AMOUNT_KEY, company_id), default=None)
    try:
        return float(value) if value is not None else MIN_SALE_AMOUNT
    except (TypeError, ValueError):
        return MIN_SALE_AMOUNT


def set_min_sale_amount(value: float, company_id: "int | None" = None) -> None:
    kv_store.set_json(_scoped_key(_MIN_SALE_AMOUNT_KEY, company_id), max(0.0, float(value)))


# Dollar/so'm kursi -- dashboard'da ROI hisoblashda ishlatiladi (sotuv summasi
# so'mda, reklama xarajati Meta'dan USD'da keladi -- ikkalasini bitta valyutaga
# keltirmasdan to'g'ridan-to'g'ri solishtirish ROI'ni yuzlab million foizga
# "buzib" ko'rsatib yuborardi, 2026-08 foydalanuvchi topgan xato). Kurs vaqt
# o'tishi bilan o'zgaradi -- shuning uchun kodga qattiq yozilmagan, admin
# "Sozlamalar" sahifasidan xohlagan vaqtda yangilay oladi.
USD_TO_UZS_RATE = 11_800.0           # standart (admin o'zgartirmagan bo'lsa) -- 2026-08 taxminiy bozor kursi
_USD_TO_UZS_RATE_KEY = "usd_to_uzs_rate"


def get_usd_to_uzs_rate(company_id: "int | None" = None) -> float:
    """Admin "Sozlamalar" sahifasida o'zgartirishi mumkin bo'lgan dollar/so'm
    kursini qaytaradi (kv_store'da, HAR BIR kompaniya uchun ALOHIDA
    saqlanadi) -- o'zgartirilmagan bo'lsa standart `USD_TO_UZS_RATE`
    qaytadi. Real kurs muntazam o'zgargani uchun buni vaqti-vaqti bilan
    yangilab turish tavsiya etiladi."""
    value = kv_store.get_json(_scoped_key(_USD_TO_UZS_RATE_KEY, company_id), default=None)
    try:
        rate = float(value) if value is not None else USD_TO_UZS_RATE
        return rate if rate > 0 else USD_TO_UZS_RATE
    except (TypeError, ValueError):
        return USD_TO_UZS_RATE


def set_usd_to_uzs_rate(value: float, company_id: "int | None" = None) -> None:
    kv_store.set_json(_scoped_key(_USD_TO_UZS_RATE_KEY, company_id), max(1.0, float(value)))


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


# ---------------------------------------------------------------------------
# 2026-10-01, PLAN 5-bosqich: KPI/bonus qoidalari HAR BIR KOMPANIYA uchun
# sozlanadi (Sozlamalar -> KPI va bonus). Standart qiymatlar -- yuqoridagi
# Dunyabunya hujjati (o'zgartirilmagan kompaniyalarda natija AYNAN avvalgidek).
# Bazaga yangi ustun qo'shilmaydi -- kv_store'da JSON (migratsiyasiz).
# ---------------------------------------------------------------------------
_KPI_CONFIG_KEY = "kpi_config"

DEFAULT_KPI_CONFIG: dict = {
    "salary_fixed": SALARY_FIXED,
    "activation_first_fixed": 10_000.0,
    "activation_second_fixed": 20_000.0,
    "activation_percent": 0.5,              # % (0.5 = xarid summasining 0.5%)
    "repeat_window_days": REPEAT_WINDOW_DAYS,
    "progressive_tiers": [[75, 10_000], [150, 15_000], [300, 20_000]],       # [min sotuv, har sotuvga so'm]
    "turnover_tiers": [[75_000_000, 500_000], [150_000_000, 1_000_000], [300_000_000, 2_000_000]],  # [min oborot, fiks bonus]
    "turnover_top": 400_000_000,           # shundan yuqorisida har "step" uchun +step_bonus
    "turnover_step": 100_000_000,
    "turnover_step_bonus": 500_000,
    "survival_min_sales": SURVIVAL_MIN_SALES,
    "daily_calls_norm": DAILY_CALLS_NORM,
}


def _num(v, default, *, lo=0.0, hi=1e13):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f or f < lo or f > hi:  # NaN / chegaradan tashqari
        return default
    return f


def normalize_kpi_config(raw: "dict | None") -> dict:
    """Foydalanuvchi kiritgan (yoki kv'dan o'qilgan) qiymatlarni tekshirib,
    yetishmaganini standartdan to'ldiradi. Bosqichlar o'sish tartibida."""
    cfg = {k: (list(map(list, v)) if isinstance(v, list) else v) for k, v in DEFAULT_KPI_CONFIG.items()}
    if not isinstance(raw, dict):
        return cfg
    for key in ("salary_fixed", "activation_first_fixed", "activation_second_fixed", "turnover_top",
                "turnover_step", "turnover_step_bonus"):
        if key in raw:
            cfg[key] = _num(raw[key], cfg[key])
    if "activation_percent" in raw:
        cfg["activation_percent"] = _num(raw["activation_percent"], cfg["activation_percent"], hi=100)
    for key in ("repeat_window_days", "survival_min_sales", "daily_calls_norm"):
        if key in raw:
            cfg[key] = int(_num(raw[key], cfg[key], hi=100_000))
    for key in ("progressive_tiers", "turnover_tiers"):
        tiers = raw.get(key)
        if isinstance(tiers, list):
            clean = []
            for t in tiers:
                if isinstance(t, (list, tuple)) and len(t) == 2:
                    lo, val = _num(t[0], None), _num(t[1], None)
                    if lo is not None and val is not None and lo > 0:
                        clean.append([lo, val])
            clean.sort(key=lambda t: t[0])
            cfg[key] = clean  # bo'sh ro'yxat ham mumkin -- shu bonus o'chirilgan
    if cfg["turnover_step"] <= 0:
        cfg["turnover_step"] = 0
    return cfg


def get_kpi_config(company_id: "int | None" = None) -> dict:
    try:
        raw = kv_store.get_json(_scoped_key(_KPI_CONFIG_KEY, company_id), default=None)
    except Exception:  # noqa: BLE001
        raw = None
    return normalize_kpi_config(raw)


def set_kpi_config(raw: dict, company_id: "int | None" = None) -> dict:
    cfg = normalize_kpi_config(raw)
    kv_store.set_json(_scoped_key(_KPI_CONFIG_KEY, company_id), cfg)
    return cfg


def _fmt_count(n) -> str:
    return f"{int(round(n))}"


def _fmt_mln(v) -> str:
    m = v / 1_000_000
    return (f"{m:.0f}" if abs(m - round(m)) < 1e-9 else f"{m:.1f}") + " mln"


def activation_bonus_for_sale(sale_number: int, amount: float, days_since_first_sale: float | None, cfg: "dict | None" = None) -> float:
    """Bitta sotuv uchun "mijozni faollashtirish" bonusini (A) hisoblaydi.
    `days_since_first_sale` faqat sale_number==2 uchun ma'noli (1-sotuvdan
    necha kun o'tgani)."""
    cfg = cfg or DEFAULT_KPI_CONFIG
    pct = cfg["activation_percent"] / 100.0
    if sale_number == 1:
        return float(cfg["activation_first_fixed"]) + amount * pct
    if sale_number == 2:
        if days_since_first_sale is not None and days_since_first_sale <= cfg["repeat_window_days"]:
            return float(cfg["activation_second_fixed"]) + amount * pct
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


def _scaled_progressive_tiers(factor: float, cfg: "dict | None" = None) -> list[dict]:
    cfg = cfg or DEFAULT_KPI_CONFIG
    raw = cfg["progressive_tiers"]
    tiers = []
    for i, (lo, rate) in enumerate(raw):
        hi = raw[i + 1][0] - 1 if i + 1 < len(raw) else None
        label = f"{_fmt_count(lo)} - {_fmt_count(hi)} ta" if hi is not None else f"{_fmt_count(lo)}+ ta"
        tiers.append({
            "label": label,
            "min": round(lo * factor),
            "max": (round(hi * factor) if hi is not None else None),
            "rate": rate,
        })
    return tiers


def _scaled_turnover_tiers(factor: float, cfg: "dict | None" = None) -> list[dict]:
    cfg = cfg or DEFAULT_KPI_CONFIG
    raw = cfg["turnover_tiers"]
    top = cfg["turnover_top"]
    tiers = []
    for i, (lo, bonus) in enumerate(raw):
        hi = raw[i + 1][0] if i + 1 < len(raw) else (top if top and top > lo else None)
        label = f"{_fmt_mln(lo)} - {_fmt_mln(hi)}" if hi is not None else f"{_fmt_mln(lo)}+"
        tiers.append({
            "label": label,
            "min": lo * factor,
            "max": (hi * factor) if hi is not None else float("inf"),
            "bonus": bonus,
        })
    return tiers


def progressive_rate_for_count(total_sales_count: int, factor: float = 1.0, cfg: "dict | None" = None) -> int:
    """Oylik jami sotuvlar soniga qarab, HAR BIR sotuv uchun bonus stavkasi.
    `factor` -- proratsiya koeffitsienti (0..1), chegaralarni shu nisbatda
    kamaytiradi (masalan yarim oy ishlagan menejer uchun 75 o'rniga ~38)."""
    cfg = cfg or DEFAULT_KPI_CONFIG
    if factor <= 0 or not cfg["progressive_tiers"]:
        return 0
    rate = 0
    for i, (lo, r) in enumerate(cfg["progressive_tiers"]):
        threshold = round(lo * factor)
        if i == 0:
            threshold = max(threshold, 1)
        if total_sales_count >= threshold:
            rate = r
    return int(rate)


def turnover_bonus_for_amount(total_turnover: float, factor: float = 1.0, cfg: "dict | None" = None) -> float:
    """Oylik umumiy oborot bo'yicha pog'onali FIKS bonus (proratsiyalangan)."""
    cfg = cfg or DEFAULT_KPI_CONFIG
    if factor <= 0 or not cfg["turnover_tiers"]:
        return 0.0
    bonus = 0.0
    for lo, b in cfg["turnover_tiers"]:
        if total_turnover >= lo * factor:
            bonus = float(b)
    top, step, step_bonus = cfg["turnover_top"], cfg["turnover_step"], cfg["turnover_step_bonus"]
    if top and step and total_turnover > top * factor:
        extra_steps = math.ceil((total_turnover - top * factor) / (step * factor))
        bonus += extra_steps * float(step_bonus)
    return bonus


def _next_turnover_milestone(turnover: float, factor: float, cfg: "dict | None" = None) -> tuple[float, float] | None:
    """Oborot bo'yicha KEYINGI bonus bosqichi (pog'ona) qiymatini va shu
    bosqichga yetganda olinadigan bonus (C)ni qaytaradi -- yuqori chegaradan
    keyin ham (har "step" uchun +bonus) ishlaydi."""
    cfg = cfg or DEFAULT_KPI_CONFIG
    if factor <= 0 or not cfg["turnover_tiers"]:
        return None
    marks = [lo * factor for lo, _b in cfg["turnover_tiers"]]
    top, step = cfg["turnover_top"], cfg["turnover_step"]
    if top:
        marks.append(top * factor)
    for m in marks:
        if turnover < m:
            return m, turnover_bonus_for_amount(m, factor, cfg)
    if top and step:
        base = top * factor
        st = step * factor
        steps_done = math.floor((turnover - base) / st)
        milestone = base + (steps_done + 1) * st
        return milestone, turnover_bonus_for_amount(milestone, factor, cfg)
    return None


def month_bounds(year: int, month: int) -> tuple[dt.datetime, dt.datetime]:
    """[oy boshi, keyingi oy boshi) -- yarim ochiq oraliq sifatida qaytaradi."""
    start = dt.datetime(year, month, 1)
    if month == 12:
        end = dt.datetime(year + 1, 1, 1)
    else:
        end = dt.datetime(year, month + 1, 1)
    return start, end


def compute_manager_report(valid_sales: list[dict], year: int, month: int, hire_date=None, cfg: "dict | None" = None) -> dict:
    """`valid_sales` -- shu menejerning shu oydagi, minimal chek shartidan
    o'tgan va QAYTARILMAGAN sotuvlari, har biri:
      {"sale_number": int, "amount": float, "sold_at": datetime,
       "days_since_first_sale": float|None, "lead_id": int}
    (kunlar/tartib butun LEAD tarixidan hisoblab kelinadi, faqat shu oyga
    tegishlilari shu ro'yxatda bo'ladi).

    Qaytaradi: oklad/bonus_a/bonus_b/bonus_c/jami, sales_count/turnover,
    proratsiya ma'lumoti (factor/work_days/days_in_month), UI uchun tier
    ro'yxatlari (is_current bilan) va kunlik taqsimot."""
    cfg = cfg or get_kpi_config()
    factor, work_days, days_in_month = compute_prorate_factor(year, month, hire_date)

    sales_count = len(valid_sales)
    turnover = sum(s["amount"] for s in valid_sales)

    bonus_a = sum(
        activation_bonus_for_sale(s["sale_number"], s["amount"], s.get("days_since_first_sale"), cfg)
        for s in valid_sales
    )
    rate = progressive_rate_for_count(sales_count, factor, cfg)
    bonus_b = rate * sales_count
    bonus_c = turnover_bonus_for_amount(turnover, factor, cfg)

    daily: dict[str, dict] = {}
    for s in valid_sales:
        day = s["sold_at"].strftime("%Y-%m-%d") if s.get("sold_at") else "noma'lum"
        d = daily.setdefault(day, {"sales_count": 0, "turnover": 0.0})
        d["sales_count"] += 1
        d["turnover"] += s["amount"]

    oklad = float(cfg["salary_fixed"])
    jami = oklad + bonus_a + bonus_b + bonus_c

    survival_min = round(cfg["survival_min_sales"] * factor)
    progressive_tiers = _scaled_progressive_tiers(factor, cfg)
    for t in progressive_tiers:
        t["is_current"] = sales_count >= t["min"] and (t["max"] is None or sales_count <= t["max"])
    turnover_tiers = _scaled_turnover_tiers(factor, cfg)
    for t in turnover_tiers:
        t["is_current"] = turnover >= t["min"] and turnover < t["max"]

    plan_top = cfg["turnover_top"] or (cfg["turnover_tiers"][-1][0] if cfg["turnover_tiers"] else 0)
    daily_turnover_target = round((plan_top * factor) / days_in_month) if days_in_month else 0

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

    milestone = _next_turnover_milestone(turnover, factor, cfg)
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
        "turnover_bonus_start": round((cfg["turnover_tiers"][0][0] if cfg["turnover_tiers"] else 0) * factor),
        "plan_top": plan_top,
        "repeat_window_days": cfg["repeat_window_days"],
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
