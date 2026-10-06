/* Wolfenstein Studio: one fullscreen page for both games with the sections
   Karten | Video | Audio | Texturen | Texte | Modelle. Karten is the 3D viewer (app.js, loaded
   after this file, driven by the events studio:section and studio:game); Modelle draws with
   models.js (loaded before this file); the sections read the Studio API of
   wolfsdk/mapserver.py + wolfsdk/studio.py:

     /api/games                                        which game has which section
     /api/<g>/<kind>?q=&group=&lang=&offset=&limit=&facets=1    kind = videos|sounds|textures|texts|models|scripts
     /api/<g>/<kind>/info?id=                          one item
     /api/<g>/videos/frames?id=&start=&w=&seek=        WVF1 frame stream (header below)
     /api/<g>/videos/frame.png?id=&n=&w=  /api/<g>/videos/audio.wav?id=&tracks=<track ids>
     /api/<g>/sounds/audio?id=  /api/<g>/textures/image.png?id=&max=  /api/<g>/texts/text?id=
     /api/<g>/models/info?id=  /api/<g>/models/mesh.bin?id=&lod=0     (models.js has the format)

   URL parameters: s=karten|video|audio|texturen|texte|modelle|scripts  game=tnc|tno  id=<item id>
   (a ?map= without game= is a Wolfenstein II map; the map viewer's own parameters are in app.js).

   5.1 sounds (defect 1): Wwise leaves 150 six-channel sounds in WAVE order (FL FR FC LFE
   SL SR) inside Ogg Vorbis, whose own 6-channel order is L C R RL RR LFE. Measured in
   Edge 153 with synthetic Vorbis streams that carry signal in one channel only
   (tools/verify_studio_front.py, section "5.1"): Vorbis channel k comes out of
   decodeAudioData and out of a MediaElementAudioSourceNode as channel [0,2,1,4,5,3][k].
   So output channel j takes decoded channel WAVE_FROM_EDGE[j] and every WAVE speaker
   lands on the Web Audio speaker of the same name (L R C LFE SL SR). */
(function () {
  'use strict';

  const Q = new URLSearchParams(location.search);
  const $ = (id) => document.getElementById(id);
  const B = document.body;
  const SECTIONS = { karten: 'Maps', video: 'Video', audio: 'Audio', texturen: 'Textures', texte: 'Texts', modelle: 'Models', scripts: 'Scripts' };
  const KIND = { video: 'videos', audio: 'sounds', texturen: 'textures', texte: 'texts', modelle: 'models', scripts: 'scripts' };
  const NOUN = { videos: ['Video', 'Videos'], sounds: ['Sound', 'Sounds'], textures: ['Texture', 'Textures'], texts: ['Text', 'Texts'],
    models: ['Model', 'Models'], scripts: ['Script', 'Scripts'] };
  const GAMES = { tnc: 'Wolfenstein II: The New Colossus', tno: 'Wolfenstein: The New Order', yb: 'Wolfenstein: Youngblood' };
  const WAVE_FROM_EDGE = [0, 2, 1, 4, 5, 3];
  const LANGS = { 'english(us)': 'English (US)', english: 'English', german: 'German', 'french(france)': 'French',
    french: 'French', italian: 'Italian', 'portuguese(brazil)': 'Portuguese (Brazil)',
    brazilian_portuguese: 'Portuguese (Brazil)', russian: 'Russian', polish: 'Polish', japanese: 'Japanese',
    'spanish(mexico)': 'Spanish (Mexico)', latin_spanish: 'Spanish (Latin America)', 'spanish(spain)': 'Spanish (Spain)',
    spanish: 'Spanish', s_chinese: 'Chinese (simplified)', t_chinese: 'Chinese (traditional)' };
  const langName = (l) => LANGS[l] || l;
  const plural = (n, one, many) => num(n) + ' ' + (n === 1 ? one : many);
  const PAGE = 200;
  const VIDEO_W = 960;                  // frame stream width: 1920-wide videos come as 960x540 (step 2)

  const store = {
    get(k) { try { return localStorage.getItem('studio.' + k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem('studio.' + k, v); } catch (e) { /* private mode */ } },
  };

  let section = [Q.get('s'), Q.has('map') ? 'karten' : null, store.get('section'), 'karten'].find((s) => s in SECTIONS);
  let game = [Q.get('game'), Q.has('map') ? 'tnc' : null, store.get('game'), 'tnc'].find((g) => g in GAMES);
  B.dataset.section = section;          // app.js reads both before it starts
  B.dataset.game = game;

  // ---- helpers -----------------------------------------------------------------

  const num = (n) => (+n).toLocaleString('en-US');
  const dec = (n, d) => (+n).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
  function bytes(n) {
    if (n < 1024) return num(n) + ' B';
    if (n < 1048576) return dec(n / 1024, 1) + ' KB';
    return dec(n / 1048576, 1) + ' MB';
  }
  function tc(s, tenths) {               // 83.4 -> 1:23 (or 1:23.4)
    s = Math.max(0, s || 0);
    const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, r = s - 60 * Math.floor(s / 60);
    const sec = tenths ? dec(Math.floor(r * 10) / 10, 1).padStart(4, '0') : String(Math.floor(r)).padStart(2, '0');
    return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + sec;
  }
  const chName = (n) => ({ 1: 'Mono', 2: 'Stereo', 4: 'Quad', 6: '5.1' }[n] || n + ' channels');
  const khz = (r) => dec(r / 1000, r % 1000 ? 1 : 0) + ' kHz';
  const api = (kind, leaf, params) => '/api/' + game + '/' + kind + (leaf ? '/' + leaf : '') + (params ? '?' + new URLSearchParams(params) : '');
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  // folder labels: the shortest tail of each path no other folder shares ('one' twice -> 'sd/one', 'joe/one')
  function shortNames(groups) {
    const parts = groups.map((g) => g.split('/')), tail = (p, k) => p.slice(-k).join('/'), count = new Map();
    for (const p of parts) for (let k = 1; k <= p.length; k++) count.set(tail(p, k), (count.get(tail(p, k)) || 0) + 1);
    const out = new Map();
    parts.forEach((p, i) => {
      let k = 1;
      while (k < p.length && count.get(tail(p, k)) > 1) k++;
      out.set(groups[i], [tail(p, k), p.slice(0, -k).join('/')]);
    });
    return out;
  }
  function facts(dl, rows) {              // <dl class="facts wide">: one card per fact
    dl.textContent = '';
    for (const [k, v, cls] of rows) {
      if (v == null || v === '') continue;
      const box = el('div'), dd = el('dd', cls, v);
      dd.title = v;
      box.append(el('dt', null, k), dd);
      dl.append(box);
    }
  }
  async function fetchOk(url, opt) {
    let r;
    try {
      r = await fetch(url, opt);
    } catch (e) {
      if (e.name === 'AbortError') throw e;
      throw new Error('No connection to the Studio server. Is the loader still running?');
    }
    if (!r.ok) {
      let t = '';
      try { t = (await r.text()).trim(); } catch (e) { /* ignore */ }
      if (t[0] === '{') { try { t = String(JSON.parse(t).error || ''); } catch (e) { /* not JSON: the text itself */ } }
      const err = new Error(t && t.length < 400 && !/^</.test(t) ? t : 'The server answered with ' + r.status + '.');
      err.status = r.status;
      throw err;
    }
    return r;
  }
  const getJson = async (url, opt) => (await fetchOk(url, opt)).json();
  let toastTimer = 0;
  function toast(text, ms) {
    const t = $('toast');
    t.textContent = text;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, ms || 3500);
  }
  async function copy(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {                        // no clipboard permission: the old way
      const ta = el('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.append(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
    }
    toast(text.length > 80 ? 'Copied (' + num(text.length) + ' characters)' : 'Copied: ' + text);
  }
  const typing = (t) => t && (t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || (t.tagName === 'INPUT' && /^(text|search)$/.test(t.type)));

  // ---- virtual list: a pool of absolutely placed rows over a tall spacer -------------------

  class VList {
    constructor(host, rowH, render) {
      this.host = host;
      this.space = host.querySelector('.vspace');
      this.rowH = rowH;
      this.render = render;
      this.n = 0;
      this.pool = [];
      host.addEventListener('scroll', () => this.draw(), { passive: true });
      new ResizeObserver(() => { if (this.onresize) this.onresize(); this.draw(); }).observe(host);
    }
    setRows(n, keepScroll) {
      this.n = n;
      this.space.style.height = n * this.rowH + 'px';
      if (!keepScroll) this.host.scrollTop = 0;
      this.draw();
    }
    draw() {
      const top = this.host.scrollTop, h = this.host.clientHeight || 900;
      const a = Math.max(0, Math.floor(top / this.rowH) - 3), b = Math.min(this.n, Math.ceil((top + h) / this.rowH) + 3);
      while (this.pool.length < b - a) {
        const d = el('div', 'vrow');
        this.space.append(d);
        this.pool.push(d);
      }
      for (let k = 0; k < this.pool.length; k++) {
        const d = this.pool[k], i = a + k;
        if (i >= b) { d.hidden = true; continue; }
        d.hidden = false;
        d.style.transform = 'translateY(' + i * this.rowH + 'px)';
        d.dataset.i = i;
        this.render(i, d);
      }
    }
    reveal(i, centre) {
      const y = i * this.rowH, h = this.host.clientHeight;
      if (centre) this.host.scrollTop = y - (h - this.rowH) / 2;
      else if (y < this.host.scrollTop) this.host.scrollTop = y;
      else if (y + this.rowH > this.host.scrollTop + h) this.host.scrollTop = y + this.rowH - h;
    }
  }

  // ---- listings: search, paging, folders ---------------------------------------------

  const LOADING = 'laden';
  class Listing {
    constructor(g, kind) {
      Object.assign(this, { game: g, kind, q: '', lang: null, group: null, gen: 0, pages: new Map(), total: 0,
        groups: [], langs: [], collapsed: new Set(), flat: true, rows: 0, loaded: false, error: null,
        selId: null, selIdx: -1, scroll: 0, onchange: null });
    }
    url(params) { return '/api/' + this.game + '/' + this.kind + '?' + new URLSearchParams(params); }
    params(offset) {
      const p = { q: this.q, offset, limit: PAGE };
      if (this.group != null) p.group = this.group;
      if (this.lang != null) p.lang = this.lang;
      return p;
    }
    async load() {
      const gen = ++this.gen;
      this.pages.clear();
      const t = performance.now();
      try {
        const d = await getJson(this.url(Object.assign(this.params(0), { facets: 1 })));
        if (gen !== this.gen) return false;
        Object.assign(this, { total: d.total, groups: d.groups, langs: d.langs, loaded: true, error: null, took: performance.now() - t,
          names: shortNames(d.groups.map(([g]) => g)) });
        this.pages.set(0, d.items);
      } catch (e) {
        if (gen !== this.gen) return false;
        Object.assign(this, { total: 0, groups: [], langs: [], loaded: true, error: e, names: new Map() });
      }
      this.layout();
      return true;
    }
    // Folder headers come from the facet counts (search hits per group). With a language
    // filter the facets no longer describe the result: then the list is flat.
    layout() {
      let gs = this.groups;
      if (this.group != null) gs = gs.filter(([g]) => g === this.group || g.startsWith(this.group + '/'));
      const sum = gs.reduce((a, x) => a + x[1], 0);
      this.flat = this.kind === 'textures' || this.lang != null || sum !== this.total;
      this.gs = gs;
      this.rowAt = [];
      this.idxAt = [];
      let r = 0, it = 0;
      if (!this.flat) {
        for (const [g, n] of gs) {
          this.rowAt.push(r);
          this.idxAt.push(it);
          r += 1 + (this.collapsed.has(g) ? 0 : n);
          it += n;
        }
      }
      this.rows = this.flat ? this.total : r;
    }
    row(i) {                             // -> {h: group, n, gi, open} | {idx}
      if (this.flat) return { idx: i };
      const gi = bisect(this.rowAt, i), d = i - this.rowAt[gi], [g, n] = this.gs[gi];
      return d === 0 ? { h: g, n, gi, open: !this.collapsed.has(g) } : { idx: this.idxAt[gi] + d - 1 };
    }
    rowOf(idx) {
      if (this.flat) return idx;
      const gi = bisect(this.idxAt, idx);
      return this.collapsed.has(this.gs[gi][0]) ? this.rowAt[gi] : this.rowAt[gi] + 1 + idx - this.idxAt[gi];
    }
    groupOf(idx) { return this.flat ? null : this.gs[bisect(this.idxAt, idx)][0]; }
    item(idx) {
      const p = Math.floor(idx / PAGE), page = this.pages.get(p);
      if (Array.isArray(page)) return page[idx - p * PAGE] || null;
      if (page === undefined) this.fetchPage(p);
      return null;
    }
    async itemAsync(idx) {
      for (let k = 0; k < 3; k++) {
        const it = this.item(idx);
        if (it) return it;
        const p = this.pages.get(Math.floor(idx / PAGE));
        if (p && p.then) await p.catch(() => null); else await this.fetchPage(Math.floor(idx / PAGE));
      }
      return this.item(idx);
    }
    fetchPage(p) {
      const gen = this.gen;
      const job = getJson(this.url(this.params(p * PAGE))).then((d) => {
        if (gen !== this.gen) return;
        this.pages.set(p, d.items);
        if (this.onchange) this.onchange();
      }, () => { if (gen === this.gen) this.pages.set(p, []); });
      this.pages.set(p, job);
      return job;
    }
  }
  function bisect(a, v) {                // last index with a[i] <= v
    let lo = 0, hi = a.length - 1;
    while (lo < hi) { const m = (lo + hi + 1) >> 1; if (a[m] <= v) lo = m; else hi = m - 1; }
    return lo;
  }

  // ---- the shell: tabs, game switch, list pane ------------------------------------------

  const lists = {};                      // "game/kind" -> Listing
  let L = null;                          // the listing of the section on screen
  let gamesInfo = null;
  let deepLink = Q.has('id'), openDeep = false;   // ?id= opens that item once

  const listView = new VList($('list'), 44, renderRow);
  const folderView = listView;           // the texture section shows folders in the same pane
  listView.host.addEventListener('click', (e) => {
    const r = e.target.closest('.vrow');
    if (r) pickRow(+r.dataset.i, true);
  });

  function listing(g, kind) {
    const k = g + '/' + kind;
    if (!lists[k]) lists[k] = new Listing(g, kind);
    return lists[k];
  }

  function rowDetail(it) {
    switch (L.kind) {
      case 'videos':
        if (it.error) return ['unreadable', ''];
        return [it.width + '×' + it.height + ' · ' + dec(it.fps, it.fps % 1 ? 2 : 0) + ' fps · '
          + (it.has_audio ? it.tracks + (it.tracks === 1 ? ' audio track' : ' audio tracks') : 'sound in the sound banks'), tc(it.duration)];
      case 'sounds':
        return [(it.codec === 'pcm' ? 'PCM' : 'Vorbis') + ' · ' + chName(it.channels) + ' · ' + khz(it.sample_rate)
          + (it.lang ? ' · ' + langName(it.lang) : ''), it.duration < 10 ? tc(it.duration, true) : tc(it.duration)];
      case 'texts':
        return [[L.flat ? it.type : '', it.lang && langName(it.lang)].filter(Boolean).join(' · '), bytes(it.size)];
      case 'models':
        if (it.error) return ['unreadable', ''];
        // the server counts surfaces and triangles once a model was opened (null before)
        return [[it.format, it.surfaces != null && plural(it.surfaces, 'surface', 'surfaces'),
          it.tris != null && plural(it.tris, 'triangle', 'triangles')].filter(Boolean).join(' · '), it.size != null ? bytes(it.size) : ''];
      case 'scripts':
        return [plural(it.nodes, 'node', 'nodes') + ' · ' + plural(it.edges, 'wire', 'wires') + (it.missions ? ' · ' + plural(it.missions, 'mission node', 'mission nodes') : '')
          + (it.editable ? '' : ' · read-only'), ''];
      default:
        return ['', ''];
    }
  }

  function rowParts(d) {
    if (!d.firstChild) {
      d.append(el('i', 'ch'), el('div', 'tx'), el('span', 'mt mono'));
      d.children[1].append(el('span', 'nm'), el('span', 'sd mono'));
    }
    return { ch: d.children[0], nm: d.children[1].children[0], sd: d.children[1].children[1], mt: d.children[2] };
  }

  function renderRow(i, d) {
    const p = rowParts(d);
    if (L.kind === 'textures') {            // folders of the texture grid
      const g = i === 0 ? null : L.groups[i - 1];
      d.className = 'vrow folder' + ((g ? g[0] : null) === L.group ? ' sel' : '');
      p.ch.textContent = '';
      const name = g ? g[0] : 'All folders', [nm, parent] = g ? L.names.get(name) : [name, 'Search hits in all folders'];
      p.nm.textContent = nm;
      p.sd.textContent = parent;
      p.mt.textContent = num(g ? g[1] : L.groups.reduce((a, x) => a + x[1], 0));
      d.title = name;
      return;
    }
    const r = L.row(i);
    if (r.h != null) {
      d.className = 'vrow hd' + (r.open ? ' open' : '');
      const [nm, parent] = L.names.get(r.h) || [r.h, ''];
      p.ch.textContent = '';
      p.nm.textContent = nm;
      p.sd.textContent = parent;
      p.mt.textContent = num(r.n);
      d.title = r.h;
      return;
    }
    const it = L.item(r.idx);
    if (it && it.id === L.selId && L.selIdx < 0) L.selIdx = r.idx;   // after a new search: found again by id (not while stepping away)
    d.className = 'vrow' + (it && it.id === L.selId ? ' sel' : '') + (L.flat ? '' : ' in');
    p.ch.textContent = '';
    if (!it) { p.nm.textContent = '…'; p.sd.textContent = ''; p.mt.textContent = ''; d.title = ''; return; }
    const [sd, mt] = rowDetail(it);
    p.nm.textContent = it.name;
    p.sd.textContent = L.flat && it.group ? it.group + (sd ? ' · ' + sd : '') : sd;
    p.mt.textContent = mt;
    d.title = it.id;
    if (it.error) d.classList.add('bad');
  }

  function pickRow(i, user) {
    if (!L) return;
    if (L.kind === 'textures') {
      const g = i === 0 ? null : L.groups[i - 1][0];
      if (g === L.group && !Tex.opened) return;
      L.group = g;
      $('tpane').hidden = true;
      reloadGrid();
      return;
    }
    const r = L.row(i);
    if (r.h != null) {
      if (r.open) L.collapsed.add(r.h); else L.collapsed.delete(r.h);
      L.layout();
      listView.setRows(L.rows, true);
      foldLabel();
      return;
    }
    selectIdx(r.idx, user);
  }

  async function selectIdx(idx, user) {
    if (idx < 0 || idx >= L.total) return;
    const lst = L;
    const g = lst.groupOf(idx);
    if (g != null && lst.collapsed.has(g)) { lst.collapsed.delete(g); lst.layout(); listView.setRows(lst.rows, true); }
    lst.selIdx = idx;
    listView.reveal(lst.rowOf(idx));
    listView.draw();
    const it = await lst.itemAsync(idx);
    if (!it || lst !== L || lst.selIdx !== idx) return;
    openItem(it.id, user);
  }

  function openItem(id, user) {
    L.selId = id;
    listView.draw();
    syncUrl();
    if (section === 'video') Video.open(id);
    else if (section === 'audio') Audio.open(id, user);
    else if (section === 'texturen') Tex.open(id);
    else if (section === 'texte') Text.open(id);
    else if (section === 'scripts') Scripts.open(game, id);
    else if (section === 'modelle') Models.open(game, id);
  }

  function foldLabel() {
    const any = L && !L.flat && L.gs.some(([g]) => !L.collapsed.has(g));
    $('fold').textContent = any ? 'Collapse all' : 'Expand all';
    $('fold').hidden = !L || L.flat || L.kind === 'textures' || !L.gs.length;
  }
  $('fold').addEventListener('click', () => {
    if (!L || L.flat) return;
    const any = L.gs.some(([g]) => !L.collapsed.has(g));
    L.collapsed = any ? new Set(L.gs.map(([g]) => g)) : new Set();
    L.layout();
    listView.setRows(L.rows, true);
    if (!any && L.selIdx >= 0) listView.reveal(L.rowOf(L.selIdx));
    foldLabel();
  });

  function countLine() {
    const [one, many] = NOUN[L.kind];
    if (!L.loaded) return 'loading …';
    if (L.error) return 'Error';
    const n = L.total;
    if (!n && L.q) return 'No matches';
    let s = num(n) + ' ' + (n === 1 ? one : many);
    if (!L.flat && L.gs.length > 1) s += ' · ' + plural(L.gs.length, 'folder', 'folders');
    return s;
  }

  function langOptions() {
    const sel = $('lang');
    const langs = L.langs || [];
    sel.hidden = !(langs.length > 1 || (langs.length === 1 && langs[0][0]));
    if (sel.hidden) return;
    sel.textContent = '';
    const all = langs.reduce((a, x) => a + x[1], 0);
    sel.append(new Option('All languages (' + num(all) + ')', '\u0001'));
    for (const [lg, n] of langs) sel.append(new Option((lg ? langName(lg) : 'no language') + ' (' + num(n) + ')', lg));
    sel.value = L.lang == null ? '\u0001' : L.lang;
  }
  $('lang').addEventListener('change', (e) => {
    L.lang = e.target.value === '\u0001' ? null : e.target.value;
    reload();
  });

  let searchTimer = 0;
  $('q').addEventListener('input', (e) => {
    clearTimeout(searchTimer);
    const v = e.target.value;
    searchTimer = setTimeout(() => { if (L && L.q !== v) { L.q = v; reload(); } }, 120);
  });
  $('q').addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown' && L && L.kind !== 'textures') { e.preventDefault(); $('list').focus(); step(1); }
    if (e.key === 'Enter' && L && L.kind !== 'textures' && L.total) { e.preventDefault(); selectIdx(Math.max(0, L.selIdx), true); }
  });

  async function reload() {
    const lst = L;
    $('count').textContent = 'searching …';
    const done = await lst.load();
    if (!done || lst !== L) return;
    lst.selIdx = -1;                     // found again by id when its row is drawn
    showList(false);
    if (lst.kind === 'textures') reloadGrid(true);
  }

  function showList(keepScroll) {
    $('count').textContent = countLine();
    $('count').title = L.took ? 'Search ' + Math.round(L.took) + ' ms' : '';
    langOptions();
    foldLabel();
    if (L.error) {
      listView.setRows(0);
      showErr(L.error.message);
      return;
    }
    listView.setRows(L.kind === 'textures' ? L.groups.length + 1 : L.rows, keepScroll);
    if (keepScroll) listView.host.scrollTop = L.scroll;
    if (!L.total && !L.selId) emptyInfo(L.q ? 'No matches for "' + L.q + '".' : 'Nothing found.');
  }

  async function openSection() {
    stopMedia();
    const kind = KIND[section];
    if (L) L.scroll = listView.host.scrollTop;
    L = listing(game, kind);
    L.onchange = () => { if (L.kind === 'textures') Tex.draw(); else listView.draw(); };
    $('q').value = L.q;
    $('q').placeholder = { videos: 'Search videos …', sounds: 'Search sounds (' + soundFields() + ') …', textures: 'Search textures …', texts: 'Search texts (name, type) …',
      models: 'Search models (name, folder) …', scripts: 'Search scripts (name, map) …' }[kind];
    $('side').dataset.kind = kind;
    $('studio').dataset.kind = kind;
    hidePanes();
    if (!L.loaded) {
      listView.setRows(0);
      $('count').textContent = 'loading …';
      emptyInfo(NOUN[kind][1] + ' are being read … (a few seconds the first time)', true);
      const lst = L;
      await lst.load();
      if (lst !== L) return;
      if (deepLink && Q.get('s') === section && (Q.get('game') || game) === game) { L.selId = Q.get('id'); deepLink = false; openDeep = true; }
    }
    showList(true);
    const deep = openDeep && L.selId ? findSel(L) : null;
    openDeep = false;
    if (kind === 'textures') {
      reloadGrid(true);
      if (deep) { Tex.open(L.selId); deep.then((i) => { if (i >= 0) Tex.mark(i); }); }
      return;
    }
    if (L.selId) openItem(L.selId, false);
    else if (L.total && !L.error) emptyInfo();   // else showList already said "Keine Treffer" or showed the error
  }

  // an item opened by ?id=: find its place in the list, mark it and scroll it into the middle
  async function findSel(lst) {
    let d;
    try {
      d = await getJson(lst.url(Object.assign(lst.params(0), { limit: 1, find: lst.selId })));
    } catch (e) {
      return -1;                           // the item opens anyway
    }
    if (lst !== L || d.index < 0) return -1;
    lst.selIdx = d.index;
    if (lst.kind !== 'textures') {
      listView.reveal(lst.rowOf(d.index), true);
      listView.draw();
    }
    return d.index;
  }

  const soundFields = () => (game === 'tno' ? 'name, folder' : 'ID, bank, folder');   // TNO has no sound banks

  function step(dir) {
    if (!L || !L.total || L.kind === 'textures') return;
    selectIdx(Math.min(L.total - 1, Math.max(0, (L.selIdx < 0 ? (dir > 0 ? -1 : 0) : L.selIdx) + dir)), true);
  }
  $('list').addEventListener('keydown', (e) => {
    if (L && L.kind === 'textures') return;
    const k = { ArrowDown: 1, ArrowUp: -1, PageDown: 12, PageUp: -12 }[e.key];
    if (k) { e.preventDefault(); step(k); }
    if (e.key === 'Home') { e.preventDefault(); selectIdx(0, true); }
    if (e.key === 'End') { e.preventDefault(); selectIdx(L.total - 1, true); }
  });

  // ---- stage: panes, errors, empty state ---------------------------------------------------

  function hidePanes() {
    for (const id of ['vpane', 'apane', 'tpane', 'xpane', 'mpane', 'kpane', 'err', 'empty', 'grid']) $(id).hidden = true;
  }
  function showPane(id) {
    hidePanes();
    $(id).hidden = false;
  }
  function showErr(text) {
    const keepGrid = section === 'texturen' && !$('grid').hidden && $('tpane').hidden;
    hidePanes();
    if (keepGrid) $('grid').hidden = false;
    $('err').querySelector('p').textContent = text;
    $('err').hidden = false;
    $('err').dataset.text = text;
  }
  function emptyInfo(text, busy) {
    if (section === 'texturen' && !text) return;   // the grid is the texture section's resting view
    hidePanes();
    $('empty').hidden = false;
    const box = $('empty');
    box.textContent = '';
    box.classList.toggle('busy', !!busy);
    if (busy) box.append(el('i', 'spin'));
    const kind = KIND[section];
    box.append(el('h2', null, text || (L && L.total ? NOUN[kind][1] + ' of ' + GAMES[game] : '')));
    if (!text && L && L.total) {
      box.append(el('p', 'big mono', countLine()));
      const tips = {
        videos: ['Pick a video on the left. Space plays, ←/→ jumps 5 s, F for fullscreen.',
          'Far behind a keyframe, the timeline jumps back to that keyframe (ticks = keyframes); Shift+click jumps exactly.'],
        sounds: ['Pick a sound on the left: it plays at once. ↑/↓ browses and plays the next one.',
          'Several search words: all must match (' + soundFields() + ').'],
        texts: ['Pick a text on the left. Search in the text, copy lines or everything.'],
        models: ['Pick a model on the left: it stands in its base pose with its textures. Drag rotates, mouse wheel zooms, right mouse button pans.',
          'On the right, show and hide surfaces (characters carry variants such as gore parts) and try your own PNG as a skin.'],
        scripts: ['Pick a script on the left: its nodes appear as a graph. Drag pans, mouse wheel zooms, click selects.',
          'The Missions button shows only mission and objective nodes and the flow between them. Edits are saved as a mod.'],
      }[kind] || [];
      for (const t of tips) box.append(el('p', 'dim', t));
    }
  }

  // ---- sections and games ------------------------------------------------------------------

  function showSection(s) {
    if (!(s in SECTIONS)) return;
    if (L && section !== 'karten') L.scroll = listView.host.scrollTop;
    section = s;
    B.dataset.section = s;
    store.set('section', s);
    for (const b of document.querySelectorAll('#tabs button')) b.setAttribute('aria-selected', String(b.dataset.s === s));
    $('studio').hidden = s === 'karten';
    if (s !== 'karten') document.title = SECTIONS[s] + ' · Wolfenstein Studio';
    document.dispatchEvent(new CustomEvent('studio:section', { detail: s }));
    if (s === 'karten') stopMedia(); else openSection();
    syncUrl();
  }

  function setGame(g, quiet) {
    if (!(g in GAMES)) return;
    for (const r of document.querySelectorAll('input[name=game]')) r.checked = r.value === g;
    $('gamename').textContent = GAMES[g];
    if (g === game && !quiet) return;
    const changed = g !== game;
    game = g;
    B.dataset.game = g;
    store.set('game', g);
    if (!changed) return;
    document.dispatchEvent(new CustomEvent('studio:game', { detail: g }));
    if (section !== 'karten') { L = null; openSection(); }
    syncUrl();
  }

  function syncUrl() {
    const p = new URLSearchParams();
    p.set('s', section);
    p.set('game', game);
    if (section !== 'karten' && L && L.selId) p.set('id', L.selId);
    if (section === 'karten' && Q.has('shot')) return;   // the map viewer's headless runs keep their URL
    try { history.replaceState(null, '', '?' + p); } catch (e) { /* file:// or sandbox */ }
  }

  for (const b of document.querySelectorAll('#tabs button')) b.addEventListener('click', () => showSection(b.dataset.s));
  for (const r of document.querySelectorAll('input[name=game]')) r.addEventListener('change', () => setGame(r.value));

  async function loadGames() {
    try {
      gamesInfo = await getJson('/api/games');
    } catch (e) {
      gamesInfo = null;                   // an older server: maps of Wolfenstein II only
      return;
    }
    for (const gi of gamesInfo) {
      const r = document.querySelector('input[name=game][value=' + gi.id + ']');
      if (!r) continue;
      r.disabled = !gi.available;
      r.parentElement.title = gi.name + (gi.available ? '' : ' – not found');
    }
    const cur = gamesInfo.find((x) => x.id === game);
    if (cur && !cur.available) {
      const other = gamesInfo.find((x) => x.available);
      if (other) { setGame(other.id); toast(GAMES[cur.id] + ' was not found.'); }
    }
  }

  // fullscreen for the whole page (the map viewer has its own button in its tool row)
  const browserFullscreen = () => !document.fullscreenElement && innerHeight >= screen.height - 1 && innerWidth >= screen.width - 1;
  function pageFullscreen() {
    if (document.fullscreenElement) { document.exitFullscreen().catch(() => {}); return true; }
    if (browserFullscreen()) { toast('Browser fullscreen: press F11 to leave'); return false; }
    document.documentElement.requestFullscreen().catch(() => toast('The browser refused fullscreen'));
    return true;
  }
  $('sfs').addEventListener('click', pageFullscreen);
  const fsLabel = () => { $('sfs').textContent = document.fullscreenElement || browserFullscreen() ? 'Window' : 'Fullscreen'; };
  document.addEventListener('fullscreenchange', fsLabel);
  window.addEventListener('resize', fsLabel);

  function stopMedia() {
    Video.stop();
    Audio.stop();
    Models.stop();
  }

  // ---- video ---------------------------------------------------------------------------------

  const Video = (() => {
    const cv = $('vcanvas'), cx = cv.getContext('2d', { alpha: false }), au = $('vaudio');
    const seekEl = $('vseek');
    let info = null, id = null, gen = 0;
    let fps = 30, nf = 0, pos = 0, playing = false, waiting = '';
    let st = null;                        // the running frame stream
    let wall = { t0: 0, s0: 0 };          // wall clock, kept in step with the audio clock
    let audioOn = false;                  // the video has sound and the <audio> is usable
    let drawn = 0, lastDraw = 0, seeking = null, seeks = 0;
    const pool = [];

    au.volume = +(store.get('volume') || 0.8);
    $('vvol').value = au.volume;

    function clock() {
      const m = bank ? bank.master.el : audioOn ? au : null;   // bank sound: its longest start-0 layer is the clock
      if (m && !m.paused && !m.ended && m.readyState >= 2) {
        const t = m.currentTime + (bank ? bank.master.l.start : 0);
        wall = { t0: performance.now(), s0: t };
        return t;
      }
      return wall.s0 + (performance.now() - wall.t0) / 1000;
    }

    function stop() {
      gen++;
      playing = false;
      closeStream();
      au.pause();
      if (au.getAttribute('src')) { au.removeAttribute('src'); au.load(); }
      bankOff();
      document.exitFullscreen && document.fullscreenElement === $('vplayer') && document.exitFullscreen().catch(() => {});
      setWait('');
      ui();
    }

    function closeStream() {
      if (!st) return;
      st.ctl.abort();
      for (const f of st.q) pool.push(f.buf);
      if (st.wake) st.wake();
      st = null;
    }

    function setWait(text) {
      waiting = text;
      $('vwait').hidden = !text;
      $('vwait').querySelector('span').textContent = text;
      $('vbig').hidden = playing || !info || !!text;
    }

    async function open(vid) {
      stop();
      const my = ++gen;
      id = vid;
      showPane('vpane');
      $('vtitle').textContent = vid.split('/').pop();
      $('vsub').textContent = vid;
      $('vfacts').textContent = '';
      $('vnote').hidden = true;
      $('vtracks').hidden = true;
      cx.fillStyle = '#000';
      cx.fillRect(0, 0, cv.width, cv.height);
      setWait('Opening video …');
      try {
        info = await getJson(api('videos', 'info', { id: vid }));
      } catch (e) {
        if (my === gen) { setWait(''); showErr(e.message); }
        return;
      }
      if (my !== gen) return;
      if (info.error) { setWait(''); showErr('Video unreadable: ' + info.error); return; }
      $('vfile').href = api('videos', 'file', { id: vid });   // assetio: the .bk2 itself, and its Bink audio
      $('vwav').hidden = !(info.tracks && info.tracks.length);
      $('vwav').href = api('videos', 'audio.wav', { id: vid });
      $('vwav').download = vid.split('/').pop().replace(/\.\w+$/, '') + '.wav';
      fps = info.fps_num / info.fps_den;
      nf = info.frames;
      pos = 0;
      drawn = 0;
      wall = { t0: performance.now(), s0: 0 };
      $('vtitle').textContent = info.name;
      const keys = info.keyframes || [0];
      const gap = keys.length > 1 ? Math.max(...keys.slice(1).concat(nf).map((k, i) => k - keys[i])) / fps : 0;
      facts($('vfacts'), [
        ['Resolution', info.width + ' × ' + info.height],
        ['Frame rate', dec(fps, fps % 1 ? 3 : 0) + ' fps'],
        ['Duration', tc(info.duration, true) + ' (' + num(nf) + ' frames)'],
        ['Keyframes', num(keys.length) + (gap ? ' · largest gap ' + dec(gap, 1) + ' s' : ' · only at the start')],
        ['Audio tracks', info.tracks.length ? trackCount(info.tracks) : 'none in the video'],
        ['File', bytes(info.size) + ' · ' + (info.id.endsWith('.bk2') ? 'Bink 2' : 'Bink') + ', Rev. ' + (info.revision || '?')],
      ]);
      buildSeekKeys(keys);
      buildTracks();
      if (info.audio_note) {
        $('vnote').hidden = false;
        $('vnote').textContent = '';
        $('vnote').append(el('b', null, 'Sound is in the sound banks'), el('span', null, info.audio_note));
      }
      bankSet(bankLang);
      bankNote();
      audioOn = info.tracks.length > 0;
      if (audioOn) setAudio(currentTracks());
      // poster: a still from the stream's own scaling (a keyframe, instant)
      try {
        const img = new Image();
        img.src = api('videos', 'frame.png', { id: vid, n: info.poster, w: VIDEO_W });
        await img.decode();
        if (my !== gen) return;
        cv.width = img.naturalWidth;
        cv.height = img.naturalHeight;
        cx.drawImage(img, 0, 0);
        drawn = -1;                        // the poster, not a played frame
      } catch (e) {
        if (my !== gen) return;
      }
      setWait('');
      ui();
    }

    function trackCount(tr) {             // "11: 4 Stereo, 7 Mono"
      const by = {};
      for (const t of tr) by[chName(t.channels)] = (by[chName(t.channels)] || 0) + 1;
      return tr.length + (tr.length > 1 ? ': ' + Object.entries(by).map(([k, n]) => n + ' ' + k).join(', ') : ' (' + chName(tr[0].channels) + ')');
    }

    function currentTracks() {
      const box = $('vtracks').querySelector('.menubox');
      if ($('vtracks').hidden) return info.default_tracks;
      return [...box.querySelectorAll('input:checked')].map((i) => +i.value).filter((v) => v >= 0);
    }

    function buildTracks() {
      const box = $('vtracks').querySelector('.menubox');
      box.textContent = '';
      const tr = info.tracks;
      $('vtracks').hidden = tr.length < 2;
      if (tr.length < 2) return;
      const def = new Set(info.default_tracks);
      const stereo = tr.filter((t) => t.channels !== 1), mono = tr.filter((t) => t.channels === 1);
      if (stereo.length) {
        box.append(el('div', 'cap', 'Music & effects'));
        for (const t of stereo) {
          const l = el('label', 'check');
          const i = el('input');
          i.type = 'checkbox';
          i.value = t.id;
          i.checked = def.has(t.id);
          l.append(i, el('span', null, t.label));
          box.append(l);
        }
      }
      if (mono.length) {
        box.append(el('div', 'cap', 'Voice (language per track unknown)'));
        for (const t of mono.concat({ id: -1, label: 'no voice' })) {
          const l = el('label', 'check');
          const i = el('input');
          i.type = 'radio';
          i.name = 'voice';
          i.value = t.id;
          i.checked = t.id === -1 ? !mono.some((m) => def.has(m.id)) : def.has(t.id);
          l.append(i, el('span', null, t.label));
          box.append(l);
        }
      }
      box.onchange = () => {
        const ids = currentTracks();
        trackSummary(ids);
        if (!ids.length) { audioOn = false; au.pause(); au.removeAttribute('src'); au.load(); toast('No sound: no track selected'); return; }
        audioOn = true;
        const at = pos / fps, was = playing;
        pause();
        setAudio(ids, at).then(() => { if (was) play(); });
      };
      trackSummary(info.default_tracks);
    }
    function trackSummary(ids) {
      $('vtracks').querySelector('summary').textContent = 'Audio tracks ' + ids.length + '/' + info.tracks.length;
    }

    async function setAudio(ids, at) {
      const my = gen;
      au.src = api('videos', 'audio.wav', { id, tracks: ids.join(',') });
      au.currentTime = at || 0;
      au.load();
      if (at) au.currentTime = at;
      return new Promise((res) => {
        const done = () => { au.removeEventListener('canplay', done); au.removeEventListener('error', bad); res(); };
        const bad = () => {
          done();
          if (my !== gen) return;
          audioOn = false;
          toast('Sound could not be loaded – the video plays on without sound.', 5000);
        };
        au.addEventListener('canplay', done);
        au.addEventListener('error', bad);
      });
    }

    // -- the WVF1 stream -----------------------------------------------------------------

    function startStream(start, mode) {
      closeStream();
      const s = { ctl: new AbortController(), q: [], done: false, first: null, next: 0, wake: null, err: null, head: null };
      st = s;
      s.ready = (async () => {
        const r = await fetchOk(api('videos', 'frames', { id, start, w: VIDEO_W, seek: mode }), { signal: s.ctl.signal });
        const rd = r.body.getReader();
        const hb = new Uint8Array(32);
        let hn = 0, fb = 0, cur = null, fn = 0, W = 0, H = 0;
        let resolveHead;
        s.head = new Promise((ok) => { resolveHead = ok; });
        (async () => {
          try {
            for (;;) {
              const { value, done } = await rd.read();
              if (done) break;
              let p = 0;
              while (p < value.length) {
                if (hn < 32) {
                  const k = Math.min(32 - hn, value.length - p);
                  hb.set(value.subarray(p, p + k), hn);
                  hn += k;
                  p += k;
                  if (hn === 32) {
                    const dv = new DataView(hb.buffer);
                    if (String.fromCharCode(hb[0], hb[1], hb[2], hb[3]) !== 'WVF1') throw new Error('Frame stream without WVF1 header');
                    W = dv.getUint16(4, true);
                    H = dv.getUint16(6, true);
                    s.first = s.next = dv.getUint32(8, true);
                    s.count = dv.getUint32(12, true);
                    fb = W * H * 4;
                    s.W = W;
                    s.H = H;
                    resolveHead(s);
                  }
                  continue;
                }
                if (!cur) { cur = pool.pop(); if (!cur || cur.length !== fb) cur = new Uint8ClampedArray(fb); }
                const k = Math.min(fb - fn, value.length - p);
                cur.set(value.subarray(p, p + k), fn);
                fn += k;
                p += k;
                if (fn === fb) {
                  s.q.push({ n: s.next++, buf: cur, img: new ImageData(cur, W, H) });
                  cur = null;
                  fn = 0;
                  while (s.q.length >= 24 && st === s) await new Promise((w) => { s.wake = w; });
                  if (st !== s) return;
                }
              }
            }
          } catch (e) {
            if (e.name !== 'AbortError') s.err = e;
            resolveHead(s);
          } finally {
            s.done = true;
          }
        })();
        return s.head;
      })();
      s.ready.catch((e) => { if (e.name !== 'AbortError') s.err = e; s.done = true; });
      return s;
    }

    function recycle(f) { if (pool.length < 30) pool.push(f.buf); }

    function draw(f) {
      if (cv.width !== st.W || cv.height !== st.H) { cv.width = st.W; cv.height = st.H; }
      cx.putImageData(f.img, 0, 0);
      pos = f.n;
      drawn = Math.max(drawn, 0) + 1;     // the poster (-1) is no played frame
      lastDraw = performance.now();
    }

    // wait until the stream has `n` frames (or ended); true when it did
    async function preroll(s, n) {
      try { await s.ready; } catch (e) { return false; }   // s.err says why
      const t = performance.now();
      while (st === s && !s.done && s.q.length < n) {
        if (performance.now() - t > 60000) return false;
        await new Promise((r) => setTimeout(r, 15));
      }
      return st === s && !s.err;
    }

    async function play() {
      if (!info || playing || info.error) return;
      const my = gen;
      if (bank && bankCtx.state !== 'running') bankCtx.resume().catch(() => {});   // still inside the click
      // go on with the stream when it continues the picture on screen, else start one there
      let from = drawn > 0 ? pos + 1 : 0;
      if (pos >= nf - 1) from = 0;
      else if (drawn > 0 && st && !st.err && (st.q.length ? st.q[0].n : st.next) === pos + 1) from = null;
      if (from != null) startStream(from, 'exact');
      const s = st;
      setWait('Decoding frames …');
      const ok = await preroll(s, 6);
      if (my !== gen || st !== s) return;
      if (!ok) { setWait(''); if (s.err) showErrInPlayer(s.err.message); return; }
      const t0 = (s.q.length ? s.q[0].n : pos) / fps;
      if (audioOn) {
        setWait(au.readyState >= 3 ? '' : 'Preparing sound …');
        au.currentTime = t0;
        try {
          await au.play();
        } catch (e) {
          if (my !== gen) return;
          if (e.name === 'NotAllowedError') { setWait(''); toast('The browser needs a click before it plays.'); return; }
          audioOn = false;
        }
        if (my !== gen) { au.pause(); return; }
      }
      if (bank) {
        const b = bank;
        setWait('Preparing sound …');
        await Promise.all(b.layers.map((y) => y.ready));
        if (my !== gen || bank !== b) return;
        if (!b.layers.some((y) => y.ok)) toast('Sound could not be loaded – the video plays without sound.', 5000);
        bankSync(t0, true, 0.02);
      }
      wall = { t0: performance.now(), s0: t0 };
      playing = true;
      setWait('');
      ui();
    }

    function pause() {
      if (!playing) return;
      playing = false;
      au.pause();
      if (bank) bankSync(pos / fps, false, 1e9);
      wall = { t0: performance.now(), s0: pos / fps };
      ui();
    }

    function toggle() { if (playing) pause(); else play(); }

    function showErrInPlayer(text) { toast('Video: ' + text, 6000); }

    // seek to frame n. seek=auto (the server): exact while that decodes at most auto_exact frames
    // past the keyframe (~1-2 s), else the keyframe itself; Shift asks for exact anyway.
    async function seek(n, exact) {
      if (!info) return;
      n = Math.max(0, Math.min(nf - 1, Math.round(n)));
      const my = gen, was = playing || (seeking && seeking.was);
      const keys = info.keyframes || [0];
      const key = keys.filter((k) => k <= n).pop() || 0;
      const decode = exact || n - key <= info.auto_exact ? n - key : 0;
      playing = false;
      au.pause();
      if (bank) bankSync(n / fps, false, 1e9);
      seeking = { n, was };
      const s = startStream(n, exact ? 'exact' : 'auto');
      setWait(decode > 60 ? 'Jumping to ' + tc(n / fps) + ' – decoding ' + num(decode) + ' frames from the keyframe …' : 'Jumping …');
      const ok = await preroll(s, 1);
      if (my !== gen || st !== s) return;
      seeking = null;
      setWait('');
      if (!ok || !s.q.length) { if (s.err) showErrInPlayer(s.err.message); return; }
      const f = s.q.shift();
      draw(f);
      recycle(f);
      wall = { t0: performance.now(), s0: pos / fps };
      if (audioOn) au.currentTime = pos / fps;
      if (bank) bankSync(pos / fps, false, 0.02);
      if (s.first !== n) toast('Jumped to the keyframe: ' + tc(s.first / fps) + ' (frame ' + num(s.first) + ') – Shift+click jumps exactly');
      seeks++;
      ui();
      if (was) play();
    }

    function tick() {
      requestAnimationFrame(tick);
      if (!playing || !st) return;
      const t = clock();
      const target = Math.floor(t * fps + 1e-3);
      let f = null;
      while (st.q.length && st.q[0].n <= target) {
        if (f) recycle(f);
        f = st.q.shift();
      }
      if (f) { draw(f); recycle(f); if (st.wake) { const w = st.wake; st.wake = null; w(); } }
      if (bank && performance.now() - bank.synced > 500) { bank.synced = performance.now(); bankSync(t, true, 0.07); }
      if (st.done && !st.q.length && (st.err || target >= nf - 1 || st.next >= nf)) {
        playing = false;
        au.pause();
        if (bank) bankSync(t, false, 1e9);
        if (st.err) showErrInPlayer(st.err.message); else pos = nf - 1;
        ui();
        return;
      }
      // frames late while the sound runs on: say so after a moment (the sound keeps the clock)
      const late = !f && !st.q.length && !st.done && performance.now() - lastDraw > 400;
      if (late !== (waiting === 'Buffering …')) setWait(late ? 'Buffering …' : '');
      if (!seekDrag) ui(t);
    }
    requestAnimationFrame(tick);

    function ui(t) {
      const cur = t != null ? Math.min(t, nf / fps) : pos / fps;
      const dur = nf / fps;
      $('vtime').textContent = tc(cur) + ' / ' + tc(dur);
      const frac = dur ? Math.min(1, cur / dur) : 0;
      seekEl.style.setProperty('--p', frac);
      seekEl.setAttribute('aria-valuemax', String(Math.round(dur)));
      seekEl.setAttribute('aria-valuenow', String(Math.round(cur)));
      seekEl.setAttribute('aria-valuetext', tc(cur) + ' of ' + tc(dur));
      $('vpane').dataset.frame = pos;
      if ($('vpane').dataset.playing === (playing ? '1' : '0') && $('vbig').hidden === (playing || !info || !!waiting)) return;
      $('vplay').innerHTML = playing ? '&#10074;&#10074;' : '&#9654;';
      $('vplay').setAttribute('aria-label', playing ? 'Pause' : 'Play');
      $('vbig').hidden = playing || !info || !!waiting;
      $('vpane').dataset.playing = playing ? '1' : '0';
    }

    function buildSeekKeys(keys) {
      const box = seekEl.querySelector('.keys');
      box.textContent = '';
      if (keys.length > 400 || nf < 2) return;
      for (const k of keys) {
        const i = el('i');
        i.style.left = (k / (nf - 1) * 100) + '%';
        box.append(i);
      }
    }

    // seek bar: drag shows the target and where it will land, release jumps
    let seekDrag = null;
    const frac = (e) => { const r = seekEl.getBoundingClientRect(); return Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)); };
    function tip(e) {
      if (!info) return;
      const f = frac(e), n = Math.round(f * (nf - 1));
      const key = (info.keyframes || [0]).filter((k) => k <= n).pop() || 0;
      const t = $('vseek').querySelector('.tip');
      t.hidden = false;
      t.style.left = f * 100 + '%';
      t.textContent = tc(n / fps) + (n - key > info.auto_exact && !e.shiftKey ? ' → Keyframe ' + tc(key / fps) : '');
    }
    seekEl.addEventListener('pointerdown', (e) => {
      if (!info) return;
      seekEl.setPointerCapture(e.pointerId);
      seekDrag = { shift: e.shiftKey };
      seekEl.style.setProperty('--p', frac(e));
      tip(e);
    });
    seekEl.addEventListener('pointermove', (e) => {
      if (!info) return;
      tip(e);
      if (seekDrag) { seekEl.style.setProperty('--p', frac(e)); $('vtime').textContent = tc(frac(e) * nf / fps) + ' / ' + tc(nf / fps); }
    });
    seekEl.addEventListener('pointerleave', () => { if (!seekDrag) seekEl.querySelector('.tip').hidden = true; });
    seekEl.addEventListener('pointerup', (e) => {
      if (!seekDrag) return;
      const shift = seekDrag.shift || e.shiftKey;
      seekDrag = null;
      seekEl.querySelector('.tip').hidden = true;
      seek(frac(e) * (nf - 1), shift);
    });
    seekEl.addEventListener('keydown', (e) => {
      const d = { ArrowLeft: -5, ArrowRight: 5, PageDown: -30, PageUp: 30 }[e.key];
      if (d) { e.preventDefault(); e.stopPropagation(); jump(d); }
    });
    function jump(sec) { if (info) seek(pos + sec * fps); }

    $('vplay').addEventListener('click', toggle);
    $('vbig').addEventListener('click', toggle);
    cv.addEventListener('click', toggle);
    $('vvol').addEventListener('input', (e) => { au.volume = +e.target.value; au.muted = false; store.set('volume', au.volume); muteIcon(); bankVol(); });
    function muteIcon() {
      $('vmute').innerHTML = au.muted || au.volume === 0 ? '&#128263;' : '&#128266;';
      $('vmute').setAttribute('aria-label', au.muted ? 'Sound on' : 'Sound off');
    }
    function mute() { au.muted = !au.muted; muteIcon(); bankVol(); }
    $('vmute').addEventListener('click', mute);
    function fullscreen() {
      if (document.fullscreenElement === $('vplayer')) document.exitFullscreen().catch(() => {});
      else $('vplayer').requestFullscreen().catch(() => toast('The browser refused fullscreen'));
    }
    $('vfs').addEventListener('click', fullscreen);
    cv.addEventListener('dblclick', fullscreen);
    au.addEventListener('waiting', () => { if (playing) setWait('Sound loading …'); });
    au.addEventListener('playing', () => { if (waiting === 'Sound loading …') setWait(''); });
    muteIcon();

    // -- cutscene sound from the Wwise banks (info.bank_audio, wolfsdk/cutsceneaudio.py) --------------
    // One <audio> per layer (/api/tnc/sounds/audio), mixed in Web Audio: 5.1 WAVE-order layers through
    // Audio.waveGraph, per-layer gain relative to the loudest, a master gain = the volume slider.
    // All layers start with frame 0 plus their Play delay (measured +0.01 s against the embedded mixes).
    let bank = null, bankCtx = null, bankMaster = null, bankScope = null;
    let bankLang = store.get('bankLang') || 'english(us)';
    function bankVol() { if (bankMaster) bankMaster.gain.value = au.muted ? 0 : au.volume; }
    function bankOff() {
      if (!bank) return;
      for (const y of bank.layers) { y.el.pause(); y.el.removeAttribute('src'); y.el.load(); y.out.disconnect(); }
      bank = null;
    }
    function bankSet(lang) {
      bankOff();
      const ba = info && info.bank_audio;
      if (!ba || !ba.langs) return;
      if (!bankCtx) {
        try {
          bankCtx = new AudioContext();
          bankMaster = bankCtx.createGain();
          bankScope = bankCtx.createAnalyser();
          bankMaster.connect(bankCtx.destination);
          bankMaster.connect(bankScope);
          const d = bankCtx.destination;
          d.channelCount = Math.min(6, d.maxChannelCount) >= 6 ? 6 : Math.min(2, d.maxChannelCount);
          bankVol();
        } catch (e) { bankCtx = null; return; }
      }
      lang = ba.langs[lang] ? lang : ba.default;
      const ls = ba.langs[lang], top = Math.max(...ls.map((l) => l.gain_db));
      bank = { lang, layers: [], synced: 0 };
      for (const l of ls) {
        const a = new window.Audio();
        a.preload = 'auto';
        a.src = api('sounds', 'audio', { id: l.sound });
        const src = bankCtx.createMediaElementSource(a), out = bankCtx.createGain();
        out.gain.value = Math.pow(10, (l.gain_db - top) / 20);
        (l.channel_order === 'wave' && l.channels === 6 ? Audio.waveGraph(bankCtx, src) : src).connect(out);
        out.connect(bankMaster);
        const y = { l, el: a, out, ok: true };
        y.ready = new Promise((res) => {
          a.addEventListener('canplay', () => res(true), { once: true });
          a.addEventListener('error', () => { y.ok = false; res(false); }, { once: true });
        });
        bank.layers.push(y);
      }
      bank.master = bank.layers.filter((y) => y.l.start === 0).sort((p, q) => q.l.duration - p.l.duration)[0] || bank.layers[0];
    }
    // every layer to video time t (off by more than tol); run: layers inside their span play, the rest wait
    function bankSync(t, run, tol) {
      for (const y of bank.layers) {
        if (!y.ok) continue;
        const want = t - y.l.start, inside = want >= 0 && want < y.l.duration;
        if (inside && Math.abs(y.el.currentTime - want) > tol) y.el.currentTime = want;
        if (run && inside && y.el.paused) y.el.play().catch(() => {});
        else if ((!run || !inside) && !y.el.paused) y.el.pause();
      }
    }
    function bankNote() {
      const ba = info.bank_audio;
      if (!ba) return;
      const n = $('vnote');
      if (!bank) { n.append(el('span', null, ' ' + (ba.reason || 'No cutscene sound found.'))); return; }
      n.textContent = '';
      n.append(el('b', null, 'Cutscene sound from the sound banks'), el('span', null, ba.note));
      const langs = Object.keys(ba.langs);
      if (langs.length > 1) {
        const sel = el('select');
        for (const lg of langs) sel.append(new Option(langName(lg), lg, false, lg === bank.lang));
        sel.onchange = () => {
          bankLang = sel.value;
          store.set('bankLang', bankLang);
          const was = playing;
          pause();
          bankSet(bankLang);
          bankNote();
          if (was) play();
        };
        n.append(' ', sel);
      }
      for (const y of bank.layers) {
        const row = el('span');
        row.style.display = 'block';
        const a = el('a', null, y.l.role);
        a.href = '?s=audio&game=tnc&id=' + encodeURIComponent(y.l.sound);
        const lg = y.l.sound.split('/')[1];
        row.append(a, ' · ' + [lg === 'sfx' ? 'no language' : langName(lg), chName(y.l.channels), tc(y.l.duration),
          y.l.start ? 'starts at ' + dec(y.l.start, 1) + ' s' : 'starts with the video', dec(y.l.gain_db, 1) + ' dB', y.l.sound].join(' · '));
        n.append(row);
      }
    }
    function bankState() {
      if (!bank) return info && info.bank_audio ? { reason: info.bank_audio.reason || null } : null;
      let rms = 0;
      if (bankScope) {
        const d = new Float32Array(bankScope.fftSize);
        bankScope.getFloatTimeDomainData(d);
        rms = Math.sqrt(d.reduce((s, x) => s + x * x, 0) / d.length);
      }
      return { event: info.bank_audio.event, lang: bank.lang, playing: bank.layers.some((y) => !y.el.paused), rms,
        ctx: bankCtx && bankCtx.state, master: bank.master.l.sound,
        layers: bank.layers.map((y) => ({ sound: y.l.sound, media: y.l.media, role: y.l.role, start: y.l.start,
          time: y.el.currentTime, paused: y.el.paused, ready: y.el.readyState, ok: y.ok })) };
    }

    function state() {
      return { id, playing, pos, frames: nf, fps, drawn, seeks, audio: audioOn, audioTime: au.currentTime, audioPaused: au.paused,
        first: st && st.first, waiting, tracks: info && !$('vtracks').hidden ? currentTracks() : null, bank: bankState(),
        note: $('vnote').hidden ? null : $('vnote').textContent, canvas: [cv.width, cv.height] };
    }
    return { open, stop, play, pause, toggle, seek, jump, mute, fullscreen, state, active: () => !!info && section === 'video' };
  })();

  // ---- audio ---------------------------------------------------------------------------------

  const Audio = (() => {
    const plain = $('aaudio');
    const wave = new window.Audio();        // 5.1 sounds in WAVE order: through the remap graph
    wave.preload = 'auto';
    let au = plain, info = null, id = null, gen = 0, ctx = null, gain = null, src = null;
    let lanes = null, laneNames = [], waveState = 'leer', route = 'direkt', peak = 1;
    const cvs = $('acanvas');
    const vol = +(store.get('volume') || 0.8);
    plain.volume = vol;
    $('avol').value = vol;

    // decoded channel j of an Edge decode -> speaker order of the file's WAVE mask
    function speakerChannels(buf, order) {
      const ch = [];
      for (let c = 0; c < buf.numberOfChannels; c++) ch.push(buf.getChannelData(c));
      return order === 'wave' && ch.length === 6 ? WAVE_FROM_EDGE.map((k) => ch[k]) : ch;
    }
    // the playback graph for a WAVE-ordered 5.1 source: splitter -> merger with the measured permutation
    function waveGraph(c, source) {
      const sp = c.createChannelSplitter(6), mg = c.createChannelMerger(6);
      source.connect(sp);
      WAVE_FROM_EDGE.forEach((from, to) => sp.connect(mg, from, to));
      return mg;
    }
    function ensureGraph() {
      if (ctx) return true;
      try {
        ctx = new AudioContext();
        src = ctx.createMediaElementSource(wave);
        gain = ctx.createGain();
        gain.gain.value = plain.muted ? 0 : plain.volume;
        waveGraph(ctx, src).connect(gain);
        gain.connect(ctx.destination);
        // a 5.1 device gets the six speakers, anything else the Web Audio stereo downmix
        ctx.destination.channelCount = Math.min(6, ctx.destination.maxChannelCount) >= 6 ? 6 : Math.min(2, ctx.destination.maxChannelCount);
        return true;
      } catch (e) {
        ctx = null;
        return false;
      }
    }

    function stop() {
      gen++;
      for (const a of [plain, wave]) {
        a.pause();
        if (a.getAttribute('src')) { a.removeAttribute('src'); a.load(); }
      }
      ui();
    }

    async function open(sid, autoplay) {
      stop();
      const my = ++gen;
      id = sid;
      info = null;
      lanes = null;
      showPane('apane');
      $('atitle').textContent = sid.split('/').pop();
      $('asub').textContent = sid;
      $('achips').textContent = '';
      $('afacts').textContent = '';
      $('adl').removeAttribute('href');
      setWave('laden', 'Computing waveform …');
      try {
        info = await getJson(api('sounds', 'info', { id: sid }));
      } catch (e) {
        if (my === gen) showErr(e.message);
        return;
      }
      if (my !== gen) return;
      const url = api('sounds', 'audio', { id: sid });
      const isWave = info.channel_order === 'wave' && info.channels === 6;
      $('atitle').textContent = info.name || sid;
      const lang = info.lang && info.lang !== 'sfx' ? langName(info.lang) : null;   // TNC: 'sfx' is the folder, not a language
      chips([info.codec === 'pcm' ? 'PCM' : 'Vorbis', chName(info.channels) + (isWave ? ' · reordered' : ''), khz(info.sample_rate),
        tc(info.duration, info.duration < 60), bytes(info.size), lang]);
      laneNames = { 1: ['M'], 2: ['L', 'R'], 4: ['L', 'R', 'SL', 'SR'], 6: ['L', 'R', 'C', 'LFE', 'SL', 'SR'] }[info.channels]
        || Array.from({ length: info.channels }, (_, i) => String(i + 1));
      facts($('afacts'), [
        ['Channels', info.channels + ' (' + chName(info.channels) + ')'],
        ['Order', isWave ? 'WAVE → L R C LFE SL SR' : null],
        ['Sample rate', num(info.sample_rate) + ' Hz'],
        ['Duration', dec(info.duration, 3) + ' s'],
        ['Samples', info.samples ? num(info.samples) : null],
        ['Size', bytes(info.size)],
        ['Folder', info.group],
        ['Bank', info.bank || null],
        ['Source', info.source],
        ['Language', lang],
        ['Channel mask', info.channel_mask != null ? '0x' + info.channel_mask.toString(16).toUpperCase() : null],
        ['Variants', info.variants && info.variants.length ? plural(info.variants.length, 'identical copy', 'identical copies') : null],
      ]);
      if (info.playable === false) { setWave('fehler', info.note || 'No playable sound.'); return; }
      const ext = info.mime === 'audio/wav' ? '.wav' : '.ogg';
      $('adl').href = url;
      $('adl').download = (info.name || 'sound').replace(/\.wav$/, '') + ext;
      route = isWave && ensureGraph() ? 'wave' : 'direkt';
      au = route === 'wave' ? wave : plain;
      au.src = url;
      au.load();
      if (autoplay) play();
      waveform(url, my, isWave);
    }

    function chips(list) {
      const box = $('achips');
      box.textContent = '';
      for (const c of list) if (c) box.append(el('span', 'chip', c));
    }

    async function waveform(url, my, isWave) {
      if (info.size > 64 << 20) { setWave('aus', 'Too long for a waveform – playback works anyway.'); return; }
      let buf;
      try {
        const bytesIn = await (await fetchOk(url)).arrayBuffer();
        if (my !== gen) return;
        buf = await new OfflineAudioContext(1, 1, 8000).decodeAudioData(bytesIn);
      } catch (e) {
        if (my !== gen) return;
        // defect 7: Web Audio refuses some streams (e.g. 48 frames): <audio> still plays them
        setWave('fehler', 'No waveform: Web Audio cannot decode this stream. '
          + 'Playback uses the audio element.');
        return;
      }
      if (my !== gen) return;
      lanes = speakerChannels(buf, isWave ? 'wave' : null);
      peak = 0;
      for (const d of lanes) for (let i = 0; i < d.length; i++) { const v = Math.abs(d[i]); if (v > peak) peak = v; }
      const box = el('div');
      box.append(el('dt', null, 'Peak level'), el('dd', null, peak > 0 ? dec(20 * Math.log10(peak), 1) + ' dBFS' : 'Silence'));
      $('afacts').append(box);
      setWave('ok', '');
      paint();
    }

    function setWave(s, text) {
      waveState = s;
      $('awave').dataset.wave = s;
      $('amsg').hidden = !text;
      $('amsg').textContent = text;
      if (s !== 'ok') for (const c of [cvs, $('ahot')]) c.getContext('2d').clearRect(0, 0, c.width, c.height);
    }

    // min/max per pixel column, one lane per channel (speaker order); painted twice, dim and
    // amber, and the amber copy is clipped to the played part (CSS --p), so playing costs nothing
    function paint() {
      const box = $('awave'), dpr = Math.min(2, devicePixelRatio || 1), hot = $('ahot');
      const W = Math.max(1, Math.round(box.clientWidth * dpr)), H = Math.max(1, Math.round(box.clientHeight * dpr));
      const css = getComputedStyle(document.documentElement);
      hot.style.width = box.clientWidth + 'px';
      for (const [cv, color] of [[cvs, css.getPropertyValue('--wave').trim()], [hot, css.getPropertyValue('--accent').trim()]]) {
        cv.width = W;
        cv.height = H;
        const c = cv.getContext('2d');
        if (!lanes) continue;
        const n = lanes.length, lh = H / n, len = lanes[0].length;
        const gain = peak > 1e-4 ? Math.min(12, 1 / peak) : 1;   // quiet sounds drawn up to +21 dB, the facts say the true peak
        c.font = (10 * dpr) + 'px ui-monospace, Consolas, monospace';
        for (let k = 0; k < n; k++) {
          const d = lanes[k], mid = lh * k + lh / 2, amp = (lh / 2 - 3 * dpr) * gain;
          c.fillStyle = 'rgba(255,255,255,0.07)';
          c.fillRect(0, Math.round(mid), W, 1);
          c.fillStyle = color || '#5b6573';
          for (let x = 0; x < W; x++) {
            const a = Math.floor(x * len / W), b = Math.max(a + 1, Math.floor((x + 1) * len / W));
            let lo = 0, hi = 0;
            for (let i = a; i < b && i < len; i++) { const v = d[i]; if (v < lo) lo = v; else if (v > hi) hi = v; }
            c.fillRect(x, mid - hi * amp, 1, Math.max(1, (hi - lo) * amp));
          }
          if (n > 1) {
            c.fillStyle = 'rgba(217,223,230,0.6)';
            c.fillText(laneNames[k] || '', 6 * dpr, lh * k + 13 * dpr);
          }
        }
      }
      box.dataset.painted = lanes ? String(W) : '';
    }
    new ResizeObserver(() => { if (lanes) paint(); }).observe($('awave'));

    async function play() {
      if (!info || info.playable === false) return;
      if (route === 'wave' && ctx && ctx.state !== 'running') { try { await ctx.resume(); } catch (e) { /* stays silent */ } }
      try {
        await au.play();
      } catch (e) {
        if (e.name === 'NotAllowedError') toast('The browser needs a click before it plays.');
        else if (e.name !== 'AbortError') toast('Not playable: ' + (e.message || e.name), 5000);
      }
      ui();
    }
    function pause() { au.pause(); ui(); }
    function toggle() { if (au.paused) play(); else pause(); }
    function jump(sec) { if (info) { au.currentTime = Math.max(0, Math.min(info.duration, au.currentTime + sec)); ui(); } }

    function ui() {
      const dur = info ? info.duration : 0, t = Math.min(au.currentTime || 0, dur || 0);
      $('atime').textContent = tc(t, dur < 60) + ' / ' + tc(dur, dur < 60);
      $('awave').style.setProperty('--p', dur ? t / dur : 0);
      const on = au.paused ? '0' : '1';
      if ($('apane').dataset.playing === on) return;
      $('apane').dataset.playing = on;
      $('aplay').innerHTML = au.paused ? '&#9654;' : '&#10074;&#10074;';
      $('aplay').setAttribute('aria-label', au.paused ? 'Play' : 'Pause');
    }
    for (const a of [plain, wave]) {
      for (const ev of ['play', 'pause', 'ended', 'seeked', 'loadedmetadata']) a.addEventListener(ev, () => { if (a === au) ui(); });
      a.addEventListener('error', () => {
        if (a !== au || !a.getAttribute('src')) return;
        const code = a.error ? a.error.code : 0;
        toast('Playback failed (error code ' + code + ').', 5000);
      });
    }
    (function loop() { requestAnimationFrame(loop); if (!au.paused) ui(); })();

    // waveform = seek bar
    const wv = $('awave');
    function seekTo(e) {
      if (!info) return;
      const r = wv.getBoundingClientRect(), f = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
      au.currentTime = f * info.duration;
      ui();
    }
    let drag = false;
    wv.addEventListener('pointerdown', (e) => { drag = true; wv.setPointerCapture(e.pointerId); seekTo(e); });
    wv.addEventListener('pointermove', (e) => { if (drag) seekTo(e); });
    wv.addEventListener('pointerup', () => { drag = false; });
    wv.addEventListener('keydown', (e) => {
      const d = { ArrowLeft: -5, ArrowRight: 5 }[e.key];
      if (d) { e.preventDefault(); e.stopPropagation(); jump(d); }
    });
    $('aplay').addEventListener('click', toggle);
    function setVol(v, muted) {
      plain.volume = v;
      plain.muted = muted;
      if (gain) gain.gain.value = muted ? 0 : v;
      $('amute').innerHTML = muted || v === 0 ? '&#128263;' : '&#128266;';
      store.set('volume', v);
    }
    $('avol').addEventListener('input', (e) => setVol(+e.target.value, false));
    function mute() { setVol(plain.volume, !plain.muted); }
    $('amute').addEventListener('click', mute);

    function state() {
      return { id, route, wave: waveState, lanes: lanes ? lanes.length : 0, names: laneNames, time: au.currentTime, paused: au.paused,
        ready: au.readyState, duration: au.duration, error: au.error ? au.error.code : null, painted: wv.dataset.painted || null };
    }
    return { open, stop, play, pause, toggle, jump, mute, state, speakerChannels, waveGraph, active: () => !!info && section === 'audio' };
  })();

  // ---- textures --------------------------------------------------------------------------------

  const Tex = (() => {
    const grid = new VList($('grid'), 176, renderTiles);
    let cols = 1, idle = 0, zoom = { s: 1, x: 0, y: 0 }, cur = null, curIdx = -1, gen = 0;
    const thumbMax = (devicePixelRatio || 1) > 1.25 ? 256 : 128;
    const view = $('tview'), img = $('timg');
    function layoutCols() {             // as many 156 px columns as fit, then stretched to the full width
      if (!$('grid').clientWidth) return cols;
      const w = $('grid').clientWidth - 16, c = Math.max(1, Math.floor(w / 156));
      $('grid').style.setProperty('--tw', Math.floor(w / c) - 8 + 'px');
      return c;
    }
    grid.onresize = () => {
      if (!$('grid').clientWidth) return;   // hidden behind the detail view
      const c = layoutCols();
      if (c !== cols && L && L.kind === 'textures') { cols = c; grid.setRows(Math.ceil(L.total / cols), true); }
    };
    $('grid').addEventListener('scroll', () => { clearTimeout(idle); idle = setTimeout(() => grid.draw(), 90); }, { passive: true });

    function renderTiles(i, row) {
      if (!L || L.kind !== 'textures') return;
      row.className = 'vrow tiles';
      while (row.children.length < cols) {
        const t = el('button', 'tile');
        t.type = 'button';
        t.append(el('span', 'thumb'), el('span', 'nm'));
        t.firstChild.append(el('img'));
        row.append(t);
      }
      const settled = !idle || performance.now() - lastScroll > 80;
      for (let c = 0; c < row.children.length; c++) {
        const t = row.children[c], idx = i * cols + c;
        t.hidden = c >= cols || idx >= L.total;
        if (t.hidden) continue;
        const it = L.item(idx);
        t.dataset.idx = idx;
        t.classList.toggle('sel', idx === curIdx);
        const im = t.firstChild.firstChild;
        if (!it) { t.lastChild.textContent = '…'; im.removeAttribute('src'); t.title = ''; continue; }
        t.lastChild.textContent = it.name;
        t.title = it.id + (it.group ? '\n' + it.group : '');
        const want = api('textures', 'image.png', { id: it.id, max: thumbMax });
        if (im.dataset.want !== want) {
          im.dataset.want = want;
          im.removeAttribute('src');
          t.classList.remove('broken', 'strip', 'swatch');
          const cc = /^constantcolor\(\s*([^)]*)\)/i.exec(it.name);    // TNO: a colour the engine makes from its name
          t.firstChild.dataset.rgba = cc ? cc[1].split(/[\s,]+/).filter(Boolean).join(' ') : '';   // shown on the swatch
          if (cc) t.lastChild.textContent = 'Constant color';
        }
        if (!im.getAttribute('src') && settled) {
          im.onerror = () => { t.classList.add('broken'); };
          im.onload = () => {
            const w = im.naturalWidth, h = im.naturalHeight;
            im.classList.toggle('px', w <= 64 || h <= 64);
            t.classList.toggle('strip', Math.max(w, h) >= 8 * Math.min(w, h));   // 256x4 lookup strips: fill the tile
            t.classList.toggle('swatch', w <= 16 && h <= 16);                     // a solid colour: fill it, over a light check
          };
          im.src = want;
        }
      }
    }
    let lastScroll = 0;
    $('grid').addEventListener('scroll', () => { lastScroll = performance.now(); }, { passive: true });
    $('grid').addEventListener('click', (e) => {
      const t = e.target.closest('.tile');
      if (t) openIdx(+t.dataset.idx);
    });

    function reload(keep) {
      if (!L || L.kind !== 'textures') return;
      cols = layoutCols();
      if (!keep) {
        L.load().then((ok) => {
          if (!ok || L.kind !== 'textures') return;
          $('count').textContent = countLine();
          folderView.draw();
          showGrid();
        });
        return;
      }
      showGrid();
    }
    function showGrid() {
      if (!$('tpane').hidden) return;
      hidePanes();
      $('grid').hidden = false;
      cols = layoutCols();
      grid.setRows(Math.ceil(L.total / cols), true);
      if (!L.total) { emptyInfo(L.q ? 'No matches for "' + L.q + '".' : 'No textures in this folder.'); $('grid').hidden = false; }
      folderView.draw();
    }

    function mark(idx) { curIdx = idx; }  // an item opened by link: its tile, for ←/→ and the way back to the grid

    async function openIdx(idx) {
      if (!L || idx < 0 || idx >= L.total) return;
      const it = await L.itemAsync(idx);
      if (!it) return;
      curIdx = idx;
      L.selIdx = idx;
      L.selId = it.id;
      syncUrl();
      open(it.id);
    }

    async function open(tid) {
      const my = ++gen;
      cur = null;
      hidePanes();
      $('tpane').hidden = false;
      $('ttitle').textContent = tid.split('/').pop();
      $('tsub').textContent = tid;
      $('tfacts').textContent = '';
      $('tmsg').hidden = false;
      $('tmsg').textContent = 'Loading texture …';
      img.hidden = true;
      $('tdl').removeAttribute('href');
      $('tpane').dataset.loaded = '0';
      let inf;
      try {
        inf = await getJson(api('textures', 'info', { id: tid }));
      } catch (e) {
        if (my === gen) { $('tmsg').textContent = e.message; $('tpane').dataset.loaded = 'fehler'; }
        return;
      }
      if (my !== gen) return;
      cur = inf;
      const path = inf.group && !inf.group.startsWith('(') ? inf.group + '/' + inf.name : inf.name;
      $('ttitle').textContent = /^constantcolor\(/i.test(inf.name) ? 'Constant color' : inf.name;
      $('tsub').textContent = path;
      const url = api('textures', 'image.png', { id: tid, max: 2048 });
      $('tdl').href = api('textures', 'export', { id: tid });   // assetio: full size, any side
      $('tdl').download = inf.name.replace(/\.(tga|png|dds|bimage)$/i, '') + '.png';
      const rows = [
        ['Format', inf.format || null], ['Size', inf.width ? inf.width + ' × ' + inf.height : null],
        ['Mips', inf.mips != null ? String(inf.mips) : null], ['Faces', inf.faces > 1 ? String(inf.faces) : null],
        ['Folder', inf.group], ['Entry', tid !== path ? tid : null], ['Archive', inf.archive], ['Texture ID', inf.tex_id || null],
        ['Size in archive', bytes(inf.size)],
      ];
      facts($('tfacts'), rows);
      if (inf.error) { $('tmsg').textContent = 'Texture unreadable: ' + inf.error; $('tpane').dataset.loaded = 'fehler'; return; }
      try {
        const blob = await (await fetchOk(url)).blob();
        if (my !== gen) return;
        const old = img.src;
        img.src = URL.createObjectURL(blob);
        await img.decode();
        if (old.startsWith('blob:')) URL.revokeObjectURL(old);
      } catch (e) {
        if (my !== gen) return;
        $('tmsg').textContent = e.message || 'Cannot display the picture.';
        $('tpane').dataset.loaded = 'fehler';
        return;
      }
      if (my !== gen) return;
      img.hidden = false;
      $('tmsg').hidden = true;
      rows.splice(2, 0, ['Preview', img.naturalWidth + ' × ' + img.naturalHeight
        + (inf.width && img.naturalWidth < inf.width ? ' (largest level served)' : '')]);
      facts($('tfacts'), rows);
      fit();
      $('tpane').dataset.loaded = '1';
    }

    function apply() {
      img.style.transform = 'translate(' + zoom.x + 'px,' + zoom.y + 'px) scale(' + zoom.s + ')';
      img.classList.toggle('px', zoom.s >= 2);
      view.dataset.zoom = zoom.s.toFixed(3);
    }
    function fit() {
      const vw = view.clientWidth - 32, vh = view.clientHeight - 32, iw = img.naturalWidth || 1, ih = img.naturalHeight || 1;
      zoom.s = Math.min(vw / iw, vh / ih, 8);
      zoom.x = (view.clientWidth - iw * zoom.s) / 2;
      zoom.y = (view.clientHeight - ih * zoom.s) / 2;
      apply();
    }
    function oneToOne() {
      const iw = img.naturalWidth, ih = img.naturalHeight;
      zoom = { s: 1, x: (view.clientWidth - iw) / 2, y: (view.clientHeight - ih) / 2 };
      apply();
    }
    view.addEventListener('wheel', (e) => {
      e.preventDefault();
      const r = view.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
      const f = Math.exp(-e.deltaY * 0.0015), s = Math.min(32, Math.max(0.05, zoom.s * f));
      zoom.x = mx - (mx - zoom.x) * s / zoom.s;
      zoom.y = my - (my - zoom.y) * s / zoom.s;
      zoom.s = s;
      apply();
    }, { passive: false });
    let pan = null;
    view.addEventListener('pointerdown', (e) => { pan = { x: e.clientX - zoom.x, y: e.clientY - zoom.y }; view.setPointerCapture(e.pointerId); view.classList.add('drag'); });
    view.addEventListener('pointermove', (e) => { if (pan) { zoom.x = e.clientX - pan.x; zoom.y = e.clientY - pan.y; apply(); } });
    view.addEventListener('pointerup', () => { pan = null; view.classList.remove('drag'); });
    view.addEventListener('dblclick', () => (zoom.s < 1.5 ? oneToOne() : fit()));
    $('tfit').addEventListener('click', fit);
    $('t11').addEventListener('click', oneToOne);
    function close() {
      gen++;
      $('tpane').hidden = true;
      showGrid();
      grid.reveal(Math.floor(curIdx / cols));
      grid.draw();
      $('grid').focus({ preventScroll: true });
    }
    $('tback').addEventListener('click', close);
    new ResizeObserver(() => { if (!$('tpane').hidden && img.naturalWidth) fit(); }).observe(view);

    function state() {
      return { opened: !$('tpane').hidden, loaded: $('tpane').dataset.loaded, natural: [img.naturalWidth, img.naturalHeight],
        cols, idx: curIdx, zoom: zoom.s, message: $('tmsg').hidden ? null : $('tmsg').textContent };
    }
    return {
      reload, open, close, openIdx, mark, state, draw: () => { grid.draw(); folderView.draw(); },
      get opened() { return !$('tpane').hidden; },
      step: (d) => openIdx(curIdx + d), active: () => section === 'texturen',
    };
  })();
  function reloadGrid(keep) { Tex.reload(keep); }

  // ---- texts ----------------------------------------------------------------------------------

  const Text = (() => {
    const view = new VList($('xlines'), 20, renderLine);
    let lines = [], shown = [], term = '', gen = 0, text = '', sel = -1, info = null;

    function renderLine(i, d) {
      const n = shown[i];
      if (!d.firstChild) d.append(el('span', 'ln mono'), el('span', 'lt mono'));
      d.className = 'vrow line' + (n === sel ? ' sel' : '');
      d.children[0].textContent = n + 1;
      const s = lines[n], lt = d.children[1];
      lt.textContent = '';
      if (!term) { lt.textContent = s; return; }
      const low = s.toLowerCase();
      let p = 0;
      for (let k = low.indexOf(term); k >= 0 && p < s.length; k = low.indexOf(term, p)) {
        if (k > p) lt.append(document.createTextNode(s.slice(p, k)));
        lt.append(el('mark', null, s.slice(k, k + term.length)));
        p = k + term.length;
      }
      if (p < s.length) lt.append(document.createTextNode(s.slice(p)));
    }
    $('xlines').addEventListener('click', (e) => {
      const r = e.target.closest('.vrow');
      if (!r) return;
      sel = shown[+r.dataset.i];
      view.draw();
      const box = $('xline');
      box.hidden = false;
      box.firstChild.textContent = 'Line ' + num(sel + 1);
      box.children[1].textContent = lines[sel];
    });
    $('xline').querySelector('button').addEventListener('click', () => { if (sel >= 0) copy(lines[sel]); });
    $('xcopy').addEventListener('click', () => copy(text));

    function filter() {
      term = $('xq').value.trim().toLowerCase();
      shown = [];
      let hits = 0;
      for (let i = 0; i < lines.length; i++) {
        if (!term) { shown.push(i); continue; }
        const low = lines[i].toLowerCase();
        let k = low.indexOf(term);
        if (k < 0) continue;
        shown.push(i);
        while (k >= 0) { hits++; k = low.indexOf(term, k + term.length); }
      }
      $('xcount').textContent = term ? plural(hits, 'match', 'matches') + ' in ' + plural(shown.length, 'line', 'lines') : plural(lines.length, 'line', 'lines');
      $('xpane').dataset.hits = term ? String(hits) : '';
      view.setRows(shown.length);
    }
    let t = 0;
    $('xq').addEventListener('input', () => { clearTimeout(t); t = setTimeout(filter, 100); });

    async function open(tid) {
      const my = ++gen;
      showPane('xpane');
      $('xtitle').textContent = tid.split(':').slice(1).join(':') || tid;
      $('xsub').textContent = 'loading …';
      $('xline').hidden = true;
      sel = -1;
      lines = [];
      shown = [];
      view.setRows(0);
      $('xcount').textContent = '';
      $('xpane').dataset.loaded = '0';
      $('xdl').href = api('texts', 'file', { id: tid });   // assetio: the file as the game has it (decrypted)
      try {
        const [inf, body] = await Promise.all([getJson(api('texts', 'info', { id: tid })),
          fetchOk(api('texts', 'text', { id: tid })).then((r) => r.text())]);
        if (my !== gen) return;
        info = inf;
        text = body.replace(/^﻿/, '');
      } catch (e) {
        if (my === gen) showErr(e.message);
        return;
      }
      lines = text.split(/\r?\n/);
      if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop();
      $('xsub').textContent = [info.type, info.lang && langName(info.lang), bytes(info.size), plural(lines.length, 'line', 'lines'), info.archive].filter(Boolean).join(' · ');
      filter();
      $('xpane').dataset.loaded = '1';
    }
    function state() { return { lines: lines.length, shown: shown.length, term, hits: +($('xpane').dataset.hits || 0), loaded: $('xpane').dataset.loaded }; }
    return { open, state };
  })();

  // ---- models (models.js) ---------------------------------------------------------------------------

  const Models = window.StudioModels({ $, el, num, dec, plural, fetchOk, getJson, api, toast, showPane, showErr,
    counted(info) {                      // an opened model's counts into its list row
      if (!L || L.kind !== 'models') return;
      for (const page of L.pages.values()) {
        if (!Array.isArray(page)) continue;
        for (const it of page) if (it.id === info.id) Object.assign(it, { tris: info.surfaces.reduce((a, s) => a + s.tris, 0), surfaces: info.surfaces.length });
      }
      listView.draw();
    } });

  const Scripts = window.StudioScripts({ $, el, num, plural, fetchOk, getJson, api, toast, showPane, showErr });

  // ---- keyboard ---------------------------------------------------------------------------------

  document.addEventListener('keydown', (e) => {
    if (section === 'karten') return;      // app.js has the keyboard there
    if (e.code === 'F11') { if (pageFullscreen()) e.preventDefault(); return; }
    if (e.ctrlKey && e.key === 'f') { e.preventDefault(); $('q').focus(); $('q').select(); return; }
    if (typing(e.target) || e.ctrlKey || e.altKey || e.metaKey) {
      if (e.key === 'Escape' && typing(e.target)) e.target.blur();
      return;
    }
    // Space plays/pauses unless it would press some other button; the player's own buttons count as the player
    const onButton = e.target && (e.target.tagName === 'BUTTON' || e.target.tagName === 'SUMMARY' || e.target.tagName === 'A')
      && !(e.target.closest && e.target.closest('#vplayer .controls, #apane .controls'));
    if (e.key === '/') { e.preventDefault(); $('q').focus(); return; }
    if (section === 'video' && Video.active()) {
      if (e.key === ' ' && !onButton) { e.preventDefault(); Video.toggle(); return; }
      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { e.preventDefault(); Video.jump(e.key === 'ArrowLeft' ? -5 : 5); return; }
      if (e.key === 'f' || e.key === 'F') { Video.fullscreen(); return; }
      if (e.key === 'm' || e.key === 'M') { Video.mute(); return; }
    }
    if (section === 'audio' && Audio.active()) {
      if (e.key === ' ' && !onButton) { e.preventDefault(); Audio.toggle(); return; }
      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { e.preventDefault(); Audio.jump(e.key === 'ArrowLeft' ? -5 : 5); return; }
      if (e.key === 'm' || e.key === 'M') { Audio.mute(); return; }
    }
    if (section === 'texturen' && Tex.opened) {
      if (e.key === 'Escape') { Tex.close(); return; }
      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { e.preventDefault(); Tex.step(e.key === 'ArrowLeft' ? -1 : 1); return; }
    }
    if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && e.target !== $('list') && section !== 'texturen') {
      e.preventDefault();
      step(e.key === 'ArrowDown' ? 1 : -1);
    }
  });

  // ---- start --------------------------------------------------------------------------------------

  for (const b of document.querySelectorAll('#tabs button')) b.setAttribute('aria-selected', String(b.dataset.s === section));
  setGame(game, true);
  $('studio').hidden = section === 'karten';
  if (Q.get('q') && section !== 'karten') listing(game, KIND[section]).q = Q.get('q');
  document.addEventListener('DOMContentLoaded', () => {
    loadGames();
    if (section !== 'karten') openSection();
    fsLabel();
  });

  // for tools/verify_studio_front.py (and curious people in the console)
  window.studio = {
    WAVE_FROM_EDGE,
    state: () => ({
      section, game,
      list: L && { kind: L.kind, total: L.total, rows: L.rows, flat: L.flat, groups: L.gs ? L.gs.length : 0, q: L.q, group: L.group, lang: L.lang, sel: L.selId, error: L.error && L.error.message },
      video: Video.state(), audio: Audio.state(), tex: Tex.state(), text: Text.state(), model: Models.state(), scripts: Scripts.state(),
      error: $('err').hidden ? null : $('err').dataset.text,
    }),
    audio: { speakerChannels: Audio.speakerChannels, waveGraph: Audio.waveGraph },
    show: showSection, game: setGame, select: (i) => selectIdx(i, true),
    labels: () => (L && L.names ? [...L.names.values()].map((v) => v[0]) : []),   // folder names as the list shows them
  };
})();
