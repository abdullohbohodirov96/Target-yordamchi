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

from flask import Flask, request, jsonify, render_template, redirect, url_for, flash
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user, login_required,
    current_user,
)

import meta_api
import orchestrator
import budget_tracker
import kv_store
import monthly_report
from db import init_db, get_session, Manager, Lead, LeadNote, CustomField, FunnelStage
from dashboard_data import get_kpis
import lead_sync

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
# Flask-Login user wrapper
# ---------------------------------------------------------------------------

class ManagerUser(UserMixin):
    def __init__(self, manager: Manager):
        self.id = str(manager.id)
        self.username = manager.username
        self.full_name = manager.full_name
        self.role = manager.role


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
    tg_send(chat_id, "Noma'lum buyruq. /start yozing.\n\nQo'shimcha buyruqlar: /vazifalar (doimiy vazifalar ro'yxati), /vazifa_off <ID> (birini bekor qilish).")


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

    if result is None:
        try:
            result = orchestrator.call_light_chat(KNOWLEDGE_BASE, history, max_tokens=800)
        except Exception as e:
            logger.exception("Web yordamchi: call_light_chat xatosi")
            result = f"⚠️ Xatolik yuz berdi: {e}"

    history.append({"role": "assistant", "content": result})
    kv_store.set_json(history_key, history[-12:])
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
# Dashboard
# ---------------------------------------------------------------------------

@app.route("/")
@login_required
def dashboard():
    period = request.args.get("period", "last_30d")
    level = request.args.get("level", "campaign")
    if level not in ("campaign", "adset", "ad"):
        level = "campaign"
    show_all = request.args.get("show_all") == "1"
    data = get_kpis(level=level, date_preset=period, active_only=not show_all)
    return render_template("dashboard.html", data=data, period=period, level=level, show_all=show_all)


# ---------------------------------------------------------------------------
# CRM: lidlar
# ---------------------------------------------------------------------------

def _active_funnel_stages(session):
    """Faol voronka bosqichlarini tartib bo'yicha qaytaradi -- admin
    /settings/funnel'da qo'shgan/o'zgartirgan bosqichlar shu yerdan o'qiladi,
    filter tugmalari va status <select> shularga qarab quriladi."""
    return session.query(FunnelStage).filter_by(is_active=True).order_by(FunnelStage.sort_order).all()


@app.route("/leads")
@login_required
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


@app.route("/leads/new", methods=["GET", "POST"])
@login_required
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
            new_status = request.form.get("status")
            note_text = request.form.get("note", "").strip()
            sale_amount = request.form.get("sale_amount", "").strip()

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

            if new_status in stage_by_key:
                lead.status = new_status
                if stage_by_key[new_status].category == "sold":
                    lead.sold_at = dt.datetime.utcnow()
                    if sale_amount:
                        try:
                            lead.sale_amount = float(sale_amount)
                        except ValueError:
                            pass

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

            manager_row = session.query(Manager).filter_by(username=current_user.username).first()
            if note_text:
                session.add(LeadNote(lead_id=lead.id, manager_id=manager_row.id if manager_row else None, text=note_text))
            if manager_row and not lead.assigned_manager_id:
                lead.assigned_manager_id = manager_row.id
            session.commit()
            flash("Saqlandi.", "success")
            return redirect(url_for("leads_list"))

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
        stages=stages_view, current_stage_color=current_stage_color,
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
            if username and password:
                if session.query(Manager).filter_by(username=username).first():
                    flash("Bu username allaqachon mavjud.", "error")
                else:
                    m = Manager(username=username, full_name=full_name, role=role)
                    m.set_password(password)
                    session.add(m)
                    session.commit()
                    flash(f"{username} qo'shildi.", "success")
        all_managers = session.query(Manager).order_by(Manager.created_at).all()
        rows = [{"id": m.id, "username": m.username, "full_name": m.full_name, "role": m.role, "is_active": m.is_active} for m in all_managers]
    finally:
        session.close()
    return render_template("managers.html", managers=rows)


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
                            "role": m.role, "is_active": m.is_active,
                        })

                m.username = new_username
                m.full_name = new_full_name or None
                m.role = new_role
                m.is_active = is_active
                if new_password:
                    m.set_password(new_password)
                session.commit()
                flash(f"{new_username} yangilandi.", "success")
                return redirect(url_for("managers"))

        m_view = {"id": m.id, "username": m.username, "full_name": m.full_name, "role": m.role, "is_active": m.is_active}
    finally:
        session.close()
    return render_template("manager_edit.html", m=m_view)


# ---------------------------------------------------------------------------
# Admin: anketa savollari (lead detail sahifasida ko'rinadigan qo'shimcha maydonlar)
# ---------------------------------------------------------------------------

@app.route("/settings/fields", methods=["GET", "POST"])
@login_required
@admin_required
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
# Admin: voronka (funnel) bosqichlari
# ---------------------------------------------------------------------------

FUNNEL_COLORS = ["blue", "good", "bad", "warn", "dim"]
FUNNEL_CATEGORIES = [("active", "Faol (hali hal bo'lmagan)"), ("qualified", "Sifatli"), ("unqualified", "Sifatsiz"), ("sold", "Sotildi")]


@app.route("/settings/funnel", methods=["GET", "POST"])
@login_required
@admin_required
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
@admin_required
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
    from scheduler import start_scheduler
    start_scheduler(app)
    return app


if __name__ == "__main__":
    application = create_app()
    application.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
