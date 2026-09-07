"""Replay metadata-only monitoring failure evidence from CloudWatch Logs."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Literal

from .ingest import parse_span
from .ingest_failure import IngestFailureLedger, PendingFailureEvidence
from .ingest_lambda import SpanAggregateWriter


@dataclass(frozen=True)
class FailureReplayReport:
    mode: Literal["dry-run", "apply"]
    discovered_count: int
    ready_count: int
    recovered_count: int
    failed_count: int
    errors: dict[str, str]


def _source_event(logs_client, evidence: PendingFailureEvidence) -> dict | None:
    source = evidence.source
    request = {
        "logGroupName": source.log_group,
        "logStreamNames": [source.log_stream],
        "startTime": source.event_timestamp_ms,
        "endTime": source.event_timestamp_ms + 1,
    }
    matches: list[dict] = []
    next_token: str | None = None
    while True:
        if next_token is not None:
            request["nextToken"] = next_token
        response = logs_client.filter_log_events(**request)
        matches.extend(
            event
            for event in response.get("events", [])
            if (
                str(event.get("eventId")) == source.event_id
                and int(event.get("timestamp", -1))
                == source.event_timestamp_ms
                and str(event.get("logStreamName")) == source.log_stream
            )
        )
        new_token = response.get("nextToken")
        if not new_token or new_token == next_token:
            break
        next_token = str(new_token)
    if len(matches) != 1:
        return None
    payload = json.loads(matches[0]["message"])
    return payload if isinstance(payload, dict) else None


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", {})
    code = response.get("Error", {}).get("Code")
    return str(code) if code else type(exc).__name__


def replay_ingest_failures(
    *,
    ledger: IngestFailureLedger,
    logs_client,
    writer: SpanAggregateWriter,
    apply: bool = False,
    max_events: int = 100,
) -> FailureReplayReport:
    """Read exact retained events and resolve only spans matching the ledger."""
    pending = ledger.pending(limit=max_events)
    ready = recovered = 0
    errors: dict[str, str] = {}
    for evidence in pending:
        event_id = evidence.source.event_id
        try:
            payload = _source_event(logs_client, evidence)
        except Exception as exc:
            errors[event_id] = f"source_lookup_failed:{_error_code(exc)}"
            continue
        if payload is None:
            errors[event_id] = "source_event_not_found"
            continue
        span = parse_span(payload)
        if (
            span is None
            or span.trace_id != evidence.trace_id
            or span.span_id != evidence.span_id
            or span.observed_at != evidence.observed_at
        ):
            errors[event_id] = "source_event_identity_mismatch"
            continue
        ready += 1
        if not apply:
            continue
        try:
            added = writer.add(
                evidence.record_id,
                span,
                failure_evidence_key=evidence.key,
            )
            resolved = added or ledger.resolve_if_ingested(
                evidence.source,
                span,
            )
        except Exception as exc:
            errors[event_id] = f"replay_failed:{_error_code(exc)}"
            continue
        if not resolved:
            errors[event_id] = "aggregate_not_confirmed"
            continue
        recovered += 1
    return FailureReplayReport(
        mode="apply" if apply else "dry-run",
        discovered_count=len(pending),
        ready_count=ready,
        recovered_count=recovered,
        failed_count=len(errors),
        errors=errors,
    )
