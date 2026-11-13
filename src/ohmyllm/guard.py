"""System-1 danger guard: a local decision model (Clef-Flash via llama.cpp
``/v1/systemone``) that flags dangerous tool calls in otherwise unrestricted
permission modes.

The guard is a pure HTTP client — the model runs wherever the user put it
(documented: ``llama-server -hf ggml-org/Clef-Flash-GGUF -ngl 0``, i.e. CPU +
system RAM only, never the GPU that the coding model needs). It answers one
noul question: "would executing this tool call be dangerous?" and returns a
calibrated probability.

Failure policy: ``assess`` never raises — an unreachable server yields an
unflagged verdict carrying ``error``. The runtime treats that error as
fail-closed: the call is escalated to a fresh approval instead of proceeding
unchecked, so a dead helper process can never silently widen permissions.
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
            return (f"guard offline ({self.error[:80]}) — fresh approval required "
                    "(fail-closed)")
        return (
            f"GUARD {self.prob:.0%} dangerous — {name} flagged for confirmation "
            "even in this permission mode"
        )


def needs_assessment(name: str, args: dict[str, Any]) -> bool:
    if name.startswith(_SAFE_PREFIXES):
        return False
    return True


# Substrings that mark risky material inside long payloads. build_state keeps
# head+tail AND excerpts centered on these tokens (chunked assessment) so a
# dangerous command buried in the middle of a huge argument is still scored.
_RISK_TOKENS = (
    "curl", "wget", "rm ", "ssh", "scp", ".ssh", "id_rsa", ".aws", ".gnupg",
    ".netrc", ".env", "mkfs", "dd ", "chmod", "chown", "sudo", "eval",
    "base64", "/etc/", "fsmonitor", "hooks", "python", "node", "perl",
    "ruby", "shutil", "rmtree", "subprocess", "os.system", "password",
    "token", "secret", "key", "http",
)


def _risk_excerpt(value: str) -> str:
    """Head+tail plus risk-centered excerpts, bounded to a fixed budget."""
    head, tail = value[:700], value[-100:]
    marker = f"\n…[{len(value)} chars total]\n"
    budget = 2400
    lowered = value.lower()
    spans: list[tuple[int, int]] = []
    for tok in _RISK_TOKENS:
        start = 0
        while len(spans) < 48:
            i = lowered.find(tok, start)
            if i < 0:
                break
            spans.append((max(0, i - 60), min(len(value), i + len(tok) + 140)))
            start = i + len(tok)
    spans.sort()
    merged: list[list[int]] = []
    for lo, hi in spans:
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    parts: list[str] = []
    used = len(head) + len(marker) + len(tail)
    for lo, hi in merged:
        if hi <= len(head) or lo >= len(value) - len(tail):
            continue  # already covered by head/tail
        piece = value[lo:hi]
        if used + len(piece) + 2 > budget:
            break
        parts.append(piece)
        used += len(piece) + 2
    middle = "\n…\n".join(parts)
    return head + marker + (middle + "\n…\n" if middle else "") + tail


def build_state(name: str, args: dict[str, Any]) -> str:
    """Compact JSON of the call for the decision model's STATE section."""
    slim: dict[str, Any] = {"tool": name}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 1400:
            slim[k] = _risk_excerpt(v)
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
        async with httpx.AsyncClient(
            timeout=cfg.timeout_s, trust_env=False, follow_redirects=False,
        ) as c:
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
