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
import logging
import datetime as dt

from sqlalchemy import (
    create_engine, Column, Integer, String, Text, Float, DateTime, Boolean,
    ForeignKey, UniqueConstraint, Index, inspect as sa_inspect, text as sa_text,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from werkzeug.security import generate_password_hash, check_password_hash

logger = logging.getLogger("db")

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
    phone_number = Column(String(32), nullable=True)  # shu menejerning telefon raqami (ma'lumot uchun -- Moi Zvonki qo'ng'iroqlarini bog'lash ENDI `moizvonki_login` orqali, chunki rasmiy API javobida telefon emas, login/email qaytadi)
    moizvonki_login = Column(String(128), nullable=True)  # Moi Zvonki (moizvonki.ru) tizimidagi shu menejerning shaxsiy LOGIN(email)i -- calls.list javobidagi "user_account" maydoniga mos kelishi kerak, aks holda qo'ng'iroq HECH KIMGA biriktirilmaydi (call_sync.py)
    allowed_modules = Column(Text, nullable=True)  # JSON ro'yxat, masalan ["dashboard","leads","analytics"] -- admin uchun HAR DOIM e'tiborsiz (adminda hammasi ochiq), faqat "manager" rolidagi hisoblar uchun ishlatiladi. NULL -- standart bo'limlar (permissions.DEFAULT_MANAGER_MODULES)
    hire_date = Column(DateTime, nullable=True)  # ish boshlagan sana -- KPI/bonus oylik rejasi (75 sotuv, oborot bosqichlari) shu oy ichida necha kun ishlaganiga qarab PRORATSIYA qilinadi (kpi_bonus.py). NULL -- to'liq oy ishlagan deb hisoblanadi.
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
    adset_name = Column(String(255), nullable=True)
    ad_id = Column(String(64), nullable=True)
    ad_name = Column(String(255), nullable=True)  # reklama/video nomi -- "qaysi videodan kelgan" shu orqali ko'rinadi
    form_id = Column(String(64), nullable=True, index=True)  # Meta Instant Form ID -- lead-sync diagnostikasi uchun (forma nechta lead berdi vs bazada nechtasi bor)
    form_name = Column(String(255), nullable=True)
    source = Column(String(16), nullable=False, default="meta")  # "meta" (avtomatik) | "manual" (qo'lda) | "import" (Excel)

    full_name = Column(String(255), nullable=True)
    phone = Column(String(64), nullable=True)
    email = Column(String(255), nullable=True)
    raw_field_data = Column(Text, nullable=True)  # Meta'dan kelgan to'liq forma javoblari (JSON matn)
    extra_data = Column(Text, nullable=True)  # admin belgilagan qo'shimcha anketa savollariga javoblar (JSON: {field_key: value})

    status = Column(String(16), nullable=False, default="new")  # new/contacted/qualified/unqualified/sold
    quality_note = Column(Text, nullable=True)
    # MUHIM: bitta lead endi bir nechta sotuvga ega bo'lishi mumkin (1-sotuv,
    # 2-sotuv, ...) -- haqiqiy tafsilotlar `Sale` jadvalida saqlanadi.
    # `sale_amount`/`sold_at` shu yerda ESKI kodlar (dashboard revenue hisobi
    # va h.k.) o'zgarishsiz ishlashi uchun QAYTA HISOBLANGAN KESH sifatida
    # qoldirilgan: sale_amount = shu leadning barcha QAYTARILMAGAN sotuvlari
    # YIG'INDISI, sold_at = birinchi (eng qadimgi) sotuv vaqti. Har safar Sale
    # qo'shilganda/qaytarilgan deb belgilanganda `_recompute_lead_sale_total()`
    # orqali yangilanadi (app.py).
    sale_amount = Column(Float, nullable=True)
    sold_at = Column(DateTime, nullable=True)

    assigned_manager_id = Column(Integer, ForeignKey("managers.id"), nullable=True)
    assigned_manager = relationship("Manager")

    # "Qayta aloqa" (follow-up) -- menejer lead bilan gaplashganda "qachon
    # yana bog'lanish kerak"ni shu yerga belgilaydi. `next_contact_at` sana
    # kelganda (yoki o'tib ketganda) lead "/qayta-aloqa" ro'yxatida va
    # kunlik Telegram eslatmasida (scheduler.py: job_followup_reminders)
    # ko'rinadi. NULL -- hech qanday qayta aloqa rejalashtirilmagan.
    next_contact_at = Column(DateTime, nullable=True)
    next_contact_note = Column(Text, nullable=True)  # nima haqida qayta bog'lanish kerak (ixtiyoriy)

    lead_created_time = Column(DateTime, nullable=True)  # Meta'da lead yaratilgan vaqt
    created_at = Column(DateTime, default=dt.datetime.utcnow)
    updated_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)

    __table_args__ = (
        Index("ix_leads_status", "status"),
        Index("ix_leads_campaign", "campaign_id"),
        Index("ix_leads_next_contact", "next_contact_at"),
    )


class Sale(Base):
    """Bitta lead (mijoz)ning HAR BIR alohida sotuvi -- bitta odam bir necha
    marta xarid qilishi mumkin (1-sotuv, 2-sotuv, 3-sotuv, ...), KPI/bonus
    tizimi (`kpi_bonus.py`) aynan shu jadvaldan hisoblanadi: qaysi menejer,
    qachon, qancha summaga, shu mijozning nechinchi xaridi ekanini bilish
    kerak (masalan "mijozni faollashtirish bonusi" faqat 1- va 2-sotuvga,
    15 kun ichida bo'lsa, tegadi)."""
    __tablename__ = "sales"

    id = Column(Integer, primary_key=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False, index=True)
    lead = relationship("Lead", backref="sales")
    manager_id = Column(Integer, ForeignKey("managers.id"), nullable=True, index=True)
    manager = relationship("Manager")

    sale_number = Column(Integer, nullable=False, default=1)  # shu LEAD uchun nechinchi sotuv (1,2,3...) -- lifetime tartib
    amount = Column(Float, nullable=False)
    invoice_number = Column(String(64), nullable=True)  # nakladnoy/chek raqami (ixtiyoriy, buxgalteriya moslashtirishi uchun)
    sold_at = Column(DateTime, default=dt.datetime.utcnow, index=True)

    is_returned = Column(Boolean, nullable=False, default=False)  # vozvrat -- KPI/bonus hisobidan chiqarib tashlanadi
    returned_at = Column(DateTime, nullable=True)
    note = Column(Text, nullable=True)

    created_at = Column(DateTime, default=dt.datetime.utcnow)


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


class CallRecord(Base):
    """"Mening qo'ng'iroqlarim" (Moi Zvonki, moizvonki.ru) xizmatidan
    sinxronlangan bitta qo'ng'iroq yozuvi -- menejer haqiqatan lead bilan
    gaplashganini (necha marta, qancha davomiylikda) TEKSHIRISH uchun.

    MUHIM: bu jadval hozircha BO'SH turishi mumkin -- `call_sync.py` xizmat
    API kaliti sozlanmaguncha hech narsa yozmaydi (`/individual-tekshirish`
    sahifasi buni foydalanuvchiga aniq tushuntiradi, bo'sh jadvalni "hech
    kim qo'ng'iroq qilmagan" deb noto'g'ri ko'rsatmaydi)."""
    __tablename__ = "call_records"

    id = Column(Integer, primary_key=True)
    external_id = Column(String(64), unique=True, nullable=True)  # Moi Zvonki'dagi asl qo'ng'iroq ID'i (dublikatni oldini olish)
    manager_id = Column(Integer, ForeignKey("managers.id"), nullable=True, index=True)
    manager = relationship("Manager")
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=True, index=True)
    lead = relationship("Lead")
    phone_number = Column(String(32), nullable=True, index=True)  # qo'ng'iroq qilingan/qilingan tomon raqami
    manager_phone_number = Column(String(32), nullable=True)  # qaysi ichki/menejer raqamidan qo'ng'iroq qilingan
    direction = Column(String(16), nullable=True)  # "outgoing" | "incoming"
    duration_seconds = Column(Integer, nullable=False, default=0)
    started_at = Column(DateTime, nullable=True, index=True)
    recording_url = Column(Text, nullable=True)
    raw_data = Column(Text, nullable=True)  # xizmatdan kelgan to'liq JSON (keyinchalik kerak bo'lsa)
    created_at = Column(DateTime, default=dt.datetime.utcnow)


class SmmSnapshot(Base):
    """Instagram Business / Facebook Page uchun HAR KUNLIK "hozirgi holat"
    suratlanishi (obunachilar soni va h.k.) -- `smm_sync.py` har kuni bir
    marta yozadi, shu orqali vaqt bo'yicha O'SISH grafigini chizish mumkin
    (Meta Graph API o'zi "tarixiy obunachilar sonini" bermaydi, faqat JORIY
    sonni beradi -- shuning uchun o'zimiz kunma-kun saqlab boramiz)."""
    __tablename__ = "smm_snapshots"

    id = Column(Integer, primary_key=True)
    platform = Column(String(16), nullable=False, index=True)  # "instagram" | "facebook"
    date = Column(String(10), nullable=False, index=True)  # "YYYY-MM-DD" (Toshkent kuni)
    followers_count = Column(Integer, nullable=True)
    media_count = Column(Integer, nullable=True)  # Instagram uchun -- jami post soni
    created_at = Column(DateTime, default=dt.datetime.utcnow)

    __table_args__ = (UniqueConstraint("platform", "date", name="uq_smm_snapshot_platform_date"),)


class SmmPost(Base):
    """Instagram/Facebook'dagi bitta post/media haqidagi ENG OXIRGI
    sinxronlangan statistika (like, comment, qamrov va h.k.) -- `smm_sync.py`
    muntazam yangilab turadi (postlar statistikasi vaqt o'tishi bilan
    o'zgarib turadi, shuning uchun "snapshot" emas, har doim eng so'nggi
    holat saqlanadi, `external_id` bo'yicha upsert qilinadi)."""
    __tablename__ = "smm_posts"

    id = Column(Integer, primary_key=True)
    platform = Column(String(16), nullable=False, index=True)  # "instagram" | "facebook"
    external_id = Column(String(64), unique=True, nullable=False)  # IG media id / FB post id
    caption = Column(Text, nullable=True)
    permalink = Column(Text, nullable=True)
    media_type = Column(String(32), nullable=True)  # IMAGE | VIDEO | CAROUSEL_ALBUM | REEL | STATUS | ...
    thumbnail_url = Column(Text, nullable=True)  # post/video muqovasi -- "Eng faol postlar" jadvalida ko'rsatish uchun
    posted_at = Column(DateTime, nullable=True, index=True)
    like_count = Column(Integer, nullable=True, default=0)
    comments_count = Column(Integer, nullable=True, default=0)
    shares_count = Column(Integer, nullable=True, default=0)  # faqat Facebook
    saved_count = Column(Integer, nullable=True, default=0)   # faqat Instagram
    reach = Column(Integer, nullable=True)
    impressions = Column(Integer, nullable=True)
    raw_data = Column(Text, nullable=True)
    last_synced_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)
    created_at = Column(DateTime, default=dt.datetime.utcnow)


class Competitor(Base):
    """Admin qo'shgan raqobatchilar ro'yxati -- har kuni soat 10:00da
    `competitor_sync.py` Meta Ad Library orqali ularning joriy
    reklamalarini tekshiradi va `competitor_analytics.py` qisqa amaliy
    hisobot tayyorlaydi (2026-08, foydalanuvchi so'rovi)."""
    __tablename__ = "competitors"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)  # ko'rinadigan nom, masalan "Arboss"
    domain = Column(String(255), nullable=True)  # veb-sayt, masalan "arboss.uz"
    search_term = Column(String(255), nullable=True)  # Ad Library'da qidiriladigan kalit so'z -- bo'sh bo'lsa `name` ishlatiladi
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow)


class CompetitorAd(Base):
    """Meta Ad Library'dan topilgan bitta reklamaning ENG OXIRGI holati --
    `external_id` (Meta'ning ad_archive_id) bo'yicha upsert qilinadi (SMM
    postlar bilan bir xil pattern -- `SmmPost`ga qara)."""
    __tablename__ = "competitor_ads"

    id = Column(Integer, primary_key=True)
    competitor_id = Column(Integer, ForeignKey("competitors.id"), nullable=False, index=True)
    external_id = Column(String(64), unique=True, nullable=False)
    page_name = Column(String(255), nullable=True)
    body_text = Column(Text, nullable=True)
    snapshot_url = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)  # hozir ishlab turibdimi (ad_delivery_stop_time yo'q bo'lsa)
    ad_started_at = Column(DateTime, nullable=True)
    first_seen_at = Column(DateTime, default=dt.datetime.utcnow)
    last_seen_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)


class AssistantUnanswered(Base):
    """Web AI-yordamchisi javob TOPA OLMAGAN savollar (2026-08, NotebookLM
    orqali o'rganilgan "Chatplace" AI-agent yondashuvi asosida qo'shildi --
    u yerda ham bilim bazasida yo'q savollar admin uchun alohida
    ro'yxatga ajratib qo'yiladi). Yordamchi javobida `[[UNANSWERED]]`
    belgisini qo'shsa, `app.py` shu belgini o'qib bu jadvalga yozadi va
    foydalanuvchiga ko'rsatishdan oldin belgini olib tashlaydi."""
    __tablename__ = "assistant_unanswered"

    id = Column(Integer, primary_key=True)
    manager_id = Column(Integer, ForeignKey("managers.id"), nullable=True)
    manager_name = Column(String(255), nullable=True)  # kesh -- manager keyin o'chsa ham savol tarixi tushunarli qolsin
    question = Column(Text, nullable=False)
    is_resolved = Column(Boolean, default=False)
    created_at = Column(DateTime, default=dt.datetime.utcnow, index=True)


class CustomField(Base):
    """Admin CRM anketa savollarini (lead'ni to'ldirishda menejer javob berishi
    kerak bo'lgan qo'shimcha maydonlarni) o'zi qo'sha/tahrirlay oladi -- kodga
    qattiq yozilmagan, dinamik. Har bir lead javobi Lead.extra_data (JSON)
    ichida `key` bo'yicha saqlanadi."""
    __tablename__ = "custom_fields"

    id = Column(Integer, primary_key=True)
    key = Column(String(64), unique=True, nullable=False)  # extra_data JSON kaliti (masalan "byudjet")
    label = Column(String(255), nullable=False)  # ko'rinadigan savol matni
    field_type = Column(String(16), nullable=False, default="text")  # text | number | select
    options = Column(Text, nullable=True)  # field_type="select" bo'lsa, vergul bilan ajratilgan variantlar
    sort_order = Column(Integer, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow)


class FunnelStage(Base):
    """Admin CRM voronkasining (lead holatlari) bosqichlarini o'zi qo'sha/
    nomlay/tartiblay oladi -- kodga qattiq yozilmagan. Har bir bosqich to'rtta
    QAT'IY kategoriyadan biriga (`category`) tegishli bo'ladi -- dashboard'dagi
    ACTIVE/QUAL/LOST/WON ustunlari va CPL/ROI hisob-kitobi shu kategoriyaga
    asoslanadi (referens dashboard shu 4 ustunni talab qiladi), lekin bosqich
    NOMI va nechta bosqich borligi to'liq moslashuvchan.

    `key` maydoni Lead.status'da saqlanadigan qiymat -- standart 5 ta bosqich
    (new/contacted/qualified/unqualified/sold) eski ma'lumotlar bilan mos
    kelishi uchun aynan shu key'lar bilan urug'lantiriladi (`seed_default_funnel_stages`)."""
    __tablename__ = "funnel_stages"

    id = Column(Integer, primary_key=True)
    key = Column(String(32), unique=True, nullable=False)
    label = Column(String(64), nullable=False)
    category = Column(String(16), nullable=False)  # active | qualified | unqualified | sold
    color = Column(String(16), nullable=False, default="blue")  # blue|good|bad|warn|dim (style.css badge ranglari)
    sort_order = Column(Integer, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow)


DEFAULT_FUNNEL_STAGES = [
    # (key, label, category, color)
    ("new", "Yangi", "active", "blue"),
    ("contacted", "Bog'lanildi", "active", "warn"),
    ("qualified", "Sifatli", "qualified", "blue"),
    ("unqualified", "Sifatsiz", "unqualified", "bad"),
    ("sold", "Sotildi", "sold", "good"),
]


def seed_default_funnel_stages() -> None:
    """Birinchi ishga tushirishda (yoki jadval bo'sh bo'lsa) standart 5 ta
    voronka bosqichini yaratadi -- eski (funnel sozlamasi qo'shilishidan
    oldingi) lead'lar statuslari bilan mos kelishi uchun key'lar o'zgarmas."""
    session = get_session()
    try:
        if session.query(FunnelStage).count() > 0:
            return
        for i, (key, label, category, color) in enumerate(DEFAULT_FUNNEL_STAGES):
            session.add(FunnelStage(key=key, label=label, category=category, color=color, sort_order=i))
        session.commit()
    finally:
        session.close()


class StandingTask(Base):
    """Telegram orqali berilgan DOIMIY/TAKRORLANUVCHI on/off buyrug'i --
    masalan "shu targetni har kuni 22:00 dan 08:00 gacha o'chirib tur".
    Foydalanuvchi buyruqni FAQAT BIR MARTA beradi -- keyin `scheduler.py`
    dagi `job_standing_tasks` uni har ~5 daqiqada tekshirib, joriy Toshkent
    vaqtiga qarab avtomatik yoqadi/o'chiradi, qayta buyruq berish shart emas."""
    __tablename__ = "standing_tasks"

    id = Column(Integer, primary_key=True)
    chat_id = Column(String(32), nullable=False, index=True)  # buyruq qaysi Telegram chatdan kelgan
    object_id = Column(String(64), nullable=False)  # Meta campaign/adset/ad ID
    object_name = Column(String(255), nullable=True)
    on_time = Column(String(5), nullable=False)   # "HH:MM" -- shu vaqtda ACTIVE qilinadi
    off_time = Column(String(5), nullable=False)  # "HH:MM" -- shu vaqtda PAUSED qilinadi
    is_active = Column(Boolean, nullable=False, default=True)
    last_desired_state = Column(String(8), nullable=True)  # "on"|"off" -- keraksiz qayta so'rovlarni oldini olish uchun
    last_checked_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    created_by_text = Column(Text, nullable=True)  # asl foydalanuvchi buyrug'i/sababi (audit uchun)
    created_at = Column(DateTime, default=dt.datetime.utcnow)


class StandingReport(Base):
    """Foydalanuvchi so'ragan QO'SHIMCHA doimiy hisobot vaqti -- asosiy
    09:00 dagi kunlik hisobotdan tashqari, masalan "har kuni kechqurun
    20:00 da ham hisobot ber"."""
    __tablename__ = "standing_reports"

    id = Column(Integer, primary_key=True)
    chat_id = Column(String(32), nullable=False, index=True)
    time_hhmm = Column(String(5), nullable=False)  # "HH:MM"
    label = Column(String(255), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    last_sent_date = Column(String(10), nullable=True)  # "YYYY-MM-DD" -- bir kunda ikki marta yubormaslik uchun
    created_at = Column(DateTime, default=dt.datetime.utcnow)


class KVEntry(Base):
    """kv_store.py o'rniga -- Vercel KV/Upstash'ni almashtiradi. orchestrator.py
    va budget_tracker.py shu jadval orqali holatni (suhbat tarixi, byudjet
    balansi, oxirgi hisobot, pending action) saqlaydi."""
    __tablename__ = "kv_store"

    key = Column(String(255), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)


def _migrate_add_missing_columns() -> None:
    """MUHIM BUG FIX: `Base.metadata.create_all()` FAQAT hali mavjud bo'lmagan
    YANGI jadvallarni yaratadi -- ALLAQACHON mavjud jadvalga keyinroq model
    kodiga qo'shilgan yangi ustunlarni AVTOMATIK qo'shib bermaydi (bu
    SQLAlchemy'ning o'zi shunday ishlaydi, xato emas). Aynan shu sabab
    production'da "column leads.adset_name does not exist" xatosiga olib
    keldi -- `Lead` modeliga `adset_name`/`ad_name`/`source`/`extra_data`
    ustunlari kod orqali qo'shilgan edi, lekin `leads` jadvali Postgres'da
    ALLAQACHON mavjud bo'lgani uchun `create_all()` ularni jadvalga
    qo'shmadi, natijada har qanday SELECT xato berdi.

    Bu funksiya ishga tushganda HAR BIR modeldagi ustunni haqiqiy bazadagi
    ustunlar bilan solishtirib, YETISHMAGANLARINI o'zi `ALTER TABLE ... ADD
    COLUMN` bilan qo'shib qo'yadi -- Alembic kabi alohida migratsiya
    vositasisiz, oddiy MVP darajasida, lekin endi xavfsiz. Yangi ustun
    doim NULL qabul qiladigan qilib qo'shiladi (modelda `nullable=False`
    bo'lsa ham) -- aks holda mavjud qatorlar uchun standart qiymatsiz
    NOT NULL ustun qo'shish xato beradi; kod bu maydonlarni allaqachon
    bo'sh/None holatda ham to'g'ri ko'rsatadi."""
    inspector = sa_inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # yangi jadval -- create_all() to'liq to'g'ri sxema bilan yaratgan
            existing_cols = {c["name"] for c in inspector.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing_cols:
                    continue
                try:
                    col_type = col.type.compile(dialect=engine.dialect)
                    conn.execute(sa_text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col_type}'))
                    logger.warning("Migratsiya: %s.%s ustuni qo'shildi (%s)", table.name, col.name, col_type)
                except Exception as e:
                    logger.error("Migratsiya XATOSI: %s.%s qo'shib bo'lmadi -- %s", table.name, col.name, e)


def init_db() -> None:
    """Jadvallarni yaratadi (agar hali yo'q bo'lsa) va mavjud jadvallarga
    yetishmayotgan ustunlarni qo'shadi (`_migrate_add_missing_columns`).
    Ilova ishga tushganda bir marta chaqiriladi."""
    if engine is None:
        raise RuntimeError(
            "DATABASE_URL o'rnatilmagan -- Render'da Postgres qo'shing "
            "(New -> PostgreSQL), keyin loyihaga ulang."
        )
    Base.metadata.create_all(engine)
    _migrate_add_missing_columns()
    seed_default_funnel_stages()


def get_session():
    if SessionLocal is None:
        raise RuntimeError("DATABASE_URL o'rnatilmagan.")
    return SessionLocal()
