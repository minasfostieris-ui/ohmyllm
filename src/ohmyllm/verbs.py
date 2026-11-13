"""Rotating spinner verbs for the TUI busy/thinking indicators.

Time-driven on purpose: the TUI status bar (1 s refresh tick) calls
:func:`verb` and gets a deterministic answer for the current wall-clock
window, so no extra timers or state are needed. The GUI ticker keeps its
own independent verb list and cadence in app.js — the two are not
synchronized and do not claim to be.
"""

from __future__ import annotations

import random
import time

VERBS: tuple[str, ...] = (
    'pondering', 'ruminating', 'contemplating',
    'deliberating', 'thinking hard', 'finding x',
    'divining', 'untangling', 'synapsing',
    'connecting dots', 'reading tea leaves', 'consulting the oracle',
    'vibing', 'crunching', 'warming up',
    'spinning up', 'cracking knuckles', 'squinting',
    'deep-diving', 'following the white rabbit', 'asking the void',
    'feeding the hamster', 'oiling the gears', 'summoning context',
    'unfolding', 'digesting', 'reflecting',
    'brooding', 'scheming', 'daydreaming',
    'muttering', 'pacing', 'counting to ten',
    'checking assumptions', 'double-checking', 'triple-checking',
    'brainstorming', 'whiteboarding', 'rubber-ducking',
    'reading the fine print', 'hitting the books', 'putting on glasses',
    'sharpening pencils', 'unrolling the map', 'stirring the pot',
    'steeping the tea', 'spinning the globe', 'flipping pages',
    'cross-referencing', 'triangulating', 'extrapolating',
    'hypothesizing', 'theorizing', 'overthinking',
    'second-guessing', 'third-guessing', 'panicking (productively)',
    'sweating', 'calculating', 'estimating',
    'gesturing vaguely', 'nodding slowly', 'stroking the beard',
    'pondering the orb', 'questioning reality', 'asking "but why?"',
    'asking "and then what?"', 'stacking context', 'tidying the desk',
    'brewing', 'marinating', 'buffering dramatically',
    'channeling the machine spirit', 'negotiating with the tokens',
)

# seconds each verb stays on screen before the tick lands on the next one
ROTATE_S = 2.7


def _pick(window: int) -> str:
    # a per-window permutation so it never reads like a fixed cycle
    return VERBS[random.Random(window).randrange(len(VERBS))]


def verb(now: float | None = None) -> str:
    """The verb for the current time window; deterministic within a window."""
    t = (time.monotonic() if now is None else now) / ROTATE_S
    idx = int(t)
    v = _pick(idx)
    if v == _pick(idx - 1):  # never show the same verb twice in a row
        v = _pick(idx + 1)
    return v
