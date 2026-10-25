# DECISIONS.md — OhMyLLM architecture decisions

- **Python >= 3.11, src layout, setuptools**: broad availability; console scripts give
  `ohmyllm`/`omllm`/`OMLLM` binaries per name on all OSes (case handled by separate scripts).
- **Textual for TUI, Rich for plain mode**: reactive screens, modals, mouse, bindings; headless
  `App.run_test()` makes the UI testable in CI.
- **Pydantic v2 config + hand-rolled TOML emitter; `tomllib` to read**: avoids extra deps
  (`tomli-w`/pydantic-settings) while keeping `config.toml` human-editable, per spec.
- **Project root detection** walks up from cwd for `.git`/`pyproject.toml`/`package.json`/... ;
  `~/.ohmyllm` = global; `<root>/.ohmyllm` = project-local soul.md + history (preferred when
  present/created), exactly as spec requires; `OMLLM_HOME` env override for tests.
- **Five permission modes enforced by PolicyEngine only** — model output never changes mode;
  no tool executes without an `evaluate()` decision (spec invariant, tested).
- **Tool-call paths**: native backend tools when model is known tool-capable; otherwise fenced
  JSON protocol + ReAct fallback; JSON repair for malformed calls; <=2 correction retries; all
  parsed calls re-validated against schema before execution.
- **MockBackend is a first-class dev mode** (`backend=mock`), not a fake in production paths:
  real Ollama/LM Studio code paths are exercised via httpx.MockTransport in tests.
- **Chat sessions**: one JSON file per session under `history/sessions/` — human-inspectable,
  atomic writes (tmp+os.replace); delete moves to `history/trash/` (never hard-deletes).
- **soul.md backups** to `~/.ohmyllm/backups/soul/` (keep 50) before every agent edit; file
  writes by tools back up to `backups/files/<ts>/`.
- **Secret redaction** in audit logs via regex patterns (Bearer/sk-/password=/keys); soul tool
  refuses secret-looking memories unless user explicitly asks.
- **No network at runtime by default**: httpx used only against configured localhost backends
  unless user enables `http_fetch` + approves; `http_fetch` is OFF in strict/safe/sandboxed.
- **VRAM estimation**: bytes-based (file size + non-offloaded KV cache + runtime overhead),
  quantization known, param count used when available (`/api/show`); comfortable = min + 20% +
  ctx-dependent KV; GPU totals from nvidia-smi/rocm-smi/sysfs/sysctl/PowerShell (parsed, with
  unit tests against fixture outputs).
- **full_access** requires typing the phrase `I ACCEPT FULL ACCESS` in the TUI/CLI; catastrophic
  command block stays on unless `safety.catastrophic_override=true`; every action audited.
- **Model-suggested chat titles**: optional, non-blocking; default titles derive from the first
  user message so no slow background calls are required.

## GUI phase (P8)

- **Bridge = stdlib only**: `ThreadingHTTPServer` + one asyncio loop thread; no FastAPI/uvicorn
  added. Loopback bind, ephemeral port, per-process token (`X-OMLLM-Token` or `?t=`), token
  exempt only for `/api/health` and the static CSS/JS (no secrets in them).
- **One engine, three fronts**: the bridge reuses `Services`/`AgentRuntime`/`PolicyEngine`/
  `ApprovalManager` unchanged — GUI cannot bypass any gate; approvals flow out as SSE
  `approval_request` and resolve via `POST /api/approval/<id>`, timing out to DENIED after
  10 min (fail-closed).
- **Per-server state**: `Handler.state` is a property reading `self.server.state` so multiple
  bridges (tests!) never share sessions/approvals; `serve(stop=Event)` gives clean shutdown.
- **Frontend ships inside the wheel** (`src/ohmyllm/gui_static/`, package-data) — browser mode
  and the Tauri window load the same served console; the Rust shell embeds only a loader page.
- **Tauri shell stays dumb**: spawn `python -m ohmyllm.bridge` (interpreter via
  `OMLLM_PYTHON`/sibling venv/`python3 -c import ohmyllm` probe), read port+token from a temp
  port-file, `window.navigate()` to the console, kill child on exit. No Tauri commands, no
  capabilities surface.
- **No npm, no frameworks, no CDN**: hand-written HTML/CSS/JS (vanilla, ~900 lines), system
  font stack, own Markdown renderer + tiny syntax highlighter — offline by construction.
- **Visual QA in a real browser** found and fixed: `hidden` attribute overridden by display
  rules (global `[hidden]{display:none!important}`), Enter-submit on textarea, SSE field
  mismatches (`token.text`, numeric risk), boot-token injection placeholder collision, settings
  schema bound to real config fields, truncated chat-id prefixes (`SessionStore.resolve`).

## Live-model behavior (found against real qwen2.5:1.5b / granite-4.2-3b)

- **Empty-reply nudge**: small models sometimes stream zero content (before or after tool
  calls). The runtime treats silence as a recoverable parse failure: one correction nudge
  within `agent.max_parse_retries`, then a clean empty finish — never a silent blank answer
  loop. Regression-tested both directions (recovers / terminates).
- **Model metadata is real when the server offers it**: Ollama `/api/show` keys
  (`<arch>.context_length`, `general.parameter_count`, `<arch>.block_count`) now drive ctx
  display and the estimator's layer count; LM Studio sizes come from `lms ls --json` because
  neither HTTP API exposes them. Capability flags read `tool_use` (LM Studio's token) as well
  as `tools`/`function_calling`.
