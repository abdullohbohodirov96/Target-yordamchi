"""
scheduler.py — Render'da DOIMIY jarayon ichida ishlaydigan fon vazifalar
jadvali (APScheduler). Vercel versiyasidagi tashqi cron-job.org'ga ehtiyoj
QOLMAYDI -- bu yerda jarayon o'chmaydi, shuning uchun jadval to'g'ridan-to'g'ri
shu jarayon ichida ishlaydi.

Jadval (standart, ENV orqali sozlanadi):
  - 09:00 Toshkent -- ADMIN TARGET HISOBOTI (har doim yuboriladi, faqat OpenAI,
    hech qanday action bajarmaydi) -- foydalanuvchi so'ragan "har kuni 9:00"
    talabi aynan shu.
  - Har soatda -- to'liq audit + avtomatik tuzatish (Targetolog: byudjet
    oshirish/kamaytirish, pause/resume) -- FAQAT diqqatga loyiq narsa bo'lsa
    Telegram'ga yuboradi.
  - Har 4 soatda -- byudjet balansi ogohlantirishi.
  - Har 15 daqiqada -- Meta Lead Ads'dan yangi lidlarni CRM bazasiga tortish.
"""

import os
import logging
import datetime as dt

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import orchestrator
import budget_tracker
import lead_sync

logger = logging.getLogger("scheduler")

TIMEZONE = os.environ.get("TIMEZONE", "Asia/Tashkent")


def _report_targets() -> list[int]:
    targets = []
    for env_name in ("TELEGRAM_AGENTS_GROUP_ID", "TELEGRAM_REPORT_GROUP_ID"):
        raw = os.environ.get(env_name)
        if raw:
            try:
                targets.append(int(raw))
            except ValueError:
                logger.warning("%s noto'g'ri formatda: %r", env_name, raw)
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


def job_admin_report() -> str:
    targets = _report_targets()
    if not targets:
        return "hisobot yuboriladigan chat yo'q"
    now = dt.datetime.utcnow() + dt.timedelta(hours=5)  # Toshkent = UTC+5
    try:
        report = orchestrator.build_admin_report(
            now.strftime("%d.%m.%Y"), now.strftime("%H:%M"),
            "Ertalabki holat va bugungi ish rejasi",
            insight_kwargs={"date_preset": "today"},
        )
    except Exception as e:
        logger.exception("Admin hisobot xatosi")
        for cid in targets:
            _tg_send(cid, f"⚠️ Kunlik hisobotni tayyorlashda xatolik: {e}")
        return f"xato: {e}"
    send_results = {cid: _tg_send(cid, report) for cid in targets}
    if all(r["ok"] for r in send_results.values()):
        return f"yuborildi -> {targets}"
    return f"URINISH QILINDI, lekin ba'zilari rad etildi: {send_results}"


def job_watch_cycle() -> str:
    targets = _report_targets()
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


def job_lead_sync() -> dict:
    try:
        return lead_sync.sync_once()
    except Exception as e:
        logger.exception("Lead sync xatosi")
        return {"error": str(e)}


JOBS = {
    "admin-report": job_admin_report,
    "watch": job_watch_cycle,
    "budget": job_budget_check,
    "lead-sync": job_lead_sync,
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
    scheduler.add_job(job_admin_report, CronTrigger(hour=9, minute=0), id="admin-report")
    scheduler.add_job(job_watch_cycle, CronTrigger(minute=5), id="watch")  # har soatning 5-daqiqasida
    scheduler.add_job(job_budget_check, CronTrigger(hour="*/4", minute=10), id="budget")
    scheduler.add_job(job_lead_sync, CronTrigger(minute="*/15"), id="lead-sync")
    scheduler.start()
    logger.info("Scheduler ishga tushdi (timezone=%s)", TIMEZONE)
