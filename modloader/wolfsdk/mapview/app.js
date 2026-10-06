/* Karten in 3D: browser side of the map viewer (three.js r128, vendored), the
   "Karten" section of the Studio (studio.js runs first and owns the page around it).

   Data contract (served by wolfsdk/mapserver.py, built by wolfsdk/mapgeo.py,
   mapcoll.py and maptex.py; for The New Order by tnomap/tnomapcoll/tnomaptex
   under /api/tno/..., same shapes):
     /api/maps                      [{id, title, group, entities_bytes}]
     /api/map/<id>/scene.json       {meta, materials, meshes, instances, entities, textures}
     /api/map/<id>/geometry.bin     per surface: f32 pos[3n] | f32 uv[2n] | u32 idx at its offsets
     /api/map/<id>/collision.json   hulls + base64 streams on a grid (meta.encoding)
     /api/tex/<id>[?max=N]          "WTX1" u16 fmt, u16 flags(bit0 sRGB), u16 w, u16 h, u32 n, raw blocks
     /api/tex/<id>.png[?max=N]      the same mip as PNG (fallback without the GPU extension)
   Game space is +Z up in meters; three.js gets (x, y, z) -> (x, z, -y) through
   the `world` group (rotation.x = -PI/2).

   URL parameters (all optional, for bookmarks and headless screenshots):
     map=<id>  view=orbit|top|fly  cut=<z m> (empty: default height)  mode=tex|flat  tex=png
     layers=static,dynamic,sky,coll  cats=enemy,weapon,...  pick=<entity name>
     panel=0  texmax=N  pngmax=N  shot=1 (render once, when everything is loaded)
     game=tnc|tno (studio.js puts the game on <body data-game>; ?map= names a map of that game)
   Studio events on document: studio:section (the section changed), studio:game (detail: game). */
(function () {
  'use strict';

  const Q = new URLSearchParams(location.search);
  const SHOT = Q.has('shot');
  const $ = (id) => document.getElementById(id);
  const TEX_MAX = +Q.get('texmax') || 1024;
  const PNG_MAX = +Q.get('pngmax') || 512;
  const FORCE_PNG = Q.get('tex') === 'png';
  const GROUPS = ['Campaign', 'DLC', 'Other', 'Custom'];   // the server's group names
  const KINDS = ['static', 'dynamic', 'sky'];
  const CATS = [                       // [cat, label, colour, shown by default]
    ['enemy', 'Enemies', '#ff5a4f', true],
    ['npc', 'Characters', '#ff9f43', false],
    ['weapon', 'Weapons', '#ffd43b', true],
    ['item', 'Items', '#8ce99a', true],
    ['spawn', 'Spawn points', '#4dd4e8', true],
    ['door', 'Doors', '#74a9ff', true],
    ['light', 'Lights', '#fff1b8', false],
    ['trigger', 'Triggers', '#c29bff', false],
    ['audio', 'Audio', '#f783ac', false],
    ['logic', 'Logic', '#a3adb8', false],
    ['other', 'Other', '#6c7784', false],
  ];
  const COLL = { world: '#6ea8dc', static: '#8fd0e8', dynamic: '#f2b36b', clip: '#e0666a' };   // solid kinds only

  // ---- small helpers -------------------------------------------------------

  const store = {
    get(k) { try { return localStorage.getItem('mapview.' + k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem('mapview.' + k, v); } catch (e) { /* private mode */ } },
  };
  let errors = 0;
  window.addEventListener('error', () => { errors++; });
  const consoleError = console.error;
  console.error = function () { errors++; return consoleError.apply(console, arguments); };

  const fmt = (n) => n.toLocaleString('en-US');
  const mb = (n) => (n / 1048576).toLocaleString('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const srgb = (hex) => new THREE.Color(hex).convertSRGBToLinear();
  let GAME = ['tno', 'yb'].includes(document.body.dataset.game) ? document.body.dataset.game : 'tnc';
  const API = () => (GAME === 'tnc' ? '/api/' : '/api/' + GAME + '/');   // the unprefixed URLs are Wolfenstein II's
  const mapKey = () => (GAME === 'tnc' ? 'map' : 'map.' + GAME);
  const shown = () => { const s = document.body.dataset.section; return !s || s === 'karten'; };
  const mapUrl = (id, file) => API() + 'map/' + id.split('/').map(encodeURIComponent).join('/') + '/' + file;
  const csv = (k) => (Q.has(k) ? new Set(Q.get(k).split(',').filter(Boolean)) : null);

  function setState(s) { document.body.dataset.state = s; }
  function status(text, frac) {
    $('msg').textContent = text;
    $('bar').style.width = frac == null ? '0' : (clamp(frac, 0, 1) * 100).toFixed(1) + '%';
  }
  async function fetchOk(url) {
    const r = await fetch(url);
    if (!r.ok) {
      let t = '';
      try { t = (await r.text()).trim().slice(0, 300); } catch (e) { /* ignore */ }
      throw new Error(t || (r.status + ' ' + r.statusText));
    }
    return r;
  }
  async function getBin(url, onProgress) {
    const r = await fetchOk(url);
    const total = +r.headers.get('Content-Length') || 0;
    if (!r.body || !total || SHOT) return r.arrayBuffer();   // headless virtual time does not wait for streamed reads
    const out = new Uint8Array(total);
    const rd = r.body.getReader();
    let got = 0;
    for (;;) {
      const { done, value } = await rd.read();
      if (done) break;
      if (got + value.length > total) throw new Error('geometry longer than announced');
      out.set(value, got);
      got += value.length;
      onProgress(got, total);
    }
    if (got !== total) throw new Error('geometry incomplete: ' + got + ' of ' + total + ' bytes');
    return out.buffer;
  }
  function b64(s) {
    const bin = atob(s);
    const u = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
    return u;
  }

  // ---- renderer, camera, lights -------------------------------------------

  const canvas = $('view');
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: !SHOT, preserveDrawingBuffer: SHOT, powerPreference: 'high-performance' });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(innerWidth, innerHeight);
  renderer.outputEncoding = THREE.sRGBEncoding;
  const gl = renderer.getContext();
  const ext = renderer.extensions;
  const HAS = {                        // what this GPU decodes; sRGB is decoded in the shader (r128), so s3tc_srgb is only reported
    s3tc: ext.has('WEBGL_compressed_texture_s3tc'), s3tc_srgb: ext.has('WEBGL_compressed_texture_s3tc_srgb'),
    rgtc: ext.has('EXT_texture_compression_rgtc'), bptc: ext.has('EXT_texture_compression_bptc'),
  };
  const GPU = {};                      // WTX1 format code -> three constant
  if (HAS.s3tc) {
    GPU[1] = THREE.RGBA_S3TC_DXT1_Format;   // RGBA variant: BC1 punch-through alpha for "mask" materials
    GPU[3] = THREE.RGBA_S3TC_DXT5_Format;
  }
  if (HAS.bptc) GPU[7] = THREE.RGBA_BPTC_Format;
  // ponytail: three r128 has no RGTC constants, so BC4/BC5 take the PNG path here; no retail albedo
  // uses them (maptex report). This line takes over once three is upgraded.
  if (HAS.rgtc && THREE.RED_RGTC1_Format) { GPU[4] = THREE.RED_RGTC1_Format; GPU[5] = THREE.RED_GREEN_RGTC2_Format; }
  const CODE = { BC1: 1, BC3: 3, BC4: 4, BC5: 5, BC7: 7, RGBA8: 0 };
  const ANISO = Math.min(8, renderer.capabilities.getMaxAnisotropy());
  document.body.dataset.ext = Object.keys(HAS).filter((k) => HAS[k]).join(',') || 'none';

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0b0e12);
  const world = new THREE.Group();
  world.rotation.x = -Math.PI / 2;
  scene.add(world);
  scene.add(new THREE.HemisphereLight(0xe6eeff, 0x40362c, 0.9));
  const sun = new THREE.DirectionalLight(0xffffff, 0.55);
  sun.position.set(0.45, 1, 0.3);
  scene.add(sun);

  const camera = new THREE.PerspectiveCamera(55, innerWidth / innerHeight, 0.1, 5000);
  camera.position.set(30, 30, 30);
  const controls = new THREE.OrbitControls(camera, canvas);
  controls.screenSpacePanning = true;
  controls.listenToKeyEvents(canvas);   // arrow keys pan while the view has focus
  controls.addEventListener('change', () => { dirty = true; });

  const cutPlane = new THREE.Plane(new THREE.Vector3(0, -1, 0), 0);   // keeps three y (= game z) <= constant

  const dot = (() => {                 // marker sprite: filled disc with a dark rim
    const c = document.createElement('canvas');
    c.width = c.height = 32;
    const g = c.getContext('2d');
    g.fillStyle = '#000'; g.beginPath(); g.arc(16, 16, 15, 0, 7); g.fill();
    g.fillStyle = '#fff'; g.beginPath(); g.arc(16, 16, 11, 0, 7); g.fill();
    return new THREE.CanvasTexture(c);
  })();
  const ring = (() => {
    const c = document.createElement('canvas');
    c.width = c.height = 64;
    const g = c.getContext('2d');
    g.lineWidth = 5; g.strokeStyle = '#fff'; g.beginPath(); g.arc(32, 32, 26, 0, 7); g.stroke();
    return new THREE.CanvasTexture(c);
  })();
  const pickMark = new THREE.Points(new THREE.BufferGeometry().setAttribute('position', new THREE.Float32BufferAttribute([0, 0, 0], 3)),
    new THREE.PointsMaterial({ size: 26, sizeAttenuation: false, map: ring, transparent: true, depthTest: false }));
  pickMark.renderOrder = 10;
  pickMark.visible = false;
  world.add(pickMark);

  // ---- UI state -----------------------------------------------------------

  let S = null;          // the open map
  let seq = 0;           // load token: a newer openMap() wins
  let dirty = true;
  let ready = false;     // everything the URL asked for is loaded
  let nav = 'orbit';
  // The New Order: the MegaTexture is not decoded, walls have no image -> flat by default
  const defaultShade = () => (Q.get('mode') === 'flat' || Q.get('mode') === 'tex' ? Q.get('mode') : GAME === 'tno' ? 'flat' : 'tex');
  let shade = defaultShade();
  const layersQ = csv('layers');
  const layers = { static: true, dynamic: true, sky: false, coll: false };
  if (layersQ) for (const k in layers) layers[k] = layersQ.has(k);
  const catsQ = csv('cats');
  const cats = {};
  for (const [c, , , on] of CATS) cats[c] = catsQ ? catsQ.has(c) : on;
  let cutOn = Q.has('cut');

  // ---- map list -----------------------------------------------------------

  let loadedFor = null;                // the game whose map list is in the <select>
  async function loadMaps() {
    loadedFor = GAME;
    const game = GAME;
    $('mapnote').hidden = game !== 'tno';
    status(game === 'tno' ? 'Reading maps … (The New Order: a few seconds the first time)' : 'Reading maps …');
    let maps;
    try {
      maps = await (await fetchOk(API() + 'maps')).json();
      if (game !== GAME) return;
    } catch (e) {
      if (game !== GAME) return;
      setState('fehler');
      status('Error: map list not readable – ' + e.message);
      return;
    }
    const sel = $('map');
    sel.textContent = '';
    const groups = GROUPS.concat([...new Set(maps.map((m) => m.group))].filter((g) => !GROUPS.includes(g)));
    for (const g of groups) {
      const list = maps.filter((m) => m.group === g);
      if (!list.length) continue;
      const og = document.createElement('optgroup');
      og.label = g + ' (' + list.length + ')';
      for (const m of list) {
        const o = new Option(m.title || m.id, m.id);
        if (!m.entities_bytes) { o.disabled = true; o.textContent += ' – cannot be shown'; }
        og.append(o);
      }
      sel.append(og);
    }
    sel.disabled = false;
    const ok = (id) => maps.some((m) => m.id === id && m.entities_bytes);
    const first = maps.find((m) => m.entities_bytes);
    const qmap = (Q.get('game') || 'tnc') === game ? Q.get('map') : null;
    const want = [qmap, store.get(mapKey()), first && first.id].find((id) => id && (ok(id) || id === qmap));
    if (!want) { status('No map that can be shown was found.'); return; }
    if (ok(want)) sel.value = want;
    mapTitle();
    openMap(want);
  }

  // ---- building a map -----------------------------------------------------

  function disposeScene(s) {
    world.remove(s.root);
    s.root.traverse((o) => { if (o.geometry) o.geometry.dispose(); });
    for (const m of s.texMats.concat(s.flatMats, s.extraMats)) m.dispose();
    for (const t of s.textures.values()) t.dispose();
  }

  function hashColor(name) {
    let h = 2166136261;
    for (let i = 0; i < name.length; i++) h = Math.imul(h ^ name.charCodeAt(i), 16777619);
    h >>>= 0;                          // clay look: faint tint, lightness varies so neighbours stay apart
    return new THREE.Color().setHSL((h % 360) / 360, 0.1, 0.48 + ((h >>> 9) % 20) / 100).convertSRGBToLinear();
  }

  function material(m, color) {
    const mat = new THREE.MeshPhongMaterial({ color, flatShading: true, side: THREE.DoubleSide, specular: 0x000000, shininess: 0 });
    if (m.alpha === 'mask') mat.alphaTest = 0.5;
    if (m.alpha === 'blend') { mat.transparent = true; mat.opacity = 0.75; mat.depthWrite = false; }
    return mat;
  }

  function buildScene(doc, buf) {
    const s = {
      doc, root: new THREE.Group(), kinds: {}, meshes: [], markers: [], textures: new Map(),
      texMats: [], flatMats: [], extraMats: [], usage: new Float64Array(doc.materials.length),
      placed: 0, tex: null, coll: null, collGroup: null,
    };
    world.add(s.root);
    for (const k of KINDS) {
      s.kinds[k] = new THREE.Group();
      s.kinds[k].visible = layers[k];
      s.root.add(s.kinds[k]);
    }
    doc.materials.forEach((m, i) => {
      s.texMats.push(material(m, srgb(m.albedo ? 0x9aa1a9 : 0x6d747c)));
      s.flatMats.push(material(m, hashColor(m.name)));
    });

    const byKey = new Map();           // "mesh:kind" -> [instance matrices]
    for (const inst of doc.instances) {
      const k = inst.mesh + ':' + inst.kind;
      let a = byKey.get(k);
      if (!a) byKey.set(k, (a = []));
      a.push(inst.m);
    }
    const geos = new Map();            // byte offsets -> BufferGeometry (customMaterial copies share bytes)
    const M = new THREE.Matrix4();
    for (const [k, list] of byKey) {
      const [mi, kind] = k.split(':');
      const group = s.kinds[kind] || s.kinds.static;
      s.placed += list.length;
      for (const f of doc.meshes[+mi].surfaces) {
        if (doc.materials[f.material].alpha === 'editor') continue;   // clip/collision hulls: the game never draws them
        const gk = f.pos_off + ':' + f.idx_off;
        let g = geos.get(gk);
        if (!g) {
          g = new THREE.BufferGeometry();
          g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(buf, f.pos_off, f.vcount * 3), 3));
          g.setAttribute('uv', new THREE.BufferAttribute(new Float32Array(buf, f.uv_off, f.vcount * 2), 2));
          g.setIndex(new THREE.BufferAttribute(new Uint32Array(buf, f.idx_off, f.icount), 1));
          geos.set(gk, g);
        }
        const im = new THREE.InstancedMesh(g, (shade === 'tex' ? s.texMats : s.flatMats)[f.material], list.length);
        im.frustumCulled = false;      // r128 culls by the geometry's own bounds, not the instances'
        im.userData.mat = f.material;
        im.userData.key = k;             // "mesh:kind", its instances in scene.json order
        for (let j = 0; j < list.length; j++) {
          const m = list[j];
          M.set(m[0], m[1], m[2], m[3], m[4], m[5], m[6], m[7], m[8], m[9], m[10], m[11], 0, 0, 0, 1);
          im.setMatrixAt(j, M);
        }
        group.add(im);
        s.meshes.push(im);
        s.usage[f.material] += list.length * f.icount;
      }
    }

    // entity markers, one Points cloud per category
    const per = {};
    doc.entities.forEach((e, i) => { (per[e.cat] || (per[e.cat] = [])).push(i); });
    for (const [c, , color] of CATS.concat(Object.keys(per).filter((c) => !CATS.some((x) => x[0] === c)).map((c) => [c, c, '#6c7784']))) {
      const ids = per[c];
      if (!ids) continue;
      const pos = new Float32Array(ids.length * 3);
      ids.forEach((i, j) => { const e = doc.entities[i]; pos[3 * j] = e.x; pos[3 * j + 1] = e.y; pos[3 * j + 2] = e.z; });
      const g = new THREE.BufferGeometry();
      g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
      const mat = new THREE.PointsMaterial({ color: srgb(color), size: 9, sizeAttenuation: false, map: dot, alphaTest: 0.5, transparent: true, depthTest: false });
      s.extraMats.push(mat);
      const p = new THREE.Points(g, mat);
      p.renderOrder = 5;
      p.visible = !!cats[c];
      p.userData = { cat: c, ids };
      s.root.add(p);
      s.markers.push(p);
    }

    // framing
    const bb = doc.meta.bbox_core || doc.meta.bbox || { min: [-10, -10, -10], max: [10, 10, 10] };
    const full = doc.meta.bbox || bb;
    const lo = bb.min, hi = bb.max;
    s.center = new THREE.Vector3((lo[0] + hi[0]) / 2, (lo[2] + hi[2]) / 2, -(lo[1] + hi[1]) / 2);
    s.ext = { x: hi[0] - lo[0], y: hi[1] - lo[1], z: hi[2] - lo[2] };
    s.size = Math.max(s.ext.x, s.ext.y, s.ext.z, 4);
    s.zmin = lo[2];
    s.zmax = hi[2];
    // default cut: a head height above the floor most gameplay entities stand on
    const zs = doc.entities.filter((e) => /^(enemy|spawn|item|weapon|door)$/.test(e.cat)).map((e) => e.z).sort((a, b) => a - b);
    const floor = zs.length ? zs[zs.length >> 1] : lo[2] + (hi[2] - lo[2]) / 3;
    s.cutDefault = clamp(floor + 2.2, lo[2], hi[2]);
    const diag = Math.hypot(full.max[0] - full.min[0], full.max[1] - full.min[1], full.max[2] - full.min[2]);
    camera.far = Math.max(1000, diag * 3);
    camera.near = 0.1;
    camera.updateProjectionMatrix();
    controls.maxDistance = Math.max(200, diag * 1.5);
    return s;
  }

  // ---- textures -------------------------------------------------------------

  function finishTex(t, srgbFlag) {
    t.encoding = srgbFlag ? THREE.sRGBEncoding : THREE.LinearEncoding;
    t.wrapS = t.wrapT = THREE.RepeatWrapping;
    t.anisotropy = ANISO;
    t.flipY = false;                   // game UVs are top-down, like the raw block rows
    t.needsUpdate = true;
    return t;
  }

  async function loadTex(id, info) {
    const code = info ? CODE[String(info.format).split('_')[0]] : undefined;
    if (!FORCE_PNG && (code === 0 || GPU[code])) {
      const b = await (await fetchOk(API() + 'tex/' + id + '?max=' + TEX_MAX)).arrayBuffer();
      const dv = new DataView(b);
      if (b.byteLength < 16 || String.fromCharCode(dv.getUint8(0), dv.getUint8(1), dv.getUint8(2), dv.getUint8(3)) !== 'WTX1') {
        throw new Error('no WTX1 block');
      }
      const c = dv.getUint16(4, true), flags = dv.getUint16(6, true), w = dv.getUint16(8, true), h = dv.getUint16(10, true), n = dv.getUint32(12, true);
      if (16 + n > b.byteLength) throw new Error('WTX1 block cut off');
      const data = new Uint8Array(b, 16, n);
      if (c === 0) {
        const t = new THREE.DataTexture(data, w, h, THREE.RGBAFormat);
        t.magFilter = THREE.LinearFilter;
        t.minFilter = THREE.LinearMipmapLinearFilter;
        t.generateMipmaps = true;
        return { tex: finishTex(t, flags & 1), path: 'bc' };
      }
      if (GPU[c]) {
        const t = new THREE.CompressedTexture([{ data, width: w, height: h }], w, h, GPU[c]);
        t.magFilter = THREE.LinearFilter;
        t.minFilter = THREE.LinearFilter;   // one mip only: a mipmap filter would sample an incomplete texture (black)
        // ponytail: drop the CPU copy once it is on the GPU (up to ~700 MB on big maps); a lost
        // WebGL context is not restored, reopen the map then.
        t.onUpdate = () => { t.mipmaps = []; };
        return { tex: finishTex(t, flags & 1), path: 'bc' };
      }
      // served format differs from the stored one and the GPU lacks it: fall through to PNG
    }
    const t = await new Promise((ok, bad) => pngLoader.load(API() + 'tex/' + id + '.png?max=' + PNG_MAX, ok, undefined,
      () => bad(new Error('PNG ' + id))));
    return { tex: finishTex(t, !info || info.srgb !== false), path: 'png' };
  }
  const pngLoader = new THREE.TextureLoader();

  function loadTextures(s, token) {
    if (s.tex) return s.tex.job;
    const info = new Map(s.doc.textures.map((t) => [t.id, t]));
    const mats = new Map();            // texture id -> material indices
    const weight = new Map();
    s.doc.materials.forEach((m, i) => {
      if (!m.albedo) return;
      if (!mats.has(m.albedo)) { mats.set(m.albedo, []); weight.set(m.albedo, 0); }
      mats.get(m.albedo).push(i);
      weight.set(m.albedo, weight.get(m.albedo) + s.usage[i]);
    });
    const queue = [...mats.keys()].sort((a, b) => weight.get(b) - weight.get(a));   // most drawn first
    const st = s.tex = { total: queue.length, ok: 0, failed: 0, paths: new Set(), job: null };
    const show = () => status('Textures ' + (st.ok + st.failed) + ' / ' + st.total + (st.failed ? ' (' + st.failed + ' failed)' : ''),
      (st.ok + st.failed) / Math.max(1, st.total));
    async function worker() {
      while (queue.length && token === seq) {
        const id = queue.shift();
        try {
          // one retry: a busy local server may drop a connection now and then
          const { tex, path } = await loadTex(id, info.get(id)).catch(() => loadTex(id, info.get(id)));
          if (token !== seq) { tex.dispose(); return; }
          s.textures.set(id, tex);
          st.paths.add(path);
          for (const i of mats.get(id)) {
            const m = s.texMats[i];
            m.map = tex;
            m.color.set(0xffffff);
            m.needsUpdate = true;
          }
          st.ok++;
          dirty = true;
        } catch (e) {
          st.failed++;
        }
        if (ready || !SHOT) show();
        facts();
      }
    }
    st.job = Promise.all(Array.from({ length: 6 }, worker));
    return st.job;
  }

  // ---- collision (lazy) -------------------------------------------------------

  function loadCollision(s, token) {
    if (s.coll) return s.coll;
    s.coll = (async () => {
      status('Loading collision …');
      const d = await (await fetchOk(mapUrl(s.doc.meta.id, 'collision.json'))).json();
      if (token !== seq) return;
      status('Building collision …');
      const enc = d.meta.encoding, o = enc.grid_origin, step = enc.step;
      const p8 = b64(d.p8), p16 = new Uint16Array(b64(d.p16).buffer);
      const i8 = b64(d.i8), i16 = new Uint16Array(b64(d.i16).buffer);
      const want = d.kinds.map((k) => (k in COLL ? k : null));
      const acc = d.kinds.map(() => ({ nv: 0, nt: 0, hulls: 0 }));
      for (const h of d.hulls) if (want[h[0]]) { const a = acc[h[0]]; a.nv += h[2]; a.nt += h[3]; a.hulls++; }
      for (const a of acc) { a.pos = new Float32Array(a.nv * 3); a.idx = new Uint32Array(a.nt * 3); a.v = 0; a.t = 0; }
      let q8 = 0, q16 = 0, r8 = 0, r16 = 0;
      for (const [k, , nv, nt, bits, bx, by, bz] of d.hulls) {
        const a = acc[k], keep = !!want[k], base = a.v;
        for (let i = 0; i < nv; i++) {
          let gx, gy, gz;
          if (bits === 8) { gx = bx + p8[q8++]; gy = by + p8[q8++]; gz = bz + p8[q8++]; } else { gx = p16[q16++]; gy = p16[q16++]; gz = p16[q16++]; }
          if (keep) { const j = 3 * (base + i); a.pos[j] = o[0] + gx * step; a.pos[j + 1] = o[1] + gy * step; a.pos[j + 2] = o[2] + gz * step; }
        }
        for (let i = 0; i < nt * 3; i++) {
          const v = nv <= 256 ? i8[r8++] : i16[r16++];
          if (keep) a.idx[a.t++] = base + v;
        }
        if (keep) a.v += nv;
      }
      if (q8 !== p8.length || q16 !== p16.length || r8 !== i8.length || r16 !== i16.length) {
        throw new Error('collision.json: data streams do not match the hulls');
      }
      const g = new THREE.Group();
      let hulls = 0, tris = 0;
      d.kinds.forEach((k, i) => {
        if (!want[i] || !acc[i].nt) return;
        const geo = new THREE.BufferGeometry();
        geo.setAttribute('position', new THREE.BufferAttribute(acc[i].pos, 3));
        geo.setIndex(new THREE.BufferAttribute(acc[i].idx, 1));
        const mat = new THREE.MeshPhongMaterial({ color: srgb(COLL[k]), flatShading: true, side: THREE.DoubleSide, transparent: true, opacity: 0.5,
          depthWrite: false, polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -1, specular: 0, shininess: 0 });
        s.extraMats.push(mat);
        g.add(new THREE.Mesh(geo, mat));
        hulls += acc[i].hulls;
        tris += acc[i].nt;
      });
      g.visible = layers.coll;
      s.root.add(g);
      s.collGroup = g;
      s.collInfo = { hulls, tris, step };
      document.body.dataset.coll = hulls + '/' + tris;
      $('n-coll').textContent = fmt(hulls);
      dirty = true;
      if (ready || !SHOT) status('Collision: ' + fmt(hulls) + ' hulls, ' + fmt(tris) + ' triangles (grid ' + step + ' m)');
    })();
    s.coll.catch((e) => { s.coll = null; status('Error: collision – ' + e.message); });
    return s.coll;
  }

  // ---- open a map -------------------------------------------------------------

  async function openMap(id) {
    const token = ++seq;
    if (S) { disposeScene(S); S = null; }
    ready = false;
    setState('laden');
    closePick();
    dirty = true;
    try {
      status('Preparing scene … (a few seconds the first time a map is opened)', 0.02);
      const doc = await (await fetchOk(mapUrl(id, 'scene.json'))).json();
      if (token !== seq) return;
      const buf = await getBin(mapUrl(id, 'geometry.bin'), (got, total) => {
        if (token === seq) status('Geometry ' + mb(got) + ' / ' + mb(total) + ' MB', got / total);
      });
      if (token !== seq) return;
      status('Building scene …');
      S = buildScene(doc, buf);
      store.set(mapKey(), id);         // only maps that open: a map that fails is not reopened at the next start
      document.title = (doc.meta.title || id) + ' – Maps · Wolfenstein Studio';
      setupCut();
      for (const k of KINDS) $('n-' + k).textContent = fmt(doc.instances.filter((i) => i.kind === k).length);
      $('n-coll').textContent = '';
      buildCatList();
      facts();
      const view = Q.get('view');
      if (view === 'fly' && token === 1) startAtSpawn(); else if (view === 'top' && token === 1) viewTop(); else viewHome();
      const jobs = [];
      if (shade === 'tex') jobs.push(loadTextures(S, token));
      if (layers.coll) jobs.push(loadCollision(S, token));
      if (!SHOT) status(summary());
      await Promise.all(jobs);
      if (token !== seq) return;
      const want = Q.get('pick');
      if (want && token === 1) {
        const i = doc.entities.findIndex((e) => e.name === want);
        if (i >= 0) showPick(i);
      }
      ready = true;
      status(summary());
      draw();                          // now, not on the next frame: headless virtual time may never give one
    } catch (e) {
      if (token !== seq) return;
      ready = true;
      setState('fehler');
      status('Error: ' + e.message);
    }
  }

  function summary() {
    const d = S.doc, t = S.tex;
    let s = d.meta.title + ' · ' + fmt(S.placed) + ' objects';
    if (t) s += ' · textures ' + t.ok + '/' + t.total + (t.paths.size ? ' (' + [...t.paths].join('+').toUpperCase() + ')' : '');
    if (t && t.failed) s += ', ' + t.failed + ' failed';
    if (GAME === 'tno') s += ' · walls without texture: MegaTexture not decoded';
    return s;
  }

  function facts() {
    if (!S) return;
    const m = S.doc.meta, c = m.counts || {}, cov = m.coverage || {}, t = S.tex;
    const rows = [
      ['Objects', fmt(S.placed) + (cov.models_total ? ' / ' + fmt(cov.models_total) : '')],
      ['Triangles', c.triangles_drawn ? (c.triangles_drawn / 1e6).toLocaleString('en-US', { maximumFractionDigits: 2 }) + ' M' : '–'],
      ['Entities', fmt(S.doc.entities.length)],
      ['Textures', t ? t.ok + ' / ' + t.total + (t.paths.size ? ' ' + [...t.paths].join('+').toUpperCase() : '') : 'off', t && t.failed],
      ['GPU formats', document.body.dataset.ext.toUpperCase().replace(/,/g, ' ')],
    ];
    const dl = $('facts');
    dl.textContent = '';
    for (const [k, v, bad] of rows) {
      const dt = document.createElement('dt'), dd = document.createElement('dd');
      dt.textContent = k; dd.textContent = v;
      if (bad) dd.className = 'bad';
      dl.append(dt, dd);
    }
  }

  // ---- views ------------------------------------------------------------------

  function viewHome() {
    if (!S) return;
    setNav('orbit');
    const c = S.center, d = S.size;
    controls.target.copy(c);
    camera.position.set(c.x + d * 0.55, c.y + d * 0.5, c.z + d * 0.6);
    controls.update();
    dirty = true;
  }

  function viewTop() {
    if (!S) return;
    setNav('orbit');
    const c = S.center, half = Math.max(S.ext.y / 2, S.ext.x / 2 / camera.aspect, 2);
    const h = half / Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)) * 1.08;
    controls.target.copy(c);
    camera.position.set(c.x, c.y + h, c.z + h * 1e-4);   // straight down; game +y up the screen
    controls.update();
    dirty = true;
  }

  function startAtSpawn() {
    const ents = S.doc.entities;
    const e = ents.find((x) => /playerstart/i.test(x.cls)) || ents.find((x) => x.cat === 'spawn');
    if (!e) { viewHome(); return; }
    const dx = S.center.x - e.x, dz = S.center.z + e.y;
    const v = openView(e, Math.atan2(-dx, -dz));
    camera.position.copy(v.pos);
    yaw = v.yaw;
    pitch = 0;
    camera.rotation.set(pitch, yaw, 0, 'YXZ');
    document.body.dataset.flystart = v.up + ',' + v.turn + ',' + v.score.toFixed(1);
    setNav('fly', true);
  }

  // A player start can face a wall or sit inside rubble the game hides (TNO c06p1, c03p2, c01p3,
  // c16p1). Look around from eye height with a tiny depth render (8 directions, the one towards
  // the map centre first) and take a direction whose nearer quarter of pixels is >= OPEN m away
  // (at 4 m a rubble pile filling the view still passed); if none is, try again higher up.
  // Returns the most open pose found.
  function openView(e, yaw0) {
    const W = 48, H = 27, OPEN = 8;
    const rt = new THREE.WebGLRenderTarget(W, H);
    const px = new Uint8Array(W * H * 4), d = new Float32Array(W * H);
    const cam = new THREE.PerspectiveCamera(camera.fov, W / H, 0.05, 400);
    const mat = new THREE.MeshDepthMaterial({ depthPacking: THREE.RGBADepthPacking, side: THREE.DoubleSide });
    const hidden = S.markers.concat(pickMark, S.collGroup || []).filter((o) => o.visible);
    const keep = { bg: scene.background, clip: renderer.clippingPlanes, cc: renderer.getClearColor(new THREE.Color()), ca: renderer.getClearAlpha() };
    hidden.forEach((o) => { o.visible = false; });
    scene.background = null;
    renderer.clippingPlanes = [];
    renderer.setClearColor(0xffffff, 1);
    scene.overrideMaterial = mat;
    let best = null;
    try {
      for (const up of [1.7, 3, 5, 8, 12]) {
        let here = null;
        for (let k = 0; k < 8; k++) {
          cam.position.set(e.x, e.z + up, -e.y);
          cam.rotation.set(0, yaw0 + k * Math.PI / 4, 0, 'YXZ');
          cam.updateMatrixWorld();
          renderer.setRenderTarget(rt);
          renderer.clear();
          renderer.render(scene, cam);
          renderer.readRenderTargetPixels(rt, 0, 0, W, H, px);
          for (let i = 0; i < W * H; i++) {        // RGBADepthPacking -> depth in [0,1] -> metres along the view
            const z = px[4 * i + 3] / 256 + px[4 * i + 2] / 65536 + px[4 * i + 1] / 16777216 + px[4 * i] / 4294967296;
            d[i] = z > 0.9999 ? cam.far : cam.near * cam.far / (cam.far - z * (cam.far - cam.near));
          }
          d.sort();
          const v = { score: d[(W * H) >> 2], pos: cam.position.clone(), yaw: yaw0 + k * Math.PI / 4, up, turn: k * 45 };
          if (k === 0 && v.score >= OPEN) { here = v; break; }   // the view towards the centre is open: keep it
          if (!here || v.score > here.score) here = v;
        }
        if (!best || here.score > best.score) best = here;
        if (here.score >= OPEN) { best = here; break; }
      }
    } finally {
      scene.overrideMaterial = null;
      renderer.setRenderTarget(null);
      renderer.setClearColor(keep.cc, keep.ca);
      renderer.clippingPlanes = keep.clip;
      scene.background = keep.bg;
      hidden.forEach((o) => { o.visible = true; });
      rt.dispose();
      mat.dispose();
    }
    return best;
  }

  // ---- section cut ------------------------------------------------------------

  function setupCut() {
    const r = $('cutz');
    r.min = (Math.floor(S.zmin) - 1).toString();
    r.max = (Math.ceil(S.zmax) + 1).toString();
    const q = parseFloat(Q.get('cut'));
    r.value = String(Number.isFinite(q) && seq === 1 ? q : Math.round(S.cutDefault * 4) / 4);
    applyCut();
  }
  function applyCut() {
    const r = $('cutz');
    $('cut').checked = cutOn;
    r.disabled = !cutOn;
    cutPlane.constant = +r.value;
    renderer.clippingPlanes = cutOn ? [cutPlane] : [];
    $('cutv').textContent = cutOn ? 'z ≤ ' + (+r.value).toLocaleString('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 2 }) + ' m' : 'off';
    dirty = true;
  }
  $('cut').addEventListener('change', (e) => { cutOn = e.target.checked; applyCut(); });
  $('cutz').addEventListener('input', applyCut);

  // ---- layers, markers, shading -----------------------------------------------

  for (const box of document.querySelectorAll('[data-layer]')) {
    const k = box.dataset.layer;
    box.checked = layers[k];
    box.addEventListener('change', () => {
      layers[k] = box.checked;
      if (!S) return;
      if (k === 'coll') {
        if (S.collGroup) S.collGroup.visible = box.checked;
        else if (box.checked) loadCollision(S, seq);
      } else {
        S.kinds[k].visible = box.checked;
      }
      dirty = true;
    });
  }

  function buildCatList() {
    const box = $('cats');
    box.textContent = '';
    for (const p of S.markers) {
      const c = p.userData.cat, def = CATS.find((x) => x[0] === c) || [c, c, '#6c7784'];
      const label = document.createElement('label');
      label.className = 'check';
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.checked = !!cats[c];
      input.addEventListener('change', () => {
        cats[c] = input.checked;
        p.visible = input.checked;
        if (!input.checked && pickMark.userData.cat === c) closePick();
        dirty = true;
      });
      const dotEl = document.createElement('i');
      dotEl.style.setProperty('--c', def[2]);
      const name = document.createElement('span');
      name.textContent = def[1];
      const n = document.createElement('em');
      n.textContent = fmt(p.userData.ids.length);
      label.append(input, dotEl, name, n);
      box.append(label);
    }
    document.body.dataset.markers = S.markers.reduce((a, p) => a + p.userData.ids.length, 0);
  }

  function markersXray(on) {           // orbit: markers show through walls; flying: only what is in sight
    if (S) for (const p of S.markers) p.material.depthTest = !on;
  }

  function setShade(v) {
    shade = v;
    for (const r of document.querySelectorAll('input[name=shade]')) r.checked = r.value === v;
    if (!S) return;
    for (const im of S.meshes) im.material = (v === 'tex' ? S.texMats : S.flatMats)[im.userData.mat];
    if (v === 'tex') loadTextures(S, seq);
    dirty = true;
  }
  for (const r of document.querySelectorAll('input[name=shade]')) r.addEventListener('change', () => setShade(r.value));
  setShade(shade);

  // ---- picking ----------------------------------------------------------------

  const v3 = new THREE.Vector3();
  function pickAt(px, py) {
    if (!S) return;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    let best = -1, bd = 12 * 12, bz = Infinity;
    for (const p of S.markers) {
      if (!p.visible) continue;
      const a = p.geometry.attributes.position.array, ids = p.userData.ids;
      for (let j = 0; j < ids.length; j++) {
        if (cutOn && a[3 * j + 2] > cutPlane.constant) continue;
        v3.set(a[3 * j], a[3 * j + 2], -a[3 * j + 1]).project(camera);
        if (v3.z < -1 || v3.z > 1) continue;
        const dx = (v3.x + 1) / 2 * w - px, dy = (1 - v3.y) / 2 * h - py, d = dx * dx + dy * dy;
        if (d < bd - 4 || (d <= bd + 4 && v3.z < bz)) { best = ids[j]; bd = Math.min(bd, d); bz = v3.z; }
      }
    }
    if (best >= 0) showPick(best); else closePick();
  }

  function showPick(i) {
    const e = S.doc.entities[i];
    const def = CATS.find((x) => x[0] === e.cat) || [e.cat, e.cat, '#6c7784'];
    $('pick').style.setProperty('--c', def[2]);
    $('pickcat').textContent = def[1] + ' · entity ' + i;
    $('pickname').textContent = e.name;
    $('pickcls').textContent = e.cls || '–';
    $('pickdef').textContent = e.def || '–';
    const f = (x) => x.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    $('pickpos').textContent = f(e.x) + '  ' + f(e.y) + '  ' + f(e.z);
    $('pick').hidden = false;
    pickMark.geometry.attributes.position.setXYZ(0, e.x, e.y, e.z);
    pickMark.geometry.attributes.position.needsUpdate = true;
    pickMark.userData.cat = e.cat;
    pickMark.visible = true;
    document.body.dataset.pick = e.name;
    dirty = true;
  }
  function closePick() {
    $('pick').hidden = true;
    pickMark.visible = false;
    delete document.body.dataset.pick;
    dirty = true;
  }
  $('pickx').addEventListener('click', closePick);

  let down = null;
  canvas.addEventListener('pointerdown', (e) => {
    down = { x: e.clientX, y: e.clientY };
    if (nav === 'fly' && document.pointerLockElement !== canvas) lockPointer();
  });
  canvas.addEventListener('pointerup', (e) => {
    if (down && nav === 'orbit' && Math.hypot(e.clientX - down.x, e.clientY - down.y) < 5) {
      const r = canvas.getBoundingClientRect();
      pickAt(e.clientX - r.left, e.clientY - r.top);
    }
    down = null;
  });

  // ---- fly mode ---------------------------------------------------------------

  let yaw = 0, pitch = 0, speed = 8;
  const keys = new Set();
  const fwd = new THREE.Vector3(), right = new THREE.Vector3();

  function lockPointer() {
    try {
      const p = canvas.requestPointerLock();
      if (p && p.catch) p.catch(() => {});
    } catch (e) { /* not allowed here: drag to look */ }
  }

  function setNav(v, keepPose) {
    if (v === nav && !keepPose) return;
    const was = nav;
    nav = v;
    for (const r of document.querySelectorAll('input[name=nav]')) r.checked = r.value === v;
    $('flybtn').classList.toggle('on', v === 'fly');
    $('flybtn').textContent = v === 'fly' ? 'Orbit' : 'Fly mode';
    if (v === 'fly') {
      controls.enabled = false;
      if (!keepPose) {
        camera.getWorldDirection(fwd);
        pitch = Math.asin(clamp(fwd.y, -1, 1));
        yaw = Math.atan2(-fwd.x, -fwd.z);
        camera.rotation.set(pitch, yaw, 0, 'YXZ');
      }
      if (S) speed = clamp(S.size / 25, 4, 40);
      camera.near = 0.05;
      camera.updateProjectionMatrix();
      markersXray(false);
      if (!SHOT) lockPointer();
      if (document.activeElement && document.activeElement !== document.body) document.activeElement.blur();
    } else {
      if (document.pointerLockElement) document.exitPointerLock();
      if (was === 'fly') {
        camera.getWorldDirection(fwd);
        controls.target.copy(camera.position).addScaledVector(fwd, 10);
      }
      camera.near = 0.1;
      camera.updateProjectionMatrix();
      markersXray(true);
      controls.enabled = true;
      controls.update();
    }
    flyHint();
    dirty = true;
  }
  for (const r of document.querySelectorAll('input[name=nav]')) r.addEventListener('change', () => setNav(r.value));
  $('flybtn').addEventListener('click', () => setNav(nav === 'fly' ? 'orbit' : 'fly'));

  function flyHint() {
    const locked = document.pointerLockElement === canvas;
    const h = $('hint');
    h.hidden = nav !== 'fly';
    $('cross').hidden = !(nav === 'fly' && locked);
    h.textContent = locked
      ? 'WASD fly · Q/E down/up · Shift faster · speed ' + speed.toFixed(1) + ' m/s (mouse wheel) · Esc releases the mouse'
      : 'Fly mode: click into the view to look around (or drag with the mouse button held) · WASD fly · F back to orbit';
  }
  document.addEventListener('pointerlockchange', flyHint);

  document.addEventListener('mousemove', (e) => {
    if (nav !== 'fly') return;
    const locked = document.pointerLockElement === canvas;
    if (!locked && !(down && (e.buttons & 1))) return;
    yaw -= e.movementX * 0.0022;
    pitch = clamp(pitch - e.movementY * 0.0022, -1.55, 1.55);
    camera.rotation.set(pitch, yaw, 0, 'YXZ');
    dirty = true;
  });
  canvas.addEventListener('wheel', (e) => {
    if (nav !== 'fly') return;
    e.preventDefault();
    speed = clamp(speed * (e.deltaY < 0 ? 1.25 : 0.8), 0.5, 400);
    flyHint();
  }, { passive: false });

  function flyStep(dt) {
    let f = 0, r = 0, u = 0;
    if (keys.has('KeyW')) f++;
    if (keys.has('KeyS')) f--;
    if (keys.has('KeyD')) r++;
    if (keys.has('KeyA')) r--;
    if (keys.has('KeyE')) u++;
    if (keys.has('KeyQ')) u--;
    if (!f && !r && !u) return false;
    const v = speed * (keys.has('ShiftLeft') || keys.has('ShiftRight') ? 4 : 1) * dt;
    camera.getWorldDirection(fwd);
    right.set(Math.cos(yaw), 0, -Math.sin(yaw));
    camera.position.addScaledVector(fwd, f * v).addScaledVector(right, r * v);
    camera.position.y += u * v;
    return true;
  }

  // ---- keyboard, panel, fullscreen ------------------------------------------

  const typing = (t) => t && (t.tagName === 'SELECT' || t.tagName === 'TEXTAREA' || (t.tagName === 'INPUT' && t.type === 'text'));
  document.addEventListener('keydown', (e) => {
    if (!shown()) return;              // another Studio section has the keyboard
    if (e.code === 'F11') { if (toggleFullscreen(true)) e.preventDefault(); return; }
    if (typing(e.target) || e.ctrlKey || e.altKey || e.metaKey) return;
    if (e.code === 'Escape') { if (nav !== 'fly') closePick(); return; }
    if (e.repeat && !/^Key[WASDQE]$|^Shift/.test(e.code)) return;
    if (e.code === 'KeyF') { setNav(nav === 'fly' ? 'orbit' : 'fly'); return; }
    if (e.code === 'KeyH') { togglePanel(); return; }
    if (e.code === 'KeyT') { setShade(shade === 'tex' ? 'flat' : 'tex'); return; }
    if (nav === 'fly' && /^Key[WASDQE]$|^Shift/.test(e.code)) { keys.add(e.code); e.preventDefault(); }
  });
  document.addEventListener('keyup', (e) => keys.delete(e.code));
  window.addEventListener('blur', () => keys.clear());

  function togglePanel(force) {
    const collapsed = force != null ? force : !document.body.classList.contains('collapsed');
    document.body.classList.toggle('collapsed', collapsed);
    store.set('panel', collapsed ? '0' : '1');
    (collapsed ? $('show') : $('hide')).focus({ preventScroll: true });
  }
  $('hide').addEventListener('click', () => togglePanel(true));
  $('show').addEventListener('click', () => togglePanel(false));
  document.body.classList.toggle('collapsed', (Q.get('panel') || store.get('panel')) === '0');

  const browserFullscreen = () => !document.fullscreenElement && innerHeight >= screen.height - 1 && innerWidth >= screen.width - 1;
  function toggleFullscreen(fromKey) {
    if (document.fullscreenElement) { document.exitFullscreen().catch(() => {}); return true; }
    if (browserFullscreen()) {                 // Edge --start-fullscreen: only the browser's own F11 leaves it
      if (!fromKey) status('Browser full screen: press F11 to leave');
      return false;
    }
    document.documentElement.requestFullscreen().catch(() => status('The browser refused full screen'));
    return true;
  }
  // the button for it is the Studio's own in the top bar (studio.js), the same in every section

  // a long map name does not fit the closed <select>: then it is shown in full below it
  function mapTitle() {
    const sel = $('map'), o = sel.selectedOptions[0], t = o && !sel.disabled ? o.textContent : '';
    const c = mapTitle.cx || (mapTitle.cx = document.createElement('canvas').getContext('2d'));
    c.font = getComputedStyle(sel).font;
    $('maptitle').textContent = t;
    $('maptitle').hidden = !t || c.measureText(t).width <= sel.clientWidth - 34;
  }

  $('map').addEventListener('change', (e) => { mapTitle(); openMap(e.target.value); });
  $('home').addEventListener('click', viewHome);
  $('top').addEventListener('click', viewTop);

  window.addEventListener('resize', () => {
    renderer.setSize(innerWidth, innerHeight);
    camera.aspect = innerWidth / innerHeight;
    camera.updateProjectionMatrix();
    dirty = true;
  });

  // ---- render loop (on demand) ------------------------------------------------

  let last = performance.now();
  function tick(now) {
    const dt = clamp((now - last) / 1000, 0, 0.1);   // frame and timer clocks may disagree: never step backwards
    last = now;
    if (nav === 'fly' && flyStep(dt)) dirty = true;
    if (dirty && !(SHOT && !ready)) draw();
  }
  function frame(now) {
    requestAnimationFrame(frame);
    tick(now);
  }
  // ?shot runs headless on virtual time, which may never deliver an animation frame:
  // tools/verify_mapfront.py then steps the loop itself (and reads the built scene)
  if (SHOT) window.mapview = { tick: () => tick(performance.now()), state: () => S };
  function draw() {
    dirty = false;
    renderer.render(scene, camera);
    if (SHOT) {                        // for tools/verify_mapfront.py: camera position and view-projection matrix
      document.body.dataset.cam = camera.position.toArray().map((v) => v.toFixed(3)).join(',');
      document.body.dataset.vp = new THREE.Matrix4().multiplyMatrices(camera.projectionMatrix, camera.matrixWorldInverse).elements.join(',');
    }
    if (ready && S && document.body.dataset.state === 'laden') {
      const b = document.body.dataset;
      b.map = S.doc.meta.id;
      b.inst = S.placed;
      b.draws = S.meshes.length;
      b.tex = S.tex ? S.tex.ok + '/' + S.tex.total : 'aus';
      b.texfail = S.tex ? S.tex.failed : 0;
      b.texpath = S.tex ? [...S.tex.paths].join('+') : '';
      b.calls = renderer.info.render.calls;
      b.tris = renderer.info.render.triangles;
      b.glerr = gl.getError();
      b.err = errors;
      setState('fertig');
    }
  }
  // ---- Studio: the section is shown or the game changes ------------------------

  document.addEventListener('studio:section', () => {
    if (!shown()) { keys.clear(); if (document.pointerLockElement) document.exitPointerLock(); return; }
    renderer.setSize(innerWidth, innerHeight);
    camera.aspect = innerWidth / innerHeight;
    camera.updateProjectionMatrix();
    dirty = true;
    if (loadedFor !== GAME) loadMaps();
  });
  document.addEventListener('studio:game', (ev) => {
    if (ev.detail === GAME) return;
    GAME = ev.detail;
    seq++;                             // a map still loading for the other game is dropped
    if (S) { disposeScene(S); S = null; }
    closePick();
    setShade(defaultShade());
    const sel = $('map');
    sel.textContent = '';
    sel.append(new Option('Loading maps …'));
    sel.disabled = true;
    mapTitle();
    $('facts').textContent = '';
    $('cats').textContent = '';
    for (const k of KINDS.concat('coll')) $('n-' + k).textContent = '';
    setState('start');
    loadedFor = null;
    dirty = true;
    if (shown()) loadMaps();
  });

  requestAnimationFrame(frame);
  flyHint();
  if (shown()) loadMaps();
})();
