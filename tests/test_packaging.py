"""Packaging guard: every non-Python resource under src/ohmyllm must be declared
in [tool.setuptools.package-data], or wheel installs break at runtime
(e.g. Textual's app.tcss missing -> StylesheetError crash for real users)."""

from __future__ import annotations

import fnmatch
import tomllib
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


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
        out.append(str(f.relative_to(root)))
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
