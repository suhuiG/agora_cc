"""호출별 opaque delegation 발급·검증·회수."""
from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, replace

from .models import DelegationContext, DecisionReason
from .store import IdentityRecordNotFound, IdentityStore


class DelegationError(PermissionError):
    def __init__(self, reason: DecisionReason = DecisionReason.INVALID_DELEGATION):
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True)
class DelegatedAsset:
    asset_id: str
    asset_version: str
    # None means the deployment-fixed operation subset was not observable.
    # Delegations live for at most 900 seconds, so legacy compatibility is not
    # a durable reason to interpret an absent subset as whole-asset access.
    operations: tuple[str, ...] | None

    def allows(self, asset_version: str, operation_id: str) -> bool:
        return (
            (not self.asset_version or self.asset_version == asset_version)
            and self.operations is not None
            and operation_id in self.operations
        )


class DelegationService:
    def __init__(self, store: IdentityStore, *, now=None, new_handle=None, new_id=None):
        self.store = store
        self._now = now or (lambda: int(time.time()))
        self._new_handle = new_handle or (lambda: secrets.token_urlsafe(32))
        self._new_id = new_id or (lambda: uuid.uuid4().hex)

    @staticmethod
    def hash_handle(handle: str) -> str:
        return hashlib.sha256(handle.encode("utf-8")).hexdigest()

    def issue(
        self,
        *,
        principal_id: str,
        agent_id: str,
        workload_id: str,
        allowed_asset_ids: Iterable[str] = (),
        principal_groups: Iterable[str] = (),
        principal_email: str = "",
        ttl_seconds: int = 900,
    ) -> tuple[str, DelegationContext]:
        # OAuth agent(ADR-0019)는 Ed25519 workload_id가 없어요 — client-side _authorize를
        # 폐지해 validate()가 이 경로에서 호출되지 않으니 workload_id는 선택이에요.
        # principal·agent만 필수. legacy(Ed25519) agent는 여전히 non-empty를 넘겨요.
        if not principal_id or not agent_id:
            raise ValueError("principal, agent는 비어 있을 수 없어요.")
        if not 60 <= ttl_seconds <= 900:
            raise ValueError("delegation TTL은 60~900초여야 해요.")
        now = self._now()
        handle = self._new_handle()
        allowed_assets = tuple(
            dict.fromkeys(
                asset_id.strip()
                for asset_id in allowed_asset_ids
                if asset_id and asset_id.strip()
            )
        )
        # 사람의 그룹을 이 행에 박아요. Gateway interceptor 는 외부 조회가 금지돼서
        # (ADR-0091) 그룹 단위 grant 를 판정하려면 이 값이 필요해요. 발급 시점의
        # 인증된 세션 값이라 신선하고, 클라이언트가 위조할 수 없어요.
        groups = tuple(
            dict.fromkeys(
                group.strip() for group in principal_groups if group and group.strip()
            )
        )
        context = DelegationContext(
            handle_hash=self.hash_handle(handle),
            principal_groups=groups,
            # MCP 에 넘길 `agora_user_id` 값이에요 (ADR-0095). 발급 시점의 검증된 email 이라
            # 클라이언트가 위조할 수 없어요. 빈 값이면 interceptor 가 채우지 않고 거부해요.
            principal_email=principal_email.strip(),
            principal_id=principal_id,
            agent_id=agent_id,
            invocation_id=self._new_id(),
            workload_id=workload_id,
            created_at=now,
            expires_at=now + ttl_seconds,
            allowed_asset_ids=allowed_assets,
        )
        self.store.put_delegation(context)
        return handle, context

    def validate(
        self, handle: str, *, agent_id: str, workload_id: str
    ) -> DelegationContext:
        if not handle:
            raise DelegationError()
        try:
            context = self.store.get_delegation(self.hash_handle(handle))
        except IdentityRecordNotFound as exc:
            raise DelegationError() from exc
        if (
            context.revoked_at is not None
            or context.expires_at <= self._now()
            or context.agent_id != agent_id
            or context.workload_id != workload_id
        ):
            raise DelegationError()
        return context

    def revoke(self, handle: str) -> None:
        try:
            context = self.store.get_delegation(self.hash_handle(handle))
        except IdentityRecordNotFound:
            return
        self.store.put_delegation(replace(context, revoked_at=self._now()))


def delegated_assets(descriptors: dict) -> tuple[DelegatedAsset, ...]:
    """Read the deployment-fixed MCP asset, version, and operation declarations."""
    if not isinstance(descriptors, dict):
        return ()
    agent = descriptors.get("agent")
    if not isinstance(agent, dict):
        return ()
    dependencies = agent.get("agoraDependencies")
    if not isinstance(dependencies, dict):
        return ()
    assets = dependencies.get("mcpAssets")
    if not isinstance(assets, list):
        return ()
    return tuple(
        DelegatedAsset(
            asset_id=str(item.get("assetId", "")).strip(),
            asset_version=str(
                item.get("version") or item.get("assetVersion") or ""
            ).strip(),
            operations=(
                tuple(
                    dict.fromkeys(
                        operation.strip()
                        for operation in item["operations"]
                        if isinstance(operation, str) and operation.strip()
                    )
                )
                if isinstance(item.get("operations"), list)
                else (() if "operations" in item else None)
            ),
        )
        for item in assets
        if isinstance(item, dict) and str(item.get("assetId", "")).strip()
    )


def delegated_asset_ids(descriptors: dict) -> tuple[str, ...]:
    """Agent descriptor에 배포 시 고정한 MCP 자산 allowlist를 읽어요."""
    return tuple(
        dict.fromkeys(asset.asset_id for asset in delegated_assets(descriptors))
    )


def delegated_workload_binding(descriptors: dict) -> tuple[str, str]:
    """Agent descriptor의 Runtime별 Ed25519 workload ID와 공개키를 읽어요."""
    if not isinstance(descriptors, dict):
        return "", ""
    agent = descriptors.get("agent")
    if not isinstance(agent, dict):
        return "", ""
    binding = agent.get("workloadIdentity")
    if (
        not isinstance(binding, dict)
        or binding.get("version") != 1
        or binding.get("algorithm") != "Ed25519"
    ):
        return "", ""
    workload_id = str(binding.get("id") or "").strip()
    public_key = str(binding.get("publicKey") or "").strip()
    return (workload_id, public_key) if workload_id and public_key else ("", "")
