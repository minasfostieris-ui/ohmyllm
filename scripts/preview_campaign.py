"""2000-scenario visual campaign against the INSTALLED wheel.

Each scenario drives a realistic write-file streaming sequence (native
tool_stream events, text-mode token JSON, or the full approval lifecycle)
at a deterministic size, then asserts:
  - the live preview exists, is colored (bold cyan header + bright_white rail)
  - it GROWS across the stream and reaches the final content line
  - no raw JSON preamble leaks into the chat as a message
  - the separator bar sits at one constant pixel column on every row and
    never intrudes on the input/status rows
  - no app exception
Every scenario exports its screenshot to imgs/scen<N>.svg.

Speed model: scenarios are grouped by terminal size (deterministic from the
seed), one app boot per size, all size-groups run concurrently. 2000 boots
become 9.
"""
import asyncio
import json
import os
import pathlib
import random
import re
import sys

_root = pathlib.Path(os.environ.get("OMLLM_CAMPAIGN_DIR", "/tmp/campaign"))
home = _root / "home"
ws = _root / "ws"
home.mkdir(parents=True, exist_ok=True)
ws.mkdir(exist_ok=True)
(home / "config.toml").write_text('first_run_completed = true\ndefault_backend = "mock"\n')
os.environ["OMLLM_HOME"] = str(home)
os.environ["OMLLM_WORKSPACE"] = str(ws)
IMG = _root / "imgs"
IMG.mkdir(exist_ok=True)

from textual.containers import VerticalScroll  # noqa: E402

from ohmyllm.tui.app import OhMyLLMApp  # noqa: E402
from ohmyllm.tui.widgets import ToolPanel  # noqa: E402

SIZES = [(70, 20), (80, 22), (90, 24), (100, 26), (110, 28), (120, 32), (140, 36), (160, 42), (200, 50)]


def scenario_params(n: int):
    rng = random.Random(n)
    size = rng.choice(SIZES)
    mode = n % 3
    lines = [f"row {i} unique-{rng.randrange(10**6)}" for i in range(rng.randrange(20, 60))]
    content = "\n".join(lines) + "\n"
    return size, mode, lines, content


def cells(svg: str):
    rows: dict[int, list[tuple[float, str]]] = {}
    for m in re.finditer(r'<text[^>]*x="([\d.]+)"[^>]*y="([\d.]+)"[^>]*>(.*?)</text>', svg, re.S):
        x, y, body = float(m.group(1)), float(m.group(2)), m.group(3)
        text = re.sub(r"<[^>]+>", "", body).replace("&#160;", " ").replace("&amp;", "&")
        rows.setdefault(round(y), []).append((x, text))
    return rows


def check_svg(n: int, svg: str) -> str | None:
    rows = cells(svg)
    xs = [x for _, items in rows.items() for x, t in items if "\u258f" in t]
    if not xs:
        return f"scen{n}: bar missing"
    if max(xs) - min(xs) > 0.01:
        return f"scen{n}: bar varies {min(xs)}..{max(xs)}"
    ys = sorted({round(float(m.group(1))) for m in re.finditer(r'<text[^>]*y="([\d.]+)"', svg)})
    bar_ys = sorted(y for y, items in rows.items() for x, t in items if "\u258f" in t)
    if bar_ys and ys and ys[-1] - bar_ys[-1] < 3 * ((ys[1] - ys[0]) if len(ys) > 1 else 24):
        return f"scen{n}: bar intrudes on input/status rows"
    for _, items in rows.items():
        for _, t in items:
            if t.lstrip().startswith('{"name"') or t.lstrip().startswith('{"path"'):
                return f"scen{n}: raw JSON preamble leaked: {t[:40]!r}"
    return None


async def run_scenario(app, pilot, n: int) -> str | None:
    size, mode, lines, content = scenario_params(n)
    scr = app.screen
    hist = scr.query_one("#history", VerticalScroll)
    hist.remove_children()
    panel = scr.query_one(ToolPanel)
    panel._lines.clear()
    panel.update("")
    scr._file_live = None
    scr._stream_md = None
    scr._think_md = None
    scr._buffer = ""
    scr._tool_live_active = False
    scr._file_pending = None
    scr._file_flush = 0.0
    panel.add_event({"type": "thinking", "step": 1})
    payload_native = json.dumps({"path": "f.txt", "content": content})
    cm = re.search(r'"content"\s*:\s*"', payload_native)
    assert cm
    growth: list[int] = []
    if mode in (0, 2):
        step = 70 if mode == 0 else 90
        for k in list(range(50, len(payload_native), step)) + [len(payload_native)]:
            scr._file_flush = 0.0
            scr._on_event({"type": "tool_stream", "name": "write_file", "chars": k,
                           "tail": payload_native[max(0, k - 900):k],
                           "content_tail": payload_native[cm.end():k][-1600:]})
            if scr._file_live:
                growth.append(len(scr._file_live._Static__content.plain))
        if mode == 2:
            scr._on_event({"type": "tool_plan", "name": "write_file", "level": "M",
                           "risk": 4, "approval": False, "allowed": True})
            scr._on_event({"type": "tool_result", "name": "write_file", "ok": True,
                           "file": {"path": "f.txt", "created": True, "added": len(lines)}})
    else:
        payload_text = json.dumps({"name": "write_file",
                                   "arguments": {"path": "f.txt", "content": content}})
        scr._buffer = ""
        for i in range(0, len(payload_text), 50):
            scr._on_event({"type": "token", "text": payload_text[i:i + 50]})
            if scr._file_live:
                growth.append(len(scr._file_live._Static__content.plain))
    if mode != 2:
        if scr._file_live is None:
            return f"scen{n} mode{mode}: preview vanished"
        body = scr._file_live._Static__content
        plain = body.plain
        if lines[-1] not in plain:
            return f"scen{n} mode{mode}: final line missing: {plain[-40:]!r}"
        styles = {str(sp.style) for sp in body.spans}
        if not {"bold cyan", "bright_white"} <= styles:
            return f"scen{n} mode{mode}: not colored {styles}"
        if len(growth) < 3 or growth[-1] <= growth[0]:
            return f"scen{n} mode{mode}: no growth {growth[:2]}->{growth[-1:]}"
    await pilot.pause(0.15)
    svg = app.export_screenshot()
    (IMG / f"scen{n}.svg").write_text(svg)
    err = check_svg(n, svg)
    if err:
        return err
    if app._exception:
        return f"scen{n}: exception {app._exception}"
    return None


async def size_worker(size, ids, results: list, done: list) -> None:
    """One boot for every scenario of this terminal size."""
    app = OhMyLLMApp()
    w, h = size
    async with app.run_test(size=(w, h)) as pilot:
        await pilot.pause(0.3)
        for n in ids:
            try:
                r = await run_scenario(app, pilot, n)
                if r:
                    results.append(r)
                    print("FAIL", r, flush=True)
            except Exception as exc:  # noqa: BLE001
                results.append(f"scen{n}: CRASH {type(exc).__name__}: {exc}")
            finally:
                done.append(1)
                if len(done) % 200 == 0:
                    print(f"progress {len(done)}, {len(results)} fails", flush=True)


async def main():
    total = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    groups: dict = {}
    for n in range(total):
        groups.setdefault(scenario_params(n)[0], []).append(n)
    results: list = []
    done: list = []
    await asyncio.gather(*(size_worker(s, ids, results, done) for s, ids in groups.items()))
    print(f"CAMPAIGN {'OK' if not results else 'FAILED'}: {total} scenarios, {len(results)} failures")
    return 0 if not results else 1


sys.exit(asyncio.run(main()))
