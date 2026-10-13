"""test_leads_realtime_quality_offline.py -- 2026-10, foydalanuvchi shikoyati:
"lid qo'shsam menejerlarga ko'rinmayapti", "target lidlarni o'zi avtomatik
tortolmayapti, instant formalardan hammasini srazu tortish kerak", "lid
sifatini har kuni hisobot qilib bersin -- aniq son bilan".

Tekshiruvlar (tarmoqsiz, vaqtinchalik SQLite, production fail-closed rejim):
  1. OAuth oynasi endi `leads_retrieval`/`pages_manage_ads`/`pages_manage_metadata` so'raydi
     (jonli xato: "Formalarni olishda xatolik ... kod 200" -- bu ruxsat hech qachon so'ralmagan).
  2. Ruxsat xatosi lidga xos, tushunarli matn bilan + yetishmayotgan ruxsatlar nomi.
  3. Reklama hisobidagi BOSHQA sahifadagi forma ham topiladi va o'qiladi.
  4. Qo'lda/import kiritilgan o'sha telefon -- dublikat emas, Meta ma'lumoti biriktiriladi.
  5. Real-vaqt `leadgen` webhook: imzoli so'rov -> lid darhol CRM'ga, menejerga xabar.
     Imzosiz so'rov -> 401, hech narsa yozilmaydi. Begona sahifa -> hech narsa.
  6. Platforma egasi B kompaniyasi CRM'ida "+ Yangi lead" -> lid B'ga yoziladi,
     B menejeri ko'radi, tanlangan menejerga Telegram xabari.
  7. Kunlik sifat hisoboti -- aniq sonlar, faqat shu kunda tushganlar.

    cd app && python3 scripts/test_leads_realtime_quality_offline.py
"""

import datetime as dt
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("REPLIX_TEST_DEFAULT_UNSCOPED", None)
os.environ.pop("TENANT_SCOPE_MODE", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ["TELEGRAM_BOT_TOKEN"] = "123:abc"
os.environ["FLASK_SECRET_KEY"] = "test-realtime"
_TMP = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 'rt.db')}"

import requests  # noqa: E402

TG_SENT = []


class _Resp:
    def __init__(self, payload):
        self._p = payload
        self.text = json.dumps(payload)
        self.status_code = 200

    def json(self):
        return self._p


def _fake_post(url, *a, **kw):
    if "api.telegram.org" in url:
        TG_SENT.append(((kw.get("json") or {}).get("chat_id"), (kw.get("json") or {}).get("text")))
        return _Resp({"ok": True})
    raise requests.ConnectionError("offline")


def _offline(*_a, **_k):
    raise requests.ConnectionError("offline test")


requests.post = _fake_post
for _n in ("get", "put", "delete", "request"):
    setattr(requests, _n, _offline)
requests.Session.request = _offline  # type: ignore[assignment]

import app as app_module  # noqa: E402
import db  # noqa: E402
import kv_store  # noqa: E402
import lead_notify  # noqa: E402
import lead_sync  # noqa: E402
import meta_api  # noqa: E402
import tz_utils  # noqa: E402

meta_api._retry_sleep = lambda attempt: None
app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
db.init_db()

FAILED = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


PW = "parol12345"
FAR = dt.datetime.utcnow() + dt.timedelta(days=3650)
s = db.get_session()
with db.unscoped():
    owner_co_id = db.get_default_company_id()
    owner = db.Manager(username="owner", full_name="Ega", role="admin", company_id=owner_co_id)
    owner.set_password(PW)
    cb = db.Company(name="B savdo", plan="unlimited", is_active=True, paid_until=FAR,
                    meta_page_id="pageB", meta_ad_account_id="act_B", telegram_group_id="-100777")
    cb.set_meta_access_token("tokB")
    s.add_all([owner, cb])
    s.commit()
    B = cb.id
    b_admin = db.Manager(username="b_admin", full_name="B Admin", role="admin", company_id=B)
    b_admin.set_password(PW)
    b_mgr = db.Manager(username="b_mgr", full_name="Muslima B", role="manager", company_id=B,
                       telegram_user_id="5551", allowed_modules=json.dumps(["leads"]))
    b_mgr.set_password(PW)
    s.add_all([b_admin, b_mgr])
    s.commit()
    B_MGR = b_mgr.id
    db.seed_default_funnel_stages_for_company(B)
    db.seed_default_funnel_stages_for_company(owner_co_id)
s.close()

credsB = lead_sync._CompanyCreds(id=B, meta_page_id="pageB", meta_access_token="tokB", meta_ad_account_id="act_B")


def leads_b():
    s = db.get_session()
    try:
        with db.scoped_as(B):
            return s.query(db.Lead).order_by(db.Lead.id).all()
    finally:
        s.close()


def raw(i, form="fB", phone="+998901112233", name=None):
    return {"id": f"ML{i}", "created_time": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S+0000"),
            "form_id": form, "campaign_id": "c1", "adset_id": "as1", "ad_id": "ad1",
            "field_data": [{"name": "full_name", "values": [name or f"Mijoz {i}"]},
                           {"name": "phone_number", "values": [phone]}]}


STRUCT = {"campaigns": [{"id": "c1", "name": "AB | Lead | peno"}], "adsets": [{"id": "as1", "name": "usta"}],
          "ads": [{"id": "ad1", "name": "Video 1"}]}

# ---------------------------------------------------------------- 1. OAuth
url = meta_api.oauth_dialog_url("https://replix.uz/cb", "st", include_ads_scope=True)
check("OAuth: leads_retrieval so'raladi", "leads_retrieval" in url)
check("OAuth: pages_manage_ads va pages_manage_metadata so'raladi", "pages_manage_ads" in url and "pages_manage_metadata" in url)
with mock.patch.dict(os.environ, {"META_OAUTH_SKIP_LEAD_SCOPES": "1"}):
    check("OAuth: favqulodda o'chirish bayrog'i ishlaydi", "leads_retrieval" not in meta_api.oauth_dialog_url("https://x/cb", "s", include_ads_scope=False))

# ---------------------------------------------------------------- 2. Ruxsat xatosi
T0 = int(time.time()) - 3600
kv_store.set_json(lead_sync._since_key(B), T0)
kv_store.set_json(lead_sync._backlog_key(B), T0)
perm_err = meta_api.MetaAPIError({"message": "(#200) Permissions error", "code": 200})
with mock.patch.object(meta_api, "get_lead_forms", side_effect=perm_err), \
     mock.patch.object(meta_api, "get_ad_account_lead_form_ids", return_value=set()), \
     mock.patch.object(meta_api, "get_granted_permissions", return_value={"pages_show_list", "ads_management"}):
    r = lead_sync.sync_once(company=credsB)
joined = " | ".join(r["errors"])
check("ruxsat xatosi: lidga xos tushunarli matn", "lidlarni o'qishga ruxsat" in joined, joined)
check("ruxsat xatosi: yetishmayotgan ruxsatlar nomi bilan", "leads_retrieval" in joined and "pages_manage_ads" in joined, joined)

# ---------------------------------------------------------------- 3+4. Barcha formalar + birlashtirish
s = db.get_session()
with db.scoped_as(B):
    imported = db.Lead(company_id=B, full_name="Import Ali", phone="+998 90 111 22 33", source="import",
                       status="contacted", assigned_manager_id=B_MGR)
    s.add(imported)
    s.commit()
    IMPORTED_ID = imported.id
s.close()

asked_pages = {}


def fake_get_leads(form_id, since=None, *, access_token=None, page_id=None):
    asked_pages[form_id] = page_id
    if form_id == "fB":
        return [raw(1, "fB", phone="+998901112233"), raw(2, "fB", phone="+998935554433")]
    if form_id == "fOther":
        return [raw(3, "fOther", phone="+998977776655")]
    return []


TG_SENT.clear()
kv_store.set_json(f"lead_sync_ad_forms:{B}", None)  # 2-qadamdagi bo'sh kesh
with mock.patch.object(meta_api, "get_lead_forms", return_value=[{"id": "fB", "name": "Asosiy forma", "leads_count": 2}]), \
     mock.patch.object(meta_api, "get_ad_account_lead_form_ids", return_value={"fB", "fOther"}), \
     mock.patch.object(meta_api, "get_lead_form_info", return_value={"id": "fOther", "name": "Boshqa sahifa formasi", "page": {"id": "pageOther"}}), \
     mock.patch.object(meta_api, "get_leads", side_effect=fake_get_leads), \
     mock.patch.object(meta_api, "get_account_structure", return_value=STRUCT), \
     mock.patch("meta_events.dispatch_lead_event"):
    r = lead_sync.sync_once(company=credsB)
check("sync xatosiz", not r["errors"], str(r["errors"]))
check("reklamadagi boshqa sahifa formasi ham tekshirildi", r["forms_checked"] == 2 and asked_pages.get("fOther") == "pageOther", str(asked_pages))
rows = leads_b()
by_meta = {l.meta_lead_id: l for l in rows if l.meta_lead_id}
check("2 ta yangi lid (ML2, ML3) + 1 ta birlashtirildi (ML1)", r["new_leads"] == 2 and r.get("merged_leads") == 1, str(r))
merged = next(l for l in rows if l.id == IMPORTED_ID)
check("import lidga Meta ma'lumoti biriktirildi (dublikat yo'q)", merged.meta_lead_id == "ML1" and merged.campaign_name == "AB | Lead | peno"
      and len([l for l in rows if l.phone and l.phone.replace(' ', '').endswith('901112233')]) == 1)
check("birlashtirilgan lidning holati/menejeri saqlandi", merged.status == "contacted" and merged.assigned_manager_id == B_MGR)
check("yangi lidlarga kampaniya/reklama nomi yozildi", by_meta["ML2"].ad_name == "Video 1" and by_meta["ML3"].form_name == "Boshqa sahifa formasi")
check("yangi lidlar haqida B menejeriga Telegram xabari", any(c == "5551" and "Yangi lid" in (t or "") for c, t in TG_SENT), str(TG_SENT))

# ---------------------------------------------------------------- 5. Real-vaqt webhook
meta_api.META_APP_SECRET = "appsecret"
client = app_module.app.test_client()


def signed_post(payload):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"appsecret", body, hashlib.sha256).hexdigest()
    return client.post("/webhooks/instagram", data=body, content_type="application/json",
                       headers={"X-Hub-Signature-256": sig})


payload = {"object": "page", "entry": [{"id": "pageB", "time": 1, "changes": [
    {"field": "leadgen", "value": {"leadgen_id": "ML10", "form_id": "fB", "page_id": "pageB"}}]}]}
TG_SENT.clear()
threads = []
import threading  # noqa: E402

_orig_thread = threading.Thread


class _SyncThread(_orig_thread):  # webhook fon oqimini testda sinxron bajarish
    def start(self):
        self.run()


with mock.patch.object(app_module.threading, "Thread", _SyncThread), \
     mock.patch.object(meta_api, "get_lead_by_id", return_value=raw(10, "fB", phone="+998991230000", name="Tezkor Mijoz")), \
     mock.patch.object(meta_api, "get_lead_form_info", return_value={"id": "fB", "name": "Asosiy forma"}), \
     mock.patch.object(meta_api, "get_account_structure", return_value=STRUCT), \
     mock.patch("meta_events.dispatch_lead_event"):
    bad = client.post("/webhooks/instagram", data=json.dumps(payload), content_type="application/json")
    check("imzosiz webhook -> 401", bad.status_code == 401)
    check("imzosiz webhook lid yozmadi", "ML10" not in {l.meta_lead_id for l in leads_b()})
    ok = signed_post(payload)
    check("imzoli leadgen webhook -> 200", ok.status_code == 200)
    rows = leads_b()
    hit = [l for l in rows if l.meta_lead_id == "ML10"]
    check("real-vaqt lid darhol B CRM'ida", len(hit) == 1 and hit[0].full_name == "Tezkor Mijoz" and hit[0].company_id == B)
    check("real-vaqt lid haqida menejerga xabar", any(c == "5551" and "Tezkor Mijoz" in (t or "") for c, t in TG_SENT), str(TG_SENT))
    signed_post(payload)
    check("takroriy webhook dublikat yaratmaydi", len([l for l in leads_b() if l.meta_lead_id == "ML10"]) == 1)
    other = {"object": "page", "entry": [{"id": "pageX", "changes": [{"field": "leadgen", "value": {"leadgen_id": "MLX"}}]}]}
    signed_post(other)
s2 = db.get_session()
with db.unscoped():
    check("begona sahifa lidi hech qayerga yozilmadi", s2.query(db.Lead).filter_by(meta_lead_id="MLX").count() == 0)
s2.close()

# ---------------------------------------------------------------- 6. Egasi B CRM'ida qo'lda lid
TG_SENT.clear()
oc = app_module.app.test_client()
oc.post("/login", data={"username": "owner", "password": PW})
page = oc.get(f"/leads/new?company_id={B}").get_data(as_text=True)
check("egasi B formasida B menejeri ro'yxatda", "Muslima B" in page and f'name="company_id" value="{B}"' in page)
with mock.patch.object(app_module.threading, "Thread", _SyncThread):
    resp = oc.post("/leads/new", data={"full_name": "QOLDA_B_LID", "phone": "+998900000001",
                                       "company_id": str(B), "assigned_manager_id": str(B_MGR)})
check("qo'lda lid saqlandi", resp.status_code == 302)
manual = [l for l in leads_b() if l.full_name == "QOLDA_B_LID"]
check("lid egasining emas, B kompaniyasiga yozildi", len(manual) == 1 and manual[0].assigned_manager_id == B_MGR)
check("tanlangan menejerga Telegram xabari", any(c == "5551" and "QOLDA_B_LID" in (t or "") for c, t in TG_SENT), str(TG_SENT))
mc = app_module.app.test_client()
mc.post("/login", data={"username": "b_mgr", "password": PW})
check("B menejeri lidni CRM'da ko'radi", "QOLDA_B_LID" in mc.get("/leads").get_data(as_text=True))
# Begona kompaniya ID bilan oddiy admin -- e'tiborsiz (o'z kompaniyasiga yoziladi)
bc = app_module.app.test_client()
bc.post("/login", data={"username": "b_admin", "password": PW})
bc.post("/leads/new", data={"full_name": "B_ADMIN_LID", "phone": "+998900000002", "company_id": str(owner_co_id)})
s3 = db.get_session()
with db.unscoped():
    x = s3.query(db.Lead).filter_by(full_name="B_ADMIN_LID").first()
    check("oddiy admin company_id bilan boshqa kompaniyaga yoza olmaydi", x is not None and x.company_id == B)
s3.close()

# ---------------------------------------------------------------- 7. Kunlik sifat hisoboti
s = db.get_session()
with db.scoped_as(B):
    s.query(db.Lead).delete()
    now = dt.datetime.utcnow()
    yesterday = now - dt.timedelta(days=1, hours=2)
    data = [("qualified", "meta", "Forma A"), ("qualified", "meta", "Forma A"), ("sold", "meta", "Forma A"),
            ("unqualified", "meta", "Forma B"), ("unqualified", "import", None), ("contacted", "meta", "Forma B"),
            ("new", "meta", "Forma B")]
    for i, (st, src, form) in enumerate(data):
        s.add(db.Lead(company_id=B, full_name=f"R{i}", status=st, source=src, form_name=form,
                      lead_created_time=now - dt.timedelta(minutes=5), assigned_manager_id=B_MGR if i < 4 else None))
    s.add(db.Lead(company_id=B, full_name="Kechagi", status="qualified", source="meta", lead_created_time=yesterday, created_at=yesterday))
    old_lead = db.Lead(company_id=B, full_name="Eski Xaridor", status="sold", source="meta", form_name="Forma A",
                       lead_created_time=now - dt.timedelta(days=5), created_at=now - dt.timedelta(days=5))
    s.add(old_lead)
    s.flush()
    s.add(db.Sale(company_id=B, lead_id=old_lead.id, manager_id=B_MGR, sale_number=1, amount=2500000, sold_at=now - dt.timedelta(minutes=3)))
    s.add(db.Sale(company_id=B, lead_id=old_lead.id, manager_id=B_MGR, sale_number=2, amount=999, sold_at=now - dt.timedelta(minutes=2), is_returned=True))
    s.commit()
s.close()
today = tz_utils.today_local()
c = lead_notify.lead_quality_counts(B, *lead_notify._day_bounds_utc(today))
check("faqat bugungi lidlar sanaldi (7 ta, kechagisi yo'q)", c["total"] == 7, str(c))
check("aniq sonlar: sifatli 2 + sotildi 1, sifatsiz 2, jarayonda 1, yangi 1",
      (c["qualified"], c["sold"], c["unqualified"], c["in_progress"], c["new"]) == (2, 1, 2, 1, 1), str(c))
text = lead_notify.build_lead_quality_report(B, today)
check("hisobot matni: jami 7", "Jami tushgan lid: 7 ta" in text, text)
check("hisobot matni: sifatli 3 (sotildi 1)", "Sifatli: 3 ta (shundan sotildi: 1)" in text, text)
check("hisobot matni: sifatsiz 2", "Sifatsiz: 2 ta" in text, text)
check("hisobot matni: sifat darajasi 60%", "60%" in text, text)
check("hisobot matni: forma va menejer bo'yicha", "Forma A: 3 ta" in text and "Muslima B" in text, text)
came_str = (tz_utils.to_local(now - dt.timedelta(days=5))).strftime("%d.%m.%Y")
check("sotuvlar bo'limi: 1 ta (qaytarilgani sanalmaydi), summa", "Bugun sotildi: 1 ta — jami 2 500 000 so'm" in text, text)
check("sotuv qatori: qachon tushgan -> sotilgan, necha kunda, menejer", f"Eski Xaridor: {came_str}da tushgan" in text and "(5 kunda)" in text and "menejer: Muslima B" in text, text)
empty = lead_notify.build_lead_quality_report(B, today - dt.timedelta(days=30))
check("lid bo'lmagan kun -- '0 ta' xabari", "0 ta" in empty and "sotuv: 0 ta" in empty, empty)

import scheduler  # noqa: E402

sent = []
with mock.patch.object(scheduler, "_tg_send", side_effect=lambda chat, txt: sent.append((chat, txt)) or {"ok": True}), \
     mock.patch.object(scheduler, "_daily_summary_targets", return_value=[]):
    res = scheduler.job_lead_quality_report()
    check("kunlik job: B guruhiga yuborildi", any(ch == -100777 and "Lid sifati" in t for ch, t in sent), str(res))
    n = len(sent)
    scheduler.job_lead_quality_report()
    check("kuniga bir marta (takror yuborilmaydi)", len(sent) == n)
check("kunlik job boshqa kompaniya guruhiga B ma'lumotini yubormadi", all(ch == -100777 for ch, _ in sent))

print()
if FAILED:
    print(f"{len(FAILED)} ta test YIQILDI: {FAILED}")
    sys.exit(1)
print("Barcha testlar o'tdi.")
