"""test_meta_result_dedup_offline.py -- 2026-09, foydalanuvchi skrinshot bilan
xabar qildi: Target jadvalida "Meta natija" = 2 ko'rsatilgan, lekin HAQIQIY
Meta Ads Manager'da o'sha campaign uchun "Results" = 1 edi. Sabab:
`dashboard_data._extract_action_count()` avval bitta haqiqiy lidni ifodalovchi
bir nechta action_type ("lead" VA "onsite_conversion.lead_grouped")ni
QO'SHARDI, endi ULARDAN BIRINI (ustuvor/eng aniqini) tanlaydi. Bu test o'sha
aniq stsenariyni (va boshqa goal turlarini) offline qayta ishlab chiqaradi --
tarmoq yoki haqiqiy Meta hisobi kerak emas."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

import dashboard_data as dd

failures = []


def check(label, cond):
    if not cond:
        failures.append(label)
        print(f"FAIL: {label}")
    else:
        print(f"ok: {label}")


# 1) Foydalanuvchi topgan aniq holat: Instant Form lead -- Meta HAM "lead"
#    HAM "onsite_conversion.lead_grouped"ni bitta haqiqiy lid uchun qaytaradi.
#    Natija Ads Manager'dagi kabi 1 bo'lishi kerak, 2 EMAS.
actions_dup_lead = [
    {"action_type": "lead", "value": "1"},
    {"action_type": "onsite_conversion.lead_grouped", "value": "1"},
]
count, label = dd._resolve_meta_result("LEAD_GENERATION", actions_dup_lead, reach=500, impressions=900)
check("Instant Form dublikat lead -- 1 ta hisoblanadi (2 EMAS)", count == 1)
check("label -- Lidlar", "Lid" in label or "lead" in label.lower())

# 2) Real qiymatlar har xil bo'lsa ham (masalan eski "lead" ustunidagi son
#    yangilanmagan/farq qilsa) -- ENG USTUVOR (onsite_conversion.lead_grouped)
#    tanlanadi, "lead"niki emas.
actions_mismatched = [
    {"action_type": "lead", "value": "3"},
    {"action_type": "onsite_conversion.lead_grouped", "value": "2"},
]
count2, _ = dd._resolve_meta_result("LEAD_GENERATION", actions_mismatched, reach=0, impressions=0)
check("Nomuvofiq qiymatlarda ustuvor (lead_grouped=2) tanlanadi, yig'indi (5) EMAS", count2 == 2)

# 3) Faqat eski "lead" turi kelgan (yangi grouped hali yo'q) -- baribir
#    to'g'ri hisoblanishi kerak (zaxira ishlayapti).
actions_legacy_only = [{"action_type": "lead", "value": "4"}]
count3, _ = dd._resolve_meta_result("LEAD_GENERATION", actions_legacy_only, reach=0, impressions=0)
check("Faqat eski 'lead' turi -- 4 ta to'g'ri hisoblanadi", count3 == 4)

# 4) Lead umuman kelmagan -- 0, va label baribir "Lidlar" bo'lishi kerak
#    (Impressions'ga tushib ketmasligi kerak, goal aniq LEAD_GENERATION).
count4, label4 = dd._resolve_meta_result("LEAD_GENERATION", [], reach=0, impressions=1200)
check("Lead kelmagan -- 0", count4 == 0)
check("Lead kelmagan ham label Lidlar turkumida qoladi (Impressions emas)", "Lid" in label4 or "lead" in label4.lower())

# 5) CONVERSATIONS uchun ham xuddi shunday dublikat-himoya ishlashi kerak
#    (uchta alternativ nom bitta suhbatni bildirishi mumkin).
actions_conv_dup = [
    {"action_type": "onsite_conversion.messaging_conversation_started_7d", "value": "5"},
    {"action_type": "onsite_conversion.messaging_first_reply", "value": "5"},
]
count5, _ = dd._resolve_meta_result("CONVERSATIONS", actions_conv_dup, reach=0, impressions=0)
check("CONVERSATIONS dublikat -- yig'indi (10) emas, bitta ustuvor qiymat (5)", count5 == 5)

# 6) _extract_action_count() to'g'ridan-to'g'ri -- ustuvorlik tartibi
#    hurmat qilinishini tekshiradi (ro'yxatdagi ikkinchi/uchinchisi mavjud
#    bo'lsa ham, birinchisi ustun turadi).
c = dd._extract_action_count(
    [{"action_type": "b", "value": "9"}, {"action_type": "a", "value": "1"}],
    ["a", "b", "c"],
)
check("_extract_action_count ustuvorlik tartibini hurmat qiladi (a=1, b=9 emas)", c == 1)

# 7) Hech qanday mos action_type topilmasa -- 0 (xato emas).
c2 = dd._extract_action_count([{"action_type": "x", "value": "1"}], ["a", "b"])
check("_extract_action_count mos kelmasa 0 qaytaradi", c2 == 0)

if failures:
    print(f"\n{len(failures)} ta test MUVAFFAQIYATSIZ:")
    for f in failures:
        print(f" - {f}")
    sys.exit(1)
print(f"\nBARCHA TESTLAR O'TDI ({7} ta)")
