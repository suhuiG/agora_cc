"""Cross-domain contract for removing obsolete MCP capability approvals."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AssetCapabilityCleanupReport:
    deleted: tuple[tuple[str, str], ...] = ()
    unknown: tuple[tuple[str, str], ...] = ()


class AssetCapabilityCleaner(Protocol):
    def delete_record_approvals(
        self,
        record_id: str,
        *,
        replacement_record_id: str,
        actor: str,
    ) -> AssetCapabilityCleanupReport: ...

    def record_unknown(
        self,
        record_id: str,
        *,
        replacement_record_id: str,
        actor: str,
        reason: str,
    ) -> None: ...
