#!/usr/bin/env bash
# Install the systemd units for the Personal Job Agent. Run ON THE VPS with sudo.
# Substitutes the real repo path / user / venv python into the unit templates.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-$USER}"
PYTHON="$REPO/.venv/bin/python"
UNIT_DIR="/etc/systemd/system"

if [[ ! -x "$PYTHON" ]]; then
  echo "venv python not found at $PYTHON — run: uv venv && uv pip install -e '.[telegram,llm]'" >&2
  exit 1
fi

echo "Repo:   $REPO"
echo "User:   $RUN_USER"
echo "Python: $PYTHON"

for unit in jobagent-bot.service jobagent-pipeline.service jobagent-pipeline.timer \
            jobagent-ingest.service jobagent-ingest.timer; do
  sed -e "s|__REPO__|$REPO|g" \
      -e "s|__USER__|$RUN_USER|g" \
      -e "s|__PYTHON__|$PYTHON|g" \
      "$REPO/deploy/$unit" | sudo tee "$UNIT_DIR/$unit" >/dev/null
  echo "installed $unit"
done

sudo systemctl daemon-reload
sudo systemctl enable --now jobagent-bot.service
sudo systemctl enable --now jobagent-pipeline.timer
sudo systemctl enable --now jobagent-ingest.timer

echo
echo "Done. Check status:"
echo "  systemctl status jobagent-bot.service"
echo "  systemctl list-timers 'jobagent-*'"
echo "  journalctl -u jobagent-bot -f"
