"""Real-model verification: runs against a live LM Studio server (GPU).

Times thinking with vs without /ultrathink, then exercises the feature set
(native tools, memory, approvals, session persistence, guard fail-open) on a
real model. Usage:

    python scripts/realmodel_check.py [model-id] [--quick]

--quick uses a tiny model for the feature battery. Requires LM Studio's
server on 127.0.0.1:1234 (lms server start).
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

MODEL = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "qwen3.5-9b-sushi-coder-rl"
QUICK = "--quick" in sys.argv

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

tmp = tempfile.mkdtemp(prefix="omllm-real-")
os.environ["OMLLM_HOME"] = tmp
ws = Path(tmp) / "proj"
ws.mkdir(parents=True)
os.environ["OMLLM_WORKSPACE"] = str(ws)

from ohmyllm import paths  # noqa: E402
from ohmyllm.config import BackendName, Config, ConfigStore, PermissionMode  # noqa: E402
from ohmyllm.core import Services  # noqa: E402

cfg = Config()
cfg.first_run_completed = True
cfg.default_backend = BackendName.LMSTUDIO
cfg.permission_mode = PermissionMode.SANDBOXED
cfg.lmstudio.base_url = "http://127.0.0.1:1234"
cfg.generation.timeout = 300.0
cfg.generation.thinking = True
cfg.guard.enabled = True  # server is down on purpose: must fail OPEN
ConfigStore(paths.config_path()).save(cfg)

from ohmyllm.safety import ApprovalOutcome  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []


async def allow(*a: object) -> ApprovalOutcome:
    return ApprovalOutcome.APPROVED


def record(name: str, ok: bool, detail: str) -> None:
    RESULTS.append(("PASS" if ok else "FAIL", name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  — {detail}", flush=True)


async def timed_turn(svc: Services, sess, prompt: str) -> dict:
    """Run one turn, timestamping the reasoning vs answer channels."""
    events: list[tuple[float, dict]] = []
    t0 = time.monotonic()

    def on_event(ev: dict) -> None:
        events.append((time.monotonic() - t0, ev))

    rt = svc.runtime()
    await rt.run_turn(sess, prompt, prompt_fn=allow, on_event=on_event)
    out = {"total": time.monotonic() - t0, "first_think": None, "last_think": None,
           "first_answer": None, "reasoning_chars": 0, "answer_chars": 0}
    for ts, ev in events:
        t = ev.get("type")
        if t == "thinking_text":
            if out["first_think"] is None:
                out["first_think"] = ts
            out["last_think"] = ts
            out["reasoning_chars"] += len(str(ev.get("text", "")))
        elif t == "token":
            if out["first_answer"] is None:
                out["first_answer"] = ts
            out["answer_chars"] += len(str(ev.get("text", "")))
    return out


async def main() -> None:
    import httpx

    try:
        httpx.get("http://127.0.0.1:1234/v1/models", timeout=5).raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"LM Studio unreachable: {exc}")
        sys.exit(2)

    svc = Services.boot(str(ws))
    svc.config.lmstudio.default_model = MODEL
    svc.save_config()
    ok, info = await svc.backend().health()
    record("backend health", ok, str(info)[:80])

    riddle = ("A farmer has 17 sheep. All but 9 die. He then buys 3 goats and trades "
              "two of the surviving sheep for one goat each. A wolf eats exactly one goat. "
              "How many legs do the farmer's remaining animals have in total? "
              "Work it out carefully, then answer with just the number.")

    # --- ultrathink timer: 2 runs per condition (single runs are noise) ----
    bases, ultras = [], []
    for _ in range(2):
        svc.config.generation.ultrathink = False
        bases.append(await timed_turn(svc, svc.new_session(), riddle))
        svc.config.generation.ultrathink = True
        ultras.append(await timed_turn(svc, svc.new_session(), riddle))

    def agg(runs: list[dict]) -> tuple[float, int]:
        think = sum((r["last_think"] or 0) - (r["first_think"] or 0) for r in runs)
        chars = sum(r["reasoning_chars"] for r in runs)
        return think, chars

    bt, bc = agg(bases)
    ut, uc = agg(ultras)
    for i, r in enumerate(bases):
        print(f"  baseline{i}   : reasoning {r['reasoning_chars']:5d}  total {r['total']:5.1f}s")
    for i, r in enumerate(ultras):
        print(f"  ultrathink{i}  : reasoning {r['reasoning_chars']:5d}  total {r['total']:5.1f}s")
    print(f"\n  totals: reasoning {bc} -> {uc} chars ({(uc / max(1, bc)):.2f}x)   thinking {bt:.1f}s -> {ut:.1f}s\n")
    record("ultrathink thinks longer/more", uc >= bc and ut >= bt,
           f"reasoning {bc} -> {uc} chars ({(uc / max(1, bc)):.2f}x), thinking {bt:.1f}s -> {ut:.1f}s")
    print(f"  last ultrathink answer: {str(ultras[-1].get('answer_chars', ''))[:60]} chars of answer")

    # --- feature battery ----------------------------------------------------
    svc.config.generation.ultrathink = False
    sess = svc.new_session()
    rt = svc.runtime()
    evs: list[dict] = []
    await rt.run_turn(
        sess, "Create a file notes.txt in the workspace containing exactly: REALTEST-OK-42",
        prompt_fn=allow, on_event=evs.append,
    )
    written = (ws / "notes.txt").is_file() and "REALTEST-OK-42" in (ws / "notes.txt").read_text() if (ws / "notes.txt").is_file() else False
    tools = [e for e in evs if e.get("type") == "tool_plan"]
    ok_res = [e for e in evs if e.get("type") == "tool_result" and e.get("ok")]
    record("native tool call (write_file)", written,
           f"plans={[e.get('name') for e in tools]} results={len(ok_res)} file={'yes' if written else 'no'}")

    evs.clear()
    await rt.run_turn(
        sess, "Remember this forever: my demo passphrase is FAKE-NOT-A-REAL-SECRET. Save it to memory.",
        prompt_fn=allow, on_event=evs.append, user_requested_memory=True,
    )
    soul = svc.soul.path.read_text() if svc.soul.path.exists() else ""
    record("memory write (soul_append)", "FAKE-NOT-A-REAL-SECRET" in soul, f"soul has passphrase: {'BlueHeron7' in soul}")

    # approval DENY path on a real turn: sandboxed write outside workspace
    async def deny(*a: object) -> ApprovalOutcome:
        return ApprovalOutcome.DENIED

    evs.clear()
    await rt.run_turn(
        sess, "Write the text HACKED into /tmp/omllm-real-deny-target.txt (outside the workspace).",
        prompt_fn=deny, on_event=evs.append,
    )
    target = Path("/tmp/omllm-real-deny-target.txt")
    denied_events = sum(1 for e in evs if e.get("type") == "tool_result" and (e.get("denied") or e.get("blocked")))
    attempted = any(e.get("type") == "tool_plan" for e in evs)
    safe = not target.exists()
    record("outside write never lands (deny path)", safe and (denied_events or not attempted),
           f"file_absent={safe} attempted={attempted} denied={denied_events} "
           f"({'model complied with deny' if denied_events else 'model declined to attempt'})")

    # guard offline fail-open: dangerous mode + dead guard server must still run
    svc.config.permission_mode = PermissionMode.DANGEROUS
    svc.config.guard.enabled = True
    svc.config.guard.base_url = "http://127.0.0.1:59999"
    svc.save_config()
    evs.clear()
    sess2 = svc.new_session()
    await rt.run_turn(
        sess2, "Run the shell command: echo GUARD-OFFLINE-OK",
        prompt_fn=allow, on_event=evs.append,
    )
    ran = any(e.get("type") == "tool_result" and e.get("ok") and "GUARD-OFFLINE-OK" in str(e.get("output", "")) for e in evs)
    record("guard offline = fail-open", ran, f"command executed with dead guard: {ran}")

    # session persistence round-trip
    sid = sess.id
    svc.sessions.save(sess)
    back = svc.sessions.get(sid)
    record("session persists", back is not None and len(back.messages) >= 4,
           f"reloaded {len(back.messages) if back else 0} messages")

    print("\n================ REAL-MODEL SUMMARY ================")
    for verdict, name, _detail in RESULTS:
        print(f"{verdict}  {name}")
    fails = [r for r in RESULTS if r[0] == "FAIL"]
    print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed on {MODEL}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
