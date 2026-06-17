#!/usr/bin/env bash
# One-shot VPS bootstrap for the Personal Job Agent. Run ON THE VPS.
# Installs system deps, the project venv, then points you at the rest of the runbook.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

echo "==> Installing uv (Python package/venv manager) if missing"
command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

echo "==> Creating venv + installing project with all runtime extras"
uv venv
uv pip install -e ".[telegram,llm,mcp,apply]"

echo "==> Initializing the store"
.venv/bin/python scripts/init_db.py

cat <<'NOTE'

Bootstrap done. Remaining manual steps (see docs/DEPLOYMENT.md):

  1. Create .env from .env.example and fill in:
       OPENROUTER_API_KEY, TELEGRAM_* (bot token, chat id, api_id/hash, channels)
  2. One-time Telegram login (creates the .session):
       .venv/bin/python scripts/telegram_login.py
  3. Smoke-test the pipeline without sending:
       .venv/bin/python scripts/pipeline.py --no-send
  4. Install services (bot + timers):
       sudo bash scripts/install_services.sh
  5. (Optional) Set server timezone so the 07:00 digest is your local time:
       sudo timedatectl set-timezone Africa/Addis_Ababa

  Hermes Agent (the agentic brain for Phase 3 cv_tailor etc.) is independent of
  the systemd pipeline above. Install it when you reach Phase 3:
       curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
NOTE
