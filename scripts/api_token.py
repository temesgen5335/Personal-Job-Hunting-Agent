"""Print the API bearer token for this install.

Needed when `JOBAGENT_REQUIRE_AUTH_READS` is on: the dashboard renders reads
server-side and has no browser session to borrow, so it carries a token of its own in
`JOBAGENT_API_TOKEN`.

The token is derived, not stored — `sha256(password|master_key)` — so this prints the
same value `POST /auth/login` returns, without needing the API to be running.

    JOBAGENT_API_TOKEN=$(python scripts/api_token.py)
"""

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jobagent.config import get_settings  # noqa: E402

if __name__ == "__main__":
    settings = get_settings()
    if not settings.dashboard_password:
        print("DASHBOARD_PASSWORD is not set — there is no token to derive.",
              file=sys.stderr)
        raise SystemExit(1)
    print(hashlib.sha256(
        f"{settings.dashboard_password}|{settings.master_key}".encode()).hexdigest())
