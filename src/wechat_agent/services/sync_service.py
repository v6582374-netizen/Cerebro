from __future__ import annotations

import re
from datetime import date, datetime, timezone

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from ..models import (
    Article,
    ArticleSummary,
    SOURCE_STATUS_ACTIVE,
    SOURCE_STATUS_MATCH_FAILED,
    SYNC_ITEM_STATUS_FAILED,
    SYNC_ITEM_STATUS_SUCCESS,
    Subscription,
    SyncRun,
    SyncRunItem,
    utcnow,
)
from ..schemas import RawArticle
from ..time_utils import local_day_bounds_utc
from .fetcher import Fetcher
from .recommender import Recommender
from .source_resolver import SourceResolver
from .summarizer import Summarizer


class SyncService:
    def __init__(
        self,
        resolver: SourceResolver,
        fetcher: Fetcher,
        summarizer: Summarizer,
        recommender: Recommender,
    ) -> None:
        self.resolver = resolver
        self.fetcher = fetcher
        self.summarizer = summarizer
        self.recommender = recommender

    def sync(self, session: Session, target_date: date, trigger: str = "view") -> SyncRun:
        run = SyncRun(trigger=trigger, started_at=utcnow(), success_count=0, fail_count=0)
        session.add(run)
        session.flush()

        day_start, _ = local_day_bounds_utc(target_date)

        subscriptions = session.scalars(select(Subscription).order_by(Subscription.id.asc())).all()
        for sub in subscriptions:
            self._sync_subscription(session=session, run=run, sub=sub, since=day_start)

        self._refresh_low_quality_summaries(session=session, target_date=target_date)
        self.recommender.recompute_scores_for_date(session=session, target_date=target_date)

        run.finished_at = utcnow()
        return run

    def _sync_subscription(self, session: Session, run: SyncRun, sub: Subscription, since: datetime) -> None:
        result = self.resolver.resolve(sub)
        if not result.ok or not result.source_url:
            sub.source_status = SOURCE_STATUS_MATCH_FAILED
            sub.last_error = result.error or "未匹配到可用公开源"
            run.fail_count += 1
            session.add(
                SyncRunItem(
                    sync_run_id=run.id,
                    subscription_id=sub.id,
                    status=SYNC_ITEM_STATUS_FAILED,
                    new_count=0,
                    error_message=sub.last_error,
                )
            )
            return

        sub.source_url = result.source_url
        sub.source_status = SOURCE_STATUS_ACTIVE
        sub.last_error = None

        try:
            raw_articles = self.fetcher.fetch(source_url=result.source_url, since=since)
        except Exception as exc:  # noqa: BLE001
            run.fail_count += 1
            sub.last_error = str(exc)
            session.add(
                SyncRunItem(
                    sync_run_id=run.id,
                    subscription_id=sub.id,
                    status=SYNC_ITEM_STATUS_FAILED,
                    new_count=0,
                    error_message=str(exc),
                )
            )
            return

        new_count = 0
        for raw in raw_articles:
            inserted = self._upsert_article(session=session, sub=sub, raw=raw)
            if not inserted:
                continue
            new_count += 1

        run.success_count += 1
        session.add(
            SyncRunItem(
                sync_run_id=run.id,
                subscription_id=sub.id,
                status=SYNC_ITEM_STATUS_SUCCESS,
                new_count=new_count,
                error_message=None,
            )
        )

    def _upsert_article(self, session: Session, sub: Subscription, raw: RawArticle) -> bool:
        existing = session.scalar(
            select(Article).where(
                Article.subscription_id == sub.id,
                Article.external_id == raw.external_id,
            )
        )
        if existing is not None:
            return False

        published_at = raw.published_at
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)

        article = Article(
            subscription_id=sub.id,
            external_id=raw.external_id,
            title=raw.title,
            url=raw.url,
            published_at=published_at,
            fetched_at=utcnow(),
            content_excerpt=raw.content_excerpt,
            raw_hash=raw.raw_hash,
        )
        session.add(article)
        session.flush()

        summary = self.summarizer.summarize(raw)
        session.add(
            ArticleSummary(
                article_id=article.id,
                summary_text=summary.summary_text,
                model=summary.model,
            )
        )

        embedding_text = f"{article.title}\n{summary.summary_text}\n{article.content_excerpt or ''}".strip()
        self.recommender.ensure_article_embedding(
            session=session,
            article_id=article.id,
            text=embedding_text,
        )
        return True

    def _refresh_low_quality_summaries(self, session: Session, target_date: date) -> None:
        day_start, day_end = local_day_bounds_utc(target_date)
        stmt = (
            select(Article, ArticleSummary)
            .outerjoin(ArticleSummary, ArticleSummary.article_id == Article.id)
            .where(and_(Article.published_at >= day_start, Article.published_at < day_end))
        )

        for article, summary in session.execute(stmt).all():
            if summary is not None and not self._needs_refresh(summary.summary_text):
                continue

            raw = RawArticle(
                external_id=article.external_id,
                title=article.title,
                url=article.url,
                published_at=article.published_at,
                content_excerpt=article.content_excerpt or "",
                raw_hash=article.raw_hash or article.external_id,
            )
            refreshed = self.summarizer.summarize(raw)

            if summary is None:
                session.add(
                    ArticleSummary(
                        article_id=article.id,
                        summary_text=refreshed.summary_text,
                        model=refreshed.model,
                    )
                )
            else:
                summary.summary_text = refreshed.summary_text
                summary.model = refreshed.model

    def _needs_refresh(self, summary_text: str) -> bool:
        compact = re.sub(r"\s+", "", summary_text or "")
        if len(compact) < 24:
            return True
        if "<" in summary_text or ">" in summary_text:
            return True
        if re.search(r"\d{4}-\d{2}-\d{2}", summary_text) and len(compact) < 40:
            return True
        metadata_tokens = ("关注前沿科技", "原创", "发布于", "发表于")
        if any(token in summary_text for token in metadata_tokens):
            return True
        return False
