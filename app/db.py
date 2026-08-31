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
    ForeignKey, UniqueConstraint, Index, LargeBinary, inspect as sa_inspect, text as sa_text,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, deferred
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


class Company(Base):
    """Multi-tenant SaaS asosi -- HAR BIR mijoz-kompaniya (masalan boshqa
    biznes, o'z Meta/IG/FB akkaunti bilan) shu jadvalda bitta qator bo'ladi.

    MUHIM (2026-08, foydalanuvchi so'rovi -- "vena ai digan web bor
    bizanikiga oxshagan shunaqa qil ... kop kompaniyalar ishlatolidigan
    qil"): bu FAQAT 1-BOSQICH -- ma'lumotlar bazasi negizi (`Company`
    jadvali + boshqa jadvallarga `company_id`). Quyidagilar HALI qo'shilmagan
    -- foydalanuvchi bilan kelishilgan bosqichma-bosqich, kam xavfli
    yondashuvga ko'ra KEYINGI bosqichlarda qo'shiladi:
      - Ro'yxatdan o'tish/login sahifasi (`password_hash`/`email` maydonlari
        shu uchun endi tayyor turibdi, lekin hali hech qanday route ulardan
        foydalanmaydi).
      - Har bir kompaniyaning o'z Meta/IG/FB akkauntini ulash oqimi.
      - `meta_api.py`/`orchestrator.py`/`scheduler.py`/`dashboard_data.py`
        HOZIRGI bitta GLOBAL Meta akkaunt (ENV o'zgaruvchilari orqali)
        o'rniga har bir so'rov/fon vazifasi uchun TO'G'RI kompaniyaning
        `meta_access_token`/`meta_ad_account_id`sidan foydalanadigan qilib
        qayta ishlanishi kerak -- bu eng katta va eng xavfli qism, shuning
        uchun ALOHIDA bosqichga qoldirildi.
      - `FunnelStage.key`/`CustomField.key` kabi hozir GLOBAL unique
        bo'lgan maydonlar `(company_id, key)` bo'yicha unique bo'lishi kerak
        (hozircha ustunlar shunchaki qo'shildi, eski unique cheklov
        tegilmadi -- ishlab turgan production bazasida cheklovni xavfsiz
        o'zgartirish alohida, ehtiyotkorlik bilan qilinadigan migratsiya).

    Hozircha bu jadval yaratilgani va `ensure_default_company()` orqali
    MAVJUD hamma eski qator "Company #1"ga biriktirilgani uchun sayt HECH
    QANDAY xatti-harakatini o'zgartirmaydi -- bu sof QO'SHIMCHA (additive)
    sxema, hech bir route/so'rov hali `company_id` bo'yicha filtrlamaydi."""
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    slug = Column(String(64), unique=True, nullable=True)  # kelajakda URL/subdomain uchun, masalan "acme"
    email = Column(String(255), unique=True, nullable=True)  # kompaniya egasining login email'i (ro'yxatdan o'tish -- keyingi bosqich)
    password_hash = Column(String(255), nullable=True)
    phone = Column(String(32), nullable=True)
    plan = Column(String(32), nullable=False, default="trial")  # trial | start | business | unlimited -- aniq tariflar hali loyihalashtirilmagan (9-band)
    is_active = Column(Boolean, nullable=False, default=True)

    # Har bir kompaniyaning O'Z Meta (Facebook/Instagram) reklama hisobi --
    # keyingi bosqichda meta_api.py shu maydonlardan (hozirgi global ENV
    # o'zgaruvchilari o'rniga) foydalanadigan qilib qayta ishlanadi.
    meta_access_token = Column(Text, nullable=True)
    meta_ad_account_id = Column(String(32), nullable=True)
    meta_page_id = Column(String(32), nullable=True)
    ig_business_id = Column(String(32), nullable=True)
    telegram_group_id = Column(String(32), nullable=True)  # shu kompaniyaning o'z Telegram guruhi (hozirgi global TELEGRAM_AGENTS_GROUP_ID o'rniga)

    trial_ends_at = Column(DateTime, nullable=True)
    # 2026-08 (foydalanuvchi so'rovi -- "hammasini akkauntlani tarif asosida
    # ishlidigan qilib ber tolovsiz ishlamasin"): to'lov qilingan MUDDAT.
    # `NULL` = "muddat cheklovi yo'q" -- SIZNING o'z kompaniyangiz
    # (`ensure_default_company()` shuni qo'yadi) va admin qo'lda "cheksiz"
    # deb belgilagan kompaniyalar uchun. Yangi (kelajakda ro'yxatdan
    # o'tadigan) mijoz-kompaniyalar uchun bu sana bo'lishi SHART -- shu
    # sanadan o'tsa, `is_paid_up()` False qaytaradi va `app.py`dagi
    # to'lov-tekshiruvi (`_enforce_subscription`) web kirishni to'xtatadi.
    # HALI to'liq avtomatik to'lov integratsiyasi (Payme/Click) yo'q --
    # hozircha bu sanani ADMIN qo'lda (`/companies` sahifasi orqali)
    # to'lov kelganda yangilaydi.
    paid_until = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow)

    def set_password(self, raw: str) -> None:
        self.password_hash = generate_password_hash(raw)

    def is_paid_up(self, now: "dt.datetime | None" = None) -> bool:
        """True -- bu kompaniya HOZIR saytdan foydalanishi mumkin.
        `is_active=False` (admin qo'lda o'chirgan) bo'lsa -- HAR DOIM False.
        `paid_until` bo'lmasa (NULL) -- muddat cheklovi yo'q, True. Aks
        holda -- muddat hali o'tmagan bo'lsa True."""
        if not self.is_active:
            return False
        if self.paid_until is None:
            return True
        now = now or dt.datetime.utcnow()
        return self.paid_until >= now

    def check_password(self, raw: str) -> bool:
        return check_password_hash(self.password_hash, raw) if self.password_hash else False


class Manager(Base):
    """Admin yoki menejer hisobi -- dashboard/CRM'ga kirish uchun."""
    __tablename__ = "managers"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True, index=True)  # 2026-08 multi-tenant 1-bosqich -- hali hech qanday route bo'yicha filtrlamaydi
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
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True, index=True)  # 2026-08 multi-tenant 1-bosqich -- hali hech qanday route bo'yicha filtrlamaydi
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
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True, index=True)  # 2026-08 multi-tenant 1-bosqich -- hali hech qanday route bo'yicha filtrlamaydi
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

    # 2026-08 V6.1, foydalanuvchi ANIQ so'ragan ("bittadan ham audio qo'shish
    # mumkin bo'lsin"): admin panelda QO'LDA yuklangan (Moi Zvonki'dan EMAS)
    # yozuvlar uchun -- bularda `recording_url` YO'Q (uzoq server havolasi
    # yo'q), audio BAYTLARINING O'ZI shu yerda saqlanadi. Render'ning Docker
    # runtime'i EFEMER disk ishlatadi (har deploy/restart'da fayllar
    # yo'qoladi), shuning uchun faylni diskka yozish O'RNIGA baza (Postgres
    # BYTEA) ishlatiladi -- shu bilan audio ham tinglash, ham keyinroq qayta
    # tahlil qilish uchun DOIMIY saqlanadi. `call_analysis.analyze_call_record()`
    # `recording_url` bo'sh bo'lsa, ENDI shu maydonlardan audio o'qiydi.
    # `deferred()`: bu ustun OG'IR (bir necha MB gacha audio baytlari) --
    # oddiy `session.query(CallRecord)...` bilan RO'YXAT ko'rsatilganda
    # (masalan "AI analiz" tab'ida 200 tagacha yozuv) HAR SAFAR bu katta
    # ustunni ham yuklab olish keraksiz tarmoq/xotira sarfini keltirib
    # chiqarardi -- `deferred` FAQAT haqiqatda `call.uploaded_audio_data`ga
    # murojaat qilinganda (masalan tahlil/audio-proxy paytida) alohida
    # so'rov bilan yuklaydi.
    uploaded_audio_data = deferred(Column(LargeBinary, nullable=True))
    uploaded_audio_format = Column(String(16), nullable=True)  # masalan "mp3"/"wav"/"m4a" (kengaytmadan olingan)

    # AI qo'ng'iroq tahlili (2026-08, foydalanuvchi bergan audio-tahlil
    # prompti asosida, `call_analysis.py`) -- `ai_analyzed_at` NULL bo'lsa,
    # hali tahlil qilinmagan (yoki hali navbatda) degani.
    ai_overview = Column(Text, nullable=True)
    ai_score = Column(Integer, nullable=True)  # 1-10
    ai_status = Column(String(16), nullable=True)  # bad | average | good
    ai_color = Column(String(16), nullable=True)  # red | yellow | green
    ai_result = Column(Text, nullable=True)
    # 2026-08 V6, foydalanuvchi ANIQ so'ragan MUHIM O'ZGARISH: bu ENDI
    # tahlil MODELI tomonidan qayta yozilgan/qayta-yorliqlangan matn EMAS --
    # transkripsiya PIPELINE'ining O'ZI ishlab chiqargan, ALLAQACHON
    # gapiruvchi-yorliqlangan (Manager/Mijoz/Speaker N) YAKUNIY matn
    # (`ai_raw_transcription` bilan BIR XIL qiymat). Avval tahlil modeli
    # `normalizedTranscript` orqali bu yorliqlarni O'ZI QAYTA TOPARDI --
    # aynan shu narsa "butun qo'ng'iroq Mijoz" xatosining ILDIZI edi.
    ai_transcription = Column(Text, nullable=True)
    ai_error = Column(Text, nullable=True)
    ai_analyzed_at = Column(DateTime, nullable=True, index=True)

    # 2026-08, PIPELINE AUDIT (foydalanuvchi so'rovi -- to'liq transkripsiya
    # quvur liniyasini tekshirish/tuzatish): xom (ASR/diarizatsiya bosqichidan
    # to'g'ridan-to'g'ri, HECH QANDAY AI "tozalash"siz) transkripsiya endi
    # ALOHIDA saqlanadi -- debug/aniqlikni solishtirish uchun, hech qachon
    # ustidan yozilmaydi. `ai_transcription` (yuqorida) shu xom matndan AI
    # tomonidan "normalizatsiya qilingan" (Manager/Mijoz yorliqli, tahlil
    # bosqichida qayta formatlangan) versiya bo'lib qoladi.
    ai_raw_transcription = Column(Text, nullable=True)
    ai_diarized_json = Column(Text, nullable=True)  # diarizatsiya API'sining xom JSON javobi (mavjud bo'lsa) -- debug uchun
    ai_customer_request = Column(Text, nullable=True)  # JSON: {"product","brand","quantity","measurement","parameters"}
    ai_operator_mistakes = Column(Text, nullable=True)  # JSON ro'yxat (string'lar)
    ai_positive_points = Column(Text, nullable=True)  # JSON ro'yxat (string'lar)
    ai_sale_result = Column(String(32), nullable=True)  # sold | lost | pending | information_only | unknown
    ai_callback_required = Column(Boolean, nullable=True)
    ai_recommended_response = Column(Text, nullable=True)  # = "recommendedAction" (spec nomlanishi), ustun nomi eskicha saqlangan
    ai_callback_reason = Column(Text, nullable=True)
    ai_model_transcribe = Column(String(64), nullable=True)  # haqiqatda ishlagan transkripsiya modeli (debug)
    ai_model_analysis = Column(String(64), nullable=True)  # haqiqatda ishlagan tahlil modeli (debug)
    ai_audio_channels = Column(Integer, nullable=True)  # ffprobe orqali aniqlangan kanal soni (mavjud bo'lsa)
    ai_audio_codec = Column(String(32), nullable=True)
    ai_audio_duration_sec = Column(Float, nullable=True)
    # Holat mashinasi (foydalanuvchi so'rovi -- "aniq holatlar" kerak edi):
    # uploaded -> processing_audio -> transcribing -> analyzing -> completed
    # (yoki -- transkripsiya SIFATI yetarli bo'lmasa -- "transcription_failed",
    # yoki tahlil bosqichida kutilmagan xato bo'lsa -- "failed").
    # `ai_analyzed_at` mavjudligi eskicha "tugadi/tugamadi" belgisi bo'lib
    # qoladi (orqaga moslik uchun); `ai_stage` esa QAYSI bosqichda ekanini
    # aniq ko'rsatadi -- masalan transkripsiya muvaffaqiyatli (SIFATI
    # "good"), lekin tahlil muvaffaqiyatsiz bo'lsa ("failed"), qayta ishga
    # tushirilganda AUDIO QAYTA YUKLAB OLINMAYDI/QAYTA TRANSKRIPSIYA
    # QILINMAYDI, faqat tahlil bosqichi qaytadan sinaladi. Aksincha,
    # "transcription_failed" bo'lsa -- xom transkripsiya SIFATSIZ deb
    # topilgani uchun keyingi urinishda AUDIO QAYTADAN TO'LIQ qayta
    # ishlanadi (transkripsiya bosqichidan boshlab).
    ai_stage = Column(String(24), nullable=True, index=True)

    # 2026-08, TRANSKRIPSIYA SIFAT DARVOZASI (foydalanuvchi so'rovi -- xato
    # transkripsiyani ("Allah'a sığındık" kabi turkcha "gibberish") hech
    # qachon tahlilga yubormaslik kerak): har bir urinishning sifati va
    # nechta urinish qilingani saqlanadi (debug + UI xabar uchun).
    ai_transcription_quality = Column(String(16), nullable=True)  # good | suspicious | failed
    ai_transcription_confidence = Column(Float, nullable=True)  # 0.0-1.0, tanlangan transkripsiyaning sifat darvozasi ishonchi
    ai_transcription_quality_reasons = Column(Text, nullable=True)  # JSON ro'yxat -- YAKUNIY tanlangan urinishning sabablari
    ai_transcription_attempts = Column(Integer, nullable=True)
    ai_transcription_attempts_log = Column(Text, nullable=True)  # JSON: [{"attempt","model","quality","reasons"}, ...]
    ai_analysis_confidence = Column(Float, nullable=True)  # 0.0-1.0, tahlil modelining o'z ishonchi
    ai_score_reasons = Column(Text, nullable=True)  # JSON: rubrika bo'yicha har bir mezon uchun ball+sabab
    # Stereo-kanal ajratishda QAYSI jismoniy kanal operator sifatida
    # ISHLATILGANI (0 yoki 1) -- bu HAR DOIM YOZUV TIZIMINING konvensiyasi
    # asosida, kontent-taxmin EMAS, lekin baribir "tekshirilmagan taxmin"
    # ekanini debug ko'rinishida aniq ko'rsatish uchun saqlanadi.
    ai_operator_channel = Column(Integer, nullable=True)

    # 2026-08 V6 (foydalanuvchi ANIQ so'ragan "DEBUG INFORMATION" bo'limi):
    # diarizatsiya + segment-darajasida qayta transkripsiya arxitekturasining
    # TO'LIQ debug JSON'i -- xom diarizatsiya segmentlari, guruhlangan
    # segment chegaralari, har bir guruh uchun gpt-transcribe'ga yuborilgan
    # audio/urinishlar/qayta-urinishlar soni, tanlangan yakuniy matn
    # (yoki [noaniq]), va gapiruvchi-xaritalash ishonchi. Faqat diarizatsiya
    # yo'li ishlatilganda to'ldiriladi (stereo-split/oddiy mono yo'lda `None`).
    ai_segment_debug_json = Column(Text, nullable=True)


class SmmSnapshot(Base):
    """Instagram Business / Facebook Page uchun HAR KUNLIK "hozirgi holat"
    suratlanishi (obunachilar soni va h.k.) -- `smm_sync.py` har kuni bir
    marta yozadi, shu orqali vaqt bo'yicha O'SISH grafigini chizish mumkin
    (Meta Graph API o'zi "tarixiy obunachilar sonini" bermaydi, faqat JORIY
    sonni beradi -- shuning uchun o'zimiz kunma-kun saqlab boramiz)."""
    __tablename__ = "smm_snapshots"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True, index=True)  # 2026-08 multi-tenant 1-bosqich -- hali hech qanday route bo'yicha filtrlamaydi
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
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True, index=True)  # 2026-08 multi-tenant 1-bosqich -- hali hech qanday route bo'yicha filtrlamaydi
    platform = Column(String(16), nullable=False, index=True)  # "instagram" | "facebook"
    external_id = Column(String(64), unique=True, nullable=False)  # IG media id / FB post id
    caption = Column(Text, nullable=True)
    permalink = Column(Text, nullable=True)
    media_type = Column(String(32), nullable=True)  # IMAGE | VIDEO | CAROUSEL_ALBUM | REEL | STATUS | ...
    thumbnail_url = Column(Text, nullable=True)  # post/video muqovasi -- "Eng faol postlar" jadvalida ko'rsatish uchun
    posted_at = Column(DateTime, nullable=True, index=True)
    like_count = Column(Integer, nullable=True, default=0)
    comments_count = Column(Integer, nullable=True, default=0)
    # 2026-08 (item 6, foydalanuvchi shikoyati -- "nechta repost" ko'rinmasdi):
    # avval BU FAQAT Facebook uchun to'g'ri hisoblanardi -- Instagram tomonida
    # `shares_count` doim 0 bilan qattiq yozib qo'yilgan edi (`smm_sync.py`),
    # chunki Instagram insights so'rovi bu metrikani UMUMAN so'ramas edi.
    # Endi ikkalasi uchun ham haqiqiy qiymat (`meta_api.get_instagram_media_insights`
    # yangilangan metrikalar ro'yxati orqali).
    #
    # MUHIM BUG FIX (2026-08, foydalanuvchi shikoyati: "smm haliyam notori
    # ishlayapti"): bu ustunda ILGARI `default=0` bor edi -- SQLAlchemy'da
    # bu FAQAT "ustun umuman berilmagan"da EMAS, balki qiymat ATAYLAB
    # `None` qilib berilganda ham ishga tushadi (ORM "berilmagan" bilan
    # "ataylab None" ni farqlay olmaydi) -- shuning uchun Instagram
    # insights so'rovi MUVAFFAQIYATSIZ bo'lib `smm_sync.py` ATAYLAB `None`
    # yozmoqchi bo'lganda ham, bazaga baribir "0" (soxta "tasdiqlangan
    # nol") yozilib qolar edi. `default` olib tashlandi -- endi `saved_count`/
    # `follows_count`/`reach`/`impressions` bilan BIR XIL qoidaga bo'ysunadi.
    shares_count = Column(Integer, nullable=True)
    saved_count = Column(Integer, nullable=True)   # faqat Instagram (FEED/REELS) -- Facebook uchun bu tushuncha yo'q, shuning uchun None ("—"), 0 emas
    # 2026-08 (item 6): "nechta obunachi qo'shildi videodan" -- Instagram
    # Graph API'ning "follows" metrikasi (FAQAT FEED va STORY turidagi media
    # uchun mavjud -- REELS uchun Meta bu metrikani UMUMAN bermaydi, va
    # Facebook'da ham post darajasida bunday metrika yo'q). NULL = "bu
    # media turi/platforma uchun Meta bu ma'lumotni bermaydi" (haqiqiy 0
    # bilan chalkashtirmaslik uchun, xuddi `reach`/`impressions`dagi kabi).
    follows_count = Column(Integer, nullable=True)
    reach = Column(Integer, nullable=True)
    impressions = Column(Integer, nullable=True)  # 2026-08dan: Instagram uchun bu ustunga endi "views" metrikasi yoziladi (eski "impressions"/"plays" metrikalari Meta tomonidan bekor qilingan -- pastga, meta_api.py'ga qarang)
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
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True, index=True)  # 2026-08 multi-tenant 1-bosqich -- hali hech qanday route bo'yicha filtrlamaydi
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
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True, index=True)  # 2026-08 multi-tenant 1-bosqich -- hali hech qanday route bo'yicha filtrlamaydi
    manager_id = Column(Integer, ForeignKey("managers.id"), nullable=True)
    manager_name = Column(String(255), nullable=True)  # kesh -- manager keyin o'chsa ham savol tarixi tushunarli qolsin
    question = Column(Text, nullable=False)
    is_resolved = Column(Boolean, default=False)
    created_at = Column(DateTime, default=dt.datetime.utcnow, index=True)


class IgDmConversation(Base):
    """Instagram Direct (DM) suhbatlarining ENG OXIRGI holati -- 2026-08,
    foydalanuvchi so'rovi ("ig chatlarni tahlilini ham qoshish kerak, lekin
    byudjetni yo'lini top, qimmat bo'p ketmasin"). `external_id` bo'yicha
    upsert qilinadi (SmmPost bilan bir xil naqsh).

    XARAJATNI NAZORAT QILISH STRATEGIYASI (ATAYLAB IKKI QATLAMGA
    AJRATILGAN):
      1. Ushbu jadvalning `last_message_from`/`is_unanswered`/`unanswered_since`
         maydonlari `ig_dm_sync.py` tomonidan HAR SAFAR (har 15 daqiqada)
         HECH QANDAY AI CHAQIRILMASDAN, oddiy vaqt/tomon solishtirish orqali
         hisoblanadi -- "menejer javob bermadi" ogohlantirishi UMUMAN AI
         talab qilmaydi, shuning uchun bu qism DEYARLI BEPUL.
      2. Faqat "lid sifati qanday" (`ai_lead_quality`/`ai_summary`) haqiqatan
         ham AI (gpt-4o-mini, arzon) talab qiladi -- va bu ham HAR xabar
         uchun EMAS, balki DAVRIY (odatda har 2-3 soatda, `ig_dm_analysis.py`)
         va FAQAT oxirgi tahlildan beri YANGI mijoz xabari kelgan suhbatlar
         uchun ishga tushadi (`ai_analyzed_message_count < message_count`
         solishtiruvi orqali -- o'zgarmagan suhbat qayta-qayta tahlil
         qilinib, pul isrof qilinmaydi)."""
    __tablename__ = "ig_dm_conversations"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True, index=True)  # 2026-08 multi-tenant 1-bosqich -- hali hech qanday route bo'yicha filtrlamaydi
    external_id = Column(String(64), unique=True, nullable=False)  # Meta Graph API suhbat (conversation) ID'i
    customer_ig_id = Column(String(64), nullable=True, index=True)  # mijozning Instagram-Scoped ID'i (IGSID)
    customer_username = Column(String(255), nullable=True)  # ma'lum bo'lsa (Meta har doim ham bermaydi)

    message_count = Column(Integer, nullable=False, default=0)  # shu suhbatda hozircha sinxronlangan JAMI xabar soni
    last_message_at = Column(DateTime, nullable=True, index=True)
    last_message_text = Column(Text, nullable=True)  # oxirgi xabar matni -- ro'yxatda "oldindan ko'rish" uchun
    last_message_from = Column(String(16), nullable=True)  # "customer" | "business"

    # Javobsiz-xabar holati -- FAQAT deterministik (AI ishtirokisiz) hisoblanadi.
    is_unanswered = Column(Boolean, nullable=False, default=False)  # oxirgi xabar mijozdan va biznes hali javob bermagan
    unanswered_since = Column(DateTime, nullable=True)  # mijozning javobsiz qolgan birinchi xabari vaqti
    unanswered_alert_sent_at = Column(DateTime, nullable=True)  # shu "javobsizlik davri" uchun Telegram ogohlantirishi allaqachon yuborilganmi (qayta-qayta yubormaslik uchun)

    # AI (gpt-4o-mini) davriy lid-sifat tahlili -- ixtiyoriy, faqat yangi
    # xabar bo'lsa yangilanadi.
    ai_lead_quality = Column(String(16), nullable=True)  # "hot" | "warm" | "cold" | None (hali tahlil qilinmagan)
    ai_summary = Column(Text, nullable=True)  # 1-2 gapli xulosa (nima haqida so'rayapti, nimani xohlaydi)
    ai_reasons = Column(Text, nullable=True)  # JSON ro'yxat -- bahoning qisqa sabablari
    ai_analyzed_message_count = Column(Integer, nullable=False, default=0)  # oxirgi AI tahlil paytida nechta xabar bor edi (o'zgarish borligini aniqlash uchun)
    ai_analyzed_at = Column(DateTime, nullable=True)
    ai_model = Column(String(64), nullable=True)
    ai_error = Column(Text, nullable=True)

    created_at = Column(DateTime, default=dt.datetime.utcnow)
    last_synced_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)


class IgDmMessage(Base):
    """Bitta Instagram DM xabari -- `IgDmConversation.message_count`/
    `last_message_*` shu jadvaldan HISOBLANADI, lekin AI tahlili uchun
    (`ig_dm_analysis.py`) suhbatning so'nggi xabarlari matn sifatida
    kerak bo'ladi, shuning uchun alohida saqlanadi (faqat metadata emas)."""
    __tablename__ = "ig_dm_messages"

    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey("ig_dm_conversations.id"), nullable=False, index=True)
    external_id = Column(String(64), unique=True, nullable=True)  # Meta xabar ID'i (dublikatni oldini olish uchun; ba'zan bo'sh kelishi mumkin)
    sender = Column(String(16), nullable=False)  # "customer" | "business"
    text = Column(Text, nullable=True)
    sent_at = Column(DateTime, nullable=True, index=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow)


class CustomField(Base):
    """Admin CRM anketa savollarini (lead'ni to'ldirishda menejer javob berishi
    kerak bo'lgan qo'shimcha maydonlarni) o'zi qo'sha/tahrirlay oladi -- kodga
    qattiq yozilmagan, dinamik. Har bir lead javobi Lead.extra_data (JSON)
    ichida `key` bo'yicha saqlanadi."""
    __tablename__ = "custom_fields"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True, index=True)  # 2026-08 multi-tenant 1-bosqich -- `key` hali GLOBAL unique (pastdagi Company docstring'ga qarang), keyingi bosqichda (company_id, key) bo'lishi kerak
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
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True, index=True)  # 2026-08 multi-tenant 1-bosqich -- `key` hali GLOBAL unique (pastdagi Company docstring'ga qarang), keyingi bosqichda (company_id, key) bo'lishi kerak
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
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True, index=True)  # 2026-08 multi-tenant 1-bosqich -- hali hech qanday route bo'yicha filtrlamaydi
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
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True, index=True)  # 2026-08 multi-tenant 1-bosqich -- hali hech qanday route bo'yicha filtrlamaydi
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


# 2026-08: `ai_sale_result` boshida `String(16)` edi, endi yangi tahlil
# sxemasida `"information_only"` (17 belgi) qiymati qo'shildi -- bu
# ALLAQACHON deploy qilingan ustunga sig'maydi (`_migrate_add_missing_columns`
# FAQAT yo'q ustunlarni QO'SHADI, mavjud ustun TURINI o'zgartirmaydi).
# Shuning uchun bu yerda ALOHIDA, xavfsiz (qayta ishga tushirilsa ham xato
# bermaydigan) "ustun turini kengaytirish" qadami qo'shildi.
_COLUMN_TYPE_WIDENING = {
    ("call_records", "ai_sale_result"): "VARCHAR(32)",
}


def _migrate_widen_columns() -> None:
    inspector = sa_inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for (table, col), new_type in _COLUMN_TYPE_WIDENING.items():
            if table not in existing_tables:
                continue
            try:
                conn.execute(sa_text(f'ALTER TABLE "{table}" ALTER COLUMN "{col}" TYPE {new_type}'))
            except Exception as e:
                logger.error("Migratsiya XATOSI (ustun turini kengaytirish): %s.%s -- %s", table, col, e)


# 2026-08 multi-tenant 1-bosqich: `company_id` ustuni qo'shilgan HAMMA
# jadvallar -- `ensure_default_company()` shu ro'yxat bo'yicha eski (hali
# `company_id IS NULL` bo'lgan) qatorlarni standart kompaniyaga biriktiradi.
_COMPANY_SCOPED_MODELS = [
    Manager, Lead, CallRecord, SmmSnapshot, SmmPost, Competitor,
    AssistantUnanswered, CustomField, FunnelStage, StandingTask, StandingReport,
    IgDmConversation,
]

DEFAULT_COMPANY_NAME = "Asosiy kompaniya"


def ensure_default_company() -> None:
    """MUHIM (2026-08, multi-tenant asosi -- 1-bosqich): `Company` jadvali
    yangi qo'shildi, lekin loyihada ALLAQACHON ko'p yillik haqiqiy ma'lumot
    bor (lidlar, menejerlar, qo'ng'iroqlar...) -- shularning HAMMASI, agar
    hech narsa qilinmasa, "kompaniyasiz" (`company_id IS NULL`) holda
    qolib ketardi. Bu funksiya HAR ISHGA TUSHISHDA (`init_db()` orqali)
    chaqiriladi va:

      1. Agar `companies` jadvali BO'SH bo'lsa -- joriy (hozirgi, yagona)
         biznesni ANIQ "Company #1" sifatida yaratadi (mavjud Meta/Telegram
         ENV o'zgaruvchilaridan meta_access_token/ad_account_id/telegram
         guruhini nusxalab, "unlimited" tarifda -- bu SIZNING o'z
         biznesingiz, mijoz emas).
      2. `company_id IS NULL` bo'lgan HAR BIR eski qatorni (barcha
         "company-scoped" jadvallarda, yuqoridagi `_COMPANY_SCOPED_MODELS`)
         shu Company #1'ga biriktiradi.

    Natijada: sayt xatti-harakati BUTUNLAY o'zgarmaydi (hech qayerda hali
    `company_id` bo'yicha filtr YO'Q -- bu keyingi bosqich), lekin
    ro'yxatdan o'tish/yangi kompaniyalar qo'shilishi uchun ma'lumotlar
    bazasi negizi tayyor bo'ladi."""
    session = get_session()
    try:
        default_company = session.query(Company).order_by(Company.id.asc()).first()
        if default_company is None:
            default_company = Company(
                name=os.environ.get("DEFAULT_COMPANY_NAME") or DEFAULT_COMPANY_NAME,
                plan="unlimited",
                is_active=True,
                meta_access_token=os.environ.get("META_ACCESS_TOKEN") or None,
                meta_ad_account_id=os.environ.get("META_AD_ACCOUNT_ID") or None,
                telegram_group_id=os.environ.get("TELEGRAM_AGENTS_GROUP_ID") or None,
            )
            session.add(default_company)
            session.commit()
            logger.warning(
                "Multi-tenant: standart '%s' yaratildi (id=%s)",
                default_company.name, default_company.id,
            )

        for model in _COMPANY_SCOPED_MODELS:
            updated = (
                session.query(model)
                .filter(model.company_id.is_(None))
                .update({model.company_id: default_company.id}, synchronize_session=False)
            )
            if updated:
                logger.warning(
                    "Multi-tenant: %s.company_id -- %s ta eski qator '%s'ga biriktirildi",
                    model.__tablename__, updated, default_company.name,
                )
        session.commit()
    finally:
        session.close()


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
    _migrate_widen_columns()
    seed_default_funnel_stages()
    ensure_default_company()


def get_session():
    if SessionLocal is None:
        raise RuntimeError("DATABASE_URL o'rnatilmagan.")
    return SessionLocal()
