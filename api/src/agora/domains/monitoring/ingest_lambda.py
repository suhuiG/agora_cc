"""CloudWatch Logs subscription consumer for hourly monitoring aggregates."""

from __future__ import annotations

import base64
import gzip
import json
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from importlib.resources import files
from typing import Callable, Mapping

from botocore.exceptions import ClientError

from .ingest import (
    AGGREGATE_SCHEMA_VERSION,
    PARSER_VERSION,
    ParsedSpan,
    parse_span,
)
from .ingest_failure import FailureEvidenceSource, IngestFailureLedger

_LATENCY_BOUNDS_MS = (100, 250, 500, 1000, 2500, 5000, 10000, 30000)
# The Lambda timeout is 60 seconds. Four full-jitter retries sleep at most
# 25+50+100+200ms per span; the shared 2s budget and 5s reserve bound a batch.
_TRANSACTION_CONFLICT_MAX_RETRIES = 4
_TRANSACTION_CONFLICT_BASE_DELAY_SECONDS = 0.025
_TRANSACTION_CONFLICT_MAX_DELAY_SECONDS = 0.2
_TRANSACTION_CONFLICT_SLEEP_BUDGET_SECONDS = 2.0
_TRANSACTION_CONFLICT_MIN_REMAINING_MILLIS = 5_000


@dataclass(frozen=True)
class MonitoringIngestConfig:
    stage: str
    region: str
    aggregate_table: str
    deploy_jobs_table: str
    retention_days: int

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] = os.environ,
    ) -> "MonitoringIngestConfig":
        if env.get("AGORA_ROLE") != "lambda":
            raise ValueError("monitoring ingest requires AGORA_ROLE=lambda")
        stage = env.get("AGORA_MONITORING_STAGE", "")
        if stage not in ("dev", "prod"):
            raise ValueError("AGORA_MONITORING_STAGE must be dev or prod")
        region = env.get("AWS_REGION", "").strip()
        aggregate_table = env.get(
            "AGORA_MONITORING_AGGREGATE_TABLE", ""
        ).strip()
        deploy_jobs_table = env.get(
            "AGORA_MONITORING_DEPLOY_JOBS_TABLE", ""
        ).strip()
        if not region or not aggregate_table or not deploy_jobs_table:
            raise ValueError("monitoring ingest coordinates must be non-empty")
        try:
            retention_days = int(env["AGORA_MONITORING_RETENTION_DAYS"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "AGORA_MONITORING_RETENTION_DAYS must be a positive integer"
            ) from exc
        if retention_days < 1:
            raise ValueError(
                "AGORA_MONITORING_RETENTION_DAYS must be a positive integer"
            )
        return cls(
            stage=stage,
            region=region,
            aggregate_table=aggregate_table,
            deploy_jobs_table=deploy_jobs_table,
            retention_days=retention_days,
        )


_DEPLOY_JOBS_SCAN_CONTRACT = json.loads(
    files(__package__).joinpath(
        "deploy-jobs-scan-contract.json"
    ).read_text(encoding="utf-8")
)


def _ddb(item: dict) -> dict:
    # Table.meta.client keeps the resource serializer event handlers.
    return item


class TransactionConflictExhausted(RuntimeError):
    def __init__(self, *, attempts: int) -> None:
        super().__init__(
            f"DynamoDB transaction conflict exhausted after {attempts} attempts"
        )
        self.attempts = attempts


def _cancellation_codes(exc: Exception) -> tuple[str, ...]:
    response = getattr(exc, "response", {})
    if response.get("Error", {}).get("Code") != "TransactionCanceledException":
        return ()
    return tuple(
        str(reason.get("Code", ""))
        for reason in response.get("CancellationReasons", [])
    )


class SpanAggregateWriter:
    def __init__(
        self,
        table,
        *,
        retention_days: int = 400,
        now=None,
        sleep: Callable[[float], None] | None = None,
        jitter: Callable[[], float] | None = None,
        remaining_time_millis: Callable[[], int] | None = None,
        max_conflict_retries: int = _TRANSACTION_CONFLICT_MAX_RETRIES,
        retry_sleep_budget_seconds: float = (
            _TRANSACTION_CONFLICT_SLEEP_BUDGET_SECONDS
        ),
    ) -> None:
        if max_conflict_retries < 0:
            raise ValueError("max_conflict_retries must be non-negative")
        if retry_sleep_budget_seconds < 0:
            raise ValueError("retry_sleep_budget_seconds must be non-negative")
        self._table = table
        self._retention_days = retention_days
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep or time.sleep
        self._jitter = jitter or random.random
        self._remaining_time_millis = remaining_time_millis or (
            lambda: 60_000
        )
        self._max_conflict_retries = max_conflict_retries
        self._remaining_retry_sleep_budget_seconds = (
            retry_sleep_budget_seconds
        )

    def add(
        self,
        record_id: str,
        span: ParsedSpan,
        *,
        failure_evidence_key: Mapping[str, str] | None = None,
    ) -> bool:
        now = self._now()
        expiry = int((now + timedelta(days=self._retention_days)).timestamp())
        increments = {
            "span_count": span.span_count,
            "unclassified_count": span.unclassified_count,
            "invocations": span.invocations,
            "input_tokens": span.input_tokens,
            "output_tokens": span.output_tokens,
            "errors": span.errors,
            # Keep arrival coverage unknown until an independent denominator
            # is wired.
            "sampling_unknown_count": 1,
            "sampling_contract_span_count": span.span_count,
            "aggregate_schema_versions": {
                Decimal(AGGREGATE_SCHEMA_VERSION)
            },
            "parser_versions": {PARSER_VERSION},
        }
        duration_ms = span.duration_nano / 1_000_000
        if span.invocations:
            for bound in _LATENCY_BOUNDS_MS:
                if duration_ms <= bound:
                    increments[f"latency_le_{bound}ms"] = 1
            if duration_ms > _LATENCY_BOUNDS_MS[-1]:
                increments["latency_overflow"] = 1
        names = {f"#n{index}": name for index, name in enumerate(increments)}
        values = {
            f":v{index}": value
            for index, value in enumerate(increments.values())
        }
        add_expression = ", ".join(
            f"{alias} :v{index}"
            for index, alias in enumerate(names)
        )
        set_parts = [
            "#source = :source",
            "ingested_at = :ingested",
            "parser_version = :parser",
            "aggregate_schema_version = :aggregate_schema",
            "as_of = :as_of",
            "expires_at = :expiry",
        ]
        expression_values = {
            ":source": "aws/spans",
            ":ingested": now.isoformat().replace("+00:00", "Z"),
            ":parser": PARSER_VERSION,
            ":aggregate_schema": AGGREGATE_SCHEMA_VERSION,
            ":as_of": span.observed_at,
            ":expiry": expiry,
            **values,
        }
        transact_items = [
            {
                "Put": {
                    "TableName": self._table.name,
                    "Item": _ddb({
                        "PK": f"DEDUP#{span.trace_id}",
                        "SK": span.span_id,
                        "expires_at": expiry,
                    }),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            },
            {
                "Update": {
                    "TableName": self._table.name,
                    "Key": _ddb({
                        "PK": f"AGENT#{record_id}",
                        "SK": f"HOUR#{span.hour_bucket}",
                    }),
                    "UpdateExpression": (
                        f"SET {', '.join(set_parts)} "
                        f"ADD {add_expression}"
                    ),
                    "ExpressionAttributeNames": {
                        "#source": "source",
                        **names,
                    },
                    "ExpressionAttributeValues": _ddb(expression_values),
                }
            },
        ]
        if failure_evidence_key is not None:
            transact_items.append({
                "Update": {
                    "TableName": self._table.name,
                    "Key": _ddb(dict(failure_evidence_key)),
                    "UpdateExpression": (
                        "SET #status=:resolved, resolved_at=:resolved_at, "
                        "resolution=:resolution"
                    ),
                    "ConditionExpression": "#status=:pending",
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": _ddb({
                        ":pending": "PENDING",
                        ":resolved": "RESOLVED",
                        ":resolved_at": now.isoformat().replace("+00:00", "Z"),
                        ":resolution": "aggregate_transaction",
                    }),
                }
            })
        retries = 0
        while True:
            try:
                self._table.meta.client.transact_write_items(
                    TransactItems=transact_items
                )
                return True
            except Exception as exc:
                codes = _cancellation_codes(exc)
                if "ConditionalCheckFailed" in codes:
                    return False
                if "TransactionConflict" not in codes:
                    raise
                attempts = retries + 1
                if retries >= self._max_conflict_retries:
                    raise TransactionConflictExhausted(
                        attempts=attempts
                    ) from exc
                delay_ceiling = min(
                    _TRANSACTION_CONFLICT_BASE_DELAY_SECONDS * (2**retries),
                    _TRANSACTION_CONFLICT_MAX_DELAY_SECONDS,
                )
                delay = min(
                    delay_ceiling * self._jitter(),
                    self._remaining_retry_sleep_budget_seconds,
                )
                if (
                    self._remaining_retry_sleep_budget_seconds <= 0
                    or self._remaining_time_millis()
                    <= (
                        _TRANSACTION_CONFLICT_MIN_REMAINING_MILLIS
                        + int(delay * 1000)
                    )
                ):
                    raise TransactionConflictExhausted(
                        attempts=attempts
                    ) from exc
                self._sleep(delay)
                self._remaining_retry_sleep_budget_seconds -= delay
                retries += 1


@dataclass(frozen=True)
class SubscriptionEvent:
    payload: dict
    source: FailureEvidenceSource


@dataclass(frozen=True)
class SubscriptionBatch:
    events: list[SubscriptionEvent]
    started_at: str | None
    ended_at: str | None


def _subscription_batch(event: dict) -> SubscriptionBatch:
    encoded = event["awslogs"]["data"]
    envelope = json.loads(gzip.decompress(base64.b64decode(encoded)))
    log_group = envelope.get("logGroup")
    log_stream = envelope.get("logStream")
    if (
        not isinstance(log_group, str)
        or not log_group
        or not isinstance(log_stream, str)
        or not log_stream
    ):
        raise ValueError("subscription log group and stream must be non-empty")
    log_events = envelope.get("logEvents", [])
    timestamps = [
        item["timestamp"]
        for item in log_events
        if isinstance(item.get("timestamp"), (int, float))
    ]

    def timestamp(value: int | float) -> str:
        return datetime.fromtimestamp(
            value / 1000,
            tz=timezone.utc,
        ).isoformat().replace("+00:00", "Z")

    events: list[SubscriptionEvent] = []
    for item in log_events:
        event_id = item.get("id")
        event_timestamp = item.get("timestamp")
        if (
            not isinstance(event_id, str)
            or not event_id
            or not isinstance(event_timestamp, int)
        ):
            raise ValueError(
                "subscription event ID and timestamp must be present"
            )
        events.append(SubscriptionEvent(
            payload=json.loads(item["message"]),
            source=FailureEvidenceSource(
                log_group=log_group,
                log_stream=log_stream,
                event_id=event_id,
                event_timestamp_ms=event_timestamp,
            ),
        ))
    return SubscriptionBatch(
        events=events,
        started_at=timestamp(min(timestamps)) if timestamps else None,
        ended_at=timestamp(max(timestamps)) if timestamps else None,
    )


def _owned_runtimes(table) -> dict[str, str]:
    output: dict[str, str] = {}
    projection = _DEPLOY_JOBS_SCAN_CONTRACT["projectionAttributes"]
    request: dict = {
        "Select": _DEPLOY_JOBS_SCAN_CONTRACT["select"],
        "ProjectionExpression": ", ".join(
            "#asset_type" if name == "asset_type" else name
            for name in projection
        ),
        "ExpressionAttributeNames": {"#asset_type": "asset_type"},
    }
    while True:
        page = table.scan(**request)
        for item in page.get("Items", []):
            runtime_id = item.get("runtime_id")
            record_id = item.get("redeploy_record_id") or item.get("record_id")
            if (
                item.get("asset_type") == "agent"
                and runtime_id
                and record_id
            ):
                output[str(runtime_id)] = str(record_id)
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            return output
        request["ExclusiveStartKey"] = last_key


def _metric(name: str, value: int, *, stage: str) -> None:
    print(json.dumps({
        "_aws": {
            "Timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": "Agora/MonitoringAggregate",
                "Dimensions": [["Stage"]],
                "Metrics": [{"Name": name, "Unit": "Count"}],
            }],
        },
        "Stage": stage,
        name: value,
    }))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _advance_pipeline_timestamp(
    table,
    *,
    timestamp_field: str,
    epoch_field: str,
    value: datetime,
) -> None:
    normalized = value.astimezone(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    elapsed = normalized - epoch
    epoch_us = (
        elapsed.days * 86_400_000_000
        + elapsed.seconds * 1_000_000
        + elapsed.microseconds
    )
    try:
        table.update_item(
            Key={"PK": "PIPELINE", "SK": "STATUS"},
            UpdateExpression="SET #timestamp=:timestamp, #epoch=:epoch",
            ConditionExpression=(
                "attribute_not_exists(#epoch) OR #epoch < :epoch"
            ),
            ExpressionAttributeNames={
                "#timestamp": timestamp_field,
                "#epoch": epoch_field,
            },
            ExpressionAttributeValues={
                ":timestamp": normalized.isoformat(
                    timespec="microseconds"
                ).replace("+00:00", "Z"),
                ":epoch": epoch_us,
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != (
            "ConditionalCheckFailedException"
        ):
            raise


def handler(event, context):
    import boto3

    config = MonitoringIngestConfig.from_env()
    aggregate_table = boto3.resource(
        "dynamodb", region_name=config.region
    ).Table(
        config.aggregate_table
    )
    deploy_table = boto3.resource(
        "dynamodb", region_name=config.region
    ).Table(
        config.deploy_jobs_table
    )
    writer = SpanAggregateWriter(
        aggregate_table,
        retention_days=config.retention_days,
        remaining_time_millis=(
            context.get_remaining_time_in_millis
            if context is not None
            and callable(
                getattr(context, "get_remaining_time_in_millis", None)
            )
            else None
        ),
    )
    failure_ledger = IngestFailureLedger(aggregate_table)
    started_at = _utc_now()
    now = started_at.isoformat().replace("+00:00", "Z")
    batch: SubscriptionBatch | None = None
    try:
        # Decode first so a downstream failure can retain the affected event
        # interval instead of reporting only the Lambda's processing time.
        batch = _subscription_batch(event)
        ownership = _owned_runtimes(deploy_table)
        accepted = duplicates = foreign = invalid = unclassified = 0
        failures = new_failures = 0
        failed_event_timestamps: list[int] = []
        accepted_as_of: list[str] = []
        for subscription_event in batch.events:
            span = parse_span(subscription_event.payload)
            if span is None:
                invalid += 1
                continue
            record_id = ownership.get(span.runtime_id)
            if record_id is None:
                foreign += 1
                continue
            unclassified += span.unclassified_count
            try:
                added = writer.add(record_id, span)
            except TransactionConflictExhausted as exc:
                evidence_status = failure_ledger.record_transaction_conflict(
                    source=subscription_event.source,
                    record_id=record_id,
                    span=span,
                    attempts=exc.attempts,
                )
                if evidence_status == "already_ingested":
                    duplicates += 1
                    continue
                failures += 1
                if evidence_status == "pending":
                    new_failures += 1
                    failed_event_timestamps.append(
                        subscription_event.source.event_timestamp_ms
                    )
                continue
            if added:
                accepted += 1
                accepted_as_of.append(span.observed_at)
            else:
                duplicates += 1
        update = (
            "ADD accepted_count :accepted, duplicate_count :duplicates, "
            "foreign_count :foreign, invalid_count :invalid"
        )
        values = {
            ":accepted": accepted,
            ":duplicates": duplicates,
            ":foreign": foreign,
            ":invalid": invalid,
        }
        if new_failures:
            def event_timestamp(value: int) -> str:
                return datetime.fromtimestamp(
                    value / 1000,
                    tz=timezone.utc,
                ).isoformat().replace("+00:00", "Z")

            update = (
                "SET last_failure_at=:failure_at, "
                "coverage_gap_started_at="
                "if_not_exists(coverage_gap_started_at,:gap_start), "
                "coverage_gap_ended_at=:gap_end "
                f"{update}, failure_count :failures, "
                "failure_evidence_count :failures"
            )
            values.update({
                ":failure_at": now,
                ":gap_start": event_timestamp(
                    min(failed_event_timestamps)
                ),
                ":gap_end": event_timestamp(
                    max(failed_event_timestamps)
                ),
                ":failures": new_failures,
            })
        aggregate_table.update_item(
            Key={"PK": "PIPELINE", "SK": "STATUS"},
            UpdateExpression=update,
            ExpressionAttributeValues=values,
        )
        if accepted_as_of:
            _advance_pipeline_timestamp(
                aggregate_table,
                timestamp_field="last_ingest_success_at",
                epoch_field="last_ingest_success_epoch_us",
                value=_utc_now(),
            )
            _advance_pipeline_timestamp(
                aggregate_table,
                timestamp_field="newest_span_at",
                epoch_field="newest_span_epoch_us",
                value=max(
                    datetime.fromisoformat(
                        value.replace("Z", "+00:00")
                    )
                    for value in accepted_as_of
                ),
            )
        if accepted:
            _metric("IngestSuccess", 1, stage=config.stage)
        if failures:
            _metric("IngestFailure", failures, stage=config.stage)
        _metric("ForeignSpans", foreign, stage=config.stage)
        _metric("UnclassifiedSpans", unclassified, stage=config.stage)
        return {
            "accepted": accepted,
            "duplicates": duplicates,
            "foreign": foreign,
            "failures": failures,
        }
    except Exception:
        update = "SET last_failure_at=:now"
        values: dict = {":now": now, ":one": 1}
        if (
            batch is not None
            and batch.started_at is not None
            and batch.ended_at is not None
        ):
            update += (
                ", coverage_gap_started_at="
                "if_not_exists(coverage_gap_started_at,:gap_start), "
                "coverage_gap_ended_at=:gap_end"
            )
            values.update({
                ":gap_start": batch.started_at,
                ":gap_end": batch.ended_at,
            })
        update += " ADD failure_count :one"
        aggregate_table.update_item(
            Key={"PK": "PIPELINE", "SK": "STATUS"},
            UpdateExpression=update,
            ExpressionAttributeValues=values,
        )
        _metric("IngestFailure", 1, stage=config.stage)
        raise
