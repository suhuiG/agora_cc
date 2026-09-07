"""Aggregate read contract for monitoring API responses."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal, Protocol

from .ingest import AGGREGATE_SCHEMA_VERSION, PARSER_VERSION
from .ingest_failure import FailureEvidenceSummary, IngestFailureLedger


@dataclass(frozen=True)
class PipelineHealth:
    status: Literal["ok", "idle", "unknown", "stale", "failed"]
    last_success_at: datetime | None
    failure_count: int
    dlq_depth: int | None
    reason: str | None = None
    gap_start_at: datetime | None = None
    gap_end_at: datetime | None = None
    replay_status: Literal["partial", "completed"] | None = None
    replay_expected_count: int = 0
    replay_recovered_count: int = 0
    replay_failed_count: int = 0
    newest_span_at: datetime | None = None
    traffic_window_start: datetime | None = None
    traffic_window_end: datetime | None = None
    traffic_count: int | None = None
    traffic_observation_status: Literal[
        "not_required", "ok", "unknown"
    ] = "not_required"
    traffic_observation_reason: str | None = None
    pending_failure_count: int | None = None


@dataclass(frozen=True)
class AggregateSnapshot:
    status: Literal["ok", "unobserved"]
    reason: str | None
    invocations: int | None
    input_tokens: int | None
    output_tokens: int | None
    p95_latency_ms: float | None
    error_rate: float | None
    as_of: datetime | None
    sampling_rate: float | None
    span_count: int
    unclassified_count: int
    window_start: datetime | None = None
    window_end: datetime | None = None
    latency_reason: str | None = None

    @classmethod
    def unobserved(
        cls,
        reason: str,
        *,
        as_of: datetime | None = None,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> "AggregateSnapshot":
        return cls(
            status="unobserved",
            reason=reason,
            invocations=None,
            input_tokens=None,
            output_tokens=None,
            p95_latency_ms=None,
            error_rate=None,
            as_of=as_of,
            sampling_rate=None,
            span_count=0,
            unclassified_count=0,
            window_start=window_start,
            window_end=window_end,
        )


class AggregateReader(Protocol):
    def batch_get(self, record_ids: list[str]) -> dict[str, AggregateSnapshot]: ...
    def pipeline_health(self) -> PipelineHealth: ...


class InMemoryAggregateReader:
    def __init__(
        self,
        *,
        snapshots: dict[str, AggregateSnapshot],
        pipeline: PipelineHealth,
    ) -> None:
        self._snapshots = snapshots
        self._pipeline = pipeline

    def batch_get(self, record_ids: list[str]) -> dict[str, AggregateSnapshot]:
        return {
            record_id: self._snapshots.get(
                record_id,
                AggregateSnapshot.unobserved("no_data_in_range"),
            )
            for record_id in record_ids
        }

    def pipeline_health(self) -> PipelineHealth:
        return self._pipeline


class DisabledAggregateReader(InMemoryAggregateReader):
    def __init__(self) -> None:
        super().__init__(
            snapshots={},
            pipeline=PipelineHealth(
                status="unknown",
                last_success_at=None,
                failure_count=0,
                dlq_depth=None,
                reason="aggregate_not_deployed",
            ),
        )

    def batch_get(self, record_ids: list[str]) -> dict[str, AggregateSnapshot]:
        missing = AggregateSnapshot.unobserved("aggregate_not_deployed")
        return {record_id: replace(missing) for record_id in record_ids}


class NonOwnerAggregateReader(InMemoryAggregateReader):
    def __init__(self) -> None:
        super().__init__(
            snapshots={},
            pipeline=PipelineHealth(
                status="unknown",
                last_success_at=None,
                failure_count=0,
                dlq_depth=None,
                reason="not_ingest_owner",
            ),
        )

    def batch_get(self, record_ids: list[str]) -> dict[str, AggregateSnapshot]:
        missing = AggregateSnapshot.unobserved("not_ingest_owner")
        return {record_id: replace(missing) for record_id in record_ids}


_LATENCY_BOUNDS_MS = (100, 250, 500, 1000, 2500, 5000, 10000, 30000)


def _number(value) -> int:
    return int(value) if isinstance(value, (int, Decimal)) else 0


def _utc_timestamp(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _observed_sampling_rate(
    items: list[dict],
) -> tuple[float | None, datetime | None]:
    """Read only independently observed aws/spans arrival coverage."""
    sampling_values = []
    observed_at = []
    for item in items:
        span_count = _number(item.get("span_count"))
        if (
            item.get("aggregate_schema_version")
            != AGGREGATE_SCHEMA_VERSION
            or item.get("aggregate_schema_versions")
            != {Decimal(AGGREGATE_SCHEMA_VERSION)}
            or item.get("parser_version") != PARSER_VERSION
            or item.get("parser_versions") != {PARSER_VERSION}
            or _number(item.get("sampling_contract_span_count"))
            != span_count
        ):
            return None, None
        if _number(item.get("sampling_unknown_count")) > 0:
            return None, None
        value = item.get("sampling_rate")
        if not isinstance(value, (int, float, Decimal)):
            return None, None
        decimal_value = Decimal(str(value))
        if not 0 <= decimal_value <= 1:
            return None, None
        recorded_values = item.get("sampling_rates")
        if not isinstance(recorded_values, set) or recorded_values != {
            decimal_value
        }:
            return None, None
        sampling_values.extend(recorded_values)
        sampling_time = _utc_timestamp(item.get("sampling_observed_at"))
        if sampling_time is None:
            return None, None
        observed_at.append(sampling_time)
    unique_sampling_values = set(sampling_values)
    return (
        (
            float(next(iter(unique_sampling_values)))
            if len(unique_sampling_values) == 1
            else None
        ),
        min(observed_at, default=None),
    )


class DynamoAggregateReader:
    """Read hourly aggregates and independently observe ingest/DLQ health."""

    def __init__(
        self,
        *,
        table_name: str,
        region: str,
        dlq_url: str,
        max_lag_seconds: int,
        table=None,
        sqs_client=None,
        now=None,
    ) -> None:
        self._table_name = table_name
        self._region = region
        self._dlq_url = dlq_url
        self._max_lag_seconds = max_lag_seconds
        self._table = table
        self._sqs = sqs_client
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _tbl(self):
        if self._table is None:
            import boto3
            self._table = boto3.resource(
                "dynamodb", region_name=self._region
            ).Table(self._table_name)
        return self._table

    def _sqs_client(self):
        if self._sqs is None:
            import boto3
            self._sqs = boto3.client("sqs", region_name=self._region)
        return self._sqs

    def batch_get(self, record_ids: list[str]) -> dict[str, AggregateSnapshot]:
        from boto3.dynamodb.conditions import Key

        current_hour = self._now().astimezone(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        start = current_hour - timedelta(hours=24)
        window_end = current_hour + timedelta(hours=1)
        output: dict[str, AggregateSnapshot] = {}
        for record_id in record_ids:
            try:
                response = self._tbl().query(
                    KeyConditionExpression=(
                        Key("PK").eq(f"AGENT#{record_id}")
                        & Key("SK").between(
                            f"HOUR#{start:%Y-%m-%dT%H:00:00Z}",
                            f"HOUR#{current_hour:%Y-%m-%dT%H:00:00Z}",
                        )
                    ),
                    ConsistentRead=True,
                )
            except Exception:
                output[record_id] = AggregateSnapshot.unobserved(
                    "ingest_failed",
                    window_start=start,
                    window_end=window_end,
                )
                continue
            items = response.get("Items", [])
            if not items:
                output[record_id] = AggregateSnapshot.unobserved(
                    "no_data_in_range",
                    window_start=start,
                    window_end=window_end,
                )
                continue
            invocations = sum(_number(item.get("invocations")) for item in items)
            if invocations == 0:
                output[record_id] = AggregateSnapshot.unobserved(
                    "no_data_in_range",
                    window_start=start,
                    window_end=window_end,
                )
                continue
            as_of_values = [
                datetime.fromisoformat(str(item["as_of"]).replace("Z", "+00:00"))
                for item in items
                if item.get("as_of")
            ]
            as_of = max(as_of_values, default=None)
            sampling_rate, sampling_as_of = (
                _observed_sampling_rate(items)
            )
            if sampling_as_of is not None:
                as_of = min(
                    value for value in (as_of, sampling_as_of)
                    if value is not None
                )
            histogram = [
                sum(_number(item.get(f"latency_le_{bound}ms")) for item in items)
                for bound in _LATENCY_BOUNDS_MS
            ]
            rank = max(1, (invocations * 95 + 99) // 100)
            p95 = next(
                (
                    bound
                    for bound, count in zip(_LATENCY_BOUNDS_MS, histogram)
                    if count >= rank
                ),
                None,
            )
            latency_overflow = sum(
                _number(item.get("latency_overflow")) for item in items
            )
            errors = sum(_number(item.get("errors")) for item in items)
            output[record_id] = AggregateSnapshot(
                status="ok",
                reason=None,
                invocations=invocations,
                input_tokens=sum(
                    _number(item.get("input_tokens")) for item in items
                ),
                output_tokens=sum(
                    _number(item.get("output_tokens")) for item in items
                ),
                p95_latency_ms=p95,
                error_rate=(errors / invocations) if invocations else None,
                as_of=as_of,
                sampling_rate=sampling_rate,
                span_count=sum(_number(item.get("span_count")) for item in items),
                unclassified_count=sum(
                    _number(item.get("unclassified_count")) for item in items
                ),
                window_start=start,
                window_end=window_end,
                latency_reason=(
                    "latency_overflow"
                    if p95 is None and latency_overflow > 0
                    else None
                ),
            )
        return output

    def pipeline_health(self) -> PipelineHealth:
        try:
            item = self._tbl().get_item(
                Key={"PK": "PIPELINE", "SK": "STATUS"},
                ConsistentRead=True,
            ).get("Item", {})
        except Exception:
            return PipelineHealth(
                "unknown", None, 0, None, "ingest_health_unavailable"
            )
        last_success = _utc_timestamp(item.get("last_ingest_success_at"))
        newest_span = _utc_timestamp(item.get("newest_span_at"))
        last_failure = (
            datetime.fromisoformat(str(item["last_failure_at"]).replace("Z", "+00:00"))
            if item.get("last_failure_at")
            else None
        )
        failures = _number(item.get("failure_count"))
        evidenced_failures = _number(item.get("failure_evidence_count"))
        gap_start = _utc_timestamp(item.get("coverage_gap_started_at"))
        gap_end = _utc_timestamp(item.get("coverage_gap_ended_at"))
        replay_status = {
            "PARTIAL": "partial",
            "COMPLETED": "completed",
        }.get(item.get("replay_status"))
        replay_expected = _number(item.get("replay_expected_count"))
        replay_recovered = _number(item.get("replay_recovered_count"))
        replay_failed = _number(item.get("replay_failed_count"))
        replay_resolved_failures = _number(
            item.get("replay_resolved_failure_count")
        )
        replay_completed_at = _utc_timestamp(item.get("replay_completed_at"))
        completed_replay_resolved_failure = (
            replay_status == "completed"
            and replay_expected > 0
            and replay_recovered == replay_expected
            and replay_failed == 0
            and replay_resolved_failures > 0
            and replay_completed_at is not None
            and last_failure is not None
            and last_failure <= replay_completed_at
            and failures == 0
        )
        evidence: FailureEvidenceSummary | None
        try:
            evidence = IngestFailureLedger(self._tbl()).summary()
        except Exception:
            evidence = None
        pending_failures = (
            evidence.pending_count if evidence is not None else None
        )
        if evidence is not None:
            if evidence.latest_ingest_at is not None:
                last_success = max(
                    value
                    for value in (
                        last_success,
                        evidence.latest_ingest_at,
                    )
                    if value is not None
                )
            if evidence.latest_resolved_span_at is not None:
                newest_span = max(
                    value
                    for value in (
                        newest_span,
                        evidence.latest_resolved_span_at,
                    )
                    if value is not None
                )
        try:
            attrs = self._sqs_client().get_queue_attributes(
                QueueUrl=self._dlq_url,
                AttributeNames=[
                    "ApproximateNumberOfMessages",
                    "ApproximateNumberOfMessagesNotVisible",
                    "ApproximateNumberOfMessagesDelayed",
                ],
            )["Attributes"]
            depth = sum(
                int(attrs[name])
                for name in (
                    "ApproximateNumberOfMessages",
                    "ApproximateNumberOfMessagesNotVisible",
                    "ApproximateNumberOfMessagesDelayed",
                )
            )
        except Exception:
            depth = None
        if replay_status == "partial":
            return PipelineHealth(
                "failed",
                last_success,
                failures,
                depth,
                "ingest_replay_partial",
                gap_start,
                gap_end,
                replay_status,
                replay_expected,
                replay_recovered,
                replay_failed,
                newest_span,
                pending_failure_count=pending_failures,
            )
        if (depth is not None and depth > 0) or (
            pending_failures is not None and pending_failures > 0
        ):
            return PipelineHealth(
                "failed",
                last_success,
                failures,
                depth,
                "ingest_failed",
                gap_start,
                gap_end,
                replay_status,
                replay_expected,
                replay_recovered,
                replay_failed,
                newest_span,
                pending_failure_count=pending_failures,
            )
        if depth is None:
            return PipelineHealth(
                "unknown",
                last_success,
                failures,
                None,
                "dlq_unobserved",
                newest_span_at=newest_span,
                pending_failure_count=pending_failures,
            )
        if evidence is None:
            return PipelineHealth(
                "unknown",
                last_success,
                failures,
                depth,
                "failure_evidence_unobserved",
                newest_span_at=newest_span,
                pending_failure_count=None,
            )
        if last_success is None or newest_span is None:
            return PipelineHealth(
                "unknown",
                last_success,
                failures,
                depth,
                "pipeline_timestamps_unobserved",
                gap_start,
                gap_end,
                replay_status,
                replay_expected,
                replay_recovered,
                replay_failed,
                newest_span,
                pending_failure_count=pending_failures,
            )
        ledger_resolved_failure = (
            failures > 0
            and evidenced_failures == failures
            and last_failure is not None
            and evidence.total_count >= evidenced_failures
            and evidence.total_count == evidence.resolved_count
            and evidence.pending_count == 0
            and evidence.latest_created_at is not None
            and evidence.latest_created_at >= last_failure
            and evidence.latest_resolved_at is not None
            and evidence.latest_resolved_at >= last_failure
        )
        failure_resolved = (
            completed_replay_resolved_failure or ledger_resolved_failure
        )
        if (
            last_failure is not None
            and not failure_resolved
            and (last_success is None or last_failure > last_success)
        ):
            return PipelineHealth(
                "failed",
                last_success,
                failures,
                depth,
                "ingest_failed",
                gap_start,
                gap_end,
                replay_status,
                replay_expected,
                replay_recovered,
                replay_failed,
                newest_span,
                pending_failure_count=pending_failures,
            )
        if failures > 0 and not failure_resolved:
            return PipelineHealth(
                "unknown",
                last_success,
                failures,
                depth,
                "ingest_data_gap",
                gap_start,
                gap_end,
                pending_failure_count=pending_failures,
            )
        observed_at = self._now()
        if (observed_at - newest_span).total_seconds() > self._max_lag_seconds:
            return PipelineHealth(
                "stale",
                last_success,
                failures,
                depth,
                "pipeline_lag",
                newest_span_at=newest_span,
                traffic_window_start=newest_span,
                traffic_window_end=observed_at,
                pending_failure_count=pending_failures,
            )
        return PipelineHealth(
            "ok",
            last_success,
            0 if failure_resolved else failures,
            depth,
            newest_span_at=newest_span,
            pending_failure_count=pending_failures,
        )


def fold_top_n(
    counts: dict[str, int],
    *,
    limit: int = 20,
) -> tuple[dict[str, int], int]:
    """Bound a high-cardinality dimension and return its explicit other count."""
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    kept = dict(ordered[:limit])
    return kept, sum(count for _, count in ordered[limit:])
