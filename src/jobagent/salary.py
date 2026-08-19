"""Parse free-text compensation into structured numbers.

`salary_text` has been stored since v1 and never read: not filtered on, not ranked on,
not shown as anything but a string. For most people compensation is a top-three filter.

The parser is deliberately conservative. Job ads write money in every imaginable way,
and a wrong number is far worse than no number — it would filter out a job that pays
well, invisibly. So anything ambiguous returns None rather than a guess, and
`salary_text` is always kept verbatim alongside whatever was extracted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Order matters: symbols are searched before codes, because the "$" in "US$120,000"
# should win over a stray "usd" elsewhere in the sentence.
_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "¥": "JPY", "₦": "NGN"}
_CODES = ("USD", "EUR", "GBP", "CAD", "AUD", "CHF", "SEK", "INR", "JPY", "NGN", "ZAR")

# A number with optional thousands separators and an optional k/m suffix.
_NUM = r"(\d[\d,.\s]*\d|\d)\s*([kKmM])?"
# The separator may carry a currency marker of its own: "$120,000 - $160,000" is the
# single most common form there is, and without allowing it here the range never matched
# and the parser silently fell through to the single-number branch — reporting the LOWER
# bound as both min and max. Found by running it, not by reading it.
_CUR_MARK = r"(?:[$€£₹¥₦]|USD|EUR|GBP|CAD|AUD|CHF|SEK|INR|JPY|NGN|ZAR)?\s*"
_RANGE = re.compile(rf"{_NUM}\s*(?:-|–|—|to|up to)\s*{_CUR_MARK}{_NUM}", re.I)
_SINGLE = re.compile(_NUM)

_PERIODS = (
    # (regex, canonical, annualisation factor). The factors are the conventional ones —
    # 2080 work-hours and 260 work-days a year — used only to compare like with like.
    (re.compile(r"\b(per\s+hour|/\s*h(ou)?r|hourly|an hour|p/h)\b", re.I), "hour", 2080),
    (re.compile(r"\b(per\s+day|/\s*day|daily|day rate)\b", re.I), "day", 260),
    (re.compile(r"\b(per\s+week|/\s*wk|weekly)\b", re.I), "week", 52),
    (re.compile(r"\b(per\s+month|/\s*mo(nth)?|monthly|a month)\b", re.I), "month", 12),
    (re.compile(r"\b(per\s+year|per\s+annum|/\s*(yr|year)|annually|annual|annum|"
                r"p\.?a\.?|yearly)\b", re.I), "year", 1),
)

# Text that means "there is a number here but it is not pay".
_NOT_PAY = re.compile(
    r"\b(equity|\d+\s*%|employees|founded|years?\s+of\s+experience|"
    r"\d{4}\s*-\s*\d{4}|401\(?k\)?)\b", re.I)


@dataclass(frozen=True)
class Salary:
    """What could be extracted. Any field may be None — partial beats wrong."""

    min: float | None = None
    max: float | None = None
    currency: str | None = None
    period: str | None = None

    @property
    def annual_min(self) -> float | None:
        return self._annualise(self.min)

    @property
    def annual_max(self) -> float | None:
        return self._annualise(self.max)

    def _annualise(self, value: float | None) -> float | None:
        """Normalise to a yearly figure so an hourly rate and a salary compare sanely.

        Returns None when the period is unknown rather than assuming "year": guessing
        would make "$50/hour" look like a $50 job and rank it below everything.
        """
        if value is None or self.period is None:
            return None
        factor = next((f for _, name, f in _PERIODS if name == self.period), None)
        return value * factor if factor else None


def _to_number(digits: str, suffix: str | None) -> float | None:
    cleaned = digits.replace(",", "").replace(" ", "")
    # A dot may be a decimal point or a thousands separator ("120.000" in de-DE).
    # Exactly three trailing digits reads as a separator, which is the European form.
    if cleaned.count(".") == 1:
        whole, _, frac = cleaned.partition(".")
        cleaned = whole + frac if len(frac) == 3 else cleaned
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if suffix and suffix.lower() == "k":
        value *= 1_000
    elif suffix and suffix.lower() == "m":
        value *= 1_000_000
    return value


def _currency(text: str) -> str | None:
    for symbol, code in _SYMBOLS.items():
        if symbol in text:
            return code
    upper = text.upper()
    for code in _CODES:
        if re.search(rf"\b{code}\b", upper):
            return code
    return None


def _period(text: str) -> str | None:
    for pattern, name, _ in _PERIODS:
        if pattern.search(text):
            return name
    return None


def parse_salary(text: str | None) -> Salary:
    """Best-effort extraction. Returns an all-None Salary when nothing is confident.

    Never raises: this runs on every ingested posting, and one weird string must not
    take down a whole source.
    """
    if not text or not text.strip():
        return Salary()
    if _NOT_PAY.search(text):
        return Salary()

    currency, period = _currency(text), _period(text)

    match = _RANGE.search(text)
    if match:
        low = _to_number(match.group(1), match.group(2))
        high = _to_number(match.group(3), match.group(4))
        # "120-160k": the suffix on the upper bound applies to both, or the range reads
        # as 120 to 160,000 — a thousand-fold error in the direction that hides jobs.
        if low is not None and high is not None and match.group(4) and not match.group(2):
            low = _to_number(match.group(1), match.group(4))
        if low is not None and high is not None and low > high:
            low, high = high, low
        return Salary(min=low, max=high, currency=currency, period=period)

    single = _SINGLE.search(text)
    if single:
        value = _to_number(single.group(1), single.group(2))
        # A bare small number with no currency and no suffix is not pay — it is a team
        # size, a year, or a bullet that happened to start with a digit.
        if value is None or (value < 1000 and not single.group(2) and not currency):
            return Salary()
        return Salary(min=value, max=value, currency=currency, period=period)

    return Salary()


def infer_period(salary: Salary) -> str | None:
    """Guess a missing period from magnitude, for DISPLAY only.

    Never used for filtering: a guess that decides whether a job is shown is a guess
    that hides jobs. Kept as a separate function so that distinction is structural
    rather than a comment someone can miss.
    """
    if salary.period or salary.max is None:
        return salary.period
    if salary.max <= 500:
        return "hour"
    if salary.max <= 20_000:
        return "month"
    return "year"
