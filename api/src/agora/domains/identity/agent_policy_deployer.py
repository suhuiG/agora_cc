"""컴파일된 Cedar policy를 AgentCore Policy Engine에 배포해요(compiler→Policy Engine 배선).

M1은 산출물을 원장(AgentPolicyDeployment)에만 남겼어요. M2는 그 Cedar를 실제 Policy
Engine에 create/update 하고, ACTIVE read-back + 정규화 hash 대조로 fail-closed 승격해요
(스펙 §4.3·§4.7·§5.1 step 8·§8.1). 컴파일러가 소유한 policy 집합이 canonical이에요.

AWS 클라이언트는 port(PolicyEngineClient)로 주입 — 단위 테스트는 fake만 써요.
"""
from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from typing import Protocol

from .agent_policy_compiler import CompiledSharedGatewayPolicies
from .agent_policy_cutover import (
    GatewayCallAttempt as GatewayCallAttempt,
    GatewayCallObservation as GatewayCallObservation,
    GatewayCallVerification as GatewayCallVerification,
    GatewayCallVerifier,
    GatewayPolicyCutover,
    GatewayPolicyCutoverManager,
    GatewayPolicyCutoverPhase as GatewayPolicyCutoverPhase,
    GatewaySharedPolicyDeployment,
    PolicyNameConflict,
)
from .cognito_client_names import MANAGED_AGENT_CLIENT_PREFIXES
from .models import (
    AgentPolicyDeployment,
    ApprovalState,
    DesiredState,
    EffectiveState,
    IdentityBindingStatus,
    IdentityType,
    PolicyDeploymentStatus,
)
from .store import IdentityRecordNotFound, IdentityStore

_log = logging.getLogger(__name__)

# AgentCore policy 이름 규칙: [A-Za-z][A-Za-z0-9_]*, 하이픈 불가, 1–48자.
_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_]")
_WS = re.compile(r"\s+")
# 배포된 policy `name` 끝의 revision suffix(`_r{N}`) — reconcile 소유 판별용.
_POLICY_REV = re.compile(r"_r(\d+)$")
_DEV_POLICY_NAME = re.compile(
    r"^Agent_dev_([0-9a-f]{24})_[0-9a-f]{8}_r([1-9]\d*)$"
)

# 옛 revision 회수가 실패했을 때 원장·UI 에 남기는 문장이에요. 세 호출부(동기 `deploy`,
# `AgentPolicyService._deploy`, dev identity 승격 루프)가 같은 문장을 써야 운영자가 화면에서
# 같은 사고를 하나로 알아봐요.
STALE_REVISION_CLEANUP_FAILED = (
    "stale revision cleanup failed (effective permissions NOT narrowed)"
)


def _aws_error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return ""
    error = response.get("Error")
    return str(error.get("Code", "")) if isinstance(error, dict) else ""


def _policy_items(response: object) -> tuple[dict, ...]:
    if not isinstance(response, dict) or "policies" not in response:
        raise ValueError("list_policies response is missing policies")
    policies = response["policies"]
    if not isinstance(policies, (list, tuple)) or not all(
        isinstance(policy, dict) for policy in policies
    ):
        raise ValueError("list_policies policies is not an object list")
    return tuple(policies)


class PolicyRevisionCleanupError(Exception):
    """이전 소유 revision 정책 삭제가 실패했어요(prune_stale fail-closed 신호)."""


class PolicyEngineClient(Protocol):
    def create_policy(
        self, *, policy_engine_id: str, name: str, definition: dict,
        validation_mode: str, description: str | None = None,
    ) -> dict: ...

    def get_policy(self, *, policy_engine_id: str, policy_id: str) -> dict: ...

    def update_policy(
        self, *, policy_engine_id: str, policy_id: str, definition: dict,
        validation_mode: str,
    ) -> dict: ...

    def list_policies(
        self, *, policy_engine_id: str, target_resource_scope: str | None = None,
    ) -> dict: ...

    def delete_policy(self, *, policy_engine_id: str, policy_id: str) -> None: ...

    def tag_resource(
        self, *, resource_arn: str, tags: dict[str, str]
    ) -> None: ...


@dataclass(frozen=True)
class ReconcileReport:
    in_sync: tuple[str, ...] = ()
    stale: tuple[str, ...] = ()
    drift: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    unmanaged: tuple[str, ...] = ()


@dataclass(frozen=True)
class PolicyInventoryItem:
    policy_id: str
    name: str
    remote_status: str
    agent_record_id: str | None
    ledger_revision: int | None
    ledger_status: str | None
    reason: str


@dataclass(frozen=True)
class PolicyReconciliationItem:
    agent_record_id: str
    catalog_record_present: bool | str
    in_sync: tuple[str, ...] = ()
    stale: tuple[str, ...] = ()
    drift: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentAuthorizationInventoryItem:
    record_id: str
    identity_type: str
    identity_status: str
    policy_principal_id: str
    client_id: str
    client_claim: str
    tool_count: int
    tool_effective_state_counts: dict[str, int]
    policy_count: int
    policy_status_counts: dict[str, int]
    cedar_observation: str = "unknown"
    cedar_in_sync: tuple[str, ...] = ()
    cedar_stale: tuple[str, ...] = ()
    cedar_drift: tuple[str, ...] = ()
    cedar_missing: tuple[str, ...] = ()
    client_exists: bool | str = "unknown"
    catalog_record: str = "unknown"
    classification: str = "unknown"
    authorization_verdict: str = "unknown"
    fail_closed_blocked: bool = True
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class CognitoClientInventoryItem:
    client_id: str
    name: str
    reason: str = "identity_missing"


@dataclass(frozen=True)
class AuthorizationReclaimCandidate:
    record_id: str
    client_id: str
    policy_ids: tuple[str, ...] = ()
    classification: str = "orphan"
    reason: str = "catalog_record_missing"


@dataclass(frozen=True)
class AuthorizationInventorySummary:
    identity_total: int | str = 0
    agents_without_tools_or_policies: int | str = 0
    missing_client_reference_count: int | str = 0
    orphan_client_count: int | str = 0
    tool_effective_state_counts: dict[str, int] | str = field(
        default_factory=dict
    )
    policy_status_counts: dict[str, int] | str = field(
        default_factory=dict
    )
    catalog_orphan_count: int | str = 0
    fail_closed_evaluated_agent_count: int | str = 0
    fail_closed_blocked_agent_count: int | str = 0
    not_applicable_agent_count: int | str = 0
    unknown_agent_count: int | str = 0
    gate_active_tool_count: int | str = "unknown"
    gate_evaluated_agent_count: int | str = "unknown"
    gate_unknown_agent_count: int | str = "unknown"

    @classmethod
    def unknown(cls) -> AuthorizationInventorySummary:
        return cls(
            identity_total="unknown",
            agents_without_tools_or_policies="unknown",
            missing_client_reference_count="unknown",
            orphan_client_count="unknown",
            tool_effective_state_counts="unknown",
            policy_status_counts="unknown",
            catalog_orphan_count="unknown",
            fail_closed_evaluated_agent_count="unknown",
            fail_closed_blocked_agent_count="unknown",
            not_applicable_agent_count="unknown",
            unknown_agent_count="unknown",
            gate_active_tool_count="unknown",
            gate_evaluated_agent_count="unknown",
            gate_unknown_agent_count="unknown",
        )


@dataclass(frozen=True)
class PolicyInventoryReport:
    status: str
    managed: tuple[PolicyInventoryItem, ...] = ()
    orphan: tuple[PolicyInventoryItem, ...] = ()
    unmanaged: tuple[PolicyInventoryItem, ...] = ()
    reconciliation: tuple[PolicyReconciliationItem, ...] = ()
    agents: tuple[AgentAuthorizationInventoryItem, ...] = ()
    orphan_clients: tuple[CognitoClientInventoryItem, ...] = ()
    reclaim_candidates: tuple[AuthorizationReclaimCandidate, ...] = ()
    summary: AuthorizationInventorySummary = field(
        default_factory=AuthorizationInventorySummary
    )
    shared_cutovers: tuple[GatewayPolicyCutover, ...] = ()
    shared_missing: tuple[str, ...] = ()
    reason: str = ""

    @classmethod
    def unknown(
        cls,
        reason: str,
        *,
        shared_cutovers: tuple[GatewayPolicyCutover, ...] = (),
    ) -> PolicyInventoryReport:
        return cls(
            status="unknown",
            summary=AuthorizationInventorySummary.unknown(),
            shared_cutovers=shared_cutovers,
            reason=reason,
        )


class AgentPolicyDeployer:
    def __init__(
        self, client: PolicyEngineClient, store: IdentityStore, *,
        engine_id: str, validation_mode: str = "FAIL_ON_ANY_FINDINGS",
        now=None, sleep=None, max_polls: int = 5,
        cognito_client=None, user_pool_id: str = "",
    ) -> None:
        self._client = client
        self._store = store
        self._engine_id = engine_id
        self._validation_mode = validation_mode
        self._cognito = cognito_client
        self._user_pool_id = user_pool_id
        import time
        self._now = now or (
            lambda: __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        self._sleep = sleep or time.sleep
        self._max_polls = max_polls
        self._gateway_cutover = GatewayPolicyCutoverManager(
            client,
            store,
            engine_id=engine_id,
            validation_mode=validation_mode,
            now=self._now,
            sleep=self._sleep,
            max_polls=max_polls,
        )

    @staticmethod
    def policy_name(agent_record_id: str, revision: int) -> str:
        # 전체 record ID 해시와 revision suffix를 먼저 확보해 긴 공통 접두어의 agent도
        # 서로 다른 policy 이름을 얻도록 해요(§4.7 canonical).
        slug = _NAME_UNSAFE.sub("_", agent_record_id)
        record_hash = hashlib.sha256(agent_record_id.encode("utf-8")).hexdigest()[:8]
        prefix, suffix = "Agent_", f"_{record_hash}_r{revision}"
        slug_budget = max(0, 48 - len(prefix) - len(suffix))
        return f"{prefix}{slug[:slug_budget]}{suffix}"

    @staticmethod
    def normalize_cedar(text: str) -> str:
        return _WS.sub(" ", text).strip()

    @staticmethod
    def shared_policy_name(
        gateway_arn: str,
        revision: int,
        policy_key: str,
    ) -> str:
        return GatewayPolicyCutoverManager.policy_name(
            gateway_arn,
            revision,
            policy_key,
        )

    def cut_over_shared_gateway(
        self,
        compiled: CompiledSharedGatewayPolicies,
        *,
        created_by: str,
        call_verifier: GatewayCallVerifier | None = None,
    ) -> GatewayPolicyCutover:
        return self._gateway_cutover.cut_over(
            compiled,
            created_by=created_by,
            call_verifier=call_verifier,
        )

    def observe_shared_gateway_cutover(
        self,
        cutover: GatewayPolicyCutover,
    ) -> bool:
        return self._gateway_cutover.observe(cutover)

    def deploy(
        self,
        deployment: AgentPolicyDeployment,
        *,
        description: str | None = None,
        tags: dict[str, str] | None = None,
        wait_polls: int | None = None,
    ) -> AgentPolicyDeployment:
        """Cedar policy를 배포해요.

        `wait_polls`는 동기 관측 예산이에요(생략하면 인스턴스 기본값). **재진입할 수 있는
        호출자만 짧은 기본값을 쓰세요.** 기본값(5회 = 약 8초)은 배포 job 폴러처럼 다음
        poll에서 이어받을 수 있는 호출자를 위한 값이라, 동기 HTTP 요청 경로에서 그대로
        쓰면 아직 `CREATING`인 정책을 실패로 오인해요(IH-75 실측: 코드 다운로드가 500).
        """
        name = self.policy_name(deployment.agent_record_id, deployment.revision)
        definition = {"cedar": {"statement": deployment.cedar_policy}}
        policy_id = deployment.agentcore_policy_id
        if not policy_id:
            try:
                create_kwargs = {
                    "policy_engine_id": self._engine_id,
                    "name": name,
                    "definition": definition,
                    "validation_mode": self._validation_mode,
                }
                if description is not None:
                    create_kwargs["description"] = description
                created = self._client.create_policy(
                    **create_kwargs,
                )
                policy_id = created["policyId"]
                if tags:
                    try:
                        policy_arn = str(created.get("policyArn") or "")
                        if not policy_arn:
                            raise RuntimeError(
                                "policy ARN is required to tag a dev identity policy"
                            )
                        self._client.tag_resource(
                            resource_arn=policy_arn, tags=tags
                        )
                    except Exception as exc:
                        if _aws_error_code(exc) == "AccessDeniedException":
                            _log.warning(
                                "TagResource best-effort skipped for policy %s: %s",
                                policy_id,
                                exc,
                            )
                        else:
                            self._rollback_created_policy(policy_id)
                            raise
            except PolicyNameConflict:
                # 같은 revision의 최초 create 재시도만 update로 수렴해요. policy ID를
                # checkpoint한 재진입은 위에서 create/update 없이 곧바로 poll해요.
                policy_id = self._find_policy_id(name)
                if not self._conflict_policy_owned_by_agent(
                    deployment.agent_record_id, policy_id
                ):
                    return self._fail(
                        deployment, ["principal mismatch on conflict"]
                    )
                self._client.update_policy(
                    policy_engine_id=self._engine_id, policy_id=policy_id,
                    definition=definition, validation_mode=self._validation_mode,
                )
        # ADR-0049: 원격 policy id 를 받은 **직후** 원장에 못박아요. 동기 관측이 끝난 뒤에
        # 저장하면 그 사이(최대 수 초) 프로세스가 죽었을 때 PENDING 원장에 id 가 없어
        # 재개도 못 하고 원격 policy 가 orphan 으로 남아요(적대적 리뷰 지적, 2026-08-21).
        self._pending(deployment, policy_id, "CREATING")
        status, statement, findings = self._poll(policy_id, max_polls=wait_polls)
        if findings or status in ("CREATE_FAILED", "UPDATE_FAILED"):
            # 확정 실패도 policy id 를 남겨야 보상이 원격 policy 를 지울 수 있어요.
            # 안 남기면 IH-68 이 막으려던 policy 누적이 그대로 발생해요.
            return self._fail(
                deployment,
                findings or [f"status={status}"],
                agentcore_policy_id=policy_id,
            )
        if status != "ACTIVE":
            return self._pending(deployment, policy_id, status)
        if self.normalize_cedar(statement) != self.normalize_cedar(
            deployment.cedar_policy
        ):
            return self._fail(
                deployment,
                ["read-back mismatch"],
                agentcore_policy_id=policy_id,
            )
        deployed_hash = hashlib.sha256(
            self.normalize_cedar(statement).encode("utf-8")
        ).hexdigest()
        # IA-33: 새 revision이 ACTIVE로 확정된 뒤에야 이전 소유 revision을 지워요. Cedar는
        # ACTIVE permit을 합집합하므로, 옛 `_r{<N}`이 남으면 유효 권한이 최광의 union으로 굳어
        # 하향/회수가 반영되지 않아요. 삭제 실패는 fail-closed — 넓은 권한이 남은 채 "성공"으로
        # 보고하지 않고 FAILED로 귀결해요(ADR-0028).
        cleanup_errors = self._delete_prior_owned_revisions(
            deployment.agent_record_id,
            current_revision=deployment.revision,
            gateway_arn=deployment.gateway_id,
            keep_policy_id=policy_id,
        )
        if cleanup_errors:
            _log.error(
                "IA-33 이전 revision 정리 실패(agent=%s, r%s): %s",
                deployment.agent_record_id, deployment.revision,
                "; ".join(cleanup_errors),
            )
            return self._fail(
                deployment,
                [STALE_REVISION_CLEANUP_FAILED, *cleanup_errors],
                agentcore_policy_id=policy_id,
            )
        return self._store.mark_agent_policy_deployment(
            deployment.agent_record_id, deployment.revision,
            status=PolicyDeploymentStatus.ACTIVE,
            agentcore_policy_id=policy_id,
            deployed_policy_hash=deployed_hash,
            deployed_at=self._now(),
            validation_findings=(),
        )

    def delete(self, deployment: AgentPolicyDeployment) -> None:
        """보상 시 이 revision의 실제 Policy Engine policy를 제거해요."""
        if not deployment.agentcore_policy_id:
            return
        self._client.delete_policy(
            policy_engine_id=self._engine_id,
            policy_id=deployment.agentcore_policy_id,
        )

    def _rollback_created_policy(self, policy_id: str) -> None:
        """Wait for create to settle, then delete without masking the trigger."""
        try:
            self._poll(policy_id)
            self._client.delete_policy(
                policy_engine_id=self._engine_id,
                policy_id=policy_id,
            )
        except Exception:
            _log.exception(
                "policy rollback failed for %s; preserving original error",
                policy_id,
            )

    def _fail(
        self, deployment, findings, *, agentcore_policy_id: str = ""
    ) -> AgentPolicyDeployment:
        # 이전 effective policy는 그대로 두고 이 revision만 FAILED (§8.1). 정리 실패로
        # FAILED가 될 때는 새로 만든 policy id를 기록해 teardown이 orphan을 지울 수 있게 해요.
        return self._store.mark_agent_policy_deployment(
            deployment.agent_record_id, deployment.revision,
            status=PolicyDeploymentStatus.FAILED,
            agentcore_policy_id=agentcore_policy_id,
            validation_findings=tuple(findings),
        )

    def _pending(
        self, deployment, policy_id: str, status: str
    ) -> AgentPolicyDeployment:
        return self._store.mark_agent_policy_deployment(
            deployment.agent_record_id,
            deployment.revision,
            status=PolicyDeploymentStatus.PENDING,
            agentcore_policy_id=policy_id,
            validation_findings=(f"status={status or 'UNKNOWN'}",),
        )

    def observe(self, deployment: AgentPolicyDeployment) -> AgentPolicyDeployment:
        """원격 policy 상태를 **한 번만 읽어** 판정한 사본을 돌려줘요(원장 쓰기 없음).

        IH-78 의 비동기 발급 경로가 PENDING 원장을 승격할 때 써요. 여기서 쓰기를 하지 않는
        이유는 승격 주체(호출자)가 원장 갱신을 소유해야 판정과 기록이 한 곳에 모이기
        때문이에요. 원격 policy 를 만들거나 지우지 않으므로 비파괴예요.

        `agentcore_policy_id` 가 없으면 관측 대상이 없다는 뜻이라 그대로 돌려줘요 —
        "관측 못 함" 을 통과나 실패로 바꾸지 않아요(ADR-0037 §4).
        """
        policy_id = deployment.agentcore_policy_id
        if not policy_id:
            return deployment
        status, statement, findings = self._poll(policy_id, max_polls=1)
        if findings or status in ("CREATE_FAILED", "UPDATE_FAILED"):
            return replace(
                deployment,
                status=PolicyDeploymentStatus.FAILED,
                validation_findings=tuple(findings or [f"status={status}"]),
            )
        if status != "ACTIVE":
            return replace(
                deployment,
                status=PolicyDeploymentStatus.PENDING,
                validation_findings=(f"status={status or 'UNKNOWN'}",),
            )
        if self.normalize_cedar(statement) != self.normalize_cedar(
            deployment.cedar_policy
        ):
            return replace(
                deployment,
                status=PolicyDeploymentStatus.FAILED,
                validation_findings=("read-back mismatch",),
            )
        deployed_hash = hashlib.sha256(
            self.normalize_cedar(statement).encode("utf-8")
        ).hexdigest()
        return replace(
            deployment,
            status=PolicyDeploymentStatus.ACTIVE,
            deployed_policy_hash=deployed_hash,
            deployed_at=self._now(),
            validation_findings=(),
        )

    def reclaim_superseded_revisions(
        self, agent_record_id: str, *, current_revision: int,
        gateway_arn: str, keep_policy_id: str,
    ) -> tuple[str, ...]:
        """`current_revision` 미만의 이 agent 소유 revision 정책을 회수해요.

        ## 왜 공개 진입점이 필요한가

        `deploy()` 는 **동기 관측이 ACTIVE 로 끝났을 때만** 이 정리에 도달해요(`:412`). 그런데
        dev identity 발급은 활성화를 기다리지 않고 `wait_polls=1` 로 create 만 하고 PENDING
        으로 돌아와요(IH-78 — Cedar 활성화 실측 8.5~72.6초). 그래서 그 경로는 정리 지점을
        **한 번도 지나가지 않고**, ACTIVE 를 나중에 관측하는 주체(`reconcile_pending_policies`)
        가 같은 회수를 할 방법이 없었어요.

        라이브 실측이 그 결과예요 — 같은 정책의 `_r1` 과 `_r2` 가 **둘 다 ACTIVE** 였어요.
        Cedar ACTIVE permit 은 합집합이라 좁힌 r2 를 올려도 r1 이 계속 허용해요 — 회수가
        무효예요.

        ## 순서 (뒤집지 마세요)

        새로 만들고 → **ACTIVE 로 관측한 뒤** → 옛 것 삭제예요
        (`shared_policy_provisioner.provision` 과 같은 순서). 먼저 지우면 새 정책이 실패했을 때
        그 agent 가 권한 0 이 되는 창이 열려요. 관측 못 한 상태(UNKNOWN)에서도 지우면 안 돼요 —
        새 정책이 실제로 살았는지 모르니까요(ADR-0037 §4).

        빈 tuple 이면 회수 완료(또는 회수할 것이 없음)예요. 비어 있지 않으면 **옛 permit 이
        남아 있을 수 있다**는 뜻이니 호출자가 실패로 다뤄야 해요. 조용히 넘기면 원장이
        "좁혔다" 고 거짓말해요.

        멱등이에요 — 이미 사라진 revision 은 live 목록에 없어 다시 지우지 않아요.
        """
        return tuple(self._delete_prior_owned_revisions(
            agent_record_id,
            current_revision=current_revision,
            gateway_arn=gateway_arn,
            keep_policy_id=keep_policy_id,
        ))

    def _delete_prior_owned_revisions(
        self, agent_record_id: str, *, current_revision: int,
        gateway_arn: str, keep_policy_id: str,
    ) -> list[str]:
        """canonical(현재) 미만인 이 agent 소유 revision 정책을 Policy Engine에서 지워요.

        소유 증거는 원장에 checkpoint된 exact `agentcore_policy_id`뿐이에요. 파생 이름이
        같아도 ID가 다르면 unmanaged로 남기고 삭제하지 않아요(ADR-0067). 삭제 실패는
        예외로 올리지 않고 사유 문자열 목록으로 반환해 나머지 revision까지 계속 시도하고,
        호출부가 fail-closed 판정을 하게 해요. 멱등: 이미 사라진 revision은 list 결과에
        없어 재삭제하지 않아요.
        """
        errors: list[str] = []
        deleted: list[int] = []
        try:
            ledger = self._store.list_agent_policy_deployments(
                agent_record_id
            )
        except Exception as exc:  # noqa: BLE001 - 소유 원장 미관측은 삭제 금지
            return [
                "policy ownership unknown: "
                f"{type(exc).__name__}: {exc}"
            ]
        owned_by_policy_id: dict[str, list[int]] = {}
        for item in ledger:
            if (
                item.revision < current_revision
                and item.agentcore_policy_id
            ):
                owned_by_policy_id.setdefault(
                    item.agentcore_policy_id,
                    [],
                ).append(item.revision)
        ambiguous = {
            policy_id
            for policy_id, revisions in owned_by_policy_id.items()
            if len(revisions) != 1
        }
        for policy_id in sorted(ambiguous):
            errors.append(
                "policy ownership unknown: ledger policyId "
                f"{policy_id} belongs to revisions "
                f"{sorted(owned_by_policy_id[policy_id])}"
            )

        try:
            resp = self._client.list_policies(
                policy_engine_id=self._engine_id,
                target_resource_scope=gateway_arn,
            )
        except Exception as exc:  # noqa: BLE001 - 불완전 조회는 ACTIVE 승격 금지
            return [
                "policy listing incomplete: "
                f"{type(exc).__name__}: {exc}"
            ]
        if resp.get("nextToken"):
            return [
                "policy listing incomplete: unconsumed nextToken returned"
            ]
        candidates: dict[str, int] = {}
        for policy in resp.get("policies", []):
            scope = policy.get("targetResourceScope", gateway_arn)
            if scope != gateway_arn:
                continue
            pid = str(policy.get("policyId") or "")
            if not pid:
                errors.append(
                    "policy ownership unknown: live policyId is missing"
                )
                continue
            if pid == keep_policy_id:
                continue
            revisions = owned_by_policy_id.get(pid, [])
            if len(revisions) == 1:
                candidates[pid] = revisions[0]
                continue
            derived_revision = self._owned_revision(
                agent_record_id,
                str(policy.get("name") or ""),
            )
            if derived_revision is not None:
                errors.append(
                    "unmanaged policy not deleted: "
                    f"{pid} has generated name for r{derived_revision} "
                    "but no matching ledger policyId"
                )

        requested: dict[str, int] = {}
        for pid, revision in candidates.items():
            try:
                self._client.delete_policy(
                    policy_engine_id=self._engine_id, policy_id=pid
                )
                requested[pid] = revision
            except Exception as exc:  # noqa: BLE001 - 남은 revision도 계속 시도
                errors.append(
                    f"r{revision} ({pid}): {type(exc).__name__}: {exc}"
                )

        confirmed_absent: set[str] = set()
        if requested:
            confirmed_absent, confirmation_errors = (
                self._confirm_policy_absence(
                    set(requested),
                    gateway_arn=gateway_arn,
                )
            )
            errors.extend(confirmation_errors)
        for pid in sorted(confirmed_absent):
            revision = requested[pid]
            try:
                self._store.mark_agent_policy_deployment(
                    agent_record_id,
                    revision,
                    status=PolicyDeploymentStatus.SUPERSEDED,
                )
                deleted.append(revision)
            except Exception as exc:  # noqa: BLE001 - 원장 불일치도 성공으로 접지 않아요.
                errors.append(
                    f"r{revision} ({pid}) ledger update: "
                    f"{type(exc).__name__}: {exc}"
                )
        if deleted:
            _log.info(
                "IA-33 agent=%s r%s 배포 후 이전 소유 revision 정리: %s",
                agent_record_id, current_revision, sorted(deleted),
            )
        return errors

    def _confirm_policy_absence(
        self,
        policy_ids: set[str],
        *,
        gateway_arn: str,
    ) -> tuple[set[str], list[str]]:
        """삭제 요청한 policy가 완전한 live inventory에서 사라졌는지 확인해요."""
        remaining = set(policy_ids)
        for attempt in range(max(1, self._max_polls)):
            try:
                response = self._client.list_policies(
                    policy_engine_id=self._engine_id,
                    target_resource_scope=gateway_arn,
                )
            except Exception as exc:  # noqa: BLE001 - 부재 관측 실패는 unknown이에요.
                return set(), [
                    "policy deletion observation incomplete: "
                    f"{type(exc).__name__}: {exc}"
                ]
            if response.get("nextToken"):
                return set(), [
                    "policy deletion observation incomplete: "
                    "unconsumed nextToken returned"
                ]
            remaining = {
                str(policy.get("policyId") or "")
                for policy in response.get("policies", [])
                if policy.get("targetResourceScope", gateway_arn)
                == gateway_arn
                and str(policy.get("policyId") or "") in policy_ids
            }
            if not remaining:
                return set(policy_ids), []
            if attempt + 1 < max(1, self._max_polls):
                self._sleep(2)
        return set(policy_ids) - remaining, [
            f"policy deletion unconfirmed: {sorted(remaining)}"
        ]

    def prune_stale(
        self, agent_record_id: str, *, gateway_arn: str
    ) -> tuple[str, ...]:
        """reconcile()가 stale로 분류한 이 agent 소유 옛 revision 정책을 실제로 삭제해요.

        재컴파일 없이도 canonical 미만 소유 revision을 수렴시키는 정리 진입점이에요
        (reconcile은 리포트만 하므로). 소유·canonical 판별은 reconcile을 그대로 재사용하니
        다른 agent·unmanaged 정책은 대상이 아니에요. 부분 삭제가 조용히 성공처럼 보이지
        않도록, 하나라도 실패하면 성공분을 담아 예외를 올려요(fail-closed).
        """
        report = self.reconcile(agent_record_id, gateway_arn=gateway_arn)
        deleted: list[str] = []
        errors: list[str] = []
        for pid in report.stale:
            try:
                self._client.delete_policy(
                    policy_engine_id=self._engine_id, policy_id=pid
                )
                deleted.append(pid)
            except Exception as exc:  # noqa: BLE001 - 나머지도 계속 시도 후 집계 보고
                errors.append(f"{pid}: {type(exc).__name__}: {exc}")
        if deleted:
            _log.info(
                "IA-33 prune_stale agent=%s 정리한 stale revision: %s",
                agent_record_id, deleted,
            )
        if errors:
            raise PolicyRevisionCleanupError(
                f"deleted={deleted}; failed={'; '.join(errors)}"
            )
        return tuple(deleted)

    def delete_unmanaged_policies(
        self,
        policy_ids: tuple[str, ...] | list[str],
        *,
        catalog_record_ids: set[str],
        gateway_arn: str,
    ) -> tuple[str, ...]:
        """원장이 소유를 모르는(unmanaged) 정책만 명시 요청으로 삭제해요.

        `unmanaged` 는 원장에 소유자가 없어 자동 회수 대상이 아니에요(ADR-0067). 그래서
        이 진입점은 **호출자가 policy_id 를 명시**해야 하고, 삭제 직전에 fresh snapshot 을
        다시 떠서 그 id 가 여전히 `unmanaged` 인지 확인해요. 그 사이 원장이 소유를 주장하게
        됐거나(managed·orphan) snapshot 이 `unknown` 이면 아무것도 지우지 않아요(fail-closed).
        """
        requested = tuple(dict.fromkeys(str(pid) for pid in policy_ids if pid))
        if not requested:
            return ()
        report = self.inventory(
            catalog_record_ids=catalog_record_ids, gateway_arn=gateway_arn
        )
        if report.status != "observed":
            raise PolicyRevisionCleanupError(
                f"inventory unobservable; refusing unmanaged delete: {report.reason}"
            )
        unmanaged_ids = {item.policy_id for item in report.unmanaged}
        claimed = [pid for pid in requested if pid not in unmanaged_ids]
        if claimed:
            raise PolicyRevisionCleanupError(
                "refusing unmanaged delete; not unmanaged in fresh snapshot: "
                f"{sorted(claimed)}"
            )
        deleted: list[str] = []
        errors: list[str] = []
        for pid in requested:
            try:
                self._client.delete_policy(
                    policy_engine_id=self._engine_id, policy_id=pid
                )
                deleted.append(pid)
            except Exception as exc:  # noqa: BLE001 - 나머지도 시도하고 집계 보고
                errors.append(f"{pid}: {type(exc).__name__}: {exc}")
        if deleted:
            _log.info("IH-81 unmanaged 정책 명시 삭제: %s", deleted)
        if errors:
            raise PolicyRevisionCleanupError(
                f"deleted={deleted}; failed={'; '.join(errors)}"
            )
        return tuple(deleted)

    def prune_orphan_dev_policies(
        self,
        *,
        existing_credential_ids: set[str],
        gateway_arn: str,
    ) -> tuple[str, ...]:
        """Delete owned dev policies whose credential record no longer exists."""
        response = self._client.list_policies(
            policy_engine_id=self._engine_id,
            target_resource_scope=gateway_arn,
        )
        deleted: list[str] = []
        errors: list[str] = []
        terminal = {"ACTIVE", "CREATE_FAILED", "UPDATE_FAILED"}
        for policy in response.get("policies", []):
            if policy.get("targetResourceScope", gateway_arn) != gateway_arn:
                continue
            policy_id = str(policy.get("policyId") or "")
            name = str(policy.get("name") or "")
            match = _DEV_POLICY_NAME.fullmatch(name)
            if not policy_id or match is None:
                continue
            credential_id, revision_text = match.groups()
            revision = int(revision_text)
            if (
                credential_id in existing_credential_ids
                or self.policy_name(f"dev-{credential_id}", revision) != name
            ):
                continue
            try:
                status = str(policy.get("status") or "")
                if status not in terminal:
                    status, _, _ = self._poll(policy_id)
                if status not in terminal:
                    raise RuntimeError(f"policy status did not settle: {status}")
                self._client.delete_policy(
                    policy_engine_id=self._engine_id,
                    policy_id=policy_id,
                )
                deleted.append(policy_id)
            except Exception as exc:  # noqa: BLE001 - continue cleaning siblings
                errors.append(
                    f"{policy_id}: {type(exc).__name__}: {exc}"
                )
        if deleted:
            _log.warning("removed orphan dev identity policies: %s", deleted)
        if errors:
            raise PolicyRevisionCleanupError(
                f"deleted={deleted}; failed={'; '.join(errors)}"
            )
        return tuple(deleted)

    def _poll(self, policy_id: str, *, max_polls: int | None = None):
        budget = self._max_polls if max_polls is None else max(1, max_polls)
        status, statement, findings = "", "", []
        for attempt in range(budget):
            resp = self._client.get_policy(
                policy_engine_id=self._engine_id, policy_id=policy_id
            )
            policy = resp.get("policy", resp)
            status = policy.get("status", "")
            statement = (
                policy.get("definition", {}).get("cedar", {}).get("statement", "")
            )
            findings = list(policy.get("statusReasons", []) or [])
            if status in ("ACTIVE", "CREATE_FAILED", "UPDATE_FAILED"):
                break
            if attempt + 1 < budget:
                self._sleep(2)
        return status, statement, findings

    def _find_policy_id(self, name: str) -> str:
        # policyId는 opaque라 startswith로 못 찾아요 — 파생 policy_name과 name 속성을 동등 비교.
        # 실 list_policies 반환 스키마(name 필드 존재)는 T8 E2E서 확정.
        resp = self._client.list_policies(policy_engine_id=self._engine_id)
        for policy in resp.get("policies", []):
            if policy.get("name") == name:
                return policy.get("policyId", "")
        raise PolicyNameConflict(f"policy {name} not found on conflict fallback")

    def _conflict_policy_owned_by_agent(
        self, agent_record_id: str, policy_id: str
    ) -> bool:
        try:
            principal_id = self._store.get_agent_identity_binding(
                agent_record_id
            ).policy_principal_id
        except IdentityRecordNotFound:
            return False
        response = self._client.get_policy(
            policy_engine_id=self._engine_id, policy_id=policy_id
        )
        policy = response.get("policy", response)
        statement = (
            policy.get("definition", {}).get("cedar", {}).get("statement", "")
        )
        return f'AgentCore::OAuthUser::"{principal_id}"' in statement

    def _canonical_deployment(
        self,
        agent_record_id: str,
        deployments: tuple[AgentPolicyDeployment, ...] | None = None,
    ) -> AgentPolicyDeployment | None:
        # canonical = 상태가 ACTIVE인 **최신** deployment. latest(max revision)가 FAILED여도
        # 그 아래 ACTIVE revision이 실효(effective)로 살아있으면 그게 canonical이에요
        # (§4.7·§8.1 — 실패한 새 revision은 이전 effective를 대체하지 않음). store엔 "최신
        # ACTIVE" 전용 조회가 없어 전체 revision을 훑어 ACTIVE 중 최댓값을 골라요.
        active = [
            d
            for d in (
                deployments
                if deployments is not None
                else self._store.list_agent_policy_deployments(agent_record_id)
            )
            if d.status is PolicyDeploymentStatus.ACTIVE
        ]
        return max(active, key=lambda d: d.revision) if active else None

    def _owned_revision(self, agent_record_id: str, name: str) -> int | None:
        # 소유 판별은 policyId가 아니라 list_policies의 `name` 필드로 해요(_find_policy_id와
        # 동일) — 실 엔진의 policyId는 opaque(`pol-…`)라 접두 매칭이 불가하거든요. name 끝의
        # _r{N}에서 revision을 뽑아 policy_name으로 재구성한 뒤 exact 비교해요. policy_name과
        # 같은 파생(48자 절단 포함)을 쓰므로 긴 record_id로 절단된 배포명·다른 agent 접두
        # 충돌에 무관하게 이 agent 소유만 True예요. 소유가 아니면 None.
        match = _POLICY_REV.search(name)
        if match is None:
            return None
        revision = int(match.group(1))
        return revision if self.policy_name(agent_record_id, revision) == name else None

    def reconcile(
        self,
        agent_record_id: str,
        *,
        gateway_arn: str,
        observed_policies: tuple[dict, ...] | None = None,
        observed_deployments: tuple[AgentPolicyDeployment, ...] | None = None,
    ) -> ReconcileReport:
        all_deployments = (
            observed_deployments
            if observed_deployments is not None
            else tuple(
                self._store.list_all_agent_policy_deployments()
            )
        )
        deployments = tuple(
            deployment
            for deployment in all_deployments
            if deployment.agent_record_id == agent_record_id
        )
        checkpointed_policy_ids = {
            deployment.agentcore_policy_id
            for deployment in all_deployments
            if deployment.agentcore_policy_id
        }
        deployments_by_revision = {
            deployment.revision: deployment for deployment in deployments
        }
        canonical = self._canonical_deployment(agent_record_id, deployments)
        canonical_revision = canonical.revision if canonical else None
        canonical_name = (
            self.policy_name(agent_record_id, canonical.revision)
            if canonical
            else None
        )
        resp = (
            {"policies": observed_policies}
            if observed_policies is not None
            else self._client.list_policies(
                policy_engine_id=self._engine_id,
                target_resource_scope=gateway_arn,
            )
        )
        in_sync, stale, drift, unmanaged = [], [], [], []
        found_canonical = False
        for policy in resp.get("policies", []):
            scope = policy.get("targetResourceScope", gateway_arn)
            if scope != gateway_arn:
                continue
            pid = policy.get("policyId", "")
            revision = self._owned_revision(agent_record_id, policy.get("name", ""))
            if revision is None:
                if pid in checkpointed_policy_ids:
                    # 같은 Gateway의 다른 agent가 원장에 checkpoint한 정책은 이
                    # agent의 unmanaged 정책이 아니에요.
                    continue
                # 컴파일러가 소유하지 않은 policy — 권한상승 후보(§4.7·§8.2-13).
                unmanaged.append(pid)
                continue
            deployment = deployments_by_revision.get(revision)
            if deployment is None:
                # 생성 이름은 소유 증거가 아니에요. 해당 revision의 원장 행이 있어야
                # 이 agent가 관리하는 정책으로 볼 수 있어요(ADR-0067).
                unmanaged.append(pid)
                continue
            if (
                not deployment.agentcore_policy_id
                or deployment.agentcore_policy_id != pid
            ):
                # 원장에 checkpoint된 실 policyId만 소유 증거예요. 같은 파생 이름으로
                # 다시 만들어진 다른 정책을 이 agent의 revision으로 승격하지 않아요.
                unmanaged.append(pid)
                continue
            if deployment.status is not PolicyDeploymentStatus.ACTIVE:
                # PENDING/FAILED/SUPERSEDED 원장은 effective revision이 아니므로 같은
                # definition이어도 in_sync로 승격하지 않아요.
                stale.append(pid)
                continue
            if revision == canonical_revision:
                found_canonical = True
                statement = (
                    policy.get("definition", {})
                    .get("cedar", {}).get("statement", "")
                )
                remote_active = str(policy.get("status") or "") == "ACTIVE"
                same_definition = self.normalize_cedar(
                    statement
                ) == self.normalize_cedar(canonical.cedar_policy)
                (
                    in_sync
                    if remote_active and same_definition
                    else drift
                ).append(pid)
            else:
                # 컴파일러 소유지만 현재 canonical revision이 아님 → stale.
                stale.append(pid)
        missing = (
            (canonical_name,) if canonical_name and not found_canonical else ()
        )
        return ReconcileReport(
            in_sync=tuple(in_sync), stale=tuple(stale), drift=tuple(drift),
            missing=missing, unmanaged=tuple(unmanaged),
        )

    def inventory(
        self,
        *,
        catalog_record_ids: set[str] | None,
        gateway_arn: str,
        authorization_target_agent_ids: set[str] | None = None,
        catalog_record_statuses: dict[str, str] | None = None,
        catalog_observation_error: str = "",
        observed_cognito_clients: tuple[dict, ...] | None = None,
    ) -> PolicyInventoryReport:
        """한 관측에서 identity/Cognito/Catalog/Cedar inventory를 만들어요."""
        try:
            ledger = self._store.get_agent_authorization_ledger_snapshot()
        except Exception as exc:  # noqa: BLE001 - 관측 실패는 빈 inventory가 아니에요.
            return PolicyInventoryReport.unknown(
                f"identity ledger unobservable: {type(exc).__name__}: {exc}"
            )
        catalog_observed = catalog_record_ids is not None
        catalog_ids = catalog_record_ids or set()
        observation_errors: list[str] = []
        if not catalog_observed:
            observation_errors.append(
                "catalog records unobservable"
                + (
                    f": {catalog_observation_error}"
                    if catalog_observation_error
                    else ""
                )
            )
        list_shared = getattr(
            self._store,
            "list_gateway_policy_cutovers",
            None,
        )
        try:
            shared_cutovers = (
                tuple(list_shared(gateway_arn))
                if list_shared is not None
                else ()
            )
        except Exception as exc:  # noqa: BLE001 - 관측 불가는 빈 원장이 아니에요.
            shared_cutovers = ()
            observation_errors.append(
                "shared policy ledger unobservable: "
                f"{type(exc).__name__}: {exc}"
            )
        policy_observed = True
        try:
            response = self._client.list_policies(
                policy_engine_id=self._engine_id,
                target_resource_scope=gateway_arn,
            )
            # 계측 자체를 먼저 검증해요 — boto3는 `policies` 키를 돌려주므로 잘못 읽으면
            # 조용히 "0건"이 돼요(AGENTS.md 측정 도구 검증 규칙).
            policies = _policy_items(response)
        except Exception as exc:  # noqa: BLE001
            response = {"policies": ()}
            policy_observed = False
            observation_errors.append(
                f"policy engine unobservable: {type(exc).__name__}: {exc}"
            )
        if response.get("nextToken"):
            response = {"policies": ()}
            policy_observed = False
            observation_errors.append(
                "policy engine listing incomplete: unconsumed nextToken returned"
            )
        cognito_observed = True
        try:
            cognito_clients = (
                observed_cognito_clients
                if observed_cognito_clients is not None
                else self._list_cognito_clients()
            )
        except Exception as exc:  # noqa: BLE001
            cognito_clients = ()
            cognito_observed = False
            observation_errors.append(
                f"cognito clients unobservable: {type(exc).__name__}: {exc}"
            )

        policies = _policy_items(response) if policy_observed else ()
        deployments = ledger.policy_deployments
        deployments_by_agent: dict[
            str, list[AgentPolicyDeployment]
        ] = defaultdict(list)
        for deployment in deployments:
            deployments_by_agent[deployment.agent_record_id].append(
                deployment
            )
        by_policy_id: dict[str, list[AgentPolicyDeployment]] = {}
        for deployment in deployments:
            if deployment.agentcore_policy_id:
                by_policy_id.setdefault(
                    deployment.agentcore_policy_id, []
                ).append(deployment)
        shared_by_policy_id: dict[
            str,
            list[tuple[GatewayPolicyCutover, GatewaySharedPolicyDeployment]],
        ] = {}
        for cutover in shared_cutovers:
            for shared_policy in cutover.policies:
                if shared_policy.agentcore_policy_id:
                    shared_by_policy_id.setdefault(
                        shared_policy.agentcore_policy_id,
                        [],
                    ).append((cutover, shared_policy))
        ambiguous_shared = sorted(
            policy_id
            for policy_id, claims in shared_by_policy_id.items()
            if len(claims) != 1
        )
        if ambiguous_shared:
            policy_observed = False
            observation_errors.append(
                "ambiguous shared ledger ownership for policies "
                f"{ambiguous_shared}"
            )
        if policy_observed:
            for policy in policies:
                if (
                    policy.get("targetResourceScope", gateway_arn)
                    != gateway_arn
                ):
                    continue
                policy_id = str(policy.get("policyId") or "")
                if not policy_id:
                    policy_observed = False
                    observation_errors.append(
                        "policy engine item identity unobservable: "
                        "policyId missing"
                    )
                    break
                matches = {
                    (item.agent_record_id, item.revision)
                    for item in by_policy_id.get(policy_id, ())
                }
                if len(matches) > 1:
                    policy_observed = False
                    observation_errors.append(
                        f"ambiguous ledger ownership for policy {policy_id}"
                    )
                    break
                if matches and shared_by_policy_id.get(policy_id):
                    # 같은 policyId를 agent 원장과 공유 원장이 동시에 주장하면 소유를
                    # 결정할 수 없어요 — fail-closed로 관측 불가로 올려요.
                    policy_observed = False
                    observation_errors.append(
                        "ambiguous agent/shared ledger ownership for policy "
                        f"{policy_id}"
                    )
                    break
        if not policy_observed:
            policies = ()
        # 공유 정책이 live에서 사라졌는지는 policy engine을 실제로 관측했을 때만
        # 판단할 수 있어요. 관측 불가를 "없어졌다"로 보고하지 않아요.
        live_policy_ids = {
            str(policy.get("policyId") or "")
            for policy in policies
            if policy.get("targetResourceScope", gateway_arn) == gateway_arn
        }
        latest_shared = (
            max(shared_cutovers, key=lambda item: item.revision)
            if shared_cutovers
            else None
        )
        expected_shared_ids = {
            shared_policy.agentcore_policy_id
            for shared_policy in (
                latest_shared.policies if latest_shared is not None else ()
            )
            if shared_policy.agentcore_policy_id
            and shared_policy.remote_status == "ACTIVE"
        }
        shared_missing = (
            tuple(sorted(expected_shared_ids - live_policy_ids))
            if policy_observed
            else ()
        )
        observed_agent_policies = tuple(
            policy
            for policy in policies
            if str(policy.get("policyId") or "") not in shared_by_policy_id
        )
        agent_record_ids = sorted({
            deployment.agent_record_id for deployment in deployments
        })
        if policy_observed:
            try:
                reconciliation = tuple(
                    self._reconciliation_item(
                        agent_record_id,
                        catalog_record_ids=catalog_ids,
                        catalog_observed=catalog_observed,
                        gateway_arn=gateway_arn,
                        observed_policies=observed_agent_policies,
                        observed_deployments=deployments,
                    )
                    for agent_record_id in agent_record_ids
                )
            except Exception as exc:  # noqa: BLE001
                reconciliation = ()
                policy_observed = False
                policies = ()
                observation_errors.append(
                    f"policy reconciliation unobservable: "
                    f"{type(exc).__name__}: {exc}"
                )
        else:
            reconciliation = ()

        managed: list[PolicyInventoryItem] = []
        orphan: list[PolicyInventoryItem] = []
        unmanaged: list[PolicyInventoryItem] = []
        for policy in policies:
            if policy.get("targetResourceScope", gateway_arn) != gateway_arn:
                continue
            policy_id = str(policy.get("policyId") or "")
            name = str(policy.get("name") or "")
            matches = {
                (item.agent_record_id, item.revision): item
                for item in by_policy_id.get(policy_id, ())
            }
            # policyId 부재·agent/shared 원장 이중 소유·중복 소유는 위 사전 검사에서
            # 이미 관측 오류로 누적돼 policies가 비워지므로 여기 도달하지 않아요.
            shared_matches = shared_by_policy_id.get(policy_id, ())
            remote_status = str(policy.get("status") or "UNKNOWN")
            if shared_matches:
                cutover, shared = shared_matches[0]
                try:
                    readback = self._client.get_policy(
                        policy_engine_id=self._engine_id,
                        policy_id=policy_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    observation_errors.append(
                        "shared policy readback unobservable for "
                        f"{policy_id}: {type(exc).__name__}: {exc}"
                    )
                    continue
                else:
                    remote = readback.get("policy", readback)
                    remote_status = str(
                        remote.get("status") or remote_status
                    )
                    statement = (
                        remote.get("definition", {})
                        .get("cedar", {})
                        .get("statement", "")
                    )
                    if not statement:
                        observation_errors.append(
                            "shared policy statement is unavailable for "
                            f"{policy_id}"
                        )
                        continue
                    elif self.normalize_cedar(
                        statement
                    ) == self.normalize_cedar(shared.cedar_policy):
                        sync = "in_sync"
                    else:
                        sync = "drift"
                managed.append(PolicyInventoryItem(
                    policy_id=policy_id,
                    name=name,
                    remote_status=remote_status,
                    agent_record_id=None,
                    ledger_revision=cutover.revision,
                    ledger_status=(
                        f"{cutover.phase.value}/{shared.remote_status}"
                    ),
                    reason=(
                        f"gateway_shared_policy:{shared.policy_key}:{sync}"
                    ),
                ))
                continue
            if not matches:
                unmanaged.append(PolicyInventoryItem(
                    policy_id=policy_id,
                    name=name,
                    remote_status=remote_status,
                    agent_record_id=None,
                    ledger_revision=None,
                    ledger_status=None,
                    reason="ledger_policy_missing",
                ))
                continue
            deployment = next(iter(matches.values()))
            if not catalog_observed:
                continue
            item = PolicyInventoryItem(
                policy_id=policy_id,
                name=name,
                remote_status=remote_status,
                agent_record_id=deployment.agent_record_id,
                ledger_revision=deployment.revision,
                ledger_status=deployment.status.value,
                reason=(
                    "catalog_record_present"
                    if deployment.agent_record_id in catalog_ids
                    else "catalog_record_missing"
                ),
            )
            (
                managed
                if deployment.agent_record_id in catalog_ids
                else orphan
            ).append(item)

        def _policy_id(item: PolicyInventoryItem) -> str:
            return item.policy_id

        reconciliation_by_agent = {
            item.agent_record_id: item for item in reconciliation
        }
        tools_by_agent = self._group_by_agent(ledger.tool_bindings)
        policies_by_agent = self._group_by_agent(
            ledger.policy_deployments
        )
        claims_by_client: dict[str, list[str]] = defaultdict(list)
        for claim in ledger.client_claims:
            claims_by_client[claim.client_id].append(claim.agent_record_id)
        cognito_by_id = {
            str(client.get("ClientId") or ""): client
            for client in cognito_clients
        }
        if "" in cognito_by_id:
            cognito_by_id = {}
            cognito_observed = False
            observation_errors.append(
                "cognito client identity unobservable: ClientId missing"
            )
        record_statuses = catalog_record_statuses or {
            record_id: "UNKNOWN" for record_id in catalog_ids
        }

        agents: list[AgentAuthorizationInventoryItem] = []
        reclaim_candidates: list[AuthorizationReclaimCandidate] = []
        for identity in ledger.identities:
            record_id = identity.agent_record_id
            agent_tools = tuple(tools_by_agent.get(record_id, ()))
            agent_policies = tuple(policies_by_agent.get(record_id, ()))
            tool_counts = self._enum_counts(
                binding.effective_state.value for binding in agent_tools
            )
            policy_counts = self._enum_counts(
                deployment.status.value for deployment in agent_policies
            )
            client_exists: bool | str = (
                bool(
                    identity.client_id
                    and identity.client_id in cognito_by_id
                )
                if cognito_observed
                else "unknown"
            )
            claim_owners = tuple(
                sorted(set(claims_by_client.get(identity.client_id, ())))
            )
            if not identity.client_id:
                client_claim = "absent"
            elif claim_owners == (record_id,):
                client_claim = "present"
            elif not claim_owners:
                client_claim = "absent"
            else:
                client_claim = f"mismatch({','.join(claim_owners)})"

            catalog_present = record_id in catalog_ids
            if not catalog_observed:
                catalog_record = "unknown"
            elif catalog_present:
                catalog_record = (
                    f"present({record_statuses.get(record_id, 'UNKNOWN')})"
                )
            else:
                catalog_record = "absent"
            reasons: list[str] = []
            identity_consistent = True
            if identity.identity_type is not IdentityType.OAUTH_CLIENT:
                identity_consistent = False
                reasons.append("identity_type_not_oauth_client")
            if identity.status is not IdentityBindingStatus.ACTIVE:
                identity_consistent = False
                reasons.append("identity_not_active")
            if (
                cognito_observed
                and (not identity.client_id or client_exists is False)
            ):
                identity_consistent = False
                reasons.append("cognito_client_missing")
            if identity.policy_principal_id != identity.client_id:
                identity_consistent = False
                reasons.append("policy_principal_client_mismatch")
            if client_claim != "present":
                identity_consistent = False
                reasons.append("client_claim_inconsistent")

            if not catalog_observed:
                classification = "unknown"
                reasons.append("catalog_records_unobservable")
            elif not policy_observed:
                classification = "unknown"
                reasons.append("policy_engine_unobservable")
            elif not cognito_observed:
                classification = "unknown"
                reasons.append("cognito_clients_unobservable")
            elif not catalog_present:
                classification = "orphan"
                reasons.append("catalog_record_missing")
            elif not identity_consistent:
                classification = "dangling_client"
            else:
                classification = "managed"

            policy_reconciliation = reconciliation_by_agent.get(record_id)
            cedar_in_sync = (
                policy_reconciliation.in_sync
                if policy_reconciliation is not None
                else ()
            )
            cedar_stale = (
                policy_reconciliation.stale
                if policy_reconciliation is not None
                else ()
            )
            cedar_drift = (
                policy_reconciliation.drift
                if policy_reconciliation is not None
                else ()
            )
            cedar_missing = (
                policy_reconciliation.missing
                if policy_reconciliation is not None
                else ()
            )
            required_tools = tuple(
                binding
                for binding in agent_tools
                if (
                    binding.approval_state is ApprovalState.APPROVED
                    and binding.desired_state is DesiredState.ALLOWED
                )
            )
            applicable = bool(required_tools or agent_policies)
            latest = (
                max(agent_policies, key=lambda item: item.revision)
                if agent_policies
                else None
            )
            tools_realized = all(
                binding.effective_state is EffectiveState.ACTIVE
                for binding in required_tools
            )
            policy_realized = bool(
                latest is not None
                and latest.status is PolicyDeploymentStatus.ACTIVE
                and cedar_in_sync
                and not (
                    cedar_stale
                    or cedar_drift
                    or cedar_missing
                )
            )
            if not applicable:
                authorization_verdict = "not_applicable"
            elif not (
                catalog_observed
                and policy_observed
                and cognito_observed
            ):
                authorization_verdict = "unknown"
            elif (
                classification == "managed"
                and tools_realized
                and policy_realized
            ):
                authorization_verdict = "coherent"
            else:
                authorization_verdict = "diverged"
                if not tools_realized:
                    reasons.append("tool_effective_state_incomplete")
                if not policy_realized:
                    reasons.append("policy_revision_incomplete")
            fail_closed_blocked = (
                applicable and authorization_verdict != "coherent"
            )
            agents.append(AgentAuthorizationInventoryItem(
                record_id=record_id,
                identity_type=identity.identity_type.value,
                identity_status=identity.status.value,
                policy_principal_id=identity.policy_principal_id,
                client_id=identity.client_id,
                client_claim=client_claim,
                tool_count=len(agent_tools),
                tool_effective_state_counts=tool_counts,
                policy_count=len(agent_policies),
                policy_status_counts=policy_counts,
                cedar_observation=(
                    "observed" if policy_observed else "unknown"
                ),
                cedar_in_sync=cedar_in_sync,
                cedar_stale=cedar_stale,
                cedar_drift=cedar_drift,
                cedar_missing=cedar_missing,
                client_exists=client_exists,
                catalog_record=catalog_record,
                classification=classification,
                authorization_verdict=authorization_verdict,
                fail_closed_blocked=fail_closed_blocked,
                reasons=tuple(dict.fromkeys(reasons)),
            ))
            if classification == "orphan":
                reclaim_candidates.append(AuthorizationReclaimCandidate(
                    record_id=record_id,
                    client_id=identity.client_id,
                    policy_ids=tuple(sorted(
                        deployment.agentcore_policy_id
                        for deployment in agent_policies
                        if deployment.agentcore_policy_id
                    )),
                ))

        if catalog_observed and policy_observed and cognito_observed:
            candidate_record_ids = {
                candidate.record_id for candidate in reclaim_candidates
            }
            for record_id, agent_policies in deployments_by_agent.items():
                if (
                    record_id in catalog_ids
                    or record_id in candidate_record_ids
                ):
                    continue
                reclaim_candidates.append(AuthorizationReclaimCandidate(
                    record_id=record_id,
                    client_id="",
                    policy_ids=tuple(sorted(
                        deployment.agentcore_policy_id
                        for deployment in agent_policies
                        if deployment.agentcore_policy_id
                    )),
                ))

        agents.sort(key=lambda item: item.record_id)
        referenced_clients = {
            identity.client_id
            for identity in ledger.identities
            if identity.client_id
        }
        orphan_clients = (
            tuple(sorted(
                (
                CognitoClientInventoryItem(
                    client_id=client_id,
                    name=str(client.get("ClientName") or ""),
                )
                for client_id, client in cognito_by_id.items()
                if (
                    str(client.get("ClientName") or "").startswith(
                        MANAGED_AGENT_CLIENT_PREFIXES
                    )
                    and client_id not in referenced_clients
                )
                ),
                key=lambda item: item.client_id,
            ))
            if cognito_observed
            else ()
        )
        tool_state_counts = self._enum_counts(
            binding.effective_state.value
            for binding in ledger.tool_bindings
        )
        gate_active_tool_count: int | str = "unknown"
        gate_evaluated_agent_count: int | str = "unknown"
        gate_unknown_agent_count: int | str = "unknown"
        if authorization_target_agent_ids is not None:
            target_ids = authorization_target_agent_ids
            scoped_bindings = tuple(
                binding
                for binding in ledger.tool_bindings
                if (
                    binding.agent_record_id in target_ids
                    and binding.gateway_id == gateway_arn
                )
            )
            scoped_policy_agents = {
                deployment.agent_record_id
                for deployment in ledger.policy_deployments
                if (
                    deployment.agent_record_id in target_ids
                    and deployment.gateway_id == gateway_arn
                )
            }
            scoped_applicable_agents = scoped_policy_agents | {
                binding.agent_record_id
                for binding in scoped_bindings
                if (
                    binding.approval_state is ApprovalState.APPROVED
                    and binding.desired_state is DesiredState.ALLOWED
                )
            }
            scoped_agents = {
                agent.record_id: agent
                for agent in agents
                if agent.record_id in target_ids
            }
            gate_active_tool_count = sum(
                binding.effective_state is EffectiveState.ACTIVE
                for binding in scoped_bindings
            )
            gate_evaluated_agent_count = sum(
                record_id in scoped_agents
                for record_id in scoped_applicable_agents
            )
            gate_unknown_agent_count = sum(
                scoped_agents[record_id].authorization_verdict == "unknown"
                for record_id in scoped_applicable_agents
                if record_id in scoped_agents
            )
        policy_status_counts = self._enum_counts(
            deployment.status.value
            for deployment in ledger.policy_deployments
        )
        summary = AuthorizationInventorySummary(
            identity_total=len(ledger.identities),
            agents_without_tools_or_policies=sum(
                not tools_by_agent.get(identity.agent_record_id)
                and not policies_by_agent.get(identity.agent_record_id)
                for identity in ledger.identities
            ),
            missing_client_reference_count=(
                sum(
                    bool(agent.client_id)
                    and agent.client_exists is False
                    for agent in agents
                )
                if cognito_observed
                else "unknown"
            ),
            orphan_client_count=(
                len(orphan_clients) if cognito_observed else "unknown"
            ),
            tool_effective_state_counts=tool_state_counts,
            policy_status_counts=policy_status_counts,
            catalog_orphan_count=(
                sum(
                    agent.classification == "orphan"
                    for agent in agents
                )
                if (
                    catalog_observed
                    and policy_observed
                    and cognito_observed
                )
                else "unknown"
            ),
            fail_closed_evaluated_agent_count=sum(
                agent.authorization_verdict != "not_applicable"
                for agent in agents
            ),
            fail_closed_blocked_agent_count=sum(
                agent.fail_closed_blocked for agent in agents
            ),
            not_applicable_agent_count=sum(
                agent.authorization_verdict == "not_applicable"
                for agent in agents
            ),
            unknown_agent_count=sum(
                agent.authorization_verdict == "unknown"
                for agent in agents
            ),
            gate_active_tool_count=gate_active_tool_count,
            gate_evaluated_agent_count=gate_evaluated_agent_count,
            gate_unknown_agent_count=gate_unknown_agent_count,
        )
        return PolicyInventoryReport(
            status="unknown" if observation_errors else "observed",
            managed=tuple(sorted(managed, key=_policy_id)),
            orphan=tuple(sorted(orphan, key=_policy_id)),
            unmanaged=tuple(sorted(unmanaged, key=_policy_id)),
            reconciliation=reconciliation,
            agents=tuple(agents),
            orphan_clients=orphan_clients,
            reclaim_candidates=tuple(sorted(
                reclaim_candidates,
                key=lambda item: item.record_id,
            )),
            summary=summary,
            shared_cutovers=shared_cutovers,
            shared_missing=shared_missing,
            reason="; ".join(observation_errors),
        )

    def _list_cognito_clients(self) -> tuple[dict, ...]:
        if self._cognito is None or not self._user_pool_id:
            raise RuntimeError("M2 OAuth Cognito user pool is not configured")
        clients: list[dict] = []
        paginator = self._cognito.get_paginator("list_user_pool_clients")
        for page in paginator.paginate(
            UserPoolId=self._user_pool_id,
            MaxResults=60,
        ):
            clients.extend(page.get("UserPoolClients", ()))
        return tuple(clients)

    @staticmethod
    def _group_by_agent(items) -> dict[str, list]:
        grouped: dict[str, list] = defaultdict(list)
        for item in items:
            grouped[item.agent_record_id].append(item)
        return grouped

    @staticmethod
    def _enum_counts(values) -> dict[str, int]:
        return dict(sorted(Counter(values).items()))

    def _reconciliation_item(
        self,
        agent_record_id: str,
        *,
        catalog_record_ids: set[str],
        catalog_observed: bool = True,
        gateway_arn: str,
        observed_policies: tuple[dict, ...],
        observed_deployments: tuple[AgentPolicyDeployment, ...],
    ) -> PolicyReconciliationItem:
        report = self.reconcile(
            agent_record_id,
            gateway_arn=gateway_arn,
            observed_policies=observed_policies,
            observed_deployments=observed_deployments,
        )
        return PolicyReconciliationItem(
            agent_record_id=agent_record_id,
            catalog_record_present=(
                agent_record_id in catalog_record_ids
                if catalog_observed
                else "unknown"
            ),
            in_sync=report.in_sync,
            stale=report.stale,
            drift=report.drift,
            missing=report.missing,
        )


class BotoPolicyEngineClient:
    """boto3 `bedrock-agentcore-control` 어댑터.

    주의: 실제 boto3 멤버 이름(카멜케이스: policyEngineId·name·definition·validationMode·
    policyId·targetResourceScope)은 E2E([controller 실행]) 첫 호출에서 service model로
    확정해요. ConflictException은 PolicyNameConflict로 매핑해요.
    """

    def __init__(self, boto_client) -> None:
        self._c = boto_client

    def create_policy(
        self, *, policy_engine_id, name, definition, validation_mode,
        description=None,
    ):
        from botocore.exceptions import ClientError
        try:
            kwargs = {
                "policyEngineId": policy_engine_id,
                "name": name,
                "definition": definition,
                "validationMode": validation_mode,
            }
            if description is not None:
                kwargs["description"] = description
            return self._c.create_policy(
                **kwargs,
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConflictException":
                raise PolicyNameConflict(name) from exc
            raise

    def get_policy(self, *, policy_engine_id, policy_id):
        return self._c.get_policy(
            policyEngineId=policy_engine_id, policyId=policy_id
        )

    def update_policy(self, *, policy_engine_id, policy_id, definition, validation_mode):
        return self._c.update_policy(
            policyEngineId=policy_engine_id, policyId=policy_id,
            definition=definition, validationMode=validation_mode,
        )

    def list_policies(self, *, policy_engine_id, target_resource_scope=None):
        kwargs = {"policyEngineId": policy_engine_id}
        if target_resource_scope:
            kwargs["targetResourceScope"] = target_resource_scope
        policies = []
        response = {}
        seen_tokens: set[str] = set()
        while True:
            page = self._c.list_policies(**kwargs)
            page_policies = _policy_items(page)
            if not response:
                response = {
                    key: value
                    for key, value in page.items()
                    if key not in {"policies", "nextToken"}
                }
            policies.extend(page_policies)
            token = str(page.get("nextToken") or "")
            if not token:
                break
            if token in seen_tokens:
                raise RuntimeError(
                    f"list_policies repeated nextToken: {token}"
                )
            seen_tokens.add(token)
            kwargs["nextToken"] = token
        response["policies"] = policies
        return response

    def delete_policy(self, *, policy_engine_id, policy_id):
        from botocore.exceptions import ClientError
        try:
            self._c.delete_policy(
                policyEngineId=policy_engine_id,
                policyId=policy_id,
            )
        except ClientError as exc:
            # 이미 삭제된 revision(정상 재배포가 canonical 미만 owned revision을 정리한 뒤
            # 전체 teardown이 원장 기준으로 다시 delete)은 멱등 no-op으로 삼켜요 — teardown이
            # ResourceNotFound로 RuntimeError를 내던 noise 방지(IA-33 적대적 리뷰 finding 1).
            if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                return
            raise

    def tag_resource(self, *, resource_arn, tags):
        self._c.tag_resource(resourceArn=resource_arn, tags=tags)
