from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

SOURCE_STATUS_PENDING = "PENDING"
SOURCE_STATUS_ACTIVE = "ACTIVE"
SOURCE_STATUS_MATCH_FAILED = "MATCH_FAILED"

SYNC_ITEM_STATUS_SUCCESS = "SUCCESS"
SYNC_ITEM_STATUS_FAILED = "FAILED"

SOURCE_MODE_AUTO = "auto"
SOURCE_MODE_MANUAL = "manual"

HEALTH_STATE_CLOSED = "CLOSED"
HEALTH_STATE_OPEN = "OPEN"
HEALTH_STATE_HALF_OPEN = "HALF_OPEN"

FETCH_STATUS_SUCCESS = "SUCCESS"
FETCH_STATUS_FAILED = "FAILED"
FETCH_STATUS_SKIPPED = "SKIPPED"

DISCOVERY_STATUS_SUCCESS = "SUCCESS"
DISCOVERY_STATUS_DELAYED = "DELAYED"
DISCOVERY_STATUS_FAILED = "FAILED"

BIND_STATUS_BOUND = "BOUND"
BIND_STATUS_PENDING = "PENDING"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    wechat_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_status: Mapped[str] = mapped_column(String(50), nullable=False, default=SOURCE_STATUS_PENDING)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    preferred_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_mode: Mapped[str] = mapped_column(String(32), nullable=False, default=SOURCE_MODE_AUTO)
    discovery_status: Mapped[str] = mapped_column(String(32), nullable=False, default=SOURCE_STATUS_PENDING)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    articles: Mapped[list[Article]] = relationship(back_populates="subscription", cascade="all, delete")
    sources: Mapped[list[SubscriptionSource]] = relationship(back_populates="subscription", cascade="all, delete")
    source_health: Mapped[list[SourceHealth]] = relationship(back_populates="subscription", cascade="all, delete")
    discovery_runs: Mapped[list[DiscoveryRun]] = relationship(back_populates="subscription", cascade="all, delete")
    article_refs: Mapped[list[ArticleRef]] = relationship(back_populates="subscription", cascade="all, delete")


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (UniqueConstraint("subscription_id", "external_id", name="uq_article_source_external"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    external_id: Mapped[str] = mapped_column(String(512), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    content_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)

    subscription: Mapped[Subscription] = relationship(back_populates="articles")
    summary: Mapped[ArticleSummary | None] = relationship(back_populates="article", cascade="all, delete")
    read_state: Mapped[ReadState | None] = relationship(back_populates="article", cascade="all, delete")
    embedding: Mapped[ArticleEmbedding | None] = relationship(back_populates="article", cascade="all, delete")


class ArticleSummary(Base):
    __tablename__ = "article_summaries"

    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    summary_text: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    article: Mapped[Article] = relationship(back_populates="summary")


class ReadState(Base):
    __tablename__ = "read_states"

    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    article: Mapped[Article] = relationship(back_populates="read_state")


class ArticleEmbedding(Base):
    __tablename__ = "article_embeddings"

    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    vector_json: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    article: Mapped[Article] = relationship(back_populates="embedding")


class RecommendationScoreEntry(Base):
    __tablename__ = "recommendation_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    score: Mapped[float] = mapped_column(nullable=False)
    detail_json: Mapped[str] = mapped_column(Text, nullable=False)
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trigger: Mapped[str] = mapped_column(String(50), nullable=False)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fail_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sync_batches: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    official_msgs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    article_refs_extracted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    blocked_by_auth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    items: Mapped[list[SyncRunItem]] = relationship(back_populates="sync_run", cascade="all, delete")


class SyncRunItem(Base):
    __tablename__ = "sync_run_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sync_run_id: Mapped[int] = mapped_column(ForeignKey("sync_runs.id", ondelete="CASCADE"), nullable=False)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    new_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    sync_run: Mapped[SyncRun] = relationship(back_populates="items")


class SubscriptionSource(Base):
    __tablename__ = "subscription_sources"
    __table_args__ = (UniqueConstraint("subscription_id", "provider", "source_url", name="uq_sub_source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    is_pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    subscription: Mapped[Subscription] = relationship(back_populates="sources")


class SourceHealth(Base):
    __tablename__ = "source_health"
    __table_args__ = (UniqueConstraint("subscription_id", "provider", "source_url", name="uq_source_health"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default=HEALTH_STATE_CLOSED)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    success_rate_24h: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    avg_latency_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    subscription: Mapped[Subscription] = relationship(back_populates="source_health")


class FetchAttempt(Base):
    __tablename__ = "fetch_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sync_run_id: Mapped[int] = mapped_column(ForeignKey("sync_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    http_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class AuthSessionEntry(Base):
    __tablename__ = "auth_sessions"

    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    encrypted_blob: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class WeChatAccount(Base):
    __tablename__ = "wechat_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wxuin: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    nickname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class WeChatSyncState(Base):
    __tablename__ = "wechat_sync_states"

    account_id: Mapped[int] = mapped_column(ForeignKey("wechat_accounts.id", ondelete="CASCADE"), primary_key=True)
    sync_key_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    sync_host: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_selector: Mapped[str | None] = mapped_column(String(16), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class OfficialAccountEntry(Base):
    __tablename__ = "official_accounts"
    __table_args__ = (UniqueConstraint("account_id", "user_name", name="uq_official_account_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("wechat_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    user_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    nick_name: Mapped[str] = mapped_column(String(255), nullable=False)
    verify_flag: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class InboundMessageEntry(Base):
    __tablename__ = "inbound_messages"
    __table_args__ = (UniqueConstraint("account_id", "msg_id", name="uq_inbound_msg"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("wechat_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    msg_id: Mapped[str] = mapped_column(String(128), nullable=False)
    from_user_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    msg_type: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    app_msg_type: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    create_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class SubscriptionBinding(Base):
    __tablename__ = "subscription_bindings"

    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    official_user_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    bind_status: Mapped[str] = mapped_column(String(32), nullable=False, default=BIND_STATUS_BOUND)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class DiscoveryRun(Base):
    __tablename__ = "discovery_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sync_run_id: Mapped[int] = mapped_column(ForeignKey("sync_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    ref_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    subscription: Mapped[Subscription] = relationship(back_populates="discovery_runs")


class ArticleRef(Base):
    __tablename__ = "article_refs"
    __table_args__ = (UniqueConstraint("subscription_id", "url", name="uq_article_ref_sub_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title_hint: Mapped[str | None] = mapped_column(String(512), nullable=True)
    published_at_hint: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    subscription: Mapped[Subscription] = relationship(back_populates="article_refs")


class CoverageDaily(Base):
    __tablename__ = "coverage_daily"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    total_subs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_subs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delayed_subs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fail_subs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    coverage_ratio: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    detail_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
