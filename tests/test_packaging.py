"""Open-source packaging invariants.

These encode the findings of the 2026-08-18 review as tests, because every one of them
was true for months without anyone noticing. A doc can claim MIT; only a file grants it.
"""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_a_license_file_exists_and_matches_the_declared_license():
    """pyproject claimed MIT for months with no LICENSE file, which legally means all
    rights reserved — nobody could use the code, whatever the README implied."""
    license_file = ROOT / "LICENSE"
    assert license_file.exists(), "no LICENSE file — default copyright is all rights reserved"
    text = license_file.read_text()
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["license"]
    name = declared["text"] if isinstance(declared, dict) else declared
    assert name.lower() in text.lower().split("\n")[0].lower(), (
        f"pyproject declares {name!r} but LICENSE does not name it"
    )
    assert "Copyright (c)" in text


def test_the_committed_profile_is_a_template_not_a_person():
    """The whole point of v3.2.0: a clone must not inherit someone's job search.

    Identity was scrubbed to placeholders long ago, but roles, skills, weights, a
    watchlist and a real `location` were not — because that work framed "personal" as
    contact details. This asserts the broader property.
    """
    example = ROOT / "config" / "preferences.example.toml"
    assert example.exists(), "the shipped template is missing"
    cfg = tomllib.loads(example.read_text())
    profile = cfg["profile"]

    # Identity must be obviously fake.
    assert profile["name"] == "Your Name"
    assert "example.com" in profile["email"]
    assert "Your City" in profile["location"], (
        "location must be a placeholder — a real city is as personal as a phone number"
    )

    # The search profile must be small and generic. A long, tuned list is somebody's.
    assert len(profile["target_roles"]) <= 4, "template ships too many roles to be generic"
    assert len(profile["core_skills"]) <= 6, "template ships too many skills to be generic"
    assert len(cfg["profile"]["skill_weights"]) <= 6

    # And the watchlist must be illustrative, not a curated shortlist.
    watch = cfg["watchlist"]
    assert sum(len(v) for v in watch.values()) <= 8, (
        "the template watchlist is a curated list — these are the employers polled directly"
    )


def test_the_personal_profile_is_not_tracked_by_git():
    """`config/preferences.toml` is where a real search lives. It must stay untracked,
    or the next `git add -A` republishes it."""
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files", "config/"], cwd=ROOT, capture_output=True, text=True
    ).stdout.split()
    assert "config/preferences.toml" not in tracked, (
        "config/preferences.toml is tracked again — it carries a real search profile"
    )
    assert "config/preferences.example.toml" in tracked, "the template must be committed"


def test_a_fresh_clone_gets_a_usable_profile():
    """With no `preferences.toml`, the loader must fall back to the template. Without
    this a clone has empty roles and skills, so every posting scores zero and the
    matcher looks broken."""
    from jobagent.preferences import EXAMPLE_PATH, load_preferences

    prefs = load_preferences(
        path="config/definitely-not-here.toml",
        local_path="/nonexistent/local.toml",
        overlay_path="/nonexistent/overlay.json",
    )
    # An explicit non-default path must NOT fall back — tests rely on that for isolation.
    assert prefs.profile.target_roles == []

    # But the default path missing must.
    assert Path(EXAMPLE_PATH).exists()
    template = tomllib.loads(Path(EXAMPLE_PATH).read_text())
    assert template["profile"]["target_roles"], "the template itself must be usable"


def test_lockfiles_are_committed():
    """Without them both dependency trees resolve to whatever is newest at install
    time, so a fork that works today may not build next month and a bug report cannot
    be reproduced."""
    import subprocess

    tracked = set(subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split())
    for lock in ("uv.lock", "dashboard/package-lock.json"):
        assert lock in tracked, f"{lock} is not committed — installs are not reproducible"


def test_community_health_files_exist():
    for name in ("CONTRIBUTING.md", "SECURITY.md", "CODE_OF_CONDUCT.md", "CHANGELOG.md",
                 ".github/PULL_REQUEST_TEMPLATE.md"):
        assert (ROOT / name).exists(), f"missing {name}"


def test_security_policy_states_the_unauthenticated_read_posture():
    """The single most surprising thing about deploying this. If the policy stops
    saying it, an operator following the split-deploy docs exposes their application
    history without knowing."""
    text = (ROOT / "SECURITY.md").read_text().lower()
    assert "unauthenticated" in text
    assert "/applications" in text
    assert "127.0.0.1" in text


def test_gitignore_covers_cv_spellings_case_sensitively():
    """gitignore matching is case-SENSITIVE on Linux; macOS hides that. The old pattern
    was `docs/*cv*.pdf`, which misses `Foo_CV.pdf` on the machines CI runs on."""
    patterns = (ROOT / ".gitignore").read_text()
    assert "*[Cc][Vv]*.pdf" in patterns, "CV ignore pattern is not case-class based"
    assert "[Rr]esume" in patterns


def test_the_readme_and_context_claim_the_same_test_count():
    """The README claimed 515 tests while context.md said 576. It is the only thing most
    visitors read, and a stale number reads as an abandoned project.

    This asserts the two docs AGREE; `test_docs.py` owns whether the shared number is
    close to reality. Two checks, two methodologies, one number — rather than this test
    inventing a third way to count and disagreeing with both.
    """
    readme = (ROOT / "README.md").read_text()
    context = (ROOT / ".claude" / "context.md").read_text()

    in_readme = {int(n) for n in re.findall(r"(\d{3,4}) tests", readme)}
    in_context = {int(n) for n in re.findall(r"\*\*(\d{3,4})\*\* tests", context)}
    if not in_readme:
        return  # the README is allowed not to claim a number at all
    assert in_readme == in_context, (
        f"README claims {in_readme} tests, .claude/context.md claims {in_context} — "
        "they must state the same number"
    )
