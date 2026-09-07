"""Metadata-only contracts for admin agent monitoring."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

MetricSource = Literal["span", "log", "ledger", "aws_api", "aggregate"]
UnobservedReason = Literal[
    "no_instrumentation",
    "no_aggregate",
    "aggregate_not_deployed",
    "ingest_stalled",
    "traffic_unobserved",
    "no_data_in_range",
    "ingest_failed",
    "ingest_health_unavailable",
    "dlq_unobserved",
    "no_successful_ingest",
    "pipeline_unobserved",
    "pipeline_timestamps_unobserved",
    "not_ingest_owner",
    "sampling_unknown",
    "latency_overflow",
    "cost_contract_unavailable",
    "population_incomplete",
    "not_sampled",
    "pipeline_lag",
    "not_applicable",
]


class ObservedMetric(BaseModel):
    value: float
    status: Literal["ok"] = "ok"
    as_of: datetime | None
    source: MetricSource
    sampling_rate: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description=(
            "Independently observed aws/spans arrival coverage; excludes "
            "X-Ray control-plane rates and requested_sampling"
        ),
    )
    window_start: datetime | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    window_end: datetime | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class UnobservedMetric(BaseModel):
    value: None = None
    status: Literal["unobserved"] = "unobserved"
    reason: UnobservedReason | None = None
    as_of: datetime | None
    source: MetricSource
    sampling_rate: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description=(
            "Independently observed aws/spans arrival coverage; excludes "
            "X-Ray control-plane rates and requested_sampling"
        ),
    )
    window_start: datetime | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    window_end: datetime | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


Metric = Annotated[
    ObservedMetric | UnobservedMetric,
    Field(discriminator="status"),
]


class SummaryTile(BaseModel):
    primary: Metric
    secondary: Metric | None = None


class FleetSummary(BaseModel):
    coherence_anomalies: SummaryTile
    authorization_denials: SummaryTile
    cost: SummaryTile
    errors: SummaryTile
    observability_health: SummaryTile


class DeploymentState(BaseModel):
    phase: str | None
    as_of: datetime | None


class Reconciliation(BaseModel):
    expected: list[str] | None
    observed: list[str] | None
    verdict: Literal["coherent", "diverged", "unknown"]
    reason: Literal[
        "unknown_authorization",
        "not_applicable",
        "builtin_reachability_unprobed",
        "verify_missing",
        "report_invalid",
        "invalid_tool_identifier",
        "verify_unknown",
        "self_validation",
        "deployment_version_mismatch",
        "verdict_conflict",
        "declaration_unresolvable",
    ] | None = None
    expected_source: Literal["deployment_ledger", "runtime_selfcheck"]
    observed_source: Literal["deployment_ledger", "runtime_selfcheck"]
    self_validation: bool = False

    @model_validator(mode="after")
    def mark_self_validation(self) -> "Reconciliation":
        self.self_validation = self.expected_source == self.observed_source
        return self


class AgentMetrics(BaseModel):
    invocations: Metric
    tokens: Metric
    estimated_cost: Metric
    p95_latency: Metric
    error_rate: Metric


class PipelineHealth(BaseModel):
    status: Literal["ok", "idle", "unknown", "stale", "failed"]
    last_success_at: datetime | None = Field(
        description="Wall-clock time of the latest successful accepted ingest"
    )
    newest_span_at: datetime | None = Field(
        default=None,
        description="Newest accepted span timestamp used for data freshness",
    )
    failure_count: int = Field(ge=0)
    dlq_depth: int | None = Field(default=None, ge=0)
    reason: str | None = None
    gap_start_at: datetime | None = None
    gap_end_at: datetime | None = None
    replay_status: Literal["partial", "completed"] | None = None
    replay_expected_count: int = Field(default=0, ge=0)
    replay_recovered_count: int = Field(default=0, ge=0)
    replay_failed_count: int = Field(default=0, ge=0)
    traffic_window_start: datetime | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    traffic_window_end: datetime | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    traffic_count: int | None = Field(
        default=None, ge=0, exclude_if=lambda value: value is None
    )
    traffic_observation_status: Literal[
        "not_required", "ok", "unknown"
    ] = Field(
        default="not_required",
        exclude_if=lambda value: value == "not_required",
    )
    traffic_observation_reason: str | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    pending_failure_count: int | None = Field(default=None, ge=0)


class GovernanceState(BaseModel):
    scan_status: str | None
    scan_risk: str | None
    gate_verdict: str | None
    as_of: datetime | None
    reason: Literal["scan_version_mismatch", "scan_version_unknown"] | None = None


class AgentMonitoring(BaseModel):
    record_id: str
    name: str
    version: str
    owner: str
    owner_team: str
    registry_status: str
    deployment: DeploymentState
    reconciliation: Reconciliation
    instrumentation: Literal["enabled", "not_configured", "unknown"]
    metrics: AgentMetrics
    pipeline: PipelineHealth
    governance: GovernanceState


class AgentMonitoringFleet(BaseModel):
    summary: FleetSummary
    agents: list[AgentMonitoring]
    pipeline: PipelineHealth
    observed_population: int = Field(ge=0)
    truncated: bool
