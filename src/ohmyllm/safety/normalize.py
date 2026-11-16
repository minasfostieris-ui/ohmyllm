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

import posixpath
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
# R7 S6: assignment-prefix keywords — `export X="rm -rf /"` leads the segment
# with the keyword, so the quote-aware extractor must skip it (and its flags)
# before reading assignments. EXTENDS the R6 E4 extractor (Terry R7 verdict:
# uncovered variant, not a regression — the pristine E4 baseline stays).
_ASSIGN_PREFIXES = frozenset({"export", "declare", "typeset", "local", "readonly"})
_VAR_REF_RX = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
# R2 C3: dynamic-target resolution — default-value braces, positional params,
# pure-literal command substitution, and xargs stdin synthesis.
_DEFAULT_RX = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-?([^{}]*)\}")
_POS_REF_RX = re.compile(r"\$\{([0-9]+|[@*])\}|\$([0-9]+|[@*])")
_SUBST_RX = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
_ASSIGN_TOK_RX = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
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
                elif nxt == "c" and i + 2 < len(body):
                    # R5 A15-A17: bash decodes \cX as the control character
                    # ord(X) & 0x1f — \cI is TAB, \cJ is LF, \c? is DEL.
                    # Leaving it as a literal "cX" fabricates phantom operands
                    # and hides separators from the catastrophic gate.
                    cx = body[i + 2]
                    out.append("\x7f" if cx == "?" else chr(ord(cx) & 0x1F))
                    i += 3
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
    # R5 A15-A17: decode \cX control escapes in ANY quoting context (the
    # conservative security view — a wrapper may re-parse the payload with
    # ANSI-C semantics). \c? is DEL, otherwise ord(X) & 0x1f.
    out = re.sub(
        r"\\c(.)",
        lambda m: "\x7f" if m.group(1) == "?" else chr(ord(m.group(1)) & 0x1F),
        out,
    )
    out = _ESCAPE_RX.sub(r"\1", out)
    out = out.replace("'", "").replace('"', "")
    # Tabs/VT/FF surviving quote removal (e.g. a literal tab inside "...")
    # are argument separators too — fold them so `touch\t/tmp` cannot span
    # a whitespace-sensitive deny glob (H1). Newlines stay: they chain.
    out = re.sub(r"[^\S\n]+", " ", out)
    return _WS_RX.sub(" ", out).strip()


def _assignment_values(text: str) -> dict[str, str]:
    """Literal ``VAR=value`` assignments, LAST wins (real shell semantics)."""
    values: dict[str, str] = {}
    for m in _ASSIGN_RX.finditer(text):
        values[m.group(1)] = m.group(2).strip("'\"")
    return values


def _assignment_values_raw(text: str, *, posix: bool = False) -> dict[str, str]:
    """Quote-aware ``VAR=value`` extraction on RAW text (R6 E4 family): a
    quoted multi-word value (``X="rm -rf /"``) resolves FULLY instead of
    truncating at the first space after quote removal. LAST wins. Only
    command-position assignments (segment start / after a separator) are
    read; _read_word honors single/double quotes and backslash escapes so the
    whole quoted value is ONE word. Never executes.

    R7 gate-fix (#119): ``posix=True`` is the PRODUCT-shell (/bin/sh)
    fail-closed interpretation — inside "..." a backslash before a non-special
    char stays LITERAL (POSIX content rule), and an odd backslash run before a
    quote whose preceding char is non-special is read as a string terminator
    so the hidden tail segments surface (Alex H1: echo "a\"; X="rm -rf /"; $X
    — the unclosed tail desyncs bash parity; stub-proven executable sibling).
    The bash-parity default is unchanged; callers union both interpretations."""
    values: dict[str, str] = {}
    for _sep, seg in _raw_segments(text, posix=posix):
        pos = 0
        # R7 S6: skip a leading export/declare/typeset/local/readonly word and
        # its flag tokens so the assignments that follow are still read with
        # their FULL quoted values.
        word, end, _closed = _read_word(seg, 0, posix=posix)
        if word in _ASSIGN_PREFIXES:
            pos = end
            while pos < len(seg):
                flag, fend, _fclosed = _read_word(seg, pos, posix=posix)
                if fend <= pos or not flag.startswith("-"):
                    break
                pos = fend
        while pos < len(seg):
            word, end, closed = _read_word(seg, pos, posix=posix)
            if end <= pos:
                break
            m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", word, re.DOTALL)
            if not m:
                break
            values[m.group(1)] = m.group(2)
            pos = end
            if not closed:
                break
    return values


def _merged_assignments(raw: str, norm: str) -> dict[str, str]:
    """Legacy normalized extraction UNION quote-aware raw extraction (R6 E4):
    the raw quote-aware value wins per key so multi-word values are not
    truncated, while the legacy pass still catches assignments exposed only
    after deobfuscation (e.g. ``export X=…``).

    R7 gate-fix (#119): the UNION also covers the POSIX (/bin/sh) double-quote
    interpretation — bash parity and POSIX disagree on backslash runs before
    a closing quote (Alex H1), and the product shell executes the POSIX read.
    POSIX values win per key; the bash-parity pass is kept intact (Terry R7
    verdict: extend, don't revert)."""
    values = _assignment_values(norm)
    values.update(_assignment_values_raw(raw))
    values.update(_assignment_values_raw(raw, posix=True))
    return values


def _sub_refs(text: str, values: dict[str, str]) -> str:
    """Bounded substitution of ``$VAR``/``${VAR}`` from *values* (3 passes so
    chained indirection terminates). Unassigned references are left as-is.
    R7 S3: a custom non-empty IFS splits UNQUOTED substituted values at
    runtime (``IFS=,; X=rm,-rf,/; $X`` runs ``rm -rf /``) — translate the
    assigned IFS characters to spaces inside substituted values so the
    downstream gates see the real words. Empty IFS means NO splitting."""
    if not values:
        return text
    ifs = values.get("IFS") or ""
    split_chars = [ch for ch in ifs if not ch.isspace()]

    def _sub(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2)
        val = values.get(name)
        if val is None:
            return m.group(0)
        for ch in split_chars:
            val = val.replace(ch, " ")
        return val

    out = text
    for _ in range(3):
        nxt = _VAR_REF_RX.sub(_sub, out)
        if nxt == out:
            break
        out = nxt
    return out


def _expand_vars(text: str) -> str:
    """Bounded, NON-executing expansion of literal ``VAR=value`` assignments
    into ``$VAR``/``${VAR}`` references (C2). LAST assignment wins — real
    shell semantics for sequential assignment (R2 C2: first-wins let
    ``X=echo; X=rm; $X -rf /`` masquerade as echo). At most 3 substitution
    passes so chained indirection terminates. Unassigned references are
    left as-is.
    """
    return _sub_refs(text, _assignment_values(text))


# R7 S1 (FAMILY): bash comma-list brace expansion. /bin/sh -> bash KEEPS brace
# expansion (Alex R7 env fact), so `rm -rf /{home,usr}` and `{rm,-rf,/}` are
# real execution paths, not theoretical spellings.
_BRACE_MAX_WORDS = 64
_BRACE_MAX_PASSES = 64


def _expand_braces(text: str) -> str | None:
    """Bounded, NON-executing bash brace-list expansion (R7 S1 family).
    Expansion is WORD-LOCAL (bash distributes prefix/suffix within ONE word:
    ``rm -rf /{a,b}`` -> ``rm -rf /a /b``, never a duplicated verb). Innermost
    lists first; ``${VAR}`` parameter braces ($-prefixed) and comma-less
    braces (``{}``, ``{x}``) stay literal. Returns None when a word is
    UNRESOLVABLE within budget — ``{a..b}`` range syntax, word explosion
    (>64 words) or pass exhaustion — so destructive verbs can fail closed.
    Over-inclusive even inside quotes (module philosophy): harmless for
    non-destructive commands, which never fail closed on braces."""
    if "{" not in text:
        return text
    words: list[str] = []
    for word in text.split():
        if "{" not in word:
            words.append(word)
            continue
        out = word
        for _ in range(_BRACE_MAX_PASSES):
            pos = 0
            expanded = False
            while not expanded:
                close = out.find("}", pos)
                if close < 0:
                    break
                open_ = out.rfind("{", 0, close)
                if open_ < 0 or (open_ > 0 and out[open_ - 1] == "$"):
                    pos = close + 1
                    continue
                body = out[open_ + 1:close]
                if "," not in body:
                    if ".." in body:
                        return None  # {a..b} range: not statically expanded
                    pos = close + 1
                    continue
                prefix, suffix = out[:open_], out[close + 1:]
                out = " ".join(prefix + part + suffix for part in body.split(","))
                if len(out.split()) > _BRACE_MAX_WORDS:
                    return None
                expanded = True
            if not expanded:
                break
        else:
            return None  # pass budget exhausted: unresolvable
        words.append(out)
    return " ".join(words)


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


def _unquote_outer(tok: str) -> str:
    """Strip ONE layer of matching outer quotes from a wrapper payload token
    (R7 gate-fix #119): ``sh -c 'echo "a\"; X="rm -rf /"; $X'`` hands the
    recursion a single-quoted blob whose interior quoting the assignment
    extractor needs. Unquoted tokens pass through unchanged."""
    if len(tok) >= 2 and tok[0] in "'\"" and tok[-1] == tok[0]:
        return tok[1:-1]
    return tok


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
# R6 E3: env -S/--split-string takes a COMMAND STRING (split into words and
# executed) — its value is a nested payload, not an ordinary flag argument.
_ENV_REQ_SHORT = frozenset("uCS")
_ENV_REQ_LONG = frozenset({"--unset", "--chdir", "--split-string"})
_ENV_NESTED_SHORT = frozenset("S")
_ENV_NESTED_LONG = frozenset({"--split-string"})
# sudo/doas are transparent prefixes: the real executable follows their own
# flags (R2 W5 — `sudo rm -rf $HOME` must still position-match as rm).
_PRIV_WRAPPERS = frozenset({"sudo", "doas"})
_PRIV_REQ_SHORT = frozenset("ugChpRTU")
_PRIV_REQ_LONG = frozenset({
    "--user", "--group", "--host", "--prompt", "--close-from",
    "--other-user", "--role", "--type",
})
# R5 A04-A13/B8: transparent exec prefixes — they run the following command
# unchanged, so the deny floor, catastrophic gate and scorer must see through
# them exactly like sudo/doas. busybox/toybox multiplex an applet name; the
# rest are pure exec wrappers with their own (quote-aware) flag grammar.
_TRANSPARENT_WRAPPERS = frozenset({
    "busybox", "toybox", "exec", "command", "nice", "nohup", "time",
    "timeout", "stdbuf", "ionice",
})
_TRANSPARENT_REQ_SHORT: dict[str, frozenset[str]] = {
    "exec": frozenset("a"),  # R6 E2: exec -a NAME sets argv[0], then runs NAME's command
    "nice": frozenset("n"),
    "timeout": frozenset("ks"),
    "stdbuf": frozenset("ioe"),
    "ionice": frozenset("cn"),
}
_TRANSPARENT_REQ_LONG: dict[str, frozenset[str]] = {
    "nice": frozenset({"--adjustment"}),
    "timeout": frozenset({"--signal", "--kill-after"}),
    "stdbuf": frozenset({"--input", "--output", "--error"}),
    "ionice": frozenset({"--class", "--classdata"}),
}
_TIMEOUT_DURATION_RX = re.compile(r"[0-9]+(?:\.[0-9]+)?[smhd]?")
# R5 A18/A19: wrapper-nesting budget. The variant walk exposes two wrapper
# levels and the scorer recurses three; anything deeper is unanalyzable and
# must fail closed instead of silent-allowing the hidden innermost payload.
_NESTING_BUDGET = 3


def _drop_token(text: str) -> str:
    """Remove the first whitespace-delimited token and its trailing space."""
    m = re.match(r"\S+\s*", text)
    return text[m.end():] if m else ""


def _read_word(text: str, start: int = 0, *, posix: bool = False) -> tuple[str, int, bool]:
    """One shell word from *start* (R4-S1): single quotes are literal, double
    quotes and backslash escapes are honored, so a quoted multi-word flag
    argument (``sudo -p 'x y'``) is consumed as ONE token instead of desyncing
    the wrapper-skip. Returns (word, end_offset, closed); an unclosed quote
    yields the remainder with closed=False so callers can fail closed.

    R7 gate-fix (#119): ``posix=True`` applies the PRODUCT-shell (/bin/sh)
    fail-closed reading — inside "..." a backslash escapes ONLY $ ` " \\ newline
    (POSIX content rule); before any other char it stays LITERAL. Quote
    closure additionally treats an odd backslash run preceded by a non-special
    char as a terminator (conservative: the unclosed-tail family Alex H1
    exploited desyncs bash parity). The default bash-parity path is unchanged."""
    buf: list[str] = []
    quote: str | None = None
    escaped = False
    i = start
    while i < len(text):
        ch = text[i]
        if escaped:
            buf.append(ch)
            escaped = False
            i += 1
            continue
        if quote == "'":
            if ch == "'":
                quote = None
            else:
                buf.append(ch)
            i += 1
            continue
        if ch == "\\":
            if quote == '"' and posix and (
                    i + 1 >= len(text) or text[i + 1] not in "$`\"\\\n"):
                # POSIX: backslash before a non-special char is literal.
                buf.append(ch)
                i += 1
                continue
            if i + 1 < len(text):
                buf.append(text[i + 1])
                i += 2
            else:
                escaped = True
                i += 1
            continue
        if quote == '"':
            if ch == '"':
                quote = None
            else:
                buf.append(ch)
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            i += 1
            continue
        if ch.isspace():
            break
        buf.append(ch)
        i += 1
    while i < len(text) and text[i].isspace():
        i += 1
    return "".join(buf), i, quote is None and not escaped


def _cluster_flag_value(tok: str, flag: str, arg_takers: str) -> tuple[bool, str]:
    """Parse a short-flag cluster for *flag* (R6 E5/E6/E7/E8 family): returns
    (present, attached_value). Scans left-to-right and STOPS at any char in
    *arg_takers* — that char consumes the rest as its attached value, so a
    later *flag* char is part of the value, not a flag (``sed -e's/i/x/'`` is
    NOT in-place; ``cp -St`` is suffix "t", not target). *flag* itself may
    take an attached value (everything after it). Long tokens (``--…``) and
    bare ``-`` return (False, ""). ONE parser shared by every clustered-flag
    gate so the grammars cannot desync."""
    if not tok.startswith("-") or tok.startswith("--") or tok == "-":
        return False, ""
    for idx, ch in enumerate(tok[1:], start=1):
        if ch == flag:
            return True, tok[idx + 1:]
        if ch in arg_takers:
            return False, ""
    return False, ""


def _quotes_balanced(text: str) -> bool:
    """shlex-style quote-state scan (R4-S1): False when a quote or trailing
    escape is left unclosed — wrapper-flag boundaries are then unknowable
    and callers must fail closed rather than guess."""
    quote: str | None = None
    escaped = False
    for ch in text:
        if escaped:
            escaped = False
            continue
        if quote == "'":
            if ch == "'":
                quote = None
            continue
        if ch == "\\":
            escaped = True
            continue
        if quote == '"':
            if ch == '"':
                quote = None
            continue
        if ch in "'\"":
            quote = ch
    return quote is None and not escaped


def _skip_wrapper_flags(
    payload: str,
    *,
    req_short: frozenset[str],
    opt_short: frozenset[str] = frozenset(),
    req_long: frozenset[str] = frozenset(),
    assignments: bool = False,
    nested_short: frozenset[str] = frozenset(),
    nested_long: frozenset[str] = frozenset(),
) -> str:
    """Consume a wrapper's own leading flags so *payload* starts at the real
    command. Required-argument flags also consume their SEPARATED argument;
    optional-argument flags never do (their value is only accepted attached),
    so benign payloads are preserved rather than over-stripped. ``VAR=val``
    assignments are consumed when *assignments* is set (env).
    R4-S1: token reading is quote-aware — ``sudo -p 'x y' rm …`` consumes the
    whole quoted prompt, keeping the real payload executable visible to the
    catastrophic gate, the deny floor and the scorer."""
    while payload:
        tok, end, closed = _read_word(payload)
        if not tok and end == 0:
            break
        rest = payload[end:]
        if assignments and re.match(r"[A-Za-z_][A-Za-z0-9_]*=", tok):
            payload = rest
            continue
        if not tok.startswith("-") or tok == "-":
            break  # first non-flag token: the payload command starts here
        if tok.startswith("--"):
            name, eq, _val = tok.partition("=")
            # R6 E3: a nested-command long flag (--split-string=) yields its
            # value AS the payload (a command string to walk), plus any
            # trailing words — never swallow it as an ordinary flag arg.
            if name in nested_long:
                if eq:
                    return (_val + " " + rest).strip()
                _arg, arg_end, _closed = _read_word(rest)
                return (_arg + " " + rest[arg_end:]).strip()
            # attached (--flag=v), optional and no-arg longs consume only the
            # token; required-arg longs also drop their separated argument.
            if not eq and name in req_long:
                _arg, arg_end, _closed = _read_word(rest)
                payload = rest[arg_end:]
            else:
                payload = rest
            continue
        take_next = False
        chars = tok[1:]
        for idx, ch in enumerate(chars):
            if ch in nested_short:
                # R6 E3: attached -SVALUE (or separated -S VALUE) is a nested
                # command string. Quote removal may have fabricated this
                # cluster (-S'rm -rf /' -> -Srm -rf /); the attached value is
                # everything after S, and the rest follows as further words.
                attached = chars[idx + 1:]
                if attached:
                    return (attached + " " + rest).strip()
                _arg, arg_end, _closed = _read_word(rest)
                return (_arg + " " + rest[arg_end:]).strip()
            if ch in req_short:
                take_next = idx == len(chars) - 1  # no attached value follows
                break
            if ch in opt_short:
                break  # optional arg: attached only, never a separated token
        if take_next:
            _arg, arg_end, _closed = _read_word(rest)
            payload = rest[arg_end:]
        else:
            payload = rest
        if not closed:
            # Unclosed flag token: boundaries are unknowable — stop rather
            # than fabricate a payload (callers fail closed on the quote
            # imbalance itself).
            break
    return payload


def _skip_env_flags(payload: str) -> str:
    """Consume env's own flags (R6 E3 family): ONE grammar shared by every
    gate (variants, nesting, unresolved scan, catastrophic gate, scorer) so
    the -S/--split-string nested command is always exposed. env assignments
    (VAR=val) are consumed; -S's value is returned as the payload (a nested
    command string) rather than swallowed."""
    return _skip_wrapper_flags(
        payload, req_short=_ENV_REQ_SHORT, req_long=_ENV_REQ_LONG,
        assignments=True, nested_short=_ENV_NESTED_SHORT,
        nested_long=_ENV_NESTED_LONG)


def _strip_transparent_prefixes(text: str) -> str:
    """Consume transparent exec prefixes (R5 A04-A13) plus sudo/doas/env so
    the real payload command is exposed: ``busybox rm -rf /``,
    ``timeout 5 rm …``, ``stdbuf -oL rm …``, ``sudo busybox rm …``. Flag
    consumption is quote-aware (_read_word); ``timeout`` also consumes its
    positional DURATION. Assignments are consumed for env. Never executes."""
    payload = text
    for _ in range(16):
        tok, end, _closed = _read_word(payload)
        if not tok:
            break
        base = tok.rsplit("/", 1)[-1].lower()
        rest = payload[end:]
        if base in _PRIV_WRAPPERS:
            payload = _skip_wrapper_flags(
                rest, req_short=_PRIV_REQ_SHORT, req_long=_PRIV_REQ_LONG)
        elif base == "env":
            payload = _skip_env_flags(rest)
        elif base in _TRANSPARENT_WRAPPERS:
            payload = _skip_wrapper_flags(
                rest,
                req_short=_TRANSPARENT_REQ_SHORT.get(base, frozenset()),
                req_long=_TRANSPARENT_REQ_LONG.get(base, frozenset()),
            )
            if base == "timeout":
                dur, dend, _dc = _read_word(payload)
                if dur and _TIMEOUT_DURATION_RX.fullmatch(dur):
                    payload = payload[dend:]
        else:
            break
    return payload


def nesting_exceeds_budget(command: str) -> bool:
    """True when wrapper nesting (sh -c / eval / env / xargs / sudo /
    transparent prefixes) exceeds the analysis budget (R5 A18/A19). The
    bounded variant walk and scorer cannot see the innermost payload beyond
    that depth, so callers must fail closed rather than silent-allow."""

    def walk(text: str, depth: int) -> bool:
        if depth > _NESTING_BUDGET:
            return True
        norm = _strip_tricks(text)
        for seg in _SPLIT_RX.split(norm):
            seg = seg.strip()
            if not seg:
                continue
            payload = _strip_transparent_prefixes(seg)
            if payload != seg:
                if walk(payload, depth + 1):
                    return True
                continue
            tokens = seg.split()
            i = 0
            while i < len(tokens) and _ASSIGN_TOK_RX.match(tokens[i]):
                i += 1
            if i >= len(tokens):
                continue
            base = tokens[i].rsplit("/", 1)[-1]
            rest = tokens[i + 1:]
            if base in _SHELL_WRAPPERS and "-c" in rest[:2]:
                m = re.search(r"(?:^|\s)-c\s+", seg)
                if m and walk(seg[m.end():], depth + 1):
                    return True
            elif base == "eval":
                if walk(" ".join(rest), depth + 1):
                    return True
            elif base == "env":
                p = _skip_env_flags(" ".join(rest))
                if p.strip() and walk(p, depth + 1):
                    return True
            elif base == "xargs":
                p = _skip_wrapper_flags(
                    " ".join(rest), req_short=_XARGS_REQ_SHORT,
                    opt_short=_XARGS_OPT_SHORT, req_long=_XARGS_REQ_LONG)
                if p.strip() and walk(p, depth + 1):
                    return True
        return False

    return walk(str(command), 0)


# R6 E1 (FAMILY): ONE root-operand grammar shared by the structural order-free
# check (_rm_root_order_free), the catastrophic gate (policy._CATASTROPHIC_RULES)
# and the risk rules (risk._RULES). Dot components are consumed in EVERY
# position and duplicate slashes tolerated: /home/. == /home, /etc/./ == /etc,
# /./home == /home, ~/. == ~, $HOME/. == $HOME, //home/. == /home. A SUBPATH
# (/home/user, /home/user/.) is NOT root-equivalent — the trailing dot group
# only consumes "." / ".." components, never a real name.
_ROOT_BARE_RX = r"/+(?:\.{1,2}/+)*\.{0,2}"
# R7 S4: EVERY top-level system directory is root-equivalent (deleting /bin,
# /lib, /root, /proc … bricks the OS). lib64 precedes lib for a clean match;
# the boundary lookahead keeps subpaths (/dev/null, /proc/self, /home/user)
# NON-root.
_ROOT_DIRS_RX = (r"/+(?:\.{1,2}/+)*(?:home|usr|etc|var|boot|opt|srv"
                 r"|bin|sbin|lib64|lib|root|sys|proc|dev|run)(?:/+\.{1,2})*/*")
# R7 S5: ~root folds to /root (root-equivalent via _ROOT_DIRS_RX). Other
# ~user homes are unresolvable -> fail closed for destructive verbs below.
_ROOT_HOME_RX = r"(?:~(?:root)?|\$HOME)(?:/+\.{1,2})*/*"
# R7 S2 (FAMILY): a pathname glob in the FIRST component of an absolute path
# can expand to any top-level dir (/hom* /?ome /[h]ome /us* -> /home /usr),
# with or without a trailing subpath (/[h]ome/. still denotes /home/.).
# The glob char cannot cross '/' — /tmp/* and /home/user/* (component 2+)
# and relative globs (*.bak) are NOT root-equivalent.
_ROOT_GLOB_RX = r"/+(?:\.{1,2}/+)*[^/\s'\"]*[*?\[][^/\s'\"]*(?:/[^\s'\"]*)?(?=[\s'\"*]|$)"
#: Shared operand body (no anchors) — policy/risk compose their rules from it.
ROOT_OPERAND_RX = rf"(?:{_ROOT_BARE_RX}|{_ROOT_DIRS_RX}|{_ROOT_HOME_RX}|{_ROOT_GLOB_RX})"
_RM_ROOT_OPERAND_RX = re.compile(rf"^{ROOT_OPERAND_RX}$")
#: Bare filesystem root only (/, /., /.., //, /./) — the sensitive-target scan
#: skips these for non-write verbs (echo "rm -rf /" stays inert).
BARE_ROOT_OPERAND_RX = re.compile(rf"^(?:{_ROOT_BARE_RX})$")
_ROOT_GLOB_OPERAND_RX = re.compile(rf"^(?:{_ROOT_GLOB_RX})$")
# R7 S5: a WHOLE ~user home (no real subpath) — the user's home directory is
# unknowable to static analysis, so destructive verbs fail closed on it.
_TILDE_USER_HOME_RX = re.compile(r"^~[A-Za-z0-9_.-]+(?:/+\.{1,2})*/*$")


def _unresolvable_operand(tok: str) -> bool:
    """R7 fail-closed family: True when a destructive verb's operand carries
    an expansion static resolution cannot prove safe — an unexpanded
    ``$…``/backtick (R2 C3), a first-component absolute glob (S2), a whole
    ~user home (S5), or an unresolvable brace list (S1). Non-destructive
    verbs never fail closed on these (over-block guard)."""
    if "$" in tok or "`" in tok:
        return True
    if _ROOT_GLOB_OPERAND_RX.match(tok) or _TILDE_USER_HOME_RX.match(tok):
        return True
    # R9 E3: ~user/…/.. traversals kernel-resolve against an unknowable HOME
    # (rm -rf ~root/../etc IS /etc, cd-pwd proof) — destructive verbs fail
    # closed on them; plain ~user/subpath operands stay resolvable (R2 W6).
    m = re.match(r"^~([A-Za-z0-9_.-]*)(/.*)?$", tok)
    if m and _DOTDOT_COMPONENT_RX.search(m.group(2) or ""):
        return True
    # Unbalanced brace fragments are expansion debris (e.g. IFS splitting a
    # value that contained a brace list) — the real operand is unknowable.
    if ("{" in tok) != ("}" in tok):
        return True
    return "{" in tok and _expand_braces(tok) is None


def _rm_root_order_free(tokens: list[str]) -> bool:
    """True when *tokens* (executable first, normalized spelling) are an rm
    with BOTH recursive and force flags AND a root-equivalent operand in ANY
    position (R5 A01-A03): ``rm -rf zzz /`` and ``rm zzz -rf /`` are the same
    catastrophe as ``rm -rf /``."""
    if not tokens or tokens[0].rsplit("/", 1)[-1].lower() != "rm":
        return False
    recursive = force = False
    root = False
    for tok in tokens[1:]:
        if tok == "--recursive":
            recursive = True
        elif tok == "--force":
            force = True
        elif tok.startswith("-") and tok != "-":
            if not tok.startswith("--"):
                recursive = recursive or "r" in tok[1:] or "R" in tok[1:]
                force = force or "f" in tok[1:]
        elif _RM_ROOT_OPERAND_RX.match(tok):
            root = True
    return recursive and force and root


def _xargs_repl(text: str) -> str | None:
    """The replacement string of xargs' ``-I``/``-i``/``--replace`` flag
    (R3 S7), or None when no placeholder substitution is configured. Only
    xargs' own leading flags are scanned; separated flag arguments (``-n 1``)
    are skipped so they cannot be misread as the placeholder or the payload.
    Bare ``-i``/``--replace`` default to ``{}`` (getopt: attached-only).
    R4-S3: word reading is quote-aware so ``-a 'my file'`` cannot desync the
    scan."""
    toks: list[str] = []
    pos = 0
    while pos < len(text):
        word, pos, _closed = _read_word(text, pos)
        if word:
            toks.append(word)
    i = 0
    while i < len(toks):
        tok = toks[i]
        if not tok.startswith("-") or tok == "-":
            break
        if tok.startswith("--"):
            name, eq, val = tok.partition("=")
            if name == "--replace":
                return val if eq else "{}"
            i += 2 if (not eq and name in _XARGS_REQ_LONG) else 1
            continue
        if tok.startswith("-I"):
            return tok[2:] or (toks[i + 1] if i + 1 < len(toks) else "{}")
        if tok.startswith("-i"):
            return tok[2:] or "{}"
        step = 1
        chars = tok[1:]
        for idx, ch in enumerate(chars):
            if ch in _XARGS_REQ_SHORT:
                step = 2 if idx == len(chars) - 1 else 1
                break
            if ch in _XARGS_OPT_SHORT:
                break
        i += step
    return None


def _xargs_has_arg_file(text: str) -> bool:
    """True when xargs' OWN leading flags include -a/--arg-file (R6 E8): the
    operands then come from a file the normalizer cannot read, so a destructive
    payload must fail closed. Cluster-aware (-ra) and quote-aware; stops at the
    first non-flag token (the payload command) so payload flags are not
    mistaken for xargs' own."""
    toks: list[str] = []
    pos = 0
    while pos < len(text):
        word, pos, _closed = _read_word(text, pos)
        if word:
            toks.append(word)
    i = 0
    while i < len(toks):
        tok = toks[i]
        if not tok.startswith("-") or tok == "-":
            break
        if tok.startswith("--"):
            name, eq, _val = tok.partition("=")
            if name == "--arg-file":
                return True
            i += 2 if (not eq and name in _XARGS_REQ_LONG) else 1
            continue
        if _cluster_flag_value(tok, "a", "dEILnPs")[0]:
            return True
        step = 1
        chars = tok[1:]
        for idx, ch in enumerate(chars):
            if ch in _XARGS_REQ_SHORT:
                step = 2 if idx == len(chars) - 1 else 1
                break
            if ch in _XARGS_OPT_SHORT:
                break
        i += step
    return False


# ---------------------------------------------------------------- R2 C3
# Dynamic-target resolution. The deny floor and the catastrophic gate need
# the EFFECTIVE operand, not the spelling: ${UNSET:-/}, $(echo /), backticks,
# "$@"/"$N" positionals and xargs stdin payloads all denote real targets.

def _raw_segments(text: str, *, posix: bool = False) -> list[tuple[str, str]]:
    """Split *text* into (preceding_separator, segment) pairs on unquoted
    ``&&``/``||``/``;``/``|``/``&``/newline. Quote-aware so payload boundaries
    (``bash -c '...'``) survive for positional-argument extraction.

    R7 gate-fix (#119): ``posix=True`` is the PRODUCT-shell (/bin/sh) fail-closed
    interpretation — an odd backslash run before a quote whose preceding char
    is non-special closes the string, exposing tail segments that the bash
    parity read merges into an inert lead (Alex H1 unclosed-tail family).
    Bash parity (default) is unchanged; the pass is over-inclusive by design
    and harmless for non-destructive commands, which never fail closed."""
    out: list[tuple[str, str]] = []
    buf: list[str] = []
    quote: str | None = None
    sep = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                if quote == "'":
                    quote = None
                else:
                    # R7 S6: backslash PARITY — a quote closes only after an
                    # EVEN run of backslashes ("a\\" ends the string; the old
                    # single-char lookbehind desynced on escaped backslashes).
                    j = i - 1
                    n = 0
                    while j >= 0 and text[j] == "\\":
                        n += 1
                        j -= 1
                    closes = n % 2 == 0
                    if posix and not closes and j >= 0 and text[j] not in "$`\"\\\n":
                        # R7 gate-fix (#119): product-shell fail-closed read —
                        # an odd backslash run preceded by a NON-special char
                        # is a potential string terminator (Alex H1 unclosed-
                        # tail family): close it so the tail segments surface
                        # and destructive verbs fail closed. Non-destructive
                        # commands are unaffected (over-inclusive by design).
                        closes = True
                    if closes:
                        quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < len(text):
            buf.append(text[i:i + 2])
            i += 2
            continue
        if ch in ";|&\n":
            j = i
            while j < len(text) and text[j] in ";|&\n":
                j += 1
            seg = "".join(buf).strip()
            if seg:
                out.append((sep, seg))
            sep = text[i:j]
            buf = []
            i = j
            continue
        buf.append(ch)
        i += 1
    seg = "".join(buf).strip()
    if seg:
        out.append((sep, seg))
    return out


def _tokens_raw(seg: str) -> list[str]:
    """Quote-preserving tokenization (POSIX shell words, quotes kept) so a
    ``bash -c`` payload stays ONE token with its trailing positional args
    visible.

    R9: bash-parity quote scanner (the SAME grammar as _raw_segments) —
    shlex.split(posix=False) closes a double quote at EVERY \" regardless
    of backslash parity, shredding nested echo-pipe payloads
    (echo "echo \"…\" | sh" | sh) into debris tokens. Inside double quotes
    a backslash escapes only $, `, \", \\ and newline; $'…' keeps ANSI-C
    semantics. Unbalanced quotes degrade to whitespace splitting (the
    fail-closed lanes own the imbalance itself)."""
    out: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    ansi = False
    i = 0
    while i < len(seg):
        ch = seg[i]
        if quote is not None:
            buf.append(ch)
            if ch == "\\" and i + 1 < len(seg) and (ansi
                    or (quote == '"' and seg[i + 1] in "$`\"\\\n")):
                buf.append(seg[i + 1])
                i += 2
                continue
            if ch == quote:
                if quote == "'" and not ansi:
                    quote = None
                else:
                    j = i - 1
                    n = 0
                    while j >= 0 and seg[j] == "\\":
                        n += 1
                        j -= 1
                    if n % 2 == 0:
                        quote = None
                        ansi = False
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            ansi = ch == "'" and i > 0 and seg[i - 1] == "$"
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < len(seg):
            buf.append(seg[i:i + 2])
            i += 2
            continue
        if ch.isspace():
            if buf:
                out.append("".join(buf))
                buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if buf:
        out.append("".join(buf))
    if quote is not None:
        return seg.split()
    return out


# R8 J2/J3: shell interpreters that EXECUTE stdin as code when fed by a pipe
# or a herestring (and no -c payload / script-file operand wins instead).
# busybox/sudo/env/… prefixes are stripped transparently before the check.
_STDIN_SHELLS = _SHELL_WRAPPERS | {"ash"}


def _shell_reads_stdin(rest: list[str]) -> bool:
    """True when a shell's own arguments leave it reading commands from
    stdin (R8 J2/J3): no -c (exact or short cluster — the inline payload
    ignores stdin) and no script-file operand (the script ignores stdin).
    Long --flags, bare - and -s/-i/… keep stdin reading."""
    for t in rest:
        if t == "-" or t.startswith("--"):
            continue
        if t.startswith("-"):
            if "c" in t[1:]:
                return False
            continue
        return False
    return True


def _decode_shell_escapes(text: str) -> str:
    """Decode backslash escapes (\\n, \\t, \\\\, …) the way printf's FORMAT
    and echo -e do. Runs on RAW token content — _strip_tricks would have
    already erased the backslash (R8 J2 printf-\\n leg).

    R9 E5: octal ``\\NNN`` (incl. printf's ``\\0NNN``) and hex ``\\xHH`` are
    decoded too — ``printf "rm -rf /\\057"`` and ``\\x2f`` both denote a real
    ``/`` once the shell receives them (stub OUT1/OUT2 proofs)."""
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt == "x":
                j = i + 2
                while j < len(text) and j < i + 4 and text[j] in "0123456789abcdefABCDEF":
                    j += 1
                if j > i + 2:
                    out.append(chr(int(text[i + 2:j], 16) & 0xFF))
                    i = j
                    continue
            elif nxt in "01234567":
                j = i + 1
                while j < len(text) and j < i + 4 and text[j] in "01234567":
                    j += 1
                out.append(chr(int(text[i + 1:j], 8) & 0xFF))
                i = j
                continue
            out.append(_ANSI_ESCAPES.get(nxt, nxt))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _dq_escape_decode(tok: str) -> str:
    """R9: inside DOUBLE quotes the shell removes a backslash only before
    $, `, \", \\ or newline (bash manual). Quote removal alone leaves \\\"
    debris that desyncs nested echo-pipe synthesis (echo \"…\" | sh chains)."""
    out: list[str] = []
    i = 0
    while i < len(tok):
        ch = tok[i]
        if ch == "\\" and i + 1 < len(tok) and tok[i + 1] in "$`\"\\\n":
            out.append(tok[i + 1])
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _literal_output(seg: str) -> str | None:
    """The literal stdout of a pure ``echo``/``printf`` segment, or None when
    the output is not statically knowable (variables, substitutions, other
    executables). Never executes anything.

    R8 J2: printf's FORMAT is read from the RAW quote-aware tokens and its
    backslash escapes are decoded — quote removal + escape stripping would
    otherwise mangle ``printf "rm -rf /\\n"`` into a non-root phantom. echo -e
    decodes escapes too (bash semantics); plain echo does NOT."""
    norm = _strip_tricks(seg)
    tokens = norm.split()
    if not tokens:
        return None
    exe = tokens[0].rsplit("/", 1)[-1]
    if exe == "echo":
        args = [t for t in tokens[1:] if t not in ("-n", "-e", "-E")]
        if any("$" in t or "`" in t for t in args):
            return None
        if "-e" in tokens[1:] and "-E" not in tokens[1:]:
            raw = _tokens_raw(seg)
            rargs = [_dq_escape_decode(_unquote_outer(t)) if t.startswith('"')
                     else _unquote_outer(t)
                     for t in raw[1:] if t not in ("-n", "-e", "-E")]
            if any("$" in t or "`" in t for t in rargs):
                return None
            return _decode_shell_escapes(" ".join(rargs)).strip() or None
        raw = _tokens_raw(seg)
        rargs = [_dq_escape_decode(_unquote_outer(t)) if t.startswith('"')
                 else _unquote_outer(t)
                 for t in raw[1:] if t not in ("-n", "-e", "-E")]
        return " ".join(rargs) or None
    if exe == "printf":
        raw = _tokens_raw(seg)
        if len(raw) < 2:
            return None
        fmt = _dq_escape_decode(_unquote_outer(raw[1])) if raw[1].startswith('"') \
            else _unquote_outer(raw[1])
        args = [_dq_escape_decode(_unquote_outer(t)) if t.startswith('"')
                else _unquote_outer(t) for t in raw[2:]]
        if any("$" in t or "`" in t for t in [fmt, *args]):
            return None
        if "%" in fmt.replace("%s", ""):
            return None  # only plain %s placeholders are statically foldable
        out = fmt
        for arg in args:
            out = out.replace("%s", arg, 1)
        out = _decode_shell_escapes(out).strip()
        return out or None
    return None


# R9 structural rescan (task #130): E1-E5+B11 are ONE family — synthesized
# stdin/literal-output payloads escaping re-analysis. Every synthesis site
# routes through _rescan_payload (ONE entry point, bounded depth). These
# helpers compute what a pipe stage's stdin/stdout statically IS.
_RESCAN_MAX_DEPTH = 6


def _herestring_content(seg: str) -> str | None:
    """The (outer-unquoted) content of a ``<<<`` herestring, or None."""
    toks = _tokens_raw(seg)
    if "<<<" not in toks:
        return None
    hi = toks.index("<<<")
    return _unquote_outer(" ".join(toks[hi + 1:]))


def _is_passthrough(seg: str) -> bool:
    """True when a pipe stage forwards its stdin to stdout UNCHANGED (R9 E1):
    cat (no file operands), tee (stdout always mirrors stdin), head/tail (no
    file operands, non-zero count), sed with an empty script, awk print.
    Transparent/privilege prefixes are stripped first. Conservative: any
    other shape is NOT a passthrough (its output is not the upstream stdin)."""
    toks = _tokens_raw(seg)
    if "<<<" in toks:
        toks = toks[:toks.index("<<<")]
    i = 0
    while i < len(toks) and _ASSIGN_TOK_RX.match(_strip_tricks(toks[i])):
        i += 1
    if i >= len(toks):
        return False
    payload = _strip_transparent_prefixes(" ".join(toks[i:]))
    ptoks = [_unquote_outer(_strip_tricks(t)) for t in _tokens_raw(payload)]
    if not ptoks:
        return False
    base = ptoks[0].rsplit("/", 1)[-1].lower()
    args = ptoks[1:]
    if base == "cat":
        return all(a.startswith("-") for a in args)
    if base == "tee":
        return True
    if base in ("head", "tail"):
        k = 0
        while k < len(args):
            a = args[k]
            if a in ("-n", "-c", "--lines", "--bytes"):
                if k + 1 >= len(args):
                    return False
                cnt = args[k + 1].lstrip("+-")
                if cnt.isdigit() and int(cnt) == 0:
                    return False
                k += 2
                continue
            if a.startswith(("-n", "-c", "--lines=", "--bytes=")):
                val = (a.split("=", 1)[1] if "=" in a else a[2:]).lstrip("+-")
                if val.isdigit() and int(val) == 0:
                    return False
                k += 1
                continue
            if a.startswith("-"):
                k += 1
                continue
            return False  # file operand: stdout is the FILE, not stdin
        return True
    if base == "sed":
        return args == [""]
    if base == "awk":
        return len(args) == 1 and args[0].replace(" ", "") in ("{print}", "{print$0}")
    return False


def _pipe_producer(segs: list[tuple[str, str]], idx: int) -> str | None:
    """The segment whose stdout reaches pipe stage *idx* (R9 E1): walk back
    over passthrough stages (bounded 6 hops). A passthrough fed by a
    herestring IS the producer (its content is its stdout)."""
    j = idx - 1
    for _ in range(6):
        if j < 0:
            return None
        sep_j, seg_j = segs[j]
        if not _is_passthrough(seg_j):
            return seg_j
        if _herestring_content(seg_j):
            return seg_j
        if j == 0 or "|" not in sep_j or "||" in sep_j:
            return None
        j -= 1
    return None


def _pipe_literal(segs: list[tuple[str, str]], idx: int) -> str | None:
    """The statically-literal stdout reaching pipe stage *idx*, or None.
    Replaces every single-hop ``_literal_output(segs[idx-1])`` read (R9 E1):
    multi-hop passthrough chains, herestring-fed passthroughs, and upstream
    variable assignments (R9 B30: X=/{a..z}; echo \"rm -rf $X\" | sh)."""
    prod = _pipe_producer(segs, idx)
    if prod is None:
        return None
    if _is_passthrough(prod):
        hs = _herestring_content(prod)
        return hs.strip() if hs and hs.strip() else None
    raw = "".join(sep + s for sep, s in segs)
    norm = " ".join(_strip_tricks(s) for _sep, s in segs)
    expanded = _sub_refs(prod, _merged_assignments(raw, norm))
    return _literal_output(expanded)


def _pipe_remote_source(segs: list[tuple[str, str]], idx: int) -> bool:
    """True when the producer feeding pipe stage *idx* is curl/wget (R9:
    the curl|sh invariant must hold across intermediate passthrough pipes —
    risk rule 68's ``[^|;]*`` cannot cross them)."""
    prod = _pipe_producer(segs, idx)
    if prod is None:
        return False
    toks = prod.split()
    i = 0
    while i < len(toks) and _ASSIGN_TOK_RX.match(toks[i]):
        i += 1
    payload = _strip_transparent_prefixes(" ".join(toks[i:]))
    ptoks = payload.split()
    return bool(ptoks) and ptoks[0].rsplit("/", 1)[-1].lower() in ("curl", "wget")


def _c_payload(rest: list[str]) -> str:
    """The -c command string of a shell's argument list (exact ``-c VALUE``
    or attached ``-cVALUE``), outer-unquoted; \"\" when absent."""
    for k, t in enumerate(rest):
        if t == "-c":
            return _unquote_outer(rest[k + 1]) if k + 1 < len(rest) else ""
        if t.startswith("-c") and not t.startswith("--"):
            return _unquote_outer(t[2:])
    return ""


def _inherits_stdin(payload: str) -> bool:
    """R9 E4: True when a -c/eval payload SPAWNS a stdin-reading shell that
    inherits the OUTER pipe/herestring (``sh -c sh``, ``eval sh``,
    ``sh -c 'cat | sh'``). Every upstream segment inside the payload must be
    a pure passthrough — a literal producer (echo …) feeds the inner shell
    its OWN output instead, so the outer stdin is NOT executed."""
    segs = _raw_segments(payload)
    if not segs:
        return False
    _sep, last = segs[-1]
    toks = last.split()
    i = 0
    while i < len(toks) and _ASSIGN_TOK_RX.match(toks[i]):
        i += 1
    stripped = _strip_transparent_prefixes(" ".join(toks[i:]))
    ptoks = stripped.split()
    if not ptoks:
        return False
    if ptoks[0].rsplit("/", 1)[-1].lower() not in _STDIN_SHELLS:
        return False
    rest = ptoks[1:]
    if "<<<" in rest:
        rest = rest[:rest.index("<<<")]
    if not _shell_reads_stdin(rest):
        return False
    return all(_is_passthrough(segs[j][1]) for j in range(len(segs) - 1))


def _seg_feeding(segs: list[tuple[str, str]], idx: int, sep: str, seg: str,
                 stdin: str | None) -> str | None:
    """The stdin content a segment's shell would EXECUTE: its own herestring
    first, else the upstream pipe's literal output, else inherited stdin
    (R9 E2/E4 — ONE feeding computation shared by every scan/walk site)."""
    hs = _herestring_content(seg)
    if hs is not None and hs.strip():
        return hs
    if "|" in sep and "||" not in sep and idx > 0:
        pl = _pipe_literal(segs, idx)
        if pl and pl.strip():
            return pl
    return stdin


def _expand_positionals(text: str, positionals: list[str]) -> str:
    """Expand ``$@``/``$*``/``$N``/``${N}`` from tracked ``set --`` or
    ``sh -c`` positional arguments. Out-of-range refs are left as-is."""
    if not positionals:
        return text

    def _sub(m: re.Match[str]) -> str:
        ref = m.group(1) or m.group(2)
        if ref in ("@", "*"):
            return " ".join(positionals)
        idx = int(ref)
        return positionals[idx - 1] if 1 <= idx <= len(positionals) else m.group(0)

    return _POS_REF_RX.sub(_sub, text)


def _fold_defaults(text: str) -> str:
    """Fold ``${VAR:-default}``/``${VAR-default}``: the assigned value when
    VAR has a literal assignment, else the default (R2 C3)."""
    values: dict[str, str] = {}
    for m in _ASSIGN_RX.finditer(text):
        values[m.group(1)] = m.group(2).strip("'\"")

    def _sub(m: re.Match[str]) -> str:
        name, default = m.group(1), m.group(2)
        return values.get(name) or default

    return _DEFAULT_RX.sub(_sub, text)


def _sub_literal_subshells(text: str) -> str:
    """Substitute ``$()``/backtick interiors whose output is a pure literal
    echo/printf into operand position (R2 C3). Non-literal interiors are
    left untouched for the fail-closed scan."""

    def _sub(m: re.Match[str]) -> str:
        inner = m.group(1) if m.group(1) is not None else (m.group(2) or "")
        lit = _literal_output(inner)
        return lit if lit is not None else m.group(0)

    return _SUBST_RX.sub(_sub, text)


def _resolve_dynamic(text: str, positionals: list[str] | None = None) -> str:
    """Resolve dynamic targets on already deobfuscated text, preserving
    segment separators: ``set --`` positionals, ``$@``/``$N``, default-value
    braces, literal command substitution, and xargs stdin synthesis (the
    upstream pipe segment's literal output becomes payload operands)."""
    segs = _raw_segments(text)
    if not segs:
        return text
    pos = list(positionals or [])
    for _sep, seg in segs:
        toks = seg.split()
        if toks and toks[0] == "set" and "--" in toks[1:]:
            pos = toks[toks.index("--") + 1:]
    out: list[str] = []
    for idx, (sep, seg) in enumerate(segs):
        s = _expand_positionals(seg, pos)
        s = _fold_defaults(s)
        s = _sub_literal_subshells(s)
        toks = s.split()
        if (toks and toks[0].rsplit("/", 1)[-1] == "xargs"
                and "|" in sep and "||" not in sep and idx > 0):
            lit = _pipe_literal(segs, idx)
            remainder = _raw_remainder(s)
            payload = _skip_wrapper_flags(
                remainder, req_short=_XARGS_REQ_SHORT,
                opt_short=_XARGS_OPT_SHORT, req_long=_XARGS_REQ_LONG,
            )
            if lit is not None and payload.strip():
                repl = _xargs_repl(remainder)
                if repl is not None:
                    # R3 S7: -I REPL substitutes the placeholder with the
                    # upstream literal — `echo rm | xargs -I{} {} -rf /`
                    # denotes `rm -rf /`, not a command named "{}".
                    head = s[:len(s) - len(remainder)]
                    flags = remainder[:len(remainder) - len(payload)]
                    s = head + flags + payload.replace(repl, lit)
                else:
                    s = s + " " + lit
        elif toks:
            # R8 J2/J3 + R9 E1/E4: stdin synthesis for ANY shell interpreter —
            # a pipe RHS or herestring-fed sh/bash/dash/ash/zsh/ksh (incl.
            # busybox/sudo prefixes) EXECUTES its stdin as code. The synthesized
            # content REPLACES the shell segment (it IS the command run);
            # transparent prefixes are preserved. -c payloads and script-file
            # operands ignore stdin — UNLESS the payload spawns a stdin-reading
            # shell (R9 E4: sh -c sh / eval sh inherit the pipe). curl|sh stays
            # untouched: curl output is not statically literal, so the existing
            # remote-script rule keeps firing (cross-mode invariant).
            i = 0
            while i < len(toks) and _ASSIGN_TOK_RX.match(toks[i]):
                i += 1
            payload = _strip_transparent_prefixes(" ".join(toks[i:]))
            ptoks = payload.split()
            pbase = ptoks[0].rsplit("/", 1)[-1].lower() if ptoks else ""
            if pbase in _STDIN_SHELLS or pbase == "eval":
                rest = ptoks[1:]
                hs: str | None = None
                if "<<<" in rest:
                    hi = rest.index("<<<")
                    hs = " ".join(rest[hi + 1:])
                    rest = rest[:hi]
                reads = _shell_reads_stdin(rest)
                if not reads:
                    # R9 E4: the -c payload boundary is quote-aware — read it
                    # from the RAW segment (deobfuscation splits multi-word
                    # payloads: sh -c 'busybox sh' must inherit as ONE unit).
                    inner = (_unquote_outer(" ".join(rest)) if pbase == "eval"
                             else _c_payload(_tokens_raw(seg)))
                    reads = _inherits_stdin(inner)
                if reads:
                    head = s[:len(s) - len(payload)] if s.endswith(payload) else ""
                    if hs is not None:
                        if hs.strip():
                            s = head + hs
                    elif "|" in sep and "||" not in sep and idx > 0:
                        lit = _pipe_literal(segs, idx)
                        if lit is not None and lit.strip():
                            s = head + lit
        out.append(sep + s)
    return "".join(out)


_DESTRUCTIVE_EXES = frozenset({"rm", "shred", "dd"})

# R8 J1: filesystem-destructive verbs whose path operands are lexically
# collapsed (kernel-resolution semantics) BEFORE root-operand matching —
# the kernel resolves /home/../etc to /etc before rm ever sees it, so the
# dotdot spelling must not dodge the catastrophic gate / deny floor / scorer.
_COLLAPSE_VERBS = frozenset({"rm", "shred", "dd", "find", "chmod", "chown", "wipefs"})
# A path token with a "."/".." COMPONENT (not a bare name like 777 or file.txt
# — mode operands and plain filenames are never rewritten).
_DOT_COMPONENT_RX = re.compile(r"(?:^|/)\.{1,2}(?:/|$)")
# Upward traversal specifically: relative operands only collapse against cwd
# when they actually escape via ".." — workspace-relative cleanup (find . /
# find ./build -delete, R3 S6) must keep its relative, approvable spelling.
_DOTDOT_COMPONENT_RX = re.compile(r"(?:^|/)\.\.(?:/|$)")


def _collapse_destructive_seg(seg: str, cwd: str | None = None) -> str | None:
    """R8 J1: normpath-collapse the path operands of a filesystem-destructive
    verb segment (assignment prefixes and transparent wrappers preserved).
    Returns the collapsed segment, or None when the verb is not destructive
    or nothing changed. Dynamic ``$…``/backtick, brace, bare ``~``/unknown-user
    and ``key=value`` operands are left untouched (the fail-closed lane owns
    unresolvable ones); relative operands collapse against *cwd* only when it
    is supplied. Non-destructive verbs are NEVER collapsed (over-block guard)."""
    toks = seg.split()
    i = 0
    while i < len(toks) and _ASSIGN_TOK_RX.match(toks[i]):
        i += 1
    if i >= len(toks):
        return None
    orig = " ".join(toks[i:])
    payload = _strip_transparent_prefixes(orig)
    ptoks = payload.split()
    if not ptoks:
        return None
    if ptoks[0].rsplit("/", 1)[-1].lower() not in _COLLAPSE_VERBS:
        return None
    changed = False
    out: list[str] = []
    for tok in ptoks[1:]:
        if (tok.startswith("-") or "=" in tok
                or "$" in tok or "`" in tok or "{" in tok
                or not _DOT_COMPONENT_RX.search(tok)):
            out.append(tok)
            continue
        if tok.startswith("/"):
            new = posixpath.normpath(tok)
        elif tok.startswith("~"):
            # R9 E3: ~user/../… kernel-resolves against the user's HOME
            # (cd ~root/../etc && pwd → /etc). Named-user homes resolve via
            # pwd; bare ~ and unknown users stay untouched (fail-closed lane).
            m = re.match(r"^~([A-Za-z0-9_.-]+)(/.*)?$", tok)
            if m is None:
                out.append(tok)
                continue
            try:
                import pwd
                home = pwd.getpwnam(m.group(1)).pw_dir
            except (KeyError, ImportError):
                home = ""
            if not home.startswith("/"):
                out.append(tok)
                continue
            new = posixpath.normpath(home + (m.group(2) or ""))
        elif cwd and cwd.startswith("/") and _DOTDOT_COMPONENT_RX.search(tok):
            new = posixpath.normpath(posixpath.join(cwd, tok))
        else:
            out.append(tok)
            continue
        changed = changed or new != tok
        out.append(new)
    if not changed:
        return None
    prefix = orig[:len(orig) - len(payload)]
    head = " ".join(toks[:i])
    return ((head + " ") if head else "") + prefix + " ".join([ptoks[0]] + out)


def _is_find_delete(args: list[str]) -> bool:
    joined = " ".join(args)
    return "-delete" in args or bool(re.search(r"-exec(dir)?\s+rm\b", joined))


def _destructive_brace_position(text: str) -> bool:
    """R8 J4: True when an unresolvable-brace segment carries a filesystem-
    destructive verb at COMMAND position (assignment skip + transparent
    prefix strip), or the command-position word itself is brace-built
    (R7 S1 doctrine: the brace word may BE the verb). Subcommand/argument
    positions (git rm --cached, docker rm, echo rm) are NOT filesystem-
    destructive and must never fail closed on a brace range."""
    toks = text.split()
    i = 0
    while i < len(toks) and _ASSIGN_TOK_RX.match(toks[i]):
        i += 1
    payload = _strip_transparent_prefixes(" ".join(toks[i:]))
    ptoks = payload.split()
    if not ptoks:
        return False
    head = ptoks[0]
    if "{" in head:
        return True
    base = head.rsplit("/", 1)[-1]
    return base in _DESTRUCTIVE_EXES or (
        base == "find" and "-delete" in ptoks[1:])


def _destructive_shape(payload: str, *, posix: bool = False) -> bool:
    """R9 depth-exhaustion backstop: True when a filesystem-destructive verb
    sits at COMMAND position in the payload or its one-pass dynamic resolution
    (synthesized stdin included). Non-destructive commands NEVER fail closed."""
    for text in (payload, _resolve_dynamic(payload)):
        for _sep, seg in _raw_segments(text, posix=posix):
            if _destructive_brace_position(seg):
                return True
    return False


def _rescan_payload(payload: str, positionals: list[str], depth: int,
                    *, posix: bool = False, stdin: str | None = None) -> bool:
    """R9 structural rescan — the ONE entry point every synthesis site routes
    through (E2 meta-gap): a synthesized payload is re-analyzed as if it were
    a fresh command line, recursively, bounded at _RESCAN_MAX_DEPTH. On depth
    exhaustion only DESTRUCTIVE shapes fail closed (non-destructive commands
    never do; full_access silent-ALLOW semantics preserved)."""
    if depth > _RESCAN_MAX_DEPTH:
        return _destructive_shape(payload, posix=posix)
    return _scan_unresolved(payload, positionals, depth, posix=posix, stdin=stdin)


def unresolved_destructive_operands(command: str) -> bool:
    """Fail-closed input for the catastrophic gate (R2 C3): True when a
    destructive executable (rm/shred/dd/find -delete) carries an operand the
    normalizer cannot resolve — an unexpanded ``$…``/backtick after full
    resolution, or an xargs payload fed by non-literal piped stdin. Such
    commands must never be silent-allowed in full_access.

    R7 gate-fix (#119): the scan runs under BOTH shell interpretations — bash
    parity AND the product-shell (/bin/sh) POSIX double-quote read. When the
    two disagree on quote closure (Alex H1), a destructive verb exposed only
    by the POSIX pass still fails closed. Non-destructive commands never fail
    closed on either pass."""
    cmd = str(command)
    return _scan_unresolved(cmd, [], 0) or _scan_unresolved(cmd, [], 0, posix=True)


def _scan_unresolved(text: str, positionals: list[str], depth: int,
                     *, posix: bool = False, stdin: str | None = None) -> bool:
    if depth > _RESCAN_MAX_DEPTH:
        return _destructive_shape(text, posix=posix)
    segs = _raw_segments(text, posix=posix)
    pos = list(positionals)
    for _sep, seg in segs:
        stripped = [_strip_tricks(t) for t in _tokens_raw(seg)]
        if stripped and stripped[0] == "set" and "--" in stripped[1:]:
            pos = stripped[stripped.index("--") + 1:]
    # assignments may live in an EARLIER segment than the reference
    # (``X=/tmp/f; rm -rf $X``) — collect them across the whole text.
    norm_all = " ".join(_strip_tricks(seg) for _sep, seg in segs)
    values = _merged_assignments(text, norm_all)
    for idx, (sep, seg) in enumerate(segs):
        rtoks = _tokens_raw(seg)
        if not rtoks:
            continue
        # R9 E2: the stdin this segment's shell would EXECUTE (herestring,
        # upstream pipe literal through passthrough chains, or inherited).
        feeding = _seg_feeding(segs, idx, sep, seg, stdin)
        base = _strip_tricks(rtoks[0]).rsplit("/", 1)[-1]
        if base in _SHELL_WRAPPERS and "-c" in rtoks[1:3]:
            ci = rtoks.index("-c")
            if ci + 1 < len(rtoks):
                pargs = [_strip_tricks(t) for t in rtoks[ci + 2:]]
                if _rescan_payload(_unquote_outer(rtoks[ci + 1]), pargs[1:],
                                   depth + 1, posix=posix, stdin=feeding):
                    return True
            continue
        norm = _strip_tricks(seg)
        if base == "eval":
            # R7 gate-fix (#119): recurse on the RAW remainder (outer quotes
            # stripped) so the interior assignment quoting survives.
            if _rescan_payload(_unquote_outer(_raw_remainder(seg)), pos,
                               depth + 1, posix=posix, stdin=feeding):
                return True
            continue
        if base == "env":
            payload = _skip_env_flags(_raw_remainder(norm))
            if _rescan_payload(payload, pos, depth + 1, posix=posix, stdin=feeding):
                return True
            continue
        norm = _expand_vars(norm)
        norm = _sub_refs(norm, values)
        norm = _expand_positionals(norm, pos)
        norm = _fold_defaults(norm)
        norm = _sub_literal_subshells(norm)
        # $HOME is always assigned in a real shell — it is resolvable, not
        # an unknown operand (R2 W6 balance: `rm $HOME/notes.txt` is benign).
        norm = norm.replace("$HOME", "~")
        # R7 S1: expand brace lists so a brace-built verb/operand is visible
        # ({rm,-rf,/} IS rm). R8 J4: an UNRESOLVABLE brace list fails closed
        # only when a destructive verb sits at COMMAND position (or the
        # brace word may BE the verb) — git rm --cached / docker rm /
        # echo rm carry the word in subcommand/argument position and pass.
        braced = _expand_braces(norm)
        if braced is None:
            if _destructive_brace_position(norm):
                return True
        else:
            norm = braced
        toks = norm.split()
        i = 0
        while i < len(toks) and _ASSIGN_TOK_RX.match(toks[i]):
            i += 1
        if i >= len(toks):
            continue
        base = toks[i].rsplit("/", 1)[-1]
        args = toks[i + 1:]
        if base in _PRIV_WRAPPERS:
            # sudo/doas are transparent: scan the real executable's operands
            payload = _skip_wrapper_flags(
                " ".join(args), req_short=_PRIV_REQ_SHORT,
                req_long=_PRIV_REQ_LONG,
            )
            toks = payload.split()
            if not toks:
                continue
            base = toks[0].rsplit("/", 1)[-1]
            args = toks[1:]
        if base in _TRANSPARENT_WRAPPERS:
            # R5 A04-A13: busybox/toybox/exec/nice/… are transparent too —
            # an unresolved operand behind them is still unresolved.
            payload = _strip_transparent_prefixes(" ".join([base] + args))
            toks = payload.split()
            if not toks:
                continue
            base = toks[0].rsplit("/", 1)[-1]
            args = toks[1:]
        if base in _STDIN_SHELLS:
            # R9 E2/E4: a stdin-reading shell EXECUTES its feeding content —
            # rescan it as a fresh command line (the meta-gap fix). curl/wget
            # producers fail closed across passthrough hops (curl|sh invariant
            # extended; rule 68 cannot cross intermediate pipes).
            sargs = args[:args.index("<<<")] if "<<<" in args else args
            if _shell_reads_stdin(sargs):
                if feeding and feeding.strip():
                    if _rescan_payload(feeding, pos, depth + 1, posix=posix):
                        return True
                elif "|" in sep and "||" not in sep and idx > 0 \
                        and _pipe_remote_source(segs, idx):
                    return True
            continue
        if base == "xargs":
            argtext = " ".join(args)
            repl = _xargs_repl(argtext)
            payload = _skip_wrapper_flags(
                argtext, req_short=_XARGS_REQ_SHORT,
                opt_short=_XARGS_OPT_SHORT, req_long=_XARGS_REQ_LONG,
            )
            ptoks = payload.split()
            if not ptoks:
                continue
            # R7 S1: a brace-built xargs payload verb is the real verb.
            # R8 J4: command-position-aware — git rm --cached / docker rm
            # payloads are not filesystem-destructive and pass.
            pbraced = _expand_braces(payload)
            if pbraced is None:
                if _destructive_brace_position(payload):
                    return True
                continue
            ptoks = pbraced.split()
            lit: str | None = None
            if "|" in sep and "||" not in sep and idx > 0:
                lit = _pipe_literal(segs, idx)
            if repl is None and lit is not None:
                # R9 B11: append-lane parity with -I — the upstream literal
                # becomes payload OPERANDS, so brace/glob/$ debris inside it
                # fails closed exactly like the substituted form.
                ptoks = ptoks + lit.split()
            if repl is not None:
                if lit is not None:
                    ptoks = [t.replace(repl, lit) for t in ptoks]
                elif any(repl in t for t in ptoks):
                    # R3 S7 fail-closed: an unsubstituted placeholder (exe or
                    # operand) is an unresolvable target, exactly like $VAR.
                    return True
            pbase = ptoks[0].rsplit("/", 1)[-1]
            destructive = pbase in _DESTRUCTIVE_EXES or (
                pbase == "find" and _is_find_delete(ptoks[1:]))
            if not destructive:
                continue
            # R6 E8: -a/--arg-file feeds operands from a file the normalizer
            # cannot read — a destructive payload is unresolvable, fail closed.
            if _xargs_has_arg_file(argtext):
                return True
            if "|" in sep and "||" not in sep and lit is None:
                return True
            if any(_unresolvable_operand(t) for t in ptoks[1:] if not t.startswith("-")):
                return True
            continue
        if base in _DESTRUCTIVE_EXES or (base == "find" and _is_find_delete(args)):
            operands = [t for t in args if not t.startswith("-")]
            # R7 fail-closed family: $…/backtick (R2 C3) + first-component
            # glob (S2) + whole ~user home (S5) + unresolvable braces (S1).
            if any(_unresolvable_operand(t) for t in operands):
                return True
    return False


def command_variants(command: str, cwd: str | None = None) -> list[str]:
    """Return normalized variants of *command* for deny-glob matching.

    Includes the normalized whole, its whitespace-free form, every chained
    segment (including single-``&`` background chains), each segment's
    executable (full token and basename), one level of ``$()``/backtick/
    ``(...)`` interiors, and one level of ``sh -c``/``bash -c``/``eval``/
    ``env``/``xargs`` payloads. Dynamic targets are resolved (R2 C3):
    ``${VAR:-default}``, literal ``$()``/backtick output, ``set --``/``sh -c``
    positionals (``$@``/``$N``), and xargs stdin payloads synthesized from
    the upstream pipe segment's literal output. Order is stable and
    duplicates are removed.
    """
    seen: list[str] = []

    def add(value: str) -> None:
        value = value.strip()
        if value and value not in seen:
            seen.append(value)

    def walk(text: str, depth: int, positionals: list[str] | None = None,
             stdin: str | None = None) -> None:
        norm = _strip_tricks(text)
        if not norm:
            return
        add(norm)
        add(_compact(norm))
        expanded = _sub_refs(norm, _merged_assignments(text, norm))
        # R7 S1: brace expansion is part of the shared grammar — the deny
        # floor and the catastrophic gate must see the expanded words. Runs
        # AFTER substitution so braces inside assigned values expand too
        # (IFS=,; X=rm,-rf,/{home,usr}; $X).
        braced = _expand_braces(expanded)
        if braced is None:
            braced = expanded  # unresolvable: _scan_unresolved fails closed
        if braced != norm:
            add(braced)
            add(_compact(braced))
            if depth < 1:
                # re-walk the expanded form (fixpoint: re-expansion is a
                # no-op, so this terminates after one extra level)
                walk(braced, depth + 1, positionals)
        resolved = _resolve_dynamic(braced, positionals)
        if resolved != expanded:
            add(resolved)
            add(_compact(resolved))
            if depth < 1:
                walk(resolved, depth + 1, positionals)
        if depth < 1:
            for match in _SUBSHELL_RX.finditer(norm):
                inner = match.group(1) or match.group(2) or match.group(3) or ""
                if inner.strip():
                    walk(inner, depth + 1)
        segments = [s.strip() for s in _SPLIT_RX.split(norm) if s.strip()]
        # R7 gate-fix (#119): the EXPANDED form's segments must surface at
        # every depth — wrapper recursion (depth >= 1) does not re-walk the
        # expanded text, so a resolved payload segment (Alex H1 inside
        # sh -c '…': …; rm -rf /) would otherwise stay hidden from the
        # anchored deny globs and the catastrophic gate.
        if braced != norm:
            segments += [s.strip() for s in _SPLIT_RX.split(braced) if s.strip()]
        for seg in segments:
            add(seg)
            add(_compact(seg))
            folded = _semantic_flags(seg)
            if folded != seg:
                add(folded)
                add(_compact(folded))
            # R8 J1: kernel-resolution collapse of destructive operands —
            # /home/../etc IS /etc, so the collapsed spelling must reach the
            # deny floor and the catastrophic gate too (both flag forms).
            for base_seg in (seg, folded):
                collapsed = _collapse_destructive_seg(base_seg, cwd)
                if collapsed and collapsed != base_seg:
                    add(collapsed)
                    add(_compact(collapsed))
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
                payload = _skip_env_flags(_raw_remainder(seg))
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
                    walk(payload, depth + 1, positionals)
            elif base in _TRANSPARENT_WRAPPERS:
                # R5 A04-A13/B8: transparent exec prefixes must expose the
                # real payload to anchored deny globs and the catastrophic
                # gate (busybox rm -rf / denotes rm -rf /).
                payload = _strip_transparent_prefixes(seg)
                if payload.strip() and payload != seg:
                    walk(payload, depth + 1, positionals)
        if depth < _RESCAN_MAX_DEPTH:
            # Quote-aware pass (R2 C3): precise `sh -c` payload boundaries
            # expose trailing positional arguments ($0, $1, …), and xargs
            # payloads gain the upstream pipe segment's literal output as
            # operands — the normalized whole-text pass above cannot see
            # either because quote removal erases the payload boundary.
            # R9: the budget matches the rescan bound so nested synthesis
            # fixpoints (echo "…" | sh chains) stay visible to the floor.
            raw_segs = _raw_segments(text)
            for idx, (sep, rseg) in enumerate(raw_segs):
                rtoks = _tokens_raw(rseg)
                if not rtoks:
                    continue
                rbase = _strip_tricks(rtoks[0]).rsplit("/", 1)[-1]
                feeding = _seg_feeding(raw_segs, idx, sep, rseg, stdin)
                if rbase in _SHELL_WRAPPERS and "-c" in rtoks[1:3]:
                    ci = rtoks.index("-c")
                    if ci + 1 < len(rtoks):
                        pargs = [_strip_tricks(t) for t in rtoks[ci + 2:]]
                        walk(_unquote_outer(rtoks[ci + 1]), depth + 1, pargs[1:],
                             stdin=feeding)
                elif rbase == "eval":
                    # R7 gate-fix (#119): raw eval payload keeps its interior
                    # quoting for the assignment extractor (Alex H1 family).
                    payload = _unquote_outer(_raw_remainder(rseg))
                    if payload.strip():
                        walk(payload, depth + 1, positionals, stdin=feeding)
                elif rbase == "xargs":
                    # R4-S3: the RAW remainder keeps quoted flag arguments
                    # (``-a 'my file'``) intact for the quote-aware skip, and
                    # non-piped xargs payloads are exposed too — the deny
                    # floor must see them even without upstream stdin.
                    remainder = _raw_remainder(rseg)
                    payload = _skip_wrapper_flags(
                        remainder,
                        req_short=_XARGS_REQ_SHORT, opt_short=_XARGS_OPT_SHORT,
                        req_long=_XARGS_REQ_LONG,
                    )
                    if not payload.strip():
                        continue
                    if "|" in sep and "||" not in sep and idx > 0:
                        lit = _pipe_literal(raw_segs, idx)
                        if lit is not None:
                            repl = _xargs_repl(remainder)
                            if repl is not None:
                                # R3 S7: substitute the -I placeholder so the
                                # deny floor sees the real payload command.
                                walk(payload.replace(repl, lit), depth + 1, positionals)
                            else:
                                walk(payload + " " + lit, depth + 1, positionals)
                    else:
                        walk(payload, depth + 1, positionals)
                elif rbase in _STDIN_SHELLS:
                    # R8 J2/J3 + R9 E1/E2/E4: quote-aware raw pass — herestring
                    # content, literal piped stdin (through passthrough chains)
                    # and INHERITED stdin are EXECUTED by the shell; walk them
                    # as command input so a quoted multi-command payload
                    # (sh <<< "rm -rf /; echo done") exposes its inner segments
                    # to the deny floor and the catastrophic gate.
                    rest = rtoks[1:]
                    hs_raw: str | None = None
                    if "<<<" in rest:
                        hi = rest.index("<<<")
                        hs_raw = _unquote_outer(" ".join(rest[hi + 1:]))
                        rest = rest[:hi]
                    if _shell_reads_stdin(rest):
                        content = hs_raw if hs_raw and hs_raw.strip() else None
                        if content is None and "|" in sep and "||" not in sep and idx > 0:
                            content = _pipe_literal(raw_segs, idx)
                        if content is None:
                            content = stdin
                        if content and content.strip():
                            walk(content, depth + 1, positionals)
                elif rbase == "env":
                    # R6 E3: quote-aware raw pass — the -S nested command keeps
                    # its word boundaries (-S'rm -rf /' is ONE quoted value).
                    payload = _skip_env_flags(_raw_remainder(rseg))
                    if payload.strip():
                        walk(payload, depth + 1, positionals, stdin=feeding)
                elif rbase in _PRIV_WRAPPERS:
                    # R4-S1: sudo/doas are transparent — walk the real payload
                    # from the RAW remainder so quoted multi-word flag args
                    # (``sudo -p 'x y' rm -rf /home``) cannot hide it from the
                    # deny floor or the catastrophic gate.
                    payload = _skip_wrapper_flags(
                        _raw_remainder(rseg),
                        req_short=_PRIV_REQ_SHORT, req_long=_PRIV_REQ_LONG,
                    )
                    if payload.strip():
                        walk(payload, depth + 1, positionals, stdin=feeding)
                elif rbase in _TRANSPARENT_WRAPPERS:
                    # R5 A04-A13 / R6 E2: quote-aware raw pass — re-prepend the
                    # wrapper word (the remainder starts at its FLAGS, and
                    # _strip_transparent_prefixes keys on the executable) so a
                    # quoted multi-word flag arg (exec -a 'my name' rm …) is
                    # consumed as ONE word and the real payload stays visible.
                    payload = _strip_transparent_prefixes(
                        (_strip_tricks(rtoks[0]) + " " + _raw_remainder(rseg)).strip())
                    if payload.strip():
                        walk(payload, depth + 1, positionals, stdin=feeding)

    walk(str(command), 0)
    return seen


def normalize_command(command: str) -> str:
    """Single canonical normalized form for risk scoring: deobfuscate,
    expand literal variable assignments (last-wins), resolve dynamic targets
    (defaults, positionals, literal substitutions, xargs stdin), fold
    semantic flags. Unlike :func:`command_variants` this returns ONE string
    (no bare-executable variants) so scoring cannot be inflated by stray
    tokens."""
    raw = str(command)
    norm = _strip_tricks(raw)
    text = _sub_refs(norm, _merged_assignments(raw, norm))
    # R7 S1: expand brace lists in the canonical form too (risk scoring) —
    # after substitution so braces inside assigned values expand as well.
    braced = _expand_braces(text)
    if braced is not None:
        text = braced
    resolved = _resolve_dynamic(text)
    # R8 J1: collapse destructive-verb operands in the canonical form too, so
    # risk scoring sees the kernel-resolved path (/home/../etc -> /etc).
    segs = _raw_segments(resolved)
    if segs:
        parts: list[str] = []
        for sep, seg in segs:
            collapsed = _collapse_destructive_seg(seg)
            parts.append(sep + (collapsed if collapsed else seg))
        resolved = "".join(parts)
    return _semantic_flags(resolved)
