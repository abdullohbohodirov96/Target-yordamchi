"""So'nggi qidiruvlarda ko'rilgan reklamalar (jarayon xotirasida, 6 soat).

"AI Analyze" mijoz yuborgan ixtiyoriy matnni emas, faqat SERVER o'zi
qidiruvda olgan reklamani tahlil qiladi -- shu bilan AI tokenini begona matn
bilan sarflash imkoni yo'q. Har bir kompaniya faqat o'z qidiruvi natijasini
tahlil qila oladi."""

from __future__ import annotations

import threading
import time

from .provider import Ad

TTL_SECONDS = 6 * 3600
MAX_ITEMS = 3000

_lock = threading.Lock()
_items: dict = {}


def remember(company_id, ads: list[Ad]) -> None:
    now = time.time()
    with _lock:
        for ad in ads:
            _items[(company_id, ad.id)] = (now, ad)
        if len(_items) > MAX_ITEMS:
            for k in sorted(_items, key=lambda k: _items[k][0])[: len(_items) - MAX_ITEMS]:
                _items.pop(k, None)


def get(company_id, ad_id: str) -> "Ad | None":
    with _lock:
        hit = _items.get((company_id, str(ad_id)))
    if not hit or time.time() - hit[0] > TTL_SECONDS:
        return None
    return hit[1]
