"""Cognito Principal 기반 플랫폼 RBAC."""
from __future__ import annotations

from collections.abc import Callable

from fastapi import HTTPException, Request

from ..identity.context import current_principal

CONSOLE_ROLES = ("admin",)
ADMIN_ONLY = ("admin",)


def get_current_roles(request: Request) -> tuple[str, ...]:
    return current_principal(request).roles


def require_role(*allowed: str) -> Callable[[Request], None]:
    def _dep(request: Request) -> None:
        roles = get_current_roles(request)
        if not (set(roles) & set(allowed)):
            raise HTTPException(403, "권한 없음 (관리 기능은 admin 전용)")
    return _dep
