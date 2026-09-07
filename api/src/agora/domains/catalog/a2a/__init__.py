"""A2A(Agent-to-Agent) 프로토콜 연동 — agent-card discovery.

MCP가 initialize+tools/list 핸드셰이크로 tool을 가져오듯, A2A는 단순 HTTP GET으로
`.well-known/agent-card.json`을 가져와요(RFC 8615 well-known URI). discovery와 실행 RPC가
분리돼 있어, 카드 GET만으로 "이 도메인이 유효한 A2A agent를 광고하는지" 검증할 수 있어요.
"""

from .protocol import (
    A2AProtocolError,
    AgentCardInfo,
    AgentSkillInfo,
    fetch_agent_card,
)

__all__ = [
    "A2AProtocolError",
    "AgentCardInfo",
    "AgentSkillInfo",
    "fetch_agent_card",
]
