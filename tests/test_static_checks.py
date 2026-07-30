"""Static guard: no undefined names anywhere in the package.

Motivation: `bot/app.py` called an undefined `_llm()` on the /apply fit-check line —
a guaranteed NameError on the bot's primary path, shipped and unnoticed. The Telegram
handlers have no runtime coverage (they need live Update/Context objects), so a
static check is what actually catches this class of bug rather than a unit test.

Scoped to F821 (undefined name) on purpose: it is a correctness rule, not style, so
this test stays meaningful without turning into a lint-formatting gate.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_no_undefined_names_in_package():
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--select", "F821",
         "--output-format=concise", "src/", "scripts/"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    # ruff exits 0 (clean) or 1 (violations); anything else means it could not run.
    if proc.returncode not in (0, 1):
        pytest.skip(f"ruff unavailable: {(proc.stderr or proc.stdout).strip()[:160]}")
    assert proc.returncode == 0, f"undefined name(s) — these are runtime NameErrors:\n{proc.stdout}"
