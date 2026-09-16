/* creative_studio.js — Kreativ studiya (AI rasm-generatsiya) muharriri
   (2026-09, foydalanuvchi so'rovi: "AI reklama rasmini yaratsin, avval
   savol-javob, natijani brauzerda tahrirlash (matn/logo pozitsiyasi),
   PNG/PDF yuklab olish, Avtopilotda ishlatish").

   Vanilla JS, freymvorksiz, CDN'siz -- `autopilot.js` bilan bir xil naqsh:
     1. store      -- BITTA kanonik asset (`#cs-data` JSON'idan); server har
                      javobda TO'LIQ yangilangan `asset`ni qaytaradi va biz
                      uni butunlay almashtiramiz (holat, brif, qatlamlar,
                      rasm URL'lari, kvota -- bitta haqiqat manbai).
     2. api()      -- fetch + CSRF (X-CSRFToken) + o'zbekcha xato.
     3. render*()  -- holatga qarab: savol-javob kartasi (collecting_brief),
                      spinner (generating), canvas-muharrir (ready), xato
                      kartasi (failed).
     4. canvas     -- matnsiz fon (`base_image_url`) + qatlamlar JS
                      tomonidan chiziladi (Pillow render'ining oldindan
                      ko'rinishi); sichqoncha/sensor bilan sudrash va
                      o'lcham; "Saqlash" -> POST /layers -> server QAYTA
                      chizadi (server har doim haqiqat manbai).
   Ikkinchi kichik IIFE (pastda) -- galereya sahifasidagi "Bu reklamada
   ishlatish" tugmasi (Avtopilotdan kelinganda). */
(function () {
  'use strict';

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
  function fmtTime(iso) {
    if (!iso) { return '—'; }
    var d = new Date(iso);
    if (isNaN(d.getTime())) { return iso; }
    var p = function (n) { return (n < 10 ? '0' : '') + n; };
    return p(d.getDate()) + '.' + p(d.getMonth() + 1) + '.' + d.getFullYear() + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }
  function svgIcon(name) {
    var paths = {
      check: '<path stroke-linecap="round" stroke-linejoin="round" d="M5 12l4 4L19 7"/>',
      refresh: '<path stroke-linecap="round" stroke-linejoin="round" d="M4 4v6h6M20 20v-6h-6M5 15a7 7 0 0011.9 3.1M19 9A7 7 0 007.1 5.9"/>',
      download: '<path stroke-linecap="round" stroke-linejoin="round" d="M12 4v12m0 0l-4-4m4 4l4-4"/><path d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2"/>',
      eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
      eyeOff: '<path d="M3 3l18 18M10.6 10.6A3 3 0 0013.4 13.4M9.9 5.1A10 10 0 0112 5c6.5 0 10 7 10 7a17 17 0 01-3.2 4.1M6.6 6.6A17 17 0 002 12s3.5 7 10 7c1.4 0 2.7-.3 3.9-.8"/>',
      rocket: '<path d="M4.5 16.5c-1.5 1.3-2 5-2 5s3.7-.5 5-2c.7-.8.7-2 0-2.7-.7-.7-2-.7-3 0z"/><path d="M12 15l-3-3 2.5-5.5A9 9 0 0121.5 2.5 9 9 0 0117.5 12.5L12 15z"/>',
      plus: '<path stroke-linecap="round" d="M12 5v14M5 12h14"/>',
      trash: '<path stroke-linecap="round" d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
      warn: '<path d="M12 9v4m0 4h.01M10.3 3.9L2.5 17.5A2 2 0 004.2 20.5h15.6a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"/>'
    };
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">' + (paths[name] || '') + '</svg>';
  }
  function miniBtn(label, onclick, opts) {
    opts = opts || {};
    var b = h('button', { type: 'button', class: 'ap-mini-btn' + (opts.cls ? ' ' + opts.cls : ''), disabled: !!opts.disabled, title: opts.title });
    if (opts.icon) { b.innerHTML = svgIcon(opts.icon) + ' '; b.querySelector('svg').style.cssText = 'width:12px;height:12px;vertical-align:-2px'; }
    b.appendChild(document.createTextNode(label));
    b.addEventListener('click', onclick);
    return b;
  }
  function bigBtn(label, onclick, opts) {
    opts = opts || {};
    var b = h('button', { type: 'button', class: 'btn ap-btn-sm' + (opts.cls ? ' ' + opts.cls : ''), disabled: !!opts.disabled, title: opts.title });
    if (opts.icon) { b.innerHTML = svgIcon(opts.icon) + ' '; b.querySelector('svg').style.cssText = 'width:13px;height:13px;vertical-align:-2px;margin-right:4px'; }
    b.appendChild(document.createTextNode(label));
    b.addEventListener('click', onclick);
    return b;
  }
  function statusChip(label, cls) { return h('span', { class: 'badge badge-color-' + cls + ' ap-chip-status' }, [h('span', { class: 'ap-dot' }), label]); }
  function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }
  function round3(v) { return Math.round(v * 1000) / 1000; }

  // ==================================================================
  // MUHARRIR SAHIFASI
  // ==================================================================
  var root = document.getElementById('cs-app');
  if (root) {
    var CSRF = root.dataset.csrf;
    var store = {
      asset: JSON.parse(document.getElementById('cs-data').textContent),
      serverLayers: null,   // oxirgi saqlangan qatlamlar (bekor qilish uchun)
      selected: -1,         // tanlangan qatlam indeksi
      busy: null,           // null | 'brief' | 'generating' | 'saving' | 'using' | 'chatedit'
      msg: null,            // {text, cls}
      dirty: false,
      baseImg: null, baseSrc: null,
      logoImg: null, logoSrc: null,
      drag: null,
      editChat: []          // "AI bilan tezkor o'zgartirish" -- faqat shu sahifa sessiyasida (serverda saqlanmaydi)
    };
    store.serverLayers = JSON.parse(JSON.stringify(store.asset.layers || []));
    var LAYER_LABELS = { text: 'Matn', badge: 'Tugma/belgi', panel: 'Panel', logo: 'Logotip' };
    var FONT_STACK = '"DejaVu Sans", "Plus Jakarta Sans", Arial, sans-serif';

    // ---- API
    function apiUrl(path) {
      var url = store.asset.base_url + (path || '');
      if (store.asset.from_autopilot) { url += (url.indexOf('?') >= 0 ? '&' : '?') + 'from_autopilot=' + encodeURIComponent(store.asset.from_autopilot); }
      return url;
    }
    function api(path, opts) {
      opts = opts || {};
      var init = { method: opts.method || 'GET', credentials: 'same-origin', headers: { 'X-CSRFToken': CSRF } };
      if (opts.body instanceof FormData) { init.body = opts.body; }
      else if (opts.body != null) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.body); }
      return fetch(opts.absolute || apiUrl(path), init).then(function (r) {
        return r.json().catch(function () { return { error: 'Server javobi tushunarsiz (' + r.status + ').' }; }).then(function (json) {
          if (json && json.asset) { replaceAsset(json.asset); }
          if (!r.ok) { var err = new Error(json.error || 'Xatolik (' + r.status + ')'); err.payload = json; err.status = r.status; throw err; }
          return json;
        });
      });
    }
    function replaceAsset(asset) {
      var keepFrom = store.asset.from_autopilot;
      var keepAuto = (store.asset.urls || {}).autopilot;
      store.asset = asset;
      if (!store.asset.from_autopilot && keepFrom) { store.asset.from_autopilot = keepFrom; store.asset.urls = store.asset.urls || {}; store.asset.urls.autopilot = keepAuto; }
      store.serverLayers = JSON.parse(JSON.stringify(asset.layers || []));
      store.dirty = false;
      if (store.selected >= (asset.layers || []).length) { store.selected = -1; }
      render();
    }
    function setMsg(text, cls) { store.msg = text ? { text: text, cls: cls || 'ok' } : null; renderBanner(); }

    // ---- Rasmlar
    function ensureImages() {
      var a = store.asset;
      if (a.base_image_url && a.base_image_url !== store.baseSrc) {
        store.baseSrc = a.base_image_url;
        var img = new Image();
        img.onload = function () { store.baseImg = img; drawCanvas(); };
        img.onerror = function () { store.baseImg = null; drawCanvas(); };
        img.src = a.base_image_url;
      }
      var logo = (a.brand || {}).logo_url;
      if (logo && logo !== store.logoSrc) {
        store.logoSrc = logo;
        var li = new Image();
        li.onload = function () { store.logoImg = li; drawCanvas(); };
        li.onerror = function () { store.logoImg = null; drawCanvas(); };
        li.src = logo;
      }
      if (!logo) { store.logoImg = null; store.logoSrc = null; }
    }

    // ==================================================================
    // TOP BAR + BANNER
    // ==================================================================
    function renderTopbar() {
      var a = store.asset;
      var el = document.getElementById('cs-topbar');
      el.innerHTML = '';
      var listUrl = (a.urls.list || '/kreativ') + (a.from_autopilot ? '?from_autopilot=' + a.from_autopilot : '');
      var left = h('div', { class: 'ap-topbar-left' }, [
        h('a', { href: listUrl, class: 'ap-mini-btn', text: '← Kreativlar', style: 'text-decoration:none' }),
        h('span', { class: 'ap-topbar-title', text: a.title }),
        statusChip(a.status_label, a.status_color),
        h('span', { class: 'badge badge-color-' + (a.kind === 'ai_generated' ? 'blue' : 'dim'), text: a.kind_label }),
        h('span', { class: 'badge badge-color-dim', text: a.aspect }),
        a.template ? h('span', { class: 'ap-topbar-meta', text: 'Shablon: ' + a.template.name }) : null
      ]);
      var q = a.quota || {};
      var right = h('div', { class: 'ap-topbar-actions' }, [
        h('span', { class: 'cs-quota-chip' + (q.can_generate ? '' : ' warn'), text: q.label || '' })
      ]);
      if (a.from_autopilot && a.urls.autopilot) {
        right.appendChild(h('a', { href: a.urls.autopilot, class: 'ap-mini-btn', text: 'Avtopilotga qaytish', style: 'text-decoration:none' }));
      }
      right.appendChild(miniBtn('O\'chirish', function () {
        if (!confirm('Kreativni o\'chirasizmi? Bu amalni qaytarib bo\'lmaydi.')) { return; }
        api('/ochirish', { method: 'POST', body: {} }).then(function () { window.location = listUrl; }).catch(function (e) { setMsg(e.message, 'err'); });
      }, { cls: 'danger', icon: 'trash' }));
      el.appendChild(left); el.appendChild(right);
    }

    function renderBanner() {
      var a = store.asset;
      var el = document.getElementById('cs-banner');
      el.innerHTML = '';
      var q = a.quota || {};
      if (store.msg) {
        el.appendChild(h('div', { class: 'ap-modal-msg ' + store.msg.cls, style: 'margin-bottom:0' }, [store.msg.text]));
      }
      if (!q.can_generate && (a.status === 'collecting_brief' || a.status === 'failed')) {
        var card = h('div', { class: 'card ap-warn-card' }, [
          h('h2', { text: 'AI generatsiya hozir yopiq' }),
          h('p', { style: 'margin:0;font-size:13px' }, [q.label + '. ', h('a', { href: a.urls.pricing || '#', text: 'Tarifni oshiring' }), ' yoki ', h('a', { href: a.urls.templates || '#', text: 'shablondan boshlang' }), ' (kvota sarflanmaydi).'])
        ]);
        el.appendChild(card);
      }
    }

    // ==================================================================
    // BRIF (AI SUHBAT -- 2026-09, foydalanuvchi fikri: "savollar bir xil
    // shablon bo'lmasin, agent ishlasin" -- statik 5 ta savol o'rniga
    // AI agent navbatma-navbat, foydalanuvchi aytganiga qarab, keyingi
    // savolni o'zi tanlaydi; `asset.brief_conversation` -- transkript)
    // ==================================================================
    function briefReady() { return !((store.asset.missing_questions || []).length); }
    function renderBrief(main) {
      var a = store.asset;
      var wrap = h('div', { class: 'cs-brief' });
      wrap.appendChild(h('div', { class: 'cs-brief-head' }, [
        h('span', { class: 'ap-sparkle', text: '✨' }),
        h('div', {}, [h('h2', { text: 'AI bilan suhbat' }), h('p', { class: 'text-faint', text: 'AI kompaniya profilingizni allaqachon biladi -- savolga qisqa javob yozing, u aynan shu reklamaga mos keyingi savolni o\'zi tanlaydi (shablon savol emas).' })])
      ]));
      if (store.busy === 'generating') { main.appendChild(wrap); wrap.appendChild(spinnerCard('AI rasm yaratmoqda… bir necha soniya (odatda 10-30 s).')); return; }

      var card = h('div', { class: 'cs-chat-card' });
      var log = h('div', { class: 'cs-chat-log', id: 'cs-brief-chat-log' });
      var conv = a.brief_conversation || [];
      if (!conv.length && store.busy !== 'brief') { log.appendChild(h('div', { class: 'cs-chat-msg cs-chat-msg-agent', text: 'Savol tayyorlanmoqda…' })); }
      conv.forEach(function (t) {
        log.appendChild(h('div', { class: 'cs-chat-msg ' + (t.role === 'user' ? 'cs-chat-msg-user' : 'cs-chat-msg-agent'), text: t.text }));
      });
      if (store.busy === 'brief') { log.appendChild(h('div', { class: 'cs-chat-typing', text: 'AI yozmoqda…' })); }
      card.appendChild(log);

      if (briefReady()) {
        var can = (a.quota || {}).can_generate;
        var act = h('div', { class: 'cs-q-actions', style: 'padding:12px 16px' });
        var gen = h('button', { type: 'button', class: 'btn ap-btn-sm ap-btn-ai', text: '✨ Yaratish', disabled: !can, title: can ? '1 ta AI generatsiya sarflanadi' : (a.quota || {}).label });
        gen.addEventListener('click', generate);
        act.appendChild(gen);
        act.appendChild(h('span', { class: 'text-faint', style: 'font-size:12px', text: can ? '1 ta AI generatsiya sarflanadi (' + (a.quota || {}).label + ')' : 'AI generatsiya yopiq -- tarifni oshiring' }));
        card.appendChild(act);
      } else {
        var lastTurn = conv.length ? conv[conv.length - 1] : null;
        var form = h('form', { class: 'cs-chat-form', id: 'cs-brief-chat-form' });
        var input = h('textarea', { rows: 2, placeholder: (lastTurn && lastTurn.placeholder) || 'Javobingizni yozing…', maxlength: 1000, id: 'cs-brief-chat-input', disabled: store.busy === 'brief' });
        var send = h('button', { type: 'submit', class: 'btn ap-btn-sm', text: 'Yuborish', disabled: store.busy === 'brief' });
        form.appendChild(input); form.appendChild(send);
        var submit = function () {
          var v = input.value.trim();
          if (!v || store.busy) { return; }
          input.value = '';
          store.asset.brief_conversation = (store.asset.brief_conversation || []).concat([{ role: 'user', text: v }]);
          store.busy = 'brief'; renderMain();
          api('/brief', { method: 'POST', body: { message: v } }).then(function () { store.busy = null; setMsg(null); renderMain(); })
            .catch(function (e) { store.busy = null; setMsg(e.message, 'err'); renderMain(); });
        };
        form.addEventListener('submit', function (e) { e.preventDefault(); submit(); });
        input.addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(); } });
        card.appendChild(form);
        setTimeout(function () { input.focus(); }, 0);
      }
      wrap.appendChild(card);
      main.appendChild(wrap);
      var logEl = document.getElementById('cs-brief-chat-log');
      if (logEl) { logEl.scrollTop = logEl.scrollHeight; }
    }

    function spinnerCard(text) {
      return h('div', { class: 'cs-spinner-card' }, [h('span', { class: 'cs-spinner' }), h('span', { text: text })]);
    }

    function generate() {
      if (store.busy) { return; }
      store.busy = 'generating'; setMsg(null); renderMain(); renderSide();
      api('/generate', { method: 'POST', body: {} }).then(function () {
        store.busy = null; store.selected = -1;
        setMsg('Rasm tayyor! Matn va logotipni sudrab joylashtiring, keyin "Saqlash"ni bosing.', 'ok');
        render();
      }).catch(function (e) {
        store.busy = null;
        setMsg(e.message + (e.payload && e.payload.quota_exceeded ? '' : ''), e.payload && e.payload.quota_exceeded ? 'warn' : 'err');
        render();
      });
    }
    function regenerate() {
      if (store.busy) { return; }
      var q = store.asset.quota || {};
      if (!q.can_generate) { setMsg(q.label + ' -- tarifni oshiring.', 'warn'); return; }
      if (!confirm('AI yangi fon rasmi yaratadi -- 1 ta generatsiya sarflanadi (' + q.label + '). Tahrirlagan matn/joylashuvlaringiz saqlanib qoladi. Davom etaymi?')) { return; }
      if (store.dirty) {
        // Avval joriy qatlamlarni saqlaymiz -- yangi fon ustiga aynan shu joylashuv chiziladi
        store.busy = 'saving'; renderSide();
        api('/layers', { method: 'POST', body: { layers: store.asset.layers } }).then(function () { store.busy = null; doRegenerate(); })
          .catch(function (e) { store.busy = null; setMsg('Qatlamlar saqlanmadi: ' + e.message, 'err'); renderSide(); });
        return;
      }
      doRegenerate();
    }
    function doRegenerate() {
      store.busy = 'generating'; setMsg(null); renderMain(); renderSide();
      api('/regenerate', { method: 'POST', body: {} }).then(function () { store.busy = null; setMsg('Yangi fon tayyor.', 'ok'); render(); })
        .catch(function (e) { store.busy = null; setMsg(e.message, e.payload && e.payload.quota_exceeded ? 'warn' : 'err'); render(); });
    }

    // ==================================================================
    // ASOSIY PANEL
    // ==================================================================
    function renderMain() {
      var a = store.asset;
      var main = document.getElementById('cs-main');
      main.innerHTML = '';
      var layout = document.getElementById('cs-layout');
      var editorMode = a.status === 'ready' && store.busy !== 'generating';
      layout.classList.toggle('cs-layout-single', !editorMode);
      document.getElementById('cs-side').hidden = !editorMode;

      if (store.busy === 'generating') {
        main.appendChild(h('div', { class: 'cs-brief' }, [spinnerCard('AI rasm yaratmoqda… bir necha soniya (odatda 10-30 s). Sahifani yopmang.')]));
        return;
      }
      if (a.status === 'collecting_brief' || (a.status === 'failed' && (a.missing_questions || []).length)) { renderBrief(main); return; }
      if (a.status === 'generating') {
        main.appendChild(h('div', { class: 'cs-brief' }, [
          spinnerCard('Bu rasm uchun generatsiya ketyapti (boshqa oynada boshlangan bo\'lishi mumkin). Birozdan keyin sahifani yangilang.'),
          h('div', { class: 'cs-q-actions' }, [miniBtn('Sahifani yangilash', function () { window.location.reload(); }, { icon: 'refresh' })])
        ]));
        return;
      }
      if (a.status === 'failed') {
        var card = h('div', { class: 'cs-brief' }, [
          h('div', { class: 'card ap-err-card' }, [h('h2', { text: 'Rasm yaratilmadi' }), h('p', { text: a.error_message || 'Noma\'lum xato.' })]),
          h('div', { class: 'cs-q-actions' }, [
            bigBtn('Qayta urinish', function () { if (a.has_base_image) { doRegenerate(); } else { generate(); } }, { icon: 'refresh', disabled: !(a.quota || {}).can_generate, cls: 'ap-btn-ai' }),
            miniBtn('Javoblarni o\'zgartirish', function () { store.asset.status = 'collecting_brief'; renderMain(); }),
            h('a', { href: a.urls.templates || '#', class: 'ap-mini-btn', text: 'Shablondan boshlash', style: 'text-decoration:none' })
          ])
        ]);
        main.appendChild(card);
        return;
      }
      renderEditor(main);
    }

    // ==================================================================
    // CANVAS MUHARRIR
    // ==================================================================
    var canvasEl = null, ctx = null, scale = 1, dpr = 1;

    function renderEditor(main) {
      var a = store.asset;
      var wrap = h('div', { class: 'cs-editor' });
      wrap.appendChild(h('div', { class: 'cs-editor-hint' }, [
        h('span', { text: 'Qatlamni bosib tanlang, sudrab joylashtiring; pastki o\'ng burchakdagi tutqich bilan o\'lchamini o\'zgartiring. Bu oldindan ko\'rinish -- "Saqlash" bosilganda server yakuniy rasmni aniq shrift bilan qayta chizadi.' }),
        store.dirty ? h('span', { class: 'badge badge-color-warn', text: 'Saqlanmagan o\'zgarishlar' }) : h('span', { class: 'badge badge-color-good', text: 'Saqlangan' })
      ]));
      var box = h('div', { class: 'cs-canvas-wrap' });
      canvasEl = h('canvas', { class: 'cs-canvas', tabindex: 0, 'aria-label': 'Reklama rasmi muharriri' });
      box.appendChild(canvasEl);
      wrap.appendChild(box);
      main.appendChild(wrap);
      ctx = canvasEl.getContext('2d');
      bindCanvasEvents();
      ensureImages();
      sizeCanvas();
      drawCanvas();
    }

    function sizeCanvas() {
      if (!canvasEl) { return; }
      var a = store.asset;
      var box = canvasEl.parentNode;
      var maxW = Math.max(200, box.clientWidth - 2);
      var maxH = Math.max(240, Math.min(window.innerHeight - 260, 760));
      var ratio = a.height / a.width;
      var w = Math.min(maxW, maxH / ratio);
      var hgt = w * ratio;
      dpr = window.devicePixelRatio || 1;
      canvasEl.style.width = Math.round(w) + 'px';
      canvasEl.style.height = Math.round(hgt) + 'px';
      canvasEl.width = Math.round(w * dpr);
      canvasEl.height = Math.round(hgt * dpr);
      scale = w / a.width;  // rasm pikseli -> ekran pikseli
    }
    window.addEventListener('resize', function () { if (canvasEl && store.asset.status === 'ready') { sizeCanvas(); drawCanvas(); } });

    // Pillow `_fit_text`/`_wrap_lines` bilan bir xil mantiq (oldindan ko'rinish uchun)
    function fontStr(kind, px) { return (kind === 'regular' ? '' : 'bold ') + px + 'px ' + FONT_STACK; }
    function wrapLines(text, maxW) {
      var lines = [];
      String(text || '').split('\n').forEach(function (para) {
        var words = para.split(/\s+/).filter(Boolean);
        if (!words.length) { lines.push(''); return; }
        var cur = words[0];
        for (var i = 1; i < words.length; i++) {
          if (ctx.measureText(cur + ' ' + words[i]).width <= maxW) { cur += ' ' + words[i]; } else { lines.push(cur); cur = words[i]; }
        }
        lines.push(cur);
      });
      return lines;
    }
    function fitText(text, kind, size, boxW, boxH) {
      var minSize = Math.max(8, size * 0.4);
      for (;;) {
        ctx.font = fontStr(kind, size);
        var lines = wrapLines(text, boxW);
        var lineH = size * 1.2;
        var tooWide = lines.some(function (ln) { return ctx.measureText(ln).width > boxW; });
        if ((!tooWide && lineH * lines.length <= boxH) || size <= minSize) { return { lines: lines, lineH: lineH, size: size }; }
        size = Math.max(minSize, size * 0.9);
      }
    }
    function hexToRgba(hex, alpha) {
      var m = /^#?([0-9a-f]{6})$/i.exec(String(hex || '').trim());
      if (!m) { return 'rgba(17,17,17,' + (alpha == null ? 1 : alpha) + ')'; }
      var n = parseInt(m[1], 16);
      return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + (alpha == null ? 1 : alpha) + ')';
    }
    function layerRect(l) {
      var a = store.asset;
      return { x: l.x * a.width * scale, y: l.y * a.height * scale, w: Math.max(1, l.w * a.width * scale), h: Math.max(1, l.h * a.height * scale) };
    }
    function roundRect(x, y, w, hh, r) {
      ctx.beginPath();
      ctx.moveTo(x + r, y); ctx.lineTo(x + w - r, y); ctx.quadraticCurveTo(x + w, y, x + w, y + r);
      ctx.lineTo(x + w, y + hh - r); ctx.quadraticCurveTo(x + w, y + hh, x + w - r, y + hh);
      ctx.lineTo(x + r, y + hh); ctx.quadraticCurveTo(x, y + hh, x, y + hh - r);
      ctx.lineTo(x, y + r); ctx.quadraticCurveTo(x, y, x + r, y); ctx.closePath();
    }

    function drawCanvas() {
      if (!ctx || !canvasEl || store.asset.status !== 'ready') { return; }
      var a = store.asset;
      var W = a.width * scale, H = a.height * scale;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, W, H);
      // Fon (cover, markazdan)
      if (store.baseImg) {
        var img = store.baseImg;
        var s = Math.max(W / img.width, H / img.height);
        var nw = img.width * s, nh = img.height * s;
        ctx.drawImage(img, (W - nw) / 2, (H - nh) / 2, nw, nh);
      } else {
        ctx.fillStyle = '#E2E8F0'; ctx.fillRect(0, 0, W, H);
        ctx.fillStyle = '#64748B'; ctx.font = '13px ' + FONT_STACK; ctx.textAlign = 'center'; ctx.fillText('Fon rasmi yuklanmoqda…', W / 2, H / 2); ctx.textAlign = 'left';
      }
      (a.layers || []).forEach(function (l) {
        if (l.hidden) { return; }
        var r = layerRect(l);
        try {
          if (l.type === 'panel') {
            var op = l.opacity == null ? 1 : Number(l.opacity);
            if (l.gradient) {
              var g = ctx.createLinearGradient(0, r.y, 0, r.y + r.h);
              g.addColorStop(0, hexToRgba(l.bg_color, 0)); g.addColorStop(1, hexToRgba(l.bg_color, op));
              ctx.fillStyle = g;
            } else { ctx.fillStyle = hexToRgba(l.bg_color, op); }
            ctx.fillRect(r.x, r.y, r.w, r.h);
          } else if (l.type === 'badge') {
            var text = String(l.text || '').trim();
            if (text.indexOf('{{') >= 0) { return; }
            var opb = l.opacity == null ? 1 : Number(l.opacity);
            roundRect(r.x, r.y, r.w, r.h, Math.max(2, Math.min(r.w, r.h) * 0.22));
            ctx.fillStyle = hexToRgba(l.bg_color, opb); ctx.fill();
            if (text) {
              var padX = r.w * 0.08, padY = r.h * 0.12;
              var f = fitText(text, l.font, Math.max(8, (Number(l.size_ratio) || 0.028) * H), r.w - 2 * padX, r.h - 2 * padY);
              ctx.font = fontStr(l.font, f.size); ctx.fillStyle = hexToRgba(l.color || '#FFFFFF', 1); ctx.textBaseline = 'top';
              var cy = r.y + (r.h - f.lineH * f.lines.length) / 2;
              f.lines.forEach(function (ln) {
                var lw = ctx.measureText(ln).width;
                var lx = l.align === 'left' ? r.x + padX : (l.align === 'right' ? r.x + r.w - padX - lw : r.x + (r.w - lw) / 2);
                ctx.fillText(ln, lx, cy); cy += f.lineH;
              });
            }
          } else if (l.type === 'text') {
            var t = String(l.text || '').trim();
            if (!t || t.indexOf('{{') >= 0) { return; }
            var ft = fitText(t, l.font, Math.max(8, (Number(l.size_ratio) || 0.04) * H), r.w, r.h);
            ctx.font = fontStr(l.font, ft.size); ctx.fillStyle = hexToRgba(l.color || '#111111', 1); ctx.textBaseline = 'top';
            var ty = r.y;
            ft.lines.forEach(function (ln) {
              var lw = ctx.measureText(ln).width;
              var lx = l.align === 'center' ? r.x + (r.w - lw) / 2 : (l.align === 'right' ? r.x + r.w - lw : r.x);
              ctx.fillText(ln, lx, ty); ty += ft.lineH;
            });
          } else if (l.type === 'logo') {
            if (store.logoImg) {
              var li = store.logoImg;
              var sc = Math.min(r.w / li.width, r.h / li.height);
              var lw2 = li.width * sc, lh2 = li.height * sc;
              var lx2 = l.align === 'left' ? r.x : (l.align === 'center' ? r.x + (r.w - lw2) / 2 : r.x + r.w - lw2);
              ctx.drawImage(li, lx2, r.y + (r.h - lh2) / 2, lw2, lh2);
            } else {
              ctx.save(); ctx.setLineDash([4, 3]); ctx.strokeStyle = 'rgba(255,255,255,0.8)'; ctx.lineWidth = 1; ctx.strokeRect(r.x, r.y, r.w, r.h);
              ctx.fillStyle = 'rgba(255,255,255,0.85)'; ctx.font = 'bold ' + Math.max(9, Math.min(12, r.h * 0.3)) + 'px ' + FONT_STACK; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
              ctx.fillText('LOGO', r.x + r.w / 2, r.y + r.h / 2); ctx.restore();
            }
          }
        } catch (e) { /* bitta buzuq qatlam butun chizmani to'xtatmasin */ }
      });
      // Qatlam chegaralari (yarim shaffof) + tanlangan
      (a.layers || []).forEach(function (l, i) {
        if (l.hidden || l.type === 'panel') { return; }
        var r = layerRect(l);
        ctx.save();
        ctx.setLineDash([]);
        ctx.strokeStyle = i === store.selected ? 'rgba(11,99,245,0.95)' : 'rgba(11,99,245,0.35)';
        ctx.lineWidth = i === store.selected ? 2 : 1;
        ctx.strokeRect(r.x + 0.5, r.y + 0.5, r.w - 1, r.h - 1);
        if (i === store.selected) {
          ctx.fillStyle = '#0B63F5';
          ctx.fillRect(r.x + r.w - 6, r.y + r.h - 6, 12, 12);
          ctx.fillStyle = 'rgba(11,99,245,0.9)'; ctx.font = 'bold 10px ' + FONT_STACK; ctx.textBaseline = 'bottom'; ctx.textAlign = 'left';
          ctx.fillText(LAYER_LABELS[l.type] || l.type, r.x + 2, r.y - 2);
        }
        ctx.restore();
      });
      if (store.selected >= 0 && (a.layers[store.selected] || {}).type === 'panel' && !a.layers[store.selected].hidden) {
        var pr = layerRect(a.layers[store.selected]);
        ctx.save(); ctx.strokeStyle = 'rgba(11,99,245,0.95)'; ctx.lineWidth = 2; ctx.setLineDash([6, 4]); ctx.strokeRect(pr.x + 1, pr.y + 1, pr.w - 2, pr.h - 2);
        ctx.fillStyle = '#0B63F5'; ctx.setLineDash([]); ctx.fillRect(pr.x + pr.w - 6, pr.y + pr.h - 6, 12, 12); ctx.restore();
      }
    }

    // ---- Sudrash / o'lcham (pointer events: sichqoncha + sensor)
    function canvasPoint(e) {
      var rect = canvasEl.getBoundingClientRect();
      return { x: e.clientX - rect.left, y: e.clientY - rect.top };
    }
    function hitTest(p) {
      var a = store.asset;
      var layers = a.layers || [];
      // Avval tanlangan qatlamning o'lcham tutqichi
      if (store.selected >= 0 && layers[store.selected] && !layers[store.selected].hidden) {
        var r0 = layerRect(layers[store.selected]);
        if (p.x >= r0.x + r0.w - 9 && p.x <= r0.x + r0.w + 9 && p.y >= r0.y + r0.h - 9 && p.y <= r0.y + r0.h + 9) { return { index: store.selected, mode: 'resize' }; }
      }
      // Ustki qatlamdan pastga; panel'lar eng oxirida (dekorativ)
      var order = [];
      for (var i = layers.length - 1; i >= 0; i--) { if (!layers[i].hidden && layers[i].type !== 'panel') { order.push(i); } }
      for (var j = layers.length - 1; j >= 0; j--) { if (!layers[j].hidden && layers[j].type === 'panel') { order.push(j); } }
      for (var k = 0; k < order.length; k++) {
        var r = layerRect(layers[order[k]]);
        if (p.x >= r.x && p.x <= r.x + r.w && p.y >= r.y && p.y <= r.y + r.h) { return { index: order[k], mode: 'move' }; }
      }
      return null;
    }
    function bindCanvasEvents() {
      canvasEl.addEventListener('pointerdown', function (e) {
        if (store.busy) { return; }
        var p = canvasPoint(e);
        var hit = hitTest(p);
        if (!hit) { store.selected = -1; drawCanvas(); renderSide(); return; }
        var l = store.asset.layers[hit.index];
        store.selected = hit.index;
        store.drag = { mode: hit.mode, index: hit.index, startX: p.x, startY: p.y, ox: l.x, oy: l.y, ow: l.w, oh: l.h, moved: false };
        try { canvasEl.setPointerCapture(e.pointerId); } catch (err) { /* eski brauzer */ }
        drawCanvas(); renderSide();
        e.preventDefault();
      });
      canvasEl.addEventListener('pointermove', function (e) {
        var p = canvasPoint(e);
        if (!store.drag) {
          var hit = hitTest(p);
          canvasEl.style.cursor = hit ? (hit.mode === 'resize' ? 'nwse-resize' : 'move') : 'default';
          return;
        }
        var a = store.asset;
        var l = a.layers[store.drag.index];
        var dx = (p.x - store.drag.startX) / (a.width * scale);
        var dy = (p.y - store.drag.startY) / (a.height * scale);
        if (store.drag.mode === 'move') {
          l.x = round3(clamp(store.drag.ox + dx, 0, Math.max(0, 1 - l.w)));
          l.y = round3(clamp(store.drag.oy + dy, 0, Math.max(0, 1 - l.h)));
        } else {
          l.w = round3(clamp(store.drag.ow + dx, 0.03, 1 - l.x));
          l.h = round3(clamp(store.drag.oh + dy, 0.02, 1 - l.y));
        }
        store.drag.moved = true; store.dirty = true;
        drawCanvas();
        e.preventDefault();
      });
      var end = function (e) {
        if (!store.drag) { return; }
        var moved = store.drag.moved;
        store.drag = null;
        try { canvasEl.releasePointerCapture(e.pointerId); } catch (err) { /* */ }
        if (moved) { renderMain(); }
        renderSide();
      };
      canvasEl.addEventListener('pointerup', end);
      canvasEl.addEventListener('pointercancel', end);
      canvasEl.addEventListener('keydown', function (e) {
        if (store.selected < 0) { return; }
        var l = store.asset.layers[store.selected];
        var step = e.shiftKey ? 0.02 : 0.005;
        var moved = true;
        if (e.key === 'ArrowLeft') { l.x = round3(clamp(l.x - step, 0, 1 - l.w)); }
        else if (e.key === 'ArrowRight') { l.x = round3(clamp(l.x + step, 0, 1 - l.w)); }
        else if (e.key === 'ArrowUp') { l.y = round3(clamp(l.y - step, 0, 1 - l.h)); }
        else if (e.key === 'ArrowDown') { l.y = round3(clamp(l.y + step, 0, 1 - l.h)); }
        else { moved = false; }
        if (moved) { e.preventDefault(); store.dirty = true; drawCanvas(); renderSide(); }
      });
    }

    // ==================================================================
    // O'NG PANEL: qatlamlar + xususiyatlar + amallar
    // ==================================================================
    function markDirty() { store.dirty = true; drawCanvas(); var b = document.querySelector('.cs-editor-hint .badge'); if (b) { b.className = 'badge badge-color-warn'; b.textContent = 'Saqlanmagan o\'zgarishlar'; } }
    function propField(label, control, hint) {
      var el = h('div', { class: 'ap-field' }, [h('div', { class: 'ap-field-head' }, [h('span', { class: 'ap-field-label', text: label })]), control]);
      if (hint) { el.appendChild(h('div', { class: 'ap-field-hint', text: hint })); }
      return el;
    }
    function numInput(l, key, min, max, step, onchange) {
      var inp = h('input', { type: 'number', min: min, max: max, step: step, value: Math.round((l[key] || 0) * 100) });
      inp.addEventListener('change', function () { var v = clamp(Number(inp.value) / 100, min / 100, max / 100); if (isNaN(v)) { return; } l[key] = round3(v); onchange && onchange(); markDirty(); });
      return inp;
    }
    // "#RGB" / "RRGGBB" / " #rrggbb " -> "#RRGGBB" yoki null (noto'g'ri).
    function normalizeHex(v) {
      var s = String(v || '').trim().replace(/^#/, '');
      if (/^[0-9a-f]{3}$/i.test(s)) { s = s[0] + s[0] + s[1] + s[1] + s[2] + s[2]; }
      return /^[0-9a-f]{6}$/i.test(s) ? ('#' + s.toUpperCase()) : null;
    }
    // Rang tanlagich (swatch) + qo'lda yoziladigan HEX maydoni, ikki
    // tomonlama sinxron (brend kit formasidagi naqsh). 2026-09,
    // foydalanuvchi so'rovi: "rang kodini o'zim yozib/qo'yib qo'yay".
    function colorField(label, current, onchange) {
      var cur = normalizeHex(current) || '#111111';
      var swatch = h('input', { type: 'color', value: cur, title: label });
      var hex = h('input', { type: 'text', class: 'cs-hex-input', value: cur, placeholder: '#RRGGBB', maxlength: 7, spellcheck: 'false', 'aria-label': label + ' (HEX)' });
      swatch.addEventListener('input', function () { var v = normalizeHex(swatch.value); if (!v) { return; } hex.value = v; hex.classList.remove('invalid'); onchange(v); });
      hex.addEventListener('input', function () {
        var v = normalizeHex(hex.value);
        hex.classList.toggle('invalid', !v && hex.value.trim() !== '');
        if (v) { swatch.value = v; onchange(v); }
      });
      var finalize = function () { var v = normalizeHex(hex.value); if (v) { hex.value = v; swatch.value = v; onchange(v); } else { hex.value = swatch.value.toUpperCase(); } hex.classList.remove('invalid'); };
      hex.addEventListener('change', finalize);
      hex.addEventListener('blur', finalize);
      hex.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); finalize(); } });
      return propField(label, h('div', { class: 'cs-color-row' }, [swatch, hex]));
    }

    function renderSide() {
      var a = store.asset;
      var side = document.getElementById('cs-side');
      side.innerHTML = '';
      if (a.status !== 'ready' || store.busy === 'generating') { return; }
      var layers = a.layers || [];
      var busy = !!store.busy;

      // ---- Amallar
      var act = h('div', { class: 'cs-side-actions' });
      var save = h('button', { type: 'button', class: 'btn ap-btn-sm', disabled: busy, html: store.busy === 'saving' ? '<span class="btn-spinner"></span> Saqlanmoqda…' : svgIcon('check') + ' Saqlash' });
      if (save.querySelector('svg')) { save.querySelector('svg').style.cssText = 'width:13px;height:13px;vertical-align:-2px;margin-right:4px'; }
      save.addEventListener('click', saveLayers);
      act.appendChild(save);
      act.appendChild(miniBtn('Bekor qilish', function () { store.asset.layers = JSON.parse(JSON.stringify(store.serverLayers)); store.dirty = false; store.selected = -1; renderMain(); renderSide(); }, { disabled: busy || !store.dirty, title: 'Oxirgi saqlangan holatga qaytarish' }));
      act.appendChild(miniBtn('Qayta generatsiya', regenerate, { icon: 'refresh', disabled: busy || !(a.quota || {}).can_generate, title: (a.quota || {}).can_generate ? 'Yangi AI fon (1 generatsiya sarflanadi), matnlar saqlanadi' : (a.quota || {}).label }));
      side.appendChild(h('div', { class: 'cs-side-section' }, [h('h3', { class: 'ap-section-title', text: 'Amallar' }), act]));

      var exp = h('div', { class: 'cs-side-actions' });
      exp.appendChild(h('a', { href: a.urls.export_png || '#', class: 'ap-mini-btn', html: svgIcon('download') + ' PNG yuklab olish', style: 'text-decoration:none' }));
      exp.appendChild(h('a', { href: a.urls.export_pdf || '#', class: 'ap-mini-btn', html: svgIcon('download') + ' PDF yuklab olish', style: 'text-decoration:none' }));
      exp.querySelectorAll('svg').forEach(function (s) { s.style.cssText = 'width:12px;height:12px;vertical-align:-2px'; });
      if (a.from_autopilot && a.urls.autopilot) {
        var use = h('button', { type: 'button', class: 'btn ap-btn-sm ap-btn-publish', disabled: busy, html: store.busy === 'using' ? '<span class="btn-spinner"></span> Yuborilmoqda…' : svgIcon('rocket') + ' Avtopilotda ishlatish' });
        if (use.querySelector('svg')) { use.querySelector('svg').style.cssText = 'width:13px;height:13px;vertical-align:-2px;margin-right:4px'; }
        use.addEventListener('click', useInAutopilot);
        exp.appendChild(use);
      }
      side.appendChild(h('div', { class: 'cs-side-section' }, [h('h3', { class: 'ap-section-title', text: 'Eksport' }), exp, store.dirty ? h('div', { class: 'ap-field-hint', text: 'Eksport oxirgi SAQLANGAN holatni beradi -- avval "Saqlash"ni bosing.' }) : null]));

      // ---- AI bilan tezkor o'zgartirish (2026-09, foydalanuvchi so'rovi:
      // OpenAI'ni qayta chaqirmasdan, oddiy tilda "logotipni kattaroq
      // qil" kabi buyruq bilan tuzatish -- kvota sarflanmaydi).
      side.appendChild(renderChangeChatSection());

      // ---- Targetga ochish (2026-09): tayyor kreativdan Avtopilot qoralamasi
      if (!a.from_autopilot && a.urls.target_create) { side.appendChild(renderTargetSection()); }

      // ---- Qatlamlar ro'yxati
      var list = h('div', { class: 'cs-layer-list' });
      layers.forEach(function (l, i) {
        var snippet = (l.type === 'logo') ? (a.brand.has_logo ? 'Brend logotipi' : 'Logotip yuklanmagan') : (l.type === 'panel' ? 'Dekorativ panel' : (String(l.text || '').replace(/\{\{.*?\}\}/g, '(bo\'sh)') || '(bo\'sh matn)'));
        var row = h('div', { class: 'cs-layer-row' + (i === store.selected ? ' selected' : '') + (l.hidden ? ' hidden-layer' : '') });
        var eye = h('button', { type: 'button', class: 'cs-layer-eye', html: svgIcon(l.hidden ? 'eyeOff' : 'eye'), title: l.hidden ? 'Ko\'rsatish' : 'Yashirish' });
        eye.addEventListener('click', function (e) { e.stopPropagation(); l.hidden = !l.hidden; if (!l.hidden) { delete l.hidden; } markDirty(); renderSide(); });
        row.appendChild(eye);
        row.appendChild(h('span', { class: 'cs-layer-type', text: LAYER_LABELS[l.type] || l.type }));
        row.appendChild(h('span', { class: 'cs-layer-text', text: snippet, title: snippet }));
        row.addEventListener('click', function () { store.selected = i; drawCanvas(); renderSide(); });
        list.appendChild(row);
      });
      var addRow = h('div', { class: 'ap-inline-actions', style: 'margin-top:8px' }, [
        miniBtn('Matn qo\'shish', function () { addLayer('text'); }, { icon: 'plus' }),
        miniBtn('Tugma/belgi qo\'shish', function () { addLayer('badge'); }, { icon: 'plus' }),
        !layers.some(function (l) { return l.type === 'logo'; }) ? miniBtn('Logotip qo\'shish', function () { addLayer('logo'); }, { icon: 'plus' }) : null
      ]);
      side.appendChild(h('div', { class: 'cs-side-section' }, [h('h3', { class: 'ap-section-title', text: 'Qatlamlar' }), list, addRow]));

      // ---- Tanlangan qatlam xususiyatlari
      var sec = h('div', { class: 'cs-side-section' }, [h('h3', { class: 'ap-section-title', text: 'Tanlangan qatlam' })]);
      var l = layers[store.selected];
      if (!l) {
        sec.appendChild(h('div', { class: 'ap-empty', style: 'padding:14px', text: 'Canvas\'da yoki ro\'yxatdan qatlamni tanlang.' }));
      } else {
        if (l.type === 'text' || l.type === 'badge') {
          var ta = h('textarea', { rows: 3, maxlength: 300, placeholder: 'Matn' });
          ta.value = String(l.text || '').indexOf('{{') >= 0 ? '' : (l.text || '');
          ta.addEventListener('input', function () { l.text = ta.value; if (l.hidden && ta.value.trim()) { delete l.hidden; } markDirty(); });
          sec.appendChild(propField('Matn', ta, String(l.text || '').indexOf('{{') >= 0 ? 'Bu qatlam uchun ma\'lumot topilmadi -- matn kiritsangiz ko\'rinadi.' : null));
          var g = h('div', { class: 'ap-grid-2' });
          g.appendChild(colorField('Matn rangi', l.color, function (v) { l.color = v; markDirty(); }));
          var font = h('select', {}, [h('option', { value: 'bold', text: 'Qalin', selected: l.font !== 'regular' }), h('option', { value: 'regular', text: 'Oddiy', selected: l.font === 'regular' })]);
          font.addEventListener('change', function () { l.font = font.value; markDirty(); });
          g.appendChild(propField('Shrift', font));
          sec.appendChild(g);
          var g2 = h('div', { class: 'ap-grid-2' });
          var size = h('input', { type: 'range', min: 1, max: 15, step: 0.2, value: Math.round((Number(l.size_ratio) || 0.04) * 1000) / 10 });
          size.addEventListener('input', function () { l.size_ratio = round3(Number(size.value) / 100); markDirty(); });
          g2.appendChild(propField('Shrift hajmi', size, 'Rasm balandligiga nisbatan; sig\'masa avtomatik kichrayadi.'));
          var align = h('select', {}, ['left', 'center', 'right'].map(function (v) { return h('option', { value: v, text: { left: 'Chapga', center: 'Markazga', right: 'O\'ngga' }[v], selected: (l.align || (l.type === 'badge' ? 'center' : 'left')) === v }); }));
          align.addEventListener('change', function () { l.align = align.value; markDirty(); });
          g2.appendChild(propField('Tekislash', align));
          sec.appendChild(g2);
        }
        if (l.type === 'badge' || l.type === 'panel') {
          var g3 = h('div', { class: 'ap-grid-2' });
          g3.appendChild(colorField('Fon rangi', l.bg_color, function (v) { l.bg_color = v; markDirty(); }));
          var op = h('input', { type: 'range', min: 0, max: 100, step: 5, value: Math.round((l.opacity == null ? 1 : Number(l.opacity)) * 100) });
          op.addEventListener('input', function () { l.opacity = round3(Number(op.value) / 100); markDirty(); });
          g3.appendChild(propField('Shaffoflik', op));
          sec.appendChild(g3);
          if (l.type === 'panel') {
            var grad = h('input', { type: 'checkbox', checked: !!l.gradient });
            grad.addEventListener('change', function () { l.gradient = grad.checked; markDirty(); });
            sec.appendChild(h('label', { class: 'ap-check' }, [grad, 'Gradient (yuqorida shaffof, pastda to\'q)']));
          }
        }
        if (l.type === 'logo') {
          var la = h('select', {}, ['left', 'center', 'right'].map(function (v) { return h('option', { value: v, text: { left: 'Chapga', center: 'Markazga', right: 'O\'ngga' }[v], selected: (l.align || 'right') === v }); }));
          la.addEventListener('change', function () { l.align = la.value; markDirty(); });
          sec.appendChild(propField('Logotip tekislash', la, a.brand.has_logo ? 'Logotip "contain" rejimida to\'rtburchakka sig\'diriladi.' : 'Logotip yuklanmagan -- qatlam bo\'sh qoladi.'));
          if (!a.brand.has_logo) { sec.appendChild(h('a', { href: (a.urls.brand_settings || '#') + '?next=' + encodeURIComponent(window.location.pathname + window.location.search), class: 'ap-mini-btn primary', text: 'Logotip yuklash (Brend kit)', style: 'text-decoration:none;display:inline-block;margin-bottom:10px' })); }
        }
        var pos = h('div', { class: 'cs-pos-grid' });
        pos.appendChild(propField('X, %', numInput(l, 'x', 0, 100, 1)));
        pos.appendChild(propField('Y, %', numInput(l, 'y', 0, 100, 1)));
        pos.appendChild(propField('Kenglik, %', numInput(l, 'w', 3, 100, 1)));
        pos.appendChild(propField('Balandlik, %', numInput(l, 'h', 2, 100, 1)));
        sec.appendChild(pos);
        var hid = h('input', { type: 'checkbox', checked: !!l.hidden });
        hid.addEventListener('change', function () { if (hid.checked) { l.hidden = true; } else { delete l.hidden; } markDirty(); renderSide(); });
        sec.appendChild(h('label', { class: 'ap-check' }, [hid, 'Qatlamni yashirish']));
        sec.appendChild(h('div', { class: 'ap-inline-actions', style: 'margin-top:10px' }, [
          miniBtn('Qatlamni o\'chirish', function () { layers.splice(store.selected, 1); store.selected = -1; markDirty(); renderSide(); }, { cls: 'danger', icon: 'trash' })
        ]));
      }
      side.appendChild(sec);

      // ---- Server natijasi
      if (a.image_url) {
        var res = h('div', { class: 'cs-side-section' }, [h('h3', { class: 'ap-section-title', text: 'Yakuniy rasm (server)' })]);
        res.appendChild(h('a', { href: a.image_url, target: '_blank', rel: 'noopener', class: 'cs-result-thumb cs-aspect-' + a.aspect.replace(':', '-') }, [h('img', { src: a.image_url, alt: 'Yakuniy rasm' })]));
        res.appendChild(h('div', { class: 'ap-field-hint', text: a.width + '×' + a.height + ' px · yangilangan ' + fmtTime(a.updated_at) }));
        side.appendChild(res);
      }
    }

    function addLayer(type) {
      var a = store.asset;
      var l;
      if (type === 'text') { l = { id: 'text_' + Date.now(), type: 'text', x: 0.08, y: 0.4, w: 0.84, h: 0.12, align: 'left', font: 'bold', size_ratio: 0.05, color: '#FFFFFF', text: 'Yangi matn' }; }
      else if (type === 'badge') { l = { id: 'badge_' + Date.now(), type: 'badge', x: 0.08, y: 0.5, w: 0.4, h: 0.07, align: 'center', font: 'bold', size_ratio: 0.026, color: '#FFFFFF', bg_color: '#111111', opacity: 0.95, text: 'Buyurtma bering' }; }
      else { l = { id: 'logo', type: 'logo', x: 0.8, y: 0.06, w: 0.14, h: 0.09, align: 'right' }; }
      a.layers = a.layers || [];
      a.layers.push(l);
      store.selected = a.layers.length - 1;
      markDirty(); renderSide();
    }

    function saveLayers() {
      if (store.busy) { return Promise.resolve(); }
      store.busy = 'saving'; setMsg(null); renderSide();
      return api('/layers', { method: 'POST', body: { layers: store.asset.layers } }).then(function () {
        store.busy = null; setMsg('Saqlandi -- yakuniy rasm qayta chizildi.', 'ok'); render();
      }).catch(function (e) { store.busy = null; setMsg('Saqlanmadi: ' + e.message, 'err'); renderSide(); throw e; });
    }

    // "AI bilan tezkor o'zgartirish" -- allaqachon tayyor rasmni erkin
    // matndagi buyruq bilan tuzatish (masalan "logotipni kattaroq qil",
    // "sarlavhani qisqartir"): POST /ozgartir -- LLM MAVJUD qatlamlar
    // ustidan kichik patch beradi, server QAYTA chizadi (OpenAI CHAQIRIL-
    // MAYDI, kvota sarflanmaydi). Suhbat faqat shu sahifa sessiyasida
    // (`store.editChat`) -- brif suhbatidan farqli, serverda saqlanmaydi.
    function renderChangeChatSection() {
      var sec = h('div', { class: 'cs-side-section' }, [h('h3', { class: 'ap-section-title', text: 'AI bilan tezkor o\'zgartirish' })]);
      sec.appendChild(h('div', { class: 'ap-field-hint', style: 'margin-bottom:8px', text: 'Masalan: "logotipni kattaroq qil", "sarlavhani qisqartir", "fonni to\'qroq qil". OpenAI qayta chaqirilmaydi -- tez va bepul.' }));
      if (store.editChat.length) {
        var log = h('div', { class: 'cs-chat-log cs-chat-log-sm' });
        store.editChat.forEach(function (m) {
          log.appendChild(h('div', { class: 'cs-chat-msg ' + (m.role === 'user' ? 'cs-chat-msg-user' : 'cs-chat-msg-agent') + (m.err ? ' err' : ''), text: m.text }));
        });
        if (store.busy === 'chatedit') { log.appendChild(h('div', { class: 'cs-chat-typing', text: 'AI yozmoqda…' })); }
        sec.appendChild(log);
      }
      var form = h('form', { class: 'cs-chat-form' });
      var busy = store.busy === 'chatedit';
      var input = h('textarea', { rows: 2, placeholder: 'Nimani o\'zgartiray?', maxlength: 500, disabled: busy });
      var send = h('button', { type: 'submit', class: 'btn ap-btn-sm', text: 'Yuborish', disabled: busy });
      form.appendChild(input); form.appendChild(send);
      form.addEventListener('submit', function (e) {
        e.preventDefault();
        var v = input.value.trim();
        if (!v || store.busy) { return; }
        input.value = '';
        store.editChat.push({ role: 'user', text: v });
        store.busy = 'chatedit'; renderSide();
        api('/ozgartir', { method: 'POST', body: { message: v } }).then(function (r) {
          store.busy = null;
          store.editChat.push({ role: 'ai', text: r.reply || 'O\'zgartirildi.' });
          store.selected = -1; render();
        }).catch(function (e2) {
          store.busy = null;
          store.editChat.push({ role: 'ai', text: e2.message, err: true });
          renderSide();
        });
      });
      input.addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); form.dispatchEvent(new Event('submit', { cancelable: true })); } });
      sec.appendChild(form);
      return sec;
    }

    // "Targetga ochish": maqsad + kunlik byudjet (+ hudud, profilda bo'lmasa)
    // -> POST /kreativ/<id>/target-yarat (oddiy forma, sahifa almashadi).
    // Server yetishmagan ma'lumot bo'lsa Avtopilot wizard'iga (kreativ
    // biriktirilgan holda) yo'naltiradi, aks holda darhol /avtopilot/<id>.
    function renderTargetSection() {
      var a = store.asset;
      var t = a.target || {};
      var sec = h('div', { class: 'cs-side-section', id: 'cs-target-section' }, [h('h3', { class: 'ap-section-title', text: 'Targetga ochish' })]);
      if (!t.can_create) {
        sec.appendChild(h('div', { class: 'ap-field-hint', text: 'Bu rasmdan Avtopilot kampaniyasini faqat admin tuza oladi. Rasmni PNG qilib yuklab olib, adminga bering.' }));
        return sec;
      }
      sec.appendChild(h('div', { class: 'ap-field-hint', style: 'margin-bottom:8px', text: 'Shu rasm bilan AI to\'liq kampaniya rejasini (Campaign → Ad Set → Ad) tuzadi -- siz faqat ko\'rib chiqib tasdiqlaysiz.' }));
      var form = h('form', { method: 'post', action: a.urls.target_create, class: 'cs-target-form', id: 'cs-target-form' });
      form.appendChild(h('input', { type: 'hidden', name: 'csrf_token', value: CSRF }));
      var obj = h('select', { name: 'objective' }, (t.objectives || []).map(function (o) { return h('option', { value: o.value, text: o.label, selected: o.value === (t.default_objective || 'MESSAGES') }); }));
      form.appendChild(propField('Maqsad', obj));
      var budget = h('input', { type: 'number', name: 'budget', min: 1, step: 'any', inputmode: 'decimal', placeholder: 'masalan 200000', required: true });
      form.appendChild(propField('Kunlik byudjet', budget, 'Reklama hisobi valyutasida. Byudjetni AI taxmin qila olmaydi.'));
      if (t.needs_location) {
        var loc = h('input', { type: 'text', name: 'location', placeholder: 'masalan Toshkent', required: true });
        form.appendChild(propField('Shahar/hudud', loc, 'Profilda standart hudud yo\'q -- reklama qayerda chiqadi?'));
      }
      var btn = h('button', { type: 'submit', class: 'btn ap-btn-sm ap-btn-publish', html: svgIcon('rocket') + ' Targetga ochish' });
      btn.querySelector('svg').style.cssText = 'width:13px;height:13px;vertical-align:-2px;margin-right:4px';
      form.appendChild(btn);
      form.addEventListener('submit', function (e) {
        e.preventDefault();
        if (!form.reportValidity()) { return; }
        btn.disabled = true; btn.innerHTML = '<span class="btn-spinner"></span> AI reja tuzmoqda…';
        // Saqlash sidebar'ni qayta chizadi (bu forma DOM'dan chiqadi) --
        // shuning uchun qiymatlar oldindan olinib, yangi yashirin forma yuboriladi.
        var values = { csrf_token: CSRF, objective: obj.value, budget: budget.value, location: (form.querySelector('input[name="location"]') || {}).value || '' };
        var go = function () {
          var f = h('form', { method: 'post', action: a.urls.target_create, hidden: true });
          Object.keys(values).forEach(function (k) { f.appendChild(h('input', { type: 'hidden', name: k, value: values[k] })); });
          document.body.appendChild(f);
          store.dirty = false;  // beforeunload ogohlantirishi chiqmasin
          f.submit();
        };
        if (store.dirty) { saveLayers().then(go).catch(function () { /* xabar ko'rsatildi, sidebar qayta chizildi */ }); } else { go(); }
      });
      sec.appendChild(form);
      sec.appendChild(h('div', { class: 'ap-field-hint', style: 'margin-top:8px' }, [h('a', { href: a.urls.autopilot_new + '?creative_asset_id=' + a.id, text: 'Barcha savollar bilan (Avtopilot wizard)' })]));
      return sec;
    }

    function useInAutopilot() {
      var a = store.asset;
      if (store.busy || !a.urls.autopilot) { return; }
      var go = function () {
        store.busy = 'using'; renderSide();
        api(null, { absolute: a.urls.autopilot + '/media/from-kreativ', method: 'POST', body: { creative_asset_id: a.id } }).then(function (r) {
          if (r.upload_error) { alert('Rasm qoralamaga qo\'shildi, lekin Meta\'ga yuklanmadi: ' + r.upload_error); }
          window.location = a.urls.autopilot;
        }).catch(function (e) { store.busy = null; setMsg(e.message, 'err'); renderSide(); });
      };
      if (store.dirty) { saveLayers().then(go).catch(function () { /* xabar ko'rsatildi */ }); } else { go(); }
    }

    // ---- Boshlash
    function render() { renderTopbar(); renderBanner(); renderMain(); renderSide(); }
    window.addEventListener('beforeunload', function (e) { if (store.dirty) { e.preventDefault(); e.returnValue = ''; } });
    render();
  }

  // ==================================================================
  // GALEREYA: "Bu reklamada ishlatish" (Avtopilotdan kelinganda)
  // ==================================================================
  // Asosiy sahifadagi inline shablonlar: "Yana N ta" -- sahifa almashmaydi
  var tplToggle = document.getElementById('cs-inline-tpl-toggle');
  if (tplToggle) {
    tplToggle.addEventListener('click', function () {
      var expanded = tplToggle.getAttribute('aria-expanded') === 'true';
      document.querySelectorAll('#cs-inline-tpl .cs-inline-tpl-more').forEach(function (c) { c.hidden = expanded; });
      tplToggle.setAttribute('aria-expanded', expanded ? 'false' : 'true');
      tplToggle.textContent = expanded ? tplToggle.dataset.more : tplToggle.dataset.less;
    });
  }

  var list = document.getElementById('cs-list');
  if (list && list.dataset.fromAutopilot && list.dataset.autopilotUrl) {
    var csrfList = list.dataset.csrf;
    var msgEl = document.getElementById('cs-list-msg');
    list.querySelectorAll('.cs-use-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        btn.disabled = true; btn.textContent = 'Yuborilmoqda…';
        fetch(list.dataset.autopilotUrl + '/media/from-kreativ', {
          method: 'POST', credentials: 'same-origin', headers: { 'X-CSRFToken': csrfList, 'Content-Type': 'application/json' },
          body: JSON.stringify({ creative_asset_id: Number(btn.dataset.assetId) })
        }).then(function (r) { return r.json().catch(function () { return { error: 'Server javobi tushunarsiz (' + r.status + ').' }; }).then(function (j) { if (!r.ok) { throw new Error(j.error || 'Xatolik'); } return j; }); })
          .then(function (r) {
            if (r.upload_error) { alert('Rasm qoralamaga qo\'shildi, lekin Meta\'ga yuklanmadi: ' + r.upload_error); }
            window.location = list.dataset.autopilotUrl;
          })
          .catch(function (e) {
            btn.disabled = false; btn.textContent = 'Bu reklamada ishlatish';
            if (msgEl) { msgEl.hidden = false; msgEl.className = 'ap-modal-msg err'; msgEl.textContent = e.message; }
          });
      });
    });
  }
})();
