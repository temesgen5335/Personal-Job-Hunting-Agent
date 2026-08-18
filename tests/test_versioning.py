"""One version, one place.

The API once reported "2.0" while pyproject.toml said "3.0.0" — two numbers, both
claiming to be the version, and nothing to say which was wrong. These tests make that
state unreachable rather than merely discouraged.
"""

import re
import tomllib
from pathlib import Path

import jobagent

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


def test_the_version_is_semver():
    assert SEMVER.match(jobagent.__version__), (
        f"{jobagent.__version__!r} is not SemVer — releases are ordered by it, and the "
        "upgrade policy in docs/VERSIONING.md is stated in terms of MAJOR/MINOR/PATCH"
    )


def test_pyproject_reads_the_version_from_code_rather_than_repeating_it():
    """A literal in pyproject is a second source of truth that goes stale silently."""
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "version" not in cfg["project"], (
        "pyproject.toml declares its own version literal — it must stay dynamic and read "
        "src/jobagent/__init__.py, or the two will drift"
    )
    assert "version" in cfg["project"].get("dynamic", [])
    hatch = cfg["tool"]["hatch"]["version"]
    assert hatch["path"] == "src/jobagent/__init__.py"


def test_the_installed_metadata_agrees_with_the_code():
    """Catches a stale editable install — the state where a contributor bumps the
    version, sees the old one everywhere, and has no idea why."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        installed = version("personal-job-agent")
    except PackageNotFoundError:
        return  # running from a bare checkout; the code literal is still authoritative
    assert installed == jobagent.__version__, (
        f"installed metadata says {installed}, code says {jobagent.__version__} — "
        "reinstall with `uv pip install -e .` after bumping"
    )


def test_the_api_reports_the_package_version():
    """/health is what the dashboard footer and any monitor read."""
    from fastapi.testclient import TestClient

    from jobagent.api import create_app
    from jobagent.config import Settings

    app = create_app(settings=Settings(_env_file=None), profile=None, llm=None, cv_master="")
    assert app.version == jobagent.__version__
    assert TestClient(app).get("/health").json()["version"] == jobagent.__version__


def test_no_module_hardcodes_a_version_string():
    """The regression net for the original bug: a literal that looks like a version,
    sitting next to a FastAPI/app constructor, is how the drift started."""
    offenders = []
    for path in (ROOT / "src").rglob("*.py"):
        if path.name == "__init__.py" and path.parent.name == "jobagent":
            continue  # the one permitted literal
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"version\s*=\s*[\"']\d+\.\d+", line):
                offenders.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    assert not offenders, "hardcoded version literals found:\n" + "\n".join(offenders)


def test_the_changelog_documents_the_current_version():
    """A release nobody wrote down is one nobody can upgrade to deliberately."""
    changelog = (ROOT / "CHANGELOG.md").read_text()
    assert f"[{jobagent.__version__}]" in changelog, (
        f"CHANGELOG.md has no entry for {jobagent.__version__}"
    )
