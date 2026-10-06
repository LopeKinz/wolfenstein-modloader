"""Locating, validating and launching the game installation."""

import json
import os
import re
import subprocess
import urllib.parse
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


class Title:
    """One supported game: how to find it, how to start it, what is inside.

    `marker` is the file that proves an install is complete, and it doubles
    as the cheapest way to tell the two apart: The New Order keeps a single
    master.index beside its chunks, while The New Colossus ships no index
    file at all and carries the file table inside each chunk_N.resources.

    `mods` says whether the patching machinery understands this title. False
    means the loader still finds it, starts it and passes cvars to it -- only
    writing into its archives is missing. Both supported titles are True now;
    the flag stays because it is what the UI greys its buttons out by, and a
    third title would arrive without a patcher again.
    """

    def __init__(self, key, name, exe, app_id, folders, engine, marker, mods):
        self.key = key
        self.name = name
        self.exe = exe
        self.app_id = app_id
        self.folders = folders
        self.engine = engine
        self.marker = marker
        self.mods = mods

    @property
    def run_url(self):
        return "steam://rungameid/%s" % self.app_id

    def __repr__(self):
        return "<Title %s>" % self.key


TITLES = {
    "tno": Title(
        key="tno",
        name="Wolfenstein: The New Order",
        exe="WolfNewOrder_x64.exe",
        app_id="201810",
        folders=("Wolfenstein.The.New.Order", "Wolfenstein The New Order"),
        engine="id Tech 5",
        marker="master.index",
        mods=True,
    ),
    "tnc": Title(
        key="tnc",
        name="Wolfenstein II: The New Colossus",
        exe="NewColossus_x64vk.exe",
        app_id="612880",
        folders=("Wolfenstein.II.The.New.Colossus",
                 "Wolfenstein II The New Colossus"),
        engine="id Tech 6",
        marker="chunk_1.resources",
        # Wired through wolfsdk/tncpatch.py: mod.installation_for() hands the
        # New Colossus archives to that module instead of patch.py.
        mods=True,
    ),
}

# Read-only in the Studio. Not in TITLES: the loader, its GUI and the CLI must not offer
# it (no patcher, and the cvar catalogue of either other game would be wrong for it).
YOUNGBLOOD = Title(
    key="yb",
    name="Wolfenstein: Youngblood",
    exe="Youngblood_x64vk.exe",
    app_id="1056960",
    folders=("Wolfenstein Youngblood",),
    engine="id Tech 6",
    marker="gameresources_pc.resources",
    mods=False,
)

# Kept so older code and saved configs that say EXE_NAME/APP_ID still mean TNO.
EXE_NAME = TITLES["tno"].exe
APP_ID = TITLES["tno"].app_id
STEAM_RUN_URL = TITLES["tno"].run_url


class GameError(Exception):
    pass


class Game:
    """One installed Wolfenstein, of either supported title."""

    def __init__(self, root, title=None):
        self.root = Path(root)
        self.title = title or _guess_title(self.root) or TITLES["tno"]
        self.exe = self.root / self.title.exe
        self.base = self.root / "base"
        self.backup = self.root / "base" / "_wolfsdk_backup"

    @property
    def supports_mods(self):
        return self.title.mods

    # -- discovery ---------------------------------------------------------

    @classmethod
    def find(cls, explicit=None, title=None):
        """Resolve an installation: explicit path, else saved config, then Steam.

        `title` is a key from TITLES; without one the first valid install
        wins, preferring whatever the config last remembered.

        An explicit path is the only candidate. Not a game there, or not the
        wanted one, is an error -- falling back to the remembered install
        turned a mistyped test-copy path into writes on the real game.
        """
        wanted = TITLES[title] if title else None
        # None is "no path given"; "" is a given path that is empty (an unset
        # %KOPIE% in `--game "%KOPIE%"`), never a reason to take the real game.
        if explicit is not None:
            no_other = (" The path was given explicitly, so the loader does not "
                        "use any other installation.")
            if not str(explicit).strip():
                raise GameError("The given game path is empty.%s" % no_other)
            game = cls(explicit)
            if not game.root.is_dir():
                raise GameError('The folder "%s" does not exist.%s' % (game.root, no_other))
            if not game.is_valid():
                t = wanted or _guess_title(game.root)
                need = ("%s and base\\%s" % (t.exe, t.marker) if t
                        else " or ".join(x.exe for x in TITLES.values()))
                raise GameError('"%s" does not hold a complete game installation (looked for: %s).%s'
                                % (game.root, need, no_other))
            if wanted and game.title is not wanted:
                raise GameError('"%s" holds %s, but %s is needed here.'
                                % (game.root, game.title.name, wanted.name))
            return game
        candidates = _remembered_paths() + _steam_candidates()
        for path in candidates:
            game = cls(path)
            if game.is_valid() and (wanted is None or game.title is wanted):
                return game
        raise GameError(
            "No installation found. Give the path with --game "
            '"...\\Wolfenstein.The.New.Order"'
        )

    @classmethod
    def find_all(cls):
        """Every supported title that is installed, one install each."""
        found = {}
        for path in _remembered_paths() + _steam_candidates():
            game = cls(path)
            if game.is_valid():
                found.setdefault(game.title.key, game)
        return [found[k] for k in TITLES if k in found]

    def is_valid(self):
        return self.exe.is_file() and (self.base / self.title.marker).is_file()

    def remember(self):
        cfg = _load_config()
        cfg["game_path"] = str(self.root)
        paths = cfg.get("game_paths")
        cfg["game_paths"] = paths if isinstance(paths, dict) else {}
        cfg["game_paths"][self.title.key] = str(self.root)
        _save_config(cfg)

    # -- launching ---------------------------------------------------------

    def launch(self, args=()):
        """Start the game through Steam, handing it engine arguments.

        Starting WolfNewOrder_x64.exe directly does not work on the retail
        build. It is SteamStub-wrapped: the wrapper sees it was not started by
        Steam, bounces the start back to the client and kills the current
        process. Measured on this installation -- the bounced process dies so
        early that it never even opens qconsole.log. No window, no error, no
        log line. From the outside the launch simply does nothing, which is
        exactly what "the game does not start" looks like.

        Steam's own URL handler takes everything after the double slash and
        passes it to the game as its command line, which the engine confirms
        in the log as

            Command line is: ...WolfNewOrder_x64.exe +g_showHud 0

        Verified in-game: launched this way, +g_showHud 0 removes the HUD.

        No process handle comes back -- Steam starts the game, not us.
        """
        if not self.is_valid():
            raise GameError("Incomplete installation: %s" % self.root)
        url = self.title.run_url
        tail = " ".join(_quote(str(a)) for a in args)
        if tail:
            url += "//" + urllib.parse.quote(tail, safe="")
        os.startfile(url)  # noqa: S606 - handing a steam:// URL to Steam
        return url

    def steam_launch_options(self, args):
        """The same arguments, formatted for Steam's launch-options box."""
        return " ".join(["%command%"] + [_quote(str(a)) for a in args])

    def __repr__(self):
        return "<Game %s %s>" % (self.title.key, self.root)


def _quote(arg):
    return '"%s"' % arg if " " in arg else arg


def steam_is_running():
    """Is the Steam client up?

    Launching the exe directly is the only way to pass per-launch cvars, but
    it means Steam is not started for us. Without it the game exits during
    startup without a window and without a log line.

    Not a precondition any more -- the steam:// URL starts the client if it
    is closed. The caller only uses this to warn that the first launch will
    take a while, instead of leaving the user staring at nothing.

    Undetectable counts as running: a broken check must never turn into a
    scary message about a launch that is going to work fine.

    Bytes, not text, as in mod.running_game: the German "keine Aufgaben ...
    ausgeführt" is OEM-encoded, a text decode dies in the reader thread and
    hands back stdout=None.
    """
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq steam.exe", "/NH"],
            capture_output=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    return not out or b"steam.exe" in out.lower()


# -- config -----------------------------------------------------------------


def load_config():
    """Persisted loader state: game path, enabled mods, tweak values."""
    try:
        return json.loads(CONFIG_PATH.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), "utf-8")


_load_config = load_config
_save_config = save_config


# -- steam discovery --------------------------------------------------------


def _steam_root():
    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(env)
        if base:
            path = Path(base) / "Steam"
            if path.is_dir():
                return path
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            return Path(winreg.QueryValueEx(key, "SteamPath")[0])
    except Exception:  # noqa: BLE001 - registry is best-effort only
        return None


def _guess_title(root):
    """Which title lives at `root`, going by the executable that is there."""
    for title in TITLES.values():
        if (Path(root) / title.exe).is_file():
            return title
    return None


def _remembered_paths():
    cfg = _load_config()
    out = [Path(v) for v in (cfg.get("game_paths") or {}).values()]
    if cfg.get("game_path"):
        out.append(Path(cfg["game_path"]))
    return out


def find_root(title):
    """Install folder of `title` in a Steam library (exe and marker present), or None."""
    for path in _steam_candidates([title]):
        if (path / title.exe).is_file() and (path / "base" / title.marker).is_file():
            return path
    return None


def _steam_candidates(titles=None):
    """Every Steam library folder that could hold one of the games."""
    root = _steam_root()
    if not root:
        return []
    libraries = [root]
    vdf = root / "steamapps" / "libraryfolders.vdf"
    try:
        text = vdf.read_text("utf-8", errors="replace")
    except OSError:
        text = ""
    # The vdf format nests differently across Steam versions; pulling every
    # "path" value out with a regex survives all of them.
    for match in re.finditer(r'"path"\s*"([^"]+)"', text):
        libraries.append(Path(match.group(1).replace("\\\\", "\\")))
    out = []
    for lib in libraries:
        common = lib / "steamapps" / "common"
        if not common.is_dir():
            continue
        for title in titles or TITLES.values():
            for name in title.folders:
                out.append(common / name)
    return out
