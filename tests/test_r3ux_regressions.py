"""R3 UX/docs regressions (task #65) — one test per finding where testable.

Reproduce-first discipline: this file was written BEFORE the remaining fixes and
run against the pristine f8d6952 control in .validation/r3ux-prefix, where every
finding below failed (evidence: .validation/jimmy-r3ux-prefix-reproduce.log).
The browser-only PoCs (U1 misrouted approval id, U2 live-surface contrast, U3
stale /yolo, U5/U6 case, U10 blank stream, U11 stranded focus) are asserted live
in scripts/gui_headless_check.py; what is provable offline is pinned here.

Guard-compatible with scripts/verify_isolated.py: no subprocess, no browser,
loopback bridges on ephemeral ports, Textual pilots only.
"""

import io
import re
from pathlib import Path

from rich.console import Console

from ohmyllm.plain import PlainApp
from ohmyllm.tui.app import OhMyLLMApp
from ohmyllm.tui.screens.chat import ChatScreen
from ohmyllm.tui.screens.help import HELP

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "src" / "ohmyllm" / "gui_static" / "app.js").read_text(encoding="utf-8")
STYLES = (ROOT / "src" / "ohmyllm" / "gui_static" / "styles.css").read_text(encoding="utf-8")
CHECKER = (ROOT / "scripts" / "gui_headless_check.py").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
BACKENDS = (ROOT / "docs" / "BACKENDS.md").read_text(encoding="utf-8")
USE_CASES = (ROOT / "docs" / "USE-CASES.md").read_text(encoding="utf-8")

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


def _slash_case(cmd: str) -> str:
    m = re.search(rf'case "{re.escape(cmd)}":', APP_JS)
    assert m, f"{cmd} case not found"
    nxt = re.search(r'\n      case "', APP_JS[m.end():])
    end = m.end() + (nxt.start() if nxt else 4000)
    return APP_JS[m.start():end]


def _checker_fn(name: str) -> str:
    m = re.search(rf"^def {re.escape(name)}\(.*?(?=^def |\Z)", CHECKER, re.M | re.S)
    assert m, f"{name}() not found in the checker"
    return m.group(0)


# --------------------------------------------------------------- WCAG math
def _rgb(value: str) -> tuple[float, float, float]:
    value = value.strip()
    if value.startswith("#"):
        h = value[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    nums = [float(x) for x in re.findall(r"[\d.]+", value)]
    return tuple(nums[:3])  # type: ignore[return-value]


def _alpha(value: str) -> float:
    nums = [float(x) for x in re.findall(r"[\d.]+", value)]
    return nums[3] if len(nums) > 3 else 1.0


def _lum(c) -> float:
    def ch(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(v) for v in c)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _ratio(a, b) -> float:
    la, lb = _lum(a), _lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _over(fg, alpha: float, bg):
    return tuple(alpha * f + (1 - alpha) * b for f, b in zip(fg, bg, strict=True))


def _token(name: str) -> str:
    m = re.search(rf"--{re.escape(name)}:\s*([^;]+);", STYLES)
    assert m, f"--{name} token missing"
    return m.group(1).strip()


def _rule_color(selector: str) -> str:
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", STYLES)
    assert m, f"{selector} rule missing"
    c = re.search(r"(?:^|;)\s*color:\s*([^;]+);", m.group(1))
    assert c, f"{selector} has no color declaration"
    return c.group(1).strip()


def _resolve(value: str) -> str:
    m = re.fullmatch(r"var\(--([\w-]+)\)", value.strip())
    return _token(m.group(1)) if m else value.strip()


# ------------------------------------------------------- control integrity
def test_control_python_sources_are_the_ones_under_test():
    """Reproduce-first control: the imported package must come from THIS tree.

    Without this, a snapshot run could silently exercise the live sources and
    'prove' a pre-fix failure that was never actually observed.
    """
    import ohmyllm
    from ohmyllm.tui.screens import chat as chat_mod

    assert Path(ohmyllm.__file__).resolve().is_relative_to((ROOT / "src").resolve()), \
        f"control leak: ohmyllm imported from {ohmyllm.__file__}, expected under {ROOT / 'src'}"
    assert Path(chat_mod.__file__).resolve().is_relative_to((ROOT / "src").resolve())


# --------------------------------------------------------------------- U1
def test_u1_inline_card_click_decides_the_clicked_id():
    handler = re.search(r'\$\("#stream"\)\.addEventListener\("click".*?\n\}\);', APP_JS, re.S)
    assert handler, "stream click handler missing"
    body = handler.group(0)
    assert re.search(r"answerApproval\(\s*b\.dataset\.dec\s*,\s*"
                     r"b\.closest\(['\"]\.app-card['\"]\)\.dataset\.aid\s*\)", body), \
        "U1: the inline card button must pass its OWN card id, not the queue head"


def test_u1_answer_and_close_resolve_by_id():
    answer = _js_fn("answerApproval")
    assert re.search(r"async function answerApproval\(decision,\s*aid\)", answer), \
        "U1: answerApproval must accept the request id"
    assert "state.approvalQueue.find((x) => x.id === aid)" in answer
    assert "closeApproval(d.id)" in answer
    close = _js_fn("closeApproval")
    assert "findIndex((x) => x.id === aid)" in close and ".splice(i, 1)" in close, \
        "U1: closeApproval must drop the decided id, never shift() the head"
    assert ".shift()" not in close


def test_u1_background_is_inert_while_the_approval_modal_is_up():
    assert "function syncApprovalInert()" in APP_JS
    inert = _js_fn("syncApprovalInert")
    assert '"#main"' in inert and '"#sidebar"' in inert and ".inert = true" in inert
    for fn in ("openModal", "closeModal"):
        assert "syncApprovalInert()" in _js_fn(fn), f"U1: {fn} must sync the inert background"


# --------------------------------------------------------------------- U2
def test_u2_live_secondary_surfaces_use_dim_not_mute():
    for sel in (".sb-row .prev", ".sb-row time", ".chip .cnt"):
        assert _resolve(_rule_color(sel)) == _token("dim"), \
            f"U2: {sel} must render --dim (AA on bg3 / acc-dim), not --mute"


def test_u2_live_surface_contrast_meets_wcag_aa():
    bg3 = _rgb(_token("bg3"))
    dim = _rgb(_token("dim"))
    mute = _rgb(_token("mute"))
    acc_dim = _token("acc-dim")
    # the finding: --mute measures 4.36:1 on bg3 — below the 4.5:1 AA floor
    assert _ratio(mute, bg3) < 4.5, "premise changed: --mute now passes on bg3"
    assert _ratio(dim, bg3) >= 4.5
    for sel in (".sb-row .prev", ".sb-row time"):
        assert _ratio(_rgb(_resolve(_rule_color(sel))), bg3) >= 4.5, f"U2: {sel} on bg3"
    # .chip.active paints acc-dim over the canvas: composite over every base
    cnt = _rgb(_resolve(_rule_color(".chip .cnt")))
    overlay = _rgb(acc_dim)
    a = _alpha(acc_dim)
    worst = min(_ratio(cnt, _over(overlay, a, _rgb(_token(b))))
                for b in ("bg0", "bg1", "bg2", "bg3"))
    assert worst >= 4.5, f"U2: .chip.active .cnt worst-case contrast {worst:.2f}:1"


# --------------------------------------------------------------------- U3
def test_u3_bare_yolo_toggles_from_server_truth():
    block = _slash_case("/yolo")
    assert "state.status && state.status.yolo" in block, \
        "U3: bare /yolo must read the live server state, never the stale local flag"
    assert re.search(r"state\.status\.yolo\s*=\s*want", block), \
        "U3: the local mirror must be updated so the next bare toggle is honest"


# --------------------------------------------------------------------- U4
def test_u4_gui_cast_sends_the_trailing_instruction():
    m = re.search(r'post\("/api/skills/cast",\s*\{([^}]*)\}\)', APP_JS)
    assert m, "skill cast POST not found"
    fields = m.group(1)
    assert "name" in fields and "session" in fields, "existing field semantics must not change"
    assert re.search(r"\bargs:\s*arg\b", fields), \
        "U4: the trailing instruction must travel as the additive `args` string field"


async def test_u4_tui_cast_carries_the_instruction(oml_env, svc):
    _write_skill(oml_env["home"])
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        assert app.session is not None
        app.screen.query_one("#chat-input").value = "/skill:probe-skill run it for this repo"
        await pilot.press("enter")
        await pilot.pause(0.8)
        users = [m for m in app.session.messages if m.get("role") == "user"]
        assert users, "the cast never reached the session"
        text = users[-1]["content"]
        assert "Do the probe." in text, "skill body missing"
        assert "run it for this repo" in text, \
            "U4: the TUI dropped the trailing instruction after /skill:name"


async def test_u4_tui_cast_without_instruction_keeps_legacy_message(oml_env, svc):
    _write_skill(oml_env["home"])
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        app.screen.query_one("#chat-input").value = "/skill:probe-skill"
        await pilot.press("enter")
        await pilot.pause(0.8)
        users = [m for m in app.session.messages if m.get("role") == "user"]
        assert users
        assert users[-1]["content"].endswith("Apply this skill to my requests in this chat."), \
            "U4: an empty instruction must keep the legacy message byte-identical"


def test_u4_bridge_cast_accepts_the_additive_args_field(oml_env, svc, bridge):
    """Cross-wave contract: POST /api/skills/cast takes an optional `args`."""
    _write_skill(oml_env["home"])
    sid = bridge.post("/api/chats", {"first_message": ""})["id"]
    r = bridge.post("/api/skills/cast",
                    {"name": "probe-skill", "session": sid, "args": "run it for this repo"})
    assert r.get("ok") is True, f"U4 contract: cast rejected the additive args field: {r}"
    sess = bridge.get(f"/api/chats/{sid}")
    text = sess["messages"][-1]["content"]
    assert "run it for this repo" in text, "U4: the instruction never reached the session"
    legacy = bridge.post("/api/skills/cast", {"name": "probe-skill", "session": sid})
    assert legacy.get("ok") is True, "U4: the legacy two-field cast must keep working"


def test_u4_docs_describe_the_trailing_instruction():
    assert "/skill:release-checklist run it for this repo" in USE_CASES
    assert "instruction" in USE_CASES.lower()


# ----------------------------------------------------------------- U5/U6
def test_u5_gui_mode_normalizes_case_like_plain():
    block = _slash_case("/mode")
    assert re.search(r'setModeUi\(\(arg \|\| ""\)\.toLowerCase\(\)', block), \
        "U5: /mode SAFE must apply the mode, not fall through to the chooser"


def test_u6_gui_tone_normalizes_case_like_plain():
    block = _slash_case("/tone")
    assert re.search(r"const want = arg\.toLowerCase\(\);", block), \
        "U6: /tone TERSE must be normalized before validation"
    assert re.search(r"opts\.includes\(want\)", block)
    assert re.search(r"\{\s*tone:\s*want,\s*confirm:\s*true\s*\}", block), \
        "U6: the normalized tone must be what gets posted"


async def test_u5_u6_plain_reference_is_case_insensitive(oml_env, svc):
    """The parity reference: plain already lowercases both arguments."""
    app = PlainApp(svc)
    app.console = Console(file=io.StringIO(), width=200)
    await app._slash("/tone TERSE")
    assert svc.config.agent.tone == "terse"
    app._mode("SANDBOXED")
    assert svc.config.permission_mode.value == "sandboxed"


# --------------------------------------------------------------------- U7
def test_u7_docs_drop_the_stale_measured_counts():
    stale = ("916 passed", "178/178", "921 tests")
    for s in stale:
        assert s not in CHANGELOG, f"U7: CHANGELOG still claims {s!r}"
        assert s not in README, f"U7: README still claims {s!r}"


# --------------------------------------------------------------------- U8
def test_u8_checker_approval_stub_is_id_aware():
    src = _checker_fn("f_r2_approval_queue")
    assert "lambda route: route.fulfill" not in src, \
        "U8: a blanket 200 stub accepts any id and hides misrouted decisions"
    assert "post_data" in src and "404" in src, \
        "U8: the stub must read the request and 404 ids that are not queued"
    assert "state.approvalQueue.map" in src, "U8: the stub must validate against the live queue"
    assert re.search(r'decided\[-1\]\["id"\] == "r2[ab]"', src), \
        "U8: the flow must assert WHICH id was decided"


# --------------------------------------------------------------------- U9
def test_u9_checker_covers_the_768px_breakpoint():
    m = re.search(r"^WIDTHS = \{(.*)\}$", CHECKER, re.M)
    assert m and "768" in m.group(1), "U9: 768px (the overlay breakpoint) is untested"
    assert re.search(r'"tablet":\s*"gui-tablet\.png"', CHECKER), \
        "U9: the 768px capture pass needs a screenshot slot"


def test_u9_checker_banner_reports_numeric_counts():
    assert "passed, {len(FAILURES)} failed," in CHECKER, \
        "U9: the banner must state passed/failed/total, not a bare ALL CHECKS PASSED"
    assert re.search(r'COUNTS\["pass"\]', CHECKER), "U9: passes must actually be counted"


def test_u9_no_hardcoded_pass_assertions():
    hits = re.findall(r"^\s*check\(.*,\s*True\s*\)\s*$", CHECKER, re.M)
    assert hits == [], f"U9: {len(hits)} check(..., True) sites assert nothing: {hits[:3]}"


# -------------------------------------------------------------------- U10
def test_u10_rejected_send_restores_the_hero_not_a_blank_stream():
    fn = _js_fn("sendNormal")
    m = re.search(r"if \(!accepted && !partial\) \{(.*?)\n  \}", fn, re.S)
    assert m, "U10: the rejection branch is missing"
    branch = m.group(1)
    assert "userNode.remove()" in branch, "the phantom user bubble must go"
    assert re.search(r'if \(!\$\("#stream"\)\.querySelector\("\.msg"\)\) emptyState\(\);', branch), \
        "U10: an emptied stream must repaint the hero, never stay blank"
    assert "restoreDraft(text)" in branch, "U10: the rejected draft must survive"


# -------------------------------------------------------------------- U11
def test_u11_sidebar_close_hands_focus_back_before_inert():
    fn = _js_fn("setSidebar")
    focus = re.search(r'if \(!open && sb\.contains\(document\.activeElement\)\)\s*'
                      r'\$\("#btn-sidebar"\)\.focus\(\);', fn)
    assert focus, "U11: closing the sidebar must restore focus to the toggle"
    inert = fn.index("sb.inert = !open")
    assert focus.start() < inert, \
        "U11: focus must move BEFORE inert blurs the focused descendant onto <body>"


# -------------------------------------------------------------------- U12
def test_u12_tui_help_documents_remember():
    assert "/remember" in HELP, "U12: /remember is missing from the TUI help screen"
    assert any(cmd == "/remember" for cmd, _a, _d in ChatScreen.SLASH)


def test_u12_backends_think_tags_are_real_literals():
    assert "`...`" not in BACKENDS, "U12: placeholder '...' is not a think tag"
    assert "`<think>…</think>`" in BACKENDS, "U12: the real tag pair must be shown"
    assert "`＜think＞…＜/think＞`" in BACKENDS, "U12: the full-width variant must be shown"
    assert "◁think▷" in BACKENDS, "U12: the triangle variant must be shown"
