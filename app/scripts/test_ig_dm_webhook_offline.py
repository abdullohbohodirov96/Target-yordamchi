"""test_ig_dm_webhook_offline.py — 2026-09, foydalanuvchi so'rovi (item 9):
"Webhook arxitekturasini ham qo'sh: yangi Instagram DM kelganda Meta
webhook orqali DBga yozilsin. Polling/Yangilash faqat fallback va
initial sync bo'lsin."

TARMOQSIZ (offline, Meta'ga chiqmasdan) quyidagilarni tekshiradi:
  1. `GET /webhooks/instagram` -- Meta'ning verifikatsiya handshake'i:
     to'g'ri `hub.verify_token` bilan `hub.challenge`ni AYNAN qaytaradi,
     noto'g'ri token bilan 403.
  2. `meta_api.verify_webhook_signature()` -- HMAC-SHA256 imzoni to'g'ri
     tasdiqlaydi/rad etadi.
  3. `POST /webhooks/instagram` -- imzosiz/noto'g'ri imzoli so'rov 401
     bilan rad etiladi, HECH NARSA bazaga yozilmaydi.
  4. `POST /webhooks/instagram` -- to'g'ri imzo bilan kelgan xabar
     bazaga yoziladi, VA -- muhimi -- IKKI XIL kompaniyaning (ikki xil
     `meta_page_id`) webhook hodisalari ALOHIDA-ALOHIDA, ARALASHMASDAN
     o'z company_id'siga yoziladi (multi-tenant izolyatsiya).
  5. `meta_api.subscribe_page_to_messaging_webhook()` -- to'g'ri
     endpoint/parametr bilan chaqirilishi.

Ishga tushirish:
    cd app && python3 scripts/test_ig_dm_webhook_offline.py
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
_DB_PATH = os.path.join(_TMPDIR, "test_ig_dm_webhook.db")

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

app_module.app.config["TESTING"] = True
db_module.init_db()

meta_api.META_APP_SECRET = "test-app-secret-xyz"
meta_api.META_WEBHOOK_VERIFY_TOKEN = "test-verify-token-123"


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


def _sign(body: bytes) -> str:
    digest = hmac.new(meta_api.META_APP_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _messaging_payload(page_id: str, *, sender_id: str, recipient_id: str, mid: str, text: str) -> bytes:
    payload = {
        "object": "instagram",
        "entry": [{
            "id": page_id,
            "messaging": [{
                "sender": {"id": sender_id},
                "recipient": {"id": recipient_id},
                "timestamp": int(dt.datetime.utcnow().timestamp() * 1000),
                "message": {"mid": mid, "text": text},
            }],
        }],
    }
    return json.dumps(payload).encode("utf-8")


def test_verify_handshake_echoes_challenge_with_correct_token():
    with app_module.app.test_client() as client:
        r = client.get("/webhooks/instagram", query_string={
            "hub.mode": "subscribe", "hub.verify_token": "test-verify-token-123", "hub.challenge": "CHALLENGE_XYZ",
        })
        assert r.status_code == 200
        assert r.get_data(as_text=True) == "CHALLENGE_XYZ"
    print("OK: GET /webhooks/instagram to'g'ri verify_token bilan hub.challenge'ni AYNAN qaytaradi")


def test_verify_handshake_rejects_wrong_token():
    with app_module.app.test_client() as client:
        r = client.get("/webhooks/instagram", query_string={
            "hub.mode": "subscribe", "hub.verify_token": "NOTO'G'RI", "hub.challenge": "CHALLENGE_XYZ",
        })
        assert r.status_code == 403
    print("OK: GET /webhooks/instagram noto'g'ri verify_token bilan 403 qaytaradi")


def test_verify_webhook_signature_accepts_valid_and_rejects_tampered():
    body = b'{"object":"instagram","entry":[]}'
    valid_sig = _sign(body)
    assert meta_api.verify_webhook_signature(body, valid_sig) is True

    tampered_body = b'{"object":"instagram","entry":[{"evil":true}]}'
    assert meta_api.verify_webhook_signature(tampered_body, valid_sig) is False
    assert meta_api.verify_webhook_signature(body, None) is False
    assert meta_api.verify_webhook_signature(body, "sha256=deadbeef") is False
    print("OK: verify_webhook_signature() to'g'ri HMAC-SHA256 imzoni tasdiqlaydi, o'zgartirilgan/soxta imzoni rad etadi")


def test_post_webhook_without_signature_is_rejected_and_writes_nothing():
    page_id = "page_sig_test"
    _make_company("Signature Test", page_id=page_id)
    body = _messaging_payload(page_id, sender_id="CUST_SIG", recipient_id="BIZ", mid="wamid.sig1", text="Salom")

    with app_module.app.test_client() as client:
        r = client.post("/webhooks/instagram", data=body, content_type="application/json")
        assert r.status_code == 401

    session = db_module.get_session()
    try:
        with db_module.unscoped():
            msg = session.query(db_module.IgDmMessage).filter_by(external_id="wamid.sig1").first()
        assert msg is None, "imzosiz so'rovdan HECH NARSA bazaga yozilmasligi kerak"
    finally:
        session.close()
    print("OK: POST /webhooks/instagram imzosiz so'rovni 401 bilan rad etadi, bazaga hech narsa yozmaydi")


def test_post_webhook_with_valid_signature_ingests_message():
    page_id = "page_valid_sig"
    company_id = _make_company("Valid Sig Co", page_id=page_id)
    body = _messaging_payload(page_id, sender_id="CUST_VALID", recipient_id="BIZ", mid="wamid.valid1", text="Narxi qancha?")

    with app_module.app.test_client() as client:
        r = client.post("/webhooks/instagram", data=body, content_type="application/json", headers={
            "X-Hub-Signature-256": _sign(body),
        })
        assert r.status_code == 200

    session = db_module.get_session()
    try:
        with db_module.unscoped():
            msg = session.query(db_module.IgDmMessage).filter_by(external_id="wamid.valid1").first()
        assert msg is not None, "to'g'ri imzoli webhook xabari bazaga yozilishi kerak"
        assert msg.company_id == company_id
        assert msg.text == "Narxi qancha?"
        assert msg.sender == "customer"
    finally:
        session.close()
    print("OK: POST /webhooks/instagram to'g'ri imzo bilan kelgan xabarni darhol bazaga yozadi (Graph API'ga qayta murojaat qilmasdan)")


def test_post_webhook_isolates_messages_by_company():
    """MUHIM (multi-tenant): ikki xil kompaniyaning (ikki xil
    `meta_page_id`) webhook hodisalari ARALASHMASDAN, HAR BIRI O'Z
    `company_id`siga yozilishi kerak."""
    page_a = "page_iso_a"
    page_b = "page_iso_b"
    company_a = _make_company("Izolyatsiya A", page_id=page_a)
    company_b = _make_company("Izolyatsiya B", page_id=page_b)

    body_a = _messaging_payload(page_a, sender_id="CUST_A", recipient_id="BIZ_A", mid="wamid.iso.a1", text="A kompaniyasiga xabar")
    body_b = _messaging_payload(page_b, sender_id="CUST_B", recipient_id="BIZ_B", mid="wamid.iso.b1", text="B kompaniyasiga xabar")

    with app_module.app.test_client() as client:
        r_a = client.post("/webhooks/instagram", data=body_a, content_type="application/json", headers={"X-Hub-Signature-256": _sign(body_a)})
        r_b = client.post("/webhooks/instagram", data=body_b, content_type="application/json", headers={"X-Hub-Signature-256": _sign(body_b)})
        assert r_a.status_code == 200 and r_b.status_code == 200

    session = db_module.get_session()
    try:
        with db_module.unscoped():
            msg_a = session.query(db_module.IgDmMessage).filter_by(external_id="wamid.iso.a1").first()
            msg_b = session.query(db_module.IgDmMessage).filter_by(external_id="wamid.iso.b1").first()
            conv_a = session.query(db_module.IgDmConversation).filter_by(id=msg_a.conversation_id).first()
            conv_b = session.query(db_module.IgDmConversation).filter_by(id=msg_b.conversation_id).first()
        assert msg_a.company_id == company_a and msg_b.company_id == company_b
        assert conv_a.company_id == company_a and conv_b.company_id == company_b
        assert conv_a.customer_ig_id == "CUST_A" and conv_b.customer_ig_id == "CUST_B"
        assert conv_a.id != conv_b.id, "ikkala kompaniyaning suhbati ALOHIDA qatorlar bo'lishi kerak, ARALASHMASLIGI kerak"
    finally:
        session.close()
    print("OK: POST /webhooks/instagram ikki xil kompaniyaning DM hodisalarini aralashtirmasdan, HAR BIRINI O'Z company_id'siga yozadi")


def test_post_webhook_unknown_page_is_ignored_silently():
    """Hech qaysi kompaniyaga tegishli bo'lmagan (yoki hali ulanmagan)
    `page_id` uchun kelgan webhook -- xato tashlamasdan, jim
    o'tkazib yuborilishi kerak (Meta HAR DOIM tezkor 200 kutadi)."""
    body = _messaging_payload("page_unknown_xyz", sender_id="CUST_U", recipient_id="BIZ", mid="wamid.unknown1", text="?")
    with app_module.app.test_client() as client:
        r = client.post("/webhooks/instagram", data=body, content_type="application/json", headers={
            "X-Hub-Signature-256": _sign(body),
        })
        assert r.status_code == 200
    session = db_module.get_session()
    try:
        with db_module.unscoped():
            msg = session.query(db_module.IgDmMessage).filter_by(external_id="wamid.unknown1").first()
        assert msg is None
    finally:
        session.close()
    print("OK: noma'lum page_id uchun webhook xatosiz, jim o'tkazib yuboriladi (200 qaytadi)")


def test_subscribe_page_to_messaging_webhook_calls_correct_endpoint():
    calls = []

    def fake_post(path, data, token=None):
        calls.append((path, dict(data), token))
        return {"success": True}

    with mock.patch.object(meta_api, "_post", side_effect=fake_post):
        result = meta_api.subscribe_page_to_messaging_webhook("page_123", "PAGE_TOKEN_ABC")

    assert result == {"success": True}
    assert len(calls) == 1
    path, data, token = calls[0]
    assert path == "page_123/subscribed_apps"
    assert data == {"subscribed_fields": "messages"}
    assert token == "PAGE_TOKEN_ABC"
    print("OK: subscribe_page_to_messaging_webhook() to'g'ri endpoint (POST /{page_id}/subscribed_apps?subscribed_fields=messages) chaqiradi")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
