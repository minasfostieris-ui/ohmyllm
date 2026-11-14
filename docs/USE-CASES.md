# OhMyLLM use-cases — local-first, by example

Concrete things you can do with a local model (Ollama or LM Studio) and the
`oml` alias. Everything below runs on your machine; cloud providers are
opt-in per key, and nothing phones home by default. Examples target the
0.1.1 release surface; a few commands (`state`, `chats search`, `/steer`,
`@diff`/`@folder` references) are finalized in this release cycle.

## 1. Offline working-tree review before you commit

```sh
oml run "@diff review my uncommitted changes for injection and resource leaks" --json
```

The `@diff` reference expands the working-tree git diff into the prompt
locally (`@staged` covers the index); `run --json` gives one machine-readable
result on stdout (diagnostics on stderr), so it pipes into CI or a pre-commit
hook. No code leaves the box when the backend is Ollama/LM Studio.

Range review (e.g. `main...HEAD`) is deliberately not supported: the git seam
only allowlists `diff`, `diff --staged` and `show <ref>`, and `..` ranges are
refused. To review a committed change, use `@git:<ref>` (a single commit/tag/
branch, optionally `REV:path`).

## 2. Log triage from a pipe

```sh
cat app.log | oml run - "group these errors by root cause, cite line numbers"
journalctl -u myservice --since today | oml run - "what failed and when?"
```

The explicit `-` argument reads stdin (bounded), so logs never need to be
pasted into a chat window or uploaded anywhere.

## 3. Privacy drafting in chat mode

In the TUI: `/chat`, then draft your message. Chat mode blocks file
writes, shell and mail at the safety layer; reads and research stay open —
so sensitive text (legal, medical, personal) cannot trigger those side
effects. Combined with a local backend, the draft never leaves the machine.

## 4. Shell copilot with a hard deny floor

```sh
oml run "find the largest files under /var and suggest cleanups"
```

Shell suggestions run through the policy engine: workspace confinement,
mandatory one-shot shell approval (session consent never covers shell), and
the `safety.deny` floor — fnmatch globs matched against normalized command
variants in EVERY mode, including `/yolo` and full_access. An invalid rule
fails closed: shell is denied entirely, not silently ignored. Set it in
config.toml:

```toml
safety.deny = ["*rm -rf*", "*mkfs*"]
```

The legacy `safety.command_denylist` remains a separate substring blocklist
checked in the shell branch. Catastrophic patterns stay blocked regardless
of mode.

## 5. Repo onboarding without reading everything

```sh
oml run "@folder:src summarize the architecture and list the entry points"
```

`@folder:` pulls a bounded, local snapshot of the tree into context. Ask
follow-ups in the TUI; the session persists, so tomorrow's you resumes where
today's you stopped (`oml chats list`, `oml chats open <id>`).

## 6. Repeatable playbooks via skills

Drop a folder with a `SKILL.md` (frontmatter + instructions) and a
`references/` subdirectory of supporting docs into your skills dir, then:

```text
/skill:release-checklist run it for this repo
```

Skills are plain files you own and version; `references/` keeps long
checklists out of the prompt until the skill needs them. `/skillimport`
opens the folder. Anything after `/skill:<name>` is sent along with the cast
as your instruction — in the TUI and in the GUI console alike.

## 7. Supervising long runs from herdr/tmux

```sh
oml state --json    # busy/idle, current session, turn, backend/model in use
```

Structured state output lets a multiplexer pane, a status line, or a
supervisor script poll progress without scraping the TUI. Pair with
`oml status --json` for config/backend diagnostics.

## 8. Incident forensics across past sessions

```sh
oml chats search "connection refused"
oml chats export <session-id> --format markdown
```

Full-text search over locally stored sessions finds where you saw an error
before; export produces a shareable (still local) artifact for the
postmortem. Sessions live in your home directory, not a service.

## 9. Mid-flight correction without restarting

While a turn is running, type a correction and hit Enter — or use `/steer`
— and it is queued as an interjection injected before the model's next
step. No cancel-and-retype, no lost tool progress.

## 10. Tutor mode: plan first, or stay strict

`/plan` switches to research-and-design mode (no writes, no shell, no
mail; reads and research stay open) — good for "teach me how this works"
sessions where the model should explain, not do. For stricter gating, keep
the default sandboxed permission mode: workspace writes ask by default
(outside writes are blocked), and only allowlisted shell forms can run,
each with fresh one-shot approval.

## 11. Batch file work over local files

```sh
oml run "read every .csv in ./data and write a combined summary to ./data/summary.md" --yes
```

`--yes` auto-approves non-shell tool calls for this run; shell commands
still require fresh approval, so a truly unattended batch must stay
shell-free. Sandboxed mode confines writes to the workspace. The audit log
records every tool decision (allowed/denied/blocked) for later review.

## 12. Debugging the context budget

When answers degrade on long sessions, watch the TUI's `compacted context:
N → M messages` lines and per-turn stats (tokens, steps, tok/s), or inspect
`oml run --json` usage fields. Compaction is explicit and logged, so you can
see exactly when history was summarized and start a fresh chat (`/new`)
before quality drops.

## 13. Air-gapped demos and development

```sh
oml run "hello" --backend mock
```

The scripted mock backend exercises the whole agent loop — streaming, tool
calls, approvals, memory — with zero GPU and zero network. Useful for
demos, CI, and developing on a train.