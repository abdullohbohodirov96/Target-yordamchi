"""test_meta_campaign_payload_offline.py -- docs/PLAN.md, 2-bosqich:
  1. Graph API versiyasi v21.0 emas (standart v25.0), META_GRAPH_API_VERSION
     bilan almashtiriladi.
  2. Kampaniya yaratishda `is_adset_budget_sharing_enabled` HAR DOIM
     yuboriladi (Meta Ad Set byudjetli kampaniyada buni majburiy talab
     qiladi -- aks holda kampaniya Ads Manager'ga UMUMAN tushmaydi).
Tarmoqsiz: `meta_api._post` almashtiriladi.

Ishga tushirish:
    cd app && python3 scripts/test_meta_campaign_payload_offline.py
"""

import importlib
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

failures = []


def check(name, cond, detail=""):
    print(("OK:   " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def run_all():
    os.environ.pop("META_GRAPH_API_VERSION", None)
    import meta_api
    meta_api = importlib.reload(meta_api)
    check("standart versiya v25.0", meta_api.GRAPH_API_VERSION == "v25.0" and meta_api.GRAPH_URL.endswith("/v25.0"), meta_api.GRAPH_URL)
    os.environ.setdefault("META_APP_ID", "123")
    url = meta_api.oauth_dialog_url("https://replix.uz/cb", "st", include_ads_scope=True)
    check("OAuth dialog ham shu versiyada", "/v25.0/dialog/oauth" in url, url)

    os.environ["META_GRAPH_API_VERSION"] = "v26.0"
    meta_api = importlib.reload(meta_api)
    check("META_GRAPH_API_VERSION bilan almashtiriladi", meta_api.GRAPH_URL.endswith("/v26.0"), meta_api.GRAPH_URL)
    os.environ.pop("META_GRAPH_API_VERSION", None)
    meta_api = importlib.reload(meta_api)

    with mock.patch.object(meta_api, "_post", return_value={"id": "c1"}) as post:
        meta_api.create_campaign("Test", "OUTCOME_LEADS", "PAUSED", [], access_token="t", ad_account_id="act_1")
        path, payload = post.call_args[0][0], post.call_args[0][1]
        check("so'rov act_1/campaigns ga", path == "act_1/campaigns", path)
        check("is_adset_budget_sharing_enabled yuborildi (False)", payload.get("is_adset_budget_sharing_enabled") is False, str(payload))
        check("status PAUSED saqlandi", payload.get("status") == "PAUSED")
        meta_api.create_campaign("Test", "OUTCOME_LEADS", "PAUSED", [], access_token="t", ad_account_id="act_1", adset_budget_sharing=True)
        check("adset_budget_sharing=True uzatiladi", post.call_args[0][1].get("is_adset_budget_sharing_enabled") is True)

    if failures:
        print(f"\n{len(failures)} ta XATO")
        sys.exit(1)
    print("\nBARCHA TESTLAR O'TDI")


if __name__ == "__main__":
    run_all()
