# Replix — ish rejasi

> Har sessiya boshida shu faylni o'qib, **birinchi bajarilmagan** (`[ ]`) vazifadan davom etiladi.
> Bajarilgan vazifa `[x]` qilib belgilanadi. Qisman bajarilganlari ostida izoh qoldiriladi.

## Ish qoidalari (qisqa)
- Vazifa boshidan oxirigacha mustaqil bajariladi.
- To'xtab so'raladigan holatlar: bazaning tuzilishi o'zgarsa (migratsiya), real reklama akkauntida o'zgarish,
  mijozlar ko'radigan narx yoki matn o'zgarsa, kalit yoki qaror kerak bo'lsa.
- Har o'zgarishdan keyin `app/scripts/test_*.py` ishga tushiriladi; hammasi o'tsa — alohida aniq commit.
- Kalit/tokenlar hech qachon kodga yoki commitga yozilmaydi.
- Sessiya oxirida push + hisobot (nima qilindi, deploy, tekshiruv qadamlari, xato/rollback).

## 1-bosqich. Xavfsizlik va izolyatsiya
- [x] app/db.py dagi `_apply_tenant_scope` ni "yopiq holatda yiqiladigan" qil: company_id bo'lmasa so'rov rad etilsin
  - Kontekst yo'q → `TenantScopeError`. Filtrsiz o'qish faqat `db.unscoped()` orqali (login, cron ro'yxatlari, migratsiya).
  - Favqulodda zaxira: Render'da `TENANT_SCOPE_MODE=warn` → rad etish o'rniga ERROR log (kodni qaytarmasdan).
- [x] Filtrni update/delete so'rovlariga ham qo'lla
- [x] Barcha scheduler/cron ishlari aniq company_id bilan ishlashini tekshir va tuzat
  - `@db.company_scoped` dekoratori: sync_once (lead/call/ig/smm), CPL hard-kill, audit, admin hisobot.
  - Doimiy vazifalar/hisobotlar, CRM webhook, IG DM tahlil — har bir yozuv o'z kompaniyasi kontekstida.
  - Telegram webhook va fon oqimlari (thread) chat egasi kompaniyasi kontekstida.
  - Dalil: `scripts/test_fail_closed_production_paths_offline.py` (barcha 19 fon vazifasi + 59 sahifa qat'iy rejimda).
- [x] Har bir kompaniyaning Meta tokeni shifrlanganini tekshir
  - Barcha maxfiy ustunlar (Meta/CAPI token, Moi Zvonki, Payme) Fernet bilan shifrlangan, sahifalarda faqat •••.
  - Eski ochiq matnli qiymatlar ishga tushishda bir marta shifrlanadi (`db.encrypt_legacy_plaintext_secrets`, idempotent).
  - MultiFernet: `TOKEN_ENCRYPTION_KEY` keyin qo'shilsa ham eski shifrlar o'qiladi.
- [x] A kompaniya B'ning lidlari, kampaniyalari, menejerlari va tokenlarini ko'rmasligini tekshiruvchi testlar
  - `scripts/test_tenant_isolation_a_vs_b_offline.py` (production rejimida, admin + menejer, 59 sahifa, ijobiy nazorat bilan).
- [x] Telegram webhook secret va bot kirish huquqlarini tekshir
  - `TELEGRAM_WEBHOOK_SECRET` yo'q bo'lsa ham ilova barqaror secret hosil qilib, mavjud webhook URL'iga avtomatik qo'shadi; so'ng secret'siz so'rov → 403.
  - Egasi buyruqlari (/pause, /resume, /analyze, /status) faqat egasi chatida; yot chat hech bir kompaniya ma'lumotini ko'rmaydi.
  - Dalil: `scripts/test_telegram_webhook_secret_offline.py`.

## 2-bosqich. Meta, Avtopilot, CAPI
- [x] Meta'ning har bir javobi va xatosini log qilish, foydalanuvchiga tushunarli ko'rsatish
  - Har chaqiruv bitta qator log (`META POST act_.../campaigns -> HTTP 400 ... code= subcode= fbtrace_id=`), token hech qachon logda yo'q.
  - `meta_api.friendly_meta_error` — yagona o'zbekcha tarjima (+ "kod 100/4834011"), Target/Telegram/avtopilot bir xil.
  - Dalil: `scripts/test_meta_error_logging_offline.py`.
- [x] Graph API versiyasini v21.0 dan yangilash
  - Standart `v25.0` (muddati 2028-07); Render'da `META_GRAPH_API_VERSION` bilan almashtiriladi.
- [x] Kampaniya yaratishda `is_adset_budget_sharing_enabled` maydonini qo'shish (reklama Ads Manager'ga qoralama sifatida ham tushmayapti)
  - Har doim yuboriladi (standart `False`). Dalil: `scripts/test_meta_campaign_payload_offline.py`.
  - ⚠️ Real akkauntda tekshirish egasi bilan birga (PAUSED holatda) qilinadi.
- [x] Avtopilot zanjirini (savol-javob, reja, tasdiq, PAUSED holatda chiqarish) tekshirish
  - Tekshirildi: savol-javob → AI reja → 3 ta tasdiq (kampaniya/adset/reklama) → faqat admin nashr qiladi; tahrir tegishli tasdiqni bekor qiladi; A kompaniya B qoralamasiga kira olmaydi (mavjud testlar).
  - Qaror: standart PAUSED (2026-10-01) — bajarildi.
- [x] CAPI signallari (yangi lid, sifatli lid, sotuv) ishlashini tekshirish
  - Lead / QualifiedLead / Purchase to'g'ri joylarda, event_id bilan (dublikatsiz), telefon/email SHA-256.
  - Qo'shildi: Lead Ads lidlari uchun `custom_data.event_source="crm"` + `lead_event_source` (Meta Conversion Leads optimizatsiyasi uchun).
  - Dalil: `scripts/test_capi_signals_offline.py`.

## 3-bosqich. Kreativ studiya
- [x] AI faqat fon yoki obyektni chizsin, matn, logo va narxni shablon ustiga kod qo'ysin
  - Allaqachon shunday edi: AI'ga "no text/logo/numbers" qat'iy ko'rsatma, matn/logo/narx Pillow qatlamlari (20 shablonning hammasi testlangan).
- [x] Brend kit asosida 5–8 shablon (1:1, 4:5, 9:16), karusel
  - 20 ta mavjud shablon + **6 ta karusel uslubi** (Yorqin gradient, Minimal oq, Qora hashamat, Ikki rangli, Qadamlar, Stiker pop) — brend ranglari bilan, real namuna lentalari galereyada.
  - Karusel: 3–6 karta (ilgak → afzalliklar → CTA), OpenAI rasm kvotasi sarflanmaydi, ZIP yuklab olish, har karta alohida tahrirlanadi.
  - 9:16 (Stories/Reels) xavfsiz zonasi: matn/logo/CTA platforma interfeysi ostiga tushmaydi.
  - Dalil: `scripts/test_creative_carousel_offline.py`.
  - Keyingi qadam (kelajak): karuselni Avtopilot orqali Meta karusel reklamasi sifatida chiqarish.
- [x] Sifat tayyor bo'lguncha "beta" deb belgilash
  - Kreativ studiya sahifalari, karusel va chap menyuda "BETA" belgisi.

## 4-bosqich. CPL avtopilot va hisobotlar
- [x] 3 daraja: ogohlantirish, pauza, harakat (byudjet ±10–20%, auditoriya kengaytirish, zaif reklamani o'chirish). Biznes egalari uchun sukut bo'yicha pauza, targetologlar uchun harakat faqat o'zi yoqsa
- [x] Minimal ma'lumot chegarasi, kuniga maksimal o'zgarishlar soni, har harakat logda va orqaga qaytariladigan
- [x] Har kompaniyaga alohida kunlik Telegram hisobot
- [x] (4-bosqichdan keyin) Karuselni Avtopilot orqali Meta karusel reklamasi sifatida chiqarish — faqat PAUSED (kod tayyor; **birinchi real akkaunt sinovi egasi bilan qoladi**)

## 5-bosqich. KPI va agentlik
- [x] `kpi_bonus.py` dagi Dunyabunya qoidalarini (oklad 4 000 000 so'm va bonuslar) har kompaniya o'zi sozlaydigan qilish
- [ ] Agentlik rejimi: bitta targetolog bir nechta mijoz kompaniyasini bitta logindan boshqarsin

## 6-bosqich. Instagram
- [ ] Kommentariya va Direct'ga javob berish (`instagram_manage_comments`, `instagram_manage_messages`)
- [x] Kalit so'z bo'yicha avtojavob, issiq lidni CRM'ga va Telegram'ga (Instagram xabarlar sahifasida; standart o'chiq, avtojavob faqat egasi matn yozsa)
- [ ] Post, karusel, Reels joylash

## 7-bosqich. Tariflar va sayt
- [ ] `plans.py`: Sinov 7 kun bepul; Start $29 (1 akkaunt, 2 menejer); Biznes $69 (1–2 akkaunt, 10 menejer); Agentlik $99 (5 mijoz akkaunti, har qo'shimchasi +$15); yillik to'lovda 2 oy bepul. **O'zgartirishdan oldin egasiga ko'rsatish shart**
- [x] Bosh sahifada ikki yo'l: "Men biznes egasiman" va "Men targetologman"
- [x] Qo'llanma bo'limi

## Qo'shimcha bajarilganlar (rejadan tashqari)
- [x] (2026-10-01) 4-bosqich: CPL rejimi (faqat ogohlantirish / avtomatik pauza) Sozlamalar → CPL'da; kuniga ko'pi bilan 10 ta avto-pauza; Dashboard'da "Qayta yoqish" (jurnalda resumed); harakat darajasi = "AI avtomatik kuzatuv" (egasi yoqsa), AI byudjetni sutkada 1 marta oshiradi, oylik shift, yangi kampaniya doim PAUSED.
- [x] (2026-10-01) 5-bosqich: KPI/bonus qoidalari Sozlamalar → Umumiy → "KPI va bonus qoidalari"da (oklad, 1/2-xarid bonusi, %, oyna, reja, sotuv/oborot bosqichlari); standart = Dunyabunya, natija aynan avvalgidek.
- [x] (2026-10-01) Instagram raqobatchi tahlili (Business Discovery API) + Ad Library havolalari.
- [x] (2026-10-01) Narxlar: har tarif yonida Payme yechadigan so'm summasi; "--" → "—".
- [x] Dizayn: logo 2 marta chiqishi, login til tanlagichi, telefon sarlavhasi, bo'sh sidebar qutisi, dashboard grafik o'qi, lidlar qidiruvi — tuzatildi (2026-09-30).
- [x] Sinov muddati "N kun qoldi" yaxlitlash xatosi tuzatildi.
- [x] (2026-10-02) Shablon va karusel namunalari haqiqiy professional suratlarda (Shopify Burst — tijorat uchun bepul, `static/creative_templates/photos/CREDITS.md`); matn surat ustida doim o'qiladi (avtomatik scrim + soya, WCAG kontrast). Dalil: `scripts/test_creative_scrim_offline.py`.
- [x] (2026-10-03) Raqobatchilar: Ad Library veb-sahifasidan o'qish (`adlib_scraper.py`, `ADLIB_SCRAPER=1`) — qidiruvda video/rasm kartochkalar, qachondan beri ishlayotgani; tahlil AI'siz (`competitor_insights.py`). Dalil: `scripts/test_adlib_scraper_offline.py`, `scripts/test_competitor_insights_offline.py`.
- [x] (2026-10-02) Dizayn auditi (150 sahifa ko'rinishi): Target jadvali sarlavhalari o'zbekchada, voronka ranglari namuna bilan, kirgan foydalanuvchi /login'da dashboard'ga o'tadi, telefonda AI tugmasi pastki tugmalarni yopmaydi.

## ✅ Qarorlar (2026-10-01, egasi qarorni hamkorga topshirdi)
1. **Avtopilot nashri standart PAUSED** — pul sarfi faqat egasi "Faollashtirish" bosganda. Review oynasida "Darhol yoqilsin" tanlash mumkin. (Bajarildi.)
2. **FAQ/landing narxi** — endi `plans.py`dan avtomatik (`{min_price}`): hozir "$20 dan". 7-bosqichda narx o'zgarsa o'zi yangilanadi. (Bajarildi.)
3. **Sinov 7 kun** (avval 5) — Meta "o'rganish davri" 3–5 kun, mijoz natijani ko'rib ulgursin. Barcha matnlar `{trial_days}`dan. (Bajarildi.)
4. **Sinovda Meta reklama hisobini ulash ochiq** — asosiy qiymat (o'z natijalari + lidlar) sinovda ko'rinsin; AI kvotalari o'zgarmadi. (Bajarildi.)
5. **Karusel → Avtopilot (Meta karusel reklamasi)** — HA, lekin 4-bosqich (CPL himoyasi) tugagach; faqat PAUSED, birinchi sinov egasi bilan real akkauntda. (Rejada: 4-bosqichdan keyin.)
