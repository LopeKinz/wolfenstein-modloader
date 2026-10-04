/* Skripte: the Kiscule viewer and editor of the Studio (Wolfenstein II's level scripts: a node
   graph in decl text inside the 'extkisclule' entries). studio.js owns the list and the page; it
   calls window.StudioScripts(kit) once and then open(id) per script.

   Data contract (kind "scripts", TNC only; TNO answers 422):
     /api/tnc/scripts/info?id=[&mod=folder]  {id, name, map, archive, editable, reason, nodes: [{id, type, short, label,
                                  params: {path: raw decl text}, ports: [{name, dir: in|out, kind: event|variable}],
                                  mission: {role, decl}}], edges: [{id, from: {node, port}, to: {node, port}}],
                                  mod: the Studio mod whose copy this is (null: the game's), mods: [{folder, name}]}
     /api/tnc/scripts/nodetypes  [{type, short, category, description, ports, params: [{path, type, default, min, max, enum,
                                  on a list count X.num: resizable, item, ports, count; on a count parameter: follows}]}]
     POST /api/tnc/scripts/save?id=&name=|folder=[&author=]  {ops: [...]}  -> {message, mod, folder, added, base}
   Param values are raw decl text as the server sends them ("text" with quotes, NULL, true, 0.5):
   an edit keeps the quoting of the value it replaces, a new node starts from the type's defaults.
   A list grows or shrinks at its end (set X.num), new items as the type's `item`, the ports with it
   where the type's `ports` say so. Saved into a mod that has the script already, the edits add to its copy.

   The graph: Canvas2D, a layered layout of our own (cycles broken by DFS, longest path layers,
   sources pulled next to what they feed, barycentre passes, components packed on shelves),
   only what is on screen is drawn and text drops out when zoomed far out.
   Edits are a list of actions over the graph as loaded: undo replays the list without its last
   action, Save sends the list compacted (an added-then-removed node or edge never leaves). */
window.StudioScripts = function (kit) {
  'use strict';

  const { $, el, num, plural, fetchOk, getJson, api, toast, showPane, showErr } = kit;
  const NODE_W = 216, HEAD = 36, ROW = 16, PAD = 7, GAP_X = 96, GAP_Y = 24, SHELF = 140;
  const ROLE = { start: 'Mission start', objective_start: 'Objective start', objective_complete: 'Objective complete',
    set_active: 'Active objective' };
  const C = { node: '#161c23', head: '#1c232c', line: '#323c48', text: '#d9dfe6', dim: '#8793a1', accent: '#f2b233',
    accentSoft: 'rgba(242,178,51,0.16)', bad: '#ff7a70', event: 'rgba(160,174,190,0.62)', variable: 'rgba(84,170,206,0.62)',
    varPort: '#54aace', bg: '#0b0e12' };
  const FONT = '600 12px system-ui, "Segoe UI", Arial, sans-serif';
  const FONT2 = '11px system-ui, "Segoe UI", Arial, sans-serif';
  const FONTP = '10.5px ui-monospace, Consolas, monospace';
  const store = {
    get(k) { try { return localStorage.getItem('studio.io.' + k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem('studio.io.' + k, v); } catch (e) { /* private mode */ } },
  };

  // ---- the pane (index.html only holds the empty #kpane) -------------------------------------

  const pane = $('kpane');
  pane.innerHTML = `
    <div class="kgraph">
      <canvas id="kcanvas" tabindex="0" aria-label="Node graph of the script: drag pans, mouse wheel zooms, click selects"></canvas>
      <div class="kbar">
        <button id="kfit" type="button" title="Fit the graph (double-click on empty space)">Fit</button>
        <button id="kmis" type="button" aria-pressed="false" title="Show only the mission flow: mission and objective nodes and the event paths between them">Missions</button>
        <button id="kadd" type="button" title="Add a node from the catalogue">Add node</button>
        <input id="kfind" type="search" placeholder="Find node …" autocomplete="off" spellcheck="false" aria-label="Find a node by type, label or id">
        <span id="kcount" class="mono dim small"></span>
      </div>
      <p id="kread" class="kread" hidden></p>
      <div id="kpal" class="kpal" hidden role="dialog" aria-label="Add node">
        <div class="kpalhead"><input id="kpalq" type="search" placeholder="Search node types …" autocomplete="off" spellcheck="false" aria-label="Search node types">
          <select id="kpalcat" aria-label="Category"></select></div>
        <div id="kpallist" class="kpallist" role="listbox" aria-label="Node types"></div>
        <p id="kpalnote" class="dim small"></p>
      </div>
      <div id="kwait" class="wait" hidden><i></i><span></span></div>
      <div id="kmsg" class="wmsg" hidden></div>
    </div>
    <div class="tside kside">
      <h1 id="ktitle"></h1>
      <div id="ksub" class="sub mono"></div>
      <div id="kinsp" class="kinsp"></div>
      <div class="kedits">
        <div class="mhead"><h2 class="cap" id="kedcap">Edits</h2>
          <button id="kundo" class="ghost" type="button" title="Undo the last edit (Ctrl+Z)" disabled>Undo</button></div>
        <ol id="kedlist" class="kedlist small"></ol>
        <button id="ksave" class="accent" type="button" disabled title="Save the edits as a mod the loader applies">Save as mod…</button>
      </div>
    </div>`;
  const cv = $('kcanvas'), cx = cv.getContext('2d'), box = cv.parentElement;

  // ---- state ---------------------------------------------------------------------------------

  let S = null;                          // the script on screen
  let types = null, typesGame = null, typesErr = null, typeMap = new Map();
  let gen = 0, sel = null;               // sel: {node} | {edge}
  let view = { s: 1, x: 0, y: 0 }, W = 1, H = 1, dpr = 1, drawn = 0, queued = false;
  let drag = null, hover = null, mouse = null, nextNew = 1;
  const memo = new Map();                // script id -> its actions, kept while the page lives

  const typeOf = (n) => typeMap.get(n.type) || null;
  const isMission = (n) => !!(n.mission && n.mission.role) || /ActionMission$/.test(n.type);
  const unq = (v) => (v.length >= 2 && v[0] === '"' && v[v.length - 1] === '"' ? v.slice(1, -1) : v);
  const generic = (path) => path.replace(/\[\d+\]/g, '[]');

  function build(info, acts) {
    const s = { info, acts, nodes: new Map(), edges: new Map(), pos: { all: new Map(), mis: new Map() }, flow: null,
      readOnly: !info.editable, missions: false };
    for (const n of info.nodes) s.nodes.set(n.id, nodeOf(n));
    for (const e of info.edges) s.edges.set(e.id, Object.assign({}, e));
    return s;
  }
  function nodeOf(n) {
    const ports = n.ports || [];
    const ins = ports.filter((p) => p.dir === 'in'), outs = ports.filter((p) => p.dir === 'out');
    return Object.assign({}, n, { params: Object.assign({}, n.params), ins, outs,
      inAt: new Map(ins.map((p, i) => [p.name, i])), outAt: new Map(outs.map((p, i) => [p.name, i])),
      h: HEAD + Math.max(ins.length, outs.length, 1) * ROW + PAD });
  }
  // the graph as loaded plus every recorded op, in order
  function replay() {
    const s = S, keep = s.pos;
    s.nodes = new Map(s.info.nodes.map((n) => [n.id, nodeOf(n)]));
    s.edges = new Map(s.info.edges.map((e) => [e.id, Object.assign({}, e)]));
    for (const a of s.acts) for (const op of a.ops) applyOp(s, op);
    s.pos = keep;
    s.flow = null;
    if (sel && ((sel.node && !s.nodes.has(sel.node)) || (sel.edge && !s.edges.has(sel.edge)))) sel = null;
    kindEdges();
  }
  function applyOp(s, op) {
    if (op.op === 'set') {
      const n = s.nodes.get(op.node);
      if (n && isList(n, op.path)) resizeList(s, n, op.path, Number(op.value));
      else if (n) n.params[op.path] = op.value;
    } else if (op.op === 'add_node') {
      const t = typeMap.get(op.type);
      s.nodes.set(op.id, nodeOf({ id: op.id, type: op.type, short: t ? t.short : op.type, label: t ? t.short : op.type,
        params: Object.assign({}, t && t.initial, op.params), ports: t ? t.ports : [], mission: { role: null, decl: null }, added: true }));
    } else if (op.op === 'remove_node') s.nodes.delete(op.node);
    else if (op.op === 'add_edge') s.edges.set(op.id, { id: op.id, from: op.from, to: op.to, added: true });
    else if (op.op === 'remove_edge') s.edges.delete(op.edge);
  }

  // lists: X.num resizes list X at its end as the server does (kiscule.py resize): new items start as the
  // type's `item`, and where the type gives `ports` per item they come and go with it, wires included
  const listSpec = (n, path) => { const t = typeOf(n); return t && (t.params || []).find((p) => p.path === generic(path)); };
  function isList(n, path) {
    if (!/\.num$/.test(path)) return false;
    const spec = listSpec(n, path), base = path.slice(0, -4) + '.item[';
    return !!(spec && ('item' in spec || 'ports' in spec || spec.resizable === false)) || Object.keys(n.params).some((k) => k.startsWith(base));
  }
  function itemAt(path, k) {             // the item of list `path` (X.num) that parameter k belongs to, or -1
    const base = path.slice(0, -4) + '.item[';
    const m = k.startsWith(base) ? /^(\d+)\](\.|$)/.exec(k.slice(base.length)) : null;
    return m ? Number(m[1]) : -1;
  }
  function newItem(n, path) {            // the type's `item`, else a copy of the last scalar item; undefined: unknown
    const spec = listSpec(n, path);
    if (spec && 'item' in spec) return spec.item;
    const last = n.params[path.slice(0, -4) + '.item[' + (Number(n.params[path]) - 1) + ']'];
    return last === undefined ? undefined : kindOf(last) === 'str' ? '""' : last;
  }
  function listPorts(n, path) {          // [kept ports, dropped ports] when list `path` goes to m items
    const spec = listSpec(n, path) || {}, per = (spec.ports || []).length, old = Number(n.params[path]) || 0;
    return (m) => {
      const at = n.ports.length - old * per + Math.min(old, m) * per;
      const add = [];
      for (let i = old; i < m; i++) for (const p of spec.ports || []) add.push({ name: p.name.replace('%d', i), dir: p.dir, kind: p.kind });
      return [n.ports.slice(0, at).concat(add), n.ports.slice(at)];
    };
  }
  const onPort = (e, id, p) => (p.dir === 'out' ? e.from.node === id && e.from.port === p.name : e.to.node === id && e.to.port === p.name);
  function resizeList(s, n, path, m) {
    const old = Number(n.params[path]) || 0, spec = listSpec(n, path) || {}, item = newItem(n, path);
    const params = {}, keys = Object.keys(n.params), base = path.slice(0, -4) + '.item[';
    const last = keys.reduce((a, k, i) => (k === path || itemAt(path, k) >= 0 ? i : a), -1);
    keys.forEach((k, i) => {
      if (itemAt(path, k) < m) params[k] = k === path ? String(m) : n.params[k];   // not an item: -1
      if (i === last) {
        for (let j = old; j < m; j++) {
          if (item !== null && typeof item === 'object') for (const sub of Object.keys(item)) params[base + j + '].' + sub] = item[sub];
          else params[base + j + ']'] = item;
        }
      }
    });
    if (spec.count && spec.count in params) params[spec.count] = String(m);
    const [ports, cut] = listPorts(n, path)(m);
    for (const e of [...s.edges.values()]) if (cut.some((p) => onPort(e, n.id, p))) s.edges.delete(e.id);
    const clean = Object.fromEntries(Object.entries(n).filter(([k]) => k[0] !== '_'));   // drop the cached texts
    s.nodes.set(n.id, nodeOf(Object.assign(clean, { params, ports })));
  }
  function kindEdges() {                 // an edge's kind is its out port's; connected ports for the dots
    S.wired = new Set();
    for (const e of S.edges.values()) {
      const a = S.nodes.get(e.from.node), i = a ? a.outAt.get(e.from.port) : undefined;
      e.kind = i != null ? a.outs[i].kind : 'event';
      S.wired.add(e.from.node + '\u0001o\u0001' + e.from.port);
      S.wired.add(e.to.node + '\u0001i\u0001' + e.to.port);
    }
  }
  function record(label, ops) {
    if (!S || S.readOnly || !ops.length) return;
    S.acts.push({ label, ops });
    for (const op of ops) applyOp(S, op);
    S.flow = null;
    kindEdges();
    edits();
    redraw();
  }
  function undo() {
    if (!S || !S.acts.length) return;
    const a = S.acts.pop();
    replay();
    edits();
    inspect();
    redraw();
    toast('Undone: ' + a.label);
  }

  // the ops for the server: added-then-removed nodes and edges vanish, sets fold into their node.
  // A list resize keeps its place (later ops may need the items and ports it makes or drop what it cut),
  // so the sets of a resized node are neither folded into its add_node nor dropped for matching the loaded value.
  function compact(acts) {
    const ops = acts.flatMap((a) => a.ops);
    const resized = new Set(ops.filter((o) => o.op === 'set' && /\.num$/.test(o.path)).map((o) => o.node));
    const added = new Set(ops.filter((o) => o.op === 'add_node').map((o) => o.id));
    const gone = new Set(ops.filter((o) => o.op === 'remove_node' && added.has(o.node)).map((o) => o.node));
    const newEdges = new Map(ops.filter((o) => o.op === 'add_edge').map((o) => [o.id, o]));
    const deadEdges = new Set(ops.filter((o) => o.op === 'remove_edge' && newEdges.has(o.edge)).map((o) => o.edge));
    for (const [id, o] of newEdges) if (gone.has(o.from.node) || gone.has(o.to.node)) deadEdges.add(id);
    const out = [], adds = new Map(), sets = new Map();
    for (const o of ops) {
      if (o.op === 'add_node') { if (gone.has(o.id)) continue; const c = { op: 'add_node', type: o.type, id: o.id, params: Object.assign({}, o.params) }; adds.set(o.id, c); out.push(c); continue; }
      if (o.op === 'set') {
        if (gone.has(o.node)) continue;
        if (/\.num$/.test(o.path)) { out.push({ op: 'set', node: o.node, path: o.path, value: o.value }); continue; }
        if (adds.has(o.node) && !resized.has(o.node)) { adds.get(o.node).params[o.path] = o.value; continue; }
        const k = o.node + '\u0001' + o.path;
        if (sets.has(k)) out.splice(out.indexOf(sets.get(k)), 1);
        const c = { op: 'set', node: o.node, path: o.path, value: o.value };
        sets.set(k, c);
        out.push(c);
        continue;
      }
      if (o.op === 'remove_node') { if (!gone.has(o.node)) out.push({ op: 'remove_node', node: o.node }); continue; }
      if (o.op === 'add_edge') { if (!deadEdges.has(o.id)) out.push({ op: 'add_edge', from: o.from, to: o.to }); continue; }
      if (o.op === 'remove_edge') { if (!deadEdges.has(o.edge)) out.push({ op: 'remove_edge', edge: o.edge }); }
    }
    // a set back to the loaded value is no edit; a set on a node removed later is moot
    const orig = new Map(S.info.nodes.map((n) => [n.id, n.params]));
    const removed = new Set(out.filter((o) => o.op === 'remove_node').map((o) => o.node));
    return out.filter((o) => o.op !== 'set' || !(removed.has(o.node) || (!resized.has(o.node) && (orig.get(o.node) || {})[o.path] === o.value)));
  }

  // ---- layout --------------------------------------------------------------------------------

  // positions for `ids` into `pos`: components laid out on their own, then packed on shelves
  function layout(ids, pos) {
    const set = new Set(ids), succ = new Map(), pred = new Map(), und = new Map();
    for (const id of ids) { succ.set(id, []); pred.set(id, []); und.set(id, []); }
    for (const e of S.edges.values()) {
      const a = e.from.node, b = e.to.node;
      if (a === b || !set.has(a) || !set.has(b)) continue;
      succ.get(a).push(b);
      pred.get(b).push(a);
      und.get(a).push(b);
      und.get(b).push(a);
    }
    const seen = new Set(), comps = [];
    for (const id of ids) {
      if (seen.has(id)) continue;
      const comp = [id];
      seen.add(id);
      for (let k = 0; k < comp.length; k++) for (const w of und.get(comp[k])) if (!seen.has(w)) { seen.add(w); comp.push(w); }
      comps.push(comp);
    }
    const boxes = comps.map((c) => layoutComp(c, succ, pred, pos));
    // shelves: biggest first, rows no wider than the widest component or the square of the area
    const order = boxes.map((b, i) => i).sort((p, q) => comps[q].length - comps[p].length || boxes[q].h - boxes[p].h);
    const area = boxes.reduce((a, b) => a + (b.w + SHELF) * (b.h + SHELF), 0);
    const maxW = Math.max(Math.sqrt(area) * 1.5, ...boxes.map((b) => b.w));
    let x = 0, y = 0, rowH = 0;
    for (const i of order) {
      const b = boxes[i];
      if (x > 0 && x + b.w > maxW) { x = 0; y += rowH + SHELF; rowH = 0; }
      for (const id of comps[i]) { const p = pos.get(id); p.x += x; p.y += y; }
      x += b.w + SHELF;
      rowH = Math.max(rowH, b.h);
    }
  }

  function layoutComp(ids, succ, pred, pos) {
    // DFS from the sources: back edges (cycles) leave the layering, the rest is a DAG
    const st = new Map(), post = [], dsucc = new Map(ids.map((id) => [id, []]));
    const roots = ids.filter((id) => !pred.get(id).length).concat(ids);
    for (const r of roots) {
      if (st.get(r)) continue;
      st.set(r, 1);
      const stack = [[r, 0]];
      while (stack.length) {
        const top = stack[stack.length - 1], ss = succ.get(top[0]);
        if (top[1] < ss.length) {
          const w = ss[top[1]++], sw = st.get(w) || 0;
          if (sw === 0) { st.set(w, 1); dsucc.get(top[0]).push(w); stack.push([w, 0]); }
          else if (sw === 2) dsucc.get(top[0]).push(w);
        } else { st.set(top[0], 2); post.push(top[0]); stack.pop(); }
      }
    }
    const topo = post.slice().reverse(), dpred = new Map(ids.map((id) => [id, []]));
    for (const v of ids) for (const w of dsucc.get(v)) dpred.get(w).push(v);
    const L = new Map(ids.map((id) => [id, 0]));
    for (const v of topo) for (const w of dsucc.get(v)) L.set(w, Math.max(L.get(w), L.get(v) + 1));
    for (const v of post) {               // sinks first: a source moves up to just before what it feeds
      const ss = dsucc.get(v);
      if (!dpred.get(v).length && ss.length) L.set(v, Math.min(...ss.map((w) => L.get(w))) - 1);
    }
    const lo = Math.min(...L.values());
    const layers = [];
    for (const v of topo) {
      const l = L.get(v) - lo;
      (layers[l] = layers[l] || []).push(v);
    }
    const node = (id) => S.nodes.get(id);
    const y = new Map(), cy = (id) => y.get(id) + node(id).h / 2;
    for (const layer of layers) {
      if (!layer) continue;
      let t = 0;
      for (const v of layer) { y.set(v, t); t += node(v).h + GAP_Y; }
    }
    // barycentre passes: down by predecessors, up by successors, then both
    const pass = (seq, nb) => {
      for (const layer of seq) {
        if (!layer) continue;
        const want = new Map();
        for (const v of layer) {
          const ns = nb(v);
          want.set(v, ns.length ? ns.reduce((a, w) => a + cy(w), 0) / ns.length : cy(v));
        }
        layer.sort((p, q) => want.get(p) - want.get(q));
        let t = -Infinity;
        for (const v of layer) {
          const top = Math.max(t, want.get(v) - node(v).h / 2);
          y.set(v, top);
          t = top + node(v).h + GAP_Y;
        }
      }
    };
    const both = (v) => dpred.get(v).concat(dsucc.get(v));
    for (let k = 0; k < 3; k++) {
      pass(layers, (v) => dpred.get(v));
      pass(layers.slice().reverse(), (v) => dsucc.get(v));
    }
    pass(layers, both);
    const top = Math.min(...y.values());
    let w = 0, h = 0;
    layers.forEach((layer, l) => {
      if (!layer) return;
      for (const v of layer) {
        const p = { x: l * (NODE_W + GAP_X), y: y.get(v) - top };
        pos.set(v, p);
        w = Math.max(w, p.x + NODE_W);
        h = Math.max(h, p.y + node(v).h);
      }
    });
    return { w, h };
  }

  // ---- what is shown: everything, or the mission flow ----------------------------------------

  function flow() {
    if (S.flow) return S.flow;
    const M = [...S.nodes.values()].filter(isMission).map((n) => n.id);
    const fw = new Map(), bw = new Map();
    for (const e of S.edges.values()) {
      if (e.kind !== 'event') continue;
      (fw.get(e.from.node) || fw.set(e.from.node, []).get(e.from.node)).push(e.to.node);
      (bw.get(e.to.node) || bw.set(e.to.node, []).get(e.to.node)).push(e.from.node);
    }
    const reach = (adj) => {
      const out = new Set(M);
      const q = M.slice();
      while (q.length) for (const w of adj.get(q.pop()) || []) if (!out.has(w)) { out.add(w); q.push(w); }
      return out;
    };
    const f = reach(fw), b = reach(bw), keep = new Set(M);
    for (const id of f) if (b.has(id)) keep.add(id);
    const mset = new Set(M);
    for (const e of S.edges.values()) {   // one step around every mission node: what starts it, what it starts, its inputs
      if (mset.has(e.to.node)) keep.add(e.from.node);
      if (mset.has(e.from.node) && e.kind === 'event') keep.add(e.to.node);
    }
    for (const n of S.nodes.values()) if (n.added) keep.add(n.id);
    S.flow = { ids: [...S.nodes.keys()].filter((id) => keep.has(id)), set: keep, missions: M.length };
    return S.flow;
  }
  const shown = () => (S.missions ? flow().set : null);
  const posMap = () => (S.missions ? S.pos.mis : S.pos.all);
  function visible(id) { const f = shown(); return !f || f.has(id); }

  // nodes that have no place yet (added, or newly in the flow) go in a column right of the rest
  function place() {
    const pos = posMap(), ids = S.missions ? flow().ids : [...S.nodes.keys()];
    if (!pos.size) { layout(ids, pos); return true; }
    const miss = ids.filter((id) => !pos.has(id));
    if (!miss.length) return false;
    let r = -Infinity, t = Infinity;
    for (const [id, p] of pos) if (S.nodes.has(id)) { r = Math.max(r, p.x + NODE_W); t = Math.min(t, p.y); }
    let y = isFinite(t) ? t : 0;
    for (const id of miss) { pos.set(id, { x: isFinite(r) ? r + GAP_X : 0, y }); y += S.nodes.get(id).h + GAP_Y; }
    return false;
  }

  // ---- drawing -------------------------------------------------------------------------------

  function redraw() { if (!queued) { queued = true; requestAnimationFrame(draw); } }

  function resize() {
    dpr = Math.min(2, window.devicePixelRatio || 1);
    W = Math.max(1, box.clientWidth);
    H = Math.max(1, box.clientHeight);
    cv.width = Math.round(W * dpr);
    cv.height = Math.round(H * dpr);
    redraw();
  }
  new ResizeObserver(resize).observe(box);

  function portY(n, i) { return HEAD + i * ROW + ROW / 2; }
  function outXY(n, port) {
    const p = posMap().get(n.id), i = n.outAt.get(port);
    return i == null ? [p.x + NODE_W, p.y + HEAD / 2] : [p.x + NODE_W, p.y + portY(n, i)];
  }
  function inXY(n, port) {
    const p = posMap().get(n.id), i = n.inAt.get(port);
    return i == null ? [p.x, p.y + HEAD / 2] : [p.x, p.y + portY(n, i)];
  }
  function curve(path, a, b) {
    const d = Math.max(36, Math.abs(b[0] - a[0]) / 2);
    path.moveTo(a[0], a[1]);
    path.bezierCurveTo(a[0] + d, a[1], b[0] - d, b[1], b[0], b[1]);
  }
  function fit(text, max) {             // the longest head of text that fits (current font), with an ellipsis
    if (cx.measureText(text).width <= max) return text;
    let lo = 0, hi = text.length;
    while (lo < hi) { const m = (lo + hi + 1) >> 1; if (cx.measureText(text.slice(0, m) + '…').width <= max) lo = m; else hi = m - 1; }
    return text.slice(0, lo) + '…';
  }

  function draw() {
    queued = false;
    cx.setTransform(1, 0, 0, 1, 0, 0);
    cx.clearRect(0, 0, cv.width, cv.height);
    drawn = 0;
    if (!S || !S.nodes.size) return;
    const pos = posMap(), s = view.s;
    cx.setTransform(s * dpr, 0, 0, s * dpr, view.x * dpr, view.y * dpr);
    const x0 = -view.x / s, y0 = -view.y / s, x1 = x0 + W / s, y1 = y0 + H / s;
    const selN = sel && sel.node, selE = sel && sel.edge;
    const hiNode = selN || (hover && hover.node);
    // edges in three strokes: event, variable, highlighted
    const pe = new Path2D(), pv = new Path2D(), ph = new Path2D();
    for (const e of S.edges.values()) {
      const a = S.nodes.get(e.from.node), b = S.nodes.get(e.to.node);
      if (!a || !b || !visible(a.id) || !visible(b.id) || !pos.has(a.id) || !pos.has(b.id)) continue;
      const p = outXY(a, e.from.port), q = inXY(b, e.to.port), d = Math.max(36, Math.abs(q[0] - p[0]) / 2);
      if (Math.max(p[0], q[0]) + d < x0 || Math.min(p[0], q[0]) - d > x1 || Math.max(p[1], q[1]) < y0 || Math.min(p[1], q[1]) > y1) continue;
      curve(e.id === selE || a.id === hiNode || b.id === hiNode ? ph : e.kind === 'variable' ? pv : pe, p, q);
    }
    cx.lineWidth = Math.max(1.2, 1 / s);
    cx.strokeStyle = C.variable;
    cx.setLineDash([6, 4]);
    cx.stroke(pv);
    cx.setLineDash([]);
    cx.strokeStyle = C.event;
    cx.stroke(pe);
    cx.lineWidth = Math.max(2.4, 2 / s);
    cx.strokeStyle = C.accent;
    cx.stroke(ph);
    const text = s >= 0.2, ports = s >= 0.42;
    for (const n of S.nodes.values()) {
      const p = pos.get(n.id);
      if (!p || !visible(n.id) || p.x > x1 || p.x + NODE_W < x0 || p.y > y1 || p.y + n.h < y0) continue;
      drawn++;
      const mis = isMission(n), on = n.id === selN, found = finder.hits.has(n.id);
      cx.fillStyle = C.node;
      cx.fillRect(p.x, p.y, NODE_W, n.h);
      cx.fillStyle = mis ? C.accentSoft : C.head;
      cx.fillRect(p.x, p.y, NODE_W, HEAD);
      cx.lineWidth = on ? Math.max(2.5, 2 / s) : Math.max(1, 1 / s);
      cx.strokeStyle = on ? C.accent : mis ? 'rgba(242,178,51,0.75)' : found ? '#e8e0c8' : C.line;
      cx.strokeRect(p.x, p.y, NODE_W, n.h);
      if (n.added) { cx.fillStyle = C.accent; cx.fillRect(p.x, p.y, 4, HEAD); }
      if (!text) continue;
      cx.textBaseline = 'middle';
      cx.font = FONT;
      cx.fillStyle = mis ? C.accent : C.text;
      cx.fillText(n._t1 || (n._t1 = fit(n.short || n.type, NODE_W - 16)), p.x + 8, p.y + 12);
      const sub = mis && n.mission && n.mission.role ? ROLE[n.mission.role] + (n.mission.decl ? ' · ' + n.mission.decl : '')
        : n.label && n.label !== n.short ? n.label : '';
      if (sub) {
        cx.font = FONT2;
        cx.fillStyle = mis ? '#e9c77c' : C.dim;
        if (n._s2 !== sub) { n._s2 = sub; n._t2 = fit(sub, NODE_W - 16); }
        cx.fillText(n._t2, p.x + 8, p.y + 27);
      }
      if (!ports) continue;
      cx.font = FONTP;
      for (const [list, out] of [[n.ins, false], [n.outs, true]]) {
        list.forEach((pt, i) => {
          const yy = p.y + portY(n, i), xx = out ? p.x + NODE_W : p.x;
          const wired = S.wired.has(n.id + (out ? '\u0001o\u0001' : '\u0001i\u0001') + pt.name);
          const hot = hover && hover.port && hover.node === n.id && hover.port.name === pt.name && hover.dir === (out ? 'out' : 'in');
          cx.fillStyle = cx.strokeStyle = hot ? C.accent : pt.kind === 'variable' ? C.varPort : C.text;
          cx.lineWidth = Math.max(1, 1 / s);
          cx.beginPath();
          if (pt.kind === 'variable') cx.arc(xx, yy, 3.6, 0, 6.3);
          else { cx.moveTo(xx - 3.5, yy - 4); cx.lineTo(xx + 3.5, yy); cx.lineTo(xx - 3.5, yy + 4); cx.closePath(); }
          if (wired || hot) cx.fill(); else { cx.fillStyle = C.node; cx.fill(); cx.stroke(); }
          cx.fillStyle = wired ? '#c3cbd4' : C.dim;
          cx.textAlign = out ? 'right' : 'left';
          const k = out ? '_o' + i : '_i' + i;
          cx.fillText(n[k] || (n[k] = fit(pt.name, NODE_W / 2 - 14)), out ? xx - 8 : xx + 8, yy);
          cx.textAlign = 'left';
        });
      }
    }
    if (drag && drag.wire && mouse) {     // the wire being drawn
      const a = S.nodes.get(drag.wire.node), p = outXY(a, drag.wire.port);
      const t = hover && hover.port && hover.dir === 'in' ? inXY(S.nodes.get(hover.node), hover.port.name) : [mouse.wx, mouse.wy];
      const pw = new Path2D();
      curve(pw, p, t);
      cx.lineWidth = Math.max(2.4, 2 / s);
      cx.strokeStyle = hover && hover.port && hover.dir === 'in' ? (wireError(drag.wire, hover) ? C.bad : C.accent) : C.accent;
      cx.stroke(pw);
    }
  }

  // ---- view: fit, pan, zoom, find ------------------------------------------------------------

  function bounds(ids) {
    const pos = posMap();
    let a = Infinity, b = Infinity, c = -Infinity, d = -Infinity;
    for (const id of ids) {
      const p = pos.get(id), n = S.nodes.get(id);
      if (!p || !n || !visible(id)) continue;
      a = Math.min(a, p.x); b = Math.min(b, p.y); c = Math.max(c, p.x + NODE_W); d = Math.max(d, p.y + n.h);
    }
    return isFinite(a) ? { x: a, y: b, w: c - a, h: d - b } : null;
  }
  function fitView(ids, most) {
    const r = bounds(ids || [...S.nodes.keys()]);
    if (!r) return;
    let s = Math.min(most || 1.1, Math.max(0.03, Math.min((W - 60) / r.w, (H - 110) / r.h)));
    const small = !ids && (S.missions ? flow().ids.length : S.nodes.size) <= 40;
    if (small && s < 0.5) s = 0.5;        // a small graph stays readable: it starts at the left edge instead of shrinking to a strip
    view = { s, x: r.w * s > W - 60 ? 30 - r.x * s : (W - r.w * s) / 2 - r.x * s, y: 50 + (H - 50 - r.h * s) / 2 - r.y * s };
    redraw();
  }
  function centre(id) {
    const p = posMap().get(id), n = S.nodes.get(id);
    if (!p) return;
    const s = Math.max(view.s, 0.75);
    view = { s, x: W / 2 - (p.x + NODE_W / 2) * s, y: H / 2 - (p.y + n.h / 2) * s };
    redraw();
  }
  function zoomAt(f, sx, sy) {
    const s = Math.min(2.5, Math.max(0.03, view.s * f));
    view.x = sx - (sx - view.x) * s / view.s;
    view.y = sy - (sy - view.y) * s / view.s;
    view.s = s;
    redraw();
  }

  const finder = { hits: new Set(), list: [], at: -1 };
  $('kfind').addEventListener('input', () => {
    const q = $('kfind').value.trim().toLowerCase();
    finder.list = !S || !q ? [] : [...S.nodes.values()].filter((n) => visible(n.id)
      && (n.id + ' ' + n.type + ' ' + (n.label || '') + ' ' + ((n.mission && n.mission.decl) || '')).toLowerCase().includes(q)).map((n) => n.id);
    finder.hits = new Set(finder.list);
    finder.at = -1;
    $('kcount').textContent = q ? plural(finder.list.length, 'match', 'matches') + (finder.list.length ? ' · Enter jumps' : '') : counts();
    redraw();
  });
  $('kfind').addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || !finder.list.length) return;
    e.preventDefault();
    finder.at = (finder.at + (e.shiftKey ? -1 : 1) + finder.list.length) % finder.list.length;
    select({ node: finder.list[finder.at] }, true);
  });

  // ---- pointer -------------------------------------------------------------------------------

  function toWorld(e) {
    const r = cv.getBoundingClientRect(), sx = e.clientX - r.left, sy = e.clientY - r.top;
    return { sx, sy, wx: (sx - view.x) / view.s, wy: (sy - view.y) / view.s };
  }
  function hitTest(wx, wy) {
    const pos = posMap(), m = 7 / Math.min(1, view.s);
    let found = null;
    for (const n of S.nodes.values()) {   // the last drawn is on top
      const p = pos.get(n.id);
      if (!p || !visible(n.id) || wx < p.x - m || wx > p.x + NODE_W + m || wy < p.y || wy > p.y + n.h) continue;
      found = { node: n.id };
      const i = Math.floor((wy - p.y - HEAD) / ROW);
      if (view.s >= 0.42 && i >= 0) {
        if (Math.abs(wx - p.x) <= m && i < n.ins.length) Object.assign(found, { port: n.ins[i], dir: 'in' });
        else if (Math.abs(wx - p.x - NODE_W) <= m && i < n.outs.length) Object.assign(found, { port: n.outs[i], dir: 'out' });
      }
      if (!found.port && (wx < p.x || wx > p.x + NODE_W)) found = null;
    }
    if (found) return found;
    const tol = 6 / view.s;               // an edge: sampled along its curve
    let best = null, bd = tol;
    for (const e of S.edges.values()) {
      const a = S.nodes.get(e.from.node), b = S.nodes.get(e.to.node);
      if (!a || !b || !visible(a.id) || !visible(b.id) || !pos.has(a.id) || !pos.has(b.id)) continue;
      const p = outXY(a, e.from.port), q = inXY(b, e.to.port), d = Math.max(36, Math.abs(q[0] - p[0]) / 2);
      if (wx < Math.min(p[0], q[0]) - tol || wx > Math.max(p[0], q[0]) + tol || wy < Math.min(p[1], q[1]) - tol || wy > Math.max(p[1], q[1]) + tol) continue;
      for (let k = 0; k <= 24; k++) {
        const t = k / 24, u = 1 - t;
        const x = u * u * u * p[0] + 3 * u * u * t * (p[0] + d) + 3 * u * t * t * (q[0] - d) + t * t * t * q[0];
        const y = u * u * u * p[1] + 3 * u * u * t * p[1] + 3 * u * t * t * q[1] + t * t * t * q[1];
        const dd = Math.hypot(x - wx, y - wy);
        if (dd < bd) { bd = dd; best = e.id; }
      }
    }
    return best ? { edge: best } : null;
  }

  cv.addEventListener('contextmenu', (e) => e.preventDefault());
  cv.addEventListener('pointerdown', (e) => {
    if (!S) return;
    cv.focus({ preventScroll: true });
    closePalette();
    const w = toWorld(e), h = e.button === 0 ? hitTest(w.wx, w.wy) : null;
    cv.setPointerCapture(e.pointerId);
    mouse = w;
    if (h && h.port && h.dir === 'out' && !S.readOnly) { drag = { wire: { node: h.node, port: h.port.name, kind: h.port.kind } }; select({ node: h.node }); return; }
    if (h && h.node) {
      const p = posMap().get(h.node);
      drag = { node: h.node, dx: w.wx - p.x, dy: w.wy - p.y, sx: w.sx, sy: w.sy, moved: false };
      select({ node: h.node });
      return;
    }
    if (h && h.edge) { select({ edge: h.edge }); return; }
    drag = { pan: true, x: view.x - w.sx, y: view.y - w.sy, sx: w.sx, sy: w.sy, moved: false };
  });
  cv.addEventListener('pointermove', (e) => {
    if (!S) return;
    const w = toWorld(e);
    mouse = w;
    if (drag && drag.pan) {
      drag.moved = drag.moved || Math.hypot(w.sx - drag.sx, w.sy - drag.sy) > 3;
      view.x = drag.x + w.sx;
      view.y = drag.y + w.sy;
      redraw();
      return;
    }
    if (drag && drag.node) {
      if (!drag.moved && Math.hypot(w.sx - drag.sx, w.sy - drag.sy) < 4) return;
      drag.moved = true;
      const p = posMap().get(drag.node);
      p.x = w.wx - drag.dx;
      p.y = w.wy - drag.dy;
      redraw();
      return;
    }
    const h = hitTest(w.wx, w.wy);
    const key = h ? (h.node || '') + '|' + (h.port ? h.dir + h.port.name : '') + '|' + (h.edge || '') : '';
    if (key !== (hover ? hover.key : '')) { hover = h ? Object.assign(h, { key }) : null; redraw(); }
    cv.style.cursor = drag && drag.wire ? 'crosshair' : h && h.port && (h.dir === 'out' || (drag && drag.wire)) && !S.readOnly ? 'crosshair'
      : h ? 'pointer' : 'grab';
    if (drag && drag.wire) redraw();
  });
  cv.addEventListener('pointerup', () => {
    if (!S || !drag) return;
    const d = drag;
    drag = null;
    if (d.pan && !d.moved) select(null);
    if (d.wire) {
      if (hover && hover.port && hover.dir === 'in') connect(d.wire, hover);
      else if (hover && hover.node && hover.node !== d.wire.node) toast('Drop the wire on an input port (left side of a node).');
      redraw();
    }
  });
  cv.addEventListener('wheel', (e) => {
    if (!S) return;
    e.preventDefault();
    const w = toWorld(e);
    zoomAt(Math.exp(-e.deltaY * 0.0015), w.sx, w.sy);
  }, { passive: false });
  cv.addEventListener('dblclick', (e) => { if (S && !hitTest(toWorld(e).wx, toWorld(e).wy)) fitView(); });
  cv.addEventListener('keydown', (e) => {
    const d = { ArrowLeft: [60, 0], ArrowRight: [-60, 0], ArrowUp: [0, 60], ArrowDown: [0, -60] }[e.key];
    if (d) { e.preventDefault(); e.stopPropagation(); view.x += d[0]; view.y += d[1]; redraw(); return; }
    if (e.key === '+' || e.key === '=') { zoomAt(1.25, W / 2, H / 2); e.stopPropagation(); }
    if (e.key === '-') { zoomAt(0.8, W / 2, H / 2); e.stopPropagation(); }
    if ((e.key === 'f' || e.key === 'F') && !e.ctrlKey) { fitView(); e.stopPropagation(); }
  });
  pane.addEventListener('keydown', (e) => {
    if (!S) return;
    const typing = e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' || e.target.tagName === 'TEXTAREA');
    if ((e.ctrlKey || e.metaKey) && (e.key === 'z' || e.key === 'Z') && !typing) { e.preventDefault(); e.stopPropagation(); undo(); return; }
    if (e.key === 'Escape') {
      if (!$('kpal').hidden) { closePalette(); cv.focus(); }
      else if (drag && drag.wire) { drag = null; redraw(); }
      else if (!typing) select(null);
      e.stopPropagation();
      return;
    }
    if ((e.key === 'Delete' || e.key === 'Backspace') && !typing && sel) { e.preventDefault(); e.stopPropagation(); removeSel(); }
  });

  // ---- edits ---------------------------------------------------------------------------------

  function wireError(w, h) {
    const b = S.nodes.get(h.node);
    if (h.node === w.node) return 'A node cannot be wired to itself.';
    const an = (k) => (/^[aeiou]/.test(k) ? 'an ' : 'a ') + k;
    if (h.port.kind !== w.kind) return 'Wire ' + an(w.kind) + ' output to ' + an(w.kind) + ' input (this input is ' + h.port.kind + ').';
    for (const e of S.edges.values()) {
      if (e.from.node === w.node && e.from.port === w.port && e.to.node === h.node && e.to.port === h.port.name) return 'These ports are wired already.';
    }
    return b ? null : 'Unknown node.';
  }
  function connect(w, h) {
    const err = wireError(w, h);
    if (err) { toast(err, 5000); return; }
    const a = S.nodes.get(w.node), b = S.nodes.get(h.node), id = 'new-edge-' + nextNew++;
    record('Wire ' + a.short + '.' + w.port + ' → ' + b.short + '.' + h.port.name,
      [{ op: 'add_edge', id, from: { node: w.node, port: w.port }, to: { node: h.node, port: h.port.name } }]);
    select({ edge: id });
  }
  function removeSel() {
    if (!S || !sel) return;
    if (S.readOnly) { toast('Read-only: ' + (S.info.reason || 'this script cannot be changed.')); return; }
    if (sel.edge) {
      const e = S.edges.get(sel.edge);
      if (!e) return;
      record('Remove wire ' + edgeName(e), [{ op: 'remove_edge', edge: e.id }]);
    } else {
      const n = S.nodes.get(sel.node);
      if (!n) return;
      const ops = [...S.edges.values()].filter((e) => e.from.node === n.id || e.to.node === n.id).map((e) => ({ op: 'remove_edge', edge: e.id }));
      ops.push({ op: 'remove_node', node: n.id });
      record('Remove ' + n.short + (ops.length > 1 ? ' and ' + plural(ops.length - 1, 'wire', 'wires') : ''), ops);
    }
    select(null);
  }
  function edgeName(e) {
    const a = S.nodes.get(e.from.node), b = S.nodes.get(e.to.node);
    return (a ? a.short : e.from.node) + '.' + e.from.port + ' → ' + (b ? b.short : e.to.node) + '.' + e.to.port;
  }
  function newId() {                     // a fresh id in the style of the game's (31-bit numbers)
    for (;;) {
      const id = String(100000000 + Math.floor(Math.random() * 2000000000));
      if (!S.nodes.has(id) && !S.info.nodes.some((n) => n.id === id)) return id;
    }
  }
  function addNode(t) {
    if (!S || S.readOnly) return;
    const id = newId(), c = { x: (W / 2 - view.x) / view.s - NODE_W / 2, y: (H / 2 - view.y) / view.s - 40 };
    const k = S.acts.filter((a) => a.ops[0].op === 'add_node').length % 6;
    posMap().set(id, { x: c.x + k * 24, y: c.y + k * 24 });   // the other view places it when it is shown
    record('Add ' + t.short, [{ op: 'add_node', type: t.type, id, params: {} }]);   // starts as t.initial
    select({ node: id });
  }

  function edits() {
    const list = $('kedlist');
    list.textContent = '';
    const n = S ? S.acts.length : 0;
    $('kedcap').textContent = n ? 'Edits (' + n + ')' : 'Edits';
    $('kundo').disabled = !n || S.readOnly;
    $('ksave').disabled = !S || S.readOnly || !compact(S.acts).length;
    if (!S) return;
    if (!n) list.append(el('li', 'dim', S.readOnly ? 'Read-only: nothing can be changed here.' : 'No edits yet. Change a parameter, wire ports or add a node.'));
    S.acts.forEach((a) => list.append(el('li', null, a.label)));
    list.scrollTop = list.scrollHeight;
    $('kcount').textContent = counts();
  }
  function counts() {
    if (!S) return '';
    const f = S.missions ? flow() : null;
    return f ? plural(f.ids.length, 'node', 'nodes') + ' of ' + num(S.nodes.size) + ' in the mission flow'
      : plural(S.nodes.size, 'node', 'nodes') + ' · ' + plural(S.edges.size, 'wire', 'wires');
  }

  // ---- inspector -----------------------------------------------------------------------------

  function select(s, jump) {
    sel = s;
    inspect();
    if (jump && s && s.node) centre(s.node);
    redraw();
  }

  function facts(rows) {
    const dl = el('dl', 'facts wide');
    for (const [k, v] of rows) {
      if (v == null || v === '') continue;
      const b = el('div'), dd = el('dd', null, v);
      dd.title = v;
      b.append(el('dt', null, k), dd);
      dl.append(b);
    }
    return dl;
  }

  function inspect() {
    const box = $('kinsp');
    box.textContent = '';
    if (!S) return;
    const info = S.info;
    if (!sel) {
      $('ktitle').textContent = info.name;
      $('ksub').textContent = info.id + (info.mod ? ' · as in mod ' + info.mod : '');
      const missions = [...S.nodes.values()].filter(isMission).length;
      box.append(facts([['Map', info.map], ['Archive', info.archive], ['Nodes', num(S.nodes.size)], ['Wires', num(S.edges.size)],
        ['Mission nodes', missions ? num(missions) : 'none'], ['Editable', info.editable ? 'yes' : 'no']]));
      if (!info.editable) box.append(el('p', 'note', 'Read-only: ' + (info.reason || 'this script cannot be changed.')));
      if (info.mods && info.mods.length) {     // the game's copy, or the copy one of the Studio mods has
        const row = el('label', 'kparam'), pick = el('select');
        pick.id = 'kver';
        pick.append(new Option('Game version', ''));
        for (const m of info.mods) pick.append(new Option('As in mod ' + m.name + ' (' + m.folder + ')', m.folder));
        pick.value = info.mod || '';
        pick.addEventListener('change', () => open(S.game, info.id, pick.value || null));
        row.append(el('span', 'kpn', 'Version'), pick);
        box.append(row);
      }
      if (info.mod) box.append(el('p', 'note', 'This is the copy in your Studio mod ' + info.mod + ', with the edits saved there. Saving adds new edits to it.'));
      const tips = el('ul', 'repnotes small');
      for (const t of ['Click a node to see and edit its parameters, a wire to remove it.',
        'Drag from an output port (right) to an input port (left) to wire. ▸ event, ● variable (dashed).',
        'Missions shows only the mission flow, mission nodes are amber.',
        'Delete removes the selection, Ctrl+Z undoes, F fits, the mouse wheel zooms.']) tips.append(el('li', null, t));
      box.append(tips);
      return;
    }
    if (sel.edge) {
      const e = S.edges.get(sel.edge);
      if (!e) return;
      $('ktitle').textContent = 'Wire';
      $('ksub').textContent = e.added ? 'added in this session' : 'id ' + e.id;
      box.append(facts([['From', edgeName(e).split(' → ')[0]], ['To', edgeName(e).split(' → ')[1]], ['Kind', e.kind]]));
      const row = el('div', 'row');
      row.append(jumpBtn('Go to source', e.from.node), jumpBtn('Go to target', e.to.node));
      box.append(row);
      const del = el('button', null, 'Remove wire');
      del.type = 'button';
      del.disabled = S.readOnly;
      del.addEventListener('click', removeSel);
      box.append(del);
      return;
    }
    const n = S.nodes.get(sel.node);
    if (!n) return;
    const t = typeOf(n);
    $('ktitle').textContent = n.short || n.type;
    $('ksub').textContent = n.type;
    const chips = el('div', 'chips');
    if (t && t.category) chips.append(el('span', 'chip', t.category));
    if (isMission(n)) chips.append(el('span', 'chip kmis', n.mission && n.mission.role ? ROLE[n.mission.role] || n.mission.role : 'Mission node'));
    if (n.added) chips.append(el('span', 'chip', 'added'));
    if (chips.firstChild) box.append(chips);
    if (t && t.description) box.append(el('p', 'kdesc', t.description));
    else if (!types) box.append(el('p', 'dim small', typesErr ? 'No node reference: ' + typesErr : 'Loading the node reference …'));
    box.append(facts([['Label', n.label !== n.short ? n.label : null], ['Mission', n.mission && n.mission.decl], ['Node id', n.id]]));
    // parameters
    const paths = Object.keys(n.params);
    box.append(el('h2', 'cap', 'Parameters' + (paths.length ? ' (' + paths.length + ')' : '')));
    if (!paths.length) box.append(el('p', 'dim small', 'This node has no parameters.'));
    const form = el('div', 'kparams');
    for (const path of paths) form.append(paramRow(n, path, t));
    box.append(form);
    // ports
    box.append(el('h2', 'cap', 'Ports'));
    const pl = el('div', 'kports');
    for (const [list, dir] of [[n.ins, 'in'], [n.outs, 'out']]) {
      for (const p of list) {
        const doc = t && (t.ports || []).find((q) => q.name === p.name && q.dir === p.dir);
        const r = el('div', 'kport');
        const head = el('div', 'kph');
        head.append(el('span', 'kpk ' + p.kind, dir === 'in' ? '◂ in' : 'out ▸'), el('b', null, p.name), el('span', 'dim', p.kind));
        r.append(head);
        const d = doc && (doc.description || doc.doc);
        if (d) r.append(el('p', 'dim', d));
        const wires = [...S.edges.values()].filter((e) => (dir === 'in' ? e.to.node === n.id && e.to.port === p.name : e.from.node === n.id && e.from.port === p.name));
        for (const e of wires) {
          const other = dir === 'in' ? e.from : e.to, o = S.nodes.get(other.node);
          const b = el('button', 'klink', (dir === 'in' ? '← ' : '→ ') + (o ? o.short : other.node) + '.' + other.port);
          b.type = 'button';
          b.title = o && o.label ? o.label : other.node;
          b.addEventListener('click', () => select({ node: other.node }, true));
          r.append(b);
        }
        pl.append(r);
      }
    }
    if (!pl.firstChild) pl.append(el('p', 'dim small', 'No ports.'));
    box.append(pl);
    const del = el('button', null, 'Remove node');
    del.type = 'button';
    del.disabled = S.readOnly;
    del.title = 'Removes the node and its wires (Delete)';
    del.addEventListener('click', removeSel);
    box.append(del);
  }
  function jumpBtn(text, id) {
    const b = el('button', null, text);
    b.type = 'button';
    b.addEventListener('click', () => select({ node: id }, true));
    return b;
  }

  // one parameter: the input fits the type of the reference (or of the value), the value keeps its quoting
  function paramRow(n, path, t) {
    const raw = n.params[path], spec = t && (t.params || []).find((p) => p.path === generic(path));
    if (isList(n, path)) return listRow(n, path, spec);
    let type = spec && spec.type ? String(spec.type) : kindOf(raw);
    const row = el('label', 'kparam');
    const name = el('span', 'kpn mono', path);
    name.title = path + (spec ? ' · ' + spec.type + (spec.min != null ? ' · min ' + spec.min : '') + (spec.max != null ? ' · max ' + spec.max : '') : '');
    row.append(name);
    const err = el('span', 'kperr small');
    const locked = S.readOnly || !!(spec && spec.follows) || type === 'null';
    if (spec && spec.follows) row.title = 'The length of ' + spec.follows.slice(0, -4) + ': changes with its + and −';
    let input, read;
    const enumv = spec && Array.isArray(spec.enum) && spec.enum.length ? spec.enum.map(String) : null;
    if (enumv) {
      const q = raw.length >= 2 && raw[0] === '"' && raw[raw.length - 1] === '"';
      for (let i = 0; i < enumv.length; i++) if (q && enumv[i] !== 'NULL' && enumv[i][0] !== '"') enumv[i] = '"' + enumv[i] + '"';
      input = el('select');
      for (const v of enumv.includes(raw) ? enumv : [raw].concat(enumv)) input.append(new Option(unq(v) === '' ? '(empty)' : unq(v), v));
      input.value = raw;
      read = () => input.value;
    } else if (/^bool/.test(type) || raw === 'true' || raw === 'false') {
      input = el('input');
      input.type = 'checkbox';
      input.checked = raw === 'true';
      read = () => (input.checked ? 'true' : 'false');
    } else if (/int|float/.test(type) && !/str/.test(type)) {
      input = el('input');
      input.type = 'number';
      input.step = type === 'int' ? '1' : 'any';
      if (spec && spec.min != null && type === 'int') input.min = spec.min;
      input.value = raw;
      read = () => {
        const v = input.value.trim();
        if (!/^-?\d+(\.\d+)?$/.test(v) && !/^-?\d*\.\d+$/.test(v)) throw new Error('A number, please.');
        const x = Number(v);
        if (type === 'int') { if (!Number.isInteger(x)) throw new Error('A whole number, please.'); return String(x); }
        return x.toFixed(10).replace(/\.?0+$/, '') || '0';
      };
    } else {
      const quoted = raw.length >= 2 && raw[0] === '"' && raw[raw.length - 1] === '"';
      const nullable = /null/.test(type) || raw === 'NULL';
      const wrap = el('span', 'kstr');
      input = el('input');
      input.type = 'text';
      input.spellcheck = false;
      input.value = raw === 'NULL' ? '' : quoted ? raw.slice(1, -1) : raw;
      let nul = null;
      if (nullable) {
        nul = el('input');
        nul.type = 'checkbox';
        nul.checked = raw === 'NULL';
        nul.title = 'NULL: no value';
        nul.disabled = locked;
        input.disabled = nul.checked || locked;
        const l = el('label', 'knull');
        l.append(nul, document.createTextNode('NULL'));
        nul.addEventListener('change', () => { input.disabled = nul.checked; commit(); });
        wrap.append(input, l);
      } else {
        input.disabled = locked;
        wrap.append(input);
      }
      read = () => {
        if (nul && nul.checked) return 'NULL';
        const v = input.value;
        if (/["\r\n\0]/.test(v)) throw new Error('No quotes or line breaks in a value.');
        return quoted || /str/.test(type) || raw === 'NULL' ? '"' + v + '"' : v;
      };
      input.title = raw;
      row.append(wrap, err);
      input.addEventListener('change', commit);
      return row;
    }
    input.disabled = locked;
    input.title = raw;
    input.addEventListener('change', commit);
    row.append(input, err);
    return row;

    function commit() {
      let v;
      try { v = read(); } catch (e) { err.textContent = e.message; row.classList.add('bad'); return; }
      err.textContent = '';
      row.classList.remove('bad');
      if (v === n.params[path]) return;
      record('Set ' + n.short + '.' + path + ' = ' + (v.length > 40 ? v.slice(0, 40) + '…' : v), [{ op: 'set', node: n.id, path, value: v }]);
      redraw();
    }
  }
  // a list's count with − and +: the list shrinks or grows at its end, its ports with it where the type says so
  function listRow(n, path, spec) {
    const count = Number(n.params[path]) || 0, list = path.slice(0, -4);
    const row = el('div', 'kparam');
    row.append(el('span', 'kpn mono', path));
    const ctl = el('span', 'kcount'), minus = el('button', 'ghost', '−'), plus = el('button', 'ghost', '+');
    minus.type = plus.type = 'button';
    ctl.append(minus, el('b', null, String(count)), plus);
    row.append(ctl);
    const fixed = spec && spec.resizable === false, per = (spec && spec.ports) || [];
    minus.disabled = S.readOnly || fixed || count <= 0;
    plus.disabled = S.readOnly || fixed || count >= 256 || newItem(n, path) === undefined;
    minus.setAttribute('aria-label', 'Remove the last item of ' + list);
    plus.setAttribute('aria-label', 'Add an item to ' + list);
    row.title = fixed ? 'The ports of ' + n.short + ' are named after what this list holds: it cannot be resized here.'
      : newItem(n, path) === undefined ? 'What a new item of this list holds is not known: it can only shrink.'
        : per.length ? 'Each item comes with the ports ' + per.map((p) => p.name.replace('%d', 'N')).join(', ') + '.' : '';
    const go = (m) => {
      const cut = listPorts(n, path)(m)[1];
      const wires = [...S.edges.values()].filter((e) => cut.some((p) => onPort(e, n.id, p))).length;
      record((m > count ? 'Add item ' + count + ' to ' : 'Remove item ' + m + ' of ') + n.short + '.' + list
        + (per.length ? (m > count ? ' (+' : ' (−') + plural(per.length, 'port', 'ports') + ')' : '')
        + (wires ? ' and ' + plural(wires, 'wire', 'wires') : ''), [{ op: 'set', node: n.id, path, value: String(m) }]);
      inspect();
    };
    minus.addEventListener('click', () => go(count - 1));
    plus.addEventListener('click', () => go(count + 1));
    return row;
  }
  function kindOf(raw) {
    if (raw === 'true' || raw === 'false') return 'bool';
    if (raw === 'NULL') return 'null|str';
    if (/^-?\d+$/.test(raw)) return 'int';
    if (/^-?\d+\.\d+$/.test(raw)) return 'float';
    return 'str';
  }

  // ---- the palette ---------------------------------------------------------------------------

  async function loadTypes(g) {
    if (types && typesGame === g) return types;
    try {
      const list = await getJson(api('scripts', 'nodetypes'));
      types = list;
      typesGame = g;
      typesErr = null;
      typeMap = new Map(list.map((t) => [t.type, t]));
    } catch (e) {
      typesErr = e.message;
    }
    return types;
  }
  function openPalette() {
    if (!S || S.readOnly) return;
    $('kpal').hidden = false;
    const cat = $('kpalcat');
    if (!cat.options.length && types) {
      cat.append(new Option('All categories', ''));
      for (const c of [...new Set(types.map((t) => t.category || 'Other'))].sort()) cat.append(new Option(c, c));
    }
    paletteList();
    $('kpalq').focus();
    $('kpalq').select();
  }
  function closePalette() { $('kpal').hidden = true; }
  function paletteList() {
    const list = $('kpallist'), q = $('kpalq').value.trim().toLowerCase(), c = $('kpalcat').value;
    list.textContent = '';
    if (!types) { $('kpalnote').textContent = typesErr ? 'The node reference did not load: ' + typesErr : 'Loading …'; return; }
    const hits = types.filter((t) => (!c || (t.category || 'Other') === c)
      && (!q || (t.short + ' ' + t.type + ' ' + (t.description || '')).toLowerCase().includes(q)))
      .sort((p, r) => p.short.localeCompare(r.short));
    for (const t of hits.slice(0, 300)) {
      const b = el('button', 'kpalrow');
      b.type = 'button';
      b.append(el('b', null, t.short), el('span', 'dim', t.category || ''));
      if (t.description) b.append(el('span', 'kpald', t.description));
      b.title = t.type;
      b.addEventListener('click', () => { closePalette(); addNode(t); cv.focus(); });
      list.append(b);
    }
    $('kpalnote').textContent = plural(hits.length, 'node type', 'node types') + (hits.length > 300 ? ' (first 300 shown)' : '') + ' · a click adds it in the middle of the view';
  }
  $('kpalq').addEventListener('input', paletteList);
  $('kpalcat').addEventListener('change', paletteList);
  $('kpalq').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { const b = $('kpallist').querySelector('button'); if (b) { e.preventDefault(); b.click(); } }
  });
  $('kadd').addEventListener('click', () => ($('kpal').hidden ? openPalette() : closePalette()));
  $('kfit').addEventListener('click', () => fitView());
  $('kmis').addEventListener('click', () => missions(!S.missions));
  $('kundo').addEventListener('click', undo);

  function missions(on) {
    if (!S) return;
    if (on && !flow().missions) { toast('No mission or objective nodes in this script.'); on = false; }
    S.missions = on;
    $('kmis').setAttribute('aria-pressed', String(on));
    $('kmis').classList.toggle('on', on);
    if (sel && sel.node && !visible(sel.node)) sel = null;
    place();
    fitView();
    $('kcount').textContent = counts();
    inspect();
  }

  // ---- open ----------------------------------------------------------------------------------

  const memoKey = (id, mod) => id + '\u0001' + (mod || '');
  async function open(game, id, mod) {     // mod: show the copy in that Studio mod instead of the game's
    const my = ++gen;
    if (S) memo.set(memoKey(S.info.id, S.info.mod), S.acts);
    S = null;
    sel = null;
    hover = null;
    drag = null;
    closePalette();
    showPane('kpane');
    $('ktitle').textContent = id.split('/').pop();
    $('ksub').textContent = id;
    $('kinsp').textContent = '';
    $('kread').hidden = true;
    $('kfind').value = '';
    finder.hits = new Set();
    finder.list = [];
    edits();
    wait('Reading the script …');
    redraw();
    let info;
    try {
      [info] = await Promise.all([getJson(api('scripts', 'info', mod ? { id, mod } : { id })), loadTypes(game)]);
    } catch (e) {
      if (my === gen) { wait(''); showErr(e.message); }
      return;
    }
    if (my !== gen) return;
    S = build(info, memo.get(memoKey(info.id, info.mod)) || []);
    S.game = game;
    if (S.acts.length) replay(); else kindEdges();
    wait(S.nodes.size > 150 ? 'Laying out ' + num(S.nodes.size) + ' nodes …' : '');
    await new Promise((r) => setTimeout(r, 0));   // the message shows before a big layout
    if (my !== gen) return;
    const t0 = performance.now();
    layout([...S.nodes.keys()], S.pos.all);
    S.layoutMs = performance.now() - t0;
    wait('');
    $('ktitle').textContent = info.name;
    $('kread').hidden = info.editable;
    $('kread').textContent = info.editable ? '' : 'Read-only: ' + (info.reason || 'this script cannot be changed.');
    $('kadd').disabled = !info.editable;
    $('kmis').disabled = !flow().missions;
    $('kmis').title = flow().missions ? 'Show only the mission flow: mission and objective nodes and the event paths between them'
      : 'This script has no mission or objective nodes';
    $('kmis').classList.remove('on');
    $('kmis').setAttribute('aria-pressed', 'false');
    $('kmsg').hidden = !!S.nodes.size;
    $('kmsg').textContent = S.nodes.size ? '' : 'This script is empty: no nodes.';
    edits();
    inspect();
    resize();
    fitView(null, 1);
  }
  function wait(text) {
    $('kwait').hidden = !text;
    $('kwait').querySelector('span').textContent = text;
  }

  // ---- save as mod ---------------------------------------------------------------------------

  const dlg = el('dialog');
  dlg.id = 'kdlg';
  dlg.setAttribute('aria-labelledby', 'kdlgtitle');
  dlg.innerHTML = `
    <form method="dialog" class="rephead"><h2 id="kdlgtitle">Save script as mod</h2><button class="icon" value="close" aria-label="Close">&#xd7;</button></form>
    <p id="kdlgwhat" class="sub mono"></p>
    <div class="kdlgbody">
      <ul class="repnotes small">
        <li>The loader applies the mod; the game files stay untouched. Structurally checked by the server, not confirmed in game.</li>
        <li>The server checks every edit (types, ports, parameter values, no loose wires) and refuses the whole save if one fails.</li>
      </ul>
      <div id="kdlgops" class="repdiff mono"></div>
      <fieldset class="repmod">
        <legend class="cap">Save as mod</legend>
        <label class="check"><input type="radio" name="kdlgto" value="new" checked><span>New mod</span></label>
        <div class="reprow"><input id="kdlgname" type="text" maxlength="60" placeholder="Mod name" autocomplete="off"><input id="kdlgauthor" type="text" maxlength="60" placeholder="Author (optional)" autocomplete="off"></div>
        <label class="check"><input type="radio" name="kdlgto" value="add" id="kdlgadd"><span>Add to one of my Studio mods</span></label>
        <select id="kdlgfolder" aria-label="Studio mod"></select>
      </fieldset>
      <p id="kdlgbase" class="small dim"></p>
      <p id="kdlgstatus" class="repstatus small" role="status" aria-live="polite"></p>
      <div class="reprow end"><button id="kdlgsave" class="accent" type="button" disabled>Save as mod</button></div>
    </div>`;
  document.body.append(dlg);
  let saving = null;

  function target() {
    return $('kdlgadd').checked ? { folder: $('kdlgfolder').value } : { name: $('kdlgname').value.trim(), author: $('kdlgauthor').value.trim() };
  }
  function ready() {
    const t = target();
    $('kdlgsave').disabled = !!saving || !(S && saving === null && compact(S.acts).length) || (t.folder !== undefined ? !t.folder : !t.name);
    // which copy the server edits: the mod's own when it has the script already (studiomod.save_script)
    const has = S && t.folder && (S.info.mods || []).some((m) => m.folder === t.folder);
    $('kdlgbase').textContent = !has ? '' : S.info.mod ? 'Your edits add to the copy in this mod, the one on screen.'
      : 'This mod changes this script already: your edits go onto its copy (Version in the script overview shows it).';
  }
  function status(text, cls) {
    $('kdlgstatus').textContent = text || '';
    $('kdlgstatus').className = 'repstatus small' + (cls ? ' ' + cls : '');
  }
  async function mods(game, pick) {
    const sel = $('kdlgfolder');
    sel.textContent = '';
    let list = [];
    try { list = await getJson('/api/' + game + '/mods'); } catch (e) { /* a new mod still works */ }
    for (const m of list) sel.append(new Option(m.name + ' (' + m.folder + ', ' + m.assets + ' assets)', m.folder));
    $('kdlgadd').disabled = sel.disabled = !list.length;
    const want = pick || store.get('folder.' + game);
    if (list.some((m) => m.folder === want)) {
      sel.value = want;
      if (pick || !$('kdlgname').value.trim()) $('kdlgadd').checked = true;
    }
    if (!list.length) dlg.querySelector('input[name=kdlgto][value=new]').checked = true;
    // edits made on a mod's copy go back into that mod: a new mod or another one would start from another copy
    const fixed = !!(S && S.info.mod && list.some((m) => m.folder === S.info.mod));
    if (fixed) { sel.value = S.info.mod; $('kdlgadd').checked = true; }
    for (const x of [dlg.querySelector('input[name=kdlgto][value=new]'), $('kdlgname'), $('kdlgauthor')]) x.disabled = fixed;
    sel.disabled = !list.length || fixed;
    ready();
  }
  function opText(o) {
    const nm = (id) => { const n = S.nodes.get(id) || S.info.nodes.find((x) => x.id === id); return (n ? n.short : '?') + ' ' + id; };
    if (o.op === 'set') return '~ ' + nm(o.node) + ' · ' + o.path + ' = ' + o.value;
    if (o.op === 'add_node') return '+ node ' + (typeMap.get(o.type) || { short: o.type }).short + ' ' + o.id;
    if (o.op === 'remove_node') return '- node ' + nm(o.node);
    if (o.op === 'add_edge') return '+ wire ' + nm(o.from.node) + '.' + o.from.port + ' → ' + nm(o.to.node) + '.' + o.to.port;
    const e = S.info.edges.find((x) => x.id === o.edge);
    return '- wire ' + (e ? nm(e.from.node) + '.' + e.from.port + ' → ' + nm(e.to.node) + '.' + e.to.port : o.edge);
  }
  function openSave(game) {
    if (!S || S.readOnly) return;
    const ops = compact(S.acts);
    if (!ops.length) { toast('Nothing to save: the edits cancel out.'); return; }
    saving = null;
    $('kdlgwhat').textContent = S.info.id;
    const box = $('kdlgops');
    box.textContent = '';
    for (const o of ops) { const d = el('div', o.op.startsWith('remove') ? 'del' : o.op === 'set' ? '' : 'add', opText(o)); box.append(d); }
    status(plural(ops.length, 'edit', 'edits') + ' go to the server.');
    if (!dlg.open) dlg.showModal();
    mods(game);
    ready();
  }
  async function save() {
    const s = S, ops = compact(s.acts), t = target(), game = s.game;
    const q = Object.assign({ id: s.info.id }, t);
    if (!q.author) delete q.author;
    saving = true;
    ready();
    status('Checking and saving …');
    let d;
    try {
      d = await (await fetchOk(api('scripts', 'save', q), {
        method: 'POST', body: JSON.stringify({ ops }),
        headers: { 'Content-Type': 'application/json', 'X-Wolfsdk-Studio': '1' },
      })).json();
    } catch (e) {
      saving = null;
      status(e.message, 'bad');
      ready();
      return;
    }
    saving = null;
    s.saved = { folder: d.folder, ops: ops.length };
    store.set('folder.' + game, d.folder);
    status(d.message, 'ok');
    toast(d.message, 8000);
    $('kdlgname').value = '';
    await showSaved(s, d);
    await mods(game, d.folder);
  }
  // after a save the view becomes the mod's copy, edits included, so the next save adds to it;
  // the nodes keep their places (an added node under the id the server gave it)
  async function showSaved(s, d) {
    let info;
    try { info = await getJson(api('scripts', 'info', { id: s.info.id, mod: d.folder })); } catch (e) { return; }
    if (S !== s) return;
    memo.delete(memoKey(s.info.id, s.info.mod));
    const id = (k) => (d.added && d.added[k]) || k, remap = (m) => new Map([...m].map(([k, p]) => [id(k), p]));
    S = build(info, []);
    Object.assign(S, { game: s.game, saved: s.saved, layoutMs: s.layoutMs, missions: s.missions,
      pos: { all: remap(s.pos.all), mis: remap(s.pos.mis) } });
    if (sel && sel.node) sel = S.nodes.has(id(sel.node)) ? { node: id(sel.node) } : null;
    else sel = null;
    kindEdges();
    place();
    edits();
    inspect();
    redraw();
  }
  $('kdlgsave').addEventListener('click', save);
  for (const x of ['kdlgname', 'kdlgfolder']) $(x).addEventListener('input', ready);
  $('kdlgfolder').addEventListener('change', () => { $('kdlgadd').checked = true; ready(); });
  for (const x of dlg.querySelectorAll('input[name=kdlgto]')) x.addEventListener('change', ready);
  $('kdlgname').addEventListener('focus', () => { dlg.querySelector('input[name=kdlgto][value=new]').checked = true; ready(); });
  dlg.addEventListener('keydown', (e) => e.stopPropagation());   // the list behind must not step
  $('ksave').addEventListener('click', () => openSave(S && S.game));

  // ---- for tests (and curious people in the console) ----------------------------------------

  function screenOf(id, port, dir) {
    const n = S && S.nodes.get(id), p = n && posMap().get(id);
    if (!p) return null;
    const w = port ? (dir === 'in' ? inXY(n, port) : outXY(n, port)) : [p.x + NODE_W / 2, p.y + 12];
    const r = cv.getBoundingClientRect();
    return [r.left + view.x + w[0] * view.s, r.top + view.y + w[1] * view.s];
  }
  function state() {
    if (!S) return { open: false };
    return { open: true, id: S.info.id, mod: S.info.mod || null, nodes: S.nodes.size, edges: S.edges.size, readOnly: S.readOnly, reason: S.info.reason,
      missions: S.missions, flow: S.missions ? flow().ids.length : null, sel, acts: S.acts.map((a) => a.label),
      ops: compact(S.acts), drawn, view: Object.assign({}, view), layoutMs: S.layoutMs, saved: S.saved || null,
      types: types ? types.length : 0, dialog: dlg.open, status: $('kdlgstatus').textContent };
  }
  window.studioScripts = { state, screenOf, fit: () => fitView(), centre, select: (id) => select({ node: id }, true), missions };

  return { open, state };
};
