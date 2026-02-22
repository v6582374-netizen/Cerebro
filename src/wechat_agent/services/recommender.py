from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timedelta, timezone

from openai import OpenAI
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from ..models import (
    Article,
    ArticleEmbedding,
    ArticleSummary,
    ReadState,
    RecommendationScoreEntry,
    utcnow,
)
from ..schemas import RecommendationScore, UserProfile
from ..time_utils import local_day_bounds_utc


def _normalize_vector(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0:
        return vector
    return [v / norm for v in vector]


def _cosine_similarity(v1: list[float], v2: list[float]) -> float:
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    numerator = sum(a * b for a, b in zip(v1, v2, strict=True))
    denom1 = math.sqrt(sum(a * a for a in v1))
    denom2 = math.sqrt(sum(b * b for b in v2))
    if denom1 == 0 or denom2 == 0:
        return 0.0
    return numerator / (denom1 * denom2)


class Recommender:
    def __init__(
        self,
        api_key: str | None,
        base_url: str | None,
        embed_model: str,
        client: OpenAI | None = None,
        vector_size: int = 64,
    ) -> None:
        self.embed_model = embed_model
        self.vector_size = vector_size
        if client is not None:
            self.client = client
        elif api_key:
            self.client = OpenAI(api_key=api_key, base_url=base_url)
        else:
            self.client = None

    def embed_text(self, text: str) -> list[float]:
        if self.client is not None:
            try:
                response = self.client.embeddings.create(model=self.embed_model, input=text)
                embedding = response.data[0].embedding
                return _normalize_vector([float(v) for v in embedding])
            except Exception:  # noqa: BLE001
                pass
        return self._local_embedding(text)

    def _local_embedding(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        raw = [((digest[i % len(digest)] / 255.0) * 2.0) - 1.0 for i in range(self.vector_size)]
        return _normalize_vector(raw)

    def ensure_article_embedding(self, session: Session, article_id: int, text: str) -> list[float]:
        existing = session.get(ArticleEmbedding, article_id)
        if existing is not None:
            return [float(v) for v in json.loads(existing.vector_json)]

        vector = self.embed_text(text)
        session.add(
            ArticleEmbedding(
                article_id=article_id,
                vector_json=json.dumps(vector),
                model=self.embed_model if self.client is not None else "local-hash",
            )
        )
        session.flush()
        return vector

    def build_user_profile(self, session: Session, now: datetime | None = None) -> UserProfile:
        reference = now or utcnow()
        lower_bound = reference - timedelta(days=30)

        stmt = (
            select(ArticleEmbedding.vector_json)
            .join(ReadState, ReadState.article_id == ArticleEmbedding.article_id)
            .join(Article, Article.id == ArticleEmbedding.article_id)
            .where(
                and_(
                    ReadState.is_read.is_(True),
                    Article.published_at >= lower_bound,
                )
            )
        )
        vectors = [[float(v) for v in json.loads(row[0])] for row in session.execute(stmt).all()]
        if not vectors:
            return UserProfile(vector=[], sample_size=0)

        dim = len(vectors[0])
        avg = [0.0] * dim
        for vec in vectors:
            if len(vec) != dim:
                continue
            for i, value in enumerate(vec):
                avg[i] += value

        sample_size = len(vectors)
        avg = [value / sample_size for value in avg]
        return UserProfile(vector=_normalize_vector(avg), sample_size=sample_size)

    def score(
        self,
        article_vector: list[float],
        profile: UserProfile,
        published_at: datetime,
        now: datetime | None = None,
    ) -> RecommendationScore:
        reference = now or utcnow()
        published = published_at if published_at.tzinfo is not None else published_at.replace(tzinfo=timezone.utc)
        topic_score = 0.0
        if profile.sample_size > 0 and profile.vector:
            topic_score = max(_cosine_similarity(article_vector, profile.vector), 0.0)

        age_hours = max((reference - published).total_seconds() / 3600.0, 0.0)
        freshness_score = math.exp(-age_hours / 48.0)

        if profile.sample_size == 0:
            final = freshness_score
        else:
            final = 0.7 * topic_score + 0.3 * freshness_score

        return RecommendationScore(score=final, topic_score=topic_score, freshness_score=freshness_score)

    def upsert_recommendation(
        self,
        session: Session,
        article_id: int,
        recommendation: RecommendationScore,
        profile_size: int,
    ) -> None:
        payload = {
            "topic_score": recommendation.topic_score,
            "freshness_score": recommendation.freshness_score,
            "profile_size": profile_size,
        }
        existing = session.scalar(
            select(RecommendationScoreEntry).where(RecommendationScoreEntry.article_id == article_id)
        )
        if existing:
            existing.score = recommendation.score
            existing.detail_json = json.dumps(payload)
            existing.scored_at = utcnow()
            return

        session.add(
            RecommendationScoreEntry(
                article_id=article_id,
                score=recommendation.score,
                detail_json=json.dumps(payload),
            )
        )

    def recompute_scores_for_date(self, session: Session, target_date: date) -> None:
        day_start, day_end = local_day_bounds_utc(target_date)

        profile = self.build_user_profile(session=session)

        stmt = (
            select(Article.id, Article.title, Article.content_excerpt, Article.published_at, ArticleSummary.summary_text)
            .outerjoin(ArticleSummary, ArticleSummary.article_id == Article.id)
            .where(and_(Article.published_at >= day_start, Article.published_at < day_end))
        )

        for article_id, title, excerpt, published_at, summary in session.execute(stmt).all():
            text = f"{title}\n{summary or ''}\n{excerpt or ''}".strip()
            vector = self.ensure_article_embedding(session=session, article_id=article_id, text=text)
            rec = self.score(article_vector=vector, profile=profile, published_at=published_at)
            self.upsert_recommendation(
                session=session,
                article_id=article_id,
                recommendation=rec,
                profile_size=profile.sample_size,
            )
