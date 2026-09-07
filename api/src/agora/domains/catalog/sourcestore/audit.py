"""AuditContext — 검증된 Cognito Principal을 감사 필드로 변환."""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request


@dataclass(frozen=True)
class AuditContext:
    principal_id: str

    def principal(self) -> str:
        if not self.principal_id:
            raise ValueError("감사 principal은 비어 있을 수 없어요.")
        return self.principal_id

    @classmethod
    def from_request(cls, request: Request) -> "AuditContext":
        from ...identity.context import current_principal
        return cls(principal_id=current_principal(request).principal_id)
