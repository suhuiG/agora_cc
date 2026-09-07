"""advance() — poll-driven 4-phase 상태 머신 (D1: Lambda tool-provider).

QUEUED → BUILDING → REGISTERING_TARGET → READY (또는 FAILED).
서버 stateless — 매 poll마다 job + 실제 AWS 상태로 재구성해 한 단계 전진.
신규 배포 실패 시 소유 증거를 먼저 확인해 자기 Target만 보상하고 Lambda 좌표는 보존.
재배포는 운영 자산 보호를 위해 teardown하지 않고 고아 가능성만 보고.
"""
from __future__ import annotations

import json
import logging

from ....shared.sensitivity_movement import (
    RedeploySensitivityGuard,
    RedeploySensitivityRequest,
)
from ....shared.slug import gateway_target_name
from .models import BuildArtifact, DeployJob, DeployPhase, terminal
from .target_slices import TargetSplit, split_target_slices

_log = logging.getLogger(__name__)


def _fail(
    job: DeployJob,
    phase_name: str,
    message: str,
    port,
    gateway_id,
    now,
    owner_id: str,
) -> DeployJob:
    job.phase = DeployPhase.FAILED
    job.error = {"phase": phase_name, "message": message}
    job.updated_at = now()
    if job.is_redeploy and job.resource_owner_id is not None:
        skipped = "redeploy resource retained; orphan cleanup may be required"
        job.error["compensation_skipped"] = skipped
        _log.error(
            "MCP redeploy compensation skipped: %s; "
            "job_id=%s resource_owner_id=%s current_owner_id=%s",
            skipped,
            job.job_id,
            job.resource_owner_id,
            owner_id,
        )
    elif (
        job.lambda_arn
        and job.resource_owner_id != owner_id
        and not job.is_redeploy
    ):
        # create 호출 성공만으로는 소유 증거가 아니에요. 같은 job의 다른 실행자가
        # 같은 이름/clientToken으로 얻은 리소스일 수 있어, 영속된 owner 표식 없이는
        # 삭제보다 누수를 보고하는 편이 안전해요(HP-09가 lease/tag를 완성).
        job.error["compensation_skipped"] = "resource ownership not proven"
        _log.error(
            "MCP deploy compensation skipped: resource ownership not proven; "
            "job_id=%s resource_owner_id=%s current_owner_id=%s",
            job.job_id,
            job.resource_owner_id,
            owner_id,
        )
    return job


def compensate_failure(
    job: DeployJob,
    port,
    *,
    gateway_id: str,
    owner_id: str,
) -> bool:
    """FAILED claim과 Lambda owner가 확인된 뒤 자기 Target만 보상 삭제해요."""
    if (
        job.phase is not DeployPhase.FAILED
        or not job.lambda_arn
        or job.is_redeploy
        or job.resource_owner_id != owner_id
    ):
        return False
    try:
        port.verify_lambda_owner(
            job.lambda_arn,
            expected_owner_job_id=job.job_id,
            expected_owner_asset_id=job.source_ref.asset_id,
            expected_owner_principal=job.principal,
        )
    except Exception as exc:
        job.error["compensation_error"] = f"{type(exc).__name__}: {exc}"
        _log.exception(
            "MCP deploy compensation owner check failed: "
            "job_id=%s owner_id=%s",
            job.job_id,
            owner_id,
        )
        return True

    errors: list[str] = []
    if job.gateway_targets:
        targets = [dict(target) for target in job.gateway_targets]
        for target in targets:
            target_id = str(target.get("target_id") or "")
            if not target_id or not target.get("created"):
                continue
            try:
                port.delete_target(gateway_id, target_id)
                target["state"] = "DELETED"
                target["action"] = "COMPENSATED"
            except Exception as exc:
                target["state"] = "FAILED"
                target["error"] = f"{type(exc).__name__}: {exc}"
                errors.append(
                    f"{target.get('gateway_target_name') or target_id}: "
                    f"{type(exc).__name__}: {exc}"
                )
        job.gateway_targets = tuple(targets)
    elif job.target_id:
        errors.append(
            f"target {job.target_id}: retained because target creation "
            "ownership is unproven"
        )

    # DeleteFunction has no RevisionId or owner condition. A matching read can
    # become stale if another job replaces the same name before deletion.
    errors.append(
        "lambda: automatic compensation retained Lambda because "
        "conditional deletion is unavailable"
    )
    if errors:
        job.error["compensation_error"] = "; ".join(errors)
        _log.error(
            "MCP deploy compensation failed: job_id=%s owner_id=%s errors=%s",
            job.job_id,
            owner_id,
            errors,
        )
    return True


def _selected_tools_inline(tools_inline: str, selected_tools: tuple[str, ...]) -> str:
    if not selected_tools:
        return tools_inline
    payload = json.loads(tools_inline)
    tools = payload.get("tools")
    if not isinstance(tools, list):
        raise ValueError("빌드 산출물의 tools 형식이 올바르지 않아요.")
    selected = set(selected_tools)
    payload["tools"] = [
        tool for tool in tools
        if isinstance(tool, dict) and tool.get("name") in selected
    ]
    found = {str(tool.get("name")) for tool in payload["tools"]}
    missing = sorted(selected - found)
    if missing:
        raise ValueError("빌드 산출물에서 선택한 tool을 찾을 수 없어요: " + ", ".join(missing))
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _target_failure(entry: dict, exc: Exception) -> str:
    error = f"{type(exc).__name__}: {exc}"
    entry["state"] = "FAILED"
    entry["error"] = error
    return f"{entry['gateway_target_name']}: {error}"


def _observe_gateway_target_quota(port, gateway_id: str) -> dict:
    sources = {
        "current": "ListGatewayTargets",
        "quota": "ServiceQuotas.GetServiceQuota",
    }

    def _unknown(reason: str) -> dict:
        return {
            "status": "unknown",
            "current": None,
            "quota": None,
            "usage_ratio": None,
            "threshold_ratio": 0.7,
            "threshold_reached": None,
            "reason": reason,
            "sources": sources,
        }

    try:
        observed = port.observe_gateway_target_quota(gateway_id)
    except Exception as exc:
        return _unknown(f"{type(exc).__name__}: {exc}")

    def _number(value) -> bool:
        return (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
        )

    valid = isinstance(observed, dict) and observed.get("sources") == sources
    if valid and observed.get("status") == "ok":
        valid = (
            isinstance(observed.get("current"), int)
            and not isinstance(observed.get("current"), bool)
            and observed["current"] >= 0
            and _number(observed.get("quota"))
            and observed["quota"] > 0
            and _number(observed.get("usage_ratio"))
            and observed["usage_ratio"] >= 0
            and _number(observed.get("threshold_ratio"))
            and 0 < observed["threshold_ratio"] <= 1
            and isinstance(observed.get("threshold_reached"), bool)
        )
    elif valid and observed.get("status") == "unknown":
        valid = (
            observed.get("current") is None
            and observed.get("quota") is None
            and observed.get("usage_ratio") is None
            and _number(observed.get("threshold_ratio"))
            and 0 < observed["threshold_ratio"] <= 1
            and observed.get("threshold_reached") is None
            and isinstance(observed.get("reason"), str)
            and bool(observed["reason"].strip())
        )
    else:
        valid = False
    if not valid:
        return _unknown("Gateway target quota probe returned an invalid result")
    return dict(observed)


def _redeploy_topology_error(
    job: DeployJob,
    plan: TargetSplit,
) -> str | None:
    if not job.is_redeploy:
        return None
    snapshot = job.redeploy_target_snapshot
    if not isinstance(snapshot, dict):
        return (
            "기존 Gateway Target topology 관측 증거가 없어 재배포를 거부해요. "
            "단계 ③ 승인 흐름이 필요해요."
        )
    mode = snapshot.get("mode")
    if mode == "legacy":
        return (
            "legacy Target을 split Target으로 옮기는 재배포는 단계 ②에서 "
            "지원하지 않아요. 단계 ③ 승인 흐름이 필요해요."
        )
    if mode != "split":
        reason = str(snapshot.get("reason") or "기존 topology를 관측할 수 없어요.")
        return f"{reason} 재배포를 거부해요. 단계 ③ 승인 흐름이 필요해요."

    prior_targets = snapshot.get("targets")
    if not isinstance(prior_targets, list):
        return (
            "기존 Gateway Target topology 원장이 올바르지 않아 재배포를 "
            "거부해요. 단계 ③ 승인 흐름이 필요해요."
        )
    prior_names: dict[str, str] = {}
    prior_owner: dict[str, str] = {}
    for target in prior_targets:
        if not isinstance(target, dict):
            return (
                "기존 Gateway Target topology 원장이 올바르지 않아 재배포를 "
                "거부해요. 단계 ③ 승인 흐름이 필요해요."
            )
        sensitivity = str(target.get("sensitivity") or "").strip().upper()
        target_name = str(
            target.get("gateway_target_name") or ""
        ).strip()
        operations = target.get("operations")
        if (
            not sensitivity
            or not target_name
            or not isinstance(operations, list)
        ):
            return (
                "기존 Gateway Target topology 원장이 올바르지 않아 재배포를 "
                "거부해요. 단계 ③ 승인 흐름이 필요해요."
            )
        previous_name = prior_names.setdefault(sensitivity, target_name)
        if previous_name != target_name:
            return (
                f"기존 {sensitivity} 슬라이스의 Target 이름이 중복돼 "
                "재배포를 거부해요. 단계 ③ 승인 흐름이 필요해요."
            )
        for operation in operations:
            operation_id = str(operation).strip()
            if not operation_id:
                continue
            previous = prior_owner.setdefault(operation_id, sensitivity)
            if previous != sensitivity:
                return (
                    f"기존 operation {operation_id}의 Target 소속이 중복돼 "
                    "재배포를 거부해요. 단계 ③ 승인 흐름이 필요해요."
                )

    desired_names = {
        target_slice.sensitivity: target_slice.gateway_target_name
        for target_slice in plan.slices
    }
    desired_owner: dict[str, str] = {}
    for target_slice in plan.slices:
        for operation in target_slice.operations:
            previous = desired_owner.setdefault(
                operation,
                target_slice.sensitivity,
            )
            if previous != target_slice.sensitivity:
                return (
                    f"새 operation {operation}의 Target 소속이 중복돼 재배포를 "
                    "거부해요. 단계 ③ 승인 흐름이 필요해요."
                )

    moved = [
        f"{operation}: {prior_owner[operation]} -> {desired_owner[operation]}"
        for operation in sorted(prior_owner.keys() & desired_owner.keys())
        if prior_owner[operation] != desired_owner[operation]
    ]
    renamed = [
        f"{sensitivity}: {prior_names[sensitivity]} -> "
        f"{desired_names[sensitivity]}"
        for sensitivity in sorted(prior_names.keys() & desired_names.keys())
        if prior_names[sensitivity] != desired_names[sensitivity]
    ]
    added = sorted(desired_names.keys() - prior_names.keys())
    removed = sorted(prior_names.keys() - desired_names.keys())
    if not moved and not renamed and not added and not removed:
        return None

    details: list[str] = []
    if moved:
        details.append("도구 이동: " + ", ".join(moved))
    if renamed:
        details.append("Target 이름 변경: " + ", ".join(renamed))
    if added:
        details.append("추가 슬라이스: " + ", ".join(added))
    if removed:
        details.append("삭제 슬라이스: " + ", ".join(removed))
    return (
        "Gateway Target topology가 바뀌는 재배포를 거부해요 ("
        + "; ".join(details)
        + "). 단계 ③ 승인 흐름이 필요해요."
    )


def _reconcile_gateway_targets(
    job: DeployJob,
    port,
    *,
    gateway_id: str,
    plan: TargetSplit,
) -> list[str]:
    """Create or update approved sensitivity slices and retain evidence."""
    job.unassigned_tools = plan.unassigned_tools
    entries: list[dict] = []
    errors: list[str] = []

    for target_slice in plan.slices:
        sensitivity = target_slice.sensitivity
        target_name = target_slice.gateway_target_name
        entry = {
            "sensitivity": sensitivity,
            "gateway_target_name": target_name,
            "target_id": None,
            "operations": list(target_slice.operations),
            "state": "ABSENT",
            "action": "NONE",
            "error": None,
            "created": False,
        }
        entries.append(entry)
        try:
            existing_target = port.find_gateway_target(
                gateway_id,
                target_name,
            )
        except Exception as exc:
            errors.append(_target_failure(
                entry,
                RuntimeError(
                    "Gateway Target inventory observation unknown: "
                    f"{type(exc).__name__}: {exc}"
                ),
            ))
            continue

        try:
            if job.is_redeploy and existing_target:
                entry["target_id"] = existing_target
                changed = port.update_gateway_target(
                    gateway_id,
                    existing_target,
                    target_name,
                    job.lambda_arn,
                    target_slice.tools_inline,
                )
                entry["action"] = "UPDATE" if changed else "NOOP"
            elif job.is_redeploy:
                entry["target_id"] = port.wire_lambda_target(
                    gateway_id,
                    target_name,
                    job.lambda_arn,
                    target_slice.tools_inline,
                    client_token_seed=job.job_id,
                )
                entry["action"] = "CREATE"
                entry["created"] = True
            elif existing_target:
                retried_target = port.wire_lambda_target(
                    gateway_id,
                    target_name,
                    job.lambda_arn,
                    target_slice.tools_inline,
                    client_token_seed=job.job_id,
                )
                if retried_target != existing_target:
                    raise RuntimeError(
                        "idempotent create returned a different target"
                    )
                entry["target_id"] = retried_target
                entry["action"] = "CREATE_RETRY"
                entry["created"] = True
            else:
                entry["target_id"] = port.wire_lambda_target(
                    gateway_id,
                    target_name,
                    job.lambda_arn,
                    target_slice.tools_inline,
                    client_token_seed=job.job_id,
                )
                entry["action"] = "CREATE"
                entry["created"] = True
            entry["state"] = "SYNCHRONIZING"
        except Exception as exc:
            errors.append(_target_failure(entry, exc))

    job.gateway_targets = tuple(entries)
    if (
        job.gateway_target_quota is None
        and any(
            entry.get("created") and entry.get("target_id")
            for entry in entries
        )
    ):
        job.gateway_target_quota = _observe_gateway_target_quota(
            port,
            gateway_id,
        )
    job.target_id = next(
        (
            str(entry["target_id"])
            for entry in entries
            if entry.get("sensitivity")
            and entry.get("target_id")
            and entry.get("state") not in {"ABSENT", "DELETED", "FAILED"}
        ),
        None,
    )
    return errors


def advance(
    job,
    port,
    *,
    gateway_id,
    exec_role_arn,
    register,
    now,
    owner_id: str,
    redeploy_sensitivity_guard: RedeploySensitivityGuard | None = None,
    provision_shared_policy=None,
    provision_read_access=None,
):
    if terminal(job.phase):
        return job

    if job.phase is DeployPhase.QUEUED:
        job.build_id = port.start_build(
            job.job_id, job.source_ref, job.build_type, job.asset_type)
        job.phase = DeployPhase.BUILDING
        job.updated_at = now()
        return job

    if job.phase is DeployPhase.BUILDING:
        bs = port.get_build_status(job.build_id)
        if bs.state == "IN_PROGRESS":
            return job
        if bs.state == "FAILED":
            return _fail(
                job, "building", bs.reason or "build failed",
                port, gateway_id, now, owner_id,
            )
        artifact = bs.artifact or BuildArtifact(build_type=job.build_type, uri="")
        job.artifact_uri = artifact.uri
        job.tools_inline = artifact.tools_inline or job.tools_inline
        try:
            job.tools_inline = _selected_tools_inline(
                job.tools_inline or "",
                job.selected_tools,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as e:
            return _fail(
                job, "building", str(e), port, gateway_id, now, owner_id,
            )
        display_name = job.meta.get("name") or job.job_id
        applied_sensitivity_changes: list[dict] = []
        if job.is_redeploy:
            prior_sensitivity_error = (
                dict(job.error)
                if isinstance(job.error, dict)
                and job.error.get("phase") == "sensitivity_approval"
                else {}
            )
            if redeploy_sensitivity_guard is None:
                decision = {
                    "status": "unknown",
                    "reason": "재배포 민감도 승인 게이트가 배선되지 않았어요.",
                }
            else:
                try:
                    guard_request: RedeploySensitivityRequest = {
                        "record_id": job.redeploy_record_id,
                        "tools_inline": job.tools_inline,
                        "actor": job.principal,
                    }
                    decision = redeploy_sensitivity_guard(guard_request)
                except Exception as exc:
                    decision = {
                        "status": "unknown",
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
            if not isinstance(decision, dict):
                decision = {
                    "status": "unknown",
                    "reason": "재배포 민감도 승인 게이트 응답이 올바르지 않아요.",
                }
            if decision.get("status") != "ready":
                job.error = {
                    "phase": "sensitivity_approval",
                    "status": str(decision.get("status") or "unknown"),
                    "message": str(
                        decision.get("reason")
                        or "민감도 변경 이동 또는 승인을 기다리고 있어요."
                    ),
                }
                if isinstance(decision.get("change"), dict):
                    job.error["change"] = decision["change"]
                if isinstance(decision.get("impact"), list):
                    job.error["impact"] = decision["impact"]
                job.updated_at = now()
                return job
            sanitized_tools_inline = decision.get("tools_inline")
            if not isinstance(sanitized_tools_inline, str):
                job.error = {
                    "phase": "sensitivity_approval",
                    "status": "unknown",
                    "message": (
                        "재배포 민감도 게이트의 정제 schema를 "
                        "확인하지 못했어요."
                    ),
                }
                job.updated_at = now()
                return job
            job.tools_inline = sanitized_tools_inline
            raw_changes = decision.get("changes")
            if isinstance(raw_changes, list):
                applied_sensitivity_changes.extend(
                    dict(change)
                    for change in raw_changes
                    if isinstance(change, dict)
                )
            prior_change = prior_sensitivity_error.get("change")
            if (
                isinstance(prior_change, dict)
                and not any(
                    change.get("request_id")
                    == prior_change.get("request_id")
                    for change in applied_sensitivity_changes
                )
            ):
                applied_sensitivity_changes.append(dict(prior_change))
            job.error = None
        name = (
            job.meta.get("_gateway_target_name")
            or gateway_target_name(display_name)
        )
        try:
            plan = split_target_slices(
                display_name,
                job.tools_inline or "",
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return _fail(
                job,
                "building",
                f"Gateway Target 슬라이스 계산 실패: {exc}",
                port,
                gateway_id,
                now,
                owner_id,
            )
        job.unassigned_tools = plan.unassigned_tools
        topology_error = _redeploy_topology_error(job, plan)
        # A wired stage-3 guard reaches here only after the required movement
        # or downgrade approval completed, replacing stage 2's blanket rejection.
        if topology_error and redeploy_sensitivity_guard is None:
            return _fail(
                job,
                "building",
                topology_error,
                port,
                gateway_id,
                now,
                owner_id,
            )
        # Lambda is shared by the sensitivity-derived target slices.
        try:
            if job.is_redeploy:
                deployment = port.update_lambda_code(
                    name,
                    artifact,
                    exec_role_arn=exec_role_arn,
                    owner_job_id=job.job_id,
                    owner_asset_id=job.source_ref.asset_id,
                    owner_principal=job.principal,
                    owner_source_version=job.source_ref.version,
                )
                job.lambda_arn = deployment.arn
                if deployment.created:
                    job.resource_owner_id = owner_id
            else:
                deployment = port.create_lambda(
                    name,
                    artifact,
                    exec_role_arn=exec_role_arn,
                    owner_job_id=job.job_id,
                    owner_asset_id=job.source_ref.asset_id,
                    owner_principal=job.principal,
                    owner_source_version=job.source_ref.version,
                )
                job.lambda_arn = deployment.arn
                if deployment.created:
                    job.resource_owner_id = owner_id
        except Exception as e:
            verb = "갱신" if job.is_redeploy else "생성"
            failed = _fail(
                job, "building", f"Lambda {verb} 실패: {e}",
                port, gateway_id, now, owner_id,
            )
            if job.is_redeploy and applied_sensitivity_changes:
                failed.error["sensitivity_consistency"] = {
                    "status": "diverged",
                    "reason": (
                        "민감도 Target 이동은 적용됐지만 Lambda 코드 갱신은 "
                        "실패했어요."
                    ),
                    "changes": applied_sensitivity_changes,
                }
            return failed
        target_errors = _reconcile_gateway_targets(
            job,
            port,
            gateway_id=gateway_id,
            plan=plan,
        )
        if target_errors:
            return _fail(
                job,
                "building",
                "Gateway Target 일부 조정 실패: " + "; ".join(target_errors),
                port,
                gateway_id,
                now,
                owner_id,
            )
        job.phase = DeployPhase.REGISTERING_TARGET
        job.updated_at = now()
        return job

    if job.phase is DeployPhase.REGISTERING_TARGET:
        if job.gateway_targets:
            entries = [dict(target) for target in job.gateway_targets]
            target_errors: list[str] = []
            synchronizing = False
            for entry in entries:
                if (
                    not entry.get("sensitivity")
                    or not entry.get("target_id")
                    or entry.get("state") in {"ABSENT", "DELETED"}
                ):
                    continue
                try:
                    status = port.get_target_status(
                        gateway_id,
                        str(entry["target_id"]),
                    )
                except Exception as exc:
                    target_errors.append(_target_failure(entry, exc))
                    continue
                if status.state == "SYNCHRONIZING":
                    entry["state"] = "SYNCHRONIZING"
                    synchronizing = True
                elif status.state == "READY":
                    entry["state"] = "READY"
                    entry["error"] = None
                else:
                    reason = status.reason or "gateway target failed"
                    target_errors.append(
                        _target_failure(entry, RuntimeError(reason))
                    )
            job.gateway_targets = tuple(entries)
            if target_errors:
                return _fail(
                    job,
                    "registering_target",
                    "Gateway Target 일부 동기화 실패: "
                    + "; ".join(target_errors),
                    port,
                    gateway_id,
                    now,
                    owner_id,
                )
            if synchronizing:
                return job
        elif job.target_id:
            # Jobs created before split-target rollout retain the legacy field.
            ts = port.get_target_status(gateway_id, job.target_id)
            if ts.state == "SYNCHRONIZING":
                return job
            if ts.state == "FAILED":
                return _fail(
                    job, "registering_target",
                    ts.reason or "gateway target failed",
                    port, gateway_id, now, owner_id,
                )
        job.gateway_url = port.gateway_endpoint(gateway_id)
        try:
            job.record_id = register(job)
        except Exception as e:
            return _fail(
                job, "registering_target", f"카탈로그 등재 실패: {e}",
                port, gateway_id, now, owner_id,
            )
        # IA-71: Target 목록이 바뀌었으니 Gateway 공유 정책을 다시 맞춰요(ADR-0080·0093).
        #
        # **등재 뒤에 불러요** — provisioner 는 Registry 의 Target 원장에서 정책을 컴파일하니
        # 이 자산의 record 가 먼저 있어야 해요.
        #
        # 정책이 없으면 그 Gateway 는 fail-closed 예요(Cedar 가 전부 거부). 그래서 실패를
        # 삼키지 않고 job 을 실패시켜요 — "배포됐는데 아무 도구도 못 부르는" 상태를 성공으로
        # 보여주면 안 돼요. 도메인 경계 때문에 identity 를 직접 import 하지 않고 주입받아요.
        if provision_shared_policy is not None:
            try:
                report = provision_shared_policy(job)
            except Exception as e:
                return _fail(
                    job, "registering_target",
                    f"Gateway 공유 정책 provisioning 실패: {type(e).__name__}: {e}",
                    port, gateway_id, now, owner_id,
                )
            job.shared_policy_report = report
            if not report.get("ok", False):
                return _fail(
                    job, "registering_target",
                    "Gateway 공유 정책을 올리지 못했어요("
                    f"{report.get('verdict') or 'unknown'}): "
                    f"{report.get('reason') or ''}",
                    port, gateway_id, now, owner_id,
                )
        # ADR-0094: 카탈로그 READ 도구를 회원 그룹에 기본 부여해요.
        #
        # **실패해도 배포를 막지 않아요.** 권한이 없으면 아무도 못 부르는 fail-closed 상태고,
        # 정책 부재와 달리 자산 자체는 정상 등재예요. 관리자가 나중에 수동 부여할 수 있어요.
        # 대신 결과를 job 에 남겨 "왜 못 부르나" 를 화면이 말할 수 있게 해요(ADR-0094 결정 7).
        if provision_read_access is not None:
            try:
                job.read_access_report = provision_read_access(job)
            except Exception as exc:
                job.read_access_report = {
                    "warnings": [
                        "READ 기본 권한 부여에 실패했어요: "
                        f"{type(exc).__name__}: {exc}"
                    ],
                }
        job.phase = DeployPhase.READY
        job.updated_at = now()
        return job

    return job
