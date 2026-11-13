"""Task #14 integrated-review regressions that span no single existing module.

F9: the /yolo help text must match the actual shell-consent semantics —
yolo auto-approves routine tool calls only; shell/network/mail still ask,
and catastrophic commands stay blocked in every mode.
"""

from __future__ import annotations

from ohmyllm.tui.screens.chat import ChatScreen


def test_yolo_help_matches_shell_consent_semantics():
    entry = next(e for e in ChatScreen.SLASH if e[0] == "/yolo")
    help_text = entry[2]
    assert "auto-approves routine tool calls" in help_text
    assert "shell/network/mail still ask" in help_text
    assert "catastrophic commands stay blocked" in help_text
    assert "skips approvals" not in help_text  # the old, misleading wording
