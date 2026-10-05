/* TokenTier dashboard. Vanilla JS, no build step, no network except same-origin /api and /events.
 * All log-derived text is inserted with textContent / text nodes, never innerHTML.
 * Dates: nothing hard-coded. "Today" comes from the browser clock (see nowDate()), re-evaluated
 * every 30 s and on visibilitychange. Debug hook: set window.__ttNow (ms, Date or function) to fake the clock.
 */
(function () {
  'use strict';

  // ------------------------------------------------------------------ helpers
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  function h(tag, props) {
    var e = document.createElement(tag);
    if (props) Object.keys(props).forEach(function (k) {
      var v = props[k];
      if (k.indexOf('aria-') === 0) { if (v != null) e.setAttribute(k, String(v)); return; }
      if (v == null || v === false) return;
      if (k === 'class') e.className = v;
      else if (k === 'text') e.textContent = v;
      else if (k === 'dataset') Object.keys(v).forEach(function (d) { e.dataset[d] = v[d]; });
      else if (k.slice(0, 2) === 'on') e.addEventListener(k.slice(2), v);
      else e.setAttribute(k, v === true ? '' : v);
    });
    for (var i = 2; i < arguments.length; i++) append(e, arguments[i]);
    return e;
  }
  function append(e, c) {
    if (c == null || c === false) return;
    if (Array.isArray(c)) { c.forEach(function (x) { append(e, x); }); return; }
    e.appendChild(c.nodeType ? c : document.createTextNode(String(c)));
  }
  var SVGNS = 'http://www.w3.org/2000/svg';
  function sv(tag, attrs) {
    var e = document.createElementNS(SVGNS, tag);
    if (attrs) Object.keys(attrs).forEach(function (k) { if (attrs[k] != null) e.setAttribute(k, attrs[k]); });
    for (var i = 2; i < arguments.length; i++) append(e, arguments[i]);
    return e;
  }
  function svText(x, y, text, attrs) {
    var t = sv('text', Object.assign({ x: x, y: y }, attrs || {}));
    t.textContent = text;
    return t;
  }
  function store(k, v) {
    try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; }
    return null;
  }
  // small line icons (static markup built with createElementNS, no data involved)
  var ICONS = {
    sun: [['circle', { cx: 8, cy: 8, r: 3 }], ['path', { d: 'M8 1.5v1.6M8 12.9v1.6M1.5 8h1.6M12.9 8h1.6M3.4 3.4l1.1 1.1M11.5 11.5l1.1 1.1M3.4 12.6l1.1-1.1M11.5 4.5l1.1-1.1' }]],
    moon: [['path', { d: 'M13.5 9.6A5.6 5.6 0 0 1 6.4 2.5a5.6 5.6 0 1 0 7.1 7.1z' }]],
    copy: [['rect', { x: 5.5, y: 5.5, width: 8, height: 8, rx: 1.5 }], ['path', { d: 'M10.5 5.5V3.8A1.3 1.3 0 0 0 9.2 2.5H3.8a1.3 1.3 0 0 0-1.3 1.3v5.4a1.3 1.3 0 0 0 1.3 1.3h1.7' }]],
    chev: [['path', { d: 'M4 6l4 4 4-4' }]],
    list: [['path', { d: 'M5.5 4h8M5.5 8h8M5.5 12h8' }], ['circle', { cx: 2.6, cy: 4, r: .6 }], ['circle', { cx: 2.6, cy: 8, r: .6 }], ['circle', { cx: 2.6, cy: 12, r: .6 }]],
    panel: [['rect', { x: 2, y: 2.5, width: 12, height: 11, rx: 1.6 }], ['path', { d: 'M9.5 2.5v11M11.2 5.5h.8M11.2 8h.8' }]],
    folder: [['path', { d: 'M2 4.6c0-.9.7-1.6 1.6-1.6h2.6l1.4 1.6h4.8c.9 0 1.6.7 1.6 1.6v5.6c0 .9-.7 1.6-1.6 1.6H3.6c-.9 0-1.6-.7-1.6-1.6z' }]]
  };
  function icon(name, cls) {
    var s = sv('svg', { viewBox: '0 0 16 16', 'aria-hidden': 'true', focusable: 'false', class: cls || null });
    (ICONS[name] || []).forEach(function (p) { s.append(sv(p[0], p[1])); });
    return s;
  }
  function announce(msg) {
    var a = $('#announce');
    a.textContent = '';
    setTimeout(function () { a.textContent = msg; }, 30);
  }

  // ------------------------------------------------------------------ clock / dates
  function nowDate() {
    var v = window.__ttNow;
    if (typeof v === 'function') return new Date(v());
    if (v != null) return new Date(v);
    return new Date();
  }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function dateKey(d) { return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()); }
  function keyToDate(k) { var p = k.split('-').map(Number); return new Date(p[0], p[1] - 1, p[2]); }
  function addDays(k, n) { var d = keyToDate(k); d.setDate(d.getDate() + n); return dateKey(d); }
  function todayKey() { return dateKey(nowDate()); }

  var fDayLong = new Intl.DateTimeFormat(undefined, { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' });
  var fDayMid = new Intl.DateTimeFormat(undefined, { weekday: 'short', day: 'numeric', month: 'short' });
  var fDayShort = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short' });
  var fWeekday = new Intl.DateTimeFormat(undefined, { weekday: 'short', day: 'numeric' });
  var fTime = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' });
  var fTimeSec = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  var fFull = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'medium' });
  var nInt = new Intl.NumberFormat();
  var nCompact = new Intl.NumberFormat(undefined, { notation: 'compact', minimumFractionDigits: 1, maximumFractionDigits: 1 });
  var nRate = new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 3 });
  var nPct = new Intl.NumberFormat(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  var cost2 = new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 });
  var cost4 = new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD', minimumFractionDigits: 4, maximumFractionDigits: 4 });
  var DASH = '–';

  function dayParts(key) {
    if (!key) return { name: 'Unknown date', date: '' };
    var t = todayKey(), base = fDayLong.format(keyToDate(key));
    if (key === t) return { name: 'Today', date: base, today: true };
    if (key === addDays(t, -1)) return { name: 'Yesterday', date: base };
    return { name: fDayMid.format(keyToDate(key)), date: '' };
  }
  function dayLabel(key) { var p = dayParts(key); return p.date ? p.name + ' · ' + p.date : p.name; }
  var LEGACY_TIP = 'Imported from the old router-kit log: token breakdown unavailable, so no cost estimate';
  function legacyBadge() { return h('span', { class: 'chip legacy-badge', title: LEGACY_TIP }, 'legacy'); }
  // 1 -> "1.0", 1.5 -> "1.5", 1.25 -> "1.25"
  function fmtMult(v) {
    if (!isNum(v)) return DASH;
    var t = String(v);
    return /^-?\d+$/.test(t) ? t + '.0' : t;
  }
  var MULT_CMD = 'tokentier config set baseline_token_multiplier 1.5';
  function multSource(src) {
    if (src === 'pricing.json') return 'from pricing.json';
    if (src === 'config.json') return 'from config.json';
    if (src === 'env') return 'from the TOKENTIER_BASELINE_TOKEN_MULTIPLIER environment variable';
    if (src === 'flag') return 'from a command-line flag';
    return 'built-in default';
  }
  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  function fmtCost(v) {
    if (!isNum(v)) return DASH;
    return (v !== 0 && Math.abs(v) < 0.01 ? cost4 : cost2).format(v);
  }
  function fmtInt(v) { return isNum(v) ? nInt.format(v) : DASH; }
  function fmtTok(v) { return !isNum(v) ? DASH : (Math.abs(v) < 1000 ? nInt.format(v) : nCompact.format(v)); }
  function parseT(iso) { var d = iso ? new Date(iso) : null; return d && !isNaN(d) ? d : null; }
  function fmtTime(iso) { var d = parseT(iso); return d ? fTime.format(d) : DASH; }
  function fmtFull(iso) { var d = parseT(iso); return d ? fFull.format(d) : DASH; }
  function fmtDur(ms) {
    if (!isNum(ms)) return DASH;
    var s = Math.round(ms / 1000);
    if (s < 1) return '<1s';
    if (s < 60) return s + 's';
    var m = Math.floor(s / 60);
    if (m < 60) return m + 'm ' + pad(s % 60) + 's';
    return Math.floor(m / 60) + 'h ' + pad(m % 60) + 'm';
  }
  function fmtAgo(ms) {
    var s = Math.max(0, Math.round(ms / 1000));
    if (s < 45) return 'just now';
    var m = Math.round(s / 60);
    if (m < 60) return m + ' min ago';
    var hr = Math.floor(m / 60);
    if (hr < 24) return hr + ' h ago';
    var d = Math.round(hr / 24);
    return d + (d === 1 ? ' day ago' : ' days ago');
  }
  // relative for today ("3 min ago"), clock time otherwise; absolute on hover
  function timeEl(iso, opts) {
    var d = parseT(iso);
    if (!d) return h('span', null, DASH);
    var e = h('time', { datetime: iso, title: fFull.format(d) });
    var age = nowDate() - d;
    if ((opts && opts.rel === 'always') || (dateKey(d) === todayKey() && age < 3600000)) { e.dataset.rel = String(d.getTime()); e.textContent = fmtAgo(age); }
    else e.textContent = opts && opts.withDate ? fDayShort.format(d) + ', ' + fTime.format(d) : fTime.format(d);
    return e;
  }
  function txt(v) { return v == null || v === '' ? DASH : String(v); }
  function midEllipsis(s, max) {
    s = String(s == null ? '' : s);
    if (s.length <= max) return s;
    var k = max - 1, a = Math.ceil(k / 2), b = Math.floor(k / 2);
    return s.slice(0, a) + '…' + s.slice(s.length - b);
  }
  function hue(s) {
    var x = 2166136261;
    s = String(s || '');
    for (var i = 0; i < s.length; i++) { x ^= s.charCodeAt(i); x = Math.imul(x, 16777619); }
    return (x >>> 0) % 360;
  }
  function shortModel(m) { return m ? String(m).replace(/^claude-/, '').replace(/-\d{8}$/, '') : ''; }
  // uuid-like ids -> first 8 chars; other ids (legacy-2026-10-03, ...) stay readable
  function shortSid(id) { id = String(id || ''); return /^[0-9a-f]{8}-?[0-9a-f-]{8,}$/i.test(id) ? id.slice(0, 8) : midEllipsis(id, 18); }
  function cap(s) { s = String(s || ''); return s.charAt(0).toUpperCase() + s.slice(1); }

  // ------------------------------------------------------------------ state
  var DEFAULTS = { range: 'today', from: '', to: '', project: '', session: '', status: '', tier: '', q: '', router: '', sort: '', tab: 'overview', task: '' };
  var S = Object.assign({}, DEFAULTS);
  var D = { ov: null, tasks: null, projects: null, sessions: null, pricing: null, health: null, detail: null, detailErr: null };
  var RANGES = ['today', 'yesterday', '7d', '30d', 'all', 'custom'];
  var TABS = ['overview', 'projects', 'sessions'];
  var SORTS = ['', 'cost', 'duration', 'tokens'];
  var UI = { density: store('tt-density') === 'compact' ? 'compact' : 'comfortable', collapsed: {}, pSort: 'recent', pQ: '', sSort: 'recent', sheet: false };
  try { UI.collapsed = JSON.parse(store('tt-collapsed') || '{}') || {}; } catch (e) { UI.collapsed = {}; }

  function parseHash() {
    var p = new URLSearchParams(location.hash.replace(/^#/, ''));
    var s = Object.assign({}, DEFAULTS);
    Object.keys(DEFAULTS).forEach(function (k) { if (p.has(k)) s[k] = p.get(k) || ''; });
    if (RANGES.indexOf(s.range) < 0) s.range = DEFAULTS.range;
    if (TABS.indexOf(s.tab) < 0) s.tab = 'overview';
    if (SORTS.indexOf(s.sort) < 0) s.sort = '';
    if (s.router !== '1') s.router = '';
    if (s.range === 'custom' && !s.from && !s.to) s.range = DEFAULTS.range;
    return s;
  }
  function writeHash() {
    var p = new URLSearchParams();
    Object.keys(DEFAULTS).forEach(function (k) { if (S[k] && S[k] !== DEFAULTS[k]) p.set(k, S[k]); });
    if (S.range !== 'custom') { p.delete('from'); p.delete('to'); }
    var qs = p.toString();
    var url = location.pathname + location.search + (qs ? '#' + qs : '');
    try { history.replaceState(null, '', url); } catch (e) { /* ignore */ }
    store('tt-last', qs);
  }

  // ------------------------------------------------------------------ api
  function api(path, params) {
    var q = new URLSearchParams();
    Object.keys(params || {}).forEach(function (k) { var v = params[k]; if (v !== '' && v != null) q.set(k, v); });
    var qs = q.toString();
    return fetch(path + (qs ? '?' + qs : ''), { cache: 'no-store' }).then(function (r) {
      return r.json().catch(function () { return null; }).then(function (b) {
        if (!r.ok) throw new Error((b && b.error) || ('HTTP ' + r.status));
        return b;
      });
    });
  }
  function rangeParams() {
    if (S.range === 'custom') {
      var f = S.from || S.to || todayKey(), t = S.to || S.from || todayKey();
      return { from: f, to: t };
    }
    return { from: S.range };
  }
  function taskParams(extra) {
    return Object.assign({}, rangeParams(), {
      project: S.project, session: S.session, status: S.status, tier: S.tier, q: S.q, sort: S.sort,
      router_only: S.router ? '1' : ''
    }, extra || {});
  }
  function hasTaskFilters() { return !!(S.status || S.tier || S.q || S.router || S.session); }

  // ------------------------------------------------------------------ refresh
  var seq = 0, detailSeq = 0, announceCount = false;
  function refresh() {
    var my = ++seq;
    var loaded = D.tasks ? D.tasks.items.length : 0;
    var limit = Math.max(100, Math.min(1000, Math.ceil(loaded / 100) * 100));
    var rp = rangeParams();
    var reqs = [
      api('api/overview', Object.assign({}, rp, { project: S.project })),
      api('api/tasks', taskParams({ limit: limit })),
      api('api/projects'),
      api('api/pricing'),
      api('api/health'),
      S.tab === 'sessions' ? api('api/sessions', Object.assign({}, rp, { project: S.project })) : Promise.resolve(D.sessions),
      S.task ? api('api/tasks/' + encodeURIComponent(S.task)).then(function (t) { return { t: t }; }, function (e) { return { err: e.message }; }) : Promise.resolve(null)
    ];
    return Promise.all(reqs).then(function (r) {
      if (my !== seq) return;
      D.ov = r[0]; D.tasks = r[1]; D.projects = r[2]; D.pricing = r[3]; D.health = r[4]; D.sessions = r[5];
      D.detail = r[6] && r[6].t || null; D.detailErr = r[6] && r[6].err || null;
      hideBanner();
      renderAll();
      $('#updated').textContent = 'updated ' + fTimeSec.format(nowDate());
      if (announceCount) { announceCount = false; announce(fmtInt(D.tasks.total) + (D.tasks.total === 1 ? ' task' : ' tasks')); }
    }, function (e) {
      if (my !== seq) return;
      showBanner(e);
    });
  }
  function showBanner(e) {
    var net = e && (e.name === 'TypeError');
    $('#banner-msg').textContent = net
      ? 'Cannot reach the TokenTier server. Is it running? (python3 dashboard/server.py)'
      : 'The server returned an error: ' + (e && e.message || 'unknown');
    $('#banner').hidden = false;
    $('#updated').textContent = 'update failed';
    $('#list').classList.toggle('stale', true);
    if (!D.ov) renderLoading(true);
  }
  function hideBanner() { $('#banner').hidden = true; $('#list').classList.remove('stale'); }

  function loadMore() {
    var my = ++seq;
    var n = D.tasks.items.length;
    var btn = $('#more');
    btn.disabled = true; btn.textContent = 'Loading…';
    api('api/tasks', taskParams({ limit: 100, offset: n })).then(function (r) {
      btn.disabled = false;
      if (my !== seq) return;
      D.tasks = { total: r.total, items: D.tasks.items.concat(r.items), counts: r.counts || D.tasks.counts };
      renderList();
    }, function (e) { btn.disabled = false; showBanner(e); });
  }
  function resetList() { D.tasks = null; }
  function changed(opts) {
    if (!opts || !opts.keepList) resetList();
    writeHash();
    syncControls();
    if (S.tab === 'overview') { renderToolbar(); renderList(); }
    announceCount = true;
    refresh();
  }

  // ------------------------------------------------------------------ live (SSE with polling fallback)
  var es = null, pollTimer = null, debounceTimer = null, pending = false;
  function setLive(on, mode) {
    $('#dot').dataset.live = on ? 'true' : 'false';
    $('#live-text').textContent = on ? 'Live' : (mode === 'polling' ? 'Polling' : 'Offline');
  }
  function scheduleRefresh() {
    if (document.hidden) { pending = true; return; }
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(refresh, 500);
  }
  function startPoll() {
    if (pollTimer) return;
    pollTimer = setInterval(function () { if (document.hidden) { pending = true; } else { refresh(); } }, 5000);
  }
  function stopPoll() { clearInterval(pollTimer); pollTimer = null; }
  function connectLive() {
    if (!window.EventSource) { setLive(false, 'polling'); startPoll(); return; }
    es = new EventSource('events');
    es.onopen = function () { setLive(true); stopPoll(); };
    es.onmessage = function () { scheduleRefresh(); };
    es.onerror = function () { setLive(false, 'polling'); startPoll(); };
  }

  // ------------------------------------------------------------------ midnight rollover + clocks
  var lastToday = todayKey();
  function checkRollover() {
    var k = todayKey();
    if (k !== lastToday) {
      lastToday = k;
      updateTodayBits();
      renderAll();          // labels flip immediately from cached data
      refresh();            // then re-fetch: server resolves today/7d/... keywords itself
      return true;
    }
    return false;
  }
  function tick() {
    if (document.hidden) return;
    if (checkRollover()) return;
    if (D.ov && D.ov.totals.running > 0) refresh();   // pick up stale transitions and new costs
  }
  // 1 s ticker: running timers and relative times only, never refetches; paused while hidden
  var secs = 0;
  function liveTick() {
    if (document.hidden) return;
    var now = nowDate().getTime();
    $$('[data-start]').forEach(function (e) {
      var st = Number(e.dataset.start);
      if (st) e.textContent = fmtDur(Math.max(0, now - st));
    });
    if (++secs % 15 === 0) {
      $$('[data-rel]').forEach(function (e) { e.textContent = fmtAgo(now - Number(e.dataset.rel)); });
    }
  }
  function updateTodayBits() {
    var t = todayKey();
    $('#today-label').textContent = '· ' + fDayLong.format(keyToDate(t));
    $('#from').max = t; $('#to').max = t;
  }
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) return;
    var rolled = checkRollover();
    secs = 14; liveTick();
    if (!rolled && pending) { pending = false; refresh(); }
  });

  // ------------------------------------------------------------------ small components
  function tierKey(t) {
    var base = t && D.pricing && D.pricing.models && D.pricing.models[t] && D.pricing.models[t].tier || t;
    return base === 'haiku' || base === 'sonnet' || base === 'opus' ? base : 'other';
  }
  function tierLabel(key) {
    var m = D.pricing && D.pricing.models && D.pricing.models[key];
    return (m && m.label) || (key ? cap(key) : DASH);
  }
  function tierChip(key, model) {
    return h('span', { class: 'chip t-' + tierKey(key), title: model ? 'Model: ' + model : null }, key ? tierLabel(key) : DASH);
  }
  function tierDot(key) { return h('span', { class: 'tdot t-' + tierKey(key), 'aria-hidden': 'true' }); }
  function baselineName() { return D.pricing && D.pricing.baseline_tier ? cap(D.pricing.baseline_tier) : 'Opus'; }
  var GLYPH = { pass: '✓', fail: '✕', escalated: '↗', stale: '⏸', unknown: '?', running: '' };
  var WORD = { pass: 'Passed', fail: 'Failed', escalated: 'Escalated', stale: 'Stale', unknown: 'Unverified', running: 'Running' };
  var TIP_INFERRED = 'No STATUS line in the report; inferred from its content';
  var TIP_UNVERIFIED = 'The worker did not report a status and its report mentions a problem or is empty: please check';
  function stKey(st) { return GLYPH.hasOwnProperty(st) ? st : 'unknown'; }
  function staleTip(t) {
    var m = t && t.stale_reason ? /(\d+)/.exec(t.stale_reason) : null;
    return 'No activity for ' + (m ? m[1] : 'a while') + ' min: the worker was probably stopped. Its tokens are not logged because it never reported back.';
  }
  function statusTip(t) {
    var s = stKey(t.status);
    if (s === 'unknown') return TIP_UNVERIFIED;
    if (s === 'stale') return staleTip(t);
    if (s === 'pass' && t.status_source === 'inferred') return 'Passed. ' + TIP_INFERRED;
    return WORD[s];
  }
  function statusChip(st, src, t) {
    var s = stKey(st || 'unknown');
    var tip = statusTip(t || { status: s, status_source: src });
    if (s === 'pass' && src === 'inferred') {
      return h('span', { class: 'chip s-pass', title: tip }, h('span', { class: 'g', 'aria-hidden': 'true' }, '✓'), 'Passed', h('span', { class: 'src-mark' }, 'inferred'));
    }
    return h('span', { class: 'chip s-' + s, title: tip },
      s === 'running' ? h('span', { class: 'pip', 'aria-hidden': 'true' }) : h('span', { class: 'g', 'aria-hidden': 'true' }, GLYPH[s]), WORD[s]);
  }
  function statusSourceText(t) {
    if (t.status === 'running' || t.status === 'stale') return DASH;
    if (t.status_source === 'status_line') return 'STATUS line in the report';
    if (t.status_source === 'inferred') return TIP_INFERRED;
    return t.status === 'unknown' ? 'none (' + TIP_UNVERIFIED + ')' : DASH;
  }
  function projChip(name, path) {
    var n = name || 'unknown';
    return h('span', { class: 'pj', title: path ? n + ' · ' + path : n },
      h('span', { class: 'pj-dot', style: '--h:' + hue(n), 'aria-hidden': 'true' }), h('span', null, midEllipsis(n, 28)));
  }
  function mixBar(mix, cls) {
    var keys = ['haiku', 'sonnet', 'opus', 'unknown'];
    var tot = keys.reduce(function (a, k) { return a + (mix && mix[k] || 0); }, 0);
    var parts = keys.filter(function (k) { return mix && mix[k]; }).map(function (k) {
      return tierLabel(k === 'unknown' ? 'other' : k) + ' ' + Math.round(mix[k] / tot * 100) + '%';
    });
    var bar = h('div', { class: 'mix' + (cls ? ' ' + cls : ''), role: 'img', 'aria-label': tot ? 'Tier mix: ' + parts.join(', ') : 'No tasks', title: tot ? parts.join(' · ') : null });
    keys.forEach(function (k) {
      if (mix && mix[k]) bar.append(h('i', { class: 't-' + tierKey(k), style: 'width:' + (mix[k] / tot * 100).toFixed(2) + '%' }));
    });
    return bar;
  }

  // ------------------------------------------------------------------ render: controls
  function syncControls() {
    $$('#range button').forEach(function (b) { b.setAttribute('aria-pressed', String(b.dataset.range === S.range)); });
    $('#custom').hidden = S.range !== 'custom';
    if (S.range === 'custom') { $('#from').value = S.from || ''; $('#to').value = S.to || ''; }
    var sel = $('#project');
    if (sel.value !== S.project) sel.value = S.project;
    if (document.activeElement !== $('#f-q')) $('#f-q').value = S.q;
    $('#q-clear').hidden = !$('#f-q').value;
    $('#f-router').checked = !!S.router;
    $('#f-sort').value = S.sort;
    $$('#density button').forEach(function (b) { b.setAttribute('aria-pressed', String(b.dataset.density === UI.density)); });
    $('#list').classList.toggle('compact', UI.density === 'compact');
    TABS.forEach(function (t) {
      var on = S.tab === t;
      var b = $('#tab-' + t);
      b.setAttribute('aria-selected', String(on));
      b.tabIndex = on ? 0 : -1;
      $('#view-' + t).hidden = !on;
    });
    document.body.dataset.tab = S.tab;
    renderActiveFilters();
  }
  function filterChip(k, label, title, onClear) {
    return h('span', { class: 'chip-filter' },
      h('span', { title: title }, h('span', { class: 'k' }, k + ' '), label),
      h('button', { type: 'button', 'aria-label': 'Clear ' + k.toLowerCase() + ' filter', title: 'Clear ' + k.toLowerCase() + ' filter', onclick: onClear }, '✕'));
  }
  function renderActiveFilters() {
    var c = $('#active-filters');
    var kids = [];
    if (S.project) kids.push(filterChip('Project', midEllipsis(S.project, 40), S.project, function () { S.project = ''; S.session = ''; changed(); }));
    if (S.session) kids.push(filterChip('Session', shortSid(S.session), S.session, clearSession));
    if (kids.length > 1 || (kids.length && hasTaskFilters())) kids.push(h('button', { type: 'button', class: 'btn ghost sm', onclick: clearAllFilters }, 'Clear all'));
    c.replaceChildren.apply(c, kids);
    c.hidden = !kids.length;
  }
  function clearSession() { S.session = ''; changed(); }
  function clearAllFilters() { S.project = ''; S.session = ''; S.status = ''; S.tier = ''; S.q = ''; S.router = ''; changed(); }

  function renderProjectSelect() {
    var sel = $('#project');
    var names = visibleProjects().map(function (p) { return p.project; });
    if (S.project && names.indexOf(S.project) < 0) names.push(S.project);
    var sig = names.join('\u0001');
    if (sel.dataset.sig !== sig) {
      sel.dataset.sig = sig;
      sel.replaceChildren(h('option', { value: '' }, 'All projects'));
      names.forEach(function (n) { sel.append(h('option', { value: n }, midEllipsis(n, 40))); });
    }
    sel.value = S.project;
    $('#tabn-projects').textContent = names.length ? fmtInt(visibleProjects().length) : '';
  }

  var STATUS_FILTERS = [
    { v: '', label: 'All' },
    { v: 'running', label: 'Running', s: 'running' },
    { v: 'pass', label: 'Passed', s: 'pass' },
    { v: 'fail', label: 'Failed', s: 'fail' },
    { v: 'escalated', label: 'Escalated', s: 'escalated' },
    { v: 'stale,unknown', label: 'Stale / unverified', s: 'stale' }
  ];
  function statusPressed(v) {
    if (v === 'stale,unknown') return S.status === v || S.status === 'stale' || S.status === 'unknown';
    return S.status === v;
  }
  function renderToolbar() {
    var c = D.tasks && D.tasks.counts && D.tasks.counts.status;
    var sb = $('#f-status');
    var focus = document.activeElement && sb.contains(document.activeElement) ? document.activeElement.dataset.v : null;
    sb.replaceChildren.apply(sb, STATUS_FILTERS.map(function (f) {
      var n = null;
      if (c) {
        n = f.v === '' ? Object.keys(c).reduce(function (a, k) { return a + c[k]; }, 0)
          : f.v === 'stale,unknown' ? (c.stale || 0) + (c.unknown || 0) : (c[f.v] || 0);
      }
      return h('button', { type: 'button', class: 'fchip' + (f.s ? ' s-' + f.s : ''), 'aria-pressed': statusPressed(f.v) ? 'true' : 'false', dataset: { v: f.v },
        onclick: function () { S.status = f.v === '' || statusPressed(f.v) ? '' : f.v; changed(); } },
        f.s ? (f.s === 'running' ? h('span', { class: 'g', 'aria-hidden': 'true' }, '●') : h('span', { class: 'g', 'aria-hidden': 'true' }, GLYPH[f.s])) : null,
        f.label, n == null ? null : h('span', { class: 'n' }, fmtInt(n)));
    }));
    if (focus != null) { var fb = sb.querySelector('[data-v="' + focus + '"]'); if (fb) fb.focus(); }

    var tc = D.tasks && D.tasks.counts && D.tasks.counts.tier || {};
    var tb = $('#f-tier');
    var tfocus = document.activeElement && tb.contains(document.activeElement) ? document.activeElement.dataset.v : null;
    var keys = ['haiku', 'sonnet', 'opus'];
    ['lead', 'unknown'].forEach(function (k) { if (tc[k] || S.tier === k) keys.push(k); });
    tb.replaceChildren.apply(tb, keys.map(function (k) {
      var on = S.tier === k;
      return h('button', { type: 'button', class: 'fchip t-' + tierKey(k), 'aria-pressed': on ? 'true' : 'false', dataset: { v: k }, title: 'Show only ' + tierLabel(k) + ' tasks',
        onclick: function () { S.tier = on ? '' : k; changed(); } },
        tierDot(k), tierLabel(k), D.tasks && D.tasks.counts ? h('span', { class: 'n' }, fmtInt(tc[k] || 0)) : null);
    }));
    if (tfocus) { var tf = tb.querySelector('[data-v="' + tfocus + '"]'); if (tf) tf.focus(); }
  }

  // ------------------------------------------------------------------ render: all
  function renderAll() {
    renderProjectSelect();
    syncControls();
    if (S.tab === 'overview' || !D.ov) {
      renderKpis();
      renderCharts();
      renderSavings();
      renderToolbar();
      renderList();
      renderDetail();
      renderPricing();
    }
    if (S.tab === 'projects') renderProjects();
    if (S.tab === 'sessions') renderSessions();
    renderFooter();
  }
  function renderLoading(err) {
    var k = $('#kpis');
    if (err) {
      k.replaceChildren(h('div', { class: 'state', style: 'grid-column:1/-1' }, h('b', null, 'No data yet'), 'The dashboard will fill in as soon as the server answers.'));
      return;
    }
    var tiles = [];
    for (var i = 0; i < 10; i++) tiles.push(h('div', { class: 'sk sk-tile', 'aria-hidden': 'true' }));
    k.replaceChildren.apply(k, tiles);
    ['#g-tier', '#g-cost', '#g-tasks'].forEach(function (s) {
      $(s + ' .chart').replaceChildren(h('span', { class: 'sk', style: 'height:150px', 'aria-hidden': 'true' }));
    });
    var rows = [];
    for (var j = 0; j < 6; j++) rows.push(h('div', { class: 'sk sk-row', 'aria-hidden': 'true' }));
    $('#list').replaceChildren.apply($('#list'), rows);
    $('#list').setAttribute('aria-busy', 'true');
  }

  // ------------------------------------------------------------------ KPIs
  function kpi(o) {
    var tag = o.onclick ? 'button' : 'div';
    var props = { class: 'kpi', dataset: { fk: 'kpi-' + o.key } };
    if (o.onclick) { props.type = 'button'; props.onclick = o.onclick; props['aria-pressed'] = o.pressed ? 'true' : 'false'; }
    return h(tag, props,
      h('span', { class: 'kpi-l' }, o.dot ? tierDot(o.dot) : null, o.label),
      h('span', { class: 'kpi-v ' + (o.cls || ''), title: o.title }, o.value),
      h('span', { class: 'kpi-s' }, o.sub || ''));
  }
  // the lead (main session) block of /api/overview; has=false when nothing was logged in range
  function leadOf(t) {
    var l = t && t.lead;
    if (!l || !l.tokens) return { has: false, cost: 0, tokens: { total: 0 }, turns: 0, sessions: 0, unknown_cost: 0 };
    return { has: l.tokens.total > 0, cost: l.cost, tokens: l.tokens, turns: l.turns, sessions: l.sessions, unknown_cost: l.unknown_cost || 0 };
  }
  function setFilter(k, v) { S[k] = S[k] === v ? '' : v; changed(); }
  function renderKpis() {
    var el = $('#kpis');
    if (!D.ov) return;
    var focus = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.fk : null;
    var o = D.ov, t = o.totals, bt = o.by_tier;
    var tiles = [
      kpi({ key: 'done', label: 'Completed', cls: 'c-green', value: [fmtInt(t.completed), h('small', null, ' / ' + fmtInt(t.tasks))], sub: fmtInt(t.running) + ' running', title: 'passed tasks / all tasks in range' }),
      kpi({ key: 'esc', label: 'Escalations', cls: t.escalations ? 'c-amber' : '', value: fmtInt(t.escalations), sub: 'tier jumps' })
    ];
    ['haiku', 'sonnet', 'opus'].forEach(function (k) {
      tiles.push(kpi({
        key: k, dot: k, label: tierLabel(k) + ' tasks', value: fmtInt(bt[k] ? bt[k].tasks : null),
        sub: bt[k] ? fmtCost(bt[k].cost) : '', pressed: S.tier === k, onclick: function () { setFilter('tier', k); },
        title: 'Click to filter the task list by this tier'
      }));
    });
    var tk = t.tokens;
    tiles.push(kpi({ key: 'tok', label: 'Tokens', value: fmtTok(tk.total), sub: 'out ' + fmtTok(tk.output) + ' · cache ' + fmtTok(tk.cache_read), title: fmtInt(tk.total) + ' tokens: ' + 'input ' + fmtInt(tk.input) + ', output ' + fmtInt(tk.output) + ', cache write ' + fmtInt(tk.cache_creation) + ', cache read ' + fmtInt(tk.cache_read) }));
    tiles.push(kpi({ key: 'cost', label: 'Est. cost', value: fmtCost(t.cost), sub: t.unknown_cost_tasks ? fmtInt(t.unknown_cost_tasks) + ' without cost data' : 'USD, estimate' }));
    tiles.push(kpi({ key: 'fail', label: 'Failures', cls: t.failed ? 'c-red' : '', value: fmtInt(t.failed), sub: S.status === 'fail' ? 'filtering the list' : 'click to filter', pressed: S.status === 'fail', onclick: function () { setFilter('status', 'fail'); } }));
    var ld = leadOf(t);
    tiles.push(kpi({ key: 'lead', label: 'Lead session', cls: 'c-mute', value: ld.has ? fmtCost(ld.cost) : DASH,
      sub: ld.has ? fmtTok(ld.tokens.total) + ' tokens' + (ld.unknown_cost ? ' · ' + fmtInt(ld.unknown_cost) + ' unpriced' : '') : 'no lead data',
      title: ld.has ? 'The main session own usage: ' + fmtInt(ld.tokens.total) + ' tokens (input ' + fmtInt(ld.tokens.input) + ', output ' + fmtInt(ld.tokens.output) + ', cache write ' + fmtInt(ld.tokens.cache_creation) + ', cache read ' + fmtInt(ld.tokens.cache_read) + '), ' + fmtInt(ld.turns) + ' turns in ' + fmtInt(ld.sessions) + ' sessions. Not part of the routing savings.' : 'Logged by the Stop hook once the lead session has run a turn' }));
    tiles.push(kpi({ key: 'spend', label: 'Total spend', value: fmtCost(isNum(t.total_spend) ? t.total_spend : t.cost),
      sub: ld.has ? 'tasks + lead session' : 'tasks only', title: 'Estimated cost of delegated tasks plus the lead session' }));
    el.replaceChildren.apply(el, tiles);
    if (focus) { var f = el.querySelector('[data-fk="' + focus + '"]'); if (f) f.focus(); }
  }

  // ------------------------------------------------------------------ charts (hand-rolled SVG)
  var tipEl = null;
  function bindTip(svg) {
    svg.addEventListener('mousemove', function (e) {
      var tgt = e.target.closest && e.target.closest('[data-tip]');
      if (!tgt) { tipEl.classList.remove('show'); return; }
      tipEl.textContent = tgt.getAttribute('data-tip');
      tipEl.classList.add('show');
      var x = Math.min(e.clientX + 14, window.innerWidth - tipEl.offsetWidth - 8);
      tipEl.style.left = Math.max(4, x) + 'px';
      tipEl.style.top = (e.clientY + 16) + 'px';
    });
    svg.addEventListener('mouseleave', function () { tipEl.classList.remove('show'); });
  }
  function niceMax(v) {
    if (!(v > 0)) return 1;
    var p = Math.pow(10, Math.floor(Math.log10(v))), n = v / p;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * p;
  }
  var axisCost = new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD', notation: 'compact', maximumFractionDigits: 2 });
  function srTable(caption, head, rows) {
    return h('div', { class: 'sr' }, h('table', null, h('caption', null, caption),
      h('thead', null, h('tr', null, head.map(function (x) { return h('th', { scope: 'col' }, x); }))),
      h('tbody', null, rows.map(function (r) { return h('tr', null, r.map(function (c) { return h('td', null, c); })); }))));
  }
  function emptyChart(el, msg) { el.replaceChildren(h('div', { class: 'empty-chart' }, msg)); }
  function chartWidth(el) { return Math.max(220, Math.floor(el.clientWidth || el.parentNode.clientWidth - 32 || 300)); }

  function drawTier() {
    var el = $('#g-tier .chart'), o = D.ov;
    var total = o.totals.tasks;
    if (!total) return emptyChart(el, 'No tasks in this range.');
    var keys = ['haiku', 'sonnet', 'opus'];
    if (o.by_tier.unknown && o.by_tier.unknown.tasks) keys.push('unknown');
    var W = chartWidth(el), rowH = keys.length > 3 ? 44 : 56, labW = 92, valW = 78, H = keys.length * rowH;
    var barW = Math.max(40, W - labW - valW);
    var svg = sv('svg', { width: W, height: H, viewBox: '0 0 ' + W + ' ' + H, role: 'img',
      'aria-label': 'Tier distribution: ' + keys.map(function (k) { return tierLabel(k) + ' ' + o.by_tier[k].tasks + ' tasks'; }).join(', ') + '. Total ' + total + ' tasks.' });
    var rows = [];
    keys.forEach(function (k, i) {
      var b = o.by_tier[k], y = i * rowH + (rowH - 34) / 2, pct = total ? b.tasks / total * 100 : 0;
      var tip = tierLabel(k) + ': ' + b.tasks + ' tasks (' + nPct.format(pct) + '%), ' + fmtTok(b.tokens) + ' tokens, ' + fmtCost(b.cost);
      var g = sv('g', { 'data-tip': tip }, sv('title', null, tip));
      g.append(svText(0, y + 13, tierLabel(k), { class: 'lab' }),
        svText(0, y + 30, fmtCost(b.cost), { class: 'val' }),
        sv('rect', { class: 'track', x: labW, y: y + 11, width: barW, height: 12, rx: 6 }),
        sv('rect', { class: 'fill-' + tierKey(k), x: labW, y: y + 11, width: Math.max(b.tasks ? 6 : 0, barW * b.tasks / total), height: 12, rx: 6 }),
        svText(labW + barW + 10, y + 21, b.tasks + ' · ' + Math.round(pct) + '%', { class: 'val' }));
      svg.append(g);
      rows.push([tierLabel(k), String(b.tasks), nPct.format(pct) + '%', fmtInt(b.tokens), fmtCost(b.cost)]);
    });
    bindTip(svg);
    el.replaceChildren(svg, srTable('Tier distribution', ['Tier', 'Tasks', 'Share', 'Tokens', 'Cost'], rows));
  }

  function dayAxis(g, days, ml, band, base, W, todayK) {
    var n = days.length, every = Math.max(1, Math.ceil(n / Math.floor((W - ml) / 52)));
    days.forEach(function (d, i) {
      var isToday = d.date === todayK;
      if (i % every && !isToday) return;
      var dd = keyToDate(d.date);
      var lab = n <= 8 ? fWeekday.format(dd) : fDayShort.format(dd);
      g.append(svText(ml + i * band + band / 2, base + 16, lab, { 'text-anchor': 'middle', class: isToday ? 'today-lab' : null }));
    });
  }
  function seriesDays() {
    var asc = D.ov.daily.slice().reverse();
    var capped = asc.length > 90;
    return { days: capped ? asc.slice(-90) : asc, capped: capped };
  }
  function drawBars(sel, o) {
    var el = $(sel + ' .chart'), sd = seriesDays(), days = sd.days, todayK = todayKey();
    var W = chartWidth(el), H = 190, ml = 46, mr = 6, mt = 12, mb = 26, pw = W - ml - mr, ph = H - mt - mb;
    var n = days.length, band = pw / n;
    var max = 0;
    days.forEach(function (d) { o.series.forEach(function (s) { max = Math.max(max, s.get(d) || 0); }); });
    var top = niceMax(max);
    var svg = sv('svg', { width: W, height: H, viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': o.summary(days) });
    [0, .5, 1].forEach(function (f) {
      var y = mt + ph - ph * f;
      svg.append(sv('line', { class: f ? 'grid' : 'axis', x1: ml, x2: W - mr, y1: y, y2: y }),
        svText(ml - 6, y + 4, o.fmtAxis(top * f), { 'text-anchor': 'end' }));
    });
    var bw = Math.max(2, Math.min(18, (band - 6) / o.series.length));
    days.forEach(function (d, i) {
      var x0 = ml + i * band, isToday = d.date === todayK;
      if (isToday) svg.append(sv('rect', { class: 'today-band', x: x0, y: mt - 4, width: band, height: ph + 4, rx: 4 }));
      var gx = x0 + (band - bw * o.series.length) / 2;
      o.series.forEach(function (s, j) {
        var v = s.get(d) || 0, bh = top ? ph * v / top : 0;
        if (v > 0) svg.append(sv('rect', { class: s.cls, x: gx + j * bw, y: mt + ph - Math.max(1, bh), width: Math.max(1, bw - (bw > 4 ? 1.5 : 0)), height: Math.max(1, bh), rx: bw > 6 ? 2.5 : 0 }));
        else svg.append(sv('line', { class: 'zero', x1: gx + j * bw, x2: gx + (j + 1) * bw - (bw > 4 ? 1.5 : 0), y1: mt + ph - 1, y2: mt + ph - 1 }));
        if (o.valueLabels && n <= 14 && v > 0 && j === 0) svg.append(svText(gx + j * bw + bw / 2, mt + ph - bh - 4, String(v), { class: 'val', 'text-anchor': 'middle' }));
      });
      var tip = (isToday ? 'Today · ' : '') + fDayLong.format(keyToDate(d.date)) + ': ' + o.tipFor(d);
      svg.append(sv('rect', { class: 'hit', x: x0, y: mt - 4, width: band, height: ph + 4, 'data-tip': tip }, sv('title', null, tip)));
    });
    var ax = sv('g');
    dayAxis(ax, days, ml, band, mt + ph, W, todayK);
    svg.append(ax);
    bindTip(svg);
    var kids = [svg, srTable(o.caption, o.head, days.map(o.rowsFor))];
    if (sd.capped) kids.push(h('p', { class: 'note' }, 'Showing the last 90 days of the range.'));
    el.replaceChildren.apply(el, kids);
  }
  function drawCost() {
    var o = D.ov, t = o.totals, ld = leadOf(t);
    var series = [{ cls: 'fill-actual', get: function (d) { return d.cost; } }, { cls: 'fill-base', get: function (d) { return d.baseline_cost; } }];
    if (ld.has) series.push({ cls: 'fill-lead', get: function (d) { return d.lead_cost; } });
    $('#g-cost .legend-lead').hidden = !ld.has;
    drawBars('#g-cost', {
      series: series,
      fmtAxis: function (v) { return v === 0 ? '$0' : axisCost.format(v); },
      summary: function (days) {
        var today = days.filter(function (d) { return d.date === todayKey(); })[0];
        return 'Cost per day over ' + days.length + ' days, router actual versus all-Opus baseline. Range total ' + fmtCost(t.cost) + ' actual, ' + fmtCost(t.baseline_cost) + ' baseline.' + (today ? ' Today ' + fmtCost(today.cost) + ' actual, ' + fmtCost(today.baseline_cost) + ' baseline.' : '') +
          (ld.has ? ' A third muted bar shows the lead session cost, not part of the comparison: range total ' + fmtCost(ld.cost) + (today ? ', today ' + fmtCost(today.lead_cost) : '') + '.' : '');
      },
      tipFor: function (d) { return 'actual ' + fmtCost(d.cost) + ' · all-Opus ' + fmtCost(d.baseline_cost) + (ld.has ? ' · lead session ' + fmtCost(d.lead_cost) : '') + ' · ' + d.tasks + ' tasks'; },
      caption: 'Cost per day', head: ld.has ? ['Date', 'Actual cost', 'All-Opus cost', 'Lead session cost', 'Tasks'] : ['Date', 'Actual cost', 'All-Opus cost', 'Tasks'],
      rowsFor: function (d) { return ld.has ? [d.date, fmtCost(d.cost), fmtCost(d.baseline_cost), fmtCost(d.lead_cost), String(d.tasks)] : [d.date, fmtCost(d.cost), fmtCost(d.baseline_cost), String(d.tasks)]; }
    });
  }
  function drawTasksPerDay() {
    var t = D.ov.totals;
    drawBars('#g-tasks', {
      series: [{ cls: 'fill-tasks', get: function (d) { return d.tasks; } }], valueLabels: true,
      fmtAxis: function (v) { return nInt.format(Math.round(v * 10) / 10); },
      summary: function (days) { return 'Tasks per day over ' + days.length + ' days. ' + t.tasks + ' tasks in total.'; },
      tipFor: function (d) { return d.tasks + ' tasks · ' + fmtTok(d.tokens) + ' tokens · ' + fmtCost(d.cost); },
      caption: 'Tasks per day', head: ['Date', 'Tasks', 'Tokens', 'Cost'],
      rowsFor: function (d) { return [d.date, String(d.tasks), fmtInt(d.tokens), fmtCost(d.cost)]; }
    });
  }
  function renderCharts() {
    if (!D.ov) return;
    drawTier(); drawCost(); drawTasksPerDay();
  }

  // ------------------------------------------------------------------ savings
  function renderSavings() {
    if (!D.ov) return;
    var t = D.ov.totals, p = D.pricing || {};
    var neg = t.saved < 0;
    var mult = p.baseline_token_multiplier;
    var warns = D.health && Array.isArray(D.health.warnings) ? D.health.warnings.map(String) : [];
    var bt = p.baseline_tier ? tierLabel(p.baseline_tier) : 'Opus';
    var frac = t.baseline_cost > 0 ? Math.max(0, Math.min(100, t.cost / t.baseline_cost * 100)) : 0;
    var el = $('#savings');
    var pctText = nPct.format(Math.abs(t.saved_pct)) + '% ' + (neg ? 'more' : 'cheaper') + ' than all-Opus';
    var pctAriaLabel = 'Router cost is ' + nPct.format(Math.abs(t.saved_pct)) + '% ' + (neg ? 'higher' : 'lower') + ' than the same tokens on Opus';
    el.replaceChildren.apply(el, [
      h('div', { class: 'sv-row' },
        h('div', { class: 'sv-big' + (neg ? ' neg' : '') },
          h('span', { class: 'lab' }, neg ? 'Extra vs all-Opus' : 'Saved vs all-Opus'),
          h('span', { class: 'amt' }, fmtCost(Math.abs(t.saved))),
          h('span', { class: 'pct', title: pctAriaLabel }, pctText)),
        h('div', { class: 'sv-cmp' },
          h('span', null, h('i', { class: 'sw sw-actual' }), 'Router actual ', h('b', { class: 'act' }, fmtCost(t.cost))),
          h('span', null, h('i', { class: 'sw sw-base' }), 'All-Opus ', h('b', { class: 'base' }, fmtCost(t.baseline_cost))))),
      h('div', { class: 'sv-bar', role: 'img', 'aria-label': 'Actual cost is ' + Math.round(frac) + ' percent of the all-Opus baseline' }, h('i', { style: 'width:' + frac + '%' })),
      h('div', { class: 'sv-notes' },
        t.legacy_tasks > 0 ? h('p', { class: 'sv-foot sv-legacy', title: LEGACY_TIP }, fmtInt(t.legacy_tasks) + ' imported legacy tasks (' + fmtInt(t.legacy_tokens) + ' tokens) are not priced.') : null,
        h('p', { class: 'sv-foot' }, 'Baseline = same tokens priced at Opus rates x multiplier; an estimate.' +
          (p.updated ? ' Prices dated ' + p.updated + '.' : '') + (bt && p.baseline_tier && p.baseline_tier !== 'opus' ? ' Baseline tier: ' + bt + '.' : '') +
          (leadOf(t).has ? ' Lead session (' + fmtCost(leadOf(t).cost) + ') is not part of the routing comparison.' : '')),
        h('p', { class: 'sv-foot sv-mult' }, 'Multiplier ', h('b', null, fmtMult(mult)), ' (' + multSource(p.baseline_source) + ')',
          isNum(mult) && mult !== 1 ? [' ', h('span', { class: 'chip adjusted-badge', title: 'The baseline multiplier is not 1.0, so the savings figure is adjusted' }, 'adjusted')] : null,
          '. To change it, run ', h('code', null, MULT_CMD), ' (any number above 0; 1.0 compares token for token).'),
        warns.length ? h('p', { class: 'sv-foot sv-warn' }, 'Configuration warning: ' + warns.join(' ')) : null)
    ]);
  }

  // ------------------------------------------------------------------ task feed
  function chainsOf(items) {
    var by = {};
    items.forEach(function (t) { if (t.tool_use_id) (by[t.tool_use_id] = by[t.tool_use_id] || []).push(t); });
    Object.keys(by).forEach(function (k) {
      if (by[k].length < 2) { delete by[k]; return; }
      by[k].sort(function (a, b) { return (Date.parse(a.started_at) || 0) - (Date.parse(b.started_at) || 0) || String(a.task_id).localeCompare(String(b.task_id)); });
    });
    return by;
  }
  function domId(prefix, s) { return prefix + String(s).replace(/[^A-Za-z0-9_-]/g, '_'); }
  function taskCard(t, ctx) {
    var sel = S.task === t.task_id;
    var tk = tierKey(t.tier), st = stKey(t.status);
    var label = t.label || t.agent_type || '(no label)';
    var known = t.cost_known && t.cost;
    var tokKnown = t.cost_known || t.tokens_total > 0;
    var el = h('div', { class: 'task t-' + tk + (st === 'stale' ? ' is-muted' : ''), role: 'option', id: domId('task-', t.task_id),
      'aria-selected': sel ? 'true' : 'false', tabindex: '-1', dataset: { id: t.task_id } });
    el.append(h('span', { class: 'st-ic s-' + st, title: statusTip(t), 'aria-hidden': 'true' }, GLYPH[st]));

    var showChip = st !== 'pass' || t.status_source === 'inferred';
    var title = h('div', { class: 't-title' + (t.label ? '' : ' nolabel'), title: label },
      showChip ? statusChip(t.status, t.status_source, t) : h('span', { class: 'sr' }, WORD[st] + ': '), label);

    var meta = h('div', { class: 't-meta' });
    if (!S.project) meta.append(projChip(t.project, t.project_path));
    meta.append(h('span', null, tierChip(t.tier, t.model), t.tier === 'unknown' && t.model ? h('span', { class: 'mono' }, shortModel(t.model)) : null));
    meta.append(h('span', null, timeEl(t.started_at, { withDate: !!S.sort })));
    var running = st === 'running' && parseT(t.started_at);
    var dur = h('span', { class: 'num' + (running ? ' live-dur' : ''), title: running ? 'Elapsed, live' : 'Duration' }, fmtDur(t.duration_ms));
    if (running) dur.dataset.start = String(parseT(t.started_at).getTime());
    meta.append(h('span', null, dur));
    if (t.agent_type) meta.append(h('span', { class: 't-agent' }, t.agent_type));
    var chain = t.tool_use_id && ctx.chains[t.tool_use_id];
    if (chain) {
      var idx = chain.indexOf(t);
      meta.append(h('span', { class: 'chain-badge', title: 'Escalation chain, attempt ' + (idx + 1) + ' of ' + chain.length },
        chain.map(function (c) { return cap(c.tier); }).join(' → ')));
    }
    if (st === 'stale' && t.stale_reason) meta.append(h('span', { class: 't-reason' }, t.stale_reason));
    if (t.legacy) meta.append(legacyBadge());

    var side = h('div', { class: 't-side' });
    if (st !== 'running') side.append(h('span', { class: 't-cost' + (known ? '' : ' dim') }, known ? fmtCost(t.cost.total) : DASH));
    side.append(h('span', { class: 't-tok', title: tokKnown ? fmtInt(t.tokens_total) + ' tokens' : (st === 'running' ? 'Tokens and cost are logged when the task ends' : null) },
      tokKnown ? fmtTok(t.tokens_total) + ' tok' : (st === 'running' ? 'cost at end' : DASH)));
    if (known && ctx.max > 0) {
      side.append(h('span', { class: 't-bar', 'aria-hidden': 'true' }, h('i', { style: 'width:' + Math.max(2, t.cost.total / ctx.max * 100).toFixed(1) + '%' })));
    }
    if (isNum(t.saved) && t.saved >= 0.005) side.append(h('span', { class: 't-saved' }, 'saved ' + fmtCost(t.saved) + ' vs ' + baselineName()));
    el.append(h('div', { class: 't-main' }, title, meta), side);
    return el;
  }
  var listSig = '';
  function renderList() {
    var list = $('#list'), st = $('#list-state'), more = $('#more'), cnt = $('#list-count');
    var T = D.tasks;
    if (!T) { if (!D.ov) return; list.setAttribute('aria-busy', 'true'); list.classList.add('stale'); return; }
    list.removeAttribute('aria-busy'); list.classList.remove('stale');
    var sig = JSON.stringify([T.items, T.total, S.task, S.sort, S.project, UI.collapsed, UI.density, todayKey(), D.ov && D.ov.daily, D.pricing && D.pricing.updated]);
    if (sig === listSig && list.childNodes.length) return;
    listSig = sig;
    var focusId = document.activeElement && list.contains(document.activeElement) && document.activeElement.dataset ? document.activeElement.dataset.id : null;
    var focusDay = document.activeElement && document.activeElement.classList && document.activeElement.classList.contains('day-t') ? document.activeElement.dataset.date : null;
    st.hidden = true;
    if (!T.items.length) {
      list.replaceChildren();
      st.hidden = false;
      var msg;
      if (hasTaskFilters()) msg = [h('b', null, 'No tasks match these filters'), 'Try another status, tier or search.', h('br'), h('button', { type: 'button', class: 'btn', onclick: clearTaskFilters }, 'Clear filters')];
      else if (D.health && D.health.tasks === 0) msg = [h('b', null, 'No tasks yet'), 'Run a routed task in Claude Code and it appears here live.'];
      else msg = [h('b', null, 'No tasks in this range'), 'Pick a longer range, or run a routed task in Claude Code and it appears here live.'];
      st.replaceChildren();
      append(st, [h('span', { class: 'state-ic' }, icon('list')), msg]);
      more.hidden = true;
      cnt.textContent = '0 tasks';
      return;
    }
    var ctx = { chains: chainsOf(T.items), max: 0 };
    T.items.forEach(function (t) { if (t.cost_known && t.cost && t.cost.total > ctx.max) ctx.max = t.cost.total; });
    var days = {};
    (D.ov && D.ov.daily || []).forEach(function (d) { days[d.date] = d; });
    var nodes = [], todayK = todayKey();
    var groups = [];
    if (S.sort) {
      var lab = { cost: 'Most expensive first', duration: 'Longest first', tokens: 'Most tokens first' }[S.sort];
      groups.push({ key: 'sorted', label: lab, items: T.items });
    } else {
      T.items.forEach(function (t) {
        var k = t.date || '';
        if (!groups.length || groups[groups.length - 1].key !== k) groups.push({ key: k, items: [] });
        groups[groups.length - 1].items.push(t);
      });
    }
    var first = true;
    groups.forEach(function (g) {
      var hid = domId('dh-', g.key), lid = domId('dl-', g.key);
      var collapsed = !S.sort && !!UI.collapsed[g.key];
      var p = S.sort ? { name: g.label, date: '' } : dayParts(g.key);
      var di = days[g.key];
      var summary = h('span', { class: 'day-s' });
      if (!S.sort && di && !hasTaskFilters()) {
        var saved = (di.baseline_cost || 0) - (di.cost || 0);
        append(summary, [h('span', null, h('b', null, fmtInt(di.tasks)), di.tasks === 1 ? ' task' : ' tasks'),
          h('span', null, h('b', null, fmtCost(di.cost))),
          saved > 0.005 ? h('span', { class: 'ok' }, 'saved ', h('b', null, fmtCost(saved))) : null]);
      } else {
        summary.append(h('span', null, h('b', null, fmtInt(g.items.length)), ' shown'));
      }
      var head = S.sort
        ? h('h3', { class: 'day-t', id: hid }, h('span', { class: 'dname' }, p.name))
        : h('h3', null, h('button', { type: 'button', class: 'day-t', id: hid, 'aria-expanded': collapsed ? 'false' : 'true', 'aria-controls': lid, dataset: { date: g.key },
          title: collapsed ? 'Expand this day' : 'Collapse this day' },
          icon('chev', 'chev'), h('span', { class: 'dname' }, p.name), p.date ? h('span', { class: 'muted' }, p.date) : null));
      var box = h('div', { class: 'day-items', role: 'listbox', id: lid, 'aria-labelledby': hid, hidden: collapsed });
      if (!collapsed) g.items.forEach(function (t) { box.append(taskCard(t, ctx)); });
      nodes.push(h('section', { class: 'day' + (g.key === todayK ? ' today' : '') }, h('div', { class: 'day-h' }, head, summary), box));
      first = false;
    });
    list.replaceChildren.apply(list, nodes);
    // roving tabindex: the selected (or first visible) task is the single tab stop
    var opts = $$('.task', list);
    var tab = opts.filter(function (o) { return o.dataset.id === (focusId || S.task); })[0] || opts[0];
    if (tab) tab.tabIndex = 0;
    more.hidden = T.items.length >= T.total;
    more.textContent = 'Load more (' + fmtInt(Math.min(100, T.total - T.items.length)) + ' of ' + fmtInt(T.total - T.items.length) + ' remaining)';
    cnt.textContent = T.items.length < T.total ? 'Showing ' + fmtInt(T.items.length) + ' of ' + fmtInt(T.total) : fmtInt(T.total) + (T.total === 1 ? ' task' : ' tasks');
    if (focusId) { var f = $('#' + domId('task-', focusId)); if (f) f.focus({ preventScroll: true }); }
    if (focusDay != null) { var fd = list.querySelector('.day-t[data-date="' + focusDay + '"]'); if (fd) fd.focus({ preventScroll: true }); }
    liveTick();
  }
  function clearTaskFilters() { S.status = ''; S.tier = ''; S.q = ''; S.router = ''; S.session = ''; changed(); }

  function isWide() { return window.matchMedia ? matchMedia('(min-width:1100px)').matches : true; }
  function markSelected() {
    $$('#list .task').forEach(function (r) { r.setAttribute('aria-selected', String(r.dataset.id === S.task)); });
  }
  function selectTask(id, opts) {
    opts = opts || {};
    var same = S.task === id && D.detail && D.detail.task_id === id;
    S.task = id;
    writeHash();
    markSelected();
    UI.sheet = true;
    syncSheet(!opts.keepFocus);
    if (same) return;
    var my = ++detailSeq;
    D.detail = null; D.detailErr = null; detailSig = '';
    $('#detail-body').replaceChildren(h('span', { class: 'sk sk-line', style: 'width:70%;height:18px' }), h('span', { class: 'sk sk-line', style: 'width:40%' }),
      h('span', { class: 'sk', style: 'height:64px;margin:16px 0' }), h('span', { class: 'sk sk-line' }), h('span', { class: 'sk sk-line', style: 'width:85%' }), h('span', { class: 'sk sk-line', style: 'width:60%' }));
    $('#detail-close').hidden = false;
    api('api/tasks/' + encodeURIComponent(id)).then(function (t) {
      if (my !== detailSeq) return;
      D.detail = t; renderDetail();
    }, function (e) {
      if (my !== detailSeq) return;
      D.detailErr = e.message; renderDetail();
    });
  }
  function closeDetail() {
    var id = S.task;
    S.task = ''; D.detail = null; D.detailErr = null; detailSig = '';
    writeHash();
    markSelected();
    UI.sheet = false;
    syncSheet(false);
    renderDetail();
    var r = id && $('#' + domId('task-', id));
    if (r) { $$('#list .task').forEach(function (o) { o.tabIndex = -1; }); r.tabIndex = 0; r.focus({ preventScroll: !isWide() ? false : true }); }
  }
  // narrow screens: the details panel is a bottom sheet (dialog); wide screens: a sticky side panel
  function syncSheet(moveFocus) {
    var col = $('#details'), bd = $('#backdrop'), wide = isWide();
    var open = !wide && UI.sheet && !!S.task && S.tab === 'overview';
    col.classList.toggle('open', open);
    if (!wide) {
      col.setAttribute('role', 'dialog');
      col.setAttribute('aria-modal', 'true');
      col.setAttribute('aria-labelledby', 'details-h');
    } else {
      col.removeAttribute('role'); col.removeAttribute('aria-modal'); col.removeAttribute('aria-labelledby');
    }
    bd.hidden = !open;
    if (open) requestAnimationFrame(function () { bd.classList.add('show'); });
    else bd.classList.remove('show');
    document.body.style.overflow = open ? 'hidden' : '';
    if (open && moveFocus) setTimeout(function () { $('#details-h').focus({ preventScroll: true }); }, 30);
  }

  // ------------------------------------------------------------------ details + pricing
  function dl(pairs) {
    var el = h('dl', { class: 'dl' });
    pairs.forEach(function (p) { if (p) el.append(h('dt', null, p[0]), h('dd', null, p[1])); });
    return el;
  }
  var detailSig = '';
  function renderDetail() {
    var body = $('#detail-body'), t = D.detail;
    $('#detail-close').hidden = !S.task;
    var sig = JSON.stringify([S.task, t, D.detailErr, D.pricing && D.pricing.updated, D.health && D.health.tasks === 0]);
    if (sig === detailSig && body.childNodes.length) return;
    detailSig = sig;
    if (S.task && D.detailErr && !t) {
      body.replaceChildren(h('div', { class: 'd-empty' }, h('b', null, 'Task not available'), 'Task ' + S.task + ' could not be loaded (' + D.detailErr + ').', h('br'),
        h('button', { type: 'button', class: 'btn', style: 'margin-top:12px', onclick: closeDetail }, 'Dismiss')));
      return;
    }
    if (!t) {
      if (S.task) return; // loading
      var kids = [h('div', { class: 'd-empty' }, h('span', { class: 'state-ic' }, icon('panel')), h('b', null, 'No task selected'),
        'Select a task to see its full title, result, escalation chain and cost breakdown.',
        h('div', { class: 'd-keys' }, h('span', null, h('kbd', { class: 'kbd' }, '↑'), h('kbd', { class: 'kbd' }, '↓'), 'move'), h('span', null, h('kbd', { class: 'kbd' }, 'Enter'), 'open'),
          h('span', null, h('kbd', { class: 'kbd' }, 'Esc'), 'close'), h('span', null, h('kbd', { class: 'kbd' }, '/'), 'search')))];
      if (D.health && D.health.tasks === 0) {
        kids.push(h('div', { class: 'help' }, h('h3', null, 'First run'),
          h('p', null, 'TokenTier has not logged any tasks yet.'),
          h('ol', null, h('li', null, 'Keep this dashboard running.'), h('li', null, 'Run a routed task in Claude Code (a fast-, mid- or deep-worker).'), h('li', null, 'It appears in the list live, no reload needed.'))));
      }
      body.replaceChildren.apply(body, kids);
      return;
    }
    var keepScroll = $('#detail').scrollTop;
    var rawOpen = !!$('#detail-body details.raw[open]');
    var p = D.pricing && D.pricing.models || {};
    var spec = t.pricing_key ? p[t.pricing_key] : null;
    var chain = [t].concat(t.same_tool_use || []);
    chain.sort(function (a, b) { return (Date.parse(a.started_at) || 0) - (Date.parse(b.started_at) || 0) || String(a.task_id).localeCompare(String(b.task_id)); });
    var st = stKey(t.status);
    var k = [];
    k.push(h('div', { class: t.label ? 'd-title' : 'd-title nolabel' }, t.label || t.agent_type || '(no label)'),
      h('div', { class: 'd-chips' }, statusChip(t.status, t.status_source, t), tierChip(t.tier, t.model),
        h('span', { class: 'chip t-other' }, t.router_worker ? 'router worker' : 'not a router worker'), t.legacy ? legacyBadge() : null));
    var known = t.cost_known && t.cost;
    k.push(h('div', { class: 'd-money' },
      h('div', null, h('span', null, 'Cost'), h('b', null, known ? fmtCost(t.cost.total) : DASH)),
      h('div', null, h('span', null, 'On ' + baselineName()), h('b', null, fmtCost(t.baseline_cost))),
      h('div', { class: isNum(t.saved) && t.saved > 0 ? 'ok' : null }, h('span', null, 'Saved'), h('b', null, fmtCost(t.saved)))));
    var running = st === 'running' && parseT(t.started_at);
    var durEl = h('span', { class: 'num' }, fmtDur(t.duration_ms));
    if (running) durEl.dataset.start = String(parseT(t.started_at).getTime());
    k.push(dl([
      ['Project', h('span', null, projChip(t.project, null), t.project_path ? h('span', { class: 'path' }, t.project_path) : null)],
      ['Session', t.session_id ? h('button', { type: 'button', class: 'link mono', title: 'Filter tasks by session ' + t.session_id, onclick: function () { S.session = t.session_id; S.tab = 'overview'; changed(); $('#feed-h').focus(); } }, shortSid(t.session_id)) : DASH],
      ['Model', h('span', { class: 'mono' }, txt(t.model))],
      ['Agent type', txt(t.agent_type)],
      [st === 'stale' ? 'Stale' : 'Status source', st === 'stale' ? (t.stale_reason || 'no recent activity') : txt(statusSourceText(t))],
      ['Started', fmtFull(t.started_at)],
      ['Ended', t.ended_at ? fmtFull(t.ended_at) : (st === 'running' ? 'still running' : DASH)],
      t.last_activity ? ['Last activity', fmtFull(t.last_activity)] : null,
      ['Duration', durEl],
      ['Task id', h('span', { class: 'mono' }, txt(t.task_id))]
    ]));
    k.push(h('div', { class: 'd-h' }, 'Escalation chain'));
    if (chain.length > 1) {
      var ch = h('ol', { class: 'chain' });
      chain.forEach(function (c) {
        var cur = c.task_id === t.task_id;
        ch.append(h('li', { class: 'step t-' + tierKey(c.tier), 'aria-current': cur ? 'step' : null },
          h('span', { class: 'tdot', 'aria-hidden': 'true' }),
          h('button', { type: 'button', title: cur ? 'This attempt' : 'Open this attempt', onclick: function () { if (!cur) selectTask(c.task_id, { keepFocus: true }); } },
            tierChip(c.tier, c.model), statusChip(c.status, c.status_source, c),
            h('span', { class: 'when' }, fmtTime(c.started_at) + ' · ' + (c.cost_known && c.cost ? fmtCost(c.cost.total) : DASH)))));
      });
      k.push(ch);
    } else {
      k.push(h('p', { class: 'note' }, 'No escalation: a single attempt on ' + tierLabel(t.tier) + '.'));
    }
    var tk = t.tokens || {}, cs = t.cost || {};
    var rows = [['Input', tk.input, spec && spec.input, cs.input], ['Output', tk.output, spec && spec.output, cs.output],
      ['Cache write', tk.cache_creation, spec && spec.cache_write, cs.cache_write], ['Cache read', tk.cache_read, spec && spec.cache_read, cs.cache_read]];
    k.push(h('div', { class: 'd-h' }, 'Token breakdown'));
    k.push(h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' },
      h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Type'), h('th', { class: 'r', scope: 'col' }, 'Tokens'), h('th', { class: 'r', scope: 'col' }, '$/MTok'), h('th', { class: 'r', scope: 'col' }, 'Cost'))),
      h('tbody', null, rows.map(function (r) {
        return h('tr', null, h('td', { class: 'strong' }, r[0]), h('td', { class: 'r' }, fmtInt(r[1])),
          h('td', { class: 'r' }, isNum(r[2]) ? nRate.format(r[2]) : DASH), h('td', { class: 'r' }, fmtCost(r[3])));
      })),
      h('tfoot', null, h('tr', null, h('td', null, 'Total'), h('td', { class: 'r' }, fmtInt(t.tokens_total)), h('td', { class: 'r' }, spec ? tierLabel(t.pricing_key) : DASH), h('td', { class: 'r' }, t.cost_known && cs ? fmtCost(cs.total) : DASH))))));
    if (t.legacy) k.push(h('p', { class: 'note' }, legacyBadge(), ' Imported from the old router-kit log: only a single token total (' + fmtInt(t.legacy_tokens_total) + ') was recorded, with no input/output/cache breakdown, so no cost estimate is shown and this task is excluded from cost and savings totals.'));
    else if (!t.cost_known) k.push(h('p', { class: 'note' }, st === 'running' ? 'Cost appears when the task ends.' : 'Cost unavailable: missing token data or an unknown model.'));
    var mult = D.pricing && D.pricing.baseline_token_multiplier;
    if (isNum(mult) && mult !== 1) k.push(h('p', { class: 'note' }, 'Baseline uses token multiplier ' + fmtMult(mult) + '.'));
    k.push(h('div', { class: 'd-h' }, 'Result'), h('pre', { class: 'result' }, t.result == null || t.result === '' ? (st === 'running' ? 'Running, no result yet.' : DASH) : t.result));
    var evs = t.events || [];
    if (evs.length) k.push(h('details', { class: 'raw', open: rawOpen }, h('summary', null, 'Raw log events (' + evs.length + ')'), h('pre', null, JSON.stringify(evs, null, 2))));
    body.replaceChildren.apply(body, k);
    $('#detail').scrollTop = keepScroll;
    liveTick();
  }

  function renderPricing() {
    var el = $('#pricing-body'), p = D.pricing;
    if (!p || !p.models) { el.replaceChildren(h('p', { class: 'note' }, 'Pricing unavailable.')); return; }
    var keys = Object.keys(p.models);
    var cur = D.detail && D.detail.pricing_key;
    el.replaceChildren(
      h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' },
        h('caption', { class: 'sr' }, 'Price per million tokens in US dollars'),
        h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Tier'), h('th', { class: 'r', scope: 'col' }, 'Input'), h('th', { class: 'r', scope: 'col' }, 'Output'), h('th', { class: 'r', scope: 'col' }, 'Cache write'), h('th', { class: 'r', scope: 'col' }, 'Cache read'))),
        h('tbody', null, keys.map(function (k) {
          var m = p.models[k];
          return h('tr', { class: k === cur ? 'sel' : null },
            h('td', { class: 'strong' }, tierChip(k)),
            ['input', 'output', 'cache_write', 'cache_read'].map(function (f) { return h('td', { class: 'r' }, isNum(m[f]) ? nRate.format(m[f]) : DASH); }));
        })))),
      h('p', { class: 'note' }, 'US$ per million tokens (MTok). Updated ' + txt(p.updated) + '. Source: ' + txt(p.source)),
      h('p', { class: 'note' }, 'All-Opus baseline: ' + (p.baseline_tier ? tierLabel(p.baseline_tier) : DASH) + ', token multiplier ' + fmtMult(p.baseline_token_multiplier) + ' (' + multSource(p.baseline_source) + '). An estimate.'));
  }

  // ------------------------------------------------------------------ projects
  function visibleProjects() {
    // pseudo projects (no tasks, no lead usage) are hidden
    return (D.projects || []).filter(function (p) { return p.tasks > 0 || p.lead_tokens > 0 || p.lead_cost > 0 || p.project === S.project; });
  }
  function sparkline(daily) {
    var vals = (daily || []).map(function (d) { return (d.cost || 0) + (d.lead_cost || 0); });
    var n = vals.length, W = 280, H = 44, pad = 3;
    var max = Math.max.apply(null, vals.concat([0]));
    var total = vals.reduce(function (a, b) { return a + b; }, 0);
    var peakI = vals.indexOf(max);
    var label = 'Daily spend, last ' + n + ' days: total ' + fmtCost(total) + (max > 0 ? ', peak ' + fmtCost(max) + ' on ' + fDayShort.format(keyToDate(daily[peakI].date)) : ', no spend') + '.';
    var svg = sv('svg', { class: 'spark', viewBox: '0 0 ' + W + ' ' + H, preserveAspectRatio: 'none', role: 'img', 'aria-label': label }, sv('title', null, label));
    var x = function (i) { return n > 1 ? i * (W / (n - 1)) : W / 2; };
    var y = function (v) { return max > 0 ? H - pad - (v / max) * (H - 2 * pad) : H - pad; };
    var pts = vals.map(function (v, i) { return x(i).toFixed(1) + ',' + y(v).toFixed(1); });
    svg.append(sv('line', { class: 'base', x1: 0, x2: W, y1: H - pad + .5, y2: H - pad + .5, 'vector-effect': 'non-scaling-stroke' }));
    if (max > 0) {
      svg.append(sv('path', { class: 'area', d: 'M0,' + (H - pad) + ' L' + pts.join(' L') + ' L' + W + ',' + (H - pad) + ' Z' }),
        sv('path', { class: 'line', d: 'M' + pts.join(' L'), 'vector-effect': 'non-scaling-stroke' }));
    }
    return { svg: svg, total: total };
  }
  function statusRow(sc, running) {
    var row = h('div', { class: 'sc-row' });
    var items = [['pass', 'passed'], ['fail', 'failed'], ['escalated', 'escalated']];
    items.forEach(function (it) {
      row.append(h('span', { class: 's-' + it[0] }, h('span', { class: 'g', 'aria-hidden': 'true' }, GLYPH[it[0]]), ' ', h('b', null, fmtInt(sc && sc[it[0]] || 0)), ' ' + it[1]));
    });
    var stale = (sc && sc.stale || 0) + (sc && sc.unknown || 0);
    if (stale) row.append(h('span', { class: 's-stale' }, h('span', { class: 'g', 'aria-hidden': 'true' }, GLYPH.stale), ' ', h('b', null, fmtInt(stale)), ' stale / unverified'));
    return row;
  }
  function mixLegend(mix) {
    var keys = ['haiku', 'sonnet', 'opus', 'unknown'];
    var tot = keys.reduce(function (a, k) { return a + (mix && mix[k] || 0); }, 0);
    var lg = h('div', { class: 'mix-legend' });
    keys.forEach(function (k) {
      if (!mix || !mix[k]) return;
      lg.append(h('span', null, tierDot(k), k === 'unknown' ? 'Other' : cap(k), ' ', h('b', null, Math.round(mix[k] / tot * 100) + '%')));
    });
    if (!tot) lg.append(h('span', null, 'No delegated tasks'));
    return lg;
  }
  function projectCard(p) {
    var on = S.project === p.project;
    var hval = hue(p.project);
    var pick = function () { S.project = on ? '' : p.project; S.session = ''; S.tab = 'overview'; changed(); window.scrollTo(0, 0); };
    var sp = sparkline(p.daily);
    var last = parseT(p.last_active);
    var card = h('article', { class: 'pcard' + (on ? ' sel' : ''), 'aria-labelledby': domId('pn-', p.project) },
      h('div', { class: 'pc-head' },
        h('span', { class: 'avatar', style: '--h:' + hval, 'aria-hidden': 'true' }, (p.project || '?').replace(/[^A-Za-z0-9]/g, '').charAt(0) || '?'),
        h('div', { style: 'min-width:0' },
          h('h3', { class: 'pc-name', id: domId('pn-', p.project) },
            h('button', { type: 'button', class: 'stretch', title: on ? 'Show all projects again' : 'Filter the dashboard to ' + p.project, 'aria-pressed': on ? 'true' : 'false', onclick: pick }, midEllipsis(p.project, 48),
              h('span', { class: 'sr' }, on ? ' (filtering the dashboard; activate to clear)' : ', filter the dashboard'))),
          p.project_path ? h('span', { class: 'pc-path', title: p.project_path }, midEllipsis(p.project_path, p.running ? 22 : 44)) : h('span', { class: 'pc-path' }, 'path unknown')),
        p.running ? h('span', { class: 'runbadge' }, h('span', { class: 'pip', 'aria-hidden': 'true' }), fmtInt(p.running) + ' running') : null),
      h('div', { class: 'stats' },
        h('div', null, h('span', null, 'Tasks'), h('b', null, fmtInt(p.tasks))),
        h('div', null, h('span', null, 'Sessions'), h('b', null, fmtInt(p.sessions))),
        h('div', { title: 'Delegated tasks ' + fmtCost(p.cost) + ' + lead session ' + fmtCost(p.lead_cost) }, h('span', null, 'Spend'), h('b', null, fmtCost(isNum(p.spend) ? p.spend : p.cost))),
        h('div', { class: p.saved > 0 ? 'ok' : null, title: 'vs all-Opus ' + fmtCost(p.baseline_cost) }, h('span', null, 'Saved'), h('b', null, fmtCost(p.saved)))),
      h('div', null, h('div', { class: 'spark-h' }, h('span', null, 'Last 14 days'), h('span', null, h('b', null, fmtCost(sp.total)))), sp.svg),
      h('div', null, mixBar(p.tier_mix), mixLegend(p.tier_mix)),
      statusRow(p.status_counts),
      h('div', { class: 'card-foot' },
        h('span', null, last ? ['Active ', timeEl(p.last_active, { rel: 'always' })] : 'No activity yet',
          p.lead_cost > 0 ? h('span', { title: 'The main session own usage, not part of the routing savings' }, ' · lead ' + fmtCost(p.lead_cost)) : null),
        h('span', { class: 'acts above' },
          h('button', { type: 'button', class: 'btn ghost sm', onclick: function () { S.project = p.project; S.session = ''; S.tab = 'sessions'; changed(); }, 'aria-label': 'Sessions of ' + p.project }, 'Sessions'))));
    card.style.setProperty('--h', hval);
    return card;
  }
  function renderProjects() {
    var el = $('#projects-body');
    if (!D.projects) { el.replaceChildren(h('span', { class: 'sk', style: 'height:320px;border-radius:14px' }), h('span', { class: 'sk', style: 'height:320px;border-radius:14px' })); return; }
    var all = visibleProjects();
    var q = UI.pQ.toLowerCase();
    var L = all.filter(function (p) { return !q || String(p.project).toLowerCase().indexOf(q) >= 0 || String(p.project_path || '').toLowerCase().indexOf(q) >= 0; });
    if (UI.pSort === 'spend') L = L.slice().sort(function (a, b) { return (b.spend || 0) - (a.spend || 0); });
    else if (UI.pSort === 'tasks') L = L.slice().sort(function (a, b) { return b.tasks - a.tasks; });
    $('#p-count').textContent = q ? fmtInt(L.length) + ' of ' + fmtInt(all.length) : fmtInt(all.length) + (all.length === 1 ? ' project' : ' projects');
    if (!L.length) {
      el.replaceChildren(h('div', { class: 'state', style: 'grid-column:1/-1' }, h('span', { class: 'state-ic' }, icon('folder')),
        all.length ? [h('b', null, 'No project matches'), 'Try another search.'] : [h('b', null, 'No projects yet'), 'Run a routed task in Claude Code and it appears here live.']));
      return;
    }
    var focusP = document.activeElement && el.contains(document.activeElement) ? document.activeElement.closest('article') && document.activeElement.closest('article').getAttribute('aria-labelledby') : null;
    el.replaceChildren.apply(el, L.map(projectCard));
    if (focusP) { var f = $('#' + focusP + ' .stretch'); if (f) f.focus({ preventScroll: true }); }
  }

  // ------------------------------------------------------------------ sessions
  function copyText(s, done) {
    var ok = function () { done(true); }, bad = function () { done(false); };
    try {
      if (navigator.clipboard && window.isSecureContext !== false) { navigator.clipboard.writeText(s).then(ok, bad); return; }
    } catch (e) { /* fall through */ }
    bad();
  }
  function sessionCard(s) {
    var on = S.session === s.session_id;
    var pick = function () { S.session = on ? '' : s.session_id; S.tab = 'overview'; changed(); setTimeout(function () { var hd = $('#feed-h'); hd.focus({ preventScroll: true }); hd.scrollIntoView({ block: 'start' }); }, 0); };
    var start = parseT(s.started_at), end = parseT(s.ended_at || s.last_active);
    var dur = start && end ? end - start : null;
    var short = shortSid(s.session_id);
    var stateWord = { active: 'Active', idle: 'Idle', finished: 'Finished' }[s.state] || 'Finished';
    var copyBtn = h('button', { type: 'button', class: 'sid above', title: 'Copy full session id ' + s.session_id, 'aria-label': 'Copy session id ' + short },
      h('span', null, short), icon('copy'));
    copyBtn.addEventListener('click', function () {
      copyText(s.session_id, function (ok) {
        announce(ok ? 'Session id copied' : 'Copy failed: ' + s.session_id);
        copyBtn.firstChild.textContent = ok ? 'copied' : short;
        setTimeout(function () { copyBtn.firstChild.textContent = short; }, 1200);
      });
    });
    var prev = s.preview_label;
    return h('article', { class: 'scard' + (on ? ' sel' : ''), 'aria-label': 'Session ' + short },
      h('div', { class: 'sc-head' }, copyBtn,
        h('span', { class: 'state-b ' + (s.state || 'finished') }, s.state === 'active' ? h('span', { class: 'pip', 'aria-hidden': 'true' }) : null, stateWord, s.running ? ' · ' + s.running + ' running' : '')),
      h('div', { class: 'sc-when' }, S.project ? null : projChip(s.project, null),
        h('span', { class: 'num', title: fmtFull(s.started_at) + ' – ' + (s.ended_at ? fmtFull(s.ended_at) : s.state === 'active' ? 'now' : 'last activity ' + fmtFull(s.last_active)) },
          fmtTime(s.started_at) + ' – ' + (s.state === 'active' && !s.ended_at ? 'now' : fmtTime(s.ended_at || s.last_active))),
        dur != null ? h('span', { class: 'num' }, fmtDur(dur)) : null),
      h('div', { style: 'min-width:0' },
        h('span', { class: 'sc-prev-l' }, s.tasks ? 'Latest task' : 'Lead session only'),
        h('button', { type: 'button', class: 'stretch sc-prev' + (prev ? '' : ' none'), title: prev || null, 'aria-pressed': on ? 'true' : 'false', onclick: pick },
          prev || 'No delegated tasks', h('span', { class: 'sr' }, on ? ' (filtering tasks; activate to clear)' : ', show this session in the task list'))),
      h('div', { class: 'stats' },
        h('div', null, h('span', null, 'Tasks'), h('b', null, fmtInt(s.tasks)), s.tasks ? mixBar(s.tier_mix, 'sm') : null),
        h('div', { title: 'Delegated tasks' + (s.top_label ? '. Most expensive: ' + s.top_label + ' (' + fmtCost(s.top_cost) + ')' : '') }, h('span', null, 'Spend'), h('b', null, fmtCost(s.cost))),
        h('div', { class: s.saved > 0 ? 'ok' : null }, h('span', null, 'Saved'), h('b', null, fmtCost(s.saved))),
        h('div', { title: s.lead_tokens ? fmtInt(s.lead_tokens) + ' lead tokens' : 'No lead usage logged' }, h('span', null, 'Lead'), h('b', null, s.lead_tokens ? fmtCost(s.lead_cost) : DASH))));
  }
  function renderSessions() {
    var el = $('#sessions-body');
    var chipRow = $('#sessions-chip-row');
    var chips = [];
    if (S.project) chips.push(filterChip('Project', midEllipsis(S.project, 40), S.project, function () { S.project = ''; S.session = ''; changed(); }));
    if (S.session) chips.push(filterChip('Session', shortSid(S.session), S.session, clearSession));
    chipRow.replaceChildren.apply(chipRow, chips);
    chipRow.hidden = !chips.length;
    if (!D.sessions) {
      el.replaceChildren(h('div', { class: 'card-grid' }, [1, 2, 3].map(function () { return h('span', { class: 'sk', style: 'height:210px;border-radius:14px' }); })));
      return;
    }
    var L = D.sessions.slice();
    $('#s-count').textContent = fmtInt(L.length) + (L.length === 1 ? ' session' : ' sessions');
    if (!L.length) {
      el.replaceChildren(h('div', { class: 'state' }, h('span', { class: 'state-ic' }, icon('list')), h('b', null, 'No sessions in this range'), 'Pick a longer range, or run a routed task in Claude Code and it appears here live.'));
      return;
    }
    var focusLabel = document.activeElement && el.contains(document.activeElement) && document.activeElement.closest('article') ? document.activeElement.closest('article').getAttribute('aria-label') : null;
    var focusCls = focusLabel && document.activeElement.classList.contains('sid') ? '.sid' : '.stretch';
    var nodes = [], todayK = todayKey();
    if (UI.sSort === 'recent') {
      var groups = [];
      L.forEach(function (s) {
        var k = s.date || '';
        if (!groups.length || groups[groups.length - 1].key !== k) groups.push({ key: k, items: [] });
        groups[groups.length - 1].items.push(s);
      });
      groups.forEach(function (g) {
        var p = dayParts(g.key);
        var spend = g.items.reduce(function (a, s) { return a + (s.spend || 0); }, 0);
        nodes.push(h('section', { class: 'sess-day' + (g.key === todayK ? ' today' : '') },
          h('h3', null, h('span', { class: 'dname' }, p.name), p.date ? h('span', { class: 'muted' }, p.date) : null,
            h('span', { class: 'muted' }, fmtInt(g.items.length) + (g.items.length === 1 ? ' session · ' : ' sessions · ') + fmtCost(spend))),
          h('div', { class: 'card-grid' }, g.items.map(sessionCard))));
      });
    } else {
      L.sort(UI.sSort === 'spend' ? function (a, b) { return (b.spend || 0) - (a.spend || 0); } : function (a, b) { return b.tasks - a.tasks; });
      nodes.push(h('div', { class: 'card-grid' }, L.map(sessionCard)));
    }
    el.replaceChildren.apply(el, nodes);
    if (focusLabel) {
      var art = $$('article', el).filter(function (a) { return a.getAttribute('aria-label') === focusLabel; })[0];
      var f = art && art.querySelector(focusCls); if (f) f.focus({ preventScroll: true });
    }
  }

  // ------------------------------------------------------------------ footer
  function renderFooter() {
    var hl = D.health || {}, tz = '';
    try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) { tz = ''; }
    var off = -nowDate().getTimezoneOffset(), sign = off >= 0 ? '+' : '-';
    var offs = sign + pad(Math.floor(Math.abs(off) / 60)) + ':' + pad(Math.abs(off) % 60);
    var foot = $('#foot');
    var open = !!foot.querySelector('details[open]');
    foot.replaceChildren(h('span', null, 'Timezone: ' + (tz || 'browser local') + ' (UTC' + offs + ') in this browser' +
      (hl.tz ? ' · server ' + hl.tz + ' ' + hl.tz_offset : '') + ' · TokenTier ' + txt(hl.version) + ' · all data stays on this machine.'),
      hl.log_dir ? h('details', { class: 'logdir', open: open }, h('summary', null, 'Log folder'), h('code', null, hl.log_dir)) : null);
  }

  // ------------------------------------------------------------------ wiring
  function debounce(fn, ms) { var t; return function () { var a = arguments; clearTimeout(t); t = setTimeout(function () { fn.apply(null, a); }, ms); }; }
  function typing(el) { return el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.isContentEditable); }
  function setTheme(next, persist) {
    document.documentElement.dataset.theme = next;
    if (persist) store('tt-theme', next);
    var b = $('#theme');
    b.replaceChildren(icon(next === 'light' ? 'moon' : 'sun'));
    b.setAttribute('aria-label', next === 'light' ? 'Switch to dark theme' : 'Switch to light theme');
    b.title = next === 'light' ? 'Dark theme' : 'Light theme';
  }

  function wire() {
    tipEl = $('#tip');
    $('#range').addEventListener('click', function (e) {
      var b = e.target.closest('button[data-range]');
      if (!b) return;
      S.range = b.dataset.range;
      if (S.range === 'custom') {
        var t = todayKey();
        if (!S.to) S.to = t;
        if (!S.from) S.from = addDays(t, -6);
      } else { S.from = ''; S.to = ''; }
      changed();
    });
    function customChange() {
      var f = $('#from').value, t = $('#to').value;
      if (!f && !t) return;
      S.from = f; S.to = t; S.range = 'custom';
      changed();
    }
    $('#from').addEventListener('change', customChange);
    $('#to').addEventListener('change', customChange);
    $('#project').addEventListener('change', function (e) { S.project = e.target.value; S.session = ''; changed(); });
    $('#f-router').addEventListener('change', function (e) { S.router = e.target.checked ? '1' : ''; changed(); });
    $('#f-sort').addEventListener('change', function (e) { S.sort = e.target.value; changed(); });
    var qInput = debounce(function () { S.q = $('#f-q').value.trim(); changed(); }, 300);
    $('#f-q').addEventListener('input', function () { $('#q-clear').hidden = !$('#f-q').value; qInput(); });
    $('#f-q').addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && $('#f-q').value) { e.preventDefault(); e.stopPropagation(); $('#f-q').value = ''; $('#q-clear').hidden = true; S.q = ''; changed(); }
    });
    $('#q-clear').addEventListener('click', function () { $('#f-q').value = ''; $('#q-clear').hidden = true; S.q = ''; changed(); $('#f-q').focus(); });
    $('#density').addEventListener('click', function (e) {
      var b = e.target.closest('button[data-density]'); if (!b) return;
      UI.density = b.dataset.density; store('tt-density', UI.density);
      syncControls(); renderList();
    });
    $('#more').addEventListener('click', loadMore);
    $('#retry').addEventListener('click', function () { refresh(); });
    $('#detail-close').addEventListener('click', closeDetail);
    $('#backdrop').addEventListener('click', closeDetail);
    $('#p-q').addEventListener('input', debounce(function (e) { UI.pQ = e.target.value.trim(); renderProjects(); }, 150));
    $('#p-sort').addEventListener('change', function (e) { UI.pSort = e.target.value; renderProjects(); });
    $('#s-sort').addEventListener('change', function (e) { UI.sSort = e.target.value; renderSessions(); });

    var list = $('#list');
    list.addEventListener('click', function (e) {
      var d = e.target.closest('.day-t[data-date]');
      if (d) {
        var k = d.dataset.date;
        if (UI.collapsed[k]) delete UI.collapsed[k]; else UI.collapsed[k] = 1;
        store('tt-collapsed', JSON.stringify(UI.collapsed));
        renderList();
        return;
      }
      var r = e.target.closest('.task');
      if (r) {
        $$('.task', list).forEach(function (o) { o.tabIndex = -1; });
        r.tabIndex = 0;
        selectTask(r.dataset.id, { keepFocus: isWide() });
      }
    });
    list.addEventListener('keydown', function (e) {
      var r = e.target.closest && e.target.closest('.task'); if (!r) return;
      var rows = $$('.task', list), i = rows.indexOf(r), j = -1;
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); selectTask(r.dataset.id); return; }
      if (e.key === 'ArrowDown') j = i + 1;
      else if (e.key === 'ArrowUp') j = i - 1;
      else if (e.key === 'Home') j = 0;
      else if (e.key === 'End') j = rows.length - 1;
      else return;
      e.preventDefault();
      if (rows[j]) {
        r.tabIndex = -1; rows[j].tabIndex = 0;
        rows[j].focus({ preventScroll: true });
        var reduce = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
        rows[j].scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'nearest' });
      }
    });

    document.addEventListener('keydown', function (e) {
      var a = document.activeElement;
      if (e.key === '/' && !e.ctrlKey && !e.metaKey && !e.altKey && !typing(a)) {
        e.preventDefault();
        if (S.tab === 'projects') { $('#p-q').focus(); return; }
        if (S.tab !== 'overview') { S.tab = 'overview'; writeHash(); syncControls(); refresh(); }
        $('#f-q').focus(); $('#f-q').select();
        return;
      }
      if (e.key === 'Escape' && S.task && S.tab === 'overview' && !(a && a.tagName === 'SELECT')) {
        e.preventDefault(); closeDetail(); return;
      }
      // focus trap inside the bottom sheet
      if (e.key === 'Tab' && $('#details').classList.contains('open')) {
        var f = $$('#details button:not([hidden]), #details [href], #details summary, #details [tabindex="-1"]#details-h', document).filter(function (x) { return x.offsetParent !== null || x.id === 'details-h'; });
        if (!f.length) return;
        var firstEl = f[0], lastEl = f[f.length - 1];
        if (!$('#details').contains(a)) { e.preventDefault(); firstEl.focus(); }
        else if (e.shiftKey && a === firstEl) { e.preventDefault(); lastEl.focus(); }
        else if (!e.shiftKey && a === lastEl) { e.preventDefault(); firstEl.focus(); }
      }
    });

    $$('.tabs [role=tab]').forEach(function (b) {
      b.addEventListener('click', function () { S.tab = b.dataset.tab; writeHash(); syncControls(); syncSheet(false); renderAll(); refresh(); });
      b.addEventListener('keydown', function (e) {
        if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
        var i = TABS.indexOf(S.tab) + (e.key === 'ArrowRight' ? 1 : TABS.length - 1);
        S.tab = TABS[i % TABS.length]; writeHash(); syncControls(); syncSheet(false); $('#tab-' + S.tab).focus(); renderAll(); refresh();
      });
    });

    $('#theme').addEventListener('click', function () {
      var cur = document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
      setTheme(cur === 'light' ? 'dark' : 'light', true);
    });
    var th = store('tt-theme');
    var eff = th === 'light' || th === 'dark' ? th : (window.matchMedia && matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
    if (th === 'light' || th === 'dark') setTheme(th, false);
    else { setTheme(eff, false); delete document.documentElement.dataset.theme; }

    window.addEventListener('hashchange', function () {
      var n = parseHash();
      if (JSON.stringify(n) === JSON.stringify(S)) return;
      var taskChanged = n.task !== S.task;
      S = n; resetList(); syncControls();
      if (!S.task) { UI.sheet = false; D.detail = null; detailSig = ''; }
      syncSheet(false);
      refresh().then(function () { if (taskChanged && S.task) selectTask(S.task, { keepFocus: true }); });
    });

    if (window.matchMedia) {
      var mq = matchMedia('(min-width:1100px)');
      var onMq = function () { syncSheet(false); };
      if (mq.addEventListener) mq.addEventListener('change', onMq); else if (mq.addListener) mq.addListener(onMq);
    }
    var lastW = 0;
    if (window.ResizeObserver) {
      new ResizeObserver(debounce(function () {
        var w = $('#graphs').clientWidth;
        if (w !== lastW) { lastW = w; if (D.ov && S.tab === 'overview') renderCharts(); }
      }, 120)).observe($('#graphs'));
    }
  }

  // ------------------------------------------------------------------ boot
  function boot() {
    var hasHash = location.hash.replace(/^#/, '');
    S = parseHash();
    if (!hasHash) {
      var last = store('tt-last');
      if (last) { location.hash = last; S = parseHash(); }
    }
    wire();
    updateTodayBits();
    syncControls();
    renderLoading(false);
    if (S.task) UI.sheet = true;
    refresh().then(function () { if (S.task && !D.detail && !D.detailErr) selectTask(S.task, { keepFocus: true }); syncSheet(false); });
    connectLive();
    setInterval(tick, 30000);
    setInterval(liveTick, 1000);
  }
  boot();
})();
