"""R5 UX regression contracts (task #92).

Reproduce-first: every fix-assertion below is ABSENT at pristine HEAD fe4475e
(.validation/r5ux-prefix) and present after the fix, so the suite fails pre-fix
and passes post-fix. Guard-assertions pin unchanged R3/R4 behaviour so the R5
fixes cannot regress the consent trap, inert background, or disconnect honesty.

Source-contract style (same discipline as tests/test_r4ux_regressions.py): the
browser-level proof lives in scripts/gui_headless_check.py R5 flows; these tests
pin the exact code contracts cheaply and deterministically under the guard.
"""
from __future__ import annotations

import re
from pathlib import Path

import ohmyllm

# Source root derived from the IMPORTED package (R4 discipline): a snapshot
# control run with PYTHONPATH pinned at .validation/r5ux-prefix/src really reads
# the pristine JS/HTML instead of silently exercising the live tree.
PKG = Path(ohmyllm.__file__).resolve().parent
TREE = PKG.parent.parent
GUI = PKG / "gui_static"
APP = (GUI / "app.js").read_text(encoding="utf-8")
HTML = (GUI / "index.html").read_text(encoding="utf-8")
CSS = (GUI / "styles.css").read_text(encoding="utf-8")


def _slice(text: str, start_marker: str, end_marker: str) -> str:
    i = text.index(start_marker)
    j = text.index(end_marker, i)
    return text[i:j]


def answer_body() -> str:
    return _slice(APP, "async function answerApproval", "composer basics")


def arrow_body() -> str:
    return _slice(APP, 'else if (e.key === "ArrowLeft"', "b.onauxclick")


def poll_body() -> str:
    return _slice(APP, "async function pollState", "async function openChat")


def boot_show_body() -> str:
    # whole refreshStatus catch, INCLUDING the "Disconnected" repaint line
    # (_slice ends at the start of the end marker, so end past the catch).
    return _slice(APP, "state.connected = false;", "permission mode */")


def cast_body() -> str:
    return _slice(APP, 'if (cmd.startsWith("/skill:"))', "toast(`unknown command")


# --------------------------------------------------------------------- F1
def test_f1_successor_replaces_focus_with_safe_deny_default():
    body = answer_body()
    # The finally must hand focus to the SAME safe default as the first approval
    # (#ap-deny), never focusables()[0] (= Allow once) on a queued successor.
    assert "#ap-deny" in body, "F1: successor focus must prefer the Deny safe default"
    m = re.search(r"const\s+safe\s*=\s*\$\(\"#ap-deny\"\)", body)
    assert m, "F1: expected an explicit #ap-deny safe-default target in answerApproval"


def test_f1_successor_never_blindly_focuses_first_allow():
    body = answer_body()
    # focusables()[0] may remain only as a FALLBACK when Deny is unavailable;
    # the primary target must be the safe default, not the first DOM button.
    assert re.search(r"safe.*\?.*focusables\(am\)\[0\]", body, re.S), \
        "F1: Deny must be primary, focusables()[0] only a fallback"


def test_f1_initial_approval_still_defaults_to_deny():
    assert 'openModal("#approval-modal", "#ap-deny");' in APP, \
        "F1 guard: initial approval safe default must remain Deny"


def test_f1_consent_trap_and_inert_still_intact():
    assert "function holdModalFocus" in APP and "if (!items.length)" in APP, \
        "F1 guard: R4-01 in-flight trap must survive"
    assert '["#main", "#sidebar", "#topbar", "#rail"]' in APP, \
        "F1 guard: R4-16 inert coverage must survive"


# --------------------------------------------------------------------- F2
def test_f2_arrow_roving_scrolls_focused_tab_into_view():
    body = arrow_body()
    assert "sib.focus();" in body, "F2 guard: arrow handler still focuses the sibling"
    assert re.search(r"sib\.scrollIntoView\(", body), \
        "F2: arrow-roving must scroll the focused tab into view at narrow widths"


def test_f2_scroll_is_instant_and_inline_nearest():
    body = arrow_body()
    m = re.search(r"sib\.scrollIntoView\(\{[^}]*\}\)", body)
    assert m, "F2: expected a scrollIntoView options object on the sibling"
    opts = m.group(0)
    assert 'inline: "nearest"' in opts and 'behavior: "instant"' in opts, \
        "F2: instant + inline:nearest (smooth gets clobbered by keepScroll restore)"


# --------------------------------------------------------------------- F3
def test_f3_pollstate_repaint_guarded_by_connected():
    body = poll_body()
    m = re.search(r"if \(!state\.streaming[^)]*\)\s*\$\(\"#run-state\"\)", body)
    assert m, "F3: expected the run-state repaint line in pollState"
    assert "state.connected" in m.group(0), \
        "F3: partial outage (status down, state up) must not repaint Ready while dot off"


def test_f3_full_outage_still_reports_disconnected():
    body = boot_show_body()
    assert "state.connected = false;" in body and '"Disconnected";' in body, \
        "F3 guard: refreshStatus catch must keep reporting Disconnected"


# --------------------------------------------------------------------- F4
def test_f4_boot_error_card_is_announced():
    m = re.search(r"<div id=\"boot-error\"[^>]*>", HTML)
    assert m, "F4: #boot-error card must exist"
    tag = m.group(0)
    assert 'role="alert"' in tag or 'aria-live="assertive"' in tag, \
        "F4: boot error must be announced to screen readers (role=alert / aria-live)"


def test_f4_boot_error_moves_focus_to_retry():
    body = boot_show_body()
    assert re.search(r"\$\(\"#boot-retry\"\)\.focus\(\)", body), \
        "F4: showing the boot error must move focus to the Retry control"


def test_f4_retry_control_present():
    assert 'id="boot-retry"' in HTML, "F4 guard: retry button must exist"


def test_f4_boot_does_not_steal_focus_from_retry():
    # boot() ended with an unconditional input.focus() — the live focus thief
    # that parked the keyboard on the composer while the boot card was up.
    assert re.search(r'if \(\$\("#boot-error"\)\.hidden\) input\.focus\(\);', APP), \
        "F4: boot() must skip the composer focus while the boot-error card is up"


# --------------------------------------------------------------------- F5
def test_f5_cast_adds_persistent_in_stream_note():
    body = cast_body()
    assert re.search(r"addMsg\(\s*\"systemish\"", body), \
        "F5: GUI cast must leave a persistent in-stream sysline (TUI parity), not just a toast"


def test_f5_cast_note_carries_skill_name_parity():
    body = cast_body()
    assert "skill cast" in body and "${name}" in body, \
        "F5: the sysline must name the cast skill, matching the TUI sysline"


def test_f5_cast_still_posts_name_session_args():
    body = cast_body()
    assert re.search(r"post\(\"/api/skills/cast\",\s*\{\s*name,\s*session:\s*state\.session,\s*args:\s*arg\s*\}\)", body), \
        "F5 guard: cast POST contract (name + session + additive args) must survive"


def test_f5_systemish_note_style_exists():
    assert ".msg.systemish .content" in CSS, "F5 guard: persistent note styling must exist"
