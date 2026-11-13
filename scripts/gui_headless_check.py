#!/usr/bin/env python3
"""Headless GUI verification for the OhMyLLM web GUI (Task #3 deliverable).

Standalone on purpose — NOT collected by pytest (it spawns a real browser).
Run directly:  python3 scripts/gui_headless_check.py

Safety contract:
  * fully isolated temp HOME / OMLLM_HOME / workspace, set BEFORE importing
    ohmyllm — never touches the user's real state or any live service;
  * synthetic fake backend (MockBackend subclass, e2e_check.py pattern);
  * system Chromium at /usr/bin/chromium, headless, SANDBOX RETAINED
    (no --no-sandbox), fresh temp user-data-dir per viewport width;
  * the only URL ever opened is the genuine local bridge on 127.0.0.1,
    with the per-process token from the private discovery port file.

Requires: playwright (sync API) + /usr/bin/chromium. Exits non-zero on any
failed assertion; the bridge is always shut down in `finally`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

RT = Path(__file__).resolve().parents[1]
VALIDATION = RT / ".validation"
CHROMIUM = "/usr/bin/chromium"
WIDTHS = {"narrow": 420, "laptop": 1280, "desktop": 1720}

# --------------------------------------------------------------------------
# Isolation MUST happen before any ohmyllm import (paths caches env at import).
# --------------------------------------------------------------------------
TMP = Path(tempfile.mkdtemp(prefix="omllm-gui-check-"))
HOME = TMP / "home"
WS = TMP / "ws"
HOME.mkdir(mode=0o700)  # discovery parent must be a real, owner-only dir
WS.mkdir()
os.environ["HOME"] = str(HOME)
os.environ["OMLLM_HOME"] = str(HOME)
os.environ["OMLLM_WORKSPACE"] = str(WS)
sys.path.insert(0, str(RT / "src"))

from ohmyllm import bridge as B  # noqa: E402
from ohmyllm import paths  # noqa: E402
from ohmyllm.backends import registry  # noqa: E402
from ohmyllm.backends.mock import MockBackend  # noqa: E402
from ohmyllm.config import Config, ConfigStore, PermissionMode  # noqa: E402
from ohmyllm.config.models import BackendName  # noqa: E402


class FakeBackend(MockBackend):
    """Deterministic scripted backend: fast tokens, a real shell-approval
    trigger ("run ls" — ls is on the sandbox allowlist, so the policy asks
    for a fresh one-shot approval), and a slow reply to open a steer window."""

    delay = 0.04

    def _plan_calls(self, user_text, has_tools):
        calls = super()._plan_calls(user_text, has_tools)
        if has_tools and re.search(r"\brun ls\b", user_text, re.IGNORECASE):
            calls.append({
                "id": "call_ls", "type": "function",
                "function": {"name": "run_command", "arguments": {"command": "ls"}},
            })
        return calls

    def _plain_reply(self, user_text):
        if re.search(r"\bslow\b", user_text, re.IGNORECASE):
            return "Slow turn for steering verification. " * 30
        return super()._plain_reply(user_text)


registry._CLASSES[BackendName.MOCK] = FakeBackend

FAILURES: list[str] = []


def check(name: str, ok: bool) -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {name}", flush=True)
    if not ok:
        FAILURES.append(name)
    return ok


def flow(name: str, fn) -> None:
    """Run one verification flow; an exception is a FAIL, never a crash."""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        check(f"{name} (no exception: {type(exc).__name__}: {str(exc)[:600]})", False)


class Client:
    """Minimal token-authenticated bridge client (conftest.py pattern)."""

    def __init__(self, port: int, token: str) -> None:
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.token = token

    def req(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method)
        r.add_header("Content-Type", "application/json")
        r.add_header("X-OMLLM-Token", self.token)
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                return json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            return {"__status__": e.code, **json.loads(e.read() or b"{}")}

    def get(self, p):
        return self.req("GET", p)

    def post(self, p, body=None):
        return self.req("POST", p, body or {})


def boot_bridge():
    """Start the real bridge against the isolated home with the fake backend."""
    paths.ensure_global_dirs()
    cfg = Config()
    cfg.default_backend = "mock"
    cfg.permission_mode = PermissionMode.SANDBOXED
    cfg.first_run_completed = True
    ConfigStore().save(cfg)

    stop = threading.Event()
    port_file = HOME / "p.port"

    def run() -> None:
        B.serve(workspace=str(WS), port=0, write_port_file=str(port_file), stop=stop)

    th = threading.Thread(target=run, daemon=True)
    th.start()
    info = None
    deadline = time.time() + 60
    while time.time() < deadline:
        if port_file.exists():
            try:
                info = json.loads(port_file.read_text())
                break
            except (ValueError, OSError):
                pass
        if not th.is_alive():
            break
        time.sleep(0.05)
    if info is None:
        stop.set()
        th.join(timeout=10)
        raise RuntimeError("bridge never wrote its discovery port file")
    client = Client(info["port"], info["token"])
    for _ in range(200):
        try:
            r = urllib.request.urlopen(client.base + "/api/health", timeout=5)
            if json.loads(r.read()).get("ok"):
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.05)
    else:
        stop.set()
        th.join(timeout=10)
        raise RuntimeError("bridge health never came up")
    return client, stop, th


# --------------------------------------------------------------------------
# Browser helpers — sandbox retained, fresh profile per width.
# --------------------------------------------------------------------------
def open_gui(pw, port: int, token: str, width: int):
    profile = tempfile.mkdtemp(prefix=f"omllm-gui-profile-{width}-", dir=str(TMP))
    ctx = pw.chromium.launch_persistent_context(
        profile,
        executable_path=CHROMIUM,
        headless=True,
        viewport={"width": width, "height": 900},
        # NO --no-sandbox: the Chromium sandbox stays enabled.
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.set_default_timeout(20_000)
    page.on("dialog", lambda d: d.accept())  # beforeunload must never hang us
    page.goto(f"http://127.0.0.1:{port}/#token={token}", wait_until="domcontentloaded")
    return ctx, page


def wait_boot(page) -> None:
    page.wait_for_function(
        "() => !document.getElementById('conn-dot').classList.contains('off')"
        " && document.getElementById('run-state').textContent === 'Ready'",
        timeout=30_000,
    )


def wait_ready(page) -> None:
    page.wait_for_function(
        "() => document.getElementById('run-state').textContent === 'Ready'"
        " && document.getElementById('cancel').hidden",
        timeout=60_000,
    )


def send(page, text: str) -> None:
    page.fill("#input", text)
    page.press("#input", "Enter")


def assistant_text(page) -> str:
    return page.eval_on_selector_all(
        "#stream .msg.assistant .content", "els => els.map(e => e.textContent).join('\\n')")


def approval_dom_probe(page) -> dict:
    """Pin the shell-consent UX with hard DOM evidence: modal text plus each
    button's hidden attribute, computed display, and Playwright visibility."""
    return page.evaluate("""() => {
        const m = document.getElementById('approval-modal');
        const btns = {};
        for (const id of ['ap-allow', 'ap-session', 'ap-always', 'ap-deny']) {
            const b = document.getElementById(id);
            btns[id] = {hiddenAttr: b.hidden,
                        display: getComputedStyle(b).display,
                        rects: b.getClientRects().length};
        }
        return {text: m.innerText.replace(/\\s+/g, ' ').trim().slice(0, 400), buttons: btns};
    }""")


def dismiss_stray_modals(page) -> None:
    """Defensive cleanup between flows (Escape is fail-closed for approvals)."""
    for _ in range(4):
        if not page.evaluate("() => !!document.querySelector('.modal:not([hidden])')"):
            return
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)


# --------------------------------------------------------------------------
# Full interaction flows (laptop width).
# --------------------------------------------------------------------------
def flows_laptop(page) -> None:
    def f_boot():
        wait_boot(page)
        check("boot: bridge connected, run-state Ready", True)
        check("boot: composer visible with required placeholder",
              page.is_visible("#composer")
              and page.get_attribute("#input", "placeholder")
              == "Ask anything, / for commands, @ for context…")
        check("boot: composer textarea has aria-label",
              bool(page.get_attribute("#input", "aria-label")))

    def f_ime():
        page.fill("#input", "にほんご")
        page.evaluate("""() => {
            const el = document.getElementById('input');
            el.dispatchEvent(new CompositionEvent('compositionstart', {bubbles: true}));
            el.dispatchEvent(new KeyboardEvent('keydown',
                {key: 'Enter', code: 'Enter', isComposing: true, bubbles: true, cancelable: true}));
            el.dispatchEvent(new CompositionEvent('compositionend', {bubbles: true}));
        }""")
        page.wait_for_timeout(400)
        # the seeded session already holds one user bubble — assert the IME
        # text specifically never became a message
        check("IME: composing Enter does not submit",
              page.input_value("#input") == "にほんご"
              and page.eval_on_selector_all(
                  "#stream .msg.user",
                  "els => els.filter(e => e.textContent.includes('にほんご')).length") == 0)
        page.fill("#input", "")

    def f_hints():
        page.fill("#input", "/")
        page.wait_for_selector("#slash-hint:not([hidden])", timeout=5000)
        check("slash palette hint opens on '/'", True)
        page.fill("#input", "@")
        page.wait_for_selector("#at-hint:not([hidden])", timeout=5000)
        check("@-ref autocomplete opens on '@'", True)
        page.fill("#input", "")

    def f_stream():
        send(page, "hello")
        page.wait_for_function(
            "() => document.getElementById('run-state').textContent === 'Ready'"
            " && document.querySelector('#stream .msg.assistant')", timeout=30_000)
        check("streamed assistant reply arrives", "mock model" in assistant_text(page))

    def f_approval_allow():
        send(page, "run ls")
        page.wait_for_selector("#approval-modal:not([hidden])", timeout=30_000)
        probe = approval_dom_probe(page)
        print("PROBE approval modal: " + json.dumps(probe, ensure_ascii=False), flush=True)
        b = probe["buttons"]
        shell_ok = ("run_command" in probe["text"]
                    and b["ap-allow"]["rects"] > 0 and b["ap-deny"]["rects"] > 0
                    and b["ap-session"]["hiddenAttr"] and b["ap-session"]["display"] == "none"
                    and b["ap-always"]["hiddenAttr"] and b["ap-always"]["display"] == "none")
        check("shell approval: only Allow once / Deny offered",
              shell_ok and page.is_visible("#ap-allow") and page.is_visible("#ap-deny")
              and page.is_hidden("#ap-session") and page.is_hidden("#ap-always"))
        page.click("#ap-allow")
        wait_ready(page)
        st = page.evaluate(EMPTY_AGENT_JS)
        check("allow: one approval card, zero empty agent rows",
              st["cards"] == 1 and st["uniq"] == 1 and st["empty"] == 0 and st["echoes"] == 0)
        check("one-shot allow: tool ran and turn completed",
              page.eval_on_selector_all("#stream .tool-entry", "e => e.length") >= 1
              and "Done" in assistant_text(page))

    def f_approval_deny():
        send(page, "run ls")
        page.wait_for_selector("#approval-modal:not([hidden])", timeout=30_000)
        page.click("#ap-deny")
        wait_ready(page)
        check("deny path: card reads 'denied by you'",
              "denied by you" in page.text_content("#stream"))
        st = page.evaluate(EMPTY_AGENT_JS)
        check("deny: two unique approval cards, zero empty agent rows",
              st["cards"] == 2 and st["uniq"] == 2 and st["empty"] == 0 and st["echoes"] == 0)

    def f_steer():
        send(page, "slow steer window")
        page.wait_for_selector("#cancel:not([hidden])", timeout=15_000)
        send(page, "focus on the tests please")
        page.wait_for_selector("#steer-chip:not([hidden])", timeout=10_000)
        check("steer chip shown while turn busy",
              "steer queued" in page.text_content("#steer-chip"))
        wait_ready(page)

    def f_draft_switch():
        # the app guards new-chat/switch while a turn streams (ported UX) —
        # always await turn end first instead of racing the guard
        wait_ready(page)
        page.fill("#input", "draft alpha")
        page.click("#btn-new")
        page.wait_for_function("() => document.getElementById('input').value === ''", timeout=15_000)
        page.fill("#input", "draft beta")
        # actions now live in a reserved in-flow slot (visibility reveal), so
        # the title hit area is never overlapped; the left-biased click is
        # belt-and-braces. The click sits inside the try so a click failure
        # still emits the PROBE diagnostics below.
        try:
            page.click("#sb-list .sb-row:has-text('quartz') .title",
                       position={"x": 8, "y": 8})
            page.wait_for_function("() => document.getElementById('input').value === 'draft alpha'",
                                   timeout=15_000)
        except Exception:
            print("PROBE draft-switch: " + json.dumps(page.evaluate("""() => ({
                input: document.getElementById('input').value,
                modal: (document.querySelector('.modal:not([hidden])') || {}).id || null,
                toasts: document.getElementById('toasts').innerText.slice(0, 200),
                rows: Array.from(document.querySelectorAll('#sb-list .sb-row .title')).map(e => e.textContent)
            })""")), flush=True)
            raise
        check("draft persists across session switch", True)
        st = page.evaluate(EMPTY_AGENT_JS)
        check("replay: empty placeholders skipped, echo is one decided card",
              st["empty"] == 0 and st["echoes"] == 0 and st["cards"] == 1)
        page.fill("#input", "")

    def f_draft_reject():
        page.route("**/api/message", lambda route: route.fulfill(
            status=400, content_type="application/json",
            body=json.dumps({"error": "synthetic rejection for draft test"})))
        try:
            send(page, "rejected draft")
            page.wait_for_function("() => document.getElementById('input').value === 'rejected draft'",
                                   timeout=15_000)
            check("rejected submission restores draft, no phantom bubble",
                  page.eval_on_selector_all(
                      "#stream .msg.user",
                      "els => els.filter(e => e.textContent.includes('rejected draft')).length") == 0)
        finally:
            page.unroute("**/api/message")

    def f_search():
        page.fill("#sb-search", "quartz")
        page.wait_for_selector("#sb-list .sb-row:has-text('quartz')", timeout=10_000)
        check("sidebar search returns seeded session", True)
        page.fill("#sb-search", "")
        page.wait_for_timeout(500)

    def f_dirty_settings():
        page.keyboard.press("Control+g")
        page.wait_for_selector("#view-settings:not([hidden])", timeout=10_000)
        page.wait_for_selector("#set-generation-temperature", timeout=10_000)
        page.fill("#set-generation-temperature", "0.9")
        page.keyboard.press("Control+h")
        page.wait_for_selector("#confirm-modal:not([hidden])", timeout=10_000)
        check("dirty settings editor warns on navigation", True)
        page.click("#cf-cancel")
        page.wait_for_selector("#confirm-modal", state="hidden", timeout=5000)
        check("cancel keeps you on the dirty editor",
              page.is_visible("#view-settings") and page.input_value("#set-generation-temperature") == "0.9")
        page.fill("#set-generation-temperature", "0.7")
        page.keyboard.press("Control+h")
        page.wait_for_selector("#view-help:not([hidden])", timeout=10_000)
        check("clean editor navigates without warning", True)
        # back to chat through the real view menu (no state hacks)
        page.click("#btn-menu")
        page.wait_for_selector("#view-menu:not([hidden])", timeout=5000)
        page.click("#view-menu [data-view='chat']")
        page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)

    def f_focus_trap():
        page.click("#chip-mode")
        page.wait_for_selector("#mode-modal:not([hidden])", timeout=10_000)
        inside = True
        for _ in range(8):
            page.keyboard.press("Tab")
            inside = inside and page.evaluate(
                "() => document.getElementById('mode-modal').contains(document.activeElement)")
        check("modal focus trap: Tab cycles inside the modal", inside)
        page.keyboard.press("Escape")
        page.wait_for_selector("#mode-modal", state="hidden", timeout=5000)
        check("Escape closes modal and restores focus to opener",
              page.evaluate("() => document.activeElement && document.activeElement.id") == "chip-mode")

    for name, fn in [
        ("boot", f_boot), ("ime", f_ime), ("hints", f_hints), ("stream", f_stream),
        ("approval-allow", f_approval_allow), ("approval-deny", f_approval_deny),
        ("steer", f_steer), ("draft-switch", f_draft_switch), ("draft-reject", f_draft_reject),
        ("search", f_search), ("dirty-settings", f_dirty_settings), ("focus-trap", f_focus_trap),
    ]:
        dismiss_stray_modals(page)
        flow(name, fn)


# transcript hygiene probe (task #23): zero empty agent rows, one approval
# card per request id, and no approval echo leaking into agent prose
EMPTY_AGENT_JS = """() => {
    const empty = Array.from(document.querySelectorAll('#stream .msg.assistant'))
        .filter(e => !((e.querySelector('.content') || {}).textContent || '').trim()).length;
    const cards = Array.from(document.querySelectorAll('#stream .app-card'));
    const aids = cards.map(c => c.dataset.aid || '');
    const echoes = Array.from(document.querySelectorAll('#stream .msg.assistant .content'))
        .filter(e => /^\\[(asked to run|attempted) /.test((e.textContent || '').trim())).length;
    return {empty, cards: cards.length, uniq: new Set(aids).size, echoes};
}"""


def flows_responsive(page, width: int) -> None:
    def f():
        wait_boot(page)
        check(f"{width}px: boot + health", True)
        check(f"{width}px: composer visible", page.is_visible("#composer"))
        if width <= 900:
            check(f"{width}px: sidebar starts closed (overlay mode)",
                  page.evaluate("() => !document.getElementById('sidebar').classList.contains('open')"))
            page.click("#btn-sidebar")
            page.wait_for_selector("#sidebar.open", timeout=5000)
            check(f"{width}px: sidebar toggles open as overlay", True)
            page.click("#btn-sidebar")
            page.wait_for_timeout(200)
        else:
            check(f"{width}px: sidebar visible by default", page.is_visible("#sidebar"))
        check(f"{width}px: no horizontal overflow",
              page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1"))
    flow(f"responsive-{width}", f)


def main() -> int:
    if not Path(CHROMIUM).exists():
        print(f"FAIL  chromium not found at {CHROMIUM}")
        return 1
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("FAIL  playwright is not installed (pip install playwright)")
        return 1

    VALIDATION.mkdir(parents=True, exist_ok=True)
    client, stop, th = boot_bridge()
    shot = {"narrow": "gui-narrow.png", "laptop": "gui-laptop.png", "desktop": "gui-desktop.png"}
    contexts = []
    try:
        # Seed a searchable session; it becomes the bridge's active session.
        seeded = client.post("/api/chats", {"first_message": "quartz falcon migration notes"})
        check("seed session created via /api/chats", bool(seeded.get("id")))

        with sync_playwright() as pw:
            for label, width in WIDTHS.items():
                ctx = page = None
                try:
                    ctx, page = open_gui(pw, client.port, client.token, width)
                    contexts.append(ctx)
                    if label == "laptop":
                        flows_laptop(page)
                    else:
                        flows_responsive(page, width)
                    dismiss_stray_modals(page)
                    # capture hygiene: let entrance animations settle (view
                    # switches replay them) and drop transient test toasts so
                    # the progress screenshots show the resting UI
                    page.wait_for_timeout(600)
                    page.evaluate("() => { document.getElementById('toasts').innerHTML = ''; }")
                    # bottom-middle contract: composer center must match the
                    # main chat area center (sidebar excluded), sit anchored
                    # at its bottom, and never spill past the main area edges
                    geo = page.evaluate("""() => {
                        const c = document.getElementById('composer').getBoundingClientRect();
                        const m = document.getElementById('main').getBoundingClientRect();
                        return {cx: c.left + c.width / 2, mx: m.left + m.width / 2,
                                cb: c.bottom, mb: m.bottom,
                                cl: c.left, cr: c.right, ml: m.left, mr: m.right};
                    }""")
                    dx = abs(geo["cx"] - geo["mx"])
                    check(f"{width}px: composer bottom-centered in main (dx={dx:.1f}px)",
                          dx <= 2 and geo["mb"] - 40 <= geo["cb"] <= geo["mb"]
                          and geo["cl"] >= geo["ml"] - 1 and geo["cr"] <= geo["mr"] + 1)
                    page.screenshot(path=str(VALIDATION / shot[label]), full_page=False)
                    check(f"screenshot saved: .validation/{shot[label]}",
                          (VALIDATION / shot[label]).exists())
                except Exception as exc:  # noqa: BLE001
                    check(f"{label} width run (no exception: {type(exc).__name__}: {str(exc)[:600]})", False)
                finally:
                    if ctx is not None:
                        try:
                            ctx.close()
                        except Exception:  # noqa: BLE001
                            pass
    finally:
        stop.set()
        th.join(timeout=15)
        check("bridge shut down cleanly", not th.is_alive())
        shutil.rmtree(TMP, ignore_errors=True)

    print(f"\n{len(FAILURES)} failure(s)" if FAILURES else "\nALL CHECKS PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())

