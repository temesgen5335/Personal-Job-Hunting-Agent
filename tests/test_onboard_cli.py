"""onboard.py's non-interactive paths. The interactive prompts are a manual smoke;
these cover --example-config and --config (the agent-onboarding hook), and prove no
run writes outside the paths it is told to."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_example_config_prints_valid_json():
    r = subprocess.run([sys.executable, "scripts/onboard.py", "--example-config"],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)                       # must be valid JSON
    assert "target_roles" in data and "remote_scope" in data


def test_config_run_writes_env_and_overlay(tmp_path):
    cfg = tmp_path / "onboard.json"
    cfg.write_text(json.dumps({
        "name": "Smoke User", "target_roles": "AI Engineer",
        "core_skills": ["Python"], "remote_scope": "global", "keyless": True,
        "dashboard_password": "smoke-pw",
    }))
    env = tmp_path / ".env"
    overlay = tmp_path / "profile.json"
    r = subprocess.run(
        [sys.executable, "scripts/onboard.py", "--config", str(cfg),
         "--env", str(env), "--overlay", str(overlay)],
        cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    envtext = env.read_text()
    assert "DASHBOARD_PASSWORD=smoke-pw" in envtext
    assert "JOBAGENT_MASTER_KEY=" in envtext                  # generated
    prof = json.loads(overlay.read_text())["profile"]
    assert prof["name"] == "Smoke User"
    assert prof["target_roles"] == ["AI Engineer"]
    assert prof["remote_scope"] == "global"


def test_print_only_writes_nothing(tmp_path):
    cfg = tmp_path / "onboard.json"; cfg.write_text(json.dumps({"name": "X", "dashboard_password": "p"}))
    env = tmp_path / ".env"; overlay = tmp_path / "profile.json"
    r = subprocess.run(
        [sys.executable, "scripts/onboard.py", "--config", str(cfg),
         "--env", str(env), "--overlay", str(overlay), "--print-only"],
        cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert not env.exists() and not overlay.exists()          # nothing written
