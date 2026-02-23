from __future__ import annotations

import calendar
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
import re
import time
from urllib.parse import urlparse

import feedparser
import httpx

from ..schemas import DiscoveredArticleRef

_INDEX_PATTERN = re.compile(
    r'href="(https://wechat2rss\.xlab\.app/feed/[a-f0-9]+\.xml)"[^>]*>([^<]+)</a>',
    re.IGNORECASE,
)


def _normalize_name(text: str) -> str:
    lowered = (text or "").strip().lower()
    return re.sub(r"[\W_]+", "", lowered)


def _normalize_mp_link(raw: str | None) -> str | None:
    if not raw:
        return None
    value = raw.strip().replace("&amp;", "&").replace("&#38;", "&")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.netloc.lower() != "mp.weixin.qq.com":
        return None
    if not parsed.path.startswith("/s"):
        return None
    return value


class Wechat2RssDiscoveryProvider:
    name = "wechat2rss_directory"

    def __init__(
        self,
        index_url: str = "https://wechat2rss.xlab.app/list/all",
        timeout_seconds: int = 15,
        cache_ttl_seconds: int = 1800,
        client: httpx.Client | None = None,
    ) -> None:
        self.index_url = index_url
        self.cache_ttl_seconds = max(60, cache_ttl_seconds)
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=timeout_seconds, follow_redirects=True)
        self._index_cache: list[tuple[str, str]] = []
        self._index_updated_at = 0.0
        self._feed_cache: dict[str, tuple[float, list[DiscoveredArticleRef]]] = {}

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def search(self, subscription_name: str, target_date: date, limit: int = 8) -> list[DiscoveredArticleRef]:
        self._ensure_index()
        matched = self._match_candidates(subscription_name=subscription_name, limit=3)
        refs: list[DiscoveredArticleRef] = []
        seen: set[str] = set()
        for feed_url, score in matched:
            feed_refs = self._fetch_feed_refs(feed_url=feed_url, target_date=target_date, limit=limit)
            for item in feed_refs:
                if item.url in seen:
                    continue
                seen.add(item.url)
                item.confidence = max(0.3, min(0.98, item.confidence * score))
                refs.append(item)
                if len(refs) >= limit:
                    return refs
        return refs

    def _ensure_index(self) -> None:
        now = time.time()
        if self._index_cache and (now - self._index_updated_at) < self.cache_ttl_seconds:
            return
        response = self.client.get(
            self.index_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2_1) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        response.raise_for_status()
        pairs = _INDEX_PATTERN.findall(response.text)
        cache: list[tuple[str, str]] = []
        for feed_url, title in pairs:
            name = title.strip()
            if not name:
                continue
            cache.append((name, feed_url.strip()))
        self._index_cache = cache
        self._index_updated_at = now

    def _match_candidates(self, subscription_name: str, limit: int) -> list[tuple[str, float]]:
        normalized_target = _normalize_name(subscription_name)
        if not normalized_target:
            return []

        scored: list[tuple[str, float]] = []
        for candidate_name, feed_url in self._index_cache:
            candidate_norm = _normalize_name(candidate_name)
            if not candidate_norm:
                continue
            if candidate_norm == normalized_target:
                score = 1.0
            elif normalized_target in candidate_norm or candidate_norm in normalized_target:
                score = 0.92
            else:
                ratio = SequenceMatcher(None, normalized_target, candidate_norm).ratio()
                if ratio < 0.50:
                    continue
                score = 0.55 + (ratio * 0.35)
            scored.append((feed_url, score))

        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:limit]

    def _fetch_feed_refs(self, feed_url: str, target_date: date, limit: int) -> list[DiscoveredArticleRef]:
        now = time.time()
        cached = self._feed_cache.get(feed_url)
        if cached and (now - cached[0]) < self.cache_ttl_seconds:
            return cached[1][:limit]

        response = self.client.get(
            feed_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2_1) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
            },
        )
        response.raise_for_status()
        parsed = feedparser.parse(response.text)
        refs: list[DiscoveredArticleRef] = []
        for rank, entry in enumerate(parsed.entries, start=1):
            link = _normalize_mp_link(entry.get("link"))
            if not link:
                continue
            published_hint = None
            published_parsed = entry.get("published_parsed")
            if published_parsed:
                try:
                    ts = calendar.timegm(published_parsed)
                    published_hint = datetime.fromtimestamp(ts, tz=timezone.utc)
                except Exception:
                    published_hint = None
            title_hint = str(entry.get("title") or "").strip() or None
            confidence = max(0.45, 0.95 - ((rank - 1) * 0.05))
            refs.append(
                DiscoveredArticleRef(
                    url=link,
                    title_hint=title_hint,
                    published_at_hint=published_hint,
                    channel=self.name,
                    confidence=confidence,
                )
            )
            if len(refs) >= limit:
                break
        self._feed_cache[feed_url] = (now, refs)
        return refs
