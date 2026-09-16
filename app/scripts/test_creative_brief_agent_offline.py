"""test_creative_brief_agent_offline.py — Kreativ studiya: AI BRIF AGENTI
(2026-09, haqiqiy foydalanuvchi fikri -- "armatura" desa statik "Rasm
qanday ko'rinishda bo'lsin?" emas, "qaysi diametr, narxi qancha" kabi
ANIQ savol kutilgan edi: "savollar bir xil shablon bo'lmasin, agent
ishlasin"). `creative_brief_agent.py` -- LLM (`orchestrator._call_agent`)
MOCK qilinadi (`test_ai_campaign_planner_offline.py`dagi naqsh bilan):

  1. Bo'sh suhbat -> birinchi savol (LLM "done: false" qaytaradi).
  2. Ko'p burilishli suhbat -> "done: true", `brief`da 5 ta kalit ham bor.
  3. QATTIQ XAVFSIZLIK TO'RI: LLM "done: true" desa ham telefon (yoki
     mahsulot fokusi) bo'sh bo'lsa -- tizim buni RAD ETADI, ESKI statik
     savol matnini (`creative_studio._BRIEF_BY_KEY`) qaytaradi.
  4. Savollar chegarasi (5 ta): 6-chaqiruvda LLM "done: false" desa ham
     MAJBURIY yakunlanadi (best-effort `brief`) -- MAJBURIY maydon hali
     yo'q bo'lsagina BITTA qo'shimcha savolga ruxsat beriladi.
  5. LLM ishlamasa (`AgentUnavailableError`/`TargetologFormatError`) --
     suhbat HECH QACHON qulflanib qolmaydi: ESKI statik savol yoki
     (agar suhbatdan hammasi allaqachon chiqarilsa) best-effort "done".

Ishga tushirish:
    cd app && python3 scripts/test_creative_brief_agent_offline.py
"""
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("META_ACCESS_TOKEN", "test-dummy-token")
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_test_dummy")
os.environ.setdefault("META_PAGE_ID", "page_test_dummy")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import orchestrator  # noqa: E402
import creative_studio  # noqa: E402
import creative_brief_agent as agent  # noqa: E402

failures = []


def check(name, cond):
    print(("OK  " if cond else "FAIL") + " " + name)
    if not cond:
        failures.append(name)


# CTX -- telefon YO'Q, profilda mahsulot YO'Q -- ikkalasi ham majburiy
# (creative_studio.missing_questions() bilan bir xil shartlar).
CTX_BARE = {
    "company_id": 1, "company_name": "Nur Armatura", "phone": None,
    "business_category": {"key": "construction", "label": "Qurilish materiallari"},
    "profile_answers": {"product_or_service": "", "target_audience": ""},
    "missing_fields": ["product_or_service"],
    "meta_assets": {}, "recent_performance": {},
}

# CTX -- telefon VA mahsulot ALLAQACHON bor -- qattiq to'r ishga tushmaydi.
CTX_FULL = {
    "company_id": 2, "company_name": "Oq Uy Mebel", "phone": "+998 90 111 22 33",
    "business_category": {"key": "furniture_interior", "label": "Mebel / interyer"},
    "profile_answers": {"product_or_service": "Oshxona mebeli", "target_audience": "25-45 yosh"},
    "missing_fields": [],
    "meta_assets": {}, "recent_performance": {},
}


def _agent_turn(text):
    return {"role": "agent", "text": text}


def _user_turn(text):
    return {"role": "user", "text": text}


def test_first_question():
    with mock.patch.object(orchestrator, "_call_agent", return_value={"done": False, "question": "Qaysi diametr/turdagi armatura, narxi qancha?", "placeholder": "Masalan: 10mm, 12mm"}) as m:
        step = agent.next_step(CTX_BARE, [])
    check("LLM 1 marta chaqirildi", m.call_count == 1)
    check("birinchi savol qaytdi", step["done"] is False and "diametr" in step["question"].lower())
    check("placeholder ham qaytdi", step["placeholder"] == "Masalan: 10mm, 12mm")
    check("brief hali None", step["brief"] is None)
    # Prompt kompaniya kontekstini o'z ichiga oladi (armatura misoli emas,
    # lekin kompaniya nomi bo'lishi kerak)
    check("prompt kompaniya nomini o'z ichiga oladi", "Nur Armatura" in m.call_args[0][1])
    check("bo'sh suhbat -> promptda shunga mos izoh", "BIRINCHI" in m.call_args[0][1])


def test_multi_turn_done_all_keys():
    conversation = [
        _agent_turn("Aynan qaysi mahsulotga urg'u berilsin?"),
        _user_turn("Armatura"),
        _agent_turn("Qaysi diametr/turdagi armatura, narxi qancha?"),
        _user_turn("12mm, GOST 5781, 12500 so'm/kg"),
        _agent_turn("Mijozlar bog'lanadigan telefon raqamingiz?"),
        _user_turn("+998 90 555 44 33"),
    ]
    brief = {"focus": "Armatura 12mm, GOST 5781", "offer_text": "", "cta_preference": "", "style_notes": "", "phone": "+998 90 555 44 33"}
    with mock.patch.object(orchestrator, "_call_agent", return_value={"done": True, "brief": brief}):
        step = agent.next_step(CTX_BARE, conversation)
    check("done true", step["done"] is True)
    check("brief 5 ta kalitning barchasini o'z ichiga oladi", set(step["brief"].keys()) == {"focus", "offer_text", "cta_preference", "style_notes", "phone"})
    check("focus to'g'ri o'tdi", "Armatura" in step["brief"]["focus"])
    check("telefon normallashtirildi", step["brief"]["phone"].startswith("+998"))
    check("question/placeholder None (done bo'lganda)", step["question"] is None and step["placeholder"] is None)


def test_hard_safety_net_phone():
    # LLM "done: true" deydi, lekin telefon bo'sh -- tizim buni rad etishi kerak.
    conversation = [_agent_turn("Mahsulot?"), _user_turn("Armatura")]
    brief = {"focus": "Armatura", "offer_text": "", "cta_preference": "", "style_notes": "", "phone": ""}
    with mock.patch.object(orchestrator, "_call_agent", return_value={"done": True, "brief": brief}):
        step = agent.next_step(CTX_BARE, conversation)
    expected_q = creative_studio._BRIEF_BY_KEY["phone"][1]
    check("qattiq to'r: telefon yo'q -> done overridden to False", step["done"] is False)
    check("qattiq to'r: ESKI statik telefon savoli qaytdi", step["question"] == expected_q)


def test_hard_safety_net_focus():
    # Telefon bor (CTX_BARE'da yo'q, shuning uchun conversationda beramiz),
    # lekin focus bo'sh -- mahsulot ham profilda yo'q -> majburiy.
    conversation = [_agent_turn("Telefon raqamingiz?"), _user_turn("+998 90 000 00 00")]
    brief = {"focus": "", "offer_text": "", "cta_preference": "", "style_notes": "", "phone": "+998 90 000 00 00"}
    with mock.patch.object(orchestrator, "_call_agent", return_value={"done": True, "brief": brief}):
        step = agent.next_step(CTX_BARE, conversation)
    expected_q = creative_studio._BRIEF_BY_KEY["focus"][1]
    check("qattiq to'r: focus yo'q -> done overridden to False", step["done"] is False)
    check("qattiq to'r: ESKI statik focus savoli qaytdi", step["question"] == expected_q)


def test_hard_safety_net_not_triggered_when_full():
    # CTX_FULL -- profilda mahsulot va telefon bor -- LLM "done" desa,
    # brief'da bu ikkalasi bo'sh bo'lsa ham qattiq to'r ISHGA TUSHMAYDI.
    with mock.patch.object(orchestrator, "_call_agent", return_value={"done": True, "brief": {"focus": "", "offer_text": "-20%", "cta_preference": "", "style_notes": "", "phone": ""}}):
        step = agent.next_step(CTX_FULL, [_agent_turn("q"), _user_turn("a")])
    check("profilda mahsulot+telefon bo'lsa qattiq to'r ishga tushmaydi", step["done"] is True)


def test_turn_cap_forces_completion():
    # 5 ta agent savoli allaqachon berilgan, ikkalasi ham majburiy maydon
    # (CTX_FULL -- majburiy emas) SUHBATDA allaqachon bor -> LLM "done:
    # false" desa ham MAJBURIY yakunlanadi.
    conversation = []
    for i in range(5):
        conversation.append(_agent_turn(f"savol {i}"))
        conversation.append(_user_turn(f"javob {i}"))
    with mock.patch.object(orchestrator, "_call_agent", return_value={"done": False, "question": "yana savol?", "placeholder": None}) as m:
        step = agent.next_step(CTX_FULL, conversation)
    check("5 savoldan keyin LLM 'false' desa ham majburiy yakunlandi", step["done"] is True)
    check("brief best-effort (birinchi javob -- focus)", step["brief"]["focus"] == "javob 0")
    check("LLM baribir 1 marta chaqirildi", m.call_count == 1)

    # Endi majburiy maydon (telefon) HALI yo'q -- cheklovga qaramay BITTA
    # qo'shimcha (deterministik) savolga ruxsat berilishi kerak.
    with mock.patch.object(orchestrator, "_call_agent", return_value={"done": False, "question": "yana savol?", "placeholder": None}):
        step2 = agent.next_step(CTX_BARE, conversation)
    check("cheklovda ham majburiy maydon yo'q bo'lsa -- BITTA qo'shimcha savolga ruxsat", step2["done"] is False)
    check("bu qo'shimcha savol ESKI statik (focus yoki phone)", step2["question"] in (creative_studio._BRIEF_BY_KEY["focus"][1], creative_studio._BRIEF_BY_KEY["phone"][1]))


def test_llm_failure_fallback():
    # Bo'sh suhbat + LLM ikkala provayder ham ishlamadi -> qulflanib
    # qolmaydi, ESKI statik savol (focus -- CTX_BARE'da mahsulot yo'q).
    with mock.patch.object(orchestrator, "_call_agent", side_effect=orchestrator.AgentUnavailableError("down")):
        step = agent.next_step(CTX_BARE, [])
    check("LLM ishlamasa -> qulflanmaydi, statik focus savoli", step["done"] is False and step["question"] == creative_studio._BRIEF_BY_KEY["focus"][1])

    # Suhbatda allaqachon fokus+telefon bor bo'lsa (foydalanuvchi
    # javoblaridan chiqarib olinadi) -- LLM ishlamasa ham "done: true"
    # best-effort brief bilan (bloklanib qolmaydi).
    conversation = [_agent_turn("Mahsulot?"), _user_turn("Armatura, 12mm"), _agent_turn("Telefon?"), _user_turn("+998 90 777 66 55")]
    with mock.patch.object(orchestrator, "_call_agent", side_effect=orchestrator.TargetologFormatError("erkin matn")):
        step2 = agent.next_step(CTX_BARE, conversation)
    check("LLM ishlamasa lekin hammasi suhbatdan chiqsa -> done true", step2["done"] is True)
    check("best-effort focus = birinchi javob", step2["brief"]["focus"] == "Armatura, 12mm")
    check("best-effort phone topildi", step2["brief"]["phone"].startswith("+998"))


test_first_question()
test_multi_turn_done_all_keys()
test_hard_safety_net_phone()
test_hard_safety_net_focus()
test_hard_safety_net_not_triggered_when_full()
test_turn_cap_forces_completion()
test_llm_failure_fallback()

print()
if failures:
    print(f"MUVAFFAQIYATSIZ: {len(failures)} ta test o'tmadi:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("BARCHA TESTLAR O'TDI (creative_brief_agent)")
