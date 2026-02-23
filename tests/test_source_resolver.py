from __future__ import annotations

import json

from wechat_agent.models import Subscription
from wechat_agent.schemas import ResolveResult
from wechat_agent.services.source_resolver import SourceResolver


class FakeProvider:
    def probe(self, source_url: str):
        if source_url.endswith("gh_ok"):
            return True, None
        return False, "not found"


class SelectiveProvider:
    def __init__(self, accepted_url: str) -> None:
        self.accepted_url = accepted_url

    def probe(self, source_url: str):
        if source_url == self.accepted_url:
            return True, None
        return False, "not found"


class FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:  # noqa: D401
        return None


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


def test_source_resolver_tries_multiple_templates():
    sub = Subscription(name="测试号", wechat_id="gh_ok")
    expected = "https://second.example.com/gh_ok"
    resolver = SourceResolver(
        templates=("https://first.example.com/{wechat_id}", "https://second.example.com/{wechat_id}"),
        provider=SelectiveProvider(accepted_url=expected),
    )

    result = resolver.resolve(sub)

    assert result.ok is True
    assert result.source_url == expected


def test_source_resolver_loads_wechat2rss_from_vitepress_asset(monkeypatch):
    sub = Subscription(name="思想花火", wechat_id="gh_unknown")

    hash_map = {"list_all.md": "abc123"}
    hash_json = json.dumps(hash_map, ensure_ascii=False).replace('"', '\\"')
    index_html = (
        "<html><head></head><body>"
        f'<script>window.__VP_HASH_MAP__=JSON.parse("{hash_json}");</script>'
        "</body></html>"
    )
    asset_js = (
        '<a href="https://wechat2rss.xlab.app/feed/demo.xml">'
        "思想花火"
        "</a>"
    )

    def fake_get(url: str, timeout: int, follow_redirects: bool):
        if url.endswith("/list/all/"):
            return FakeResponse(index_html)
        if "list_all.md.abc123" in url:
            return FakeResponse(asset_js)
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("wechat_agent.services.source_resolver.httpx.get", fake_get)

    resolver = SourceResolver(
        templates=("https://example.com/{wechat_id}",),
        provider=SelectiveProvider("https://wechat2rss.xlab.app/feed/demo.xml"),
        wechat2rss_index_url="https://wechat2rss.xlab.app/list/all/",
    )

    result = resolver.resolve(sub)

    assert result.ok is True
    assert result.source_url == "https://wechat2rss.xlab.app/feed/demo.xml"
