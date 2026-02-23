from __future__ import annotations

import json
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    CoverageDaily,
    DISCOVERY_STATUS_DELAYED,
    DISCOVERY_STATUS_FAILED,
    DISCOVERY_STATUS_SUCCESS,
    DiscoveryRun,
    Subscription,
    SyncRun,
)
from ..schemas import CoverageReport
from ..time_utils import local_day_bounds_utc


class CoverageService:
    def compute(self, session: Session, target_date: date) -> CoverageReport:
        day_start, day_end = local_day_bounds_utc(target_date)
        run = session.scalar(
            select(SyncRun)
            .where(SyncRun.started_at >= day_start, SyncRun.started_at < day_end)
            .order_by(SyncRun.started_at.desc())
            .limit(1)
        )
        if run is None:
            run = session.scalar(select(SyncRun).order_by(SyncRun.started_at.desc()).limit(1))

        subscriptions = session.scalars(select(Subscription).order_by(Subscription.name.asc())).all()
        total = len(subscriptions)

        status_by_sub: dict[int, str] = {}
        error_kind_by_sub: dict[int, str] = {}
        if run is not None:
            rows = session.execute(
                select(DiscoveryRun.subscription_id, DiscoveryRun.status, DiscoveryRun.error_kind)
                .where(DiscoveryRun.sync_run_id == run.id)
            ).all()
            for sub_id, status, error_kind in rows:
                status_by_sub[int(sub_id)] = str(status)
                if error_kind:
                    error_kind_by_sub[int(sub_id)] = str(error_kind)

        success_subs = 0
        delayed_subs = 0
        fail_subs = 0
        details: list[dict[str, str]] = []
        for sub in subscriptions:
            status = status_by_sub.get(sub.id, DISCOVERY_STATUS_FAILED)
            if status == DISCOVERY_STATUS_SUCCESS:
                success_subs += 1
            elif status == DISCOVERY_STATUS_DELAYED:
                delayed_subs += 1
            else:
                fail_subs += 1
            details.append(
                {
                    "name": sub.name,
                    "wechat_id": sub.wechat_id,
                    "status": status,
                    "error_kind": error_kind_by_sub.get(sub.id, ""),
                }
            )

        coverage_ratio = (success_subs + delayed_subs) / total if total else 1.0
        detail_json = json.dumps(details, ensure_ascii=False)

        existing = session.get(CoverageDaily, target_date)
        if existing is None:
            session.add(
                CoverageDaily(
                    date=target_date,
                    total_subs=total,
                    success_subs=success_subs,
                    delayed_subs=delayed_subs,
                    fail_subs=fail_subs,
                    coverage_ratio=coverage_ratio,
                    detail_json=detail_json,
                )
            )
        else:
            existing.total_subs = total
            existing.success_subs = success_subs
            existing.delayed_subs = delayed_subs
            existing.fail_subs = fail_subs
            existing.coverage_ratio = coverage_ratio
            existing.detail_json = detail_json

        return CoverageReport(
            date=target_date,
            total_subs=total,
            success_subs=success_subs,
            delayed_subs=delayed_subs,
            fail_subs=fail_subs,
            coverage_ratio=coverage_ratio,
            detail_json=detail_json,
        )
