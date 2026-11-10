# Contributing to OhMyLLM

Thanks for your interest! OhMyLLM is a local-first agentic AI platform for
Ollama & LM Studio — a TUI, a plain CLI and a GUI sharing one agent engine,
one permission engine and one memory file. This guide is short on purpose.

## Ground rules

1. **Local-first is non-negotiable.** Nothing may phone home, auto-download a
   model, or spawn a local server (`ollama serve`, LM Studio) behind the
   user's back. Spawning happens only where the user explicitly asks
   (`oml backend <name> --start`, `oml run`, the wizard).
2. **The permission engine is sacred.** Every tool call passes through
   `src/ohmyllm/safety/policy.py`. New tools must be classified in
   `TOOL_CLASSES` and gated in all five permission modes. Catastrophic
   commands (`rm -rf /`, fork bombs, `mkfs`, …) stay blocked in every mode
   and through `/yolo` — the only escape hatch is the explicit
   `safety.catastrophic_override` config flag.
3. **Secrets never reach stdout, logs or the UI.** Keys are stored in
   `~/.ohmyllm/config.toml` (chmod 600) and displayed masked everywhere;
   env vars win over the file. Audit logs redact.
4. **Small models are first-class citizens.** Tool-call parsing must keep
   working via fenced JSON and ReAct fallback, not just native tool calls.

## Development setup

```bash
git clone https://github.com/minasfostieris-ui/ohmyllm.git
cd ohmyllm
uv venv && uv pip install -e ".[dev]"    # or: python -m venv .venv && pip install -e .
```

Python ≥ 3.11. The GUI needs nothing extra (static HTML/JS served by the
bridge); the native Tauri window is optional (`gui/`).

## Running the checks

Every PR must pass all of these:

```bash
pytest tests/ -q                      # the suite (~470 tests, no network needed)
ruff check src tests                  # lint
python -m mypy src/ohmyllm            # types
python scripts/tui_smoke.py           # boots the real TUI headless, wizard + chat + screens
node --check src/ohmyllm/gui_static/app.js   # GUI static layer syntax
```

Tests never require a running Ollama/LM Studio — the mock backend is a fully
real scripted agent. Use `OMLLM_HOME=/tmp/somewhere` in experiments so you
never touch a real config with real keys.

## Style

- Conventional-ish commit prefixes: `feat:`, `fix:`, `docs:`, `chore:`.
- Keep changes focused; one behavior per PR.
- Prefer extending existing abstractions (`Services`, `PolicyEngine`,
  `ToolRegistry`, `PromptBuilder`) over parallel systems.
- Docstrings and comments say *why*; the code says *what*.

## Proposing a feature

Open a **Discussions → Ideas** thread first for anything that changes
behavior or adds a tool — you'll get faster feedback there than in a PR.
Bugs and small fixes can go straight to a PR.

## What gets rejected

- Cloud-only features or telemetry "for convenience".
- Tools without a permission class and a policy test.
- Dependencies that don't earn their size (this runs on a laptop with a 12 GB GPU).
