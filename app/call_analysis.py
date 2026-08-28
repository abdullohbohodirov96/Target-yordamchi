"""call_analysis.py — qo'ng'iroq yozuvlarini (Moi Zvonki `recording_url`) AI
yordamida TAHLIL qiladi: to'liq transkripsiya (Manager/Mijoz ajratilgan
holda), mijoz so'rovi, menejer xatolari, ijobiy tomonlar, savdo natijasi,
va 1-10 baho (rang bilan: qizil/sariq/yashil).

=====================================================================
TARIX (arxitektura qanday o'zgarib kelgani -- keyingi safar kimdir shu
kodni o'qisa, "nega bunday" degan savolga javob bo'lsin deb saqlanmoqda)
=====================================================================
V1: audio to'g'ridan-to'g'ri OpenAI audio-preview chat modellariga
    yuborilardi -- BARCHA shunday model nomlari bu akkaunt uchun 404
    qaytardi (org verification talab qilinadi shekilli).
V2: ikki bosqichli: whisper-1 orqali transkripsiya, keyin oddiy matn
    modeli orqali tahlil. Ishladi, lekin sifat past edi (gibberish,
    "language=uz" 400 xatosi, atamalar noto'g'ri tanilishi va h.k.)
V3: ixtiyoriy gpt-4o-transcribe-diarize qatlami qo'shildi (haqiqiy
    diarizatsiya, taxminsiz).
V4 (2026-08, HOZIRGI, foydalanuvchining to'liq audit-va-qayta-qurish
    so'rovi asosida): quyidagilar qo'shildi/o'zgardi:
  - Transkripsiya modeli ustuvorligi: `gpt-4o-transcribe` ENDI BIRINCHI
    nomzod (avval whisper-1 birinchi edi) -- sifatliroq deb hisoblanadi,
    lekin whisper-1 baribir oxirgi fallback sifatida qoladi (eng keng
    mavjud).
  - Markazlashtirilgan domen lug'ati (`call_glossary.py`) -- endi
    "prompt" hint SHU MODULDAN olinadi, alohida hardcoded matn emas.
  - Audio metadata (kodek, sample rate, davomiylik, kanallar soni)
    ffprobe orqali aniqlanadi -- LEKIN ffprobe/ffmpeg Render'ning
    joriy "python" runtime'ida (Docker emas) KAFOLATLANGAN mavjud EMAS.
    Shuning uchun BUTUNLAY `shutil.which()` bilan himoyalangan: mavjud
    bo'lmasa, bu qadam JIM o'tkazib yuboriladi (butun tahlil to'xtamaydi).
  - Stereo (2 kanalli) yozuvlar uchun -- agar ffmpeg mavjud bo'lsa VA
    yozuv 2 kanalli bo'lsa -- kanallar ALOHIDA-ALOHIDA transkripsiya
    qilinadi (kanal 0 = odatda operator/Manager liniyasi, kanal 1 =
    mijoz liniyasi -- bu YOZUV TIZIMINING konvensiyasi, `CALL_OPERATOR_CHANNEL`
    orqali sozlanadi) va vaqt tamg'asi bo'yicha xronologik birlashtiriladi.
    Bu DIARIZATSIYADAN KO'RA ISHONCHLIROQ (taxmin emas, jismoniy kanal).
    Agar biror qadam ishlamasa -- JIM oddiy (mono) yo'lga qaytiladi.
  - Xom (`ai_raw_transcription`) va normallashtirilgan (`ai_transcription`)
    transkripsiya BAZADA ALOHIDA saqlanadi -- xom versiya HECH QACHON AI
    tomonidan "tozalanmaydi" (aynan ASR natijasi).
  - Tahlil bosqichi endi OpenAI Responses API (`/v1/responses`) orqali,
    QAT'IY JSON Schema (Structured Outputs) bilan ishlaydi -- erkin
    matnni parslash o'rniga. Model: `OPENAI_ANALYSIS_MODEL` (standart --
    `gpt-4o-mini`, ANIQ shu akkauntda ishlab turgan model). ESLATMA:
    foydalanuvchi taklif qilgan "gpt-5.6-terra" HAQIQIY OpenAI modeli
    EMAS (bunday model mavjud emas) -- shuning uchun ataylab ishlatilmadi,
    o'rniga mavjud/tasdiqlangan model ishlatildi (bu holat foydalanuvchiga
    yakuniy hisobotda ALOHIDA aytiladi).
  - Tahlil natijasi kengaytirildi: mijoz so'rovi (mahsulot/brend/miqdor/
    o'lcham/parametrlar), menejer xatolari, ijobiy tomonlar, savdo
    natijasi (sotildi/yo'qotildi/kutilmoqda/noma'lum), qayta bog'lanish
    kerakmi, tavsiya etilgan javob.
  - Status/rang HAMON server kodida score'dan DETERMINISTIK hisoblanadi
    (modelga ishonilmaydi) -- bu V2'dan buyon shunday edi, o'zgarmadi.
  - Jarayon bosqichlari (`ai_stage`): uploaded/processing_audio/
    transcribing/analyzing/completed/failed -- agar transkripsiya
    muvaffaqiyatli, lekin TAHLIL xato bergan bo'lsa, keyingi urinishda
    AUDIO QAYTA YUKLAB OLINMAYDI/QAYTA TRANSKRIPSIYA QILINMAYDI -- mavjud
    xom transkripsiyadan to'g'ridan-to'g'ri tahlil qilinadi.
  - Vaqtinchalik (tarmoq/5xx) xatolarda avtomatik qayta urinish
    qo'shildi -- 4xx (validatsiya/autentifikatsiya) xatolarida ASLO
    qayta urinilmaydi.
  - Batafsil log: qaysi model ishlatilgani, audio davomiyligi/formati/
    kanallar soni, transkripsiya/tahlil davomiyligi (soniyada), xatolar --
    API KALITI HECH QACHON log qilinmaydi.
V5 (2026-08, HOZIRGI, foydalanuvchi HAQIQIY production bug'ini ko'rsatgach
    -- o'zbekcha qo'ng'iroqlar Turkcha/Arabcha/Portugalcha/Inglizcha
    ma'nosiz matnga transkripsiya qilinib, keyin tahlil modeli shu
    yaramas matndan "to'qib chiqarilgan" umumiy xulosa/ball berardi --
    asosida): quyidagilar qo'shildi/o'zgardi:
  - HAQIQIY SIFAT DARVOZASI (`call_quality.py`, `transcribe_with_quality_gate()`):
    endi HECH QANDAY transkripsiya sinovdan o'tmasdan (script/harf/
    takrorlanish/tezlik/kutilgan-signal tekshiruvlari) tahlilga
    YUBORILMAYDI. Sifatsiz bo'lsa -- 3 bosqichli qayta urinish zanjiri
    (`_mono_transcribe_ladder`: oddiy -> kuchli kontekst -> zaxira model),
    barchasi muvaffaqiyatsiz bo'lsa `ai_stage = "transcription_failed"`,
    TAHLIL UMUMAN CHAQIRILMAYDI, `ai_score = NULL` -- bu aynan ko'rsatilgan
    bug'ning TO'G'RIDAN-TO'G'RI tuzatilishi.
  - Diarizatsiyada "birinchi gapirgan = Manager" TAXMINI BUTUNLAY OLIB
    TASHLANDI (chiquvchi/kiruvchi qo'ng'iroq yoki kesilgan yozuvda bu
    ko'pincha NOTO'G'RI edi) -- endi FAQAT operatorga XOS iboralar
    (`_OPERATOR_EVIDENCE_PHRASES`) orqali DALIL bilan aniqlanadi
    (`_guess_operator_speaker`), aks holda "Speaker 1"/"Speaker 2" saqlanadi.
  - Baholash rubrikasi (`RUBRIC`) "applicable"/"earned" formatiga
    o'tkazildi: model har mezon uchun bu suhbatga UMUMAN aloqadormi
    (applicable) va necha ball ERISHILGANI (earned)ni ko'rsatadi; YAKUNIY
    `score` server kodida `earned_applicable / possible_applicable * 10`
    formulasi bilan hisoblanadi (aloqasiz mezonlar denominatordan
    CHIQARILADI). Har bir mezon, `operatorMistakes`, `positivePoints`
    endi `evidenceTurnIds` orqali ANIQ `normalizedTranscript` bo'lagiga
    bog'langan -- hallyusinatsiya qilingan indekslar JIM chiqarib
    tashlanadi.
  - `customerRequest` sxemasi kengaytirildi: `measurement` (masalan
    "8 santimetrlisidan") va `parameters` (ro'yxat, masalan "120 plotnost")
    maydonlari qo'shildi -- IKKALASI HAM transkriptda AYNAN kelgan
    ko'rinishda saqlanadi, "to'g'irlanmaydi"/"adabiylashtirilmaydi".
  - `OPENAI_ANALYSIS_MODEL` ENDI `OPENAI_MODEL`ga MUTLAQO BOG'LIQ EMAS
    (avval fallback sifatida unga qarardi) -- boshqa xususiyatlar
    `OPENAI_MODEL`ni o'zgartirsa ham, qo'ng'iroq-tahlil YASHIRINCHA
    ta'sirlanmaydi.
  - `transcriptionConfidence`/`ai_transcription_quality_reasons` va
    `ai_operator_channel` bazaga qo'shildi -- sifat darvozasining
    ISHONCH darajasi va sababi endi UI/debug'da alohida ko'rinadi.
  - `_download_audio()` endi NOMLANGAN xato kodlari bilan ishlaydi
    (`AudioDownloadError.code`: `audio_invalid` / `audio_expired` /
    `audio_download_failed`) -- Content-Length solishtirish, JSON/HTML
    javoblarni aniqlash, 401/403/404/410'ni "muddati o'tgan" deb
    belgilash qo'shildi.
  - Render deploy `runtime: docker`ga o'tkazildi (repo ildizidagi
    `Dockerfile` orqali) -- shu bilan ffmpeg/ffprobe production'da
    HAQIQATDA mavjud bo'ladi (avval "python" runtime'da apt-get yo'q
    edi, stereo-kanal ajratish/audio metadata JIM o'chirilgan holda
    ishlardi). `/api/health` endi `ffmpeg_available`/`ffprobe_available`ni
    ko'rsatadi, `log_model_config()` ishga tushganda qaysi model
    ishlatilayotganini logga yozadi.
V6 (2026-08, HOZIRGI, V5 production'ga chiqqandan KEYIN foydalanuvchi
    HALI HAM ikkita jiddiy muammoni ko'rsatgach: (1) ba'zan BUTUN
    qo'ng'iroq "Mijoz" deb belgilanardi, (2) matnda ma'nosiz "to'qilgan"
    so'zlar chiqardi -- masalan "duxovoy karina labarakam"): ILDIZ
    SABABLAR IKKITA BUTUNLAY BOSHQA joyda topildi va IKKALASI HAM
    tuzatildi:
  - ILDIZ SABAB #1 (nonsense so'zlar): `gpt-4o-transcribe-diarize`ning
    O'Z MATNI (garchi gapiruvchi/vaqt ajratishi to'g'ri bo'lsa ham) ba'zan
    hallyusinatsiya qilardi. TUZATISH: diarizatsiya ENDI FAQAT gapiruvchi
    ID + boshlanish/tugash vaqti uchun ishlatiladi -- uning `text` maydoni
    HECH QACHON o'qilmaydi/ishlatilmaydi. HAQIQIY matn: (a) ketma-ket bir
    xil gapiruvchi bo'laklari ~5-15s guruhlarga BIRLASHTIRILADI
    (`_group_diarization_segments`, qisqa 0.5-2s bo'lakni ALOHIDA
    transkripsiya qilish kontekstni buzib hallyusinatsiyani oshiradi),
    (b) har bir guruh chegarasiga ~0.35s padding qo'shiladi (audio
    chegarasidan tashqariga chiqmaydi, `_compute_padded_bounds`),
    (c) ffmpeg bilan ORIGINAL audiodan shu oraliq kesib olinadi
    (`_cut_audio_segment_bytes`), (d) har bir kesilgan bo'lak ALOHIDA,
    MUSTAQIL ravishda `_mono_transcribe_ladder` orqali (max 2 qayta
    urinish bilan) qayta transkripsiya qilinadi (`_transcribe_segment_group`) --
    bitta bo'lakning muvaffaqiyatsizligi boshqa bo'lakka TA'SIR qilmaydi.
    Agar HECH BIR urinish "good" bermasa -- TO'QILGAN matn SAQLANMAYDI,
    o'rniga `[noaniq]` markeri qo'yiladi (xom/rad etilgan matn FAQAT
    debug maydonida ko'rinadi). Butun qo'ng'iroq bo'yicha `[noaniq]`
    ulushi 40%dan oshsa -- sifat "failed" (tahlil UMUMAN chaqirilmaydi),
    15-40% oralig'ida "suspicious" deb belgilanadi. Bularning barchasi
    `_reassemble_call_from_diarization()`da orkestratsiya qilinadi.
  - ILDIZ SABAB #2 (butun qo'ng'iroq "Mijoz"): bu V5'da "tuzatilgan"
    deb o'ylangan diarizatsiya-qatlamidagi kod EMAS edi -- aksincha,
    TAHLIL MODELINING O'ZI, `normalizedTranscript` maydoni orqali,
    transkripsiya pipeline'i ALLAQACHON (to'g'ri yoki "unknown" sifatida)
    belgilagan gapiruvchi yorliqlarini MUSTAQIL RAVISHDA QAYTA TOPARDI --
    va ko'pincha noto'g'ri, hammasini "mijoz" deb qayta yozib yuborardi.
    TUZATISH: `_ANALYSIS_JSON_SCHEMA`dan `normalizedTranscript` BUTUNLAY
    OLIB TASHLANDI. Tahlil modeliga ENDI transkripsiya PIPELINE'ining
    o'zi ishlab chiqargan, `[N] Yorliq: matn` bilan INDEKSLANGAN
    (`_render_indexed_transcript`), O'ZGARMAS matn beriladi -- model
    FAQAT mazmunni tahlil qiladi, `evidenceTurnIds` uchun shu tashqi
    indekslardan foydalanadi, lekin yorliqlarni QAYTA CHIQARMAYDI/
    O'ZGARTIRMAYDI (buyruq `_ANALYSIS_SYSTEM_PROMPT` qoida-5da ANIQ
    taqiqlangan). `analyze_call_record()`da `call.ai_transcription`
    ENDI to'g'ridan-to'g'ri pipeline matnidan (`transcript_text`)
    o'rnatiladi -- modelning "normalizatsiya qilingan" versiyasi ENDI
    UMUMAN mavjud emas.
  - `_guess_operator_speaker()`ga (V5'dan) `call_direction`/`first_speaker`
    parametrlari qo'shildi -- LEKIN faqat SO'Z-DALILI ORQALI
    ALLAQACHON aniq (boshqalardan qat'iy ko'p) YETAKCHI bo'lgan nomzod
    borida, uning ustunligini SAL kuchaytiradi; hech qachon 0-0 yoki N-N
    TENGLIKNI (yoki dalilsiz holatni) yo'nalish/tartib orqali "hal
    qilib" YUBORMAYDI (bu aynan diarizatsiya-darajasida "birinchi
    gapiruvchi=Manager" xatosining boshqa shakli bo'lardi).
  - `_TURN_RE` (matn-parsing regex) `Speaker\\s*1`/`Speaker\\s*2`dan
    `Speaker\\s*\\d+`ga UMUMLASHTIRILDI -- diarizatsiya 3+ xom gapiruvchi
    ID chiqarsa ham to'g'ri ishlaydi.
  - Yangi `ai_segment_debug_json` ustuni (db.py) -- xom diarizatsiya
    segmentlari, guruhlangan chegaralar, har bir guruh uchun urinishlar/
    qayta-urinishlar/tanlangan matn, va gapiruvchi-xaritalash ishonchi
    (admin debug ko'rinishida, `individual_check_ai_debug.html`).
  - Stereo (jismoniy 2-kanalli) yo'l ENG YUQORI USTUVORLIKDA qoladi --
    haqiqiy stereo bo'lsa, diarizatsiya/segment-qayta-transkripsiya
    UMUMAN chaqirilmaydi (regressiya testi bilan tekshirilgan).
V6.1 (2026-08, HOZIRGI, V6 deploy qilingandan keyin -- foydalanuvchi
    ishlab chiqarishda `analyze_call_record()`ni ANIQ ko'rsatgan uchta
    qo'shimcha so'rovi asosida): quyidagilar qo'shildi:
  - `analyze_call_record()` ENDI ikkita audio manbasini qo'llab-quvvatlaydi:
    Moi Zvonki'dan kelgan `recording_url` (avvalgidek, `_download_audio()`
    orqali) VA admin panelda QO'LDA yuklangan `uploaded_audio_data`/
    `uploaded_audio_format` (bazada saqlangan baytlar, tarmoqdan yuklab
    olish SHART emas) -- ikkalasi ham BIR XIL pipeline (sifat darvozasi,
    tahlil) orqali ishlaydi. Yangi admin marshruti
    (`/individual-tekshirish/audio-yuklash`) orqali bitta audio faylni
    qo'lda yuklab, YANGI yozuv sifatida DARHOL (fon oqimida) tahlil qilish
    mumkin -- masalan muammoli haqiqiy namunani skriptsiz sinash uchun.
  - `scheduler.py`dagi `job_call_sync()` ENDI yangi qo'ng'iroq(lar)
    sinxronlangandan KEYIN DARHOL (shu job ichida, alohida
    `job_call_analysis` cron'ini -- soatning :10/:30/:50 daqiqalarini --
    kutmasdan) tahlil navbatini ishga tushiradi -- yangi audio bilan
    tahlil orasidagi kechikish (avval 20 daqiqagacha) YO'QOLADI.
  - "AI analiz" ro'yxatida oxirgi 1 soat ichida tahlil qilingan yozuvlar
    endi yashil "Yangi" nishonchasi bilan ajratib ko'rsatiladi.

MUHIM CHEKLOV (foydalanuvchi talabi bilan): faqat OpenAI ishlatiladi.
Boshqa transkripsiya provayderi (AssemblyAI, Deepgram, Google Speech,
Azure Speech va h.k.) QO'SHILMAGAN va qo'shilmasligi kerak, agar
foydalanuvchi ANIQ so'ramasa.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import datetime as dt

import requests

import call_glossary
import call_quality

logger = logging.getLogger("call_analysis")

# ---------------------------------------------------------------------------
# Model konfiguratsiyasi
# ---------------------------------------------------------------------------

# 2026-08 V4: `gpt-4o-transcribe` ENDI BIRINCHI nomzod (foydalanuvchi
# so'rovi bilan sifat uchun ustuvor qilindi). `whisper-1` OXIRIDA --
# eng keng mavjud fallback sifatida saqlanadi. `OPENAI_TRANSCRIBE_MODEL`
# environment variable orqali bu ro'yxatning BOSHIGA qo'shimcha nomzod
# qo'shish mumkin (masalan test uchun).
_TRANSCRIBE_MODEL_CANDIDATES = ["gpt-4o-transcribe", "gpt-4o-mini-transcribe", "whisper-1"]

# Stereo-kanal ajratishda har bir kanalni ALOHIDA transkripsiya qilish
# uchun vaqt tamg'asi (segments) KERAK -- bunga faqat `whisper-1`ning
# `verbose_json` formati javob beradi (`gpt-4o-transcribe`/`-mini`
# `verbose_json`/timestamp granularity'ni QO'LLAB-QUVVATLAMAYDI, OpenAI
# hujjatlariga ko'ra) -- shuning uchun bu FAQAT shu tor vazifa uchun
# whisper-1 ATAYLAB ishlatiladi, umumiy ustuvorlikka zid emas.
_CHANNEL_TIMESTAMP_MODEL = "whisper-1"

# 2026-08 V5, foydalanuvchi ANIQ so'ragan: bu ENDI `OPENAI_MODEL`ga
# (Telegram/assistant xususiyatlari ishlatadigan, `orchestrator.py`)
# ASLO BOG'LIQ EMAS -- faqat `OPENAI_ANALYSIS_MODEL` o'qiladi. Bu orqali
# "OPENAI_MODEL boshqa joyda o'zgartirilsa, qo'ng'iroq-tahlil modeli
# yashirincha o'zgarib qolmasin" degan aniq talab bajarildi. ESLATMA:
# foydalanuvchi so'ragan "gpt-5.6-terra" HAQIQIY OpenAI modeli EMAS --
# standart qiymat sifatida tasdiqlangan, ishlaydigan model qo'yilgan;
# `render.yaml`dagi shu nomdagi environment variable orqali istalgan
# vaqtda (kod o'zgartirmasdan) yangilanishi mumkin.
OPENAI_ANALYSIS_MODEL = os.environ.get("OPENAI_ANALYSIS_MODEL", "gpt-4o-mini")

# Qaysi jismoniy audio kanal operator (Manager) liniyasi deb hisoblanadi.
# Ko'pchilik IP-telefoniya/qo'ng'iroq yozish tizimlarida kanal 0 (chap) --
# operator, kanal 1 (o'ng) -- mijoz, lekin bu YOZUV TIZIMIGA BOG'LIQ
# konvensiya -- boshqacha bo'lsa shu environment variable orqali "1"ga
# o'zgartirilsin.
CHANNEL_OPERATOR_INDEX = int(os.environ.get("CALL_OPERATOR_CHANNEL", "0") or "0")

# Vaqtinchalik (tarmoq/5xx) xatolarda necha marta qayta urinish.
_MAX_RETRIES = 2
_TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}

# ---------------------------------------------------------------------------
# 2026-08 V6, foydalanuvchi HAQIQIY production bug'ini (2) ko'rsatgach --
# diarizatsiya matnini "master transkript" sifatida ISHLATISH xato ekani
# aniqlandi: `gpt-4o-transcribe-diarize` gapiruvchi/vaqt chegaralarini
# YAXSHI aniqlaydi, lekin uning O'Z MATNI ko'pincha ma'nosiz so'zlar
# "to'qib chiqaradi" ("duxovoy karina labarakam" kabi). Shuning uchun
# ENDI diarizatsiya FAQAT gapiruvchi ID + boshlanish/tugash vaqtini
# berish uchun ishlatiladi; HAQIQIY matn har bir gapiruvchi bo'lagini
# ffmpeg bilan ORIGINAL AUDIODAN kesib olib, ALOHIDA `gpt-4o-transcribe`ga
# yuborish orqali OLINADI (pastga qarang, "SEGMENT DARAJASIDA QAYTA
# TRANSKRIPSIYA").
# ---------------------------------------------------------------------------

# Ketma-ket bir xil gapiruvchi bo'laklarini BITTA guruhga birlashtirish
# uchun maksimal natija davomiyligi (soniya) -- juda uzun guruh audio
# konteksti/API cheklovlarini buzmasin uchun yumshoq yuqori chegara.
_SEGMENT_GROUP_TARGET_MAX_SEC = 15.0
# Bir xil gapiruvchining ikkita ketma-ket bo'lagi orasidagi TANAFFUS shu
# qiymatdan katta bo'lsa -- ular ENDI bitta "navbat" (turn) deb
# hisoblanmaydi, guruh ALOHIDA boshlanadi (tabiiy navbat chegarasi saqlanadi).
_SEGMENT_GROUP_MAX_GAP_SEC = 1.5
# Har bir kesilgan segmentga ffmpeg bilan qo'shiladigan yostiqcha (birinchi/
# oxirgi bo'g'in "kesilib qolishi"ning oldini olish uchun) -- audio
# chegaralaridan tashqariga chiqmaydi.
_SEGMENT_PAD_BEFORE_SEC = 0.35
_SEGMENT_PAD_AFTER_SEC = 0.35
# Bitta segment uchun MAKSIMAL qayta urinishlar (foydalanuvchi ANIQ
# so'ragan: "Maximum 2 retries per segment" -- ya'ni birinchi urinish +
# 2 qayta urinish = jami 3, `_mono_transcribe_ladder` bilan BIR XIL
# zanjir qayta ishlatiladi, chunki mantiq aynan mos keladi).
_SEGMENT_MAX_RETRIES = 2
# Aniqlanmagan/ishonchsiz deb topilgan segment o'rniga qo'yiladigan
# belgi -- foydalanuvchi ANIQ so'ragan: "TO'QIB CHIQARISHDAN ko'ra shu
# afzal".
_NOANIQ_MARKER = "[noaniq]"
# Yakuniy transkriptning necha ulushi `_NOANIQ_MARKER` bo'lsa -- butun
# qo'ng'iroq sifati "failed" (tahlil UMUMAN chaqirilmaydi) deb
# belgilanadi (foydalanuvchi ANIQ so'ragan: "ko'p qismi noaniq bo'lsa,
# ishonchli savdo-tahlili YASAMA").
_SEGMENT_NOANIQ_BLOCK_RATIO = 0.40
# Bundan yuqori (lekin bloklash chegarasidan past) ulush -- "suspicious"ga
# tushiriladi (hatto umumiy matn sifat tekshiruvi "good" desa ham).
_SEGMENT_NOANIQ_WARN_RATIO = 0.15

def is_configured() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


# ---------------------------------------------------------------------------
# Tarmoq qatlami -- qayta urinish (retry) bilan
# ---------------------------------------------------------------------------

def _openai_request(method: str, url: str, *, headers: dict, json_body=None, data=None, files=None, timeout: int = 90):
    """Barcha OpenAI so'rovlari SHU orqali yuboriladi -- vaqtinchalik
    (tarmoq uzilishi, timeout, HTTP 429/5xx) xatolarda avtomatik qayta
    urinadi (`_MAX_RETRIES` marta, ortib boruvchi kutish bilan).
    VALIDATSIYA/AUTENTIFIKATSIYA xatolari (400/401/403/404/422) ASLO
    qayta urinilmaydi -- ular takrorlansa ham natija o'zgarmaydi, faqat
    vaqt behuda ketadi."""
    last_exc = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = requests.request(
                method, url, headers=headers, json=json_body, data=data, files=files, timeout=timeout,
            )
        except requests.RequestException as e:
            last_exc = e
            if attempt < _MAX_RETRIES:
                wait = 1.5 * (attempt + 1)
                logger.warning(
                    "OpenAI so'rovi tarmoq xatosi (%s, urinish %s/%s): %s -- %.1fs kutib qayta uriniladi",
                    url, attempt + 1, _MAX_RETRIES + 1, e, wait,
                )
                time.sleep(wait)
                continue
            raise
        if resp.status_code in _TRANSIENT_STATUS_CODES and attempt < _MAX_RETRIES:
            wait = 1.5 * (attempt + 1)
            logger.warning(
                "OpenAI vaqtinchalik xatosi (HTTP %s, %s, urinish %s/%s) -- %.1fs kutib qayta uriniladi",
                resp.status_code, url, attempt + 1, _MAX_RETRIES + 1, wait,
            )
            time.sleep(wait)
            continue
        return resp
    raise last_exc


def _extract_openai_error(resp) -> str:
    try:
        body = resp.json()
        msg = (body.get("error") or {}).get("message")
        if msg:
            return msg
    except Exception:
        pass
    return (resp.text or "")[:300] or f"HTTP {resp.status_code}"


# ---------------------------------------------------------------------------
# Audio yuklab olish va formatni aniqlash
# ---------------------------------------------------------------------------

def _guess_audio_format(url: str, content_type: "str | None") -> str:
    ct = (content_type or "").lower()
    if "wav" in ct or url.lower().endswith(".wav"):
        return "wav"
    if "ogg" in ct or url.lower().endswith(".ogg"):
        return "ogg"
    if "m4a" in ct or url.lower().endswith(".m4a"):
        return "m4a"
    return "mp3"


def _detect_magic_format(data: bytes) -> "str | None":
    head = data[:16]
    if head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return "mp3"
    if head[:4] == b"RIFF":
        return "wav"
    if head[:4] == b"OggS":
        return "ogg"
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return "m4a"
    return None


def _sniff_audio_format(data: bytes, content_type: "str | None", url: str) -> str:
    return _detect_magic_format(data) or _guess_audio_format(url, content_type)


class AudioDownloadError(RuntimeError):
    """`_download_audio()` xatosi -- 2026-08 V5, foydalanuvchi ANIQ so'ragan
    NOMLANGAN xato kodlari bilan, shunda chaqiruvchi/admin-debug/UI xato
    TURINI (nima uchun) aniq ajrata oladi, shunchaki umumiy matn emas:
      - "audio_download_failed": tarmoq/HTTP xatosi (server javob bermadi,
        5xx, ulanish uzildi va h.k.) -- vaqtinchalik bo'lishi mumkin.
      - "audio_expired": havola ANIQ ishlamay qolgan ko'rinadi (403/404/410,
        yoki javob matnida "expired"/"muddati o'tgan" kabi belgilar bor).
      - "audio_invalid": javob keldi, lekin bu haqiqiy audio EMAS (juda
        kichik, matn/HTML/JSON, yoki tanish audio formatiga mos kelmaydi).
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_AUDIO_EXPIRED_MARKERS = ("expired", "muddati", "eskirgan", "not found", "404", "link is no longer valid")


def _download_audio(url: str) -> "tuple[bytes, str]":
    try:
        resp = requests.get(url, timeout=60, allow_redirects=True)
    except requests.RequestException as e:
        raise AudioDownloadError("audio_download_failed", f"Yozuvni yuklab olishda tarmoq xatosi: {e}") from e

    if resp.status_code in (401, 403, 404, 410):
        raise AudioDownloadError(
            "audio_expired",
            f"Yozuv havolasi (recording_url) ishlamayapti (HTTP {resp.status_code}) -- "
            "muddati o'tgan yoki o'chirilgan bo'lishi mumkin.",
        )
    try:
        resp.raise_for_status()
    except requests.RequestException as e:
        raise AudioDownloadError("audio_download_failed", f"Yozuvni yuklab olishda HTTP xatosi: {e}") from e

    data = resp.content
    content_type = resp.headers.get("Content-Type", "")

    # Content-Length bilan solishtirish -- to'liq yuklanmagan/kesilgan
    # javobni ANIQLASH uchun (aniq xato tashlamaydi, faqat LOGGA yozadi,
    # chunki ba'zi serverlar bu sarlavhani noto'g'ri/chunked yuborishi
    # mumkin -- foydalanuvchi ANIQ so'ragan: qattiq rad emas, kuzatuv).
    content_length_header = resp.headers.get("Content-Length")
    if content_length_header:
        try:
            expected_len = int(content_length_header)
            if expected_len > 0 and abs(len(data) - expected_len) > max(64, int(expected_len * 0.02)):
                logger.warning(
                    "Qo'ng'iroq yozuvi hajmi Content-Length sarlavhasiga mos kelmadi "
                    "(kutilgan=%s bayt, olingan=%s bayt, url=%s) -- baribir davom etiladi.",
                    expected_len, len(data), url,
                )
        except ValueError:
            pass

    ct_lower = content_type.lower()
    if "json" in ct_lower or "html" in ct_lower or ct_lower.startswith("text/"):
        preview = data[:200].decode("utf-8", errors="replace")
        if any(marker in preview.lower() for marker in _AUDIO_EXPIRED_MARKERS):
            raise AudioDownloadError(
                "audio_expired",
                f"Yozuv havolasi muddati o'tgan ko'rinadi (Content-Type: {content_type}): {preview!r}",
            )
        raise AudioDownloadError(
            "audio_invalid",
            f"Yozuv havolasidan (recording_url) audio o'rniga matn/HTML/JSON qaytdi "
            f"(Content-Type: {content_type or 'nomaʼlum'}): {preview!r}. "
            "Havola noto'g'ri yoki yozuv hali tayyor bo'lmagan bo'lishi mumkin.",
        )

    if len(data) < 500:
        raise AudioDownloadError(
            "audio_invalid",
            f"Yuklab olingan yozuv fayli juda kichik ({len(data)} bayt, Content-Type: "
            f"{content_type or 'nomaʼlum'}) -- bu haqiqiy audio emasligi mumkin "
            "(havola muddati o'tgan yoki yozuv hali tayyor bo'lmagan bo'lishi mumkin)."
        )
    if _detect_magic_format(data) is None:
        head = data[:60]
        looks_textual = bool(head) and all(32 <= b < 127 or b in (9, 10, 13) for b in head[:20])
        if looks_textual:
            preview = head.decode("utf-8", errors="replace")
            raise AudioDownloadError(
                "audio_invalid",
                f"Yozuv havolasidan (recording_url) audio o'rniga matn/HTML qaytdi "
                f"(Content-Type: {content_type or 'nomaʼlum'}) -- boshlanishi: {preview!r}. "
                "Havola muddati o'tgan yoki noto'g'ri bo'lishi mumkin.",
            )
        logger.warning(
            "Yozuv fayli tanish magic-byte'larga mos kelmadi (Content-Type: %s, boshi: %r) -- "
            "baribir yuborilmoqda.", content_type, data[:16],
        )

    audio_format = _sniff_audio_format(data, content_type, url)
    return data, audio_format


# ---------------------------------------------------------------------------
# Audio metadata (ffprobe) -- IXTIYORIY, faqat mavjud bo'lsa ishlaydi
# ---------------------------------------------------------------------------

def _ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


# 2026-08 V5, foydalanuvchi ANIQ so'ragan: `/api/health`da ffmpeg/ffprobe
# HAQIQATDA mavjudligini ko'rsatish kerak (Docker runtime'ga o'tish shu
# tekshiruvni MA'NOLI qiladi) -- shuning uchun ochiq (public) nom bilan
# ham eksport qilinadi.
def ffmpeg_available() -> bool:
    return _ffmpeg_available()


def ffprobe_available() -> bool:
    return _ffprobe_available()


def log_model_config() -> None:
    """Ilova ishga tushganda LOGGA (hech qachon API kalitlarni EMAS) qaysi
    transkripsiya/tahlil modellari HAQIQATDA ishlatilishini yozadi --
    foydalanuvchi ANIQ so'ragan: production loglarida yangi modellar
    ishlatilayotgani ko'rinishi kerak."""
    primary = os.environ.get("OPENAI_TRANSCRIBE_MODEL") or _TRANSCRIBE_MODEL_CANDIDATES[0]
    fallback = os.environ.get("OPENAI_TRANSCRIBE_FALLBACK_MODEL", "whisper-1")
    logger.info("Call transcription model: primary=%s, fallback=%s, channel-timestamp=%s", primary, fallback, _CHANNEL_TIMESTAMP_MODEL)
    logger.info("Call analysis model: %s", OPENAI_ANALYSIS_MODEL)
    logger.info(
        "Call audio tooling: ffmpeg_available=%s, ffprobe_available=%s, operator_channel_index=%s",
        ffmpeg_available(), ffprobe_available(), CHANNEL_OPERATOR_INDEX,
    )


def probe_audio_metadata(audio_bytes: bytes, audio_format: str) -> "dict | None":
    """ffprobe orqali audio metadata (kodek, sample rate, davomiylik,
    kanallar soni, bitrate) ni o'qiydi. Render'ning joriy "python"
    runtime'ida ffmpeg/ffprobe O'RNATILMAGAN bo'lishi mumkin -- shuning
    uchun `shutil.which()` bilan himoyalangan, mavjud bo'lmasa `None`
    qaytaradi (xato tashlamaydi, butun tahlilni to'xtatmaydi). Original
    fayl HECH QACHON o'zgartirilmaydi -- faqat vaqtinchalik nusxa
    o'qiladi va so'ng o'chiriladi."""
    if not _ffprobe_available():
        logger.info("ffprobe topilmadi -- audio metadata aniqlash o'tkazib yuborildi.")
        return None
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=f".{audio_format}", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_streams", "-show_format",
                "-of", "json", tmp_path,
            ],
            capture_output=True, text=True, timeout=20,
        )
        if proc.returncode != 0:
            logger.warning("ffprobe xato bilan tugadi (%s): %s", proc.returncode, (proc.stderr or "")[:300])
            return None
        info = json.loads(proc.stdout or "{}")
        streams = info.get("streams") or []
        audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
        fmt = info.get("format") or {}
        if not audio_streams:
            return {"channels": None, "codec": None, "duration_sec": None, "sample_rate": None}
        a = audio_streams[0]
        duration = fmt.get("duration") or a.get("duration")
        return {
            "channels": int(a["channels"]) if a.get("channels") is not None else None,
            "codec": a.get("codec_name"),
            "duration_sec": float(duration) if duration is not None else None,
            "sample_rate": int(a["sample_rate"]) if a.get("sample_rate") is not None else None,
        }
    except Exception as e:
        logger.warning("Audio metadata (ffprobe) aniqlashda xato: %s", e)
        return None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Stereo (2-kanalli) yozuvlarni ALOHIDA kanal bo'yicha qayta ishlash
# ---------------------------------------------------------------------------

def split_stereo_channels(audio_bytes: bytes, audio_format: str) -> "tuple[bytes, bytes] | None":
    """2 kanalli audio faylni IKKI alohida mono WAV faylga ajratadi
    (ffmpeg orqali). Muvaffaqiyatsiz bo'lsa (ffmpeg yo'q, fayl mono,
    xato) -- `None` qaytaradi, chaqiruvchi oddiy (mono/diarizatsiya)
    yo'lga qaytadi. Original bayt-massiv HECH QACHON o'zgartirilmaydi --
    faqat vaqtinchalik nusxalar bilan ishlanadi."""
    if not _ffmpeg_available():
        return None
    src_path = left_path = right_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=f".{audio_format}", delete=False) as src:
            src.write(audio_bytes)
            src_path = src.name
        left_fd, left_path = tempfile.mkstemp(suffix=".wav")
        right_fd, right_path = tempfile.mkstemp(suffix=".wav")
        os.close(left_fd)
        os.close(right_fd)
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-i", src_path,
                "-filter_complex", "[0:a]channelsplit=channel_layout=stereo[left][right]",
                "-map", "[left]", left_path,
                "-map", "[right]", right_path,
            ],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            logger.warning("ffmpeg kanal ajratishda xato: %s", (proc.stderr or "")[:400])
            return None
        with open(left_path, "rb") as f:
            left_bytes = f.read()
        with open(right_path, "rb") as f:
            right_bytes = f.read()
        if not left_bytes or not right_bytes:
            return None
        return left_bytes, right_bytes
    except Exception as e:
        logger.warning("Stereo kanal ajratishda kutilmagan xato: %s", e)
        return None
    finally:
        for p in (src_path, left_path, right_path):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass


def _transcribe_channel_with_timestamps(api_key: str, channel_bytes: bytes) -> "list | None":
    """Bitta kanalni `whisper-1` + `verbose_json` bilan transkripsiya
    qiladi -- natija `segments` ro'yxati (har biri `start`/`end`/`text`
    bilan), keyinchalik ikki kanalni VAQT bo'yicha xronologik
    birlashtirish uchun kerak. Xato bo'lsa `None` qaytaradi."""
    try:
        resp = _openai_request(
            "POST", "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": ("channel.wav", channel_bytes, "audio/wav")},
            data={"model": _CHANNEL_TIMESTAMP_MODEL, "response_format": "verbose_json", "temperature": "0"},
            timeout=90,
        )
    except requests.RequestException as e:
        logger.warning("Kanal transkripsiyasi tarmoq xatosi: %s", e)
        return None
    if not resp.ok:
        logger.warning("Kanal transkripsiyasi xato (HTTP %s): %s", resp.status_code, _extract_openai_error(resp))
        return None
    try:
        return resp.json().get("segments") or []
    except Exception:
        return None


def try_stereo_channel_transcription(audio_bytes: bytes, audio_format: str, channels_hint: "int | None") -> "str | None":
    """Agar yozuv 2 kanalli bo'lsa VA ffmpeg mavjud bo'lsa -- har bir
    kanalni ALOHIDA transkripsiya qilib, vaqt tamg'asi bo'yicha
    xronologik "Manager: ...\\nMijoz: ...\\n" matniga birlashtiradi.
    Bu DIARIZATSIYADAN (taxmindan) ko'ra ISHONCHLIROQ -- chunki
    gapiruvchi jismoniy kanal bilan aniqlanadi, kontent bilan taxmin
    qilinmaydi. Har qanday bosqichda muammo chiqsa -- `None` qaytadi,
    chaqiruvchi mono yo'lga (diarizatsiya/oddiy transkripsiya) o'tadi."""
    if channels_hint is not None and channels_hint != 2:
        return None
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    split = split_stereo_channels(audio_bytes, audio_format)
    if not split:
        return None
    left_bytes, right_bytes = split
    left_segments = _transcribe_channel_with_timestamps(api_key, left_bytes)
    right_segments = _transcribe_channel_with_timestamps(api_key, right_bytes)
    if not left_segments and not right_segments:
        return None

    operator_is_left = CHANNEL_OPERATOR_INDEX == 0
    left_label = "Manager" if operator_is_left else "Mijoz"
    right_label = "Mijoz" if operator_is_left else "Manager"

    merged = []
    for seg in (left_segments or []):
        text = (seg.get("text") or "").strip()
        if text:
            merged.append((seg.get("start", 0.0), left_label, text))
    for seg in (right_segments or []):
        text = (seg.get("text") or "").strip()
        if text:
            merged.append((seg.get("start", 0.0), right_label, text))
    if not merged:
        return None
    merged.sort(key=lambda t: t[0])
    lines = [f"{label}: {text}" for _start, label, text in merged]
    logger.info(
        "Stereo kanal transkripsiyasi muvaffaqiyatli: %s bo'lak (chap=%s, o'ng=%s)",
        len(lines), len(left_segments or []), len(right_segments or []),
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Mono transkripsiya (oddiy, kanal ajratilmagan yozuvlar uchun)
# ---------------------------------------------------------------------------

def _post_transcription(
    api_key: str, model: str, audio_bytes: bytes, audio_format: str,
    include_language: bool = True, strong: bool = False,
):
    data = {
        "model": model,
        "response_format": "text",
        # 2026-08 V4: hint matni endi `call_glossary.py`dan olinadi --
        # markazlashtirilgan, kengaytiriladigan lug'at (dialog namunasi +
        # sohaga oid atamalar ro'yxati). `strong=True` -- SIFAT DARVOZASI
        # oldingi urinishni "suspicious"/"failed" deb topganda, QAYTA
        # urinishda til/kontekstni yanada qat'iyroq ta'kidlaydi.
        "prompt": call_glossary.build_transcription_prompt(strong=strong),
        "temperature": "0",
    }
    if include_language:
        data["language"] = "uz"
    return _openai_request(
        "POST", "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {api_key}"},
        files={"file": (f"audio.{audio_format}", audio_bytes, f"audio/{audio_format}")},
        data=data,
        timeout=90,
    )


def _call_transcribe_model(api_key: str, model: str, audio_bytes: bytes, audio_format: str, strong: bool = False) -> "str | None":
    """Bitta modelga BIR marta (mantiqiy) transkripsiya so'rovi yuboradi --
    avval `language='uz'` bilan, agar bu ANIQ shu parametr sababli 400
    bilan rad etilsa (ba'zi model/akkauntlar buni qo'llab-quvvatlamaydi),
    DARHOL tilsiz qayta so'raydi. Bu ICHKI API-moslik fallback -- SIFAT
    darvozasi retry hisobiga KIRMAYDI (foydalanuvchi so'ragan "maksimal
    2-3 SIFAT urinishi" shu emas, balki tashqi `_mono_transcribe_ladder`
    darajasidagi urinishlar)."""
    for include_language in (True, False):
        resp = _post_transcription(api_key, model, audio_bytes, audio_format, include_language=include_language, strong=strong)
        if resp.status_code == 400 and include_language and "language" in _extract_openai_error(resp).lower():
            continue
        if resp.status_code == 404:
            logger.warning("OpenAI transkripsiya modeli '%s' topilmadi (404): %s", model, _extract_openai_error(resp))
            return None
        if not resp.ok:
            raise RuntimeError(f"OpenAI transkripsiya xatosi (model={model}, HTTP {resp.status_code}): {_extract_openai_error(resp)}")
        return (resp.text or "").strip() or None
    return None


_QUALITY_RANK = {"good": 3, "suspicious": 2, "failed": 1, "empty": 0, "error": 0}


def _mono_transcribe_ladder(audio_bytes: bytes, audio_format: str, audio_duration_sec: "float | None", attempts_log: list) -> "tuple[str, str, str, float, list] | None":
    """Mono/aralash yozuv uchun -- foydalanuvchi ANIQ so'ragan 3 bosqichli
    SIFAT-nazorat qilingan qayta urinish zanjiri:
      1-urinish: asosiy model (`gpt-4o-transcribe`), oddiy kontekst.
      2-urinish: XUDDI SHU model, lekin KUCHLIROQ til/kontekst ta'kidi
                 bilan (1-urinish "suspicious"/"failed" bo'lsa).
      3-urinish (zaxira): BOSHQA model (`whisper-1`) -- ba'zan muammo
                 modelning o'ziga xos bo'lishi mumkin, shuning uchun
                 haqiqatda BOSHQA modelni sinab ko'ramiz.
    Har bir urinishdan keyin `call_quality.assess_quality()` orqali sifat
    tekshiriladi -- FAQAT "good" YETARLI deb topiladi, aks holda keyingi
    urinishga o'tiladi. Uchala urinish ham "good" bermasa -- ENG YAXSHI
    (eng past darajada yomon) natija DEBUG uchun qaytariladi, lekin
    chaqiruvchi buni TAHLIL uchun ishlatmasligi kerak (`quality_status`ni
    tekshirsin)."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY sozlanmagan -- qo'ng'iroq tahlili ishlamaydi.")

    primary_model = os.environ.get("OPENAI_TRANSCRIBE_MODEL") or _TRANSCRIBE_MODEL_CANDIDATES[0]
    fallback_model = os.environ.get("OPENAI_TRANSCRIBE_FALLBACK_MODEL", "whisper-1")
    if fallback_model == primary_model:
        fallback_model = "whisper-1" if primary_model != "whisper-1" else "gpt-4o-mini-transcribe"

    plan = [
        (primary_model, False, "oddiy kontekst (1-urinish)"),
        (primary_model, True, "kuchliroq til/kontekst ta'kidi (2-urinish, sifat pastligi uchun qayta)"),
        (fallback_model, True, "zaxira model (3-urinish, boshqa model)"),
    ]

    best = None  # (text, model, quality_status, confidence, reasons)
    for model, strong, note in plan:
        try:
            text = _call_transcribe_model(api_key, model, audio_bytes, audio_format, strong=strong)
        except RuntimeError as e:
            attempts_log.append({"attempt": len(attempts_log) + 1, "model": model, "note": note, "quality": "error", "confidence": 0.0, "reasons": [str(e)]})
            continue
        if not text:
            attempts_log.append({"attempt": len(attempts_log) + 1, "model": model, "note": note, "quality": "empty", "confidence": 0.0, "reasons": ["Bo'sh transkripsiya qaytdi yoki model mavjud emas."]})
            continue
        q = call_quality.assess_quality(text, audio_duration_sec)
        attempts_log.append({
            "attempt": len(attempts_log) + 1, "model": model, "note": note,
            "quality": q["status"], "confidence": q["confidence"], "reasons": q["reasons"], "preview": text[:200],
        })
        if call_quality.is_acceptable(q["status"]):
            return text, model, q["status"], q["confidence"], q["reasons"]
        if best is None or _QUALITY_RANK[q["status"]] > _QUALITY_RANK[best[2]]:
            best = (text, model, q["status"], q["confidence"], q["reasons"])

    return best


# ---------------------------------------------------------------------------
# Diarizatsiya (ixtiyoriy qatlam, mono yozuvlar uchun)
# ---------------------------------------------------------------------------

def _post_diarized_transcription(api_key: str, model: str, audio_bytes: bytes, audio_format: str):
    return _openai_request(
        "POST", "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {api_key}"},
        files={"file": (f"audio.{audio_format}", audio_bytes, f"audio/{audio_format}")},
        data={"model": model, "response_format": "diarized_json", "chunking_strategy": "auto"},
        timeout=90,
    )


# 2026-08 V5, foydalanuvchi ANIQ ko'rsatdi: "birinchi gapirgan = Manager"
# TAXMINI XAVFLI (chiquvchi/kiruvchi qo'ng'iroqlarda, yoki suhbat
# boshi kesilgan yozuvlarda, bu ko'pincha NOTO'G'RI). Shuning uchun
# BUTUNLAY OLIB TASHLANDI -- endi Manager/Mijoz FAQAT quyidagi kabi
# operatorga XOS iboralar TOPILGANDA (dalil bilan) belgilanadi; aks
# holda "Speaker 1"/"Speaker 2" saqlanadi (foydalanuvchi ANIQ so'ragan:
# "noto'g'ri ishonch bilan belgilashdan ko'ra noaniqlikni saqlash afzal").
_OPERATOR_EVIDENCE_PHRASES = [
    "firmasidan", "kompaniyasidan", "yordam bera olaman", "buyurtma qabul",
    "menejerman", "operatorman", "qanday yordam bera olaman", "xush kelibsiz",
    "filialimiz", "kompaniyamiz", "assalomu alaykum, xush kelibsiz",
]


def _guess_operator_speaker(speaker_texts: dict, first_speaker: "str | None" = None, call_direction: "str | None" = None) -> "str | None":
    """Har bir xom gapiruvchi ID uchun to'plangan (QAYTA TRANSKRIPSIYA
    QILINGAN, diarizatsiyaning o'z matni EMAS) matndan operatorga XOS
    iboralar sonini hisoblaydi. FAQAT bitta gapiruvchi ANIQ ko'proq
    (kamida 1ta va boshqasidan ko'p) mos kelsa -- o'sha operator deb
    ISHONCH bilan qaytariladi. Teng bo'lsa yoki hech kimda topilmasa --
    `None` (noaniq, chaqiruvchi "Speaker 1/2" saqlaydi).

    2026-08 V6, foydalanuvchi ANIQ so'ragan ("CallRecord.direction'ni BIR
    signal sifatida ishlat, lekin YAGONA signal sifatida EMAS; gapiruvchi
    shaxsini FAQAT navbat tartibi+yo'nalish orqali QATTIQ belgilama"):
    `call_direction`/`first_speaker` HECH QACHON yolg'iz o'zi ishonch
    yaratmaydi VA HATTO TENGLIKNI (tie) ham hal qilmaydi -- nudge FAQAT
    `first_speaker` so'z-dalili bo'yicha ALLAQACHON (nudge'siz) BOSHQA
    HAR BIR nomzoddan QAT'IY ko'proq bo'lsa qo'llaniladi (ya'ni natijaga
    umuman TA'SIR qilmaydi, faqat ishonch farqini SAL kengaytiradi) --
    shu bilan 0-0 yoki N-N tenglik holatida yo'nalish HECH QACHON
    "g'olibni" o'zi belgilay olmaydi."""
    hits = {speaker: sum(1 for p in _OPERATOR_EVIDENCE_PHRASES if p in text.lower()) for speaker, text in speaker_texts.items()}
    if call_direction == "outgoing" and first_speaker and first_speaker in hits:
        others = [v for k, v in hits.items() if k != first_speaker]
        if others and hits[first_speaker] > max(others):
            hits[first_speaker] += 0.5
    ranked = sorted(hits.items(), key=lambda kv: kv[1], reverse=True)
    if len(ranked) >= 2 and ranked[0][1] > 0 and ranked[0][1] > ranked[1][1]:
        return ranked[0][0]
    return None


# ---------------------------------------------------------------------------
# SEGMENT DARAJASIDA QAYTA TRANSKRIPSIYA (2026-08 V6)
# ---------------------------------------------------------------------------
# Foydalanuvchi HAQIQIY production misoli bilan ko'rsatdi: `gpt-4o-transcribe-
# diarize`ning O'Z MATNI ba'zan ma'nosiz so'zlar "to'qib chiqaradi" ("duxovoy
# karina labarakam", "Bitonkarige bargo" va h.k.) -- garchi gapiruvchi/vaqt
# ajratishi TO'G'RI bo'lsa ham. ENDI diarizatsiya FAQAT quyidagilar uchun
# ishlatiladi: gapiruvchi ID + boshlanish/tugash vaqti. HAQIQIY matn har bir
# gapiruvchi bo'lagini (guruhlangan, yostiqchali) ffmpeg bilan ORIGINAL
# audiodan kesib olib, alohida `gpt-4o-transcribe`ga yuborish orqali olinadi.


def _group_diarization_segments(segments: list) -> list:
    """Diarizatsiya `segments`ini ("speaker","start","end") ketma-ket BIR
    XIL gapiruvchi bo'laklarini BITTA guruhga birlashtiradi -- foydalanuvchi
    ANIQ so'ragan: "0.5-2 soniyalik har bir bo'lakni ALOHIDA transkripsiya
    qilish KONTEKSTNI buzadi va hallyusinatsiyani oshiradi". Guruhlash
    qoidasi: FAQAT bir xil gapiruvchi VA orasidagi tanaffus juda katta
    bo'lmasa (`_SEGMENT_GROUP_MAX_GAP_SEC`) VA natija davomiyligi
    `_SEGMENT_GROUP_TARGET_MAX_SEC`dan oshib ketmasa -- birlashtiriladi.
    Gapiruvchi almashishi HAR DOIM tabiiy navbat (turn) chegarasi sifatida
    SAQLANADI (hech qachon kesib o'tilmaydi)."""
    if not segments:
        return []
    groups = []
    current = None
    for seg in segments:
        speaker = str(seg.get("speaker", "")).strip() or "?"
        try:
            start = float(seg.get("start", 0.0) or 0.0)
        except (TypeError, ValueError):
            start = 0.0
        try:
            end = float(seg.get("end", start) or start)
        except (TypeError, ValueError):
            end = start
        if end < start:
            end = start
        if current is None:
            current = {"speaker": speaker, "start": start, "end": end, "diar_segments": [seg]}
            continue
        gap = start - current["end"]
        prospective_duration = end - current["start"]
        same_speaker = speaker == current["speaker"]
        if same_speaker and gap <= _SEGMENT_GROUP_MAX_GAP_SEC and prospective_duration <= _SEGMENT_GROUP_TARGET_MAX_SEC:
            current["end"] = max(current["end"], end)
            current["diar_segments"].append(seg)
        else:
            groups.append(current)
            current = {"speaker": speaker, "start": start, "end": end, "diar_segments": [seg]}
    if current:
        groups.append(current)
    return groups


def _compute_padded_bounds(start: float, end: float, total_duration_sec: "float | None") -> "tuple[float, float]":
    """Segment chegaralariga yostiqcha (padding) qo'shadi (birinchi/oxirgi
    bo'g'in kesilib qolmasligi uchun) -- audio chegaralaridan (0 va umumiy
    davomiylik) TASHQARIGA HECH QACHON chiqmaydi. `total_duration_sec`
    noma'lum bo'lsa (ffprobe mavjud emas) -- faqat pastki chegara (0)
    qo'llaniladi, yuqori chegara ffmpeg'ning o'ziga (fayl oxirigacha)
    qoldiriladi."""
    padded_start = max(0.0, start - _SEGMENT_PAD_BEFORE_SEC)
    padded_end = end + _SEGMENT_PAD_AFTER_SEC
    if total_duration_sec is not None and total_duration_sec > 0:
        padded_end = min(padded_end, total_duration_sec)
    if padded_end <= padded_start:
        padded_end = padded_start + 0.05
    return padded_start, padded_end


def _cut_audio_segment_bytes(src_path: str, start: float, end: float, total_duration_sec: "float | None") -> "bytes | None":
    """ffmpeg bilan `src_path`dagi ORIGINAL audiodan `[start,end]` (+
    yostiqcha) oralig'ini 16kHz mono WAV sifatida kesib oladi. Original
    fayl o'zgartirilmaydi (faqat o'qiladi); natija vaqtinchalik faylga
    yozilib, o'qilgach DARHOL o'chiriladi. Muvaffaqiyatsiz bo'lsa `None`."""
    padded_start, padded_end = _compute_padded_bounds(start, end, total_duration_sec)
    duration = max(0.05, padded_end - padded_start)
    out_path = None
    try:
        out_fd, out_path = tempfile.mkstemp(suffix=".wav")
        os.close(out_fd)
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-ss", f"{padded_start:.3f}", "-i", src_path,
                "-t", f"{duration:.3f}", "-ar", "16000", "-ac", "1", out_path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            logger.warning("ffmpeg segment kesishda xato: %s", (proc.stderr or "")[:300])
            return None
        with open(out_path, "rb") as f:
            data = f.read()
        return data or None
    except Exception as e:
        logger.warning("Segment kesishda kutilmagan xato: %s", e)
        return None
    finally:
        if out_path:
            try:
                os.unlink(out_path)
            except OSError:
                pass


def _transcribe_segment_group(group: dict, group_index: int) -> dict:
    """Bitta guruhlangan segmentni (ORIGINAL audiodan allaqachon ffmpeg
    bilan kesilgan, `group["audio_bytes"]`da) `_mono_transcribe_ladder`
    orqali transkripsiya qiladi -- bu ZANJIR ALLAQACHON foydalanuvchi
    so'ragan "maksimal 2 qayta urinish" qoidasiga mos (1-urinish oddiy +
    2-urinish kuchli kontekst + 3-urinish zaxira model = 1 boshlang'ich +
    2 qayta urinish). Agar HECH BIRI "good" bermasa -- matn o'rniga
    `_NOANIQ_MARKER` qo'yiladi (foydalanuvchi ANIQ so'ragan: "TO'QIB
    CHIQARISHDAN ko'ra noaniqlikni saqlash AFZAL"). Qaytaradi: to'liq
    DEBUG lug'ati (section 14 talabiga mos)."""
    segment_attempts: list = []
    audio_bytes = group.get("audio_bytes")
    segment_duration = group["end"] - group["start"]
    debug = {
        "group_index": group_index,
        "raw_speaker": group["speaker"],
        "start": round(group["start"], 2),
        "end": round(group["end"], 2),
        "duration_sec": round(segment_duration, 2),
        "diar_segment_count": len(group.get("diar_segments") or []),
        "diar_text_preview": " ".join((s.get("text") or "").strip() for s in (group.get("diar_segments") or []))[:200],
    }
    if not audio_bytes:
        debug.update({"attempts": [], "retries": 0, "final_text": _NOANIQ_MARKER, "used_noaniq": True, "quality": "error", "reason": "ffmpeg segmentni kesa olmadi"})
        return debug
    try:
        best = _mono_transcribe_ladder(audio_bytes, "wav", segment_duration, segment_attempts)
    except RuntimeError as e:
        debug.update({"attempts": segment_attempts, "retries": len(segment_attempts), "final_text": _NOANIQ_MARKER, "used_noaniq": True, "quality": "error", "reason": str(e)})
        return debug
    debug["attempts"] = segment_attempts
    debug["retries"] = max(0, len(segment_attempts) - 1)
    if best and call_quality.is_acceptable(best[2]):
        text, model, quality, confidence, reasons = best
        debug.update({"final_text": text, "used_noaniq": False, "quality": quality, "confidence": confidence, "model": model})
    elif best:
        # Sifat darvozasi HECH BIR urinishni "good" deb topmadi -- garbage
        # matnni SAQLAMAYMIZ (faqat debug uchun preview'da ko'rsatamiz),
        # yakuniy transkriptga `_NOANIQ_MARKER` qo'yamiz.
        text, model, quality, confidence, reasons = best
        debug.update({
            "final_text": _NOANIQ_MARKER, "used_noaniq": True, "quality": quality, "confidence": confidence,
            "model": model, "rejected_text_preview": text[:200] if text else "",
        })
    else:
        debug.update({"final_text": _NOANIQ_MARKER, "used_noaniq": True, "quality": "empty", "confidence": 0.0, "reason": "Hech qanday urinish natija bermadi."})
    return debug


def _reassemble_call_from_diarization(audio_bytes: bytes, audio_format: str, audio_duration_sec: "float | None", call_direction: "str | None" = None) -> "dict | None":
    """Diarizatsiya + segment darajasida qayta transkripsiya -- 2026-08 V6
    arxitekturasining markazi. Muvaffaqiyatsiz bo'lsa (ffmpeg yo'q,
    diarizatsiya so'rovi ishlamadi, hech qanday guruh chiqmadi) `None`
    qaytaradi -- chaqiruvchi ODDIY mono zanjirga (`_mono_transcribe_ladder`)
    o'tishi kerak. MUHIM: diarizatsiyaning O'Z MATNI HECH QACHON yakuniy
    transkriptga ishlatilmaydi -- faqat gapiruvchi ID va vaqt chegaralari
    uchun.

    Qaytaradi (muvaffaqiyatli bo'lsa):
      {
        "text": str, "model": str, "quality_status": ..., "confidence": ...,
        "quality_reasons": [...], "diarized_raw_json": str,
        "segment_debug_json": str,  # section 14 -- to'liq debug JSON
      }
    """
    if not _ffmpeg_available():
        logger.info("ffmpeg mavjud emas -- segment darajasida qayta transkripsiya o'tkazib yuboriladi (oddiy zanjirga o'tiladi).")
        return None
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    diar_model = os.environ.get("OPENAI_DIARIZE_MODEL", "gpt-4o-transcribe-diarize")
    try:
        resp = _post_diarized_transcription(api_key, diar_model, audio_bytes, audio_format)
    except requests.RequestException as e:
        logger.warning("Diarizatsiya so'rovi (model=%s) tarmoq xatosi bilan tugadi: %s", diar_model, e)
        return None
    if not resp.ok:
        logger.warning(
            "Diarizatsiya modeli '%s' ishlamadi (HTTP %s): %s -- oddiy transkripsiya yo'liga o'tilmoqda.",
            diar_model, resp.status_code, _extract_openai_error(resp),
        )
        return None
    try:
        raw_json = resp.json()
        raw_segments = raw_json.get("segments") or []
    except Exception:
        logger.warning("Diarizatsiya javobini JSON qilib o'qib bo'lmadi -- oddiy transkripsiya yo'liga o'tilmoqda.")
        return None
    if not raw_segments:
        return None
    try:
        diarized_raw_json = json.dumps(raw_json, ensure_ascii=False)[:20000]
    except Exception:
        diarized_raw_json = ""

    groups = _group_diarization_segments(raw_segments)
    if not groups:
        return None

    # Original audio'ni BITTA vaqtinchalik faylga yozamiz (har bir segment
    # uchun qayta yozmaslik uchun) -- original bayt-massiv o'zgartirilmaydi.
    src_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=f".{audio_format}", delete=False) as src:
            src.write(audio_bytes)
            src_path = src.name

        for group in groups:
            group["audio_bytes"] = _cut_audio_segment_bytes(src_path, group["start"], group["end"], audio_duration_sec)
    finally:
        if src_path:
            try:
                os.unlink(src_path)
            except OSError:
                pass

    # Har bir guruhni CHRONOLOGIK tartibda (allaqachon shunday, chunki
    # diarizatsiya segmentlari vaqt bo'yicha keladi) qayta transkripsiya
    # qilamiz -- gapiruvchi almashishi TABIIY navbat chegarasi sifatida
    # saqlanadi, boshqa segmentga TA'SIR qilmaydi (har biri MUSTAQIL).
    group_debugs = [_transcribe_segment_group(g, i) for i, g in enumerate(groups)]

    noaniq_count = sum(1 for d in group_debugs if d["used_noaniq"])
    noaniq_ratio = noaniq_count / len(group_debugs) if group_debugs else 1.0

    # Gapiruvchi-rolini (Manager/Mijoz) FAQAT haqiqiy (qayta transkripsiya
    # qilingan, `[noaniq]` EMAS) matndan to'plangan dalil asosida aniqlaymiz --
    # diarizatsiyaning o'z (ehtimol yaramas) matnidan EMAS.
    speaker_order = []
    speaker_texts: dict = {}
    for g, d in zip(groups, group_debugs):
        raw_speaker = g["speaker"]
        if raw_speaker not in speaker_order:
            speaker_order.append(raw_speaker)
        if not d["used_noaniq"]:
            speaker_texts[raw_speaker] = speaker_texts.get(raw_speaker, "") + " " + d["final_text"]

    operator_speaker = None
    if len(speaker_order) == 2:
        operator_speaker = _guess_operator_speaker(speaker_texts, first_speaker=speaker_order[0], call_direction=call_direction)
    speaker_mapping_confident = operator_speaker is not None

    lines = []
    for g, d in zip(groups, group_debugs):
        raw_speaker = g["speaker"]
        if speaker_mapping_confident:
            label = "Manager" if raw_speaker == operator_speaker else "Mijoz"
        else:
            label = f"Speaker {speaker_order.index(raw_speaker) + 1}"
        d["speaker_label"] = label
        lines.append(f"{label}: {d['final_text']}")
    final_text = "\n".join(lines)

    # Umumiy sifat: standart matn-darajasidagi tekshiruv (skript/harf/
    # takrorlanish) VA `[noaniq]` ulushi -- IKKALASIDAN ENG YOMONI olinadi
    # (foydalanuvchi ANIQ so'ragan: "ko'p qismi noaniq bo'lsa, ishonchli
    # tahlil YASAMA").
    generic_q = call_quality.evaluate_transcription_quality(final_text, audio_duration_sec)
    status, confidence, reasons = generic_q["status"], generic_q["confidence"], list(generic_q["reasons"])
    if noaniq_ratio >= _SEGMENT_NOANIQ_BLOCK_RATIO:
        status = "failed"
        confidence = min(confidence, 1.0 - noaniq_ratio)
        reasons.append(f"Bo'laklarning {noaniq_ratio:.0%} qismi aniqlanmadi ([noaniq]) -- ishonchli tahlil uchun yetarli emas.")
    elif noaniq_ratio >= _SEGMENT_NOANIQ_WARN_RATIO and status == "good":
        status = "suspicious"
        confidence = min(confidence, 1.0 - noaniq_ratio)
        reasons.append(f"Bo'laklarning {noaniq_ratio:.0%} qismi aniqlanmadi ([noaniq]).")

    segment_debug = {
        "diarize_model": diar_model,
        "groups": group_debugs,
        "noaniq_ratio": round(noaniq_ratio, 3),
        "speaker_mapping": {
            "confident": speaker_mapping_confident,
            "mapped_operator_raw_speaker": operator_speaker,
            "raw_speakers_found": speaker_order,
            "call_direction_considered": call_direction,
        },
    }
    try:
        segment_debug_json = json.dumps(segment_debug, ensure_ascii=False)[:20000]
    except Exception:
        segment_debug_json = ""

    return {
        "text": final_text,
        "model": "gpt-4o-transcribe (diarizatsiya + segment darajasida qayta transkripsiya)",
        "quality_status": status,
        "confidence": round(max(0.0, min(1.0, confidence)), 2),
        "quality_reasons": reasons,
        "diarized_raw_json": diarized_raw_json,
        "segment_debug_json": segment_debug_json,
    }


# ---------------------------------------------------------------------------
# TO'LIQ transkripsiya orkestratsiyasi -- SIFAT DARVOZASI bilan
# ---------------------------------------------------------------------------

def transcribe_with_quality_gate(audio_bytes: bytes, audio_format: str, audio_duration_sec: "float | None", channels: "int | None", call_direction: "str | None" = None) -> dict:
    """2026-08, foydalanuvchi haqiqiy muammoni ko'rsatgach QO'SHILDI:
    ba'zi qo'ng'iroqlar TURKCHA/ma'nosiz matnga transkripsiya qilinib,
    keyin tahlil modeli shu yaramas matndan umumiy/noto'g'ri xulosa
    "to'qib chiqarardi". ENDI hech qanday transkripsiya to'g'ridan-to'g'ri
    tahlilga yuborilmaydi -- avval SIFATI tekshiriladi (`call_quality.py`),
    sifatsiz bo'lsa QAYTA URINILADI (boshqa model/kontekst bilan), va agar
    hech biri "good" bermasa -- ANIQ `"transcription_failed"` holati
    qaytariladi (tahlil UMUMAN chaqirilmaydi, xulosa TO'QILMAYDI).

    Urinish tartibi (eng ishonchlidan boshlab):
      1) 2-kanalli (stereo) bo'lsa -- kanal bo'yicha ajratib transkripsiya
         (jismoniy kanalga asoslangan, taxminsiz -- eng ishonchli).
      2) Diarizatsiya + SEGMENT DARAJASIDA QAYTA TRANSKRIPSIYA (2026-08 V6):
         diarizatsiya FAQAT gapiruvchi/vaqt uchun ishlatiladi, HAQIQIY matn
         har bir guruhlangan bo'lakni ffmpeg bilan kesib, alohida
         `gpt-4o-transcribe`ga yuborish orqali olinadi (diarizatsiyaning o'z
         matni HECH QACHON ishlatilmaydi).
      3) Oddiy transkripsiya 3-bosqichli SIFAT-nazorat zanjiri
         (`_mono_transcribe_ladder`) -- agar (2) ffmpeg yo'qligi yoki
         diarizatsiya so'rovi ishlamagani sababli o'tkazib yuborilgan bo'lsa.

    Qaytaradi:
      {
        "text": str | None,           # TANLANGAN transkripsiya matni (eng yaxshisi, hatto "good" bo'lmasa ham -- debug uchun)
        "model": str | None,
        "quality_status": "good" | "suspicious" | "failed",
        "confidence": 0.0-1.0,          # tanlangan variantning sifat-ishonchi (ai_transcription_confidence uchun)
        "quality_reasons": [str, ...],  # tanlangan variant uchun sabablar (ai_transcription_quality_reasons uchun)
        "attempts": [...],             # barcha urinishlar (debug/UI uchun)
        "diarized_raw_json": str | None,
        "segment_debug_json": str | None,  # section 14 -- to'liq segment-darajasidagi debug
        "operator_channel_used": int | None,  # stereo-split ishlatilgan bo'lsa CHANNEL_OPERATOR_INDEX, aks holda None
      }

    MUHIM: chaqiruvchi FAQAT `quality_status == "good"` bo'lsa tahlilga
    yuborishi kerak -- boshqa holatda `ai_stage = "transcription_failed"`
    qo'yilishi va tahlil UMUMAN chaqirilmasligi kerak."""
    attempts_log = []
    candidates = []  # (text, model, quality_status, confidence, reasons, operator_channel_used, segment_debug_json)
    diarized_raw_json = None
    segment_debug_json = None

    if channels == 2:
        stereo_text = try_stereo_channel_transcription(audio_bytes, audio_format, channels)
        if stereo_text:
            model_label = f"{_CHANNEL_TIMESTAMP_MODEL} (stereo-split)"
            q = call_quality.assess_quality(stereo_text, audio_duration_sec)
            attempts_log.append({
                "attempt": len(attempts_log) + 1, "model": model_label, "note": "stereo-kanal ajratish",
                "quality": q["status"], "confidence": q["confidence"], "reasons": q["reasons"], "preview": stereo_text[:200],
            })
            if call_quality.is_acceptable(q["status"]):
                return {
                    "text": stereo_text, "model": model_label, "quality_status": "good",
                    "confidence": q["confidence"], "quality_reasons": q["reasons"],
                    "attempts": attempts_log, "diarized_raw_json": None, "segment_debug_json": None,
                    "operator_channel_used": CHANNEL_OPERATOR_INDEX,
                }
            candidates.append((stereo_text, model_label, q["status"], q["confidence"], q["reasons"], CHANNEL_OPERATOR_INDEX, None))

    # 2026-08 V6: haqiqiy stereo bo'lmasa (yoki stereo natija yetarli
    # bo'lmasa) -- diarizatsiyani NOKERAK ishlatmaslik o'rniga (foydalanuvchi
    # ANIQ so'ragan: "jismoniy kanal ALLAQACHON gapiruvchini aniqlasa,
    # diarizatsiyani ISHLATMA") biz baribir davom etamiz, chunki bu yerga
    # yetib kelgan bo'lsak jismoliy kanal YO'Q yoki yetarli emas edi.
    segment_result = _reassemble_call_from_diarization(audio_bytes, audio_format, audio_duration_sec, call_direction)
    if segment_result:
        attempts_log.append({
            "attempt": len(attempts_log) + 1, "model": segment_result["model"], "note": "diarizatsiya + segment qayta transkripsiya",
            "quality": segment_result["quality_status"], "confidence": segment_result["confidence"],
            "reasons": segment_result["quality_reasons"], "preview": (segment_result["text"] or "")[:200],
        })
        diarized_raw_json = segment_result["diarized_raw_json"]
        segment_debug_json = segment_result["segment_debug_json"]
        if call_quality.is_acceptable(segment_result["quality_status"]):
            return {
                "text": segment_result["text"], "model": segment_result["model"], "quality_status": "good",
                "confidence": segment_result["confidence"], "quality_reasons": segment_result["quality_reasons"],
                "attempts": attempts_log, "diarized_raw_json": diarized_raw_json, "segment_debug_json": segment_debug_json,
                "operator_channel_used": None,
            }
        candidates.append((
            segment_result["text"], segment_result["model"], segment_result["quality_status"],
            segment_result["confidence"], segment_result["quality_reasons"], None, segment_debug_json,
        ))

    mono_best = _mono_transcribe_ladder(audio_bytes, audio_format, audio_duration_sec, attempts_log)
    if mono_best:
        mono_text, mono_model, mono_quality, mono_confidence, mono_reasons = mono_best
        if call_quality.is_acceptable(mono_quality):
            return {
                "text": mono_text, "model": mono_model, "quality_status": "good",
                "confidence": mono_confidence, "quality_reasons": mono_reasons,
                "attempts": attempts_log, "diarized_raw_json": None, "segment_debug_json": None,
                "operator_channel_used": None,
            }
        candidates.append((mono_text, mono_model, mono_quality, mono_confidence, mono_reasons, None, None))

    # Hech biri "good" bermadi -- ENG YAXSHI (eng kam yomon) variantni
    # DEBUG/qo'lda-tekshirish uchun qaytaramiz, lekin chaqiruvchi buni
    # TAHLILGA yubormasligi kerak.
    if candidates:
        best = max(candidates, key=lambda c: _QUALITY_RANK[c[2]])
        return {
            "text": best[0], "model": best[1], "quality_status": best[2],
            "confidence": best[3], "quality_reasons": best[4],
            "attempts": attempts_log, "diarized_raw_json": diarized_raw_json, "segment_debug_json": best[6],
            "operator_channel_used": best[5],
        }
    return {
        "text": None, "model": None, "quality_status": "failed",
        "confidence": 0.0, "quality_reasons": ["Hech qanday transkripsiya urinishi natija bermadi."],
        "attempts": attempts_log, "diarized_raw_json": None, "segment_debug_json": None,
        "operator_channel_used": None,
    }


# ---------------------------------------------------------------------------
# Transkripsiya matnini "gap-bo'lib-gap" bo'laklarga ajratish (UI uchun)
# ---------------------------------------------------------------------------

_TURN_RE = re.compile(
    r"(?m)^(Manager|Mijoz|Speaker\s*\d+)\s*:\s*"
    r"(.*(?:\n(?!(?:Manager|Mijoz|Speaker\s*\d+)\s*:).*)*)"
)


def parse_transcript_turns(text: str) -> list:
    if not text:
        return []
    turns = []
    for m in _TURN_RE.finditer(text):
        speaker_raw = m.group(1).strip()
        body = m.group(2).strip()
        if not body:
            continue
        low = speaker_raw.lower()
        if low.startswith("manager"):
            speaker = "manager"
        elif low.startswith("mijoz"):
            speaker = "mijoz"
        else:
            speaker = "unknown"
        turns.append({"speaker": speaker, "raw_label": speaker_raw, "text": body})
    if not turns:
        turns = [{"speaker": "unknown", "raw_label": "", "text": text.strip()}]
    return turns


def _turns_to_labeled_text(turns: list) -> str:
    """Structured Output'dan kelgan `[{"speaker","text"}, ...]`ni
    "Manager: ...\\nMijoz: ...\\n" matniga aylantiradi (deterministik,
    server kodida -- modelning o'z formatlashiga ishonilmaydi)."""
    lines = []
    for t in turns or []:
        speaker = (t.get("speaker") or "unknown").strip().lower()
        text = (t.get("text") or "").strip()
        if not text:
            continue
        label = {"manager": "Manager", "mijoz": "Mijoz"}.get(speaker, "Speaker")
        lines.append(f"{label}: {text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tahlil bosqichi -- OpenAI Responses API + Structured Outputs (JSON Schema)
# ---------------------------------------------------------------------------

# 2026-08 V5, foydalanuvchi so'rovi -- avvalgi "modeldan 1-10 son so'rash"
# o'rniga endi DETERMINISTIK RUBRIKA: model har bir mezon uchun FAQAT
# transkriptdagi DALILGA asoslanib ball qo'yadi, YAKUNIY `score` esa
# SERVER KODIDA shu ballar YIG'INDISI sifatida hisoblanadi (modelning
# o'zi "umumiy baho" TANLAMAYDI -- shu bilan mezon-ball-yig'indi orasida
# nomuvofiqlik chiqib qolishi imkonsiz).
RUBRIC = [
    ("greeting", "Salomlashish / qo'ng'iroqni ochish", 1),
    ("needIdentified", "Mijoz ehtiyoji aniqlandimi", 2),
    ("correctAnswer", "To'g'ri/tegishli javob berildi", 2),
    ("alternativeOffered", "Muqobil yechim taklif qilindi", 1),
    ("questionsHandled", "Mijoz savollariga javob berildi", 1),
    ("nextStep", "Keyingi qadam/savdo siljishi", 1),
    ("professionalCommunication", "Professional muloqot", 1),
    ("properClosing", "To'g'ri yakunlash/callback", 1),
]
_RUBRIC_MAX_TOTAL = sum(maxp for _k, _l, maxp in RUBRIC)  # = 10


_EVIDENCE_ITEM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text", "evidenceTurnIds"],
    "properties": {
        "text": {"type": "string", "description": "ANIQ, transkriptga asoslangan band (umumiy/mavhum gap emas)."},
        "evidenceTurnIds": {
            "type": "array", "items": {"type": "integer"},
            "description": "Foydalanuvchi xabarida berilgan, [N] bilan indekslangan transkript bo'laklaridan (0 dan boshlab) qaysi biri(lari) shu bandga dalil ekani.",
        },
    },
}


def _rubric_schema_properties() -> dict:
    props = {}
    for key, _label, max_points in RUBRIC:
        props[key] = {
            "type": "object",
            "additionalProperties": False,
            "required": ["applicable", "earned", "reason", "evidenceTurnIds"],
            "properties": {
                "applicable": {"type": "boolean", "description": "Bu mezon UMUMAN shu suhbatga aloqadormi (masalan mahsulot so'ralmagan bo'lsa 'to'g'ri javob' mezoni aloqasiz bo'lishi mumkin)."},
                "earned": {"type": "integer", "enum": list(range(max_points + 1)), "description": f"0 dan {max_points} gacha -- FAQAT applicable=true bo'lsa ma'noli."},
                "reason": {"type": "string", "description": "Transkriptdagi ANIQ dalilga asoslangan qisqa izoh."},
                "evidenceTurnIds": {"type": "array", "items": {"type": "integer"}},
            },
        }
    return props


_ANALYSIS_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "overview", "scoreReasons", "customerRequest",
        "operatorMistakes", "positivePoints", "conversationResult",
        "callbackRequired", "callbackReason", "recommendedAction", "analysisConfidence",
    ],
    "properties": {
        "overview": {"type": "string", "description": "1-3 gapdan iborat, ANIQ faktlarga asoslangan xulosa (filler matn emas)."},
        "scoreReasons": {
            "type": "object",
            "additionalProperties": False,
            "required": [k for k, _l, _m in RUBRIC],
            "properties": _rubric_schema_properties(),
        },
        "customerRequest": {
            "type": "object",
            "additionalProperties": False,
            "required": ["product", "brand", "quantity", "unit", "measurement", "parameters", "intent"],
            "properties": {
                "product": {"type": ["string", "null"]},
                "brand": {"type": ["string", "null"]},
                "quantity": {"type": ["string", "null"]},
                "unit": {"type": ["string", "null"]},
                "measurement": {"type": ["string", "null"], "description": "Masalan '8 santimetrlisidan' -- aynan transkriptda kelgan ko'rinishda."},
                "parameters": {"type": "array", "items": {"type": "string"}, "description": "Boshqa xususiyatlar, masalan '120 plotnost' -- aynan transkriptda kelgan ko'rinishda saqlangan bandlar."},
                "intent": {"type": ["string", "null"]},
            },
        },
        "operatorMistakes": {"type": "array", "items": _EVIDENCE_ITEM_SCHEMA},
        "positivePoints": {"type": "array", "items": _EVIDENCE_ITEM_SCHEMA},
        "conversationResult": {"type": "string", "enum": ["sold", "lost", "pending", "information_only", "unknown"]},
        "callbackRequired": {"type": "boolean"},
        "callbackReason": {"type": ["string", "null"]},
        "recommendedAction": {"type": ["string", "null"]},
        "analysisConfidence": {"type": "number", "description": "0.0 (juda noaniq) dan 1.0 (juda ishonchli) gacha."},
    },
}

_RUBRIC_CRITERIA_TEXT = "\n".join(f'- "{key}" (max {maxp} ball): {label}' for key, label, maxp in RUBRIC)

_ANALYSIS_SYSTEM_PROMPT = f"""Sen professional savdo va mijoz bilan suhbatlarni tahlil qiluvchi AI assistentsan.

Senga qo'ng'iroq suhbatining TAYYOR TRANSKRIPSIYASI beriladi -- HAR BIR gap `[N] Gapiruvchi: matn` ko'rinishida, 0 dan boshlab INDEKSLANGAN (N -- shu gapning "turn ID"si, `evidenceTurnIds` maydonlarida shu indekslarga ishora qilasan). Matn asosan o'zbek tilida bo'lishi mumkin, lekin ruscha, inglizcha yoki boshqa so'zlar aralashishi mumkin.

QAT'IY QOIDALAR (buzilmasin):

1. Transkripsiyada AYTILGAN so'zlarni HECH QACHON "to'g'ri" yoki "adabiy" o'zbek tiliga o'zgartirma yoki tarjima qilma. Masalan agar transkripsiyada "120 plotnost, 8 santimetrlisidan kerak" deyilgan bo'lsa -- buni SHU KO'RINISHIDA saqla, "120 zichlik, 8 santimetrlik" kabi "tuzatilgan" versiyaga almashtirma.
2. Transkripsiyada YO'Q ma'lumotni o'zingdan qo'shib chiqarma yoki umumiy bilimingdan foydalanib "to'ldirma" (masalan "8 sm bazalt albatta mavjud" kabi tasdiqni faqat MATNDA aniq aytilgan bo'lsagina yoz -- kompaniyaning ICHKI mahsulot ma'lumoti tasdiqlamasa, buni "menejer noto'g'ri ma'lumot berdi" deb DA'VO QILMA, faqat "menejer shunday javob berdi" deb KUZATUV sifatida yoz).
3. Noaniq/eshitilmagan/uzilgan joylarni "[noaniq]" deb belgila -- o'zingdan taxmin qilib to'ldirma.
4. Mahsulot/brend nomini ANIQ bilmasang yoki noaniq eshitilgan bo'lsa -- "eng yaqin" nomga zo'rma-zo'raki moslashtirma, transkripsiyada qanday kelgan bo'lsa saqla yoki "[noaniq]" deb belgila.
5. GAPIRUVCHI YORLIQLARI (Manager/Mijoz/Speaker N) SENGA ALLAQACHON BERILGAN -- ularni maxsus transkripsiya pipeline'i (diarizatsiya + audio-darajasidagi dalillar asosida) aniqlagan. SEN BU YORLIQLARNI HECH QACHON O'ZGARTIRMAYSAN, QAYTA TAXMIN QILMAYSAN yoki "tuzatmaysan" -- hatto notekis/tushunarsiz ko'rinsa ham (masalan bitta "Speaker 1" juda ko'p gapirgandek tuyulsa ham, buni "aslida Manager/Mijoz" deb o'zgartirma). Sening vazifang FAQAT berilgan yorliqlar asosida MAZMUNNI tahlil qilish -- yorliqlarni QAYTA CHIQARMAYSAN va javobingda ularni takrorlamaysan.
6. HAR BIR "operatorMistakes"/"positivePoints" bandi ANIQ transkript qismiga asoslangan bo'lishi kerak -- umumiy/mavhum gap YOZMA. YOMON misol: "Manager mijozga yaxshi xizmat ko'rsatmadi." YAXSHI misol: "Manager mijoz 8 sm mahsulot so'raganida alternativani tekshirishni taklif qilmadi." HAR BIR bandning "evidenceTurnIds" maydonida berilgan transkriptdagi (0 dan boshlab hisoblanadigan, `[N]` bilan ko'rsatilgan) QAYSI bo'lak(lar) shu bandga DALIL ekanini ko'rsat.

{call_glossary.build_analysis_glossary_note()}

BAHOLASH RUBRIKASI (scoreReasons) -- har bir mezon uchun:
{_RUBRIC_CRITERIA_TEXT}
Har bir mezon uchun: agar mezon shu suhbatga UMUMAN ALOQADOR bo'lmasa (masalan mijoz muqobil variant so'ramagan, demak "alternativ taklif qilish" mezoni aloqasiz) -- "applicable": false qo'y (bu "jarima" EMAS, keyinchalik umumiy balldan CHIQARIB TASHLANADI). Aloqador bo'lsa -- "applicable": true va "earned"ga FAQAT transkriptda ANIQ dalil bo'lgan darajada ball ber (0 dan max ballgacha). "reason" va "evidenceTurnIds" orqali QAYSI transkript qismiga asoslanganingni ko'rsat.

MIJOZ SO'ROVI (customerRequest): mijoz nima so'ragani -- mahsulot, brend, miqdor, o'lchov birligi (unit -- santimetr/millimetr/kvadrat/kub), o'lcham (measurement -- masalan "8 santimetrlisidan", AYNAN transkriptda kelgan ko'rinishda), boshqa parametrlar (parameters -- ro'yxat, masalan "120 plotnost", AYNAN transkriptda kelgan ko'rinishda), va mijozning asosiy niyati (intent, masalan "narx bilish", "buyurtma berish", "shikoyat"). Aniq aytilmagan maydonlarni `null` (yoki bo'sh ro'yxat) qoldir -- o'zingdan TO'LDIRMA, TAXMIN QILMA.

MENEJER XATOLARI / IJOBIY TOMONLAR: aniq, transkripsiyaga asoslangan, dalil (evidenceTurnIds) bilan bandlar ro'yxati (bo'sh bo'lishi ham mumkin) -- qoida 6ga qara.

SUHBAT NATIJASI (conversationResult): "sold" (sotildi/buyurtma berildi), "lost" (rad etildi/qiziqmadi), "pending" (hali hal bo'lmagan, o'ylab ko'radi), "information_only" (mijoz faqat ma'lumot so'radi, sotib olish niyati aniq ko'rinmadi), "unknown" (transkripsiyadan aniqlab bo'lmaydi).

CALLBACK (callbackRequired/callbackReason): mijozga qayta qo'ng'iroq/aloqa qilish kerakmi va NEGA (masalan "keyinroq qo'ng'iroq qilaman" deyilgan bo'lsa).

TAVSIYA (recommendedAction): menejer/kompaniya keyingi safar qanday harakat qilishi kerakligi bo'yicha qisqa, amaliy tavsiya (yoki `null`, agar tavsiya qilish uchun yetarli asos bo'lmasa).

ISHONCH DARAJASI (analysisConfidence, 0.0-1.0): transkript noaniq/uzuq-yuluq yoki ma'nosi tushunarsiz qismlar ko'p bo'lsa -- PASTROQ qiymat qo'y. Aniq, to'liq, tushunarli suhbat uchun YUQORIROQ.

MUHIM: JSON'dan tashqarida hech qanday matn yozma, markdown ishlatma."""


def _extract_responses_output_text(data: dict) -> str:
    """OpenAI Responses API javobidan (`/v1/responses`) matnli JSON'ni
    chiqarib oladi. Struktura: `output` -- xabarlar ro'yxati, har birida
    `content` -- bo'laklar ro'yxati, `type == "output_text"` bo'lgani
    haqiqiy matnni saqlaydi."""
    for item in data.get("output") or []:
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if part.get("type") in ("output_text", "text") and part.get("text"):
                return part["text"]
    # Ba'zi SDK/versiyalarda qulaylik uchun to'g'ridan-to'g'ri maydon bo'lishi mumkin.
    if data.get("output_text"):
        return data["output_text"]
    raise ValueError("OpenAI Responses API javobidan matn topilmadi.")


def _parse_analysis_json(raw_text: str, turn_count: int) -> dict:
    """`turn_count` -- ANALIZ CHAQIRUVCHISI (`_analyze_transcript`) modelga
    yuborgan, PIPELINE tomonidan allaqachon aniqlangan (`parse_transcript_turns`)
    gaplar soni. 2026-08 V6: model ENDI o'zi `normalizedTranscript` ishlab
    chiqarmaydi (gapiruvchi yorliqlarini qayta o'ylab topmasligi uchun) --
    shuning uchun `evidenceTurnIds` diapazoni ENDI shu TASHQARIDAN berilgan
    sondan hisoblanadi, model javobidan EMAS."""
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    data = json.loads(text)

    if "scoreReasons" not in data or "overview" not in data:
        raise ValueError("Model javobida 'overview'/'scoreReasons' maydonlari yo'q.")

    def _clean_evidence_ids(raw_ids) -> list:
        # Model TURN indekslarini "to'qib chiqarishi" (haqiqatda mavjud
        # bo'lmagan indeks) mumkin -- shuning uchun haqiqiy diapazondan
        # tashqari qiymatlar JIM chiqarib tashlanadi, xato tashlanmaydi.
        out = []
        for i in raw_ids or []:
            try:
                i = int(i)
            except (TypeError, ValueError):
                continue
            if 0 <= i < turn_count and i not in out:
                out.append(i)
        return out

    # YAKUNIY score HECH QACHON modeldan to'g'ridan-to'g'ri olinmaydi --
    # rubrika mezonlari bo'yicha "earned/possible" (FAQAT aloqador
    # mezonlar bo'yicha) NISBATI server kodida hisoblanadi, so'ng 10
    # ballik shkalaga o'tkaziladi (foydalanuvchi ANIQ so'ragan formula:
    # earned_applicable / possible_applicable * 10).
    raw_reasons = data.get("scoreReasons") or {}
    score_reasons = []
    earned_total = 0
    possible_total = 0
    for key, label, max_points in RUBRIC:
        entry = raw_reasons.get(key) or {}
        applicable = bool(entry.get("applicable", True))
        try:
            earned = max(0, min(max_points, int(entry.get("earned", 0))))
        except (TypeError, ValueError):
            earned = 0
        if applicable:
            earned_total += earned
            possible_total += max_points
        score_reasons.append({
            "criterion": key, "label": label, "applicable": applicable,
            "earned": earned if applicable else None, "possible": max_points,
            "reason": (entry.get("reason") or "").strip(),
            "evidenceTurnIds": _clean_evidence_ids(entry.get("evidenceTurnIds")),
        })
    if possible_total > 0:
        score = max(0, min(10, round(earned_total / possible_total * 10)))
    else:
        # Hech qanday mezon aloqador emas deb topilsa (juda kamdan-kam,
        # masalan juda qisqa/uzilgan suhbat) -- neytral o'rtacha qiymat.
        score = 5

    # Status/rang HAMISHA (deterministik) hisoblangan score'dan
    # olinadi -- modelning o'z bahosiga ASLO ishonilmaydi.
    if score <= 3:
        status, color = "bad", "red"
    elif score <= 6:
        status, color = "average", "yellow"
    else:
        status, color = "good", "green"

    data["score"] = score
    data["scoreReasons"] = score_reasons
    data["status"] = status
    data["color"] = color

    def _clean_evidence_items(raw_items) -> list:
        cleaned = []
        for item in raw_items or []:
            if isinstance(item, str):
                # Model qoidani buzib oddiy string qaytarsa ham qulab tushmaslik uchun.
                cleaned.append({"text": item, "evidenceTurnIds": []})
            elif isinstance(item, dict) and item.get("text"):
                cleaned.append({"text": item["text"], "evidenceTurnIds": _clean_evidence_ids(item.get("evidenceTurnIds"))})
        return cleaned

    data["operatorMistakes"] = _clean_evidence_items(data.get("operatorMistakes"))
    data["positivePoints"] = _clean_evidence_items(data.get("positivePoints"))
    data.setdefault("customerRequest", {})
    data.setdefault("conversationResult", "unknown")
    data.setdefault("callbackRequired", False)
    data.setdefault("callbackReason", None)
    data.setdefault("recommendedAction", None)
    try:
        conf = float(data.get("analysisConfidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    data["analysisConfidence"] = max(0.0, min(1.0, conf))
    return data


_CONVERSATION_RESULT_LABELS_UZ = {
    "sold": "Sotildi",
    "lost": "Yo'qotildi",
    "pending": "Kutilmoqda",
    "information_only": "Faqat ma'lumot so'radi",
    "unknown": "Noma'lum",
}


def _build_result_summary(data: dict) -> str:
    """`ai_result` ustuni (jadvalda "Natija" sifatida ko'rsatiladi) uchun
    qisqa, server tomonidan yig'ilgan matn -- modeldan alohida erkin matn
    so'rash o'rniga, allaqachon structured maydonlardan deterministik
    quriladi."""
    sale = _CONVERSATION_RESULT_LABELS_UZ.get(data.get("conversationResult"), "Noma'lum")
    parts = [f"Natija: {sale}."]
    if data.get("callbackRequired"):
        reason = data.get("callbackReason")
        parts.append(f"Mijozga qayta bog'lanish kerak{f' ({reason})' if reason else ''}.")
    if data.get("recommendedAction"):
        parts.append(f"Tavsiya: {data['recommendedAction']}")
    return " ".join(parts)


def _render_indexed_transcript(turns: list) -> str:
    """`parse_transcript_turns()`dan kelgan gaplarni `[N] Yorliq: matn`
    ko'rinishiga o'tkazadi -- ANALIZ MODELIGA yuboriladigan matn shu
    (xom "Manager: .../Mijoz: ..." matn EMAS), shunda model `evidenceTurnIds`
    uchun ANIQ, deterministik indekslardan foydalana oladi va gapiruvchi
    yorlig'ini o'zi "qayta o'ylab topish" o'rniga aynan shu yorliqni
    ko'radi (2026-08 V6: model bu yorliqlarni ENDI o'zgartirmaydi)."""
    label_map = {"manager": "Manager", "mijoz": "Mijoz"}
    lines = []
    for i, t in enumerate(turns or []):
        speaker = (t.get("speaker") or "unknown").strip().lower()
        raw_label = (t.get("raw_label") or "").strip()
        label = label_map.get(speaker) or raw_label or "Speaker"
        lines.append(f"[{i}] {label}: {t.get('text') or ''}")
    return "\n".join(lines)


def _analyze_transcript(transcript_text: str, turns: "list | None" = None) -> dict:
    """Matn -> tahlil. OpenAI Responses API + Structured Outputs (qat'iy
    JSON Schema) orqali -- erkin matnni qo'lda parslash EMAS. Chaqiruvchi
    (`analyze_call_record`) BU FUNKSIYANI FAQAT transkripsiya SIFATI
    "good" deb topilgandan keyin chaqiradi -- sifatsiz/shubhali
    transkripsiya HECH QACHON shu yerga yetib kelmasligi kerak.

    2026-08 V6, foydalanuvchi ANIQ so'ragan MUHIM O'ZGARISH: gapiruvchi
    yorliqlari (Manager/Mijoz/Speaker N) ENDI transkripsiya PIPELINE'i
    tomonidan ALLAQACHON aniqlangan (`parse_transcript_turns` orqali) --
    tahlil MODELIGA bu yorliqlarni QAYTA ISHLAB CHIQISH so'ralmaydi (avval
    shunday edi -- `normalizedTranscript` maydoni orqali -- va aynan shu
    sabab "butun qo'ng'iroq Mijoz deb belgilanadi" xatosining ILDIZI edi:
    model o'z holicha, ko'pincha noto'g'ri, birinchi gapiruvchini yoki
    barcha noaniq gaplarni "mijoz" deb qayta belgilab, pipeline'ning
    to'g'ri/noaniq qoldirgan yorliqlarini "ustidan yozib" yuborardi).
    Endi modelga FAQAT allaqachon `[N] Yorliq: matn` bilan indekslangan,
    O'ZGARMAS matn beriladi -- model MAZMUNNI tahlil qiladi, yorliqlarni
    EMAS."""
    if turns is None:
        turns = parse_transcript_turns(transcript_text)
    indexed_transcript = _render_indexed_transcript(turns)
    api_key = os.environ.get("OPENAI_API_KEY")
    resp = _openai_request(
        "POST", "https://api.openai.com/v1/responses",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        json_body={
            "model": OPENAI_ANALYSIS_MODEL,
            "temperature": 0,
            "input": [
                {"role": "system", "content": _ANALYSIS_SYSTEM_PROMPT},
                {"role": "user", "content": f"Transkripsiya matni (har bir gap [N] bilan indekslangan, N -- evidenceTurnIds uchun ishlatiladigan turn ID):\n\n{indexed_transcript}"},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "call_analysis",
                    "schema": _ANALYSIS_JSON_SCHEMA,
                    "strict": True,
                },
            },
        },
        timeout=90,
    )
    if not resp.ok:
        err_msg = _extract_openai_error(resp)
        raise RuntimeError(f"OpenAI tahlil xatosi (model={OPENAI_ANALYSIS_MODEL}, HTTP {resp.status_code}): {err_msg}")
    raw_text = _extract_responses_output_text(resp.json())
    return _parse_analysis_json(raw_text, len(turns))


# ---------------------------------------------------------------------------
# Asosiy orkestratsiya -- bitta yozuvni tahlil qilish
# ---------------------------------------------------------------------------

def analyze_call_record(session, call) -> dict:
    """Bitta `db.CallRecord` yozuvini tahlil qiladi. Bosqichlar
    (`call.ai_stage`): processing_audio -> transcribing -> analyzing ->
    completed. Agar transkripsiya SIFATI yetarli bo'lmasa (barcha
    urinishlardan keyin ham) -- `ai_stage = "transcription_failed"`,
    TAHLIL UMUMAN CHAQIRILMAYDI (foydalanuvchi ANIQ so'ragan: sifatsiz
    transkriptdan hech qachon xulosa "to'qilmasin"). Agar OLDINGI
    urinishda transkripsiya SIFATI "good" bo'lib, faqat TAHLIL bosqichi
    kutilmagan xato bergan bo'lsa (`ai_stage == "failed"`, transkripsiya
    "good" bo'lgani `ai_transcription_quality`dan ko'rinadi) -- audio
    QAYTA yuklab olinmaydi/QAYTA transkripsiya qilinmaydi, mavjud xom
    transkripsiyadan to'g'ridan-to'g'ri tahlil qilinadi. Aksincha,
    `ai_stage == "transcription_failed"` bo'lgan yozuvlar uchun --
    transkripsiya SIFATSIZ deb topilgani uchun -- AUDIO QAYTADAN TO'LIQ
    (boshidan) qayta ishlanadi."""
    now = dt.datetime.utcnow()
    # 2026-08 V6.1, foydalanuvchi ANIQ so'ragan ("bittadan ham audio
    # qo'shish mumkin bo'lsin"): Moi Zvonki'dan kelgan yozuvlar
    # `recording_url` orqali (HTTP) yuklab olinadi, ADMIN qo'lda yuklagan
    # yozuvlar esa audio BAYTLARINI to'g'ridan-to'g'ri bazadan
    # (`uploaded_audio_data`) oladi -- IKKALASI HAM shu funksiya orqali,
    # BIR XIL pipeline (sifat darvozasi, tahlil) bilan ishlanadi.
    if not call.recording_url and not call.uploaded_audio_data:
        raise ValueError("Bu qo'ng'iroqda yozuv (recording_url) ham, qo'lda yuklangan audio ham yo'q.")

    resume_from_transcript = (
        bool(call.ai_raw_transcription)
        and call.ai_transcription_quality == "good"
        and call.ai_stage == "failed"
    )

    try:
        if resume_from_transcript:
            logger.info(
                "Qo'ng'iroq #%s: oldingi transkripsiya SIFATLI (good) deb topilgan, faqat TAHLIL "
                "qayta urinilmoqda (audio qayta yuklanmaydi).", call.id,
            )
            transcript_text = call.ai_raw_transcription
            call.ai_stage = "analyzing"
            session.commit()
        else:
            call.ai_stage = "processing_audio"
            session.commit()
            t0 = time.monotonic()
            if call.recording_url:
                audio_bytes, audio_format = _download_audio(call.recording_url)
            else:
                # Qo'lda yuklangan audio -- tarmoqdan yuklab olish SHART
                # emas, baytlar ALLAQACHON bazada.
                audio_bytes = bytes(call.uploaded_audio_data)
                audio_format = call.uploaded_audio_format or "mp3"

            metadata = probe_audio_metadata(audio_bytes, audio_format)
            channels = metadata.get("channels") if metadata else None
            duration_sec = metadata.get("duration_sec") if metadata else None
            if metadata:
                call.ai_audio_channels = metadata.get("channels")
                call.ai_audio_codec = metadata.get("codec")
                call.ai_audio_duration_sec = metadata.get("duration_sec")
                session.commit()
                duration_label = f"{duration_sec:.1f}s" if duration_sec is not None else "noma'lum"
                logger.info(
                    "Qo'ng'iroq #%s audio metadata: kodek=%s, kanal=%s, davomiylik=%s",
                    call.id, metadata.get("codec"), metadata.get("channels"), duration_label,
                )

            call.ai_stage = "transcribing"
            session.commit()

            outcome = transcribe_with_quality_gate(
                audio_bytes, audio_format, duration_sec, channels,
                call_direction=getattr(call, "direction", None),
            )
            transcribe_elapsed = time.monotonic() - t0

            call.ai_raw_transcription = outcome["text"]
            call.ai_model_transcribe = outcome["model"]
            call.ai_transcription_quality = outcome["quality_status"]
            call.ai_transcription_confidence = outcome.get("confidence")
            call.ai_transcription_quality_reasons = json.dumps(outcome.get("quality_reasons") or [], ensure_ascii=False)[:2000]
            call.ai_transcription_attempts = len(outcome["attempts"])
            call.ai_transcription_attempts_log = json.dumps(outcome["attempts"], ensure_ascii=False)[:20000]
            call.ai_operator_channel = outcome.get("operator_channel_used")
            if outcome["diarized_raw_json"]:
                call.ai_diarized_json = outcome["diarized_raw_json"]
            if outcome.get("segment_debug_json"):
                call.ai_segment_debug_json = outcome["segment_debug_json"]

            logger.info(
                "Qo'ng'iroq #%s transkripsiya tugadi: %s urinish, tanlangan model=%s, sifat=%s, %.1fs, %s belgi",
                call.id, len(outcome["attempts"]), outcome["model"], outcome["quality_status"],
                transcribe_elapsed, len(outcome["text"] or ""),
            )

            if not call_quality.is_acceptable(outcome["quality_status"]):
                # 2026-08, foydalanuvchi ANIQ so'ragan MAJBURIY QOIDA:
                # sifatsiz transkripsiya TAHLILGA YUBORILMAYDI -- yolg'on
                # ishonchli, "to'qilgan" xulosa ko'rsatishdan ko'ra, aniq
                # "transkripsiya sifati yetarli emas" holati AFZAL.
                reasons = "; ".join(
                    r for a in outcome["attempts"] for r in (a.get("reasons") or [])
                ) or "sabab aniqlanmadi"
                call.ai_error = (
                    f"Transkripsiya sifati yetarli emas ({outcome['quality_status']}, "
                    f"{len(outcome['attempts'])} urinishdan keyin). Sabablar: {reasons}"
                )[:2000]
                call.ai_stage = "transcription_failed"
                call.ai_analyzed_at = now
                session.commit()
                return {"analysisStatus": "not_analyzed_due_to_bad_transcription", "quality_status": outcome["quality_status"]}

            transcript_text = outcome["text"]
            call.ai_stage = "analyzing"
            session.commit()

        turns = parse_transcript_turns(transcript_text)
        t1 = time.monotonic()
        result = _analyze_transcript(transcript_text, turns)
        analyze_elapsed = time.monotonic() - t1
        logger.info(
            "Qo'ng'iroq #%s tahlil tugadi: model=%s, %.1fs, baho=%s, ishonch=%.2f",
            call.id, OPENAI_ANALYSIS_MODEL, analyze_elapsed, result.get("score"), result.get("analysisConfidence", 0),
        )

        # 2026-08 V6: `ai_transcription` ENDI modelning o'zi qayta yozgan
        # ("normalizedTranscript") versiya EMAS -- transkripsiya PIPELINE'i
        # o'zi ishlab chiqargan, ALLAQACHON gapiruvchi-yorliqlangan matn
        # (`transcript_text`) TO'G'RIDAN-TO'G'RI ko'rsatiladi. Bu -- "gapiruvchi
        # yorliqlarini tahlil modeli hech qachon o'zgartirmasligi" qoidasining
        # bevosita natijasi (endi o'zgartirishga IMKONIYATI ham yo'q).
        call.ai_overview = result["overview"]
        call.ai_score = result["score"]
        call.ai_score_reasons = json.dumps(result["scoreReasons"], ensure_ascii=False)
        call.ai_status = result["status"]
        call.ai_color = result["color"]
        call.ai_result = _build_result_summary(result)
        call.ai_transcription = transcript_text
        call.ai_customer_request = json.dumps(result["customerRequest"], ensure_ascii=False)
        call.ai_operator_mistakes = json.dumps(result["operatorMistakes"], ensure_ascii=False)
        call.ai_positive_points = json.dumps(result["positivePoints"], ensure_ascii=False)
        call.ai_sale_result = result["conversationResult"]
        call.ai_callback_required = bool(result["callbackRequired"])
        call.ai_callback_reason = result.get("callbackReason")
        call.ai_recommended_response = result.get("recommendedAction")
        call.ai_analysis_confidence = result["analysisConfidence"]
        call.ai_model_analysis = OPENAI_ANALYSIS_MODEL
        call.ai_error = None
        call.ai_stage = "completed"
        call.ai_analyzed_at = now
        session.commit()
        return result
    except AudioDownloadError as e:
        logger.exception("Qo'ng'iroq #%s audio yuklashda xato (kod=%s)", call.id, e.code)
        call.ai_error = f"[{e.code}] {e}"[:2000]
        call.ai_stage = "failed"
        call.ai_analyzed_at = now
        session.commit()
        raise
    except Exception as e:
        logger.exception("Qo'ng'iroq #%s tahlilida xato", call.id)
        call.ai_error = f"{type(e).__name__}: {e}"[:2000]
        call.ai_stage = "failed"
        call.ai_analyzed_at = now
        session.commit()
        raise


def run_pending_analysis(session, limit: int = 10) -> dict:
    """Hali tahlil qilinmagan, YOZUVI BOR va "haqiqiy" (min_real_talk_seconds
    chegarasidan uzun) qo'ng'iroqlarni topib, birma-bir tahlil qiladi.
    Hali umuman urinilmagan yozuvlarga USTUNLIK beriladi, qolgan joy
    bo'lsagina xatolik bilan tugaganlar qayta sinaladi (bu safar, agar
    transkripsiya avval muvaffaqiyatli bo'lgan bo'lsa, faqat tahlil
    qayta sinaladi -- `analyze_call_record` ichidagi mantiq bilan)."""
    import call_analytics
    from db import CallRecord

    if not is_configured():
        return {"analyzed": 0, "failed": 0, "remaining": 0, "retry_remaining": 0, "error": "OPENAI_API_KEY sozlanmagan"}

    min_seconds = call_analytics.get_min_real_talk_seconds()
    base_filter = (
        CallRecord.recording_url.isnot(None),
        CallRecord.duration_seconds >= min_seconds,
    )
    never_tried_q = session.query(CallRecord).filter(*base_filter, CallRecord.ai_analyzed_at.is_(None))
    retry_q = session.query(CallRecord).filter(*base_filter, CallRecord.ai_error.isnot(None))

    never_tried = never_tried_q.order_by(CallRecord.started_at.desc()).limit(limit).all()
    remaining_slots = max(0, limit - len(never_tried))
    to_retry = (
        retry_q.order_by(CallRecord.started_at.desc()).limit(remaining_slots).all()
        if remaining_slots else []
    )

    analyzed, failed = 0, 0
    for call in never_tried + to_retry:
        try:
            analyze_call_record(session, call)
            analyzed += 1
        except Exception:
            failed += 1

    return {
        "analyzed": analyzed,
        "failed": failed,
        "remaining": never_tried_q.count(),
        "retry_remaining": retry_q.count(),
    }
