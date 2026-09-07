"""MCP 서버 규약 정적 검증 (배포 전 반려 게이트).

AgentCore Runtime MCP 계약(port 8000·/mcp·/ping·ARM64)을 소스가 만족하는지
휴리스틱으로 확인해요. 완전 보장은 배포 시 CREATE_FAILED가 최종 방어선이고,
여기선 흔한 실수(진입점 없음·MCP 아님)를 빨리 반려해요.
"""
from __future__ import annotations

from .models import SpecCheckError

# 파이썬 진입점 후보(루트 또는 하위). 하나라도 있으면 진입점 존재로 봐요.
_ENTRY_HINTS = ("pyproject.toml", "server.py", "__main__.py", "app.py", "main.py")
# MCP/FastMCP 사용 신호. pyproject 또는 파이썬 파일 내용에서 찾아요.
# mcp 2.0의 MCPServer(mcp.server.mcpserver)도 명시적으로 잡아요. 지금은 b"mcp.server"가
# mcp.server.mcpserver를 우연히 substring 매칭해 통과하지만, 의도를 명시하려고 추가해요.
# (본문은 .lower() 후 비교하므로 실제 매칭은 b"mcpserver"가 담당 — b"MCPServer"는 명시용.)
_MCP_HINTS = (b"fastmcp", b"mcp.server", b"mcpserver", b"MCPServer",
              b'"mcp"', b"'mcp'", b"mcp>=", b"mcp ")


def decide_build_type(paths: tuple[str, ...]) -> str:
    """루트 Dockerfile 있으면 container, 없으면 codezip."""
    return "container" if "Dockerfile" in paths else "codezip"


def check_agent_spec(manifest) -> None:
    """agent 소스 규약 정적 검증. 미충족 시 SpecCheckError.

    필수 조건:
    1. agent-card.json 또는 legacy agent.json 카드 검증 — AgentBinding.validate에 위임(재구현 금지).
    2. Python 진입점(main.py)이 루트에 있어야 해요 (codezip 실행 규약) — 여기 고유 검사.
    """
    from ...catalog.sourcestore.bindings import get_binding
    from ...catalog.sourcestore.models import SourceStoreError

    try:
        get_binding("agent").validate(manifest)  # 카드 파일 검증 재사용
    except SourceStoreError as e:
        raise SpecCheckError(str(e)) from e

    if "main.py" not in manifest.paths():
        raise SpecCheckError(
            "agent requires 'main.py' at the root as the codezip entrypoint."
        )


def check_mcp_spec(paths: tuple[str, ...], read_file) -> None:
    """규약 미충족이면 SpecCheckError. read_file(path) -> bytes 주입."""
    if not any(p == h or p.endswith("/" + h) for p in paths for h in _ENTRY_HINTS):
        raise SpecCheckError(
            "MCP 진입점을 찾지 못했어요 (pyproject.toml 또는 server.py 등이 필요해요)."
        )
    # MCP 사용 신호를 pyproject·파이썬 파일에서 탐색.
    scanned = [p for p in paths if p.endswith(".py") or p.endswith("pyproject.toml")]
    for p in scanned:
        try:
            body = read_file(p).lower()
        except Exception:
            continue
        if any(h in body for h in _MCP_HINTS):
            return
    raise SpecCheckError(
        "MCP/FastMCP 사용 신호를 찾지 못했어요 (mcp 의존성·FastMCP import 필요)."
    )
