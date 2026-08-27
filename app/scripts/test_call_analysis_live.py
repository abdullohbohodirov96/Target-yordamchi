#!/usr/bin/env python3
"""scripts/test_call_analysis_live.py

2026-08 V5, foydalanuvchi ANIQ so'ragan: HAQIQIY OpenAI API'ga (haqiqiy
`OPENAI_API_KEY` bilan) chaqiruv qiladigan, QO'LDA ishga tushiriladigan
sinov skripti -- lokal audio faylni to'liq pipeline (audio metadata ->
transkripsiya + SIFAT DARVOZASI -> [agar sifat "good" bo'lsa] tahlil)
orqali o'tkazadi va natijani konsolga chiqaradi.

MUHIM: bu skript CI/avtomatik test suite'ning BIR QISMI EMAS (offline
sinovlar -- `scripts/test_call_analysis_offline.py` -- HECH QACHON
haqiqiy tarmoq/API chaqiruvi qilmaydi). Bu skript FAQAT qo'lda, haqiqiy
`OPENAI_API_KEY` mavjud bo'lganda, haqiqiy audio namunasi bilan ishlatish
uchun -- masalan yangi model/prompt o'zgarishini production'ga chiqarishdan
OLDIN qo'lda tekshirish uchun.

Ishlatish:
    export OPENAI_API_KEY=sk-...
    cd app && python3 scripts/test_call_analysis_live.py sample.mp3
    cd app && python3 scripts/test_call_analysis_live.py sample.mp3 --channels 2

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


def main():
    parser = argparse.ArgumentParser(description="Haqiqiy OpenAI API bilan to'liq qo'ng'iroq-tahlil pipeline'ini qo'lda sinaydi.")
    parser.add_argument("audio_path", help="Lokal audio fayl (mp3/wav/ogg/m4a).")
    parser.add_argument("--channels", type=int, default=None, help="Kanal soni haqida QO'LDA maslahat (ffprobe mavjud bo'lmasa avtomatik aniqlanmaydi).")
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

    print("\n--- Transkripsiya + SIFAT DARVOZASI ---")
    outcome = ca.transcribe_with_quality_gate(audio_bytes, audio_format, duration_sec, channels)
    print(f"Model: {outcome['model']}")
    print(f"Sifat holati: {outcome['quality_status']} (ishonch: {outcome.get('confidence')})")
    print(f"Sabablar: {outcome.get('quality_reasons')}")
    print(f"Urinishlar soni: {len(outcome['attempts'])}")
    for a in outcome["attempts"]:
        print(f"  - urinish {a['attempt']}: model={a['model']}, sifat={a['quality']}, ishonch={a.get('confidence')}")
    print(f"\nTranskripsiya matni (birinchi 500 belgi):\n{(outcome['text'] or '')[:500]}")

    if not call_quality.is_acceptable(outcome["quality_status"]):
        print(
            "\n*** SIFAT DARVOZASI: transkripsiya YETARLI SIFATDA EMAS -- "
            "TAHLIL CHAQIRILMAYDI (bu KUTILGAN xatti-harakat, xato EMAS). ***"
        )
        return

    print("\n--- Tahlil (Structured Outputs) ---")
    result = ca._analyze_transcript(outcome["text"])
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
