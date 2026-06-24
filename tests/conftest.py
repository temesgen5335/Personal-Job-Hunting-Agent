import pytest

import jobagent.config as cfg


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """Settings are process-cached; reset around each test for isolation."""
    cfg._cached = None
    yield
    cfg._cached = None
