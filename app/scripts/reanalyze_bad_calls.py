#!/usr/bin/env python3
"""scripts/reanalyze_bad_calls.py

2026-08 V5, foydalanuvchi ANIQ so'ragan: yangi SIFAT DARVOZASI (`call_quality.py`)
va tahlil kodi (`call_analysis.py`) joriy qilinganidan OLDIN tahlil qilingan --
ya'ni ESKI, sifat tekshiruvisiz pipeline orqali o'tgan -- qo'ng'iroqlarni
ANIQLAB, ularni YANGI kod bilan (audio'dan boshlab, TO'LIQ) QAYTA tahlil
qilish uchun.

MUHIM (foydalanuvchi ANIQ so'ragan): bu skript AVTOMATIK ravishda deploy
paytida ISHGA TUSHIRILMAYDI va hech qanday scheduler/cron/webhookka
ULANMAGAN -- faqat ADMIN qo'lda, terminal orqali (masalan Render Shell'da
yoki lokal) ishga tushiradi.

Ishlatish:
    cd app && python3 scripts/reanalyze_bad_calls.py --dry-run
    cd app && python3 scripts/reanalyze_bad_calls.py --limit 20
    cd app && python3 scripts/reanalyze_bad_calls.py --call-id 1234

Nomzodlarni ANIQLASH mezoni (quyidagilardan BIRI YETARLI):
  1. `ai_transcription_quality` NULL -- eski pipeline sifat darvozasidan
     OLDIN yozilgan, demak UMUMAN tekshirilmagan.
  2. `ai_transcription_quality` "good"dan BOSHQA (suspicious/failed) --
     bunday holatda yangi kod ostida bu yozuv ALLAQACHON tahlilga
     yuborilmasligi kerak edi, lekin eski yozuv bo'lishi mumkin.
  3. `ai_stage` "transcription_failed" yoki "failed".
  4. Xom/normallashtirilgan transkripsiyada foydalanuvchi ko'rsatgan ANIQ
     bug misollariga o'xshash iboralar bor ("Allah'a", "Düğün", "sığındık"
     va h.k.) -- bu FAQAT QO'SHIMCHA, yordamchi belgi, yagona mezon EMAS
     (umumiy sifat-darvoza tekshiruvi ancha ishonchli manba).

Skript HECH QACHON audio fayllarni yoki bazadagi boshqa (call-analysis'ga
aloqasi bo'lmagan) ma'lumotlarni o'zgartirmaydi -- faqat shu bitta
qo'ng'iroqning AI-tahlil maydonlarini TOZALAYDI (audio'dan qayta boshlash
uchun) va `call_analysis.analyze_call_record()`ni chaqiradi.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import call_analysis as ca
from db import init_db, get_session, CallRecord

_KNOWN_BUG_MARKERS = [
    "allah'a", "düğün", "sığındık", "kılıç mı",
    # 2026-08 V6, foydalanuvchi HAQIQIY production misolidan (gpt-4o-transcribe-diarize'ning
    # o'z matnidan chiqqan hallyusinatsiya) -- yordamchi belgi sifatida qo'shildi.
    "duxovoy karina", "bitonkarige", "kachina aslam",
]


def _is_bad_candidate(call: CallRecord) -> bool:
    if call.ai_transcription_quality is None:
        return True
    if call.ai_transcription_quality != "good":
        return True
    if call.ai_stage in ("transcription_failed", "failed"):
        return True
    text = ((call.ai_raw_transcription or "") + " " + (call.ai_transcription or "")).lower()
    if any(marker in text for marker in _KNOWN_BUG_MARKERS):
        return True
    # 2026-08 V6, foydalanuvchi ANIQ ko'rsatgan bug: BUTUN qo'ng'iroq
    # "Mijoz:" deb belgilangan (bironta ham "Manager:"/"Speaker" yorlig'i
    # yo'q) -- bu ESKI (V6'gacha bo'lgan) tahlil-modeli-relabel xatosiga
    # xos aniq izdir, YANGI kod bilan qayta ishlashga arziydi.
    turns = ca.parse_transcript_turns(call.ai_transcription or "")
    if turns and all(t["speaker"] == "mijoz" for t in turns):
        return True
    return False


def _reset_for_full_reprocess(call: CallRecord) -> None:
    """`analyze_call_record()`ning `resume_from_transcript` yo'liga EMAS,
    TO'LIQ (audio'dan boshlab) qayta ishlash yo'liga tushishi uchun --
    eski/sifatsiz transkripsiya/tahlil maydonlarini TOZALAYDI."""
    call.ai_raw_transcription = None
    call.ai_transcription_quality = None
    call.ai_transcription_confidence = None
    call.ai_transcription_quality_reasons = None
    call.ai_segment_debug_json = None  # 2026-08 V6 -- eski segment-debug'ni ham tozalab, YANGI pipeline bilan qayta yozdiramiz
    call.ai_stage = "uploaded"
    call.ai_error = None


def main():
    parser = argparse.ArgumentParser(
        description="Eski/buzuq transkripsiya bilan tahlil qilingan qo'ng'iroqlarni YANGI kod bilan qayta tahlil qiladi."
    )
    parser.add_argument("--dry-run", action="store_true", help="Faqat nomzodlarni RO'YXATLAYDI, HECH NARSANI o'zgartirmaydi.")
    parser.add_argument("--limit", type=int, default=20, help="Bir martada qayta tahlil qilinadigan MAKSIMAL qo'ng'iroqlar soni (standart 20).")
    parser.add_argument("--call-id", type=int, default=None, help="Faqat BITTA aniq qo'ng'iroqni (ID bo'yicha) qayta tahlil qilish.")
    args = parser.parse_args()

    if not ca.is_configured():
        print("XATO: OPENAI_API_KEY sozlanmagan -- qayta tahlil ishlamaydi.")
        sys.exit(1)

    init_db()
    session = get_session()
    try:
        if args.call_id:
            call = session.get(CallRecord, args.call_id)
            if not call:
                print(f"Qo'ng'iroq #{args.call_id} topilmadi.")
                sys.exit(1)
            calls = [call]
        else:
            all_analyzed = (
                session.query(CallRecord)
                .filter(CallRecord.recording_url.isnot(None), CallRecord.ai_analyzed_at.isnot(None))
                .order_by(CallRecord.ai_analyzed_at.desc())
                .all()
            )
            calls = [c for c in all_analyzed if _is_bad_candidate(c)][: args.limit]

        print(f"Nomzodlar topildi: {len(calls)} ta.")
        for c in calls:
            print(f"  #{c.id}: sifat={c.ai_transcription_quality!r}, bosqich={c.ai_stage!r}, baho={c.ai_score}")

        if not calls:
            return
        if args.dry_run:
            print("\n--dry-run: hech narsa o'zgartirilmadi (haqiqiy qayta tahlil uchun --dry-run'siz ishga tushiring).")
            return

        ok, failed = 0, 0
        for c in calls:
            print(f"Qayta ishlanmoqda: #{c.id} ...")
            _reset_for_full_reprocess(c)
            session.commit()
            try:
                ca.analyze_call_record(session, c)
                print(f"  OK: yangi sifat={c.ai_transcription_quality!r}, bosqich={c.ai_stage!r}, baho={c.ai_score}")
                ok += 1
            except Exception as e:
                print(f"  XATO: {e}")
                failed += 1

        print(f"\nTugadi: {ok} ta muvaffaqiyatli, {failed} ta xato bilan.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
