"""competitor_insights.py — raqobatchi reklamalarini AI'SIZ (token sarflamasdan)
avtomatik tahlil qilish (2026-10, foydalanuvchi so'rovi: "token ai qiladigan
emas avtomatik qiladigan qilishimiz kerak xozircha").

Faqat reklama ma'lumotidan (matn, boshlangan sana, birinchi/oxirgi ko'rilgan
vaqt, faol/to'xtagan) aniq qoidalar bilan hisoblanadi:
  - Holat: nechta faol, oxirgi 7 kunda nechta yangi, nechtasi to'xtadi.
  - "Ishlayotgan" reklamalar: 14+ kun to'xtatilmagan reklama odatda foyda
    keltiradi (aks holda reklamachi uni o'chirardi) -- eng uzoq ishlaganlari.
  - Takliflar: chegirma %, narxlar, bepul yetkazish, muddatli to'lov,
    sovg'a/bonus, "1+1", shoshiltirish ("faqat bugun").
  - Aloqa kanali: Telegram, telefon, Direct, sayt.
  - Til: o'zbek (lotin/kirill), rus.
  - Bizga tavsiyalar -- yuqoridagi faktlardan qoida bilan.

Natija `competitor_analytics` bilan bir xil "## Sarlavha" formatida
(`cp_summary` filtri va Telegram uchun)."""

import datetime as dt
import re

LONG_RUNNING_DAYS = 14
NEW_WINDOW_DAYS = 7

# (kalit, ko'rinadigan nom, regex) -- reklama matnida taklif belgilari.
OFFER_PATTERNS = [
    ("discount", "Chegirma", re.compile(r"(-?\d{1,2}\s?%|chegirma|skidka|скидк|aksiya|акци|sale\b)", re.I)),
    ("free_delivery", "Bepul yetkazib berish", re.compile(r"(bepul\s+yetkaz|tekin\s+yetkaz|бесплатн\w*\s+доставк|free\s+delivery)", re.I)),
    ("installment", "Muddatli to'lov / nasiya", re.compile(r"(muddatli|nasiya|bo'lib\s+to'la|рассрочк|uzum\s*nasiya|(?<!\d)0\s?%\s*(ga|на)?)", re.I)),
    ("gift", "Sovg'a / bonus", re.compile(r"(sovg'a|sovga|bonus|подар|бонус|gift)", re.I)),
    ("bundle", "1+1 / 2+1 taklif", re.compile(r"\b\d\s?\+\s?\d\b")),
    ("urgency", "Shoshiltirish (cheklangan vaqt)", re.compile(r"(faqat\s+bugun|faqat\s+shu\s+hafta|oxirgi\s+kun|cheklangan|только\s+сегодня|успей|ограничен)", re.I)),
    ("free", "Bepul (sinov/konsultatsiya)", re.compile(r"(bepul|tekin|бесплатн|free\b)", re.I)),
    ("warranty", "Kafolat", re.compile(r"(kafolat|гаранти|warranty)", re.I)),
]

CHANNEL_PATTERNS = [
    ("Telegram", re.compile(r"(t\.me/|telegram|телеграм|@[a-z0-9_]{4,}bot\b)", re.I)),
    ("Telefon", re.compile(r"(\+?998[\s\-]?\(?\d{2}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}|qo'ng'iroq|звоните)", re.I)),
    ("Instagram Direct", re.compile(r"(direct|директ|dm\b|yozing|пишите)", re.I)),
    ("Sayt", re.compile(r"(https?://(?!t\.me)[^\s]+|www\.[^\s]+|\b[a-z0-9-]+\.uz\b)", re.I)),
]

PRICE_RE = re.compile(
    r"(\d{1,3}(?:[\s.,]\d{3})+|\d+(?:[.,]\d+)?\s?(?:mln|million|млн|ming|минг|тыс|k\b))\s?(so'm|som|сум|sum|uzs|\$)?",
    re.I,
)
PRICE_CURRENCY_RE = re.compile(r"(so'm|som\b|сум|sum\b|uzs|\$|mln|млн|ming|тыс)", re.I)
CYRILLIC_RE = re.compile(r"[а-яёўқғҳ]", re.I)
UZ_CYR_RE = re.compile(r"[ўқғҳ]", re.I)
UZ_LAT_RE = re.compile(r"(o'|g'|\bva\b|\buchun\b|\bbilan\b|\bsiz\b|\bbiz\b|lar\b|ni\b)", re.I)


def _days(a: "dt.datetime | None", now: dt.datetime) -> "int | None":
    if a is None:
        return None
    return max(0, (now - a).days)


def _short(text: "str | None", n: int = 90) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return "(matnsiz reklama — rasm/video)"
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def _language(texts: list[str]) -> "str | None":
    joined = " ".join(texts)
    if not joined.strip():
        return None
    cyr = len(CYRILLIC_RE.findall(joined))
    letters = len(re.findall(r"[a-zа-яё]", joined, re.I)) or 1
    if cyr / letters > 0.5:
        return "o'zbek (kirill)" if UZ_CYR_RE.search(joined) else "rus"
    if UZ_LAT_RE.search(joined):
        return "o'zbek (lotin)"
    return "lotin (aniq emas)"


def _prices(texts: list[str]) -> list[str]:
    found = []
    for t in texts:
        for m in PRICE_RE.finditer(t or ""):
            whole = m.group(0).strip()
            if not PRICE_CURRENCY_RE.search(whole):
                continue  # valyutasiz raqam (telefon, yil) -- narx emas
            if whole not in found:
                found.append(whole)
    return found[:5]


def analyze(competitor, ads: list, *, now: "dt.datetime | None" = None, since: "dt.datetime | None" = None) -> dict:
    """`ads` -- raqobatchining BARCHA saqlangan reklamalari (faol + to'xtagan).
    `since` -- oldingi tahlil vaqti (to'xtaganlarni sanash uchun).
    Qaytaradi: {"summary_text", "facts"}."""
    now = now or dt.datetime.utcnow()
    active = [a for a in ads if a.is_active]
    texts = [(a.body_text or "") for a in active]
    new_cut = now - dt.timedelta(days=NEW_WINDOW_DAYS)
    new_ads = [a for a in active if (a.ad_started_at or a.first_seen_at or now) >= new_cut]
    stop_cut = since or new_cut
    stopped = [a for a in ads if not a.is_active and a.last_seen_at and a.last_seen_at >= stop_cut]

    def run_days(a):
        return _days(a.ad_started_at or a.first_seen_at, now) or 0

    long_running = sorted([a for a in active if run_days(a) >= LONG_RUNNING_DAYS], key=run_days, reverse=True)

    offers = []
    for key, label, rx in OFFER_PATTERNS:
        n = sum(1 for t in texts if rx.search(t))
        if n:
            offers.append((key, label, n))
    # "Bepul" umumiy belgisi aniqrog'i (bepul yetkazish) bilan takrorlanmasin.
    keys = {k for k, _, _ in offers}
    if "free_delivery" in keys:
        offers = [o for o in offers if o[0] != "free"]
        keys.discard("free")
    channels = [name for name, rx in CHANNEL_PATTERNS if any(rx.search(t) for t in texts)]
    prices = _prices(texts)
    lang = _language(texts)

    lines = []
    lines.append("## Holat")
    lines.append(f"Hozir faol reklamalar: {len(active)} ta")
    lines.append(f"Oxirgi {NEW_WINDOW_DAYS} kunda yangi: {len(new_ads)} ta")
    lines.append(f"To'xtatilgan (oldingi tahlildan beri): {len(stopped)} ta")
    if active:
        avg = sum(run_days(a) for a in active) / len(active)
        lines.append(f"Reklamalar o'rtacha {avg:.0f} kundan beri ishlayapti")

    if long_running:
        lines.append("")
        lines.append(f"## Ishlayotgan reklamalar ({LONG_RUNNING_DAYS}+ kun to'xtatilmagan)")
        lines.append("Uzoq vaqt o'chirilmagan reklama odatda foyda keltiradi — shularga e'tibor bering:")
        for a in long_running[:3]:
            lines.append(f"• {run_days(a)} kun — {_short(a.body_text)}")

    if new_ads:
        lines.append("")
        lines.append("## Yangi reklamalar")
        for a in new_ads[:3]:
            lines.append(f"• {_short(a.body_text)}")

    lines.append("")
    lines.append("## Takliflar")
    if offers:
        for _, label, n in offers:
            lines.append(f"• {label} — {n} ta reklamada")
    else:
        lines.append("Matnda aniq aksiya/chegirma topilmadi (rasm yoki videoda bo'lishi mumkin).")
    if prices:
        lines.append(f"Narxlar: {', '.join(prices)}")

    meta = []
    if channels:
        meta.append("Mijozni yo'naltiradi: " + ", ".join(channels))
    if lang:
        meta.append(f"Reklama tili: {lang}")
    if meta:
        lines.append("")
        lines.append("## Aloqa va til")
        lines.extend(meta)

    recs = []
    if long_running:
        recs.append(f"Ularning {run_days(long_running[0])} kundan beri ishlayotgan reklamasini o'rganing — taklifi va formati ishlayapti.")
    if "discount" in keys:
        recs.append("Ular chegirma bilan jalb qilyapti — narxni tushirmasdan, qo'shimcha qiymat (bonus, kafolat, tez yetkazish) bilan javob bering.")
    if "installment" in keys:
        recs.append("Ular muddatli to'lov taklif qilyapti — sizda ham bo'lsa, reklamada aniq yozing.")
    if "urgency" in keys:
        recs.append("Ular shoshiltirish ishlatyapti — sizning reklamangizda ham muddatli aksiya sinab ko'ring.")
    if len(new_ads) >= 3:
        recs.append(f"So'nggi {NEW_WINDOW_DAYS} kunda {len(new_ads)} ta yangi reklama — raqobatchi faollashdi, CPL'ingizni kuzating.")
    if len(stopped) >= 3 and len(new_ads) == 0:
        recs.append("Raqobatchi reklamalarini kamaytiryapti — auditoriyani egallash uchun qulay payt.")
    if not recs:
        recs.append("Sezilarli o'zgarish yo'q — keyingi tekshiruvda solishtiramiz.")
    lines.append("")
    lines.append("## Bizga tavsiya")
    lines.extend(f"• {r}" for r in recs[:4])

    return {
        "summary_text": "\n".join(lines),
        "facts": {
            "active": len(active), "new": len(new_ads), "stopped": len(stopped),
            "long_running": len(long_running), "offers": sorted(keys), "channels": channels,
            "prices": prices, "language": lang,
        },
    }
