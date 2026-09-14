"""Full guided onboarding — `make onboard`. The curated, sectioned config that turns a
fresh clone into a personalized install: identity, search intent, geo scope, sources,
one LLM choice (paid key+model or keyless), optional email/Telegram, then the password.

Runs three ways:
  onboard.py                      interactive, sectioned, every question skippable
  onboard.py --config c.json      non-interactive from a JSON mapping (agents use this)
  onboard.py --example-config     print a copyable example config and exit
  --print-only                    compute and show what would be written; write nothing

Logic lives in tested helpers (setup_wizard, preferences); this file is I/O glue.
Secrets and env-shaped settings go to .env; profile/sources/watchlist to the overlay.
"""

import argparse
import json
import secrets
import sys
from getpass import getpass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from jobagent.preferences import save_overlay                       # noqa: E402
from jobagent.secrets_store import SecretStore                      # noqa: E402
from jobagent.setup_wizard import (                                 # noqa: E402
    Answers, ONBOARD_EXAMPLE, answers_from_mapping, env_updates,
    merge_env, parse_env, profile_overlay, split_list,
)

CORE_PROVIDERS = ("groq", "gemini", "openrouter", "openai", "anthropic", "cerebras")


def _ask(prompt: str, default: str = "") -> str:
    d = f" [{default}]" if default else ""
    got = input(f"{prompt}{d}: ").strip()
    return got or default


def _ask_bool(prompt: str, default: bool) -> bool:
    d = "Y/n" if default else "y/N"
    got = input(f"{prompt} ({d}): ").strip().lower()
    return default if not got else got.startswith("y")


def _interactive() -> Answers:
    print("\n== onboard: a few sections, every question is skippable (press enter) ==\n")
    a = Answers()
    print("-- You --")
    a.name = _ask("Name"); a.headline = _ask("Headline"); a.email = _ask("Email"); a.phone = _ask("Phone")
    print("-- What you're looking for --")
    a.target_roles = split_list(_ask("Target roles (comma-separated)"))
    a.core_skills = split_list(_ask("Core skills (comma-separated)"))
    a.seniority = _ask("Seniority", "mid"); a.location = _ask("Location"); a.timezone = _ask("Timezone")
    a.domains = split_list(_ask("Domains (comma-separated)"))
    a.remote_only = _ask_bool("Remote only?", True)
    if _ask_bool("Only genuinely global-remote roles (exclude location-locked)?", False):
        a.remote_scope = "global"
        a.geo_eligible = split_list(_ask("  Always-eligible locations (e.g. worldwide, emea)"))
        a.geo_blocked = split_list(_ask("  Always-exclude locations (e.g. remote us)"))
    print("-- Sources --  (leave blank to keep defaults)")
    for src in ("remoteok", "remotive", "himalayas", "greenhouse", "lever", "ashby", "telegram"):
        ans = input(f"  enable {src}? (Y/n, blank=default): ").strip().lower()
        if ans:
            a.sources[src] = ans.startswith("y")
    for board in ("greenhouse", "lever", "ashby"):
        slugs = split_list(_ask(f"  {board} company slugs (comma-separated)"))
        if slugs:
            a.watchlist[board] = slugs
    print("-- LLM --")
    if _ask_bool("Add a paid/keyed LLM provider now? (No = keyless)", False):
        prov = _ask(f"Provider {CORE_PROVIDERS}", "groq").lower()
        if prov in CORE_PROVIDERS:
            a.llm_provider = prov
            a.llm_api_key = getpass(f"  {prov} API key (hidden): ").strip()
            a.llm_model = _ask("  Model (blank = provider default)")
    else:
        a.keyless = True
        a.pollinations_enabled = _ask_bool("  Enable keyless Pollinations LLM (for CV drafting)?", False)
    print("-- Email (optional, for one-click email applications) --")
    if _ask_bool("Configure SMTP now?", False):
        a.smtp_host = _ask("  SMTP host"); a.smtp_port = _ask("  SMTP port", "587")
        a.smtp_user = _ask("  SMTP user"); a.smtp_password = getpass("  SMTP password (hidden): ").strip()
        a.apply_from_email = _ask("  From email", a.email)
    print("-- Telegram bot (optional) --")
    if _ask_bool("Add a Telegram bot now?", False):
        a.telegram_bot_token = getpass("  Bot token (hidden): ").strip()
        a.telegram_chat_id = _ask("  Chat id"); a.telegram_owner_id = _ask("  Owner id", a.telegram_chat_id)
    print("-- Access --")
    a.dashboard_password = getpass("Dashboard password (hidden, blank = auto-generate): ").strip()
    return a


def main() -> None:
    ap = argparse.ArgumentParser(description="Full guided onboarding.")
    ap.add_argument("--config", help="JSON mapping to run non-interactively")
    ap.add_argument("--example-config", action="store_true", help="print an example config and exit")
    ap.add_argument("--print-only", action="store_true", help="show what would be written; write nothing")
    ap.add_argument("--env", default=str(ROOT / ".env"))
    ap.add_argument("--overlay", default=None, help="overlay path (default: the profile overlay)")
    args = ap.parse_args()

    if args.example_config:
        print(json.dumps(ONBOARD_EXAMPLE, indent=2))
        return

    if args.config:
        answers = answers_from_mapping(json.loads(Path(args.config).read_text()))
    else:
        answers = _interactive()

    if not answers.dashboard_password:
        answers.dashboard_password = secrets.token_urlsafe(18)

    env_path = Path(args.env)
    existing_env = env_path.read_text() if env_path.exists() else ""
    master = parse_env(existing_env).get("JOBAGENT_MASTER_KEY") or SecretStore.generate_key()
    new_env = merge_env(existing_env, env_updates(answers, master_key=master))
    overlay = profile_overlay(answers)

    if args.print_only:
        print("[print-only] .env keys:", ", ".join(sorted(parse_env(new_env))))
        print("[print-only] overlay sections:", ", ".join(sorted(overlay)))
        return

    env_path.write_text(new_env)
    save_overlay(overlay, overlay_path=args.overlay)   # args.overlay=None → the real overlay
    print(f"✅ onboard complete. Dashboard password: {answers.dashboard_password}")
    print("   Next:  make pipeline   # a real pull (keyless works)   then   make run")


if __name__ == "__main__":
    main()
