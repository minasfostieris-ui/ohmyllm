"""Model capability heuristics shared by the TUI/GUI model selectors.

Servers rarely tell us whether a model *can* think or see, so we infer from
the model name (and, where available, the chat template / mmproj scan).
Values are deliberately coarse: "levels" (graded /think works), "on-off"
(hybrid thinking toggle), "forced" (always thinks), "none", None=unknown.
"""
from __future__ import annotations

import re

_LEVELS = re.compile(r"(gpt-oss|qwen3(?![-.]?\d)|qwen3\.5|qwen3\.8|deepseek-v3\.2|deepseek-v4|kimi-k2|glm-4\.6|nemotron|swarm)", re.I)
_FORCED = re.compile(r"(r1|thinking-2507|sushi|qwq|vibethinker|reason|think[-_]chat|stupidchat)", re.I)
_HYBRID = re.compile(r"(qwen3\.5-\d|qwen3[.-]\d|tongyi|my-finetune-conversational)", re.I)
_NONE = re.compile(r"(llama-[234]|qwen2\.5|gemma-?3(?![-.]4)|gpt2|opt-|phi-3-mini|phi-4|ornith|dolphin|smollm|mistral-7b|warmchat|uncensored-?rp|qwythos|lfm2|matrix-mini|axon|prism|bonsai)", re.I)


def thinking_support(name: str) -> str | None:
    low = name.lower()
    if _FORCED.search(low):
        return "forced"
    if _LEVELS.search(low):
        return "levels"
    if _NONE.search(low):
        return "none"
    if _HYBRID.search(low):
        return "on-off"
    return None


def quant_bits(quant: str | None) -> float:
    """Approximate bits-per-weight for sorting (lower = smaller)."""
    if not quant:
        return 99.0
    q = quant.upper()
    if "IQ1" in q:
        return 1.2
    if "IQ2" in q:
        return 2.2
    if "IQ3" in q:
        return 3.3
    if "IQ4" in q:
        return 4.3
    if q.startswith("TQ") or "TPRUNED" in q:
        return 5.0
    m = re.search(r"Q(\d)", q)
    base = float(m.group(1)) if m else 8.0
    if "Q8_0" in q or q == "Q8":
        return 8.0
    if q in ("BF16", "F16", "F32"):
        return 16.0
    if "_S" in q:
        base -= 0.3
    if "_M" in q:
        base += 0.1
    if "_L" in q or "XL" in q:
        base += 0.4
    if q.startswith("MXFP4"):
        return 4.3
    return base


def family(name: str) -> str:
    low = name.lower().lstrip("0123456789-/_. ")
    parts = re.split(r"[/\s_-]+", low)
    return (parts[0] if parts else low)[:12]
