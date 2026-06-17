"""Run the interactive Telegram bot (long-running; polling).

Usage: .venv/bin/python scripts/run_bot.py
Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env, and the [telegram] extra.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jobagent.bot.app import build_application  # noqa: E402
from jobagent.config import get_settings  # noqa: E402


def main() -> None:
    s = get_settings()
    if not s.telegram_bot_token:
        sys.exit("Set TELEGRAM_BOT_TOKEN in .env (from @BotFather).")
    if not s.telegram_destination:
        sys.exit("Set TELEGRAM_CHAT_ID in .env (your numeric user id).")
    app = build_application(s.telegram_bot_token, s.telegram_destination, s.db_path)
    print(f"Bot running (owner chat_id={s.telegram_destination}). Ctrl-C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
