"""플레이그라운드 도메인 라우터 — Initializr 생성과 배포 Agent 실행.

- POST /api/playground/prompt   : 설명+도구 → system prompt (Bedrock+폴백)
- POST /api/playground/scaffold  : spec → files(JSON) 또는 ?format=zip → zip
- POST /api/playground/invoke    : 배포된 runtime + 메시지 → AgentCore 응답
- GET  /api/playground/agents/{record_id}/logs : 배포 runtime 로그
- GET  /api/playground/agents/{record_id}/run-config : 선언 구성 + 실체 관측
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from ...shared.bedrock_models import validate_model_id
from ...shared.agent_blueprint import reject_disabled_builtin_tools
from ...shared.config import load_config
from ...shared.dev_identity import (
    DevAuthorizationFailure,
    DevAuthorizationReason,
    DevAuthorizationState,
    DevIdentityAuthorizationError,
    DevIdentityNoAccessError,
    DevSelectedTool,
    dev_identity_blueprint_id,
)
from ...shared.monitoring_traffic import INVOCATION_OUTCOME_FAILURE
from ...shared.gateway_tools import (
    McpGatewayTargetError,
    declared_agent_tools,
    gateway_tool_names,
    gateway_tool_prefixes,
    mcp_gateway_target_index,
)
from ...shared.deps import (
    enforce_agent_invoke_gate,
    get_agentcore_invoker,
    get_agentcore_memory_control_client,
    get_catalog_endpoint,
    get_delegated_asset_ids,
    get_delegated_workload_binding,
    get_delegation_service,
    get_current_principal,
    get_dev_identity_service,
    get_prompt_service,
    find_catalog_record,
    read_tool_approval_states,
    record_agent_invoke_audit,
    record_agent_invoke_usage,
)
from .invoke_service import InvokeError, require_user_token_lifetime
from .scaffold import (
    TIMEOUT_UNSUPPORTED_MESSAGE,
    ScaffoldSpec, ToolRef, build_scaffold, normalize_truncation_strategy,
    scaffold_to_tree, scaffold_to_zip,
    scaffold_with_dev_environment,
)

router = APIRouter(tags=["playground"])


class ToolInput(BaseModel):
    name: str
    kind: str
    asset_id: str | None = None
    description: str = ""
    endpoint: str | None = None
    # skill 본문을 S3에서 찾기 위한 좌표. 프론트가 카탈로그 카드에서 그대로 실어줘요
    # ("skill/{owner}/{name}/{version}/" 형식).
    source_prefix: str | None = None
    version: str | None = None
    # 서버가 채우는 SKILL.md 본문. 클라이언트가 직접 보낼 필요는 없어요.
    skill_markdown: str | None = None
    # 서버가 카탈로그 descriptor에서 채우는 Gateway target name(결함 #10). MCP tool
    # authorization 비교 기준이에요. 클라이언트가 보내도 아래 바인딩에서 덮어써요.
    target_name: str | None = None
    # 서버가 Registry gatewayTargets[]에서 확정한 operation별 Target 이름.
    operation_targets: dict[str, str] = Field(default_factory=dict)
    # MCP는 하나 이상 명시해야 해요. 빈 목록은 전체로 확장하지 않고 422로 거부해요.
    operations: list[str] = Field(default_factory=list)
    # 서버가 Registry descriptor의 명시 태그로 채워요. 클라이언트 값은 아래
    # 카탈로그 재결속에서 덮어써 probe 안전 근거를 위조하지 못하게 해요.
    operation_sensitivities: dict[str, str] = Field(default_factory=dict)
    authorization_probe_operations: list[str] = Field(default_factory=list)


class PromptRequest(BaseModel):
    name: str = ""
    description: str = ""
    tools: list[ToolInput] = Field(default_factory=list)


# 도구 왕복 하나가 메시지 4개를 쓰고, Memory 복원이 toolResult 메시지를 버려 역할 교대가
# 깨지므로 하한을 여유 있게 잡아요(IH-70 실측 근거는 아래 필드 주석).
SLIDING_WINDOW_MIN = 10


class SlidingWindowTruncation(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_name=True)

    strategy: Literal["sliding_window"]
    # IH-70: 최소값이 1이면 도구를 쓰는 agent 가 구조적으로 깨져요. 도구 왕복 하나가
    # user + assistant(toolUse) + user(toolResult) + assistant 로 **4개**를 쓰고, 다음 질문
    # 까지 5개예요. 게다가 복원 이력에서 tool 블록을 떼면 toolResult 만 담긴 메시지가
    # 사라져서 역할 교대가 깨진 이력이 만들어져요. 그 정리는 SDK 가 아니라 생성 코드의
    # `_repair_restored_conversation` 이 해요 — `_filter_restored_tool_context` 는 기본값
    # `False` 이고 우리가 켜지 않아요(IH-99, 2026-08-22 정정).
    # 실측(2026-08-22): window_size=5 + MCP 도구 조합에서 5번째 턴부터 Bedrock 이
    # `The conversation must end with a user message` 로 거부하고 세션이 오염됐어요.
    # 그래서 Agora 는 하한을 10 으로 고정해요.
    window_size: int = Field(
        default=40,
        ge=SLIDING_WINDOW_MIN,
        validation_alias=AliasChoices("window_size", "num_messages"),
    )

    @model_validator(mode="before")
    @classmethod
    def reject_conflicting_window_aliases(cls, value):
        if (
            isinstance(value, dict)
            and "window_size" in value
            and "num_messages" in value
        ):
            raise ValueError("window_size and num_messages cannot both be set")
        return value


class NoTruncation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: Literal["none"]


TruncationRequest = Annotated[
    SlidingWindowTruncation | NoTruncation,
    Field(discriminator="strategy"),
]


class DisabledMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["DISABLED"] = "DISABLED"


class NamedMemoryStrategyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_]{0,47}$")


class MemoryStrategyConfigs(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    semantic: NamedMemoryStrategyConfig | None = Field(
        default=None,
        alias="SEMANTIC",
    )
    summarization: NamedMemoryStrategyConfig | None = Field(
        default=None,
        alias="SUMMARIZATION",
    )
    def selected(self) -> set[str]:
        return {
            strategy
            for strategy, config in (
                ("SEMANTIC", self.semantic),
                ("SUMMARIZATION", self.summarization),
            )
            if config is not None
        }


class ManagedMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["MANAGED"]
    strategies: list[
        Literal[
            "SEMANTIC",
            "SUMMARIZATION",
        ]
    ] = Field(min_length=1)
    strategy_configs: MemoryStrategyConfigs | None = None
    retention_days: int = Field(default=30, ge=3, le=365)

    @model_validator(mode="after")
    def configs_match_selected_strategies(self):
        if len(self.strategies) != len(set(self.strategies)):
            raise ValueError("memory strategies must not contain duplicates")
        if self.strategy_configs is None:
            return self
        if self.strategy_configs.selected() != set(self.strategies):
            raise ValueError(
                "strategy_configs must match selected memory strategies"
            )
        return self


MemoryRequest = Annotated[
    DisabledMemory | ManagedMemory,
    Field(discriminator="mode"),
]


class ScaffoldRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    model: str = "sonnet-5"
    description: str = ""
    system_prompt: str = ""
    tools: list[ToolInput] = Field(default_factory=list)
    memory: MemoryRequest = Field(default_factory=DisabledMemory)
    truncation: TruncationRequest | None = None
    max_tokens: int | None = Field(
        default=None,
        gt=0,
        validation_alias=AliasChoices(
            "max_tokens", AliasPath("limits", "max_tokens")
        ),
    )
    max_iterations: int | None = Field(
        default=None,
        gt=0,
        validation_alias=AliasChoices(
            "max_iterations", AliasPath("limits", "max_iterations")
        ),
    )
    timeout_seconds: int | None = Field(
        default=None,
        gt=0,
        validation_alias=AliasChoices(
            "timeout_seconds", AliasPath("limits", "timeout_seconds")
        ),
    )
    builtin_tools: list[Literal["browser", "code_interpreter"]] = Field(
        default_factory=list
    )

    @field_validator("builtin_tools")
    @classmethod
    def reject_builtin_tools(
        cls,
        value: list[Literal["browser", "code_interpreter"]],
    ) -> list[Literal["browser", "code_interpreter"]]:
        reject_disabled_builtin_tools(value)
        return value

    @field_validator("model")
    @classmethod
    def supported_model(cls, value: str) -> str:
        return validate_model_id(value)

    @field_validator("truncation", mode="before")
    @classmethod
    def supported_truncation(cls, value):
        if not value:
            return None
        if not isinstance(value, dict):
            return value
        return {
            **value,
            "strategy": normalize_truncation_strategy(value.get("strategy")),
        }

    @field_validator("timeout_seconds")
    @classmethod
    def reject_unsupported_timeout(cls, value: int | None) -> int | None:
        if value is not None:
            raise ValueError(TIMEOUT_UNSUPPORTED_MESSAGE)
        return value


class InvokeRequest(BaseModel):
    record_id: str
    prompt: str
    session_id: str


def _verified_user_token(
    request: Request,
    *,
    principal_source: str,
    token_expires_at: int,
) -> str | None:
    """Cognito middleware가 검증한 access token만 Runtime relay에 사용해요."""
    if principal_source != "cognito":
        return None
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        message = "검증된 사람 토큰을 Runtime에 전달할 수 없어요."
        raise InvokeError(
            message,
            status=409,
            detail={
                "message": message,
                "remediation": "세션을 갱신한 뒤 다시 시도해 주세요.",
                "reason": "user_token_not_forwarded",
            },
        )
    verified_token = token.strip()
    if not verified_token:
        message = "검증된 사람 토큰을 Runtime에 전달할 수 없어요."
        raise InvokeError(
            message,
            status=409,
            detail={
                "message": message,
                "remediation": "세션을 갱신한 뒤 다시 시도해 주세요.",
                "reason": "user_token_not_forwarded",
            },
        )
    require_user_token_lifetime(token_expires_at)
    return verified_token


def _refs(tools: list[ToolInput]) -> list[ToolRef]:
    return [ToolRef(name=t.name, kind=t.kind, asset_id=t.asset_id,
                    description=t.description,
                    endpoint=t.endpoint, skill_markdown=t.skill_markdown,
                    target_name=t.target_name, operations=list(t.operations),
                    operation_targets=dict(t.operation_targets),
                    operation_sensitivities=dict(t.operation_sensitivities),
                    authorization_probe_operations=list(
                        t.authorization_probe_operations
                    ))
            for t in tools]


def _tool_target_bindings(
    tool: ToolInput,
) -> tuple[tuple[str, list[str]], ...]:
    grouped: dict[str, list[str]] = {}
    for operation in tool.operations:
        target_name = (
            tool.operation_targets.get(operation)
            or tool.target_name
            or ""
        )
        if target_name:
            grouped.setdefault(target_name, []).append(operation)
    return tuple(grouped.items())


def _asset_coords(source_prefix: str) -> tuple[str, str] | None:
    """"skill/{owner}/{name}/{version}/" → (asset_id, version). 형식이 다르면 None."""
    parts = [p for p in (source_prefix or "").split("/") if p]
    if len(parts) < 4:
        return None
    return f"{parts[1]}/{parts[2]}", parts[3]


def _with_skill_bodies(tools: list[ToolInput]) -> list[ToolInput]:
    """소스 모드 skill의 SKILL.md 본문을 S3에서 읽어 채워요.

    build_scaffold는 순수함수 계약이라(부수효과 없음) I/O를 여기서 해요.
    개별 skill 읽기 실패는 건너뛰어요 — skill 하나 때문에 스캐폴드 생성이나 배포가
    막히면 안 되고, 본문 없는 skill은 build_scaffold가 알아서 배선에서 제외해요.
    """
    from ...shared.deps import get_source_store

    out: list[ToolInput] = []
    store = None
    for t in tools:
        if t.kind != "skill" or t.skill_markdown or not t.source_prefix:
            out.append(t)
            continue
        coords = _asset_coords(t.source_prefix)
        if coords is None:
            out.append(t)
            continue
        asset_id, version = coords
        try:
            store = store or get_source_store()
            raw = store.read_file(asset_id, t.version or version, "SKILL.md")
            body = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            out.append(t.model_copy(update={"skill_markdown": body}))
        except Exception:
            out.append(t)      # 못 읽으면 그대로 — 그 skill만 배선에서 빠져요
    return out


def _with_catalog_mcp_bindings(tools: list[ToolInput]) -> list[ToolInput]:
    """MCP asset_id와 endpoint를 승인된 카탈로그 레코드에서 다시 결속해요."""
    out: list[ToolInput] = []
    for tool in tools:
        if tool.kind != "mcp":
            out.append(tool)
            continue
        if not tool.asset_id:
            raise HTTPException(422, f"MCP 자산 ID가 필요해요: {tool.name}")
        record = find_catalog_record(tool.asset_id)
        if record is None:
            raise HTTPException(422, f"MCP 자산을 찾을 수 없어요: {tool.name}")
        if (
            record.descriptor_type.value != "MCP"
            or record.status.value != "APPROVED"
        ):
            raise HTTPException(422, f"승인된 MCP 자산만 선택할 수 있어요: {tool.name}")
        catalog_endpoint = get_catalog_endpoint(record.descriptors)
        if not catalog_endpoint:
            raise HTTPException(422, f"MCP endpoint가 없어요: {tool.name}")
        endpoint = load_config().m2_oauth_gateway_url or catalog_endpoint
        mcp_node = record.descriptors.get("mcp") if isinstance(record.descriptors, dict) else None
        try:
            target_index = mcp_gateway_target_index(record.descriptors)
        except McpGatewayTargetError as exc:
            raise HTTPException(
                422,
                f"MCP Gateway Target 원장이 올바르지 않아요: {tool.name}",
            ) from exc
        tools_node = mcp_node.get("tools") if isinstance(mcp_node, dict) else None
        tools_inline = (
            tools_node.get("inlineContent")
            if isinstance(tools_node, dict)
            else ""
        )
        try:
            tools_document = json.loads(tools_inline) if tools_inline else {}
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                422, f"MCP operation 목록이 올바르지 않아요: {tool.name}"
            ) from exc
        available_operations = {
            str(item.get("name"))
            for item in (
                tools_document.get("tools", [])
                if isinstance(tools_document, dict)
                else []
            )
            if isinstance(item, dict) and item.get("name")
        }
        if not available_operations:
            raise HTTPException(
                422, f"MCP operation 목록을 확인할 수 없어요: {tool.name}"
            )
        requested_operations = list(dict.fromkeys(tool.operations))
        if not requested_operations:
            raise HTTPException(
                422,
                f"MCP operation을 하나 이상 선택해야 해요: {tool.name}",
            )
        missing_operations = sorted(
            set(requested_operations) - available_operations
        )
        if missing_operations:
            raise HTTPException(
                422,
                "승인된 MCP에 없는 operation이에요: "
                + ", ".join(missing_operations),
            )
        operations = requested_operations
        try:
            if target_index.split:
                target_index.select(operations)
            elif not target_index.legacy_target_name:
                raise McpGatewayTargetError(
                    "legacy gatewayTargetName이 없어요."
                )
        except McpGatewayTargetError as exc:
            raise HTTPException(
                422,
                f"MCP operation의 Gateway target을 확인할 수 없어요: {tool.name}",
            ) from exc
        operation_targets = {
            operation: target_name
            for operation in available_operations
            if (target_name := target_index.target_name(operation))
        }
        selected_target_names = tuple(dict.fromkeys(
            operation_targets[operation] for operation in operations
        ))
        target_name = (
            selected_target_names[0]
            if len(selected_target_names) == 1
            else target_index.legacy_target_name
        )
        explicit_sensitivities = {
            str(item.get("name")): str(item.get("sensitivity")).strip().upper()
            for item in (
                tools_document.get("tools", [])
                if isinstance(tools_document, dict)
                else []
            )
            if (
                isinstance(item, dict)
                and item.get("sensitivity")
            )
        }
        authorization_probe_operations = sorted(
            operation
            for operation, sensitivity in explicit_sensitivities.items()
            if sensitivity == "READ" and operation not in operations
        )
        out.append(
            tool.model_copy(
                update={
                    "name": record.name,
                    "endpoint": endpoint,
                    "version": record.version,
                    "target_name": target_name,
                    "operations": operations,
                    "operation_targets": operation_targets,
                    "operation_sensitivities": explicit_sensitivities,
                    "authorization_probe_operations": (
                        authorization_probe_operations
                    ),
                }
            )
        )
    return out


@router.post("/api/playground/prompt")
def playground_prompt(req: PromptRequest):
    md = get_prompt_service().generate(
        name=req.name, description=req.description, tools=_refs(req.tools))
    return {"system_prompt": md}


def _dev_authorization_failure_message(
    failure: DevAuthorizationFailure,
    *,
    asset_name: str,
) -> str:
    operation = failure.operation_id or "선택한 operation"
    subject = f"{asset_name}의 {operation}"
    missing = ", ".join(failure.missing_capabilities)
    messages = {
        DevAuthorizationReason.ASSET_BINDING_INVALID: (
            f"{asset_name}의 현재 등록 상태가 다운로드 요청과 일치하지 않아요. "
            "도구 목록을 새로고침한 뒤 다시 선택해 주세요."
        ),
        DevAuthorizationReason.ASSET_BINDING_UNOBSERVABLE: (
            f"{asset_name}의 등록 상태를 확인하지 못했어요."
        ),
        DevAuthorizationReason.ASSET_CAPABILITY_MISSING: (
            f"{subject}에 자산 권한 정책이 없어요. 거버넌스에서 이 operation의 "
            "요구 권한을 선언해 주세요."
        ),
        DevAuthorizationReason.ASSET_CAPABILITY_UNOBSERVABLE: (
            f"{subject}의 자산 권한 정책을 확인하지 못했어요."
        ),
        DevAuthorizationReason.ASSET_CAPABILITY_NOT_APPROVED: (
            f"{subject}의 자산 권한 정책이 승인되지 않았어요. "
            "거버넌스에서 정책 상태를 확인해 주세요."
        ),
        DevAuthorizationReason.GATEWAY_TARGET_UNRESOLVED: (
            f"{subject}의 Gateway target을 찾을 수 없어요."
        ),
        DevAuthorizationReason.GATEWAY_TARGET_UNOBSERVABLE: (
            f"{subject}의 Gateway target을 확인하지 못했어요."
        ),
        DevAuthorizationReason.CONNECTION_MISSING: (
            f"{subject}의 권한 connection을 찾을 수 없어요."
        ),
        DevAuthorizationReason.CONNECTION_UNOBSERVABLE: (
            f"{subject}의 권한 connection을 확인하지 못했어요."
        ),
        DevAuthorizationReason.CONNECTION_INACTIVE: (
            f"{subject}의 권한 connection이 비활성 상태예요."
        ),
        DevAuthorizationReason.REQUIRED_CAPABILITIES_MISSING: (
            f"{subject}의 자산 권한 정책에 요구 capability가 없어요. "
            "거버넌스에서 정책을 다시 선언해 주세요."
        ),
        DevAuthorizationReason.CONNECTION_CAPABILITIES_UNOBSERVABLE: (
            f"{subject}의 connection capability를 확인하지 못했어요."
        ),
        DevAuthorizationReason.CONNECTION_CAPABILITY_INACTIVE: (
            f"{subject}에 필요한 capability가 connection에서 활성 상태가 "
            f"아니에요: {missing or '이름 확인 불가'}."
        ),
        DevAuthorizationReason.CONNECTION_CEILING_EXCEEDED: (
            f"{subject}에 필요한 capability가 connection ceiling 밖에 있어요: "
            f"{missing or '이름 확인 불가'}."
        ),
        DevAuthorizationReason.PRINCIPAL_GRANTS_UNOBSERVABLE: (
            f"{subject}에 대한 사용자 grant를 확인하지 못했어요."
        ),
        DevAuthorizationReason.PRINCIPAL_GRANT_MISSING: (
            f"{subject}에 필요한 사용자 grant가 없어요: "
            f"{missing or '이름 확인 불가'}."
        ),
        DevAuthorizationReason.OPERATION_SENSITIVITY_NOT_READ: (
            f"{subject}는 조회(READ) 도구가 아니라서 로컬 다운로드에는 담기지 않아요. "
            "배포한 agent에서 쓰거나 거버넌스에서 승인을 받아 주세요."
        ),
        DevAuthorizationReason.OPERATION_SENSITIVITY_UNKNOWN: (
            f"{subject}의 민감도가 Registry에 없어서 로컬 다운로드 허용 여부를 판정할 수 "
            "없어요. 해당 MCP를 다시 등록해 Gateway target 민감도를 붙여 주세요."
        ),
    }
    return messages[failure.reason]


def _dev_authorization_error_detail(
    error: DevIdentityAuthorizationError,
    *,
    tools: list[ToolInput],
) -> tuple[int, dict]:
    names = {
        str(tool.asset_id): tool.name
        for tool in tools
        if tool.asset_id
    }
    has_unknown = any(
        failure.state is DevAuthorizationState.UNKNOWN
        for failure in error.failures
    )
    serialized = []
    messages: list[str] = []
    for failure in error.failures:
        asset_name = names.get(failure.asset_id, failure.asset_id)
        message = _dev_authorization_failure_message(
            failure,
            asset_name=asset_name,
        )
        messages.append(message)
        serialized.append({
            "asset_id": failure.asset_id,
            "asset_name": asset_name,
            "operation_id": failure.operation_id,
            "connection_id": failure.connection_id,
            "state": failure.state.value,
            "reason": failure.reason.value,
            "missing_capabilities": list(failure.missing_capabilities),
            "message": message,
        })
    return (
        503 if has_unknown else 403,
        {
            "message": " ".join(dict.fromkeys(messages)),
            "code": (
                "DEV_IDENTITY_AUTHORIZATION_UNKNOWN"
                if has_unknown
                else "DEV_IDENTITY_AUTHORIZATION_BLOCKED"
            ),
            "failures": serialized,
            "action_url": "/governance",
        },
    )


#: 제외된 operation id 를 ZIP 응답에 실어 보내는 헤더. 값은 JSON 배열(ASCII)이에요.
#:
#: 화면이 다운로드 직후 알림을 띄우는 유일한 통로예요. 본문은 ZIP 바이너리고
#: (`res.blob()`), 다른 JSON 응답을 하나 더 만들면 왕복이 늘어나면서 두 응답이 어긋날 수
#: 있어요. 자세한 이유는 아래 사용처 주석에 있어요.
_EXCLUDED_OPERATIONS_HEADER = "X-Agora-Excluded-Operations"


def _issued_mcp_assets(
    mcps: list[ToolInput], issued
) -> list[dict]:
    """`.env` 의 `AGORA_MCP_ASSETS` — **실제로 발급된** operation 만 담아요.

    요청한 도구 목록(`_tool_target_bindings`)을 그대로 쓰면, 제외된 쓰기 도구가 크리덴셜에는
    없는데 `.env` 에는 남아요. 그러면 생성 코드가 그 도구를 모델에 노출하고, 모델이 부르면
    Gateway 가 거부해요 — 사용자에게는 "도구가 있다고 했는데 안 된다" 로 보이는 조용히 틀린
    답이지, 표시상의 문제가 아니에요.

    좌표(자산·target·operation)가 하나라도 빈 grant 는 통과시키지 않아요. 신규 발급은 네 값을
    항상 채우고(`_authorized_actions`), 못 채운 건 원장 binding 도 없어서 어차피 못 불러요.
    """
    issued_operations = {
        (grant.asset_id, grant.gateway_target_name, grant.operation_id)
        for grant in issued.record.action_grants
        if grant.asset_id and grant.gateway_target_name and grant.operation_id
    }
    assets: list[dict] = []
    for tool in mcps:
        asset_id = str(tool.asset_id)
        for target_name, target_operations in _tool_target_bindings(tool):
            operations = [
                operation
                for operation in target_operations
                if (asset_id, target_name, operation) in issued_operations
            ]
            if not operations:
                continue
            assets.append({
                "asset_id": asset_id,
                "name": target_name,
                "endpoint": str(tool.endpoint),
                "tool_prefixes": gateway_tool_prefixes(target_name),
                "operations": operations,
                "allowed_tool_names": [
                    tool_name
                    for operation in operations
                    for tool_name in gateway_tool_names(target_name, operation)
                ],
            })
    return assets


def _excluded_operations_header(issued) -> str:
    """제외된 operation id 를 헤더에 실을 ASCII JSON 배열로 만들어요.

    `ensure_ascii=True` 가 latin-1 안전성의 근거예요 — 비ASCII 도구 이름이 들어와도 헤더가
    깨지지 않게 `\\uXXXX` 로 이스케이프돼요. 사유 문구(한국어)는 절대 여기 담지 않아요.
    """
    operation_ids = [
        failure.operation_id
        for failure in issued.excluded
        if failure.operation_id
    ]
    if not operation_ids:
        return ""
    return json.dumps(list(dict.fromkeys(operation_ids)), ensure_ascii=True)


@router.post("/api/playground/scaffold")
def playground_scaffold(
    req: ScaffoldRequest,
    request: Request,
    format: str = "",
    include_dev_identity: bool = False,
):
    tools = _with_catalog_mcp_bindings(req.tools)
    spec = ScaffoldSpec(
        name=req.name, model=req.model, description=req.description,
        system_prompt=req.system_prompt, tools=_refs(_with_skill_bodies(tools)),
        truncation=req.truncation.model_dump() if req.truncation else {},
        memory=req.memory.model_dump(by_alias=True, exclude_none=True),
        max_tokens=req.max_tokens,
        max_iterations=req.max_iterations,
        timeout_seconds=req.timeout_seconds,
        builtin_tools=list(req.builtin_tools),
    )
    files = build_scaffold(spec)
    if format == "zip":
        excluded_header = ""
        if include_dev_identity:
            mcps = [
                tool for tool in tools
                if (
                    tool.kind == "mcp"
                    and tool.asset_id
                    and tool.version
                    and tool.endpoint
                )
            ]
            unresolved = [
                tool.name
                for tool in mcps
                if any(
                    not (
                        tool.operation_targets.get(operation)
                        or tool.target_name
                    )
                    for operation in tool.operations
                )
            ]
            if unresolved:
                raise HTTPException(
                    422,
                    "Gateway target을 확인할 수 없는 MCP 자산이에요: "
                    + ", ".join(unresolved),
                )
            base_url = load_config().web_base_url
            if not base_url:
                raise HTTPException(
                    503,
                    {
                        "message": (
                            "dev 크리덴셜 포함 다운로드에는 "
                            "AGORA_WEB_BASE_URL 설정이 필요해요."
                        ),
                        "code": "DEV_IDENTITY_CONFIGURATION_UNAVAILABLE",
                    },
                )
            try:
                # 그룹은 **인증된 세션**에서 와요 — 회원 기본 READ 는 사람이 아니라 그룹에
                # 달려 있어서(`catalog_read_access.py`) 이걸 안 넘기면 발급이 "grant 없음" 으로
                # 막혀요. 클라이언트가 정하는 값이면 스스로 권한을 부여할 수 있으니, 요청
                # 본문·헤더가 아니라 `Principal` 에서만 읽어요.
                principal = get_current_principal(request)
                issued = get_dev_identity_service().issue(
                    principal=principal.principal_id,
                    principal_groups=tuple(principal.roles),
                    # MCP 가 `agora_user_id` 로 받을 값 (ADR-0095). 비어 있으면 그 인자를
                    # 선언한 도구가 `owner_identity_missing` 으로 막혀요 — 조용히 봇의 값을
                    # 통과시키는 것보다 안전해요.
                    principal_email=principal.email,
                    blueprint_id=dev_identity_blueprint_id(spec.name),
                    selected_tools=tuple(
                        DevSelectedTool(
                            asset_id=str(tool.asset_id),
                            asset_version=str(tool.version),
                            operations=tuple(tool.operations),
                        )
                        for tool in mcps
                    ),
                )
            except DevIdentityAuthorizationError as exc:
                status_code, detail = _dev_authorization_error_detail(
                    exc,
                    tools=mcps,
                )
                raise HTTPException(status_code, detail) from exc
            except DevIdentityNoAccessError as exc:
                names = ", ".join(tool.name for tool in mcps)
                # 전부 제외돼서 0개가 된 경우와 애초에 통과가 없던 경우는 처방이 달라요 —
                # 전자는 거버넌스에 볼 게 없고(조회 도구를 고르는 게 답), 후자는 grant·정책을
                # 확인해야 해요. 같은 문구로 뭉치면 사용자가 없는 화면을 헤매요.
                if exc.excluded:
                    excluded_names = ", ".join(
                        dict.fromkeys(
                            failure.operation_id
                            for failure in exc.excluded
                            if failure.operation_id
                        )
                    )
                    raise HTTPException(
                        403,
                        {
                            "message": (
                                "고른 operation이 전부 로컬 다운로드 대상이 아니에요"
                                f": {excluded_names}. 로컬 실행에는 조회(READ) 도구만 "
                                "담을 수 있어요 — 조회 도구를 하나 이상 골라 주세요."
                            ),
                            "code": "DEV_IDENTITY_NO_ACCESS",
                            "excluded_operations": [
                                failure.operation_id for failure in exc.excluded
                            ],
                        },
                    ) from exc
                raise HTTPException(
                    403,
                    {
                        "message": (
                            "선택한 MCP tool에 다운로드 가능한 operation이 없어요"
                            f": {names}. 거버넌스에서 자산 권한 정책과 사용자 "
                            "grant를 확인하거나 해당 tool을 제외해 주세요."
                        ),
                        "code": "DEV_IDENTITY_NO_ACCESS",
                        "action_url": "/governance",
                    },
                ) from exc
            except Exception as exc:  # noqa: BLE001 - preserve code-only recovery.
                logging.getLogger(__name__).exception(
                    "dev identity issuance failed; blueprint_id=%s assets=%s",
                    dev_identity_blueprint_id(spec.name),
                    [str(tool.asset_id) for tool in mcps],
                )
                raise HTTPException(
                    503,
                    {
                        "message": (
                            "dev 크리덴셜 발급 인프라를 사용할 수 없어요. 잠시 후 다시 "
                            "시도하거나 크리덴셜 없이 코드만 다운로드해 주세요."
                        ),
                        "code": "DEV_IDENTITY_ISSUANCE_UNAVAILABLE",
                    },
                ) from exc
            files = scaffold_with_dev_environment(
                files,
                token_url=f"{base_url}/api/dev-identity/token",
                call_handle_url=f"{base_url}/api/dev-identity/call-handle",
                credential=issued.credential,
                mcp_assets=_issued_mcp_assets(mcps, issued),
            )
            excluded_header = _excluded_operations_header(issued)
        data = scaffold_to_zip(files)
        headers = {"Content-Disposition": f'attachment; filename="{req.name}.zip"'}
        if excluded_header:
            # ZIP 은 바이너리 본문이라 JSON 필드를 붙일 자리가 없어요. 그래서 헤더로 실어요 —
            # 두 번째 왕복을 만들지 않고, ZIP 안에 넣으면 브라우저가 풀 수 없어서 알림을
            # 못 띄워요. 값은 **ASCII 만** (HTTP 헤더는 latin-1) — 한국어 사유는 화면이 만들어요.
            headers[_EXCLUDED_OPERATIONS_HEADER] = excluded_header
        return Response(
            content=data, media_type="application/zip", headers=headers)
    return {"files": scaffold_to_tree(files)}


def _unknown_invocation_usage(reason: str) -> dict:
    return {
        "status": "unknown",
        "source": None,
        "reason": reason,
        "remediation": (
            "사용량 기록을 시작하려면 agent를 최신 scaffold로 재배포하세요."
            if reason == "agent_usage_not_reported"
            else None
        ),
        "metrics": None,
    }


def _restrict_usage_to_declared_tools(usage: dict, descriptors: object) -> dict:
    """Discard self-reported tools when Registry has a complete declaration."""
    metrics = usage.get("metrics") if isinstance(usage, dict) else None
    tools = metrics.get("tool_metrics") if isinstance(metrics, dict) else None
    if not isinstance(tools, dict):
        return usage
    declarations = declared_agent_tools(descriptors)
    if any(
        not declaration.operations_declared
        or not declaration.expected_tool_names
        for declaration in declarations
    ):
        return usage
    expected_names = {
        name
        for declaration in declarations
        for name in declaration.expected_tool_names
    }
    filtered_tools = {
        name: metric for name, metric in tools.items() if name in expected_names
    }
    discarded = len(tools) - len(filtered_tools)
    if discarded == 0:
        return usage
    reported_discarded = usage.get("tool_metrics_discarded_count")
    previous_discarded = (
        reported_discarded
        if isinstance(reported_discarded, int)
        and not isinstance(reported_discarded, bool)
        and reported_discarded > 0
        else 0
    )
    restricted = {
        **usage,
        "metrics": {**metrics, "tool_metrics": filtered_tools},
        "tool_metrics_status": "unknown",
        "tool_metrics_reason": "tool_names_not_declared",
        "tool_metrics_discarded_count": previous_discarded + discarded,
    }
    return restricted


def _record_usage_best_effort(
    *,
    invocation_id: str,
    principal_id: str,
    agent_id: str,
    session_id: str,
    usage: dict,
) -> dict:
    try:
        record_agent_invoke_usage(
            invocation_id=invocation_id,
            principal_id=principal_id,
            agent_id=agent_id,
            session_id=session_id,
            usage=usage,
        )
    except Exception:
        logging.getLogger(__name__).exception(
            "Invocation usage recording failed",
            extra={"invocation_id": invocation_id, "agent_id": agent_id},
        )
        return {"status": "unknown", "reason": "usage_recording_failed"}
    return {"status": "recorded", "reason": None}


@router.post("/api/playground/invoke")
def playground_invoke(req: InvokeRequest, request: Request):
    rec = find_catalog_record(req.record_id)
    if rec is None:
        raise HTTPException(404, "Asset을 찾을 수 없어요.")
    if rec.descriptor_type.value != "Agent":
        raise HTTPException(422, "Agent 자산만 호출할 수 있어요.")
    if rec.status.value != "APPROVED":
        raise HTTPException(409, "승인된 Agent만 호출할 수 있어요.")
    agent = (rec.descriptors or {}).get("agent") or {}
    arn = agent.get("runtimeArn") if isinstance(agent, dict) else None
    if not (isinstance(arn, str) and arn):
        raise HTTPException(422, "아직 배포되지 않았어요. Initializr에서 먼저 배포해 주세요.")
    # OAuth agent(ADR-0019)는 Ed25519 workload identity가 없어요 — tool 인가는 OAuth Gateway
    # Cedar가 강제하므로 client-side workload 검증이 불필요해요. 있으면(legacy) 쓰고 없으면 빈
    # 값으로 진행해요. runtimeArn + APPROVED + agent-invoke 게이트로 호출 자격은 충분히 판정돼요.
    workload_id, _ = get_delegated_workload_binding(rec.descriptors)
    principal = get_current_principal(request)
    # confused-deputy 방어(§4.8·§8.4): 비인가 사용자는 delegation 발급·Runtime 호출 전에
    # agent-invoke 게이트에서 차단해요. /api/invocations와 같은 판정을 공유해요(결함 #1).
    enforce_agent_invoke_gate(
        agent_id=req.record_id,
        owner_user=rec.owner_user,
        caller_principal_id=principal.principal_id,
        caller_groups=principal.roles,
    )
    try:
        user_token = _verified_user_token(
            request,
            principal_source=principal.source,
            token_expires_at=principal.token_expires_at,
        )
    except InvokeError as e:
        raise HTTPException(
            e.status,
            e.detail if e.detail is not None else str(e),
        ) from e
    handle, delegation = get_delegation_service().issue(
        principal_id=principal.principal_id,
        agent_id=req.record_id,
        workload_id=workload_id,
        allowed_asset_ids=get_delegated_asset_ids(rec.descriptors),
        # 그룹 단위 grant 판정용. interceptor 는 외부 조회를 못 해서 이 행에 담아야 해요.
        principal_groups=principal.roles,
        # MCP 에 넘길 `agora_user_id` 값 (ADR-0095).
        principal_email=principal.email,
    )
    invoke_error = None
    try:
        from ...shared.actor import derive_scoped_actor_id

        result = get_agentcore_invoker().invoke(
            runtime_arn=arn,
            prompt=req.prompt,
            session_id=req.session_id,
            actor_id=derive_scoped_actor_id(
                req.record_id,
                principal.principal_id,
            ),
            # IA-61: handle 은 `agoraContext` 로 보내요 (`call_handle`), `metadata` 가
            # 아니에요. handle 은 bearer 성격의 자격증명이고 `message.metadata` 는 대화
            # 메시지의 일부라 세션 메모리·대화 이력에 남을 수 있어요. `invocation_id`·
            # `agent_id` 는 비밀이 아니라 그대로 둬요(감사 상관용).
            call_handle=handle,
            metadata={
                "agora": {
                    "invocation_id": delegation.invocation_id,
                    "agent_id": req.record_id,
                }
            },
            user_token=user_token,
        )
    except InvokeError as e:
        invoke_error = e
    finally:
        get_delegation_service().revoke(handle)
    if invoke_error is not None:
        failed_usage = (
            invoke_error.usage
            if invoke_error.usage.get("status") == "observed"
            else _unknown_invocation_usage("invocation_failed")
        )
        failed_usage = _restrict_usage_to_declared_tools(
            failed_usage, rec.descriptors
        )
        record_agent_invoke_audit(
            invocation_id=delegation.invocation_id,
            principal_id=principal.principal_id,
            agent_id=req.record_id,
            invocation_outcome=INVOCATION_OUTCOME_FAILURE,
        )
        _record_usage_best_effort(
            invocation_id=delegation.invocation_id,
            principal_id=principal.principal_id,
            agent_id=req.record_id,
            session_id=req.session_id,
            usage=failed_usage,
        )
        raise HTTPException(
            invoke_error.status,
            invoke_error.detail if invoke_error.detail is not None else str(invoke_error),
        )
    # 성공한 OAuth invoke를 감사 로그에 남겨요(결함 #5). 이 경로는 /internal/
    # authorization/decide를 거치지 않아 이 기록이 없으면 admin 감사 조회가 비어요.
    record_agent_invoke_audit(
        invocation_id=delegation.invocation_id,
        principal_id=principal.principal_id,
        agent_id=req.record_id,
    )
    usage = getattr(result, "usage", None)
    if not isinstance(usage, dict):
        usage = _unknown_invocation_usage("agent_usage_not_reported")
    usage = _restrict_usage_to_declared_tools(usage, rec.descriptors)
    usage_recording = _record_usage_best_effort(
        invocation_id=delegation.invocation_id,
        principal_id=principal.principal_id,
        agent_id=req.record_id,
        session_id=req.session_id,
        usage=usage,
    )
    return {
        "result": str(result),
        "invocation_id": delegation.invocation_id,
        "delegation_expires_at": delegation.expires_at,
        "trace_id": getattr(result, "trace_id", None),
        "span_id": getattr(result, "span_id", None),
        "requested_sampling": getattr(result, "requested_sampling", None),
        "usage": usage,
        "usage_recording": usage_recording,
    }


# JSON-RPC "method not found". selfcheck 를 모르는 구 배포본을 미지원으로 구분해요.
_METHOD_NOT_FOUND = -32601


@router.get("/api/playground/agents/{record_id}/run-config")
def playground_agent_run_config(
    record_id: str, request: Request, probe: bool = False
):
    """이 agent 가 무슨 모델·도구·기억으로 도는지 — 선언과 실체를 분리해 돌려줘요.

    Playground 는 "지금 무엇을 쓰는지"를 보여줘야 하는데 출처가 둘이에요. 선언은 배포
    원장(Registry descriptor)에서 즉시 읽고, 실체는 배포된 runtime 의 `agora/selfcheck`
    를 호출해야 알 수 있어요. selfcheck 는 세션 microVM 을 하나 쓰니 `probe=true` 일 때만
    호출하고, 안 했으면 `not_probed` 로 **관측 안 함을 명시**해요(통과로 접지 않아요).

    probe 는 대화 세션과 **분리된 session id** 로 호출해요 — 같은 세션을 쓰면 probe 가
    대화용 Agent·Memory 세션에 끼어들어요(IH-86 과 같은 격리 이유).
    """
    from ...shared.actor import derive_scoped_actor_id
    from . import run_config as rc

    rec = find_catalog_record(record_id)
    if rec is None:
        raise HTTPException(404, "Asset을 찾을 수 없어요.")
    if rec.descriptor_type.value != "Agent":
        raise HTTPException(422, "Agent 자산만 조회할 수 있어요.")
    # 실체 관측은 실제 invoke 라서 `/invoke` 와 **같은 승인 검사**가 필요해요. 이게 없으면
    # REJECTED agent 에 stale `runtimeArn` 이 남아 있을 때 `/invoke` 는 409 인데
    # `?probe=true` 는 selfcheck 를 실제로 호출해요(codex 리뷰 재현, 2026-08-22).
    if rec.status.value != "APPROVED":
        raise HTTPException(409, "승인된 Agent만 조회할 수 있어요.")
    principal = get_current_principal(request)
    # 실체 관측은 실제 invoke 예요 — 조회 자격도 invoke 게이트와 같아야 해요.
    enforce_agent_invoke_gate(
        agent_id=record_id,
        owner_user=rec.owner_user,
        caller_principal_id=principal.principal_id,
        caller_groups=principal.roles,
    )
    declared = rc.declared_run_config(rec.descriptors or {})
    agent = (rec.descriptors or {}).get("agent") or {}
    arn = agent.get("runtimeArn") if isinstance(agent, dict) else None
    runtime_arn = arn if isinstance(arn, str) else ""
    memory_descriptor = (
        agent.get("memory") if isinstance(agent, dict) else None
    )
    memory_mode = (
        str(memory_descriptor.get("mode") or "")
        if isinstance(memory_descriptor, dict)
        else ""
    )
    memory_id = (
        str(memory_descriptor.get("memoryId") or "").strip()
        if isinstance(memory_descriptor, dict)
        else ""
    )

    if not probe:
        observed = rc.not_probed()
    elif not runtime_arn:
        # 아직 Runtime 이 없는 agent 는 selfcheck 경로가 없어요 — 관측 대상은 배포된
        # AgentCore Runtime 뿐이에요.
        observed = rc.unsupported(
            "배포된 Runtime 이 없어 selfcheck 로 실체를 관측할 수 없어요."
        )
    else:
        observed = _probe_run_config(
            runtime_arn,
            actor_id=derive_scoped_actor_id(record_id, principal.principal_id),
            # 관측 호출도 handle 을 실어요 — 이 호출이 콜드 스타트를 유발하면 그때 MCP
            # 연결이 일어나고, handle 이 없으면 agent 가 도구 0개로 고착돼요.
            call_handle=_observation_call_handle(record_id, principal, agent),
        )
        if memory_mode == "MANAGED":
            observed["memory_resource"] = (
                rc.observe_agentcore_memory(
                    get_agentcore_memory_control_client(),
                    memory_id,
                )
                if memory_id
                else rc.memory_resource_unknown(
                    "원장에 AgentCore Memory ID가 없어 실체를 조회할 수 없어요."
                )
            )
        elif memory_mode == "DISABLED":
            observed["memory_resource"] = rc.memory_resource_not_applicable()
    return {
        "record_id": record_id,
        "declared": declared,
        "observed": observed,
        "model_reconciliation": rc.reconcile_model(
            declared["model"], observed
        ),
        "memory_reconciliation": rc.reconcile_memory(
            declared["memory"], observed
        ),
        "reconciliation": rc.reconcile_tools(declared["tools"], observed),
        # ADR-0104(IH-153): 「런타임 등록」과 「④ 승인」은 다른 축이에요. 등록돼 있는데
        # 호출은 거부되는 상태가 정상이라(«listed but denied», ADR-0099 §4.6) 화면이 그
        # 이유를 말해야 해요. 원장은 **서버가** 읽어요 — 웹이 `/api/assets/.../tool-bindings`
        # 를 붙이면 owner 아닌 정당한 호출자가 403 을 받아요(그 경로는 owner·admin 전용).
        "tool_authorization": rc.tool_authorization(
            declared["tools"], _tool_approval_states(record_id)
        ),
        "unexpected_tools": rc.unexpected_tools(declared["tools"], observed),
        "logging_outlet": rc.logging_outlet(),
    }


def _tool_approval_states(record_id: str) -> dict[str, str] | None:
    """④ 원장의 `gateway_action → approval_state`. 실패는 `None`(미관측)이에요.

    조회 실패로 화면 전체를 죽이지 않아요 — 실체 관측 실패와 같은 취급이에요. 대신 `None` 을
    빈 dict 으로 접지 않아요: 빈 dict 은 「전부 미신청이라고 관측함」이고 `None` 은 「관측 못
    함」이에요. 후자를 전자로 접으면 승인된 도구가 「신청 안 됨」으로 보여요.
    """
    try:
        return read_tool_approval_states(record_id)
    except Exception as error:
        logging.getLogger(__name__).warning(
            "④ tool binding 승인 상태를 읽지 못했어요: %s: %s",
            type(error).__name__, error,
        )
        return None


def _observation_call_handle(record_id: str, principal, agent) -> str | None:
    """실체 관측용 delegation handle. 실패하면 `None` (관측만 못 해요, 대화는 무관).

    대화 경로와 **같은 방식**으로 발급해요 — 사람 축은 관측을 요청한 사용자이고, 허용 자산은
    그 agent 가 선언한 MCP 로 한정해요. 관측이 특별한 권한을 갖지 않아야 해요.

    왜 필요한가: 이 관측 호출이 콜드 스타트를 유발하면 그 자리에서 MCP `initialize` 가
    일어나요. handle 이 없으면 Gateway 가 막고 agent 가 **도구 0개로 고착**돼요.
    """
    try:
        descriptors = agent.get("descriptors") if isinstance(agent, dict) else None
        handle, _delegation = get_delegation_service().issue(
            principal_id=principal.principal_id,
            agent_id=record_id,
            workload_id="",
            allowed_asset_ids=get_delegated_asset_ids(descriptors or {}),
            principal_groups=principal.roles,
            principal_email=principal.email,
        )
        return handle
    except Exception as error:
        # 관측 실패는 대화를 막지 않아요 — `unavailable` 로 화면에 남아요.
        logging.getLogger(__name__).warning(
            "실체 관측용 handle 을 발급하지 못했어요: %s: %s",
            type(error).__name__, error,
        )
        return None


def _probe_run_config(
    runtime_arn: str, *, actor_id: str, call_handle: str | None = None
) -> dict:
    """배포된 runtime 에 `agora/selfcheck` 를 한 번 호출해 실체를 관측해요.

    **`call_handle` 을 반드시 실어요.** 이 호출이 콜드 스타트를 유발하면 그 자리에서 agent 가
    만들어지고 MCP `initialize` 가 일어나요. handle 이 없으면 Gateway 가 막고 **agent 가 도구
    0개로 고착**돼요 — 그 프로세스가 사는 동안 이어지는 대화도 도구를 못 써요.

    2026-08-30 실측: agent 선택만 했는데 `denied method=initialize reason=invalid_delegation`
    이 찍히고, 뒤이은 대화가 "정보를 불러오지 못했습니다" 로 끝났어요.
    """
    import uuid

    from . import run_config as rc

    try:
        data = get_agentcore_invoker().call(
            runtime_arn=runtime_arn,
            method="agora/selfcheck",
            params={},
            # runtimeSessionId 는 33자 이상이어야 하고, 대화 세션과 겹치면 안 돼요.
            session_id=f"agoraselfcheck{uuid.uuid4().hex}",
            actor_id=actor_id,
            call_handle=call_handle,
            # 대화 turn 기본값(300초)보다 **일부러 짧아요.** 이건 생존 확인이라 5분을
            # 기다리면 화면이 멈춘 것처럼 보여요. 대화는 이력 재생·도구 호출 때문에 길어질
            # 수 있지만 selfcheck 는 그럴 이유가 없어요.
            timeout_sec=60,
        )
    except InvokeError as error:
        return rc.unavailable(f"selfcheck 호출에 실패했어요: {error}")
    except Exception as error:
        # InvokeError 만 잡으면 timeout·DNS·SDK 예외가 500 으로 새어나가요(codex 리뷰 재현).
        # 이 엔드포인트의 계약은 "관측 실패는 unavailable" 이라 여기서 닫아요. 원인은 서버
        # 로그에만 남겨요 — 클라이언트 detail 로 업스트림 내부를 흘리지 않게요.
        logging.getLogger(__name__).warning(
            "selfcheck probe failed (%s): %s", type(error).__name__, error
        )
        return rc.unavailable(
            f"selfcheck 호출에 실패했어요: {type(error).__name__}"
        )
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        if error.get("code") == _METHOD_NOT_FOUND:
            return rc.unsupported("agora/selfcheck 를 지원하지 않는 배포본이에요.")
        return rc.unavailable(
            str(error.get("message") or error)[:200]
        )
    return rc.observed_run_config(
        data.get("result") if isinstance(data, dict) else None
    )


@router.get("/api/playground/agents/{record_id}/logs")
def playground_agent_logs(record_id: str, request: Request,
                          limit: int = 200, since_ms: int | None = None,
                          include_health: bool = False):
    """배포된 agent의 CloudWatch 런타임 로그 (Playground 로그 패널).

    Playground엔 로그를 볼 경로가 없어서 도구가 안 붙어도 사용자가 원인을 알 수
    없었어요(실사용 2026-07-27). `Tool #N` 호출·토큰 경고·에러가 여기 보여요.

    프론트는 runtime ARN을 모르니 record_id만 받고 서버가 도출해요(invoke와 동일 계약).

    로그는 내부 정보가 섞일 수 있어 **소유자와 어드민만** 조회해요(IH-187).

    어드민을 넣은 근거: Playground 는 어드민이 남의 agent 를 «호출» 하는 걸 이미 허용해요
    (`authorize_agent_invoke` — 소유자 · principal allowlist · group). 그런데 로그 조회만
    소유자 전용이라, 어드민이 남의 agent 를 Playground 에서 테스트하면 대화는 되는데 활동
    트리의 model·도구·memory 축이 전부 403 으로 막혔어요. 프론트는 403 을 영구 실패로 보고
    폴링을 멈추므로(`useRuntimeLogs.ts` 의 `permanent`), 트리가 통째로 비고 요약 타일이
    `–` 로 남아요 — 「관측 못 함」이 화면에서는 「활동이 없음」처럼 읽혀요.

    ⚠️ **어드민보다 넓히지 마세요.** 로그 스트림에는 다른 사용자의 동시 세션 줄이 섞일 수
    있어요. 「그 agent 를 부를 수 있는 사람」까지 열면 남의 대화 내용이 보여요.
    """
    from ...shared.deps import get_runtime_log_reader
    from .runtime_logs import runtime_id_from_arn

    rec = find_catalog_record(record_id)
    if rec is None:
        raise HTTPException(404, "Asset not found")
    principal = get_current_principal(request)
    if rec.owner_user != principal.principal_id and not principal.is_admin:
        raise HTTPException(403, "런타임 로그는 자산 소유자와 어드민만 볼 수 있어요.")
    arn = ((rec.descriptors or {}).get("agent") or {}).get("runtimeArn")
    runtime_id = runtime_id_from_arn(arn if isinstance(arn, str) else "")
    if not runtime_id:
        return {"log_status": "not_deployed", "lines": [], "next_since_ms": since_ms or 0}
    return get_runtime_log_reader().fetch(
        runtime_id, limit=max(1, min(limit, 500)), since_ms=since_ms,
        include_health=include_health)
