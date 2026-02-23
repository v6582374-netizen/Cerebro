from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from wechat_agent.cli import app
from wechat_agent.config import get_settings
from wechat_agent.db import init_db, session_scope
from wechat_agent.models import OfficialAccountEntry, Subscription, WeChatAccount

runner = CliRunner()


@pytest.fixture
def v3_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db_path = tmp_path / "wechat_agent_v3_test.db"
    env_path = tmp_path / ".env"
    config_home = tmp_path / "config_home"
    monkeypatch.setenv("WECHAT_AGENT_DB_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("WECHAT_AGENT_ENV_FILE", str(env_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("WECHAT_WEB_ENABLED", "true")
    monkeypatch.setenv("DISCOVERY_V2_ENABLED", "false")
    monkeypatch.setenv("STRICT_AUTH_REQUIRED", "true")
    monkeypatch.setenv("SESSION_PROVIDER", "wechat_web")
    monkeypatch.setenv("SESSION_BACKEND", "file")
    monkeypatch.setenv("EXTREME_LOCAL_MODE", "true")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("AI_PROVIDER", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_view_blocks_without_auth(v3_env):
    out = runner.invoke(app, ["view", "--mode", "source", "--no-interactive"])
    assert out.exit_code == 0
    assert "当前登录态无效" in out.stdout
    assert "wechat-agent login" in out.stdout
    assert "AI: provider=" in out.stdout


def test_auth_status_missing(v3_env):
    out = runner.invoke(app, ["auth", "status"])
    assert out.exit_code == 0
    assert "auth_status:" in out.stdout
    assert "state=missing" in out.stdout
    assert "provider=wechat_web" in out.stdout


def test_sub_bind_command(v3_env):
    settings = get_settings()
    init_db(settings)
    with session_scope(settings) as session:
        account = WeChatAccount(wxuin="100001", nickname="tester", status="ACTIVE")
        session.add(account)
        session.flush()
        sub = Subscription(name="测试号", wechat_id="auto_test_1")
        session.add(sub)
        session.flush()
        session.add(
            OfficialAccountEntry(
                account_id=account.id,
                user_name="gh_test_official",
                nick_name="测试号",
                verify_flag=8,
            )
        )
        session.commit()

    out = runner.invoke(app, ["sub", "bind", "--name", "测试号", "--account", "gh_test_official"])
    assert out.exit_code == 0
    assert "绑定成功" in out.stdout
