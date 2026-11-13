# Permission Modes

OhMyLLM gates **every** tool call through a policy engine before execution. The
model can never change the mode — only you can, through the TUI, the GUI, or the
exact-phrase confirmation. Every gated decision is journaled to
`~/.ohmyllm/logs/audit.jsonl` (with secret redaction).

## The five modes

| Mode | Reads | File writes | Commands | Network | Notes |
|---|---|---|---|---|---|
| `strict` | workspace-scoped | forbidden | forbidden | forbidden | memory writes only when explicitly requested |
| `safe` | workspace-scoped | forbidden | forbidden | forbidden | conservative daily driver |
| `sandboxed` (default) | workspace auto; outside asks | workspace writes ask by default; outside blocked | allowlisted forms require fresh approval | disabled by default; enabled allowlist asks | strongest practical boundary |
| `dangerous` | outside workspace asks | risk and boundary gated | every call requires fresh approval | disabled by default; enabled allowlist asks | for trusted work |
| active `full_access` | auto except sensitive state | auto except protected/system gates | auto except catastrophic guard | only when enabled and allowlisted | process-local confirmation required |

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

- **Sensitive paths** (`~/.ssh`, `~/.gnupg`, browser profiles, `.env` variants,
  private OhMyLLM state, keyring files…) are denied unless an explicit
  component-aware `safety.allow_sensitive_paths` boundary permits them. Prefix
  siblings do not match, and filesystem tools reject symlink traversal.
- **Catastrophic commands** (`rm -rf /`, fork bombs, `mkfs`, `dd` to block
  devices, `curl | sh`, history wipes, …) are blocked unless
  `safety.catastrophic_override = true`; the decision remains audited.
- **Unknown tools** are denied (fail closed).
- **Approval failures fail closed**: a crashed UI or timed-out approval means
  *denied*.

## Granting full_access

`full_access` cannot become active from persisted configuration alone. In the
TUI, plain UI, or dedicated GUI permission control, you must type the exact
phrase in the current process:

```
I ACCEPT FULL ACCESS
```

The phrase gate exists because full_access means the agent runs tools with no
human in the loop; the catastrophic-command guard remains as the last backstop.
On restart, persisted `full_access` is deactivated to `dangerous`, advances the
policy generation, and invalidates prior approvals.


## Danger guard (Clef-Flash)

`dangerous` requires fresh approval for every shell call and gates risky or
boundary-crossing mutations; `full_access` runs most tools unprompted, with
the catastrophic-command and protected-system guards still in force. The
optional **danger guard** adds a fast local *decision model* in front of
non-read calls in both modes: each call is scored by
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

Failure policy: if the guard server is down, calls in dangerous/full_access
are escalated to a fresh approval (fail-closed) and the tool panel notes the
guard was offline — an unreachable guard can never silently widen permissions.

Any llama.cpp `/v1/systemone` decision server works (Clef, Laya, openjev):
point `guard.base_url` at it and set `guard.model`.

## Approval outcomes

When the engine asks, you can answer:

- **Allow once** — this exact call only.
- **Allow for session** — this exact validated request in the current workspace,
  chat session, and policy generation.
- **Permanent rule** — the same exact request identity bound to the workspace
  and policy generation (no session binding, so it survives new chat sessions),
  persisted privately to `~/.ohmyllm/rules.json`.
- **Deny** — the model is told the call was denied and must adapt.

Approval identities hash the complete canonical arguments; values are neither
whitespace-normalized nor truncated. Shell approvals are always one-shot outside
active `full_access`, even if “session” or “always” is selected. Legacy unscoped
approval files — including older permanent keys that embedded a session id —
are intentionally ignored.

## Tool classes

`read`, `write`, `shell`, `net`, `memory_read`, `memory_write`, `status` — each
with its own risk scoring. `risk` is 0–10; `level` is `low|medium|high|critical`
and decides which approvals are unavoidable regardless of mode.

## Network and bridge boundaries

`http_fetch` is an unconditional denial while disabled. Enabling it also
requires an explicit exact-host or `.suffix` allowlist. URL credentials,
private/local/metadata/reserved addresses, redirects, and inherited proxy
settings are rejected. Remote model backend URLs separately require
`safety.allow_remote_backends = true`; automatic backend startup remains
loopback-only.

The GUI bridge binds loopback and accepts its token only in the
`X-OMLLM-Token` header. Query tokens do not authenticate. Browser launch uses a
transient fragment that the page immediately removes; discovery state is a
private, bounded JSON file cleaned up on exit. Host, Origin, body size, JSON
shape, request concurrency, event queues, and interjection queues are bounded
and fail closed.

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
If the audit log becomes persistently unwritable, mutating tool calls are
escalated to a fresh approval (fail-closed) until audit health is restored.
