# Payme Subscribe API -- avtomatik oylik to'lov

2026-09, foydalanuvchi so'rovi: "tolov avtomatik otishi uchun ... tolov
otkanini tekshirib boladigan qilish" -- ya'ni mijozning oylik obuna
to'lovini kartasidan AVTOMATIK yechib olish, mijoz har oy qaytadan
to'lamasin.

## 1-bosqich -- Payme bilan shartnoma (kod BILAN bog'liq emas)

Payme orqali pul qabul qilish uchun oddiy jismoniy shaxs sifatida
(faqat pasport bilan) shartnoma tuzib bo'lmaydi -- INN va bank hisob
raqami kerak. Kerak bo'ladigan narsa: **YaTT** (yakka tartibdagi
tadbirkor) yoki **MChJ** (yuridik shaxs).

Ariza **b2b-partner.payme.uz** orqali (yoki hisobingiz bo'lgan bank
Payme bilan hamkor bo'lsa, bank orqali ham) topshiriladi. Tasdiqlangach
sizga beriladi:

- **Merchant ID** (kassa ID)
- **Kalit (key)** -- avval TEST muhit uchun, keyin PROD uchun alohida

## 2-bosqich -- ENV o'zgaruvchilari

Kalitlar kelgach, Render'da (Settings -> Environment) quyidagilarni
qo'shing:

| O'zgaruvchi | Tavsif |
|---|---|
| `PAYME_MERCHANT_ID` | Payme bergan kassa (Merchant) ID |
| `PAYME_TEST_KEY` | TEST muhit kaliti (sinov paytida) |
| `PAYME_KEY` | PROD (haqiqiy) kalit -- faqat production'ga tayyor bo'lganda |
| `PAYME_TEST_MODE` | `true` (standart) -- TEST muhitda ishlaydi (`checkout.test.paycom.uz`). PROD'ga o'tishda `false` qiling -- shunda `PAYME_KEY` va `checkout.paycom.uz` ishlatiladi. |
| `PAYME_USD_TO_UZS_RATE` | `plans.py`dagi narxlar USD'da, Payme esa faqat so'mda ishlaydi -- shu kurs orqali o'tkaziladi (standart: 12700, JORIY kursga moslab o'rnating). |

`PAYME_MERCHANT_ID` va (test rejimida) `PAYME_TEST_KEY` o'rnatilmagan
bo'lsa -- butun oqim (kartani bog'lash sahifasi, `job_payme_autopay()`
fon vazifasi) o'z-o'zidan "sozlanmagan" holatda jim turadi, hech kimga
xato ko'rsatmaydi va hech qanday tarmoq so'rovi yubormaydi.

## 3-bosqich -- oqim qanday ishlaydi

1. `/tolov` sahifasida admin kartasini (raqam + amal qilish muddati)
   bir marta kiritadi -- `payme_subscribe.create_card()`.
2. Odatda SMS tasdiqlash talab qilinadi -- kod kelgach shu yerda
   tasdiqlaydi -- `verify_card()`. Shundan keyin token DOIMIY (shifrlangan
   holda, `Company.payme_card_token`) saqlanadi. **Xom karta raqami
   hech qachon saqlanmaydi.**
3. Har kuni ertalab (07:00 Toshkent) `scheduler.job_payme_autopay()`
   muddati (`paid_until`) 1 kun ichida tugaydigan (yoki 3 kun oldin
   tugagan, hali "imtiyoz" davrida) HAR BIR kartasi bog'langan
   kompaniyani topib, avtomatik to'laydi va `paid_until`ni 30 kunga
   uzaytiradi. Har bir davr uchun FAQAT bitta urinish (`PaymeReceipt`
   jadvali orqali) -- ikki marta pul yechib olinmaydi.
4. Muvaffaqiyatli/muvaffaqiyatsiz har bir urinish haqida kompaniyaning
   Telegram guruhiga (`telegram_group_id` bo'lsa) xabar boradi.

## MUHIM -- ishga tushirishdan oldin tasdiqlab olish kerak bo'lgan narsa

`payme_subscribe.create_card()` xom karta raqamini BIZNING backend
serverimiz orqali Payme'ga yuboradi (Payme'ning rasmiy `cards.create`
parametrlariga mos). Lekin Payme, shu bilan bir qatorda, karta kiritish
FORMASI uchun alohida (PCI-DSS) talablar ham qo'yadi -- bu odatda karta
raqami BROWSER'dan to'g'ridan-to'g'ri (bizning serverimizga tegmasdan)
Payme'ning o'z JS-widget'iga borishi kerakligini bildiradi.

Payme rasman ulanish tasdiqlangach, ularning texnik integratsiya
bo'limi bilan ANIQ qaysi usul tavsiya etilishini tekshirib chiqing --
buni ochiq hujjatlardan to'liq aniqlab bo'lmadi. Agar rasmiy JS-widget
talab qilinsa, `templates/payment.html`dagi karta-kiritish formasi
o'shanga moslab (karta maydonlari bevosita Payme'ga ketadigan qilib)
almashtiriladi -- backend tomon (`payme_subscribe.py`, `PaymeReceipt`,
`job_payme_autopay()`) o'zgarishsiz qoladi, chunki ular FAQAT tokenlar
bilan ishlaydi.
