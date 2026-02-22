from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class RawArticle:
    external_id: str
    title: str
    url: str
    published_at: datetime
    content_excerpt: str
    raw_hash: str
    source_name: str | None = None


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
    article_id: int
    source_name: str
    published_at: datetime
    title: str
    url: str
    summary: str
    is_read: bool
    score: float | None = None
