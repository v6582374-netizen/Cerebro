from __future__ import annotations

from datetime import datetime, timedelta, timezone

from wechat_agent.schemas import UserProfile
from wechat_agent.services.recommender import Recommender


def test_recommendation_score_prefers_topic_similarity():
    now = datetime.now(timezone.utc)
    service = Recommender(api_key=None, base_url=None, embed_model="test")
    profile = UserProfile(vector=[1.0, 0.0], sample_size=5)

    near = service.score(article_vector=[1.0, 0.0], profile=profile, published_at=now - timedelta(hours=1))
    far = service.score(article_vector=[0.0, 1.0], profile=profile, published_at=now - timedelta(hours=1))

    assert near.score > far.score


def test_cold_start_uses_freshness_only():
    now = datetime.now(timezone.utc)
    service = Recommender(api_key=None, base_url=None, embed_model="test")
    empty_profile = UserProfile(vector=[], sample_size=0)

    fresh = service.score(article_vector=[1.0, 0.0], profile=empty_profile, published_at=now - timedelta(minutes=10))
    stale = service.score(article_vector=[1.0, 0.0], profile=empty_profile, published_at=now - timedelta(days=4))

    assert fresh.score > stale.score
