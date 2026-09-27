#!/usr/bin/env python3
"""Check or set the SDK version across every binding.

    python scripts/sync_version.py            # check that all versions match
    python scripts/sync_version.py 0.2.0      # set a new version everywhere
"""

from __future__ import annotations

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (file, regex with one group around the version)
TARGETS = [
    ("sdk/python/pyproject.toml", r'(?m)^version = "([^"]+)"'),
    ("sdk/python/src/wolfsdk/__init__.py", r'__version__ = "([^"]+)"'),
    ("sdk/typescript/package.json", r'(?m)^  "version": "([^"]+)"'),
    ("sdk/rust/Cargo.toml", r'(?m)^version = "([^"]+)"'),
    ("sdk/go/version.go", r'const Version = "([^"]+)"'),
    ("spec/SPEC.md", r"Version: \*\*([^*]+)\*\*"),
]


def main() -> int:
    new = sys.argv[1] if len(sys.argv) > 1 else None
    if new and not re.fullmatch(r"\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?", new):
        print(f"not a semantic version: {new}")
        return 2
    found = {}
    for rel, pattern in TARGETS:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            print(f"missing: {rel}")
            return 1
        with open(path, encoding="utf-8") as f:
            text = f.read()
        m = re.search(pattern, text)
        if not m:
            print(f"no version found in {rel}")
            return 1
        found[rel] = m.group(1)
        if new:
            text = text[: m.start(1)] + new + text[m.end(1):]
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
    if new:
        lock = os.path.join(ROOT, "sdk/typescript/package-lock.json")
        if os.path.exists(lock):
            with open(lock, encoding="utf-8") as f:
                data = json.load(f)
            data["version"] = new
            data.get("packages", {}).get("", {})["version"] = new
            with open(lock, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
        print(f"set version {new} in {len(TARGETS)} files")
        return 0
    versions = set(found.values())
    for rel, v in found.items():
        print(f"{v:10} {rel}")
    if len(versions) != 1:
        print("version mismatch")
        return 1
    tag = os.environ.get("GITHUB_REF_NAME", "")
    if tag.startswith("v") and tag[1:] != versions.pop():
        print(f"tag {tag} does not match the SDK version")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
