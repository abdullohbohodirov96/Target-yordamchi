"""test_lead_sync_reliability_offline.py — Meta lidlari CRM'ga "uzilib
qolmasdan" tushishi (2026-10, foydalanuvchi: "web crm ga leadlani avtomatik
aniq tortvotimi bir uzilib qovoti"):

  1. Bitta forma xato bersa -- O'SHA formaning cursori joyida qoladi, keyingi
     sync o'sha oynadan qayta o'qiydi va lid YO'QOLMAYDI (ilgari yo'qolardi).
  2. Muvaffaqiyatli formaning cursori oldinga suriladi; overlap 2 soat.
  3. Kech ko'ringan lid (Meta 30 daqiqa kechiktirgan) keyingi sync'da olinadi.
  4. Boshqa kompaniyada allaqachon bor `meta_lead_id` -- IntegrityError bilan
     butun sync yiqilmaydi, qolgan lidlar yoziladi.
  5. Bir kompaniya uchun parallel sync -- ikkinchisi o'tkazib yuboriladi.
  6. Status: consecutive_failures / last_success_at; kampaniya nomi xatosi
     "xato" emas (ogohlantirish).
  7. Scheduler: 2+ ketma-ket xatoda kompaniya Telegram guruhiga ogohlantirish
     (6 soatda bir marta), tiklanganda "tiklandi" xabari; guruhi yo'q
     kompaniyaga hech narsa yuborilmaydi.
"""
import os
import sys
import tempfile
import threading
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(tempfile.mkdtemp(), 'ls.db')}"
for k, v in (("ANTHROPIC_API_KEY", "test-dummy"), ("META_ACCESS_TOKEN", "test-dummy-token"),
             ("META_AD_ACCOUNT_ID", "act_test"), ("FLASK_SECRET_KEY", "test-secret"), ("TELEGRAM_BOT_TOKEN", "123:abc")):
    os.environ.setdefault(k, v)

import db  # noqa: E402
import kv_store  # noqa: E402
import lead_sync  # noqa: E402
import meta_api  # noqa: E402
import scheduler  # noqa: E402

FAILED = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILED.append(name)


db.init_db()
s = db.get_session()
with db.unscoped():
    ca = db.Company(name="LS A", is_active=True, plan="unlimited", telegram_group_id="-1001")
    cb = db.Company(name="LS B", is_active=True, plan="unlimited")
    s.add_all([ca, cb]); s.commit()
    A, B = ca.id, cb.id
s.close()

credsA = lead_sync._CompanyCreds(id=A, meta_page_id="pageA", meta_access_token="tokA", meta_ad_account_id=None)
credsB = lead_sync._CompanyCreds(id=B, meta_page_id="pageB", meta_access_token="tokB", meta_ad_account_id=None)
FORMS = [{"id": "f1", "name": "Forma 1", "leads_count": 5}, {"id": "f2", "name": "Forma 2", "leads_count": 5}]


def lead(i, form="f1", ts="2026-10-06T10:00:00+0000"):
    return {"id": f"L{i}", "created_time": ts, "form_id": form,
            "field_data": [{"name": "full_name", "values": [f"Mijoz {i}"]}, {"name": "phone_number", "values": ["+998901234567"]}]}


def leads_in(company_id):
    s = db.get_session()
    try:
        with db.scoped_as(company_id):
            return sorted(x.meta_lead_id for x in s.query(db.Lead).all())
    finally:
        s.close()


# Boshlang'ich cursor (birinchi sync faqat cursor qo'yadi)
T0 = int(time.time()) - 3 * 3600
kv_store.set_json(lead_sync._since_key(A), T0)
kv_store.set_json(lead_sync._backlog_key(A), T0)

calls = []


def fake_get_leads_fail_f2(form_id, since=None, **kw):
    calls.append((form_id, since))
    if form_id == "f2":
        raise meta_api.MetaAPIError({"message": "rate limit", "code": 17})
    return [lead(1, "f1")]


with mock.patch.object(meta_api, "get_lead_forms", return_value=FORMS), \
     mock.patch.object(meta_api, "get_leads", side_effect=fake_get_leads_fail_f2), \
     mock.patch("meta_events.dispatch_lead_event"):
    r1 = lead_sync.sync_once(company=credsA)
check("1-sync: f1 lidi yozildi, f2 xato", r1["new_leads"] == 1 and any("Forma 2" in e for e in r1["errors"]))
cur = kv_store.get_json(lead_sync._form_since_key(A))
check("f2 cursori joyida qoldi (T0), f1 oldinga surildi (hozir - 2 soat)",
      cur.get("f2") == T0 and abs(cur.get("f1") - (time.time() - lead_sync._SYNC_OVERLAP_SECONDS)) < 120)
check("overlap 2 soat", lead_sync._SYNC_OVERLAP_SECONDS == 7200)
st = lead_sync.get_last_status(A)
check("status: consecutive_failures=1, last_success_at yo'q", st["consecutive_failures"] == 1 and not st.get("last_success_at"))

# 2-sync: f2 endi ishlaydi -- uzilish vaqtidagi lid (L2) eski oynadan olinadi
calls.clear()


def fake_get_leads_ok(form_id, since=None, **kw):
    calls.append((form_id, since))
    return [lead(1, "f1")] if form_id == "f1" else [lead(2, "f2")]


with mock.patch.object(meta_api, "get_lead_forms", return_value=FORMS), \
     mock.patch.object(meta_api, "get_leads", side_effect=fake_get_leads_ok), \
     mock.patch("meta_events.dispatch_lead_event"):
    r2 = lead_sync.sync_once(company=credsA)
since_by_form = dict(calls)
check("f2 eski cursor (T0) bilan qayta so'raldi", since_by_form.get("f2") == T0)
check("uzilish vaqtidagi L2 yozildi, L1 dublikat bo'lmadi", r2["new_leads"] == 1 and leads_in(A) == ["L1", "L2"])
st = lead_sync.get_last_status(A)
check("status: tiklandi -> consecutive_failures=0, last_success_at bor", st["consecutive_failures"] == 0 and st.get("last_success_at"))

# 3. Kech ko'ringan lid: cursor overlap ichida
cur = kv_store.get_json(lead_sync._form_since_key(A))
late_created = time.time() - 30 * 60  # 30 daq oldin yaratilgan, endi ko'rindi
check("30 daqiqa kech ko'ringan lid yangi oyna ichida (yo'qolmaydi)", cur["f1"] < late_created)

# 4. Boshqa kompaniyada bor meta_lead_id -- yiqilmaydi
kv_store.set_json(lead_sync._since_key(B), T0)
kv_store.set_json(lead_sync._backlog_key(B), T0)
with mock.patch.object(meta_api, "get_lead_forms", return_value=[{"id": "fb", "name": "B forma"}]), \
     mock.patch.object(meta_api, "get_leads", return_value=[lead(1, "fb"), lead(9, "fb")]), \
     mock.patch("meta_events.dispatch_lead_event"):
    rb = lead_sync.sync_once(company=credsB)
check("B: A'dagi L1 o'tkazildi, L9 yozildi, xato yo'q", rb["new_leads"] == 1 and leads_in(B) == ["L9"] and not rb["errors"])
check("A lidlari o'zgarmadi", leads_in(A) == ["L1", "L2"])

# 5. Parallel sync
lk = lead_sync._company_lock(A)
lk.acquire()
try:
    with mock.patch.object(meta_api, "get_lead_forms") as gf:
        rp = lead_sync.sync_once(company=credsA)
    check("parallel sync o'tkazib yuborildi (Meta chaqirilmadi)", rp.get("skipped") and gf.call_count == 0)
finally:
    lk.release()

# 6. Kampaniya nomi xatosi -- ogohlantirish, xato emas
credsA2 = lead_sync._CompanyCreds(id=A, meta_page_id="pageA", meta_access_token="tokA", meta_ad_account_id="act_1")
with mock.patch.object(meta_api, "get_lead_forms", return_value=FORMS), \
     mock.patch.object(meta_api, "get_leads", return_value=[]), \
     mock.patch.object(meta_api, "get_account_structure", side_effect=meta_api.MetaAPIError({"message": "x"})):
    r6 = lead_sync.sync_once(company=credsA2)
check("kampaniya nomlari xatosi -> notice, errors bo'sh", not r6["errors"] and any("Kampaniya nomlari" in n for n in r6["notices"]))

# 7. Telegram ogohlantirish
sent = []
with mock.patch.object(scheduler, "_tg_send", side_effect=lambda chat, text: sent.append((chat, text)) or {"ok": True}):
    for i in range(2):
        with mock.patch.object(meta_api, "get_lead_forms", side_effect=meta_api.MetaAPIError({"message": "Error validating access token", "code": 190})):
            with mock.patch.object(lead_sync, "sync_all_companies", side_effect=lambda: {"per_company": {A: lead_sync.sync_once(company=credsA), B: {"skipped": True}}}):
                scheduler.job_lead_sync()
    check("2 ketma-ket xatodan keyin A guruhiga 1 ta ogohlantirish", len(sent) == 1 and sent[0][0] == -1001 and "tushmayapti" in sent[0][1])
    with mock.patch.object(meta_api, "get_lead_forms", side_effect=meta_api.MetaAPIError({"message": "token", "code": 190})):
        with mock.patch.object(lead_sync, "sync_all_companies", side_effect=lambda: {"per_company": {A: lead_sync.sync_once(company=credsA)}}):
            scheduler.job_lead_sync()
    check("6 soat ichida takror ogohlantirish yo'q", len(sent) == 1)
    with mock.patch.object(meta_api, "get_lead_forms", return_value=FORMS), mock.patch.object(meta_api, "get_leads", return_value=[]):
        with mock.patch.object(lead_sync, "sync_all_companies", side_effect=lambda: {"per_company": {A: lead_sync.sync_once(company=credsA)}}):
            scheduler.job_lead_sync()
    check("tiklanganda 'tiklandi' xabari", len(sent) == 2 and "tiklandi" in sent[1][1])
    sent.clear()
    kv_store.set_json(lead_sync._status_key(B), {"consecutive_failures": 5, "errors": ["x"]})
    with mock.patch.object(lead_sync, "sync_all_companies", return_value={"per_company": {B: {"errors": ["x"]}}}):
        scheduler.job_lead_sync()
    check("Telegram guruhi yo'q kompaniyaga hech narsa yuborilmaydi", sent == [])

print()
if FAILED:
    print(f"{len(FAILED)} ta test YIQILDI: {FAILED}")
    sys.exit(1)
print("Hammasi o'tdi.")
