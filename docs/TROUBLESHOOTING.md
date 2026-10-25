# Troubleshooting

First stop, always:

```bash
ohmyllm doctor          # 25+ checks, each with a fix hint
tail -n 50 ~/.ohmyllm/logs/ohmyllm.log
```

## Ollama won't start / "connection refused :11434"

- Is it installed? `command -v ollama`. If not: <https://ollama.com/download>.
- Try manually: `ollama serve` and watch for errors; OhMyLLM's own launch is
  logged to `~/.ohmyllm/logs/ollama-serve.log`.
- Different port? Set `ollama.host` in `~/.ohmyllm/config.toml` (or the
  Settings screen) to match, e.g. `http://127.0.0.1:11500`.
- WSL2/containers: `127.0.0.1` inside the distro isn't the host — point
  `ollama.host` at the host IP or run Ollama inside.

## LM Studio: "server not running"

OhMyLLM cannot make the model server appear by magic. Open LM Studio →
Developer/Server tab → **Start Server** (default `http://127.0.0.1:1234`),
or run `lms server start`. The Backend screen repeats these steps and can
try the launch for you.

## "No models found"

- Ollama: `ollama pull qwen2.5:7b-instruct` (or use Models → Pull in the
  TUI/GUI with the progress bar).
- LM Studio: download a model in the app first; then Models → Refresh.
- Just evaluating the UI? `ohmyllm backend mock` — the mock backend runs the
  full agent loop with no model installed.

## Model is too slow / crashes with OOM

`ohmyllm models` shows each model's minimum and comfortable VRAM against
your GPU. "over" means it will spill to RAM (slow) or fail. Pick a smaller
quant (Q4_K_M is the sweet spot) or lower `generation.num_ctx` — context
eats VRAM fast (≈4 KB/token/GPU-layer on most architectures).

## Approvals never appear

- Check the mode chip: `dangerous`/`full_access` skip most approvals.
- `memory.agent_writes = false` silently *denies* memory tools (the reason
  shows in the tool result and audit log).
- The GUI bridge times approvals out after 10 minutes → denied (fail-closed
  by design).

## "bad token" / GUI won't connect

- The bridge token is per-process. Reload the page from the URL `ohmyllm gui`
  printed, or restart the GUI.
- Port already in use? `ohmyllm gui --port 0` picks a free one.
- Native window says "Could not reach the OhMyLLM bridge": launch it through
  `ohmyllm gui` (it sets `OMLLM_PYTHON` for you), or export
  `OMLLM_PYTHON=/path/to/.venv/bin/python` pointing at an environment where
  `ohmyllm` is installed.

## Where things live

| Path | Contents |
|---|---|
| `~/.ohmyllm/config.toml` | settings |
| `~/.ohmyllm/soul.md` | global memory |
| `~/.ohmyllm/history/sessions/` | chat JSON |
| `~/.ohmyllm/history/trash/` | deleted chats (restorable) |
| `~/.ohmyllm/backups/` | soul + file-write backups |
| `~/.ohmyllm/logs/audit.jsonl` | every gated decision (redacted) |
| `~/.ohmyllm/logs/ohmyllm.log` | app log |
| `<project>/.ohmyllm/` | project-local soul/history (when a workspace marker exists) |

Override the root with `OMLLM_HOME`, the workspace with `OMLLM_WORKSPACE`.

## Corrupt config

OhMyLLM never dies on a bad `config.toml`: it keeps working with defaults
(field-by-field where possible), records the problem for `doctor` and the TUI
banner to surface, and leaves your file untouched so you can fix it by hand.
Unknown keys you added by hand survive round-trips.

## Uninstall

Delete `~/.ohmyllm` (config, memory, sessions, logs) and per-project
`.ohmyllm/` folders, then `pipx uninstall ohmyllm` (or remove the venv).
Nothing else was written; there is no daemon, no service, no autostart —
`ohmyllm self-install` only ever appends alias lines to your shell profile
if you run it.
