"""Execution-neutral agent definition shared by the Runtime deploy paths."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


@dataclass(frozen=True)
class BlueprintRevisionRef:
    blueprint_id: str
    revision: str

    def to_dict(self) -> dict[str, str]:
        return {"blueprint_id": self.blueprint_id, "revision": self.revision}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BlueprintRevisionRef":
        return cls(
            blueprint_id=str(value["blueprint_id"]),
            revision=str(value["revision"]),
        )


class ExecutionKind(str, Enum):
    RUNTIME = "RUNTIME"


class BuiltinTool(str, Enum):
    BROWSER = "browser"
    CODE_INTERPRETER = "code_interpreter"


BUILTIN_TOOLS_DISABLED_MESSAGE = (
    "AgentCore 내장 도구는 SDK 충돌로 일시 비활성입니다. "
    "strands-agents-tools extras가 bedrock-agentcore<1.2.0을 요구해 "
    "검증된 1.22.0과 함께 설치할 수 없습니다. 내장 도구 없이 배포해 주세요."
)


def normalize_builtin_tools(values: Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if not isinstance(values, (list, tuple)):
        raise ValueError("builtin_tools must be an array")
    try:
        return tuple(dict.fromkeys(BuiltinTool(str(value)).value for value in values))
    except ValueError as exc:
        raise ValueError(
            "builtin_tools supports only browser and code_interpreter"
        ) from exc


def reject_disabled_builtin_tools(values: Any) -> tuple[str, ...]:
    normalized = normalize_builtin_tools(values)
    if normalized:
        raise ValueError(BUILTIN_TOOLS_DISABLED_MESSAGE)
    return ()


@dataclass(frozen=True)
class AgentExecutionBinding:
    kind: ExecutionKind
    revision: BlueprintRevisionRef
    resource_arn: str
    version: str = ""
    endpoint_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "revision": self.revision.to_dict(),
            "resource_arn": self.resource_arn,
            "version": self.version,
            "endpoint_name": self.endpoint_name,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AgentExecutionBinding":
        return cls(
            kind=ExecutionKind(value["kind"]),
            revision=BlueprintRevisionRef.from_dict(value["revision"]),
            resource_arn=str(value["resource_arn"]),
            version=str(value.get("version", "")),
            endpoint_name=str(value.get("endpoint_name", "")),
        )


@dataclass(frozen=True)
class CompatibilityFinding:
    code: str
    message: str
    feature: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "feature": self.feature}


@dataclass(frozen=True)
class BlueprintModel:
    provider: str
    model_id: str
    temperature: float | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "provider": self.provider,
            "model_id": self.model_id,
        }
        if self.temperature is not None:
            value["temperature"] = self.temperature
        return value


@dataclass(frozen=True)
class BlueprintTool:
    asset_id: str
    kind: str
    operations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "kind": self.kind,
            "operations": list(self.operations),
        }


@dataclass(frozen=True)
class BlueprintSkill:
    asset_id: str

    def to_dict(self) -> dict[str, str]:
        return {"asset_id": self.asset_id}


@dataclass(frozen=True)
class AgentBlueprint:
    schema_version: int
    name: str
    description: str
    model: BlueprintModel
    system_prompt: str
    tools: tuple[BlueprintTool, ...] = ()
    skills: tuple[BlueprintSkill, ...] = ()
    memory: dict[str, Any] = field(default_factory=dict)
    limits: dict[str, int] = field(default_factory=dict)
    truncation: dict[str, Any] = field(default_factory=dict)
    execution_preference: str = "AUTO"
    builtin_tools: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AgentBlueprint":
        model = value.get("model") or {}
        return cls(
            schema_version=int(value.get("schema_version", 1)),
            name=str(value["name"]).strip(),
            description=str(value.get("description", "")),
            model=BlueprintModel(
                provider=str(model.get("provider", "BEDROCK")).upper(),
                model_id=str(model.get("model_id", "")),
                temperature=(
                    float(model["temperature"])
                    if model.get("temperature") is not None
                    else None
                ),
            ),
            system_prompt=str(value.get("system_prompt", "")),
            tools=tuple(
                BlueprintTool(
                    asset_id=str(tool["asset_id"]),
                    kind=str(tool.get("kind", "MCP")).upper(),
                    operations=tuple(
                        dict.fromkeys(str(op) for op in tool.get("operations", ()))
                    ),
                )
                for tool in value.get("tools", ())
            ),
            skills=tuple(
                BlueprintSkill(asset_id=str(skill["asset_id"]))
                for skill in value.get("skills", ())
            ),
            memory=dict(value.get("memory") or {}),
            limits={
                str(key): int(item)
                for key, item in (value.get("limits") or {}).items()
            },
            truncation=dict(value.get("truncation") or {}),
            execution_preference=str(
                value.get("execution_preference", "AUTO")
            ).upper(),
            builtin_tools=normalize_builtin_tools(value.get("builtin_tools")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "description": self.description,
            "model": self.model.to_dict(),
            "system_prompt": self.system_prompt,
            "tools": [tool.to_dict() for tool in self.tools],
            "skills": [skill.to_dict() for skill in self.skills],
            "memory": dict(self.memory),
            "limits": dict(self.limits),
            "truncation": dict(self.truncation),
            "execution_preference": self.execution_preference,
            "builtin_tools": list(self.builtin_tools),
        }
