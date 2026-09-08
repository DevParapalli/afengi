"""Zip the project for sharing/backup -- stdlib only, no deps.

Bundles the working tree into a single zip, skipping virtualenvs, caches, VCS
internals, build output, and runtime-generated artifacts. Secrets (.env/.netrc)
are skipped by default; pass --include-secrets to keep them.

Run:
    uv run scripts/make_zip.py                 # -> ../<project>.zip
    uv run scripts/make_zip.py --out foo.zip   # custom path
    uv run scripts/make_zip.py --include-secrets
"""

from __future__ import annotations

import argparse
import fnmatch
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Directory names pruned anywhere in the tree.
EXCLUDE_DIRS = {
    ".venv",
    ".git",
    "__pycache__",
    ".ruff_cache",
    ".pytest_cache",
    ".mypy_cache",
    "node_modules",
    "dist",
    ".idea",
    ".vs",
}
# File name globs dropped anywhere (caches + runtime-generated artifacts).
EXCLUDE_GLOBS = ("*.pyc", "*.pyo", "*.db", "*.sqlite", "*.sqlite3", "*.generated.sql")
# Exact repo-relative paths dropped (runtime dumps).
EXCLUDE_RELS = {"scripts/frames.txt", "scripts/frame.txt"}
# Secret files -- excluded unless --include-secrets.
SECRET_RELS = {".env", ".netrc"}


def _iter_files(include_secrets: bool, out_path: Path):
    """Yield (absolute_path, posix_relpath) for every file that should be zipped."""
    skip_rels = EXCLUDE_RELS if include_secrets else EXCLUDE_RELS | SECRET_RELS
    for dirpath, dirnames, filenames in ROOT.walk():
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for name in filenames:
            path = dirpath / name
            rel = path.relative_to(ROOT).as_posix()
            if path == out_path:
                continue
            if rel in skip_rels:
                continue
            if any(fnmatch.fnmatch(name, pat) for pat in EXCLUDE_GLOBS):
                continue
            yield path, rel


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Zip the project, minus caches/venv/runtime files."
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT.parent / f"{ROOT.name}.zip",
        help="Output zip path (default: ../<project>.zip).",
    )
    parser.add_argument(
        "--include-secrets",
        action="store_true",
        help="Include .env / .netrc (excluded by default).",
    )
    args = parser.parse_args()

    out_path = args.out.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    count = 0
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, rel in _iter_files(args.include_secrets, out_path):
            zf.write(path, rel)
            count += 1

    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"wrote {out_path}")
    print(f"files: {count}   size: {size_mb:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
