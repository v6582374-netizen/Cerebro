from __future__ import annotations

from pathlib import Path

import pytest

from wechat_agent.config import get_settings


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db_path = tmp_path / "wechat_agent_test.db"
    env_path = tmp_path / ".env"
    monkeypatch.setenv("WECHAT_AGENT_DB_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("SOURCE_TEMPLATES", "https://example.com/rss/{wechat_id}")
    monkeypatch.setenv("DEFAULT_VIEW_MODE", "source")
    monkeypatch.setenv("WECHAT_AGENT_ENV_FILE", str(env_path))
    monkeypatch.setenv("DISCOVERY_V2_ENABLED", "false")
    monkeypatch.setenv("WECHAT_WEB_ENABLED", "false")
    monkeypatch.setenv("SESSION_BACKEND", "file")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("AI_PROVIDER", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
