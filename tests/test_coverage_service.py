from __future__ import annotations

from datetime import date
import json

from wechat_agent.config import get_settings
from wechat_agent.db import init_db, session_scope
from wechat_agent.models import (
    DISCOVERY_STATUS_FAILED,
    DISCOVERY_STATUS_SUCCESS,
    DiscoveryRun,
    Subscription,
    SyncRun,
    utcnow,
)
from wechat_agent.services.coverage_service import CoverageService


def test_coverage_service_collects_error_kind(isolated_env):
    settings = get_settings()
    init_db(settings)

    with session_scope(settings) as session:
        sub_ok = Subscription(name="号A", wechat_id="gh_a")
        sub_fail = Subscription(name="号B", wechat_id="gh_b")
        session.add_all([sub_ok, sub_fail])
        session.flush()

        run = SyncRun(trigger="view", started_at=utcnow(), success_count=1, fail_count=1)
        session.add(run)
        session.flush()

        session.add_all(
            [
                DiscoveryRun(
                    sync_run_id=run.id,
                    subscription_id=sub_ok.id,
                    channel="search_index",
                    status=DISCOVERY_STATUS_SUCCESS,
                    ref_count=1,
                    error_kind=None,
                    error_message=None,
                    latency_ms=20,
                ),
                DiscoveryRun(
                    sync_run_id=run.id,
                    subscription_id=sub_fail.id,
                    channel="weread",
                    status=DISCOVERY_STATUS_FAILED,
                    ref_count=0,
                    error_kind="AUTH_EXPIRED",
                    error_message="token expired",
                    latency_ms=30,
                ),
            ]
        )
        session.commit()

    with session_scope(settings) as session:
        report = CoverageService().compute(session=session, target_date=date.today())
        session.commit()

    assert report.total_subs == 2
    assert report.success_subs == 1
    assert report.fail_subs == 1
    assert report.coverage_ratio == 0.5
    details = json.loads(report.detail_json)
    assert any(item["error_kind"] == "AUTH_EXPIRED" for item in details)
