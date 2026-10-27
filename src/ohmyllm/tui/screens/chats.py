"""Chats browsing screen: search, filter, open, rename, pin, delete, export."""

from __future__ import annotations

from datetime import datetime

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import Button, DataTable, Input, Label, OptionList, Static, Switch
from textual.widgets._option_list import Option


def _ws_short(ws: str | None) -> str:
    """Home-relative, basename-preserving workspace label (no mid-path clipping)."""
    from pathlib import Path

    if not ws:
        return "—"
    home = str(Path.home())
    if ws.startswith(home):
        ws = "~" + ws[len(home):]
    return ws[-24:] if len(ws) <= 24 else "…" + ws[-23:]


class ChatsScreen(Screen):
    BINDINGS = [
        Binding("escape", "close", "Back"),
        Binding("enter", "open", "Open", priority=True),
        Binding("n", "new", "New chat"),
        Binding("r", "rename", "Rename"),
        Binding("p", "pin", "Pin"),
        Binding("x", "delete", "Delete"),
        Binding("e", "export_md", "Export .md"),
        Binding("j", "export_json", "Export .json"),
        Binding("t", "toggle_filter", "Pinned only"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("[b]💬 Chats[/] — enter open · n new · r rename · p pin · x delete · e/j export · t pinned", id="screen-title")
        with Horizontal(id="chats-filters"):
            yield Input(placeholder="search chats…", id="chat-search")
            yield Label(" pinned only:")
            yield Switch(id="only-pinned", value=False)
            yield Button("New chat", id="btn-new", variant="primary")
        yield DataTable(id="chats-table", cursor_type="row")
        yield OptionList(id="chats-preview", classes="detail-box")

    def on_mount(self) -> None:
        table = self.query_one("#chats-table", DataTable)
        # fixed widths that FIT the viewport: a horizontal overflow would
        # paint a fat scrollbar bar and clip the id column (user P0)
        table.add_column("pin", key="pin", width=3)
        table.add_column("title", key="title")
        table.add_column("updated", key="updated", width=16)
        table.add_column("backend", key="backend", width=8)
        table.add_column("model", key="model", width=12)
        table.add_column("msgs", key="msgs", width=4)
        table.add_column("id", key="id", width=12)
        self._fit_title()
        table.focus()
        self._rows: dict[tuple, str] = {}
        self.refresh_chats()
        self.set_interval(0.5, self._update_preview)

    def on_resize(self, event) -> None:  # noqa: ANN001
        self._fit_title()

    def _fit_title(self) -> None:
        try:
            table = self.query_one("#chats-table", DataTable)
        except NoMatches:
            return
        # 6 fixed cols (3+16+8+12+4+12=55) + 2 padding each (12) + vertical bar (1)
        # + own border (2) — the table has a solid frame now, not a band
        table.columns["title"].width = max(16, (table.size.width or 106) - 70)

    @work(exclusive=True)
    async def refresh_chats(self) -> None:
        app = self.app
        rows = app.svc.sessions.list()
        self._all = {s.id: s for s in rows}
        self._populate(rows)

    def _filtered(self):
        q = self.query_one("#chat-search", Input).value.strip().lower()
        only_pinned = self.query_one("#only-pinned", Switch).value
        sess = list(getattr(self, "_all", {}).values())
        sess.sort(key=lambda s: (s.pinned, s.updated_at), reverse=True)
        out = []
        for s in sess:
            if only_pinned and not s.pinned:
                continue
            if q:
                hay = (s.title + " " + " ".join(str(m.get("content", ""))[:400] for m in s.messages[-25:])).lower()
                if q not in hay:
                    continue
            out.append(s)
        return out

    def _populate(self, _rows=None) -> None:
        try:
            table = self.query_one("#chats-table", DataTable)
        except NoMatches:
            return
        table.clear()
        self._row_order: list[str] = []
        for s in self._filtered():
            when = datetime.fromtimestamp(s.updated_at).strftime("%Y-%m-%d %H:%M")
            table.add_row(
                "📌" if s.pinned else "", s.title[:46], when, s.backend or "—",
                (s.model or "—")[:12], str(s.message_count), s.id[:12],
            )
            self._row_order.append(s.id)
        if self._row_order:
            table.move_cursor(row=0)
        self._update_preview()

    def _selected_id(self) -> str | None:
        table = self.query_one("#chats-table", DataTable)
        if not table.row_count:
            return None
        row = table.cursor_coordinate.row
        if 0 <= row < len(getattr(self, "_row_order", [])):
            return self._row_order[row]
        return None

    @on(DataTable.RowSelected)
    def _row_selected(self) -> None:
        # fires on mouse click and on Enter over a row — open the chat
        self._open_selected()

    def _update_preview(self) -> None:
        try:
            opts = self.query_one("#chats-preview", OptionList)
        except NoMatches:
            return
        sid = self._selected_id()
        sess = self._all.get(sid) if sid else None
        opts.clear_options()
        if sess is None:
            msg = "no chats stored yet — press n" if not getattr(self, "_row_order", []) else "↑↓ select · enter or double-click opens"
            opts.add_option(Option(f"[dim]{msg}[/dim]", id="np"))
            return
        opts.add_option(Option(f"[dim]ws:[/] {_ws_short(sess.workspace)}", id="ws"))
        for i, m in enumerate(sess.messages[-3:]):
            role = {"user": "you", "assistant": "model", "tool": "tool"}.get(str(m.get("role")), "?")
            opts.add_option(Option(f"[dim]{role}:[/] {str(m.get('content', ''))[:110]}", id=f"p{i}"))
        opts.add_option(Option(f"[dim]preview of {sess.preview}[/dim]", id="last"))

    def action_open(self) -> None:
        self._open_selected()

    def _open_selected(self) -> None:
        sid = self._selected_id()
        if sid and self.app.load_session(sid):
            self.app.notify("chat resumed")

    def action_new(self) -> None:
        self.app.new_chat()

    @on(Button.Pressed, "#btn-new")
    def _new_btn(self) -> None:
        self.action_new()

    def action_rename(self) -> None:
        sid = self._selected_id()
        if not sid:
            return
        sess = self._all[sid]

        async def _r() -> None:
            title = await self.app.prompt(f"Rename chat (current: {sess.title})", "new title", initial=sess.title)
            if title:
                self.app.svc.sessions.rename(sid, title)
                self.refresh_chats()

        self.run_worker(_r(), exclusive=False)

    def action_pin(self) -> None:
        sid = self._selected_id()
        if sid:
            self.app.svc.sessions.toggle_pin(sid)
            self.refresh_chats()

    def action_delete(self) -> None:
        sid = self._selected_id()
        if not sid:
            return
        sess = self._all[sid]

        async def _d() -> None:
            export = await self.app.confirm(
                f"[b]Delete[/] '{sess.title[:44]}' ({sess.message_count} msgs)?",
                "Deleted chats go to the trash folder and can be restored manually. We export markdown first.",
                danger=True,
                ok_label="Export + Delete",
            )
            if not export:
                return
            from ...sessions.export import export as do_export

            path = do_export(self.app.svc.sessions, sess, "markdown")
            self.app.svc.sessions.delete(sid)
            self.app.notify(f"deleted (exported → {path})")
            self.refresh_chats()

        self.run_worker(_d(), exclusive=False)

    def action_export_md(self) -> None:
        self._export("markdown")

    def action_export_json(self) -> None:
        self._export("json")

    def _export(self, fmt: str) -> None:
        sid = self._selected_id()
        if not sid:
            return
        from ...sessions.export import export

        sess = self.app.svc.sessions.get(sid)
        if sess:
            path = export(self.app.svc.sessions, sess, fmt)
            self.app.notify(f"exported → {path}")
            self.refresh_chats()

    def action_toggle_filter(self) -> None:
        sw = self.query_one("#only-pinned", Switch)
        sw.value = not sw.value
        self._populate()

    def action_close(self) -> None:
        self.app.replace_screen("chat")

    @on(Switch.Changed, "#only-pinned")
    def _filter_changed(self) -> None:
        self._populate()

    @on(Input.Changed, "#chat-search")
    def _search_changed(self) -> None:
        self._populate()
