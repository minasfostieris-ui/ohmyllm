"""Command deobfuscation helpers for the ``safety.deny`` floor (D3).

The deny floor must see through the cheap tricks models (and attackers) use
to sneak a forbidden command past a glob list: quoting, backslash escapes,
``${IFS}`` whitespace, ``&&``/``||``/``;``/pipe/single-``&`` (background)
chaining, one level of ``$()``, backtick or bare ``(...)`` subshell interiors,
and ``sh -c`` / ``bash -c`` / ``eval`` / ``env`` / ``xargs`` wrappers.

This module is deliberately *over*-inclusive: it produces every plausible
normalized variant of a command so the policy engine can fnmatch each one.
It never executes anything and has no side effects.

Additional deobfuscation layers (R1 security review):
  * ANSI-C ``$'...'`` quoting is decoded (``$'\t'`` etc.) BEFORE generic
    quote removal, so escaped payloads stay visible (H1);
  * ``${VAR}`` is folded to ``$VAR`` and literal ``VAR=value`` assignments
    are expanded into ``$VAR`` references with a bounded, non-executing
    substitution (C1/C2) — unknown variables are left untouched;
  * long-form destructive flags (``rm --recursive --force``) are folded to
    their short equivalents (``rm -rf``) so spelling cannot dodge gates (C1).
"""

from __future__ import annotations

import re

# Single & matters: `echo ok & rm -rf X` backgrounds the first command and
# still runs the second — the floor must see both segments.
_SPLIT_RX = re.compile(r"&&|\|\||[;|&\n]")
_SUBSHELL_RX = re.compile(r"\$\(([^()]*)\)|`([^`]*)`|\(([^()]*)\)")
# Collapse whitespace EXCEPT newlines: newline-chained segments must survive
# normalization so the deny floor can split and inspect them.
_WS_RX = re.compile(r"[^\S\n]+")
_ALL_WS_RX = re.compile(r"\s+")
_IFS_RX = re.compile(r"\$\{IFS\}|\$IFS")
_ESCAPE_RX = re.compile(r"\\(.)", re.DOTALL)
_SHELL_WRAPPERS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_BRACE_VAR_RX = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ANSI_C_RX = re.compile(r"\$'((?:[^'\\]|\\.)*)'")
_ANSI_ESCAPES = {
    "a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r",
    "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"',
    "e": "\x1b", "E": "\x1b", "0": "\x00",
}
_ASSIGN_RX = re.compile(r"(?:^|[\s;|&])([A-Za-z_][A-Za-z0-9_]*)=([^\s;|&]*)")
_VAR_REF_RX = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
# Quote splicing: '"' / "'" adjacency concatenates shell strings. The splice
# quotes are pure syntax — dropping them exposes hidden $'...' payloads (H1).


def _decode_ansi_c(text: str) -> str:
    """Decode ANSI-C ``$'...'`` quoting (H1). Runs before generic quote
    removal so ``$'\\t'`` becomes a real tab instead of a literal ``t``.
    Handles ``\\xHH``, ``\\uHHHH``, ``\\UHHHHHHHH`` and octal ``\\NNN`` too,
    so ``$'\\x2f'`` cannot hide a ``/`` from the catastrophic gate."""

    def _sub(m: re.Match[str]) -> str:
        body = m.group(1)
        out: list[str] = []
        i = 0
        while i < len(body):
            ch = body[i]
            if ch == "\\" and i + 1 < len(body):
                nxt = body[i + 1]
                if nxt == "x":
                    j = i + 2
                    while j < len(body) and j < i + 4 and body[j] in "0123456789abcdefABCDEF":
                        j += 1
                    if j > i + 2:
                        out.append(chr(int(body[i + 2:j], 16)))
                        i = j
                        continue
                elif nxt in "uU":
                    width = 4 if nxt == "u" else 8
                    digits = body[i + 2:i + 2 + width]
                    if len(digits) == width and all(c in "0123456789abcdefABCDEF" for c in digits):
                        out.append(chr(int(digits, 16)))
                        i += 2 + width
                        continue
                elif nxt in "01234567":
                    j = i + 1
                    while j < len(body) and j < i + 4 and body[j] in "01234567":
                        j += 1
                    out.append(chr(int(body[i + 1:j], 8)))
                    i = j
                    continue
                out.append(_ANSI_ESCAPES.get(nxt, nxt))
                i += 2
            else:
                out.append(ch)
                i += 1
        return "".join(out)

    return _ANSI_C_RX.sub(_sub, text)


def _strip_tricks(text: str) -> str:
    """Remove quoting/escape/whitespace obfuscation, collapse whitespace."""
    out = _IFS_RX.sub(" ", text)
    # Unsplice '"' / "'" adjacencies (fixpoint) BEFORE decoding: splicing
    # hides ANSI-C payloads from the decoder, and a premature decode would
    # consume the splice quotes as body (touch$'"'\t'"'/tmp → touch$'\t'/tmp).
    while True:
        nxt = out.replace("'\"", "").replace("\"'", "")
        if nxt == out:
            break
        out = nxt
    out = _decode_ansi_c(out)
    out = _BRACE_VAR_RX.sub(r"$\1", out)
    out = _ESCAPE_RX.sub(r"\1", out)
    out = out.replace("'", "").replace('"', "")
    # Tabs/VT/FF surviving quote removal (e.g. a literal tab inside "...")
    # are argument separators too — fold them so `touch\t/tmp` cannot span
    # a whitespace-sensitive deny glob (H1). Newlines stay: they chain.
    out = re.sub(r"[^\S\n]+", " ", out)
    return _WS_RX.sub(" ", out).strip()


def _expand_vars(text: str) -> str:
    """Bounded, NON-executing expansion of literal ``VAR=value`` assignments
    into ``$VAR``/``${VAR}`` references (C2). First assignment wins (shell
    semantics for sequential assignment), at most 3 substitution passes so
    chained indirection terminates. Unassigned references are left as-is.
    """
    values: dict[str, str] = {}
    for m in _ASSIGN_RX.finditer(text):
        name, val = m.group(1), m.group(2)
        if name not in values:
            values[name] = val.strip("'\"")
    if not values:
        return text

    def _sub(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2)
        return values.get(name, m.group(0))

    out = text
    for _ in range(3):
        nxt = _VAR_REF_RX.sub(_sub, out)
        if nxt == out:
            break
        out = nxt
    return out


_RM_LONG_FLAGS = {
    "--recursive": "r", "--force": "f", "--dir": "d", "--verbose": "v",
    "--interactive": "i", "--one-file-system": "x", "--no-preserve-root": "",
}


def _semantic_flags(seg: str) -> str:
    """Fold long-form ``rm`` flags into their short equivalents (C1) so
    ``rm --recursive --force /`` normalizes to ``rm -rf /``. Only flag
    spelling changes; arguments and their order are preserved."""
    tokens = seg.split()
    if not tokens:
        return seg
    exe = tokens[0].rsplit("/", 1)[-1]
    if exe != "rm":
        return seg
    out: list[str] = [tokens[0]]
    shorts: list[str] = []

    def flush() -> None:
        if shorts:
            out.append("-" + "".join(dict.fromkeys(shorts)))
            shorts.clear()

    for tok in tokens[1:]:
        if tok in _RM_LONG_FLAGS:
            folded = _RM_LONG_FLAGS[tok]
            if folded:
                shorts.append(folded)
            else:  # --no-preserve-root keeps its own token (gate matches it)
                flush()
                out.append(tok)
        elif tok.startswith("-") and tok != "-":
            flush()
            out.append(tok)
        else:
            flush()
            out.append(tok)
    flush()
    return " ".join(out)


def _compact(text: str) -> str:
    return _ALL_WS_RX.sub("", text)


def _raw_remainder(seg: str) -> str:
    """Segment text after the first token, preserving internal whitespace."""
    m = re.match(r"\S+\s+", seg)
    return seg[m.end():] if m else ""


# Wrapper flags that consume a SEPARATED argument (``xargs -n 1``, ``-I {}``,
# ``-E eof``, ``env -u FOO``, ``--unset FOO``). Optional-argument flags (xargs
# -i/-e/-l, --replace/--eof) are deliberately excluded: getopt accepts their
# value only attached, so the following token is the real payload command and
# must stay visible to the deny floor (consuming it would re-open the bypass).
# NB: -E REQUIRES an argument (unlike the optional -e); -l is the optional
# deprecated synonym of --max-lines, while -L requires one.
_XARGS_REQ_SHORT = frozenset("adEILnPs")
_XARGS_OPT_SHORT = frozenset("iel")
_XARGS_REQ_LONG = frozenset({
    "--arg-file", "--delimiter", "--max-args", "--max-lines",
    "--max-chars", "--max-procs", "--process-slot-var",
})
_ENV_REQ_SHORT = frozenset("uC")
_ENV_REQ_LONG = frozenset({"--unset", "--chdir"})


def _drop_token(text: str) -> str:
    """Remove the first whitespace-delimited token and its trailing space."""
    m = re.match(r"\S+\s*", text)
    return text[m.end():] if m else ""


def _skip_wrapper_flags(
    payload: str,
    *,
    req_short: frozenset[str],
    opt_short: frozenset[str] = frozenset(),
    req_long: frozenset[str] = frozenset(),
    assignments: bool = False,
) -> str:
    """Consume a wrapper's own leading flags so *payload* starts at the real
    command. Required-argument flags also consume their SEPARATED argument;
    optional-argument flags never do (their value is only accepted attached),
    so benign payloads are preserved rather than over-stripped. ``VAR=val``
    assignments are consumed when *assignments* is set (env)."""
    while payload:
        m = re.match(r"(\S+)(\s+|$)", payload)
        if not m:
            break
        tok = m.group(1)
        rest = payload[m.end():]
        if assignments and re.match(r"[A-Za-z_][A-Za-z0-9_]*=", tok):
            payload = rest
            continue
        if not tok.startswith("-") or tok == "-":
            break  # first non-flag token: the payload command starts here
        if tok.startswith("--"):
            name, eq, _val = tok.partition("=")
            # attached (--flag=v), optional and no-arg longs consume only the
            # token; required-arg longs also drop their separated argument.
            payload = _drop_token(rest) if (not eq and name in req_long) else rest
            continue
        take_next = False
        chars = tok[1:]
        for idx, ch in enumerate(chars):
            if ch in req_short:
                take_next = idx == len(chars) - 1  # no attached value follows
                break
            if ch in opt_short:
                break  # optional arg: attached only, never a separated token
        payload = _drop_token(rest) if take_next else rest
    return payload


def command_variants(command: str) -> list[str]:
    """Return normalized variants of *command* for deny-glob matching.

    Includes the normalized whole, its whitespace-free form, every chained
    segment (including single-``&`` background chains), each segment's
    executable (full token and basename), one level of ``$()``/backtick/
    ``(...)`` interiors, and one level of ``sh -c``/``bash -c``/``eval``/
    ``env``/``xargs`` payloads. Order is stable and duplicates are removed.
    """
    seen: list[str] = []

    def add(value: str) -> None:
        value = value.strip()
        if value and value not in seen:
            seen.append(value)

    def walk(text: str, depth: int) -> None:
        norm = _strip_tricks(text)
        if not norm:
            return
        add(norm)
        add(_compact(norm))
        expanded = _expand_vars(norm)
        if expanded != norm:
            add(expanded)
            add(_compact(expanded))
            if depth < 1:
                # re-walk the expanded form (fixpoint: re-expansion is a
                # no-op, so this terminates after one extra level)
                walk(expanded, depth + 1)
        if depth < 1:
            for match in _SUBSHELL_RX.finditer(norm):
                inner = match.group(1) or match.group(2) or match.group(3) or ""
                if inner.strip():
                    walk(inner, depth + 1)
        segments = [s.strip() for s in _SPLIT_RX.split(norm) if s.strip()]
        for seg in segments:
            add(seg)
            add(_compact(seg))
            folded = _semantic_flags(seg)
            if folded != seg:
                add(folded)
                add(_compact(folded))
            tokens = seg.split()
            if not tokens:
                continue
            exe = tokens[0]
            add(exe)
            base = exe.rsplit("/", 1)[-1]
            add(base)
            # Two wrapper levels: `bash -c "bash -c 'rm -rf /'"` must expose
            # the innermost payload to anchored deny rules (bounded depth).
            if depth >= 2:
                continue
            if base in _SHELL_WRAPPERS and "-c" in tokens[1:3]:
                # take the RAW remainder so newlines inside the payload survive
                m = re.search(r"(?:^|\s)-c\s+", seg)
                payload = seg[m.end():] if m else ""
                if payload.strip():
                    walk(payload, depth + 1)
            elif base == "eval":
                payload = _raw_remainder(seg)
                if payload.strip():
                    walk(payload, depth + 1)
            elif base == "env":
                payload = _skip_wrapper_flags(
                    _raw_remainder(seg),
                    req_short=_ENV_REQ_SHORT, req_long=_ENV_REQ_LONG,
                    assignments=True,
                )
                if payload.strip():
                    walk(payload, depth + 1)
            elif base == "xargs":
                # Skip xargs' own flags (consuming separated arguments such as
                # `-n 1` / `-I {}`), then take the RAW remainder from the first
                # non-dash token so payload flags (e.g. -rf) survive.
                payload = _skip_wrapper_flags(
                    _raw_remainder(seg),
                    req_short=_XARGS_REQ_SHORT, opt_short=_XARGS_OPT_SHORT,
                    req_long=_XARGS_REQ_LONG,
                )
                if payload.strip():
                    walk(payload, depth + 1)

    walk(str(command), 0)
    return seen


def normalize_command(command: str) -> str:
    """Single canonical normalized form for risk scoring: deobfuscate,
    expand literal variable assignments, fold semantic flags. Unlike
    :func:`command_variants` this returns ONE string (no bare-executable
    variants) so scoring cannot be inflated by stray tokens."""
    return _semantic_flags(_expand_vars(_strip_tricks(str(command))))
