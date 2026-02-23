from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
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
from .source_gateway import SourceGateway
from .source_resolver import SourceResolver
from .summarizer import Summarizer


class SyncService:
    def __init__(
        self,
        resolver: SourceResolver,
        fetcher: Fetcher,
        summarizer: Summarizer,
        recommender: Recommender,
        sync_overlap_seconds: int = 120,
        incremental_sync_enabled: bool = True,
        source_gateway: SourceGateway | None = None,
    ) -> None:
        self.resolver = resolver
        self.fetcher = fetcher
        self.summarizer = summarizer
        self.recommender = recommender
        self.sync_overlap_seconds = max(sync_overlap_seconds, 0)
        self.incremental_sync_enabled = incremental_sync_enabled
        self.source_gateway = source_gateway

    def sync(self, session: Session, target_date: date, trigger: str = "view") -> SyncRun:
        run = SyncRun(trigger=trigger, started_at=utcnow(), success_count=0, fail_count=0)
        session.add(run)
        session.flush()

        day_start, _ = local_day_bounds_utc(target_date)
        last_success_map = self._last_success_time_by_subscription(session)
        new_article_ids: list[int] = []

        subscriptions = session.scalars(select(Subscription).order_by(Subscription.id.asc())).all()
        for sub in subscriptions:
            since = day_start
            if self.incremental_sync_enabled:
                last_success = last_success_map.get(sub.id)
                if last_success is not None:
                    overlap = timedelta(seconds=self.sync_overlap_seconds)
                    since = max(day_start, last_success - overlap)
            self._sync_subscription(
                session=session,
                run=run,
                sub=sub,
                since=since,
                new_article_ids=new_article_ids,
            )

        self._refresh_low_quality_summaries(session=session, article_ids=new_article_ids)
        self.recommender.recompute_scores_for_date(session=session, target_date=target_date)

        run.finished_at = utcnow()
        return run

    def _last_success_time_by_subscription(self, session: Session) -> dict[int, datetime]:
        stmt = (
            select(SyncRunItem.subscription_id, SyncRun.finished_at)
            .join(SyncRun, SyncRun.id == SyncRunItem.sync_run_id)
            .where(
                SyncRunItem.status == SYNC_ITEM_STATUS_SUCCESS,
                SyncRun.finished_at.is_not(None),
            )
            .order_by(SyncRun.finished_at.desc())
        )
        result: dict[int, datetime] = {}
        for subscription_id, finished_at in session.execute(stmt).all():
            if subscription_id in result:
                continue
            if finished_at is None:
                continue
            value = finished_at
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            result[int(subscription_id)] = value
        return result

    def _sync_subscription(
        self,
        session: Session,
        run: SyncRun,
        sub: Subscription,
        since: datetime,
        new_article_ids: list[int],
    ) -> None:
        if self.source_gateway is not None:
            self._sync_subscription_v2(
                session=session,
                run=run,
                sub=sub,
                since=since,
                new_article_ids=new_article_ids,
            )
            return
        self._sync_subscription_v1(
            session=session,
            run=run,
            sub=sub,
            since=since,
            new_article_ids=new_article_ids,
        )

    def _sync_subscription_v1(
        self,
        session: Session,
        run: SyncRun,
        sub: Subscription,
        since: datetime,
        new_article_ids: list[int],
    ) -> None:
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

        self._record_success_items(
            session=session,
            run=run,
            sub=sub,
            raw_articles=raw_articles,
            new_article_ids=new_article_ids,
        )

    def _sync_subscription_v2(
        self,
        session: Session,
        run: SyncRun,
        sub: Subscription,
        since: datetime,
        new_article_ids: list[int],
    ) -> None:
        fetch_result = self.source_gateway.fetch_with_failover(
            session=session,
            sync_run_id=run.id,
            sub=sub,
            since=since,
        )
        if not fetch_result.ok or not fetch_result.candidate.url:
            sub.source_status = SOURCE_STATUS_MATCH_FAILED
            sub.last_error = fetch_result.error_message or "未匹配到可用公开源"
            run.fail_count += 1
            session.add(
                SyncRunItem(
                    sync_run_id=run.id,
                    subscription_id=sub.id,
                    status=SYNC_ITEM_STATUS_FAILED,
                    new_count=0,
                    error_message=f"{fetch_result.error_kind or 'UNKNOWN'}: {sub.last_error}",
                )
            )
            return

        sub.source_url = fetch_result.candidate.url
        sub.preferred_provider = fetch_result.candidate.provider
        sub.source_status = SOURCE_STATUS_ACTIVE
        sub.last_error = None

        self._record_success_items(
            session=session,
            run=run,
            sub=sub,
            raw_articles=fetch_result.articles,
            new_article_ids=new_article_ids,
        )

    def _record_success_items(
        self,
        session: Session,
        run: SyncRun,
        sub: Subscription,
        raw_articles: list[RawArticle],
        new_article_ids: list[int],
    ) -> None:
        new_count = 0
        for raw in raw_articles:
            article_id = self._upsert_article(session=session, sub=sub, raw=raw)
            if article_id is None:
                continue
            new_count += 1
            new_article_ids.append(article_id)

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

    def _upsert_article(self, session: Session, sub: Subscription, raw: RawArticle) -> int | None:
        existing = session.scalar(
            select(Article).where(
                Article.subscription_id == sub.id,
                Article.external_id == raw.external_id,
            )
        )
        if existing is not None:
            published_at = raw.published_at
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)
            if existing.published_at != published_at:
                existing.published_at = published_at
            if (raw.content_excerpt or "") and existing.content_excerpt != raw.content_excerpt:
                existing.content_excerpt = raw.content_excerpt
            if (raw.raw_hash or "") and existing.raw_hash != raw.raw_hash:
                existing.raw_hash = raw.raw_hash
            return None

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
        return article.id

    def _refresh_low_quality_summaries(self, session: Session, article_ids: list[int]) -> None:
        if not article_ids:
            return
        stmt = (
            select(Article, ArticleSummary)
            .outerjoin(ArticleSummary, ArticleSummary.article_id == Article.id)
            .where(Article.id.in_(article_ids))
        )

        for article, summary in session.execute(stmt).all():
            if summary is not None and not self._needs_refresh(summary.summary_text, summary.model):
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

    def _needs_refresh(self, summary_text: str, model: str | None = None) -> bool:
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
        if compact.endswith(("…", "...", "..", "，", ",", "、", "；", ";", "：", ":")):
            return True
        if (model or "").strip().lower() == "fallback":
            if len(compact) >= 48 and compact[-1] not in {"。", "！", "？", "!", "?"}:
                return True
        return False
