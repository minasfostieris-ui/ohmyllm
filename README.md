# OhMyLLM

[![CI](https://github.com/minasfostieris-ui/ohmyllm/actions/workflows/ci.yml/badge.svg)](https://github.com/minasfostieris-ui/ohmyllm/actions/workflows/ci.yml)

**A local-first agentic AI platform for Ollama and LM Studio.** A real tool
loop, a persistent memory you can read as plain Markdown, a permission engine
that never lets the model play god, and three frontends — TUI, CLI, and a
native GUI — all on top of one engine. Nothing phones home.

```
you ──> ohmyllm ──> agent runtime ──> policy engine ──> tools
              │            │                (strict … full_access)
              │            └── soul.md  ◄── persistent memory (plain Markdown)
              ▼
   Ollama / LM Studio / mock   (loopback only)
```

## What it does

- **Agent loop with real tools** — `read_file`, `list_dir`, `search_files`,
  `write_file` (backup + unified-diff preview), `patch_file` (unique-match
  edits), `run_command` (timeout + risk scoring), `http_fetch` (off by
  default, allowlist-gated), `soul_read/append/upsert/search/forget`,
  `get_system_status`. Tool calls work natively on tool-capable models, via
  fenced JSON on medium ones, and via ReAct as last resort — with automatic
  argument repair for small models.
- **Permission modes** — `strict`, `safe`, `sandboxed`, `dangerous`,
  `full_access`. The model can never change the mode; `full_access` requires
  typing `I ACCEPT FULL ACCESS`; catastrophic commands (`rm -rf /`, `mkfs`,
  `curl | sh`, …) stay blocked even then unless you flip a config flag.
  Every decision is audited to `~/.ohmyllm/logs/audit.jsonl` with secret
  redaction. → [docs/PERMISSIONS.md](docs/PERMISSIONS.md)
- **soul.md memory** — the agent's persistent identity/knowledge file. It is
  plain Markdown, yours to edit; automatic backups, changelog, revert,
  similarity-dedup upserts, search/forget. → [docs/MEMORY.md](docs/MEMORY.md)
- **Full chat browser** — every session persisted (JSON, atomic writes),
  searchable by title *and* message body, pin, tag, rename, delete→trash
  (with export offer), restore, Markdown/JSON export.
- **VRAM intelligence** — detects NVIDIA/AMD/integrated GPUs (nvidia-smi,
  rocm-smi, sysfs, sysctl, WMI) and tells you per model: minimum and
  comfortable VRAM, whether it fits your card, and why a model is tool-weak.
- **Backends** — Ollama (NDJSON streaming, native tools, auto-`serve`, model
  pull with live progress) and LM Studio (OpenAI-compatible + native API,
  SSE tool-call accumulation, launch helpers). Plus a **mock backend** — a
  scripted but fully real agent for demos/tests without any model.
  → [docs/BACKENDS.md](docs/BACKENDS.md)
- **Three frontends** — Textual TUI (the flagship), `--plain` Rich chat, and
  a native GUI (`ohmyllm gui`, Tauri window or browser) — same engine, same
  policy gates.

## Install

```bash
# from the repo (any Python ≥3.11)
bash scripts/install.sh                  # or: uv tool install . / pipx install .
ohmyllm --version          # aliases: omllm, oml, OMLLM, OML (all equivalent)
```

Windows: `powershell -File scripts\install.ps1`. Manual:
`python -m venv .venv && .venv/bin/pip install -e .`.

## Quickstart

```bash
ohmyllm                 # TUI — first run opens the setup wizard
ohmyllm chat --plain    # no-frills chat in any terminal
ohmyllm run "list files here and summarise"   # one-shot agent task
ohmyllm models          # list models with VRAM fit
ohmyllm backend ollama --start
ohmyllm doctor          # 25+ health checks with fixes
ohmyllm gui             # native window (or browser console)
```

Inside the TUI: `Ctrl+K` command palette, `Ctrl+O` chats, `Ctrl+M` models,
`Ctrl+B` backend, `Ctrl+P` permission mode, `Ctrl+S` memory, `Ctrl+D`
doctor, `Ctrl+G` settings, `Ctrl+N` new chat, `Ctrl+X` stop, `Ctrl+H` help,
`Ctrl+Q` quit. `/remember <fact>` writes straight into soul.md; `/forget`,
`/rename`, `/export`, `/mode`, `/model` do what they say. `/think off|low|medium|high|xhigh|max` sets the reasoning
effort ladder — mapped to `reasoning_effort` on LM Studio/vLLM and graded
`think` levels on Ollama, and **capability-gated**: models without a thinking
mode never see the knob (the slash menu tells you when your model can't
think). `/ultrathink` rides the ceiling AND adds a **critique-and-revise
pass**: the model attacks its own draft for errors and edge cases, re-derives
everything, and rewrites. Measured on a local Qwen3.5-9B: 4.6x the reasoning
(1435 -> 6596 chars, 9s -> 58s thinking) with a visibly better answer — and
it beats `/think max` on every model because the review pass is pure
prompting, not an API knob.

**`/market` — the model marketplace.** Twenty-four curated downloads in
three sections: 9 language models (Qwen3.8-27B in two honest quants,
Sushi-Coder-RL, official Gemma-4 QAT, GPT-OSS-20B, Qwen3-Coder-30B,
R1-Distill-14B, Phi-4), 4 **speech-to-text** models and 11 **Phonon TTS**
voices — real sizes, a **"will it fit my GPU" verdict** from a live VRAM
probe (NVIDIA, AMD, Apple Silicon unified memory) for LLMs, MB sizes for
voice. Enter or click starts the download — LLMs straight into your LM
Studio models folder, voice models into `~/.ohmyllm/voice/`; progress bar,
MB/s, ETA, cancel; the same shelf appears as a **Market tab in the GUI**.
NInfer builds are auto-selected only when a NInfer runtime is installed.

**🔑 Providers — where API keys go.** Cloud models are first-class: type
`/providers` (TUI) or open the **Providers** tab (GUI) — a connect screen
like OpenCode's, grouped Popular / Other / Custom. Pick OpenRouter,
Anthropic, OpenAI, Google Gemini, DeepSeek, OpenCode Zen, Groq, Mistral,
xAI, Together, Fireworks, Cerebras, Vercel AI Gateway, 302.AI… paste the
key, and it becomes the default backend (`u` / click). Keys are stored in
`~/.ohmyllm/config.toml` (chmod 600, owner-only) and displayed masked
everywhere; `OMLLM_KEY_OPENROUTER` / `OPENAI_API_KEY` / the vendor's own
env var also work and win over the file — so secrets managers can inject
them. Custom OpenAI-compatible endpoints (home vLLM, a LAN box) connect the
same way. Anthropic speaks its own wire format; OhMyLLM translates,
including native tool calls and thinking blocks.

**🎙 Voice I/O.** **Hold Ctrl+E** in the TUI and talk — release to stop;
the transcript lands in your input box, ready to edit before sending. OhMyLLM routes to whichever engine + model you actually have: **SenseVoice-Small** (English·한국어·中文·日本語·Cantonese, 254 MB — the multilingual default), **Parakeet TDT 0.6B v3** (best English accuracy), or **Whisper** multilingual ggmls (~99 other languages — add WhisperX for VAD on long recordings). `/tts on` makes replies spoken through the gradium **Phonon** family (en/es/fr/de/pt + multilingual, 22–85 MB); `/say <text>` speaks once; `/voice` shows what OhMyLLM can see on your system. STT runs through whisper.cpp / WhisperX / sensevoice.cpp / parakeet.cpp and TTS through audio.cpp — install any of them and OhMyLLM finds the binary on PATH. Nothing is claimed as available unless both the runtime and the model exist — and `/onboarding` walks you through the whole choice.

**Ctrl+C copies your selection** (it never quits); quitting is Ctrl+Q with
a confirm. Ctrl+V pastes clipboard images (see Vision paste below).

The **model selector** (`/models`, `Ctrl+M`) sorts by size, params, quant,
family, thinking, or vision (`o` in the TUI, a dropdown in the GUI) and
shows what each model can do: 👁 vision comes from scanning your disk for
`mmproj` sidecars — a model file with no sidecar is a definitive *no*, not
a shrug — and thinking support (`⚡ levels / ◐ on-off / ★ forced / ✗ none`)
from the server's chat template where it exists, else the model's base
family with all the `-uncensored-heretic-abliterated` noise stripped.

**K/V cache quant** (`q4_1` by default) shrinks the attention cache so long
conversations fit your card — exposed in Settings, the per-model modal, and
`[generation] kv_cache`; Ollama gets its boolean equivalent.

**Vision paste.** `Ctrl+V` an image from the clipboard: it shows up in the
chat as a 📷 card (real preview when the terminal supports it), and if the
selected model has vision — detected by scanning your disk for `mmproj`
companion files, since the server APIs don't report it — the image rides
along as a proper multimodal message. Non-vision models get plain text and
you get told why; a server that rejects the image gets an automatic
text-only resend.

## GUI

`ohmyllm gui` boots a **Tauri** window when the shell is built
(`gui/src-tauri`, one `cargo build --release`) and otherwise serves the exact
same console to your browser. Both talk to a loopback-only bridge
(`ohmyllm.bridge`) that binds `127.0.0.1`, picks an ephemeral port, and
requires a per-process token — the GUI shares the TUI's policy engine,
approvals (with diff previews), audit trail, and sessions store. Chat
streaming, first-run wizard, model table with VRAM fit, backend cards,
permission modes with the phrase gate, soul editor, doctor, command palette:
all in there.

```bash
ohmyllm gui --browser            # force browser mode
ohmyllm gui --no-open            # just print the URL
# build the native window (needs Rust + webkit2gtk-4.1 / Tauri prereqs):
cd gui/src-tauri && cargo build --release
```

## Configuration

`~/.ohmyllm/config.toml` (per-project overrides live in `.ohmyllm/`).
Everything is editable from the TUI/GUI settings screens; hand-editing is
safe — unknown keys are preserved, corrupt files are quarantined and backed
up. Highlights:

```toml
default_backend = "ollama"
permission_mode = "safe"

[generation]
temperature = 0.7
num_ctx = 4096
max_tokens = 2048

[agent]
max_steps = 8
native_tools = "auto"     # auto | force | off

[memory]
agent_writes = true

[safety]
http_fetch_enabled = false
workspace_write_requires_confirmation = true

[guard]                          # optional Clef-Flash danger flagger
enabled = false                  # for dangerous/full_access modes
base_url = "http://127.0.0.1:9931"  # llama-server -ngl 0 (CPU+RAM only)
model = "clef-flash"
danger_threshold = 0.55
```

## Development

```bash
uv sync --extra dev            # or pip install -e ".[dev]"
pytest tests/ -q               # 114 tests, no network, no model required
ruff check src tests scripts
mypy src/ohmyllm
python scripts/e2e_check.py    # scripted agent: memory + tools + audit
python scripts/tui_smoke.py    # headless TUI click-through
```

## Privacy

OhMyLLM only ever talks to `127.0.0.1` (model servers, GUI bridge). There is
no telemetry, no update pings, no cloud dependency. The `http_fetch` tool is
disabled by default; enabling it still requires a host allowlist and, in
every mode below `full_access`, your approval.

## Troubleshooting

See [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — backend won't
start, models too big for VRAM, approvals not appearing, GUI port/token
issues, and where the logs live (`~/.ohmyllm/logs/`).

## Feedback & feature requests

The fastest way to reach me: **Discord — `gonber0606`**. Bug reports and
feature requests are welcome there; GitHub issues work too but I see them
later.

## Disclaimer

OhMyLLM is provided **as is, without warranty of any kind**, under the MIT
license. In particular:

- **AI output can be wrong, harmful, or unsafe to act on.** You are
  responsible for what you let the agent do with it.
- **The permission engine is a guardrail, not a security boundary.** Mode
  checks, risk scoring, and command allowlists are best-effort heuristics
  (regex + path analysis). `dangerous` and `full_access` let the model run
  real commands on your real machine — that is the point, and that is the
  risk. Do not run them on machines or accounts you cannot afford to lose,
  and do not feed untrusted content (web pages, files from strangers) to an
  agent that has execution rights.
- **Third-party models are your responsibility.** OhMyLLM ships no models.
  Whatever you download from Ollama, LM Studio, or Hugging Face carries its
  own license and behavior; you must comply with it yourself.
- **No affiliation.** OhMyLLM is an independent hobby project. It is not
  affiliated with, endorsed by, or sponsored by Ollama, LM Studio,
  Cloudflare, ggml-org, or any model provider. All trademarks and names are
  the property of their respective owners, used here only to describe
  compatibility.

By using this software you accept these terms; by redistributing it you keep
this notice and the MIT license intact.
