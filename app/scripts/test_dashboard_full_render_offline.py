"""test_dashboard_full_render_offline.py — Dashboard ("/") sahifasini
TARMOQSIZ (offline) to'liq render qilib tekshiradi. Meta/Anthropic API'ga
umuman chaqiruv qilinmaydi (soxta ENV kalitlar bilan faqat import vaqtidagi
tekshiruvni o'tkazish uchun; get_kpis() haqiqiy tarmoqqa urinib ko'radi,
lekin bu xato try/except bilan tutiladi -- target_summary None qoladi,
Dashboard baribir 200 qaytarishi kerak).

2026-08, foydalanuvchi so'rovi: "dashboardga ham hamma malumotlani qoshib
qoygin, hamma narsani qosh, toliq malumotlar bolsin" asosida qo'shildi --
Dashboard'ga Target/SMM/Instagram DM qisqacha ko'rinishi qo'shildi. Bu test
tekshiradi:
  1. "target" moduliga ruxsati bor foydalanuvchi (admin) uchun sahifa 200
     qaytaradi va yangi bo'lim (SMM/DM kartalari) haqiqiy ma'lumot bilan
     to'g'ri render bo'ladi.
  2. "target" moduliga ruxsati YO'Q menejer uchun ham sahifa 200 qaytaradi
     va bu bo'lim UMUMAN ko'rinmaydi (Meta/SMM/DM so'rovlari yuborilmaydi).
  3. Rendered HTML'da tag balansi buzilmagan (yopilmagan/mos kelmagan teg
     yo'q) va sahifadagi barcha inline <script> bloklari JS sintaksisi
     bo'yicha to'g'ri (node --check).

Ishga tushirish:
    cd app && python3 scripts/test_dashboard_full_render_offline.py
"""

import os
import re
import subprocess
import sys
import tempfile
import datetime as dt
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMPDIR = tempfile.mkdtemp()
_DB_PATH = os.path.join(_TMPDIR, "test_dashboard.db")

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("META_ACCESS_TOKEN", "test-dummy-token")
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_test_dummy")
os.environ.setdefault("META_PAGE_ID", "page_test_dummy")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402
import permissions  # noqa: E402

app_module.app.config["TESTING"] = True
db_module.init_db()

_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


class _TagBalanceChecker(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.errors = []

    def handle_starttag(self, tag, attrs):
        if tag not in _VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        pass  # self-closing (<tag />) -- OK, doesn't push

    def handle_endtag(self, tag):
        if tag in _VOID_TAGS:
            return
        if not self.stack:
            self.errors.append(f"yopuvchi </{tag}> lekin ochiq teg yo'q")
            return
        if self.stack[-1] == tag:
            self.stack.pop()
        elif tag in self.stack:
            # Mos kelmagan -- ichkarida biror teg yopilmagan qolib ketgan
            self.errors.append(f"mos kelmagan teg: </{tag}> kutilganda ochiq stack: {self.stack[-3:]}")
            while self.stack and self.stack[-1] != tag:
                self.stack.pop()
            if self.stack:
                self.stack.pop()
        else:
            self.errors.append(f"yopuvchi </{tag}> hech qanday ochiq tegga mos kelmadi")


def _check_html(html: str, label: str):
    checker = _TagBalanceChecker()
    checker.feed(html)
    assert not checker.errors, f"[{label}] teg balans xatolari: {checker.errors}"
    assert not checker.stack, f"[{label}] yopilmagan teglar qoldi: {checker.stack}"

    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, re.S)
    for i, s in enumerate(scripts):
        if not s.strip() or "src=" in s[:5]:
            continue
        r = subprocess.run(["node", "--check", "-"], input=s, capture_output=True, text=True)
        assert r.returncode == 0, f"[{label}] script #{i} JS sintaksis xatosi: {r.stderr}"

    assert "None" not in re.sub(r"<script.*?</script>", "", html, flags=re.S), (
        f"[{label}] sahifada tashqariga chiqib qolgan literal 'None' matni bor -- "
        "ehtimol biror joyda Python None qiymati to'g'ridan-to'g'ri chiqarilgan"
    )


# --- Fixture: kompaniya, admin, menejer, va Target/SMM/DM uchun real ma'lumot
_session = db_module.get_session()
try:
    company1 = _session.query(db_module.Company).order_by(db_module.Company.id.asc()).first()
    assert company1 is not None and company1.id == 1

    admin = db_module.Manager(username="dash_admin", full_name="Admin", role="admin", company_id=1)
    admin.set_password("parol123")
    _session.add(admin)

    manager_no_target = db_module.Manager(
        username="dash_manager", full_name="Oddiy menejer", role="manager",
        company_id=1, allowed_modules=permissions.serialize_allowed_modules(["dashboard", "leads"]),  # "target" YO'Q
    )
    manager_no_target.set_password("parol123")
    _session.add(manager_no_target)
    _session.commit()

    now = dt.datetime.utcnow()
    lead = db_module.Lead(full_name="Test Mijoz", phone="+998901234567", status="new", created_at=now)
    _session.add(lead)
    _session.commit()
    _session.add(db_module.Sale(lead_id=lead.id, amount=500000.0, sold_at=now, is_returned=False))

    _session.add(db_module.SmmSnapshot(platform="instagram", date=now.strftime("%Y-%m-%d"), followers_count=1200, media_count=40))
    _session.add(db_module.SmmPost(
        platform="instagram", external_id="ig_dash_1", media_type="REEL", posted_at=now,
        like_count=300, comments_count=25, shares_count=10, saved_count=40, follows_count=5,
        reach=9000, impressions=15000,
    ))

    _session.add(db_module.IgDmConversation(
        external_id="conv_1", customer_ig_id="u1", customer_username="mijoz1",
        message_count=2, last_message_at=now,
        last_message_from="customer", last_message_text="Salom, narxi qancha?",
        is_unanswered=True, ai_lead_quality="hot",
    ))
    _session.commit()
finally:
    _session.close()


def _login(client, username, password="parol123"):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=False)


def test_admin_sees_full_dashboard_with_target_smm_dm_summary():
    with app_module.app.test_client() as client:
        r = _login(client, "dash_admin")
        assert r.status_code == 302, f"login muvaffaqiyatsiz: {r.status_code}"
        r2 = client.get("/", follow_redirects=False)
        assert r2.status_code == 200, f"Dashboard 200 qaytarishi kerak, olindi: {r2.status_code}"
        html = r2.get_data(as_text=True)
        _check_html(html, "dashboard (admin)")

        assert "SMM" in html and "Instagram" in html, "SMM/Instagram qisqacha bo'limi ko'rinmadi"
        assert "Instagram xabarlar (DM)" in html, "Instagram DM qisqacha kartasi ko'rinmadi"
        assert "Jami suhbatlar" in html and "1" in html, "DM statistikasi (jami suhbatlar) ko'rinmadi"
        assert "mijoz1" not in html, "Dashboard'da individual DM matni ko'rsatilmasligi kerak (faqat statistika)"
    print("OK: admin uchun Dashboard'da Target/SMM/Instagram DM qisqacha bo'limi to'g'ri render bo'ladi, HTML tag balansi va JS sintaksisi toza")


def test_manager_without_target_module_does_not_see_summary_section():
    with app_module.app.test_client() as client:
        r = _login(client, "dash_manager")
        assert r.status_code == 302, f"login muvaffaqiyatsiz: {r.status_code}"
        r2 = client.get("/", follow_redirects=False)
        assert r2.status_code == 200, f"Dashboard 200 qaytarishi kerak, olindi: {r2.status_code}"
        html = r2.get_data(as_text=True)
        _check_html(html, "dashboard (manager, target moduli yo'q)")

        assert "Target / SMM / Instagram" not in html, (
            "'target' moduliga ruxsati yo'q menejerga bu bo'lim UMUMAN ko'rinmasligi kerak"
        )
    print("OK: 'target' moduliga ruxsati yo'q menejer uchun yangi bo'lim ko'rinmaydi, sahifa baribir 200 qaytaradi")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
