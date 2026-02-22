from __future__ import annotations

from datetime import datetime, timezone

from typer.testing import CliRunner

from wechat_agent.cli import app
from wechat_agent.schemas import RawArticle, SummaryResult
from wechat_agent.services.summarizer import Summarizer

runner = CliRunner()


def _fake_fetch(self, source_url: str, since):
    return [
        RawArticle(
            external_id=f"external-{source_url}",
            title="CLI 集成测试文章",
            url="https://example.com/test",
            published_at=datetime.now(timezone.utc),
            content_excerpt="这是一篇用于 CLI 集成测试的文章内容。",
            raw_hash="hash-cli",
        )
    ]


def _fake_probe(self, source_url: str):
    return True, None


def _fake_summary(self, article: RawArticle) -> SummaryResult:
    return SummaryResult(summary_text="这是一条用于集成测试的摘要内容，长度满足三十字。", model="fake", used_fallback=True)


def test_cli_view_modes_and_read_mark(isolated_env, monkeypatch):
    monkeypatch.setattr("wechat_agent.providers.template_feed_provider.TemplateFeedProvider.fetch", _fake_fetch)
    monkeypatch.setattr("wechat_agent.providers.template_feed_provider.TemplateFeedProvider.probe", _fake_probe)
    monkeypatch.setattr(Summarizer, "summarize", _fake_summary)

    add_a = runner.invoke(app, ["sub", "add", "--name", "号A", "--wechat-id", "gh_a"])
    add_b = runner.invoke(app, ["sub", "add", "--name", "号B", "--wechat-id", "gh_b"])

    assert add_a.exit_code == 0
    assert add_b.exit_code == 0

    today = datetime.now().strftime("%Y-%m-%d")

    source_out = runner.invoke(app, ["view", "--mode", "source", "--date", today])
    time_out = runner.invoke(app, ["view", "--mode", "time", "--date", today])
    recommend_out = runner.invoke(app, ["view", "--mode", "recommend", "--date", today])

    assert source_out.exit_code == 0
    assert time_out.exit_code == 0
    assert recommend_out.exit_code == 0

    assert "同步完成" in source_out.stdout
    assert "号A" in source_out.stdout
    assert "原文链接" in source_out.stdout
    assert "https://example.com/test" in source_out.stdout
    assert "AI摘要" in time_out.stdout
    assert "推荐分" in recommend_out.stdout
    assert "AI: summary=" in source_out.stdout
    assert "AI: summary=" in time_out.stdout
    assert "AI: summary=" in recommend_out.stdout

    mark = runner.invoke(app, ["read", "mark", "--article-id", "1", "--state", "read"])
    assert mark.exit_code == 0
    assert "AI: summary=" in mark.stdout

    recommend_after_mark = runner.invoke(app, ["view", "--mode", "recommend", "--date", today])
    assert recommend_after_mark.exit_code == 0
    assert "[x]" in recommend_after_mark.stdout
    assert "AI: summary=" in recommend_after_mark.stdout


def test_cli_test_prev_day_switch(isolated_env, monkeypatch):
    monkeypatch.setattr("wechat_agent.providers.template_feed_provider.TemplateFeedProvider.fetch", _fake_fetch)
    monkeypatch.setattr("wechat_agent.providers.template_feed_provider.TemplateFeedProvider.probe", _fake_probe)
    monkeypatch.setattr(Summarizer, "summarize", _fake_summary)

    add = runner.invoke(app, ["sub", "add", "--name", "号A", "--wechat-id", "gh_a"])
    assert add.exit_code == 0

    out = runner.invoke(app, ["view", "--mode", "source", "--test-prev-day"])
    assert out.exit_code == 0
    assert "测试模式: 已切换为前一天数据。" in out.stdout
    assert "AI: summary=" in out.stdout


def test_cli_view_interactive_read_toggle(isolated_env, monkeypatch):
    monkeypatch.setattr("wechat_agent.providers.template_feed_provider.TemplateFeedProvider.fetch", _fake_fetch)
    monkeypatch.setattr("wechat_agent.providers.template_feed_provider.TemplateFeedProvider.probe", _fake_probe)
    monkeypatch.setattr(Summarizer, "summarize", _fake_summary)

    add = runner.invoke(app, ["sub", "add", "--name", "号A", "--wechat-id", "gh_a"])
    assert add.exit_code == 0

    today = datetime.now().strftime("%Y-%m-%d")
    interactive = runner.invoke(
        app,
        ["view", "--mode", "source", "--date", today, "--interactive"],
        input="r 1\nq\n",
    )
    assert interactive.exit_code == 0
    assert "进入交互已读模式" in interactive.stdout
    assert "已更新 1 篇文章状态。" in interactive.stdout
    assert "AI: summary=" in interactive.stdout

    after = runner.invoke(app, ["view", "--mode", "source", "--date", today, "--no-interactive"])
    assert after.exit_code == 0
    assert "[x]" in after.stdout
    assert "AI: summary=" in after.stdout


def test_non_view_commands_append_ai_footer(isolated_env):
    empty_list = runner.invoke(app, ["sub", "list"])
    empty_status = runner.invoke(app, ["status"])
    not_found = runner.invoke(app, ["read", "mark", "--article-id", "999", "--state", "read"])

    assert empty_list.exit_code == 0
    assert empty_status.exit_code == 0
    assert not_found.exit_code == 0

    assert "AI: summary=" in empty_list.stdout
    assert "AI: summary=" in empty_status.stdout
    assert "AI: summary=" in not_found.stdout
