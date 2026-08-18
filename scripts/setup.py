"""Interactive first-run setup. `make setup`.

The only part of setup that touches stdin — all logic lives in
`jobagent.setup_wizard`, so it is testable without driving a terminal.

Safe to re-run: existing values are offered as defaults and kept on a bare Enter, and
`.env` is updated key-by-key rather than rewritten, so nothing you tuned by hand is lost.
"""

import argparse
import getpass
import json
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from jobagent.preferences import load_preferences  # noqa: E402
from jobagent.secrets_store import SecretStore  # noqa: E402
from jobagent.setup_wizard import (  # noqa: E402
    ENV_TEMPLATE,
    Answers,
    env_updates,
    merge_env,
    next_steps,
    parse_env,
    profile_overlay,
    split_list,
)

ENV_PATH = ROOT / ".env"
OVERLAY_PATH = ROOT / "data" / "profile.json"

PROVIDERS = ("groq", "gemini", "openrouter", "openai", "anthropic")


def ask(prompt: str, default: str = "") -> str:
    shown = f" [{default}]" if default else ""
    try:
        got = input(f"  {prompt}{shown}: ").strip()
    except EOFError:
        return default
    return got or default


def ask_yes(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    got = ask(f"{prompt} ({d})").lower()
    return default if not got else got.startswith("y")


def ask_secret(prompt: str, already_set: bool) -> str:
    """Never echoes, and never reveals the stored value — only whether one exists."""
    note = " [already set — Enter to keep]" if already_set else ""
    try:
        return getpass.getpass(f"  {prompt}{note}: ").strip()
    except EOFError:
        return ""


def main() -> None:
    ap = argparse.ArgumentParser(description="Configure a fresh install.")
    ap.add_argument("--print-only", action="store_true",
                    help="show what would be written, change nothing")
    args = ap.parse_args()

    env_text = ENV_PATH.read_text() if ENV_PATH.exists() else ENV_TEMPLATE
    env = parse_env(env_text)
    prefs = load_preferences()
    profile = prefs.profile

    print("\n  Personal Job Agent — setup")
    print("  Enter to accept the value in brackets. Ctrl-C to stop; nothing is written")
    print("  until the end.\n")

    a = Answers()

    print("  ── You ────────────────────────────────────────────")
    a.name = ask("Your name", profile.name if profile.name != "Your Name" else "")
    a.headline = ask("One-line headline",
                     profile.headline if "Your one-line" not in profile.headline else "")
    a.email = ask("Email (used on application forms)",
                  profile.email if "example.com" not in profile.email else "")

    print("\n  ── What you're looking for ────────────────────────")
    print("  These drive every match score, so be specific.")
    a.target_roles = split_list(ask(
        "Target roles, comma-separated", ", ".join(profile.target_roles[:3])))
    a.core_skills = split_list(ask(
        "Core skills, comma-separated", ", ".join(profile.core_skills[:5])))
    a.seniority = ask("Seniority (junior/mid/senior/staff)", profile.seniority or "mid")
    a.location = ask("Where you are",
                     profile.location if "Your City" not in profile.location else "")
    a.remote_only = ask_yes("Remote only?", True)

    print("\n  ── Access ─────────────────────────────────────────")
    print("  The dashboard password gates every write — applying, editing, config.")
    print("  Without it the app is read-only (that is the fail-closed default).")
    pw = ask_secret("Dashboard password", bool(env.get("DASHBOARD_PASSWORD")))
    if not pw and not env.get("DASHBOARD_PASSWORD"):
        pw = secrets.token_urlsafe(18)
        print(f"    generated one for you: {pw}")
        print("    (stored in .env — copy it somewhere safe now)")
    a.dashboard_password = pw

    master_key = env.get("JOBAGENT_MASTER_KEY") or SecretStore.generate_key()
    if not env.get("JOBAGENT_MASTER_KEY"):
        print("    generated JOBAGENT_MASTER_KEY (encrypts credentials saved in Settings)")

    print("\n  ── Optional ───────────────────────────────────────")
    print("  Skip all of this: five of six job sources are public APIs and matching")
    print("  falls back to heuristics, so the agent works with no keys at all.")
    if ask_yes("Add an LLM key now? (enables CV tailoring + cover letters)", False):
        a.llm_provider = ask(f"Provider ({'/'.join(PROVIDERS)})", "groq")
        if a.llm_provider not in PROVIDERS:
            print(f"    unknown provider {a.llm_provider!r} — skipping")
            a.llm_provider = ""
        else:
            a.llm_api_key = ask_secret(f"{a.llm_provider} API key", False)
    if ask_yes("Add a Telegram bot now? (digest + /jobs bot)", False):
        a.telegram_bot_token = ask_secret("Bot token from @BotFather", False)
        a.telegram_chat_id = ask("Your numeric chat id", env.get("TELEGRAM_CHAT_ID", ""))

    # --- write -------------------------------------------------------------------
    updates = env_updates(a, master_key=master_key)
    new_env = merge_env(env_text, updates)
    existing_overlay = {}
    if OVERLAY_PATH.exists():
        try:
            existing_overlay = json.loads(OVERLAY_PATH.read_text() or "{}")
        except ValueError:
            existing_overlay = {}
    overlay = profile_overlay(a, existing_overlay)

    print("\n  ── Writing ────────────────────────────────────────")
    print(f"  .env                 {len(updates)} key(s): "
          f"{', '.join(sorted(updates))}")
    print(f"  data/profile.json    profile for {overlay['profile'].get('name') or '(unnamed)'}")

    if args.print_only:
        print("\n  --print-only: nothing written.")
        return

    ENV_PATH.write_text(new_env)
    OVERLAY_PATH.parent.mkdir(parents=True, exist_ok=True)
    OVERLAY_PATH.write_text(json.dumps(overlay, indent=2) + "\n")

    print("\n  ✅ Done.\n")
    for step in next_steps(a, has_llm=bool(a.llm_api_key or any(
                               env.get(f"{p.upper()}_API_KEY") for p in PROVIDERS)),
                           has_telegram=bool(a.telegram_bot_token or env.get("TELEGRAM_BOT_TOKEN"))):
        print(f"    {step}")
    print()


if __name__ == "__main__":
    main()
