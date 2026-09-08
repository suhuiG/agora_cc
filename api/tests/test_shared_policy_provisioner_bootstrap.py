"""공유 Gateway 정책 프로비저닝의 빈 원장 처리.

세 상태를 구분해야 해요. 셋이 섞이면 신규 계정의 첫 MCP 배포가 막히거나, 반대로 살아 있는
Cedar 리비전이 빈 집합으로 덮여요.

| 원장 관측 | 엔진의 우리 정책 | 기대 |
| --- | --- | --- |
| 성공, 0건 | 0장 | `unchanged` (만들 것이 없음) |
| 성공, 0건 | 1장 이상 | `refused` (기존 리비전 보존) |
| 실패 | 무관 | `unknown` (관측 실패를 0건으로 접지 않음) |
"""
from __future__ import annotations

import pytest

from agora.domains.identity.agent_policy_compiler import SharedGatewayPolicySpec
from agora.domains.identity.models import AgentAuthorizationLedgerSnapshot
from agora.domains.identity.shared_policy_provisioner import (
    _NAME_PREFIX,
    SharedPolicyProvisioner,
    _gateway_hash,
)

GATEWAY_ID = "agora-m2-oauth-dev-abc123"
GATEWAY_ARN = (
    "arn:aws:bedrock-agentcore:ap-northeast-2:111122223333:gateway/" + GATEWAY_ID
)
ENGINE_ID = "AgoraM2OAuthGatewayDev-abc123"
SCOPE = "https://agora-m2-oauth-dev/invoke"


class FakeControl:
    """`SharedPolicyProvisioner` 가 부르는 control-plane 호출만 흉내내요."""

    def __init__(self, policies=()):
        self.policies = list(policies)
        self.created: list[dict] = []

    def get_gateway(self, **_kw):
        return {
            "interceptorConfigurations": [{"interceptionPoints": ["REQUEST"]}],
        }

    def list_gateway_targets(self, **_kw):
        return {"items": []}

    def list_policies(self, **_kw):
        # 응답 키는 `policies` 예요. `items` 로 주면 provisioner 가 RuntimeError 를 내요.
        return {"policies": self.policies}

    def create_policy(self, **kw):
        self.created.append(kw)
        return {"policyId": "p-1", "status": "ACTIVE"}


class FakeStore:
    """④ binding·② rule 원장. `boom=True` 면 관측 실패를 흉내내요."""

    def __init__(self, *, boom: bool = False):
        self.boom = boom

    def get_agent_authorization_ledger_snapshot(self):
        if self.boom:
            raise RuntimeError("DynamoDB unavailable")
        return AgentAuthorizationLedgerSnapshot()

    def list_agent_tool_bindings(self, _agent_id):
        return []

    def list_domain_policy_rules(self):
        return []


def _spec():
    return SharedGatewayPolicySpec(
        gateway_arn=GATEWAY_ARN,
        targets=(),
        invoke_scope=SCOPE,
        scope_names=(SCOPE,),
    )


def _provision(control, store):
    return SharedPolicyProvisioner(control, store, sleep=lambda *_: None).provision(
        gateway_id=GATEWAY_ID,
        engine_id=ENGINE_ID,
        spec_without_interceptor=_spec(),
        created_by="test",
    )


def _owned_policy_name(revision: int = 1) -> str:
    return f"{_NAME_PREFIX}{_gateway_hash(GATEWAY_ARN)}_r{revision}"


def test_empty_ledger_with_no_live_policy_is_a_noop():
    """신규 계정: 선언이 0건이고 엔진도 비어 있으면 만들 것이 없어요.

    이걸 거부로 다루면 첫 MCP 배포가 `registering_target` 에서 죽어요 — 도구 인가 승인은
    배포 **후**에 하는 절차라 아무도 첫 배포를 통과할 수 없어요.
    """
    control = FakeControl(policies=[])
    report = _provision(control, FakeStore())

    assert report.ok is True
    assert report.verdict == "unchanged"
    assert control.created == []


def test_empty_ledger_with_live_policy_still_refuses():
    """살아 있는 리비전이 있으면 빈 집합으로 덮지 않아요.

    가드가 원래 막으려던 상태예요. 위 no-op 이 이걸 무장해제하지 않았음을 여기서 고정해요.
    """
    control = FakeControl(policies=[
        {"name": _owned_policy_name(), "policyId": "p-live"},
    ])
    report = _provision(control, FakeStore())

    assert report.ok is False
    assert report.verdict == "refused"
    assert control.created == []


def test_ledger_read_failure_is_unknown_not_empty():
    """원장을 못 읽은 것과 0건은 달라요. 못 읽었으면 아무것도 바꾸지 않아요."""
    control = FakeControl(policies=[])
    report = _provision(control, FakeStore(boom=True))

    assert report.ok is False
    assert report.verdict == "unknown"
    assert control.created == []


@pytest.mark.parametrize("policies", [[], [{"name": "Gateway_other_r1", "policyId": "x"}]])
def test_other_gateway_policies_do_not_count_as_ours(policies):
    """다른 Gateway 의 정책은 우리 리비전이 아니에요 — no-op 판정을 막지 않아요."""
    control = FakeControl(policies=policies)
    report = _provision(control, FakeStore())

    assert report.verdict == "unchanged"
    assert control.created == []
