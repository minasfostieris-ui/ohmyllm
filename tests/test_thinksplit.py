"""Reasoning-format support for models we cannot host: every family's exact
wire format is simulated chunk-by-chunk, including tags split mid-delta."""
from __future__ import annotations

import asyncio
import json

import httpx

from ohmyllm.backends.lmstudio import LMStudioBackend
from ohmyllm.backends.thinksplit import ThinkSplitter
from ohmyllm.config import Config
from ohmyllm.config.models import GenerationSettings

THINK_OPEN = "<" + "think>"
THINK_CLOSE = "<" + "/think>"
REASON_OPEN = "<" + "reasoning>"
REASON_CLOSE = "<" + "/reasoning>"


def split(deltas: list[str]) -> tuple[str, str]:
    sp = ThinkSplitter()
    r, c = [], []
    for d in deltas:
        for kind, text in sp.feed(d):
            (r if kind == "reasoning" else c).append(text)
    for kind, text in sp.flush():
        (r if kind == "reasoning" else c).append(text)
    return "".join(r), "".join(c)


def _chunks(s: str, n: int) -> list[str]:
    return [s[i : i + n] for i in range(0, len(s), n)] or [""]


def test_qwen3_deepseek_style_tags():
    raw = (THINK_OPEN + "Let me count: 9 sheep alive + 3 goats - 1 eaten = 11 animals, "
           "4 legs each. 44.\n" + THINK_CLOSE + "The answer is 44.")
    reasoning, content = split(_chunks(raw, 7))
    assert "count: 9 sheep" in reasoning
    assert content == "The answer is 44."


def test_fullwidth_kimi_glm_variant():
    raw = "hmm＜think＞pondering＜/think＞ok"
    reasoning, content = split(_chunks(raw, 4))
    assert reasoning == "pondering"
    assert content == "hmmok"


def test_thinking_and_reasoning_tag_variants():
    for op, cl in (("<thinking>", "</thinking>"), (REASON_OPEN, REASON_CLOSE),
                   ("<thought>", "</thought>")):
        reasoning, content = split([op + "deep" + cl + "final"])
        assert (reasoning, content) == ("deep", "final"), op


def test_no_tags_is_all_content():
    reasoning, content = split(["just ", "plain ", "text"])
    assert reasoning == ""
    assert content == "just plain text"


def test_unclosed_block_is_all_reasoning():
    reasoning, content = split(_chunks(THINK_OPEN + "rambling on and on", 5))
    assert reasoning == "rambling on and on"
    assert content == ""


def test_stray_closer_without_opener_is_stripped():
    reasoning, content = split(["answer here", THINK_CLOSE, " more"])
    assert content == "answer here more"
    assert reasoning == ""


def test_angle_bracket_in_prose_not_swallowed():
    reasoning, content = split(["if a < b", " then 1 else 0"])
    assert content == "if a < b then 1 else 0"
    assert reasoning == ""


def test_code_fence_after_think_block_survives():
    fence = "`" * 3
    raw = THINK_OPEN + "why the fence works" + THINK_CLOSE + "\n" + fence + "py\nprint(1)\n" + fence
    reasoning, content = split(_chunks(raw, 6))
    assert reasoning == "why the fence works"
    assert fence + "py" in content


def test_tag_split_across_every_boundary():
    raw = "premid" + THINK_OPEN + "buried" + THINK_CLOSE + "post"
    for size in range(1, len(raw) + 1):
        reasoning, content = split(_chunks(raw, size))
        assert reasoning == "buried", size
        assert content == "premidpost", size


# ---------------------------------------------------------------- wire level

def _sse(deltas: list[dict]) -> httpx.MockTransport:
    lines = []
    for d in deltas:
        lines.append("data: " + json.dumps({"choices": [{"delta": d}]}) + "\n\n")
    lines.append("data: [DONE]\n\n")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="".join(lines))

    return httpx.MockTransport(handler)


def _collect(deltas: list[dict]) -> tuple[str, str]:
    cfg = Config()
    be = LMStudioBackend(cfg, transport=_sse(deltas))

    async def go() -> tuple[str, str]:
        r, c = [], []
        async for ch in be.stream_chat("m", [{"role": "user", "content": "x"}], GenerationSettings()):
            r.append(ch.reasoning or "")
            c.append(ch.content or "")
        return "".join(r), "".join(c)

    return asyncio.run(go())


def test_lmstudio_inline_qwen3_stream_is_clean():
    deltas = [{"content": THINK_OPEN + "step by step"}, {"content": " math" + THINK_CLOSE},
              {"content": "FINAL ANSWER 44"}]
    reasoning, content = _collect(deltas)
    assert reasoning == "step by step math"
    assert content == "FINAL ANSWER 44"
    assert THINK_OPEN not in content and THINK_CLOSE not in content


def test_lmstudio_native_reasoning_field_still_works():
    # gpt-oss / Nemotron-Cascade / vLLM parsers: reasoning arrives as its own field
    deltas = [{"reasoning": "analyzing"}, {"reasoning": " more"}, {"content": "done"}]
    reasoning, content = _collect(deltas)
    assert reasoning == "analyzing more"
    assert content == "done"


def test_lmstudio_reasoning_content_field_deepseek_vllm():
    deltas = [{"reasoning_content": "hmm"}, {"content": "ok"}]
    reasoning, content = _collect(deltas)
    assert reasoning == "hmm"
    assert content == "ok"


def test_lmstudio_thinking_only_inline_fallback():
    # model thought the whole turn and never closed: answer = the thoughts
    deltas = [{"content": THINK_OPEN + "long reasoning never closed"}]
    reasoning, content = _collect(deltas)
    assert "long reasoning" in reasoning
    assert "long reasoning" in content  # fallback so the user sees an answer


def test_lmstudio_mixed_native_field_and_inline_tags():
    # server parses reasoning_content AND the model still emits a stray block
    deltas = [{"reasoning_content": "field thought"},
              {"content": "pre" + THINK_OPEN + "inline thought" + THINK_CLOSE + "post"}]
    reasoning, content = _collect(deltas)
    assert "field thought" in reasoning and "inline thought" in reasoning
    assert content == "prepost"


def test_lmstudio_raw_r1_content_style():
    # DeepSeek-R1 raw llama.cpp: everything in content,  at the very start
    deltas = [{"content": THINK_OPEN + "A: solve"}]
    reasoning, content = _collect(deltas)
    assert reasoning == "A: solve"
    # thinking-only stream fallback: the thoughts become the answer so the
    # user is never shown an empty turn
    assert content == "A: solve"
    r2, c2 = _collect([{"content": THINK_OPEN + "x" + THINK_CLOSE + "The answer"}])
    assert c2 == "The answer" and r2 == "x"


# ------------------------------------------------------- token efficiency & knobs

def test_head_tail_clip_keeps_the_error_tail():
    from ohmyllm.agents.runtime import _clip_headtail
    text = "A" * 5000 + "\nFAILED: the real error is here"
    clipped = _clip_headtail(text, 3000)
    assert len(clipped) < len(text)
    assert clipped.endswith("the real error is here"), "traceback tail must survive"
    assert "chars skipped" in clipped
    assert _clip_headtail("short", 3000) == "short"


def test_shell_cap_keeps_tail():
    from ohmyllm.tools.shell_tools import OUTPUT_CAP, _cap
    text = "x" * (OUTPUT_CAP + 2000) + "\nAssertionError: boom"
    out = _cap(text)
    assert out.endswith("AssertionError: boom")
    assert len(out) < len(text)


def test_memory_block_is_last_for_prefix_cache_stability(oml_env, svc):
    """llama.cpp/LM Studio/Ollama cache the prompt prefix; soul.md changes
    mid-session, so the memory block must sit at the END of the system prompt
    and the stable blocks before it must not move."""
    soul = svc.soul
    soul.ensure()
    soul.append(section="User", text="early fact")
    pb1 = svc.runtime().prompt_builder
    first = pb1.system_prompt(mode=svc.policy.mode, native=True)
    soul.append(section="User", text="later fact added mid-session")
    second = pb1.system_prompt(mode=svc.policy.mode, native=True)
    marker = "## Memory"
    assert first.index(marker) > first.index("Current mode:"), "memory must come after mode_note"
    stable = second[: second.index(marker)]
    assert stable == first[: first.index(marker)], "prefix before memory must be byte-identical"


def test_thinking_budget_opt_in_wire():
    import asyncio

    cfg = Config()
    cfg.generation.ultrathink = True
    cfg.generation.thinking_budget = 8000
    cap: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["body"] = json.loads(request.content.decode())
        return httpx.Response(200, text="data: [DONE]\n\n")

    import httpx as _h
    be = LMStudioBackend(cfg, transport=_h.MockTransport(handler))

    async def go():
        async for _ in be.stream_chat("m", [{"role": "user", "content": "x"}], cfg.generation):
            pass

    asyncio.run(go())
    assert cap["body"]["thinking_budget"] == 8000
