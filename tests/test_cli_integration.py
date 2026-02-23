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


def _fake_fetch_partial(self, source_url: str, since):
    if source_url.endswith("/gh_b"):
        return []
    return _fake_fetch(self, source_url, since)


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

    mark = runner.invoke(app, ["read", "mark", "--id", "1", "--date", today, "--state", "read"])
    assert mark.exit_code == 0
    assert "AI: provider=" in mark.stdout

    recommend_after_mark = runner.invoke(app, ["view", "--mode", "recommend", "--date", today])
    assert recommend_after_mark.exit_code == 0
    assert "[x]" in recommend_after_mark.stdout
    assert "AI: provider=" in recommend_after_mark.stdout


def test_source_view_shows_all_subscriptions_even_without_updates(isolated_env, monkeypatch):
    monkeypatch.setattr("wechat_agent.providers.template_feed_provider.TemplateFeedProvider.fetch", _fake_fetch_partial)
    monkeypatch.setattr("wechat_agent.providers.template_feed_provider.TemplateFeedProvider.probe", _fake_probe)
    monkeypatch.setattr(Summarizer, "summarize", _fake_summary)

    add_a = runner.invoke(app, ["sub", "add", "--name", "号A", "--wechat-id", "gh_a"])
    add_b = runner.invoke(app, ["sub", "add", "--name", "号B", "--wechat-id", "gh_b"])
    assert add_a.exit_code == 0
    assert add_b.exit_code == 0

    today = datetime.now().strftime("%Y-%m-%d")
    out = runner.invoke(app, ["view", "--mode", "source", "--date", today, "--no-interactive"])
    assert out.exit_code == 0
    assert "号A" in out.stdout
    assert "号B" in out.stdout
    assert "当天无更新。" in out.stdout
    assert "new=1" in out.stdout


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
    not_found = runner.invoke(app, ["read", "mark", "--id", "999", "--state", "read"])

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

    done_out = runner.invoke(app, ["done", "-i", "1", "--date", today])
    assert done_out.exit_code == 0
    assert "已批量更新 1 篇文章状态为: read" in done_out.stdout

    todo_out = runner.invoke(app, ["todo", "-i", "1", "--date", today])
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
    opened = runner.invoke(app, ["open", "--id", "1", "--date", today])
    assert opened.exit_code == 0
    assert "已尝试打开文章:" in opened.stdout
    assert "AI: provider=" in opened.stdout


def test_history_does_not_trigger_sync(isolated_env, monkeypatch):
    monkeypatch.setattr("wechat_agent.providers.template_feed_provider.TemplateFeedProvider.fetch", _fake_fetch)
    monkeypatch.setattr("wechat_agent.providers.template_feed_provider.TemplateFeedProvider.probe", _fake_probe)
    monkeypatch.setattr(Summarizer, "summarize", _fake_summary)

    add = runner.invoke(app, ["add", "-n", "号A", "-i", "gh_a"])
    assert add.exit_code == 0

    today = datetime.now().strftime("%Y-%m-%d")
    first_view = runner.invoke(app, ["view", "--mode", "source", "--date", today, "--no-interactive"])
    assert first_view.exit_code == 0

    monkeypatch.setattr(
        "wechat_agent.providers.template_feed_provider.TemplateFeedProvider.fetch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("history should not fetch")),
    )
    history_out = runner.invoke(app, ["history", "--mode", "source", "--date", today, "--no-interactive"])
    assert history_out.exit_code == 0
    assert "历史查询:" in history_out.stdout
    assert "CLI 集成测试文章" in history_out.stdout


def test_source_pin_and_list(isolated_env):
    add = runner.invoke(app, ["sub", "add", "--name", "号A", "--wechat-id", "gh_a"])
    assert add.exit_code == 0

    pin = runner.invoke(
        app,
        [
            "source",
            "pin",
            "--wechat-id",
            "gh_a",
            "--provider",
            "manual",
            "--url",
            "https://example.com/manual.xml",
        ],
    )
    assert pin.exit_code == 0
    assert "已置顶源" in pin.stdout

    listed = runner.invoke(app, ["source", "list", "--wechat-id", "gh_a"])
    assert listed.exit_code == 0
    assert "manual" in listed.stdout
    assert "https://example.com/manual.xml" in listed.stdout


def test_view_stale_fallback_and_strict_live(isolated_env, monkeypatch):
    monkeypatch.setattr(Summarizer, "summarize", _fake_summary)
    monkeypatch.setattr(
        "wechat_agent.services.source_gateway.Wechat2RssIndexProvider.discover",
        lambda _self, session, sub: [],
    )

    def probe_ok(self, source_url: str):
        return True, None

    def fetch_ok(self, source_url: str, since):
        suffix = source_url.rstrip("/").split("/")[-1]
        return [
            RawArticle(
                external_id=f"{suffix}-1",
                title=f"{suffix}-标题",
                url=f"https://example.com/{suffix}",
                published_at=datetime.now(timezone.utc),
                content_excerpt=f"{suffix}-正文",
                raw_hash=f"hash-{suffix}",
            )
        ]

    def fetch_partial_fail(self, source_url: str, since):
        if source_url.endswith("/gh_b"):
            raise RuntimeError("upstream 503")
        return fetch_ok(self, source_url, since)

    add_a = runner.invoke(app, ["sub", "add", "--name", "号A", "--wechat-id", "gh_a"])
    add_b = runner.invoke(app, ["sub", "add", "--name", "号B", "--wechat-id", "gh_b"])
    assert add_a.exit_code == 0
    assert add_b.exit_code == 0

    today = datetime.now().strftime("%Y-%m-%d")
    monkeypatch.setattr("wechat_agent.providers.template_feed_provider.TemplateFeedProvider.probe", probe_ok)
    monkeypatch.setattr("wechat_agent.providers.template_feed_provider.TemplateFeedProvider.fetch", fetch_ok)
    first = runner.invoke(app, ["view", "--mode", "source", "--date", today, "--no-interactive"])
    assert first.exit_code == 0
    assert "gh_a-标题" in first.stdout
    assert "gh_b-标题" in first.stdout

    monkeypatch.setattr("wechat_agent.providers.template_feed_provider.TemplateFeedProvider.fetch", fetch_partial_fail)
    stale_view = runner.invoke(app, ["view", "--mode", "source", "--date", today, "--no-interactive"])
    assert stale_view.exit_code == 0
    assert "stale_sources_used=1" in stale_view.stdout
    assert "状态: 使用缓存" in stale_view.stdout
    assert "gh_b-标题" in stale_view.stdout

    strict_live = runner.invoke(
        app,
        ["view", "--mode", "source", "--date", today, "--strict-live", "--no-interactive"],
    )
    assert strict_live.exit_code == 0
    assert "stale_sources_used=0" in strict_live.stdout
    assert "状态: 完全失败(待修复)" in strict_live.stdout
    assert "gh_a-标题" in strict_live.stdout
    assert "gh_b-标题" not in strict_live.stdout


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
