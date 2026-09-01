"""test_multitenant_sync_offline.py — 2026-09, foydalanuvchi so'rovi:
"boshqa kompaniyalar ham bitta tugma bilan Facebook/Instagram'ga ulansin,
ularniki HAM ishlasin". Bu fayl `smm_sync.py`/`ig_dm_sync.py`ning YANGI
`sync_all_companies()` funksiyalarini va `scheduler.job_ig_dm_sync()`ning
har bir kompaniyaning O'Z Telegram guruhiga ogohlantirish yuborishini
tekshiradi (2026-08'dagi yagona-global-akkaunt bug'ining davomi -- endi
sync YOZISH tomoni ham to'g'ri ko'p-tenant bo'lishi kerak, aks holda
boshqa kompaniyaning ulagan hisobi baribir platforma egasining
`company_id`siga yozilib qolardi).

Tekshiriladigan aniq xatti-harakatlar:
  1. `smm_sync.sync_all_companies()` -- Meta hisobi ulagan HAR BIR
     kompaniyani O'Z tokeni/page_id'si bilan sinxronlaydi, natijalar
     TO'G'RI `company_id` bilan bazaga yoziladi (bir-biriga aralashmaydi).
  2. `ig_dm_sync.sync_all_companies()` -- xuddi shunday, Instagram DM
     suhbatlari uchun.
  3. `scheduler.job_ig_dm_sync()` -- javobsiz-suhbat ogohlantirishini
     FAQAT o'sha kompaniyaning `Company.telegram_group_id`siga yuboradi;
     guruh sozlanmagan kompaniya uchun HECH QAYERGA (platforma egasining
     umumiy guruhiga ham EMAS) yubormaydi.

Ishga tushirish:
    cd app && python3 scripts/test_multitenant_sync_offline.py
"""

import os
import sys
import tempfile
import datetime as dt
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")


def _fresh_modules(db_path):
    """Har bir test o'z ALOHIDA SQLite fayli va tegishli modullarning
    TOZA nusxasi bilan ishlaydi (`test_ig_dm_offline.py`dagi bilan bir
    xil naqsh -- bu modullar `db`dan sinf/funksiyalarni IMPORT VAQTIDA
    olib qo'yadi)."""
    for name in ("db", "kv_store", "smm_sync", "ig_dm_sync", "scheduler"):
        sys.modules.pop(name, None)
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ.pop("META_ACCESS_TOKEN", None)
    os.environ.pop("META_PAGE_ID", None)
    os.environ.pop("TELEGRAM_AGENTS_GROUP_ID", None)
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    import db as db_module
    db_module.init_db()
    import smm_sync
    import ig_dm_sync
    return db_module, smm_sync, ig_dm_sync


def _make_company(db_module, *, name, meta_page_id, meta_access_token, telegram_group_id=None):
    session = db_module.get_session()
    try:
        c = db_module.Company(
            name=name, meta_page_id=meta_page_id, meta_access_token=meta_access_token,
            telegram_group_id=telegram_group_id, is_active=True,
        )
        session.add(c)
        session.commit()
        return c.id
    finally:
        session.close()


def test_smm_sync_all_companies_writes_correct_company_id_for_each():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, smm_sync, _ = _fresh_modules(os.path.join(tmp, "s1.db"))
        # Company #1 (default) has no Meta creds -- odatdagidek "sozlanmagan" holat.
        company_a = _make_company(db_module, name="Kompaniya A", meta_page_id="page_a", meta_access_token="tok_a")
        company_b = _make_company(db_module, name="Kompaniya B", meta_page_id="page_b", meta_access_token="tok_b")

        import meta_api

        def fake_get_facebook_page_profile(*, page_id=None, access_token=None):
            fan_counts = {"tok_a": 1000, "tok_b": 5000}
            return {"fan_count": fan_counts.get(access_token, 0)}

        def fake_get_facebook_page_posts(*, limit=25, page_id=None, access_token=None):
            return []

        def fake_get_instagram_business_account_id(*, page_id=None, access_token=None):
            return None  # Instagram ulanmagan -- faqat Facebook qismini tekshiramiz

        with mock.patch.object(meta_api, "get_facebook_page_profile", side_effect=fake_get_facebook_page_profile), \
             mock.patch.object(meta_api, "get_facebook_page_posts", side_effect=fake_get_facebook_page_posts), \
             mock.patch.object(meta_api, "get_instagram_business_account_id", side_effect=fake_get_instagram_business_account_id):
            overall = smm_sync.sync_all_companies()

        assert overall["companies_synced"] == 2, f"faqat Meta ulagan 2 ta kompaniya sinxronlanishi kerak edi: {overall}"
        assert overall["per_company"][company_a]["facebook"]["followers_count"] == 1000
        assert overall["per_company"][company_b]["facebook"]["followers_count"] == 5000

        session = db_module.get_session()
        with db_module.unscoped():
            snap_a = session.query(db_module.SmmSnapshot).filter_by(company_id=company_a, platform="facebook").first()
            snap_b = session.query(db_module.SmmSnapshot).filter_by(company_id=company_b, platform="facebook").first()
        session.close()
        assert snap_a.followers_count == 1000, "Kompaniya A'ning yozuvi O'Z followers_count'i bilan saqlanishi kerak"
        assert snap_b.followers_count == 5000, "Kompaniya B'ning yozuvi O'Z followers_count'i bilan saqlanishi kerak, A bilan aralashmasligi kerak"

        assert smm_sync.get_last_status(company_a)["facebook"]["followers_count"] == 1000
        assert smm_sync.get_last_status(company_b)["facebook"]["followers_count"] == 5000
    print("OK: smm_sync.sync_all_companies() har bir kompaniyani O'Z Meta hisobi bilan, TO'G'RI company_id'ga yozib sinxronlaydi")


def test_ig_dm_sync_all_companies_isolates_conversations_per_company():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, _, ig_dm_sync = _fresh_modules(os.path.join(tmp, "s2.db"))
        company_a = _make_company(db_module, name="Kompaniya A", meta_page_id="page_a", meta_access_token="tok_a")
        company_b = _make_company(db_module, name="Kompaniya B", meta_page_id="page_b", meta_access_token="tok_b")

        import meta_api

        def fake_ig_business_id(*, page_id=None, access_token=None):
            return {"page_a": "IG_A", "page_b": "IG_B"}.get(page_id)

        def fake_conversations(*, limit=50, page_id=None, access_token=None):
            conv_id = f"conv_{page_id}"
            return [{"id": conv_id, "updated_time": dt.datetime.utcnow().isoformat(), "participants": {"data": [
                {"id": {"page_a": "IG_A", "page_b": "IG_B"}.get(page_id)},
                {"id": f"CUST_{page_id}", "username": f"mijoz_{page_id}"},
            ]}}]

        def fake_messages(conversation_id, limit=40, page_id=None, access_token=None):
            return [{
                "id": f"{conversation_id}-m1", "message": "Salom",
                "created_time": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S+0000"),
                "from": {"id": f"CUST_{page_id}"},
            }]

        with mock.patch.object(meta_api, "get_instagram_business_account_id", side_effect=fake_ig_business_id), \
             mock.patch.object(meta_api, "get_instagram_conversations", side_effect=fake_conversations), \
             mock.patch.object(meta_api, "get_instagram_conversation_messages", side_effect=fake_messages):
            overall = ig_dm_sync.sync_all_companies()

        assert overall["companies_synced"] == 2
        assert overall["per_company"][company_a]["conversations_checked"] == 1
        assert overall["per_company"][company_b]["conversations_checked"] == 1

        session = db_module.get_session()
        with db_module.unscoped():
            conv_a = session.query(db_module.IgDmConversation).filter_by(external_id="conv_page_a").first()
            conv_b = session.query(db_module.IgDmConversation).filter_by(external_id="conv_page_b").first()
        session.close()
        assert conv_a.company_id == company_a
        assert conv_b.company_id == company_b
        assert conv_a.customer_username == "mijoz_page_a"
        assert conv_b.customer_username == "mijoz_page_b"
    print("OK: ig_dm_sync.sync_all_companies() har bir kompaniyaning Instagram DM suhbatlarini O'Z company_id'si bilan, aralashmasdan saqlaydi")


def test_scheduler_routes_overdue_alert_only_to_companys_own_telegram_group():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, _, ig_dm_sync = _fresh_modules(os.path.join(tmp, "s3.db"))
        # Company A has its own Telegram group -- should receive its alert there.
        company_a = _make_company(db_module, name="Kompaniya A", meta_page_id="page_a", meta_access_token="tok_a", telegram_group_id="-1001111")
        # Company B has NOT configured a Telegram group -- must NOT get routed anywhere (not even the owner's global group).
        company_b = _make_company(db_module, name="Kompaniya B", meta_page_id="page_b", meta_access_token="tok_b", telegram_group_id=None)
        os.environ["TELEGRAM_AGENTS_GROUP_ID"] = "-1009999"  # platforma egasining UMUMIY guruhi -- bu yerga SIZIB CHIQMASLIGI kerak
        os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"

        sys.modules.pop("scheduler", None)
        import scheduler

        now = dt.datetime.utcnow()
        overdue_a = [{"conversation_id": 1, "customer": "mijoz_a", "preview": "Salom", "since_minutes": 45}]
        overdue_b = [{"conversation_id": 2, "customer": "mijoz_b", "preview": "Narxi?", "since_minutes": 50}]
        fake_overall = {
            "companies_synced": 2,
            "per_company": {
                company_a: {"configured": True, "conversations_checked": 1, "new_messages": 1, "overdue": overdue_a, "errors": []},
                company_b: {"configured": True, "conversations_checked": 1, "new_messages": 1, "overdue": overdue_b, "errors": []},
            },
        }

        sent_messages = []

        def fake_tg_send(chat_id, text):
            sent_messages.append((chat_id, text))
            return {"ok": True, "error": None}

        marked_sent = []

        with mock.patch.object(scheduler.ig_dm_sync, "sync_all_companies", return_value=fake_overall), \
             mock.patch.object(scheduler, "_tg_send", side_effect=fake_tg_send), \
             mock.patch.object(scheduler.ig_dm_sync, "mark_alert_sent", side_effect=lambda cid: marked_sent.append(cid)):
            result = scheduler.job_ig_dm_sync()

        assert result == fake_overall
        assert len(sent_messages) == 1, f"faqat Kompaniya A'ga (o'z guruhi sozlangan) xabar yuborilishi kerak edi: {sent_messages}"
        chat_id, text = sent_messages[0]
        assert chat_id == -1001111, "Kompaniya A'ning xabari O'Z telegram_group_id'iga borishi kerak"
        assert "mijoz_a" in text
        assert "mijoz_b" not in text, "Kompaniya B'ning mijozi haqidagi ma'lumot Kompaniya A'ning guruhiga sizib chiqmasligi kerak"
        assert -1009999 not in [c for c, _ in sent_messages], "platforma egasining umumiy guruhiga HECH NARSA yuborilmasligi kerak"
        assert marked_sent == [1], "faqat muvaffaqiyatli yuborilgan Kompaniya A'ning ogohlantirishi belgilanishi kerak"
    print("OK: scheduler.job_ig_dm_sync() har bir kompaniyaning ogohlantirishini FAQAT o'zining Telegram guruhiga yuboradi, guruhi yo'q kompaniya uchun hech qayerga (platforma egasiga ham) yubormaydi")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
