"""test_ai_budget_guards_offline.py -- 2026-10-01 pul xavfsizligi (audit B7):
  1. AI bir adset byudjetini sutkada faqat 1 marta oshiradi.
  2. Kamaytirish cheklanmaydi.
  3. Oylik shiftdan oshadigan oshirish rad etiladi (USD hisob).
  4. AI yaratgan kampaniya/adset/reklama har doim PAUSED.

    cd app && python3 scripts/test_ai_budget_guards_offline.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-dummy-token")

import kv_store  # noqa: E402
import meta_api  # noqa: E402
import orchestrator  # noqa: E402

failures: list[str] = []


def check(name, cond, detail=""):
    print(("OK:   " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


store: dict = {}
kv_store.get_json = lambda k, default=None: store.get(k, default)
kv_store.set_json = lambda k, v: store.__setitem__(k, v)
budget = {"v": 1000}  # $10/kun
meta_api.get_adset_details = lambda adset_id, access_token=None: {"daily_budget": str(budget["v"])}


def _adjust(adset_id, current, percent, access_token=None):
    budget["v"] = int(current * (1 + percent / 100))


meta_api.adjust_budget_by_percent = _adjust
orchestrator._spend_to_usd_factor = lambda *a, **k: (1.0, "USD")
orchestrator.db.get_current_company_id = lambda: 1
orchestrator.BUSINESS_RULES["monthly_budget_cap_usd"] = 3000
orchestrator.BUSINESS_RULES["max_daily_budget_change_percent"] = 20

action = {"object_id": "as1", "params": {"percent": 20}}
r = orchestrator._execute_adjust_budget(action, "increase")
check("1-oshirish bajarildi", r["new_budget_cents"] == 1200, str(r))
try:
    orchestrator._execute_adjust_budget(action, "increase")
    check("2-oshirish 24 soat ichida rad etiladi", False)
except meta_api.MetaAPIError:
    check("2-oshirish 24 soat ichida rad etiladi", True)
r = orchestrator._execute_adjust_budget(action, "decrease")
check("kamaytirish cheklanmaydi", r["new_budget_cents"] == 960, str(r))

budget["v"] = 9000  # $90/kun -> +20% = $108 x 30 = $3240 > $3000
try:
    orchestrator._execute_adjust_budget({"object_id": "as2", "params": {"percent": 20}}, "increase")
    check("oylik shiftdan oshsa rad etiladi", False)
except meta_api.MetaAPIError:
    check("oylik shiftdan oshsa rad etiladi", True)
check("rad etilgan oshirish byudjetni o'zgartirmadi", budget["v"] == 9000)

calls = {}
meta_api.create_campaign = lambda **k: calls.setdefault("campaign", k) and {"id": "c1"}
meta_api.create_adset = lambda **k: calls.setdefault("adset", k) and {"id": "s1"}
meta_api.create_ad = lambda **k: calls.setdefault("ad", k) and {"id": "a1"}
orchestrator._execute_launch_campaign({"params": {
    "campaign": {"name": "X", "objective": "OUTCOME_LEADS", "status": "ACTIVE"},
    "adset": {"name": "Y", "daily_budget_cents": 500, "status": "ACTIVE"},
    "ad": {"creative_id": "cr1", "status": "ACTIVE"},
}})
check("kampaniya PAUSED", calls["campaign"]["status"] == "PAUSED")
check("adset PAUSED", calls["adset"]["status"] == "PAUSED")
check("reklama PAUSED", calls["ad"]["status"] == "PAUSED")

if failures:
    print(f"\n{len(failures)} ta XATO")
    sys.exit(1)
print("\nBARCHA TESTLAR O'TDI")
