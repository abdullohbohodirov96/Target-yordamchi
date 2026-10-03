"""guide_content.py -- "Qo'llanma" sahifasi (/qollanma) matnlari, 3 tilda.

2026-10-01, PLAN 7-bosqich. Har bo'lim: id, sarlavha, kimga (owner/targetolog/
hammaga), qadamlar ro'yxati va (ixtiyoriy) ilova ichidagi sahifa endpoint'i.
Matn faqat shu yerda -- sahifa shabloni (templates/guide.html) uni chizadi.
"""

# (endpoint, label kaliti) -- login qilgan foydalanuvchiga "Ochish" tugmasi.
SECTIONS = [
    {"id": "boshlash", "audience": "all", "endpoint": "signup"},
    {"id": "meta", "audience": "all", "endpoint": "connect_accounts"},
    {"id": "telegram", "audience": "all", "endpoint": "connect_accounts"},
    {"id": "lidlar", "audience": "owner", "endpoint": "leads_list"},
    {"id": "cpl", "audience": "all", "endpoint": "settings_cpl"},
    {"id": "avtopilot", "audience": "targetolog", "endpoint": "autopilot_list"},
    {"id": "kreativ", "audience": "targetolog", "endpoint": "creative_list"},
    {"id": "raqobatchilar", "audience": "all", "endpoint": "ig_benchmark_page"},
    {"id": "kpi", "audience": "owner", "endpoint": "settings_general"},
    {"id": "tolov", "audience": "owner", "endpoint": "payment_page"},
]

TEXT = {
    "uz": {
        "title": "Qo'llanma",
        "subtitle": "Replix'ni 15 daqiqada ishga tushiring: qadamma-qadam yo'riqnoma. Savol qolsa — ilovadagi AI-yordamchiga yozing.",
        "aud_all": "Hammaga", "aud_owner": "Biznes egasi", "aud_targetolog": "Targetolog",
        "open": "Ochish", "toc": "Mundarija",
        "boshlash": ("Boshlash", [
            "replix.uz → \"Bepul boshlash\". Kompaniya nomi, login va parol kiriting — {trial_days} kun bepul, karta shart emas.",
            "Keyingi qadamda biznesingiz haqida 4–5 ta savolga javob bering (mahsulot, auditoriya, narx). AI reklama matni va tahlilni shunga moslaydi.",
            "Sinov tugashidan oldin Tarif va to'lov sahifasida tarif tanlaysiz.",
        ]),
        "meta": ("Facebook / Instagram (Meta) ulash", [
            "Chap menyu → Connection (Akkauntlarni ulash) → \"Facebook orqali ulash\".",
            "Facebook oynasida biznes sahifangiz, Instagram akkauntingiz va reklama hisobingizni belgilang, ruxsat bering.",
            "Bitta sahifa/hisob bo'lsa hammasi avtomatik saqlanadi; bir nechta bo'lsa — kerakligini tanlaysiz. Pixel ham o'zi topiladi.",
            "Tayyor: Dashboard va Target sahifasida xarajat, lid va CPL ko'rinadi; reklamadan kelgan lidlar CRM'ga o'zi tushadi.",
        ]),
        "telegram": ("Telegram bot va guruh", [
            "Akkauntlarni ulash sahifasi → \"👥 Guruhga qo'shish (bir tugma)\". Telegram ochiladi.",
            "Ish guruhingizni tanlang va botni qo'shing — guruh kompaniyangizga o'zi bog'lanadi.",
            "\"📱 Telegramda shaxsan ulash\" — har bir menejer o'z eslatmalarini shaxsiy chatda oladi (Mening profilim sahifasidan ham).",
            "Guruhga keladi: kunlik hisobot, CPL ogohlantirish/pauza, yangi lidlar. Botga savol ham yozish mumkin (@bot nomi bilan).",
        ]),
        "lidlar": ("Lidlar va CRM", [
            "Lidlar sahifasi — barcha mijozlar: manba, holat, mas'ul menejer.",
            "Lidni oching → holatini o'zgartiring (yangi → aloqa qilindi → sifatli → sotildi). Sotuv summasini kiriting — KPI va ROI shundan.",
            "Qayta aloqa sahifasida bugun kimga qo'ng'iroq qilish kerakligi ko'rinadi; muddati o'tganlar haqida admin xabar oladi.",
        ]),
        "cpl": ("CPL himoyasi (lid narxi)", [
            "Sozlamalar → Target avtomatik o'chirish (CPL): maqbul CPL, \"o'chirish\" chegarasi va minimal xarajatni kiriting (dollarda).",
            "Rejimni tanlang: \"Faqat ogohlantirish\" (targetologlar uchun) yoki \"Avtomatik pauza\" (biznes egalari uchun, tavsiya).",
            "Har 15 daqiqada tekshiriladi; kuniga ko'pi bilan 10 ta reklama avtomatik o'chiriladi.",
            "Noto'g'ri o'chgan reklamani Dashboard'dagi \"Qayta yoqish\" tugmasi bilan yoqing — unga 7 kun tegilmaydi.",
        ]),
        "avtopilot": ("Avtopilot (AI target)", [
            "Avtopilot → \"Yangi kampaniya\". AI bir nechta savol beradi (maqsad, byudjet, hudud).",
            "AI reja tuzadi: auditoriya, byudjet, matn. Tekshirib, kerak bo'lsa tahrirlang.",
            "\"Meta'ga nashr qilish\" — kampaniya PAUZADA yaratiladi, pul sarflanmaydi. Ads Manager'da tekshirib, \"Faollashtirish\"ni o'zingiz bosasiz.",
        ]),
        "kreativ": ("Kreativ studiya", [
            "Kreativ studiya → shablon tanlang (1:1, 4:5, 9:16). Brend ranglari va logotip avtomatik qo'yiladi.",
            "Matn, narx va tugmani yozing — AI faqat fonni chizadi, matn aniq va o'qiladigan bo'ladi.",
            "Karusel: 3–6 karta, 6 xil uslub; ZIP qilib yuklab olasiz.",
        ]),
        "raqobatchilar": ("Raqobatchilar tahlili", [
            "Raqobatchilar → Instagram tahlil. Username yozing (vergul bilan 5 tagacha): masalan dunyabunya, buxoro_maktabi.",
            "So'nggi 12–30 post bo'yicha o'rtacha like, komment, video ko'rishlar, ER va post chastotasi; eng yaxshi ko'rsatkich yashil.",
            "\"Ad Library'da ko'rish\" — raqobatchining hozirgi reklamalari, matni, rasmi va qachondan ishlayotgani (Meta rasmiy sahifasi).",
            "Faqat ochiq Business/Creator akkauntlar ko'rinadi; buning uchun o'z Instagram'ingiz ulangan bo'lishi kerak.",
        ]),
        "kpi": ("Menejerlar KPI va bonus", [
            "Sozlamalar → Umumiy → \"KPI va bonus qoidalari\": oklad, xarid bonuslari, foiz, oylik reja, sotuv va oborot bosqichlari.",
            "Analitika → KPI: har menejerning shu oydagi oyligi, bonuslari va keyingi bosqichgacha qancha qolgani.",
            "Oy o'rtasida ishga kirgan menejerga reja avtomatik kamaytiriladi (ishlagan kunlariga qarab).",
        ]),
        "tolov": ("Tarif va to'lov", [
            "Sozlamalar → Tarif va to'lov. Har tarif yonida dollar va Payme yechadigan so'm summasi yozilgan.",
            "Payme kartasini ulasangiz har oy avtomatik to'lanadi; yoki karta orqali o'tkazib \"To'lov qildim\" bosasiz.",
            "Tarifni istalgan vaqtda oshirish mumkin; ma'lumotlaringiz saqlanib qoladi.",
        ]),
    },
    "ru": {
        "title": "Инструкция",
        "subtitle": "Запустите Replix за 15 минут: пошаговое руководство. Остались вопросы — напишите AI-ассистенту в приложении.",
        "aud_all": "Для всех", "aud_owner": "Владелец бизнеса", "aud_targetolog": "Таргетолог",
        "open": "Открыть", "toc": "Содержание",
        "boshlash": ("Начало", [
            "replix.uz → «Начать бесплатно». Название компании, логин и пароль — {trial_days} дней бесплатно, карта не нужна.",
            "Далее ответьте на 4–5 вопросов о бизнесе (продукт, аудитория, цены) — AI подстроит тексты и анализ.",
            "До конца пробного периода выберите тариф на странице «Тариф и оплата».",
        ]),
        "meta": ("Подключение Facebook / Instagram (Meta)", [
            "Левое меню → Подключения → «Подключить через Facebook».",
            "В окне Facebook отметьте страницу, Instagram и рекламный аккаунт, выдайте доступ.",
            "Если страница/аккаунт один — всё сохранится автоматически; если несколько — выберете нужный. Pixel находится сам.",
            "Готово: на дашборде и в Таргете видны расход, лиды и CPL; лиды из рекламы сами попадают в CRM.",
        ]),
        "telegram": ("Telegram-бот и группа", [
            "Страница «Подключение аккаунтов» → «👥 Добавить в группу (одна кнопка)». Откроется Telegram.",
            "Выберите рабочую группу и добавьте бота — группа привяжется к компании автоматически.",
            "«📱 Подключить лично в Telegram» — каждый менеджер получает свои напоминания в личку (также из «Моего профиля»).",
            "В группу приходят: ежедневный отчёт, CPL-предупреждения/паузы, новые лиды. Боту можно задать вопрос (через @имя бота).",
        ]),
        "lidlar": ("Лиды и CRM", [
            "Страница «Лиды» — все клиенты: источник, статус, ответственный менеджер.",
            "Откройте лид → меняйте статус (новый → связались → качественный → продано). Укажите сумму продажи — от неё считаются KPI и ROI.",
            "«Повторная связь» показывает, кому звонить сегодня; о просроченных узнаёт администратор.",
        ]),
        "cpl": ("CPL-защита (цена лида)", [
            "Настройки → Автоотключение таргета (CPL): целевой CPL, порог отключения и минимальный расход (в долларах).",
            "Выберите режим: «Только предупреждение» (для таргетологов) или «Автопауза» (для владельцев, рекомендуется).",
            "Проверка каждые 15 минут; в день автоматически отключается не более 10 объявлений.",
            "Ошибочно отключённое объявление включите кнопкой «Включить снова» на дашборде — 7 дней его не тронут.",
        ]),
        "avtopilot": ("Автопилот (AI-таргет)", [
            "Автопилот → «Новая кампания». AI задаст несколько вопросов (цель, бюджет, регион).",
            "AI составит план: аудитория, бюджет, текст. Проверьте и при необходимости отредактируйте.",
            "«Опубликовать в Meta» — кампания создаётся НА ПАУЗЕ, деньги не тратятся. Проверьте в Ads Manager и нажмите «Активировать» сами.",
        ]),
        "kreativ": ("Креатив-студия", [
            "Креатив-студия → выберите шаблон (1:1, 4:5, 9:16). Цвета бренда и логотип подставятся сами.",
            "Впишите текст, цену и кнопку — AI рисует только фон, текст остаётся чётким.",
            "Карусель: 3–6 карточек, 6 стилей; скачивание ZIP-архивом.",
        ]),
        "raqobatchilar": ("Анализ конкурентов", [
            "Конкуренты → Анализ Instagram. Введите username (до 5 через запятую): например dunyabunya, buxoro_maktabi.",
            "Средние лайки, комментарии, просмотры видео, ER и частота постов по последним 12–30 постам; лучший показатель — зелёный.",
            "«Открыть в Ad Library» — текущая реклама конкурента, тексты, креативы и дата запуска (официальная страница Meta).",
            "Видны только открытые Business/Creator аккаунты; ваш Instagram должен быть подключён.",
        ]),
        "kpi": ("KPI и бонусы менеджеров", [
            "Настройки → Общее → «Правила KPI и бонусов»: оклад, бонусы за покупки, процент, план, пороги продаж и оборота.",
            "Аналитика → KPI: зарплата и бонусы каждого менеджера за месяц и сколько осталось до следующего порога.",
            "Менеджеру, вышедшему в середине месяца, план уменьшается пропорционально отработанным дням.",
        ]),
        "tolov": ("Тариф и оплата", [
            "Настройки → Тариф и оплата. У каждого тарифа указаны доллары и сумма в сумах, которую спишет Payme.",
            "С привязанной картой Payme оплата списывается автоматически каждый месяц; или переведите на карту и нажмите «Я оплатил».",
            "Тариф можно повысить в любой момент — данные сохраняются.",
        ]),
    },
    "en": {
        "title": "Guide",
        "subtitle": "Get Replix running in 15 minutes: a step-by-step guide. Still have questions? Ask the AI assistant inside the app.",
        "aud_all": "Everyone", "aud_owner": "Business owner", "aud_targetolog": "Media buyer",
        "open": "Open", "toc": "Contents",
        "boshlash": ("Getting started", [
            "replix.uz → \"Start free\". Enter company name, login and password — {trial_days} days free, no card needed.",
            "Next, answer 4–5 questions about your business (product, audience, prices) so AI tailors ad copy and analysis.",
            "Before the trial ends, choose a plan on the Plan & billing page.",
        ]),
        "meta": ("Connect Facebook / Instagram (Meta)", [
            "Left menu → Connection (Connect accounts) → \"Connect via Facebook\".",
            "In the Facebook window select your page, Instagram account and ad account, then grant access.",
            "With a single page/account everything is saved automatically; with several you pick one. The Pixel is found automatically.",
            "Done: spend, leads and CPL appear on the dashboard and Target page; ad leads land in the CRM automatically.",
        ]),
        "telegram": ("Telegram bot and group", [
            "Connect accounts page → \"👥 Add to group (one click)\". Telegram opens.",
            "Pick your work group and add the bot — the group is linked to your company automatically.",
            "\"📱 Connect personally in Telegram\" — each manager gets their own reminders privately (also from My profile).",
            "The group receives the daily report, CPL warnings/pauses and new leads. You can also ask the bot questions (mention it).",
        ]),
        "lidlar": ("Leads and CRM", [
            "The Leads page lists every customer: source, status, assigned manager.",
            "Open a lead → change its status (new → contacted → qualified → sold). Enter the sale amount — KPI and ROI use it.",
            "Follow-ups shows who to call today; the admin is alerted about overdue ones.",
        ]),
        "cpl": ("CPL protection (cost per lead)", [
            "Settings → Auto-pause ads (CPL): target CPL, kill threshold and minimum spend (in USD).",
            "Pick a mode: \"Warn only\" (media buyers) or \"Auto-pause\" (business owners, recommended).",
            "Checked every 15 minutes; at most 10 ads are paused automatically per day.",
            "Resume a wrongly paused ad with \"Resume\" on the dashboard — it is left alone for 7 days.",
        ]),
        "avtopilot": ("Autopilot (AI targeting)", [
            "Autopilot → \"New campaign\". AI asks a few questions (goal, budget, region).",
            "AI drafts a plan: audience, budget, copy. Review and edit if needed.",
            "\"Publish to Meta\" creates the campaign PAUSED — no money is spent. Check it in Ads Manager and click \"Activate\" yourself.",
        ]),
        "kreativ": ("Creative studio", [
            "Creative studio → pick a template (1:1, 4:5, 9:16). Brand colours and logo are applied automatically.",
            "Type the text, price and button — AI only draws the background, so text stays crisp.",
            "Carousel: 3–6 cards, 6 styles; download as a ZIP.",
        ]),
        "raqobatchilar": ("Competitor analysis", [
            "Competitors → Instagram analysis. Enter usernames (up to 5, comma-separated), e.g. dunyabunya, buxoro_maktabi.",
            "Average likes, comments, video views, ER and posting frequency over the last 12–30 posts; the best value is green.",
            "\"Open in Ad Library\" shows the competitor's current ads, copy, creatives and start dates (Meta's official page).",
            "Only public Business/Creator accounts are visible, and your own Instagram must be connected.",
        ]),
        "kpi": ("Manager KPI and bonuses", [
            "Settings → General → \"KPI and bonus rules\": salary, purchase bonuses, percent, monthly plan, sales and turnover tiers.",
            "Analytics → KPI: each manager's salary and bonuses this month and how far the next tier is.",
            "For a manager who joined mid-month the plan is reduced in proportion to days worked.",
        ]),
        "tolov": ("Plan and billing", [
            "Settings → Plan & billing. Each plan shows the USD price and the exact UZS amount Payme charges.",
            "With a linked Payme card you are charged automatically each month; or transfer to the card and click \"I have paid\".",
            "You can upgrade any time — your data is kept.",
        ]),
    },
}


def sections_for(lang_code: str, trial_days: int) -> tuple[dict, list[dict]]:
    texts = TEXT.get(lang_code) or TEXT["uz"]
    out = []
    for sec in SECTIONS:
        title, steps = texts[sec["id"]]
        out.append({
            **sec, "title": title,
            "steps": [s.replace("{trial_days}", str(trial_days)) for s in steps],
            "audience_label": texts["aud_" + sec["audience"]],
        })
    return texts, out
