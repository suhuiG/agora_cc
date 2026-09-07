"""advance_agent() — poll-driven AgentCore Runtime 배포 상태 머신.

QUEUED → BUILDING → DEPLOYING → PROVISIONING → VERIFYING → READY (또는 FAILED).
서버 stateless — 매 poll마다 job + 실제 AWS 상태로 재구성해 한 단계 전진.
실패 시 생성된 AgentCore Runtime을 delete_runtime으로 보상.

MCP 경로(jobs.py)와 완전히 분리 — Gateway·target 개념 없음.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from ....shared.memory import (
    UnsupportedMemoryStrategyError,
    require_supported_memory_strategies,
)
from .models import (
    BuildArtifact,
    ConcurrentJobAdvance,
    DeployJob,
    DeployPhase,
    ProvisioningCheckpointError,
    terminal,
)
from .port import IdentityOutboundProvisioningError

_log = logging.getLogger(__name__)

# AgentCore Memory 는 `CreateMemory` 응답 직후에도 한동안 `CREATING` 이에요. 전략이 붙은
# Memory 는 특히 오래 걸려요 — 실측(2026-08-21, dev/ap-northeast-2, SEMANTIC 1개):
# 생성 후 **65초 뒤에도 `CREATING`**(`DeleteMemory` 가 transitional state 로 거부),
# 약 3분 뒤 `ACTIVE`. `updatedAt` 은 상태 전이를 반영하지 않아서 지표로 못 써요.
# 내장 도구와 같은 20회(폴 간격 3초 ≈ 60초) 상한으로는 정상 배포가 timeout 돼요
# (job agent-client-43832f686471dce6bd257c5754108b22). 여유를 두고 6분치로 잡아요.
_MEMORY_READY_MAX_ATTEMPTS = 120
# Deployer 한 번은 최대 5회 관측과 4번의 2초 sleep(약 8초), job poll은 기본
# 3초예요. 36회면 약 6분 36초로, dev에서 Memory ACTIVE까지 약 3분 걸린 실측에
# 2배 여유를 둔 IH-61의 6분 상한과 같은 규모예요.
_POLICY_READY_MAX_ATTEMPTS = 36
# 내장 도구(CUSTOM Browser·Code Interpreter)는 생성 직후 곧 READY 가 돼요. 폴 간격 3초에
# 20회 ≈ 60초 상한이에요. 이름 없는 리터럴이던 것을 상수로 올렸어요 — 진행 표시(IH-77)가
# 같은 값을 보여줘야 화면과 동작이 어긋나지 않아요.
_BUILTIN_READY_MAX_ATTEMPTS = 20


def wait_detail(job: DeployJob) -> str | None:
    """지금 무엇을 기다리는지 사람이 읽을 문장. 기다리는 게 없으면 None.

    IH-77: '나의 요청' 화면이 phase 이름만 보여주면 6분 걸리는 배포를 장애로 오인해요
    (실측 2026-08-21). 대기 카운터는 job 이 재진입할 때마다 오르니 그 값이 곧 관측 횟수예요.
    상한을 함께 보여줘 남은 여유를 알 수 있게 해요. 상한은 프로덕션 상수를 그대로 읽어요 —
    화면에 리터럴을 따로 적으면 동작과 어긋나요.
    """
    waits = (
        ("내장 도구 준비", job.builtin_poll_attempts, _BUILTIN_READY_MAX_ATTEMPTS),
        ("Memory 활성화", job.memory_poll_attempts, _MEMORY_READY_MAX_ATTEMPTS),
        ("Cedar 정책 활성화", job.policy_poll_attempts, _POLICY_READY_MAX_ATTEMPTS),
    )
    active = [(label, n, cap) for label, n, cap in waits if n]
    if not active:
        return None
    label, seen, cap = max(active, key=lambda item: item[1])
    return f"{label} 대기 중 ({seen}/{cap} 관측)"

_ROLE_PROPAGATION_SECONDS = 15
_ROLE_PROPAGATION_DEADLINE_SECONDS = 60


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _role_propagation_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return (
        "role" in text
        and any(
            marker in text
            for marker in (
                "cannot be assumed",
                "not found",
                "does not exist",
                "is invalid",
                "not authorized to perform sts:assumerole",
            )
        )
    )


def _fail(
    job: DeployJob,
    phase_name: str,
    message: str,
    port,
    now,
    owner_id: str,
    compensate_record=None,
) -> DeployJob:
    """FAILED 전이를 준비하고 파괴적 보상은 서비스의 CAS 뒤로 미뤄요."""
    owns_resource = job.resource_owner_id == owner_id
    owns_provisioning = (
        job.provisioning_owner_id == owner_id
        or (job.provisioning_owner_id is None and owns_resource)
    )
    job.phase = DeployPhase.FAILED
    job.error = {"phase": phase_name, "message": message}
    job.updated_at = now()
    if (
        (
            job.runtime_id
            and not job.reuses_runtime
            and not job.record_bound
            and not owns_resource
        )
        or (
            (
                job.record_id
                or (not job.is_redeploy and job.oauth_client_id)
            )
            and not owns_provisioning
        )
    ):
        skipped = "resource ownership not proven"
        job.error.setdefault("compensation_skipped", []).append(skipped)
        _log.error(
            "agent deploy compensation skipped: %s; job_id=%s "
            "resource_owner_id=%s current_owner_id=%s",
            skipped,
            job.job_id,
            job.resource_owner_id,
            owner_id,
        )
    return job


def compensate_failure(
    job: DeployJob,
    port,
    *,
    owner_id: str,
    compensate_record=None,
) -> bool:
    """FAILED claim 저장 뒤 이 실행자가 소유한 Agent 산출물만 되돌려요."""
    if job.phase is not DeployPhase.FAILED:
        return False
    compensation_errors: list[str] = []
    attempted = False
    owns_resource = job.resource_owner_id == owner_id
    owns_provisioning = (
        job.provisioning_owner_id == owner_id
        or (job.provisioning_owner_id is None and owns_resource)
    )
    resources = job.builtin_resources or {}
    owners = job.builtin_owner_ids or {}
    for kind, resource in tuple(resources.items()):
        if owners.get(kind) != owner_id:
            compensation_errors.append(
                f"{kind}: resource ownership not proven"
            )
            continue
        attempted = True
        try:
            port.delete_builtin_tool(kind, resource["id"])
            resources.pop(kind, None)
            owners.pop(kind, None)
        except Exception as exc:
            compensation_errors.append(
                f"{kind}: {type(exc).__name__}: {exc}"
            )
    if job.memory_id and job.memory_owner_id == owner_id:
        attempted = True
        try:
            port.delete_memory(job.memory_id)
            job.memory_id = None
            job.memory_owner_id = None
        except Exception as exc:
            compensation_errors.append(
                f"memory: {type(exc).__name__}: {exc}"
            )
    elif job.memory_id and job.memory_owner_id:
        compensation_errors.append("memory: resource ownership not proven")
    if (
        owns_provisioning
        and compensate_record is not None
        and (
            job.record_id
            or (not job.is_redeploy and job.oauth_client_id)
        )
    ):
        attempted = True
        try:
            compensation_errors.extend(compensate_record(job) or ())
        except Exception as exc:
            compensation_errors.append(
                f"provisioned artifacts: {type(exc).__name__}: {exc}"
            )
    identity_cleanup_allowed = not job.runtime_id
    if (
        job.runtime_id
        and not job.reuses_runtime
        and not job.record_bound
        and owns_resource
    ):
        attempted = True
        try:
            port.delete_runtime(job.runtime_id)
            identity_cleanup_allowed = True
        except Exception as exc:
            compensation_errors.append(
                f"runtime: {type(exc).__name__}: {exc}"
            )
    identity_resources = (
        (
            "workload identity",
            job.workload_identity_name,
            job.workload_identity_created,
            job.workload_identity_owner_id,
            port.delete_workload_identity,
        ),
        (
            "oauth provider",
            job.oauth_provider_name,
            job.oauth_provider_created,
            job.oauth_provider_owner_id,
            port.delete_oauth2_credential_provider,
        ),
    )
    for label, name, created, identity_owner_id, delete in identity_resources:
        if not name or not created:
            continue
        if not identity_cleanup_allowed:
            compensation_errors.append(
                f"{label}: runtime deletion not confirmed"
            )
            continue
        if identity_owner_id != owner_id:
            compensation_errors.append(
                f"{label}: resource ownership not proven"
            )
            continue
        attempted = True
        try:
            delete(name)
            if label == "workload identity":
                job.workload_identity_name = None
                job.workload_identity_created = False
                job.workload_identity_owner_id = None
            else:
                job.oauth_provider_name = None
                job.oauth_provider_created = False
                job.oauth_provider_owner_id = None
        except Exception as exc:
            compensation_errors.append(
                f"{label}: {type(exc).__name__}: {exc}"
            )
    if job.execution_role_name and job.execution_role_owner_id == owner_id:
        if not identity_cleanup_allowed:
            compensation_errors.append(
                "execution role: runtime deletion not confirmed"
            )
        else:
            attempted = True
            try:
                port.delete_agent_execution_role(
                    job.execution_role_name,
                    shared_policy_arn=(
                        job.execution_role_shared_policy_arn or ""
                    ),
                )
                job.execution_role_name = None
                job.execution_role_arn = None
                job.execution_role_owner_id = None
            except Exception as exc:
                compensation_errors.append(
                    f"execution role: {type(exc).__name__}: {exc}"
                )
    if compensation_errors:
        job.error["compensation_errors"] = compensation_errors
        _log.error(
            "agent deploy compensation incomplete: job_id=%s errors=%s",
            job.job_id,
            compensation_errors,
        )
    return attempted


def advance_agent(job: DeployJob, port, *, exec_role_arn: str, cognito: dict,
                  register, now, verify=None, compensate_record=None,
                  finalize=None, checkpoint_role=None, owner_id: str,
                  fail_closed_on_unknown_authorization: bool = False,
                  fail_closed_on_unknown_builtin_tools: bool = False,
                  builtin_config: dict | None = None,
                  agent_role_config: dict | None = None) -> DeployJob:
    """agent 경로 상태 머신. poll마다 한 단계 전진.

    verify: job → VerifyReport. VERIFYING 단계에서 도구 동작을 확인해요.
      None이면 검증을 건너뛰고 바로 등재해요(미배선 환경 하위호환).
    """
    if terminal(job.phase):
        return job

    # ── QUEUED: 빌드 시작 + Memory 생성 착수 ─────────────────────────────
    if job.phase is DeployPhase.QUEUED:
        if job.memory_config and job.memory_config.get("mode") == "MANAGED":
            try:
                require_supported_memory_strategies(
                    job.memory_config.get("strategies")
                )
            except UnsupportedMemoryStrategyError as exc:
                return _fail(
                    job,
                    "queued",
                    str(exc),
                    port,
                    now,
                    owner_id,
                    compensate_record,
                )
        job.build_id = port.start_build(
            job.job_id, job.source_ref, job.build_type, job.asset_type)
        # IH-82: Memory 생성을 여기서 시작해요. AgentCore Memory 는 ACTIVE 까지 수 분이
        # 걸리는데(실측 약 2~3분), 그 대기를 빌드(실측 29~68초)·runtime 생성(19초)·
        # Cedar 활성화(8.5~72.6초)와 **겹치게** 하려면 가장 이른 시점에 착수해야 해요.
        # 생성 호출 자체는 즉시 반환되고(CREATING), ACTIVE 대기는 VERIFYING 앞에서 해요.
        # 소유권은 만든 직후 원장에 못박아요 — 이 poll 의 store 쓰기에 build_id 와 함께
        # 실려요(ADR-0049 의 체크포인트 원칙).
        if job.memory_config and job.memory_config.get("mode") == "MANAGED":
            name = job.meta.get("name") or job.job_id
            try:
                memory = port.ensure_agent_memory(
                    name,
                    job.memory_config,
                    client_token_seed=job.job_id,
                )
            except Exception as exc:
                return _fail(
                    job, "queued",
                    f"AgentCore Memory 생성 실패: {exc}",
                    port, now, owner_id, compensate_record,
                )
            job.memory_id = job.memory_id or memory.memory_id
            if memory.created:
                job.memory_owner_id = owner_id
        job.phase = DeployPhase.BUILDING
        job.updated_at = now()
        return job

    # ── BUILDING: 빌드 완료 대기 → create_agent_runtime ─────────────────
    if job.phase is DeployPhase.BUILDING:
        bs = port.get_build_status(job.build_id)
        if bs.state == "IN_PROGRESS":
            return job
        if bs.state == "FAILED":
            return _fail(
                job, "building", bs.reason or "build failed",
                port, now, owner_id,
            )
        # SUCCEEDED
        artifact = bs.artifact or BuildArtifact(build_type=job.build_type, uri="")
        job.artifact_uri = artifact.uri
        job.otel_instrumentation_status = (
            "enabled" if artifact.otel_instrumented else "not_configured"
        )
        name = job.meta.get("name") or job.job_id
        try:
            role_config = agent_role_config or {}
            runtime_role_arn = exec_role_arn
            agent_key = job.redeploy_record_id or job.job_id
            if role_config.get("enabled", True):
                stage = str(role_config.get("stage") or "")
                shared_policy_arn = str(
                    role_config.get("shared_policy_arn") or ""
                )
                boundary_arn = str(
                    role_config.get("permissions_boundary_arn") or ""
                )
                if not job.execution_role_name:
                    def checkpoint_created_role(deployed_role):
                        job.execution_role_name = deployed_role.role_name
                        job.execution_role_arn = deployed_role.role_arn
                        job.execution_role_shared_policy_arn = (
                            shared_policy_arn
                        )
                        job.execution_role_owner_id = owner_id
                        observed = _time(now())
                        job.execution_role_ready_after = _iso(
                            observed
                            + timedelta(seconds=_ROLE_PROPAGATION_SECONDS)
                        )
                        job.execution_role_deadline = _iso(
                            observed
                            + timedelta(
                                seconds=_ROLE_PROPAGATION_DEADLINE_SECONDS
                            )
                        )
                        job.execution_role_last_status = "created"
                        job.updated_at = _iso(observed)
                        if checkpoint_role is not None:
                            checkpoint_role(job)

                    deployed_role = port.ensure_agent_execution_role(
                        agent_key,
                        name,
                        stage,
                        shared_policy_arn=shared_policy_arn,
                        permissions_boundary_arn=boundary_arn,
                        provider_name=str(
                            cognito.get("provider_name") or ""
                        ),
                        existing_role_arn=job.execution_role_arn or "",
                        on_created=checkpoint_created_role,
                    )
                    job.execution_role_name = deployed_role.role_name
                    job.execution_role_arn = deployed_role.role_arn
                    job.execution_role_shared_policy_arn = shared_policy_arn
                    if deployed_role.created:
                        return job
                try:
                    runtime_role_arn = port.get_agent_execution_role(
                        job.execution_role_name
                    )
                    job.execution_role_last_status = "get-role=ready"
                except Exception as exc:
                    job.execution_role_last_status = (
                        f"get-role={type(exc).__name__}: {exc}"
                    )
                    if (
                        job.execution_role_deadline
                        and _time(now()) >= _time(job.execution_role_deadline)
                    ):
                        raise RuntimeError(
                            "agent execution role propagation deadline "
                            f"exceeded ({job.execution_role_last_status})"
                        ) from exc
                    job.updated_at = now()
                    return job
                if (
                    job.execution_role_ready_after
                    and _time(now()) < _time(job.execution_role_ready_after)
                ):
                    job.updated_at = now()
                    return job
            else:
                job.execution_role_arn = None
            if job.builtin_tools:
                config = builtin_config or {}
                role_arn = str(config.get("execution_role_arn") or "")
                stage = str(config.get("stage") or "")
                bucket = str(config.get("recording_bucket") or "")
                if not role_arn or not stage:
                    raise RuntimeError(
                        "AgentCore built-in tool infrastructure is not configured"
                    )
                resources = job.builtin_resources or {}
                owners = job.builtin_owner_ids or {}
                created_now = False
                for kind in job.builtin_tools:
                    if kind in resources:
                        continue
                    deployed = port.ensure_builtin_tool(
                        kind,
                        name,
                        agent_key=agent_key,
                        stage=stage,
                        execution_role_arn=role_arn,
                        recording_bucket=bucket,
                    )
                    resources[kind] = {
                        "id": deployed.resource_id,
                        "arn": deployed.resource_arn,
                        "status": deployed.status,
                        "network_mode": deployed.network_mode,
                        **(
                            {"recording_prefix": f"{agent_key}/browser/"}
                            if kind == "browser"
                            else {}
                        ),
                    }
                    if deployed.created:
                        owners[kind] = owner_id
                    # A later tool can fail in this same poll. Keep every
                    # successful create on the FAILED job for compensation.
                    job.builtin_resources = resources
                    job.builtin_owner_ids = owners
                    created_now = True
                job.builtin_resources = resources
                job.builtin_owner_ids = owners
                if created_now:
                    job.updated_at = now()
                    return job
                configured_now = False
                for kind, resource in resources.items():
                    if (
                        resource.get("logs_configured") is True
                        or resource.get("logsConfigured") is True
                    ):
                        continue
                    port.ensure_builtin_observability(
                        kind,
                        resource["id"],
                        resource["arn"],
                        stage=stage,
                    )
                    resource["logs_configured"] = True
                    configured_now = True
                if configured_now:
                    job.updated_at = now()
                    return job
                pending = False
                for kind, resource in resources.items():
                    observed = port.get_builtin_tool_status(
                        kind, resource["id"]
                    )
                    resource["status"] = observed.state
                    if observed.state == "CREATING":
                        pending = True
                    elif observed.state != "READY":
                        raise RuntimeError(
                            f"{kind} status={observed.state}: "
                            f"{observed.reason or 'unknown'}"
                        )
                if pending:
                    job.builtin_poll_attempts += 1
                    if job.builtin_poll_attempts >= _BUILTIN_READY_MAX_ATTEMPTS:
                        raise RuntimeError(
                            "built-in tool readiness is unknown after timeout"
                        )
                    job.updated_at = now()
                    return job
            if (
                job.memory_config
                and job.memory_config.get("mode") == "MANAGED"
            ):
                memory = port.ensure_agent_memory(
                    name,
                    job.memory_config,
                    client_token_seed=job.job_id,
                    agent_key=agent_key,
                    stage=str((builtin_config or {}).get("stage") or ""),
                )
                if job.memory_id and job.memory_id != memory.memory_id:
                    raise RuntimeError(
                        "기존 AgentCore Memory ID가 배포 대상과 일치하지 않아요."
                    )
                job.memory_id = job.memory_id or memory.memory_id
                if memory.created:
                    job.memory_owner_id = owner_id
                # IH-82: ACTIVE 대기는 **여기서 하지 않아요.** runtime 은 생성 시점에
                # Memory ACTIVE 를 요구하지 않고(`AGORA_MEMORY_ID` env 로만 받고 실제
                # 사용은 요청 처리 때예요), 여기서 막으면 Memory 활성화가 runtime 생성·
                # Cedar 활성화와 겹치지 못해 배포가 그만큼 길어져요(실측 412초 중 161초).
                # IH-61 이 이 대기를 도입한 이유는 **검증**이 Memory 를 필요로 해서였으니,
                # 대기는 VERIFYING 앞으로 옮겼어요. FAILED 는 여기서도 즉시 실패예요 —
                # 기다릴 이유가 없어요.
                memory_status = port.get_memory_status(job.memory_id)
                if memory_status.state == "FAILED":
                    raise RuntimeError(
                        "AgentCore Memory provisioning failed: "
                        f"{memory_status.reason or 'unknown'}"
                    )
            runtime_cognito = {
                **cognito,
                "builtin_tool_ids": {
                    kind: resource["id"]
                    for kind, resource in (job.builtin_resources or {}).items()
                },
                "memory_id": (
                    job.memory_id or ""
                    if job.memory_config
                    and job.memory_config.get("mode") == "MANAGED"
                    else ""
                ),
            }
            if job.reuses_runtime:
                # 재배포: 같은 runtime을 새 artifact로 갱신해요(이름·ARN·endpoint 유지).
                # 실패해도 기존 runtime은 그대로라 사용자는 옛 버전을 계속 쓸 수 있어요.
                deployment = port.update_agent_runtime(
                    job.redeploy_runtime_id, artifact,
                    exec_role_arn=runtime_role_arn,
                    cognito=runtime_cognito,
                    name=name,
                )
            else:
                deployment = port.create_agent_runtime(
                    name, artifact,
                    exec_role_arn=runtime_role_arn,
                    cognito=runtime_cognito,
                )
            job.runtime_id, job.runtime_arn = deployment
            job.workload_id = getattr(deployment, "workload_id", "") or None
            job.workload_public_key = (
                getattr(deployment, "workload_public_key", "") or None
            )
            _capture_identity_outbound(job, deployment, owner_id)
        except Exception as e:
            if isinstance(e, IdentityOutboundProvisioningError):
                _capture_identity_outbound(job, e.deployment, owner_id)
            if (
                (agent_role_config or {}).get("enabled", True)
                and job.execution_role_deadline
                and _role_propagation_error(e)
            ):
                job.execution_role_last_status = (
                    f"create-runtime={type(e).__name__}: {e}"
                )
                if _time(now()) < _time(job.execution_role_deadline):
                    job.updated_at = now()
                    return job
                e = RuntimeError(
                    "agent execution role propagation deadline exceeded "
                    f"({job.execution_role_last_status})"
                )
            verb = "갱신" if job.is_redeploy else "생성"
            return _fail(
                job, "building", f"AgentCore Runtime {verb} 실패: {e}",
                port, now, owner_id,
            )
        if not job.reuses_runtime and deployment.created:
            job.resource_owner_id = owner_id
        job.phase = DeployPhase.DEPLOYING
        job.updated_at = now()
        return job

    # ── DEPLOYING: runtime CREATING→READY 폴링 ──────────────────────────
    if job.phase is DeployPhase.DEPLOYING:
        rs = port.get_runtime_status(job.runtime_id)
        if rs.state == "CREATING":
            return job
        if rs.state == "CREATE_FAILED":
            return _fail(
                job, "deploying", rs.reason or "runtime create failed",
                port, now, owner_id,
            )
        # runtime이 떴다고 도구가 붙은 건 아니에요 — 검증을 거쳐야 READY(등재)로 가요.
        job.phase = DeployPhase.PROVISIONING
        job.updated_at = now()
        return job

    # ── PROVISIONING: Registry·identity·ReadOnly baseline 준비 ─────────
    if job.phase is DeployPhase.PROVISIONING:
        try:
            job.record_id = register(job)
            stage = str((agent_role_config or {}).get("stage") or "")
            if job.execution_role_name:
                port.retag_agent_execution_role(
                    job.execution_role_name,
                    record_id=job.record_id,
                    stage=stage,
                )
            if job.memory_id:
                port.tag_agent_memory(
                    job.memory_id,
                    record_id=job.record_id,
                    stage=stage,
                )
            if job.builtin_resources:
                stage = str((builtin_config or {}).get("stage") or "")
                for resource in job.builtin_resources.values():
                    port.tag_builtin_tool(
                        resource["arn"],
                        record_id=job.record_id,
                        stage=stage,
                        network_mode=str(resource.get("network_mode") or ""),
                    )
        except (ConcurrentJobAdvance, ProvisioningCheckpointError):
            raise
        except Exception as e:
            return _fail(
                job,
                "provisioning",
                f"Agent 프로비저닝 실패: {e}",
                port,
                now,
                owner_id,
                compensate_record,
            )
        if (
            job.provisioned_tool_bindings
            or job.provisioned_policy_revision is not None
        ):
            # register()가 이번 실행이 만든 정확한 binding/revision을 기록한 뒤에만
            # 보상 소유를 주장해요. 아래 phase CAS가 져서 이 표식이 저장되지 않으면
            # stale 실행자는 그 산출물을 삭제할 수 없어요.
            job.provisioning_owner_id = owner_id
        if job.provisioning_policy_outcome == "DEPLOY_FAILED":
            # IH-68: 이유 없이 "실패했어요" 만 남기면 진단이 불가능해요. 원장의 policy
            # deployment 레코드에 있던 findings 는 이어지는 보상이 지워버리니(실측
            # 2026-08-21), 여기서 메시지에 붙여 job.error 에 보존해요.
            findings = " / ".join(job.provisioning_policy_findings)
            _log.error(
                "Cedar policy 배포 실패; job_id=%s findings=%s",
                job.job_id,
                findings or "(원인 미기록)",
            )
            return _fail(
                job,
                "provisioning",
                (
                    f"Cedar policy 배포에 실패했어요: {findings}"
                    if findings
                    else "Cedar policy 배포에 실패했어요. (원인이 기록되지 않았어요)"
                ),
                port,
                now,
                owner_id,
                compensate_record,
            )
        if job.provisioning_policy_outcome == "DEPLOY_IN_PROGRESS":
            job.policy_poll_attempts += 1
            if job.policy_poll_attempts >= _POLICY_READY_MAX_ATTEMPTS:
                status = next(
                    (
                        finding.removeprefix("status=")
                        for finding in job.provisioning_policy_findings
                        if finding.startswith("status=")
                    ),
                    "UNKNOWN",
                )
                return _fail(
                    job,
                    "provisioning",
                    f"Cedar policy가 준비되지 않았어요(상태={status})",
                    port,
                    now,
                    owner_id,
                    compensate_record,
                )
            job.updated_at = now()
            return job
        if (
            fail_closed_on_unknown_authorization
            and job.authorization_verdict == "unknown"
        ):
            return _fail(
                job,
                "provisioning",
                "Agent authorization 상태를 확인할 수 없어요.",
                port,
                now,
                owner_id,
                compensate_record,
            )
        job.phase = DeployPhase.VERIFYING
        job.updated_at = now()
        return job

    # ── VERIFYING: 선택한 도구가 실제로 동작하는지 검증 ─────────────────
    if job.phase is DeployPhase.VERIFYING:
        # IH-61 의 Memory ACTIVE 대기가 여기 있어요(IH-82 로 BUILDING 에서 이동).
        # 검증은 배포된 agent 를 실제로 호출하고, agent 는 session manager 로 Memory 를
        # 쓰기 때문에 **검증 직전에는 ACTIVE 여야 해요.** 전이 상태는 실패가 아니라
        # "아직 아님" 이라 phase 를 유지하고 재진입해요(ADR-0049).
        if job.memory_id and job.memory_config and (
            job.memory_config.get("mode") == "MANAGED"
        ):
            memory_status = port.get_memory_status(job.memory_id)
            if memory_status.state == "FAILED":
                return _fail(
                    job, "verifying",
                    "AgentCore Memory provisioning failed: "
                    f"{memory_status.reason or 'unknown'}",
                    port, now, owner_id, compensate_record,
                )
            if memory_status.state != "ACTIVE":
                job.memory_poll_attempts += 1
                if job.memory_poll_attempts >= _MEMORY_READY_MAX_ATTEMPTS:
                    return _fail(
                        job, "verifying",
                        "AgentCore Memory가 준비되지 않았어요"
                        f"(상태={memory_status.state or 'unknown'})",
                        port, now, owner_id, compensate_record,
                    )
                job.updated_at = now()
                return job
        if verify is None:
            # 검증기 미배선(테스트·구환경) — 기존 동작대로 READY 처리해요.
            return _finalize_and_finish(
                job, port, now, owner_id, finalize, compensate_record
            )
        try:
            report = verify(job)
        except Exception as e:
            return _fail(
                job,
                "verifying",
                f"검증 실행 실패: {type(e).__name__}: {e}",
                port,
                now,
                owner_id,
                compensate_record,
            )
        job.verify_report = report.to_dict()
        if not report.ok:
            return _fail(
                job,
                "verifying",
                report.reason or "도구 동작 검증 실패",
                port,
                now,
                owner_id,
                compensate_record,
            )
        if (
            getattr(report, "verdict", "") == "unknown_authorization"
            and fail_closed_on_unknown_authorization
        ):
            warning = "; ".join(getattr(report, "warnings", ())) or (
                "Agent authorization 상태를 확인할 수 없어요."
            )
            return _fail(
                job,
                "verifying",
                warning,
                port,
                now,
                owner_id,
                compensate_record,
            )
        if (
            getattr(report, "unprobed_builtin_tools", ())
            and fail_closed_on_unknown_builtin_tools
        ):
            return _fail(
                job,
                "verifying",
                "내장 도구 도달성을 확인할 수 없어요.",
                port,
                now,
                owner_id,
                compensate_record,
            )
        return _finalize_and_finish(
            job, port, now, owner_id, finalize, compensate_record
        )

    return job


def _capture_identity_outbound(job, deployment, owner_id: str) -> None:
    job.identity_outbound_status = getattr(
        deployment,
        "identity_outbound_status",
        "not_applicable",
    )
    job.identity_outbound_error = (
        getattr(deployment, "identity_outbound_error", "") or None
    )
    job.identity_outbound_warnings = tuple(
        getattr(deployment, "identity_outbound_warnings", ()) or ()
    )
    job.oauth_provider_name = (
        getattr(deployment, "oauth_provider_name", "") or None
    )
    provider_created_now = bool(
        getattr(deployment, "oauth_provider_created", False)
    )
    job.oauth_provider_created = (
        job.oauth_provider_created or provider_created_now
    )
    if provider_created_now:
        job.oauth_provider_owner_id = owner_id
    job.workload_identity_name = (
        getattr(deployment, "workload_identity_name", "") or None
    )
    workload_created_now = bool(
        getattr(deployment, "workload_identity_created", False)
    )
    job.workload_identity_created = (
        job.workload_identity_created or workload_created_now
    )
    if workload_created_now:
        job.workload_identity_owner_id = owner_id


def _finalize_and_finish(
    job: DeployJob, port, now, owner_id, finalize, compensate_record
) -> DeployJob:
    """PROVISIONING과 VERIFYING을 통과한 job만 READY로 확정해요."""
    if finalize is not None:
        try:
            finalize(job)
        except Exception as exc:
            return _fail(
                job,
                "finalizing",
                f"카탈로그 최종 반영 실패: {exc}",
                port,
                now,
                owner_id,
                compensate_record,
            )
    job.phase = DeployPhase.READY
    job.updated_at = now()
    return job
