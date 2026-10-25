# OhMyLLM — Progress Log

Living log; newest phase at top. See PLAN.md for the checklist (all green)
and TEST_RESULTS.md for the verification record.

## P9 — Interjections, compaction, live file preview, delete/quit fixes, GUI polish (COMPLETE)

- **Delete/rename/revert/perms silent-death root cause**: modal-await flows
  scheduled with `call_next(coro)` were GC'd while parked on the confirm
  future — the task was destroyed pending and nothing ran. All such flows
  (chats delete/rename, memory revert/clear, perms apply, chat `/delete`,
  safe-quit, mode apply) now use `run_worker(coro(), exclusive=False)`,
  which retains the task. Verified end-to-end: delete completes in ~3.6 s,
  clean exit, no "Task was destroyed" noise. Regression test
  `test_delete_chat_completes`.
- **Quit modal**: `_quit_prompt_open` guard stops stacked Ctrl+C modals (the
  real "Cancel does nothing" bug); `ConfirmModal` accepts y/n/Esc only —
  Enter removed (focused-button + global yes was ambiguous). Test
  `test_quit_modal_single_instance_and_cancel`.
- **Interjections**: type while the agent is busy → queued (`⏳ queued`
  sysline in TUI, 202 `{queued:true}` toast in GUI). `run_turn` drains the
  queue at every step boundary as a `user` message + `interjection` event;
  leftovers auto-send as the next turn. Live proof: model quoted the
  interjected word mid-task.
- **Context compaction**: `runtime._maybe_compact` — when estimated tokens
  cross `compact_ratio × num_ctx` (and ≥14 messages), the head is summarized
  by the model itself and replaced with one `system` (`compacted`) message;
  last 8 kept verbatim. `compaction` event → TUI sysline + panel line + GUI
  event line. Config: `agent.compact`, `agent.compact_ratio=0.75`.
- **Live file-write preview**: lmstudio streams partial `write_file` args
  every 64 chars → `tool_progress` → `tool_stream` event → TUI `msg-file-live`
  Static (last 14 lines, 0.1 s throttle, sealed on tool_result) + panel
  `✍️ name Nk chars` + GUI `<details>` progress. Diff stats on completion:
  `ToolResult.meta` (after `error`, positional-safe) carries
  `{path, created, added, removed, changed}` via `file_tools.line_stats`
  (difflib) → `📄 created f.txt +N` / `✏️ edited f.txt -N ~N +N` lines in
  TUI and GUI.
- **t/s honesty**: `_finish` takes `reasoning_chars`; estimate
  `max(tokens, (text+reasoning)//4)`; ollama path uses
  `max(eval_count, est)`; stats show tok even without eval_count. gpt-oss
  went 2.3 → ~25 t/s (reasoning tokens were the missing numerator).
- **Native app**: `ohmyllm-gui` (Tauri, built) prints
  `OhMyLLM console → http://127.0.0.1:PORT/?t=TOKEN` on boot (stdout
  inherited). `oml gui` launches native + optional web bridge, prints the
  URL, writes `gui-web.pid`/`gui-web.url`; `oml gui --stop` kills it. TUI
  `/gui` prefers native, also boots the web console and notifies the URL.
- **Workspace awareness**: system prompt now states the workspace root and
  that relative paths resolve inside it (kills bogus "outside my workspace"
  refusals for sandboxed mode).
- **GUI fixes** (all verified in headless Chromium screenshots): dark
  `color-scheme` + custom select/checkbox/number styling (the OS-white
  dropdowns were the "what is this" complaint); chat-card actions always
  visible with text labels `◇ pin / ↧ export /  rename / ✕ delete`
  (hover-only mystery icons before); wizard lists **all** models (was
  capped at 8); **wizard completion now persists server-side** —
  `PATCH /api/config {first_run_completed:true}` (POST had no route; a
  fresh browser profile re-showed it every launch); `Save Ctrl+Enter` kbd
  glyph fixed; models warnings clamped to one line; textarea resize.
- **TUI**: slash-menu-behind-input fixed (`#chat-root { height: 1fr }`);
  tool-panel border dimmed (the blue artifact); history scrollbar hidden.
- **The artifact, finally root-caused**: the bright cyan vertical bar was
  Textual's scrollbar thumb — default `scrollbar-color: $scrollbar` =
  **ANSI blue**, which the user's Orca terminal palette renders as
  `#63d0df` cyan. The earlier `ScrollBar { color: … }` rule styled the
  widget's *text* color, not the thumb, so nothing changed. The dashed
  blue line was the 1-col history scrollbar thumb beside the panel border;
  the black block on the chats table was the scrollbar corner
  (`scrollbar-corner-color: ansi_default` → black). Fix: scrollbar-size
  0 0 on Screen + every scrollable widget type (VerticalScroll, DataTable,
  OptionList, TextArea, ScrollView, Log) — no bar is ever rendered,
  scrolling is untouched (cursor keys, j/k, wheel, Home/End verified);
  the chat/panel separator is a dedicated `#panel-sep` Static (2 cells,
  `background: #00ffff`) toggled in lockstep with the panel via
  `ToolPanel._set_visible` — a *thick, unmistakable cyan bar*. A border
  could never do this: Textual's `thick` style paints `█` with the
  widget's foreground, so border-color is ignored. NOTE for future
  verification: this session's shell exports `NO_COLOR=1`, which forces
  Textual monochrome and makes every truecolor render grey in
  `export_screenshot` — always run color checks with `env -u NO_COLOR`;
  thinking panel lines use `dim` instead of ANSI magenta.

## P8 — Tauri GUI + bridge (COMPLETE)

- `src/ohmyllm/bridge.py`: stdlib `ThreadingHTTPServer` on 127.0.0.1,
  ephemeral port, per-process token; REST (status/config/backends/models/
  soul/chats CRUD+export/doctor/audit/mode) + SSE chat streaming with
  out-of-band approval POSTs. Reuses `Services`/`AgentRuntime`/`PolicyEngine`
  unchanged — the GUI cannot bypass a single gate.
- `src/ohmyllm/gui_static/`: hand-written console (no frameworks, no CDN):
  first-run wizard, streaming chat with tool event lines + diff-bearing
  approval modal, chats browser, models table with VRAM fit, backend cards,
  soul editor with backups, doctor, settings (real config schema), command
  palette, mode phrase-gate UI. Dark precision-instrument theme; responsive;
  keyboard-complete; reduced-motion aware.
- `gui/src-tauri/`: Rust shell spawns the bridge child (auto-detects
  interpreter via `OMLLM_PYTHON`/sibling venv), waits on the port/token file,
  navigates the webview to the served console, kills the child on exit.
  `cargo build --release` verified on this host (webkit2gtk-4.1).
- `ohmyllm gui` CLI: native window when built, browser console otherwise
  (`--browser`, `--no-open`, `--port`).
- Verified end-to-end in a real browser against a live bridge (wizard →
  memory turn → approval → write → phrase gate → all screens) **and** as a
  real native window on the desktop (screenshot-verified).
- Packaging: `gui_static/*` ships inside the wheel (`[tool.setuptools
  .package-data]`), verified via `uv build`.

## P0–P7 — Core platform (COMPLETE)

- Core: paths, config (pydantic + hand-rolled TOML, corrupt-safe), safety
  (5-mode policy matrix, risk scorer, fail-closed approvals, redacted JSONL
  audit), soul.md memory (sections, dedupe upsert, search/forget, backups,
  changelog, secret refusal), sessions store (atomic JSON, trash/restore,
  pin/tags/search/export), VRAM detect+estimator, Ollama/LM Studio/mock
  backends, agent runtime (native/fenced-JSON/ReAct parsing, JSON repair,
  validation, correction retries, cancellation, timeouts), 13 tools, doctor
  (~25 checks), self-install, plain Rich chat, full typer CLI.
- TUI: 10 screens incl. 9-step wizard, approval modal with diffs, command
  palette, chats browser, models with VRAM columns, backend cards, permission
  phrase gate, soul editor, settings, doctor, help.
- Installers: `scripts/install.sh` (uv-or-venv, aliases, PATH guidance,
  fake-HOME e2e verified), `scripts/install.ps1`.
- Test suite grew to 114 tests incl. 12 bridge integration tests driving the
  real HTTP+SSE surface (memory flow, approve/deny round-trips, CRUD,
  phrase gate, token enforcement).

## Real-backend verification (live hardware)

- **Ollama 0.40.10** installed user-level (`~/.local/share/ollama-bin`), serving on
  127.0.0.1:11434 with `qwen2.5:1.5b-instruct-q4_K_M` pulled: real NDJSON streaming
  at ~100–148 tok/s on the host's AMD GPU, real native tool calls (soul_append →
  soul.md; write_file → approval modal → file on disk), and a real
  out-of-workspace path (`/home/ohmyllm/…`) correctly blocked by the sandbox
  policy, with the model visibly adapting to the denial.
- **LM Studio** server started via its `lms` CLI; the real 33-model library listed
  with on-disk sizes (via `lms ls --json`), quantizations, `tool_use` capability
  flags and VRAM fits; `granite-4.2-3b` ran a real tool turn (list_dir → summary).
- **Native window** verified on both display backends: X11 (screenshot-confirmed
  content) and Wayland (process + bridge child stable).
- **install.ps1** parse-validated with real PowerShell 7.4.6: 0 syntax errors.
- Found & fixed during live testing: the GUI renderer dropped the first streamed
  token (`ensureLive` reset `liveText` after the increment); the runtime now
  nudges models once when they answer with silence (2 regression tests).

## Global command install (user-reported: `oml`/`ohmyllm` unknown in fish)

- Root cause: everything had only ever been installed into the repo venv and a
  fake-HOME sandbox; the user's real shell never got the commands. Fixed with
  `uv tool install .` → `~/.local/bin/{ohmyllm,omllm,oml,OMLLM,OML}` (already
  on the user's fish PATH), verified live with `fish -lc`.
- `oml`/`OML` added as first-class aliases everywhere: pyproject scripts,
  install.sh, install.ps1 (re-parsed: 0 errors), selfinstall, docs.
- **DuplicateIds wizard crash (user-reported)**: the `_detect` worker re-rendered
  step 1 via `remove_children()` (async in Textual) + immediate same-ID remount →
  crash whenever a real backend probe finished while the user sat on step 1.
  Reproduced deterministically, fixed by awaiting removals (`_show_step`,
  `_advance`, `_back`, `on_mount`, `_detect`, `_load_models` replace), pinned with
  a pytest regression + a permanent hammer in `tui_smoke`, re-verified against the
  **installed wheel** with the live Ollama server and the user's real `~/.ohmyllm`.
  Lesson recorded: headless tests must run with realistic worker latency, not
  instant-fail fake probes.
- **Interjections, compaction, live file writing, GUI wizard cap** (user
  screenshots): messages sent while the agent runs are QUEUED and injected
  into the session before the next model call — the agent sees the tool
  results AND the new instruction together (live proof: model replied
  "Intercalated within the text is the word intercalated"). The GUI's
  /api/message returns 202-queued while busy instead of refusing. Context
  compaction: when estimated tokens pass 75% of num_ctx, old turns are
  summarized by the model itself into one `compacted` system message
  (agent.compact / agent.compact_ratio; 🗜 panel line + history note).
  Watching writes: LM Studio tool-argument streaming now emits
  `tool_stream{name,chars,tail}` every 150 chars and the chat shows a live
  `📝 writing <file>` preview block that grows with the content and is
  replaced by the diff line on completion (verified: 5 distinct preview
  states during a 20-line write). The GUI wizard's `.slice(0, 8)` cap on
  models is gone — all 33 render in the scrollable list. The native app
  prints its loopback URL to the terminal on boot (rebuilt binary).
  Scrollbars: muted to grey, horizontal overflow killed — the blue
  artifacts are gone. Mock backend gained a `delay` knob for realistic
  latency tests.
- **Native app + real-time file feedback + workspace awareness** (user
  screenshots): built the Tauri window (`ohmyllm-gui`, installed to
  ~/.local/bin). `oml gui` now boots the APP in the background and prints an
  OPTIONAL localhost web console URL in the CLI (`oml gui --stop` closes it)
  — no more blocking foreground, no more silent browser fallback. `/gui` in
  the TUI prefers the native app too. File edits now appear in the chat as
  they happen: `📄 created x +N` / `✏️ edited x -N ~N +N` (red/yellow/green,
  computed with difflib in ToolResult.meta), and LM Studio tool-argument
  streaming shows a live `✍️ name … Nk chars` progress line. The system
  prompt now states the workspace root and that everything under it is the
  agent's to write — the "I can't write to ~/Downloads" confusion is gone
  (sandboxed asks now, anyway). Slash-menu layout fixed: `#chat-root` was
  100% tall and shoved the menu BEHIND the docked input; it's 1fr now and
  the menu renders above the input (geometry-pinned test). Scrollbars
  restyled to a subtle 1-cell accent.
- **Skills subsystem + live thinking + /gui** (user requests):
  `oml skills import <folder>` ingests OpenCode/Claude-style SKILL.md
  folders into `~/.ohmyllm/skills/` (single skill or a parent of many;
  re-import refreshes). The agent gets a `## Skills` index in its system
  prompt plus always-allowed `skills_list`/`skills_read` tools — it reads a
  skill before working in its domain. All 36 of the user's OpenCode skills
  ported. Thinking now streams LIVE: a dim 💭 block grows under the input
  while the model reasons, then seals into a collapsed, expandable
  Collapsible (TUI) / `<details>` (GUI) the moment the answer starts.
  `/gui` (alias `/omlgui`) launches the console bridge from inside the TUI
  (detached process, pid-tracked, re-open on second call). Token math:
  reasoning chars always count toward the rate AND the `N tok` figure
  (est = max(reported, (answer+reasoning)/4)) — live gpt-oss: 153 tok,
  26.4 t/s.
- **Batch: slash menu, thinking blocks, sandboxed asks, honest rates**
  (user screenshots + requests):
  - Typing `/` pops a command menu above the input: filtered options with
    descriptions, ↑/↓/Tab navigate, Enter completes (doesn't send).
  - 💭 expandable thinking: reasoning streams as `thinking_text` events,
    collected and mounted as a collapsed `Collapsible("💭 thinking process")`
    under every reply (TUI) and a `<details>` block (GUI). A thinking-only
    model response is now used as the answer instead of the empty-reply nudge.
  - Sandboxed policy: outside-workspace reads/writes/redirects now ASK
    (approval modal) instead of hard-blocking — the `~/Downloads` incident
    was cwd-as-workspace + block-instead-of-ask. System paths (/etc, /usr,
    Windows dirs) and sensitive paths (keys, browsers, history) stay
    blocked in every mode; full_access asks before system writes.
  - t/s was still false because gpt-oss spends most decode time in the
    reasoning channel, which wasn't counted: est tokens now include
    reasoning chars → live proof 2.3 → **24.5 t/s** on the same model.
  - Quit crash (`NoActiveWorker`): `prompt`/`confirm` now use
    callback+future instead of worker-bound `push_screen_wait`;
    ConfirmModal gained y/n/enter/esc keys (it was mouse-only).
- **Switch-screens-mid-turn crash** (user traceback `NoMatches: #chat-title`):
  Textual keeps a replaced screen `is_mounted=True` while deleting its
  children — so a turn finishing after the user switched chats ran the
  worker's post-turn UI calls against a widget-less screen and crashed the
  app. Every post-await UI path in chat/chats/models now probes with
  `try: query_one(...) except NoMatches: return` (is_mounted is a lie; the
  probe is the truth). Regression test pins the exact detached condition.
- **Honest turn metrics + `/think`** (user report: t/s counted idle time):
  decode rate is now measured from first token to last (load/queue/prefill
  excluded) or straight from Ollama's `eval_duration` when available — live
  proof: Ollama `ttft 1.6s · 36.2 t/s`, LM Studio `ttft 2.9s · 4.7 t/s`
  (was 0.7 with idle counted). TTFT appears in the TUI stats line, the GUI
  turn line and `run` output. `/think [on|off]` (+ Settings switch) enables
  the reasoning channel (`thinking: true` for Ollama, `reasoning_effort`
  for LM Studio); reasoning deltas show as 💭 lines in the activity panel
  and still fall back to the answer when a model puts everything there.
- **Silent step-limit deaths**: turns that exhausted max_steps in tool
  loops ended with no text at all. The runtime now forces one tool-free
  summarizing pass and the TUI surfaces a red hint if even that is empty.
  Token efficiency: identical tool calls within a turn are answered from a
  cache with a "stop repeating" note, tool output caps dropped 8000→3000,
  and the system prompt now forbids filesystem-hunting for facts already
  in the injected memory block (the 8× search_files loop from the user's
  screenshot: same prompt answered in 3 calls).
- **gpt-oss 500 on tool history** (user screenshot "Great, it died"): strict
  chat templates (gpt-oss) refuse `tool` messages whose preceding assistant
  turn doesn't declare matching `tool_calls` — the engine raised inside the
  template (`tool_call_id is none`). Native tool calls are now persisted on
  the assistant message and replayed; `_outgoing` also repairs legacy
  sessions by synthesizing the missing anchor and drops empty assistant
  turns. Revived the user's exact crashed session live: reply + stats
  footer + a fresh tool call, all through the repaired history.
- **LM Studio "unloading after every prompt" + "empty model reply"** (user
  screenshots): three stacked causes, all fixed. (1) LM Studio refused
  requests to unloaded models with an SSE `event: error` that the parser
  silently swallowed → every turn looked like an empty reply; errors now
  surface. (2) The backend now calls `POST /api/v1/models/load` (with an
  `lms load` fallback) before chatting, and sends `"unload_model": false`
  so the model stays resident — second prompt lands in ~2 s instead of
  re-loading. (3) gpt-oss streams answers into `delta.reasoning`; that
  channel now becomes the reply when `content` is empty (Ollama `thinking`
  handled the same way). Verified live: cold gpt-oss-20b → real reply →
  state stays `loaded`.
- **Chats screen dead rows** (user screenshot): `RowKey` objects were keyed
  against integer cursor rows, so selection always resolved to None — the
  preview claimed "no chats" while rows sat right there, and Enter/clicks
  did nothing. Index-based mapping + `RowSelected` handler: click or Enter
  opens the chat. Phantom 18-row gap between filter and table fixed via
  explicit `#chats-filters` height.
- **Per-turn stats footer**: every assistant reply now ends with
  `· model · N tok · T t/s · S steps · K tool calls` in the TUI, the GUI
  turn line, and `ohmyllm run` output (fields shown when the backend
  reports them). Empty replies additionally surface a red hint line.
- **Reply rendered twice** (latent TUI bug): `turn_end` finalized the stream
  and `_run_turn` finalized again with the full text — every successful
  reply duplicated once content arrived. Pinned by a dedupe test.
- **Tool panel width hog**: the right-hand activity column claimed 42
  columns on every chat screen even with nothing to show; it now stays
  hidden until the first tool/memory/retry event.
- **Wizard could grant full_access** (security): the mode step listed
  full_access despite its own "not selectable" note, and finish wrote it
  straight to config, bypassing the confirmation phrase. Removed from the
  list; the resolver clamps; regression test added.
- **Settings swap left stale objects**: saving settings replaced
  `svc.config` but cached backends kept the old config (host/binary edits
  silently ignored). New `Services.apply_config()` drops the cache; both
  the TUI settings screen and the GUI bridge use it.
- **`/backend ollama` crash** (TUI slash): assigned a raw string to an
  enum field → next title refresh hit `AttributeError: value`. Enum-ized.
- **No default model = cryptic failure**: with `default_model` unset,
  requests went out with an empty model name. The runtime now auto-picks
  the best installed model (tool-capable first) and caches the pick.
- **CLI one-shot run was chat-only in practice**: every approval failed
  closed (`prompt_fn=None`). `ohmyllm run` now prompts y/N per tool call
  (or `-y` to auto-approve within policy) and prints the stats footer.
- **Bridge concurrency**: a second browser tab could start a parallel turn
  on the same session; `/api/message` now answers 409 while busy.
- **Help screen**: GUI console boot instructions (`oml gui`, `--browser`,
  `--no-open`, token note) added to the TUI help; Markdown spacing fixed.
- **Empty wizard model list (user screenshot)**: the same ID-collision family —
  `_load_models` swapped in a new `OptionList` with the same ID via `replace()`;
  the collision was swallowed by its `except`, so the step rendered "33 model(s)
  found" above an empty list. Fixed by populating the existing list in place
  (`clear_options` + `add_option`), pinned by a test asserting `option_count > 0`
  after the worker, and re-verified on the installed wheel against the user's
  live LM Studio (33/33 rows) and Ollama. Wizard default model now prefers the
  configured default, else the first tool-capable model.
- Real home initialized (`~/.ohmyllm`): config → ollama default with the
  user-level binary path + `qwen2.5:1.5b-instruct-q4_K_M`; `ohmyllm run` with
  the real model wrote soul.md and the audit log; `ohmyllm gui` serves the
  full console from the installed wheel (verified via HTTP).
