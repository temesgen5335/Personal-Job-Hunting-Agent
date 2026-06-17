"""Push the current top-matches digest to your Telegram. Hermes cron calls this
(e.g. every morning). One-shot — does not run the interactive bot.

Usage: .venv/bin/python scripts/send_digest.py [N]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jobagent.bot.notify import send_message  # noqa: E402
from jobagent.bot.service import jobs_text  # noqa: E402
from jobagent.config import get_settings  # noqa: E402
from jobagent.store import Store  # noqa: E402


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    s = get_settings()
    store = Store(s.db_path)
    store.init_schema()
    text = jobs_text(store, n)
    store.close()
    sent = send_message(s.telegram_bot_token, s.telegram_destination, text)
    print(f"Digest sent in {sent} message(s) to chat {s.telegram_destination}.")


if __name__ == "__main__":
    main()
