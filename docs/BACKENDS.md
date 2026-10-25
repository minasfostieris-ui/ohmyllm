# Backends

OhMyLLM speaks to local model servers only. Three backends ship in the box;
the agent runtime, policy engine, and memory layer are identical for all of
them.

## Ollama

- **API**: native HTTP (`/api/version`, `/api/tags`, `/api/show`,
  `/api/chat`, `/api/pull`) — NDJSON streaming, native tool calling, real
  token usage stats.
- **Auto-start**: if `ollama serve` isn't running, OhMyLLM launches it
  (configurable `ollama.auto_start`), waits for health, logs to
  `~/.ohmyllm/logs/ollama-serve.log`.
- **Model intelligence**: per model it reads parameter count, context length,
  quantization, and the tool-support template, then estimates minimum and
  comfortable VRAM against your actual GPU (`ohmyllm models`).
- **Pull** with live progress from the TUI/GUI models screen.
- Install: <https://ollama.com/download> — then `ollama pull qwen2.5:7b-instruct`
  or use the wizard.

## LM Studio

- **API**: OpenAI-compatible `/v1` (models, chat, SSE streaming) merged with
  the native `/api/v0` for richer metadata (state, quant, params).
- **Tool calls**: SSE deltas accumulate `tool_calls` correctly, including
  fragmented arguments; falls back to the JSON protocol when the model is
  tool-weak.
- **Auto-start**: tries the `lms` CLI first, then launching the app binary
  (`lmstudio.binary_path`); otherwise the Backend screen shows the exact
  steps (Developer → Enable Server, default `http://127.0.0.1:1234`).

## Mock

A first-class, selectable backend: a scripted agent that really executes
tool calls (memory triggers, file creation) with zero model required. It's
what the test suite and `ohmyllm gui` demos run on, and it's honest about
being fake — the UI labels it "Mock (dev/test)".

## Model quality for agents

OhMyLLM tells you when a model is a poor agent driver and why: no tool
template, tiny context, or too large for your VRAM. The runtime then picks
the right calling protocol per model:

1. **native** tool calls (recommended; automatic when supported),
2. **fenced JSON** `{"tool_calls": […]}` blocks (repaired aggressively —
   truncation, trailing commas, Python literals, prose-wrapped JSON),
3. **ReAct** `Thought/Action/Observation` as last resort.

Malformed calls bounce back to the model with a correction message instead
of crashing the turn.

## Adding a backend

Subclass `BaseBackend` (`src/ohmyllm/backends/base.py`): `is_installed`,
`health`, `ensure_running`, `list_models`, `stream_chat`, `pull`. Register it
in `backends/__init__.py` and add a `BackendName` enum member + a settings
model — the TUI, GUI, doctor, and audit pick it up automatically.
