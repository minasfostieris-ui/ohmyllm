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


def _assignment_values(text: str) -> dict[str, str]:
    """Literal ``VAR=value`` assignments, LAST wins (real shell semantics)."""
    values: dict[str, str] = {}
    for m in _ASSIGN_RX.finditer(text):
        values[m.group(1)] = m.group(2).strip("'\"")
    return values


def _sub_refs(text: str, values: dict[str, str]) -> str:
    """Bounded substitution of ``$VAR``/``${VAR}`` from *values* (3 passes so
    chained indirection terminates). Unassigned references are left as-is."""
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


def _expand_vars(text: str) -> str:
    """Bounded, NON-executing expansion of literal ``VAR=value`` assignments
    into ``$VAR``/``${VAR}`` references (C2). LAST assignment wins — real
    shell semantics for sequential assignment (R2 C2: first-wins let
    ``X=echo; X=rm; $X -rf /`` masquerade as echo). At most 3 substitution
    passes so chained indirection terminates. Unassigned references are
    left as-is.
    """
    return _sub_refs(text, _assignment_values(text))


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
# sudo/doas are transparent prefixes: the real executable follows their own
# flags (R2 W5 — `sudo rm -rf $HOME` must still position-match as rm).
_PRIV_WRAPPERS = frozenset({"sudo", "doas"})
_PRIV_REQ_SHORT = frozenset("ugChpRTU")
_PRIV_REQ_LONG = frozenset({
    "--user", "--group", "--host", "--prompt", "--close-from",
    "--other-user", "--role", "--type",
})


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


# ---------------------------------------------------------------- R2 C3
# Dynamic-target resolution. The deny floor and the catastrophic gate need
# the EFFECTIVE operand, not the spelling: ${UNSET:-/}, $(echo /), backticks,
# "$@"/"$N" positionals and xargs stdin payloads all denote real targets.

def _raw_segments(text: str) -> list[tuple[str, str]]:
    """Split *text* into (preceding_separator, segment) pairs on unquoted
    ``&&``/``||``/``;``/``|``/``&``/newline. Quote-aware so payload boundaries
    (``bash -c '...'``) survive for positional-argument extraction."""
    out: list[tuple[str, str]] = []
    buf: list[str] = []
    quote: str | None = None
    sep = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if quote is not None:
            buf.append(ch)
            if ch == quote and (quote == "'" or text[i - 1:i] != "\\"):
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
    visible. Falls back to whitespace splitting on unbalanced quotes."""
    import shlex
    try:
        return shlex.split(seg, posix=False)
    except ValueError:
        return seg.split()


def _literal_output(seg: str) -> str | None:
    """The literal stdout of a pure ``echo``/``printf`` segment, or None when
    the output is not statically knowable (variables, substitutions, other
    executables). Never executes anything."""
    norm = _strip_tricks(seg)
    tokens = norm.split()
    if not tokens:
        return None
    exe = tokens[0].rsplit("/", 1)[-1]
    if exe == "echo":
        args = [t for t in tokens[1:] if t not in ("-n", "-e", "-E")]
        if any("$" in t or "`" in t for t in args):
            return None
        return " ".join(args) or None
    if exe == "printf" and len(tokens) >= 2:
        fmt, args = tokens[1], tokens[2:]
        if any("$" in t or "`" in t for t in [fmt, *args]):
            return None
        if "%" in fmt.replace("%s", ""):
            return None  # only plain %s placeholders are statically foldable
        out = fmt
        for arg in args:
            out = out.replace("%s", arg, 1)
        out = out.replace("\\n", "\n").strip()
        return out or None
    return None


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
            lit = _literal_output(segs[idx - 1][1])
            payload = _skip_wrapper_flags(
                _raw_remainder(s), req_short=_XARGS_REQ_SHORT,
                opt_short=_XARGS_OPT_SHORT, req_long=_XARGS_REQ_LONG,
            )
            if lit is not None and payload.strip():
                s = s + " " + lit
        out.append(sep + s)
    return "".join(out)


_DESTRUCTIVE_EXES = frozenset({"rm", "shred", "dd"})


def _is_find_delete(args: list[str]) -> bool:
    joined = " ".join(args)
    return "-delete" in args or bool(re.search(r"-exec(dir)?\s+rm\b", joined))


def unresolved_destructive_operands(command: str) -> bool:
    """Fail-closed input for the catastrophic gate (R2 C3): True when a
    destructive executable (rm/shred/dd/find -delete) carries an operand the
    normalizer cannot resolve — an unexpanded ``$…``/backtick after full
    resolution, or an xargs payload fed by non-literal piped stdin. Such
    commands must never be silent-allowed in full_access."""
    return _scan_unresolved(str(command), [], 0)


def _scan_unresolved(text: str, positionals: list[str], depth: int) -> bool:
    if depth > 3:
        return False
    segs = _raw_segments(text)
    pos = list(positionals)
    for _sep, seg in segs:
        stripped = [_strip_tricks(t) for t in _tokens_raw(seg)]
        if stripped and stripped[0] == "set" and "--" in stripped[1:]:
            pos = stripped[stripped.index("--") + 1:]
    # assignments may live in an EARLIER segment than the reference
    # (``X=/tmp/f; rm -rf $X``) — collect them across the whole text.
    values = _assignment_values(" ".join(_strip_tricks(seg) for _sep, seg in segs))
    for idx, (sep, seg) in enumerate(segs):
        rtoks = _tokens_raw(seg)
        if not rtoks:
            continue
        base = _strip_tricks(rtoks[0]).rsplit("/", 1)[-1]
        if base in _SHELL_WRAPPERS and "-c" in rtoks[1:3]:
            ci = rtoks.index("-c")
            if ci + 1 < len(rtoks):
                pargs = [_strip_tricks(t) for t in rtoks[ci + 2:]]
                if _scan_unresolved(rtoks[ci + 1], pargs[1:], depth + 1):
                    return True
            continue
        norm = _strip_tricks(seg)
        if base == "eval":
            if _scan_unresolved(_raw_remainder(norm), pos, depth + 1):
                return True
            continue
        if base == "env":
            payload = _skip_wrapper_flags(
                _raw_remainder(norm), req_short=_ENV_REQ_SHORT,
                req_long=_ENV_REQ_LONG, assignments=True,
            )
            if _scan_unresolved(payload, pos, depth + 1):
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
        if base == "xargs":
            payload = _skip_wrapper_flags(
                " ".join(args), req_short=_XARGS_REQ_SHORT,
                opt_short=_XARGS_OPT_SHORT, req_long=_XARGS_REQ_LONG,
            )
            ptoks = payload.split()
            if not ptoks:
                continue
            pbase = ptoks[0].rsplit("/", 1)[-1]
            destructive = pbase in _DESTRUCTIVE_EXES or (
                pbase == "find" and _is_find_delete(ptoks[1:]))
            if not destructive:
                continue
            if "|" in sep and "||" not in sep:
                upstream = segs[idx - 1][1] if idx > 0 else ""
                if _literal_output(upstream) is None:
                    return True
            if any("$" in t or "`" in t for t in ptoks[1:] if not t.startswith("-")):
                return True
            continue
        if base in _DESTRUCTIVE_EXES or (base == "find" and _is_find_delete(args)):
            operands = [t for t in args if not t.startswith("-")]
            if any("$" in t or "`" in t for t in operands):
                return True
    return False


def command_variants(command: str) -> list[str]:
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

    def walk(text: str, depth: int, positionals: list[str] | None = None) -> None:
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
                walk(expanded, depth + 1, positionals)
        resolved = _resolve_dynamic(expanded, positionals)
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
                    walk(payload, depth + 1, positionals)
        if depth < 2:
            # Quote-aware pass (R2 C3): precise `sh -c` payload boundaries
            # expose trailing positional arguments ($0, $1, …), and xargs
            # payloads gain the upstream pipe segment's literal output as
            # operands — the normalized whole-text pass above cannot see
            # either because quote removal erases the payload boundary.
            raw_segs = _raw_segments(text)
            for idx, (sep, rseg) in enumerate(raw_segs):
                rtoks = _tokens_raw(rseg)
                if not rtoks:
                    continue
                rbase = _strip_tricks(rtoks[0]).rsplit("/", 1)[-1]
                if rbase in _SHELL_WRAPPERS and "-c" in rtoks[1:3]:
                    ci = rtoks.index("-c")
                    if ci + 1 < len(rtoks):
                        pargs = [_strip_tricks(t) for t in rtoks[ci + 2:]]
                        walk(rtoks[ci + 1], depth + 1, pargs[1:])
                elif rbase == "xargs" and "|" in sep and "||" not in sep and idx > 0:
                    lit = _literal_output(raw_segs[idx - 1][1])
                    payload = _skip_wrapper_flags(
                        _raw_remainder(_strip_tricks(rseg)),
                        req_short=_XARGS_REQ_SHORT, opt_short=_XARGS_OPT_SHORT,
                        req_long=_XARGS_REQ_LONG,
                    )
                    if lit is not None and payload.strip():
                        walk(payload + " " + lit, depth + 1, positionals)

    walk(str(command), 0)
    return seen


def normalize_command(command: str) -> str:
    """Single canonical normalized form for risk scoring: deobfuscate,
    expand literal variable assignments (last-wins), resolve dynamic targets
    (defaults, positionals, literal substitutions, xargs stdin), fold
    semantic flags. Unlike :func:`command_variants` this returns ONE string
    (no bare-executable variants) so scoring cannot be inflated by stray
    tokens."""
    text = _expand_vars(_strip_tricks(str(command)))
    return _semantic_flags(_resolve_dynamic(text))
