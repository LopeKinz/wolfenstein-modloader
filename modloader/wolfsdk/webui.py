"""The loader window as a local web page (wolfsdk/loaderui/) in an Edge/Chrome app window.

tkinter cannot draw a modern interface -- no rounded corners, no shadows, no
transitions -- so the window is a page served by this process, like the
Studio. Python still does all the work, with the same modules as the old
tkinter window (gui.py): mod, cvars, mapinstall, game. Nothing runs in the
browser but the page.

    python -m wolfsdk gui              this window
    python -m wolfsdk gui --classic    the tkinter window (gui.py)

127.0.0.1 on a free port. The Host header must be ours (no DNS rebinding),
and every POST needs the header X-Wolfsdk-Loader: 1: a foreign page cannot
send it without a CORS preflight, which this server never answers.

  GET  /api/state                 install, status, counts, sync, busy, last job
  GET  /api/mods                  this title's mods with their tick
  GET  /api/tweaks                this title's cvar catalogue with the values
  GET  /api/info                  console commands, key bindings, notes
  POST /api/game {key}            switch title         POST /api/folder     folder dialog
  POST /api/mods {id, on} | {all: true|false}         POST /api/reload     rescan mods/
  POST /api/tweak {key, value}    one cvar, "" = engine default
  POST /api/extra {text}          the custom cvar lines
  POST /api/preset {name, reset}  a The New Colossus preset
  POST /api/check | /api/apply | /api/revert          background job (one at a time)
  POST /api/launch {apply?}       start; answers {need_apply: true} while the ticks are not in the game
  POST /api/map/install | /api/map/uninstall | /api/studio | /api/open {what: mods|support}
  POST /api/bye                   the page is closing
  POST /api/setting {update_check} | /api/update/dismiss {version}

Update notifier: the one connection to the internet. Once per start (at most
every UPDATE_EVERY, cached in %LOCALAPPDATA%\\wolfsdk\\update.json) this process
asks GitHub for the releases of REPO and takes the newest non-draft one tagged
wolfsdk-X.Y.Z -- the repo also carries the SDK packages' v* releases, so
"latest release" would be the wrong question. Off with config.json
"update_check": false; a dismissed version stays quiet until the next one.

Ticks and values are saved as they change, in the same config.json keys the
tkinter window and `wolfsdk play` use (enabled_mods, tweaks_by_title,
extra_cvars).

The process ends when its windows are gone: the browser processes it started
have exited, or -- when it could not track one (default browser, a second
window handed to a running Edge) -- no page has called in for LINGER seconds.
"""

import importlib.util
import json
import os
import subprocess
import sys
import threading
import re
import time
import traceback
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import SUPPORT_URL, __version__, cvars, mapinstall, mod as modlib
from .game import Game, GameError, load_config, save_config, steam_is_running
from .patch import PatchError
from .tncpatch import TncPatchError

ROOT = Path(__file__).resolve().parent.parent
MODS_DIR = ROOT / "mods"
UI = Path(__file__).resolve().parent / "loaderui"
HOST = "127.0.0.1"
LINGER = 150            # s without a call before an untracked page counts as closed (hidden tabs poll 1/min)
HANDOFF = 8             # s: a browser process that exits this fast handed its window to one already running
WINDOW = (1280, 860)
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png"}
FAILED = ("Error", "Refused", "Cancelled", "Check failed", "Backup not ready", "Recovery needed",
          "Start failed", "Installation aborted")
REPO = "LopeKinz/wolfenstein-modloader"
RELEASES = "https://api.github.com/repos/%s/releases?per_page=30" % REPO
RELEASE_PAGE = "https://github.com/%s/releases/" % REPO
TAG = "wolfsdk-"
UPDATE_EVERY = 6 * 3600


def version_tuple(text):
    """'0.10.2' -> (0, 10, 2); None for anything that is not plain dotted numbers."""
    m = re.fullmatch(r"v?(\d+(?:\.\d+){0,3})", (text or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def newest_release(releases):
    """The newest published wolfsdk-X.Y.Z release in a GitHub /releases answer, or None."""
    best = None
    for rel in releases if isinstance(releases, list) else []:
        tag = str(rel.get("tag_name") or "")
        v = version_tuple(tag[len(TAG):]) if tag.startswith(TAG) else None
        url = str(rel.get("html_url") or "")
        if v is None or rel.get("draft") or rel.get("prerelease") or not url.startswith(RELEASE_PAGE):
            continue
        if best is None or v > best[0]:
            best = (v, {"version": ".".join(map(str, v)), "url": url, "name": str(rel.get("name") or tag),
                        "published": str(rel.get("published_at") or "")[:10],
                        "notes": str(rel.get("body") or "")[:4000]})
    return best[1] if best else None


def fetch_releases(timeout=5):
    req = urllib.request.Request(RELEASES, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "WolfSDK/%s" % __version__})
    with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 -- fixed https URL
        return json.loads(r.read(1 << 22).decode("utf-8"))


class Problem(Exception):
    """A refusal the page shows as it is (409)."""


def _tone(text, ok=True):
    if not ok or text.startswith(FAILED):
        return "danger"
    return "warn" if text.startswith("Your selection is out of sync") or "Conflict" in text else "ok"


def _ask(kind, **options):
    """A native file or folder dialog, on top of the app window. Tk only lives
    for the dialog, in the calling thread."""
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        ask = filedialog.askdirectory if kind == "folder" else filedialog.askopenfilename
        return ask(parent=root, **options) or ""
    finally:
        root.destroy()


class Loader:
    """Everything the window can do, without the window. Thread-safe."""

    def __init__(self, game_path=None):
        self.lock = threading.RLock()
        self.dialog = threading.Lock()   # one native dialog at a time
        self.config = load_config()
        self.installs = {g.title.key: g for g in Game.find_all()}
        self.game = None
        self.values = {}                 # title key -> {cvar: value}, catalogue cvars only
        self.preset_undo = {}
        self.busy = False
        self.job = {"id": 0, "kind": "", "done": True, "ok": True, "tone": "ok", "text": ""}
        self.note = ""
        self.rev = 0                     # every change; the sync answer is recomputed per rev
        self.catalog_rev = 0             # title switch or mod rescan: the page refetches its lists
        self._sync = (-1, None)
        self.procs = []                  # [(Popen, started)]
        self.latest = None               # newest_release() once the update check has run
        self.all_mods, self.mods, self.mod_errors = [], [], []
        try:
            self.resolve(game_path)
        except GameError:
            self.reload_mods()

    # -- install -------------------------------------------------------------

    @property
    def key(self):
        return self.game.title.key if self.game else "tno"

    def resolve(self, path=None, title=None):
        game = Game.find(path, title) if title else Game.find(path)
        game.remember()                  # writes config.json itself: read it back before saving ours
        with self.lock:
            self.config = load_config()
            self.installs[game.title.key] = game
            self.game = game
            self.reload_mods()

    def switch(self, key):
        game = self.installs.get(key)
        if game is None:
            raise Problem("This game is not installed.")
        self.resolve(str(game.root))

    def choose_folder(self):
        with self.dialog:
            chosen = _ask("folder", title="Choose the Wolfenstein install folder")
        if not chosen:
            return {"chosen": False}
        try:
            self.resolve(chosen)
        except GameError as exc:
            raise Problem(str(exc))
        return {"chosen": True}

    def install(self):
        return modlib.installation_for(self.game)

    def bump(self, catalog=False):
        self.rev += 1
        self.catalog_rev += catalog

    def save(self):
        if self.game:
            self.config["game_path"] = str(self.game.root)
        save_config(self.config)
        self.bump()

    # -- mods ----------------------------------------------------------------

    def reload_mods(self):
        with self.lock:
            self.all_mods, errors = modlib.discover(MODS_DIR)
            self.mods = modlib.for_title(self.all_mods, self.key)
            self.mod_errors = list(errors)
            self.bump(catalog=True)

    def enabled_ids(self):
        return set(self.config.get("enabled_mods", []))

    def enabled_mods(self):
        on = self.enabled_ids()
        return [m for m in self.mods if m.id in on]

    def set_mods(self, ids, on):
        with self.lock:
            known = {m.id for m in self.all_mods}
            ticks = self.enabled_ids() & known   # a mod whose folder is gone drops out
            ticks = ticks | set(ids) if on else ticks - set(ids)
            self.config["enabled_mods"] = sorted(ticks)
            self.save()
            return {"active": len(self.enabled_mods()), "total": len(self.mods)}

    def mods_json(self):
        on = self.enabled_ids()
        return {"rev": self.catalog_rev, "errors": self.mod_errors, "mods": [
            {"id": m.id, "name": m.name, "version": m.version, "author": m.author,
             "description": m.description, "priority": m.priority, "assets": len(m.assets),
             "cvars": len(m.cvars), "on": m.id in on} for m in self.mods]}

    # -- tweaks --------------------------------------------------------------

    def tweak_values(self):
        """This title's values, read from config.json on first use (old configs: flat "tweaks").
        Keys outside the catalogue stay as saved: `wolfsdk play` passes them on, so a save
        here must not drop them."""
        key = self.key
        if key not in self.values:
            by = self.config.get("tweaks_by_title")
            saved = (dict(by[key]) if isinstance(by, dict) and key in by
                     else dict(self.config.get("tweaks") or {}) if key == "tno" else {})
            self.values[key] = {k: str(v) for k, v in saved.items()}
        return self.values[key]

    def _store_tweaks(self):
        current = {k: v for k, v in self.tweak_values().items() if v}
        by = self.config.get("tweaks_by_title")
        by = by if isinstance(by, dict) else {}
        by[self.key] = current
        self.config["tweaks_by_title"] = by
        if self.key == "tno":            # anything still reading the flat key gets the same
            self.config["tweaks"] = current
        self.save()

    def set_tweak(self, key, value):
        value = str(value).strip()
        if len(value) > 200 or "\n" in value or "\r" in value:
            raise Problem("A cvar value is one short line.")
        with self.lock:
            if key not in {t.key for t in cvars.full_catalog(self.key)}:
                raise Problem("Unknown cvar: %s" % key)
            self.tweak_values()[key] = value
            self._store_tweaks()
            return {"tweaks": len(self.collect_tweaks())}

    def set_extra(self, text):
        if len(text) > 20000:
            raise Problem("The custom cvars are too long.")
        with self.lock:
            self.config["extra_cvars"] = text.strip()
            self.save()
            return {"tweaks": len(self.collect_tweaks())}

    def collect_tweaks(self):
        out = {k: v for k, v in self.tweak_values().items() if v}
        for line in (self.config.get("extra_cvars") or "").splitlines():
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            parts = line.replace("=", " ").split(None, 1)
            if len(parts) == 2:
                out[parts[0]] = parts[1].strip()
        return out

    def settings(self):
        """The tweaks plus the cvars of the ticked mods (mods win, as in `wolfsdk play`)."""
        out = self.collect_tweaks()
        out.update({k: str(v) for m in self.enabled_mods() for k, v in m.cvars.items()})
        return out

    def tweaks_json(self):
        key = self.key
        values = self.tweak_values()
        items = []
        for t in cvars.full_catalog(key):
            flag = tip = ""                  # a short chip on the row, the whole story as its tooltip
            if cvars.blocked_for(key, t.key):
                flag, tip = (("via default.cfg", "Not a retail command: delivered via default.cfg on the next "
                              "apply/start.") if key == "tnc" else
                             ("cheat", "Cheat cvar: also sent as +toggle at start, past the retail gate."))
            elif t.confidence == "low":
                flag, tip = "unverified", "The description was matched to this name with low confidence."
            items.append({"key": t.key, "label": t.label, "group": t.category, "kind": t.kind,
                          "default": t.default, "on": t.value_on, "lo": t.lo, "hi": t.hi,
                          "choices": [list(c) for c in t.choices], "note": t.note, "flag": flag, "tip": tip,
                          "value": values.get(t.key, "")})
        presets = [{"name": n, "count": len(p)} for n, p in cvars.TNC_PRESETS.items()] if key == "tnc" else []
        return {"rev": self.catalog_rev, "title": key, "groups": list(cvars.categories_for(key, full=True)),
                "presets": presets, "extra": self.config.get("extra_cvars", ""), "items": items}

    def preset(self, name, reset=False):
        preset = cvars.TNC_PRESETS.get(name)
        if self.key != "tnc" or not preset:
            raise Problem("No such preset.")
        with self.lock:
            values = self.tweak_values()
            current = {k: v for k, v in values.items() if v}
            if reset:
                new = cvars.reset_preset(current, name, self.preset_undo.pop(name, None))
                msg = "%s reset." % name
            else:
                self.preset_undo.setdefault(name, current)
                new = cvars.apply_preset(current, name)
                args, cfile = cvars.split_tnc(preset)
                msg = ("%s: %d cvars set (%d launch args, %d via default.cfg)."
                       % (name, len(preset), len(args), len(cfile)))
            for k in preset:
                values[k] = new.get(k, "")
            self._store_tweaks()
            self.note = msg
            return {"text": msg, "values": {k: values.get(k, "") for k in preset}}

    # -- state ---------------------------------------------------------------

    def status(self):
        """(tone, title, hint) of the install, as the old overview card."""
        if not self.game:
            return "danger", "No game install found", "Choose the game folder at the top right."
        if not self.game.supports_mods:
            return ("warn", "%s is ready to launch" % self.game.title.name,
                    "Cvars work; archive mods are disabled for this game.")
        install = self.install()
        try:
            journal = install.read_journal()
            if journal and journal.get("state") == "applying":
                return "danger", "Recovery needed", "The last patch run was interrupted. Choose Revert now."
            if journal:
                return ("ok", "Mod set active · backup in place",
                        "%d mod(s) with %d asset(s) are installed in the game."
                        % (len(journal.get("mods", [])), len(journal.get("assets", []))))
            return "ok", "Original state · ready", "No archive mods installed. A check writes nothing."
        except Exception as exc:  # noqa: BLE001
            return "danger", "Could not read the status", str(exc)
        finally:
            install.close()

    def in_sync(self):
        """True when the game already carries exactly the ticked mods (plus, for The New
        Colossus, the tweaks mod whose id hashes what it would write)."""
        if not self.game or not self.game.supports_mods:
            return True
        try:
            install = self.install()
            try:
                to_apply, _args, _refusal = modlib.with_tnc_tweaks(install, self.enabled_mods(), self.settings())
                applied = modlib.applied_ids(install)
            finally:
                install.close()
        except Exception:  # noqa: BLE001 -- cannot tell, so do not nag
            return True
        return applied == {m.id for m in to_apply}

    def sync(self):
        rev = self.rev
        if self._sync[0] != rev:
            self._sync = (rev, self.in_sync())
        return self._sync[1]

    def map_state(self):
        if not (self.game and self.game.title.key == "tnc"):
            return {"available": False, "text": "Custom maps are for Wolfenstein II: The New Colossus only."}
        try:
            st = mapinstall.status(self.game)
            text = mapinstall.describe(st)[1 if st["dlc"] else 0]
            if st["installiert"]:
                text += "  Source: %s" % Path(st["installiert"]["quelle"]).parent.name
            return {"available": True, "text": text, "installed": bool(st["installiert"])}
        except Exception as exc:  # noqa: BLE001
            return {"available": True, "text": "Custom map: could not read the status (%s)" % exc}

    def state(self):
        with self.lock:
            g = self.game
            tone, title, hint = self.status()
            return {
                "version": __version__, "rev": self.rev, "catalog_rev": self.catalog_rev,
                "games": [{"key": k, "name": x.title.name, "engine": x.title.engine, "root": str(x.root)}
                          for k, x in sorted(self.installs.items())],
                "game": g.title.key if g else None, "name": g.title.name if g else "",
                "engine": g.title.engine if g else "", "path": str(g.root) if g else "",
                "supports_mods": bool(g and g.supports_mods),
                "status": {"tone": tone, "title": title, "hint": hint},
                "sync": self.sync() if g else None, "busy": self.busy, "job": self.job, "note": self.note,
                "counts": {"mods": len(self.enabled_mods()), "mods_total": len(self.mods),
                           "tweaks": len(self.collect_tweaks())},
                "map": self.map_state(),
                "studio": importlib.util.find_spec(__package__ + ".mapserver") is not None,
                "update": self.update_state(),
            }

    def info(self):
        return {"console": "Ctrl+^ (Ctrl + the key left of 1), no setting needed.",
                "commands": [list(c) for c in cvars.COMMANDS],
                "binds": [list(b) for b in cvars.TNO_BINDS],
                "notes": [
                    "The New Order: Cheats and QoL change no game file. They are passed as arguments "
                    "when the game starts.",
                    "The New Colossus: cvars the retail console accepts go as start arguments, the rest "
                    "into the game's default.cfg inside the archives ('Revert' removes it). Not yet "
                    "confirmed in game.",
                    "The game is started through Steam. Starting the EXE directly does nothing: the "
                    "SteamStub wrapper hands the start back and exits.",
                    "Mods write into the .resources archives. 'Revert' restores the originals bit for "
                    "bit. Both games have their own archives, their own backups and their own mods.",
                    "Cvar lists come from the engines' own registries (The New Colossus 3,116 settable, "
                    "The New Order 3,186) with the game's own descriptions.",
                ]}

    # -- background jobs -----------------------------------------------------

    def run(self, kind, fn, *args, then=None):
        with self.lock:
            if self.busy:
                raise Problem("Another action is still running.")
            self.busy = True
            self.job = {"id": self.job["id"] + 1, "kind": kind, "done": False, "ok": True, "tone": "", "text": ""}
            self.bump()
            jid = self.job["id"]

        def work():
            ok = True
            try:
                text = fn(*args)
            except Exception as exc:  # noqa: BLE001 -- shown on the page, never lost
                text, ok = "Error: %s" % exc, False
            if ok and then and not text.startswith(FAILED):
                try:
                    text += "\n" + then()
                except GameError as exc:
                    text, ok = text + "\nStart failed: %s" % exc, False
            with self.lock:
                self.job.update(done=True, ok=ok, tone=_tone(text, ok), text=text)
                self.note = text.splitlines()[0] if text else ""
                self.busy = False
                self.bump()

        threading.Thread(target=work, name="loader-" + kind, daemon=True).start()
        return {"job": jid}

    def need_game(self, mods=False):
        if not self.game:
            raise Problem("Please choose the game folder first.")
        if mods and not self.game.supports_mods:
            raise Problem("%s runs on %s. The loader cannot write this game's archives yet - starting "
                          "with cheats and QoL works, though." % (self.game.title.name, self.game.title.engine))

    def check(self):
        self.need_game()
        return self.run("check", self.check_worker, self.enabled_mods())

    def check_worker(self, active):
        """Read-only equivalent of apply: fail before any archive is touched."""
        if not active:
            return "Check OK - no mods selected."
        clashes = modlib.texture_clashes(active)
        if clashes:
            return "\n".join(["Check failed - nothing was changed."] + ["Error     %s" % e for e in clashes])
        install = self.install()
        try:
            journal = install.read_journal()
            if install.read_manifest():
                problems = install.verify() if self.key == "tnc" else install.verify_backup()
                if problems:
                    return "Backup not ready:\n  " + "\n  ".join(problems)
            selected = {m.id for m in active}
            if journal:
                if journal.get("state") == "applying":
                    return "Recovery needed: a patch run was interrupted. Please revert first."
                applied = set(journal.get("mods", []))
                if applied == selected:
                    return ("Check OK - your selection matches the game.\n%d mod(s), %d asset(s); "
                            "the backup is intact." % (len(applied), len(journal.get("assets", []))))
                return ("Your selection is out of sync with the game.\nFor an exact payload check, "
                        "revert first; applying does that automatically.")
            payloads, conflicts, errors = modlib.build_payloads(install, active)
            lines = ["Conflict  %s" % c for c in conflicts] + ["Error     %s" % e for e in errors]
            if errors:
                return "\n".join(["Check failed - nothing was changed."] + lines)
            copies = sum(len(install.find(*k)) for k in payloads)
            head = ["%d mod(s), %d asset(s), %d archive copies checked." % (len(active), len(payloads), copies),
                    "%d conflict(s); higher priority wins." % len(conflicts) if conflicts
                    else "No conflicts. Ready to apply."]
            return "\n".join(head + lines)
        finally:
            install.close()

    def apply(self):
        self.need_game(mods=True)
        studio = "Studio closed (it keeps the game files open).\n" if _close_studio() else ""
        return self.run("apply", lambda: studio + self.apply_worker(self.enabled_mods(), self.settings()))

    def apply_worker(self, active, settings):
        install = self.install()
        try:
            active, _args, refusal = modlib.with_tnc_tweaks(install, active, settings)
            if refusal:
                return "Refused: %s" % refusal
            if not active:               # an empty selection means "no mods": a revert
                if not install.is_modified():
                    return "No mod active - the game is already in its original state."
                modlib.refuse_while_running(install)
                install.revert()
                return "No mod active - the game is back to its original state."
            before = install.is_modified()
            report = modlib.apply_mods(install, active)
            reset = before and not report["ok"] and not install.is_modified()
        finally:
            install.close()
        lines = ["Conflict  %s" % c for c in report["conflicts"]] + ["Error     %s" % e for e in report["errors"]]
        if report["ok"]:
            j = report["journal"]
            lines.insert(0, "%d asset(s) patched." % len(j["assets"]))
            if j["missing"]:
                lines.append("Not found: %s" % ", ".join(j["missing"]))
        else:
            lines.insert(0, "Cancelled - the old mods were reverted, the game is back to its original state."
                         if reset else "Cancelled - nothing was changed.")
        return "\n".join(lines)

    def revert(self):
        self.need_game(mods=True)
        studio = "Studio closed (it keeps the game files open).\n" if _close_studio() else ""
        return self.run("revert", lambda: studio + self.revert_worker())

    def revert_worker(self):
        install = self.install()
        try:
            modlib.refuse_while_running(install)
            install.revert()
            return "The game is back to its original state."
        except (PatchError, TncPatchError, modlib.ModError) as exc:
            return "Error: %s" % exc
        finally:
            install.close()

    def launch(self, apply=None):
        """Without `apply` and with ticks the game does not carry yet: {need_apply}
        -- ticking a mod changes nothing until it is written, and starting the
        previous mod set looks like the loader ignored the click."""
        self.need_game()
        if apply is None and not self.in_sync():
            return {"need_apply": True}
        if apply:
            studio = "Studio closed (it keeps the game files open).\n" if _close_studio() else ""
            return self.run("launch", lambda: studio + self.apply_worker(self.enabled_mods(), self.settings()),
                            then=self.start_game)
        try:
            text = self.start_game()
        except GameError as exc:
            raise Problem("Start failed: %s" % exc)
        with self.lock:
            self.note = text
            self.bump()
        return {"started": True, "text": text}

    def start_game(self):
        settings = self.settings()
        note = ""
        if self.key == "tnc":
            settings, cfile = cvars.split_tnc(settings)
            if cfile:
                note = (" %d more cvar(s) need cfile:default.cfg and only take effect if the mods are applied."
                        % len(cfile))
        elif self.key == "tno":
            dropped = [k for k in settings if cvars.tno_route(k)[1] == "drop"]
            settings = {k: v for k, v in settings.items() if k not in dropped}
            if dropped:
                note = " Left out, not New Order cvars: %s." % ", ".join(dropped)
        self.game.launch(cvars.build_args(settings, cvars.binds_for(self.key), self.key))
        steam = "" if steam_is_running() else " Steam is starting too, this takes a moment."
        return "Start requested through Steam, %d cvar(s).%s%s" % (len(settings), steam, note)

    # -- map, Studio, folders ------------------------------------------------

    def map_install(self):
        self.need_game()
        with self.dialog:
            path = _ask("file", title="Choose the map container (chunk_22.resources)",
                        filetypes=[("Map container", "*.resources"), ("All files", "*.*")])
        if not path:
            return {"chosen": False}
        return self.run("map", self.map_worker, mapinstall.install, self.game, path)

    def map_uninstall(self):
        self.need_game()
        return self.run("map", self.map_worker, mapinstall.uninstall, self.game)

    @staticmethod
    def map_worker(fn, *args):
        closed = _close_studio()
        try:
            note = fn(*args)
        except (modlib.ModError, OSError) as exc:
            note = "Error: %s" % exc
        return note + ("\n\nThe Studio was closed for this; reopen it if you need it." if closed else "")

    def open_studio(self):
        """The Studio server runs in this process (mapserver.py), rooted in Wolfenstein II."""
        game = next((g for g in [self.game, *self.installs.values()] if g and g.title.key == "tnc"), None)
        if game is None:
            raise Problem("Wolfenstein II: The New Colossus was not found. The Studio needs this game as its base.")
        if importlib.util.find_spec(__package__ + ".mapserver") is None:
            raise Problem("The Studio is part of WolfSDK Studio, the separate package for modders.")
        from . import mapserver
        url = mapserver.start(game.root)
        cmd = mapserver.browser_command(url)
        self.track(_popen(cmd) if cmd else None) or webbrowser.open(url)
        with self.lock:
            self.note = "Studio running: %s" % url
            self.bump()
        return {"url": url}

    def track(self, proc):
        if proc is not None:
            self.procs.append((proc, time.time()))
        return proc

    def open(self, what):
        if what == "mods":
            MODS_DIR.mkdir(parents=True, exist_ok=True)
            os.startfile(str(MODS_DIR))  # noqa: S606 -- opening a folder for the user
        elif what == "support":
            webbrowser.open(SUPPORT_URL)
        elif what == "release":
            url = (self.latest or {}).get("url", RELEASE_PAGE)
            webbrowser.open(url if url.startswith(RELEASE_PAGE) else RELEASE_PAGE)
        else:
            raise Problem("Nothing to open: %s" % what)
        return {}

    # -- update notifier -----------------------------------------------------

    def check_updates(self, fetch=fetch_releases, now=None, cache_file=None):
        """Fill self.latest from the cache, or from GitHub when the cache is older
        than UPDATE_EVERY or was written by another version. Never raises: no
        network is no news."""
        if self.config.get("update_check") is False:
            return
        path = cache_file or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "wolfsdk" / "update.json"
        now = time.time() if now is None else now
        try:
            cache = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            cache = {}
        fresh = (isinstance(cache, dict) and cache.get("for") == __version__
                 and 0 <= now - float(cache.get("checked") or 0) < UPDATE_EVERY)
        if not fresh:
            try:
                cache = {"checked": now, "for": __version__, "latest": newest_release(fetch())}
            except Exception:  # noqa: BLE001 -- offline, rate limit, GitHub down
                return
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(cache), "utf-8")
            except OSError:
                pass
        latest = cache.get("latest")
        with self.lock:
            self.latest = latest if isinstance(latest, dict) and str(latest.get("url", "")).startswith(RELEASE_PAGE) else None
            self.bump()

    def update_state(self):
        latest, cur = self.latest, version_tuple(__version__)
        new = version_tuple((latest or {}).get("version"))
        available = bool(new and cur and new > cur)
        return {"enabled": self.config.get("update_check") is not False, "current": __version__,
                "available": available, "latest": latest if available else None,
                "dismissed": available and self.config.get("update_dismissed") == latest["version"]}

    def setting(self, body):
        with self.lock:
            if "update_check" in body:
                self.config["update_check"] = bool(body["update_check"])
                if not body["update_check"]:
                    self.latest = None
            self.save()
        if self.config.get("update_check") is not False and self.latest is None:
            threading.Thread(target=self.check_updates, name="loader-update", daemon=True).start()
        return {}

    def dismiss_update(self, version):
        with self.lock:
            self.config["update_dismissed"] = str(version)[:20]
            self.save()
        return {}


def _close_studio():
    """Stop this process's Studio server if it runs: it keeps archives open, and
    Windows then refuses to swap them. True when it was running."""
    server = sys.modules.get(__package__ + ".mapserver")
    if server is None or getattr(server, "_server", None) is None:
        return False
    server.stop()
    return True


# -- HTTP -------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "wolfsdk-loader"

    def log_message(self, *args):
        pass                            # pythonw: no console

    def do_GET(self):
        self._serve(None)

    def do_POST(self):
        self.close_connection = True
        if self.headers.get("X-Wolfsdk-Loader") != "1":
            return self._json({"error": "Only from the loader page."}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 <= n <= 1 << 20:
            return self._json({"error": "Body too large."}, 413)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return self._json({"error": "The body must be a JSON object."}, 400)
        self._serve(body)

    def _serve(self, body):
        srv = self.server
        if self.headers.get("Host") not in srv.hosts:
            return self._json({"error": "Only for %s." % srv.hosts[0]}, 403)
        path = self.path.partition("?")[0]
        srv.seen = time.time()
        try:
            if not path.startswith("/api/"):
                return self._static(path) if body is None else self._json({"error": "Not found."}, 404)
            route = (GETS if body is None else POSTS).get(path[5:])
            if route is None:
                return self._json({"error": "Not found."}, 404)
            self._json(route(srv, body))
        except Problem as exc:
            self._json({"error": str(exc)}, 409)
        except GameError as exc:
            self._json({"error": str(exc)}, 409)
        except (ConnectionError, TimeoutError):
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001 -- a request must never take the window down
            _log(traceback.format_exc())
            self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)

    def _send(self, code, ctype, data):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, "application/json; charset=utf-8", json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _static(self, path):
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        p = (UI / rel).resolve()
        if UI.resolve() not in p.parents or not p.is_file() or p.suffix not in TYPES:
            return self._json({"error": "Not found."}, 404)
        self._send(200, TYPES[p.suffix], p.read_bytes())


def _bye(srv, _body):
    srv.bye = time.time()
    return {}


GETS = {
    "state": lambda s, _b: s.loader.state(),
    "mods": lambda s, _b: s.loader.mods_json(),
    "tweaks": lambda s, _b: s.loader.tweaks_json(),
    "info": lambda s, _b: s.loader.info(),
}
POSTS = {
    "game": lambda s, b: (s.loader.switch(str(b.get("key"))), {})[1],
    "folder": lambda s, _b: s.loader.choose_folder(),
    "mods": lambda s, b: s.loader.set_mods([m.id for m in s.loader.mods], bool(b["all"])) if "all" in b
    else s.loader.set_mods([str(b.get("id"))], bool(b.get("on"))),
    "reload": lambda s, _b: (s.loader.reload_mods(), s.loader.mods_json())[1],
    "tweak": lambda s, b: s.loader.set_tweak(str(b.get("key")), b.get("value", "")),
    "extra": lambda s, b: s.loader.set_extra(str(b.get("text", ""))),
    "preset": lambda s, b: s.loader.preset(str(b.get("name")), bool(b.get("reset"))),
    "check": lambda s, _b: s.loader.check(),
    "apply": lambda s, _b: s.loader.apply(),
    "revert": lambda s, _b: s.loader.revert(),
    "launch": lambda s, b: s.loader.launch(b.get("apply")),
    "map/install": lambda s, _b: s.loader.map_install(),
    "map/uninstall": lambda s, _b: s.loader.map_uninstall(),
    "studio": lambda s, _b: s.loader.open_studio(),
    "open": lambda s, b: s.loader.open(str(b.get("what"))),
    "setting": lambda s, b: s.loader.setting(b),
    "update/dismiss": lambda s, b: s.loader.dismiss_update(b.get("version", "")),
    "bye": _bye,
}


class LoaderServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, loader):
        self.loader = loader
        self.seen = time.time()
        self.bye = 0.0
        super().__init__((HOST, 0), Handler)
        port = self.server_address[1]
        self.hosts = ("%s:%d" % (HOST, port), "localhost:%d" % port)
        self.url = "http://%s:%d/" % (HOST, port)

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return                      # the window closed while a keep-alive socket waited
        _log(traceback.format_exc())


# -- the window ---------------------------------------------------------------

def _log(text):
    log = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "wolfsdk" / "start.log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(text.rstrip() + "\n")
    except OSError:
        pass
    return log


def _popen(cmd):
    try:
        return subprocess.Popen(cmd, close_fds=True)
    except OSError:
        return None


def window_command(url):
    """argv for an app window of url (Edge, else Chrome), or None. Its own
    profile -- not the Studio's -- so it is a window and a process of its own."""
    env = os.environ.get
    local = Path(env("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    for name, rel in (("edge", r"Microsoft\Edge\Application\msedge.exe"),
                      ("chrome", r"Google\Chrome\Application\chrome.exe")):
        for var in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
            exe = Path(env(var), rel) if env(var) else None
            if exe and exe.is_file():
                return [str(exe), "--app=" + url, "--window-size=%d,%d" % WINDOW,
                        "--user-data-dir=" + str(local / "wolfsdk" / ("%s-loader-profile" % name)),
                        "--no-first-run", "--no-default-browser-check"]
    return None


def alive(loader, server, now=None):
    """Is any window of this process still open?"""
    now = time.time() if now is None else now
    closed = False
    for proc, started in loader.procs:
        if proc.poll() is None:
            return True
        # a process that exited within HANDOFF handed its window to a browser
        # already running: that window is untracked, the page's calls decide
        closed = closed or getattr(proc, "_wolfsdk_seen_exit", now) - started > HANDOFF
    if closed:
        return False
    # bye was the page's last call: closed, unless it calls again (a reload) within 5 s
    return now - server.seen < (5 if server.bye >= server.seen else LINGER)


def main(game_path=None):
    try:
        loader = Loader(game_path)
        server = LoaderServer(loader)
    except Exception:  # noqa: BLE001 -- under pythonw an error would vanish silently
        log = _log(traceback.format_exc())
        _fatal("The loader could not start:\n%s\n\nDetails: %s" % (sys.exc_info()[1], log))
        return 1
    threading.Thread(target=server.serve_forever, name="loader-http", daemon=True).start()
    threading.Thread(target=loader.check_updates, name="loader-update", daemon=True).start()
    cmd = window_command(server.url)
    if not loader.track(_popen(cmd) if cmd else None):
        webbrowser.open(server.url)
    try:
        while True:
            time.sleep(2)
            for proc, _started in loader.procs:
                if proc.poll() is not None and not hasattr(proc, "_wolfsdk_seen_exit"):
                    proc._wolfsdk_seen_exit = time.time()
            if not loader.busy and not alive(loader, server):
                break
    except KeyboardInterrupt:
        pass
    _close_studio()
    server.shutdown()
    server.server_close()
    return 0


def _fatal(text):
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("WolfSDK", text)
        root.destroy()
    except Exception:  # noqa: BLE001
        print(text, file=sys.stderr)
