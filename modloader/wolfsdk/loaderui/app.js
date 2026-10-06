/* WolfSDK loader page. Python (wolfsdk/webui.py) does the work; this file only
   shows its state and sends what was clicked. Every POST carries
   X-Wolfsdk-Loader: 1, which the server requires. */
'use strict';
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;

  // Lucide-style line icons (24 px grid)
  const P = {
    home: '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/><path d="M10 21v-6h4v6"/>',
    sliders: '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
    layers: '<path d="m12 2 10 5-10 5L2 7l10-5Z"/><path d="m2 17 10 5 10-5"/><path d="m2 12 10 5 10-5"/>',
    box: '<path d="M21 8 12 3 3 8v8l9 5 9-5V8Z"/><path d="m3 8 9 5 9-5M12 13v8"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/>',
    play: '<path d="M7 4.5v15l12-7.5-12-7.5Z" fill="currentColor" stroke="none"/>',
    folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    refresh: '<path d="M20 11a8 8 0 0 0-14.9-3.5M4 4v4h4"/><path d="M4 13a8 8 0 0 0 14.9 3.5M20 20v-4h-4"/>',
    check: '<path d="m5 12.5 4.5 4.5L19 7"/>',
    x: '<path d="M6 6l12 12M18 6 6 18"/>',
    map: '<path d="m9 4-6 2.5v13.5l6-2.5 6 2.5 6-2.5V4l-6 2.5L9 4Z"/><path d="M9 4v13.5M15 6.5V20"/>',
    shield: '<path d="M12 3 4.5 6v6c0 4.5 3.2 8 7.5 9 4.3-1 7.5-4.5 7.5-9V6L12 3Z"/><path d="m8.8 12 2.2 2.2 4.2-4.4"/>',
    alert: '<path d="M12 4 2.5 20h19L12 4Z"/><path d="M12 10v4M12 17h.01"/>',
    undo: '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
    download: '<path d="M12 3v12M7 10l5 5 5-5"/><path d="M5 21h14"/>',
    list: '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
    film: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4"/>',
    image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="m21 16-5-5-8 9"/>',
    copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h8"/>',
    external: '<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
    heart: '<path d="M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.6-7 10-7 10Z"/>',
    up: '<circle cx="12" cy="12" r="9"/><path d="m8 12 4-4 4 4M12 8v8"/>',
    lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
    ban: '<circle cx="12" cy="12" r="9"/><path d="m5.6 5.6 12.8 12.8"/>',
    disk: '<rect x="3" y="13" width="18" height="7" rx="2"/><path d="M5 13 7.5 5h9L19 13M7 16.5h.01"/>',
    file: '<path d="M14 3H6v18h12V7l-4-4Z"/><path d="M14 3v4h4M9 13h6M9 17h6"/>',
    sync: '<path d="M21 12a9 9 0 0 1-15.5 6.2M3 12a9 9 0 0 1 15.5-6.2"/><path d="M18.5 2.5v3.7h-3.7M5.5 21.5v-3.7h3.7"/>',
  };
  const ic = (n, cls = '') => `<svg class="i ${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${P[n] || ''}</svg>`;
  const TONE_ICON = { ok: 'shield', warn: 'alert', danger: 'alert' };

  async function api(path, body) {
    const opt = body === undefined ? {} : {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Wolfsdk-Loader': '1' }, body: JSON.stringify(body),
    };
    const r = await fetch('/api/' + path, opt);
    let data = null;
    try { data = await r.json(); } catch (_) { /* empty answer */ }
    if (!r.ok) throw new Error((data && data.error) || 'HTTP ' + r.status);
    return data;
  }

  const S = {
    st: null, page: 'overview', catRev: -1, fails: 0, lastJob: null,
    mods: [], modErrors: [], modsRev: -2, sel: null, modQ: '', modF: 'all',
    tw: null, twRev: -2, tq: '', tg: 'All', tOnly: false, preset: null,
    info: null, log: [], updToast: false,
  };

  // ---- small helpers ---------------------------------------------------------------------
  function text(el, t) { if (el && el.textContent !== t) el.textContent = t; }
  function tone(el, t) { el.classList.remove('tone-ok', 'tone-warn', 'tone-danger'); el.classList.add('tone-' + t); }
  function fillIcons(root = document) {
    for (const el of $$('[data-icon]', root)) {
      if (el.dataset.iconDone) continue;
      el.dataset.iconDone = '1';
      el.insertAdjacentHTML('afterbegin', ic(el.dataset.icon));
    }
  }
  function count(el, to) {
    const from = Number(el.dataset.v || 0);
    el.dataset.v = to;
    if (from === to || reduce) { el.textContent = to; return; }
    const t0 = performance.now(), d = 700;
    const step = t => {
      const k = Math.min(1, (t - t0) / d), e = 1 - Math.pow(1 - k, 3);
      el.textContent = Math.round(from + (to - from) * e);
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }
  function debounce(fn, ms) {
    let t;
    return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  }
  const busy = () => !!(S.st && S.st.busy);

  // ---- toasts, modal, activity -----------------------------------------------------------
  function toast(t, title, body = '', actions = []) {
    const el = document.createElement('div');
    el.className = 'toast tone-' + t;
    el.innerHTML = `${ic(t === 'ok' ? 'check' : t === 'info' ? 'info' : 'alert')}<div class="t-title">${esc(title)}</div>
      <button class="btn ghost icon x" aria-label="Dismiss">${ic('x')}</button>
      ${body ? `<div class="t-body">${esc(body)}</div>` : ''}
      ${actions.length ? `<div class="t-actions">${actions.map((a, i) => `<button class="btn" data-i="${i}">${esc(a.label)}</button>`).join('')}</div>` : ''}`;
    const close = () => { el.classList.add('out'); setTimeout(() => el.remove(), 260); };
    el.querySelector('.x').onclick = close;
    for (const b of $$('[data-i]', el)) b.onclick = () => { close(); actions[+b.dataset.i].run(); };
    $('#toasts').append(el);
    while ($('#toasts').children.length > 4) $('#toasts').firstElementChild.remove();
    setTimeout(close, t === 'danger' ? 12000 : 6000);
  }

  function modal({ t = 'warn', icon, title, body, actions }) {
    return new Promise(resolve => {
      const m = $('#modal');
      m.innerHTML = `<div class="dialog tone-${t}" role="dialog" aria-modal="true" aria-label="${esc(title)}">
        <div class="d-icon">${ic(icon || TONE_ICON[t] || 'info')}</div><h2>${esc(title)}</h2>
        <div class="d-body">${esc(body)}</div>
        <div class="d-actions">${actions.map((a, i) => `<button class="btn ${a.kind || ''}" data-i="${i}">${esc(a.label)}</button>`).join('')}</div></div>`;
      m.hidden = false;
      const done = v => { m.hidden = true; m.innerHTML = ''; document.removeEventListener('keydown', key, true); resolve(v); };
      const key = e => { if (e.key === 'Escape') { e.stopPropagation(); done(null); } };
      document.addEventListener('keydown', key, true);
      m.onclick = e => { if (e.target === m) done(null); };
      for (const b of $$('[data-i]', m)) b.onclick = () => done(actions[+b.dataset.i].value);
      const primary = $$('.d-actions .btn', m).pop();
      primary && primary.focus();
    });
  }

  const KIND = {
    check: ['Check finished', 'Check failed'], apply: ['Mods applied', 'Apply did not go through'],
    revert: ['Game reverted', 'Revert failed'], launch: ['Starting the game', 'Start did not go through'],
    map: ['Custom map', 'Custom map'],
  };
  const RUNNING = { check: 'Checking mod set, conflicts and backup…', apply: 'Applying mods…', revert: 'Reverting…',
    launch: 'Applying mods, then starting…', map: 'Working on the custom map…' };

  function logEntry(t, title, body) {
    S.log.unshift({ t, title, body, when: new Date() });
    S.log.length = Math.min(S.log.length, 40);
    $('#logList').innerHTML = S.log.map(e => `<div class="entry tone-${e.t}"><div class="entry-head">${ic(e.t === 'ok' ? 'check' : 'alert')}${esc(e.title)}
      <span class="when">${e.when.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span></div>${e.body ? `<pre>${esc(e.body)}</pre>` : ''}</div>`).join('');
  }

  function jobDone(j) {
    const names = KIND[j.kind] || ['Done', 'Failed'];
    const title = names[j.tone === 'danger' ? 1 : 0];
    logEntry(j.tone, title, j.text);
    const lines = j.text.split('\n');
    toast(j.tone, title, lines.slice(0, 3).join('\n'),
      lines.length > 3 ? [{ label: 'Show all', run: () => drawer(true) }] : []);
    if (j.kind === 'apply' || j.kind === 'revert' || j.kind === 'launch') S.modsRev = -2;
  }

  function drawer(open) { $('#drawer').classList.toggle('open', open ?? !$('#drawer').classList.contains('open')); }

  // ---- state -------------------------------------------------------------------------------
  let pollTimer = 0;
  async function poll() {
    clearTimeout(pollTimer);
    try {
      setState(await api('state'));
      S.fails = 0;
      $('#offline').hidden = true;
    } catch (_) {
      if (++S.fails >= 3) $('#offline').hidden = false;
    }
    pollTimer = setTimeout(poll, busy() ? 600 : document.hidden ? 15000 : 2500);
  }

  function setState(st) {
    S.st = st;
    document.body.classList.toggle('busy', st.busy);
    renderTop(st);
    renderPlay(st);
    renderOverview(st);
    renderStudio(st);
    renderUpdate(st);
    text($('#tabModCount'), String(st.counts.mods));
    if (st.catalog_rev !== S.catRev) {
      S.catRev = st.catalog_rev;
      if (S.page === 'mods') ensureMods();
      if (S.page === 'tweaks') ensureTweaks();
    }
    const j = st.job;
    if (S.lastJob === null) S.lastJob = j.id;
    else if (j.done && j.id > S.lastJob) { S.lastJob = j.id; jobDone(j); }
  }

  function renderTop(st) {
    const g = st.games.find(x => x.key === st.game);
    text($('#gameName'), g ? g.name : 'No game found');
    const eng = $('#gameEngine');
    eng.hidden = !g;
    text(eng, g ? g.engine : '');
    tone($('#gameDot').parentElement, st.status.tone);
    text($('#gamePath'), st.path);
    $('#gamePath').title = st.path;
    const menuKey = JSON.stringify([st.games, st.game]), gm = $('#gameMenu');
    if (gm.dataset.k === menuKey) { $('#gameBtn').disabled = st.busy; return; }
    gm.dataset.k = menuKey;
    gm.innerHTML = st.games.map(x => `<button class="menu-item" role="menuitem" data-game="${esc(x.key)}">
        <span class="tick">${x.key === st.game ? ic('check') : '<svg class="i"></svg>'}</span><span>${esc(x.name)}</span><span class="badge">${esc(x.engine)}</span>
        <span class="sub">${esc(x.root)}</span></button>`).join('')
      + `${st.games.length ? '<hr>' : ''}<button class="menu-item" role="menuitem" data-act="folder"><span>${ic('folder')}</span><span>Choose folder…</span><span></span>
        <span class="sub">Point the loader at an install it did not find</span></button>`;
    $('#gameBtn').disabled = st.busy;
  }

  function renderPlay(st) {
    tone($('#pdot'), st.busy ? 'warn' : st.status.tone);
    text($('#psTitle'), st.busy ? (RUNNING[st.job.kind] || 'Working…') : st.status.title);
    const pending = st.sync === false;
    text($('#psSub'), st.busy ? 'This can take a moment for large mods.'
      : pending ? 'Your selection is not in the game yet. Apply mods, or press Start game.' : (st.note || st.status.hint));
    const mods = !st.busy && st.supports_mods;
    $('#applyBtn').disabled = !mods;
    $('#revertBtn').disabled = !mods;
    $('#applyBtn').classList.toggle('pending', pending && mods);
    const start = $('#startBtn');
    start.disabled = st.busy || !st.game;
    const spinning = st.busy && st.job.kind === 'launch';
    if (start.dataset.spin !== String(spinning)) {
      start.dataset.spin = String(spinning);
      start.innerHTML = spinning ? '<span class="spin"></span>Starting…' : ic('play') + 'Start game';
    }
    for (const b of $$('[data-act="check"], [data-act="map-install"], [data-act="map-remove"], [data-act="all-on"], [data-act="all-off"]'))
      b.disabled = st.busy || !st.game;
  }

  function renderOverview(st) {
    const hero = $('#hero');
    tone(hero, st.status.tone);
    const want = TONE_ICON[st.status.tone];
    if (hero.dataset.icon !== want) { hero.dataset.icon = want; $('#heroIcon').innerHTML = ic(want); }
    text($('#heroTitle'), st.status.title);
    text($('#heroHint'), st.status.hint);
    const sync = st.game && st.supports_mods ? (st.sync === false ? 'pending' : 'ok') : '';
    const box = $('#heroSync');
    if (box.dataset.s !== sync) {
      box.dataset.s = sync;
      box.innerHTML = sync === 'pending' ? `<span class="sync-chip tone-warn">${ic('sync')}Changes not applied yet</span>`
        : sync === 'ok' ? `<span class="sync-chip tone-ok">${ic('check')}In sync with the game</span>` : '';
    }
    count($('#statMods'), st.counts.mods);
    text($('#statModsOf'), st.counts.mods_total ? '/ ' + st.counts.mods_total : '');
    $('#statModsBar').style.transform = `scaleX(${st.counts.mods_total ? st.counts.mods / st.counts.mods_total : 0})`;
    count($('#statTweaks'), st.counts.tweaks);
    text($('#statArchives'), !st.game ? 'Not ready' : st.supports_mods ? 'Ready' : 'Launch only');
    text($('#statArchivesSub'), !st.game ? 'choose the game folder' : st.supports_mods ? 'mods can be written' : 'cvars only for this game');
    text($('#mapText'), st.map.text);
    $('#mapCard').classList.toggle('disabled', !st.map.available);
    for (const b of $$('#mapCard .btn')) b.disabled = st.busy || !st.map.available;
  }

  function renderStudio(st) {
    const b = $('#studioBtn');
    b.hidden = !st.studio;
    if (!st.studio) text($('#studioText'), 'The Studio is part of WolfSDK Studio, the separate package for modders (WolfSDK-Studio-<version>.zip).');
  }

  function renderUpdate(st) {
    const u = st.update, pill = $('#updatePill');
    const show = u.available && !u.dismissed;
    pill.hidden = !show;
    if (show && pill.dataset.v !== u.latest.version) {
      pill.dataset.v = u.latest.version;
      pill.innerHTML = `<span class="pulse"></span>Update ${esc(u.latest.version)} available`;
    }
    if (show && !S.updToast) {
      S.updToast = true;
      toast('ok', `WolfSDK ${u.latest.version} is out`, `You have ${u.current}.`, [{ label: 'What is new', run: showUpdate }]);
    }
    $('#updSwitch').setAttribute('aria-checked', String(u.enabled));
    text($('#updText'), u.available
      ? `Version ${u.latest.version} is available (you have ${u.current}).`
      : `You have version ${u.current}. Asks GitHub for new WolfSDK releases, nothing else.`);
  }

  async function showUpdate() {
    const u = S.st.update;
    if (!u.available) return;
    const notes = (u.latest.notes || '').replace(/\r/g, '').trim();
    const v = await modal({
      t: 'ok', icon: 'up', title: `WolfSDK ${u.latest.version}`,
      body: `${u.latest.name}${u.latest.published ? ' · ' + u.latest.published : ''}\nYou have ${u.current}.${notes ? '\n\n' + notes : ''}`,
      actions: [{ label: 'Skip this version', value: 'skip', kind: 'ghost' }, { label: 'Later', value: null },
        { label: 'Download', value: 'get', kind: 'primary' }],
    });
    if (v === 'get') api('open', { what: 'release' }).catch(e => toast('danger', 'Could not open the page', e.message));
    if (v === 'skip') { await api('update/dismiss', { version: u.latest.version }); poll(); }
  }

  // ---- mods -------------------------------------------------------------------------------
  async function ensureMods(force) {
    if (!force && S.modsRev === S.catRev) return;
    try {
      const r = await api('mods');
      S.mods = r.mods; S.modErrors = r.errors; S.modsRev = S.catRev;
      if (S.sel && !S.mods.some(m => m.id === S.sel)) S.sel = null;
      renderMods(true);
    } catch (e) { toast('danger', 'Could not read the mods', e.message); }
  }
  function modVisible(m) {
    if (S.modF === 'on' && !m.on) return false;
    if (S.modF === 'off' && m.on) return false;
    const q = S.modQ.trim().toLowerCase();
    return !q || [m.id, m.name, m.author, m.description].join(' ').toLowerCase().includes(q);
  }
  function modRow(m, i) {
    const delay = i >= 0 ? `animation-delay:${Math.min(i, 18) * 16}ms` : 'animation:none';
    return `<div class="mod ${m.on ? '' : 'off'} ${S.sel === m.id ? 'sel' : ''}" data-id="${esc(m.id)}" role="option" tabindex="0" aria-selected="${S.sel === m.id}" style="${delay}">
      <button class="switch" role="switch" aria-checked="${m.on}" aria-label="${esc(m.name)}" data-toggle></button>
      <div class="main"><div class="name">${esc(m.name)}</div><div class="meta">${esc(m.id)}${m.author ? ' · ' + esc(m.author) : ''}</div></div>
      <div class="side"><span class="chip">v${esc(m.version)}</span><span class="chip" title="Priority: the higher one wins a conflict">P${m.priority}</span>
      <span class="chip">${m.assets} asset${m.assets === 1 ? '' : 's'}</span></div></div>`;
  }
  function renderMods(animate) {
    const vis = S.mods.filter(modVisible);
    $('#modList').innerHTML = vis.length ? vis.map((m, i) => modRow(m, animate ? i : -1)).join('')
      : `<div class="empty">${S.mods.length ? 'No mod matches.' : 'No mods for this game in the mods folder.'}</div>`;
    const active = S.mods.filter(m => m.on).length;
    text($('#modCount'), `${active} active · ${vis.length} of ${S.mods.length}`);
    const err = $('#modErrors');
    err.hidden = !S.modErrors.length;
    err.textContent = S.modErrors.map(e => '! ' + e).join('\n');
    renderDetail();
  }
  function renderDetail() {
    const d = $('#modDetail'), m = S.mods.find(x => x.id === S.sel);
    if (!m) {
      d.innerHTML = `<div class="detail-empty">${ic('layers')}<h3>No mod selected</h3><p>Pick a mod to read what it does.</p></div>`;
      return;
    }
    d.innerHTML = `<div class="detail-in"><div class="eyebrow">Selected mod</div>
      <div><h2>${esc(m.name)}</h2><div class="dim small mono">${esc(m.id)}</div></div>
      <div class="chips"><span class="chip">version ${esc(m.version)}</span><span class="chip">priority ${m.priority}</span>
        <span class="chip">${m.assets} asset${m.assets === 1 ? '' : 's'}</span>${m.cvars ? `<span class="chip">${m.cvars} cvar${m.cvars === 1 ? '' : 's'}</span>` : ''}
        ${m.author ? `<span class="chip">by ${esc(m.author)}</span>` : ''}</div>
      <p class="desc">${esc(m.description || 'No description.')}</p>
      <button class="btn wide ${m.on ? '' : 'primary'}" data-detail-toggle>${m.on ? ic('x') + 'Turn off' : ic('check') + 'Turn on'}</button></div>`;
  }
  function paintMod(m) {
    const row = $(`.mod[data-id="${CSS.escape(m.id)}"]`);
    if (row) {
      row.classList.toggle('off', !m.on);
      $('.switch', row).setAttribute('aria-checked', String(m.on));
      if (!modVisible(m)) row.remove();
    }
    text($('#modCount'), `${S.mods.filter(x => x.on).length} active · ${$$('.mod').length} of ${S.mods.length}`);
    if (S.sel === m.id) renderDetail();
  }
  async function setMod(id, on) {
    const m = S.mods.find(x => x.id === id);
    if (!m) return;
    m.on = on; paintMod(m);
    try {
      const r = await api('mods', { id, on });
      S.st.counts.mods = r.active;
      text($('#tabModCount'), String(r.active));
      poll();
    } catch (e) { m.on = !on; paintMod(m); toast('danger', 'Could not change the mod', e.message); }
  }
  function selectMod(id, focus) {
    S.sel = id;
    for (const r of $$('.mod')) { const on = r.dataset.id === id; r.classList.toggle('sel', on); r.setAttribute('aria-selected', on); if (on && focus) r.focus(); }
    renderDetail();
  }

  // ---- tweaks -----------------------------------------------------------------------------
  async function ensureTweaks(force) {
    if (!force && S.twRev === S.catRev && S.tw) return;
    try {
      S.tw = await api('tweaks');
      S.twRev = S.catRev;
      if (!S.tw.groups.includes(S.tg)) S.tg = 'All';
      $('#twGroup').innerHTML = ['All', ...S.tw.groups].map(g => `<option ${g === S.tg ? 'selected' : ''}>${esc(g)}</option>`).join('');
      if (document.activeElement !== $('#extra')) $('#extra').value = S.tw.extra || '';
      renderPresets();
      renderTweaks();
    } catch (e) { toast('danger', 'Could not read the cvars', e.message); }
  }
  function renderPresets() {
    const box = $('#twPresets'), ps = S.tw.presets;
    box.hidden = !ps.length;
    if (!ps.length) return;
    if (!ps.some(p => p.name === S.preset)) S.preset = ps[0].name;
    box.innerHTML = `<span class="eyebrow">Presets</span><div class="seg">${ps.map(p => `<button data-preset="${esc(p.name)}" aria-pressed="${p.name === S.preset}" title="${p.count} cvars">${esc(p.name)}</button>`).join('')}</div>
      <button class="btn" data-act="preset">${ic('check')}Apply preset</button><button class="btn ghost" data-act="preset-reset">${ic('undo')}Reset</button>
      <span class="note">Not yet confirmed in game. Photo Mode = screenshot quality only.</span>`;
  }
  function twVisible(t) {
    if (S.tg !== 'All' && t.group !== S.tg) return false;
    if (S.tOnly && !t.value) return false;
    const q = S.tq.trim().toLowerCase();
    return !q || t.key.toLowerCase().includes(q) || t.label.toLowerCase().includes(q) || (t.note || '').toLowerCase().includes(q);
  }
  function twControl(t) {
    const k = esc(t.key);
    if (t.kind === 'bool') return `<button class="switch" role="switch" aria-checked="${t.value === t.on}" data-k="${k}" aria-label="${esc(t.label)}"></button>`;
    if (t.kind === 'choice') return `<select class="input" data-k="${k}" aria-label="${esc(t.label)}"><option value="">default</option>${t.choices.map(c => `<option value="${esc(c[0])}" ${String(c[0]) === t.value ? 'selected' : ''}>${esc(c[1] ?? c[0])}</option>`).join('')}</select>`;
    return `<input class="input" data-k="${k}" value="${esc(t.value)}" placeholder="${esc(t.default ?? 'default')}" spellcheck="false" aria-label="${esc(t.label)}" ${t.kind === 'text' ? '' : 'inputmode="decimal"'}>`;
  }
  function twRow(t) {
    return `<div class="tw ${t.value ? 'set' : ''}" data-row="${esc(t.key)}"><div class="ctl">${twControl(t)}</div>
      <div class="label">${esc(t.label)}</div>
      <div class="key">${t.label !== t.key ? esc(t.key) : ''}${t.flag ? `<span class="chip flag ${t.flag === 'cheat' ? '' : 'warn'}" title="${esc(t.tip)}">${esc(t.flag)}</span>` : ''}${t.default != null && t.default !== '' ? `<span class="chip">default ${esc(t.default)}</span>` : ''}</div>
      ${t.note ? `<div class="note">${esc(t.note)}</div>` : ''}</div>`;
  }
  // The whole list, every cvar: rows off screen cost no layout (content-visibility in app.css).
  function renderTweaks() {
    const hits = S.tw.items.filter(twVisible), parts = [];
    let g = null;
    for (const t of hits) {     // one section per category: its sticky head gives way to the next one
      if (t.group !== g) {
        if (g !== null) parts.push('</section>');
        g = t.group;
        parts.push(`<section class="tw-group"><div class="group-head"><span>${esc(g)}</span><i></i></div>`);
      }
      parts.push(twRow(t));
    }
    if (g !== null) parts.push('</section>');
    $('#twList').innerHTML = hits.length ? parts.join('') : '<div class="empty">Nothing found.</div>';
    text($('#twCount'), `${hits.length} of ${S.tw.items.length} cvars`);
  }
  async function setTweak(key, value, row) {
    const t = S.tw.items.find(x => x.key === key);
    try {
      const r = await api('tweak', { key, value });
      t.value = value;
      if (row) { row.classList.toggle('set', !!value); row.classList.remove('saved'); void row.offsetWidth; row.classList.add('saved'); }
      if (S.st) { S.st.counts.tweaks = r.tweaks; count($('#statTweaks'), r.tweaks); }
      poll();
    } catch (e) { toast('danger', 'Could not save ' + key, e.message); }
  }
  const NUM = { int: /^-?\d+$/, float: /^-?(\d+\.?\d*|\.\d+)(e-?\d+)?$/i };
  const typed = debounce((input) => {
    const t = S.tw.items.find(x => x.key === input.dataset.k), v = input.value.trim();
    const bad = v && NUM[t.kind] && !NUM[t.kind].test(v);
    input.classList.toggle('bad', !!bad);
    if (!bad && v !== t.value) setTweak(t.key, v, input.closest('.tw'));
  }, 450);
  const saveExtra = debounce(async () => {
    try {
      const r = await api('extra', { text: $('#extra').value });
      if (S.st) count($('#statTweaks'), r.tweaks);
      const s = $('#extraSaved'); s.classList.add('show'); setTimeout(() => s.classList.remove('show'), 1400);
    } catch (e) { toast('danger', 'Could not save the custom cvars', e.message); }
  }, 600);

  // ---- info -------------------------------------------------------------------------------
  async function ensureInfo() {
    if (S.info) return;
    try { S.info = await api('info'); } catch (e) { toast('danger', 'Could not read the info', e.message); return; }
    text($('#infoConsole'), S.info.console);
    $('#infoCmds').innerHTML = S.info.commands.map(([c, d]) => `<div><code>${esc(c)}</code><span class="desc">${esc(d)}</span>
      <span class="copy"><button class="btn ghost icon" data-copy="${esc(c)}" title="Copy" aria-label="Copy ${esc(c)}">${ic('copy')}</button></span></div>`).join('');
    $('#infoBinds').innerHTML = S.info.binds.map(([k, c, d]) => `<div class="bind"><kbd>${esc(k)}</kbd><code>${esc(c)}</code><span class="muted">${esc(d)}</span></div>`).join('');
    $('#infoNotes').innerHTML = S.info.notes.map(n => `<li>${esc(n)}</li>`).join('');
  }

  // ---- navigation -------------------------------------------------------------------------
  function ink() {
    const t = $(`.tab[data-page="${S.page}"]`), k = $('#ink');
    if (!t) return;
    k.style.width = (t.offsetWidth - 24) + 'px';
    k.style.transform = `translateX(${t.offsetLeft + 12}px)`;
  }
  function go(page) {
    if (!$('#page-' + page)) page = 'overview';
    S.page = page;
    for (const t of $$('.tab')) t.setAttribute('aria-selected', String(t.dataset.page === page));
    for (const p of $$('.page')) p.classList.toggle('active', p.id === 'page-' + page);
    ink();
    $('#main').scrollTop = 0;
    if (page === 'mods') ensureMods();
    if (page === 'tweaks') ensureTweaks();
    if (page === 'info') ensureInfo();
    try { sessionStorage.setItem('page', page); } catch (_) { /* private mode */ }
  }

  // ---- actions ----------------------------------------------------------------------------
  async function run(path, body) {
    try { const r = await api(path, body); poll(); return r; } catch (e) { toast('danger', 'Not possible right now', e.message); return null; }
  }
  async function start() {
    const r = await run('launch', {});
    if (!r) return;
    if (r.need_apply) {
      const v = await modal({
        t: 'warn', icon: 'sync', title: 'Mods not in the game yet',
        body: 'The ticked mods are not in the game files yet. Apply them now and then start?',
        actions: [{ label: 'Cancel', value: null, kind: 'ghost' }, { label: 'Start without', value: false },
          { label: 'Apply and start', value: true, kind: 'primary' }],
      });
      if (v === null) return;
      const r2 = await run('launch', { apply: v });
      if (r2 && r2.started) { toast('ok', 'Starting the game', r2.text); logEntry('ok', 'Starting the game', r2.text); }
    } else if (r.started) { toast('ok', 'Starting the game', r.text); logEntry('ok', 'Starting the game', r.text); }
  }
  const ACTS = {
    start,
    apply: () => run('apply', {}),
    check: () => run('check', {}),
    revert: async () => {
      const v = await modal({ t: 'danger', icon: 'undo', title: 'Revert the game?', body: 'Undo all mod changes and restore the original game files, bit for bit.',
        actions: [{ label: 'Cancel', value: false, kind: 'ghost' }, { label: 'Revert', value: true, kind: 'primary' }] });
      if (v) run('revert', {});
    },
    folder: async () => { const r = await run('folder', {}); if (r && r.chosen) { S.info = null; toast('ok', 'Game folder set'); } },
    'map-install': () => run('map/install', {}),
    'map-remove': async () => {
      const v = await modal({ t: 'warn', icon: 'map', title: 'Remove the custom map?', body: 'c2v1 will then load the original map from the DLC again.',
        actions: [{ label: 'Cancel', value: false, kind: 'ghost' }, { label: 'Remove map', value: true, kind: 'primary' }] });
      if (v) run('map/uninstall', {});
    },
    studio: async () => { const r = await run('studio', {}); if (r) toast('ok', 'Studio is opening', 'It runs in its own window.'); },
    'open-mods': () => run('open', { what: 'mods' }),
    support: () => run('open', { what: 'support' }),
    'all-on': async () => { await run('mods', { all: true }); ensureMods(true); },
    'all-off': async () => { await run('mods', { all: false }); ensureMods(true); },
    reload: async () => { await run('reload', {}); await ensureMods(true); toast('ok', 'Mods folder rescanned', `${S.mods.length} mods for this game.`); },
    log: () => drawer(),
    preset: async () => applyPreset(false),
    'preset-reset': async () => applyPreset(true),
  };
  async function applyPreset(reset) {
    const r = await run('preset', { name: S.preset, reset });
    if (!r) return;
    for (const [k, v] of Object.entries(r.values)) { const t = S.tw.items.find(x => x.key === k); if (t) t.value = v; }
    if (!reset) { S.tOnly = true; $('#twOnly').setAttribute('aria-pressed', 'true'); }
    renderTweaks();
    toast('ok', reset ? 'Preset reset' : 'Preset applied', r.text);
  }

  // ---- wiring -----------------------------------------------------------------------------
  function wire() {
    $('#folderBtn').innerHTML = ic('folder');
    fillIcons();

    document.addEventListener('click', e => {
      const tab = e.target.closest('.tab');
      if (tab) return go(tab.dataset.page);
      const goto = e.target.closest('[data-go]');
      if (goto) return go(goto.dataset.go);
      const game = e.target.closest('[data-game]');
      if (game) { menu(false); if (game.dataset.game !== S.st.game) run('game', { key: game.dataset.game }); return; }
      const act = e.target.closest('[data-act]');
      if (act && !act.disabled) { if (act.closest('#gameMenu')) menu(false); return ACTS[act.dataset.act] && ACTS[act.dataset.act](); }
      if (!e.target.closest('.switcher')) menu(false);
    });
    $('#gameBtn').onclick = e => { e.stopPropagation(); menu(!$('#gameMenu').classList.contains('open')); };
    $('#folderBtn').onclick = () => ACTS.folder();
    $('#updatePill').onclick = showUpdate;
    $('#updSwitch').onclick = async e => {
      const on = e.currentTarget.getAttribute('aria-checked') !== 'true';
      e.currentTarget.setAttribute('aria-checked', String(on));
      await run('setting', { update_check: on });
    };

    // mods
    $('#modList').addEventListener('click', e => {
      const row = e.target.closest('.mod');
      if (!row) return;
      const m = S.mods.find(x => x.id === row.dataset.id);
      if (e.target.closest('[data-toggle]')) setMod(m.id, !m.on);
      else selectMod(m.id);
    });
    $('#modList').addEventListener('keydown', e => {
      const row = e.target.closest('.mod');
      if (!row) return;
      const m = S.mods.find(x => x.id === row.dataset.id);
      if (e.key === ' ') { e.preventDefault(); setMod(m.id, !m.on); }
      else if (e.key === 'Enter') selectMod(m.id);
      else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        const next = e.key === 'ArrowDown' ? row.nextElementSibling : row.previousElementSibling;
        if (next && next.dataset.id) selectMod(next.dataset.id, true);
      }
    });
    $('#modDetail').addEventListener('click', e => {
      if (e.target.closest('[data-detail-toggle]')) { const m = S.mods.find(x => x.id === S.sel); if (m) setMod(m.id, !m.on); }
    });
    $('#modSearch').addEventListener('input', debounce(e => { S.modQ = e.target.value; renderMods(false); $('#main').scrollTop = 0; }, 80));
    $('#modFilter').addEventListener('click', e => {
      const b = e.target.closest('button');
      if (!b) return;
      S.modF = b.dataset.f;
      for (const x of $$('#modFilter button')) x.setAttribute('aria-pressed', String(x === b));
      renderMods(true);
    });

    // tweaks
    $('#twSearch').addEventListener('input', debounce(e => { S.tq = e.target.value; renderTweaks(); $('#main').scrollTop = 0; }, 150));
    $('#twGroup').addEventListener('change', e => { S.tg = e.target.value; renderTweaks(); $('#main').scrollTop = 0; });
    $('#twOnly').onclick = e => {
      S.tOnly = !S.tOnly;
      e.currentTarget.setAttribute('aria-pressed', String(S.tOnly));
      renderTweaks();
    };
    $('#twPresets').addEventListener('click', e => {
      const b = e.target.closest('[data-preset]');
      if (!b) return;
      S.preset = b.dataset.preset;
      for (const x of $$('[data-preset]')) x.setAttribute('aria-pressed', String(x === b));
    });
    $('#twList').addEventListener('click', e => {
      const sw = e.target.closest('.switch[data-k]');
      if (!sw) return;
      const t = S.tw.items.find(x => x.key === sw.dataset.k), on = sw.getAttribute('aria-checked') !== 'true';
      sw.setAttribute('aria-checked', String(on));
      setTweak(t.key, on ? t.on : '', sw.closest('.tw'));
    });
    $('#twList').addEventListener('change', e => {
      if (e.target.matches('select[data-k]')) setTweak(e.target.dataset.k, e.target.value, e.target.closest('.tw'));
    });
    $('#twList').addEventListener('input', e => { if (e.target.matches('input[data-k]')) typed(e.target); });
    $('#extra').addEventListener('input', saveExtra);

    // info
    $('#infoCmds').addEventListener('click', async e => {
      const b = e.target.closest('[data-copy]');
      if (!b) return;
      try { await navigator.clipboard.writeText(b.dataset.copy); toast('ok', 'Copied', b.dataset.copy); }
      catch (_) { toast('danger', 'Could not copy'); }
    });

    document.addEventListener('keydown', e => {
      if (e.key === 'Escape') { menu(false); drawer(false); }
      if (e.ctrlKey && /^[1-5]$/.test(e.key)) { e.preventDefault(); go($$('.tab')[+e.key - 1].dataset.page); }
      if (e.ctrlKey && e.key.toLowerCase() === 'f' && (S.page === 'mods' || S.page === 'tweaks')) {
        e.preventDefault(); $(S.page === 'mods' ? '#modSearch' : '#twSearch').focus();
      }
    });
    addEventListener('resize', ink);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
    addEventListener('pagehide', () => {
      fetch('/api/bye', { method: 'POST', keepalive: true, headers: { 'Content-Type': 'application/json', 'X-Wolfsdk-Loader': '1' }, body: '{}' }).catch(() => {});
    });
  }
  function menu(open) {
    $('#gameMenu').classList.toggle('open', open);
    $('#gameBtn').setAttribute('aria-expanded', String(open));
    if (open) { const first = $('#gameMenu .menu-item'); first && first.focus(); }
  }

  wire();
  let first = 'overview';
  try { first = sessionStorage.getItem('page') || first; } catch (_) { /* private mode */ }
  go(first);
  document.fonts && document.fonts.ready.then(ink);
  poll();
  window.loader = { state: () => ({ page: S.page, st: S.st, mods: S.mods.length, sel: S.sel, tweaks: S.tw && S.tw.items.length }), go };
})();
