from __future__ import annotations

from datetime import datetime, timezone

from wechat_agent.schemas import RawArticle
from wechat_agent.services.summarizer import Summarizer


def test_fallback_summary_length_between_30_and_50():
    article = RawArticle(
        external_id="e1",
        title="这是一篇关于技术趋势和产品设计实践的长标题文章",
        url="https://example.com/a",
        published_at=datetime.now(timezone.utc),
        content_excerpt="文章详细讨论了产品设计、技术架构、用户体验和团队协作的实践方法。",
        raw_hash="hash",
    )

    service = Summarizer(api_key=None, base_url=None, chat_model="gpt-4o-mini")
    result = service.summarize(article)

    assert 30 <= len(result.summary_text) <= 50
