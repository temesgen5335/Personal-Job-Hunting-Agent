"""Is the search profile still the shipped template?

`make check` calls this. It is a WARNING, not a failure: a generic profile still runs,
still ingests, and still ranks — it just ranks generically, and the operator should
know that rather than conclude the matcher is broken. Failing here would block `make
run` on a first clone, which is exactly when someone wants to see it work at all.

Exit code is always 0. The output is the product.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jobagent.preferences import DEFAULT_PATH, EXAMPLE_PATH, load_preferences  # noqa: E402

# Only these fields are checked. The omissions are deliberate: `work_mode = "remote"`,
# `must_haves = ["remote"]` and `exclude_keywords` are values a real operator plausibly
# lands on unchanged, so equality with the template is not evidence of anything. The
# fields below are either identity (obvious placeholders) or the ones that actually
# define the search — matching those means the profile was never made yours.
CHECKED = (
    "name", "headline", "email", "phone", "cv_path", "location", "timezone",
    "target_roles", "core_skills", "domains", "keywords", "skill_weights",
)


def _template_values() -> dict:
    """Read the shipped template rather than duplicating its values here, so editing
    the template can never leave this check quietly stale."""
    import tomllib

    if not Path(EXAMPLE_PATH).exists():
        return {}
    prof = tomllib.loads(Path(EXAMPLE_PATH).read_text()).get("profile", {})
    return {k: v for k, v in prof.items() if k in CHECKED}


def _template_watchlist() -> dict:
    import tomllib

    if not Path(EXAMPLE_PATH).exists():
        return {}
    w = tomllib.loads(Path(EXAMPLE_PATH).read_text()).get("watchlist", {})
    return {k: w.get(k, []) for k in ("greenhouse", "lever", "ashby")}


def main() -> None:
    using_template = not Path(DEFAULT_PATH).exists()
    prefs = load_preferences()
    profile = prefs.profile
    tmpl = _template_values()

    unedited = [
        key for key, value in tmpl.items()
        if getattr(profile, key, None) == value and value not in ("", [], None)
    ]
    # The watchlist decides which employers get polled directly, so shipping someone
    # else's is as wrong as shipping their skills.
    tmpl_watch = _template_watchlist()
    if tmpl_watch and {
        "greenhouse": prefs.watchlist.greenhouse,
        "lever": prefs.watchlist.lever,
        "ashby": prefs.watchlist.ashby,
    } == tmpl_watch:
        unedited.append("watchlist")

    if using_template:
        print(f"ℹ️  no {DEFAULT_PATH} — running on the shipped template "
              f"({EXAMPLE_PATH}).")
        print(f"   cp {EXAMPLE_PATH} {DEFAULT_PATH}   (or edit it in the dashboard: "
              f"Settings → Profile)")

    if unedited:
        shown = ", ".join(sorted(unedited)[:6])
        more = f" (+{len(unedited) - 6} more)" if len(unedited) > 6 else ""
        print(f"⚠️  profile still has template values: {shown}{more}")
        print("   Matching scores every posting against these, so results will be "
              "generic until you set your own roles, skills and weights.")
    elif not using_template:
        n = len(profile.target_roles), len(profile.core_skills)
        print(f"✅ profile personalised — {n[0]} target role(s), {n[1]} core skill(s)")


if __name__ == "__main__":
    main()
