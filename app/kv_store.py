"""
kv_store.py — eski Vercel KV/Upstash wrapper'ining Postgres'ga ko'chirilgan
versiyasi. `orchestrator.py` va `budget_tracker.py` shu modulni
`get_json`/`set_json` orqali chaqiradi -- interfeys ATAYLAB bir xil
qoldirilgan, shuning uchun o'sha ikki fayl DEYARLI o'zgarishsiz ishlayveradi.

Render'da bu doimiy jarayon bo'lgani uchun tashqi Redis/KV xizmatiga
ehtiyoj yo'q -- shu Postgres bazadagi oddiy `kv_store` jadvali yetarli.
"""

import json

from db import KVEntry, get_session


def get_json(key: str, default=None):
    session = get_session()
    try:
        row = session.get(KVEntry, key)
        if row is None:
            return default
        try:
            return json.loads(row.value)
        except (TypeError, json.JSONDecodeError):
            return default
    finally:
        session.close()


def set_json(key: str, value) -> None:
    session = get_session()
    try:
        raw = json.dumps(value, ensure_ascii=False)
        row = session.get(KVEntry, key)
        if row is None:
            row = KVEntry(key=key, value=raw)
            session.add(row)
        else:
            row.value = raw
        session.commit()
    finally:
        session.close()
