"""PurgeService — 자산 하나(모든 버전)와 연결된 모든 데이터·리소스를 완전 삭제해요.

의존성 역순으로 best-effort 정리해요: 참조(AWS 리소스·소스·거버넌스·요청·번들)를
먼저 지우고, 레지스트리 레코드를 마지막에 지워요. 각 단계는 독립 try/except라
한 단계 실패가 나머지를 막지 않아요. 결과는 PurgeReport로 반환해요.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping

from ..registry.models import DescriptorType, RecordStatus
from .models import PurgeReport

# 완전삭제(complete-delete)는 DEPRECATED된 버전도 반드시 지워야 하니 전 상태를 훑어요.
# registry.list_records 기본값은 DEPRECATED/CREATING을 빼서 sibling이 누락되거든요.
#
# **enum 을 그대로 써요 — 손으로 적지 않아요.** 6개를 하드코딩했다가 `UPDATING` 을 빠뜨려서,
# purge 시점에 재배포 중이던 sibling 이 `list_records` 결과에서 빠지고 그 버전의 레코드·소스·
# 거버넌스·통계·번들·신원·도구 승인이 전부 살아남았어요(2026-09-06 확인). 보고서에도 아무
# 흔적이 없었어요. enum 을 쓰면 새 상태가 생겨도 자동으로 포함돼요 —
# `catalog/mcp/approval_cleanup.py` 와 `catalog/router.py` 가 이미 그 방식이에요.
_ALL_STATUSES = tuple(RecordStatus)

_log = logging.getLogger(__name__)

# sourcePrefix가 들어있을 수 있는 descriptor 키(skill/mcp/agent 공통 규약).
_SOURCE_KEYS = ("skill", "mcp", "agent")


def _asset_id_from_record(rec) -> str | None:
    """레코드 descriptors에서 asset_id(owner/name)를 도출해요. sourcePrefix 규약:
    "{type}/{owner}/{name}/{version}/" → owner/name."""
    for key in _SOURCE_KEYS:
        node = rec.descriptors.get(key) if isinstance(rec.descriptors, dict) else None
        if isinstance(node, dict):
            prefix = node.get("sourcePrefix")
            if isinstance(prefix, str) and prefix:
                parts = [p for p in prefix.split("/") if p]
                if len(parts) >= 4:                       # [type, owner, name, version]
                    return "/".join(parts[1:-1])          # owner/name
    return None


def _connect_target(rec) -> tuple[str, str] | None:
    node = rec.descriptors.get("mcp") if isinstance(rec.descriptors, dict) else None
    if isinstance(node, dict):
        tid = node.get("gatewayTargetId")
        if isinstance(tid, str) and tid:
            gateway_id = node.get("gatewayIdentifier")
            return (
                gateway_id if isinstance(gateway_id, str) else "",
                tid,
            )
    return None


def _split_targets(rec) -> list[tuple[str, str]]:
    """Read sensitivity-sliced target coordinates from a deploy descriptor."""
    node = rec.descriptors.get("mcp") if isinstance(rec.descriptors, dict) else None
    if not isinstance(node, dict) or "gatewayTargets" not in node:
        return []
    raw_targets = node.get("gatewayTargets")
    gateway_id = node.get("gatewayIdentifier")
    if not isinstance(raw_targets, list):
        raise ValueError("MCP gatewayTargets ledger is not a list")
    if raw_targets and (
        not isinstance(gateway_id, str) or not gateway_id.strip()
    ):
        raise ValueError("MCP gatewayTargets ledger has no gateway identifier")

    targets: list[tuple[str, str]] = []
    for target in raw_targets:
        if not isinstance(target, dict):
            raise ValueError("MCP gatewayTargets ledger has a non-object entry")
        target_id = target.get("gatewayTargetId")
        if not isinstance(target_id, str) or not target_id.strip():
            raise ValueError("MCP gatewayTargets ledger has no target identifier")
        coordinate = (gateway_id, target_id)
        if coordinate not in targets:
            targets.append(coordinate)
    return targets


def _uses_deploy_runtime(rec) -> bool:
    """Whether the record can own resources in the runtime deploy ledger."""
    descriptors = rec.descriptors if isinstance(rec.descriptors, dict) else {}
    for key in ("mcp", "agent"):
        node = descriptors.get(key)
        if isinstance(node, dict) and node.get("sourcePrefix"):
            return True
    agent = descriptors.get("agent")
    if not isinstance(agent, dict):
        return False
    execution = agent.get("executionBinding")
    return bool(
        agent.get("runtimeArn")
        or agent.get("runtimeId")
        or (
            isinstance(execution, dict)
            and (execution.get("runtimeArn") or execution.get("runtimeId"))
        )
    )


def _memory_id(rec) -> str | None:
    agent = (
        rec.descriptors.get("agent")
        if isinstance(rec.descriptors, dict)
        else None
    )
    memory = agent.get("memory") if isinstance(agent, dict) else None
    value = memory.get("memoryId") if isinstance(memory, dict) else None
    return value if isinstance(value, str) and value else None


def _builtin_resources(rec) -> dict[str, str]:
    agent = (
        rec.descriptors.get("agent")
        if isinstance(rec.descriptors, dict)
        else None
    )
    resources = (
        agent.get("builtinToolResources")
        if isinstance(agent, dict)
        else None
    )
    if not isinstance(resources, dict):
        return {}
    return {
        kind: str(value["id"])
        for kind, value in resources.items()
        if kind in {"browser", "code_interpreter"}
        and isinstance(value, dict)
        and value.get("id")
    }


class PurgeService:
    def __init__(self, *, registry, registry_id, deploy_service, connect_gateway,
                 source_store, deploy_port, gov_store, request_log, bundle_store,
                 stats_store, identity_store=None,
                 authorization_reclaimer=None,
                 asset_capability_cleaner=None,
                 access_grant_cleaner=None,
                 provision_shared_policy=None) -> None:
        self.registry = registry
        self.registry_id = registry_id
        self.deploy_service = deploy_service
        self.connect_gateway = connect_gateway
        self.source_store = source_store
        self.deploy_port = deploy_port
        self.gov_store = gov_store
        self.request_log = request_log
        self.bundle_store = bundle_store
        self.stats_store = stats_store
        # 외부 Agent workload 신원 회수용. 미주입이면 그 단계를 skip으로 남겨요(하위호환).
        self.identity_store = identity_store
        # 관리형 Agent의 Cedar/Cognito/인가 원장 회수 port. identity 도메인 조립은
        # shared.deps가 소유하고 catalog는 record_id만 넘겨요.
        self.authorization_reclaimer = authorization_reclaimer
        # MCP AssetCapability 삭제·감사 구현은 identity가 소유해요. Catalog는 삭제 전
        # Registry 소유 좌표를 확인하고 shared contract만 호출합니다.
        self.asset_capability_cleaner = asset_capability_cleaner
        # ⑦ access grant 삭제·감사 구현도 identity가 소유해요. Catalog는 record_id만 넘겨요 —
        # AuditEvent를 여기서 조립하면 catalog가 identity 모델에 직접 의존하게 돼요.
        self.access_grant_cleaner = access_grant_cleaner
        # MCP purge가 ④ 선언 집합을 줄인 직후 Gateway 공유 정책을 다시 맞춰요.
        # 배포 job과 같은 기본 60-poll 예산을 쓰도록 callable을 인자 없이 호출합니다.
        self.provision_shared_policy = provision_shared_policy

    def purge(self, record_id: str, *, actor: str = "") -> PurgeReport:
        report = PurgeReport()
        rec = self.registry.get_record(self.registry_id, record_id)
        asset_id = _asset_id_from_record(rec)

        # 같은 asset_id의 모든 sibling record_id 수집(멀티 버전 완전 삭제).
        sibling_ids = self._sibling_record_ids(record_id, asset_id)
        is_mcp_purge = (
            getattr(rec.descriptor_type, "value", rec.descriptor_type)
            == DescriptorType.MCP.value
        )

        def _stage(label: str, fn):
            try:
                fn()
                report.deleted.append(label)
                return True
            except Exception as e:                        # best-effort
                _log.exception("purge stage failed: %s", label)
                report.failed.append({"store": label, "reason": f"{type(e).__name__}: {e}"})
                return False

        # 1. AWS 런타임 리소스 (모든 sibling record)
        def _teardown_aws():
            errors: list[Exception] = []
            split_targets: list[tuple[str, str]] = []
            for rid in sibling_ids:
                try:
                    record = self.registry.get_record(self.registry_id, rid)
                    for coordinate in _split_targets(record):
                        if coordinate not in split_targets:
                            split_targets.append(coordinate)
                    if _uses_deploy_runtime(record):
                        self.deploy_service.teardown_for_record(rid)
                except Exception as exc:
                    errors.append(exc)
            # The Registry descriptor is an independent fallback for active
            # targets when a deploy-job row is missing. Production deletion is
            # idempotent, so coordinates also present in the job ledger are safe.
            for gateway_id, target_id in split_targets:
                try:
                    self.deploy_port.delete_target(gateway_id, target_id)
                except Exception as exc:
                    errors.append(RuntimeError(
                        f"{gateway_id}/{target_id}: "
                        f"{type(exc).__name__}: {exc}"
                    ))
            if len(errors) == 1:
                raise errors[0]
            if errors:
                raise RuntimeError("; ".join(
                    f"{type(error).__name__}: {error}"
                    for error in errors
                ))
        if not _stage("aws-runtime", _teardown_aws):
            # Deploy jobs and Registry descriptors own the exact runtime/Target
            # coordinates needed for a safe retry.
            return report

        # 2. 관리형 Agent 인가 — Runtime이 더는 호출하지 못하게 한 뒤 Cedar permit,
        #    Cognito client, identity 원장을 의존 역순으로 회수해요. Memory나 내장도구
        #    정리가 막혀도 유효한 credential/permit을 남기지 않도록 그보다 먼저 실행해요.
        agent_ids = (
            sibling_ids
            if rec.descriptor_type == DescriptorType.AGENT
            else []
        )
        job_client_ids: dict[str, set[str]] = {
            rid: set()
            for rid in agent_ids
        }
        if agent_ids:
            try:
                jobs_for_coordinates = self.deploy_service.store.list()
            except Exception as e:
                _log.exception("purge stage failed: deploy-job-coordinates")
                report.failed.append({
                    "store": "deploy-job-coordinates",
                    "reason": f"{type(e).__name__}: {e}",
                })
                return report
            for job in jobs_for_coordinates:
                candidate = str(
                    getattr(job, "oauth_client_id", "") or ""
                )
                if job.record_id in job_client_ids and candidate:
                    job_client_ids[job.record_id].add(candidate)
        job_client_cleanup_failed = False
        if self.authorization_reclaimer is not None:
            for rid in agent_ids:
                candidates = job_client_ids[rid]
                if len(candidates) > 1:
                    report.failed.append({
                        "store": f"agent-authorization:{rid}",
                        "reason": (
                            "RuntimeError: multiple oauth client coordinates "
                            "found in deploy jobs"
                        ),
                    })
                    job_client_cleanup_failed = True
                    continue
                fallback_client_id = next(iter(candidates), "")
                cleanup_succeeded = _stage(
                    f"agent-authorization:{rid}",
                    lambda record_id=rid, fallback=fallback_client_id: (
                        self.authorization_reclaimer(
                            record_id,
                            fallback_managed_client_id=fallback,
                        )
                        if fallback
                        else self.authorization_reclaimer(record_id)
                    ),
                )
                if fallback_client_id and not cleanup_succeeded:
                    job_client_cleanup_failed = True
        elif agent_ids:
            report.skipped.append("agent-authorization (reclaimer 미주입)")
            job_client_cleanup_failed = any(job_client_ids.values())
        if job_client_cleanup_failed:
            return report

        memory_ids = {
            memory_id
            for rid in sibling_ids
            if (record := self._safe_get(rid)) is not None
            if (memory_id := _memory_id(record))
        }
        if memory_ids:
            try:
                for memory_id in sorted(memory_ids):
                    self.deploy_port.delete_memory(memory_id)
                report.deleted.append("agentcore-memory")
            except Exception as e:
                # Memory carries billed user data. Keep Registry descriptors and
                # deploy jobs as durable retry coordinates until deletion succeeds.
                _log.exception("purge stage failed: agentcore-memory")
                report.failed.append({
                    "store": "agentcore-memory",
                    "reason": f"{type(e).__name__}: {e}",
                })
                return report

        builtin_resources = {
            (kind, resource_id)
            for rid in sibling_ids
            if (record := self._safe_get(rid)) is not None
            for kind, resource_id in _builtin_resources(record).items()
        }
        if builtin_resources:
            try:
                for kind, resource_id in sorted(builtin_resources):
                    self.deploy_port.delete_builtin_tool(kind, resource_id)
                report.deleted.append("agentcore-builtin-tools")
            except Exception as e:
                # Active sessions can make Delete* fail. Preserve descriptors and
                # jobs as retry coordinates instead of reporting a false cleanup.
                _log.exception("purge stage failed: agentcore-builtin-tools")
                report.failed.append({
                    "store": "agentcore-builtin-tools",
                    "reason": f"{type(e).__name__}: {e}",
                })
                return report

        # 3. connect-MCP Gateway target (모든 sibling 버전 — 버전마다 target이 달라요)
        #    각 sibling record의 gatewayTargetId를 모아 중복 제거 후 하나씩 teardown 해요.
        targets: list[tuple[str, str]] = []
        for rid in sibling_ids:
            r = self._safe_get(rid)
            if r is not None:
                target = _connect_target(r)
                if target and target not in targets:
                    targets.append(target)
        if targets:
            target_delete_failed = False
            for gateway_id, target_id in targets:
                label = (
                    f"connect-gateway-target:{gateway_id or '<legacy>'}:{target_id}"
                )
                try:
                    self.connect_gateway.teardown(
                        target_id,
                        gateway_id=gateway_id,
                    )
                    report.deleted.append(label)
                except Exception as e:
                    target_delete_failed = True
                    _log.exception("purge stage failed: %s", label)
                    report.failed.append({
                        "store": label,
                        "reason": f"{type(e).__name__}: {e}",
                    })
            if target_delete_failed:
                # Registry descriptor가 gateway/target 재시도 좌표의 durable owner예요.
                # 하나라도 실패하면 모든 sibling record를 보존하고 다음 purge에서
                # 성공 target의 NotFound를 멱등 성공으로 처리하며 다시 대조해요.
                return report

        # 4. 소스 전 버전 + S3 객체
        if asset_id:
            def _purge_source():
                res = self.source_store.purge_asset(asset_id)
                for err in res.get("s3_errors", []):
                    report.failed.append({"store": "source-s3", "reason": err})
            _stage("source", _purge_source)
        else:
            report.skipped.append("source (asset_id 도출 실패)")

        # 5. 빌드 아티팩트 (best-effort; asset_id+각 버전)
        if asset_id:
            def _purge_artifacts():
                for rid in sibling_ids:
                    r = self._safe_get(rid)
                    if r is not None:
                        self.deploy_port.purge_artifacts(asset_id, r.version)
            _stage("build-artifacts", _purge_artifacts)

        # 6. 거버넌스 (버전별 record_id)
        def _purge_gov():
            for rid in sibling_ids:
                self.gov_store.purge_record(rid)
        _stage("governance", _purge_gov)

        # 7. 요청 로그
        def _purge_requests():
            for rid in sibling_ids:
                self.request_log.delete_by_record(rid)
        _stage("request-log", _purge_requests)

        # 8. deploy job row
        def _purge_jobs():
            for job in self.deploy_service.store.list():
                if (
                    getattr(job, "record_id", None) in sibling_ids
                    or getattr(job, "redeploy_record_id", None) in sibling_ids
                ):
                    self.deploy_service.store.delete(job.job_id)
        _stage("deploy-jobs", _purge_jobs)

        # 9. 번들 멤버십
        def _purge_bundles():
            for rid in sibling_ids:
                self.bundle_store.remove_member_everywhere(rid)
        _stage("bundle-membership", _purge_bundles)

        # 10. 다운로드 통계 카운터 + 스냅샷 무효화 — registry 삭제 앞
        def _purge_stats():
            for rid in sibling_ids:
                self.stats_store.purge_record(rid)
            self.stats_store.invalidate_snapshot()
        _stage("download-stats", _purge_stats)

        # 11. 외부 Agent workload 신원 — registry 삭제 앞.
        #     신원은 descriptors가 아니라 identity 스토어에 있어서(ADR-017 리뷰 수정) 레코드를
        #     지워도 자동으로 사라지지 않아요. 남겨두면 회수되지 않은 공개키가 신원 평면에
        #     방치돼요 — BACKLOG의 "폐기 시 AccessGrant를 함께 회수한다"와 같은 맥락이에요.
        if self.identity_store is not None:
            # record별로 예외를 격리하고 **record별로 결과를 남겨요.** loop 전체가 중단되면
            # 뒤 sibling의 공개키가 조용히 남고, 그 뒤 registry 단계는 레코드를 전부 지워서
            # 남은 신원을 추적할 단서도 사라져요. stage 하나만 실패로 적으면 "어느 record의
            # 신원이 남았는지"를 알 수 없어 수동 정리도 불가능해요.
            for rid in sibling_ids:
                def _purge_one(record_id=rid):
                    self.identity_store.delete_external_workload_identity(record_id)
                _stage(f"external-workload-identity:{rid}", _purge_one)
        else:
            report.skipped.append("external-workload-identity (store 미주입)")

        # 12. MCP를 참조하는 모든 agent ④ binding — Registry agent 목록이 아니라
        # identity snapshot으로 partition을 발견하고, 소비자와 같은 Query로 각 partition을
        # 읽어요. 그래야 Registry record가 없는 dev credential pseudo-agent도 정리돼요.
        mcp_record_ids = sibling_ids if is_mcp_purge else []
        mcp_bindings_observed_absent = not mcp_record_ids
        if mcp_record_ids and self.identity_store is not None:
            mcp_bindings_observed_absent = _stage(
                "mcp-agent-tool-bindings",
                lambda: self._purge_mcp_agent_tool_bindings(mcp_record_ids),
            )
        elif mcp_record_ids:
            report.skipped.append("mcp-agent-tool-bindings (store 미주입)")

        # 13. 이 자산을 가리키는 모든 ⑦ access grant — ④ 바로 뒤, Registry 삭제 «앞» 이에요.
        #
        # **왜 이 자리인가.** (1) 인가 층을 위에서 아래로 걷어내는 기존 순서와 같아요 —
        # ④ 를 먼저 지우면 그 사이 창에서도 interceptor 가 ⑦ 를 읽을 일이 없어요(④ 가 0행이면
        # ⑦ 조회 자체가 안 일어나요). (2) 단계 16 주석이 적어 둔 이유가 여기도 그대로 적용돼요
        # — Registry 레코드가 사라지면 소유자·좌표 증거를 잃어서 감사 기록을 남길 수 없어요.
        # (3) ⑦ 는 공유 정책 열거에 참여하지 않으니(열거 기준은 ④ 선언 집합) 단계 15 와는
        # 순서 의존이 없어요. 그래도 앞에 두는 편이 「인가 원장 정리 → 정책 수렴」 으로 읽혀요.
        mcp_grants_observed_absent = not mcp_record_ids
        if mcp_record_ids and self.access_grant_cleaner is not None:
            mcp_grants_observed_absent = _stage(
                "mcp-access-grants",
                lambda: self._purge_mcp_access_grants(
                    mcp_record_ids,
                    actor=actor,
                ),
            )
        elif mcp_record_ids:
            report.skipped.append("mcp-access-grants (cleaner 미주입)")

        # 14. 자산 «현재 버전» 행(`ASSET#<record_id>` / `VERSION`) — record_id 마다 한 행이에요.
        #
        # **왜 ④·⑦ 뒤인가.** 이 행은 ④ 세대 대조의 **기대값**이에요(ADR-0099 결정 13). ④ 행이
        # 아직 있는 동안 지우면 interceptor 가 그 자산의 도구를 전부 거부해요 — fail-closed 라
        # 열리지는 않지만 필요 없는 창이에요. ④·⑦ 를 먼저 걷어내면 그 창이 아예 없어요.
        # Registry 삭제 앞인 이유는 앞 단계들과 같아요: 삭제가 실패하면 record_id 를 다시 찾을
        # 수 있는 durable 좌표가 Registry 레코드뿐이에요.
        #
        # **⑦ 단계에 합치지 않은 이유.** `_stage` 라벨이 운영자의 재시도 좌표예요. 다른 행
        # 계열의 실패를 `mcp-access-grants` 로 보고하면 엉뚱한 테이블을 보게 돼요. 단계 11 처럼
        # record 별 라벨을 써서 어느 sibling 이 남았는지도 남겨요.
        #
        # **자산 종류로 좁히지 않아요.** 키에 종류 discriminator 가 없고 삭제는 멱등이에요.
        # 오늘 이 행을 쓰는 유일한 경로는 MCP 배포지만(`deps._record_asset_version`), ⑦ 잔재가
        # 생긴 이유가 바로 「writer 의 범위를 cleanup 의 범위로 가정한 것」이었어요.
        #
        # store 미주입은 단계 11 과 같이 `skipped` 로 남기고 Registry 를 막지는 않아요.
        # 프로덕션은 항상 주입해요(`test_production_purge_wires_*` 가 그걸 못박아요) — 여기서
        # 막으면 이 그래프에 도달할 수 없는 분기 때문에 무관한 테스트 다수를 바꿔야 해요.
        asset_versions_observed_absent = True
        if self.identity_store is not None:
            for rid in sibling_ids:
                def _purge_asset_version(record_id=rid):
                    self.identity_store.delete_asset_version(record_id)
                if not _stage(f"asset-version:{rid}", _purge_asset_version):
                    asset_versions_observed_absent = False
        else:
            report.skipped.append("asset-version (store 미주입)")

        # 15. ④ 선언 집합 축소를 공유 Cedar 정책에 반영해요. HTTP의 8-poll helper를
        # 거치지 않고 배포 job과 같은 기본 60-poll provisioner를 직접 호출합니다.
        shared_policy_observed_converged = not mcp_record_ids
        if mcp_record_ids and self.provision_shared_policy is not None:
            def _provision_shared_policy():
                raw_report = self.provision_shared_policy()
                if not isinstance(raw_report, Mapping):
                    raise RuntimeError(
                        "shared policy provisioner returned "
                        f"{type(raw_report).__name__}"
                    )
                warnings = raw_report.get("warnings")
                if raw_report.get("ok") is not True or warnings:
                    reason = str(
                        warnings
                        or raw_report.get("reason")
                        or raw_report.get("verdict")
                        or "unknown"
                    )
                    raise RuntimeError(
                        f"shared policy provisioning did not succeed: {reason}"
                    )

            shared_policy_observed_converged = _stage(
                "shared-gateway-policy",
                _provision_shared_policy,
            )
        elif mcp_record_ids:
            report.skipped.append("shared-gateway-policy (provisioner 미주입)")

        # 16. MCP 자산 권한 승인 — Registry가 소유자 증거를 잃기 전에 회수해요.
        #
        # 같은 Target의 새 record는 기존 record가 live인 동안 등록 충돌로 막혀요. 따라서
        # 재등록 hook에서 old/new를 함께 찾는 것은 정상 수명주기에서 불가능하고, hard purge가
        # old record를 지우기 직전이 소유자를 확인하며 승인을 회수할 마지막 시점이에요.
        if self.asset_capability_cleaner is not None:
            for candidate_id in mcp_record_ids:
                label = f"asset-capability:{candidate_id}"
                try:
                    record = self.registry.get_record(
                        self.registry_id,
                        candidate_id,
                    )
                except Exception:  # noqa: BLE001 - retain an unknown coordinate.
                    reason = "purge_record_unobservable"
                    try:
                        self.asset_capability_cleaner.record_unknown(
                            candidate_id,
                            replacement_record_id="",
                            actor=actor,
                            reason=reason,
                        )
                    except Exception:  # noqa: BLE001 - report/log retain coordinates.
                        _log.exception(
                            "asset capability purge unknown audit failed; "
                            "record_id=%s actor=%s reason=%s",
                            candidate_id,
                            actor,
                            reason,
                        )
                    report.failed.append({
                        "store": label,
                        "reason": reason,
                    })
                    continue
                owner = str(getattr(record, "owner_user", "") or "")
                reason = ""
                if not owner:
                    reason = "purge_owner_unobservable"
                elif not actor:
                    reason = "purge_actor_unobservable"
                elif owner != actor:
                    reason = "purge_actor_owner_mismatch"
                if reason:
                    try:
                        self.asset_capability_cleaner.record_unknown(
                            record.record_id,
                            replacement_record_id="",
                            actor=actor,
                            reason=reason,
                        )
                    except Exception:  # noqa: BLE001 - report/log retain coordinates.
                        _log.exception(
                            "asset capability purge unknown audit failed; "
                            "record_id=%s actor=%s reason=%s",
                            record.record_id,
                            actor,
                            reason,
                        )
                    report.failed.append({
                        "store": label,
                        "reason": reason,
                    })
                    continue
                try:
                    cleanup = (
                        self.asset_capability_cleaner.delete_record_approvals(
                            record.record_id,
                            replacement_record_id="",
                            actor=actor,
                        )
                    )
                except Exception as e:  # noqa: BLE001 - registry deletion continues.
                    _log.exception(
                        "asset capability purge failed; record_id=%s actor=%s",
                        record.record_id,
                        actor,
                    )
                    report.failed.append({
                        "store": label,
                        "reason": f"{type(e).__name__}: {e}",
                    })
                    continue
                for candidate_id, operation_id in cleanup.unknown:
                    report.failed.append({
                        "store": (
                            f"asset-capability:{candidate_id}:{operation_id}"
                        ),
                        "reason": "delete_failed",
                    })
                if not cleanup.unknown:
                    report.deleted.append(label)
        elif mcp_record_ids:
            report.skipped.append("asset-capability (cleaner 미주입)")

        # 17. 레지스트리 레코드 (aux purge 체이닝) — 마지막
        def _purge_registry():
            for rid in sibling_ids:
                self.registry.delete_record(self.registry_id, rid)
        # 원인별로 갈라서 남겨요 — 운영자가 어느 원장을 봐야 하는지 알 수 있어야 해요.
        # MCP 전용 블로커만 걸렸을 때의 문구는 그대로 유지해요(기존 계약).
        registry_blockers: list[str] = []
        if (
            mcp_record_ids
            and not (
                mcp_bindings_observed_absent
                and mcp_grants_observed_absent
                and shared_policy_observed_converged
            )
        ):
            registry_blockers.append("MCP authorization cleanup incomplete")
        if not asset_versions_observed_absent:
            registry_blockers.append("asset version rows unobserved")
        if registry_blockers:
            report.skipped.append(
                f"registry ({'; '.join(registry_blockers)})"
            )
        else:
            _stage("registry", _purge_registry)

        # CloudWatch 로그·ECR은 정리 API 없음 — skipped 명시.
        report.skipped.append("cloudwatch-logs (retention only)")
        return report

    def _purge_mcp_access_grants(
        self,
        mcp_record_ids: list[str],
        *,
        actor: str,
    ) -> None:
        """이 자산의 모든 버전을 가리키는 ⑦ 행을 사람 축·그룹 축 양쪽에서 지워요.

        발견·삭제·감사는 identity 쪽(`IdentityAccessGrantCleaner`)이 소유해요. 여기서는
        **관측 실패와 「0행」을 구분**해서 예외로 올리는 일만 해요 — `_stage` 가 그걸
        `report.failed` 에 넣고, 그러면 단계 16 이 Registry 레코드를 보존해요.
        """
        report = self.access_grant_cleaner.delete_record_grants(
            tuple(mcp_record_ids),
            actor=actor,
        )
        if not report.observed:
            raise RuntimeError(
                "⑦ access grant 원장을 읽지 못했어요 — 「0행」이 아니에요: "
                f"{report.reason or 'unknown'}"
            )
        if report.unknown:
            coordinates = ", ".join(
                f"{subject}/{asset_id}/{operation_id}"
                for subject, asset_id, operation_id in report.unknown
            )
            raise RuntimeError(
                "⑦ access grant 가 purge 뒤에도 남아 있어요: "
                f"{coordinates}"
            )

    def _purge_mcp_agent_tool_bindings(
        self,
        mcp_record_ids: list[str],
    ) -> None:
        """MCP sibling을 참조하는 ④ 행을 모든 agent partition에서 제거해요."""
        purged_asset_ids = set(mcp_record_ids)
        snapshot = self.identity_store.get_agent_authorization_ledger_snapshot()
        # 공유 정책 소비자 observe_policy_ledger와 같은 발견식이며, 둘은 갈라지면 안 돼요.
        agent_ids = sorted({
            binding.agent_record_id
            for binding in snapshot.tool_bindings
        } | {
            identity.agent_record_id
            for identity in snapshot.identities
        })
        for agent_id in agent_ids:
            bindings = self.identity_store.list_agent_tool_bindings(agent_id)
            created_bindings = tuple(
                {
                    "asset_id": binding.asset_id,
                    "asset_version": binding.asset_version,
                    "operation_id": binding.operation_id,
                }
                for binding in bindings
                if binding.asset_id in purged_asset_ids
            )
            if created_bindings:
                self.identity_store.rollback_agent_provisioning(
                    agent_id,
                    created_bindings=created_bindings,
                    policy_revision=None,
                )

        # 삭제 성공 판단도 소비자 Query로 해요. snapshot 자체의 행을 지웠다고 가정하면
        # partition/SK drift가 있을 때 고아를 성공으로 기록하게 됩니다.
        remaining_snapshot = (
            self.identity_store.get_agent_authorization_ledger_snapshot()
        )
        remaining_agent_ids = sorted({
            binding.agent_record_id
            for binding in remaining_snapshot.tool_bindings
        } | {
            identity.agent_record_id
            for identity in remaining_snapshot.identities
        })
        queried_bindings = tuple(
            binding
            for agent_id in remaining_agent_ids
            for binding in self.identity_store.list_agent_tool_bindings(
                agent_id
            )
        )
        snapshot_keys = {
            (
                binding.agent_record_id,
                binding.asset_id,
                binding.asset_version,
                binding.operation_id,
            )
            for binding in remaining_snapshot.tool_bindings
        }
        queried_keys = {
            (
                binding.agent_record_id,
                binding.asset_id,
                binding.asset_version,
                binding.operation_id,
            )
            for binding in queried_bindings
        }
        if snapshot_keys != queried_keys:
            raise RuntimeError(
                "④ binding 원장의 전체 관측과 소비자 Query 결과가 달라요."
            )
        remaining = [
            binding
            for binding in queried_bindings
            if binding.asset_id in purged_asset_ids
        ]
        if remaining:
            coordinates = ", ".join(
                sorted({
                    f"{binding.agent_record_id}/"
                    f"{binding.asset_id}/"
                    f"{binding.operation_id}"
                    for binding in remaining
                })
            )
            raise RuntimeError(
                "MCP agent tool bindings remained after purge: "
                f"{coordinates}"
            )

    def _sibling_record_ids(self, record_id: str, asset_id: str | None) -> list[str]:
        """asset_id가 같은 모든 레코드 id를 모아요. 도출 실패면 자기 자신만."""
        if not asset_id:
            return [record_id]
        ids = []
        try:
            # statuses를 전 상태로 명시해요 — 기본값은 DEPRECATED를 빼서 soft-delete된
            # 버전이 누락되거든요(완전삭제는 그런 잔재까지 지워야 해요).
            for r in self.registry.list_records(
                self.registry_id, statuses=_ALL_STATUSES, max_results=1000):
                if _asset_id_from_record(r) == asset_id:
                    ids.append(r.record_id)
        except Exception:
            _log.exception("sibling 수집 실패 — 자기 자신만 삭제")
        if record_id not in ids:
            ids.append(record_id)
        return ids

    def _safe_get(self, record_id: str):
        try:
            return self.registry.get_record(self.registry_id, record_id)
        except Exception:
            return None
