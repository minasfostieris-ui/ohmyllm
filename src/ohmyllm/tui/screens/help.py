"""Help screen."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Markdown

HELP = """\
# 🐣 OhMyLLM help

**Local-first agentic AI.** Everything runs on your machine: your models, your files, your memory.

## Global keyboard shortcuts
| keys | what |
|---|---|
| `Ctrl+K` | command palette (everything is searchable here) |
| `Ctrl+N` | new chat |
| `Ctrl+O` / `F2` | browse, search, resume chats |
| `Ctrl+M` | model manager |
| `Ctrl+B` | backend & server control |
| `Ctrl+P` | permission modes |
| `Ctrl+S` | memory / soul.md |
| `Ctrl+G` | settings |
| `Ctrl+D` | doctor (diagnostics) |
| `Ctrl+H` | this help |
| `Ctrl+X` | cancel the current agent turn |
| `Ctrl+C` / `Ctrl+Q` | quit (with confirmation) |

## Chat slash commands
`/help` `/model [name]` `/backend [ollama|lmstudio|mock]` `/mode [strict|safe|sandboxed|dangerous|full_access]`
`/memory` `/soul` `/doctor` `/settings` `/chats` `/new` `/clear` `/rename <title>` `/delete` `/think [on|off]`
`/export [markdown|json]` `/remember <fact>` `/forget <thing>` `/gui` `/quit`
`/market` `/providers` `/voice` `/onboarding` `/ultrathink [on|off]` `/agentloop [on|off]`

**`/agentloop`** arms a relentless completion loop: the system prompt forbids
stopping until every requirement is verified with evidence (run it, test it,
screenshot it and look at it if you can see images), the step budget rises,
and one completion audit blocks any final answer that lacks fresh proof.

**Where do API keys go?** `/providers` (or the Providers tab in the GUI):
paste your OpenRouter / Anthropic / OpenAI / … key — stored in
`~/.ohmyllm/config.toml` (owner-only), shown masked. Env keys
(`OMLLM_KEY_OPENROUTER`, `OPENAI_API_KEY`, …) work too and win.

**Voice**: hold `Ctrl+E` in chat to talk (SenseVoice/Parakeet/Whisper by
language — models in `/market`).


## Skills
Import OpenCode/Claude-style skill folders: `oml skills import ~/.config/opencode/skills`
Each folder needs a `SKILL.md` (name + description frontmatter). The agent sees the
index, reads a skill with `skills_read` before working in its domain, and lists them
with `skills_list`. Imported skills live in `~/.ohmyllm/skills/`.

## GUI console
The graphical console runs beside this TUI. From any terminal:

| command | what |
|---|---|
| `oml gui` or `ohmyllm gui` | native window; falls back to your browser automatically |
| `ohmyllm gui --browser` | force the browser console |
| `ohmyllm gui --no-open` | print the URL only (open it anywhere on this machine) |

Same engine, same permission gates, same chats & memory files. The GUI bridge binds
to 127.0.0.1 only with a per-process token; nothing else on your network can reach it.

## Permission modes (enforced by the platform, never by the model)
- **strict** — chat only; tools blocked; memory writes only when you explicitly ask.
- **safe** — read-only inside the workspace + soul.md memory tools.
- **sandboxed** *(default)* — workspace read/write (writes ask, unless "allow session"),
  small allowlist of shell commands, memory tools, no network.
- **dangerous** — broader files & shell, confirmations on anything risky, network needs approval.
- **full_access** — no routine prompts; enabled only by typing the confirmation phrase;
  catastrophic commands stay blocked unless you override in Settings; everything is audited.

## Memory
`soul.md` is loaded into every conversation as a secondary system prompt. The agent saves
durable facts with dedicated `soul_*` tools (never secrets, unless you insist). Backups are
automatic in `~/.ohmyllm/backups/soul/`. Project folders may carry their own
`.ohmyllm/soul.md` which takes precedence there.

## Agent loop
Message → model (streaming) → tool call (native or fenced-JSON/ReAct) → **validate** →
**policy check** → **approval (if required)** → execute (timeout-guarded) → result back to
model → repeat, until a final answer or the step limit. Every attempt is logged to
`~/.ohmyllm/logs/audit.jsonl` with redacted arguments.

## Copying text
Select with the mouse and use the palette action *Copy last reply*, or `/export`.
"""


class HelpScreen(Screen):
    BINDINGS = [Binding("escape", "close", "Back")]

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Markdown(HELP)

    def action_close(self) -> None:
        self.app.replace_screen("chat")
