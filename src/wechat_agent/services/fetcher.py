from __future__ import annotations

from datetime import datetime

from ..providers.template_feed_provider import TemplateFeedProvider
from ..schemas import RawArticle


class Fetcher:
    def __init__(self, provider: TemplateFeedProvider) -> None:
        self.provider = provider

    def fetch(self, source_url: str, since: datetime) -> list[RawArticle]:
        return self.provider.fetch(source_url=source_url, since=since)
