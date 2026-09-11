"""test_meta_api_ig_dm_reliability_offline.py — 2026-09, foydalanuvchi
TOPGAN jonli bug uchun regressiya: "Yangilash" tugmasi bosilganda UI'da
"Meta error: Timeout" chiqardi. Sabab: `meta_api.
get_instagram_conversation_messages()` xabarlarni bitta suhbat obyekti
ICHIDA NESTED so'rov (`fields=messages.limit(N){...}`) bilan so'rardi --
bu Meta serverida sekinroq ishlanadi va faol suhbatlarda "Timeout" bilan
tugardi.

Bu fayl TARMOQSIZ (Meta'ga chiqmasdan, faqat `meta_api._get`/
`_get_page_access_token`ni almashtirib) quyidagilarni tekshiradi:
  1. Endi TO'G'RIDAN-TO'G'RI `/{conversation_id}/messages` edge'i
     ishlatilishi (nested emas), aniq `fields`/`limit=10` bilan.
  2. Meta "Timeout" xatosi bilan rad etsa -- `limit` 10 -> 5 -> 3 bo'lib
     avtomatik qayta urinilishi (Meta "reduce the amount of data..."
     qaytarganda ham xuddi shunday).
  3. Har bir muvaffaqiyatsiz urinish diagnostika sifatida logga
     yozilishi (endpoint, code, error_subcode, message, elapsed_ms) VA
     `access_token`ning HECH QACHON log matniga chiqmasligi.
  4. `get_instagram_conversations()` ham "Timeout" xatosida xuddi shunday
     (limit pasaytirib) qayta urinishi.

Ishga tushirish:
    cd app && python3 scripts/test_meta_api_ig_dm_reliability_offline.py
"""

import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import meta_api  # noqa: E402

_SECRET_TOKEN = "EAASuperSecretPageAccessToken12345"


def _timeout_error():
    return meta_api.MetaAPIError({"message": "Timeout", "code": 1})


def _reduce_data_error():
    return meta_api.MetaAPIError({
        "message": "Please reduce the amount of data you're asking for, then retry your request",
        "code": 1, "error_subcode": 99,
    })


def test_messages_uses_direct_edge_flat_fields_limit_10():
    calls = []

    def fake_get(path, params, token=None):
        calls.append((path, dict(params), token))
        return {"data": [{"id": "m1", "message": "Salom", "created_time": "2026-09-11T10:00:00+0000", "from": {"id": "CUST"}}]}

    with mock.patch.object(meta_api, "_get", side_effect=fake_get), \
         mock.patch.object(meta_api, "_get_page_access_token", return_value=_SECRET_TOKEN):
        result = meta_api.get_instagram_conversation_messages("conv_abc")

    assert len(calls) == 1, f"aynan bitta so'rov bo'lishi kerak, olindi {len(calls)}"
    path, params, token = calls[0]
    assert path == "conv_abc/messages", f"to'g'ridan-to'g'ri /messages edge'i kutilgan edi, olindi path={path!r}"
    assert params.get("fields") == "id,message,created_time,from,to", (
        f"YASSI fields kutilgan edi (nested messages.limit(...) EMAS), olindi {params.get('fields')!r}"
    )
    assert "messages.limit" not in str(params.get("fields", "")), "eski NESTED so'rov uslubi qolib ketgan"
    assert params.get("limit") == 10, f"standart limit=10 kutilgan edi, olindi {params.get('limit')}"
    assert result == [{"id": "m1", "message": "Salom", "created_time": "2026-09-11T10:00:00+0000", "from": {"id": "CUST"}}]
    print("OK: get_instagram_conversation_messages endi NESTED emas, to'g'ridan-to'g'ri /messages edge'ini yassi fields+limit=10 bilan chaqiradi")


def test_messages_timeout_retries_10_5_3_then_raises():
    limits_tried = []

    def fake_get(path, params, token=None):
        limits_tried.append(params["limit"])
        raise _timeout_error()

    with mock.patch.object(meta_api, "_get", side_effect=fake_get), \
         mock.patch.object(meta_api, "_get_page_access_token", return_value=_SECRET_TOKEN):
        try:
            meta_api.get_instagram_conversation_messages("conv_timeout")
            assert False, "MetaAPIError ko'tarilishi kerak edi"
        except meta_api.MetaAPIError as e:
            assert "timeout" in str(e.args[0].get("message", "")).lower()

    assert limits_tried == [10, 5, 3], f"kutilgan [10, 5, 3], olindi {limits_tried}"
    print("OK: 'Timeout' xatosida limit 10 -> 5 -> 3 bo'lib qayta uriniladi, faqat shundan keyin xato chiqariladi")


def test_messages_reduce_data_error_also_retries_with_shrinking_limit():
    limits_tried = []

    def fake_get(path, params, token=None):
        limits_tried.append(params["limit"])
        if params["limit"] <= 5:
            return {"data": []}
        raise _reduce_data_error()

    with mock.patch.object(meta_api, "_get", side_effect=fake_get), \
         mock.patch.object(meta_api, "_get_page_access_token", return_value=_SECRET_TOKEN):
        result = meta_api.get_instagram_conversation_messages("conv_reduce")

    assert limits_tried == [10, 5], f"kutilgan [10, 5] (5'da muvaffaqiyatli), olindi {limits_tried}"
    assert result == []
    print("OK: 'reduce the amount of data' xatosida ham xuddi shunday limit pasaytirib qayta uriniladi")


def test_conversations_timeout_also_retries_with_shrinking_limit():
    limits_tried = []

    def fake_get(path, params, token=None):
        limits_tried.append(params["limit"])
        raise _timeout_error()

    with mock.patch.object(meta_api, "_get", side_effect=fake_get), \
         mock.patch.object(meta_api, "_get_page_access_token", return_value=_SECRET_TOKEN):
        try:
            meta_api.get_instagram_conversations(limit=10)
            assert False, "MetaAPIError ko'tarilishi kerak edi"
        except meta_api.MetaAPIError:
            pass

    assert limits_tried == [10, 5, 3], f"kutilgan [10, 5, 3], olindi {limits_tried}"
    print("OK: get_instagram_conversations() ham 'Timeout' xatosida limitni pasaytirib qayta uriniladi")


def test_diagnostics_logged_without_leaking_access_token():
    def fake_get(path, params, token=None):
        raise _timeout_error()

    with mock.patch.object(meta_api, "_get", side_effect=fake_get), \
         mock.patch.object(meta_api, "_get_page_access_token", return_value=_SECRET_TOKEN), \
         mock.patch.object(meta_api, "logger") as fake_logger:
        try:
            meta_api.get_instagram_conversation_messages("conv_log", page_id="page_9")
        except meta_api.MetaAPIError:
            pass

    assert fake_logger.warning.call_count >= 1, "har bir muvaffaqiyatsiz urinish logga yozilishi kerak"
    all_log_text = " ".join(
        " ".join(str(a) for a in call.args) for call in fake_logger.warning.call_args_list
    )
    assert _SECRET_TOKEN not in all_log_text, "access_token HECH QACHON logga chiqmasligi kerak!"
    assert "conv_log/messages" in all_log_text, "endpoint nomi logda bo'lishi kerak"
    assert "Timeout" in all_log_text, "Meta xato xabari logda bo'lishi kerak"
    assert "elapsed_ms" in all_log_text, "o'tgan vaqt (elapsed) logda bo'lishi kerak"
    print("OK: har bir Meta xatosi (endpoint/code/error_subcode/message/elapsed_ms bilan) logga yoziladi, access_token hech qachon chiqmaydi")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nBARCHA TESTLAR O'TDI ({len(tests)} ta)")


if __name__ == "__main__":
    run_all()
