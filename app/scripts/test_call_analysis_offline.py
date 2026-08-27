"""test_call_analysis_offline.py — `call_analysis.py`/`call_glossary.py`
uchun TARMOQSIZ (offline) tekshiruvlar. Haqiqiy OpenAI API'ga chaqiruv
QILMAYDI (bu sandbox'da imkonsiz) -- faqat toza-Python mantiqni
(glossary, score->status/color, transkript-parsing, retry-klassifikatsiya,
JSON schema shakli, kanal-birlashtirish) tekshiradi.

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
import call_analysis as ca


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


def test_analysis_glossary_note_warns_against_forcing():
    note = call_glossary.build_analysis_glossary_note()
    assert "TUZATISH uchun EMAS" in note
    print("OK: tahlil lug'at eslatmasi so'zlarni majburiy tuzatishni taqiqlaydi")


def test_score_to_status_color_mapping():
    cases = [(1, "bad", "red"), (3, "bad", "red"), (4, "average", "yellow"),
             (6, "average", "yellow"), (7, "good", "green"), (10, "good", "green")]
    for score, exp_status, exp_color in cases:
        raw = json.dumps({
            "overview": "x", "score": score, "normalizedTranscript": [],
            "customerRequest": {}, "operatorMistakes": [], "positivePoints": [],
            "saleResult": "unknown", "callbackRequired": False, "recommendedResponse": "",
        })
        parsed = ca._parse_analysis_json(raw)
        assert parsed["status"] == exp_status and parsed["color"] == exp_color, (score, parsed)
    print("OK: score -> status/color deterministik xaritalash to'g'ri (barcha diapazonlar)")


def test_score_clamped_and_defaults_on_bad_value():
    raw = json.dumps({
        "overview": "x", "score": 99, "normalizedTranscript": [],
        "customerRequest": {}, "operatorMistakes": [], "positivePoints": [],
        "saleResult": "unknown", "callbackRequired": False, "recommendedResponse": "",
    })
    parsed = ca._parse_analysis_json(raw)
    assert parsed["score"] == 10, parsed

    raw2 = json.dumps({"overview": "x", "score": "noaniq"})
    parsed2 = ca._parse_analysis_json(raw2)
    assert parsed2["score"] == 5, parsed2  # noto'g'ri qiymatda xavfsiz standart
    print("OK: score chegaralanadi (1-10) va noto'g'ri qiymatda standart 5ga tushadi")


def test_never_trusts_model_status_even_if_present():
    # Model o'zi "status": "good" desa ham, score=2 bo'lsa server "bad"/"red"ni majburlaydi.
    raw = json.dumps({
        "overview": "x", "score": 2, "status": "good", "color": "green",
        "normalizedTranscript": [], "customerRequest": {}, "operatorMistakes": [],
        "positivePoints": [], "saleResult": "unknown", "callbackRequired": False,
        "recommendedResponse": "",
    })
    parsed = ca._parse_analysis_json(raw)
    assert parsed["status"] == "bad" and parsed["color"] == "red", parsed
    print("OK: modelning o'z status/color'iga ISHONILMAYDI -- score'dan qayta hisoblanadi")


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


def test_diarize_segments_confident_vs_not():
    # Aynan 2 gapiruvchi -- Manager/Mijoz deb ISHONCH bilan belgilanadi.
    segs_2 = [{"speaker": "A", "text": "salom"}, {"speaker": "B", "text": "assalom"}]
    text, confident = ca._build_labeled_transcript_from_segments(segs_2)
    assert confident is True
    assert text.startswith("Manager:")

    # 3 xil "gapiruvchi" (masalan diarizatsiya xatosi/shovqin ajratib yuborgan) --
    # ISHONCHSIZ deb belgilanadi, Manager/Mijoz deb NOTO'G'RI ishonch bilan
    # belgilanmaydi -- "Speaker N" saqlanadi.
    segs_3 = [{"speaker": "A", "text": "salom"}, {"speaker": "B", "text": "assalom"}, {"speaker": "C", "text": "?"}]
    text3, confident3 = ca._build_labeled_transcript_from_segments(segs_3)
    assert confident3 is False
    assert "Speaker 1" in text3 and "Manager" not in text3
    print("OK: diarizatsiya ishonch mezoni (2 gapiruvchi=ishonchli, 3+=Speaker N) to'g'ri ishlaydi")


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


def test_result_summary_includes_sale_result_and_callback():
    summary = ca._build_result_summary({
        "saleResult": "sold", "callbackRequired": True, "recommendedResponse": "Ertaga qo'ng'iroq qil.",
    })
    assert "Sotildi" in summary
    assert "qayta bog'lanish" in summary
    assert "Ertaga qo'ng'iroq qil." in summary
    print("OK: ai_result xulosasi saleResult/callback/tavsiyani to'g'ri birlashtiradi")


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


def test_analysis_json_schema_shape():
    schema = ca._ANALYSIS_JSON_SCHEMA
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    for key in ["overview", "score", "normalizedTranscript", "customerRequest",
                "operatorMistakes", "positivePoints", "saleResult",
                "callbackRequired", "recommendedResponse"]:
        assert key in schema["required"], f"'{key}' schema.required'da yo'q"
        assert key in schema["properties"], f"'{key}' schema.properties'da yo'q"
    assert schema["properties"]["saleResult"]["enum"] == ["sold", "lost", "pending", "unknown"]
    print("OK: tahlil JSON Schema shakli spesifikatsiyaga mos (barcha maydonlar, enum'lar)")


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
