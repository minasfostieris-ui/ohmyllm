# Patch notes

Format: newest first. Every push appends here.

## Unreleased

### Added
- **Email** — `oml mail setup` connects an IMAP/SMTP account (Gmail/Outlook/Fastmail/iCloud auto-detected, app passwords, `OMLLM_KEY_MAIL` env override, login verified before saving). Four new agent tools: `email_search` (from:/to:/subject:/recent:N queries), `email_read` (MIME→text, quote-stripped, fenced as UNTRUSTED — instructions inside inbound mail are never followed), `email_draft` (RFC2822 `.eml` to `~/.ohmyllm/mail/outbox/` + IMAP Drafts append; works with zero setup), `email_send` (SMTP, proper In-Reply-To/References threading). Gating: send is approval-required in sandboxed/dangerous; silent only in full_access with `[mail] enabled=true`; an optional `recipient_allowlist` is enforced even in full_access and inside the tool itself. `/mail` status command in TUI + GUI; Settings screen and GUI config PATCH can never wipe or echo back the mail password.
- **Market** — MiniCPM-V-4.7-35B-A3B · IQ3_XS (14.5 GB, OpenBMB multimodal MoE, vision companion auto-fetched). New `hybrid` flag: MoE models designed for CPU+GPU offload report amber "hybrid CPU+GPU on N GB" instead of a false red "won't fit" on smaller cards.
- **GUI hold-to-talk** — Ctrl+E now records SERVER-side (pw-record/arecord via the bridge, same pipeline as the TUI): works in the Tauri window where browser getUserMedia is blocked, and ignores browser permission state entirely.
- **Provider brand logos** in the GUI providers list (simple-icons, CC0, vendored as inline SVG; near-black brand marks auto-whitened).
- **`/tone`** — how the AI talks: `ruthless` (brutally honest, no sugarcoating), `chill`, `friendly`, `professional`, `terse` (maximum signal), or `default`. Set it with `/tone <name>` in the TUI, plain CLI or GUI, or the Tone field on the Settings → Agent tab / the `agent.tone` dropdown; persists to config and applies from the next turn. Invalid names never stick.
- **`/agentloop [on|off]`** — relentless verified-completion mode with a one-shot completion audit per turn; now also available in plain mode.
- **Agentloop VERDICT GATE + ECHO BREAKER** — field-tested three times on LM Studio `gemma-4-e4b-uncensored` (Ember & Oak website task): the loop wrote all three files but stopped early and, post-audit, degenerated into a 30+ minute echo loop regurgitating its evidence ledger — every step one copy starting at a random offset of the previous, truncated by max_tokens, so neither identical prefixes nor within-copy duplication catch it. Now: a looped turn may only finish when its answer cites a tool it actually ran this turn (uncited "all done" answers bounce back up to twice); the breaker flags cross-step OVERLAP (this reply's opening already inside the previous reply), exact repeats, and self-duplicated replies — two detections end the turn, keeping the ledger as the final answer; audit/gate/step-limit prompts now demand the ledger ONCE and the forced final pass is hard-capped at 1200 tokens so a wedged model cannot stream forever. TUI and plain mode surface gate/break as status lines. Rotating-, growing- and identical-ledger regressions are covered by tests replaying the exact field shapes.

### Fixed
- **Starting `oml` never launches LM Studio or Ollama behind your back.** `auto_start` now defaults to `false` for both local backends; boot probes are read-only and tell you what to start manually. Enable `[lmstudio] auto_start = true` / `[ollama] auto_start = true` in config.toml to restore spawning — the Backend screen, wizard and `oml run` still start servers on explicit demand regardless.
- **Plain-mode approvals were deaf.** Rich's streaming `Live` captures the terminal input while it runs — approval answers typed underneath it vanished and the tool died as "denied". The Live display now pauses for the prompt. `[a]lways this session` is TOOL-level (a loop writing five files prompts once, not five times); permanent rules keep exact-argument matching. Approval parsing matches the labels it prints (`a`=always, `p`=permanent, bare Enter=yes), and the prompt no longer advertises `l`/`o` keys that did nothing.
- **A wedged local server could hang oml forever.** Both stream_chat clients ran with `read=None` — if LM Studio/Ollama accepted the request and emitted zero SSE lines, the turn waited unboundedly. The gap between stream chunks is now capped (≥5 min per gap, never killed mid-token), surfacing a clean backend error instead.
- Cross-process config sync both ways: the GUI bridge reloads `config.toml` (mtime watch) before serving status/providers/config; the TUI reloads on provider/model refresh.
- Cloud context windows: provider `/models` metadata, `-128k/-1m` name suffixes and `id:256k` static-list declarations now drive the ctx% bar, auto-compaction and history trim (previously pinned to the local `num_ctx`, so a 256k cloud model compacted at ~3k tokens).
- Parakeet market entry switched to ggml-org `ggml-*.bin` (the installed whisper-cpp CLI cannot read handy-computer GGUFs); family-aware STT file matching.
- TUI input: long single-line text no longer replaced by a horizontal scrollbar; send button centered; market download progress docked and cancellable with esc.

## v0.1.0

Local-first agentic AI platform: TUI + GUI + bridge, permission engine, optional Clef-Flash danger guard.
