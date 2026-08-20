"""Read a reply and propose — never apply — an application outcome.

The tracker knows what you sent and learns nothing about what came back, so the funnel
and the response rates only ever reflect what the operator remembered to type in. That
makes the analytics a diary rather than data, and it is why matching quality cannot
currently be scored against what actually got replies.

**This module proposes. It never decides.** Every result is surfaced for one-tap
confirmation, exactly like the HITL gate on sending (R2). The reasoning is the same:
a wrong automatic transition corrupts the operator's own history silently, and a
rejection email misread as an interview invitation is worse than no detection at all.

Pure functions only — no IMAP, no network, no store. `reader.py` does the fetching.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Ordered by strength. The FIRST match wins, so the sequence matters: a rejection that
# also says "we will keep your CV on file for future interviews" must not read as an
# interview invitation. Rejections are checked first for exactly that reason.
_RULES: tuple[tuple[str, tuple[str, ...], int], ...] = (
    (
        "rejected",
        (
            r"\bunfortunately\b",
            r"\bwe (?:have )?(?:decided|regret)\b",
            r"\bnot (?:be )?(?:moving|proceeding|progressing) forward\b",
            r"\bwill not be (?:moving|proceeding|progressing)\b",
            r"\bwe (?:are|'re| have) (?:not )?(?:pursuing|selected) other candidates?\b",
            r"\bother candidates?\b.{0,40}\bbetter (?:fit|match)\b",
            r"\bno longer under consideration\b",
            r"\bwe.{0,20}\bnot (?:to )?(?:move|proceed)\b",
            r"\bposition has been filled\b",
            r"\bdecided not to (?:move|proceed|continue)\b",
        ),
        2,
    ),
    (
        "offer",
        (
            r"\bpleased to offer\b",
            r"\bdelighted to offer\b",
            r"\bwe.{0,15}\bofferring?\b.{0,25}\bposition\b",
            r"\boffer of employment\b",
            r"\bformal offer\b",
            r"\bwelcome (?:you )?(?:aboard|to the team)\b",
        ),
        2,
    ),
    (
        "interview",
        (
            r"\bschedule (?:a|an|your) (?:call|chat|interview|conversation)\b",
            r"\binvite you to (?:an?|the) (?:interview|call|conversation)\b",
            r"\bwould (?:you )?(?:like|love) to (?:speak|chat|talk|meet)\b",
            r"\bnext (?:step|stage|round)\b",
            r"\bmove(?:d|ing)? (?:you )?(?:forward|to the next)\b",
            r"\bbook (?:a|some) time\b",
            r"\btechnical (?:screen|interview|assessment)\b",
            r"\bavailability (?:for|to)\b.{0,30}\b(?:call|interview|chat)\b",
        ),
        2,
    ),
)

# Mail that is about an application but carries no outcome. Detected so a mailbox full
# of these does not read as "nothing is arriving" — the difference between a quiet
# process and a broken detector is worth being able to see.
_ACKNOWLEDGEMENT = (
    r"\bwe (?:have )?received your application\b",
    r"\bthanks? (?:you )?for (?:applying|your application|your interest)\b",
    r"\byour application (?:has been|was) (?:received|submitted)\b",
    r"\bwe are reviewing\b",
)

# Automated mail that is not a reply at all. Checked before everything else, because a
# job alert digest quotes enough hiring language to trip every rule above.
_NOT_A_REPLY = (
    r"\bunsubscribe\b",
    r"\bjob alert\b",
    r"\bnew jobs? (?:for|matching)\b",
    r"\brecommended jobs?\b",
    r"\bnewsletter\b",
    r"\bno-?reply@",
    r"\bverify your (?:email|account)\b",
    r"\bpassword reset\b",
)


@dataclass(frozen=True)
class Proposal:
    """A suggested outcome. `status` is None when nothing was detected."""

    status: str | None = None
    confidence: str = "none"        # none | low | high
    reason: str = ""                # the phrase that matched, for the operator to judge
    kind: str = "unknown"           # outcome | acknowledgement | not_a_reply | unknown
    matched: list[str] = field(default_factory=list)


def _hits(text: str, patterns) -> list[str]:
    found = []
    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            found.append(m.group(0).strip())
    return found


def classify(subject: str | None, body: str | None) -> Proposal:
    """Propose an outcome for one email. Never raises.

    Confidence is deliberately coarse — `high` only when two or more independent
    phrases agree. A single phrase is `low`, which the UI shows differently, because
    one idiom is exactly how a polite rejection gets read as an invitation.
    """
    text = f"{subject or ''}\n{body or ''}"
    if not text.strip():
        return Proposal()

    # Truncated deliberately: quoted history below a reply contains the ORIGINAL
    # application and often an earlier rejection, so scanning the whole thread would
    # classify the conversation rather than the newest message.
    head = _strip_quoted(text)[:4000]

    if _hits(head, _NOT_A_REPLY):
        return Proposal(kind="not_a_reply")

    for status, patterns, _ in _RULES:
        matched = _hits(head, patterns)
        if matched:
            return Proposal(
                status=status,
                confidence="high" if len(matched) >= 2 else "low",
                reason=matched[0],
                kind="outcome",
                matched=matched,
            )

    if _hits(head, _ACKNOWLEDGEMENT):
        return Proposal(kind="acknowledgement")
    return Proposal()


def _strip_quoted(text: str) -> str:
    """Drop the quoted thread below a reply.

    Without this, a rejection quoting the original "we'd like to schedule a call" would
    be classified from the older message — and the newest message is the whole point.
    """
    markers = (
        r"\nOn .{0,80}wrote:",          # Gmail / Apple Mail
        r"\n-{2,}\s*Original Message",  # Outlook
        r"\n_{5,}",                     # Outlook divider
        r"\n>{1,}\s",                   # plain quoting
        r"\nFrom:\s",                   # forwarded header block
    )
    cut = len(text)
    for marker in markers:
        m = re.search(marker, text)
        if m:
            cut = min(cut, m.start())
    return text[:cut]
