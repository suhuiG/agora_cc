"""소스 없는 자산의 llm-judge 입력 문서 생성 (순수 함수).

connect형 MCP·도메인 연결 agent는 업로드된 소스가 없어요. 그래서 소스 기반 도구
(gitleaks·semgrep·trivy)는 검사 대상이 없고(게이트 not_applicable), llm-judge만
실효 검사가 돼요 — 단 입력이 필요해요.

여기서 registry descriptors를 llm-judge가 읽을 텍스트로 평탄화해요:
  - MCP: server 이름·instructions + tool별 name/description/inputSchema
  - agent: agent card(A2A) 원본 + skill 목록

이 표면이 곧 공격면이에요. MCP tool의 description·inputSchema는 클라이언트 LLM의
컨텍스트에 그대로 주입되므로 tool poisoning·prompt injection의 1차 매체이고, A2A agent
card의 skill 설명도 같은 성질이에요. 그래서 소스가 없더라도 판정할 거리가 있어요.
"""
from __future__ import annotations

import json

# 문서 전체 상한. llm-judge Lambda가 Bedrock 컨텍스트에 싣는 입력이라, tool이 수백 개인
# MCP에서도 토큰이 터지지 않게 자릅니다. 잘렸으면 문서 끝에 표시해요(판정자가 알아야 함).
_MAX_CHARS = 60_000


def _as_dict(value) -> dict:
    """inlineContent(JSON 문자열) 또는 dict를 dict로. 실패하면 빈 dict."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _inline(node: dict, key: str) -> dict:
    """descriptors의 {key: {"inlineContent": "<json>"}} 노드를 dict로 풀어요."""
    raw = (node or {}).get(key)
    if isinstance(raw, dict) and "inlineContent" in raw:
        return _as_dict(raw.get("inlineContent"))
    return _as_dict(raw)


def _mcp_section(node: dict) -> list[str]:
    """MCP descriptors → 판정 대상 텍스트 줄 목록."""
    lines: list[str] = ["## MCP 서버", ""]
    server = node.get("server") if isinstance(node.get("server"), dict) else {}
    lines.append(f"- name: {server.get('name', '') or '(없음)'}")
    lines.append(f"- endpoint: {node.get('endpoint', '') or '(없음)'}")
    if node.get("upstreamEndpoint"):
        lines.append(f"- upstream endpoint: {node['upstreamEndpoint']}")
    instructions = str(server.get("instructions", "") or "")
    if instructions:
        lines += ["", "### server instructions", "", instructions]

    tools = _inline(node, "tools").get("tools")
    lines += ["", "### tools", ""]
    if not isinstance(tools, list) or not tools:
        lines.append("(tool 정보 없음)")
        return lines
    for i, tool in enumerate(tools, 1):
        if not isinstance(tool, dict):
            continue
        lines.append(f"#### {i}. {tool.get('name', '(무명)')}")
        lines.append("")
        lines.append(f"description: {tool.get('description', '') or '(없음)'}")
        schema = tool.get("inputSchema")
        if schema:
            # inputSchema의 필드명·description도 주입 매체라 그대로 실어요.
            lines += ["", "inputSchema:", "```json",
                      json.dumps(schema, ensure_ascii=False, indent=2), "```"]
        lines.append("")
    return lines


def _agent_section(node: dict) -> list[str]:
    """agent descriptors(A2A card) → 판정 대상 텍스트 줄 목록."""
    lines: list[str] = ["## A2A Agent", ""]
    lines.append(f"- endpoint: {node.get('endpoint', '') or '(없음)'}")
    card = _inline(node, "agentCard")
    if not card:
        lines.append("(agent card 없음)")
        return lines
    lines.append(f"- name: {card.get('name', '') or '(없음)'}")
    lines.append(f"- version: {card.get('version', '') or '(없음)'}")
    description = str(card.get("description", "") or "")
    if description:
        lines += ["", "### description", "", description]

    skills = card.get("skills")
    lines += ["", "### skills", ""]
    if not isinstance(skills, list) or not skills:
        lines.append("(skill 정보 없음)")
    else:
        for i, skill in enumerate(skills, 1):
            if not isinstance(skill, dict):
                continue
            lines.append(f"#### {i}. {skill.get('name', '') or skill.get('id', '(무명)')}")
            lines.append("")
            lines.append(f"description: {skill.get('description', '') or '(없음)'}")
            examples = skill.get("examples")
            if isinstance(examples, list) and examples:
                lines.append("examples:")
                lines += [f"  - {e}" for e in examples if isinstance(e, str)]
            lines.append("")

    # 인증·전송 선언도 판정 대상 — 인증 없는 쓰기 도구는 excessive agency 신호예요.
    for key in ("securitySchemes", "security", "capabilities",
                "defaultInputModes", "defaultOutputModes"):
        if card.get(key) is not None:
            lines += [f"### {key}", "", "```json",
                      json.dumps(card[key], ensure_ascii=False, indent=2), "```", ""]
    return lines


def build_judge_document(descriptors: dict, *, name: str = "", asset_type: str = "") -> str:
    """registry descriptors → llm-judge 입력 텍스트. 판정할 내용이 없으면 "".

    빈 문자열을 돌려주면 호출부는 judge 입력을 만들지 않아요(빈 입력으로 "통과" 판정이
    나오는 걸 막기 위해 — 그 경우 게이트는 not_run으로 남아 사람 심사로 가요).
    """
    if not isinstance(descriptors, dict):
        return ""
    header = [f"# 자산: {name or '(무명)'}", "", f"- 자산 타입: {asset_type or '(미상)'}",
              "- 소스 번들: 없음 (endpoint 연결형 자산 — descriptor만 검사 가능)", ""]
    body: list[str] = []
    mcp = descriptors.get("mcp")
    if isinstance(mcp, dict):
        body += _mcp_section(mcp)
    agent = descriptors.get("agent")
    if isinstance(agent, dict):
        body += _agent_section(agent)
    if not body:
        return ""
    doc = "\n".join(header + body).strip()
    if len(doc) > _MAX_CHARS:
        doc = doc[:_MAX_CHARS] + "\n\n[문서가 길어 잘렸어요 — 이후 내용은 검사되지 않았어요]"
    return doc
