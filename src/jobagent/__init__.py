"""Personal job-hunting agent — see docs/ARCHITECTURE.md and docs/VERSIONING.md."""

# THE single source of truth for the project version. `pyproject.toml` reads this
# attribute (hatchling `version.source = "code"`), so bumping here bumps the package,
# the FastAPI app, /health and the dashboard footer in one edit.
#
# Deliberately a plain literal rather than importlib.metadata: an editable install
# caches its dist-info, so metadata stays stale until someone reinstalls — a
# contributor who bumped the version would keep seeing the old one and have no idea
# why. A literal cannot go stale. It is also how the API came to report "2.0" while
# pyproject said "3.0.0"; the fix is one number, not a cleverer lookup.
__version__ = "3.2.0"

__all__ = ["__version__"]
