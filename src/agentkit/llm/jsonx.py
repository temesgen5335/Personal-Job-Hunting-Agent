"""Tolerant JSON parsing for model output.

Models emit JSON that is *nearly* valid, and the failure modes are consistent enough to
handle rather than surrender to. Every rule here is a real observed failure, not a
hypothetical:

- literal newlines inside strings (invalid JSON; `strict=False` accepts them). This one
  shipped a real bug once: strict parsing failed, the fallback returned the whole raw
  blob, and the blob was one step away from being sent to a third party as if it were
  the finished text.
- markdown fences around the object, with or without a language tag
- prose before or after the object ("Here's the JSON: {...} Hope that helps!")
- trailing commas
- the literal `null` where an object was asked for

Returning None is always allowed — the caller decides whether to repair, retry on
another model, or degrade. Nothing here raises.
"""

from __future__ import annotations

import json
import re

_FENCE_OPEN = re.compile(r"^```[a-zA-Z0-9]*\s*")
_FENCE_CLOSE = re.compile(r"\s*```$")
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def strip_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = _FENCE_OPEN.sub("", t)
        t = _FENCE_CLOSE.sub("", t)
    return t.strip()


def extract_first_object(text: str) -> str | None:
    """The first balanced {...} or [...], ignoring braces inside strings.

    A regex cannot do this correctly — `{"a": "}"}` breaks any non-counting approach —
    so track string state and escapes explicitly.
    """
    start = None
    depth = 0
    in_string = False
    escape = False
    opener = closer = ""

    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if start is None and ch in "{[":
            start, opener = i, ch
            closer = "}" if ch == "{" else "]"
            depth = 1
            continue
        if start is not None:
            if ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
    return None


def loads(text: str) -> object | None:
    """Parse model output into JSON, repairing the usual damage. None if hopeless."""
    if not text or not text.strip():
        return None
    candidate = strip_fences(text)

    for attempt in (candidate, extract_first_object(candidate)):
        if not attempt:
            continue
        for repaired in (attempt, _TRAILING_COMMA.sub(r"\1", attempt)):
            try:
                # strict=False permits literal control characters inside strings, which
                # is the single most common way model JSON is invalid.
                return json.loads(repaired, strict=False)
            except (ValueError, TypeError):
                continue
    return None


def loads_object(text: str) -> dict | None:
    """Same, but only when the result is an object. `null` and arrays return None."""
    value = loads(text)
    return value if isinstance(value, dict) else None


def missing_keys(obj: dict, required: tuple[str, ...]) -> tuple[str, ...]:
    """Which required keys are absent — the body of a repair prompt worth sending."""
    return tuple(k for k in required if k not in obj)
