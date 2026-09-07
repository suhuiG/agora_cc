"""Derive deployed Gateway targets from MCP tool sensitivity tags."""

from __future__ import annotations

import json
from dataclasses import dataclass

from ....shared.permission_group import PermissionGroup, allowed_tags
from ....shared.slug import gateway_target_name


@dataclass(frozen=True)
class TargetSlice:
    sensitivity: str
    gateway_target_name: str
    tools_inline: str
    operations: tuple[str, ...]


@dataclass(frozen=True)
class TargetSplit:
    slices: tuple[TargetSlice, ...]
    unassigned_tools: tuple[dict[str, str], ...]


def sensitivity_tags() -> tuple[str, ...]:
    """Return the shared sensitivity vocabulary in deterministic order."""
    ordered: list[str] = []
    for group in PermissionGroup:
        for tag in sorted(allowed_tags(group)):
            if tag not in ordered:
                ordered.append(tag)
    return tuple(ordered)


def gateway_target_names(asset_name: str) -> dict[str, str]:
    """Return every possible sensitivity-derived target name for an asset."""
    return {
        sensitivity: gateway_target_name(
            asset_name,
            sensitivity=sensitivity,
        )
        for sensitivity in sensitivity_tags()
    }


def split_target_slices(asset_name: str, tools_inline: str) -> TargetSplit:
    """Partition an inline MCP schema into non-empty, fail-closed slices."""
    try:
        document = json.loads(tools_inline)
        tools = document["tools"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"tool schema JSON must contain a tools list: {exc}"
        ) from exc
    if not isinstance(document, dict) or not isinstance(tools, list):
        raise ValueError("tool schema JSON must contain a tools list")

    tags = sensitivity_tags()
    buckets: dict[str, list[dict]] = {tag: [] for tag in tags}
    unassigned: list[dict[str, str]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name") or "")
        raw_sensitivity = tool.get("sensitivity")
        sensitivity = (
            raw_sensitivity.strip().upper()
            if isinstance(raw_sensitivity, str)
            else ""
        )
        if not sensitivity:
            unassigned.append({
                "name": name,
                "reason": "missing_sensitivity",
            })
            continue
        if sensitivity not in buckets:
            unassigned.append({
                "name": name,
                "reason": "unknown_sensitivity",
                "sensitivity": sensitivity,
            })
            continue
        buckets[sensitivity].append(tool)

    names = gateway_target_names(asset_name)
    slices: list[TargetSlice] = []
    for sensitivity in tags:
        slice_tools = buckets[sensitivity]
        if not slice_tools:
            continue
        payload = {**document, "tools": slice_tools}
        slices.append(TargetSlice(
            sensitivity=sensitivity,
            gateway_target_name=names[sensitivity],
            tools_inline=json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            operations=tuple(
                str(tool["name"])
                for tool in slice_tools
                if tool.get("name")
            ),
        ))
    return TargetSplit(tuple(slices), tuple(unassigned))
