"""Where deal notifications come from, and where clarification requests go back to.

GmailIMAP polls INBOX by UID for UNSEEN messages whose subject matches the configured
pattern, using an App Password over IMAP. Messages are NOT marked seen at fetch time;
the orchestrator calls `mark_processed` only after it has finished handling the
message, so a crash mid-way leaves the email unseen and it is retried on the next
poll. A processed-ID file provides a second guard so a message can never trigger a
second phone call even if someone marks it unread.

In production this becomes Gmail push notifications via Pub/Sub, which changes the
transport, not the agent logic: the agent only ever sees `InboundEmail`.
"""
from __future__ import annotations
import email
import imaplib
import os
import re
import smtplib
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import EmailMessage
from html import unescape
from typing import Iterable, Optional


@dataclass
class InboundEmail:
    message_id: str
    subject: str
    body: str
    sender: str
    to: str = ""
    uid: str = ""
    auto_submitted: bool = False


class EmailSource:
    name = "base"

    def fetch_new(self) -> Iterable[InboundEmail]:
        raise NotImplementedError

    def mark_processed(self, msg: InboundEmail) -> None:
        raise NotImplementedError

    def send(self, to: str, subject: str, body: str, in_reply_to: Optional[str] = None) -> None:
        raise NotImplementedError


class MockEmailSource(EmailSource):
    name = "mock"

    def __init__(self, queue: Optional[list[InboundEmail]] = None):
        self.queue = list(queue or [])
        self.sent: list[dict] = []
        self.processed: list[str] = []

    def fetch_new(self):
        items, self.queue = self.queue, []
        return items

    def mark_processed(self, msg):
        self.processed.append(msg.message_id)

    def send(self, to, subject, body, in_reply_to=None):
        self.sent.append({"to": to, "subject": subject, "body": body, "in_reply_to": in_reply_to})


class ProcessedIds:
    """Durable set of Message-IDs already handled. Small, append-only, survives restarts."""
    def __init__(self, path: str):
        self.path = path
        self._ids: set[str] = set()
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self._ids = {l.strip() for l in f if l.strip()}

    def __contains__(self, mid: str) -> bool:
        return mid in self._ids

    def add(self, mid: str) -> None:
        if mid in self._ids:
            return
        self._ids.add(mid)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(mid + "\n")


_SAFE_SEARCH = re.compile(r"[^A-Za-z0-9 :\-_.]")


class GmailIMAP(EmailSource):
    name = "gmail_imap"

    def __init__(self, address: str, app_password: str, subject_pattern: str, processed_ids_path: str = "processed_message_ids.txt"):
        self.address, self.app_password = address, app_password
        self.subject_pattern = _SAFE_SEARCH.sub("", subject_pattern)   # IMAP SEARCH is a command string; keep it plain ASCII
        self.processed = ProcessedIds(processed_ids_path)

    def _connect(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
        conn.login(self.address, self.app_password)
        conn.select("INBOX")
        return conn

    def fetch_new(self):
        conn = self._connect()
        try:
            typ, data = conn.uid("search", None, "UNSEEN", "SUBJECT", f'"{self.subject_pattern}"')
            if typ != "OK":
                return []
            out = []
            for uid in data[0].split():
                typ, msg_data = conn.uid("fetch", uid, "(RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                msg = email.message_from_bytes(msg_data[0][1])
                mid = (msg.get("Message-ID") or f"<uid-{uid.decode()}@imap>").strip()
                if mid in self.processed:
                    conn.uid("store", uid, "+FLAGS", "\\Seen")
                    continue
                out.append(InboundEmail(
                    message_id=mid,
                    subject=str(make_header(decode_header(msg.get("Subject", "")))),
                    body=_plain_body(msg),
                    sender=msg.get("From", ""),
                    to=msg.get("To", ""),
                    uid=uid.decode(),
                    auto_submitted=_is_auto(msg),
                ))
            return out
        finally:
            _quiet_logout(conn)

    def mark_processed(self, msg):
        self.processed.add(msg.message_id)
        if not msg.uid:
            return
        conn = self._connect()
        try:
            conn.uid("store", msg.uid.encode(), "+FLAGS", "\\Seen")
        finally:
            _quiet_logout(conn)

    def send(self, to, subject, body, in_reply_to=None):
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = self.address, to, subject
        if in_reply_to:
            m["In-Reply-To"], m["References"] = in_reply_to, in_reply_to
        m["Auto-Submitted"] = "auto-replied"        # so other automations do not answer our answer
        m.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(self.address, self.app_password)
            s.send_message(m)


def _is_auto(msg) -> bool:
    return (msg.get("Auto-Submitted", "no").lower() != "no"
            or msg.get("Precedence", "").lower() in ("bulk", "junk", "auto_reply")
            or msg.get("X-Autoreply", "").lower() == "yes")


def _quiet_logout(conn):
    try:
        conn.logout()
    except Exception:  # noqa: BLE001
        pass


_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>|<br\s*/?>|</p>|<[^>]+>", re.I | re.S)


def _html_to_text(html: str) -> str:
    text = _TAG_RE.sub(lambda m: "\n" if m.group(0).lower().startswith(("<br", "</p")) else "", html)
    return unescape(text)


def _plain_body(msg) -> str:
    """Prefer text/plain; fall back to a de-tagged text/html part (Outlook and most CRMs send HTML only)."""
    plain, html = None, None
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        ctype = part.get_content_type()
        disp = (part.get("Content-Disposition") or "").lower()
        if disp.startswith("attachment"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        if ctype == "text/plain" and plain is None:
            plain = text
        elif ctype == "text/html" and html is None:
            html = text
    if plain and plain.strip():
        return plain
    if html:
        return _html_to_text(html)
    return ""


def build_email_source(settings) -> EmailSource:
    if settings.gmail_address and settings.gmail_app_password:
        return GmailIMAP(settings.gmail_address, settings.gmail_app_password, settings.deal_subject_pattern,
                         settings.processed_ids_path)
    return MockEmailSource()
