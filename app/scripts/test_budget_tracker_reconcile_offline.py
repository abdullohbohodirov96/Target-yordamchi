"""test_budget_tracker_reconcile_offline.py -- audit B4: bir kunda ko'p
tekshiruv bugungi xarajatni qayta-qayta ayirmasligi.

    cd app && python3 scripts/test_budget_tracker_reconcile_offline.py
"""
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import budget_tracker  # noqa: E402
import kv_store  # noqa: E402
import meta_api  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(("OK:   " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


store = {}
kv_store.get_json = lambda k, default=None: store.get(k, default)
kv_store.set_json = lambda k, v: store.__setitem__(k, v)
today = datetime.now(timezone.utc).date()
yesterday = (today - timedelta(days=1)).isoformat()
spend_by_day = {today.isoformat(): 10.0}


def _spend(since, until):
    return sum(v for d, v in spend_by_day.items() if since <= d <= until)


meta_api.get_account_spend = _spend
meta_api.get_account_daily_spend_avg = lambda days=3: 20.0

budget_tracker.record_deposit(500.0, chat_id=1)
check("deposit: bugungi oldingi xarajat ayirilmaydi", store["budget_state"]["balance_usd"] == 500.0, str(store["budget_state"]))

spend_by_day[today.isoformat()] = 25.0
budget_tracker.get_status()
budget_tracker.get_status()
budget_tracker.get_status()
check("3 marta tekshiruv -> faqat +15 ayirildi", abs(store["budget_state"]["balance_usd"] - 485.0) < 1e-6,
      str(store["budget_state"]["balance_usd"]))

# Kun almashdi: kechagi (endi "since") qolgan xarajat + bugungi
st = store["budget_state"]
st["last_reconciled_at"] = datetime.fromisoformat(yesterday + "T23:00:00+00:00").isoformat()
st["counted_day"] = yesterday
st["counted_spend_on_last_day"] = 25.0
spend_by_day.clear()
spend_by_day[yesterday] = 30.0
spend_by_day[today.isoformat()] = 7.0
budget_tracker.get_status()
check("kun almashganda: kechagi +5 va bugungi 7 ayirildi", abs(store["budget_state"]["balance_usd"] - 473.0) < 1e-6,
      str(store["budget_state"]["balance_usd"]))
budget_tracker.get_status()
check("yana tekshiruv -- o'zgarmaydi", abs(store["budget_state"]["balance_usd"] - 473.0) < 1e-6)

if failures:
    sys.exit(1)
print("\nBARCHA TESTLAR O'TDI")
