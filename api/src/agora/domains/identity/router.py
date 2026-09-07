"""로그인 사용자 정보 API."""
from __future__ import annotations

from fastapi import APIRouter, Request

from .context import current_principal

router = APIRouter(tags=["identity"])


@router.get("/api/me")
def me(request: Request):
    principal = current_principal(request)
    return {
        "principal_id": principal.principal_id,
        "roles": list(principal.roles),
        "source": principal.source,
        # 소유 팀. 값이 없으면 빈 문자열이고, 프론트는 그때 등록 폼의 자유입력을 유지해요
        # (인사 IdP가 team을 채우지 않는 환경이 정상이에요).
        "team": principal.team,
        # 담당자 표시용 email. 없는 환경과 기존 세션은 빈 문자열이에요.
        "email": principal.email,
        "can": {
            "register_asset": principal.has_any_role(("user", "admin")),
            "manage_governance": principal.is_admin,
            "manage_access": principal.is_admin,
        },
    }
