"""A2A agent-card 클라이언트 — well-known URL에서 Agent Card를 GET해요.

base URL(예: https://agent.example.com)을 주면 `/.well-known/agent-card.json`을 자동
discover하고, 이미 카드 URL이면 그대로 GET해요. 성공 시 정체성·capabilities·skills를
파싱해 돌려줘요(등록 전 connect 테스트·미리보기용). 네트워크는 http_get으로 주입 가능해요.

MCP와의 차이: MCP는 JSON-RPC 세션(initialize+tools/list)이지만 A2A는 단순 GET. 카드의
skills[]는 호출 가능한 함수(inputSchema)가 아니라 자연어 역량 광고예요.
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field

from ..mcp.registry import validate_endpoint  # http/https·netloc·셸 메타문자 SSRF 방어 재사용

# 표준 well-known 경로(A2A v0.3.0~, RFC 8615). 구 규약 agent.json은 v0.3.0에서 breaking 변경됨.
WELL_KNOWN_PATH = "/.well-known/agent-card.json"
# 악의적/거대 카드가 descriptor에 박제돼 모든 뷰어로 전달되는 걸 막는 상한.
MAX_CARD_BYTES = 1 * 1024 * 1024   # 1 MB
MAX_SKILLS = 200


class A2AProtocolError(Exception):
    """agent-card 조회 실패(도달 불가·비-JSON·필수 필드 누락 등)."""


@dataclass(frozen=True)
class AgentSkillInfo:
    id: str
    name: str
    description: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentCardInfo:
    name: str | None
    description: str | None
    version: str | None
    protocol_version: str | None
    url: str | None
    capabilities: dict = field(default_factory=dict)
    security_schemes: tuple[str, ...] = ()   # 선언된 인증 스킴 이름들(있으면 "이 agent는 인증 요구")
    skills: list[AgentSkillInfo] = field(default_factory=list)
    raw: dict = field(default_factory=dict)  # 원본 카드(무손실 등록용)


def _card_url(endpoint: str) -> str:
    """입력이 이미 카드 URL이면 그대로, base URL이면 well-known 경로를 붙여요."""
    e = endpoint.strip().rstrip("/")
    if e.endswith("agent-card.json") or e.endswith("agent.json"):
        return e
    return e + WELL_KNOWN_PATH


def _default_get(url: str, timeout: float):
    # 일부 호스트가 기본 urllib User-Agent를 봇으로 보고 403을 줘요 — 식별 UA를 명시.
    req = urllib.request.Request(url, method="GET", headers={
        "Accept": "application/json",
        "User-Agent": "agora-registry/0.1 (+agent-card discovery)",
    })
    return urllib.request.urlopen(req, timeout=timeout)


def _read_capped(resp) -> bytes:
    body = resp.read(MAX_CARD_BYTES + 1)
    if len(body) > MAX_CARD_BYTES:
        raise A2AProtocolError(f"agent-card가 너무 커요 (최대 {MAX_CARD_BYTES} bytes).")
    return body


def fetch_agent_card(endpoint: str, *, timeout: float = 10.0,
                     http_get=None) -> AgentCardInfo:
    """endpoint(base URL 또는 카드 URL)에서 Agent Card를 GET·파싱해요. 실패 시 A2AProtocolError.

    http_get 미지정 시 모듈 속성 _default_get을 호출 시점에 참조해요(테스트 monkeypatch 반영).
    """
    if http_get is None:
        http_get = _default_get
    # SSRF·주입 방어를 fetch보다 먼저 (McpEndpointError 던질 수 있음).
    # `require_https=False`: A2A 연결형은 Gateway target을 만들지 않아 AgentCore의
    # `https://.*` 제약을 받지 않아요. 여기서 https 를 강제하면 지금 동작하는 등록이
    # 깨져요 — MCP connect 쪽만 좁히는 게 CA-31 의 범위예요.
    validate_endpoint(endpoint, require_https=False)
    url = _card_url(endpoint)
    try:
        resp = http_get(url, timeout)
    except A2AProtocolError:
        raise
    except Exception as e:  # 도달 불가·타임아웃·DNS 실패 등
        raise A2AProtocolError(f"agent-card를 가져오지 못했어요: {e}") from e

    status = getattr(resp, "status", None) or getattr(resp, "status_code", None)
    if status and status >= 400:
        raise A2AProtocolError(f"agent-card 응답 오류: HTTP {status} ({url})")

    try:
        card = json.loads(_read_capped(resp))
    except A2AProtocolError:
        raise
    except Exception as e:
        raise A2AProtocolError(f"agent-card JSON 파싱 실패: {e}") from e
    if not isinstance(card, dict):
        raise A2AProtocolError("agent-card가 JSON 객체가 아니에요.")

    # 필수 필드 최소 검증 — A2A agent를 스스로 광고하는지(자기 광고) 확인.
    if not card.get("name"):
        raise A2AProtocolError("agent-card에 name이 없어요 (유효한 A2A agent가 아니에요).")

    skills_raw = card.get("skills") or []
    if len(skills_raw) > MAX_SKILLS:
        raise A2AProtocolError(f"skill이 너무 많아요 (최대 {MAX_SKILLS}개).")
    skills = [
        AgentSkillInfo(
            id=str(s.get("id", "")),
            name=str(s.get("name", "")),
            description=str(s.get("description", "")),
            tags=tuple(s.get("tags") or ()),
        )
        for s in skills_raw if isinstance(s, dict)
    ]
    sec = card.get("securitySchemes")
    security_schemes = tuple(sec.keys()) if isinstance(sec, dict) else ()

    return AgentCardInfo(
        name=card.get("name"),
        description=card.get("description"),
        version=card.get("version"),
        protocol_version=card.get("protocolVersion"),
        url=card.get("url"),
        capabilities=card.get("capabilities") or {},
        security_schemes=security_schemes,
        skills=skills,
        raw=card,
    )
