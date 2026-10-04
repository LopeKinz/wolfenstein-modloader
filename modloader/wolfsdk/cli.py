"""Command line interface: wolfsdk <command>."""

import argparse
import json
import sys
from pathlib import Path

from . import cvars, mapinstall, mod as modlib
from .decl import Decl, DeclError
from . import game as gamemod
from .game import Game, GameError, TITLES, load_config, save_config
from .patch import PatchError
from .tncpatch import TncPatchError

ROOT = Path(__file__).resolve().parent.parent
MODS_DIR = ROOT / "mods"


def _settings(game=None):
    """The cvar settings the loader saved, plus the user's own lines.

    The GUI stores per title, because the two games share almost no cvar
    names. It used to write only the flat "tweaks" key; reading just that one
    here meant every launch from Spielen.cmd handed the game the values from
    before the user last pressed Save, silently.
    """
    cfg = load_config()
    key = game.title.key if game else "tno"
    by_title = cfg.get("tweaks_by_title")
    if isinstance(by_title, dict) and key in by_title:
        settings = dict(by_title[key])
    else:
        settings = dict(cfg.get("tweaks") or {})
    for line in (cfg.get("extra_cvars") or "").splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        parts = line.replace("=", " ").split(None, 1)
        if len(parts) == 2:
            settings[parts[0]] = parts[1].strip()
    return settings


def _game(args):
    return Game.find(getattr(args, "game", None), getattr(args, "title", None))


def _runtime_cvars(game):
    """Cvars declared by enabled mods for this title.

    Runtime-only mods have no archive payload; keeping their values in the
    manifest lets ``launch`` and ``play`` apply them consistently.
    """
    cfg = load_config()
    enabled = set(cfg.get("enabled_mods", []))
    mods, _ = modlib.discover(MODS_DIR)
    values = {}
    for mod in modlib.for_title(mods, game.title.key):
        if mod.id in enabled:
            values.update({key: str(value) for key, value in mod.cvars.items()})
    return values


def _install(game):
    """The patcher for this title -- part one's or part two's."""
    return modlib.installation_for(game)


def _split_key(key):
    if ":" not in key:
        raise SystemExit("An asset must be written as 'type:name', e.g. weapon:weapon/shotgun_base")
    return key.split(":", 1)


# -- commands ---------------------------------------------------------------


def cmd_games(args):
    """Which supported titles are installed."""
    games = Game.find_all()
    if not games:
        print("No installation found.")
        return
    for g in games:
        print("%-5s %-34s %-10s Mods: %s"
              % (g.title.key, g.title.name, g.title.engine,
                 "yes" if g.supports_mods else "not yet"))
        print("      %s" % g.root)


def cmd_info(args):
    game = _game(args)
    install = _install(game)
    print("Game       : %s  (%s, %s)" % (game.root, game.title.key, game.title.engine))
    print("Archives   : %d" % len(install.archives))
    total = sum(len(a.entries) for a in install.archives)
    print("Assets     : %d" % total)
    journal = install.read_journal()
    if journal and journal.get("state") == "applying":
        print("State      : RECOVERY NEEDED (a patch run was interrupted)")
    else:
        print("State      : %s" % ("MODIFIED" if journal else "original"))
    if journal:
        print("  Mods     : %s" % (", ".join(journal["mods"]) or "-"))
        print("  Assets   : %d patched" % len(journal["assets"]))
    # Same question, two names: part one checks the copied index files,
    # part two the archive head regions.
    problems = (install.verify() if game.title.key == "tnc"
                else install.verify_backup())
    if problems and install.read_manifest():
        print("Backup     : PROBLEMS")
        for p in problems:
            print("   ! %s" % p)
    elif install.read_manifest():
        print("Backup     : ok")
    install.close()


def cmd_list(args):
    install = _install(_game(args))
    shown = 0
    for archive in install.archives:
        for e in archive.entries:
            if args.type and e.type != args.type:
                continue
            if args.grep and args.grep.lower() not in e.name.lower():
                continue
            print("%-16s %-58s %8d B" % (e.type, e.name, e.usize))
            shown += 1
            if shown >= args.limit:
                print("... (limit of %d reached, raise --limit)" % args.limit)
                install.close()
                return
    print("\n%d match(es)" % shown)
    install.close()


def cmd_types(args):
    install = _install(_game(args))
    counts = {}
    for archive in install.archives:
        for e in archive.entries:
            counts[e.type] = counts.get(e.type, 0) + 1
    for name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        print("%8d  %s" % (count, name))
    install.close()


def cmd_cat(args):
    install = _install(_game(args))
    type_, name = _split_key(args.asset)
    try:
        data = modlib.read_asset(install, type_, name)
    except (*modlib.PATCH_ERRORS, modlib.ModError) as exc:
        raise SystemExit(str(exc))
    if args.raw:
        sys.stdout.buffer.write(data)
    else:
        sys.stdout.write(data.decode("utf-8", "replace"))
    install.close()


def cmd_paths(args):
    """List the dotted paths inside a decl -- the addresses mods use."""
    install = _install(_game(args))
    type_, name = _split_key(args.asset)
    try:
        text = modlib.read_asset(install, type_, name).decode("utf-8")
    except (*modlib.PATCH_ERRORS, modlib.ModError, UnicodeDecodeError) as exc:
        raise SystemExit(str(exc))
    try:
        decl = Decl(text)
    except DeclError as exc:
        raise SystemExit("Could not parse the decl: %s" % exc)
    for path in sorted(decl.values):
        value = decl.values[path]
        if value.kind == "block":
            if args.blocks:
                print("%-64s  {block}" % path)
            continue
        if args.grep and args.grep.lower() not in path.lower():
            continue
        print("%-64s  %s" % (path, value.text[:60]))
    install.close()


def cmd_extract(args):
    install = _install(_game(args))
    out = Path(args.outdir)
    count = 0
    failed = 0
    for archive in install.archives:
        for e in archive.entries:
            if args.type and e.type != args.type:
                continue
            if args.grep and args.grep.lower() not in e.name.lower():
                continue
            dest = out / e.type / (e.name + ".decl" if args.type_suffix else e.name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                dest.write_bytes(archive.read(e))
                count += 1
            except Exception:  # noqa: BLE001
                failed += 1
            if count and count % 5000 == 0:
                print("  ... %d extracted" % count)
    print("Extracted %d asset(s) to %s (%d error(s))" % (count, out, failed))
    install.close()


def cmd_image_info(args):
    from .bim import BimError, expected_size, parse

    install = _install(_game(args))
    type_, name = _split_key(args.asset if ":" in args.asset else "image:" + args.asset)
    try:
        image = parse(modlib.read_asset(install, type_, name))
    except (*modlib.PATCH_ERRORS, modlib.ModError, BimError) as exc:
        raise SystemExit(str(exc))
    print("%s" % name)
    print("  %s" % image.describe())
    print("  Format code: %d" % image.format_code)
    print("  Hash       : 0x%08X" % image.hash)
    print("  Mip levels :")
    for width, height, size, offset in image.mips:
        want = expected_size(width, height, image.format_code)
        flag = "" if want in (None, size) else "  (expected %d)" % want
        print("     %5dx%-5d %8d B @%d%s" % (width, height, size, offset, flag))
    install.close()


def cmd_image_export(args):
    """Write a game texture out as PNG so it can be edited."""
    from .bim import BimError, parse
    from .image import ImageError, bim_to_png

    install = _install(_game(args))
    name = args.asset.split(":", 1)[1] if ":" in args.asset else args.asset
    try:
        data = modlib.read_asset(install, "image", name)
        png = bim_to_png(parse(data), data)
    except (*modlib.PATCH_ERRORS, modlib.ModError, BimError, ImageError) as exc:
        raise SystemExit(str(exc))
    out = Path(args.out or (name.split("/")[-1] + ".png"))
    out.write_bytes(png)
    print("%s -> %s (%d bytes)" % (name, out, len(png)))
    install.close()


def cmd_image_import(args):
    """Turn a PNG into a texture-replacement mod."""
    from .bim import BimError, parse
    from .image import ImageError, png_to_bim

    install = _install(_game(args))
    name = args.asset.split(":", 1)[1] if ":" in args.asset else args.asset
    try:
        original = modlib.read_asset(install, "image", name)
        template = parse(original)
    except (*modlib.PATCH_ERRORS, modlib.ModError, BimError) as exc:
        raise SystemExit(str(exc))
    try:
        bim = png_to_bim(Path(args.png).read_bytes(), template=template)
    except (OSError, ImageError) as exc:
        raise SystemExit(str(exc))

    folder = MODS_DIR / args.mod
    (folder / "assets").mkdir(parents=True, exist_ok=True)
    rel = "assets/%s.bimage" % name.replace("/", "_").replace(" ", "_")
    (folder / rel).write_bytes(bim)

    manifest_path = folder / "mod.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text("utf-8"))
    else:
        manifest = {
            "id": "local.%s" % args.mod,
            "name": args.mod,
            "version": "1.0.0",
            "author": "",
            "description": "Texture replacement, made with 'wolfsdk image-import'.",
            "priority": 100,
            "assets": {},
        }
    manifest.setdefault("assets", {})["image:%s" % name] = {"file": rel}
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), "utf-8")
    print("%s replaced by %s" % (name, args.png))
    print("  %dx%d, %d bytes (original %d, %s)"
          % (template.width, template.height, len(bim), len(original), template.format_name))
    print("  Mod '%s' -> enable it with: python -m wolfsdk enable %s"
          % (args.mod, manifest["id"]))
    install.close()


def cmd_tweaks(args):
    if args.all:
        print("Mining every cvar name out of the EXE is 'wolfsdk mine-cvars'.\n")
    for category in cvars.CATEGORIES:
        print("=== %s" % category)
        for t in cvars.CATALOG:
            if t.category != category:
                continue
            default = (" [default %s]" % t.default) if t.default else ""
            flag = "" if t.confidence == "high" else "  (?%s)" % t.confidence
            print("  %-28s %-34s %s%s%s" % (t.key, t.label, t.kind, default, flag))
            if args.verbose and t.note:
                print("      %s" % t.note)
        print()
    print("The New Order console: Ctrl+^ (Ctrl + the key left of 1), no cvar needed:")
    for name, desc in cvars.COMMANDS:
        print("  %-27s %s" % (name, desc))


def cmd_launch(args):
    game = _game(args)
    game.remember()
    settings = _settings(game)
    settings.update(_runtime_cvars(game))
    for pair in args.set or []:
        if "=" not in pair:
            raise SystemExit("--set needs cvar=value, not %r" % pair)
        key, value = pair.split("=", 1)
        settings[key] = value
    if args.god:
        settings["g_permaGodMode"] = "1"
    if args.ammo:
        settings["g_permaInfiniteAmmo"] = "1"
    if args.console:
        settings["com_allowConsole"] = "1"
    if args.skip_intro:
        settings["com_skipIntroVideo"] = "1"
    if args.fov:
        settings["g_fov"] = str(args.fov)

    binds = cvars.binds_for(game.title.key) if args.binds else ()
    arguments = cvars.build_args(settings, binds, game.title.key)
    if args.print_only:
        print("Arguments:\n  %s\n" % " ".join(arguments))
        print("Steam launch options:\n  %s" % game.steam_launch_options(arguments))
        return
    print("Starting with %d cvar(s)%s" % (len(settings), " and key bindings" if binds else ""))
    print("  %s" % " ".join(arguments)[:300])
    print("Starting through Steam: %s" % game.launch(arguments))
    if not gamemod.steam_is_running():
        print("Steam is not running yet and will be started too - this takes a moment.")


def cmd_play(args):
    """One click: put the enabled mods into the game, then start it.

    The two steps are separate commands on purpose -- applying rewrites
    archives and takes a while -- but the common case is "play with what I
    ticked", and doing that by hand means remembering which of apply/revert
    the current selection needs.
    """
    game = _game(args)
    game.remember()
    cfg = load_config()
    mods, errors = modlib.discover(MODS_DIR)
    for e in errors:
        print("  ! %s" % e)
    enabled = set(cfg.get("enabled_mods", []))
    # Only the mods written for the title being started. The two games share
    # neither asset names nor archive format, so handing a New Order mod to
    # The New Colossus can only end in "Asset existiert nicht im Spiel".
    for_this = modlib.for_title(mods, game.title.key)
    active = [m for m in for_this if m.id in enabled]
    missing = enabled - {m.id for m in mods}
    if missing:
        print("  ! Enabled, but not found: %s" % ", ".join(sorted(missing)))

    settings = _settings(game)
    settings.update({key: str(value) for mod in active for key, value in mod.cvars.items()})

    install = _install(game)
    try:
        # TNC only: cvars the retail console/+arg gate rejects move from
        # `settings` into one more mod (cfile:default.cfg, appended) -- see
        # modlib.with_tnc_tweaks. For The New Order this is a no-op.
        to_apply, settings, refusal = modlib.with_tnc_tweaks(install, active, settings)
        if refusal:
            raise SystemExit("%s - the game was not started." % refusal)
        # Compare against what is on disk, not against the config: a mod that
        # vanished from mods/ can never be "in the game", and comparing to the
        # config would re-apply on every single start. The tweaks mod's id
        # carries a hash of its content, so a changed tweak set compares
        # unequal here too and gets re-applied, same as a changed mod selection.
        if modlib.applied_ids(install) == {m.id for m in to_apply}:
            print("The mods are already in the game (%d active)." % len(active))
        elif to_apply:
            print("Selection changed - applying %d mod(s): %s"
                  % (len(to_apply), ", ".join(m.id for m in to_apply)))
            report = _apply(install, to_apply)
            if not report["ok"]:
                raise SystemExit("%s - the game was not started." % report["kopf"])
            print("OK: %d asset(s) patched." % len(report["journal"]["assets"]))
        elif install.is_modified():
            print("No mod enabled - restoring the original game…")
            install.revert()
        else:
            # Part two's revert() refuses when no backup was ever taken, and
            # rightly so -- but "nothing to undo" is not an error to abort a
            # launch over.
            print("No mod enabled - the game is in its original state.")
    finally:
        install.close()

    binds = cvars.binds_for(game.title.key) if args.binds else ()
    game.launch(cvars.build_args(settings, binds, game.title.key))
    print("Start requested through Steam, %d cvar(s)%s."
          % (len(settings), " and key bindings" if binds else ""))
    if not gamemod.steam_is_running():
        print("Steam is not running yet and will be started too - this takes a moment.")


def cmd_ingame_menu(args):
    """Add (or remove) the mod page in the engine's own DevGUI."""
    from . import devmenu

    game = _game(args)
    if game.title.key != "tno":
        raise SystemExit(
            "The in-game page is built on The New Order's DevGUI decl; "
            "there is no version of it for %s." % game.title.name)
    install = _install(game)
    if args.remove:
        if not install.is_modified():
            print("Nothing is patched - there is nothing to remove.")
            install.close()
            return
        install.revert()
        print("Reverted. The page is gone (together with all mod patches).")
        install.close()
        return

    settings = load_config().get("tweaks", {})
    if args.preview:
        print(devmenu.build_page(settings=settings))
        install.close()
        return

    payload = devmenu.payload(install, settings=settings)
    report = install.apply({devmenu.TARGET: payload}, ["wolfsdk.ingame_menu"])
    print("In-game menu added (%d asset(s))." % len(report["assets"]))
    print("Open it in the game with F10; the page is called '%s'." % devmenu.PAGE_NAME)
    print("Start with:  python -m wolfsdk launch --set devgui=1")
    install.close()


def cmd_mods(args):
    mods, errors = modlib.discover(MODS_DIR)
    if args.title:
        mods = modlib.for_title(mods, args.title)
    if not mods and not errors:
        print("No mods in %s%s"
              % (MODS_DIR, " for %s" % args.title if args.title else ""))
        return
    cfg = load_config()
    enabled = set(cfg.get("enabled_mods", []))
    for m in mods:
        mark = "[x]" if m.id in enabled else "[ ]"
        print("%s %-4s %-34s v%-8s prio %-4d %s"
              % (mark, m.spiel, m.id, m.version, m.priority, m.name))
        if args.verbose:
            for key in m.assets:
                spec = m.assets[key]
                how = "replaced" if spec.get("file") else "%d value(s)" % len(spec.get("set") or {})
                print("       %-56s %s" % (key, how))
    for e in errors:
        print("  ! %s" % e)


def cmd_check(args):
    """Build and validate a complete mod set without writing game files."""
    game = _game(args)
    mods, discovery_errors = modlib.discover(MODS_DIR)
    available = modlib.for_title(mods, game.title.key)
    if args.all:
        active = available
    else:
        enabled = set(load_config().get("enabled_mods", []))
        active = [m for m in available if m.id in enabled]

    print("Pre-check  : %s" % game.title.name)
    print("Mod set    : %s" % (", ".join(m.id for m in active) or "no mods"))
    if discovery_errors:
        for error in discovery_errors:
            print("  ERROR manifest: %s" % error)
        raise SystemExit("Pre-check failed: invalid mod manifests.")
    if not active:
        print("OK: no mods selected; nothing would be patched.")
        return
    # Mods only, so it also answers when the game is already modified below.
    clashes = modlib.texture_clashes(active)
    if clashes:
        for error in clashes:
            print("  ERROR: %s" % error)
        raise SystemExit("Pre-check failed; nothing was changed.")

    install = _install(game)
    try:
        journal = install.read_journal()
        if journal:
            if journal.get("state") == "applying":
                raise SystemExit(
                    "Found an unfinished patch run. Run 'wolfsdk revert' first; "
                    "the write-ahead journal can recover it.")
            raise SystemExit(
                "The installation is already modified. Run 'wolfsdk revert' first, "
                "so the pre-check starts from the original state.")

        manifest = install.read_manifest()
        if manifest:
            problems = (install.verify() if game.title.key == "tnc"
                        else install.verify_backup())
            if problems:
                for problem in problems:
                    print("  ERROR backup: %s" % problem)
                raise SystemExit("Pre-check failed: the backup is unusable.")

        payloads, conflicts, errors = modlib.build_payloads(install, active)
        for conflict in conflicts:
            print("  CONFLICT: %s" % conflict)
        for error in errors:
            print("  ERROR: %s" % error)
        if errors:
            raise SystemExit("Pre-check failed; nothing was changed.")

        copies = sum(len(install.find(type_, name)) for type_, name in payloads)
        original_bytes = sum(len(modlib.read_asset(install, *key)) for key in payloads)
        replacement_bytes = sum(len(data) for data in payloads.values())
        print("Assets     : %d logical, %d archive cop(ies)" % (len(payloads), copies))
        print("Payload    : %d -> %d bytes" % (original_bytes, replacement_bytes))

        if game.title.key == "tno":
            from .resources import plan_in_place

            direct, rebuild = 0, set()
            for key, data in payloads.items():
                for archive, entry in install.find(*key):
                    if plan_in_place(entry, data) is None:
                        rebuild.add(archive.resources_path.name)
                    else:
                        direct += 1
            print("Write plan : %d in place, %d archive rebuild(s)" %
                  (direct, len(rebuild)))
            for name in sorted(rebuild):
                print("  Rebuild: %s" % name)

        suffix = (" %d conflict(s), the load order decides." % len(conflicts)
                  if conflicts else "")
        print("OK: the mod set can be applied; nothing was changed.%s" % suffix)
    finally:
        install.close()


def cmd_apply(args):
    game = _game(args)
    install = _install(game)
    mods, errors = modlib.discover(MODS_DIR)
    for e in errors:
        print("  ! %s" % e)
    cfg = load_config()
    enabled = set(cfg.get("enabled_mods", []))
    active = [m for m in modlib.for_title(mods, game.title.key) if m.id in enabled]
    settings = _settings(game)
    settings.update({key: str(value) for mod in active for key, value in mod.cvars.items()})
    # TNC only: a mod carrying the Cheats & QoL tweaks the retail gate
    # rejects as +args, appended to cfile:default.cfg -- see with_tnc_tweaks.
    to_apply, _args_unused, refusal = modlib.with_tnc_tweaks(install, active, settings)
    if refusal:
        install.close()
        raise SystemExit(refusal)
    if not to_apply:
        print("No enabled mods for %s." % game.title.name)
        install.close()
        return
    print("Applying %d mod(s): %s" % (len(to_apply), ", ".join(m.id for m in to_apply)))
    try:
        report = _apply(install, to_apply)
    except (PatchError, TncPatchError) as exc:
        raise SystemExit(str(exc))
    if report["ok"]:
        j = report["journal"]
        print("OK: %d asset(s) patched." % len(j["assets"]))
        if j["missing"]:
            print("  Not found: %s" % ", ".join(j["missing"]))
    else:
        print("%s." % report["kopf"])
    install.close()


def _apply(install, active):
    """apply_mods, its conflicts and errors printed, plus a true headline for a
    refusal ("kopf"): apply_mods may already have reverted the old mods."""
    before = install.is_modified()
    report = modlib.apply_mods(install, active)
    for c in report["conflicts"]:
        print("  ! Conflict %s" % c)
    for e in report["errors"]:
        print("  ! %s" % e)
    reset = before and not report["ok"] and not install.is_modified()
    report["kopf"] = ("Cancelled - the old mods were reverted, the game is back to its "
                      "original state" if reset else "Cancelled - nothing was changed")
    return report


def cmd_revert(args):
    install = _install(_game(args))
    try:
        install.revert()
        print("The game is back to its original state.")
    except (PatchError, TncPatchError) as exc:
        raise SystemExit(str(exc))
    install.close()


def cmd_new_mod(args):
    root = MODS_DIR / args.id.replace(".", "_")
    if root.exists():
        raise SystemExit("Already exists: %s" % root)
    (root / "decls").mkdir(parents=True)
    manifest = {
        "id": args.id,
        "name": args.name or args.id,
        "version": "1.0.0",
        "author": "",
        "description": "",
        "priority": 100,
        "assets": {
            "damage:damage/tungsten/buckshot": {
                "set": {"edit.damageParms.maxDamage": 40}
            }
        },
    }
    (root / "mod.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), "utf-8")
    print("Mod skeleton created: %s" % root)
    print("Next step: wolfsdk paths damage:damage/tungsten/buckshot")


def cmd_enable(args):
    cfg = load_config()
    mods, _ = modlib.discover(MODS_DIR)
    known = {m.id for m in mods}
    enabled = set(cfg.get("enabled_mods", []))
    for mod_id in args.ids:
        if mod_id not in known:
            print("  ! unknown: %s" % mod_id)
            continue
        if args.off:
            enabled.discard(mod_id)
        else:
            enabled.add(mod_id)
    cfg["enabled_mods"] = sorted(enabled)
    save_config(cfg)
    print("Enabled: %s" % (", ".join(cfg["enabled_mods"]) or "-"))


def cmd_map_status(args):
    """What dlc/dlc_0 holds: none, ours, hand-made or foreign; installed chunk."""
    game = Game.find(args.game, "tnc")
    for line in mapinstall.describe(mapinstall.status(game, sha=True)):
        print(line)


def cmd_map_install(args):
    print(mapinstall.install(Game.find(args.game, "tnc"), args.datei))


def cmd_map_uninstall(args):
    print(mapinstall.uninstall(Game.find(args.game, "tnc")))


def cmd_gui(args):
    from . import gui

    gui.main(getattr(args, "game", None))


def cmd_maps(args):
    """The Studio: local server (127.0.0.1) plus a fullscreen app window.
    'maps [id]' opens it on the maps (a Wolfenstein II map id opens that map),
    'studio [bereich]' on a section."""
    import importlib.util
    import time
    if importlib.util.find_spec(__package__ + ".mapserver") is None:   # the loader package ships without it
        raise SystemExit("The Studio is not part of this package. It comes with WolfSDK Studio "
                         "(WolfSDK-Studio-<version>.zip), the package for modders.")
    from . import mapcatalog, mapserver

    game = Game.find(getattr(args, "game", None), "tnc")
    map_id = getattr(args, "map_id", None)
    try:
        if map_id and map_id not in mapcatalog._plan(game.root):
            raise SystemExit("Unknown map: %s" % map_id)
        url = mapserver.url_for(mapserver.start(game.root), map_id)
    except (OSError, ValueError) as exc:
        raise SystemExit("The Studio could not be started: %s" % exc)
    section = getattr(args, "bereich", None)
    if section:
        url += "?s=" + STUDIO_SECTIONS.get(section, section)
    print("Studio: %s  (Ctrl+C quits)" % url, flush=True)
    if not args.no_browser:
        mapserver.open_app(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        mapserver.stop()


# -- wiring -----------------------------------------------------------------

# English section names for 'studio [section]'; the Studio's own URL keys are
# German. The German names are still accepted.
STUDIO_SECTIONS = {"maps": "karten", "textures": "texturen", "texts": "texte"}


def build_parser():
    p = argparse.ArgumentParser(
        prog="wolfsdk",
        description="Mod loader and mod SDK for Wolfenstein: The New Order and The New Colossus",
    )
    p.add_argument("--game", help="path to the game installation")
    p.add_argument("--title", choices=sorted(TITLES),
                   help="which game: tno = The New Order, tnc = The New Colossus")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="show the installation and its patch state").set_defaults(fn=cmd_info)
    sub.add_parser("games", help="list the installed Wolfenstein games").set_defaults(fn=cmd_games)
    sub.add_parser("types", help="list asset types with their counts").set_defaults(fn=cmd_types)
    sub.add_parser("gui", help="open the loader window").set_defaults(fn=cmd_gui)

    s = sub.add_parser("maps", help="open the Studio on the maps (3D, fullscreen)")
    s.add_argument("map_id", nargs="?", help="e.g. game/dlc/c02/c2v1 (otherwise the map list)")
    s.add_argument("--no-browser", action="store_true",
                   help="only start the server and print its address")
    s.set_defaults(fn=cmd_maps)

    s = sub.add_parser("studio", help="open the Studio: maps, video, audio, textures, texts (fullscreen)")
    s.add_argument("bereich", nargs="?", metavar="section",
                   type=lambda v: {"karten": "maps", "texturen": "textures", "texte": "texts"}.get(v, v),
                   choices=["maps", "video", "audio", "textures", "texts"],
                   help="start in this section: maps, video, audio, textures or texts "
                        "(otherwise the one used last)")
    s.add_argument("--no-browser", action="store_true",
                   help="only start the server and print its address")
    s.set_defaults(fn=cmd_maps)

    s = sub.add_parser("list", help="list assets")
    s.add_argument("--type")
    s.add_argument("--grep")
    s.add_argument("--limit", type=int, default=200)
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("cat", help="print an asset's content")
    s.add_argument("asset", help="type:name")
    s.add_argument("--raw", action="store_true", help="raw bytes instead of text")
    s.set_defaults(fn=cmd_cat)

    s = sub.add_parser("paths", help="list the editable paths of a decl")
    s.add_argument("asset", help="type:name")
    s.add_argument("--grep")
    s.add_argument("--blocks", action="store_true", help="show blocks too")
    s.set_defaults(fn=cmd_paths)

    s = sub.add_parser("extract", help="extract assets to disk")
    s.add_argument("outdir")
    s.add_argument("--type")
    s.add_argument("--grep")
    s.add_argument("--type-suffix", action="store_true", help="append .decl")
    s.set_defaults(fn=cmd_extract)

    s = sub.add_parser("image-info", help="analyse a BIM image (format, mips)")
    s.add_argument("asset", help="image:name or just the name")
    s.set_defaults(fn=cmd_image_info)

    s = sub.add_parser("image-export", help="write a texture out as PNG")
    s.add_argument("asset", help="image:name or just the name")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_image_export)

    s = sub.add_parser("image-import", help="make a texture-replacement mod from a PNG")
    s.add_argument("asset", help="image:name or just the name")
    s.add_argument("png")
    s.add_argument("--mod", default="textures", help="target folder under mods/")
    s.set_defaults(fn=cmd_image_import)

    s = sub.add_parser("tweaks", help="show the cheat and QoL catalogue")
    s.add_argument("--all", action="store_true")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_tweaks)

    s = sub.add_parser("launch", help="start the game with cheats/QoL")
    s.add_argument("--set", action="append", metavar="CVAR=VALUE")
    s.add_argument("--god", action="store_true")
    s.add_argument("--ammo", action="store_true", help="infinite ammo")
    s.add_argument("--console", action="store_true", help="set com_allowConsole 1 (does not open the console; Ctrl+^ does)")
    s.add_argument("--skip-intro", action="store_true")
    s.add_argument("--fov", type=int)
    s.add_argument("--binds", action="store_true", help="bind F1-F4")
    s.add_argument("--print-only", action="store_true", help="only show the arguments")
    s.set_defaults(fn=cmd_launch)

    s = sub.add_parser("play", help="apply the enabled mods (if needed) and start the game")
    s.add_argument("--binds", action="store_true", help="pass the key bindings too")
    s.set_defaults(fn=cmd_play)

    s = sub.add_parser("ingame-menu", help="add the mod page to the in-game DevGUI")
    s.add_argument("--preview", action="store_true", help="only show it, write nothing")
    s.add_argument("--remove", action="store_true", help="remove it again (reverts all mod patches)")
    s.set_defaults(fn=cmd_ingame_menu)

    s = sub.add_parser("mods", help="list mods")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_mods)

    s = sub.add_parser("check", help="fully check the mod set without writing anything")
    s.add_argument("--all", action="store_true",
                   help="check all mods for this game, not just the enabled ones")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("enable", help="enable/disable mods")
    s.add_argument("ids", nargs="+")
    s.add_argument("--off", action="store_true")
    s.set_defaults(fn=cmd_enable)

    sub.add_parser("apply", help="write the enabled mods into the game").set_defaults(fn=cmd_apply)
    sub.add_parser("revert", help="restore the original game").set_defaults(fn=cmd_revert)

    sub.add_parser("map-status", help="custom map (dlc/dlc_0): show its state"
                   ).set_defaults(fn=cmd_map_status)
    s = sub.add_parser("map-install", help="install a custom map (replaces c2v1)")
    s.add_argument("datei", metavar="file", help="map container, e.g. ...\\chunk_22.resources")
    s.set_defaults(fn=cmd_map_install)
    sub.add_parser("map-uninstall", help="remove the custom map (c2v1 is original again)"
                   ).set_defaults(fn=cmd_map_uninstall)

    s = sub.add_parser("new-mod", help="create a mod skeleton")
    s.add_argument("id")
    s.add_argument("--name")
    s.set_defaults(fn=cmd_new_mod)

    return p


def main(argv=None):
    # The Windows console defaults to cp1252, which mangles non-ASCII text
    # (paths, mod names, the ellipsis) in this tool's output.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    # ModError carries the "game is running" refusal from apply/play and
    # every map command; a patcher error is a message too, not a traceback.
    except (GameError, modlib.ModError, *modlib.PATCH_ERRORS) as exc:
        raise SystemExit(str(exc))


if __name__ == "__main__":
    main()
