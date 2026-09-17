/* autopilot.js — Meta Ads Autopilot ko'rib chiqish sahifasi (2026-09,
   foydalanuvchi so'rovi: "Replix ... Ads Manager'dagi har bir maydonni
   boshidan o'zi to'ldirmasligi kerak").

   Vanilla JS, freymvorksiz, CDN'siz. Tuzilma:
     1. store      -- BITTA kanonik qoralama (`#ap-data` JSON'idan); server
                      har javobda to'liq yangilangan `draft`ni qaytaradi va
                      biz uni butunlay almashtiramiz (forma, chat, tasdiq --
                      hammasi bitta haqiqat manbai).
     2. api()      -- fetch + CSRF (X-CSRFToken) + o'zbekcha xato.
     3. patch navbati -- qo'lda kiritilgan har bir maydon LOKAL holatga
                      darhol yoziladi (UI qotmaydi), 600 ms dan keyin
                      scope bo'yicha guruhlanib `POST /patch`ga ketadi
                      (server: allowlist -> validate -> tasdiqni bekor
                      qilish -> audit). Chat (`POST /ai-edit`) ham aynan
                      shu server quvuridan o'tadi -- ikki xil kirish, bitta
                      holat.
     4. render*()  -- topbar / daraxt / tablar / daraja formalari / chat /
                      pastki panel; har maydonda manba belgisi ("✨ AI" /
                      "Siz o'zgartirdingiz" / "Meta"), AI tushuntirishi va
                      ishonch %, inline tekshiruv xatolari.
     5. modallar   -- yakuniy xulosa (nashr), faollashtirish, preview. */
(function () {
  'use strict';

  var root = document.getElementById('ap-app');
  if (!root) { return; }
  var CSRF = root.dataset.csrf;
  var IS_ADMIN = root.dataset.isAdmin === '1';
  var CONNECT_URL = root.dataset.connectUrl;
  var PRICING_URL = root.dataset.pricingUrl;
  var LIST_URL = root.dataset.listUrl;
  // 2026-09, Kreativ studiya: media bo'limidagi "Kreativ studiyadan tanlash"
  // havolasi (galereya `?from_autopilot=<id>` bilan ochiladi, tanlangan rasm
  // `POST /avtopilot/<id>/media/from-kreativ` orqali qaytadi).
  var CREATIVE_URL = root.dataset.creativeUrl;

  var store = {
    draft: JSON.parse(document.getElementById('ap-data').textContent),
    tab: 'campaign',
    chat: [],
    saving: 'idle',   // idle | saving | saved | error
    saveError: null,
    pending: {},      // scope -> {path: value}
    timer: null,
    inflight: false,
    previewFmt: 'instagram_feed',
    previewCache: {},
    typeahead: {},    // key -> results
    // 2026-09: Instant Form "yangi savol" kompozitori -- HALI tasdiqlanmagan
    // qoralama (type/label/options). Faqat lokal: serverga "Qo'shish"
    // bosilgandagina ketadi. null = yopiq. `store`da saqlanadi, chunki
    // fon autosave javobi butun editorni qayta chizadi -- yozilayotgan
    // matn yo'qolmasin.
    lfComposer: null
  };
  var LEVELS = ['campaign', 'adset', 'ad'];
  var LEVEL_LABELS = { campaign: 'Kampaniya', adset: 'Ad Set', ad: 'Reklama' };
  var PREVIEW_LABELS = { facebook_feed: 'Facebook lenta', facebook_mobile: 'Facebook mobil', instagram_feed: 'Instagram lenta', instagram_story: 'Instagram Story', instagram_reels: 'Instagram Reels' };
  var PLATFORM_LABELS = { facebook: 'Facebook', instagram: 'Instagram', messenger: 'Messenger', audience_network: 'Audience Network' };
  var FB_POS_LABELS = { feed: 'Lenta', story: 'Story', reels: 'Reels', marketplace: 'Marketplace', video_feeds: 'Video lenta', search: 'Qidiruv' };
  var IG_POS_LABELS = { stream: 'Lenta', story: 'Story', reels: 'Reels', explore: 'Explore' };
  var SPECIAL_LABELS = { NONE: 'Yo\'q', HOUSING: 'Uy-joy', EMPLOYMENT: 'Ish', CREDIT: 'Kredit', ISSUES_ELECTIONS_POLITICS: 'Siyosat', FINANCIAL_PRODUCTS_SERVICES: 'Moliya' };
  // 2026-09: savol turi yorliqlari endi SERVERDAN keladi (`d.options.lead_question_types`,
  // `campaign_draft.LEAD_QUESTION_TYPE_LABELS` -- to'liq Ads Manager ro'yxati), qarang `renderLeadFormEditor()`.
  var VALIDATION_FIELD_MAP = {
    // validate_state `field` -> forma yo'li (inline xato ko'rsatish uchun)
    'campaign.name': 'campaign.name', 'campaign.objective': 'objective', 'campaign.special_ad_categories': 'campaign.special_ad_categories', 'campaign.pixel': 'campaign.pixel', 'campaign.meta_objective': 'objective',
    'adset.name': 'adset.name', 'adset.daily_budget': 'adset.daily_budget', 'adset.lifetime_budget': 'adset.lifetime_budget', 'adset.currency': 'adset.daily_budget',
    'adset.start_time': 'adset.start_time', 'adset.end_time': 'adset.end_time', 'adset.optimization_goal': 'adset.optimization_goal', 'adset.bid_strategy': 'adset.bid_strategy',
    'adset.destination_type': 'adset.destination_type', 'adset.age': 'adset.targeting.age_min', 'adset.genders': 'adset.targeting.genders',
    'adset.geo_locations': 'adset.targeting.geo_locations', 'adset.placements': 'adset.targeting.placements', 'adset.custom_audiences': 'adset.targeting.custom_audiences',
    'adset.excluded_custom_audiences': 'adset.targeting.excluded_custom_audiences',
    'ad.name': 'ad.name', 'ad.page_id': 'ad.page_id', 'ad.instagram_actor_id': 'ad.instagram_actor_id', 'ad.media': 'ad.media', 'ad.primary_text': 'ad.primary_text',
    'ad.headline': 'ad.headline', 'ad.cta': 'ad.cta', 'ad.link_url': 'ad.link_url', 'ad.messages.greeting': 'ad.messages.greeting',
    'ad.lead_form': 'ad.lead_form.existing_form_id', 'ad.lead_form.name': 'ad.lead_form.new_form.name', 'ad.lead_form.questions': 'ad.lead_form.new_form.questions',
    'ad.lead_form.privacy_url': 'ad.lead_form.new_form.privacy_url'
  };

  // ------------------------------------------------------------------
  // Yordamchilar
  // ------------------------------------------------------------------
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function h(tag, attrs, children) {
    var el = document.createElement(tag);
    attrs = attrs || {};
    Object.keys(attrs).forEach(function (k) {
      var v = attrs[k];
      if (v == null || v === false) { return; }
      if (k === 'class') { el.className = v; }
      else if (k === 'html') { el.innerHTML = v; }
      else if (k === 'text') { el.textContent = v; }
      else if (k.indexOf('on') === 0 && typeof v === 'function') { el.addEventListener(k.slice(2), v); }
      else if (k === 'dataset') { Object.keys(v).forEach(function (d) { el.dataset[d] = v[d]; }); }
      else if (k === 'disabled' || k === 'checked' || k === 'selected' || k === 'hidden' || k === 'required') { el[k] = !!v; }
      else if (k === 'value') { el.value = v; }
      else { el.setAttribute(k, v === true ? '' : v); }
    });
    (children || []).forEach(function (c) {
      if (c == null || c === false) { return; }
      if (Array.isArray(c)) { c.forEach(function (cc) { if (cc) { el.appendChild(typeof cc === 'string' ? document.createTextNode(cc) : cc); } }); }
      else { el.appendChild(typeof c === 'string' ? document.createTextNode(c) : c); }
    });
    return el;
  }
  function getPath(obj, path) {
    var node = obj;
    var parts = path.split('.');
    for (var i = 0; i < parts.length; i++) { if (node == null || typeof node !== 'object') { return undefined; } node = node[parts[i]]; }
    return node;
  }
  function setPath(obj, path, value) {
    var parts = path.split('.');
    var node = obj;
    for (var i = 0; i < parts.length - 1; i++) { if (node[parts[i]] == null || typeof node[parts[i]] !== 'object') { node[parts[i]] = {}; } node = node[parts[i]]; }
    node[parts[parts.length - 1]] = value;
  }
  function fmtMoney(v, cur) {
    if (v == null || v === '') { return '—'; }
    var n = Number(v);
    if (isNaN(n)) { return String(v); }
    return n.toLocaleString('ru-RU').replace(/,/g, ' ') + ' ' + (cur || '');
  }
  function fmtTime(iso) {
    if (!iso) { return '—'; }
    var d = new Date(iso);
    if (isNaN(d.getTime())) { return iso; }
    var p = function (n) { return (n < 10 ? '0' : '') + n; };
    return p(d.getDate()) + '.' + p(d.getMonth() + 1) + '.' + d.getFullYear() + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }
  function toLocalInput(iso) {
    if (!iso) { return ''; }
    return String(iso).slice(0, 16);
  }
  function state() { return store.draft.state; }
  function editable() { return IS_ADMIN && store.draft.is_editable; }
  function sourceOf(path) { return (store.draft.field_sources || {})[path]; }
  function svgIcon(name) {
    var paths = {
      check: '<path stroke-linecap="round" stroke-linejoin="round" d="M5 12l4 4L19 7"/>',
      x: '<path stroke-linecap="round" d="M6 6l12 12M18 6L6 18"/>',
      campaign: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M7 9h10M7 13h6"/>',
      adset: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/>',
      ad: '<rect x="4" y="5" width="16" height="14" rx="2"/><path d="M4 15l4-4 4 4 3-3 5 5"/>',
      refresh: '<path stroke-linecap="round" stroke-linejoin="round" d="M4 4v6h6M20 20v-6h-6M5 15a7 7 0 0011.9 3.1M19 9A7 7 0 007.1 5.9"/>',
      eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
      upload: '<path stroke-linecap="round" stroke-linejoin="round" d="M12 16V4m0 0l-4 4m4-4l4 4"/><path d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2"/>',
      plus: '<path stroke-linecap="round" d="M12 5v14M5 12h14"/>',
      rocket: '<path d="M4.5 16.5c-1.5 1.3-2 5-2 5s3.7-.5 5-2c.7-.8.7-2 0-2.7-.7-.7-2-.7-3 0z"/><path d="M12 15l-3-3 2.5-5.5A9 9 0 0121.5 2.5 9 9 0 0117.5 12.5L12 15z"/>',
      warn: '<path d="M12 9v4m0 4h.01M10.3 3.9L2.5 17.5A2 2 0 004.2 20.5h15.6a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"/>'
    };
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">' + (paths[name] || '') + '</svg>';
  }

  // ------------------------------------------------------------------
  // API
  // ------------------------------------------------------------------
  function api(path, opts) {
    opts = opts || {};
    var url = store.draft.base_url + (path || '');
    var init = { method: opts.method || 'GET', credentials: 'same-origin', headers: { 'X-CSRFToken': CSRF } };
    if (opts.body instanceof FormData) { init.body = opts.body; }
    else if (opts.body != null) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.body); }
    return fetch(url, init).then(function (r) {
      return r.json().catch(function () { return { error: 'Server javobi tushunarsiz (' + r.status + ').' }; }).then(function (json) {
        if (json && json.draft) { replaceDraft(json.draft); }
        if (!r.ok) { var err = new Error(json.error || 'Xatolik (' + r.status + ')'); err.payload = json; err.status = r.status; throw err; }
        return json;
      });
    });
  }

  function replaceDraft(draft) {
    // Hali yuborilmagan (navbatdagi) qiymatlar yo'qolmasin
    var keep = [];
    Object.keys(store.pending).forEach(function (scope) {
      Object.keys(store.pending[scope]).forEach(function (p) { keep.push([p, store.pending[scope][p]]); });
    });
    store.draft = draft;
    keep.forEach(function (kv) { setPath(store.draft.state, kv[0], kv[1]); });
    render();
  }

  // ------------------------------------------------------------------
  // Patch navbati (qo'lda tahrir)
  // ------------------------------------------------------------------
  function queueChange(scope, path, value, opts) {
    if (!editable()) { return; }
    opts = opts || {};
    setPath(store.draft.state, path, value);
    store.draft.field_sources[path] = 'USER_OVERRIDDEN';
    store.draft.approvals[scope === 'objective' ? 'campaign' : scope] = false;
    if (scope === 'objective') { store.draft.approvals.adset = false; store.draft.approvals.ad = false; }
    store.pending[scope] = store.pending[scope] || {};
    store.pending[scope][path] = value;
    store.saving = 'saving';
    renderTopbar(); renderTree(); renderBottombar();
    clearTimeout(store.timer);
    store.timer = setTimeout(flushPending, opts.immediate ? 0 : 600);
    if (opts.rerender) { renderEditor(); }
  }

  function flushPending() {
    // 2026-09: promise qaytaradi (avval yo'q edi) -- "Qoralama sifatida
    // saqlash" tugmasi hali yuborilmagan (debounce navbatidagi) o'zgarishlar
    // ketishini KUTIB, keyin aniq tasdiqlash so'rovini yuborishi uchun
    // (mavjud chaqiruvchilar -- `setTimeout(flushPending, ...)` -- qaytgan
    // qiymatga e'tibor bermaydi, xatti-harakati o'zgarmaydi).
    if (store.inflight) { clearTimeout(store.timer); store.timer = setTimeout(flushPending, 300); return Promise.resolve(); }
    var scopes = Object.keys(store.pending);
    if (!scopes.length) { return Promise.resolve(); }
    var scope = scopes[0];
    var changes = store.pending[scope];
    delete store.pending[scope];
    store.inflight = true;
    return api('/patch', { method: 'POST', body: { scope: scope, changes: changes } })
      .then(function () { store.saving = Object.keys(store.pending).length ? 'saving' : 'saved'; store.saveError = null; })
      .catch(function (e) {
        store.saving = 'error'; store.saveError = e.message;
        chatSystem('Saqlanmadi: ' + e.message, 'err');
      })
      .then(function () {
        store.inflight = false;
        renderTopbar(); renderTree(); renderBottombar(); renderEditor();
        if (Object.keys(store.pending).length) { return flushPending(); }
      });
  }

  // 2026-09, foydalanuvchi so'rovi: kampaniyani to'liq tekshiruv/nashrdan
  // o'tkazmasdan turib ham "aniq saqlandi" deb ANIQ bilish imkoni. Forma/
  // chat tahriri ALLAQACHON avtomatik saqlanadi (yuqoridagi debounce +
  // `/patch`), lekin bu passiv va ko'zga tashlanmasligi mumkin -- shu
  // tugma HAL QILUVCHI harakat: avval navbatdagi o'zgarishlarni darhol
  // yuboradi, so'ng serverga ANIQ tasdiqlash so'rovini (`/save-draft`)
  // yuboradi va chatda ko'rinadigan tasdiq ko'rsatadi.
  function saveDraftExplicit(btn) {
    var original = btn.textContent;
    btn.disabled = true; btn.textContent = 'Saqlanmoqda…';
    clearTimeout(store.timer);
    Promise.resolve(flushPending()).then(function () {
      return api('/save-draft', { method: 'POST', body: {} });
    }).then(function () {
      btn.disabled = false; btn.textContent = original;
      chatSystem('✓ Qoralama saqlandi. Istalgan payt "Avtopilot" ro\'yxatidan qaytib kirib davom ettirishingiz mumkin -- hech narsa yo\'qolmaydi.');
    }).catch(function (e) {
      btn.disabled = false; btn.textContent = original;
      chatSystem('Saqlashda xato: ' + e.message, 'err');
    });
  }

  // ------------------------------------------------------------------
  // Tekshiruv xatolari
  // ------------------------------------------------------------------
  function errorsForScope(scope) { return (store.draft.validation || []).filter(function (e) { return e.scope === scope; }); }
  function errorsForPath(path) {
    return (store.draft.validation || []).filter(function (e) {
      var key = VALIDATION_FIELD_MAP[e.scope + '.' + e.field];
      if (key === path) { return true; }
      return !key && (e.scope + '.' + e.field) === path;
    }).map(function (e) { return e.message; });
  }

  // ------------------------------------------------------------------
  // Umumiy forma bo'laklari
  // ------------------------------------------------------------------
  function srcBadge(path) {
    var src = sourceOf(path);
    if (!src) { return null; }
    var cls = src === 'AI_RECOMMENDED' ? 'ap-src-ai' : (src === 'USER_OVERRIDDEN' ? 'ap-src-user' : 'ap-src-meta');
    var label = (store.draft.source_labels || {})[src] || src;
    var conf = ((store.draft.ai_plan || {}).confidence || {})[path];
    var wrap = h('span', { class: 'ap-inline-actions' }, [h('span', { class: 'ap-src ' + cls, text: label })]);
    if (src === 'AI_RECOMMENDED' && conf != null) { wrap.appendChild(h('span', { class: 'ap-src-conf', text: 'ishonch ' + conf + '%' })); }
    return wrap;
  }
  function fieldWrap(path, label, control, opts) {
    opts = opts || {};
    var errs = opts.errors || errorsForPath(path);
    var explain = ((store.draft.ai_plan || {}).explanations || {})[path];
    var head = h('div', { class: 'ap-field-head' }, [h('span', { class: 'ap-field-label', text: label }), h('span', { class: 'ap-inline-actions' }, [srcBadge(path), opts.actions])]);
    var el = h('div', { class: 'ap-field' + (errs.length ? ' has-error' : ''), dataset: { field: path } }, [head, control]);
    if (opts.hint) { el.appendChild(h('div', { class: 'ap-field-hint', text: opts.hint })); }
    if (explain && sourceOf(path) === 'AI_RECOMMENDED') { el.appendChild(h('div', { class: 'ap-field-explain', text: '✨ ' + explain })); }
    errs.forEach(function (m) { el.appendChild(h('div', { class: 'ap-field-error', text: m })); });
    return el;
  }
  function textInput(scope, path, opts) {
    opts = opts || {};
    var value = getPath(state(), path);
    var attrs = { type: opts.type || 'text', value: value == null ? '' : value, disabled: !editable() || opts.disabled, placeholder: opts.placeholder, dataset: { path: path } };
    if (opts.min != null) { attrs.min = opts.min; }
    if (opts.max != null) { attrs.max = opts.max; }
    if (opts.step != null) { attrs.step = opts.step; }
    if (opts.maxlength) { attrs.maxlength = opts.maxlength; }
    if (opts.textarea) { delete attrs.type; }
    var el = h(opts.textarea ? 'textarea' : 'input', attrs);
    if (opts.textarea) { el.value = value == null ? '' : value; if (opts.rows) { el.rows = opts.rows; } }
    el.addEventListener('input', function () {
      var v = el.value;
      if (opts.type === 'number') { v = v === '' ? null : Number(v); if (v != null && isNaN(v)) { return; } }
      if (opts.type === 'datetime-local') { v = v ? v + ':00' : null; }
      if (opts.nullable && v === '') { v = null; }
      queueChange(scope, path, v);
    });
    return el;
  }
  function selectInput(scope, path, options, opts) {
    opts = opts || {};
    var value = getPath(state(), path);
    var el = h('select', { disabled: !editable() || opts.disabled, dataset: { path: path } });
    if (opts.allowEmpty) { el.appendChild(h('option', { value: '', text: opts.emptyLabel || '—' })); }
    options.forEach(function (o) {
      var op = h('option', { value: o.value, text: o.label, disabled: o.disabled });
      if (String(o.value) === String(value == null ? '' : value)) { op.selected = true; }
      el.appendChild(op);
    });
    el.addEventListener('change', function () { queueChange(scope, path, el.value === '' ? null : el.value, { immediate: true, rerender: opts.rerender }); });
    return el;
  }
  function checkbox(label, checked, onchange, disabled) {
    var input = h('input', { type: 'checkbox', checked: checked, disabled: disabled || !editable() });
    input.addEventListener('change', function () { onchange(input.checked); });
    return h('label', { class: 'ap-check' + (disabled ? ' disabled' : '') }, [input, label]);
  }
  function miniBtn(label, onclick, opts) {
    opts = opts || {};
    var b = h('button', { type: 'button', class: 'ap-mini-btn' + (opts.cls ? ' ' + opts.cls : ''), disabled: opts.disabled || (!opts.always && !editable()), title: opts.title });
    if (opts.icon) { b.innerHTML = svgIcon(opts.icon) + ' '; b.querySelector('svg').style.cssText = 'width:12px;height:12px;vertical-align:-2px'; }
    b.appendChild(document.createTextNode(label));
    b.addEventListener('click', onclick);
    return b;
  }
  function unsupportedRow(item) {
    return h('div', { class: 'ap-unsupported' }, [h('span', { text: item.label }), h('span', { text: item.note })]);
  }
  // 2026-09, foydalanuvchi so'rovi ("interfeys qiyinlashib qolgan"): "Ads
  // Manager'da bor, lekin API orqali boshqarilmaydi" ro'yxati endi
  // tahrirlanadigan maydonlar bilan bir xil og'irlikda turmaydi -- yig'ilgan
  // (<details>), xira, kerak bo'lsa ochiladi. Ma'lumot yo'qolmadi, faqat
  // ikkinchi darajaga tushdi.
  function unsupportedBlock(items) {
    if (!items || !items.length) { return null; }
    var det = h('details', { class: 'ap-unsupported-block' }, [
      h('summary', { text: 'Ads Manager\'da bor, lekin Meta API orqali boshqarilmaydi (' + items.length + ')' })
    ]);
    items.forEach(function (u) { det.appendChild(unsupportedRow(u)); });
    return det;
  }
  function sectionTitle(text, extra) { return h('h3', { class: 'ap-section-title' }, [text, extra]); }

  // Typeahead (hudud / qiziqish) -- Meta qidiruvi orqali; uydirma key yo'q
  function typeahead(endpoint, placeholder, onPick) {
    var wrap = h('div', { class: 'ap-typeahead' });
    var input = h('input', { type: 'text', placeholder: placeholder, autocomplete: 'off', disabled: !editable() });
    var list = h('div', { class: 'ap-typeahead-list', hidden: true });
    var timer = null;
    input.addEventListener('input', function () {
      clearTimeout(timer);
      var q = input.value.trim();
      if (q.length < 2) { list.hidden = true; return; }
      timer = setTimeout(function () {
        fetch(endpoint + '?q=' + encodeURIComponent(q), { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (data) {
          list.innerHTML = '';
          var results = data.results || [];
          if (!results.length) { list.appendChild(h('div', { class: 'ap-typeahead-empty', text: data.error || 'Topilmadi' })); list.hidden = false; return; }
          results.forEach(function (r) {
            var item = h('div', { class: 'ap-typeahead-item', text: r.name + (r.region ? ', ' + r.region : '') + (r.country_code ? ' (' + r.country_code + ')' : '') + (r.type ? ' — ' + r.type : '') + (r.audience_size_lower_bound ? ' · ~' + Number(r.audience_size_lower_bound).toLocaleString('ru-RU') : '') });
            item.addEventListener('mousedown', function (e) { e.preventDefault(); input.value = ''; list.hidden = true; onPick(r); });
            list.appendChild(item);
          });
          list.hidden = false;
        }).catch(function () { list.hidden = true; });
      }, 250);
    });
    input.addEventListener('blur', function () { setTimeout(function () { list.hidden = true; }, 150); });
    wrap.appendChild(input); wrap.appendChild(list);
    return wrap;
  }
  function tagList(items, labelFn, onRemove, cls) {
    var wrap = h('div', { class: 'ap-tags' });
    if (!items.length) { wrap.appendChild(h('span', { class: 'text-faint', text: 'Tanlanmagan' })); }
    items.forEach(function (it, i) {
      var tag = h('span', { class: 'ap-tag' + (cls ? ' ' + cls : '') }, [labelFn(it)]);
      if (editable() && onRemove) { var x = h('button', { type: 'button', text: '×', title: 'O\'chirish' }); x.addEventListener('click', function () { onRemove(i); }); tag.appendChild(x); }
      wrap.appendChild(tag);
    });
    return wrap;
  }

  // ------------------------------------------------------------------
  // TOP BAR
  // ------------------------------------------------------------------
  function statusChip(label, cls) { return h('span', { class: 'badge badge-color-' + cls + ' ap-chip-status' }, [h('span', { class: 'ap-dot' }), label]); }
  function renderTopbar() {
    var d = store.draft;
    var el = document.getElementById('ap-topbar');
    el.innerHTML = '';
    var statusCls = { draft: 'dim', publishing: 'warn', published: 'blue', active: 'good', failed: 'bad', archived: 'dim' }[d.status] || 'dim';
    var syncCls = { local: 'dim', synced: 'good', local_changes: 'warn', publishing: 'warn', meta_changed: 'warn', sync_error: 'bad' }[d.sync_status] || 'dim';
    var conn = d.connection || {};
    var metaLine = [];
    if (conn.has_ad_account) { metaLine.push((conn.ad_account_name ? conn.ad_account_name + ' · ' : '') + conn.ad_account_id + ' · ' + (d.currency || conn.currency)); }
    else { metaLine.push('Reklama hisobi ulanmagan'); }
    var left = h('div', { class: 'ap-topbar-left' }, [
      h('a', { href: LIST_URL, class: 'ap-mini-btn', text: '← Ro\'yxat', style: 'text-decoration:none' }),
      h('span', { class: 'ap-topbar-title', text: d.title }),
      h('span', { class: 'badge badge-color-blue', text: d.objective_label }),
      statusChip(d.status_label, statusCls),
      d.has_meta_ids ? statusChip(d.sync_label, syncCls) : (d.status === 'draft' ? h('span', { class: 'badge badge-color-dim', text: 'Draft' }) : null),
      h('span', { class: 'ap-topbar-meta' }, [h('span', { text: metaLine.join(' ') })])
    ]);
    var saving = h('span', { class: 'ap-saving' + (store.saving === 'error' ? ' err' : '') });
    saving.textContent = store.saving === 'saving' ? 'Saqlanmoqda…' : store.saving === 'saved' ? 'Saqlandi' : store.saving === 'error' ? 'Saqlanmadi: ' + (store.saveError || '') : 'Avtomatik saqlanadi';
    var right = h('div', { class: 'ap-topbar-actions' }, [saving]);
    right.appendChild(miniBtn('Preview', openPreviewModal, { icon: 'eye', always: true }));
    if (d.has_meta_ids && IS_ADMIN) {
      right.appendChild(miniBtn('Yangilash', function () {
        chatSystem('Meta bilan sinxronlanmoqda…');
        api('/sync', { method: 'POST' }).then(function (r) { chatSystem('Sinxron holati: ' + (store.draft.sync_label || r.sync_status)); }).catch(function (e) { chatSystem(e.message, 'err'); });
      }, { icon: 'refresh', always: true, title: 'Meta\'dagi holatni o\'qib solishtirish' }));
    }
    if (editable()) {
      right.appendChild(miniBtn('AI qayta reja', function () {
        if (!confirm('AI rejani qaytadan tuzadi. Siz qo\'lda o\'zgartirgan maydonlar saqlanadi, tasdiqlar bekor bo\'ladi. Davom etaymi?')) { return; }
        chatSystem('AI rejani qayta tuzmoqda…');
        api('/replan', { method: 'POST', body: {} }).then(function () { chatSystem('Yangi reja tayyor. Qo\'lda o\'zgartirgan maydonlaringiz saqlandi.'); }).catch(function (e) { chatSystem(e.message, 'err'); });
      }, { always: true, title: 'Rejani qaytadan tuzish (override\'lar saqlanadi)' }));
    }
    if (IS_ADMIN && d.status !== 'archived') {
      right.appendChild(miniBtn('Arxivlash', function () {
        if (!confirm('Qoralamani arxivlaysizmi? (Meta\'dagi obyektlarga tegilmaydi)')) { return; }
        api('/archive', { method: 'POST', body: {} }).then(function () { window.location = LIST_URL; }).catch(function (e) { chatSystem(e.message, 'err'); });
      }, { always: true, cls: 'danger' }));
    }
    el.appendChild(left); el.appendChild(right);
    renderBanner();
  }

  function renderBanner() {
    var d = store.draft;
    var el = document.getElementById('ap-banner');
    el.innerHTML = '';
    var conn = d.connection || {};
    if (conn.problems && conn.problems.length) {
      var card = h('div', { class: 'card ap-warn-card' }, [h('h2', { text: 'Meta\'ga nashr uchun quyidagilar yetishmayapti' }), h('ul', {}, conn.problems.map(function (p) { return h('li', { text: p }); }))]);
      var links = h('p', { class: 'page-subtitle', style: 'margin:8px 0 0' });
      links.appendChild(document.createTextNode('AI reja tuzish va tahrirlash ishlaydi; nashr uchun: '));
      links.appendChild(h('a', { href: CONNECT_URL, text: 'Sozlamalar → Ulanishlar' }));
      if (!conn.plan_allows_meta) { links.appendChild(document.createTextNode(' · ')); links.appendChild(h('a', { href: PRICING_URL, text: 'Tarifni yangilash' })); }
      card.appendChild(links);
      el.appendChild(card);
    }
    if (!IS_ADMIN) {
      el.appendChild(h('div', { class: 'hint-box', style: 'margin:0', text: 'Siz qoralamani faqat ko\'rishingiz mumkin -- tahrirlash, tasdiqlash va nashr qilish admin uchun.' }));
    }
    if (d.status === 'failed' && d.publish_error) {
      var errCard = h('div', { class: 'card ap-err-card' }, [h('h2', { text: 'Nashr "' + (d.publish_step || '?') + '" bosqichida to\'xtadi' }), h('p', { text: d.publish_error + ' Tuzatib, "Meta\'ga nashr qilish"ni qayta bosing -- allaqachon yaratilgan qadamlar takrorlanmaydi.' })]);
      if (d.last_meta_error_raw) {
        // 2026-09: Meta'ning xom xatolik matni -- diagnostika uchun,
        // yopiq holda (kerak bo'lganda ochiladi).
        var det = h('details', { style: 'margin-top:8px' }, [
          h('summary', { style: 'cursor:pointer;font-size:12px;color:var(--text-faint,#888)', text: 'Texnik tafsilot (Meta xabari)' }),
          h('pre', { style: 'white-space:pre-wrap;word-break:break-word;font-size:11px;background:var(--bg-2,#f5f5f5);padding:8px;border-radius:6px;margin-top:6px', text: d.last_meta_error_raw })
        ]);
        errCard.appendChild(det);
      }
      el.appendChild(errCard);
    }
    var w = document.getElementById('ap-warnings');
    w.innerHTML = '';
    var warnings = ((d.ai_plan || {}).warnings || []);
    if (warnings.length) {
      w.appendChild(h('div', { class: 'card ap-warn-card' }, [h('h2', { text: 'AI ogohlantirishlari' }), h('ul', {}, warnings.map(function (x) { return h('li', { text: x }); }))]));
    }
  }

  // ------------------------------------------------------------------
  // CHAP DARAXT
  // ------------------------------------------------------------------
  function levelState(scope) {
    if (store.draft.approvals[scope]) { return 'ok'; }
    return errorsForScope(scope).length ? 'err' : 'todo';
  }
  function renderTree() {
    var el = document.getElementById('ap-tree');
    el.innerHTML = '';
    var s = state();
    var names = { campaign: s.campaign.name || 'Kampaniya', adset: s.adset.name || 'Ad Set', ad: s.ad.name || 'Reklama' };
    var icons = { campaign: 'campaign', adset: 'adset', ad: 'ad' };
    LEVELS.forEach(function (scope, i) {
      var st = levelState(scope);
      var stEl = h('span', { class: 'ap-tree-state ' + st, html: st === 'ok' ? svgIcon('check') : (st === 'err' ? svgIcon('x') : '') });
      var node = h('button', { type: 'button', class: 'ap-tree-node level-' + i + (store.tab === scope ? ' active' : ''), html: svgIcon(icons[scope]) });
      node.appendChild(h('span', { class: 'ap-tree-text' }, [h('span', { class: 'ap-tree-label', text: LEVEL_LABELS[scope] }), h('span', { class: 'ap-tree-name', text: names[scope], title: names[scope] })]));
      node.appendChild(stEl);
      node.addEventListener('click', function () { setTab(scope); });
      el.appendChild(node);
    });
    var t = s.adset.targeting || {};
    var geo = t.geo_locations || {};
    var locs = (geo.cities || []).map(function (c) { return c.name || c.key; }).concat((geo.regions || []).map(function (r) { return r.name || r.key; })).concat(geo.countries || []);
    var budgetType = s.adset.budget_type || 'daily';
    var budget = budgetType === 'daily' ? s.adset.daily_budget : s.adset.lifetime_budget;
    var summary = h('div', { class: 'ap-tree-summary' });
    summary.innerHTML = '<div><strong>' + esc(budgetType === 'daily' ? 'Kunlik' : 'Umumiy') + ':</strong> ' + esc(fmtMoney(budget, s.adset.currency)) + '</div>'
      + '<div><strong>Muddat:</strong> ' + esc(s.adset.duration_days ? s.adset.duration_days + ' kun' : 'cheksiz') + '</div>'
      + '<div><strong>Hudud:</strong> ' + esc(locs.length ? locs.join(', ') : 'tanlanmagan') + '</div>'
      + '<div><strong>Yosh:</strong> ' + esc(t.age_min + '–' + t.age_max) + '</div>'
      + '<div><strong>Tasdiqlar:</strong> ' + esc(LEVELS.filter(function (l) { return store.draft.approvals[l]; }).length + '/3') + '</div>';
    el.appendChild(summary);
    if ((store.draft.ai_plan || {}).reasoning_summary) {
      el.appendChild(h('div', { class: 'ap-tree-summary' }, [h('strong', { text: '✨ AI mulohazasi: ' }), (store.draft.ai_plan || {}).reasoning_summary]));
    }
  }

  // ------------------------------------------------------------------
  // TABLAR + FORMA
  // ------------------------------------------------------------------
  function setTab(tab) { store.tab = tab; store.lfComposer = null; renderTabs(); renderTree(); renderEditor(); renderBottombar(); window.scrollTo({ top: 0, behavior: 'smooth' }); }
  function renderTabs() {
    var el = document.getElementById('ap-tabs');
    el.innerHTML = '';
    var tabs = LEVELS.map(function (l) { return { key: l, label: LEVEL_LABELS[l] }; }).concat([{ key: 'events', label: 'Jurnal' }]);
    tabs.forEach(function (t) {
      var b = h('button', { type: 'button', class: 'ap-tab' + (store.tab === t.key ? ' active' : ''), role: 'tab', text: t.label });
      if (LEVELS.indexOf(t.key) >= 0) {
        var st = levelState(t.key);
        b.appendChild(h('span', { class: 'ap-tab-badge ' + (st === 'ok' ? 'ok' : st === 'err' ? 'err' : ''), text: st === 'ok' ? 'tasdiqlangan' : st === 'err' ? errorsForScope(t.key).length + ' xato' : 'kutilmoqda' }));
      } else if (t.key === 'events') {
        b.appendChild(h('span', { class: 'ap-tab-badge', text: String((store.draft.events || []).length) }));
      }
      b.addEventListener('click', function () { setTab(t.key); });
      el.appendChild(b);
    });
  }

  function renderEditor() {
    var body = document.getElementById('ap-editor-body');
    // Fokusni saqlab qolish (qayta chizishda yozayotgan maydon yo'qolmasin)
    var active = document.activeElement;
    var focusPath = active && active.dataset ? active.dataset.path : null;
    var selStart = active && typeof active.selectionStart === 'number' ? active.selectionStart : null;
    body.innerHTML = '';
    if (store.tab === 'campaign') { body.appendChild(renderCampaign()); }
    else if (store.tab === 'adset') { body.appendChild(renderAdset()); }
    else if (store.tab === 'ad') { body.appendChild(renderAd()); }
    else { body.appendChild(renderEvents()); }
    if (focusPath) {
      var again = body.querySelector('[data-path="' + focusPath + '"]');
      if (again) { again.focus(); try { if (selStart != null) { again.setSelectionRange(selStart, selStart); } } catch (e) { /* select/number */ } }
    }
  }

  // ---- Kampaniya darajasi
  function renderCampaign() {
    var s = state();
    var d = store.draft;
    var wrap = h('div');
    var sec = h('div', { class: 'ap-section' }, [sectionTitle('Kampaniya')]);
    sec.appendChild(fieldWrap('campaign.name', 'Kampaniya nomi', textInput('campaign', 'campaign.name', { maxlength: 255 })));
    var objSel = selectInput('objective', 'objective', d.options.objectives, { rerender: true });
    sec.appendChild(fieldWrap('objective', 'Maqsad', objSel, { hint: 'Maqsad o\'zgarsa Meta maqsadi, optimallashtirish, yo\'nalish va tugma avtomatik moslanadi; uchala tasdiq bekor bo\'ladi.' }));
    var meta = d.objective_meta || {};
    sec.appendChild(fieldWrap('campaign.meta_objective', 'Meta maqsadi (avtomatik)', h('input', { type: 'text', value: (meta.meta_objective || '') + ' · ' + (meta.optimization_goal || ''), disabled: true }), { hint: 'Maqsaddan deterministik olinadi -- AI ham, siz ham o\'zgartirmaysiz.' }));
    var cats = s.campaign.special_ad_categories || [];
    var catBox = h('div', { class: 'ap-checks' });
    d.options.special_ad_categories.filter(function (c) { return c !== 'NONE'; }).forEach(function (c) {
      catBox.appendChild(checkbox(SPECIAL_LABELS[c] || c, cats.indexOf(c) >= 0, function (on) {
        var next = cats.filter(function (x) { return x !== c; });
        if (on) { next.push(c); }
        queueChange('campaign', 'campaign.special_ad_categories', next, { immediate: true });
      }));
    });
    sec.appendChild(fieldWrap('campaign.special_ad_categories', 'Maxsus reklama toifasi', catBox, { hint: 'Uy-joy/ish/kredit/siyosat reklamalari uchun Meta talab qiladi; oddiy biznes uchun bo\'sh qoldiring.' }));
    sec.appendChild(fieldWrap('campaign.budget_mode', 'Byudjet darajasi', h('input', { type: 'text', value: 'Ad Set darajasida (ABO)', disabled: true })));
    // 2026-09: standart -- nashr qilinishi bilan ACTIVE; nashr modalida
    // "Pauzada qolsin"ni tanlasa PAUSED (qarang `openSummaryModal`).
    var launchText = d.launch_active === false
      ? 'PAUSED -- nashrdan keyin alohida "Faollashtirish" bilan yoqiladi'
      : 'ACTIVE -- nashr qilinishi bilan darhol ishga tushadi (nashr oynasida o\'zgartirish mumkin)';
    sec.appendChild(fieldWrap('campaign.status', 'Nashrdan keyingi holat', h('input', { type: 'text', value: launchText, disabled: true })));
    if (s.objective === 'SALES') {
      var pixels = d.assets.pixels || [];
      var pxText = d.connection.has_pixel ? 'Pixel ulangan' + (pixels.length ? ': ' + pixels.map(function (p) { return p.name || p.id; }).join(', ') : '') : 'Pixel tanlanmagan -- Sozlamalar > Ulanishlar';
      sec.appendChild(fieldWrap('campaign.pixel', 'Pixel (sotuv maqsadi uchun)', h('input', { type: 'text', value: pxText, disabled: true })));
    }
    wrap.appendChild(sec);
    wrap.appendChild(unsupportedBlock(d.options.unsupported.filter(function (u) { return u.key === 'cbo' || u.key === 'ab_test'; })));
    return wrap;
  }

  // ---- Ad Set darajasi
  function renderAdset() {
    var s = state();
    var d = store.draft;
    var a = s.adset;
    var t = a.targeting || {};
    var wrap = h('div');

    // Nom + byudjet + muddat
    var sec = h('div', { class: 'ap-section' }, [sectionTitle('Ad Set')]);
    sec.appendChild(fieldWrap('adset.name', 'Ad Set nomi', textInput('adset', 'adset.name', { maxlength: 255 })));
    var grid = h('div', { class: 'ap-grid-3' });
    grid.appendChild(fieldWrap('adset.budget_type', 'Byudjet turi', selectInput('adset', 'adset.budget_type', [{ value: 'daily', label: 'Kunlik' }, { value: 'lifetime', label: 'Umumiy (lifetime)' }], { rerender: true })));
    if ((a.budget_type || 'daily') === 'daily') {
      grid.appendChild(fieldWrap('adset.daily_budget', 'Kunlik byudjet (' + (a.currency || d.currency) + ')', textInput('adset', 'adset.daily_budget', { type: 'number', min: 1, step: 'any' })));
    } else {
      grid.appendChild(fieldWrap('adset.lifetime_budget', 'Umumiy byudjet (' + (a.currency || d.currency) + ')', textInput('adset', 'adset.lifetime_budget', { type: 'number', min: 1, step: 'any' })));
    }
    grid.appendChild(fieldWrap('adset.duration_days', 'Muddat (kun)', textInput('adset', 'adset.duration_days', { type: 'number', min: 1, max: 365 }), { hint: 'Tugash sanasi boshlanish + muddat.' }));
    sec.appendChild(grid);
    var grid2 = h('div', { class: 'ap-grid-2' });
    grid2.appendChild(fieldWrap('adset.start_time', 'Boshlanish', textInput('adset', 'adset.start_time', { type: 'datetime-local' })));
    grid2.appendChild(fieldWrap('adset.end_time', 'Tugash', textInput('adset', 'adset.end_time', { type: 'datetime-local' })));
    grid2.querySelectorAll('input[type="datetime-local"]').forEach(function (inp) { inp.value = toLocalInput(getPath(s, inp.dataset.path)); });
    sec.appendChild(grid2);
    wrap.appendChild(sec);

    // Yo'nalish + optimallashtirish
    var sec2 = h('div', { class: 'ap-section' }, [sectionTitle('Yo\'nalish va optimallashtirish')]);
    var dests = d.options.destinations || [];
    if (dests.length) {
      var cards = h('div', { class: 'ap-radio-cards' });
      dests.forEach(function (o) {
        var sel = a.destination_type === o.value;
        var card = h('label', { class: 'ap-radio-card' + (sel ? ' selected' : '') + (!o.available ? ' disabled' : ''), title: o.note || '' }, [
          h('strong', { text: o.label }), o.note ? h('small', { text: o.note }) : null
        ]);
        if (o.available && editable()) { card.addEventListener('click', function () { queueChange('adset', 'adset.destination_type', o.value, { immediate: true, rerender: true }); }); }
        cards.appendChild(card);
      });
      sec2.appendChild(fieldWrap('adset.destination_type', 'Yo\'nalish (destination)', cards));
    }
    var g3 = h('div', { class: 'ap-grid-2' });
    g3.appendChild(fieldWrap('adset.optimization_goal', 'Optimallashtirish maqsadi', h('input', { type: 'text', value: a.optimization_goal || '', disabled: true }), { hint: 'Kampaniya maqsadidan avtomatik. Billing: ' + (a.billing_event || 'IMPRESSIONS') }));
    g3.appendChild(fieldWrap('adset.bid_strategy', 'Stavka strategiyasi', selectInput('adset', 'adset.bid_strategy', [
      { value: 'LOWEST_COST_WITHOUT_CAP', label: 'Eng past narx (tavsiya)' }, { value: 'LOWEST_COST_WITH_BID_CAP', label: 'Stavka chegarasi bilan' }, { value: 'COST_CAP', label: 'Narx chegarasi (cost cap)' }
    ])));
    sec2.appendChild(g3);
    wrap.appendChild(sec2);

    // Targeting
    var sec3 = h('div', { class: 'ap-section' }, [sectionTitle('Auditoriya (targeting)')]);
    var geo = t.geo_locations || { cities: [], regions: [], countries: [] };
    var geoBox = h('div');
    geoBox.appendChild(tagList((geo.cities || []).map(function (c) { return { kind: 'cities', item: c, label: (c.name || c.key) + ' (shahar)' }; })
      .concat((geo.regions || []).map(function (r) { return { kind: 'regions', item: r, label: (r.name || r.key) + ' (viloyat)' }; }))
      .concat((geo.countries || []).map(function (c) { return { kind: 'countries', item: c, label: c + ' (davlat)' }; })),
      function (x) { return x.label; },
      function (i) {
        var all = (geo.cities || []).map(function (c) { return ['cities', c]; }).concat((geo.regions || []).map(function (r) { return ['regions', r]; })).concat((geo.countries || []).map(function (c) { return ['countries', c]; }));
        var target = all[i];
        var next = JSON.parse(JSON.stringify(geo));
        next[target[0]] = next[target[0]].filter(function (x) { return x !== target[1] && JSON.stringify(x) !== JSON.stringify(target[1]); });
        queueChange('adset', 'adset.targeting.geo_locations', next, { immediate: true, rerender: true });
      }));
    geoBox.appendChild(typeahead('/avtopilot/api/search-geo', 'Shahar/viloyat/davlat qidirish (Meta)…', function (r) {
      var next = JSON.parse(JSON.stringify(geo));
      var type = String(r.type || 'city').toLowerCase();
      if (type === 'country') { if (next.countries.indexOf(String(r.key).toUpperCase()) < 0) { next.countries.push(String(r.key).toUpperCase()); } }
      else if (type === 'region' || type === 'state' || type === 'province') { next.regions.push({ key: String(r.key), name: r.name }); }
      else { next.cities.push({ key: String(r.key), name: r.name, radius: 0, distance_unit: 'kilometer' }); }
      queueChange('adset', 'adset.targeting.geo_locations', next, { immediate: true, rerender: true });
    }));
    sec3.appendChild(fieldWrap('adset.targeting.geo_locations', 'Hudud', geoBox, { hint: 'Faqat Meta qidiruvidan tanlanadi (haqiqiy geo key). Chatda "Faqat Toshkent qil" deb ham yozsangiz bo\'ladi.' }));

    var ageGrid = h('div', { class: 'ap-grid-3' });
    ageGrid.appendChild(fieldWrap('adset.targeting.age_min', 'Yosh (min)', textInput('adset', 'adset.targeting.age_min', { type: 'number', min: 13, max: 65 })));
    ageGrid.appendChild(fieldWrap('adset.targeting.age_max', 'Yosh (max)', textInput('adset', 'adset.targeting.age_max', { type: 'number', min: 13, max: 65 }), { errors: [] }));
    var genders = t.genders || [];
    var gVal = genders.length === 1 ? String(genders[0]) : '';
    var gSel = h('select', { disabled: !editable(), dataset: { path: 'adset.targeting.genders' } }, [
      h('option', { value: '', text: 'Hammasi', selected: gVal === '' }), h('option', { value: '1', text: 'Erkaklar', selected: gVal === '1' }), h('option', { value: '2', text: 'Ayollar', selected: gVal === '2' })
    ]);
    gSel.addEventListener('change', function () { queueChange('adset', 'adset.targeting.genders', gSel.value ? [Number(gSel.value)] : [], { immediate: true }); });
    ageGrid.appendChild(fieldWrap('adset.targeting.genders', 'Jins', gSel));
    sec3.appendChild(ageGrid);

    var interests = t.interests || [];
    var intBox = h('div');
    intBox.appendChild(tagList(interests, function (i) { return i.name || i.id; }, function (idx) {
      var next = interests.filter(function (_, j) { return j !== idx; });
      queueChange('adset', 'adset.targeting.interests', next, { immediate: true, rerender: true });
    }));
    intBox.appendChild(typeahead('/avtopilot/api/search-interests', 'Qiziqish qidirish (Meta)…', function (r) {
      if (interests.some(function (i) { return String(i.id) === String(r.id); })) { return; }
      queueChange('adset', 'adset.targeting.interests', interests.concat([{ id: String(r.id), name: r.name }]), { immediate: true, rerender: true });
    }));
    sec3.appendChild(fieldWrap('adset.targeting.interests', 'Qiziqishlar', intBox, { hint: 'Bo\'sh qoldirsangiz keng (broad) auditoriya -- Advantage+ bilan ko\'pincha samaraliroq.' }));
    sec3.appendChild(fieldWrap('adset.targeting.advantage_audience', 'Advantage+ auditoriya', checkbox('Meta auditoriyani avtomatik kengaytirsin', !!t.advantage_audience, function (on) { queueChange('adset', 'adset.targeting.advantage_audience', on, { immediate: true }); })));

    var auds = d.assets.custom_audiences || [];
    ['custom_audiences', 'excluded_custom_audiences'].forEach(function (key) {
      var cur = t[key] || [];
      var box = h('div');
      box.appendChild(tagList(cur, function (x) { return x.name || x.id; }, function (idx) { queueChange('adset', 'adset.targeting.' + key, cur.filter(function (_, j) { return j !== idx; }), { immediate: true, rerender: true }); }, key === 'excluded_custom_audiences' ? 'dim' : ''));
      if (auds.length) {
        var sel = h('select', { disabled: !editable() }, [h('option', { value: '', text: 'Auditoriya qo\'shish…' })].concat(auds.filter(function (a2) { return !cur.some(function (c) { return String(c.id) === String(a2.id); }); }).map(function (a2) { return h('option', { value: a2.id, text: (a2.name || a2.id) + (a2.approximate_count_lower_bound ? ' · ~' + a2.approximate_count_lower_bound : '') }); })));
        sel.addEventListener('change', function () {
          var pick = auds.filter(function (a2) { return String(a2.id) === sel.value; })[0];
          if (pick) { queueChange('adset', 'adset.targeting.' + key, cur.concat([{ id: String(pick.id), name: pick.name || '' }]), { immediate: true, rerender: true }); }
        });
        box.appendChild(sel);
      } else {
        box.appendChild(h('div', { class: 'text-faint', style: 'font-size:12px', text: 'Reklama hisobida Custom auditoriya topilmadi.' }));
      }
      sec3.appendChild(fieldWrap('adset.targeting.' + key, key === 'custom_audiences' ? 'Custom auditoriyalar' : 'Chiqarib tashlanadigan auditoriyalar', box));
    });
    wrap.appendChild(sec3);

    // Joylashuvlar
    var sec4 = h('div', { class: 'ap-section' }, [sectionTitle('Joylashuvlar (placements)')]);
    var pl = t.placements || { mode: 'automatic', publisher_platforms: [], facebook_positions: [], instagram_positions: [] };
    var modeSel = selectInput('adset', 'adset.targeting.placements.mode', [{ value: 'automatic', label: 'Avtomatik (Advantage+ placements, tavsiya)' }, { value: 'manual', label: 'Qo\'lda' }], { rerender: true });
    sec4.appendChild(fieldWrap('adset.targeting.placements.mode', 'Rejim', modeSel));
    if (pl.mode === 'manual') {
      var plats = h('div', { class: 'ap-checks' });
      d.options.placements.publisher_platforms.forEach(function (p) {
        var dis = p === 'instagram' && !d.connection.has_instagram;
        plats.appendChild(checkbox(PLATFORM_LABELS[p] || p, (pl.publisher_platforms || []).indexOf(p) >= 0, function (on) {
          var next = (pl.publisher_platforms || []).filter(function (x) { return x !== p; });
          if (on) { next.push(p); }
          queueChange('adset', 'adset.targeting.placements.publisher_platforms', next, { immediate: true, rerender: true });
        }, dis));
      });
      sec4.appendChild(fieldWrap('adset.targeting.placements', 'Platformalar', plats, { hint: !d.connection.has_instagram ? 'Instagram: akkaunt ulanmagan -- Meta API orqali hozircha sozlanmagan.' : null }));
      if ((pl.publisher_platforms || []).indexOf('facebook') >= 0) {
        var fb = h('div', { class: 'ap-checks' });
        d.options.placements.facebook_positions.forEach(function (p) {
          fb.appendChild(checkbox(FB_POS_LABELS[p] || p, (pl.facebook_positions || []).indexOf(p) >= 0, function (on) {
            var next = (pl.facebook_positions || []).filter(function (x) { return x !== p; }); if (on) { next.push(p); }
            queueChange('adset', 'adset.targeting.placements.facebook_positions', next, { immediate: true });
          }));
        });
        sec4.appendChild(fieldWrap('adset.targeting.placements.facebook_positions', 'Facebook joylari (bo\'sh = hammasi)', fb));
      }
      if ((pl.publisher_platforms || []).indexOf('instagram') >= 0) {
        var ig = h('div', { class: 'ap-checks' });
        d.options.placements.instagram_positions.forEach(function (p) {
          ig.appendChild(checkbox(IG_POS_LABELS[p] || p, (pl.instagram_positions || []).indexOf(p) >= 0, function (on) {
            var next = (pl.instagram_positions || []).filter(function (x) { return x !== p; }); if (on) { next.push(p); }
            queueChange('adset', 'adset.targeting.placements.instagram_positions', next, { immediate: true });
          }));
        });
        sec4.appendChild(fieldWrap('adset.targeting.placements.instagram_positions', 'Instagram joylari (bo\'sh = hammasi)', ig));
      }
    }
    wrap.appendChild(sec4);
    return wrap;
  }

  // ---- Reklama darajasi
  function renderAd() {
    var s = state();
    var d = store.draft;
    var ad = s.ad;
    var wrap = h('div');

    // Identity
    var sec = h('div', { class: 'ap-section' }, [sectionTitle('Reklama')]);
    sec.appendChild(fieldWrap('ad.name', 'Reklama nomi', textInput('ad', 'ad.name', { maxlength: 255 })));
    var g = h('div', { class: 'ap-grid-2' });
    var pages = (d.assets.pages || []).map(function (p) { return { value: p.id, label: (p.name || 'Sahifa') + ' · ' + p.id }; });
    if (ad.page_id && !pages.some(function (p) { return String(p.value) === String(ad.page_id); })) { pages.push({ value: ad.page_id, label: ad.page_id }); }
    g.appendChild(fieldWrap('ad.page_id', 'Facebook sahifa', pages.length ? selectInput('ad', 'ad.page_id', pages, { allowEmpty: true, emptyLabel: 'Sahifa tanlang…' }) : h('input', { type: 'text', value: 'Sahifa ulanmagan', disabled: true })));
    var igs = (d.assets.instagram_accounts || []).map(function (a) { return { value: a.id, label: (a.username ? '@' + a.username : 'Instagram') + ' · ' + a.id }; });
    if (ad.instagram_actor_id && !igs.some(function (p) { return String(p.value) === String(ad.instagram_actor_id); })) { igs.push({ value: ad.instagram_actor_id, label: ad.instagram_actor_id }); }
    g.appendChild(fieldWrap('ad.instagram_actor_id', 'Instagram akkaunt', igs.length ? selectInput('ad', 'ad.instagram_actor_id', igs, { allowEmpty: true, emptyLabel: 'Sahifa nomidan (IG yo\'q)' }) : h('input', { type: 'text', value: 'Instagram ulanmagan -- Meta API orqali hozircha sozlanmagan', disabled: true })));
    sec.appendChild(g);
    wrap.appendChild(sec);

    // Media
    wrap.appendChild(renderMediaSection());

    // Matnlar
    var sec2 = h('div', { class: 'ap-section' }, [sectionTitle('Matn va tugma')]);
    sec2.appendChild(copyField('primary_text', 'Asosiy matn (primary text)', { textarea: true, rows: 4, maxlength: 1000 }));
    sec2.appendChild(copyField('headline', 'Sarlavha (headline)', { maxlength: 125 }));
    sec2.appendChild(copyField('description', 'Tavsif (description)', { maxlength: 255 }));
    var g2 = h('div', { class: 'ap-grid-2' });
    g2.appendChild(fieldWrap('ad.cta', 'Tugma (CTA)', selectInput('ad', 'ad.cta', d.options.cta)));
    var linkLabel = s.objective === 'CALLS' ? 'Telefon (tel:+998...)' : 'Havola (link)';
    var linkHint = null;
    if (s.objective === 'MESSAGES') { linkHint = 'Xabar maqsadi uchun shart emas (sahifa havolasi ishlatiladi).'; }
    // 2026-09, foydalanuvchi savoli ("nega havola bo'ladi, men Instant
    // Formaga to'ldiryapman?"): LEADS'da tugma to'g'ridan-to'g'ri Instant
    // Form'ni ochadi (pastdagi "Instant Form" bo'limi) -- bu havola HECH
    // QAYERGA olib bormaydi, Meta shunchaki texnik jihatdan shu maydonni
    // talab qiladi. Bo'sh qoldirsangiz -- Facebook sahifangiz havolasi
    // avtomatik qo'yiladi, reklamada ko'rinmaydi/bosilmaydi.
    if (s.objective === 'LEADS') { linkHint = 'Instant Form uchun shart emas -- tugma sahifani emas, pastdagi formani ochadi. Bo\'sh qoldirsangiz, Meta talabi bo\'yicha sahifangiz havolasi orqa fonda avtomatik ishlatiladi (hech qayerda ko\'rinmaydi).'; }
    g2.appendChild(fieldWrap('ad.link_url', linkLabel, textInput('ad', 'ad.link_url', { placeholder: s.objective === 'CALLS' ? 'tel:+998901234567' : 'https://…', nullable: true }), { hint: linkHint }));
    sec2.appendChild(g2);
    var g3 = h('div', { class: 'ap-grid-2' });
    g3.appendChild(fieldWrap('ad.display_link', 'Ko\'rinadigan havola', textInput('ad', 'ad.display_link', { placeholder: 'masalan sayt.uz', nullable: true })));
    g3.appendChild(fieldWrap('ad.url_tags', 'URL teglari (UTM)', textInput('ad', 'ad.url_tags', { placeholder: 'utm_source=facebook&utm_campaign=…', nullable: true })));
    sec2.appendChild(g3);
    wrap.appendChild(sec2);

    // Maqsadga xos
    if (s.objective === 'MESSAGES') { wrap.appendChild(renderMessagesEditor()); }
    if (s.objective === 'LEADS') { wrap.appendChild(renderLeadFormEditor()); }

    wrap.appendChild(unsupportedBlock(d.options.unsupported.filter(function (u) { return u.key === 'advantage_plus_creative' || u.key === 'dynamic_creative'; })));

    // Preview (inline)
    var pv = h('div', { class: 'ap-section' }, [sectionTitle('Ko\'rinish (preview)', miniBtn('Kattaroq ochish', openPreviewModal, { always: true }))]);
    pv.appendChild(previewWidget(false));
    wrap.appendChild(pv);
    return wrap;
  }

  function copyField(key, label, opts) {
    var path = 'ad.' + key;
    var variants = ((state().ad.copy_variants || {})[key]) || [];
    var actions = h('span', { class: 'ap-inline-actions' }, [
      miniBtn('AI qayta yozsin', function () { regenerate(key); }, { cls: 'primary', title: 'Faqat shu maydon uchun yangi variant' })
    ]);
    var control = h('div');
    control.appendChild(textInput('ad', path, opts));
    if (variants.length) {
      var vs = h('div', { class: 'ap-variants' });
      variants.forEach(function (v, i) {
        var sel = v === getPath(state(), path);
        var row = h('div', { class: 'ap-variant' + (sel ? ' selected' : '') }, [h('span', { class: 'text-faint', text: (i + 1) + '.' }), h('span', { class: 'ap-variant-text', text: v })]);
        if (!sel) { row.appendChild(miniBtn('Tanlash', function () { queueChange('ad', path, v, { immediate: true, rerender: true }); })); }
        vs.appendChild(row);
      });
      control.appendChild(vs);
    }
    return fieldWrap(path, label, control, { actions: actions, hint: opts.maxlength ? 'Ko\'pi bilan ' + opts.maxlength + ' belgi' : null });
  }

  function regenerate(field) {
    if (!editable()) { return; }
    chatSystem('AI "' + field + '" uchun yangi variant yozmoqda…');
    api('/regenerate-copy', { method: 'POST', body: { field: field } })
      .then(function (r) { chatSystem(r.reply || 'Yangi variant yozildi.'); })
      .catch(function (e) { chatSystem(e.message, 'err'); });
  }

  // ---- Media
  function currentAspect() {
    // Kreativ studiya uchun tavsiya etilgan nisbat: faqat Story/Reels
    // joylashuvlari tanlangan bo'lsa 9:16, aks holda 1:1 (lenta).
    var pl = ((state().adset || {}).targeting || {}).placements || {};
    if (pl.mode === 'manual') {
      var pos = (pl.facebook_positions || []).concat(pl.instagram_positions || []);
      if (pos.length && pos.every(function (p) { return p === 'story' || p === 'reels'; })) { return '9:16'; }
    }
    return '1:1';
  }
  function renderMediaSection() {
    var d = store.draft;
    var media = d.media || [];
    var sec = h('div', { class: 'ap-section' }, [sectionTitle('Media (rasm / video)')]);
    var grid = h('div', { class: 'ap-media-grid' });
    media.forEach(function (m) {
      var item = h('div', { class: 'ap-media-item' + (m.selected ? ' selected' : ''), title: m.filename });
      if (m.kind === 'image') { item.appendChild(h('img', { src: m.url, alt: m.filename })); }
      else { var v = h('video', { src: m.url, muted: true, playsinline: true }); item.appendChild(v); }
      item.appendChild(h('span', { class: 'ap-media-idx', text: String(m.index + 1) }));
      var stCls = m.upload_status === 'uploaded' ? 'ok' : (m.upload_status === 'failed' ? 'err' : '');
      var stText = m.upload_status === 'uploaded' ? 'Meta\'da' : (m.upload_status === 'failed' ? 'Meta xatosi' : 'Lokal');
      item.appendChild(h('span', { class: 'ap-media-status ' + stCls, text: stText, title: m.upload_error || '' }));
      if (editable() && !m.selected) {
        item.addEventListener('click', function () {
          api('/media/' + m.id + '/select', { method: 'POST', body: {} }).then(function (r) { if (r.upload_error) { chatSystem(r.upload_error, 'warn'); } }).catch(function (e) { chatSystem(e.message, 'err'); });
        });
      }
      if (editable()) {
        // 2026-09, foydalanuvchi so'rovi: yuklangan rasm/videoni o'chirish imkoni.
        var del = h('button', { type: 'button', class: 'ap-media-del', title: 'O\'chirish', html: '&times;' });
        del.addEventListener('click', function (e) {
          e.stopPropagation();
          if (!confirm('Bu media o\'chirilsinmi?')) { return; }
          api('/media/' + m.id + '/ochirish', { method: 'POST', body: {} }).catch(function (err) { chatSystem(err.message, 'err'); });
        });
        item.appendChild(del);
      }
      grid.appendChild(item);
    });
    if (editable()) {
      var add = h('label', { class: 'ap-media-item ap-media-add', html: svgIcon('upload') });
      var input = h('input', { type: 'file', accept: 'image/*,video/mp4,video/quicktime,video/webm' });
      input.addEventListener('change', function () {
        if (!input.files.length) { return; }
        var fd = new FormData();
        fd.append('file', input.files[0]);
        fd.append('select', media.length ? '0' : '1');
        chatSystem('Media yuklanmoqda…');
        api('/media', { method: 'POST', body: fd }).then(function (r) { chatSystem(r.upload_error ? 'Fayl saqlandi, lekin Meta\'ga yuklanmadi: ' + r.upload_error : 'Media yuklandi va Meta\'ga yuborildi.', r.upload_error ? 'warn' : null); }).catch(function (e) { chatSystem(e.message, 'err'); });
      });
      add.appendChild(input);
      add.appendChild(h('span', { text: 'Yuklash' }));
      grid.appendChild(add);
      if (CREATIVE_URL) {
        // Kreativ studiya galereyasi -- tayyor AI rasmni shu qoralamaga tanlash
        var fromCs = h('a', { class: 'ap-media-item ap-media-add ap-media-creative', href: CREATIVE_URL + '?from_autopilot=' + d.id + '&aspect=' + encodeURIComponent(currentAspect()), title: 'AI yaratgan yoki shablondan tayyorlangan rasmni tanlash' }, [
          h('span', { class: 'ap-media-creative-icon', text: '🎨' }), h('span', { text: 'Kreativ studiyadan tanlash' })
        ]);
        grid.appendChild(fromCs);
      }
    }
    var selected = media.filter(function (m) { return m.selected; })[0];
    sec.appendChild(fieldWrap('ad.media', 'Reklama vizuali', grid, { hint: selected ? 'Tanlangan: ' + (selected.index + 1) + '-media (' + selected.filename + '). Chatda "2-rasmni tanla" deb ham yozsangiz bo\'ladi.' : 'Rasm yoki video yuklang -- AI rasm yaratmaydi. JPG/PNG/WEBP 30 MB, MP4/MOV/WEBM 300 MB gacha.' }));
    if (!media.length && (state().ad.media || {}).image_hash) {
      sec.appendChild(h('div', { class: 'ap-field-hint', text: 'Meta\'dagi mavjud rasm (hash ' + state().ad.media.image_hash + ') ishlatilmoqda.' }));
    }
    return sec;
  }

  // ---- Xabarlar (MESSAGES)
  function renderMessagesEditor() {
    var ad = state().ad;
    var msgs = ad.messages || { greeting: '', quick_replies: [] };
    var sec = h('div', { class: 'ap-section' }, [sectionTitle('Xabar oqimi (salomlashuv + tezkor javoblar)', miniBtn('AI qayta yozsin', function () { regenerate('messages'); }, { cls: 'primary' }))]);
    sec.appendChild(fieldWrap('ad.messages.greeting', 'Salomlashuv (greeting)', textInput('ad', 'ad.messages.greeting', { textarea: true, rows: 3, maxlength: 500 }), { hint: 'Mijoz reklamani bosganda birinchi ko\'radigan xabar.' }));
    var list = h('div', { class: 'ap-list-editor' });
    var qr = msgs.quick_replies || [];
    qr.forEach(function (q, i) {
      var input = h('input', { type: 'text', value: q, maxlength: 80, disabled: !editable(), dataset: { path: 'ad.messages.quick_replies.' + i } });
      input.addEventListener('input', function () { var next = qr.slice(); next[i] = input.value; queueChange('ad', 'ad.messages.quick_replies', next); });
      var row = h('div', { class: 'ap-list-row' }, [h('span', { class: 'text-faint', text: (i + 1) + '.' }), input, miniBtn('O\'chirish', function () { queueChange('ad', 'ad.messages.quick_replies', qr.filter(function (_, j) { return j !== i; }), { immediate: true, rerender: true }); }, { cls: 'danger' })]);
      list.appendChild(row);
    });
    if (qr.length < 4 && editable()) {
      list.appendChild(h('div', {}, [miniBtn('Tezkor javob qo\'shish', function () { queueChange('ad', 'ad.messages.quick_replies', qr.concat(['Yangi savol']), { immediate: true, rerender: true }); }, { icon: 'plus' })]));
    }
    sec.appendChild(fieldWrap('ad.messages.quick_replies', 'Tezkor javoblar (ko\'pi bilan 4 ta Meta\'ga ketadi)', list));
    return sec;
  }

  // ---- Lead forma (LEADS)
  // 2026-09, foydalanuvchi so'rovi ("interfeys qiyinlashib qolgan", "savol
  // qo'shishda tasdiqlash bosqichi bo'lsin"): bo'lim Ads Manager'dagi Instant
  // Form muharriri kabi 3 ta aniq blokka bo'lindi -- (1) Kirish (sarlavha/
  // matn), (2) Savollar, (3) Rahmat xabari. Yangi savol qo'shish endi BITTA
  // kompozitor orqali: tur tanlanadi -> standart maydon darhol qo'shiladi,
  // maxsus savol esa avval shu yerda to'ldirilib, "Qo'shish" bilan
  // tasdiqlangandan keyingina ro'yxatga (va serverga) tushadi.
  var LF_COMMON_TYPES = ['FULL_NAME', 'PHONE', 'FIRST_NAME', 'LAST_NAME', 'CITY'];
  var LF_CUSTOM_TEXT = '__CUSTOM_TEXT__';
  var LF_CUSTOM_CHOICE = '__CUSTOM_CHOICE__';

  function lfSubhead(text, count) {
    return h('div', { class: 'ap-lf-subhead' }, [h('span', { text: text }), count != null ? h('span', { class: 'ap-lf-count', text: String(count) }) : null]);
  }

  function renderLeadFormEditor() {
    var d = store.draft;
    var lf = state().ad.lead_form || { mode: 'new', existing_form_id: null, new_form: {} };
    var sec = h('div', { class: 'ap-section ap-lf' }, [sectionTitle('Instant Form (lead forma)', miniBtn('AI matnlarni qayta yozsin', function () { regenerate('lead_form'); }, { cls: 'primary' }))]);
    var forms = (d.assets.lead_forms || []).map(function (f) { return { value: f.id, label: (f.name || 'Forma') + ' · ' + f.id }; });
    // 2026-09, foydalanuvchi so'rovi ("kompaniya, facebook, biznes, ads
    // menejer ulangan, ad account ulangan forumlar chiqib kelsin"): ulangan
    // Facebook sahifangizdagi TAYYOR Instant Form'lar shu yerda avtomatik
    // ko'rinadi (Meta'dan haqiqiy ro'yxat, `meta_publish.get_meta_assets`).
    // Hali bitta ham yo'q bo'lsa -- sabab shu yerda tushuntiriladi.
    sec.appendChild(fieldWrap('ad.lead_form.mode', 'Forma manbai', selectInput('ad', 'ad.lead_form.mode', [{ value: 'new', label: 'Yangi forma (Replix yaratadi)' }, { value: 'existing', label: 'Mavjud forma (Meta\'dan) — ' + forms.length + ' ta topildi', disabled: !forms.length }], { rerender: true }),
      { hint: forms.length ? 'Sahifangizda ' + forms.length + ' ta tayyor Instant Form topildi -- xohlasangiz shulardan birini tanlashingiz mumkin.' : 'Sahifangizda hali tayyor Instant Form yo\'q -- Replix hoziroq yangisini avtomatik yaratadi. Meta\'da avval yaratilgan forma bo\'lsa, ulanish/yangilanishdan keyin shu yerda ko\'rinadi.' }));
    if (lf.mode === 'existing') {
      sec.appendChild(fieldWrap('ad.lead_form.existing_form_id', 'Mavjud Instant Form', forms.length ? selectInput('ad', 'ad.lead_form.existing_form_id', forms, { allowEmpty: true, emptyLabel: 'Tanlang…' }) : h('input', { type: 'text', value: 'Sahifada tayyor forma topilmadi', disabled: true })));
      return sec;
    }
    var nf = lf.new_form || {};

    // (1) Kirish
    var intro = h('div', { class: 'ap-lf-block' }, [lfSubhead('Kirish (forma ochilganda ko\'rinadi)')]);
    intro.appendChild(fieldWrap('ad.lead_form.new_form.name', 'Forma nomi', textInput('ad', 'ad.lead_form.new_form.name', { maxlength: 100 }), { hint: 'Faqat Ads Manager\'da ko\'rinadi, mijozga emas.' }));
    var g = h('div', { class: 'ap-grid-2' });
    g.appendChild(fieldWrap('ad.lead_form.new_form.intro_headline', 'Kirish sarlavhasi', textInput('ad', 'ad.lead_form.new_form.intro_headline', { maxlength: 60 })));
    g.appendChild(fieldWrap('ad.lead_form.new_form.privacy_url', 'Maxfiylik siyosati havolasi', textInput('ad', 'ad.lead_form.new_form.privacy_url', { placeholder: 'https://…' }), { hint: 'Meta majburiy talab qiladi.' }));
    intro.appendChild(g);
    intro.appendChild(fieldWrap('ad.lead_form.new_form.intro_description', 'Kirish matni', textInput('ad', 'ad.lead_form.new_form.intro_description', { textarea: true, rows: 2, maxlength: 300 })));
    sec.appendChild(intro);

    // (2) Savollar
    // 2026-09, foydalanuvchi so'rovi ("ads menejerda instant forum
    // yaratayotganingda to'liq hali bor... multiplay choice bor va
    // boshqalar... shularni hammasini to'liq qil"): savol turlari
    // SERVERDAN keladi (`campaign_draft.LEAD_QUESTION_TYPES_ORDER` -- Ads
    // Manager'dagi TO'LIQ standart maydonlar ro'yxati), VA "Maxsus savol"
    // ikki ko'rinishda: erkin matn (javob yozadi) yoki bir nechta variant --
    // "multiple choice" (`q.options` massivi bo'lsa).
    var qtypeOptions = d.options.lead_question_types || [];
    var qtypeLabels = {};
    qtypeOptions.forEach(function (o) { qtypeLabels[o.value] = o.label; });
    var qs = nf.questions || [];
    var qBlock = h('div', { class: 'ap-lf-block' }, [lfSubhead('Savollar', qs.length)]);
    var list = h('div', { class: 'ap-lf-questions' });
    qs.forEach(function (q, i) {
      var card = h('div', { class: 'ap-lf-q' + (q.type === 'CUSTOM' ? ' custom' : '') });
      var head = h('div', { class: 'ap-lf-q-head' }, [h('span', { class: 'ap-lf-q-num', text: String(i + 1) })]);
      if (q.type === 'CUSTOM') {
        var input = h('input', { type: 'text', value: q.label || '', disabled: !editable(), placeholder: 'Savol matni', maxlength: 100, 'aria-label': 'Savol matni', dataset: { path: 'ad.lead_form.new_form.questions.' + i + '.label' } });
        input.addEventListener('input', function () { var next = JSON.parse(JSON.stringify(qs)); next[i].label = input.value; queueChange('ad', 'ad.lead_form.new_form.questions', next); });
        head.appendChild(input);
        head.appendChild(h('span', { class: 'ap-lf-q-kind', text: Array.isArray(q.options) ? 'Bir nechta variant' : 'Erkin matn' }));
      } else {
        head.appendChild(h('span', { class: 'ap-lf-q-label', text: qtypeLabels[q.type] || q.type }));
        head.appendChild(h('span', { class: 'ap-lf-q-kind', text: 'Standart maydon' }));
      }
      if (editable()) {
        head.appendChild(miniBtn('O\'chirish', function () { queueChange('ad', 'ad.lead_form.new_form.questions', qs.filter(function (_, j) { return j !== i; }), { immediate: true, rerender: true }); }, { cls: 'danger', title: 'Savolni o\'chirish' }));
      }
      card.appendChild(head);
      if (q.type === 'CUSTOM' && Array.isArray(q.options)) {
        // "Bir nechta variant" (multiple choice) -- har bir variant alohida qator.
        var optWrap = h('div', { class: 'ap-lf-opts' });
        q.options.forEach(function (opt, oi) {
          var orow = h('div', { class: 'ap-lf-opt' }, [h('span', { class: 'ap-lf-opt-dot' })]);
          var oInput = h('input', { type: 'text', value: opt || '', disabled: !editable(), placeholder: (oi + 1) + '-variant', maxlength: 80, 'aria-label': (oi + 1) + '-variant', dataset: { path: 'ad.lead_form.new_form.questions.' + i + '.options.' + oi } });
          oInput.addEventListener('input', function () { var next = JSON.parse(JSON.stringify(qs)); next[i].options[oi] = oInput.value; queueChange('ad', 'ad.lead_form.new_form.questions', next); });
          orow.appendChild(oInput);
          if (editable()) {
            orow.appendChild(miniBtn('✕', function () {
              var next = JSON.parse(JSON.stringify(qs));
              next[i].options = next[i].options.filter(function (_, j) { return j !== oi; });
              queueChange('ad', 'ad.lead_form.new_form.questions', next, { immediate: true, rerender: true });
            }, { cls: 'danger', title: 'Variantni o\'chirish' }));
          }
          optWrap.appendChild(orow);
        });
        if (editable() && q.options.length < 12) {
          optWrap.appendChild(h('div', {}, [miniBtn('+ Variant qo\'shish', function () {
            var next = JSON.parse(JSON.stringify(qs));
            next[i].options = (next[i].options || []).concat(['']);
            queueChange('ad', 'ad.lead_form.new_form.questions', next, { immediate: true, rerender: true });
          })]));
        }
        card.appendChild(optWrap);
      }
      list.appendChild(card);
    });
    if (!qs.length) { list.appendChild(h('div', { class: 'ap-lf-empty', text: 'Hali savol yo\'q -- pastdan qo\'shing.' })); }
    if (editable()) { list.appendChild(renderLeadFormComposer(qs, qtypeOptions)); }
    // Yorliq bo'sh -- blok sarlavhasi ("Savollar N") allaqachon bor; faqat manba belgisi (AI/Siz) qoladi
    qBlock.appendChild(fieldWrap('ad.lead_form.new_form.questions', '', list, { hint: 'Kamida 2 ta savol; Telefon shart (lid bilan bog\'lanish uchun). Chatda "Lead formga byudjet degan savol qo\'sh" deb ham yozsangiz bo\'ladi.' }));
    // 2026-09, halollik: foydalanuvchi Ads Manager'dagi "conditional logic"
    // (javobga qarab keyingi savolga sakrash / formani erta tugatish)ni
    // so'radi. Meta Marketing API'ning ommaviy `leadgen_forms` endpointida
    // bunday maydon YO'Q (faqat CSV asosidagi "cascading dropdown" bor, bu
    // boshqa narsa). Soxta UI qurmaymiz -- qisqa, ochiq eslatma.
    qBlock.appendChild(h('div', { class: 'ap-lf-note', text: 'Eslatma: Ads Manager\'dagi "javobga qarab savolni o\'tkazib yuborish" (conditional logic) Meta API orqali sozlanmaydi -- kerak bo\'lsa nashrdan keyin Ads Manager\'da qo\'shiladi.' }));
    sec.appendChild(qBlock);

    // (3) Rahmat xabari
    var thanks = h('div', { class: 'ap-lf-block' }, [lfSubhead('Rahmat xabari (forma yuborilgandan keyin)')]);
    var g2 = h('div', { class: 'ap-grid-2' });
    g2.appendChild(fieldWrap('ad.lead_form.new_form.thank_you_title', 'Sarlavha', textInput('ad', 'ad.lead_form.new_form.thank_you_title', { maxlength: 60 })));
    g2.appendChild(fieldWrap('ad.lead_form.new_form.thank_you_body', 'Matn', textInput('ad', 'ad.lead_form.new_form.thank_you_body', { maxlength: 300 })));
    thanks.appendChild(g2);
    sec.appendChild(thanks);
    return sec;
  }

  // Yangi savol kompozitori. Standart maydon -> darhol qo'shiladi (yozadigan
  // narsa yo'q). Maxsus savol -> avval shu yerda to'ldiriladi (matn, variant-
  // lar), faqat "Qo'shish" bosilganda ro'yxatga tushadi; tur o'zgartirilsa
  // yoki boshqa tabga o'tilsa -- qoralama tashlab yuboriladi, serverga hech
  // narsa ketmaydi.
  function renderLeadFormComposer(qs, qtypeOptions) {
    var usedTypes = {};
    qs.forEach(function (q) { usedTypes[q.type] = true; });
    var comp = store.lfComposer;
    var wrap = h('div', { class: 'ap-lf-composer' + (comp ? ' open' : '') });
    var typeSel = h('select', { 'aria-label': 'Savol turi', dataset: { path: 'lf.composer.type' } }, [h('option', { value: '', text: 'Savol turini tanlang…' })]);
    var common = h('optgroup', { label: 'Asosiy maydonlar' });
    var other = h('optgroup', { label: 'Boshqa maydonlar' });
    qtypeOptions.forEach(function (o) {
      // 2026-09, foydalanuvchi so'rovi: Email O'zbekistonda deyarli
      // ishlatilmaydi -- yangi savol sifatida taklif qilinmaydi (Meta'dan
      // import qilingan eski formada bo'lsa, ro'yxatda ko'rinishda qoladi).
      if (o.value === 'CUSTOM' || o.value === 'EMAIL' || usedTypes[o.value]) { return; }
      (LF_COMMON_TYPES.indexOf(o.value) >= 0 ? common : other).appendChild(h('option', { value: o.value, text: o.label }));
    });
    var custom = h('optgroup', { label: 'Maxsus savol' }, [
      h('option', { value: LF_CUSTOM_TEXT, text: 'Maxsus savol — erkin matn javobi' }),
      h('option', { value: LF_CUSTOM_CHOICE, text: 'Maxsus savol — bir nechta variant (multiple choice)' })
    ]);
    if (common.children.length) { typeSel.appendChild(common); }
    if (other.children.length) { typeSel.appendChild(other); }
    typeSel.appendChild(custom);
    if (comp) { typeSel.value = comp.kind === 'choice' ? LF_CUSTOM_CHOICE : LF_CUSTOM_TEXT; }
    var addBtn = miniBtn('+ Qo\'shish', function () {
      var v = typeSel.value;
      if (!v) { typeSel.focus(); return; }
      if (v === LF_CUSTOM_TEXT || v === LF_CUSTOM_CHOICE) { return; } // kompozitor ochiq -- pastdagi "Qo'shish" bilan tasdiqlanadi
      store.lfComposer = null;
      queueChange('ad', 'ad.lead_form.new_form.questions', qs.concat([{ type: v }]), { immediate: true, rerender: true });
    }, { cls: 'primary' });
    typeSel.addEventListener('change', function () {
      var v = typeSel.value;
      if (v === LF_CUSTOM_TEXT || v === LF_CUSTOM_CHOICE) {
        // Tur o'zgarsa qoralama noldan (yarim yozilgan matn aralashib ketmasin)
        store.lfComposer = { kind: v === LF_CUSTOM_CHOICE ? 'choice' : 'text', label: '', options: ['', ''], error: null };
        renderEditor();
        var first = document.querySelector('[data-path="lf.composer.label"]');
        if (first) { first.focus(); }
      } else if (store.lfComposer) {
        store.lfComposer = null;
        renderEditor();
      }
      addBtn.hidden = (v === LF_CUSTOM_TEXT || v === LF_CUSTOM_CHOICE);
    });
    addBtn.hidden = !!comp;
    wrap.appendChild(h('div', { class: 'ap-lf-composer-row' }, [typeSel, addBtn]));
    if (!comp) { return wrap; }

    // --- Maxsus savol qoralamasi (faqat lokal)
    var box = h('div', { class: 'ap-lf-draft' });
    box.appendChild(h('div', { class: 'ap-lf-draft-title', text: comp.kind === 'choice' ? 'Yangi savol — bir nechta variant' : 'Yangi savol — erkin matn javobi' }));
    var labelId = 'lf-composer-label';
    box.appendChild(h('label', { class: 'ap-field-label', for: labelId, text: 'Savol matni' }));
    var labelInput = h('input', { type: 'text', id: labelId, value: comp.label, placeholder: 'Masalan: Qaysi xizmat qiziqtiradi?', maxlength: 100, dataset: { path: 'lf.composer.label' } });
    labelInput.addEventListener('input', function () { comp.label = labelInput.value; comp.error = null; showErr(); });
    box.appendChild(labelInput);
    if (comp.kind === 'choice') {
      box.appendChild(h('div', { class: 'ap-field-label', style: 'margin-top:10px', text: 'Javob variantlari (kamida 2 ta)' }));
      var opts = h('div', { class: 'ap-lf-opts' });
      comp.options.forEach(function (opt, oi) {
        var orow = h('div', { class: 'ap-lf-opt' }, [h('span', { class: 'ap-lf-opt-dot' })]);
        var oInput = h('input', { type: 'text', value: opt, placeholder: (oi + 1) + '-variant', maxlength: 80, 'aria-label': (oi + 1) + '-variant', dataset: { path: 'lf.composer.opt.' + oi } });
        oInput.addEventListener('input', function () { comp.options[oi] = oInput.value; comp.error = null; showErr(); });
        orow.appendChild(oInput);
        if (comp.options.length > 2) {
          orow.appendChild(miniBtn('✕', function () { comp.options.splice(oi, 1); renderEditor(); }, { cls: 'danger', title: 'Variantni o\'chirish' }));
        }
        opts.appendChild(orow);
      });
      if (comp.options.length < 12) {
        opts.appendChild(h('div', {}, [miniBtn('+ Variant', function () {
          comp.options.push(''); renderEditor();
          var last = document.querySelector('[data-path="lf.composer.opt.' + (comp.options.length - 1) + '"]');
          if (last) { last.focus(); }
        })]));
      }
      box.appendChild(opts);
    }
    var errEl = h('div', { class: 'ap-field-error', id: 'lf-composer-error', role: 'alert', hidden: !comp.error, text: comp.error || '' });
    function showErr() { errEl.hidden = !comp.error; errEl.textContent = comp.error || ''; }
    box.appendChild(errEl);
    var actions = h('div', { class: 'ap-lf-draft-actions' });
    actions.appendChild(miniBtn('Qo\'shish', function () {
      var label = (comp.label || '').trim();
      if (!label) { comp.error = 'Savol matnini yozing.'; showErr(); labelInput.focus(); return; }
      var q = { type: 'CUSTOM', label: label };
      if (comp.kind === 'choice') {
        var good = comp.options.map(function (o) { return (o || '').trim(); }).filter(Boolean);
        if (good.length < 2) { comp.error = 'Kamida 2 ta variant yozing.'; showErr(); return; }
        q.options = good;
      }
      store.lfComposer = null;
      queueChange('ad', 'ad.lead_form.new_form.questions', qs.concat([q]), { immediate: true, rerender: true });
      chatSystem('Savol qo\'shildi: "' + label + '"');
    }, { cls: 'primary' }));
    actions.appendChild(miniBtn('Bekor qilish', function () { store.lfComposer = null; renderEditor(); }));
    box.appendChild(actions);
    wrap.appendChild(box);
    return wrap;
  }

  // ---- Preview
  function previewWidget(inModal) {
    var wrap = h('div');
    var tabs = h('div', { class: 'ap-preview-tabs' });
    (store.draft.options.preview_formats || []).forEach(function (f) {
      tabs.appendChild(miniBtn(PREVIEW_LABELS[f] || f, function () { store.previewFmt = f; loadPreview(frame, note, f); tabs.querySelectorAll('button').forEach(function (b) { b.classList.toggle('primary', b.dataset.fmt === f); }); }, { always: true, cls: store.previewFmt === f ? 'primary' : '' }));
      tabs.lastChild.dataset.fmt = f;
    });
    var note = h('div', { class: 'ap-preview-note local', text: 'Yuklanmoqda…' });
    var frame = h('div', { class: 'ap-preview-frame' });
    wrap.appendChild(tabs); wrap.appendChild(note); wrap.appendChild(frame);
    loadPreview(frame, note, store.previewFmt);
    return wrap;
  }
  function loadPreview(frame, note, fmt) {
    frame.innerHTML = '<div class="ap-empty">Yuklanmoqda…</div>';
    fetch(store.draft.base_url + '/preview?fmt=' + encodeURIComponent(fmt), { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (data) {
      frame.innerHTML = data.html || '';
      if (data.local) { note.className = 'ap-preview-note local'; note.textContent = 'Replix lokal ko\'rinish — Meta preview emas. ' + (data.reason || ''); }
      else { note.className = 'ap-preview-note meta'; note.textContent = 'Meta rasmiy preview (' + (PREVIEW_LABELS[fmt] || fmt) + ').'; }
    }).catch(function () { frame.innerHTML = '<div class="ap-empty">Preview olinmadi.</div>'; });
  }
  function openPreviewModal() {
    var body = document.getElementById('ap-modal-preview-body');
    body.innerHTML = '';
    body.appendChild(previewWidget(true));
    openModal('ap-modal-preview');
  }

  // ---- Jurnal
  function renderEvents() {
    var wrap = h('div', { class: 'ap-events' });
    var events = store.draft.events || [];
    if (!events.length) { wrap.appendChild(h('div', { class: 'ap-empty', text: 'Hali yozuv yo\'q.' })); return wrap; }
    events.forEach(function (e) {
      var who = e.actor_label + (e.manager_name ? ' (' + e.manager_name + ')' : '');
      var details = [];
      var dd = e.details || {};
      if (dd.changed_paths && dd.changed_paths.length) { details.push('O\'zgardi: ' + dd.changed_paths.join(', ')); }
      if (dd.reset_approvals && dd.reset_approvals.length) { details.push('Tasdiq bekor: ' + dd.reset_approvals.map(function (s) { return LEVEL_LABELS[s] || s; }).join(', ')); }
      if (dd.message) { details.push('Buyruq: "' + dd.message + '"'); }
      if (dd.error) { details.push('Xato: ' + dd.error); }
      if (dd.step) { details.push('Bosqich: ' + dd.step); }
      if (dd.sync_status) { details.push('Sinxron: ' + dd.sync_status); }
      if (dd.campaign_id) { details.push('Meta ID: ' + dd.campaign_id); }
      if (dd.warnings && dd.warnings.length) { details.push('Ogohlantirish: ' + dd.warnings.join('; ')); }
      wrap.appendChild(h('div', { class: 'ap-event' }, [
        h('div', { class: 'ap-event-time', text: fmtTime(e.created_at) }),
        h('div', {}, [h('span', { class: 'ap-event-actor', text: who }), ' — ', e.action_label + (e.scope ? ' (' + (LEVEL_LABELS[e.scope] || e.scope) + ')' : ''), details.length ? h('div', { class: 'ap-event-details', text: details.join(' · ') }) : null])
      ]));
    });
    return wrap;
  }

  // ------------------------------------------------------------------
  // CHAT (Replix AI)
  // ------------------------------------------------------------------
  function chatPush(role, text, cls, meta, links) { store.chat.push({ role: role, text: text, cls: cls, meta: meta, links: links }); renderChat(); }
  function chatSystem(text, cls) { chatPush('ai', text, cls); }
  function renderChat() {
    var log = document.getElementById('ap-chat-log');
    log.innerHTML = '';
    if (!store.chat.length) {
      log.appendChild(h('div', { class: 'ap-msg ap-msg-ai', text: 'Salom! Men qoralamani siz uchun tuzdim. Nimani o\'zgartirmoqchisiz? Masalan: "Yoshni 25-45 qil", "Faqat Toshkent qil", "Headline\'ni kuchliroq qil", "3-rasmni tanla".' }));
    }
    store.chat.forEach(function (m) {
      var el = h('div', { class: 'ap-msg ' + (m.role === 'user' ? 'ap-msg-user' : 'ap-msg-ai') + (m.cls ? ' ' + m.cls : ''), text: m.text });
      if (m.meta) { el.appendChild(h('div', { class: 'ap-msg-meta', text: m.meta })); }
      // 2026-09: bir xabarda bir nechta ad set yaratilganda -- yangi
      // qoralama(lar)ga to'g'ridan-to'g'ri o'tish havolalari.
      if (m.links && m.links.length) {
        var linksWrap = h('div', { class: 'ap-msg-links' });
        m.links.forEach(function (lk) {
          var a = h('a', { href: lk.url, text: lk.label || lk.url, target: '_blank', rel: 'noopener' });
          linksWrap.appendChild(a);
        });
        el.appendChild(linksWrap);
      }
      log.appendChild(el);
    });
    if (store.chatTyping) { log.appendChild(h('div', { class: 'ap-msg-typing', text: 'Replix AI yozmoqda…' })); }
    log.scrollTop = log.scrollHeight;
    var suggest = document.getElementById('ap-chat-suggest');
    suggest.innerHTML = '';
    var ideas = ['Yoshni 25-45 qil', 'Faqat Toshkent qil', 'Budjetni 300 ming qil', 'Buni broad qil', 'Headline\'ni kuchliroq qil'];
    if (state().objective === 'MESSAGES') { ideas.push('Tezkor javob qo\'sh: Yetkazib berish bormi?'); }
    if (state().objective === 'LEADS') { ideas.push('Lead formga byudjet degan savol qo\'sh'); }
    ideas.forEach(function (t) { suggest.appendChild(miniBtn(t, function () { document.getElementById('ap-chat-input').value = t; sendChat(); }, { always: !IS_ADMIN ? false : true, disabled: !editable() })); });
    var input = document.getElementById('ap-chat-input');
    var send = document.getElementById('ap-chat-send');
    input.disabled = !editable() || !!store.chatTyping;
    send.disabled = !editable() || !!store.chatTyping;
    if (!IS_ADMIN) { input.placeholder = 'Chat orqali tahrir faqat admin uchun'; }
  }
  function sendChat() {
    var input = document.getElementById('ap-chat-input');
    var text = input.value.trim();
    if (!text || !editable()) { return; }
    input.value = '';
    chatPush('user', text);
    store.chatTyping = true; renderChat();
    api('/ai-edit', { method: 'POST', body: { message: text } }).then(function (r) {
      store.chatTyping = false;
      var meta = [];
      if (r.applied && r.changed_paths && r.changed_paths.length) { meta.push('O\'zgardi: ' + r.changed_paths.join(', ')); }
      if (r.reset_scopes && r.reset_scopes.length) { meta.push('Tasdiq bekor qilindi: ' + r.reset_scopes.map(function (s) { return LEVEL_LABELS[s] || s; }).join(', ') + ' -- qayta ko\'rib chiqing.'); }
      if (r.warnings && r.warnings.length) { meta.push(r.warnings.join(' ')); }
      var links = (r.extra_drafts && r.extra_drafts.length) ? r.extra_drafts.map(function (d) {
        return { label: '→ ' + (d.label ? d.label + ' (' : '') + d.title + (d.label ? ')' : ''), url: d.url };
      }) : null;
      chatPush('ai', r.reply || (r.applied ? 'O\'zgartirildi.' : 'Tushunmadim.'), r.clarify ? 'warn' : null, meta.join(' · ') || null, links);
      if (r.applied && r.changed_paths && r.changed_paths.length) {
        var scope = r.changed_paths[0] === 'objective' ? 'campaign' : r.changed_paths[0].split('.')[0];
        if (LEVELS.indexOf(scope) >= 0 && store.tab !== scope) { setTab(scope); }
      }
    }).catch(function (e) {
      store.chatTyping = false;
      chatPush('ai', (e.payload && e.payload.reply) || e.message, 'err');
    });
  }

  // ------------------------------------------------------------------
  // PASTKI PANEL: Orqaga / Tasdiqlash / Keyingisi / Nashr / Faollashtirish
  // ------------------------------------------------------------------
  function renderBottombar() {
    var d = store.draft;
    var el = document.getElementById('ap-bottombar');
    el.innerHTML = '';
    var idx = LEVELS.indexOf(store.tab);
    var dots = h('span', { class: 'ap-approve-dots' }, LEVELS.map(function (l) { return h('span', { class: d.approvals[l] ? 'ok' : '', title: LEVEL_LABELS[l] }); }));
    var left = h('div', { class: 'ap-bottombar-left' }, [dots, h('span', { class: 'ap-bottombar-status', text: LEVELS.filter(function (l) { return d.approvals[l]; }).length + '/3 tasdiqlangan' + (d.has_meta_ids ? ' · Meta ID: ' + d.meta.campaign_id : '') })]);
    // 2026-09, foydalanuvchi so'rovi: to'liq tekshiruv/tasdiq/nashrdan
    // o'tmasdan ham, ANIQ "saqlandi" deb bilish tugmasi -- hech qanday
    // tasdiq yoki tekshiruvni talab qilmaydi (holat allaqachon har bir
    // tahrirda avtomatik saqlanadi, bu shunchaki buni ANIQ tasdiqlaydi).
    if (IS_ADMIN && d.is_editable && (d.status === 'draft' || d.status === 'failed')) {
      var saveBtn = h('button', { type: 'button', class: 'btn btn-outline ap-btn-sm', text: 'Qoralama sifatida saqlash' });
      saveBtn.addEventListener('click', function () { saveDraftExplicit(saveBtn); });
      left.appendChild(saveBtn);
    }
    var right = h('div', { class: 'ap-bottombar-right' });
    // 2026-09, Target Analizi: faqat Meta'ga chiqarilgan (jonli/import
    // qilingan) kampaniyalar uchun ma'no beradi -- yangi, hali nashr
    // qilinmagan qoralamada haqiqiy statistika yo'q.
    if (d.has_meta_ids) {
      var taBtn = h('button', { type: 'button', class: 'btn btn-outline ap-btn-sm', html: '&#128269; Target Analizi' });
      taBtn.addEventListener('click', openTargetAnalizModal);
      right.appendChild(taBtn);
    }
    var back = h('button', { type: 'button', class: 'btn btn-outline ap-btn-sm', text: 'Orqaga', disabled: idx <= 0 });
    back.addEventListener('click', function () { if (idx > 0) { setTab(LEVELS[idx - 1]); } });
    right.appendChild(back);
    if (idx >= 0) {
      var scope = LEVELS[idx];
      var errs = errorsForScope(scope);
      var approved = d.approvals[scope];
      var canApprove = IS_ADMIN && d.is_editable && !approved && !errs.length;
      var wrapA = h('span', { class: 'ap-tooltip-wrap' });
      if (!IS_ADMIN) { wrapA.dataset.tip = 'Tasdiqlash faqat admin uchun.'; }
      else if (errs.length) { wrapA.dataset.tip = errs.map(function (e) { return e.message; }).join(' • '); }
      var approve = h('button', { type: 'button', class: 'btn ap-btn-sm' + (approved ? ' ap-btn-good' : ''), disabled: !canApprove, html: approved ? svgIcon('check') + ' Tasdiqlangan' : (LEVEL_LABELS[scope] + 'ni tasdiqlash') });
      if (approved) { approve.querySelector('svg').style.cssText = 'width:13px;height:13px;vertical-align:-2px;margin-right:4px'; }
      approve.addEventListener('click', function () {
        approve.disabled = true;
        api('/approve', { method: 'POST', body: { scope: scope } }).then(function () {
          chatSystem(LEVEL_LABELS[scope] + ' tasdiqlandi.');
          if (idx < LEVELS.length - 1) { setTab(LEVELS[idx + 1]); }
        }).catch(function (e) { chatSystem(e.message, 'err'); renderBottombar(); });
      });
      wrapA.appendChild(approve);
      right.appendChild(wrapA);
    }
    var isLast = idx === LEVELS.length - 1;
    if (!isLast && idx >= 0) {
      var next = h('button', { type: 'button', class: 'btn btn-outline ap-btn-sm', text: 'Keyingisi →' });
      next.addEventListener('click', function () { setTab(LEVELS[idx + 1]); });
      right.appendChild(next);
    }
    if (store.tab === 'events') {
      var toAd = h('button', { type: 'button', class: 'btn btn-outline ap-btn-sm', text: 'Reklamaga qaytish' });
      toAd.addEventListener('click', function () { setTab('ad'); });
      right.appendChild(toAd);
    }
    // Yakuniy harakat
    if (d.status === 'published') {
      var act = h('button', { type: 'button', class: 'btn ap-btn-sm ap-btn-good', text: 'Faollashtirish', disabled: !IS_ADMIN });
      act.addEventListener('click', openActivateModal);
      right.appendChild(act);
      if (d.sync_status === 'local_changes' && IS_ADMIN) { right.appendChild(pushButton()); }
    } else if (d.status === 'active') {
      right.appendChild(h('span', { class: 'badge badge-color-good', text: 'Meta\'da faol' }));
      if (d.sync_status === 'local_changes' && IS_ADMIN) { right.appendChild(pushButton()); }
    } else if (d.status !== 'archived' && d.status !== 'publishing') {
      var wrapP = h('span', { class: 'ap-tooltip-wrap' });
      var reasons = d.publish_reasons || [];
      if (!d.can_publish) { wrapP.dataset.tip = reasons.length ? reasons.join(' • ') : 'Uchala darajani tasdiqlang.'; }
      var pub = h('button', { type: 'button', class: 'btn ap-btn-sm ap-btn-publish', disabled: !d.can_publish || !IS_ADMIN, html: svgIcon('rocket') + ' Meta\'ga nashr qilish' });
      pub.querySelector('svg').style.cssText = 'width:13px;height:13px;vertical-align:-2px;margin-right:5px';
      pub.addEventListener('click', openSummaryModal);
      wrapP.appendChild(pub);
      right.appendChild(wrapP);
    } else if (d.status === 'publishing') {
      right.appendChild(h('span', { class: 'badge badge-color-warn', text: 'Nashr qilinmoqda…' }));
    }
    el.appendChild(left); el.appendChild(right);
  }
  function pushButton() {
    var b = h('button', { type: 'button', class: 'btn btn-outline ap-btn-sm', text: 'O\'zgarishlarni Meta\'ga yuborish' });
    b.addEventListener('click', function () {
      if (!confirm('Lokal o\'zgarishlar Meta\'dagi kampaniyaga yuboriladi. Davom etaymi?')) { return; }
      b.disabled = true;
      api('/push', { method: 'POST', body: {} }).then(function (r) {
        var res = r.result || {};
        chatSystem('Meta\'ga yuborildi: ' + ((res.pushed || []).join(', ') || 'hech narsa') + ((res.skipped || []).length ? '. O\'tkazib yuborildi (API qo\'llamaydi): ' + res.skipped.join(', ') : ''));
      }).catch(function (e) { chatSystem(e.message, 'err'); renderBottombar(); });
    });
    return b;
  }

  // ------------------------------------------------------------------
  // MODALLAR
  // ------------------------------------------------------------------
  function openModal(id) { document.getElementById(id).classList.add('open'); }
  function closeModal(id) { document.getElementById(id).classList.remove('open'); }
  document.querySelectorAll('[data-close]').forEach(function (b) { b.addEventListener('click', function () { closeModal(b.dataset.close); }); });
  document.querySelectorAll('.modal-overlay').forEach(function (ov) { ov.addEventListener('click', function (e) { if (e.target === ov) { ov.classList.remove('open'); } }); });

  function openSummaryModal() {
    var body = document.getElementById('ap-modal-summary-body');
    body.innerHTML = '<div class="ap-empty">Yuklanmoqda…</div>';
    openModal('ap-modal-summary');
    fetch(store.draft.base_url + '/summary', { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (data) {
      body.innerHTML = '';
      var s = data.summary || {};
      var dl = h('dl', { class: 'ap-summary-grid' });
      [['Maqsad', s.objective_label], ['Kampaniya', s.campaign_name], ['Ad Set', s.adset_name], ['Reklama', s.ad_name], ['Byudjet', s.budget_line], ['Muddat', s.duration_line],
       ['Taxminiy jami', s.estimated_total_line], ['Hudud', (s.locations || []).join(', ') || '—'], ['Auditoriya', s.audience_line], ['Joylashuv', s.placements_line],
       ['Kreativ', s.creative], ['Sarlavha', s.headline], ['Tugma', s.cta_label], ['Yo\'nalish', s.destination_type || '—']].forEach(function (kv) {
        dl.appendChild(h('dt', { text: kv[0] })); dl.appendChild(h('dd', { text: kv[1] == null ? '—' : String(kv[1]) }));
      });
      body.appendChild(dl);
      if (s.primary_text) { body.appendChild(h('div', { class: 'ap-field-hint', style: 'white-space:pre-wrap;margin-bottom:12px', text: s.primary_text })); }

      // 2026-09, foydalanuvchi so'rovi: "srazu aktiv holatga chiqazadigan
      // qilish kerak, pauzaga emas" -- standart ACTIVE, lekin Ads
      // Manager'dagi kabi xohlasa PAUSED'ga o'tkazishi mumkin.
      var launchActive = store.draft.launch_active !== false;
      var launchWrap = h('div', { class: 'ap-launch-toggle', role: 'radiogroup', 'aria-label': 'Nashrdan keyingi holat' });
      launchWrap.appendChild(h('span', { text: 'Nashr qilingach:' }));
      var mkRadio = function (val, label) {
        var lbl = h('label');
        var inp = h('input', { type: 'radio', name: 'launch_status_toggle', value: val ? 'active' : 'paused', checked: launchActive === val, disabled: !IS_ADMIN });
        inp.addEventListener('change', function () {
          launchActive = val;
          api('/launch-status', { method: 'POST', body: { active: val } }).catch(function (e) {
            // Saqlanmasa -- foydalanuvchi bilsin (real pul sarfi shunga bog'liq)
            launchActive = store.draft.launch_active !== false;
            launchWrap.querySelectorAll('input').forEach(function (r) { r.checked = (r.value === 'active') === launchActive; });
            chatSystem('Nashr holati saqlanmadi: ' + e.message, 'err');
          });
          okMsg.textContent = launchActive
            ? 'Hammasi joyida. Kampaniya Meta\'da ACTIVE (darhol ishga tushadi) holatda yaratiladi.'
            : 'Hammasi joyida. Kampaniya Meta\'da PAUSED holatda yaratiladi -- pul sarflanmaydi. Keyin "Faollashtirish" bilan yoqasiz.';
        });
        lbl.appendChild(inp); lbl.appendChild(h('span', { text: label }));
        return lbl;
      };
      launchWrap.appendChild(mkRadio(true, 'Darhol yoqilsin (Active)'));
      launchWrap.appendChild(mkRadio(false, 'Pauzada qolsin -- keyin qo\'lda yoqaman'));
      body.appendChild(launchWrap);

      var reasons = data.reasons || [];
      var okMsg = h('div', { class: 'ap-modal-msg ok', text: launchActive
        ? 'Hammasi joyida. Kampaniya Meta\'da ACTIVE (darhol ishga tushadi) holatda yaratiladi.'
        : 'Hammasi joyida. Kampaniya Meta\'da PAUSED holatda yaratiladi -- pul sarflanmaydi. Keyin "Faollashtirish" bilan yoqasiz.' });
      if (reasons.length) {
        body.appendChild(h('div', { class: 'ap-modal-msg err' }, ['Nashr qilib bo\'lmaydi:', h('ul', {}, reasons.map(function (r) { return h('li', { text: r }); }))]));
      } else {
        body.appendChild(okMsg);
      }
      var actions = h('div', { class: 'ap-modal-actions' });
      var back = h('button', { type: 'button', class: 'btn btn-outline', text: 'Orqaga' });
      back.addEventListener('click', function () { closeModal('ap-modal-summary'); });
      var go = h('button', { type: 'button', class: 'btn ap-btn-publish', text: 'Tasdiqlash va nashr qilish', disabled: !!reasons.length || !IS_ADMIN });
      go.addEventListener('click', function () {
        go.disabled = true; back.disabled = true;
        go.innerHTML = '<span class="btn-spinner"></span> Meta\'ga yuborilmoqda…';
        api('/publish', { method: 'POST', body: {} }).then(function (r) {
          body.innerHTML = '';
          var res = r.result || {};
          var statusWord = launchActive ? 'ACTIVE' : 'PAUSED';
          body.appendChild(h('div', { class: 'ap-modal-msg ok', text: 'Kampaniya Meta\'da yaratildi (' + statusWord + '). Campaign ' + res.campaign_id + ' · Ad Set ' + res.adset_id + ' · Ad ' + res.ad_id }));
          if (res.warnings && res.warnings.length) { body.appendChild(h('div', { class: 'ap-modal-msg warn' }, [h('ul', {}, res.warnings.map(function (w) { return h('li', { text: w }); }))])); }
          var a2 = h('div', { class: 'ap-modal-actions' });
          var close = h('button', { type: 'button', class: 'btn btn-outline', text: 'Yopish' }); close.addEventListener('click', function () { closeModal('ap-modal-summary'); });
          a2.appendChild(close);
          if (!launchActive) {
            var act = h('button', { type: 'button', class: 'btn ap-btn-good', text: 'Faollashtirish' }); act.addEventListener('click', function () { closeModal('ap-modal-summary'); openActivateModal(); });
            a2.appendChild(act);
          }
          body.appendChild(a2);
          chatSystem('Meta\'ga chiqarildi (' + statusWord + ').');
        }).catch(function (e) {
          body.appendChild(h('div', { class: 'ap-modal-msg err', text: e.message + (e.payload && e.payload.step ? ' (bosqich: ' + e.payload.step + ')' : '') }));
          back.disabled = false; go.disabled = false; go.textContent = 'Qayta urinish';
        });
      });
      actions.appendChild(back); actions.appendChild(go); body.appendChild(actions);
    }).catch(function () { body.innerHTML = '<div class="ap-modal-msg err">Xulosa olinmadi.</div>'; });
  }

  function openActivateModal() {
    var body = document.getElementById('ap-modal-activate-body');
    body.innerHTML = '';
    var s = state();
    body.appendChild(h('div', { class: 'ap-modal-msg warn', text: 'Kampaniya, Ad Set va reklama Meta\'da ACTIVE qilinadi -- reklama xarajati boshlanadi: ' + fmtMoney(s.adset.daily_budget || s.adset.lifetime_budget, s.adset.currency) + ((s.adset.budget_type || 'daily') === 'daily' ? ' / kun' : ' jami') + '.' }));
    var actions = h('div', { class: 'ap-modal-actions' });
    var back = h('button', { type: 'button', class: 'btn btn-outline', text: 'Bekor qilish' }); back.addEventListener('click', function () { closeModal('ap-modal-activate'); });
    var go = h('button', { type: 'button', class: 'btn ap-btn-good', text: 'Ha, faollashtirish', disabled: !IS_ADMIN });
    go.addEventListener('click', function () {
      go.disabled = true; back.disabled = true;
      api('/activate', { method: 'POST', body: { confirm: true } }).then(function () {
        body.innerHTML = '';
        body.appendChild(h('div', { class: 'ap-modal-msg ok', text: 'Kampaniya faollashtirildi. Natijalarni Marketing > Target bo\'limida kuzating.' }));
        var a2 = h('div', { class: 'ap-modal-actions' });
        var close = h('button', { type: 'button', class: 'btn', text: 'Yopish' }); close.addEventListener('click', function () { closeModal('ap-modal-activate'); });
        a2.appendChild(close); body.appendChild(a2);
        chatSystem('Kampaniya Meta\'da faollashtirildi.');
      }).catch(function (e) {
        body.appendChild(h('div', { class: 'ap-modal-msg err', text: e.message }));
        back.disabled = false; go.disabled = false;
      });
    });
    actions.appendChild(back); actions.appendChild(go); body.appendChild(actions);
    openModal('ap-modal-activate');
  }

  // ------------------------------------------------------------------
  // TARGET ANALIZI -- AI diagnostika (Meta statistikasi + CRM lid sifati +
  // o'rnatilgan yaxshi amaliyotlar), so'ng foydalanuvchi checkbox bilan
  // ALOHIDA-ALOHIDA tasdiqlagan o'zgarishlarnigina jonli Meta kampaniyasiga
  // qo'llanadi. Hech narsa avtomatik/oldindan belgilangan holda qo'llanmaydi.
  // ------------------------------------------------------------------
  var TA_SEVERITY_LABEL = { past: 'Past', "o'rtacha": "O'rtacha", yuqori: 'Yuqori' };
  var TA_SEVERITY_CLS = { past: 'dim', "o'rtacha": 'warn', yuqori: 'bad' };

  function taFmtVal(v) {
    if (v == null || v === '') { return '—'; }
    if (Array.isArray(v)) {
      if (!v.length) { return '—'; }
      return v.map(function (x) { return (x && typeof x === 'object') ? (x.name || x.label || JSON.stringify(x)) : String(x); }).join(', ');
    }
    if (typeof v === 'object') { return JSON.stringify(v); }
    return String(v);
  }

  function openTargetAnalizModal() {
    var body = document.getElementById('ap-modal-target-analiz-body');
    body.innerHTML = '';
    body.appendChild(h('div', { class: 'ap-empty' }, [
      h('span', { class: 'btn-spinner' }), ' Tahlil qilinmoqda — Meta statistikasi, CRM va AI xulosasi (bir necha soniya davom etishi mumkin)…'
    ]));
    openModal('ap-modal-target-analiz');
    api('/target-analiz', { method: 'POST', body: {} }).then(function (r) {
      renderTargetAnaliz(r.result);
    }).catch(function (e) {
      body.innerHTML = '';
      body.appendChild(h('div', { class: 'ap-modal-msg err', text: e.message }));
      var close = h('button', { type: 'button', class: 'btn btn-outline', text: 'Yopish' });
      close.addEventListener('click', function () { closeModal('ap-modal-target-analiz'); });
      body.appendChild(close);
    });
  }

  function renderTargetAnaliz(result) {
    var body = document.getElementById('ap-modal-target-analiz-body');
    body.innerHTML = '';
    if (!result) { body.appendChild(h('div', { class: 'ap-modal-msg err', text: 'Natija olinmadi.' })); return; }
    if (!result.sufficient) {
      body.appendChild(h('div', { class: 'ap-modal-msg warn', text: result.reason || 'Hali yetarli ma\'lumot yo\'q.' }));
      var closeEarly = h('div', { class: 'ap-modal-actions' }, [(function () {
        var b = h('button', { type: 'button', class: 'btn btn-outline', text: 'Yopish' });
        b.addEventListener('click', function () { closeModal('ap-modal-target-analiz'); });
        return b;
      })()]);
      body.appendChild(closeEarly);
      return;
    }
    if (result.summary) { body.appendChild(h('div', { class: 'ap-field-hint', style: 'margin-bottom:14px;font-size:13px', text: result.summary })); }

    var perf = result.performance || {};
    var w30 = perf.last_30d || {};
    var w7 = perf.last_7d || {};
    var crm = result.crm || {};
    var dl = h('dl', { class: 'ap-summary-grid' });
    [
      ['So\'nggi 7 kun — xarajat', fmtMoney(w7.spend, store.draft.currency)],
      ['So\'nggi 7 kun — natija', w7.results != null ? (w7.results + ' (' + w7.result_label + ')') : '—'],
      ['So\'nggi 7 kun — 1 natija narxi', w7.cost_per_result != null ? fmtMoney(w7.cost_per_result, store.draft.currency) : '—'],
      ['So\'nggi 30 kun — xarajat', fmtMoney(w30.spend, store.draft.currency)],
      ['So\'nggi 30 kun — natija', w30.results != null ? (w30.results + ' (' + w30.result_label + ')') : '—'],
      ['Eng yuqori chastota (so\'nggi 7 kun)', perf.max_frequency_recent_7d != null ? perf.max_frequency_recent_7d : '—'],
      ['CRM lid sifati (30 kun)', crm.total ? (crm.won + ' yutilgan / ' + crm.lost + ' yo\'qotilgan / jami ' + crm.total) : 'Bu davrda CRM\'da lid topilmadi']
    ].forEach(function (kv) { dl.appendChild(h('dt', { text: kv[0] })); dl.appendChild(h('dd', { text: kv[1] == null ? '—' : String(kv[1]) })); });
    body.appendChild(dl);

    if (result.issues && result.issues.length) {
      body.appendChild(sectionTitle('Aniqlangan muammolar'));
      var issuesWrap = h('div', { class: 'ap-ta-issues' });
      result.issues.forEach(function (it) {
        issuesWrap.appendChild(h('div', { class: 'ap-ta-issue' }, [
          h('div', { class: 'ap-ta-issue-head' }, [
            h('span', { class: 'badge badge-color-' + (TA_SEVERITY_CLS[it.severity] || 'dim'), text: TA_SEVERITY_LABEL[it.severity] || it.severity }),
            h('strong', { text: it.issue })
          ]),
          it.evidence ? h('div', { class: 'ap-field-hint', text: it.evidence }) : null
        ]));
      });
      body.appendChild(issuesWrap);
    }

    var selected = {};
    var applyBtn;
    function updateApplyBtn() { if (applyBtn) { applyBtn.disabled = !Object.keys(selected).length || !IS_ADMIN; } }

    if (result.changes && result.changes.length) {
      body.appendChild(sectionTitle('Taklif qilingan o\'zgarishlar — faqat belgilanganlari qo\'llanadi'));
      var changesWrap = h('div', { class: 'ap-ta-changes' });
      result.changes.forEach(function (ch, i) {
        var cb = h('input', { type: 'checkbox', disabled: !IS_ADMIN });
        cb.addEventListener('change', function () { if (cb.checked) { selected[i] = ch; } else { delete selected[i]; } updateApplyBtn(); });
        changesWrap.appendChild(h('div', { class: 'ap-ta-change' }, [
          h('label', { class: 'ap-check' }, [cb, h('code', { text: ch.path })]),
          h('div', { class: 'ap-ta-diff' }, [
            h('span', { class: 'ap-ta-before', text: taFmtVal(ch.current_value) }),
            h('span', { class: 'ap-ta-arrow', text: ' → ' }),
            h('span', { class: 'ap-ta-after', text: taFmtVal(ch.proposed_value) })
          ]),
          ch.why ? h('div', { class: 'ap-field-hint', text: ch.why }) : null
        ]));
      });
      body.appendChild(changesWrap);
    } else {
      body.appendChild(h('div', { class: 'ap-field-hint', text: 'AI hozircha aniq, dalilga asoslangan taklif topmadi.' }));
    }

    var actions = h('div', { class: 'ap-modal-actions' });
    var close = h('button', { type: 'button', class: 'btn btn-outline', text: 'Yopish' });
    close.addEventListener('click', function () { closeModal('ap-modal-target-analiz'); });
    applyBtn = h('button', { type: 'button', class: 'btn ap-btn-good', text: 'Tanlanganlarni qo\'llash', disabled: true });
    applyBtn.addEventListener('click', function () {
      var payload = Object.keys(selected).map(function (i) { var ch = selected[i]; return { path: ch.path, value: ch.proposed_value }; });
      if (!payload.length) { return; }
      if (!confirm('Tanlangan ' + payload.length + ' ta o\'zgarish JONLI Meta kampaniyasiga yuboriladi. Davom etaymi?')) { return; }
      applyBtn.disabled = true; close.disabled = true;
      applyBtn.innerHTML = '<span class="btn-spinner"></span> Qo\'llanmoqda…';
      api('/target-analiz/qollash', { method: 'POST', body: { changes: payload } }).then(function (r) {
        body.innerHTML = '';
        body.appendChild(h('div', { class: 'ap-modal-msg ok', text: 'Qo\'llandi va Meta\'ga yuborildi: ' + ((r.changed_paths || []).join(', ') || 'hech narsa') }));
        if (r.skipped && r.skipped.length) { body.appendChild(h('div', { class: 'ap-modal-msg warn', text: 'O\'tkazib yuborildi: ' + r.skipped.join(', ') })); }
        var a2 = h('div', { class: 'ap-modal-actions' });
        var closeBtn2 = h('button', { type: 'button', class: 'btn', text: 'Yopish' });
        closeBtn2.addEventListener('click', function () { closeModal('ap-modal-target-analiz'); });
        a2.appendChild(closeBtn2); body.appendChild(a2);
        chatSystem('Target Analizi: tanlangan o\'zgarishlar Meta\'ga qo\'llandi.');
      }).catch(function (e) {
        body.appendChild(h('div', { class: 'ap-modal-msg err', text: e.message }));
        close.disabled = false; applyBtn.disabled = false; applyBtn.textContent = 'Qayta urinish';
      });
    });
    actions.appendChild(close); actions.appendChild(applyBtn);
    body.appendChild(actions);
  }

  // ------------------------------------------------------------------
  // Boshlash
  // ------------------------------------------------------------------
  function render() { renderTopbar(); renderTree(); renderTabs(); renderEditor(); renderChat(); renderBottombar(); }
  document.getElementById('ap-chat-form').addEventListener('submit', function (e) { e.preventDefault(); sendChat(); });
  document.getElementById('ap-chat-input').addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); } });
  window.addEventListener('beforeunload', function (e) { if (Object.keys(store.pending).length || store.inflight) { e.preventDefault(); e.returnValue = ''; } });
  // Boshlang'ich tab: tasdiqlanmagan birinchi daraja
  store.tab = LEVELS.filter(function (l) { return !store.draft.approvals[l]; })[0] || 'ad';
  render();
})();
