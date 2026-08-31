"""Extract and verify the ATS-readable text layer of a rendered PDF.

Optional bridge for verifying a *rendered* application artifact (a compiled CV PDF)
with the same checks `verify.ats_report` runs over Markdown. Nothing in the hot path
imports this — it is used only when a PDF actually exists, so the base install needs
no PDF toolchain.

Extraction tries pypdf (BSD, optional `pip install pypdf`) first, then Poppler
`pdftotext` if pypdf is missing, raises, or yields no extractable characters.

Vendored and adapted from MadsLorentzen/ai-job-search `tools/verify_pdf.py` (MIT).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


class VerificationError(Exception):
    """Raised when a PDF cannot be read or does not satisfy its checks."""


def _run_tool(command: list[str]) -> str:
    try:
        return subprocess.run(
            command, check=True, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        ).stdout
    except FileNotFoundError as exc:
        raise VerificationError(
            f"required command '{command[0]}' was not found. Install pypdf "
            "(`pip install pypdf`) or poppler-utils (macOS: brew install poppler, "
            "Debian/Ubuntu: apt install poppler-utils)."
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip() or (exc.stdout or "").strip() or "command failed"
        raise VerificationError(f"{command[0]} could not read the PDF: {detail}") from exc


def normalize_text(text: str) -> str:
    return " ".join((text or "").split())


def parse_page_count(pdfinfo_output: str) -> int:
    match = re.search(r"^Pages:\s+(\d+)\s*$", pdfinfo_output, re.MULTILINE)
    if not match:
        raise VerificationError("pdfinfo output did not contain a page count")
    return int(match.group(1))


def _extract_pypdf(pdf_path: Path):
    """Return (text, pages) or None if pypdf is unavailable, raises, or yields no text."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        reader = PdfReader(str(pdf_path))
        pages = len(reader.pages)
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return None
    if len(normalize_text(text)) == 0:
        return None
    return text, pages


def _extract_pdftotext(pdf_path: Path):
    text = _run_tool(["pdftotext", "-layout", "-enc", "UTF-8", str(pdf_path), "-"])
    pages = parse_page_count(_run_tool(["pdfinfo", str(pdf_path)]))
    return text, pages


def extract_text_layer(pdf_path) -> tuple[str, int, str]:
    """Extract ATS-readable text. Returns (text, pages, extractor_name)."""
    pdf_path = Path(pdf_path)
    if not pdf_path.is_file():
        raise VerificationError(f"PDF does not exist: {pdf_path}")
    result = _extract_pypdf(pdf_path)
    if result is not None:
        text, pages = result
        return text, pages, "pypdf"
    text, pages = _extract_pdftotext(pdf_path)
    return text, pages, "pdftotext"
