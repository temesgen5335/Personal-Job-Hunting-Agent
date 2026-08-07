"""Classify provider failures so failover can be intelligent.

`MultiLLM` falls through on *any* exception, which means a permanent 401 costs a full
retry on every single call and a rate limit is treated the same as a wrong API key.
Classifying first lets the caller do the obviously-right thing per case: retry a
timeout, wait out a 429, and never touch a dead key again this process.

Classification order is most-reliable signal first — SDK exception type, then HTTP
status, and only then string matching. String heuristics are last because provider
error prose changes without notice.

The SDKs are imported lazily elsewhere in this project and may be absent entirely, so
types are matched by name rather than by `isinstance`. This module imports cleanly with
no provider SDK installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Verdict(StrEnum):
    PERMANENT = "permanent"          # bad key, disabled account — never retry
    RATE_LIMIT = "rate_limit"
    TRANSIENT = "transient"          # 5xx, timeout, connection reset
    BAD_REQUEST = "bad_request"      # our payload is wrong; retrying changes nothing
    CAPABILITY = "capability"        # model can't do this (no tools, unknown model)
    CONTEXT = "context"              # prompt too long for this model
    CONTENT_FILTER = "content_filter"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Classified:
    verdict: Verdict
    message: str
    status: int | None = None
    retry_after_s: float | None = None

    @property
    def retryable_same_backend(self) -> bool:
        return self.verdict in (Verdict.TRANSIENT, Verdict.RATE_LIMIT, Verdict.UNKNOWN)

    @property
    def should_cooldown(self) -> bool:
        return self.verdict in (Verdict.PERMANENT, Verdict.RATE_LIMIT, Verdict.TRANSIENT)


_RETRY_AFTER_CAP = 300.0

# Matched on `type(exc).__name__` so no SDK import is needed.
_BY_TYPE_NAME = {
    "AuthenticationError": Verdict.PERMANENT,
    "PermissionDeniedError": Verdict.PERMANENT,
    "RateLimitError": Verdict.RATE_LIMIT,
    "APITimeoutError": Verdict.TRANSIENT,
    "APIConnectionError": Verdict.TRANSIENT,
    "InternalServerError": Verdict.TRANSIENT,
    "NotFoundError": Verdict.CAPABILITY,
    "UnprocessableEntityError": Verdict.BAD_REQUEST,
    "ConnectTimeout": Verdict.TRANSIENT,
    "ReadTimeout": Verdict.TRANSIENT,
    "ConnectError": Verdict.TRANSIENT,
}

_BY_STATUS = {
    400: None,        # ambiguous — inspect the body below
    401: Verdict.PERMANENT,
    402: Verdict.PERMANENT,
    403: Verdict.PERMANENT,
    404: Verdict.CAPABILITY,
    408: Verdict.TRANSIENT,
    413: Verdict.CONTEXT,
    422: Verdict.BAD_REQUEST,
    429: Verdict.RATE_LIMIT,
}


def _retry_after(exc: Exception) -> float | None:
    """Honor a numeric Retry-After, capped — a hostile or buggy value must not wedge
    the process."""
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    raw = None
    for key in ("retry-after", "Retry-After", "x-ratelimit-reset-requests"):
        if key in headers:
            raw = headers[key]
            break
    if raw is None:
        return None
    try:
        return max(0.0, min(_RETRY_AFTER_CAP, float(str(raw).rstrip("s"))))
    except (TypeError, ValueError):
        return None      # HTTP-date form → fall back to our own backoff


def _from_body(text: str) -> Verdict:
    t = text.lower()
    if "context length" in t or "context_length" in t or "too many tokens" in t:
        return Verdict.CONTEXT
    # Groq returns 400 tool_use_failed when the model emits a malformed call. That is a
    # *generation* failure, not a missing capability — observed live on
    # llama-3.3-70b-versatile, which is measured 5/5 on tool loops. Classifying it as
    # CAPABILITY was wrong twice over: it never retries, and it blames a model that can
    # in fact do the job. Generation is non-deterministic, so a retry usually succeeds,
    # and the attempt budget bounds it.
    if "tool_use_failed" in t or "failed to call a function" in t:
        return Verdict.TRANSIENT
    if "tool" in t or "function calling" in t:
        return Verdict.CAPABILITY
    if "content" in t and ("filter" in t or "policy" in t):
        return Verdict.CONTENT_FILTER
    return Verdict.BAD_REQUEST


def classify(exc: Exception) -> Classified:
    name = type(exc).__name__
    text = str(exc)
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)

    verdict = _BY_TYPE_NAME.get(name)

    if verdict is None and isinstance(status, int):
        verdict = _BY_STATUS.get(status)
        if status == 400 or verdict is None:
            verdict = _from_body(text) if status and 400 <= status < 500 else None
        if verdict is None and isinstance(status, int) and status >= 500:
            verdict = Verdict.TRANSIENT

    if verdict is None:
        low = text.lower()
        if "rate limit" in low or "429" in low:
            verdict = Verdict.RATE_LIMIT
        elif "timeout" in low or "timed out" in low:
            verdict = Verdict.TRANSIENT
        elif "api key" in low or "unauthorized" in low:
            verdict = Verdict.PERMANENT
        elif "context length" in low:
            verdict = Verdict.CONTEXT
        else:
            verdict = Verdict.UNKNOWN

    return Classified(verdict=verdict, message=f"{name}: {text[:200]}",
                      status=status if isinstance(status, int) else None,
                      retry_after_s=_retry_after(exc))
