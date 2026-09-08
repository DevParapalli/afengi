"""Bump this project's version -- the single source of truth in pyproject.toml.

``[project].version`` is read at runtime (see framework/config.py) and used in the agent card
+ telemetry, so this script only touches that one line. Dependency-free: reads with tomllib,
writes with a targeted regex so the rest of the file (formatting, comments) is untouched.

Base/dev tool only -- it is NOT shipped inside a generated agent (scripts/ is excluded from
the zips). A generated agent sets its initial version from the frontend; later changes are a
one-line edit of its own pyproject.toml.

Run:
    uv run scripts/bump_version.py patch      # 0.1.0 -> 0.1.1
    uv run scripts/bump_version.py minor      # 0.1.1 -> 0.2.0
    uv run scripts/bump_version.py major      # 0.2.0 -> 1.0.0
    uv run scripts/bump_version.py --set 2.3.4
"""

from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"

# Strict major.minor.patch (no pre-release / build metadata -- the bump math assumes numbers).
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def _read_version() -> str:
    """Current [project].version from pyproject.toml."""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return data["project"]["version"]


def _bump(version: str, part: str) -> str:
    """Return version with `part` (major|minor|patch) incremented and lower parts reset."""
    m = _SEMVER.match(version)
    if not m:
        raise SystemExit(f"current version '{version}' is not X.Y.Z; bump manually")
    major, minor, patch = (int(g) for g in m.groups())
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def _write_version(new: str) -> None:
    """Replace the [project].version line in place (bytes -> line endings preserved)."""
    text = PYPROJECT.read_bytes().decode("utf-8")
    text, n = re.subn(r'(?m)^(version\s*=\s*")[^"]*(")', rf"\g<1>{new}\g<2>", text, count=1)
    if n != 1:
        raise SystemExit("could not find a version line to update in pyproject.toml")
    PYPROJECT.write_bytes(text.encode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Bump pyproject.toml [project].version.")
    parser.add_argument(
        "part", nargs="?", choices=["major", "minor", "patch"], help="which part to increment"
    )
    parser.add_argument(
        "--set", dest="set_version", metavar="X.Y.Z", help="set an exact version"
    )
    args = parser.parse_args()

    if bool(args.part) == bool(args.set_version):
        parser.error("give exactly one of: major | minor | patch | --set X.Y.Z")

    old = _read_version()
    if args.set_version:
        if not _SEMVER.match(args.set_version):
            raise SystemExit(f"--set '{args.set_version}' is not X.Y.Z")
        new = args.set_version
    else:
        new = _bump(old, args.part)

    _write_version(new)
    print(f"{old} -> {new}")


if __name__ == "__main__":
    main()
