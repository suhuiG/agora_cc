"""Observe ambiguous legacy/racing MCP re-registration records."""
from __future__ import annotations

from dataclasses import dataclass
import logging

from ....shared.asset_capability_cleanup import AssetCapabilityCleaner
from ....shared.gateway_tools import McpGatewayTargetError, mcp_gateway_target_index
from ..registry.models import DescriptorType, RecordNotFound, RecordStatus

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class McpReregistrationCleanupReport:
    skipped_foreign: tuple[str, ...] = ()
    unknown: tuple[tuple[str, str, str], ...] = ()


def _target_names(record) -> frozenset[str]:
    index = mcp_gateway_target_index(record.descriptors)
    if index.split:
        return frozenset(target.name for target in index.targets)
    if index.legacy_target_name:
        return frozenset((index.legacy_target_name,))
    return frozenset()


class McpReregistrationApprovalCleanup:
    def __init__(self, registry, registry_id: str, cleaner: AssetCapabilityCleaner) -> None:
        self._registry = registry
        self._registry_id = registry_id
        self._cleaner = cleaner

    def _record_unknown(
        self,
        record_id: str,
        *,
        replacement_record_id: str,
        actor: str,
        reason: str,
    ) -> None:
        try:
            self._cleaner.record_unknown(
                record_id,
                replacement_record_id=replacement_record_id,
                actor=actor,
                reason=reason,
            )
        except Exception:  # noqa: BLE001 - log remains the fallback ledger.
            _log.exception(
                "MCP approval cleanup unknown audit failed; "
                "record_id=%s replacement_record_id=%s actor=%s reason=%s",
                record_id,
                replacement_record_id,
                actor,
                reason,
            )

    def reconcile(
        self,
        record_id: str,
        *,
        actor: str,
    ) -> McpReregistrationCleanupReport:
        current = self._registry.get_record(self._registry_id, record_id)
        if current.descriptor_type is not DescriptorType.MCP:
            return McpReregistrationCleanupReport()
        if current.status is not RecordStatus.DRAFT:
            return McpReregistrationCleanupReport()
        try:
            current_targets = _target_names(current)
        except McpGatewayTargetError:
            self._record_unknown(
                record_id,
                replacement_record_id=record_id,
                actor=actor,
                reason="current_target_unobservable",
            )
            return McpReregistrationCleanupReport(
                unknown=((record_id, "", "current_target_unobservable"),)
            )
        if not current_targets:
            self._record_unknown(
                record_id,
                replacement_record_id=record_id,
                actor=actor,
                reason="current_target_unresolved",
            )
            return McpReregistrationCleanupReport(
                unknown=((record_id, "", "current_target_unresolved"),)
            )

        try:
            records = self._registry.list_records(
                self._registry_id,
                statuses=tuple(RecordStatus),
                max_results=None,
            )
        except Exception:  # noqa: BLE001 - retained as unknown evidence.
            reason = "candidate_records_unobservable"
            self._record_unknown(
                record_id,
                replacement_record_id=record_id,
                actor=actor,
                reason=reason,
            )
            return McpReregistrationCleanupReport(
                unknown=((record_id, "", reason),)
            )
        skipped_foreign: list[str] = []
        unknown: list[tuple[str, str, str]] = []
        for listed in records:
            old_record_id = str(getattr(listed, "record_id", "") or "")
            if not old_record_id or old_record_id == record_id:
                continue
            if getattr(listed, "descriptor_type", None) is not DescriptorType.MCP:
                continue
            try:
                old = self._registry.get_record(
                    self._registry_id,
                    old_record_id,
                )
            except RecordNotFound:
                reason = "old_record_not_found"
                self._record_unknown(
                    old_record_id,
                    replacement_record_id=record_id,
                    actor=actor,
                    reason=reason,
                )
                unknown.append((old_record_id, "", reason))
                continue
            except Exception:  # noqa: BLE001 - retained as unknown evidence.
                reason = "old_record_unobservable"
                self._record_unknown(
                    old_record_id,
                    replacement_record_id=record_id,
                    actor=actor,
                    reason=reason,
                )
                unknown.append((old_record_id, "", reason))
                continue
            try:
                old_targets = _target_names(old)
            except McpGatewayTargetError:
                reason = "old_target_unobservable"
                self._record_unknown(
                    old_record_id,
                    replacement_record_id=record_id,
                    actor=actor,
                    reason=reason,
                )
                unknown.append((old_record_id, "", reason))
                continue
            if not old_targets:
                reason = "old_target_unresolved"
                self._record_unknown(
                    old_record_id,
                    replacement_record_id=record_id,
                    actor=actor,
                    reason=reason,
                )
                unknown.append((old_record_id, "", reason))
                continue
            if not (old_targets & current_targets):
                continue
            current_owner = str(current.owner_user or "")
            old_owner = str(old.owner_user or "")
            if not current_owner or not old_owner:
                reason = "owner_unobservable"
                self._record_unknown(
                    old_record_id,
                    replacement_record_id=record_id,
                    actor=actor,
                    reason=reason,
                )
                unknown.append((old_record_id, "", reason))
                continue
            if current_owner != actor:
                reason = "registration_actor_mismatch"
                self._record_unknown(
                    old_record_id,
                    replacement_record_id=record_id,
                    actor=actor,
                    reason=reason,
                )
                unknown.append((old_record_id, "", reason))
                continue
            if old_owner != current_owner:
                skipped_foreign.append(old_record_id)
                continue
            # A shared Target is a collision signal, not proof that these records
            # are generations of one asset. Only purge owns an exact old
            # record_id plus its Registry owner, so registration cannot delete.
            reason = "asset_identity_unproven"
            self._record_unknown(
                old_record_id,
                replacement_record_id=record_id,
                actor=actor,
                reason=reason,
            )
            unknown.append((old_record_id, "", reason))
        return McpReregistrationCleanupReport(
            skipped_foreign=tuple(skipped_foreign),
            unknown=tuple(unknown),
        )
