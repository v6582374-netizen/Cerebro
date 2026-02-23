from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from wechat_agent.config import get_settings
from wechat_agent.db import init_db, session_scope
from wechat_agent.models import (
    HEALTH_STATE_HALF_OPEN,
    HEALTH_STATE_OPEN,
    SOURCE_MODE_AUTO,
    SOURCE_MODE_MANUAL,
    SourceHealth,
    Subscription,
    SubscriptionSource,
    utcnow,
)
from wechat_agent.models import SyncRun
from wechat_agent.schemas import SourceCandidate
from wechat_agent.services.source_gateway import (
    MANUAL_PROVIDER,
    SourceGateway,
    SourceHealthService,
    SourceRouter,
    WECHAT2RSS_PROVIDER,
    ManualSourceProvider,
    Wechat2RssIndexProvider,
    _Wechat2RssItem,
)
from wechat_agent.providers.template_feed_provider import TemplateFeedProvider


class _EmptyProvider:
    name = "empty"

    def discover(self, session, sub):
        return []

    def probe(self, candidate):
        raise NotImplementedError

    def fetch(self, candidate, since):
        raise NotImplementedError


def test_source_router_prefers_pinned(isolated_env):
    settings = get_settings()
    init_db(settings)

    with session_scope(settings) as session:
        sub = Subscription(name="号A", wechat_id="gh_a")
        session.add(sub)
        session.commit()
        session.refresh(sub)

        candidates = [
            SourceCandidate(
                subscription_id=sub.id,
                provider="rsshub_mirror",
                url="https://example.com/rss",
                priority=20,
                is_pinned=False,
                confidence=0.9,
                discovered_at=utcnow(),
            ),
            SourceCandidate(
                subscription_id=sub.id,
                provider="manual",
                url="https://example.com/manual",
                priority=0,
                is_pinned=True,
                confidence=0.7,
                discovered_at=utcnow(),
            ),
        ]
        router = SourceRouter()
        picked = router.pick_best(sub=sub, candidates=candidates, health={})
        assert picked is not None
        assert picked.provider == "manual"


def test_health_service_opens_and_half_opens_circuit(isolated_env):
    settings = get_settings()
    init_db(settings)
    health_service = SourceHealthService(fail_threshold=3, cooldown_minutes=30)

    with session_scope(settings) as session:
        sub = Subscription(name="号A", wechat_id="gh_a")
        session.add(sub)
        session.commit()
        session.refresh(sub)

        candidate = SourceCandidate(
            subscription_id=sub.id,
            provider="rsshub_mirror",
            url="https://example.com/rss",
            priority=20,
            is_pinned=False,
            confidence=0.5,
            discovered_at=utcnow(),
        )
        run = SyncRun(trigger="test")
        session.add(run)
        session.commit()
        session.refresh(run)

        for _ in range(3):
            health_service.record_attempt(
                session=session,
                sync_run_id=run.id,
                candidate=candidate,
                status="FAILED",
                latency_ms=100,
                error_kind="HTTP_5XX",
                error_message="503",
            )
        session.commit()

        row = session.scalar(select(SourceHealth).where(SourceHealth.subscription_id == sub.id))
        assert row is not None
        assert row.state == HEALTH_STATE_OPEN
        assert health_service.should_skip_for_circuit(session=session, candidate=candidate) is True

        row.cooldown_until = utcnow() - timedelta(minutes=1)
        session.commit()
        assert health_service.should_skip_for_circuit(session=session, candidate=candidate) is False

        row = session.scalar(select(SourceHealth).where(SourceHealth.subscription_id == sub.id))
        assert row is not None
        assert row.state == HEALTH_STATE_HALF_OPEN


def test_wechat2rss_discover_rejects_weak_match(isolated_env):
    settings = get_settings()
    init_db(settings)

    class _DummyFeed:
        def probe(self, _url):
            return True, None

        def fetch(self, source_url, since):  # noqa: ARG002
            return []

    provider = Wechat2RssIndexProvider(index_url="https://example.com", feed_provider=_DummyFeed())
    provider._cache = [
        _Wechat2RssItem(name="VLabTeam", url="https://example.com/vlab.xml", normalized_name="vlabteam"),
        _Wechat2RssItem(name="ADLab", url="https://example.com/adlab.xml", normalized_name="adlab"),
    ]

    with session_scope(settings) as session:
        sub = Subscription(name="打边炉ARTDBL", wechat_id="ARTDBL")
        session.add(sub)
        session.commit()
        session.refresh(sub)
        rows = provider.discover(session=session, sub=sub)
        assert rows == []


def test_gateway_demotes_legacy_manual_pinned(isolated_env):
    settings = get_settings()
    init_db(settings)
    gateway = SourceGateway(
        providers=[_EmptyProvider()],
        router=SourceRouter(),
        health_service=SourceHealthService(),
    )

    with session_scope(settings) as session:
        sub = Subscription(name="号A", wechat_id="gh_a")
        session.add(sub)
        session.commit()
        session.refresh(sub)

        session.add(
            SubscriptionSource(
                subscription_id=sub.id,
                provider=MANUAL_PROVIDER,
                source_url="https://example.com/legacy.xml",
                priority=0,
                is_pinned=True,
                is_active=True,
                confidence=1.0,
                metadata_json='{"legacy":true}',
            )
        )
        session.commit()

        _ = gateway.discover_candidates(session=session, sub=sub)
        row = session.scalar(
            select(SubscriptionSource).where(
                SubscriptionSource.subscription_id == sub.id,
                SubscriptionSource.provider == MANUAL_PROVIDER,
            )
        )
        assert row is not None
        assert row.is_pinned is False
        assert row.is_active is False
        assert row.priority >= 95


def test_manual_provider_does_not_backfill_source_url_in_auto_mode(isolated_env):
    settings = get_settings()
    init_db(settings)
    provider = ManualSourceProvider(feed_provider=TemplateFeedProvider(timeout_seconds=1))

    with session_scope(settings) as session:
        sub = Subscription(
            name="号A",
            wechat_id="gh_a",
            source_url="https://example.com/manual.xml",
            source_mode=SOURCE_MODE_AUTO,
        )
        session.add(sub)
        session.commit()
        session.refresh(sub)
        candidates = provider.discover(session=session, sub=sub)
        assert candidates == []

        sub.source_mode = SOURCE_MODE_MANUAL
        session.commit()
        candidates = provider.discover(session=session, sub=sub)
        assert len(candidates) == 1
        assert candidates[0].url == "https://example.com/manual.xml"
    provider.feed_provider.close()


def test_gateway_deactivates_weak_wechat2rss_entries(isolated_env):
    settings = get_settings()
    init_db(settings)
    gateway = SourceGateway(
        providers=[_EmptyProvider()],
        router=SourceRouter(),
        health_service=SourceHealthService(),
    )
    with session_scope(settings) as session:
        sub = Subscription(name="打边炉ARTDBL", wechat_id="ARTDBL")
        session.add(sub)
        session.commit()
        session.refresh(sub)
        session.add(
            SubscriptionSource(
                subscription_id=sub.id,
                provider=WECHAT2RSS_PROVIDER,
                source_url="https://example.com/weak.xml",
                priority=60,
                is_pinned=False,
                is_active=True,
                confidence=0.3,
                metadata_json='{"name":"VLabTeam","score":4}',
            )
        )
        session.commit()
        _ = gateway.discover_candidates(session=session, sub=sub)
        row = session.scalar(
            select(SubscriptionSource).where(
                SubscriptionSource.subscription_id == sub.id,
                SubscriptionSource.provider == WECHAT2RSS_PROVIDER,
            )
        )
        assert row is not None
        assert row.is_active is False
