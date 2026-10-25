# Permission Modes

OhMyLLM gates **every** tool call through a policy engine before execution. The
model can never change the mode — only you can, through the TUI, the GUI, or the
exact-phrase confirmation. Every gated decision is journaled to
`~/.ohmyllm/logs/audit.jsonl` (with secret redaction).

## The five modes

| Mode | Reads | File writes | Commands | Network | Notes |
|---|---|---|---|---|---|
| `strict` | workspace-scoped, auto | forbidden | always approval, allowlisted | blocked (unless allowlisted + approval) | memory writes only when you explicitly ask (`/remember`) |
| `safe` (default) | workspace-scoped, auto | forbidden | approval by risk score | gated | balanced daily driver |
| `sandboxed` | workspace-scoped, auto | workspace writes, confirmation optional | allowlisted base commands + subcommand gates | gated | strongest containment |
| `dangerous` | mostly auto | auto outside workspace unless sensitive | auto except catastrophic/high risk | gated approvals | for trusted work |
| `full_access` | auto | auto | auto, except catastrophic | auto | **no approvals at all** |

## Honest limitations

The engine inspects tool arguments with regex and path analysis. That makes
it good at stopping accidents, typos, and careless agents — it is **not** a
security boundary. Shell syntax is slippery (obfuscation, encodings, nested
expansion), and a model determined enough to craft a bypass plus a user who
approves the call = game over by design: the human approval is the boundary.
If you need containment against hostile content, run OhMyLLM inside an OS
sandbox or VM; `sandboxed` mode is the strongest the engine itself offers.

## Cross-mode invariants

These hold in **every** mode, including `full_access`:

- **Sensitive paths** (`~/.ssh`, `~/.gnupg`, browser profiles, `.env`, keyring
  files…) are denied unless you explicitly set
  `safety.allow_sensitive_paths` to a matching prefix. The read gate uses the
  same rule.
- **Catastrophic commands** (`rm -rf /`, fork bombs, `mkfs`, `dd` to block
  devices, `curl | sh`, history wipes, …) are blocked unless
  `safety.block_catastrophic = false` — a config option deliberately named so
  it can never be flipped casually, and the block reason is always audited.
- **Unknown tools** are denied (fail closed).
- **Approval failures fail closed**: a crashed UI or timed-out approval means
  *denied*.

## Granting full_access

`full_access` cannot be set from the CLI, the config file, or the GUI settings
form. In the TUI (or GUI permission chip) you must type the exact phrase:

```
I ACCEPT FULL ACCESS
```

The phrase gate exists because full_access means the agent runs tools with no
human in the loop; the catastrophic-command guard remains as the last backstop.


## Danger guard (Clef-Flash)

`dangerous` and `full_access` normally run mutating tools with no prompt.
The optional **danger guard** puts a fast local *decision model* in front of
them: every non-read tool call is scored by
[Cloudflare Clef-Flash](https://huggingface.co/Cloudflare/clef-flash)
(a 9B Qwen fine-tune that answers typed questions in one forward pass —
it does not generate text). If the model's `dangerous` probability crosses
`guard.danger_threshold`, the call is escalated to a normal approval prompt
showing the guard's probability. The guard never *blocks* — it only forces
the human back into the loop for calls it considers dangerous.

**CPU + RAM only (by design).** The coding model owns your GPU; the guard
must not compete for VRAM. Serve it with llama.cpp pinned to zero GPU layers:

```bash
llama-server -hf ggml-org/Clef-Flash-GGUF -ngl 0 --port 9931 --ubatch-size 4096
```

(`-ngl 0` = no GPU offload; the Q4 GGUF needs ~6 GB system RAM. Clef prompts
are scored in a single batch, so keep `--ubatch-size` above your typical
tool-call size.) Then in **Settings → Danger guard** tick *enabled* (or set
`[guard] enabled = true` in `config.toml`). `oml doctor` probes the server.

Failure policy: if the guard server is down, calls proceed unchecked in
dangerous/full_access modes (fail-open) and the tool panel notes the guard
was offline — the helper process must never brick your agent.

Any llama.cpp `/v1/systemone` decision server works (Clef, Laya, openjev):
point `guard.base_url` at it and set `guard.model`.

## Approval outcomes

When the engine asks, you can answer:

- **Allow once** — this call only.
- **Allow for session** — same tool + same risk level, rest of the session.
- **Permanent rule** — persisted to `~/.ohmyllm/approvals.json`; manage them
  with `ohmyllm audit` / the settings screen.
- **Deny** — the model is told the call was denied and must adapt.

## Tool classes

`read`, `write`, `shell`, `net`, `memory_read`, `memory_write`, `status` — each
with its own risk scoring. `risk` is 0–10; `level` is `low|medium|high|critical`
and decides which approvals are unavoidable regardless of mode.

## Auditing

Every gate decision, approval outcome, and tool result lands in
`audit.jsonl`:

```json
{"ts": 1760000000.1, "event": "tool", "tool": "run_command", "mode": "safe",
 "decision": "approved", "ok": true, "risk": 6, "args_preview": "git commit …",
 "duration_ms": 812}
```

Secrets matching common patterns (API keys, tokens, passwords in `KEY=…`
positions) are redacted before writing. `ohmyllm audit -n 50` shows the tail.
