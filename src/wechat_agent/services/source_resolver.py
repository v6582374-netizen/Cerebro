from __future__ import annotations

import html
import re
from dataclasses import dataclass

import httpx

from ..models import SOURCE_STATUS_MATCH_FAILED, SOURCE_STATUS_PENDING, Subscription
from ..schemas import ResolveResult
from ..providers.template_feed_provider import TemplateFeedProvider


@dataclass(frozen=True, slots=True)
class Wechat2RssItem:
    name: str
    feed_url: str
    normalized_name: str


_ANCHOR_PATTERN = re.compile(
    r'<a href="(?P<url>https://wechat2rss\.xlab\.app/feed/[^"]+\.xml)"[^>]*>(?P<name>.*?)</a>',
    re.IGNORECASE,
)


def _normalize_name(value: str) -> str:
    lowered = value.strip().lower()
    lowered = re.sub(r"\s+", "", lowered)
    lowered = re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", lowered)
    return lowered


class SourceResolver:
    def __init__(
        self,
        templates: tuple[str, ...],
        provider: TemplateFeedProvider,
        wechat2rss_index_url: str | None = None,
    ) -> None:
        self.templates = templates
        self.provider = provider
        self.wechat2rss_index_url = wechat2rss_index_url
        self._wechat2rss_cache: list[Wechat2RssItem] | None = None

    def resolve(self, sub: Subscription) -> ResolveResult:
        if sub.source_url:
            return ResolveResult(ok=True, source_url=sub.source_url)

        last_error: str | None = None
        for template in self.templates:
            try:
                candidate = template.format(wechat_id=sub.wechat_id)
            except KeyError:
                continue

            ok, error = self.provider.probe(candidate)
            if ok:
                return ResolveResult(ok=True, source_url=candidate)
            last_error = error

        fallback = self._resolve_from_wechat2rss(sub)
        if fallback.ok:
            return fallback
        if fallback.error:
            last_error = f"{last_error or '模板源失败'}; wechat2rss: {fallback.error}"

        if sub.source_status == SOURCE_STATUS_PENDING:
            sub.source_status = SOURCE_STATUS_MATCH_FAILED
        return ResolveResult(ok=False, error=last_error or "未匹配到可用公开源")

    def _resolve_from_wechat2rss(self, sub: Subscription) -> ResolveResult:
        if not self.wechat2rss_index_url:
            return ResolveResult(ok=False, error="未配置 wechat2rss 索引地址")

        try:
            items = self._load_wechat2rss_items()
        except Exception as exc:  # noqa: BLE001
            return ResolveResult(ok=False, error=f"加载 wechat2rss 列表失败: {exc}")

        if not items:
            return ResolveResult(ok=False, error="wechat2rss 列表为空")

        normalized_sub = _normalize_name(sub.name)
        if not normalized_sub:
            return ResolveResult(ok=False, error="订阅名称为空，无法匹配 wechat2rss")

        best: Wechat2RssItem | None = None
        best_score = -1
        for item in items:
            score = self._match_score(normalized_sub, item.normalized_name)
            if score > best_score:
                best_score = score
                best = item

        if best is None or best_score <= 0:
            return ResolveResult(ok=False, error=f"wechat2rss 未找到匹配: {sub.name}")

        ok, error = self.provider.probe(best.feed_url)
        if not ok:
            return ResolveResult(ok=False, error=error or f"wechat2rss 源不可用: {best.feed_url}")
        return ResolveResult(ok=True, source_url=best.feed_url)

    def _load_wechat2rss_items(self) -> list[Wechat2RssItem]:
        if self._wechat2rss_cache is not None:
            return self._wechat2rss_cache

        response = httpx.get(self.wechat2rss_index_url, timeout=20, follow_redirects=True)
        response.raise_for_status()
        body = response.text

        items: list[Wechat2RssItem] = []
        for match in _ANCHOR_PATTERN.finditer(body):
            raw_name = html.unescape(match.group("name")).strip()
            url = match.group("url").strip()
            if not raw_name:
                continue
            normalized = _normalize_name(raw_name)
            if not normalized:
                continue
            items.append(Wechat2RssItem(name=raw_name, feed_url=url, normalized_name=normalized))

        self._wechat2rss_cache = items
        return items

    def _match_score(self, a: str, b: str) -> int:
        if a == b:
            return 100
        if a in b or b in a:
            return min(len(a), len(b))
        common = len(set(a) & set(b))
        if common >= 2 and (common / max(len(a), len(b))) >= 0.5:
            return common
        return 0
