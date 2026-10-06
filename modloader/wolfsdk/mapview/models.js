/* Modelle: the 3D model viewer of the Studio (three.js r128, vendored). studio.js owns the list
   and the page; it calls window.StudioModels(kit) once and then open(game, id) per model.

   Data contract (wolfsdk/studio.py kind "models", served by wolfsdk/mapserver.py):
     /api/<g>/models/info?id=          {id, name, group, archive, bounds: {min, max}, lods, joints,
                                        surfaces: [{index, name, material, verts, tris, albedo, albedo_size}]}
     /api/<g>/models/mesh.bin?id=&lod=0
         "WMD1" u32 surface count, per surface: u32 nv, u32 ni, f32 position[3nv],
         f32 normal[3nv] (0,0,0 = unknown), f32 uv[2nv], u32 index[ni]; little endian,
         info.surfaces order, game space (+Z up, as the map viewer: rotated by the `world` group);
         the triangles are counter-clockwise (three.js front faces) and drawn as sent, the server
         zeroes normals its triangles contradict (md6.fix_normals), those are computed here
     albedo: a PNG under /api/..., game UVs are top-down (flipY off, as app.js finishTex)
   Optional per surface, used when the server sends them: albedo_error (the colour map exists but
   cannot be shown), visible:false (hidden at start: a gore, gear or upgrade variant its md6Def's
   meshKits switch off; info.md6def names that def; "Standard" goes back to it), alpha:'mask'
   (cut-out at 50 %).

   Eigener Skin: a PNG from the disk (checked by its signature: accept= only filters the dialog)
   replaces the albedo of the chosen surface's material in this page only (the game swaps a material's image, so every surface of that material shows
   it). Nothing is written; only the export sends it to the local server.

   Exportieren: /api/<g>/models/export?id=&format=&surfaces=<the switched-on indices> (wolfsdk/modelexport.py)
   as a download; with an own skin on one of those surfaces a POST with the PNG as the body and
   skin=<material>. Bind pose only: no skeleton, weights or animations. */
window.StudioModels = function (kit) {
  'use strict';

  const { $, el, num, dec, plural, fetchOk, getJson, api, toast, showPane, showErr, counted } = kit;
  const pane = $('mpane'), box = $('mview'), canvas = $('mcanvas');
  const GREY = 0x8a9099;
  let R = null, scene, world, camera, controls, drawn = 0, dirty = false;
  let M = null;                          // the model on screen
  let gen = 0, ctl = null, stale = 0, wire = false, message = null, want = null;
  let exporting = false, lastExport = null;

  // the export control goes under the skin (index.html is not this feature's file: built here)
  const expHead = el('h2', 'cap', 'Export'), expRow = el('div', 'row'), expNote = el('p', 'dim small');
  const expFmt = el('select'), expBtn = el('button', 'accent', 'Export');
  expFmt.id = 'mexpfmt';
  expFmt.setAttribute('aria-label', 'Export format');
  for (const [v, t, tip] of [['glb', 'GLB (one file)', 'glTF 2.0 binary, textures embedded: Blender, 3D Viewer, Sketchfab'],
    ['obj', 'OBJ + PNG (ZIP)', 'ZIP with .obj, .mtl and the textures as PNG'], ['gltf', 'glTF + PNG (ZIP)', 'ZIP with .gltf, .bin and the textures as PNG']]) {
    const o = new Option(t, v);
    o.title = tip;
    expFmt.append(o);
  }
  expBtn.id = 'mexport';
  expBtn.type = 'button';
  expBtn.disabled = true;
  expBtn.title = 'Saves the switched-on surfaces with their colour textures (and your own skin) as a 3D file for Blender and similar tools.\n'
    + 'Base pose only: skeleton, weights and animations are not exported.';
  expNote.id = 'mexpnote';
  expRow.append(expFmt, expBtn);
  $('mskinnote').after(expHead, expRow, expNote);

  // the animation player sits under the facts (built here as well): clips made for the model's skeleton
  const anHead = el('h2', 'cap', 'Animation'), anQ = el('input'), anRow = el('div', 'row');
  const anSel = el('select'), anPlay = el('button', null, 'Play'), anNote = el('p', 'dim small');
  anQ.type = 'search';
  anQ.id = 'manimq';
  anQ.placeholder = 'Filter animations …';
  anQ.setAttribute('aria-label', 'Filter animations');
  anSel.id = 'manim';
  anSel.setAttribute('aria-label', 'Animation');
  anPlay.id = 'manimplay';
  anPlay.type = 'button';
  anNote.id = 'manimnote';
  anRow.append(anSel, anPlay);
  $('mfacts').after(anHead, anQ, anRow, anNote);

  // ---- three.js, created on the first model ------------------------------------------------

  function init() {
    if (R) return true;
    try {
      R = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    } catch (e) {
      R = null;
      return false;
    }
    R.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    R.outputEncoding = THREE.sRGBEncoding;
    R.toneMapping = THREE.ACESFilmicToneMapping;
    R.setClearColor(0x000000, 0);        // the pane's gradient shows through
    scene = new THREE.Scene();
    world = new THREE.Group();
    world.rotation.x = -Math.PI / 2;     // game (x, y, z) -> three (x, z, -y), as app.js
    scene.add(world);
    const pm = new THREE.PMREMGenerator(R);
    scene.environment = pm.fromScene(studioRoom(), 0.04).texture;   // reflections for the metal/gloss maps
    pm.dispose();
    scene.add(new THREE.HemisphereLight(0xffffff, 0x3c3a36, 0.25));
    camera = new THREE.PerspectiveCamera(35, 1, 0.01, 1000);
    const head = new THREE.DirectionalLight(0xffffff, 0.65);   // a head light: the side you look at is lit
    head.position.set(0.4, 0.7, 1);
    head.target.position.set(0, 0, -1);
    camera.add(head, head.target);
    scene.add(camera);
    controls = new THREE.OrbitControls(camera, canvas);
    controls.screenSpacePanning = true;
    controls.addEventListener('change', () => { dirty = true; });
    new ResizeObserver(resize).observe(box);
    resize();
    requestAnimationFrame(loop);
    return true;
  }

  // a grey room with a few bright panels: a neutral studio environment for PBR reflections
  function studioRoom() {
    const room = new THREE.Scene(), cube = new THREE.BoxGeometry(1, 1, 1);
    const walls = new THREE.Mesh(cube, new THREE.MeshBasicMaterial({ color: 0x3a3c40, side: THREE.BackSide }));
    walls.scale.set(20, 10, 20);
    room.add(walls);
    for (const [x, y, z, sx, sy, sz, k] of [[0, 4.9, 0, 8, 0.1, 8, 3], [-9.9, 1, 2, 0.1, 4, 6, 2], [9.9, 2, -3, 0.1, 3, 5, 1.5], [0, 1, 9.9, 6, 3, 0.1, 1]]) {
      const panel = new THREE.Mesh(cube, new THREE.MeshBasicMaterial({ color: new THREE.Color().setScalar(k) }));
      panel.position.set(x, y, z);
      panel.scale.set(sx, sy, sz);
      room.add(panel);
    }
    return room;
  }

  function resize() {
    const w = box.clientWidth, h = box.clientHeight;
    if (!R || !w || !h) return;
    R.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    dirty = true;
  }

  function loop() {
    requestAnimationFrame(loop);
    if (M && M.playing && !pane.hidden) {   // a playing clip redraws every frame; otherwise only on change
      const now = performance.now();
      M.mixer.update((now - M.clock) / 1000);
      M.clock = now;
      dirty = true;
    }
    if (!dirty || pane.hidden) return;
    dirty = false;
    R.render(scene, camera);
    drawn = R.info.render.triangles;
  }

  // ---- mesh.bin -------------------------------------------------------------------------------

  function parse(buf, count) {
    const bad = (why) => new Error('Model data damaged: ' + why + '.');
    const dv = new DataView(buf), n = buf.byteLength;
    if (n < 8 || String.fromCharCode(dv.getUint8(0), dv.getUint8(1), dv.getUint8(2), dv.getUint8(3)) !== 'WMD1') throw bad('no WMD1 header');
    if (dv.getUint32(4, true) !== count) throw bad(dv.getUint32(4, true) + ' surfaces in the geometry, ' + count + ' in the description');
    const out = [];
    let o = 8;
    for (let s = 1; s <= count; s++) {
      if (o + 8 > n) throw bad('the data ends before surface ' + s);
      const nv = dv.getUint32(o, true), ni = dv.getUint32(o + 4, true);
      o += 8;
      if (ni % 3) throw bad('surface ' + s + ' has ' + ni + ' indices, not a multiple of 3');
      if (o + nv * 32 + ni * 4 > n) throw bad('surface ' + s + ' is cut off');
      const pos = new Float32Array(buf, o, nv * 3), nrm = new Float32Array(buf, o + nv * 12, nv * 3);
      const uv = new Float32Array(buf, o + nv * 24, nv * 2), idx = new Uint32Array(buf, o + nv * 32, ni);
      o += nv * 32 + ni * 4;
      for (let i = 0; i < ni; i++) if (idx[i] >= nv) throw bad('surface ' + s + ' points to vertex ' + idx[i] + ' but has only ' + nv);
      out.push({ pos, nrm, uv, idx });
    }
    if (o !== n) throw bad(num(n - o) + ' bytes too many at the end');
    return out;
  }

  // the server's winding is right (counter-clockwise, checked by signed volume); unknown normals
  // (0,0,0: none stored, or zeroed by the server because the triangles contradict them) come from
  // the triangles, the rest stay as sent
  function geometry(s) {
    const { pos: p, nrm: n, idx } = s, nv = p.length / 3;
    const ok = (i) => n[3 * i] * n[3 * i] + n[3 * i + 1] * n[3 * i + 1] + n[3 * i + 2] * n[3 * i + 2] > 0.01;
    let known = 0;
    for (let i = 0; i < nv; i++) if (ok(i)) known++;
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(p, 3));
    g.setAttribute('uv', new THREE.BufferAttribute(s.uv, 2));
    g.setIndex(new THREE.BufferAttribute(idx, 1));
    if (known === nv) {
      g.setAttribute('normal', new THREE.BufferAttribute(n, 3));
    } else {
      g.computeVertexNormals();
      const c = g.attributes.normal.array;
      for (let i = 0; known && i < nv; i++) if (ok(i)) { c[3 * i] = n[3 * i]; c[3 * i + 1] = n[3 * i + 1]; c[3 * i + 2] = n[3 * i + 2]; }
    }
    return { g, computed: nv - known };
  }

  function build(info, surfs) {
    const root = new THREE.Group(), lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
    const parts = info.surfaces.map((sf, i) => {
      const s = surfs[i], { g, computed } = geometry(s);
      for (let j = 0; j < s.pos.length; j++) {
        const v = s.pos[j], k = j % 3;
        if (v < lo[k]) lo[k] = v;
        if (v > hi[k]) hi[k] = v;
      }
      const mat = new THREE.MeshPhysicalMaterial({ color: new THREE.Color(GREY).convertSRGBToLinear(), side: THREE.DoubleSide,
        roughness: 0.75, metalness: 0, wireframe: wire });
      if (sf.alpha === 'mask') mat.alphaTest = 0.5;
      const mesh = new THREE.Mesh(g, mat);
      mesh.visible = sf.visible !== false;
      root.add(mesh);
      return { sf, mesh, mat, tris: s.idx.length / 3, verts: s.pos.length / 3, computed,
        tex: sf.albedo ? 'laden' : sf.albedo_error ? 'fehler' : 'ohne', err: sf.albedo_error || null, orig: null, skin: null };
    });
    world.add(root);
    return { root, parts, box: isFinite(lo[0]) ? { min: lo, max: hi } : null };
  }

  function dispose(m) {
    if (m.mixer) m.mixer.stopAllAction();
    m.playing = false;
    world.remove(m.root);
    for (const p of m.parts) { p.mesh.geometry.dispose(); p.mat.dispose(); }
    for (const t of m.textures) t.dispose();
    if (m.skin) m.skin.tex.dispose();
  }

  // ---- textures --------------------------------------------------------------------------------

  async function imageOf(blob) {
    const u = URL.createObjectURL(blob);
    try {
      const im = new Image();
      im.src = u;
      await im.decode();
      return im;
    } finally {
      URL.revokeObjectURL(u);
    }
  }

  function texture(im, linear) {
    const t = new THREE.Texture(im);
    t.encoding = linear ? THREE.LinearEncoding : THREE.sRGBEncoding;
    t.wrapS = t.wrapT = THREE.RepeatWrapping;
    t.flipY = false;                     // game UVs are top-down, like the image rows
    t.anisotropy = Math.min(8, R.capabilities.getMaxAnisotropy());
    t.needsUpdate = true;
    return t;
  }

  function setMap(p, t) {
    p.mat.map = t;
    p.mat.color.set(t ? 0xffffff : new THREE.Color(GREY).convertSRGBToLinear());
    p.mat.needsUpdate = true;
    dirty = true;
  }

  // maps load for the surfaces given (at open: the visible ones); a surface switched on later brings its own
  async function loadTextures(m, my, signal, parts) {
    const urls = [...new Set(parts.map((p) => p.sf.albedo).filter((u) => u && !m.texReq.has(u)))];
    urls.forEach((u) => m.texReq.add(u));
    m.tex.total += urls.length;
    await Promise.all(urls.map(async (u) => {
      let t = null, err = null;
      try {
        if (!/^\/api\//.test(u)) throw new Error('unexpected address ' + u);
        t = texture(await imageOf(await (await fetchOk(u, { signal })).blob()));
      } catch (e) {
        err = e.name === 'EncodingError' ? 'image not readable' : e.message;
      }
      if (my !== gen) { if (t) t.dispose(); return; }
      if (t) m.textures.push(t);
      for (const p of m.parts) {
        if (p.sf.albedo !== u) continue;
        p.tex = t ? 'ok' : 'fehler';
        p.err = err;
        p.orig = t;
        if (t && !p.skin) setMap(p, t);
      }
      m.tex[t ? 'ok' : 'failed']++;
      rows();
      showFacts();
    }));
  }

  const WHITE = new THREE.DataTexture(new Uint8Array([255, 255, 255, 255]), 1, 1);
  WHITE.needsUpdate = true;

  // three r128 samples alphaMap on uv and aoMap on uv2; the game's hair shaders do the opposite
  // (strand mask on in_TexCoord1, colour and AO on the first set), so the two chunks swap coordinates
  function maskOnUv2(mat) {
    mat.onBeforeCompile = (sh) => {
      sh.fragmentShader = sh.fragmentShader
        .replace('#include <alphamap_fragment>', THREE.ShaderChunk.alphamap_fragment.replace(/\bvUv\b/g, 'vUv2'))
        .replace('#include <aomap_fragment>', THREE.ShaderChunk.aomap_fragment.replace(/\bvUv2\b/g, 'vUv'));
    };
    mat.customProgramCacheKey = () => 'mask-on-uv2';
  }

  // Material maps (studio.model_pbr): normal, roughness/metal, occlusion, a base colour with metal tint
  // or alpha. A material without them, or one that fails, keeps the colour map alone.
  async function loadPbr(m, my, signal, parts) {
    const urls = [...new Set(parts.map((p) => p.sf.pbr).filter((u) => u && /^\/api\//.test(u) && !m.pbrReq.has(u)))];
    urls.forEach((u) => m.pbrReq.add(u));
    await Promise.all(urls.map(async (u) => {
      const parts = m.parts.filter((p) => p.sf.pbr === u);
      try {
        const info = await (await fetchOk(u, { signal })).json();
        const maps = {};
        await Promise.all(Object.entries(info.maps || {}).map(async ([slot, url]) => {
          if (!/^\/api\//.test(url)) return;
          maps[slot] = texture(await imageOf(await (await fetchOk(url, { signal })).blob()), slot !== 'base');
        }));
        if (my !== gen) { Object.values(maps).forEach((t) => t.dispose()); return; }
        m.textures.push(...Object.values(maps));
        for (const p of parts) {
          const mat = p.mat;
          if (maps.base) { p.orig = maps.base; if (!p.skin) setMap(p, maps.base); }
          if (maps.mr) { mat.roughnessMap = mat.metalnessMap = maps.mr; mat.roughness = mat.metalness = 1; }
          else if (info.roughness != null) mat.roughness = info.roughness;
          // glTF convention (green flipped server-side), UVs top-down like glTF: as GLTFLoader, y scale -1
          if (maps.normal) { mat.normalMap = maps.normal; mat.normalScale.set(1, -1); }
          if (maps.mask && p.sf.uv2 && /^\/api\//.test(p.sf.uv2)) {   // hair: the strand mask on the second uv set
            const uv2 = new Float32Array(await (await fetchOk(p.sf.uv2, { signal })).arrayBuffer());
            if (my !== gen) return;
            p.mesh.geometry.setAttribute('uv2', new THREE.BufferAttribute(uv2, 2));
            mat.alphaMap = maps.mask;
            mat.aoMap = maps.occ || WHITE;   // an aoMap is what gives three's shader the second uv
            maskOnUv2(mat);
          } else if (maps.occ) { p.mesh.geometry.setAttribute('uv2', p.mesh.geometry.attributes.uv); mat.aoMap = maps.occ; }
          if (info.emissive && mat.map) { mat.emissive.set(0xffffff); mat.emissiveMap = mat.map; }
          if (info.alpha === 'mask' || (info.alpha === 'mask2' && mat.alphaMap)) mat.alphaTest = 0.5;
          // r128: F0 = 0.16 * reflectivity^2, so glTF's specular factor (F0 / 0.04) is reflectivity 0.5 * sqrt(f)
          if (info.specular != null) mat.reflectivity = 0.5 * Math.sqrt(info.specular);
          if (info.clearcoat) { mat.clearcoat = info.clearcoat[0]; mat.clearcoatRoughness = info.clearcoat[1]; }
          mat.needsUpdate = true;
        }
        dirty = true;
      } catch (e) {
        if (e.name !== 'AbortError') console.warn('material maps of', u, e.message);
      }
    }));
  }

  // ---- open a model -----------------------------------------------------------------------------

  function wait(text) {
    $('mwait').hidden = !text;
    $('mwait').querySelector('span').textContent = text || '';
  }

  function fail(text) {
    message = text;
    wait('');
    $('mmsg').textContent = text;
    $('mmsg').hidden = false;
    pane.dataset.loaded = 'fehler';
  }

  function stop() {
    gen++;
    if (ctl) { ctl.abort(); ctl = null; }
    if (M && !M.done) { dispose(M); M = null; }   // a finished model stays for the way back to this section
    wait('');
  }

  async function open(game, id) {
    showPane('mpane');
    if (M && M.game === game && M.id === id && M.done) { dirty = true; return; }   // back from another section
    stop();
    const my = gen;
    want = id;
    message = null;
    if (M) { dispose(M); M = null; }
    const tail = id.split('/').pop().replace(/\$.*/, '');
    $('mtitle').textContent = tail;
    $('msub').textContent = id;
    $('mfacts').textContent = '';
    $('msurfs').textContent = '';
    $('mmsg').hidden = true;
    skinUi();
    animUi();
    pane.dataset.loaded = '0';
    if (!init()) { fail('3D view not possible: this browser does not provide WebGL.'); return; }
    dirty = true;
    ctl = new AbortController();
    const signal = ctl.signal;
    wait('Reading model …');
    let info, surfs;
    try {
      info = await getJson(api('models', 'info', { id }), { signal });
      if (my !== gen) { stale++; return; }
      $('mtitle').textContent = info.name || tail;
      if (counted && info.surfaces) counted(info);
      if (!info.surfaces || !info.surfaces.length) { fail('This model has no surfaces that can be shown.'); return; }
      wait('Loading geometry …');
      const buf = await (await fetchOk(api('models', 'mesh.bin', { id, lod: 0 }), { signal })).arrayBuffer();
      if (my !== gen) { stale++; return; }
      surfs = parse(buf, info.surfaces.length);
    } catch (e) {
      if (my !== gen || e.name === 'AbortError') { stale++; return; }
      if (!info) { message = e.message; wait(''); showErr(e.message); return; }   // unknown id, unreadable model: as the other sections
      fail(e.message);
      return;
    }
    M = Object.assign({ game, id, info, textures: [], tex: { total: 0, ok: 0, failed: 0 }, texReq: new Set(), pbrReq: new Set(),
      skin: null, done: false }, build(info, surfs));
    fit();
    rows();
    skinUi();
    showFacts();
    animUi();
    const shown = M.parts.filter((p) => p.mesh.visible);
    wait(shown.some((p) => p.sf.albedo) ? 'Loading textures …' : '');
    await loadTextures(M, my, signal, shown);
    if (my !== gen) { stale++; return; }
    wait('');                              // the colour maps are on: material maps follow without a veil
    await loadPbr(M, my, signal, shown);
    if (my !== gen) { stale++; return; }
    wait('');
    M.done = true;
    ctl = null;
    pane.dataset.loaded = '1';
    showFacts();
  }

  // ---- animation: md6skl bones + skin weights + md6anim tracks (server: wolfsdk/md6anim.py) ----------------

  function animUi() {
    const ok = !!(M && (M.game === 'tnc' || M.game === 'yb') && M.info.format === 'md6mesh' && M.info.joints > 0);
    for (const x of [anHead, anQ, anRow, anNote]) x.hidden = !ok;
    anSel.textContent = '';
    anSel.append(new Option('Base pose', ''));
    anSel.disabled = anPlay.disabled = true;
    anPlay.textContent = 'Play';
    anNote.textContent = '';
    if (!ok) return;
    const m = M;
    anNote.textContent = 'Looking for animations …';
    getJson(api('models', 'anims.json', { id: m.id })).then((L) => {
      if (m !== M) return;
      m.anims = L.anims;
      fillAnims();
      anNote.textContent = L.anims.length ? plural(L.anims.length, 'animation', 'animations') + ' for ' + tailOf(L.skeleton)
        : 'No animation names this skeleton (' + tailOf(L.skeleton) + ').';
    }).catch((e) => { if (m === M) anNote.textContent = e.message; });
  }

  function fillAnims() {
    const q = anQ.value.trim().toLowerCase(), cur = anSel.value, groups = new Map();
    anSel.textContent = '';
    anSel.append(new Option('Base pose', ''));
    for (const a of (M && M.anims) || []) {
      if (q && !a.id.toLowerCase().includes(q)) continue;
      if (!groups.has(a.group)) {
        const og = document.createElement('optgroup');
        og.label = a.group;
        groups.set(a.group, og);
        anSel.append(og);
      }
      groups.get(a.group).append(new Option(a.name, a.id));
    }
    anSel.value = cur;
    if (anSel.value !== cur) anSel.value = '';
    anSel.disabled = !(M && M.anims && M.anims.length);
  }

  // first clip: the surfaces become SkinnedMeshes on the model's bones (bind pose = the md6skl's)
  async function rig(m) {
    if (m.rig) return m.rig;
    const [S, buf] = await Promise.all([getJson(api('models', 'skeleton.json', { id: m.id })),
      fetchOk(api('models', 'skin.bin', { id: m.id })).then((r) => r.arrayBuffer())]);
    const bones = S.names.map((n, i) => {
      const b = new THREE.Bone();
      b.name = 'j' + i;                  // joint names can repeat; tracks address bones by index
      b.position.fromArray(S.pos[i]);
      b.quaternion.fromArray(S.rot[i]);
      return b;
    });
    bones.forEach((b, i) => (S.parents[i] >= 0 ? bones[S.parents[i]] : m.root).add(b));
    m.root.updateMatrixWorld(true);
    const skeleton = new THREE.Skeleton(bones), dv = new DataView(buf);
    let o = 8;
    for (let i = 0; i < dv.getUint32(4, true) && i < m.parts.length; i++) {
      const nv = dv.getUint32(o, true), p = m.parts[i];
      const js = new Uint16Array(buf.slice(o + 4, o + 4 + 8 * nv)), ws = new Float32Array(buf.slice(o + 4 + 8 * nv, o + 4 + 24 * nv));
      o += 4 + 24 * nv;
      if (nv !== p.verts || !nv) continue;   // a surface the mesh.bin packs differently stays rigid
      const g = p.mesh.geometry;
      g.setAttribute('skinIndex', new THREE.Uint16BufferAttribute(js, 4));
      g.setAttribute('skinWeight', new THREE.Float32BufferAttribute(ws, 4));
      p.mat.skinning = true;             // three r128 compiles the skinning chunks only with this flag
      p.mat.needsUpdate = true;
      const sm = new THREE.SkinnedMesh(g, p.mat);
      sm.visible = p.mesh.visible;
      sm.frustumCulled = false;          // the posed model leaves the bind pose's bounds
      m.root.remove(p.mesh);
      m.root.add(sm);
      sm.bind(skeleton);
      p.mesh = sm;
    }
    // back to the md6skl bind pose (Skeleton.pose() would give the root bones their world matrix, the view's
    // Z-up turn included, so a figure would lie on its side)
    const rest = () => bones.forEach((b, i) => { b.position.fromArray(S.pos[i]); b.quaternion.fromArray(S.rot[i]); });
    m.rig = { skeleton, rest };
    return m.rig;
  }

  async function playAnim(id) {
    const m = M;
    if (!m) return;
    if (m.action) {
      m.action.stop();
      m.mixer.uncacheClip(m.action.getClip());
      m.action = null;
    }
    m.playing = false;
    anPlay.textContent = 'Play';
    anPlay.disabled = true;
    if (!id) {
      if (m.rig) m.rig.rest();
      anNote.textContent = '';
      dirty = true;
      return;
    }
    anNote.textContent = 'Loading animation …';
    try {
      await rig(m);
      const A = await getJson(api('models', 'anim.json', { id: m.id, anim: id }));
      if (m !== M || anSel.value !== id) return;
      const times = Float32Array.from({ length: A.frames }, (_, f) => f / A.fps), tracks = [];
      for (const t of A.tracks) {
        if (t.rot) tracks.push(new THREE.QuaternionKeyframeTrack('j' + t.joint + '.quaternion', t.rot.length === 4 ? [0] : times, t.rot));
        if (t.pos) tracks.push(new THREE.VectorKeyframeTrack('j' + t.joint + '.position', t.pos.length === 3 ? [0] : times, t.pos));
      }
      m.rig.rest();                      // joints the clip leaves alone keep the bind pose
      m.mixer = m.mixer || new THREE.AnimationMixer(m.root);
      m.action = m.mixer.clipAction(new THREE.AnimationClip(A.name, Math.max(A.frames - 1, 1) / A.fps, tracks));
      m.action.play();
      m.playing = true;
      m.clock = performance.now();
      anPlay.disabled = false;
      anPlay.textContent = 'Pause';
      anNote.textContent = A.name + ' · ' + plural(A.frames, 'frame', 'frames') + ' · ' + A.fps + ' fps';
      dirty = true;
    } catch (e) {
      if (m === M) anNote.textContent = e.message;
    }
  }
  anQ.addEventListener('input', fillAnims);
  anSel.addEventListener('change', () => playAnim(anSel.value));
  anPlay.addEventListener('click', () => {
    if (!M || !M.action) return;
    M.playing = !M.playing;
    M.clock = performance.now();
    anPlay.textContent = M.playing ? 'Pause' : 'Play';
    dirty = true;
  });

  // ---- camera ---------------------------------------------------------------------------------

  // the visible surfaces fill the view; a model long along x (a weapon) is seen from the side, the
  // rest (figures face +x) from the front, a little from above
  function fit() {
    if (!M) return;
    const b = new THREE.Box3();
    world.updateMatrixWorld(true);
    for (const p of M.parts) if (p.mesh.visible) b.expandByObject(p.mesh);
    if (b.isEmpty()) b.setFromObject(M.root);
    if (b.isEmpty()) return;
    const c = b.getCenter(new THREE.Vector3()), size = b.getSize(new THREE.Vector3());
    const r = Math.max(size.length() / 2, 1e-3);
    const ex = size.x, ey = size.z, ez = size.y;           // game extents
    const g = ex > 1.6 * Math.max(ey, ez) ? [-0.35, 1, 0.4] : [1, -0.55, 0.35];
    const dir = new THREE.Vector3(g[0], g[2], -g[1]).normalize();
    const half = THREE.MathUtils.degToRad(camera.fov) / 2, halfX = Math.atan(Math.tan(half) * camera.aspect);
    const d = r / Math.sin(Math.min(half, halfX)) * 1.1;
    camera.near = Math.max(d / 200, 1e-4);
    camera.far = d * 40;
    camera.position.copy(c).addScaledVector(dir, d);
    camera.updateProjectionMatrix();
    controls.target.copy(c);
    controls.minDistance = r * 0.02;
    controls.maxDistance = d * 10;
    controls.update();
    dirty = true;
  }

  // ---- side panel: facts, surfaces, own skin ------------------------------------------------------

  const m2 = (v) => dec(v, v < 10 ? 2 : 1);
  function showFacts() {
    if (!M) return;
    const i = M.info, shown = M.parts.filter((p) => p.mesh.visible);
    const tris = M.parts.reduce((a, p) => a + p.tris, 0), vis = shown.reduce((a, p) => a + p.tris, 0);
    const b = M.box, t = M.tex, variants = M.parts.filter((p) => p.sf.visible === false).length;
    const dl = $('mfacts');
    dl.textContent = '';
    for (const [k, v, cls] of [
      ['Triangles', num(tris) + (vis !== tris ? ' (' + num(vis) + ' visible)' : '')],
      ['Surfaces', num(M.parts.length) + (shown.length !== M.parts.length ? ' (' + num(shown.length) + ' visible)' : '')],
      ['Vertices', num(M.parts.reduce((a, p) => a + p.verts, 0))],
      ['Size', b ? m2(b.max[0] - b.min[0]) + ' × ' + m2(b.max[1] - b.min[1]) + ' × ' + m2(b.max[2] - b.min[2]) + ' m' : null],
      ['Textures', t.total ? num(t.ok) + ' / ' + num(t.total) + (t.failed ? ' · ' + num(t.failed) + ' failed' : '') : 'none', t.failed ? 'bad' : null],
      ['Joints', i.joints ? num(i.joints) + ' · base pose' : 'none (rigid model)'],
      ['LODs', i.lods ? num(i.lods) + ' · showing LOD 0' : null],
      ['Variants', variants ? plural(variants, 'surface', 'surfaces') + ' off at start (md6Def ' + tailOf(i.md6def) + ')' : null],
      ['Folder', i.group],
      ['Archive', i.archive],
    ]) {
      if (v == null || v === '') continue;
      const dd = el('dd', cls, v);
      dd.title = v;
      dl.append(el('dt', null, k), dd);
    }
  }

  const tailOf = (s) => (s || '').split('/').pop();
  function rows() {
    const host = $('msurfs');
    if (!M) { host.textContent = ''; return; }
    if (host.children.length !== M.parts.length) {
      host.textContent = '';
      M.parts.forEach((p, i) => {
        const row = el('label', 'check msurf'), cb = el('input'), tx = el('span', 'tx');
        cb.type = 'checkbox';
        cb.dataset.i = i;
        tx.append(el('span', 'nm'), el('span', 'sd mono'));
        row.append(cb, tx, el('em', 'mono'));
        host.append(row);
      });
    }
    M.parts.forEach((p, i) => {
      const row = host.children[i], [cb, tx, em] = row.children, sz = p.sf.albedo_size;
      cb.checked = p.mesh.visible;
      row.classList.toggle('off', !p.mesh.visible);
      tx.firstChild.textContent = p.sf.name || 'Surface ' + (i + 1);
      const img = p.skin ? 'own skin' : p.tex === 'ohne' ? 'no texture' : p.tex === 'fehler' ? 'texture missing' : sz ? sz[0] + '×' + sz[1] : 'texture';
      tx.lastChild.textContent = tailOf(p.sf.material) + ' · ' + img;
      tx.lastChild.classList.toggle('bad', p.tex === 'fehler');
      em.textContent = num(p.tris);
      row.title = (p.sf.material || '') + (p.err ? '\n' + p.err : '') + '\n' + plural(p.tris, 'triangle', 'triangles') + ', ' + plural(p.verts, 'vertex', 'vertices');
    });
    expUi();
  }

  function show(i, on) {
    const p = M.parts[i], m = M, my = gen;
    p.mesh.visible = on;
    dirty = true;
    if (on && ((p.sf.albedo && !m.texReq.has(p.sf.albedo)) || (p.sf.pbr && !m.pbrReq.has(p.sf.pbr)))) {
      loadTextures(m, my, undefined, [p]).then(() => my === gen && loadPbr(m, my, undefined, [p]));
    }
  }
  $('msurfs').addEventListener('change', (e) => {
    if (!M || !e.target.dataset.i) return;
    show(+e.target.dataset.i, e.target.checked);
    rows();
    showFacts();
  });
  for (const [b, on] of [['mall', () => true], ['mnone', () => false], ['mdefault', (p) => p.sf.visible !== false]]) {
    $(b).addEventListener('click', () => {
      if (!M) return;
      M.parts.forEach((p, i) => show(i, on(p)));
      rows();
      showFacts();
    });
  }
  function setWire(on) {
    wire = on;
    $('mwire').classList.toggle('on', on);
    $('mwire').setAttribute('aria-pressed', String(on));
    if (M) for (const p of M.parts) p.mat.wireframe = on;
    dirty = true;
  }
  $('mwire').addEventListener('click', () => setWire(!wire));
  $('mfit').addEventListener('click', fit);
  canvas.addEventListener('dblclick', fit);

  // own skin: the options are the surfaces; the image goes on every surface of the chosen one's material
  function skinUi() {
    const sel = $('mskinsurf'), key = M ? M.game + ':' + M.id : '';
    if (sel.dataset.for !== key) {         // a new model: its surfaces, the largest textured one chosen (a weapon's body)
      sel.dataset.for = key;
      sel.textContent = '';
      let best = 0;
      if (M) M.parts.forEach((p, i) => {
        sel.append(new Option((p.sf.name || 'Surface ' + (i + 1)) + ' – ' + tailOf(p.sf.material), i));
        const b = M.parts[best];
        if (p.sf.albedo && (!b.sf.albedo || p.tris > b.tris)) best = i;
      });
      if (M) sel.value = String(best);
    }
    sel.disabled = !M;
    $('mskinpick').disabled = !M;
    $('mskinreset').disabled = !(M && M.skin);
    $('mskinmod').disabled = !(M && M.skin && M.game === 'tnc');
    $('mskinnote').textContent = M && M.skin
      ? 'Own skin "' + M.skin.name + '" (' + M.skin.w + '×' + M.skin.h + ') on ' + tailOf(M.skin.material) + '. Preview only, nothing changes in the game.'
      : 'Choose a PNG: it goes straight onto every surface with the chosen surface\'s material. Preview only, nothing changes in the game.';
    expUi();
  }

  $('mskinpick').addEventListener('click', () => $('mskinfile').click());
  $('mskinfile').addEventListener('change', async (e) => {
    const f = e.target.files && e.target.files[0];
    e.target.value = '';                 // the same file again is a new change
    const m = M, p = m && m.parts[+$('mskinsurf').value];
    if (!f || !p) return;
    if (f.size > 64 * 1024 * 1024) {       // the server refuses more (MAX_SKIN) and drops the connection
      toast('"' + f.name + '" is too large (' + Math.round(f.size / 1048576) + ' MB). The skin may be at most 64 MB.', 6000);
      return;
    }
    let im, bytes;
    try {                                  // accept= only filters the dialog: the bytes decide
      bytes = new Blob([await f.arrayBuffer()], { type: 'image/png' });   // the export sends exactly what the preview shows, even if the file changes on disk
      const sig = new Uint8Array(await bytes.slice(0, 8).arrayBuffer());
      if (![0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a].every((v, i) => sig[i] === v)) {
        toast('"' + f.name + '" is not a PNG file. The skin must be a PNG.', 5000);
        return;
      }
      im = await imageOf(bytes);
    } catch (err) {
      toast('"' + f.name + '" is not a readable image.', 5000);
      return;
    }
    if (m !== M) return;
    unskin();
    const t = texture(im), mat = p.sf.material;
    m.skin = { name: f.name, w: im.naturalWidth, h: im.naturalHeight, material: mat, tex: t, file: bytes };
    for (const q of m.parts) if (q.sf.material === mat) { q.skin = t; setMap(q, t); }
    const o = p.sf.albedo_size;
    if (o && o[0] * im.naturalHeight !== o[1] * im.naturalWidth) {
      toast('Aspect ratio differs: your image ' + im.naturalWidth + '×' + im.naturalHeight + ', the original ' + o[0] + '×' + o[1] + '. The UVs will not fit.', 7000);
    } else if (o && o[0] !== im.naturalWidth) {
      toast('Your image ' + im.naturalWidth + '×' + im.naturalHeight + ', the original ' + o[0] + '×' + o[1] + ': the preview scales it.', 5000);
    }
    rows();
    skinUi();
  });
  function unskin() {
    if (!M || !M.skin) return;
    for (const q of M.parts) if (q.skin) { q.skin = null; setMap(q, q.orig); }
    M.skin.tex.dispose();
    M.skin = null;
  }
  $('mskinreset').addEventListener('click', () => { unskin(); rows(); skinUi(); });
  // assetio.js: the same PNG as a mod, on that material's colour map (every weapon or figure)
  $('mskinmod').addEventListener('click', () => {
    if (M && M.skin && window.studioIO) window.studioIO.open({ game: M.game, kind: 'models', id: M.id, skin: M.skin.material, file: M.skin.file, fileName: M.skin.name });
  });

  // ---- export: exactly the switched-on surfaces, the own skin when one of them wears it -----------

  const shownIdx = () => (M ? M.parts.map((p, i) => (p.mesh.visible ? i : -1)).filter((i) => i >= 0) : []);
  function expUi() {
    const on = shownIdx(), all = M ? M.parts.length : 0;
    const skinned = M && M.skin && on.some((i) => M.parts[i].skin);
    expBtn.disabled = !M || !on.length || exporting;
    expFmt.disabled = !M;
    expBtn.textContent = exporting ? 'Exporting …' : 'Export';
    expNote.textContent = !M ? '' : !on.length ? 'No surface switched on: nothing to export.'
      : (on.length === all ? (all === 1 ? 'The surface' : 'All ' + num(all) + ' surfaces') : num(on.length) + ' of ' + num(all) + ' surfaces (as switched on)')
        + ' with colour textures' + (M.skin ? (skinned ? ' and your own skin "' + M.skin.name + '"' : '; your own skin is left out, its surfaces are off') : '')
        + '. Base pose only: no skeleton, weights or animations. The file goes to the browser\'s download folder.';
  }

  expBtn.addEventListener('click', async () => {
    const m = M, on = shownIdx();
    if (!m || exporting || !on.length) return;
    const fmt = expFmt.value, q = { id: m.id, format: fmt, surfaces: on.join(',') };
    const skin = m.skin && on.some((i) => m.parts[i].skin) ? m.skin : null;
    if (skin) q.skin = skin.material;
    exporting = true;
    expUi();
    try {
      const r = await fetchOk(api('models', 'export', q), skin ? { method: 'POST', body: skin.file, headers: { 'Content-Type': 'image/png' } } : undefined);
      const blob = await r.blob();
      const name = (/filename="([^"]+)"/.exec(r.headers.get('Content-Disposition') || '') || [])[1] || 'model' + (fmt === 'glb' ? '.glb' : '.zip');
      const a = el('a');
      a.href = URL.createObjectURL(blob);
      a.download = name;
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 60000);
      lastExport = { name, size: blob.size, format: fmt, surfaces: on, skin: skin ? skin.name : null };
      toast('Exported: ' + name + ' (' + dec(blob.size / 1048576, 1) + ' MB), in the browser\'s download folder.', 5000);
    } catch (e) {
      lastExport = { error: e.message };
      toast('Export failed: ' + e.message, 7000);
    } finally {
      exporting = false;
      expUi();
    }
  });

  function state() {
    const p = M ? M.parts : [];
    return {
      id: M ? M.id : want, loaded: pane.dataset.loaded || '', message, stale, wire, drawn, done: !!(M && M.done),
      surfaces: p.length, visible: p.filter((x) => x.mesh.visible).length,
      tris: p.reduce((a, x) => a + x.tris, 0), shownTris: p.reduce((a, x) => a + (x.mesh.visible ? x.tris : 0), 0),
      textures: M ? M.tex : null, box: M ? M.box : null, info: M ? { bounds: M.info.bounds, surfaces: M.info.surfaces.length } : null,
      parts: p.map((x) => ({ name: x.sf.name, material: x.sf.material, tris: x.tris, verts: x.verts, visible: x.mesh.visible,
        tex: x.tex, map: x.mat.map ? [x.mat.map.image.naturalWidth, x.mat.map.image.naturalHeight] : null, skin: !!x.skin,
        first: Array.from(x.mesh.geometry.index.array.slice(0, 3)), computed: x.computed, wire: x.mat.wireframe,
        pbr: ['normalMap', 'roughnessMap', 'aoMap', 'emissiveMap'].filter((k) => x.mat[k]), alphaTest: x.mat.alphaTest,
        start: x.sf.visible !== false })),
      skin: M && M.skin ? { name: M.skin.name, w: M.skin.w, h: M.skin.h, material: M.skin.material } : null,
      export: lastExport, exportNote: expNote.textContent, exportOk: !expBtn.disabled,
      anims: M && M.anims ? M.anims.length : null, animNote: anNote.textContent,
      anim: M && M.action ? { clip: M.action.getClip().name, playing: !!M.playing, time: M.action.time,
        skinned: p.filter((x) => x.mesh.isSkinnedMesh).length } : null,
      cam: R ? camera.position.toArray() : null, dist: R ? camera.position.distanceTo(controls.target) : null,
    };
  }

  return { open, stop, state, fit };
};
