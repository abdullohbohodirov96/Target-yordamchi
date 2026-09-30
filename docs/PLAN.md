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
- [ ] Telegram webhook secret va bot kirish huquqlarini tekshir

## 2-bosqich. Meta, Avtopilot, CAPI
- [ ] Meta'ning har bir javobi va xatosini log qilish, foydalanuvchiga tushunarli ko'rsatish
- [ ] Graph API versiyasini v21.0 dan yangilash
- [ ] Kampaniya yaratishda `is_adset_budget_sharing_enabled` maydonini qo'shish (reklama Ads Manager'ga qoralama sifatida ham tushmayapti)
- [ ] Avtopilot zanjirini (savol-javob, reja, tasdiq, PAUSED holatda chiqarish) tekshirish
- [ ] CAPI signallari (yangi lid, sifatli lid, sotuv) ishlashini tekshirish

## 3-bosqich. Kreativ studiya
- [ ] AI faqat fon yoki obyektni chizsin, matn, logo va narxni shablon ustiga kod qo'ysin
- [ ] Brend kit asosida 5–8 shablon (1:1, 4:5, 9:16), karusel
- [ ] Sifat tayyor bo'lguncha "beta" deb belgilash

## 4-bosqich. CPL avtopilot va hisobotlar
- [ ] 3 daraja: ogohlantirish, pauza, harakat (byudjet ±10–20%, auditoriya kengaytirish, zaif reklamani o'chirish). Biznes egalari uchun sukut bo'yicha pauza, targetologlar uchun harakat faqat o'zi yoqsa
- [ ] Minimal ma'lumot chegarasi, kuniga maksimal o'zgarishlar soni, har harakat logda va orqaga qaytariladigan
- [ ] Har kompaniyaga alohida kunlik Telegram hisobot

## 5-bosqich. KPI va agentlik
- [ ] `kpi_bonus.py` dagi Dunyabunya qoidalarini (oklad 4 000 000 so'm va bonuslar) har kompaniya o'zi sozlaydigan qilish
- [ ] Agentlik rejimi: bitta targetolog bir nechta mijoz kompaniyasini bitta logindan boshqarsin

## 6-bosqich. Instagram
- [ ] Kommentariya va Direct'ga javob berish (`instagram_manage_comments`, `instagram_manage_messages`)
- [ ] Kalit so'z bo'yicha avtojavob, issiq lidni CRM'ga va Telegram'ga
- [ ] Post, karusel, Reels joylash

## 7-bosqich. Tariflar va sayt
- [ ] `plans.py`: Sinov 7 kun bepul; Start $29 (1 akkaunt, 2 menejer); Biznes $69 (1–2 akkaunt, 10 menejer); Agentlik $99 (5 mijoz akkaunti, har qo'shimchasi +$15); yillik to'lovda 2 oy bepul. **O'zgartirishdan oldin egasiga ko'rsatish shart**
- [ ] Bosh sahifada ikki yo'l: "Men biznes egasiman" va "Men targetologman"
- [ ] Qo'llanma bo'limi
