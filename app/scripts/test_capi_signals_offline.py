"""test_capi_signals_offline.py -- docs/PLAN.md, 2-bosqich: CAPI signallari
(yangi lid, sifatli lid, sotuv) to'g'ri tuzilishda yuborilishini tarmoqsiz
tekshiradi (`meta_api._post` almashtiriladi).
  1. Lead Ads lidi (lead_id bor): user_data.lead_id, action_source
     "system_generated", custom_data.event_source="crm" va lead_event_source.
  2. Sotuv: value + currency.
  3. Telefon/email faqat SHA-256 xesh ko'rinishida (ochiq raqam yo'q).
  4. event_id barqaror (dublikatdan himoya).
  5. Moslashtiradigan ma'lumot bo'lmasa -- hech narsa yuborilmaydi.

Ishga tushirish:
    cd app && python3 scripts/test_capi_signals_offline.py
"""

import json
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import meta_api  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(("OK:   " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def _send(event_name, **kw):
    with mock.patch.object(meta_api, "_post", return_value={"events_received": 1, "fbtrace_id": "t"}) as post:
        meta_api.send_conversion_event(event_name, pixel_id="px1", access_token="tok", **kw)
    if not post.called:
        return None, None
    path, payload = post.call_args[0][0], post.call_args[0][1]
    return path, payload["data"][0]


def run_all():
    path, ev = _send("Lead", phone="+998 90 123-45-67", lead_id="1234567890", external_id="55", event_id="lead-55-lead")
    check("so'rov px1/events ga", path == "px1/events", path)
    check("Lead: user_data.lead_id bor", ev["user_data"].get("lead_id") == "1234567890")
    check("Lead: action_source system_generated", ev["action_source"] == "system_generated")
    check("Lead: custom_data.event_source=crm", ev.get("custom_data", {}).get("event_source") == "crm", json.dumps(ev))
    check("Lead: lead_event_source bor", ev.get("custom_data", {}).get("lead_event_source") == meta_api.CAPI_LEAD_EVENT_SOURCE)
    check("telefon xeshlangan (ochiq raqam yo'q)", "998901234567" not in json.dumps(ev) and len(ev["user_data"]["ph"][0]) == 64)
    check("event_id barqaror", ev["event_id"] == "lead-55-lead")

    _, ev = _send("QualifiedLead", phone="+998901234567", lead_id="1234567890", event_id="lead-55-qualifiedlead")
    check("QualifiedLead: crm maydonlari bilan", ev["event_name"] == "QualifiedLead" and ev["custom_data"]["event_source"] == "crm")

    _, ev = _send("Purchase", email="Ali@Mail.uz", lead_id="1234567890", value=1500000, event_id="lead-55-purchase-1")
    check("Purchase: value + currency", ev["custom_data"]["value"] == 1500000.0 and ev["custom_data"]["currency"] == "UZS")
    check("Purchase: email xeshlangan", "ali@mail.uz" not in json.dumps(ev).lower() and len(ev["user_data"]["em"][0]) == 64)

    _, ev = _send("Purchase", phone="+998901234567", value=100)
    check("sayt lidi (lead_id yo'q): crm maydonlari qo'shilmaydi", "event_source" not in ev.get("custom_data", {}))

    path, ev = _send("Lead")
    check("moslashtiradigan ma'lumot yo'q -- yuborilmaydi", path is None)

    if failures:
        print(f"\n{len(failures)} ta XATO")
        sys.exit(1)
    print("\nBARCHA TESTLAR O'TDI")


if __name__ == "__main__":
    run_all()
