"""Print a fresh JOBAGENT_MASTER_KEY (Fernet key) for the encrypted config store.
Run: .venv/bin/python scripts/genkey.py  → paste the value into .env
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jobagent.secrets_store import SecretStore  # noqa: E402

if __name__ == "__main__":
    print(SecretStore.generate_key())
