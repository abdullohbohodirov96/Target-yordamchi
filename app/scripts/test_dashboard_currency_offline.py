"""test_dashboard_currency_offline.py -- 2026-10-01: so'mli reklama
hisobida xarajat dollarga o'tkaziladi (ROI/CPL buzilmaydi) va CPL hard-kill
uni ikkinchi marta o'tkazmaydi; last_Nd CRM oynasi bugunni olmaydi.

    cd app && python3 scripts/test_dashboard_currency_offline.py
"""
import os
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["REPLIX_TEST_DEFAULT_UNSCOPED"] = "1"
os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-dummy-token")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(tempfile.mkdtemp(), 'cur.db')}"

import dashboard_data  # noqa: E402
import db  # noqa: E402
import meta_api  # noqa: E402
import orchestrator  # noqa: E402
import tz_utils  # noqa: E402

db.init_db()
failures = []


def check(name, cond, detail=""):
    print(("OK:   " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


rows = [{"campaign_id": "c1", "campaign_name": "K", "spend": "1250000", "impressions": "1000", "reach": "800", "actions": []}]
with mock.patch.object(meta_api, "get_insights", return_value=rows), \
     mock.patch.object(meta_api, "get_account_structure", return_value={"campaigns": [], "adsets": [], "ads": []}, create=True), \
     mock.patch.object(orchestrator, "_spend_to_usd_factor", return_value=(1 / 12500, "UZS")):
    r = dashboard_data._get_kpis_uncached(level="campaign", date_preset="last_7d", access_token="t", ad_account_id="act_1")
spend = (r.get("rows") or [{}])[0].get("spend")
check("so'mli hisob: 1 250 000 so'm -> $100", spend is not None and abs(spend - 100.0) < 1e-6, str(r)[:300])
check("spend_in_usd belgisi qo'yildi", r.get("spend_in_usd") is True)

with mock.patch.object(meta_api, "get_insights", return_value=rows), \
     mock.patch.object(meta_api, "get_account_structure", return_value={"campaigns": [], "adsets": [], "ads": []}, create=True), \
     mock.patch.object(orchestrator, "_spend_to_usd_factor", return_value=(1.0, None)):
    r = dashboard_data._get_kpis_uncached(level="campaign", date_preset="last_7d", access_token="t", ad_account_id="act_1")
check("dollarli hisob o'zgarmaydi", abs(r["rows"][0]["spend"] - 1250000.0) < 1e-6 and r.get("spend_in_usd") is False)

start, end = dashboard_data._date_preset_bounds_utc("last_3d")
today_start_utc = tz_utils.to_utc(tz_utils.now_local().replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None))
check("last_3d CRM oynasi bugundan oldin tugaydi", end == today_start_utc, f"{end} vs {today_start_utc}")
check("last_3d aniq 3 kun", (end - start).days == 3, str(end - start))

if failures:
    sys.exit(1)
print("\nBARCHA TESTLAR O'TDI")
