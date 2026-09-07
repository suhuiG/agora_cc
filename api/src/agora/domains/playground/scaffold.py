"""Agent Initializr 스캐폴드 생성 — 순수함수(부수효과 없음).

build_scaffold(spec)의 출력 하나를 EXPLORE(트리)·GENERATE(zip)·DEPLOY(업로드)가
공유해요. publish/generators/code.py의 dict[str, bytes] 스타일을 계승해요.
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any

from ...shared.bedrock_models import MODEL_ID_MAP as _MODEL_ID_MAP
from ...shared.bedrock_models import (
    PLATFORM_TEMPERATURE,
    model_metadata,
    model_supports_sampling_params,
)
from ...shared.gateway_denial import DENIAL_MESSAGES_BY_REASON
from ...shared.gateway_tools import (
    gateway_tool_name,
    gateway_tool_names,
    gateway_tool_prefixes,
)
from ...shared.memory import (
    MEMORY_NAMESPACE_TEMPLATES,
    MEMORY_RETRIEVAL_RELEVANCE_SCORE,
    MEMORY_RETRIEVAL_TOP_K,
    MEMORY_STRATEGY_NAMES,
    SUPPORTED_MEMORY_STRATEGIES,
)
from ...shared.actor import (
    AGENTCORE_ACTOR_ID_MAX_LENGTH,
    AGENTCORE_ACTOR_ID_PATTERN,
)
from ...shared.slug import gateway_target_name as _gateway_target_name
from ...shared.agent_blueprint import reject_disabled_builtin_tools

# 기존 import 경로를 유지해 playground 소비자가 shared 구현 위치를 알 필요 없게 해요.
MODEL_ID_MAP = _MODEL_ID_MAP

TIMEOUT_UNSUPPORTED_MESSAGE = (
    "AgentCore Runtime이 세션 상한을 관리하고, "
    "컨테이너 레벨 timeout은 아직 미지원입니다"
)
MAX_ITERATIONS_DESCRIPTION = (
    "Counts completed tool batches. max_iterations=1 stops after the first "
    "tool batch, before the model interprets its results."
)


@dataclass
class ToolRef:
    name: str
    kind: str  # "skill" | "mcp" | "agent"
    asset_id: str | None = None
    description: str = ""
    endpoint: str | None = None
    # Gateway가 라이브 tool 접두어(`{target}___{op}`)에 쓰는 정규화된 target name.
    # MCP authorization·selfcheck 비교 기준이에요(결함 #10). 없으면 name으로 폴백해요.
    target_name: str | None = None
    # Catalog binding이 Registry split 원장에서 확정한 operation별 Target 이름.
    operation_targets: dict[str, str] = field(default_factory=dict)
    # 새 Playground MCP 요청은 라우터가 비어 있지 않은 목록으로 검증해요.
    # 직접 호출·이미 생성된 legacy binding만 빈 목록으로 남을 수 있어요.
    operations: list[str] = field(default_factory=list)
    # Registry가 명시적으로 승인한 operation sensitivity만 담아요. 이름 추론값은
    # 실제 호출의 안전 근거가 될 수 없으므로 probe 허용 집합에 사용하지 않아요.
    operation_sensitivities: dict[str, str] = field(default_factory=dict)
    # 같은 승인 MCP에 실제 존재하지만 이 agent가 선택하지 않은 명시 READ operation.
    # Cedar/Gateway negative control 전용이며 일반 agent tool로 등록하지 않아요.
    authorization_probe_operations: list[str] = field(default_factory=list)
    # skill 본문(SKILL.md). 소스 모드 skill은 descriptors에 본문이 없고 S3에만 있어서
    # 라우터가 read_file로 읽어 채워요(build_scaffold는 순수함수 계약 유지).
    # 비어 있으면 그 skill은 배선에서 빠져요 — 배포를 막지는 않아요.
    skill_markdown: str | None = None


@dataclass
class ScaffoldSpec:
    name: str
    model: str            # 프론트 모델 id (MODEL_ID_MAP 키)
    description: str
    system_prompt: str
    tools: list[ToolRef] = field(default_factory=list)
    memory: dict[str, Any] = field(default_factory=dict)
    truncation: dict[str, Any] = field(default_factory=dict)
    max_tokens: int | None = None
    max_iterations: int | None = None
    timeout_seconds: int | None = None
    builtin_tools: list[str] = field(default_factory=list)


# 도구 왕복 하나가 메시지 4개(user + assistant(toolUse) + user(toolResult) + assistant)를
# 쓰고, 복원 이력에서 tool 블록을 떼면 toolResult 만 담긴 메시지가 사라져요. 그래서 작은
# window 는 역할 교대가 깨진 이력을 모델에 보내 대화를 영구히 못 쓰게 만들어요(IH-70).
#
# ⚠️ 이 정리를 하는 주체는 SDK 가 아니라 생성 코드의 `_repair_restored_conversation` 이에요.
# 예전 주석은 SDK `_filter_restored_tool_context` 가 해준다고 적었지만 우리 구성에서는
# 거짓이에요 — 그 필드는 기본값 `False` 이고 `AgentCoreMemoryConfig(...)` 에서 켜지 않아요
# (IH-99, 2026-08-22 정정).
#
# router 의 하한과 같은 값이어야 해요 — 한쪽만 고치면 API 로 직접 들어온 값이 검증을
# 통과해 생성 코드에서 터져요.
SLIDING_WINDOW_MIN = 10


def normalize_truncation(value: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize supported UI/Blueprint context-management spellings."""
    raw = dict(value or {})
    if not raw:
        return {}
    normalized_strategy = normalize_truncation_strategy(raw.get("strategy"))
    allowed_fields = {
        "sliding_window": {"strategy", "window_size", "num_messages"},
        "none": {"strategy"},
    }
    unexpected = sorted(set(raw) - allowed_fields[normalized_strategy])
    if unexpected:
        raise ValueError(
            "truncation fields are not valid for "
            f"{normalized_strategy}: {', '.join(unexpected)}"
        )

    if normalized_strategy == "sliding_window":
        if "window_size" in raw and "num_messages" in raw:
            raise ValueError(
                "truncation window_size and num_messages cannot both be set"
            )
        window_size = raw.get("window_size", raw.get("num_messages", 40))
        if isinstance(window_size, bool) or not isinstance(window_size, int):
            raise ValueError("truncation window_size must be an integer")
        if window_size < SLIDING_WINDOW_MIN:
            # IH-70: 도구 왕복 하나가 메시지 4개를 쓰고, Memory 복원이 toolResult 만 담긴
            # 메시지를 버려서 역할 교대가 깨져요. 작은 window 는 그 깨진 구간을 그대로
            # 모델에 보내 세션을 오염시켜요(실측 2026-08-22: window 5 에서 5번째 턴 실패).
            raise ValueError(
                "truncation window_size must be at least "
                f"{SLIDING_WINDOW_MIN}"
            )
        return {"strategy": normalized_strategy, "window_size": window_size}

    return {"strategy": "none"}


def normalize_memory(value: dict[str, Any] | None) -> dict[str, Any]:
    raw = dict(value or {"mode": "DISABLED"})
    mode = str(raw.get("mode", "DISABLED")).upper()
    if mode == "DISABLED":
        if set(raw) - {"mode"}:
            raise ValueError("DISABLED memory does not accept managed settings")
        return {"mode": "DISABLED"}
    if mode != "MANAGED":
        raise ValueError("memory mode must be DISABLED or MANAGED")

    unexpected = set(raw) - {
        "mode",
        "strategies",
        "strategy_configs",
        "retention_days",
    }
    if unexpected:
        raise ValueError(
            "unsupported memory fields: " + ", ".join(sorted(unexpected))
        )
    allowed = set(SUPPORTED_MEMORY_STRATEGIES)
    strategies = list(
        dict.fromkeys(str(item).upper() for item in raw.get("strategies", ()))
    )
    if not strategies:
        raise ValueError("MANAGED memory requires at least one strategy")
    invalid = sorted(set(strategies) - allowed)
    if invalid:
        raise ValueError(
            "unsupported memory strategies: " + ", ".join(invalid)
        )
    configs = raw.get("strategy_configs")
    normalized_configs = None
    if configs is not None:
        normalized_configs = _normalize_memory_strategy_configs(
            configs,
            strategies,
        )
    else:
        normalized_configs = _normalize_memory_strategy_configs(
            {
                strategy: {"name": MEMORY_STRATEGY_NAMES[strategy]}
                for strategy in strategies
            },
            strategies,
        )
    retention_days = raw.get("retention_days", 30)
    if (
        isinstance(retention_days, bool)
        or not isinstance(retention_days, int)
        or not 3 <= retention_days <= 365
    ):
        raise ValueError("memory retention_days must be between 3 and 365")
    normalized = {
        "mode": "MANAGED",
        "strategies": strategies,
        "retention_days": retention_days,
    }
    if normalized_configs is not None:
        normalized["strategy_configs"] = normalized_configs
    return normalized


_MEMORY_STRATEGY_NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,47}$")
def _normalize_memory_strategy_configs(
    value: Any,
    strategies: list[str],
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != set(strategies):
        raise ValueError(
            "strategy_configs must match selected memory strategies"
        )
    normalized: dict[str, dict[str, Any]] = {}
    for strategy in strategies:
        config = value.get(strategy)
        if not isinstance(config, dict):
            raise ValueError(f"{strategy} strategy config must be an object")
        if set(config) != {"name"}:
            raise ValueError(f"{strategy} strategy config fields are invalid")
        name = config.get("name")
        if not isinstance(name, str) or not _MEMORY_STRATEGY_NAME.fullmatch(name):
            raise ValueError(f"{strategy} strategy name is invalid")
        output: dict[str, Any] = {
            "name": name,
            "namespaces": [MEMORY_NAMESPACE_TEMPLATES[strategy]],
        }
        normalized[strategy] = output
    return normalized


def _memory_retrieval_config(
    memory: dict[str, Any],
) -> dict[str, dict[str, int | float]]:
    """Project ledger-owned namespaces into the runtime SDK retrieval contract."""
    configs = memory.get("strategy_configs") or {}
    retrieval = {}
    for strategy in memory.get("strategies") or ():
        namespaces = (configs.get(strategy) or {}).get("namespaces") or ()
        if len(namespaces) != 1:
            raise ValueError(
                f"{strategy} requires exactly one server-owned namespace"
            )
        namespace = namespaces[0]
        if "{memoryStrategyId}" in namespace:
            raise ValueError(
                "server-owned memory namespace must not contain "
                "{memoryStrategyId}"
            )
        retrieval[namespace] = {
            "top_k": MEMORY_RETRIEVAL_TOP_K,
            "relevance_score": MEMORY_RETRIEVAL_RELEVANCE_SCORE,
        }
    return retrieval


def normalize_truncation_strategy(value: Any) -> str:
    """Return the canonical strategy for accepted UI and Blueprint spellings."""
    strategy = str(value if value is not None else "").strip().lower()
    strategy = strategy.replace("-", "_").replace(" ", "_")
    aliases = {
        "sliding": "sliding_window",
        "sliding_window": "sliding_window",
        "none": "none",
    }
    try:
        return aliases[strategy]
    except KeyError as exc:
        raise ValueError(
            f"unsupported truncation strategy: {value!r}"
        ) from exc


def _conversation_manager_projection(
    truncation: dict[str, Any],
) -> tuple[str, str]:
    strategy = truncation["strategy"]
    if strategy == "sliding_window":
        name = "SlidingWindowConversationManager"
        expression = f"{name}(window_size={truncation['window_size']})"
    else:
        name = "NullConversationManager"
        expression = f"{name}()"
    return name, expression


def _positive_limit(name: str, value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _bedrock_model_id(model: str) -> str:
    return model_metadata(model)["bedrockModelId"]


def _skill_dir_name(tool: ToolRef) -> str:
    """생성물에서 이 skill을 담을 디렉터리명. frontmatter name과 맞춰요.

    Strands는 `from_file`로 읽을 때 디렉터리명과 frontmatter name이 다르면 경고를
    내요(실측 2026-07-28). 우리는 `from_content`로 읽어서 경고 대상은 아니지만,
    사람이 생성물을 열어볼 때 헷갈리지 않게 이름을 일치시켜요.
    """
    from ..catalog.registry.aws_mapping import _skill_name_slug
    md = tool.skill_markdown or ""
    for line in md.splitlines()[:12]:
        if line.strip().startswith("name:"):
            raw = line.split(":", 1)[1].strip().strip("\"'")
            if raw:
                return raw
    return _skill_name_slug(tool.name)


def build_scaffold(spec: ScaffoldSpec) -> dict[str, bytes]:
    reject_disabled_builtin_tools(spec.builtin_tools)
    # IH-101 temporary disablement (2026-08-22): retain the generation path for
    # reactivation only after strands-agents-tools extras allow
    # bedrock-agentcore>=1.22.0.
    # builtin_tools = list(normalize_builtin_tools(spec.builtin_tools))
    model_id = _bedrock_model_id(spec.model)
    max_tokens = _positive_limit("max_tokens", spec.max_tokens)
    max_iterations = _positive_limit("max_iterations", spec.max_iterations)
    timeout_seconds = _positive_limit("timeout_seconds", spec.timeout_seconds)
    if timeout_seconds is not None:
        raise ValueError(TIMEOUT_UNSUPPORTED_MESSAGE)
    execution_limits = {
        key: value
        for key, value in (
            ("max_tokens", max_tokens),
            ("max_iterations", max_iterations),
        )
        if value is not None
    }
    if max_iterations is not None:
        execution_limits["max_iterations_description"] = (
            MAX_ITERATIONS_DESCRIPTION
        )
    truncation = normalize_truncation(spec.truncation)
    memory = normalize_memory(spec.memory)
    managed_memory = memory["mode"] == "MANAGED"
    memory_retrieval_config = _memory_retrieval_config(memory)
    conversation_manager = (
        _conversation_manager_projection(truncation)
        if truncation
        else None
    )
    # asset_id와 endpoint가 모두 결속된 MCP만 런타임 도구로 만들어요. 둘 중 하나라도
    # 없으면 authorization 대상과 실제 호출 대상의 동일성을 증명할 수 없으므로 제외해요.
    mcps = [
        t for t in spec.tools
        if t.kind == "mcp" and t.asset_id and t.endpoint
    ]

    def _target_name(m: ToolRef, operation: str = "") -> str:
        # Gateway가 라이브 tool 접두어에 쓰는 정규화된 이름을 authorization 비교 기준으로
        # 써요(결함 #10). 저장돼 있으면(신규·재등록 자산) 그 값을 쓰고, 없으면(기존 자산)
        # slug(name)를 Gateway와 같은 규칙으로 정규화해 폴백해요 — `.`·`_` 기존 자산도
        # backfill 없이 즉시 정합해요. ASCII-하이픈 자산은 정규화해도 name과 같아 무변.
        # (비-ASCII 기존 자산은 slug가 이미 hash8로 손실돼 이 폴백으론 복원 불가 — 저장된
        # target_name이 있어야 정합. shared.slug.gateway_target_name docstring 참고.)
        return (
            m.operation_targets.get(operation)
            or m.target_name
            or _gateway_target_name(m.name)
        )

    def _target_bindings(
        m: ToolRef,
    ) -> tuple[tuple[str, tuple[str, ...]], ...]:
        grouped: dict[str, list[str]] = {}
        for operation in m.operations:
            grouped.setdefault(
                _target_name(m, operation),
                [],
            ).append(operation)
        if not grouped:
            grouped[_target_name(m)] = []
        return tuple(
            (target_name, tuple(operations))
            for target_name, operations in grouped.items()
        )

    # 본문이 없는 skill은 배선에서 빠져요(라우터가 S3에서 못 읽은 경우).
    # 배포를 막지 않고 그 skill만 제외해요.
    skills = [t for t in spec.tools if t.kind == "skill" and (t.skill_markdown or "").strip()]
    skill_dirs = [_skill_dir_name(t) for t in skills]

    # 루트 main.py — AgentCore A2A codezip 진입점(entryPoint=["main.py"]).
    # sample/chatbot-agent 방식: Starlette 포트 9000, POST / (JSON-RPC), 카드, /ping.
    main_py = f'''"""{spec.name} — AgentCore A2A 진입점 (codezip entryPoint=["main.py"]).
포트 9000에서 A2A JSON-RPC 2.0 서버를 서빙해요. 로직은 agent/ 패키지에 있어요.
"""
from __future__ import annotations

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agent.handler import handle_jsonrpc, load_agent_card
from agent.tools import set_call_handle, set_user_token

_AGENT_CARD_CACHE: dict | None = None


def _get_agent_card() -> dict:
    global _AGENT_CARD_CACHE
    if _AGENT_CARD_CACHE is None:
        _AGENT_CARD_CACHE = load_agent_card()
    return _AGENT_CARD_CACHE


async def _a2a(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {{"jsonrpc": "2.0", "id": None, "error": {{"code": -32700, "message": "파싱 오류"}}}},
            status_code=400,
        )
    trace_headers = dict()
    for name in ("traceparent", "X-Amzn-Trace-Id"):
        value = request.headers.get(name)
        if value:
            trace_headers[name] = value
    session_id = request.headers.get(
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id", ""
    )
    actor_id = request.headers.get("X-Agora-Actor-Id", "")
    # IA-61: 이번 호출의 delegation handle 을 요청 스코프에 심어요. `agent/tools.py` 의
    # `_BearerAuth` 가 이걸 읽어 Gateway 호출에 `X-Agora-Call` 헤더로 붙이고, Gateway REQUEST
    # interceptor 가 그 값으로 원장을 조회해요(IA-54). 없으면 도구 호출이 거부돼요.
    #
    # `agoraContext` 로 받아요, A2A `message.metadata` 가 아니에요 — handle 은 bearer 성격의
    # 자격증명이고 `message.metadata` 는 대화 메시지의 일부라 세션 메모리·대화 이력에 남을 수
    # 있어요. `agoraContext` 는 `message` 의 형제인 제어 채널이에요.
    #
    # **여기서 심는 이유**: 요청 경계라서요. `agent/handler.py` 는 소켓 없이 단독 테스트되므로
    # 거기서 `agent.tools` 를 import 하면 테스트가 깨져요.
    #
    # 값이 없어도 **항상** 덮어써요 — 같은 task 에서 이전 호출의 handle 이 남으면 handle 없는
    # 호출이 통과해요(인가 우회).
    _params = body.get("params") if isinstance(body, dict) else None
    _agora_context = (
        _params.get("agoraContext") if isinstance(_params, dict) else None
    )
    set_call_handle(
        _agora_context.get("callHandle")
        if isinstance(_agora_context, dict)
        else None
    )
    set_user_token(
        (request.headers.get("X-Agora-User-Token") or "").strip(),
        forwarded=(
            isinstance(_agora_context, dict)
            and _agora_context.get("userTokenForwarded") is True
        ),
    )
    return JSONResponse(handle_jsonrpc(
        body,
        trace_headers=trace_headers,
        session_id=session_id,
        actor_id=actor_id,
    ))


async def _agent_card(_request: Request) -> Response:
    return JSONResponse(_get_agent_card())


async def _ping(_request: Request) -> Response:
    return JSONResponse({{"status": "Healthy"}})


app = Starlette(
    routes=[
        Route("/", _a2a, methods=["POST"]),
        Route("/.well-known/agent-card.json", _agent_card, methods=["GET"]),
        Route("/ping", _ping, methods=["GET"]),
    ]
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9000)
'''

    conversation_manager_import = (
        "from strands.agent.conversation_manager import "
        f"{conversation_manager[0]}\n"
        if conversation_manager
        else ""
    )
    conversation_manager_argument = (
        f"    conversation_manager={conversation_manager[1]},\n"
        if conversation_manager
        else ""
    )
    model_arguments = [f'model_id="{model_id}"']
    if max_tokens is not None:
        model_arguments.append(f"max_tokens={max_tokens}")
    if model_supports_sampling_params(spec.model):
        model_arguments.append(f"temperature={PLATFORM_TEMPERATURE}")
    model_arguments_source = ",\n    ".join(model_arguments)
    limit_hook_import = (
        "from agent.limits import IterationLimitHook\n"
        if max_iterations is not None
        else ""
    )
    limit_hook_argument = (
        f"    hooks=[IterationLimitHook(max_iterations={max_iterations})],\n"
        if max_iterations is not None
        else ""
    )
    memory_imports = (
        "import atexit\n"
        "from collections import OrderedDict\n"
        "\n"
        "from bedrock_agentcore.memory.integrations.strands.config import "
        "AgentCoreMemoryConfig\n"
        "from bedrock_agentcore.memory.integrations.strands.session_manager "
        "import AgentCoreMemorySessionManager\n"
        if managed_memory
        else ""
    )
    builtin_imports = ""
    builtin_requirements = b""
    builtin_helpers = ""
    # IH-101 temporary disablement. Re-enable this retained block only when
    # strands-agents-tools extras permit bedrock-agentcore>=1.22.0.
    # builtin_setup = ""
    # builtin_values: list[str] = []
    # if "browser" in builtin_tools:
    #     builtin_imports += (
    #         "from strands_tools.browser import AgentCoreBrowser\n"
    #     )
    #     builtin_setup += (
    #         "browser_tool = AgentCoreBrowser(\n"
    #         "    region=os.environ.get(\"AWS_REGION\") or "
    #         "os.environ.get(\"AWS_DEFAULT_REGION\"),\n"
    #         "    identifier=_required_builtin_id(\"AGORA_BROWSER_ID\"),\n"
    #         ")\n"
    #     )
    #     builtin_values.append("browser_tool.browser")
    #     builtin_requirements += (
    #         b"strands-agents-tools[agent-core-browser]>=0.8.6\n"
    #     )
    # if "code_interpreter" in builtin_tools:
    #     builtin_imports += (
    #         "from strands_tools.code_interpreter import "
    #         "AgentCoreCodeInterpreter\n"
    #     )
    #     builtin_setup += (
    #         "code_interpreter_tool = AgentCoreCodeInterpreter(\n"
    #         "    region=os.environ.get(\"AWS_REGION\") or "
    #         "os.environ.get(\"AWS_DEFAULT_REGION\"),\n"
    #         "    identifier=_required_builtin_id("
    #         "\"AGORA_CODE_INTERPRETER_ID\"),\n"
    #         ")\n"
    #     )
    #     builtin_values.append("code_interpreter_tool.code_interpreter")
    #     builtin_requirements += (
    #         b"strands-agents-tools[agent-core-code-interpreter]>=0.8.6\n"
    #     )
    # builtin_helpers = (
    #     "\n\ndef _required_builtin_id(name: str) -> str:\n"
    #     "    value = os.environ.get(name, \"\").strip()\n"
    #     "    if not value:\n"
    #     "        raise RuntimeError(f\"{name} is required for a CUSTOM "
    #     "AgentCore built-in tool\")\n"
    #     "    return value\n\n\n"
    #     f"{builtin_setup}"
    #     if builtin_tools
    #     else ""
    # )
    # builtin_tools_expression = ", ".join(builtin_values)
    # builtin_tools_list = (
    #     f", {builtin_tools_expression}" if builtin_tools_expression else ""
    # )
    memory_setup = (
        "    import logging\n"
        "\n"
        "    log = logging.getLogger(__name__)\n"
        "    # Compatibility boundary: Agora creates only new Memory resources and\n"
        "    # does not configure Bedrock guardrails. Strands 1.51 ordinary turns\n"
        "    # therefore avoid legacy migration/read_message and guardrail redaction,\n"
        "    # the SDK paths that require denied GetEvent/DeleteEvent actions.\n"
        '    memory_id = os.environ.get("AGORA_MEMORY_ID", "").strip()\n'
        "    memory_status = {\"status\": \"ok\", \"reason\": \"\"}\n"
        "    session_manager = None\n"
        "    actor_valid = bool(\n"
        "        actor_id\n"
        "        and len(actor_id) <= _AGENTCORE_ACTOR_ID_MAX_LENGTH\n"
        "        and _AGENTCORE_ACTOR_ID.fullmatch(actor_id)\n"
        "    )\n"
        "    # ── 로컬 실행은 memory 없이 계속 돌아요 ─────────────────────────────\n"
        "    #\n"
        "    # memory 에 필요한 두 값은 **배포 경로만** 공급해요 — `AGORA_MEMORY_ID` 는\n"
        "    # 배포 API 가 주입하고(`runtime/deploy/aws_adapter.py`), `session_id` 는\n"
        "    # AgentCore 가 `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` 헤더로만 줘요.\n"
        "    # 로컬 A2A 클라이언트에는 둘 다 없어서, 예전에는 첫 요청이 RuntimeError 로\n"
        "    # 죽었어요. 대화 자체를 못 하니 도구도 프롬프트도 시험할 수 없었어요.\n"
        "    #\n"
        "    # 그래서 **로컬에서는 degrade** 해요 — 이미 있는 degrade 경로와 같은 모양이에요\n"
        "    # (`session_manager=None` 이면 Agent 는 기억 없이 정상 동작해요).\n"
        "    # 배포 런타임에서는 **그대로 raise** 해요: 거기서 값이 비는 건 배포 결함이고,\n"
        "    # 조용히 기억을 잃으면 사용자는 대화가 이어지지 않는 이유를 알 수 없어요.\n"
        "    #\n"
        "    # 배포 판별은 `agent/tools.py`·`agent/__init__.py` 의 `_deployed_runtime()` 과\n"
        "    # **같은 판정**이에요. import 하지 않고 여기서 다시 쓰는 이유는 `agent.tools` 의\n"
        "    # private 이름을 모듈 경계 밖으로 끌어오지 않으려는 거예요(그 모듈을 대역으로\n"
        "    # 바꿔치기하는 테스트도 깨져요). 세 곳이 어긋나면 테스트가 잡아요.\n"
        "    deployed = os.environ.get(\"AGORA_RUNTIME_ENV\") == \"deployed\" or bool(\n"
        '        os.environ.get("AGORA_OAUTH_PROVIDER_NAME")\n'
        '        and os.environ.get("AGORA_WORKLOAD_IDENTITY_NAME")\n'
        "    )\n"
        "    if not memory_id or not session_id:\n"
        "        missing = \"AGORA_MEMORY_ID\" if not memory_id else \"session_id\"\n"
        "        if deployed:\n"
        "            raise RuntimeError(f\"MANAGED memory requires {missing}\")\n"
        "        memory_status = {\n"
        '            "status": "degraded",\n'
        '            "reason": (\n'
        '                "memory skipped: 로컬 실행에는 memory 를 쓰지 않아요"\n'
        '                f" (빠진 값: {missing}). 기억은 배포한 agent 에서만 동작해요 — "\n'
        '                "쓰기 MCP 도구와 같아요."\n'
        "            ),\n"
        "        }\n"
        '        log.warning("%s", memory_status["reason"])\n'
        "        agent = _create_agent(session_manager=None)\n"
        "        agent._agora_memory_status = memory_status\n"
        "        return agent\n"
        "    if not actor_valid:\n"
        "        memory_status = {\n"
        '            "status": "degraded",\n'
        '            "reason": "memory skipped: actor_id violates AgentCore contract",\n'
        "        }\n"
        '        log.error("%s", memory_status["reason"])\n'
        "    else:\n"
        "        memory_config = AgentCoreMemoryConfig(\n"
        "            memory_id=memory_id,\n"
        "            session_id=session_id,\n"
        "            actor_id=actor_id,\n"
        f"            retrieval_config={memory_retrieval_config!r},\n"
        "        )\n"
        "        # Memory restore is optional enrichment. A restore failure must not\n"
        "        # block the conversation, but it remains visible to selfcheck.\n"
        "        try:\n"
        "            session_manager = AgentCoreMemorySessionManager(\n"
        "                agentcore_memory_config=memory_config,\n"
        '                region_name=os.environ.get("AWS_REGION") or '
        'os.environ.get("AWS_DEFAULT_REGION"),\n'
        "            )\n"
        "        except Exception as error:\n"
        "            memory_status = {\n"
        '                "status": "degraded",\n'
        '                "reason": (\n'
        '                    f"memory restore failed: {type(error).__name__}: {error}"\n'
        "                ),\n"
        "            }\n"
        '            log.error("%s", memory_status["reason"])\n'
        "    agent = _create_agent(session_manager=session_manager)\n"
        "    agent._agora_memory_status = memory_status\n"
        "    return agent\n"
        if managed_memory
        else "    return _create_agent()\n"
    )
    cached_agent = (
        "_SESSION_AGENT_CACHE_MAX = 32\n"
        "_SESSION_AGENTS = OrderedDict()\n"
        "_SESSION_AGENT_ACTIVE = {}\n"
        "_SESSION_CONVERSATION_OBSERVATIONS = OrderedDict()\n"
        "_SESSION_AGENT_LOCK = RLock()\n"
        if managed_memory
        # **import 시점에 만들지 않아요** (2026-08-30). `Agent(tools=[MCPClient…])` 가
        # 그 자리에서 MCP `initialize` 를 하는데, 그때는 이번 호출의 delegation handle 이
        # 아직 없어요 — Gateway 가 `invalid_delegation` 으로 막고 agent 는 **도구 0개**로
        # 떠요. 첫 요청 안에서 만들면 handle 이 있어서 연결이 성립해요.
        else (
            "_CACHED_AGENT = None\n"
            "_CACHED_AGENT_LOCK = RLock()\n"
            "\n"
            "\n"
            "def _cached_agent():\n"
            '    """첫 호출에서 만들어요 — MCP 연결이 handle 있는 시점에 일어나야 해요."""\n'
            "    global _CACHED_AGENT\n"
            "    with _CACHED_AGENT_LOCK:\n"
            "        if _CACHED_AGENT is None:\n"
            "            _CACHED_AGENT = _new_agent()\n"
            "        return _CACHED_AGENT\n"
        )
    )
    build_agent_body = (
        "    return _get_or_create_session_agent(session_id, actor_id)"
        if managed_memory
        else "    return _cached_agent()"
    )
    session_cache = (
        '''
def _cleanup_agent(agent: Agent) -> None:
    """Release MCP providers owned by an evicted Strands Agent."""
    agent.cleanup()


def _observe_conversation_messages(
    agent: Agent,
    *,
    key: tuple[str, str],
    source: str,
) -> None:
    """메시지 전량 급감만 session/actor별 대화 무결성 상태에 남겨요."""
    import logging

    log = logging.getLogger(__name__)
    messages = getattr(agent, "messages", None)
    if not isinstance(messages, list):
        return
    current_count = len(messages)
    with _SESSION_AGENT_LOCK:
        previous = _SESSION_CONVERSATION_OBSERVATIONS.get(key)
        previous_count = (
            previous["message_count"] if previous is not None else None
        )
        if previous_count is None:
            integrity = {
                "status": "unknown",
                "reason": "conversation message baseline has not been observed",
                "previous_message_count": None,
                "current_message_count": current_count,
            }
        elif previous_count > 0 and current_count == 0:
            integrity = {
                "status": "degraded",
                "reason": (
                    "conversation context may be incomplete: messages dropped "
                    f"{previous_count} -> {current_count}"
                ),
                "previous_message_count": previous_count,
                "current_message_count": current_count,
            }
        elif current_count > 0:
            integrity = {
                "status": "ok",
                "reason": "",
                "previous_message_count": previous_count,
                "current_message_count": current_count,
            }
        else:
            integrity = dict(previous["integrity"])
        _SESSION_CONVERSATION_OBSERVATIONS[key] = {
            "message_count": current_count,
            "integrity": integrity,
        }
        _SESSION_CONVERSATION_OBSERVATIONS.move_to_end(key)
        while (
            len(_SESSION_CONVERSATION_OBSERVATIONS)
            > _SESSION_AGENT_CACHE_MAX * 2
        ):
            _SESSION_CONVERSATION_OBSERVATIONS.popitem(last=False)
        agent._agora_conversation_integrity = dict(integrity)
    log.info(
        "conversation messages observed: messages=%d previous=%s source=%s",
        current_count,
        previous_count if previous_count is not None else "none",
        source,
    )
    # IH-108 실측은 이전 대화가 통째로 0개가 되는 형태예요. Sliding-window 같은
    # 정상 감소를 추측으로 판정하지 않고, 관측된 전량 급감만 degraded로 남겨요.
    if not (
        previous_count is not None
        and previous_count > 0
        and current_count == 0
    ):
        return
    log.error(
        "conversation messages dropped: previous=%d current=%d source=%s",
        previous_count,
        current_count,
        source,
    )


def _trim_session_agents_locked() -> None:
    while len(_SESSION_AGENTS) > _SESSION_AGENT_CACHE_MAX:
        for key, agent in _SESSION_AGENTS.items():
            if _SESSION_AGENT_ACTIVE.get(id(agent), 0) == 0:
                del _SESSION_AGENTS[key]
                _cleanup_agent(agent)
                break
        else:
            # Active invocations are never cleaned up underneath a request. The
            # releasing lease trims the temporary overflow.
            return


def _repair_restored_conversation(agent: Agent) -> None:
    """복원된 대화가 역할 교대와 thinking 계약을 지키게 손봐요 (IH-70 · IH-93 ①).

    두 가지 이유로 복원 이력이 깨질 수 있어요.

    1. 복원된 이력에는 **tool 블록이 그대로 들어 있어요.** 그런데 이번 턴에 그 짝이
       없으면 Bedrock 이 `toolUse`/`toolResult` 쌍이 안 맞는다고 거부해요. 그래서 이
       함수가 tool 블록을 떼어내는데, 그 결과 assistant(toolUse) / user(toolResult) 두
       메시지가 비어 사라지고 **assistant 가 연속**으로 남아요.

       ⚠️ 예전 주석은 「SDK `_filter_restored_tool_context` 가 복원 시 걸러준다」고 적혀
       있었는데 **우리 구성에서는 거짓이에요**(IH-99, 2026-08-22 정정). 그 필드는 기본값
       `False` 이고 `AgentCoreMemoryConfig(...)` 를 만들 때 우리가 켜지 않아요. 그러니
       걸러내는 주체는 SDK 가 아니라 **이 함수**예요 — 이 함수를 지우면 tool 블록이 그대로
       모델로 가요.
    2. 턴이 실패하면 사용자 메시지만 저장되고 응답이 저장되지 않아 **user 가 연속**으로
       쌓여요. 실측(2026-08-22): 한 번 실패한 뒤 그 세션의 모든 턴이 Bedrock 의
       `The conversation must end with a user message` 로 계속 실패했어요.

    Bedrock Converse 는 역할이 번갈아야 해서 위 두 상태를 모두 거부해요. 오염은 Memory 에
    남으니 **복원 직후 한 번** 고쳐요 — 여기서는 아직 이번 턴의 tool 메시지가 없어서
    toolUse/toolResult 쌍을 깨뜨릴 위험이 없어요(모델 호출 중간에 손대면 그 쌍이 깨져요).

    thinking 계약 (IH-93 ①, 2026-09-06 재현):

    기본 모델은 thinking 이 켜져 있어서 복원 이력의 assistant 메시지에
    `reasoningContent`(`reasoningText` 또는 `redactedContent`)가 들어와요. Anthropic 계약은
    **최신 assistant 메시지의 thinking 블록을 모델이 준 그대로** 요구해요 — 재배치·수정·
    부분 삭제는 `thinking or redacted_thinking blocks in the latest assistant message
    cannot be modified` 로 400 이에요(`messages.N.content.M` 을 가리켜요). 단 턴 밖이면
    이전 턴의 thinking 을 **생략하는 것은 허용**돼요.

    그래서 두 규칙을 지켜요.

    1. 메시지가 tool 블록을 잃으면 그 메시지의 `reasoningContent` 도 같이 버려요. 턴이
       해체됐으니 그 thinking 은 온전한 턴에 속하지 않고, 남기는 쪽이 금지된 분기예요.
    2. 연속된 두 메시지를 합칠 때 어느 쪽이든 `reasoningContent` 를 들고 있으면 **합치지
       않아요.** 이어 붙이면 서로 다른 서명의 thinking 시퀀스가 한 메시지의 비-선두
       인덱스에 박혀요(보고된 `messages.3.content.2` 가 정확히 이거예요). 앞쪽 잔재를
       버리고 뒤쪽 = 최신 턴을 온전히 보존해요.

    블록 판별은 **키 `reasoningContent` 존재**로만 해요 — 안쪽이 `reasoningText` 인지
    `redactedContent`(bytes) 인지에 의존하면 redacted 변종에서 조용히 새요.
    """
    import logging

    log = logging.getLogger(__name__)
    messages = getattr(agent, "messages", None)
    message_count = len(messages) if isinstance(messages, list) else 0
    tool_block_messages = sum(
        1
        for message in messages or ()
        if isinstance(message, dict)
        and any(
            isinstance(block, dict)
            and ("toolUse" in block or "toolResult" in block)
            for block in message.get("content") or ()
        )
    )
    if not messages:
        log.info(
            "restored conversation inspected: messages=%d tool_blocks=%d changed=%s",
            message_count,
            tool_block_messages,
            "false",
        )
        return
    repaired: list = []
    # 관측 계약 — 셋 다 여기서 세는 값이고, 하나도 추정값이 아니에요.
    dropped_orphaned = 0    # 해체된 tool-use 턴에서 버린 thinking 블록 수
    dropped_superseded = 0  # 합치기를 거부하면서 앞쪽 잔재로 버린 thinking 블록 수
    merges_skipped = 0      # thinking 때문에 합치지 않은 연속 메시지 쌍 수
    for message in messages:
        role = message.get("role")
        original = list(message.get("content") or [])
        content = [
            block for block in original
            if "toolUse" not in block and "toolResult" not in block
        ]
        if len(content) != len(original):
            # tool 블록을 잃었으니 이 메시지는 더 이상 온전한 tool-use 턴이 아니에요.
            # 턴 안의 thinking 은 필수지만 턴 밖이면 생략이 허용돼요 — 턴이 사라진
            # 지금은 남기는 쪽이 금지된 분기예요.
            kept = [block for block in content if "reasoningContent" not in block]
            dropped_orphaned += len(content) - len(kept)
            content = kept
        if not content:
            # 내용이 tool 뿐인 메시지는 복원 이력에 남을 이유가 없어요.
            continue
        previous = repaired[-1] if repaired else None
        if previous is not None and previous.get("role") == role:
            previous_content = list(previous.get("content") or [])
            previous_thinking = [
                block for block in previous_content
                if "reasoningContent" in block
            ]
            incoming_thinking = [
                block for block in content if "reasoningContent" in block
            ]
            if previous_thinking or incoming_thinking:
                # 이어 붙이면 서로 다른 서명의 thinking 시퀀스가 한 메시지에 섞여
                # `cannot be modified` 로 거부돼요. 앞쪽 잔재를 버리고 뒤쪽 = 최신
                # 턴을 온전히 보존해요(자리와 role 은 그대로라 교대는 유지돼요).
                merges_skipped += 1
                dropped_superseded += len(previous_thinking)
                repaired[-1] = {"role": role, "content": content}
                continue
            # 같은 역할이 연속되면 합쳐요. 내용을 버리지 않으면서 교대를 회복해요.
            repaired[-1]["content"] = previous_content + content
            continue
        repaired.append({"role": role, "content": content})
    # 대화는 user 로 시작해야 해요(선두 assistant 는 prefill 로 취급돼 거부돼요).
    while repaired and repaired[0].get("role") != "user":
        repaired.pop(0)
    changed = repaired != messages
    log.info(
        "restored conversation inspected: messages=%d tool_blocks=%d changed=%s",
        message_count,
        tool_block_messages,
        str(changed).lower(),
    )
    if changed:
        log.info(
            "restored conversation repaired: %d -> %d messages",
            len(messages),
            len(repaired),
        )
        if dropped_orphaned or dropped_superseded or merges_skipped:
            # thinking 계약 때문에 무엇을 버렸는지 남겨요. 이 줄이 없으면 라이브에서
            # IH-93 ① 수정이 실제로 도는지 확인할 방법이 없어요.
            log.info(
                "restored conversation thinking guard: dropped_orphaned=%d"
                " dropped_superseded=%d merges_skipped=%d",
                dropped_orphaned,
                dropped_superseded,
                merges_skipped,
            )
        messages[:] = repaired


def _get_or_create_session_agent(session_id: str, actor_id: str) -> Agent:
    key = (session_id, actor_id)
    with _SESSION_AGENT_LOCK:
        agent = _SESSION_AGENTS.get(key)
        if agent is None:
            agent = _new_agent(session_id=session_id, actor_id=actor_id)
            _repair_restored_conversation(agent)
            _observe_conversation_messages(agent, key=key, source="restore")
            _SESSION_AGENTS[key] = agent
            _trim_session_agents_locked()
        else:
            _SESSION_AGENTS.move_to_end(key)
        return agent


@contextmanager
def use_agent(*, session_id: str, actor_id: str):
    """Lease a session Agent so eviction cannot close active MCP clients."""
    require_forwarded_user_token()
    with _SESSION_AGENT_LOCK:
        agent = _get_or_create_session_agent(session_id, actor_id)
        agent_key = id(agent)
        _SESSION_AGENT_ACTIVE[agent_key] = _SESSION_AGENT_ACTIVE.get(agent_key, 0) + 1
    try:
        with agent._agora_invocation_lock:
            # 이번 호출의 handle 을 **agent 소유 slot** 에 심어요 (IA-61 정정, 2026-08-30).
            #
            # contextvar 만으로는 MCP 호출에 실리지 않아요 — Strands 는 전송 스레드를 띄울
            # 때 context 스냅샷을 한 번만 떠서, 두 번째 턴이 회수된 첫 턴 handle 을 보내요
            # (`_BearerAuth` 주석에 실측 근거).
            #
            # lock 안이라 이 agent 의 다른 호출과 겹치지 않고, slot 이 agent 소유라 다른
            # 세션과도 섞이지 않아요. **빈 값이어도 항상 덮어써요** — 이전 턴 handle 이
            # 남으면 handle 없는 호출이 통과해요(인가 우회).
            slot = getattr(agent, "_agora_call_handle_slot", None)
            if slot is not None:
                slot.update({
                    "value": current_call_handle(),
                    "user_token": current_user_token(),
                    "user_token_forwarded": user_token_was_forwarded(),
                })
            yield agent
    finally:
        with _SESSION_AGENT_LOCK:
            remaining = _SESSION_AGENT_ACTIVE[agent_key] - 1
            if remaining:
                _SESSION_AGENT_ACTIVE[agent_key] = remaining
            else:
                del _SESSION_AGENT_ACTIVE[agent_key]
            _trim_session_agents_locked()


def _cleanup_session_agents() -> None:
    with _SESSION_AGENT_LOCK:
        agents = list(_SESSION_AGENTS.values())
        _SESSION_AGENTS.clear()
    for agent in agents:
        _cleanup_agent(agent)


atexit.register(_cleanup_session_agents)
'''
        if managed_memory
        else '''
def _observe_conversation_messages(
    agent: Agent,
    *,
    key: tuple[str, str],
    source: str,
) -> None:
    """DISABLED Memory에는 복원 대화 관측이 없어요."""
    return None


@contextmanager
def use_agent(*, session_id: str, actor_id: str):
    """Keep the disabled-Memory singleton behavior unchanged."""
    require_forwarded_user_token()
    # **handle 을 먼저 심고 나서 agent 를 만들어요.** 첫 호출에서 MCP `initialize` 가
    # 일어나는데, 그 시점에 handle 이 없으면 Gateway 가 막고 도구 0개로 떠요.
    agent = _cached_agent()
    with agent._agora_invocation_lock:
        # MANAGED Memory 경로와 **같은 이유**로 handle 을 여기서도 갱신해요 (IA-61 정정).
        # contextvar 만으로는 Strands 전송 스레드에 안 보여요 — `_BearerAuth` 주석 참고.
        # 이 경로가 기본값(Memory 사용 안 함)이라 여기가 빠지면 대부분의 agent 가 두 번째
        # 턴부터 `invalid_delegation` 이에요.
        slot = getattr(agent, "_agora_call_handle_slot", None)
        if slot is not None:
            slot.update({
                "value": current_call_handle(),
                "user_token": current_user_token(),
                "user_token_forwarded": user_token_was_forwarded(),
            })
        yield agent
'''
    )
    core_py = f'''"""{spec.name} — 에이전트 로직 (모델·프롬프트·도구 배선).
Agent Initializr가 생성한 스캐폴드예요. ../system_prompt.md 를 수정해 페르소나를 다듬으세요.
A2A 서버(main.py)가 run_agent(text)로 이 로직을 호출해요.
"""
import os
import pathlib
import re
from contextlib import contextmanager
from threading import RLock

{memory_imports}\
from strands import Agent
{conversation_manager_import}\
from strands.models import BedrockModel
{builtin_imports}\

{limit_hook_import}\
from agent.callback_handler import LineSafeCallbackHandler
from agent.tools import (
    current_call_handle,
    current_user_token,
    load_mcp_clients,
    load_skills,
    new_call_handle_slot,
    require_forwarded_user_token,
    user_token_was_forwarded,
)

_AGENTCORE_ACTOR_ID = re.compile({AGENTCORE_ACTOR_ID_PATTERN!r})
_AGENTCORE_ACTOR_ID_MAX_LENGTH = {AGENTCORE_ACTOR_ID_MAX_LENGTH}

model = BedrockModel(
    {model_arguments_source},
)

_PROMPT_PATH = pathlib.Path(__file__).resolve().parent.parent / "system_prompt.md"
SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")
{builtin_helpers}\

# skill은 plugins=, MCP는 tools= 예요. Strands가 둘을 다른 파라미터로 받아서
# skill(AgentSkills)을 tools=에 넣으면 로드되지 않아요(실측 2026-07-28).
def _create_agent(*, session_manager=None) -> Agent:
    callback_handler = LineSafeCallbackHandler()
    # 이 agent 의 MCP 클라이언트들이 공유하는 handle 칸. `use_agent` 가 호출마다 갱신해요.
    #
    # **여기서 미리 채워요.** 바로 아래 `Agent(tools=[MCPClient…])` 가 그 자리에서 MCP
    # `initialize` 를 하는데, 칸이 비어 있으면 Gateway 가 `invalid_delegation` 으로 막고
    # agent 가 **도구 0개**로 떠요(2026-08-30 실측: verify 가 `tools: []` 로 실패).
    handle_slot = new_call_handle_slot()
    handle_slot.update({{
        "value": current_call_handle(),
        "user_token": current_user_token(),
        "user_token_forwarded": user_token_was_forwarded(),
    }})
    agent = Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
{conversation_manager_argument}\
        # IH-101 re-enable: tools=[*load_mcp_clients(){{builtin_tools_list}}],
        tools=[*load_mcp_clients(handle_slot)],
        plugins=[*load_skills()],
{limit_hook_argument}\
        session_manager=session_manager,
        callback_handler=callback_handler,
    )
    agent._agora_callback_handler = callback_handler
    agent._agora_invocation_lock = RLock()
    agent._agora_call_handle_slot = handle_slot
    return agent


def _new_agent(*, session_id: str = "", actor_id: str = "") -> Agent:
{memory_setup}\


{cached_agent}\

{session_cache}\

def build_agent(*, session_id: str, actor_id: str) -> Agent:
{build_agent_body}


class AgentRunResult(str):
    """Text-compatible result carrying aggregate-only invocation usage."""

    def __new__(cls, text: str, *, usage: dict):
        value = super().__new__(cls, text)
        value.usage = usage
        return value


def _metric_snapshot(metrics) -> dict:
    usage = getattr(metrics, "accumulated_usage", {{}}) or {{}}
    accumulated = getattr(metrics, "accumulated_metrics", {{}}) or {{}}
    tools = {{}}
    for name, metric in (getattr(metrics, "tool_metrics", {{}}) or {{}}).items():
        tools[name] = {{
            "call_count": getattr(metric, "call_count", 0),
            "success_count": getattr(metric, "success_count", 0),
            "error_count": getattr(metric, "error_count", 0),
            "total_time": getattr(metric, "total_time", 0),
        }}
    return {{
        "input_tokens": usage.get("inputTokens", 0),
        "output_tokens": usage.get("outputTokens", 0),
        "total_tokens": usage.get("totalTokens", 0),
        "cache_read_input_tokens": usage.get("cacheReadInputTokens", 0),
        "cache_write_input_tokens": usage.get("cacheWriteInputTokens", 0),
        "latency_ms": accumulated.get("latencyMs", 0),
        "cycle_count": getattr(metrics, "cycle_count", 0),
        "cycle_durations": list(getattr(metrics, "cycle_durations", ()) or ()),
        "tool_metrics": tools,
    }}


def _metric_delta(current, previous):
    return current - previous if current >= previous else current


def _invocation_usage(metrics, before: dict) -> dict:
    after = _metric_snapshot(metrics)
    usage = {{
        field: _metric_delta(after[field], before[field])
        for field in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cache_read_input_tokens",
            "cache_write_input_tokens",
            "latency_ms",
            "cycle_count",
        )
    }}
    usage["cycle_durations"] = after["cycle_durations"][
        len(before["cycle_durations"]):
    ]
    tools = {{}}
    for name, metric in after["tool_metrics"].items():
        previous = before["tool_metrics"].get(name, {{}})
        delta = {{
            field: _metric_delta(metric[field], previous.get(field, 0))
            for field in (
                "call_count",
                "success_count",
                "error_count",
                "total_time",
            )
        }}
        if delta["call_count"] or delta["success_count"] or delta["error_count"]:
            tools[name] = delta
    usage["tool_metrics"] = tools
    return usage


def run_agent(text: str, *, session_id: str, actor_id: str) -> str:
    """사용자 텍스트를 에이전트에 넘기고 응답 문자열을 돌려줘요."""
    with use_agent(session_id=session_id, actor_id=actor_id) as agent:
        key = (session_id, actor_id)
        _observe_conversation_messages(agent, key=key, source="turn_start")
        before = _metric_snapshot(getattr(agent, "event_loop_metrics", None))
        try:
            result = agent(text)
            metrics = getattr(result, "metrics", None)
            return AgentRunResult(
                str(result),
                usage=(
                    _invocation_usage(metrics, before)
                    if metrics is not None
                    else {{}}
                ),
            )
        except Exception as error:
            metrics = getattr(agent, "event_loop_metrics", None)
            if metrics is not None:
                error._agora_usage = _invocation_usage(metrics, before)
            # IH-70: 턴이 실패하면 사용자 메시지는 이미 저장됐는데 응답이 없어서 다음 턴에
            # user 가 연속으로 쌓여요. Bedrock 은 역할 교대를 요구하므로 **한 번 실패한
            # 세션이 영구히 못 쓰게** 돼요(실측 2026-08-22: 이후 모든 턴이 같은 오류).
            # 그래서 실패도 assistant 메시지로 기록해 교대를 지켜요. 실패를 성공처럼
            # 꾸미는 게 아니라 "이 턴은 실패했다"를 이력에 남기는 거예요 — 예외는 그대로
            # 올려서 호출자가 오류로 처리해요.
            # 이 보정은 **살아 있는 세션 Agent** 의 이력만 고쳐요(캐시된 Agent 는 다음
            # 턴에 재사용되니 여기서 막아야 해요). Memory 에 남은 오염은 다음 복원 때
            # `_repair_restored_conversation` 이 고쳐요 — 두 경로가 함께 필요해요.
            # Strands 의 `_append_messages` 는 async·private 이라 쓰지 않고 리스트에 직접
            # 넣어요(세션 매니저 훅을 타지 않지만, 그건 복원 복구가 담당해요).
            messages = getattr(agent, "messages", None)
            if messages and messages[-1].get("role") == "user":
                messages.append({{"role": "assistant", "content": [
                    {{"text": "(이 턴은 오류로 응답하지 못했어요.)"}}
                ]}})
            raise
        finally:
            _observe_conversation_messages(agent, key=key, source="turn_end")
            callback_handler = getattr(agent, "_agora_callback_handler", None)
            flush = getattr(callback_handler, "flush", None)
            if callable(flush):
                flush()
'''

    # 생성물 tools.py 내부에 f-string이 많아, f-string 이스케이프 대신
    # plain 템플릿 + 치환으로 구성해요 (@@MCP_COMMENTS@@ / @@MCP_URLS@@).
    mcp_lines = "\n".join(
        f'# {m.name}: {m.endpoint or "(endpoint 미지정)"}' for m in mcps
    )
    # _MCP_ASSETS의 "name"은 생성 코드에서 authorization 비교 기준(target_name)으로
    # 쓰여요 — Gateway 정규화 이름을 넣어야 라이브 tool 접두어와 매칭돼요(결함 #10).
    mcp_assets = repr([
        {
            "asset_id": m.asset_id,
            "name": target_name,
            "endpoint": m.endpoint,
            "tool_prefixes": gateway_tool_prefixes(target_name),
            "operations": list(target_operations),
            "allowed_tool_names": tuple(
                tool_name
                for operation in target_operations
                for tool_name in gateway_tool_names(
                    target_name,
                    operation,
                )
            ),
        }
        for m in mcps
        for target_name, target_operations in _target_bindings(m)
    ])
    skill_comments = "\n".join(f"# {t.name} → skills/{d}/" for t, d in zip(skills, skill_dirs))
    skill_dirs_lit = ", ".join(repr(d) for d in skill_dirs)
    error_helpers_py = r'''
_ERROR_TEXT_LIMIT = 256
_ERROR_DETAIL_LIMIT = 12
_ERROR_DATA_DEPTH_LIMIT = 4
_ERROR_DATA_NODE_LIMIT = 64
_ERROR_DATA_FIELD_LIMIT = 24
_SENSITIVE_FIELD_PARTS = (
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+\S+")
_EMAIL_RE = re.compile(
    r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(?:[\"'])?(?:access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"client[_-]?secret|api[_-]?key|password|credential|authorization)"
    r"(?:[\"'])?\s*[:=]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)"
)
_SAFE_ENUM_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SAFE_COORDINATE_RE = re.compile(r"^[A-Za-z0-9_.:/=;-]{1,200}$")


def _byte_size(value) -> int:
    if isinstance(value, bytes):
        return len(value)
    return len(str(value).encode("utf-8", errors="replace"))


def _new_disclosure_stats() -> dict:
    return {
        "redacted_fields": 0,
        "redacted_bytes": 0,
        "omitted_fields": 0,
        "omitted_bytes": 0,
        "_nodes": 0,
    }


def _is_sensitive_field(name) -> bool:
    normalized = str(name).strip().lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_FIELD_PARTS)


def _redact_text(value, stats: dict, limit: int = _ERROR_TEXT_LIMIT) -> str:
    text = (
        value.decode("utf-8", errors="replace")
        if isinstance(value, bytes)
        else str(value)
    )

    def _replace(pattern, marker: str, current: str) -> str:
        def _sub(match):
            stats["redacted_fields"] += 1
            stats["redacted_bytes"] += _byte_size(match.group(0))
            return marker

        return pattern.sub(_sub, current)

    text = _replace(_BEARER_RE, "<redacted:bearer>", text)
    text = _replace(_EMAIL_RE, "<redacted:email>", text)
    text = _replace(
        _SECRET_ASSIGNMENT_RE,
        "<redacted:secret-assignment>",
        text,
    )
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) > limit:
        kept = encoded[:limit].decode("utf-8", errors="ignore")
        stats["omitted_fields"] += 1
        stats["omitted_bytes"] += len(encoded) - len(
            kept.encode("utf-8", errors="replace")
        )
        return kept + "...[truncated]"
    return text


def _sanitize_data(value, stats: dict, *, depth: int = 0):
    if (
        depth > _ERROR_DATA_DEPTH_LIMIT
        or stats["_nodes"] >= _ERROR_DATA_NODE_LIMIT
    ):
        stats["omitted_fields"] += 1
        if isinstance(value, (str, bytes, int, float, bool)) or value is None:
            stats["omitted_bytes"] += _byte_size(value)
        return "<omitted:structure-limit>"
    stats["_nodes"] += 1
    if isinstance(value, dict):
        output = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _ERROR_DATA_FIELD_LIMIT:
                stats["omitted_fields"] += len(value) - index
                break
            safe_key = _redact_text(key, stats, limit=80)
            if _is_sensitive_field(key):
                stats["redacted_fields"] += 1
                stats["redacted_bytes"] += _byte_size(item)
                output[safe_key] = "<redacted:sensitive-field>"
            else:
                output[safe_key] = _sanitize_data(
                    item,
                    stats,
                    depth=depth + 1,
                )
        return output
    if isinstance(value, (list, tuple)):
        output = [
            _sanitize_data(item, stats, depth=depth + 1)
            for item in value[:_ERROR_DATA_FIELD_LIMIT]
        ]
        if len(value) > _ERROR_DATA_FIELD_LIMIT:
            stats["omitted_fields"] += len(value) - _ERROR_DATA_FIELD_LIMIT
        return output
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str) and _SAFE_ENUM_RE.fullmatch(value):
        return value
    stats["omitted_fields"] += 1
    stats["omitted_bytes"] += _byte_size(value)
    return "<omitted:string>"


def _public_stats(stats: dict) -> dict:
    return {key: value for key, value in stats.items() if not key.startswith("_")}


def _response_coordinates(response) -> dict:
    coordinates = {}
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        coordinates["http_status"] = status
    headers = getattr(response, "headers", None)
    if headers is not None:
        for name, key in (
            ("x-amzn-requestid", "request_id"),
            ("x-amz-request-id", "request_id"),
            ("x-amzn-trace-id", "trace_id"),
        ):
            observed = headers.get(name)
            if observed and _SAFE_COORDINATE_RE.fullmatch(str(observed)):
                coordinates[key] = str(observed)
    return coordinates


def _field(value, name: str):
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _coordinate(value):
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str) and _SAFE_COORDINATE_RE.fullmatch(value):
        return value
    return None


def _value_length(value) -> int | None:
    if isinstance(value, (str, bytes, list, tuple, dict)):
        return len(value)
    return None


def _result_coordinates(result, *, tool: str | None = None) -> dict:
    coordinates = {"error_type": type(result).__name__}
    if tool:
        coordinates["tool"] = tool
    for name in ("code", "http_status", "status_code", "request_id", "trace_id"):
        observed = _coordinate(_field(result, name))
        if observed is not None:
            key = "http_status" if name == "status_code" else name
            coordinates[key] = observed
    content_length = _value_length(_field(result, "content"))
    if content_length is not None:
        coordinates["content_length"] = content_length
    structured = _field(result, "structuredContent")
    if structured is None:
        structured = _field(result, "structured_content")
    structured_length = _value_length(structured)
    if structured_length is not None:
        coordinates["structured_content_length"] = structured_length
    return coordinates


def _structured_mcp_error(error) -> dict | None:
    mcp_error = getattr(error, "error", None)
    if mcp_error is None:
        return None
    stats = _new_disclosure_stats()
    structured = {
        "code": _sanitize_data(getattr(mcp_error, "code", None), stats),
        "message": _redact_text(getattr(mcp_error, "message", ""), stats),
        "data": _sanitize_data(getattr(mcp_error, "data", None), stats),
    }
    structured["redaction"] = _public_stats(stats)
    return structured


def _exception_details(error: BaseException) -> list[dict]:
    """Flatten exception groups/chains with bounded, body-free diagnostics."""
    details: list[dict] = []
    seen: set[int] = set()
    stack: list[tuple[str, BaseException]] = [("root", error)]
    while stack and len(details) < _ERROR_DETAIL_LIMIT:
        relation, current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        stats = _new_disclosure_stats()
        detail = {
            "relation": relation,
            "type": type(current).__name__,
            "message": _redact_text(current, stats),
        }
        response = getattr(current, "response", None)
        if response is not None:
            detail.update(_response_coordinates(response))
        mcp_error = _structured_mcp_error(current)
        if mcp_error is not None:
            detail["mcp_error"] = mcp_error
        disclosure = _public_stats(stats)
        if any(disclosure.values()):
            detail["redaction"] = disclosure
        details.append(detail)
        remaining = _ERROR_DETAIL_LIMIT - len(details)
        if isinstance(current, BaseExceptionGroup):
            stack.extend(
                ("group", child)
                for child in reversed(current.exceptions[:remaining])
            )
        context = current.__context__
        if context is not None and context is not current.__cause__:
            stack.append(("context", context))
        if current.__cause__ is not None:
            stack.append(("cause", current.__cause__))
    return details


def _exception_reason(error: BaseException) -> str:
    parts = []
    for detail in _exception_details(error):
        suffix = []
        if "http_status" in detail:
            suffix.append(f"status={detail['http_status']}")
        if "request_id" in detail:
            suffix.append(f"request_id={detail['request_id']}")
        if "trace_id" in detail:
            suffix.append(f"trace_id={detail['trace_id']}")
        if "mcp_error" in detail:
            suffix.append(
                "mcp_error="
                + json.dumps(
                    detail["mcp_error"],
                    ensure_ascii=False,
                    default=str,
                )
            )
        metadata = f" [{', '.join(suffix)}]" if suffix else ""
        parts.append(
            f"{detail['type']}: {detail['message']}{metadata}"
        )
    return " | ".join(parts)


def _protocol_error_payload(result) -> dict:
    return {
        "isError": True,
        **_result_coordinates(result),
    }


#: Agora Gateway 가 쓴 거부 문구 — 슬러그 → 문장. 포털의 `shared/gateway_denial.py` 에서
#: 생성 시점에 박아 넣어요(사본을 손으로 유지하지 않아요, IH-183).
_AGORA_DENIAL_MESSAGES = @@DENIAL_MESSAGES@@
_AGORA_DENIAL_REASON_BY_MESSAGE = {
    message: reason for reason, message in _AGORA_DENIAL_MESSAGES.items()
}


def _denial_reason(result) -> str:
    """도구 결과가 Agora Gateway 의 인가 거부인지 **정확 일치**로 알아봐요 (IH-183).

    Strands 는 `McpError` 를 `str(exception)`(= `error.message`) 로 접어 도구 결과의 content
    텍스트에 담아요. interceptor 가 그 자리에 싣는 값은 위 표의 문장 그대로예요.

    **부분 일치를 허용하지 않아요.** 허용하면 MCP 가 자기 에러 본문에 이 문장을 끼워 넣어
    다른 실패를 「권한 문제」로 위장할 수 있어요. 알아보지 못한 텍스트는 사유 없이 지나가고,
    본문은 `_result_coordinates` 의 allowlist 대로 계속 빠져요.
    """
    content = _field(result, "content")
    if not isinstance(content, (list, tuple)):
        return ""
    for item in content[:_ERROR_DATA_FIELD_LIMIT]:
        text = _field(item, "text")
        if isinstance(text, str):
            reason = _AGORA_DENIAL_REASON_BY_MESSAGE.get(text.strip(), "")
            if reason:
                return reason
    return ""


def _tool_probe_payload(result, *, tool: str) -> dict:
    status = _field(result, "status")
    is_error = bool(_field(result, "isError")) or status == "error"
    payload = {
        "status": "error" if is_error else "success",
        **_result_coordinates(result, tool=tool),
    }
    # 실패했을 때만 봐요 — 성공 결과에 이 문장이 들어 있어도 사유가 아니에요.
    if is_error and (reason := _denial_reason(result)):
        payload["denial_reason"] = reason
    return payload
'''.replace(
        "@@DENIAL_MESSAGES@@",
        repr(dict(DENIAL_MESSAGES_BY_REASON)),
    )
    memory_logging_config = (
        '    # AgentCore Memory retrieval 성공은 INFO 로만 남고 기본 WARNING 레벨에서는\n'
        '    # 실패 ERROR 만 보여요(IH-95). 전체 로그 레벨은 건드리지 않고 Memory 통합\n'
        '    # namespace 만 INFO 로 올려 주입 성공을 CloudWatch 에서 관측하게 해요.\n'
        '    logging.getLogger("bedrock_agentcore.memory.integrations.strands")'
        ".setLevel(logging.INFO)\n"
        if managed_memory
        else ""
    )
    logging_config_py = f'''"""Generated agent logging contract."""
from __future__ import annotations

import logging
import sys

log = logging.getLogger("agent")


def configure_logging() -> None:
    """Configure generated-agent loggers without enabling global DEBUG."""
    # AgentCore A2A codezip은 tools import 뒤 uvicorn.run()으로 기동돼요. uvicorn의
    # logger는 root로 전파하지 않고 BedrockAgentCoreApp은 자체 handler를 붙이므로,
    # 그 logger만 전파를 끄고 root에 stdout 출구를 한 번 만들어요.
    logging.getLogger("bedrock_agentcore.app").propagate = False
    logging.basicConfig(
        stream=sys.stdout,
        format="%(levelname)s %(name)s %(message)s",
    )
    # 생성 코드의 모든 agent.* INFO를 보장해요. 전체 root를 DEBUG로 올리지 않아요.
    # IH-70의 원래 "restored conversation repaired" 로그도 이 레벨 누락으로 버려졌어요.
    log.setLevel(logging.INFO)

    # Strands event loop는 traceback을 DEBUG에만 남겨 이 namespace만 올려요.
    logging.getLogger("strands.event_loop").setLevel(logging.DEBUG)
    logging.getLogger("strands.agent.conversation_manager").setLevel(
        logging.DEBUG
    )
{memory_logging_config}'''
    callback_handler_py = '''"""Line-safe Strands callback output for CloudWatch."""
from __future__ import annotations


class LineSafeCallbackHandler:
    """Buffer streaming text so stdout never contains a partial response line."""

    def __init__(self, verbose_tool_use: bool = True) -> None:
        self.tool_count = 0
        self._verbose_tool_use = verbose_tool_use
        self._text_chunks: list[str] = []

    def _append_text(self, value: object) -> None:
        if not isinstance(value, str) or not value:
            return
        self._text_chunks.append(value)
        if "\\n" not in value:
            return
        lines = "".join(self._text_chunks).split("\\n")
        self._text_chunks = [lines.pop()]
        for line in lines:
            print(line.removesuffix("\\r"))

    def flush(self) -> None:
        if not self._text_chunks:
            return
        print("".join(self._text_chunks))
        self._text_chunks.clear()

    def __call__(self, **kwargs: object) -> None:
        self._append_text(kwargs.get("reasoningText"))
        self._append_text(kwargs.get("data"))

        event = kwargs.get("event")
        tool_use = None
        # Some models use delta, but Agora only generates BedrockModel agents, whose tool
        # name/id arrives in contentBlockStart.
        if isinstance(event, dict):
            tool_use = (
                event.get("contentBlockStart", {})
                .get("start", {})
                .get("toolUse")
            )
        if isinstance(tool_use, dict):
            self.flush()
            self.tool_count += 1
            if self._verbose_tool_use:
                print(f"Tool #{self.tool_count}: {tool_use.get('name', '')}")

        if kwargs.get("complete") is True or kwargs.get("result") is not None:
            self.flush()
'''
    tools_py = '''"""선택한 skill·MCP를 Strands 도구로 로드해요.

skill은 Agent(plugins=[...]), MCP는 Agent(tools=[...])로 들어가요 — Strands가
둘을 다른 파라미터로 받아요.

MCP Gateway는 Cognito OAuth Bearer를 요구해요. **두 환경이 서로 폴백하지 않아요** —
`_deployed_runtime()` 로 갈라요.

배포 런타임(AGORA_OAUTH_PROVIDER_NAME + AGORA_WORKLOAD_IDENTITY_NAME 둘 다 있음):
- AgentCore Identity 만 써요. GetWorkloadAccessToken → GetResourceOauth2Token(M2M) —
  client secret은 Token Vault에 있고 컨테이너엔 없어요.
- 실패하면 도구 없이 기동해요(경고 로그). dev 크리덴셜로 내려가지 않아요 — 폴백하면
  ZIP 을 내려받은 사람의 신원으로 호출이 나가거든요(호출자가 아니라요).

로컬 실행(위 두 값이 없음):
1) Agora dev broker: AGORA_DEV_TOKEN_URL + AGORA_DEV_CREDENTIAL.
2) .env fallback: COGNITO_TOKEN_URL/CLIENT_ID/CLIENT_SECRET로 직접 발급.
3) 둘 다 없으면 MCP 도구 없이 기동해요(경고 로그).

토큰은 요청 시점에 httpx.Auth로 주입해요. 세션이 토큰 만료보다 오래 살아도
매 요청마다 최신 토큰이 실려요.
"""
import asyncio
import base64
import binascii
import concurrent.futures
import contextvars
import json
import logging
import math
import os
import pathlib
import re
import time

import boto3
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from strands.tools.mcp import MCPClient

log = logging.getLogger(__name__)

# 생성물 루트(= agent/ 의 부모). skills/ 본문을 여기 기준으로 찾아요.
_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Initializr에서 선택한 skill (본문은 skills/{dir}/SKILL.md 에 구워져요).
@@SKILL_COMMENTS@@_SKILL_DIRS: list[str] = [@@SKILL_DIRS@@]

# Initializr에서 선택한 MCP 자산·Gateway endpoint의 결속(로컬 폴백용).
@@MCP_COMMENTS@@_baked_mcp_assets: list[dict] = @@MCP_ASSETS@@
# 배포 환경은 승인 Registry record로 해석된 binding을 AGORA_MCP_ASSETS env로 주입해요
# (endpoint 재해석·재등록 반영). 주입값을 우선 쓰고, 없으면(로컬 개발) 위 생성 시점
# 목록으로 폴백해요 — endpoint를 코드에 고정하지 않아요.
_injected_mcp_assets = os.environ.get("AGORA_MCP_ASSETS", "").strip()
_MCP_ASSETS: list[dict] = (
    json.loads(_injected_mcp_assets) if _injected_mcp_assets else _baked_mcp_assets
)

_token_cache: dict = {"value": None, "exp": 0.0}
@@ERROR_HELPERS@@


def _deployed_runtime() -> bool:
    """지금 **배포 런타임**인가요? — dev 폴백을 잠글지 결정해요.

    ## 왜 이 판별이 필요한가요

    Initializr ZIP 의 `.env` 에는 7일 유효한 dev 크리덴셜이 들어 있어요. 그 폴더가 소스로
    올라가 컨테이너에 들어오면, Identity 가 실패하는 순간 배포된 agent 가 **ZIP 을 내려받은
    사람의 신원으로** 조용히 돌아요 — 호출자가 아니라요. 토큰과 호출 handle 이 짝을 이뤄서
    둘 다 그 사람 것이 돼요. 그래서 배포 런타임에서는 폴백을 아예 잠가요. 실패하면 도구 없이
    도는 게 맞아요 — 남의 신원으로 성공하는 것보다요(fail-closed).

    ## 신호를 두 개 보는 이유

    - `AGORA_RUNTIME_ENV=deployed` — 배포 API 가 **조건 없이** 넣는 표식이에요
      (`runtime/deploy/aws_adapter.py` `_agent_runtime_payload`). 이게 정본이에요.
    - Identity 두 값 — 이 표식이 생기기 **전에 배포된** 런타임을 위한 폴백이에요. 그 런타임은
      재배포할 때까지 표식이 없어요.

    파생 신호만 쓰면 안 돼요: MCP 도구가 없는 agent 는 주입할 env 가 하나도 없어서
    `environmentVariables` 파라미터 자체가 생략돼요. 그때 Identity 두 값도 없으니 배포
    런타임을 로컬로 오인하게 돼요 — 폴백이 살아나는 바로 그 조합이에요.

    로컬에서 이 값들을 손으로 설정하면 dev 브로커가 꺼져서 도구가 안 불려요. 그건 사용자가
    자기 발을 쏘는 경우라 막지 않아요(`docs/06-risks.md`).
    """
    if os.environ.get("AGORA_RUNTIME_ENV") == "deployed":
        return True
    return bool(
        os.environ.get("AGORA_OAUTH_PROVIDER_NAME")
        and os.environ.get("AGORA_WORKLOAD_IDENTITY_NAME")
    )


def _identity_token() -> str | None:
    """AgentCore Identity 경유 M2M 토큰 (배포 환경 전용).

    workload access token은 요청 헤더(WorkloadAccessToken)로만 오고 env에는 없어요.
    모듈 로드 시점엔 요청이 없으니 exec role 자격으로 직접 발급해요
    (AGORA_WORKLOAD_IDENTITY_NAME = 배포 API가 만든 우리 소유 workload identity).
    """
    provider = os.environ.get("AGORA_OAUTH_PROVIDER_NAME")
    workload_name = os.environ.get("AGORA_WORKLOAD_IDENTITY_NAME")
    if not provider or not workload_name:
        return None
    client = boto3.client("bedrock-agentcore")
    workload_token = client.get_workload_access_token(
        workloadName=workload_name,
    )["workloadAccessToken"]
    scope = os.environ.get("AGORA_OAUTH_SCOPE", "")
    resp = client.get_resource_oauth2_token(
        workloadIdentityToken=workload_token,
        resourceCredentialProviderName=provider,
        scopes=[scope] if scope else [],
        oauth2Flow="M2M",
    )
    return resp.get("accessToken")


def _env_token() -> str | None:
    """.env client_credentials fallback (로컬 개발 전용).

    urllib이 아니라 httpx를 써요. urllib은 file:// 스킴도 받아서, token URL이
    설정으로 흘러오는 이 자리에선 임의 파일 읽기 표면이 돼요(semgrep
    dynamic-urllib-use-detected). httpx는 http/https만 지원해 그 표면이 없어요.
    """
    url = os.environ.get("COGNITO_TOKEN_URL")
    cid = os.environ.get("COGNITO_CLIENT_ID")
    secret = os.environ.get("COGNITO_CLIENT_SECRET")
    if not (url and cid and secret):
        return None
    scope = os.environ.get("COGNITO_SCOPE", "")
    form = {"grant_type": "client_credentials"}
    if scope:
        form["scope"] = scope
    resp = httpx.post(url, data=form, auth=(cid, secret), timeout=10)
    resp.raise_for_status()
    data = resp.json()
    _token_cache["exp"] = time.time() + float(data.get("expires_in", 3600))
    return data.get("access_token")


def _dev_broker_token() -> str | None:
    """Agora가 발급한 opaque dev credential을 access token으로 교환해요."""
    url = os.environ.get("AGORA_DEV_TOKEN_URL")
    credential = os.environ.get("AGORA_DEV_CREDENTIAL")
    if not (url and credential):
        return None
    resp = httpx.post(
        url,
        headers={"Authorization": f"Bearer {credential}"},
        timeout=10,
    )
    if resp.status_code == 401:
        raise RuntimeError(
            "dev 크리덴셜이 만료되었거나 폐기됐어요. "
            "포털에서 dev 크리덴셜을 재발급하세요."
        )
    resp.raise_for_status()
    data = resp.json()
    _token_cache["exp"] = time.time() + float(data["expires_in"])
    return data.get("access_token")


def _dev_broker_call_handle() -> str:
    """dev 크리덴셜을 **호출 handle 한 개**로 바꿔요. 로컬 실행 전용이에요.

    Gateway REQUEST interceptor 는 모든 `tools/call` 에 `X-Agora-Call` 을 요구해요. 배포된
    agent 는 Agora 가 호출할 때 handle 을 실어 주지만, 로컬 실행에는 그 경로가 없어요 —
    그래서 크리덴셜로 Agora 에 물어봐요.

    **캐시하지 않아요.** handle TTL 은 60초예요. 캐시하면 곧 `invalid_delegation` 으로 막히고,
    무엇보다 handle 은 그 자체가 호출 자격이라 오래 들고 있을 이유가 없어요.
    """
    if _deployed_runtime():
        # 배포 런타임 — Agora 가 호출할 때 handle 을 실어 줘요. 여기서 브로커를 부르면 그
        # 호출의 신원이 **dev 크리덴셜 소유자**로 바뀌어요. 빈 값을 돌려주면 헤더가 붙지
        # 않고 interceptor 가 fail-closed 로 거부해요 — 그게 맞는 결과예요.
        return ""
    url = os.environ.get("AGORA_DEV_CALL_HANDLE_URL")
    credential = os.environ.get("AGORA_DEV_CREDENTIAL")
    if not (url and credential):
        return ""
    resp = httpx.post(
        url,
        headers={"Authorization": f"Bearer {credential}"},
        timeout=10,
    )
    if resp.status_code == 401:
        raise RuntimeError(
            "dev 크리덴셜이 만료되었거나 폐기됐어요. "
            "포털에서 dev 크리덴셜을 재발급하세요."
        )
    resp.raise_for_status()
    return str(resp.json().get("delegation_handle") or "")


def _get_token(slot: dict | None = None) -> str | None:
    """Gateway Bearer를 돌려줘요.

    사람 토큰은 요청마다 헤더로 오므로 캐시하지 않고 agent slot의 현재 값을 그대로
    써요. 포털이 전달을 선언했는데 헤더가 사라진 경우에도 기계 토큰으로 폴백하지
    않아요 — 선언 없는 VERIFYING·폴러 요청만 기존 기계 토큰 캐시를 사용해요.
    """
    if slot is not None:
        user_token = slot.get("user_token")
        user_token_forwarded = slot.get("user_token_forwarded") is True
        if isinstance(user_token, str) and user_token:
            return user_token
        if user_token_forwarded:
            return None
    if _token_cache["value"] and time.time() < _token_cache["exp"] - 60:
        return _token_cache["value"]
    # 재발급이니 이전 만료값을 비워요. 안 비우면 Identity 경로(응답에 TTL 없음)에서
    # 아래 `if not exp` 분기가 두 번째부터 거짓이 되어 캐시가 영구 만료돼요.
    _token_cache["exp"] = 0.0
    try:
        if _deployed_runtime():
            # 배포 런타임 — Identity 로만 발급해요. 실패해도 dev 크리덴셜·.env 로
            # 내려가지 않아요. 폴백하면 내려받은 사람의 신원으로 호출이 나가거든요.
            token = _identity_token()
            if token is None:
                log.warning(
                    "AgentCore Identity 토큰이 비었어요. 배포 런타임에서는 dev 폴백을 "
                    "쓰지 않아요 — MCP 도구 없이 계속해요."
                )
        else:
            token = _dev_broker_token() or _env_token()
    except Exception as e:
        log.warning("MCP 토큰 발급 실패: %s", e)
        return None
    if token:
        _token_cache["value"] = token
        if not _token_cache["exp"]:
            # Identity 응답엔 TTL이 없어 55분으로 잡아요(Cognito 기본 1시간).
            _token_cache["exp"] = time.time() + 3300
    return token


_CALL_HANDLE: contextvars.ContextVar = contextvars.ContextVar(
    "agora_call_handle", default=""
)
_USER_TOKEN: contextvars.ContextVar = contextvars.ContextVar(
    "agora_user_token", default=""
)
_USER_TOKEN_FORWARDED: contextvars.ContextVar = contextvars.ContextVar(
    "agora_user_token_forwarded", default=False
)


def set_call_handle(handle) -> None:
    """이번 호출의 delegation handle 을 요청 스코프에 기록해요 (IA-61).

    **이 값만으로는 MCP 호출에 실리지 않아요.** `agent_call_handle_slot()` 로 얻은 agent
    소유 slot 에 복사돼야 해요 — 이유는 아래 `_BearerAuth` 주석에 있어요.

    **빈 값이어도 항상 set 해요.** 안 그러면 같은 task 에서 이전 호출의 handle 이 남아
    handle 없는 호출이 통과할 수 있어요 — 그건 인가 우회예요.
    """
    _CALL_HANDLE.set(handle if isinstance(handle, str) else "")


def current_call_handle() -> str:
    """요청 스코프에 기록된 handle. 없으면 빈 문자열."""
    return _CALL_HANDLE.get()


def set_user_token(token, *, forwarded: bool = False) -> None:
    """이번 호출의 사람 토큰과 포털의 전달 선언을 요청 스코프에 기록해요."""
    _USER_TOKEN.set(token if isinstance(token, str) else "")
    _USER_TOKEN_FORWARDED.set(forwarded is True)


def current_user_token() -> str:
    """요청 스코프의 사람 access token. 없으면 빈 문자열."""
    return _USER_TOKEN.get()


def user_token_was_forwarded() -> bool:
    """포털 body가 사람 토큰 전달을 선언했는지 돌려줘요."""
    return _USER_TOKEN_FORWARDED.get()


_FORWARDED_USER_TOKEN_MESSAGES = {
    "user_token_not_forwarded": (
        "포털이 사람 토큰 전달을 선언했지만 Runtime 헤더에서 찾지 못했어요."
    ),
    "user_token_expired": "전달된 사람 토큰이 만료됐어요.",
    "user_token_invalid": "전달된 사람 토큰의 만료 시각을 확인할 수 없어요.",
}


class ForwardedUserTokenError(RuntimeError):
    """사람 토큰 전달 계약이 깨졌음을 A2A 경계까지 보존해요."""

    def __init__(self, reason: str):
        message = _FORWARDED_USER_TOKEN_MESSAGES.get(reason)
        if message is None:
            raise ValueError("지원하지 않는 사람 토큰 오류 사유예요.")
        super().__init__(message)
        self.reason = reason
        # handler.py는 tools.py를 import하지 않고 단독 실행돼요. namespaced attribute로만
        # typed error를 식별해 순환 의존 없이 `error.data.reason`으로 바꿔요.
        self._agora_user_token_reason = reason


def _user_token_expiry(token: str) -> float | None:
    """서명 판정 없이 JWT exp만 읽어요. 허용은 Gateway가 서명 검증한 뒤 결정해요."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        encoded = parts[1]
        encoded += "=" * (-len(encoded) % 4)
        claims = json.loads(
            base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8")
        )
    except (
        binascii.Error,
        UnicodeDecodeError,
        UnicodeEncodeError,
        ValueError,
        TypeError,
    ):
        return None
    if not isinstance(claims, dict):
        return None
    expiry = claims.get("exp")
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
        return None
    expiry = float(expiry)
    return expiry if math.isfinite(expiry) else None


def require_forwarded_user_token(
    slot: dict | None = None,
    *,
    now: float | None = None,
) -> None:
    """선언된 사람 토큰이 실제로 있고 아직 유효한지 요청 경계에서 확인해요."""
    if slot is None:
        token = current_user_token()
        forwarded = user_token_was_forwarded()
    else:
        token = slot.get("user_token")
        forwarded = slot.get("user_token_forwarded") is True
    if not forwarded:
        return
    if not isinstance(token, str) or not token:
        raise ForwardedUserTokenError("user_token_not_forwarded")
    expiry = _user_token_expiry(token)
    if expiry is None:
        raise ForwardedUserTokenError("user_token_invalid")
    if expiry <= (time.time() if now is None else now):
        raise ForwardedUserTokenError("user_token_expired")


def new_call_handle_slot() -> dict:
    """agent 하나가 소유하는 handle·사람 토큰 칸을 만들어요."""
    return {
        "value": "",
        "user_token": "",
        "user_token_forwarded": False,
    }


class _BearerAuth(httpx.Auth):
    """요청 직전에 토큰과 호출 handle 을 붙여요 — 세션이 길어도 만료 토큰을 안 보내요.

    ## handle 을 contextvar 에서 직접 읽지 않는 이유 (2026-08-30 실측)

    Strands `MCPClient` 는 전송을 **백그라운드 스레드**에서 돌리고, 그 스레드를 띄울 때
    `contextvars.copy_context()` 로 **스냅샷을 한 번만** 떠요(`mcp_client.py:342`). 그래서
    스레드는 `start()` 순간의 handle 을 영구히 들고 있어요.

    Agora 는 handle 을 **호출마다 발급하고 끝나면 회수**해요. 두 사실이 겹치면 **첫 턴만
    동작**해요 — 두 번째 턴은 회수된 첫 턴 handle 을 보내서 `invalid_delegation` 이에요.
    (실측: 원장의 delegation 행 2개가 모두 `revoked_at` 을 갖고, interceptor 로그에
    `denied method=tools/call reason=invalid_delegation` 2건.)

    그래서 **가변 slot** 을 읽어요. dict 는 스레드 경계를 넘어 보이고, slot 은 **agent
    하나가 소유**해서 다른 세션과 섞이지 않아요. agent 당 동시 호출은
    `_agora_invocation_lock` 이 막아 주니 그 안에서 갱신하면 경쟁도 없어요.
    """

    def __init__(self, handle_slot: dict):
        self._handle_slot = handle_slot

    def auth_flow(self, request):
        require_forwarded_user_token(self._handle_slot)
        token = _get_token(self._handle_slot)
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
        # Gateway REQUEST interceptor 가 이 헤더로 원장을 조회해요(IA-54). 헤더 이름은
        # `X-Agora-Call` 로 확정됐어요(ADR-0091 — ADR-0078 의 `X-Agora-Handle` 을 대체).
        # 값이 없으면 헤더를 아예 붙이지 않아요. 빈 헤더를 보내면 interceptor 가 "빈
        # handle" 과 "헤더 없음" 을 구분해야 하는데, 그 구분에 의미가 없어요.
        handle = str(self._handle_slot.get("value") or "")
        if not handle:
            # 로컬 실행 경로 — Agora 가 호출한 게 아니라 슬롯이 비어 있어요. dev 크리덴셜이
            # 있으면 그걸로 handle 을 받아요. 없으면 헤더를 붙이지 않고, interceptor 가
            # 거부해요(fail-closed).
            try:
                handle = _dev_broker_call_handle()
            except Exception as e:  # noqa: BLE001
                log.warning("dev 호출 handle 발급 실패: %s", e)
                handle = ""
        if handle:
            request.headers["X-Agora-Call"] = handle
        yield request


async def _probe_gateway_authorization(
    *, tool: str, endpoint: str, arguments: dict
) -> dict:
    """Call Gateway directly and report only untrusted, structured candidates.

    이 probe 는 **요청 스코프에서 직접 돌아요**(백그라운드 스레드가 아니에요). 그래서
    contextvar 를 그대로 읽어 slot 을 만들어요 — Strands 클라이언트와 달리 스냅샷 문제가
    없어요.
    """
    phase = "initialize"
    try:
        async with streamablehttp_client(
            endpoint,
            auth=_BearerAuth({
                "value": current_call_handle(),
                "user_token": current_user_token(),
                "user_token_forwarded": user_token_was_forwarded(),
            }),
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                phase = "tools_call"
                result = await session.call_tool(tool, arguments)
    except Exception as error:
        exceptions = _exception_details(error)
        status = next(
            (
                detail["http_status"]
                for detail in exceptions
                if "http_status" in detail
            ),
            None,
        )
        request_id = next(
            (
                detail["request_id"]
                for detail in exceptions
                if "request_id" in detail
            ),
            None,
        )
        identifier = None
        for detail in exceptions:
            mcp_error = detail.get("mcp_error")
            if not isinstance(mcp_error, dict):
                continue
            error_data = mcp_error.get("data")
            identifier = {
                "code": mcp_error.get("code"),
                "reason": (
                    error_data.get("reasonCode")
                    if isinstance(error_data, dict)
                    else None
                ),
            }
            break
        candidate = {
            "kind": "exception",
            "phase": phase,
            "http_status": status,
            "request_id": request_id,
            "gateway_identifier": identifier,
            "exceptions": exceptions,
        }
        return {
            "outcome": "UNKNOWN",
            "source": "gateway_probe_candidate",
            "reason": (
                "Gateway deny candidate is untrusted until Agora observes "
                "a Gateway-owned identifier independently."
            ),
            "candidate": candidate,
        }
    if getattr(result, "isError", False):
        protocol_error = _protocol_error_payload(result)
        return {
            "outcome": "UNKNOWN",
            "source": "gateway_probe_candidate",
            "reason": (
                "MCP isError is a tool result, not Gateway authorization evidence."
            ),
            "candidate": {
                "kind": "tool_result",
                "phase": "tools_call",
                "tool": tool,
                "gateway_identifier": None,
                "protocol_error": protocol_error,
            },
        }
    return {
        "outcome": "ALLOW",
        "source": "gateway_authorization",
        "status": 200,
    }


def probe_gateway_authorization(
    *, tool: str, endpoint: str, arguments: dict
) -> dict:
    """Run the async MCP client outside Starlette's active event loop."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(
            asyncio.run,
            _probe_gateway_authorization(
                tool=tool,
                endpoint=endpoint,
                arguments=arguments,
            ),
        ).result()


def _bound_operation(tool_name: str, target_prefixes: tuple[str, ...]) -> str | None:
    """공용 Gateway tool 이름을 선택 자산의 operation으로 안전하게 분리해요."""
    for prefix in target_prefixes:
        if tool_name.startswith(prefix) and len(tool_name) > len(prefix):
            return tool_name[len(prefix):]
    return None


def _belongs_to_target(tool, *, target_prefixes: tuple[str, ...], **_kwargs) -> bool:
    return _bound_operation(
        str(getattr(tool, "tool_name", "")),
        target_prefixes,
    ) is not None


def _belongs_to_binding(
    tool,
    *,
    allowed_tool_names: tuple[str, ...],
    target_prefixes: tuple[str, ...],
    **_kwargs,
) -> bool:
    tool_name = str(getattr(tool, "tool_name", ""))
    if allowed_tool_names:
        return tool_name in allowed_tool_names
    # Empty operations means all approved operations. Legacy bindings do not
    # carry the operation universe, so retain target scoping for that case.
    return _bound_operation(tool_name, target_prefixes) is not None


def _binding_tool_prefixes(binding: dict) -> tuple[str, ...]:
    """Read current bindings and derive prefixes for legacy three-field bindings."""
    prefixes = binding.get("tool_prefixes")
    if prefixes:
        return tuple(prefixes)
    target_name = binding["name"]
    targets = dict.fromkeys((target_name, target_name.replace("-", "_")))
    return tuple(f"{target}___" for target in targets)


def _binding_allowed_tool_names(binding: dict) -> tuple[str, ...]:
    names = binding.get("allowed_tool_names")
    if names:
        return tuple(names)
    operations = binding.get("operations") or ()
    target_name = binding["name"]
    targets = dict.fromkeys((target_name, target_name.replace("-", "_")))
    return tuple(
        f"{target}___{operation}"
        for operation in operations
        for target in targets
    )


def load_skills() -> list:
    """선택한 skill을 AgentSkills 플러그인으로 로드해요.

    Agent(plugins=[...])에 넣어야 해요 — tools=에 넣으면 로드되지 않아요.
    Skill.from_content로 본문 문자열을 직접 파싱해서 파일시스템/샌드박스 의존이
    없어요. 개별 skill 파싱 실패는 그 skill만 건너뛰고 나머지는 살려요.
    """
    if not _SKILL_DIRS:
        return []
    from strands.vended_plugins.skills import AgentSkills, Skill

    loaded = []
    for rel in _SKILL_DIRS:
        try:
            md = (_ROOT / "skills" / rel / "SKILL.md").read_text(encoding="utf-8")
            loaded.append(Skill.from_content(md))
        except Exception as e:
            log.warning("skill 로드 실패(%s): %s — 이 skill만 제외하고 기동해요.", rel, e)
    return [AgentSkills(skills=loaded)] if loaded else []


def load_mcp_clients(handle_slot: dict | None = None) -> list:
    """endpoint별 MCPClient를 만들어요. 토큰은 각 요청의 auth_flow에서 읽어요.

    `handle_slot` 은 이 agent 가 소유하는 delegation handle 칸이에요. 안 주면 handle 이
    안 실려서 Gateway 가 전부 거부해요 — 호출부가 반드시 넘겨요.

    continue_on_error=True가 중요해요. MCPClient 생성자는 연결하지 않고
    실제 연결은 Agent(tools=[...]) 생성 시 일어나요 — 이 플래그가 없으면
    Gateway 장애 하나가 agent 컨테이너 부팅 전체를 막아요.
    """
    if not _MCP_ASSETS:
        return []
    clients = []
    for binding in _MCP_ASSETS:
        url = binding["endpoint"]
        target_prefixes = _binding_tool_prefixes(binding)
        allowed_tool_names = _binding_allowed_tool_names(binding)
        slot = handle_slot if handle_slot is not None else new_call_handle_slot()

        def _factory(url=url, slot=slot):
            return streamablehttp_client(url, auth=_BearerAuth(slot))
        clients.append(MCPClient(
            _factory,
            tool_filters={
                "allowed": [
                    lambda tool, allowed_tool_names=allowed_tool_names,
                    target_prefixes=target_prefixes, **kwargs:
                    _belongs_to_binding(
                        tool,
                        allowed_tool_names=allowed_tool_names,
                        target_prefixes=target_prefixes,
                        **kwargs,
                    )
                ]
            },
            continue_on_error=True,
        ))
    return clients
'''.replace(
        "@@ERROR_HELPERS@@", error_helpers_py
    ).replace(
        "@@MCP_COMMENTS@@", mcp_lines + "\n" if mcp_lines else ""
    ).replace("@@MCP_ASSETS@@", mcp_assets).replace(
        "@@SKILL_COMMENTS@@", skill_comments + "\n" if skill_comments else ""
    ).replace("@@SKILL_DIRS@@", skill_dirs_lit)

    handler_py = '''"""A2A JSON-RPC 2.0 핸들러 — message/send 처리 (sample/chatbot-agent 방식).
서버(main.py)와 분리해 소켓 없이 단독 테스트할 수 있어요. run_agent를 주입하면
그걸 쓰고(테스트), 없으면 agent.core.run_agent를 호출해요.
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as distribution_version
import json
import logging
import math
import pathlib
import re
from contextlib import contextmanager

_AGENT_CARD_PATH = pathlib.Path(__file__).resolve().parent.parent / "agent-card.json"

# Initializr에서 선택한 도구 이름 — 배포 검증(agora/selfcheck)이 실제 등록과 비교해요.
# MCP와 skill을 나눠 둬요: 노출 이름 규칙이 달라서 한 목록으로 합치면 판정이 틀려요.
_EXPECTED_MCP: list[tuple[str, tuple[str, ...], bool]] = [@@EXPECTED_MCP@@]
_EXPECTED_SKILLS: list[str] = [@@EXPECTED_SKILLS@@]
_EXPECTED_TOOLS: list[str] = [
    name for name, _patterns, _exact in _EXPECTED_MCP
] + _EXPECTED_SKILLS
_EXECUTION_LIMITS = @@EXECUTION_LIMITS@@
_USER_TOKEN_ERROR_CODE = -32004
_USER_TOKEN_ERROR_MESSAGE = (
    "사용자 인증 토큰을 전달하거나 갱신한 뒤 다시 시도해 주세요."
)
_USER_TOKEN_ERROR_REASONS = frozenset({
    "user_token_not_forwarded",
    "user_token_expired",
    "user_token_invalid",
})
_PROBEABLE_READ_TOOLS: frozenset[str] = frozenset([@@PROBEABLE_READ_TOOLS@@])
_AUTHORIZATION_PROBE_TOOLS: frozenset[str] = frozenset(
    [@@AUTHORIZATION_PROBE_TOOLS@@]
)
_GATEWAY_ENDPOINTS: frozenset[str] = frozenset([@@GATEWAY_ENDPOINTS@@])
_DECLARED_MEMORY_MODE = @@DECLARED_MEMORY_MODE@@
@@ERROR_HELPERS@@

log = logging.getLogger(__name__)

_USAGE_COUNT_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_input_tokens",
    "cache_write_input_tokens",
    "cycle_count",
)
_USAGE_DURATION_FIELDS = ("latency_ms",)
_TOOL_METRIC_COUNT_FIELDS = ("call_count", "success_count", "error_count")
_TOOL_METRIC_DURATION_FIELDS = ("total_time",)
_TOOL_METRICS_MAX = 50
_TOOL_NAME_MAX_LENGTH = 200
_TOOL_NAME_CHARS = re.compile(r"^[A-Za-z0-9_.:-]+$")
_TOOL_METRICS_UNKNOWN_REASON = "invalid_or_excess_tool_names"


def _non_negative_number(value, *, integer: bool):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    if integer and not isinstance(value, int):
        return None
    return value


def _safe_tool_metric_name(name) -> bool:
    if (
        not isinstance(name, str)
        or len(name) > _TOOL_NAME_MAX_LENGTH
        or name.count("___") != 1
    ):
        return False
    target, operation = name.split("___", 1)
    return bool(
        target
        and operation
        and _TOOL_NAME_CHARS.fullmatch(target)
        and _TOOL_NAME_CHARS.fullmatch(operation)
    )


def _usage_metadata(raw) -> dict | None:
    """Copy only aggregate counters; prompts, arguments, results and memory stay out."""
    if not isinstance(raw, dict):
        return None
    usage = {}
    for field in _USAGE_COUNT_FIELDS:
        value = _non_negative_number(raw.get(field), integer=True)
        if value is not None:
            usage[field] = value
    for field in _USAGE_DURATION_FIELDS:
        value = _non_negative_number(raw.get(field), integer=False)
        if value is not None:
            usage[field] = value
    durations = raw.get("cycle_durations")
    if isinstance(durations, (list, tuple)):
        safe_durations = [
            value
            for item in durations
            if (value := _non_negative_number(item, integer=False)) is not None
        ]
        usage["cycle_durations"] = safe_durations
    raw_tools = raw.get("tool_metrics")
    if isinstance(raw_tools, dict):
        tools = {}
        discarded = 0
        for name, raw_metric in raw_tools.items():
            if (
                not _safe_tool_metric_name(name)
                or not isinstance(raw_metric, dict)
                or len(tools) >= _TOOL_METRICS_MAX
            ):
                discarded += 1
                continue
            metric = {}
            for field in _TOOL_METRIC_COUNT_FIELDS:
                value = _non_negative_number(raw_metric.get(field), integer=True)
                if value is not None:
                    metric[field] = value
            for field in _TOOL_METRIC_DURATION_FIELDS:
                value = _non_negative_number(raw_metric.get(field), integer=False)
                if value is not None:
                    metric[field] = value
            tools[name] = metric
        usage["tool_metrics"] = tools
        if discarded:
            usage.update({
                "tool_metrics_status": "unknown",
                "tool_metrics_reason": _TOOL_METRICS_UNKNOWN_REASON,
                "tool_metrics_discarded_count": discarded,
            })
    return usage or None


def _bedrock_agentcore_version() -> str | None:
    """Observe the distribution installed in this Runtime image."""
    try:
        return distribution_version("bedrock-agentcore")
    except PackageNotFoundError:
        return None


def load_agent_card() -> dict:
    return json.loads(_AGENT_CARD_PATH.read_text(encoding="utf-8"))


def _match_expected(registered: list) -> list:
    """기대 도구 중 등록되지 않은 것을 돌려줘요.

    MCP와 skill의 노출 규칙이 달라서 따로 판정해요:
      - MCP: 선택 operation은 정확한 Gateway tool 이름으로 비교해요. Strands가
        target의 하이픈을 밑줄로 바꾸는 변형도 정확 이름으로 함께 허용해요.
        operation 정보가 없는 legacy binding만 target prefix로 비교해요.
      - skill: AgentSkills가 'skills' 단일 도구로 합쳐 노출해요. 그 하나가 있으면
        선택한 skill들이 로드된 것으로 봐요(개별 이름은 도구로 안 나와요).
    두 목록을 합쳐서 판정하면 skill이 붙었을 때 MCP 누락이 가려져요(실측 결함).
    """
    names = list(registered)
    missing = []
    for want, patterns, exact in _EXPECTED_MCP:
        matched = (
            any(r in patterns for r in names)
            if exact
            else any(any(r.startswith(prefix) for prefix in patterns) for r in names)
        )
        if not matched:
            missing.append(want)
    if _EXPECTED_SKILLS and "skills" not in names:
        missing.extend(_EXPECTED_SKILLS)
    return missing


def _observe_conversation_manager(agent) -> dict | None:
    """Return the conversation manager configuration applied by Strands."""
    manager = getattr(agent, "conversation_manager", None)
    if manager is None:
        return None
    name = type(manager).__name__
    parameter_names = {
        "SlidingWindowConversationManager": ("window_size",),
        "SummarizingConversationManager": (
            "summary_ratio",
            "preserve_recent_messages",
        ),
        "NullConversationManager": (),
    }.get(name, ())
    return {
        "name": name,
        "parameters": {
            parameter: getattr(manager, parameter)
            for parameter in parameter_names
            if hasattr(manager, parameter)
        },
    }


def _observe_model(agent) -> dict:
    """Read the model selected by the live Strands Agent."""
    config = getattr(getattr(agent, "model", None), "config", None)
    model_id = config.get("model_id") if isinstance(config, dict) else None
    if not isinstance(model_id, str) or not model_id:
        return {"status": "unknown", "model_id": None}
    return {"status": "ok", "model_id": model_id}


def _observe_memory(agent) -> dict:
    """Read runtime Memory attachment and retrieval namespaces."""
    # Strands 1.51 stores Agent(session_manager=...) only on this private field
    # and exposes no public accessor. The real-Agent contract test must fail if
    # a future supported SDK moves it.
    manager = getattr(agent, "_session_manager", None)
    config = getattr(manager, "config", None)
    runtime_status = getattr(agent, "_agora_memory_status", None)
    retrieval_config = getattr(config, "retrieval_config", None)
    status = (
        runtime_status.get("status")
        if isinstance(runtime_status, dict)
        else "ok"
    )
    reason = (
        str(runtime_status.get("reason") or "")
        if isinstance(runtime_status, dict)
        else ""
    )
    retrieval_namespaces = (
        sorted(
            namespace
            for namespace in retrieval_config
            if isinstance(namespace, str) and namespace.strip()
        )
        if isinstance(retrieval_config, dict)
        else []
    )
    retrieval_configuration_observed = (
        isinstance(retrieval_config, dict)
        and bool(retrieval_namespaces)
        and len(retrieval_namespaces) == len(retrieval_config)
    )
    if _DECLARED_MEMORY_MODE == "MANAGED" and manager is None:
        status = "degraded"
        reason = reason or (
            "MANAGED Memory session manager를 runtime에서 관측할 수 없어요."
        )
        configuration_status = "unknown"
        mode = None
        retrieval_namespaces = []
    elif _DECLARED_MEMORY_MODE == "MANAGED":
        configuration_status = (
            "ok" if retrieval_configuration_observed else "unknown"
        )
        mode = "MANAGED"
    elif manager is not None:
        status = "degraded"
        reason = reason or (
            "DISABLED Memory 선언과 달리 session manager가 부착돼 있어요."
        )
        configuration_status = (
            "ok" if retrieval_configuration_observed else "unknown"
        )
        mode = "MANAGED"
    else:
        configuration_status = "ok"
        mode = "DISABLED"
        retrieval_namespaces = []
    return {
        "status": status,
        "reason": reason,
        "configuration_status": configuration_status,
        "mode": mode,
        "runtime_retrieval_namespaces": retrieval_namespaces,
        "session_manager_attached": manager is not None,
        "session_manager_type": type(manager).__name__ if manager else None,
        "memory_id_present": bool(getattr(config, "memory_id", "")),
    }


def _observe_conversation_integrity(agent) -> dict:
    """Read session-scoped conversation continuity observations."""
    observed = getattr(agent, "_agora_conversation_integrity", None)
    if isinstance(observed, dict):
        return dict(observed)
    return {
        "status": "unknown",
        "reason": "conversation message baseline has not been observed",
        "previous_message_count": None,
        "current_message_count": None,
    }


@contextmanager
def _request_agent(*, session_id: str, actor_id: str):
    try:
        from agent.core import use_agent
    except ImportError:
        try:
            from agent.core import build_agent
        except ImportError:
            from agent.core import agent
            yield agent
            return
        yield build_agent(session_id=session_id, actor_id=actor_id)
        return
    with use_agent(session_id=session_id, actor_id=actor_id) as agent:
        yield agent


def _selfcheck(*, session_id: str = "", actor_id: str = "") -> dict:
    """등록된 도구를 관측해 보고해요. 기대값과 판정은 verifier가 소유해요."""
    bedrock_agentcore_version = _bedrock_agentcore_version()
    try:
        with _request_agent(session_id=session_id, actor_id=actor_id) as agent:
            model = _observe_model(agent)
            conversation_manager = _observe_conversation_manager(agent)
            memory = _observe_memory(agent)
            conversation_integrity = _observe_conversation_integrity(agent)
            registered = sorted(agent.tool_registry.get_all_tools_config().keys())
    except Exception as e:
        return {
            "tools": [],
            "bedrock_agentcore_version": bedrock_agentcore_version,
            "model": {"status": "unknown", "model_id": None},
            "conversation_manager": None,
            "memory": {
                "status": "degraded",
                "reason": f"agent initialization failed: {type(e).__name__}: {e}",
                "configuration_status": "unknown",
                "mode": None,
                "runtime_retrieval_namespaces": [],
                "session_manager_attached": False,
                "session_manager_type": None,
                "memory_id_present": False,
            },
            "conversation_integrity": {
                "status": "unknown",
                "reason": (
                    "conversation integrity was not observed because "
                    f"agent initialization failed: {type(e).__name__}: {e}"
                ),
                "previous_message_count": None,
                "current_message_count": None,
            },
            "limits": _EXECUTION_LIMITS,
            "errors": [f"agent 초기화 실패: {type(e).__name__}: {e}"],
        }
    return {
        "tools": registered,
        "bedrock_agentcore_version": bedrock_agentcore_version,
        "model": model,
        "conversation_manager": conversation_manager,
        "memory": memory,
        "conversation_integrity": conversation_integrity,
        "limits": _EXECUTION_LIMITS,
        "errors": [],
    }


def _tool_call(
    tool: str,
    arguments: dict,
    *,
    session_id: str,
    actor_id: str,
) -> dict:
    """Call one registered tool directly, without asking the model to choose it."""
    with _request_agent(session_id=session_id, actor_id=actor_id) as agent:
        if tool not in _PROBEABLE_READ_TOOLS:
            raise ValueError(f"READ probe로 허용되지 않은 도구: {tool}")
        registered = agent.tool_registry.get_all_tools_config()
        if tool not in registered:
            raise ValueError(f"등록되지 않은 도구: {tool}")
        result = getattr(agent.tool, tool)(
            record_direct_tool_call=False,
            **arguments,
        )
        return _tool_probe_payload(result, tool=tool)


def _authorization_probe(tool: str, endpoint: str, arguments: dict) -> dict:
    """Call one baked safe negative-control operation through the real Gateway."""
    if tool not in _AUTHORIZATION_PROBE_TOOLS:
        raise ValueError(f"인가 probe로 승인되지 않은 도구: {tool}")
    if endpoint not in _GATEWAY_ENDPOINTS:
        raise ValueError("인가 probe endpoint가 승인된 Gateway와 일치하지 않아요")
    from agent.tools import probe_gateway_authorization

    return probe_gateway_authorization(
        tool=tool,
        endpoint=endpoint,
        arguments=arguments,
    )


def handle_jsonrpc(
    body: dict,
    *,
    run=None,
    trace_headers: dict[str, str] | None = None,
    session_id: str = "",
    actor_id: str = "",
) -> dict:
    req_id = body.get("id")
    if body.get("jsonrpc") != "2.0":
        return _error(req_id, -32600, "jsonrpc 필드가 \\'2.0\\'이어야 해요")
    method = body.get("method", "")
    params = body.get("params", {})
    agora_context = (
        params.get("agoraContext")
        if isinstance(params, dict)
        else None
    )
    payload_actor_id = (
        agora_context.get("actorId")
        if isinstance(agora_context, dict)
        else None
    )
    # Both values come from the same authenticated portal caller. Payload avoids
    # per-Runtime header allowlist wiring; the header remains a legacy fallback.
    if isinstance(payload_actor_id, str) and payload_actor_id:
        actor_id = payload_actor_id
    # IA-61 의 `callHandle` 은 여기서 읽지 않아요 — 요청 경계인 `main.py` 의 `_a2a` 가
    # contextvar 로 심어요. 이 모듈은 소켓 없이 단독 테스트되므로(`run=` 주입) `agent.tools`
    # 를 import 하면 테스트에서 `ModuleNotFoundError` 가 나요.
    if method == "agora/selfcheck":
        # 배포 검증 전용 — 등록 도구를 결정적으로 보고해요(모델 호출 없음).
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": _selfcheck(session_id=session_id, actor_id=actor_id),
        }
    if method == "agora/tool-call":
        tool = params.get("tool") if isinstance(params, dict) else None
        arguments = params.get("arguments") if isinstance(params, dict) else None
        if not isinstance(tool, str) or not isinstance(arguments, dict):
            return _error(req_id, -32602, "tool과 arguments가 필요해요")
        try:
            result = _tool_call(
                tool,
                arguments,
                session_id=session_id,
                actor_id=actor_id,
            )
        except ValueError as e:
            return _error(req_id, -32602, str(e))
        except Exception as e:
            return _error(
                req_id,
                -32603,
                f"도구 호출 실패: {_exception_reason(e)}",
            )
        return {"jsonrpc": "2.0", "id": req_id, "result": result}
    if method == "agora/authorization-probe":
        tool = params.get("tool") if isinstance(params, dict) else None
        endpoint = params.get("endpoint") if isinstance(params, dict) else None
        arguments = params.get("arguments") if isinstance(params, dict) else None
        if (
            not isinstance(tool, str)
            or not isinstance(endpoint, str)
            or not isinstance(arguments, dict)
        ):
            return _error(req_id, -32602, "tool, endpoint, arguments가 필요해요")
        try:
            result = _authorization_probe(tool, endpoint, arguments)
        except ValueError as e:
            return _error(req_id, -32602, str(e))
        except Exception as e:
            return _error(
                req_id,
                -32603,
                f"Gateway 인가 probe 실패: {_exception_reason(e)}",
            )
        return {"jsonrpc": "2.0", "id": req_id, "result": result}
    if method == "message/send":
        text = _extract_text(params)
        injected = run is not None
        if run is None:
            from agent.core import run_agent as run
        try:
            # `run_agent` 는 두 인자를 **항상** 요구해요. 예전에는 둘 다 비면 `run(text)` 로
            # 불렀는데, 그러면 AgentCore 세션 헤더가 없는 **로컬 실행이 늘 TypeError** 로
            # 죽었어요(2026-08-30 실측). 빈 문자열도 그대로 넘겨요 — Memory 사용 안 함 경로는
            # 이 값을 쓰지 않고, MANAGED 경로는 빈 키 하나로 모여요.
            #
            # 짧은 형태는 **주입된 run 에만** 남겨요(테스트 대역이 이 인자를 안 받을 수 있어요).
            reply = (
                run(text)
                if injected and not (session_id or actor_id)
                else run(text, session_id=session_id, actor_id=actor_id)
            )
        except Exception as e:
            user_token_reason = getattr(
                e,
                "_agora_user_token_reason",
                None,
            )
            if user_token_reason in _USER_TOKEN_ERROR_REASONS:
                log.error("message/send failed: %s", user_token_reason)
                response = _error(
                    req_id,
                    _USER_TOKEN_ERROR_CODE,
                    _USER_TOKEN_ERROR_MESSAGE,
                )
                response["error"]["data"] = {
                    "reason": user_token_reason,
                }
            else:
                reason = _exception_reason(e)
                log.error("message/send failed: %s", reason)
                response = _error(
                    req_id,
                    -32603,
                    f"모델 호출 실패: {reason}",
                )
            usage = _usage_metadata(getattr(e, "_agora_usage", None))
            if usage is not None:
                response["error"].setdefault("data", {})["metadata"] = {
                    "agora": {"usage": usage}
                }
            return response
        metadata = None
        if isinstance(reply, dict):
            text_reply = str(reply.get("text", ""))
            usage = _usage_metadata(reply.get("usage"))
        else:
            text_reply = str(reply)
            usage = _usage_metadata(getattr(reply, "usage", None))
        if usage is not None:
            metadata = {"agora": {"usage": usage}}
        message = {
            "role": "agent",
            "parts": [{"kind": "text", "text": text_reply}],
        }
        if metadata is not None:
            message["metadata"] = metadata
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "status": {"state": "completed"},
                "message": message,
            },
        }
    return _error(req_id, -32601, f"지원하지 않는 method: {method!r}")


def _extract_text(params: dict) -> str:
    try:
        parts = params["message"]["parts"]
        return " ".join(p.get("text", "") for p in parts if p.get("kind") == "text" or p.get("type") == "text").strip()
    except (KeyError, TypeError):
        return "(내용 없음)"


def _error(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
'''.replace(
        "@@ERROR_HELPERS@@", error_helpers_py
    ).replace(
        # selfcheck는 Gateway가 붙인 `{target}___{op}` tool과 비교하므로 정규화된
        # target name을 기대치로 써야 해요(결함 #10).
        "@@EXPECTED_MCP@@",
        ", ".join(
            repr(expected)
            for m in mcps
            for expected in (
                [
                    (
                        gateway_tool_name(
                            _target_name(m, operation),
                            operation,
                        ),
                        gateway_tool_names(
                            _target_name(m, operation),
                            operation,
                        ),
                        True,
                    )
                    for operation in m.operations
                ]
                or [
                    (
                        _target_name(m),
                        gateway_tool_prefixes(_target_name(m)),
                        False,
                    )
                ]
            )
        ),
    ).replace("@@EXPECTED_SKILLS@@", ", ".join(repr(s.name) for s in skills)).replace(
        "@@PROBEABLE_READ_TOOLS@@",
        ", ".join(
            repr(tool_name)
            for mcp in mcps
            for operation in mcp.operations
            if mcp.operation_sensitivities.get(operation, "").strip().upper()
            == "READ"
            for tool_name in gateway_tool_names(
                _target_name(mcp, operation),
                operation,
            )
        ),
    ).replace(
        "@@AUTHORIZATION_PROBE_TOOLS@@",
        ", ".join(
            repr(tool_name)
            for mcp in mcps
            for operation in mcp.authorization_probe_operations
            for tool_name in gateway_tool_names(
                _target_name(mcp, operation),
                operation,
            )
        ),
    ).replace(
        "@@GATEWAY_ENDPOINTS@@",
        ", ".join(
            repr(endpoint)
            for endpoint in dict.fromkeys(
                mcp.endpoint for mcp in mcps if mcp.endpoint
            )
        ),
    ).replace(
        "@@DECLARED_MEMORY_MODE@@", repr(memory["mode"])
    ).replace("@@EXECUTION_LIMITS@@", repr(execution_limits))

    limits_py = '''"""Agent 실행의 tool cycle 상한을 강제해요."""
from strands.hooks import HookProvider
from strands.hooks.events import AfterToolsEvent, BeforeInvocationEvent

_COUNTER_KEY = "agora_iteration_count"


class IterationLimitHook(HookProvider):
    def __init__(self, *, max_iterations: int):
        if max_iterations < 1:
            raise ValueError("max_iterations must be positive")
        self.max_iterations = max_iterations

    def register_hooks(self, registry, **_kwargs) -> None:
        registry.add_callback(BeforeInvocationEvent, self._before_invocation)
        registry.add_callback(AfterToolsEvent, self._after_tools)

    def _before_invocation(self, event: BeforeInvocationEvent) -> None:
        event.invocation_state[_COUNTER_KEY] = 0

    def _after_tools(self, event: AfterToolsEvent) -> None:
        # One iteration is one completed tool batch. max_iterations=1 stops
        # after the first batch, before the model interprets its tool results.
        count = int(event.invocation_state.get(_COUNTER_KEY, 0)) + 1
        event.invocation_state[_COUNTER_KEY] = count
        if count >= self.max_iterations:
            unit = "tool batch" if self.max_iterations == 1 else "tool batches"
            event.end_turn = (
                "Agent stopped because it reached the configured "
                f"max_iterations limit ({self.max_iterations} {unit})."
            )
'''

    # A2A 0.3 스키마: skill 항목마다 id·name·description·tags 필수(등재 거부 방지).
    # protocolVersion도 0.3.0이어야 registry가 수용해요(실측 2026-07-24).
    agent_card = {
        "name": spec.name,
        "description": spec.description,
        "version": "1.0.0",
        "protocolVersion": "0.3.0",
        "skills": [
            {
                "id": t.name,
                "name": t.name,
                "description": t.description or f"{t.name} 도구",
                "tags": [t.kind],
            }
            for t in spec.tools
        ],
    }
    # mcpAssets.name은 카탈로그에 등록된 MCP 이름과 정확히 같아야 해요 — 배포가 이 이름으로
    # 승인된 최신 MCP record를 해석(record.name == name)해 실제 Gateway endpoint를 주입해요.
    # assetId를 고정하지 않아 MCP 재등록(새 record)에도 견고해요(name 기반 resolution).
    # endpoint는 그 MCP의 Gateway 주소이고 operations는 선택된 operation 목록이에요.
    agora_policy = {
        "version": 2,
        "memory": memory,
        "builtinTools": [],
        # IH-101 re-enable after compatible extras: "builtinTools": builtin_tools,
        "mcpAssets": [
            {
                "name": tool.name,
                "endpoint": tool.endpoint,
                "operations": list(tool.operations),
            }
            for tool in mcps
        ],
    }
    if conversation_manager:
        manager_name = conversation_manager[0]
        agora_policy["conversationManager"] = {
            "name": manager_name,
            "parameters": (
                {"window_size": truncation["window_size"]}
                if manager_name == "SlidingWindowConversationManager"
                else {}
            ),
        }

    memory_readme = (
        """
## 기억(memory)은 로컬에서 동작하지 않아요 — 배포하면 돼요

기억을 `AWS 관리형`으로 골랐어요. **로컬에서도 대화는 정상으로 돼요** — 다만 기억만
빠진 채로 돌아요. 첫 요청 때 로그에 이렇게 남아요:

```
WARNING  memory skipped: 로컬 실행에는 memory 를 쓰지 않아요 (빠진 값: AGORA_MEMORY_ID).
         기억은 배포한 agent 에서만 동작해요 — 쓰기 MCP 도구와 같아요.
```

그래서 로컬에서 시험할 수 있는 것과 못 하는 것이 이렇게 갈려요.

| 로컬에서 | 배포하면 |
| --- | --- |
| 프롬프트·모델·조회(READ) 도구 ✅ | 전부 ✅ |
| 기억(memory) ❌ | ✅ |
| 쓰기(CREATE·UPDATE·DELETE) 도구 ❌ | ✅ (승인 후) |

기억과 쓰기 도구는 **같은 이유로** 로컬에서 빠져요 — 둘 다 Agora가 통제하는 배포 환경의
자원이 필요해요. 시험하려면 카탈로그에 등록해 배포한 뒤 Playground에서 부르세요.

막히는 이유는 **두 개**고, 하나를 우회해도 다른 하나가 남아요.

1. `AGORA_MEMORY_ID` — Agora가 **배포할 때** 만들어 주입하는 AgentCore Memory 리소스
   ID라 로컬에는 없어요. 그래서 `.env.example`에도 이 키를 두지 않았어요 — 채워서 될
   값이 아니거든요.
2. 세션 ID — `session_id`는 AgentCore Runtime이 붙이는
   `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` 헤더에서만 와요. 로컬 A2A
   클라이언트에는 그 헤더가 없어요.

로컬에는 `bedrock-agentcore:CreateEvent`·`ListEvents`·`RetrieveMemoryRecords` IAM 권한도
없어요. 배포하면 세 가지가 모두 채워져서 그대로 동작해요.

로컬에서 기억까지 쓰는 방법은 없어요. 억지로 `AGORA_MEMORY_ID`를 채워도 세션 ID가 없어서
같은 자리에서 막혀요.

## Memory compatibility
Managed Memory supports newly provisioned Agora Memory resources only. Guardrail
redaction and legacy Memory migration are rejected because those SDK paths require
`DeleteEvent`, which the Runtime permission boundary deliberately denies.
"""
        if managed_memory
        else ""
    )

    readme = f'''# {spec.name}

Agent Initializr로 생성한 Strands 에이전트예요 (A2A 프로토콜, 포트 9000).

## 구조
- `main.py` — AgentCore A2A 진입점(codezip entryPoint). 포트 9000, JSON-RPC 서버.
- `agent/core.py` — 모델·프롬프트·도구 배선. `run_agent(text)`.
- `agent/handler.py` — A2A JSON-RPC `message/send` 핸들러.
- `agent/tools.py` — skill·MCP 로더.
- `system_prompt.md` — 페르소나. 자유롭게 수정하세요.

## 로컬 실행
```bash
pip install -r requirements.txt
python main.py   # 0.0.0.0:9000
```

## 로컬 MCP 인증

**`.env` 파일이 있나요?** 있으면 dev 크리덴셜이 포함된 ZIP이에요 — 바로 실행하면 MCP가
호출돼요. 없으면 코드 전용 ZIP이니 `cp .env.example .env` 로 만든 뒤 아래를 채우세요.

로컬에서 MCP 도구를 부르려면 값이 **두 개** 필요해요.

| 키 | 하는 일 |
| --- | --- |
| `AGORA_DEV_TOKEN_URL` | 크리덴셜을 Gateway 접근 토큰으로 바꿔요 (1시간) |
| `AGORA_DEV_CALL_HANDLE_URL` | 호출마다 새 호출 handle을 받아요 (최대 15분) |

**handle URL이 비어 있으면 토큰이 있어도 모든 호출이 거부돼요.** Gateway가 호출자의 신원을
handle로만 확인하기 때문이에요. 배포 환경에서는 Agora가 호출할 때 handle을 실어 주지만,
로컬 실행에는 그 경로가 없어서 크리덴셜로 직접 받아요.

크리덴셜은 7일 뒤 만료돼요. 만료되거나 폐기되면 Agent Initializr에서 다시 내려받아
재발급하세요.

`COGNITO_*` 는 별도로 발급받은 Gateway 인증 정보를 쓰는 대안 경로예요. 이 경로도 handle은
따로 필요해서, 소유자 격리가 걸린 도구는 dev 크리덴셜 쪽을 쓰는 게 맞아요.

## 배포
Agora 카탈로그 > 등록 > "agent 소스 배포"로 이 폴더를 업로드하면
AgentCore Runtime(serverProtocol=A2A)에 배포돼요.
{memory_readme}
'''

    # 패키지 import 시 로컬 .env를 os.environ에 주입해요. agent.tools가 모듈 로드 시점에
    # os.environ을 읽으므로 그 전에(패키지 import 시) 실행돼야 해서 __init__에 둬요.
    #
    # 배포 런타임에서는 `AGORA_DEV_*` 를 **건너뛰어요**. 예전 주석은 "배포엔 .env가 없어
    # no-op" 이라고 단정했는데, 그 근거를 실제로 확인해 보니 이유가 달랐어요.
    #
    # 지금 루트 `.env` 가 컨테이너에 안 들어가는 건 codezip 빌드가 `cp -r ./* package/` 를
    # 쓰고 `./*` 가 dotfile 을 매치하지 않아서예요(`infra/lib/runtime-deploy/build-pipeline.ts`).
    # 설계가 아니라 glob 동작에 기댄 우연이에요. 소스에 Dockerfile 이 있으면 container 분기가
    # 빌드 컨텍스트를 통째로 넣으니 그 우연도 없어요.
    #
    # 그래서 이 층을 둬요. `.env` 가 어떤 경로로든 들어와도 배포 런타임에서는 dev 크리덴셜을
    # 읽지 않아요 — 읽으면 Identity 실패 시 내려받은 사람의 신원으로 호출이 나가요.
    #
    # `_deployed_runtime()` 는 agent.tools 에도 같은 이름으로 있어요. 여기서 import 하지
    # 않고 복제하는 이유: agent.tools 는 모듈 로드 시점에 os.environ 을 읽으니, 주입 **전에**
    # 그걸 import 하면 순서가 뒤집혀요.
    agent_init_py = (
        '"""Agent 패키지 — import 시점에 로컬 .env를 os.environ으로 주입해요.\n'
        "\n"
        "로컬 개발에선 프로젝트 루트의 .env(.env.example 복사본)가 MCP\n"
        "endpoint(AGORA_MCP_ASSETS)·Gateway 토큰(COGNITO_*) 설정을 공급해요. 이미 설정된\n"
        "env는 덮지 않아요 — 배포 주입값이 우선.\n"
        "\n"
        "배포 런타임에서는 AGORA_DEV_* 를 건너뛰어요. dev 크리덴셜은 로컬 실행 전용이고,\n"
        "배포된 agent 가 그걸 쓰면 호출 신원이 크리덴셜 소유자로 바뀌거든요.\n"
        '"""\n'
        "from __future__ import annotations\n\n"
        "import os\n"
        "import pathlib\n\n\n"
        "from agent.logging_config import configure_logging\n\n\n"
        "def _deployed_runtime() -> bool:\n"
        '    """배포 런타임인가요? (agent/tools.py 의 같은 함수와 판정이 같아야 해요.)\n'
        "\n"
        "    AGORA_RUNTIME_ENV 는 배포 API 가 조건 없이 넣는 표식이에요. Identity 두 값은\n"
        "    그 표식이 생기기 전에 배포된 런타임을 위한 폴백이에요.\n"
        '    """\n'
        '    if os.environ.get("AGORA_RUNTIME_ENV") == "deployed":\n'
        "        return True\n"
        "    return bool(\n"
        '        os.environ.get("AGORA_OAUTH_PROVIDER_NAME")\n'
        '        and os.environ.get("AGORA_WORKLOAD_IDENTITY_NAME")\n'
        "    )\n\n\n"
        "def _load_dotenv() -> None:\n"
        '    env_path = pathlib.Path(__file__).resolve().parent.parent / ".env"\n'
        "    if not env_path.exists():\n"
        "        return\n"
        "    deployed = _deployed_runtime()\n"
        '    for raw in env_path.read_text(encoding="utf-8").splitlines():\n'
        "        line = raw.strip()\n"
        '        if not line or line.startswith("#") or "=" not in line:\n'
        "            continue\n"
        '        key, _, value = line.partition("=")\n'
        "        key = key.strip()\n"
        "        if deployed and key.startswith(\"AGORA_DEV_\"):\n"
        "            # 로컬 전용 크리덴셜. 배포 런타임에 주입하면 신원이 바뀌어요.\n"
        "            continue\n"
        "        if key and key not in os.environ:\n"
        "            os.environ[key] = value.strip().strip('\"').strip(\"'\")\n\n\n"
        "_load_dotenv()\n"
        "configure_logging()\n"
    )
    files: dict[str, bytes] = {
        ".gitignore": b"__pycache__/\n*.pyc\n.env\n.venv/\n",
        ".env.example": (
            # 리전은 Agora 가 실제로 배포하는 리전이어야 해요. `scaffold_with_dev_environment`
            # 가 이 파일을 **줄 단위로 복사**하면서 네 키(`AGORA_MCP_ASSETS`·`AGORA_DEV_*`)만
            # 치환하니, 여기 적힌 리전이 그대로 `.env` 에 실려 나가요. 그리고 생성
            # `agent/__init__.py` 가 사용자가 `AWS_REGION` 을 export 하지 않았을 때 이 값을
            # 주입하고, 생성 `core.py` 는 그 변수를 Memory session manager 에 그대로 넘겨요.
            # us-west-2 로 두면 로컬 실행이 Agora 리소스가 없는 리전을 가리켜요.
            f"AWS_REGION=ap-northeast-2\nBEDROCK_MODEL_ID={model_id}\n"
            "\n# MCP endpoint 결속. 배포 땐 Agora가 승인 Registry에서 자동 주입해요.\n"
            "# 로컬에선 여기서 공급하면 코드의 baked 폴백 대신 이 값을 써요(비우면 baked 폴백).\n"
            '# 형식: [{"name":"<Gateway target>","endpoint":"https://.../mcp",'
            '"operations":["<operation>"],"allowed_tool_names":["<target>___<operation>"]}]\n'
            "AGORA_MCP_ASSETS=\n"
            # `AGORA_MEMORY_ID=` 를 **일부러 안 적어요.** 빈 줄로 두면 "채우면 되는 값" 처럼
            # 보이는데, MANAGED memory ZIP 은 로컬에서 **독립된 두 이유로** 못 돌아요:
            #   ① 값이 비면 `RuntimeError("MANAGED memory requires AGORA_MEMORY_ID")`
            #   ② session_id 는 AgentCore 헤더(`X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`)
            #      로만 오니, 일반 로컬 A2A 클라이언트는
            #      `RuntimeError("memory requires verified session_id")` 로 죽어요.
            # 로컬엔 `bedrock-agentcore:CreateEvent/ListEvents/RetrieveMemoryRecords` IAM 도
            # 없어요. 배포 경로는 영향 없어요 — 배포 백엔드가 env 로 직접 주입해요
            # (`runtime/deploy/aws_adapter.py` `env_vars["AGORA_MEMORY_ID"]`).
            # IH-101 re-enable after compatible extras:
            # "\n# [S/runtime] Agent별 CUSTOM 내장 도구 ID. 선택한 도구만 필요해요.\n"
            # "AGORA_BROWSER_ID=\nAGORA_CODE_INTERPRETER_ID=\n"
            "\n# Agora dev 크리덴셜 (기본 7일 만료, 포털에서 rotate/재발급)\n"
            "# 빈 값은 권한을 부여하지 않아요 — 세 줄이 다 채워져야 로컬에서 MCP를 불러요.\n"
            "#   TOKEN_URL       크리덴셜 -> Gateway 접근 토큰 (1시간)\n"
            "#   CALL_HANDLE_URL 호출마다 새 호출 handle (최대 15분, 캐시 금지)\n"
            "# handle 이 없으면 토큰이 있어도 Gateway 가 전부 거부해요 — 호출자 신원을\n"
            "# handle 로만 확인하거든요.\n"
            "AGORA_DEV_TOKEN_URL=\nAGORA_DEV_CALL_HANDLE_URL=\n"
            "AGORA_DEV_CREDENTIAL=\n"
            "\n# 로컬 실행용 MCP Gateway 인증 (배포 환경은 AgentCore Identity가 대신해요)\n"
            "COGNITO_TOKEN_URL=\nCOGNITO_CLIENT_ID=\nCOGNITO_CLIENT_SECRET=\nCOGNITO_SCOPE=\n"
        ).encode(),
        "README.md": readme.encode(),
        "agent-card.json": _json_bytes(agent_card),
        # 1.51은 이 생성물이 의존하는 Strands public/private 계약을 검증한 minor예요.
        # patch 수정은 받고, private field와 callback 계약을 다시 검증하지 않은 다음
        # minor는 설치하지 않도록 상한을 둬요. bedrock-agentcore 1.22.0은 Memory
        # retrieval context를 마지막 user message에 삽입해 Claude assistant-prefill
        # 오류를 피하는 버전이에요. 이후 버전에서 DeleteEvent가 일반 turn으로 이동하는
        # drift를 막기 위해 정확히 고정해요.
        # boto3 1.39.7은 bedrock-agentcore 서비스 모델과
        # get_workload_access_token/get_resource_oauth2_token을 모두 갖춘 최초 버전이에요.
        # mcp 하한은 strands-agents 1.51이 요구하는 >=1.23을 따라요.
        "requirements.txt": (
            b"strands-agents>=1.51,<1.52\nbedrock-agentcore==1.22.0\n"
            + builtin_requirements
            + b"aws-opentelemetry-distro>=0.19,<0.20\n"
            + b"boto3>=1.39.7\nstarlette>=0.40\nuvicorn>=0.30\n"
            b"mcp>=1.23,<2\nhttpx>=0.27\n"
        ),
        "main.py": main_py.encode(),
        "system_prompt.md": spec.system_prompt.encode(),
        "agent/__init__.py": agent_init_py.encode(),
        "agent/callback_handler.py": callback_handler_py.encode(),
        "agent/logging_config.py": logging_config_py.encode(),
        "agent/core.py": core_py.encode(),
        "agent/handler.py": handler_py.encode(),
        "agent/tools.py": tools_py.encode(),
        "agora-policy.json": _json_bytes(agora_policy),
    }
    if max_iterations is not None:
        files["agent/limits.py"] = limits_py.encode()
    # 선택한 skill 본문을 생성물에 구워요. 컨테이너에 Agora API base URL이 없어서
    # 런타임에 받아올 수 없거든요(주입 env는 OAuth 3개뿐).
    from ..catalog.registry.aws_mapping import ensure_frontmatter
    for tool, dirname in zip(skills, skill_dirs):
        md = ensure_frontmatter(
            tool.skill_markdown or "", name=tool.name, description=tool.description)
        files[f"skills/{dirname}/SKILL.md"] = md.encode()
    return files


def scaffold_with_dev_environment(
    files: dict[str, bytes],
    *,
    token_url: str,
    call_handle_url: str,
    credential: str,
    mcp_assets: list[dict],
) -> dict[str, bytes]:
    """Return a zip-only copy with the one-time dev credential baked into .env.

    `call_handle_url` 이 없으면 로컬에서 도구를 부를 수 없어요 — interceptor 가 모든
    `tools/call` 에 handle 을 요구하는데, 로컬엔 그걸 얻을 다른 경로가 없거든요.
    """
    out = dict(files)
    example = files[".env.example"].decode("utf-8")
    values = {
        "AGORA_MCP_ASSETS": json.dumps(
            mcp_assets, ensure_ascii=False, separators=(",", ":")
        ),
        "AGORA_DEV_TOKEN_URL": token_url,
        "AGORA_DEV_CALL_HANDLE_URL": call_handle_url,
        "AGORA_DEV_CREDENTIAL": credential,
    }
    lines = []
    for line in example.splitlines():
        key, separator, _ = line.partition("=")
        if separator and key in values:
            lines.append(f"{key}={values[key]}")
        else:
            lines.append(line)
    out[".env"] = ("\n".join(lines) + "\n").encode("utf-8")
    return out


def scaffold_to_tree(files: dict[str, bytes]) -> list[dict]:
    """평탄 경로 딕셔너리를 EXPLORE용 중첩 FileNode 트리로 바꿔요."""
    root: list[dict] = []
    dirs: dict[str, dict] = {}

    def _ensure_dir(path: str) -> list[dict]:
        if path == "":
            return root
        if path in dirs:
            return dirs[path]["children"]
        parent, _, name = path.rpartition("/")
        node = {"name": name, "path": path, "kind": "dir", "children": []}
        dirs[path] = node
        _ensure_dir(parent).append(node)
        return node["children"]

    for path in sorted(files):
        parent, _, name = path.rpartition("/")
        _ensure_dir(parent).append({
            "name": name, "path": path, "kind": "file",
            "content": files[path].decode("utf-8", errors="replace"),
        })
    return root


def scaffold_to_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(files):
            zf.writestr(path, files[path])
    return buf.getvalue()


def _json_bytes(obj: dict) -> bytes:
    return json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
