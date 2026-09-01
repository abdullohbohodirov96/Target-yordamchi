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
             mock.patch.object(ig_dm_sync.meta_api, "get_instagram_conversations", return_value=[conv]), \
             mock.patch.object(ig_dm_sync.meta_api, "get_instagram_conversation_messages", return_value=messages):
            result = ig_dm_sync.sync_once()

        assert result["configured"] is True
        assert result["conversations_checked"] == 1
        assert result["new_messages"] == 2
        assert len(result["overdue"]) == 1, f"45 daqiqa > 30 daqiqa chegarasi -- overdue bo'lishi kerak edi: {result}"

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
             mock.patch.object(ig_dm_sync.meta_api, "get_instagram_conversations", return_value=[conv]), \
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
             mock.patch.object(ig_dm_sync.meta_api, "get_instagram_conversations", return_value=[conv]), \
             mock.patch.object(ig_dm_sync.meta_api, "get_instagram_conversation_messages", return_value=messages):
            result1 = ig_dm_sync.sync_once()
        assert len(result1["overdue"]) == 1
        conv_id = result1["overdue"][0]["conversation_id"]
        ig_dm_sync.mark_alert_sent(conv_id)

        # Xuddi shu (yangi xabarsiz) holatda QAYTA sinxronlansa -- ogohlantirish
        # IKKINCHI marta yubormaslik ro'yxatiga tushmasligi kerak.
        with mock.patch.object(ig_dm_sync.meta_api, "get_instagram_business_account_id", return_value="BIZ_ID"), \
             mock.patch.object(ig_dm_sync.meta_api, "get_instagram_conversations", return_value=[conv]), \
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
             mock.patch.object(ig_dm_sync.meta_api, "get_instagram_conversations", return_value=[conv_ok, conv_bad]), \
             mock.patch.object(ig_dm_sync.meta_api, "get_instagram_conversation_messages", side_effect=_messages_side_effect):
            result = ig_dm_sync.sync_once()

        assert result["conversations_checked"] == 2
        assert result["new_messages"] == 1  # faqat conv_ok muvaffaqiyatli
        assert result["errors"], "conv_bad xatosi errors ro'yxatiga tushishi kerak"
        assert any("ruxsat" in e.lower() or "permission" in e.lower() for e in result["errors"])
    print("OK: bitta suhbatning Meta xatosi butun sinxronizatsiyani to'xtatmaydi (qolganlari davom etadi)")


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
