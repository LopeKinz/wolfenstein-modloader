"""Kiscule, Wolfenstein II's level scripting: container, text, node graph and edits.

`extkisclule` entries look binary in the archive and are not. Behind the
archive's own compression sits a second layer, and behind that plain text in
the decl dialect -- a node graph, not bytecode. This module opens both layers,
packs them again bit-exactly, models the graph and applies the Studio's edits.
tools/kiscule.py is its command line (cache, signatures, --roundtrip, --verify).

## The container

    0x00  i64  usize   >=0: length after unpacking. <0: stored text, |usize| long
    0x08  u64  csize   always len(payload) - 16
    0x10  ...          Kraken frame (0x8C...) or the stored text

Measured over every extkisclule entry of the install (3162 entries, 3010
names, archive compression 0 in all of them): 17 are stored, the rest Kraken.
The stored ones are not a special case but *empty* kiscules -- 39 bytes of
text Kraken cannot make smaller (`_should_store`). None is cfile-encrypted.

## The text

A strict line grammar, machine-written. Every line is one of three, indented
with tabs to the nesting depth:

    <tabs>key = {          block start
    <tabs>}                block end
    <tabs>key = value;     scalar

Five value kinds: int, float, "string", true/false, NULL. A file starts with
a line break and ends in `}` plus one NUL byte. Line ends are LF or CRLF per
file, never mixed. Over all 2408 base/ scripts: 5 415 467 lines, 0 unparsable.

## Edits (apply_ops, the Studio's scripts/save)

    {op: 'set', node, path, value}          a parameter (path as in view()); X.num resizes list X:
                                            new items start as a retail item of the type, and where
                                            the ports follow the list (LISTS) they grow or shrink
                                            with it, the edges on a dropped port at both ends
    {op: 'add_node', type, id, params}      a node of a catalogued type (docs/kiscule_nodes.json),
                                            built like a retail node of that type; `id` names it
                                            for the later ops of the same request
    {op: 'remove_node', node}               the node and every edge end that touches it
    {op: 'add_edge', from: {node, port}, to: {node, port}}   output -> input, event or variable on both
    {op: 'remove_edge', edge}               an edge id of view()

Every edge is written at both of its ends, as in retail. After the ops the
text is parsed again and must not hold an edge end the original did not
(retail itself ships 43 broken ones in 30 files: those stay as they are).
"""

import functools
import hashlib
import json
import re
import struct
from pathlib import Path

from . import oodle

TYPE = "extkisclule"
HDR = struct.Struct("<qQ")
NODES_JSON = Path(__file__).resolve().parent.parent / "docs" / "kiscule_nodes.json"
MAX_LIST = 256          # items a resized list may hold (retail's longest parameter list: 99)


class KisculeError(Exception):
    pass


class EditError(KisculeError):
    """An edit op that cannot be applied (the request's fault: 400)."""


# -- layer 1: the container ------------------------------------------------------------

def unpack(payload, oo=None):
    """The payload of an extkisclule entry -> the text (NUL included)."""
    if len(payload) < 16:
        raise KisculeError("too short for the 16-byte header: %d bytes" % len(payload))
    usize, csize = HDR.unpack_from(payload, 0)
    if csize + 16 != len(payload):
        raise KisculeError("csize %d does not match %d bytes of payload" % (csize, len(payload)))
    if usize < 0:
        if -usize != csize:
            raise KisculeError("stored, but |usize| %d != csize %d" % (-usize, csize))
        return payload[16:]
    return (oo or oodle.load()).decompress(payload[16:], usize)


def _should_store(inner, oo):
    """Retail stores the text when Kraken does not make it smaller."""
    return len(oo.compress(inner, level=oodle.LEVEL_NORMAL)) >= len(inner)


def pack(inner, oo=None):
    """The way back. Reproduces the retail bytes (tools/kiscule.py --roundtrip)."""
    oo = oo or oodle.load()
    if _should_store(inner, oo):
        return HDR.pack(-len(inner), len(inner)) + inner
    frame = oo.compress(inner, level=oodle.LEVEL_NORMAL)
    return HDR.pack(len(inner), len(frame)) + frame


# -- layer 2: the text -------------------------------------------------------------------

_LINE = re.compile(r"^(\t*)([^\s=]+) = (.*)$")
SCALAR_KINDS = ("int", "float", "str", "bool", "null")


def value_kind(raw):
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        return "str"
    if raw in ("true", "false"):
        return "bool"
    if raw == "NULL":
        return "null"
    if re.fullmatch(r"-?\d+", raw):
        return "int"
    if re.fullmatch(r"-?\d+\.\d+", raw):
        return "float"
    return "?"


class Block(list):
    """A block: an ordered list of (key, value), value = str | Block.

    Ordered and with duplicates, because writing it back has to be bit-exact.
    get/require read it; on purpose it does *not* pretend to be a dict.
    """

    def get(self, key, default=None):
        for k, v in self:
            if k == key:
                return v
        return default

    def require(self, key):
        v = self.get(key, _MISS)
        if v is _MISS:
            raise KisculeError("key missing: %s" % key)
        return v

    def items_array(self):
        """The `item[i]` sequence of a block with `num`, the count checked."""
        num = int(self.require("num"))
        if num and self.get("item[0]", _MISS) is _MISS:   # Youngblood (kiscule version 8): items keyed by node id
            keyed = [v for k, v in self if k.startswith("item[")]
            if len(keyed) == num:
                return keyed
        out = []
        for i in range(num):
            v = self.get("item[%d]" % i, _MISS)
            if v is _MISS:
                raise KisculeError("num = %d, but item[%d] is missing" % (num, i))
            out.append(v)
        extra = [k for k, _ in self if k.startswith("item[")]
        if len(extra) != num:
            raise KisculeError("num = %d, but %d item[] entries" % (num, len(extra)))
        return out


_MISS = object()


class Doc:
    """A parsed Kiscule script. `render()` gives the bytes back."""

    def __init__(self, data):
        if not data.endswith(b"\x00"):
            raise KisculeError("no NUL at the end")
        body = data[:-1].decode("latin-1")
        self.nl = "\r\n" if "\r\n" in body else "\n"
        lines = body.split(self.nl)
        if lines[0] != "":
            raise KisculeError("the first line is not empty: %r" % lines[0][:40])
        # The root block has no `{` line of its own: its content is at depth 1,
        # the closing `}` at depth 0.
        self.root, i = _parse(lines, 1, 1)
        if i != len(lines):
            raise KisculeError("text left after the root block at line %d" % i)

    def render(self):
        out = []
        _emit(self.root, 1, out)
        out.append("}")
        return (self.nl + self.nl.join(out)).encode("latin-1") + b"\x00"

    @property
    def kiscule(self):
        return self.root.require("edit").require("kiscule")

    def line_count(self):
        """Physical lines: one per scalar, two per block, plus the root `}`."""
        n = [0]
        _count(self.root, n)
        return n[0] + 1


def _parse(lines, i, depth):
    """Elements at indentation `depth`. The `}` stands at depth-1."""
    closer = "\t" * (depth - 1) + "}"
    block = Block()
    while i < len(lines):
        line = lines[i]
        if line == closer:
            return block, i + 1
        m = _LINE.match(line)
        if not m or len(m.group(1)) != depth:
            raise KisculeError("line %d does not fit depth %d: %r" % (i + 1, depth, line[:60]))
        key, rest = m.group(2), m.group(3)
        if rest == "{":
            inner, i = _parse(lines, i + 1, depth + 1)
            block.append((key, inner))
            continue
        if not rest.endswith(";"):
            raise KisculeError("line %d without a semicolon: %r" % (i + 1, line[:60]))
        block.append((key, rest[:-1]))
        i += 1
    raise KisculeError("block end missing at depth %d" % depth)


def _emit(block, depth, out):
    pad = "\t" * depth
    for key, value in block:
        if isinstance(value, Block):
            out.append("%s%s = {" % (pad, key))
            _emit(value, depth + 1, out)
            out.append("%s}" % pad)
        else:
            out.append("%s%s = %s;" % (pad, key, value))


def _count(block, n):
    for _, value in block:
        n[0] += 1
        if isinstance(value, Block):
            n[0] += 1
            _count(value, n)


# -- layer 3: the graph --------------------------------------------------------------------

DIRECTION = {"InputRegular": "in", "InputVariable": "in", "InputNotification": "in", "InputEvent": "in",
             "OutputRegular": "out", "OutputVariable": "out", "OutputNotification": "out"}
KIND = {"InputRegular": "event", "InputNotification": "event", "InputEvent": "event", "OutputRegular": "event",
        "OutputNotification": "event", "InputVariable": "variable", "OutputVariable": "variable"}


class Port:
    __slots__ = ("name", "id", "type", "properties", "visible", "edges")

    def __init__(self, blk):
        self.name = unquote(blk.require("name"))
        self.id = int(blk.require("id"))
        self.type = unquote(blk.require("type"))
        self.properties = int(blk.require("properties"))
        self.visible = blk.require("visible") == "true"
        self.edges = [(int(c.require("id")), int(c.require("endNodeID")), int(c.require("endConnectionPointID")))
                      for c in blk.require("connections").items_array()]

    @property
    def direction(self):
        return DIRECTION.get(self.type, "?")

    @property
    def kind(self):
        return KIND.get(self.type)


class Node:
    __slots__ = ("kind", "id", "pos", "param_class", "params", "ports")

    def __init__(self, blk):
        self.kind = unquote(blk.require("name"))
        self.id = int(blk.require("id"))
        p = blk.get("nodePos")
        # Youngblood writes "$0x<double bits> 2920": the readable number is the last word
        self.pos = (int(float(p.require("x").split()[-1])), int(float(p.require("y").split()[-1]))) if p else None
        # `parameters = NULL;` occurs -- nodes without a parameter object.
        par = blk.get("parameters")
        if not isinstance(par, Block):
            par = None
        self.param_class = unquote(par.require("class")) if par else None
        obj = par.get("object") if par else None
        self.params = obj if isinstance(obj, Block) else None
        cp = blk.get("connectionPoints")
        self.ports = [Port(c) for c in cp.items_array()] if isinstance(cp, Block) else []


class Graph:
    def __init__(self, doc, name=""):
        self.name = name
        self.doc = doc
        k = doc.kiscule
        self.version = k.get("version")
        nodes = k.get("nodes")
        self.nodes = [Node(b) for b in nodes.items_array()] if nodes else []
        self.by_id = {n.id: n for n in self.nodes}
        self.comments = k.get("comments").items_array() if k.get("comments") else []
        self.variables = k.get("variables").items_array() if k.get("variables") else []

    def port(self, node_id, port_id):
        n = self.by_id.get(node_id)
        if n is None:
            return None, None
        for p in n.ports:
            if p.id == port_id:
                return n, p
        return n, None


def unquote(s):
    return s[1:-1] if len(s) >= 2 and s[0] == '"' and s[-1] == '"' else s


def flatten_params(blk, prefix=""):
    """parameters.object -> [(dotted path, raw value)], lists as item[i]."""
    out = []
    if blk is None:
        return out
    for key, value in blk:
        path = "%s.%s" % (prefix, key) if prefix else key
        if isinstance(value, Block):
            out.extend(flatten_params(value, path))
        else:
            out.append((path, value))
    return out


def short(kind):
    return kind[len("idKisculeNode"):] if kind.startswith("idKisculeNode") else kind


def generic(path):
    """A parameter path with its list indices dropped: targets.item[3] -> targets.item[]."""
    return re.sub(r"\[\d+\]", "[]", path)


# -- the Studio's view -------------------------------------------------------------------------

MISSION = "idKisculeNodeActionMission"
MISSION_ROLES = (("MissionStart", "start"), ("ObjectiveStart", "objective_start"),
                 ("ObjectiveComplete", "objective_complete"), ("SetActiveObjective", "set_active"))


def edges(graph):
    """({(from node, from port, to node, to port): None} in file order, broken edge ends).

    An edge stands at both of its ends; it is listed once, from either end.
    An end whose other side is missing (node or port) or points the wrong way
    is broken and not listed."""
    out, broken = {}, 0
    for n in graph.nodes:
        for p in n.ports:
            for _eid, en, ep in p.edges:
                _tn, tp = graph.port(en, ep)
                if tp is None or "?" in (p.direction, tp.direction) or p.direction == tp.direction:
                    broken += 1
                    continue
                out.setdefault((n.id, p.id, en, ep) if p.direction == "out" else (en, ep, n.id, p.id), None)
    return out, broken


def edge_id(key):
    return "%d.%d>%d.%d" % key


def _mission(n, params):
    if n.kind != MISSION:
        return {"role": None, "decl": None}
    wired = {p.name for p in n.ports if p.direction == "in" and p.edges}
    role = next((r for name, r in MISSION_ROLES if name in wired), None)
    decls = [unquote(params.get("mission", "NULL"))]
    if role not in (None, "start"):
        decls.insert(0, next((unquote(v) for k, v in params.items()
                              if k.startswith("objectives.item[") and value_kind(v) == "str"), "NULL"))
    return {"role": role, "decl": next((d for d in decls if d not in ("", "NULL")), None)}


def _label(n, params):
    """The node's short name, plus its first non-empty top-level string parameter."""
    text = next((unquote(v) for k, v in params.items() if "." not in k and value_kind(v) == "str" and len(v) > 2), "")
    name = short(n.kind)
    return "%s: %s" % (name, text if len(text) <= 60 else text[:57] + "...") if text else name


def view(graph):
    """{nodes, edges, broken_edges} as the Studio's scripts/info sends them."""
    nodes = []
    for n in graph.nodes:
        params = dict(flatten_params(n.params))
        nodes.append({"id": str(n.id), "type": n.kind, "short": short(n.kind), "label": _label(n, params),
                      "params": params, "pos": list(n.pos) if n.pos else None,
                      "ports": [{"name": p.name, "dir": p.direction, "kind": p.kind} for p in n.ports],
                      "mission": _mission(n, params)})
    found, broken = edges(graph)
    names = {(n.id, p.id): p.name for n in graph.nodes for p in n.ports}
    out = [{"id": edge_id(k), "from": {"node": str(k[0]), "port": names[k[0], k[1]]},
            "to": {"node": str(k[2]), "port": names[k[2], k[3]]}} for k in found]
    return {"nodes": nodes, "edges": out, "broken_edges": broken}


def summary(graph):
    """(nodes, edges, ActionMission nodes): the counts of a listing row, as view() counts them."""
    return len(graph.nodes), len(edges(graph)[0]), sum(n.kind == MISSION for n in graph.nodes)


def defects(graph):
    """Every edge end without a proper other end: target missing, same direction, or not mirrored."""
    out = set()
    for n in graph.nodes:
        for p in n.ports:
            for _eid, en, ep in p.edges:
                _tn, tp = graph.port(en, ep)
                if (tp is None or "?" in (p.direction, tp.direction) or p.direction == tp.direction
                        or not any(e[1] == n.id and e[2] == p.id for e in tp.edges)):
                    out.add((n.id, p.id, en, ep))
    return out


# -- the node catalogue ---------------------------------------------------------------------------

# The catalogue marks a list `resizable: false` when its type's port sets vary. Measured over every
# retail node of those types (tools/verify_studio_kiscule.py part A checks it again): for two the
# ports follow a list's length -- per item i these ports, after the type's fixed ones, in item order;
# for four the mark is wrong, their ports stay the same whatever the length. The rest
# (FlowFlagCheck, FlowProgressionCheck, MidnightNotification, OpMidnightParametersControl) name
# their ports after the items' contents and stay unresizable.
LISTS = {   # (type, list): (ports per item i, the parameter that holds the count)
    ("idKisculeNodeDynamicSwitch", "weigths"): ((("Case %d", "OutputRegular", 9),), "outputCount"),  # 211/211
    ("idKisculeNodeInputListener", "conditions"): ((("Cond %d Passed", "OutputRegular", 9),
                                                    ("Cond %d Stopped", "OutputRegular", 9)), None),  # 12/12
    ("idKisculeNodeActionMission", "objectives"): ((), None),       # 582/582: the same 24 ports for 0..14
    ("idKisculeNodeActionSoundPostEvent", "entities"): ((), None),  # 1864/1864
    ("idKisculeNodeMapMovementNotification", "listenedEntities"): ((), None),          # 46/46, always empty
    ("idKisculeNodeInputListener", "conditions.item[].triggerEntities"): ((), None),   # 12/12, always empty
}


@functools.lru_cache(maxsize=1)
def nodetypes():
    """{full type: entry} of docs/kiscule_nodes.json (tools/build_kiscule_nodes.py writes it), its
    list counts (X.num) told what resizing does: `item` (what a new item starts as: raw text, or
    {path: raw} for a block), `ports` per item and `count`; a parameter holding a count `follows` it."""
    try:
        entries = json.loads(NODES_JSON.read_text("utf-8"))["types"]
    except (OSError, ValueError, KeyError) as exc:
        raise KisculeError("The node catalogue %s cannot be read: %s" % (NODES_JSON.name, exc))
    types = {t["type"]: t for t in entries}
    for (kind, lst), (ports, count) in LISTS.items():
        spec = {p["path"]: p for p in types[kind]["params"]}
        spec[lst + ".num"].pop("resizable", None)
        spec[lst + ".num"]["ports"] = [{"name": n, "dir": DIRECTION[pt], "kind": KIND[pt]} for n, pt, _pr in ports]
        if count:
            spec[lst + ".num"]["count"] = count
            spec[count]["follows"] = lst + ".num"
    for t in types.values():
        for p in t["params"]:
            item = new_item(t, p["path"][:-4]) if p["path"].endswith(".num") and p.get("resizable") is not False else None
            if item is not None:
                p["item"] = dict(flatten_params(item)) if isinstance(item, Block) else item
    return types


def new_item(t, lst, items=()):
    """What a new item of list `lst` (generic path) of catalogue entry `t` starts as: the type
    template's first item (a retail one, free text blanked), else the catalogue's item default, else
    '""' or a copy of the last scalar item; None when nothing tells."""
    here = _at(_block(t["template"]["parameters"]), "object.%s.item[0]" % lst.replace("[]", "[0]")) if t else None
    if here is not None:
        return here
    spec = next((p for p in (t or {}).get("params", []) if p["path"] == lst + ".item[]"), None)
    if spec:
        return spec["default"]
    if items and not isinstance(items[-1], Block):
        return '""' if value_kind(items[-1]) == "str" else items[-1]
    return None


def initial_params(t):
    """{path: raw value} a new node of catalogue entry `t` starts with: add_node builds it from the template."""
    par = _block(t["template"]["parameters"])
    obj = par.get("object") if isinstance(par, Block) else None
    return dict(flatten_params(obj if isinstance(obj, Block) else None))


def _block(value):
    """A template's nested [[key, value], ...] lists -> Block (strings stay strings)."""
    return Block((k, _block(v) if isinstance(v, list) else v) for k, v in value) if isinstance(value, list) else value


# -- edits ------------------------------------------------------------------------------------------

_STR = re.compile(r'"(?:[^"\\\x00-\x1f]|\\[^\x00-\x1f])*"\Z')
_PLAIN = re.compile(r'[^"\\\x00-\x1f\x7f]*\Z')


def literal(value, kinds):
    """The decl text of `value` for a parameter of these kinds ('int', 'float|int', 'null|str', ...),
    or EditError. A string may come plain (it is quoted) or quoted as the decl writes it."""
    if isinstance(value, bool):
        value = "true" if value else "false"
    elif isinstance(value, (int, float)):
        value = repr(value)
    if not isinstance(value, str):
        raise EditError("a value must be text, a number or true/false")
    allowed = set(kinds.split("|"))
    text = value.strip() if not ("str" in allowed and value.startswith('"')) else value
    kind = value_kind(text)
    if "float" in allowed:
        allowed.add("int")              # the decl parser reads 0 for 0.0 (retail writes both)
    if kind == "str" and "str" in allowed and not _STR.match(text):
        raise EditError("the string %s holds an unescaped quote or a control character" % text[:60])
    if kind in ("int", "float") and kind in allowed and not -2 ** 31 <= float(text) < 2 ** 31:
        raise EditError("%s is out of range" % text)
    if kind in allowed:
        ok = text
    elif "str" in allowed and _PLAIN.match(value):
        ok = '"%s"' % value
    else:
        raise EditError("%r is not a valid %s value" % (value[:60], " or ".join(sorted(set(kinds.split("|"))))))
    try:
        ok.encode("latin-1")
    except UnicodeEncodeError:
        raise EditError("%r holds a character the script's encoding (Latin-1) cannot store" % value[:60])
    return ok


def _q(s):
    return '"%s"' % s


def _port(name, pid, ptype, props, visible):
    """A connection point block, without edges, as retail writes one."""
    return Block([("name", _q(name)), ("id", str(pid)), ("connections", Block([("num", "0")])),
                  ("type", _q(ptype)), ("properties", str(props)), ("visible", visible)])


def _at(block, path):
    """The value at a dotted path of a block, or None."""
    for part in path.split("."):
        block = block.get(part) if isinstance(block, Block) else None
    return block


def _set_items(block, items):
    """Rewrite a num/item[] block to hold `items` (nothing else may be in it)."""
    if any(k != "num" and not k.startswith("item[") for k, _ in block):
        raise KisculeError("not a plain list block")
    block[:] = [("num", str(len(items)))] + [("item[%d]" % i, v) for i, v in enumerate(items)]


def _is_list(block):
    return block.get("num") is not None and all(k == "num" or k.startswith("item[") for k, _ in block)


class _Editor:
    def __init__(self, doc, types, seed):
        self.doc, self.types, self.seed = doc, types, seed
        self.before = defects(Graph(doc))
        k = doc.kiscule
        if not k:                      # an empty kiscule (15 retail scripts): the skeleton retail writes
            k += [("version", "6"), ("nodes", Block([("num", "0")])), ("comments", Block([("num", "0")])),
                  ("variables", Block([("num", "0")]))]
        self.nodes_block = k.get("nodes")
        if not isinstance(self.nodes_block, Block):
            raise EditError("this script has no node list")
        self.items = list(self.nodes_block.items_array())
        self.by_ref = {blk.require("id"): blk for blk in self.items}
        self.taken = {int(v) for blk in self.items for v in _ids(blk)}
        self.added = {}

    # lookups

    def node(self, ref):
        blk = self.by_ref.get(ref if isinstance(ref, str) else None)
        if blk is None:
            raise EditError("there is no node %r" % (ref,))
        return blk

    def port(self, blk, name, direction):
        hits = [p for p in blk.require("connectionPoints").items_array()
                if unquote(p.require("name")) == name and DIRECTION.get(unquote(p.require("type"))) == direction]
        if not hits:
            raise EditError("node %s (%s) has no %s port %r"
                            % (blk.require("id"), short(unquote(blk.require("name"))),
                               "output" if direction == "out" else "input", name))
        if len(hits) > 1:
            raise EditError("node %s has %d %s ports named %r" % (blk.require("id"), len(hits), direction, name))
        return hits[0]

    def fresh(self, label):
        for n in range(1000):
            h = int.from_bytes(hashlib.sha256(("%s|%s|%d" % (self.seed, label, n)).encode()).digest()[:4],
                               "little") & 0x7FFFFFFF
            if h > 0xFFFF and h not in self.taken:          # retail ids: 13122 .. 2^31-1
                self.taken.add(h)
                return h
        raise KisculeError("no free id")

    # ops

    def apply(self, op):
        if not isinstance(op, dict):
            raise EditError("an op must be an object")
        fn = getattr(self, "op_" + str(op.get("op")), None)
        if fn is None:
            raise EditError("unknown op %r (set, add_node, remove_node, add_edge, remove_edge)" % (op.get("op"),))
        fn(op)

    def op_set(self, op):
        blk = self.node(op.get("node"))
        self.set_param(blk, op.get("path"), op.get("value"))

    def set_param(self, blk, path, value):
        kind = unquote(blk.require("name"))
        par = blk.get("parameters")
        obj = par.get("object") if isinstance(par, Block) else None
        if not isinstance(path, str) or not isinstance(obj, Block):
            raise EditError("node %s has no parameter %r" % (blk.require("id"), path))
        parts, here = path.split("."), obj
        for part in parts[:-1]:
            here = here.get(part)
            if not isinstance(here, Block):
                raise EditError("node %s has no parameter %r" % (blk.require("id"), path))
        old = here.get(parts[-1])
        if old is None or isinstance(old, Block):
            raise EditError("node %s has no parameter %r" % (blk.require("id"), path))
        spec = {p["path"]: p for p in self.types.get(kind, {}).get("params", [])}
        if parts[-1] == "num" and _is_list(here):
            return self.resize(blk, here, path, value, spec)
        p = spec.get(generic(path))
        new = literal(value, p["type"] if p else value_kind(old))
        if p and p.get("follows") and new != _at(obj, p["follows"]):
            raise EditError("%s is the length of the list %s (as in every retail %s): resize the list instead"
                            % (path, p["follows"][:-4], short(kind)))
        here[[k for k, _ in here].index(parts[-1])] = (parts[-1], new)

    def resize(self, blk, here, path, value, spec):
        n = int(literal(value, "int"))
        if not 0 <= n <= MAX_LIST:
            raise EditError("a list holds 0 to %d items" % MAX_LIST)
        kind, lst = unquote(blk.require("name")), generic(path[:-4])
        p = spec.get(generic(path))
        if p is not None and p.get("resizable") is False:
            raise EditError("the ports of %s are named after what its list %s holds, and the editor does not "
                            "rebuild them: this list cannot be resized here" % (short(kind), path[:-4]))
        items = here.items_array()
        old = len(items)
        for _ in range(old, n):
            item = new_item(self.types.get(kind), lst, items)
            if item is None:
                raise EditError("the item type of the list %s is not known" % path[:-4])
            items.append(item)
        _set_items(here, items[:n])
        ports, count = LISTS.get((kind, lst), ((), None))
        if ports:
            self.list_ports(blk, ports, old, n)
        if count:
            obj = blk.get("parameters").get("object")
            obj[[k for k, _ in obj].index(count)] = (count, str(n))

    def list_ports(self, blk, per, old, new):
        """The ports a list's items give (LISTS): grown or cut from `old` to `new` items at the end of
        the node's ports, the edges on a cut port gone at both ends."""
        cps = blk.require("connectionPoints")
        ports = cps.items_array()
        keep = len(ports) - old * len(per)
        tail = [(unquote(q.require("name")), unquote(q.require("type")), int(q.require("properties")))
                for q in ports[max(keep, 0):]]
        if keep < 0 or tail != [(name % i, pt, pr) for i in range(old) for name, pt, pr in per]:
            raise EditError("the ports of node %s do not end in the ones its %d list items give, as in retail: "
                            "the list is not resized" % (blk.require("id"), old))
        nid, cut = blk.require("id"), keep + new * len(per)
        self.unlink(nid, {q.require("id") for q in ports[cut:]})
        _set_items(cps, ports[:cut] + [_port(name % i, self.fresh("port|%s|%s" % (nid, name % i)), pt, pr, "true")
                                       for i in range(old, new) for name, pt, pr in per])

    def unlink(self, nid, ports=None):
        """Drop every edge end pointing at node `nid` (only at these port ids of it, when given)."""
        for b in self.items:
            for p in b.require("connectionPoints").items_array():
                conns = p.require("connections")
                _set_items(conns, [c for c in conns.items_array() if c.require("endNodeID") != nid
                                   or ports is not None and c.require("endConnectionPointID") not in ports])

    def op_add_node(self, op):
        kind, ref, params = op.get("type"), op.get("id"), op.get("params") or {}
        t = self.types.get(kind)
        if t is None:
            raise EditError("unknown node type %r" % (kind,))
        if not isinstance(ref, str) or not ref.strip() or len(ref) > 64:
            raise EditError("add_node needs an id (1 to 64 characters) for the new node")
        if ref in self.by_ref:
            raise EditError("the id %r is taken already" % ref)
        if not isinstance(params, dict):
            raise EditError("params must be an object {path: value}")
        tp = t["template"]
        nid = self.fresh("node|" + ref)
        xs = [int(b.get("nodePos").require("x")) for b in self.items if isinstance(b.get("nodePos"), Block)]
        ports = Block([("num", str(len(tp["ports"])))])
        for i, (name, ptype, props, visible) in enumerate(tp["ports"]):
            ports.append(("item[%d]" % i, _port(name, self.fresh("port|%s|%d" % (ref, i)), ptype, props, visible)))
        blk = Block([("name", _q(kind)), ("id", str(nid)),
                     ("nodePos", Block([("x", str(max(xs, default=0) + 400)), ("y", str(150 * len(self.added)))])),
                     ("parameters", _block(tp["parameters"]) if tp["parameters"] is not None else "NULL"),
                     ("connectionPoints", ports)])
        for path, value in params.items():
            self.set_param(blk, path, value)
        self.items.append(blk)
        self.by_ref[ref] = self.by_ref[str(nid)] = blk
        self.added[ref] = str(nid)

    def op_remove_node(self, op):
        blk = self.node(op.get("node"))
        nid = blk.require("id")
        self.items = [b for b in self.items if b is not blk]
        self.by_ref = {k: b for k, b in self.by_ref.items() if b is not blk}
        self.unlink(nid)

    def op_add_edge(self, op):
        src, dst = op.get("from"), op.get("to")
        if not (isinstance(src, dict) and isinstance(dst, dict)):
            raise EditError("add_edge needs from: {node, port} and to: {node, port}")
        a, b = self.node(src.get("node")), self.node(dst.get("node"))
        pa, pb = self.port(a, src.get("port"), "out"), self.port(b, dst.get("port"), "in")
        ka, kb = KIND[unquote(pa.require("type"))], KIND[unquote(pb.require("type"))]
        if ka != kb:
            raise EditError("an %s port cannot be connected to a %s port" % (ka, kb))
        ends = ((pa, b, pb), (pb, a, pa))
        if any(c.require("endNodeID") == b.require("id") and c.require("endConnectionPointID") == pb.require("id")
               for c in pa.require("connections").items_array()):
            raise EditError("these two ports are connected already")
        eid = str(self.fresh("edge|%s.%s>%s.%s" % (a.require("id"), pa.require("id"), b.require("id"), pb.require("id"))))
        for port, node, other in ends:
            conns = port.require("connections")
            _set_items(conns, conns.items_array() + [Block([
                ("name", '""'), ("id", eid), ("endNodeID", node.require("id")),
                ("endConnectionPointID", other.require("id"))])])

    def op_remove_edge(self, op):
        m = re.fullmatch(r"(\d+)\.(\d+)>(\d+)\.(\d+)", str(op.get("edge")))
        if not m:
            raise EditError("there is no edge %r" % (op.get("edge"),))
        a, pa, b, pb = m.groups()
        gone = 0
        for node, port, end, end_port in ((a, pa, b, pb), (b, pb, a, pa)):
            blk = self.by_ref.get(node)
            for p in (blk.require("connectionPoints").items_array() if blk is not None else ()):
                if p.require("id") != port:
                    continue
                conns = p.require("connections")
                keep = [c for c in conns.items_array()
                        if (c.require("endNodeID"), c.require("endConnectionPointID")) != (end, end_port)]
                gone += int(conns.require("num")) - len(keep)
                _set_items(conns, keep)
        if not gone:
            raise EditError("there is no edge %r" % op.get("edge"))

    def finish(self):
        _set_items(self.nodes_block, self.items)
        text = self.doc.render()
        try:
            again = Doc(text)
            graph = Graph(again)
        except (KisculeError, ValueError) as exc:
            raise EditError("the edited script does not read back: %s" % exc)
        if again.render() != text:
            raise EditError("the edited script does not read back the same")
        new = sorted(defects(graph) - self.before)
        if new:
            raise EditError("%d edge end(s) would point nowhere, e.g. node %d port %d -> node %d port %d"
                            % ((len(new),) + new[0]))
        return text


def _ids(blk):
    """Every id in a node block: the node's, its ports', its connections'."""
    yield blk.require("id")
    cp = blk.get("connectionPoints")
    for p in (cp.items_array() if isinstance(cp, Block) else ()):
        yield p.require("id")
        for c in p.require("connections").items_array():
            yield c.require("id")


def apply_ops(doc, ops, types=None, seed=""):
    """Apply the Studio's edit ops to `doc` in place (module docstring). Returns
    {id of add_node: the new node's id}. EditError names the first op that fails;
    the doc is then half edited and must be dropped."""
    if not isinstance(ops, list) or not ops:
        raise EditError("ops must be a non-empty list")
    ed = _Editor(doc, nodetypes() if types is None else types, seed)
    for i, op in enumerate(ops):
        try:
            ed.apply(op)
        except EditError as exc:
            raise EditError("op %d (%s): %s" % (i + 1, op.get("op") if isinstance(op, dict) else "?", exc))
    ed.finish()
    return ed.added
