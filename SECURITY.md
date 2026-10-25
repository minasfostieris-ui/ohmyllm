# Security Policy

## What counts as a vulnerability

OhMyLLM is a local-first tool: it talks only to `127.0.0.1` model servers and
runs tools on your own machine. In that context, report privately:

- Any way the permission engine can be bypassed **without the user
  approving it** in `strict`, `safe`, or `sandboxed` mode (e.g. a path or
  command that slips past the gates and executes).
- Bridge token leakage or remote access to the GUI bridge (it must bind
  localhost and require the token on every non-`/health` route).
- Path traversal / arbitrary file read or write through the built-in tools
  in modes that forbid it.
- Secret leakage into logs, audit rows, or the GUI (secret redaction is a
  documented invariant).

## What does *not* count

- `dangerous` / `full_access` doing dangerous things — that is the documented
  purpose of those modes, and the README disclaimer says so.
- A model producing bad, wrong, or offensive output — that is the model, not
  this software.
- Prompt-injection that ends in a tool call **you approved** — approvals are
  the user's consent gate.
- Attacks requiring physical access to the machine or already-compromised
  accounts on it.

## How to report

Use GitHub's **private vulnerability reporting** on this repo (Security tab →
Report a vulnerability), or message me on Discord (`gonber0606`) and ask for
a private channel. Please don't open a public issue for live exploits.

I aim to acknowledge within 7 days. There is no bug bounty.

## Note on the guardrails

The policy engine is a **guardrail, not a sandbox**. It is regex and path
analysis over tool arguments — good at stopping accidents and careless
agents, not a hardened boundary against a determined attacker with code
execution already on the box. For hostile workloads, run OhMyLLM inside a
real OS sandbox or VM.
