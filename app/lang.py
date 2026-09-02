"""lang.py — 2026-09, foydalanuvchi so'rovi: "сделай на русском, на
узбекском и на всех" (mijozga ko'rinadigan sahifalarni bir necha tilda
qilish). Og'ir Flask-Babel/gettext infratuzilmasi o'rniga eng ODDIY,
tushunarli yondashuv: har bir matn kaliti uchun til-so'zlik (dict).

QAMROV (ATAYLAB BOSQICHMA-BOSQICH, foydalanuvchiga aytilgan): bu FAQAT
mijoz birinchi marta ko'radigan "kirish yo'li" sahifalarini qamraydi --
bosh sahifa (landing), kirish, ro'yxatdan o'tish. Butun ICHKI CRM
(dashboard, lidlar, target, sozlamalar va h.k. -- o'nlab sahifa, yuzlab
matn) hali FAQAT o'zbekcha -- bu ALOHIDA, ancha katta keyingi bosqich
(foydalanuvchiga aniq aytilgan, "Til qamrovi" savolida "butun CRM"
tanlangan bo'lsa ham, xavfsiz va sifatli bajarish uchun bosqichma-bosqich
davom ettiriladi).

Yangi til qo'shish: `SUPPORTED_LANGS`ga kodni qo'shing, `TRANSLATIONS`ga
shu kod uchun to'liq lug'at qo'shing (kalitlar `TRANSLATIONS["uz"]`dagi
bilan bir xil bo'lishi kerak -- `translate()` yetishmagan kalit uchun
avtomatik o'zbekchaga qaytadi, hech qachon xato bermaydi)."""

SUPPORTED_LANGS = ["uz", "ru"]
DEFAULT_LANG = "uz"

LANG_LABELS = {"uz": "O'zbekcha", "ru": "Русский"}

TRANSLATIONS = {
    "uz": {
        "nav.features": "Imkoniyatlar",
        "nav.how": "Qanday ishlaydi",
        "nav.pricing": "Tariflar",
        "nav.login": "Kirish",
        "nav.signup": "Bepul boshlash",
        "nav.home": "Bosh sahifa",

        "hero.eyebrow": "O'zbekiston bizneslari uchun CRM",
        "hero.h1": "5-6 ta xizmat o'rniga — <span>bitta</span> Replix",
        "hero.sub": "CRM, reklama monitoring, SMM hisobot, qo'ng'iroq nazorati va AI tahlil uchun alohida-alohida xizmatlarga to'lash shart emas. Replix hammasini BITTA obunada, ancha arzon narxda birlashtiradi — Instagram va Facebook reklamalaringizni, sotuv jamoangizni va mijozlar bazangizni bitta joyda kuzating.",
        "hero.cta_trial": "14 kun bepul sinash",
        "hero.cta_pricing": "Tariflarni ko'rish",
        "hero.note1": "✓ Karta shart emas",
        "hero.note2": "✓ 5 daqiqada ishga tushiriladi",
        "hero.note3": "✓ O'zbek tilida to'liq qo'llab-quvvatlash",

        "compare.h2": "Nega 5 ta alohida xizmat o'rniga bitta Replix?",
        "compare.sub": "Odatda CRM, reklama monitoring, SMM hisobot va qo'ng'iroq tahlili — har biri ALOHIDA xizmat, alohida login, alohida to'lov. Replix'da hammasi bitta obunada.",
        "compare.separate_head": "Alohida-alohida sotib olsangiz",
        "compare.replix_head": "Replix bilan",
        "compare.total_separate": "Taxminan $140–270/oy + har biriga alohida sozlash va login",
        "compare.total_replix": "$50/oy dan boshlanadi — bitta login, bitta hisobot",
        "compare.item1": "CRM / lidlar tizimi",
        "compare.item1_price": "~$30–50/oy",
        "compare.item2": "Reklama (target) monitoring",
        "compare.item2_price": "~$40–80/oy",
        "compare.item3": "SMM hisobot xizmati",
        "compare.item3_price": "~$20–40/oy",
        "compare.item4": "AI qo'ng'iroq tahlili",
        "compare.item4_price": "~$50–100/oy",

        "features.h2": "Bitta tizim — butun sotuv jarayoni",
        "features.sub": "Alohida-alohida jadval va Excel fayllar o'rniga, reklamadan sotuvgacha bo'lgan yo'lning har bosqichi shu yerda.",
        "features.f1_title": "Lidlar va CRM voronkasi",
        "features.f1_desc": "Meta Lead Ads'dan avtomatik kelgan va qo'lda kiritilgan lidlarni bitta voronkada boshqaring: yangi → aloqa qilindi → malakali → sotildi.",
        "features.f2_title": "Target monitoring",
        "features.f2_desc": "Xarajat, lead soni, CPL va konversiya — kampaniya kesimida, real vaqtda. Byudjet oshib ketishidan oldin ogohlantirish oladi.",
        "features.f3_title": "SMM hisobot va Instagram xabarlar",
        "features.f3_desc": "Obunachilar dinamikasi, kontent samaradorligi va Instagram Direct xabarlar bitta joyda — javobsiz qolgan mijoz yo'qolmaydi.",
        "features.f4_title": "AI qo'ng'iroq tahlili",
        "features.f4_desc": "Har bir sotuv qo'ng'irog'i avtomatik tinglanadi va tahlil qilinadi — sifat balli, kamchiliklar va tavsiyalar bilan (Biznes va Ekspert tarifida).",
        "features.f5_title": "Menejerlar KPI va bonus",
        "features.f5_desc": "Har oy avtomatik hisoblanadigan reja-bajarilish jadvali — kim qancha sotdi, bonus qancha bo'lishi bir qarashda ko'rinadi.",
        "features.f6_title": "Telegram bot orqali hisobot",
        "features.f6_desc": "Kuniga bir marta, jamoangiz Telegram guruhiga avtomatik yuboriladigan holat hisoboti — hech kim CRM'ni ochmasa ham xabardor bo'ladi.",

        "steps.h2": "4 qadamda ishga tushiring",
        "steps.sub": "Alohida sozlash mutaxassisi kerak emas — ro'yxatdan o'tgach, o'zingiz 5-10 daqiqada tayyorlaysiz.",
        "steps.s1_title": "Ro'yxatdan o'ting",
        "steps.s1_desc": "Kompaniya nomi, login va parol — shu qadar, karta kerak emas.",
        "steps.s2_title": "Akkauntingizni ulang",
        "steps.s2_desc": "Instagram (va tarifga qarab Facebook reklama hisobingizni) ulang.",
        "steps.s3_title": "Jamoangizni qo'shing",
        "steps.s3_desc": "Menejerlaringizga login yarating, kimga qaysi bo'lim ochiq ekanini belgilang.",
        "steps.s4_title": "Natijalarni kuzating",
        "steps.s4_desc": "Lidlar, xarajat va sotuvlar real vaqtda bir ekranda to'planadi.",

        "pricing.h2": "Har bosqich uchun aniq tarif",
        "pricing.sub": "Sinovdan boshlang, jamoangiz o'sgani sayin tarifni oshiring — yashirin to'lov yo'q.",
        "pricing.badge_popular": "Eng ommabop",
        "pricing.cta_trial": "Bepul boshlash",
        "pricing.cta_signup": "Ro'yxatdan o'tish",
        "pricing.free": "Bepul",
        "pricing.per_month": "/oy",
        "pricing.monthly_renew": "har oy yangilanadi",
        "pricing.days": "kun",

        "cta_band.h2": "Reklama byudjetingiz oqilona ishlayaptimi?",
        "cta_band.sub": "Bugun ro'yxatdan o'ting, Instagram akkauntingizni ulang va birinchi natijalarni bugunoq ko'ring.",
        "cta_band.button": "14 kun bepul boshlash",

        "footer.rights": "Barcha huquqlar himoyalangan.",
        "footer.login": "Kirish",

        "mock.new_leads": "Yangi lidlar",
        "mock.cpl": "CPL",
        "mock.sales": "Sotuv",
        "mock.status_sold": "sotildi",
        "mock.status_qualified": "malakali",
        "mock.status_contacted": "aloqa qilindi",
        "mock.status_new": "yangi",

        "login.subtitle": "Kirish uchun login/parolingizni kiriting",
        "login.username": "Login",
        "login.password": "Parol",
        "login.submit": "Kirish",
        "login.no_account": "Hali kompaniyangiz yo'qmi?",
        "login.signup_link": "Ro'yxatdan o'ting",

        "signup.title": "Kompaniyangizni ro'yxatdan o'tkazing",
        "signup.subtitle": "2 daqiqada tayyor bo'ladi — keyingi qadamda Instagram akkauntingizni ulaysiz.",
        "signup.choose_plan": "Tarifni tanlang",
        "signup.company_name": "Kompaniya nomi",
        "signup.admin_username": "Admin login",
        "signup.admin_full_name": "To'liq ism (ixtiyoriy)",
        "signup.email": "Email (ixtiyoriy)",
        "signup.password": "Parol",
        "signup.password2": "Parolni tasdiqlang",
        "signup.submit": "Ro'yxatdan o'tish",
        "signup.have_account": "Allaqachon hisobingiz bormi?",
        "signup.login_link": "Kirish",

        # 2026-09, SEO/AEO tuzatish: foydalanuvchi so'rovi ("ai qidirganda
        # ideal chiqishi uchun kriteriyalar bo'yicha yoz"). Google AI
        # Overview replix.uz so'rovida bog'liq bo'lmagan "Replix.ai" (AI matn
        # yozish xizmati) va "Ecom Learn by Replix" (kurs platformasi) kabi
        # BOSHQA "Replix" nomli mahsulotlarni aralashtirib yuborgani
        # aniqlandi -- chunki "Replix" nomi internetda bir nechta turli
        # kompaniyaga tegishli. Bu FAQ blok ANIQ, deklarativ javoblar bilan
        # o'zini his qilib, o'sha boshqa mahsulotlardan farqlaydi (bu --
        # "disambiguation" -- qidiruv/AI tizimlariga to'g'ri obyektni
        # ko'rsatishning tan olingan usuli).
        "faq.q1": "Replix nima?",
        "faq.a1": "Replix — O'zbekiston bizneslari uchun CRM, Meta (Instagram/Facebook) reklama monitoring, SMM hisobot va AI qo'ng'iroq tahlili xizmatlarini BITTA platformada birlashtiruvchi tizim. U reklama byudjeti, lidlar va sotuv jamoasini reklamadan sotuvgacha bitta ekranda kuzatib borishga yordam beradi.",
        "faq.q2": "Replix kimlar uchun mo'ljallangan?",
        "faq.a2": "Instagram va Facebook orqali reklama beradigan O'zbekiston kichik va o'rta bizneslari uchun — sotuv jamoasi, reklama xarajati va mijozlar bazasini bitta joyda ko'rishni istaydiganlar uchun.",
        "faq.q3": "Replix qancha turadi?",
        "faq.a3": "Narxlar oyiga $50 dan boshlanadi. 14 kunlik bepul sinov mavjud, karta ma'lumoti talab qilinmaydi.",
        "faq.q4": "Replix.uz \"Replix.ai\" yoki boshqa \"Replix\" nomli xizmatlar bilan bog'liqmi?",
        "faq.a4": "Yo'q. Replix (replix.uz) — O'zbekistonda ishlab chiqilgan, CRM va Meta reklama monitoringga ixtisoslashgan alohida platforma. U matn/kontent yozuvchi AI-yordamchilar yoki boshqa davlatlardagi kurs platformalari bilan hech qanday aloqasi yo'q — bu butunlay boshqa kompaniya va mahsulot.",
        "faq.q5": "Instagram yoki Facebook akkauntimni qanday ulayman?",
        "faq.a5": "Ro'yxatdan o'tgach, \"Akkauntlarni ulash\" sahifasida bitta tugma orqali Facebook hisobingizga kirasiz — sahifa ID yoki token qo'lda kiritish shart emas.",

        # 2026-09, foydalanuvchi so'rovi ("web ozida ushatta pasida forma
        # qilib qoy ... aloqa uchun nomerlar ham qoshib qoy"): bosh sahifa
        # pastidagi "Biz bilan bog'laning" bo'limi uchun.
        "contact.h2": "Biz bilan bog'laning",
        "contact.sub": "Savolingiz bormi? Telefon yoki Telegram orqali yozing, yoki quyidagi formani to'ldiring — tez orada javob beramiz.",
        "contact.note": "Formani to'ldiring — odatda 1 soat ichida siz bilan bog'lanamiz.",
        "contact.name": "Ismingiz",
        "contact.phone": "Telefon raqamingiz",
        "contact.message": "Xabar (ixtiyoriy)",
        "contact.submit": "Yuborish",
    },
    "ru": {
        "nav.features": "Возможности",
        "nav.how": "Как это работает",
        "nav.pricing": "Тарифы",
        "nav.login": "Войти",
        "nav.signup": "Начать бесплатно",
        "nav.home": "Главная",

        "hero.eyebrow": "CRM для бизнеса в Узбекистане",
        "hero.h1": "5-6 сервисов — теперь <span>один</span> Replix",
        "hero.sub": "Не нужно платить отдельно за CRM, мониторинг рекламы, SMM-отчёты, контроль звонков и AI-анализ. Replix объединяет всё это в ОДНОЙ подписке по значительно более низкой цене — отслеживайте рекламу в Instagram и Facebook, отдел продаж и базу клиентов в одном месте.",
        "hero.cta_trial": "14 дней бесплатно",
        "hero.cta_pricing": "Смотреть тарифы",
        "hero.note1": "✓ Карта не нужна",
        "hero.note2": "✓ Запуск за 5 минут",
        "hero.note3": "✓ Полная поддержка на узбекском и русском",

        "compare.h2": "Зачем 5 отдельных сервисов, если можно один?",
        "compare.sub": "Обычно CRM, мониторинг рекламы, SMM-отчёты и анализ звонков — это ОТДЕЛЬНЫЕ сервисы: отдельный вход, отдельная оплата. В Replix всё в одной подписке.",
        "compare.separate_head": "Если покупать по отдельности",
        "compare.replix_head": "С Replix",
        "compare.total_separate": "Примерно $140–270/мес + отдельная настройка и вход для каждого",
        "compare.total_replix": "От $50/мес — один вход, один отчёт",
        "compare.item1": "CRM / система лидов",
        "compare.item1_price": "~$30–50/мес",
        "compare.item2": "Мониторинг рекламы (target)",
        "compare.item2_price": "~$40–80/мес",
        "compare.item3": "Сервис SMM-отчётов",
        "compare.item3_price": "~$20–40/мес",
        "compare.item4": "AI-анализ звонков",
        "compare.item4_price": "~$50–100/мес",

        "features.h2": "Одна система — весь процесс продаж",
        "features.sub": "Вместо отдельных таблиц и файлов Excel — каждый этап пути от рекламы до продажи здесь.",
        "features.f1_title": "Лиды и воронка CRM",
        "features.f1_desc": "Управляйте лидами, автоматически пришедшими из Meta Lead Ads и введёнными вручную, в одной воронке: новый → на связи → квалифицирован → продан.",
        "features.f2_title": "Мониторинг рекламы",
        "features.f2_desc": "Расход, число лидов, CPL и конверсия — в разрезе кампаний, в реальном времени. Предупреждение до превышения бюджета.",
        "features.f3_title": "SMM-отчёты и сообщения Instagram",
        "features.f3_desc": "Динамика подписчиков, эффективность контента и сообщения Instagram Direct в одном месте — ни один клиент без ответа не потеряется.",
        "features.f4_title": "AI-анализ звонков",
        "features.f4_desc": "Каждый звонок продажи автоматически прослушивается и анализируется — с оценкой качества, недочётами и рекомендациями (в тарифах Бизнес и Эксперт).",
        "features.f5_title": "KPI и бонусы менеджеров",
        "features.f5_desc": "Автоматически рассчитываемый ежемесячный план-факт — сразу видно, кто сколько продал и какой будет бонус.",
        "features.f6_title": "Отчёт через Telegram-бота",
        "features.f6_desc": "Раз в день автоматический отчёт о статусе отправляется в Telegram-группу вашей команды — все в курсе, даже не открывая CRM.",

        "steps.h2": "Запуск за 4 шага",
        "steps.sub": "Отдельный специалист по настройке не нужен — после регистрации вы сами всё подготовите за 5-10 минут.",
        "steps.s1_title": "Зарегистрируйтесь",
        "steps.s1_desc": "Название компании, логин и пароль — и всё, карта не нужна.",
        "steps.s2_title": "Подключите аккаунт",
        "steps.s2_desc": "Подключите Instagram (и, в зависимости от тарифа, рекламный аккаунт Facebook).",
        "steps.s3_title": "Добавьте команду",
        "steps.s3_desc": "Создайте логины для менеджеров, укажите, какой раздел кому доступен.",
        "steps.s4_title": "Следите за результатами",
        "steps.s4_desc": "Лиды, расходы и продажи собираются на одном экране в реальном времени.",

        "pricing.h2": "Понятный тариф для каждого этапа",
        "pricing.sub": "Начните с пробного периода, повышайте тариф по мере роста команды — никаких скрытых платежей.",
        "pricing.badge_popular": "Популярный выбор",
        "pricing.cta_trial": "Начать бесплатно",
        "pricing.cta_signup": "Зарегистрироваться",
        "pricing.free": "Бесплатно",
        "pricing.per_month": "/мес",
        "pricing.monthly_renew": "продлевается ежемесячно",
        "pricing.days": "дней",

        "cta_band.h2": "Ваш рекламный бюджет работает разумно?",
        "cta_band.sub": "Зарегистрируйтесь сегодня, подключите Instagram и увидите первые результаты уже сегодня.",
        "cta_band.button": "Начать бесплатно на 14 дней",

        "footer.rights": "Все права защищены.",
        "footer.login": "Войти",

        "mock.new_leads": "Новые лиды",
        "mock.cpl": "CPL",
        "mock.sales": "Продажи",
        "mock.status_sold": "продано",
        "mock.status_qualified": "квалифицирован",
        "mock.status_contacted": "на связи",
        "mock.status_new": "новый",

        "login.subtitle": "Введите логин/пароль для входа",
        "login.username": "Логин",
        "login.password": "Пароль",
        "login.submit": "Войти",
        "login.no_account": "Ещё нет компании?",
        "login.signup_link": "Зарегистрироваться",

        "signup.title": "Зарегистрируйте свою компанию",
        "signup.subtitle": "Займёт 2 минуты — на следующем шаге вы подключите Instagram.",
        "signup.choose_plan": "Выберите тариф",
        "signup.company_name": "Название компании",
        "signup.admin_username": "Логин администратора",
        "signup.admin_full_name": "Полное имя (необязательно)",
        "signup.email": "Email (необязательно)",
        "signup.password": "Пароль",
        "signup.password2": "Подтвердите пароль",
        "signup.submit": "Зарегистрироваться",
        "signup.have_account": "Уже есть аккаунт?",
        "signup.login_link": "Войти",

        "faq.q1": "Что такое Replix?",
        "faq.a1": "Replix — это система для бизнеса в Узбекистане, объединяющая CRM, мониторинг рекламы Meta (Instagram/Facebook), SMM-отчёты и AI-анализ звонков в ОДНОЙ платформе. Она помогает отслеживать рекламный бюджет, лиды и отдел продаж на одном экране — от рекламы до продажи.",
        "faq.q2": "Для кого предназначен Replix?",
        "faq.a2": "Для малого и среднего бизнеса в Узбекистане, который даёт рекламу в Instagram и Facebook — для тех, кто хочет видеть отдел продаж, расходы на рекламу и базу клиентов в одном месте.",
        "faq.q3": "Сколько стоит Replix?",
        "faq.a3": "Тарифы начинаются от $50 в месяц. Доступен 14-дневный бесплатный пробный период, банковская карта не требуется.",
        "faq.q4": "Связан ли Replix.uz с «Replix.ai» или другими сервисами с названием «Replix»?",
        "faq.a4": "Нет. Replix (replix.uz) — отдельная платформа, разработанная в Узбекистане и специализирующаяся на CRM и мониторинге рекламы Meta. Она никак не связана с AI-помощниками для написания текстов или курс-платформами с похожим названием в других странах — это совершенно другая компания и продукт.",
        "faq.q5": "Как подключить аккаунт Instagram или Facebook?",
        "faq.a5": "После регистрации на странице «Подключение аккаунтов» вы входите в свой аккаунт Facebook одной кнопкой — вводить ID страницы или токен вручную не нужно.",

        "contact.h2": "Свяжитесь с нами",
        "contact.sub": "Есть вопрос? Напишите по телефону или в Telegram, либо заполните форму ниже — ответим в ближайшее время.",
        "contact.note": "Заполните форму — обычно отвечаем в течение 1 часа.",
        "contact.name": "Ваше имя",
        "contact.phone": "Номер телефона",
        "contact.message": "Сообщение (необязательно)",
        "contact.submit": "Отправить",
    },
}


def translate(key: str, lang: str) -> str:
    table = TRANSLATIONS.get(lang) or TRANSLATIONS[DEFAULT_LANG]
    val = table.get(key)
    if val is not None:
        return val
    # Yetishmagan kalit -- avtomatik standart tilga (o'zbekcha) qaytadi,
    # HECH QACHON xato bermaydi yoki kalitning o'zini ko'rsatmaydi.
    return TRANSLATIONS[DEFAULT_LANG].get(key, key)
