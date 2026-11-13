"""Packaging guard: every non-Python resource under src/ohmyllm must be declared
in [tool.setuptools.package-data], or wheel installs break at runtime
(e.g. Textual's app.tcss missing -> StylesheetError crash for real users)."""

from __future__ import annotations

import fnmatch
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
PYPROJECT = ROOT / "pyproject.toml"
CREDENTIAL_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx",
    "credentials*.json", "*credentials*.json", "secrets*.json", "*token*.json",
)


def _resource_files() -> list[str]:
    out = []
    for f in sorted(SRC.rglob("*")):
        if not f.is_file():
            continue
        if f.suffix == ".py" or "__pycache__" in f.parts:
            continue
        if any(part.endswith(".egg-info") for part in f.parts):
            continue
        root = SRC / "ohmyllm"
        if root not in f.parents:
            continue
        out.append(f.relative_to(root).as_posix())
    return out


def _declared_patterns() -> dict[str, list[str]]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return data["tool"]["setuptools"]["package-data"]


def test_every_resource_is_declared_as_package_data():
    patterns = _declared_patterns()
    assert patterns, "package-data section missing"
    # flatten: package -> patterns, matching against ohmyllm-relative paths
    candidates: list[str] = []
    for pkg, globs in patterns.items():
        prefix = "" if pkg == "ohmyllm" else pkg.split("ohmyllm.", 1)[1].replace(".", "/") + "/"
        candidates += [prefix + g for g in globs]
    for rel in _resource_files():
        assert any(fnmatch.fnmatch(rel, pat) for pat in candidates), (
            f"{rel} ships nowhere: add a [tool.setuptools.package-data] glob — "
            f"wheel installs will crash without it"
        )


def test_known_resources_present():
    # if this list changes, update docs + the GUI/bridge tests together
    files = _resource_files()
    assert "ohmyllm/tui/app.tcss" in files or "tui/app.tcss" in files
    assert any("gui_static/index.html" in f for f in files)


def test_package_inventory_excludes_credential_like_resources():
    resources = _resource_files()
    for rel in resources:
        name = Path(rel).name
        assert not any(fnmatch.fnmatch(name.casefold(), pattern) for pattern in CREDENTIAL_PATTERNS), (
            f"credential-like resource must not ship: {rel}"
        )

    # Guard future broad package-data globs even when no secret file exists yet.
    package_globs = [pattern for patterns in _declared_patterns().values() for pattern in patterns]
    synthetic_secrets = (
        ".env", ".env.production", "client.pem", "private.key",
        "credentials.json", "gui_static/session-token.json",
    )
    for rel in synthetic_secrets:
        assert not any(fnmatch.fnmatch(rel, pattern) for pattern in package_globs), (
            f"package-data pattern would include credential-like artifact: {rel}"
        )


def test_credential_files_are_explicitly_ignored():
    ignored = set((ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())
    required = {".ohmyllm/", ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx",
                "credentials*.json", "*credentials*.json", "secrets*.json", "*token*.json"}
    assert required <= ignored
