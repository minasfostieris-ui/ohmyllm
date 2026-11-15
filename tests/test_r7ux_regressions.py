"""R7 UX regression contracts (task 110, Chloe R7-01/02/03).

Reproduce-first: every fix-assertion below is ABSENT at pristine HEAD c8ed95d
(gui_static verified byte-identical to HEAD before the fix) and present after
the fix, so the suite fails pre-fix and passes post-fix. Control-assertions pin
unchanged behaviour (Listening paint, keydown modal gate, consent status paint,
implemented Ctrl+Q) so the R7 fixes cannot regress them.

Source-contract style (same discipline as tests/test_r5ux_regressions.py): the
browser-level proof lives in scripts/gui_headless_check.py R7 flows; these tests
pin the exact code contracts cheaply and deterministically under the guard.
R7-04 (do_GET 404 fallback) is the bridge lane — deliberately NOT pinned here.
"""
from __future__ import annotations

import re
from pathlib import Path

import ohmyllm

# Source root derived from the IMPORTED package (R4/R5 discipline): a snapshot
# control run with PYTHONPATH pinned really reads the pristine JS instead of
# silently exercising the live tree.
PKG = Path(ohmyllm.__file__).resolve().parent
TREE = PKG.parent.parent
GUI = PKG / "gui_static"
APP = (GUI / "app.js").read_text(encoding="utf-8")


def _slice(text: str, start_marker: str, end_marker: str) -> str:
    i = text.index(start_marker)
    j = text.index(end_marker, i)
    return text[i:j]


def poll_body() -> str:
    return _slice(APP, "async function pollState", "async function openChat")


def refresh_body() -> str:
    # whole refreshStatus incl. the catch (ends at the permission-mode banner)
    return _slice(APP, "async function refreshStatus", "permission mode */")


def voice_body() -> str:
    return _slice(APP, "let talking = false", "/* K23: GUI parity toggles")


def talk_start_body() -> str:
    return _slice(APP, "async function talkStart", "async function talkStop")


def talk_stop_body() -> str:
    return _slice(APP, "async function talkStop", "/* K23: GUI parity toggles")


def keydown_body() -> str:
    return _slice(APP, 'document.addEventListener("keydown"', 'document.addEventListener("keyup"')


def keyup_body() -> str:
    return _slice(APP, 'document.addEventListener("keyup"', 'window.addEventListener("blur"')


def help_keys_body() -> str:
    return _slice(APP, "function renderHelp", "const refs = [")


# ------------------------------------------------------------------ R7-01
def test_r7_01_pollstate_never_repaints_over_live_listening():
    body = poll_body()
    m = re.search(r"if \(!state\.streaming[^)]*\)\s*\$\(\"#run-state\"\)", body)
    assert m, "R7-01: expected the run-state repaint line in pollState"
    guard = m.group(0)
    assert "!talking" in guard, \
        "R7-01: pollState must not repaint Ready over live 'Listening…' (mic-live truthfulness)"
    assert "state.connected" in guard, \
        "R7-01 guard: the R5-F3 partial-outage connected guard must survive"


def test_r7_01_refreshstatus_never_repaints_over_live_listening():
    body = refresh_body()
    m = re.search(r"if \(!state\.streaming[^)]*\)\s*\$\(\"#run-state\"\)", body)
    assert m, "R7-01: expected the run-state repaint line in refreshStatus"
    guard = m.group(0)
    assert "!talking" in guard, \
        "R7-01: the 5s refreshStatus tick must not repaint Ready while recording"
    assert "state.approvalQueue.length" in guard, \
        "R7-01 guard: the existing approval-pending guard must survive"


def test_r7_01_transcribing_survives_until_the_stop_settles():
    voice = voice_body()
    assert re.search(r"let talking = false,\s*transcribing = false", voice), \
        "R7-01: a transcribing flag must cover the stop POST await (talking is already false)"
    stop = talk_stop_body()
    assert "transcribing = true" in stop and "transcribing = false" in stop, \
        "R7-01: talkStop must raise the flag before 'Transcribing…' and lower it at settle"
    for name, body in (("pollState", poll_body()), ("refreshStatus", refresh_body())):
        m = re.search(r"if \(!state\.streaming[^)]*\)\s*\$\(\"#run-state\"\)", body)
        assert m and "!transcribing" in m.group(0), \
            f"R7-01: {name} must keep 'Transcribing…' until talkStop settles"


def test_r7_01_talkstart_paints_listening_with_release_hint():
    body = talk_start_body()
    assert '"Listening… release Ctrl+E"' in body, \
        "R7-01 control: talkStart must paint the Listening status with the release hint"
    assert 'input.classList.add("mic-live")' in body, \
        "R7-01 control: the mic-live ring must still be applied"


# ------------------------------------------------------------------ R7-02
def test_r7_02_ctrl_e_keyup_is_modal_gated():
    body = keyup_body()
    m = re.search(r"if \(modalStack\.length\) return;", body)
    assert m, "R7-02: the keyup talkStop path must be modal-gated like keydown (L2923)"
    assert body.index("modalStack.length") < body.index("talkStop()"), \
        "R7-02: the modal gate must run BEFORE talkStop in the keyup handler"


def test_r7_02_talkstop_preserves_pending_approval_status():
    body = talk_stop_body()
    assert re.search(
        r'\$\("#run-state"\)\.textContent = state\.approvalQueue\.length \? "Waiting for approval"',
        body,
    ), "R7-02: talkStop settle must restore 'Waiting for approval' while a decision is pending"


def test_r7_02_talkstart_error_path_preserves_approval_status():
    body = talk_start_body()
    assert re.search(
        r'\$\("#run-state"\)\.textContent = state\.approvalQueue\.length \? "Waiting for approval"',
        body,
    ), "R7-02: a failed voice start must not clobber a pending consent status either"


def test_r7_02_keydown_still_modal_gated_and_starts_talk():
    body = keydown_body()
    assert "if (modalStack.length) return;" in body, \
        "R7-02 control: the keydown modal gate must survive"
    assert "if (!e.repeat) talkStart();" in body, \
        "R7-02 control: Ctrl+E keydown must still start talk (no auto-repeat restarts)"


def test_r7_02_approval_still_paints_waiting_for_approval():
    assert '$("#run-state").textContent = "Waiting for approval";' in APP, \
        "R7-02 control: showApproval must keep painting the consent-pending status"


# ------------------------------------------------------------------ R7-03
def test_r7_03_help_table_lists_ctrl_q():
    keys = help_keys_body()
    assert re.search(r'\["Ctrl\+Q",\s*"[^"]*shut down[^"]*confirm[^"]*"\]', keys), \
        "R7-03: the help keys table must list Ctrl+Q (shut down, with confirm)"


def test_r7_03_ctrl_q_quit_is_implemented():
    body = keydown_body()
    assert 'else if (k === "q") { e.preventDefault(); handleSlash("/quit"); }' in body, \
        "R7-03 control: the Ctrl+Q → /quit implementation must survive"


def test_r7_03_help_covers_every_implemented_global_hotkey():
    body = keydown_body()
    views = re.search(r"const views = \{([^}]*)\}", body)
    assert views, "R7-03: expected the views hotkey map in the keydown handler"
    letters = re.findall(r"(\w+):", views.group(1))
    letters += re.findall(r'else if \(k === "(\w+)"', body)
    keys = help_keys_body()
    # single letters render upper-case (Ctrl+Q); word keys title-case (Ctrl+End)
    missing = [k for k in letters
               if f'"Ctrl+{k.upper() if len(k) == 1 else k.capitalize()}' not in keys]
    assert not missing, \
        f"R7-03: implemented global hotkeys missing from the help table: {missing}"
