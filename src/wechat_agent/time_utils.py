from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone


def local_day_bounds_utc(target_date: date) -> tuple[datetime, datetime]:
    local_tz = datetime.now().astimezone().tzinfo or timezone.utc
    start_local = datetime.combine(target_date, time.min).replace(tzinfo=local_tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def shift_midnight_publish_time(
    dt: datetime,
    *,
    is_midnight_publish: bool,
    shift_days: int = 2,
) -> datetime:
    if not is_midnight_publish or shift_days <= 0:
        return dt
    return dt + timedelta(days=shift_days)
