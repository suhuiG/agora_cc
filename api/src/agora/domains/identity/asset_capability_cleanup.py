"""Identity-owned deletion and audit of obsolete MCP approvals."""
from __future__ import annotations

import logging

from ...shared.asset_capability_cleanup import AssetCapabilityCleanupReport
from .models import (
    AuditEvent,
    AuthorizationOutcome,
    DecisionReason,
)

_log = logging.getLogger(__name__)


class IdentityAssetCapabilityCleaner:
    def __init__(self, store, *, now, new_id) -> None:
        self._store = store
        self._now = now
        self._new_id = new_id

    def delete_record_approvals(
        self,
        record_id: str,
        *,
        replacement_record_id: str,
        actor: str,
    ) -> AssetCapabilityCleanupReport:
        deleted: list[tuple[str, str]] = []
        unknown: list[tuple[str, str]] = []
        is_purge = not replacement_record_id
        for capability in self._store.list_asset_capabilities(record_id):
            event = self._event(
                record_id=record_id,
                operation_id=capability.operation_id,
                replacement_record_id=replacement_record_id,
                actor=actor,
                connection_id=capability.connection_id,
                capabilities=capability.required_capabilities,
                event_type=(
                    "ASSET_CAPABILITY_ASSET_PURGE_DELETED"
                    if is_purge
                    else "ASSET_CAPABILITY_REREGISTRATION_DELETED"
                ),
                failure_type=(
                    "ASSET_PURGE_REVOKES_APPROVAL"
                    if is_purge
                    else "MCP_REREGISTRATION_RESETS_APPROVAL"
                ),
            )
            try:
                removed = self._store.delete_asset_capability_with_audit(
                    record_id,
                    capability.operation_id,
                    expected_version=capability.version,
                    event=event,
                )
            except Exception as exc:  # noqa: BLE001 - returned for reconciliation.
                coordinate = (record_id, capability.operation_id)
                unknown.append(coordinate)
                reason = (
                    f"delete_failed:{type(exc).__name__}:"
                    f"{capability.operation_id}"
                )
                try:
                    self.record_unknown(
                        record_id,
                        replacement_record_id=replacement_record_id,
                        actor=actor,
                        reason=reason,
                    )
                except Exception:  # noqa: BLE001 - keep the coordinate in report/log.
                    _log.exception(
                        "asset capability deletion and unknown audit both failed; "
                        "record_id=%s operation_id=%s replacement_record_id=%s "
                        "actor=%s reason=%s",
                        record_id,
                        capability.operation_id,
                        replacement_record_id,
                        actor,
                        reason,
                    )
                continue
            if removed:
                deleted.append((record_id, capability.operation_id))
        return AssetCapabilityCleanupReport(
            deleted=tuple(deleted),
            unknown=tuple(unknown),
        )

    def record_unknown(
        self,
        record_id: str,
        *,
        replacement_record_id: str,
        actor: str,
        reason: str,
    ) -> None:
        is_purge = not replacement_record_id
        self._store.append_audit(
            self._event(
                record_id=record_id,
                operation_id="",
                replacement_record_id=replacement_record_id,
                actor=actor,
                event_type=(
                    "ASSET_CAPABILITY_ASSET_PURGE_CLEANUP_UNKNOWN"
                    if is_purge
                    else "ASSET_CAPABILITY_REREGISTRATION_CLEANUP_UNKNOWN"
                ),
                failure_type=reason,
            )
        )

    def _event(
        self,
        *,
        record_id: str,
        operation_id: str,
        replacement_record_id: str,
        actor: str,
        event_type: str,
        failure_type: str,
        connection_id: str = "",
        capabilities: tuple[str, ...] = (),
    ) -> AuditEvent:
        is_purge = not replacement_record_id
        invocation_id = (
            f"asset-purge:{record_id}"
            if is_purge
            else f"mcp-reregistration:{replacement_record_id}"
        )
        return AuditEvent(
            event_id=self._new_id(),
            invocation_id=invocation_id,
            principal_id=actor,
            agent_id="" if is_purge else replacement_record_id,
            asset_id=record_id,
            operation_id=operation_id,
            connection_id=connection_id,
            capabilities=capabilities,
            decision=AuthorizationOutcome.DENY,
            reason=DecisionReason.ASSET_CAPABILITY_NOT_APPROVED,
            target="" if is_purge else replacement_record_id,
            timestamp=self._now(),
            workload_id="",
            event_type=event_type,
            failure_type=failure_type,
        )
