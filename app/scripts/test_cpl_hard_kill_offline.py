"""test_cpl_hard_kill_offline.py — `orchestrator.enforce_cpl_hard_kill()`
uchun TARMOQSIZ (offline) tekshiruvlar. Haqiqiy Meta API'ga chaqiruv
QILMAYDI -- `dashboard_data.get_kpis`, `meta_api.get_account_structure` va
`orchestrator._execute_and_verify_status` mock qilinadi, faqat CPL
hard-kill'ning sof qaror-qabul qilish mantig'i (kimni pauza qilish, kimni
qoldirish, xatoni qanday qayd etish) tekshiriladi.

Ishga tushirish:
    cd app && python3 scripts/test_cpl_hard_kill_offline.py
Muvaffaqiyatli bo'lsa "BARCHA TESTLAR O'TDI" chiqadi, aks holda
AssertionError bilan to'xtaydi (qaysi test ekani ko'rinadi).
"""

import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# `orchestrator.py` modul yuklanganda (import vaqtida) `anthropic.Anthropic(...)`
# klientini tayyorlaydi -- shu uchun ANTHROPIC_API_KEY muhit o'zgaruvchisi
# talab qilinadi (haqiqiy tarmoq chaqiruvi bu faylda UMUMAN qilinmaydi,
# faqat import vaqtidagi tekshiruvni o'tkazish uchun). Productionda bular
# haqiqiy qiymatlar bilan sozlangan -- bu yerda faqat offline test import
# qila olishi uchun soxta qiymat qo'yiladi (agar allaqachon sozlangan bo'lsa,
# ustidan yozilmaydi).
os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("META_ACCESS_TOKEN", "test-dummy-token")
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_test_dummy")

import orchestrator as orch
import meta_api


def _row(ad_id, name="Test ad", status="ACTIVE", spend=0.0, cpl=0.0,
         crm_leads_total=0, goal="", meta_result=0, meta_leads=0):
    return {
        "id": ad_id, "name": name, "status": status, "spend": spend, "cpl": cpl,
        "crm_leads_total": crm_leads_total, "goal": goal,
        "meta_result": meta_result, "meta_leads": meta_leads,
    }


_BASE_RULES = {
    "cpl_hard_kill_usd": 1.5,
    "cpl_hard_kill_min_spend_usd": 3.0,
    "cpl_hard_kill_zero_lead_multiplier": 3.0,
    "protected_campaign_ids": [],
}


def _run(rows, rules=None, structure=None):
    """`enforce_cpl_hard_kill()`ni berilgan qatorlar/qoidalar bilan chaqiradi,
    `_execute_and_verify_status`ni mock qilib (haqiqiy pauza QILMAYDI),
    natija dict'i va chaqirilgan ad_id'lar ro'yxatini qaytaradi."""
    rules = {**_BASE_RULES, **(rules or {})}
    structure = structure if structure is not None else {"ads": []}
    with mock.patch.object(orch, "BUSINESS_RULES", rules), \
         mock.patch.object(orch.dashboard_data, "get_kpis", return_value={"rows": rows}), \
         mock.patch.object(orch.meta_api, "get_account_structure", return_value=structure), \
         mock.patch.object(orch, "_execute_and_verify_status") as mock_pause:
        result = orch.enforce_cpl_hard_kill()
    paused_ad_ids = [c.args[0] for c in mock_pause.call_args_list]
    return result, paused_ad_ids


def test_pauses_ad_over_threshold_with_sufficient_spend():
    rows = [_row("ad_1", spend=10.0, cpl=2.5, crm_leads_total=4)]
    result, paused_ids = _run(rows)
    assert paused_ids == ["ad_1"], f"kutilgan ['ad_1'], olindi: {paused_ids}"
    assert len(result["paused"]) == 1
    assert result["paused"][0]["cpl"] == 2.5
    assert not result["errors"]
    print("OK: CPL chegaradan yuqori va xarajat yetarli bo'lsa -- reklama pauza qilinadi")


def test_leaves_ad_below_threshold_alone():
    rows = [_row("ad_2", spend=10.0, cpl=1.2, crm_leads_total=8)]
    result, paused_ids = _run(rows)
    assert paused_ids == [], f"pauza qilinmasligi kerak edi, olindi: {paused_ids}"
    assert result["checked"] == 1
    print("OK: CPL chegaradan past bo'lsa -- reklama tegilmaydi")


def test_skips_ad_with_insufficient_spend_sample():
    # CPL $2.0 -- chegaradan ($1.5) yuqori, LEKIN xarajat ($2.0) minimal
    # bo'sag'adan ($3.0) past -- bitta erta/tasodifiy qimmat lead uchun
    # asossiz pauza qilinmasligi kerak.
    rows = [_row("ad_3", spend=2.0, cpl=2.0, crm_leads_total=1)]
    result, paused_ids = _run(rows)
    assert paused_ids == [], f"kam xarajatli namunada pauza qilinmasligi kerak edi, olindi: {paused_ids}"
    print("OK: CPL yuqori, lekin xarajat hajmi juda kichik bo'lsa -- pauza qilinmaydi (shovqin himoyasi)")


def test_pauses_zero_lead_high_spend_ad():
    # cpl_hard_kill_usd=1.5, multiplier=3.0 -> chegara $4.5. $5.0 sarflandi,
    # lekin birorta ham lead yo'q (cpl=0.0, chunki 0'ga bo'linish yo'q).
    rows = [_row("ad_4", spend=5.0, cpl=0.0, crm_leads_total=0, goal="LEAD_GENERATION")]
    result, paused_ids = _run(rows)
    assert paused_ids == ["ad_4"], f"kutilgan ['ad_4'], olindi: {paused_ids}"
    assert "lead" in result["paused"][0]["reason"].lower()
    print("OK: hali birorta lead kelmagan, lekin xarajat juda baland bo'lsa -- reklama pauza qilinadi")


def test_leaves_zero_lead_low_spend_ad_alone():
    rows = [_row("ad_5", spend=2.0, cpl=0.0, crm_leads_total=0, goal="LEAD_GENERATION")]
    result, paused_ids = _run(rows)
    assert paused_ids == [], f"kam xarajatli zero-lead namunada pauza qilinmasligi kerak edi, olindi: {paused_ids}"
    print("OK: hali lead yo'q, lekin xarajat ham hali kam bo'lsa -- pauza qilinmaydi")


def test_skips_paused_and_inactive_ads():
    rows = [
        _row("ad_6", status="PAUSED", spend=100.0, cpl=10.0, crm_leads_total=1),
        _row("ad_7", status="ARCHIVED", spend=100.0, cpl=10.0, crm_leads_total=1),
    ]
    result, paused_ids = _run(rows)
    assert paused_ids == [], f"FAQAT ACTIVE reklamalar tekshirilishi kerak, olindi: {paused_ids}"
    assert result["checked"] == 0
    print("OK: ACTIVE bo'lmagan reklamalar hisobga olinmaydi (ular allaqachon to'xtatilgan)")


def test_respects_protected_campaign_ids():
    rows = [_row("ad_8", spend=10.0, cpl=5.0, crm_leads_total=1)]
    structure = {"ads": [{"id": "ad_8", "campaign_id": "camp_protected"}]}
    result, paused_ids = _run(rows, rules={"protected_campaign_ids": ["camp_protected"]}, structure=structure)
    assert paused_ids == [], f"himoyalangan kampaniyadagi reklama pauza qilinmasligi kerak, olindi: {paused_ids}"
    print("OK: `protected_campaign_ids`dagi kampaniyaga tegishli reklama CPL yuqori bo'lsa ham tegilmaydi")


def test_records_pause_errors_without_crashing():
    rows = [_row("ad_9", spend=10.0, cpl=5.0, crm_leads_total=1)]
    rules = {**_BASE_RULES}
    with mock.patch.object(orch, "BUSINESS_RULES", rules), \
         mock.patch.object(orch.dashboard_data, "get_kpis", return_value={"rows": rows}), \
         mock.patch.object(orch.meta_api, "get_account_structure", return_value={"ads": []}), \
         mock.patch.object(orch, "_execute_and_verify_status", side_effect=meta_api.MetaAPIError({"message": "Meta xatosi"})):
        result = orch.enforce_cpl_hard_kill()
    assert result["paused"] == []
    assert len(result["errors"]) == 1 and "ad_9" in result["errors"][0]
    print("OK: pauza qilishda Meta xatosi chiqsa -- funksiya yiqilmaydi, xato ro'yxatga yoziladi")


def test_disabled_when_threshold_not_configured():
    rows = [_row("ad_10", spend=100.0, cpl=50.0, crm_leads_total=1)]
    result, paused_ids = _run(rows, rules={"cpl_hard_kill_usd": 0})
    assert paused_ids == []
    assert result["checked"] == 0
    print("OK: cpl_hard_kill_usd sozlanmagan/0 bo'lsa -- tekshiruv butunlay o'tkazib yuboriladi")


def test_get_kpis_error_returns_gracefully():
    with mock.patch.object(orch, "BUSINESS_RULES", dict(_BASE_RULES)), \
         mock.patch.object(orch.dashboard_data, "get_kpis", return_value={"error": "Meta ulanmadi", "rows": []}), \
         mock.patch.object(orch, "_execute_and_verify_status") as mock_pause:
        result = orch.enforce_cpl_hard_kill()
    mock_pause.assert_not_called()
    assert result["checked"] == 0
    assert result["errors"] == ["Meta ulanmadi"]
    print("OK: dashboard_data.get_kpis xato qaytarsa -- funksiya yiqilmasdan xatoni qaytaradi")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
