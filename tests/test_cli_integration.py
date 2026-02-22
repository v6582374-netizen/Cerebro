from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import os

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
    assert "标题(可点击)" in source_out.stdout
    assert "https://example.com/test" in source_out.stdout
    assert "https://example.com/test" in time_out.stdout
    assert "https://example.com/test" in recommend_out.stdout
    assert "AI: provider=" in source_out.stdout
    assert "AI: provider=" in time_out.stdout
    assert "AI: provider=" in recommend_out.stdout

    mark = runner.invoke(app, ["read", "mark", "--article-id", "1", "--state", "read"])
    assert mark.exit_code == 0
    assert "AI: provider=" in mark.stdout

    recommend_after_mark = runner.invoke(app, ["view", "--mode", "recommend", "--date", today])
    assert recommend_after_mark.exit_code == 0
    assert "[x]" in recommend_after_mark.stdout
    assert "AI: provider=" in recommend_after_mark.stdout


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
    assert "AI: provider=" in interactive.stdout

    after = runner.invoke(app, ["view", "--mode", "source", "--date", today, "--no-interactive"])
    assert after.exit_code == 0
    assert "[x]" in after.stdout
    assert "AI: provider=" in after.stdout


def test_non_view_commands_append_ai_footer(isolated_env):
    empty_list = runner.invoke(app, ["sub", "list"])
    empty_status = runner.invoke(app, ["status"])
    not_found = runner.invoke(app, ["read", "mark", "--article-id", "999", "--state", "read"])

    assert empty_list.exit_code == 0
    assert empty_status.exit_code == 0
    assert not_found.exit_code == 0

    assert "AI: provider=" in empty_list.stdout
    assert "AI: provider=" in empty_status.stdout
    assert "AI: provider=" in not_found.stdout


def test_quick_alias_commands(isolated_env, monkeypatch):
    monkeypatch.setattr("wechat_agent.providers.template_feed_provider.TemplateFeedProvider.fetch", _fake_fetch)
    monkeypatch.setattr("wechat_agent.providers.template_feed_provider.TemplateFeedProvider.probe", _fake_probe)
    monkeypatch.setattr(Summarizer, "summarize", _fake_summary)

    add = runner.invoke(app, ["add", "-n", "号A", "-i", "gh_a"])
    assert add.exit_code == 0

    list_out = runner.invoke(app, ["list"])
    assert list_out.exit_code == 0
    assert "号A" in list_out.stdout

    today = datetime.now().strftime("%Y-%m-%d")
    show_out = runner.invoke(app, ["show", "-m", "source", "-d", today, "--no-interactive"])
    assert show_out.exit_code == 0
    assert "CLI 集成测试文章" in show_out.stdout

    done_out = runner.invoke(app, ["done", "-i", "1"])
    assert done_out.exit_code == 0
    assert "已批量更新 1 篇文章状态为: read" in done_out.stdout

    todo_out = runner.invoke(app, ["todo", "-i", "1"])
    assert todo_out.exit_code == 0
    assert "已批量更新 1 篇文章状态为: unread" in todo_out.stdout


def test_open_command(isolated_env, monkeypatch):
    monkeypatch.setattr("wechat_agent.providers.template_feed_provider.TemplateFeedProvider.fetch", _fake_fetch)
    monkeypatch.setattr("wechat_agent.providers.template_feed_provider.TemplateFeedProvider.probe", _fake_probe)
    monkeypatch.setattr(Summarizer, "summarize", _fake_summary)

    add = runner.invoke(app, ["add", "-n", "号A", "-i", "gh_a"])
    assert add.exit_code == 0
    today = datetime.now().strftime("%Y-%m-%d")
    show = runner.invoke(app, ["show", "-m", "source", "-d", today, "--no-interactive"])
    assert show.exit_code == 0

    monkeypatch.setattr("wechat_agent.cli.webbrowser.open", lambda *_args, **_kwargs: True)
    opened = runner.invoke(app, ["open", "--article-id", "1"])
    assert opened.exit_code == 0
    assert "已尝试打开文章:" in opened.stdout
    assert "AI: provider=" in opened.stdout


def test_config_api_interactive_writes_env(isolated_env):
    env_path = Path(os.environ["WECHAT_AGENT_ENV_FILE"])
    if env_path.exists():
        env_path.unlink()

    configured = runner.invoke(
        app,
        ["config", "api"],
        input="openai\n\nsk-test-key\n",
    )
    assert configured.exit_code == 0
    assert "配置已保存" in configured.stdout
    assert "AI: provider=openai" in configured.stdout

    content = env_path.read_text(encoding="utf-8")
    assert "AI_PROVIDER=openai" in content
    assert "OPENAI_BASE_URL=https://api.openai.com/v1" in content
    assert "OPENAI_API_KEY=sk-test-key" in content

    shown = runner.invoke(app, ["config", "show"])
    assert shown.exit_code == 0
    assert "OPENAI_BASE_URL=https://api.openai.com/v1" in shown.stdout
    assert "OPENAI_API_KEY=sk-t...-key" in shown.stdout
    assert "AI: provider=openai" in shown.stdout
