from __future__ import annotations

from datetime import datetime, timezone

import httpx

from .feed_parser import parse_feed
from ..schemas import RawArticle
from ..time_utils import shift_midnight_publish_time


class TemplateFeedProvider:
    def __init__(self, timeout_seconds: int = 15, client: httpx.Client | None = None, midnight_shift_days: int = 2) -> None:
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=timeout_seconds, follow_redirects=True)
        self.midnight_shift_days = midnight_shift_days

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def fetch(self, source_url: str, since: datetime) -> list[RawArticle]:
        response = self.client.get(source_url, headers={"Accept": "application/rss+xml,application/xml,*/*"})
        response.raise_for_status()

        articles = parse_feed(response.text, source_url=source_url)

        safe_since = since if since.tzinfo is not None else since.replace(tzinfo=timezone.utc)
        filtered: list[RawArticle] = []
        for article in articles:
            shifted_published_at = shift_midnight_publish_time(
                article.published_at,
                is_midnight_publish=article.is_midnight_publish,
                shift_days=self.midnight_shift_days,
            )
            article.published_at = shifted_published_at
            if article.published_at >= safe_since:
                filtered.append(article)

        seen: set[str] = set()
        deduped: list[RawArticle] = []
        for article in filtered:
            if article.external_id in seen:
                continue
            seen.add(article.external_id)
            deduped.append(article)

        return deduped

    def probe(self, source_url: str) -> tuple[bool, str | None]:
        try:
            response = self.client.get(source_url, headers={"Accept": "application/rss+xml,application/xml,*/*"})
            response.raise_for_status()
            articles = parse_feed(response.text, source_url=source_url)
            if not articles:
                return False, "源可访问但未解析到文章"
            return True, None
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
