"""call_quality.py — transkripsiya SIFAT DARVOZASI (quality gate).

2026-08, foydalanuvchi haqiqiy muammoni ko'rsatdi: ba'zi o'zbekcha
qo'ng'iroq yozuvlari transkripsiya qilinganda TURKCHA, ARABCHA,
PORTUGALCHA yoki umuman ma'nosiz/inglizcha matnga aylanib qolyapti --
masalan:

    "Allah'a sığındık."                       (turkcha)
    "Düğün yurdu yok kılıç mı ders..."         (turkcha)
    "Hello, thank you very much today."        (inglizcha "gibberish")
    arabcha yozuv (arab alifbosi bilan)
    portugalcha-o'xshash matn (ã/õ bilan)

Bu OpenAI'ning audio-transkripsiya modellarida (Whisper oilasi va
undan keyingi gpt-4o-transcribe oilasida ham) MA'LUM, hujjatlashtirilgan
"hallyusinatsiya" xatti-harakati -- audio sifati past/shovqinli/juda
qisqa yoki boshqa "kutilmagan" bo'lsa, model "bilmayman" deb aytish
o'rniga BOSHQA tilda ravon ko'rinadigan, lekin MA'NOSIZ matn "to'qib
chiqaradi". O'zbek va turk tillari ikkalasi ham turkiy til oilasiga
kirgani uchun, model ayni shu ikkovi orasida "sirg'alib ketishi"
ayniqsa ehtimoldan yiroq emas -- lekin arabcha/portugalcha/inglizcha
holatlar ham xuddi shu umumiy hallyusinatsiya muammosining boshqa
ko'rinishlari, xolos.

MUHIM: bu yerda hech qanday tashqi til-aniqlash kutubxonasi (masalan
`langdetect`) ISHLATILMAYDI -- sababi, umumiy kutubxonalar o'zbek tilini
alohida sinf sifatida TANIMAYDI (o'rgatilgan 55+ tilga kirmaydi), demak
HAQIQIY, TO'G'RI o'zbekcha matnni ham "turkcha" deb noto'g'ri
tasniflashi mumkin edi -- bu esa aynan foydalanuvchi TAQIQLAGAN narsa
("legitimate Uzbek+rus kod-almashinuvini FAQAT ruscha so'z borligi
uchun rad etma" qoidasi bilan bir xil ruhda).

Shuning uchun bu yerda ANIQ, DETERMINISTIK, deyarli nol yolg'on-signal
beruvchi belgilar ishlatiladi:

  1. YOZUV TIZIMI (script) MOS KELMASLIGI -- ENG ISHONCHLI signal: agar
     matnda arab alifbosi (arabcha/forscha/urdu), ibroniy, xitoy/yapon/
     koreys belgilari topilsa -- bu 100% ishonch bilan noto'g'ri
     (o'zbekcha/ruscha bunday belgilarni UMUMAN ishlatmaydi).
  2. TURKCHA/PORTUGALCHAGA XOS LOTIN HARFLARI: standart o'zbek lotin
     alifbosi (sh, ch, oʻ, gʻ digraflar/apostrof bilan) VA rus krill
     alifbosi -- ikkalasi ham "ı", "ğ", "ş", "ç", "ö", "ü" (turkcha) yoki
     "ã", "õ" (portugalcha) harflarini UMUMAN ishlatmaydi.
  3. TAKRORLANISH: hallyusinatsiya ko'pincha bir xil so'z/iboraning
     tinimsiz takrorlanishi ko'rinishida bo'ladi.
  4. UZUNLIK/DAVOMIYLIK NISBATI: audio necha soniya davom etgani bilan
     transkripsiya necha so'zdan iboratligi taqqoslanadi.
  5. INGLIZCHA "GIBBERISH" (yumshoq signal): agar matn deyarli TO'LIQ
     odatiy inglizcha funksiya-so'zlaridan iborat bo'lsa VA hech qanday
     o'zbek/rus belgisi topilmasa -- bu ham shubhali (lekin qattiq rad
     etilmaydi, chunki brend nomlari/raqamlar tabiiy ravishda inglizcha
     bo'lishi mumkin).
  6. KUTILGAN LUG'AT ISHORASI (yumshoq, ijobiy signal, HECH QACHON
     yolg'iz o'zi rad etish uchun ishlatilmaydi).

Har bir funksiya `confidence` (0.0-1.0) ham qaytaradi -- UI'da "AI tahlil
aniqligi past" kabi ogohlantirish ko'rsatish uchun."""

from __future__ import annotations

import re

import call_glossary

# Turkcha/portugalchaga XOS, standart o'zbek lotin/rus krill alifbosida
# UMUMAN uchramaydigan lotin harflari.
_FOREIGN_LATIN_MARKER_CHARS = set("ığşçöüĞŞÇÖÜãõÃÕ") | {"İ"}

# Arab, ibroniy, xitoy/yapon/koreys yozuv tizimlari -- o'zbekcha/ruscha
# matnda BUTUNLAY uchramasligi kerak bo'lgan unicode diapazonlar.
_FOREIGN_SCRIPT_RANGES = [
    (0x0600, 0x06FF, "arab"),      # arabcha/forscha/urdu
    (0x0590, 0x05FF, "ibroniy"),
    (0x4E00, 0x9FFF, "xitoycha"),
    (0x3040, 0x30FF, "yaponcha"),
    (0xAC00, 0xD7A3, "koreyscha"),
]

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

# Odatiy o'zbek/rus so'zlashuv/qo'ng'iroq-markazi so'zlari -- FAQAT
# yumshoq, IJOBIY signal sifatida.
_EXPECTED_COMMON_WORDS = {
    "ha", "yoq", "yo'q", "yoʻq", "rahmat", "mumkin", "kerak", "salom",
    "assalomu", "alaykum", "xayr", "tushunarli", "bor", "narx", "qancha",
    "yetkazib", "bering", "bo'ladi", "boʻladi", "shu", "menejer", "mijoz",
    "да", "нет", "спасибо", "хорошо", "можно", "нужно", "алло",
    "здравствуйте", "цена", "доставка",
}

# Odatiy inglizcha funksiya-so'zlar -- FAQAT "bu matn asosan inglizcha
# gibberish ko'rinadimi" degan yumshoq signal uchun.
_ENGLISH_STOPWORDS = {
    "the", "is", "are", "hello", "thank", "you", "how", "today", "very",
    "much", "please", "and", "for", "with", "this", "that", "have", "was",
    "were", "what", "when", "where", "why", "would", "could", "should",
}


def _foreign_script_hits(text: str) -> "list[str]":
    found = []
    for lo, hi, name in _FOREIGN_SCRIPT_RANGES:
        if any(lo <= ord(c) <= hi for c in text):
            found.append(name)
    return found


def _foreign_latin_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    marked = sum(1 for c in letters if c in _FOREIGN_LATIN_MARKER_CHARS)
    return marked / len(letters)


def _max_repetition_run(words: "list[str]") -> int:
    if not words:
        return 0
    best = run = 1
    for i in range(1, len(words)):
        if words[i].lower() == words[i - 1].lower():
            run += 1
            best = max(best, run)
        else:
            run = 1
    return best


def _unique_word_ratio(words: "list[str]") -> float:
    if not words:
        return 1.0
    return len(set(w.lower() for w in words)) / len(words)


def _has_expected_uzbek_ru_signal(text: str, words: "list[str]") -> bool:
    has_cyrillic = any("Ѐ" <= c <= "ӿ" for c in text)
    low = text.lower()
    # FAQAT apostrof-belgili oʻ/gʻ (Uzbek lotin alifbosiga XOS -- boshqa
    # tillarda deyarli uchramaydi). Oddiy "sh"/"ch" harflar birikmasi
    # BAHOGA OLINMAYDI -- bu ingliz/boshqa ko'p tillarda ham tabiiy
    # uchraydi (masalan "much", "which"), demak signal sifatida ISHONCHSIZ.
    has_uzbek_apostrophe_digraph = any(m in low for m in ("o'", "g'", "oʻ", "gʻ"))
    has_common_word = any(w.lower() in _EXPECTED_COMMON_WORDS for w in words)
    has_glossary_term = any(term.lower() in low for term in call_glossary.GLOSSARY_TERMS)
    return has_cyrillic or has_uzbek_apostrophe_digraph or has_common_word or has_glossary_term


def evaluate_transcription_quality(text: str, audio_duration_sec: "float | None" = None) -> dict:
    """Transkripsiya matnini baholaydi. Qaytaradi:
    `{"status": "good"|"suspicious"|"failed", "confidence": 0.0-1.0, "reasons": [str, ...]}`.

    QOIDA (foydalanuvchi ANIQ so'ragan): bu validator chin o'zbekcha+rus
    kod-almashinuvini ASLO rad etmaydi -- "kutilmagan til" signali FAQAT
    yozuv tizimi mos kelmasligi / turkcha-portugalchaga xos harflar
    orqali (modul docstringiga qarang), umumiy til-aniqlagich orqali
    EMAS."""
    reasons = []
    text = (text or "").strip()

    if not text:
        return {"status": "failed", "confidence": 0.0, "reasons": ["Transkripsiya bo'sh."]}

    words = _WORD_RE.findall(text)
    word_count = len(words)

    # 1) Yozuv tizimi mos kelmasligi -- ENG ISHONCHLI, deyarli 0% yolg'on-signal.
    script_hits = _foreign_script_hits(text)
    if script_hits:
        reasons.append(f"Matnda {', '.join(script_hits)} yozuv tizimiga xos belgilar topildi -- o'zbekcha/ruscha bunday belgilarni ishlatmaydi.")
        return {"status": "failed", "confidence": 0.02, "reasons": reasons}

    # 2) Turkcha/portugalchaga xos lotin harflari.
    foreign_ratio = _foreign_latin_ratio(text)
    if foreign_ratio > 0.015 and len(text) > 8:
        reasons.append(
            f"Turkcha/portugalchaga xos harflar ('ı','ğ','ş','ç','ö','ü','ã','õ') matnning {foreign_ratio:.1%} "
            "qismida topildi -- model boshqa tilga 'sirg'alib ketgan' bo'lishi mumkin."
        )
        return {"status": "failed", "confidence": 0.05, "reasons": reasons}

    # 3) Takrorlanish (hallyusinatsiyaga xos naqsh).
    max_run = _max_repetition_run(words)
    if word_count >= 6 and max_run >= 6:
        reasons.append(f"Bir xil so'z {max_run} marta ketma-ket takrorlangan (hallyusinatsiyaga xos naqsh).")
        return {"status": "failed", "confidence": 0.1, "reasons": reasons}

    # 4) Davomiylikka nisbatan juda qisqa.
    if audio_duration_sec and audio_duration_sec > 15 and word_count < 3:
        reasons.append(
            f"Audio {audio_duration_sec:.0f} soniya davom etgan, lekin transkripsiyada "
            f"atigi {word_count} ta so'z bor -- juda qisqa."
        )
        return {"status": "failed", "confidence": 0.15, "reasons": reasons}

    # ---- "suspicious" darajadagi (yengilroq) signallar ----
    suspicious = False
    confidence = 1.0

    unique_ratio = _unique_word_ratio(words)
    if word_count > 20 and unique_ratio < 0.35:
        suspicious = True
        confidence -= 0.35
        reasons.append(f"So'zlarning faqat {unique_ratio:.0%} noyob -- ortiqcha takrorlanish.")

    if audio_duration_sec and audio_duration_sec > 10:
        wps = word_count / audio_duration_sec
        if wps < 0.4:
            suspicious = True
            confidence -= 0.25
            reasons.append(f"So'zlash tezligi juda past ({wps:.2f} so'z/soniya) -- audio davomiyligiga nisbatan matn kam.")

    has_expected_signal = _has_expected_uzbek_ru_signal(text, words)
    if word_count > 15 and not has_expected_signal:
        suspicious = True
        confidence -= 0.3
        reasons.append(
            "Matnda kutilgan o'zbek/rus tili belgilari (krill, digraflar, odatiy so'zlar, "
            "sohaga oid atamalar) umuman topilmadi."
        )

    if word_count >= 8 and not has_expected_signal:
        english_hits = sum(1 for w in words if w.lower() in _ENGLISH_STOPWORDS)
        if english_hits / word_count > 0.3:
            suspicious = True
            confidence -= 0.3
            reasons.append("Matn asosan inglizcha funksiya-so'zlardan iborat -- kutilmagan til bo'lishi mumkin.")

    confidence = max(0.0, min(1.0, confidence))
    if suspicious:
        return {"status": "suspicious", "confidence": round(confidence, 2), "reasons": reasons}
    return {"status": "good", "confidence": 1.0, "reasons": []}


# Orqaga moslik uchun eski nom (V4'da shu nom bilan chaqirilgan) --
# yangi kod `evaluate_transcription_quality`ni ishlatishi kerak.
assess_quality = evaluate_transcription_quality


def is_acceptable(status: str) -> bool:
    """Faqat "good" sifat tahlil bosqichiga yuborish uchun YETARLI deb
    hisoblanadi (foydalanuvchi ANIQ so'ragan: "suspicious" yoki "failed"
    holatda AVVAL qayta urinish kerak, tahlil qilinmasin)."""
    return status == "good"
