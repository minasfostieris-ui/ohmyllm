"""Optional network tool (http_fetch) — gated hard by policy + approvals."""

from __future__ import annotations

import re

from .spec import ToolContext, ToolResult

MAX_BYTES = 200_000
BODY_CAP = 12_000


def http_fetch(ctx: ToolContext, args: dict) -> ToolResult:
    import httpx

    url = str(args.get("url") or "")
    if not re.match(r"^https?://", url):
        return ToolResult(False, "", "http_fetch only supports http(s) URLs")
    timeout = float(args.get("timeout") or 20.0)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as c:
            with c.stream("GET", url) as r:
                status = r.status_code
                headers = {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "content-length", "server")}
                final_url = str(r.url)
                body = b""
                total = 0
                for chunk in r.iter_bytes():
                    body += chunk
                    total += len(chunk)
                    if total >= MAX_BYTES:
                        break
    except httpx.HTTPError as exc:
        return ToolResult(False, "", f"fetch failed: {exc}")
    text = body.decode("utf-8", errors="replace")
    ctype = headers.get("content-type", "")
    if "html" in ctype or text.lstrip().startswith(("<!DOCTYPE", "<html")):
        text = _strip_html(text)
    shown = text[:BODY_CAP] + (f"\n… [{len(text) - BODY_CAP} chars truncated]" if len(text) > BODY_CAP else "")
    return ToolResult(True, f"GET {final_url} → {status}\nheaders: {headers}\n\n{shown}")


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = html.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    html = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))) if int(m.group(1)) < 1000 else "?", html)
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n\s*\n+", "\n\n", html)
    return html.strip()
