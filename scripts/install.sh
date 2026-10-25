#!/usr/bin/env bash
# OhMyLLM installer — isolated venv, global aliases, PATH guidance, doctor.
# Usage: bash scripts/install.sh [--dir INSTALL_DIR] [--system] [--no-doctor]
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="${HOME}/.ohmyllm-app"
SYSTEM_WIDE=0
RUN_DOCTOR=1

args=("$@")
i=0
while [ $i -lt ${#args[@]} ]; do
  case "${args[$i]}" in
    --dir) i=$((i+1)); INSTALL_DIR="${args[$i]}";;
    --system) SYSTEM_WIDE=1;;
    --no-doctor) RUN_DOCTOR=0;;
    *) echo "unknown option: ${args[$i]}" >&2; exit 2;;
  esac
  i=$((i+1))
done

find_python() {
  for cand in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
      if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        echo "$cand"; return 0
      fi
    fi
  done
  return 1
}

echo "▸ OhMyLLM installer"
PY="$(find_python || true)"
if [ -z "${PY}" ]; then
  echo "✖ Python >= 3.11 not found."
  echo "  Install it first:"
  echo "    Debian/Ubuntu: sudo apt install python3.12 python3.12-venv"
  echo "    Fedora:        sudo dnf install python3.12"
  echo "    Arch:          sudo pacman -S python"
  echo "    macOS:         brew install python"
  exit 1
fi
echo "  python: $("$PY" --version 2>&1) ($(command -v "$PY"))"

BIN=""
if command -v uv >/dev/null 2>&1; then BIN="uv"; fi

if [ "$SYSTEM_WIDE" = "1" ]; then
  echo "▸ system-wide install (you asked for it)"
  if [ -n "$BIN" ]; then
    uv pip install --system "$REPO_DIR"
  else
    "$PY" -m pip install "$REPO_DIR"
  fi
else
  echo "▸ creating isolated environment at $INSTALL_DIR"
  mkdir -p "$INSTALL_DIR"
  if [ -n "$BIN" ]; then
    uv venv "$INSTALL_DIR/venv" >/dev/null 2>&1 || true
    uv pip install --python "$INSTALL_DIR/venv/bin/python" "$REPO_DIR"
  else
    "$PY" -m venv "$INSTALL_DIR/venv"
    "$INSTALL_DIR/venv/bin/python" -m pip install -q --upgrade pip
    "$INSTALL_DIR/venv/bin/python" -m pip install -q "$REPO_DIR"
  fi
  SRC="$INSTALL_DIR/venv/bin"
fi

# locate installed entry point
OHMYLLM_BIN=""
if [ "$SYSTEM_WIDE" = "1" ]; then
  OHMYLLM_BIN="$(command -v ohmyllm || true)"
else
  OHMYLLM_BIN="$SRC/ohmyllm"
fi

if [ -z "$OHMYLLM_BIN" ] || [ ! -x "$OHMYLLM_BIN" ]; then
  echo "✖ installation did not produce an 'ohmyllm' executable"
  exit 1
fi

BIN_DIR="$(dirname "$OHMYLLM_BIN")"
echo "▸ ensuring case aliases next to $OHMYLLM_BIN"
for name in omllm oml OMLLM OML; do
  target="$BIN_DIR/$name"
  [ "$name" = "omllm" ] && [ -x "$target" ] && continue
  if [ ! -e "$target" ]; then
    ln -s "$OHMYLLM_BIN" "$target" 2>/dev/null || cp "$OHMYLLM_BIN" "$target"
  fi
done

# user bin symlinks unless already on PATH
mkdir -p "$HOME/.local/bin"
if ! command -v ohmyllm >/dev/null 2>&1; then
  ln -sf "$OHMYLLM_BIN" "$HOME/.local/bin/ohmyllm" 2>/dev/null || true
  ln -sf "$BIN_DIR/omllm" "$HOME/.local/bin/omllm" 2>/dev/null || true
  ln -sf "$BIN_DIR/oml" "$HOME/.local/bin/oml" 2>/dev/null || true
  ln -sf "$BIN_DIR/OMLLM" "$HOME/.local/bin/OMLLM" 2>/dev/null || true
  ln -sf "$BIN_DIR/OML" "$HOME/.local/bin/OML" 2>/dev/null || true
fi

echo "▸ verify"
if command -v ohmyllm >/dev/null 2>&1; then
  ohmyllm --version || true
elif [ -x "$OHMYLLM_BIN" ]; then
  "$OHMYLLM_BIN" --version || true
fi

if [ "$RUN_DOCTOR" = "1" ]; then
  echo "▸ running doctor"
  if command -v ohmyllm >/dev/null 2>&1; then ohmyllm doctor || true; else "$OHMYLLM_BIN" doctor || true; fi
fi

echo
echo "✔ installed."
case ":$PATH:" in
  *":$BIN_DIR:"*) :;;
  *)
    echo "⚠ '$BIN_DIR' (and ~/.local/bin) are not on your PATH yet."
    echo "  bash/zsh — add to ~/.bashrc or ~/.zshrc:"
    echo "    export PATH=\"$BIN_DIR:\$PATH\""
    echo "  fish:"
    echo "    fish_add_path $BIN_DIR"
    echo "  then open a new terminal."
    ;;
esac

echo
echo "Next steps:"
echo "  ohmyllm            # open the TUI (first run = wizard)"
echo "  ohmyllm doctor     # environment check"
echo "  ohmyllm run 'hello'  # one-shot agent task"
