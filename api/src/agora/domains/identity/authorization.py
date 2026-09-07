"""AccessGrant 교집합 판정과 ALLOW/DENY 감사."""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from functools import partial
from threading import Lock

from .models import (
    ApprovalState,
    AssetCapabilityStatus,
    AuditEvent,
    AuthorizationDecision,
    AuthorizationMode,
    AuthorizationOutcome,
    CapabilityStatus,
    ConnectionStatus,
    DecisionReason,
    DelegationContext,
    DesiredState,
    GrantStatus,
    SecurityFailureType,
    SecurityRejectionEvent,
)
from .store import IdentityRecordNotFound, IdentityStore

_security_logger = logging.getLogger("agora.security")
_authorization_logger = logging.getLogger("agora.authorization")


class AuditWriteError(RuntimeError):
    pass


def record_security_rejection(
    *,
    reason: DecisionReason,
    failure_type: SecurityFailureType,
    timestamp: str | None = None,
    invocation_id: str = "",
    workload_id: str = "",
    principal_id: str = "",
    agent_id: str = "",
    asset_id: str = "",
    operation_id: str = "",
) -> None:
    event = SecurityRejectionEvent(
        event_type="SECURITY_REJECTION",
        severity="WARNING",
        reason_code=reason.value,
        failure_type=failure_type.value,
        timestamp=timestamp or _format_event_timestamp(time.time_ns()),
        invocation_id=invocation_id,
        workload_id=workload_id,
        principal_id=principal_id,
        agent_id=agent_id,
        asset_id=asset_id,
        operation_id=operation_id,
    )
    _security_logger.warning(
        json.dumps(asdict(event), separators=(",", ":"), sort_keys=True)
    )


class AuthorizationService:
    def __init__(
        self,
        store: IdentityStore,
        *,
        asset_version=None,
        now=None,
        new_id=None,
        event_clock=None,
    ) -> None:
        self.store = store
        self._asset_version = asset_version or (lambda _asset_id: "unknown")
        self._now = now or (lambda: int(time.time()))
        self._new_id = new_id or (lambda: uuid.uuid4().hex)
        self._event_clock = event_clock or time.time_ns
        self._event_lock = Lock()
        self._last_event_ns = 0

    def decide(
        self,
        context: DelegationContext,
        *,
        asset_id: str,
        operation_id: str,
        mode: AuthorizationMode = AuthorizationMode.LEGACY_DELEGATED,
        trace_id: str = "",
        span_id: str = "",
    ) -> AuthorizationDecision:
        connection_id = ""
        capabilities: tuple[str, ...] = ()
        target = ""
        record = partial(
            self._record,
            context,
            trace_id=trace_id,
            span_id=span_id,
        )

        if asset_id not in context.allowed_asset_ids:
            # 유효한 delegation이지만 그 delegation에 없는 자산 → ASSET_NOT_DELEGATED.
            # (delegation 자체가 무효인 경우의 INVALID_DELEGATION과 구분 — ADR-015,
            #  docs/identity-delegated-access.md의 안정 reason code 계약.)
            return record(
                asset_id=asset_id,
                operation_id=operation_id,
                connection_id=connection_id,
                capabilities=capabilities,
                target=target,
                outcome=AuthorizationOutcome.DENY,
                reason=DecisionReason.ASSET_NOT_DELEGATED,
                mode=mode,
            )

        active_asset_version = self._asset_version(asset_id)
        if not active_asset_version:
            return record(
                asset_id=asset_id,
                operation_id=operation_id,
                connection_id=connection_id,
                capabilities=capabilities,
                target=target,
                outcome=AuthorizationOutcome.DENY,
                reason=DecisionReason.ASSET_INACTIVE,
                mode=mode,
            )

        if mode is AuthorizationMode.AGENT_POLICY:
            # policy + identity(IA-19): 승인된 AgentToolBinding만으로 판정해요. delegated-access의
            # AssetCapability ∩ Connection/Capability ∩ ceiling ∩ 사용자 Grant(3중 체크)는 요구하지
            # 않아요 — 최종 강제선은 Gateway ENFORCE(Cedar, agent 신원 principal)이고 decide는 그와
            # 정합한 advisory예요. active_asset_version을 쓰므로 MCP 버전 불일치 binding은 자동 제외.
            try:
                binding = self.store.get_agent_tool_binding(
                    context.agent_id, asset_id, active_asset_version, operation_id
                )
            except IdentityRecordNotFound:
                binding = None
            if (
                binding is None
                or binding.approval_state is not ApprovalState.APPROVED
                or binding.desired_state is not DesiredState.ALLOWED
            ):
                return record(
                    asset_id=asset_id,
                    operation_id=operation_id,
                    connection_id=connection_id,
                    capabilities=capabilities,
                    target=target,
                    outcome=AuthorizationOutcome.DENY,
                    reason=DecisionReason.AGENT_TOOL_NOT_BOUND,
                    mode=mode,
                )
            return record(
                asset_id=asset_id,
                operation_id=operation_id,
                connection_id=connection_id,
                capabilities=capabilities,
                target=target,
                outcome=AuthorizationOutcome.ALLOW,
                reason=DecisionReason.ALLOWED,
                mode=mode,
            )

        # 이하 LEGACY_DELEGATED 경로 — AssetCapability ∩ Connection ∩ ceiling ∩ Grant(3중 체크).
        try:
            asset_capability = self.store.get_asset_capability(asset_id, operation_id)
        except IdentityRecordNotFound:
            return record(
                asset_id=asset_id,
                operation_id=operation_id,
                connection_id=connection_id,
                capabilities=capabilities,
                target=target,
                outcome=AuthorizationOutcome.DENY,
                reason=DecisionReason.ASSET_CAPABILITY_NOT_FOUND,
                mode=mode,
            )

        connection_id = asset_capability.connection_id
        capabilities = tuple(sorted(set(asset_capability.required_capabilities)))
        if asset_capability.asset_version != active_asset_version:
            return record(
                asset_id=asset_id,
                operation_id=operation_id,
                connection_id=connection_id,
                capabilities=capabilities,
                target=target,
                outcome=AuthorizationOutcome.DENY,
                reason=DecisionReason.ASSET_VERSION_MISMATCH,
                mode=mode,
            )
        if asset_capability.status is not AssetCapabilityStatus.APPROVED:
            return record(
                asset_id=asset_id,
                operation_id=operation_id,
                connection_id=connection_id,
                capabilities=capabilities,
                target=target,
                outcome=AuthorizationOutcome.DENY,
                reason=DecisionReason.ASSET_CAPABILITY_NOT_APPROVED,
                mode=mode,
            )

        try:
            connection = self.store.get_connection(connection_id)
        except IdentityRecordNotFound:
            connection = None
        if connection is None or connection.status is not ConnectionStatus.ACTIVE:
            return record(
                asset_id=asset_id,
                operation_id=operation_id,
                connection_id=connection_id,
                capabilities=capabilities,
                target=target,
                outcome=AuthorizationOutcome.DENY,
                reason=DecisionReason.CONNECTION_INACTIVE,
                mode=mode,
            )
        target = connection.target

        active_capabilities = {
            item.name
            for item in self.store.list_connection_capabilities(connection_id)
            if item.status is CapabilityStatus.ACTIVE
        }
        required = set(capabilities)
        if not required or not required.issubset(active_capabilities):
            return record(
                asset_id=asset_id,
                operation_id=operation_id,
                connection_id=connection_id,
                capabilities=capabilities,
                target=target,
                outcome=AuthorizationOutcome.DENY,
                reason=DecisionReason.CAPABILITY_INACTIVE,
                mode=mode,
            )

        ceiling = set(connection.ceiling)
        if not required.issubset(ceiling):
            return record(
                asset_id=asset_id,
                operation_id=operation_id,
                connection_id=connection_id,
                capabilities=capabilities,
                target=target,
                outcome=AuthorizationOutcome.DENY,
                reason=DecisionReason.CAPABILITY_OUTSIDE_CEILING,
                mode=mode,
            )

        now = self._now()
        granted = {
            capability
            for grant in self.store.list_grants(
                principal_id=context.principal_id,
                connection_id=connection_id,
            )
            if grant.status is GrantStatus.ACTIVE
            and (grant.expires_at is None or grant.expires_at > now)
            for capability in grant.capabilities
        }
        if not required.issubset(granted):
            return record(
                asset_id=asset_id,
                operation_id=operation_id,
                connection_id=connection_id,
                capabilities=capabilities,
                target=target,
                outcome=AuthorizationOutcome.DENY,
                reason=DecisionReason.CAPABILITY_NOT_GRANTED,
                mode=mode,
            )

        return record(
            asset_id=asset_id,
            operation_id=operation_id,
            connection_id=connection_id,
            capabilities=capabilities,
            target=target,
            outcome=AuthorizationOutcome.ALLOW,
            reason=DecisionReason.ALLOWED,
            mode=mode,
        )

    def _record(
        self,
        context: DelegationContext,
        *,
        asset_id: str,
        operation_id: str,
        connection_id: str,
        capabilities: tuple[str, ...],
        target: str,
        outcome: AuthorizationOutcome,
        reason: DecisionReason,
        mode: AuthorizationMode = AuthorizationMode.LEGACY_DELEGATED,
        trace_id: str = "",
        span_id: str = "",
    ) -> AuthorizationDecision:
        timestamp = self._next_event_timestamp()
        # 유효 delegation의 allowlist 위반(ASSET_NOT_DELEGATED)은 API 계약상 그 reason을
        # 그대로 응답하되, 별도 security 스트림에도 기록해요. reason code(응답 계약)와
        # security event 여부를 분리해 계약을 보존해요.
        is_security_rejection = reason is DecisionReason.ASSET_NOT_DELEGATED
        failure_type = (
            SecurityFailureType.DELEGATION_ASSET_NOT_ALLOWED.value
            if is_security_rejection
            else ""
        )
        event = AuditEvent(
            event_id=self._new_id(),
            invocation_id=context.invocation_id,
            principal_id=context.principal_id,
            agent_id=context.agent_id,
            asset_id=asset_id,
            operation_id=operation_id,
            connection_id=connection_id,
            capabilities=capabilities,
            decision=outcome,
            reason=reason,
            target=target,
            timestamp=timestamp,
            workload_id=context.workload_id,
            event_type=(
                "SECURITY_REJECTION"
                if is_security_rejection
                else "AUTHORIZATION_DECISION"
            ),
            severity="WARNING" if is_security_rejection else "INFO",
            failure_type=failure_type,
            mode=mode.value,
            trace_id=trace_id,
            span_id=span_id,
        )
        if is_security_rejection:
            record_security_rejection(
                reason=reason,
                failure_type=SecurityFailureType.DELEGATION_ASSET_NOT_ALLOWED,
                timestamp=timestamp,
                invocation_id=context.invocation_id,
                workload_id=context.workload_id,
                principal_id=context.principal_id,
                agent_id=context.agent_id,
                asset_id=asset_id,
                operation_id=operation_id,
            )
        try:
            self.store.append_audit(event)
        except Exception as exc:
            raise AuditWriteError("authorization 감사 기록에 실패했어요.") from exc
        _authorization_logger.info(
            json.dumps(
                {
                    "decision": outcome.value,
                    "invocation_id": context.invocation_id,
                    "reason": reason.value,
                    "span_id": span_id,
                    "trace_id": trace_id,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return AuthorizationDecision(
            decision=outcome,
            reason=reason,
            invocation_id=context.invocation_id,
            principal_id=context.principal_id,
            connection_id=connection_id,
            capabilities=capabilities,
            trace_id=trace_id,
        )

    def _next_event_timestamp(self) -> str:
        with self._event_lock:
            event_ns = max(int(self._event_clock()), self._last_event_ns + 1)
            self._last_event_ns = event_ns
        return _format_event_timestamp(event_ns)


def _format_event_timestamp(event_ns: int) -> str:
    seconds, nanoseconds = divmod(event_ns, 1_000_000_000)
    prefix = datetime.fromtimestamp(seconds, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    return f"{prefix}.{nanoseconds:09d}Z"
