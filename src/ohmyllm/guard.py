"""System-1 danger guard: a local decision model (Clef-Flash via llama.cpp
``/v1/systemone``) that flags dangerous tool calls in otherwise unrestricted
permission modes.

The guard is a pure HTTP client — the model runs wherever the user put it
(documented: ``llama-server -hf ggml-org/Clef-Flash-GGUF -ngl 0``, i.e. CPU +
system RAM only, never the GPU that the coding model needs). It answers one
noul question: "would executing this tool call be dangerous?" and returns a
calibrated probability.

Failure policy: if the guard server is unreachable the call is treated as
NOT flagged (fail-open) but the error is surfaced in the reasons so the user
sees the guard was offline — dangerous mode must not brick itself over a
dead helper process.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .config import GuardSettings

# tools that only read can never need flagging; everything else is assessed
_SAFE_PREFIXES = ("read_", "list_", "search_", "glob", "grep", "web_search")


@dataclass
class GuardVerdict:
    flagged: bool
    prob: float | None = None
    error: str | None = None

    def reason(self, name: str) -> str:
        if self.error:
            return f"guard offline ({self.error[:80]}) — call proceeded unchecked"
        return (
            f"GUARD {self.prob:.0%} dangerous — {name} flagged for confirmation "
            "even in this permission mode"
        )


def needs_assessment(name: str, args: dict[str, Any]) -> bool:
    if name.startswith(_SAFE_PREFIXES):
        return False
    return True


def build_state(name: str, args: dict[str, Any]) -> str:
    """Compact JSON of the call for the decision model's STATE section."""
    slim: dict[str, Any] = {"tool": name}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 1400:
            slim[k] = v[:1200] + f"\n…[{len(v)} chars total]\n" + v[-100:]
        else:
            slim[k] = v
    return json.dumps(slim, ensure_ascii=False, default=str)


async def assess(cfg: GuardSettings, name: str, args: dict[str, Any]) -> GuardVerdict:
    """Ask the decision server. Never raises."""
    import httpx

    body = {
        "model": cfg.model,
        "state": build_state(name, args),
        "questions": {
            "dangerous": {"type": "noul", "instructions": cfg.question},
        },
    }
    try:
        async with httpx.AsyncClient(timeout=cfg.timeout_s) as c:
            resp = await c.post(cfg.base_url.rstrip("/") + "/v1/systemone", json=body)
            if resp.status_code != 200:
                return GuardVerdict(False, error=f"HTTP {resp.status_code}: {resp.text[:120]}")
            data = resp.json()
        answers = data.get("answers") or {}
        ans = answers.get("dangerous") or {}
        if "noul" not in ans:
            return GuardVerdict(False, error="no noul answer in response")
        prob = float(ans["noul"])
        return GuardVerdict(prob >= cfg.danger_threshold, prob=prob)
    except Exception as exc:  # noqa: BLE001 — guard must never break the turn
        return GuardVerdict(False, error=f"{type(exc).__name__}: {exc}")
