"""도메인 규칙 Cedar 정책 admin API (IH-132).

## 클라이언트는 Cedar 를 보낼 수 없어요

이 라우터가 받는 건 **구조화된 값**이에요 — 자산·도구·인자 이름·연산자·임계값·설명.
`permit` 문장은 서버가 조립해요(`domain_policy.compile_domain_rule`). pydantic 모델을
`extra="forbid"` 로 닫아 뒀으니 `cedar` 같은 키가 섞이면 **422** 로 거부돼요. 조용히 무시하면
클라이언트가 Cedar 를 보내도 되는 것처럼 보이고, 다음 사람이 그 경로를 열어요.

`normalize_draft` 도 같은 확인을 한 번 더 해요 — 라우터를 우회해 서비스를 직접 부르는 경로
(다른 백엔드 코드·스크립트)에도 같은 규칙이 걸리게요.

## 좌표는 config 에서만 와요

대상 Gateway 와 policy engine 은 `AGORA_M2_OAUTH_*` 에서 읽어요. 요청이 Gateway 를 고를 수
있으면 인가 대상 자원을 클라이언트가 정하는 셈이에요.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field

from ..governance.authz import require_role
from .context import current_principal
from .domain_policy import (
    ConflictUnobservable,
    DomainRuleIneffective,
    EnforcementPromotionRefused,
    InvalidDomainRule,
)
from .models import DomainPolicyRule
from .shared_policy_trigger import (
    SharedPolicyProvisioningFailed,
    require_shared_policy_provisioned,
)
from .store import IdentityRecordNotFound

_log = logging.getLogger(__name__)

router = APIRouter(tags=["identity-domain-policy"])

_BASE = "/api/admin/identity/domain-policies"


class DomainRuleBody(BaseModel):
    """구조화된 입력만 받아요. `extra="forbid"` 가 Cedar 원문 유입을 막는 첫 관문이에요."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1, max_length=200)
    asset_version: str = Field(default="", max_length=100)
    target_name: str = Field(min_length=1, max_length=100)
    tool_name: str = Field(min_length=1, max_length=200)
    argument: str = Field(min_length=1, max_length=64)
    operator: str = Field(min_length=1, max_length=2)
    value_kind: str = Field(min_length=1, max_length=16)
    value: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=300)


class DomainRuleCreateBody(DomainRuleBody):
    #: 굵은 문이 이 action 을 이미 허용해도 저장할지 — 관리자가 명시해야 해요.
    #: 원장에 `conflict_acknowledged` 로 남아요.
    acknowledge_ineffective: bool = False


def _service():
    from ...shared.deps import get_domain_policy_service

    try:
        return get_domain_policy_service()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


def _draft_payload(body: DomainRuleBody) -> dict:
    return body.model_dump(exclude={"acknowledge_ineffective"})


def _active_membership(rule: DomainPolicyRule) -> bool | None:
    """공유 컴파일러의 ACTIVE ② 판정이에요. 미관측은 False로 접지 않아요."""

    if rule.observed_status == "UNKNOWN" or not rule.enforcement_mode:
        return None
    return (
        rule.observed_status == "ACTIVE"
        and rule.enforcement_mode == "ACTIVE"
        and bool(rule.remote_policy_id)
    )


def _find_rule(service, rule_id: str) -> DomainPolicyRule:
    for rule in service.list_rules():
        if rule.rule_id == rule_id:
            return rule
    raise IdentityRecordNotFound(rule_id)


def _provision_shared_policy_after_rule_commit(*, change: str) -> dict:
    from ...shared import deps as shared_deps

    try:
        return require_shared_policy_provisioned(
            shared_deps.provision_shared_gateway_policy
        )
    except SharedPolicyProvisioningFailed as exc:
        provisioning = exc.report.get("verdict") == "provisioning"
        raise HTTPException(502, {
            "message": (
                "도메인 규칙 변경은 저장됐고 공유 Gateway 정책은 활성화 중이에요."
                if provisioning
                else (
                    "도메인 규칙 변경은 저장됐지만 공유 Gateway 정책을 그 상태에 "
                    "맞추지 못했어요."
                )
            ),
            "reason": (
                "shared_policy_provisioning_in_progress"
                if provisioning
                else "shared_policy_provisioning_failed"
            ),
            "change": change,
            "ledger_committed": True,
            "shared_policy_provisioning": exc.report,
            "remediation": (
                "정책이 활성화 중이니 잠시 뒤 다시 눌러 주세요."
                if provisioning
                else (
                    "도메인 규칙을 임의로 되돌리지 말고 공유 정책 provisioning을 "
                    "다시 실행해 주세요."
                )
            ),
        }) from exc


@router.get(_BASE, dependencies=[Depends(require_role("admin"))])
def list_domain_policies():
    """원장에 있는 도메인 규칙을 나열해요 (AWS 호출 없음)."""
    service = _service()
    return {
        "gateway_arn": service.coordinates.gateway_arn,
        "rules": jsonable_encoder(service.list_rules()),
    }


@router.get(f"{_BASE}/options", dependencies=[Depends(require_role("admin"))])
def domain_policy_options():
    """폼 선택지 — 자산 → 도구 → 인자(자산 원장의 `inputSchema` 에서)."""
    try:
        return _service().options().to_dict()
    except Exception as exc:
        raise HTTPException(
            502, f"선택지를 읽지 못했어요: {type(exc).__name__}: {exc}"
        ) from exc


@router.post(f"{_BASE}/preview", dependencies=[Depends(require_role("admin"))])
def preview_domain_policy(body: DomainRuleBody):
    """저장 전에 서버가 조립할 Cedar 문장과 굵은 문 충돌 판정을 돌려줘요."""
    try:
        return _service().preview(_draft_payload(body))
    except InvalidDomainRule as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post(_BASE, dependencies=[Depends(require_role("admin"))])
def create_domain_policy(body: DomainRuleCreateBody, request: Request):
    """도메인 규칙을 만들어요. 상태는 생성 후 다시 관측한 값이에요."""
    principal_id = current_principal(request).principal_id
    try:
        rule = _service().create(
            _draft_payload(body),
            created_by=principal_id,
            acknowledge_ineffective=body.acknowledge_ineffective,
        )
    except InvalidDomainRule as exc:
        raise HTTPException(400, str(exc)) from exc
    except DomainRuleIneffective as exc:
        raise HTTPException(409, {
            "message": (
                "이 도구는 다른 ACTIVE permit 이 이미 허용하고 있어요. Cedar 의 permit 은 "
                "합집합이라 이 정책을 추가해도 요청이 거부되지 않아요."
            ),
            "reason": "coarse_gate_already_permits",
            "conflicts": [
                {
                    "policy_id": c.policy_id,
                    "policy_name": c.policy_name,
                    "kind": c.kind,
                    "label": c.label,
                    "detail": c.detail,
                }
                for c in exc.conflicts
            ],
            "remediation": (
                "굵은 문에서 이 action 을 빼거나, 지금은 효력이 없다는 것을 확인하고 "
                "「알고도 저장」을 선택해 주세요."
            ),
        }) from exc
    except ConflictUnobservable as exc:
        # 관측 실패는 통과가 아니에요(ADR-0037 §4). 「충돌 없음」으로 저장하지 않아요.
        raise HTTPException(409, {
            "message": (
                "라이브 Cedar 정책을 읽지 못해 굵은 문 충돌을 판정할 수 없었어요. "
                "관측하지 못한 것을 「충돌 없음」으로 적지 않으려고 저장을 멈췄어요."
            ),
            "reason": "conflict_unobservable",
            "detail": str(exc),
        }) from exc
    except Exception as exc:
        raise HTTPException(
            502, f"정책을 만들지 못했어요: {type(exc).__name__}: {exc}"
        ) from exc
    return jsonable_encoder(rule)


class PromoteBody(BaseModel):
    """승격 요청. **모드는 받지 않아요** — 이 엔드포인트는 `ACTIVE` 로만 올려요.

    모드를 인자로 받으면 「생성은 LOG_ONLY」 규칙을 이 경로로 우회할 수 있어요.
    """

    model_config = ConfigDict(extra="forbid")

    acknowledge_ineffective: bool = False


@router.post(
    f"{_BASE}/{{rule_id}}/promote",
    dependencies=[Depends(require_role("admin"))],
)
def promote_domain_policy(rule_id: str, body: PromoteBody, request: Request):
    """`LOG_ONLY` → `ACTIVE` 로 올려요. 굵은 문 충돌을 **다시** 재고 올려요."""
    principal_id = current_principal(request).principal_id
    try:
        rule = _service().promote(
            rule_id,
            principal_id=principal_id,
            acknowledge_ineffective=body.acknowledge_ineffective,
        )
    except IdentityRecordNotFound as exc:
        raise HTTPException(404, "도메인 규칙을 찾을 수 없어요.") from exc
    except DomainRuleIneffective as exc:
        raise HTTPException(409, {
            "message": (
                "승격 시점에도 다른 ACTIVE permit 이 이 도구를 허용하고 있어요. "
                "강제로 올려도 요청이 거부되지 않아요."
            ),
            "reason": "coarse_gate_already_permits",
            "conflicts": [
                {
                    "policy_id": c.policy_id,
                    "policy_name": c.policy_name,
                    "kind": c.kind,
                    "label": c.label,
                    "detail": c.detail,
                }
                for c in exc.conflicts
            ],
        }) from exc
    except ConflictUnobservable as exc:
        raise HTTPException(409, {
            "message": (
                "라이브 Cedar 정책을 읽지 못해 굵은 문 충돌을 다시 판정할 수 없었어요. "
                "관측하지 못한 상태로 강제를 켜지 않아요."
            ),
            "reason": "conflict_unobservable",
            "detail": str(exc),
        }) from exc
    except EnforcementPromotionRefused as exc:
        raise HTTPException(409, {
            "message": str(exc),
            "reason": exc.reason,
        }) from exc
    except Exception as exc:
        raise HTTPException(
            502, f"승격하지 못했어요: {type(exc).__name__}: {exc}"
        ) from exc
    response = jsonable_encoder(rule)
    if _active_membership(rule) is True:
        response["shared_policy_provisioning"] = (
            _provision_shared_policy_after_rule_commit(
                change="domain_policy_promoted"
            )
        )
    return response


@router.post(
    f"{_BASE}/{{rule_id}}/refresh",
    dependencies=[Depends(require_role("admin"))],
)
def refresh_domain_policy(rule_id: str):
    """원격 상태를 다시 관측해요 — `UNKNOWN` 을 뒤늦게 닫는 경로예요."""
    service = _service()
    try:
        before = _find_rule(service, rule_id)
        rule = service.refresh_status(rule_id)
    except IdentityRecordNotFound as exc:
        raise HTTPException(404, "도메인 규칙을 찾을 수 없어요.") from exc
    before_active = _active_membership(before)
    after_active = _active_membership(rule)
    response = jsonable_encoder(rule)
    # ACTIVE member가 관측됐으면 이전 UNKNOWN 여부와 무관하게 다시 맞춰요. 승격 폴링이
    # 타임아웃된 규칙은 원장에 UNKNOWN으로 남아 이전 집합 포함 여부를 알 수 없기 때문이에요.
    # 반대로 기존 ACTIVE가 명확히 빠진 경우에도 열거에서 걷어내야 해요.
    if after_active is True or (
        before_active is True and after_active is False
    ):
        response["shared_policy_provisioning"] = (
            _provision_shared_policy_after_rule_commit(
                change="domain_policy_refreshed"
            )
        )
    return response


@router.delete(
    f"{_BASE}/{{rule_id}}",
    dependencies=[Depends(require_role("admin"))],
)
def delete_domain_policy(rule_id: str, request: Request):
    """원격 정책을 지우고 원장 행을 없애요. 원격 삭제가 실패하면 원장을 남겨요."""
    principal_id = current_principal(request).principal_id
    service = _service()
    try:
        before = _find_rule(service, rule_id)
        result = service.delete(rule_id, principal_id=principal_id)
    except IdentityRecordNotFound as exc:
        raise HTTPException(404, "도메인 규칙을 찾을 수 없어요.") from exc
    if result.get("deleted") and _active_membership(before) is True:
        result["shared_policy_provisioning"] = (
            _provision_shared_policy_after_rule_commit(
                change="domain_policy_deleted"
            )
        )
    return result
