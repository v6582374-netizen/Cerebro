from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import func, select

from wechat_agent.config import get_settings
from wechat_agent.db import init_db, session_scope
from wechat_agent.models import Article, Subscription
from wechat_agent.schemas import RawArticle, ResolveResult, SummaryResult
from wechat_agent.services.recommender import Recommender
from wechat_agent.services.sync_service import SyncService


class FakeResolver:
    def resolve(self, sub: Subscription) -> ResolveResult:
        if sub.wechat_id == "gh_fail":
            return ResolveResult(ok=False, error="source not found")
        return ResolveResult(ok=True, source_url=sub.source_url or "https://example.com/rss")


class FakeFetcher:
    def fetch(self, source_url: str, since: datetime):
        return [
            RawArticle(
                external_id="external-1",
                title="测试文章",
                url="https://example.com/article/1",
                published_at=datetime.now(timezone.utc),
                content_excerpt="这是测试文章内容。",
                raw_hash="hash-1",
            )
        ]


class FakeSummarizer:
    def summarize(self, article: RawArticle) -> SummaryResult:
        return SummaryResult(summary_text="这是一段用于测试的摘要内容，长度超过三十字。", model="fake", used_fallback=True)


def test_sync_skip_failed_and_deduplicate(isolated_env):
    settings = get_settings()
    init_db(settings)

    with session_scope(settings) as session:
        session.add(Subscription(name="成功号", wechat_id="gh_ok", source_url="https://example.com/rss/ok"))
        session.add(Subscription(name="失败号", wechat_id="gh_fail"))
        session.commit()

    service = SyncService(
        resolver=FakeResolver(),
        fetcher=FakeFetcher(),
        summarizer=FakeSummarizer(),
        recommender=Recommender(api_key=None, base_url=None, embed_model="test"),
    )

    with session_scope(settings) as session:
        run1 = service.sync(session=session, target_date=date.today(), trigger="test")
        session.commit()
        run2 = service.sync(session=session, target_date=date.today(), trigger="test")
        session.commit()

        count_articles = session.scalar(select(func.count()).select_from(Article))

        assert run1.success_count == 1
        assert run1.fail_count == 1
        assert run2.success_count == 1
        assert run2.fail_count == 1
        assert count_articles == 1
