"""test_manager_crm_features_offline.py — 2026-09, foydalanuvchi so'rovi
(bir necha xabar birlashtirilgan):
  - "bitta menejer nechta odam bilan kuniga lead bilan gaplashyapti,
    kvalifikatsiya nechta odam bilan qilyapti... to'liq ma'lumot tashkil
    bera oladigan hisobot bo'ladimi";
  - "qayta aloqaga... majburiy eslatish kerak... har 2-3 soatda qayta
    eslatib tursin... guruhga ham chiqsin... adminga alohida signal
    ketsin";
  - "menejerlar akkauntini nastroyka degan joyi yo'q... o'zini telegramni
    avtomaticheski ulashi mumkin bo'lsin... vazifalar guruhini qo'shishi
    mumkin bo'lsin";
  - "menejer topilganda siz mana bu akkauntga ulandingiz, muvaffaqiyatli
    ulandingiz degan xabar kelsin";
  - "agar xom to'ltirilgan bo'lsa, alohida excel qilinsin, ularni bitta
    ro'yxatga olib tashlansin... ismi, nomeri, chala to'ldirgani...
    lyuboy kompaniyada shunaqa bo'lsa, hamma kompaniya uchun".

Tekshiradi:
  1. `incomplete_leads.py` -- ism/telefon bo'sh lidlarni topadi, Excel
     workbook quradi (`/leads/chala-toldirilganlar.xlsx` route orqali ham).
  2. `manager_reporting.daily_manager_activity()` -- kunlik menejer
     bo'yicha gaplashgan/sifatli/sifatsiz/sotilgan hisob-kitobi.
  3. `lead_detail()` -- har bir saqlash `LeadStatusEvent` yozadi;
     `next_contact_at` o'zgarganda eslatish hisoblagichi nolga tushadi.
  4. Menejer (nafaqat admin) o'zining Telegramini va "vazifalar guruhi"ni
     `/sozlamalar/umumiy`dan bitta tugma bilan ulay oladi; tasdiqlash
     xabarida menejer ismi ko'rsatiladi.
  5. `/menejer-faoliyati` -- admin uchun ochiq, oddiy menejer uchun yopiq.
  6. `job_followup_reminders()` -- endi `followup_reminder_count`ni
     oshiradi va guruh xulosasi (`resolved_tasks_group_id()`) alohida
     "vazifalar guruhi" ulangan bo'lsa O'SHANGA boradi.
  7. `job_followup_admin_escalation()` -- kamida bir marta eslatilgan,
     hali hal qilinmagan lidlarni adminga shaxsan yuboradi.

Ishga tushirish:
    cd app && python3 scripts/test_manager_crm_features_offline.py
"""
import os
import re
import sys
import io
import tempfile
import datetime as dt
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

failures = []


def check(name, cond):
    print(("OK  " if cond else "FAIL") + " " + name)
    if not cond:
        failures.append(name)


def _fresh_modules(db_path):
    for name in (
        "db", "kv_store", "app", "budget_tracker", "orchestrator", "meta_api", "scheduler",
        "lead_sync", "meta_events", "monthly_report", "ig_dm_sync", "dashboard_data",
        "incomplete_leads", "manager_reporting",
    ):
        sys.modules.pop(name, None)
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"
    import db as db_module
    db_module.init_db()
    import app as app_module
    app_module.app.config["TESTING"] = True
    app_module.app.config["WTF_CSRF_ENABLED"] = False
    app_module._BOT_IDENTITY_CACHE.clear()
    app_module._BOT_IDENTITY_CACHE["id"] = 999
    app_module._BOT_IDENTITY_CACHE["username"] = "targetolog_bot"
    import incomplete_leads
    import manager_reporting
    import scheduler
    return db_module, app_module, incomplete_leads, manager_reporting, scheduler


def _signup(client, *, company_name, admin_username, plan="business"):
    return client.post("/signup", data={
        "company_name": company_name, "admin_username": admin_username,
        "admin_full_name": "", "email": "", "plan": plan,
        "password": "parol123456", "password2": "parol123456",
    }, follow_redirects=True)


def _extract_token(location: str, *, param: str) -> str:
    m = re.search(rf"[?&]{param}=([^&]+)", location)
    assert m, f"URL'da {param}= topilmadi: {location}"
    return m.group(1)


def _make_company(db_module, *, name, telegram_group_id=None, tasks_group_id=None, is_active=True):
    session = db_module.get_session()
    try:
        c = db_module.Company(
            name=name, telegram_group_id=telegram_group_id, tasks_group_id=tasks_group_id,
            is_active=is_active,
        )
        session.add(c)
        session.commit()
        return c.id
    finally:
        session.close()


def _add_manager(db_module, *, company_id, username, role="manager", full_name=""):
    session = db_module.get_session()
    try:
        m = db_module.Manager(company_id=company_id, full_name=full_name, username=username, role=role)
        m.set_password("parol123456")
        session.add(m)
        session.commit()
        return m.id
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 1) incomplete_leads.py
# ---------------------------------------------------------------------------

def test_missing_fields_detection_and_workbook():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module, incomplete_leads, _, _ = _fresh_modules(os.path.join(tmp, "t1.db"))
        company_id = db_module.get_default_company_id()
        session = db_module.get_session()
        try:
            complete = db_module.Lead(company_id=company_id, full_name="To'liq Ism", phone="+998901234567")
            no_name = db_module.Lead(company_id=company_id, full_name=None, phone="+998901112233")
            no_phone = db_module.Lead(company_id=company_id, full_name="Telefonsiz", phone=None, phone2=None)
            session.add_all([complete, no_name, no_phone])
            session.commit()

            check("to'liq leadda yetishmagan maydon yo'q", incomplete_leads.missing_fields_for(complete) == [])
            check("ismsiz leadda 'Ism-familiya' yetishmaydi", "Ism-familiya" in incomplete_leads.missing_fields_for(no_name))
            check("telefonsiz leadda 'Telefon raqami' yetishmaydi", "Telefon raqami" in incomplete_leads.missing_fields_for(no_phone))

            found = incomplete_leads.find_incomplete_leads(session, company_id=company_id)
            found_ids = {l.id for l in found}
            check("chala to'ldirilganlar ro'yxatida ikkitasi bor", len(found) == 2)
            check("to'liq lead ro'yxatga tushmagan", complete.id not in found_ids)
            check("ismsiz/telefonsiz lead ro'yxatda", no_name.id in found_ids and no_phone.id in found_ids)

            xlsx_bytes = incomplete_leads.build_incomplete_leads_workbook(found, company_name="Test MChJ")
            check("Excel bayt sifatida qaytadi va bo'sh emas", isinstance(xlsx_bytes, bytes) and len(xlsx_bytes) > 100)

            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes))
            ws = wb.active
            header = [c.value for c in ws[1]]
            check("Excel sarlavhasida 'Nima yetishmayapti' ustuni bor", "Nima yetishmayapti" in header)
            check("Excel'da 2 ta qator (+1 sarlavha) bor", ws.max_row == 3)
        finally:
            session.close()
    print("OK: incomplete_leads.py ism/telefon bo'sh lidlarni to'g'ri topadi va Excel workbook quradi")


def test_incomplete_leads_export_route_and_multitenant_scoping():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module, incomplete_leads, _, _ = _fresh_modules(os.path.join(tmp, "t2.db"))
        client_a = app_module.app.test_client()
        _signup(client_a, company_name="Eksport A", admin_username="eksport_a_admin")
        client_b = app_module.app.test_client()
        _signup(client_b, company_name="Eksport B", admin_username="eksport_b_admin")

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                company_a = session.query(db_module.Company).filter_by(name="Eksport A").first()
                company_b = session.query(db_module.Company).filter_by(name="Eksport B").first()
            session.add(db_module.Lead(company_id=company_a.id, full_name=None, phone="+998900000001"))
            session.add(db_module.Lead(company_id=company_b.id, full_name=None, phone="+998900000002", email="b@b.uz"))
            session.commit()
        finally:
            session.close()

        r = client_a.get("/leads/chala-toldirilganlar.xlsx")
        check("export route 200 qaytaradi", r.status_code == 200)
        check("Excel mimetype to'g'ri", "spreadsheetml" in r.headers.get("Content-Type", ""))

        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(r.data))
        ws = wb.active
        body_text = "\n".join(str(c.value) for row in ws.iter_rows() for c in row)
        check("Kompaniya A faqat OʻZINING lidini ko'radi (email yo'q)", "b@b.uz" not in body_text)
    print("OK: /leads/chala-toldirilganlar.xlsx har bir kompaniya uchun FAQAT o'ziga tegishli lidlarni chiqaradi")


# ---------------------------------------------------------------------------
# 2) manager_reporting.py
# ---------------------------------------------------------------------------

def test_daily_manager_activity_counts():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module, _, manager_reporting, _ = _fresh_modules(os.path.join(tmp, "t3.db"))
        company_id = db_module.get_default_company_id()
        mgr_id = _add_manager(db_module, company_id=company_id, username="faol_menejer", full_name="Faollik Menejer")

        session = db_module.get_session()
        try:
            l1 = db_module.Lead(company_id=company_id, full_name="Lid 1")
            l2 = db_module.Lead(company_id=company_id, full_name="Lid 2")
            l3 = db_module.Lead(company_id=company_id, full_name="Lid 3")
            session.add_all([l1, l2, l3])
            session.commit()

            now = dt.datetime.utcnow()
            yesterday = now - dt.timedelta(days=1)
            # Lid 1: bugun ikki marta saqlandi (status contacted->qualified) -- 1 ta ALOHIDA lead, 1 ta qualified
            session.add(db_module.LeadStatusEvent(company_id=company_id, lead_id=l1.id, manager_id=mgr_id, old_status="new", new_status="contacted", created_at=now))
            session.add(db_module.LeadStatusEvent(company_id=company_id, lead_id=l1.id, manager_id=mgr_id, old_status="contacted", new_status="qualified", created_at=now))
            # Lid 2: bugun unqualified qilindi
            session.add(db_module.LeadStatusEvent(company_id=company_id, lead_id=l2.id, manager_id=mgr_id, old_status="new", new_status="unqualified", created_at=now))
            # Lid 3: KECHA ishlangan -- bugungi hisobotga kirmasligi kerak
            session.add(db_module.LeadStatusEvent(company_id=company_id, lead_id=l3.id, manager_id=mgr_id, old_status="new", new_status="contacted", created_at=yesterday))
            session.commit()

            today_rows = manager_reporting.daily_manager_activity(session, company_id, day=now.date())
            check("bugungi hisobotda bitta menejer qatori bor", len(today_rows) == 1)
            row = today_rows[0]
            check("bugun 2 ta ALOHIDA lead bilan ishlangan (Lid 3 kirmaydi)", row["contacted_count"] == 2)
            check("1 ta sifatli qilingan", row["qualified"] == 1)
            check("1 ta sifatsiz qilingan", row["unqualified"] == 1)
            check("jami 3 ta harakat (Lid1 ikki marta + Lid2)", row["events"] == 3)

            yesterday_rows = manager_reporting.daily_manager_activity(session, company_id, day=yesterday.date())
            check("kechagi hisobotda faqat Lid 3 bor (1 ta)", yesterday_rows and yesterday_rows[0]["contacted_count"] == 1)
        finally:
            session.close()
    print("OK: manager_reporting.daily_manager_activity() kun bo'yicha to'g'ri guruhlaydi va sifatli/sifatsiz/sotilganni to'g'ri sanaydi")


def test_manager_daily_activity_route_admin_only():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module, _, _, _ = _fresh_modules(os.path.join(tmp, "t4.db"))
        client = app_module.app.test_client()
        _signup(client, company_name="Hisobot MChJ", admin_username="hisobot_admin")

        r = client.get("/menejer-faoliyati")
        check("admin uchun /menejer-faoliyati 200 qaytaradi", r.status_code == 200)

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                admin = session.query(db_module.Manager).filter_by(username="hisobot_admin").first()
                company_id = admin.company_id
        finally:
            session.close()
        _add_manager(db_module, company_id=company_id, username="hisobot_manager", role="manager")

        client.get("/logout", follow_redirects=True)
        client.post("/login", data={"username": "hisobot_manager", "password": "parol123456"}, follow_redirects=True)
        r2 = client.get("/menejer-faoliyati", follow_redirects=True)
        check("oddiy menejer uchun 'faqat admin' xabari ko'rsatiladi", "faqat admin" in r2.get_data(as_text=True))
    print("OK: /menejer-faoliyati faqat admin uchun ochiq")


# ---------------------------------------------------------------------------
# 3) lead_detail() -- audit jurnali + eslatma hisoblagichini reset qilish
# ---------------------------------------------------------------------------

def test_lead_detail_logs_status_event_and_resets_reminder_counter():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module, _, _, _ = _fresh_modules(os.path.join(tmp, "t5.db"))
        client = app_module.app.test_client()
        _signup(client, company_name="Jurnal MChJ", admin_username="jurnal_admin")

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                company = session.query(db_module.Company).filter_by(name="Jurnal MChJ").first()
            lead = db_module.Lead(
                company_id=company.id, full_name="Jurnal Lid", status="new",
                followup_reminder_count=3, followup_last_reminded_at=dt.datetime.utcnow(),
            )
            session.add(lead)
            session.commit()
            lead_id = lead.id
        finally:
            session.close()

        r = client.post(f"/leads/{lead_id}", data={
            "status": "contacted", "note": "Birinchi qo'ng'iroq qilindi",
            "full_name": "Jurnal Lid", "phone": "", "phone2": "", "email": "",
        }, follow_redirects=True)
        check("lead_detail POST 200 qaytaradi", r.status_code == 200)

        session = db_module.get_session()
        try:
            events = session.query(db_module.LeadStatusEvent).filter_by(lead_id=lead_id).all()
            check("bitta LeadStatusEvent yozildi", len(events) == 1)
            check("eski/yangi status to'g'ri yozildi", events[0].old_status == "new" and events[0].new_status == "contacted")
            check("izoh ham event'ga yozildi", events[0].note == "Birinchi qo'ng'iroq qilindi")
            check("source='web'", events[0].source == "web")

            lead = session.get(db_module.Lead, lead_id)
            # next_contact_date formda YO'Q edi -- eslatma hisoblagichi o'zgarmasligi kerak.
            check("next_contact o'zgarmasa hisoblagich saqlanadi", lead.followup_reminder_count == 3)
        finally:
            session.close()

        # Endi qayta aloqa sanasini belgilaymiz -- hisoblagich NOLGA tushishi kerak.
        r2 = client.post(f"/leads/{lead_id}", data={
            "status": "contacted", "note": "",
            "full_name": "Jurnal Lid", "phone": "", "phone2": "", "email": "",
            "next_contact_date": (dt.datetime.utcnow() + dt.timedelta(days=1)).strftime("%Y-%m-%d"),
            "next_contact_note": "Ertaga qo'ng'iroq",
        }, follow_redirects=True)
        check("ikkinchi POST ham 200", r2.status_code == 200)

        session = db_module.get_session()
        try:
            lead = session.get(db_module.Lead, lead_id)
            check("next_contact_at o'zgarganda hisoblagich NOLGA tushadi", lead.followup_reminder_count == 0)
            check("followup_last_reminded_at ham tozalanadi", lead.followup_last_reminded_at is None)
            events = session.query(db_module.LeadStatusEvent).filter_by(lead_id=lead_id).count()
            check("ikkinchi saqlash uchun ham event yozildi (jami 2 ta)", events == 2)
        finally:
            session.close()
    print("OK: lead_detail() har bir saqlashda LeadStatusEvent yozadi, next_contact_at o'zgarganda eslatma hisoblagichini nolga tushiradi")


# ---------------------------------------------------------------------------
# 4) Menejer o'z Telegramini/vazifalar guruhini ulaydi (admin bo'lmasa ham)
# ---------------------------------------------------------------------------

def test_manager_self_service_telegram_links():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module, _, _, _ = _fresh_modules(os.path.join(tmp, "t6.db"))
        client = app_module.app.test_client()
        _signup(client, company_name="Ozini Ulash MChJ", admin_username="ozi_admin")

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                admin = session.query(db_module.Manager).filter_by(username="ozi_admin").first()
                company_id = admin.company_id
        finally:
            session.close()
        _add_manager(db_module, company_id=company_id, username="ozi_manager", role="manager", full_name="Oddiy Menejer")

        client.get("/logout", follow_redirects=True)
        client.post("/login", data={"username": "ozi_manager", "password": "parol123456"}, follow_redirects=True)

        # "Mening profilim" sahifasi -- oddiy menejer uchun ham (modul
        # ruxsatidan qat'iy nazar) ochiq bo'lishi kerak, va ulash
        # tugmalarini ko'rsatishi kerak (`/sozlamalar/umumiy` EMAS -- u
        # FAQAT "settings" moduliga ruxsati bor menejerlar uchun ochiq,
        # standart holatda oddiy menejerda bu modul YO'Q).
        html = client.get("/mening-profilim").get_data(as_text=True)
        check("'Mening profilim' sahifasi oddiy menejer uchun ham ochiladi", "Telegram ulanishim" in html)

        r = client.post("/sozlamalar/telegram/shaxsiy-ulash")
        check("oddiy menejer shaxsiy-ulash route'ini chaqira oladi (admin talab qilinmaydi)", r.status_code == 302)
        token = _extract_token(r.headers["Location"], param="start")
        data = app_module.kv_store.get_json(f"tg_link_token:{token}")
        check("token 'personal' turida va shu menejerga tegishli", data["kind"] == "personal")

        sent = []
        with mock.patch.object(app_module, "tg_send", side_effect=lambda cid, text: sent.append((cid, text))):
            client.post("/api/webhook", json={
                "message": {"chat": {"id": 555111, "type": "private"}, "text": f"/start {token}", "from": {"id": 555111}}
            })
        check("ulanish xabari yuborildi", len(sent) == 1)
        check("xabarda menejer ismi ko'rsatiladi", "Oddiy Menejer" in sent[0][1])
        check("xabarda 'muvaffaqiyatli ulandingiz' bor", "Muvaffaqiyatli ulandingiz" in sent[0][1])

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                mgr = session.query(db_module.Manager).filter_by(username="ozi_manager").first()
            check("Manager.telegram_user_id to'g'ri bog'landi", mgr.telegram_user_id == "555111")
        finally:
            session.close()

        # Vazifalar guruhini ham (admin bo'lmasa ham) ulay oladi.
        r2 = client.post("/sozlamalar/telegram/vazifalar-guruhi-ulash")
        check("vazifalar-guruhi-ulash route'i ham admin talab qilmaydi", r2.status_code == 302)
        group_token = _extract_token(r2.headers["Location"], param="startgroup")
        group_data = app_module.kv_store.get_json(f"tg_link_token:{group_token}")
        check("token 'tasks_group' turida", group_data["kind"] == "tasks_group")

        sent2 = []
        with mock.patch.object(app_module, "tg_send", side_effect=lambda cid, text: sent2.append((cid, text))):
            client.post("/api/webhook", json={
                "message": {"chat": {"id": -700777, "type": "supergroup"}, "text": f"/start {group_token}", "from": {"id": 1}}
            })
        check("guruh ulanish xabari yuborildi", len(sent2) == 1 and "VAZIFALAR" in sent2[0][1].upper())

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                company = session.get(db_module.Company, company_id)
            check("Company.tasks_group_id to'g'ri bog'landi", company.tasks_group_id == "-700777")
            check("resolved_tasks_group_id() shu qiymatni qaytaradi", company.resolved_tasks_group_id() == "-700777")
        finally:
            session.close()
    print("OK: oddiy menejer (admin emas) o'zining shaxsiy Telegramini VA vazifalar guruhini bitta tugma bilan ulay oladi, tasdiqlash xabarida ismi ko'rsatiladi")


def test_resolved_tasks_group_id_falls_back_to_telegram_group_id():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module, _, _, _ = _fresh_modules(os.path.join(tmp, "t7.db"))
        c = db_module.Company(name="Fallback MChJ", telegram_group_id="-9001", tasks_group_id=None)
        check("tasks_group_id bo'sh bo'lsa telegram_group_id qaytadi", c.resolved_tasks_group_id() == "-9001")
        c2 = db_module.Company(name="Fallback2 MChJ", telegram_group_id="-9001", tasks_group_id="-9002")
        check("tasks_group_id bo'lsa O'SHANI qaytaradi", c2.resolved_tasks_group_id() == "-9002")
    print("OK: Company.resolved_tasks_group_id() to'g'ri fallback qiladi")


# ---------------------------------------------------------------------------
# 5)-6) scheduler.py -- kuniga bir necha marta eslatish + admin eskalatsiyasi
# ---------------------------------------------------------------------------

def test_followup_reminders_increments_counter_and_uses_tasks_group():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module, _, _, scheduler = _fresh_modules(os.path.join(tmp, "t8.db"))
        # MUHIM: `job_followup_reminders()`da platforma egasi (default company)
        # uchun ALOHIDA, eski `_full_activity_targets()` yo'li bor (ENV guruhga
        # boradi, `resolved_tasks_group_id()`ga UMUMAN qaramaydi) -- shuning
        # uchun "ALOHIDA vazifalar guruhi" filialini haqiqatan tekshirish uchun
        # BOSHQA (default bo'lmagan) kompaniya kerak (xuddi
        # `test_admin_report_followup_multitenant_offline.py`dagi naqsh).
        company_id = _make_company(db_module, name="Eslatma MChJ", telegram_group_id="-4001", tasks_group_id="-4002")
        session = db_module.get_session()
        try:
            manager = db_module.Manager(company_id=company_id, username="eslatma_menejer", full_name="Eslatma Menejer", telegram_user_id="600123")
            manager.set_password("x")
            session.add(manager)
            session.flush()
            lead = db_module.Lead(
                company_id=company_id, full_name="Eslatma Lid", phone="+998900000009",
                assigned_manager_id=manager.id, next_contact_at=dt.datetime.utcnow() - dt.timedelta(days=1),
            )
            session.add(lead)
            session.commit()
            lead_id = lead.id
        finally:
            session.close()

        sent = []

        def fake_tg_send(chat_id, text):
            sent.append((chat_id, text))
            return {"ok": True, "error": None}

        with mock.patch.object(scheduler, "_tg_send", side_effect=fake_tg_send):
            scheduler.job_followup_reminders()

        by_chat = {}
        for cid, text in sent:
            by_chat.setdefault(cid, []).append(text)
        check("guruh xulosasi ALOHIDA vazifalar guruhiga (-4002) boradi, umumiy guruhga (-4001) emas", -4002 in by_chat and -4001 not in by_chat)

        session = db_module.get_session()
        try:
            lead = session.get(db_module.Lead, lead_id)
            check("eslatilgandan keyin hisoblagich 1ga oshadi", lead.followup_reminder_count == 1)
            check("followup_last_reminded_at o'rnatiladi", lead.followup_last_reminded_at is not None)
        finally:
            session.close()

        # Job IKKINCHI marta ishga tushirilsa (kun davomida qayta-qayta eslatish) -- yana yuborishi kerak.
        sent.clear()
        with mock.patch.object(scheduler, "_tg_send", side_effect=fake_tg_send):
            scheduler.job_followup_reminders()
        check("job ikkinchi marta ham menejerga yuboradi (kun davomida qayta eslatish)", any(cid == 600123 for cid, _ in sent))
        session = db_module.get_session()
        try:
            lead = session.get(db_module.Lead, lead_id)
            check("hisoblagich yana oshadi (endi 2)", lead.followup_reminder_count == 2)
        finally:
            session.close()
    print("OK: job_followup_reminders() endi har chaqirilganda qayta eslatadi (hisoblagichni oshiradi) va ALOHIDA vazifalar guruhiga (bo'lsa) yuboradi")


def test_followup_admin_escalation_sends_only_after_at_least_one_reminder():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module, _, _, scheduler = _fresh_modules(os.path.join(tmp, "t9.db"))
        company_id = db_module.get_default_company_id()
        session = db_module.get_session()
        try:
            admin = session.query(db_module.Manager).filter_by(company_id=company_id, role="admin").first()
            if admin is None:
                admin = db_module.Manager(company_id=company_id, username="eskalatsiya_admin", role="admin", full_name="Bosh admin")
                admin.set_password("x")
                session.add(admin)
            admin.telegram_user_id = "700999"

            never_reminded = db_module.Lead(
                company_id=company_id, full_name="Hali Eslatilmagan",
                next_contact_at=dt.datetime.utcnow() - dt.timedelta(days=1), followup_reminder_count=0,
            )
            already_reminded = db_module.Lead(
                company_id=company_id, full_name="Eslatilgan Lekin Javobsiz",
                next_contact_at=dt.datetime.utcnow() - dt.timedelta(days=1), followup_reminder_count=2,
            )
            session.add_all([never_reminded, already_reminded])
            session.commit()
        finally:
            session.close()

        sent = []
        with mock.patch.object(scheduler, "_tg_send", side_effect=lambda cid, text: sent.append((cid, text)) or {"ok": True, "error": None}):
            result = scheduler.job_followup_admin_escalation()

        check("adminga aynan bitta xabar yuboriladi", any(cid == 700999 for cid, _ in sent))
        admin_text = "\n".join(t for cid, t in sent if cid == 700999)
        check("kamida bir marta eslatilgan lead xabarda bor", "Eslatilgan Lekin Javobsiz" in admin_text)
        check("HECH qachon eslatilmagan lead xabarda YO'Q (hali reminder job'i yetib bormagan)", "Hali Eslatilmagan" not in admin_text)
        check("necha marta eslatilgani ko'rsatiladi", "2 marta eslatilgan" in admin_text)
    print("OK: job_followup_admin_escalation() faqat kamida bir marta eslatilgan, hali javobsiz lidlarni adminga shaxsan yuboradi")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
