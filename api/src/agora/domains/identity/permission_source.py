"""사용자 토큰에 주입할 업무 권한 소스 계약."""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .models import GrantStatus
from .store import IdentityStore


@dataclass(frozen=True)
class ScopedPermission:
    connection_id: str
    capability: str


class PermissionSourcePort(Protocol):
    def permissions_for(self, user_id: str) -> frozenset[ScopedPermission]: ...


class AgoraLocalAdapter:
    """기존 AccessGrant를 IdP permission claim으로 투영해요."""

    def __init__(
        self,
        store: IdentityStore,
        *,
        now: Callable[[], int] | None = None,
    ) -> None:
        self._store = store
        self._now = now or (lambda: int(time.time()))

    def permissions_for(self, user_id: str) -> frozenset[ScopedPermission]:
        now = self._now()
        return frozenset(
            ScopedPermission(
                connection_id=grant.connection_id,
                capability=capability,
            )
            for grant in self._store.list_grants(principal_id=user_id)
            if grant.status is GrantStatus.ACTIVE
            and (grant.expires_at is None or grant.expires_at > now)
            for capability in grant.capabilities
        )
