"""competitor_analytics.py — har kuni soat 10:00da raqobatchilar
reklamalari haqida qisqa, amaliy hisobot tayyorlaydi (2026-08, foydalanuvchi
so'rovi: "raqobatchilarni nimaga target reklama yoqkan, qanday takliflar
beryapti, yangi mahsulot qo'shyaptimi -- shularni tahlil qilib bersin").

Xom reklama matnlari (`CompetitorAd`) yig'ilib, YENGIL OpenAI chaqiruvi
(`orchestrator.call_light`) orqali sintez qilinadi -- bu Targetolog
kabi haqiqiy Meta harakatini BAJARMAYDI, faqat matn tahlili, shuning uchun
qimmat Anthropic chaqiruvi shart emas (xarajat strategiyasi `orchestrator.py`
dagi bilan bir xil)."""

import orchestrator
from db import get_session, Competitor, CompetitorAd

SYSTEM_PROMPT = """Sen Meta Ads bo'yicha tajribali marketing tahlilchisan.
Senga bir nechta raqobatchi kompaniyaning HOZIRDA Facebook/Instagram'da
ishlab turgan reklamalarining matni beriladi (kompaniya nomi bo'yicha
guruhlangan). Vazifang:

1. Har bir raqobatchi UCHUN alohida (kompaniya nomini sarlavha qilib): qanday
   taklif/aksiya/mahsulot targ'ib qilinayotganini 1-2 gapda ayt. Agar oldingi
   holatga nisbatan YANGI narsa (yangi mahsulot, yangi aksiya, narx
   o'zgarishi) ko'ringan bo'lsa alohida ta'kidla.
2. Agar bir nechta raqobatchida umumiy naqsh ko'ringan bo'lsa (masalan
   bir nechtasi bir xil chegirma/mavsumiy aksiya qilyapti) - shuni alohida
   band qilib yoz.
3. Oxirida BIZNING kampaniyalarimiz uchun 2-3 ta amaliy tavsiya ber
   (masalan "ular X aksiyasini qilyapti, biz ham shunga o'xshash lekin
   farqli taklif sinab ko'rishimiz mumkin").

Javobni O'ZBEK tilida, qisqa va amaliy yoz -- bu Telegram xabari sifatida
yuboriladi, uzun umumiy-nazariy gap kerak emas, HAR doim konkret gapir."""


def build_daily_report() -> str | None:
    """Hech qanday faol raqobatchi bo'lmasa `None` qaytaradi (jim
    o'tkaziladi). Raqobatchilar bor-u, birortasida ham hozircha faol
    reklama topilmagan bo'lsa -- shuni ochiq aytadigan qisqa xabar
    qaytaradi (foydalanuvchi "hech narsa topilmadi" bilan "hali
    sinxronizatsiya bo'lmagan"ni farqlay olishi uchun)."""
    session = get_session()
    try:
        competitors = session.query(Competitor).filter_by(is_active=True).all()
        if not competitors:
            return None

        blocks = []
        has_any_ad = False
        for comp in competitors:
            ads = (
                session.query(CompetitorAd)
                .filter_by(competitor_id=comp.id, is_active=True)
                .order_by(CompetitorAd.last_seen_at.desc())
                .limit(8)
                .all()
            )
            if not ads:
                blocks.append(f"## {comp.name} ({comp.domain or '—'})\n(hozircha faol reklama topilmadi)")
                continue
            has_any_ad = True
            lines = [f"## {comp.name} ({comp.domain or '—'})"]
            for ad in ads:
                text = (ad.body_text or "").strip().replace("\n", " ")[:400]
                lines.append(f"- {text or '(matnsiz reklama)'}")
            blocks.append("\n".join(lines))

        if not has_any_ad:
            return (
                "📊 Raqobatchilar tahlili: hozircha birortasida ham faol "
                "reklama topilmadi (Meta Ad Library'da) -- yoki hali "
                "reklama yoqmaganlar, yoki sinxronizatsiya endi boshlandi."
            )

        user_content = "\n\n".join(blocks)
        try:
            summary = orchestrator.call_light(SYSTEM_PROMPT, user_content, max_tokens=900)
        except Exception as e:
            summary = f"⚠️ Tahlil qilib bo'lmadi ({e}), lekin xom ma'lumot:\n\n{user_content[:1500]}"

        return "📊 Kunlik raqobatchilar tahlili:\n\n" + summary
    finally:
        session.close()
