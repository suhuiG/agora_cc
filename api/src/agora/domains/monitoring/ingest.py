"""ADR-0053 span normalization for the aggregate ingestion Lambda."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

PARSER_VERSION = "adr-0053-v1"
AGGREGATE_SCHEMA_VERSION = 3
_RUNTIME_ID = re.compile(r"[:/]runtime/([^/]+)/runtime-endpoint/")
_KNOWN_SPANS = {
    ("POST /", "opentelemetry.instrumentation.starlette", "SERVER"),
    ("chat", "strands.telemetry.tracer", "INTERNAL"),
    ("execute_event_loop_cycle", "strands.telemetry.tracer", "INTERNAL"),
    ("invoke_agent Strands Agents", "strands.telemetry.tracer", "INTERNAL"),
}
_MODEL_SCOPE = "opentelemetry.instrumentation.botocore.bedrock-runtime"


@dataclass(frozen=True)
class ParsedSpan:
    trace_id: str
    span_id: str
    runtime_id: str
    service_name: str
    hour_bucket: str
    observed_at: str
    invocations: int
    input_tokens: int
    output_tokens: int
    errors: int
    duration_nano: int
    span_count: int
    unclassified_count: int


def parse_span(span: dict[str, Any]) -> ParsedSpan | None:
    required = ("traceId", "spanId", "parentSpanId", "durationNano")
    if not all(key in span for key in required):
        return None
    resource = span.get("resource")
    attributes = span.get("attributes")
    if not isinstance(resource, dict) or not isinstance(attributes, dict):
        return None
    resource_attributes = resource.get("attributes")
    if not isinstance(resource_attributes, dict):
        return None
    resource_id = resource_attributes.get("cloud.resource_id")
    match = _RUNTIME_ID.search(resource_id) if isinstance(resource_id, str) else None
    if match is None:
        return None
    try:
        end_ns = int(span["endTimeUnixNano"])
        duration_ns = int(span["durationNano"])
    except (KeyError, TypeError, ValueError):
        return None
    ended_at = datetime.fromtimestamp(end_ns / 1_000_000_000, tz=timezone.utc)
    hour = ended_at.replace(minute=0, second=0, microsecond=0)
    name = span.get("name")
    scope = span.get("scope")
    scope_name = scope.get("name") if isinstance(scope, dict) else ""
    kind = str(span.get("kind") or "")
    invocation = (
        name == "invoke_agent Strands Agents"
        and scope_name == "strands.telemetry.tracer"
        and kind == "INTERNAL"
    )
    known = (name, scope_name, kind) in _KNOWN_SPANS or (
        isinstance(name, str)
        and name.startswith("chat ")
        and scope_name == _MODEL_SCOPE
        and kind == "CLIENT"
    )
    status = span.get("status")
    status_code = status.get("code") if isinstance(status, dict) else None
    return ParsedSpan(
        trace_id=str(span["traceId"]),
        span_id=str(span["spanId"]),
        runtime_id=match.group(1),
        service_name=str(resource_attributes.get("service.name") or ""),
        hour_bucket=hour.strftime("%Y-%m-%dT%H:00:00Z"),
        observed_at=ended_at.isoformat().replace("+00:00", "Z"),
        invocations=1 if invocation else 0,
        input_tokens=int(attributes.get("gen_ai.usage.input_tokens", 0))
        if invocation else 0,
        output_tokens=int(attributes.get("gen_ai.usage.output_tokens", 0))
        if invocation else 0,
        errors=1 if invocation and status_code == "ERROR" else 0,
        duration_nano=duration_ns if invocation else 0,
        span_count=1,
        unclassified_count=0 if known else 1,
    )
