"""Config robustness: blank env vars (as CI injects for unset secrets) must not crash."""

from jobagent.config import Settings


def test_blank_optional_ints_become_none(monkeypatch):
    # GitHub Actions sets `FOO: ${{ secrets.MISSING }}` to "" — must not ValidationError.
    for var in ("TELEGRAM_CHAT_ID", "TELEGRAM_API_ID", "TELEGRAM_OWNER_ID"):
        monkeypatch.setenv(var, "")
    s = Settings(_env_file=None)
    assert s.telegram_chat_id is None
    assert s.telegram_api_id is None
    assert s.telegram_destination is None


def test_real_int_values_still_parse(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    s = Settings(_env_file=None)
    assert s.telegram_chat_id == 12345
    assert s.telegram_destination == 12345
