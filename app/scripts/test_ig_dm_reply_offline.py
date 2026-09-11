"""test_ig_dm_reply_offline.py — 2026-09, foydalanuvchi so'rovi:
"habarlar joyida bombosh man shunaqa qilishm kerakki bu yerda manager
jovob berolidigan qilishim kerak ... gaplashish joyini suhbat oborilsin
shu yerda ... tayor ozini shablonlarini yaratib olish mumkin bolsin".

Bu fayl `/instagram-xabarlar` sahifasining YANGI qismlarini (Meta'ga
haqiqiy so'rov yubormasdan, TARMOQSIZ) tekshiradi:
  - Menejer suhbatga to'g'ridan-to'g'ri javob yoza oladi
    (`/instagram-xabarlar/reply` -- `meta_api.send_instagram_message` mock
    qilinadi), javob yozilgach suhbat "javobsiz" holatidan chiqadi.
  - Meta xato qaytarsa (masalan 24 soatlik oyna tugagan) -- foydalanuvchiga
    tushunarli xabar ko'rsatiladi VA bazaga soxta xabar YOZILMAYDI.
  - Javob shablonlarini (CannedReply) qo'shish/o'chirish va ular sahifada
    (JS orqali tezkor qo'yish uchun `data-text` bilan) ko'rinishi.
  - Instagram ulanmagan kompaniya uchun sahifa xato bermay, tushunarli
    banner bilan ochiladi.
  - Boshqa kompaniyaning suhbati/shabloni bu yerga chiqmaydi (multi-tenant
    izolyatsiya, `with_loader_criteria` orqali).

Ishga tushirish:
    cd app && python3 scripts/test_ig_dm_reply_offline.py
"""

import os
import sys
import tempfile
import datetime as dt
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMPDIR = tempfile.mkdtemp()
_DB_PATH = os.path.join(_TMPDIR, "test_ig_dm_reply.db")

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("META_ACCESS_TOKEN", "test-dummy-token")
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_test_dummy")
os.environ.setdefault("META_PAGE_ID", "page_test_dummy")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402
import meta_api  # noqa: E402

app_module.app.config["TESTING"] = True
db_module.init_db()


def _login(client, username, password="parol123"):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=False)


def _make_company(name, *, connect_meta=True):
    session = db_module.get_session()
    try:
        c = db_module.Company(name=name, is_active=True)
        if connect_meta:
            c.meta_page_id = "page_123"
            c.set_meta_access_token("tok_abc")
        session.add(c)
        session.commit()
        cid = c.id
        m = db_module.Manager(username=f"{name}_admin", full_name="A", role="admin", company_id=cid)
        m.set_password("parol123")
        session.add(m)
        session.commit()
        return cid, m.username
    finally:
        session.close()


def _make_conversation(company_id, *, external_id, customer_ig_id, is_unanswered=True):
    session = db_module.get_session()
    try:
        now = dt.datetime.utcnow()
        conv = db_module.IgDmConversation(
            company_id=company_id, external_id=external_id, customer_ig_id=customer_ig_id,
            customer_username="mijoz1", message_count=1, last_message_at=now,
            last_message_text="Narxi qancha?", last_message_from="customer",
            is_unanswered=is_unanswered, unanswered_since=now - dt.timedelta(minutes=40) if is_unanswered else None,
        )
        session.add(conv)
        session.flush()
        session.add(db_module.IgDmMessage(
            conversation_id=conv.id, company_id=company_id, external_id=f"{external_id}-m1",
            sender="customer", text="Narxi qancha?", sent_at=now,
        ))
        session.commit()
        return conv.id
    finally:
        session.close()


def test_reply_sends_message_and_clears_unanswered():
    cid, username = _make_company("DM javob A")
    conv_id = _make_conversation(cid, external_id="conv_r1", customer_ig_id="CUST_R1")

    with app_module.app.test_client() as client:
        _login(client, username)
        with mock.patch.object(meta_api, "send_instagram_message", return_value={"message_id": "mid_1"}) as m:
            r = client.post("/instagram-xabarlar/reply", data={"conversation_id": conv_id, "text": "Narxi 100$"}, follow_redirects=True)
        assert r.status_code == 200
        assert m.call_count == 1
        assert m.call_args.args[0] == "CUST_R1"
        assert m.call_args.args[1] == "Narxi 100$"

    session = db_module.get_session()
    try:
        with db_module.unscoped():
            conv = session.get(db_module.IgDmConversation, conv_id)
            assert conv.is_unanswered is False, "javob yozilgach suhbat 'javobsiz' bo'lmasligi kerak"
            assert conv.unanswered_since is None
            assert conv.last_message_from == "business"
            assert conv.last_message_text == "Narxi 100$"
            msgs = session.query(db_module.IgDmMessage).filter_by(conversation_id=conv_id).order_by(db_module.IgDmMessage.id).all()
            assert len(msgs) == 2, "mijoz xabari + menejer javobi -- jami 2 ta bo'lishi kerak"
            assert msgs[-1].sender == "business"
            assert msgs[-1].text == "Narxi 100$"
    finally:
        session.close()
    print("OK: menejer yozgan javob Meta'ga yuboriladi, bazaga saqlanadi va suhbat 'javob berilgan' holatiga o'tadi")


def test_reply_meta_error_shown_and_nothing_saved():
    cid, username = _make_company("DM javob B")
    conv_id = _make_conversation(cid, external_id="conv_r2", customer_ig_id="CUST_R2")

    with app_module.app.test_client() as client:
        _login(client, username)
        error = meta_api.MetaAPIError({"message": "This message is sent outside of allowed window", "code": 10, "error_subcode": 2018278})
        with mock.patch.object(meta_api, "send_instagram_message", side_effect=error):
            r = client.post("/instagram-xabarlar/reply", data={"conversation_id": conv_id, "text": "Kech javob"}, follow_redirects=True)
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "24 soatlik" in html, "24-soatlik oyna haqida tushunarli xabar ko'rsatilishi kerak"

    session = db_module.get_session()
    try:
        with db_module.unscoped():
            conv = session.get(db_module.IgDmConversation, conv_id)
            assert conv.is_unanswered is True, "yuborish muvaffaqiyatsiz bo'lsa, holat o'zgarmasligi kerak"
            msgs = session.query(db_module.IgDmMessage).filter_by(conversation_id=conv_id).all()
            assert len(msgs) == 1, "Meta xato qaytarsa, soxta 'business' xabar bazaga yozilmasligi kerak"
    finally:
        session.close()
    print("OK: Meta xatosi (masalan 24 soatlik oyna) tushunarli ko'rsatiladi va bazaga soxta xabar yozilmaydi")


def test_canned_reply_add_shows_on_page_and_delete_removes():
    cid, username = _make_company("DM shablon A")
    conv_id = _make_conversation(cid, external_id="conv_t1", customer_ig_id="CUST_T1", is_unanswered=False)

    with app_module.app.test_client() as client:
        _login(client, username)
        # ESLATMA: sarlavha ATAYLAB forma placeholder'idagi namunadan ("Masalan:
        # Narx so'ralganda") FARQLI qilib tanlandi -- aks holda "matn sahifada
        # bor/yo'q" tekshiruvi placeholder tufayli har doim "bor" chiqib,
        # sinov hech narsani chindan tekshirmagan bo'lar edi.
        r = client.post("/instagram-xabarlar/templates", data={"action": "add", "title": "Yetkazib berish haqida", "text": "Yetkazib berish bepul, 1-2 kun ichida."}, follow_redirects=True)
        assert r.status_code == 200

        r2 = client.get(f"/instagram-xabarlar?c={conv_id}")
        html = r2.get_data(as_text=True)
        assert "Yetkazib berish haqida" in html
        assert 'data-text="Yetkazib berish bepul, 1-2 kun ichida."' in html, "JS tezkor-qo'yish uchun matn data-text atributida bo'lishi kerak"

        session = db_module.get_session()
        try:
            t = session.query(db_module.CannedReply).filter_by(company_id=cid).first()
            template_id = t.id
        finally:
            session.close()

        r3 = client.post("/instagram-xabarlar/templates", data={"action": "delete", "template_id": template_id}, follow_redirects=True)
        assert r3.status_code == 200
        r4 = client.get("/instagram-xabarlar")
        assert "Yetkazib berish haqida" not in r4.get_data(as_text=True)
    print("OK: javob shablonini qo'shish sahifada (tezkor qo'yish uchun data-text bilan) darhol ko'rinadi, o'chirish ro'yxatdan olib tashlaydi")


def test_not_configured_company_shows_banner_no_crash():
    cid, username = _make_company("DM ulanmagan", connect_meta=False)
    with app_module.app.test_client() as client:
        _login(client, username)
        r = client.get("/instagram-xabarlar")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "Instagram DM ulanmagan" in html
        assert "Hali Instagram DM suhbati yo'q" in html or "Instagram hali ulanmagan" in html
    print("OK: Meta ulanmagan kompaniya uchun sahifa xatosiz, tushunarli banner bilan ochiladi")


def test_cross_company_conversation_and_template_isolation():
    cid_a, user_a = _make_company("DM izolyatsiya A")
    cid_b, user_b = _make_company("DM izolyatsiya B")
    conv_a = _make_conversation(cid_a, external_id="conv_iso_a", customer_ig_id="CUST_ISO_A")
    _make_conversation(cid_b, external_id="conv_iso_b", customer_ig_id="CUST_ISO_B")

    session = db_module.get_session()
    try:
        session.add(db_module.CannedReply(company_id=cid_a, title="Faqat A uchun", text="A matni"))
        session.commit()
    finally:
        session.close()

    with app_module.app.test_client() as client:
        _login(client, user_b)
        html = client.get("/instagram-xabarlar").get_data(as_text=True)
        assert "Faqat A uchun" not in html, "boshqa kompaniyaning shabloni ko'rinmasligi kerak"
        assert "mijoz1" not in html or "CUST_ISO_A" not in html

        # B kompaniyasi menejeri A kompaniyaning suhbatiga javob yoza olmasligi kerak.
        with mock.patch.object(meta_api, "send_instagram_message") as m:
            client.post("/instagram-xabarlar/reply", data={"conversation_id": conv_a, "text": "ruxsatsiz urinish"}, follow_redirects=True)
        assert m.call_count == 0, "boshqa kompaniyaning suhbatiga yozib bo'lmasligi kerak"
    print("OK: boshqa kompaniyaning IG DM suhbati/shabloni ko'rinmaydi va unga javob yozib bo'lmaydi")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
