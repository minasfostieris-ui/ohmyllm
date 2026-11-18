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
# R12: DOTALL — bash ANSI-C keeps \<NL> as a literal backslash+newline pair,
# so the span body must match across newlines (\\. with re.DOTALL).
_ANSI_C_RX = re.compile(r"\$'((?:[^'\\]|\\.)*)'", re.DOTALL)
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
# R10 F12: parameter transforms (${X@Q}/@E/@P/…) and nameref declarations.
_TRANSFORM_RX = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)@([QEPAaUuLlKk])\}")
_NAMEREF_RX = re.compile(
    r"\b(?:declare|typeset|local)\s+-[A-Za-z]*n[A-Za-z]*\s+([A-Za-z_][A-Za-z0-9_]*)=([A-Za-z_][A-Za-z0-9_]*)")
# Quote splicing: '"' / "'" adjacency concatenates shell strings. The splice
# quotes are pure syntax — dropping them exposes hidden $'...' payloads (H1).


_HEX_DIGITS = "0123456789abcdefABCDEF"
_OCT_DIGITS = "01234567"


def _decode_escapes(body: str, grammar: str = "ansi") -> tuple[str, bool]:
    """R12 structural grammar (F1/F2/F3/F4, OB3): the ONE bash escape decoder,
    differential-validated against real bash (.validation/felix-r12sec/bash_gt.py,
    evidence/grammar.jsonl — 104 legs). FOUR grammars, ONE implementation, so
    the decode lanes cannot desync (>=3 prior instance patches: R8 J2, R9 E5,
    R10 F5 — the R11 checkpoint still caught %b vs FORMAT octal desync):

      "ansi"   $'...' — \\u greedy 1-4 hex / \\U greedy 1-8 (bash accepts SHORT
               forms: \\u72m IS rm), \\xH{1,2}, octal \\[0-7]{1,3} (no leading 0
               required) & 0xFF, \\cX control char, \\<NL> stays a LITERAL pair,
               unknown escapes keep the backslash (\\8, \\x, \\u{} are literal).
      "b"      printf %b — octal leading 0 OPTIONAL (\\0162 AND \\162 are r),
               \\0 + up to 3 more digits, \\x/\\u/\\U as ansi; \\c SUPPRESSES all
               remaining output (returns suppress=True).
      "format" printf FORMAT chunk — ANSI-C-style octal (\\0162 is 0x0E+'2'),
               \\x/\\u/\\U, table escapes; \\c is NOT decoded (literal).
      "echo"   echo -e — octal REQUIRES leading 0 (\\162m stays literal),
               \\x/\\u/\\U, table; \\c suppresses the rest (incl. trailing NL).

    Returns (decoded, suppress_rest). Never executes anything."""
    out: list[str] = []
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = body[i + 1]
        if nxt == "c" and grammar in ("b", "echo"):
            return "".join(out), True  # \c: stop ALL further output
        if nxt == "x":
            j = i + 2
            while j < n and j < i + 4 and body[j] in _HEX_DIGITS:
                j += 1
            if j > i + 2:
                out.append(chr(int(body[i + 2:j], 16) & 0xFF))
                i = j
                continue
            out.append("\\x")  # no hex digits: literal (bash parity)
            i += 2
            continue
        if nxt in "uU":
            width = 4 if nxt == "u" else 8
            j = i + 2
            while j < n and j < i + 2 + width and body[j] in _HEX_DIGITS:
                j += 1
            if j > i + 2:  # GREEDY 1..width digits (F1: \u72m IS rm)
                out.append(chr(int(body[i + 2:j], 16)))
                i = j
                continue
            out.append("\\" + nxt)  # \u, \u{}, \u{72}: literal with backslash
            i += 2
            continue
        if nxt in _OCT_DIGITS:
            if grammar == "echo" and nxt != "0":
                out.append("\\" + nxt)  # echo octal REQUIRES the leading 0
                i += 2
                continue
            if grammar in ("b", "echo") and nxt == "0":
                j = i + 2  # \0 then up to 3 MORE octal digits (\0162 -> r)
                while j < n and j < i + 5 and body[j] in _OCT_DIGITS:
                    j += 1
                out.append(chr(int(body[i + 1:j], 8) & 0xFF))
                i = j
                continue
            j = i + 1  # ansi/format: \[0-7]{1,3}, no leading-0 rule
            while j < n and j < i + 4 and body[j] in _OCT_DIGITS:
                j += 1
            out.append(chr(int(body[i + 1:j], 8) & 0xFF))
            i = j
            continue
        if nxt == "c" and grammar == "ansi" and i + 2 < n:
            # R5 A15-A17: \cX is the control character ord(X) & 0x1f.
            cx = body[i + 2]
            out.append("\x7f" if cx == "?" else chr(ord(cx) & 0x1F))
            i += 3
            continue
        if nxt == "\n" and grammar == "ansi":
            out.append("\\\n")  # $'\<NL>' keeps the LITERAL pair (oracle)
            i += 2
            continue
        if nxt in _ANSI_ESCAPES:
            out.append(_ANSI_ESCAPES[nxt])
            i += 2
            continue
        out.append("\\" + nxt)  # unknown: bash keeps the backslash
        i += 2
    return "".join(out), False


def _decode_escape_body(body: str) -> str:
    """ANSI-C escape-body entry — IS _decode_escapes(..., "ansi") (R12: ONE
    decoder, the grammars cannot desync). Shared by $'...' quoting, _decode_word
    spans and every payload-word decode site."""
    return _decode_escapes(body, "ansi")[0]


def _decode_ansi_c(text: str) -> str:
    """Decode ANSI-C ``$'...'`` quoting (H1). Runs before generic quote
    removal so ``$'\\t'`` becomes a real tab instead of a literal ``t``.
    R10: delegates to the ONE escape-body decoder (_decode_escape_body)."""
    return _ANSI_C_RX.sub(lambda m: _decode_escape_body(m.group(1)), text)


def _strip_continuations(text: str) -> str:
    """R10 structural grammar (F7): bash removes backslash-newline line
    continuations BEFORE tokenization — ``r\\<NL>m`` IS ``rm`` and
    ``s\\<NL>h`` IS ``sh`` (SEM6/SEM7 proofs). Inside SINGLE quotes the
    pair is literal and stays. Quote-aware so DQ backslash parity holds."""
    out: list[str] = []
    quote: str | None = None
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if quote == "'":
            if ch == "'":
                quote = None
            out.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            if text[i + 1] == "\n":
                i += 2  # continuation: removed (unquoted and inside DQ)
                continue
            out.append(text[i:i + 2])
            i += 2
            continue
        if quote is None and ch in "'\"":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if quote == '"' and ch == '"':
            quote = None
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _decode_word(tok: str) -> str:
    """R10 structural grammar (F4): the ONE payload-word parser. Decodes a
    shell word — including QUOTE CONCATENATION (``'a'\"b\"'c'`` is one word
    ``abc``; shlex.quote nests build exactly this debris) — with bash-faithful
    per-span rules: SQ literal, DQ backslash only before $ ` \" \\ NL, ANSI-C
    $'...' via the shared escape decoder, unquoted backslash pairs, and
    backslash-NL continuation removal. Each rescan layer decodes EXACTLY its
    own layer (closure): the decoded payload is re-parsed by the next layer,
    so escapes never accumulate across nesting depths."""
    out: list[str] = []
    i = 0
    n = len(tok)
    while i < n:
        ch = tok[i]
        if ch == "$" and i + 1 < n and tok[i + 1] == "'":
            j = i + 2
            body: list[str] = []
            while j < n:
                if tok[j] == "\\" and j + 1 < n:
                    body.append(tok[j:j + 2])
                    j += 2
                    continue
                if tok[j] == "'":
                    break
                body.append(tok[j])
                j += 1
            out.append(_decode_escape_body("".join(body)))
            i = j + 1
            continue
        if ch == "'":
            j = tok.find("'", i + 1)
            if j < 0:
                out.append(tok[i + 1:])
                break
            out.append(tok[i + 1:j])
            i = j + 1
            continue
        if ch == '"':
            j = i + 1
            dq: list[str] = []
            while j < n:
                if tok[j] == "\\" and j + 1 < n and tok[j + 1] in "$`\"\\\n":
                    if tok[j + 1] == "\n":
                        j += 2
                        continue
                    dq.append(tok[j + 1])
                    j += 2
                    continue
                if tok[j] == '"':
                    break
                dq.append(tok[j])
                j += 1
            out.append("".join(dq))
            i = j + 1
            continue
        if ch == "\\" and i + 1 < n:
            if tok[i + 1] == "\n":
                i += 2
                continue
            out.append(tok[i + 1])
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_tricks(text: str) -> str:
    """Remove quoting/escape/whitespace obfuscation, collapse whitespace."""
    # R10 F7: line continuations are removed BEFORE any other decode — the
    # old _ESCAPE_RX pass turned \<NL> into a real newline and SPLIT words.
    text = _strip_continuations(text)
    out = _IFS_RX.sub(" ", text)
    # R12 d4 (corpus-found): escape-bearing ANSI-C spans decode BEFORE the
    # splice fixpoint — the loop would eat a \" pair and desync the $'…'
    # closing quote (depth-4 sh -c nests). Backslash-free splice fragments
    # still merge first (R10 F7 unchanged).
    out = _ANSI_C_RX.sub(
        lambda m: _decode_escape_body(m.group(1)) if "\\" in m.group(1)
        else m.group(0), out)
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
    # R12 OB4: an escaped quote (\" / \') is literal quote CONTENT, not a
    # quoting delimiter — bash keeps r\"m\" as ONE word r"m" (never rm).
    # Sentinel it so escape removal + quote stripping cannot fabricate a
    # bare word out of quoted fragments.
    out = out.replace('\\"', "\ue000").replace("\\'", "\ue001")
    out = _ESCAPE_RX.sub(r"\1", out)
    out = out.replace("'", "").replace('"', "")
    out = out.replace("\ue000", '"').replace("\ue001", "'")
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
            val = m.group(2)
            pos = end
            # R10 F2: a command-substitution value spans whitespace
            # (X=$(cat <<< 'rm -rf /')) — keep reading words until the
            # substitution parens balance, so the FULL value is tracked.
            while _subst_paren_depth(val) > 0 and pos < len(seg):
                nxt, nend, _nclosed = _read_word(seg, pos, posix=posix)
                if nend <= pos:
                    break
                val += " " + nxt
                pos = nend
            values[m.group(1)] = val
            if not closed:
                break
    return values


def _subst_paren_depth(val: str) -> int:
    """Unbalanced ``$(``/backtick substitution depth of an assignment value
    (R10 F2): quote-aware so parens inside quotes do not count."""
    depth = 0
    quote: str | None = None
    i = 0
    while i < len(val):
        ch = val[i]
        if quote == "'":
            if ch == "'":
                quote = None
        elif ch == "\\" and quote != "'":
            i += 1
        elif quote == '"':
            if ch == '"':
                quote = None
        elif ch in "'\"":
            quote = ch
        elif val.startswith("$(", i):
            depth += 1
            i += 1
        elif ch == "`":
            depth = depth + 1 if depth == 0 else depth
        elif ch == ")" and depth > 0:
            depth -= 1
        i += 1
    return depth


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
    # R10 synthesis closure: a substitution-valued assignment spans pipe
    # separators (X=$(echo … | cat)) that _raw_segments splits — extract from
    # the WHOLE-TEXT literal-folded reading too, so the full value is tracked.
    folded = _sub_literal_subshells(raw)
    if folded != raw:
        values.update(_assignment_values_raw(folded))
        values.update(_assignment_values_raw(folded, posix=True))
    # R10 F2: printf -v / read assignment sources join the ONE value map.
    values.update(_cmd_assignments(raw))
    # R10 F12: nameref (declare -n R=X) aliases resolve to their target's
    # value — rm -rf $R with X=/ IS rm -rf /.
    for m in _NAMEREF_RX.finditer(raw):
        target = values.get(m.group(2))
        if target is not None:
            values[m.group(1)] = target
    return values


def _scope_values(raw: str) -> dict[str, str]:
    """R10: OUTER-scope values for the -c/eval payload pre-substitution —
    quote-aware RAW extraction only (both shell interpretations, the folded
    reading, command-assignment sources and namerefs). The legacy
    deobfuscated pass of _merged_assignments would read assignments that
    live INSIDE the quoted payload (truncated at the first space after quote
    removal) and inject them back into it, destroying the payload's own
    quoting — the R7 H1 POSIX-parity family must keep its interior intact."""
    values = _assignment_values_raw(raw)
    values.update(_assignment_values_raw(raw, posix=True))
    folded = _sub_literal_subshells(raw)
    if folded != raw:
        values.update(_assignment_values_raw(folded))
        values.update(_assignment_values_raw(folded, posix=True))
    values.update(_cmd_assignments(raw))
    for m in _NAMEREF_RX.finditer(raw):
        target = values.get(m.group(2))
        if target is not None:
            values[m.group(1)] = target
    return values


def _sub_refs(text: str, values: dict[str, str]) -> str:
    """Bounded substitution of ``$VAR``/``${VAR}`` from *values* (3 passes so
    chained indirection terminates). Unassigned references are left as-is.
    R7 S3: a custom non-empty IFS splits UNQUOTED substituted values at
    runtime (``IFS=,; X=rm,-rf,/; $X`` runs ``rm -rf /``) — translate the
    assigned IFS characters to spaces inside substituted values so the
    downstream gates see the real words. Empty IFS means NO splitting.

    R10 F12: ``${VAR@OP}`` parameter transforms are part of the SAME
    substitution grammar. @Q quotes the value — a multi-word value stays
    ONE quoted word (R13: masked whitespace inside the SQ wrap, never a
    fabricated ``rm -rf /`` — X1 superseded by X1b). @E/@P
    substitute the decoded value (eval re-parses it), @U/@u/@L/@l case-fold,
    other ops substitute the value as-is."""
    if not values:
        return text
    # R12 OB2: IFS unset (None) → default whitespace splitting (downstream
    # tokenizers do it); IFS explicitly EMPTY ("") → NO splitting at all —
    # `$X` stays ONE word. The old `or ""` conflated the two.
    ifs_raw = values.get("IFS")
    no_split = ifs_raw == ""
    split_chars = [ch for ch in (ifs_raw or "") if not ch.isspace()]

    def _xform(m: re.Match[str]) -> str:
        name, op = m.group(1), m.group(2)
        val = values.get(name)
        if val is None:
            return m.group(0)
        if op == "Q":
            if re.search(r"\s", val):
                # R13 LOW: multi-word @Q is ONE SQ-quoted word — bash never
                # re-splits it (GT rc=127 ENOENT: 'shred /dev/sda' is one
                # nonexistent command name). Mask the whitespace INSIDE the
                # quote so no lane can fabricate verb+operands; single-word
                # @Q + eval reparse (X1b) keeps the deny.
                masked = re.sub(r"\s", "\ue002", val)
                return "'" + masked.replace("'", "'\\''") + "'"
            return "'" + val.replace("'", "'\\''") + "'"
        if op in ("E", "P"):
            return _decode_shell_escapes(val)
        if op == "U":
            return val.upper()
        if op == "u":
            return val[:1].upper() + val[1:]
        if op == "L":
            return val.lower()
        if op == "l":
            return val[:1].lower() + val[1:]
        return val

    def _sub(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2)
        val = values.get(name)
        if val is None:
            return m.group(0)
        if no_split:
            # Empty IFS: mask whitespace with a sentinel so downstream
            # tokenizers cannot re-split the ONE word (path consumers
            # restore it to a space).
            return re.sub(r"\s", "\ue002", val)
        if split_chars and not any(ch.isspace() for ch in (ifs_raw or "")) \
                and not any(ch in val for ch in split_chars):
            # R13 LOW: non-empty IFS with NO IFS whitespace and no delimiter
            # present in the value → bash performs NO split: ONE word (GT
            # rc=127 ENOENT). Mask whitespace so downstream tokenizers
            # cannot fabricate a destructive command line. IFS containing
            # whitespace keeps the legacy split (bash splits on it too).
            return re.sub(r"\s", "\ue002", val)
        for ch in split_chars:
            val = val.replace(ch, " ")
        return val

    out = text
    for _ in range(3):
        nxt = _TRANSFORM_RX.sub(_xform, out)
        nxt = _VAR_REF_RX.sub(_sub, nxt)
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


def _cmd_assignments(text: str) -> dict[str, str]:
    """R10 F2 synthesis pillar: assignment sources that are COMMANDS, not
    ``VAR=value`` words — ``printf -v VAR fmt args`` (the formatted output
    IS the value) and ``read [-r] VAR… <<< content`` (one variable gets the
    whole line; several split it on IFS whitespace). Feeds the ONE merged
    value map so ``eval "$X"`` sees the laundered payload."""
    values: dict[str, str] = {}
    for _sep, seg in _raw_segments(text):
        toks = _tokens_raw(seg)
        if not toks:
            continue
        base = _strip_tricks(toks[0]).rsplit("/", 1)[-1]
        if base == "printf" and len(toks) >= 4 and toks[1] == "-v":
            var = _decode_word(toks[2])
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", var):
                val = _literal_output("printf " + " ".join(toks[3:]))
                if val is not None:
                    values[var] = val
        elif base == "read" and "<<<" in toks:
            hi = toks.index("<<<")
            content = " ".join(_decode_word(t) for t in toks[hi + 1:]).strip()
            names = [t for t in toks[1:hi] if not t.startswith("-")]
            if not names or not content:
                continue
            if len(names) == 1:
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", names[0]):
                    values[names[0]] = content
            else:
                words = content.split()
                for k, nm in enumerate(names):
                    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", nm):
                        values[nm] = words[k] if k < len(words) else ""
    return values


def procsub_interiors(text: str) -> list[str]:
    """Interior commands of process substitutions ``>(…)``/``<(…)`` (R5
    A20-A21), quote-aware with balanced-paren scanning. ``$((…))`` arithmetic
    is NOT a procsub (the preceding ``$`` disqualifies it). Interiors are
    real command boundaries: their writers and verbs must be gated.
    R10: the ONE procsub grammar lives here (normalize) — policy re-exports
    it so the redirect scan, the variant walk and the scorer cannot desync."""
    out: list[str] = []
    quote: str | None = None
    escaped = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if quote == "'":
            if ch == "'":
                quote = None
            i += 1
            continue
        if ch == "\\":
            escaped = True
            i += 1
            continue
        if quote == '"':
            if ch == '"':
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            i += 1
            continue
        if ch in "<>" and i + 1 < n and text[i + 1] == "(" \
                and (i == 0 or text[i - 1] != "$"):
            depth = 1
            j = i + 2
            q2: str | None = None
            esc2 = False
            while j < n and depth:
                c2 = text[j]
                if esc2:
                    esc2 = False
                elif q2 == "'":
                    if c2 == "'":
                        q2 = None
                elif c2 == "\\":
                    esc2 = True
                elif q2 == '"':
                    if c2 == '"':
                        q2 = None
                elif c2 in "'\"":
                    q2 = c2
                elif c2 == "(":
                    depth += 1
                elif c2 == ")":
                    depth -= 1
                j += 1
            inner = text[i + 2:j - 1] if depth == 0 else text[i + 2:]
            if inner.strip():
                out.append(inner)
            i = j
            continue
        i += 1
    return out


def _quote_mask(text: str) -> str:
    """Blank the CONTENT of quoted spans (length-preserving) so text-level
    synthesis extractors (coproc/function bodies) never match inside an
    echo string — ``echo "f(){ rm -rf /; }"`` is inert data, not a definition."""
    out: list[str] = []
    quote: str | None = None
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if quote == "'":
            out.append(" " if ch != "'" else ch)
            if ch == "'":
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append("  " if quote else text[i:i + 2])
            i += 2
            continue
        if quote == '"':
            out.append(" " if ch != '"' else ch)
            if ch == '"':
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
        out.append(ch)
        i += 1
    return "".join(out)


_COPROC_BRACE_RX = re.compile(r"\bcoproc\s+(?:[A-Za-z_]\w*\s+)?\{([^}]*)\}", re.DOTALL)
_FUNC_PAREN_RX = re.compile(r"\b([A-Za-z_]\w*)\s*\(\s*\)\s*\{([^}]*)\}", re.DOTALL)
_FUNC_KW_RX = re.compile(r"\bfunction\s+([A-Za-z_]\w*)\s*(?:\(\s*\)\s*)?\{([^}]*)\}", re.DOTALL)


def _coproc_bodies(text: str) -> list[str]:
    """R10 F1 synthesis pillar: coproc bodies execute asynchronously — both
    the brace form (``coproc [NAME] { …; }``) and the plain form
    (``coproc cmd args`` — a NAME is only legal with a brace compound).
    Quote-masked so echo strings stay inert; the mask is LENGTH-PRESERVING,
    so bodies are extracted from the ORIGINAL text via match spans (the
    masked copy would blank a quoted payload like ``bash -c 'rm -rf /'``)."""
    masked = _quote_mask(text)
    out: list[str] = []
    for m in _COPROC_BRACE_RX.finditer(masked):
        body = text[m.start(1):m.end(1)].strip()
        if body:
            out.append(body)
        masked = masked.replace(m.group(0), " " * len(m.group(0)), 1)
    for m in re.finditer(r"\bcoproc\s+([^;|&\n]+)", masked):
        cmd = text[m.start(1):m.end(1)].strip()
        if cmd:
            out.append(cmd)
    return out


def _function_bodies(text: str) -> dict[str, str]:
    """R10 F12 synthesis pillar: shell function definitions (``f(){ …; }``,
    ``f () { …; }``, ``function f { …; }``) — the body is the payload that
    runs when the name is called. Quote-masked; braces span segments, so
    extraction is text-level, not segment-level. Bodies come from the
    ORIGINAL text via match spans — the length-preserving mask only locates
    unquoted braces, it must not blank the payload's own quoting."""
    masked = _quote_mask(text)
    bodies: dict[str, str] = {}
    for rx in (_FUNC_PAREN_RX, _FUNC_KW_RX):
        for m in rx.finditer(masked):
            body = text[m.start(2):m.end(2)].strip()
            if body:
                bodies[m.group(1)] = body
    return bodies


_READ_SUBST_RX = re.compile(r"\$\(<\s*([^()<>|]+?)\s*\)")


def _written_files(segs: list[tuple[str, str]]) -> dict[str, str]:
    """R10 F11 synthesis pillar: path → literal content for files WRITTEN by
    earlier segments — echo/printf redirects (``>``/``>>``) and tee pipe
    targets. The content is the producer's static stdout with the redirect
    tokens removed; append accumulates. Consumed by source/./sh FILE and
    ``$(<file)`` read-substitution folding."""
    written: dict[str, str] = {}
    for idx, (sep, seg) in enumerate(segs):
        toks = _tokens_raw(seg)
        if not toks:
            continue
        ri: int | None = None
        for k, t in enumerate(toks):
            if t in (">", ">>", ">|"):
                ri = k
                break
        if ri is not None and ri + 1 < len(toks):
            target = _decode_word(toks[ri + 1])
            content = _literal_output(" ".join(toks[:ri]))
            if content and target and not target.startswith("&"):
                if toks[ri] == ">>" and target in written:
                    written[target] = written[target] + "\n" + content
                else:
                    written[target] = content
            continue
        stripped = [_strip_tricks(t) for t in toks]
        i = 0
        while i < len(stripped) and _ASSIGN_TOK_RX.match(stripped[i]):
            i += 1
        if i < len(stripped) and stripped[i].rsplit("/", 1)[-1] == "tee" \
                and "|" in sep and "||" not in sep and idx > 0:
            content = _pipe_literal(segs, idx)
            if content:
                for t in stripped[i + 1:]:
                    if not t.startswith("-"):
                        written[t] = content
    return written


def _fold_read_subst(text: str, written: dict[str, str]) -> str:
    """R10 F11: ``$(<file)`` is bash's read-substitution — the file's content
    in operand position. Fold it when the content was tracked as written;
    unknown files stay untouched (the fail-closed lane owns them)."""
    if not written or "$(<" not in text:
        return text
    return _READ_SUBST_RX.sub(
        lambda m: written.get(m.group(1).strip(), m.group(0)), text)


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
    m = _ANSI_C_RX.fullmatch(tok)
    if m:
        # R12 d4: bash hands the wrapper the DECODED string — the quote-aware
        # variant walk must reach the innermost payload of deep ANSI-C nests.
        return _decode_escape_body(m.group(1))
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
_PRIV_WRAPPERS = frozenset({"sudo", "doas", "pkexec", "su", "runuser"})
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
    # R10 F9/M1/M2: session/namespace/pty wrappers run the payload unchanged.
    "setsid", "parallel", "systemd-run", "chroot", "nsenter", "script",
})
_TRANSPARENT_REQ_SHORT: dict[str, frozenset[str]] = {
    "exec": frozenset("a"),  # R6 E2: exec -a NAME sets argv[0], then runs NAME's command
    "nice": frozenset("n"),
    "timeout": frozenset("ks"),
    "stdbuf": frozenset("ioe"),
    "ionice": frozenset("cn"),
    "nsenter": frozenset("t"),  # R10 M1: -t PID targets the namespace
}
_TRANSPARENT_REQ_LONG: dict[str, frozenset[str]] = {
    "nice": frozenset({"--adjustment"}),
    "timeout": frozenset({"--signal", "--kill-after"}),
    "stdbuf": frozenset({"--input", "--output", "--error"}),
    "ionice": frozenset({"--class", "--classdata"}),
    "nsenter": frozenset({"--target"}),
}
# R10 M2: script -c/--command runs a NESTED command string (pty wrapper).
_TRANSPARENT_NESTED_SHORT: dict[str, frozenset[str]] = {"script": frozenset("c")}
_TRANSPARENT_NESTED_LONG: dict[str, frozenset[str]] = {"script": frozenset({"--command"})}
# R10 M3: at/batch execute their STDIN as a deferred job — stdin-exec
# synthesis applies unconditionally (time words are not script operands).
_DEFERRED_EXEC = frozenset({"at", "batch"})
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
        if ch == "$" and i + 1 < len(text) and text[i + 1] == "'":
            # R12 (F1 values): $'...' ANSI-C span — backslash escapes the
            # next char (\' does NOT close), the body is decoded with the
            # ONE shared ANSI-C decoder, and the decoded value (spaces
            # included) is part of THIS word, bash-faithful.
            j = i + 2
            body: list[str] = []
            while j < len(text):
                c = text[j]
                if c == "\\" and j + 1 < len(text):
                    body.append(c)
                    body.append(text[j + 1])
                    j += 2
                    continue
                if c == "'":
                    break
                body.append(c)
                j += 1
            buf.append(_decode_escape_body("".join(body)))
            if j >= len(text):
                # Unclosed $' — fail closed like any other unclosed quote.
                return "".join(buf), len(text), False
            i = j + 1
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
    and callers must fail closed rather than guess.

    R12 OB1: ``$'...'`` ANSI-C spans are their OWN quoting context — inside
    them ``\\'`` is an escaped quote, NOT a closer (bash accepts
    ``sh -c $'echo it\\\\\\'s fine'`` as valid, rc=0). A plain single-quote
    state scan desynced on every escaped inner quote and failed closed on
    VALID payloads (DENY r10 all modes). The ``$`` opener only counts when
    unescaped (even backslash run before it)."""
    quote: str | None = None
    ansi = False
    escaped = False
    i = 0
    while i < len(text):
        ch = text[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if quote == "'":
            if ansi and ch == "\\":
                i += 2
                continue
            if ch == "'":
                quote = None
                ansi = False
            i += 1
            continue
        if ch == "\\":
            escaped = True
            i += 1
            continue
        if quote == '"':
            if ch == '"':
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            if ch == "'" and i > 0 and text[i - 1] == "$":
                j = i - 2
                n = 0
                while j >= 0 and text[j] == "\\":
                    n += 1
                    j -= 1
                ansi = n % 2 == 0
                # R13 LOW: $$ is the COMPLETE PID parameter — a following
                # '…' is a PLAIN SQ span (bash: eval $$'rm -rf /' is valid
                # PID-concat, GT rc=127). Only the desync shape (inner \')
                # then fails closed; valid $$ shapes stay balanced. The
                # first $ must itself be unescaped (\$$'…' IS ANSI-C).
                if ansi and j >= 0 and text[j] == "$":
                    k = j - 1
                    n2 = 0
                    while k >= 0 and text[k] == "\\":
                        n2 += 1
                        k -= 1
                    if n2 % 2 == 0:
                        ansi = False
            i += 1
            continue
        i += 1
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


def _skip_priv_flags(payload: str, base: str = "") -> str:
    """R10 M1: ONE privilege-wrapper flag grammar. sudo/doas/pkexec/runuser
    flags are consumed normally; su additionally takes an optional USER
    positional before its -c NESTED command (``su root -c 'rm -rf /'``)."""
    p = _skip_wrapper_flags(
        payload, req_short=_PRIV_REQ_SHORT, req_long=_PRIV_REQ_LONG,
        nested_short=frozenset("c") if base == "su" else frozenset())
    if base != "su":
        return p
    tok, end, _closed = _read_word(p)
    if tok and not tok.startswith("-"):
        rest = p[end:]
        nxt = _skip_wrapper_flags(
            rest, req_short=_PRIV_REQ_SHORT, req_long=_PRIV_REQ_LONG,
            nested_short=frozenset("c"))
        if nxt != rest:
            return nxt
    return p


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
            payload = _skip_priv_flags(rest, base)
        elif base == "env":
            payload = _skip_env_flags(rest)
        elif base in _TRANSPARENT_WRAPPERS:
            payload = _skip_wrapper_flags(
                rest,
                req_short=_TRANSPARENT_REQ_SHORT.get(base, frozenset()),
                req_long=_TRANSPARENT_REQ_LONG.get(base, frozenset()),
                nested_short=_TRANSPARENT_NESTED_SHORT.get(base, frozenset()),
                nested_long=_TRANSPARENT_NESTED_LONG.get(base, frozenset()),
            )
            if base == "timeout":
                dur, dend, _dc = _read_word(payload)
                if dur and _TIMEOUT_DURATION_RX.fullmatch(dur):
                    payload = payload[dend:]
            elif base == "chroot":
                # R10 M1: chroot NEWROOT cmd — consume the ONE positional
                # root operand (like timeout's duration) so the payload shows.
                _root, rend, _rc = _read_word(payload)
                if payload:
                    payload = payload[rend:]
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
    text = _strip_continuations(text)  # R10 F7: continuations join words
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
    seg = _strip_continuations(seg)  # R10 F7: continuations join words
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
_STDIN_SHELLS = _SHELL_WRAPPERS | {"ash", "source", "."}


def _shell_reads_stdin(rest: list[str]) -> bool:
    """True when a shell's own arguments leave it reading commands from
    stdin (R8 J2/J3): no -c (exact or short cluster — the inline payload
    ignores stdin) and no script-file operand (the script ignores stdin).
    Long --flags, bare - and -s/-i/… keep stdin reading. R12 F6: an explicit
    /dev/stdin (or /dev/fd/0) script operand IS stdin — ``source /dev/stdin``
    and ``sh /dev/stdin`` execute the feeding content, not a regular file."""
    for t in rest:
        if t in ("-", "/dev/stdin", "/dev/fd/0") or t.startswith("--"):
            continue
        if t.startswith("-"):
            if "c" in t[1:]:
                return False
            continue
        return False
    return True


def _decode_shell_escapes(text: str) -> str:
    """Compatibility entry (R8 J2 / R9 E5 legacy callers, ${X@E}/@P transforms):
    R12 delegates to the ONE decoder — bash ${X@E} expands with ANSI-C
    semantics, so the "ansi" grammar IS the faithful read. The old independent
    \\nnn/\\xHH scanner was a SECOND decoder and desynced from %b (Vera F2,
    Terry T-NEW1); it no longer exists."""
    return _decode_escapes(text, "ansi")[0]


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


_PRINTF_INT_RX = re.compile(r"[+-]?(?:0[xX][0-9a-fA-F]+|0[0-7]+|[0-9]+)")
_PRINTF_REUSE_BUDGET = 64


def _printf_int(arg: str) -> int | None:
    """bash printf integer-argument parse (decimal, 0x hex, 0 octal), or None
    when not statically an integer."""
    a = arg.strip()
    if not _PRINTF_INT_RX.fullmatch(a):
        return None
    try:
        if re.match(r"[+-]?0[xX]", a):
            return int(a, 16)
        if re.match(r"[+-]?0[0-7]", a):
            return int(a, 8)
        return int(a, 10)
    except ValueError:
        return None


def _printf_pad(s: str, width: int, left: bool, zero: bool) -> str:
    if width <= len(s):
        return s
    pad = ("0" if zero and not left else " ") * (width - len(s))
    return s + pad if left else pad + s


def _printf_pass(
    fmt: str, args: list[str], ai: int,
) -> tuple[str | None, int, bool, bool]:
    """ONE pass of the printf format scanner (R12 F2/F3/F4 structural rewrite,
    differential-validated against bash_gt printf/reuse legs). Returns
    (chunk | None-if-not-foldable, next_arg_index, suppressed, has_directives).
    Literal chunks decode through the "format" grammar ONLY (never the whole
    result — that second decode layer was the F2 desync); %s arguments stay
    RAW; %b arguments decode through the "b" grammar and \\c suppresses ALL
    remaining output incl. format reuse; width/precision/flags/* are bash
    mechanics; %% is literal; %(date)T and float directives are not foldable."""
    out: list[str] = []
    lit: list[str] = []
    has_dir = False
    i = 0
    n = len(fmt)

    def flush() -> None:
        if lit:
            out.append(_decode_escapes("".join(lit), "format")[0])
            lit.clear()

    while i < n:
        ch = fmt[i]
        if ch != "%":
            lit.append(ch)
            i += 1
            continue
        j = i + 1
        if j < n and fmt[j] == "(":
            return None, ai, False, True  # %(date)T: nondeterministic
        flags = ""
        while j < n and fmt[j] in "-+ #0":
            flags += fmt[j]
            j += 1
        width = ""
        star_w = False
        if j < n and fmt[j] == "*":
            star_w = True
            j += 1
        else:
            while j < n and fmt[j].isdigit():
                width += fmt[j]
                j += 1
        prec = ""
        star_p = False
        has_prec = False
        if j < n and fmt[j] == ".":
            has_prec = True
            j += 1
            if j < n and fmt[j] == "*":
                star_p = True
                j += 1
            else:
                while j < n and fmt[j].isdigit():
                    prec += fmt[j]
                    j += 1
        if j >= n:
            lit.append(fmt[i:])  # incomplete trailing directive: literal
            break
        d = fmt[j]
        if d == "%":
            flush()
            out.append("%")
            i = j + 1
            continue
        if d not in "diouxXfeEgGcsbq":
            return None, ai, False, True
        flush()
        has_dir = True
        w = 0
        if star_w:
            wv = _printf_int(args[ai]) if ai < len(args) else 0
            if wv is None:
                return None, ai, False, True
            w = wv
            ai += 1
        elif width:
            w = int(width)
        p = -1
        if has_prec:
            if star_p:
                pv = _printf_int(args[ai]) if ai < len(args) else 0
                if pv is None:
                    return None, ai, False, True
                p = pv
                ai += 1
            else:
                p = int(prec) if prec else 0
        arg = args[ai] if ai < len(args) else ""
        ai += 1
        if d in "feEgG":
            return None, ai, False, True  # floats: not statically foldable
        if d == "s":
            s = arg if p < 0 else arg[:p]
            out.append(_printf_pad(s, w, "-" in flags, False))
        elif d == "b":
            dec, sup = _decode_escapes(arg, "b")
            if p >= 0:
                dec = dec[:p]
            out.append(_printf_pad(dec, w, "-" in flags, False))
            if sup:
                return "".join(out), ai, True, True
        elif d == "q":
            if arg == "":
                s = "''"
            elif any(ord(c) < 0x20 for c in arg):
                return None, ai, False, True  # bash $'...' quoting: not foldable
            else:
                s = "".join(
                    c if (c.isalnum() or c in "_./-:") else "\\" + c
                    for c in arg)
            out.append(_printf_pad(s, w, "-" in flags, False))
        else:  # d i o u x X
            v = _printf_int(arg)
            if v is None:
                return None, ai, False, True
            digits = format(abs(v), {"o": "o", "x": "x", "X": "X"}.get(d, "d"))
            if p >= 0:
                digits = digits.rjust(p, "0")
            s = ("-" if v < 0 else "") + digits
            out.append(_printf_pad(s, w, "-" in flags, "0" in flags and p < 0))
        i = j + 1
    flush()
    return "".join(out), ai, False, has_dir


def _printf_expand(fmt: str, args: list[str]) -> str | None:
    """bash printf mechanics (R12 F3): the format is REUSED until the args are
    exhausted; a literal-only format (no arg-consuming directive, incl. %%)
    prints ONCE and ignores args; no args means ONE pass (%s -> \"\"); \\c in a
    %b argument stops ALL further output. Bounded reuse budget; None when not
    statically foldable. End-stripped per the _literal_output contract."""
    parts: list[str] = []
    ai = 0
    for _ in range(_PRINTF_REUSE_BUDGET):
        chunk, ai, sup, has_dir = _printf_pass(fmt, args, ai)
        if chunk is None:
            return None
        parts.append(chunk)
        if sup or not has_dir or ai >= len(args):
            break
    else:
        return None  # reuse budget exhausted: not statically foldable
    return "".join(parts).strip() or None


def _literal_output(seg: str) -> str | None:
    """The literal stdout of a pure ``echo``/``printf`` segment, or None when
    the output is not statically knowable (variables, substitutions, other
    executables). Never executes anything.

    R10 structural grammar: word decoding goes through the ONE payload-word
    parser (_decode_word — quote concatenation, ANSI-C spans, bash-faithful
    DQ rules). echo flags are scanned LAST-WINS (F6: bash reads -e/-E in
    order; the last one wins, clusters like -neE count per character).
    printf is a real directive scanner (F5): %% literal, %s raw argument,
    %b decodes the argument through the shared escape decoder, any other
    directive is not statically foldable. ``printf -v VAR`` writes a
    variable, not stdout → None (F2 assignment lane handles it)."""
    norm = _strip_tricks(seg)
    tokens = norm.split()
    if not tokens:
        return None
    exe = tokens[0].rsplit("/", 1)[-1]
    if exe == "echo":
        raw = _tokens_raw(seg)
        decode = False
        k = 1
        while k < len(raw):
            t = raw[k]
            if t == "--":
                k += 1
                break
            if re.fullmatch(r"-[neE]+", t):
                for c in t[1:]:
                    if c == "e":
                        decode = True
                    elif c == "E":
                        decode = False
                k += 1
                continue
            break
        rargs = [_decode_word(t) for t in raw[k:]]
        if any("$" in t or "`" in t for t in rargs):
            return None
        if decode:
            # R12 T-NEW1/OB3: the ECHO grammar (octal REQUIRES leading 0,
            # \c suppresses the rest) — NOT the ANSI-C/format decoder.
            parts: list[str] = []
            for a in rargs:
                dec, sup = _decode_escapes(a, "echo")
                parts.append(dec)
                if sup:
                    break
            out = " ".join(parts)
        else:
            out = " ".join(rargs)
        return out.strip() or None
    if exe == "printf":
        raw = _tokens_raw(seg)
        if len(raw) >= 2 and raw[1] in ("-v", "--"):
            if raw[1] == "-v":
                return None  # -v writes a variable — no stdout payload
            raw = raw[:1] + raw[2:]
        if len(raw) < 2:
            return None
        fmt = _decode_word(raw[1])
        args = [_decode_word(t) for t in raw[2:]]
        if any("$" in t or "`" in t for t in [fmt, *args]):
            return None
        # R12 F2/F3/F4: ONE bash-faithful scanner (reuse, width/precision,
        # %b grammar + \c suppression, format-chunk decode, %s args RAW).
        return _printf_expand(fmt, args)
    return None


# R9 structural rescan (task #130): E1-E5+B11 are ONE family — synthesized
# stdin/literal-output payloads escaping re-analysis. Every synthesis site
# routes through _rescan_payload (ONE entry point, bounded depth). These
# helpers compute what a pipe stage's stdin/stdout statically IS.
_RESCAN_MAX_DEPTH = 6


def _herestring_content(seg: str) -> str | None:
    """The decoded content of a ``<<<`` herestring, or None. R10 F4: each
    word goes through the ONE payload-word parser — exactly ONE decode
    layer per rescan, so escapes never accumulate across nesting depths."""
    toks = _tokens_raw(seg)
    if "<<<" not in toks:
        return None
    hi = toks.index("<<<")
    return " ".join(_decode_word(t) for t in toks[hi + 1:])


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
        # R12 F8: /dev/null contributes nothing — cat /dev/null - still
        # forwards stdin. cat /dev/null ALONE outputs empty (not stdin).
        if not args:
            return True
        return all(a.startswith("-") or a == "/dev/null" for a in args) \
            and any(a == "-" or a.startswith("-") for a in args)
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
        # R12 F8 tier1: an identity substitution s/X/X/ (any delimiter,
        # optional g/i/I/p flags) forwards content unchanged.
        if args == [""]:
            return True
        if len(args) == 1:
            m = re.fullmatch(r"s(.)(.*?)\1(.*?)\1[gIpimM]*", args[0])
            return m is not None and m.group(2) == m.group(3)
        return False
    if base == "awk":
        return len(args) == 1 and args[0].replace(" ", "") in (
            "1", "{print}", "{print$0}")
    if base in ("grep", "egrep", "fgrep"):
        # R12 F8 tier1: a match-all pattern with no file operands forwards
        # every non-empty line — empty lines are shell no-ops, so the command
        # content reaching the consumer is unchanged.
        return len(args) == 1 and args[0].replace(" ", "") in (
            ".", "^", ".*", "^.*")
    return False


def _is_forwarder(seg: str) -> bool:
    """R12 F8 tier2: True when a pipe stage forwards its stdin to stdout,
    possibly TRANSFORMED (tr/sed/grep/awk) or mixed with file content
    (cat - f). The remote-source rule must cross these hops — curl's payload
    still reaches the consumer shell. tier1 (unchanged forwarding) is a
    subset. File-only stages (stdout is the FILE, not stdin), in-place sed
    (stdout empty) and executors (sh) are NOT forwarders."""
    if _is_passthrough(seg):
        return True
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
    if base == "tr":
        return True  # tr only ever reads stdin
    if base == "cat":
        return "-" in args or "/dev/stdin" in args
    if base in ("grep", "egrep", "fgrep", "sed", "awk"):
        if base == "sed" and any(
                a == "-i" or a.startswith("-i") or a.startswith("--in-place")
                for a in args):
            return False
        # Skip the program/pattern operand (first non-flag, or the -e/-f
        # value); any FURTHER non-flag operand is a FILE — stdout is then
        # the file content, not the forwarded stdin.
        k = 0
        seen_prog = False
        while k < len(args):
            a = args[k]
            if a.startswith("-"):
                if a in ("-e", "-f", "--expression", "--file", "--regexp"):
                    k += 1
                    seen_prog = True
                k += 1
                continue
            if not seen_prog:
                seen_prog = True
                k += 1
                continue
            return False  # file operand
        return seen_prog
    return False


def _pipe_producer(segs: list[tuple[str, str]], idx: int,
                   *, forwarders: bool = False) -> str | None:
    """The segment whose stdout reaches pipe stage *idx* (R9 E1): walk back
    over passthrough stages. R10 F3: the walk is FULL and linear (O(n),
    examines j=0) — the old 6-iteration cap made the effective budget 5 and
    let ``echo … | cat×7 | sh`` evade synthesis entirely. A passthrough fed
    by a herestring IS the producer (its content is its stdout). Hop count
    alone NEVER fails a command closed; only the rescan DEPTH budget does,
    and only for destructive shapes.

    R12 F8: ``forwarders=True`` walks over tier2 forwarders too (tr/sed/grep/
    awk transformations, cat - f) — used by the REMOTE-source rule, where a
    transformed curl payload still reaches the consumer shell."""
    check = _is_forwarder if forwarders else _is_passthrough
    j = idx - 1
    while j >= 0:
        sep_j, seg_j = segs[j]
        if not check(seg_j):
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


def _static_output(segs: list[tuple[str, str]], idx: int) -> str | None:
    """R10 synthesis pillar: the statically-known stdout of segment *idx*
    regardless of shape — herestring-fed passthrough, upstream pipe chain,
    or a plain echo/printf. ONE reader for every synthesis consumer (procsub
    interiors, $(<file), write-then-exec, coproc bodies)."""
    seg = segs[idx][1]
    if _is_passthrough(seg):
        hs = _herestring_content(seg)
        if hs and hs.strip():
            return hs.strip()
    if idx > 0 and "|" in segs[idx][0] and "||" not in segs[idx][0]:
        return _pipe_literal(segs, idx)
    if _is_passthrough(seg):
        return None  # bare passthrough: stdout IS stdin, not a literal
    return _literal_output(seg)


def _pipe_remote_source(segs: list[tuple[str, str]], idx: int) -> bool:
    """True when the producer feeding pipe stage *idx* is curl/wget (R9:
    the curl|sh invariant must hold across intermediate passthrough pipes —
    risk rule 68's ``[^|;]*`` cannot cross them). R12 F8: the walk crosses
    tier2 FORWARDERS too (identity sed, grep ., tr -d, awk 1, cat /dev/null -
    and any transformed-stdin hop) — the remote payload still reaches sh."""
    prod = _pipe_producer(segs, idx, forwarders=True)
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


def _sub_literal_subshells(text: str, written: dict[str, str] | None = None) -> str:
    """Substitute ``$()``/backtick interiors whose output is statically
    literal into operand position (R2 C3). R10 synthesis pillar: the interior
    is read through _static_output — herestring-fed passthroughs and FULL
    pipe chains fold too (``$(cat <<< 'rm -rf /')``, ``$(echo … | cat)``),
    and ``$(<file)`` folds from the written-file map (F11). Non-literal
    interiors are left untouched for the fail-closed scan."""

    def _sub(m: re.Match[str]) -> str:
        inner = m.group(1) if m.group(1) is not None else (m.group(2) or "")
        lit: str | None = None
        if written and inner.lstrip().startswith("<"):
            path = inner.lstrip()[1:].strip()
            if path in written:
                lit = written[path]
            else:
                return m.group(0)
        # R12 F5: $(cat FILE) is the file-fed operand chain — a single-file
        # cat with no flags reads exactly the tracked written content.
        if lit is None and written:
            itoks = inner.split()
            if len(itoks) == 2 and itoks[0].rsplit("/", 1)[-1] == "cat" \
                    and not itoks[1].startswith("-"):
                path = _decode_word(itoks[1])
                if path in written:
                    lit = written[path]
        if lit is None:
            isegs = _raw_segments(inner)
            if isegs:
                lit = _static_output(isegs, len(isegs) - 1)
            if lit is None:
                lit = _literal_output(inner)
        if lit is None:
            return m.group(0)
        # Assignment values are NOT word-split by the shell — a multi-word
        # literal keeps its word integrity (X=$(echo 'rm -rf /') assigns ONE
        # string; unquoted folding would truncate the tracked value).
        if re.search(r"\s", lit) and re.search(
                r"[A-Za-z_][A-Za-z0-9_]*=$", m.string[:m.start()]):
            return "'" + lit.replace("'", "'\\''") + "'"
        return lit

    return _SUBST_RX.sub(_sub, text)


def _resolve_dynamic(text: str, positionals: list[str] | None = None) -> str:
    """Resolve dynamic targets on already deobfuscated text, preserving
    segment separators: ``set --`` positionals, ``$@``/``$N``, default-value
    braces, literal command substitution, and xargs stdin synthesis (the
    upstream pipe segment's literal output becomes payload operands)."""
    segs = _raw_segments(text)
    if not segs:
        return text
    # R10 F11: fold $(<file) read-substitutions from tracked written content
    # BEFORE any downstream synthesis reads the text.
    written = _written_files(segs)
    folded = _fold_read_subst(text, written)
    if folded != text:
        text = folded
        segs = _raw_segments(text)
    # R10 synthesis closure: _raw_segments is NOT paren-aware — a pipe inside
    # $() splits into bogus segments and the per-seg fold below never sees the
    # whole interior. Pre-fold literal substitutions on the WHOLE text first.
    folded = _sub_literal_subshells(text, written)
    if folded != text:
        text = folded
        segs = _raw_segments(text)
    # R10 F12: function definitions in scope — a call executes the body.
    funcs = _function_bodies(text)
    pos = list(positionals or [])
    for _sep, seg in segs:
        toks = seg.split()
        if toks and toks[0] == "set" and "--" in toks[1:]:
            pos = toks[toks.index("--") + 1:]
    out: list[str] = []
    for idx, (sep, seg) in enumerate(segs):
        s = _expand_positionals(seg, pos)
        s = _fold_defaults(s)
        s = _sub_literal_subshells(s, written)
        toks = s.split()
        if funcs and toks and toks[0].rsplit("/", 1)[-1] in funcs:
            # R10 F12: the function body IS the executed command (positional
            # args of the call are not threaded — the body's own words are
            # the payload the gates must see).
            out.append(sep + funcs[toks[0].rsplit("/", 1)[-1]])
            continue
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
                    # R10 F10: -0/--null splits the producer on NUL, not
                    # whitespace — printf '/\0' denotes the operand /.
                    flags_region = remainder[:len(remainder) - len(payload)]
                    if any(t == "-0" or t == "--null" for t in flags_region.split()):
                        s = s + " " + " ".join(p for p in lit.split("\x00") if p)
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
            if pbase in _STDIN_SHELLS or pbase == "eval" or pbase in _DEFERRED_EXEC \
                    or pbase in ("source", "."):
                rest = ptoks[1:]
                hs: str | None = None
                if "<<<" in rest:
                    hi = rest.index("<<<")
                    # R10 grammar closure: the herestring content IS the
                    # command line — decode it through the ONE word parser
                    # (a raw token join keeps a quote layer that downstream
                    # word reading truncates to the first fragment).
                    hs = _herestring_content(s)
                    rest = rest[:hi]
                reads = True if pbase in _DEFERRED_EXEC else _shell_reads_stdin(rest)
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
                elif "-c" not in rest[:2] and (
                        pbase in _SHELL_WRAPPERS or pbase in ("source", ".")):
                    # R10 F11: script-file operand — when an earlier segment
                    # WROTE the file, its content IS the executed command
                    # (write-then-exec). Unknown files stay untouched.
                    files = [t for t in rest if not t.startswith("-")]
                    if files:
                        content = written.get(_decode_word(files[0]))
                        if content and content.strip():
                            head = s[:len(s) - len(payload)] if s.endswith(payload) else ""
                            s = head + content
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


def _collapse_destructive_seg(seg: str, cwd: str | None = None,
                              *, cd_changed: bool = False) -> str | None:
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
    verb = ptoks[0].rsplit("/", 1)[-1].lower()
    changed = False
    out: list[str] = []
    for tok in ptoks[1:]:
        if (tok.startswith("-") or "=" in tok
                or "$" in tok or "`" in tok or "{" in tok
                or not (_DOT_COMPONENT_RX.search(tok)
                        or tok == "~+" or tok.startswith("~+/"))):
            out.append(tok)
            continue
        if tok.startswith("/"):
            new = posixpath.normpath(tok)
        elif tok == "~+" or tok.startswith("~+/"):
            # R10 F8: ~+ IS $PWD — resolve against the effective cwd (the
            # tracked cd state). Unresolvable without a cwd: left untouched.
            if not cwd or not cwd.startswith("/"):
                out.append(tok)
                continue
            new = posixpath.normpath(cwd + tok[2:])
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
        elif cwd and cwd.startswith("/") and (
                _DOTDOT_COMPONENT_RX.search(tok)
                or (cd_changed and verb in ("rm", "shred", "dd", "wipefs")
                    and not tok.startswith(("/", "~")))):
            # R10 F8: after a cd changed the effective cwd, rm/shred/dd/wipefs
            # relative operands resolve against it (cd /etc; rm -rf . IS
            # rm -rf /etc). find/chmod/chown keep their approvable relative
            # spelling even after cd (Z13 over-block guard). Without a cd,
            # workspace-relative cleanup keeps its legacy form.
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


def _update_cd_state(seg: str, vdir: str | None, cdpath: str,
                     changed: bool) -> tuple[str | None, str, bool]:
    """R10 F8: track the effective cwd across segments — ``cd DIR`` (with
    CDPATH search for relative dirs) updates it; CDPATH assignments update
    the search path. Returns (vdir, cdpath, changed).

    R12 F7: assignment prefixes (``CDPATH=/ cd etc``) and the ``--`` flag
    terminator (``cd -- /etc``) are part of the SAME cd grammar; cd's own
    -L/-P/-e/-@ flags are skipped, bare ``-`` (OLDPWD) stays untracked."""
    toks = seg.split()
    for t in toks:
        m = re.match(r"CDPATH=(.*)", t)
        if m:
            cdpath = m.group(1).strip("'\"")
    i = 0
    while i < len(toks) and _ASSIGN_TOK_RX.match(toks[i]):
        i += 1
    if i < len(toks) and toks[i] == "cd":
        k = i + 1
        while k < len(toks) and toks[k].startswith("-") \
                and toks[k] not in ("--", "-"):
            k += 1
        if k < len(toks) and toks[k] == "--":
            k += 1
        if k >= len(toks):
            # R13 LOW: bare cd / cd -- (flags only) lands HOME (bash GT).
            # Track the landing as ~ — relative collapse stays inert for ~
            # (workspace-relative cleanup keeps its approvable form).
            return "~", cdpath, True
        if k < len(toks) and not toks[k].startswith("-"):
            target = toks[k]
            if target.startswith("/"):
                return posixpath.normpath(target), cdpath, True
            if cdpath:
                for entry in cdpath.split(":"):
                    cand = posixpath.normpath(posixpath.join(entry or ".", target))
                    if cand.startswith("/"):
                        return cand, cdpath, True
            if vdir and vdir.startswith("/"):
                return posixpath.normpath(posixpath.join(vdir, target)), cdpath, True
    return vdir, cdpath, changed


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


def _catastrophic_shape_seg(seg: str) -> bool:
    """R10 budget pillar: True when a segment is a CATASTROPHIC destructive
    shape — a destructive verb at command position carrying a root-equivalent
    or unresolvable operand. Workspace-relative destructive cleanup
    (``find . -delete``, ``rm -rf ./build``) is NOT catastrophic: it keeps
    its approvable standing contract even at depth exhaustion (H8/Z13).
    _strip_tricks first: an exhaustion payload may still carry ONE quote
    layer (herestring content joined from raw tokens) — the verb must be
    read through the SAME word parser, not a naive split."""
    toks = _strip_tricks(seg).split()
    i = 0
    while i < len(toks) and _ASSIGN_TOK_RX.match(toks[i]):
        i += 1
    payload = _strip_transparent_prefixes(" ".join(toks[i:]))
    ptoks = payload.split()
    if not ptoks:
        return False
    head = ptoks[0]
    if "{" in head:
        return True  # brace-built verb: the command itself is unknowable
    base = head.rsplit("/", 1)[-1].lower()
    args = ptoks[1:]
    if base == "find":
        if not _is_find_delete(args):
            return False
        # find's PATH operands precede the expression; only absolute or
        # upward-traversal roots make a -delete catastrophic.
        for a in args:
            if a.startswith("-"):
                break
            if a.startswith(("/", "~")) or _DOTDOT_COMPONENT_RX.search(a):
                return True
            if _unresolvable_operand(a):
                return True
        return False
    if base not in _DESTRUCTIVE_EXES:
        return False
    return any(_RM_ROOT_OPERAND_RX.match(a) or _unresolvable_operand(a)
               for a in args)


def _destructive_shape(payload: str, *, posix: bool = False) -> bool:
    """R9 depth-exhaustion backstop, R10 refinement: True when a CATASTROPHIC
    destructive shape (root-equivalent/unresolvable operand, brace-built verb,
    absolute/upward find -delete) sits at COMMAND position in the payload or
    its one-pass dynamic resolution (synthesized stdin included).
    Non-destructive commands AND workspace-relative destructive cleanup NEVER
    fail closed — the budget is destructive-only AND catastrophe-only."""
    for text in (payload, _resolve_dynamic(payload)):
        for _sep, seg in _raw_segments(text, posix=posix):
            if _catastrophic_shape_seg(seg):
                return True
    return False


def _exec_payloads(text: str, written: dict[str, str] | None = None,
                   *, posix: bool = False) -> list[str]:
    """R12 F5 structural fix: the ONE shared exec-site enumerator. Every site
    whose content is EXECUTED as code — coproc bodies, function bodies,
    procsub interiors (+ exec-consumer static stdout), sh -c / eval payloads,
    stdin-shell feeding content, write-then-source file content — resolved
    through the SAME operand-resolution pass (written-file fold + scope
    values + _sub_refs + literal-subshell fold). All three lanes (decision
    _scan_unresolved, exposure command_variants, scorer normalize_command)
    consume THIS list, so a site can never be wired in one lane and unwired
    in another (the F5 class). Never executes anything."""
    segs = _raw_segments(text, posix=posix)
    raw_segs = segs
    if written is None:
        written = _written_files(segs)
    folded = _sub_literal_subshells(_fold_read_subst(text, written), written)
    if folded != text:
        text = folded
        segs = _raw_segments(text, posix=posix)
    values = _scope_values(text)
    out: list[str] = []

    def add(p: str) -> None:
        p = p.strip()
        if p and p not in out:
            out.append(p)

    def resolve(s: str) -> str:
        s = _sub_refs(s, values)
        return _sub_literal_subshells(_fold_read_subst(s, written), written)

    # R13 HIGH (exec site 9): substitution-stdout EXEC — when the FIRST
    # word of a segment IS a command substitution, its static stdout is
    # the command line the shell executes (`printf '%b' '\0162m -rf /'`
    # runs rm -rf /). Read from the RAW segments: the pre-fold above only
    # folds LITERAL subshells, and the lead lanes' _strip_tricks destroys
    # the escape spellings BEFORE any fold — the decoded stdout must come
    # from the ONE static reader (_static_output) on the raw interior.
    # Match at the SEGMENT head (_tokens_raw does not quote backtick
    # words); the substitution must BE the whole first word.
    # ONE wiring: all 3 lanes consume this list (decision/exposure/scorer).
    for _sep9, rseg9 in raw_segs:
        s9 = rseg9.lstrip()
        m9 = _SUBST_RX.match(s9)
        if m9 is None:
            continue
        rest9 = s9[m9.end():]
        if rest9 and not rest9[0].isspace():
            continue
        inner9 = m9.group(1) if m9.group(1) is not None else m9.group(2)
        isegs9 = _raw_segments(inner9)
        if not isegs9:
            continue
        lit9 = _static_output(isegs9, len(isegs9) - 1)
        if lit9 and lit9.strip():
            add(resolve(lit9))

    for body in _coproc_bodies(text):
        add(resolve(body))
    for body in _function_bodies(text).values():
        add(resolve(body))
    for idx, (sep, seg) in enumerate(segs):
        rtoks = _tokens_raw(seg)
        if not rtoks:
            continue
        base = _strip_tricks(rtoks[0]).rsplit("/", 1)[-1]
        for inner in procsub_interiors(seg):
            add(resolve(inner))
            if base in _STDIN_SHELLS or base in ("eval", "source", "."):
                isegs = _raw_segments(inner)
                if isegs:
                    lit = _static_output(isegs, len(isegs) - 1)
                    if lit and lit.strip():
                        add(resolve(lit))
                    else:
                        # R13 MED (L02): the interior stdout is not fully
                        # static, but an UNRESOLVED read-subst source
                        # ($(cat f)/$(<f)) inside the resolved interior is
                        # unresolved BY CONSTRUCTION — emit it so the gated
                        # floor sees it (tracked files already folded).
                        for mm in _UNRES_EXEC_SRC_RX.finditer(resolve(inner)):
                            add(mm.group(0))
        if base in _SHELL_WRAPPERS and "-c" in rtoks[1:3]:
            ci = rtoks.index("-c")
            if ci + 1 < len(rtoks):
                add(resolve(_unquote_outer(rtoks[ci + 1])))
            continue
        if base == "eval":
            add(resolve(_unquote_outer(_raw_remainder(seg))))
            continue
        if base in ("source", ".") or (
                base in _SHELL_WRAPPERS
                and not _shell_reads_stdin(rtoks[1:])):
            rest0 = rtoks[1:]
            if base in _SHELL_WRAPPERS:
                rest0 = [t for t in rest0 if not t.startswith("-")]
            if rest0:
                content = written.get(_decode_word(rest0[0]))
                if content and content.strip():
                    add(resolve(content))
        if base in _STDIN_SHELLS or base in _DEFERRED_EXEC:
            rest = rtoks[1:]
            hs = _herestring_content(seg)
            if "<<<" in rest:
                rest = rest[:rest.index("<<<")]
            if base in _DEFERRED_EXEC or _shell_reads_stdin(rest):
                content = hs if hs and hs.strip() else None
                if content is None and "|" in sep and "||" not in sep and idx > 0:
                    content = _pipe_literal(segs, idx)
                if content and content.strip():
                    add(resolve(content))
    return out


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


# R13 MED (L02/L20): an exec payload that IS a read-subst of an UNTRACKED
# file — single cat, no flags, or $(<file) — is unresolved BY CONSTRUCTION
# (tracked sources fold to their content during resolve). Flagged cat and
# multi-command interiors are OUTSIDE the boundary.
_UNRES_EXEC_SRC_RX = re.compile(
    r"\$\((?:cat\s+[^()<>|;&\s]+|<\s*[^()<>|;&\s]+)\s*\)")


def unresolved_exec_read_source(command: str) -> bool:
    """R13 MED decision (DESTRUCTIVE_UNRESOLVABLE, GATED): True when an
    exec-site payload is an unresolved read-subst source. The policy floor
    (SB/DA risk >= 9) consumes this; full_access keeps its silent-ALLOW
    exemption — a scorer floor would flip catastrophic=score>=9 and break
    the FA contract. Non-exec consumers (echo \"$X\") never fire."""
    for p in _exec_payloads(str(command)):
        if _UNRES_EXEC_SRC_RX.fullmatch(p.strip()):
            return True
    return False


def _scan_unresolved(text: str, positionals: list[str], depth: int,
                     *, posix: bool = False, stdin: str | None = None) -> bool:
    if depth > _RESCAN_MAX_DEPTH:
        return _destructive_shape(text, posix=posix)
    segs = _raw_segments(text, posix=posix)
    # R10 F11: written-file content folds into $(<file) before the scan.
    written = _written_files(segs)
    folded = _fold_read_subst(text, written)
    if folded != text:
        text = folded
        segs = _raw_segments(text, posix=posix)
    # R10 synthesis closure: whole-text literal-substitution pre-fold (see
    # _resolve_dynamic — pipes inside $() survive only before segment split).
    folded = _sub_literal_subshells(text, written)
    if folded != text:
        text = folded
        segs = _raw_segments(text, posix=posix)
    pos = list(positionals)
    # R12 F5: payloads the context-carrying per-segment branches already
    # rescanned — the shared-pass loop below skips them (no double recursion).
    handled: set[str] = set()
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
                cp = _sub_refs(_unquote_outer(rtoks[ci + 1]), _scope_values(text))
                handled.add(cp.strip())
                if _rescan_payload(cp, pargs[1:],
                                   depth + 1, posix=posix, stdin=feeding):
                    return True
            continue
        norm = _strip_tricks(seg)
        if base == "eval":
            # R7 gate-fix (#119): recurse on the RAW remainder (outer quotes
            # stripped) so the interior assignment quoting survives.
            # R10 F2/F12: outer-scope values (X=…; eval "$X"/"${X@Q}") are
            # substituted FIRST — the rescan payload alone loses the scope.
            ep = _sub_refs(_unquote_outer(_raw_remainder(seg)), _scope_values(text))
            handled.add(ep.strip())
            if _rescan_payload(ep, pos,
                               depth + 1, posix=posix, stdin=feeding):
                return True
            continue
        if base == "env":
            payload = _skip_env_flags(_raw_remainder(norm))
            handled.add(payload.strip())
            if _rescan_payload(payload, pos, depth + 1, posix=posix, stdin=feeding):
                return True
            continue
        norm = _expand_vars(norm)
        norm = _sub_refs(norm, values)
        norm = _expand_positionals(norm, pos)
        norm = _fold_defaults(norm)
        norm = _sub_literal_subshells(norm, written)
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
            # sudo/doas/pkexec/su/runuser are transparent: scan the real
            # executable's operands (R10 M1: ONE priv flag grammar).
            payload = _skip_priv_flags(" ".join(args), base.lower())
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
        if base in _STDIN_SHELLS or base in _DEFERRED_EXEC:
            # R9 E2/E4: a stdin-reading shell EXECUTES its feeding content —
            # rescan it as a fresh command line (the meta-gap fix). curl/wget
            # producers fail closed across passthrough hops (curl|sh invariant
            # extended; rule 68 cannot cross intermediate pipes).
            # R10 M3: at/batch ALWAYS execute their stdin (deferred job).
            sargs = args[:args.index("<<<")] if "<<<" in args else args
            if base in _DEFERRED_EXEC or _shell_reads_stdin(sargs):
                if feeding and feeding.strip():
                    handled.add(feeding.strip())
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
                # R10 F10: -0/--null splits on NUL instead of whitespace.
                flags_region = argtext[:len(argtext) - len(payload)]
                if any(t == "-0" or t == "--null" for t in flags_region.split()):
                    ptoks = ptoks + [p for p in lit.split("\x00") if p]
                else:
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
    # R12 F5: the ONE shared exec-site enumerator closes the lane unwiring —
    # coproc/function/procsub/exec-consumer/write-then-exec payloads are
    # rescanned as fresh command lines exactly like the -c/eval sites above.
    for p in _exec_payloads(text, written, posix=posix):
        if p not in handled and _rescan_payload(p, pos, depth + 1, posix=posix):
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
    # R12 F5: the shared exec-site pass walks each distinct resolved payload
    # ONCE per command_variants call (dedup across the whole walk tree).
    payload_seen: set[str] = set()

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
        # R10 synthesis pillar: coproc bodies (F1) and function definitions
        # (F12) execute their bodies as code — walk them as command input.
        # Quote-masked extraction keeps echo strings inert.
        if depth < _RESCAN_MAX_DEPTH:
            for body in _coproc_bodies(text):
                walk(body, depth + 1)
            for body in _function_bodies(text).values():
                walk(body, depth + 1)
        segments = [s.strip() for s in _SPLIT_RX.split(norm) if s.strip()]
        # R7 gate-fix (#119): the EXPANDED form's segments must surface at
        # every depth — wrapper recursion (depth >= 1) does not re-walk the
        # expanded text, so a resolved payload segment (Alex H1 inside
        # sh -c '…': …; rm -rf /) would otherwise stay hidden from the
        # anchored deny globs and the catastrophic gate.
        if braced != norm:
            segments += [s.strip() for s in _SPLIT_RX.split(braced) if s.strip()]
        # R10 F8: track the effective cwd across segments (cd/CDPATH) so
        # ~+ and post-cd relative operands collapse against the REAL cwd.
        vdir: str | None = cwd
        cdpath = ""
        cd_changed = False
        for seg in segments:
            add(seg)
            add(_compact(seg))
            folded = _semantic_flags(seg)
            if folded != seg:
                add(folded)
                add(_compact(folded))
            vdir, cdpath, cd_changed = _update_cd_state(seg, vdir, cdpath, cd_changed)
            # R8 J1: kernel-resolution collapse of destructive operands —
            # /home/../etc IS /etc, so the collapsed spelling must reach the
            # deny floor and the catastrophic gate too (both flag forms).
            for base_seg in (seg, folded):
                collapsed = _collapse_destructive_seg(base_seg, vdir,
                                                      cd_changed=cd_changed)
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
                payload = _sub_refs(_raw_remainder(seg),
                                    _merged_assignments(text, norm))
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
            written = _written_files(raw_segs)
            for idx, (sep, rseg) in enumerate(raw_segs):
                rtoks = _tokens_raw(rseg)
                if not rtoks:
                    continue
                rbase = _strip_tricks(rtoks[0]).rsplit("/", 1)[-1]
                feeding = _seg_feeding(raw_segs, idx, sep, rseg, stdin)
                # R10 F1: procsub interiors EXECUTE regardless of consumer —
                # every interior is a real command boundary (ONE grammar).
                for inner in procsub_interiors(rseg):
                    walk(inner, depth + 1)
                    # An exec consumer (shell/eval/source/dot) runs the
                    # procsub's static stdout AS CODE — synthesize it through
                    # the ONE output reader (herestring/pipe/echo/printf).
                    if rbase in _STDIN_SHELLS or rbase in ("eval", "source", "."):
                        isegs = _raw_segments(inner)
                        if isegs:
                            lit = _static_output(isegs, len(isegs) - 1)
                            if lit and lit.strip():
                                walk(lit, depth + 1)
                # R10 F11: source/./sh FILE executes the file's content —
                # when an earlier segment WROTE it, the payload is known.
                if rbase in ("source", ".") or (
                        rbase in _SHELL_WRAPPERS
                        and not _shell_reads_stdin(rtoks[1:])):
                    rest0 = rtoks[1:]
                    if rbase in _SHELL_WRAPPERS:
                        rest0 = [t for t in rest0 if not t.startswith("-")]
                    if rest0:
                        script = _decode_word(rest0[0])
                        content = written.get(script)
                        if content and content.strip():
                            walk(content, depth + 1)
                if rbase in _SHELL_WRAPPERS and "-c" in rtoks[1:3]:
                    ci = rtoks.index("-c")
                    if ci + 1 < len(rtoks):
                        pargs = [_strip_tricks(t) for t in rtoks[ci + 2:]]
                        walk(_sub_refs(_unquote_outer(rtoks[ci + 1]),
                                       _scope_values(text)),
                             depth + 1, pargs[1:], stdin=feeding)
                elif rbase == "eval":
                    # R7 gate-fix (#119): raw eval payload keeps its interior
                    # quoting for the assignment extractor (Alex H1 family).
                    # R10 F2/F12: outer-scope values substitute first.
                    payload = _sub_refs(_unquote_outer(_raw_remainder(rseg)),
                                        _scope_values(text))
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
                                # R10 F10: -0/--null splits on NUL, not space.
                                flags_region = remainder[:len(remainder) - len(payload)]
                                if any(t == "-0" or t == "--null"
                                       for t in flags_region.split()):
                                    lit = " ".join(p for p in lit.split("\x00") if p)
                                walk(payload + " " + lit, depth + 1, positionals)
                    else:
                        walk(payload, depth + 1, positionals)
                elif rbase in _STDIN_SHELLS or rbase in _DEFERRED_EXEC:
                    # R8 J2/J3 + R9 E1/E2/E4: quote-aware raw pass — herestring
                    # content, literal piped stdin (through passthrough chains)
                    # and INHERITED stdin are EXECUTED by the shell; walk them
                    # as command input so a quoted multi-command payload
                    # (sh <<< "rm -rf /; echo done") exposes its inner segments
                    # to the deny floor and the catastrophic gate.
                    # R10 F4: herestring content goes through the ONE word
                    # parser (exactly ONE decode layer per rescan — closure).
                    # R10 M3: at/batch always execute their stdin.
                    rest = rtoks[1:]
                    hs_raw = _herestring_content(rseg)
                    if "<<<" in rest:
                        rest = rest[:rest.index("<<<")]
                    if rbase in _DEFERRED_EXEC or _shell_reads_stdin(rest):
                        content = hs_raw if hs_raw and hs_raw.strip() else None
                        if content is None and "|" in sep and "||" not in sep and idx > 0:
                            content = _pipe_literal(raw_segs, idx)
                        if content is None:
                            content = stdin
                        if content and content.strip():
                            # R10 F11: synthesized stdin carries the OUTER
                            # written-file context — $(<file) folds here too.
                            content = _sub_literal_subshells(
                                _fold_read_subst(content, written), written)
                            walk(content, depth + 1, positionals)
                elif rbase == "env":
                    # R6 E3: quote-aware raw pass — the -S nested command keeps
                    # its word boundaries (-S'rm -rf /' is ONE quoted value).
                    payload = _skip_env_flags(_raw_remainder(rseg))
                    if payload.strip():
                        walk(payload, depth + 1, positionals, stdin=feeding)
                elif rbase in _PRIV_WRAPPERS:
                    # R4-S1 + R10 M1: privilege wrappers are transparent —
                    # walk the real payload from the RAW remainder through
                    # the ONE priv flag grammar (su USER -c nested included).
                    payload = _skip_priv_flags(_raw_remainder(rseg), rbase.lower())
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
        # R12 F5: the ONE shared exec-site enumerator — resolved coproc/
        # function/procsub/exec-consumer/-c/eval/stdin/write-then-exec payloads
        # reach the deny floor even when the raw walks above only see debris.
        if depth < _RESCAN_MAX_DEPTH:
            for p in _exec_payloads(text):
                if p not in payload_seen:
                    payload_seen.add(p)
                    walk(p, depth + 1, positionals)

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
    # R12 F5: the scorer consumes the SAME shared exec-site enumerator —
    # coproc/function/procsub/exec-consumer payloads are scored as code, so
    # a destructive site cannot hide behind an unwired lane.
    payloads = _exec_payloads(raw)
    if payloads:
        # "; " join: the scorer's segment split ([;&|]) and _positional_verbs
        # must see each payload as its OWN command segment (a \n join would
        # glue it to the previous segment's verb and hide the rm head).
        # R12: payloads are deobfuscated with the SAME _strip_tricks pass as
        # the lead text — a RAW append would leave \cJ/\x2f spellings intact
        # (bash re-parses the payload, so its escapes decode exactly like the
        # canonical lane) and the segment sub-score would miss the verb.
        resolved = resolved + "; " + "; ".join(_strip_tricks(p) for p in payloads)
    # R8 J1: collapse destructive-verb operands in the canonical form too, so
    # risk scoring sees the kernel-resolved path (/home/../etc -> /etc).
    segs = _raw_segments(resolved)
    if segs:
        parts: list[str] = []
        vdir: str | None = None
        cdpath = ""
        cd_changed = False
        for sep, seg in segs:
            vdir, cdpath, cd_changed = _update_cd_state(seg, vdir, cdpath, cd_changed)
            collapsed = _collapse_destructive_seg(seg, vdir, cd_changed=cd_changed)
            parts.append(sep + (collapsed if collapsed else seg))
        resolved = "".join(parts)
    return _semantic_flags(resolved)
