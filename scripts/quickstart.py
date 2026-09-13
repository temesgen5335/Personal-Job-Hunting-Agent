"""One-command keyless first run: ensure a ready `.env`, seed a demo store, print how to
launch. `make quickstart`.

All logic lives in tested helpers (setup_wizard.bootstrap_env, seed_demo.seed_if_empty);
this file is only the I/O glue, so no automated test drives it (it writes the real .env).
"""

import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from jobagent.config import get_settings              # noqa: E402
from jobagent.secrets_store import SecretStore         # noqa: E402
from jobagent.setup_wizard import bootstrap_env, parse_env  # noqa: E402
from seed_demo import seed_if_empty                    # noqa: E402


def main() -> None:
    env_path = ROOT / ".env"
    example = ROOT / ".env.example"
    existing = (env_path.read_text() if env_path.exists()
                else example.read_text() if example.exists() else "")
    password = secrets.token_urlsafe(18)
    body = bootstrap_env(existing, password=password, master_key=SecretStore.generate_key())
    env_path.write_text(body)
    shown = parse_env(body)["DASHBOARD_PASSWORD"]

    settings = get_settings()
    stats = seed_if_empty(settings.db_path)
    if stats is not None:
        print(f"✅ seeded {stats['total_jobs']} demo jobs into {settings.db_path}")
    else:
        print(f"ℹ️  {settings.db_path} already has jobs — left it untouched")

    print()
    print("  Dashboard password (write this down — it unlocks Settings & actions):")
    print(f"    {shown}")
    print()
    print("  Launch it:")
    print("    make run        # dashboard on http://127.0.0.1:1234")
    print()
    print("  When you want your own jobs (no keys needed):")
    print("    make setup      # optional: set your profile")
    print("    make pipeline   # a real, zero-key pull")


if __name__ == "__main__":
    main()
