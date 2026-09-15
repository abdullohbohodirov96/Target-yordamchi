"""test_ig_dm_ad_attribution_offline.py — 2026-09, foydalanuvchi so'rovi
("birato'lasini to'g'irlab yubor. hozir smsdan misol uchun target
yoqilinadi har xil... o'shani aniqlash yo'lini topishimiz kerak...
agar sifatli chiqsa, unga ham sifatli sifatsiz... copyni ulash mumkin
bo'lsa, copyni ulash kerak"):

TARMOQSIZ (offline, Meta'ga chiqmasdan, faqat mock) quyidagilarni tekshiradi:
  1. Webhook orqali kelgan xabarda `referral.ad_id` bo'lsa (ham
     `m.referral`, ham `m.message.referral` ko'rinishida) --
     `IgDmConversation.source_ad_id` shu qiymat bilan to'ldiriladi.
  2. `ig_dm_sync.resolve_ad_sources()` -- HAR BIR NOYOB `ad_id` uchun
     BIR MARTA (suhbatlar soniga qarab EMAS) Meta'dan reklama nomi+copy
     matnini so'raydi va `IgDmAdSource`ga keshlaydi; ikkinchi chaqiriqda
     ALLAQACHON keshlangan ad_id uchun QAYTA so'ramaydi (xarajat nazorati).
  3. `resolve_ad_sources()` -- Meta xato qaytarsa (masalan reklama
     o'chirilgan), qatorni `resolve_error` bilan baribir keshlaydi (aks
     holda har 15 daqiqada xato bergan reklamani qayta-qayta so'rayverardi).
  4. `ig_dm_analytics.build_dm_report()`/`build_period_analytics()` --
     suhbatga reklama nomi/copy'sini biriktiradi, va `by_ad` davr
     bo'yicha hot/warm/cold+sotilgan taqsimotini to'g'ri hisoblaydi.
  5. `POST /instagram-xabarlar/lid` -- suhbatni CRM lidiga aylantiradi,
     `IgDmConversation.linked_lead_id`ni belgilaydi, va IKKINCHI marta
     bosilsa YANGI lid YARATMAYDI (mavjudiga qaytaradi).

Ishga tushirish:
    cd app && python3 scripts/test_ig_dm_ad_attribution_offline.py
"""

import os
import sys
import json
import hmac
import hashlib
import tempfile
import datetime as dt
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMPDIR = tempfile.mkdtemp()
_DB_PATH = os.path.join(_TMPDIR, "test_ig_dm_ad_attribution.db")

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("META_ACCESS_TOKEN", "test-dummy-token")
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_test_dummy")
os.environ.setdefault("META_PAGE_ID", "page_test_dummy")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
os.environ["META_APP_SECRET"] = "test-app-secret-xyz"
os.environ["META_WEBHOOK_VERIFY_TOKEN"] = "test-verify-token-123"

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402
import meta_api  # noqa: E402
import ig_dm_sync  # noqa: E402
import ig_dm_analytics  # noqa: E402

app_module.app.config["TESTING"] = True
app_module.app.config["WTF_CSRF_ENABLED"] = False
db_module.init_db()

meta_api.META_APP_SECRET = "test-app-secret-xyz"
meta_api.META_WEBHOOK_VERIFY_TOKEN = "test-verify-token-123"


def _login(client, username, password="parol123"):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=False)


def _make_company(name, *, page_id):
    session = db_module.get_session()
    try:
        c = db_module.Company(name=name, is_active=True, ig_business_id="IG_BIZ")
        c.meta_page_id = page_id
        c.set_meta_access_token("tok_abc")
        session.add(c)
        session.commit()
        return c.id
    finally:
        session.close()


def _make_company_with_manager(name, *, page_id=None):
    session = db_module.get_session()
    try:
        c = db_module.Company(name=name, is_active=True)
        if page_id:
            c.meta_page_id = page_id
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


def _sign(body: bytes) -> str:
    digest = hmac.new(meta_api.META_APP_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _messaging_payload_with_referral(page_id, *, sender_id, mid, text, ad_id, referral_on_message=False):
    referral = {"ad_id": ad_id, "source": "ADS", "type": "OPEN_THREAD"}
    m = {
        "sender": {"id": sender_id},
        "recipient": {"id": "BIZ"},
        "timestamp": int(dt.datetime.utcnow().timestamp() * 1000),
        "message": {"mid": mid, "text": text},
    }
    if referral_on_message:
        m["message"]["referral"] = referral
    else:
        m["referral"] = referral
    payload = {"object": "instagram", "entry": [{"id": page_id, "messaging": [m]}]}
    return json.dumps(payload).encode("utf-8")


def _post_webhook(client, body):
    return client.post("/webhooks/instagram", data=body, content_type="application/json", headers={
        "X-Hub-Signature-256": _sign(body),
    })


def test_webhook_referral_on_messaging_item_sets_source_ad_id():
    page_id = "page_ref_top"
    _make_company("Referral Top Test", page_id=page_id)
    body = _messaging_payload_with_referral(page_id, sender_id="CUST_REF1", mid="wamid.ref1", text="Salom, narxi?", ad_id="ad_111")

    with app_module.app.test_client() as client:
        r = _post_webhook(client, body)
        assert r.status_code == 200

    session = db_module.get_session()
    try:
        with db_module.unscoped():
            msg = session.query(db_module.IgDmMessage).filter_by(external_id="wamid.ref1").first()
            conv = session.query(db_module.IgDmConversation).filter_by(id=msg.conversation_id).first()
        assert conv.source_ad_id == "ad_111"
    finally:
        session.close()
    print("OK: webhook 'messaging' elementining O'ZIDAGI referral.ad_id -> IgDmConversation.source_ad_id")


def test_webhook_referral_on_message_field_sets_source_ad_id():
    page_id = "page_ref_msg"
    _make_company("Referral Message Test", page_id=page_id)
    body = _messaging_payload_with_referral(
        page_id, sender_id="CUST_REF2", mid="wamid.ref2", text="Qiziqdim", ad_id="ad_222", referral_on_message=True,
    )

    with app_module.app.test_client() as client:
        r = _post_webhook(client, body)
        assert r.status_code == 200

    session = db_module.get_session()
    try:
        with db_module.unscoped():
            msg = session.query(db_module.IgDmMessage).filter_by(external_id="wamid.ref2").first()
            conv = session.query(db_module.IgDmConversation).filter_by(id=msg.conversation_id).first()
        assert conv.source_ad_id == "ad_222"
    finally:
        session.close()
    print("OK: webhook 'message.referral.ad_id' (ikkinchi mumkin bo'lgan joylashuv) ham -> source_ad_id")


def test_resolve_ad_sources_caches_once_per_unique_ad_id():
    session = db_module.get_session()
    try:
        with db_module.unscoped():
            company = db_module.Company(name="Resolve Test Co", is_active=True)
            session.add(company)
            session.commit()
            company_id = company.id

            # ikkita suhbat, IKKALASI ham BIR XIL ad_id'dan -- shuning
            # uchun Graph API BIR MARTA chaqirilishi kerak (ikki marta EMAS).
            for i, ad_id in enumerate(["ad_shared", "ad_shared"]):
                session.add(db_module.IgDmConversation(
                    external_id=f"resolve-test-{i}", company_id=company_id,
                    customer_ig_id=f"cust_{i}", channel="instagram", source_ad_id=ad_id,
                ))
            session.commit()
    finally:
        session.close()

    calls = []

    def fake_get_ad_creative_details(ad_id, *, access_token=None):
        calls.append(ad_id)
        return {
            "ad_id": ad_id, "ad_name": "Kuzgi aksiya reklamasi",
            "adset_id": "as1", "creative_id": "cr1",
            "object_story_spec": {"link_data": {"title": "Kuzgi aksiya", "message": "Bugun -30% chegirma!"}},
            "image_hash": None, "video_id": None,
        }

    session = db_module.get_session()
    try:
        with mock.patch.object(meta_api, "get_ad_creative_details", side_effect=fake_get_ad_creative_details):
            with db_module.unscoped():
                n1 = ig_dm_sync.resolve_ad_sources(session, company_id=company_id, access_token="tok")
            session.commit()
            assert n1 == 1, f"bitta NOYOB ad_id uchun bitta qator keshlanishi kerak, oldi={n1}"
            assert calls == ["ad_shared"], "Graph API BIR MARTA (noyob ad_id soniga qarab) chaqirilishi kerak"

            with db_module.unscoped():
                cached = session.query(db_module.IgDmAdSource).filter_by(company_id=company_id, ad_id="ad_shared").first()
            assert cached is not None
            assert cached.ad_name == "Kuzgi aksiya reklamasi"
            assert cached.ad_copy == "Kuzgi aksiya\nBugun -30% chegirma!"

            # ikkinchi chaqiriq -- ALLAQACHON keshlangan, Graph API'ga
            # QAYTA so'rov YUBORILMASLIGI kerak (xarajat nazorati).
            with db_module.unscoped():
                n2 = ig_dm_sync.resolve_ad_sources(session, company_id=company_id, access_token="tok")
            session.commit()
            assert n2 == 0, "allaqachon keshlangan ad_id uchun qayta so'rov yuborilmasligi kerak"
            assert calls == ["ad_shared"], "ikkinchi chaqiriqda Graph API UMUMAN chaqirilmasligi kerak"
    finally:
        session.close()
    print("OK: resolve_ad_sources() har bir NOYOB ad_id uchun FAQAT BIR MARTA Meta'ga so'rov yuboradi va keshlaydi")


def test_resolve_ad_sources_caches_error_without_retrying():
    session = db_module.get_session()
    try:
        with db_module.unscoped():
            company = db_module.Company(name="Resolve Error Co", is_active=True)
            session.add(company)
            session.commit()
            company_id = company.id
            session.add(db_module.IgDmConversation(
                external_id="resolve-err-test", company_id=company_id,
                customer_ig_id="cust_err", channel="instagram", source_ad_id="ad_deleted",
            ))
            session.commit()
    finally:
        session.close()

    calls = []

    def fake_error(ad_id, *, access_token=None):
        calls.append(ad_id)
        raise meta_api.MetaAPIError("Bu reklama o'chirilgan yoki topilmadi")

    session = db_module.get_session()
    try:
        with mock.patch.object(meta_api, "get_ad_creative_details", side_effect=fake_error):
            with db_module.unscoped():
                n1 = ig_dm_sync.resolve_ad_sources(session, company_id=company_id, access_token="tok")
            session.commit()
            assert n1 == 1
            with db_module.unscoped():
                cached = session.query(db_module.IgDmAdSource).filter_by(company_id=company_id, ad_id="ad_deleted").first()
            assert cached is not None and cached.resolve_error
            assert cached.ad_name is None

            with db_module.unscoped():
                n2 = ig_dm_sync.resolve_ad_sources(session, company_id=company_id, access_token="tok")
            session.commit()
            assert n2 == 0, "xato bilan keshlangan ad_id ham QAYTA so'ralmasligi kerak"
            assert calls == ["ad_deleted"], "faqat bitta chaqiriq bo'lishi kerak"
    finally:
        session.close()
    print("OK: resolve_ad_sources() Meta xato qaytarsa ham qatorni keshlaydi (resolve_error bilan), qayta-qayta so'ramaydi")


def test_analytics_attach_ad_name_copy_and_by_ad_breakdown():
    session = db_module.get_session()
    try:
        with db_module.unscoped():
            company = db_module.Company(name="Analytics Ad Co", is_active=True)
            session.add(company)
            session.commit()
            company_id = company.id

            session.add(db_module.IgDmAdSource(
                company_id=company_id, ad_id="ad_hot", ad_name="Yozgi to'plam",
                ad_copy="Yozgi to'plam\nHozir buyurtma bering!", resolved_at=dt.datetime.utcnow(),
            ))
            session.commit()

            now = dt.datetime.utcnow()
            conv_hot = db_module.IgDmConversation(
                external_id="analytics-hot", company_id=company_id, customer_ig_id="cust_hot",
                channel="instagram", source_ad_id="ad_hot", ai_lead_quality="hot",
                last_message_at=now, message_count=1,
            )
            session.add(conv_hot)
            session.commit()
            session.add(db_module.IgDmMessage(
                conversation_id=conv_hot.id, external_id="analytics-hot-m1", sender="customer",
                text="Sotib olaman", sent_at=now, company_id=company_id,
            ))
            session.commit()

            report = ig_dm_analytics.build_dm_report(session)
            conv_dict = next(c for c in report["conversations"] if c["id"] == conv_hot.id)
            assert conv_dict["source_ad_name"] == "Yozgi to'plam"
            assert conv_dict["source_ad_copy"] == "Yozgi to'plam\nHozir buyurtma bering!"

            period = ig_dm_analytics.build_period_analytics(session, now - dt.timedelta(days=1), now + dt.timedelta(days=1))
            assert period["by_ad"], "davr tahlilida by_ad ro'yxati bo'sh bo'lmasligi kerak"
            ad_entry = next(a for a in period["by_ad"] if a["ad_id"] == "ad_hot")
            assert ad_entry["ad_name"] == "Yozgi to'plam"
            assert ad_entry["hot_count"] == 1
            assert ad_entry["conversation_count"] == 1
    finally:
        session.close()
    print("OK: build_dm_report()/build_period_analytics() reklama nomi+copy'ni biriktiradi va by_ad taqsimotini to'g'ri hisoblaydi")


def test_dm_to_lead_route_creates_lead_and_is_idempotent():
    company_id, username = _make_company_with_manager("DM Lead Conversion Co", page_id="page_lead_conv")

    session = db_module.get_session()
    try:
        with db_module.unscoped():
            session.add(db_module.IgDmAdSource(
                company_id=company_id, ad_id="ad_convert", ad_name="Konversiya reklamasi",
                ad_copy="Konversiya reklamasi\nBugun buyurtma bering", resolved_at=dt.datetime.utcnow(),
            ))
            conv = db_module.IgDmConversation(
                external_id="lead-conv-test", company_id=company_id, customer_ig_id="cust_lead",
                customer_username="mijoz_ali", channel="instagram", source_ad_id="ad_convert",
                ai_lead_quality="hot", ai_summary="Mijoz narxni so'radi va buyurtma berishni xohlaydi.",
            )
            session.add(conv)
            session.commit()
            conv_id = conv.id
    finally:
        session.close()

    with app_module.app.test_client() as client:
        _login(client, username)

        r1 = client.post("/instagram-xabarlar/lid", data={"conversation_id": conv_id}, follow_redirects=False)
        assert r1.status_code in (302, 303)

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                conv = session.get(db_module.IgDmConversation, conv_id)
                assert conv.linked_lead_id is not None
                lead = session.get(db_module.Lead, conv.linked_lead_id)
            assert lead is not None
            assert lead.source == "dm"
            assert lead.ad_name == "Konversiya reklamasi"
            assert lead.full_name == "mijoz_ali"
            first_lead_id = lead.id
        finally:
            session.close()

        # ikkinchi marta bosilsa -- YANGI lid YARATILMASLIGI, MAVJUDIGA
        # qaytarilishi kerak (ikki marta lid yaratib yubormaslik uchun).
        r2 = client.post("/instagram-xabarlar/lid", data={"conversation_id": conv_id}, follow_redirects=False)
        assert r2.status_code in (302, 303)
        assert f"/leads/{first_lead_id}" in r2.headers.get("Location", "")

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                lead_count = session.query(db_module.Lead).filter_by(company_id=company_id).count()
            assert lead_count == 1, "ikkinchi bosishda YANGI lid yaratilmasligi kerak"
        finally:
            session.close()
    print("OK: POST /instagram-xabarlar/lid DM suhbatidan lid yaratadi, ikkinchi bosishda dublikat YARATMAYDI")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
