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
        "normalizedTranscript": [],
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
        parsed = ca._parse_analysis_json(raw)
        assert parsed["score"] == score, (score, parsed["score"])
        assert parsed["status"] == exp_status and parsed["color"] == exp_color, (score, parsed)
    print("OK: earned/possible*10 -> score -> status/color deterministik xaritalash to'g'ri (barcha diapazonlar)")


def test_score_earned_clamped_to_max_points():
    reasons = {key: {"applicable": False, "earned": 0, "reason": "", "evidenceTurnIds": []} for key, _l, _m in ca.RUBRIC}
    reasons["greeting"] = {"applicable": True, "earned": 99, "reason": "haddan tashqari qiymat", "evidenceTurnIds": []}
    raw = _base_raw_analysis(scoreReasons=reasons)
    parsed = ca._parse_analysis_json(raw)
    greeting_entry = next(r for r in parsed["scoreReasons"] if r["criterion"] == "greeting")
    assert greeting_entry["earned"] == 1, greeting_entry  # greeting max = 1 ball
    assert parsed["score"] == 10, parsed  # yagona applicable mezon to'liq bajarilgan -> 10/10
    print("OK: modelning 'earned' qiymati mezon max balligacha CHEGARALANADI")


def test_score_all_criteria_not_applicable_falls_back_to_five():
    reasons = {key: {"applicable": False, "earned": 0, "reason": "aloqasiz", "evidenceTurnIds": []} for key, _l, _m in ca.RUBRIC}
    raw = _base_raw_analysis(scoreReasons=reasons)
    parsed = ca._parse_analysis_json(raw)
    assert parsed["score"] == 5, parsed
    print("OK: barcha mezonlar 'applicable=false' bo'lsa neytral standart ball (5) qo'llaniladi")


def test_never_trusts_model_status_even_if_present():
    # Model o'zi "status": "good" desa ham, past ball bo'lsa server "bad"/"red"ni majburlaydi.
    raw = _base_raw_analysis(total_score=2, status="good", color="green")
    parsed = ca._parse_analysis_json(raw)
    assert parsed["status"] == "bad" and parsed["color"] == "red", parsed
    print("OK: modelning o'z status/color'iga ISHONILMAYDI -- score'dan qayta hisoblanadi")


def test_evidence_turn_ids_filtered_to_valid_range():
    raw = _base_raw_analysis(
        total_score=5,
        normalizedTranscript=[{"speaker": "manager", "text": "a"}, {"speaker": "mijoz", "text": "b"}],
        operatorMistakes=[{"text": "noto'g'ri javob berdi", "evidenceTurnIds": [0, 5, -1, "x", 1, 0]}],
    )
    parsed = ca._parse_analysis_json(raw)
    assert parsed["operatorMistakes"][0]["evidenceTurnIds"] == [0, 1], parsed["operatorMistakes"]
    print("OK: mavjud bo'lmagan/noto'g'ri turn indekslari JIM chiqarib tashlanadi (xato tashlanmaydi)")


def test_operator_mistakes_tolerates_bare_string_fallback():
    raw = _base_raw_analysis(total_score=5, operatorMistakes=["oddiy string band (qoidaga zid)"])
    parsed = ca._parse_analysis_json(raw)
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


# ---------------------------------------------------------------------------
# Diarizatsiya -- "birinchi gapiruvchi=Manager" TAXMINI OLIB TASHLANGAN,
# endi FAQAT dalilga (operatorga xos iboralar) asoslanadi.
# ---------------------------------------------------------------------------

def test_diarize_no_evidence_stays_unconfident():
    # 2 gapiruvchi, lekin HECH birida operatorga xos ibora yo'q --
    # "birinchi = Manager" deb NOTO'G'RI ishonch bilan belgilanMAYDI.
    segs = [{"speaker": "A", "text": "Salom"}, {"speaker": "B", "text": "Salom, qalaysiz"}]
    text, confident = ca._build_labeled_transcript_from_segments(segs)
    assert confident is False
    assert "Speaker 1" in text and "Speaker 2" in text
    assert "Manager" not in text and "Mijoz" not in text
    print("OK: dalilsiz 2-gapiruvchi holatda ENDI 'Speaker 1/2' saqlanadi (Manager deb TAXMIN qilinmaydi)")


def test_diarize_with_operator_evidence_assigns_manager():
    # B gapiruvchida operatorga XOS ibora bor ("yordam bera olaman") --
    # shu ASOSDA (dalil bilan) B = Manager deb ishonch bilan belgilanadi.
    segs = [
        {"speaker": "A", "text": "Menga penopleks kerak edi."},
        {"speaker": "B", "text": "Assalomu alaykum, sizga qanday yordam bera olaman?"},
    ]
    text, confident = ca._build_labeled_transcript_from_segments(segs)
    assert confident is True
    lines = text.split("\n")
    assert lines[0].startswith("Mijoz:")   # A birinchi gapirgan, lekin dalil YO'Q -- Manager deb TAXMIN qilinmaydi
    assert lines[1].startswith("Manager:")  # B da dalil bor
    print("OK: dalil (operatorga xos ibora) topilganda TO'G'RI gapiruvchi Manager deb belgilanadi (tartibga qaramay)")


def test_diarize_three_speakers_never_confident():
    # 3+ "gapiruvchi" (masalan diarizatsiya xatosi) -- ikki-kishilik
    # solishtiruv mantiqi UMUMAN ishlatilmaydi, har doim "Speaker N".
    segs = [{"speaker": "A", "text": "salom"}, {"speaker": "B", "text": "assalom"}, {"speaker": "C", "text": "?"}]
    text, confident = ca._build_labeled_transcript_from_segments(segs)
    assert confident is False
    assert "Speaker 1" in text and "Manager" not in text
    print("OK: 3+ gapiruvchi holatda hech qachon Manager/Mijoz deb ISHONCH bilan belgilanmaydi")


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
    with mock.patch.object(ca, "_try_diarized_transcription", return_value=None), \
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
    for key in ["overview", "scoreReasons", "normalizedTranscript", "customerRequest",
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
