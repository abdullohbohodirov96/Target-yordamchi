"""test_lead_phone2_and_other_answers_offline.py — 2026-09, foydalanuvchi
so'rovi: "target leadlarni o'zi tortmayapti to'xtab qopti to'g'rila va
nima savol bo'lsa ushani ulash bo'lsin, misol: instagram formda target'da
savollar to'g'irlanadi-yu ushani to'g'irlash hamma savolga javobi
tushadigan bo'lsin, xozir ikkita nomerdan bittasi tushmayapti to'g'rila
ushani".

HAQIQIY topilgan sabab: Instant Form'da ADMIN ikkita telefon-turidagi
savol qo'shgan bo'lsa (masalan "sizning raqamingiz" + "qo'shimcha/
yaqiningizning raqami"), `lead_sync._extract_name_phone_email()` FAQAT
BITTA `phone` qiymatini tanib olar edi -- ikkinchisi (va umuman, ism/
telefon/email bo'lmagan HAR QANDAY boshqa savol javobi) faqat xom
`Lead.raw_field_data` JSON ichida qolib, CRM'ning hech bir joyida
ko'rsatilmasdi -- ya'ni foydalanuvchi nuqtai nazaridan "javob tushmagan"
edi.

Bu fayl tekshiradi:
  1. `lead_sync._extract_name_phone_email()` -- ikkita ALOHIDA telefon-
     turidagi kalit bo'lsa, ikkalasi ham (`phone`, `phone2`) qaytarilishi.
  2. Bitta telefon bo'lsa `phone2` `None` qolishi (yolg'on-musbat yo'q).
  3. `lead_sync.sync_once()` orqali uchi-uchigacha -- yaratilgan `Lead`
     qatorida `phone`/`phone2` ikkalasi ham TO'G'RI saqlanishi.
  4. `lead_sync.other_form_answers()` -- ism/telefon/telefon2/email'dan
     TASHQARI, formadagi boshqa (masalan admin qo'shgan maxsus) savol
     javobi CRM'da ko'rsatish uchun to'g'ri ajratib olinishi, va
     ALLAQACHON ko'rsatilgan maydonlar (ism/telefon/email) takrorlanib
     chiqmasligi.

Ishga tushirish:
    cd app && python3 scripts/test_lead_phone2_and_other_answers_offline.py
"""

import os
import sys
import tempfile
import datetime as dt
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ.setdefault("META_APP_ID", "test_app_id")
os.environ.setdefault("META_APP_SECRET", "test_app_secret")


def _fresh_modules(db_path):
    for name in ("db", "kv_store", "lead_sync", "orchestrator", "scheduler", "meta_events", "app"):
        sys.modules.pop(name, None)
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ.pop("META_ACCESS_TOKEN", None)
    os.environ.pop("META_PAGE_ID", None)
    os.environ.pop("META_AD_ACCOUNT_ID", None)
    import db as db_module
    db_module.init_db()
    import lead_sync
    return db_module, lead_sync


def test_extract_name_phone_email_captures_second_phone_field():
    import lead_sync

    fd = {
        "full_name": "Aziz Aziz",
        "telefon_raqamingiz": "+998901112233",
        "qoshimcha_telefon_raqami": "+998907778899",
    }
    name, phone, phone2, email = lead_sync._extract_name_phone_email(fd)
    assert name == "Aziz Aziz"
    assert phone == "+998901112233", f"birinchi telefon to'g'ri tanilishi kerak: {phone}"
    assert phone2 == "+998907778899", f"IKKINCHI telefon ham tanilishi kerak (avval yo'qolib qolardi): {phone2}"
    print("OK: _extract_name_phone_email() ikkita alohida telefon-savolini ikkalasini ham (phone, phone2) tanib oladi")


def test_extract_name_phone_email_single_phone_has_no_phone2():
    import lead_sync

    fd = {"full_name": "Karim Karim", "phone_number": "+998901112233"}
    name, phone, phone2, email = lead_sync._extract_name_phone_email(fd)
    assert phone == "+998901112233"
    assert phone2 is None, f"bitta telefon bo'lganda phone2 None bo'lishi kerak (yolg'on-musbat yo'q): {phone2}"
    print("OK: _extract_name_phone_email() bitta telefon bo'lsa phone2'ni yolg'on hosil qilmaydi")


def test_sync_once_persists_both_phones_and_other_answer_via_raw_field_data():
    with tempfile.TemporaryDirectory() as tmp:
        db_module, lead_sync = _fresh_modules(os.path.join(tmp, "p1.db"))
        company_id = db_module.get_default_company_id()
        fake_company = lead_sync._CompanyCreds(
            id=company_id, meta_page_id="page_x", meta_access_token="tok_x", meta_ad_account_id=None,
        )

        old_cursor = int(dt.datetime.utcnow().timestamp()) - 3600
        import kv_store
        kv_store.set_json(lead_sync._since_key(company_id), old_cursor)

        import meta_api

        def fake_get_lead_forms(page_id, *, access_token=None):
            return [{"id": "form_1", "name": "Ariza formasi", "leads_count": 1}]

        def fake_get_leads(form_id, since=None, *, access_token=None, page_id=None):
            return [{
                "id": "meta_lead_1", "created_time": dt.datetime.utcnow().isoformat(),
                "campaign_id": None, "adset_id": None, "ad_id": None, "form_id": form_id,
                "field_data": [
                    {"name": "full_name", "values": ["Nodira Nodirova"]},
                    {"name": "telefon_raqamingiz", "values": ["+998901112233"]},
                    {"name": "qoshimcha_telefon_raqami", "values": ["+998907778899"]},
                    {"name": "nechta_xona_kerak", "values": ["3 xonali"]},
                ],
            }]

        with mock.patch.object(meta_api, "get_lead_forms", side_effect=fake_get_lead_forms), \
             mock.patch.object(meta_api, "get_leads", side_effect=fake_get_leads), \
             mock.patch.object(meta_api, "get_account_structure", return_value={"campaigns": [], "adsets": [], "ads": []}), \
             mock.patch("meta_events.dispatch_lead_event"):
            result = lead_sync.sync_once(company=fake_company)

        assert result["new_leads"] == 1, f"1 ta yangi lead kutilgan edi: {result}"

        session = db_module.get_session()
        try:
            with db_module.unscoped():
                lead = session.query(db_module.Lead).filter_by(meta_lead_id="meta_lead_1").first()
            assert lead is not None, "lead bazaga yozilmagan"
            assert lead.phone == "+998901112233", f"birinchi telefon noto'g'ri: {lead.phone}"
            assert lead.phone2 == "+998907778899", f"IKKINCHI telefon Lead.phone2'ga yozilishi kerak (avval umuman saqlanmasdi): {lead.phone2}"

            others = lead_sync.other_form_answers(
                lead.raw_field_data, exclude_values=[lead.full_name, lead.phone, lead.phone2, lead.email],
            )
            others_dict = dict(others)
            assert "nechta_xona_kerak" in others_dict and others_dict["nechta_xona_kerak"] == "3 xonali", \
                f"admin qo'shgan boshqa savol javobi CRM'da ko'rsatish uchun ajratib olinishi kerak: {others}"
            # Ism/telefon/telefon2 ALLAQACHON alohida ko'rsatiladi -- "boshqa
            # javoblar" ro'yxatida TAKRORLANIB chiqmasligi kerak.
            assert "full_name" not in others_dict and "telefon_raqamingiz" not in others_dict \
                and "qoshimcha_telefon_raqami" not in others_dict, \
                f"allaqachon ko'rsatilgan maydonlar 'boshqa javoblar'da takrorlanmasligi kerak: {others}"
        finally:
            session.close()
    print("OK: sync_once() ikkinchi telefonni Lead.phone2'ga yozadi va other_form_answers() qolgan (masalan admin qo'shgan maxsus) savol javobini CRM uchun to'g'ri ajratib beradi")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
