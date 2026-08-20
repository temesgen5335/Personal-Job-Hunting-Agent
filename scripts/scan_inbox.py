"""Read the applying mailbox and record outcome proposals. `make inbox`.

Proposes only — nothing is applied. Review and confirm in the dashboard
(Applications → detected outcomes) or via POST /inbox/proposals/{id}.

    python scripts/scan_inbox.py --days 30
"""

import argparse
import imaplib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from jobagent.config import get_settings  # noqa: E402
from jobagent.inbox.reader import InboxReader, scan  # noqa: E402
from jobagent.store import Store  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Detect application outcomes in your inbox.")
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()

    settings = get_settings()
    if not settings.imap_host:
        print("IMAP_HOST is not set — inbox detection is off.")
        print("Set IMAP_HOST / IMAP_USER / IMAP_PASSWORD in .env to enable it.")
        raise SystemExit(1)

    conn = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    conn.login(settings.imap_user, settings.imap_password)
    store = Store(settings.db_path)
    store.init_schema()
    try:
        report = scan(store, InboxReader(conn, settings.imap_folder), days=args.days)
    finally:
        store.close()
        try:
            conn.logout()
        except Exception:  # noqa: BLE001 — logout failure must not mask the report
            pass

    print(f"examined {report.examined} message(s)")
    print(f"  proposed        {report.proposed}")
    print(f"  acknowledgements {report.acknowledgements}")
    print(f"  not replies     {report.skipped_not_a_reply}")
    print(f"  unattributable  {report.unmatched}")
    if report.proposed:
        print("\nNothing has been changed. Review them in the dashboard → Applications.")


if __name__ == "__main__":
    main()
