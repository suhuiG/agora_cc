"""agent 호출 자격 판정 — 소유자/그룹 기반 default-deny (스펙 §4.8, 순수 함수).

per-user tool 판정을 미루는 대신 두는 유일한 사용자층 통제선이에요(confused-deputy 완화, §8.4).
파괴적 operation 예외는 두지 않아요 — 판정 축은 "이 사용자가 이 agent를 부를 수 있는가" 하나예요.
"""
from __future__ import annotations

from .models import (
    AgentInvokeAuthorization,
    AgentInvokeDecision,
    AgentInvokeReason,
    AuthorizationOutcome,
)


def authorize_agent_invoke(
    authz: AgentInvokeAuthorization,
    *,
    caller_principal_id: str,
    caller_groups: tuple[str, ...],
) -> AgentInvokeDecision:
    def _decide(outcome: AuthorizationOutcome, reason: AgentInvokeReason):
        return AgentInvokeDecision(
            decision=outcome,
            reason=reason,
            agent_id=authz.agent_id,
            caller_principal_id=caller_principal_id,
        )

    if caller_principal_id and caller_principal_id == authz.owner_principal_id:
        return _decide(AuthorizationOutcome.ALLOW, AgentInvokeReason.OWNER)
    if caller_principal_id in authz.allowed_principals:
        return _decide(AuthorizationOutcome.ALLOW, AgentInvokeReason.PRINCIPAL_ALLOWED)
    if set(caller_groups) & set(authz.allowed_groups):
        return _decide(AuthorizationOutcome.ALLOW, AgentInvokeReason.GROUP_ALLOWED)
    return _decide(AuthorizationOutcome.DENY, AgentInvokeReason.NOT_AUTHORIZED)
