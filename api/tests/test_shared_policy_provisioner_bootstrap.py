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

from agora.domains.identity.agent_policy_compiler import (
    GatewayPolicyTarget,
    SharedGatewayPolicySpec,
)
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
    """`SharedPolicyProvisioner` 가 부르는 control-plane 호출만 흉내내요.

    `interceptor` 로 부착 관측 결과를 고를 수 있어요 — `True` 부착, `False` 미부착,
    `None` 관측 실패(`get_gateway` 가 던짐).
    """

    def __init__(self, policies=(), *, interceptor: bool | None = True):
        self.policies = list(policies)
        self.interceptor = interceptor
        self.created: list[dict] = []

    def get_gateway(self, **_kw):
        if self.interceptor is None:
            raise RuntimeError("throttled")
        if self.interceptor is False:
            return {"interceptorConfigurations": []}
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


def _spec(**over):
    fields = dict(
        gateway_arn=GATEWAY_ARN,
        targets=(),
        invoke_scope=SCOPE,
        scope_names=(SCOPE,),
    )
    fields.update(over)
    return SharedGatewayPolicySpec(**fields)


def _provision(control, store, spec=None):
    return SharedPolicyProvisioner(control, store, sleep=lambda *_: None).provision(
        gateway_id=GATEWAY_ID,
        engine_id=ENGINE_ID,
        spec_without_interceptor=spec or _spec(),
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


def test_noop_requires_a_completely_empty_engine():
    """`Gateway_` 밖의 계열이 남아 있으면 no-op 으로 접지 않아요.

    `_owned_policies` 는 `Gateway_{hash}_` 만 세요. `DomainRule_*` 같은 다른 계열의 잔존
    permit 을 「없음」으로 읽으면, 폐기된 permit 이 실제 인가를 계속 허용하는데도 no-op 이
    성공을 보고해요.
    """
    control = FakeControl(policies=[{"name": "DomainRule_stale", "policyId": "p-orphan"}])
    report = _provision(control, FakeStore())

    assert report.ok is False
    assert report.verdict == "refused"
    assert control.created == []


@pytest.mark.parametrize(
    ("interceptor", "label"),
    [(False, "미부착이 관측됨"), (None, "부착 여부를 관측하지 못함")],
)
def test_noop_requires_interceptor_attached(interceptor, label):
    """interceptor 부착은 컴파일러가 무조건 요구하는 선행 조건이에요.

    도구 단위 ④·⑦ 판정은 REQUEST interceptor 가 해요. 강제 지점이 떨어진 Gateway 를
    「바꿀 것 없음」으로 보고하면 그 배포가 성공으로 넘어가요. 관측 실패도 통과로 접지 않아요.
    """
    control = FakeControl(policies=[], interceptor=interceptor)
    report = _provision(control, FakeStore())

    assert report.verdict == "refused", label
    assert report.ok is False
    assert "interceptor" in report.reason
    assert control.created == []


def test_structural_checks_still_run_on_an_empty_bootstrap():
    """빈 원장·빈 엔진이어도 컴파일러의 **구조 검사**는 그대로 돌아요.

    빈 선언 가드만 끄는 플래그를 넘기지 않고 compile 앞에서 조기 반환하면, gateway ARN 형식·
    scope 접두어 충돌·Target 이름 중복 같은 검사를 전부 건너뛰어요. 그러면 잘못된 bootstrap
    입력이 «바꿀 것 없음» 으로 성공 보고돼요. 이 테스트가 그 우회를 막아요.
    """
    control = FakeControl(policies=[])
    report = _provision(control, FakeStore(), spec=_spec(gateway_arn="not-an-arn"))

    assert report.ok is False
    assert report.verdict == "refused"
    assert "gateway ARN" in report.reason
    assert control.created == []


def test_duplicate_target_names_are_refused_on_an_empty_bootstrap():
    """같은 축의 다른 구조 검사(Target 이름 중복)도 살아 있는지 확인해요."""
    dup = (
        GatewayPolicyTarget(name="t1", sensitivity="low", operations=("read",)),
        GatewayPolicyTarget(name="t1", sensitivity="low", operations=("read",)),
    )
    control = FakeControl(policies=[])
    report = _provision(control, FakeStore(), spec=_spec(targets=dup))

    assert report.ok is False
    assert report.verdict == "refused"
    assert control.created == []
