from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from wechat_agent.config import get_settings
from wechat_agent.db import init_db, session_scope
from wechat_agent.models import HEALTH_STATE_HALF_OPEN, HEALTH_STATE_OPEN, SourceHealth, Subscription, utcnow
from wechat_agent.models import SyncRun
from wechat_agent.schemas import SourceCandidate
from wechat_agent.services.source_gateway import SourceHealthService, SourceRouter


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
