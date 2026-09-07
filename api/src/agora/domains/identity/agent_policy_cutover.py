"""Gateway-wide Cedar policy lifecycle for IA-53.

⚠️ **모듈 전체 폐기 — ADR-0093, 2026-08-29.**

이 모듈은 **살아있는 per-agent 정책을 보존하며** 공유 정책으로 갈아타는 마이그레이션
기계예요. 순서(좁은 정책 생성 → `ACTIVE` 확인 → 옛 정책 삭제 → 실제 호출로 확인)는 실측으로
얻은 지식이고, 뒤집으면 4~5초 전면 거부 창이 생겨요.

폐기 이유는 순서가 틀렸기 때문이 아니에요. **지킬 대상이 없어졌어요** — 2026-08-29 에 배포된
agent·정책·MCP 를 전부 지우고 클린 슬레이트에서 시작하기로 했고, 그러면 공유 정책은
마이그레이션 산출물이 아니라 Gateway 인프라의 초기 상태예요.

프로덕션 진입점은 `agent_policy_service` 의 `cut_over_shared_gateway`·
`observe_shared_gateway_cutover`·`_shared_policy_result` 였고 전부 폐기 표시했어요.

`GatewayCallProbe`·`GatewayCallEvidenceObserver` 는 production 구현이 **없었어요.** 그래서
`phase` 가 `VERIFYING_CALL` 에서 멈추고 스위치가 켜지지 않았어요. ADR-0093 은 증거를
Gateway 로그 상관 대신 **Agora 자체 프로브의 Gateway 응답**으로 받기로 했어요 — 여기
`GatewayCallVerification.qualified` 의 11개 조건은 그 **더 강한 등급**의 기준으로 남겨둬요.
등급을 올릴 때 무엇을 만족해야 하는지 잃지 않으려고요(ADR-0093 § Open risks).

되살릴 조건: 살아있는 정책을 보존하며 전환해야 하는 상황 + ADR-0093 을 대체하는 새 ADR.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import Protocol

from .agent_policy_compiler import (
    MAX_CEDAR_POLICY_BYTES,
    CompiledSharedGatewayPolicies,
)
from .models import AgentPolicyDeployment, PolicyDeploymentStatus


_log = logging.getLogger(__name__)
_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_]")
_WS = re.compile(r"\s+")
_DEV_CREDENTIAL_AGENT_ID = re.compile(r"^dev-[0-9a-f]{24}$")


class PolicyNameConflict(Exception):
    """A policy with the requested name already exists."""


class GatewayPolicyCutoverPhase(str, Enum):
    CREATING = "CREATING"
    AWAITING_ACTIVE = "AWAITING_ACTIVE"
    DELETING_LEGACY = "DELETING_LEGACY"
    VERIFYING_CALL = "VERIFYING_CALL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class GatewaySharedPolicyDeployment:
    policy_key: str
    policy_name: str
    cedar_policy: str
    policy_hash: str
    target_count: int
    size_bytes: int
    agentcore_policy_id: str = ""
    remote_status: str = "NOT_CREATED"
    deployed_policy_hash: str = ""
    status_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class GatewayCallVerification:
    """Independent Gateway-call evidence collected once per cutover."""

    status: str = "required"
    reachable_call_observed: bool = False
    negative_control_observed: bool = False
    evidence_source: str = ""
    positive_call_id: str = ""
    negative_call_id: str = ""
    positive_target_event_id: str = ""
    negative_decision_event_id: str = ""
    observation_window_id: str = ""
    negative_control_kind: str = ""
    detail: str = ""

    @property
    def qualified(self) -> bool:
        return (
            self.status == "observed"
            and self.reachable_call_observed
            and self.negative_control_observed
            and bool(self.evidence_source)
            and bool(self.positive_call_id)
            and bool(self.negative_call_id)
            and self.positive_call_id != self.negative_call_id
            and bool(self.positive_target_event_id)
            and bool(self.negative_decision_event_id)
            and bool(self.observation_window_id)
            and self.negative_control_kind
            in GatewayCallVerifier._NEGATIVE_CONTROL_KINDS
        )


@dataclass(frozen=True)
class GatewayCallAttempt:
    """One call made by the probing owner."""

    call_id: str


@dataclass(frozen=True)
class GatewayCallObservation:
    """Evidence read by an owner independent from the call subject."""

    evidence_source: str
    positive_target_event_id: str
    negative_decision_event_id: str
    observation_window_id: str
    detail: str = ""


class GatewayCallProbe(Protocol):
    def invoke_allowed(self, gateway_arn: str) -> GatewayCallAttempt: ...

    def invoke_negative_control(
        self,
        gateway_arn: str,
        *,
        negative_control_kind: str,
    ) -> GatewayCallAttempt: ...


class GatewayCallEvidenceObserver(Protocol):
    def observe(
        self,
        gateway_arn: str,
        *,
        positive_call_id: str,
        negative_call_id: str,
        negative_control_kind: str,
    ) -> GatewayCallObservation: ...


class GatewayCallVerifier:
    """Run both calls, then correlate them through an independent observer."""

    #: 음성 대조의 종류 — **강제 지점이 바뀌면 이 목록도 바뀌어요** (ADR-0099 결정 8).
    #:
    #: 옛 값은 `danger_scope_absent` · `danger_scope_prefix_overlap` 이었어요. Cedar danger
    #: 백스톱을 없앤 뒤로는 그 대조를 **만들 수가 없어요** — 시험할 `forbid` 가 존재하지
    #: 않으니까요. 옛 값을 관용으로 남기면 「이빨을 확인했다」는 기록이 실제로는 아무것도
    #: 시험하지 않은 것이 되니, 받아들이지 않아요.
    #:
    #: 새 대조는 강제 지점(REQUEST interceptor)의 두 층을 각각 겨눠요.
    #: - `tool_binding_absent`  : ④ 가 없어서 거부 (`tool_not_approved`)
    #: - `human_grant_absent`   : ④ 는 있고 ⑦ 이 없어서 거부 (`human_grant_missing`)
    #:
    #: 두 사유가 **서로 달라야** 해요. 같은 사유가 나오면 한 층만 보고 있다는 뜻이에요(IH-147).
    _NEGATIVE_CONTROL_KINDS = {
        "tool_binding_absent",
        "human_grant_absent",
    }

    def __init__(
        self,
        probe: GatewayCallProbe,
        observer: GatewayCallEvidenceObserver,
        *,
        negative_control_kind: str,
    ) -> None:
        if negative_control_kind not in self._NEGATIVE_CONTROL_KINDS:
            raise ValueError("unsupported Gateway negative control")
        self._probe = probe
        self._observer = observer
        self._negative_control_kind = negative_control_kind

    def verify(self, gateway_arn: str) -> GatewayCallVerification:
        positive = self._probe.invoke_allowed(gateway_arn)
        negative = self._probe.invoke_negative_control(
            gateway_arn,
            negative_control_kind=self._negative_control_kind,
        )
        positive_call_id = positive.call_id.strip()
        negative_call_id = negative.call_id.strip()
        if (
            not positive_call_id
            or not negative_call_id
            or positive_call_id == negative_call_id
        ):
            raise RuntimeError(
                "Gateway probes must return two distinct call IDs"
            )
        observed = self._observer.observe(
            gateway_arn,
            positive_call_id=positive_call_id,
            negative_call_id=negative_call_id,
            negative_control_kind=self._negative_control_kind,
        )
        positive_target_event_id = (
            observed.positive_target_event_id.strip()
        )
        negative_decision_event_id = (
            observed.negative_decision_event_id.strip()
        )
        evidence_source = observed.evidence_source.strip()
        observation_window_id = observed.observation_window_id.strip()
        fully_observed = all((
            positive_target_event_id,
            negative_decision_event_id,
            evidence_source,
            observation_window_id,
        ))
        return GatewayCallVerification(
            status="observed" if fully_observed else "unknown",
            reachable_call_observed=bool(positive_target_event_id),
            negative_control_observed=bool(negative_decision_event_id),
            evidence_source=evidence_source,
            positive_call_id=positive_call_id,
            negative_call_id=negative_call_id,
            positive_target_event_id=positive_target_event_id,
            negative_decision_event_id=negative_decision_event_id,
            observation_window_id=observation_window_id,
            negative_control_kind=self._negative_control_kind,
            detail=observed.detail,
        )


@dataclass(frozen=True)
class GatewayPolicyCutover:
    gateway_arn: str
    revision: int
    phase: GatewayPolicyCutoverPhase
    policies: tuple[GatewaySharedPolicyDeployment, ...]
    created_at: str
    created_by: str
    updated_at: str
    version: int = 1
    delete_requested_policy_ids: tuple[str, ...] = ()
    deleted_policy_ids: tuple[str, ...] = ()
    unmanaged_policy_ids: tuple[str, ...] = ()
    preserved_policy_ids: tuple[str, ...] = ()
    call_verification: GatewayCallVerification = GatewayCallVerification()
    findings: tuple[str, ...] = ()


class SharedPolicyEngineClient(Protocol):
    def create_policy(
        self,
        *,
        policy_engine_id: str,
        name: str,
        definition: dict,
        validation_mode: str,
        description: str | None = None,
    ) -> dict: ...

    def get_policy(
        self,
        *,
        policy_engine_id: str,
        policy_id: str,
    ) -> dict: ...

    def list_policies(
        self,
        *,
        policy_engine_id: str,
        target_resource_scope: str | None = None,
    ) -> dict: ...

    def delete_policy(
        self,
        *,
        policy_engine_id: str,
        policy_id: str,
    ) -> None: ...


class GatewayPolicyCutoverStore(Protocol):
    def put_gateway_policy_cutover(
        self,
        cutover: GatewayPolicyCutover,
        *,
        expected_revision: int,
    ) -> None: ...

    def update_gateway_policy_cutover(
        self,
        cutover: GatewayPolicyCutover,
        *,
        expected_version: int,
    ) -> None: ...

    def get_latest_gateway_policy_cutover(
        self,
        gateway_arn: str,
    ) -> GatewayPolicyCutover | None: ...

    def list_gateway_policy_cutovers(
        self,
        gateway_arn: str,
    ) -> list[GatewayPolicyCutover]: ...

    def list_all_agent_policy_deployments(
        self,
    ) -> list[AgentPolicyDeployment]: ...

    def mark_agent_policy_deployment(
        self,
        agent_record_id: str,
        revision: int,
        *,
        status: PolicyDeploymentStatus,
    ) -> AgentPolicyDeployment: ...


class GatewayPolicyCutoverManager:
    """Create, activate, prune, and externally verify one shared policy set."""

    def __init__(
        self,
        client: SharedPolicyEngineClient,
        store: GatewayPolicyCutoverStore,
        *,
        engine_id: str,
        validation_mode: str,
        now,
        sleep,
        max_polls: int,
    ) -> None:
        self._client = client
        self._store = store
        self._engine_id = engine_id
        self._validation_mode = validation_mode
        self._now = now
        self._sleep = sleep
        self._max_polls = max_polls

    @staticmethod
    def normalize_cedar(text: str) -> str:
        return _WS.sub(" ", text).strip()

    @staticmethod
    def policy_name(
        gateway_arn: str,
        revision: int,
        policy_key: str,
    ) -> str:
        gateway_hash = hashlib.sha256(
            gateway_arn.encode("utf-8")
        ).hexdigest()[:10]
        key = _NAME_UNSAFE.sub("_", policy_key)
        suffix = f"_r{revision}"
        prefix = f"Gateway_{gateway_hash}_"
        key_budget = max(1, 48 - len(prefix) - len(suffix))
        return f"{prefix}{key[:key_budget]}{suffix}"

    def cut_over(
        self,
        compiled: CompiledSharedGatewayPolicies,
        *,
        created_by: str,
        call_verifier: GatewayCallVerifier | None = None,
    ) -> GatewayPolicyCutover:
        """Create -> ACTIVE -> ledger-ID delete -> independent call probe."""
        self._validate_compiled(compiled)
        if self._validation_mode != "FAIL_ON_ANY_FINDINGS":
            raise RuntimeError(
                "Gateway shared policies require FAIL_ON_ANY_FINDINGS"
            )
        latest = self._store.get_latest_gateway_policy_cutover(
            compiled.gateway_arn
        )
        hashes = tuple(policy.policy_hash for policy in compiled.policies)
        if latest is not None:
            latest_hashes = tuple(
                policy.policy_hash for policy in latest.policies
            )
            if (
                latest_hashes == hashes
                and latest.phase is GatewayPolicyCutoverPhase.COMPLETED
            ):
                if self.observe(latest):
                    return latest
                return self._update(
                    latest,
                    phase=GatewayPolicyCutoverPhase.UNKNOWN,
                    call_verification=GatewayCallVerification(
                        status="required",
                        detail=(
                            "previous call evidence was invalidated by "
                            "shared policy live drift"
                        ),
                    ),
                    findings=(
                        "completed shared policy live read-back is "
                        "missing, inactive, unobservable, or drifted",
                    ),
                )
            if latest.phase not in {
                GatewayPolicyCutoverPhase.COMPLETED,
                GatewayPolicyCutoverPhase.FAILED,
            }:
                if latest_hashes != hashes:
                    raise RuntimeError(
                        "a different Gateway policy cutover is already in progress"
                    )
                return self._resume(
                    latest,
                    call_verifier=call_verifier,
                )
        expected_revision = latest.revision if latest is not None else 0
        revision = expected_revision + 1
        now = self._now()
        cutover = GatewayPolicyCutover(
            gateway_arn=compiled.gateway_arn,
            revision=revision,
            phase=GatewayPolicyCutoverPhase.CREATING,
            policies=tuple(
                GatewaySharedPolicyDeployment(
                    policy_key=policy.policy_key,
                    policy_name=self.policy_name(
                        compiled.gateway_arn,
                        revision,
                        policy.policy_key,
                    ),
                    cedar_policy=policy.cedar_policy,
                    policy_hash=policy.policy_hash,
                    target_count=policy.target_count,
                    size_bytes=policy.size_bytes,
                )
                for policy in compiled.policies
            ),
            created_at=now,
            created_by=created_by,
            updated_at=now,
        )
        self._store.put_gateway_policy_cutover(
            cutover,
            expected_revision=expected_revision,
        )
        return self._resume(cutover, call_verifier=call_verifier)

    @staticmethod
    def _validate_compiled(
        compiled: CompiledSharedGatewayPolicies,
    ) -> None:
        # 계약: **굵은 문 정확히 한 장**이에요 (ADR-0099 결정 8).
        #
        # 옛 계약은 「굵은 문 1 + danger 백스톱 1장 이상」이었어요. 백스톱이 없어져서
        # (`agent_policy_compiler.compile_shared_gateway_policies` docstring) 그 요구를
        # **다른 것으로 바꾸지 않고** 정확히 한 장으로 좁혀요. 「1장 이상」으로 느슨하게 두면
        # 옛 백스톱이 섞여 들어와도 이 검증기가 통과시켜요.
        policies = compiled.policies
        keys = tuple(policy.policy_key for policy in policies)
        if (
            not compiled.gateway_arn.startswith("arn:")
            or keys != ("coarse-gate",)
        ):
            raise ValueError(
                "shared policy set must contain exactly one coarse gate"
            )
        coarse = policies[0]
        if coarse.target_count != 0:
            raise ValueError("coarse gate cannot own Target entries")
        for policy in policies:
            encoded = policy.cedar_policy.encode("utf-8")
            if (
                not encoded
                or len(encoded) != policy.size_bytes
                or len(encoded) > MAX_CEDAR_POLICY_BYTES
                or hashlib.sha256(encoded).hexdigest()
                != policy.policy_hash
            ):
                raise ValueError(
                    f"shared policy metadata mismatch: {policy.policy_key}"
                )

    def observe(self, cutover: GatewayPolicyCutover) -> bool:
        """Return true only when every ledger ID is currently ACTIVE and exact."""
        if not cutover.policies:
            return False
        for policy in cutover.policies:
            if not policy.agentcore_policy_id:
                return False
            try:
                response = self._client.get_policy(
                    policy_engine_id=self._engine_id,
                    policy_id=policy.agentcore_policy_id,
                )
            except Exception:  # noqa: BLE001 - unobservable is not active
                return False
            remote = response.get("policy", response)
            statement = (
                remote.get("definition", {})
                .get("cedar", {})
                .get("statement", "")
            )
            if (
                remote.get("status") != "ACTIVE"
                or self.normalize_cedar(statement)
                != self.normalize_cedar(policy.cedar_policy)
            ):
                return False
        return True

    def _resume(
        self,
        cutover: GatewayPolicyCutover,
        *,
        call_verifier: GatewayCallVerifier | None,
    ) -> GatewayPolicyCutover:
        if cutover.phase is GatewayPolicyCutoverPhase.COMPLETED:
            return cutover
        if cutover.phase is GatewayPolicyCutoverPhase.FAILED:
            return cutover
        if cutover.phase is GatewayPolicyCutoverPhase.UNKNOWN:
            cutover = self._set_phase(
                cutover,
                GatewayPolicyCutoverPhase.AWAITING_ACTIVE,
                findings=(),
            )

        backstops = tuple(
            index
            for index, policy in enumerate(cutover.policies)
            if policy.policy_key != "coarse-gate"
        )
        gates = tuple(
            index
            for index, policy in enumerate(cutover.policies)
            if policy.policy_key == "coarse-gate"
        )
        for indexes in (backstops, gates):
            for index in indexes:
                cutover = self._create(cutover, index)
                if cutover.phase in {
                    GatewayPolicyCutoverPhase.FAILED,
                    GatewayPolicyCutoverPhase.UNKNOWN,
                }:
                    return cutover
            cutover, all_active = self._observe_group(cutover, indexes)
            if cutover.phase in {
                GatewayPolicyCutoverPhase.FAILED,
                GatewayPolicyCutoverPhase.UNKNOWN,
            }:
                return cutover
            if not all_active:
                return self._set_phase(
                    cutover,
                    GatewayPolicyCutoverPhase.AWAITING_ACTIVE,
                )

        cutover = self._set_phase(
            cutover,
            GatewayPolicyCutoverPhase.DELETING_LEGACY,
        )
        cutover = self._delete_owned_predecessors(cutover)
        if cutover.phase is GatewayPolicyCutoverPhase.UNKNOWN:
            return cutover
        cutover = self._set_phase(
            cutover,
            GatewayPolicyCutoverPhase.VERIFYING_CALL,
        )
        if cutover.call_verification.qualified:
            return self._set_phase(
                cutover,
                GatewayPolicyCutoverPhase.COMPLETED,
                findings=(),
            )
        if call_verifier is None:
            return cutover
        try:
            verification = call_verifier.verify(cutover.gateway_arn)
        except Exception as exc:  # noqa: BLE001 - unobservable is not passing
            verification = GatewayCallVerification(
                status="unknown",
                evidence_source=type(exc).__name__,
                detail=str(exc)[:800],
            )
        cutover = self._update(
            cutover,
            call_verification=verification,
        )
        if not verification.qualified:
            return self._update(
                cutover,
                findings=(
                    "actual Gateway call verification is incomplete",
                ),
            )
        return self._set_phase(
            cutover,
            GatewayPolicyCutoverPhase.COMPLETED,
            findings=(),
        )

    def _create(
        self,
        cutover: GatewayPolicyCutover,
        index: int,
    ) -> GatewayPolicyCutover:
        policy = cutover.policies[index]
        if policy.agentcore_policy_id:
            return cutover
        try:
            created = self._client.create_policy(
                policy_engine_id=self._engine_id,
                name=policy.policy_name,
                definition={"cedar": {"statement": policy.cedar_policy}},
                validation_mode=self._validation_mode,
                description=(
                    "Agora Gateway shared authorization policy "
                    f"{policy.policy_key} r{cutover.revision}"
                ),
            )
            policy_id = str(created.get("policyId") or "")
            if not policy_id:
                raise RuntimeError(
                    "create_policy response did not contain policyId"
                )
        except PolicyNameConflict as exc:
            return self._set_phase(
                cutover,
                GatewayPolicyCutoverPhase.UNKNOWN,
                findings=(
                    "shared policy name conflict has no ledger ownership "
                    f"evidence: {exc}",
                ),
            )
        except Exception as exc:  # noqa: BLE001 - retain failure evidence
            return self._set_phase(
                cutover,
                GatewayPolicyCutoverPhase.FAILED,
                findings=(
                    "shared policy create failed: "
                    f"{type(exc).__name__}: {exc}",
                ),
            )
        policies = list(cutover.policies)
        policies[index] = replace(
            policy,
            agentcore_policy_id=policy_id,
            remote_status="CREATING",
        )
        try:
            return self._update(cutover, policies=tuple(policies))
        except Exception:
            try:
                self._client.delete_policy(
                    policy_engine_id=self._engine_id,
                    policy_id=policy_id,
                )
            except Exception:
                _log.exception(
                    "uncheckpointed shared policy cleanup failed: %s",
                    policy_id,
                )
            raise

    def _observe_group(
        self,
        cutover: GatewayPolicyCutover,
        indexes: tuple[int, ...],
    ) -> tuple[GatewayPolicyCutover, bool]:
        all_active = True
        for index in indexes:
            policy = cutover.policies[index]
            try:
                status, statement, findings, terminal = self._poll(
                    policy.agentcore_policy_id
                )
            except Exception as exc:  # noqa: BLE001 - persist unknown evidence
                reason = (
                    "shared policy read-back unobservable: "
                    f"{type(exc).__name__}: {str(exc)[:800]}"
                )
                observed = replace(
                    policy,
                    remote_status="UNKNOWN",
                    status_reasons=(reason,),
                )
                cutover = self._replace_policy(
                    cutover,
                    index,
                    observed,
                )
                return (
                    self._set_phase(
                        cutover,
                        GatewayPolicyCutoverPhase.UNKNOWN,
                        findings=(reason,),
                    ),
                    False,
                )
            if not terminal:
                reason = (
                    "shared policy polling exhausted before a terminal "
                    f"status was observed: status={status or 'UNKNOWN'}"
                )
                observed = replace(
                    policy,
                    remote_status=status or "UNKNOWN",
                    status_reasons=(reason,),
                )
                cutover = self._replace_policy(cutover, index, observed)
                return (
                    self._set_phase(
                        cutover,
                        GatewayPolicyCutoverPhase.UNKNOWN,
                        findings=(reason,),
                    ),
                    False,
                )
            if findings or status in ("CREATE_FAILED", "UPDATE_FAILED"):
                failed = replace(
                    policy,
                    remote_status=status or "UNKNOWN",
                    status_reasons=tuple(
                        findings or [f"status={status or 'UNKNOWN'}"]
                    ),
                )
                cutover = self._replace_policy(cutover, index, failed)
                return (
                    self._set_phase(
                        cutover,
                        GatewayPolicyCutoverPhase.FAILED,
                        findings=failed.status_reasons,
                    ),
                    False,
                )
            if status != "ACTIVE":
                all_active = False
                observed = replace(
                    policy,
                    remote_status=status or "UNKNOWN",
                    status_reasons=(f"status={status or 'UNKNOWN'}",),
                )
            elif self.normalize_cedar(statement) != self.normalize_cedar(
                policy.cedar_policy
            ):
                observed = replace(
                    policy,
                    remote_status=status,
                    status_reasons=("read-back mismatch",),
                )
                cutover = self._replace_policy(cutover, index, observed)
                return (
                    self._set_phase(
                        cutover,
                        GatewayPolicyCutoverPhase.FAILED,
                        findings=("shared policy read-back mismatch",),
                    ),
                    False,
                )
            else:
                deployed_hash = hashlib.sha256(
                    self.normalize_cedar(statement).encode("utf-8")
                ).hexdigest()
                observed = replace(
                    policy,
                    remote_status="ACTIVE",
                    deployed_policy_hash=deployed_hash,
                    status_reasons=(),
                )
            cutover = self._replace_policy(cutover, index, observed)
        return cutover, all_active

    def _replace_policy(
        self,
        cutover: GatewayPolicyCutover,
        index: int,
        policy: GatewaySharedPolicyDeployment,
    ) -> GatewayPolicyCutover:
        policies = list(cutover.policies)
        policies[index] = policy
        return self._update(cutover, policies=tuple(policies))

    def _delete_owned_predecessors(
        self,
        cutover: GatewayPolicyCutover,
    ) -> GatewayPolicyCutover:
        inventory = self._owned_predecessor_inventory(cutover)
        if isinstance(inventory, str):
            return self._set_phase(
                cutover,
                GatewayPolicyCutoverPhase.UNKNOWN,
                findings=(inventory,),
            )
        candidates, unmanaged, preserved, owners = inventory
        cutover = self._update(
            cutover,
            unmanaged_policy_ids=unmanaged,
            preserved_policy_ids=preserved,
        )
        if unmanaged:
            return self._set_phase(
                cutover,
                GatewayPolicyCutoverPhase.UNKNOWN,
                findings=(
                    "live policies without ledger ownership must be "
                    "resolved before cutover: "
                    + ", ".join(unmanaged),
                ),
            )
        requested = (
            set(cutover.delete_requested_policy_ids)
            | set(candidates)
        )
        cutover = self._update(
            cutover,
            delete_requested_policy_ids=tuple(sorted(requested)),
        )
        delete_errors: dict[str, str] = {}
        for policy_id in sorted(requested):
            try:
                self._client.delete_policy(
                    policy_engine_id=self._engine_id,
                    policy_id=policy_id,
                )
            except Exception as exc:  # noqa: BLE001 - continue siblings
                delete_errors[policy_id] = (
                    f"{policy_id}: {type(exc).__name__}: {exc}"
                )
        confirmed, confirmation_errors = self._confirm_absence(
            requested,
            gateway_arn=cutover.gateway_arn,
        )
        cutover = self._update(
            cutover,
            deleted_policy_ids=tuple(sorted(
                set(cutover.deleted_policy_ids) | confirmed
            )),
        )
        errors = [
            detail
            for policy_id, detail in sorted(delete_errors.items())
            if policy_id not in confirmed
        ]
        errors.extend(confirmation_errors)
        ledger_ids = (
            confirmed
            | (
                set(cutover.deleted_policy_ids)
                & set(owners)
            )
        )
        for policy_id in sorted(ledger_ids):
            for deployment in owners[policy_id]:
                if (
                    deployment.status
                    is PolicyDeploymentStatus.SUPERSEDED
                ):
                    continue
                try:
                    self._store.mark_agent_policy_deployment(
                        deployment.agent_record_id,
                        deployment.revision,
                        status=PolicyDeploymentStatus.SUPERSEDED,
                    )
                except Exception as exc:  # noqa: BLE001 - ledger is evidence
                    errors.append(
                        f"{policy_id} ledger update: "
                        f"{type(exc).__name__}: {exc}"
                    )
        if errors:
            return self._set_phase(
                cutover,
                GatewayPolicyCutoverPhase.UNKNOWN,
                findings=tuple(errors),
            )
        return cutover

    def _owned_predecessor_inventory(
        self,
        cutover: GatewayPolicyCutover,
    ) -> (
        tuple[
            tuple[str, ...],
            tuple[str, ...],
            tuple[str, ...],
            dict[str, tuple[AgentPolicyDeployment, ...]],
        ]
        | str
    ):
        try:
            deployments = self._store.list_all_agent_policy_deployments()
        except Exception as exc:  # noqa: BLE001 - ownership unobservable
            return (
                "legacy policy ownership unknown: "
                f"{type(exc).__name__}: {exc}"
            )
        all_owners: dict[str, list[AgentPolicyDeployment]] = {}
        for deployment in deployments:
            if (
                deployment.gateway_id == cutover.gateway_arn
                and deployment.agentcore_policy_id
            ):
                all_owners.setdefault(
                    deployment.agentcore_policy_id,
                    [],
                ).append(deployment)
        ambiguous = {
            policy_id
            for policy_id, items in all_owners.items()
            if len({
                (item.agent_record_id, item.revision)
                for item in items
            }) != 1
        }
        if ambiguous:
            return (
                "legacy policy ownership unknown: duplicate ledger policyId "
                + ", ".join(sorted(ambiguous))
            )
        preserved_ids = {
            policy_id
            for policy_id, items in all_owners.items()
            if items
            and _DEV_CREDENTIAL_AGENT_ID.fullmatch(
                items[0].agent_record_id
            )
        }
        owners = {
            policy_id: items
            for policy_id, items in all_owners.items()
            if policy_id not in preserved_ids
        }
        try:
            prior_cutovers = tuple(
                self._store.list_gateway_policy_cutovers(
                    cutover.gateway_arn
                )
            )
        except Exception as exc:  # noqa: BLE001
            return (
                "shared policy ownership unknown: "
                f"{type(exc).__name__}: {exc}"
            )
        shared_claims: dict[str, list[tuple[int, str]]] = {}
        for prior in prior_cutovers:
            if prior.revision >= cutover.revision:
                continue
            for policy in prior.policies:
                if policy.agentcore_policy_id:
                    shared_claims.setdefault(
                        policy.agentcore_policy_id,
                        [],
                    ).append((prior.revision, policy.policy_key))
        ambiguous_shared = {
            policy_id
            for policy_id, claims in shared_claims.items()
            if len(claims) != 1 or policy_id in all_owners
        }
        if ambiguous_shared:
            return (
                "shared policy ownership unknown: duplicate ledger policyId "
                + ", ".join(sorted(ambiguous_shared))
            )
        try:
            response = self._client.list_policies(
                policy_engine_id=self._engine_id,
                target_resource_scope=cutover.gateway_arn,
            )
            live_policies = self._policy_items(response)
        except Exception as exc:  # noqa: BLE001
            return (
                "policy listing incomplete: "
                f"{type(exc).__name__}: {exc}"
            )
        if response.get("nextToken"):
            return "policy listing incomplete: unconsumed nextToken returned"
        current_ids = {
            policy.agentcore_policy_id
            for policy in cutover.policies
            if policy.agentcore_policy_id
        }
        live_ids: set[str] = set()
        for policy in live_policies:
            if (
                policy.get("targetResourceScope", cutover.gateway_arn)
                != cutover.gateway_arn
            ):
                continue
            policy_id = str(policy.get("policyId") or "")
            if not policy_id:
                return "policy ownership unknown: live policyId is missing"
            live_ids.add(policy_id)
        owned_ids = set(owners) | set(shared_claims)
        active_legacy_ids = {
            policy_id
            for policy_id, items in owners.items()
            if any(
                item.status is not PolicyDeploymentStatus.SUPERSEDED
                for item in items
            )
        }
        candidates = tuple(sorted(
            (
                (live_ids & owned_ids)
                | active_legacy_ids
            )
            - current_ids
        ))
        unmanaged = tuple(sorted(
            live_ids
            - set(candidates)
            - current_ids
            - preserved_ids
        ))
        preserved = tuple(sorted(live_ids & preserved_ids))
        frozen_owners = {
            policy_id: tuple(items)
            for policy_id, items in owners.items()
        }
        for policy_id in shared_claims:
            frozen_owners[policy_id] = ()
        return candidates, unmanaged, preserved, frozen_owners

    def _update(
        self,
        cutover: GatewayPolicyCutover,
        **changes,
    ) -> GatewayPolicyCutover:
        updated = replace(
            cutover,
            **changes,
            updated_at=self._now(),
            version=cutover.version + 1,
        )
        self._store.update_gateway_policy_cutover(
            updated,
            expected_version=cutover.version,
        )
        return updated

    def _set_phase(
        self,
        cutover: GatewayPolicyCutover,
        phase: GatewayPolicyCutoverPhase,
        *,
        findings: tuple[str, ...] | None = None,
    ) -> GatewayPolicyCutover:
        changes = {"phase": phase}
        if findings is not None:
            changes["findings"] = findings
        return self._update(cutover, **changes)

    def _poll(
        self,
        policy_id: str,
    ) -> tuple[str, str, list[str], bool]:
        status, statement, findings = "", "", []
        terminal = False
        for attempt in range(max(1, self._max_polls)):
            response = self._client.get_policy(
                policy_engine_id=self._engine_id,
                policy_id=policy_id,
            )
            policy = response.get("policy", response)
            status = str(policy.get("status") or "")
            statement = str(
                policy.get("definition", {})
                .get("cedar", {})
                .get("statement", "")
            )
            findings = list(policy.get("statusReasons", ()) or ())
            if status in ("ACTIVE", "CREATE_FAILED", "UPDATE_FAILED"):
                terminal = True
                break
            if attempt + 1 < max(1, self._max_polls):
                self._sleep(2)
        return status, statement, findings, terminal

    @staticmethod
    def _policy_items(response: object) -> tuple[dict, ...]:
        if not isinstance(response, dict) or "policies" not in response:
            raise ValueError("list_policies response is missing policies")
        policies = response["policies"]
        if not isinstance(policies, (list, tuple)) or not all(
            isinstance(policy, dict) for policy in policies
        ):
            raise ValueError("list_policies policies is not an object list")
        return tuple(policies)

    def _confirm_absence(
        self,
        policy_ids: set[str],
        *,
        gateway_arn: str,
    ) -> tuple[set[str], list[str]]:
        remaining = set(policy_ids)
        for attempt in range(max(1, self._max_polls)):
            try:
                response = self._client.list_policies(
                    policy_engine_id=self._engine_id,
                    target_resource_scope=gateway_arn,
                )
                live_policies = self._policy_items(response)
            except Exception as exc:  # noqa: BLE001
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
                for policy in live_policies
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
