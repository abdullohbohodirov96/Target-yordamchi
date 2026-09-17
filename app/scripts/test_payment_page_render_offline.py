"""test_payment_page_render_offline.py — 2026-09, foydalanuvchi so'rovi
("to'lov jarayonini yaxshilash" -> "To'lov sahifasini soddalashtirish"):
`/tolov` (`templates/payment.html`) qayta tuzildi -- raqamlangan
bosqichlar o'rniga aniq sarlavhalar, Payme avtoto'lov (agar sozlangan
bo'lsa) "Tavsiya etiladi" belgisi bilan ustuvor ko'rsatiladi, qo'lda
to'lash + tasdiqlash bitta blokka birlashtirildi, va avtoto'lov
ALLAQACHON yoqilgan bo'lsa qo'lda to'lash bo'limi <details> ichida
yig'ilgan holda ko'rsatiladi.

Bu fayl TARMOQSIZ ravishda sahifa HAR BIR holatda (Payme sozlanmagan,
sozlangan-lekin-karta-yo'q, karta-tasdiqlash-kutilmoqda, avtoto'lov
tayyor) xatosiz (200) render bo'lishini va kutilgan asosiy elementlar
(masalan "Tavsiya etiladi" belgisi, "Qo'lda to'lash" bo'limi) to'g'ri
joyda paydo bo'lishini tekshiradi.

Ishga tushirish:
    cd app && python3 scripts/test_payment_page_render_offline.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")


def _fresh_modules(db_path, *, payme_merchant_id="", payme_test_key=""):
    for name in ("db", "app", "scheduler", "payme_subscribe", "kv_store", "orchestrator", "budget_tracker"):
        sys.modules.pop(name, None)
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["PAYME_MERCHANT_ID"] = payme_merchant_id
    os.environ["PAYME_TEST_KEY"] = payme_test_key
    os.environ["PAYME_TEST_MODE"] = "true"
    os.environ.pop("PAYME_KEY", None)
    os.environ.pop("PAYME_CARD_NUMBER", None)
    os.environ.pop("PAYME_CARD_HOLDER", None)
    os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"

    import db as db_module
    db_module.init_db()
    import app as app_module
    app_module.app.config["TESTING"] = True
    app_module.app.config["WTF_CSRF_ENABLED"] = False  # 2026-09, CSRF endi majburiy -- testlarda so'rovlar session-tashqarisida yasaladi
    return db_module, app_module


def _make_company(db_module, *, name, plan="start", card_token=None, card_masked=None, pending_token=None, autopay=True):
    session = db_module.get_session()
    try:
        c = db_module.Company(name=name, is_active=True, plan=plan)
        c.payme_autopay_enabled = autopay
        session.add(c)
        session.commit()
        if card_token:
            c.set_payme_card_token(card_token)
            c.payme_card_masked = card_masked or "860006******6311"
            session.commit()
        if pending_token:
            c.set_payme_card_pending_token(pending_token)
            session.commit()
        cid = c.id
        m = db_module.Manager(username=f"{name}_admin", full_name="A", role="admin", company_id=cid)
        m.set_password("parol123")
        session.add(m)
        session.commit()
        return cid, m.username
    finally:
        session.close()


def _login(client, username, password="parol123"):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=False)


def test_payment_page_renders_when_payme_not_configured():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_modules(os.path.join(tmp, "p1.db"))
        _cid, username = _make_company(db_module, name="Sozlanmagan")
        client = app_module.app.test_client()
        _login(client, username)
        r = client.get("/tolov")
        assert r.status_code == 200
        html = r.get_data(as_text=True).replace("&#39;", "'")
        assert "Avtomatik to'lov (Payme)" not in html, "Payme sozlanmagan bo'lsa avtoto'lov bo'limi umuman ko'rinmasligi kerak"
        assert "Qo'lda to'lash" in html
        assert "To'lov qildim" in html
        assert "<details" not in html, "Payme yo'q holatda yig'ma (details) bo'lim kerak emas -- yagona yo'l ochiq turishi kerak"
    print("OK: /tolov -- Payme sozlanmagan holatda faqat qo'lda to'lash bo'limi (yig'ilmagan) ko'rsatiladi")


def test_payment_page_shows_recommended_badge_when_card_not_bound():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_modules(os.path.join(tmp, "p2.db"), payme_merchant_id="M", payme_test_key="K")
        _cid, username = _make_company(db_module, name="Kartasiz")
        client = app_module.app.test_client()
        _login(client, username)
        r = client.get("/tolov")
        assert r.status_code == 200
        html = r.get_data(as_text=True).replace("&#39;", "'")
        assert "Tavsiya etiladi" in html
        assert "Kartani bog'lash" in html
        assert "Qo'lda to'lash" in html, "karta hali bog'lanmagan bo'lsa qo'lda to'lash yo'li ham OCHIQ ko'rinishi kerak"
        assert "<details" not in html, "avtoto'lov hali TAYYOR emas -- yig'ma bo'lim faqat ALLAQACHON yoqilganda kerak"
    print("OK: /tolov -- Payme sozlangan lekin karta bog'lanmagan holatda avtoto'lov 'Tavsiya etiladi' belgisi bilan ustuvor, qo'lda to'lash ham ochiq ko'rinadi")


def test_payment_page_collapses_manual_section_when_autopay_ready():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_modules(os.path.join(tmp, "p3.db"), payme_merchant_id="M", payme_test_key="K")
        _cid, username = _make_company(db_module, name="Tayyor", card_token="TOK-1", autopay=True)
        client = app_module.app.test_client()
        _login(client, username)
        r = client.get("/tolov")
        assert r.status_code == 200
        html = r.get_data(as_text=True).replace("&#39;", "'")
        assert "Tavsiya etiladi" not in html, "avtoto'lov ALLAQACHON tayyor bo'lsa, endi u o'zi tanlangan holat -- alohida belgi kerak emas"
        assert "Bog'langan karta" in html
        assert "<details" in html and "Qo'lda to'lash kerakmi?" in html, "avtoto'lov tayyor bo'lsa qo'lda to'lash IXTIYORIY (yig'ma) bo'lishi kerak"
        assert "To'lov qildim" in html
    print("OK: /tolov -- avtoto'lov ALLAQACHON yoqilgan bo'lsa, qo'lda to'lash bo'limi <details> ichida yig'ilgan holatda ko'rsatiladi")


def test_payment_page_renders_pending_card_verification_state():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_modules(os.path.join(tmp, "p4.db"), payme_merchant_id="M", payme_test_key="K")
        _cid, username = _make_company(db_module, name="Kutilmoqda", pending_token="PEND-1")
        client = app_module.app.test_client()
        _login(client, username)
        r = client.get("/tolov")
        assert r.status_code == 200
        html = r.get_data(as_text=True).replace("&#39;", "'")
        assert "SMS kod" in html
        assert "Tasdiqlash" in html
    print("OK: /tolov -- SMS kod tasdiqlash kutilayotgan holat ham xatosiz render bo'ladi")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
