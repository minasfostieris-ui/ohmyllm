# OhMyLLM Implementation Plan

Goal: production-grade local-first agentic AI platform (Ollama + LM Studio), polished Textual TUI,
permission-gated tools, persistent `soul.md` memory, full chat-session manager, global commands
`ohmyllm` / `omllm` / `OMLLM`, complete tests + docs.

## Phases

### P0 Scaffold
- [x] Project tree, pyproject (setuptools src layout, 3 console scripts), planning docs
- [x] venv + deps (textual 8.2.8, rich, typer 0.27, pydantic 2.14, httpx 0.28, pytest, ruff, mypy)

### P1 Core libraries (typed, tested, TUI-free)
- [x] `paths` — OMLLM_HOME, config/logs/backups/history dirs, project-root detection
- [x] `config` — pydantic models + hand-rolled TOML emitter/reader (tomllib); atomic save
- [x] `safety` — PermissionMode (strict/safe/sandboxed/dangerous/full_access), RiskScorer
      (destructive patterns, command risk), PolicyEngine (path allow/deny, workspace confinement,
      per-mode tool gates), ApprovalManager (session rules + permanent rules), AuditLogger
      (JSONL + secret redaction)
- [x] `memory` — soul.md manager: sections, append/upsert (dedupe by similarity)/search/forget,
      backups, changelog, injection cap, secret refusal
- [x] `sessions` — chat session store (JSON per session), title auto-gen, rename, pin, tags,
      search/filter, delete→trash, export md/json, project-local vs global storage
- [x] `vram` — GPU detection (nvidia-smi / rocm-smi / sysfs / macOS sysctl / PowerShell) +
      heuristic estimator (size, quantization, params, ctx, kv cache, overhead, offload split)
- [x] `backends` — BaseBackend ABC, OllamaBackend (/api/version,tags,chat,pull,show; auto-start
      `ollama serve`; NDJSON streaming; native tools), LMStudioBackend (/v1/models,
      /v1/chat/completions SSE; health; auto-start best effort; instruction fallback), MockBackend
      (scripted, emits memory tool calls; dev/test mode)
- [x] `agents` — PromptBuilder (base + soul + tool protocol), ToolCallParser (native + ReAct +
      fenced JSON + repair), ToolCallValidator, AgentRuntime (steps, retries, timeouts,
      cancellation, events, tokens/s; permission engine is the ONLY gate to execution)
- [x] `tools` — soul_*, read_file, list_dir, search_files, write_file (backup+diff preview),
      patch_file, run_command, http_fetch (disabled by default), get_system_status
- [x] `doctor` — checks w/ pass/fail/warn + fixes; `selfinstall` — global aliases/shims per OS
- [x] `plain` — non-TUI Rich chat fallback

### P2 TUI (Textual)
- [x] App shell: dark theme, status bar (backend/model/mode/VRAM/tok-s/context), toasts,
      command palette (Ctrl+K), global bindings
- [x] Chat/Agent screen: streaming markdown, syntax highlight, tool activity panel, memory
      indicator, error panel, progress
- [x] Chats screen: browse/search/filter/open/rename/delete(→trash,export offer)/export/pin/new,
      draft preserved across screen switches
- [x] Model manager screen (list, search, VRAM est, tool-suitability reason, pull, test ping,
      per-model settings)
- [x] Backend/server screen (health, start/stop, instructions panel)
- [x] Permission mode screen (5 modes + warnings, full_access phrase gate)
- [x] Memory/soul screen (view/edit, recent additions, revert backup, clear, allow-agent-writes)
- [x] Settings screen (generation, UI, memory, safety, server ports, auto-start)
- [x] Doctor screen + Help screen
- [x] First-run wizard (backend detect → choose → start → health → models → default → gen
      settings → VRAM → permission mode → soul init → done)
- [x] Approval modal (tool name/args/risk/mode, diff preview for writes, exact cmd + cwd + risk
      for shell; Allow / Deny / Allow-session / permanent rule)

### P3 CLI
- [x] typer: TUI default, `chat [--plain]`, `models`, `backend [show|set]`, `doctor`, `init`,
      `soul`, `run "<task>"`, `logs`, `audit`, `version`, `self-install`, `chats
      list|open|delete|export`, global `--plain`

### P4 Install + packaging
- [x] scripts/install.sh, scripts/install.ps1 (detect python, isolated venv, install, aliases,
      PATH instructions, doctor run)
- [x] verify ohmyllm/omllm/OMLLM launch from clean PATH

### P5 Tests (pytest)
- [x] unit: config roundtrip, policy decisions (every mode × tool class), risk scorer,
      soul append/upsert/search/forget/backups, sessions store incl. project-vs-global,
      VRAM estimator math, GPU parse (fixtures), parser/repair/validation, backend health+stream
      (mock transports), alias/shim logic, doctor checks
- [x] integration: mock Ollama/LM Studio servers (httpx.MockTransport), memory write flow,
      permission-blocked flow, approval flow, streaming, plain mode
- [x] e2e smoke: init → soul created → mock model chosen → "my name is Minas" → memory tool
      executed → soul.md updated → audit.jsonl has entry
- [x] TUI headless smoke: screens mount, wizard completes, chats CRUD, palette, approvals modal

### P6 Quality + docs
- [x] ruff + mypy clean; compileall
- [x] README (features/install/quickstart/backends/permissions/memory/shortcuts/CLI/troubleshooting/
      FAQ/security), docs/PERMISSIONS.md, docs/MEMORY.md, docs/BACKENDS.md, docs/TROUBLESHOOTING.md
- [x] Planning docs kept current each loop

### P7 Agent-loop verification
- Repeatedly run pytest, ruff, mypy, headless TUI smoke, CLI smoke (`--plain`, `run`, `doctor`);
  diagnose and fix until everything passes; then final report.

## Definition of Done = user's DoD checklist (24 items) + chat menu DoD (8 items).


### P8 Tauri GUI (user request)
- [x] `ohmyllm gui` boots a native Tauri window; the Python CLI is the backend via a
      local loopback bridge (`src/ohmyllm/bridge.py`: stdlib HTTP + SSE, token auth)
- [x] Rust shell (`gui/src-tauri/`): spawns/stops the bridge, serves static frontend
- [x] Web UI (`gui/`): hand-written HTML/CSS/JS — chat streaming, approvals with diff,
      chats browser, models with VRAM, permissions, soul editor, doctor; polished per
      web-design/frontend skills
- [x] Bridge API tests (pytest against ephemeral real server)
- [x] cargo check + docs + PATH/alias integration