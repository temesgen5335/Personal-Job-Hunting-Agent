"""Render a tailored Markdown CV to a PDF with a real, ATS-readable text layer.

Optional: needs fpdf2 (`pip install 'jobagent[apply]'` or `pip install fpdf2`), which is
pure Python with no system dependency — unlike the LaTeX toolchain ai-job-search uses.

This closes the loop `verify.py` opens. Today the ATS report checks the tailored
Markdown, but the email attaches a *different* file (the static `profile.cv_path`). When
APPLY_RENDER_CV_PDF is on, the PDF this produces is BOTH what gets attached AND what
`ats_report_for_pdf` checks — so the report describes the exact bytes that get sent.

Deliberately plain: a single-column layout a résumé parser reads cleanly beats a pretty
multi-column one it garbles (see verify.py). For a fully designed CV, keep your own PDF
at `profile.cv_path` and leave APPLY_RENDER_CV_PDF off — nothing here replaces it silently.

Limitation: the fpdf2 core fonts are Latin-1. Characters outside it are replaced, and the
ATS report — running over this very PDF — will honestly flag any contact detail that did
not survive, which is the whole point of verifying the rendered artifact.
"""

from __future__ import annotations

from pathlib import Path


class RenderUnavailable(RuntimeError):
    """No PDF rendering backend is installed."""


def is_available() -> bool:
    """True if a PDF can be rendered without raising RenderUnavailable."""
    import importlib.util
    return importlib.util.find_spec("fpdf") is not None


def _safe(text: str) -> str:
    """Core fonts are Latin-1; drop what they cannot encode rather than crash. The ATS
    report over the output will catch anything important that was lost."""
    return (text or "").encode("latin-1", "replace").decode("latin-1")


def render_cv_pdf(markdown: str, out_path: str) -> str:
    """Render Markdown CV text to a single-column PDF at `out_path`. Returns the path.

    Raises RenderUnavailable if fpdf2 is not installed. Handles headings (`#`/`##`/`###`),
    bullets (`-`/`*`/`•`), and inline `**bold**`/`__underline__` (via fpdf2 markdown)."""
    try:
        from fpdf import FPDF
    except ImportError as exc:
        raise RenderUnavailable(
            "PDF rendering needs fpdf2 — `pip install fpdf2` (or `pip install "
            "'jobagent[apply]'`), or leave APPLY_RENDER_CV_PDF off.") from exc

    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_margins(18, 15, 18)
    pdf.add_page()

    def cell(text: str, *, size: int = 10, bold: bool = False) -> None:
        # Explicit width (epw) + new_x=LMARGIN: multi_cell(w=0) leaves the cursor at the
        # right margin, so the next call gets ~0 width and fpdf2 raises. Pin both.
        pdf.set_font("Helvetica", style="B" if bold else "", size=size)
        pdf.multi_cell(pdf.epw, size * 0.5 if bold else 5, _safe(text),
                       markdown=True, new_x="LMARGIN", new_y="NEXT")

    for raw in (markdown or "").splitlines():
        line = raw.strip()
        if not line:
            pdf.ln(3)
            continue
        if line.startswith("###"):
            pdf.ln(2); cell(line.lstrip("#").strip(), size=11, bold=True)
        elif line.startswith("##"):
            pdf.ln(2); cell(line.lstrip("#").strip(), size=13, bold=True)
        elif line.startswith("#"):
            pdf.ln(2); cell(line.lstrip("#").strip(), size=16, bold=True)
        elif line[:2] in ("- ", "* ") or line.startswith("• "):
            cell("  -  " + line[1:].strip().lstrip("*-•").strip())
        else:
            cell(line)

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    pdf.output(out_path)
    return out_path
