"""Local web server for the Studio: the 3D map viewer (wolfsdk/mapview/) and
the asset sections of both games (wolfsdk/studio.py).

    url = start(game_root)     one server per process, 127.0.0.1, free port
    open_app(url)              Edge --app fullscreen, else Chrome, else default browser
    python -m wolfsdk maps [map id] [--no-browser]

tkinter cannot draw 3D and the loader has no third-party dependencies, so
the viewer is a page in the browser. This process serves it the data:

  GET /                                 wolfsdk/mapview/index.html (+ its files)
  GET /api/maps                         [{id, title, group, entities_bytes}]
  GET /api/map/<id>/scene.json          mapgeo.scene      (cached file)
  GET /api/map/<id>/geometry.bin        mapgeo.scene      (cached file)
  GET /api/map/<id>/collision.json      mapcoll.collision (cached file)
  GET /api/tex/<texture id>[?max=N]     maptex.texture_block: WTX1 + one mip
  GET /api/tex/<texture id>.png[?max=N] maptex.texture_png: the same mip

A texture id carries no map, so /api/tex asks the maps opened last (a
scene.json request opens its map). Maps without a .entities in their
container (an exec carrier such as dlc_5) are left out of /api/maps: there is
nothing to show. These unprefixed map endpoints are Wolfenstein II's
(game_root) and stay as they are; the same five with the game in the path:

  GET /api/<g>/maps, /api/<g>/map/<id>/{scene.json,geometry.bin,collision.json},
  GET /api/<g>/tex/<texture id>[.png][?max=N]
      tnc: the endpoints above; tno: tnomap.maps/scene, tnomapcoll.collision,
      tnomaptex.texture_block/texture_png (a TNO texture id needs no map)

The Studio, <g> = tnc | tno, <kind> = videos | sounds | textures | texts,
every id as the query parameter id= (URL-encoded):

  GET /api/games                                 [{id, name, available, root, sections}]
  GET /api/<g>/<kind>?q=&group=&lang=&offset=0&limit=100&facets=1&find=<id>
        {total, offset, limit, items: [{id, name, group, lang, ...}],
         groups: [[group, n]], langs: [[lang, n]],   (facets only with facets=1)
         index: place of <id> in the hits or -1}      (only with find=)
  GET /api/<g>/<kind>/info?id=                   everything about one item
  GET /api/<g>/videos/frames?id=&start=0&count=&w=960&seek=auto|exact|key
                                                 WVF1 stream (studio.py), no Range
  GET /api/<g>/videos/frame.png?id=&n=&w=480     one frame (n: default poster)
  GET /api/<g>/videos/audio.wav?id=&tracks=0,3   track ids, default stereo + 1st voice
  GET /api/<g>/sounds/audio?id=                  audio/ogg or audio/wav as decoded
  GET /api/<g>/textures/image.png?id=&max=512
  GET /api/<g>/texts/text?id=                    text/plain; charset=utf-8
  GET /api/<g>/models?...                        the list above, rows + format, size, tris, surfaces
  GET /api/<g>/models/info?id=                   {bounds, lods, joints, surfaces: [{albedo, ...}]}
  GET /api/<g>/models/mesh.bin?id=&lod=0         WMD1 (wolfsdk/md6.py)
  GET /api/<g>/models/albedo.png?id=<tex>&max=1024&v=<key>   a surface's colour map
  GET /api/<g>/models/uv2.bin?id=&surface=N&v=<key>             f32[2nv]: a hair surface's second uv set
  GET /api/<g>/models/anims.json?id=                       {skeleton, joints, anims: [{id, name, group}]} (TNC md6mesh)
  GET /api/<g>/models/skeleton.json?id=                    {names, parents, rot, pos}: bind pose, local, game axes
  GET /api/<g>/models/skin.bin?id=                         WSK1: per surface u32 nv, u16 joint[4nv], f32 weight[4nv]
  GET /api/<g>/models/anim.json?id=&anim=<md6anim>         {name, fps, frames, tracks: [{joint, rot?, pos?}]}
  GET /api/<g>/models/pbr.json?id=<material>&v=<key>         {alpha, roughness, emissive, maps: {slot: URL}} (TNC)
  GET /api/<g>/models/pbr.png?id=<material>&slot=mr|normal|base|occ&v=<key>   one material map
  GET /api/<g>/models/export?id=&format=glb|gltf|obj&surfaces=0,2,5&max=4096 (default: full resolution)
      the model as a file to save (wolfsdk/modelexport.py), Content-Disposition
      attachment; surfaces: the indices to export (absent or empty: those shown at start)
  POST the same with &skin=<material> and a PNG as the body (<= 64 MB): the
      viewer's own skin replaces that material's colour map in the file
      models: TNC only; its errors are JSON {"error": English line}, not text,
      the stdlib's refusals there too (501 for POST elsewhere, 414 for a too long line)

Exports and replacements (wolfsdk/studiomod.py); files as attachments:

  GET /api/<g>/textures/export?id=              the texture as PNG at full size (8192 px skies too)
  GET /api/<g>/texts/file?id=                   the text's file (TNC decrypted/unpacked)
  GET /api/<g>/videos/file?id=                  the .bk2/.bik file (Range)
  GET /api/<g>/<kind>/export.zip?q=&group=&lang=[&check=1]
      every hit in its natural format as one ZIP, streamed (no Content-Length,
      Connection: close); limits (studio.ZIP_LIMITS) are checked before the
      first byte: 413; check=1 answers {count, bytes} instead; models: 422
  GET /api/<g>/<kind>/replace?id=[&skin=<material>]
      {ok, reason, form, target, group, blood, notes}: what replacing writes, or why not
  POST the same, the file as the body (PNG <= 64 MB, text <= 8 MB, WAV <= 256 MB,
      Bink video <= 1 GB), header
      X-Wolfsdk-Studio: 1 (else 403: another page cannot send it without a
      preflight, and OPTIONS is never answered), name=<new mod> or
      folder=<Studio mod>, author=, blood=0|1, skin= (models)
      -> {mod, folder, name, assets, form, notes, message}; writes mods/<folder>/
      only, never the game
  GET /api/<g>/mods                             the Studio's own mods of that game

Every answer with a body of known bytes (files, JSON, audio, PNG, text)
takes a single Range (206 + Content-Range, 416 when unsatisfiable): without
it Chromium guesses <audio>.duration from the bitrate and is off by seconds.

Only 127.0.0.1 is bound, the Host header must name it, and static paths
cannot leave wolfsdk/mapview/. Studio ids are catalog keys, never paths.
Errors are one German line of text/plain (JSON {"error"} under /models, and
English JSON on the replace, export.zip and mods routes). Read-only on both
installs; the replace route writes under mods/ only.
"""

import atexit
import json
import os
import re
import socket
import socketserver
import subprocess
import sys
import threading
import time
import webbrowser
import zipfile
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote

from . import SUPPORT_URL, kiscule, mapcatalog, mapcoll, mapgeo, maptex, modelexport, studio, studiomod, tnomap, tnomapcoll, tnomaptex
from .tncimage import TncImageError

MAPVIEW = Path(__file__).resolve().parent / "mapview"
HOST = "127.0.0.1"
MOUNTS = 3          # maps kept open for /api/tex
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".json": "application/json",
         ".txt": "text/plain; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml",
         ".ico": "image/x-icon", ".bin": "application/octet-stream"}
_TEX = re.compile(r"/api/(?:(tnc|tno)/)?tex/([0-9a-f]{16})(\.png)?\Z")
_MAP = re.compile(r"/api/(?:(tnc|tno)/)?map/(.+)/(scene\.json|geometry\.bin|collision\.json)\Z")
_STUDIO = re.compile(r"/api/([^/]+)/(videos|sounds|textures|texts|models|scripts)(?:/([a-z0-9.]+))?\Z")
_NEW = ("replace", "export.zip")         # every kind: English JSON errors (studiomod)
_LEAVES = {"videos": ("info", "frames", "frame.png", "audio.wav", "file") + _NEW, "sounds": ("info", "audio") + _NEW,
           "textures": ("info", "image.png", "export") + _NEW, "texts": ("info", "text", "file") + _NEW,
           "models": ("info", "mesh.bin", "uv2.bin", "albedo.png", "pbr.json", "pbr.png", "export",
                      "anims.json", "skeleton.json", "skin.bin", "anim.json") + _NEW,
           "scripts": ("info", "nodetypes", "save")}
_JSON_ERRORS = re.compile(r"/api/[^/]+/(?:models(?:/|\Z)|scripts(?:/|\Z)|[a-z]+/(?:replace|export\.zip)\Z|mods\Z)")
_EXPORT = re.compile(r"/api/[^/]+/models/export\Z")
_POSTS = re.compile(r"/api/[^/]+/(?:models/export|scripts/save|[a-z]+/replace)\Z")
_MODS = re.compile(r"/api/([^/]+)/mods\Z")
_RANGE = re.compile(r"\s*bytes\s*=\s*(\d{0,18})\s*-\s*(\d{0,18})\s*\Z", re.I)
_REFUSED = {400: "Bad request.", 414: "The address is too long.", 431: "Too many or too long header lines.",
            501: "Only GET and HEAD are supported.", 505: "This HTTP version is not supported."}


class _Fail(Exception):
    def __init__(self, code, text, headers=()):
        super().__init__(text)
        self.code = code
        self.headers = headers


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"     # keep-alive: every answer carries Content-Length
    server_version = "wolfsdk-mapserver"

    def log_message(self, *args):
        pass                            # the GUI has no console (pythonw: stderr is None)

    def send_error(self, code, message=None, explain=None):
        """The stdlib's own refusals (another method, a broken request line, too
        long a line): one English line, JSON under /models as there. The target
        comes from the raw line: self.path may be the previous request's."""
        self._sent = False
        line = getattr(self, "raw_requestline", b"").decode("latin-1").split()
        self._json = len(line) > 1 and bool(_JSON_ERRORS.match(unquote(line[1].partition("?")[0])))
        self.close_connection = True
        extra = (("Allow", "GET, HEAD"),) if code == 501 else ()
        self._text(code, _REFUSED.get(code, "Request refused (%d)." % code), self.command == "HEAD", extra)

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_POST(self):
        """Only models/export (the viewer's own skin PNG) and <kind>/replace (the
        replacement file) take a body; every other target answers as without
        this method: 501. The body is read only after its checks."""
        self.close_connection = True    # one POST per connection: a body left unread cannot spoil the next request
        path = unquote(self.path.partition("#")[0].partition("?")[0])
        if not _POSTS.match(path):
            return self.send_error(501)
        self._sent, self._json = False, True
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            n = -1
        if _EXPORT.match(path):
            if not 0 <= n <= modelexport.MAX_SKIN:
                return self._text(413 if n > modelexport.MAX_SKIN else 411,
                                  "The custom skin is too large (at most %d MB)." % (modelexport.MAX_SKIN >> 20)
                                  if n > 0 else "Content-Length is missing: send the skin as a PNG body.", False)
            return self.do_GET(body=self.rfile.read(n))
        if self.headers.get("X-Wolfsdk-Studio") != "1":
            return self._text(403, "Replacing works from the Studio page only (header X-Wolfsdk-Studio missing).", False)
        most = studiomod.MAX_TEXT if "/texts/" in path else studiomod.MAX_OPS if "/scripts/" in path else studiomod.MAX_PNG
        most = {"sounds": studiomod.MAX_WAV, "videos": studiomod.MAX_VIDEO}.get(path.split("/")[3], most)
        if not 0 < n <= most:
            return self._text(413 if n > most else 411, "The file is too large (at most %d MB)." % (most >> 20)
                              if n > most else "Content-Length is missing or 0: send the file as the body.", False)
        self.do_GET(body=self.rfile.read(n))

    def do_GET(self, head=False, body=None):
        self._sent = False
        self._body = body               # the POST body of models/export, None for GET/HEAD
        self._json = bool(_JSON_ERRORS.match(unquote(self.path.partition("?")[0])))
        try:
            if self.headers.get("Host") not in self.server.hosts:
                raise _Fail(403, "Only for %s." % self.server.hosts[0])
            # origin-form only: urlsplit would read a leading // as a host name
            raw, _, qs = self.path.partition("#")[0].partition("?")
            path, query = unquote(raw), parse_qs(qs)
            if path == "/support":      # the page itself stays free of external URLs
                raise _Fail(302, "See " + SUPPORT_URL, (("Location", SUPPORT_URL),))
            if path.startswith("/api/"):
                self._api(path, query, head)
            else:
                self._static(path, head)
        except _Fail as exc:
            self._text(exc.code, str(exc), head, exc.headers)
        except studio.StudioError as exc:
            self._text(exc.status, str(exc), head)
        except (KeyError, TncImageError) as exc:
            self._text(404, str(exc.args[0]) if exc.args else "Not found.", head)
        except tnomaptex.TnoTexError as exc:
            self._text(422, "Texture cannot be shown: %s" % exc, head)
        except (ConnectionError, TimeoutError):
            self.close_connection = True   # the browser went away mid-answer
        except Exception as exc:  # noqa: BLE001 -- a request must never take the server down
            self._text(500, "%s: %s" % (type(exc).__name__, exc), head)

    # -- answers -------------------------------------------------------------

    def _head(self, code, ctype, length, extra=()):
        self._sent = True
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in extra or (("Cache-Control", "no-cache"),):
            self.send_header(k, v)
        self.end_headers()

    def _range(self, size):
        """(start, end inclusive) of the one Range asked for, or None for all
        of it: no header, another unit, or several ranges (a 200 is allowed).
        Raises 416 for a range that is malformed or lies past the end."""
        spec = self.headers.get("Range")
        if not spec or not spec.strip().lower().startswith("bytes") or "," in spec:
            return None
        m = _RANGE.match(spec)
        a, b = (m.group(1), m.group(2)) if m else ("", "")
        if a:
            start, end = int(a), min(int(b), size - 1) if b else size - 1
            ok = start < size and (not b or int(b) >= start)
        else:
            start, end = max(0, size - int(b or 0)), size - 1
            ok = bool(b) and int(b) > 0 and size > 0
        if not ok:
            raise _Fail(416, "Range %s lies outside the %d bytes." % (spec.strip(), size),
                        (("Content-Range", "bytes */%d" % size),))
        return start, end

    def _bytes(self, data, ctype, head, extra=()):
        r = self._range(len(data))
        extra = tuple(extra or (("Cache-Control", "no-cache"),)) + (("Accept-Ranges", "bytes"),)
        if r is None:
            self._head(200, ctype, len(data), extra)
        else:
            self._head(206, ctype, r[1] - r[0] + 1,
                       extra + (("Content-Range", "bytes %d-%d/%d" % (r[0], r[1], len(data))),))
            data = memoryview(data)[r[0]:r[1] + 1]
        if not head:
            self.wfile.write(data)

    def _file(self, path, ctype, head, more=()):
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            r = self._range(size)
            extra = (("Cache-Control", "no-cache"), ("Accept-Ranges", "bytes")) + tuple(more)
            if r is None:
                self._head(200, ctype, size, extra)
                left = size
            else:
                self._head(206, ctype, r[1] - r[0] + 1,
                           extra + (("Content-Range", "bytes %d-%d/%d" % (r[0], r[1], size)),))
                f.seek(r[0])
                left = r[1] - r[0] + 1
            while not head and left:
                chunk = f.read(min(left, 1 << 20))
                if not chunk:
                    raise OSError("%s got shorter while being read" % Path(path).name)
                self.wfile.write(chunk)
                left -= len(chunk)

    def _stream(self, ctype, stream, head):
        """A body made while it is sent (WVF1 frames): no Range, no caching."""
        try:
            self._head(200, ctype, stream.length, (("Cache-Control", "no-store"),))
            if not head:
                self.wfile.write(stream.header)
                for chunk in stream:
                    self.wfile.write(chunk)
        finally:
            stream.close()

    def _text(self, code, text, head, extra=()):
        if self._sent:                  # failed mid-file: the only honest signal left is to hang up
            self.close_connection = True
            return
        text = studio._english(text)    # library errors (tncimage, tnccrypt, ...) still speak German
        body = (text + "\n").encode("utf-8")
        ctype = "text/plain; charset=utf-8"
        if getattr(self, "_json", False):
            body, ctype = studio.dumps({"error": text}), "application/json"
        try:
            self._head(code, ctype, len(body),
                       (("Cache-Control", "no-cache"),) + tuple(extra))
            if not head:
                self.wfile.write(body)
        except (ConnectionError, TimeoutError):
            self.close_connection = True

    # -- routes --------------------------------------------------------------

    def _static(self, path, head):
        rel = path[1:] or "index.html"
        parts = rel.split("/")
        # no drive letters, UNC, streams, backslashes or dot segments; then
        # the resolved file must still lie inside the viewer folder
        if ":" in rel or "\\" in rel or "\0" in rel or any(p in ("", ".", "..") for p in parts):
            raise _Fail(404, "Not found.")
        root = self.server.static.resolve()
        try:
            target = root.joinpath(*parts).resolve()
        except (OSError, ValueError):
            raise _Fail(404, "Not found.")
        if root not in target.parents or not target.is_file():
            raise _Fail(404, "Not found.")
        self._file(target, TYPES.get(target.suffix.lower(), "application/octet-stream"), head)

    def _api(self, path, query, head):
        srv = self.server
        if path in ("/api/maps", "/api/tnc/maps"):
            return self._bytes(srv.maps_json(), "application/json", head)
        if path == "/api/tno/maps":
            return self._bytes(srv.tno_maps_json(), "application/json", head)
        if path == "/api/yb/maps":
            raise _Fail(404, "Maps of Wolfenstein: Youngblood are not supported yet.")
        if path == "/api/games":
            return self._bytes(studio.dumps(srv.studio.games()), "application/json", head)
        m = _STUDIO.match(path)
        if m:
            return self._studio(*m.groups(), query, head)
        m = _MODS.match(path)
        if m:
            srv.studio.root(m.group(1))         # an unknown game: 404
            return self._bytes(studio.dumps(studiomod.studio_mods(srv.mods_dir, m.group(1))), "application/json", head)
        m = _MAP.match(path)
        if m and m.group(1) == "tno":
            root, map_id, leaf = srv.studio.root("tno"), m.group(2), m.group(3)
            if leaf == "collision.json":
                return self._file(tnomapcoll.collision(root, map_id), "application/json", head)
            _doc, geo = tnomap.scene(root, map_id)
            return self._file(geo.with_name("scene.json") if leaf == "scene.json" else geo,
                              "application/json" if leaf == "scene.json" else "application/octet-stream", head)
        if m:
            _game, map_id, leaf = m.groups()
            if leaf == "collision.json":
                return self._file(mapcoll.collision(srv.game_root, map_id), "application/json", head)
            _doc, geo = mapgeo.scene(srv.game_root, map_id)
            if leaf == "scene.json":
                srv.mount(map_id)           # the textures of this map come next
                return self._file(geo.with_name("scene.json"), "application/json", head)
            return self._file(geo, "application/octet-stream", head)
        m = _TEX.match(path)
        if m:
            game, tid, png = m.groups()
            try:
                side = int(query.get("max", ["1024"])[-1])
            except ValueError:
                raise _Fail(400, "max must be a whole number.")
            # bounded and a power of two: every value is another cache file in maptex
            side = 1 << (max(16, min(4096, side)).bit_length() - 1)
            if game == "tno":
                fn = tnomaptex.texture_png if png else tnomaptex.texture_block
                data = fn(srv.studio.root("tno"), tid, side)
            else:
                fn = maptex.texture_png if png else maptex.texture_block
                data = srv.texture(fn, tid, side)
            return self._bytes(data, "image/png" if png else "application/octet-stream", head,
                               (("Cache-Control", "max-age=86400"),))   # ids are content-addressed
        raise _Fail(404, "Unknown endpoint: %s" % path)

    def _studio(self, game, kind, leaf, query, head):
        st = self.server.studio

        def arg(name, default=None):
            return query.get(name, [default])[-1]

        def num(name, default=None):
            v = arg(name)
            if v is None or v == "":
                return default
            try:
                return int(v)
            except ValueError:
                raise _Fail(400, "%s must be a whole number." % name)

        json_ = "application/json"
        if leaf is None:
            return self._bytes(studio.dumps(st.listing(
                game, kind, arg("q", ""), arg("group"), arg("lang"), num("offset", 0),
                num("limit", 100), arg("facets") in ("1", "true"), arg("find"))), json_, head)
        if leaf not in _LEAVES[kind]:
            raise _Fail(404, "Unknown endpoint: %s/%s" % (kind, leaf))
        if kind == "scripts":
            return self._scripts(game, leaf, query, head)
        if leaf == "export.zip":
            rows = st.export_rows(game, kind, arg("q", ""), arg("group"), arg("lang"))
            if arg("check") == "1":
                return self._bytes(studio.dumps({"count": len(rows), "bytes": sum(r.get("size") or 0 for r in rows)}),
                                   json_, head)
            return self._zip(st, game, kind, rows, "%s_%s%s" % (game, kind, "_" + arg("q") if arg("q") else ""), head)
        item = arg("id")
        if leaf in ("replace", "file") or (leaf == "export" and kind == "textures"):
            if not item:
                raise _Fail(400, "id= is missing.")
            if leaf == "replace" and self._body is None:
                return self._bytes(studio.dumps(studiomod.replace_info(st, game, kind, item, arg("skin"))), json_, head)
            if leaf == "replace":
                return self._bytes(studio.dumps(studiomod.replace(
                    st, game, kind, item, self._body, arg("name"), arg("folder"), arg("author", ""),
                    arg("blood", "1") != "0", arg("skin"), self.server.mods_dir)), json_, head)
            path, data = st.export_file(game, kind, item)
            more = (("Content-Disposition", _attachment(path.rsplit("/", 1)[-1])),)
            if isinstance(data, Path):
                return self._file(data, "application/octet-stream", head, more)
            return self._bytes(data, "image/png" if kind == "textures" else "application/octet-stream", head,
                               (("Cache-Control", "no-store"),) + more)
        if not item:
            raise _Fail(400, "id= is missing.")
        if leaf == "info":
            return self._bytes(studio.dumps(st.info(game, kind, item)), json_, head)
        if leaf == "frames":
            return self._stream("application/octet-stream", st.frames(
                game, item, num("start", 0), num("count"), num("w", 960), arg("seek", "auto")), head)
        if leaf == "frame.png":
            return self._bytes(st.video_png(game, item, num("n"), num("w", 480)), "image/png", head)
        if leaf == "audio.wav":
            raw = arg("tracks")
            try:
                tracks = [int(t) for t in raw.split(",")] if raw else None
            except ValueError:
                raise _Fail(400, "tracks must be a list of track ids, e.g. 0,1,3.")
            return self._bytes(st.video_wav(game, item, tracks), "audio/wav", head)
        if leaf == "audio":
            mime, data = st.sound(game, item)
            return self._bytes(data, mime, head)
        if leaf == "image.png":
            return self._bytes(st.texture_png(game, item, num("max", 512)), "image/png", head)
        if leaf == "mesh.bin":
            return self._bytes(st.model_mesh(game, item, num("lod", 0)), "application/octet-stream", head)
        if leaf == "albedo.png":
            return self._bytes(st.model_albedo(game, item, num("max", studio.ALBEDO_MAX)), "image/png", head,
                               (("Cache-Control", "max-age=86400"),))   # v= changes when an archive or .texdb does
        if leaf == "uv2.bin":
            return self._bytes(st.model_uv2(game, item, num("surface", 0)), "application/octet-stream", head,
                               (("Cache-Control", "max-age=86400"),))
        if leaf == "anims.json":
            return self._bytes(studio.dumps(st.model_anims(game, item)), json_, head)
        if leaf == "skeleton.json":
            return self._bytes(studio.dumps(st.model_skeleton(game, item)), json_, head)
        if leaf == "skin.bin":
            return self._bytes(st.model_skin(game, item), "application/octet-stream", head)
        if leaf == "anim.json":     # anim = md6anim name
            return self._bytes(st.model_anim(game, item, arg("anim", "")), json_, head)
        if leaf == "pbr.json":      # id = material name
            return self._bytes(studio.dumps(st.model_pbr(game, item)), json_, head, (("Cache-Control", "max-age=86400"),))
        if leaf == "pbr.png":
            return self._bytes(st.model_pbr_png(game, item, arg("slot", "")), "image/png", head,
                               (("Cache-Control", "max-age=86400"),))
        if leaf == "export":
            raw, skin = arg("surfaces"), arg("skin")
            try:
                surfaces = None if raw is None else [int(s) for s in raw.split(",") if s.strip()]
            except ValueError:
                raise _Fail(400, "surfaces must be a list of surface numbers, e.g. 0,2,5.")
            if (skin is None) != (self._body is None):
                raise _Fail(400, "A custom skin comes by POST: skin=<material> in the address, the PNG as the body.")
            name, data, mime = modelexport.export(st, game, item, arg("format", "glb"), surfaces,
                                                  None if skin is None else (skin, self._body),
                                                  num("max", modelexport.FULL_TEX))
            return self._bytes(data, mime, head, (("Cache-Control", "no-store"),
                                                  ("Content-Disposition", 'attachment; filename="%s"' % name)))
        return self._bytes(st.text(game, item).encode("utf-8"), "text/plain; charset=utf-8", head)

    def _scripts(self, game, leaf, query, head):
        """scripts/info (GET, mod= shows it as in that Studio mod), scripts/nodetypes (GET) and
        scripts/save (POST: {ops: [...]}), see studiomod.script_info and save_script."""
        st = self.server.studio

        def arg(name, default=None):
            return query.get(name, [default])[-1]

        st.catalog(game, studio.SCRIPTS)            # TNO: 422, an unknown game: 404
        if leaf == "nodetypes":
            return self._bytes(studio.dumps(kiscule_types()), "application/json", head)
        if leaf == "info":
            if not arg("id"):
                raise _Fail(400, "id= is missing.")
            return self._bytes(studio.dumps(studiomod.script_info(
                st, game, arg("id"), arg("mod"), self.server.mods_dir)), "application/json", head)
        if self._body is None:
            raise _Fail(405, "Saving is a POST: the body is {\"ops\": [...]}.", (("Allow", "POST"),))
        if not arg("id"):
            raise _Fail(400, "id= is missing.")
        try:
            ops = json.loads(self._body.decode("utf-8")).get("ops")
        except (ValueError, UnicodeDecodeError, AttributeError):
            raise _Fail(400, "The body must be JSON: {\"ops\": [...]}.")
        return self._bytes(studio.dumps(studiomod.save_script(
            st, game, arg("id"), ops, arg("name"), arg("folder"), arg("author", ""), self.server.mods_dir)),
            "application/json", head)

    def _zip(self, st, game, kind, rows, stem, head):
        """Every row's export (studio.export_file) as one ZIP, written while it is made:
        no Content-Length, the connection closes after it. An item that fails is left
        out and named in export-errors.txt."""
        self._sent = self.close_connection = True
        self.send_response(200)
        for k, v in (("Content-Type", "application/zip"), ("Content-Disposition", _attachment(stem + ".zip")),
                     ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"), ("Connection", "close")):
            self.send_header(k, v)
        self.end_headers()
        if head:
            return
        seen, failed = set(), []
        method = zipfile.ZIP_DEFLATED if kind == "texts" else zipfile.ZIP_STORED   # PNG, Ogg, Bink: packed already
        with zipfile.ZipFile(self.wfile, "w", method) as z:
            for r in rows:
                try:
                    path, data = st.export_file(game, kind, r["id"])
                except studio.StudioError as exc:
                    failed.append("%s: %s" % (r["id"], exc))
                    continue
                stem_, dot, ext = path.rpartition(".") if "." in path.rsplit("/", 1)[-1] else (path, "", "")
                n = 1
                while path.lower() in seen:
                    n += 1
                    path = "%s_%d%s%s" % (stem_, n, dot, ext)
                seen.add(path.lower())
                if isinstance(data, Path):
                    z.write(data, path)
                else:
                    z.writestr(zipfile.ZipInfo(path, time.localtime()[:6]), data, method)
            if failed:
                z.writestr("export-errors.txt", "\n".join(failed) + "\n")


def kiscule_types():
    """docs/kiscule_nodes.json without the templates add_node builds from; `initial` is what a new node shows."""
    return [dict({k: v for k, v in t.items() if k != "template"}, initial=kiscule.initial_params(t))
            for t in kiscule.nodetypes().values()]


def _attachment(name):
    """Content-Disposition for a download named `name` (ASCII fallback plus UTF-8)."""
    plain = re.sub(r'[^A-Za-z0-9._()\- ]+', "_", name) or "export"
    return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (plain, quote(name, safe=""))


class MapServer(ThreadingHTTPServer):
    daemon_threads = True             # a 100 MB download must not hold up the loader's exit
    allow_reuse_address = False       # port 0 needs no reuse; on Windows it would allow a second bind

    def __init__(self, game_root, static=MAPVIEW, tno_root=None, mods_dir=studiomod.MODS):
        self.game_root = Path(game_root)
        self.static = Path(static)
        self.mods_dir = Path(mods_dir)    # where 'Replace' saves its mods (a temp folder in the verifier)
        # the other game's root is looked up on first use (config, Steam)
        self.studio = studio.Studio({"tnc": self.game_root, "tno": tno_root})
        self._mounts = OrderedDict()
        self._lock = threading.Lock()
        self._open_lock = threading.Lock()
        self._maps = (None, b"")
        self._tno_maps = (None, b"")
        self._maps_lock = threading.Lock()
        super().__init__((HOST, 0), Handler)
        port = self.server_address[1]
        self.hosts = ("%s:%d" % (HOST, port), "localhost:%d" % port)
        self.url = "http://%s:%d/" % (HOST, port)

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return                      # a browser tab closed while its keep-alive socket waited
        super().handle_error(request, client_address)

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # TCPServer's, not HTTPServer's: that one asks reverse DNS for a name (getfqdn)
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = HOST, self.server_address[1]

    def maps_json(self):
        """/api/maps, rebuilt only when a spec or an archive changed (maps() takes 1-3 s)."""
        root = self.game_root
        files = [root / "base" / "packagemapspec.json", *root.glob("base/*.resources"),
                 *root.glob("dlc/dlc_*/base/*")]
        key = sorted((str(p), st.st_size, st.st_mtime_ns) for p in files for st in [p.stat()])
        with self._maps_lock:
            if self._maps[0] != key:
                out = [{k: m[k] for k in ("id", "title", "group", "entities_bytes")}
                       for m in mapcatalog.maps(root) if m["entities"]]
                self._maps = (key, json.dumps(out, ensure_ascii=False).encode("utf-8"))
            return self._maps[1]

    def tno_maps_json(self):
        """/api/tno/maps in the shape of /api/maps, rebuilt when an archive changed."""
        root = self.studio.root("tno")
        key = studio._stamp([p for pair in tnomap._pairs(root) for p in pair])
        with self._maps_lock:
            if self._tno_maps[0] != key:
                out = [{k: m[k] for k in ("id", "title", "group", "entities_bytes")}
                       for m in tnomap.maps(root) if m["entities"]]
                self._tno_maps = (key, json.dumps(out, ensure_ascii=False).encode("utf-8"))
            return self._tno_maps[1]

    def mount(self, map_id):
        """The map's open Mount; the last MOUNTS maps stay open for /api/tex."""
        with self._open_lock:           # ponytail: one map opens at a time (1-2 s each)
            with self._lock:
                m = self._mounts.get(map_id)
            if m is None:
                m = mapcatalog.mount(self.game_root, map_id)
                # mapcatalog builds its name index lazily and not thread-safely:
                # build it here, before any other thread can see this Mount
                m.find("", "")
            with self._lock:
                self._mounts[map_id] = m
                self._mounts.move_to_end(map_id)
                while len(self._mounts) > MOUNTS:
                    self._mounts.popitem(last=False)   # closed once the last request lets go
        return m

    def texture(self, fn, tid, side):
        with self._lock:
            mounts = list(reversed(self._mounts.values()))
        for m in mounts:
            try:
                return fn(m, tid, side)
            except KeyError:
                continue
        raise KeyError("Texture %s belongs to no open map." % tid)

    def server_close(self):
        super().server_close()
        with self._lock:
            mounts, self._mounts = list(self._mounts.values()), OrderedDict()
        for m in mounts:
            m.close()
        self.studio.close()


# -- one server per process ---------------------------------------------------

_server = None
_server_lock = threading.Lock()


def start(game_root):
    """URL of this process's server, started on first use (background thread)."""
    global _server
    with _server_lock:
        if _server is not None and _server.game_root != Path(game_root):
            _stop()
        if _server is None:
            _server = MapServer(game_root)
            threading.Thread(target=_server.serve_forever, name="mapserver", daemon=True).start()
            atexit.register(stop)
        return _server.url


def _stop():
    global _server
    if _server is not None:
        _server.shutdown()
        _server.server_close()
        _server = None


def stop():
    with _server_lock:
        _stop()


def url_for(base, map_id=None):
    """The viewer URL; with a map id the frontend opens that map (?map=)."""
    return base + ("?map=" + quote(map_id, safe="/") if map_id else "")


# -- the app window -------------------------------------------------------------

def _candidates():
    env = os.environ.get
    for name, rel in (("edge", r"Microsoft\Edge\Application\msedge.exe"),
                      ("chrome", r"Google\Chrome\Application\chrome.exe")):
        for var in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
            if env(var):
                yield name, Path(env(var), rel)


def browser_command(url):
    """argv that opens url as a fullscreen app window (Edge, else Chrome), or None.
    Its own profile, so it is a window of its own instead of a tab in the
    user's browser."""
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    for name, exe in _candidates():
        if exe.is_file():
            return [str(exe), "--app=" + url, "--start-fullscreen",
                    "--user-data-dir=" + str(local / "wolfsdk" / ("%s-profile" % name)),
                    "--no-first-run", "--no-default-browser-check"]
    return None


def open_app(url):
    """Show the viewer. Returns how: 'edge'/'chrome' path or 'webbrowser'."""
    cmd = browser_command(url)
    if cmd:
        try:
            subprocess.Popen(cmd, close_fds=True)
            return cmd[0]
        except OSError:
            pass                        # found but not startable: the default browser still works
    webbrowser.open(url)
    return "webbrowser"
