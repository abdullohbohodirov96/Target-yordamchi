"""test_ig_hot_leads_offline.py -- PLAN 6-bosqich: DM kalit so'z -> issiq lid.
  1. Standart o'chiq: kalit so'z bo'lsa ham hech narsa qilinmaydi.
  2. Yoqilgach: webhook orqali "narxi qancha? 90 123 45 67" -> CRM'da lid
     (telefon bilan), suhbatga bog'lanadi, Telegram guruhga xabar.
  3. Shu suhbatda qayta yozsa -- ikkinchi lid/xabar yo'q.
  4. Avtojavob faqat matn bo'lsa va BIR marta; echo (biznes) xabari e'tiborsiz.
  5. Boshqa kompaniya sozlamasi ta'sir qilmaydi; sahifa formasi saqlaydi.

    cd app && python3 scripts/test_ig_hot_leads_offline.py
"""
import os
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("REPLIX_TEST_DEFAULT_UNSCOPED", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-dummy-token")
os.environ["FLASK_SECRET_KEY"] = "test-secret-hot"
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(tempfile.mkdtemp(), 'hot.db')}"

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402
import ig_dm_sync  # noqa: E402
import ig_hot_leads  # noqa: E402
import meta_api  # noqa: E402
import scheduler  # noqa: E402

db_module.init_db()
app_module.app.config["TESTING"] = True
app_module.app.config["WTF_CSRF_ENABLED"] = False
failures = []


def check(name, cond, detail=""):
    print(("OK:   " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


s = db_module.get_session()
try:
    with db_module.unscoped():
        a = db_module.Company(name="A", plan="unlimited", is_active=True, meta_page_id="PA", telegram_group_id="-1001", ig_business_id="IGA")
        a.set_meta_access_token("TOK_A")
        b = db_module.Company(name="B", plan="unlimited", is_active=True, meta_page_id="PB", ig_business_id="IGB")
        b.set_meta_access_token("TOK_B")
        s.add_all([a, b])
        s.commit()
        m = db_module.Manager(username="a_admin", role="admin", company_id=a.id)
        m.set_password("parol12345")
        s.add(m)
        s.commit()
        A, B = a.id, b.id
finally:
    s.close()


def company(cid):
    s_ = db_module.get_session()
    try:
        with db_module.unscoped():
            c = s_.get(db_module.Company, cid)
            s_.expunge(c)
            return c
    finally:
        s_.close()


def ingest(cid, sender, text, mid, echo=False):
    with db_module.scoped_as(cid):
        return ig_dm_sync.ingest_webhook_message(company(cid), sender_id=sender, recipient_id="BIZ", message_id=mid,
                                                 text=text, timestamp_ms=None, is_echo=echo)


def leads(cid):
    s_ = db_module.get_session()
    try:
        with db_module.scoped_as(cid):
            return s_.query(db_module.Lead).all()
    finally:
        s_.close()


tg = []
sent = []
with mock.patch.object(scheduler, "_tg_send", side_effect=lambda c, t: tg.append((c, t)) or {"ok": True}), \
     mock.patch.object(meta_api, "send_instagram_message", side_effect=lambda r, t, **k: sent.append((r, t, k.get("access_token"))) or {"ok": True}):
    ingest(A, "cust1", "Assalomu alaykum, narxi qancha?", "m1")
    check("standart o'chiq: lid yaratilmadi", leads(A) == [])

    with db_module.scoped_as(A):
        ig_hot_leads.save_settings(A, enabled=True, keywords_text="narx, buyurtma", auto_reply=True, reply_text="Rahmat! Tez javob beramiz.")
    ingest(A, "cust2", "Salom", "m2")
    check("kalit so'zsiz xabar: lid yo'q", leads(A) == [])
    ingest(A, "cust2", "Narxi qancha? Tel: 90 123 45 67", "m3")
    ls = leads(A)
    check("issiq lid yaratildi", len(ls) == 1 and ls[0].source == "dm" and ls[0].phone == "+998901234567", str([(l.source, l.phone) for l in ls]))
    check("lid izohida kalit so'z", ls and "narx" in (ls[0].quality_note or ""))
    check("Telegram guruhga xabar", len(tg) == 1 and tg[0][0] == -1001 and "Issiq lid" in tg[0][1], str(tg))
    check("avtojavob bir marta, o'z tokeni bilan", sent == [("cust2", "Rahmat! Tez javob beramiz.", "TOK_A")], str(sent))
    s_ = db_module.get_session()
    try:
        with db_module.scoped_as(A):
            conv = s_.query(db_module.IgDmConversation).filter_by(customer_ig_id="cust2").one()
            check("suhbat lidga bog'landi", conv.linked_lead_id == ls[0].id)
    finally:
        s_.close()

    ingest(A, "cust2", "buyurtma bermoqchiman", "m4")
    check("qayta yozsa: ikkinchi lid yo'q", len(leads(A)) == 1)
    check("qayta yozsa: ikkinchi Telegram/avtojavob yo'q", len(tg) == 1 and len(sent) == 1)
    ingest(A, "cust3", "narx?", "m5", echo=True)
    check("biznes (echo) xabari e'tiborsiz", len(leads(A)) == 1)

    ingest(B, "custB", "narxi qancha", "m6")
    check("B kompaniyada (o'chiq) lid yo'q", leads(B) == [])

    with db_module.scoped_as(A):
        d = ig_hot_leads.save_settings(A, enabled=True, keywords_text="", auto_reply=True, reply_text="")
    check("matnsiz avtojavob yoqilmaydi; bo'sh kalit so'z -- standart", d["auto_reply"] is False and d["keywords"] == ig_hot_leads.DEFAULT_KEYWORDS)

c = app_module.app.test_client()
c.post("/login", data={"username": "a_admin", "password": "parol12345"})
body = c.get("/instagram-xabarlar").get_data(as_text=True)
check("sahifada sozlama formasi", "/instagram-xabarlar/issiq-lid" in body)
c.post("/instagram-xabarlar/issiq-lid", data={"keywords": "zakaz", "reply_text": ""})
with db_module.scoped_as(A):
    st = ig_hot_leads.get_settings(A)
check("forma: o'chirildi va saqlandi", st["enabled"] is False and st["keywords"] == ["zakaz"], str(st))

if failures:
    print(f"\n{len(failures)} ta XATO")
    sys.exit(1)
print("\nBARCHA TESTLAR O'TDI")
