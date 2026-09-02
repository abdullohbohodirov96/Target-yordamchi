# Meta Ads + Conversions API — sozlash qo'llanmasi

Bu hujjat Replix'ning Meta (Facebook/Instagram) integratsiyasini ishga
tushirish uchun **inson tomonidan bir marta, Meta App Dashboard'da**
bajariladigan qadamlarni tasvirlaydi. Kod tomoni (OAuth almashinuvi,
token shifrlash, Conversions API yuborish) allaqachon tayyor — bu
hujjatdagi qadamlarsiz ham tizim ishlaydi, lekin har bir mijoz
"Facebook orqali ulash" tugmasi o'rniga qo'lda token/Dataset ID
kiritishga majbur bo'ladi.

2026-09 holatiga ko'ra ikkita yo'l mavjud, ikkalasi ham shu bitta kodga
mos:

1. **Hoziroq ishlaydigan yo'l (ushbu hujjatning asosiy qismi)** — klassik
   Facebook Login OAuth. Meta App Review'ning ODDIY (Standard/asosiy)
   qismidan tashqari hech narsa talab qilmaydi — o'z sahifasi/reklama
   hisobiga administrator bo'lgan har qanday mijoz darhol ulasa bo'ladi.
2. **Kelajakdagi kengaytma (hujjat oxirida, "Bosqich 2")** — "Facebook
   Login for Business" + Conversions API Integration Template
   (Embedded Signup, muddatsiz Business Integration System User tokeni).
   Bu Replix'ning O'Z Business Manager'ini Verification'dan o'tkazishni
   va alohida App Review'ni talab qiladi — hozircha QURILMAGAN (faqat
   quyida hujjatlashtirilgan, chunki Meta hali buni to'liq avtomatik
   qilishga imkon bermaydi).

---

## 1-qadam: Meta App yaratish (yoki mavjudini ishlatish)

1. https://developers.facebook.com/apps saytiga o'ting, **Create App**
   tugmasini bosing.
2. App turi: **Business**.
3. App nomini kiriting (masalan "Replix CRM"), Business Portfolio'ni
   tanlang (Replix'ning o'z Business Manager'i — mijozlarnikimas).

## 2-qadam: "Facebook Login" mahsulotini qo'shish

1. App Dashboard'da **Add Product** → **Facebook Login** → **Set Up**.
2. Chap menyudan **Facebook Login** → **Settings**'ga o'ting.
3. **Valid OAuth Redirect URIs** maydoniga ANIQ shu manzilni qo'shing
   (kod shu route'ni kutadi, `app.py`dagi
   `connect_facebook_callback`):

   ```
   https://<sizning-domeningiz>/connect-accounts/facebook/callback
   ```

   Lokal test uchun qo'shimcha qator: `http://localhost:5000/connect-accounts/facebook/callback`.

4. **Client OAuth Login**, **Web OAuth Login** — yoqilgan bo'lishi kerak.

## 3-qadam: So'raladigan ruxsatlar (permissions)

Kod har doim quyidagilarni so'raydi (`meta_api.oauth_dialog_url()`):

- `pages_show_list`
- `pages_read_engagement`
- `instagram_basic`

Kompaniyaning tarifi reklama ulashga ruxsat bersa (`plans.py`,
`can_connect_meta_ads=True`), qo'shimcha ravishda:

- `ads_management`
- `ads_read`
- `business_management`

App Dashboard'da **App Review** → **Permissions and Features**
bo'limida shu ro'yxatdagilarni so'rang. `pages_show_list`,
`pages_read_engagement`, `instagram_basic` odatda **Standard Access**
sifatida App Review'siz ham cheklangan miqyosda ishlaydi (rivojlanish/
test paytida App administratorlari/testerlar uchun); ommaviy
(har qanday mijoz) foydalanish uchun **Advanced Access** kerak —
buning uchun App Review topshiring (har bir ruxsat uchun qisqa ekran
yozuvi/skrinshot bilan, "nima uchun kerakligini" ko'rsating — masalan
"mijozning reklama hisobi xarajatini CRM panelida ko'rsatish uchun").

`ads_management`/`ads_read`/`business_management` — bular doim App
Review talab qiladi (mijozning O'Z reklama hisobini boshqarish/o'qish
uchun).

### Marketing API Access Tier (2026 yangilanishi)

Meta 2026-yilda Marketing API kirishini "Access Tier" tizimiga
o'tkazdi: **Limited Access** (standart, past chegaralar) va **Full
Access** (yuqori chegaralar) — bu endi FAQAT bir martalik qo'lda review
emas, balki HAQIQIY foydalanish statistikasiga (kamida 15 kun ichida
500+ chaqiruv, xato darajasi 15% dan past) asoslangan. Amalda: kichik
boshlanishda **Limited Access** yetarli (bir nechta mijoz), keyin
o'sish bilan Meta avtomatik **Full Access**ga ko'tarish imkonini
taklif qilishi mumkin — buni App Dashboard → Marketing API
bo'limidan kuzatib boring.

## 4-qadam: App Domains, maxfiylik va o'chirish sahifalari

App Review topshirish uchun **Settings → Basic** bo'limida quyidagilar
SHART:

- **App Domains**: `<sizning-domeningiz>`
- **Privacy Policy URL**: mavjud (yoki tez orada qo'shiladigan) maxfiylik
  siyosati sahifangiz manzili
- **Terms of Service URL**
- **User Data Deletion**: yoki alohida URL, yoki "Data Deletion
  Instructions URL" — foydalanuvchi ma'lumotlarini qanday o'chirish
  mumkinligini tushuntiruvchi sahifa/yo'riqnoma

Bularsiz App Review so'rovlari RAD ETILADI.

## 5-qadam: Business Verification

**Business Manager → Business Settings → Security Center → Start
Verification**. Rasmiy hujjat (yuridik tashkilot guvohnomasi yoki
patent/litsenziya), telefon/email tasdiqlash talab qilinadi. Bu jarayon
odatda bir necha kundan bir necha haftagacha davom etadi. Advanced
Access va ayniqsa 2-bosqich (Embedded Signup) uchun MAJBURIY.

## 6-qadam: Dev vs Live rejim

- **Development mode** (standart): faqat App administratorlari/
  developerlari/testerlari (App Dashboard → Roles) ulasin oladi —
  HAQIQIY mijozlar hali ULANA OLMAYDI.
- **Live mode**ga o'tkazish uchun: App Review'dan kamida bitta ruxsat
  o'tgan bo'lishi (yoki faqat `pages_show_list`/asosiy ruxsatlar bilan
  ham Live'ga o'tish mumkin — Advanced Access kerak bo'lmagan
  ruxsatlar bilan cheklangan tarzda), Privacy Policy/Terms URL'lari
  to'ldirilgan bo'lishi kerak.
- **MUHIM**: Live'ga o'tkazmasdan turib, faqat O'ZINGIZ (Replix admin)
  yoki qo'shilgan test foydalanuvchilar bilan sinab ko'rishingiz
  mumkin — ommaviy mijozlarga ochish uchun Live SHART.

## 7-qadam: Environment o'zgaruvchilar (Render)

Render Dashboard → target-crm servisi → Environment bo'limiga
qo'shing (`render.yaml`da `sync: false` bilan belgilangan —
qiymatlarni Render Dashboard orqali qo'lda kiritish kerak):

| O'zgaruvchi | Qayerdan olinadi | Majburiymi? |
|---|---|---|
| `META_APP_ID` | App Dashboard → Settings → Basic → App ID | Ha (OAuth tugmasi uchun) |
| `META_APP_SECRET` | App Dashboard → Settings → Basic → App Secret ("Show" tugmasi) | Ha — **HECH QACHON brauzerga/frontend kodga chiqarilmaydi**, faqat serverda (`meta_api.py`) ishlatiladi |
| `TOKEN_ENCRYPTION_KEY` | `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` buyrug'i bilan BIR MARTA generatsiya qilinadi | Tavsiya etiladi (sozlanmasa, `FLASK_SECRET_KEY`dan zaxira kalit avtomatik hosil bo'ladi — ishlaydi, lekin alohida kalit xavfsizroq) |
| `META_ACCESS_TOKEN` / `META_AD_ACCOUNT_ID` / `META_PAGE_ID` | ESKI, global (bitta akkaunt) integratsiya qoldig'i — YANGI mijozlar uchun endi SHART EMAS (har bir kompaniya o'z hisobini OAuth/Advanced orqali ulaydi) | Yo'q |
| `META_GRAPH_API_VERSION` | Hozircha `meta_api.py`da `v21.0` deb qattiq yozilgan (kodda `GRAPH_API_VERSION` o'zgaruvchisi) — alohida env o'zgaruvchi sifatida chiqarish past-ustuvorlik, kelajakda kerak bo'lsa qo'shish oson | Yo'q |

**MUHIM (xavfsizlik):** `META_APP_SECRET`ni HECH QACHON frontend/shablon
(HTML/JS) kodiga qo'ymang — u faqat `meta_api.py`ning server tomonidagi
`oauth_exchange_code()`/`oauth_exchange_long_lived()` funksiyalarida
ishlatiladi.

`TOKEN_ENCRYPTION_KEY`ni o'zgartirsangiz (yoki `FLASK_SECRET_KEY`ga
tayanayotgan bo'lsangiu-yu uni almashtirsangiz), ESKI shifrlangan
tokenlar o'qib bo'lmay qoladi — kompaniyalar Meta'ni QAYTA ulashi kerak
bo'ladi. Shuning uchun ishlab chiqarishga chiqqach, bu kalitni
o'zgartirmang.

## 8-qadam: Mijoz tomonidagi oqim (hech qanday sozlash kerak emas)

Yuqoridagi qadamlar FAQAT Replix administratori tomonidan (bir marta)
bajariladi. Har bir YANGI mijoz uchun oqim to'liq o'z-o'zidan:

1. **Sozlamalar → Akkauntlarni ulash → "Facebook orqali ulash"**.
2. Facebook login oynasi ochiladi, mijoz O'Z hisobiga kiradi, so'ralgan
   ruxsatlarni (yuqoridagi ro'yxat) tasdiqlaydi.
3. Agar bir nechta sahifa/Business Portfolio/reklama hisobi/Pixel
   topilsa — tanlov ekrani ko'rsatiladi (Business → Ad Account →
   Dataset/Pixel navbati bilan).
4. Saqlangach, "Sozlamalar → Akkauntlarni ulash" sahifasida
   **"Meta Ads: ULANGAN"** holati, tanlangan Business/Ad Account/
   Dataset nomlari va **"Test connection"**/**"Disconnect"** tugmalari
   ko'rinadi.

Mijoz Pixel ID, Graph API, CAPI yoki token haqida HECH NARSA bilishi
SHART EMAS.

### Test connection

"Test connection" tugmasi Meta'ning rasmiy **Test Events**
mexanizmidan foydalanadi (`test_event_code` maydoni,
`meta_api.send_conversion_event(..., test_event_code=...)`) — bu
hodisa HAQIQIY reklama statistikasiga/optimizatsiyaga ta'sir
qilmaydi, lekin Meta Events Manager → Data Sources → (Dataset) →
**Test events** bo'limida DARHOL "Server" manbai bilan ko'rinadi.
Test kodini shu bo'limdan olib, kelajakda maxsus test maydoniga
kiritish mumkin (hozircha kod ixtiyoriy, berilmasa oddiy — lekin
HAQIQIY statistikaga ta'sir qiluvchi — hodisa yuboriladi, bu ham
xavfsiz, chunki `send_conversion_event`ning o'zi faqat moslashtirish
uchun zarur maydonlarni yuboradi).

### Token muddati va qayta ulanish

Klassik OAuth uzun-muddatli foydalanuvchi tokeni ~60 kun amal qiladi
(Meta buni "kafolatlanmagan, erta tugashi mumkin" deb ochiq
ogohlantiradi). Meta xato kodi **190** (OAuthException) qaytsa,
Replix kompaniyaning holatini avtomatik `reauth_required`ga
o'tkazadi — "Sozlamalar → Akkauntlarni ulash" sahifasida qizil
"Meta ulanishi muddati tugagan" bandi va "Qayta ulash" tugmasi
darhol ko'rinadi (haftalab jim buzilib turmaydi).

### Advanced / Qo'lda sozlash (muddatsiz token)

60 kunlik OAuth tokeniga ishonchsiz bo'lgan yoki uni avtomatlashtirmoqchi
bo'lgan mijozlar uchun — Business Manager'ning O'ZI orqali MUDDATSIZ
**System User** tokeni yaratish mumkin (bu Meta App Review'dan
mustaqil, mijozning o'z Business Manager huquqi bilan amalga
oshiriladi):

1. Business Settings → Users → **System Users** → **Add** (yoki
   mavjudidan foydalaning).
2. Shu System User'ga kerakli Ad Account/Dataset'ga **Full Control**
   (yoki kamida Analyst) huquqini biriktiring (Assign Assets).
3. **Generate New Token** → ilovangizni (Replix App ID) tanlang →
   kerakli ruxsatlarni (`ads_management` yoki shunchaki CAPI uchun
   Pixel huquqi) belgilang → token yaratiladi (bu token MUDDATSIZ,
   qo'lda bekor qilinmaguncha amal qiladi).
4. Shu tokenni va Dataset (Pixel) ID'ni Replix'dagi "Sozlamalar →
   Akkauntlarni ulash → Advanced / Qo'lda sozlash" bo'limiga kiriting
   — saqlashdan OLDIN tizim Meta'ning o'ziga so'rov yuborib, ular
   TO'G'RILIGINI tekshiradi.

---

## Bosqich 2 (kelajakda, hozircha QURILMAGAN): Embedded Signup / BISU

Meta'ning SaaS platformalar uchun maxsus mo'ljallangan yechimi —
**"Facebook Login for Business"** mahsuloti + **Conversions API
Integration Template** — Embedded Signup (`FB.login()`,
`config_id`, `override_default_response_type: true`) orqali
**muddatsiz, faqat bitta mijoz biznesiga tegishli Business Integration
System User (BISU) tokeni** beradi. Bu klassik OAuth'dan farqli, chunki
token umuman muddati tugamaydi va mijoz hech qanday qo'lda token
yaratish qadamini bajarmaydi.

Bu QURILMAGAN, chunki quyidagilar Replix tomonidan (bir martalik,
inson ishtirokidagi, haftalar davom etadigan) qadamlarni talab qiladi:

1. Replix'ning O'Z Business Manager'ini **Business Verification**dan
   o'tkazish (yuqoridagi 5-qadam — lekin bu safar Advanced Access
   emas, aynan shu maxsus shablon uchun).
2. App Dashboard'da yangi **Configuration** yaratish (`config_id`) —
   Conversions API Integration Template ostida, faqat Verification'dan
   o'tgan Business'larga ochiladi.
3. `ads_management`, `business_management`, `ads_read`,
   `pages_read_engagement` ruxsatlariga aynan shu shablon ostida
   alohida App Review.

Yuqoridagilar bajarilgach, kod tomonida qo'shiladigan narsa: Embedded
Signup JS SDK tugmasi (`FB.login()` bilan `config_id`) va callback'da
`GET /me?fields=client_business_id`, `/owned_pixels`, `/client_pixels`
so'rovlari orqali BISU tokenini olish — bu klassik OAuth callback'ining
YONIDA (o'rniga emas) ishlaydi, chunki ba'zi mijozlar hali ham oddiy
tokenlarni afzal ko'rishi mumkin. Bitta yangi env o'zgaruvchi
(`META_LOGIN_CONFIG_ID`) qo'shilib, mavjud bo'lsa yangi tugma
ko'rsatiladi, bo'lmasa hozirgi oqim davom etadi — hech narsa
buzilmaydi.

---

## Xavfsizlik nazorat ro'yxati (allaqachon amalga oshirilgan)

- OAuth kod almashinuvi FAQAT serverda (`meta_api.oauth_exchange_code`),
  `META_APP_SECRET` HECH QACHON brauzerga chiqmaydi.
- CSRF himoyasi: `state` parametri Flask'ning imzolangan sessiyasida
  saqlanadi va callback'da solishtiriladi.
- Har bir kompaniya FAQAT o'z Meta tokenlari/ID'lari bilan ishlaydi —
  boshqa kompaniyaning ma'lumotlariga hech qachon fallback qilinmaydi
  (`_company_meta_creds()`, `test_meta_cross_tenant_isolation_offline.py`).
- Barcha tokenlar (`meta_access_token`, `meta_capi_access_token`)
  bazada Fernet bilan SHIFRLANGAN holda saqlanadi (`crypto_util.py`).
- Tokenlar HECH QACHON frontend'ga qaytarilmaydi (saqlangandan keyin
  faqat "•••••••••••••• saqlangan" ko'rsatiladi) va HECH QACHON log
  fayllariga yozilmaydi (`meta_api.safe_error_message()`).
- Manual/Advanced token+Dataset ID SAQLASHDAN OLDIN Meta'ning o'ziga
  haqiqiy so'rov bilan tekshiriladi (`verify_dataset_credentials`).
- CAPI orqali Meta'ga HECH QACHON qo'ng'iroq matni, AI tahlili, shaxsiy
  eslatmalar, tibbiy/moliyaviy ma'lumot yuborilmaydi — faqat
  moslashtirish uchun zarur maydonlar (telefon/email xeshlangan holda,
  ichki lead ID, ixtiyoriy fbp/fbc, sotuv summasi).
