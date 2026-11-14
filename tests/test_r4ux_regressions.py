"""R4 UX regressions (task #82) — one test per CONFIRMED finding where testable.

Reproduce-first discipline: this file was written BEFORE the fixes and run
against the pristine 87037b0 control in .validation/r4ux-prefix, where every
finding below failed (evidence: .validation/jimmy-r4ux-prefix-reproduce.log).
The browser-only PoCs (R4-01 focus escape during the decision POST, R4-04
orphaned session, R4-03 poll tabindex reset, R4-05 sidebar focus theft, R4-09
chip lag, R4-14 blank market grid) are ALSO asserted live in
scripts/gui_headless_check.py; what is provable offline is pinned here.

REFUTED findings are deliberately NOT "fixed" or pinned as defects: R4-00/00b
(sandbox), R4-02 (approval queue), R4-08 (yolo carry), R4-10 (rejected send),
R4-12 (AA — the only sub-threshold surface is a DISABLED control, exempt under
WCAG 1.4.3), R4-13 (tab auto-scroll), R4-17 (provider/market census), R4-99.

Source root is derived from the IMPORTED package, so a snapshot control run
(PYTHONPATH pinned at .validation/r4ux-prefix/src) really reads the pristine
JS/docs instead of silently exercising the live tree.

Guard-compatible with scripts/verify_isolated.py: no subprocess, no browser,
loopback bridges on ephemeral ports, Textual pilots only.
"""

import re
from pathlib import Path

import ohmyllm
from ohmyllm.doctor import run_checks
from ohmyllm.tools.registry import build_default_registry
from ohmyllm.tui.app import OhMyLLMApp

PKG = Path(ohmyllm.__file__).resolve().parent
TREE = PKG.parent.parent
APP_JS = (PKG / "gui_static" / "app.js").read_text(encoding="utf-8")
STYLES = (PKG / "gui_static" / "styles.css").read_text(encoding="utf-8")
CHECKER = (TREE / "scripts" / "gui_headless_check.py").read_text(encoding="utf-8")
README = (TREE / "README.md").read_text(encoding="utf-8")
CHANGELOG = (TREE / "CHANGELOG.md").read_text(encoding="utf-8")
PERMISSIONS = (TREE / "docs" / "PERMISSIONS.md").read_text(encoding="utf-8")
TROUBLESHOOTING = (TREE / "docs" / "TROUBLESHOOTING.md").read_text(encoding="utf-8")

SKILL_MD = ("---\nname: probe-skill\ndescription: A probe skill.\n---\n\n"
            "# Probe\n\nDo the probe.\n")


def _write_skill(home: Path) -> Path:
    d = home / "skills" / "probe-skill"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    return d


def _js_fn(name: str) -> str:
    """Body of a top-level `function name(...)` / `async function name(...)`."""
    m = re.search(rf"^(?:async )?function {re.escape(name)}\(", APP_JS, re.M)
    assert m, f"{name}() not found in app.js"
    start = m.start()
    depth = 0
    for i in range(APP_JS.index("{", start), len(APP_JS)):
        if APP_JS[i] == "{":
            depth += 1
        elif APP_JS[i] == "}":
            depth -= 1
            if depth == 0:
                return APP_JS[start:i + 1]
    raise AssertionError(f"unbalanced braces in {name}()")


def _brace_body(src: str, idx: int) -> str:
    """Balanced {...} body of the block whose header starts at idx."""
    start = src.index("{", idx)
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start + 1:i]
    raise AssertionError("unbalanced braces")


def _tab_trap() -> str:
    m = re.search(r'document\.addEventListener\("keydown", \(e\) => \{\n'
                  r"  const top = modalStack.*?\n\}, true\);", APP_JS, re.S)
    assert m, "the modal keydown trap is missing"
    return m.group(0)


def _skill_branch() -> str:
    m = re.search(r'if \(cmd\.startsWith\("/skill:"\)\) \{', APP_JS)
    assert m, "/skill: branch not found"
    return _brace_body(APP_JS, m.start())


# ------------------------------------------------------- control integrity
def test_control_sources_match_the_imported_package():
    """The JS/docs under assertion must come from the SAME tree as the Python.

    Without this a snapshot run could read the live (already fixed) app.js and
    'prove' a pre-fix failure that was never actually observed.
    """
    from ohmyllm.tui.screens import chat as chat_mod

    assert Path(chat_mod.__file__).resolve().is_relative_to(PKG), \
        f"control leak: chat.py imported from {chat_mod.__file__}, expected under {PKG}"
    assert (TREE / "src" / "ohmyllm" / "__init__.py").resolve() == (PKG / "__init__.py").resolve(), \
        "control leak: the JS/docs tree is not the tree the package was imported from"
    assert (TREE / "scripts" / "gui_headless_check.py").exists()


# ------------------------------------------------------------------- R4-01
def test_r4_01_tab_trap_holds_when_the_modal_has_no_focusables():
    """High: during the decision POST every button is disabled, so
    focusables() is empty and the old `items.length &&` guard let the browser
    default Tab run — focus escaped the dialog into the chrome."""
    trap = _tab_trap()
    tab = trap[trap.index('if (e.key === "Tab")'):]
    assert "items.length &&" not in tab, \
        "R4-01: the empty-focusables case must not fall through to the default Tab"
    m = re.search(r"if\s*\(\s*!items\.length\s*\)", tab)
    assert m, "R4-01: an explicit empty-focusables branch is required"
    body = _brace_body(tab, m.start())
    assert "preventDefault" in body, \
        "R4-01: with no focusables the trap must swallow Tab, not release it"


def test_r4_01_disabling_the_approval_buttons_does_not_strand_focus():
    fn = _js_fn("answerApproval")
    disable = '$$("#approval-modal button").forEach((b) => (b.disabled = true));'
    assert disable in fn, "the in-flight disable loop moved"
    tail = fn[fn.index(disable):]
    assert re.search(r"holdModalFocus\(", tail), \
        "R4-01: disabling the focused button blurs it to <body>; hold focus in the dialog"
    hold = _js_fn("holdModalFocus")
    assert ".focus()" in hold and "tabIndex = -1" in hold, \
        "R4-01: the dialog itself must be focusable so the trap has an anchor"


# ------------------------------------------------------------------- R4-16
def test_r4_16_consent_inert_covers_the_topbar_and_the_rail():
    inert = _js_fn("syncApprovalInert")
    for sel in ('"#main"', '"#sidebar"', '"#topbar"', '"#rail"'):
        assert sel in inert, f"R4-16: {sel} stays reachable behind the consent scrim"
    assert ".inert = true" in inert and 'aria-hidden' in inert


def test_r4_16_sidebar_row_is_not_a_button_wrapping_buttons():
    fn = _js_fn("renderSidebar")
    assert 'row.setAttribute("role", "button")' not in fn, \
        "R4-16: role=button on a row nesting four real buttons is invalid ARIA"
    assert re.search(r'row\.setAttribute\("role",\s*"group"\)', fn), \
        "R4-16: the row is a group; the open affordance lives on .sb-main"
    assert re.search(r'row\.setAttribute\("aria-label"', fn), \
        "R4-16: a group needs an accessible name"
    assert 'class="sb-main" role="button"' in fn, \
        "R4-16: the open affordance must keep button semantics for AT"


# ------------------------------------------------------------------- R4-04
def test_r4_04_failed_successor_load_restores_the_closed_tab():
    """High: loadSessionIntoView bumps loadSeq BEFORE the fetch, so the old
    `loadSeq === seqBefore` test could not tell 'refused' from 'failed' and a
    failed successor left state.session pointing at a chat with no tab."""
    fn = _js_fn("closeTab")
    assert "loadSeq === seqBefore" not in fn, \
        "R4-04: the loadSeq equality test misclassifies a genuine load failure"
    assert "const ok = await openChat(next);" in fn
    after = fn[fn.index("const ok = await openChat(next);"):]
    assert re.search(r"if \(!ok", after), "R4-04: the failure path must be handled"
    assert re.search(r"state\.openTabs\.splice\(", after), \
        "R4-04: the pruned tab must be restored when no open tab owns the view"
    assert "!state.openTabs.includes(state.session)" in after, \
        "R4-04: the orphan condition is 'no open tab owns the view'"


def test_r4_04_orphan_recovery_kills_the_stale_stream():
    fn = _js_fn("closeTab")
    assert "killStaleStream()" in fn, \
        "R4-04: recovering to the empty state must not leave a live turn painting"
    kill = _js_fn("killStaleStream")
    assert "/api/cancel" in kill or '$("#cancel").click()' in kill, \
        "R4-04: the bridge must be asked to cancel the orphaned turn"
    assert "state.streaming = false" in kill
    assert "loadAbort?.abort()" in kill, "R4-04: a pending chat load must be aborted too"


# ------------------------------------------------------------------- R4-03
def test_r4_03_poll_keeps_roving_tabindex_on_the_arrow_focused_tab():
    fn = _js_fn("renderTabs")
    assert "b.tabIndex = active ? 0 : -1;" not in fn, \
        "R4-03: the 2s poll resets the roving tabindex on the focused tab"
    # target the ROVING ternary assignment, not the arrow-handler's plain
    # `b.tabIndex = -1` (legitimate R3 code that exists pre- and post-fix).
    m = re.search(r"b\.tabIndex = ([^;]*\?[^;]*);", fn)
    assert m and ("aeTab" in m.group(1) or "focusSid" in m.group(1)), \
        "R4-03: the tab holding document.activeElement must keep tabIndex 0"
    assert re.search(r"if \(!act && !focusSid && tabs\.firstChild\)", fn), \
        "R4-03: the fallback must not add a second tabIndex 0 while a tab holds focus"


# ------------------------------------------------------------------- R4-05
def test_r4_05_sidebar_rebuild_restores_focus_to_the_same_chat():
    fn = _js_fn("renderSidebar")
    assert re.search(r"""closest\(['"]#sb-list \.sb-row['"]\)""", fn), \
        "R4-05: the focused row must be identified before innerHTML is reset"
    assert "dataset.cid" in fn, "R4-05: rows need a stable chat id to restore focus by"
    reset = fn.index('list.innerHTML = "";')
    built = fn.index("list.appendChild(row);")
    assert built > reset
    assert ".focus()" in fn[built:], \
        "R4-05: focus must be restored to the same chat after the rebuild"
    empty = fn[reset:built]
    assert ".focus()" in empty, \
        "R4-05: the no-rows early return must not strand focus on <body> either"


# ------------------------------------------------------------------- R4-06
def test_r4_06_skill_cast_guard_runs_before_the_composer_clear():
    branch = _skill_branch()
    guard = branch.index("if (!state.session)")
    clear = branch.index("clear();")
    assert guard < clear, \
        "R4-06: /skill:name wiped the typed instruction before the no-session guard"
    assert re.search(r'if \(!state\.session\) \{[^}]*keep\(\)', branch), \
        "R4-06: the refused cast must keep the draft"


# ------------------------------------------------------------------- R4-07
async def _tui_slash(text: str, oml_env, preset: bool = False):
    """Drive one slash command through the real TUI and record its notices.

    The TUI boots its OWN Services, and agent.yolo is a runtime-only flag
    (Field exclude=True) that never reaches config.toml — so the starting
    state must be set on the app's service, not on the fixture's.
    """
    app = OhMyLLMApp()
    notes: list[tuple[str, str]] = []
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        app.svc.config.agent.yolo = preset
        orig = app.notify

        def rec(message, *a, **kw):
            notes.append((str(message), str(kw.get("severity") or "")))
            return orig(message, *a, **kw)

        app.notify = rec
        app.screen.query_one("#chat-input").value = text
        await pilot.press("enter")
        await pilot.pause(0.8)
        yolo = bool(getattr(app.svc.config.agent, "yolo", False))
    return yolo, notes


async def test_r4_07_tui_invalid_yolo_arg_never_arms(oml_env, svc):
    """High: a typo used to fall through to the toggle, so `/yolo banana`
    ARMED YOLO. The GUI already rejects it; the TUI leg must match."""
    svc.config.agent.yolo = False
    yolo, notes = await _tui_slash("/yolo banana", oml_env, preset=False)
    assert yolo is False, "R4-07: an invalid /yolo arg armed YOLO in the TUI"
    assert any("usage: /yolo" in n for n, _s in notes), \
        f"R4-07: the rejection must be announced, got {notes}"


async def test_r4_07_tui_invalid_yolo_arg_never_disarms_either(oml_env, svc):
    yolo, notes = await _tui_slash("/yolo OFFF", oml_env, preset=True)
    assert yolo is True, "R4-07: an invalid arg must leave the state untouched"
    assert any("usage: /yolo" in n for n, _s in notes)


async def test_r4_07_tui_explicit_yolo_args_still_work(oml_env, svc):
    svc.config.agent.yolo = False
    on, _n = await _tui_slash("/yolo ON", oml_env)
    assert on is True, "R4-07: /yolo ON must still arm (case-insensitive)"
    off, _n2 = await _tui_slash("/yolo off", oml_env)
    assert off is False, "R4-07: /yolo off must still disarm"


async def test_r4_07_tui_bare_yolo_still_toggles(oml_env, svc):
    svc.config.agent.yolo = False
    yolo, _n = await _tui_slash("/yolo", oml_env)
    assert yolo is True, "R4-07: a bare /yolo must keep toggling"


def test_r4_07_gui_yolo_reference_rejects_an_unknown_arg():
    """Parity reference: the GUI leg is already correct — pin it."""
    m = re.search(r'case "/yolo": \{', APP_JS)
    assert m
    block = _brace_body(APP_JS, m.start())
    assert 'toast("usage: /yolo [on|off]", "warn"); keep(); return;' in block, \
        "R4-07: the GUI reference behaviour changed"


# ------------------------------------------------------------------- R4-09
def test_r4_09_mode_chip_repaints_from_the_applied_mode():
    """Low (reframed): the server and toast were right immediately but the
    chip kept the OLD mode until the 5s refreshStatus tick."""
    fn = _js_fn("chooseMode")
    assert "await refreshStatus();" in fn, \
        "R4-09: chooseMode fired refreshStatus un-awaited and never re-checked"
    assert re.search(r"paintModeChip\(mode\)", fn), \
        "R4-09: the applied mode must paint the chip at once"
    assert re.search(r"state\.status\.mode !== mode", fn), \
        "R4-09: a stale snapshot must be re-asserted, not left on screen"
    paint = _js_fn("paintModeChip")
    assert "chip.textContent" in paint and "chip.className" in paint
    assert "state.status.mode = mode" in paint, \
        "R4-09: the local mirror must agree with the painted chip"


# ------------------------------------------------------------------- R4-11
def test_r4_11_help_esc_row_documents_the_fail_closed_deny():
    m = re.search(r'\["Esc",\s*"([^"]+)"\]', APP_JS)
    assert m, "the help Esc row is missing"
    row = m.group(1).lower()
    assert "deny" in row or "denies" in row, \
        f"R4-11: Esc on an approval DENIES it (fail-closed) — undocumented: {row!r}"
    assert "approval" in row, "R4-11: the row must say WHICH modal denies"


def test_r4_11_permissions_doc_documents_esc_deny():
    assert re.search(r"\bEsc\b", PERMISSIONS), \
        "R4-11: docs/PERMISSIONS.md never mentions the Esc deny path"
    low = PERMISSIONS.lower()
    assert "fail closed" in low or "fail-closed" in low
    esc = PERMISSIONS[PERMISSIONS.index("Esc"):]
    assert "deny" in esc[:400].lower() or "denied" in esc[:400].lower(), \
        "R4-11: the doc must state that Esc denies the approval"


# ------------------------------------------------------------------- R4-14
def test_r4_14_market_failure_paints_a_persistent_error_with_retry():
    fn = _js_fn("renderMarket")
    catch = fn[fn.index("catch"):]
    assert 'innerHTML = ""; toast' not in catch, \
        "R4-14: a failed catalog fetch left a BLANK grid plus a 4s toast"
    assert "marketError(" in catch, "R4-14: the failure must paint a real error state"
    err = _js_fn("marketError")
    assert 'setAttribute("role", "alert")' in err, \
        "R4-14: the error state must be announced to AT"
    assert "market-retry" in err and "renderMarket()" in err, \
        "R4-14: a persistent retry affordance is required"
    assert ".market-error" in STYLES, "R4-14: the error state needs styling"


# ------------------------------------------------------------------- R4-15
def test_r4_15_doctor_claim_matches_the_measured_count(oml_env):
    n = len(run_checks())
    assert n > 0
    for name, doc in (("README.md", README), ("docs/TROUBLESHOOTING.md", TROUBLESHOOTING)):
        assert "25+" not in doc, f"R4-15: {name} still claims 25+ checks; measured {n}"
        claims = [int(x) for x in re.findall(r"(\d+)\+?\s+(?:health\s+)?checks", doc)]
        assert claims, f"R4-15: {name} states no check count at all"
        assert all(c == n for c in claims), \
            f"R4-15: {name} claims {claims} but a clean isolated env runs {n}"


# ------------------------------------------------------------------- R4-18
def test_r4_18_changelog_states_the_laptop_only_deep_flows():
    assert "deep interaction flows run at laptop width (1280 px) only" in CHANGELOG, \
        "R4-18: CHANGELOG implies the deep flows run at all six widths"
    assert "responsive flow set" in CHANGELOG, \
        "R4-18: the other five widths run the responsive flows — say so"
    assert re.search(r"flows_laptop", CHECKER) and re.search(r'if label == "laptop":', CHECKER), \
        "R4-18: the checker dispatch this wording describes must still be the real one"


# ------------------------------------------------------------------- R4-19
async def test_r4_19_tui_cast_framing_matches_the_bridge(oml_env, svc, bridge):
    _write_skill(oml_env["home"])
    instruction = "run it for this repo"
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        app.screen.query_one("#chat-input").value = f"/skill:probe-skill {instruction}"
        await pilot.press("enter")
        await pilot.pause(0.8)
        users = [m for m in app.session.messages if m.get("role") == "user"]
        assert users, "the TUI cast never reached the session"
        tui_text = users[-1]["content"]
    sid = bridge.post("/api/chats", {"first_message": ""})["id"]
    r = bridge.post("/api/skills/cast",
                    {"name": "probe-skill", "session": sid, "args": instruction})
    assert r.get("ok") is True, f"bridge cast rejected: {r}"
    bridge_text = bridge.get(f"/api/chats/{sid}")["messages"][-1]["content"]
    assert "Additional instruction from the user:" in bridge_text, \
        "the bridge framing changed — re-check the parity reference"
    assert tui_text == bridge_text, \
        f"R4-19: TUI cast framing differs from the bridge:\nTUI={tui_text!r}\nBRIDGE={bridge_text!r}"


def test_r4_19_gui_unknown_mode_errors_instead_of_opening_the_chooser():
    fn = _js_fn("setModeUi")
    m = re.search(r"if \(!MODE_DESC\[preset\]\)", fn)
    assert m, "R4-19: an unknown /mode argument silently opened the chooser"
    body = _brace_body(fn, m.start())
    assert "toast(" in body and "return;" in body, \
        "R4-19: plain/TUI print an error — the GUI must reject, not open the chooser"
    assert fn.index("chooseMode(preset)") > m.start(), \
        "R4-19: only a KNOWN preset may reach chooseMode"


def test_r4_19_action_map_matches_the_tool_registry():
    m = re.search(r"const ACTION = \{(.*?)\n\};", APP_JS, re.S)
    assert m, "the ACTION icon map is missing"
    keys = set(re.findall(r"(\w+):\s*\[", m.group(1)))
    names = set(build_default_registry().names())
    stale = {"glob", "grep", "web_fetch", "web_search", "soul_write", "soul_remember"}
    assert not (keys & stale), f"R4-19: stale tool names still in ACTION: {sorted(keys & stale)}"
    assert keys == names, \
        f"R4-19: ACTION drifts from the registry — missing {sorted(names - keys)}, extra {sorted(keys - names)}"


def test_r4_19_checker_registers_the_r4_live_flows():
    for name in ("r4-approval-focus", "r4-close-tab-recovery", "r4-roving-tabindex",
                 "r4-sidebar-focus", "r4-skill-draft", "r4-mode-chip", "r4-market-error",
                 "r4-row-aria", "r4-help-esc", "r4-action-map"):
        assert f'"{name}"' in CHECKER, f"R4: the checker never runs the {name} live flow"
