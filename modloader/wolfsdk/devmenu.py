"""In-game mod menu, built by extending the engine's own DevGUI.

The game ships `file:debugmenu.txt`, a data-driven definition of the developer
overlay that `devgui 1` opens (F10 in the stock keybinds). It is a
comma-separated list of page objects with trailing commas allowed:

    {
      "pageName" : "General",
      "entries" : [
        { "title" : "No clip",     "cmd"  : "noclip" },
        { "title" : "Show fps/mem","cvar" : "com_showfps", "values" : [ "0", "1" ] },
        { "title" : "Show Stats",  "cmd"  : "con_noprint 0; toggle vt_showStats",
          "help" : "show virtual texture system statistics" }
      ]
    },
    { "pageName" : "VT", ... }

Appending another page therefore gives a real in-game UI with no injection and
no executable patch -- the engine renders it, we only supply data. Not yet seen
in a running game; in Wolfenstein II the engine zeroes `devgui` every frame, so
there it needs a process patch (re_probes/agent_reports/r11_devgui.md).

Entry forms the stock file uses, and that we reuse:

    "cmd"    console command, run when the entry is activated
    "cvar"   cvar the entry edits
    "values" list of values to cycle through (with "cvar")
    "help"   line shown for the selected entry
"""

import json

from . import cvars

PAGE_NAME = "WOLFSDK"
TARGET = ("file", "debugmenu.txt")
MARKER = '"pageName" : "%s"' % PAGE_NAME


_ASCII = str.maketrans({
    "ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue",
    "ß": "ss", "é": "e", "è": "e", "–": "-", "—": "-", "…": "...",
})


def _ascii(text):
    """Keep menu text plain ASCII.

    The engine's parser is unknown territory; \\uXXXX escapes and raw UTF-8
    are both risks we do not need to take for a handful of umlauts.
    """
    return text.translate(_ASCII).encode("ascii", "replace").decode("ascii")


def _entry(title, cmd=None, cvar=None, values=None, help_=None):
    title, help_ = _ascii(title), _ascii(help_) if help_ else help_
    out = {"title": title}
    if cvar:
        out["cvar"] = cvar
    if values:
        out["values"] = [str(v) for v in values]
    if cmd:
        out["cmd"] = cmd
    if help_:
        out["help"] = help_
    return out


def _toggle(tweak, values=("0", "1")):
    return _entry(tweak.label, cvar=tweak.key, values=values, help_=tweak.note or tweak.key)


def default_entries(settings=None):
    """The mod menu's contents.

    `settings` is the loader's current tweak dict; any cvar the user set to a
    non-default value gets that value offered in its cycle, so the in-game
    menu can switch between "off" and "what I configured".
    """
    settings = settings or {}
    entries = []

    entries.append(_entry("--- CHEATS ---", cmd="con_noprint 0"))
    for key in ("g_permaGodMode", "g_permaInfiniteAmmo", "g_AimAssist_Disable_All"):
        tweak = cvars.BY_KEY.get(key)
        if tweak:
            entries.append(_toggle(tweak))
    entries += [
        _entry("No clip", cmd="noclip", help_="Fly freely through walls"),
        _entry("No target", cmd="notarget", help_="Enemies ignore you"),
        _entry("Kill yourself", cmd="kill", help_="Respawn at the last checkpoint"),
        _entry("Unlock all secrets", cmd="unlocksecretall"),
        _entry("Restart map", cmd="restartmap"),
    ]

    entries.append(_entry("--- MOVEMENT ---", cmd="con_noprint 0"))
    for key, values in (
        ("pm_runspeed", ("200", "400", "800")),
        ("pm_sprintspeed", ("300", "600", "1200")),
        ("pm_jumpheight", ("60", "120", "240")),
        ("g_gravity", ("1066", "400", "-200")),
    ):
        tweak = cvars.BY_KEY.get(key)
        if tweak:
            entries.append(_toggle(tweak, values))

    entries.append(_entry("--- VIEW ---", cmd="con_noprint 0"))
    fov_values = ["80", "95", "110"]
    configured = settings.get("g_fov")
    if configured and configured not in fov_values:
        fov_values.append(str(configured))
    entries += [
        _entry("Field of view (FOV)", cvar="g_fov", values=fov_values),
        _entry("HUD", cvar="g_showHud", values=("1", "0")),
        _entry("No head bobbing", cvar="pm_noBob", values=("0", "1")),
        _entry("FPS counter", cvar="com_showFPS", values=("0", "1"), cmd="con_noprint 0"),
        _entry("Gore", cvar="g_goreBloodyMess", values=("0", "1")),
    ]

    entries.append(_entry("--- TIME ---", cmd="con_noprint 0"))
    entries.append(
        _entry("Difficulty", cvar="g_gameDifficulty", values=("0", "1", "2", "3"))
    )
    return entries


def build_page(entries=None, settings=None, page_name=PAGE_NAME):
    """Render one DevGUI page as text, matching the stock file's style."""
    entries = entries if entries is not None else default_entries(settings)
    lines = ["{", '\t"pageName" : "%s",' % page_name, '\t"entries" : [']
    for index, item in enumerate(entries):
        comma = "," if index < len(entries) - 1 else ""
        lines.append("\t\t" + json.dumps(item, ensure_ascii=True) + comma)
    lines.append("\t]")
    lines.append("}")
    return "\n".join(lines)


def strip_page(text, page_name=PAGE_NAME):
    """Remove a previously injected page, so injection stays idempotent."""
    marker = '"pageName" : "%s"' % page_name
    start = text.find(marker)
    if start < 0:
        return text
    open_brace = text.rfind("{", 0, start)
    if open_brace < 0:
        return text
    depth = 0
    i = open_brace
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    end = i + 1
    # Swallow the separating comma on whichever side it sits.
    head, tail = text[:open_brace], text[end:]
    if head.rstrip().endswith(","):
        head = head.rstrip()[:-1] + "\n"
    elif tail.lstrip().startswith(","):
        tail = tail.lstrip()[1:]
    return head.rstrip() + "\n" + tail.lstrip("\n")


def inject(text, page_text=None, settings=None, page_name=PAGE_NAME):
    """Append (or replace) our page in debugmenu.txt content."""
    base = strip_page(text, page_name).rstrip()
    page = page_text if page_text is not None else build_page(settings=settings,
                                                              page_name=page_name)
    return base + ",\n" + page + "\n"


def payload(install, settings=None, page_name=PAGE_NAME):
    """Read the stock menu, inject our page, return bytes ready for apply()."""
    original = install.read(*TARGET).decode("utf-8")
    return inject(original, settings=settings, page_name=page_name).encode("utf-8")
