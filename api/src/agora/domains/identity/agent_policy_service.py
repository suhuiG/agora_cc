"""AgentToolBinding + AgentIdentityBinding을 Cedar policy로 컴파일해 배포 원장에 기록해요.

M1 범위: 실제 AgentCore Policy Engine 배포는 M2. 여기서는 결정론적 산출물(Cedar·hash·
revision)을 AgentPolicyDeployment로 남겨 reconciliation·감사가 읽을 수 있게 해요.

⚠️ **per-agent 정책 경로 폐기 — ADR-0093, 2026-08-29.**

`compile_and_deploy`·`resume_deploy` 는 agent 하나당 Cedar 정책 한 장을 만들던 경로예요.
공유 정책을 Gateway 인프라로 provisioning 하기로 정해서 **프로덕션 진입점을 껐어요**
(`_PER_AGENT_POLICY_DEPRECATED` 게이트). 코드는 남겨요 — 살아있는 per-agent 정책을 보존하며
전환해야 하는 상황이 오면 그 순서 지식이 필요하거든요.

같은 이유로 `cut_over_shared_gateway`·`observe_shared_gateway_cutover`·
`_shared_policy_result` 도 폐기예요. 클린 슬레이트로 시작해서 마이그레이션 대상이 없어요.

되살릴 조건: ADR-0093 을 대체하는 새 ADR. 게이트만 끄면 되지만, 그때는 `_shared_policy_result`
가 요구하는 `GatewayCallProbe` production 구현이 여전히 없다는 걸 기억해요.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from datetime import datetime, timezone

from ...shared.permission_group import DEFAULT_PERMISSION_GROUP
from .agent_policy_compiler import (
    CompiledSharedGatewayPolicies,
    NoToolAccessError,
    SharedGatewayPolicySpec,
    build_policy_spec,
    compile_agent_policy,
    compile_shared_gateway_policies,
)
from .agent_policy_cutover import (
    GatewayCallVerifier,
    GatewayPolicyCutover,
    GatewayPolicyCutoverPhase,
)
from .agent_policy_deployer import STALE_REVISION_CLEANUP_FAILED
from .delegation import delegated_assets
from .models import (
    AgentPolicyDeployOutcome,
    AgentPolicyDeployResult,
    AgentPolicyDeployment,
    ApprovalState,
    DesiredState,
    EffectiveState,
    IdentityType,
    PolicyDeploymentStatus,
    PolicyMode,
)
from .store import IdentityRecordNotFound, IdentityStore


_log = logging.getLogger(__name__)

# 폐기 스위치 (ADR-0093, 2026-08-29). env 로 열지 않아요 — 되살리는 건 설계 결정이라
# 운영 설정으로 뒤집힐 자리가 아니에요. 되살릴 때 이 상수를 `False` 로 바꾸고 ADR 을 새로 써요.
_PER_AGENT_POLICY_DEPRECATED = True
_NO_TOOL_ACCESS_POLICY_GAP = (
    "no tool access compiled; previous AgentCore policy may remain active"
)
_NO_TOOL_ACCESS_LEDGER_FAILURE = (
    "no-tool-access gap ledger recording failed"
)


class UnsupportedAgentPolicyPrincipalError(ValueError):
    """OAuth client가 아닌 agent 신원은 OAuthUser Cedar policy로 컴파일할 수 없어요."""

    def __init__(
        self, message: str, deployment: AgentPolicyDeployment
    ) -> None:
        super().__init__(message)
        self.deployment = deployment


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AgentPolicyService:
    def __init__(
        self,
        store: IdentityStore,
        *,
        new_id=None,
        now=None,
        deployer_factory=None,
        deployer_configured=None,
        declaration_resolver=None,
        scope_observer=None,
    ) -> None:
        self.store = store
        self._new_id = new_id or (lambda: uuid.uuid4().hex)
        self._now = now or _now_iso
        self._deployer_factory = deployer_factory
        self._deployer_configured = deployer_configured or (lambda: False)
        self._declaration_resolver = declaration_resolver
        self._scope_observer = scope_observer

    def cut_over_shared_gateway(
        self,
        spec: SharedGatewayPolicySpec,
        *,
        created_by: str,
        call_verifier: GatewayCallVerifier | None = None,
    ) -> GatewayPolicyCutover:
        """Compile and cut over the Gateway-wide policy set.

        Targets come from the Registry ledger. Scope names are replaced with a
        complete Cognito observation so a caller cannot self-validate a new
        scope name.
        """
        if not self._deployer_configured():
            raise RuntimeError(
                "Gateway shared policy deployer is not configured"
            )
        compiled = self.compile_shared_gateway(spec)
        return self._deployer_factory().cut_over_shared_gateway(
            compiled,
            created_by=created_by,
            call_verifier=call_verifier,
        )

    def compile_shared_gateway(
        self,
        spec: SharedGatewayPolicySpec,
    ) -> CompiledSharedGatewayPolicies:
        """Compile from Registry targets and independently observed scopes."""
        if self._scope_observer is None:
            raise RuntimeError(
                "Cognito scope observer is not configured"
            )
        observation = self._scope_observer.observe()
        if observation.status != "observed":
            raise RuntimeError(
                "Cognito scope inventory is unknown: "
                f"{observation.reason}"
            )
        observed_scopes = tuple(observation.scope_names)
        if spec.invoke_scope not in observed_scopes:
            raise RuntimeError(
                "observed Cognito scope inventory does not contain "
                "the required invoke scope"
            )
        compiled = compile_shared_gateway_policies(
            replace(spec, scope_names=observed_scopes)
        )
        return compiled

    def _shared_policy_result(
        self,
        agent_record_id: str,
    ) -> AgentPolicyDeployResult | None:
        getter = getattr(
            self.store,
            "get_latest_gateway_policy_cutover",
            None,
        )
        if getter is None:
            return None
        bindings = self.store.list_agent_tool_bindings(agent_record_id)
        gateways = {
            binding.gateway_id
            for binding in bindings
            if binding.gateway_id
        }
        if len(gateways) != 1:
            return None
        cutover = getter(next(iter(gateways)))
        if cutover is None:
            return None
        if cutover.phase is GatewayPolicyCutoverPhase.FAILED:
            return AgentPolicyDeployResult(
                AgentPolicyDeployOutcome.DEPLOY_FAILED
            )
        externally_active = all(
            policy.remote_status == "ACTIVE"
            and bool(policy.deployed_policy_hash)
            for policy in cutover.policies
        ) and bool(cutover.policies)
        completed = (
            cutover.phase is GatewayPolicyCutoverPhase.COMPLETED
            and cutover.call_verification.qualified
            and externally_active
        )
        observed_live = False
        if completed and self._deployer_configured():
            try:
                observed_live = (
                    self._deployer_factory()
                    .observe_shared_gateway_cutover(cutover)
                )
            except Exception:  # noqa: BLE001 - unobservable is not active
                observed_live = False
        outcome = (
            AgentPolicyDeployOutcome.DEPLOYED_ACTIVE
            if observed_live
            else AgentPolicyDeployOutcome.DEPLOY_IN_PROGRESS
        )
        return AgentPolicyDeployResult(outcome)

    def compile_agent(
        self, agent_record_id: str, *, created_by: str
    ) -> AgentPolicyDeployment:
        identity = self.store.get_agent_identity_binding(agent_record_id)
        bindings = self.store.list_agent_tool_bindings(agent_record_id)
        if self._declaration_resolver is not None:
            declarations = delegated_assets(
                self._declaration_resolver(agent_record_id)
            )
            bindings = tuple(
                binding
                for binding in bindings
                if any(
                    declaration.asset_id == binding.asset_id
                    and declaration.allows(
                        binding.asset_version,
                        binding.operation_id,
                    )
                    for declaration in declarations
                )
            )
        latest = self.store.get_latest_agent_policy_deployment(agent_record_id)
        expected_revision = latest.revision if latest else 0
        revision = expected_revision + 1
        principal_id = identity.policy_principal_id
        if (
            identity.identity_type is not IdentityType.OAUTH_CLIENT
            or principal_id.lower().startswith("arn:")
        ):
            reason = (
                "agent policy requires an OAuth client identity; "
                f"got identity_type={identity.identity_type.value}, "
                f"principal_kind={'arn' if principal_id.lower().startswith('arn:') else 'non_arn'}"
            )
            gateways = {binding.gateway_id for binding in bindings}
            failed = AgentPolicyDeployment(
                agent_record_id=agent_record_id,
                revision=revision,
                gateway_id=next(iter(gateways)) if len(gateways) == 1 else "",
                policy_id=f"agent-{agent_record_id}-r{revision}",
                policy_hash="",
                cedar_policy="",
                action_count=0,
                mode=PolicyMode.LOG_ONLY,
                status=PolicyDeploymentStatus.FAILED,
                created_at=self._now(),
                created_by=created_by,
                validation_findings=(reason,),
            )
            self.store.put_agent_policy_deployment(
                failed, expected_revision=expected_revision
            )
            raise UnsupportedAgentPolicyPrincipalError(reason, failed)
        # IA-22e: agent 권한 그룹(ceiling)으로 각 tool의 민감도 태그를 걸러요(ADR-0018 §6).
        # invoke authz 레코드가 없으면 최소권한(ReadOnly) 기본. 태그는 binding에 비정규화돼 있어요.
        try:
            permission_group = self.store.get_agent_invoke_authorization(
                agent_record_id
            ).permission_group
        except IdentityRecordNotFound:
            permission_group = DEFAULT_PERMISSION_GROUP
        sensitivity_by_action = {
            b.gateway_action: b.sensitivity for b in bindings if b.sensitivity
        }
        # NoToolAccessError는 호출부로 전파 — allow-all·gateway invoke 권한을 만들지 않아요(§4.3).
        # principal은 agent별 OAuth client ID만 허용해 shared IAM role permit 합집합을 막아요.
        # gateway resource ARN은 컴파일러가 승인된 binding의 gateway_id에서 파생해요.
        spec = build_policy_spec(
            principal_id=principal_id,
            agent_record_id=agent_record_id,
            revision=revision,
            bindings=bindings,
            permission_group=permission_group,
            sensitivity_by_action=sensitivity_by_action,
        )
        compiled = compile_agent_policy(spec)
        deployment = AgentPolicyDeployment(
            agent_record_id=agent_record_id,
            revision=revision,
            gateway_id=spec.gateway_arn,
            policy_id=f"agent-{agent_record_id}-r{revision}",
            policy_hash=compiled.policy_hash,
            cedar_policy=compiled.cedar_policy,
            action_count=compiled.action_count,
            mode=PolicyMode.LOG_ONLY,
            status=PolicyDeploymentStatus.PENDING,
            created_at=self._now(),
            created_by=created_by,
            compiled_actions=spec.actions,
        )
        self.store.put_agent_policy_deployment(
            deployment, expected_revision=expected_revision
        )
        return deployment

    def compile_and_deploy(
        self, agent_record_id: str, *, created_by: str
    ) -> AgentPolicyDeployResult:
        # 폐기 게이트 (ADR-0093, 2026-08-29). 호출부가 7곳이라 각각 고치지 않고 여기 한 곳에서
        # 끊어요 — 진입점이 하나면 되살릴 때도 한 곳만 봐요.
        if _PER_AGENT_POLICY_DEPRECATED:
            return AgentPolicyDeployResult(
                AgentPolicyDeployOutcome.SKIPPED_PER_AGENT_DEPRECATED
            )
        try:
            self.store.get_agent_identity_binding(agent_record_id)
        except IdentityRecordNotFound:
            return AgentPolicyDeployResult(
                AgentPolicyDeployOutcome.SKIPPED_NO_IDENTITY
            )
        shared_result = self._shared_policy_result(agent_record_id)
        if shared_result is not None:
            return shared_result
        try:
            deployment = self.compile_agent(
                agent_record_id, created_by=created_by
            )
        except IdentityRecordNotFound:
            return AgentPolicyDeployResult(
                AgentPolicyDeployOutcome.SKIPPED_NO_IDENTITY
            )
        except NoToolAccessError:
            return self._no_tool_access_result(agent_record_id)
        except UnsupportedAgentPolicyPrincipalError as exc:
            self._record_binding_effective_state(
                agent_record_id,
                deployment=exc.deployment,
                observed_state=EffectiveState.FAILED,
            )
            return AgentPolicyDeployResult(
                AgentPolicyDeployOutcome.DEPLOY_FAILED,
                exc.deployment,
            )

        if not self._deployer_configured():
            self._record_binding_effective_state(
                agent_record_id,
                deployment=deployment,
                observed_state=EffectiveState.UNKNOWN,
            )
            return AgentPolicyDeployResult(
                AgentPolicyDeployOutcome.SKIPPED_NO_DEPLOYER,
                deployment,
            )
        return self._deploy(agent_record_id, deployment)

    def _no_tool_access_result(
        self,
        agent_record_id: str,
    ) -> AgentPolicyDeployResult:
        latest = self.store.get_latest_agent_policy_deployment(
            agent_record_id
        )
        if latest is None:
            return AgentPolicyDeployResult(
                AgentPolicyDeployOutcome.SKIPPED_NO_TOOL_ACCESS
            )
        findings = tuple(dict.fromkeys((
            *latest.validation_findings,
            _NO_TOOL_ACCESS_POLICY_GAP,
        )))
        _log.warning(
            "agent policy compile skipped(agent=%s): %s",
            agent_record_id,
            _NO_TOOL_ACCESS_POLICY_GAP,
        )
        try:
            observed = self.store.mark_agent_policy_deployment(
                agent_record_id,
                latest.revision,
                status=latest.status,
                validation_findings=findings,
            )
        except Exception as exc:
            _log.warning(
                "no-tool-access gap ledger 기록 실패(agent=%s): %s",
                agent_record_id,
                exc,
                exc_info=True,
            )
            findings = tuple(dict.fromkeys((
                *findings,
                _NO_TOOL_ACCESS_LEDGER_FAILURE,
            )))
            observed = replace(
                latest,
                validation_findings=findings,
            )
            outcome = AgentPolicyDeployOutcome.DEPLOY_FAILED
        else:
            outcome = AgentPolicyDeployOutcome.SKIPPED_NO_TOOL_ACCESS
        return AgentPolicyDeployResult(
            outcome,
            observed,
        )

    def resume_deploy(
        self,
        agent_record_id: str,
        *,
        revision: int,
        policy_id: str,
    ) -> AgentPolicyDeployResult:
        # 폐기 게이트 (ADR-0093, 2026-08-29). `compile_and_deploy` 와 짝이에요 — 한쪽만 끄면
        # 중단된 배포를 이어받는 경로로 per-agent 정책이 다시 생겨요.
        if _PER_AGENT_POLICY_DEPRECATED:
            return AgentPolicyDeployResult(
                AgentPolicyDeployOutcome.SKIPPED_PER_AGENT_DEPRECATED
            )
        deployment = next(
            (
                item
                for item in self.store.list_agent_policy_deployments(
                    agent_record_id
                )
                if item.revision == revision
            ),
            None,
        )
        if (
            deployment is None
            or deployment.status is not PolicyDeploymentStatus.PENDING
            or deployment.agentcore_policy_id != policy_id
        ):
            raise ValueError(
                "pending Cedar policy checkpoint does not match the ledger"
            )
        shared_result = self._shared_policy_result(agent_record_id)
        if shared_result is not None:
            return shared_result
        if not self._deployer_configured():
            self._record_binding_effective_state(
                agent_record_id,
                deployment=deployment,
                observed_state=EffectiveState.UNKNOWN,
            )
            return AgentPolicyDeployResult(
                AgentPolicyDeployOutcome.SKIPPED_NO_DEPLOYER,
                deployment,
            )
        return self._deploy(agent_record_id, deployment)

    def _deploy(
        self,
        agent_record_id: str,
        deployment: AgentPolicyDeployment,
    ) -> AgentPolicyDeployResult:
        observation_failed = False
        try:
            deployed = self._deployer_factory().deploy(deployment)
        except Exception as exc:
            checkpoint = next(
                (
                    item
                    for item in self.store.list_agent_policy_deployments(
                        agent_record_id
                    )
                    if item.revision == deployment.revision
                ),
                deployment,
            )
            # Deployer checkpoints the remote policy ID before readback. An
            # exception after that point means Cedar could not be observed;
            # create/validation rejection before an ID exists is a known failure.
            observation_failed = bool(checkpoint.agentcore_policy_id)
            # IA-31: 타입명만 남기면 "왜 거부됐는지"(예: Cedar ValidationException의
            # finding 상세)를 알 수 없어요. 전체 메시지를 findings(UI 노출)와 로그에 남겨요.
            _log.warning(
                "agent policy 배포 실패(agent=%s): %s",
                agent_record_id, exc, exc_info=True,
            )
            deployed = self.store.mark_agent_policy_deployment(
                agent_record_id,
                deployment.revision,
                status=PolicyDeploymentStatus.FAILED,
                validation_findings=(
                    f"deployment error: {type(exc).__name__}",
                    f"detail: {str(exc)[:800]}",
                ),
            )
        if (
            not observation_failed
            and deployed.status is PolicyDeploymentStatus.ACTIVE
        ):
            # 새 revision 이 **ACTIVE 로 관측된 뒤에만** 옛 revision 을 회수해요.
            # `observation_failed`(UNKNOWN)에서는 오지 않아요 — 새 정책이 실제로 살았는지
            # 모르는 상태에서 지우면 권한 0 인 창이 열려요(ADR-0037 §4).
            deployed = self._reclaim_superseded_revisions(
                agent_record_id, deployed
            )
        if observation_failed:
            effective_state = EffectiveState.UNKNOWN
        elif deployed.status is PolicyDeploymentStatus.ACTIVE:
            effective_state = EffectiveState.ACTIVE
        elif deployed.status is PolicyDeploymentStatus.FAILED:
            effective_state = EffectiveState.FAILED
        else:
            effective_state = EffectiveState.UNKNOWN
        self._record_binding_effective_state(
            agent_record_id,
            deployment=deployed,
            observed_state=effective_state,
        )
        if deployed.status is PolicyDeploymentStatus.ACTIVE:
            outcome = AgentPolicyDeployOutcome.DEPLOYED_ACTIVE
        elif deployed.status is PolicyDeploymentStatus.PENDING:
            outcome = AgentPolicyDeployOutcome.DEPLOY_IN_PROGRESS
        else:
            outcome = AgentPolicyDeployOutcome.DEPLOY_FAILED
        return AgentPolicyDeployResult(outcome, deployed)

    def _reclaim_superseded_revisions(
        self,
        agent_record_id: str,
        deployed: AgentPolicyDeployment,
    ) -> AgentPolicyDeployment:
        """새 revision 이 ACTIVE 인 뒤 옛 revision permit 을 회수해요.

        **Cedar ACTIVE permit 은 합집합이에요.** 옛 `_r{<N}` 이 남으면 유효 권한이 최광의
        union 으로 굳어 좁히는 재컴파일이 무효가 돼요 — 도구를 회수해도 옛 정책이 계속
        허용해요. 그래서 "좁히는 배포" 는 새 정책 생성만으로 끝나지 않고 **옛 revision 삭제까지
        한 단위**예요.

        회수 실패는 성공으로 접지 않아요. 새 것이 ACTIVE 인데 옛 것이 남으면 **권한이 넓은
        채로 남은 것**이라, 동기 경로(`AgentPolicyDeployer.deploy` `:412`)와 똑같이 이 revision
        을 FAILED 로 떨어뜨려 fail-closed 판정이 나오게 해요(ADR-0028).

        ## 이건 이음매 방어예요 (2026-08-31 측정)

        지금 물려 있는 `AgentPolicyDeployer.deploy` 는 동기 관측이 ACTIVE 로 끝나면 자기 안에서
        이미 회수해요. 그래서 이 호출은 보통 **멱등 no-op** 이고, 비용은 `list_policies` 한
        번이에요.

        그래도 두는 이유: 회수가 배포기 **구현 안쪽**에만 있으면, ACTIVE 를 나중에 관측해서
        승격하는 호출자가 생기는 순간 그 경로엔 회수가 없어요. 그게 정확히 dev identity 경로에서
        일어난 일이고(`DevIdentityService.reconcile_pending_policies`), 라이브에 r1·r2 가 함께
        ACTIVE 로 남은 원인이에요. 회수 책임을 "ACTIVE 를 확정하는 쪽" 에 두면 그 실수를
        반복하지 않아요.
        """
        deployer = self._deployer_factory()
        try:
            errors = deployer.reclaim_superseded_revisions(
                agent_record_id,
                current_revision=deployed.revision,
                gateway_arn=deployed.gateway_id,
                keep_policy_id=deployed.agentcore_policy_id,
            )
        except Exception as exc:  # noqa: BLE001 - 회수를 관측 못 한 건 통과가 아니에요.
            errors = (
                "revision reclaim unobservable: "
                f"{type(exc).__name__}: {exc}",
            )
        if not errors:
            return deployed
        findings = tuple(dict.fromkeys((
            STALE_REVISION_CLEANUP_FAILED,
            *errors,
        )))
        _log.error(
            "옛 revision 회수 실패(agent=%s, r%s): %s",
            agent_record_id,
            deployed.revision,
            "; ".join(errors),
        )
        try:
            return self.store.mark_agent_policy_deployment(
                agent_record_id,
                deployed.revision,
                status=PolicyDeploymentStatus.FAILED,
                agentcore_policy_id=deployed.agentcore_policy_id,
                validation_findings=findings,
            )
        except Exception as exc:  # noqa: BLE001 - 원장 기록 실패도 성공으로 접지 않아요.
            _log.warning(
                "옛 revision 회수 실패의 원장 기록이 실패했어요(agent=%s, r%s): %s",
                agent_record_id,
                deployed.revision,
                exc,
                exc_info=True,
            )
            return replace(
                deployed,
                status=PolicyDeploymentStatus.FAILED,
                validation_findings=findings,
            )

    def _record_binding_effective_state(
        self,
        agent_record_id: str,
        *,
        deployment: AgentPolicyDeployment,
        observed_state: EffectiveState,
    ) -> None:
        """Record one policy observation across its TOOL binding snapshot."""
        active_actions = set(deployment.compiled_actions)
        action_snapshot_missing = (
            observed_state is EffectiveState.ACTIVE
            and deployment.action_count > 0
            and not active_actions
        )
        for binding in self.store.list_agent_tool_bindings(agent_record_id):
            if binding.policy_revision > deployment.revision:
                continue
            target: EffectiveState | None = None
            if (
                binding.approval_state is ApprovalState.REJECTED
                or binding.desired_state is DesiredState.REVOKED
            ):
                if observed_state is not EffectiveState.ACTIVE:
                    target = observed_state
                elif action_snapshot_missing:
                    target = EffectiveState.UNKNOWN
                elif binding.gateway_action in active_actions:
                    target = EffectiveState.ACTIVE
                else:
                    target = EffectiveState.REVOKED
            elif binding.approval_state is not ApprovalState.APPROVED:
                continue
            elif observed_state is EffectiveState.ACTIVE:
                if action_snapshot_missing:
                    target = EffectiveState.UNKNOWN
                elif binding.gateway_action in active_actions:
                    target = EffectiveState.ACTIVE
                else:
                    target = EffectiveState.REVOKED
            elif binding.effective_state is not EffectiveState.ACTIVE:
                # A failed or unobservable new revision does not erase a prior
                # independently observed ACTIVE revision.
                target = observed_state
            if target is None:
                continue
            self.store.set_agent_tool_binding_effective_state(
                binding,
                effective_state=target,
                policy_revision=deployment.revision,
            )

    def delete_agent_artifacts(
        self,
        agent_record_id: str,
        *,
        delete_managed_client=None,
        managed_client_id: str = "",
        fallback_managed_client_id: str = "",
    ) -> None:
        """원격 Cedar, 관리형 Cognito client, 로컬 인가 원장을 제거해요.

        원격 좌표나 client 회수가 실패하면 재시도 근거인 로컬 원장을 보존해요.
        """
        errors: list[str] = []
        deployments = self.store.list_agent_policy_deployments(agent_record_id)
        if not managed_client_id and delete_managed_client is not None:
            try:
                managed_client_id = self.store.get_agent_identity_binding(
                    agent_record_id
                ).client_id
            except IdentityRecordNotFound:
                pass
            if not managed_client_id:
                reservation = self.store.get_managed_client_reservation(
                    agent_record_id
                )
                if reservation is not None:
                    managed_client_id = reservation.client_id
                    if not managed_client_id:
                        errors.append(
                            "oauth client: cleanup skipped: managed client "
                            "reservation has no client coordinate"
                        )
                elif fallback_managed_client_id:
                    managed_client_id = fallback_managed_client_id
        if self._deployer_configured():
            deployer = self._deployer_factory()
            for deployment in deployments:
                try:
                    deployer.delete(deployment)
                except Exception as exc:  # noqa: BLE001 - 나머지 revision도 정리해요.
                    errors.append(
                        f"policy r{deployment.revision}: "
                        f"{type(exc).__name__}: {exc}"
                    )
        elif any(item.agentcore_policy_id for item in deployments):
            errors.append("policy deployer unavailable for remote cleanup")
        if errors:
            raise RuntimeError("; ".join(errors))
        if managed_client_id and delete_managed_client is not None:
            try:
                deleted = delete_managed_client(managed_client_id)
                if deleted is False:
                    errors.append(
                        "oauth client: cleanup skipped: managed client "
                        "reservation no longer matches"
                    )
            except Exception as exc:  # noqa: BLE001 - 원격 policy 오류와 함께 보고해요.
                errors.append(f"oauth client: {type(exc).__name__}: {exc}")
        if not errors:
            try:
                self.store.delete_agent_authorization_artifacts(agent_record_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"identity ledger: {type(exc).__name__}: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors))

    def rollback_provisioning(
        self,
        agent_record_id: str,
        *,
        created_bindings: tuple[dict, ...],
        policy_revision: int | None,
    ) -> None:
        """실패한 재배포가 추가한 policy revision과 READ bindings만 제거해요."""
        errors: list[str] = []
        if policy_revision is not None:
            deployment = next(
                (
                    item
                    for item in self.store.list_agent_policy_deployments(
                        agent_record_id
                    )
                    if item.revision == policy_revision
                ),
                None,
            )
            if deployment is not None and self._deployer_configured():
                try:
                    self._deployer_factory().delete(deployment)
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        f"policy r{policy_revision}: {type(exc).__name__}: {exc}"
                    )
        try:
            self.store.rollback_agent_provisioning(
                agent_record_id,
                created_bindings=created_bindings,
                policy_revision=policy_revision,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"identity ledger: {type(exc).__name__}: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors))
