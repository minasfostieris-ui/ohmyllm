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

## Reasoning-model compatibility

How each family's thinking reaches OhMyLLM, and what we do about it. Servers
that parse reasoning into their own field are handled field-first; models
whose servers leak the raw chat-template tags into `delta.content` are caught
by the streaming tag splitter (`backends/thinksplit.py`), so tags and thoughts
never reach the chat, the parser, or the stored session.

| Family | Thinking arrives as | Effort control | In OhMyLLM |
|---|---|---|---|
| Qwen3 / Qwen3.5 / QwQ (LM Studio parsed, Ollama) | `delta.reasoning` / `msg.thinking` | `reasoning_effort` low/medium/high; `enable_thinking` (template) | ✅ field + `/think`, `/ultrathink` |
| Qwen3 / R1-Distill on raw llama.cpp / vLLM w/o parser | inline `...` inside content | template kwarg only | ✅ streaming tag splitter routes to reasoning |
| DeepSeek-R1 (+ distills) | `reasoning_content` (vLLM parser) or inline `...` | none (always thinks) | ✅ both paths |
| Kimi K2/K2-Thinking (K3 lineage) | `◁think▷ … ◁/think▷` triangle delimiters (SGLang/vLLM `kimi_k2` parser) or inline raw | none (thinking models always think) | ✅ splitter variant |
| GLM-4.7 / GLM-5.x / **GLM-5.3-Flash** | `reasoning_content` field via vLLM/SGLang `glm45`/`glm47` parsers; raw GGUF (unsloth et al.) streams inline `…` | **5.3 series: forced thinking, cannot be disabled**; interleaved thinking between tool calls (each block tested) | ✅ field + splitter; `/think off` is a no-op there by design |
| Qwen3.8 (Max/Flash/Flash-Next) | template `reasoning_effort` xhigh(default)/high/medium/low + `enable_thinking`; **LM Studio GGUF path exposes only on/off** (lmstudio-bug-tracker #2284) | `chat_template_kwargs` on vLLM/SGLang; LM Studio: on/off | ✅ field + splitter; we send `high` (xhigh 400s/degrades on LM Studio) |
| gpt-oss-20b/120b | `delta.reasoning` (LM Studio/OGX parser); thinking-only fallback | `reasoning_effort` low/medium/high (`none` disables) | ✅ + empty-content fallback kept |
| Nemotron-Cascade / Lightning | `reasoning` field when server parses, else content-only (RL, no tags) | `reasoning_effort` on newer servers | ✅ field + splitter passthrough |
| Phi/Minerva, OpenAI o-style proxies | `reasoning`/`reasoning_content` fields | `reasoning_effort` | ✅ passthrough |
| Think-free RL coders (e.g. sushi-coder-rl) | reasoning field, effort knob may be a no-op | — | ✅ critique-and-revise pass does the extra thinking |

`/ultrathink` deliberately does **not** rely on any of these knobs: the
critique-and-revise pass is plain prompting, so it multiplies thinking depth
(≈2.8x measured on Qwen3.5-9B) on every row of this table. ### What the ladder really does (measured)

`/think off|low|medium|high|xhigh|max` maps to `reasoning_effort` (LM Studio,
vLLM, SGLang) or `think` (Ollama graded servers). Whether levels *change
thinking length* depends on the model:

- **Template-honoring models** (Qwen3/3.5 hybrid, gpt-oss, GLM-4.6 hybrid,
  Qwen3.8 via vLLM `chat_template_kwargs`): levels are respected — more
  effort, more thinking.
- **Forced/RL thinkers** (sushi-coder-rl, GLM-5.3 series, Qwen3-Thinking-2507):
  thinking length is trained in; levels are near-no-ops and `off` cannot
  disable it (measured on Qwen3.5-9B-sushi-coder: 1062–1894 reasoning chars
  with NO monotonic trend across levels). The UI still reflects the setting;
  the model just doesn't obey.
- **`/ultrathink` beats every level on every model** because it does not rely
  on the knob: it adds a full critique-and-revise pass (measured 4.6x total
  reasoning on the same forced thinker vs baseline).

`thinking_budget`
is opt-in per model in `[generation]`. Unloaded-model servers that answer
`200 + JSON` instead of SSE now raise a real error instead of streaming
silence. (LM Studio honors it; values above the
model's own max are clamped server-side).

Untested-here honesty: the 100B+ rows are format support verified against
each server's documented wire format and simulated in
`tests/test_thinksplit.py` (chunk-by-chunk, tags split across deltas), not
against a live GPU — we do not own one that big.
