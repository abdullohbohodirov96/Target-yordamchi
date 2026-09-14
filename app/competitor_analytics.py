"""competitor_analytics.py — raqobatchilar reklamalari haqida qisqa,
amaliy hisobot tayyorlaydi (2026-08, foydalanuvchi so'rovi: "raqobatchilarni
nimaga target reklama yoqkan, qanday takliflar beryapti, yangi mahsulot
qo'shyaptimi -- shularni tahlil qilib bersin").

2026-09 (foydalanuvchi so'rovi -- "har 2 kunda bir raqobatchilani analiz
qilsin va tahlilni bot orqali yuborsin"): ILGARI `build_daily_report()`
BARCHA faol raqobatchilarni BITTA xabarga birlashtirar edi. Endi
`analyze_due_competitor()` navbati kelgan (kamida `ROTATION_DAYS` kun
oldin tahlil qilingan yoki umuman tahlil qilinmagan) BITTA raqobatchini
tanlaydi, uni Ad Library orqali yangilaydi va ALOHIDA xabar tayyorlaydi
-- shu orqali har bir raqobatchi o'zining navbatida, alohida xabar bilan
ko'rib chiqiladi.

MUHIM CHEKLOV (foydalanuvchiga aytilishi kerak bo'lgan narsa): Meta Ad
Library'ning OMMAVIY API'si (`ads_archive`) oddiy tijorat e'lonlari uchun
haqiqiy auditoriya/target sozlamalarini (yosh, jins, joylashuv,
qiziqishlar) UMUMAN qaytarmaydi -- faqat reklama matni/kreativini. Shuning
uchun "kimga target qilyapti" degan savolga aniq javob emas, FAQAT
reklama matnidan kelib chiqqan AI TAXMINI beriladi -- bu narsa har bir
hisobotda ANIQ shunday deb yozib qo'yiladi (foydalanuvchi bilan
kelishilgan qaror, 2026-09).

Xom reklama matnlari (`CompetitorAd`) yig'ilib, YENGIL OpenAI chaqiruvi
(`orchestrator.call_light`) orqali sintez qilinadi -- bu Targetolog
kabi haqiqiy Meta harakatini BAJARMAYDI, faqat matn tahlili, shuning uchun
qimmat Anthropic chaqiruvi shart emas (xarajat strategiyasi `orchestrator.py`
dagi bilan bir xil)."""

import datetime as dt

import orchestrator
import competitor_sync
from db import get_session, Competitor, CompetitorAd, CompetitorAnalysis

# 2 kunda bir marta -- foydalanuvchi aniq shunday so'ragan ("har 2 kunda
# bir raqobatchilani analiz qilsin").
ROTATION_DAYS = 2

SYSTEM_PROMPT_SINGLE = """Sen Meta Ads bo'yicha tajribali marketing tahlilchisan.
Senga BITTA raqobatchi kompaniyaning HOZIRDA Facebook/Instagram'da ishlab
turgan reklamalarining matni beriladi.

MUHIM CHEKLOV: senga faqat reklama MATNI (sarlavha/tavsif) berilgan --
Meta Ad Library ommaviy API'si haqiqiy auditoriya/target sozlamalarini
(yosh, jins, joylashuv, qiziqishlar) UMUMAN bermaydi. Shuning uchun
"TAXMINIY AUDITORIYA" bo'limida FAQAT reklama matni/tili/taklifidan kelib
chiqib ehtimoliy auditoriyani TAXMIN qilasan -- bu HAQIQIY Meta sozlamasi
EMASLIGINI albatta ta'kidlaysan.

Javobni ANIQ shu ikki bo'lim bilan, O'ZBEK tilida ber:

## Taklif
(1-3 gapda: qanday mahsulot/aksiya/taklif targ'ib qilinyapti, narx/chegirma
zikr etilganmi, oldingi holatga nisbatan yangi narsa bormi)

## Taxminiy auditoriya (bu -- AI taxmini, Meta'ning haqiqiy sozlamasi emas)
(reklama matni/tili/taklifidan kelib chiqib, ehtimol kimga mo'ljallangan
bo'lishi mumkinligi -- 1-2 gap)

Oxirida BIZNING kampaniyalarimiz uchun 1-2 ta amaliy tavsiya ber (masalan
"ular X aksiyasini qilyapti, biz ham shunga o'xshash lekin farqli taklif
sinab ko'rishimiz mumkin"). Qisqa va amaliy yoz -- bu Telegram xabari
sifatida yuboriladi, uzun umumiy-nazariy gap kerak emas, HAR doim konkret
gapir."""


def _pick_due_competitor(session) -> Competitor | None:
    """Faol raqobatchilar orasidan navbati kelganini tanlaydi -- ENG
    KO'P VAQT tahlil qilinmagani (yoki umuman tahlil qilinmagani) BIRINCHI,
    lekin FAQAT agar oxirgi tahlildan beri kamida `ROTATION_DAYS` kun
    o'tgan bo'lsa. Raqobatchilar odatda kam (bir nechta) bo'lgani uchun
    Python'da saralash SQL dialektlar orasidagi NULL-tartib farqidan
    (Postgres/SQLite) xoli va sodda."""
    cutoff = dt.datetime.utcnow() - dt.timedelta(days=ROTATION_DAYS)
    competitors = session.query(Competitor).filter_by(is_active=True).all()
    due = [c for c in competitors if c.last_analyzed_at is None or c.last_analyzed_at <= cutoff]
    if not due:
        return None
    due.sort(key=lambda c: c.last_analyzed_at or dt.datetime.min)
    return due[0]


def _build_report(competitor: Competitor, ads: list[CompetitorAd]) -> dict:
    lines = [f"## {competitor.name} ({competitor.domain or '—'})"]
    for ad in ads[:8]:
        text = (ad.body_text or "").strip().replace("\n", " ")[:400]
        lines.append(f"- {text or '(matnsiz reklama)'}")
    user_content = "\n".join(lines)
    try:
        summary = orchestrator.call_light(SYSTEM_PROMPT_SINGLE, user_content, max_tokens=500)
    except Exception as e:
        summary = f"⚠️ Tahlil qilib bo'lmadi ({e}), lekin xom ma'lumot:\n\n{user_content[:1200]}"
    telegram_text = f"📊 Raqobatchi tahlili: {competitor.name} ({competitor.domain or '—'})\n\n{summary}"
    return {"telegram_text": telegram_text, "summary_text": summary}


def _analyze(session, competitor: Competitor, *, resync: bool = True) -> dict:
    """Bitta (allaqachon aniqlangan) raqobatchini tahlil qiladi:
    xohlasa qayta sinxronlaydi (`resync`), joriy faol e'lonlarini o'qiydi,
    LLM orqali hisobot tayyorlaydi, `CompetitorAnalysis`ga yozadi va
    `last_analyzed_at`ni yangilaydi. Committ shu yerda qilinadi."""
    if resync:
        competitor_sync.sync_one(session, competitor)

    ads = (
        session.query(CompetitorAd)
        .filter_by(competitor_id=competitor.id, is_active=True)
        .order_by(CompetitorAd.last_seen_at.desc())
        .limit(8)
        .all()
    )

    if not ads:
        summary_text = None
        telegram_text = (
            f"📊 Raqobatchi tahlili: {competitor.name} ({competitor.domain or '—'})\n\n"
            "Hozircha faol reklama topilmadi (Meta Ad Library'da) -- yoki "
            "hali reklama yoqmagan, yoki sinxronizatsiya endi boshlandi."
        )
    else:
        report = _build_report(competitor, ads)
        summary_text = report["summary_text"]
        telegram_text = report["telegram_text"]

    competitor.last_analyzed_at = dt.datetime.utcnow()
    session.add(CompetitorAnalysis(
        company_id=competitor.company_id, competitor_id=competitor.id,
        summary_text=summary_text, ads_analyzed_count=len(ads),
    ))
    session.commit()
    return {
        "competitor_id": competitor.id, "competitor_name": competitor.name,
        "telegram_text": telegram_text, "summary_text": summary_text,
        "ads_analyzed_count": len(ads),
    }


def analyze_due_competitor() -> dict | None:
    """Navbati kelgan (2 kun yoki undan ko'p tahlil qilinmagan) BITTA
    faol raqobatchini tanlaydi va tahlil qiladi (scheduler'ning kunlik
    vazifasi shundan foydalanadi). Hech qanday faol raqobatchi yo'q --
    yoki bor-u, birortasining ham navbati kelmagan -- bo'lsa `None`
    qaytaradi (jim o'tkaziladi, xato emas)."""
    session = get_session()
    try:
        competitor = _pick_due_competitor(session)
        if competitor is None:
            return None
        return _analyze(session, competitor)
    finally:
        session.close()


def analyze_competitor_now(competitor_id: int) -> dict | None:
    """Sozlamalar sahifasidagi "Hozir tekshir" tugmasi uchun -- 2 kunlik
    navbatni E'TIBORGA OLMASDAN, aniq bitta raqobatchini DARHOL tahlil
    qiladi (va shu bilan uning navbat hisobini ham yangilaydi)."""
    session = get_session()
    try:
        competitor = session.get(Competitor, competitor_id)
        if competitor is None or not competitor.is_active:
            return None
        return _analyze(session, competitor)
    finally:
        session.close()
