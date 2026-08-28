"""test_call_analysis_offline.py — `call_analysis.py`/`call_glossary.py`/
`call_quality.py` uchun TARMOQSIZ (offline) tekshiruvlar. Haqiqiy OpenAI
API'ga chaqiruv QILMAYDI (bu sandbox'da imkonsiz) -- faqat toza-Python
mantiqni (glossary, applicable/earned->score->status/color, transkript-
parsing, retry-klassifikatsiya, JSON schema shakli, kanal-birlashtirish,
dalilga-asoslangan diarizatsiya, sifat darvozasi) tekshiradi.

Ishga tushirish:
    cd app && python3 scripts/test_call_analysis_offline.py
Muvaffaqiyatli bo'lsa "BARCHA TESTLAR O'TDI" chiqadi, aks holda
AssertionError bilan to'xtaydi (qaysi test ekani ko'rinadi).
"""

import os
import sys
import json
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import call_glossary
import call_quality
import call_analysis as ca


# ---------------------------------------------------------------------------
# call_glossary.py
# ---------------------------------------------------------------------------

def test_glossary_contains_all_terms():
    hint = call_glossary.build_glossary_hint()
    for term in ["bazalt", "penopleks", "plotnost", "Vetonit", "TYTAN", "kvadrat metr"]:
        assert term in hint, f"'{term}' lug'at hint'ida yo'q"
    print("OK: glossary hint barcha kutilgan atamalarni o'z ichiga oladi")


def test_glossary_extra_terms_extend_not_replace():
    hint = call_glossary.build_glossary_hint(extra_terms=["MaxsusMahsulot123"])
    assert "bazalt" in hint and "MaxsusMahsulot123" in hint
    print("OK: extra_terms mavjud ro'yxatni almashtirmay, kengaytiradi")


def test_transcription_prompt_has_dialogue_and_glossary():
    prompt = call_glossary.build_transcription_prompt()
    assert prompt.startswith("Manager:")
    assert "bazalt" in prompt
    print("OK: transkripsiya prompti dialog namunasi + lug'atni o'z ichiga oladi")


def test_transcription_prompt_strong_variant_forbids_language_switch():
    normal_prompt = call_glossary.build_transcription_prompt(strong=False)
    strong_prompt = call_glossary.build_transcription_prompt(strong=True)
    assert "Turkcha" in strong_prompt
    assert "Turkcha" not in normal_prompt
    print("OK: strong=True prompti Turkchaga/boshqa tilga aylantirishni ANIQ taqiqlaydi")


def test_analysis_glossary_note_warns_against_forcing():
    note = call_glossary.build_analysis_glossary_note()
    assert "TUZATISH uchun EMAS" in note
    print("OK: tahlil lug'at eslatmasi so'zlarni majburiy tuzatishni taqiqlaydi")


# ---------------------------------------------------------------------------
# call_quality.py -- sifat darvozasi (foydalanuvchi haqiqiy bug misollari)
# ---------------------------------------------------------------------------

def test_quality_gate_detects_arabic_script():
    q = call_quality.evaluate_transcription_quality("مرحبا بكم في المتجر الخاص بنا اليوم")
    assert q["status"] == "failed"
    assert q["confidence"] < 0.1
    print("OK: sifat darvozasi arabcha yozuv tizimini ANIQ 'failed' deb belgilaydi")


def test_quality_gate_detects_turkish_markers():
    q = call_quality.evaluate_transcription_quality("Allah'a sığındık.")
    assert q["status"] == "failed"
    print("OK: sifat darvozasi turkchaga xos harflarni (foydalanuvchi bug misoli) 'failed' deb belgilaydi")


def test_quality_gate_detects_portuguese_markers():
    q = call_quality.evaluate_transcription_quality(
        "Não posso ajudar você agora, então espere um pouco, por favor, obrigado"
    )
    assert q["status"] == "failed"
    print("OK: sifat darvozasi portugalchaga xos harflarni (ã/õ) 'failed' deb belgilaydi")


def test_quality_gate_detects_english_hallucination_as_suspicious():
    q = call_quality.evaluate_transcription_quality("Hello, thank you very much today, how are you please.")
    assert q["status"] == "suspicious"
    assert 0.0 < q["confidence"] < 1.0
    print("OK: sifat darvozasi inglizcha 'gibberish'ni (qattiq rad emas) 'suspicious' deb belgilaydi")


def test_quality_gate_detects_repetition_hallucination():
    q = call_quality.evaluate_transcription_quality("salom salom salom salom salom salom salom")
    assert q["status"] == "failed"
    print("OK: sifat darvozasi takrorlanish naqshini (hallyusinatsiya) 'failed' deb belgilaydi")


def test_quality_gate_rejects_empty_text():
    q = call_quality.evaluate_transcription_quality("")
    assert q["status"] == "failed" and q["confidence"] == 0.0
    print("OK: bo'sh transkripsiya 'failed'/0.0 ishonch bilan rad etiladi")


def test_quality_gate_accepts_legitimate_uzbek_russian_mix():
    q = call_quality.evaluate_transcription_quality(
        "Assalomu alaykum, sizga qanday yordam bera olaman? Bizda 8 santimetrlik penopleks bor, "
        "narxi 45000 so'm bo'ladi. Спасибо, хорошо, договорились, до свидания."
    )
    assert q["status"] == "good", q
    assert q["confidence"] == 1.0
    print("OK: haqiqiy o'zbek+rus kod-almashinuvi NOTO'G'RI rad etilmaydi (good/1.0)")


def test_quality_gate_confidence_field_always_present():
    for text in ["", "salom salom salom salom salom salom", "Assalomu alaykum, rahmat, xayr"]:
        q = call_quality.evaluate_transcription_quality(text)
        assert "confidence" in q and isinstance(q["confidence"], float)
    print("OK: evaluate_transcription_quality HAR DOIM 'confidence' maydonini qaytaradi")


# ---------------------------------------------------------------------------
# Rubrika (applicable/earned) -> deterministik score/status/color
# ---------------------------------------------------------------------------

def _score_reasons_for_total(total_score: int) -> dict:
    """Berilgan YAKUNIY ballga yig'iladigan (barcha mezonlar applicable=True)
    scoreReasons lug'atini quradi -- RUBRIC tartibida "ochko'zlik bilan"
    to'ldiradi (har bir mezon max ballidan oshmaydi)."""
    remaining = total_score
    reasons = {}
    for key, _label, max_points in ca.RUBRIC:
        earned = max(0, min(max_points, remaining))
        remaining -= earned
        reasons[key] = {"applicable": True, "earned": earned, "reason": "test", "evidenceTurnIds": []}
    return reasons


def _base_raw_analysis(**overrides) -> str:
    data = {
        "overview": "x",
        "scoreReasons": _score_reasons_for_total(overrides.pop("total_score", 5)),
        "customerRequest": {},
        "operatorMistakes": [],
        "positivePoints": [],
        "conversationResult": "unknown",
        "callbackRequired": False,
        "callbackReason": None,
        "recommendedAction": None,
        "analysisConfidence": 0.8,
    }
    data.update(overrides)
    return json.dumps(data)


def test_score_to_status_color_mapping():
    cases = [(1, "bad", "red"), (3, "bad", "red"), (4, "average", "yellow"),
             (6, "average", "yellow"), (7, "good", "green"), (10, "good", "green")]
    for score, exp_status, exp_color in cases:
        raw = _base_raw_analysis(total_score=score)
        parsed = ca._parse_analysis_json(raw, 0)
        assert parsed["score"] == score, (score, parsed["score"])
        assert parsed["status"] == exp_status and parsed["color"] == exp_color, (score, parsed)
    print("OK: earned/possible*10 -> score -> status/color deterministik xaritalash to'g'ri (barcha diapazonlar)")


def test_score_earned_clamped_to_max_points():
    reasons = {key: {"applicable": False, "earned": 0, "reason": "", "evidenceTurnIds": []} for key, _l, _m in ca.RUBRIC}
    reasons["greeting"] = {"applicable": True, "earned": 99, "reason": "haddan tashqari qiymat", "evidenceTurnIds": []}
    raw = _base_raw_analysis(scoreReasons=reasons)
    parsed = ca._parse_analysis_json(raw, 0)
    greeting_entry = next(r for r in parsed["scoreReasons"] if r["criterion"] == "greeting")
    assert greeting_entry["earned"] == 1, greeting_entry  # greeting max = 1 ball
    assert parsed["score"] == 10, parsed  # yagona applicable mezon to'liq bajarilgan -> 10/10
    print("OK: modelning 'earned' qiymati mezon max balligacha CHEGARALANADI")


def test_score_all_criteria_not_applicable_falls_back_to_five():
    reasons = {key: {"applicable": False, "earned": 0, "reason": "aloqasiz", "evidenceTurnIds": []} for key, _l, _m in ca.RUBRIC}
    raw = _base_raw_analysis(scoreReasons=reasons)
    parsed = ca._parse_analysis_json(raw, 0)
    assert parsed["score"] == 5, parsed
    print("OK: barcha mezonlar 'applicable=false' bo'lsa neytral standart ball (5) qo'llaniladi")


def test_never_trusts_model_status_even_if_present():
    # Model o'zi "status": "good" desa ham, past ball bo'lsa server "bad"/"red"ni majburlaydi.
    raw = _base_raw_analysis(total_score=2, status="good", color="green")
    parsed = ca._parse_analysis_json(raw, 0)
    assert parsed["status"] == "bad" and parsed["color"] == "red", parsed
    print("OK: modelning o'z status/color'iga ISHONILMAYDI -- score'dan qayta hisoblanadi")


def test_evidence_turn_ids_filtered_to_valid_range():
    # turn_count ENDI modelning o'z javobidan (normalizedTranscript) EMAS,
    # chaqiruvchi (`_analyze_transcript`) allaqachon bilgan (pipeline
    # tomonidan aniqlangan) gaplar sonidan (2026-08 V6) beriladi.
    raw = _base_raw_analysis(
        total_score=5,
        operatorMistakes=[{"text": "noto'g'ri javob berdi", "evidenceTurnIds": [0, 5, -1, "x", 1, 0]}],
    )
    parsed = ca._parse_analysis_json(raw, 2)
    assert parsed["operatorMistakes"][0]["evidenceTurnIds"] == [0, 1], parsed["operatorMistakes"]
    print("OK: mavjud bo'lmagan/noto'g'ri turn indekslari JIM chiqarib tashlanadi (xato tashlanmaydi)")


def test_operator_mistakes_tolerates_bare_string_fallback():
    raw = _base_raw_analysis(total_score=5, operatorMistakes=["oddiy string band (qoidaga zid)"])
    parsed = ca._parse_analysis_json(raw, 0)
    assert parsed["operatorMistakes"] == [{"text": "oddiy string band (qoidaga zid)", "evidenceTurnIds": []}]
    print("OK: operatorMistakes/positivePoints modelning xato-formatidagi (bare string) javobiga chidamli")


# ---------------------------------------------------------------------------
# Transkript-parsing
# ---------------------------------------------------------------------------

def test_turns_to_labeled_text_roundtrip():
    turns = [
        {"speaker": "manager", "text": "Assalomu alaykum"},
        {"speaker": "mijoz", "text": "Vaalaykum assalom"},
        {"speaker": "unknown", "text": "shovqin"},
    ]
    text = ca._turns_to_labeled_text(turns)
    assert text == "Manager: Assalomu alaykum\nMijoz: Vaalaykum assalom\nSpeaker: shovqin"
    parsed_back = ca.parse_transcript_turns(text)
    assert parsed_back[0]["speaker"] == "manager"
    assert parsed_back[1]["speaker"] == "mijoz"
    print("OK: structured turns <-> labeled-text round-trip ishlaydi")


def test_parse_transcript_turns_fallback_when_no_labels():
    turns = ca.parse_transcript_turns("shunchaki oddiy matn, yorliqsiz")
    assert len(turns) == 1 and turns[0]["speaker"] == "unknown"
    print("OK: yorliqsiz matn uchun fallback (bitta 'unknown' bo'lak) ishlaydi")


def test_turn_re_supports_three_plus_speakers():
    # 2026-08 V6: _TURN_RE ENDI "Speaker 1"/"Speaker 2" bilan CHEGARALANMAGAN --
    # diarizatsiya 3+ xom gapiruvchi ID chiqarsa ham to'g'ri parslanadi.
    text = "Speaker 1: birinchi\nSpeaker 2: ikkinchi\nSpeaker 3: uchinchi"
    turns = ca.parse_transcript_turns(text)
    assert len(turns) == 3
    assert turns[2]["raw_label"] == "Speaker 3" and turns[2]["speaker"] == "unknown"
    print("OK: _TURN_RE 3+ 'Speaker N' yorlig'ini ham to'g'ri ajratadi")


def test_render_indexed_transcript_preserves_given_labels():
    # 2026-08 V6: tahlil modeliga yuboriladigan matn HAR BIR gapni [N]
    # bilan indekslaydi va PIPELINE bergan yorliqni O'ZGARTIRMAY saqlaydi.
    turns = [
        {"speaker": "manager", "raw_label": "Manager", "text": "Assalomu alaykum"},
        {"speaker": "unknown", "raw_label": "Speaker 2", "text": "noaniq gap"},
    ]
    rendered = ca._render_indexed_transcript(turns)
    assert rendered == "[0] Manager: Assalomu alaykum\n[1] Speaker 2: noaniq gap"
    print("OK: _render_indexed_transcript [N] Yorliq: matn formatida, yorliqni o'zgartirmay chiqaradi")


# ---------------------------------------------------------------------------
# Diarizatsiya -- 2026-08 V6: diarizatsiya FAQAT gapiruvchi ID + vaqt uchun
# ishlatiladi; HAQIQIY matn HAR DOIM segmentni ffmpeg bilan kesib, alohida
# gpt-4o-transcribe'ga yuborish orqali olinadi ("birinchi gapiruvchi=Manager"
# yoki "diarizatsiya matni=yakuniy matn" TAXMINLARI YO'Q).
# ---------------------------------------------------------------------------

def test_group_diarization_segments_merges_consecutive_same_speaker():
    # Ketma-ket, BIR XIL gapiruvchining qisqa (0.5-2s) bo'laklari BITTA
    # guruhga birlashtirilishi kerak (foydalanuvchi ANIQ so'ragan --
    # har bir qisqa bo'lakni ALOHIDA transkripsiya qilish hallyusinatsiyani
    # oshiradi).
    segs = [
        {"speaker": "A", "start": 0.0, "end": 0.8},
        {"speaker": "A", "start": 0.9, "end": 1.6},
        {"speaker": "A", "start": 1.7, "end": 2.4},
        {"speaker": "B", "start": 2.5, "end": 4.0},
    ]
    groups = ca._group_diarization_segments(segs)
    assert len(groups) == 2, groups
    assert groups[0]["speaker"] == "A" and groups[0]["start"] == 0.0 and groups[0]["end"] == 2.4
    assert len(groups[0]["diar_segments"]) == 3
    assert groups[1]["speaker"] == "B"
    print("OK: ketma-ket bir-xil-gapiruvchi qisqa bo'laklar bitta guruhga birlashtiriladi")


def test_group_diarization_segments_never_merges_across_speaker_change():
    # Gapiruvchi almashishi HAR DOIM navbat chegarasi -- hech qachon kesib o'tilmaydi.
    segs = [
        {"speaker": "A", "start": 0.0, "end": 1.0},
        {"speaker": "B", "start": 1.0, "end": 1.3},
        {"speaker": "A", "start": 1.3, "end": 2.0},
    ]
    groups = ca._group_diarization_segments(segs)
    assert [g["speaker"] for g in groups] == ["A", "B", "A"]
    print("OK: gapiruvchi almashishi hech qachon guruh ichiga BIRLASHTIRILMAYDI")


def test_group_diarization_segments_respects_target_max_duration():
    # Uzoq tanaffussiz bir-xil-gapiruvchi ketma-ketlik ham
    # _SEGMENT_GROUP_TARGET_MAX_SEC dan oshib ketsa YANGI guruhga o'tadi.
    segs = [{"speaker": "A", "start": float(i * 5), "end": float(i * 5 + 4.5)} for i in range(5)]  # 0-4.5, 5-9.5, ...
    groups = ca._group_diarization_segments(segs)
    assert len(groups) >= 2, groups
    assert all(g["end"] - g["start"] <= ca._SEGMENT_GROUP_TARGET_MAX_SEC for g in groups)
    print("OK: guruh davomiyligi _SEGMENT_GROUP_TARGET_MAX_SEC dan oshsa yangi guruh boshlanadi")


def test_compute_padded_bounds_adds_padding_within_audio_bounds():
    start, end = ca._compute_padded_bounds(5.0, 6.0, total_duration_sec=100.0)
    assert start == 5.0 - ca._SEGMENT_PAD_BEFORE_SEC
    assert end == 6.0 + ca._SEGMENT_PAD_AFTER_SEC
    print("OK: oddiy holatda padding ikkala tomondan ham qo'shiladi")


def test_compute_padded_bounds_clamped_to_audio_bounds():
    # Boshida -- 0dan PASTGA chiqmaydi. Oxirida -- umumiy davomiylikdan OSHMAYDI.
    start, end = ca._compute_padded_bounds(0.1, 0.3, total_duration_sec=1.0)
    assert start == 0.0, start
    start2, end2 = ca._compute_padded_bounds(0.5, 0.99, total_duration_sec=1.0)
    assert end2 == 1.0, end2
    print("OK: padding audio chegaralaridan (0 va umumiy davomiylik) TASHQARIGA chiqmaydi")


def test_transcribe_segment_group_prefers_noaniq_over_bad_text():
    # Foydalanuvchi ANIQ so'ragan: hech qanday urinish "good" bermasa --
    # TO'QILGAN/shubhali matn SAQLANMAYDI, [noaniq] qo'yiladi.
    group = {"speaker": "A", "start": 0.0, "end": 2.0, "audio_bytes": b"fake", "diar_segments": [{"text": "diarizatsiya-matni"}]}
    bad_result = ("ma'nosiz to'qilgan matn", "gpt-4o-transcribe", "suspicious", 0.2, ["shubhali"])
    with mock.patch.object(ca, "_mono_transcribe_ladder", return_value=bad_result):
        debug = ca._transcribe_segment_group(group, 0)
    assert debug["used_noaniq"] is True
    assert debug["final_text"] == ca._NOANIQ_MARKER
    assert debug["rejected_text_preview"] == "ma'nosiz to'qilgan matn"
    print("OK: hech qanday urinish 'good' bermasa, TO'QILGAN matn o'rniga [noaniq] qo'yiladi")


def test_transcribe_segment_group_uses_good_retranscription_not_diarize_text():
    # Diarizatsiyaning O'Z matni ("diarizatsiya-matni") emas, balki
    # segmentni QAYTA TRANSKRIPSIYA qilingan ("gpt-transcribe" chiqargan)
    # matn yakuniy natijaga tushishi kerak.
    group = {"speaker": "A", "start": 0.0, "end": 2.0, "audio_bytes": b"fake", "diar_segments": [{"text": "diarizatsiya-matni (ishonchsiz)"}]}
    good_result = ("Assalomu alaykum, penopleks kerak edi", "gpt-4o-transcribe", "good", 0.9, [])
    with mock.patch.object(ca, "_mono_transcribe_ladder", return_value=good_result):
        debug = ca._transcribe_segment_group(group, 0)
    assert debug["used_noaniq"] is False
    assert debug["final_text"] == "Assalomu alaykum, penopleks kerak edi"
    assert "diarizatsiya-matni" not in debug["final_text"]
    print("OK: yakuniy matn diarizatsiyaning o'z matnidan EMAS, qayta-transkripsiya natijasidan olinadi")


def test_transcribe_segment_group_isolation_one_bad_segment_does_not_affect_other():
    # Bitta shubhali segmentning qayta urinishi FAQAT o'ziga tegishli --
    # boshqa (yaxshi) segmentga TA'SIR qilmaydi.
    good_group = {"speaker": "A", "start": 0.0, "end": 2.0, "audio_bytes": b"good-audio", "diar_segments": []}
    bad_group = {"speaker": "B", "start": 2.0, "end": 4.0, "audio_bytes": b"bad-audio", "diar_segments": []}
    good_result = ("yaxshi va tushunarli javob berildi", "gpt-4o-transcribe", "good", 0.95, [])
    bad_result = ("g'alati so'zlar tartibsiz", "gpt-4o-transcribe", "suspicious", 0.3, ["shubhali"])

    def fake_ladder(audio_bytes, fmt, duration, attempts_log):
        return bad_result if audio_bytes == b"bad-audio" else good_result

    with mock.patch.object(ca, "_mono_transcribe_ladder", side_effect=fake_ladder):
        good_debug = ca._transcribe_segment_group(good_group, 0)
        bad_debug = ca._transcribe_segment_group(bad_group, 1)
    assert good_debug["used_noaniq"] is False and good_debug["final_text"] == "yaxshi va tushunarli javob berildi"
    assert bad_debug["used_noaniq"] is True and bad_debug["final_text"] == ca._NOANIQ_MARKER
    print("OK: bitta segmentning shubhali natijasi/qayta urinishi boshqa segmentga ta'sir qilmaydi (izolyatsiya)")


def _fake_diarize_response(segments):
    class _Resp:
        ok = True
        def json(self):
            return {"segments": segments}
    return _Resp()


def test_reassemble_uses_retranscribed_text_not_diarize_own_text():
    # Diarizatsiya JAVOBINING o'zi ma'nosiz matn qaytarsa ham, YAKUNIY
    # transkript segmentni QAYTA transkripsiya qilingan (to'g'ri) matndan
    # tuzilishi kerak -- diarizatsiya matni HECH QACHON ishlatilmaydi.
    diar_segments = [
        {"speaker": "spk_0", "start": 0.0, "end": 2.0, "text": "duxovoy karina labarakam"},
        {"speaker": "spk_1", "start": 2.0, "end": 4.0, "text": "Bitonkarige bargo shanam"},
    ]
    real_texts = {
        "spk_0": "Assalomu alaykum, sizga qanday yordam bera olaman?",
        "spk_1": "Menga penopleks kerak edi, narxi qancha bo'ladi?",
    }

    def fake_transcribe_segment_group(group, idx):
        return {
            "group_index": idx, "raw_speaker": group["speaker"], "start": group["start"], "end": group["end"],
            "final_text": real_texts[group["speaker"]], "used_noaniq": False, "quality": "good",
            "confidence": 0.9, "model": "gpt-4o-transcribe", "attempts": [], "retries": 0,
            "diar_segment_count": 1, "diar_text_preview": "",
        }

    with mock.patch.object(ca, "_ffmpeg_available", return_value=True), \
         mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), \
         mock.patch.object(ca, "_post_diarized_transcription", return_value=_fake_diarize_response(diar_segments)), \
         mock.patch.object(ca, "_cut_audio_segment_bytes", return_value=b"cut-audio"), \
         mock.patch.object(ca, "_transcribe_segment_group", side_effect=fake_transcribe_segment_group):
        result = ca._reassemble_call_from_diarization(b"audio", "wav", 4.0, call_direction=None)

    assert result is not None
    assert "duxovoy karina" not in result["text"] and "Bitonkarige" not in result["text"]
    assert "Assalomu alaykum" in result["text"] and "penopleks" in result["text"]
    print("OK: yakuniy transkript diarizatsiyaning o'z matnidan EMAS, qayta-transkripsiya (gpt-transcribe) natijasidan tuziladi")


def test_reassemble_preserves_chronological_order():
    diar_segments = [
        {"speaker": "spk_0", "start": 0.0, "end": 1.0, "text": "x"},
        {"speaker": "spk_1", "start": 1.0, "end": 2.0, "text": "y"},
        {"speaker": "spk_0", "start": 2.0, "end": 3.0, "text": "z"},
    ]
    order = {"spk_0": ["birinchi gap", "uchinchi gap"], "spk_1": ["ikkinchi gap"]}
    counters = {"spk_0": 0, "spk_1": 0}

    def fake_transcribe_segment_group(group, idx):
        text = order[group["speaker"]][counters[group["speaker"]]]
        counters[group["speaker"]] += 1
        return {"final_text": text, "used_noaniq": False, "quality": "good", "confidence": 0.9,
                "model": "gpt-4o-transcribe", "attempts": [], "retries": 0, "raw_speaker": group["speaker"],
                "start": group["start"], "end": group["end"], "diar_segment_count": 1, "diar_text_preview": ""}

    with mock.patch.object(ca, "_ffmpeg_available", return_value=True), \
         mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), \
         mock.patch.object(ca, "_post_diarized_transcription", return_value=_fake_diarize_response(diar_segments)), \
         mock.patch.object(ca, "_cut_audio_segment_bytes", return_value=b"cut-audio"), \
         mock.patch.object(ca, "_transcribe_segment_group", side_effect=fake_transcribe_segment_group):
        result = ca._reassemble_call_from_diarization(b"audio", "wav", 3.0, call_direction=None)

    lines = result["text"].split("\n")
    assert "birinchi gap" in lines[0]
    assert "ikkinchi gap" in lines[1]
    assert "uchinchi gap" in lines[2]
    print("OK: yakuniy transkript XRONOLOGIK tartibda (diarizatsiya segmentlari kelgan tartibda) tuziladi")


def test_reassemble_keeps_speaker_labels_when_mapping_uncertain():
    # 2026-08 V6, foydalanuvchi ANIQ so'ragan: "birinchi gapiruvchi=Manager"
    # yoki "barcha noaniq=Mijoz" QILINMASIN -- dalil yetarli bo'lmasa
    # Speaker A/B (Speaker 1/2) saqlanishi kerak.
    diar_segments = [
        {"speaker": "spk_0", "start": 0.0, "end": 1.0, "text": "?"},
        {"speaker": "spk_1", "start": 1.0, "end": 2.0, "text": "?"},
    ]

    def fake_transcribe_segment_group(group, idx):
        return {"final_text": "oddiy salomlashish, dalilsiz", "used_noaniq": False, "quality": "good",
                "confidence": 0.8, "model": "gpt-4o-transcribe", "attempts": [], "retries": 0,
                "raw_speaker": group["speaker"], "start": group["start"], "end": group["end"],
                "diar_segment_count": 1, "diar_text_preview": ""}

    with mock.patch.object(ca, "_ffmpeg_available", return_value=True), \
         mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), \
         mock.patch.object(ca, "_post_diarized_transcription", return_value=_fake_diarize_response(diar_segments)), \
         mock.patch.object(ca, "_cut_audio_segment_bytes", return_value=b"cut-audio"), \
         mock.patch.object(ca, "_transcribe_segment_group", side_effect=fake_transcribe_segment_group):
        result = ca._reassemble_call_from_diarization(b"audio", "wav", 2.0, call_direction=None)

    assert "Manager" not in result["text"] and "Mijoz" not in result["text"]
    assert "Speaker 1" in result["text"] and "Speaker 2" in result["text"]
    assert result["segment_debug_json"] and json.loads(result["segment_debug_json"])["speaker_mapping"]["confident"] is False
    print("OK: dalil yetarli bo'lmasa 'Speaker 1/2' saqlanadi -- 'butun qo'ng'iroq Mijoz' xatosi YO'Q")


def test_reassemble_never_labels_everyone_mijoz():
    # To'g'ridan-to'g'ri, foydalanuvchi ko'rsatgan real production xatoni
    # regressiyaga qarshi tekshiradi: hech qanday sharoitda YAKUNIY matnda
    # HAMMA gap "Mijoz:" bilan boshlanmasligi kerak (kamida bitta boshqa
    # yorliq -- Manager yoki Speaker N -- bo'lishi kerak).
    diar_segments = [
        {"speaker": "spk_0", "start": 0.0, "end": 1.0, "text": "?"},
        {"speaker": "spk_1", "start": 1.0, "end": 2.0, "text": "?"},
        {"speaker": "spk_0", "start": 2.0, "end": 3.0, "text": "?"},
        {"speaker": "spk_1", "start": 3.0, "end": 4.0, "text": "?"},
    ]

    def fake_transcribe_segment_group(group, idx):
        return {"final_text": f"gap {idx}", "used_noaniq": False, "quality": "good",
                "confidence": 0.8, "model": "gpt-4o-transcribe", "attempts": [], "retries": 0,
                "raw_speaker": group["speaker"], "start": group["start"], "end": group["end"],
                "diar_segment_count": 1, "diar_text_preview": ""}

    with mock.patch.object(ca, "_ffmpeg_available", return_value=True), \
         mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), \
         mock.patch.object(ca, "_post_diarized_transcription", return_value=_fake_diarize_response(diar_segments)), \
         mock.patch.object(ca, "_cut_audio_segment_bytes", return_value=b"cut-audio"), \
         mock.patch.object(ca, "_transcribe_segment_group", side_effect=fake_transcribe_segment_group):
        result = ca._reassemble_call_from_diarization(b"audio", "wav", 4.0, call_direction=None)

    lines = [l for l in result["text"].split("\n") if l.strip()]
    labels = {l.split(":", 1)[0] for l in lines}
    assert labels != {"Mijoz"}, result["text"]
    assert len(labels) >= 2, result["text"]
    print("OK: hech qachon BARCHA gaplar 'Mijoz' deb bitta yorliqqa tushib qolmaydi (kamida 2 xil yorliq)")


def test_guess_operator_speaker_evidence_based_not_order_based():
    speaker_texts = {
        "spk_0": "Menga penopleks kerak edi.",
        "spk_1": "Assalomu alaykum, sizga qanday yordam bera olaman?",
    }
    # spk_0 birinchi gapirgan bo'lsa ham, dalil spk_1'da -- shuning uchun operator = spk_1.
    result = ca._guess_operator_speaker(speaker_texts, first_speaker="spk_0", call_direction=None)
    assert result == "spk_1", result
    print("OK: operator FAQAT dalilga (iboralarga) qarab aniqlanadi, gapirish tartibiga qarab EMAS")


def test_guess_operator_speaker_no_evidence_returns_none():
    speaker_texts = {"spk_0": "salom", "spk_1": "salom, qalaysiz"}
    result = ca._guess_operator_speaker(speaker_texts, first_speaker="spk_0", call_direction="outgoing")
    assert result is None
    print("OK: hech kimda dalil bo'lmasa, yo'nalish/tartib YOLG'IZ ishonch YARATMAYDI (None qaytadi)")


def test_guess_operator_speaker_direction_only_nudges_existing_lead_not_creates_tie_winner():
    # Ikkala nomzodda BIR XIL (nolga teng bo'lmagan) dalil miqdori bo'lsa,
    # yo'nalish TENGLIKNI hal QILMAYDI (faqat ALLAQACHON YETAKCHI bo'lgan
    # nomzodni kuchaytiradi).
    speaker_texts = {
        "spk_0": "yordam bera olaman",
        "spk_1": "yordam bera olaman",
    }
    result = ca._guess_operator_speaker(speaker_texts, first_speaker="spk_0", call_direction="outgoing")
    assert result is None, result
    print("OK: yo'nalish TENG dalil holatida g'olibni O'ZI belgilamaydi (faqat mavjud yetakchini kuchaytiradi)")


def test_channel_operator_index_mapping():
    # CHANNEL_OPERATOR_INDEX standart 0 (chap kanal = operator).
    assert ca.CHANNEL_OPERATOR_INDEX == 0
    print("OK: standart operator-kanal indeksi 0 (chap kanal)")


def test_stereo_merge_sorts_chronologically():
    left_segments = [{"start": 5.0, "text": "ikkinchi gap (chap)"}, {"start": 0.0, "text": "birinchi gap (chap)"}]
    right_segments = [{"start": 2.5, "text": "o'rtadagi gap (o'ng)"}]
    with mock.patch.object(ca, "split_stereo_channels", return_value=(b"left", b"right")), \
         mock.patch.object(ca, "_transcribe_channel_with_timestamps", side_effect=[left_segments, right_segments]), \
         mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
        result = ca.try_stereo_channel_transcription(b"fake-audio", "wav", channels_hint=2)
    lines = result.split("\n")
    assert lines[0] == "Manager: birinchi gap (chap)"
    assert lines[1] == "Mijoz: o'rtadagi gap (o'ng)"
    assert lines[2] == "Manager: ikkinchi gap (chap)"
    print("OK: stereo kanal birlashtirish VAQT bo'yicha xronologik tartiblanadi")


def test_stereo_skipped_when_channels_not_two():
    result = ca.try_stereo_channel_transcription(b"x", "wav", channels_hint=1)
    assert result is None
    print("OK: mono (1-kanalli) yozuvlar uchun stereo yo'l o'tkazib yuboriladi")


# ---------------------------------------------------------------------------
# _download_audio() -- nomlangan xato kodlari (audio_invalid/audio_expired/
# audio_download_failed)
# ---------------------------------------------------------------------------

class _FakeDownloadResp:
    def __init__(self, status_code=200, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests as _requests
            raise _requests.HTTPError(f"HTTP {self.status_code}")


def test_download_audio_network_error_gets_download_failed_code():
    import requests as _requests
    with mock.patch.object(ca.requests, "get", side_effect=_requests.ConnectionError("timeout")):
        try:
            ca._download_audio("https://example.com/rec.mp3")
            assert False, "AudioDownloadError kutilgan edi"
        except ca.AudioDownloadError as e:
            assert e.code == "audio_download_failed", e.code
    print("OK: tarmoq xatosi 'audio_download_failed' kodi bilan aniq belgilanadi")


def test_download_audio_expired_link_gets_expired_code():
    with mock.patch.object(ca.requests, "get", return_value=_FakeDownloadResp(status_code=404)):
        try:
            ca._download_audio("https://example.com/rec.mp3")
            assert False, "AudioDownloadError kutilgan edi"
        except ca.AudioDownloadError as e:
            assert e.code == "audio_expired", e.code
    print("OK: 404 qaytargan havola 'audio_expired' kodi bilan aniq belgilanadi")


def test_download_audio_non_audio_body_gets_invalid_code():
    resp = _FakeDownloadResp(status_code=200, content=b'{"error": "not ready"}', headers={"Content-Type": "application/json"})
    with mock.patch.object(ca.requests, "get", return_value=resp):
        try:
            ca._download_audio("https://example.com/rec.mp3")
            assert False, "AudioDownloadError kutilgan edi"
        except ca.AudioDownloadError as e:
            assert e.code == "audio_invalid", e.code
    print("OK: JSON/matn javob (audio o'rniga) 'audio_invalid' kodi bilan aniq belgilanadi")


def test_download_audio_too_small_gets_invalid_code():
    resp = _FakeDownloadResp(status_code=200, content=b"x" * 10, headers={"Content-Type": "audio/mpeg"})
    with mock.patch.object(ca.requests, "get", return_value=resp):
        try:
            ca._download_audio("https://example.com/rec.mp3")
            assert False, "AudioDownloadError kutilgan edi"
        except ca.AudioDownloadError as e:
            assert e.code == "audio_invalid", e.code
    print("OK: juda kichik audio fayli 'audio_invalid' kodi bilan aniq belgilanadi")


def test_download_audio_valid_mp3_succeeds():
    mp3_bytes = b"ID3" + b"\x00" * 1000
    resp = _FakeDownloadResp(status_code=200, content=mp3_bytes, headers={"Content-Type": "audio/mpeg"})
    with mock.patch.object(ca.requests, "get", return_value=resp):
        data, fmt = ca._download_audio("https://example.com/rec.mp3")
    assert data == mp3_bytes and fmt == "mp3"
    print("OK: haqiqiy mp3 audio muvaffaqiyatli yuklanadi va formati to'g'ri aniqlanadi")


# ---------------------------------------------------------------------------
# Sifat-darvozasi -> orkestratsiya (confidence/reasons propagatsiyasi)
# ---------------------------------------------------------------------------

def test_mono_ladder_propagates_confidence():
    good_text = (
        "Assalomu alaykum, sizga qanday yordam bera olaman? Menga penopleks kerak, narxi qancha bo'ladi?"
    )
    with mock.patch.object(ca, "_call_transcribe_model", return_value=good_text), \
         mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
        attempts = []
        result = ca._mono_transcribe_ladder(b"audio", "wav", None, attempts)
    assert result is not None
    _text, _model, quality, confidence, reasons = result
    assert quality == "good" and confidence == 1.0 and reasons == []
    assert attempts[0]["quality"] == "good" and attempts[0]["confidence"] == 1.0
    print("OK: _mono_transcribe_ladder confidence/reasons'ni attempts_log VA qaytish qiymatiga TO'G'RI ko'chiradi")


def test_transcribe_with_quality_gate_returns_confidence_and_reasons():
    with mock.patch.object(ca, "_reassemble_call_from_diarization", return_value=None), \
         mock.patch.object(ca, "_mono_transcribe_ladder", return_value=("matn", "gpt-4o-transcribe", "suspicious", 0.42, ["sabab"])):
        outcome = ca.transcribe_with_quality_gate(b"audio", "wav", 20.0, channels=1)
    assert outcome["quality_status"] == "suspicious"
    assert outcome["confidence"] == 0.42
    assert outcome["quality_reasons"] == ["sabab"]
    assert outcome["operator_channel_used"] is None
    print("OK: transcribe_with_quality_gate mono-zanjirdan confidence/sabablarni to'g'ri ko'taradi")


def test_transcribe_with_quality_gate_stereo_good_sets_operator_channel():
    good_text = "Assalomu alaykum, sizga qanday yordam bera olaman? Menga penopleks kerak, narxi qancha bo'ladi?"
    with mock.patch.object(ca, "try_stereo_channel_transcription", return_value=good_text):
        outcome = ca.transcribe_with_quality_gate(b"audio", "wav", None, channels=2)
    assert outcome["quality_status"] == "good"
    assert outcome["confidence"] == 1.0
    assert outcome["operator_channel_used"] == ca.CHANNEL_OPERATOR_INDEX
    print("OK: stereo-split muvaffaqiyatli bo'lsa operator_channel_used = CHANNEL_OPERATOR_INDEX qilib belgilanadi")


def test_transcribe_with_quality_gate_stereo_good_skips_diarization_regression():
    # 2026-08 V6 regressiya tekshiruvi: jismoniy STEREO kanal ALLAQACHON
    # gapiruvchini ishonchli aniqlagan bo'lsa -- diarizatsiya/segment-darajasidagi
    # qayta transkripsiya UMUMAN chaqirilmasligi kerak (foydalanuvchi ANIQ
    # so'ragan: "haqiqiy stereo bo'lsa, diarizatsiyani keraksiz ishlatma").
    good_text = "Assalomu alaykum, sizga qanday yordam bera olaman? Menga penopleks kerak, narxi qancha bo'ladi?"
    with mock.patch.object(ca, "try_stereo_channel_transcription", return_value=good_text), \
         mock.patch.object(ca, "_reassemble_call_from_diarization") as mock_reassemble:
        outcome = ca.transcribe_with_quality_gate(b"audio", "wav", None, channels=2)
    assert outcome["quality_status"] == "good"
    mock_reassemble.assert_not_called()
    print("OK: stereo natijasi 'good' bo'lsa, diarizatsiya/segment-qayta-transkripsiya UMUMAN chaqirilmaydi")


# ---------------------------------------------------------------------------
# ai_result xulosasi
# ---------------------------------------------------------------------------

def test_result_summary_includes_conversation_result_and_callback():
    summary = ca._build_result_summary({
        "conversationResult": "sold", "callbackRequired": True, "callbackReason": "ertaga qo'ng'iroq qiladi",
        "recommendedAction": "Ertaga qo'ng'iroq qil.",
    })
    assert "Sotildi" in summary
    assert "qayta bog'lanish" in summary
    assert "Ertaga qo'ng'iroq qil." in summary
    print("OK: ai_result xulosasi conversationResult/callback/tavsiyani to'g'ri birlashtiradi")


def test_result_summary_information_only_label():
    summary = ca._build_result_summary({"conversationResult": "information_only", "callbackRequired": False})
    assert "Faqat ma'lumot so'radi" in summary
    print("OK: 'information_only' natijasi to'g'ri o'zbekcha yorliq bilan ko'rsatiladi")


# ---------------------------------------------------------------------------
# Tarmoq/retry qatlami
# ---------------------------------------------------------------------------

def test_openai_request_retries_on_5xx_then_succeeds():
    calls = {"n": 0}

    class FakeResp:
        def __init__(self, status_code):
            self.status_code = status_code
            self.ok = status_code < 400
            self.text = ""

        def json(self):
            return {}

    def fake_request(method, url, **kwargs):
        calls["n"] += 1
        return FakeResp(500) if calls["n"] == 1 else FakeResp(200)

    with mock.patch("requests.request", side_effect=fake_request), mock.patch("time.sleep", return_value=None):
        resp = ca._openai_request("POST", "https://api.openai.com/v1/x", headers={})
    assert calls["n"] == 2 and resp.status_code == 200
    print("OK: 5xx xatoda avtomatik qayta uriniladi (va muvaffaqiyatga erishadi)")


def test_openai_request_does_not_retry_on_400():
    calls = {"n": 0}

    class FakeResp:
        status_code = 400
        ok = False
        text = "bad request"

        def json(self):
            return {"error": {"message": "bad request"}}

    def fake_request(method, url, **kwargs):
        calls["n"] += 1
        return FakeResp()

    with mock.patch("requests.request", side_effect=fake_request), mock.patch("time.sleep", return_value=None):
        resp = ca._openai_request("POST", "https://api.openai.com/v1/x", headers={})
    assert calls["n"] == 1, "400 (validatsiya xatosi) qayta urinilmasligi kerak edi"
    assert resp.status_code == 400
    print("OK: 400 (validatsiya) xatosida QAYTA URINILMAYDI (darhol qaytariladi)")


# ---------------------------------------------------------------------------
# JSON Schema shakli
# ---------------------------------------------------------------------------

def test_analysis_json_schema_shape():
    schema = ca._ANALYSIS_JSON_SCHEMA
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    for key in ["overview", "scoreReasons", "customerRequest",
                "operatorMistakes", "positivePoints", "conversationResult",
                "callbackRequired", "callbackReason", "recommendedAction", "analysisConfidence"]:
        assert key in schema["required"], f"'{key}' schema.required'da yo'q"
        assert key in schema["properties"], f"'{key}' schema.properties'da yo'q"
    assert schema["properties"]["conversationResult"]["enum"] == ["sold", "lost", "pending", "information_only", "unknown"]
    for key, _label, _maxp in ca.RUBRIC:
        assert key in schema["properties"]["scoreReasons"]["required"]
        props = schema["properties"]["scoreReasons"]["properties"][key]["properties"]
        assert set(props.keys()) == {"applicable", "earned", "reason", "evidenceTurnIds"}
    print("OK: tahlil JSON Schema shakli spesifikatsiyaga mos (barcha maydonlar, enum'lar, rubrika)")


def test_analysis_json_schema_no_longer_asks_model_to_relabel_speakers():
    # 2026-08 V6, foydalanuvchi ANIQ so'ragan MUHIM REGRESSIYA tekshiruvi:
    # tahlil modeli ENDI "normalizedTranscript" (o'z holicha gapiruvchi
    # qayta-belgilash) ISHLAB CHIQARMAYDI -- aynan shu maydon "butun
    # qo'ng'iroq Mijoz" xatosining ILDIZI edi.
    schema = ca._ANALYSIS_JSON_SCHEMA
    assert "normalizedTranscript" not in schema["required"]
    assert "normalizedTranscript" not in schema["properties"]
    print("OK: 'normalizedTranscript' JSON Schema'dan OLIB TASHLANGAN -- model gapiruvchi yorliqlarini ENDI qayta ishlab chiqarmaydi")


def test_analyze_transcript_sends_indexed_transcript_not_raw_labels_to_relabel():
    # `_analyze_transcript` modelga PIPELINE aniqlagan yorliqlar bilan
    # ALLAQACHON indekslangan matn yuborishi kerak -- xom "Manager: .../
    # Mijoz: ..." matnni EMAS (model buni qayta formatlab/relabel qilishi
    # mumkin bo'lgan xom matn emas, deterministik tayyor matn beriladi).
    transcript_text = "Manager: Assalomu alaykum\nMijoz: Vaalaykum assalom"
    canned = _base_raw_analysis(total_score=5)
    captured = {}

    def fake_openai_request(method, url, *, headers, json_body=None, **kwargs):
        captured["json_body"] = json_body
        class _Resp:
            ok = True
            def json(self):
                return {"output": [{"type": "message", "content": [{"type": "output_text", "text": canned}]}]}
        return _Resp()

    with mock.patch.object(ca, "_openai_request", side_effect=fake_openai_request), \
         mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
        ca._analyze_transcript(transcript_text)

    user_content = captured["json_body"]["input"][1]["content"]
    assert "[0] Manager: Assalomu alaykum" in user_content
    assert "[1] Mijoz: Vaalaykum assalom" in user_content
    assert "normalizedTranscript" not in json.dumps(captured["json_body"]["text"]["format"]["schema"])
    print("OK: _analyze_transcript modelga [N]-indekslangan, ALLAQACHON yorliqlangan matn yuboradi (relabel so'ramaydi)")


def test_customer_request_schema_shape():
    props = ca._ANALYSIS_JSON_SCHEMA["properties"]["customerRequest"]
    for key in ["product", "brand", "quantity", "unit", "measurement", "parameters", "intent"]:
        assert key in props["required"], f"'{key}' customerRequest.required'da yo'q"
        assert key in props["properties"], f"'{key}' customerRequest.properties'da yo'q"
    assert props["properties"]["parameters"]["type"] == "array"
    print("OK: customerRequest schema yangi shaklga mos (measurement/parameters bilan)")


def test_extract_responses_output_text():
    data = {
        "output": [
            {"type": "reasoning", "content": []},
            {"type": "message", "content": [{"type": "output_text", "text": '{"a":1}'}]},
        ]
    }
    assert ca._extract_responses_output_text(data) == '{"a":1}'
    print("OK: Responses API javobidan output_text to'g'ri chiqariladi")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
