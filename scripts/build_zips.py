"""Package the project into the two delivery zips for the agent-generator pipeline.

Writes TWO zips under dist/, both rooted at one top-level folder so they unzip on top
of each other into a single complete, runnable project:

- framework.zip       -- the IMMUTABLE base (framework/, db/, main.py, README, .python-version).
  Never varies per agent; the generator serves it static and overlays a per-request
  agent.zip into it.
- agent-template.zip  -- the MODIFIABLE surface (agent/, pyproject.toml, .env.example) as
  TOKENIZED {{PLACEHOLDER}} templates. It carries NO dev data: the agent files ship from
  clean, hand-authored templates under scripts/agent_template/ (never the real agent/), and
  pyproject is hardened (private index stripped) + tokenized (name/description/version). The
  generator fills the tokens (see SPEC.md Section 8.6 for the token contract).

Each manifest entry pairs a path/glob with a Transform:
    (content: bytes, filename: str, relpath: str) -> (bytes, new_filename, new_relpath)
passthrough ships a file unchanged; named transforms edit content / rename / re-path it; a
Rule.target overrides the in-zip path (maps scripts/agent_template/X.tmpl -> agent/X).

No-leak guarantee: after building, every agent-template entry is scanned against a dev-term
denylist + a non-ASCII check and the build FAILS if anything leaks (fail-closed).

Run:
    uv run scripts/build_zips.py [--out dist] [--name agent-framework]
"""

from __future__ import annotations

import argparse
import re
import tomllib
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Transform: (content, filename, relpath) -> (content, new_filename, new_relpath).
# relpath is POSIX and relative to the project root (== the in-zip path under the top folder).
Transform = Callable[[bytes, str, str], tuple[bytes, str, str]]


def passthrough(content: bytes, name: str, path: str) -> tuple[bytes, str, str]:
    """Ship a file unchanged."""
    return content, name, path


# Text suffixes get CRLF -> LF so emitted files are clean cross-platform (every file we
# ship is text; no binaries live in the manifests).
_TEXT_SUFFIXES = {
    ".py",
    ".toml",
    ".md",
    ".sql",
    ".example",
    ".txt",
    ".cfg",
    ".ini",
    ".yaml",
    ".yml",
}
_TEXT_NAMES = {".python-version", "Dockerfile", "Dockerfile.dev", ".dockerignore"}

# Belt-and-suspenders: never ship these even if a glob picks them up. Secrets (.env,
# .netrc) and build junk. The manifests are already narrow (extension-scoped globs), so
# this mainly guards __pycache__ under framework/agent.
_EXCLUDE_NAMES = {".env", ".netrc"}
_EXCLUDE_SUFFIXES = {".pyc", ".pyo"}

# Fail-closed leak guard: agent-template entries must contain none of these terms
# (case-insensitive regexes) and no non-ASCII bytes.
#
# The repo ships a neutral example agent, so this list is empty by default. If you
# develop a PRIVATE agent in agent/, add its domain vocabulary here (tool names,
# internal system names, team codes) so a build fails loudly rather than shipping
# your domain inside the tokenized template. The non-ASCII check runs regardless.
_DEV_DENYLIST: tuple[str, ...] = ()


@dataclass(frozen=True)
class Rule:
    """One manifest entry: which files, how to transform them, and whether they're required."""

    pattern: str  # a glob (supports **) or an exact relpath, resolved against ROOT
    transform: Transform = passthrough
    optional: bool = False  # no match => skip with a note instead of failing
    fallback: str = (
        ""  # tried when `pattern` matches nothing (e.g. README.codegen.md -> README.md)
    )
    target: str = (
        ""  # override the in-zip relpath (single-file rules only, e.g. .tmpl -> agent/X)
    )


# --- Named transforms -------------------------------------------------------------

_PROD_INDEX_GUIDANCE = """\
# --- Prod deployment: private package index (optional) ---
# The public PyPI default is used out of the box, so this project installs with no
# credentials -- easy to clone and experiment. To pull dependencies from a private
# index instead (e.g. a corporate package index), uncomment and adjust:
#
# [[tool.uv.index]]
# name = "my-private-index"
# url = "https://my-index.example.com/api/pypi/pypi/simple"
# default = true
# authenticate = "always"
"""


def harden_pyproject(content: bytes, name: str, path: str) -> tuple[bytes, str, str]:
    """Make pyproject a template: strip the private ``[[tool.uv.index]]`` block (emitted
    project installs from public PyPI), append commented prod-index guidance, and tokenize the
    ``[project]`` name / description / version so the generator fills them."""
    text = content.decode("utf-8")
    # Drop the [[tool.uv.index]] table through the next table header or end of file.
    text = re.sub(r"\n*\[\[tool\.uv\.index\]\].*?(?=\n\[|\Z)", "\n", text, flags=re.DOTALL)
    # Tokenize the [project] scalars (each appears once near the top).
    text = re.sub(r'(?m)^name = ".*"$', 'name = "{{PROJECT_NAME}}"', text, count=1)
    text = re.sub(r'(?m)^version = ".*"$', 'version = "{{PROJECT_VERSION}}"', text, count=1)
    text = re.sub(
        r'(?m)^description = ".*"$', 'description = "{{PROJECT_DESCRIPTION}}"', text, count=1
    )
    text = text.rstrip() + "\n\n" + _PROD_INDEX_GUIDANCE
    return text.encode("utf-8"), name, path


def rename_readme(content: bytes, name: str, path: str) -> tuple[bytes, str, str]:
    """Emit the codegen readme (or the README.md fallback) as the project's README.md."""
    return content, "README.md", "README.md"


# --- Manifests --------------------------------------------------------------------

_TPL = "scripts/agent_template"  # clean, tokenized agent templates (no dev data)

FRAMEWORK_MANIFEST: list[Rule] = [
    Rule("framework/**/*.py", passthrough),
    Rule("db/**/*.sql", passthrough),  # committed reference DDL only; *.generated.sql excluded
    Rule("scripts/update_ddl.py", passthrough),  # ship so the agent can regen prefixed DDL
    Rule("main.py", passthrough),
    # Deployment assets: generic + immutable across agents (config is all env vars),
    # so they ship static here. Per-agent k8s manifests are tokenized separately.
    Rule("Dockerfile", passthrough),
    Rule("Dockerfile.dev", passthrough),
    Rule(".dockerignore", passthrough),
    Rule("compose.yaml", passthrough),
    Rule("README.codegen.md", rename_readme, fallback="README.md"),
    # AI-agent guide for the generated project; authored as AGENTS.codegen.md so the
    # base repo isn't given a misleading "don't edit framework/" file (like README).
    Rule("AGENTS.codegen.md", target="AGENTS.md"),
    Rule(".python-version", passthrough, optional=True),
]

AGENT_MANIFEST: list[Rule] = [
    # The agent surface ships as TOKENIZED templates authored under scripts/agent_template/
    # (never the real agent/ files) -> zero dev-data leakage. `target` re-paths each in place.
    Rule(f"{_TPL}/__init__.py.tmpl", target="agent/__init__.py"),
    Rule(f"{_TPL}/agent.py.tmpl", target="agent/agent.py"),
    Rule(f"{_TPL}/config.py.tmpl", target="agent/config.py"),
    Rule(f"{_TPL}/prompts.py.tmpl", target="agent/prompts.py"),
    Rule(f"{_TPL}/tools.py.tmpl", target="agent/tools.py"),
    Rule(f"{_TPL}/env.example.tmpl", target=".env.example"),
    Rule("pyproject.toml", harden_pyproject),  # hardened + tokenized (name/desc/version)
]


# --- Build ------------------------------------------------------------------------


def _excluded(rel: Path) -> bool:
    """True for dev junk / secrets / generated files that must never be shipped."""
    if "__pycache__" in rel.parts:
        return True
    if rel.name.endswith(
        ".generated.sql"
    ):  # DBA-facing output of update_ddl.py; regenerated on site
        return True
    return rel.name in _EXCLUDE_NAMES or rel.suffix in _EXCLUDE_SUFFIXES


def _normalize(relpath: str, content: bytes) -> bytes:
    """CRLF -> LF for text files so emitted files are LF-clean on every OS."""
    p = Path(relpath)
    if p.suffix in _TEXT_SUFFIXES or p.name in _TEXT_NAMES:
        return content.replace(b"\r\n", b"\n")
    return content


def _resolve(rule: Rule) -> list[Path]:
    """Files a rule matches (its pattern, else its fallback), excluding dev junk."""
    matches = [p for p in ROOT.glob(rule.pattern) if p.is_file() and not _excluded(p)]
    if not matches and rule.fallback:
        matches = [p for p in ROOT.glob(rule.fallback) if p.is_file() and not _excluded(p)]
    return sorted(matches)


def build_zip(out_path: Path, manifest: list[Rule], top: str) -> dict[str, bytes]:
    """Build one zip from a manifest under the `top` folder; return {in-zip path: content}."""
    written: dict[str, bytes] = {}
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rule in manifest:
            files = _resolve(rule)
            if not files:
                if rule.optional:
                    print(f"  skip (optional, no match): {rule.pattern}")
                    continue
                raise SystemExit(f"manifest error: nothing matched '{rule.pattern}'")
            if rule.target and len(files) > 1:
                raise SystemExit(f"rule target set but '{rule.pattern}' matched >1 file")
            for src in files:
                relpath = src.relative_to(ROOT).as_posix()
                content, _name, newrel = rule.transform(src.read_bytes(), src.name, relpath)
                newrel = rule.target or newrel
                content = _normalize(newrel, content)
                arc = f"{top}/{newrel}"
                if arc in written:
                    raise SystemExit(f"collision: '{arc}' (from '{relpath}')")
                # Fixed timestamp (zip epoch) + 0644 perms => reproducible, stable builds.
                info = zipfile.ZipInfo(arc, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                zf.writestr(info, content)
                written[arc] = content
    return written


def _project_name() -> str:
    """Default top-folder / build identity = pyproject [project].name."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["name"]


def _assert_complete(top: str, framework: dict[str, bytes], agent: dict[str, bytes]) -> None:
    """Sanity-check the two zips together form a runnable project (fail loud otherwise)."""
    required = [
        f"{top}/framework/config.py",
        f"{top}/framework/server.py",
        f"{top}/main.py",
        f"{top}/agent/agent.py",
        f"{top}/agent/__init__.py",
        f"{top}/pyproject.toml",
    ]
    have = set(framework) | set(agent)
    missing = [r for r in required if r not in have]
    if missing:
        raise SystemExit("incomplete build; missing: " + ", ".join(missing))


def _assert_no_dev_data(entries: dict[str, bytes]) -> None:
    """Fail the build if any agent-template entry leaks dev data (denylist term / non-ASCII)."""
    problems: list[str] = []
    for arc, content in entries.items():
        try:
            low = content.decode("ascii").lower()
        except UnicodeDecodeError:
            problems.append(f"{arc}: non-ASCII bytes")
            continue
        problems += [f"{arc}: matched /{p}/" for p in _DEV_DENYLIST if re.search(p, low)]
    if problems:
        raise SystemExit("dev-data leak check FAILED:\n  " + "\n  ".join(problems))


def main() -> None:
    parser = argparse.ArgumentParser(description="Build framework.zip + agent-template.zip.")
    parser.add_argument("--out", default="dist", help="output dir (default: dist)")
    parser.add_argument(
        "--name",
        default=None,
        help="top-level folder inside the zips (default: pyproject [project].name)",
    )
    args = parser.parse_args()

    top = args.name or _project_name()
    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    fw_path = out_dir / "framework.zip"
    ag_path = out_dir / "agent-template.zip"

    print(f"building (top folder: {top}/)")
    framework = build_zip(fw_path, FRAMEWORK_MANIFEST, top)
    print(f"  framework.zip      {len(framework):3d} files")
    agent = build_zip(ag_path, AGENT_MANIFEST, top)
    print(f"  agent-template.zip {len(agent):3d} files")

    _assert_complete(top, framework, agent)
    _assert_no_dev_data(agent)  # fail-closed: no agent/dev data in the shipped template
    print("  leak check         ok (no dev data in agent-template)")
    print(f"ok -> {fw_path.relative_to(ROOT)} + {ag_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
