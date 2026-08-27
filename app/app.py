"""
app.py — asosiy Flask ilova (Render'da DOIMIY jarayon sifatida ishlaydi,
gunicorn orqali). Uch narsani birlashtiradi:

  1. Web dashboard + CRM (login qilingan admin/menejerlar uchun)
  2. Telegram bot webhook (guruh/shaxsiy chatdagi savol-javob + hisobotlar)
  3. Fon vazifalar (APScheduler): kunlik 09:00 hisobot, soatlik kuzatuv+
     avto-tuzatish, byudjet nazorati, lead-sync -- Vercel'dagi kabi tashqi
     cron-job.org shart emas, chunki bu yerda jarayon DOIMIY ishlaydi.
"""

import os
import re
import json
import logging
import threading
import datetime as dt
from collections import defaultdict

from flask import Flask, request, jsonify, render_template, redirect, url_for, flash, Response, abort
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user, login_required,
    current_user,
)

import meta_api
import orchestrator
import budget_tracker
import kv_store
import monthly_report
import permissions
from db import init_db, get_session, Manager, Lead, LeadNote, CustomField, FunnelStage, CallRecord, Sale, AssistantUnanswered, Competitor, CompetitorAd
from dashboard_data import get_kpis, _date_preset_bounds_utc, custom_range_bounds_utc
import lead_sync
import call_sync
import call_analytics
import call_analysis
import kpi_bonus
import smm_sync
import smm_analytics

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("target-crm")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")

login_manager = LoginManager(app)
login_manager.login_view = "login"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
CRON_SECRET = os.environ.get("CRON_SECRET", "")
KNOWLEDGE_BASE = orchestrator.KNOWLEDGE_BASE

# ---------------------------------------------------------------------------
# Web AI-yordamchisi uchun qo'shimcha "ohang" ko'rsatmasi (2026-08, NotebookLM
# orqali o'rganilgan Chatplace platformasi yondashuvi asosida). FAQAT web
# vidjeti (`/api/assistant`) uchun ishlatiladi -- Telegram bot o'zining
# xom KNOWLEDGE_BASE'ini o'zgarishsiz davom ettiradi, chunki bu ikkalasi
# alohida "shaxs" sifatida ko'rilgan (Telegram -- ichki jamoa uchun tezkor
# hisobot boti, web vidjet -- CRM ichidagi yordamchi).
#
# `[[UNANSWERED]]` -- modelga JAVOBINING OXIRIGA qo'shishni buyuruvchi
# yashirin belgi (foydalanuvchiga ko'rsatilmaydi, `api_assistant()` uni
# aniqlab olib tashlaydi va savolni admin ko'rishi uchun
# `AssistantUnanswered` jadvaliga yozadi) -- Chatplace'dagi "botda yo'q
# savolni admin uchun alohida ro'yxatga ajratish" tamoyilining analogi.
# ---------------------------------------------------------------------------
WEB_ASSISTANT_PERSONA = """Sen "Target CRM" tizimi ichidagi AI-yordamchisan. Suhbatdoshing -- shu CRM'dan foydalanayotgan menejer yoki admin. Quruq, robot kabi emas, balki iliq, samimiy va professional ohangda -- xuddi jonli, tajribali hamkasb kabi gaplash.

Agar pastdagi BILIM BAZASI'da so'ralgan savolga ANIQ javob TOPILMASA:
- Hech qachon o'zingdan taxmin qilib, noaniq yoki noto'g'ri bo'lishi mumkin bo'lgan ma'lumot to'qib chiqarma.
- Javobingni shunday boshla: "Menda bu bo'yicha aniq ma'lumot yo'q, lekin ..." va bilganingcha eng yaqin foydali narsani taklif qil.
- Javobingning ENG OXIRIGA (boshqa hech qayerga emas) qatordan keyin aynan shu belgini qo'sh: [[UNANSWERED]]

Agar bilim bazasida yoki suhbat tarixida javob aniq bo'lsa -- oddiygina, ishonchli javob ber, [[UNANSWERED]] belgisini HECH QACHON qo'shma."""


def _web_assistant_system_prompt() -> str:
    return f"{WEB_ASSISTANT_PERSONA}\n\n---\n\n# BILIM BAZASI\n\n{KNOWLEDGE_BASE}"


def _log_unanswered_question(session, manager_name: str | None, question: str) -> None:
    try:
        manager_row = session.query(Manager).filter_by(username=current_user.username).first() if current_user.is_authenticated else None
        session.add(AssistantUnanswered(
            manager_id=manager_row.id if manager_row else None,
            manager_name=manager_name,
            question=question[:2000],
        ))
        session.commit()
    except Exception:
        logger.exception("Javobsiz savolni saqlashda xatolik")


# ---------------------------------------------------------------------------
# Conversions API (CAPI) -- lead-sifat/sotuv signalini Meta'ga qayta yuborish
# (2026-08, NotebookLM orqali o'rganilgan "Vena AI" konsepsiyasi asosida).
# Har doim try/except bilan o'raladi -- CAPI ulanmagan yoki vaqtincha
# ishlamayotgan bo'lishi CRM'ning asosiy amalini (status/sotuv saqlash)
# HECH QACHON to'xtatmasligi kerak.
# ---------------------------------------------------------------------------

def _send_capi_lead_signal(lead, event_name: str, *, value: float | None = None, event_id_suffix: str = "") -> None:
    if not meta_api.is_capi_configured():
        return
    try:
        meta_api.send_conversion_event(
            event_name,
            phone=lead.phone,
            email=lead.email,
            lead_id=lead.meta_lead_id,
            event_id=f"lead-{lead.id}-{event_name.lower()}{event_id_suffix}",
            value=value,
        )
    except Exception:
        logger.exception("CAPI signalini yuborishda xatolik (lead_id=%s, event=%s)", lead.id, event_name)


# ---------------------------------------------------------------------------
# Flask-Login user wrapper
# ---------------------------------------------------------------------------

class ManagerUser(UserMixin):
    def __init__(self, manager: Manager):
        self.id = str(manager.id)
        self.username = manager.username
        self.full_name = manager.full_name
        self.role = manager.role
        self.phone_number = manager.phone_number
        self.allowed_modules = permissions.parse_allowed_modules(manager.allowed_modules)


@login_manager.user_loader
def load_user(user_id):
    session = get_session()
    try:
        m = session.get(Manager, int(user_id))
        return ManagerUser(m) if m and m.is_active else None
    finally:
        session.close()


def admin_required(fn):
    from functools import wraps

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != "admin":
            flash("Bu sahifa faqat admin uchun.", "error")
            return redirect(url_for("dashboard"))
        return fn(*args, **kwargs)

    return wrapper


def module_required(key: str):
    """Berilgan bo'lim (masalan "dashboard", "analytics") uchun kirish
    huquqini tekshiradi -- ADMIN uchun har doim ochiq, MENEJER uchun faqat
    admin `manager_edit` sahifasida shu bo'limni yoqib qo'ygan bo'lsa."""
    from functools import wraps

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not permissions.has_module(current_user, key):
                flash("Bu bo'limga kirish huquqingiz yo'q. Administratorga murojaat qiling.", "error")
                return redirect(url_for("leads_list") if "leads" in getattr(current_user, "allowed_modules", []) else url_for("logout"))
            return fn(*args, **kwargs)

        return wrapper

    return decorator


app.jinja_env.globals["has_module"] = permissions.has_module


def _format_som(value) -> str:
    """Pul miqdorini "4 000 000 so'm" ko'rinishida formatlaydi (KPI/bonus va
    sotuv summalari SO'M da -- Meta reklama xarajati/CPL esa hisob valyutasi
    bo'yicha $ da qoladi, bular ikki xil narsa)."""
    try:
        n = float(value or 0)
    except (TypeError, ValueError):
        return str(value)
    sign = "-" if n < 0 else ""
    return f"{sign}{abs(n):,.0f}".replace(",", " ") + " so'm"


app.jinja_env.filters["som"] = _format_som


# ---------------------------------------------------------------------------
# Telegram yordamchi funksiyalar
# ---------------------------------------------------------------------------

def tg_send(chat_id: int, text: str) -> None:
    import requests
    for i in range(0, len(text), 4000):
        chunk = text[i:i + 4000]
        try:
            requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": chunk}, timeout=20)
        except Exception:
            logger.exception("Telegramga xabar yuborishda xatolik")


def tg_send_document(chat_id: int, filename: str, file_bytes: bytes, caption: str = "") -> bool:
    import requests
    try:
        r = requests.post(
            f"{TELEGRAM_API}/sendDocument",
            data={"chat_id": chat_id, "caption": caption[:1024]},
            files={"document": (filename, file_bytes, "application/pdf")},
            timeout=55,
        )
        return bool(r.json().get("ok"))
    except Exception:
        logger.exception("Hujjat yuborishda xatolik")
        return False


def _conv_key(chat_id: int) -> str:
    return f"conv:{chat_id}"


def get_history(chat_id: int) -> list[dict]:
    return kv_store.get_json(_conv_key(chat_id), default=[])


def save_history(chat_id: int, history: list[dict]) -> None:
    kv_store.set_json(_conv_key(chat_id), history[-10:])


def _daily_report_targets() -> list[int]:
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


WELCOME_TEXT = (
    "\U0001F44B Salom! Men — Targetolog.\n\n"
    "Oddiy odam bilan gaplashgandek yozavering. Masalan:\n"
    "\"IELTS kursi uchun yangi target yoq, kunlik $20, Toshkent\"\n"
    "\"AB | Traffic | IG reklamani to'xtat\"\n"
    "\"hisobim qanday ketyapti\"\n"
    "\"AB | Traffic | IG ni har kuni 22:00 dan 08:00 gacha o'chirib tur\" — "
    "shunday deysiz, keyin buni har kuni o'zim eslab, avtomatik bajarib "
    "boraman (qayta buyruq berishingiz shart emas). Ro'yxatini ko'rish uchun "
    "/vazifalar, bekor qilish uchun /vazifa_off <ID>.\n\n"
    "Har kuni 09:00 da o'zim hisobot yuboraman, kerak bo'lsa byudjet/"
    "on-off qarorlarini o'zim qabul qilaman."
)


def _run_heavy_in_background(chat_id: int, user_text: str, history_text: str, verdict: str) -> None:
    """Render DOIMIY jarayon bo'lgani uchun (Vercel'dagi kabi serverless
    timeout yo'q), OG'IR (ACTION/ANALYSIS) buyruqlarni oddiy fon THREAD'da
    bajaramiz -- Vercel versiyasidagi murakkab "o'zimizga ichki HTTP so'rov
    yuborish" hiylasi butunlay keraksiz bo'lib qoladi."""
    try:
        result = orchestrator.execute_intent(verdict, user_text, history_text, chat_id)
    except Exception as e:
        logger.exception("Fon ishida xatolik")
        tg_send(chat_id, f"⚠️ Fon ishida kutilmagan xatolik yuz berdi: {e}")
        return
    if result is None:
        result = "Tushunmadim, aniqroq yozib qayta yuboring."
    history = get_history(chat_id)
    history.append({"role": "assistant", "content": result})
    save_history(chat_id, history)
    kv_store.set_json(f"last_report:{chat_id}", result)
    tg_send(chat_id, result)


def handle_free_text(chat_id: int, user_text: str) -> None:
    history = get_history(chat_id)
    budget_tracker.set_notify_chat_id(chat_id)

    if monthly_report.is_monthly_report_request(user_text):
        try:
            since, until, period_label = monthly_report.resolve_monthly_period(user_text)
            data = monthly_report.gather_monthly_report_data(since, until, period_label)
            pdf_bytes = monthly_report.render_monthly_report_pdf(data)
            sent = tg_send_document(
                chat_id, f"oylik_hisobot_{since}_{until}.pdf", pdf_bytes,
                caption=f"\U0001F4CA Oylik target hisoboti: {period_label}",
            )
            if not sent:
                tg_send(chat_id, "⚠️ PDF yuborishda xatolik yuz berdi.")
        except Exception as e:
            logger.exception("Oylik hisobot xatosi")
            tg_send(chat_id, f"⚠️ Oylik hisobotni tayyorlashda xatolik: {e}")
        return

    try:
        verdict, history_text = orchestrator.classify_intent(user_text, history)
    except Exception as e:
        logger.exception("classify_intent xatosi")
        tg_send(chat_id, f"⚠️ Xabarni tushunishda xatolik: {e}")
        return

    history.append({"role": "user", "content": user_text})
    save_history(chat_id, history)

    if orchestrator.is_heavy_intent(verdict):
        tg_send(chat_id, "⏳ Qabul qildim, ishlab chiqyapman...")
        thread = threading.Thread(
            target=_run_heavy_in_background,
            args=(chat_id, user_text, history_text, verdict),
            daemon=True,
        )
        thread.start()
        return

    try:
        result = orchestrator.execute_intent(verdict, user_text, history_text, chat_id)
    except Exception as e:
        logger.exception("execute_intent xatosi")
        tg_send(chat_id, f"⚠️ Buyruqni bajarishda xatolik: {e}")
        return

    if result is not None:
        kv_store.set_json(f"last_report:{chat_id}", result)
        history.append({"role": "assistant", "content": result})
        save_history(chat_id, history)
        tg_send(chat_id, result)
        return

    try:
        answer = orchestrator.call_light_chat(KNOWLEDGE_BASE, history, max_tokens=1000)
    except Exception as e:
        logger.exception("Yengil chat xatosi")
        answer = f"⚠️ Xatolik yuz berdi: {e}"
    history.append({"role": "assistant", "content": answer})
    save_history(chat_id, history)
    tg_send(chat_id, answer)


def handle_command(chat_id: int, cmd: str, args: list[str]) -> None:
    if cmd == "/start":
        kv_store.set_json(_conv_key(chat_id), [])
        budget_tracker.set_notify_chat_id(chat_id)
        tg_send(chat_id, WELCOME_TEXT)
        return
    if cmd == "/status":
        tg_send(chat_id, kv_store.get_json(f"last_report:{chat_id}", default="Hali tahlil ishga tushirilmagan."))
        return
    if cmd == "/analyze":
        tg_send(chat_id, "⏳ Hisobni tahlil qilyapman...")
        thread = threading.Thread(
            target=lambda: tg_send(chat_id, orchestrator.run_analysis_cycle(dry_run=False)),
            daemon=True,
        )
        thread.start()
        return
    if cmd == "/pause" and args:
        try:
            meta_api.pause_object(args[0])
            tg_send(chat_id, f"⏸ {args[0]} to'xtatildi.")
        except meta_api.MetaAPIError as e:
            tg_send(chat_id, f"⚠️ Xatolik: {e}")
        return
    if cmd == "/resume" and args:
        try:
            meta_api.activate_object(args[0])
            tg_send(chat_id, f"▶️ {args[0]} ishga tushirildi.")
        except meta_api.MetaAPIError as e:
            tg_send(chat_id, f"⚠️ Xatolik: {e}")
        return
    if cmd == "/vazifalar":
        tg_send(chat_id, _format_standing_tasks_text())
        return
    if cmd == "/vazifa_off" and args:
        _deactivate_standing_task(chat_id, args[0])
        return
    if cmd == "/id":
        tg_send(
            chat_id,
            f"Sizning Telegram ID'ingiz: {chat_id}\n\n"
            "Buni CRM'dagi \"Menejerlar\" bo'limida shu hisobingizning "
            "\"Telegram ID\" maydoniga kiritib qo'ying (admin ham kiritib "
            "berishi mumkin) -- shundan keyin \"Qayta aloqa\" eslatmalari "
            "har kuni shaxsan shu yerga yuboriladi.",
        )
        return
    tg_send(chat_id, "Noma'lum buyruq. /start yozing.\n\nQo'shimcha buyruqlar: /vazifalar (doimiy vazifalar ro'yxati), /vazifa_off <ID> (birini bekor qilish), /id (Telegram ID'ingizni ko'rsatish).")


def _format_standing_tasks_text() -> str:
    """`/vazifalar` buyrug'iga javob -- barcha FAOL doimiy (schedule_on_off/
    schedule_report) vazifalarni ro'yxat qiladi. MUHIM: ID'lar oldiga `T`
    (task) yoki `R` (report) prefiksi qo'yiladi -- ikkala jadval alohida
    o'zining ID ketma-ketligidan boshlangani uchun (masalan T1 va R1 ikki
    XIL yozuv bo'lishi mumkin), prefikssiz bare-ID bilan `/vazifa_off`
    qaysi jadvalga tegishli ekanini bilolmay, XATO yozuvni bekor qilib
    qo'yishi mumkin edi -- shu bug oldini olish uchun ID'lar ENDI hech qachon
    bare raqam sifatida ko'rsatilmaydi/qabul qilinmaydi."""
    from db import StandingTask, StandingReport
    session = get_session()
    try:
        tasks = session.query(StandingTask).filter_by(is_active=True).order_by(StandingTask.id).all()
        reports = session.query(StandingReport).filter_by(is_active=True).order_by(StandingReport.id).all()
        lines = ["\U0001F4CB Faol doimiy vazifalar:\n"]
        if not tasks and not reports:
            lines.append("Hozircha yo'q.")
        if tasks:
            lines.append("⏰ Avtomatik yoqish/o'chirish:")
            for t in tasks:
                lines.append(f"  T{t.id} — {t.object_name or t.object_id}: {t.on_time} da yoqiladi, {t.off_time} da o'chadi")
        if reports:
            lines.append("\n\U0001F4CA Qo'shimcha hisobot vaqtlari:")
            for r in reports:
                lines.append(f"  R{r.id} — har kuni soat {r.time_hhmm}" + (f" ({r.label})" if r.label else ""))
        lines.append("\nBekor qilish uchun: /vazifa_off T1 (yoki R1) -- yuqoridagi ID bilan.")
        return "\n".join(lines)
    finally:
        session.close()


def _deactivate_standing_task(chat_id: int, raw_id: str) -> None:
    """`raw_id` — `T<id>` (StandingTask) yoki `R<id>` (StandingReport)
    prefiksli identifikator (`/vazifalar` chiqargani bilan bir xil). Prefikssiz
    bare raqam qabul qilinmaydi -- ikki jadval ID'lari mos kelib qolib,
    noto'g'ri yozuv bekor qilinishining oldini olish uchun ataylab shunday."""
    from db import StandingTask, StandingReport
    raw = (raw_id or "").strip().upper()
    if len(raw) < 2 or raw[0] not in ("T", "R") or not raw[1:].isdigit():
        tg_send(chat_id, "ID formati noto'g'ri. Masalan: /vazifa_off T3 yoki /vazifa_off R2 (aniq ID'ni /vazifalar orqali ko'ring).")
        return
    kind, item_id = raw[0], int(raw[1:])
    model = StandingTask if kind == "T" else StandingReport
    session = get_session()
    try:
        obj = session.get(model, item_id)
        if not obj or not obj.is_active:
            tg_send(chat_id, f"{raw} topilmadi yoki allaqachon bekor qilingan. /vazifalar bilan ro'yxatni tekshiring.")
            return
        obj.is_active = False
        session.commit()
        if kind == "T":
            tg_send(chat_id, f"✅ {raw} vazifasi ({obj.object_name or obj.object_id}) bekor qilindi.")
        else:
            tg_send(chat_id, f"✅ {raw} qo'shimcha hisobot vazifasi bekor qilindi.")
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Web AI yordamchi (o'ng pastdagi dumaloq tugma orqali ochiladigan chat)
#
# HAR BIR foydalanuvchi (menejer ham, admin ham) savol-javob qila oladi --
# bu qism DOIM arzon: METRIC (haqiqiy Meta/CRM ma'lumotidan, LLM'siz,
# `build_admin_report` orqali) yoki GENERAL (OpenAI, `call_light_chat`)
# yo'liga tushadi. FAQAT ADMIN uchun esa ACTION (target yoqish/o'chirish,
# doimiy vazifa qo'shish/bekor qilish) va ANALYSIS (to'liq audit, Claude
# Sonnet ishlatadi) ham ochiq -- xuddi Telegram botga yozgandagidek. Oddiy
# menejer bunday buyruq yozsa, xavfsizlik uchun bajarilmaydi -- shunday
# xabar bilan javob qaytariladi (haqiqiy Meta hisobini menejer bevosita
# o'zgartira olmasligi kerak).
# ---------------------------------------------------------------------------

def _web_chat_history_key() -> str:
    return f"web_chat_history:{current_user.id}"


@app.route("/api/assistant", methods=["POST"])
@login_required
def api_assistant():
    data = request.get_json(silent=True) or {}
    user_text = (data.get("message") or "").strip()
    if not user_text:
        return jsonify({"error": "Xabar bo'sh."}), 400
    user_text = user_text[:2000]

    history_key = _web_chat_history_key()
    history = kv_store.get_json(history_key, default=[])
    is_admin = current_user.role == "admin"

    try:
        verdict, history_text = orchestrator.classify_intent(user_text, history)
    except Exception as e:
        logger.exception("Web yordamchi: classify_intent xatosi")
        return jsonify({"reply": f"⚠️ Xabarni tushunishda xatolik: {e}"})

    history.append({"role": "user", "content": user_text})

    if not is_admin and any(k in verdict for k in ("ACTION", "ANALYSIS", "BUDGET")):
        reply = (
            "Bu turdagi amal (target yoqish/o'chirish, byudjet qayd etish yoki "
            "to'liq tahlil) faqat ADMIN akkaunt uchun ochiq. Menejer sifatida "
            "faqat ma'lumot/statistika so'rashingiz mumkin -- masalan \"bugun "
            "necha lead keldi\" yoki \"CPL qancha\"."
        )
        history.append({"role": "assistant", "content": reply})
        kv_store.set_json(history_key, history[-12:])
        return jsonify({"reply": reply, "is_admin": is_admin})

    # ADMIN uchun doimiy vazifalar (schedule_on_off/schedule_report)
    # to'g'ri "chat"ga bog'lanishi uchun sun'iy, lekin barqaror chat_id --
    # Telegram'ning haqiqiy ID maydoni bilan HECH QACHON to'qnashmasligi
    # uchun ataylab juda katta MANFIY son qilib tuzilgan (Telegram foydalanuvchi
    # ID'lari doim musbat). Web'dan yaratilgan vazifalar /settings/tasks
    # sahifasida ko'rinadi; Telegram'ga bildirishnoma yuborilmaydi (haqiqiy
    # Telegram chat yo'q) -- bu kutilgan holat, xato emas.
    web_chat_id = -(9_000_000_000_000 + int(current_user.id)) if is_admin else None

    try:
        result = orchestrator.execute_intent(verdict, user_text, history_text, web_chat_id)
    except Exception as e:
        logger.exception("Web yordamchi: execute_intent xatosi")
        result = f"⚠️ Buyruqni bajarishda xatolik: {e}"

    unanswered = False
    if result is None:
        try:
            result = orchestrator.call_light_chat(_web_assistant_system_prompt(), history, max_tokens=800)
        except Exception as e:
            logger.exception("Web yordamchi: call_light_chat xatosi")
            result = f"⚠️ Xatolik yuz berdi: {e}"
        if result and "[[UNANSWERED]]" in result:
            unanswered = True
            result = result.replace("[[UNANSWERED]]", "").strip()

    history.append({"role": "assistant", "content": result})
    kv_store.set_json(history_key, history[-12:])

    if unanswered:
        session = get_session()
        try:
            _log_unanswered_question(session, current_user.full_name or current_user.username, user_text)
        finally:
            session.close()

    return jsonify({"reply": result, "is_admin": is_admin})


@app.route("/api/webhook", methods=["POST"])
def webhook():
    update = request.get_json(silent=True) or {}
    message = update.get("message") or update.get("edited_message")
    if not message or "text" not in message:
        return jsonify({"ok": True})

    chat_id = message["chat"]["id"]
    text = message["text"].strip()
    try:
        if text.startswith("/"):
            parts = text.split()
            handle_command(chat_id, parts[0].split("@")[0], parts[1:])
        else:
            handle_free_text(chat_id, text)
    except Exception:
        logger.exception("Webhook xatosi")
        tg_send(chat_id, "⚠️ Kutilmagan ichki xatolik yuz berdi.")
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        session = get_session()
        try:
            m = session.query(Manager).filter_by(username=username, is_active=True).first()
            if m and m.check_password(password):
                login_user(ManagerUser(m))
                return redirect(url_for("dashboard"))
        finally:
            session.close()
        flash("Login yoki parol xato.", "error")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Dashboard -- kompaniya bo'yicha UMUMIY KO'RINISH (lidlar holati, shu oy
# sotuv/oborot, menejerlar reytingi, qo'ng'iroq faolligi). Meta target/
# xarajat statistikasi endi ALOHIDA "/target" sahifasida (`target_page`).
# ---------------------------------------------------------------------------

@app.route("/")
@login_required
@module_required("dashboard")
def dashboard():
    period = request.args.get("period", "this_month")
    date_from = (request.args.get("date_from") or "").strip() or None
    date_to = (request.args.get("date_to") or "").strip() or None
    session = get_session()
    try:
        overview = _build_dashboard_overview(session, period=period, date_from=date_from, date_to=date_to)
    finally:
        session.close()
    return render_template("dashboard.html", o=overview)


_DASHBOARD_PERIOD_LABELS = {
    "today": "Bugun", "last_7d": "So'nggi 7 kun",
    "last_30d": "So'nggi 30 kun", "last_90d": "So'nggi 90 kun",
}


def _dashboard_period_bounds(period: str, date_from: str | None, date_to: str | None, month_start, month_end):
    """Dashboard'ning tepadagi KPI kartochkalari ("Yangi lidlar"/"Sotuvlar"/
    "Oborot") qaysi sana oralig'i uchun hisoblanishini aniqlaydi.

    MUHIM (2026-08, foydalanuvchi so'rovi: "sana bo'yicha ko'rish ham qo'shish
    kerak, nechta lead tushdi bugun bilib olish uchun, sana oralig'ini ham
    ko'rish mumkin bo'lsin, Meta target'ga o'xshab"): ilgari Dashboard
    BUTUNLAY qattiq joriy taqvim oyiga bog'langan edi -- "bugun" yoki boshqa
    biror davrni alohida ko'rish imkoni yo'q edi. Endi Target sahifasidagi
    kabi tayyor davrlar ('today'/'last_7d'/'last_30d'/'last_90d') + 'custom'
    (Meta Ads Manager'dagi kabi aniq sanalar oralig'i) qo'llab-quvvatlanadi,
    standart esa avvalgidek 'this_month' (joriy oy)."""
    if period == "custom" and date_from and date_to:
        bounds = custom_range_bounds_utc(date_from, date_to)
        if bounds:
            return bounds[0], bounds[1], f"{date_from} — {date_to}"
    if period in _DASHBOARD_PERIOD_LABELS:
        bounds = _date_preset_bounds_utc(period)
        if bounds:
            return bounds[0], bounds[1], _DASHBOARD_PERIOD_LABELS[period]
    return month_start, month_end, "Shu oy"


def _build_dashboard_overview(session, period: str = "this_month", date_from: str | None = None, date_to: str | None = None) -> dict:
    """Bosh sahifa ("Dashboard") uchun butun kompaniya bo'yicha umumiy
    ko'rinishni yig'adi -- lidlar holati (voronka taqsimoti), tanlangan davr
    bo'yicha yangi lid/sotuv/oborot, shu oydagi kunlik sotuv taqsimoti,
    menejerlar reytingi (KPI/bonus, har doim JORIY OYGA bog'liq -- KPI/bonus
    rejalari oylik bo'lgani uchun) va qo'ng'iroq faolligi (Moi Zvonki
    ulangan bo'lsa). `dashboard.html` shundan Chart.js diagrammalarini
    quradi."""
    now = dt.datetime.utcnow()
    year, month = now.year, now.month
    month_start, month_end = kpi_bonus.month_bounds(year, month)
    period_start, period_end, period_label = _dashboard_period_bounds(period, date_from, date_to, month_start, month_end)

    # 1) Lidlar holati -- voronka bosqichlari bo'yicha taqsimot
    stages = _active_funnel_stages(session)
    color_hex = {"blue": "#2563EB", "good": "#059669", "bad": "#DC2626", "warn": "#D97706", "dim": "#94A3B8"}
    all_leads = session.query(Lead.id, Lead.status, Lead.created_at).all()
    total_leads = len(all_leads)
    leads_in_period = sum(1 for l in all_leads if l.created_at and period_start <= l.created_at < period_end)
    status_raw_counts: dict[str, int] = defaultdict(int)
    for l in all_leads:
        status_raw_counts[l.status] += 1
    status_breakdown = [
        {
            "key": s.key, "label": s.label, "count": status_raw_counts.get(s.key, 0),
            "color": color_hex.get(s.color, "#94A3B8"),
        }
        for s in stages
    ]

    sold_key = _sold_stage_key(stages)
    sold_total = status_raw_counts.get(sold_key, 0) if sold_key else 0
    conversion_pct = round((sold_total / total_leads) * 100, 1) if total_leads else 0.0

    # 2) Shu oydagi sotuvlar -- barcha menejerlar, kunlik taqsimot (diagramma
    # HAR DOIM joriy oy uchun, tanlangan davrdan qat'i nazar -- KPI/bonus
    # va "Sotuvlar dinamikasi" grafigi oylik tushunchaga bog'liq).
    def _sales_in_range(start, end):
        return (
            session.query(Sale)
            .filter(
                Sale.is_returned == False,  # noqa: E712
                Sale.amount >= kpi_bonus.get_min_sale_amount(),
                Sale.sold_at >= start, Sale.sold_at < end,
            )
            .all()
        )

    sales_this_month = _sales_in_range(month_start, month_end)

    # Tepadagi KPI kartochkalari ("Sotuvlar"/"Oborot") uchun -- TANLANGAN
    # davr bo'yicha. Standart holatda (period="this_month") bu xuddi
    # yuqoridagi bilan bir xil oraliq, shuning uchun qayta so'ramaymiz.
    if period_start == month_start and period_end == month_end:
        sales_in_period = sales_this_month
    else:
        sales_in_period = _sales_in_range(period_start, period_end)
    sales_count_period = len(sales_in_period)
    turnover_period = sum(s.amount for s in sales_in_period)

    daily_turnover: dict[str, float] = defaultdict(float)
    for s in sales_this_month:
        if s.sold_at:
            daily_turnover[s.sold_at.strftime("%Y-%m-%d")] += s.amount
    today_key = now.strftime("%Y-%m-%d")
    sales_trend = []
    cur = month_start
    while cur < month_end:
        key = cur.strftime("%Y-%m-%d")
        if key > today_key:
            break
        sales_trend.append({"date": key, "day_label": cur.strftime("%d.%m"), "turnover": daily_turnover.get(key, 0.0)})
        cur += dt.timedelta(days=1)

    # 3) Menejerlar reytingi -- shu oy KPI/bonus
    managers_all = session.query(Manager).filter_by(role="manager", is_active=True).all()
    leaderboard = []
    total_bonus_month = 0.0
    for m in managers_all:
        r = _build_manager_kpi_report(session, m, year, month)
        bonus_total = r["bonus_a"] + r["bonus_b"] + r["bonus_c"]
        total_bonus_month += bonus_total
        leaderboard.append({
            "manager_id": r["manager_id"], "manager_name": r["manager_name"], "sales_count": r["sales_count"],
            "turnover": r["turnover"], "bonus_total": bonus_total, "jami": r["jami"],
            "sales_to_next_tier": r["sales_to_next_tier"],
            "next_progressive_tier": r["next_progressive_tier"],
            "projected_total_at_next_sales_tier": r["projected_total_at_next_sales_tier"],
            "turnover_to_next_milestone": r["turnover_to_next_milestone"],
            "next_turnover_milestone_amount": r["next_turnover_milestone_amount"],
            "projected_total_at_next_turnover_milestone": r["projected_total_at_next_turnover_milestone"],
        })
    leaderboard.sort(key=lambda x: x["turnover"], reverse=True)
    max_leaderboard_turnover = max((l["turnover"] for l in leaderboard), default=0) or 1

    # 4) Qo'ng'iroq faolligi -- shu oy, butun jamoa (Moi Zvonki ulangan bo'lsa)
    calls_overview = call_analytics.build_team_daily_call_counts(
        session, month_start, dt.datetime.strptime(today_key, "%Y-%m-%d") + dt.timedelta(days=1),
        norm_per_manager=kpi_bonus.DAILY_CALLS_NORM,
    )

    # 5) Qayta aloqa (follow-up) -- bugun va muddati o'tgan, ENG YAQINLARI
    # tepada -- Dashboard'da darhol ko'rinishi uchun ("agent o'zi eslatib
    # tursin" talabi shu kartochka + navbar belgisi orqali qoplanadi).
    today_end = now.replace(hour=23, minute=59, second=59, microsecond=0)
    due_followups = (
        session.query(Lead)
        .filter(Lead.next_contact_at.isnot(None), Lead.next_contact_at <= today_end)
        .order_by(Lead.next_contact_at.asc())
        .all()
    )
    followups_overdue = sum(1 for l in due_followups if l.next_contact_at.date() < now.date())
    followups_today = sum(1 for l in due_followups if l.next_contact_at.date() == now.date())
    followups_preview = [{
        "id": l.id, "full_name": l.full_name or "Noma'lum", "phone": l.phone,
        "assigned_manager": l.assigned_manager.full_name if l.assigned_manager else "—",
        "next_contact_at": l.next_contact_at,
        "is_overdue": l.next_contact_at.date() < now.date(),
    } for l in due_followups[:6]]

    return {
        "generated_at": now.isoformat(),
        "total_leads": total_leads,
        "leads_in_period": leads_in_period,
        "period": period,
        "period_label": period_label,
        "date_from": date_from,
        "date_to": date_to,
        "status_breakdown": status_breakdown,
        "sold_total": sold_total,
        "conversion_pct": conversion_pct,
        "sales_count_period": sales_count_period,
        "turnover_period": turnover_period,
        "sales_trend": sales_trend,
        "leaderboard": leaderboard,
        "max_leaderboard_turnover": max_leaderboard_turnover,
        "total_bonus_month": round(total_bonus_month),
        "calls_overview": calls_overview,
        "followups_overdue": followups_overdue,
        "followups_today": followups_today,
        "followups_preview": followups_preview,
    }


# ---------------------------------------------------------------------------
# Target -- Meta reklama target/xarajat statistikasi (Campaigns/Ad sets/Ads
# jadvali). Ilgari shu sahifa "/" (Dashboard) edi -- endi alohida, chunki
# "Dashboard" endi butun kompaniya bo'yicha umumiy ko'rinish bo'ldi.
# ---------------------------------------------------------------------------

@app.route("/target")
@login_required
@module_required("target")
def target_page():
    period = request.args.get("period", "last_30d")
    level = request.args.get("level", "campaign")
    if level not in ("campaign", "adset", "ad"):
        level = "campaign"
    show_all = request.args.get("show_all") == "1"
    try:
        data = get_kpis(level=level, date_preset=period, active_only=not show_all)
    except Exception as e:
        logger.exception("Target: Meta ma'lumotlarini olishda xato")
        data = {"error": str(e), "rows": [], "totals": {}, "goal_breakdown": [], "generated_at": dt.datetime.utcnow().isoformat(), "level": level}
    return render_template("target.html", data=data, period=period, level=level, show_all=show_all)


# ---------------------------------------------------------------------------
# Analitika -- barcha hisobotlarni asta-sekin to'plab boradigan umumiy bo'lim
# (target/xarajat, menejerlar faolligi, qo'ng'iroq statistikasi).
# ---------------------------------------------------------------------------

@app.route("/analitika")
@login_required
@module_required("analytics")
def analytics_page():
    period = request.args.get("period", "last_30d")
    try:
        target_data = get_kpis(level="campaign", date_preset=period, active_only=True)
    except Exception as e:
        logger.exception("Analitika: target ma'lumotlarini olishda xato")
        target_data = None

    session = get_session()
    try:
        since = dt.datetime.utcnow() - dt.timedelta(days=30)
        managers_all = session.query(Manager).filter_by(is_active=True).all()
        managers_by_id = {m.id: m for m in managers_all}

        # Menejerlar bo'yicha kunlik faollik -- LeadNote (izoh/holat o'zgarishi
        # yozilgan har safar "shu menejer shu kuni ish qildi" deb hisoblanadi).
        notes = session.query(LeadNote).filter(LeadNote.created_at >= since, LeadNote.manager_id.isnot(None)).all()
        daily_by_manager = {}
        for n in notes:
            mname = managers_by_id.get(n.manager_id)
            mname = (mname.full_name or mname.username) if mname else f"ID {n.manager_id}"
            day = n.created_at.strftime("%Y-%m-%d")
            daily_by_manager.setdefault(mname, {}).setdefault(day, 0)
            daily_by_manager[mname][day] += 1

        manager_activity = []
        for mname, by_day in daily_by_manager.items():
            total = sum(by_day.values())
            days_active = len(by_day)
            manager_activity.append({
                "manager_name": mname,
                "total_actions": total,
                "days_active": days_active,
                "avg_per_day": round(total / days_active, 1) if days_active else 0,
            })
        manager_activity.sort(key=lambda m: m["total_actions"], reverse=True)

        # Lidlar bo'yicha: necha nafari kuniga qanday kelgan / holat taqsimoti.
        leads_since = session.query(Lead).filter(Lead.created_at >= since).all()
        leads_by_day = {}
        for l in leads_since:
            day = (l.created_at or dt.datetime.utcnow()).strftime("%Y-%m-%d")
            leads_by_day[day] = leads_by_day.get(day, 0) + 1
        leads_daily = sorted(leads_by_day.items(), key=lambda kv: kv[0], reverse=True)[:14]

        status_counts = {}
        for l in leads_since:
            status_counts[l.status] = status_counts.get(l.status, 0) + 1

        # Qo'ng'iroq statistikasi (Moi Zvonki ulangan bo'lsa) -- ulanmagan
        # bo'lsa aniq "hali ulanmagan" holati ko'rsatiladi, "hech kim
        # gaplashmagan" deb noto'g'ri talqin qilinmasligi uchun.
        call_configured = call_sync.is_configured()
        call_summary = None
        if call_configured:
            check = call_analytics.build_individual_check(session, since)
            call_summary = {
                "total_sessions": check["total_sessions"],
                "suspicious_count": check["suspicious_count"],
                "has_data": check["has_data"],
                "top_managers": check["manager_summary"][:5],
            }

        # --- KPI/bonus hisobot (oylik, kun/menejer bo'yicha tanlash) -- faqat
        # admin ko'radi, chunki bu maosh/bonus ma'lumoti. ---
        kpi_reports = None
        kpi_month_str = None
        kpi_selected_manager = request.args.get("kpi_manager", "all")
        sales_managers = []
        lead_sync_status = None
        if current_user.role == "admin":
            sales_managers = session.query(Manager).filter_by(role="manager", is_active=True).order_by(Manager.full_name).all()
            now = dt.datetime.utcnow()
            kpi_month_str = request.args.get("kpi_month") or now.strftime("%Y-%m")
            try:
                kpi_year, kpi_month_num = (int(x) for x in kpi_month_str.split("-"))
            except (ValueError, TypeError):
                kpi_year, kpi_month_num = now.year, now.month
                kpi_month_str = now.strftime("%Y-%m")

            target_managers = sales_managers
            if kpi_selected_manager != "all":
                target_managers = [m for m in sales_managers if str(m.id) == kpi_selected_manager]
            kpi_reports = [_build_manager_kpi_report(session, m, kpi_year, kpi_month_num) for m in target_managers]

            lead_sync_status = lead_sync.get_last_status()
    finally:
        session.close()

    return render_template(
        "analytics.html",
        period=period,
        target_data=target_data,
        manager_activity=manager_activity,
        leads_daily=leads_daily,
        status_counts=status_counts,
        total_leads_30d=len(leads_since),
        call_configured=call_configured,
        call_summary=call_summary,
        kpi_reports=kpi_reports,
        kpi_month=kpi_month_str,
        kpi_selected_manager=kpi_selected_manager,
        sales_managers=sales_managers,
        lead_sync_status=lead_sync_status,
    )


# ---------------------------------------------------------------------------
# CRM: lidlar
# ---------------------------------------------------------------------------

def _active_funnel_stages(session):
    """Faol voronka bosqichlarini tartib bo'yicha qaytaradi -- admin
    /settings/funnel'da qo'shgan/o'zgartirgan bosqichlar shu yerdan o'qiladi,
    filter tugmalari va status <select> shularga qarab quriladi."""
    return session.query(FunnelStage).filter_by(is_active=True).order_by(FunnelStage.sort_order).all()


def _build_manager_kpi_report(session, manager, year: int, month: int) -> dict:
    """Bitta menejer uchun bitta oylik KPI/bonus hisobotini yig'adi
    (`kpi_bonus.compute_manager_report()`ga uzatiladigan xom ma'lumotni
    bazadan o'qiydi -- minimal chek va vozvrat filtri shu yerda qo'llanadi)."""
    start, end = kpi_bonus.month_bounds(year, month)
    sales = (
        session.query(Sale)
        .filter(
            Sale.manager_id == manager.id,
            Sale.is_returned == False,  # noqa: E712
            Sale.amount >= kpi_bonus.get_min_sale_amount(),
            Sale.sold_at >= start, Sale.sold_at < end,
        )
        .order_by(Sale.sold_at.asc())
        .all()
    )
    valid_sales = []
    for s in sales:
        days_since_first = None
        if s.sale_number == 2:
            first = session.query(Sale).filter_by(lead_id=s.lead_id, sale_number=1).first()
            if first and first.sold_at and s.sold_at:
                days_since_first = (s.sold_at - first.sold_at).total_seconds() / 86400.0
        valid_sales.append({
            "sale_number": s.sale_number, "amount": s.amount, "sold_at": s.sold_at,
            "days_since_first_sale": days_since_first, "lead_id": s.lead_id,
        })
    report = kpi_bonus.compute_manager_report(valid_sales, year, month, manager.hire_date)
    report["manager_id"] = manager.id
    report["manager_name"] = manager.full_name or manager.username
    report["repeat_customers"] = _build_repeat_customer_breakdown(session, manager.id, valid_sales)
    report["daily_calls"] = call_analytics.build_daily_call_counts(
        session, manager.id, start, end, norm=kpi_bonus.DAILY_CALLS_NORM
    )
    return report


def _build_repeat_customer_breakdown(session, manager_id: int, valid_sales: list) -> dict:
    """"Qayta sotuv KPI (batafsil)" kartochkasi uchun -- shu oyda 15 kun
    ICHIDA 2-marta xarid qilgan (ya'ni "mijozni faollashtirish" bonusining
    2-xarid shartiga to'g'ri kelgan) har bir mijoz uchun 1- va 2-xarid
    qatorlarini, har birining Fixed/0.5%/Jami bonus tafsilotini yig'adi."""
    qualifying_lead_ids = {
        s["lead_id"] for s in valid_sales
        if s["sale_number"] == 2 and s.get("days_since_first_sale") is not None
        and s["days_since_first_sale"] <= kpi_bonus.REPEAT_WINDOW_DAYS
    }
    if not qualifying_lead_ids:
        return {"customers": [], "total": 0.0}

    leads_by_id = {
        l.id: l for l in session.query(Lead).filter(Lead.id.in_(qualifying_lead_ids)).all()
    }

    customers = []
    grand_total = 0.0
    for lead_id in qualifying_lead_ids:
        lead_sales = (
            session.query(Sale)
            .filter(
                Sale.lead_id == lead_id,
                Sale.manager_id == manager_id,
                Sale.is_returned == False,  # noqa: E712
                Sale.sale_number.in_([1, 2]),
            )
            .order_by(Sale.sale_number.asc())
            .all()
        )
        lead = leads_by_id.get(lead_id)
        rows = []
        customer_total = 0.0
        first_sold_at = None
        for s in lead_sales:
            if s.sale_number == 1:
                fixed, first_sold_at = 10_000.0, s.sold_at
            else:
                days_since_first = None
                if first_sold_at and s.sold_at:
                    days_since_first = (s.sold_at - first_sold_at).total_seconds() / 86400.0
                elif s.sale_number == 2:
                    first = next((x for x in lead_sales if x.sale_number == 1), None)
                    if first and first.sold_at and s.sold_at:
                        days_since_first = (s.sold_at - first.sold_at).total_seconds() / 86400.0
                if days_since_first is not None and days_since_first <= kpi_bonus.REPEAT_WINDOW_DAYS:
                    fixed = 20_000.0
                else:
                    fixed = 0.0
            pct = s.amount * 0.005
            total = fixed + pct
            customer_total += total
            rows.append({
                "sale_number": s.sale_number, "amount": s.amount, "sold_at": s.sold_at,
                "fixed": fixed, "pct": pct, "total": total,
            })
        if not rows:
            continue
        grand_total += customer_total
        customers.append({
            "lead_id": lead_id,
            "lead_name": (lead.full_name if lead else None) or "Noma'lum",
            "phone": lead.phone if lead else None,
            "sales": rows,
            "customer_total": customer_total,
        })

    customers.sort(key=lambda c: c["sales"][0]["sold_at"] or dt.datetime.min)
    return {"customers": customers, "total": grand_total}


def _recompute_lead_sale_total(session, lead) -> None:
    """`Lead.sale_amount`/`sold_at`ni shu leadning barcha QAYTARILMAGAN
    `Sale` yozuvlaridan qayta hisoblaydi -- dashboard/eski kod bular orqali
    ishlashda davom etadi, haqiqiy tafsilot esa `Sale` jadvalida saqlanadi."""
    valid_sales = (
        session.query(Sale)
        .filter(Sale.lead_id == lead.id, Sale.is_returned == False)  # noqa: E712
        .order_by(Sale.sold_at.asc())
        .all()
    )
    if valid_sales:
        lead.sale_amount = sum(s.amount for s in valid_sales)
        lead.sold_at = valid_sales[0].sold_at
    else:
        lead.sale_amount = None
        lead.sold_at = None


def _sold_stage_key(stages) -> str | None:
    """Voronkadagi "sold" (sotildi) kategoriyasiga tegishli birinchi
    bosqich kalitini qaytaradi -- sotuv qo'shilganda lead statusi avtomatik
    shu bosqichga o'tkaziladi (agar hali sotilgan deb belgilanmagan bo'lsa)."""
    for s in stages:
        if s.category == "sold":
            return s.key
    return None


@app.route("/leads")
@login_required
@module_required("leads")
def leads_list():
    status_filter = request.args.get("status", "")
    search_q = request.args.get("q", "").strip()
    session = get_session()
    try:
        stages = _active_funnel_stages(session)
        q = session.query(Lead).order_by(Lead.created_at.desc())
        if status_filter:
            q = q.filter(Lead.status == status_filter)
        if search_q:
            like = f"%{search_q}%"
            q = q.filter(
                (Lead.full_name.ilike(like)) | (Lead.phone.ilike(like)) | (Lead.campaign_name.ilike(like))
            )
        leads = q.limit(300).all()
        rows = [{
            "id": l.id, "full_name": l.full_name, "phone": l.phone,
            "campaign_name": l.campaign_name, "adset_name": l.adset_name, "ad_name": l.ad_name,
            "status": l.status, "source": l.source,
            "created_at": l.created_at, "assigned_manager": l.assigned_manager.full_name if l.assigned_manager else None,
            "sale_amount": l.sale_amount,
        } for l in leads]
        stage_rows = [{"key": s.key, "label": s.label} for s in stages]
        stage_color_by_key = {s.key: s.color for s in stages}
        stage_label_by_key = {s.key: s.label for s in stages}
    finally:
        session.close()
    return render_template(
        "leads.html", leads=rows, status_filter=status_filter, search_q=search_q,
        stages=stage_rows, stage_color_by_key=stage_color_by_key, stage_label_by_key=stage_label_by_key,
    )


@app.route("/sotilgan-xaridorlar")
@login_required
@module_required("leads")
def sold_customers_list():
    """"Sotilgan xaridorlar" -- CRM bo'limi ichidagi alohida sahifa (2026-08,
    foydalanuvchi so'rovi: "bo'limni almashtirib CRM qilib, ichiga sotilgan
    xaridorlar qo'shish kerak, 1-xarid/2-xarid filtr bilan").

    `/leads` sahifasidagi "Sotildi" filtri LEAD darajasida ishlaydi (bitta
    qator = bitta lead, hatto o'sha lead necha marta xarid qilgan bo'lsa
    ham) -- bu yerda esa HAR BIR XARID (`Sale`) alohida qator, shuning
    uchun "1-xarid" (birinchi marta xarid qilganlar) va "Takroriy xarid"
    (2-marta va undan ko'p xarid qilganlar) bo'yicha filtrlash mumkin.
    `Sale.sale_number` allaqachon mavjud -- har bir sotuv yozilganda
    "shu LEAD uchun nechinchi sotuv" avtomatik hisoblab qo'yiladi
    (multi-sale-per-lead funksiyasi, ilgari qo'shilgan)."""
    purchase_filter = request.args.get("purchase", "")  # "" | "1" | "2plus"
    search_q = request.args.get("q", "").strip()
    session = get_session()
    try:
        q = (
            session.query(Sale)
            .join(Lead, Sale.lead_id == Lead.id)
            .filter(Sale.is_returned == False, Sale.amount >= kpi_bonus.get_min_sale_amount())  # noqa: E712
            .order_by(Sale.sold_at.desc())
        )
        if purchase_filter == "1":
            q = q.filter(Sale.sale_number == 1)
        elif purchase_filter == "2plus":
            q = q.filter(Sale.sale_number > 1)
        if search_q:
            like = f"%{search_q}%"
            q = q.filter((Lead.full_name.ilike(like)) | (Lead.phone.ilike(like)))
        sales = q.limit(500).all()
        rows = [{
            "id": s.id, "lead_id": s.lead_id,
            "full_name": (s.lead.full_name if s.lead else None) or "Noma'lum",
            "phone": s.lead.phone if s.lead else None,
            "manager_name": s.manager.full_name if s.manager else "—",
            "sale_number": s.sale_number, "amount": s.amount,
            "invoice_number": s.invoice_number, "sold_at": s.sold_at,
        } for s in sales]
        total_amount = sum(r["amount"] for r in rows)
    finally:
        session.close()
    return render_template(
        "sold_customers.html", sales=rows, purchase_filter=purchase_filter,
        search_q=search_q, total_amount=total_amount, total_count=len(rows),
    )


@app.route("/leads/new", methods=["GET", "POST"])
@login_required
@module_required("leads")
def lead_new():
    session = get_session()
    try:
        if request.method == "POST":
            full_name = request.form.get("full_name", "").strip()
            phone = request.form.get("phone", "").strip()
            email = request.form.get("email", "").strip()
            campaign_name = request.form.get("campaign_name", "").strip()
            adset_name = request.form.get("adset_name", "").strip()
            ad_name = request.form.get("ad_name", "").strip()
            if not full_name and not phone:
                flash("Kamida ism yoki telefon kiriting.", "error")
            else:
                lead = Lead(
                    full_name=full_name or None, phone=phone or None, email=email or None,
                    campaign_name=campaign_name or None, adset_name=adset_name or None, ad_name=ad_name or None,
                    source="manual", status="new",
                )
                session.add(lead)
                session.commit()
                flash("Lead qo'shildi.", "success")
                return redirect(url_for("lead_detail", lead_id=lead.id))
    finally:
        session.close()
    return render_template("lead_new.html")


@app.route("/leads/<int:lead_id>/delete", methods=["POST"])
@login_required
@module_required("leads")
def lead_delete(lead_id):
    session = get_session()
    try:
        lead = session.get(Lead, lead_id)
        if lead:
            session.query(LeadNote).filter_by(lead_id=lead.id).delete()
            session.delete(lead)
            session.commit()
            flash("Lead o'chirildi.", "success")
        else:
            flash("Lead topilmadi.", "error")
    finally:
        session.close()
    return redirect(url_for("leads_list"))


ALLOWED_IMPORT_EXTENSIONS = (".xlsx", ".xlsm", ".csv")

# ---------------------------------------------------------------------------
# Excel/CSV import -- "aqlli" ustun aniqlash (2026-08)
#
# Real hayotda import qilinadigan fayllar juda xilma-xil bo'ladi:
#   1. Meta Ads Manager'ning O'ZI eksport qilgan xom CSV -- odatda UTF-16
#      kodlash + TAB bilan ajratilgan, ustun nomlari "phone_number",
#      "ismingizni_kiriting!" kabi FORMA SAVOLI nomiga qarab o'zgaruvchan,
#      ID'lar "ag:"/"as:"/"c:"/"f:"/"l:" prefiks bilan, telefon esa "p:" bilan.
#   2. Admin/menejer o'zi tuzgan Excel jadval -- ustun sarlavhasi UMUMAN
#      YO'Q, faqat qatorlar (hudud, maydon, ism, telefon, telefon).
#   3. Oddiy "full_name, phone, email" ustunli oddiy jadval.
#
# Shu funksiyalar UCHALASINI HAM avtomatik tanib, faqat CRM'ga kerakli
# maydonlarni (ism, telefon -- ba'zan ikkitagacha, email, kampaniya
# atributsiyasi) ajratib oladi -- boshqa ustunlar (hudud, maydon va h.k.)
# yo'qotilmaydi, `quality_note`ga qo'shimcha kontekst sifatida yoziladi.
# ---------------------------------------------------------------------------

_NAME_HINTS = ("full_name", "full name", "ism", "fio", "f.i.o", "familiya", "имя", "фио", "name")
_PHONE_HINTS = ("phone", "telefon", "tel", "raqam", "nomer", "номер", "тел")
_ADDITIONAL_PHONE_HINTS = ("qoshimcha", "qo'shimcha", "additional", "extra", "second", "ikkinchi")
_EMAIL_HINTS = ("email", "e-mail", "почта", "pochta")
_KNOWN_HEADER_TOKENS = (
    "phone", "email", "campaign", "adset", "ad_name", "ad name", "form",
    "created_time", "lead_status", "ism", "full_name", "name", "telefon",
    "kampaniya", "platform", "is_organic", "id",
)


def _import_norm_header(h) -> str:
    return str(h or "").strip().lower()


def _import_looks_like_header_row(cells) -> bool:
    """Birinchi qator SARLAVHA (ustun nomlari) ekanligini aniqlaydi -- agar
    kamida 2 ta ma'lum kalit so'z uchrasa VA hech bir katakda uzun (7+)
    raqam ketma-ketligi (telefon/ID'ga o'xshash) bo'lmasa."""
    joined = " ".join(_import_norm_header(c) for c in cells)
    hits = sum(1 for tok in _KNOWN_HEADER_TOKENS if tok in joined)
    has_long_digit_run = any(re.search(r"\d{7,}", str(c or "")) for c in cells)
    return hits >= 2 and not has_long_digit_run


def _import_clean_phone_raw(raw):
    if raw is None:
        return None
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)
    s = str(raw).strip()
    if not s:
        return None
    # Excel/openpyxl butun sonli katakni ba'zan "900446000.0" ko'rinishida
    # STRINGga aylantirib beradi (bizga kelguncha) -- ".0"ni olib tashlaymiz,
    # aks holda keyingi raqam tozalashda soxta qo'shimcha "0" paydo bo'ladi.
    if re.fullmatch(r"\d+\.0", s):
        s = s[:-2]
    if s.lower().startswith("p:"):
        s = s[2:]
    return s


def _import_normalize_phone(raw):
    """Turli formatdagi telefon qiymatini (xom raqam, "p:" prefiksli,
    tire/bo'sh joy/vergul bilan yozilgan) imkon qadar "+998XXXXXXXXX"
    ko'rinishiga keltiradi. Aniqlab bo'lmasa, faqat raqamlarni qoldirib
    qaytaradi (yo'qotmaslik uchun)."""
    s = _import_clean_phone_raw(raw)
    if not s:
        return None
    has_plus = s.strip().startswith("+")
    digits = re.sub(r"\D", "", s)
    if len(digits) < 7:
        return None
    if has_plus:
        return "+" + digits
    if digits.startswith("998") and len(digits) >= 12:
        return "+" + digits[:12]
    if len(digits) == 9:
        return "+998" + digits
    if len(digits) in (12, 13) and digits.startswith("998"):
        return "+" + digits
    return digits


def _import_phone_key9(normalized):
    if not normalized:
        return None
    digits = re.sub(r"\D", "", normalized)
    return digits[-9:] if len(digits) >= 9 else (digits or None)


def _import_strip_id_prefix(raw):
    """Meta CSV eksportidagi "ag:123", "as:123", "c:123", "f:123", "l:123"
    prefikslarini olib tashlaydi -- shundagina bu ID'lar Meta API orqali
    olingan haqiqiy kampaniya/adset/ad ID'lari bilan MOS TUSHADI (aks holda
    dashboard'da bu lidlar hech qanday kampaniyaga bog'lanmay qoladi)."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if ":" in s:
        prefix, rest = s.split(":", 1)
        if prefix.lower() in ("ag", "as", "c", "f", "l") and rest:
            return rest
    return s


def _import_looks_phoneish(value) -> bool:
    s = _import_clean_phone_raw(value)
    if not s:
        return False
    if s.strip().startswith(("+", "p:")):
        return True
    digits = re.sub(r"\D", "", s)
    return 7 <= len(digits) <= 13 and len(digits) >= len(re.sub(r"[\s]", "", s)) - 3


def _import_looks_nameish(value) -> bool:
    s = str(value or "").strip()
    if not (2 <= len(s) <= 60):
        return False
    if _import_looks_phoneish(value):
        return False
    # Haqiqiy ism/familiyada deyarli hech qachon pastki chiziq, qavs yoki
    # o'lchov belgisi bo'lmaydi -- bular ko'pincha forma javobi KATEGORIYASI
    # (masalan "toshkent_shahri", "50-150_m²") bo'ladi, ism emas.
    if any(ch in s for ch in ("_", "(", ")", "²", "%")):
        return False
    letters = sum(1 for ch in s if ch.isalpha())
    return letters >= max(2, len(s) * 0.5)


def _import_read_rows(file):
    """Faylni (CSV yoki Excel) universal o'qiydi -- CSV uchun kodlashni
    (UTF-8/UTF-16, BOM bilan yoki bo'lmasa) va ajratuvchini (tab/vergul/
    nuqta-vergul) avtomatik aniqlaydi. Qaytaradi: list[list[str|None]]."""
    filename = file.filename.lower()
    if filename.endswith(".csv"):
        raw = file.stream.read()
        text = None
        if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
            text = raw.decode("utf-16", errors="replace")
        else:
            null_ratio = raw[:2000].count(0) / max(1, len(raw[:2000]))
            if null_ratio > 0.15:  # BOM'siz UTF-16 -- ko'p 0x00 baytlar bo'ladi
                try:
                    text = raw.decode("utf-16-le", errors="replace")
                except Exception:
                    text = None
        if text is None:
            text = raw.decode("utf-8-sig", errors="ignore")

        sample = text[:4096]
        delimiter = "\t" if sample.count("\t") > sample.count(",") else ","
        if sample.count(";") > sample.count(delimiter):
            delimiter = ";"

        import csv, io
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        return [list(r) for r in reader]
    else:
        import openpyxl
        wb = openpyxl.load_workbook(file, data_only=True)
        ws = wb.active
        return [[c.value for c in row] for row in ws.iter_rows()]


def _import_resolve_columns(rows):
    """Fayl qatorlarini tahlil qilib, har bir CRM maydoni (ism, asosiy
    telefon, qo'shimcha telefon, email, kampaniya/adset/ad/forma, ID,
    yaratilgan vaqt) qaysi ustun indeksida ekanligini topadi. Qaytaradi:
    (col_map: dict, data_rows: list[list], used_header: bool)."""
    if not rows:
        return {}, [], False

    header_cells = rows[0]
    has_header = _import_looks_like_header_row(header_cells)
    col_map = {}

    if has_header:
        header = [_import_norm_header(h) for h in header_cells]

        def find_exact(*names):
            for n in names:
                if n in header:
                    return header.index(n)
            return None

        def find_substr(hints, exclude_idx=()):
            for i, h in enumerate(header):
                if i in exclude_idx:
                    continue
                if any(hint in h for hint in hints):
                    return i
            return None

        col_map["meta_lead_id"] = find_exact("id")
        col_map["created_time"] = find_exact("created_time", "created time")
        col_map["campaign"] = find_exact("campaign_name", "campaign", "kampaniya")
        col_map["campaign_id"] = find_exact("campaign_id")
        col_map["adset"] = find_exact("adset_name", "adset", "reklama guruhi")
        col_map["adset_id"] = find_exact("adset_id")
        col_map["ad"] = find_exact("ad_name", "ad")
        col_map["ad_id"] = find_exact("ad_id")
        col_map["form"] = find_exact("form_name")

        # Ism/telefon/email'ni Meta'ning O'ZI ATTRIBUTSIYA UCHUN ishlatadigan
        # ad_name/adset_name/campaign_name/form_name ustunlari bilan
        # ARALASHTIRIB YUBORMASLIK uchun -- ular allaqachon band qilingan
        # indekslarni QIDIRUVDAN chiqarib tashlaymiz ("ad_name" ichida ham
        # "name" so'zi bor, lekin bu odam ismi emas).
        reserved = {
            i for i in (
                col_map["meta_lead_id"], col_map["created_time"],
                col_map["campaign"], col_map["campaign_id"],
                col_map["adset"], col_map["adset_id"],
                col_map["ad"], col_map["ad_id"], col_map["form"],
            ) if i is not None
        }

        idx_name = find_exact("full_name", "full name") or find_substr(_NAME_HINTS, exclude_idx=reserved)
        col_map["name"] = idx_name
        if idx_name is not None:
            reserved.add(idx_name)

        # Asosiy telefon: aniq "phone"/"phone_number" nomli ustun ustunroq --
        # "qo'shimcha"/"additional" so'zi bilan boshlanmagan birinchi moslik.
        primary_phone = find_exact("phone", "phone_number", "telefon")
        if primary_phone is None:
            for i, h in enumerate(header):
                if i in reserved or any(hint in h for hint in _ADDITIONAL_PHONE_HINTS):
                    continue
                if any(hint in h for hint in _PHONE_HINTS):
                    primary_phone = i
                    break
        col_map["phone"] = primary_phone
        if primary_phone is not None:
            reserved.add(primary_phone)

        secondary_phone = None
        for i, h in enumerate(header):
            if i in reserved:
                continue
            if any(hint in h for hint in _PHONE_HINTS):
                secondary_phone = i
                break
        col_map["phone2"] = secondary_phone
        if secondary_phone is not None:
            reserved.add(secondary_phone)

        col_map["email"] = find_substr(_EMAIL_HINTS, exclude_idx=reserved)
        return col_map, rows[1:], True

    # --- Sarlavhasiz fayl: ustunlarni QIYMATLARGA qarab taxmin qilamiz ---
    data_rows = rows
    sample = data_rows[:25]
    n_cols = max((len(r) for r in sample), default=0)
    phone_scores = []
    phone_normalized_ratio = []
    name_scores = []
    for c in range(n_cols):
        vals = [r[c] for r in sample if c < len(r) and r[c] not in (None, "")]
        if not vals:
            phone_scores.append(0.0)
            phone_normalized_ratio.append(0.0)
            name_scores.append(0.0)
            continue
        phone_scores.append(sum(1 for v in vals if _import_looks_phoneish(v)) / len(vals))
        phone_normalized_ratio.append(
            sum(1 for v in vals if str(v).strip().startswith(("+", "p:"))) / len(vals)
        )
        name_scores.append(sum(1 for v in vals if _import_looks_nameish(v)) / len(vals))

    phone_candidates = [i for i, s in enumerate(phone_scores) if s >= 0.6]
    phone_candidates.sort(key=lambda i: (phone_normalized_ratio[i], phone_scores[i]), reverse=True)
    col_map["phone"] = phone_candidates[0] if phone_candidates else None
    col_map["phone2"] = phone_candidates[1] if len(phone_candidates) > 1 else None

    name_candidates = [
        i for i, s in enumerate(name_scores)
        if s >= 0.6 and i not in phone_candidates
    ]
    name_candidates.sort(key=lambda i: name_scores[i], reverse=True)
    col_map["name"] = name_candidates[0] if name_candidates else None

    for key in ("meta_lead_id", "created_time", "campaign", "campaign_id",
                "adset", "adset_id", "ad", "ad_id", "form", "email"):
        col_map[key] = None

    col_map["_leftover_cols"] = [
        i for i in range(n_cols)
        if i not in (col_map["phone"], col_map["phone2"], col_map["name"])
    ]
    return col_map, data_rows, False


def _import_get_cell(row, idx):
    if idx is None or idx >= len(row):
        return None
    v = row[idx]
    if v is None:
        return None
    v = str(v).strip()
    return v or None


@app.route("/leads/import", methods=["GET", "POST"])
@login_required
@module_required("leads")
def leads_import():
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename.lower().endswith(ALLOWED_IMPORT_EXTENSIONS):
            flash("Fayl tanlanmadi yoki format noto'g'ri (.xlsx yoki .csv kerak).", "error")
            return redirect(url_for("leads_import"))

        try:
            rows = _import_read_rows(file)
        except Exception as e:
            flash(f"Faylni o'qishda xatolik: {e}", "error")
            return redirect(url_for("leads_import"))

        if not rows:
            flash("Fayl bo'sh.", "error")
            return redirect(url_for("leads_import"))

        col_map, data_rows, used_header = _import_resolve_columns(rows)

        if col_map.get("name") is None and col_map.get("phone") is None:
            flash(
                "Faylda ism yoki telefon ustunini aniqlab bo'lmadi -- fayl formatini "
                "tekshiring yoki ustun sarlavhalarini qo'shing (masalan: Ism, Telefon).",
                "error",
            )
            return redirect(url_for("leads_import"))

        session = get_session()
        added = 0
        skipped = 0
        seen_keys_this_file = set()
        try:
            for r in data_rows:
                if not any(c not in (None, "") for c in r):
                    continue

                full_name = _import_get_cell(r, col_map.get("name"))
                phone_raw = _import_get_cell(r, col_map.get("phone"))
                phone2_raw = _import_get_cell(r, col_map.get("phone2"))
                email = _import_get_cell(r, col_map.get("email"))

                phone = _import_normalize_phone(phone_raw)
                phone2 = _import_normalize_phone(phone2_raw)
                key9 = _import_phone_key9(phone)
                key9_2 = _import_phone_key9(phone2)

                # Agar asosiy ustunda telefon topilmasa, lekin qo'shimcha
                # ustunda bor bo'lsa -- shuni asosiy sifatida ishlatamiz
                # (masalan sarlavhasiz faylda ikkinchi ustun ko'proq
                # "p:"/"+" bilan normallashgan bo'lsa ham, birinchisida
                # qiymat bo'lmasligi mumkin).
                if not phone and phone2:
                    phone, key9 = phone2, key9_2
                    phone2, key9_2 = None, None

                if not full_name and not phone:
                    skipped += 1
                    continue

                if key9:
                    if key9 in seen_keys_this_file:
                        skipped += 1
                        continue
                    existing = session.query(Lead).filter(Lead.phone.ilike(f"%{key9}%")).first()
                    if existing:
                        skipped += 1
                        continue
                    seen_keys_this_file.add(key9)

                meta_lead_id = _import_strip_id_prefix(_import_get_cell(r, col_map.get("meta_lead_id")))
                if meta_lead_id:
                    if session.query(Lead).filter_by(meta_lead_id=meta_lead_id).first():
                        skipped += 1
                        continue

                created_time_raw = _import_get_cell(r, col_map.get("created_time"))
                lead_created_time = None
                if created_time_raw:
                    try:
                        lead_created_time = dt.datetime.strptime(created_time_raw[:19], "%Y-%m-%dT%H:%M:%S")
                    except ValueError:
                        lead_created_time = None

                quality_note = None
                if phone2 and key9_2 and key9_2 != key9:
                    quality_note = f"Qo'shimcha raqam (importdan): {phone2}"
                if not used_header:
                    leftover = [
                        _import_get_cell(r, i) for i in col_map.get("_leftover_cols", [])
                    ]
                    leftover = [v for v in leftover if v]
                    if leftover:
                        extra_note = "Import qo'shimcha ma'lumot: " + " | ".join(leftover[:4])
                        quality_note = f"{quality_note}\n{extra_note}" if quality_note else extra_note

                lead = Lead(
                    meta_lead_id=meta_lead_id,
                    campaign_id=_import_strip_id_prefix(_import_get_cell(r, col_map.get("campaign_id"))),
                    campaign_name=_import_get_cell(r, col_map.get("campaign")),
                    adset_id=_import_strip_id_prefix(_import_get_cell(r, col_map.get("adset_id"))),
                    adset_name=_import_get_cell(r, col_map.get("adset")),
                    ad_id=_import_strip_id_prefix(_import_get_cell(r, col_map.get("ad_id"))),
                    ad_name=_import_get_cell(r, col_map.get("ad")),
                    form_name=_import_get_cell(r, col_map.get("form")),
                    full_name=full_name, phone=phone or phone_raw, email=email,
                    quality_note=quality_note,
                    lead_created_time=lead_created_time,
                    source="import", status="new",
                )
                session.add(lead)
                added += 1
            session.commit()
        finally:
            session.close()
        flash(f"{added} ta lead import qilindi, {skipped} ta o'tkazib yuborildi (bo'sh yoki dublikat).", "success")
        return redirect(url_for("leads_list"))

    return render_template("leads_import.html")


@app.route("/leads/<int:lead_id>", methods=["GET", "POST"])
@login_required
@module_required("leads")
def lead_detail(lead_id):
    session = get_session()
    try:
        lead = session.get(Lead, lead_id)
        if not lead:
            flash("Lead topilmadi.", "error")
            return redirect(url_for("leads_list"))

        custom_fields = session.query(CustomField).filter_by(is_active=True).order_by(CustomField.sort_order).all()
        stages = _active_funnel_stages(session)
        stage_by_key = {s.key: s for s in stages}

        if request.method == "POST":
            form_action = request.form.get("form_action", "update")
            manager_row = session.query(Manager).filter_by(username=current_user.username).first()

            # --- Yangi sotuv qo'shish (1-sotuv, 2-sotuv, ...) -- lead allaqachon
            # "sotildi" bosqichida bo'lsa ham, keyingi xaridlarni alohida
            # qo'shish uchun mustaqil kichik forma. ---
            if form_action == "add_sale":
                amount_raw = request.form.get("amount", "").strip()
                invoice_number = request.form.get("invoice_number", "").strip() or None
                try:
                    amount = float(amount_raw)
                except ValueError:
                    amount = None
                if not amount or amount <= 0:
                    flash("Sotuv summasini to'g'ri kiriting.", "error")
                else:
                    existing_count = session.query(Sale).filter_by(lead_id=lead.id).count()
                    session.add(Sale(
                        lead_id=lead.id,
                        manager_id=(manager_row.id if manager_row else lead.assigned_manager_id),
                        sale_number=existing_count + 1,
                        amount=amount,
                        invoice_number=invoice_number,
                        sold_at=dt.datetime.utcnow(),
                    ))
                    sold_key = _sold_stage_key(stages)
                    current_category = stage_by_key[lead.status].category if lead.status in stage_by_key else None
                    if sold_key and current_category != "sold":
                        lead.status = sold_key
                    if manager_row and not lead.assigned_manager_id:
                        lead.assigned_manager_id = manager_row.id
                    session.flush()
                    _recompute_lead_sale_total(session, lead)
                    session.commit()
                    _send_capi_lead_signal(lead, "Purchase", value=amount, event_id_suffix=f"-{existing_count + 1}")
                    flash(f"{existing_count + 1}-sotuv qo'shildi.", "success")
                return redirect(url_for("lead_detail", lead_id=lead.id) + "#sales")

            # --- Sotuvni "qaytarilgan" (vozvrat) deb belgilash -- faqat admin,
            # chunki bu KPI/bonus hisobidan sotuvni butunlay chiqarib tashlaydi.
            # MUHIM: bu "bekor qilish" (pastdagi `delete_sale`) BILAN BIR XIL
            # EMAS -- "qaytarilgan" yozuv bazada QOLADI (mijoz haqiqatan xarid
            # qilib, keyin qaytargani haqidagi tarix), "bekor qilish" esa
            # xato kiritilgan yozuvni BUTUNLAY o'chiradi. ---
            if form_action == "mark_returned":
                if current_user.role != "admin":
                    flash("Faqat admin sotuvni qaytarilgan deb belgilay oladi.", "error")
                else:
                    sale_id = request.form.get("sale_id", "")
                    sale = session.get(Sale, int(sale_id)) if sale_id.isdigit() else None
                    if sale and sale.lead_id == lead.id:
                        sale.is_returned = True
                        sale.returned_at = dt.datetime.utcnow()
                        session.flush()
                        _recompute_lead_sale_total(session, lead)
                        session.commit()
                        flash("Sotuv qaytarilgan deb belgilandi -- KPI/bonus hisobidan chiqarildi.", "success")
                    else:
                        flash("Sotuv topilmadi.", "error")
                return redirect(url_for("lead_detail", lead_id=lead.id) + "#sales")

            # --- Sotuvni BEKOR QILISH (2026-08, foydalanuvchi so'rovi) --
            # xato kiritilgan sotuv yozuvini bazadan BUTUNLAY o'chiradi (vozvrat
            # emas -- iz qolmaydi). Faqat admin, chunki qaytarib bo'lmaydi. ---
            if form_action == "delete_sale":
                if current_user.role != "admin":
                    flash("Faqat admin sotuvni bekor qila oladi.", "error")
                else:
                    sale_id = request.form.get("sale_id", "")
                    sale = session.get(Sale, int(sale_id)) if sale_id.isdigit() else None
                    if sale and sale.lead_id == lead.id:
                        session.delete(sale)
                        session.flush()
                        _recompute_lead_sale_total(session, lead)
                        session.commit()
                        flash("Sotuv bekor qilindi (butunlay o'chirildi).", "success")
                    else:
                        flash("Sotuv topilmadi.", "error")
                return redirect(url_for("lead_detail", lead_id=lead.id) + "#sales")

            # --- Asosiy forma: status/izoh/ism-familiya/kvalifikatsiya ---
            # ESLATMA: sotuv summasi bu yerdan ENDI kiritilmaydi (avval "Birinchi
            # sotuv summasi" maydoni shu yerda ham bor edi, "Sotuvlar" bo'limidagi
            # forma bilan ikkilanib, chalkashlik keltirib chiqargan -- endi sotuv
            # FAQAT "Sotuvlar" bo'limidagi `add_sale` formasi orqali qo'shiladi).
            new_status = request.form.get("status")
            note_text = request.form.get("note", "").strip()

            # Asosiy ma'lumotlar (ism/telefon/email) ham shu formadan tahrirlanadi --
            # bo'sh yuborilsa eskisi saqlanadi (majburiy emas, chunki ba'zi lidlarda
            # boshidanoq email bo'lmasligi mumkin).
            new_full_name = request.form.get("full_name", "").strip()
            new_phone = request.form.get("phone", "").strip()
            new_email = request.form.get("email", "").strip()
            if "full_name" in request.form:
                lead.full_name = new_full_name or None
            if "phone" in request.form:
                lead.phone = new_phone or None
            if "email" in request.form:
                lead.email = new_email or None

            old_category = stage_by_key[lead.status].category if lead.status in stage_by_key else None
            if new_status in stage_by_key:
                lead.status = new_status

            # --- Qayta aloqa (follow-up) -- menejer shu lead bilan
            # gaplashganda "qachon yana bog'lanish kerak"ni shu yerda
            # belgilaydi. Sana maydoni bo'sh yuborilsa -- qayta aloqa
            # bekor qilinadi (rejalashtirilmagan holatga qaytadi). ---
            if "next_contact_date" in request.form:
                next_contact_raw = request.form.get("next_contact_date", "").strip()
                if next_contact_raw:
                    try:
                        lead.next_contact_at = dt.datetime.strptime(next_contact_raw, "%Y-%m-%d")
                    except ValueError:
                        flash("Qayta aloqa sanasi noto'g'ri formatda -- saqlanmadi.", "error")
                else:
                    lead.next_contact_at = None
                lead.next_contact_note = request.form.get("next_contact_note", "").strip() or None

            if custom_fields:
                try:
                    extra = json.loads(lead.extra_data) if lead.extra_data else {}
                except (TypeError, ValueError):
                    extra = {}
                for cf in custom_fields:
                    val = request.form.get(f"cf_{cf.key}", "").strip()
                    if val:
                        extra[cf.key] = val
                    elif cf.key in extra:
                        extra.pop(cf.key)
                lead.extra_data = json.dumps(extra, ensure_ascii=False)

            if note_text:
                session.add(LeadNote(lead_id=lead.id, manager_id=manager_row.id if manager_row else None, text=note_text))
            if manager_row and not lead.assigned_manager_id:
                lead.assigned_manager_id = manager_row.id
            session.flush()
            _recompute_lead_sale_total(session, lead)
            session.commit()

            # --- CAPI signal: lead yangi kategoriyaga o'tganda Meta'ga qayta
            # aloqa yuboriladi (faqat HAQIQIY o'tishda -- qayta saqlashda
            # takrorlanmasin uchun eski/yangi kategoriya solishtiriladi). ---
            new_category = stage_by_key[new_status].category if new_status in stage_by_key else old_category
            if new_status in stage_by_key and new_category != old_category:
                if new_category == "qualified":
                    _send_capi_lead_signal(lead, "QualifiedLead")
                elif new_category == "sold":
                    _send_capi_lead_signal(lead, "Purchase", value=lead.sale_amount)

            flash("Saqlandi.", "success")
            return redirect(url_for("leads_list"))

        sales = session.query(Sale).filter_by(lead_id=lead.id).order_by(Sale.sale_number.asc()).all()
        sales_view = [{
            "id": s.id, "sale_number": s.sale_number, "amount": s.amount, "sold_at": s.sold_at,
            "is_returned": s.is_returned, "manager": (s.manager.full_name or s.manager.username) if s.manager else "—",
        } for s in sales]
        notes = session.query(LeadNote).filter_by(lead_id=lead.id).order_by(LeadNote.created_at.desc()).all()
        try:
            extra = json.loads(lead.extra_data) if lead.extra_data else {}
        except (TypeError, ValueError):
            extra = {}
        custom_fields_view = [{
            "key": cf.key, "label": cf.label, "field_type": cf.field_type,
            "options": [o.strip() for o in (cf.options or "").split(",") if o.strip()],
            "value": extra.get(cf.key, ""),
        } for cf in custom_fields]
        lead_view = {
            "id": lead.id, "full_name": lead.full_name, "phone": lead.phone,
            "email": lead.email, "campaign_name": lead.campaign_name,
            "adset_name": lead.adset_name, "ad_name": lead.ad_name, "source": lead.source,
            "status": lead.status, "quality_note": lead.quality_note,
            "sale_amount": lead.sale_amount, "created_at": lead.created_at,
            "assigned_manager": lead.assigned_manager.full_name if lead.assigned_manager else None,
            "next_contact_at": lead.next_contact_at, "next_contact_note": lead.next_contact_note,
        }
        stages_view = [{"key": s.key, "label": s.label, "color": s.color} for s in stages]
        # lead.status hozirgi faol bosqichlar ro'yxatida bo'lmasligi mumkin
        # (masalan admin o'sha bosqichni keyinchalik o'chirgan/nofaol qilgan) --
        # baribir badge/select'da to'g'ri ko'rinishi uchun ro'yxatga qo'shib qo'yamiz.
        if lead.status not in {s["key"] for s in stages_view}:
            stages_view.append({"key": lead.status, "label": lead.status, "color": "dim"})
        current_stage_color = next((s["color"] for s in stages_view if s["key"] == lead.status), "dim")
        notes_view = [{"text": n.text, "created_at": n.created_at, "manager": n.manager.full_name if n.manager else "?"} for n in notes]
    finally:
        session.close()
    return render_template(
        "lead_detail.html", lead=lead_view, notes=notes_view, custom_fields=custom_fields_view,
        stages=stages_view, current_stage_color=current_stage_color, sales=sales_view,
        now_date=dt.datetime.utcnow().date(),
    )


# ---------------------------------------------------------------------------
# Qayta aloqa (follow-up) -- "next_contact_at" belgilangan barcha lidlarni
# YAQINLASHGAN/O'TIB KETGAN tartibda ko'rsatadi, shu kunda yoki muddati
# o'tgan aloqalar ENG YUQORIDA turadi ("birinchi o'rinda"). Menejer o'ziga
# biriktirilganlarni, admin BARCHASINI ko'radi. Kunlik Telegram eslatmasi
# `scheduler.py`dagi `job_followup_reminders()` orqali (shu yerdagi
# `_due_followups_query()` bilan bir xil mantiqda) yuboriladi.
# ---------------------------------------------------------------------------

def _followups_due_count(session, manager_id: int | None = None) -> int:
    """Bugun yoki muddati o'tgan ("kechiktirilgan") qayta aloqalar soni --
    navbar'dagi belgi (badge) va Dashboard kartochkasi uchun. `manager_id`
    berilsa faqat shu menejerga biriktirilgan lidlar hisoblanadi."""
    today_end = dt.datetime.utcnow().replace(hour=23, minute=59, second=59, microsecond=0)
    q = session.query(Lead).filter(Lead.next_contact_at.isnot(None), Lead.next_contact_at <= today_end)
    if manager_id is not None:
        q = q.filter(Lead.assigned_manager_id == manager_id)
    return q.count()


@app.context_processor
def _inject_followups_badge():
    """Har bir sahifada navbar'dagi "Qayta aloqa" havolasiga qizil raqamli
    belgi (badge) qo'shish uchun -- foydalanuvchi biror sahifani ochganda
    ham "bugun kimga qo'ng'iroq qilish kerak"ligi darhol ko'zga tashlanadi
    (alohida Qayta aloqa sahifasiga kirmasdan ham)."""
    if not (current_user.is_authenticated and permissions.has_module(current_user, "leads")):
        return {}
    session = get_session()
    try:
        manager_id = None
        if current_user.role != "admin":
            m = session.query(Manager).filter_by(username=current_user.username).first()
            manager_id = m.id if m else None
        return {"followups_due_count": _followups_due_count(session, manager_id)}
    except Exception:
        logger.exception("Qayta aloqa belgisini hisoblashda xatolik")
        return {}
    finally:
        session.close()


@app.route("/qayta-aloqa")
@login_required
@module_required("leads")
def followups_list():
    session = get_session()
    try:
        manager_row = None
        if current_user.role != "admin":
            manager_row = session.query(Manager).filter_by(username=current_user.username).first()

        q = session.query(Lead).filter(Lead.next_contact_at.isnot(None))
        if manager_row:
            q = q.filter(Lead.assigned_manager_id == manager_row.id)
        leads = q.order_by(Lead.next_contact_at.asc()).all()

        now = dt.datetime.utcnow()
        today_date = now.date()
        rows = []
        for l in leads:
            due_date = l.next_contact_at.date()
            days_diff = (due_date - today_date).days
            if days_diff < 0:
                urgency = "overdue"
            elif days_diff == 0:
                urgency = "today"
            else:
                urgency = "upcoming"
            rows.append({
                "id": l.id, "full_name": l.full_name, "phone": l.phone,
                "status": l.status, "assigned_manager": l.assigned_manager.full_name if l.assigned_manager else "—",
                "next_contact_at": l.next_contact_at, "next_contact_note": l.next_contact_note,
                "days_diff": days_diff, "urgency": urgency,
            })
        # Ichida ENG avval "overdue" (eng ko'p kechikkani tepada), keyin
        # "today", keyin "upcoming" (eng yaqini tepada) -- shunchaki sana
        # bo'yicha o'sish tartibi (asc) buni ALLAQACHON to'g'ri beradi,
        # chunki o'tib ketgan sanalar har doim eng kichik.
        overdue_count = sum(1 for r in rows if r["urgency"] == "overdue")
        today_count = sum(1 for r in rows if r["urgency"] == "today")
    finally:
        session.close()
    return render_template(
        "followups.html", rows=rows, overdue_count=overdue_count, today_count=today_count,
        is_admin=(current_user.role == "admin"),
    )


# ---------------------------------------------------------------------------
# Admin: menejerlar
# ---------------------------------------------------------------------------

@app.route("/managers", methods=["GET", "POST"])
@login_required
@admin_required
def managers():
    session = get_session()
    try:
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            full_name = request.form.get("full_name", "").strip()
            role = request.form.get("role", "manager")
            phone_number = request.form.get("phone_number", "").strip()
            moizvonki_login = request.form.get("moizvonki_login", "").strip().lower()
            telegram_user_id = request.form.get("telegram_user_id", "").strip()
            hire_date_str = request.form.get("hire_date", "").strip()
            modules = request.form.getlist("allowed_modules")
            if username and password:
                if session.query(Manager).filter_by(username=username).first():
                    flash("Bu username allaqachon mavjud.", "error")
                else:
                    m = Manager(username=username, full_name=full_name, role=role)
                    m.set_password(password)
                    m.phone_number = phone_number or None
                    m.moizvonki_login = moizvonki_login or None
                    m.telegram_user_id = telegram_user_id or None
                    if hire_date_str:
                        try:
                            m.hire_date = dt.datetime.strptime(hire_date_str, "%Y-%m-%d")
                        except ValueError:
                            pass
                    m.allowed_modules = permissions.serialize_allowed_modules(modules)
                    session.add(m)
                    session.commit()
                    flash(f"{username} qo'shildi.", "success")
        all_managers = session.query(Manager).order_by(Manager.created_at).all()
        rows = [{"id": m.id, "username": m.username, "full_name": m.full_name, "role": m.role, "is_active": m.is_active, "phone_number": m.phone_number, "moizvonki_login": m.moizvonki_login, "telegram_user_id": m.telegram_user_id, "hire_date": m.hire_date} for m in all_managers]
    finally:
        session.close()
    return render_template("managers.html", managers=rows, modules=permissions.MODULES, default_modules=permissions.DEFAULT_MANAGER_MODULES)


@app.route("/managers/<int:manager_id>/edit", methods=["GET", "POST"])
@login_required
@admin_required
def manager_edit(manager_id):
    session = get_session()
    try:
        m = session.get(Manager, manager_id)
        if not m:
            flash("Menejer topilmadi.", "error")
            return redirect(url_for("managers"))

        if request.method == "POST":
            new_username = request.form.get("username", "").strip()
            new_full_name = request.form.get("full_name", "").strip()
            new_role = request.form.get("role", "manager")
            new_password = request.form.get("password", "")
            new_phone = request.form.get("phone_number", "").strip()
            new_moizvonki_login = request.form.get("moizvonki_login", "").strip().lower()
            new_telegram_user_id = request.form.get("telegram_user_id", "").strip()
            new_hire_date_str = request.form.get("hire_date", "").strip()
            new_modules = request.form.getlist("allowed_modules")
            is_active = request.form.get("is_active") == "on"

            if not new_username:
                flash("Username bo'sh bo'lishi mumkin emas.", "error")
            elif session.query(Manager).filter(Manager.username == new_username, Manager.id != m.id).first():
                flash("Bu username boshqa hisobda band.", "error")
            else:
                # O'zining yagona admin hisobini nofaol qilib qo'yishning oldini
                # olamiz -- aks holda hech kim tizimga kira olmay qoladigan
                # holatga tushib qolishi mumkin.
                if not is_active and m.role == "admin":
                    other_active_admins = session.query(Manager).filter(
                        Manager.role == "admin", Manager.is_active == True, Manager.id != m.id  # noqa: E712
                    ).count()
                    if other_active_admins == 0:
                        flash("Bu yagona faol admin -- uni nofaol qilib bo'lmaydi.", "error")
                        return render_template("manager_edit.html", m={
                            "id": m.id, "username": m.username, "full_name": m.full_name,
                            "role": m.role, "is_active": m.is_active, "phone_number": m.phone_number,
                            "moizvonki_login": m.moizvonki_login, "telegram_user_id": m.telegram_user_id, "hire_date": m.hire_date,
                            "allowed_modules": permissions.parse_allowed_modules(m.allowed_modules),
                        }, modules=permissions.MODULES)

                m.username = new_username
                m.full_name = new_full_name or None
                m.role = new_role
                m.is_active = is_active
                m.phone_number = new_phone or None
                m.moizvonki_login = new_moizvonki_login or None
                m.telegram_user_id = new_telegram_user_id or None
                if new_hire_date_str:
                    try:
                        m.hire_date = dt.datetime.strptime(new_hire_date_str, "%Y-%m-%d")
                    except ValueError:
                        pass
                else:
                    m.hire_date = None
                m.allowed_modules = permissions.serialize_allowed_modules(new_modules)
                if new_password:
                    m.set_password(new_password)
                session.commit()
                flash(f"{new_username} yangilandi.", "success")
                return redirect(url_for("managers"))

        m_view = {
            "id": m.id, "username": m.username, "full_name": m.full_name, "role": m.role, "is_active": m.is_active,
            "phone_number": m.phone_number, "moizvonki_login": m.moizvonki_login, "telegram_user_id": m.telegram_user_id, "hire_date": m.hire_date,
            "allowed_modules": permissions.parse_allowed_modules(m.allowed_modules),
        }
    finally:
        session.close()
    return render_template("manager_edit.html", m=m_view, modules=permissions.MODULES)


# ---------------------------------------------------------------------------
# Individual tekshirish -- Moi Zvonki qo'ng'iroq yozuvlarini lidlar bilan
# solishtirib, menejer HAQIQATDA gaplashdimi tekshiradi. Avval qat'iy
# admin-only edi, endi (2026-08, foydalanuvchi so'rovi) admin xohlagan
# menejerga "individual_check" modulini yoqib bera oladi -- lekin
# CHEGARANI o'zgartirish (pastdagi POST route) baribir faqat admin uchun
# qoladi (bu kompaniya darajasidagi umumiy sozlama).
# ---------------------------------------------------------------------------

@app.route("/individual-tekshirish")
@login_required
@module_required("individual_check")
def individual_check():
    days = request.args.get("days", "30")
    try:
        days = max(1, min(90, int(days)))
    except (TypeError, ValueError):
        days = 30
    tab = request.args.get("tab", "calls")
    if tab not in ("calls", "ai"):
        tab = "calls"
    since = dt.datetime.utcnow() - dt.timedelta(days=days)

    session = get_session()
    try:
        check = call_analytics.build_individual_check(session, since)
        ai = _build_ai_analysis_view(session, since)
    finally:
        session.close()

    return render_template(
        "individual_check.html",
        days=days, tab=tab,
        configured=call_sync.is_configured(),
        min_real_talk_seconds=call_analytics.get_min_real_talk_seconds(),
        ai=ai,
        **check,
    )


def _build_ai_analysis_view(session, since) -> dict:
    """"AI analiz" tab uchun (2026-08, foydalanuvchi so'rovi -- audio-tahlil
    promptini avtomatik ishlatib, "Individual tekshirish"ning ICHIDA emas,
    alohida tab sifatida ko'rsatish kerak): tahlil qilingan qo'ng'iroqlar
    ro'yxati + hozircha navbatda turganlar soni ("hozir nima qilinayotgani"
    ko'rinishi uchun)."""
    from db import CallRecord, Lead, Manager

    min_seconds = call_analytics.get_min_real_talk_seconds()
    analyzed = (
        session.query(CallRecord)
        .filter(CallRecord.ai_analyzed_at.isnot(None), CallRecord.started_at >= since)
        .order_by(CallRecord.ai_analyzed_at.desc())
        .limit(200)
        .all()
    )
    pending_count = (
        session.query(CallRecord)
        .filter(
            CallRecord.recording_url.isnot(None),
            CallRecord.ai_analyzed_at.is_(None),
            CallRecord.duration_seconds >= min_seconds,
            CallRecord.started_at >= since,
        )
        .count()
    )
    lead_ids = {c.lead_id for c in analyzed if c.lead_id}
    leads_by_id = {l.id: l for l in session.query(Lead).filter(Lead.id.in_(lead_ids)).all()} if lead_ids else {}
    managers_by_id = {m.id: m for m in session.query(Manager).all()}

    rows = []
    error_count = 0
    for c in analyzed:
        lead = leads_by_id.get(c.lead_id) if c.lead_id else None
        manager = managers_by_id.get(c.manager_id) if c.manager_id else None
        if c.ai_error:
            error_count += 1

        def _load_json(raw):
            try:
                return json.loads(raw) if raw else None
            except (TypeError, ValueError):
                return None

        # 2026-08 V5, foydalanuvchi ANIQ so'ragan: operatorMistakes/
        # positivePoints endi `{"text","evidenceTurnIds"}` obyektlari --
        # lekin ESKI (V4'da tahlil qilingan) yozuvlarda hali oddiy string
        # bo'lishi mumkin, shuning uchun shablon HAR DOIM `.text` bilan
        # ishlay olishi uchun bu yerda ODDIY STRINGLAR ham obyektga
        # aylantiriladi (backward-compat).
        def _normalize_evidence_list(raw):
            items = _load_json(raw) or []
            out = []
            for item in items:
                if isinstance(item, dict):
                    out.append({"text": item.get("text", ""), "evidenceTurnIds": item.get("evidenceTurnIds") or []})
                elif isinstance(item, str):
                    out.append({"text": item, "evidenceTurnIds": []})
            return out

        rows.append({
            "id": c.id,
            "started_at": (c.started_at + dt.timedelta(hours=5)) if c.started_at else None,
            "phone_number": c.phone_number,
            "lead_name": lead.full_name if lead else None,
            "lead_id": lead.id if lead else None,
            "manager_name": (manager.full_name or manager.username) if manager else "Noma'lum",
            "recording_url": c.recording_url,
            "score": c.ai_score, "status": c.ai_status, "color": c.ai_color,
            "overview": c.ai_overview, "result": c.ai_result,
            "transcription": c.ai_transcription, "error": c.ai_error,
            "stage": c.ai_stage,
            # 2026-08 V5 -- sifat darvozasi ma'lumotlari: qo'ng'iroq
            # `transcription_failed` bo'lsa, UI aynan NEGA tahlil
            # qilinmaganini ko'rsatishi kerak (foydalanuvchi ANIQ so'ragan:
            # yolg'on ishonchli xulosa o'rniga aniq holat).
            "transcription_failed": c.ai_stage == "transcription_failed",
            "transcription_quality": c.ai_transcription_quality,
            "transcription_confidence": c.ai_transcription_confidence,
            "transcription_quality_reasons": _load_json(c.ai_transcription_quality_reasons) or [],
            "analysis_confidence": c.ai_analysis_confidence,
            # 2026-08, foydalanuvchi so'rovi -- transkripsiyani "SMS suhbat"
            # ko'rinishida (Manager/Mijoz alohida tomonlarda, gap-bo'lib-gap)
            # ko'rsatish uchun, xom matn oldindan {"speaker","text"} bo'laklarga
            # ajratib beriladi (shablon o'zi regex bilan ishlamasin uchun).
            "turns": call_analysis.parse_transcript_turns(c.ai_transcription) if c.ai_transcription else [],
            # 2026-08 V4/V5 -- kengaytirilgan tahlil maydonlari (mijoz so'rovi,
            # menejer xatolari (endi evidenceTurnIds bilan), ijobiy tomonlar,
            # savdo natijasi, qayta bog'lanish sababi, tavsiya).
            "customer_request": _load_json(c.ai_customer_request),
            "operator_mistakes": _normalize_evidence_list(c.ai_operator_mistakes),
            "positive_points": _normalize_evidence_list(c.ai_positive_points),
            "sale_result": c.ai_sale_result,
            "callback_required": c.ai_callback_required,
            "callback_reason": c.ai_callback_reason,
            "recommended_response": c.ai_recommended_response,
        })
    return {
        "rows": rows,
        "analyzed_count": len(rows),
        "error_count": error_count,
        "pending_count": pending_count,
        "openai_configured": call_analysis.is_configured(),
    }


def _run_ai_analysis_in_background(limit: int) -> None:
    """`individual_check_run_ai_analysis()` uchun fon ishchisi. 2026-08:
    avval bu ISH TO'G'RIDAN-TO'G'RI so'rov ichida (sinxron) bajarilardi --
    har bir qo'ng'iroq audio yuklab olish + transkripsiya (diarizatsiya
    urinishi bilan birga bir necha marta OpenAI'ga so'rov, har biri 90-180s
    timeout) + matn tahlili (yana bir so'rov) talab qiladi, ya'ni BITTA
    qo'ng'iroq o'zi 2-3 daqiqagacha cho'zilishi mumkin. `render.yaml`dagi
    gunicorn `--timeout 120` shundan tezroq ishchi jarayonni majburan
    o'ldirib qo'yardi -- brauzerda "ERR_CONNECTION_CLOSED" sifatida
    ko'rinardi (foydalanuvchi screenshot bilan ko'rsatdi). Endi Telegram
    botdagi bilan bir xil naqsh: HTTP so'rov DARHOL qaytadi, haqiqiy ish esa
    fon oqimida (thread) davom etadi -- xuddi `job_call_analysis()`
    (scheduler.py) allaqachon qanday ishlab turgan bo'lsa, shunday."""
    session = get_session()
    try:
        call_analysis.run_pending_analysis(session, limit=limit)
    except Exception:
        logger.exception("Fon jarayonida AI tahlili xatosi")
    finally:
        session.close()


@app.route("/individual-tekshirish/ai-tahlil-boshlash", methods=["POST"])
@login_required
@admin_required
def individual_check_run_ai_analysis():
    """"Hoziroq tahlil qilish" tugmasi -- admin bosganda, navbatda turgan
    bir nechta qo'ng'iroqni FON OQIMIDA (thread) tahlil qilishni ishga
    tushiradi va DARHOL qaytadi (yuqoridagi izohga qarang -- sinxron
    bajarish gunicorn timeout'iga urilib, ulanish uzilishiga olib kelardi)."""
    days = request.form.get("days", "30")
    if not call_analysis.is_configured():
        flash("OPENAI_API_KEY sozlanmagan -- AI tahlil ishlamaydi.", "error")
        return redirect(url_for("individual_check", days=days, tab="ai"))
    thread = threading.Thread(target=_run_ai_analysis_in_background, args=(5,), daemon=True)
    thread.start()
    flash(
        "Tahlil fon jarayonida boshlandi (audio tahlili bir necha daqiqa davom etishi mumkin) -- "
        "bir necha daqiqadan so'ng natijalarni ko'rish uchun sahifani yangilang.",
        "success",
    )
    return redirect(url_for("individual_check", days=days, tab="ai"))


@app.route("/individual-tekshirish/audio/<int:call_id>")
@login_required
def individual_check_audio_proxy(call_id):
    """2026-08 V5, foydalanuvchi ANIQ so'ragan: AI tahlil tab'idagi audio
    pleer ba'zan "0:00/0:00" ko'rsatib, ishlamay qolgan. ANIQLANMAGAN,
    lekin ENG EHTIMOLIY sabab: brauzer audio DAVOMIYLIGINI aniqlash uchun
    HTTP Range so'rovi yuboradi -- agar Moi Zvonki'ning imzolangan
    (signed) yozuv havolasi Range'ni QO'LLAB-QUVVATLAMASA yoki noto'g'ri/
    yo'q Content-Type qaytarsa, ba'zi brauzerlar pleerni "0:00/0:00"
    holatida qoldiradi. Bu AUTENTIFIKATSIYA qilingan (`login_required`)
    proxy -- yozuvni SERVER TOMONIDA yuklab oladi (Range so'rovini
    FORWARD qilib) va TO'G'RI `Content-Type`/`Accept-Ranges`/
    `Content-Range` bilan qayta uzatadi. `recording_url`ning o'zi HECH
    QACHON brauzerga to'g'ridan-to'g'ri berilmaydi (login talab qilinishi
    ham shu bilan bog'liq -- ochiq/anonim audio-ulashish EMAS)."""
    import requests as _requests
    from db import CallRecord

    session = get_session()
    try:
        call = session.get(CallRecord, call_id)
        recording_url = call.recording_url if call else None
    finally:
        session.close()
    if not recording_url:
        abort(404)

    upstream_headers = {}
    range_header = request.headers.get("Range")
    if range_header:
        upstream_headers["Range"] = range_header

    try:
        upstream = _requests.get(recording_url, headers=upstream_headers, stream=True, timeout=30)
    except _requests.RequestException:
        logger.warning("Qo'ng'iroq #%s uchun audio-proxy yuklab olishda xato", call_id)
        abort(502)

    if upstream.status_code not in (200, 206):
        abort(502)

    content_type = upstream.headers.get("Content-Type") or ""
    if not content_type.lower().startswith("audio/"):
        # Moi Zvonki ba'zan noto'g'ri/generic Content-Type qaytarishi
        # mumkin -- brauzer pleer ISHLASHI uchun audio/* MAJBURIY.
        content_type = "audio/mpeg"

    resp_headers = {
        "Content-Type": content_type,
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=3600",
    }
    for h in ("Content-Length", "Content-Range"):
        if upstream.headers.get(h):
            resp_headers[h] = upstream.headers[h]

    return Response(
        upstream.iter_content(chunk_size=65536),
        status=upstream.status_code,
        headers=resp_headers,
    )


@app.route("/individual-tekshirish/ai-debug/<int:call_id>")
@login_required
@admin_required
def individual_check_ai_debug(call_id):
    """2026-08, foydalanuvchi so'rovi -- admin/debug ko'rinish: xom
    transkripsiya, normallashtirilgan transkripsiya, ishlatilgan modellar,
    audio metadata va jarayon bosqichini ko'rsatadi. ODDIY operator UI'ga
    (`individual_check.html`ning AI tab'i) chiqarilmaydi -- faqat admin
    ANIQ shu URL'ga o'tsa ko'rinadi (masalan tahlil sifatini tekshirish
    yoki xatoni diagnostika qilish uchun)."""
    from db import CallRecord, Lead, Manager

    session = get_session()
    try:
        call = session.get(CallRecord, call_id)
        if not call:
            flash("Qo'ng'iroq topilmadi.", "error")
            return redirect(url_for("individual_check", tab="ai"))
        lead = session.get(Lead, call.lead_id) if call.lead_id else None
        manager = session.get(Manager, call.manager_id) if call.manager_id else None
        debug = {
            "id": call.id,
            "phone_number": call.phone_number,
            "lead_name": lead.full_name if lead else None,
            "manager_name": (manager.full_name or manager.username) if manager else "Noma'lum",
            "recording_url": call.recording_url,
            "stage": call.ai_stage,
            "error": call.ai_error,
            "analyzed_at": call.ai_analyzed_at,
            "model_transcribe": call.ai_model_transcribe,
            "model_analysis": call.ai_model_analysis,
            "audio_channels": call.ai_audio_channels,
            "audio_codec": call.ai_audio_codec,
            "audio_duration_sec": call.ai_audio_duration_sec,
            "operator_channel": call.ai_operator_channel,
            "transcription_quality": call.ai_transcription_quality,
            "transcription_confidence": call.ai_transcription_confidence,
            "transcription_quality_reasons": call.ai_transcription_quality_reasons,
            "transcription_attempts": call.ai_transcription_attempts,
            "transcription_attempts_log": call.ai_transcription_attempts_log,
            "analysis_confidence": call.ai_analysis_confidence,
            "raw_transcription": call.ai_raw_transcription,
            "normalized_transcription": call.ai_transcription,
            "diarized_json": call.ai_diarized_json,
            "customer_request": call.ai_customer_request,
            "operator_mistakes": call.ai_operator_mistakes,
            "positive_points": call.ai_positive_points,
            "sale_result": call.ai_sale_result,
            "callback_required": call.ai_callback_required,
            "callback_reason": call.ai_callback_reason,
            "recommended_response": call.ai_recommended_response,
            "score_reasons": call.ai_score_reasons,
            "ffmpeg_available": call_analysis.ffmpeg_available(),
            "ffprobe_available": call_analysis.ffprobe_available(),
        }
    finally:
        session.close()
    return render_template("individual_check_ai_debug.html", d=debug)


@app.route("/individual-tekshirish/chegara", methods=["POST"])
@login_required
@admin_required
def individual_check_set_threshold():
    """Admin "shubhali qo'ng'iroq" chegarasini (soniyada) o'zgartiradi --
    `call_analytics.MIN_REAL_TALK_SECONDS` standart 60 soniya edi, endi
    bu yerdan sozlanadi (kv_store'da saqlanadi)."""
    days = request.form.get("days", "30")
    value = request.form.get("min_real_talk_seconds", "").strip()
    try:
        seconds = int(value)
        if seconds < 0:
            raise ValueError
        call_analytics.set_min_real_talk_seconds(seconds)
        flash(f"Chegara {seconds} soniyaga o'rnatildi.", "success")
    except (TypeError, ValueError):
        flash("Noto'g'ri qiymat -- butun son (soniya) kiriting.", "error")
    return redirect(url_for("individual_check", days=days))


# ---------------------------------------------------------------------------
# Nastroyka (Sozlamalar) -- barcha kompaniya darajasidagi sozlamalar bitta
# joyga yig'ilgan bosh sahifa (2026-08, foydalanuvchi so'rovi: "bo'limlar
# juda ko'p bo'lib ketti" -- Voronka/Savollar/Doimiy vazifalar/Menejerlar
# endi chap menyuda alohida-alohida emas, shu "Nastroyka" bo'limi ichidan
# kartochkalar orqali ochiladi). Bu sahifaning o'zi "Umumiy" (minimal sotuv
# summasi) va "Bildirishnomalar" (menejerlarning Telegram ID'sini bog'lash)
# bo'limlarini o'z ichiga oladi -- qolganlari (Voronka/Savollar/Doimiy
# vazifalar/Menejerlar) o'zining eski sahifalariga havola qilinadi.
# ---------------------------------------------------------------------------

@app.route("/sozlamalar", methods=["GET", "POST"])
@login_required
@module_required("settings")
def settings_hub():
    session = get_session()
    try:
        if request.method == "POST":
            action = request.form.get("action")
            if current_user.role != "admin":
                flash("Bu sozlamani faqat admin o'zgartira oladi.", "error")
                return redirect(url_for("settings_hub"))

            if action == "set_min_sale":
                raw = request.form.get("min_sale_amount", "").strip()
                try:
                    value = float(raw)
                    if value < 0:
                        raise ValueError
                    kpi_bonus.set_min_sale_amount(value)
                    flash(f"Minimal sotuv summasi {value:,.0f} so'mga o'rnatildi (bu chegaradan kichik sotuvlar KPI/bonusga kirmaydi).".replace(",", " "), "success")
                except (TypeError, ValueError):
                    flash("Noto'g'ri qiymat -- summani raqam ko'rinishida kiriting.", "error")

            elif action == "set_usd_rate":
                raw = request.form.get("usd_to_uzs_rate", "").strip()
                try:
                    value = float(raw)
                    if value <= 0:
                        raise ValueError
                    kpi_bonus.set_usd_to_uzs_rate(value)
                    flash(f"Dollar/so'm kursi 1$ = {value:,.0f} so'mga o'rnatildi (ROI hisobida ishlatiladi).".replace(",", " "), "success")
                except (TypeError, ValueError):
                    flash("Noto'g'ri qiymat -- kursni raqam ko'rinishida kiriting.", "error")

            elif action == "set_telegram":
                manager_id = request.form.get("manager_id", "")
                m = session.get(Manager, int(manager_id)) if manager_id.isdigit() else None
                if m:
                    new_value = request.form.get("telegram_user_id", "").strip() or None
                    m.telegram_user_id = new_value
                    session.commit()
                    flash(f"{m.full_name or m.username} uchun Telegram ID {'yangilandi' if new_value else 'o‘chirildi'}.", "success")
                else:
                    flash("Menejer topilmadi.", "error")

            elif action == "resolve_unanswered":
                q_id = request.form.get("question_id", "")
                q = session.get(AssistantUnanswered, int(q_id)) if q_id.isdigit() else None
                if q:
                    q.is_resolved = True
                    session.commit()
                    flash("Savol hal qilingan deb belgilandi.", "success")

            return redirect(url_for("settings_hub"))

        managers_all = session.query(Manager).filter_by(is_active=True).order_by(Manager.full_name).all()
        manager_rows = [
            {"id": m.id, "name": m.full_name or m.username, "telegram_user_id": m.telegram_user_id}
            for m in managers_all
        ]
        unanswered_rows = (
            session.query(AssistantUnanswered)
            .order_by(AssistantUnanswered.is_resolved.asc(), AssistantUnanswered.created_at.desc())
            .limit(20)
            .all()
        )
        unanswered = [
            {
                "id": u.id, "question": u.question,
                "manager_name": u.manager_name or "—",
                "created_at": u.created_at, "is_resolved": u.is_resolved,
            }
            for u in unanswered_rows
        ]
    finally:
        session.close()

    return render_template(
        "settings_hub.html",
        min_sale_amount=kpi_bonus.get_min_sale_amount(),
        min_real_talk_seconds=call_analytics.get_min_real_talk_seconds(),
        usd_to_uzs_rate=kpi_bonus.get_usd_to_uzs_rate(),
        managers=manager_rows,
        unanswered=unanswered,
        capi_configured=meta_api.is_capi_configured(),
    )


# ---------------------------------------------------------------------------
# SMM hisobot -- Instagram Business va Facebook Page uchun to'liq organik
# statistika (obunachilar o'sishi, postlar, qamrov, engagement). QAT'IY
# "target" modul huquqiga bog'liq -- chap menyuda "Target" bo'limi ichida
# (SMM hisobot) sifatida ko'rinadi.
# ---------------------------------------------------------------------------

@app.route("/smm")
@login_required
@module_required("target")
def smm_report():
    days = request.args.get("days", "30")
    try:
        days = max(1, min(90, int(days)))
    except (TypeError, ValueError):
        days = 30

    session = get_session()
    try:
        report = smm_analytics.build_smm_report(session, days=days)
    finally:
        session.close()

    # MUHIM: `report` ichida ALLAQACHON "days" kaliti bor (`build_smm_report`
    # o'zi qaytaradi) -- bu yerda YANA `days=days` deb alohida berish
    # "got multiple values for keyword argument 'days'" TypeError'iga olib
    # kelardi va sahifa HAR DOIM "Internal Server Error" bilan ochilmasdi
    # (2026-08, foydalanuvchi topgan xato). Endi faqat `report`dagi bitta
    # "days" ishlatiladi.
    return render_template(
        "smm.html",
        configured=smm_sync.is_configured(),
        sync_status=smm_sync.get_last_status(),
        **report,
    )


# ---------------------------------------------------------------------------
# Admin: anketa savollari (lead detail sahifasida ko'rinadigan qo'shimcha maydonlar)
# ---------------------------------------------------------------------------

@app.route("/settings/fields", methods=["GET", "POST"])
@login_required
@module_required("settings")
def custom_fields_settings():
    session = get_session()
    try:
        if request.method == "POST":
            action = request.form.get("action")
            if action == "add":
                label = request.form.get("label", "").strip()
                field_type = request.form.get("field_type", "text")
                options = request.form.get("options", "").strip()
                if label:
                    key = "".join(ch if ch.isalnum() else "_" for ch in label.lower()).strip("_")
                    if session.query(CustomField).filter_by(key=key).first():
                        key = f"{key}_{int(dt.datetime.utcnow().timestamp())}"
                    max_order = session.query(CustomField).count()
                    session.add(CustomField(key=key, label=label, field_type=field_type, options=options or None, sort_order=max_order))
                    session.commit()
                    flash("Savol qo'shildi.", "success")
            elif action == "toggle":
                field_id = request.form.get("field_id")
                cf = session.get(CustomField, int(field_id)) if field_id else None
                if cf:
                    cf.is_active = not cf.is_active
                    session.commit()
            elif action == "delete":
                field_id = request.form.get("field_id")
                cf = session.get(CustomField, int(field_id)) if field_id else None
                if cf:
                    session.delete(cf)
                    session.commit()
                    flash("Savol o'chirildi.", "success")
            return redirect(url_for("custom_fields_settings"))

        all_fields = session.query(CustomField).order_by(CustomField.sort_order).all()
        rows = [{"id": f.id, "label": f.label, "field_type": f.field_type, "options": f.options, "is_active": f.is_active} for f in all_fields]
    finally:
        session.close()
    return render_template("custom_fields.html", fields=rows)


# ---------------------------------------------------------------------------
# Admin: raqobatchilar ro'yxati (2026-08, foydalanuvchi so'rovi -- Meta Ad
# Library orqali har kuni soat 10:00da avtomatik tahlil qilinadigan
# ro'yxatni shu yerdan qo'shish/o'chirish/tahrirlash mumkin).
# ---------------------------------------------------------------------------

@app.route("/settings/competitors", methods=["GET", "POST"])
@login_required
@module_required("settings")
def competitors_settings():
    session = get_session()
    try:
        if request.method == "POST":
            action = request.form.get("action")
            if action == "add":
                name = request.form.get("name", "").strip()
                domain = request.form.get("domain", "").strip() or None
                search_term = request.form.get("search_term", "").strip() or None
                if name:
                    session.add(Competitor(name=name, domain=domain, search_term=search_term))
                    session.commit()
                    flash(f"{name} raqobatchilar ro'yxatiga qo'shildi.", "success")
                else:
                    flash("Kompaniya nomini kiriting.", "error")
            elif action == "toggle":
                comp_id = request.form.get("competitor_id", "")
                c = session.get(Competitor, int(comp_id)) if comp_id.isdigit() else None
                if c:
                    c.is_active = not c.is_active
                    session.commit()
            elif action == "delete":
                comp_id = request.form.get("competitor_id", "")
                c = session.get(Competitor, int(comp_id)) if comp_id.isdigit() else None
                if c:
                    session.query(CompetitorAd).filter_by(competitor_id=c.id).delete()
                    session.delete(c)
                    session.commit()
                    flash("Raqobatchi o'chirildi.", "success")
            return redirect(url_for("competitors_settings"))

        all_competitors = session.query(Competitor).order_by(Competitor.created_at.desc()).all()
        rows = []
        for c in all_competitors:
            ads_count = session.query(CompetitorAd).filter_by(competitor_id=c.id, is_active=True).count()
            rows.append({
                "id": c.id, "name": c.name, "domain": c.domain,
                "search_term": c.search_term, "is_active": c.is_active,
                "active_ads_count": ads_count,
            })
    finally:
        session.close()
    return render_template("competitors.html", competitors=rows)


# ---------------------------------------------------------------------------
# Admin: voronka (funnel) bosqichlari
# ---------------------------------------------------------------------------

FUNNEL_COLORS = ["blue", "good", "bad", "warn", "dim"]
FUNNEL_CATEGORIES = [("active", "Faol (hali hal bo'lmagan)"), ("qualified", "Sifatli"), ("unqualified", "Sifatsiz"), ("sold", "Sotildi")]


@app.route("/settings/funnel", methods=["GET", "POST"])
@login_required
@module_required("settings")
def funnel_settings():
    session = get_session()
    try:
        if request.method == "POST":
            action = request.form.get("action")
            if action == "add":
                label = request.form.get("label", "").strip()
                category = request.form.get("category", "active")
                color = request.form.get("color", "blue")
                if label and category in dict(FUNNEL_CATEGORIES):
                    key = "".join(ch if ch.isalnum() else "_" for ch in label.lower()).strip("_")
                    if session.query(FunnelStage).filter_by(key=key).first():
                        key = f"{key}_{int(dt.datetime.utcnow().timestamp())}"
                    max_order = session.query(FunnelStage).count()
                    session.add(FunnelStage(key=key, label=label, category=category, color=color, sort_order=max_order))
                    session.commit()
                    flash("Bosqich qo'shildi.", "success")
            elif action == "toggle":
                stage_id = request.form.get("stage_id")
                fs = session.get(FunnelStage, int(stage_id)) if stage_id else None
                if fs:
                    fs.is_active = not fs.is_active
                    session.commit()
            elif action == "delete":
                stage_id = request.form.get("stage_id")
                fs = session.get(FunnelStage, int(stage_id)) if stage_id else None
                if fs:
                    in_use = session.query(Lead).filter_by(status=fs.key).count()
                    if in_use:
                        flash(f"O'chirib bo'lmadi: {in_use} ta lead shu bosqichda turibdi. Avval ularni boshqa bosqichga o'tkazing yoki shunchaki 'nofaol' qiling.", "error")
                    else:
                        session.delete(fs)
                        session.commit()
                        flash("Bosqich o'chirildi.", "success")
            return redirect(url_for("funnel_settings"))

        all_stages = session.query(FunnelStage).order_by(FunnelStage.sort_order).all()
        rows = [{"id": s.id, "key": s.key, "label": s.label, "category": s.category, "color": s.color, "is_active": s.is_active} for s in all_stages]
    finally:
        session.close()
    return render_template("funnel_settings.html", stages=rows, categories=FUNNEL_CATEGORIES, colors=FUNNEL_COLORS)


# ---------------------------------------------------------------------------
# Admin: Telegram orqali qo'yilgan doimiy vazifalar (avtomatik yoqish/
# o'chirish jadvali + qo'shimcha doimiy hisobot vaqtlari) -- CRM'dan ham
# ko'rib/bekor qilib bo'lishi uchun (faqat Telegram buyrug'iga qaram bo'lmasin).
# ---------------------------------------------------------------------------

@app.route("/settings/tasks", methods=["GET", "POST"])
@login_required
@module_required("settings")
def standing_tasks_settings():
    from db import StandingTask, StandingReport
    session = get_session()
    try:
        if request.method == "POST":
            action = request.form.get("action")
            kind = request.form.get("kind")
            item_id = request.form.get("item_id")
            model = StandingTask if kind == "task" else StandingReport
            obj = session.get(model, int(item_id)) if item_id else None
            if obj:
                if action == "toggle":
                    obj.is_active = not obj.is_active
                    session.commit()
                elif action == "delete":
                    session.delete(obj)
                    session.commit()
                    flash("Vazifa o'chirildi.", "success")
            return redirect(url_for("standing_tasks_settings"))

        tasks = session.query(StandingTask).order_by(StandingTask.created_at.desc()).all()
        reports = session.query(StandingReport).order_by(StandingReport.created_at.desc()).all()
        task_rows = [
            {"id": t.id, "chat_id": t.chat_id, "object_name": t.object_name or t.object_id,
             "on_time": t.on_time, "off_time": t.off_time, "is_active": t.is_active,
             "last_state": t.last_desired_state, "last_error": t.last_error}
            for t in tasks
        ]
        report_rows = [
            {"id": r.id, "chat_id": r.chat_id, "time_hhmm": r.time_hhmm,
             "label": r.label, "is_active": r.is_active, "last_sent_date": r.last_sent_date}
            for r in reports
        ]
    finally:
        session.close()
    return render_template("standing_tasks.html", tasks=task_rows, reports=report_rows)


# ---------------------------------------------------------------------------
# Health / manual trigger (fallback, CRON_SECRET bilan himoyalangan)
# ---------------------------------------------------------------------------

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "telegram_token_set": bool(TELEGRAM_TOKEN),
        "anthropic_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "openai_key_set": bool(os.environ.get("OPENAI_API_KEY")),
        "meta_token_set": bool(os.environ.get("META_ACCESS_TOKEN")),
        "meta_page_id_set": bool(os.environ.get("META_PAGE_ID")),
        "database_configured": bool(os.environ.get("DATABASE_URL")),
        "cron_secret_set": bool(CRON_SECRET),
        "moizvonki_configured": call_sync.is_configured(),
        "call_analysis_configured": call_analysis.is_configured(),
        "ffmpeg_available": call_analysis.ffmpeg_available(),
        "ffprobe_available": call_analysis.ffprobe_available(),
    })


@app.route("/api/trigger/<job_name>", methods=["GET"])
def manual_trigger(job_name):
    """Qo'lda sinash uchun (masalan deploy'dan keyin). Haqiqiy jadval
    scheduler.py orqali avtomatik ishlaydi -- bu faqat fallback/debug."""
    if not CRON_SECRET or request.args.get("secret") != CRON_SECRET:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    from scheduler import JOBS
    job = JOBS.get(job_name)
    if not job:
        return jsonify({"ok": False, "error": f"noma'lum job: {job_name}", "available": list(JOBS)}), 400
    result = job()
    return jsonify({"ok": True, "result": result})


def create_app():
    init_db()
    call_analysis.log_model_config()
    from scheduler import start_scheduler
    start_scheduler(app)
    return app


if __name__ == "__main__":
    application = create_app()
    application.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
