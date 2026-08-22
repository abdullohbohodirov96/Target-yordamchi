"""
db.py — Postgres ulanish + SQLAlchemy modellar.

Render'da bu loyiha DOIMIY (persistent) jarayon sifatida ishlaydi (Vercel'dagi
kabi har so'rovga alohida serverless funksiya emas) — shuning uchun holatni
(suhbat tarixi, byudjet balansi, lidlar, menejerlar) endi tashqi KV/Redis
emas, to'g'ridan-to'g'ri shu Postgres bazada saqlaymiz.

DATABASE_URL Render'da Postgres qo'shganda avtomatik beriladi
(Settings -> Environment -> DATABASE_URL, "postgres://..." formatida).
"""

import os
import datetime as dt

from sqlalchemy import (
    create_engine, Column, Integer, String, Text, Float, DateTime, Boolean,
    ForeignKey, UniqueConstraint, Index,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from werkzeug.security import generate_password_hash, check_password_hash

DATABASE_URL = os.environ.get("DATABASE_URL", "")
# Render/Heroku uslubidagi "postgres://" prefiksni SQLAlchemy 2.x
# "postgresql://" talab qiladi -- avtomatik tuzatamiz.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=5, max_overflow=10) if DATABASE_URL else None
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False) if engine else None
Base = declarative_base()


class Manager(Base):
    """Admin yoki menejer hisobi -- dashboard/CRM'ga kirish uchun."""
    __tablename__ = "managers"

    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    full_name = Column(String(128), nullable=False, default="")
    role = Column(String(16), nullable=False, default="manager")  # "admin" | "manager"
    telegram_user_id = Column(String(32), nullable=True)  # ixtiyoriy: shaxsiy bildirishnoma uchun
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow)

    def set_password(self, raw: str) -> None:
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        return check_password_hash(self.password_hash, raw)


class Lead(Base):
    """Meta Lead Ads'dan kelgan (yoki qo'lda kiritilgan) bitta lead.

    status oqimi:  new -> contacted -> qualified/unqualified -> (qualified bo'lsa) sold
    """
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True)
    meta_lead_id = Column(String(64), unique=True, nullable=True)  # Meta'dan kelgan asl ID (dublikatni oldini olish)
    campaign_id = Column(String(64), nullable=True, index=True)
    campaign_name = Column(String(255), nullable=True)
    adset_id = Column(String(64), nullable=True)
    ad_id = Column(String(64), nullable=True)
    form_name = Column(String(255), nullable=True)

    full_name = Column(String(255), nullable=True)
    phone = Column(String(64), nullable=True)
    email = Column(String(255), nullable=True)
    raw_field_data = Column(Text, nullable=True)  # Meta'dan kelgan to'liq forma javoblari (JSON matn)

    status = Column(String(16), nullable=False, default="new")  # new/contacted/qualified/unqualified/sold
    quality_note = Column(Text, nullable=True)
    sale_amount = Column(Float, nullable=True)
    sold_at = Column(DateTime, nullable=True)

    assigned_manager_id = Column(Integer, ForeignKey("managers.id"), nullable=True)
    assigned_manager = relationship("Manager")

    lead_created_time = Column(DateTime, nullable=True)  # Meta'da lead yaratilgan vaqt
    created_at = Column(DateTime, default=dt.datetime.utcnow)
    updated_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)

    __table_args__ = (
        Index("ix_leads_status", "status"),
        Index("ix_leads_campaign", "campaign_id"),
    )


class LeadNote(Base):
    """Menejerning lead bo'yicha yozgan har bir izohi/harakati (tarix)."""
    __tablename__ = "lead_notes"

    id = Column(Integer, primary_key=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False)
    manager_id = Column(Integer, ForeignKey("managers.id"), nullable=True)
    text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=dt.datetime.utcnow)

    lead = relationship("Lead", backref="notes")
    manager = relationship("Manager")


class KVEntry(Base):
    """kv_store.py o'rniga -- Vercel KV/Upstash'ni almashtiradi. orchestrator.py
    va budget_tracker.py shu jadval orqali holatni (suhbat tarixi, byudjet
    balansi, oxirgi hisobot, pending action) saqlaydi."""
    __tablename__ = "kv_store"

    key = Column(String(255), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)


def init_db() -> None:
    """Jadvallarni yaratadi (agar hali yo'q bo'lsa). Ilova ishga tushganda
    bir marta chaqiriladi -- alohida migratsiya vositasisiz sodda MVP uchun
    yetarli."""
    if engine is None:
        raise RuntimeError(
            "DATABASE_URL o'rnatilmagan -- Render'da Postgres qo'shing "
            "(New -> PostgreSQL), keyin loyihaga ulang."
        )
    Base.metadata.create_all(engine)


def get_session():
    if SessionLocal is None:
        raise RuntimeError("DATABASE_URL o'rnatilmagan.")
    return SessionLocal()
