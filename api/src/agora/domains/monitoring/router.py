"""Admin-only metadata monitoring for the agent fleet."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from ...shared.deps import (
    get_deploy_service,
    get_governance_monitoring_snapshots,
    get_monitoring_aggregate_reader,
    get_registry,
    get_registry_id,
    observe_agent_invoke_traffic,
)
from ...shared.config import load_config
from ..catalog.registry.models import RecordNotFound
from ..identity.context import current_principal
from .schemas import (
    AgentMetrics,
    AgentMonitoring,
    AgentMonitoringFleet,
    DeploymentState,
    FleetSummary,
    GovernanceState,
    ObservedMetric,
    PipelineHealth,
    Reconciliation,
    SummaryTile,
    UnobservedMetric,
)

router = APIRouter(tags=["monitoring"])


def _require_monitoring_admin(request: Request) -> None:
    if not current_principal(request).is_admin:
        raise HTTPException(403, "권한 없음 (agent 모니터링은 admin 전용)")


def _value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value or "")


def _timestamp(job, record) -> str | None:
    for candidate in (
        getattr(job, "updated_at", None) if job else None,
        getattr(job, "created_at", None) if job else None,
        getattr(record, "updated_at", None),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return None


def _unobserved(
    *,
    source: str,
    as_of: str | None,
    reason: str | None = None,
    sampling_rate: float | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
) -> UnobservedMetric:
    # Without an observed metric timestamp, response time would claim false freshness.
    return UnobservedMetric(
        value=None,
        status="unobserved",
        reason=reason,
        as_of=as_of,
        source=source,
        sampling_rate=sampling_rate,
        window_start=window_start,
        window_end=window_end,
    )


def _reconciliation(record, job) -> Reconciliation:
    expected = record.declared_tool_names
    if job is None:
        return Reconciliation(
            expected=list(expected) if expected is not None else None,
            observed=None,
            verdict="unknown",
            expected_source="deployment_ledger",
            observed_source="runtime_selfcheck",
            reason="verify_missing",
        )
    expected_source = "deployment_ledger"
    observed_source = job.observed_source
    observed = job.observed_tools
    reason = job.reason
    verdict = "unknown"
    if expected is None:
        reason = "declaration_unresolvable"
    elif job.source_version != record.version:
        reason = "deployment_version_mismatch"
    elif expected_source == observed_source:
        reason = "self_validation"
    elif reason is None and observed is not None:
        recomputed = "coherent" if tuple(expected) == tuple(observed) else "diverged"
        if job.verify_verdict != recomputed:
            reason = "verdict_conflict"
        else:
            verdict = recomputed
    return Reconciliation(
        expected=list(expected) if expected is not None else None,
        observed=list(observed) if observed is not None else None,
        verdict=verdict,
        reason=reason,
        expected_source=expected_source,
        observed_source=observed_source,
    )


def _instrumentation(job) -> str:
    value = getattr(job, "otel_instrumentation_status", None) if job else None
    return value if value in ("enabled", "not_configured") else "unknown"


def _instrumentation_lookup(jobs=None):
    """호출된 agent 의 계기 상태를 배포 원장에서 읽어요.

    조회 창의 트래픽은 지금 보고 있는 agent 가 아닐 수도 있어서, 단일 agent 화면도
    임의 agent_id 를 물어볼 수 있어야 해요. 원장에 없으면 `unknown` 이고, `unknown`
    은 '계기 있음' 으로 승격되지 않아요.
    """

    def lookup(agent_id: str) -> str:
        if jobs is not None and agent_id in jobs:
            return _instrumentation(jobs.get(agent_id))
        try:
            return _instrumentation(
                get_deploy_service().get_agent_monitoring_job(agent_id)
            )
        except Exception:
            return "unknown"

    return lookup


def _aggregate_for_pipeline(aggregate, pipeline):
    if pipeline.status == "stale":
        return aggregate.unobserved(
            pipeline.reason or "ingest_stalled",
            window_start=aggregate.window_start,
            window_end=aggregate.window_end,
        )
    if pipeline.status == "failed":
        return aggregate.unobserved(
            "ingest_failed",
            window_start=aggregate.window_start,
            window_end=aggregate.window_end,
        )
    if pipeline.status == "unknown":
        if pipeline.reason == "ingest_data_gap":
            return aggregate
        return aggregate.unobserved(
            pipeline.reason or "pipeline_unobserved",
            window_start=aggregate.window_start,
            window_end=aggregate.window_end,
        )
    return aggregate


def _resolve_pipeline_lag(pipeline, instrumentation_of=None):
    if pipeline.status != "stale" or pipeline.reason != "pipeline_lag":
        return pipeline
    if (
        pipeline.traffic_window_start is None
        or pipeline.traffic_window_end is None
    ):
        return replace(
            pipeline,
            status="unknown",
            reason="traffic_unobserved",
            traffic_observation_status="unknown",
            traffic_observation_reason="traffic_window_unobserved",
        )

    def as_datetime(value) -> datetime:
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    try:
        raw_window_start = as_datetime(pipeline.traffic_window_start)
        raw_window_end = as_datetime(pipeline.traffic_window_end)
    except ValueError:
        return replace(
            pipeline,
            status="unknown",
            reason="traffic_unobserved",
            traffic_count=None,
            traffic_observation_status="unknown",
            traffic_observation_reason="traffic_window_unobserved",
        )

    margin = timedelta(
        seconds=load_config().monitoring_traffic_boundary_margin_seconds
    )
    window_start = raw_window_start + margin
    window_end = raw_window_end - margin
    pipeline = replace(
        pipeline,
        traffic_window_start=window_start,
        traffic_window_end=window_end,
    )
    if window_start >= window_end:
        return replace(
            pipeline,
            status="unknown",
            reason="traffic_unobserved",
            traffic_count=None,
            traffic_observation_status="unknown",
            traffic_observation_reason="traffic_window_insufficient",
        )

    def isoformat(value) -> str:
        return value.isoformat() if hasattr(value, "isoformat") else str(value)

    observation = observe_agent_invoke_traffic(
        from_time=isoformat(window_start),
        to_time=isoformat(window_end),
    )
    if observation.status == "unknown":
        return replace(
            pipeline,
            status="unknown",
            reason="traffic_unobserved",
            traffic_count=None,
            traffic_observation_status="unknown",
            traffic_observation_reason=observation.reason,
        )
    if not observation.invocation_count:
        # 정착된 창에서 성공한 호출이 0건이에요. 적재가 멈춘 게 아니라 부를 사람이
        # 없었던 거라 **집계값을 가리지 않아요**(`_aggregate_for_pipeline` 이 idle 을
        # 통과시켜요).
        return replace(
            pipeline,
            status="idle",
            reason="no_traffic_in_range",
            traffic_count=0,
            traffic_observation_status="ok",
            traffic_observation_reason=None,
        )
    # 2026-08-24 로컬 실AWS 실측: 계기 상태가 `unknown` 인 agent 호출 1건만으로
    # `ingest_stalled` 이 떴는데, `aws/spans` 에는 그 시각 이후 span 이 0건이었고
    # 이어서 계기 있는 agent 를 부르자 span 이 정상 도착했어요. 즉 파이프라인은
    # 살아 있었고 **span 이 생길 리 없는 트래픽**을 적재 실패로 오진한 거예요.
    # 그래서 span 을 만들 수 있는 agent(계기 enabled)의 호출이 하나라도 있을 때만
    # 적재 정지를 단정해요. 아니면 fail-closed 미관측이에요 — 값은 계속 가리지만
    # "적재가 멈췄다"고는 말하지 않아요.
    instrumented_traffic = None
    if instrumentation_of is not None and observation.agent_ids:
        instrumented_traffic = any(
            instrumentation_of(agent_id) == "enabled"
            for agent_id in observation.agent_ids
        )
    if instrumented_traffic is False:
        return replace(
            pipeline,
            status="unknown",
            reason="traffic_unobserved",
            traffic_count=observation.invocation_count,
            traffic_observation_status="ok",
            traffic_observation_reason="uninstrumented_traffic_only",
        )
    return replace(
        pipeline,
        status="stale",
        reason="ingest_stalled",
        traffic_count=observation.invocation_count,
        traffic_observation_status="ok",
        traffic_observation_reason=None,
    )


def _metrics(job, aggregate) -> AgentMetrics:
    aggregate_reason = aggregate.reason or "no_data_in_range"
    instrumentation_reason = (
        "no_instrumentation"
        if _instrumentation(job) == "not_configured"
        else aggregate_reason
    )
    span_sampling_rate = (
        None
        if instrumentation_reason == "no_instrumentation"
        else aggregate.sampling_rate
    )
    if aggregate.status == "ok":
        def observed(value, source="aggregate"):
            return ObservedMetric(
                value=value,
                status="ok",
                as_of=aggregate.as_of,
                source=source,
                sampling_rate=aggregate.sampling_rate,
                window_start=aggregate.window_start,
                window_end=aggregate.window_end,
            )
        invocations = observed(aggregate.invocations)
        if instrumentation_reason == "no_instrumentation":
            tokens = _unobserved(
                source="span",
                as_of=None,
                reason="no_instrumentation",
            )
            latency = _unobserved(
                source="span",
                as_of=None,
                reason="no_instrumentation",
            )
        else:
            tokens = observed(
                aggregate.input_tokens + aggregate.output_tokens,
                "span",
            )
            latency = (
                observed(aggregate.p95_latency_ms, "span")
                if aggregate.p95_latency_ms is not None
                else _unobserved(
                    source="span",
                    as_of=aggregate.as_of,
                    reason=aggregate.latency_reason or "no_data_in_range",
                    sampling_rate=aggregate.sampling_rate,
                    window_start=aggregate.window_start,
                    window_end=aggregate.window_end,
                )
            )
        error_rate = (
            observed(aggregate.error_rate)
            if aggregate.error_rate is not None
            else _unobserved(
                source="aggregate",
                as_of=aggregate.as_of,
                reason="no_data_in_range",
                sampling_rate=aggregate.sampling_rate,
                window_start=aggregate.window_start,
                window_end=aggregate.window_end,
            )
        )
    else:
        invocations = _unobserved(
            source="aggregate",
            as_of=aggregate.as_of,
            reason=aggregate_reason,
            window_start=aggregate.window_start,
            window_end=aggregate.window_end,
        )
        tokens = _unobserved(
            source="span",
            as_of=None,
            reason=instrumentation_reason,
            sampling_rate=span_sampling_rate,
            window_start=aggregate.window_start,
            window_end=aggregate.window_end,
        )
        latency = _unobserved(
            source="span",
            as_of=None,
            reason=instrumentation_reason,
            sampling_rate=span_sampling_rate,
            window_start=aggregate.window_start,
            window_end=aggregate.window_end,
        )
        error_rate = _unobserved(
            source="aggregate",
            as_of=aggregate.as_of,
            reason=aggregate_reason,
            window_start=aggregate.window_start,
            window_end=aggregate.window_end,
        )
    return AgentMetrics(
        invocations=invocations,
        tokens=tokens,
        estimated_cost=_unobserved(
            source="aggregate",
            as_of=None,
            reason="cost_contract_unavailable",
        ),
        p95_latency=latency,
        error_rate=error_rate,
    )


def _agent(record, job, governance, aggregate, pipeline) -> AgentMonitoring:
    return AgentMonitoring(
        record_id=str(record.record_id),
        name=str(record.name),
        version=str(record.version),
        owner=str(getattr(record, "owner_user", "") or ""),
        owner_team=str(getattr(record, "owner_team", "") or ""),
        registry_status=_value(record.status),
        deployment=DeploymentState(
            phase=_value(job.phase) if job else None,
            as_of=_timestamp(job, record) if job else None,
        ),
        reconciliation=_reconciliation(record, job),
        instrumentation=_instrumentation(job),
        metrics=_metrics(job, aggregate),
        pipeline=PipelineHealth(**pipeline.__dict__),
        governance=GovernanceState(**governance),
    )


def _summary(
    items: list[AgentMonitoring],
    *,
    population_complete: bool,
) -> FleetSummary:
    timestamps = [agent.deployment.as_of for agent in items]
    complete = (
        population_complete
        and bool(items)
        and all(timestamp is not None for timestamp in timestamps)
    )
    watermark = min(
        (timestamp for timestamp in timestamps if timestamp is not None),
        default=None,
    )

    def observed(value: int) -> ObservedMetric:
        return ObservedMetric(
            value=value,
            status="ok",
            as_of=watermark,
            source="ledger",
            sampling_rate=None,
        )

    def ledger_count(value: int):
        return (
            observed(value)
            if complete
            else _unobserved(
                source="ledger",
                as_of=None,
                reason="population_incomplete",
            )
        )

    return FleetSummary(
        coherence_anomalies=SummaryTile(
            primary=ledger_count(
                sum(
                    agent.reconciliation.verdict == "diverged"
                    for agent in items
                )
            ),
            secondary=ledger_count(
                sum(
                    agent.reconciliation.verdict == "unknown"
                    for agent in items
                )
            ),
        ),
        authorization_denials=SummaryTile(
            primary=_unobserved(
                source="log",
                as_of=None,
                reason="no_aggregate",
            ),
        ),
        cost=SummaryTile(
            primary=_unobserved(
                source="aggregate",
                as_of=None,
                reason="cost_contract_unavailable",
            ),
        ),
        errors=SummaryTile(
            primary=_unobserved(
                source="aggregate",
                as_of=None,
                reason="no_aggregate",
            ),
        ),
        observability_health=SummaryTile(
            primary=ledger_count(
                sum(
                    agent.instrumentation == "not_configured"
                    for agent in items
                )
            ),
            secondary=ledger_count(
                sum(agent.instrumentation == "unknown" for agent in items)
            ),
        ),
    )


@router.get(
    "/api/admin/agents/monitoring",
    response_model=AgentMonitoringFleet,
    dependencies=[Depends(_require_monitoring_admin)],
)
def list_agent_monitoring() -> AgentMonitoringFleet:
    page = get_registry().list_agent_monitoring_records(get_registry_id())
    records = list(page.records)
    jobs = get_deploy_service().batch_agent_monitoring_jobs(
        [record.record_id for record in records]
    )
    governance = get_governance_monitoring_snapshots(records)
    reader = get_monitoring_aggregate_reader()
    aggregates = reader.batch_get([record.record_id for record in records])
    pipeline = _resolve_pipeline_lag(
        reader.pipeline_health(),
        # 배포 원장의 계기 상태로만 판단해요(집계 자기 보고가 아니에요).
        instrumentation_of=_instrumentation_lookup(jobs),
    )
    agents = [
        _agent(
            record,
            jobs.get(record.record_id),
            governance[record.record_id],
            _aggregate_for_pipeline(aggregates[record.record_id], pipeline),
            pipeline,
        )
        for record in records
    ]
    return AgentMonitoringFleet(
        summary=_summary(agents, population_complete=not page.truncated),
        agents=agents,
        pipeline=PipelineHealth(**pipeline.__dict__),
        observed_population=page.observed_population,
        truncated=page.truncated,
    )


@router.get(
    "/api/admin/agents/{record_id}/monitoring",
    response_model=AgentMonitoring,
    dependencies=[Depends(_require_monitoring_admin)],
)
def get_agent_monitoring(record_id: str) -> AgentMonitoring:
    try:
        record = get_registry().get_agent_monitoring_record(
            get_registry_id(),
            record_id,
        )
    except RecordNotFound:
        raise HTTPException(404, "Agent를 찾을 수 없어요.")
    governance = get_governance_monitoring_snapshots([record])
    job = get_deploy_service().get_agent_monitoring_job(record_id)
    reader = get_monitoring_aggregate_reader()
    aggregate = reader.batch_get([record_id])[record_id]
    pipeline = _resolve_pipeline_lag(
        reader.pipeline_health(),
        instrumentation_of=_instrumentation_lookup({record_id: job}),
    )
    return _agent(
        record,
        job,
        governance[record_id],
        _aggregate_for_pipeline(aggregate, pipeline),
        pipeline,
    )

@router.get(
    "/api/admin/monitoring/retention-policy",
    dependencies=[Depends(_require_monitoring_admin)],
)
def retention_policy() -> dict:
    """원본 telemetry 보존 경계 — 화면의 '원본 보기' 활성 여부 판단에 써요(MO-14).

    보존 상수는 `retention.py` 단일 출처예요. 경계 이전은 CloudWatch, 경계부터는 archive 라서
    화면이 '복원 필요' 안내로 바꿔요.
    """
    from .retention import (
        RAW_TELEMETRY_RETENTION_DAYS,
        raw_telemetry_availability,
    )

    return {
        "raw_telemetry_retention_days": RAW_TELEMETRY_RETENTION_DAYS,
        "policy": {
            "boundary": "observed_at_plus_retention",
            "before_boundary": "cloudwatch_retention_window",
            "at_or_after_boundary": "archive_restore_window",
        },
        # This endpoint does not read the infra coverage ledger. Time alone is
        # not evidence that either copy exists.
        "observed_availability": raw_telemetry_availability(),
    }
