"""Read the applying mailbox and record outcome PROPOSALS.

Optional and off by default: it needs IMAP credentials, and a job agent that cannot see
your inbox is still a job agent. When configured, it closes the loop that made the
tracker a diary — the funnel only ever reflected what the operator remembered to type.

Nothing here changes an application. `scan()` writes proposals; accepting one is a
separate, explicit action, gated the same way sending is (R2).

The IMAP connection is injected, so every test runs against a fake mailbox with no
network and no credentials (R17).
"""

from __future__ import annotations

import email
import re
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import Message

from jobagent.inbox.classify import classify

# Only mail newer than the oldest open application can be relevant, and scanning a
# decade of archive on every poll is how a feature like this gets turned off.
DEFAULT_LOOKBACK_DAYS = 30
MAX_MESSAGES = 200


@dataclass
class ScanReport:
    examined: int = 0
    proposed: int = 0
    acknowledgements: int = 0
    skipped_not_a_reply: int = 0
    unmatched: int = 0          # a reply we could read, but to no known application


def decode_field(raw: str | None) -> str:
    """RFC2047 headers ("=?UTF-8?B?...?=") decoded to text.

    Without this a subject line arrives base64-encoded and every rule in the classifier
    silently fails to match — a detector that reports "nothing found" rather than an
    error, which is the worst way for this to break.
    """
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except (UnicodeDecodeError, LookupError, ValueError):
        return raw


def body_text(message: Message) -> str:
    """Prefer text/plain; fall back to stripping the HTML part.

    Multipart mail from an ATS is usually both, and the HTML half is full of markup that
    would swamp the phrase matching.
    """
    parts: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain":
                parts.append(_payload(part))
        if not parts:
            for part in message.walk():
                if part.get_content_type() == "text/html":
                    parts.append(re.sub(r"<[^>]+>", " ", _payload(part)))
    else:
        raw = _payload(message)
        parts.append(re.sub(r"<[^>]+>", " ", raw)
                     if message.get_content_type() == "text/html" else raw)
    return "\n".join(p for p in parts if p)


def _payload(part: Message) -> str:
    try:
        raw = part.get_payload(decode=True)
    except (AssertionError, LookupError, ValueError):
        return ""
    if raw is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def match_application(message_text: str, sender: str, applications: list[dict]) -> dict | None:
    """Attribute a reply to one submitted application.

    Matching is deliberately conservative — company name or the exact job title in the
    subject/body, or a sender domain that matches the apply address. Attributing a
    rejection to the WRONG application is worse than attributing it to none: it would
    close out a live opportunity on the operator's own record.
    """
    haystack = message_text.lower()
    domain = sender.split("@")[-1].strip(">").lower() if "@" in sender else ""

    best: dict | None = None
    for app in applications:
        company = (app.get("company") or "").strip().lower()
        title = (app.get("title") or "").strip().lower()
        apply_email = (app.get("apply_email") or "").lower()

        # A domain match is the strongest signal available and needs nothing else.
        if domain and apply_email and domain in apply_email:
            return app
        if company and len(company) >= 4 and company in haystack:
            # Title agreement upgrades a company hit but is not required — plenty of
            # replies never name the role.
            if title and title in haystack:
                return app
            best = best or app
    return best


class InboxReader:
    """Wraps an imaplib-compatible connection. Inject one in tests."""

    def __init__(self, connection, folder: str = "INBOX"):
        self.connection = connection
        self.folder = folder

    def fetch_recent(self, *, days: int = DEFAULT_LOOKBACK_DAYS,
                     limit: int = MAX_MESSAGES) -> list[Message]:
        from datetime import datetime, timedelta, timezone

        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
        self.connection.select(self.folder)
        status, data = self.connection.search(None, f'(SINCE "{since}")')
        if status != "OK" or not data or not data[0]:
            return []
        ids = data[0].split()[-limit:]
        out: list[Message] = []
        for mid in ids:
            status, payload = self.connection.fetch(mid, "(RFC822)")
            if status != "OK" or not payload:
                continue
            raw = next((p[1] for p in payload if isinstance(p, tuple) and len(p) > 1), None)
            if raw:
                out.append(email.message_from_bytes(raw))
        return out


def scan(store, reader: InboxReader, *, days: int = DEFAULT_LOOKBACK_DAYS) -> ScanReport:
    """Read the mailbox and record proposals. Changes no application.

    Only SUBMITTED applications are candidates — an outcome for something never sent is
    a misattribution by definition, and the transition map would reject it anyway.
    """
    report = ScanReport()
    candidates = [a for a in store.list_applications(limit=500)
                  if a.get("status") in ("submitted", "interview")]
    if not candidates:
        return report

    for message in reader.fetch_recent(days=days):
        report.examined += 1
        subject = decode_field(message.get("Subject"))
        sender = decode_field(message.get("From"))
        body = body_text(message)

        proposal = classify(subject, body)
        if proposal.kind == "not_a_reply":
            report.skipped_not_a_reply += 1
            continue
        if proposal.kind == "acknowledgement":
            report.acknowledgements += 1
            continue
        if not proposal.status:
            continue

        # The sender is part of the haystack, not just the domain check: replies
        # routinely come from an ATS domain ("no-reply@greenhouse-mail.io") while the
        # display name still carries the employer ("Northwind Labs via Greenhouse").
        app = match_application(f"{sender}\n{subject}\n{body}", sender, candidates)
        if app is None:
            report.unmatched += 1
            continue

        # A Message-ID is what makes re-reading the mailbox idempotent. Falling back to
        # subject+sender is weaker but still stable for mail that omits one.
        message_id = (message.get("Message-ID") or f"{sender}|{subject}").strip()
        created = store.add_proposal(
            application_id=app["id"], message_id=message_id,
            proposed=proposal.status, confidence=proposal.confidence,
            reason=proposal.reason, subject=subject, sender=sender,
            received_at=decode_field(message.get("Date")) or None)
        if created:
            report.proposed += 1
    return report
