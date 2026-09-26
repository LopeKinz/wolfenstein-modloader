"""Command line interface: ``wolfsdk`` / ``python -m wolfsdk``."""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from typing import List, Optional

from . import __version__
from .archive import Game, validate_chunk
from .audio import parse_bsnf, stream_container_for
from .bim import decode, parse_bim
from .decl import Decl
from .errors import WolfSdkError
from .mod import Mod
from .patcher import apply, plan, revert
from .png import encode_png
from .save import fix_save, save_checksum, verify_save


def _game(args) -> Game:
    root = args.game or os.environ.get("WOLFSDK_GAME")
    if not root:
        raise SystemExit("error: pass --game PATH or set WOLFSDK_GAME")
    return Game(root)


def _split_key(key: str):
    if ":" not in key:
        raise SystemExit(f"error: asset must be 'type:name', got {key!r}")
    return key.split(":", 1)


def _read(game: Game, key: str) -> bytes:
    t, n = _split_key(key)
    data = game.read_asset(t, n)
    if data is None:
        raise SystemExit(f"error: asset {key} not found")
    return data


def cmd_info(args) -> int:
    g = _game(args)
    total = 0
    for c in g.chunks:
        total += len(c.entries)
        print(f"{c.index_name:24} {len(c.entries):7} entries  {c.resources_name}")
    print(f"{len(g.chunks)} chunks, {total} entries, {len(g.keys())} unique assets")
    return 0


def cmd_list(args) -> int:
    g = _game(args)
    seen = set()
    for occ in g:
        e = occ.entry
        if args.type and e.type != args.type:
            continue
        if args.grep and args.grep.lower() not in e.name.lower():
            continue
        if e.key in seen:
            continue
        seen.add(e.key)
        print(e.key)
    return 0


def cmd_cat(args) -> int:
    sys.stdout.write(_read(_game(args), args.asset).decode("utf-8", "replace"))
    return 0


def cmd_paths(args) -> int:
    d = Decl(_read(_game(args), args.asset).decode("utf-8"))
    width = max((len(p) for p, _, _ in d.paths()), default=0)
    for p, _, raw in d.paths():
        print(f"{p:<{width}}  {' '.join(raw.split())}")
    return 0


def cmd_extract(args) -> int:
    data = _read(_game(args), args.asset)
    with open(args.output, "wb") as f:
        f.write(data)
    print(f"{len(data)} bytes -> {args.output}")
    return 0


def cmd_image_info(args) -> int:
    b = parse_bim(_read(_game(args), args.asset))
    print(json.dumps({
        "hash": f"0x{b.hash:08x}", "type": b.texture_type, "width": b.width, "height": b.height,
        "depth": b.depth, "mips": b.mip_count, "format": b.format, "formatName": b.format_name,
        "baseWidth": b.base_width, "baseHeight": b.base_height, "header": b.header,
        "mipSizes": [[m.width, m.height, len(m.data)] for m in b.mips],
    }, indent=2))
    return 0


def cmd_image_export(args) -> int:
    w, h, rgba = decode(parse_bim(_read(_game(args), args.asset)), args.mip)
    with open(args.output, "wb") as f:
        f.write(encode_png(w, h, rgba))
    print(f"{w}x{h} -> {args.output}")
    return 0


def cmd_audio_info(args) -> int:
    g = _game(args)
    t, n = _split_key(args.asset)
    occ = g.find(t, n)
    if not occ:
        raise SystemExit(f"error: asset {args.asset} not found")
    info = [s.__dict__ | {"duration": round(s.duration, 3)} for s in parse_bsnf(g.read(occ[0]))]
    print(json.dumps({
        "container": stream_container_for(n),
        "streamOffset": occ[0].entry.stream_offset,
        "samples": info,
    }, indent=2))
    return 0


def _load_mods(paths: List[str]) -> List[Mod]:
    return [Mod.load(p) for p in paths]


def _print_plan(result) -> None:
    for c in result.conflicts:
        where = f" {c.path}" if c.path else ""
        print(f"conflict [{c.kind}] {c.asset}{where}: {', '.join(c.mods)} -> {c.winner}")
    for e in result.errors:
        print(f"error {e.asset} ({e.mod}): {e.message}")


def cmd_plan(args) -> int:
    g = _game(args)
    result = plan(g, _load_mods(args.mods))
    for key, text in result.results.items():
        t, n = key.split(":", 1)
        before = g.read_asset(t, n).decode("utf-8")
        sys.stdout.writelines(difflib.unified_diff(
            before.splitlines(True), text.splitlines(True), f"a/{key}", f"b/{key}"))
    _print_plan(result)
    return 1 if result.errors else 0


def cmd_apply(args) -> int:
    g = _game(args)
    report = apply(g, _load_mods(args.mods))
    _print_plan(report.plan)
    print(f"written: {len(report.written)}, unchanged: {len(report.unchanged)}, "
          f"rebuilt archives: {len(report.rebuilt)}")
    return 1 if report.plan.errors else 0


def cmd_revert(args) -> int:
    root = args.game or os.environ.get("WOLFSDK_GAME")
    if not root:
        raise SystemExit("error: pass --game PATH or set WOLFSDK_GAME")
    print(f"restored {revert(root)} item(s)")
    return 0


def cmd_validate(args) -> int:
    g = _game(args)
    bad = 0
    for c in g.chunks:
        for p in validate_chunk(c):
            bad += 1
            print(f"{c.resources_name}: {p}")
    print("ok" if not bad else f"{bad} problem(s)")
    return 1 if bad else 0


def cmd_save(args) -> int:
    rc = 0
    for path in args.files:
        with open(path, "rb") as f:
            data = f.read()
        ok = verify_save(data)
        if args.fix and not ok:
            with open(path, "wb") as f:
                f.write(fix_save(data))
            print(f"fixed  {path}  -> {save_checksum(data[4:]).hex()}")
        else:
            print(f"{'ok' if ok else 'BAD':5}  {path}  {data[:4].hex()}")
            rc |= 0 if ok else 1
    return rc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wolfsdk", description="Wolfenstein modding SDK")
    p.add_argument("--version", action="version", version=f"wolfsdk {__version__}")
    p.add_argument("-g", "--game", help="game directory containing base/ (or $WOLFSDK_GAME)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help, *asset):
        sp = sub.add_parser(name, help=help)
        if asset:
            sp.add_argument("asset", help="type:name")
        sp.set_defaults(fn=fn)
        return sp

    add("info", cmd_info, "summarise the archives")
    sp = add("list", cmd_list, "list assets")
    sp.add_argument("--type")
    sp.add_argument("--grep")
    add("cat", cmd_cat, "print an asset", 1)
    add("paths", cmd_paths, "list editable Decl paths", 1)
    sp = add("extract", cmd_extract, "write an asset to a file", 1)
    sp.add_argument("output")
    add("image-info", cmd_image_info, "show a BIM header", 1)
    sp = add("image-export", cmd_image_export, "export a BIM image as PNG", 1)
    sp.add_argument("output")
    sp.add_argument("--mip", type=int, default=0)
    add("audio-info", cmd_audio_info, "show a sample descriptor", 1)
    for name, fn, help in (("plan", cmd_plan, "dry run: show the diff of mods"),
                           ("apply", cmd_apply, "apply mods (journaled)")):
        sp = add(name, fn, help)
        sp.add_argument("mods", nargs="+", help="mod.json files or mod directories")
    add("revert", cmd_revert, "undo every applied change")
    add("validate", cmd_validate, "check archive invariants")
    sp = add("save", cmd_save, "verify (or --fix) save-game checksums")
    sp.add_argument("files", nargs="+")
    sp.add_argument("--fix", action="store_true")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except WolfSdkError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
