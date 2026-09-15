"""test_meta_publish_offline.py — Meta Ads Autopilot (2026-09, foydalanuvchi
so'rovi: "Replix ... Ads Manager'dagi har bir maydonni boshidan o'zi
to'ldirmasligi kerak"): `meta_publish.py` -- BARCHA `meta_api` chaqiruvlari
mock qilinadi (tarmoq yo'q):

  1. To'liq tasdiqlangan qoralama -> campaign -> adset -> creative -> ad
     tartibida, hammasi PAUSED, ID'lar bazada.
  2. Idempotent/davom ettirish: birinchi urinish "adset"da yiqiladi
     (meta_campaign_id saqlanadi, status failed, publish_step="adset",
     friendly xato); ikkinchi urinishda `create_campaign` QAYTA
     chaqirilmaydi.
  3. Tasdiqsiz nashr bloklanadi (o'zbekcha xabar).
  4. `activate_draft` campaign -> adset -> ad tartibida ACTIVE qiladi.
  5. `import_campaign` daraxt fixture'ini kanonik holatga (META_IMPORTED)
     aylantiradi, qo'llanmagan sozlamalar ogohlantirishda.
  6. `sync_draft_from_meta` Meta'dagi o'zgarishni (`meta_changed`) va
     mahalliy o'zgarishni (`local_changes`) aniqlaydi.
  7. Multi-tenant: B kompaniya `db.scoped_as` ostida A qoralamalarini ko'rmaydi.
  8. `get_meta_assets` bitta sub-so'rov xatosida qolganlarini qaytaradi.
  9. `push_updates_to_meta` -- nom/byudjet/targeting update_*, kreativ matn
     -> yangi kreativ + update_ad_creative.

Ishga tushirish:
    cd app && python3 scripts/test_meta_publish_offline.py
"""
import os
import sys
import json
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("META_ACCESS_TOKEN", "test-dummy-token")
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_test_dummy")
os.environ.setdefault("META_PAGE_ID", "page_test_dummy")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

_TMPDIR = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMPDIR, 'test_meta_publish.db')}"

import db as db_module  # noqa: E402
import meta_api  # noqa: E402
import campaign_draft as cd  # noqa: E402
import meta_publish  # noqa: E402

db_module.init_db()

failures = []


def check(name, cond):
    print(("OK  " if cond else "FAIL") + " " + name)
    if not cond:
        failures.append(name)


ASSETS = {
    "ad_account": {"id": "act_1", "name": "Acc", "currency": "UZS", "timezone_name": "Asia/Tashkent"},
    "pages": [{"id": "PAGE1", "name": "Nur", "instagram_business_account": {"id": "IG1"}}],
    "instagram_accounts": [{"id": "IG1"}], "pixels": [{"id": "PX1"}], "custom_audiences": [{"id": "AUD1", "name": "x"}],
    "lead_forms": [{"id": "FORM1"}], "recent_images": [], "campaigns": [], "errors": [],
    "pixel_id": "PX1", "page_id": "PAGE1", "ig_business_id": "IG1",
}


def _company(session, name="Nur Mebel", plan="business", **kw):
    c = db_module.Company(name=name, plan=plan, is_active=True, meta_ad_account_id="act_1", meta_page_id="PAGE1", ig_business_id="IG1", meta_pixel_id="PX1", **kw)
    c.set_meta_access_token("tok_A")
    session.add(c)
    session.commit()
    return c


def _state(objective="MESSAGES"):
    s = cd.new_empty_state(objective)
    s["campaign"]["name"] = "Replix | Nur | " + objective
    s["adset"]["name"] = "Toshkent 25-45"
    s["adset"]["daily_budget"] = 200000
    s["adset"]["duration_days"] = 7
    s["adset"]["start_time"] = "2026-09-20T09:00:00"
    s["adset"]["end_time"] = "2026-09-27T09:00:00"
    s["adset"]["destination_type"] = "INSTAGRAM_DIRECT" if objective == "MESSAGES" else s["adset"]["destination_type"]
    s["adset"]["targeting"]["age_min"] = 25
    s["adset"]["targeting"]["age_max"] = 45
    s["adset"]["targeting"]["geo_locations"]["cities"] = [{"key": "2430536", "name": "Tashkent", "radius": 0, "distance_unit": "kilometer"}]
    s["adset"]["targeting"]["interests"] = [{"id": "6003", "name": "Furniture"}]
    s["ad"]["name"] = "Reklama 1"
    s["ad"]["page_id"] = "PAGE1"
    s["ad"]["instagram_actor_id"] = "IG1"
    s["ad"]["media"]["image_hash"] = "HASH1"
    s["ad"]["primary_text"] = "Oshxona mebeli buyurtma asosida"
    s["ad"]["headline"] = "Nur Mebel"
    s["ad"]["messages"] = {"greeting": "Assalomu alaykum!", "quick_replies": ["Narxi?", "Manzil?"]}
    if objective == "LEADS":
        s["ad"]["lead_form"] = {"mode": "new", "existing_form_id": None, "new_form": {"name": "F", "intro_headline": "", "intro_description": "", "questions": [{"type": "FULL_NAME"}, {"type": "PHONE"}], "privacy_url": "https://x.uz/p", "thank_you_title": "", "thank_you_body": ""}}
    return s


def _draft(session, company, objective="MESSAGES", approved=True):
    d = db_module.CampaignDraft(company_id=company.id, title="T", objective=objective, campaign_approved=approved, adset_approved=approved, ad_approved=approved)
    d.set_state(_state(objective))
    d.set_field_sources({"adset.targeting.age_min": "AI_RECOMMENDED"})
    session.add(d)
    session.commit()
    return d


class MetaMock:
    """Barcha yaratish/o'qish chaqiruvlarini yozib boradigan soxta Meta."""

    def __init__(self, fail_at=None):
        self.calls = []
        self.fail_at = fail_at
        self.statuses = {}
        self.names = {}
        self.adset_budget = None
        self.adset_targeting = None

    def _rec(self, name, *args, **kw):
        self.calls.append((name, args, kw))
        if self.fail_at == name:
            raise meta_api.MetaAPIError({"message": "Invalid parameter: targeting spec rejected", "code": 100, "error_subcode": 1487079})

    def create_campaign(self, name, objective, status, cats, *, access_token, ad_account_id):
        self._rec("create_campaign", name, objective, status, cats, access_token=access_token, ad_account_id=ad_account_id)
        self.statuses["C1"] = status
        self.names["C1"] = name
        return {"id": "C1"}

    def create_adset(self, campaign_id, name, daily_cents, targeting, opt, billing, bid, status, promoted, **kw):
        self._rec("create_adset", campaign_id, name, daily_cents, targeting, opt, billing, bid, status, promoted, **kw)
        self.statuses["AS1"] = status
        self.names["AS1"] = name
        self.adset_budget = daily_cents
        self.adset_targeting = targeting
        return {"id": "AS1"}

    def create_ad_creative(self, ad_account_id, name, spec, *, access_token):
        self._rec("create_ad_creative", ad_account_id, name, spec, access_token=access_token)
        return {"id": "CR1"}

    def create_ad(self, adset_id, name, creative_id, status, *, access_token, ad_account_id):
        self._rec("create_ad", adset_id, name, creative_id, status, access_token=access_token, ad_account_id=ad_account_id)
        self.statuses["AD1"] = status
        self.names["AD1"] = name
        return {"id": "AD1"}

    def create_lead_form(self, page_id, cfg, *, access_token=None):
        self._rec("create_lead_form", page_id, cfg, access_token=access_token)
        return {"id": "FORMNEW"}

    def activate_object(self, object_id, *, access_token=None):
        self._rec("activate_object", object_id, access_token=access_token)
        self.statuses[object_id] = "ACTIVE"
        return {"success": True}

    def get_campaign_basic(self, cid, *, access_token):
        return {"id": cid, "name": self.names.get(cid, "?"), "status": self.statuses.get(cid, "PAUSED")}

    def get_adset_basic(self, aid, *, access_token):
        return {"id": aid, "name": self.names.get(aid, "?"), "status": self.statuses.get(aid, "PAUSED"), "daily_budget": str(self.adset_budget or 0),
                "targeting": {"age_min": (self.adset_targeting or {}).get("age_min"), "age_max": (self.adset_targeting or {}).get("age_max")},
                "end_time": "2026-09-27T09:00:00+0500"}

    def get_ad_basic(self, adid, *, access_token):
        return {"id": adid, "name": self.names.get(adid, "?"), "status": self.statuses.get(adid, "PAUSED")}

    def patches(self):
        return [
            mock.patch.object(meta_api, "create_campaign", self.create_campaign),
            mock.patch.object(meta_api, "create_adset", self.create_adset),
            mock.patch.object(meta_api, "create_ad_creative", self.create_ad_creative),
            mock.patch.object(meta_api, "create_ad", self.create_ad),
            mock.patch.object(meta_api, "create_lead_form", self.create_lead_form),
            mock.patch.object(meta_api, "activate_object", self.activate_object),
            mock.patch.object(meta_api, "get_campaign_basic", self.get_campaign_basic),
            mock.patch.object(meta_api, "get_adset_basic", self.get_adset_basic),
            mock.patch.object(meta_api, "get_ad_basic", self.get_ad_basic),
            mock.patch.object(meta_publish, "get_meta_assets", lambda company, use_cache=True: json.loads(json.dumps(ASSETS))),
        ]

    def __enter__(self):
        self._active = self.patches()
        for p in self._active:
            p.start()
        return self

    def __exit__(self, *a):
        for p in reversed(self._active):
            p.stop()


def test_full_publish_and_activate():
    session = db_module.get_session()
    try:
        c = _company(session)
        d = _draft(session, c)
        with MetaMock() as m:
            res = meta_publish.publish_draft(session, d, c, manager_id=None)
            order = [name for name, *_ in m.calls]
            check("tartib campaign->adset->creative->ad", order == ["create_campaign", "create_adset", "create_ad_creative", "create_ad"])
            check("hammasi PAUSED", all(v == "PAUSED" for v in m.statuses.values()))
            check("ID'lar bazada", (d.meta_campaign_id, d.meta_adset_id, d.meta_creative_id, d.meta_ad_id) == ("C1", "AS1", "CR1", "AD1"))
            check("status published, synced, step None", d.status == "published" and d.sync_status == "synced" and d.publish_step is None)
            check("natija ID'lar", res["campaign_id"] == "C1" and res["ad_id"] == "AD1")
            check("verify ogohlantirish yo'q", res["warnings"] == [])
            cc = m.calls[0]
            check("kompaniya tokeni/hisobi bilan", cc[2]["access_token"] == "tok_A" and cc[2]["ad_account_id"] == "act_1")
            check("meta_objective OUTCOME_ENGAGEMENT", cc[1][1] == "OUTCOME_ENGAGEMENT")
            ca = m.calls[1]
            check("byudjet kichik birlikda (UZS*100)", ca[1][2] == 20000000)
            check("optimization CONVERSATIONS + promoted page", ca[1][4] == "CONVERSATIONS" and ca[1][8] == {"page_id": "PAGE1"})
            check("destination_type yuborildi", ca[2]["destination_type"] == "INSTAGRAM_DIRECT" and ca[2]["start_time"] == "2026-09-20T09:00:00")
            check("targeting flexible_spec", ca[1][3]["flexible_spec"] == [{"interests": [{"id": "6003", "name": "Furniture"}]}])
            spec = m.calls[2][1][2]
            check("creative spec page/ig/hash", spec["page_id"] == "PAGE1" and spec["instagram_actor_id"] == "IG1" and spec["link_data"]["image_hash"] == "HASH1")
            check("creative page_welcome_message", "page_welcome_message" in spec["link_data"])
            check("ad PAUSED bilan yaratildi", m.calls[3][1][3] == "PAUSED")
            with db_module.scoped_as(c.id):
                actions = [e.action for e in session.query(db_module.CampaignDraftEvent).filter_by(draft_id=d.id).order_by(db_module.CampaignDraftEvent.id).all()]
            check("audit-jurnal to'liq", actions == ["publish_started", "meta_campaign_created", "meta_adset_created", "meta_creative_created", "meta_ad_created", "publish_verified"])

            # ikkinchi nashr -- bloklanadi
            try:
                meta_publish.publish_draft(session, d, c)
                check("published qoralama qayta nashr qilinmaydi", False)
            except meta_publish.PublishError as e:
                check("published qoralama qayta nashr qilinmaydi", "allaqachon" in e.friendly)

            # activate
            res = meta_publish.activate_draft(session, d, c, manager_id=None)
            act = [a[1][0] for a in m.calls if a[0] == "activate_object"]
            check("activate tartibi C->AS->AD", act == ["C1", "AS1", "AD1"])
            check("status active", d.status == "active" and res["activated"] == ["campaign", "adset", "ad"])
            try:
                meta_publish.activate_draft(session, d, c)
                check("ikkinchi activate rad", False)
            except meta_publish.PublishError:
                check("ikkinchi activate rad", True)
    finally:
        session.close()


def test_resume_after_failure_idempotent():
    session = db_module.get_session()
    try:
        c = _company(session, name="Resume Co")
        d = _draft(session, c)
        with MetaMock(fail_at="create_adset") as m:
            try:
                meta_publish.publish_draft(session, d, c)
                check("adset xatosi PublishError", False)
            except meta_publish.PublishError as e:
                check("adset xatosi PublishError", e.step == "adset")
                check("friendly targeting xabari", e.friendly == "Tanlangan targeting Meta tomonidan qabul qilinmadi. Hudud/yosh/qiziqishlarni tekshirib qayta urinib ko'ring.")
            check("status failed, step adset", d.status == "failed" and d.publish_step == "adset")
            check("meta_campaign_id saqlangan", d.meta_campaign_id == "C1" and d.meta_adset_id is None)
            check("publish_error friendly, raw saqlangan", "targeting" in d.publish_error and "1487079" in d.last_meta_error_raw)
            with db_module.scoped_as(c.id):
                actions = [e.action for e in session.query(db_module.CampaignDraftEvent).filter_by(draft_id=d.id).all()]
            check("meta_error + publish_failed eventlari", "meta_error" in actions and "publish_failed" in actions)
            m.fail_at = None
            res = meta_publish.publish_draft(session, d, c)
            names = [n for n, *_ in m.calls]
            check("create_campaign ikki urinishda BIR MARTA", names.count("create_campaign") == 1)
            check("create_adset ikkinchi urinishda muvaffaqiyatli", names.count("create_adset") == 2 and d.meta_adset_id == "AS1")
            check("qayta urinish yakunlandi", d.status == "published" and res["ad_id"] == "AD1")
    finally:
        session.close()


def test_blocked_without_approvals_and_plan():
    session = db_module.get_session()
    try:
        c = _company(session, name="Unapproved Co")
        d = _draft(session, c, approved=False)
        with MetaMock() as m:
            try:
                meta_publish.publish_draft(session, d, c)
                check("tasdiqsiz bloklanadi", False)
            except meta_publish.PublishError as e:
                check("tasdiqsiz bloklanadi", e.step == "validate" and "tasdiqlanmagan" in e.friendly)
            check("Meta chaqiruvi yo'q", m.calls == [])
            check("status draft qoladi", d.status == "draft")
            msgs = meta_publish.validate_for_publish(d, c, ASSETS)
            check("validate_for_publish uchta tasdiq xabari", sum("tasdiqlanmagan" in x for x in msgs) == 3)
        c2 = _company(session, name="Trial Co", plan="trial")
        d2 = _draft(session, c2)
        msgs = meta_publish.validate_for_publish(d2, c2, ASSETS)
        check("trial tarif bloklanadi", any("tarif" in x for x in msgs))
        d3 = _draft(session, c)
        s = d3.get_state()
        s["ad"]["page_id"] = "STRANGER"
        d3.set_state(s)
        msgs = meta_publish.validate_for_publish(d3, c, ASSETS)
        check("begona sahifa bloklanadi", any("ulanmagan" in x for x in msgs))
    finally:
        session.close()


def test_leads_publish_creates_form():
    session = db_module.get_session()
    try:
        c = _company(session, name="Leads Co")
        d = _draft(session, c, objective="LEADS")
        with MetaMock() as m:
            meta_publish.publish_draft(session, d, c)
            names = [n for n, *_ in m.calls]
            check("LEADS: lead form yaratildi campaign'dan oldin", names[:2] == ["create_lead_form", "create_campaign"])
            check("LEADS: meta_lead_form_id saqlandi", d.meta_lead_form_id == "FORMNEW")
            lf_call = m.calls[0]
            check("LEADS: forma kompaniya tokeni bilan", lf_call[2]["access_token"] == "tok_A" and lf_call[1][1]["privacy_policy"]["url"] == "https://x.uz/p")
            spec = [a for a in m.calls if a[0] == "create_ad_creative"][0][1][2]
            check("LEADS: CTA lead_gen_form_id", spec["link_data"]["call_to_action"]["value"] == {"lead_gen_form_id": "FORMNEW"})
            adset_call = [a for a in m.calls if a[0] == "create_adset"][0]
            check("LEADS: optimization LEAD_GENERATION", adset_call[1][4] == "LEAD_GENERATION")
    finally:
        session.close()


CAMPAIGN_TREE = {
    "id": "C777", "name": "Eski kampaniya", "objective": "OUTCOME_LEADS", "status": "ACTIVE", "special_ad_categories": [],
    "daily_budget": "5000000",
    "adsets": [{
        "id": "AS777", "name": "Eski adset", "status": "ACTIVE", "daily_budget": "15000000", "optimization_goal": "LEAD_GENERATION",
        "billing_event": "IMPRESSIONS", "bid_strategy": "LOWEST_COST_WITHOUT_CAP", "destination_type": "ON_AD",
        "start_time": "2026-09-01T09:00:00+0500", "end_time": "2026-10-01T09:00:00+0500",
        "targeting": {
            "geo_locations": {"cities": [{"key": "2430536", "name": "Tashkent", "radius": 10, "distance_unit": "kilometer"}], "regions": [{"key": "3903", "name": "Tashkent Region"}], "countries": ["UZ"]},
            "age_min": 22, "age_max": 50, "genders": [1],
            "flexible_spec": [{"interests": [{"id": "6003", "name": "Furniture"}], "life_events": [{"id": "1", "name": "Newlywed"}]}],
            "custom_audiences": [{"id": "AUD1", "name": "Old buyers"}],
            "publisher_platforms": ["facebook", "instagram"], "instagram_positions": ["stream", "story"],
            "targeting_automation": {"advantage_audience": 0},
            "excluded_geo_locations": {"cities": [{"key": "1"}]},
            "work_positions": [{"id": "9"}],
        },
        "ads": [{"id": "AD777", "name": "Eski reklama", "status": "ACTIVE", "creative": {"id": "CR777", "object_story_spec": {
            "page_id": "PAGE1", "instagram_actor_id": "IG1",
            "link_data": {"image_hash": "OLDHASH", "message": "Eski matn", "name": "Eski sarlavha", "description": "Tavsif", "link": "https://nur.uz",
                          "call_to_action": {"type": "SIGN_UP", "value": {"lead_gen_form_id": "FORM1"}}},
        }}}],
    }, {"id": "AS778", "name": "Ikkinchi", "ads": []}],
}


def test_import_campaign():
    session = db_module.get_session()
    try:
        c = _company(session, name="Import Co")
        with mock.patch.object(meta_api, "get_campaign_tree", return_value=json.loads(json.dumps(CAMPAIGN_TREE))) as gt, \
                mock.patch.object(meta_publish, "get_meta_assets", lambda company, use_cache=True: json.loads(json.dumps(ASSETS))):
            d = meta_publish.import_campaign(session, c, "C777", manager_id=None)
        check("get_campaign_tree kompaniya tokeni bilan", gt.call_args[1]["access_token"] == "tok_A")
        s = d.get_state()
        check("source IMPORTED, status active (Meta ACTIVE)", d.source == "IMPORTED" and d.status == "active")
        check("objective LEADS", s["objective"] == "LEADS" and s["campaign"]["meta_objective"] == "OUTCOME_LEADS")
        check("Meta ID'lar", (d.meta_campaign_id, d.meta_adset_id, d.meta_creative_id, d.meta_ad_id, d.meta_lead_form_id) == ("C777", "AS777", "CR777", "AD777", "FORM1"))
        check("byudjet from_minor UZS", s["adset"]["daily_budget"] == 150000.0 and s["adset"]["currency"] == "UZS")
        t = s["adset"]["targeting"]
        check("cities/regions/countries", t["geo_locations"]["cities"][0]["radius"] == 10 and t["geo_locations"]["regions"][0]["key"] == "3903" and t["geo_locations"]["countries"] == ["UZ"])
        check("yosh/jins", t["age_min"] == 22 and t["age_max"] == 50 and t["genders"] == [1])
        check("interests flexible_spec'dan", t["interests"] == [{"id": "6003", "name": "Furniture"}])
        check("custom audiences", t["custom_audiences"] == [{"id": "AUD1", "name": "Old buyers"}])
        check("placements manual", t["placements"]["mode"] == "manual" and t["placements"]["publisher_platforms"] == ["facebook", "instagram"] and t["placements"]["instagram_positions"] == ["stream", "story"])
        check("advantage_audience False", t["advantage_audience"] is False)
        check("creative matnlari", s["ad"]["primary_text"] == "Eski matn" and s["ad"]["headline"] == "Eski sarlavha" and s["ad"]["link_url"] == "https://nur.uz" and s["ad"]["media"]["image_hash"] == "OLDHASH")
        check("lead_form existing", s["ad"]["lead_form"]["mode"] == "existing" and s["ad"]["lead_form"]["existing_form_id"] == "FORM1")
        check("cta SIGN_UP", s["ad"]["cta"] == "SIGN_UP")
        check("field_sources META_IMPORTED", set(d.get_field_sources().values()) == {"META_IMPORTED"})
        check("tasdiqlar True, synced", d.all_approved and d.sync_status == "synced")
        w = d.get_ai_plan()["warnings"]
        check("ogohlantirish: 2 adset", any("2 ta Ad Set" in x for x in w))
        check("ogohlantirish: CBO", any("CBO" in x for x in w))
        check("ogohlantirish: qo'llanmagan sozlamalar", any("qo'llamaydigan sozlamalar" in x and "work_positions" in x for x in w))
        check("ogohlantirish: life_events / excluded_geo", any("life_events" in x for x in w) and any("excluded_geo" in x for x in w))
        check("import event", session.query(db_module.CampaignDraftEvent).filter_by(draft_id=d.id, action="imported").count() == 1)
        check("import qilingan holat validate'dan o'tadi", cd.validate_state(s, company=c, meta_assets=ASSETS) == [])
    finally:
        session.close()


def test_sync_detects_changes():
    session = db_module.get_session()
    try:
        c = _company(session, name="Sync Co")
        d = _draft(session, c)
        with MetaMock() as m:
            meta_publish.publish_draft(session, d, c)
            check("nashrdan keyin synced", meta_publish.sync_draft_from_meta(session, d, c) == "synced")
            # Meta'da byudjet o'zgardi
            m.adset_budget = 30000000
            check("Meta'da byudjet o'zgardi -> meta_changed", meta_publish.sync_draft_from_meta(session, d, c) == "meta_changed")
            snap = d.get_meta_snapshot()
            check("snapshot saqlandi", snap["meta"]["adset"]["daily_budget"] == 30000000 and snap["diffs"])
            # endi Meta'dagi holat asos (baseline) -- yana o'zgarmasa synced
            check("baseline yangilangach synced", meta_publish.sync_draft_from_meta(session, d, c) == "synced")
            # mahalliy o'zgarish
            s = d.get_state()
            s["ad"]["headline"] = "Yangi sarlavha"
            d.set_state(s)
            session.commit()
            check("mahalliy o'zgarish -> local_changes", meta_publish.sync_draft_from_meta(session, d, c) == "local_changes")
            # Meta'da ACTIVE qilingan bo'lsa mahalliy status ham active
            m.statuses["C1"] = "ACTIVE"
            m.statuses["AS1"] = "ACTIVE"
            m.statuses["AD1"] = "ACTIVE"
            st = meta_publish.sync_draft_from_meta(session, d, c)
            check("Meta ACTIVE -> meta_changed + status active", st == "meta_changed" and d.status == "active")
        with mock.patch.object(meta_api, "get_campaign_basic", side_effect=meta_api.MetaAPIError({"message": "boom", "code": 190})):
            check("xato -> sync_error", meta_publish.sync_draft_from_meta(session, d, c) == "sync_error" and d.sync_status == "sync_error")
    finally:
        session.close()


def test_push_updates():
    session = db_module.get_session()
    try:
        c = _company(session, name="Push Co")
        d = _draft(session, c)
        with MetaMock():
            meta_publish.publish_draft(session, d, c)
        s = d.get_state()
        s["adset"]["daily_budget"] = 300000
        s["adset"]["targeting"]["age_min"] = 30
        s["ad"]["headline"] = "Kuchli sarlavha"
        s["campaign"]["name"] = "Yangi nom"
        d.set_state(s)
        session.commit()
        with mock.patch.object(meta_api, "update_campaign", return_value={"success": True}) as uc, \
                mock.patch.object(meta_api, "update_adset", return_value={"success": True}) as ua, \
                mock.patch.object(meta_api, "update_ad", return_value={"success": True}) as uad, \
                mock.patch.object(meta_api, "create_ad_creative", return_value={"id": "CR2"}) as cc, \
                mock.patch.object(meta_api, "update_ad_creative", return_value={"success": True}) as uac:
            res = meta_publish.push_updates_to_meta(session, d, c, ["adset.daily_budget", "adset.targeting.age_min", "ad.headline", "campaign.name", "ad.url_tags"])
        check("update_campaign nom", uc.call_args[0][1] == {"name": "Yangi nom"})
        fields = ua.call_args[0][1]
        check("update_adset byudjet + targeting", fields["daily_budget"] == 30000000 and fields["targeting"]["age_min"] == 30)
        check("update_ad chaqirilmadi (nom o'zgarmadi)", uad.call_count == 0)
        check("yangi kreativ + biriktirish", cc.call_count == 1 and uac.call_args[0] == ("AD1", "CR2") and d.meta_creative_id == "CR2")
        check("pushed/skipped", "ad.creative" in res["pushed"] and res["skipped"] == ["ad.url_tags"])
        check("synced", d.sync_status == "synced")
        with mock.patch.object(meta_api, "update_campaign", side_effect=meta_api.MetaAPIError({"message": "Error validating access token: Session has expired", "code": 190})):
            try:
                meta_publish.push_updates_to_meta(session, d, c, ["campaign.name"])
                check("push xatosi", False)
            except meta_publish.PublishError as e:
                check("push xatosi friendly (token)", "muddati tugagan" in e.friendly and d.sync_status == "sync_error")
    finally:
        session.close()


def test_multitenant_isolation():
    session = db_module.get_session()
    try:
        a = _company(session, name="A Co")
        b = _company(session, name="B Co")
        da = _draft(session, a)
        da_id, a_id, b_id = da.id, a.id, b.id
        session.close()
        session = db_module.get_session()  # yangi sessiya -- identity map'da eski obyekt qolmasin
        with db_module.scoped_as(b_id):
            check("B kompaniya A qoralamalarini ko'rmaydi (query)", session.query(db_module.CampaignDraft).filter_by(id=da_id).first() is None)
            check("B kompaniya A qoralamasini ko'rmaydi (get)", session.get(db_module.CampaignDraft, da_id) is None)
            check("B kompaniya A eventlarini ko'rmaydi", session.query(db_module.CampaignDraftEvent).filter_by(draft_id=da_id).count() == 0)
        with db_module.scoped_as(a_id):
            check("A o'zinikini ko'radi", session.get(db_module.CampaignDraft, da_id) is not None)
        check("yangi modellar tenant ro'yxatida", all(m in db_module._COMPANY_SCOPED_MODELS for m in (db_module.CampaignDraft, db_module.CampaignDraftMedia, db_module.CampaignDraftEvent, db_module.IgDmAdSource)))
    finally:
        session.close()


def test_get_meta_assets_tolerates_failures():
    session = db_module.get_session()
    try:
        c = _company(session, name="Assets Co")
        meta_publish.invalidate_meta_assets_cache(c.id)
        with mock.patch.object(meta_api, "get_ad_account_info", return_value={"id": "act_1", "currency": "UZS"}), \
                mock.patch.object(meta_api, "get_ad_account_pixels", return_value=[{"id": "PX1"}]), \
                mock.patch.object(meta_api, "list_custom_audiences", side_effect=meta_api.MetaAPIError({"message": "no permission", "code": 10})), \
                mock.patch.object(meta_api, "list_ad_images", return_value=[]), \
                mock.patch.object(meta_api, "list_ad_account_campaigns", return_value=[{"id": "C1", "name": "x"}]), \
                mock.patch.object(meta_api, "oauth_list_pages", return_value=[{"id": "PAGE1", "name": "Nur", "instagram_business_account": {"id": "IG1", "username": "nur"}}]), \
                mock.patch.object(meta_api, "get_lead_forms", return_value=[{"id": "FORM1"}]) as lf:
            assets = meta_publish.get_meta_assets(c, use_cache=False)
            assets_cached = meta_publish.get_meta_assets(c)
        check("bitta xato boshqalarni to'xtatmaydi", assets["ad_account"]["currency"] == "UZS" and assets["campaigns"] and assets["custom_audiences"] == [])
        check("errors ro'yxati", len(assets["errors"]) == 1 and "custom_audiences" in assets["errors"][0])
        check("instagram_accounts sahifadan", assets["instagram_accounts"][0]["id"] == "IG1")
        check("lead_forms kompaniya tokeni bilan", lf.call_args[1]["access_token"] == "tok_A")
        check("kesh ishlaydi (ikkinchi chaqiriq so'rovsiz)", lf.call_count == 1 and assets_cached["pixel_id"] == "PX1")
        meta_publish.invalidate_meta_assets_cache(c.id)
        c2 = db_module.Company(name="No token", plan="business", is_active=True)
        session.add(c2)
        session.commit()
        assets = meta_publish.get_meta_assets(c2, use_cache=False)
        check("tokensiz -> errors", assets["errors"] and assets["pages"] == [])
    finally:
        session.close()


def test_friendly_errors():
    fe = meta_publish.friendly_publish_error
    check("token 190", "muddati tugagan" in fe(meta_api.MetaAPIError({"code": 190, "message": "x"}), "campaign"))
    check("IG not connected", "Instagram akkaunt reklama akkauntiga ulanmagan" in fe(meta_api.MetaAPIError({"code": 100, "message": "Instagram account is not connected to the page"}), "creative"))
    check("pixel", "Pixel" in fe(meta_api.MetaAPIError({"code": 100, "message": "promoted_object pixel_id required"}), "adset"))
    check("generic", "Meta bilan bog'lanishda" in fe(RuntimeError("boom"), "ad"))


test_full_publish_and_activate()
test_resume_after_failure_idempotent()
test_blocked_without_approvals_and_plan()
test_leads_publish_creates_form()
test_import_campaign()
test_sync_detects_changes()
test_push_updates()
test_multitenant_isolation()
test_get_meta_assets_tolerates_failures()
test_friendly_errors()

print()
if failures:
    print(f"MUVAFFAQIYATSIZ: {len(failures)} ta test o'tmadi:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("BARCHA TESTLAR O'TDI (meta_publish)")
