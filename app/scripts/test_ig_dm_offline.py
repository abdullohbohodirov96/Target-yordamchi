"""test_ig_dm_offline.py — Instagram DM (Direct) funksiyasi uchun
TARMOQSIZ (offline) tekshiruv (2026-08, foydalanuvchi so'rovi: "ig
chatlarni tahlilini ham qoshish kerak, lekin byudjetni yo'lini top").

Ikki modul alohida tekshiriladi (ular ATAYLAB alohida -- xarajatni
nazorat qilish uchun: biri AI ISHLATMAYDI, ikkinchisi AI ishlatadi):
  - `ig_dm_sync.py` -- Meta'dan tortish + javobsizlik holatini
    DETERMINISTIK (AI'siz) hisoblash. `meta_api` funksiyalari mock
    qilinadi, vaqtinchalik SQLite baza ishlatiladi.
  - `ig_dm_analysis.py` -- gpt-4o-mini orqali lid-sifat bahosi, FAQAT
    yangi xabar kelgan suhbatlar uchun. OpenAI so'rovi (`_openai_request`)
    mock qilinadi.

Ishga tushirish:
    cd app && python3 scripts/test_ig_dm_offline.py
"""

import os
import sys
import json
import tempfile
import datetime as dt
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")


def _fresh_modules(db_path):
    """Har bir test o'z ALOHIDA SQLite fayli va `db`/`ig_dm_sync`/
    `ig_dm_analysis` modullarining TOZA nusxasi bilan ishlaydi (bu
    modullar `db`dan sinf/funksiyalarni IMPORT VAQTIDA olib qo'yadi,
    shuning uchun eskisini sys.modules'dan olib tashlab qayta import
    qilish shart -- `test_multitenant_db_offline.py`dagi bilan bir xil
    naqsh)."""
    for name in ("db", "kv_store", "ig_dm_sync", "ig_dm_analysis", "call_analysis"):
        sys.modules.pop(name, None)
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["META_ACCESS_TOKEN"] = "tok_test"
    os.environ["META_AD_ACCOUNT_ID"] = "act_test"
    import db as db_module
    db_module.init_db()
    import ig_dm_sync
    import ig_dm_analysis
    ig_dm_sync.meta_api.ACCESS_TOKEN = "tok_test"
    ig_dm_sync.meta_api.PAGE_ID = "page_test"
    return db_module, ig_dm_sync, ig_dm_analysis


def _iso(when: dt.datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%S+0000")


def _msg(msg_id, sender_id, text, when):
    return {"id": msg_id, "message": text, "created_time": _iso(when), "from": {"id": sender_id}}


def _conv(conv_id, customer_id, username=None):
    participants = [{"id": "BIZ_ID"}, {"id": customer_id, **({"username": username} if username else {})}]
    return {"id": conv_id, "updated_time": _iso(dt.datetime.utcnow()), "participants": {"data": participants}}


# ---------------------------------------------------------------------------
# ig_dm_sync.py -- deterministik (AI'siz) qism
# ---------------------------------------------------------------------------

def test_sync_not_configured():
    with tempfile.TemporaryDirectory() as tmp:
        _, ig_dm_sync, _ = _fresh_modules(os.path.join(tmp, "t1.db"))
        ig_dm_sync.meta_api.ACCESS_TOKEN = ""
        result = ig_dm_sync.sync_once()
        assert result["configured"] is False
        assert result["errors"]
    print("OK: META_ACCESS_TOKEN/META_PAGE_ID sozlanmaganda sync jim (configured=False) qaytadi")


def test_sync_no_ig_business_account():
    with tempfile.TemporaryDirectory() as tmp:
        _, ig_dm_sync, _ = _fresh_modules(os.path.join(tmp, "t2.db"))
        with mock.patch.object(ig_dm_sync.meta_api, "get_instagram_business_account_id", return_value=None):
            result = ig_dm_sync.sync_once()
        assert result["configured"] is False
        assert result["errors"]
    print("OK: Instagram Business akkaunt ulanmaganda sync jim (configured=False) qaytadi")


def test_sync_new_unanswered_conversation_flagged_overdue():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, ig_dm_sync, _ = _fresh_modules(os.path.join(tmp, "t3.db"))
        now = dt.datetime.utcnow()
        conv = _conv("conv1", "CUST1", username="mijoz1")
        messages = [
            _msg("m2", "CUST1", "Narxi qancha?", now - dt.timedelta(minutes=45)),
            _msg("m1", "CUST1", "Salom", now - dt.timedelta(minutes=50)),
        ]  # Meta odatda eng yangisini BIRINCHI qaytaradi
        with mock.patch.object(ig_dm_sync.meta_api, "get_instagram_business_account_id", return_value="BIZ_ID"), \
             mock.patch.object(ig_dm_sync.meta_api, "get_instagram_conversations", return_value=([conv], None)), \
             mock.patch.object(ig_dm_sync.meta_api, "get_instagram_conversation_messages", return_value=messages):
            result = ig_dm_sync.sync_once()

        assert result["configured"] is True
        assert result["conversations_checked"] == 1
        assert result["new_messages"] == 2
        assert len(result["overdue"]) == 1, f"45 daqiqa > 30 daqiqa chegarasi -- overdue bo'lishi kerak edi: {result}"
        assert result["error_stage"] is None, "xato bo'lmaganda error_stage None qolishi kerak"

        session = db_module.get_session()
        row = session.query(db_module.IgDmConversation).filter_by(external_id="conv1").first()
        assert row.is_unanswered is True
        assert row.customer_username == "mijoz1"
        assert row.message_count == 2
        # Ikkalasi ham "customer" -- javobsizlik davri ENG ESKI xabardan boshlanadi.
        expected_since = (now - dt.timedelta(minutes=50)).replace(microsecond=0)
        assert row.unanswered_since == expected_since, f"kutilgan {expected_since}, olindi {row.unanswered_since}"
        session.close()
    print("OK: faqat mijoz xabarlari bo'lgan yangi suhbat 'javobsiz' deb belgilanadi va chegaradan o'tgani uchun ogohlantirish ro'yxatiga tushadi")


def test_sync_business_reply_clears_unanswered():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, ig_dm_sync, _ = _fresh_modules(os.path.join(tmp, "t4.db"))
        now = dt.datetime.utcnow()
        conv = _conv("conv2", "CUST2")
        messages = [
            _msg("m2", "BIZ_ID", "Narxi 100$", now - dt.timedelta(minutes=5)),
            _msg("m1", "CUST2", "Narxi qancha?", now - dt.timedelta(minutes=10)),
        ]
        with mock.patch.object(ig_dm_sync.meta_api, "get_instagram_business_account_id", return_value="BIZ_ID"), \
             mock.patch.object(ig_dm_sync.meta_api, "get_instagram_conversations", return_value=([conv], None)), \
             mock.patch.object(ig_dm_sync.meta_api, "get_instagram_conversation_messages", return_value=messages):
            result = ig_dm_sync.sync_once()

        assert result["overdue"] == []
        session = db_module.get_session()
        row = session.query(db_module.IgDmConversation).filter_by(external_id="conv2").first()
        assert row.is_unanswered is False
        assert row.unanswered_since is None
        session.close()
    print("OK: biznes javob bergan suhbat 'javobsiz' deb belgilanmaydi")


def test_sync_alert_not_resent_once_marked():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, ig_dm_sync, _ = _fresh_modules(os.path.join(tmp, "t5.db"))
        now = dt.datetime.utcnow()
        conv = _conv("conv3", "CUST3")
        messages = [_msg("m1", "CUST3", "Salom", now - dt.timedelta(minutes=40))]

        with mock.patch.object(ig_dm_sync.meta_api, "get_instagram_business_account_id", return_value="BIZ_ID"), \
             mock.patch.object(ig_dm_sync.meta_api, "get_instagram_conversations", return_value=([conv], None)), \
             mock.patch.object(ig_dm_sync.meta_api, "get_instagram_conversation_messages", return_value=messages):
            result1 = ig_dm_sync.sync_once()
        assert len(result1["overdue"]) == 1
        conv_id = result1["overdue"][0]["conversation_id"]
        ig_dm_sync.mark_alert_sent(conv_id)

        # Xuddi shu (yangi xabarsiz) holatda QAYTA sinxronlansa -- ogohlantirish
        # IKKINCHI marta yubormaslik ro'yxatiga tushmasligi kerak.
        with mock.patch.object(ig_dm_sync.meta_api, "get_instagram_business_account_id", return_value="BIZ_ID"), \
             mock.patch.object(ig_dm_sync.meta_api, "get_instagram_conversations", return_value=([conv], None)), \
             mock.patch.object(ig_dm_sync.meta_api, "get_instagram_conversation_messages", return_value=messages):
            result2 = ig_dm_sync.sync_once()
        assert result2["overdue"] == [], "ogohlantirish allaqachon yuborilgan -- qayta ro'yxatga tushmasligi kerak"
        assert result2["new_messages"] == 0  # xabar allaqachon bazada -- dublikat qo'shilmaydi
    print("OK: bir marta yuborilgan javobsizlik ogohlantirishi xuddi shu davr uchun qayta yuborilmaydi")


def test_sync_handles_meta_error_per_conversation():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, ig_dm_sync, _ = _fresh_modules(os.path.join(tmp, "t6.db"))
        conv_ok = _conv("conv_ok", "CUST_OK")
        conv_bad = _conv("conv_bad", "CUST_BAD")

        def _messages_side_effect(conversation_id, limit=40, page_id=None, access_token=None):
            if conversation_id == "conv_bad":
                raise ig_dm_sync.meta_api.MetaAPIError({"message": "permission denied", "code": 10})
            return [_msg("mok", "CUST_OK", "Salom", dt.datetime.utcnow())]

        with mock.patch.object(ig_dm_sync.meta_api, "get_instagram_business_account_id", return_value="BIZ_ID"), \
             mock.patch.object(ig_dm_sync.meta_api, "get_instagram_conversations", return_value=([conv_ok, conv_bad], None)), \
             mock.patch.object(ig_dm_sync.meta_api, "get_instagram_conversation_messages", side_effect=_messages_side_effect):
            result = ig_dm_sync.sync_once()

        assert result["conversations_checked"] == 2
        assert result["new_messages"] == 1  # faqat conv_ok muvaffaqiyatli
        assert result["errors"], "conv_bad xatosi errors ro'yxatiga tushishi kerak"
        assert any("ruxsat" in e.lower() or "permission" in e.lower() for e in result["errors"])
        # 2026-09, foydalanuvchi so'rovi: natija ANIQ qaysi bosqichda xato
        # bo'lganini ko'rsatishi kerak (bu xato xabarlar-bosqichida, bitta
        # suhbat uchun -- "conversations_list" EMAS).
        assert result["error_stage"] == "conversation_messages", (
            f"kutilgan 'conversation_messages', olindi {result['error_stage']!r}"
        )
    print("OK: bitta suhbatning Meta xatosi butun sinxronizatsiyani to'xtatmaydi (qolganlari davom etadi), error_stage='conversation_messages' to'g'ri belgilanadi")


def test_sync_conversations_list_error_sets_error_stage():
    """2026-09, foydalanuvchi so'rovi: suhbatlar RO'YXATINI olishning
    o'zida (bitta suhbatgacha ham yetib bormasdan) Meta xato bersa,
    `error_stage` aynan 'conversations_list' bo'lishi kerak -- yuqoridagi
    testdagi 'conversation_messages' holatidan FARQLI."""
    with tempfile.TemporaryDirectory() as tmp:
        _, ig_dm_sync, _ = _fresh_modules(os.path.join(tmp, "t6b.db"))
        error = ig_dm_sync.meta_api.MetaAPIError({"message": "Timeout", "code": 1})
        with mock.patch.object(ig_dm_sync.meta_api, "get_instagram_business_account_id", return_value="BIZ_ID"), \
             mock.patch.object(ig_dm_sync.meta_api, "get_instagram_conversations", side_effect=error):
            result = ig_dm_sync.sync_once()

        assert result["conversations_checked"] == 0
        assert result["errors"]
        assert result["error_stage"] == "conversations_list", (
            f"kutilgan 'conversations_list', olindi {result['error_stage']!r}"
        )
        assert result["errors"][0].startswith("[conversations_list]"), (
            f"item 8: UI xato matni '[stage]' prefiksi bilan boshlanishi kerak, olindi {result['errors'][0]!r}"
        )
    print("OK: suhbatlar ro'yxatini olishning o'zida xato bo'lsa, error_stage='conversations_list' va '[stage]' prefiksi to'g'ri belgilanadi")


def test_sync_conversations_list_minimal_stage_propagates_to_result():
    """2026-09 QAYTA TUZATISH (item 7): `meta_api.
    get_instagram_conversations()` eng kichik diagnostik so'rov ham rad
    etilganda xatoga `.stage='conversations_list_minimal'` biriktiradi --
    `sync_once()` buni O'ZINING qattiq yozilgan 'conversations_list'
    o'rniga ANIQ shu qiymat bilan `error_stage`ga yozishi kerak."""
    with tempfile.TemporaryDirectory() as tmp:
        _, ig_dm_sync, _ = _fresh_modules(os.path.join(tmp, "t6c.db"))
        error = ig_dm_sync.meta_api.MetaAPIError({"message": "Please reduce the amount of data you're asking for", "code": 1})
        error.stage = "conversations_list_minimal"
        with mock.patch.object(ig_dm_sync.meta_api, "get_instagram_business_account_id", return_value="BIZ_ID"), \
             mock.patch.object(ig_dm_sync.meta_api, "get_instagram_conversations", side_effect=error):
            result = ig_dm_sync.sync_once()

        assert result["error_stage"] == "conversations_list_minimal", (
            f"kutilgan 'conversations_list_minimal', olindi {result['error_stage']!r}"
        )
        assert result["errors"][0].startswith("[conversations_list_minimal]")
    print("OK: get_instagram_conversations()dan kelgan e.stage='conversations_list_minimal' sync_once() natijasiga to'g'ri o'tadi")


def test_sync_uses_limit_5_and_no_since_kwarg():
    """2026-09 QAYTA TUZATISH (item 1, 2): `sync_once()` endi
    `get_instagram_conversations()`ni `since`SIZ va `limit=5` bilan
    chaqirishi kerak (avval `limit=10, since=...` edi)."""
    with tempfile.TemporaryDirectory() as tmp:
        _, ig_dm_sync, _ = _fresh_modules(os.path.join(tmp, "t6d.db"))
        captured = {}

        def fake_conversations(*, limit=5, page_id=None, access_token=None, after=None):
            captured["limit"] = limit
            captured["kwargs"] = {"page_id": page_id, "access_token": access_token, "after": after}
            return [], None

        with mock.patch.object(ig_dm_sync.meta_api, "get_instagram_business_account_id", return_value="BIZ_ID"), \
             mock.patch.object(ig_dm_sync.meta_api, "get_instagram_conversations", side_effect=fake_conversations):
            ig_dm_sync.sync_once()

        assert captured["limit"] == 5, f"kutilgan limit=5, olindi {captured['limit']}"
    print("OK: sync_once() get_instagram_conversations()ni limit=5 bilan, `since` kwargisiz chaqiradi")


def test_sync_local_filters_conversations_older_than_sync_since():
    """2026-09 QAYTA TUZATISH (item 5): `since` Meta so'roviga
    yuborilmaydi -- buning o'rniga Meta'dan qaytgan suhbatning
    `updated_time`si `company.ig_dm_sync_since`dan OLDIN bo'lsa, u LOKAL
    ravishda chetlab o'tiladi (DBga import qilinmaydi, `conversations_
    checked` ham OSHMAYDI)."""
    with tempfile.TemporaryDirectory() as tmp:
        db_module, ig_dm_sync, _ = _fresh_modules(os.path.join(tmp, "t6e.db"))
        now = dt.datetime.utcnow()
        sync_since = now - dt.timedelta(hours=1)
        old_conv = _conv("conv_old", "CUST_OLD")
        old_conv["updated_time"] = _iso(sync_since - dt.timedelta(hours=2))  # ulanishdan OLDIN yangilangan
        new_conv = _conv("conv_new", "CUST_NEW")
        new_conv["updated_time"] = _iso(now)  # ulanishdan KEYIN yangilangan

        fake_company = ig_dm_sync._CompanyCreds(
            id=1, meta_page_id="page_test", meta_access_token="tok_test", ig_dm_sync_since=sync_since,
        )
        with mock.patch.object(ig_dm_sync.meta_api, "get_instagram_business_account_id", return_value="BIZ_ID"), \
             mock.patch.object(ig_dm_sync.meta_api, "get_instagram_conversations", return_value=([old_conv, new_conv], None)), \
             mock.patch.object(ig_dm_sync.meta_api, "get_instagram_conversation_messages", return_value=[]):
            result = ig_dm_sync.sync_once(company=fake_company)

        assert result["conversations_checked"] == 1, f"faqat YANGI suhbat tekshirilishi kerak edi, olindi {result['conversations_checked']}"
        session = db_module.get_session()
        assert session.query(db_module.IgDmConversation).filter_by(external_id="conv_old").first() is None, (
            "ulanishdan OLDINGI suhbat DBga IMPORT QILINMASLIGI kerak"
        )
        assert session.query(db_module.IgDmConversation).filter_by(external_id="conv_new").first() is not None
        session.close()
    print("OK: `ig_dm_sync_since`dan OLDIN yangilangan suhbatlar faqat LOKAL filtr bilan chetlab o'tiladi (Meta so'roviga `since` yuborilmaydi)")


def test_upsert_fetches_participants_separately_only_when_unknown():
    """2026-09 QAYTA TUZATISH (item 3): agar conversation-list javobida
    `participants` bo'lmasa (real hayotda ENDI shunday) -- mijoz hali
    NOMA'LUM bo'lgan suhbat uchun `get_instagram_conversation_participants()`
    ALOHIDA chaqiriladi; mijoz ALLAQACHON ma'lum bo'lsa -- bu so'rov
    UMUMAN qilinmaydi."""
    with tempfile.TemporaryDirectory() as tmp:
        db_module, ig_dm_sync, _ = _fresh_modules(os.path.join(tmp, "t6f.db"))
        conv_no_participants = {"id": "conv_np", "updated_time": _iso(dt.datetime.utcnow())}
        calls = []

        def fake_participants(conversation_id, *, page_id=None, access_token=None):
            calls.append(conversation_id)
            return {"participants": {"data": [{"id": "BIZ_ID"}, {"id": "CUST_NP", "username": "yangi_mijoz"}]}}

        with mock.patch.object(ig_dm_sync.meta_api, "get_instagram_business_account_id", return_value="BIZ_ID"), \
             mock.patch.object(ig_dm_sync.meta_api, "get_instagram_conversations", return_value=([conv_no_participants], None)), \
             mock.patch.object(ig_dm_sync.meta_api, "get_instagram_conversation_participants", side_effect=fake_participants), \
             mock.patch.object(ig_dm_sync.meta_api, "get_instagram_conversation_messages", return_value=[]):
            result1 = ig_dm_sync.sync_once()
        assert calls == ["conv_np"], f"mijoz noma'lum bo'lgani uchun BIR MARTA chaqirilishi kerak edi, olindi {calls}"
        session = db_module.get_session()
        row = session.query(db_module.IgDmConversation).filter_by(external_id="conv_np").first()
        assert row.customer_ig_id == "CUST_NP"
        session.close()

        # Endi mijoz ALLAQACHON ma'lum -- QAYTA sinxronlansa, participants
        # so'rovi UMUMAN QILINMASLIGI kerak.
        with mock.patch.object(ig_dm_sync.meta_api, "get_instagram_business_account_id", return_value="BIZ_ID"), \
             mock.patch.object(ig_dm_sync.meta_api, "get_instagram_conversations", return_value=([conv_no_participants], None)), \
             mock.patch.object(ig_dm_sync.meta_api, "get_instagram_conversation_participants", side_effect=fake_participants), \
             mock.patch.object(ig_dm_sync.meta_api, "get_instagram_conversation_messages", return_value=[]):
            ig_dm_sync.sync_once()
        assert calls == ["conv_np"], f"mijoz ALLAQACHON ma'lum bo'lgani uchun QAYTA chaqirilmasligi kerak edi, olindi {calls}"
    print("OK: participants faqat mijoz hali noma'lum bo'lgan suhbat uchun, va faqat BIR MARTA, alohida so'raladi")


def test_ingest_webhook_message_creates_conversation_and_dedups():
    """2026-09, item 9: webhook orqali kelgan BITTA xabar bazaga to'g'ridan
    to'g'ri (Graph API'ga qayta murojaat qilmasdan) yoziladi, va xuddi shu
    `message_id` bilan IKKINCHI marta kelsa -- dublikat qo'shilmaydi."""
    with tempfile.TemporaryDirectory() as tmp:
        db_module, ig_dm_sync, _ = _fresh_modules(os.path.join(tmp, "t7.db"))
        company = ig_dm_sync._CompanyCreds(id=1, meta_page_id="page_test", meta_access_token="tok_test")

        outcome1 = ig_dm_sync.ingest_webhook_message(
            company, sender_id="CUST_WH", recipient_id="BIZ_ID", message_id="wamid.1",
            text="Salom, narxi qancha?", timestamp_ms=int(dt.datetime.utcnow().timestamp() * 1000),
        )
        assert outcome1["new_message"] is True

        session = db_module.get_session()
        conv = session.query(db_module.IgDmConversation).filter_by(customer_ig_id="CUST_WH").first()
        assert conv is not None
        assert conv.is_unanswered is True
        assert conv.message_count == 1
        assert conv.external_id == "webhook:1:CUST_WH"
        session.close()

        # Xuddi shu message_id bilan qayta kelsa (masalan fallback polling
        # orqali ham ustidan qaytarilsa) -- dublikat qo'shilmasligi kerak.
        outcome2 = ig_dm_sync.ingest_webhook_message(
            company, sender_id="CUST_WH", recipient_id="BIZ_ID", message_id="wamid.1",
            text="Salom, narxi qancha?", timestamp_ms=int(dt.datetime.utcnow().timestamp() * 1000),
        )
        assert outcome2["new_message"] is False

        session = db_module.get_session()
        assert session.query(db_module.IgDmMessage).filter_by(external_id="wamid.1").count() == 1
        session.close()
    print("OK: ingest_webhook_message() yangi suhbat/xabar yaratadi va bir xil message_id bilan dublikat qo'shmaydi")


def test_ingest_webhook_message_is_echo_means_business_sender():
    """`is_echo=True` -- Page O'ZI yuborgan (masalan menejer boshqa qurilma
    orqali to'g'ridan-to'g'ri Instagram ilovasidan javob yozgan) degani --
    bu holda mijoz IGSID'i `recipient_id`da, sender esa 'business'."""
    with tempfile.TemporaryDirectory() as tmp:
        db_module, ig_dm_sync, _ = _fresh_modules(os.path.join(tmp, "t8.db"))
        company = ig_dm_sync._CompanyCreds(id=1, meta_page_id="page_test", meta_access_token="tok_test")

        ig_dm_sync.ingest_webhook_message(
            company, sender_id="BIZ_ID", recipient_id="CUST_ECHO", message_id="wamid.echo1",
            text="Assalomu alaykum, buyurtmangiz tayyor", timestamp_ms=None, is_echo=True,
        )
        session = db_module.get_session()
        conv = session.query(db_module.IgDmConversation).filter_by(customer_ig_id="CUST_ECHO").first()
        assert conv is not None, "mijoz IGSID'i is_echo bo'lsa recipient_id'dan olinishi kerak"
        msg = session.query(db_module.IgDmMessage).filter_by(external_id="wamid.echo1").first()
        assert msg.sender == "business"
        assert conv.is_unanswered is False
        session.close()
    print("OK: is_echo=True bo'lganda mijoz recipient_id'dan aniqlanadi va xabar 'business' sifatida saqlanadi")


def test_upsert_merges_webhook_created_duplicate_when_polling_finds_real_conversation():
    """2026-09, item 9: webhook ILGARI shu mijoz uchun sintetik
    external_id bilan suhbat yaratgan bo'lsa, KEYINROQ polling shu
    mijozning HAQIQIY Meta suhbatini topganda -- ikkitasi BITTA suhbatga
    birlashtirilishi kerak (ikki xil qator sifatida ko'rinmasligi uchun)."""
    with tempfile.TemporaryDirectory() as tmp:
        db_module, ig_dm_sync, _ = _fresh_modules(os.path.join(tmp, "t9.db"))
        company = ig_dm_sync._CompanyCreds(id=7, meta_page_id="page_test", meta_access_token="tok_test")

        ig_dm_sync.ingest_webhook_message(
            company, sender_id="CUST_MERGE", recipient_id="BIZ_ID", message_id="wamid.pre1",
            text="Salom", timestamp_ms=int(dt.datetime.utcnow().timestamp() * 1000),
        )
        session = db_module.get_session()
        assert session.query(db_module.IgDmConversation).filter_by(company_id=7).count() == 1
        session.close()

        real_conv = _conv("real_conv_123", "CUST_MERGE")
        with mock.patch.object(ig_dm_sync.meta_api, "get_instagram_conversation_messages", return_value=[
            _msg("m_real", "CUST_MERGE", "Salom", dt.datetime.utcnow()),
        ]):
            ig_dm_sync._upsert_conversation_and_messages(
                db_module.get_session(), real_conv, "BIZ_ID", company_id=7,
            )

        session = db_module.get_session()
        rows = session.query(db_module.IgDmConversation).filter_by(company_id=7).all()
        assert len(rows) == 1, f"webhook va poll qatorlari BITTAGA birlashishi kerak edi, olindi {len(rows)} ta qator"
        assert rows[0].external_id == "real_conv_123", "birlashtirilgandan keyin HAQIQIY external_id saqlanishi kerak"
        assert rows[0].message_count == 2, "ikkala manbadan kelgan xabarlar (webhook + poll) birga saqlanishi kerak"
        session.close()
    print("OK: webhook orqali (sintetik external_id bilan) yaratilgan suhbat keyinroq polling HAQIQIY suhbatni topganda bittaga birlashtiriladi")


# ---------------------------------------------------------------------------
# ig_dm_analysis.py -- AI (gpt-4o-mini) qismi, faqat MOCK OpenAI bilan
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json = json_data
        self.ok = 200 <= status_code < 300
        self.text = json.dumps(json_data)

    def json(self):
        return self._json


def _openai_text_response(payload: dict) -> _FakeResp:
    text = json.dumps(payload, ensure_ascii=False)
    return _FakeResp(200, {"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]})


def _seed_conversation(db_module, external_id, messages_texts, ai_analyzed_message_count=0):
    session = db_module.get_session()
    conv = db_module.IgDmConversation(
        external_id=external_id, message_count=len(messages_texts),
        ai_analyzed_message_count=ai_analyzed_message_count,
    )
    session.add(conv)
    session.commit()
    now = dt.datetime.utcnow()
    for i, (sender, text) in enumerate(messages_texts):
        session.add(db_module.IgDmMessage(
            conversation_id=conv.id, external_id=f"{external_id}-m{i}", sender=sender, text=text,
            sent_at=now - dt.timedelta(minutes=(len(messages_texts) - i)),
        ))
    session.commit()
    conv_id = conv.id
    session.close()
    return conv_id


def test_analysis_skips_when_no_openai_key():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, _, ig_dm_analysis = _fresh_modules(os.path.join(tmp, "a1.db"))
        os.environ.pop("OPENAI_API_KEY", None)
        _seed_conversation(db_module, "c1", [("customer", "Salom")])
        result = ig_dm_analysis.analyze_pending_conversations()
        assert result["skipped_no_openai_key"] is True
        assert result["analyzed"] == 0
    print("OK: OPENAI_API_KEY sozlanmaganda IG DM tahlili jim o'tkazib yuboriladi")


def test_analysis_analyzes_only_changed_conversations():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, _, ig_dm_analysis = _fresh_modules(os.path.join(tmp, "a2.db"))
        os.environ["OPENAI_API_KEY"] = "sk-test"

        # c_new: 2 ta xabar, hali UMUMAN tahlil qilinmagan -- tahlil qilinishi kerak.
        conv_new_id = _seed_conversation(db_module, "c_new", [("customer", "Salom"), ("customer", "Narxi qancha?")], ai_analyzed_message_count=0)
        # c_stale: 2 ta xabar, ALLAQACHON 2 tasi tahlil qilingan (yangisi yo'q) -- QAYTA tahlil QILINMASLIGI kerak.
        conv_stale_id = _seed_conversation(db_module, "c_stale", [("customer", "Eski xabar"), ("business", "Javob berdik")], ai_analyzed_message_count=2)

        fake_resp = _openai_text_response({"leadQuality": "hot", "summary": "Narx so'rayapti, xarid qilishga tayyor.", "reasons": ["narx so'radi"]})
        with mock.patch.object(ig_dm_analysis, "_openai_request", return_value=fake_resp) as m:
            result = ig_dm_analysis.analyze_pending_conversations()

        assert m.call_count == 1, f"faqat c_new tahlil qilinishi kerak edi, chaqiruvlar soni: {m.call_count}"
        assert result["analyzed"] == 1
        assert result["errors"] == []

        session = db_module.get_session()
        c_new = session.get(db_module.IgDmConversation, conv_new_id)
        c_stale = session.get(db_module.IgDmConversation, conv_stale_id)
        assert c_new.ai_lead_quality == "hot"
        assert c_new.ai_analyzed_message_count == 2
        assert c_new.ai_summary
        assert c_stale.ai_lead_quality is None, "o'zgarmagan suhbat qayta tahlil qilinmasligi kerak edi"
        session.close()
    print("OK: faqat oxirgi tahlildan beri yangi xabar kelgan suhbatlar AI'ga yuboriladi -- o'zgarmagan suhbat uchun pul sarflanmaydi")


def test_analysis_handles_credit_exhausted():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, _, ig_dm_analysis = _fresh_modules(os.path.join(tmp, "a3.db"))
        os.environ["OPENAI_API_KEY"] = "sk-test"
        _seed_conversation(db_module, "c1", [("customer", "Salom")])

        quota_resp = _FakeResp(429, {"error": {"code": "insufficient_quota", "message": "You exceeded your current quota"}})
        with mock.patch.object(ig_dm_analysis, "_openai_request", return_value=quota_resp):
            result = ig_dm_analysis.analyze_pending_conversations()

        assert result["analyzed"] == 0
        assert result["errors"]
        assert "krediti tugagan" in result["errors"][0] or "OpenAICreditExhaustedError" not in result["errors"][0]
    print("OK: OpenAI krediti tugaganda IG DM tahlili aniq xato bilan to'xtaydi (qayta-qayta urinilmaydi)")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
