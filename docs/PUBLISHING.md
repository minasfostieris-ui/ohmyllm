# Publishing checklist

Everything below is verified locally; the CI matrix is the cross-platform
proof (there is no need for a Mac or Windows box).

## Ready

- [x] `LICENSE` (MIT) at repo root; `pyproject.toml` uses the PEP 639
      `license = "MIT"` expression (no License classifier — setuptools
      rejects both together).
- [x] `python -m build` → wheel + sdist; `twine check` passes on both.
- [x] Clean-room install of the wheel in a fresh venv: `oml --help`,
      `ohmyllm --version`, config boot — all work.
- [x] Wheel ships `gui_static/*`, `*.tcss`, `guard.py`, `LICENSE` (75 files).
- [x] Full gates: ruff, mypy, pytest (incl. `tests/test_platform.py`
      simulating Windows/macOS branches on Linux), TUI smoke, E2E,
      2000-scenario campaign.
- [x] `.gitignore` excludes `.venv/`, `build/`, `dist/`, caches,
      `gui/src-tauri/target/`, `.ohmyllm/` (user state).

## Push to GitHub

```bash
cd /data/home-overflow/Downloads/Projects/ohmyllm
git remote add origin https://github.com/<YOUR-USER>/ohmyllm.git
git branch -M main
git push -u origin main
```

Then watch **Actions → CI**: 3 OSes × Python 3.11/3.12/3.13 (tests + TUI
smoke + E2E), the 2000-scenario campaign on all three OSes (SVGs uploaded
as artifacts), and the package job (build + twine check + fresh-venv smoke).

## Publish to PyPI

```bash
uvx --from build python -m build --outdir dist .   # or: python -m build
uvx twine upload dist/*                            # needs PyPI token
```

Tag the release first: `git tag v0.1.0 && git push origin v0.1.0`.
A GitHub Release with the `dist/` artifacts attached is the friendlier
channel; PyPI gives `pipx install ohmyllm` / `uv tool install ohmyllm`.

## GUI binary (optional)

The Tauri window is not in the wheel. To ship binaries:

```bash
cd gui/src-tauri && cargo tauri build   # per OS — CI matrix runners can do this
```

`oml gui` falls back to the browser console when no native binary exists,
so the Python package is fully functional without it.

## Before every release

1. Bump `version` in `pyproject.toml` + add a CHANGELOG.md entry.
2. `scripts/preview_campaign.py 2000` (≈80 s) — must print `CAMPAIGN OK`.
3. Green CI on the tag.
