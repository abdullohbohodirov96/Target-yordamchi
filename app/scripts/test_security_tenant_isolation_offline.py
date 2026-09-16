"""test_security_tenant_isolation_offline.py -- XAVFSIZLIK: kompaniyalar
orasida ma'lumot ARALASHMASLIGINI (IDOR / kross-tenant) HAQIQIY Flask
marshrutlar orqali, LOKAL (tarmoqsiz, vaqtinchalik SQLite) "hujum-sinovi".

2026-09, foydalanuvchi so'rovi: "xavfsizlikka qattiq qara, loyihani to'liq
penetration test qil ... ma'lumotlar kompaniyalar orasida aralashib
ketmasin, adminga to'liq dostup bo'lsin".

USUL (production'ga tegilmaydi -- faqat shu yerdagi kod bazasi):
  * Uchta kompaniya: #1 (platforma egasi), A va B. Har birida admin +
    oddiy menejer, va B'da har bir kompaniyaga tegishli jadvaldan
    kamida bittadan qator (Lead/Sale/LeadNote, CampaignDraft+Media,
    CreativeAsset, CompanyBrandKit, Competitor, IgDmConversation,
    CannedReply, CustomField, FunnelStage, StandingTask/Report,
    AssistantUnanswered, CallRecord). B'ning HAR BIR matn maydonida
    noyob "marker" satr bor (`B_MARKER`) -- javob HTML/JSON'ida shu satr
    ko'rinsa, bu sizib chiqish.
  * A kompaniyasining ADMINI (va alohida oddiy MENEJERI) sifatida kirib,
    B'ning HAQIQIY ID'lari bilan ID'li HAR BIR marshrutga (URL'dagi
    <int:id> ham, forma/JSON tanasidagi id ham) GET/POST yuboriladi.
  * Har bir urinishdan keyin: (1) javobda B markeri YO'Q; (2) B'ning
    BARCHA tenant-jadval qatorlari (to'liq snapshot, `db.unscoped()` bilan
    olingan) va B `Company` qatori O'ZGARMAGAN; (3) tashqi (Meta) yuborish
    funksiyalari chaqirilMAGAN.
  * Platforma-egasi marshrutlari (`/companies/*`) A admini uchun YOPIQ
    ekani ham shu yerda tekshiriladi; va IJOBIY nazorat (B admini o'z
    lidini ko'radi, A admini o'z sahifalarini ochadi) -- marker/aniqlash
    mexanizmi ishlayotganini isbotlash uchun.

Bu fayl -- dalil: yangi marshrut qo'shilganda shu ro'yxatga qo'shing.

Ishga tushirish:
    cd app && python3 scripts/test_security_tenant_isolation_offline.py
"""

import io
import json
import os
import sys
import tempfile
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("OPENAI_API_KEY", "test-dummy-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-dummy-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-dummy-token")
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_test_dummy")
os.environ.setdefault("META_PAGE_ID", "page_test_dummy")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
_TMPDIR = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMPDIR, 'security_audit.db')}"

import datetime as dt  # noqa: E402

import requests  # noqa: E402


def _offline(*_a, **_k):
    raise requests.ConnectionError("offline test -- tarmoq ATAYLAB o'chirilgan")


# Butun test tarmoqsiz: Meta/Telegram/OpenAI'ga har qanday urinish darhol
# ConnectionError (proxy/timeout kutmasdan) -- ilova bu xatoni o'zi yutadi.
for _name in ("get", "post", "put", "delete", "request"):
    setattr(requests, _name, _offline)
requests.Session.request = _offline  # type: ignore[assignment]

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402
import campaign_media  # noqa: E402
import creative_studio  # noqa: E402
import meta_api  # noqa: E402

meta_api._retry_sleep = lambda attempt: None  # tarmoq xatosida 1s/2s kutish -- testda keraksiz

app_module.app.config["TESTING"] = True
app_module.app.config["WTF_CSRF_ENABLED"] = False  # brauzersiz test -- CSRF alohida (Flask-WTF) sinovdan o'tgan
db_module.init_db()
campaign_media.MEDIA_ROOT = Path(_TMPDIR) / "ad_media"
creative_studio.CREATIVE_ROOT = Path(_TMPDIR) / "creatives"
creative_studio.BRAND_ROOT = Path(_TMPDIR) / "brand"

B_MARKER = "ZZ_SECRET_B_MARKER_ZZ"
PW = "parol123"

failures: list[str] = []
checked_routes: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("OK:   " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ---------------------------------------------------------------------------
# Urug'lantirish
# ---------------------------------------------------------------------------
FAR_FUTURE = dt.datetime.utcnow() + dt.timedelta(days=3650)


def _seed_company(session, name: str, marker: str) -> dict:
    c = db_module.Company(
        name=f"{name} {marker}", plan="unlimited", is_active=True, paid_until=FAR_FUTURE,
        email=f"{name.lower()}@test.uz",
        meta_access_token=db_module.crypto_util.encrypt_token(f"tok_{name}"),
        meta_ad_account_id=f"act_{name}", meta_page_id=f"page_{name}",
        meta_integration_status="connected",
    )
    session.add(c)
    session.commit()
    cid = c.id
    admin = db_module.Manager(username=f"{name.lower()}_admin", full_name=f"{name} admin {marker}", role="admin", company_id=cid)
    admin.set_password(PW)
    mgr = db_module.Manager(username=f"{name.lower()}_manager", full_name=f"{name} manager {marker}", role="manager", company_id=cid,
                            allowed_modules=json.dumps(["dashboard", "leads", "target", "settings", "individual_check", "analytics"]))
    mgr.set_password(PW)
    session.add_all([admin, mgr])
    session.commit()
    db_module.seed_default_funnel_stages_for_company(cid)

    lead = db_module.Lead(company_id=cid, full_name=f"Lead {marker}", phone=f"+99890{cid}000001", source="manual",
                          status="new", assigned_manager_id=mgr.id)
    session.add(lead)
    session.commit()
    session.add(db_module.LeadNote(company_id=cid, lead_id=lead.id, text=f"Izoh {marker}", manager_id=mgr.id))
    sale = db_module.Sale(company_id=cid, lead_id=lead.id, amount=1234567.0, manager_id=mgr.id, invoice_number=f"INV-{marker}")
    session.add(sale)
    draft = db_module.CampaignDraft(company_id=cid, created_by_manager_id=admin.id, title=f"Draft {marker}", source="MANUAL",
                                    status="draft", objective="MESSAGES", state_json=json.dumps({"campaign": {"name": f"Camp {marker}"}}))
    session.add(draft)
    session.commit()
    media = db_module.CampaignDraftMedia(company_id=cid, draft_id=draft.id, kind="image", storage_path=f"{cid}/{draft.id}/m.png",
                                         filename=f"media_{marker}.png", content_type="image/png", upload_status="pending")
    session.add(media)
    asset = db_module.CreativeAsset(company_id=cid, created_by_manager_id=admin.id, title=f"Asset {marker}", kind="ai_generated",
                                    status="collecting_brief", aspect="1:1", brief_conversation_json=json.dumps([{"role": "assistant", "content": f"Q {marker}"}]))
    session.add(asset)
    kit = db_module.CompanyBrandKit(company_id=cid, primary_color="#112233", preferred_styles=json.dumps([f"style_{marker}"]))
    session.add(kit)
    comp = db_module.Competitor(company_id=cid, name=f"Competitor {marker}", is_active=True)
    session.add(comp)
    conv = db_module.IgDmConversation(company_id=cid, external_id=f"conv_{cid}_{marker}", channel="instagram", customer_ig_id=f"ig_{cid}",
                                      customer_username=f"customer_{marker}", last_message_text=f"DM {marker}", last_message_from="customer", message_count=1, is_unanswered=True)
    session.add(conv)
    session.commit()
    session.add(db_module.IgDmMessage(company_id=cid, conversation_id=conv.id, sender="customer", text=f"DM {marker}"))
    session.add(db_module.CannedReply(company_id=cid, title=f"Tpl {marker}", text=f"TplText {marker}", sort_order=0))
    session.add(db_module.CustomField(company_id=cid, key=f"field_{cid}", label=f"Field {marker}", field_type="text", sort_order=0, is_active=True))
    session.add(db_module.StandingTask(company_id=cid, chat_id="123", object_id=f"obj_{cid}", object_name=f"Task {marker}", on_time="09:00", off_time="18:00", is_active=True))
    session.add(db_module.StandingReport(company_id=cid, chat_id="123", time_hhmm="10:00", is_active=True))
    session.add(db_module.AssistantUnanswered(company_id=cid, question=f"Savol {marker}", origin="web", manager_name=f"M {marker}"))
    session.add(db_module.CallRecord(company_id=cid, external_id=f"call_{cid}", manager_id=mgr.id, lead_id=lead.id, duration_seconds=10,
                                     phone_number=lead.phone, recording_url=f"https://example.invalid/{marker}.mp3"))
    session.commit()
    stage = session.query(db_module.FunnelStage).filter_by(company_id=cid).order_by(db_module.FunnelStage.sort_order.asc()).first()
    task = session.query(db_module.StandingTask).filter_by(company_id=cid).first()
    report = session.query(db_module.StandingReport).filter_by(company_id=cid).first()
    field = session.query(db_module.CustomField).filter_by(company_id=cid).first()
    tpl = session.query(db_module.CannedReply).filter_by(company_id=cid).first()
    call = session.query(db_module.CallRecord).filter_by(company_id=cid).first()
    unanswered = session.query(db_module.AssistantUnanswered).filter_by(company_id=cid).first()
    return {
        "id": cid, "admin": admin.username, "manager": mgr.username, "admin_id": admin.id, "manager_id": mgr.id,
        "lead": lead.id, "sale": sale.id, "draft": draft.id, "media": media.id, "asset": asset.id, "kit": kit.id,
        "competitor": comp.id, "conv": conv.id, "tpl": tpl.id, "field": field.id, "stage": stage.id,
        "task": task.id, "report": report.id, "unanswered": unanswered.id, "call": call.id,
    }


_s = db_module.get_session()
try:
    with db_module.unscoped():
        c1 = _s.query(db_module.Company).order_by(db_module.Company.id.asc()).first()
        assert c1 is not None and c1.id == 1
        c1.paid_until = None
        owner = db_module.Manager(username="platform_owner", full_name="Platforma egasi", role="admin", company_id=1)
        owner.set_password(PW)
        _s.add(owner)
        _s.commit()
        A = _seed_company(_s, "AlphaCo", "AA_MARKER_AA")
        B = _seed_company(_s, "BetaCo", B_MARKER)
finally:
    _s.close()

assert A["id"] != 1 and B["id"] != 1 and A["id"] != B["id"]


def _snapshot_b() -> dict:
    """B'ning BARCHA tenant-jadval qatorlari + Company qatori (filtrsiz)."""
    out = {}
    s = db_module.get_session()
    try:
        with db_module.unscoped():
            for model in db_module._TENANT_FILTERED_MODELS:
                rows = s.query(model).filter(model.company_id == B["id"]).order_by(model.id.asc()).all()
                out[model.__tablename__] = [
                    tuple(sorted((k, repr(v)) for k, v in r.__dict__.items() if not k.startswith("_") and k != "updated_at"))
                    for r in rows
                ]
            c = s.get(db_module.Company, B["id"])
            out["companies"] = tuple(sorted((k, repr(v)) for k, v in c.__dict__.items() if not k.startswith("_") and k != "updated_at"))
    finally:
        s.close()
    return out


BASELINE_B = _snapshot_b()


def _login(client, username):
    r = client.post("/login", data={"username": username, "password": PW})
    assert r.status_code == 302 and "/login" not in (r.headers.get("Location") or ""), f"login xato: {username} -> {r.status_code}"


def _body(resp) -> str:
    try:
        return resp.get_data(as_text=True)
    except Exception:
        return ""


def attack(client, label: str, method: str, url: str, *, data=None, json_body=None, headers=None, allow_200=False):
    """Bitta kross-tenant urinish: marker sizmasin, B o'zgarmasin.
    `allow_200=False` bo'lsa 200 ham (ko'pincha "redirect/404" kutiladi)
    FAQAT marker yo'q bo'lsa qabul qilinadi -- ko'p marshrut "topilmadi"
    flash bilan 302 qaytaradi, ba'zilari o'z (A) sahifasini 200 bilan."""
    checked_routes.append(f"{method} {url}")
    kwargs = {}
    if data is not None:
        kwargs["data"] = data
    if json_body is not None:
        kwargs["json"] = json_body
    if headers:
        kwargs["headers"] = headers
    resp = client.open(url, method=method, **kwargs)
    body = _body(resp)
    leaked = B_MARKER in body
    after = _snapshot_b()
    mutated = after != BASELINE_B
    detail = f"status={resp.status_code}"
    if mutated:
        diff = [k for k in BASELINE_B if BASELINE_B[k] != after.get(k)]
        detail += f" B O'ZGARDI: {diff}"
    if leaked:
        detail += " B MARKER SIZDI"
    check(f"{label}: {method} {url}", (not leaked) and (not mutated) and resp.status_code < 500, detail)
    return resp


# ---------------------------------------------------------------------------
# 1. A ADMINI -> B ma'lumotlari (IDOR)
# ---------------------------------------------------------------------------
sent_calls: list = []


def _no_network(*a, **k):
    sent_calls.append((a, k))
    raise AssertionError("tashqi Meta chaqiruvi kross-tenant urinishda BO'LMASLIGI kerak")


with mock.patch.object(meta_api, "send_instagram_message", _no_network), \
        mock.patch.object(meta_api, "pause_object", _no_network, create=True), \
        mock.patch.object(meta_api, "activate_object", _no_network, create=True):
    with app_module.app.test_client() as c:
        _login(c, A["admin"])
        L = "A-admin->B"
        b = B

        # --- Lidlar / CRM
        attack(c, L, "GET", f"/leads/{b['lead']}")
        attack(c, L, "POST", f"/leads/{b['lead']}", data={"form_action": "update", "status": "contacted", "note": "hack"})
        attack(c, L, "POST", f"/leads/{b['lead']}", data={"form_action": "add_sale", "amount": "1000"})
        attack(c, L, "POST", f"/leads/{b['lead']}/delete")
        attack(c, L, "POST", f"/leads/{A['lead']}", data={"form_action": "mark_returned", "sale_id": str(b["sale"])})
        attack(c, L, "POST", f"/leads/{A['lead']}", data={"form_action": "delete_sale", "sale_id": str(b["sale"])})
        attack(c, L, "GET", f"/leads?company_id={b['id']}")  # platforma-egasi parametri oddiy admin uchun e'tiborsiz bo'lishi kerak
        attack(c, L, "GET", f"/leads?manager_id={b['manager_id']}")
        attack(c, L, "GET", "/leads/chala-toldirilganlar.xlsx")
        attack(c, L, "GET", f"/qayta-aloqa?manager_id={b['manager_id']}")
        attack(c, L, "GET", f"/lead-analytics?manager_id={b['manager_id']}")
        attack(c, L, "GET", f"/menejer-faoliyati?manager_id={b['manager_id']}")
        attack(c, L, "GET", "/sotilgan-xaridorlar")
        attack(c, L, "GET", "/analitika")

        # --- Menejerlar
        attack(c, L, "GET", f"/managers/{b['manager_id']}/edit")
        attack(c, L, "POST", f"/managers/{b['manager_id']}/edit", data={"username": b["manager"], "full_name": "HACKED", "role": "admin", "is_active": "on"})
        attack(c, L, "POST", f"/managers/{b['admin_id']}/edit", data={"username": b["admin"], "full_name": "HACKED", "role": "manager"})
        attack(c, L, "GET", "/managers")
        attack(c, L, "POST", "/sozlamalar/umumiy", data={"action": "set_telegram", "manager_id": str(b["manager_id"]), "telegram_user_id": "999"})
        attack(c, L, "POST", "/sozlamalar/savollar", data={"action": "resolve_unanswered", "question_id": str(b["unanswered"])})
        attack(c, L, "POST", "/sozlamalar/umumiy", data={"action": "resolve_unanswered", "question_id": str(b["unanswered"])})
        attack(c, L, "GET", "/sozlamalar/savollar")

        # --- Sozlamalar: maydonlar / voronka / vazifalar / raqobatchilar
        attack(c, L, "POST", "/settings/fields", data={"action": "toggle", "field_id": str(b["field"])})
        attack(c, L, "POST", "/settings/fields", data={"action": "delete", "field_id": str(b["field"])})
        attack(c, L, "POST", "/settings/funnel", data={"action": "toggle", "stage_id": str(b["stage"])})
        attack(c, L, "POST", "/settings/funnel", data={"action": "delete", "stage_id": str(b["stage"])})
        attack(c, L, "POST", "/settings/tasks", data={"action": "toggle", "kind": "task", "item_id": str(b["task"])})
        attack(c, L, "POST", "/settings/tasks", data={"action": "delete", "kind": "task", "item_id": str(b["task"])})
        attack(c, L, "POST", "/settings/tasks", data={"action": "toggle", "kind": "report", "item_id": str(b["report"])})
        attack(c, L, "POST", "/settings/tasks", data={"action": "delete", "kind": "report", "item_id": str(b["report"])})
        attack(c, L, "GET", "/settings/tasks")
        attack(c, L, "GET", f"/settings/competitors/{b['competitor']}")
        attack(c, L, "POST", f"/settings/competitors/{b['competitor']}/analyze-now")
        attack(c, L, "POST", "/settings/competitors", data={"action": "toggle", "competitor_id": str(b["competitor"])})
        attack(c, L, "POST", "/settings/competitors", data={"action": "delete", "competitor_id": str(b["competitor"])})
        attack(c, L, "GET", "/settings/competitors")
        attack(c, L, "GET", "/settings/fields")
        attack(c, L, "GET", "/settings/funnel")

        # --- Individual tekshirish (qo'ng'iroq audio)
        attack(c, L, "GET", f"/individual-tekshirish/audio/{b['call']}")
        attack(c, L, "GET", f"/individual-tekshirish?manager_id={b['manager_id']}")

        # --- Instagram DM
        attack(c, L, "GET", f"/instagram-xabarlar?c={b['conv']}")
        attack(c, L, "POST", "/instagram-xabarlar/reply", data={"conversation_id": str(b["conv"]), "text": "hack"})
        attack(c, L, "POST", "/instagram-xabarlar/lid", data={"conversation_id": str(b["conv"])})
        attack(c, L, "POST", "/instagram-xabarlar/templates", data={"action": "delete", "template_id": str(b["tpl"])})
        attack(c, L, "GET", "/instagram-xabarlar")

        # --- Avtopilot (CampaignDraft / Media)
        d, m = b["draft"], b["media"]
        attack(c, L, "GET", f"/avtopilot/{d}")
        attack(c, L, "GET", f"/avtopilot/{d}/summary")
        attack(c, L, "GET", f"/avtopilot/{d}/preview")
        attack(c, L, "POST", f"/avtopilot/{d}/patch", json_body={"patch": {"campaign.name": "HACK"}})
        attack(c, L, "POST", f"/avtopilot/{d}/ai-edit", json_body={"message": "hack"})
        attack(c, L, "POST", f"/avtopilot/{d}/regenerate-copy", json_body={})
        attack(c, L, "POST", f"/avtopilot/{d}/replan", json_body={})
        attack(c, L, "POST", f"/avtopilot/{d}/target-analiz", json_body={})
        attack(c, L, "POST", f"/avtopilot/{d}/target-analiz/qollash", json_body={})
        attack(c, L, "POST", f"/avtopilot/{d}/approve", json_body={"level": "campaign"})
        attack(c, L, "POST", f"/avtopilot/{d}/launch-status", json_body={"active": False})
        attack(c, L, "POST", f"/avtopilot/{d}/publish", json_body={})
        attack(c, L, "POST", f"/avtopilot/{d}/activate", json_body={"confirm": True})
        attack(c, L, "POST", f"/avtopilot/{d}/sync", json_body={})
        attack(c, L, "POST", f"/avtopilot/{d}/push", json_body={})
        attack(c, L, "POST", f"/avtopilot/{d}/archive", json_body={})
        attack(c, L, "POST", f"/avtopilot/{d}/media", data={"file": (io.BytesIO(b"\x89PNG\r\n"), "x.png")}, headers={"Content-Type": "multipart/form-data"})
        attack(c, L, "POST", f"/avtopilot/{d}/media/{m}/select", json_body={})
        attack(c, L, "POST", f"/avtopilot/{d}/media/{m}/ochirish", json_body={})
        attack(c, L, "POST", f"/avtopilot/{A['draft']}/media/{m}/select", json_body={})  # o'z qoralamasi + B mediasi
        attack(c, L, "POST", f"/avtopilot/{A['draft']}/media/{m}/ochirish", json_body={})
        attack(c, L, "POST", f"/avtopilot/{A['draft']}/media/from-kreativ", json_body={"creative_asset_id": b["asset"]})
        attack(c, L, "GET", f"/avtopilot/media/{m}")
        attack(c, L, "GET", f"/avtopilot/yangi?creative_asset_id={b['asset']}")
        attack(c, L, "GET", "/avtopilot")

        # --- Kreativ studiya (CreativeAsset / BrandKit)
        a = b["asset"]
        attack(c, L, "GET", f"/kreativ/{a}")
        attack(c, L, "POST", f"/kreativ/{a}/brief", json_body={"message": "hack"})
        attack(c, L, "POST", f"/kreativ/{a}/ozgartir", json_body={"title": "HACK"})
        attack(c, L, "POST", f"/kreativ/{a}/generate", json_body={})
        attack(c, L, "POST", f"/kreativ/{a}/regenerate", json_body={})
        attack(c, L, "POST", f"/kreativ/{a}/layers", json_body={"layers": []})
        attack(c, L, "GET", f"/kreativ/{a}/rasm.png")
        attack(c, L, "GET", f"/kreativ/{a}/fon.png")
        attack(c, L, "GET", f"/kreativ/{a}/eksport.png")
        attack(c, L, "GET", f"/kreativ/{a}/eksport.pdf")
        attack(c, L, "POST", f"/kreativ/{a}/ochirish", json_body={})
        attack(c, L, "POST", f"/kreativ/{a}/target-yarat", data={"objective": "MESSAGES", "budget": "50000"})
        attack(c, L, "GET", "/kreativ")
        attack(c, L, "GET", "/sozlamalar/brend")
        attack(c, L, "GET", "/sozlamalar/brend/logo")
        attack(c, L, "GET", "/sozlamalar/brend/uslub-rasm")

        # --- Platforma egasi marshrutlari -- A admini uchun YOPIQ bo'lishi kerak
        L2 = "A-admin->owner-only"
        for meth, url, data in [
            ("GET", "/companies", None),
            ("POST", "/companies", {"name": "HACK CO", "plan": "unlimited"}),
            ("GET", f"/companies/{b['id']}/edit", None),
            ("POST", f"/companies/{b['id']}/edit", {"action": "suspend"}),
            ("POST", f"/companies/{b['id']}/edit", {"action": "extend_30"}),
            ("POST", f"/companies/{b['id']}/toggle-active", {}),
            ("POST", f"/companies/{b['id']}/delete", {"confirm_name": f"BetaCo {B_MARKER}"}),
            ("GET", f"/companies/{b['id']}/lead-forms", None),
            ("GET", f"/companies/{b['id']}/managers", None),
            ("POST", f"/companies/{b['id']}/managers", {"username": "hack_mgr", "password": "x12345678", "full_name": "H", "role": "admin"}),
            ("POST", f"/companies/{b['id']}/impersonate", {}),
            ("GET", "/companies/ai-suhbatlar", None),
            ("GET", f"/companies/ai-suhbatlar?manager_id={b['manager_id']}", None),
            ("GET", "/companies/murojaatlar", None),
        ]:
            r = attack(c, L2, meth, url, data=data)
            check(f"{L2}: {meth} {url} -> redirect (302) emas {r.status_code}", r.status_code == 302, f"status={r.status_code}")
            check(f"{L2}: {meth} {url} -> dashboard'ga", "/companies" not in (r.headers.get("Location") or "/"), r.headers.get("Location"))
        # impersonatsiya sessiya bayrog'i o'rnatilmagan bo'lishi kerak
        with c.session_transaction() as fs:
            check("A-admin: impersonatsiya bayrog'i sessiyada YO'Q", not fs.get("impersonator_manager_id"))
        # kompaniya soni o'zgarmagan (POST /companies rad etilgan)
        s = db_module.get_session()
        try:
            with db_module.unscoped():
                n_companies = s.query(db_module.Company).count()
                hack_mgr = s.query(db_module.Manager).filter_by(username="hack_mgr").first()
        finally:
            s.close()
        check("A-admin: /companies POST yangi kompaniya YARATMADI", n_companies == 3, f"count={n_companies}")
        check("A-admin: /companies/<B>/managers POST menejer YARATMADI", hack_mgr is None)

        # --- Impersonatsiyadan chiqish (bayroqsiz) -- zararsiz
        attack(c, L, "POST", "/impersonate/exit")

        # --- Telegram ulash tokenlari / kompaniya ulanishlari -- boshqa kompaniya ID'si berilmaydi, faqat current_user
        attack(c, L, "GET", "/connect-accounts")
        attack(c, L, "GET", "/sozlamalar")
        attack(c, L, "GET", "/sozlamalar/telegram")
        attack(c, L, "GET", "/sozlamalar/cpl")
        attack(c, L, "GET", "/sozlamalar/funksiyalar")
        attack(c, L, "GET", "/sozlamalar/ai")
        attack(c, L, "GET", "/sozlamalar/kompaniya-malumotlari")
        attack(c, L, "GET", "/target")
        attack(c, L, "GET", "/smm")
        attack(c, L, "GET", "/")
        attack(c, L, "GET", "/mening-profilim")
        attack(c, L, "GET", "/tolov")
        attack(c, L, "GET", "/marketplace")

    check("Kross-tenant urinishlarda tashqi Meta yuborish chaqirilMAdi", not sent_calls, str(sent_calls[:1]))

# ---------------------------------------------------------------------------
# 2. A ODDIY MENEJERI -> B ma'lumotlari + A admin-only sahifalar
# ---------------------------------------------------------------------------
with app_module.app.test_client() as c:
    _login(c, A["manager"])
    L = "A-manager->B"
    b = B
    attack(c, L, "GET", f"/leads/{b['lead']}")
    attack(c, L, "POST", f"/leads/{b['lead']}", data={"form_action": "update", "status": "contacted", "note": "hack"})
    attack(c, L, "POST", f"/leads/{b['lead']}/delete")
    attack(c, L, "GET", f"/avtopilot/{b['draft']}")
    attack(c, L, "GET", f"/kreativ/{b['asset']}")
    attack(c, L, "GET", f"/avtopilot/media/{b['media']}")
    attack(c, L, "GET", f"/kreativ/{b['asset']}/rasm.png")
    attack(c, L, "GET", f"/instagram-xabarlar?c={b['conv']}")
    attack(c, L, "GET", f"/settings/competitors/{b['competitor']}")
    attack(c, L, "GET", f"/individual-tekshirish/audio/{b['call']}")
    attack(c, L, "POST", "/settings/fields", data={"action": "delete", "field_id": str(b["field"])})
    attack(c, L, "POST", "/settings/funnel", data={"action": "delete", "stage_id": str(b["stage"])})
    attack(c, L, "POST", "/settings/tasks", data={"action": "delete", "kind": "task", "item_id": str(b["task"])})
    attack(c, L, "POST", "/settings/competitors", data={"action": "delete", "competitor_id": str(b["competitor"])})
    attack(c, L, "POST", "/instagram-xabarlar/templates", data={"action": "delete", "template_id": str(b["tpl"])})
    # admin-only (o'z kompaniyasi) -- menejer uchun yopiq
    r = attack(c, "A-manager->admin-only", "GET", f"/managers/{A['admin_id']}/edit")
    check("A-manager: /managers/<own admin>/edit -> redirect", r.status_code == 302)
    r = attack(c, "A-manager->admin-only", "POST", f"/managers/{A['admin_id']}/edit", data={"username": A["admin"], "full_name": "X", "role": "manager"})
    check("A-manager: /managers/<own admin>/edit POST -> redirect", r.status_code == 302)
    r = attack(c, "A-manager->owner-only", "GET", "/companies")
    check("A-manager: /companies -> redirect", r.status_code == 302)
    r = attack(c, "A-manager->owner-only", "POST", f"/companies/{b['id']}/impersonate")
    check("A-manager: impersonate -> redirect", r.status_code == 302)
    s = db_module.get_session()
    try:
        with db_module.unscoped():
            a_admin = s.get(db_module.Manager, A["admin_id"])
            check("A-manager: A admini o'zgarmagan (role=admin)", a_admin.role == "admin" and a_admin.full_name != "X")
    finally:
        s.close()

# ---------------------------------------------------------------------------
# 3. ANONIM -> ID'li marshrutlar (login talab qilinishi kerak)
# ---------------------------------------------------------------------------
with app_module.app.test_client() as c:
    for meth, url in [
        ("GET", f"/leads/{B['lead']}"), ("POST", f"/leads/{B['lead']}/delete"), ("GET", f"/avtopilot/{B['draft']}"),
        ("GET", f"/avtopilot/media/{B['media']}"), ("GET", f"/kreativ/{B['asset']}/rasm.png"), ("GET", f"/kreativ/{B['asset']}"),
        ("GET", f"/individual-tekshirish/audio/{B['call']}"), ("GET", f"/settings/competitors/{B['competitor']}"),
        ("GET", f"/managers/{B['manager_id']}/edit"), ("GET", "/companies"), ("GET", "/sozlamalar/brend/logo"),
        ("GET", "/leads/chala-toldirilganlar.xlsx"), ("GET", "/companies/ai-suhbatlar"),
    ]:
        r = attack(c, "anonim", meth, url)
        check(f"anonim: {meth} {url} -> login'ga (302)", r.status_code == 302 and "/login" in (r.headers.get("Location") or ""), f"status={r.status_code} loc={r.headers.get('Location')}")

# ---------------------------------------------------------------------------
# 4. IJOBIY NAZORAT -- marker/aniqlash mexanizmi haqiqatan ishlaydi, va
#    kompaniyaning O'Z admini o'z ma'lumotiga TO'LIQ kiradi.
# ---------------------------------------------------------------------------
with app_module.app.test_client() as c:
    _login(c, B["admin"])
    r = c.get(f"/leads/{B['lead']}")
    check("nazorat: B admini o'z lidini ko'radi (200 + marker)", r.status_code == 200 and B_MARKER in _body(r), f"status={r.status_code}")
    r = c.get(f"/avtopilot/{B['draft']}")
    check("nazorat: B admini o'z qoralamasini ko'radi (200 + marker)", r.status_code == 200 and B_MARKER in _body(r), f"status={r.status_code}")
    r = c.get(f"/kreativ/{B['asset']}")
    check("nazorat: B admini o'z kreativini ko'radi (200 + marker)", r.status_code == 200 and B_MARKER in _body(r), f"status={r.status_code}")
    r = c.get(f"/settings/competitors/{B['competitor']}")
    check("nazorat: B admini o'z raqobatchisini ko'radi (200 + marker)", r.status_code == 200 and B_MARKER in _body(r), f"status={r.status_code}")
    r = c.get(f"/managers/{B['manager_id']}/edit")
    check("nazorat: B admini o'z menejerini tahrirlash sahifasini ochadi (200 + marker)", r.status_code == 200 and B_MARKER in _body(r), f"status={r.status_code}")
    r = c.get(f"/instagram-xabarlar?c={B['conv']}")
    check("nazorat: B admini o'z DM suhbatini ko'radi (200 + marker)", r.status_code == 200 and B_MARKER in _body(r), f"status={r.status_code}")
    for url in ["/", "/leads", "/managers", "/sozlamalar", "/sozlamalar/umumiy", "/sozlamalar/cpl", "/sozlamalar/telegram",
                "/sozlamalar/funksiyalar", "/sozlamalar/ai", "/sozlamalar/savollar", "/sozlamalar/brend", "/settings/fields",
                "/settings/funnel", "/settings/tasks", "/settings/competitors", "/avtopilot", "/kreativ", "/instagram-xabarlar",
                "/qayta-aloqa", "/menejer-faoliyati", "/lead-analytics", "/analitika", "/individual-tekshirish", "/connect-accounts",
                "/sozlamalar/kompaniya-malumotlari", "/mening-profilim", "/tolov", "/sotilgan-xaridorlar", "/target", "/smm"]:
        r = c.get(url)
        check(f"nazorat (admin to'liq kirish): B admini GET {url} -> 200", r.status_code == 200, f"status={r.status_code} loc={r.headers.get('Location')}")
    r = c.get("/companies")
    check("nazorat: B admini (platforma egasi EMAS) /companies -> redirect", r.status_code == 302)

# Platforma egasi -- /companies ochiq, boshqa kompaniya lidlarini ko'ra oladi (ataylab), impersonatsiya ishlaydi
with app_module.app.test_client() as c:
    _login(c, "platform_owner")
    r = c.get("/companies")
    check("nazorat: platforma egasi /companies -> 200", r.status_code == 200, f"status={r.status_code}")
    r = c.get(f"/leads?company_id={B['id']}")
    check("nazorat: platforma egasi ?company_id=B bilan B lidlarini ko'radi (ataylab)", r.status_code == 200 and B_MARKER in _body(r))
    r = c.get(f"/companies/{B['id']}/managers")
    check("nazorat: platforma egasi B menejerlarini ko'radi", r.status_code == 200 and B_MARKER in _body(r))

# ---------------------------------------------------------------------------
# 5. Yozuvda begona ID: A admini o'z qo'ng'iroq yozuviga B menejer ID'sini
#    bog'lay olmasligi kerak (audio yuklash formasi)
# ---------------------------------------------------------------------------
with app_module.app.test_client() as c:
    _login(c, A["admin"])
    r = c.post("/individual-tekshirish/audio-yuklash", data={
        "audio_file": (io.BytesIO(b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 64), "test.wav"),
        "manager_id": str(B["manager_id"]), "phone_number": "+998901112233", "direction": "outgoing",
    }, content_type="multipart/form-data")
    check("A-admin: audio yuklash B menejer ID'si bilan -> 500 emas", r.status_code < 500, f"status={r.status_code}")
    s = db_module.get_session()
    try:
        with db_module.unscoped():
            cross = s.query(db_module.CallRecord).filter(
                db_module.CallRecord.company_id == A["id"], db_module.CallRecord.manager_id == B["manager_id"]).count()
    finally:
        s.close()
    check("A-admin: A qo'ng'iroq yozuvi B menejeriga BOG'LANMADI", cross == 0, f"cross-tenant manager_id qatorlar={cross}")
    s = db_module.get_session()
    try:
        with db_module.unscoped():
            created = s.query(db_module.CallRecord).filter_by(company_id=A["id"], phone_number="+998901112233").count()
    finally:
        s.close()
    check("A-admin: audio yuklash o'zi ISHLADI (A yozuvi yaratildi -- tekshiruv bo'sh emas)", created == 1, f"created={created}")

# ---------------------------------------------------------------------------
# 6. Webhook'lar: Telegram maxfiy sarlavha (sozlangan bo'lsa) va Meta imzosi
# ---------------------------------------------------------------------------
fake_update = {"message": {"chat": {"id": 424242, "type": "private"}, "text": "/status", "message_id": 1}}
with app_module.app.test_client() as c:
    with mock.patch.object(app_module, "TELEGRAM_WEBHOOK_SECRET", "test-webhook-secret"):
        with mock.patch.object(app_module, "handle_command") as hc:
            r = c.post("/api/webhook", json=fake_update)
            check("Telegram webhook: secret sozlangan, sarlavhasiz -> 403", r.status_code == 403, f"status={r.status_code}")
            check("Telegram webhook: sarlavhasiz buyruq BAJARILMADI", not hc.called)
            r = c.post("/api/webhook", json=fake_update, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"})
            check("Telegram webhook: noto'g'ri sarlavha -> 403", r.status_code == 403, f"status={r.status_code}")
            check("Telegram webhook: noto'g'ri sarlavha bilan buyruq BAJARILMADI", not hc.called)
            r = c.post("/api/webhook", json=fake_update, headers={"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"})
            check("Telegram webhook: to'g'ri sarlavha -> 200 va buyruq bajarildi", r.status_code == 200 and hc.called, f"status={r.status_code}")
    r = c.post("/webhooks/instagram", json={"object": "instagram", "entry": []})
    check("Meta webhook: imzosiz POST -> 401", r.status_code == 401, f"status={r.status_code}")
    r = c.post("/webhooks/instagram", json={"object": "instagram", "entry": []}, headers={"X-Hub-Signature-256": "sha256=deadbeef"})
    check("Meta webhook: noto'g'ri imzo -> 401", r.status_code == 401, f"status={r.status_code}")
    r = c.get("/api/trigger/daily_report")
    check("/api/trigger: CRON_SECRET'siz -> 401", r.status_code == 401, f"status={r.status_code}")
    r = c.post("/api/webhook/leads/notarealtoken", json={"full_name": "x", "phone": "+998901234567"})
    check("/api/webhook/leads/<token>: noto'g'ri token -> 404", r.status_code == 404, f"status={r.status_code}")

# ---------------------------------------------------------------------------
# 7. Sessiya cookie bayroqlari va parol xeshi
# ---------------------------------------------------------------------------
check("SESSION_COOKIE_HTTPONLY=True", app_module.app.config.get("SESSION_COOKIE_HTTPONLY") is True)
check("SESSION_COOKIE_SAMESITE=Lax", app_module.app.config.get("SESSION_COOKIE_SAMESITE") == "Lax")
check("SESSION_COOKIE_SECURE -- RENDER ENV'da True bo'ladi (lokal testda False)", "SESSION_COOKIE_SECURE" in app_module.app.config)
s = db_module.get_session()
try:
    with db_module.unscoped():
        m = s.query(db_module.Manager).filter_by(username=A["admin"]).first()
        check("Parol xeshlangan (werkzeug scrypt/pbkdf2), ochiq matn emas",
              m.password_hash != PW and (m.password_hash.startswith("scrypt:") or m.password_hash.startswith("pbkdf2:")), m.password_hash[:12])
finally:
    s.close()

# ---------------------------------------------------------------------------
# 8. Statik tekshiruvlar: `_is_platform_owner` mantiqi va tenant ro'yxati
# ---------------------------------------------------------------------------
check("_is_platform_owner: company_id==1 + role=admin (signup hech qachon id=1 bermaydi)",
      A["id"] != 1 and B["id"] != 1)
missing = []
for cls in db_module.Base.__subclasses__():
    if cls in (db_module.Company, db_module.KVEntry, db_module.ImpersonationLog):
        continue
    if hasattr(cls, "company_id") and cls not in db_module._TENANT_FILTERED_MODELS:
        missing.append(cls.__name__)
check(f"company_id ustunli HAR BIR model tenant-filtrda (yo'q: {missing})", not missing)

print(f"\nTekshirilgan marshrut-urinishlar: {len(checked_routes)}")
if failures:
    print(f"\nFAIL: {len(failures)} ta muammo:")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("\nHAMMASI O'TDI (kross-tenant sizish yoki o'zgarish topilmadi).")
