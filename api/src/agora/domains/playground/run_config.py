"""Playground 실행 구성 — 선언(원장)과 실체(runtime 관측)를 분리해 조립해요.

왜 이 모듈이 있나: Playground 는 "이 agent 가 지금 무슨 모델·도구·기억으로 도는지"를
보여줘야 하는데, 두 축의 출처가 완전히 달라요.

- **선언**: 배포 원장(Registry descriptor). 모델 alias·bedrock 모델 ID, MCP 자산과 승인
  operation, 내장 도구, `memory` 설정(mode·strategies·서버 소유 namespace)이 여기 있어요.
- **실체**: 배포된 runtime 이 `agora/selfcheck` 로 보고하는 모델 ID·도구·conversation
  manager·retrieval namespace와, AWS AgentCore `GetMemory`가 보고하는 전략이에요.

기대값의 소유자도 여기서 갈라요(ADR-0037 §4): 기대 도구 이름은 **선언 원장**에서
`gateway_tool_names` 로 도출하고, 대조 대상은 runtime 이 스스로 보고한 목록이에요. 즉
비교의 양쪽 소유자가 달라요.
"""

from __future__ import annotations

from ...shared.bedrock_models import is_model_metadata
from ...shared.gateway_tools import declared_agent_tools
from ...shared.memory import configured_memory_namespaces

# 관측 상태 어휘. `unavailable`·`unsupported` 는 **통과가 아니에요** — 화면은 이걸
# 그대로 "실제 배선 정보 없음"으로 표시해요(ADR-0037 §3의 unknown 규율).
OBSERVED_OK = "ok"
OBSERVED_NOT_PROBED = "not_probed"
OBSERVED_UNSUPPORTED = "unsupported"
OBSERVED_UNAVAILABLE = "unavailable"
MEMORY_RESOURCE_OK = "ok"
MEMORY_RESOURCE_UNKNOWN = "unknown"
MEMORY_RESOURCE_NOT_APPLICABLE = "not_applicable"


def _agent_node(descriptors: object) -> dict:
    agent = (descriptors or {}).get("agent") if isinstance(descriptors, dict) else None
    return agent if isinstance(agent, dict) else {}


def declared_model(descriptors: object) -> dict | None:
    """원장에 적힌 모델. 현재 모델 계약과 어긋나면 None(= 표시할 선언이 없음)."""
    agent = _agent_node(descriptors)
    alias = agent.get("model")
    bedrock_model_id = agent.get("bedrockModelId")
    if not is_model_metadata(alias, bedrock_model_id):
        return None
    return {"alias": str(alias), "bedrock_model_id": str(bedrock_model_id)}


def declared_tools(descriptors: object) -> list[dict]:
    """선언된 도구(MCP operation + 내장 도구)와 각 도구의 기대 등록 이름.

    `operations` 키가 없는 legacy binding 은 "승인 operation 전체"라는 뜻이라 기대 이름을
    도출할 수 없어요. 그건 `operations_declared=False` 로 표시하고 기대 이름을 비워
    둬요 — 추측한 이름으로 대조하면 거짓 신호가 나거든요.
    """
    return [
        {
            "kind": declaration.kind,
            "asset_id": declaration.asset_id,
            "target_name": declaration.target_name,
            "operation": declaration.operation,
            "label": declaration.label,
            "operations_declared": declaration.operations_declared,
            "expected_tool_names": list(
                declaration.expected_tool_names
            ),
        }
        for declaration in declared_agent_tools(descriptors)
    ]


def declared_memory(descriptors: object) -> dict:
    """선언된 AgentCore Memory 설정. namespace 는 서버가 소유한 값이에요."""
    memory = _agent_node(descriptors).get("memory")
    if not isinstance(memory, dict):
        return {"mode": "", "strategies": [], "namespaces": {},
                "retention_days": None, "memory_id_present": False}
    strategy_configs = memory.get("strategy_configs")
    namespaces = {
        str(strategy): [
            str(namespace)
            for namespace in (config.get("namespaces") or ())
        ]
        for strategy, config in (
            strategy_configs.items() if isinstance(strategy_configs, dict) else ()
        )
        if isinstance(config, dict)
    }
    return {
        "mode": str(memory.get("mode") or ""),
        "strategies": [
            str(strategy) for strategy in (memory.get("strategies") or ())
        ],
        "strategy_configs": (
            strategy_configs if isinstance(strategy_configs, dict) else {}
        ),
        "namespaces": namespaces,
        "retention_days": (
            memory["retention_days"]
            if isinstance(memory.get("retention_days"), int)
            else None
        ),
        # memoryId 자체는 내부 좌표라 값을 내보내지 않고 존재 여부만 알려줘요.
        "memory_id_present": bool(memory.get("memoryId")),
    }


def declared_conversation_manager(descriptors: object) -> dict | None:
    manager = _agent_node(descriptors).get("conversationManager")
    return manager if isinstance(manager, dict) else None


def declared_run_config(descriptors: object) -> dict:
    return {
        "model": declared_model(descriptors),
        "tools": declared_tools(descriptors),
        "memory": declared_memory(descriptors),
        "conversation_manager": declared_conversation_manager(descriptors),
    }


def _unobserved(status: str, reason: str) -> dict:
    return {
        "status": status,
        "reason": reason,
        "model": None,
        "tools": None,
        "conversation_manager": None,
        "memory": None,
        "conversation_integrity": None,
        "memory_resource": None,
        "limits": None,
    }


def logging_outlet() -> dict:
    """Python logging 출구를 판정할 독립 신호가 없으므로 unknown을 유지해요."""
    return {
        "status": "unknown",
        "reason": "이 배포본에 logging 출구가 있는지 관측할 수단이 없어요.",
        "source": "unobserved",
        "as_of": None,
    }


def not_probed(reason: str = "실체 관측을 요청하지 않았어요.") -> dict:
    return _unobserved(OBSERVED_NOT_PROBED, reason)


def unsupported(reason: str) -> dict:
    return _unobserved(OBSERVED_UNSUPPORTED, reason)


def unavailable(reason: str) -> dict:
    return _unobserved(OBSERVED_UNAVAILABLE, reason)


def observed_run_config(selfcheck: object) -> dict:
    """`agora/selfcheck` result 를 관측 축으로 정규화해요.

    selfcheck 가 `tools` 를 배열로 주지 않았으면 관측 실패로 봐요 — 빈 배열과 "형식이
    달라 못 읽음"은 다른 사실이고, 후자를 "도구 0개"로 접으면 거짓 신호가 돼요.
    """
    if not isinstance(selfcheck, dict):
        return unavailable("selfcheck 응답 형식을 읽지 못했어요.")
    tools = selfcheck.get("tools")
    if not isinstance(tools, list):
        return unavailable("selfcheck 가 등록 도구 목록을 주지 않았어요.")
    errors = [
        str(error) for error in (selfcheck.get("errors") or ()) if str(error)
    ]
    if errors:
        return unavailable("; ".join(errors))
    memory = selfcheck.get("memory")
    model = selfcheck.get("model")
    limits = selfcheck.get("limits")
    manager = selfcheck.get("conversation_manager")
    conversation_integrity = selfcheck.get("conversation_integrity")
    return {
        "status": OBSERVED_OK,
        "reason": "",
        "model": model if isinstance(model, dict) else None,
        "tools": [str(tool) for tool in tools],
        "conversation_manager": manager if isinstance(manager, dict) else None,
        "memory": memory if isinstance(memory, dict) else None,
        "conversation_integrity": (
            conversation_integrity
            if isinstance(conversation_integrity, dict)
            else None
        ),
        "memory_resource": None,
        "limits": limits if isinstance(limits, dict) else None,
    }


def reconcile_model(declared: dict | None, observed: dict) -> dict:
    """Compare the Registry-owned model expectation with the live Agent."""
    expected = (
        declared.get("bedrock_model_id")
        if isinstance(declared, dict)
        else None
    )
    model = observed.get("model")
    actual = (
        model.get("model_id")
        if isinstance(model, dict) and model.get("status") == OBSERVED_OK
        else None
    )
    verdict = "unknown"
    if isinstance(expected, str) and isinstance(actual, str):
        verdict = "coherent" if expected == actual else "diverged"
    return {
        "expected_model_id": expected,
        "observed_model_id": actual,
        "verdict": verdict,
    }


def observe_agentcore_memory(client, memory_id: str) -> dict:
    """Observe strategy types from the AWS-owned AgentCore Memory resource.

    AWS GetMemory returns ``memory.strategies[]`` and each MemoryStrategy has a
    required ``type``. This is intentionally independent of Agora's descriptor
    and generated runtime constants.
    """
    try:
        response = client.get_memory(memoryId=memory_id)
    except Exception as error:
        return {
            "status": MEMORY_RESOURCE_UNKNOWN,
            "reason": (
                "AgentCore GetMemory 호출에 실패했어요: "
                f"{type(error).__name__}"
            ),
            "strategies": None,
        }
    memory = response.get("memory") if isinstance(response, dict) else None
    strategies = memory.get("strategies") if isinstance(memory, dict) else None
    if not isinstance(strategies, list):
        return {
            "status": MEMORY_RESOURCE_UNKNOWN,
            "reason": "AgentCore GetMemory 응답에서 strategies를 읽지 못했어요.",
            "strategies": None,
        }
    strategy_types = []
    for strategy in strategies:
        strategy_type = strategy.get("type") if isinstance(strategy, dict) else None
        if not isinstance(strategy_type, str) or not strategy_type:
            return {
                "status": MEMORY_RESOURCE_UNKNOWN,
                "reason": "AgentCore GetMemory strategy type을 읽지 못했어요.",
                "strategies": None,
            }
        strategy_types.append(strategy_type)
    return {
        "status": MEMORY_RESOURCE_OK,
        "reason": "",
        "strategies": sorted(dict.fromkeys(strategy_types)),
    }


def memory_resource_not_applicable() -> dict:
    return {
        "status": MEMORY_RESOURCE_NOT_APPLICABLE,
        "reason": "",
        "strategies": [],
    }


def memory_resource_unknown(reason: str) -> dict:
    return {
        "status": MEMORY_RESOURCE_UNKNOWN,
        "reason": reason,
        "strategies": None,
    }


def _declared_memory_comparison(value: dict) -> dict:
    runtime_retrieval_namespaces = configured_memory_namespaces(value)
    return {
        "mode": value.get("mode"),
        "strategies": sorted(str(item) for item in value.get("strategies") or ()),
        "runtime_retrieval_namespaces": (
            list(runtime_retrieval_namespaces)
            if runtime_retrieval_namespaces is not None
            else None
        ),
    }


def reconcile_memory(declared: dict, observed: dict) -> dict:
    """Compare Registry expectation with runtime mode and AWS-owned strategies."""
    expected = _declared_memory_comparison(declared)
    memory = observed.get("memory")
    resource = observed.get("memory_resource")
    actual_mode = memory.get("mode") if isinstance(memory, dict) else None
    resource_strategies = (
        resource.get("strategies") if isinstance(resource, dict) else None
    )
    runtime_retrieval_namespaces = (
        memory.get("runtime_retrieval_namespaces")
        if isinstance(memory, dict)
        else None
    )
    actual = (
        {
            "mode": actual_mode,
            "strategies": sorted(str(item) for item in resource_strategies),
            "runtime_retrieval_namespaces": sorted(
                str(namespace)
                for namespace in runtime_retrieval_namespaces
            ),
        }
        if (
            isinstance(actual_mode, str)
            and isinstance(resource_strategies, list)
            and isinstance(runtime_retrieval_namespaces, list)
        )
        else None
    )
    verdict = "unknown"
    if isinstance(memory, dict) and memory.get("status") == "degraded":
        verdict = "diverged"
    elif (
        observed.get("status") == OBSERVED_OK
        and isinstance(memory, dict)
        and memory.get("configuration_status") == OBSERVED_OK
        and isinstance(actual_mode, str)
    ):
        if actual_mode != expected["mode"]:
            verdict = "diverged"
        elif expected["mode"] == "MANAGED":
            if (
                expected["runtime_retrieval_namespaces"]
                and isinstance(resource, dict)
                and resource.get("status") == MEMORY_RESOURCE_OK
                and actual is not None
            ):
                verdict = "coherent" if expected == actual else "diverged"
        elif (
            expected["mode"] == "DISABLED"
            and isinstance(resource, dict)
            and resource.get("status") == MEMORY_RESOURCE_NOT_APPLICABLE
            and actual is not None
        ):
            verdict = "coherent" if expected == actual else "diverged"
    return {
        "expected": expected,
        "observed": actual,
        "verdict": verdict,
    }


def reconcile_tools(declared: list[dict], observed: dict) -> list[dict]:
    """선언된 도구마다 runtime 이 실제로 등록했는지 대조해요.

    관측이 없으면 빈 목록을 돌려줘요 — "대조 못 함"을 "일치"로 보이게 하면 안 돼요.
    기대 이름을 도출할 수 없는 legacy binding 은 `observed=None` 으로 남겨요.
    """
    if observed.get("status") != OBSERVED_OK:
        return []
    registered = set(observed.get("tools") or ())
    rows: list[dict] = []
    for tool in declared:
        expected = list(tool.get("expected_tool_names") or ())
        rows.append({
            "kind": tool.get("kind", ""),
            "label": tool.get("label", ""),
            "expected_tool_names": expected,
            "observed": (
                any(name in registered for name in expected)
                if expected
                else None
            ),
        })
    return rows


# ④ 원장이 말하는 도구별 승인 상태 어휘 (ADR-0104, IH-153).
#
# `approved` 만 「지금 부를 수 있음」이에요. 나머지는 화면이 이유를 **말해야** 해요 —
# 미승인 도구는 `tools/list` 에 나타나지만 호출은 거부돼요(ADR-0099 §4.6 «listed but denied»).
#: Gateway 도구 이름의 구분자. 이걸 가진 기대값만 ④ binding 으로 조회할 수 있어요.
_GATEWAY_TOOL_SEPARATOR = "___"
APPROVAL_APPROVED = "approved"
APPROVAL_PENDING = "pending_approval"
APPROVAL_NOT_REQUESTED = "not_requested"
#: ④ binding 이 있을 수 없는 기대값(내장 도구·operation 선언 없는 legacy binding)이에요.
APPROVAL_NOT_APPLICABLE = "not_applicable"
#: 원장을 읽지 못했어요. **「승인됨」이 아니에요** — 화면은 이걸 미관측으로 표시해요.
APPROVAL_UNOBSERVED = "unobserved"


def tool_authorization(
    declared: list[dict],
    approval_states: dict[str, str] | None,
) -> list[dict]:
    """선언된 도구마다 ④ 원장의 승인 상태를 붙여요 (ADR-0104).

    기대값의 소유자를 갈라요(ADR-0037 §4): 등록 여부는 runtime 자기보고가, 승인 여부는
    **identity 원장**이 말해요. 둘을 한 뱃지로 뭉치면 「등록됐으니 쓸 수 있다」로 읽혀요 —
    실제로는 등록돼 있고 호출은 거부되는 상태가 정상이에요(«listed but denied»).

    join 은 **Gateway 도구 이름**으로 해요. `(asset_id, asset_version, operation_id)` 는
    선언 projection 이 `asset_version` 을 버려서 재구성할 수 없고, 도구 이름은 원장의
    `gateway_action` 과 같은 값이라 추측이 안 들어가요.

    `___` 가 없는 기대값은 ④ 판정 대상이 아니에요. 내장 도구는 `expected_tool_names=("browser",)`
    처럼 이름을 갖고(`gateway_tools.py:328-338`), operation 선언 없는 legacy binding 은 빈
    tuple 이에요 — 둘 다 원장에 binding 이 있을 수 없으니 「신청 안 됨」으로 몰면 안 돼요.
    """
    rows: list[dict] = []
    for tool in declared:
        expected = list(tool.get("expected_tool_names") or ())
        keyable = [name for name in expected if _GATEWAY_TOOL_SEPARATOR in name]
        if not keyable:
            status = APPROVAL_NOT_APPLICABLE
        elif approval_states is None:
            status = APPROVAL_UNOBSERVED
        else:
            state = next(
                (
                    approval_states[name]
                    for name in keyable
                    if name in approval_states
                ),
                "",
            )
            status = (
                APPROVAL_APPROVED
                if state == "APPROVED"
                else APPROVAL_PENDING
                if state == "REQUESTED"
                # 행이 없거나 REJECTED 예요. 관리자 승인 큐에 없으니 「승인 대기」가 아니에요.
                else APPROVAL_NOT_REQUESTED
            )
        rows.append({
            "kind": tool.get("kind", ""),
            "label": tool.get("label", ""),
            "expected_tool_names": expected,
            "approval": status,
        })
    return rows


def unexpected_tools(declared: list[dict], observed: dict) -> dict:
    """선언에 없는데 runtime 에 등록된 도구 — 3축 정합의 반대 방향이에요.

    **세 상태를 구분해요.** 빈 목록 하나로 뭉치면 "없음" 과 "판정 불가" 가 합쳐져서,
    선언에 없는 도구가 실제로 올라가 있어도 화면이 조용해요(codex 리뷰 재현, 2026-08-22).

    - `judged` + `names` 비어 있음 → 대조했고 예상 밖 도구가 **없어요**
    - `judged` + `names` 있음 → 예상 밖 도구가 **있어요**
    - `undecidable` → 선언 쪽에 기대 이름을 도출할 수 없는 binding 이 있어 **판정 못 해요**
      (operation 선언이 없는 legacy binding). 이 상태에서 이름을 나열하면 정상 도구를
      예상 밖으로 몰게 돼요.
    - `unobserved` → 실체 관측이 없어 대조 자체를 안 했어요
    """
    if observed.get("status") != OBSERVED_OK:
        return {"status": "unobserved", "names": []}
    if any(not tool.get("expected_tool_names") for tool in declared):
        return {"status": "undecidable", "names": []}
    expected = {
        name
        for tool in declared
        for name in (tool.get("expected_tool_names") or ())
    }
    return {
        "status": "judged",
        "names": sorted(
            name for name in (observed.get("tools") or ()) if name not in expected
        ),
    }
