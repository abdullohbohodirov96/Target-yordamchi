"""test_page_subnav_offline.py — 2026-09, foydalanuvchi so'rovi: "side bar
niyam chiroylrioq qilish kerak ... misol crm ichiga kirsa tepada yana
bolimlar tursin shunaqa qil tekshir ideal qil" -- CRM (yoki boshqa guruh)
ichidagi sahifada bo'lganda, o'sha guruhning qolgan bandlari sahifa
TEPASIDA gorizontal "tab" qatorida (.page-subnav, base.html) ham
ko'rinishini tekshiradi.

Tekshiradi:
  - Lidlar sahifasida (CRM guruhi) top-subnav ko'rinadi, "Lidlar" band
    active, "Sotilgan xaridorlar"/"Qayta aloqa" ham ko'rinadi.
  - Sozlamalar sahifasida (Boshqaruv guruhi) mos bandlar ko'rinadi.
  - Dashboard'da (hech qanday guruhga tegishli emas) top-subnav umuman
    chiqmaydi.
  - "target" moduliga ruxsati yo'q menejerga Marketing guruhi subnav'i
    chiqmaydi (has_module tekshiruvi saqlanган).
  - Rendered HTML tag balansi buzilmagan.

Ishga tushirish:
    cd app && python3 scripts/test_page_subnav_offline.py
"""
import os
import re
import sys
import tempfile
import datetime as dt
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMPDIR = tempfile.mkdtemp()
_DB_PATH = os.path.join(_TMPDIR, "test_subnav.db")

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
        pass

    def handle_endtag(self, tag):
        if tag in _VOID_TAGS:
            return
        if not self.stack:
            self.errors.append(f"yopuvchi </{tag}> lekin ochiq teg yo'q")
            return
        if self.stack[-1] == tag:
            self.stack.pop()
        elif tag in self.stack:
            self.errors.append(f"mos kelmagan teg: </{tag}>")
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


_session = db_module.get_session()
try:
    company1 = _session.query(db_module.Company).order_by(db_module.Company.id.asc()).first()
    assert company1 is not None and company1.id == 1

    admin = db_module.Manager(username="subnav_admin", full_name="Admin", role="admin", company_id=1)
    admin.set_password("parol123")
    _session.add(admin)

    manager_no_target = db_module.Manager(
        username="subnav_manager", full_name="Oddiy menejer", role="manager",
        company_id=1, allowed_modules=permissions.serialize_allowed_modules(["dashboard", "leads", "settings"]),
    )
    manager_no_target.set_password("parol123")
    _session.add(manager_no_target)
    _session.commit()

    now = dt.datetime.utcnow()
    lead = db_module.Lead(full_name="Subnav Mijoz", phone="+998901234567", status="new", created_at=now)
    _session.add(lead)
    _session.commit()
finally:
    _session.close()


def _login(client, username, password="parol123"):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=False)


def test_crm_page_shows_top_subnav_with_active_tab():
    with app_module.app.test_client() as client:
        r = _login(client, "subnav_admin")
        assert r.status_code == 302
        r2 = client.get("/leads", follow_redirects=False)
        assert r2.status_code == 200, r2.status_code
        html = r2.get_data(as_text=True)
        _check_html(html, "leads_list")
        assert 'class="page-subnav"' in html, "CRM sahifasida top-subnav ko'rinmadi"
        assert re.search(r'class="page-subnav-item active">.*?<span>Lidlar</span>', html, re.S), (
            "'Lidlar' bandi active holatda ko'rsatilmadi"
        )
        assert "Sotilgan xaridorlar" in html and "Qayta aloqa" in html
    print("OK: CRM sahifasida (Lidlar) top-subnav to'g'ri ko'rsatiladi, 'Lidlar' active")


def test_settings_page_shows_boshqaruv_subnav():
    with app_module.app.test_client() as client:
        r = _login(client, "subnav_admin")
        assert r.status_code == 302
        r2 = client.get("/sozlamalar", follow_redirects=False)
        assert r2.status_code == 200, r2.status_code
        html = r2.get_data(as_text=True)
        _check_html(html, "settings_hub")
        assert 'class="page-subnav"' in html
        assert "Sozlamalar" in html and "Menejerlar" in html and "Kompaniyalar" in html
    print("OK: Boshqaruv sahifasida (Sozlamalar) top-subnav to'g'ri guruh bandlarini ko'rsatadi")


def test_dashboard_has_no_subnav():
    with app_module.app.test_client() as client:
        r = _login(client, "subnav_admin")
        assert r.status_code == 302
        r2 = client.get("/", follow_redirects=False)
        assert r2.status_code == 200, r2.status_code
        html = r2.get_data(as_text=True)
        _check_html(html, "dashboard")
        assert 'class="page-subnav"' not in html, "Dashboard hech qanday guruhga tegishli emas -- subnav chiqmasligi kerak"
    print("OK: Dashboard'da (guruhga tegishli bo'lmagan sahifa) top-subnav umuman ko'rinmaydi")


def test_manager_without_module_permission_does_not_see_that_group_subnav():
    with app_module.app.test_client() as client:
        r = _login(client, "subnav_manager")
        assert r.status_code == 302
        r2 = client.get("/sozlamalar", follow_redirects=False)
        assert r2.status_code == 200, r2.status_code
        html = r2.get_data(as_text=True)
        _check_html(html, "settings_hub (manager)")
        subnav_match = re.search(r'<nav class="page-subnav">.*?</nav>', html, re.S)
        assert subnav_match, "Sozlamalar sahifasida top-subnav ko'rinmadi"
        subnav_html = subnav_match.group(0)
        assert "Menejerlar" not in subnav_html and "Kompaniyalar" not in subnav_html, (
            "Oddiy menejerga admin-only bandlar (Menejerlar/Kompaniyalar) subnav'da ham ko'rinmasligi kerak"
        )
        assert "Sozlamalar" in subnav_html
    print("OK: 'target' moduliga/admin huquqiga ega bo'lmagan menejerga tegishli bandlar subnav'da ham yashirin")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
