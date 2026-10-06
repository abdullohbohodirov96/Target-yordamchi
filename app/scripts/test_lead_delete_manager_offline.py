"""test_lead_delete_manager_offline.py — menejerlar lidni o'chira oladi
(2026-10, foydalanuvchi: "leadlani o'chirish huquqini berish kerak
managerlarga"), FK cheklovi YOQILGAN holda (PostgreSQL'dagi kabi):

  1. ESKI xato qayta tiklanadi: holat tarixi (LeadStatusEvent) bor lid --
     faqat izohni o'chirib o'chirish FK xatosi bilan yiqiladi.
  2. Menejer (faqat "leads" moduli) lid sahifasidan o'chiradi: lid, izoh,
     holat tarixi o'chadi; qo'ng'iroq yozuvi SAQLANADI (lead_id = NULL).
  3. Sotuvi bor lid o'chirilmaydi (tushunarli xabar).
  4. Ro'yxatdan bir nechtasini birdan o'chirish; sotuvlisi qoladi.
  5. Boshqa kompaniyaning lid ID'si yuborilsa -- o'chmaydi (tenant filtri).
  6. Ro'yxat sahifasida katakchalar va "Tanlanganlarni o'chirish" bor.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(tempfile.mkdtemp(), 'ld.db')}"
for k, v in (("ANTHROPIC_API_KEY", "x"), ("META_ACCESS_TOKEN", "x"), ("META_AD_ACCOUNT_ID", "act_x"), ("FLASK_SECRET_KEY", "s")):
    os.environ.setdefault(k, v)

from sqlalchemy import event  # noqa: E402

import db  # noqa: E402


@event.listens_for(db.engine, "connect")
def _fk_on(dbapi_conn, _rec):
    dbapi_conn.execute("PRAGMA foreign_keys=ON")


import app as A  # noqa: E402

FAILED = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILED.append(name)


A.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
db.init_db()
s = db.get_session()
with db.unscoped():
    c1 = db.Company(name="Del A", is_active=True, plan="unlimited")
    c2 = db.Company(name="Del B", is_active=True, plan="unlimited")
    s.add_all([c1, c2]); s.commit()
    m = db.Manager(username="del_mgr", full_name="Mgr", role="manager", company_id=c1.id, is_active=True)
    m.set_password("parol12345"); s.add(m); s.commit()

    def mk(name, company):
        l = db.Lead(full_name=name, phone="+998901112233", company_id=company, status="new", source="meta")
        s.add(l); s.commit()
        s.add(db.LeadStatusEvent(company_id=company, lead_id=l.id, old_status="new", new_status="contacted"))
        s.add(db.LeadNote(lead_id=l.id, text="izoh", company_id=company) if hasattr(db.LeadNote, "text") else db.LeadNote(lead_id=l.id, company_id=company))
        s.commit()
        return l.id

    old_style = mk("Eski usul", c1.id)
    single = mk("Bitta", c1.id)
    call = db.CallRecord(company_id=c1.id, lead_id=single, phone_number="+998901112233", direction="incoming", duration_seconds=10)
    s.add(call); s.commit(); call_id = call.id
    with_sale = mk("Sotuvli", c1.id)
    s.add(db.Sale(lead_id=with_sale, amount=100000.0, company_id=c1.id) if hasattr(db.Sale, "company_id") else db.Sale(lead_id=with_sale, amount=100000.0))
    s.commit()
    b1, b2 = mk("Ommaviy 1", c1.id), mk("Ommaviy 2", c1.id)
    other = mk("Boshqa kompaniya", c2.id)
s.close()

# 1. Eski xato
s = db.get_session()
try:
    with db.unscoped():
        lead = s.get(db.Lead, old_style)
        s.query(db.LeadNote).filter_by(lead_id=lead.id).delete()
        s.delete(lead)
        try:
            s.commit(); old_failed = False
        except Exception:
            s.rollback(); old_failed = True
finally:
    s.close()
check("ESKI usul (faqat izoh) FK xatosi bilan yiqiladi -- muammo qayta tiklandi", old_failed)


def exists(i):
    s = db.get_session()
    try:
        with db.unscoped():
            return s.get(db.Lead, i) is not None
    finally:
        s.close()


cl = A.app.test_client()
cl.post("/login", data={"username": "del_mgr", "password": "parol12345"})

# 2. Bitta lid
r = cl.post(f"/leads/{single}/delete")
check("menejer lidni o'chirdi (holat tarixi + qo'ng'iroq bor edi)", r.status_code == 302 and not exists(single))
s = db.get_session()
with db.unscoped():
    cr = s.get(db.CallRecord, call_id)
    check("qo'ng'iroq yozuvi saqlandi, lidga bog'lanishi uzildi", cr is not None and cr.lead_id is None)
    check("holat tarixi o'chdi", s.query(db.LeadStatusEvent).filter_by(lead_id=single).count() == 0)
s.close()

# 3. Sotuvli
r = cl.post(f"/leads/{with_sale}/delete", follow_redirects=True)
check("sotuvli lid o'chirilmadi + xabar", exists(with_sale) and "sotuv bor" in r.get_data(as_text=True))

# 4-5. Ommaviy
r = cl.post("/leads/bulk-delete", data={"lead_ids": [str(b1), str(b2), str(with_sale), str(other)], "next": "/leads"}, follow_redirects=True)
html = r.get_data(as_text=True)
check("ommaviy: 2 ta o'chdi", not exists(b1) and not exists(b2) and "2 ta lid" in html)
check("ommaviy: sotuvlisi qoldi + xabar", exists(with_sale) and "sotuv bor" in html)
check("boshqa kompaniyaning lidi o'chmadi", exists(other))
r = cl.post("/leads/bulk-delete", data={"next": "https://evil.example/"})
check("tashqi 'next' manziliga yo'naltirilmaydi", r.status_code == 302 and "evil" not in (r.location or ""))
r = cl.post("/leads/bulk-delete", data={"next": "//evil.example/x"})
check("'//evil' (protokolsiz) manzilga ham yo'naltirilmaydi", r.status_code == 302 and "evil" not in (r.location or ""))

# 6. Ro'yxat sahifasi
page = cl.get("/leads").get_data(as_text=True)
check("ro'yxatda katakcha va o'chirish tugmasi", 'name="lead_ids"' in page and "leadsBulkForm" in page and "Tanlanganlarni" in page)

print()
if FAILED:
    print(f"{len(FAILED)} ta test YIQILDI: {FAILED}")
    sys.exit(1)
print("Hammasi o'tdi.")
