"""AwsRegistryAdapter용 anti-corruption 변환 — Agora 도메인 ↔ AWS Agent Registry.

순수 함수만 모아요(부수효과·네트워크 없음). 실제 API의 문자열/스키마 규칙을
여기 한 곳에 격리해, 8/6 네임스페이스 이전이나 스키마 변동 시 이 파일만 고치면 돼요.

실측(2026-07-09) 규칙:
- descriptorType 문자열: MCP · A2A · CUSTOM · AGENT_SKILLS (Agora enum 값과 다름).
- recordId는 응답에 없고 recordArn 꼬리에서 파싱.
- AGENT_SKILLS.skillMd.inlineContent는 '---' frontmatter로 시작해야 함.
- mcp.server.inlineContent = {name, description, version} JSON, schemaVersion 2025-12-11.
"""

from __future__ import annotations

import json
import re

from .models import DescriptorType, RecordStatus, RegistryRecord, SearchHit

# MCP **server** descriptor 스키마 버전(server.json 검증용). Registry 지원 집합:
# 2025-12-11 · 2025-10-17 · 2025-10-11 · 2025-09-29 · 2025-09-16 · 2025-07-09 (공식문서 실측 2026-08-14).
_MCP_SCHEMA_VERSION = "2025-12-11"
# MCP **tools** descriptor 스키마 버전. **server와 지원 집합이 달라요** — tools는
# 2025-11-25 · 2025-06-18 · 2025-03-26 · 2024-11-05 만 허용해요(공식문서). server용
# '2025-12-11'을 tools에 쓰면 CreateRegistryRecord가 "Schema version '...' is not supported
# for descriptor type 'mcp'"로 거부해요(2026-08-14 실측·프로브 확인). Gateway supportedVersions와
# 겹치는 안정 버전 2025-06-18을 써요.
_MCP_TOOLS_SCHEMA_VERSION = "2025-06-18"
# A2A descriptor 레벨 schemaVersion(실측 2026-07-10). agentCard 본문의 protocolVersion과 별개.
_A2A_SCHEMA_VERSION = "0.3"
# agentCard 본문에 들어가는 A2A 프로토콜 버전(실측: 없으면 "does not match any supported version").
_A2A_PROTOCOL_VERSION = "0.3.0"
# Agora가 관리하는 Agent Runtime 공개 메타데이터 extension. A2A 0.3의
# capabilities.extensions[].params는 임의 JSON 객체를 허용해 Registry 안에서 함께 영속화돼요.
_AGORA_AGENT_EXTENSION_URI = "https://agora.example.com/extensions/runtime/v1"
_AGORA_AGENT_METADATA_FIELDS = (
    "sourcePrefix",
    "runtimeArn",
    "agoraDependencies",
    "workloadIdentity",
    "model",
    "bedrockModelId",
    "allowedTools",
    "executionBinding",
)

# Agora enum → AWS 문자열
_TO_AWS: dict[DescriptorType, str] = {
    DescriptorType.SKILL: "AGENT_SKILLS",
    DescriptorType.MCP: "MCP",
    DescriptorType.AGENT: "A2A",
    DescriptorType.APP: "CUSTOM",
    DescriptorType.MODEL: "CUSTOM",
    DescriptorType.CUSTOM: "CUSTOM",
}
# AWS 문자열 → Agora enum (CUSTOM은 APP/MODEL 구분 불가 → CUSTOM으로 역매핑)
_FROM_AWS: dict[str, DescriptorType] = {
    "AGENT_SKILLS": DescriptorType.SKILL,
    "MCP": DescriptorType.MCP,
    "A2A": DescriptorType.AGENT,
    "CUSTOM": DescriptorType.CUSTOM,
}

# Registry 서비스 네임스페이스(CA-05/ADR-0015). 구=discriminated union descriptor +
# descriptorType(A2A/AGENT_SKILLS/MCP/CUSTOM). 신=flat keyed descriptor + top-level
# recordType(AGENT/MCP/SKILL/CUSTOM, 의미 분류). shared.config와 값이 같아야 해요.
_OLD_NAMESPACE = "bedrock-agentcore"
_NEW_NAMESPACE = "agent-registry"

# Agora enum → 신 네임스페이스 recordType(의미 분류). APP/MODEL/CUSTOM → CUSTOM.
_TO_RECORD_TYPE: dict[DescriptorType, str] = {
    DescriptorType.SKILL: "SKILL",
    DescriptorType.MCP: "MCP",
    DescriptorType.AGENT: "AGENT",
    DescriptorType.APP: "CUSTOM",
    DescriptorType.MODEL: "CUSTOM",
    DescriptorType.CUSTOM: "CUSTOM",
}
# 신 recordType → Agora enum (CUSTOM은 APP/MODEL 구분 불가 → CUSTOM).
_FROM_RECORD_TYPE: dict[str, DescriptorType] = {
    "SKILL": DescriptorType.SKILL,
    "MCP": DescriptorType.MCP,
    "AGENT": DescriptorType.AGENT,
    "CUSTOM": DescriptorType.CUSTOM,
}


def to_registry_descriptor_type(dt: DescriptorType) -> str:
    return _TO_AWS[dt]


def from_registry_descriptor_type(s: str) -> DescriptorType:
    return _FROM_AWS[s]


def to_record_type(dt: DescriptorType) -> str:
    """Agora enum → 신 네임스페이스 top-level recordType 문자열(CA-05)."""
    return _TO_RECORD_TYPE[dt]


def dedup_name(display_name: str) -> str:
    """신 네임스페이스의 필수 `name`(레지스트리 내 dedup 키) 생성(CA-05).

    표시명을 slug화(소문자 영숫자·하이픈, ≤64)해요. 같은 레지스트리에 같은 표시명이
    둘 있으면 dedup 키가 충돌해 API가 거부하지만, 실사용상 표시명은 대개 고유해요.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (display_name or "").lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:64].strip("-")
    return slug or "record"


def record_id_from_arn(record_arn: str) -> str:
    """arn:...:registry/{rid}/record/{recordId} → recordId."""
    return record_arn.split("/")[-1]


def _iso(ts) -> str:
    """AWS 타임스탬프를 ISO8601 문자열로 정규화해요.

    boto3는 timestamp를 datetime으로 파싱해요. datetime이면 .isoformat(),
    이미 문자열이면 그대로, 없으면 빈 문자열을 돌려줘요.
    """
    if ts is None:
        return ""
    if isinstance(ts, str):
        return ts
    isof = getattr(ts, "isoformat", None)
    return isof() if callable(isof) else str(ts)


def _skill_name_slug(name: str) -> str:
    """SKILL.md frontmatter name 규칙에 맞게 slug화해요(실측 2026-07-09).

    Registry 규칙: 1-64 소문자 영숫자·하이픈, 시작/끝 하이픈 금지, 연속 하이픈 금지.
    표시명(대문자·공백·특수문자 가능)을 그대로 넣으면 ValidationException이 나요.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)  # 연속 하이픈 축약
    slug = slug[:64].strip("-")          # 64자 제한 후 잘린 끝 하이픈 정리
    return slug or "skill"               # 전부 제거되면 안전한 기본값


_DESC_MAX = 1024


def _skill_description(description: str, *, name: str) -> str:
    """SKILL.md frontmatter description 규칙에 맞게 정규화해요(실측 2026-07-09).

    Registry 규칙: description 필수(비어있으면 거부), 1024자 이하.
    finalize 경로는 description=""로 오는 경우가 있어, 비면 name 기반 기본값을 넣어요.
    개행은 frontmatter 한 줄을 깨뜨리니 공백으로 접어요.
    """
    desc = " ".join((description or "").split())  # 개행·중복공백 정리
    if not desc:
        desc = f"{name} skill"  # 안전한 비어있지 않은 기본값
    return desc[:_DESC_MAX]


def _mcp_server_name(name: str) -> str:
    """MCP server.json name 규칙에 맞게 역DNS+슬래시 형식으로 정규화해요(실측 2026-07-09).

    Registry schema '2025-12-11' 규칙: name은 'namespace/name' 형식이어야 함
    (예: 'io.aws/knowledge'). 슬래시 없는 평문('awsknowledgemcp')이나 점만 있는
    형식('io.aws.knowledge')은 ValidationException으로 거부돼요(실측).
    표시명을 slug화한 뒤, 슬래시가 없으면 안전한 기본 네임스페이스('agora')를 붙여요.
    """
    slug = _skill_name_slug(name)  # 소문자 영숫자·하이픈 규칙 재사용
    if "/" in name:
        # 저자가 이미 namespace/name을 준 경우: 양쪽을 각각 slug화해 보존.
        ns, _, rest = name.partition("/")
        ns_slug = _skill_name_slug(ns) or "agora"
        name_slug = _skill_name_slug(rest) or slug
        return f"{ns_slug}/{name_slug}"
    return f"agora/{slug}"


def _yaml_dq(value: str) -> str:
    """YAML double-quoted scalar로 안전하게 인용해요.

    JSON 문자열은 YAML double-quoted scalar의 부분집합(따옴표·백슬래시·제어문자
    이스케이프 동일)이라, json.dumps로 인용하면 콜론·대괄호·따옴표가 든 값도
    frontmatter를 깨지 않아요. (실측: unquoted description에 ':'가 있으면 거부됨.)
    """
    return json.dumps(value, ensure_ascii=False)


def ensure_frontmatter(markdown: str, *, name: str, description: str) -> str:
    """SKILL.md가 '---' frontmatter로 시작하도록 보장해요(Registry 검증 규칙).

    frontmatter를 새로 붙일 때 name은 slug화(_skill_name_slug), description은
    정규화(_skill_description: 비면 기본값·1024자 제한) 후 둘 다 YAML double-quote로
    인용(_yaml_dq)해서 콜론·대괄호·따옴표가 있어도 frontmatter가 깨지지 않게 해요.
    이미 frontmatter가 있으면 저자 책임이라 그대로 둬요(유효성은 저자가 보장).
    """
    if markdown.lstrip().startswith("---"):
        return markdown
    name_val = _yaml_dq(_skill_name_slug(name))
    desc_val = _yaml_dq(_skill_description(description, name=name))
    fm = f"---\nname: {name_val}\ndescription: {desc_val}\n---\n\n"
    return fm + markdown


def _normalize_a2a_card(card: dict, *, name: str, description: str,
                        endpoint: str | None) -> dict:
    """A2A Agent Card를 registry 스키마 0.3 필수필드로 보강해요.

    사용자가 올린 카드가 최소 필드만 가져도(name·skills 정도) 등재가 통과하도록,
    누락 필드를 기본값으로 채워요(실측 2026-07-17: url·defaultInputModes·
    defaultOutputModes·protocolVersion·capabilities·skills가 필수). 사용자가 준 값은
    보존하고 없는 것만 채워요.
    """
    if not isinstance(card, dict):
        card = {}
    card.setdefault("name", _mcp_server_name(name))
    card.setdefault("description", _skill_description(description, name=name))
    card.setdefault("version", "1.0.0")
    # protocolVersion이 없거나 지원 목록 밖이면 지원 버전으로 강제(스키마 거부 방지).
    card["protocolVersion"] = _A2A_PROTOCOL_VERSION
    # url은 A2A invoke endpoint. 없으면 endpoint(있으면)로, 그것도 없으면 placeholder.
    if not card.get("url"):
        card["url"] = endpoint or "https://example.invalid/a2a"
    if not isinstance(card.get("capabilities"), dict):
        card["capabilities"] = {}
    if not card.get("defaultInputModes"):
        card["defaultInputModes"] = ["text/plain"]
    if not card.get("defaultOutputModes"):
        card["defaultOutputModes"] = ["text/plain"]
    skills = card.get("skills")
    if not isinstance(skills, list) or not skills:
        card["skills"] = [{
            # Registry schema 0.3 requires one skill; keep its name neutral so an
            # empty authored list does not surface the agent itself as a skill.
            "id": "default", "name": "General",
            "description": _skill_description(description, name=name),
            "tags": ["general"],
        }]
    else:
        # skills가 있어도 항목별 필수필드(description·tags)가 빠지면 스키마 0.3이 거부해요.
        # 사용자가 준 값은 보존하고 빠진 것만 채워요(실측 2026-07-24: {id,name}만 준 항목 거부).
        for s in skills:
            if not isinstance(s, dict):
                continue
            s.setdefault("id", s.get("name") or "default")
            s.setdefault("name", s.get("id") or name)
            if not s.get("description"):
                s["description"] = _skill_description(description, name=s.get("name") or name)
            if not s.get("tags"):
                s["tags"] = ["general"]
    return card


def _agent_metadata(node: dict) -> dict:
    """Agora Agent descriptor에서 Registry에 영속화할 공개 메타데이터만 골라요."""
    if not isinstance(node, dict):
        return {}
    metadata = {}
    for field in _AGORA_AGENT_METADATA_FIELDS:
        value = node.get(field)
        if field in {"sourcePrefix", "runtimeArn", "model", "bedrockModelId"}:
            if isinstance(value, str) and value.strip():
                metadata[field] = value
        elif isinstance(value, dict):
            metadata[field] = value
        elif field == "allowedTools" and isinstance(value, list):
            metadata[field] = [
                item for item in value if isinstance(item, str) and item
            ]
    return metadata


def _set_agora_agent_extension(card: dict, node: dict) -> None:
    """A2A 카드의 Agora extension을 서버가 계산한 값으로 교체해요.

    외부 Agent Card가 같은 URI를 넣어 runtime/workload binding을 위조하지 못하도록
    authored Agora extension은 항상 제거하고 descriptor의 신뢰된 필드만 다시 넣어요.
    다른 A2A extension은 그대로 보존합니다.
    """
    capabilities = card["capabilities"]
    extensions = capabilities.get("extensions")
    if not isinstance(extensions, list):
        extensions = []
    extensions = [
        extension
        for extension in extensions
        if not (
            isinstance(extension, dict)
            and extension.get("uri") == _AGORA_AGENT_EXTENSION_URI
        )
    ]
    metadata = _agent_metadata(node)
    if metadata:
        extensions.append(
            {
                "uri": _AGORA_AGENT_EXTENSION_URI,
                "description": "Agora-managed Agent Runtime binding.",
                "required": False,
                "params": metadata,
            }
        )
    if extensions:
        capabilities["extensions"] = extensions
    else:
        capabilities.pop("extensions", None)


def _card_from_registry_descriptors(descriptors: dict) -> tuple[dict, str]:
    """AWS A2A descriptor에서 파싱한 Agent Card와 원문을 돌려줘요(old·new 둘 다 수용).

    - 신(agent-registry): `a2aAgentCard.data`
    - 구(bedrock-agentcore): `a2a.agentCard.inlineContent`
    """
    inline = None
    if isinstance(descriptors, dict):
        new_node = descriptors.get("a2aAgentCard")
        if isinstance(new_node, dict):
            inline = new_node.get("data")
        if inline is None:
            a2a = descriptors.get("a2a")
            card_node = a2a.get("agentCard") if isinstance(a2a, dict) else None
            inline = card_node.get("inlineContent") if isinstance(card_node, dict) else None
    if isinstance(inline, str):
        try:
            card = json.loads(inline)
        except (TypeError, ValueError):
            return {}, inline
        return (card if isinstance(card, dict) else {}), inline
    if isinstance(inline, dict):
        return inline, json.dumps(inline, ensure_ascii=False)
    return {}, ""


def _metadata_from_agent_card(card: dict) -> dict:
    """A2A 카드에서 Agora가 관리하는 공개 Runtime 메타데이터를 복원해요."""
    capabilities = card.get("capabilities") if isinstance(card, dict) else None
    extensions = capabilities.get("extensions") if isinstance(capabilities, dict) else None
    if not isinstance(extensions, list):
        return {}
    for extension in extensions:
        if not (
            isinstance(extension, dict)
            and extension.get("uri") == _AGORA_AGENT_EXTENSION_URI
        ):
            continue
        params = extension.get("params")
        return _agent_metadata(params) if isinstance(params, dict) else {}
    return {}


def _endpoint_from_server_json(inline: str | None) -> str:
    """MCP server.json 문자열의 remotes[].url에서 endpoint를 뽑아요. 없으면 ""."""
    try:
        payload = json.loads(inline) if isinstance(inline, str) else None
    except (TypeError, ValueError):
        payload = None
    remotes = payload.get("remotes") if isinstance(payload, dict) else None
    if isinstance(remotes, list):
        for remote in remotes:
            url = remote.get("url") if isinstance(remote, dict) else None
            if isinstance(url, str) and url.strip():
                return url.strip()
    return ""


def _mcp_domain_from_new(mcp_server: dict) -> dict:
    """신 `mcpServer`(data/additionalData.tools) → 도메인 `mcp`(server/tools.inlineContent).

    도메인 MCP shape는 구 AWS shape와 키가 같아요(`mcp.server.inlineContent`·
    `mcp.tools.inlineContent`) — 다운스트림 리더(endpoint_of_descriptors·identity
    _mcp_operation_ids·governance)가 그대로 동작해요. endpoint는 server.json remotes에서 복원.
    """
    if not isinstance(mcp_server, dict):
        return {"mcp": {}}
    server_inline = mcp_server.get("data")
    mcp: dict = {}
    if isinstance(server_inline, str):
        mcp["server"] = {"inlineContent": server_inline}
        endpoint = _endpoint_from_server_json(server_inline)
        if endpoint:
            mcp["endpoint"] = endpoint
    tools = mcp_server.get("additionalData", {}).get("tools") \
        if isinstance(mcp_server.get("additionalData"), dict) else None
    tools_data = tools.get("data") if isinstance(tools, dict) else None
    if isinstance(tools_data, str):
        mcp["tools"] = {"inlineContent": tools_data}
    return {"mcp": mcp}


def _skill_domain_from_new(agent_skills: dict) -> dict:
    """신 `agentSkillsDefinition`(additionalData.skillMd.data) → 도메인 `skill.markdown`."""
    add = agent_skills.get("additionalData") if isinstance(agent_skills, dict) else None
    skill_md = add.get("skillMd") if isinstance(add, dict) else None
    data = skill_md.get("data") if isinstance(skill_md, dict) else None
    return {"skill": {"markdown": data}} if isinstance(data, str) else {"skill": {}}


def from_registry_descriptors(dt: DescriptorType, descriptors: dict) -> dict:
    """AWS descriptors를 Agora 도메인 descriptor 모양으로 역매핑해요(old·new 둘 다 수용, CA-05).

    이 함수가 유일한 reverse 진입점(to_record)이라, 여기서 신 flat-keyed shape를 도메인
    shape로 정규화하면 Aux 없는(이관된) 레코드도 다운스트림 리더가 그대로 읽어요 —
    누출 read 지점을 개별 수정하지 않아도 돼요. 구 shape는 그대로 통과(기존 동작 불변).
    """
    if not isinstance(descriptors, dict):
        return descriptors

    if dt is DescriptorType.AGENT:
        card, inline = _card_from_registry_descriptors(descriptors)
        agent: dict = {}
        if inline:
            agent["agentCard"] = {"inlineContent": inline}
        endpoint = card.get("url") if isinstance(card, dict) else None
        if isinstance(endpoint, str) and endpoint.strip():
            agent["endpoint"] = endpoint
        agent.update(_metadata_from_agent_card(card))
        return {"agent": agent}

    # 신 네임스페이스 shape면 도메인 shape로 정규화. 구/도메인 shape는 그대로 통과.
    if dt is DescriptorType.MCP and "mcpServer" in descriptors:
        return _mcp_domain_from_new(descriptors["mcpServer"])
    if dt is DescriptorType.SKILL and "agentSkillsDefinition" in descriptors:
        return _skill_domain_from_new(descriptors["agentSkillsDefinition"])
    custom = descriptors.get("custom")
    if isinstance(custom, dict) and "data" in custom and "inlineContent" not in custom:
        return {"custom": {"inlineContent": custom["data"]}}
    return descriptors


def _has_mcp_tools(inline: str) -> bool:
    """MCP tools inlineContent에 등재할 tool이 1개 이상 있는지 판정해요(결함 #8).

    계약(codex 리뷰 반영): 문자열을 JSON 파싱해 tools 배열이 비어있지 않을 때만 True.
    tools 키가 없거나 배열이 비었으면(또는 파싱 실패/예상외 구조면) "tool 없음"으로
    간주해 False를 돌려줘요. 이렇게 해서 '{"tools": []}'처럼 비어있지 않은 문자열이지만
    tool이 0개인 경우도 mcp.tools 자리에서 생략돼 하위호환이 지켜져요.
    """
    try:
        parsed = json.loads(inline)
    except (TypeError, ValueError):
        return False
    tools = parsed.get("tools") if isinstance(parsed, dict) else None
    return isinstance(tools, list) and len(tools) > 0


def _mcp_server_inline(node: dict, *, name: str, description: str) -> str:
    """MCP server.json inlineContent를 조립해요(네임스페이스 무관 — 본문은 동일)."""
    server = node.get("server") or {}
    inline = server.get("inlineContent")
    if inline:
        return inline
    # MCP server.json 형식으로 조립해요(실측 2026-07-09, schema '2025-12-11'):
    #  - name은 역DNS+슬래시(namespace/name)여야 함 → _mcp_server_name 정규화.
    #  - 원격(연결형) MCP는 remotes:[{type,url}]가 있어야 스키마 통과(없으면 거부).
    server_obj = {
        "name": _mcp_server_name(name),
        "description": _skill_description(description, name=name),
        "version": "1.0.0",
    }
    endpoint = node.get("endpoint")
    if endpoint:
        server_obj["remotes"] = [{"type": "streamable-http", "url": endpoint}]
    return json.dumps(server_obj)


def _mcp_tools_inline(node: dict) -> str | None:
    """등재할 tool이 1개 이상일 때만 tools inlineContent 문자열을 돌려줘요(결함 #8).

    아래는 생략(None): inlineContent가 문자열이 아니거나 strip 후 비었거나, JSON 파싱
    실패거나, tools 키가 없거나 tools 배열이 비었을 때. '{"tools": []}'처럼 비어있지
    않은 문자열도 tool 0개면 생략해 하위호환을 지켜요.
    """
    tools_node = node.get("tools") if isinstance(node, dict) else None
    tools_inline = tools_node.get("inlineContent") if isinstance(tools_node, dict) else None
    if isinstance(tools_inline, str) and tools_inline.strip() and _has_mcp_tools(tools_inline):
        return tools_inline
    return None


def _a2a_card_inline(node: dict, *, name: str, description: str) -> str:
    """A2A agentCard inlineContent를 조립·정규화해요(네임스페이스 무관 — 본문은 동일)."""
    card_node = node.get("agentCard") if isinstance(node, dict) else None
    inline = card_node.get("inlineContent") if isinstance(card_node, dict) else None
    endpoint = node.get("endpoint") if isinstance(node, dict) else None
    # 사용자가 올린 카드(inline)든 자동조립이든 동일하게 스키마 0.3 필수필드를 채워요.
    # 업로드 카드가 최소 필드(name·skills 정도)만 있어도 등재가 통과하도록 정규화해요
    # (실측 2026-07-17: url·defaultInput/OutputModes·protocolVersion 누락 시 스키마 거부).
    card: dict = {}
    if inline:
        try:
            card = json.loads(inline)
        except (ValueError, TypeError):
            card = {}
    card = _normalize_a2a_card(card, name=name, description=description, endpoint=endpoint)
    _set_agora_agent_extension(card, node)
    return json.dumps(card, ensure_ascii=False)


def to_registry_descriptors(
    dt: DescriptorType, descriptors: dict, *, name: str, description: str,
    namespace: str = _OLD_NAMESPACE,
) -> dict:
    """Agora descriptors → AWS descriptors.

    네임스페이스에 따라 wrapper가 달라요(CA-05/ADR-0015). inlineContent 본문(server.json·
    agentCard·markdown)은 동일하고, 구 네임스페이스는 discriminated union(`mcp.server` 등),
    신 네임스페이스는 flat keyed(`mcpServer.data` 등 — `inlineContent`→`data`,
    `schemaVersion`/`protocolVersion`→`dataSchemaVersion`, tools/skillMd는 `additionalData`)예요.
    """
    new = namespace == _NEW_NAMESPACE
    d = descriptors if isinstance(descriptors, dict) else {}

    if dt is DescriptorType.SKILL:
        node = d.get("skill", {})
        markdown = node.get("markdown") or f"# {name}\n\n{description}\n"
        content = ensure_frontmatter(markdown, name=name, description=description)
        if new:
            # skillMd는 검색용 메타(마크다운)만 저장. skill definition(상위 data)은 미사용.
            return {"agentSkillsDefinition": {"additionalData": {"skillMd": {"data": content}}}}
        return {"agentSkills": {"skillMd": {"inlineContent": content}}}

    if dt is DescriptorType.MCP:
        node = d.get("mcp", {})
        inline = _mcp_server_inline(node, name=name, description=description)
        tools_inline = _mcp_tools_inline(node)
        if new:
            mcp: dict = {"data": inline, "dataSchemaVersion": _MCP_SCHEMA_VERSION}
            if tools_inline is not None:
                mcp["additionalData"] = {"tools": {
                    "data": tools_inline, "dataSchemaVersion": _MCP_TOOLS_SCHEMA_VERSION,
                }}
            return {"mcpServer": mcp}
        mcp = {"server": {"schemaVersion": _MCP_SCHEMA_VERSION, "inlineContent": inline}}
        if tools_inline is not None:
            mcp["tools"] = {
                "protocolVersion": _MCP_TOOLS_SCHEMA_VERSION, "inlineContent": tools_inline,
            }
        return {"mcp": mcp}

    if dt is DescriptorType.AGENT:
        node = d.get("agent", {})
        inline = _a2a_card_inline(node, name=name, description=description)
        if new:
            return {"a2aAgentCard": {"data": inline, "dataSchemaVersion": _A2A_SCHEMA_VERSION}}
        return {"a2a": {"agentCard": {
            "schemaVersion": _A2A_SCHEMA_VERSION, "inlineContent": inline,
        }}}

    # APP/MODEL/CUSTOM → custom (원형 보존)
    if new:
        return {"custom": {"data": json.dumps(descriptors)}}
    return {"custom": {"inlineContent": json.dumps(descriptors)}}


def wrap_optional_values(value, shape):
    """Create용 descriptors dict를 Update API 스키마에 맞게 감싸요.

    UpdateRegistryRecord는 partial update를 표현하려고 갱신 가능한 필드마다
    `{"optionalValue": ...}` 래퍼를 두는데, 중첩 레벨마다 반복돼요(실측 2026-07-27):
        create: {"a2a": {"agentCard": {...}}}
        update: {"optionalValue": {"a2a": {"optionalValue": {"agentCard": {...}}}}}
    래퍼 위치를 손으로 적으면 스키마가 바뀔 때 조용히 어긋나니, botocore shape를
    보고 optionalValue 멤버가 있는 자리에서만 감싸요.

    shape는 botocore StructureShape (또는 None이면 원본 그대로).
    """
    if shape is None or getattr(shape, "type_name", None) != "structure":
        return value
    members = shape.members
    # 이 레벨이 래퍼면(optionalValue 단독) 한 겹 감싸고 안쪽으로 내려가요.
    if list(members.keys()) == ["optionalValue"]:
        return {"optionalValue": wrap_optional_values(value, members["optionalValue"])}
    if not isinstance(value, dict):
        return value
    return {
        k: wrap_optional_values(v, members[k]) if k in members else v
        for k, v in value.items()
    }


def _core(aws_item: dict, registry_id: str) -> dict:
    """GetRegistryRecord / Search 응답의 공통 코어 필드를 뽑아요(old·new 둘 다 수용).

    - 타입: 구 `descriptorType`(A2A/AGENT_SKILLS/…) | 신 `recordType`(AGENT/SKILL/…).
    - 표시명: 신은 `displayName`(사람용) + `name`(dedup 키) 분리 → 표시명 우선.
      구는 `name`이 곧 표시명.
    - 버전: Search는 'version', Get/List는 'recordVersion' — 둘 다 수용.
    """
    if "descriptorType" in aws_item:
        descriptor_type = from_registry_descriptor_type(aws_item["descriptorType"])
    else:
        descriptor_type = _FROM_RECORD_TYPE.get(
            aws_item.get("recordType", ""), DescriptorType.CUSTOM)
    return {
        "record_id": aws_item["recordId"],
        "registry_id": registry_id,
        "name": aws_item.get("displayName") or aws_item["name"],
        "descriptor_type": descriptor_type,
        "version": aws_item.get("recordVersion") or aws_item.get("version") or "",
        "status": RecordStatus(aws_item["status"]),
        "description": aws_item.get("description", ""),
        # 카탈로그 목록의 "최근 수정일자" 정렬 기준. GetRegistryRecord 응답의
        # updatedAt(boto3가 datetime으로 파싱)을 ISO8601 문자열로 정규화해요.
        "updated_at": _iso(aws_item.get("updatedAt")),
    }


def to_record(aws_item: dict, *, registry_id: str) -> RegistryRecord:
    """AWS 레코드 dict → RegistryRecord(확장메타는 빈 기본값; Aux 병합은 어댑터가)."""
    c = _core(aws_item, registry_id)
    descriptors = from_registry_descriptors(
        c["descriptor_type"], aws_item.get("descriptors", {}) or {}
    )
    return RegistryRecord(
        **c, descriptors=descriptors,
    )


def to_hit(aws_item: dict, *, registry_id: str) -> SearchHit:
    c = _core(aws_item, registry_id)
    return SearchHit(
        record_id=c["record_id"], registry_id=c["registry_id"], name=c["name"],
        descriptor_type=c["descriptor_type"], version=c["version"],
        status=c["status"], description=c["description"], score=0.0,
        updated_at=c["updated_at"],  # 목록 정렬용 — 카드 조립 시 get_record 왕복 회피(N+1)
    )
