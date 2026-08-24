"""
lead_sync.py — Meta Lead Ads'dagi lidlarni muntazam (masalan har 15 daqiqada)
Postgres CRM bazasiga tortib oladi.

NEGA WEBHOOK EMAS (hozircha): Meta'ning haqiqiy real-vaqt lead webhook'i
alohida Meta App yaratish + shu Page'ni App'ga ulash + ba'zi hollarda App
Review talab qiladi -- bu boshlang'ich bosqichda ortiqcha to'siq. Polling
(har necha daqiqada so'rash) ancha sodda va tezda ishga tushadi; keyinchalik
xohlasa, `/leads_webhook` endpoint qo'shib to'liq real-vaqtga o'tish mumkin.

Oqim:
  1. META_PAGE_ID sahifadagi barcha Instant Form'larni ro'yxatlaydi.
  2. Har bir forma uchun yangi lidlarni so'raydi (`meta_api.get_leads`).
  3. Har bir lead uchun campaign_id/adset_id/ad_id orqali kampaniya NOMINI
     (`meta_api.get_account_structure`dan keshlangan xarita) biriktiradi.
  4. `meta_lead_id` bo'yicha dublikatni tekshirib, faqat YANGI lidlarni
     `leads` jadvaliga yozadi (status="new").
"""

import json
import logging
import datetime as dt

import meta_api
from db import get_session, Lead

logger = logging.getLogger("lead_sync")


def _field_data_to_dict(field_data: list[dict]) -> dict:
    """Meta lead javobini {"full_name": "...", "phone_number": "...", ...}
    ko'rinishiga soddalashtiradi -- forma savollari ixtiyoriy nom bilan
    kelgani uchun eng keng tarqalgan kalitlarni tanib olishga harakat qiladi."""
    out = {}
    for item in field_data or []:
        name = (item.get("name") or "").lower()
        values = item.get("values") or []
        out[name] = values[0] if values else None
    return out


def _extract_name_phone_email(fd: dict) -> tuple[str | None, str | None, str | None]:
    name = fd.get("full_name") or fd.get("name")
    if not name:
        first = fd.get("first_name", "")
        last = fd.get("last_name", "")
        name = f"{first} {last}".strip() or None
    phone = fd.get("phone_number") or fd.get("phone")
    email = fd.get("email")
    return name, phone, email


def sync_once() -> dict:
    """Bitta sinxronizatsiya tsiklini bajaradi. Qaytaradi:
    {"new_leads": N, "forms_checked": N, "errors": [...]}"""
    page_id = meta_api.PAGE_ID
    result = {"new_leads": 0, "forms_checked": 0, "errors": []}

    if not page_id:
        result["errors"].append("META_PAGE_ID sozlanmagan -- lead sync o'tkazib yuborildi.")
        return result

    try:
        forms = meta_api.get_lead_forms(page_id)
    except meta_api.MetaAPIError as e:
        result["errors"].append(f"Formalarni olishda xatolik: {e}")
        return result

    # Kampaniya/adset/ad ID -> NOM xaritalari (dashboard/CRM'da "qaysi target,
    # qaysi video/reklamadan kelgan" to'liq ko'rinishi uchun -- ad_name ko'pincha
    # ishlatilgan video/kreativ nomiga mos qilib qo'yiladi).
    campaign_name_by_id: dict[str, str] = {}
    adset_name_by_id: dict[str, str] = {}
    ad_name_by_id: dict[str, str] = {}
    try:
        structure = meta_api.get_account_structure(active_only=False)
        for c in structure.get("campaigns", []):
            campaign_name_by_id[c["id"]] = c.get("name", "")
        for a in structure.get("adsets", []):
            adset_name_by_id[a["id"]] = a.get("name", "")
        for a in structure.get("ads", []):
            ad_name_by_id[a["id"]] = a.get("name", "")
    except meta_api.MetaAPIError as e:
        result["errors"].append(f"Kampaniya nomlarini olishda xatolik (davom etamiz): {e}")

    session = get_session()
    try:
        for form in forms:
            form_id = form["id"]
            result["forms_checked"] += 1
            try:
                leads = meta_api.get_leads(form_id)
            except meta_api.MetaAPIError as e:
                result["errors"].append(f"Forma {form_id} lidlarini olishda xatolik: {e}")
                continue

            for raw in leads:
                meta_lead_id = raw.get("id")
                if not meta_lead_id:
                    continue
                existing = session.query(Lead).filter_by(meta_lead_id=meta_lead_id).first()
                if existing:
                    continue  # allaqachon bazada bor -- dublikat qilinmaydi

                fd = _field_data_to_dict(raw.get("field_data"))
                name, phone, email = _extract_name_phone_email(fd)
                campaign_id = raw.get("campaign_id")
                adset_id = raw.get("adset_id")
                ad_id = raw.get("ad_id")
                created_time = raw.get("created_time")
                try:
                    created_dt = dt.datetime.strptime(created_time[:19], "%Y-%m-%dT%H:%M:%S") if created_time else None
                except ValueError:
                    created_dt = None

                lead = Lead(
                    meta_lead_id=meta_lead_id,
                    campaign_id=campaign_id,
                    campaign_name=campaign_name_by_id.get(campaign_id, ""),
                    adset_id=adset_id,
                    adset_name=adset_name_by_id.get(adset_id, ""),
                    ad_id=ad_id,
                    ad_name=ad_name_by_id.get(ad_id, ""),
                    form_name=form.get("name"),
                    source="meta",
                    full_name=name,
                    phone=phone,
                    email=email,
                    raw_field_data=json.dumps(fd, ensure_ascii=False),
                    status="new",
                    lead_created_time=created_dt,
                )
                session.add(lead)
                result["new_leads"] += 1

            session.commit()
    finally:
        session.close()

    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(sync_once(), ensure_ascii=False, indent=2))
