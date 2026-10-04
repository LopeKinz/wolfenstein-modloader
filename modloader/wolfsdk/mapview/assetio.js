/* Wolfenstein Studio: export and replace (loaded after studio.js).

   Replace: the dialog #rep asks the server what replacing an item writes
   (GET /api/<g>/<kind>/replace?id=[&skin=]), shows the picked file next to the game's
   (a texture side by side, a text as a diff; a WAV or Bink 2 file only after a magic and
   size check, the server checks the rest) and POSTs it with the header
   X-Wolfsdk-Studio: 1 (name= for a new mod, folder= to add to one of the Studio's mods).
   The server writes mods/<folder>/ only (wolfsdk/studiomod.py), never the game: the
   loader applies the mod. Items and the game come from window.studio.state();
   window.studioIO.open() is also what models.js calls for "Save as mod".
   Export all: /api/<g>/<kind>/export.zip over the list's search, its limits checked
   first (?check=1). */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const S = () => window.studio.state();
  const api = (g, kind, leaf, p) => '/api/' + g + '/' + kind + (leaf ? '/' + leaf : '') + (p ? '?' + new URLSearchParams(p) : '');
  const NOUN = { textures: 'texture', texts: 'text', sounds: 'sound', videos: 'video', models: 'model skin' };
  // per replace form: file picker filter and label, POST type; wav/bink: size limit (MB, studiomod) and magic
  const FORMS = {
    png: { accept: 'image/png', pick: 'Choose PNG…', type: 'image/png' },
    text: { accept: '', pick: 'Choose edited file…', type: 'text/plain; charset=utf-8' },
    wav: { accept: '.wav,audio/wav', pick: 'Choose WAV…', type: 'audio/wav', mb: 256,
      magic: (h) => h.startsWith('RIFF') && h.slice(8, 12) === 'WAVE', wrong: 'That is not a WAV file.' },
    bink: { accept: '.bk2', pick: 'Choose Bink 2 video…', type: 'application/octet-stream', mb: 1024,
      magic: (h) => h.startsWith('KB2'), wrong: 'That is not a Bink 2 video (.bk2).' },
  };
  const ZIP = { textures: 'at most 50 textures', texts: 'at most 5,000 texts', sounds: 'at most 500 sounds and 1 GB', videos: 'at most 2 GB' };
  const store = {
    get(k) { try { return localStorage.getItem('studio.io.' + k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem('studio.io.' + k, v); } catch (e) { /* private mode */ } },
  };
  let toastTimer = 0;
  function toast(text, ms) {
    const t = $('toast');
    t.textContent = text;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, ms || 6000);
  }
  async function send(url, opt) {       // JSON, or an Error carrying the server's {"error"}
    let r;
    try { r = await fetch(url, opt); } catch (e) { throw new Error('No connection to the Studio server. Is the loader still running?'); }
    let d = null;
    try { d = await r.clone().json(); } catch (e) { /* not JSON */ }
    if (!r.ok) {
      let t = d && d.error;
      if (!t) { try { t = (await r.text()).trim(); } catch (e) { /* ignore */ } }
      const err = new Error(t && t.length < 600 && !/^</.test(t) ? t : 'The server answered ' + r.status + '.');
      err.status = r.status;
      throw err;
    }
    return d;
  }
  function clear(node) { while (node.firstChild) node.firstChild.remove(); }
  function li(text) { const x = document.createElement('li'); x.textContent = text; return x; }

  // ---- the replace dialog ----------------------------------------------------------------------

  const dlg = $('rep');
  let R = null;                          // {game, kind, id, skin, info, file, text, url}
  let gen = 0;

  function status(text, cls) {
    $('repstatus').textContent = text || '';
    $('repstatus').className = 'repstatus small' + (cls ? ' ' + cls : '');
  }

  async function mods(game, pick) {
    const sel = $('repfolder');
    clear(sel);
    let list = [];
    try { list = await send(api(game, 'mods')); } catch (e) { /* the new-mod way still works */ }
    for (const m of list) sel.append(new Option(m.name + ' (' + m.folder + ', ' + m.assets + ' assets)', m.folder));
    $('repadd').disabled = !list.length;
    sel.disabled = !list.length;
    const want = pick || store.get('folder.' + game);   // the mod saved to last: replacements collect in it
    if (list.some((m) => m.folder === want)) {
      sel.value = want;
      if (pick || !$('repname').value.trim()) $('repadd').checked = true;
    }
    if (!list.length) dlg.querySelector('input[name=repto][value=new]').checked = true;
    ready();
  }

  // opt: {game, kind, id, skin?, file?: Blob, fileName?}
  async function open(opt) {
    const my = ++gen;
    R = Object.assign({ info: null, file: null, text: null, url: null }, opt);
    $('reptitle').textContent = 'Replace ' + (NOUN[R.kind] || 'item');
    $('repwhat').textContent = R.id + (R.skin ? '  ·  material ' + R.skin : '');
    $('repreason').hidden = false;
    $('repreason').textContent = 'Checking what replacing it writes …';
    $('repbody').hidden = true;
    reset();
    if (!dlg.open) dlg.showModal();
    let info;
    try {
      info = await send(api(R.game, R.kind, 'replace', R.skin ? { id: R.id, skin: R.skin } : { id: R.id }));
    } catch (e) {
      if (my === gen) $('repreason').textContent = e.message;
      return;
    }
    if (my !== gen) return;
    R.info = info;
    if (!info.ok) {
      $('repreason').textContent = 'This ' + (NOUN[R.kind] || 'item') + ' cannot be replaced yet: ' + info.reason;
      return;
    }
    $('repreason').hidden = true;
    $('repbody').hidden = false;
    const t = info.target || {};
    const form = FORMS[info.form] || FORMS.text;
    $('repinput').accept = form.accept;
    $('reppick').textContent = form.pick;
    const notes = $('repnotes');
    clear(notes);
    if (info.form === 'png') {
      notes.append(li('Any size: it is scaled to ' + t.width + ' × ' + t.height + ', ' + t.mips + ' mip level' + (t.mips === 1 ? '' : 's') + ', ' + t.format + '.'));
    } else if (info.form === 'text') {
      notes.append(li('Edit the file you downloaded (Download file) and pick it here. It must parse; unchanged text is refused.'));
    }
    for (const n of info.notes || []) notes.append(li(n));
    const blood = info.blood || [];
    $('repbloodrow').hidden = !blood.length;
    $('repbloodtxt').textContent = 'Also the blood variant: ' + blood.map((b) => b.split('/').pop().split('$')[0]).join(', ');
    mods(R.game);
    if (R.file) use(R.file, R.fileName || 'skin.png');
  }

  function reset() {
    status('');
    $('repfile').textContent = 'No file chosen.';
    $('repcmp').hidden = true;
    $('repdiff').hidden = true;
    clear($('repdiff'));
    if (R && R.url) URL.revokeObjectURL(R.url);
    $('repnew').removeAttribute('src');
    $('repold').removeAttribute('src');
    $('repsave').disabled = true;
  }

  async function use(file, name) {
    const my = gen, r = R;
    r.file = null;
    $('repfile').textContent = name + ' (' + Math.max(1, Math.round(file.size / 1024)).toLocaleString('en') + ' KB)';
    status('');
    if (r.info.form === 'png') {
      if (file.size > 64 * 1024 * 1024) { status('The PNG is larger than 64 MB.', 'bad'); ready(); return; }
      const sig = new Uint8Array(await file.slice(0, 8).arrayBuffer());
      if (![0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a].every((v, i) => sig[i] === v)) {
        status('That is not a PNG file.', 'bad');
        ready();
        return;
      }
      if (r.url) URL.revokeObjectURL(r.url);
      r.url = URL.createObjectURL(file);
      $('repold').src = api(r.game, 'textures', 'image.png', { id: r.info.image, max: 1024 });
      $('repnew').src = r.url;
      $('repcmp').hidden = false;
      try { await $('repnew').decode(); } catch (e) {
        if (my === gen) { status('The browser cannot read this PNG; the server would refuse it too.', 'bad'); ready(); }
        return;
      }
      if (my !== gen) return;
      const im = $('repnew'), t = r.info.target;
      $('repnewcap').textContent = 'Yours: ' + im.naturalWidth + ' × ' + im.naturalHeight
        + (im.naturalWidth !== t.width || im.naturalHeight !== t.height ? ' → scaled to ' + t.width + ' × ' + t.height : '')
        + (im.naturalWidth * t.height !== im.naturalHeight * t.width ? ' (other aspect ratio: the UVs will not match)' : '');
    } else if (r.info.form === 'wav' || r.info.form === 'bink') {
      const form = FORMS[r.info.form];
      if (file.size > form.mb * 1024 * 1024) { status('The file is larger than ' + form.mb.toLocaleString('en') + ' MB.', 'bad'); ready(); return; }
      const head = String.fromCharCode(...new Uint8Array(await file.slice(0, 12).arrayBuffer()));
      if (my !== gen) return;
      if (!form.magic(head)) { status(form.wrong, 'bad'); ready(); return; }
    } else {
      if (file.size > 8 * 1024 * 1024) { status('The text is larger than 8 MB.', 'bad'); ready(); return; }
      let mine, orig;
      try {
        [mine, orig] = await Promise.all([file.text(), fetch(api(r.game, 'texts', 'text', { id: r.id })).then((x) => x.text())]);
      } catch (e) {
        if (my === gen) status('Could not read the file: ' + e.message, 'bad');
        return;
      }
      if (my !== gen) return;
      const n = diff($('repdiff'), orig.replace(/^﻿/, ''), mine.replace(/^﻿/, ''));
      $('repdiff').hidden = false;
      if (!n) { status('Nothing changed: the file equals the game\'s text.', 'bad'); ready(); return; }
    }
    r.file = file;
    ready();
  }

  // a line diff: common head and tail trimmed, 3 lines of context, the middle as - / + lines
  function diff(box, a, b) {
    clear(box);
    const x = a.split(/\r?\n/), y = b.split(/\r?\n/);
    let s = 0;
    while (s < x.length && s < y.length && x[s] === y[s]) s++;
    let ex = x.length, ey = y.length;
    while (ex > s && ey > s && x[ex - 1] === y[ey - 1]) { ex--; ey--; }
    if (s === x.length && s === y.length) { box.append(line('gap', 'No difference.')); return 0; }
    const rows = [];
    if (s > 3) rows.push(['gap', '… ' + (s - 3) + ' unchanged line(s)']);
    for (let i = Math.max(0, s - 3); i < s; i++) rows.push(['', '  ' + x[i]]);
    // the changed middle, paired line by line where both sides have one
    const del = x.slice(s, ex), add = y.slice(s, ey);
    for (let i = 0; i < Math.max(del.length, add.length); i++) {
      if (i < del.length && i < add.length && del[i] === add[i]) { rows.push(['', '  ' + del[i]]); continue; }
      if (i < del.length) rows.push(['del', '- ' + del[i]]);
      if (i < add.length) rows.push(['add', '+ ' + add[i]]);
    }
    for (let i = ex; i < Math.min(x.length, ex + 3); i++) rows.push(['', '  ' + x[i]]);
    if (x.length - ex > 3) rows.push(['gap', '… ' + (x.length - ex - 3) + ' unchanged line(s)']);
    const shown = rows.slice(0, 600);
    for (const [c, t] of shown) box.append(line(c, t));
    if (rows.length > shown.length) box.append(line('gap', '… ' + (rows.length - shown.length) + ' more diff line(s)'));
    return rows.filter(([c]) => c === 'del' || c === 'add').length;
  }
  function line(cls, text) { const d = document.createElement('div'); if (cls) d.className = cls; d.textContent = text; return d; }

  function target() {
    const add = $('repadd').checked;
    return add ? { folder: $('repfolder').value } : { name: $('repname').value.trim(), author: $('repauthor').value.trim() };
  }
  function ready() {
    const t = target();
    $('repsave').disabled = !(R && R.file) || (t.folder !== undefined ? !t.folder : !t.name);
  }

  async function save() {
    const r = R, my = gen, t = target();
    if (!r || !r.file) return;
    const q = Object.assign({ id: r.id }, t);
    if (!q.author) delete q.author;
    if (r.skin) q.skin = r.skin;
    if (r.info.form === 'png') q.blood = $('repbloodrow').hidden || $('repblood').checked ? '1' : '0';
    $('repsave').disabled = true;
    $('reppick').disabled = true;
    status(r.info.form === 'png' ? 'Encoding … (a 4096 px texture takes 1–2 minutes)' : 'Checking and saving …');
    let d;
    try {
      d = await send(api(r.game, r.kind, 'replace', q), {
        method: 'POST', body: r.file,
        headers: { 'X-Wolfsdk-Studio': '1', 'Content-Type': (FORMS[r.info.form] || FORMS.text).type },
      });
    } catch (e) {
      if (my === gen) { status(e.message, 'bad'); $('reppick').disabled = false; ready(); }
      return;
    }
    $('reppick').disabled = false;
    store.set('folder.' + r.game, d.folder);
    toast(d.message, 8000);
    if (my !== gen) return;
    status(d.message + (d.notes && d.notes.length ? ' ' + d.notes.join(' ') : ''), 'ok');
    $('repname').value = '';
    await mods(r.game, d.folder);         // the next replacement goes into the same mod unless changed
    ready();
  }

  $('reppick').addEventListener('click', () => $('repinput').click());
  $('repinput').addEventListener('change', (e) => {
    const f = e.target.files && e.target.files[0];
    e.target.value = '';
    if (f && R && R.info) use(f, f.name);
  });
  $('repsave').addEventListener('click', save);
  for (const x of ['repname', 'repfolder']) $(x).addEventListener('input', ready);
  $('repfolder').addEventListener('change', () => { $('repadd').checked = true; ready(); });
  for (const x of dlg.querySelectorAll('input[name=repto]')) x.addEventListener('change', ready);
  $('repname').addEventListener('focus', () => { dlg.querySelector('input[name=repto][value=new]').checked = true; ready(); });
  dlg.addEventListener('close', () => { gen++; if (R && R.url) URL.revokeObjectURL(R.url); });
  dlg.addEventListener('keydown', (e) => e.stopPropagation());   // the list behind must not step (studio.js listens on document)

  function openCurrent(kind) {
    const s = S(), id = s.list && s.list.kind === kind ? s.list.sel : null;
    if (!id) { toast('Open an item first.'); return; }
    open({ game: s.game, kind, id });
  }
  $('trep').addEventListener('click', () => openCurrent('textures'));
  $('xrep').addEventListener('click', () => openCurrent('texts'));
  $('arep').addEventListener('click', () => openCurrent('sounds'));
  if ($('vrep')) $('vrep').addEventListener('click', () => openCurrent('videos'));

  // ---- export all hits as one ZIP ------------------------------------------------------------------

  function zipNote() {
    const kind = $('side').dataset.kind;
    $('zipall').hidden = $('zipnote').hidden = !(kind in ZIP);
    $('zipnote').textContent = kind in ZIP ? 'Export ZIP takes the hits of the search and folder: ' + ZIP[kind] + '.' : '';
  }
  new MutationObserver(zipNote).observe($('side'), { attributes: true, attributeFilter: ['data-kind'] });
  zipNote();

  $('zipall').addEventListener('click', async () => {
    const s = S(), l = s.list;
    if (!l || !(l.kind in ZIP)) return;
    const p = { q: l.q || '' };
    if (l.group != null) p.group = l.group;
    if (l.lang != null) p.lang = l.lang;
    let c;
    try {
      c = await send(api(s.game, l.kind, 'export.zip', Object.assign({ check: '1' }, p)));
    } catch (e) {
      toast(e.message, 8000);
      return;
    }
    const a = document.createElement('a');
    a.href = api(s.game, l.kind, 'export.zip', p);
    a.download = '';
    document.body.append(a);
    a.click();
    a.remove();
    toast('Exporting ' + c.count.toLocaleString('en') + ' ' + l.kind + ' as a ZIP'
      + (l.kind === 'textures' ? ' (every texture is decoded at full size: this takes a while).' : '.'), 8000);
  });

  window.studioIO = { open, state: () => ({ open: dlg.open, id: R && R.id, kind: R && R.kind, file: !!(R && R.file), status: $('repstatus').textContent }) };
})();
