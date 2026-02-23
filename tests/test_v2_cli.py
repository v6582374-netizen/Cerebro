from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from wechat_agent.cli import app
from wechat_agent.config import get_settings
from wechat_agent.models import ArticleRef
from wechat_agent.schemas import CoverageReport, DiscoveredArticleRef, DiscoveryResult

runner = CliRunner()


@pytest.fixture
def v2_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db_path = tmp_path / "wechat_agent_v2_test.db"
    env_path = tmp_path / ".env"
    monkeypatch.setenv("WECHAT_AGENT_DB_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("SOURCE_TEMPLATES", "https://example.com/rss/{wechat_id}")
    monkeypatch.setenv("DEFAULT_VIEW_MODE", "source")
    monkeypatch.setenv("WECHAT_AGENT_ENV_FILE", str(env_path))
    monkeypatch.setenv("DISCOVERY_V2_ENABLED", "true")
    monkeypatch.setenv("SESSION_BACKEND", "file")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("AI_PROVIDER", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_sub_add_auto_bind_in_v2(v2_env, monkeypatch):
    def fake_discover(self, session, sub, target_date, since):
        url = "https://mp.weixin.qq.com/s?__biz=testbiz&mid=1&idx=1&sn=abc"
        session.add(
            ArticleRef(
                subscription_id=sub.id,
                url=url,
                title_hint="测试文章",
                channel="search_index",
                confidence=0.9,
            )
        )
        session.flush()
        return DiscoveryResult(
            ok=True,
            refs=[
                DiscoveredArticleRef(
                    url=url,
                    title_hint="测试文章",
                    published_at_hint=datetime.now(timezone.utc),
                    channel="search_index",
                    confidence=0.9,
                )
            ],
            channel_used="search_index",
            error_kind=None,
            error_message=None,
            latency_ms=15,
            status="SUCCESS",
        )

    monkeypatch.setattr("wechat_agent.services.discovery_orchestrator.DiscoveryOrchestrator.discover", fake_discover)

    added = runner.invoke(app, ["sub", "add", "--name", "测试号"])
    assert added.exit_code == 0
    assert "已新增订阅" in added.stdout
    assert "已自动绑定候选" in added.stdout

    listed = runner.invoke(app, ["sub", "list"])
    assert listed.exit_code == 0
    assert "测试号" in listed.stdout
    assert "SUCCESS" in listed.stdout
    assert "ACTIVE" in listed.stdout


def test_coverage_command_shows_error_distribution(v2_env, monkeypatch):
    report = CoverageReport(
        date=date.today(),
        total_subs=3,
        success_subs=1,
        delayed_subs=1,
        fail_subs=1,
        coverage_ratio=2 / 3,
        detail_json=json.dumps(
            [
                {"name": "号A", "wechat_id": "a", "status": "SUCCESS", "error_kind": ""},
                {"name": "号B", "wechat_id": "b", "status": "DELAYED", "error_kind": "TIMEOUT"},
                {"name": "号C", "wechat_id": "c", "status": "FAILED", "error_kind": "AUTH_EXPIRED"},
            ],
            ensure_ascii=False,
        ),
    )

    monkeypatch.setattr("wechat_agent.services.coverage_service.CoverageService.compute", lambda *args, **kwargs: report)
    out = runner.invoke(app, ["coverage", "--date", date.today().isoformat()])
    assert out.exit_code == 0
    assert "覆盖率报告" in out.stdout
    assert "失败原因分布" in out.stdout
    assert "AUTH_EXPIRED" in out.stdout
