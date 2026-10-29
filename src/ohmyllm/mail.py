"""Email transport: IMAP read/search + SMTP send, stdlib only.

Design notes
------------
* Credentials: config [mail] section (0600 file) with OMLLM_KEY_MAIL env
  override — same precedence pattern as provider keys.
* Inbound mail is UNTRUSTED: handlers fence bodies so the model treats them
  as data, never instructions (prompt injection via email is the attack).
* Sending is irreversible and an exfiltration channel: policy gates
  email_send harder than any other tool; the optional recipient allowlist
  stays enforced even in full_access.
* Drafts always work with zero setup: an RFC2822 .eml lands in
  ~/.ohmyllm/mail/outbox/ (IMAP APPEND to Drafts only when configured).
"""
from __future__ import annotations

import contextlib
import email
import imaplib
import os
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage, Message
from email.utils import formatdate, make_msgid, parsedate_to_datetime
from pathlib import Path

from . import paths
from .config.models import Config, MailSettings

ADDR_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# domain -> (imap_host, imap_port, smtp_host, smtp_port)
PROVIDERS: dict[str, tuple[str, int, str, int]] = {
    "gmail.com": ("imap.gmail.com", 993, "smtp.gmail.com", 465),
    "googlemail.com": ("imap.gmail.com", 993, "smtp.gmail.com", 465),
    "outlook.com": ("outlook.office365.com", 993, "smtp-mail.outlook.com", 587),
    "hotmail.com": ("outlook.office365.com", 993, "smtp-mail.outlook.com", 587),
    "live.com": ("outlook.office365.com", 993, "smtp-mail.outlook.com", 587),
    "microsoft.com": ("outlook.office365.com", 993, "smtp-mail.outlook.com", 587),
    "yahoo.com": ("imap.mail.yahoo.com", 993, "smtp.mail.yahoo.com", 465),
    "fastmail.com": ("imap.fastmail.com", 993, "smtp.fastmail.com", 465),
    "fastmail.fm": ("imap.fastmail.com", 993, "smtp.fastmail.com", 465),
    "icloud.com": ("imap.mail.me.com", 993, "smtp.mail.me.com", 465),
    "me.com": ("imap.mail.me.com", 993, "smtp.mail.me.com", 465),
}

ENVELOPE_CAP = 40
BODY_CAP = 12_000


def detect_provider(address: str) -> tuple[str, int, str, int] | None:
    dom = address.rsplit("@", 1)[-1].strip().lower() if "@" in address else ""
    return PROVIDERS.get(dom)


def mail_password(cfg: MailSettings) -> str:
    return os.environ.get("OMLLM_KEY_MAIL") or cfg.password


def configured(cfg: Config) -> bool:
    m = cfg.mail
    return bool(m.imap_host and m.smtp_host and m.address and (mail_password(m) or m.user))


def outbox_dir() -> Path:
    p = paths.global_home() / "mail" / "outbox"
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class Envelope:
    uid: str
    date: str
    sender: str
    recipient: str
    subject: str
    message_id: str = ""


def _imap_login(cfg: MailSettings) -> imaplib.IMAP4_SSL:
    con = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, timeout=30)
    con.login(cfg.user or cfg.address, mail_password(cfg))
    return con


def _smtp_send(cfg: MailSettings, msg: EmailMessage) -> None:
    user = cfg.user or cfg.address
    if cfg.smtp_port == 465:
        with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=30) as s:
            s.login(user, mail_password(cfg))
            s.send_message(msg, from_addr=cfg.address)
    else:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as s:
            s.ehlo()
            s.starttls(context=ssl.create_default_context())
            s.login(user, mail_password(cfg))
            s.send_message(msg, from_addr=cfg.address)


# ------------------------------------------------------------------ search
def _parse_query(query: str) -> list[str]:
    """'from:ava subject:deadline recent:14 free-text' -> IMAP SEARCH terms."""
    terms: list[str] = []
    words: list[str] = []
    for tok in (query or "").split():
        low = tok.lower()
        for pfx, key in (("from:", "FROM"), ("to:", "TO"), ("subject:", "SUBJECT")):
            if low.startswith(pfx):
                val = tok[len(pfx):].strip()
                if val:
                    terms += [key, f'"{val}"']
                break
        else:
            if low.startswith("recent:") and low[7:].isdigit():
                since = (datetime.now() - timedelta(days=int(low[7:]))).strftime("%d-%b-%Y")
                terms += ["SINCE", since]
            else:
                words.append(tok)
    if words:
        terms += ["TEXT", f'"{" ".join(words)}"']
    return terms or ["ALL"]


def search(cfg: MailSettings, query: str, limit: int = ENVELOPE_CAP) -> list[Envelope]:
    terms = _parse_query(query)
    con = _imap_login(cfg)
    try:
        con.select("INBOX", readonly=True)
        typ, data = con.uid("SEARCH", None, *terms)  # type: ignore[arg-type]
        if typ != "OK":
            return []
        uids = [u.decode() for u in (data[0].split() if data and data[0] else [])]
        uids = uids[-limit:]
        out: list[Envelope] = []
        for uid in reversed(uids):  # newest first
            typ, fdata = con.uid("FETCH", uid, "(BODY.PEEK[HEADER])")
            if typ != "OK" or not fdata or not fdata[0]:
                continue
            msg = email.message_from_bytes(_raw(fdata[0]))
            out.append(Envelope(
                uid=uid,
                date=_fmt_date(msg.get("Date")),
                sender=_one(msg.get("From")),
                recipient=_one(msg.get("To")),
                subject=(msg.get("Subject") or "(no subject)")[:200],
                message_id=(msg.get("Message-ID") or "").strip(),
            ))
        return out
    finally:
        with contextlib.suppress(Exception):
            con.logout()


def read(cfg: MailSettings, uids: list[str], max_body: int = BODY_CAP) -> list[tuple[Envelope, str]]:
    con = _imap_login(cfg)
    try:
        con.select("INBOX", readonly=True)
        out: list[tuple[Envelope, str]] = []
        for uid in uids[:10]:
            typ, fdata = con.uid("FETCH", uid, "(BODY.PEEK[])")
            if typ != "OK" or not fdata or not fdata[0]:
                continue
            msg = email.message_from_bytes(_raw(fdata[0]))
            env = Envelope(
                uid=uid, date=_fmt_date(msg.get("Date")), sender=_one(msg.get("From")),
                recipient=_one(msg.get("To")), subject=(msg.get("Subject") or "(no subject)")[:200],
                message_id=(msg.get("Message-ID") or "").strip(),
            )
            out.append((env, _body_text(msg)[:max_body]))
        return out
    finally:
        with contextlib.suppress(Exception):
            con.logout()


# ------------------------------------------------------------------ compose
def build_message(cfg: MailSettings, to: str, subject: str, body: str,
                  in_reply_to: str = "", references: str = "") -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = cfg.address
    msg["To"] = ", ".join(to.split())
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        refs = f"{references} {in_reply_to}".strip()
        msg["References"] = refs
    sig = f"\n\n-- \n{cfg.signature}" if cfg.signature else ""
    msg.set_content(body + sig)
    return msg


def save_draft(cfg: Config, msg: EmailMessage) -> Path:
    """Local outbox copy — always available, no account needed."""
    safe = re.sub(r"[^A-Za-z0-9._@-]", "_", msg["To"] or "draft")
    path = outbox_dir() / f"{datetime.now():%Y%m%d-%H%M%S}-{safe}.eml"
    path.write_bytes(bytes(msg))
    path.chmod(0o600)
    return path


def append_drafts_folder(cfg: MailSettings, msg: EmailMessage) -> bool:
    """Best-effort IMAP APPEND to the provider's Drafts folder."""
    folder = "[Gmail]/Drafts" if cfg.imap_host == "imap.gmail.com" else "Drafts"
    try:
        con = _imap_login(cfg)
        try:
            if con.select(folder, readonly=False)[0] != "OK":
                return False
            return con.append(folder, None, imaplib.Internaldate2tuple(
                con.internaldate(msg["Date"])), bytes(msg))[0] == "OK"
        finally:
            with contextlib.suppress(Exception):
                con.logout()
    except Exception:  # noqa: BLE001 — draft-in-IMAP is a nicety, never fatal
        return False


def send(cfg: MailSettings, msg: EmailMessage) -> None:
    _smtp_send(cfg, msg)


def test_login(cfg: MailSettings) -> tuple[bool, str]:
    try:
        con = _imap_login(cfg)
        typ, data = con.select("INBOX", readonly=True)
        n = int(data[0]) if typ == "OK" and data and data[0] else 0
        with contextlib.suppress(Exception):
            con.logout()
        return True, f"IMAP ok — INBOX has {n} messages"
    except Exception as exc:  # noqa: BLE001
        return False, f"IMAP login failed: {type(exc).__name__}: {exc}"


# ------------------------------------------------------------------ helpers
def _raw(fetch_item: object) -> bytes:
    """imaplib hands back tuples like (b'1 (BODY[] {123}', b'raw...')."""
    if isinstance(fetch_item, tuple) and len(fetch_item) > 1:
        return bytes(fetch_item[1])  # type: ignore[arg-type]
    return bytes(fetch_item or b"") if isinstance(fetch_item, (bytes, bytearray)) else b""


def _one(header: str | None) -> str:
    if not header:
        return ""
    return header.split(",")[0].strip()[:160]


def _fmt_date(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return parsedate_to_datetime(raw).astimezone(UTC).strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return raw[:40]


_QUOTE_CUT = re.compile(r"^(On .{5,120}wrote:|-{3,} ?Original Message|>{0,2} ?In reply to).*$", re.I)


def _body_text(msg: Message) -> str:
    """Plain-text body: text/plain preferred, HTML degraded, quotes removed."""
    part: str | None = None
    if msg.is_multipart():
        for p in msg.walk():
            if p.get_content_type() == "text/plain" and not p.get_filename():
                part = _decode(p)
                break
        if part is None:
            for p in msg.walk():
                if p.get_content_type() == "text/html" and not p.get_filename():
                    part = _html_to_text(_decode(p))
                    break
    else:
        raw = _decode(msg)
        part = _html_to_text(raw) if msg.get_content_type() == "text/html" else raw
    return _strip_quotes(part or "")


def _decode(part: Message) -> str:
    payload = part.get_payload(decode=True) or b""
    return bytes(payload).decode(part.get_content_charset() or "utf-8", errors="replace")


def _strip_quotes(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    for ln in lines:
        if ln.startswith(">") or _QUOTE_CUT.match(ln.strip()):
            break
        out.append(ln)
    body = "\n".join(out).strip()
    return body if body else text.strip()  # fully-quoted mail: show it anyway


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = html.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    html = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))) if int(m.group(1)) < 1000 else "?", html)
    html = re.sub(r"[ \t]+", " ", html)
    return re.sub(r"\n\s*\n+", "\n\n", html).strip()
