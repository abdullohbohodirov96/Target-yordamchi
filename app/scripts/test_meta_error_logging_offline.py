"""test_meta_error_logging_offline.py -- docs/PLAN.md, 2-bosqich:
"Meta'ning har bir javobi va xatosini log qilish, foydalanuvchiga tushunarli
ko'rsatish". Tarmoqsiz (`requests` almashtiriladi).
  1. Har bir Meta chaqiruvi bitta qator log: yo'l, HTTP status, vaqt;
     xatoda -- code, subcode, fbtrace_id, xabar. Token logga TUSHMAYDI.
  2. Keng tarqalgan xatolar o'zbekcha tushunarli matn + qisqa kod bilan.
  3. Tanilmagan xato -- Meta'ning o'z xabari + kod (xom JSON emas).

Ishga tushirish:
    cd app && python3 scripts/test_meta_error_logging_offline.py
"""

import logging
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import meta_api  # noqa: E402

meta_api._retry_sleep = lambda attempt: None
SECRET = "EAA_SUPER_SECRET_TOKEN_123"
failures = []


def check(name, cond, detail=""):
    print(("OK:   " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append((record.levelno, record.getMessage()))


def run_all():
    cap = _Capture()
    meta_api.logger.addHandler(cap)
    meta_api.logger.setLevel(logging.DEBUG)

    err = {"message": "Invalid parameter", "type": "OAuthException", "code": 100, "error_subcode": 4834011,
           "fbtrace_id": "AbCdTrace", "error_user_title": "Budget sharing"}
    with mock.patch.object(meta_api.requests, "post", return_value=_Resp({"error": err}, 400)):
        try:
            meta_api.create_campaign("X", "OUTCOME_LEADS", "PAUSED", [], access_token=SECRET, ad_account_id="act_9")
            check("xato ko'tarildi", False)
        except meta_api.MetaAPIError as e:
            friendly = meta_api.safe_error_message(e)
    line = next((m for lvl, m in cap.records if "META POST" in m), "")
    check("xato log qatori yozildi (WARNING)", any(lvl == logging.WARNING and "META POST act_9/campaigns" in m for lvl, m in cap.records), line)
    check("logda code/subcode/fbtrace_id bor", "code=100" in line and "subcode=4834011" in line and "fbtrace_id=AbCdTrace" in line, line)
    check("byudjet-bo'lishish xatosi o'zbekcha", "byudjet" in friendly.lower() and "kod 100/4834011" in friendly, friendly)

    cap.records.clear()
    with mock.patch.object(meta_api.requests, "post", return_value=_Resp({"id": "120000111"})):
        meta_api.create_campaign("X", "OUTCOME_LEADS", "PAUSED", [], access_token=SECRET, ad_account_id="act_9")
    check("muvaffaqiyatli yozuv INFO bilan id log qilinadi", any(lvl == logging.INFO and "ok id=120000111" in m for lvl, m in cap.records), str(cap.records))

    cap.records.clear()
    with mock.patch.object(meta_api.requests, "get", return_value=_Resp({"data": []})):
        meta_api._get("act_9/campaigns", {"fields": "id"}, token=SECRET)
    check("o'qish DEBUG bilan log qilinadi", any(lvl == logging.DEBUG and "META GET act_9/campaigns" in m for lvl, m in cap.records))

    with mock.patch.object(meta_api.requests, "get", side_effect=meta_api.requests.exceptions.ConnectionError(f"https://graph.facebook.com/x?access_token={SECRET}")):
        try:
            meta_api._get("act_9/insights", {}, token=SECRET)
        except Exception as e:
            check("tarmoq xatosi -- foydalanuvchiga tokensiz umumiy matn", SECRET not in meta_api.safe_error_message(e))
    check("HECH BIR log qatorida token yo'q", all(SECRET not in m for _l, m in cap.records))

    cases = [
        ({"code": 190, "message": "Error validating access token"}, "qayta ulang"),
        ({"code": 17, "message": "User request limit reached"}, "chegara"),
        ({"code": 200, "message": "Permissions error"}, "ruxsati"),
        ({"code": 100, "error_subcode": 1487079, "message": "Invalid targeting spec"}, "targeting"),
        ({"code": 2, "message": "Service temporarily unavailable", "is_transient": True}, "vaqtinchalik"),
    ]
    for err, needle in cases:
        msg = meta_api.safe_error_message(meta_api.MetaAPIError(err))
        check(f"kod {err['code']} -> '{needle}'", needle in msg.lower() and f"kod {err['code']}" in msg, msg)
    unknown = meta_api.safe_error_message(meta_api.MetaAPIError({"code": 99999, "message": "Something odd"}))
    check("tanilmagan xato -- Meta xabari + kod", unknown == "Something odd (kod 99999)", unknown)

    if failures:
        print(f"\n{len(failures)} ta XATO")
        sys.exit(1)
    print("\nBARCHA TESTLAR O'TDI")


if __name__ == "__main__":
    run_all()
