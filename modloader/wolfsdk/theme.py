"""A dark theme for the loader, built on ttk alone.

Why by hand: the whole SDK ships without third-party packages, so there is no
widget toolkit to fall back on. ttk can look decent, but only from the `clam`
theme -- `vista` and `xpnative` draw their widgets with the OS and ignore most
colour settings, which is why the loader used to be a grey Windows dialog no
matter what was configured.

Everything here is `Style.configure`/`Style.map` on clam plus a handful of
named styles the UI asks for by name (`Accent.TButton`, `Card.TFrame`, ...).
Plain `tk` widgets -- Canvas, Text, Listbox -- are not themed by ttk at all
and have to be coloured by the caller; PALETTE is public for that.
"""

import tkinter.font as tkfont
from tkinter import ttk

PALETTE = {
    "bg": "#0f1117",          # window
    "panel": "#171a23",       # cards, tab body
    "raised": "#222633",      # inputs, hover
    "line": "#303747",        # borders, separators
    "text": "#f2f4f8",
    "muted": "#9aa4b6",
    "dim": "#697386",
    "accent": "#d3183b",      # restrained Wolfenstein red
    "accent_hi": "#ef3152",
    "select": "#4a1a27",      # selected rows: a red tint, not a red slab
    "focus": "#8892a8",       # keyboard focus ring
    "ok": "#43c978",
    "warn": "#e4ad3a",
    "danger": "#ff6078",
}

FONT_FAMILY = "Segoe UI"
HEAD_FAMILY = "Segoe UI Semibold"   # Tk knows only normal/bold; the semibold cut is its own family
MONO_FAMILY = "Cascadia Mono"


def _pick_family(root, *wanted):
    have = set(tkfont.families(root))
    for name in wanted:
        if name in have:
            return name
    return "TkDefaultFont"


def apply(root):
    """Theme `root` and return (style, fonts). Call once, before building."""
    p = PALETTE
    family = _pick_family(root, FONT_FAMILY, "Inter", "DejaVu Sans")
    head = _pick_family(root, HEAD_FAMILY, family)
    hw = "normal" if head == HEAD_FAMILY else "bold"
    mono = _pick_family(root, MONO_FAMILY, "Consolas", "DejaVu Sans Mono", "Courier New")

    fonts = {
        "body": tkfont.Font(root=root, family=family, size=10),
        "small": tkfont.Font(root=root, family=family, size=9),
        "eyebrow": tkfont.Font(root=root, family=family, size=8, weight="bold"),
        "strong": tkfont.Font(root=root, family=head, size=10, weight=hw),
        "h1": tkfont.Font(root=root, family=head, size=18, weight=hw),
        "h2": tkfont.Font(root=root, family=head, size=11, weight=hw),
        "stat": tkfont.Font(root=root, family=head, size=22, weight=hw),
        "brand": tkfont.Font(root=root, family=family, size=17, weight="bold"),
        "mono": tkfont.Font(root=root, family=mono, size=9),
    }
    # The implicit default every unstyled widget inherits.
    tkfont.nametofont("TkDefaultFont").configure(family=family, size=10)

    style = ttk.Style(root)
    style.theme_use("clam")
    root.configure(background=p["bg"])
    # A readonly combobox drops down a plain tk Listbox, which ttk never sees:
    # without these it opens as a white Windows list in the dark window.
    for key, value in (("background", p["raised"]), ("foreground", p["text"]),
                       ("selectBackground", p["select"]), ("selectForeground", "#ffffff"),
                       ("font", fonts["body"]), ("borderWidth", 0)):
        root.option_add("*TCombobox*Listbox." + key, value)

    style.configure(".", background=p["bg"], foreground=p["text"],
                    fieldbackground=p["raised"], bordercolor=p["line"],
                    lightcolor=p["line"], darkcolor=p["line"],
                    troughcolor=p["panel"], focuscolor=p["accent"],
                    font=fonts["body"])

    # -- frames and labels -------------------------------------------------
    style.configure("TFrame", background=p["bg"])
    style.configure("Panel.TFrame", background=p["panel"])
    style.configure("Card.TFrame", background=p["panel"], relief="flat",
                    borderwidth=1)
    style.configure("Raised.TFrame", background=p["raised"])
    style.configure("Line.TFrame", background=p["line"])
    style.configure("Bar.TFrame", background=p["panel"])     # the play bar along the bottom
    # Thin colour strips: brand mark, selected nav item, a card's state.
    for name in ("accent", "ok", "warn", "danger"):
        style.configure(name.capitalize() + ".TFrame", background=p[name])

    style.configure("TLabel", background=p["bg"], foreground=p["text"])
    style.configure("Panel.TLabel", background=p["panel"], foreground=p["text"])
    style.configure("H1.TLabel", background=p["bg"], foreground=p["text"],
                    font=fonts["h1"])
    style.configure("H2.TLabel", background=p["panel"], foreground=p["text"],
                    font=fonts["h2"])
    style.configure("Muted.TLabel", background=p["bg"], foreground=p["muted"],
                    font=fonts["small"])
    style.configure("PanelMuted.TLabel", background=p["panel"],
                    foreground=p["muted"], font=fonts["small"])
    style.configure("Mono.TLabel", background=p["panel"], foreground=p["dim"],
                    font=fonts["mono"])
    style.configure("Flag.TLabel", background=p["panel"], foreground=p["warn"],
                    font=fonts["small"])
    style.configure("Brand.TLabel", background=p["bg"], foreground=p["text"],
                    font=fonts["brand"])
    style.configure("Eyebrow.TLabel", background=p["bg"], foreground=p["dim"],
                    font=fonts["eyebrow"])
    style.configure("PanelEyebrow.TLabel", background=p["panel"], foreground=p["dim"],
                    font=fonts["eyebrow"])
    style.configure("Stat.TLabel", background=p["panel"], foreground=p["text"],
                    font=fonts["stat"])
    style.configure("Accent.TLabel", background=p["bg"],
                    foreground=p["accent"], font=fonts["strong"])
    style.configure("Ok.TLabel", background=p["bg"], foreground=p["ok"])
    style.configure("Warn.TLabel", background=p["bg"], foreground=p["warn"])
    style.configure("Danger.TLabel", background=p["bg"], foreground=p["danger"])
    style.configure("PanelOk.TLabel", background=p["panel"], foreground=p["ok"],
                    font=fonts["strong"])
    style.configure("PanelWarn.TLabel", background=p["panel"], foreground=p["warn"],
                    font=fonts["strong"])
    style.configure("PanelDanger.TLabel", background=p["panel"],
                    foreground=p["danger"], font=fonts["strong"])

    # A pill that names the engine behind the selected title.
    style.configure("Badge.TLabel", background=p["raised"],
                    foreground=p["text"], font=fonts["small"],
                    padding=(9, 3))

    # -- buttons -----------------------------------------------------------
    # focusthickness 1: Tab through the window and you can see where you are.
    style.configure("TButton", background=p["raised"], foreground=p["text"],
                    borderwidth=0, focusthickness=1, focuscolor=p["focus"],
                    padding=(14, 7), relief="flat", anchor="center")
    style.map("TButton",
              background=[("disabled", p["panel"]), ("pressed", p["line"]),
                          ("active", p["line"])],
              foreground=[("disabled", p["dim"])])

    style.configure("Accent.TButton", background=p["accent"],
                    foreground="#ffffff", font=fonts["strong"],
                    padding=(20, 8), borderwidth=0, relief="flat")
    style.map("Accent.TButton",
              background=[("disabled", "#4a2028"), ("pressed", p["accent"]),
                          ("active", p["accent_hi"])],
              foreground=[("disabled", p["dim"])])

    # Secondary actions: quieter than TButton, but never so grey they read as disabled.
    style.configure("Ghost.TButton", background=p["panel"],
                    foreground=p["text"], padding=(12, 6), borderwidth=0, width=0)
    style.map("Ghost.TButton",
              background=[("disabled", p["bg"]), ("active", p["raised"])],
              foreground=[("disabled", p["dim"]), ("active", p["text"])])

    # The section tabs along the top: text only, the selected one bright and
    # underlined by a 2 px accent strip the UI places below it.
    style.configure("Tab.TButton", background=p["bg"], foreground=p["muted"],
                    padding=(14, 9), borderwidth=0, width=0)   # clam: min 11 chars otherwise
    style.map("Tab.TButton",
              background=[("active", p["bg"])],
              foreground=[("active", p["text"])])
    style.configure("TabSelected.TButton", background=p["bg"],
                    foreground=p["text"], padding=(14, 9), borderwidth=0, width=0,
                    font=fonts["strong"])
    style.map("TabSelected.TButton", background=[("active", p["bg"])])

    # The one big call to action, in the play bar.
    style.configure("Play.TButton", background=p["accent"], foreground="#ffffff",
                    font=fonts["h2"], padding=(28, 9), borderwidth=0, relief="flat")
    style.map("Play.TButton",
              background=[("disabled", "#4a2028"), ("pressed", p["accent"]),
                          ("active", p["accent_hi"])],
              foreground=[("disabled", p["dim"])])

    # -- inputs ------------------------------------------------------------
    for name in ("TEntry", "TCombobox"):
        style.configure(name, fieldbackground=p["raised"],
                        background=p["raised"], foreground=p["text"],
                        bordercolor=p["line"], insertcolor=p["text"],
                        arrowcolor=p["muted"], padding=6, relief="flat")
        style.map(name,
                  fieldbackground=[("readonly", p["raised"]),
                                   ("disabled", p["panel"])],
                  foreground=[("disabled", p["dim"])],
                  bordercolor=[("focus", p["accent"])])
    style.configure("Search.TEntry", fieldbackground=p["panel"], padding=8)

    # The tick used to be drawn in accent on an accent box: ticked looked like
    # a red square with nothing in it. White tick, bigger box.
    style.configure("TCheckbutton", background=p["panel"],
                    foreground=p["text"], focuscolor=p["focus"],
                    indicatorbackground=p["raised"], indicatorforeground="#ffffff",
                    indicatorsize=15, indicatormargin=(0, 0, 6, 0),
                    upperbordercolor=p["line"], lowerbordercolor=p["line"],
                    padding=2)
    style.map("TCheckbutton",
              background=[("active", p["panel"])],
              indicatorbackground=[("selected", p["accent"]),
                                   ("active", p["line"])],
              upperbordercolor=[("selected", p["accent"])],
              lowerbordercolor=[("selected", p["accent"])])
    style.configure("Plain.TCheckbutton", background=p["bg"])
    style.map("Plain.TCheckbutton", background=[("active", p["bg"])])

    # -- notebook ----------------------------------------------------------
    style.configure("TNotebook", background=p["bg"], borderwidth=0,
                    tabmargins=(0, 6, 0, 0))
    style.configure("TNotebook.Tab", background=p["bg"],
                    foreground=p["muted"], padding=(18, 9), borderwidth=0,
                    font=fonts["body"])
    style.map("TNotebook.Tab",
              background=[("selected", p["panel"]), ("active", p["raised"])],
              foreground=[("selected", p["text"]), ("active", p["text"])],
              expand=[("selected", (0, 0, 0, 0))])
    # The section tabs are Tab.TButtons the UI lays out itself (with an accent
    # underline ttk tabs cannot draw). The notebook still manages the pages,
    # but its own tab strip and border are hidden.
    style.configure("Content.TNotebook", background=p["bg"], borderwidth=0,
                    bordercolor=p["bg"], lightcolor=p["bg"], darkcolor=p["bg"],
                    tabmargins=0, padding=0)
    style.layout("Content.TNotebook.Tab", [])

    # -- tree, scrollbar, progress ----------------------------------------
    style.configure("Treeview", background=p["panel"],
                    fieldbackground=p["panel"], foreground=p["text"],
                    borderwidth=0, rowheight=30)
    style.map("Treeview",
              background=[("selected", p["select"])],
              foreground=[("selected", "#ffffff")])
    style.configure("Treeview.Heading", background=p["panel"],
                    foreground=p["dim"], borderwidth=0,
                    font=fonts["eyebrow"], padding=(8, 7), relief="flat")
    style.map("Treeview.Heading", background=[("active", p["raised"])])

    # A thumb in a trough, no arrow buttons: nobody clicks those any more.
    style.layout("Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {
        "sticky": "ns", "children": [("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
    style.configure("Vertical.TScrollbar", background=p["line"],
                    troughcolor=p["bg"], bordercolor=p["bg"],
                    lightcolor=p["line"], darkcolor=p["line"],
                    width=10, borderwidth=0, relief="flat", gripcount=0)
    style.map("Vertical.TScrollbar", background=[("active", p["dim"])],
              lightcolor=[("active", p["dim"])], darkcolor=[("active", p["dim"])])

    style.configure("TProgressbar", background=p["accent"],
                    troughcolor=p["raised"], borderwidth=0, thickness=3)
    style.configure("TSeparator", background=p["line"])
    style.configure("TLabelframe", background=p["panel"],
                    bordercolor=p["line"], borderwidth=1, relief="flat")
    style.configure("TLabelframe.Label", background=p["panel"],
                    foreground=p["muted"], font=fonts["h2"])

    return style, fonts


def style_text(widget, kind="body", fonts=None):
    """Colour a plain tk.Text or tk.Canvas, which ttk never touches."""
    p = PALETTE
    shade = {"canvas": p["bg"], "input": p["raised"]}.get(kind, p["panel"])
    options = {
        "background": shade,
        "highlightthickness": 0,
        "borderwidth": 0,
    }
    if kind != "canvas":
        options.update(foreground=p["text"], insertbackground=p["text"],
                       selectbackground=p["select"], selectforeground="#ffffff",
                       padx=10, pady=8)
        if fonts:
            options["font"] = fonts["body" if kind == "body" else "mono"]
    widget.configure(**options)
