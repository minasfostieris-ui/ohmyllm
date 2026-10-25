"""Chat/Agent screen: streaming markdown, slash commands, tool panel, approvals."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import Collapsible, Input, Markdown, OptionList, Static
from textual.widgets._option_list import Option

from ... import paths
from ...verbs import verb
from ...agents.runtime import AgentCancelled
from ...sessions import make_title
from ..widgets import ModeBanner, SepBar, StatusBar, ToolPanel


def pathlib_home():
    return paths.global_home()


class ChatScreen(Screen):
    SLASH: tuple[tuple[str, str, str], ...] = (
        ("/help", "", "this help screen"), ("/model", "[name]", "set or pick the default model"), ("/modelsettings", "", "per-model settings for the current model"),
        ("/backend", "[ollama|lmstudio|mock]", "switch backend"), ("/mode", "[name]", "permission mode"),
        ("/think", "[on|off]", "reasoning channel for thinking models"),
        ("/ultrathink", "[on|off]", "deep reasoning: think much longer and harder"),
        ("/memory", "", "soul.md editor"), ("/soul", "", "soul.md editor"), ("/doctor", "", "diagnostics"),
        ("/settings", "", "all settings"), ("/chats", "", "browse chats"), ("/new", "", "fresh chat"),
        ("/clear", "", "fresh chat"), ("/rename", "<title>", "rename this chat"),
        ("/delete", "", "delete this chat"), ("/export", "[markdown|json]", "export chat"),
        ("/remember", "<fact>", "save to memory"), ("/forget", "<thing>", "remove from memory"),
        ("/quit", "", "exit"), ("/gui", "", "open the web/native console"),
        ("/omlgui", "", "alias of /gui"),
    )

    BINDINGS = [
        Binding("up", "slash_prev", "", show=False, priority=False),
        Binding("down", "slash_next", "", show=False, priority=False),
        Binding("tab", "slash_complete", "", show=False, priority=False),
        Binding("ctrl+t", "toggle_thinking", "hide/show live thinking", priority=True),
    ]

    def _menu(self) -> OptionList | None:
        try:
            menu = self.query_one("#slash-menu", OptionList)
            return menu if menu.display else None
        except NoMatches:
            return None

    def action_slash_prev(self) -> None:
        menu = self._menu()
        if menu is not None and menu.option_count:
            menu.index = max(0, (menu.index or 0) - 1)

    def action_slash_next(self) -> None:
        menu = self._menu()
        if menu is not None and menu.option_count:
            menu.index = min(menu.option_count - 1, (menu.index or -1) + 1)

    def action_slash_complete(self) -> None:
        self._slash_accept()

    def _slash_accept(self) -> bool:
        menu = self._menu()
        if menu is None or not menu.option_count:
            return False
        opt = menu.get_option_at_index(menu.index if menu.index is not None else 0)
        if opt is None:
            return False
        cmd = str(opt.id)
        inp = self.query_one("#chat-input", Input)
        typed = inp.value.strip()
        if not cmd.startswith(typed) or typed.endswith(" ") or typed == cmd:
            return False
        inp.value = cmd + " "
        inp.focus()
        return True

    @on(Input.Changed, "#chat-input")
    def _input_changed(self, event: Input.Changed) -> None:
        self._update_slash_menu(event.value)

    def _update_slash_menu(self, value: str) -> None:
        menu = self.query_one("#slash-menu", OptionList)
        v = value.strip()
        if not v.startswith("/") or " " in v:
            menu.display = False
            menu.clear_options()
            return
        prefix = v.lower()
        matches = [(c, a, d) for c, a, d in self.SLASH if c.startswith(prefix)]
        menu.clear_options()
        for c, a, d in matches[:12]:
            menu.add_option(Option(f"[b]{c}[/b] [dim]{a}[/dim]  [dim]{d}[/dim]", id=c))
        if matches:
            menu.display = True
            menu.index = 0
        else:
            menu.display = False

    def compose(self) -> ComposeResult:
        yield ModeBanner(id="mode-banner")
        yield Static("", id="chat-title")
        with Horizontal(id="chat-root"):
            with Vertical(id="chat-left"):
                yield VerticalScroll(id="history")
            yield SepBar("", id="panel-sep")
            yield ToolPanel()
        yield OptionList(id="slash-menu")
        yield Input(placeholder="ask anything — / for commands (Enter to send)…", id="chat-input")
        yield StatusBar()

    def on_mount(self) -> None:
        self._buffer = ""
        self._stream_md: Markdown | None = None
        self._last_flush = 0.0
        self._turn_model = ""
        self._reasoning = ""
        self._think_md: Markdown | None = None
        self._think_col: Collapsible | None = None
        self._think_hidden: Static | None = None
        self._think_buf = ""
        self._think_flush = 0.0
        self._file_live: Static | None = None
        self._file_flush = 0.0
        self._file_pending: tuple[str, str] | None = None
        self._tool_live_active = False
        self._render_history()
        self._refresh_title()
        if self.app.session and self.app.session.id in self.app.drafts:
            self.query_one("#chat-input", Input).value = self.app.drafts.pop(self.app.session.id)
        self.query_one("#chat-input", Input).focus()
        self.app.refresh_chrome()
        if self.app.turn_active:
            # user left mid-turn (menu/screen switch): rebuild the live response
            hist = self.query_one("#history", VerticalScroll)
            if self.app.turn_thinking:
                self._think_buf = self.app.turn_thinking
                if self.app.svc.config.agent.thinking_visible:
                    self._think_md = Markdown(self._think_buf, classes="msg-think")
                    self._think_col = Collapsible(self._think_md, title=f"💭 {verb()} · thinking…",
                                                  collapsed=False, collapsed_symbol="+", expanded_symbol="-")
                    hist.mount(self._think_col)
                else:
                    self._think_hidden = Static(self._think_count(), classes="msg-system")
                    hist.mount(self._think_hidden)
            if self.app.turn_text:
                self._buffer = self.app.turn_text
                self._stream_md = Markdown(self._buffer, classes="msg-assistant")
                hist.mount(self._stream_md)
            hist.scroll_end(animate=False)
        self.set_interval(1.0, self._push_status)
        self._push_status()

    def on_unmount(self) -> None:
        try:
            inp = self.query_one("#chat-input", Input)
            if self.app.session and inp.value:
                self.app.drafts[self.app.session.id] = inp.value
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------- rendering
    def _render_history(self) -> None:
        hist = self.query_one("#history", VerticalScroll)
        hist.remove_children()
        sess = self.app.session
        if not sess:
            return
        for m in sess.messages:
            role, content = m.get("role"), str(m.get("content", ""))
            if not content or role == "tool":
                continue
            if role == "assistant":
                md = Markdown(content, classes="msg-assistant")
                hist.mount(md)
            elif role == "user":
                hist.mount(Static(_who("you", content), classes="msg-user"))
            else:
                hist.mount(Static(_who("note", content), classes="msg-system"))
        hist.scroll_end(animate=False)

    def _refresh_title(self) -> None:
        sess = self.app.session
        cfg = self.app.svc.config
        if sess is None:
            return
        saved = "✓ saved" if self.app.save_state else "draft"
        try:
            title = self.query_one("#chat-title", Static)
        except NoMatches:  # screen replaced mid-turn — children are gone
            return
        title.update(
            f"[bold]💬 {sess.title[:70]}[/]  [dim]·  {cfg.default_backend.value}:{sess.model or '—'}"
            f"  ·  mode {cfg.permission_mode.value}  ·  {sess.message_count} msgs  ·  {saved}"
            f"  ·  ctrl+K palette · F2 chats[/dim]"
        )

    def _push_status(self) -> None:
        bar = self.query(StatusBar)
        for b in bar:
            b.render_state(self.app.status_state())
        if self.app.busy:
            for panel in self.query(ToolPanel):
                panel.rotate_thinking()
            if self._think_col is not None:
                self._think_col.title = f"💭 {verb()} · thinking…"

    def _think_count(self) -> str:
        return f"💭 thinking · hidden ({len(self._think_buf)} chars) — Ctrl+T to show"

    def _follow(self, hist: VerticalScroll | None = None, force: bool = False) -> None:
        """Stick to the bottom only while the user is AT the bottom: scrolling
        up mid-stream must not be yanked back by the next token."""
        try:
            h = hist or self.query_one("#history", VerticalScroll)
        except NoMatches:
            return
        if force or h.scroll_y >= h.max_scroll_y - 2:
            h.scroll_end(animate=False)

    def action_toggle_thinking(self) -> None:
        cfg = self.app.svc.config.agent
        cfg.thinking_visible = not cfg.thinking_visible
        self.app.svc.save_config()
        if not cfg.thinking_visible and (self._think_md is not None or self._think_col is not None):
            if self._think_col is not None:
                self._think_col.remove()
                self._think_col = None
            self._think_md = None
            self._think_hidden = Static(self._think_count(), classes="msg-system")
            self.query_one("#history", VerticalScroll).mount(self._think_hidden)
        elif cfg.thinking_visible and self._think_hidden is not None:
            self._think_hidden.remove()
            self._think_hidden = None
            if self._think_buf:
                self._think_md = Markdown(self._think_buf, classes="msg-think")
                self._think_col = Collapsible(self._think_md, title=f"💭 {verb()} · thinking…",
                                              collapsed=False, collapsed_symbol="+", expanded_symbol="-")
                self.query_one("#history", VerticalScroll).mount(self._think_col)
        self.app.notify(f"live thinking {'shown' if cfg.thinking_visible else 'hidden'}")

    def _sysline(self, text: str, style: str = "system") -> None:
        try:
            hist = self.query_one("#history", VerticalScroll)
        except NoMatches:
            return
        hist.mount(Static(_who("note", text), classes=f"msg-{style}"))

    # ----------------------------------------------------------------- input
    @on(Input.Submitted, "#chat-input")
    def _submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        if self._slash_accept():  # menu open → Enter completes, doesn't send
            return
        event.input.value = ""
        if text.startswith("/"):
            self._slash(text)
            return
        self._send(text)

    def _send(self, text: str, memory_directive: bool = False) -> None:
        if self.app.busy:
            # queue it: lands in the session before the NEXT model call
            self.app.interjections.append(text)
            self._sysline(f"queued for the running turn: {text[:80]}", "system")
            self.app.notify(f"queued ({len(self.app.interjections)}) — injected at the next step")
            return
        self.query_one("#history", VerticalScroll).mount(Static(_who("you", text), classes="msg-user"))
        self.app.note_title(text)
        self._refresh_title()
        self._run_turn(text, memory_directive)

    @work(exclusive=True, group="turn")
    async def _run_turn(self, text: str, memory_directive: bool) -> None:
        app = self.app
        app.busy = True
        app.cancel_event.clear()
        app.save_state = False
        self._push_status()
        rt = app.svc.runtime()
        sess = app.session
        assert sess is not None
        try:
            result = await rt.run_turn(
                sess, text,
                prompt_fn=app.request_approval,
                on_event=self.app.dispatch_turn_event,
                cancel=app.cancel_event,
                user_requested_memory=memory_directive,
                interjections=app.interjections,
            )
            # turn_end already finalized on success; only salvage streams that
            # ended early (backend error / cancel) — never render the reply twice
            if self._stream_md is not None:
                self._finalize(result.text if (result.text and not result.error) else None)
            if result.error:
                self._sysline(f"backend error: {result.error} — check /backend or Ctrl+B", "error")
            app.tok_s = result.tokens_per_s or app.tok_s
        except AgentCancelled:
            self._finalize()
            self._sysline("turn cancelled", "system")
        except Exception as exc:  # noqa: BLE001 — surface, never crash the UI
            self._finalize()
            self._sysline(f"unexpected error: {type(exc).__name__}: {exc}", "error")
        finally:
            app.busy = False
            try:
                app.svc.sessions.save(sess)
                app.save_state = True
            except OSError as exc:
                self._sysline(f"session save failed: {exc}", "error")
            self._refresh_title()
            self._push_status()
            if app.interjections:  # messages sent after the last drain → next turn
                nxt = app.interjections.pop(0)
                self.call_next(lambda: self._send(nxt))

    # ---------------------------------------------------------------- events
    def _on_event(self, ev: dict[str, Any]) -> None:
        try:
            panel = self.query_one(ToolPanel)
        except NoMatches:
            return  # turn outlived its screen (user switched chats); drop UI events
        t = ev.get("type")
        if t == "turn_start":
            self._turn_model = str(ev.get("model") or "")
            self._reasoning = ""
            self._think_buf = ""
            self._think_flush = 0.0
        if t == "token":
            self._seal_thinking()  # the answer began; tuck the thoughts away
            self._buffer += str(ev.get("text", ""))
            if self._tool_text_live() or self._tool_live_active:
                return  # streaming a write_file call as plain text: the preview owns the view
            self._seal_file_live()
            now = time.monotonic()
            if self._stream_md is None:
                self._stream_md = Markdown(self._buffer, classes="msg-assistant")
                self.query_one("#history", VerticalScroll).mount(self._stream_md)
                self._last_flush = now
            elif now - self._last_flush > 0.06:
                self._last_flush = now
                self._stream_md.update(self._buffer)
            self._follow()
        elif t == "thinking_text":
            chunk_txt = str(ev.get("text", ""))
            self._reasoning = (self._reasoning + chunk_txt)[:20000]
            self._think_buf += chunk_txt
            now = time.monotonic()
            if not self.app.svc.config.agent.thinking_visible:
                if self._think_col is not None:
                    self._think_col.remove()
                    self._think_col = None
                if self._think_md is not None:
                    self._think_md = None
                if self._think_hidden is None:
                    self._think_hidden = Static(self._think_count(), classes="msg-system")
                    self.query_one("#history", VerticalScroll).mount(self._think_hidden)
                elif now - self._think_flush > 0.4:
                    self._think_flush = now
                    self._think_hidden.update(self._think_count())
                return
            if self._think_hidden is not None:
                self._think_hidden.remove()
                self._think_hidden = None
            if self._think_md is None:
                self._think_md = Markdown(self._think_buf, classes="msg-think")
                self._think_col = Collapsible(self._think_md, title=f"💭 {verb()} · thinking…",
                                              collapsed=False, collapsed_symbol="+", expanded_symbol="-")
                self.query_one("#history", VerticalScroll).mount(self._think_col)
                self._think_flush = now
            elif now - self._think_flush > 0.08:
                self._think_flush = now
                self._think_md.update(self._think_buf)
            self._follow()
        elif t == "tool_stream":
            panel.add_event(ev)
            self._file_preview(ev)
        elif t == "compaction":
            panel.add_event(ev)
            self._sysline(f"compacted context: {ev.get('before')} → {ev.get('after')} messages", "system")
        elif t == "interjection":
            pass  # already shown when queued
        elif t in ("tool_plan", "tool_result", "memory_activity", "retry", "error", "thinking"):
            panel.add_event(ev)
            if t == "tool_result" and ev.get("file") and ev.get("ok"):
                self._file_line(ev)
            if t == "tool_result":
                self._seal_file_live()
                if ev.get("blocked"):
                    self._sysline(str(ev.get("output", ""))[:300], "system")
        elif t == "turn_end":
            self._seal_thinking()
            self._seal_file_live()
            self._finalize()
            self._thinking_block()
            self._stats_line(ev)

    def _tool_text_live(self) -> bool:
        """True while the text buffer is a streaming write_file/patch call.

        Models without native tool support emit the call as plain JSON content;
        those tokens must drive the live preview instead of being sealed away."""
        buf = self._buffer.lstrip()
        if not buf.startswith("{"):
            return self._tool_live_active
        import re as _re
        if '"name"' not in buf and '"tool"' not in buf:
            # still only the opening brace(s) of a possible call: suppress the
            # raw-JSON flash until the envelope is identifiable (bounded)
            return self._tool_live_active or len(buf) < 24
        name_m = _re.search(r'"(?:name|tool)"\s*:\s*"([a-z_]*)"', buf)
        if name_m and not "write_file".startswith(name_m.group(1)):
            return False  # a different tool (or plain JSON answer): show its text
        self._tool_live_active = True
        m = _re.search(r'"content"\s*:\s*"((?:[^"\\]|\\.)*)\Z', buf)
        if not m:
            # content string closed: paint the complete value once, then hold
            m = _re.search(r'"content"\s*:\s*"((?:[^"\\]|\\.)*)"', buf)
        if m:
            try:
                partial = m.group(1).encode().decode("unicode_escape", errors="replace")
            except Exception:  # noqa: BLE001
                partial = m.group(1)
            self._paint_file_live("write_file", partial, len(buf))
        return True

    def _file_preview(self, ev: dict[str, Any]) -> None:
        """Live view of file content as the model streams the write arguments.

        Plain Text (no Markdown re-parsing) + 0.1s repaint throttle so the
        UI stays smooth while tokens pour in."""
        import re

        ct = str(ev.get("content_tail") or "")
        # the final stream event carries the JSON close (quote + braces): cut at it
        close_m = re.match(r'((?:[^"\\]|\\.)*)"', ct)
        if close_m:
            ct = close_m.group(1)
        if ct:
            try:
                partial = ct.encode().decode("unicode_escape", errors="replace")
            except Exception:  # noqa: BLE001
                partial = ct
        else:  # older event shape: find the marker inside the tail
            tail = str(ev.get("tail") or "")
            m = re.search(r'"content"\s*:\s*"((?:[^"\\]|\\.)*)\Z', tail)
            if not m:
                return
            try:
                partial = m.group(1).encode().decode("unicode_escape", errors="replace")
            except Exception:  # noqa: BLE001
                partial = m.group(1)
            partial = partial.replace("\\n", "\n").replace('\\"', '"')
        name = str(ev.get("name") or "")
        name = str(ev.get("name") or "")
        now = time.monotonic()
        if now - self._file_flush < 0.1:
            # coalesce bursts, but guarantee the final state paints
            if not getattr(self, "_file_flush_armed", False):
                self._file_flush_armed = True
                self.set_timer(0.15, self._flush_file_pending)
            return
        self._file_flush = now
        self._paint_file_live(name, partial, int(ev.get("chars") or 0))

    def _flush_file_pending(self) -> None:
        self._file_flush_armed = False
        if self._file_pending is not None:
            name, partial = self._file_pending
            self._file_flush = time.monotonic()
            self._paint_file_live(name, partial, 0)

    def _paint_file_live(self, name: str, partial: str, chars: int) -> None:
        from rich.text import Text as _Text
        self._file_pending = (name, partial)
        try:
            hist = self.query_one("#history", VerticalScroll)
        except NoMatches:
            return
        lines = partial.splitlines()[-14:]
        body = _Text()
        body.append(f"writing {name}…\n", style="bold cyan")
        body.append("\n".join("│ " + ln for ln in lines), style="bright_white")
        if self._file_live is None:
            self._file_live = Static(body, classes="msg-file-live")
            hist.mount(self._file_live)
        else:
            self._file_live.update(body)
        self._follow(hist)

    def _seal_file_live(self) -> None:
        self._tool_live_active = False
        if self._file_live is not None:
            live = self._file_live
            self._file_live = None
            try:
                live.remove()
            except Exception:  # noqa: BLE001
                pass

    def _file_line(self, ev: dict[str, Any]) -> None:
        from rich.text import Text

        f = ev.get("file") or {}
        name = str(f.get("path") or ev.get("name") or "file")
        line = Text()
        if f.get("created"):
            line.append("+ created ", style="bold")
            line.append(name, style="bold cyan")
            line.append(f"  +{f.get('added', 0)}", style="green")
        else:
            line.append("~ edited ", style="bold")
            line.append(name, style="bold cyan")
            if f.get("removed"):
                line.append(f" -{f['removed']}", style="bold red")
            if f.get("changed"):
                line.append(f" ~{f['changed']}", style="bold yellow")
            line.append(f" +{f.get('added', 0)}", style="bold green")
        try:
            self.query_one("#history", VerticalScroll).mount(Static(line, classes="msg-file"))
        except NoMatches:
            return

    def _seal_thinking(self) -> None:
        """Collapse the live thinking block in place: same widget the user has
        been watching, so the affordance never disappears mid-turn."""
        if self._think_md is None and self._think_hidden is None and self._think_col is None:
            return
        body = self._think_buf
        col = self._think_col
        self._think_md = None
        self._think_col = None
        if self._think_hidden is not None:
            self._think_hidden.remove()
            self._think_hidden = None
        self._think_buf = ""
        try:
            self.query_one("#history", VerticalScroll)
            self.query_one("#chat-input")  # liveness probe
        except NoMatches:
            return
        if col is not None:
            if body.strip():
                col.title = "thinking process"
                col.collapsed = True
            else:
                col.remove()
            self._reasoning = ""  # already shown as a block
            return
        if body.strip():
            self.query_one("#history", VerticalScroll).mount(
                Collapsible(Markdown(body[:4000], classes="msg-system"),
                            title="thinking process", collapsed=True,
                            collapsed_symbol="+", expanded_symbol="-")
            )
        self._reasoning = ""  # already shown as a block

    def _thinking_block(self) -> None:
        text = self._reasoning.strip()
        self._reasoning = ""
        if not text:
            return
        try:
            hist = self.query_one("#history", VerticalScroll)
        except NoMatches:
            return
        body = Markdown(text[:4000] + ("…" if len(text) > 4000 else ""), classes="msg-system")
        hist.mount(Collapsible(body, title="thinking process", collapsed=True,
                            collapsed_symbol="+", expanded_symbol="-"))

    def _stats_line(self, ev: dict[str, Any]) -> None:
        try:
            hist = self.query_one("#history", VerticalScroll)
        except NoMatches:
            return
        text = (ev.get("text") or "").strip()
        if not text and not ev.get("error"):
            self._sysline("the model returned no text — try another model (Ctrl+M) or check the server", "error")
        bits: list[str] = []
        if self._turn_model:
            bits.append(self._turn_model)
        tok = (ev.get("usage") or {}).get("eval_count") or ev.get("tokens")
        if tok:
            bits.append(f"{tok} tok")
        if ev.get("ttft_s"):
            bits.append(f"ttft {float(ev['ttft_s']):.1f}s")
        if ev.get("tokens_per_s"):
            bits.append(f"{float(ev['tokens_per_s']):.1f} t/s")
        if ev.get("steps"):
            bits.append(f"{ev['steps']} step{'s' if ev['steps'] > 1 else ''}")
        calls = ev.get("calls") or []
        if calls:
            bits.append(f"{len(calls)} tool call{'s' if len(calls) > 1 else ''}")
        if bits:
            hist.mount(Static("  ".join(["·"] + bits), classes="turn-stats"))
        tps = float(ev.get("tokens_per_s") or 0)
        if 0 < tps < 8:
            self._sysline(
                f"[yellow]⚠ {tps:.1f} t/s is CPU-speed for this model[/] — open [b]/modelsettings[/] "
                "and raise [b]GPU layers[/] (−1 = all) so the weights sit in VRAM; a 9B Q4 runs "
                "20-40 t/s on a 12 GiB GPU. If VRAM is tight, lower [b]Context length[/] too.",
                "system",
            )

    def _finalize(self, force_text: str | None = None) -> None:
        try:
            hist = self.query_one("#history", VerticalScroll)
        except NoMatches:
            self._stream_md = None
            self._buffer = ""
            return
        if self._stream_md is not None:
            self._stream_md.update(self._buffer if force_text is None else force_text)
            self._stream_md = None
        elif force_text:
            hist.mount(Markdown(force_text, classes="msg-assistant"))
        self._buffer = ""
        self._follow(hist)

    # --------------------------------------------------------------- slash
    @work(exclusive=True)
    async def _launch_gui(self) -> None:
        """Start (or re-open) the web console bridge beside this TUI."""
        import os
        import subprocess
        import sys
        import tempfile
        import time
        import webbrowser
        from pathlib import Path

        home = pathlib_home()
        native = next((c for c in [
            Path.home() / ".local" / "bin" / "ohmyllm-gui",
            Path("/usr/local/bin/ohmyllm-gui"),
        ] if c.is_file()), None)
        if shutil_which := __import__("shutil").which("ohmyllm-gui"):
            native = Path(shutil_which) if native is None else native
        if native is not None:
            env = dict(os.environ)
            env["OMLLM_PYTHON"] = sys.executable
            try:
                subprocess.Popen([str(native)], env=env, start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass  # fall through to the plain web bridge below
            # also expose the optional localhost console and surface its URL
            try:
                wport = Path(tempfile.gettempdir()) / f"ohmyllm-gui-tui-{os.getpid()}.port"
                wport.unlink(missing_ok=True)
                wchild = subprocess.Popen(
                    [sys.executable, "-m", "ohmyllm.bridge", "--port-file", str(wport)],
                    env=env, start_new_session=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                (home / "gui-web.pid").write_text(str(wchild.pid))
                deadline = time.monotonic() + 8.0
                while time.monotonic() < deadline:
                    if wport.is_file() and len(wport.read_text().strip().split(" ")) == 2:
                        port, token = wport.read_text().strip().split(" ")
                        url = f"http://127.0.0.1:{port}/?t={token}"
                        (home / "gui-web.url").write_text(url)
                        self.app._notice(f"console up · web (optional): {url}")
                        return
                    await asyncio.sleep(0.1)
                self.app._notice("console launching · web URL will be in ~/.ohmyllm/gui-web.url")
            except OSError:
                self.app._notice("console launching in the background")
            return
        pidf = home / "gui.pid"
        urlf = Path(tempfile.gettempdir()) / "ohmyllm-gui.url"
        if pidf.is_file():
            try:
                pid = int(pidf.read_text().strip())
                os.kill(pid, 0)
                if urlf.is_file():
                    webbrowser.open(urlf.read_text().strip())
                    self.app._notice("console already running — opened in browser")
                    return
            except (ValueError, OSError):
                pass
        portf = Path(tempfile.gettempdir()) / f"ohmyllm-gui-{os.getpid()}.port"
        portf.unlink(missing_ok=True)
        try:
            child = subprocess.Popen(
                [sys.executable, "-m", "ohmyllm.bridge", "--port-file", str(portf)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, start_new_session=True,
            )
        except OSError as exc:
            self.app._notice(f"could not start console: {exc}")
            return
        pidf.write_text(str(child.pid))
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if portf.is_file():
                info = portf.read_text().strip()
                if len(info.split(" ")) == 2:
                    port, token = info.split(" ")
                    url = f"http://127.0.0.1:{port}/?t={token}"
                    urlf.write_text(url)
                    with contextlib.suppress(OSError):
                        (home / "gui-web.url").write_text(url)
                    webbrowser.open(url)
                    self.app._notice(f"console up → {url}")
                    return
            await asyncio.sleep(0.1)
        self.app._notice("console bridge did not come up in 8s — check oml doctor")

    def _slash(self, line: str) -> None:
        parts = line[1:].split(maxsplit=1)
        cmd = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""
        app = self.app
        if cmd in ("help", "?"):
            app.replace_screen("help")
        elif cmd == "model":
            if arg and self._set_model(arg):
                return
            app.replace_screen("models")
        elif cmd == "modelsettings":
            from ...backends.base import BackendName, ModelInfo
            from .modals import ModelSettingsModal

            key = app.svc.config.default_backend.value
            section = getattr(app.svc.config, key, None)
            name = arg or (getattr(section, "default_model", "") if section else "") or ""
            if not name:
                app.notify("no default model set — pick one with /model first", severity="warning")
            else:
                app.push_screen(ModelSettingsModal(ModelInfo(name=name, backend=BackendName(key))))
        elif cmd == "backend":
            if arg in {"ollama", "lmstudio", "mock"}:
                from ...config import BackendName

                app.svc.config.default_backend = BackendName(arg)
                app.svc.save_config()
                app._health_probe()
                app.notify(f"backend → {arg}")
            app.replace_screen("backend")
        elif cmd == "think" and arg.lower() in ("hide", "show"):
            cfg = app.svc.config.agent
            cfg.thinking_visible = arg.lower() == "show"
            app.svc.save_config()
            app.notify(f"live thinking {'shown' if cfg.thinking_visible else 'hidden'} (Ctrl+T toggles)")
        elif cmd == "think":
            cur = app.svc.config.generation.thinking
            want = arg.lower() if arg.lower() in ("on", "off") else ("off" if cur else "on")
            app.svc.config.generation.thinking = want == "on"
            app.svc.save_config()
            app.notify(f"thinking {'enabled' if want == 'on' else 'off'} — applies from the next turn")
        elif cmd == "ultrathink":
            cur = app.svc.config.generation.ultrathink
            want = arg.lower() if arg.lower() in ("on", "off") else ("off" if cur else "on")
            app.svc.config.generation.ultrathink = want == "on"
            if want == "on":
                app.svc.config.generation.thinking = True
            app.svc.save_config()
            app.notify(f"ultrathink {'ON — the agent will think this through properly' if want == 'on' else 'off'} · applies from the next turn")
        elif cmd == "mode":
            if arg:
                self._set_mode(arg)
            else:
                app.replace_screen("perms")
        elif cmd in ("memory", "soul"):
            app.replace_screen("memory")
        elif cmd == "doctor":
            app.replace_screen("doctor")
        elif cmd == "settings":
            app.replace_screen("settings")
        elif cmd == "clear" or cmd == "new":
            app.new_chat()
        elif cmd == "chats":
            app.replace_screen("chats")
        elif cmd == "rename":
            if app.session:
                title = arg or make_title(" ".join(str(m.get("content", "")) for m in app.session.messages if m.get("role") == "user"))
                app.svc.sessions.rename(app.session.id, title)
                app.session = app.svc.sessions.get(app.session.id) or app.session
                self._refresh_title()
                app.notify(f"renamed → {app.session.title}")
        elif cmd == "delete":
            self.run_worker(self._delete_current(), exclusive=False)
        elif cmd == "export":
            fmt = "json" if "json" in arg.lower() else "markdown"
            app._export(fmt)
        elif cmd == "remember":
            self._send(f"please remember that {arg}", memory_directive=True)
        elif cmd == "forget":
            self._send(f"please forget the following: {arg}", memory_directive=True)
        elif cmd in ("gui", "omlgui", "console"):
            self._launch_gui()
        elif cmd in ("quit", "exit"):
            self.run_worker(app.action_safe_quit(), exclusive=False)
        else:
            app.notify(f"unknown command /{cmd} — /help", severity="warning")

    def _set_model(self, name: str) -> bool:
        app = self.app
        key = app.svc.config.default_backend.value
        section = getattr(app.svc.config, key, None)
        if section is None or not hasattr(section, "default_model"):
            return False
        section.default_model = name
        app.svc.save_config()
        self._refresh_title()
        if f"{key}:{name}" not in app.svc.config.model_settings:
            from ...backends.base import BackendName, ModelInfo
            from .modals import ModelSettingsModal

            app.notify(f"{name} is new here — set its per-model settings")
            app.push_screen(ModelSettingsModal(ModelInfo(name=name, backend=BackendName(key))))
        else:
            app.notify(f"default model → {key}:{name}")
        return True

    def _set_mode(self, arg: str) -> None:
        from ...config import PermissionMode as PM

        app = self.app
        try:
            mode = PM(arg.lower())
        except ValueError:
            app.notify(f"unknown mode {arg!r}", severity="error")
            return

        async def _apply() -> None:
            phrase = None
            if mode is PM.FULL_ACCESS:
                phrase = await app.prompt(
                    f"[bold red]FULL ACCESS[/] — type exactly: {app.svc.full_access_phrase()}",
                    placeholder="type the confirmation phrase",
                )
            ok = app.svc.set_mode(mode, phrase=phrase)
            app.notify(
                f"permission mode → {mode.value}" if ok else "phrase mismatch — mode unchanged",
                severity="info" if ok else "error",
            )
            app.refresh_chrome()
            self._refresh_title()

        self.run_worker(_apply(), exclusive=False)

    async def _delete_current(self) -> None:
        app = self.app
        sess = app.session
        if not sess or sess.message_count == 0:
            app.new_chat()
            return
        export = await app.confirm(
            f"Delete chat '[b]{sess.title[:40]}[/]'?",
            "It moves to the trash folder (never hard-deleted). We can export it to markdown first.",
            danger=True, ok_label="Export + delete",
        )
        if not export:
            return
        from ...sessions.export import export as do_export

        path = do_export(app.svc.sessions, sess, "markdown")
        app.notify(f"exported → {path}")
        app.svc.sessions.delete(sess.id)
        app.new_chat()




def _who(name: str, text: str) -> str:
    return f"[bold {'cyan' if name == 'you' else 'magenta'}]{name} >[/] {text}"
