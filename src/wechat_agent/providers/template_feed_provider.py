from __future__ import annotations

from datetime import datetime, timezone

import httpx

from .feed_parser import parse_feed
from ..schemas import RawArticle


class TemplateFeedProvider:
    def __init__(self, timeout_seconds: int = 15, client: httpx.Client | None = None) -> None:
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=timeout_seconds, follow_redirects=True)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def fetch(self, source_url: str, since: datetime) -> list[RawArticle]:
        response = self.client.get(source_url, headers={"Accept": "application/rss+xml,application/xml,*/*"})
        response.raise_for_status()

        articles = parse_feed(response.text, source_url=source_url)

        safe_since = since if since.tzinfo is not None else since.replace(tzinfo=timezone.utc)
        filtered = [a for a in articles if a.published_at >= safe_since]

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
