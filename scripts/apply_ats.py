"""Tier-2 ATS form-fill (Greenhouse / Lever / Ashby) with a HITL gate.

Preview — fill the form + screenshot, DO NOT submit:
    python scripts/apply_ats.py preview <rank>

Review artifacts/ats_<rank>.png, then submit:
    python scripts/apply_ats.py submit <rank>

Needs the [apply] extra and a browser:  uv pip install -e ".[apply]" && playwright install chromium
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jobagent.apply.ats import ApplicantInfo, apply_target, apply_to_job  # noqa: E402
from jobagent.bot.service import resolve_ranked_job  # noqa: E402
from jobagent.config import get_settings  # noqa: E402
from jobagent.preferences import load_preferences  # noqa: E402
from jobagent.store import Store  # noqa: E402


def main() -> None:
    if len(sys.argv) < 3 or sys.argv[1] not in {"preview", "submit"}:
        print(__doc__)
        sys.exit(1)
    cmd, arg = sys.argv[1], sys.argv[2]
    settings = get_settings()
    profile = load_preferences().profile
    store = Store(settings.db_path)
    store.init_schema()

    job = resolve_ranked_job(store, int(arg)) if arg.isdigit() else None
    store.close()
    if not job:
        sys.exit(f"No ranked job #{arg}. Run scripts/match.py first.")

    platform, url = apply_target(job)
    if platform is None:
        sys.exit(f"Not a supported ATS (Greenhouse/Lever/Ashby): {url}")
    print(f"Resolved {platform} form: {url}")

    Path("artifacts").mkdir(exist_ok=True)
    shot = f"artifacts/ats_{arg}.png"
    applicant = ApplicantInfo.from_profile(profile)
    submit = cmd == "submit"

    result = apply_to_job(platform, url, applicant, shot, submit=submit, headless=True)
    print(result.summary())
    print(f"Screenshot: {shot}")
    if not submit and not result.captcha_detected:
        print(f"\nReview the screenshot, then:  python scripts/apply_ats.py submit {arg}")
    if result.captcha_detected:
        print(f"\nFinish manually here: {url}")


if __name__ == "__main__":
    main()
