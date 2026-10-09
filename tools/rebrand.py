#!/usr/bin/env python3
"""Replace the commercial template brand placeholder across text assets."""
from __future__ import annotations

import argparse
from pathlib import Path

TEXT_EXTENSIONS = {
    ".py", ".js", ".html", ".css", ".md", ".yml", ".yaml", ".txt", ".json", ".env", ""
}
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules"}
PLACEHOLDER = "YOUR SHOP"


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebrand the shop template.")
    parser.add_argument("--name", required=True, help="New shop name.")
    parser.add_argument("--root", default=".", help="Template root directory.")
    args = parser.parse_args()

    name = str(args.name).strip()
    if not name or "\n" in name or "\r" in name:
        raise SystemExit("Shop name must be a single non-empty line.")
    if len(name) > 80:
        raise SystemExit("Shop name is too long (max 80 characters).")

    root = Path(args.root).resolve()
    if not root.is_dir():
        raise SystemExit(f"Template directory not found: {root}")

    changed = 0
    files = 0
    for path in root.rglob("*"):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        files += 1
        new_text = text.replace(PLACEHOLDER, name)
        if new_text != text:
            path.write_text(new_text, encoding="utf-8")
            changed += 1
            print(f"Updated: {path.relative_to(root)}")

    print(f"Scanned {files} text files; updated {changed} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
