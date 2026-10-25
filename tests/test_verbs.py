"""The busy/thinking spinners must actually rotate, never repeat a fixed cycle."""

from __future__ import annotations

from ohmyllm.verbs import VERBS, verb


def test_verb_is_deterministic_within_a_window():
    t = 1234.5
    assert verb(t) == verb(t + 0.5) == verb(t + 1.5)


def test_verb_rotates_across_windows_without_a_fixed_cycle():
    seq = [verb(t) for t in range(0, 3000, 3)]
    repeats = sum(1 for a, b in zip(seq, seq[1:], strict=False) if a == b)
    assert repeats <= len(seq) // 50, f"{repeats} adjacent repeats — rotation looks frozen"


def test_verb_always_from_the_list():
    assert all(verb(t * 0.37) in VERBS for t in range(500))
    assert len(VERBS) >= 60, "the whole point is variety"
