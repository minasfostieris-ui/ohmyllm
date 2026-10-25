# Memory: soul.md

OhMyLLM's memory is one human-readable file. No vector DB, no opaque blobs —
the agent's persistent self is **Markdown you can open in any editor**.

```
~/.ohmyllm/soul.md          # global memory (always loaded)
<project>/.ohmyllm/soul.md  # project memory (preferred when present)
~/.ohmyllm/backups/soul/    # timestamped backups (keeps last 50)
~/.ohmyllm/soul.changelog.jsonl
```

## Structure

Fixed sections, in order: `Identity`, `Current Work`, `Preferences`, `Pets`,
`Important Notes` (plus `Projects`, `Lessons`, `User` aliases the agent uses).
Entries are bullet lines; the agent appends `- [YYYY-MM-DD] fact` items.

## How the agent uses it

- The full file (or a budgeted slice, `memory.max_inject_chars`) is injected
  into the system prompt of every session.
- When you say something memorable ("my name is Minas", "remember that …",
  "I have a rabbit called Bnuy"), the agent calls `soul_append` /
  `soul_upsert` — real tool calls, visible in the transcript, audited.
- `soul_upsert` dedupes by similarity: re-stating a fact updates the existing
  bullet instead of spamming the file.
- `soul_forget` removes the best-matching entry after you approve it.
- In `strict` mode, memory writes happen **only** when you explicitly ask
  (`/remember …`); in `safe`/`sandboxed` the agent may write freely (toggle
  `memory.agent_writes` off to forbid it entirely).

| Action | TUI | GUI | CLI / chat |
|---|---|---|---|
| view/edit | `Ctrl+S` | Memory screen | `ohmyllm soul` / `--edit` / `--path` |
| append | `/remember <fact>` | `/remember` in composer | same — say it to the agent |
| search | `/forget <query>` | Memory screen | ask the agent (`soul_search`) |
| revert backup | Memory screen | Revert button | ask the agent, or restore from `backups/soul/` |
| clear | Memory screen | Clear button | `ohmyllm soul --edit` |

Every write takes a backup first; the Memory screen's **Revert backup** restores
the newest one. The changelog records who wrote what, when, and from which
session.

## Secrets

The memory writer **refuses** to store credential-shaped strings (API keys,
tokens, `password=…`) unless you force it — because soul.md is injected into
every prompt and exported with chats. Keep secrets out of memory; the audit
redactor assumes the same.
