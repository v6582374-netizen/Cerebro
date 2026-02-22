from __future__ import annotations

from wechat_agent.config import get_settings


def test_resolve_deepseek_auto(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "auto")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-key")
    monkeypatch.setenv("DEEPSEEK_CHAT_MODEL", "deepseek-chat")
    monkeypatch.setenv("DEEPSEEK_EMBED_MODEL", "")
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.resolved_ai_provider() == "deepseek"
    assert settings.resolved_api_key() == "deepseek-test-key"
    assert settings.resolved_chat_model() == "deepseek-chat"
    assert settings.resolved_embed_model() is None
    get_settings.cache_clear()



def test_resolve_openai_priority(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test-key")
    monkeypatch.setenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("OPENAI_EMBED_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-key")
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.resolved_ai_provider() == "openai"
    assert settings.resolved_api_key() == "openai-test-key"
    assert settings.resolved_chat_model() == "gpt-4o-mini"
    assert settings.resolved_embed_model() == "text-embedding-3-small"
    get_settings.cache_clear()
