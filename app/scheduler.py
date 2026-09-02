"""
scheduler.py — Render'da DOIMIY jarayon ichida ishlaydigan fon vazifalar
jadvali (APScheduler). Vercel versiyasidagi tashqi cron-job.org'ga ehtiyoj
QOLMAYDI -- bu yerda jarayon o'chmaydi, shuning uchun jadval to'g'ridan-to'g'ri
shu jarayon ichida ishlaydi.

Jadval (standart, ENV orqali sozlanadi):
  - 09:00 Toshkent -- ADMIN TARGET HISOBOTI (har doim yuboriladi, faqat OpenAI,
    hech qanday action bajarmaydi) -- foydalanuvchi so'ragan "har kuni 9:00"
    talabi aynan shu. **2026-08dan: KECHAGI (o'tgan to'liq kun) natijasi
    bilan** ("bugungi kun" o'rniga -- ertalab bu deyarli bo'sh bo'lardi), va
    kuniga FAQAT BIR MARTA (`kv_store` orqali kunlik "guard" bilan himoyalangan
    -- takroriy/tashqi trigger bo'lsa ham qayta yuborilmaydi).
  - Har soatda -- to'liq audit + avtomatik tuzatish (Targetolog: byudjet
    oshirish/kamaytirish, pause/resume) -- FAQAT diqqatga loyiq narsa bo'lsa
    Telegram'ga yuboradi.
  - Har 4 soatda -- byudjet balansi ogohlantirishi.
  - Har 15 daqiqada -- CPL hard-kill: LLM'siz, deterministik tekshiruv --
    bugungi kunda `cpl_hard_kill_usd` chegarasidan oshgan (yoki hali
    birorta lead kelmasdan ancha xarajat qilingan) FAOL reklamalarni
    DARHOL pauza qiladi (soatlik LLM audit tsiklidan mustaqil, 2026-08,
    foydalanuvchi shikoyati asosida qo'shildi -- `orchestrator.py`dagi
    `enforce_cpl_hard_kill()`ga qarang).
  - Har 15 daqiqada -- Meta Lead Ads'dan yangi lidlarni CRM bazasiga tortish.
  - Har 5 daqiqada -- foydalanuvchi Telegram orqali qo'ygan DOIMIY vazifalarni
    (schedule_on_off: har kuni belgilangan vaqtda avtomatik yoqish/o'chirish,
    schedule_report: qo'shimcha doimiy hisobot vaqti) tekshiradi va bajaradi.
  - 08:30 Toshkent -- "Qayta aloqa" (follow-up) eslatmasi: bugun yoki
    muddati o'tgan `Lead.next_contact_at`ga ega lidlar haqida shaxsiy
    Telegram xabari (menejerga, `Manager.telegram_user_id` bo'lsa) va
    adminlarga umumiy xulosa.
  - Har 3 soatda -- Instagram Business + Facebook Page uchun SMM statistikasi
    (obunachilar, postlar, qamrov) -- "SMM hisobot" (`/smm`) sahifasi uchun.
  - Har 15 daqiqada -- Instagram DM (Direct) suhbatlarini tortish + uzoq
    vaqt javobsiz qolgan suhbatlarni Telegram'ga ogohlantirish (AI'siz,
    bepul -- `ig_dm_sync.py`).
  - Har 3 soatda -- Instagram DM suhbatlariga gpt-4o-mini bilan lid-sifat
    bahosi (FAQAT yangi xabar kelgan suhbatlar uchun, xarajatni nazorat
    qilish uchun ATAYLAB davriy -- `ig_dm_analysis.py`).
  - 10:00 Toshkent -- Raqobatchilar tahlili: `Competitor` jadvaliga qo'shilgan
    har bir raqobatchining Meta Ad Library'dagi joriy reklamalari yangilanadi
    va qisqa amaliy hisobot tayyorlanadi (2026-08, foydalanuvchi so'rovi).
  - Har soatda (:10, :30, :50) -- qo'ng'iroq yozuvlarining AI tahlili
    (transkripsiya + 1-10 baho) -- "Individual tekshirish" sahifasidagi
    "AI analiz" bo'limi uchun (2026-08, foydalanuvchi bergan audio-tahlil
    prompti asosida, `call_analysis.py`).

Telegram guruh xabarlari IKKI turga bo'lingan (2026-08, foydalanuvchi
so'rovi -- "bittasiga to'liq harakatini, bittasiga faqat kunlik hisobotni"):
  - `_full_activity_targets()` (faqat `TELEGRAM_AGENTS_GROUP_ID`) -- soatlik
    audit, byudjet ogohlantirishi, qayta-aloqa xulosasi, raqobatchi tahlili --
    HAMMA faoliyat xabari shu yerga.
  - `_daily_summary_targets()` (`TELEGRAM_AGENTS_GROUP_ID` + `TELEGRAM_REPORT_GROUP_ID`)
    -- FAQAT soat 9:00dagi kunlik admin hisoboti (`job_admin_report`) shu
    ikkalasiga ham boradi; `TELEGRAM_REPORT_GROUP_ID` boshqa hech qanday
    xabar olmaydi.
"""

import os
import logging
import datetime as dt

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import orchestrator
import budget_tracker
import lead_sync
import call_sync
import call_analysis
import smm_sync
import ig_dm_sync
import ig_dm_analysis
import competitor_sync
import competitor_analytics
import meta_api
import db
import kv_store

logger = logging.getLogger("scheduler")

TIMEZONE = os.environ.get("TIMEZONE", "Asia/Tashkent")


def _group_id(env_name: str) -> int | None:
    raw = os.environ.get(env_name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s noto'g'ri formatda: %r", env_name, raw)
        return None


def _full_activity_targets() -> list[int]:
    """"To'liq harakat" guruhi -- soatlik audit natijalari, byudjet
    ogohlantirishlari, qayta-aloqa xulosasi va HAMMA boshqa faoliyat xabari
    shu YERGA yuboriladi (2026-08, foydalanuvchi so'rovi: "bittasiga to'liq
    bersin harakatini"). Faqat `TELEGRAM_AGENTS_GROUP_ID`. Agar u sozlanmagan
    bo'lsa, eski xulq-atvorga mos ravishda `TELEGRAM_REPORT_GROUP_ID`ga yoki
    (u ham bo'lmasa) foydalanuvchi Telegram'da /start bosgan shaxsiy chatga
    tushib qoladi -- hech qayerga yuborilmay qolib ketmasligi uchun."""
    agents_id = _group_id("TELEGRAM_AGENTS_GROUP_ID")
    if agents_id is not None:
        return [agents_id]
    report_id = _group_id("TELEGRAM_REPORT_GROUP_ID")
    if report_id is not None:
        return [report_id]
    chat_id = budget_tracker.get_notify_chat_id()
    return [chat_id] if chat_id is not None else []


def _daily_summary_targets() -> list[int]:
    """Har kuni soat 9:00dagi ADMIN HISOBOTI ikkala guruhga ham boradi --
    "to'liq harakat" guruhiga (u baribir hammasini ko'radi) VA alohida
    "faqat kunlik hisobot" guruhiga (2026-08, foydalanuvchi so'rovi:
    "bittasiga faqat kunlik hisobotni bersin, boshqasi kerak emas" --
    ya'ni bu ikkinchi guruh SOATLIK audit/qayta-aloqa/byudjet xabarlarini
    UMUMAN olmaydi, faqat shu funksiya orqali kunlik hisobotni oladi)."""
    targets = []
    for env_name in ("TELEGRAM_AGENTS_GROUP_ID", "TELEGRAM_REPORT_GROUP_ID"):
        gid = _group_id(env_name)
        if gid is not None and gid not in targets:
            targets.append(gid)
    if targets:
        return targets
    chat_id = budget_tracker.get_notify_chat_id()
    return [chat_id] if chat_id is not None else []


def _tg_send(chat_id: int, text: str) -> dict:
    """Xabar yuboradi va natijani qaytaradi: {"ok": bool, "error": str|None}.
    MUHIM (bug fix): avval Telegram'ning o'zi rad etsa (masalan "chat not
    found" -- bot guruhga qo'shilmagan, yoki ID noto'g'ri formatda) HECH
    QAYERDA ko'rinmasdi -- kod hech qanday exception ko'tarmasdi, shunchaki
    jim qolardi. Endi natija chaqiruvchiga (job_admin_report va h.k.) va
    log'ga aniq qaytariladi."""
    import requests
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN yo'q -- xabar yuborilmadi")
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN yo'q"}
    api = f"https://api.telegram.org/bot{token}"
    last_error = None
    for i in range(0, len(text), 4000):
        try:
            r = requests.post(f"{api}/sendMessage", json={"chat_id": chat_id, "text": text[i:i + 4000]}, timeout=20)
            body = r.json()
            if not body.get("ok"):
                last_error = body.get("description", str(body))
                logger.error("Telegram sendMessage rad etdi (chat_id=%s): %s", chat_id, body)
        except Exception as e:
            last_error = str(e)
            logger.exception("Telegramga xabar yuborishda xatolik (chat_id=%s)", chat_id)
    return {"ok": last_error is None, "error": last_error}


_ADMIN_REPORT_GUARD_KEY = "admin_report_last_sent_date"


def job_admin_report() -> str:
    """Har kuni 09:00 (Toshkent) -- KECHAGI kunning TO'LIQ (24 soatlik)
    natijasini beradi, "bugungi kun" emas (2026-08, foydalanuvchi so'rovi:
    "shunaqa hisobotni bir kun oldingi kunnikini bersin" -- ertalab soat
    9da "bugungi kun" statistikasi deyarli bo'sh bo'ladi, chunki kun endigina
    boshlangan, kechagi to'liq kun esa haqiqiy manzarani ko'rsatadi).

    MUHIM (kuniga FAQAT BIR MARTA): agar bu funksiya bir kunda ikkinchi marta
    chaqirilsa (masalan eski tashqi cron-job.org sozlamasi hali ham qolib
    ketgan, yoki qayta-deploy paytida ikki marta ishga tushib qolsa) --
    "boshqa avtomatik bermasin" talabiga ko'ra, ikkinchi chaqiruv JIM
    o'tkazib yuboriladi, xabar QAYTA yuborilmaydi."""
    targets = _daily_summary_targets()
    if not targets:
        return "hisobot yuboriladigan chat yo'q"
    now = dt.datetime.utcnow() + dt.timedelta(hours=5)  # Toshkent = UTC+5
    today_str = now.strftime("%Y-%m-%d")

    if kv_store.get_json(_ADMIN_REPORT_GUARD_KEY) == today_str:
        return f"bugun ({today_str}) allaqachon yuborilgan -- qayta yuborilmadi"

    yesterday = now - dt.timedelta(days=1)
    try:
        report = orchestrator.build_admin_report(
            yesterday.strftime("%d.%m.%Y"), now.strftime("%H:%M"),
            "Kechagi kun uchun to'liq yakuniy hisobot",
            insight_kwargs={"date_preset": "yesterday"},
        )
    except Exception as e:
        logger.exception("Admin hisobot xatosi")
        for cid in targets:
            _tg_send(cid, f"⚠️ Kunlik hisobotni tayyorlashda xatolik: {e}")
        return f"xato: {e}"
    send_results = {cid: _tg_send(cid, report) for cid in targets}
    kv_store.set_json(_ADMIN_REPORT_GUARD_KEY, today_str)
    if all(r["ok"] for r in send_results.values()):
        return f"yuborildi -> {targets}"
    return f"URINISH QILINDI, lekin ba'zilari rad etildi: {send_results}"


def job_watch_cycle() -> str:
    targets = _full_activity_targets()
    try:
        report = orchestrator.run_daily_cron_report(dry_run=False)
    except Exception as e:
        logger.exception("Kuzatuv tsikli xatosi")
        for cid in targets:
            _tg_send(cid, f"⚠️ Avtomatik audit/tuzatish tsiklida xatolik: {e}")
        return f"xato: {e}"
    if report is None:
        return "diqqatga loyiq narsa yo'q"
    for cid in targets:
        _tg_send(cid, "\U0001F440 Avtomatik audit natijasi:\n\n" + report)
    return f"yuborildi -> {targets}"


def job_budget_check() -> str:
    try:
        alert = budget_tracker.check_and_alert()
    except Exception as e:
        logger.exception("Byudjet tekshiruvida xatolik")
        return f"xato: {e}"
    if alert:
        _tg_send(alert["chat_id"], alert["message"])
        return "ogohlantirish yuborildi"
    return "hammasi joyida"


def job_cpl_hard_kill() -> dict:
    """Har 15 daqiqada -- CPL hard-kill deterministik (LLM'siz) tekshiruvi
    (`orchestrator.enforce_cpl_hard_kill`). Soatlik LLM audit tsiklidan
    (`job_watch_cycle`) BUTUNLAY MUSTAQIL, ancha tezroq ishlaydigan
    xavfsizlik qatlami (2026-08, foydalanuvchi shikoyati: "cpl kottalashib
    ketvoti targetni ochirmayapti hech narsa qimayapti kech qivoti" --
    LLM soatlik tsiklda `last_7d` o'rtachasiga qarab xulosa chiqargani
    uchun bitta kunlik keskin sakrash "yuvilib" ketishi mumkin edi).

    2026-09, multi-tenant (foydalanuvchi so'rovi: "har bir kompaniya o'z
    reklama hisobi bo'yicha nazorat qilinsin, o'z guruhiga xabar borsin"):
    endi `meta_ad_account_id`/`meta_access_token` ulagan HAR BIR kompaniya
    bo'yicha aylanadi (`orchestrator.enforce_cpl_hard_kill_all_companies()`)
    -- eski yagona (global) akkaunt tekshiruvi o'rniga. Har bir
    kompaniyaning pauza/xato xabari FAQAT o'sha kompaniyaning O'Z
    `Company.telegram_group_id`siga yuboriladi -- platforma egasining
    umumiy `_full_activity_targets()` guruhiga EMAS (`job_ig_dm_sync` bilan
    bir xil xavfsizlik naqshi: boshqa kompaniyaning xarajat/reklama
    ma'lumoti begona Telegram guruhiga chiqib ketmasligi kerak). Kompaniya
    o'z guruhini hali sozlamagan bo'lsa xabar shunchaki YUBORILMAYDI."""
    try:
        overall = orchestrator.enforce_cpl_hard_kill_all_companies()
    except Exception as e:
        logger.exception("CPL hard-kill tekshiruvida kutilmagan xatolik")
        return {"error": str(e)}

    for company_id, result in (overall.get("per_company") or {}).items():
        paused = result.get("paused") or []
        errors = result.get("errors") or []
        if not paused and not errors:
            continue

        chat_id = None
        session = db.get_session()
        try:
            with db.unscoped():
                company = session.query(db.Company).get(company_id)
            raw_group_id = getattr(company, "telegram_group_id", None) if company else None
        finally:
            session.close()
        if raw_group_id:
            try:
                chat_id = int(raw_group_id)
            except (TypeError, ValueError):
                logger.warning("Company id=%s telegram_group_id noto'g'ri formatda: %r", company_id, raw_group_id)
        if chat_id is None:
            # Kompaniya o'z Telegram guruhini hali sozlamagan -- boshqa
            # kompaniyaning guruhiga "sizib chiqmasligi" uchun bu yerda
            # HECH QAYERGA yuborilmaydi (yuqoridagi izohga qarang).
            continue

        lines = []
        if paused:
            lines.append(f"\U0001F6D1 CPL chegarasi oshgani uchun {len(paused)} ta reklama AVTOMATIK pauza qilindi (LLM'siz, darhol):\n")
            for p in paused:
                lines.append(f"- {p['name']} ({p['ad_id']}): {p['reason']}")
        if errors:
            lines.append("\n⚠️ Pauza qilishga urinishda xatoliklar:")
            for e in errors:
                lines.append(f"- {e}")
        message = "\n".join(lines)
        _tg_send(chat_id, message)
    return overall


def job_lead_sync() -> dict:
    """Har 15 daqiqada. 2026-09, multi-tenant (foydalanuvchi so'rovi: "yangi
    kompaniya ochilganda o'z reklama hisobidan lidlari o'z CRM'iga tushishi
    kerak"): endi `meta_page_id`/`meta_access_token` ulagan HAR BIR kompaniya
    bo'yicha aylanadi (`lead_sync.sync_all_companies()`) -- eski yagona
    (global) akkaunt `sync_once()` o'rniga. Har bir kompaniyaning yangi
    lidlari to'g'ridan-to'g'ri O'SHA kompaniyaning `company_id`si bilan
    yoziladi, shuning uchun bu yerda alohida Telegram fan-out shart emas
    (`job_ig_dm_sync`dan farqli o'laroq -- lead sync o'zi xabar yubormaydi,
    faqat bazaga yozadi)."""
    try:
        return lead_sync.sync_all_companies()
    except Exception as e:
        logger.exception("Lead sync xatosi")
        return {"error": str(e)}


def job_call_sync() -> dict:
    """Mening qo'ng'iroqlarim (Moi Zvonki) integratsiyasi -- MOIZVONKI_API_ADDRESS/
    MOIZVONKI_API_KEY sozlanmagan bo'lsa hech narsa qilmasdan tinch qaytadi
    (xato/log emas, chunki bu ixtiyoriy integratsiya). Har safar yangi
    qo'ng'iroqlarni tortib olgandan keyin `reconcile_existing_records()`
    ham chaqiriladi -- shu bilan menejer telefon raqami keyinroq
    o'zgartirilsa/to'ldirilsa ham, bazadagi ESKI yozuvlar avtomatik
    to'g'irlanadi/tozalanadi (qo'lda "call-cleanup" bosish shart emas).

    2026-08 V6.1, foydalanuvchi ANIQ so'ragan ("audio tushsa DARHOL
    tahlil qilinsin, schedulerni kutmasdan"): avval yangi qo'ng'iroqlar
    FAQAT alohida `job_call_analysis` cron'i (soatning :10/:30/:50
    daqiqalarida) orqali tahlil qilinardi -- ya'ni yangi yozuv bilan
    tahlil orasida 20 daqiqagacha kechikish bo'lishi mumkin edi. ENDI,
    agar shu sinxronizatsiya YANGI yozuv(lar) qo'shgan bo'lsa, DARHOL
    (shu job ichida, `job_call_analysis`ni kutmasdan) tahlil navbati
    ishga tushiriladi -- `job_call_analysis` cron'i baribir QOLADI
    (xavfsizlik to'ri sifatida -- masalan avvalgi urinish xato bergan
    "qayta urinish" navbatini tozalash uchun)."""
    try:
        result = call_sync.sync_once()
        if not result.get("configured"):
            return result  # jim -- sozlanmagan, bu normal holat
        try:
            result["reconcile"] = call_sync.reconcile_existing_records()
        except Exception:
            logger.exception("Qo'ng'iroq yozuvlarini tozalashda xatolik")
        if result.get("new_calls"):
            try:
                # Portlash/keskin ko'tarilishning oldini olish uchun BIR
                # martalik yuqori chegara -- odatiy holatda yangi
                # qo'ng'iroqlar soni buncha ko'p bo'lmaydi (bir necha
                # daqiqada bir nechta qo'ng'iroq), lekin xavfsizlik uchun.
                immediate_limit = min(result["new_calls"], 15)
                session = db.get_session()
                try:
                    result["immediate_analysis"] = call_analysis.run_pending_analysis(session, limit=immediate_limit)
                finally:
                    session.close()
            except Exception:
                logger.exception("Yangi qo'ng'iroqlarni DARHOL tahlil qilishda xatolik")
        return result
    except Exception as e:
        logger.exception("Qo'ng'iroq sync xatosi")
        return {"error": str(e)}


def job_call_analysis() -> dict:
    """Qo'ng'iroq yozuvlarini AI yordamida tahlil qiladi (2026-08,
    foydalanuvchi bergan audio-tahlil prompti asosida, `call_analysis.py`) --
    AVTOMATIK: har ishga tushishda hali tahlil qilinmagan, "haqiqiy"
    (shubhali emas) qo'ng'iroqlardan bir nechtasini (limit bilan, API
    xarajatini nazoratda ushlab turish uchun) tahlil qiladi. `job_call_sync`
    dan keyin ishga tushishi uchun soatning boshqa daqiqasida rejalashtirilgan
    (avval yangi qo'ng'iroqlar sinxronlansin, keyin ular tahlil qilinsin)."""
    session = db.get_session()
    try:
        return call_analysis.run_pending_analysis(session, limit=8)
    except Exception as e:
        logger.exception("Qo'ng'iroq AI tahlilida xatolik")
        return {"error": str(e)}
    finally:
        session.close()


def job_lead_cleanup() -> dict:
    """FOYDALANUVCHI ANIQ SO'ROVI bilan (2026-08) qo'lda ishga tushiriladigan
    BIR MARTALIK tozalash -- (#190) tuzatilgandan keyingi birinchi (buzilgan)
    lead-sync butun tarixiy Meta lead arxivini "yangi" deb bazaga yozib
    yuborgan edi. Bu job o'sha eski backlog'ni o'chiradi (haqiqiy sotuvi
    bo'lgan lead'larga tegmaydi) -- `/api/trigger/lead-cleanup`."""
    try:
        return lead_sync.cleanup_backlog_leads()
    except Exception as e:
        logger.exception("Lead backlog tozalashda xatolik")
        return {"error": str(e)}


def job_call_debug() -> dict:
    """VAQTINCHALIK (2026-08): barcha qo'ng'iroq "skipped_unmatched" bo'lib
    chiqqanda -- xom Moi Zvonki javobini va menejerlar telefon raqamlarini
    yonma-yon ko'rish uchun (`/api/trigger/call-debug`)."""
    try:
        return call_sync.debug_sample_calls()
    except Exception as e:
        logger.exception("Call-debug xatosi")
        return {"error": str(e)}


def job_call_cleanup() -> dict:
    """Mavjud `CallRecord`larni DARHOL qayta tekshiradi/tozalaydi --
    foydalanuvchi Menejerlar sahifasida telefon raqamini to'g'irlagandan
    keyin 20 daqiqa (keyingi avtomatik call-sync) kutmasdan darhol natija
    ko'rish uchun (`/api/trigger/call-cleanup`)."""
    try:
        return call_sync.reconcile_existing_records()
    except Exception as e:
        logger.exception("Qo'ng'iroq yozuvlarini tozalashda xatolik")
        return {"error": str(e)}


def job_smm_sync() -> dict:
    """Instagram Business + Facebook Page uchun organik SMM statistikasini
    (obunachilar, postlar, qamrov) tortib oladi -- "SMM hisobot" sahifasi
    (`/smm`) shu ma'lumotdan foydalanadi. 2026-09, multi-tenant: endi
    `meta_page_id`/`meta_access_token` ulagan HAR BIR kompaniya bo'yicha
    aylanadi (`sync_all_companies()`) -- eski yagona-akkaunt `sync_once()`
    o'rniga, shunda "bitta tugma bilan ulash" orqali qo'shilgan boshqa
    kompaniyalarning ham /smm sahifasi haqiqiy ma'lumot bilan to'ladi.
    Hech kim ulamagan bo'lsa jim qaytadi (xato emas, ixtiyoriy integratsiya)."""
    try:
        return smm_sync.sync_all_companies()
    except Exception as e:
        logger.exception("SMM sync xatosi")
        return {"error": str(e)}


def job_ig_dm_sync() -> dict:
    """Har 15 daqiqada -- Instagram DM suhbatlarini Meta'dan tortib
    yangilaydi (2026-08, foydalanuvchi so'rovi: "ig chatlarni tahlilini
    ham qoshish kerak"). Bu job AI ISHLATMAYDI (bepul) -- faqat
    yangi xabarlarni yozadi va uzoq vaqt javobsiz qolgan suhbatlarni
    aniqlaydi (`orchestrator.enforce_cpl_hard_kill`/`job_cpl_hard_kill`
    bilan bir xil naqsh: biznes-mantiq `ig_dm_sync.py`da, Telegram
    yuborish shu yerda).

    2026-09, multi-tenant (foydalanuvchi so'rovi: "boshqa kompaniyalar
    ham Instagram ulasin, ularniki HAM ishlasin"): endi
    `meta_page_id`/`meta_access_token` ulagan HAR BIR kompaniya bo'yicha
    aylanadi (`sync_all_companies()`). MUHIM: har bir kompaniyaning
    javobsiz-suhbat ogohlantirishi FAQAT o'sha kompaniyaning O'Z
    `Company.telegram_group_id`siga yuboriladi -- platforma egasining
    umumiy `_full_activity_targets()` guruhiga EMAS, chunki boshqa
    kompaniyaning mijozi bilan yozishmasi (ism, xabar matni) begona
    Telegram guruhiga chiqib ketishi mumkin emas. Kompaniya o'z guruhini
    hali sozlamagan bo'lsa ogohlantirish shunchaki YUBORILMAYDI (boshqa
    hech kimning guruhiga ham tushmaydi) -- bu ataylab shunday, boshqa
    kompaniyaning ma'lumoti sizib chiqishidan ko'ra "ogohlantirish
    yo'qolib qolishi" xavfsizroq.
    META_ACCESS_TOKEN/META_PAGE_ID sozlanmagan yoki Instagram Business
    akkaunt ulanmagan kompaniyalar uchun jim o'tkazib yuboriladi (xato
    emas, ixtiyoriy integratsiya)."""
    try:
        overall = ig_dm_sync.sync_all_companies()
    except Exception as e:
        logger.exception("Instagram DM sync xatosi")
        return {"error": str(e)}

    for company_id, result in (overall.get("per_company") or {}).items():
        overdue = result.get("overdue") or []
        if not overdue:
            continue

        chat_id = None
        session = db.get_session()
        try:
            with db.unscoped():
                company = session.query(db.Company).get(company_id)
            raw_group_id = getattr(company, "telegram_group_id", None) if company else None
        finally:
            session.close()
        if raw_group_id:
            try:
                chat_id = int(raw_group_id)
            except (TypeError, ValueError):
                logger.warning("Company id=%s telegram_group_id noto'g'ri formatda: %r", company_id, raw_group_id)
        if chat_id is None:
            # Kompaniya o'z Telegram guruhini hali sozlamagan -- boshqa
            # kompaniyaning guruhiga "sizib chiqmasligi" uchun bu yerda
            # HECH QAYERGA yuborilmaydi (yuqoridagi izohga qarang).
            continue

        lines = [f"\U0001F4E9 {len(overdue)} ta Instagram DM {ig_dm_sync.UNANSWERED_ALERT_MINUTES}+ daqiqadan beri javobsiz:\n"]
        for o in overdue:
            lines.append(f"- {o['customer']}: \"{o['preview']}\" ({o['since_minutes']} daqiqadan beri)")
        message = "\n".join(lines)
        tg_result = _tg_send(chat_id, message)
        if tg_result.get("ok"):
            for o in overdue:
                ig_dm_sync.mark_alert_sent(o["conversation_id"])
    return overall


def job_ig_dm_analysis() -> dict:
    """Har 3 soatda -- Instagram DM suhbatlariga gpt-4o-mini bilan
    lid-sifat bahosi beradi (2026-08, foydalanuvchi so'rovi, xarajatni
    nazorat qilish uchun ATAYLAB davriy, real-vaqtda EMAS -- `ig_dm_analysis.py`
    modul izohiga qarang). FAQAT oxirgi tahlildan beri yangi xabar kelgan
    suhbatlar tahlil qilinadi -- o'zgarmagan suhbat qayta ishlanmaydi."""
    try:
        return ig_dm_analysis.analyze_pending_conversations()
    except Exception as e:
        logger.exception("Instagram DM tahlilida xatosi")
        return {"error": str(e)}


def job_competitor_analysis() -> str:
    """Har kuni soat 10:00 -- admin qo'shgan raqobatchilarning Meta Ad
    Library'dagi joriy reklamalarini yangilaydi va qisqa amaliy hisobot
    tayyorlab "to'liq harakat" guruhiga yuboradi (2026-08, foydalanuvchi
    so'rovi). Raqobatchi qo'shilmagan bo'lsa jim qaytadi."""
    targets = _full_activity_targets()
    try:
        competitor_sync.sync_once()
        report = competitor_analytics.build_daily_report()
    except Exception as e:
        logger.exception("Raqobatchilar tahlilida xatolik")
        for cid in targets:
            _tg_send(cid, f"⚠️ Raqobatchilar tahlilida xatolik: {e}")
        return f"xato: {e}"
    if not report:
        return "raqobatchi qo'shilmagan"
    for cid in targets:
        _tg_send(cid, report)
    return f"yuborildi -> {targets}"


def job_followup_reminders() -> dict:
    """"Qayta aloqa" (follow-up) eslatmasi -- har kuni ertalab, `Lead.next_contact_at`
    BUGUN yoki undan OLDINROQ (kechiktirilgan) bo'lgan har bir lead uchun:
      - shu leadga biriktirilgan menejerga (agar `Manager.telegram_user_id`
        to'ldirilgan bo'lsa) SHAXSIY Telegram xabar -- "bugun kimlar bilan
        qayta bog'lanish kerak" ro'yxati (eng ko'p kechikkani birinchi).
      - "to'liq harakat" guruhiga (_full_activity_targets()) UMUMIY qisqa xulosa -- nechta
        lead kechikkan/bugungi, va biriktirilmagan (egasiz) qayta aloqalar
        bo'lsa alohida ogohlantirish (ular hech kimga yuborilmaydi, chunki
        egasi yo'q -- admin o'zi ko'rib biriktirishi kerak).
    CRM'dagi "/qayta-aloqa" sahifasi bilan BIR XIL mantiq (`app.py:
    followups_list`)."""
    session = db.get_session()
    try:
        now = dt.datetime.utcnow()
        today_end = now.replace(hour=23, minute=59, second=59, microsecond=0)
        due = (
            session.query(db.Lead)
            .filter(db.Lead.next_contact_at.isnot(None), db.Lead.next_contact_at <= today_end)
            .order_by(db.Lead.next_contact_at.asc())
            .all()
        )
        if not due:
            return {"due_count": 0, "sent_to_managers": 0}

        by_manager: dict[int, list] = {}
        unassigned = []
        for lead in due:
            if lead.assigned_manager_id:
                by_manager.setdefault(lead.assigned_manager_id, []).append(lead)
            else:
                unassigned.append(lead)

        def _line(lead) -> str:
            days_late = (now.date() - lead.next_contact_at.date()).days
            when = "BUGUN" if days_late == 0 else f"{days_late} kun KECHIKDI"
            name = lead.full_name or "Noma'lum"
            phone = lead.phone or "-"
            note = f" -- {lead.next_contact_note}" if lead.next_contact_note else ""
            return f"  • {name} ({phone}) [{when}]{note}"

        sent_to_managers = 0
        if by_manager:
            managers = session.query(db.Manager).filter(db.Manager.id.in_(by_manager.keys())).all()
            for m in managers:
                if not m.telegram_user_id:
                    continue
                leads = by_manager.get(m.id, [])
                if not leads:
                    continue
                text = f"\U0001F4DE Bugungi qayta aloqalar ({len(leads)} ta):\n\n" + "\n".join(_line(l) for l in leads)
                result = _tg_send(int(m.telegram_user_id), text)
                if result["ok"]:
                    sent_to_managers += 1
                else:
                    logger.warning("Qayta aloqa eslatmasi menejer %s (telegram_user_id=%s)ga yuborilmadi: %s", m.username, m.telegram_user_id, result["error"])

        targets = _full_activity_targets()
        if targets:
            overdue_count = sum(1 for l in due if l.next_contact_at.date() < now.date())
            today_count = len(due) - overdue_count
            summary = (
                f"\U0001F514 Qayta aloqa xulosasi: bugun {today_count} ta, kechikkan {overdue_count} ta "
                f"(jami {len(due)} ta)."
            )
            if unassigned:
                summary += f"\n⚠️ {len(unassigned)} ta lead HECH KIMGA biriktirilmagan -- egasiz qoldi:\n" + "\n".join(_line(l) for l in unassigned[:10])
            for cid in targets:
                _tg_send(cid, summary)

        return {"due_count": len(due), "overdue_count": sum(1 for l in due if l.next_contact_at.date() < now.date()), "sent_to_managers": sent_to_managers, "unassigned": len(unassigned)}
    except Exception as e:
        logger.exception("Qayta aloqa eslatmasida xatolik")
        return {"error": str(e)}
    finally:
        session.close()


def _desired_state(now_hhmm: str, on_time: str, off_time: str) -> str:
    """`on_time` dan `off_time`gacha bo'lgan oraliqda "on", qolgan vaqtda "off"
    qaytaradi. `off_time < on_time` bo'lsa (masalan on=22:00, off=08:00 --
    kechasi yoqiq, kunduzi o'chiq) ham to'g'ri ishlaydi -- yarim tunni kesib
    o'tadigan oraliq alohida hisoblanadi."""
    if on_time == off_time:
        return "on"  # cheklovsiz holat -- doimo yoqiq deb hisoblanadi
    if on_time < off_time:
        return "on" if on_time <= now_hhmm < off_time else "off"
    return "on" if now_hhmm >= on_time or now_hhmm < off_time else "off"


def job_standing_tasks() -> str:
    """Foydalanuvchi Telegram orqali bir marta qo'ygan `schedule_on_off`
    vazifalarini (`db.StandingTask`) joriy Toshkent vaqtiga solishtirib,
    kerak bo'lsa Meta'da avtomatik yoqadi/o'chiradi -- foydalanuvchi qayta
    buyruq berishi shart emas. Faqat HOLAT O'ZGARGANDA (last_desired_state'dan
    farqli bo'lganda) Meta API'ga murojaat qiladi -- keraksiz qayta so'rovlar
    yubormaslik uchun."""
    now = dt.datetime.utcnow() + dt.timedelta(hours=5)  # Toshkent = UTC+5
    now_hhmm = now.strftime("%H:%M")
    session = db.get_session()
    changes_by_chat: dict = {}
    errors_by_chat: dict = {}
    try:
        tasks = session.query(db.StandingTask).filter_by(is_active=True).all()
        for t in tasks:
            desired = _desired_state(now_hhmm, t.on_time, t.off_time)
            if desired == t.last_desired_state:
                continue
            try:
                (meta_api.activate_object if desired == "on" else meta_api.pause_object)(t.object_id)
                t.last_desired_state = desired
                t.last_checked_at = now
                t.last_error = None
                changes_by_chat.setdefault(t.chat_id, []).append((t.object_name or t.object_id, desired))
            except Exception as e:
                t.last_error = str(e)
                logger.exception("Standing task xatosi (object_id=%s)", t.object_id)
                errors_by_chat.setdefault(t.chat_id, []).append((t.object_name or t.object_id, str(e)))
        session.commit()
    finally:
        session.close()

    for chat_id, changes in changes_by_chat.items():
        lines = ["\U0001F550 Avtomatik jadval bo'yicha o'zgarish:"]
        for name, desired in changes:
            verb = "yoqdim" if desired == "on" else "o'chirdim"
            lines.append(f"   🔧 {name}: {verb}")
        try:
            _tg_send(int(chat_id), "\n".join(lines))
        except (TypeError, ValueError):
            pass
    for chat_id, errs in errors_by_chat.items():
        lines = ["⚠️ Avtomatik jadvalda xatolik chiqdi:"]
        for name, err in errs:
            lines.append(f"   {name}: {err}")
        try:
            _tg_send(int(chat_id), "\n".join(lines))
        except (TypeError, ValueError):
            pass

    total_changed = sum(len(v) for v in changes_by_chat.values())
    total_errors = sum(len(v) for v in errors_by_chat.values())
    if not total_changed and not total_errors:
        return "o'zgarish yo'q"
    return f"o'zgardi={total_changed}, xato={total_errors}"


def job_standing_reports() -> str:
    """Foydalanuvchi Telegram orqali qo'shgan QO'SHIMCHA doimiy hisobot
    vaqtlarini (`db.StandingReport`) tekshiradi -- vaqti kelgan va bugun hali
    yuborilmagan hisobotlarni tayyorlab yuboradi. Asosiy 09:00 dagi kunlik
    hisobotni ALMASHTIRMAYDI, unga QO'SHIMCHA."""
    now = dt.datetime.utcnow() + dt.timedelta(hours=5)
    now_hhmm = now.strftime("%H:%M")
    today_str = now.strftime("%Y-%m-%d")
    session = db.get_session()
    try:
        reports = session.query(db.StandingReport).filter_by(is_active=True).all()
        # MUHIM: aniq tenglik emas ( >= ) -- job har 5 daqiqada ishlaydi, aniq
        # HH:MM daqiqasiga to'g'ri kelib qolmasligi mumkin. `last_sent_date`
        # bir kunda faqat BIR MARTA yuborilishini kafolatlaydi.
        due = [r for r in reports if now_hhmm >= r.time_hhmm and r.last_sent_date != today_str]
        for r in due:
            r.last_sent_date = today_str
        session.commit()
        due_chat_ids = [r.chat_id for r in due]
    finally:
        session.close()

    if not due_chat_ids:
        return "hozircha hisobot vaqti yo'q"

    try:
        report_text = orchestrator.build_admin_report(
            now.strftime("%d.%m.%Y"), now.strftime("%H:%M"),
            "Qo'shimcha (foydalanuvchi so'ragan) hisobot",
            insight_kwargs={"date_preset": "today"},
        )
    except Exception as e:
        logger.exception("Qo'shimcha (standing) hisobot xatosi")
        report_text = f"⚠️ Qo'shimcha hisobotni tayyorlashda xatolik: {e}"

    for chat_id in due_chat_ids:
        try:
            _tg_send(int(chat_id), report_text)
        except (TypeError, ValueError):
            pass
    return f"yuborildi -> {due_chat_ids}"


JOBS = {
    "admin-report": job_admin_report,
    "watch": job_watch_cycle,
    "budget": job_budget_check,
    "lead-sync": job_lead_sync,
    "lead-cleanup": job_lead_cleanup,
    "standing-tasks": job_standing_tasks,
    "standing-reports": job_standing_reports,
    "call-sync": job_call_sync,
    "call-cleanup": job_call_cleanup,
    "call-debug": job_call_debug,
    "followup-reminders": job_followup_reminders,
    "smm-sync": job_smm_sync,
    "ig-dm-sync": job_ig_dm_sync,
    "ig-dm-analysis": job_ig_dm_analysis,
    "competitor-analysis": job_competitor_analysis,
}

_scheduler_started = False


def start_scheduler(app) -> None:
    """Flask ilova ishga tushganda BIR MARTA chaqiriladi. Gunicorn bir nechta
    worker bilan ishlayotgan bo'lsa, har bir worker o'zining schedulerini
    boshlab yuborishi mumkin (bir xil job bir necha marta ishga tushishi
    mumkin) -- shu sabab Render deploy'ida gunicorn `--workers 1` bilan
    ishga tushirilishi TAVSIYA ETILADI (README'da ko'rsatilgan), bot/scheduler
    holati bitta jarayonda qolishi uchun."""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

    scheduler = BackgroundScheduler(timezone=TIMEZONE)
    # MUHIM: har bir CronTrigger'ga ALOHIDA ham `timezone=TIMEZONE` beriladi
    # (2026-08, foydalanuvchi so'rovi: "har kuni 9da" xabari aslida 14:00da
    # kelib turgan holat kuzatilgan -- bu BackgroundScheduler darajasidagi
    # `timezone` sozlamasiga bog'liqligini kutish o'rniga, har bir trigger'ni
    # o'zi ham aniq Toshkent vaqtiga bog'lab, mumkin bo'lgan noaniqlikni
    # (masalan platforma darajasidagi TZ o'zgaruvchisi/muhit) yo'qotish uchun).
    scheduler.add_job(job_admin_report, CronTrigger(hour=9, minute=0, timezone=TIMEZONE), id="admin-report")
    scheduler.add_job(job_watch_cycle, CronTrigger(minute=5, timezone=TIMEZONE), id="watch")  # har soatning 5-daqiqasida
    scheduler.add_job(job_budget_check, CronTrigger(hour="*/4", minute=10, timezone=TIMEZONE), id="budget")
    scheduler.add_job(job_cpl_hard_kill, CronTrigger(minute="*/15", timezone=TIMEZONE), id="cpl-hard-kill")  # LLM'siz, tez CPL xavfsizlik qatlami
    scheduler.add_job(job_lead_sync, CronTrigger(minute="*/15", timezone=TIMEZONE), id="lead-sync")
    scheduler.add_job(job_standing_tasks, CronTrigger(minute="*/5", timezone=TIMEZONE), id="standing-tasks")
    scheduler.add_job(job_standing_reports, CronTrigger(minute="*/5", timezone=TIMEZONE), id="standing-reports")
    scheduler.add_job(job_call_sync, CronTrigger(minute="*/20", timezone=TIMEZONE), id="call-sync")
    scheduler.add_job(job_call_analysis, CronTrigger(minute="10,30,50", timezone=TIMEZONE), id="call-analysis")  # call-sync'dan keyin
    scheduler.add_job(job_followup_reminders, CronTrigger(hour=8, minute=30, timezone=TIMEZONE), id="followup-reminders")
    scheduler.add_job(job_smm_sync, CronTrigger(hour="*/3", minute=15, timezone=TIMEZONE), id="smm-sync")  # obunachilar/postlar tez o'zgarmaydi, har 3 soatda yetarli
    scheduler.add_job(job_ig_dm_sync, CronTrigger(minute="*/15", timezone=TIMEZONE), id="ig-dm-sync")  # AI'siz, tez -- yangi xabar/javobsizlik tekshiruvi
    scheduler.add_job(job_ig_dm_analysis, CronTrigger(hour="*/3", minute=20, timezone=TIMEZONE), id="ig-dm-analysis")  # gpt-4o-mini, davriy, xarajatni nazorat qilish uchun
    scheduler.add_job(job_competitor_analysis, CronTrigger(hour=10, minute=0, timezone=TIMEZONE), id="competitor-analysis")
    scheduler.start()
    logger.info("Scheduler ishga tushdi (timezone=%s)", TIMEZONE)
