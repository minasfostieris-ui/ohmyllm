"""Optional network tool with explicit allowlists and SSRF-resistant defaults."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

from .spec import ToolContext, ToolResult

MAX_BYTES = 200_000
BODY_CAP = 12_000

# Well-known NAT64 prefix (RFC 6052): 64:ff9b::/96 embeds an IPv4 address in
# the low 32 bits. Python's is_global can report True for the IPv6 literal
# even when the embedded IPv4 is loopback/link-local (R2 S7).
_NAT64_PREFIX = bytes.fromhex("0064ff9b" + "00" * 8)


def _embedded_ipv4(address: ipaddress.IPv4Address | ipaddress.IPv6Address):
    """Yield every IPv4 address embedded in an IPv6 literal: ipv4_mapped,
    sixtofour, teredo (server+client), and NAT64 64:ff9b::/96 extraction."""
    if not isinstance(address, ipaddress.IPv6Address):
        return
    for attr in ("ipv4_mapped", "sixtofour"):
        embedded = getattr(address, attr)
        if embedded is not None:
            yield embedded
    teredo = address.teredo
    if teredo is not None:
        yield from teredo
    packed = address.packed
    if packed[:12] == _NAT64_PREFIX:
        yield ipaddress.IPv4Address(packed[12:])


def _host_allowed(host: str, allowlist: list[str]) -> bool:
    host = host.casefold().rstrip(".")
    for raw in allowlist:
        allowed = str(raw).strip().casefold().rstrip(".")
        if not allowed:
            continue
        if allowed.startswith("."):
            suffix = allowed[1:]
            if host == suffix or host.endswith("." + suffix):
                return True
        elif host == allowed:
            return True
    return False


def _validate_url(url: str, allowlist: list[str]) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid URL: {exc}") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("http_fetch only supports absolute http(s) URLs")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL credentials are not allowed")
    host = parsed.hostname.casefold().rstrip(".")
    if not _host_allowed(host, allowlist):
        raise ValueError(f"host is not on the explicit http_fetch allowlist: {host}")
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError(
            "DNS hostnames are denied because the current transport cannot pin "
            "the validated address; use an explicit allowlisted public IP literal"
        ) from exc
    if not address.is_global:
        raise ValueError(f"private, local, metadata, or reserved address is blocked: {address}")
    # R2 S7: an IPv6 literal may embed a non-global IPv4 (NAT64, mapped,
    # 6to4, teredo) even when the top-level address reports is_global.
    for embedded in _embedded_ipv4(address):
        if not embedded.is_global:
            raise ValueError(
                f"embedded non-global address is blocked: {address} -> {embedded}"
            )
    effective_port = port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, host, effective_port


def http_fetch(ctx: ToolContext, args: dict) -> ToolResult:
    """Fetch one allowlisted public URL without proxies or redirects."""
    import httpx

    if not ctx.config.safety.http_fetch_enabled:
        return ToolResult(False, "", "http_fetch is disabled")
    allowlist = list(ctx.config.safety.http_fetch_allowlist)
    if not allowlist:
        return ToolResult(False, "", "http_fetch requires an explicit host allowlist")
    url = str(args.get("url") or "")
    try:
        _validate_url(url, allowlist)
        timeout = max(1.0, min(float(args.get("timeout") or 20.0), 60.0))
    except (TypeError, ValueError) as exc:
        return ToolResult(False, "", str(exc))

    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            with client.stream("GET", url) as response:
                status = response.status_code
                if 300 <= status < 400:
                    return ToolResult(False, "", "redirects are blocked; allowlist the final URL explicitly")
                headers = {
                    key: value
                    for key, value in response.headers.items()
                    if key.lower() in ("content-type", "content-length", "server")
                }
                final_url = str(response.url)
                # A transport must not silently change the destination URL.
                _validate_url(final_url, allowlist)
                body = bytearray()
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if len(body) < MAX_BYTES:
                        body.extend(chunk[: MAX_BYTES - len(body)])
                    if total >= MAX_BYTES:
                        break
    except (httpx.HTTPError, ValueError) as exc:
        return ToolResult(False, "", f"fetch failed: {exc}")
    text = bytes(body).decode("utf-8", errors="replace")
    ctype = headers.get("content-type", "")
    if "html" in ctype or text.lstrip().startswith(("<!DOCTYPE", "<html")):
        text = _strip_html(text)
    shown = text[:BODY_CAP] + (
        f"\n… [{len(text) - BODY_CAP} chars truncated]" if len(text) > BODY_CAP else ""
    )
    return ToolResult(True, f"GET {final_url} → {status}\nheaders: {headers}\n\n{shown}")


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = html.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    html = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))) if int(m.group(1)) < 1000 else "?", html)
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n\s*\n+", "\n\n", html)
    return html.strip()
