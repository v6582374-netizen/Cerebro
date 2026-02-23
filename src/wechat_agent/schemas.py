from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(slots=True)
class RawArticle:
    external_id: str
    title: str
    url: str
    published_at: datetime
    content_excerpt: str
    raw_hash: str
    source_name: str | None = None
    is_midnight_publish: bool = False


@dataclass(slots=True)
class ResolveResult:
    ok: bool
    source_url: str | None = None
    error: str | None = None


@dataclass(slots=True)
class SummaryResult:
    summary_text: str
    model: str
    used_fallback: bool


@dataclass(slots=True)
class UserProfile:
    vector: list[float]
    sample_size: int


@dataclass(slots=True)
class RecommendationScore:
    score: float
    topic_score: float
    freshness_score: float


@dataclass(slots=True)
class ArticleViewItem:
    day_id: int
    article_pk: int
    source_name: str
    published_at: datetime
    title: str
    url: str
    summary: str
    is_read: bool
    score: float | None = None


@dataclass(slots=True)
class SourceCandidate:
    subscription_id: int
    provider: str
    url: str
    priority: int = 100
    is_pinned: bool = False
    confidence: float = 0.0
    discovered_at: datetime | None = None
    metadata_json: str | None = None


@dataclass(slots=True)
class ProbeResult:
    ok: bool
    article_count: int = 0
    latency_ms: int = 0
    error_kind: str | None = None
    error_message: str | None = None


@dataclass(slots=True)
class SourceFetchResult:
    ok: bool
    candidate: SourceCandidate
    articles: list[RawArticle]
    latency_ms: int
    error_kind: str | None = None
    error_message: str | None = None


@dataclass(slots=True)
class AuthSession:
    provider: str
    encrypted_blob: str
    expires_at: datetime | None
    updated_at: datetime


@dataclass(slots=True)
class DiscoveredArticleRef:
    url: str
    title_hint: str | None
    published_at_hint: datetime | None
    channel: str
    confidence: float


@dataclass(slots=True)
class DiscoveryResult:
    ok: bool
    refs: list[DiscoveredArticleRef]
    channel_used: str | None
    error_kind: str | None
    error_message: str | None
    latency_ms: int
    status: str


@dataclass(slots=True)
class CoverageReport:
    date: date
    total_subs: int
    success_subs: int
    delayed_subs: int
    fail_subs: int
    coverage_ratio: float
    detail_json: str


@dataclass(slots=True)
class QrLoginSession:
    uuid: str
    qr_url: str
    started_at: datetime


@dataclass(slots=True)
class AuthProgress:
    status: str
    code: int
    redirect_uri: str | None = None
    message: str | None = None


@dataclass(slots=True)
class WeChatSession:
    base_uri: str
    wxuin: str
    sid: str
    skey: str
    pass_ticket: str
    device_id: str
    sync_key: dict
    sync_host: str
    cookies: dict[str, str]
    expires_at: datetime
    nickname: str | None = None


@dataclass(slots=True)
class OfficialAccount:
    user_name: str
    nick_name: str
    verify_flag: int


@dataclass(slots=True)
class SyncBatch:
    retcode: str
    selector: str
    messages: list[dict]
    sync_key: dict
    next_sync_host: str | None
    created_at: datetime


@dataclass(slots=True)
class InboundMessagePayload:
    msg_id: str
    from_user_name: str
    msg_type: int
    app_msg_type: int
    content: str
    create_time: datetime


@dataclass(slots=True)
class ExtractedArticleRef:
    url: str
    title_hint: str | None
    published_at_hint: datetime | None
    from_user_name: str
    msg_id: str


@dataclass(slots=True)
class BindResult:
    ok: bool
    official_user_name: str | None
    confidence: float
    reason: str
