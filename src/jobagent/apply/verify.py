"""ATS-parseability verification for generated application assets.

An ATS (applicant-tracking system) parser reads the *text* of a CV, not its layout.
A tailored CV can look right to a human and still fail a parser: a contact detail
carried only by an icon or a hyperlink is invisible, garbled glyphs extract as noise,
and a posting's keywords may be missing. This module reports all three over plain
text, so it works on the Markdown the generators already produce *and* on the text
layer extracted from a rendered PDF (see `pdf_verify.extract_text_layer`).

Design notes:
- Pure and model-free: deterministic, offline-testable (R17), no I/O.
- **Honest, never stuffing (R1).** Missing keywords are reported as gaps, never
  injected. `ok` therefore depends only on hard defects (garbled text, a contact
  detail that does not survive as literal text) — never on keyword coverage.

The technique (verify the artifact a parser actually sees, keywords honestly absent)
is adapted from MadsLorentzen/ai-job-search (MIT).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

# Tokens: words plus the punctuation that carries real skill names — c++, c#, .net,
# node.js, ci/cd, .NET. A leading dot is allowed so ".net" survives tokenization.
_TOKEN_RE = re.compile(r"\.?[a-z0-9][a-z0-9+.#/-]*[a-z0-9+#]|\.?[a-z0-9]")

# Short tokens are usually noise, but these are real, high-signal skills.
_SHORT_ALLOW = {"go", "r", "ai", "ml", "qa", "ci", "cd", "ci/cd", "ux", "ui",
                "aws", "gcp", "sql", "api", "css", "js", "ts", "c#", "c++", ".net"}

_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "else", "for", "to", "of",
    "in", "on", "at", "by", "with", "from", "as", "is", "are", "was", "were", "be",
    "been", "being", "this", "that", "these", "those", "it", "its", "you", "your",
    "we", "our", "they", "their", "will", "would", "can", "could", "should", "may",
    "have", "has", "had", "do", "does", "did", "not", "no", "so", "than", "such",
    "who", "what", "when", "where", "which", "how", "all", "any", "each", "more",
    "most", "other", "some", "up", "out", "about", "into", "over", "after", "work",
    "role", "team", "teams", "company", "job", "position", "candidate", "experience",
    "years", "year", "strong", "good", "great", "excellent", "ability", "skills",
    "skill", "including", "etc", "e.g", "you'll", "we're", "looking", "join", "help",
    "build", "building", "using", "across", "within", "per", "via", "plus", "must",
    "well", "new", "like", "one", "two", "three", "day", "days", "week", "月", "world",
}


@dataclass
class AtsReport:
    """The result of an ATS-parseability check over a text artifact.

    `ok` is a hard-defect flag: it is False only when a contact detail does not
    survive as literal text or the text extracts as garbage. Missing keywords are
    honest gaps and never flip `ok`.
    """

    ok: bool
    issues: list[str] = field(default_factory=list)
    covered: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    coverage: float = 1.0
    extractor: str = "markdown"

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "issues": self.issues,
            "covered": self.covered,
            "missing": self.missing,
            "coverage": round(self.coverage, 3),
            "extractor": self.extractor,
        }

    def summary(self) -> str:
        pct = f"{self.coverage * 100:.0f}%"
        head = "✅ ATS-clean" if self.ok else "⚠️ ATS issues"
        parts = [f"{head} · keyword coverage {pct}"]
        if self.issues:
            parts.append("issues: " + "; ".join(self.issues))
        if self.missing:
            parts.append("gaps: " + ", ".join(self.missing[:8]))
        return " · ".join(parts)


def _normalize(text: str) -> str:
    return " ".join((text or "").lower().split())


def _salient(tok: str) -> bool:
    if tok in _SHORT_ALLOW:
        return True
    if len(tok) < 3:
        return False
    if tok in _STOPWORDS:
        return False
    if tok.replace(".", "").replace("-", "").isdigit():
        return False
    return True


def _keyword_stream(text: str) -> str:
    """A space-joined stream of this text's tokens. Coverage matches against this, so a
    bigram like 'python fastapi' matches a CV that wrote 'Python, FastAPI' — the same
    tokenizer decides adjacency on both sides, so punctuation never hides a real match."""
    return " ".join(_TOKEN_RE.findall((text or "").lower()))


def extract_keywords(job: dict, *, limit: int = 25) -> list[str]:
    """Salient unigrams and adjacent bigrams from the posting, ranked by frequency.

    Heuristic on purpose — there is no skill taxonomy here. The output feeds an
    *informational* coverage check, not an auto-edit, so over-inclusion is cheap and
    a missing term is only ever surfaced as a gap for the human to weigh.
    """
    counts: Counter[str] = Counter()
    # Fields are scanned separately so adjacency never straddles a boundary — the title
    # ending "…Engineer" and the body starting "Python…" must not mint "engineer python".
    for field_text in (job.get("title", ""), job.get("description", "")):
        prev: str | None = None
        for tok in _TOKEN_RE.findall((field_text or "").lower()):
            if _salient(tok):
                counts[tok] += 1
                if prev is not None:
                    counts[f"{prev} {tok}"] += 1
                prev = tok
            else:
                prev = None
    # Rank by frequency, then prefer the more specific (longer) term on ties.
    ranked = sorted(counts, key=lambda k: (counts[k], len(k)), reverse=True)
    return ranked[:limit]


def _covered(keyword: str, haystack: str) -> bool:
    # Word-boundary match on a token stream, tolerant of the tech punctuation kept
    # by the tokenizer. `re.escape` keeps `c++`/`.net` literal.
    return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", haystack) is not None


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def ats_report(
    text: str,
    job: dict,
    *,
    name: str = "",
    email: str = "",
    phone: str = "",
    extractor: str = "markdown",
) -> AtsReport:
    """Check `text` (a CV) the way an ATS parser sees it.

    Contact fields are checked only when supplied. Keyword coverage is measured
    against the posting and reported honestly — gaps stay visible.
    """
    norm = _normalize(text)
    issues: list[str] = []

    # Garbled extraction: icon-glyph noise or replacement characters mean the text
    # layer is degraded. Harmless on Markdown; the real signal is on extracted PDFs.
    if "(cid:" in (text or ""):
        issues.append("text contains (cid:) markers — glyphs did not extract to text")
    if "�" in (text or ""):
        issues.append("text contains � replacement characters — encoding lost")

    # Contact details must appear as literal text, not only in an icon or a link.
    if name and _normalize(name) not in norm:
        issues.append("name not present as literal text")
    if email and email.lower() not in norm:
        issues.append("email not present as literal text")
    if phone and _digits(phone) and _digits(phone) not in _digits(text):
        issues.append("phone not present as literal text")

    keywords = extract_keywords(job)
    hay = _keyword_stream(text)   # tokenized stream: punctuation can't hide a real match
    covered = [k for k in keywords if _covered(k, hay)]
    missing = [k for k in keywords if k not in covered]
    coverage = (len(covered) / len(keywords)) if keywords else 1.0

    return AtsReport(
        ok=not issues,
        issues=issues,
        covered=covered,
        missing=missing,
        coverage=coverage,
        extractor=extractor,
    )


def ats_report_for_pdf(pdf_path, job: dict, **contact) -> AtsReport:
    """Run the same check over a *rendered* PDF's extracted text layer.

    Bridges the text-level report to a real artifact (e.g. the PDF actually attached
    to an application). Imports the optional extractor lazily so nothing here needs a
    PDF toolchain at import time.
    """
    from jobagent.apply.pdf_verify import extract_text_layer

    text, _pages, extractor = extract_text_layer(pdf_path)
    return ats_report(text, job, extractor=extractor, **contact)
