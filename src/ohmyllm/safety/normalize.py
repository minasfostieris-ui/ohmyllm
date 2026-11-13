"""Command deobfuscation helpers for the ``safety.deny`` floor (D3).

The deny floor must see through the cheap tricks models (and attackers) use
to sneak a forbidden command past a glob list: quoting, backslash escapes,
``${IFS}`` whitespace, ``&&``/``||``/``;``/pipe/single-``&`` (background)
chaining, one level of ``$()``, backtick or bare ``(...)`` subshell interiors,
and ``sh -c`` / ``bash -c`` / ``eval`` / ``env`` / ``xargs`` wrappers.

This module is deliberately *over*-inclusive: it produces every plausible
normalized variant of a command so the policy engine can fnmatch each one.
It never executes anything and has no side effects.
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


def _strip_tricks(text: str) -> str:
    """Remove quoting/escape/whitespace obfuscation, collapse whitespace."""
    out = _IFS_RX.sub(" ", text)
    out = _ESCAPE_RX.sub(r"\1", out)
    out = out.replace("'", "").replace('"', "")
    return _WS_RX.sub(" ", out).strip()


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
        if depth < 1:
            for match in _SUBSHELL_RX.finditer(norm):
                inner = match.group(1) or match.group(2) or match.group(3) or ""
                if inner.strip():
                    walk(inner, depth + 1)
        segments = [s.strip() for s in _SPLIT_RX.split(norm) if s.strip()]
        for seg in segments:
            add(seg)
            add(_compact(seg))
            tokens = seg.split()
            if not tokens:
                continue
            exe = tokens[0]
            add(exe)
            base = exe.rsplit("/", 1)[-1]
            add(base)
            if depth >= 1:
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
