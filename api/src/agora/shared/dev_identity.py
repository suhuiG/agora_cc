"""Cross-domain contracts for local-development identity issuance."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .slug import slugify


def dev_identity_blueprint_id(agent_name: str) -> str:
    """Return the stable Initializr dev-identity key for one named agent."""
    return f"initializr-{slugify(agent_name)}"


class DevIdentityNoAccessError(ValueError):
    """The selected workspace has no action that can be issued.

    `excluded` 는 **제외돼서** 남은 게 없어진 경우의 사유예요. 비어 있으면 애초에 통과한
    operation 이 없었다는 뜻이고, 채워져 있으면 "골랐지만 로컬 다운로드에는 담을 수 없는
    것들" 이에요. 화면이 두 상황에 다른 안내를 해야 해서 구분해요 — 전부 제외된 것도 실패로
    남겨요(담을 게 없는 크리덴셜은 존재할 이유가 없어요).
    """

    def __init__(
        self,
        blueprint_id: str,
        excluded: tuple["DevAuthorizationFailure", ...] = (),
    ) -> None:
        super().__init__(blueprint_id)
        self.blueprint_id = blueprint_id
        self.excluded = excluded


class DevAuthorizationState(str, Enum):
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class DevAuthorizationReason(str, Enum):
    ASSET_BINDING_INVALID = "asset_binding_invalid"
    ASSET_BINDING_UNOBSERVABLE = "asset_binding_unobservable"
    ASSET_CAPABILITY_MISSING = "asset_capability_missing"
    ASSET_CAPABILITY_UNOBSERVABLE = "asset_capability_unobservable"
    ASSET_CAPABILITY_NOT_APPROVED = "asset_capability_not_approved"
    GATEWAY_TARGET_UNRESOLVED = "gateway_target_unresolved"
    GATEWAY_TARGET_UNOBSERVABLE = "gateway_target_unobservable"
    CONNECTION_MISSING = "connection_missing"
    CONNECTION_UNOBSERVABLE = "connection_unobservable"
    CONNECTION_INACTIVE = "connection_inactive"
    REQUIRED_CAPABILITIES_MISSING = "required_capabilities_missing"
    CONNECTION_CAPABILITIES_UNOBSERVABLE = (
        "connection_capabilities_unobservable"
    )
    CONNECTION_CAPABILITY_INACTIVE = "connection_capability_inactive"
    CONNECTION_CEILING_EXCEEDED = "connection_ceiling_exceeded"
    PRINCIPAL_GRANTS_UNOBSERVABLE = "principal_grants_unobservable"
    PRINCIPAL_GRANT_MISSING = "principal_grant_missing"
    #: 로컬 다운로드 천장(READ) 밖이라 **제외**했어요. 치명이 아니에요 — 나머지 READ 도구로
    #: 발급은 계속돼요(`dev_identity_service._DOWNLOAD_SENSITIVITY_CEILING`).
    OPERATION_SENSITIVITY_NOT_READ = "operation_sensitivity_not_read"
    #: 민감도를 **관측하지 못했어요**(legacy 미분할 Target). 천장 판정이 불가능하니
    #: 통과로 접지 않고 발급을 세워요 — 모르는 걸 pass 로 적는 건 금지예요
    #: (AGENTS.md §"Unobservable is not passing").
    OPERATION_SENSITIVITY_UNKNOWN = "operation_sensitivity_unknown"


@dataclass(frozen=True)
class DevAuthorizationFailure:
    asset_id: str
    operation_id: str
    state: DevAuthorizationState
    reason: DevAuthorizationReason
    connection_id: str = ""
    missing_capabilities: tuple[str, ...] = ()


class DevIdentityAuthorizationError(RuntimeError):
    def __init__(self, failures: tuple[DevAuthorizationFailure, ...]) -> None:
        super().__init__("selected tools are not authorized")
        self.failures = failures


@dataclass(frozen=True)
class DevSelectedTool:
    asset_id: str
    asset_version: str
    # Empty preserves the legacy whole-asset grant behavior.
    operations: tuple[str, ...] = ()


@dataclass(frozen=True)
class DevAssetTarget:
    name: str
    # Empty means a legacy unsplit Target whose operations come from policy rows.
    operations: tuple[str, ...] = ()
    # Registry 가 명시한 민감도(READ/CREATE/UPDATE/DELETE). 이름에서 추론한 값이 아니에요 —
    # 추론값을 인가 근거로 쓰면 이름이 애매한 도구가 조용히 READ 로 분류돼요
    # (`catalog_read_access.py` 와 같은 규칙).
    #
    # 빈 문자열은 legacy(미분할 Target)라 민감도를 모른다는 뜻이에요. 그때는 자동 승인하지
    # 않아요 — 모르면 열지 않아요.
    sensitivity: str = ""


@dataclass(frozen=True)
class DevAssetAuthorizationContext:
    """Registry-owned facts used by the identity domain to select a policy."""

    asset_id: str
    asset_version: str
    targets: tuple[DevAssetTarget, ...]

    def target_sensitivity(self, operation_id: str) -> str:
        """이 operation 이 속한 Target 의 Registry 민감도. 모르면 빈 문자열."""
        for target in self.targets:
            if operation_id and operation_id in target.operations:
                return target.sensitivity
        # legacy 미분할 Target 은 operation 목록이 비어 있어요 — 민감도를 알 수 없어요.
        if len(self.targets) == 1 and not self.targets[0].operations:
            return self.targets[0].sensitivity
        return ""

    def target_name(self, operation_id: str) -> str | None:
        for target in self.targets:
            if not target.operations or operation_id in target.operations:
                return target.name
        return None
