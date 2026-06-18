"""Run the interactive Telegram bot (long-running; polling).

Usage: .venv/bin/python scripts/run_bot.py
Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env, and the [telegram] extra.
/apply also needs OPENROUTER_API_KEY (drafting) and SMTP_* (sending).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jobagent.bot.app import build_application  # noqa: E402
from jobagent.config import get_settings  # noqa: E402
from jobagent.llm_client import from_settings as llm_from_settings  # noqa: E402
from jobagent.preferences import load_preferences  # noqa: E402


def main() -> None:
    s = get_settings()
    if not s.telegram_bot_token:
        sys.exit("Set TELEGRAM_BOT_TOKEN in .env (from @BotFather).")
    if not s.telegram_destination:
        sys.exit("Set TELEGRAM_CHAT_ID in .env (your numeric user id).")

    profile = load_preferences().profile
    llm = llm_from_settings(s)  # None if no OPENROUTER_API_KEY → /apply reports it
    cv_master = ""
    cv_path = Path("config/cv_master.md")
    if cv_path.exists():
        cv_master = cv_path.read_text()

    app = build_application(s, profile, llm, cv_master)
    print(f"Bot running (owner chat_id={s.telegram_destination}, apply={'on' if llm else 'off'}). Ctrl-C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
