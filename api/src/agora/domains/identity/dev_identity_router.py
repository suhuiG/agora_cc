"""HTTP surface for temporary local-development identities."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ...shared.dev_identity import DevIdentityNoAccessError
from .agent_policy_compiler import NoToolAccessError
from .context import current_principal
from .dev_identity_models import (
    DevAuthorizationState,
    DevIdentityAuthorizationError,
    DevIdentityAuthenticationError,
    DevIdentityCredential,
    DevIdentityInfrastructureError,
    DevIdentityNotFound,
    DevIdentityOwnershipError,
    DevSelectedTool,
    IssuedDevIdentityCredential,
    SharedPolicyConvergence,
)

router = APIRouter(prefix="/api/dev-identity", tags=["dev-identity"])


def get_dev_identity_service():
    from ...shared.deps import get_dev_identity_service as get_service

    return get_service()


class SelectedToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str
    asset_version: str
    operations: list[str] = Field(default_factory=list)


class CredentialCreateInput(BaseModel):
    blueprint_id: str
    selected_tools: list[SelectedToolInput] = Field(default_factory=list)
    ttl_days: int | None = Field(default=None, ge=1)


class CredentialView(BaseModel):
    credential_id: str
    principal: str
    blueprint_id: str
    client_id: str
    actions: tuple[str, ...]
    created_at: int
    expires_at: int
    revoked_at: int | None
    last_used_at: int | None


class SharedPolicyConvergenceView(BaseModel):
    """공유 ① 열거 반영 상태 (IH-160, ADR-0110).

    `converged=false` 는 발급 실패가 아니에요 — 크리덴셜은 유효하고 원장 ④ 행도 커밋됐어요.
    그 도구가 `tools/list` 에 아직 안 보일 수 있고, 만료 sweep 이 자동으로 다시 맞춰요.

    provisioner 리포트의 `reason` 은 **싣지 않아요** — 정책 이름·ARN·AWS 예외 원문이 들어
    있고 이 엔드포인트는 어드민 전용이 아니에요.
    """

    converged: bool
    verdict: str = ""
    detail: str = ""


class IssuedCredentialView(CredentialView):
    credential: str
    #: `null` 은 「이 응답이 열거를 바꾸지 않았어요」 — `rotate` 는 비밀만 갱신해요.
    shared_policy_convergence: SharedPolicyConvergenceView | None = None


def _view(record: DevIdentityCredential) -> CredentialView:
    return CredentialView(
        credential_id=record.credential_id,
        principal=record.principal,
        blueprint_id=record.blueprint_id,
        client_id=record.client_id,
        actions=record.actions,
        created_at=record.created_at,
        expires_at=record.expires_at,
        revoked_at=record.revoked_at,
        last_used_at=record.last_used_at,
    )


def _convergence(
    value: SharedPolicyConvergence | None,
) -> SharedPolicyConvergenceView | None:
    if value is None:
        return None
    return SharedPolicyConvergenceView(
        converged=value.converged,
        verdict=value.verdict,
        detail=value.detail,
    )


def _issued(value: IssuedDevIdentityCredential) -> IssuedCredentialView:
    return IssuedCredentialView(
        **_view(value.record).model_dump(),
        credential=value.credential,
        shared_policy_convergence=_convergence(
            value.shared_policy_convergence
        ),
    )


def _authorization_error_detail(
    error: DevIdentityAuthorizationError,
) -> tuple[int, dict]:
    has_unknown = any(
        failure.state is DevAuthorizationState.UNKNOWN
        for failure in error.failures
    )
    return (
        503 if has_unknown else 403,
        {
            "message": (
                "selected tool authorization could not be determined"
                if has_unknown
                else "selected tools are not authorized"
            ),
            "code": (
                "DEV_IDENTITY_AUTHORIZATION_UNKNOWN"
                if has_unknown
                else "DEV_IDENTITY_AUTHORIZATION_BLOCKED"
            ),
            "action_url": "/governance",
            "failures": [
                {
                    "asset_id": failure.asset_id,
                    "operation_id": failure.operation_id,
                    "connection_id": failure.connection_id,
                    "state": failure.state.value,
                    "reason": failure.reason.value,
                    "missing_capabilities": list(
                        failure.missing_capabilities
                    ),
                }
                for failure in error.failures
            ],
        },
    )


@router.post("/credentials", response_model=IssuedCredentialView)
def issue_credential(body: CredentialCreateInput, request: Request):
    # 그룹·email 을 **인증된 세션**에서 같이 넘겨요. 안 넘기면 그룹으로만 READ 를 받은
    # 사람이 이 API 로는 크리덴셜을 못 받고(그룹 grant 가 안 보여요), 받아도 `agora_user_id`
    # 를 선언한 도구가 `owner_identity_missing` 으로 막혀요. scaffold 경로만 배선하고 이쪽을
    # 빼먹은 걸 codex 리뷰가 잡았어요(2026-08-30).
    caller = current_principal(request)
    principal = caller.principal_id
    try:
        issued = get_dev_identity_service().issue(
            principal=principal,
            principal_groups=tuple(caller.roles),
            principal_email=caller.email,
            blueprint_id=body.blueprint_id,
            selected_tools=tuple(
                DevSelectedTool(
                    asset_id=tool.asset_id,
                    asset_version=tool.asset_version,
                    operations=tuple(tool.operations),
                )
                for tool in body.selected_tools
            ),
            ttl_days=body.ttl_days,
        )
    except DevIdentityAuthorizationError as exc:
        status_code, detail = _authorization_error_detail(exc)
        raise HTTPException(status_code, detail) from exc
    except (DevIdentityNoAccessError, NoToolAccessError) as exc:
        raise HTTPException(403, "selected tools have no granted access") from exc
    except DevIdentityOwnershipError as exc:
        raise HTTPException(404, "dev credential not found") from exc
    return _issued(issued)


@router.get("/credentials", response_model=list[CredentialView])
def list_credentials(request: Request):
    principal = current_principal(request).principal_id
    return [_view(record) for record in get_dev_identity_service().list(
        principal=principal
    )]


@router.post(
    "/credentials/{credential_id}/rotate",
    response_model=IssuedCredentialView,
)
def rotate_credential(credential_id: str, request: Request):
    principal = current_principal(request).principal_id
    try:
        return _issued(
            get_dev_identity_service().rotate(
                credential_id, principal=principal
            )
        )
    except (DevIdentityNotFound, DevIdentityOwnershipError) as exc:
        raise HTTPException(404, "dev credential not found") from exc
    except NoToolAccessError as exc:
        raise HTTPException(403, "selected tools have no granted access") from exc


@router.delete("/credentials/{credential_id}", response_model=CredentialView)
def revoke_credential(credential_id: str, request: Request):
    principal = current_principal(request).principal_id
    try:
        return _view(
            get_dev_identity_service().revoke(
                credential_id, principal=principal
            )
        )
    except (DevIdentityNotFound, DevIdentityOwnershipError) as exc:
        raise HTTPException(404, "dev credential not found") from exc


@router.post("/token")
def issue_token(request: Request):
    scheme, _, credential = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        raise _generic_credential_error()
    try:
        return get_dev_identity_service().token(credential.strip())
    except DevIdentityAuthenticationError as exc:
        raise _generic_credential_error() from exc
    except DevIdentityInfrastructureError as exc:
        raise HTTPException(502, "dev token service unavailable") from exc


@router.post("/call-handle")
def issue_call_handle(request: Request):
    """dev 크리덴셜 하나를 **호출 handle 한 개**로 바꿔줘요.

    로컬에서 실행하는 생성 코드가 MCP 호출 직전에 불러요. 인증은 `/token` 과 같은 방식이에요
    (`Authorization: Bearer <dev credential>`).

    ## 왜 별도 엔드포인트인가

    토큰은 1시간, handle 은 최대 15분이고 **호출마다 새로 받아야** 해요(ADR-0091·ADR-0095 §13).
    한 응답에 둘을 묶으면 클라이언트가 만료된 handle 을 재사용하게 돼요.

    실패는 `/token` 과 같은 이유로 401 하나로 뭉쳐요 — 어느 조건이 틀렸는지 알려주면 크리덴셜
    탐색에 쓰일 수 있어요. 진단은 서버 로그에서 해요.
    """
    scheme, _, credential = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        raise _generic_credential_error()
    try:
        return get_dev_identity_service().call_handle(credential.strip())
    except DevIdentityAuthenticationError as exc:
        raise _generic_credential_error() from exc
    except DevIdentityInfrastructureError as exc:
        raise HTTPException(502, "dev token service unavailable") from exc


def _generic_credential_error() -> HTTPException:
    return HTTPException(
        401,
        "dev credential authentication failed",
        headers={"WWW-Authenticate": "Bearer"},
    )
