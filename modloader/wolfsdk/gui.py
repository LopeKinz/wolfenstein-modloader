"""Tkinter loader UI: tweak cheats/QoL, manage mods, launch either game.

Two titles share this window and they are not alike. The New Order brings 31
hand-curated tweaks with measured retail behaviour; The New Colossus brings
3116 mined cvar names and no measurements. So the tweak page is not a fixed
form any more -- it is a filtered view over whichever catalogue the selected
title provides, and it is rebuilt when the title changes.

Everything visual lives in theme.py. This file only asks for style names.
"""

import importlib.util
import queue
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import SUPPORT_URL, cvars, mapinstall, mod as modlib, theme
from .game import (Game, GameError, load_config, save_config,
                   steam_is_running)
from .patch import PatchError
from .tncpatch import TncPatchError

ROOT = Path(__file__).resolve().parent.parent
MODS_DIR = ROOT / "mods"

PAD = 10
GUTTER = 20            # left/right edge of header, tabs, pages and play bar
PAGE = (GUTTER, 16)    # page padding
# How many tweak rows to build at once. The full New Colossus catalogue is
# 3116 entries; building them all makes the window take seconds to appear,
# and nobody scrolls through two thousand rows anyway -- they search.
ROW_LIMIT = 120


PLACEHOLDER = "Search cvar or text…"
# The mod list's "on" column. Off is a glyph too: an empty cell showed no
# switch at all. Anything but "" and OFF counts as on (verify_gui ticks with "x").
ON, OFF = "●", "○"


def _placeholder(entry, var, text):
    """Grey hint text that disappears as soon as the box is used."""
    def show():
        if not var.get():
            var.set(text)
            entry.configure(foreground=theme.PALETTE["dim"])

    def hide(_event=None):
        if var.get() == text:
            var.set("")
        entry.configure(foreground=theme.PALETTE["text"])

    entry.bind("<FocusIn>", hide)
    entry.bind("<FocusOut>", lambda e: show())
    show()


def _autowrap(label, pady=0):
    """Pack a label across its parent and re-wrap it to its width: ttk has no
    wrap-to-fit, and a fixed wraplength is wrong in every other window size."""
    label.pack(anchor="w", fill="x", pady=pady)
    label.bind("<Configure>", lambda e: label.configure(wraplength=max(120, e.width)))
    return label


class ScrollFrame(ttk.Frame):
    """A vertically scrollable container. `body` is where children go."""

    def __init__(self, parent, style="TFrame"):
        super().__init__(parent, style=style)
        canvas = tk.Canvas(self, highlightthickness=0)
        theme.style_text(canvas, "canvas")
        canvas.configure(background=theme.PALETTE["bg"])
        scroll = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self.body = ttk.Frame(canvas, style=style)
        window = canvas.create_window((0, 0), window=self.body, anchor="nw")

        self.body.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.canvas = canvas

        def wheel(event):
            canvas.yview_scroll(-1 * (event.delta // 120), "units")

        canvas.bind_all("<MouseWheel>", wheel)


class App(ttk.Frame):
    def __init__(self, master, game_path=None, fonts=None):
        super().__init__(master)   # no padding: header, tabs and play bar run edge to edge
        self.pack(fill="both", expand=True)
        self.fonts = fonts or {}
        self.config_data = load_config()
        self.game = None
        self.tweak_vars = {}
        self.mod_vars = {}
        self.tweak_rows = None
        self.queue = queue.Queue()
        self.busy = False

        self._build_header()
        # Footer before the notebook on purpose: the packer serves widgets in
        # pack order, and the notebook expands into everything left over. Pack
        # the button bar afterwards and it is handed zero height -- present in
        # the widget tree, invisible on screen.
        self._build_footer()
        # Sections as tabs across the full width, Vortex style; the notebook
        # below keeps its own tab strip hidden and only manages the pages.
        self.tabbar = ttk.Frame(self, padding=(GUTTER - 14, 0, GUTTER, 0))
        self.tabbar.pack(fill="x")
        ttk.Frame(self, style="Line.TFrame", height=1).pack(fill="x")
        self.notebook = ttk.Notebook(self, style="Content.TNotebook")
        self.notebook.pack(fill="both", expand=True)
        self._build_dashboard_tab()
        self._build_tweaks_tab()
        self._build_mods_tab()
        self._build_studio_tab()
        self._build_info_tab()
        self._build_navigation()

        self._resolve_game(game_path, quiet=True)
        self.after(120, self._drain)

    # -- header ------------------------------------------------------------

    def _build_header(self):
        # One row: brand | game, engine, path ... folder button. The path is
        # packed last, so a narrow window clips it and nothing else.
        bar = ttk.Frame(self, padding=(GUTTER, 14, GUTTER, 6))
        bar.pack(fill="x")
        ttk.Frame(bar, style="Accent.TFrame", width=4).pack(side="left", fill="y", padx=(0, 10))
        ttk.Label(bar, text="WOLFSDK", style="Brand.TLabel").pack(side="left")
        ttk.Frame(bar, style="Line.TFrame", width=1).pack(side="left", fill="y", padx=16, pady=6)

        ttk.Button(bar, text="Choose folder…", style="Ghost.TButton",
                   command=self._choose_game).pack(side="right")

        # Both Wolfensteins can be installed at once, and they are different
        # engines -- which one is selected decides what the rest of the
        # window is even allowed to do.
        self.installs = {g.title.name: g for g in Game.find_all()}
        self.title_var = tk.StringVar()
        self.title_box = ttk.Combobox(
            bar, textvariable=self.title_var, state="readonly", width=28,
            values=list(self.installs),
        )
        self.title_box.pack(side="left")
        self.title_box.bind("<<ComboboxSelected>>", self._switch_title)
        self.badge_var = tk.StringVar(value="")
        self.badge = ttk.Label(bar, textvariable=self.badge_var,
                               style="Badge.TLabel")
        self.badge.pack(side="left", padx=(8, 0))
        self.path_var = tk.StringVar(value="(not found)")
        ttk.Label(bar, textvariable=self.path_var,
                  style="Muted.TLabel").pack(side="left", padx=(12, PAD))

    def _switch_title(self, _event=None):
        game = self.installs.get(self.title_var.get())
        if game:
            self._resolve_game(str(game.root))

    def _choose_game(self):
        chosen = filedialog.askdirectory(title="Choose the Wolfenstein install folder")
        if chosen:
            self._resolve_game(chosen)

    def _resolve_game(self, path=None, quiet=False):
        try:
            self.game = Game.find(path)
            self.game.remember()
            self.installs[self.game.title.name] = self.game
            self.title_box.configure(values=list(self.installs))
            self.title_var.set(self.game.title.name)
            self.badge_var.set(self.game.title.engine)
            self.path_var.set(str(self.game.root))
            self._set_mod_controls()
            self._rebuild_tweaks()
            self._reload_mods()
            self._refresh_status()
            self._refresh_dashboard()
        except GameError as exc:
            self.game = None
            self.path_var.set("(not found)")
            self.badge_var.set("")
            self._refresh_dashboard()
            if not quiet:
                messagebox.showerror("Game not found", str(exc))

    @property
    def title_key(self):
        return self.game.title.key if self.game else "tno"

    def _install(self):
        """The patcher for the selected title.

        Part one and part two differ in archive format, write budget and
        backup strategy, so this is a switch and not a shared interface.
        """
        return modlib.installation_for(self.game)

    # -- dashboard and navigation -----------------------------------------

    def _build_dashboard_tab(self):
        page = ttk.Frame(self.notebook, padding=PAGE)
        self.notebook.add(page, text="Overview")

        # The strip on the left takes the state's colour, so the card reads
        # green / amber / red before a word of it is read.
        card = ttk.Frame(page, style="Card.TFrame")
        card.pack(fill="x")
        self.dashboard_strip = ttk.Frame(card, style="Warn.TFrame", width=4)
        self.dashboard_strip.pack(side="left", fill="y")
        hero = ttk.Frame(card, style="Panel.TFrame", padding=(20, 16))
        hero.pack(side="left", fill="x", expand=True)
        ttk.Label(hero, text="INSTALL STATUS",
                  style="PanelEyebrow.TLabel").pack(anchor="w")
        self.dashboard_state_var = tk.StringVar(value="Looking for the game…")
        self.dashboard_state_label = ttk.Label(
            hero, textvariable=self.dashboard_state_var, style="PanelWarn.TLabel")
        self.dashboard_state_label.pack(anchor="w", pady=(5, 2))
        self.dashboard_hint_var = tk.StringVar(value="")
        _autowrap(ttk.Label(hero, textvariable=self.dashboard_hint_var,
                            style="PanelMuted.TLabel", justify="left"))

        # Actions and the custom map side by side, above the numbers: in a
        # short window the numbers clip, not the buttons. This page does not
        # scroll.
        cols = ttk.Frame(page)
        cols.pack(fill="x", pady=(PAD, 0))
        cols.columnconfigure((0, 1), weight=1, uniform="col")

        actions = ttk.Frame(cols, style="Card.TFrame", padding=(20, 16))
        actions.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        ttk.Label(actions, text="Quick actions", style="H2.TLabel").pack(anchor="w")
        _autowrap(ttk.Label(actions, style="PanelMuted.TLabel", justify="left",
                            text="Check first, then apply. Start game sits in the bar at the bottom."),
                  pady=(2, PAD))
        row = ttk.Frame(actions, style="Panel.TFrame")
        row.pack(fill="x", side="bottom")
        ttk.Button(row, text="Manage mods",
                   command=lambda: self._navigate(2)).pack(side="left")
        self.dashboard_check_btn = ttk.Button(
            row, text="Check mod set", command=self._check_mods)
        self.dashboard_check_btn.pack(side="left", padx=(6, 0))
        self.action_buttons.append(self.dashboard_check_btn)

        # A custom map is no mod: it is its own DLC folder (mapinstall.py).
        # Buttons in their own row, so a long status line cannot shove them out.
        mapcard = ttk.Frame(cols, style="Card.TFrame", padding=(20, 16))
        mapcard.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        ttk.Label(mapcard, text="Custom map", style="H2.TLabel").pack(anchor="w")
        self.map_var = tk.StringVar(value="")
        _autowrap(ttk.Label(mapcard, textvariable=self.map_var,
                            style="PanelMuted.TLabel", justify="left"), pady=(2, PAD))
        maprow = ttk.Frame(mapcard, style="Panel.TFrame")
        maprow.pack(fill="x", side="bottom")
        self.map_note = ""
        self.map_buttons = [
            ttk.Button(maprow, text="Remove map", command=self._map_uninstall),
            ttk.Button(maprow, text="Install map…", command=self._map_install),
        ]
        for button in self.map_buttons:
            button.pack(side="right", padx=(6, 0))
        self.action_buttons.extend(self.map_buttons)

        stats = ttk.Frame(page)
        stats.pack(fill="x", pady=(PAD, 0))
        for column in range(3):
            stats.columnconfigure(column, weight=1, uniform="stat")
        self.dashboard_mods_var = tk.StringVar(value="0")
        self.dashboard_tweaks_var = tk.StringVar(value="0")
        self.dashboard_assets_var = tk.StringVar(value="—")
        for column, title, variable, caption in (
            (0, "ACTIVE MODS", self.dashboard_mods_var, "current selection"),
            (1, "TWEAKS", self.dashboard_tweaks_var, "cvars set"),
            (2, "ARCHIVES", self.dashboard_assets_var, "patch support"),
        ):
            card = ttk.Frame(stats, style="Card.TFrame", padding=(20, 14))
            card.grid(row=0, column=column, sticky="nsew",
                      padx=(0 if column == 0 else 5, 0 if column == 2 else 5))
            ttk.Label(card, text=title, style="PanelEyebrow.TLabel").pack(anchor="w")
            ttk.Label(card, textvariable=variable,
                      style="Stat.TLabel").pack(anchor="w", pady=(2, 0))
            ttk.Label(card, text=caption, style="PanelMuted.TLabel").pack(anchor="w")

        safety = ttk.Frame(page, style="Card.TFrame", padding=(20, 12))
        safety.pack(fill="x", pady=(PAD, 0))
        ttk.Label(safety, text="Safety net", style="H2.TLabel").pack(side="left")
        ttk.Label(
            safety,
            text="Local and offline, no DLL injection. A journal, bit-exact backups "
                 "and a write lock protect your install.",
            style="PanelMuted.TLabel").pack(side="left", padx=(PAD, 0))

    def _build_navigation(self):
        labels = ("Overview", "Cheats & QoL", "Mods", "Studio", "System & Info")
        self.nav_buttons, self.nav_marks = [], []
        for index, label in enumerate(labels):
            item = ttk.Frame(self.tabbar)
            item.pack(side="left")
            button = ttk.Button(
                item, text=label, style="Tab.TButton", takefocus=True,
                command=lambda i=index: self._navigate(i))
            button.pack(fill="x")
            mark = ttk.Frame(item, height=2)   # the underline of the selected tab
            mark.pack(fill="x", padx=14)
            self.nav_buttons.append(button)
            self.nav_marks.append(mark)
        self.notebook.bind("<<NotebookTabChanged>>", self._sync_navigation)
        self._navigate(0)

    def _navigate(self, index):
        self.notebook.select(index)
        self._sync_navigation()

    def _sync_navigation(self, _event=None):
        if not hasattr(self, "nav_buttons"):
            return
        current = self.notebook.index(self.notebook.select())
        for index, (button, mark) in enumerate(zip(self.nav_buttons, self.nav_marks)):
            on = index == current
            button.configure(style="TabSelected.TButton" if on else "Tab.TButton")
            mark.configure(style="Accent.TFrame" if on else "TFrame")

    def _dash(self, tone, state, hint):
        """The install-status card: tone is ok, warn or danger."""
        self.dashboard_state_var.set(state)
        self.dashboard_hint_var.set(hint)
        self.dashboard_state_label.configure(style="Panel%s.TLabel" % tone.capitalize())
        self.dashboard_strip.configure(style="%s.TFrame" % tone.capitalize())
        self.status_dot.configure(style="Panel%s.TLabel" % tone.capitalize())

    def _refresh_dashboard(self):
        if not hasattr(self, "dashboard_state_var"):
            return
        self._refresh_map()
        if not self.game:
            self._dash("danger", "No game install found", "Choose a game folder at the top right.")
            self.dashboard_assets_var.set("NOT READY")
            return
        active = len(self._enabled_mods()) if hasattr(self, "tree") else 0
        tweaks = len(self._collect_tweaks()) if hasattr(self, "extra_text") else 0
        self.dashboard_mods_var.set(str(active))
        self.dashboard_tweaks_var.set(str(tweaks))
        self.dashboard_assets_var.set("READY" if self.game.supports_mods else "LAUNCH ONLY")
        if not self.game.supports_mods:
            self._dash("warn", "%s is ready to launch" % self.game.title.name,
                       "Cvars work; archive mods are disabled for this game.")
            return
        install = self._install()
        try:
            journal = install.read_journal()
            if journal and journal.get("state") == "applying":
                self._dash("danger", "Recovery needed",
                           "The last patch run was interrupted. Choose Revert now.")
            elif journal:
                self._dash("ok", "Mod set active · backup in place",
                           "%d mod(s) with %d asset(s) are installed in the game."
                           % (len(journal.get("mods", [])), len(journal.get("assets", []))))
            else:
                self._dash("ok", "Original state · ready",
                           "No archive mods installed. A check runs without writing anything.")
        except Exception as exc:  # noqa: BLE001
            self._dash("danger", "Could not read the status", str(exc))
        finally:
            install.close()

    def _refresh_map(self):
        tnc = bool(self.game) and self.game.title.key == "tnc"
        for button in self.map_buttons:
            button.configure(state="normal" if tnc and not self.busy else "disabled")
        if not tnc:
            self.map_var.set("Custom map: only for Wolfenstein II: The New Colossus.")
            return
        try:
            st = mapinstall.status(self.game)
            text = mapinstall.describe(st)[1 if st["dlc"] else 0]
            if st["installiert"]:
                text += "  Source: %s" % Path(st["installiert"]["quelle"]).parent.name
        except Exception as exc:  # noqa: BLE001
            text = "Custom map: could not read the status (%s)" % exc
        self.map_var.set(text)

    def _map_install(self):
        path = filedialog.askopenfilename(
            title="Choose the map container (chunk_22.resources)",
            filetypes=[("Map container", "*.resources"), ("All files", "*.*")])
        if path:
            self.status_var.set("Installing map…")
            self._run_bg(self._map_worker, mapinstall.install, self.game, path,
                         then=self._map_done)

    def _map_uninstall(self):
        if messagebox.askyesno(
                "Remove map",
                "Remove the custom map? c2v1 will then load the "
                "original map from the DLC again."):
            self.status_var.set("Removing map…")
            self._run_bg(self._map_worker, mapinstall.uninstall, self.game,
                         then=self._map_done)

    def _map_worker(self, fn, *args):
        """Off the Tk thread. Refusals come back as text, for _map_done."""
        closed = self._close_studio()
        try:
            self.map_note = fn(*args)
        except (modlib.ModError, OSError) as exc:
            self.map_note = "Error: %s" % exc
        if closed:
            self.map_note += "\n\nThe Studio was closed for this; reopen it if you need it."
        return self.map_note

    @staticmethod
    def _close_studio():
        """Stop this process's Studio server, if one runs. It keeps map
        archives open -- dlc_0's chunk too -- and Windows then refuses to
        swap or delete them. True when it was running."""
        server = sys.modules.get(__package__ + ".mapserver")   # never started: never imported
        if server is None or getattr(server, "_server", None) is None:
            return False
        server.stop()
        return True

    def _map_done(self):
        failed = self.map_note.startswith("Error")
        self.status_var.set("Map: " + ("refused." if failed else "done."))
        (messagebox.showerror if failed else messagebox.showinfo)("Custom map", self.map_note)

    # -- tweaks tab --------------------------------------------------------

    def _build_tweaks_tab(self):
        outer = ttk.Frame(self.notebook, padding=PAGE)
        self.notebook.add(outer, text="  Cheats & QoL  ")

        bar = ttk.Frame(outer)
        bar.pack(fill="x")
        self.filter_var = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self.filter_var,
                          style="Search.TEntry", width=30)
        entry.pack(side="left")
        self.filter_var.trace_add("write", lambda *_: self._rebuild_tweaks())
        # An empty box next to 3116 hidden entries tells nobody what to do
        # with it. ttk has no placeholder, so it is a grey value that steps
        # aside on focus -- and _rebuild_tweaks has to ignore it.
        _placeholder(entry, self.filter_var, PLACEHOLDER)

        self.group_var = tk.StringVar(value="All")
        self.group_box = ttk.Combobox(bar, textvariable=self.group_var,
                                      state="readonly", width=20)
        self.group_box.pack(side="left", padx=(6, 0))
        self.group_box.bind("<<ComboboxSelected>>",
                            lambda e: self._rebuild_tweaks())

        self.only_set = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="only set", variable=self.only_set, style="Plain.TCheckbutton",
                        command=self._rebuild_tweaks).pack(side="left", padx=(PAD, 0))

        self.count_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.count_var,
                  style="Muted.TLabel").pack(side="right")

        # The New Colossus only; _rebuild_tweaks shows or hides it per title.
        self.preset_row = ttk.Frame(outer)
        ttk.Label(self.preset_row, text="Presets", style="Muted.TLabel").pack(side="left")
        self.preset_var = tk.StringVar(value=next(iter(cvars.TNC_PRESETS)))
        ttk.Combobox(self.preset_row, textvariable=self.preset_var, state="readonly",
                     width=14, values=list(cvars.TNC_PRESETS)).pack(side="left", padx=(6, 0))
        ttk.Button(self.preset_row, text="Apply preset",
                   command=self._apply_preset).pack(side="left", padx=(6, 0))
        ttk.Button(self.preset_row, text="Reset preset",
                   command=lambda: self._apply_preset(reset=True)).pack(side="left", padx=(6, 0))
        ttk.Label(self.preset_row, style="Muted.TLabel",
                  text="Not yet confirmed in game. Photo Mode = screenshot quality only."
                  ).pack(side="left", padx=(PAD, 0))
        self.preset_undo = {}

        # Same packing lesson as the footer: the card below is packed first
        # so it is served its height, and the scroller then expands into
        # whatever is left. The other way round the list gets squeezed.
        extra = ttk.Frame(outer, style="Card.TFrame", padding=PAD)
        extra.pack(side="bottom", fill="x", pady=(PAD, 0))

        self.tweak_scroll = ScrollFrame(outer)
        self.tweak_scroll.pack(fill="both", expand=True, pady=(PAD, 0))
        self.tweak_rows = self.tweak_scroll.body
        ttk.Label(extra, text="Custom cvars", style="H2.TLabel").pack(anchor="w")
        ttk.Label(extra, style="PanelMuted.TLabel",
                  text="One assignment per line, e.g.  pm_runspeed 400").pack(anchor="w")
        self.extra_text = tk.Text(extra, height=2)
        theme.style_text(self.extra_text, "input", self.fonts)
        self.extra_text.pack(fill="x", pady=(6, 0))
        self.extra_text.insert("1.0", self.config_data.get("extra_cvars", ""))

    def _saved_tweaks(self, title_key=None):
        """Stored values for one title. Old configs kept a single dict."""
        key = title_key or self.title_key
        by_title = self.config_data.get("tweaks_by_title")
        if isinstance(by_title, dict) and key in by_title:
            return dict(by_title[key])
        if key == "tno":
            return dict(self.config_data.get("tweaks") or {})
        return {}

    def _ensure_tweak_vars(self):
        """One variable per cvar of the current title, created once.

        They outlive the rows: filtering destroys and rebuilds the widgets,
        and a value typed before a search must still be there after it.
        """
        saved = self._saved_tweaks()
        for tweak in cvars.catalog_for(self.title_key):
            if tweak.key not in self.tweak_vars:
                self.tweak_vars[tweak.key] = tk.StringVar(
                    value=saved.get(tweak.key, ""))

    def _rebuild_tweaks(self):
        if self.tweak_rows is None:
            return
        self._ensure_tweak_vars()
        catalog = cvars.catalog_for(self.title_key)
        if self.title_key == "tnc":
            self.preset_row.pack(fill="x", pady=(6, 0), before=self.tweak_scroll)
        else:
            self.preset_row.pack_forget()

        groups =["All"] + list(cvars.categories_for(self.title_key))
        if list(self.group_box["values"]) != groups:
            self.group_box.configure(values=groups)
            if self.group_var.get() not in groups:
                self.group_var.set("All")

        needle = self.filter_var.get().strip().lower()
        if needle == PLACEHOLDER.lower():
            needle = ""
        group = self.group_var.get()
        matches = []
        for tweak in catalog:
            if group != "All" and tweak.category != group:
                continue
            if self.only_set.get() and not self.tweak_vars[tweak.key].get().strip():
                continue
            if needle and needle not in tweak.key.lower() \
                    and needle not in tweak.label.lower() \
                    and needle not in (tweak.note or "").lower():
                continue
            matches.append(tweak)

        for child in self.tweak_rows.winfo_children():
            child.destroy()

        shown = matches[:ROW_LIMIT]
        current = None
        for tweak in shown:
            if tweak.category != current:
                current = tweak.category
                head = ttk.Frame(self.tweak_rows)
                head.pack(fill="x", pady=(PAD + 4, 4))
                ttk.Label(head, text=current.upper(),
                          style="Accent.TLabel").pack(side="left")
                ttk.Frame(head, style="Line.TFrame", height=1).pack(
                    side="left", fill="x", expand=True, padx=(PAD, 0))
            self._add_tweak_row(self.tweak_rows, tweak)

        if len(matches) > ROW_LIMIT:
            ttk.Label(self.tweak_rows, style="Muted.TLabel",
                      text="… and %d more. Use the search box."
                           % (len(matches) - ROW_LIMIT)).pack(anchor="w", pady=PAD)
        if not matches:
            ttk.Label(self.tweak_rows, style="Muted.TLabel",
                      text="Nothing found.").pack(anchor="w", pady=PAD)

        self.count_var.set("%d of %d cvars" % (len(matches), len(catalog)))

    def _add_tweak_row(self, parent, tweak):
        row = ttk.Frame(parent, style="Card.TFrame", padding=(PAD, 7))
        row.pack(fill="x", pady=1)
        row.columnconfigure(1, weight=1)
        row.columnconfigure(0, minsize=92)   # a tick box and a value box: labels start in one column

        var = self.tweak_vars[tweak.key]
        if tweak.kind == cvars.BOOL:
            widget = ttk.Checkbutton(row, variable=var, onvalue=tweak.value_on,
                                     offvalue="", text="")
        elif tweak.kind == cvars.CHOICE:
            widget = ttk.Combobox(row, textvariable=var, width=8,
                                  state="readonly",
                                  values=[""] + [c[0] for c in tweak.choices])
        else:
            widget = ttk.Entry(row, textvariable=var, width=8)
        widget.grid(row=0, column=0, sticky="w", padx=(0, PAD))   # level with the label, notes below

        label = ttk.Label(row, text=tweak.label, style="Panel.TLabel")
        label.grid(row=0, column=1, sticky="w")

        meta = tweak.key if tweak.label != tweak.key else ""
        if tweak.default is not None:
            meta += ("   " if meta else "") + "default %s" % tweak.default
        ttk.Label(row, text=meta, style="Mono.TLabel").grid(
            row=0, column=2, sticky="e", padx=(PAD, 0))

        # Flags on their own line: on the meta line they pushed the label aside.
        flag = ""
        if cvars.blocked_for(self.title_key, tweak.key):
            flag = ("↻ not a retail command - delivered via default.cfg on the next apply/start"
                    if self.title_key == "tnc" else "↻ cheat cvar - also sent as +toggle at start")
        elif tweak.confidence == "low":
            flag = "~ unverified"
        line = 1
        if tweak.note:
            ttk.Label(row, text=tweak.note, style="PanelMuted.TLabel",
                      wraplength=720, justify="left").grid(
                row=line, column=1, columnspan=2, sticky="w", pady=(2, 0))
            line += 1
        if flag:
            ttk.Label(row, text=flag, style="Flag.TLabel").grid(
                row=line, column=1, columnspan=2, sticky="w", pady=(2, 0))

    def _apply_preset(self, reset=False):
        """Fill in (or take back) one cvars.TNC_PRESETS group.

        Only the tweak values change; Save / Apply mods / Start game deliver
        them as before. Reset restores what the cvars held before the preset
        was applied in this session, or clears them (engine default).
        """
        name = self.preset_var.get()
        preset = cvars.TNC_PRESETS.get(name)
        if not preset:
            return
        self._ensure_tweak_vars()
        current = {k: v.get().strip() for k, v in self.tweak_vars.items() if v.get().strip()}
        if reset:
            new = cvars.reset_preset(current, name, self.preset_undo.pop(name, None))
            msg = "%s reset." % name
        else:
            self.preset_undo.setdefault(name, current)
            new = cvars.apply_preset(current, name)
            args, cfile = cvars.split_tnc(preset)
            msg = ("%s: %d cvars set (%d launch args, %d via default.cfg)."
                   % (name, len(preset), len(args), len(cfile)))
            self.only_set.set(True)
        for key in preset:
            self.tweak_vars[key].set(new.get(key, ""))
        self._rebuild_tweaks()
        self.status_var.set(msg + " Press Save to keep it.")

    def _game_settings(self):
        """The tweaks plus the cvars of the ticked mods (mods win, as in `wolfsdk play`)."""
        out = self._collect_tweaks()
        out.update({k: str(v) for m in self._enabled_mods() for k, v in m.cvars.items()})
        return out

    def _collect_tweaks(self):
        out = {}
        for key, var in self.tweak_vars.items():
            value = var.get().strip()
            if value:
                out[key] = value
        for line in self.extra_text.get("1.0", "end").splitlines():
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            parts = line.replace("=", " ").split(None, 1)
            if len(parts) == 2:
                out[parts[0]] = parts[1].strip()
        return out

    # -- mods tab ----------------------------------------------------------

    def _build_mods_tab(self):
        frame = ttk.Frame(self.notebook, padding=PAGE)
        self.notebook.add(frame, text="  Mods  ")

        # One toolbar: search on the left, the list actions on the right.
        # Right-hand buttons are packed first so the search box is the one
        # that gives way in a narrow window.
        top = ttk.Frame(frame)
        top.pack(fill="x")
        self.check_btn = ttk.Button(top, text="Check", command=self._check_mods)
        self.check_btn.pack(side="right")
        for text, command in (("All off", lambda: self._set_all_mods(False)),
                              ("All on", lambda: self._set_all_mods(True)),
                              ("Open folder", self._open_mods_dir),
                              ("Reload", self._reload_mods)):
            ttk.Button(top, text=text, style="Ghost.TButton",
                       command=command).pack(side="right", padx=(0, 6))
        self.mod_filter_var = tk.StringVar()
        mod_search = ttk.Entry(top, textvariable=self.mod_filter_var,
                               style="Search.TEntry")
        mod_search.pack(side="left", fill="x", expand=True)
        _placeholder(mod_search, self.mod_filter_var, "Search mods…")
        self.mod_filter_var.trace_add("write", lambda *_: self._filter_mods())
        self.mod_count_var = tk.StringVar()
        ttk.Label(top, textvariable=self.mod_count_var,
                  style="Muted.TLabel").pack(side="left", padx=(PAD, PAD))

        # Packed from the bottom before the list: a long description or a small
        # window shrinks the list, never pushes the safety messages out of sight.
        self.mod_log = tk.Text(frame, height=4, wrap="word")
        theme.style_text(self.mod_log, "input", self.fonts)
        self.mod_log.pack(side="bottom", fill="x")
        ttk.Label(frame, text="CHECKS & MESSAGES",
                  style="Eyebrow.TLabel").pack(side="bottom", anchor="w", pady=(PAD + 4, 6))

        # The list and, beside it, the selected mod: the description no longer
        # steals the list's height.
        body = ttk.Frame(frame)
        body.pack(fill="both", expand=True, pady=(PAD, 0))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        columns = ("on", "name", "version", "priority", "assets")
        self.tree = ttk.Treeview(body, columns=columns, show="headings", height=10)
        for col, text, width, anchor in (
            ("on", "ACTIVE", 64, "center"),
            ("name", "MOD", 280, "w"),
            ("version", "VERSION", 76, "w"),
            ("priority", "PRIORITY", 72, "center"),
            ("assets", "ASSETS", 64, "e"),
        ):
            self.tree.heading(col, text=text, anchor=anchor)
            self.tree.column(col, width=width, anchor=anchor, stretch=col == "name")
        # Switched-off mods dim, so the active set stands out at a glance.
        self.tree.tag_configure("off", foreground=theme.PALETTE["dim"])
        self.tree.tag_configure("on", foreground=theme.PALETTE["text"])
        self.tree.bind("<Button-1>", self._toggle_mod)
        self.tree.bind("<space>", self._toggle_selected_mod)
        self.tree.bind("<<TreeviewSelect>>", self._show_mod_details)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)

        details = ttk.Frame(body, style="Card.TFrame", padding=(18, 16), width=300)
        details.grid(row=0, column=2, sticky="nsew", padx=(PAD, 0))
        details.pack_propagate(False)
        ttk.Label(details, text="SELECTED MOD", style="PanelEyebrow.TLabel").pack(anchor="w")
        self.mod_detail_title = tk.StringVar(value="No mod selected")
        self.mod_detail_text = tk.StringVar(
            value="Select a mod to see its description and details.")
        _autowrap(ttk.Label(details, textvariable=self.mod_detail_title,
                            style="H2.TLabel", justify="left"), pady=(4, 0))
        self.mod_toggle_btn = ttk.Button(details, text="Turn on", state="disabled",
                                         command=self._toggle_selected_mod)
        self.mod_toggle_btn.pack(side="bottom", fill="x")
        _autowrap(ttk.Label(details, textvariable=self.mod_detail_text,
                            style="PanelMuted.TLabel", justify="left"), pady=(6, 0))
        self._reload_mods()

    def _open_mods_dir(self):
        MODS_DIR.mkdir(parents=True, exist_ok=True)
        import os

        os.startfile(str(MODS_DIR))  # noqa: S606 - opening a folder for the user

    def _reload_mods(self):
        # Kept whole as well as filtered: _enabled_ids() has to leave the
        # other title's ticks in config.json alone, and it can only tell
        # "belongs to the other game" from "folder is gone" if it still
        # knows every mod on disk.
        # get_children() omits detached rows (search filter), so delete by the
        # previous model as well or a reload can collide with an invisible iid.
        for old in getattr(self, "mods", []):
            if self.tree.exists(old.id):
                self.tree.delete(old.id)
        self.all_mods, errors = modlib.discover(MODS_DIR)
        self.mods = modlib.for_title(self.all_mods, self.title_key)
        enabled = set(self.config_data.get("enabled_mods", []))
        if self.tree.get_children():
            self.tree.delete(*self.tree.get_children())
        for m in self.mods:
            self.tree.insert(
                "", "end", iid=m.id,
                values=(ON if m.id in enabled else OFF, m.name, m.version,
                        m.priority, len(m.assets)),
                tags=("on" if m.id in enabled else "off",),
            )
        self._filter_mods()
        self._log_mods("\n".join("! " + e for e in errors) if errors else
                       "%d mod(s) for %s."
                       % (len(self.mods),
                          self.game.title.name if self.game else "The New Order"))
        self._refresh_dashboard()

    def _toggle_mod(self, event):
        # Only the ACTIVE column switches; a click elsewhere just selects the
        # row, so its details can be read without toggling it.
        row = self.tree.identify_row(event.y)
        if not row or self.tree.identify_column(event.x) != "#1":
            return
        self._mark(row, not self._is_on(row))
        self._update_mod_count()
        self._refresh_dashboard()

    def _toggle_selected_mod(self, _event=None):
        selected = self.tree.selection()
        if selected:
            row = selected[0]
            self._mark(row, not self._is_on(row))
            self._update_mod_count()
        return "break"

    def _set_all_mods(self, enabled):
        for mod in self.mods:
            self._mark(mod.id, enabled)
        self._update_mod_count()
        self._refresh_dashboard()

    def _filter_mods(self):
        if not hasattr(self, "tree"):
            return
        needle = self.mod_filter_var.get().strip().lower()
        if needle == "search mods…":
            needle = ""
        visible = 0
        for index, mod in enumerate(self.mods):
            haystack = " ".join((mod.id, mod.name, mod.author, mod.description)).lower()
            if not needle or needle in haystack:
                self.tree.move(mod.id, "", index)
                visible += 1
            else:
                self.tree.detach(mod.id)
        self._update_mod_count(visible)

    def _update_mod_count(self, visible=None):
        if visible is None:
            visible = len(self.tree.get_children())
        active = len(self._enabled_mods())
        self.mod_count_var.set("%d active  ·  %d/%d shown" %
                               (active, visible, len(self.mods)))

    def _show_mod_details(self, _event=None):
        selected = self.tree.selection()
        if not selected:
            return
        mod = next((m for m in self.mods if m.id == selected[0]), None)
        if not mod:
            return
        self.mod_detail_title.set(mod.name)
        meta = "%s  ·  version %s  ·  priority %d  ·  %d asset(s)" % (
            mod.id, mod.version, mod.priority, len(mod.assets))
        if mod.author:
            meta += "  ·  " + mod.author
        if mod.description:
            meta += "\n\n" + mod.description
        self.mod_detail_text.set(meta)
        self.mod_toggle_btn.configure(state="normal",
                                      text="Turn off" if self._is_on(mod.id) else "Turn on")

    def _enabled_mods(self):
        return [m for m in self.mods if self._is_on(m.id)]

    def _is_on(self, row):
        return self.tree.set(row, "on") not in ("", OFF)

    def _mark(self, row, on):
        self.tree.set(row, "on", ON if on else OFF)
        self.tree.item(row, tags=("on" if on else "off",))
        if row in self.tree.selection():
            self.mod_toggle_btn.configure(text="Turn off" if on else "Turn on")

    def _enabled_ids(self):
        """What goes into config.json: this title's ticks plus the other's.

        The tree only ever holds one title's mods. Saving just those would
        silently switch off every New Order mod the moment somebody looked
        at the New Colossus page.
        """
        others = {m.id for m in self.all_mods if m.spiel != self.title_key}
        keep = [i for i in self.config_data.get("enabled_mods", []) if i in others]
        return sorted(set(keep) | {m.id for m in self._enabled_mods()})

    def _log_mods(self, text):
        self.mod_log.delete("1.0", "end")
        self.mod_log.insert("1.0", text)

    # -- studio tab --------------------------------------------------------

    def _build_studio_tab(self):
        page = ttk.Frame(self.notebook, padding=PAGE)
        self.notebook.add(page, text="  Studio  ")
        # Intro and the one action in a card on top, what it holds below.
        head = ttk.Frame(page, style="Card.TFrame", padding=(20, 16))
        head.pack(fill="x")
        if importlib.util.find_spec(__package__ + ".mapserver") is not None:   # the loader package ships without it
            self.studio_btn = ttk.Button(head, text="Open Studio", style="Accent.TButton",
                                         command=self._open_studio)
            self.studio_btn.pack(side="right", padx=(PAD, 0))
            intro = "View, listen to and export everything from both games, in its own window."
        else:
            intro = ("The Studio is part of WolfSDK Studio, the separate package for modders "
                     "(WolfSDK-Studio-<version>.zip).")
        text = ttk.Frame(head, style="Panel.TFrame")
        text.pack(side="left", fill="x", expand=True)
        ttk.Label(text, text="WolfSDK Studio", style="H2.TLabel").pack(anchor="w")
        _autowrap(ttk.Label(text, text=intro, style="PanelMuted.TLabel", justify="left"),
                  pady=(2, 0))

        grid = ttk.Frame(page)
        grid.pack(fill="x", pady=(PAD, 0))
        grid.columnconfigure((0, 1), weight=1, uniform="tile")
        for i, (name, what) in enumerate((
            ("MAPS", "Walk through every level in 3D, with textures and collision."),
            ("MODELS", "Weapons, enemies, characters, vehicles and props in 3D, "
                       "with animations. Preview your own skin, export as GLB/OBJ."),
            ("VIDEO AND AUDIO", "Play cutscenes and sounds."),
            ("TEXTURES AND TEXT", "Browse every image and game text."),
        )):
            tile = ttk.Frame(grid, style="Card.TFrame", padding=(20, 16))
            tile.grid(row=i // 2, column=i % 2, sticky="nsew",
                      padx=(0, 5) if i % 2 == 0 else (5, 0), pady=(0, PAD))
            ttk.Label(tile, text=name, style="PanelEyebrow.TLabel").pack(anchor="w")
            _autowrap(ttk.Label(tile, text=what, style="Panel.TLabel", justify="left"),
                      pady=(6, 0))

    def _open_studio(self):
        """The Studio (maps in 3D, videos, sound, textures, texts) is a browser
        page served by this process (mapserver.py): tkinter has no 3D and plays
        no video. Binding the socket is instant; serving and the browser start
        run off the Tk thread. The server is rooted in Wolfenstein II and finds
        The New Order itself."""
        game = next((g for g in [self.game, *self.installs.values()]
                     if g and g.title.key == "tnc"), None)
        if game is None:
            messagebox.showerror("Studio", "Wolfenstein II: The New Colossus was not "
                                 "found. The Studio needs this game as its base.")
            return
        try:
            from . import mapserver
            url = mapserver.start(game.root)
        except Exception as exc:  # noqa: BLE001 -- under pythonw an uncaught error would vanish silently
            messagebox.showerror("Studio", "The Studio could not be started:\n%s" % exc)
            return
        self.status_var.set("Studio running: %s" % url)
        threading.Thread(target=mapserver.open_app, args=(url,), daemon=True).start()

    # -- info tab ----------------------------------------------------------

    def _build_info_tab(self):
        frame = ttk.Frame(self.notebook, padding=PAGE)
        self.notebook.add(frame, text="  Console & Info  ")
        support = ttk.Frame(frame)
        support.pack(fill="x", pady=(0, PAD))
        ttk.Label(support, text="WolfSDK is free. If it helps you, you can buy the developer a coffee.",
                  style="Muted.TLabel").pack(side="left")
        ttk.Button(support, text="Support the project", style="Accent.TButton",
                   command=lambda: webbrowser.open(SUPPORT_URL)).pack(side="right")
        text = tk.Text(frame, wrap="word", height=20)
        theme.style_text(text, "mono", self.fonts)
        text.configure(padx=18, pady=14, cursor="arrow")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        # Headings, commands and their meaning in three voices instead of one
        # grey block; notes in the body font with a hanging indent.
        p = theme.PALETTE
        text.tag_configure("h", font=self.fonts.get("h2"), foreground=p["text"],
                           spacing1=14, spacing3=6)
        text.tag_configure("cmd", foreground=p["text"])
        text.tag_configure("desc", foreground=p["muted"])
        text.tag_configure("note", font=self.fonts.get("body"), foreground=p["muted"],
                           lmargin2=14, spacing3=6)

        def heading(s):
            text.insert("end", s + "\n", "h")

        heading("The New Order console")
        text.insert("end", "Ctrl+^ (Ctrl + the key left of 1), no setting needed.\n\n", "desc")
        for name, desc in cvars.COMMANDS:
            text.insert("end", "  %-27s " % name, "cmd")
            text.insert("end", desc + "\n", "desc")
        heading("Default key bindings")
        text.insert("end", "The New Order only (in The New Colossus a key cannot reach these "
                           "commands; use the Sandbox Tools mod there).\n", "note")
        for combo, command, desc in cvars.TNO_BINDS:
            text.insert("end", "  %-6s %-34s " % (combo, command), "cmd")
            text.insert("end", desc + "\n", "desc")
        heading("Notes")
        for note in (
            "The New Order: Cheats and QoL change no game file. They are passed "
            "as arguments when the game starts.",
            "The New Colossus: cvars the retail console accepts go as start "
            "arguments, the rest into the game's default.cfg inside the archives "
            "('Revert' removes it). Not yet confirmed in game.",
            "The game is started through Steam. Starting the EXE directly does "
            "nothing: the SteamStub wrapper hands the start back and exits.",
            "Mods write into the .resources archives. 'Revert' restores the "
            "originals bit for bit. The mod list only ever shows the mods of "
            "the selected game; both games have their own archives, their own "
            "backups and their own mods.",
            "Cvar lists come from the engines' own registries (The New Colossus "
            "3,116 settable, The New Order 3,194) with the game's own descriptions.",
        ):
            text.insert("end", "–  " + note + "\n", "note")
        text.configure(state="disabled")

    # -- footer ------------------------------------------------------------

    def _build_footer(self):
        # The play bar: install state (dot), last message, and the actions
        # that touch the game, on every page.
        bar = ttk.Frame(self, style="Bar.TFrame", padding=(GUTTER, 12))
        bar.pack(side="bottom", fill="x")
        ttk.Frame(self, style="Line.TFrame", height=1).pack(side="bottom", fill="x")

        # Buttons first, label last. Tk's packer hands each widget its
        # requested size in pack order, and a Label requests room for its
        # whole string -- packed first it claimed the entire bar and pushed
        # all four buttons out of the window. They were there the whole time,
        # just off-screen.
        start_btn = ttk.Button(bar, text="Start game", style="Play.TButton",
                               command=self._launch)
        start_btn.pack(side="right", padx=(PAD, 0))
        apply_btn = ttk.Button(bar, text="Apply mods", command=self._apply)
        apply_btn.pack(side="right", padx=(6, 0))
        revert_btn = ttk.Button(bar, text="Revert", command=self._revert)
        revert_btn.pack(side="right", padx=(6, 0))
        self.mod_buttons = [apply_btn, revert_btn]
        save_btn = ttk.Button(bar, text="Save", command=self._save)
        save_btn.pack(side="right", padx=(6, 0))
        self.action_buttons = [start_btn, apply_btn, revert_btn, save_btn]

        self.status_dot = ttk.Label(bar, text="●", style="PanelWarn.TLabel")
        self.status_dot.pack(side="left", padx=(0, 8))
        self.status_var = tk.StringVar(value="")
        self.status_label = ttk.Label(bar, textvariable=self.status_var, anchor="w",
                                      style="Panel.TLabel")
        self.status_label.pack(side="left", fill="x", expand=True)
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=80)

    def _set_mod_controls(self):
        """Grey out what this title cannot do yet, instead of failing later."""
        state = ("normal" if not self.busy and self.game and self.game.supports_mods
                 else "disabled")
        for button in getattr(self, "mod_buttons", []):
            button.configure(state=state)

    def _set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for button in getattr(self, "action_buttons", []):
            button.configure(state=state)
        if hasattr(self, "check_btn"):
            self.check_btn.configure(state=state)
        if hasattr(self, "title_box"):
            self.title_box.configure(state="disabled" if busy else "readonly")
        if busy:
            self.progress.pack(side="left", padx=(0, PAD), before=self.status_label)
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.pack_forget()
        self._set_mod_controls()

    def _save(self):
        by_title = self.config_data.get("tweaks_by_title")
        if not isinstance(by_title, dict):
            by_title = {}
        current = self._collect_tweaks()
        by_title[self.title_key] = current
        self.config_data["tweaks_by_title"] = by_title
        # Keep the flat key in step. Anything still reading "tweaks" would
        # otherwise quietly serve whatever was saved before per-title storage
        # existed.
        if self.title_key == "tno":
            self.config_data["tweaks"] = current
        self.config_data["extra_cvars"] = self.extra_text.get("1.0", "end").strip()
        self.config_data["enabled_mods"] = self._enabled_ids()
        if self.game:
            self.config_data["game_path"] = str(self.game.root)
        save_config(self.config_data)
        self.status_var.set("Settings saved.")
        self._refresh_dashboard()

    def _refresh_status(self):
        if not self.game:
            self.status_var.set("No game install selected.")
            return
        if not self.game.supports_mods:
            self.status_var.set(
                "%s — starts with cvars, mods not supported yet."
                % self.game.title.name)
            return
        try:
            install = self._install()
            journal = install.read_journal()
            if journal:
                if journal.get("state") == "applying":
                    self.status_var.set(
                        "Interrupted patch run - please revert.")
                else:
                    self.status_var.set(
                        "Game patched: %d mod(s), %d asset(s)."
                        % (len(journal["mods"]), len(journal["assets"]))
                    )
            else:
                self.status_var.set("Game is in its original state.")
            install.close()
        except Exception as exc:  # noqa: BLE001
            self.status_var.set("Status unknown: %s" % exc)

    # -- actions -----------------------------------------------------------

    def _check_mods(self):
        if not self.game:
            messagebox.showerror("No game", "Please choose the game folder first.")
            return
        active = self._enabled_mods()
        self.status_var.set("Checking mod set, conflicts and backup…")
        self._run_bg(self._check_worker, active)

    def _check_worker(self, active):
        """Read-only equivalent of apply: fail before any archive is touched."""
        if not active:
            return "Check OK — no mods selected."
        # Needs only the mods, so it runs even when the game is out of step
        # with the selection and build_payloads below is skipped.
        clashes = modlib.texture_clashes(active)
        if clashes:
            return "\n".join(["Check failed — nothing was changed."]
                             + ["Error     %s" % e for e in clashes])
        install = self._install()
        try:
            journal = install.read_journal()
            manifest = install.read_manifest()
            if manifest:
                problems = (install.verify() if self.title_key == "tnc"
                            else install.verify_backup())
                if problems:
                    return "Backup not ready:\n  " + "\n  ".join(problems)

            selected = {m.id for m in active}
            if journal:
                if journal.get("state") == "applying":
                    return ("Recovery needed: a patch run was "
                            "interrupted. Please revert first.")
                applied = set(journal.get("mods", []))
                if applied == selected:
                    return ("Check OK — your selection matches the game.\n"
                            "%d mod(s), %d asset(s); the backup is intact."
                            % (len(applied), len(journal.get("assets", []))))
                return ("Your selection is out of sync with the game.\n"
                        "For an exact payload check, revert first; "
                        "applying does that automatically.")

            payloads, conflicts, errors = modlib.build_payloads(install, active)
            lines = []
            for conflict in conflicts:
                lines.append("Conflict  %s" % conflict)
            for error in errors:
                lines.append("Error     %s" % error)
            if errors:
                lines.insert(0, "Check failed — nothing was changed.")
                return "\n".join(lines)

            copies = sum(len(install.find(*key)) for key in payloads)
            lines.insert(0, "%d mod(s), %d asset(s), %d archive copies checked."
                         % (len(active), len(payloads), copies))
            if conflicts:
                lines.insert(1, "%d conflict(s); higher priority wins."
                             % len(conflicts))
            else:
                lines.insert(1, "No conflicts. Ready to apply.")
            return "\n".join(lines)
        finally:
            install.close()

    def _launch(self):
        if not self.game:
            messagebox.showerror("No game", "Please choose the game folder first.")
            return
        self._save()
        # Ticking a mod changes nothing until it is written into the
        # archives. Without this the obvious move -- tick a mod, press start
        # -- runs the previous mod set and looks like the loader ignored the
        # click.
        if not self._mods_in_sync():
            answer = messagebox.askyesnocancel(
                "Mods not in the game yet",
                "The ticked mods are not in the game files yet.\n\n"
                "Apply them now and then start?",
            )
            if answer is None:
                return
            if answer:
                self.status_var.set("Applying mods, then starting…")
                self._run_bg(self._apply_worker, self._enabled_mods(),
                             self._game_settings(), then=self._start_game)
                return
        self._start_game()

    def _mods_in_sync(self):
        """True when the game already carries exactly the ticked mods.

        For TNC, "the ticked mods" includes the Cheats & QoL tweaks cvar(s)
        that only work appended to cfile:default.cfg (modlib.with_tnc_tweaks):
        its id carries a hash of what it would write, so a changed tweak
        set compares out of sync here exactly like a changed mod selection.
        """
        if not self.game.supports_mods:
            return True  # nothing to write, so nothing can be out of step
        try:
            install = self._install()
            to_apply, _args, _refusal = modlib.with_tnc_tweaks(
                install, self._enabled_mods(), self._game_settings())
            applied = modlib.applied_ids(install)
            install.close()
        except Exception:  # noqa: BLE001 - can't tell, so don't nag
            return True
        return applied == {m.id for m in to_apply}

    def _start_game(self):
        settings = self._game_settings()
        note2 = ""
        if self.title_key == "tnc":
            settings, cfile_settings = cvars.split_tnc(settings)
            if cfile_settings:
                note2 = ("  %d more cvar(s) need cfile:default.cfg and only take effect "
                         "if the mods are applied (see above)." % len(cfile_settings))
        elif self.title_key == "tno":
            dropped = [k for k in settings if cvars.tno_route(k)[1] == "drop"]
            settings = {k: v for k, v in settings.items() if k not in dropped}
            if dropped:
                note2 = "  Left out, not New Order cvars: %s." % ", ".join(dropped)
        args = cvars.build_args(settings, cvars.binds_for(self.title_key), self.title_key)
        try:
            self.game.launch(args)
        except GameError as exc:
            messagebox.showerror("Start failed", str(exc))
            return
        note = "" if steam_is_running() else "  Steam is starting too, this takes a moment."
        self.status_var.set(
            "Start requested through Steam, %d cvar(s).%s%s" % (len(settings), note, note2))

    def _apply(self):
        if not self.game:
            messagebox.showerror("No game", "Please choose the game folder first.")
            return
        if not self.game.supports_mods:
            messagebox.showinfo(
                "Not supported yet",
                "%s runs on %s. The loader cannot write this game's archives "
                "yet - starting with cheats and QoL works, though."
                % (self.game.title.name, self.game.title.engine))
            return
        self._save()
        # The Studio's server (this process) holds the archives open; Windows then
        # refuses the rebuild or cache swap a texture mod needs. Studio mods included.
        studio = "Studio closed (it keeps the game files open). " if self._close_studio() else ""
        self.status_var.set(studio + "Applying mods…")
        self._run_bg(self._apply_worker, self._enabled_mods(), self._game_settings())

    def _apply_worker(self, active, settings):
        install = self._install()
        try:
            # TNC only: cvars the retail gate rejects as +args, delivered as
            # one more mod carrying cfile:default.cfg with them appended.
            active, _args, refusal = modlib.with_tnc_tweaks(install, active, settings)
            if refusal:
                return "Refused: %s" % refusal
            # An empty selection is not a no-op: it means "this game should
            # carry no mods", which is a revert.
            if not active:
                if not install.is_modified():
                    return "No mod active - the game is already in its original state."
                modlib.refuse_while_running(install)
                install.revert()
                return "No mod active - the game is back to its original state."
            before = install.is_modified()
            report = modlib.apply_mods(install, active)
            # A refusal can come after apply_mods reverted the old mods
            # (space check, build errors): the disk says what happened.
            reset = before and not report["ok"] and not install.is_modified()
        finally:
            install.close()
        lines = []
        for c in report["conflicts"]:
            lines.append("Conflict  %s" % c)
        for e in report["errors"]:
            lines.append("Error     %s" % e)
        if report["ok"]:
            j = report["journal"]
            lines.insert(0, "%d asset(s) patched." % len(j["assets"]))
            if j["missing"]:
                lines.append("Not found: %s" % ", ".join(j["missing"]))
        else:
            lines.insert(0, "Cancelled - the old mods were reverted, the game is back to its "
                            "original state." if reset else "Cancelled - nothing was changed.")
        return "\n".join(lines)

    def _revert(self):
        if not self.game or not self.game.supports_mods:
            return
        if not messagebox.askyesno(
            "Revert",
            "Undo all mod changes and restore the original "
            "game files?",
        ):
            return
        # The Studio's server (this process) holds the archives open; Windows then
        # refuses the rebuild or cache swap a texture mod needs. Studio mods included.
        studio = "Studio closed (it keeps the game files open). " if self._close_studio() else ""
        self.status_var.set(studio + "Reverting…")
        self._run_bg(self._revert_worker)

    def _revert_worker(self):
        install = self._install()
        try:
            modlib.refuse_while_running(install)
            install.revert()
            return "The game is back to its original state."
        except (PatchError, TncPatchError, modlib.ModError) as exc:
            return "Error: %s" % exc
        finally:
            install.close()

    # -- background plumbing ----------------------------------------------

    def _run_bg(self, fn, *args, then=None):
        if self.busy:
            return
        self._set_busy(True)

        def work():
            try:
                self.queue.put(("ok", fn(*args), then))
            except Exception as exc:  # noqa: BLE001
                self.queue.put(("err", str(exc), None))

        threading.Thread(target=work, daemon=True).start()

    def _drain(self):
        try:
            while True:
                kind, payload, then = self.queue.get_nowait()
                self._set_busy(False)
                self._log_mods(payload)
                self.status_var.set(
                    "Done." if kind == "ok" else "Failed - see messages."
                )
                self._refresh_status()
                self._refresh_dashboard()
                if kind == "ok" and then:
                    then()  # after the status line, so the callback's text wins
        except queue.Empty:
            pass
        self.after(120, self._drain)


def main(game_path=None):
    root = tk.Tk()
    root.title("WolfSDK — Mod Manager")
    root.geometry("1120x820")
    root.minsize(920, 640)
    _style, fonts = theme.apply(root)
    try:
        App(root, game_path, fonts)
    except Exception:  # noqa: BLE001 -- under pythonw a startup error would vanish silently
        import os, traceback
        log = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "wolfsdk" / "start.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(traceback.format_exc() + "\n")
        messagebox.showerror("WolfSDK", "The loader could not start:\n%s\n\nDetails: %s"
                             % (sys.exc_info()[1], log))
        root.destroy()
        return
    root.mainloop()


if __name__ == "__main__":
    main()
