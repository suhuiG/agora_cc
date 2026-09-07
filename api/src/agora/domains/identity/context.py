"""FastAPI 요청에서 검증된 Principal을 꺼내는 단일 진입점."""
from __future__ import annotations

from fastapi import HTTPException, Request

from .models import Principal
from .service import IdentityService
from .token_verifier import AuthenticationError, AuthorizationError


def current_principal(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    if isinstance(principal, Principal):
        return principal

    # router 단위 FastAPI 앱을 쓰는 테스트도 같은 경계를 타게 해요. test 모드 외에는
    # 실제 Cognito/dev 인증 규칙이 그대로 적용되므로 헤더 신뢰 우회가 생기지 않아요.
    try:
        principal = IdentityService.from_env().authenticate(request.headers)
    except AuthenticationError as exc:
        raise HTTPException(401, str(exc), headers={"WWW-Authenticate": "Bearer"}) from exc
    except AuthorizationError as exc:
        raise HTTPException(403, str(exc)) from exc
    request.state.principal = principal
    return principal


def principal_id(request: Request) -> str:
    return current_principal(request).principal_id
