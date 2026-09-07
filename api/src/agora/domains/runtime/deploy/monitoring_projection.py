"""Body-free deployment projections for admin monitoring."""

from __future__ import annotations

from ....shared.gateway_tools import is_safe_tool_identifier
from .models import AgentMonitoringJobProjection, DeployJob

_SAFE_VERDICTS = {
    "coherent",
    "diverged",
    "unknown",
    "unknown_authorization",
    "not_applicable",
}
_SAFE_SOURCES = {"deployment_ledger", "runtime_selfcheck"}


def monitoring_projection(
    job: DeployJob,
) -> AgentMonitoringJobProjection | None:
    """Strip deployment state to the metadata allowed past the store boundary."""
    if job.asset_type != "agent":
        return None
    record_id = str(job.redeploy_record_id or job.record_id or "")
    if not record_id:
        return None

    report = job.verify_report
    reason: str | None = None
    verdict = ""
    observed_tools: tuple[str, ...] | None = None
    observed_source = "runtime_selfcheck"
    if report is None:
        reason = "verify_missing"
    elif not isinstance(report, dict):
        reason = "report_invalid"
    else:
        raw_verdict = report.get("verdict")
        raw_tools = report.get("tools")
        raw_source = report.get("observed_source")
        if raw_source in _SAFE_SOURCES:
            observed_source = raw_source
        if not isinstance(raw_verdict, str) or raw_verdict not in _SAFE_VERDICTS:
            reason = "report_invalid"
        else:
            verdict = raw_verdict
        if not isinstance(raw_tools, (list, tuple)):
            reason = "report_invalid"
        elif not all(is_safe_tool_identifier(tool) for tool in raw_tools):
            reason = "invalid_tool_identifier"
        else:
            observed_tools = tuple(raw_tools)
        if report.get("unprobed_builtin_tools"):
            reason = "builtin_reachability_unprobed"
        elif verdict in {"unknown_authorization", "not_applicable"}:
            reason = verdict
        elif verdict == "unknown" and reason is None:
            reason = "verify_unknown"

    return AgentMonitoringJobProjection(
        job_id=job.job_id,
        record_id=record_id,
        phase=job.phase.value,
        source_version=job.source_ref.version,
        created_at=job.created_at,
        updated_at=job.updated_at,
        otel_instrumentation_status=job.otel_instrumentation_status,
        verify_verdict=verdict,
        observed_tools=observed_tools,
        observed_source=observed_source,
        reason=reason,
    )
