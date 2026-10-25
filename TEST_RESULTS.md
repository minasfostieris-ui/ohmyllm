# Test Results

Final verification record — every command below was run on this machine
(Linux 7.2.2, Python 3.12.14 venv, AMD 12 GiB GPU, no Ollama binary, LM
Studio app present but server stopped) and the output is what was observed.

## P9 batch — live verification (installed wheel, `uv tool install . --force`)

- **Delete/rename/revert/perms**: `call_next`→`run_worker` cutover; headless
  run: `DELETED: True`, clean exit 3.6 s, no orphaned-task warnings.
  Regression: `test_delete_chat_completes`.
- **Quit**: `test_quit_modal_single_instance_and_cancel` (double Ctrl+C →
  one modal; n/Esc cancels).
- **Interjections**: live gpt-oss run — queued message landed mid-turn;
  model reply quoted the interjected word. Bridge: `POST /api/message`
  while busy → 202 `{queued:true}` (`test_message_while_busy_is_queued`,
  `test_queues_instead_of_refusing`, `test_interjections_land_before_next_model_call`).
- **Compaction**: `test_context_compaction` — synthetic long chat →
  `compaction` event, head replaced by summary, last 8 verbatim.
- **Live preview + diffs**: `test_lmstudio_streams_tool_args_progress`;
  live write showed `✍️ gui-proof.txt` streaming then `📄 created
  gui-proof.txt +1`.
- **t/s**: gpt-oss 2.3 → 24.5–26.4 t/s after reasoning-token accounting.
- **Native + `/gui`**: `ohmyllm-gui` prints `OhMyLLM console →
  http://127.0.0.1:PORT/?t=…`; `oml gui` / `oml gui --stop` verified;
  `test_gui_slash_launches_bridge` asserts bridge launch + `gui-web.url`.
- **GUI (headless Chromium screenshots)**: dark selects/checkboxes/numbers;
  chat cards with always-visible labeled `◇ pin / ↧ export / ✎ rename / ✕
  delete` (uncut); `Save Ctrl+Enter` kbd; wizard shows all 33 models and
  **persists completion server-side** — fresh profile + reload → no wizard
  (`PATCH /api/config` route was missing a POST alias; fixed).
- Gates re-run after the batch: 163 passed, ruff clean, mypy clean (64
  files), TUI-OK, E2E-OK, wheel reinstalled.

## Automated gates

| Gate | Command | Result |
|---|---|---|
| Unit + integration | `pytest tests/` | **163 passed** |
| Lint | `ruff check src tests scripts` | All checks passed |
| Types | `mypy src/ohmyllm` | Success: no issues in 64 files |
| Byte-compile | `python -m compileall src scripts tests` | ok |
| Agent e2e | `python scripts/e2e_check.py` | E2E-OK (4 soul entries, 4 audit rows, policy block verified) |
| TUI headless | `python scripts/tui_smoke.py` | TUI-OK (wizard click-through, all screens, palette, chats table, slash commands) |

## What the 114 tests cover (highlights)

- **config**: TOML round-trip, corrupt-file survival, unknown-key retention.
- **safety**: full mode × tool-class decision matrix, unknown-tool deny,
  allowlist extras, denylist, sensitive-path gates in every mode,
  catastrophic regexes, override flag, approval persistence, fail-closed
  timeouts, audit redaction.
- **memory**: append/upsert dedupe/search/forget/backups/revert/changelog,
  secret refusal.
- **sessions**: create/save/load/list/search/pin/tags/rename/trash/restore/
  export, prefix resolution.
- **vram**: nvidia-smi/rocm-smi/sysfs parse fixtures, estimator math, fit
  statuses.
- **backends**: Ollama NDJSON + native tools + pull progress and LM Studio
  SSE tool-call accumulation over `httpx.MockTransport`; mock heuristics.
- **agents**: parser (native/fenced/ReAct/repair), validator coercion,
  runtime integration — memory flow, blocked-outside-workspace, approval
  deny→session-allow, model-cannot-change-mode, malformed→correction, step
  limit, cancellation.
- **bridge (12)**: real HTTP against an ephemeral live server — token
  enforcement (401), static console injection, mode phrase gate, config PATCH
  can never grant full_access, chats CRUD + export, soul write/revert,
  streaming memory turn, approval approve **and** deny round-trips over SSE,
  models/backends/doctor/audit endpoints.
- **CLI**: version, init, models, backend set, run (writes soul + audit),
  chats lifecycle, doctor --json, plain-mode pipe.
- **TUI headless**: chat + memory persistence, approval modal key flow,
  chats table + draft preservation, palette, help.

## Manual / visual verification

- **GUI in browser** (harness Chromium against live bridge): first-run
  wizard end-to-end (hardware detect → mock backend → model list → mode →
  identity seed → soul.md written), streaming chat with tool event lines and
  TURN summary, approval modal with colored diff (approve → file written;
  deny path covered by pytest), permission-mode chip modal with
  `I ACCEPT FULL ACCESS` phrase gate (wrong phrase → error toast + mode
  unchanged; right phrase → full_access chip), models table with VRAM fit
  columns, chats browser cards, soul editor, doctor list, settings form
  (temperature edit → persisted), command palette.
- **Native Tauri window**: `ohmyllm-gui` (release build) launched on the
  real desktop — window titled "OhMyLLM", bridge child spawned, wizard
  rendered with live GPU stats; child killed on exit.
- **CLI**: `ohmyllm run "my name is Finalcheck"` → soul entry + audit row;
  `chats list/export/delete` (prefix ids work); `doctor --json`; piped
  `chat --plain` with `remember:` trigger → memory tool fired and persisted.
- **Installer**: `HOME=/tmp/fakehome bash scripts/install.sh --dir
  /tmp/omh-app` → venv + aliases + doctor 16 pass/2 warn/3 info; PATH guard
  verified (skips symlink clobbering when `ohmyllm` is already on PATH).
- **Packaging**: `uv build --wheel` contains `ohmyllm/bridge.py` +
  `gui_static/{index.html,styles.css,app.js}`.

## Live-backend verification (real models, real GPU)

After the automated gates, the platform was exercised against **real local
servers on this machine**:

- **Ollama 0.40.10** (user-level install) + `qwen2.5:1.5b-instruct-q4_K_M`:
  `ohmyllm models` shows real size/quant/ctx (32768 from `/api/show`) and VRAM
  fit; `ohmyllm run` produced a real native `soul_append` call persisted to
  soul.md; a real `write_file` with a hallucinated absolute path was blocked by
  the sandbox policy; with a workspace-relative path the GUI approval modal
  appeared (diff + risk badge), Allow-once wrote the file to disk, and the
  model summarized the tool result. GUI streamed at ~100–148 tok/s.
- **LM Studio** (server started via `lms server start`): real 33-model library
  with sizes from `lms ls --json`, `tool_use` capability flags, VRAM fits;
  `granite-4.2-3b` completed a real list_dir tool turn with an accurate
  summary of the workspace.
- **Native window**: X11 run screenshot-verified (wizard + console); Wayland
  run stable (process + bridge child).
- **install.ps1**: parsed with real PowerShell 7.4.6 — 0 syntax errors.
- **Interjection/compaction/preview batch**: 161 tests (mid-run interjection
  ordering after tool results, compaction summary+marker+event, live preview
  mount/seal, GUI 202 queue race with delayed mock, tool_progress tail,
  busy-queue TUI); live: intercalated-reply proof, 5 preview growth samples
  on a 20-line write, wizard list uncapped.
- **Native app + file feedback batch**: 88 tests (menu geometry above the
  input, file-meta diff stats, 📄/️ history line, workspace in prompt,
  native-first /gui); live: native window boots backgrounded with optional
  localhost URL printed + `--stop`, approval modal → `📄 created
  gui-proof.txt +1` in history, file content correct, `54 tok · ttft 8.8s ·
  17.5 t/s`.
- **Skills/live-thinking/gui batch**: 81 tests (import parent+single+refresh,
  skills tools, prompt injection, policy STATUS, /gui launch via fake Popen,
  live thinking stream + seal-to-collapsible); live: 36 OpenCode skills
  imported, prompt carries the index, gpt-oss thinking visible mid-stream
  then sealed, `153 tok · ttft 8.8s · 26.4 t/s`, model named three of its
  skills unprompted.
- **Slash/thinking/sandbox batch**: 71 tests (menu pop+complete, collapsible
  mount, reasoning stream+fallback, sandboxed-asks contract incl. system-path
  blocks, quit-from-binding, detached-screen probes); live on the wheel:
  slash menu pops, gpt-oss thinking block collapsed+expandable,
  `ttft 1.4s · 24.5 t/s · 2 steps · 1 tool call`.
- **Metrics/thinking batch**: 130 tests (decode-rate math, forced final
  pass, duplicate-call cache, /think persistence, thinking flags on the
  wire), ruff, mypy, TUI-OK, E2E-OK; live: `ttft 1.6s · 36.2 t/s` (Ollama,
  eval-duration-exact) and `ttft 2.9s · 4.7 t/s` (gpt-oss, decode-only).
- **Tool-history 500 (gpt-oss template)**: unit tests for orphan-tool repair
  and persisted tool_calls; live revival of the user's crashed session
  d5462a37ff56 (7 msgs incl. legacy orphan tool result) → correct reply,
  2 steps, 1 tool call, no engine error. 124 tests total, all gates green,
  wheel reinstalled.
- **LM Studio live cycle**: cold `not-loaded` → `ohmyllm run` auto-loads
  (9 s incl. 11.3 GiB load), gpt-oss reasoning channel surfaces as the
  reply, model stays `loaded` afterwards, second turn 1.9 s. Ollama live:
  clean reply + stats footer. All on the installed wheel.
- **Sweep batch**: SSE error surfacing, chats RowKey mapping, per-turn
  stats (TUI/GUI/CLI), duplicate-render dedupe, hidden tool panel, wizard
  full_access gate, apply_config rebinding, `/backend` enum, auto-model
  pick, CLI approvals + `-y`, bridge 409, help GUI section — 122 tests,
  ruff, mypy, TUI-OK, E2E-OK, wheel reinstalled and re-verified headless
  against the user's real config (wizard completes → chat → chats opens →
  help renders).
- **Wizard model list** (user screenshot: count shown, list empty): same-ID
  `replace()` collision, now populated in place; installed-wheel proof shows
  33/33 LM Studio rows and 1/1 Ollama row with a smart default pick.
- **Wizard worker race** (user-reported `DuplicateIds` crash): reproduced
  deterministically, fixed (awaited removals), and re-verified end-to-end on the
  installed wheel with live Ollama probes and the real `~/.ohmyllm` — full 9-step
  walkthrough reaches chat, mid-step re-renders stable.
- **Global commands (user shell)**: `uv tool install .` → `ohmyllm`, `omllm`,
  `oml`, `OMLLM`, `OML` all resolve and report version in `fish -lc`; real
  `~/.ohmyllm` init → `ohmyllm run` with the real Ollama model wrote soul.md +
  audit.jsonl; `ohmyllm gui --browser` from the installed wheel serves the
  console (title, 49.8 KB app.js, 21.5 KB styles.css, live `/api/status` with
  the real model) — the packaged `gui_static` assets ship correctly.
- Live testing surfaced and fixed two genuine bugs: the GUI dropped the first
  streamed token (renderer state reset), and small models' empty replies now
  trigger one correction nudge instead of a blank answer (2 new regression
  tests).
