"""test_ig_dm_manual_refresh_offline.py — 2026-09, foydalanuvchi TOPGAN
JONLI BUG uchun regressiya testi: "Yangilash" tugmasi (`/instagram-xabarlar
?refresh=1`) bosilganda, `app.py`dagi `_MetaCreds` yengil obyekti
`company.ig_dm_sync_since`ni UNUTIB qoldirar edi -- shu sabab avtomatik
fon-sinxronizatsiya (`ig_dm_sync.sync_all_companies`) to'g'ri `since`
filtri bilan ishlasa ham, aynan shu QO'LDA tugma har safar akkauntning
BUTUN (ko'p yillik) tarixini qayta so'rab, "Please reduce the amount of
data" xatosiga qaytadan olib kelardi.

2026-09 QAYTA TUZATISH (item 1, 5 -- "reduce the amount of data" davom
etgani uchun): `ig_dm_sync_since` ENDI Meta so'roviga UMUMAN
YUBORILMAYDI -- bu fayl endi quyidagilarni tekshiradi:
  1. `_MetaCreds(company)` konstruktori `ig_dm_sync_since`ni to'g'ri
     ko'chirishi (to'g'ridan-to'g'ri, tarmoqsiz birlik tekshiruvi) --
     bu maydon ENDI faqat LOKAL filtr uchun ishlatiladi, lekin baribir
     to'g'ri ko'chirilishi kerak.
  2. `GET /instagram-xabarlar?refresh=1` `meta_api.
     get_instagram_conversations()`ni `since` KWARGISIZ va kichik
     `limit=5` bilan chaqirishi (to'liq HTTP oqimi, Meta'ga tarmoqsiz --
     `get_instagram_conversations` mock qilinadi).
  3. Meta'dan qaytgan, lekin `ig_dm_sync_since`dan OLDIN yangilangan
     suhbat -- LOKAL ravishda chetlab o'tilishi (DBga import
     qilinmasligi).

Ishga tushirish:
    cd app && python3 scripts/test_ig_dm_manual_refresh_offline.py
"""

import os
import sys
import tempfile
import datetime as dt
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMPDIR = tempfile.mkdtemp()
_DB_PATH = os.path.join(_TMPDIR, "test_ig_dm_manual_refresh.db")

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("META_ACCESS_TOKEN", "test-dummy-token")
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_test_dummy")
os.environ.setdefault("META_PAGE_ID", "page_test_dummy")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402
import meta_api  # noqa: E402
import ig_dm_sync  # noqa: E402

app_module.app.config["TESTING"] = True
db_module.init_db()


def _login(client, username, password="parol123"):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=False)


def _make_company(name, *, ig_dm_sync_since=None, page_id=None):
    # HAR BIR kompaniyaga ALOHIDA page_id -- `ig_dm_sync._ig_business_id_cache`
    # `page_id` bo'yicha keshlanadi, shu sabab bir xil page_id qayta
    # ishlatilsa, keyingi testda `get_instagram_business_account_id` mock'i
    # umuman chaqirilmasligi (kesh-hit) mumkin edi.
    page_id = page_id or f"page_{name.replace(' ', '_')}"
    session = db_module.get_session()
    try:
        c = db_module.Company(name=name, is_active=True, ig_business_id="IG_BIZ")
        c.meta_page_id = page_id
        c.set_meta_access_token("tok_abc")
        c.ig_dm_sync_since = ig_dm_sync_since
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


def test_meta_creds_carries_ig_dm_sync_since():
    """Birlik darajasida ANIQ TOPILGAN bug: `_MetaCreds.__init__` avval
    `ig_dm_sync_since`ni umuman o'qimas edi."""
    since = dt.datetime(2026, 9, 11, 12, 0, 0)
    cid, _ = _make_company("MetaCreds A", ig_dm_sync_since=since)
    session = db_module.get_session()
    try:
        company = session.get(db_module.Company, cid)
        creds = app_module._MetaCreds(company)
    finally:
        session.close()
    assert hasattr(creds, "ig_dm_sync_since"), "_MetaCreds'da ig_dm_sync_since maydoni yo'q"
    assert creds.ig_dm_sync_since == since, f"kutilgan {since}, olindi {creds.ig_dm_sync_since}"
    print("OK: _MetaCreds endi company.ig_dm_sync_since'ni to'g'ri ko'chiradi")


def test_manual_refresh_button_never_sends_since_and_uses_small_limit():
    """To'liq HTTP oqimi: 'Yangilash' tugmasi bosilganda so'rov
    `get_instagram_conversations()`ga `since` KWARGISIZ va kichik
    `limit=5` bilan ketishi kerak -- 2026-09 QAYTA TUZATISH (item 1, 2):
    `since` endi Meta so'roviga UMUMAN yuborilmaydi (avval shu narsaning
    o'zi 'reduce the amount of data' xatosini keltirib chiqarayotgan
    bo'lishi mumkin edi)."""
    since = dt.datetime.utcnow() - dt.timedelta(days=3)
    cid, username = _make_company("Manual Refresh A", ig_dm_sync_since=since)

    with app_module.app.test_client() as client:
        _login(client, username)
        with mock.patch.object(meta_api, "get_instagram_business_account_id", return_value="BIZ_ID"), \
             mock.patch.object(meta_api, "get_instagram_conversations", return_value=([], None)) as m:
            r = client.get("/instagram-xabarlar?refresh=1", follow_redirects=True)
        assert r.status_code == 200
        assert m.call_count == 1, "'Yangilash' bosilganda get_instagram_conversations aynan bir marta chaqirilishi kerak"
        _, kwargs = m.call_args
        assert "since" not in kwargs, (
            f"'Yangilash' tugmasi ENDI `since`ni Meta so'roviga UMUMAN yubormasligi kerak, olindi kwargs={kwargs}"
        )
        assert kwargs.get("limit") == 5, f"limit=5 kutilgan edi, olindi {kwargs.get('limit')}"
    print("OK: 'Yangilash' tugmasi endi `since`ni Meta so'roviga yubormaydi, kichik limit=5 bilan so'raydi")


def test_manual_refresh_without_since_still_works():
    """`ig_dm_sync_since` hali NULL bo'lgan (masalan juda eski, hali
    backfill bo'lmagan) kompaniya uchun ham sahifa qulamasligi kerak."""
    cid, username = _make_company("Manual Refresh B", ig_dm_sync_since=None)

    with app_module.app.test_client() as client:
        _login(client, username)
        with mock.patch.object(meta_api, "get_instagram_business_account_id", return_value="BIZ_ID"), \
             mock.patch.object(meta_api, "get_instagram_conversations", return_value=([], None)) as m:
            r = client.get("/instagram-xabarlar?refresh=1", follow_redirects=True)
        assert r.status_code == 200
        _, kwargs = m.call_args
        assert "since" not in kwargs
        assert kwargs.get("limit") == 5
    print("OK: ig_dm_sync_since hali belgilanmagan kompaniya uchun ham 'Yangilash' xatosiz ishlaydi")


def test_manual_refresh_locally_filters_conversation_older_than_sync_since():
    """2026-09 QAYTA TUZATISH (item 5): Meta'dan qaytgan, lekin
    `company.ig_dm_sync_since`dan OLDIN yangilangan suhbat -- Meta
    so'roviga HECH QANDAY `since` yuborilmagan bo'lsa ham -- LOKAL
    ravishda chetlab o'tilishi, DBga import QILINMASLIGI kerak."""
    since = dt.datetime.utcnow() - dt.timedelta(hours=2)
    old_updated = (since - dt.timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    cid, username = _make_company("Manual Refresh C", ig_dm_sync_since=since)
    old_conv = {"id": "old_conv_1", "updated_time": old_updated}

    with app_module.app.test_client() as client:
        _login(client, username)
        with mock.patch.object(meta_api, "get_instagram_business_account_id", return_value="BIZ_ID"), \
             mock.patch.object(meta_api, "get_instagram_conversations", return_value=([old_conv], None)), \
             mock.patch.object(meta_api, "get_instagram_conversation_messages", return_value=[]):
            r = client.get("/instagram-xabarlar?refresh=1", follow_redirects=True)
        assert r.status_code == 200

    session = db_module.get_session()
    try:
        with db_module.unscoped():
            row = session.query(db_module.IgDmConversation).filter_by(external_id="old_conv_1").first()
        assert row is None, "ulanishdan OLDIN yangilangan suhbat 'Yangilash' orqali ham DBga import qilinmasligi kerak"
    finally:
        session.close()
    print("OK: 'Yangilash' tugmasi ham ig_dm_sync_since'dan oldingi suhbatlarni lokal ravishda chetlab o'tadi")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
