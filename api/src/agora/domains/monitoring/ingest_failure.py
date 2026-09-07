"""Metadata-only evidence for monitoring spans that could not be aggregated."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from typing import Literal

from botocore.exceptions import ClientError

from .ingest import ParsedSpan

FAILURE_EVIDENCE_PARTITION = "INGEST_FAILURE"
FAILURE_EVIDENCE_VERSION = 1


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


@dataclass(frozen=True)
class FailureEvidenceSource:
    log_group: str
    log_stream: str
    event_id: str
    event_timestamp_ms: int

    def __post_init__(self) -> None:
        if not self.log_group or not self.log_stream or not self.event_id:
            raise ValueError("failure evidence source identifiers must be non-empty")
        if self.event_timestamp_ms < 0:
            raise ValueError("failure evidence timestamp must be non-negative")

    @property
    def key(self) -> dict[str, str]:
        identity = "\0".join((
            self.log_group,
            self.log_stream,
            self.event_id,
        ))
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return {
            "PK": FAILURE_EVIDENCE_PARTITION,
            "SK": f"EVENT#{digest}",
        }


@dataclass(frozen=True)
class PendingFailureEvidence:
    source: FailureEvidenceSource
    record_id: str
    trace_id: str
    span_id: str
    observed_at: str
    key: dict[str, str]


@dataclass(frozen=True)
class FailureEvidenceSummary:
    total_count: int
    pending_count: int
    resolved_count: int
    latest_created_at: datetime | None
    latest_resolved_at: datetime | None
    latest_ingest_at: datetime | None
    latest_resolved_span_at: datetime | None


class IngestFailureLedger:
    def __init__(self, table, *, now=None) -> None:
        self._table = table
        self._now = now or (lambda: datetime.now(timezone.utc))

    def record_transaction_conflict(
        self,
        *,
        source: FailureEvidenceSource,
        record_id: str,
        span: ParsedSpan,
        attempts: int,
    ) -> Literal["pending", "already_pending", "already_ingested"]:
        created_at = self._now().astimezone(timezone.utc)
        event_timestamp = datetime.fromtimestamp(
            source.event_timestamp_ms / 1000,
            tz=timezone.utc,
        )
        item = {
            **source.key,
            "status": "PENDING",
            "evidence_version": FAILURE_EVIDENCE_VERSION,
            "reason": "transaction_conflict_exhausted",
            "log_group": source.log_group,
            "log_stream": source.log_stream,
            "event_id": source.event_id,
            "event_timestamp_ms": source.event_timestamp_ms,
            "event_timestamp": event_timestamp.isoformat().replace(
                "+00:00", "Z"
            ),
            "record_id": record_id,
            "trace_id": span.trace_id,
            "span_id": span.span_id,
            "observed_at": span.observed_at,
            "hour_bucket": span.hour_bucket,
            "attempts": attempts,
            "created_at": created_at.isoformat().replace("+00:00", "Z"),
        }
        created = True
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(PK)",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != (
                "ConditionalCheckFailedException"
            ):
                raise
            created = False

        existing = self._table.get_item(
            Key=source.key,
            ConsistentRead=True,
        ).get("Item")
        if existing is None:
            raise RuntimeError("failure evidence conditional write has no existing item")
        expected_identity = {
            "log_group": source.log_group,
            "log_stream": source.log_stream,
            "event_id": source.event_id,
            "event_timestamp_ms": source.event_timestamp_ms,
            "record_id": record_id,
            "trace_id": span.trace_id,
            "span_id": span.span_id,
            "observed_at": span.observed_at,
        }
        if any(existing.get(name) != value for name, value in expected_identity.items()):
            raise RuntimeError("failure evidence identity mismatch")

        dedupe = self._table.get_item(
            Key={
                "PK": f"DEDUP#{span.trace_id}",
                "SK": span.span_id,
            },
            ConsistentRead=True,
        ).get("Item")
        if dedupe is not None:
            self._mark_resolved(source, resolution="dedupe_observed")
            return "already_ingested"
        if existing.get("status") == "RESOLVED":
            return "already_ingested"
        if existing.get("status") != "PENDING":
            raise RuntimeError("failure evidence has an unknown status")
        return "pending" if created else "already_pending"

    def _mark_resolved(
        self,
        source: FailureEvidenceSource,
        *,
        resolution: str,
    ) -> bool:
        resolved_at = self._now().astimezone(timezone.utc)
        try:
            self._table.update_item(
                Key=source.key,
                ConditionExpression="#status=:pending",
                UpdateExpression=(
                    "SET #status=:resolved, resolved_at=:resolved_at, "
                    "resolution=:resolution"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":pending": "PENDING",
                    ":resolved": "RESOLVED",
                    ":resolved_at": resolved_at.isoformat().replace(
                        "+00:00", "Z"
                    ),
                    ":resolution": resolution,
                },
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == (
                "ConditionalCheckFailedException"
            ):
                existing = self._table.get_item(
                    Key=source.key,
                    ConsistentRead=True,
                ).get("Item")
                return bool(existing and existing.get("status") == "RESOLVED")
            raise
        return True

    def resolve_if_ingested(
        self,
        source: FailureEvidenceSource,
        span: ParsedSpan,
    ) -> bool:
        dedupe = self._table.get_item(
            Key={
                "PK": f"DEDUP#{span.trace_id}",
                "SK": span.span_id,
            },
            ConsistentRead=True,
        ).get("Item")
        if dedupe is None:
            return False
        return self._mark_resolved(
            source,
            resolution="dedupe_observed",
        )

    def _items(self) -> list[dict]:
        request: dict = {
            "KeyConditionExpression": "PK = :pk",
            "ExpressionAttributeValues": {
                ":pk": FAILURE_EVIDENCE_PARTITION
            },
            "ConsistentRead": True,
        }
        items: list[dict] = []
        while True:
            response = self._table.query(**request)
            items.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                return items
            request["ExclusiveStartKey"] = last_key

    def pending(self, *, limit: int = 100) -> tuple[PendingFailureEvidence, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        output: list[PendingFailureEvidence] = []
        for item in self._items():
            status = item.get("status")
            if status == "RESOLVED":
                continue
            if status != "PENDING":
                raise ValueError("failure evidence has an unknown status")
            source = FailureEvidenceSource(
                log_group=str(item["log_group"]),
                log_stream=str(item["log_stream"]),
                event_id=str(item["event_id"]),
                event_timestamp_ms=int(item["event_timestamp_ms"]),
            )
            output.append(PendingFailureEvidence(
                source=source,
                record_id=str(item["record_id"]),
                trace_id=str(item["trace_id"]),
                span_id=str(item["span_id"]),
                observed_at=str(item["observed_at"]),
                key=source.key,
            ))
            if len(output) == limit:
                break
        return tuple(output)

    def summary(self) -> FailureEvidenceSummary:
        items = self._items()
        statuses = [item.get("status") for item in items]
        if any(status not in ("PENDING", "RESOLVED") for status in statuses):
            raise ValueError("failure evidence has an unknown status")
        created = [
            value
            for item in items
            if (value := _timestamp(item.get("created_at"))) is not None
        ]
        resolved = [
            value
            for item in items
            if (value := _timestamp(item.get("resolved_at"))) is not None
        ]
        resolved_spans = [
            value
            for item in items
            if item.get("status") == "RESOLVED"
            and (value := _timestamp(item.get("observed_at"))) is not None
        ]
        ingested = [
            value
            for item in items
            if item.get("resolution") == "aggregate_transaction"
            and (value := _timestamp(item.get("resolved_at"))) is not None
        ]
        pending_count = statuses.count("PENDING")
        resolved_count = statuses.count("RESOLVED")
        return FailureEvidenceSummary(
            total_count=len(items),
            pending_count=pending_count,
            resolved_count=resolved_count,
            latest_created_at=max(created, default=None),
            latest_resolved_at=max(resolved, default=None),
            latest_ingest_at=max(ingested, default=None),
            latest_resolved_span_at=max(resolved_spans, default=None),
        )
