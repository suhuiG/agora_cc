"""AgentCore Gateway lambda 타깃 하네스.

Gateway 계약: tool 이름은 context.client_context.custom["bedrockAgentCoreToolName"]
(형식 "{target}___{tool}"), arguments는 event dict. MCP 서버를 프로세스로 띄우지
않고, 배포된 소스의 tool 콜러블을 직접 호출해요(스파이크로 검증한 방식).
"""
import os

_TOOLS = None


def _set_tools(tools):  # 테스트·부트스트랩 훅
    global _TOOLS
    _TOOLS = tools


def _load_tools():
    """AGORA_MCP_MODULE이 가리키는 MCP 소스에서 tool 콜러블 테이블을 만들어요.
    Task 0 스파이크로 확정한 FastMCP 추출 방식을 여기 적용해요."""
    global _TOOLS
    if _TOOLS is None:
        import importlib
        mod = importlib.import_module(os.environ["AGORA_MCP_MODULE"])
        _TOOLS = _tools_from_fastmcp(mod)
    return _TOOLS


def _tools_from_fastmcp(mod):
    """MCP 서버 인스턴스를 모듈에서 찾아 tool 콜러블 테이블을 만들어요.

    mcp 1.x(FastMCP)와 2.0(MCPServer)을 둘 다 지원해요. 함수명은 하위 호환을 위해
    _tools_from_fastmcp로 유지하지만, 실제로는 _tool_manager 속성 기반 탐색이라
    두 버전 모두 동작해요(2.0의 MCPServer도 _tool_manager._tools를 동일하게 가져요).

    Task 0 스파이크 결과:
    - MCP 서버 인스턴스는 모듈 속성으로 존재해요(예: mod.mcp).
    - 인스턴스는 _tool_manager._tools: dict[name, Tool]을 가져요.
    - Tool.fn은 원본 콜러블(동기 또는 비동기).
    - Tool.is_async(bool) 또는 inspect.iscoroutinefunction으로 비동기 여부 확인.

    구현 전략:
    1. vars(mod).values()를 순회해 MCP 서버 인스턴스를 탐색해요.
       (_tool_manager 속성으로 식별 — FastMCP·MCPServer 공통)
    2. 하드코딩된 mod.mcp 대신 속성 스캔으로 범용성을 확보해요.
    3. 비동기 tool은 asyncio.run()으로 감싸요.
    """
    import inspect
    import asyncio

    # MCP 서버 인스턴스 탐색(FastMCP·MCPServer 공통 — _tool_manager로 식별)
    instance = None
    # 먼저 mod.mcp를 시도(가장 일반적인 이름)
    if hasattr(mod, "mcp") and hasattr(getattr(mod, "mcp"), "_tool_manager"):
        instance = mod.mcp
    else:
        # 속성 스캔으로 폴백 — _tool_manager가 있는 객체를 찾아요
        for val in vars(mod).values():
            if hasattr(val, "_tool_manager"):
                instance = val
                break

    if instance is None:
        raise RuntimeError(
            f"MCP 서버 인스턴스를 {mod.__name__}에서 찾지 못했어요. "
            "모듈에 MCP 서버 인스턴스(mcp = FastMCP(...) 또는 "
            "server = MCPServer(...))가 있는지 확인하세요."
        )

    tools = {}
    for name, tool in instance._tool_manager._tools.items():
        fn = tool.fn
        # is_async 속성 또는 inspect.iscoroutinefunction으로 비동기 여부 판단
        is_async = getattr(tool, "is_async", None)
        if is_async is None:
            is_async = inspect.iscoroutinefunction(fn)

        if is_async:
            # 비동기 tool을 동기 wrapper로 감싸요
            def _make_sync(f):
                def _sync(**kwargs):
                    return asyncio.run(f(**kwargs))
                return _sync
            tools[name] = _make_sync(fn)
        else:
            tools[name] = fn

    return tools


def lambda_handler(event, context):
    extended = context.client_context.custom["bedrockAgentCoreToolName"]
    tool_name = extended.split("___")[-1]
    tools = _load_tools()
    fn = tools.get(tool_name)
    if fn is None:
        raise ValueError(f"unknown tool: {tool_name}")
    return fn(**(event or {}))
