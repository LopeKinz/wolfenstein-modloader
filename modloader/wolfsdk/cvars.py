"""Cheat / QoL tweaks driven entirely by engine cvars.

Nothing here modifies a game file. The engine accepts `+<cvar> <value>` on its
command line -- the game's own build.cfg uses exactly that syntax:

    +feature_hq 1 +sys_region "ww" +g_EnableGore 1 ...

so a tweak set is just extra launch arguments. Anything that needs to change
mid-session is exposed as a keybind instead, which costs one more argument.

The New Order: every name is checked against the engine's own cvar registry
(docs/cvars_tno.json, walked out of the decrypted runtime image by
tools/extract_cvars_tno.py), see tno_route(). Unknown and read-only names are
left out. A CHEAT-flagged cvar set as `+name value` is wiped by the engine's
cheat reset during startup, so it is also sent as `+toggle name value value`,
a command that runs after that reset and force-sets the value
(re_probes/agent_reports/r8_tnocvars.md). What a cvar *does* comes from its
in-binary description; `confidence` marks the guesses. Confirmed in a running
game so far: `+g_showHud 0` (plain) and `+toggle devgui` (survives the
reset, docs/limits.md); the rest of the routing is read from the code.
"""

from dataclasses import dataclass, field
from typing import Optional

BOOL = "bool"
FLOAT = "float"
INT = "int"
CHOICE = "choice"

# Measured, not guessed. A probe config was fed to the retail build and the
# engine's own log was read back; every name below produced
#   WARNING: <name> CANNOT be set in retail!
# Everything else in the catalogue was accepted silently. All of them carry
# the CHEAT flag in docs/cvars_tno.json, and every accepted one carries
# NOCHEAT (tools/verify_tno_tweaks.py checks this). Only a fallback now: the
# registry decides how a New Order cvar is delivered (tno_route).
RETAIL_BLOCKED = frozenset({
    "ai_damageScale", "ai_invulnerable", "com_allowConsole", "com_skipIntroVideo",
    "devgui", "g_AimAssist_Disable_All", "g_EnableGore", "g_debugGore",
    "g_freeCamSpeed", "g_gameDifficulty", "g_goreBloodyMess", "g_gravity",
    "g_infiniteAmmo", "g_playerHealthRegenDelay", "g_playerHealthRegenSpeed",
    "g_showThinks", "g_useSavegameDifficulty", "pm_autoSprint", "pm_crouchspeed",
    "pm_jumpheight", "pm_noBob", "pm_noclipspeed", "pm_runspeed",
    "pm_sprintMaxTime", "pm_sprintspeed", "pm_walkspeed",
})


@dataclass(frozen=True)
class Tweak:
    key: str  # cvar name
    label: str
    category: str
    kind: str = BOOL
    default: Optional[str] = None  # engine default, where known
    value_on: str = "1"
    value_off: str = "0"
    lo: Optional[float] = None
    hi: Optional[float] = None
    choices: tuple = ()
    note: str = ""
    # "high"   -> name and meaning are unambiguous
    # "medium" -> name is explicit, exact effect unconfirmed
    # "low"    -> plausible but genuinely a guess
    confidence: str = "medium"

    def arg(self, value):
        return ["+%s" % self.key, str(value)]


CHEATS = [
    Tweak("g_permaGodMode", "God mode (permanent)", "Cheats",
          default="0", confidence="high",
          note="The engine's own permanent god mode. Archived: the game saves it to "
               "wolfConfig.cfg, so it stays on until set back to 0. "
               "TNO has no 'god' command; in the console use: toggle g_permaGodMode 0 1"),
    Tweak("g_permaInfiniteAmmo", "Infinite ammo (permanent)", "Cheats",
          default="0", confidence="high"),
    Tweak("g_infiniteAmmo", "Infinite ammo (session)", "Cheats",
          default="0", confidence="high"),
    Tweak("g_damageScale", "Damage taken scale", "Cheats", kind=FLOAT,
          default="1", lo=0.0, hi=10.0, confidence="medium",
          note="Engine: 'scale final damage on player by this factor'. 0 = you take no damage."),
    Tweak("ai_damageScale", "AI damage scale", "Cheats", kind=FLOAT,
          default="1", lo=0.0, hi=10.0, confidence="medium",
          note="Damage dealt by enemies. 0 = harmless."),
    Tweak("ai_invulnerable", "Enemies invulnerable", "Cheats",
          default="0", confidence="high",
          note="Reverse cheat - useful for testing weapon mods."),
    Tweak("g_AimAssist_Disable_All", "Aim assist fully off", "Cheats",
          default="0", confidence="high"),
    Tweak("g_playerHealthRegenSpeed", "Health regen speed", "Cheats", kind=FLOAT,
          default="5.75", lo=0.0, hi=1000.0, confidence="medium"),
    Tweak("g_playerHealthRegenDelay", "Health regen delay", "Cheats", kind=FLOAT,
          default="3", lo=0.0, hi=10000.0, confidence="medium"),
    Tweak("g_gravity", "Gravity", "Cheats", kind=FLOAT,
          default="1066", lo=-2000.0, hi=2000.0, confidence="medium",
          note="Engine: 'control the force of gravity on physics objects'."),
    Tweak("g_gameDifficulty", "Difficulty", "Cheats", kind=CHOICE,
          default="-1",
          choices=(("-1", "from the game"), ("0", "very easy"), ("1", "1"), ("2", "2"),
                   ("3", "3"), ("4", "nightmare")),
          confidence="medium",
          note="Engine: -1 = use the game's own setting, 0 = very easy ... 4 = nightmare."),
]

QOL = [
    Tweak("g_fov", "Field of view (FOV)", "Quality of Life", kind=INT,
          default="80", lo=60, hi=100, confidence="high",
          note="Engine default 80, engine range 10-100: higher values are clamped to 100."),
    Tweak("pm_noBob", "No head bobbing", "Quality of Life",
          default="0", confidence="high",
          note="Helps against motion sickness."),
    Tweak("pm_togglesprint", "Toggle sprint instead of hold", "Quality of Life",
          default="1", confidence="high"),
    Tweak("pm_autoSprint", "Auto-Sprint", "Quality of Life",
          default="0", confidence="medium"),
    Tweak("com_skipIntroVideo", "Skip intro videos", "Quality of Life",
          default="0", confidence="high"),
    Tweak("com_allowConsole", "Console flag (com_allowConsole)", "Quality of Life",
          default="0", confidence="low",
          note="Does NOT open the console: TNO's console opens with Ctrl+^ (Ctrl + the key "
               "left of 1) whether this is set or not. The only code that reads it is game "
               "init, feeding an achievement check - leave it off."),
    Tweak("com_showFPS", "FPS counter", "Quality of Life",
          default="0", confidence="high"),
    Tweak("r_swapInterval", "VSync", "Quality of Life", kind=CHOICE,
          default="-1", choices=(("0", "off"), ("1", "on (60 Hz)"), ("-1", "swap tear")),
          confidence="high"),
    Tweak("g_showHud", "Show HUD", "Quality of Life",
          default="1", confidence="high",
          note="Set to 0 for clean screenshots."),
    Tweak("g_EnableGore", "Gore", "Quality of Life", kind=CHOICE,
          default="1", choices=(("0", "blood only"), ("1", "gore"), ("2", "no gore")),
          confidence="high",
          note="Engine: 0 = only blood particles unless overridden, 1 = gore, 2 = no gore at all."),
    Tweak("g_bloodEffects", "Blood effects", "Quality of Life",
          default="1", confidence="high"),
    Tweak("g_goreBloodyMess", "Bloody-mess perk gore factor", "Quality of Life", kind=FLOAT,
          default="2.0", confidence="medium",
          note="Engine: scales the gore probability while a bloody-mess perk is active. "
               "Default 2.0, so 1 means less gore, not more."),
]

MOVEMENT = [
    Tweak("pm_runspeed", "Run speed", "Movement", kind=FLOAT,
          default="260", lo=0.0, hi=1000.0, confidence="high"),
    Tweak("pm_walkspeed", "Walk speed", "Movement", kind=FLOAT,
          default="140", lo=0.0, hi=1000.0, confidence="high"),
    Tweak("pm_sprintspeed", "Sprint speed", "Movement", kind=FLOAT,
          default="450", lo=0.0, hi=1000.0, confidence="high"),
    Tweak("pm_crouchspeed", "Crouch speed", "Movement", kind=FLOAT,
          default="115", lo=0.0, hi=1000.0, confidence="high"),
    Tweak("pm_jumpheight", "Jump height", "Movement", kind=FLOAT,
          default="62", lo=0.0, hi=500.0, confidence="high"),
    Tweak("pm_noclipspeed", "Noclip speed", "Movement", kind=FLOAT,
          default="400", lo=0.0, hi=2000.0, confidence="low",
          note="Retail TNO has no 'noclip' command, so nothing reaches this speed."),
    Tweak("pm_sprintMaxTime", "Max sprint time (ms)", "Movement", kind=INT,
          default="1500", lo=1, hi=100000, confidence="medium",
          note="Engine range 1-100000 ms. p_infiniteSprintMode (engine default 1, "
               "'disable the stamina system') may make this moot."),
    Tweak("g_freeCamSpeed", "Free camera speed", "Movement", kind=FLOAT,
          default="1.0", lo=0.0, hi=2000.0, confidence="high",
          note="Speed of the debug free camera (toggle g_freeCam 0 1, F3 bind)."),
]

CATALOG = CHEATS + QOL + MOVEMENT
BY_KEY = {t.key: t for t in CATALOG}
CATEGORIES = ("Cheats", "Quality of Life", "Movement")

# The New Order's console: Ctrl+^ (Ctrl + the key left of 1) opens it, no
# cvar needed (code-read in r7_tnocheatgate.md P2.4, not yet tried in game).
# Every name here is in re_probes/tnocommands/commands.json, or is `toggle`
# on a registered cvar. TNO has no god/noclip/notarget/give/spawn command;
# `toggle` force-sets cheat cvars, a bare `name value` line is refused.
COMMANDS = [
    ("toggle g_permaGodMode 0 1", "God mode on/off (there is no 'god' command)"),
    ("toggle g_infiniteAmmo 0 1", "Infinite ammo on/off"),
    ("toggle g_freeCam 0 1", "Free debug camera (no 'noclip'/'notarget' in TNO)"),
    ("toggle devgui 0 1", "DevGUI overlay, inside a level ('devgui 1' is refused)"),
    ("toggle", "Set any cvar, cheat ones too: 'toggle g_fov 80 100'"),
    ("kill", "Kill yourself (respawn at checkpoint)"),
    ("killMonsters", "Remove all monsters"),
    ("teleport", "Teleport to an entity: teleport <entity name>"),
    ("restartmap", "Restart the current map"),
    ("devmap", "Load a map in developer mode"),
    ("map", "Load a map"),
    ("screenshot", "Screenshot"),
]

# Keys we are willing to bind by default. F1-F4 are unbound in default.cfg.
# The New Colossus still gets exactly this list (its launch output is kept
# unchanged); The New Order gets TNO_BINDS, see binds_for().
DEFAULT_BINDS = [
    ("F1", "toggle g_permaGodMode 0 1", "Toggle god mode"),
    ("F2", "toggle g_permaInfiniteAmmo 0 1", "Toggle infinite ammo"),
    ("F3", "noclip", "Toggle noclip"),
    ("F4", "toggle g_showHud 0 1", "Toggle HUD"),
]
TNO_BINDS = [b if b[0] != "F3" else ("F3", "toggle g_freeCam 0 1", "Toggle free camera")
             for b in DEFAULT_BINDS]


def binds_for(title_key):
    """Default binds for one title: TNO has no 'noclip' command for F3.

    The New Colossus gets none: a bound key runs at the restricted level (34
    commands), where neither `toggle` nor `noclip` exists, and `+bind F1` would
    replace the game's own F1 bind. Its hotkeys come from the Sandbox Tools mod
    (`resourceExec <cfile> -s`)."""
    return TNO_BINDS if title_key == "tno" else ()


# Set on every launch unless the user puts their own value in. Sitting
# through the logo reel on each start is nobody's idea of quality of life.
# A CHEAT cvar: the startup cheat reset wipes it, but idCommonLocal::Init
# reads it (RVA 0x1337ad) after the first command-line pass and before that
# reset, so the plain +arg is the part that counts here (r8_tnocvars.md).
ALWAYS = {"com_skipIntroVideo": "1"}


def build_args(settings, binds=(), title_key=None):
    """Turn {cvar: value} into engine command-line arguments.

    title_key "tno" routes every name through tno_route(). Any other title
    passes unknown keys through unchanged, as before.
    """
    merged = dict(ALWAYS)
    merged.update(settings)
    args = []
    for key, value in merged.items():
        if value is None or value == "":
            continue
        if title_key != "tno":
            args += ["+%s" % key, str(value)]
            continue
        name, how = tno_route(key)
        value = str(value)
        if how == "plain":
            args += ["+%s" % name, value]
        elif how == "cheat":
            # The +arg serves code that reads the cvar during startup, before
            # the cheat reset; toggle with the value twice always lands on it.
            args += ["+%s" % name, value, "+toggle", name, value, value]
    for combo, command, _desc in binds:
        args += ["+bind", combo, command]
    return args


def build_cfg(settings, binds=(), header=True):
    """Render the same tweak set as a .cfg the engine can `exec`."""
    lines = []
    if header:
        lines += [
            "// Generated by wolfsdk -- do not edit by hand.",
            "// Load with:  WolfNewOrder_x64.exe +exec wolfsdk.cfg",
            "",
        ]
    by_cat = {}
    for key, value in settings.items():
        tweak = BY_KEY.get(key)
        by_cat.setdefault(tweak.category if tweak else "Other", []).append((key, value))
    for cat in list(CATEGORIES) + ["Other"]:
        items = by_cat.get(cat)
        if not items:
            continue
        lines.append("// --- %s ---" % cat)
        for key, value in items:
            lines.append("%s %s" % (key, value))
        lines.append("")
    if binds:
        lines.append("// --- Key bindings ---")
        for combo, command, desc in binds:
            lines.append('bind "%s" "%s"   // %s' % (combo, command, desc))
        lines.append("")
    return "\n".join(lines)


def defaults():
    """The catalog's known engine defaults, as a settings dict."""
    return {t.key: t.default for t in CATALOG if t.default is not None}

# -- second title -----------------------------------------------------------
#
# The New Order's catalogue above is 31 hand-curated cvars with labels
# and measured retail behaviour. The New Colossus has 3116 settable cvars from
# the engine's own registrar dump (tools/extract_cvars_tnc_exposed.py), too
# many to hand-curate -- it is loaded from docs/cvars_tnc.json and grouped by
# prefix instead. The UI treats both as the same thing: a list of Tweak.

TEXT = "text"

_TNC_GROUPS = {
    "g_": "Game & Cheats", "pm_": "Movement", "ai_": "AI",
    "com_": "System", "sys_": "System", "r_": "Renderer",
    "image_": "Textures", "vt_": "Virtual Textures", "rs_": "Renderer",
    "s_": "Audio", "snd_": "Audio", "in_": "Input", "menu_": "Menu",
    "gui_": "Menu", "ui_": "Menu", "net_": "Network", "si_": "Network",
    "bot_": "Network", "anim_": "Animation", "af_": "Animation",
    "decl_": "Decls", "fx_": "Effects", "cg_": "Effects", "vr_": "VR",
    "aic_": "AI", "dp_": "Misc", "gp_": "Misc", "job_": "System",
    "vk_": "Renderer",
}

# A description that spells out its own values, or reads like a switch, is a
# boolean. Guessing this wrong only costs a text box instead of a tick box.
_BOOL_HINTS = ("0 = ", "1 = ", "if true", "enables ", "enable ", "toggle",
               "true/false", "0=", "1=")

_tnc_cache = None


def _tnc_catalog():
    """Load the mined New Colossus catalogue, once."""
    global _tnc_cache
    if _tnc_cache is not None:
        return _tnc_cache
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "docs" / "cvars_tnc.json"
    try:
        raw = json.loads(path.read_text("utf-8"))["cvars"]
    except (OSError, ValueError, KeyError):
        _tnc_cache = []
        return _tnc_cache

    out = []
    for entry in raw:
        name = entry["name"]
        prefix = name.split("_", 1)[0] + "_" if "_" in name else "?"
        note = entry.get("description") or ""
        low = note.lower()
        kind = BOOL if entry.get("type") == "bool" or (
            entry.get("type") is None and any(h in low for h in _BOOL_HINTS)) else TEXT
        # How well the description was tied to the name, from the miner.
        match = entry.get("match") or 0
        out.append(Tweak(
            key=name,
            label=name,
            category=_TNC_GROUPS.get(prefix, "Misc"),
            kind=kind,
            default=entry.get("value"),
            note=note,
            confidence="medium" if match >= 2 else "low",
        ))
    # Useful first, plumbing last. Alphabetical would open the page on
    # Animation and bury the cheats somewhere past Network.
    order = ["Game & Cheats", "Movement", "AI", "Effects", "Renderer",
             "Textures", "Audio", "Input", "Menu", "System",
             "Animation", "Decls", "Virtual Textures", "Network", "VR"]
    rank = {name: i for i, name in enumerate(order)}
    out.sort(key=lambda t: (rank.get(t.category, len(order)), t.category, t.key))
    _tnc_cache = out
    return out


def catalog_for(title_key):
    """The tweak list for one title. Unknown keys fall back to The New Order."""
    if title_key == "tnc":
        return _tnc_catalog()
    return CATALOG


def categories_for(title_key):
    seen = []
    for tweak in catalog_for(title_key):
        if tweak.category not in seen:
            seen.append(tweak.category)
    return tuple(seen)


_tnc_exposed_cache = None


def _tnc_exposed():
    """{cvar name} the New Colossus retail console/+launch-argument gate
    accepts, from docs/cvars_tnc_exposed.json (built by
    tools/extract_cvars_tnc_exposed.py out of a static idCVar-registrar
    dump). Everything else -- registered under another name, or not
    registered at all -- is rejected the same way ("Unknown command"),
    so an empty/missing file means "treat every TNC tweak as not exposed"
    rather than silently falling back to passing it as a +arg anyway.
    """
    global _tnc_exposed_cache
    if _tnc_exposed_cache is not None:
        return _tnc_exposed_cache
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "docs" / "cvars_tnc_exposed.json"
    try:
        _tnc_exposed_cache = frozenset(json.loads(path.read_text("utf-8"))["exposed"])
    except (OSError, ValueError, KeyError):
        _tnc_exposed_cache = frozenset()
    return _tnc_exposed_cache


_tno_cache = None


def _tno_registry():
    """{lower name: entry} from docs/cvars_tno.json (tools/extract_cvars_tno.py),
    or {} if the file is missing -- tno_route then falls back to RETAIL_BLOCKED."""
    global _tno_cache
    if _tno_cache is None:
        import json
        from pathlib import Path

        path = Path(__file__).resolve().parent.parent / "docs" / "cvars_tno.json"
        try:
            raw = json.loads(path.read_text("utf-8"))["cvars"]
            _tno_cache = {c["name"].lower(): c for c in raw}
        except (OSError, ValueError, KeyError):
            _tno_cache = {}
    return _tno_cache


def tno_route(key):
    """(name, how) for one New Order cvar, `name` spelled as registered.

    how = "plain": a +name value argument; the engine force-sets it after
          wolfConfig.cfg, and nothing resets it.
          "cheat": CHEAT-flagged. The +arg is wiped by the startup cheat reset,
          so build_args adds +toggle name value value, which runs later and
          force-sets past the retail gate (r7_tnocheatgate.md P2.1).
          "drop":  not a registered cvar (a command, a typo, a New Colossus
          name) or read-only -- nothing to send.
    """
    reg = _tno_registry()
    if not reg:
        return key, "cheat" if key in RETAIL_BLOCKED else "plain"
    entry = reg.get(key.lower())
    if entry is None or "ROM" in entry["flags"]:
        return key, "drop"
    return entry["name"], "cheat" if "CHEAT" in entry["flags"] else "plain"


def blocked_for(title_key, key):
    """Retail lock state at the console/+arg level.

    The New Order: CHEAT-flagged (or unknown) in the engine's registry, i.e.
    a plain +arg does not survive and the loader sends +toggle as well (see
    tno_route). The New Colossus: everything outside the 304-name expose list mined from
    the binary's own cvar registrar (_tnc_exposed) -- not measured in a
    running game, but the classification a cfile 'resourceExec ... -s' is
    needed for regardless (see split_tnc below), so 'blocked' here means
    exactly 'needs that other route', not 'cannot be set at all'.
    """
    if title_key == "tnc":
        return key not in _tnc_exposed()
    return tno_route(key)[1] != "plain"


def split_tnc(settings):
    """(arg_settings, cfile_settings): how to deliver a New Colossus tweak set.

    The retail console and +launch-arguments share one gate that only the
    304 exposed cvars pass; every other cvar -- cheat-flagged ones like
    g_EnableGore and g_infiniteAmmo included -- comes back "Unknown command"
    there, proven from the game's own log (TODO.md, 2026-10-01). Lines inside
    a cfile the engine execs itself (e.g. cfile:default.cfg via its own
    'resourceExec default.cfg -s' at init) run at a lower restriction level
    and are not filtered by that gate (re_probes/agent_reports/r4_consolecmds.md),
    so cfile_settings is what wolfsdk/mod.py's with_tnc_tweaks() appends there.
    """
    known = {t.key.lower() for t in _tnc_catalog()}
    args, cfile = {}, {}
    for key, value in settings.items():
        if known and key.lower() not in known:
            continue    # not a New Colossus cvar (e.g. a New Order name left in config.json)
        (cfile if blocked_for("tnc", key) else args)[key] = value
    return args, cfile


# -- presets (The New Colossus) ---------------------------------------------
#
# One-click cvar groups for the Cheats & QoL page. They only fill in tweak
# values; split_tnc above decides per cvar whether it goes out as a +arg or
# into cfile:default.cfg. Values: re_probes/agent_reports/r6_renderplus.md §8
# (Ultra+, Clean Image) and r6_photocam.md §9 tier A (Photo Mode).
# tools/verify_presets.py checks every name, type and declared range against
# the engine registry. Not yet confirmed in game.
#
# Left out of r6 on purpose: g_fov 120 (gameplay, and ARCHIVE keeps it after a
# reset), r_antialiasing 9 (TSSAA 8X, cost never measured), the 16k decal
# atlas (4x its memory), com_overrideDOF (its stock distances may switch a
# manual DOF on). vt_minLockedVmtrLOD/vt_maxLockedPagesPercent use r6's own
# cautious 3/30, not 0/50. Photo Mode holds only what is safe for a whole
# session: freezing time and the free camera must toggle mid-game, and a TNC
# key bind runs behind the retail gate, where neither `toggle` nor g_stopTime
# exist (r6_photocam.md §2) -- that needs a key bound to a cfile carrier.
TNC_PRESETS = {
    "Ultra+": {
        "r_decalFilteringQuality": "4",
        "image_anisotropy": "16",
        "vt_maxAniso": "16",
        "r_imageAtlasMaxAniso": "16",
        "vt_maxLockedPagesPercent": "30",
        "vt_minLockedVmtrLOD": "3",
        "vt_maxPPF": "64",
        "r_decalDistanceFadeMultiplier": "2.5",
        "r_decalLifetimeMultiplier": "2.5",
        "r_shadowsDistanceFadeMultiplier": "1.5",
        "r_umbraMaxShadowQueriesVisible": "32",
        "r_umbraMaxShadowQueriesInvisible": "8",
        "r_waterQualityFFT": "1",
        "r_decalDistanceFadeStart": "120",
        "r_foliageStartFadeDist": "6000",
        "r_foliageSmallFadeDistMax": "10000",
        "r_foliageBigFadeDistMax": "14000",
    },
    "Clean Image": {
        "r_dof": "0",
        "r_motionblur": "0",
        "r_chromaticAberration": "0",
        "r_filmGrainRatio": "0",
        "r_vignette": "0",
        "r_shadingRateQualityOverride": "4",
        "r_sharpening": "2.5",
        "image_anisotropy": "16",
        "vt_maxAniso": "16",
        "r_imageAtlasMaxAniso": "16",
        "rs_enable": "0",
    },
    "Photo Mode": {
        "com_captureSamples": "4",
        "com_captureTGA": "1",
        "image_screenshotQuality": "100",
    },
}


def apply_preset(settings, name):
    """`settings` with preset `name`'s cvars set on top."""
    return {**settings, **TNC_PRESETS[name]}


def reset_preset(settings, name, before=None):
    """`settings` with preset `name`'s cvars back to what `before` held, and
    dropped (= engine default) where it held nothing. Without `before`, all
    of the preset's cvars are dropped."""
    before = before or {}
    out = {k: v for k, v in settings.items() if k not in TNC_PRESETS[name]}
    out.update((k, before[k]) for k in TNC_PRESETS[name] if k in before)
    return out
