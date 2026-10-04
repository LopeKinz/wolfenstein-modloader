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
    "ok": "#43c978",
    "warn": "#e4ad3a",
    "danger": "#ff6078",
}

FONT_FAMILY = "Segoe UI"
MONO_FAMILY = "Consolas"


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
    mono = _pick_family(root, MONO_FAMILY, "DejaVu Sans Mono", "Courier New")

    fonts = {
        "body": tkfont.Font(root=root, family=family, size=10),
        "small": tkfont.Font(root=root, family=family, size=9),
        "strong": tkfont.Font(root=root, family=family, size=10, weight="bold"),
        "h1": tkfont.Font(root=root, family=family, size=17, weight="bold"),
        "h2": tkfont.Font(root=root, family=family, size=11, weight="bold"),
        "mono": tkfont.Font(root=root, family=mono, size=9),
    }
    # The implicit default every unstyled widget inherits.
    tkfont.nametofont("TkDefaultFont").configure(family=family, size=10)

    style = ttk.Style(root)
    style.theme_use("clam")
    root.configure(background=p["bg"])

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
    style.configure("TButton", background=p["raised"], foreground=p["text"],
                    borderwidth=0, focusthickness=0, padding=(14, 7),
                    relief="flat", anchor="center")
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

    style.configure("Ghost.TButton", background=p["panel"],
                    foreground=p["muted"], padding=(10, 5), borderwidth=0)
    style.map("Ghost.TButton",
              background=[("active", p["raised"])],
              foreground=[("active", p["text"])])

    style.configure("Nav.TButton", background=p["bg"], foreground=p["muted"],
                    padding=(16, 11), borderwidth=0, anchor="w")
    style.map("Nav.TButton",
              background=[("active", p["raised"])],
              foreground=[("active", p["text"])])
    style.configure("NavSelected.TButton", background=p["raised"],
                    foreground=p["text"], padding=(16, 11), borderwidth=0,
                    anchor="w", font=fonts["strong"])
    style.map("NavSelected.TButton",
              background=[("active", p["raised"])],
              foreground=[("active", p["text"])])

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

    style.configure("TCheckbutton", background=p["panel"],
                    foreground=p["text"], focuscolor=p["panel"],
                    indicatorbackground=p["raised"],
                    indicatorforeground=p["accent"], padding=2)
    style.map("TCheckbutton",
              background=[("active", p["panel"])],
              indicatorbackground=[("selected", p["accent"]),
                                   ("active", p["line"])])

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
    # Main UI navigation lives in a persistent left rail. The notebook still
    # provides robust page management, but its duplicate tab strip is hidden.
    style.configure("Content.TNotebook", background=p["bg"], borderwidth=0,
                    tabmargins=0)
    style.layout("Content.TNotebook.Tab", [])

    # -- tree, scrollbar, progress ----------------------------------------
    style.configure("Treeview", background=p["panel"],
                    fieldbackground=p["panel"], foreground=p["text"],
                    borderwidth=0, rowheight=30)
    style.map("Treeview",
              background=[("selected", p["accent"])],
              foreground=[("selected", "#ffffff")])
    style.configure("Treeview.Heading", background=p["raised"],
                    foreground=p["muted"], borderwidth=0,
                    font=fonts["small"], padding=(8, 6), relief="flat")
    style.map("Treeview.Heading", background=[("active", p["line"])])

    style.configure("Vertical.TScrollbar", background=p["raised"],
                    troughcolor=p["bg"], bordercolor=p["bg"],
                    arrowcolor=p["muted"], width=12, borderwidth=0,
                    relief="flat")
    style.map("Vertical.TScrollbar", background=[("active", p["line"])])

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
                       selectbackground=p["accent"], selectforeground="#ffffff",
                       padx=10, pady=8)
        if fonts:
            options["font"] = fonts["body" if kind == "body" else "mono"]
    widget.configure(**options)
