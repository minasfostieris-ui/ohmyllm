"""R8 UX regression contracts (task 124, Jay security finding J7 LOW).

Reproduce-first: every fix-assertion below is ABSENT at pristine HEAD e263d12
and present after the fix, so the suite fails pre-fix and passes post-fix.
Control-assertions pin the benign rendering pipeline (fence extraction,
placeholder regex, headings/lists/quotes, inline markup, boot focus guard) so
the J7 hardening cannot regress normal markdown rendering.

J7: md() restores forged `\u0000B<idx>\u0000` placeholders (literal NULs survive
the JSON wire) with an UNCHECKED blocks[+bm[1]] deref → TypeError. A poisoned
persisted session replays through loadSessionIntoView (no per-message catch)
into boot() (no catch) and aborts GUI init BEFORE polling installs — every
subsequent boot is bricked until the session is deleted.

Source-contract style (same discipline as tests/test_r7ux_regressions.py): the
browser-level proof (N1a/b/c no-throw, poisoned-session boot recovery, replay
fault isolation) lives in scripts/gui_headless_check.py R8 flows; these tests
pin the exact code contracts cheaply and deterministically under the guard.
"""
from __future__ import annotations

import re
from pathlib import Path

import ohmyllm

# Source root derived from the IMPORTED package (R4/R5/R7 discipline).
PKG = Path(ohmyllm.__file__).resolve().parent
TREE = PKG.parent.parent
GUI = PKG / "gui_static"
APP = (GUI / "app.js").read_text(encoding="utf-8")


def _slice(text: str, start_marker: str, end_marker: str) -> str:
    i = text.index(start_marker)
    j = text.index(end_marker, i)
    return text[i:j]


def md_body() -> str:
    return _slice(APP, "function md(src)", 'document.addEventListener("click"')


def inline_body() -> str:
    return _slice(APP, "function inline(s)", "function md(src)")


def hl_body() -> str:
    return _slice(APP, "function hl(code, lang)", "function inline(s)")


def replay_body() -> str:
    return _slice(APP, "async function loadSessionIntoView", "/* SSE reader over fetch")


def replay_catch() -> str:
    body = replay_body()
    i = body.index("} catch (err) {")
    return body[i:body.index("if (!sess.messages?.length)")]


def boot_tail() -> str:
    # boot() plus its invocation — the file ends here.
    return APP[APP.index("async function boot()"):]


def boot_fn() -> str:
    return boot_tail()[:boot_tail().index("\nboot()")]


BOOT_SECTION = "/* ------------------------------------------------------------------ boot */"


def polling_body() -> str:
    return _slice(APP, "function installPolling", BOOT_SECTION)


# ------------------------------------------------------- J7 md() bounds
def test_r8_j7_md_bounds_checks_placeholder_index():
    body = md_body()
    i = body.index("const b = blocks[+bm[1]];")
    tail = body[i:]
    m = re.search(r"if \(!b\)", tail)
    assert m, "J7: md() must bounds-check the forged NUL-placeholder index"
    assert m.start() < tail.index("b.code"), \
        "J7: the !b guard must run BEFORE any b.code/b.lang dereference"


def test_r8_j7_md_missing_block_renders_escaped_literal():
    assert re.search(
        r"if \(!b\) \{ html \+= `<p>\$\{esc\(line\)\}</p>`; continue; \}", md_body()
    ), "J7: an out-of-range placeholder must render as escaped literal text, not throw"


def test_r8_j7_md_extraction_and_placeholder_regex_unchanged():
    body = md_body()
    assert r"const text = String(src).replace(/```(\w*)\n([\s\S]*?)```/g" in body, \
        "J7 control: fenced-block extraction must stay byte-identical"
    assert r"const bm = line.match(/^\u0000B(\d+)\u0000$/);" in body, \
        "J7 control: the placeholder restore regex must stay byte-identical"
    assert 'hl(b.code, b.lang)' in body and 'data-code="${esc(b.code)}"' in body, \
        "J7 control: real fenced blocks must still render highlighted with a copy button"


def test_r8_j7_md_benign_branches_unchanged():
    body = md_body()
    assert r"const h = line.match(/^(#{1,3})\s+(.*)/);" in body, \
        "J7 control: heading rendering must survive"
    assert r"const ul = line.match(/^[-*]\s+(.*)/), ol = line.match(/^\d+[.)]\s+(.*)/);" in body, \
        "J7 control: list rendering must survive"
    assert r'if (/^>\s?/.test(line))' in body, "J7 control: blockquote rendering must survive"
    assert "const flushPara = () =>" in body and "const flushList = () =>" in body, \
        "J7 control: paragraph/list flushing must survive"


def test_r8_j7_inline_and_hl_pipeline_unchanged():
    inl = inline_body()
    assert "s = esc(s);" in inl, "J7 control: inline must escape first"
    assert r"s = s.replace(/`([^`]+)`/g" in inl, "J7 control: inline code must survive"
    assert r's = s.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");' in inl, \
        "J7 control: bold must survive"
    assert 'rel="noopener noreferrer"' in inl, "J7 control: safe link attrs must survive"
    assert r'if (/^(json|toml|ini|yaml)$/.test(lang)) return esc(code);' in hl_body(), \
        "J7 control: data-language highlighting shortcut must survive"


# ------------------------------------------------------- J7 replay catch
def test_r8_j7_replay_wraps_each_message_in_try_catch():
    body = replay_body()
    fe = body.index("(sess.messages || []).forEach((m) => {")
    tr = body.index("try {", fe)
    first_role = body.index('if (m.role === "user")', fe)
    assert fe < tr < first_role, \
        "J7: the per-message replay body must sit inside a try block"
    assert "} catch (err) {" in body[tr:], \
        "J7: one poisoned message must be caught, not abort the whole replay"


def test_r8_j7_replay_catch_note_is_renderer_safe():
    catch = replay_catch()
    assert "systemish" in catch, "J7: the replay fault must leave a visible in-stream note"
    assert ".textContent =" in catch, \
        "J7: the fault note must use textContent — md() itself may be the thrower"
    assert "md(" not in catch and "innerHTML +=" not in catch, \
        "J7: the catch path must never re-enter the failing renderer"


def test_r8_j7_replay_catch_does_not_rethrow():
    catch = replay_catch()
    assert "throw" not in catch, \
        "J7: the replay catch must swallow the fault so forEach continues and boot survives"


# ------------------------------------------------------- J7 boot recovery
def test_r8_j7_boot_catches_replay_failure():
    assert re.search(
        r"try \{ await loadSessionIntoView\(active\); \}\s*catch \(e\) \{.*?emptyState\(\)",
        boot_fn(),
        re.S,
    ), "J7: boot must catch a failed session replay (toast + emptyState), never abort"


def test_r8_j7_polling_install_is_idempotent():
    assert "function installPolling" in APP, "J7: polling must move into installPolling()"
    assert "let pollingInstalled = false;" in APP, "J7: installPolling needs an idempotence flag"
    body = polling_body()
    assert "if (pollingInstalled) return;" in body and "pollingInstalled = true;" in body, \
        "J7: boot() and boot().catch must never double-install the timers"
    assert "setInterval(refreshStatus, 5000);" in body, "J7 control: 5s status tick preserved"
    assert "setInterval(pollState, 2000);" in body, "J7 control: 2s state poll preserved"
    assert "pollState();" in body, "J7 control: the immediate first poll preserved"
    assert "installPolling();" in boot_fn(), "J7: the boot success path must install polling"
    assert "setInterval(" not in boot_fn(), \
        "J7: bare setInterval calls must live in installPolling only"


def test_r8_j7_boot_catch_installs_polling_and_shows_error():
    tail = boot_tail()
    i = tail.index("boot().catch(")
    net = tail[i:]
    assert "installPolling();" in net, \
        "J7: even a hard boot fault must leave polling alive (GUI never bricked)"
    assert '$("#boot-error").hidden = false;' in net, "J7: the boot fault must be visible"
    assert '$("#boot-error-msg")' in net and ".textContent" in net, \
        "J7: the error card must carry the fault message (textContent, role=alert from R5)"
    assert '$("#boot-retry").focus();' in net, "J7: focus belongs on Retry when boot faults"


def test_r8_j7_boot_success_path_controls_unchanged():
    body = boot_fn()
    assert "await refreshStatus();" in body and "await renderSidebar();" in body, \
        "J7 control: boot ordering (status → sidebar → replay) must survive"
    assert "addTab(active);" in body, "J7 control: the active chat must still open a tab"
    assert 'if ($("#boot-error").hidden) input.focus();' in body, \
        "J7 control: R5-F4 focus guard (error card owns focus on outage) must survive"
