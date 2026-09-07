"""감사 타임라인의 UTC 키와 bounded 조회 범위 계약."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


AUDIT_TIMELINE_INDEX = "AuditTimelineIndex"
AUDIT_TIMELINE_MAX_RANGE = timedelta(days=31)


def parse_audit_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("audit timestamp must be ISO8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("audit timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def format_audit_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def audit_timeline_keys(timestamp: str, event_id: str) -> tuple[str, str]:
    instant = parse_audit_timestamp(timestamp)
    # Python datetime은 9자리 나노초 입력을 6자리 마이크로초로 절삭해요.
    # 같은 마이크로초 안에서는 event_id가 정렬 tie-breaker라 중복·누락은 없어요.
    canonical = format_audit_timestamp(instant)
    return f"AUDIT#{instant.date().isoformat()}", f"{canonical}#{event_id}"


def resolve_audit_range(
    from_time: str | None,
    to_time: str | None,
    *,
    now: datetime,
) -> tuple[datetime, datetime]:
    upper = parse_audit_timestamp(to_time) if to_time else now.astimezone(timezone.utc)
    lower = (
        parse_audit_timestamp(from_time)
        if from_time
        else upper - timedelta(hours=24)
    )
    if lower >= upper:
        raise ValueError("audit timeline from must be before to")
    if upper - lower > AUDIT_TIMELINE_MAX_RANGE:
        raise ValueError("audit timeline range cannot exceed 31 days")
    return lower, upper


def audit_date_partitions(lower: datetime, upper: datetime) -> tuple[str, ...]:
    current = (upper - timedelta(microseconds=1)).date()
    first = lower.date()
    partitions = []
    while current >= first:
        partitions.append(f"AUDIT#{current.isoformat()}")
        current -= timedelta(days=1)
    return tuple(partitions)


def audit_partition_bounds(
    partition: str,
    lower: datetime,
    upper: datetime,
) -> tuple[str, str]:
    date_text = partition.removeprefix("AUDIT#")
    day_start = datetime.fromisoformat(date_text).replace(tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    bounded_lower = max(lower, day_start)
    bounded_upper = min(upper, day_end) - timedelta(microseconds=1)
    return (
        f"{format_audit_timestamp(bounded_lower)}#",
        f"{format_audit_timestamp(bounded_upper)}#\uffff",
    )
