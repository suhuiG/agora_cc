"""AgentCore Gateway tool-name projections shared across runtime surfaces."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import re

from .permission_group import PermissionGroup, allowed_tags

_GATEWAY_TOOL_SEPARATOR = "___"
_TOOL_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")


@dataclass(frozen=True)
class DeclaredAgentTool:
    """One Registry-owned Agent tool declaration."""

    kind: str
    asset_id: str
    target_name: str
    operation: str
    label: str
    operations_declared: bool
    expected_tool_names: tuple[str, ...]


class McpGatewayTargetError(ValueError):
    """Registry MCP Target ledger is malformed or cannot resolve an operation."""


@dataclass(frozen=True)
class McpGatewayTarget:
    """One Registry-owned sensitivity Target and its operation membership."""

    name: str
    sensitivity: str
    operations: tuple[str, ...]
    target_id: str


@dataclass(frozen=True)
class McpGatewayTargetIndex:
    """Validated operation-to-Target index with an explicit legacy mode."""

    split: bool
    targets: tuple[McpGatewayTarget, ...] = ()
    legacy_target_name: str | None = None

    def target_name(self, operation_id: str) -> str | None:
        operation = operation_id.strip()
        if self.split:
            return next(
                (
                    target.name
                    for target in self.targets
                    if operation in target.operations
                ),
                None,
            )
        return self.legacy_target_name

    def select(
        self,
        operations: Iterable[str] | None = None,
    ) -> tuple[McpGatewayTarget, ...]:
        """Return split bindings narrowed to operations, rejecting gaps."""
        if not self.split:
            return ()
        if operations is None:
            raise McpGatewayTargetError(
                "split MCP Gateway Target 선택에는 operations가 필요해요."
            )
        requested = tuple(dict.fromkeys(
            operation.strip()
            for operation in operations
            if isinstance(operation, str) and operation.strip()
        ))
        selected: list[McpGatewayTarget] = []
        assigned: set[str] = set()
        for target in self.targets:
            target_operations = tuple(
                operation
                for operation in target.operations
                if operation in requested
            )
            if not target_operations:
                continue
            assigned.update(target_operations)
            selected.append(McpGatewayTarget(
                name=target.name,
                sensitivity=target.sensitivity,
                operations=target_operations,
                target_id=target.target_id,
            ))
        missing = sorted(set(requested) - assigned)
        if missing:
            raise McpGatewayTargetError(
                "Gateway Target에 배정되지 않은 MCP operation이에요: "
                + ", ".join(missing)
            )
        return tuple(selected)


def mcp_gateway_target_index(descriptors: object) -> McpGatewayTargetIndex:
    """Read the canonical operation-to-Target mapping from an MCP descriptor."""
    mcp = descriptors.get("mcp") if isinstance(descriptors, dict) else None
    if not isinstance(mcp, dict):
        raise McpGatewayTargetError("MCP descriptor가 올바르지 않아요.")
    if "gatewayTargets" not in mcp:
        legacy_name = mcp.get("gatewayTargetName")
        if legacy_name is None:
            return McpGatewayTargetIndex(split=False)
        if not isinstance(legacy_name, str) or not legacy_name.strip():
            raise McpGatewayTargetError(
                "legacy MCP gatewayTargetName이 올바르지 않아요."
            )
        return McpGatewayTargetIndex(
            split=False,
            legacy_target_name=legacy_name.strip(),
        )

    raw_targets = mcp.get("gatewayTargets")
    if not isinstance(raw_targets, list):
        raise McpGatewayTargetError(
            "MCP gatewayTargets 원장이 배열이 아니에요."
        )
    known_sensitivities = allowed_tags(PermissionGroup.FULL_ACCESS)
    targets: list[McpGatewayTarget] = []
    operation_owner: dict[str, str] = {}
    names: set[str] = set()
    sensitivities: set[str] = set()
    for raw_target in raw_targets:
        if not isinstance(raw_target, dict):
            raise McpGatewayTargetError(
                "MCP gatewayTargets 원장에 객체가 아닌 항목이 있어요."
            )
        name = raw_target.get("gatewayTargetName")
        target_id = raw_target.get("gatewayTargetId")
        state = raw_target.get("gatewayTargetState")
        sensitivity = raw_target.get("sensitivity")
        operations = raw_target.get("operations")
        normalized_sensitivity = (
            sensitivity.strip().upper()
            if isinstance(sensitivity, str)
            else ""
        )
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(target_id, str)
            or not target_id.strip()
            or not isinstance(state, str)
            or state.strip().lower() != "ready"
            or normalized_sensitivity not in known_sensitivities
            or not isinstance(operations, list)
            or not operations
            or not all(
                isinstance(operation, str) and operation.strip()
                for operation in operations
            )
        ):
            raise McpGatewayTargetError(
                "MCP gatewayTargets 원장이 불완전해요."
            )
        normalized_name = name.strip()
        if (
            normalized_name in names
            or normalized_sensitivity in sensitivities
        ):
            raise McpGatewayTargetError(
                "MCP gatewayTargets 이름 또는 민감도가 중복돼요."
            )
        names.add(normalized_name)
        sensitivities.add(normalized_sensitivity)
        normalized_operations = tuple(dict.fromkeys(
            operation.strip() for operation in operations
        ))
        for operation in normalized_operations:
            previous = operation_owner.setdefault(
                operation,
                normalized_name,
            )
            if previous != normalized_name:
                raise McpGatewayTargetError(
                    "MCP operation이 여러 Gateway Target에 속해요: "
                    + operation
                )
        targets.append(McpGatewayTarget(
            name=normalized_name,
            sensitivity=normalized_sensitivity,
            operations=normalized_operations,
            target_id=target_id.strip(),
        ))
    return McpGatewayTargetIndex(split=True, targets=tuple(targets))


def gateway_tool_name(target_name: str, operation_id: str) -> str:
    """Render the tool name exposed by Gateway and used by Cedar actions."""
    return f"{target_name}{_GATEWAY_TOOL_SEPARATOR}{operation_id}"


def gateway_tool_names(target_name: str, operation_id: str) -> tuple[str, ...]:
    """Return exact Gateway/Strands name variants for one operation."""
    targets = dict.fromkeys((target_name, target_name.replace("-", "_")))
    return tuple(gateway_tool_name(target, operation_id) for target in targets)


def gateway_tool_prefixes(target_name: str) -> tuple[str, ...]:
    """Return observed Gateway/Strands target-prefix variants."""
    targets = dict.fromkeys((target_name, target_name.replace("-", "_")))
    return tuple(f"{target}{_GATEWAY_TOOL_SEPARATOR}" for target in targets)


def is_safe_tool_identifier(value: object) -> bool:
    """Return whether a runtime tool name is safe metadata."""
    return isinstance(value, str) and bool(_TOOL_IDENTIFIER_RE.fullmatch(value))


def declared_agent_tools(descriptors: object) -> tuple[DeclaredAgentTool, ...]:
    """Project Agent tool declarations from the Registry descriptor."""
    agent = (
        descriptors.get("agent")
        if isinstance(descriptors, dict)
        else None
    )
    agent = agent if isinstance(agent, dict) else {}
    dependencies = agent.get("agoraDependencies")
    assets = (
        dependencies.get("mcpAssets")
        if isinstance(dependencies, dict)
        else None
    )
    declarations: list[DeclaredAgentTool] = []
    for asset in assets or ():
        if not isinstance(asset, dict):
            continue
        target_name = str(asset.get("name") or "").strip()
        asset_id = str(asset.get("assetId") or "").strip()
        if "gatewayTargets" in asset:
            raw_targets = asset.get("gatewayTargets")
            valid_targets: list[tuple[str, list[str]]] = []
            if isinstance(raw_targets, (list, tuple)):
                for target in raw_targets:
                    if not isinstance(target, dict):
                        valid_targets = []
                        break
                    split_name = str(target.get("name") or "").strip()
                    raw_operations = target.get("operations")
                    if (
                        not split_name
                        or not isinstance(raw_operations, (list, tuple))
                        or not raw_operations
                        or not all(
                            isinstance(operation, str) and operation.strip()
                            for operation in raw_operations
                        )
                    ):
                        valid_targets = []
                        break
                    valid_targets.append((
                        split_name,
                        list(dict.fromkeys(raw_operations)),
                    ))
                else:
                    for split_name, split_operations in valid_targets:
                        declarations.extend(
                            DeclaredAgentTool(
                                kind="mcp",
                                asset_id=asset_id,
                                target_name=split_name,
                                operation=operation,
                                label=f"{split_name}:{operation}",
                                operations_declared=True,
                                expected_tool_names=gateway_tool_names(
                                    split_name,
                                    operation,
                                ),
                            )
                            for operation in split_operations
                        )
                    continue
            declarations.append(
                DeclaredAgentTool(
                    kind="mcp",
                    asset_id=asset_id,
                    target_name=target_name,
                    operation="",
                    label=target_name or asset_id,
                    operations_declared=False,
                    expected_tool_names=(),
                )
            )
            continue
        operations = [
            str(operation)
            for operation in (asset.get("operations") or ())
            if str(operation).strip()
        ]
        if "operations" in asset and operations and target_name:
            declarations.extend(
                DeclaredAgentTool(
                    kind="mcp",
                    asset_id=asset_id,
                    target_name=target_name,
                    operation=operation,
                    label=f"{target_name}:{operation}",
                    operations_declared=True,
                    expected_tool_names=gateway_tool_names(
                        target_name,
                        operation,
                    ),
                )
                for operation in operations
            )
            continue
        declarations.append(
            DeclaredAgentTool(
                kind="mcp",
                asset_id=asset_id,
                target_name=target_name,
                operation="",
                label=target_name or asset_id,
                operations_declared=False,
                expected_tool_names=(),
            )
        )
    declarations.extend(
        DeclaredAgentTool(
            kind="builtin",
            asset_id="",
            target_name="",
            operation="",
            label=name,
            operations_declared=True,
            expected_tool_names=(name,),
        )
        for name in agent.get("builtinTools") or ()
        if isinstance(name, str) and name
    )
    return tuple(declarations)


def declared_agent_tool_names(
    descriptors: object,
) -> tuple[str, ...] | None:
    """Return one canonical runtime name per resolvable declaration."""
    agent = descriptors.get("agent") if isinstance(descriptors, dict) else None
    if not isinstance(agent, dict):
        return None
    dependencies = agent.get("agoraDependencies")
    if dependencies is not None and not isinstance(dependencies, dict):
        return None
    assets = dependencies.get("mcpAssets") if dependencies is not None else ()
    if not isinstance(assets, (list, tuple)) or any(
        not isinstance(asset, dict) for asset in assets
    ):
        return None
    builtin_tools = agent.get("builtinTools", ())
    if not isinstance(builtin_tools, (list, tuple)) or any(
        not is_safe_tool_identifier(name) for name in builtin_tools
    ):
        return None
    declarations = declared_agent_tools(descriptors)
    if any(
        not declaration.operations_declared
        or not declaration.expected_tool_names
        or not is_safe_tool_identifier(declaration.expected_tool_names[0])
        for declaration in declarations
    ):
        return None
    return tuple(
        dict.fromkeys(
            declaration.expected_tool_names[0]
            for declaration in declarations
            if declaration.expected_tool_names
        )
    )
