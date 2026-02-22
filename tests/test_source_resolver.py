from __future__ import annotations

from wechat_agent.models import Subscription
from wechat_agent.schemas import ResolveResult
from wechat_agent.services.source_resolver import SourceResolver


class FakeProvider:
    def probe(self, source_url: str):
        if source_url.endswith("gh_ok"):
            return True, None
        return False, "not found"


def test_source_resolver_template_match():
    sub = Subscription(name="测试号", wechat_id="gh_ok")
    resolver = SourceResolver(templates=("https://example.com/{wechat_id}",), provider=FakeProvider())

    result = resolver.resolve(sub)

    assert isinstance(result, ResolveResult)
    assert result.ok is True
    assert result.source_url == "https://example.com/gh_ok"


def test_source_resolver_fail_returns_error():
    sub = Subscription(name="失败号", wechat_id="gh_fail")
    resolver = SourceResolver(templates=("https://example.com/{wechat_id}",), provider=FakeProvider())

    result = resolver.resolve(sub)

    assert result.ok is False
    assert result.error
