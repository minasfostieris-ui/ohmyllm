# Patch notes

Format: newest first. Every push appends here.

## Unreleased

### Added
- **Email** — `oml mail setup` connects an IMAP/SMTP account (Gmail/Outlook/Fastmail/iCloud auto-detected, app passwords, `OMLLM_KEY_MAIL` env override, login verified before saving). Four new agent tools: `email_search` (from:/to:/subject:/recent:N queries), `email_read` (MIME→text, quote-stripped, fenced as UNTRUSTED — instructions inside inbound mail are never followed), `email_draft` (RFC2822 `.eml` to `~/.ohmyllm/mail/outbox/` + IMAP Drafts append; works with zero setup), `email_send` (SMTP, proper In-Reply-To/References threading). Gating: send is approval-required in sandboxed/dangerous; silent only in full_access with `[mail] enabled=true`; an optional `recipient_allowlist` is enforced even in full_access and inside the tool itself. `/mail` status command in TUI + GUI; Settings screen and GUI config PATCH can never wipe or echo back the mail password.
- **Market** — MiniCPM-V-4.7-35B-A3B · IQ3_XS (14.5 GB, OpenBMB multimodal MoE, vision companion auto-fetched). New `hybrid` flag: MoE models designed for CPU+GPU offload report amber "hybrid CPU+GPU on N GB" instead of a false red "won't fit" on smaller cards.
- **GUI hold-to-talk** — Ctrl+E now records SERVER-side (pw-record/arecord via the bridge, same pipeline as the TUI): works in the Tauri window where browser getUserMedia is blocked, and ignores browser permission state entirely.
- **Provider brand logos** in the GUI providers list (simple-icons, CC0, vendored as inline SVG; near-black brand marks auto-whitened).
- **`/agentloop [on|off]`** — relentless verified-completion mode with a one-shot completion audit per turn.

### Fixed
- Settings save rejected `cloud` as a backend — a cloud-default config could never save; validation now accepts it and any error jumps to + focuses the offending field's tab.
- Cross-process config sync both ways: the GUI bridge reloads `config.toml` (mtime watch) before serving status/providers/config; the TUI reloads on provider/model refresh.
- Cloud context windows: provider `/models` metadata, `-128k/-1m` name suffixes and `id:256k` static-list declarations now drive the ctx% bar, auto-compaction and history trim (previously pinned to the local `num_ctx`, so a 256k cloud model compacted at ~3k tokens).
- Parakeet market entry switched to ggml-org `ggml-*.bin` (the installed whisper-cpp CLI cannot read handy-computer GGUFs); family-aware STT file matching.
- TUI input: long single-line text no longer replaced by a horizontal scrollbar; send button centered; market download progress docked and cancellable with esc.

## v0.1.0

Local-first agentic AI platform: TUI + GUI + bridge, permission engine, optional Clef-Flash danger guard.
