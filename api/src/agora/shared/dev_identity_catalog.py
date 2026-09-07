"""Catalog adapter for the shared dev-identity asset contract."""
from __future__ import annotations

from ..domains.catalog.registry.models import RecordNotFound
from .dev_identity import DevAssetAuthorizationContext, DevAssetTarget
from .gateway_tools import mcp_gateway_target_index


def catalog_dev_asset_context(record) -> DevAssetAuthorizationContext | None:
    """Project an approved MCP Registry record without deciding inheritance."""
    descriptor_type = getattr(record, "descriptor_type", "")
    status = getattr(record, "status", "")
    if (
        getattr(descriptor_type, "value", descriptor_type) != "MCP"
        or getattr(status, "value", status) != "APPROVED"
    ):
        return None
    target_index = mcp_gateway_target_index(
        getattr(record, "descriptors", None)
    )
    if target_index.split:
        targets = tuple(
            DevAssetTarget(target.name, target.operations, target.sensitivity)
            for target in target_index.targets
        )
    elif target_index.legacy_target_name:
        targets = (DevAssetTarget(target_index.legacy_target_name),)
    else:
        targets = ()
    version = str(getattr(record, "version", "") or "")
    return DevAssetAuthorizationContext(
        asset_id=str(getattr(record, "record_id", "") or ""),
        asset_version=version,
        targets=targets,
    )


class CatalogDevAssetResolver:
    """Resolve only the selected live record; historical binding is identity-owned."""

    def __init__(self, registry, registry_id: str) -> None:
        self._registry = registry
        self._registry_id = registry_id

    def resolve(
        self,
        asset_id: str,
        asset_version: str,
    ) -> DevAssetAuthorizationContext | None:
        try:
            record = self._registry.get_record(self._registry_id, asset_id)
        except RecordNotFound:
            # AWS adapter는 Registry 부재와 record 부재를 같은 예외로 접어요.
            # Registry를 독립적으로 관측할 수 있을 때만 확정적인 missing으로 봅니다.
            self._registry.list_records(self._registry_id, max_results=1)
            return None
        context = catalog_dev_asset_context(record)
        if (
            context is None
            or context.asset_id != asset_id
            or context.asset_version != asset_version
        ):
            return None
        return context
