"""test_telegram_deeplink_connect_offline.py — 2026-09, foydalanuvchi
so'rovi: "telegram ulashni bosgandan keyin, avtomaticheskiy telegramga
kirib, botga kirib startni bossa, ikkita akkaunt, admin ul ham qolsin,
hisobotni olish uchun va undan tashqari guruhga qo'shish ham to'g'irlab,
yo'lini qil, oson yo'l guruhga qo'shish yo'li bo'lsin."

TARMOQSIZ (offline) tekshiradi:
  1. `/connect-accounts/telegram/personal-link` va `.../group-link`
     tugmalari bir martalik token yaratib, to'g'ri `t.me/<bot>?start=...`
     / `?startgroup=...` chuqur havolasiga yo'naltiradi.
  2. `/api/webhook` orqali kelgan `/start <token>` shaxsiy chatda
     `Manager.telegram_user_id`ni, guruh chatida `Company.
     telegram_group_id`ni AVTOMATIK bog'laydi -- foydalanuvchi hech
     qanday ID ko'chirib-joylashtirmaydi.
  3. Noto'g'ri turdagi chatdan (masalan shaxsiy token guruhdan, yoki
     aksincha) kelgan urinish RAD ETILADI, hech narsa o'zgarmaydi.
  4. Token BIR MARTALIK -- ishlatilgandan keyin qayta ishlatib bo'lmaydi.
  5. Noma'lum/eskirgan token oddiy /start oqimiga (xatosiz) tushib ketadi.
  6. Tugmalar FAQAT admin uchun ochiq (oddiy menejer uchun emas).

Ishga tushirish:
    cd app && python3 scripts/test_telegram_deeplink_connect_offline.py
"""

import os
import re
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")


def _fresh_modules(db_path):
    for name in ("db", "kv_store", "app", "budget_tracker", "orchestrator", "meta_api", "scheduler", "lead_sync", "meta_events", "monthly_report", "ig_dm_sync", "dashboard_data"):
        sys.modules.pop(name, None)
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"
    import db as db_module
    db_module.init_db()
    import app as app_module
    app_module.app.config["TESTING"] = True
    app_module._BOT_IDENTITY_CACHE.clear()
    app_module._BOT_IDENTITY_CACHE["id"] = 999
    app_module._BOT_IDENTITY_CACHE["username"] = "targetolog_bot"
    return db_module, app_module


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


# ---------------------------------------------------------------------------
# 1) Tugmalar to'g'ri chuqur havolaga yo'naltiradi
# ---------------------------------------------------------------------------

def test_personal_link_button_creates_token_and_redirects_to_start_deeplink():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_modules(os.path.join(tmp, "s1.db"))
        client = app_module.app.test_client()
        _signup(client, company_name="Shaxsiy MChJ", admin_username="shaxsiy_admin")

        r = client.post("/connect-accounts/telegram/personal-link")
        assert r.status_code == 302
        location = r.headers["Location"]
        assert location.startswith("https://t.me/targetolog_bot?start="), location
        token = _extract_token(location, param="start")

        data = app_module.kv_store.get_json(f"tg_link_token:{token}")
        assert data["kind"] == "personal"
        assert data["manager_id"] is not None
        assert data["company_id"] is not None
    print("OK: 'Telegramda shaxsan ulash' tugmasi to'g'ri t.me/<bot>?start=<token> havolasiga yo'naltiradi, token kv_store'da to'g'ri saqlanadi")


def test_group_link_button_creates_token_and_redirects_to_startgroup_deeplink():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_modules(os.path.join(tmp, "s2.db"))
        client = app_module.app.test_client()
        _signup(client, company_name="Guruh MChJ", admin_username="guruh_admin2")

        r = client.post("/connect-accounts/telegram/group-link")
        assert r.status_code == 302
        location = r.headers["Location"]
        assert location.startswith("https://t.me/targetolog_bot?startgroup="), location
        token = _extract_token(location, param="startgroup")

        data = app_module.kv_store.get_json(f"tg_link_token:{token}")
        assert data["kind"] == "group"
        assert data["company_id"] is not None
        assert "manager_id" not in data
    print("OK: 'Guruhga qo'shish' tugmasi to'g'ri t.me/<bot>?startgroup=<token> havolasiga yo'naltiradi, token kv_store'da to'g'ri saqlanadi")


def test_link_buttons_hidden_when_bot_not_configured():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_modules(os.path.join(tmp, "s3.db"))
        app_module._BOT_IDENTITY_CACHE.clear()  # bot username hali noma'lum (getMe muvaffaqiyatsiz bo'lgandek)
        client = app_module.app.test_client()
        _signup(client, company_name="Botsiz MChJ", admin_username="botsiz_admin")

        with mock.patch.object(app_module, "_get_bot_identity", return_value={}):
            html = client.get("/connect-accounts").get_data(as_text=True)
            assert "Telegramda shaxsan ulash" not in html
            assert "Guruhga qo'shish (bir tugma)" not in html

            r = client.post("/connect-accounts/telegram/personal-link", follow_redirects=True)
            assert "sozlanmagan" in r.get_data(as_text=True)
    print("OK: bot sozlanmagan bo'lsa, bir-tugma Ulash tugmalari sahifada ko'rsatilmaydi va route xavfsiz rad etadi")


# ---------------------------------------------------------------------------
# 2)-4) Webhook orqali token iste'mol qilish
# ---------------------------------------------------------------------------

def test_personal_start_deeplink_links_manager_via_private_chat():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_modules(os.path.join(tmp, "s4.db"))
        client = app_module.app.test_client()
        _signup(client, company_name="Shaxsiy2 MChJ", admin_username="shaxsiy2_admin")

        r = client.post("/connect-accounts/telegram/personal-link")
        token = _extract_token(r.headers["Location"], param="start")

        sent = []
        with mock.patch.object(app_module, "tg_send", side_effect=lambda cid, text: sent.append((cid, text))):
            resp = client.post("/api/webhook", json={
                "message": {
                    "chat": {"id": 777001, "type": "private"},
                    "text": f"/start {token}",
                    "from": {"id": 777001},
                }
            })
        assert resp.status_code == 200
        assert len(sent) == 1 and sent[0][0] == 777001
        assert "shaxsan ulandi" in sent[0][1]

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                manager = session.query(db_module.Manager).filter_by(username="shaxsiy2_admin").first()
            assert manager.telegram_user_id == "777001"
        finally:
            session.close()

        # Token bir martalik -- qayta ishlatib bo'lmaydi.
        assert app_module.kv_store.get_json(f"tg_link_token:{token}") is None
    print("OK: shaxsiy chatda /start <token> Manager.telegram_user_id'ni avtomatik bog'laydi, tasdiqlash xabari yuboriladi, token bir martalik")


def test_personal_start_deeplink_rejected_from_group_chat():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_modules(os.path.join(tmp, "s5.db"))
        client = app_module.app.test_client()
        _signup(client, company_name="Shaxsiy3 MChJ", admin_username="shaxsiy3_admin")

        r = client.post("/connect-accounts/telegram/personal-link")
        token = _extract_token(r.headers["Location"], param="start")

        sent = []
        with mock.patch.object(app_module, "tg_send", side_effect=lambda cid, text: sent.append((cid, text))):
            client.post("/api/webhook", json={
                "message": {
                    "chat": {"id": -500222, "type": "group"},
                    "text": f"/start {token}",
                    "from": {"id": 1},
                }
            })
        assert len(sent) == 1
        assert "shaxsiy ulash havolasi" in sent[0][1]

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                manager = session.query(db_module.Manager).filter_by(username="shaxsiy3_admin").first()
            assert manager.telegram_user_id is None, "Guruhdan kelgan shaxsiy token HECH NARSANI o'zgartirmasligi kerak"
        finally:
            session.close()
    print("OK: shaxsiy ulash tokeni guruh chatidan kelsa rad etiladi, hech narsa o'zgarmaydi")


def test_group_start_deeplink_links_company_group_via_group_chat():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_modules(os.path.join(tmp, "s6.db"))
        client = app_module.app.test_client()
        _signup(client, company_name="Guruh2 MChJ", admin_username="guruh2_admin")

        r = client.post("/connect-accounts/telegram/group-link")
        token = _extract_token(r.headers["Location"], param="startgroup")

        sent = []
        with mock.patch.object(app_module, "tg_send", side_effect=lambda cid, text: sent.append((cid, text))):
            resp = client.post("/api/webhook", json={
                "message": {
                    "chat": {"id": -500333, "type": "supergroup"},
                    "text": f"/start {token}",
                    "from": {"id": 1},
                }
            })
        assert resp.status_code == 200
        assert len(sent) == 1 and sent[0][0] == -500333
        assert "guruh" in sent[0][1].lower()

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                company = session.query(db_module.Company).filter_by(name="Guruh2 MChJ").first()
            assert company.telegram_group_id == "-500333"
        finally:
            session.close()
    print("OK: guruh chatida /start <token> Company.telegram_group_id'ni avtomatik bog'laydi, tasdiqlash xabari yuboriladi")


def test_group_start_deeplink_rejected_from_private_chat():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_modules(os.path.join(tmp, "s7.db"))
        client = app_module.app.test_client()
        _signup(client, company_name="Guruh3 MChJ", admin_username="guruh3_admin")

        r = client.post("/connect-accounts/telegram/group-link")
        token = _extract_token(r.headers["Location"], param="startgroup")

        sent = []
        with mock.patch.object(app_module, "tg_send", side_effect=lambda cid, text: sent.append((cid, text))):
            client.post("/api/webhook", json={
                "message": {
                    "chat": {"id": 888444, "type": "private"},
                    "text": f"/start {token}",
                    "from": {"id": 888444},
                }
            })
        assert len(sent) == 1
        assert "guruhga qo'shish havolasi" in sent[0][1]

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                company = session.query(db_module.Company).filter_by(name="Guruh3 MChJ").first()
            assert company.telegram_group_id is None
        finally:
            session.close()
    print("OK: guruh tokeni shaxsiy chatdan kelsa rad etiladi, Company.telegram_group_id o'zgarmaydi")


def test_unknown_token_falls_back_to_normal_start_flow():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_modules(os.path.join(tmp, "s8.db"))
        client = app_module.app.test_client()
        _signup(client, company_name="Notokenli MChJ", admin_username="notokenli_admin")

        sent = []
        with mock.patch.object(app_module, "tg_send", side_effect=lambda cid, text: sent.append((cid, text))):
            client.post("/api/webhook", json={
                "message": {
                    "chat": {"id": 999888, "type": "private"},
                    "text": "/start mavjud_bolmagan_token_123",
                    "from": {"id": 999888},
                }
            })
        assert len(sent) == 1
        # Notanish chat -- oddiy ro'yxatdan o'tish yo'riqnomasi ko'rsatiladi (xato tashlanmaydi).
        assert sent[0][1] == app_module._REGISTER_HELP_TEXT
    print("OK: noma'lum/yaroqsiz token bilan /start xatosiz -- oddiy /start oqimiga tushib ketadi")


def test_reused_token_only_works_once():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_modules(os.path.join(tmp, "s9.db"))
        client = app_module.app.test_client()
        _signup(client, company_name="Qayta MChJ", admin_username="qayta_admin")

        r = client.post("/connect-accounts/telegram/personal-link")
        token = _extract_token(r.headers["Location"], param="start")

        sent = []
        with mock.patch.object(app_module, "tg_send", side_effect=lambda cid, text: sent.append((cid, text))):
            client.post("/api/webhook", json={
                "message": {"chat": {"id": 111222, "type": "private"}, "text": f"/start {token}", "from": {"id": 111222}}
            })
            # Boshqa birov (yoki hatto o'zi) shu tokenni IKKINCHI marta yubordi.
            client.post("/api/webhook", json={
                "message": {"chat": {"id": 333444, "type": "private"}, "text": f"/start {token}", "from": {"id": 333444}}
            })
        assert len(sent) == 2
        assert "shaxsan ulandi" in sent[0][1]
        # Ikkinchi urinish token allaqachon iste'mol qilingani uchun ODDIY (notanish chat) oqimga tushadi.
        assert sent[1][1] == app_module._REGISTER_HELP_TEXT

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                manager = session.query(db_module.Manager).filter_by(username="qayta_admin").first()
            assert manager.telegram_user_id == "111222", "Ikkinchi (token allaqachon ishlatilgan) urinish birinchi bog'lanishni O'ZGARTIRMASLIGI kerak"
        finally:
            session.close()
    print("OK: bir martalik token ikkinchi marta ishlatilganda hech narsani o'zgartirmaydi -- birinchi bog'lanish saqlanib qoladi")


# ---------------------------------------------------------------------------
# 6) Faqat admin
# ---------------------------------------------------------------------------

def test_link_routes_require_admin():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, app_module = _fresh_modules(os.path.join(tmp, "s10.db"))
        client = app_module.app.test_client()
        _signup(client, company_name="Admin Talab MChJ", admin_username="talab_admin")

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                admin = session.query(db_module.Manager).filter_by(username="talab_admin").first()
                company_id = admin.company_id
            manager = db_module.Manager(
                company_id=company_id, full_name="Oddiy menejer", username="talab_manager",
                role="manager",
            )
            manager.set_password("parol123456")
            session.add(manager)
            session.commit()
        finally:
            session.close()

        client.get("/logout", follow_redirects=True)
        client.post("/login", data={"username": "talab_manager", "password": "parol123456"}, follow_redirects=True)

        r1 = client.post("/connect-accounts/telegram/personal-link", follow_redirects=True)
        assert "faqat admin uchun" in r1.get_data(as_text=True)
        r2 = client.post("/connect-accounts/telegram/group-link", follow_redirects=True)
        assert "faqat admin uchun" in r2.get_data(as_text=True)
    print("OK: shaxsiy/guruh Ulash tugmalari oddiy menejer uchun emas, faqat admin uchun ochiq")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
