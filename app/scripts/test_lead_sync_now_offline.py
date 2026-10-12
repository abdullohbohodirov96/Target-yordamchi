"""test_lead_sync_now_offline.py — "Replix Instant Form'dan lid tortmayapti"
(2026-10) tashxisi va himoyasi:

  1. Analitika -> Lead-sync kartasi: Facebook sahifa/token ulanmagan bo'lsa
     aniq qizil ogohlantirish + qayta ulash havolasi chiqadi.
  2. "Hozir tekshirish" (admin): ulanmagan kompaniya -> tushunarli xato;
     ulangan -> sync darhol ishlaydi, yangi lid CRM'ga tushadi, natija flash'da.
  3. Meta xatosi -> "xato bor" flash + kartada xato matni.
  4. Menejer bu tugmani ishlata olmaydi.
  5. Qotib qolgan sync qulfi 15 daqiqadan keyin eskirgan deb hisoblanadi --
     keyingi sync'lar abadiy o'tkazib yuborilmaydi; yangi qulf esa kutadi.
"""
import os
import sys
import tempfile
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(tempfile.mkdtemp(), 'lsn.db')}"
for k, v in (("ANTHROPIC_API_KEY", "x"), ("META_ACCESS_TOKEN", "x"), ("META_AD_ACCOUNT_ID", "act_x"), ("FLASK_SECRET_KEY", "s")):
    os.environ.setdefault(k, v)

import app as A  # noqa: E402
import db  # noqa: E402
import kv_store  # noqa: E402
import lead_sync  # noqa: E402
import meta_api  # noqa: E402

FAILED = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILED.append(name)


A.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
db.init_db()
s = db.get_session()
with db.unscoped():
    c_ok = db.Company(name="Ulangan", is_active=True, plan="unlimited", meta_page_id="page1")
    c_ok.set_meta_access_token("tok1") if hasattr(c_ok, "set_meta_access_token") else setattr(c_ok, "meta_access_token", "tok1")
    c_no = db.Company(name="Ulanmagan", is_active=True, plan="unlimited")
    s.add_all([c_ok, c_no]); s.commit()
    OK, NO = c_ok.id, c_no.id
    for u, cid, role in (("lsn_admin", OK, "admin"), ("lsn_admin2", NO, "admin"), ("lsn_mgr", OK, "manager")):
        m = db.Manager(username=u, full_name=u, role=role, company_id=cid, is_active=True)
        m.set_password("parol12345"); s.add(m)
    s.commit()
s.close()


def client(u):
    c = A.app.test_client()
    c.post("/login", data={"username": u, "password": "parol12345"})
    return c


adm, adm_no, mgr = client("lsn_admin"), client("lsn_admin2"), client("lsn_mgr")

# 1. Ulanmagan kompaniya kartasi
with A.app.test_request_context():
    an_url = A.url_for("analytics_page")
page = adm_no.get(an_url).get_data(as_text=True)
check("ulanmagan: qizil ogohlantirish + qayta ulash", "Lidlar tortilmayapti" in page and "Hozir tekshirish" in page)
page = adm.get(an_url).get_data(as_text=True)
check("ulangan: ogohlantirish yo'q, tugma bor", "Lidlar tortilmayapti" not in page and "Hozir tekshirish" in page)

# 2. Hozir tekshirish
r = adm_no.post("/analitika/lead-sync-now", follow_redirects=True)
check("ulanmagan -> tushunarli xato", "token ulanmagan" in r.get_data(as_text=True))

kv_store.set_json(lead_sync._since_key(OK), int(time.time()) - 3600)
kv_store.set_json(lead_sync._backlog_key(OK), int(time.time()) - 3600)
LEAD = {"id": "LSN1", "created_time": "2026-10-12T08:00:00+0000",
        "field_data": [{"name": "full_name", "values": ["Ali"]}, {"name": "phone_number", "values": ["+998901234567"]}]}
with mock.patch.object(meta_api, "get_lead_forms", return_value=[{"id": "f1", "name": "Forma", "leads_count": 1}]) as gf, \
     mock.patch.object(meta_api, "get_leads", return_value=[LEAD]), \
     mock.patch.object(meta_api, "get_account_structure", return_value={}), \
     mock.patch("meta_events.dispatch_lead_event"):
    r = adm.post("/analitika/lead-sync-now", follow_redirects=True)
html = r.get_data(as_text=True)
check("sync darhol ishladi, kompaniya sahifasi bilan", gf.call_args.args[0] == "page1")
check("flash: 1 ta yangi lid", "1 ta yangi lid" in html)
s = db.get_session()
with db.scoped_as(OK):
    check("lid CRM'ga tushdi (to'g'ri kompaniyaga)", s.query(db.Lead).filter_by(meta_lead_id="LSN1").count() == 1)
s.close()

# 3. Meta xatosi
with mock.patch.object(meta_api, "get_lead_forms", side_effect=meta_api.MetaAPIError({"message": "Error validating access token", "code": 190})):
    r = adm.post("/analitika/lead-sync-now", follow_redirects=True)
html = r.get_data(as_text=True)
check("Meta xatosi -> 'xato bor' flash + kartada sabab", "xato bor" in html and "Facebook" in html)

# 4. Menejer
r = mgr.post("/analitika/lead-sync-now")
check("menejer ishlata olmaydi", r.status_code == 302 and "lead-sync" not in (r.location or ""))

# 5. Qotgan qulf
lock = lead_sync._acquire_company_lock(999)
check("birinchi qulf olindi", lock is not None)
check("band qulf -> ikkinchisi kutmaydi (None)", lead_sync._acquire_company_lock(999) is None)
lead_sync._company_locks[999]["since"] = time.monotonic() - 16 * 60
lock2 = lead_sync._acquire_company_lock(999)
check("15 daqiqadan eski qulf eskirgan -> yangi sync davom etadi", lock2 is not None and lock2 is not lock)
lock2.release()
try:
    lock.release()  # eski (qotgan) sync oxiri yetib kelsa -- xato bermaydi
    ok = True
except RuntimeError:
    ok = False
check("eski sync tugasa ham xato yo'q", ok)

print()
if FAILED:
    print(f"{len(FAILED)} ta test YIQILDI: {FAILED}")
    sys.exit(1)
print("Hammasi o'tdi.")
