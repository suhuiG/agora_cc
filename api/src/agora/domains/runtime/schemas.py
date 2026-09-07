"""Runtime management console read model schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from .deploy.models import DeployJob, DeployPhase


class RuntimeDeploymentSummary(BaseModel):
    job_id: str
    asset_type: Literal["mcp", "agent"]
    name: str
    asset_id: str
    version: str
    phase: DeployPhase
    build_type: str
    record_id: str | None
    runtime_arn: str | None
    endpoint: str | None
    created_at: str
    updated_at: str
    error: dict[str, Any] | None
    identity_outbound_status: str | None
    identity_outbound_error: str | None
    identity_outbound_warnings: list[str]


class RuntimeDeploymentDetail(RuntimeDeploymentSummary):
    verify_report: dict[str, Any] | None
    tool_request_report: dict[str, Any] | None


class RuntimeDeploymentPage(BaseModel):
    items: list[RuntimeDeploymentSummary]
    total: int
    offset: int
    limit: int


def deployment_summary(job: DeployJob) -> RuntimeDeploymentSummary:
    """Convert an internal deploy job into the portal-safe list representation."""
    return RuntimeDeploymentSummary(
        job_id=job.job_id,
        asset_type=job.asset_type,
        name=str(job.meta.get("name") or job.source_ref.asset_id.rsplit("/", 1)[-1]),
        asset_id=job.source_ref.asset_id,
        version=job.source_ref.version,
        phase=job.phase,
        build_type=job.build_type,
        record_id=job.record_id,
        runtime_arn=job.runtime_arn,
        endpoint=job.gateway_url,
        created_at=job.created_at,
        updated_at=job.updated_at,
        error=job.error,
        identity_outbound_status=job.identity_outbound_status,
        identity_outbound_error=job.identity_outbound_error,
        identity_outbound_warnings=list(job.identity_outbound_warnings),
    )


def deployment_detail(job: DeployJob) -> RuntimeDeploymentDetail:
    """Convert an internal deploy job into the portal-safe detail representation."""
    summary = deployment_summary(job)
    return RuntimeDeploymentDetail(
        **summary.model_dump(),
        verify_report=job.verify_report,
        tool_request_report=job.tool_request_report,
    )
