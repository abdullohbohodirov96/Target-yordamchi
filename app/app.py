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
from db import init_db, get_session, Manager, Lead, LeadNote
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
    "\"hisobim qanday ketyapti\"\n\n"
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
    tg_send(chat_id, "Noma'lum buyruq. /start yozing.")


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
    data = get_kpis(level=level, date_preset=period)
    return render_template("dashboard.html", data=data, period=period, level=level)


# ---------------------------------------------------------------------------
# CRM: lidlar
# ---------------------------------------------------------------------------

@app.route("/leads")
@login_required
def leads_list():
    status_filter = request.args.get("status", "")
    session = get_session()
    try:
        q = session.query(Lead).order_by(Lead.created_at.desc())
        if status_filter:
            q = q.filter(Lead.status == status_filter)
        leads = q.limit(300).all()
        rows = [{
            "id": l.id, "full_name": l.full_name, "phone": l.phone,
            "campaign_name": l.campaign_name, "status": l.status,
            "created_at": l.created_at, "assigned_manager": l.assigned_manager.full_name if l.assigned_manager else None,
            "sale_amount": l.sale_amount,
        } for l in leads]
    finally:
        session.close()
    return render_template("leads.html", leads=rows, status_filter=status_filter)


@app.route("/leads/<int:lead_id>", methods=["GET", "POST"])
@login_required
def lead_detail(lead_id):
    session = get_session()
    try:
        lead = session.get(Lead, lead_id)
        if not lead:
            flash("Lead topilmadi.", "error")
            return redirect(url_for("leads_list"))

        if request.method == "POST":
            new_status = request.form.get("status")
            note_text = request.form.get("note", "").strip()
            sale_amount = request.form.get("sale_amount", "").strip()

            if new_status in ("new", "contacted", "qualified", "unqualified", "sold"):
                lead.status = new_status
                if new_status == "sold":
                    lead.sold_at = dt.datetime.utcnow()
                    if sale_amount:
                        try:
                            lead.sale_amount = float(sale_amount)
                        except ValueError:
                            pass
            manager_row = session.query(Manager).filter_by(username=current_user.username).first()
            if note_text:
                session.add(LeadNote(lead_id=lead.id, manager_id=manager_row.id if manager_row else None, text=note_text))
            if manager_row and not lead.assigned_manager_id:
                lead.assigned_manager_id = manager_row.id
            session.commit()
            flash("Saqlandi.", "success")
            return redirect(url_for("lead_detail", lead_id=lead_id))

        notes = session.query(LeadNote).filter_by(lead_id=lead.id).order_by(LeadNote.created_at.desc()).all()
        lead_view = {
            "id": lead.id, "full_name": lead.full_name, "phone": lead.phone,
            "email": lead.email, "campaign_name": lead.campaign_name,
            "status": lead.status, "quality_note": lead.quality_note,
            "sale_amount": lead.sale_amount, "created_at": lead.created_at,
            "assigned_manager": lead.assigned_manager.full_name if lead.assigned_manager else None,
        }
        notes_view = [{"text": n.text, "created_at": n.created_at, "manager": n.manager.full_name if n.manager else "?"} for n in notes]
    finally:
        session.close()
    return render_template("lead_detail.html", lead=lead_view, notes=notes_view)


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
