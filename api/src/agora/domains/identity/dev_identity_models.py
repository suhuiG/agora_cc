"""Identity-owned records for temporary local-development OAuth identities."""
from __future__ import annotations

from dataclasses import dataclass

from ...shared.dev_identity import (
    DevAuthorizationFailure,
    DevAuthorizationReason,
    DevAuthorizationState,
    DevIdentityAuthorizationError,
    DevSelectedTool,
    dev_identity_blueprint_id,
)

__all__ = [
    "DevAuthorizationFailure",
    "DevAuthorizationReason",
    "DevAuthorizationState",
    "DevIdentityAuthorizationError",
    "DevSelectedTool",
    "dev_identity_blueprint_id",
]


class DevIdentityAuthenticationError(RuntimeError):
    """Generic credential rejection. Callers must not expose the precise reason."""


class DevIdentityInfrastructureError(RuntimeError):
    """Token broker dependency failure. Callers must not expose upstream detail."""


class DevIdentityNotFound(LookupError):
    pass


class DevIdentityOwnershipError(RuntimeError):
    pass


@dataclass(frozen=True)
class DevActionGrant:
    action: str
    connection_id: str
    required_capabilities: tuple[str, ...]
    #: 아래 넷은 **원장 tool binding 을 쓰기 위한** 좌표예요.
    #:
    #: dev 크리덴셜로 Gateway 를 부르려면 interceptor 가 `AgentToolBinding` 을 찾아야 해요
    #: (`gateway_interceptor.py` `_authorize_tool`). 그 행은 `action` 만으로는 못 만들어요 —
    #: 자산·버전·operation·target 이 다 필요하거든요. 옛 행에는 없어서 기본값이 빈 문자열이고,
    #: 그러면 binding 을 쓰지 않아요(fail-closed — 다시 발급하면 채워져요).
    asset_id: str = ""
    asset_version: str = ""
    operation_id: str = ""
    gateway_target_name: str = ""
    #: READ/CREATE/UPDATE/DELETE. 승인 게이트를 배포 경로와 **같게** 적용하려고 들고 다녀요 —
    #: READ 만 자동 APPROVED 이고 나머지는 PENDING 이에요
    #: (`access_router._build_readonly_baseline_binding`).
    sensitivity: str = ""


@dataclass(frozen=True)
class DevIdentityCredential:
    credential_id: str
    credential_hash: str
    principal: str
    blueprint_id: str
    client_id: str
    actions: tuple[str, ...]
    action_grants: tuple[DevActionGrant, ...]
    created_at: int
    expires_at: int
    revoked_at: int | None = None
    last_used_at: int | None = None
    policy_revision: int = 0
    #: 발급 시점에 이 사람이 속해 있던 그룹. **서버가 인증된 세션에서 써요** — 클라이언트가
    #: 넣는 값이 아니에요.
    #:
    #: 회원 기본 READ 권한은 사람이 아니라 **그룹**(`GROUP#user`·`GROUP#admin`)에 달려 있어요
    #: (`catalog_read_access.py`). 이 값이 없으면 `_grants_still_cover` 가 그 grant 를 못 보고
    #: **정상 크리덴셜을 회수**해요. 그래서 재검증에도 같은 그룹 집합을 써요.
    #:
    #: 여기 굳어 있는 게 인가 구멍이 되지는 않아요 — 실제 강제는 Gateway REQUEST interceptor
    #: 예요. 모든 `tools/call` 이 handle 을 요구하고, 그 handle 의 그룹으로 사람 축 5단을
    #: **매 호출마다** 다시 봐요. 그룹에서 빠지면 다음 호출이 거부돼요. 이 필드는 회수 스윕의
    #: 판정 정밀도에만 쓰여요.
    principal_groups: tuple[str, ...] = ()
    #: 이 사람의 로그인 email. dev 크리덴셜로 handle 을 발급할 때 delegation 행에 실어서
    #: MCP 가 `agora_user_id` 로 받아요 (ADR-0095). 발급 시점의 인증된 세션에서 와요.
    #:
    #: 비어 있으면 `agora_user_id` 를 선언한 도구 호출이 `owner_identity_missing` 으로 막혀요.
    #: 조용히 봇이 실어 보낸 값을 통과시키는 것보다 안전해요.
    principal_email: str = ""


@dataclass(frozen=True)
class SharedPolicyConvergence:
    """발급이 바꾼 ④ 열거가 라이브 공유 ① Cedar 정책에 반영됐나요 (IH-160, ADR-0110).

    ## 왜 별 타입인가

    `ProvisionReport` 를 그대로 실어 보내면 `reason` 에 정책 이름·ARN·AWS 예외 원문이 실려요.
    dev 크리덴셜 발급은 **어드민 전용이 아니라** grant 가 있는 모든 사용자가 부르는 경로라,
    거기까지 내보낼 이유가 없어요. 그래서 「반영됐나 / 기계용 verdict / 사람이 읽는 한 문장」
    셋으로 좁혀요.

    `converged=False` 는 **실패가 아니라 미수렴**이에요 — 발급 자체는 성공했고 원장 ④ 행도
    커밋됐어요. 인가가 열리는 것도 아니에요(강제는 REQUEST interceptor). 다만 그 도구가
    `tools/list` 에 아직 안 보일 수 있고, 수렴은 만료 sweep 이 이어받아요.
    """

    converged: bool
    #: provisioner 의 `verdict` (`provisioned`·`unchanged`·`provisioning`·`refused`·
    #: `create_failed`·`unknown`) 또는 배선이 없을 때의 `not_wired`. 기계용 어휘예요.
    verdict: str = ""
    #: 사람이 읽는 한 문장. ARN·정책 이름·예외 원문은 **절대** 담지 않아요.
    detail: str = ""


@dataclass(frozen=True)
class IssuedDevIdentityCredential:
    record: DevIdentityCredential
    credential: str
    #: 골랐지만 **로컬 다운로드에는 담지 않은** operation 들. 발급은 성공했어요.
    #:
    #: 호출자가 이걸 그냥 버리면 사용자는 조용히 줄어든 도구 집합을 받아요 — 그게 정확히
    #: 이 필드가 있는 이유예요. `playground/router.py` 가 ZIP 응답 헤더로 실어 보내고
    #: 화면이 알림으로 보여줘요. `.env` 의 `AGORA_MCP_ASSETS` 도 이 목록을 빼고 써야 해요
    #: (안 빼면 생성 코드가 못 부르는 도구를 광고해요).
    excluded: tuple[DevAuthorizationFailure, ...] = ()
    #: 공유 ① 열거 반영 상태 (IH-160). `None` 은 **이 응답이 열거를 바꾸지 않았다**는
    #: 뜻이에요 — `rotate()` 는 비밀만 갱신하고 ④ 행을 건드리지 않아요.
    shared_policy_convergence: SharedPolicyConvergence | None = None
