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
# R3 U9: 768px is the overlay breakpoint (narrowMQ = max-width:900px) — the
# tablet layout must be exercised, not just phone and desktop widths.
WIDTHS = {"narrow": 420, "tiny": 320, "tablet": 768, "laptop": 1280, "desktop": 1720, "wide": 2560}

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
from ohmyllm.sessions.store import SessionStore  # noqa: E402


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
# R3 U9: a bare "ALL CHECKS PASSED" hides how much was actually asserted, so
# every check() is counted and the closing banner reports real numbers.
COUNTS = {"pass": 0, "fail": 0}


def check(name: str, ok: bool) -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {name}", flush=True)
    if ok:
        COUNTS["pass"] += 1
    else:
        COUNTS["fail"] += 1
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
        # K24: explicitly retain the Chromium sandbox. Playwright defaults to
        # chromium_sandbox=False, which silently passes --no-sandbox; the
        # setuid chrome-sandbox helper / unprivileged userns make True work.
        chromium_sandbox=True,
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.set_default_timeout(20_000)
    page.on("dialog", lambda d: d.accept())  # beforeunload must never hang us
    page.goto(f"http://127.0.0.1:{port}/#token={token}", wait_until="domcontentloaded")
    return ctx, page, profile


def sandbox_proof(profile: str) -> bool:
    """K24: prove the browser for this unique profile runs WITH the sandbox.

    Scan /proc for the process whose cmdline carries our --user-data-dir and
    assert it was found and never received --no-sandbox.
    """
    needle = profile.encode()
    found = False
    disabled = False
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            cmd = (p / "cmdline").read_bytes()
        except OSError:
            continue
        if needle in cmd:
            found = True
            if b"--no-sandbox" in cmd:
                disabled = True
    return found and not disabled


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
def flows_laptop(page, client) -> None:
    def f_boot():
        wait_boot(page)
        boot = page.evaluate("""() => ({
            off: document.getElementById('conn-dot').classList.contains('off'),
            rs: document.getElementById('run-state').textContent,
            status: !!state.status,
        })""")
        check("boot: bridge connected, run-state Ready",
              not boot["off"] and boot["rs"] == "Ready" and boot["status"])
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
        check("slash palette hint opens on '/'",
              page.evaluate("() => !slashHint.hidden && slashMatches.length > 0"))
        page.fill("#input", "@")
        page.wait_for_selector("#at-hint:not([hidden])", timeout=5000)
        check("@-ref autocomplete opens on '@'",
              page.evaluate("() => !atHint.hidden && atMatches.length > 0"))
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
        # task #30: the live THINK/step row must never render a bare `undefined`
        check("no 'undefined' in transcript after a tool turn",
              "undefined" not in page.text_content("#stream"))

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
        # task #30: a playful rotating verb chip runs alongside the real step
        # info for the whole busy window, independent of thinking events
        page.wait_for_selector("#run-verb:not([hidden])", timeout=5_000)
        check("live turn: verb chip visible", page.is_visible("#run-verb"))
        verbs = {page.text_content("#run-verb")}
        send(page, "focus on the tests please")
        page.wait_for_selector("#steer-chip:not([hidden])", timeout=10_000)
        check("steer chip shown while turn busy",
              "steer queued" in page.text_content("#steer-chip"))
        # the slow reply streams ~6s and the verb window is 3.5s, so a live
        # turn must surface at least two distinct verbs
        deadline = time.time() + 9
        while time.time() < deadline and len(verbs) < 2:
            page.wait_for_timeout(400)
            if page.is_visible("#run-verb"):
                verbs.add(page.text_content("#run-verb"))
            if page.evaluate("() => document.getElementById('cancel').hidden"):
                break
        check("verb rotates during a live turn", len(verbs) >= 2)
        check("verb chip never reads 'undefined'",
              all("undefined" not in v for v in verbs))
        check("no 'undefined' in transcript mid-turn",
              "undefined" not in page.text_content("#stream"))
        wait_ready(page)
        check("verb chip hidden when idle", page.is_hidden("#run-verb"))

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
        check("draft persists across session switch",
              page.input_value("#input") == "draft alpha"
              and page.evaluate("() => state.drafts.get(state.session)") == "draft alpha")
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
        # R3 U9: real assertion (was a hardcoded pass). The input handler is
        # debounced 250ms and the UNFILTERED list already contains the seeded
        # row, so waiting for the first matching row races the filtered render.
        # Wait until every rendered row actually matches the query instead.
        page.wait_for_function(
            """() => {
                const rows = Array.from(document.querySelectorAll('#sb-list .sb-row'));
                return rows.length > 0 && rows.every(r => /quartz/i.test(r.textContent));
            }""", timeout=10_000)
        rows = page.evaluate("""() => Array.from(document.querySelectorAll('#sb-list .sb-row'))
            .map(r => ({title: r.querySelector('.title').textContent, text: r.textContent}))""")
        # FTS matches message bodies too, so a hit may carry the query only in
        # its snippet; the seeded session must still be findable by title.
        check("sidebar search returns seeded session",
              len(rows) > 0
              and all("quartz" in r["text"].lower() for r in rows)
              and any("quartz" in r["title"].lower() for r in rows))
        page.fill("#sb-search", "")
        page.wait_for_timeout(500)

    def f_dirty_settings():
        page.keyboard.press("Control+g")
        page.wait_for_selector("#view-settings:not([hidden])", timeout=10_000)
        page.wait_for_selector("#set-generation-temperature", timeout=10_000)
        page.fill("#set-generation-temperature", "0.9")
        page.keyboard.press("Control+h")
        page.wait_for_selector("#confirm-modal:not([hidden])", timeout=10_000)
        check("dirty settings editor warns on navigation",
              page.is_visible("#confirm-modal")
              and page.evaluate("() => !!state.settingsDirty")
              and page.is_visible("#view-settings"))
        page.click("#cf-cancel")
        page.wait_for_selector("#confirm-modal", state="hidden", timeout=5000)
        check("cancel keeps you on the dirty editor",
              page.is_visible("#view-settings") and page.input_value("#set-generation-temperature") == "0.9")
        page.fill("#set-generation-temperature", "0.7")
        page.keyboard.press("Control+h")
        page.wait_for_selector("#view-help:not([hidden])", timeout=10_000)
        check("clean editor navigates without warning",
              page.is_visible("#view-help") and page.is_hidden("#confirm-modal")
              and page.evaluate("() => !state.settingsDirty"))
        # back to chat through the persistent left rail (task #29 replaced
        # the old top-bar view menu with the rail — same real navigation)
        page.click("#rail [data-view='chat']")
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

    # ---------------- task #29: themed controls, rail, palette, market ----
    def f_listbox():
        check("zero native <select> elements anywhere",
              page.evaluate("() => document.querySelectorAll('select').length") == 0)
        page.wait_for_timeout(300)  # let any prior-flow settling finish
        rect_js = ("() => { const r = document.getElementById('composer').getBoundingClientRect();"
                   " return [r.left, r.top, r.width, r.height]; }")
        before = page.evaluate(rect_js)
        page.click("#sel-model .dd-btn")
        page.wait_for_selector("#sel-model .dd-pop:not([hidden])", timeout=5_000)
        after = page.evaluate(rect_js)
        delta = max(abs(a - b) for a, b in zip(before, after, strict=True))
        # the popup is out-of-flow (absolute), so opening it must not move the
        # composer; a real in-flow popup would shift it by tens of px
        abs_pos = page.evaluate(
            "() => getComputedStyle(document.querySelector('#sel-model .dd-pop')).position") == "absolute"
        print(f"PROBE dropdown layout: delta={delta:.3f}px popupAbsolute={abs_pos}", flush=True)
        check("dropdown opens without layout shift", abs_pos and delta <= 1.0)
        check("composer dropdown flips up (stays on screen)",
              page.evaluate("() => document.getElementById('sel-model').classList.contains('up')"))
        check("listbox ARIA: combobox + listbox + options",
              page.evaluate("""() => {
                  const b = document.querySelector('#sel-model .dd-btn');
                  const p = document.querySelector('#sel-model .dd-pop');
                  return b.getAttribute('role') === 'combobox'
                      && b.getAttribute('aria-expanded') === 'true'
                      && p.getAttribute('role') === 'listbox'
                      && p.querySelectorAll('[role="option"]').length >= 3;
              }"""))
        opts = page.evaluate("() => document.getElementById('sel-model').options.map(o => o.value)")
        check("model options populated from the backend", "mock-tool-7b" in opts)
        orig = page.evaluate("() => document.getElementById('sel-model').value")
        page.keyboard.type("mock-t", delay=60)
        page.keyboard.press("Enter")
        page.wait_for_function("() => document.getElementById('sel-model').value === 'mock-tool-7b'", timeout=5_000)
        check("type-ahead + Enter selects a model",
              page.evaluate("""() => {
                  const el = document.getElementById('sel-model');
                  const sel = el.querySelectorAll('[role="option"][aria-selected="true"]');
                  return el.value === 'mock-tool-7b' && sel.length === 1
                      && sel[0].querySelector('.dd-opt-label').textContent
                         === el.querySelector('.dd-label').textContent;
              }"""))
        page.wait_for_function("() => state.status && state.status.model === 'mock-tool-7b'", timeout=10_000)
        check("selection propagates to the bridge",
              page.evaluate("() => state.status && state.status.model") == "mock-tool-7b"
              and client.get("/api/status").get("model") == "mock-tool-7b")
        check("popup closes after selection",
              page.evaluate("() => document.querySelector('#sel-model .dd-pop').hidden"))
        page.click("#sel-model .dd-btn")
        page.wait_for_selector("#sel-model .dd-pop:not([hidden])", timeout=5_000)
        page.keyboard.type(orig, delay=40)
        page.keyboard.press("Enter")
        page.wait_for_function("(m) => state.status && state.status.model === m", arg=orig, timeout=10_000)
        check("original model restored via type-ahead",
              page.evaluate("() => document.getElementById('sel-model').value") == orig
              and client.get("/api/status").get("model") == orig)
        ev = page.evaluate("() => document.getElementById('sel-effort').value")
        page.click("#sel-effort .dd-btn")
        page.wait_for_selector("#sel-effort .dd-pop:not([hidden])", timeout=5_000)
        page.keyboard.press("Escape")
        check("Escape closes the popup, value unchanged",
              page.evaluate("() => document.querySelector('#sel-effort .dd-pop').hidden")
              and page.evaluate("() => document.getElementById('sel-effort').value") == ev)

    def f_rail():
        check("rail: 9 view buttons + palette shortcut",
              page.evaluate("() => document.querySelectorAll('#rail .rail-btn[data-view]').length") == 9
              and page.is_visible("#rail-palette"))
        ok = True
        for v in ["models", "providers", "market", "backend", "memory", "doctor", "settings", "help", "chat"]:
            page.click(f"#rail [data-view='{v}']")
            page.wait_for_selector(f"#view-{v}:not([hidden])", timeout=10_000)
            ok = ok and page.evaluate(
                "(v) => document.querySelector(`#rail [data-view='${v}']`).classList.contains('active')", v)
        check("rail switches all 9 views, active state follows", ok)

    def f_palette_rows():
        page.click("#rail-palette")
        page.wait_for_selector("#palette-modal:not([hidden])", timeout=5_000)
        page.wait_for_selector("#palette-list li", timeout=5_000)
        n = page.eval_on_selector_all("#palette-list li", "els => els.length")
        check("palette lists the full command set (>= 20 rows)", n >= 20)
        bad = page.evaluate("""() => Array.from(document.querySelectorAll('#palette-list li'))
            .filter(li => li.scrollHeight > (parseFloat(getComputedStyle(li).lineHeight) || 20) * 1.5)
            .map(li => li.textContent.slice(0, 40))""")
        check("every palette row stays single-line", not bad)
        page.fill("#palette-input", "/help")
        page.wait_for_timeout(250)
        check("the '/help' row no longer wraps to two lines",
              page.evaluate("""() => {
                  const li = document.querySelector('#palette-list li');
                  return !!li && li.scrollHeight <= (parseFloat(getComputedStyle(li).lineHeight) || 20) * 1.5;
              }"""))
        page.keyboard.press("Escape")
        page.wait_for_selector("#palette-modal", state="hidden", timeout=5_000)

    def f_market_cards():
        page.click("#rail [data-view='market']")
        page.wait_for_selector("#view-market:not([hidden])", timeout=10_000)
        page.wait_for_selector(".market-card", timeout=15_000)
        spreads = page.evaluate("""() => {
            const byRow = {};
            for (const c of document.querySelectorAll('.market-card')) {
                const r = c.getBoundingClientRect();
                const k = Math.round(r.top);
                (byRow[k] = byRow[k] || []).push(r.height);
            }
            return Object.values(byRow).map(hs => Math.max(...hs) - Math.min(...hs));
        }""")
        check("market: equal card heights within each row",
              bool(spreads) and all(d <= 1 for d in spreads))
        # ellipsis is visual only (scrollWidth still reports full text width),
        # so pin the computed clipping style plus the fit inside the card
        repos = page.evaluate("""() => Array.from(document.querySelectorAll('.mc-repo')).map(e => {
            const cs = getComputedStyle(e);
            const card = e.closest('.market-card').getBoundingClientRect();
            const r = e.getBoundingClientRect();
            return {
                clipped: cs.overflow === 'hidden' && cs.textOverflow === 'ellipsis' && cs.whiteSpace === 'nowrap',
                fits: r.right <= card.right + 1 && r.left >= card.left - 1,
                titled: !!e.getAttribute('title')
            };
        })""")
        check("market: repo paths ellipsized within cards, all titled",
              bool(repos) and all(r["clipped"] and r["fits"] and r["titled"] for r in repos))
        page.click("#rail [data-view='chat']")
        page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)

    def f_settings_controls():
        page.keyboard.press("Control+g")
        page.wait_for_selector("#view-settings:not([hidden])", timeout=10_000)
        page.wait_for_selector("#set-generation-temperature", timeout=10_000)
        check("number inputs suppress native spinners (appearance: textfield)",
              page.evaluate("() => getComputedStyle(document.getElementById('set-generation-temperature')).appearance") == "textfield")
        check("temperature is wrapped in a themed stepper",
              page.evaluate("() => { const w = document.getElementById('set-generation-temperature').closest('.stepper');"
                            " return !!w && w.querySelectorAll('button').length === 2; }"))
        v0 = float(page.input_value("#set-generation-temperature"))
        page.click(".stepper:has(#set-generation-temperature) button:last-child")
        v1 = float(page.input_value("#set-generation-temperature"))
        check("stepper '+' increments by the step (0.1)", abs(v1 - v0 - 0.1) < 1e-9)
        page.click(".stepper:has(#set-generation-temperature) button:first-child")
        v2 = float(page.input_value("#set-generation-temperature"))
        check("stepper '−' decrements back", abs(v2 - v0) < 1e-9)
        tog = "#set-generation-streaming"
        check("toggle checkbox is themed (appearance: none)",
              page.evaluate("() => getComputedStyle(document.querySelector('#set-generation-streaming')).appearance") == "none")
        c0 = page.is_checked(tog)
        page.click(tog)
        check("toggle flips on click", page.is_checked(tog) != c0)
        page.click(tog)
        check("toggle flips back", page.is_checked(tog) == c0)
        check("settings enums are om-listbox, not native selects",
              page.evaluate("() => document.getElementById('set-generation-effort').tagName.toLowerCase()") == "om-listbox")
        e0 = page.evaluate("() => document.getElementById('set-generation-effort').value")
        page.focus("#set-generation-effort .dd-btn")
        page.keyboard.press("End")
        page.wait_for_selector("#set-generation-effort .dd-pop:not([hidden])", timeout=5_000)
        page.keyboard.press("Enter")
        page.wait_for_function("() => document.getElementById('set-generation-effort').value === 'max'", timeout=5_000)
        check("listbox End+Enter selects the last option",
              page.evaluate("""() => {
                  const el = document.getElementById('set-generation-effort');
                  const opts = Array.from(el.querySelectorAll('[role="option"]'));
                  const last = opts[opts.length - 1];
                  return el.value === 'max' && opts.length > 1
                      && last.getAttribute('aria-selected') === 'true'
                      && el.options[el.options.length - 1].value === 'max';
              }"""))
        page.click("#set-generation-effort .dd-btn")
        page.wait_for_selector("#set-generation-effort .dd-pop:not([hidden])", timeout=5_000)
        page.keyboard.type(e0, delay=40)
        page.keyboard.press("Enter")
        page.wait_for_function("(v) => document.getElementById('set-generation-effort').value === v", arg=e0, timeout=5_000)
        check("effort restored, settings clean again", page.evaluate("() => !state.settingsDirty"))
        w = page.evaluate("""() => {
            const p = document.getElementById('view-settings'), m = document.getElementById('main');
            const cs = getComputedStyle(p);
            return {inner: p.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight), main: m.clientWidth};
        }""")
        print(f"PROBE settings width @1280: inner={w['inner']:.0f}px main={w['main']:.0f}px"
              f" ({100 * w['inner'] / w['main']:.1f}%)", flush=True)
        check("settings content spans >= 90% of main width", w["inner"] >= 0.9 * w["main"])
        page.keyboard.press("Control+h")
        page.wait_for_selector("#view-help:not([hidden])", timeout=10_000)
        check("clean settings navigate without a warning",
              page.is_visible("#view-help") and page.is_hidden("#confirm-modal")
              and page.evaluate("() => !state.settingsDirty"))

    # ---------------- task #30: tab overflow + per-tab close --------------
    def f_tabs():
        ids = []
        for _ in range(7):
            r = client.post("/api/chats", {"first_message": ""})
            ids.append(r["id"])
        page.evaluate("async () => { await renderSidebar(); }")
        for sid in ids:
            page.evaluate("async (sid) => { await openChat(sid); }", sid)
        page.wait_for_function("(n) => document.querySelectorAll('#tabs .tab').length >= n",
                               arg=len(ids) + 1, timeout=10_000)
        # jump to the strip start so the right-edge fade + wheel are testable
        page.evaluate("() => document.querySelector('#tabs .tab')"
                      ".scrollIntoView({inline:'nearest',block:'nearest',behavior:'instant'})")
        # auto-scroll now follows the active tab, so settle back at the start
        # and let the scroll event refresh the edge fades before measuring.
        page.wait_for_function("() => document.getElementById('tabs').scrollLeft <= 2", timeout=5_000)
        page.wait_for_timeout(150)
        geo = page.evaluate("""() => {
            const t = document.getElementById('tabs'), w = document.getElementById('tabs-wrap');
            const n = document.getElementById('btn-new');
            return {sw: t.scrollWidth, cw: t.clientWidth, canR: w.classList.contains('can-r'),
                    newLeft: n.getBoundingClientRect().left, wrapRight: w.getBoundingClientRect().right,
                    newW: n.getBoundingClientRect().width};
        }""")
        check("tab strip overflows and scrolls horizontally", geo["sw"] > geo["cw"] + 1)
        check("overflow edge fade advertises hidden tabs", geo["canR"])
        check("'+' new-chat stays pinned and visible right of the strip",
              geo["newW"] > 0 and geo["newLeft"] >= geo["wrapRight"] - 1)
        sl0 = page.evaluate("() => document.getElementById('tabs').scrollLeft")
        page.hover("#tabs")
        page.mouse.wheel(0, 240)
        page.wait_for_timeout(450)
        sl1 = page.evaluate("() => document.getElementById('tabs').scrollLeft")
        check("vertical wheel scrolls the strip horizontally", sl1 > sl0)
        # activating a far-away chat scrolls its tab into view
        page.click("#sb-list .sb-row:has-text('quartz') .title", position={"x": 8, "y": 8})
        page.wait_for_function("""() => {
            const a = document.querySelector('#tabs .tab.active');
            if (!a) return false;
            const t = document.getElementById('tabs').getBoundingClientRect();
            const r = a.getBoundingClientRect();
            return r.left >= t.left - 1 && r.right <= t.right + 1;
        }""", timeout=5_000)
        check("activating a chat scrolls its tab into view",
              page.evaluate("""() => {
                  const a = document.querySelector('#tabs .tab.active');
                  if (!a) return false;
                  const t = document.getElementById('tabs').getBoundingClientRect();
                  const r = a.getBoundingClientRect();
                  return a.dataset.sid === state.session
                      && (a.textContent || '').includes('quartz')
                      && r.left >= t.left - 1 && r.right <= t.right + 1;
              }"""))
        reach = page.evaluate("""() => {
            const t = document.getElementById('tabs'), tr = t.getBoundingClientRect();
            return Array.from(t.querySelectorAll('.tab')).every(el => {
                el.scrollIntoView({inline: 'nearest', block: 'nearest', behavior: 'instant'});
                const r = el.getBoundingClientRect();
                return r.left >= tr.left - 1 && r.right <= tr.right + 1;
            });
        }""")
        check("no tab is unreachable (every tab scrolls into view)", reach)
        # × close on an inactive tab: the tab goes, the session stays
        n0 = page.evaluate("() => state.openTabs.length")
        v1 = ids[0]
        sel1 = f"#tabs .tab[data-sid='{v1}']"
        page.evaluate("(s) => document.querySelector(s)"
                      ".scrollIntoView({inline:'nearest',block:'nearest',behavior:'instant'})", sel1)
        page.hover(sel1)
        page.click(f"{sel1} .tab-close")
        page.wait_for_function("(sid) => !state.openTabs.includes(sid)", arg=v1, timeout=5_000)
        check("× closes the tab (count drops by one)",
              page.evaluate("() => state.openTabs.length") == n0 - 1)
        chats_after = client.get("/api/chats").get("chats", [])
        check("closed tab's session remains in Chats (bridge + sidebar)",
              any(c["id"] == v1 for c in chats_after)
              and page.evaluate("(sid) => state.chats.some(c => c.id === sid)", v1))
        # middle-click closes too
        v2 = ids[1]
        sel2 = f"#tabs .tab[data-sid='{v2}']"
        page.evaluate("(s) => document.querySelector(s)"
                      ".scrollIntoView({inline:'nearest',block:'nearest',behavior:'instant'})", sel2)
        page.click(sel2, button="middle")
        page.wait_for_function("(sid) => !state.openTabs.includes(sid)", arg=v2, timeout=5_000)
        check("middle-click closes a tab",
              page.evaluate("(sid) => !state.openTabs.includes(sid)", v2)
              and page.evaluate("(sid) => !document.querySelector(`#tabs .tab[data-sid='${sid}']`)", v2)
              and any(c["id"] == v2 for c in client.get("/api/chats").get("chats", [])))
        # closing the ACTIVE tab activates a neighbor
        act = page.evaluate("() => state.session")
        sela = f"#tabs .tab[data-sid='{act}']"
        page.evaluate("(s) => document.querySelector(s)"
                      ".scrollIntoView({inline:'nearest',block:'nearest',behavior:'instant'})", sela)
        page.hover(sela)
        page.click(f"{sela} .tab-close")
        page.wait_for_function("(sid) => !state.openTabs.includes(sid) && state.session !== sid && state.session !== null",
                               arg=act, timeout=10_000)
        check("closing the active tab activates a neighbor",
              page.evaluate("() => state.openTabs.includes(state.session)"))
        # leave a populated chat active for the capture pass
        qid = page.evaluate("() => (state.chats.find(c => (c.title || '').includes('quartz')) || {}).id")
        if qid:
            page.evaluate("async (sid) => { await openChat(sid); }", qid)

    for name, fn in [
        ("boot", f_boot), ("ime", f_ime), ("hints", f_hints), ("stream", f_stream),
        ("approval-allow", f_approval_allow), ("approval-deny", f_approval_deny),
        ("steer", f_steer), ("draft-switch", f_draft_switch), ("draft-reject", f_draft_reject),
        ("search", f_search), ("dirty-settings", f_dirty_settings), ("focus-trap", f_focus_trap),
        ("listbox", f_listbox), ("rail", f_rail), ("palette-rows", f_palette_rows),
        ("market-cards", f_market_cards), ("settings-controls", f_settings_controls),
        ("tabs", f_tabs),
        # ---- R1 UX regression flows (task #48) ----
        ("codeblock", lambda: f_codeblock(page, client)),
        ("replay-status", lambda: f_replay_status(page, client)),
        ("dates", lambda: f_dates(page, client)),
        ("close-guard", lambda: f_close_guard(page, client)),
        ("stay", lambda: f_stay(page, client)),
        ("composer-foot", lambda: f_composer_foot(page, client)),
        ("market-bars", lambda: f_market_bars(page, client)),
        ("settings-bounds", lambda: f_settings_bounds(page, client)),
        ("r1ux-misc", lambda: f_misc(page, client)),
        ("quit", lambda: f_quit(page, client)),
        ("hints-aria", lambda: f_hints_aria(page, client)),
        ("export-skills", lambda: f_export_skills(page, client)),
        ("toggles", lambda: f_toggles(page, client)),
        ("tab-patch", lambda: f_tab_patch(page, client)),
        # ---- R2 UX regression flows (task #56) ----
        ("r2-palette-aria", lambda: f_r2_palette_aria(page, client)),
        ("r2-slash-parity", lambda: f_r2_slash_parity(page, client)),
        ("r2-approval-queue", lambda: f_r2_approval_queue(page, client)),
        ("r2-models-keyboard", lambda: f_r2_models_keyboard(page, client)),
        ("r2-drop-guard", lambda: f_r2_drop_guard(page, client)),
        ("r2-toast-placement", lambda: f_r2_toast_placement(page, client)),
        ("r2-bg3-contrast", lambda: f_r2_bg3_contrast(page, client)),
        ("r2-misc-a11y", lambda: f_r2_misc(page, client)),
        ("r2-div-buttons", lambda: f_r2_div_buttons(page, client)),
        # ---- R3 UX regression flows (task #65) ----
        ("r3-inline-card-id", lambda: f_r3_inline_card_id(page, client)),
        ("r3-live-contrast", lambda: f_r3_live_contrast(page, client)),
        ("r3-yolo-truth", lambda: f_r3_yolo_truth(page, client)),
        ("r3-skill-cast-args", lambda: f_r3_skill_cast_args(page, client)),
        ("r3-mode-tone-case", lambda: f_r3_mode_tone_case(page, client)),
        ("r3-reject-hero", lambda: f_r3_reject_hero(page, client)),
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


# --------------------------------------------------------------------------
# R1 UX regression flows (task #48). Module-level so each is self-contained;
# they run AFTER the legacy laptop flows and never mutate their assertions.
# --------------------------------------------------------------------------
def f_codeblock(page, client) -> None:
    """K1: hl() tokenizes the RAW source once — quotes/comments/keywords in
    code must round-trip, never nest spans, and render + copy verbatim."""
    src = "\n".join([
        '# class demo: "return" inside a comment',
        "def f(x):",
        '    s = "class const return 42"',
        "    t = 'fn let 7'",
        "    return x + 1  # trailing class",
    ])
    probe = page.evaluate("""(src) => {
        const h = hl(src, 'python');
        const stripped = h.replace(/<span class="[a-z]+">/g, '').replace(/<\\/span>/g, '');
        const round = stripped.replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
        return {
            roundtrip: round === src,
            noNested: !/<span[^>]*>[^<]*<span/.test(h),
            cmt2: (h.match(/class="cmt"/g) || []).length === 2,
            kw2: (h.match(/class="kw"/g) || []).length === 2,
            strKeeps: /class="str">[^<]*class const return/.test(h),
        };
    }""", src)
    check("K1 hl(): raw-source roundtrip, no nested spans", probe["roundtrip"] and probe["noNested"])
    check("K1 hl(): string>comment>keyword priority (2 cmt, 2 kw, keywords kept in string)",
          probe["cmt2"] and probe["kw2"] and probe["strKeeps"])
    store = SessionStore()
    sess = store.create(title="codeblock probe")
    sess.add_message("user", "show me code")
    sess.add_message("assistant", "Here:\n\n```python\n" + src + "\n```\n")
    store.save(sess)
    page.evaluate("async (sid) => { await openChat(sid); }", sess.id)
    page.wait_for_selector("#stream .codeblock code", timeout=10_000)
    dom = page.evaluate("""() => {
        const c = document.querySelector('#stream .codeblock code');
        const b = document.querySelector('#stream .codeblock .copy-code');
        return {code: c.textContent, copy: b.dataset.code};
    }""")
    check("K1 rendered code block preserves source exactly (display + copy)",
          dom["code"].strip() == src.strip() and dom["copy"].strip() == src.strip())
    page.screenshot(path=str(VALIDATION / "gui-laptop-codeblock.png"), full_page=False)
    check("screenshot saved: .validation/gui-laptop-codeblock.png",
          (VALIDATION / "gui-laptop-codeblock.png").exists())


def f_replay_status(page, client) -> None:
    """K2: replay journals per-call id/args/status — ok/denied/blocked rows
    stay distinct, colored, and the approval echoes become decided cards."""
    store = SessionStore()
    sess = store.create(title="replay probe")
    sess.add_message("user", "run some tools")
    sess.add_message("assistant", "", tool_calls=[
        {"id": "call_a", "type": "function", "function": {"name": "run_command", "arguments": json.dumps({"command": "ls"})}},
        {"id": "call_b", "type": "function", "function": {"name": "run_command", "arguments": json.dumps({"command": "rm -rf /tmp/x"})}},
        {"id": "call_c", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "/etc/passwd", "content": "x"})}},
    ])
    sess.add_message("tool", "total 0\nfile.txt", tool_call_id="call_a", name="run_command")
    sess.add_message("assistant", "[asked to run run_command]")
    sess.add_message("tool", "The user DENIED this tool call. Do not retry it; adapt your answer.", tool_call_id="call_b", name="run_command")
    sess.add_message("assistant", "[attempted write_file]")
    sess.add_message("tool", "BLOCKED BY PERMISSION ENGINE: outside workspace", tool_call_id="call_c", name="write_file")
    sess.add_message("assistant", "Done.")
    sess.record_tool("run_command", {"command": "ls"}, "ok", "allow", 12.0)
    sess.record_tool("run_command", {"command": "rm -rf /tmp/x"}, "denied", "deny", 3.0)
    sess.record_tool("write_file", {"path": "/etc/passwd", "content": "x"}, "blocked", "deny", 1.0)
    store.save(sess)
    page.evaluate("async (sid) => { await openChat(sid); }", sess.id)
    page.wait_for_selector("#stream .tool-entry", timeout=10_000)
    info = page.evaluate("""() => Array.from(document.querySelectorAll('#stream .tool-entry')).map(e => ({
        call: e.dataset.call,
        cls: ['ok', 'denied', 'blocked', 'err'].filter(c => e.classList.contains(c)),
        tag: e.querySelector('summary .tag').textContent,
        color: getComputedStyle(e.querySelector('summary .tag')).color,
        args: e.querySelectorAll('pre')[0].textContent,
    }))""")
    ok = (len(info) == 3
          and [i["call"] for i in info] == ["call_a", "call_b", "call_c"]
          and info[0]["cls"] == ["ok"] and info[0]["tag"] == "ok"
          and info[1]["cls"] == ["denied"] and info[1]["tag"] == "denied"
          and info[2]["cls"] == ["blocked"] and info[2]["tag"] == "blocked"
          and "ls" in info[0]["args"] and "rm -rf" in info[1]["args"])
    check("K2 replay: per-call id, status class, tag and args (ok/denied/blocked)", ok)
    check("K2 replay: denied tag red, blocked tag amber",
          info[1]["color"] == "rgb(224, 104, 92)" and info[2]["color"] == "rgb(226, 166, 61)")
    st = page.evaluate(EMPTY_AGENT_JS)
    check("K2 replay: echoes are decided cards, no empty rows, no leaked prose",
          st["cards"] == 2 and st["empty"] == 0 and st["echoes"] == 0)
    page.screenshot(path=str(VALIDATION / "gui-laptop-replay.png"), full_page=False)
    check("screenshot saved: .validation/gui-laptop-replay.png",
          (VALIDATION / "gui-laptop-replay.png").exists())


def f_dates(page, client) -> None:
    """K3: /api/chats rows carry epoch SECONDS — relTime must scale to ms."""
    page.evaluate("async () => { await renderSidebar(); }")
    page.wait_for_selector("#sb-list .sb-row time", timeout=10_000)
    probe = page.evaluate("""() => {
        const nowS = Date.now() / 1000;
        return {
            h: relTime(nowS - 3600),
            m: relTime(nowS - 120),
            iso: relTime(new Date((nowS - 3600) * 1000).toISOString()),
            times: Array.from(document.querySelectorAll('#sb-list .sb-row time')).map(e => e.textContent),
        };
    }""")
    check("K3 relTime: epoch seconds -> '1h ago' / '2m ago', ISO too",
          probe["h"] == "1h ago" and probe["m"] == "2m ago" and probe["iso"] == "1h ago")
    check("K3 sidebar timestamps sane (non-empty, no 1970)",
          bool(probe["times"]) and all(t.strip() and "1970" not in t for t in probe["times"]))


def f_close_guard(page, client) -> None:
    """K5: the streaming guard runs before pruning; a refused switch restores
    the tab instead of leaking it."""
    wait_ready(page)
    sid = page.evaluate("() => state.session")
    other = client.post("/api/chats", {"first_message": ""})["id"]
    page.evaluate("(sid) => { state.openTabs = [sid]; renderTabs(); }", sid)
    send(page, "slow guard window")
    page.wait_for_selector("#cancel:not([hidden])", timeout=15_000)
    page.evaluate("async (sid) => { await closeTab(sid); }", sid)
    g1 = page.evaluate("""(sid) => ({
        present: state.openTabs.includes(sid),
        session: state.session === sid,
        toast: document.getElementById('toasts').innerText,
    })""", sid)
    check("K5 closeTab: streaming guard refuses last-tab close, tab+session intact",
          g1["present"] and g1["session"]
          and "Stop the current turn before closing the last tab" in g1["toast"])
    page.evaluate("(o) => { state.openTabs.push(o); renderTabs(); }", other)
    page.evaluate("async (sid) => { await closeTab(sid); }", sid)
    g2 = page.evaluate("""(sid) => ({
        present: state.openTabs.includes(sid), session: state.session === sid,
    })""", sid)
    check("K5 closeTab: refused switch restores the tab (no pruning leak)",
          g2["present"] and g2["session"])
    page.keyboard.press("Control+x")
    wait_ready(page)


def f_stay(page, client) -> None:
    """K6: Stay on the unsaved-edits modal cancels the switch AND new-chat."""
    other = client.post("/api/chats", {"first_message": ""})["id"]
    page.keyboard.press("Control+g")
    page.wait_for_selector("#view-settings:not([hidden])", timeout=10_000)
    page.wait_for_selector("#set-generation-temperature", timeout=10_000)
    orig = page.input_value("#set-generation-temperature")
    dirty = "0.9" if orig != "0.9" else "0.8"
    page.fill("#set-generation-temperature", dirty)
    page.wait_for_function("() => state.settingsDirty", timeout=5_000)
    n0 = len(client.get("/api/chats").get("chats", []))
    page.evaluate("(sid) => { window.__r = openChat(sid); }", other)
    page.wait_for_selector("#confirm-modal:not([hidden])", timeout=10_000)
    page.click("#cf-cancel")
    r = page.evaluate("async () => await window.__r")
    stayed = page.evaluate("""() => ({
        view: state.view,
        visible: !document.getElementById('view-settings').hidden,
        temp: document.getElementById('set-generation-temperature').value,
    })""")
    check("K6 Stay cancels chat switch (openChat false, dirty editor kept)",
          r is False and stayed["view"] == "settings" and stayed["visible"] and stayed["temp"] == dirty)
    page.evaluate("() => { window.__r2 = newChat(); }")
    page.wait_for_selector("#confirm-modal:not([hidden])", timeout=10_000)
    page.click("#cf-cancel")
    page.evaluate("async () => { await window.__r2; }")
    n1 = len(client.get("/api/chats").get("chats", []))
    check("K6 Stay cancels new-chat (no chat spawned)", n1 == n0)
    page.fill("#set-generation-temperature", orig)
    page.wait_for_function("() => !state.settingsDirty", timeout=5_000)
    page.keyboard.press("Control+h")
    page.wait_for_selector("#view-help:not([hidden])", timeout=10_000)
    check("K6 clean editor navigates after restore",
          page.is_visible("#view-help") and page.is_hidden("#confirm-modal")
          and page.evaluate("() => !state.settingsDirty"))
    page.click("#rail [data-view='chat']")
    page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)


def f_composer_foot(page, client) -> None:
    """K8: --composer-h tracks the real composer height so the stream-foot
    rides above it even when the composer grows tall."""
    page.click("#rail [data-view='chat']")
    page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)
    page.fill("#input", "\n".join(f"line {i}" for i in range(12)))
    page.wait_for_timeout(350)
    geo = page.evaluate("""() => {
        const w = document.querySelector('.composer-wrap');
        const foot = document.querySelector('.stream-foot');
        const cs = getComputedStyle(document.documentElement).getPropertyValue('--composer-h').trim();
        return {varH: parseFloat(cs), wrapH: w.offsetHeight,
                footBottom: foot.getBoundingClientRect().bottom,
                wrapTop: w.getBoundingClientRect().top};
    }""")
    check("K8 --composer-h tracks the real composer height", abs(geo["varH"] - geo["wrapH"]) <= 1)
    check("K8 stream-foot sits above the grown composer (no overlap)",
          geo["footBottom"] <= geo["wrapTop"] + 1)
    page.screenshot(path=str(VALIDATION / "gui-laptop-tall-composer.png"), full_page=False)
    check("screenshot saved: .validation/gui-laptop-tall-composer.png",
          (VALIDATION / "gui-laptop-tall-composer.png").exists())
    page.fill("#input", "")
    page.wait_for_timeout(200)


def f_market_bars(page, client) -> None:
    """K9: fit-bar widths are set through CSSOM (CSP blocks style attrs)."""
    page.click("#rail [data-view='market']")
    page.wait_for_selector("#view-market:not([hidden])", timeout=10_000)
    page.wait_for_selector(".market-card", timeout=15_000)
    page.evaluate("() => { if (marketData && !marketData.vram_bytes)"
                  " { marketData.vram_bytes = 24 * 2 ** 30; paintMarket(); } }")
    page.wait_for_timeout(200)
    bars = page.evaluate("""() => {
        const is = Array.from(document.querySelectorAll('.mc-bar i'));
        return {n: is.length,
                allInline: is.every(e => e.style.width && e.style.width.endsWith('%')),
                widths: [...new Set(is.map(e => getComputedStyle(e).width))]};
    }""")
    check("K9 market fit bars set width via CSSOM (inline style, CSP-safe)",
          bars["n"] > 0 and bars["allInline"])
    check("K9 market fit bars show varied widths (not all zero)", len(bars["widths"]) >= 2)
    page.click("#rail [data-view='chat']")
    page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)


def f_settings_bounds(page, client) -> None:
    """K10: number inputs carry min/max; an out-of-range save highlights the
    field inline, keeps the edit, and never sends a partial config payload."""
    page.keyboard.press("Control+g")
    page.wait_for_selector("#view-settings:not([hidden])", timeout=10_000)
    page.wait_for_selector("#set-generation-temperature", timeout=10_000)
    orig = page.input_value("#set-generation-temperature")
    attrs = page.evaluate("""() => {
        const t = document.getElementById('set-generation-temperature');
        const m = document.getElementById('set-agent-max_steps');
        return {tmin: t.min, tmax: t.max, mmin: m.min, mmax: m.max};
    }""")
    check("K10 number inputs carry min/max from SET_BOUNDS",
          attrs["tmin"] == "0" and attrs["tmax"] == "2" and attrs["mmin"] == "1" and attrs["mmax"] == "64")
    hits = {"n": 0}

    def guard(route):
        if route.request.method in ("PATCH", "POST"):
            hits["n"] += 1
        route.continue_()

    page.route("**/api/config", guard)
    page.route("**/api/config/agent", guard)
    try:
        page.fill("#set-generation-temperature", "5")
        page.click("#settings-save")
        page.wait_for_selector("#set-generation-temperature.invalid", timeout=5_000)
        err = page.evaluate("""() => {
            const t = document.getElementById('set-generation-temperature');
            const e = document.getElementById('set-generation-temperature-err');
            return {invalid: t.classList.contains('invalid'), aria: t.getAttribute('aria-invalid'),
                    errText: e ? e.textContent : '', value: t.value,
                    toast: document.getElementById('toasts').innerText};
        }""")
        check("K10 out-of-range save: inline error + aria-invalid + value kept",
              err["invalid"] and err["aria"] == "true" and "2" in err["errText"] and err["value"] == "5")
        check("K10 out-of-range save: human toast, no partial config write",
              "out of range" in err["toast"] and hits["n"] == 0)
        page.fill("#set-generation-temperature", orig)
        page.wait_for_function(
            "() => !document.getElementById('set-generation-temperature').classList.contains('invalid')",
            timeout=5_000)
        cleared = page.evaluate("""() => {
            const t = document.getElementById('set-generation-temperature');
            const e = document.getElementById('set-generation-temperature-err');
            return {invalid: t.classList.contains('invalid'), aria: t.getAttribute('aria-invalid'),
                    errText: e ? e.textContent.trim() : '', value: t.value};
        }""")
        check("K10 inline error clears when the field is corrected",
              not cleared["invalid"] and cleared["aria"] != "true"
              and not cleared["errText"] and cleared["value"] == orig)
    finally:
        page.unroute("**/api/config")
        page.unroute("**/api/config/agent")
    page.keyboard.press("Control+h")
    page.wait_for_selector("#view-help:not([hidden])", timeout=10_000)
    page.click("#rail [data-view='chat']")
    page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)


def f_misc(page, client) -> None:
    """K11/K30/K31/K32/K42/K36 + K22 help/mail/search honesty fixes."""
    page.click("#rail [data-view='chat']")
    page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)
    page.evaluate("() => setSidebar(false)")
    page.keyboard.press("Control+o")
    page.wait_for_timeout(200)
    k11 = page.evaluate("() => ({open: sidebarIsOpen(), focus: document.activeElement.id})")
    check("K11 Ctrl+O opens sidebar + focuses search", k11["open"] and k11["focus"] == "sb-search")
    check("K30 run-verb is aria-hidden (decorative)",
          page.get_attribute("#run-verb", "aria-hidden") == "true")
    page.keyboard.press("Control+h")
    page.wait_for_selector("#view-help:not([hidden])", timeout=10_000)
    page.wait_for_selector("#help-body table", timeout=10_000)
    check("K31 help table cells wrap (white-space: normal)",
          page.evaluate("() => Array.from(document.querySelectorAll('#help-body td'))"
                        ".every(td => getComputedStyle(td).whiteSpace === 'normal')"))
    check("K22 help documents the TUI-only subset", "TUI-only" in page.text_content("#help-body"))
    page.click("#rail [data-view='providers']")
    page.wait_for_selector("#view-providers:not([hidden])", timeout=10_000)
    page.wait_for_timeout(300)
    cur = page.evaluate("() => { const r = document.querySelector('.prov-row');"
                        " return r ? getComputedStyle(r).cursor : 'auto'; }")
    check("K32 provider rows have no pointer cursor", cur != "pointer")
    check("K42 .dd.right CSS rule present", page.evaluate("""() => {
        for (const sheet of document.styleSheets) {
            try { for (const r of sheet.cssRules) if (r.selectorText && r.selectorText.includes('.dd.right')) return true; }
            catch (e) { /* cross-origin */ }
        }
        return false;
    }"""))
    page.click("#rail [data-view='chat']")
    page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)
    page.route("**/api/voice", lambda route: route.fulfill(
        status=401, content_type="application/json", body=json.dumps({"error": "bad token"})))
    try:
        page.evaluate("async () => { await handleSlash('/voice'); }")
        page.wait_for_timeout(200)
        check("K36 401 toast gives the relaunch instruction", "relaunch with" in page.text_content("#toasts"))
    finally:
        page.unroute("**/api/voice")
    page.evaluate("async () => { await handleSlash('/mail'); }")
    page.wait_for_timeout(150)
    check("K22 /mail toast points to `oml mail`", "oml mail" in page.text_content("#toasts"))
    page.evaluate("async () => { await handleSlash('/search quartz'); }")
    page.wait_for_timeout(400)
    s = page.evaluate("""() => ({open: sidebarIsOpen(), q: document.getElementById('sb-search').value,
        rows: document.querySelectorAll('#sb-list .sb-row').length})""")
    check("K22 /search opens sidebar with the query and results",
          s["open"] and s["q"] == "quartz" and s["rows"] >= 1)
    page.fill("#sb-search", "")
    page.evaluate("async () => { await renderSidebar(); }")


def f_quit(page, client) -> None:
    """K12: /quit honors ui.confirm_quit. The shutdown endpoint is ALWAYS
    intercepted — the real bridge must never be told to shut down."""
    hits = {"n": 0}

    def shutdown_guard(route):
        hits["n"] += 1
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))

    page.route("**/api/shutdown", shutdown_guard)
    try:
        page.evaluate("() => { state.config.ui = state.config.ui || {}; state.config.ui.confirm_quit = false; }")
        page.evaluate("async () => { await handleSlash('/quit'); }")
        page.wait_for_timeout(200)
        check("K12 confirm_quit=false: /quit posts shutdown without a modal",
              hits["n"] == 1 and page.evaluate("() => document.getElementById('confirm-modal').hidden"))
        page.evaluate("() => { state.config.ui.confirm_quit = true; }")
        page.evaluate("() => { window.__q = handleSlash('/quit'); }")
        page.wait_for_selector("#confirm-modal:not([hidden])", timeout=10_000)
        page.click("#cf-cancel")
        page.evaluate("async () => { await window.__q; }")
        check("K12 confirm_quit=true: Stay cancels shutdown", hits["n"] == 1)
    finally:
        page.unroute("**/api/shutdown")
        page.evaluate("() => { if (state.config && state.config.ui) delete state.config.ui.confirm_quit; }")


def f_hints_aria(page, client) -> None:
    """K41 exact-match sorts first; K35 composer is a combobox whose
    aria-activedescendant follows the arrow keys."""
    page.click("#rail [data-view='chat']")
    page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)
    page.fill("#input", "/mode")
    page.wait_for_selector("#slash-hint:not([hidden])", timeout=5_000)
    check("K41 exact-match command sorts first", page.evaluate("() => slashMatches[0].cmd") == "/mode")
    aria = page.evaluate("""() => {
        const i = document.getElementById('input');
        return {role: i.getAttribute('role'), expanded: i.getAttribute('aria-expanded'),
                active: i.getAttribute('aria-activedescendant')};
    }""")
    check("K35 composer combobox: expanded + activedescendant",
          aria["role"] == "combobox" and aria["expanded"] == "true" and aria["active"] == "slash-opt-0")
    page.keyboard.press("ArrowDown")
    a1 = page.get_attribute("#input", "aria-activedescendant")
    page.keyboard.press("ArrowUp")
    a0 = page.get_attribute("#input", "aria-activedescendant")
    check("K35 arrow keys move aria-activedescendant", a1 == "slash-opt-1" and a0 == "slash-opt-0")
    page.keyboard.press("Enter")
    page.wait_for_selector("#mode-modal:not([hidden])", timeout=5_000)
    check("K41 Enter on the exact match opens the mode modal",
          page.is_visible("#mode-modal")
          and page.evaluate("() => document.getElementById('mode-modal').contains(document.activeElement)")
          and page.input_value("#input") == "")
    page.keyboard.press("Escape")
    page.wait_for_selector("#mode-modal", state="hidden", timeout=5_000)
    page.wait_for_timeout(150)
    check("K35 aria-expanded false after hints close", page.get_attribute("#input", "aria-expanded") == "false")
    page.fill("#input", "")


def f_export_skills(page, client) -> None:
    """K39: /export json writes a .json file, bad formats warn + keep input,
    and casting an unknown skill is refused."""
    page.click("#rail [data-view='chat']")
    page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)
    sid = page.evaluate("() => state.session")
    if not sid:
        sid = client.post("/api/chats", {"first_message": "export probe"})["id"]
        page.evaluate("async (s) => { await openChat(s); }", sid)
    page.evaluate("async () => { await handleSlash('/export json'); }")
    page.wait_for_timeout(400)
    toast = page.text_content("#toasts")
    check("K39 /export json exports a .json file", "exported" in toast and ".json" in toast)
    page.fill("#input", "/export bogus")
    page.evaluate("async () => { await handleSlash('/export bogus'); }")
    page.wait_for_timeout(200)
    bad = page.evaluate("() => ({toast: document.getElementById('toasts').innerText,"
                        " input: document.getElementById('input').value})")
    check("K39 /export rejects unknown format with usage + keeps input",
          "usage: /export" in bad["toast"] and bad["input"] == "/export bogus")
    page.fill("#input", "")
    skill_dir = HOME / "skills" / "probe-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: probe-skill\ndescription: A probe skill for the checker.\n---\n\n# Probe\n\nInstructions.\n",
        encoding="utf-8")
    page.click("#btn-plus")
    page.wait_for_selector("#plus-skills", state="visible", timeout=5_000)
    page.click("#plus-skills")
    page.wait_for_selector("#prompt-modal:not([hidden])", timeout=5_000)
    page.fill("#pm-input", "does-not-exist")
    page.click("#pm-ok")
    page.wait_for_timeout(300)
    sk = page.text_content("#toasts")
    check("K39 casting an unknown skill is refused with a helpful toast",
          "unknown skill" in sk and "probe-skill" in sk)


def f_toggles(page, client) -> None:
    """K23: Ctrl+J / Ctrl+T parity toggles, End jump-to-latest, F2 sidebar,
    and an honest refusal of vision image paste."""
    page.click("#rail [data-view='chat']")
    page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)
    j0 = page.evaluate("() => !(state.config && state.config.ui && state.config.ui.show_tool_panels === false)")
    page.keyboard.press("Control+j")
    page.wait_for_timeout(300)
    j1 = page.evaluate("() => state.config.ui.show_tool_panels")
    check("K23 Ctrl+J toggles tool panels", j1 == (not j0))
    page.keyboard.press("Control+j")
    page.wait_for_timeout(300)
    check("K23 Ctrl+J toggles back",
          page.evaluate("() => !(state.config.ui.show_tool_panels === false)") == j0)
    t0 = page.evaluate("() => !(state.config && state.config.agent && state.config.agent.thinking_visible === false)")
    page.keyboard.press("Control+t")
    page.wait_for_timeout(300)
    check("K23 Ctrl+T toggles thinking text",
          page.evaluate("() => state.config.agent.thinking_visible") == (not t0))
    page.keyboard.press("Control+t")
    page.wait_for_timeout(300)
    page.evaluate("() => { const i = document.getElementById('input'); i.blur();"
                  " if (document.activeElement && document.activeElement.blur) document.activeElement.blur();"
                  " state.following = false; document.getElementById('stream').scrollTop = 0; }")
    page.keyboard.press("End")
    page.wait_for_timeout(200)
    end = page.evaluate("() => ({following: state.following, jump: document.getElementById('jump-latest').hidden})")
    check("K23 End jumps to latest + resumes following", end["following"] and end["jump"])
    f0 = page.evaluate("() => sidebarIsOpen()")
    page.keyboard.press("F2")
    page.wait_for_timeout(200)
    check("K23 F2 toggles the sidebar", page.evaluate("() => sidebarIsOpen()") != f0)
    page.keyboard.press("F2")
    page.wait_for_timeout(200)
    paste = page.evaluate("""() => {
        const inp = document.getElementById('input');
        const dt = new DataTransfer();
        dt.items.add(new File([new Uint8Array([137, 80, 78, 71])], 'x.png', {type: 'image/png'}));
        const ev = new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true});
        inp.dispatchEvent(ev);
        return {prevented: ev.defaultPrevented, toast: document.getElementById('toasts').innerText};
    }""")
    check("K23 image paste refused with TUI-only guidance",
          paste["prevented"] and "TUI-only" in paste["toast"])


def f_tab_patch(page, client) -> None:
    """K4/K34: renderTabs diff/patches (nodes + scroll + focus survive the 2s
    poll), tabs are proper ARIA tabs, and the overflow scrollbar is hidden.
    Runs LAST so the capture pass shows a populated, overflowing strip."""
    page.click("#rail [data-view='chat']")
    page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)
    need = page.evaluate("() => Math.max(0, 12 - state.openTabs.length)")
    for _ in range(need):
        sid = client.post("/api/chats", {"first_message": ""})["id"]
        page.evaluate("async (s) => { await openChat(s); }", sid)
    page.wait_for_function("() => document.querySelectorAll('#tabs .tab').length >= 12", timeout=15_000)
    page.evaluate("""() => {
        document.querySelectorAll('#tabs .tab').forEach((t, i) => { t.__mark = 'm' + i; });
        document.getElementById('tabs').scrollLeft = 40;
    }""")
    sl_set = page.evaluate("() => document.getElementById('tabs').scrollLeft")
    page.evaluate("() => renderTabs()")
    patch = page.evaluate("""() => {
        const ts = Array.from(document.querySelectorAll('#tabs .tab'));
        return {marks: ts.every(t => typeof t.__mark === 'string'),
                scroll: document.getElementById('tabs').scrollLeft,
                roles: ts.every(t => t.getAttribute('role') === 'tab'),
                selected: ts.filter(t => t.getAttribute('aria-selected') === 'true').length,
                keyshortcuts: ts.every(t => t.getAttribute('aria-keyshortcuts') === 'Delete Backspace'),
                tablist: document.getElementById('tabs').getAttribute('role') === 'tablist',
                roving: ts.filter(t => t.tabIndex === 0).length};
    }""")
    check("K4 renderTabs diff/patch keeps nodes + scroll (no innerHTML rebuild)",
          patch["marks"] and abs(patch["scroll"] - sl_set) <= 1)
    check("K34 tabs ARIA: role=tab, one selected, keyshortcuts, tablist, roving tabindex",
          patch["roles"] and patch["selected"] == 1 and patch["keyshortcuts"]
          and patch["tablist"] and patch["roving"] == 1)
    sb = page.evaluate("""() => {
        const t = document.getElementById('tabs');
        return {sw: t.scrollWidth, cw: t.clientWidth, oh: t.offsetHeight, ch: t.clientHeight,
                sbw: getComputedStyle(t).scrollbarWidth};
    }""")
    check("tab strip overflows (scrollWidth > clientWidth)", sb["sw"] > sb["cw"])
    check("tab strip scrollbar fully hidden (zero height + scrollbar-width:none)",
          sb["oh"] - sb["ch"] == 0 and sb["sbw"] == "none")
    # visual evidence for the hidden-scrollbar contract: capture the strip
    # while it is populated and overflowing, before later flows mutate it
    page.screenshot(path=str(VALIDATION / "gui-laptop-tabs.png"))
    check("screenshot saved: .validation/gui-laptop-tabs.png",
          (VALIDATION / "gui-laptop-tabs.png").exists())
    page.evaluate("""() => {
        const a = document.querySelector('#tabs .tab.active') || document.querySelector('#tabs .tab');
        a.focus(); a.__focusMark = true;
    }""")
    page.wait_for_timeout(2300)
    foc = page.evaluate("""() => {
        const ae = document.activeElement;
        return {isTab: !!(ae && ae.closest && ae.closest('#tabs .tab')), mark: !!(ae && ae.__focusMark)};
    }""")
    check("K4 focused tab survives the 2s poll (focus + node retained)", foc["isTab"] and foc["mark"])
    del_sid = page.evaluate("() => document.activeElement.dataset.sid")
    page.keyboard.press("Delete")
    page.wait_for_function("(sid) => !state.openTabs.includes(sid)", arg=del_sid, timeout=5_000)
    check("K34 Delete closes the focused tab",
          page.evaluate("(sid) => !state.openTabs.includes(sid)", del_sid)
          and page.evaluate("(sid) => !document.querySelector(`#tabs .tab[data-sid='${sid}']`)", del_sid))
    qid = page.evaluate("() => (state.chats.find(c => (c.title || '').includes('quartz')) || {}).id")
    if qid:
        page.evaluate("async (s) => { await openChat(s); }", qid)
    # K5 regression: the closed tab must stay closed even when the neighbor
    # switch loses the load race to this newer openChat (no zombie restore).
    page.wait_for_timeout(300)
    check("K5 closed tab stays closed when a newer switch wins the load race",
          page.evaluate("(sid) => !state.openTabs.includes(sid)", del_sid))
    page.evaluate("() => { document.getElementById('tabs').scrollLeft = 0; }")


# --------------------------------------------------------------------------
# R2 UX regression flows (task #56). Module-level, self-contained; they run
# AFTER the R1 flows and prove the GUI-side fixes (U4-U17, U21-U24). Each
# restores any state it mutates so the capture pass shows the resting UI.
# --------------------------------------------------------------------------
def f_r2_palette_aria(page, client) -> None:
    """U7: the command palette is a real combobox/listbox — expanded state,
    option roles and aria-activedescendant track the arrow keys."""
    page.click("#rail [data-view='chat']")
    page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)
    page.keyboard.press("Control+k")
    page.wait_for_selector("#palette-modal:not([hidden])", timeout=5_000)
    aria = page.evaluate("""() => {
        const i = document.getElementById('palette-input');
        const ul = document.getElementById('palette-list');
        const opts = Array.from(ul.querySelectorAll('li[role="option"]'));
        return {role: i.getAttribute('role'), controls: i.getAttribute('aria-controls'),
                autocomplete: i.getAttribute('aria-autocomplete'), haspopup: i.getAttribute('aria-haspopup'),
                expanded: i.getAttribute('aria-expanded'), active: i.getAttribute('aria-activedescendant'),
                listRole: ul.getAttribute('role'), n: opts.length,
                allOptions: opts.every(o => o.id && o.getAttribute('aria-selected') !== null),
                firstSel: opts[0] ? opts[0].getAttribute('aria-selected') : null};
    }""")
    check("U7 palette combobox: role/controls/autocomplete/haspopup + listbox",
          aria["role"] == "combobox" and aria["controls"] == "palette-list"
          and aria["autocomplete"] == "list" and aria["haspopup"] == "listbox"
          and aria["listRole"] == "listbox")
    check("U7 palette options: role=option + ids + aria-selected + expanded/activedescendant",
          aria["n"] > 0 and aria["allOptions"] and aria["firstSel"] == "true"
          and aria["expanded"] == "true" and aria["active"] == "pal-opt-0")
    page.keyboard.press("ArrowDown")
    a1 = page.evaluate("""() => ({active: document.getElementById('palette-input').getAttribute('aria-activedescendant'),
        sel: (document.querySelector('#palette-list li[aria-selected="true"]') || {}).id})""")
    check("U7 ArrowDown moves aria-activedescendant + aria-selected",
          a1["active"] == "pal-opt-1" and a1["sel"] == "pal-opt-1")
    page.fill("#palette-input", "zzzznope")
    page.wait_for_timeout(150)
    empty = page.evaluate("""() => ({expanded: document.getElementById('palette-input').getAttribute('aria-expanded'),
        active: document.getElementById('palette-input').hasAttribute('aria-activedescendant')})""")
    check("U7 empty match list: aria-expanded=false, no activedescendant",
          empty["expanded"] == "false" and not empty["active"])
    page.keyboard.press("Escape")
    page.wait_for_selector("#palette-modal", state="hidden", timeout=5_000)


def f_r2_slash_parity(page, client) -> None:
    """U5/U6/U12: /ultrathink + /agentloop honor explicit on|off, /tone
    validates built-ins + custom tones, /think maps on->medium and cycles."""
    page.click("#rail [data-view='chat']")
    page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)
    wait_ready(page)
    cfg0 = client.get("/api/config")
    g0, a0 = cfg0.get("generation", {}), cfg0.get("agent", {})

    def cfg():
        return client.get("/api/config")

    def slash(cmd):
        # await refreshStatus too, so state.config is current before the next
        # command reads it (bare toggles / the effort ladder depend on it).
        page.evaluate("async (c) => { await handleSlash(c); await refreshStatus(); }", cmd)
        page.wait_for_timeout(120)

    try:
        slash("/ultrathink on")
        slash("/ultrathink on")
        check("U5 /ultrathink on is idempotent (stays true)", cfg()["generation"]["ultrathink"] is True)
        slash("/ultrathink off")
        check("U5 /ultrathink off turns it off", cfg()["generation"]["ultrathink"] is False)
        slash("/ultrathink")
        check("U5 bare /ultrathink toggles off->on", cfg()["generation"]["ultrathink"] is True)
        slash("/agentloop on")
        slash("/agentloop on")
        check("U5 /agentloop on is idempotent (stays true)", cfg()["agent"]["agent_loop"] is True)
        slash("/agentloop off")
        check("U5 /agentloop off turns it off", cfg()["agent"]["agent_loop"] is False)
        tone_before = cfg()["agent"].get("tone")
        slash("/tone bogusname")
        bad = page.evaluate("() => ({toast: document.getElementById('toasts').innerText,"
                            " input: document.getElementById('input').value})")
        check("U6 /tone rejects unknown tone with guidance + keeps input + leaves tone",
              "unknown tone" in bad["toast"] and bad["input"] == "/tone bogusname"
              and cfg()["agent"].get("tone") == tone_before)
        slash("/tone chill")
        check("U6 /tone chill (built-in) persists", cfg()["agent"].get("tone") == "chill")
        page.evaluate("""() => {
            state.config = state.config || {};
            state.config.agent = state.config.agent || {};
            state.config.agent.custom_tones = Object.assign({}, state.config.agent.custom_tones,
                {probe_tone: 'a custom probe tone'});
        }""")
        slash("/tone probe_tone")
        check("U6 /tone accepts an agent-created custom tone", cfg()["agent"].get("tone") == "probe_tone")
        slash("/think on")
        check("U12 /think on maps to medium", cfg()["generation"]["effort"] == "medium")
        slash("/think")
        check("U12 bare /think cycles the ladder (medium->high)", cfg()["generation"]["effort"] == "high")
        slash("/think bogus")
        bad2 = page.evaluate("() => document.getElementById('toasts').innerText")
        check("U12 /think rejects unknown level with usage",
              "usage: /think" in bad2 and cfg()["generation"]["effort"] == "high")
    finally:
        client.req("PATCH", "/api/config", {"generation": {
            "effort": g0.get("effort"), "ultrathink": g0.get("ultrathink")}})
        client.post("/api/config/agent", {"tone": a0.get("tone"), "agent_loop": a0.get("agent_loop"),
                                          "custom_tones": a0.get("custom_tones") or {}, "confirm": True})
        page.evaluate("async () => { await refreshStatus(); }")
        page.fill("#input", "")
        page.evaluate("() => { document.getElementById('toasts').innerHTML = ''; }")


def f_r2_approval_queue(page, client) -> None:
    """U4: answering one approval must not clobber the next queued
    approval's 'Waiting for approval' status (pollState skips run-state
    while streaming, and never resets state.streaming)."""
    page.click("#rail [data-view='chat']")
    page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)
    wait_ready(page)
    # R3 U8: an ID-AWARE stub. A blanket `200 {}` accepts any id, so a decision
    # routed to the WRONG approval (R3 U1) passed every assertion. Record what
    # was actually POSTed and 404 ids the live queue does not hold, exactly
    # like the bridge does.
    decided: list[dict] = []
    live = {"ids": []}

    def refresh_ids():
        live["ids"] = page.evaluate("() => state.approvalQueue.map(x => x.id)")

    def approval_stub(route):
        aid = route.request.url.rsplit("/", 1)[-1].split("?")[0]
        try:
            body = json.loads(route.request.post_data or "{}")
        except ValueError:
            body = {}
        decided.append({"id": aid, "decision": body.get("decision"),
                        "queued": list(live["ids"])})
        if aid in live["ids"]:
            route.fulfill(status=200, content_type="application/json", body="{}")
        else:
            route.fulfill(status=404, content_type="application/json",
                          body=json.dumps({"error": "no such approval"}))

    page.route("**/api/approval/*", approval_stub)
    try:
        page.evaluate("""() => {
            state.streaming = true;
            state.approvalQueue = [];
            document.querySelectorAll('.app-card').forEach(c => c.remove());
            queueApproval({id: 'r2a', tool: 'write_file', level: 'medium', risk: 4, mode: 'sandboxed', args: {path: 'a'}, reasons: []});
            queueApproval({id: 'r2b', tool: 'write_file', level: 'medium', risk: 4, mode: 'sandboxed', args: {path: 'b'}, reasons: []});
        }""")
        refresh_ids()
        page.wait_for_selector("#approval-modal:not([hidden])", timeout=5_000)
        st0 = page.evaluate("() => ({rs: document.getElementById('run-state').textContent, q: state.approvalQueue.length})")
        check("U4 two queued approvals: modal up, status Waiting, queue=2",
              st0["rs"] == "Waiting for approval" and st0["q"] == 2
              and live["ids"] == ["r2a", "r2b"])
        page.click("#ap-allow")
        page.wait_for_function("() => state.approvalQueue.length === 1", timeout=5_000)
        page.wait_for_timeout(150)
        refresh_ids()
        st1 = page.evaluate("""() => ({rs: document.getElementById('run-state').textContent,
            q: state.approvalQueue.length, modal: !document.getElementById('approval-modal').hidden})""")
        check("U4 after first decision: next approval keeps Waiting + modal + queue=1",
              st1["rs"] == "Waiting for approval" and st1["q"] == 1 and st1["modal"])
        check("U8 first decision POSTed the head id r2a with decision=approve",
              decided[-1]["id"] == "r2a" and decided[-1]["decision"] == "approve"
              and decided[-1]["queued"] == ["r2a", "r2b"] and live["ids"] == ["r2b"])
        page.click("#ap-deny")
        page.wait_for_function("() => state.approvalQueue.length === 0", timeout=5_000)
        page.wait_for_timeout(150)
        refresh_ids()
        st2 = page.evaluate("""() => ({rs: document.getElementById('run-state').textContent,
            modal: document.getElementById('approval-modal').hidden})""")
        check("U4 queue drained: status returns to Working (streaming), modal closed",
              st2["rs"] == "Working" and st2["modal"])
        check("U8 second decision POSTed the remaining id r2b with decision=deny",
              decided[-1]["id"] == "r2b" and decided[-1]["decision"] == "deny"
              and live["ids"] == [] and len(decided) == 2)
    finally:
        page.unroute("**/api/approval/*")
        page.evaluate("""() => {
            state.approvalQueue = []; state.streaming = false;
            document.querySelectorAll('.app-card').forEach(c => c.remove());
            document.getElementById('run-state').textContent = 'Ready';
        }""")


def f_r2_models_keyboard(page, client) -> None:
    """U8: model rows are keyboard-reachable (tabIndex=0) and Enter/Space
    fills the fit-detail panel without a mouse."""
    page.click("#rail [data-view='models']")
    page.wait_for_selector("#view-models:not([hidden])", timeout=10_000)
    page.wait_for_function("() => document.querySelectorAll('#models-table tbody tr').length > 0", timeout=20_000)
    probe = page.evaluate("""() => {
        const tr = document.querySelector('#models-table tbody tr');
        return {tab: tr.tabIndex, label: tr.getAttribute('aria-label'),
                detail: document.getElementById('model-detail').textContent};
    }""")
    check("U8 model row: tabIndex=0 + fit-details aria-label",
          probe["tab"] == 0 and (probe["label"] or "").startswith("Fit details for"))
    page.evaluate("() => document.querySelector('#models-table tbody tr').focus()")
    page.keyboard.press("Enter")
    page.wait_for_timeout(150)
    d1 = page.evaluate("() => document.getElementById('model-detail').textContent")
    name = page.evaluate("() => document.querySelector('#models-table tbody tr').getAttribute('aria-label').replace('Fit details for ', '')")
    check("U8 Enter on a model row fills fit details", d1.startswith(name))
    page.evaluate("() => { const r = document.querySelectorAll('#models-table tbody tr'); (r[1] || r[0]).focus(); }")
    page.keyboard.press("Space")
    page.wait_for_timeout(150)
    d2 = page.evaluate("() => document.getElementById('model-detail').textContent")
    check("U8 Space on a model row fills fit details", "—" in d2 and d2 != probe["detail"])
    page.click("#rail [data-view='chat']")
    page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)


def f_r2_drop_guard(page, client) -> None:
    """U14: a file drop never navigates away — it is refused with honest
    TUI-only guidance; plain-text drops into fields keep native behavior."""
    page.click("#rail [data-view='chat']")
    page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)
    probe = page.evaluate("""() => {
        const dt = new DataTransfer();
        dt.items.add(new File([new Uint8Array([1, 2, 3])], 'x.png', {type: 'image/png'}));
        const over = new DragEvent('dragover', {dataTransfer: dt, bubbles: true, cancelable: true});
        document.body.dispatchEvent(over);
        const drop = new DragEvent('drop', {dataTransfer: dt, bubbles: true, cancelable: true});
        document.body.dispatchEvent(drop);
        return {overPrevented: over.defaultPrevented, dropPrevented: drop.defaultPrevented,
                toast: document.getElementById('toasts').innerText};
    }""")
    check("U14 file dragover + drop prevented (no navigation)", probe["overPrevented"] and probe["dropPrevented"])
    check("U14 file drop refused with TUI-only guidance",
          "TUI-only" in probe["toast"] and "File drop" in probe["toast"])
    txt = page.evaluate("""() => {
        const dt = new DataTransfer();
        dt.setData('text/plain', 'hello');
        const drop = new DragEvent('drop', {dataTransfer: dt, bubbles: true, cancelable: true});
        document.getElementById('input').dispatchEvent(drop);
        return {prevented: drop.defaultPrevented, files: dt.files.length};
    }""")
    check("U14 plain-text drop into a field is NOT hijacked", not txt["prevented"] and txt["files"] == 0)
    page.evaluate("() => { document.getElementById('toasts').innerHTML = ''; }")


def f_r2_toast_placement(page, client) -> None:
    """U13: toasts anchor above the composer (tracking its live height),
    never overlapping Send or the + controls."""
    page.click("#rail [data-view='chat']")
    page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)
    page.evaluate("() => { syncComposerHeight(); toast('placement probe'); }")
    page.wait_for_selector("#toasts .toast", timeout=5_000)
    geo = page.evaluate("""() => {
        const t = document.querySelector('#toasts .toast').getBoundingClientRect();
        const c = document.querySelector('.composer-wrap').getBoundingClientRect();
        const cs = getComputedStyle(document.getElementById('toasts'));
        return {tb: t.bottom, ct: c.top, bottom: cs.bottom,
                composerH: getComputedStyle(document.documentElement).getPropertyValue('--composer-h').trim()};
    }""")
    check("U13 toast bottom sits above the composer top", geo["tb"] <= geo["ct"] + 1)
    check("U13 toasts bottom tracks --composer-h + 24px",
          abs(float(geo["bottom"].replace("px", "")) - (float(geo["composerH"].replace("px", "")) + 24)) <= 2)
    page.evaluate("() => { document.getElementById('toasts').innerHTML = ''; }")


def f_r2_bg3_contrast(page, client) -> None:
    """U9: secondary text on bg3 surfaces meets WCAG AA (>=4.5:1). The
    --mute token fails on bg3; the fixed surfaces must render --dim."""
    page.click("#rail [data-view='chat']")
    page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)
    probe = page.evaluate("""() => {
        const probeEl = document.createElement('div');
        probeEl.style.display = 'none';
        document.body.appendChild(probeEl);
        const cssColor = (v) => { probeEl.style.color = v; return getComputedStyle(probeEl).color; };
        const parse = (s) => s.replace(/[^0-9.,]/g, '').split(',').slice(0, 3).map(Number);
        const bg3 = parse(cssColor('var(--bg3)'));
        const dim = parse(cssColor('var(--dim)'));
        const mute = parse(cssColor('var(--mute)'));
        const lum = (c) => { const a = c.map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); });
            return 0.2126 * a[0] + 0.7152 * a[1] + 0.0722 * a[2]; };
        const ratio = (c1, c2) => { const l1 = lum(c1), l2 = lum(c2); return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05); };
        const host = document.createElement('div');
        host.style.display = 'none';
        host.innerHTML = '<div class="pop-list"><div class="sel"><span>pop</span></div></div>'
            + '<div class="modal palette"><ul><li class="sel"><code>c</code><small>pal</small></li></ul></div>'
            + '<div class="doctor"><span class="st skip">skip</span></div>';
        document.body.appendChild(host);
        const color = (sel) => parse(getComputedStyle(host.querySelector(sel)).color);
        const popSel = color('.pop-list div.sel span');
        const palSel = color('.modal.palette li.sel small');
        const skipEl = host.querySelector('.doctor .st.skip');
        const skipRatio = ratio(parse(getComputedStyle(skipEl).color), parse(getComputedStyle(skipEl).backgroundColor));
        let hoverDim = 0, hoverMute = 0;
        for (const sheet of document.styleSheets) {
            let rules; try { rules = sheet.cssRules; } catch (e) { continue; }
            for (const r of rules) {
                const sel = r.selectorText || '';
                const isHover = sel.endsWith(':hover span') || sel.endsWith(':hover small');
                const inScope = sel.includes('.menu') || sel.includes('.pop-list') || sel.includes('.palette');
                if (isHover && inScope) {
                    const c = r.style.color || '';
                    if (c.includes('--dim')) hoverDim++;
                    if (c.includes('--mute')) hoverMute++;
                }
            }
        }
        host.remove(); probeEl.remove();
        return {dimRatio: ratio(dim, bg3), muteRatio: ratio(mute, bg3),
                popRatio: ratio(popSel, bg3), palRatio: ratio(palSel, bg3), skipRatio,
                popIsDim: popSel.join() === dim.join(), palIsDim: palSel.join() === dim.join(),
                hoverDim, hoverMute};
    }""")
    check("U9 --dim token meets 4.5:1 on bg3 (and --mute does not)",
          probe["dimRatio"] >= 4.5 and probe["muteRatio"] < 4.5)
    check("U9 pop-list selected text on bg3 >= 4.5:1 (uses --dim)",
          probe["popRatio"] >= 4.5 and probe["popIsDim"])
    check("U9 palette selected hint on bg3 >= 4.5:1 (uses --dim)",
          probe["palRatio"] >= 4.5 and probe["palIsDim"])
    check("U9 doctor skip badge on bg3 >= 4.5:1", probe["skipRatio"] >= 4.5)
    check("U9 hover text rules use --dim, never --mute", probe["hoverDim"] >= 3 and probe["hoverMute"] == 0)


def f_r2_misc(page, client) -> None:
    """U16/U21/U22/U23/U24: link focus + honest provider hint, tab ARIA +
    roving tabindex, settings label->combobox focus, dead helper removed,
    pointercancel resets drag state."""
    page.click("#rail [data-view='chat']")
    page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)
    link = page.evaluate("""async () => {
        const p = promptModal('Probe', 'Docs: https://example.com/docs', false, '');
        const m = document.getElementById('prompt-modal');
        const a = m.querySelector('#pm-hint a');
        const foc = focusables(m);
        const res = {hasAnchor: !!a, href: a ? a.href : null, anchorFocusable: a ? foc.includes(a) : false,
                     target: a ? a.target : null, rel: a ? a.rel : null};
        document.getElementById('pm-cancel').click();
        await p;
        return res;
    }""")
    check("U16 promptModal linkifies genuine URL + anchor is focusable",
          link["hasAnchor"] and link["anchorFocusable"] and link["href"] == "https://example.com/docs"
          and link["target"] == "_blank" and "noopener" in link["rel"])
    nolink = page.evaluate("""async () => {
        const p = promptModal('Base URL', 'OpenAI-compatible base URL — the endpoint root, usually ending in /v1', false, '');
        const a = document.querySelector('#prompt-modal #pm-hint a');
        const res = {hasAnchor: !!a};
        document.getElementById('pm-cancel').click();
        await p;
        return res;
    }""")
    check("U16 provider base-URL hint has no dead 'open' link", not nolink["hasAnchor"])
    tabs = page.evaluate("""() => {
        const ts = Array.from(document.querySelectorAll('#tabs .tab'));
        const panel = document.getElementById('view-chat');
        return {n: ts.length, allControls: ts.every(t => t.getAttribute('aria-controls') === 'view-chat'),
                panelRole: panel.getAttribute('role'), panelLabel: panel.getAttribute('aria-label')};
    }""")
    check("U21 chat tabs aria-controls=view-chat + panel is a labelled tabpanel",
          tabs["n"] > 0 and tabs["allControls"] and tabs["panelRole"] == "tabpanel" and bool(tabs["panelLabel"]))
    rov = page.evaluate("""() => {
        const ts = Array.from(document.querySelectorAll('#tabs .tab'));
        if (ts.length < 2) return null;
        ts[0].focus();
        ts[0].dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowRight', bubbles: true}));
        return {first: ts[0].tabIndex, second: ts[1].tabIndex, focused: document.activeElement === ts[1]};
    }""")
    check("U21 ArrowRight moves focus + roving tabindex to the next tab",
          rov is not None and rov["first"] == -1 and rov["second"] == 0 and rov["focused"])
    check("U23 unused openSidebarNarrow helper removed",
          page.evaluate("() => typeof openSidebarNarrow === 'undefined'"))
    pc = page.evaluate("""() => {
        tabDrag = {x: 0, left: 0, id: 1}; tabDragged = true;
        window.dispatchEvent(new PointerEvent('pointercancel'));
        return {drag: tabDrag, dragged: tabDragged};
    }""")
    check("U24 pointercancel resets tabDrag + tabDragged", pc["drag"] is None and pc["dragged"] is False)
    page.click("#rail [data-view='settings']")
    page.wait_for_selector("#view-settings:not([hidden])", timeout=10_000)
    page.wait_for_selector("#set-generation-temperature", timeout=10_000)
    lab = page.evaluate("""() => {
        const row = Array.from(document.querySelectorAll('#settings-form .set-row'))
            .find(r => r.querySelector('om-listbox') && r.querySelector('label'));
        if (!row) return null;
        const btn = row.querySelector('om-listbox .dd-btn');
        row.querySelector('label').click();
        return {focused: document.activeElement === btn};
    }""")
    check("U22 settings label click focuses the combobox button", lab is not None and lab["focused"])
    page.click("#rail [data-view='chat']")
    page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)


def f_r2_div_buttons(page, client) -> None:
    """U17: sidebar rows and market cards expose button semantics and
    respond to Enter/Space without a mouse."""
    page.click("#rail [data-view='chat']")
    page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)
    sb = page.evaluate("""() => {
        const row = document.querySelector('#sb-list .sb-row');
        return row ? {role: row.getAttribute('role'), tab: row.tabIndex} : null;
    }""")
    check("U17 sidebar row: role=button + tabIndex=0", sb is not None and sb["role"] == "button" and sb["tab"] == 0)
    page.click("#rail [data-view='market']")
    page.wait_for_selector("#view-market:not([hidden])", timeout=10_000)
    page.wait_for_function("() => document.querySelectorAll('.market-card').length > 0", timeout=20_000)
    mc = page.evaluate("""() => {
        const c = document.querySelector('.market-card');
        return {role: c.getAttribute('role'), tab: c.tabIndex};
    }""")
    check("U17 market card: role=button + tabIndex=0", mc["role"] == "button" and mc["tab"] == 0)
    page.evaluate("() => document.querySelector('.market-card').focus()")
    page.keyboard.press("Space")
    page.wait_for_selector("#confirm-modal:not([hidden])", timeout=5_000)
    check("U17 Space on a market card opens the download confirm",
          page.is_visible("#confirm-modal")
          and page.evaluate("() => document.getElementById('confirm-modal').contains(document.activeElement)"))
    page.keyboard.press("Escape")
    page.wait_for_selector("#confirm-modal", state="hidden", timeout=5_000)
    page.click("#rail [data-view='chat']")
    page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)


# --------------------------------------------------------------------------
# R3 UX regression flows (task #65). Live PoCs for the findings that only a
# real browser can prove: U1 consent routing, U2 rendered-surface contrast,
# U3 server-truth toggle, U4 cast args end-to-end, U5/U6 case parity, U10 hero.
# --------------------------------------------------------------------------
# Measure a REAL element against its REAL composited background: walk every
# translucent ancestor down to an opaque base, then apply the WCAG formula.
LIVE_CONTRAST_JS = """(sels) => {
    const parse = (s) => {
        const m = (s || '').match(/[\\d.]+/g);
        if (!m) return {r: 0, g: 0, b: 0, a: 0};
        const n = m.map(Number);
        return {r: n[0], g: n[1], b: n[2], a: n.length > 3 ? n[3] : 1};
    };
    const over = (f, b) => ({r: f.a * f.r + (1 - f.a) * b.r,
                             g: f.a * f.g + (1 - f.a) * b.g,
                             b: f.a * f.b + (1 - f.a) * b.b, a: 1});
    const lum = (c) => {
        const a = [c.r, c.g, c.b].map(v => { v /= 255;
            return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); });
        return 0.2126 * a[0] + 0.7152 * a[1] + 0.0722 * a[2];
    };
    const ratio = (x, y) => { const l1 = lum(x), l2 = lum(y);
        return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05); };
    const bgOf = (el) => {
        const stack = [];
        for (let n = el; n; n = n.parentElement) {
            const c = parse(getComputedStyle(n).backgroundColor);
            if (c.a > 0) stack.push(c);
            if (c.a >= 1) break;
        }
        const root = parse(getComputedStyle(document.documentElement).backgroundColor);
        let acc = root.a >= 1 ? root : {r: 10, g: 12, b: 14, a: 1};
        for (let i = stack.length - 1; i >= 0; i--) acc = over(stack[i], acc);
        return acc;
    };
    const probe = document.createElement('div');
    probe.style.display = 'none';
    document.body.appendChild(probe);
    const token = (n) => { probe.style.color = 'var(--' + n + ')';
        return parse(getComputedStyle(probe).color); };
    const dim = token('dim'), mute = token('mute'), bg3 = token('bg3');
    const out = {dimOnBg3: ratio(dim, bg3), muteOnBg3: ratio(mute, bg3), surfaces: []};
    for (const sel of sels) {
        const el = document.querySelector(sel);
        if (!el) { out.surfaces.push({sel: sel, missing: true, ratio: 0, isDim: false}); continue; }
        const cs = getComputedStyle(el);
        const fg = parse(cs.color);
        const bg = bgOf(el);
        const px = parseFloat(cs.fontSize);
        const large = px >= 24 || (parseInt(cs.fontWeight, 10) >= 700 && px >= 18.66);
        out.surfaces.push({sel: sel, missing: false,
            ratio: ratio(fg.a >= 1 ? fg : over(fg, bg), bg),
            fg: cs.color, px: px, threshold: large ? 3 : 4.5,
            bg: 'rgb(' + Math.round(bg.r) + ', ' + Math.round(bg.g) + ', ' + Math.round(bg.b) + ')',
            isDim: fg.r === dim.r && fg.g === dim.g && fg.b === dim.b,
            text: (el.textContent || '').trim().slice(0, 40)});
    }
    probe.remove();
    return out;
}"""


def f_r3_inline_card_id(page, client) -> None:
    """R3 U1 (High): an inline approval-card button must decide ITS OWN card.

    PoC: queue A then B, then activate "Allow once" on card B the way assistive
    tech does (a synthetic DOM click). The scrim blocks ordinary pointer clicks,
    but the background used to be fully reachable, so the decision went to the
    queue HEAD: the user consented to B and the bridge approved A.
    """
    page.click("#rail [data-view='chat']")
    page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)
    wait_ready(page)
    decided: list[dict] = []
    live = {"ids": []}

    def stub(route):
        aid = route.request.url.rsplit("/", 1)[-1].split("?")[0]
        try:
            body = json.loads(route.request.post_data or "{}")
        except ValueError:
            body = {}
        decided.append({"id": aid, "decision": body.get("decision")})
        if aid in live["ids"]:
            route.fulfill(status=200, content_type="application/json", body="{}")
        else:
            route.fulfill(status=404, content_type="application/json",
                          body=json.dumps({"error": "no such approval"}))

    page.route("**/api/approval/*", stub)
    try:
        page.evaluate("""() => {
            state.streaming = true;
            state.approvalQueue = [];
            document.querySelectorAll('.app-card').forEach(c => c.remove());
            queueApproval({id: 'r3a', tool: 'write_file', level: 'medium', risk: 4,
                           mode: 'sandboxed', args: {path: 'a.txt'}, reasons: []});
            queueApproval({id: 'r3b', tool: 'write_file', level: 'medium', risk: 4,
                           mode: 'sandboxed', args: {path: 'b.txt'}, reasons: []});
        }""")
        page.wait_for_selector("#approval-modal:not([hidden])", timeout=5_000)
        live["ids"] = page.evaluate("() => state.approvalQueue.map(x => x.id)")
        bg = page.evaluate("""() => {
            const main = document.getElementById('main'), sb = document.getElementById('sidebar');
            const b = document.querySelector('.app-card[data-aid="r3b"] button[data-dec="approve"]');
            b.focus();
            return {mainInert: main.inert, mainAria: main.getAttribute('aria-hidden'),
                    sbInert: sb.inert, focusReached: document.activeElement === b};
        }""")
        check("U1 consent modal makes the background inert + aria-hidden (no AT reach)",
              bg["mainInert"] and bg["mainAria"] == "true" and bg["sbInert"]
              and not bg["focusReached"])
        page.evaluate("""() => {
            const b = document.querySelector('.app-card[data-aid="r3b"] button[data-dec="approve"]');
            b.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
        }""")
        page.wait_for_function("() => state.approvalQueue.length === 1", timeout=5_000)
        page.wait_for_timeout(200)
        live["ids"] = page.evaluate("() => state.approvalQueue.map(x => x.id)")
        st = page.evaluate("""() => {
            const card = (id) => {
                const c = document.querySelector(`.app-card[data-aid="${id}"]`);
                return c ? {state: c.querySelector('.state').textContent,
                            decided: c.classList.contains('decided'),
                            disabled: Array.from(c.querySelectorAll('button')).every(b => b.disabled)}
                         : {state: '', decided: false, disabled: false};
            };
            return {a: card('r3a'), b: card('r3b'),
                    modalArgs: document.getElementById('ap-args').textContent,
                    queue: state.approvalQueue.map(x => x.id)};
        }""")
        print("PROBE U1 inline-card decision: " + json.dumps(
            {"posted": decided, "queue": st["queue"], "a": st["a"]["state"],
             "b": st["b"]["state"]}, ensure_ascii=False), flush=True)
        check("U1 inline 'Allow once' on card B POSTs /api/approval/r3b (approve)",
              bool(decided) and decided[-1]["id"] == "r3b"
              and decided[-1]["decision"] == "approve")
        check("U1 card B is the one marked allowed; card A still waits",
              st["b"]["decided"] and "allowed" in st["b"]["state"] and st["b"]["disabled"]
              and not st["a"]["decided"] and "waiting" in st["a"]["state"])
        check("U1 the decided id is dropped, the untouched request stays queued",
              st["queue"] == ["r3a"] and "a.txt" in st["modalArgs"])
        page.click("#ap-deny")
        page.wait_for_function("() => state.approvalQueue.length === 0", timeout=5_000)
        page.wait_for_timeout(200)
        check("U1 modal Deny then decides the remaining head r3a",
              decided[-1]["id"] == "r3a" and decided[-1]["decision"] == "deny"
              and len(decided) == 2)
        released = page.evaluate("""() => ({
            mainInert: document.getElementById('main').inert,
            mainAria: document.getElementById('main').getAttribute('aria-hidden'),
            modal: document.getElementById('approval-modal').hidden})""")
        check("U1 background inert released once the consent queue drains",
              released["modal"] and not released["mainInert"]
              and released["mainAria"] != "true")
    finally:
        page.unroute("**/api/approval/*")
        page.evaluate("""() => {
            state.approvalQueue = []; state.streaming = false;
            document.querySelectorAll('.app-card').forEach(c => c.remove());
            closeModal('#approval-modal');
            document.getElementById('run-state').textContent = 'Ready';
        }""")


def f_r3_live_contrast(page, client) -> None:
    """R3 U2: WCAG AA on the REAL rendered surfaces, not synthetic hosts.

    .sb-row.active .prev / time sit on bg3; .chip.active .cnt sits on the
    translucent acc-dim overlay. --mute measures 4.36:1 / ~4.0:1 there.
    """
    page.click("#rail [data-view='chat']")
    page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)
    page.evaluate("async () => { await renderSidebar(); }")
    page.wait_for_selector("#sb-list .sb-row.active", timeout=10_000)
    sb = page.evaluate(LIVE_CONTRAST_JS,
                       ["#sb-list .sb-row.active .prev", "#sb-list .sb-row.active time"])
    print("PROBE U2 live sidebar contrast: " + json.dumps(sb, ensure_ascii=False), flush=True)
    check("U2 premise: --mute fails AA on bg3 while --dim passes",
          sb["muteOnBg3"] < 4.5 <= sb["dimOnBg3"])
    prev = next((s for s in sb["surfaces"] if s["sel"].endswith(".prev")), {})
    clock = next((s for s in sb["surfaces"] if s["sel"].endswith("time")), {})
    check("U2 live .sb-row.active .prev meets AA on bg3 (renders --dim)",
          not prev.get("missing", True) and prev.get("ratio", 0) >= 4.5 and prev.get("isDim"))
    check("U2 live .sb-row.active time meets AA on bg3 (renders --dim)",
          not clock.get("missing", True) and clock.get("ratio", 0) >= 4.5 and clock.get("isDim"))
    page.click("#rail [data-view='market']")
    page.wait_for_selector("#view-market:not([hidden])", timeout=10_000)
    page.wait_for_function("() => document.querySelectorAll('.market-card').length > 0", timeout=20_000)
    page.click("#market-head .chip")
    page.wait_for_selector("#market-head .chip.active .cnt", timeout=5_000)
    mk = page.evaluate(LIVE_CONTRAST_JS, ["#market-head .chip.active .cnt"])
    print("PROBE U2 live market chip contrast: " + json.dumps(mk, ensure_ascii=False), flush=True)
    cnt = (mk["surfaces"] or [{}])[0]
    check("U2 live .chip.active .cnt meets AA on the acc-dim composite (renders --dim)",
          not cnt.get("missing", True) and cnt.get("ratio", 0) >= 4.5 and cnt.get("isDim"))
    page.click("#rail [data-view='chat']")
    page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)


def f_r3_yolo_truth(page, client) -> None:
    """R3 U3: bare /yolo must toggle from SERVER truth, never a stale local flag.

    Report PoC: the server is already ON while the GUI's own state.yolo is
    stale/undefined, so a bare /yolo re-ARMED for a user who meant to disarm.
    The divergence is injected exactly as the PoC describes.

    Scope note: a SEPARATE pre-existing server defect (verified at f8d6952 too)
    means an out-of-band arm cannot survive the next status poll — /api/status
    calls bridge._sync_disk_config(), which reloads config.toml, and agent.yolo
    is Field(exclude=True) (never persisted). That is bridge/core ownership and
    is reported separately; this flow pins the report's minimal fix, i.e. the
    GUI's decision source, and still round-trips every POST to the real bridge.
    """
    page.click("#rail [data-view='chat']")
    page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)
    wait_ready(page)
    posts: list[dict] = []

    def capture(route):
        try:
            posts.append(json.loads(route.request.post_data or "{}"))
        except ValueError:
            posts.append({})
        route.continue_()

    y0 = bool(client.get("/api/status").get("yolo"))
    page.route("**/api/config/agent", capture)
    try:
        check("U3 /api/status carries yolo (server truth is observable)",
              "yolo" in client.get("/api/status"))
        # PoC divergence: server truth ON, the GUI's own flag stale OFF.
        page.evaluate("""() => { state.yolo = false; state.status.yolo = true;
            document.getElementById('toasts').innerHTML = ''; }""")
        page.evaluate("async () => { await handleSlash('/yolo'); }")
        page.wait_for_timeout(250)
        said = page.text_content("#toasts")
        print(f"PROBE U3 bare /yolo while status ON: posted={posts[-1] if posts else None}"
              f" toast={said[:60]!r}", flush=True)
        check("U3 bare /yolo DISARMS when server truth is ON (posts yolo:false)",
              bool(posts) and posts[-1].get("yolo") is False
              and posts[-1].get("confirm") is True)
        check("U3 the toast reports the real transition (yolo off)", "yolo off" in said)
        check("U3 the decision is mirrored into state.status (next toggle stays honest)",
              page.evaluate("() => state.status.yolo") is False)
        page.evaluate("() => { document.getElementById('toasts').innerHTML = ''; }")
        page.evaluate("async () => { await handleSlash('/yolo'); }")
        page.wait_for_timeout(250)
        check("U3 the next bare /yolo re-arms (the toggle stays honest)",
              posts[-1].get("yolo") is True and "yolo on" in page.text_content("#toasts"))
        page.evaluate("async () => { await handleSlash('/yolo OFF'); }")
        page.wait_for_timeout(250)
        check("U3 explicit /yolo OFF wins over the toggle (case-insensitive)",
              posts[-1].get("yolo") is False)
        page.evaluate("async () => { await handleSlash('/yolo on'); }")
        page.wait_for_timeout(250)
        check("U3 explicit /yolo on wins over the toggle", posts[-1].get("yolo") is True)
        page.evaluate("async () => { await refreshStatus(); }")
        page.wait_for_timeout(250)
        check("U3 refreshStatus re-syncs the mirror from /api/status",
              page.evaluate("() => state.status.yolo")
              == bool(client.get("/api/status").get("yolo")))
    finally:
        page.unroute("**/api/config/agent")
        client.post("/api/config/agent", {"yolo": y0, "confirm": True})
        page.evaluate("async () => { await refreshStatus(); }")
        page.evaluate("() => { document.getElementById('toasts').innerHTML = ''; }")


def f_r3_skill_cast_args(page, client) -> None:
    """R3 U4: /skill:name <instruction> must carry the instruction end-to-end.

    PoC: the GUI POSTed only {name, session}; the trailing instruction the docs
    promise was dropped before it ever reached the bridge.
    """
    page.click("#rail [data-view='chat']")
    page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)
    wait_ready(page)
    skill_dir = HOME / "skills" / "r3-cast-probe"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: r3-cast-probe\ndescription: R3 cast probe.\n---\n\n# Probe\n\nDo the probe.\n",
        encoding="utf-8")
    sid = page.evaluate("() => state.session")
    if not sid:
        sid = client.post("/api/chats", {"first_message": ""})["id"]
        page.evaluate("async (s) => { await openChat(s); }", sid)
    bodies: list[dict] = []

    def capture(route):
        try:
            bodies.append(json.loads(route.request.post_data or "{}"))
        except ValueError:
            bodies.append({})
        route.continue_()

    page.route("**/api/skills/cast", capture)
    try:
        page.evaluate("async () => { await handleSlash('/skill:r3-cast-probe run it for this repo'); }")
        page.wait_for_timeout(500)
        print("PROBE U4 cast body: " + json.dumps(bodies[-1] if bodies else None,
                                                 ensure_ascii=False), flush=True)
        check("U4 GUI cast POSTs the additive args string (name/session unchanged)",
              bool(bodies) and bodies[-1].get("args") == "run it for this repo"
              and bodies[-1].get("name") == "r3-cast-probe"
              and bodies[-1].get("session") == sid)
        msgs = client.get(f"/api/chats/{sid}").get("messages", [])
        text = msgs[-1]["content"] if msgs else ""
        check("U4 the instruction reaches the session transcript",
              "Do the probe." in text and "run it for this repo" in text)
        page.evaluate("async () => { await handleSlash('/skill:r3-cast-probe'); }")
        page.wait_for_timeout(500)
        msgs = client.get(f"/api/chats/{sid}").get("messages", [])
        legacy = msgs[-1]["content"] if msgs else ""
        check("U4 an empty instruction keeps the legacy cast message byte-identical",
              bodies[-1].get("args") == ""
              and legacy.endswith("Apply this skill to my requests in this chat.")
              and "Additional instruction" not in legacy)
    finally:
        page.unroute("**/api/skills/cast")
        page.evaluate("() => { document.getElementById('toasts').innerHTML = ''; }")


def f_r3_mode_tone_case(page, client) -> None:
    """R3 U5/U6: /mode SAFE and /tone TERSE must apply — plain parity.

    PoC: handleSlash lowercased the command but not the argument, so "/mode SAFE"
    opened the chooser and "/tone Friendly" was rejected as unknown.
    """
    page.click("#rail [data-view='chat']")
    page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)
    wait_ready(page)
    mode0 = client.get("/api/status").get("mode") or "sandboxed"
    tone0 = (client.get("/api/config").get("agent") or {}).get("tone") or "default"
    try:
        page.evaluate("async () => { await handleSlash('/mode SAFE'); }")
        page.wait_for_timeout(500)
        check("U5 /mode SAFE applies the mode instead of opening the chooser",
              page.is_hidden("#mode-modal")
              and client.get("/api/status").get("mode") == "safe")
        check("U5 the mode chip reflects the applied mode",
              "safe" in (page.text_content("#chip-mode") or "").lower())
        page.evaluate("async () => { await handleSlash('/tone TERSE'); }")
        page.wait_for_timeout(500)
        t = page.evaluate("""() => ({toast: document.getElementById('toasts').innerText,
            input: document.getElementById('input').value})""")
        check("U6 /tone TERSE is accepted case-insensitively and persisted",
              (client.get("/api/config").get("agent") or {}).get("tone") == "terse"
              and "unknown tone" not in t["toast"] and t["input"] == "")
        page.evaluate("async () => { await handleSlash('/tone Friendly'); }")
        page.wait_for_timeout(500)
        check("U6 /tone Friendly (mixed case) also applies",
              (client.get("/api/config").get("agent") or {}).get("tone") == "friendly")
    finally:
        page.evaluate("async (m) => { await handleSlash('/mode ' + m); }", mode0)
        page.evaluate("async (t) => { await handleSlash('/tone ' + t); }", tone0)
        page.wait_for_timeout(400)
        page.evaluate("() => { document.getElementById('toasts').innerHTML = ''; }")


def f_r3_reject_hero(page, client) -> None:
    """R3 U10: a rejected FIRST send must repaint the hero, not a blank stream.

    PoC: on an empty chat, reject /api/message; the phantom user bubble was
    removed and nothing repainted the .stream-empty hero (streamHtmlLen=0).
    """
    page.click("#rail [data-view='chat']")
    page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)
    wait_ready(page)
    fresh = client.post("/api/chats", {"first_message": ""})["id"]
    page.evaluate("async (s) => { await openChat(s); }", fresh)
    page.wait_for_function("() => !!document.querySelector('#stream .stream-empty')", timeout=10_000)
    page.route("**/api/message", lambda route: route.fulfill(
        status=400, content_type="application/json",
        body=json.dumps({"error": "busy — R3 hero probe rejection"})))
    try:
        send(page, "hello world")
        page.wait_for_function("() => document.getElementById('input').value === 'hello world'",
                               timeout=15_000)
        page.wait_for_timeout(300)
        st = page.evaluate("""() => {
            const hero = document.querySelector('#stream .stream-empty');
            return {msgs: document.querySelectorAll('#stream .msg').length,
                    hero: !!hero,
                    heroVisible: !!hero && getComputedStyle(hero).display !== 'none'
                        && hero.getBoundingClientRect().height > 0,
                    html: document.getElementById('stream').innerHTML.trim().length,
                    draft: state.drafts.get(state.session)};
        }""")
        print("PROBE U10 rejected first send: " + json.dumps(st, ensure_ascii=False), flush=True)
        check("U10 rejected first send: no phantom bubble, draft restored",
              st["msgs"] == 0 and st["draft"] == "hello world")
        check("U10 rejected first send repaints the hero (never a blank stream)",
              st["hero"] and st["heroVisible"] and st["html"] > 0)
        page.screenshot(path=str(VALIDATION / "gui-r3-u10-hero.png"))
        check("screenshot saved: .validation/gui-r3-u10-hero.png",
              (VALIDATION / "gui-r3-u10-hero.png").exists())
    finally:
        page.unroute("**/api/message")
        page.fill("#input", "")
        page.evaluate("() => { document.getElementById('toasts').innerHTML = ''; }")
        qid = page.evaluate("() => (state.chats.find(c => (c.title || '').includes('quartz')) || {}).id")
        if qid:
            page.evaluate("async (s) => { await openChat(s); }", qid)


def flows_responsive(page, width: int) -> None:
    def f():
        wait_boot(page)
        boot = page.evaluate("""() => ({
            off: document.getElementById('conn-dot').classList.contains('off'),
            rs: document.getElementById('run-state').textContent,
            status: !!state.status,
        })""")
        check(f"{width}px: boot + health",
              not boot["off"] and boot["rs"] == "Ready" and boot["status"])
        check(f"{width}px: composer visible", page.is_visible("#composer"))
        if width <= 900:
            check(f"{width}px: sidebar starts closed (overlay mode)",
                  page.evaluate("() => !document.getElementById('sidebar').classList.contains('open')"))
            page.click("#btn-sidebar")
            page.wait_for_selector("#sidebar.open", timeout=5000)
            page.wait_for_timeout(250)  # let the .22s slide-in settle
            ov = page.evaluate("""() => {
                const sb = document.getElementById('sidebar');
                const cs = getComputedStyle(sb);
                return {open: sb.classList.contains('open'), inert: sb.inert,
                        aria: sb.getAttribute('aria-hidden'), vis: cs.visibility,
                        pos: cs.position, left: sb.getBoundingClientRect().left};
            }""")
            check(f"{width}px: sidebar toggles open as overlay",
                  ov["open"] and not ov["inert"] and ov["aria"] == "false"
                  and ov["vis"] == "visible" and ov["pos"] == "fixed" and ov["left"] >= -1)
            page.click("#btn-sidebar")
            page.wait_for_timeout(200)
            # K7: a closed overlay sidebar must be inert + visibility:hidden,
            # not merely off-screen — unreachable by focus and the a11y tree.
            page.wait_for_timeout(350)
            closed = page.evaluate("""() => {
                const sb = document.getElementById('sidebar');
                document.getElementById('sb-search').focus();
                return {inert: sb.inert, aria: sb.getAttribute('aria-hidden'),
                        vis: getComputedStyle(sb).visibility,
                        focusId: document.activeElement.id,
                        inSidebar: sb.contains(document.activeElement)};
            }""")
            check(f"{width}px: closed sidebar inert + aria-hidden + visibility:hidden",
                  closed["inert"] and closed["aria"] == "true" and closed["vis"] == "hidden")
            check(f"{width}px: closed sidebar refuses focus (inert)",
                  not closed["inSidebar"] and closed["focusId"] != "sb-search")
            page.click("#btn-sidebar")
            page.wait_for_selector("#sidebar.open", timeout=5000)
            page.wait_for_timeout(250)
            opened = page.evaluate("""() => {
                const sb = document.getElementById('sidebar');
                document.getElementById('sb-search').focus();
                return {inert: sb.inert, aria: sb.getAttribute('aria-hidden'),
                        vis: getComputedStyle(sb).visibility, focusId: document.activeElement.id};
            }""")
            check(f"{width}px: reopened sidebar reachable (not inert, focus works)",
                  not opened["inert"] and opened["aria"] == "false"
                  and opened["vis"] == "visible" and opened["focusId"] == "sb-search")
            # U15: an outside pointerdown dismisses the overlay sidebar (the
            # main area is never part of #sidebar / #btn-sidebar).
            page.evaluate("""() => {
                const el = document.getElementById('main') || document.body;
                el.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true, cancelable: true}));
            }""")
            page.wait_for_timeout(300)
            check(f"{width}px: outside pointerdown dismisses overlay sidebar (U15)",
                  page.evaluate("() => !document.getElementById('sidebar').classList.contains('open')"))
            page.click("#btn-sidebar")
            page.wait_for_selector("#sidebar.open", timeout=5000)
            page.wait_for_timeout(200)
            # R3 U11: focus a descendant first — closing must hand focus back to
            # the toggle, never strand it on <body> (inert blurs the descendant).
            page.evaluate("() => document.getElementById('sb-search').focus()")
            page.keyboard.press("Escape")
            page.wait_for_timeout(350)
            check(f"{width}px: Escape dismisses overlay sidebar (U15)",
                  page.evaluate("() => !document.getElementById('sidebar').classList.contains('open')"))
            u11 = page.evaluate("""() => ({
                id: document.activeElement.id, tag: document.activeElement.tagName,
                inSidebar: document.getElementById('sidebar').contains(document.activeElement),
                pressed: document.getElementById('btn-sidebar').getAttribute('aria-pressed'),
            })""")
            check(f"{width}px: sidebar close restores focus to the toggle (U11)",
                  u11["id"] == "btn-sidebar" and not u11["inSidebar"]
                  and u11["tag"] != "BODY" and u11["pressed"] == "false")
        else:
            check(f"{width}px: sidebar visible by default", page.is_visible("#sidebar"))
        # task #29: persistent left rail (icon-only collapse on narrow)
        check(f"{width}px: rail present with all 9 views",
              page.evaluate("() => document.querySelectorAll('#rail .rail-btn[data-view]').length") == 9)
        if width <= 900:
            check(f"{width}px: rail collapses to icon-only",
                  page.evaluate("() => getComputedStyle(document.querySelector('#rail .rail-lbl')).display") == "none")
        else:
            check(f"{width}px: rail shows icon + label",
                  page.evaluate("() => getComputedStyle(document.querySelector('#rail .rail-lbl')).display") != "none")
        # non-chat views use the full main width (fluid, capped at 1800px)
        page.click("#rail [data-view='settings']")
        page.wait_for_selector("#view-settings:not([hidden])", timeout=10_000)
        page.wait_for_selector("#set-generation-temperature", timeout=10_000)
        w = page.evaluate("""() => {
            const p = document.getElementById('view-settings'), m = document.getElementById('main');
            const cs = getComputedStyle(p);
            return {inner: p.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight),
                    main: m.clientWidth, panel: p.clientWidth};
        }""")
        print(f"PROBE {width}px settings width: inner={w['inner']:.0f} main={w['main']:.0f} panel={w['panel']:.0f}", flush=True)
        if width == 1720:
            check(f"{width}px: settings spans >= 90% of main width", w["inner"] >= 0.9 * w["main"])
        if width >= 2560:
            check(f"{width}px: panel width capped at ~1800px", w["panel"] <= 1801)
        page.click("#rail [data-view='chat']")
        page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)
        check(f"{width}px: no horizontal overflow",
              page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1"))
        if width <= 620:
            # U11: key chips wrap inside the narrow key column, never bleed out
            page.click("#rail [data-view='help']")
            page.wait_for_selector("#view-help:not([hidden])", timeout=10_000)
            page.wait_for_function("() => !!document.querySelector('#help-body table')", timeout=20_000)
            kbd = page.evaluate("""() => {
                const cells = Array.from(document.querySelectorAll('#help-body td:first-child'));
                const bad = cells.filter(td => {
                    const k = td.querySelector('kbd');
                    return k && k.getBoundingClientRect().right > td.getBoundingClientRect().right + 1;
                }).length;
                return {cells: cells.length, bad};
            }""")
            check(f"{width}px: help kbd chips fit their cells (U11)", kbd["cells"] > 0 and kbd["bad"] == 0)
            # U10: the market grid track stays inside a <280px container
            page.click("#rail [data-view='market']")
            page.wait_for_selector("#view-market:not([hidden])", timeout=10_000)
            page.wait_for_function("() => document.querySelectorAll('.market-card').length > 0", timeout=20_000)
            mg = page.evaluate("""() => {
                const g = document.getElementById('market-grid');
                const gr = g.getBoundingClientRect();
                const cards = Array.from(g.querySelectorAll('.market-card'));
                return {sw: g.scrollWidth, cw: g.clientWidth, n: cards.length,
                        out: cards.filter(c => c.getBoundingClientRect().right > gr.right + 1).length};
            }""")
            check(f"{width}px: market grid fits without overflow (U10)",
                  mg["n"] > 0 and mg["sw"] <= mg["cw"] + 1 and mg["out"] == 0)
            page.click("#rail [data-view='chat']")
            page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)
            # U13: toasts anchor above the composer, never over Send
            page.evaluate("() => { syncComposerHeight(); toast('placement probe'); }")
            page.wait_for_selector("#toasts .toast", timeout=5_000)
            tp = page.evaluate("""() => {
                const t = document.querySelector('#toasts .toast').getBoundingClientRect();
                const c = document.querySelector('.composer-wrap').getBoundingClientRect();
                return {tb: t.bottom, ct: c.top};
            }""")
            check(f"{width}px: toast sits above the composer (U13)", tp["tb"] <= tp["ct"] + 1)
            page.evaluate("() => { document.getElementById('toasts').innerHTML = ''; }")
    flow(f"responsive-{width}", f)


# content gates for the per-view capture pass (task #29/#30)
VIEW_READY = {
    "models": "() => document.querySelectorAll('#models-table tbody tr').length > 0",
    "market": "() => document.querySelectorAll('.market-card').length > 0",
    "settings": "() => !!document.getElementById('set-generation-temperature')",
    "help": "() => !!document.querySelector('#help-body table')",
}


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
    shot = {"narrow": "gui-narrow.png", "tiny": "gui-tiny.png", "tablet": "gui-tablet.png",
            "laptop": "gui-laptop.png", "desktop": "gui-desktop.png", "wide": "gui-wide.png"}
    contexts = []
    try:
        # Seed a searchable session; it becomes the bridge's active session.
        seeded = client.post("/api/chats", {"first_message": "quartz falcon migration notes"})
        check("seed session created via /api/chats", bool(seeded.get("id")))

        with sync_playwright() as pw:
            for label, width in WIDTHS.items():
                ctx = page = None
                try:
                    ctx, page, profile = open_gui(pw, client.port, client.token, width)
                    check(f"{width}px: Chromium sandbox retained (no --no-sandbox)",
                          sandbox_proof(profile))
                    contexts.append(ctx)
                    if label == "laptop":
                        flows_laptop(page, client)
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
                    # capture pass: every key view at every width (chat above
                    # keeps its legacy name; the rest are gui-{label}-{view})
                    for v in ["models", "market", "settings", "help"]:
                        page.click(f"#rail [data-view='{v}']")
                        page.wait_for_selector(f"#view-{v}:not([hidden])", timeout=10_000)
                        page.wait_for_function(VIEW_READY[v], timeout=20_000)
                        page.wait_for_timeout(450)
                        if v == "market":
                            # K9: give the fit bars a real VRAM budget so the
                            # capture shows varied CSSOM widths, not all 0%
                            page.evaluate("() => { if (marketData && !marketData.vram_bytes)"
                                          " { marketData.vram_bytes = 24 * 2 ** 30; paintMarket(); } }")
                            page.wait_for_timeout(200)
                        page.evaluate("() => { document.getElementById('toasts').innerHTML = ''; }")
                        name = f"gui-{label}-{v}.png"
                        page.screenshot(path=str(VALIDATION / name), full_page=False)
                        check(f"screenshot saved: .validation/{name}", (VALIDATION / name).exists())
                    page.click("#rail [data-view='chat']")
                    page.wait_for_selector("#view-chat:not([hidden])", timeout=10_000)
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

    # R3 U9: numeric banner — the pass count is real (COUNTS), never implied.
    total = COUNTS["pass"] + len(FAILURES)
    print(f"\n{COUNTS['pass']} passed, {len(FAILURES)} failed, {total} checks total"
          + ("" if FAILURES else " — ALL CHECKS PASSED"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())

