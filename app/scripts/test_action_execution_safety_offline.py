"""test_action_execution_safety_offline.py — 2026-09, saqlangan xavfsizlik
auditi (`claude/targetolog-audit-2026-09.md`, KRITIK 2/3/4-bandlar) bo'yicha
tuzatilgan ijro-xavfsizligi tekshiruvlari uchun TARMOQSIZ (offline)
testlar. Haqiqiy Meta API'ga chaqiruv QILMAYDI.

Tekshiriladigan xatti-harakat -- barchasi Marketolog bosqichiga (LLM)
BOG'LIQ EMAS, `skip_marketolog=true` (productiondagi haqiqiy sozlama) bilan
ham serverda majburiy qo'llaniladi:
  1. `object_id` joriy hisob strukturasida (`meta_api.get_account_structure`)
     mavjud bo'lmasa -- Meta'ga so'rov UMUMAN ketmaydi, action "failed"ga
     yoziladi (LLM halyutsinatsiyasidan himoya).
  2. `object_id` (yoki uning campaign'i) `protected_campaign_ids`da bo'lsa --
     bajarilmaydi, "skipped"ga yoziladi (himoyalangan kampaniya himoyasi
     endi FAQAT CPL hard-kill'da emas, asosiy ijro yo'lida ham ishlaydi).
  3. `increase_budget`/`decrease_budget` so'ralgan foizi
     `max_daily_budget_change_percent`dan katta bo'lsa -- serverda shu
     chegaraga qisqartiriladi (LLM/Marketolog nima taklif qilishidan
     qat'iy nazar).
  4. `launch_campaign` (yangi obyekt yaratadi) object_id tekshiruvidan
     mustasno -- va agar rejada FAQAT shu turdagi action bo'lsa,
     account_structure hech so'ralmaydi (keraksiz API chaqiruvidan saqlanish).
  5. Tekshiruv uchun kerakli account_structure o'zi olinmasa (masalan
     tarmoq xatosi) -- "fail closed": pul-harakatli action'lar ijro
     etilmasdan xatolik bilan to'xtatiladi.

Ishga tushirish:
    cd app && python3 scripts/test_action_execution_safety_offline.py
"""

import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("META_ACCESS_TOKEN", "test-dummy-token")
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_test_dummy")

import orchestrator as orch  # noqa: E402
import meta_api  # noqa: E402


_STRUCTURE = {
    "campaigns": [
        {"id": "100", "name": "Campaign A", "status": "ACTIVE"},
        {"id": "200", "name": "Himoyalangan kampaniya", "status": "ACTIVE"},
    ],
    "adsets": [
        {"id": "10", "campaign_id": "100", "name": "Adset A"},
        {"id": "20", "campaign_id": "200", "name": "Himoyalangan adset"},
    ],
    "ads": [
        {"id": "1", "campaign_id": "100", "adset_id": "10", "name": "Ad A"},
    ],
}

_BASE_RULES = {
    "skip_marketolog": True,
    "protected_campaign_ids": [],
    "max_daily_budget_change_percent": 20,
}


def _plan(actions):
    return {"summary": "test action_plan", "actions": actions}


def test_blocks_hallucinated_object_id():
    plan = _plan([{"type": "pause_ad", "object_id": "9999-mavjud-emas", "object_name": "?"}])
    with mock.patch.object(orch, "BUSINESS_RULES", dict(_BASE_RULES)), \
         mock.patch.object(orch.meta_api, "get_account_structure", return_value=_STRUCTURE), \
         mock.patch.object(orch, "_execute_and_verify_status") as m_exec:
        text, stats = orch._finish_pipeline(plan, dry_run=False)
    assert not m_exec.called, "mavjud bo'lmagan object_id uchun Meta'ga haqiqiy so'rov ketmasligi kerak"
    assert stats["failed"] == 1 and stats["succeeded"] == 0
    print("OK: hisobda mavjud bo'lmagan (halyutsinatsiya qilingan) object_id bloklanadi, Meta'ga so'rov ketmaydi")


def test_blocks_protected_campaign():
    plan = _plan([{"type": "pause_ad", "object_id": "20", "object_name": "himoyalangan adset"}])
    rules = {**_BASE_RULES, "protected_campaign_ids": ["200"]}
    with mock.patch.object(orch, "BUSINESS_RULES", rules), \
         mock.patch.object(orch.meta_api, "get_account_structure", return_value=_STRUCTURE), \
         mock.patch.object(orch, "_execute_and_verify_status") as m_exec:
        text, stats = orch._finish_pipeline(plan, dry_run=False)
    assert not m_exec.called, "himoyalangan kampaniyaga tegishli obyekt uchun Meta'ga so'rov ketmasligi kerak"
    assert stats["skipped"] == 1 and stats["failed"] == 0 and stats["succeeded"] == 0
    print("OK: protected_campaign_ids endi asosiy ijro yo'lida ham (nafaqat CPL hard-kill'da) hurmat qilinadi")


def test_allows_valid_object_id():
    plan = _plan([{"type": "pause_ad", "object_id": "1", "object_name": "haqiqiy ad"}])
    with mock.patch.object(orch, "BUSINESS_RULES", dict(_BASE_RULES)), \
         mock.patch.object(orch.meta_api, "get_account_structure", return_value=_STRUCTURE), \
         mock.patch.object(orch, "_execute_and_verify_status", return_value={"status": "PAUSED", "verified": True}) as m_exec:
        text, stats = orch._finish_pipeline(plan, dry_run=False)
    assert m_exec.called, "hisobda HAQIQATAN mavjud object_id uchun ijro bloklanmasligi kerak"
    assert stats["succeeded"] == 1 and stats["failed"] == 0 and stats["skipped"] == 0
    print("OK: joriy hisobda haqiqatan mavjud object_id normal bajariladi")


def test_launch_campaign_exempt_and_skips_structure_fetch():
    plan = _plan([{"type": "launch_campaign", "object_name": "yangi kampaniya",
                    "params": {"campaign": {}, "adset": {}}}])
    with mock.patch.object(orch, "BUSINESS_RULES", dict(_BASE_RULES)), \
         mock.patch.object(orch.meta_api, "get_account_structure", return_value=_STRUCTURE) as m_structure, \
         mock.patch.object(orch, "_execute_launch_campaign", return_value={"campaign": {"id": "999"}}) as m_exec:
        text, stats = orch._finish_pipeline(plan, dry_run=False)
    assert m_exec.called
    assert stats["succeeded"] == 1
    assert not m_structure.called, "launch_campaign YANGI obyekt yaratadi -- object_id tekshiruvi (va uning uchun account_structure so'rovi) kerak emas"
    print("OK: launch_campaign object_id tekshiruvidan mustasno, keraksiz account_structure so'ralmaydi")


def test_budget_change_capped_server_side():
    plan = _plan([{"type": "increase_budget", "object_id": "10", "object_name": "adset A",
                    "params": {"percent": 500}}])
    rules = {**_BASE_RULES, "max_daily_budget_change_percent": 20}
    with mock.patch.object(orch, "BUSINESS_RULES", rules), \
         mock.patch.object(orch.meta_api, "get_account_structure", return_value=_STRUCTURE), \
         mock.patch.object(orch.meta_api, "get_adset_details", side_effect=[
             {"daily_budget": "10000"}, {"daily_budget": "12000"},
         ]), \
         mock.patch.object(orch.meta_api, "adjust_budget_by_percent") as m_adjust:
        text, stats = orch._finish_pipeline(plan, dry_run=False)
    assert stats["succeeded"] == 1, f"kutilmagan natija, stats={stats}"
    m_adjust.assert_called_once()
    called_percent = m_adjust.call_args[0][2]
    assert called_percent == 20, f"LLM 500% so'ragan bo'lsa ham, serverda 20%ga cheklanishi kerak edi -- olindi: {called_percent}"
    print("OK: byudjet o'zgarish foizi LLM taklifidan qat'iy nazar business_rules.json'dagi max_daily_budget_change_percent'ga cheklanadi")


def test_fail_closed_when_structure_unavailable():
    plan = _plan([{"type": "pause_ad", "object_id": "1", "object_name": "ad"}])
    with mock.patch.object(orch, "BUSINESS_RULES", dict(_BASE_RULES)), \
         mock.patch.object(orch.meta_api, "get_account_structure", side_effect=meta_api.MetaAPIError({"message": "tarmoq xatosi"})), \
         mock.patch.object(orch, "_execute_and_verify_status") as m_exec:
        text, stats = orch._finish_pipeline(plan, dry_run=False)
    assert not m_exec.called, "xavfsizlik tekshiruvi uchun ma'lumot olinmasa, ijro HECH QACHON davom etmasligi kerak (fail closed)"
    assert stats["failed"] == 1
    print("OK: xavfsizlik tekshiruvi uchun account_structure olinmasa, pul-harakatli action ijro etilmaydi (fail closed)")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
