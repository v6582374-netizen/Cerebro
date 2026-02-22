from __future__ import annotations

from pathlib import Path

import pytest

from wechat_agent.config import get_settings


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db_path = tmp_path / "wechat_agent_test.db"
    monkeypatch.setenv("WECHAT_AGENT_DB_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("SOURCE_TEMPLATES", "https://example.com/rss/{wechat_id}")
    monkeypatch.setenv("DEFAULT_VIEW_MODE", "source")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
