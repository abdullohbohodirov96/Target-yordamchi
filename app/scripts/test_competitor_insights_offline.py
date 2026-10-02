"""test_competitor_insights_offline.py — raqobatchi reklamalarini AI'SIZ
avtomatik tahlil (`competitor_insights.analyze` + `competitor_analytics`):

  1. Holat: faol / 7 kunda yangi / oldingi tahlildan beri to'xtagan soni.
  2. 14+ kun ishlayotgan reklamalar eng uzoq birinchi bo'lib chiqadi.
  3. Takliflar: chegirma, muddatli to'lov ("10%" muddatli to'lov EMAS),
     bepul yetkazish ("bepul" umumiy belgisi takrorlanmaydi), 1+1, shoshiltirish.
  4. Narx: valyutali raqam olinadi, telefon/yil olinmaydi.
  5. Aloqa kanali (Telegram, telefon) va til (o'zbek lotin / rus).
  6. Tavsiyalar faktlardan chiqadi; matnsiz reklama yiqitmaydi.
  7. `competitor_analytics._analyze` standart holatda OpenAI'ni (call_light)
     UMUMAN chaqirmaydi, tahlil CompetitorAnalysis'ga yoziladi, Telegram
     matnida "##" belgisi qolmaydi.
"""
import datetime as dt
import os
import sys
import tempfile
from types import SimpleNamespace as NS
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(tempfile.mkdtemp(), 'ci.db')}"
os.environ.pop("COMPETITOR_AI_SUMMARY", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy")

import competitor_insights as ci  # noqa: E402

FAILED = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILED.append(name)


NOW = dt.datetime(2026, 10, 2, 12, 0)


def ad(text, started_days_ago, active=True, last_seen_days_ago=0):
    return NS(body_text=text, is_active=active,
              ad_started_at=NOW - dt.timedelta(days=started_days_ago),
              first_seen_at=NOW - dt.timedelta(days=started_days_ago),
              last_seen_at=NOW - dt.timedelta(days=last_seen_days_ago))


ads = [
    ad("Kuzgi kolleksiya -30% chegirma! Faqat bugun. Telegram: t.me/dunyabunya", 40),
    ad("Muddatli to'lov 0% ga, 6 oy. Narxi 1 290 000 so'm. Tel: +998 90 123 45 67", 20),
    ad("Yangi modellar keldi, bepul yetkazib beramiz", 2),
    ad("1+1 aksiya — ikkinchisi sovg'a", 1),
    ad("", 3),  # matnsiz (video) reklama
    ad("Eski reklama 2024 yil", 60, active=False, last_seen_days_ago=1),
    ad("Juda eski to'xtagan", 90, active=False, last_seen_days_ago=30),
]
comp = NS(name="Dunyabunya", domain="dunyabunya.uz")
r = ci.analyze(comp, ads, now=NOW, since=NOW - dt.timedelta(days=2))
f = r["facts"]
txt = r["summary_text"]

check("faol reklamalar = 5", f["active"] == 5)
check("7 kunda yangi = 3", f["new"] == 3)
check("oldingi tahlildan beri to'xtagan = 1 (30 kun oldingisi sanalmaydi)", f["stopped"] == 1)
check("14+ kun ishlayotgan = 2", f["long_running"] == 2)
lr_sec = txt.split("## Ishlayotgan")[1].split("##")[0]
check("eng uzoq (40 kun) birinchi", lr_sec.index("40 kun") < lr_sec.index("20 kun"))
for k in ("discount", "installment", "free_delivery", "bundle", "urgency", "gift"):
    check(f"taklif topildi: {k}", k in f["offers"])
check("'bepul yetkazish' bor -> umumiy 'free' takrorlanmaydi", "free" not in f["offers"])
check("narx olinadi (1 290 000 so'm)", any("1 290 000" in p for p in f["prices"]))
check("telefon raqami narx emas", not any("998" in p for p in f["prices"]))
check("kanallar: Telegram va Telefon", "Telegram" in f["channels"] and "Telefon" in f["channels"])
check("til: o'zbek (lotin)", f["language"] == "o'zbek (lotin)")
check("matnsiz reklama yiqitmaydi va belgilanadi", "matnsiz reklama" in txt)
check("tavsiya bo'limi bor", "## Bizga tavsiya" in txt and "40 kundan beri" in txt)

r2 = ci.analyze(comp, [ad("Скидка 10% на всё, звоните", 3)], now=NOW)
check("'10%' muddatli to'lov deb olinmaydi", "installment" not in r2["facts"]["offers"] and "discount" in r2["facts"]["offers"])
check("til: rus", r2["facts"]["language"] == "rus")
r3 = ci.analyze(comp, [], now=NOW)
check("reklamasiz -> xatosiz, tavsiya bor", r3["facts"]["active"] == 0 and "## Bizga tavsiya" in r3["summary_text"])

# 7. competitor_analytics -- AI chaqirilmaydi
import db  # noqa: E402
import competitor_analytics  # noqa: E402
import competitor_sync  # noqa: E402

db.init_db()
s = db.get_session()
try:
    with db.unscoped():
        co = db.Company(name="Test Co")
        s.add(co)
        s.commit()
        cid = co.id
    with db.scoped_as(cid):
        c = db.Competitor(name="Arboss", company_id=cid)
        s.add(c)
        s.commit()
        for i, a in enumerate(ads):
            s.add(db.CompetitorAd(company_id=cid, competitor_id=c.id, external_id=f"x{i}", body_text=a.body_text,
                                  is_active=a.is_active, ad_started_at=a.ad_started_at, first_seen_at=a.first_seen_at,
                                  last_seen_at=a.last_seen_at))
        s.commit()
        with mock.patch.object(competitor_analytics.orchestrator, "call_light", side_effect=AssertionError("AI chaqirildi")) as cl, \
             mock.patch.object(competitor_sync, "sync_one", return_value={}):
            res = competitor_analytics._analyze(s, c)
        check("standart holatda AI (call_light) chaqirilmaydi", cl.call_count == 0)
        check("tahlil matni yozildi", res["summary_text"] and "## Holat" in res["summary_text"])
        check("Telegram matnida '##' qolmaydi", "##" not in res["telegram_text"] and "HOLAT" in res["telegram_text"])
        check("CompetitorAnalysis saqlandi", s.query(db.CompetitorAnalysis).filter_by(competitor_id=c.id).count() == 1)
finally:
    s.close()

print()
if FAILED:
    print(f"{len(FAILED)} ta test YIQILDI: {FAILED}")
    sys.exit(1)
print("Hammasi o'tdi.")
