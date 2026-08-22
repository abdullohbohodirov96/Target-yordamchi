# Target CRM — Render versiyasi

Eski loyiha (`Targeting-yordmachi-ai`, Vercel'da) Meta Ads'ni tahlil qilib,
Telegram orqali boshqarardi, lekin serverless (Vercel) muhitida bir nechta
muammo bor edi: kunlik hisobot ko'pincha kelmasdi (faqat 1 ta cron ishlagan,
u ham 9:00 emas, 13:00da va faqat "diqqatga loyiq narsa bo'lsa"), guruhda
savolga javob bermasdi (Telegram "Group Privacy" yoqilgan bo'lishi mumkin),
va lidlar hech qayerga saqlanmasdi.

Bu versiya **Render**'da **doimiy jarayon** sifatida ishlaydi (Vercel'dagi
kabi har so'rovga alohida qisqa muddatli funksiya emas) va ustiga to'liq
**CRM + web dashboard** qo'shilgan:

- 📊 **Dashboard** (`/`) — har bir kampaniya uchun: xarajat, ko'rishlar,
  lidlar soni, sifatli/sifatsiz taqsimot, sotuvlar soni, o'rtacha chek,
  daromad, ROI.
- 📋 **CRM** (`/leads`) — Meta Lead Ads'dan har 15 daqiqada avtomatik
  tortilgan lidlar. Menejerlar kirib, holatini (yangi → bog'lanildi →
  sifatli/sifatsiz → sotildi) va izohini yozadi.
- 👥 **Menejerlar** (`/managers`, faqat admin) — yangi menejer/admin hisob
  qo'shish.
- 🤖 **Telegram bot** — eski botning BARCHA mantig'i saqlangan (erkin matn
  bilan buyruq berish, savolga javob, oylik PDF hisobot va h.k.), lekin
  endi:
  - Har kuni **09:00 (Toshkent)** — ADMIN TARGET HISOBOTI har doim yuboriladi.
  - **Har soat** — to'liq audit + avtomatik tuzatish (byudjet oshirish/
    kamaytirish, pause/resume) — faqat diqqatga loyiq narsa bo'lsa yuboradi.
  - **Har 4 soatda** — byudjet balansi nazorati.
  - **Har 15 daqiqada** — yangi lidlarni CRM bazasiga tortish.
  - Bularning barchasi shu jarayonning ICHIDA ishlaydi (APScheduler) —
    tashqi cron-job.org yoki Vercel Cron shart emas.

## Nima uchun eski kod deyarli o'zgarishsiz qoldi

`meta_api.py`, `orchestrator.py`, `budget_tracker.py`, `monthly_report.py`,
`agents/*.md`, `business_rules.json` — bularning barchasi eski, ishlab
tekshirilgan loyihadan olingan va deyarli o'zgarishsiz ishlatilgan. Faqat
`kv_store.py` Postgres'ga ko'chirildi (interfeys bir xil qoldi — `get_json`/
`set_json`), shuning uchun qolgan hamma narsa avvalgidek ishlayveradi.

## O'rnatish (Render)

### 1. Repo'ni GitHub'ga yuklang

Bu papkani (`target-crm/`) o'zingizning YANGI GitHub repo'ingizga push
qiling (masalan `target-crm` nomi bilan).

### 2. Render'da Blueprint orqali deploy qiling

Render dashboard → **New → Blueprint** → repo'ni tanlang. `render.yaml`
avtomatik: bitta web service + bitta Postgres baza yaratadi.

Agar Blueprint ishlatmasangiz, qo'lda: **New → Web Service**, root
directory `app`, build command `pip install -r requirements.txt`, start
command:
```
gunicorn wsgi:application --workers 1 --threads 4 --timeout 120
```
**MUHIM:** `--workers 1` bo'lishi SHART — bir nechta worker bo'lsa,
Telegram scheduler (APScheduler) va suhbat holati bir nechta nusxada
ishga tushib, xabarlar takrorlanishi yoki qarama-qarshi holatga tushishi
mumkin. Alohida **PostgreSQL** ham qo'shing (New → PostgreSQL) va uning
`DATABASE_URL`sini web service'ga ulang.

### 3. Environment Variables

| O'zgaruvchi | Qayerdan olinadi |
|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather |
| `TELEGRAM_AGENTS_GROUP_ID` / `TELEGRAM_REPORT_GROUP_ID` | pastga qarang |
| `ANTHROPIC_API_KEY` | console.anthropic.com |
| `OPENAI_API_KEY` (MAJBURIY) | platform.openai.com |
| `META_ACCESS_TOKEN` / `META_AD_ACCOUNT_ID` | **eski Vercel loyihangizdan ko'chiring** (Vercel → Settings → Environment Variables) — token hali amal qiladi bo'lsa qayta ishlatish mumkin |
| `META_PAGE_ID` | **YANGI, MAJBURIY endi** — CRM lead-sync shu Page'ning Instant Form'laridan lead o'qiydi. Facebook Page → About → Page ID |
| `CRON_SECRET`, `FLASK_SECRET_KEY` | `render.yaml`da `generateValue: true` — Render o'zi tasodifiy qiymat yaratadi |

Guruh chat_id'larini bilish uchun: botni ikkala guruhga qo'shing, guruhda
bittadan xabar yozing, keyin brauzerda oching:
```
https://api.telegram.org/bot<TOKEN>/getUpdates
```
va har bir guruh uchun `"chat":{"id": -100...}` qiymatini oling.

### 4. Birinchi admin hisobni yarating

Deploy tugagach, Render dashboard → sizning service → **Shell** bo'limini
oching va:
```bash
python scripts/create_manager.py --username admin --password KuchliParol123 --full-name "Abdulloh" --role admin
```
Shundan keyin `/managers` orqali boshqa menejerlarni admin panel ichidan
qo'sha olasiz — Shell'ga qayta kirish shart emas.

### 5. Telegram webhook'ni ro'yxatdan o'tkazing

```
https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<render-domeningiz>/api/webhook
```

### 6. Guruh Privacy'ni o'chiring (MUHIM — eski botda aynan shu sabab guruhda javob bermagan)

@BotFather → `/setprivacy` → botingizni tanlang → **Disable**. Shundan
keyin bot guruhdagi HAR BIR xabarni ko'radi (avval faqat buyruq/@mention'ni
ko'rar edi).

### 7. Tekshirish

`https://<domeningiz>/api/health` — barcha kerakli o'zgaruvchilar
o'rnatilganini ko'rsatadi.

## CRM haqida muhim eslatma

Lidlar hozircha **polling** orqali (har 15 daqiqada Meta'dan so'rab)
olinadi, real-vaqt webhook orqali emas — bu ancha soddaroq va Meta App
Review talab qilmaydi. Agar kelajakda haqiqiy real-vaqt (soniyalar ichida)
kerak bo'lsa, alohida Meta App + Webhooks sozlash orqali qo'shish mumkin —
hozirgi MVP uchun 15 daqiqalik kechikish odatda muammo emas.

## Keyingi qadam — dashboard dizayni

Siz aytgan "misol" (referens dizayn) kelgach, `templates/dashboard.html`
va `static/style.css`ni aynan o'sha ko'rinishga moslashtiraman — hozirgi
dizayn funksional, lekin sodda (standart) ko'rinishda.
