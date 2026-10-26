"""Streaming splitter for inline reasoning tags.

Some servers hand reasoning back in `delta.reasoning` / `delta.reasoning_content`
(vLLM, LM Studio with a parser, Ollama `thinking`), but plenty of deployments —
raw llama.cpp, vLLM without --reasoning-parser, OpenAI-compatible proxies,
Ollama --think keep — stream the thinking INSIDE `delta.content`, wrapped in
model-family tags: qwen3/qwq/deepseek-r1-distill use think pairs, kimi/GLM-Z1
use full-width variants, some templates use thinking/reasoning/thought pairs.

This module turns that soup into a clean (reasoning, content) stream so the
UI, the parser, and the stored session never see tags or leaked thoughts.
It is deliberately model-name-agnostic: any of these tags appearing anywhere
starts a reasoning block, which keeps the big models (120B+) supported by
string handling we can actually test on a laptop.
"""
from __future__ import annotations

from collections.abc import Iterable

# open tag -> matching close tag
_PAIRS: dict[str, str] = {
    '<think>': '</think>',
    '＜think＞': '＜/think＞',
    '<thinking>': '</thinking>',
    '<reasoning>': '</reasoning>',
    '<thought>': '</thought>',
}
# closers that may appear without an opener (models that forget to open, or
# servers that split the stream mid-block); also stripped when stray outside
_EXTRA_CLOSERS = tuple(dict.fromkeys(list(_PAIRS.values()) + ['</think>', '＜/think＞', '</thinking>', '</reasoning>', '</thought>']))
_ALL_OPENERS = tuple(_PAIRS)
_ALL_CLOSERS = tuple(dict.fromkeys(list(_PAIRS.values()) + list(_EXTRA_CLOSERS)))


def _prefix_overlap(buf: str, tags: Iterable[str]) -> int:
    """Length of the longest buffer suffix that is a strict prefix of any tag."""
    best = 0
    for tag in tags:
        top = min(len(buf), len(tag) - 1)
        for n in range(top, best, -1):
            if buf.endswith(tag[:n]):
                best = n
                break
    return best


class ThinkSplitter:
    """Feed raw content deltas; get (kind, text) tuples, kind in
    {"reasoning", "content"}. Holdback is minimal: only when a chunk ends
    mid-tag does a byte wait for the next delta."""

    def __init__(self) -> None:
        self._buf = ""
        self._close: str | None = None  # active block's closer, None = outside

    def feed(self, delta: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        if not delta:
            return out
        self._buf += delta
        while True:
            if self._close is None:
                open_at, open_tag = self._find(self._buf, _ALL_OPENERS)
                close_at, close_tag = self._find(self._buf, _ALL_CLOSERS)
                if close_at >= 0 and (open_at < 0 or close_at < open_at):
                    # stray closer with no open block: strip it, keep streaming
                    stray = self._buf[:close_at]
                    self._buf = self._buf[close_at + len(close_tag):]
                    if stray:
                        out.append(("content", stray))
                    continue
                if open_at < 0:
                    hold = _prefix_overlap(self._buf, _ALL_OPENERS)
                    text = self._buf[: len(self._buf) - hold]
                    self._buf = self._buf[len(self._buf) - hold:]
                    if text:
                        out.append(("content", text))
                    return out
                stray = self._buf[:open_at]
                self._buf = self._buf[open_at + len(open_tag):]
                if stray:
                    out.append(("content", stray))
                self._close = _PAIRS[open_tag]
            else:
                close_at, close_tag = self._find(self._buf, (self._close, *_EXTRA_CLOSERS))
                if close_at < 0:
                    hold = _prefix_overlap(self._buf, (self._close, *_ALL_CLOSERS))
                    text = self._buf[: len(self._buf) - hold]
                    self._buf = self._buf[len(self._buf) - hold:]
                    if text:
                        out.append(("reasoning", text))
                    return out
                text = self._buf[:close_at]
                self._buf = self._buf[close_at + len(close_tag):]
                if text:
                    out.append(("reasoning", text))
                self._close = None

    def flush(self) -> list[tuple[str, str]]:
        """End of stream: whatever is buffered goes out in its current mode."""
        out: list[tuple[str, str]] = []
        text, self._buf = self._buf, ""
        if text:
            out.append(("reasoning" if self._close else "content", text))
        self._close = None
        return out

    @staticmethod
    def _find(buf: str, tags: tuple[str, ...]) -> tuple[int, str]:
        best = (-1, "")
        for tag in tags:
            i = buf.find(tag)
            if i >= 0 and (best[0] < 0 or i < best[0]):
                best = (i, tag)
        return best
