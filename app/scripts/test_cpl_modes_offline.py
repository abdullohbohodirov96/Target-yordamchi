"""test_cpl_modes_offline.py -- PLAN 4-bosqich: CPL avtopilot darajalari.
  1. "warn" rejimi: reklama o'chirilmaydi, ogohlantirish (kuniga 1 marta).
  2. "pause" rejimi: pauza + kunlik hisoblagich; chegara (10) to'lsa -- warn.
  3. Dashboard: "Qayta yoqish" -- faqat o'z jurnalidagi reklama, faqat admin;
     yozuv "resumed"; begona ad_id rad etiladi.
  4. Sozlamalar: rejimni saqlash (kompaniyaga alohida).

    cd app && python3 scripts/test_cpl_modes_offline.py
"""
import os
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("REPLIX_TEST_DEFAULT_UNSCOPED", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-dummy-token")
os.environ["FLASK_SECRET_KEY"] = "test-secret-cplmode"
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(tempfile.mkdtemp(), 'cplmode.db')}"

import requests  # noqa: E402


def _offline(*_a, **_k):
    raise requests.ConnectionError("offline test")


for _name in ("get", "post", "put", "delete", "request"):
    setattr(requests, _name, _offline)
requests.Session.request = _offline  # type: ignore[assignment]

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402
import meta_api  # noqa: E402
import orchestrator as orch  # noqa: E402

meta_api._retry_sleep = lambda attempt: None

db_module.init_db()
app_module.app.config["TESTING"] = True
app_module.app.config["WTF_CSRF_ENABLED"] = False
failures = []


def check(name, cond, detail=""):
    print(("OK:   " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


RULES = {"cpl_hard_kill_usd": 1.5, "cpl_hard_kill_min_spend_usd": 3.0,
         "cpl_hard_kill_zero_lead_multiplier": 3.0, "protected_campaign_ids": []}


def _rows(n):
    return [{"id": f"ad_{i}", "name": f"Ad {i}", "status": "ACTIVE", "spend": 10.0, "cpl": 2.5,
             "crm_leads_total": 4, "goal": "", "meta_result": 0, "meta_leads": 0} for i in range(n)]


store: dict = {}


def _run(rows, company_id):
    with mock.patch.object(orch, "BUSINESS_RULES", RULES), \
         mock.patch.object(orch.kv_store, "get_json", side_effect=lambda k, default=None: store.get(k, default)), \
         mock.patch.object(orch.kv_store, "set_json", side_effect=lambda k, v: store.__setitem__(k, v)), \
         mock.patch.object(orch.dashboard_data, "get_kpis", return_value={"rows": rows}), \
         mock.patch.object(orch.meta_api, "get_account_structure", return_value={"ads": []}), \
         mock.patch.object(orch, "_spend_to_usd_factor", return_value=(1.0, None)), \
         mock.patch.object(orch, "_recently_auto_paused_ad_ids", return_value=set()), \
         mock.patch.object(orch, "_record_auto_pause"), \
         mock.patch.object(orch, "_record_auto_action") as rec, \
         mock.patch.object(orch, "_execute_and_verify_status") as pause, \
         db_module.scoped_as(company_id):
        res = orch.enforce_cpl_hard_kill()
    return res, [c.args[0] for c in pause.call_args_list], rec


def test_modes():
    cid = db_module.get_default_company_id()
    with mock.patch.object(orch.kv_store, "get_json", side_effect=lambda k, default=None: store.get(k, default)), \
         mock.patch.object(orch.kv_store, "set_json", side_effect=lambda k, v: store.__setitem__(k, v)):
        check("standart rejim -- pause", orch.get_cpl_mode(cid) == "pause")
        orch.set_cpl_mode("warn", cid)
        check("warn saqlandi", orch.get_cpl_mode(cid) == "warn")
        check("boshqa kompaniyaga ta'sir qilmadi", orch.get_cpl_mode(cid + 999) == "pause")
    res, paused, rec = _run(_rows(2), cid)
    check("warn: hech narsa pauza qilinmadi", paused == [], str(paused))
    check("warn: 2 ta ogohlantirish", len(res.get("warned", [])) == 2, str(res))
    check("warn: jurnalga 'warned' yozildi", all(c.args[3] == "warned" for c in rec.call_args_list) and rec.call_count == 2)
    res, paused, rec = _run(_rows(2), cid)
    check("warn: shu kuni qayta ogohlantirilmaydi", res.get("warned") == [] and rec.call_count == 0, str(res))

    with mock.patch.object(orch.kv_store, "get_json", side_effect=lambda k, default=None: store.get(k, default)), \
         mock.patch.object(orch.kv_store, "set_json", side_effect=lambda k, v: store.__setitem__(k, v)):
        orch.set_cpl_mode("pause", cid)
    store_before = dict(store)
    rows = [{**r, "id": f"p_{i}"} for i, r in enumerate(_rows(12))]
    res, paused, rec = _run(rows, cid)
    check(f"pause: kuniga ko'pi bilan {orch.MAX_AUTO_PAUSES_PER_DAY} ta pauza", len(paused) == orch.MAX_AUTO_PAUSES_PER_DAY, str(len(paused)))
    check("pause: qolganlari ogohlantirildi", len(res["warned"]) == 2 and "chegarasi to'lgan" in res["warned"][0]["reason"], str(res["warned"])[:200])
    del store_before


def test_resume_route():
    s = db_module.get_session()
    try:
        with db_module.unscoped():
            a_id = db_module.get_default_company_id()
            a = s.get(db_module.Company, a_id)
            a.meta_ad_account_id = "act_A"
            a.set_meta_access_token("TOKEN_A")
            a.plan = "unlimited"
            b = db_module.Company(name="B", plan="unlimited", is_active=True)
            s.add(b)
            s.commit()
            for comp, uname, role in ((a, "a_admin", "admin"), (a, "a_mgr", "manager")):
                m = db_module.Manager(username=uname, role=role, company_id=comp.id,
                                      allowed_modules='["dashboard","target","settings"]')
                m.set_password("parol12345")
                s.add(m)
            s.add(db_module.AdAutoActionLog(company_id=a_id, ad_id="ad_A1", ad_name="A reklama", action="paused", reason="CPL", cpl=3, spend=10))
            s.add(db_module.AdAutoActionLog(company_id=b.id, ad_id="ad_B1", ad_name="B reklama", action="paused", reason="CPL", cpl=3, spend=10))
            s.commit()
    finally:
        s.close()

    c = app_module.app.test_client()
    c.post("/login", data={"username": "a_admin", "password": "parol12345"})
    body = c.get("/").get_data(as_text=True)
    check("dashboard: 'Qayta yoqish' tugmasi", "/reklama/ad_A1/qayta-yoqish" in body)
    check("dashboard: B reklamasi ko'rinmaydi", "B reklama" not in body)
    with mock.patch.object(orch, "_execute_and_verify_status") as act:
        r = c.post("/reklama/ad_B1/qayta-yoqish")
        check("begona (B) reklamani yoqib bo'lmaydi", act.call_count == 0 and r.status_code == 302)
        c.post("/reklama/ad_A1/qayta-yoqish")
        check("o'z reklamasi o'z tokeni bilan yoqildi", act.call_count == 1 and act.call_args.args[:2] == ("ad_A1", "ACTIVE")
              and act.call_args.kwargs.get("access_token") == "TOKEN_A", str(act.call_args))
    s = db_module.get_session()
    try:
        with db_module.unscoped():
            actions = [r.action for r in s.query(db_module.AdAutoActionLog).filter_by(ad_id="ad_A1").all()]
    finally:
        s.close()
    check("jurnalga 'resumed' yozildi", "resumed" in actions, str(actions))
    body = c.get("/").get_data(as_text=True)
    check("qayta yoqilgandan keyin tugma yo'qoladi", "/reklama/ad_A1/qayta-yoqish" not in body)

    with mock.patch.object(orch.kv_store, "set_json") as kvset:
        c.post("/sozlamalar/cpl", data={"action": "set_cpl_mode", "cpl_mode": "warn"})
        check("sozlamalar: rejim saqlandi", kvset.called and kvset.call_args.args[1] == "warn", str(kvset.call_args))
    c.get("/logout")

    c.post("/login", data={"username": "a_mgr", "password": "parol12345"})
    with mock.patch.object(orch, "_execute_and_verify_status") as act:
        c.post("/reklama/ad_A1/qayta-yoqish")
        check("menejer reklamani yoqa olmaydi (faqat admin)", act.call_count == 0)


test_modes()
test_resume_route()
if failures:
    print(f"\n{len(failures)} ta XATO")
    sys.exit(1)
print("\nBARCHA TESTLAR O'TDI")
