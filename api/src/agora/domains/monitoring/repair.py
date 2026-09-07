"""Evidence-based repair of split monitoring pipeline timestamps."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .ingest_lambda import _advance_pipeline_timestamp


@dataclass(frozen=True)
class PipelineTimestampRepair:
    mode: str
    scanned_count: int
    aggregate_count: int
    last_ingest_success_at: datetime | None
    newest_span_at: datetime | None


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def repair_pipeline_timestamps(
    table,
    *,
    apply: bool = False,
) -> PipelineTimestampRepair:
    """Derive pipeline timestamps from retained aggregate-hour evidence."""
    request: dict = {
        "ProjectionExpression": "PK, SK, ingested_at, as_of",
        "ConsistentRead": True,
    }
    scanned = 0
    aggregate_count = 0
    ingest_times: list[datetime] = []
    span_times: list[datetime] = []
    while True:
        page = table.scan(**request)
        scanned += int(page.get("ScannedCount", len(page.get("Items", []))))
        for item in page.get("Items", []):
            if not (
                str(item.get("PK", "")).startswith("AGENT#")
                and str(item.get("SK", "")).startswith("HOUR#")
            ):
                continue
            aggregate_count += 1
            if value := _timestamp(item.get("ingested_at")):
                ingest_times.append(value)
            if value := _timestamp(item.get("as_of")):
                span_times.append(value)
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            break
        request["ExclusiveStartKey"] = last_key

    last_ingest = max(ingest_times, default=None)
    newest_span = max(span_times, default=None)
    if apply and last_ingest is not None:
        _advance_pipeline_timestamp(
            table,
            timestamp_field="last_ingest_success_at",
            epoch_field="last_ingest_success_epoch_us",
            value=last_ingest,
        )
    if apply and newest_span is not None:
        _advance_pipeline_timestamp(
            table,
            timestamp_field="newest_span_at",
            epoch_field="newest_span_epoch_us",
            value=newest_span,
        )
    return PipelineTimestampRepair(
        mode="apply" if apply else "dry-run",
        scanned_count=scanned,
        aggregate_count=aggregate_count,
        last_ingest_success_at=last_ingest,
        newest_span_at=newest_span,
    )
