"""soul_* memory tools — the model's only sanctioned route to soul.md."""

from __future__ import annotations

from .spec import ToolContext, ToolResult


def soul_read(ctx: ToolContext, args: dict) -> ToolResult:
    if ctx.soul is None:
        return ToolResult(False, "", "memory is unavailable")
    text = ctx.soul.read()
    limit = int(args.get("max_chars") or 20000)
    return ToolResult(True, text[:limit] + ("\n… [truncated]" if len(text) > limit else ""))


def soul_search(ctx: ToolContext, args: dict) -> ToolResult:
    if ctx.soul is None:
        return ToolResult(False, "", "memory is unavailable")
    query = str(args.get("query") or "")
    section = args.get("section") or None
    hits = ctx.soul.search(query, section=str(section) if section else None)
    if not hits:
        return ToolResult(True, f"no memories matching {query!r}")
    return ToolResult(True, "\n".join(f"[{e.section} #{e.index}] {e.text}" for e in hits[:20]))


def soul_append(ctx: ToolContext, args: dict) -> ToolResult:
    if ctx.soul is None:
        return ToolResult(False, "", "memory is unavailable")
    try:
        res = ctx.soul.append(
            str(args.get("section") or "Important Notes"),
            str(args.get("text") or ""),
            force=bool(args.get("force")),
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the model as tool error
        return ToolResult(False, "", str(exc))
    verb = "already remembered" if res["action"] == "duplicate" else "saved"
    return ToolResult(True, f"{verb} in ## {res['section']}: {res['entry']}")


def soul_upsert(ctx: ToolContext, args: dict) -> ToolResult:
    if ctx.soul is None:
        return ToolResult(False, "", "memory is unavailable")
    try:
        res = ctx.soul.upsert(
            str(args.get("section") or "Important Notes"),
            str(args.get("text") or ""),
            force=bool(args.get("force")),
        )
    except Exception as exc:  # noqa: BLE001
        return ToolResult(False, "", str(exc))
    if res["action"] == "updated":
        return ToolResult(True, f"updated existing memory in ## {res['section']} (was: {res['previous']})")
    verb = "already remembered" if res["action"] == "duplicate" else "saved"
    return ToolResult(True, f"{verb} in ## {res['section']}: {res['entry']}")


def soul_forget(ctx: ToolContext, args: dict) -> ToolResult:
    if ctx.soul is None:
        return ToolResult(False, "", "memory is unavailable")
    res = ctx.soul.forget(
        str(args.get("query") or ""),
        section=str(args["section"]) if args.get("section") else None,
        index=int(args["index"]) if args.get("index") is not None else None,
    )
    if res["action"] == "not_found":
        return ToolResult(False, "", f"no memory matched {res['query']!r} — use soul_search first")
    return ToolResult(True, f"forgotten from ## {res['section']}: {res['removed']}")
