"""test_companies_managers_modal_offline.py — Kompaniyalar/Menejerlar
sahifalaridagi "Yangi qo'shish" formasi endi doim ko'rinib turgan karta
EMAS, balki tugma bosilganda ochiladigan MODAL DIALOG (2026-09, foydalanuvchi
so'rovi: "kompaniyalar yaratish oynasini chiroyliro qil unaqa turmasin,
tugmani bossa oyna ochilsin to'liq ekranga, xudda shunaqa muammo managerlar
oynasidayam"). Bu test TARMOQSIZ tekshiradi:
  1. Ikkala sahifa ham 200 qaytaradi va modal-dialog HTML belgilari mavjud.
  2. Rendered HTML'da teg balansi buzilmagan.
  3. Har ikkala sahifadagi inline <script> bloklari JS sintaksisi bo'yicha
     to'g'ri (node --check).
  4. Kompaniya yaratishda validatsiya xatosi bo'lsa (masalan bo'sh nom),
     modal AVTOMATIK ochiq holatda qaytariladi (foydalanuvchi xatoni
     ko'rish uchun formani qayta ochishi shart emas).

Ishga tushirish:
    cd app && python3 scripts/test_companies_managers_modal_offline.py
"""

import os
import re
import subprocess
import sys
import tempfile
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMPDIR = tempfile.mkdtemp()
_DB_PATH = os.path.join(_TMPDIR, "test_modal.db")

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("META_ACCESS_TOKEN", "test-dummy-token")
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_test_dummy")
os.environ.setdefault("META_PAGE_ID", "page_test_dummy")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402

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

    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, re.S)
    for i, s in enumerate(scripts):
        if not s.strip() or "src=" in s[:5]:
            continue
        r = subprocess.run(["node", "--check", "-"], input=s, capture_output=True, text=True)
        assert r.returncode == 0, f"[{label}] script #{i} JS sintaksis xatosi: {r.stderr}"


_session = db_module.get_session()
try:
    owner = db_module.Manager(username="modal_test_owner", full_name="Owner", role="admin", company_id=1)
    owner.set_password("parol123")
    _session.add(owner)
    _session.commit()
finally:
    _session.close()


def _login(client, username="modal_test_owner", password="parol123"):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=False)


def test_companies_page_has_modal_markup_and_is_valid():
    with app_module.app.test_client() as client:
        _login(client)
        r = client.get("/companies")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert 'id="newCompanyModal"' in html and "modal-overlay" in html, "modal HTML mavjud bo'lishi kerak"
        assert 'id="newCompanyBtn"' in html, "'Yangi kompaniya' tugmasi mavjud bo'lishi kerak"
        assert 'newCompanyModal' in html and ' open"' not in html.split('id="newCompanyModal"')[0][-40:], (
            "GET so'rovida modal boshida OCHIQ bo'lmasligi kerak"
        )
        _check_html(html, "companies")
    print("OK: /companies sahifasida modal dialog HTML to'g'ri, teg balansi va JS sintaksisi toza")


def test_managers_page_has_modal_markup_and_is_valid():
    with app_module.app.test_client() as client:
        _login(client)
        r = client.get("/managers")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert 'id="newManagerModal"' in html and "modal-overlay" in html
        assert 'id="newManagerBtn"' in html
        _check_html(html, "managers")
    print("OK: /managers sahifasida modal dialog HTML to'g'ri, teg balansi va JS sintaksisi toza")


def test_company_validation_error_reopens_modal():
    with app_module.app.test_client() as client:
        _login(client)
        r = client.post("/companies", data={"name": "", "email": "", "plan": "trial"}, follow_redirects=True)
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "bo&#39;sh bo&#39;lishi mumkin emas" in html or "bo'sh bo'lishi mumkin emas" in html, (
            "validatsiya xato xabari ko'rinishi kerak"
        )
        assert 'class="modal-overlay open"' in html, (
            "validatsiya xatosidan keyin modal AVTOMATIK ochiq holatda qaytishi kerak "
            "(aks holda foydalanuvchi xatoni ko'rmay, qayta tugma bosishi kerak bo'lardi)"
        )
    print("OK: kompaniya yaratishda validatsiya xatosi bo'lsa, modal avtomatik ochiq qaytariladi")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
