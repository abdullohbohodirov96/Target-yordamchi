#!/usr/bin/env python3
"""scripts/test_call_analysis_live.py

2026-08 V5, foydalanuvchi ANIQ so'ragan: HAQIQIY OpenAI API'ga (haqiqiy
`OPENAI_API_KEY` bilan) chaqiruv qiladigan, QO'LDA ishga tushiriladigan
sinov skripti -- lokal audio faylni to'liq pipeline (audio metadata ->
transkripsiya + SIFAT DARVOZASI -> [agar sifat "good" bo'lsa] tahlil)
orqali o'tkazadi va natijani konsolga chiqaradi.

2026-08 V6, foydalanuvchi ANIQ so'ragan qo'shimcha ("REAL AUDIO REGRESSION
TEST"): endi diarizatsiya + segment-darajasidagi qayta transkripsiya
yo'li ishlatilgan bo'lsa (`_reassemble_call_from_diarization`) -- xom
diarizatsiya segmentlari, ularning guruhlanishi, har bir guruh uchun
QAYSI audio bo'lak `gpt-4o-transcribe`ga yuborilgani, segment natijasi,
segment boshiga qayta-urinishlar soni, tanlangan yakuniy matn (yoki
`[noaniq]`) va gapiruvchi-xaritalash ishonchi ALOHIDA chop etiladi.
`--skip-analysis` bayrog'i orqali TAHLIL (Structured Outputs) bosqichi
BUTUNLAY o'tkazib yuboriladi -- foydalanuvchi ANIQ so'ragan: "faqat
transkripsiyani sinash uchun savdo-tahlili SHART bo'lmasin".

MUHIM: bu skript CI/avtomatik test suite'ning BIR QISMI EMAS (offline
sinovlar -- `scripts/test_call_analysis_offline.py` -- HECH QACHON
haqiqiy tarmoq/API chaqiruvi qilmaydi). Bu skript FAQAT qo'lda, haqiqiy
`OPENAI_API_KEY` mavjud bo'lganda, haqiqiy audio namunasi bilan ishlatish
uchun -- masalan yangi model/prompt o'zgarishini production'ga chiqarishdan
OLDIN qo'lda tekshirish uchun, yoki foydalanuvchining o'z real qo'ng'iroq
namunasini (masalan "933.mp3") qo'lda tekshirish uchun.

Ishlatish:
    export OPENAI_API_KEY=sk-...
    cd app && python3 scripts/test_call_analysis_live.py sample.mp3
    cd app && python3 scripts/test_call_analysis_live.py sample.mp3 --channels 2
    cd app && python3 scripts/test_call_analysis_live.py /path/to/933.mp3 --skip-analysis
    cd app && python3 scripts/test_call_analysis_live.py /path/to/933.mp3 --direction outgoing

Xavfsizlik: bu skript API KALITINI HECH QACHON konsolga chiqarmaydi/
logga yozmaydi -- faqat environment variable'dan o'qiydi (mavjudligini
tekshiradi, qiymatini EMAS)."""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import call_analysis as ca
import call_quality


def _print_segment_debug(segment_debug_json: "str | None") -> None:
    """2026-08 V6 -- `ai_segment_debug_json` (diarizatsiya + segment
    darajasidagi qayta transkripsiya) tarkibini o'qish OSON bo'lgan
    ko'rinishda konsolga chiqaradi (spec-bo'lim 16: diarizatsiya
    segmentlari, guruhlangan segmentlar, segment transkriptlari,
    gapiruvchi-xaritalash, yakuniy transkript)."""
    if not segment_debug_json:
        print("(diarizatsiya + segment-darajasidagi yo'l ISHLATILMAGAN -- stereo-split yoki oddiy mono zanjir ishlatilgan.)")
        return
    try:
        debug = json.loads(segment_debug_json)
    except (TypeError, ValueError):
        print("(segment_debug_json JSON sifatida o'qilmadi -- xom qiymat:)")
        print(segment_debug_json)
        return

    print(f"Diarizatsiya modeli: {debug.get('diarize_model')}")
    groups = debug.get("groups") or []
    print(f"Guruhlangan segmentlar soni: {len(groups)} (xom diarizatsiya bo'laklari guruhlangandan keyin)")
    for g in groups:
        print(
            f"  [{g.get('group_index')}] xom-gapiruvchi={g.get('raw_speaker')!r} "
            f"vaqt=[{g.get('start')}s..{g.get('end')}s] ({g.get('duration_sec')}s, "
            f"{g.get('diar_segment_count')} ta xom diarizatsiya bo'lagidan)"
        )
        if g.get("diar_text_preview"):
            print(f"        diarizatsiyaning O'Z matni (ISHLATILMAYDI, faqat ma'lumot uchun): {g['diar_text_preview'][:150]!r}")
        print(f"        qayta-urinishlar soni: {g.get('retries', 0)}, sifat: {g.get('quality')}, ishonch: {g.get('confidence')}")
        if g.get("used_noaniq"):
            print(f"        YAKUNIY: [noaniq] (hech qanday urinish 'good' bermadi)")
            if g.get("rejected_text_preview"):
                print(f"        (rad etilgan/shubhali matn, DEBUG uchun: {g['rejected_text_preview'][:150]!r})")
        else:
            print(f"        YAKUNIY MATN: {(g.get('final_text') or '')[:200]!r}")

    mapping = debug.get("speaker_mapping") or {}
    print(f"\nGapiruvchi-xaritalash: ishonchli={mapping.get('confident')}, "
          f"operator deb topilgan xom-ID={mapping.get('mapped_operator_raw_speaker')!r}, "
          f"topilgan xom-gapiruvchilar={mapping.get('raw_speakers_found')}, "
          f"call_direction hisobga olindi={mapping.get('call_direction_considered')!r}")
    print(f"[noaniq] ulushi: {debug.get('noaniq_ratio')}")


def main():
    parser = argparse.ArgumentParser(description="Haqiqiy OpenAI API bilan to'liq qo'ng'iroq-tahlil pipeline'ini qo'lda sinaydi.")
    parser.add_argument("audio_path", help="Lokal audio fayl (mp3/wav/ogg/m4a).")
    parser.add_argument("--channels", type=int, default=None, help="Kanal soni haqida QO'LDA maslahat (ffprobe mavjud bo'lmasa avtomatik aniqlanmaydi).")
    parser.add_argument("--direction", choices=["incoming", "outgoing"], default=None, help="Qo'ng'iroq yo'nalishi haqida QO'LDA maslahat (CallRecord.direction'ga mos -- gapiruvchi-xaritalashda YORDAMCHI signal sifatida ishlatiladi).")
    parser.add_argument("--skip-analysis", action="store_true", help="FAQAT transkripsiya+sifat-darvozasini sinaydi -- tahlil (Structured Outputs) bosqichi BUTUNLAY chaqirilmaydi (savdo-tahlili SHART emas).")
    args = parser.parse_args()

    if not ca.is_configured():
        print("XATO: OPENAI_API_KEY sozlanmagan (environment variable sifatida). Bu skript FAQAT haqiqiy kalit bilan ishlaydi.")
        sys.exit(1)
    if not os.path.exists(args.audio_path):
        print(f"XATO: fayl topilmadi: {args.audio_path}")
        sys.exit(1)

    ca.log_model_config()

    with open(args.audio_path, "rb") as f:
        audio_bytes = f.read()
    audio_format = os.path.splitext(args.audio_path)[1].lstrip(".").lower() or "mp3"

    print(f"\n--- Audio metadata ({'ffprobe' if ca.ffprobe_available() else 'ffprobe MAVJUD EMAS'}) ---")
    metadata = ca.probe_audio_metadata(audio_bytes, audio_format)
    if metadata:
        print(json.dumps(metadata, ensure_ascii=False, indent=2))
        channels = metadata.get("channels")
        duration_sec = metadata.get("duration_sec")
    else:
        print("ffprobe mavjud emas yoki metadata o'qib bo'lmadi -- kanal soni QO'LDA berilgan qiymatdan olinadi.")
        channels = args.channels
        duration_sec = None
    if not ca.ffmpeg_available():
        print(
            "\n*** OGOHLANTIRISH: ffmpeg MAVJUD EMAS -- 2026-08 V6 segment-darajasidagi "
            "qayta transkripsiya (diarizatsiya + ffmpeg-kesish) ISHLATILMAYDI, oddiy "
            "mono zanjirga o'tiladi. Agar '933.mp3' kabi real muammoli namunani "
            "V6 arxitekturasi bilan sinamoqchi bo'lsangiz, ffmpeg o'rnatilgan "
            "muhitda (masalan Render Shell/Docker konteynerida) ishga tushiring. ***"
        )

    print("\n--- Transkripsiya + SIFAT DARVOZASI (diarizatsiya + segment-darajasidagi qayta transkripsiya, 2026-08 V6) ---")
    outcome = ca.transcribe_with_quality_gate(audio_bytes, audio_format, duration_sec, channels, call_direction=args.direction)
    print(f"Tanlangan model: {outcome['model']}")
    print(f"Sifat holati: {outcome['quality_status']} (ishonch: {outcome.get('confidence')})")
    print(f"Sabablar: {outcome.get('quality_reasons')}")
    print(f"Urinishlar soni (yuqori darajadagi -- stereo/diarizatsiya/mono): {len(outcome['attempts'])}")
    for a in outcome["attempts"]:
        print(f"  - urinish {a['attempt']}: model={a['model']}, sifat={a['quality']}, ishonch={a.get('confidence')}, izoh={a.get('note')}")

    print("\n--- Diarizatsiya/segment-darajasidagi to'liq debug (spec-bo'lim 16) ---")
    _print_segment_debug(outcome.get("segment_debug_json"))

    print(f"\n--- Yakuniy (gapiruvchi-yorliqlangan) transkript ---\n{outcome['text'] or '(bo’sh)'}")

    if args.skip_analysis:
        print("\n(--skip-analysis: tahlil bosqichi o'tkazib yuborildi -- foydalanuvchi ANIQ so'ragan, savdo-tahlili SHART emas.)")
        return

    if not call_quality.is_acceptable(outcome["quality_status"]):
        print(
            "\n*** SIFAT DARVOZASI: transkripsiya YETARLI SIFATDA EMAS -- "
            "TAHLIL CHAQIRILMAYDI (bu KUTILGAN xatti-harakat, xato EMAS). ***"
        )
        return

    print("\n--- Tahlil (Structured Outputs) ---")
    turns = ca.parse_transcript_turns(outcome["text"])
    result = ca._analyze_transcript(outcome["text"], turns)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
