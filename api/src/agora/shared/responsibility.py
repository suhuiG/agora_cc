"""Domain-neutral contract for operational asset responsibility."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ResponsibilityContacts:
    owner_contact: str
    escalation_contact: str


@dataclass(frozen=True)
class ResponsibilityStatus:
    record_id: str
    contacts: ResponsibilityContacts
    status: str
    blocking_reasons: tuple[str, ...]
    required_for: str = "production_approval"
    authorization_effect: str = "none"


@dataclass(frozen=True)
class ResponsibilityChange:
    event_id: str
    record_id: str
    changed_at: str
    changed_by: str
    reason: str
    before: ResponsibilityContacts
    after: ResponsibilityContacts


class ResponsibilityIncomplete(ValueError):
    """The asset is not eligible for production approval."""

    def __init__(self, status: ResponsibilityStatus) -> None:
        self.status = status
        super().__init__(", ".join(status.blocking_reasons))


class ResponsibilityConflict(RuntimeError):
    """The responsibility record changed concurrently."""


@runtime_checkable
class AssetResponsibilityPort(Protocol):
    def get(self, record_id: str) -> ResponsibilityStatus: ...

    def change(
        self,
        record_id: str,
        *,
        owner_contact: str,
        escalation_contact: str,
        changed_by: str,
        reason: str,
    ) -> ResponsibilityStatus: ...

    def history(self, record_id: str) -> tuple[ResponsibilityChange, ...]: ...

    def require_for_approval(self, record_id: str) -> ResponsibilityStatus: ...
