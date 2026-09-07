"""Operational owner and escalation contract for catalog assets."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from ...shared.responsibility import (
    ResponsibilityChange,
    ResponsibilityConflict,
    ResponsibilityContacts,
    ResponsibilityIncomplete,
    ResponsibilityStatus,
)

_EMAIL_ROUTE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _normalise_email(value: str) -> str:
    route = (value or "").strip().lower()
    if len(route) > 254 or not _EMAIL_ROUTE.fullmatch(route):
        raise ValueError("contact route must be a valid email address")
    return route


def responsibility_status(
    record_id: str,
    owner_contact: str,
    escalation_contact: str,
) -> ResponsibilityStatus:
    owner = (owner_contact or "").strip().lower()
    escalation = (escalation_contact or "").strip().lower()
    reasons: list[str] = []
    if not owner:
        reasons.append("owner_contact_required")
    elif len(owner) > 254 or not _EMAIL_ROUTE.fullmatch(owner):
        reasons.append("owner_contact_invalid")
    if not escalation:
        reasons.append("escalation_contact_required")
    elif len(escalation) > 254 or not _EMAIL_ROUTE.fullmatch(escalation):
        reasons.append("escalation_contact_invalid")
    if owner and escalation and owner == escalation:
        reasons.append("distinct_escalation_contact_required")
    return ResponsibilityStatus(
        record_id=record_id,
        contacts=ResponsibilityContacts(owner, escalation),
        status="complete" if not reasons else "incomplete",
        blocking_reasons=tuple(reasons),
    )


class CatalogAssetResponsibility:
    """Owns validation, atomic contact changes, and append-only history."""

    def __init__(self, registry, registry_id: str, store, *, now=None, new_id=None):
        self._registry = registry
        self._registry_id = registry_id
        self._store = store
        self._now = now or (lambda: datetime.now(timezone.utc).isoformat())
        self._new_id = new_id or (lambda: uuid.uuid4().hex)

    def get(self, record_id: str) -> ResponsibilityStatus:
        self._registry.get_record(self._registry_id, record_id)
        ext = self._store.get_ext(record_id)
        return responsibility_status(
            record_id,
            str(ext.get("owner_contact") or ""),
            str(ext.get("escalation_contact") or ""),
        )

    def change(
        self,
        record_id: str,
        *,
        owner_contact: str,
        escalation_contact: str,
        changed_by: str,
        reason: str,
    ) -> ResponsibilityStatus:
        self._registry.get_record(self._registry_id, record_id)
        owner = _normalise_email(owner_contact)
        escalation = _normalise_email(escalation_contact)
        if owner == escalation:
            raise ValueError("escalation contact must differ from owner contact")
        actor = (changed_by or "").strip()
        change_reason = (reason or "").strip()
        if not actor:
            raise ValueError("changed_by is required")
        if not change_reason:
            raise ValueError("change reason is required")

        ext = self._store.get_ext(record_id)
        before = ResponsibilityContacts(
            str(ext.get("owner_contact") or ""),
            str(ext.get("escalation_contact") or ""),
        )
        after = ResponsibilityContacts(owner, escalation)
        event = ResponsibilityChange(
            event_id=self._new_id(),
            record_id=record_id,
            changed_at=self._now(),
            changed_by=actor,
            reason=change_reason,
            before=before,
            after=after,
        )
        try:
            self._store.apply_responsibility_change(event)
        except ResponsibilityConflict:
            raise
        return responsibility_status(record_id, owner, escalation)

    def history(self, record_id: str) -> tuple[ResponsibilityChange, ...]:
        # Audit outlives the mutable catalog record. Requiring a Registry read here
        # would make preserved events unqueryable after a hard purge.
        return tuple(self._store.list_responsibility_changes(record_id))

    def require_for_approval(self, record_id: str) -> ResponsibilityStatus:
        status = self.get(record_id)
        if status.status != "complete":
            raise ResponsibilityIncomplete(status)
        return status
