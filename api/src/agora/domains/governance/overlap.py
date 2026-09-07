"""중복/유사 자산 탐지 — 결정론적 점수 계산 (순수 함수, I/O 없음).

**왜 LLM이 아니라 규칙인가.** 중복검토는 등록 훅에서 매번 돌고(스캔과 달리 사용자를 기다리게
하지 않는 저비용 후처리) 결과가 사람 검토 큐에 그대로 노출돼요. LLM 판정은 같은 입력에 다른
점수를 내서 "어제는 중복 아니었는데 오늘은 중복"이 되고, 그걸 근거로 반려하면 소유자가 반박할
수 없어요. 규칙 기반은 `reasons`에 무엇이 겹쳤는지 그대로 남아서 검토자가 판단을 검증할 수 있어요.

**점수 축.** 자산이 "겹친다"는 건 이름이 비슷한 게 아니라 **같은 일을 한다**는 뜻이에요.
그래서 이름은 보조 축이고, 실제 능력(MCP tool 이름·Agent skill 이름)이 주 축이에요.

  - tool/skill 이름 교집합 (최대 55점) — 능력이 겹치면 실제로 중복이에요
  - 이름 유사도 (최대 25점) — 토큰 Jaccard. `slack-mcp` vs `slack-connector`
  - endpoint 동일 (25점) — 같은 대상을 가리키면 사실상 같은 자산이에요
  - 설명 유사도 (최대 15점) — 가장 약한 신호라 가중치가 낮아요

합계는 100으로 clamp해요. band는 high>=70 / medium 40-69 / low<40 (설계노트 §2-B).
"""
from __future__ import annotations

import json
import re

# 비교에서 제외할 토큰. 자산 이름·설명에 흔해서 남겨두면 무관한 자산끼리 점수가 붙어요.
_STOPWORDS = frozenset({
    "mcp", "agent", "skill", "server", "service", "api", "tool", "tools",
    "the", "a", "an", "for", "of", "and", "or", "to", "with", "in", "on",
    "agora", "aws", "amazon", "get", "list", "set", "run", "use",
})

# 한국어 자산 설명에 흔한 어절. 남겨두면 "기능을 제공하는 서버"류 문구만으로 점수가 붙어요.
#
# 접두 일치로 걸러지므로(`_is_ko_stopword`) **의미어의 접두가 되는 말은 넣지 않아요.**
# 예: `관리`를 넣으면 `관리자`·`관리형`이 함께 사라져서 실제 도메인 용어를 잃어요. 반대로
# `조회`·`검색`처럼 자산의 일을 나타내는 말도 넣지 않아요 — 그건 진짜 유사 신호예요.
_STOPWORDS_KO = frozenset({
    "서버", "서비스", "기능", "위한", "그리고", "또는", "제공", "지원",
    "합니다", "해요", "있는", "하는", "대한", "통해", "각종", "다양한", "간단한",
})

HIGH_THRESHOLD = 70
MEDIUM_THRESHOLD = 40

_W_CAPABILITY = 55
_W_NAME = 25
_W_ENDPOINT = 25
_W_DESCRIPTION = 15
# 소스 본문(Skill의 SKILL.md 등) 유사도. capability와 같은 "실제로 같은 일을 하는가" 축이라
# 가중치도 같아요 — Skill 자산은 tool/skill 이름이 없어서 이게 유일한 능력 신호예요.
_W_CONTENT = 55


def _tokens(text: str) -> set[str]:
    """비교용 토큰 집합. 라틴 문자와 한글을 모두 다뤄요.

    **왜 한글을 따로 다루나.** 처음엔 `[^a-z0-9]+`로만 쪼갰더니 `벤더 주문 조회` 같은 한국어
    설명이 **빈 집합**이 돼서 설명 축이 통째로 죽었어요. Agora의 자산 설명은 대부분 한국어라
    그 축이 없으면 사실상 tool 이름만 보고 판단하게 돼요.

    라틴 토큰은 끝의 복수형 `s`를 떼요 — `orders`와 `order`는 같은 능력을 가리키는데 별개
    토큰으로 세면 유사도가 실제보다 낮게 나와요(실측: 25%).
    """
    if not text:
        return set()
    lowered = text.lower()
    out: set[str] = set()

    for token in re.split(r"[^a-z0-9]+", lowered):
        if len(token) > 1 and token not in _STOPWORDS:
            out.add(_singular(token))

    # 한글은 어절 단위로 봐요 — 형태소 분석기를 들이면 의존성과 실패 지점이 늘고, 어절 일치
    # 만으로도 "같은 설명"은 충분히 잡혀요.
    #
    # stopword는 **접두 일치**로 걸러요. `기능을`·`제공하는`처럼 조사·어미가 붙은 형태가
    # 그대로 남으면 "기능을 제공하는 서버"만 겹치는 무관한 자산끼리 점수가 붙어요(실측 5점).
    for token in re.findall(r"[가-힣]{2,}", lowered):
        if not _is_ko_stopword(token):
            out.add(token)

    return out


def _is_ko_stopword(token: str) -> bool:
    """어절이 stopword로 시작하면 stopword로 봐요(조사·어미 흡수)."""
    return any(token.startswith(stop) for stop in _STOPWORDS_KO)


def _singular(token: str) -> str:
    """단순 복수형 `s` 제거. `status`·`bus`처럼 원래 s로 끝나는 짧은 단어는 건드리지 않아요."""
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _inline_json(node: dict, key: str) -> dict:
    """`{"inlineContent": "<json>"}` 래핑을 벗겨요 (judge_document._inline과 같은 규약).

    Registry는 tool 정의·agent card를 문자열로 감싸 저장해요. 파싱 실패는 "정보 없음"으로
    다뤄요 — 여기서 예외를 올리면 등록 훅이 깨져요.
    """
    value = node.get(key)
    if isinstance(value, dict) and "inlineContent" in value:
        try:
            parsed = json.loads(value["inlineContent"])
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return value if isinstance(value, dict) else {}


def capability_names(descriptors: dict) -> set[str]:
    """자산이 제공하는 능력 이름 집합 — MCP tool 이름 + Agent skill 이름/id.

    judge_document의 `_mcp_section`/`_agent_section`이 읽는 것과 같은 자리를 보지만, 저기는
    사람이 읽을 마크다운을 만들고 여기는 비교용 집합을 만들어요. 형태가 달라서 공유하지 않아요.
    """
    if not isinstance(descriptors, dict):
        return set()
    out: set[str] = set()

    mcp = descriptors.get("mcp")
    if isinstance(mcp, dict):
        for tool in _inline_json(mcp, "tools").get("tools") or []:
            if isinstance(tool, dict) and tool.get("name"):
                out.add(str(tool["name"]).strip().lower())

    agent = descriptors.get("agent")
    if isinstance(agent, dict):
        for skill in _inline_json(agent, "agentCard").get("skills") or []:
            if not isinstance(skill, dict):
                continue
            label = skill.get("name") or skill.get("id")
            if label:
                out.add(str(label).strip().lower())

    return {name for name in out if name}


def endpoint_of(descriptors: dict) -> str:
    """자산이 가리키는 **원본(upstream)** 대상 endpoint. 없으면 "".

    `upstreamEndpoint`만 신뢰해요. hosted MCP와 배포형 Agent의 `endpoint`에는 Agora가 만든
    **공유 인프라 주소**가 들어가요(MCP는 하나의 Gateway URL, Agent는 AgentCore Runtime
    invocation URL). 여러 자산이 같은 Gateway를 공유하니 그 값을 비교하면 무관한 자산끼리 전부
    "같은 대상"으로 오판돼요 — 실제로 무관한 두 MCP가 이것 때문에 25점을 받았어요.

    URL 패턴으로 "Agora 인프라인지" 가려내는 방식은 쓰지 않아요. Gateway URL 형태는 리전·
    스테이지·테스트 환경마다 다르고, 새 형태가 생기면 조용히 오판으로 돌아가요. 대신
    **호출부가 실제로 공유 여부를 세서** 판단해요(`shared_endpoints` 참조).

    upstream이 없는 자산은 "고유 대상 정보 없음"으로 다루고 tool/skill 축이 판단해요
    (그게 원래 주 축이에요).
    """
    if not isinstance(descriptors, dict):
        return ""

    mcp = descriptors.get("mcp")
    if isinstance(mcp, dict):
        upstream = mcp.get("upstreamEndpoint")
        if isinstance(upstream, str) and upstream.strip():
            return _normalize_endpoint(upstream)

    return ""


def raw_endpoints(descriptors: dict) -> set[str]:
    """descriptor에 적힌 모든 endpoint(공유 인프라 포함). 공유 여부 판정용이에요."""
    if not isinstance(descriptors, dict):
        return set()
    out: set[str] = set()
    for key in ("mcp", "agent"):
        node = descriptors.get(key)
        if not isinstance(node, dict):
            continue
        for field_name in ("upstreamEndpoint", "endpoint"):
            value = node.get(field_name)
            if isinstance(value, str) and value.strip():
                out.add(_normalize_endpoint(value))
    return out


def shared_endpoints(all_descriptors: list[dict], *, min_owners: int = 3) -> set[str]:
    """`min_owners`개 이상의 자산이 함께 쓰는 endpoint 집합 — 식별력이 없는 주소예요.

    Agora Gateway처럼 플랫폼이 나눠주는 주소는 자연히 많은 자산에 등장해요. 하드코딩한 URL
    패턴 대신 **실제 데이터에서 공유 사실을 세서** 판단하니, 새 인프라 형태가 생겨도 규칙을
    고칠 필요가 없어요.

    기본값 3은 "우연히 두 자산이 같은 원본을 가리키는 진짜 중복"을 인프라로 오인하지 않기
    위한 값이에요 — 2로 잡으면 잡아야 할 중복이 사라져요.
    """
    counts: dict[str, int] = {}
    for descriptors in all_descriptors:
        for endpoint in raw_endpoints(descriptors):
            counts[endpoint] = counts.get(endpoint, 0) + 1
    return {endpoint for endpoint, n in counts.items() if n >= min_owners}


def _normalize_endpoint(value: str) -> str:
    return value.strip().rstrip("/").lower()


def score_pair(
    *,
    name: str,
    description: str,
    descriptors: dict,
    other_name: str,
    other_description: str,
    other_descriptors: dict,
    ignore_endpoints: frozenset[str] | set[str] = frozenset(),
    content: str = "",
    other_content: str = "",
) -> tuple[int, list[str]]:
    """두 자산의 중복 점수(0~100)와 근거 목록을 돌려줘요.

    `ignore_endpoints`에 든 주소는 endpoint 축에서 무시해요 — `shared_endpoints()`로 구한
    "여러 자산이 공유하는 플랫폼 주소"를 넘기면 인프라 공유가 중복으로 오판되지 않아요.

    `content`/`other_content`는 소스 본문(Skill의 SKILL.md 등)이에요. **Skill 자산은 descriptor에
    tool/skill 이름이 없어서**(`{skill: {sourcePrefix}}`만) capability 축이 항상 0이었어요 —
    실측: 같은 SKILL.md를 이름만 바꿔 등록했는데 28점(이름·설명만). 본문을 넘기면 그 축이 살아요.
    """
    reasons: list[str] = []
    total = 0.0

    caps, other_caps = capability_names(descriptors), capability_names(other_descriptors)
    shared_caps = sorted(caps & other_caps)
    if shared_caps:
        # 분모는 더 작은 쪽 — tool 3개짜리가 tool 30개짜리에 다 포함되면 그건 완전 중복이에요.
        # Jaccard를 쓰면 0.1로 묻혀서 "작은 자산이 큰 자산에 흡수된" 실제 중복을 놓쳐요.
        ratio = len(shared_caps) / min(len(caps), len(other_caps))
        total += _W_CAPABILITY * ratio
        shown = ", ".join(shared_caps[:5])
        more = f" 외 {len(shared_caps) - 5}개" if len(shared_caps) > 5 else ""
        reasons.append(f"공통 tool/skill {len(shared_caps)}개: {shown}{more}")

    name_sim = _jaccard(_tokens(name), _tokens(other_name))
    if name_sim > 0:
        total += _W_NAME * name_sim
        reasons.append(f"이름 유사도 {round(name_sim * 100)}%")

    endpoint, other_endpoint = endpoint_of(descriptors), endpoint_of(other_descriptors)
    if (endpoint and endpoint == other_endpoint
            and endpoint not in ignore_endpoints):
        total += _W_ENDPOINT
        reasons.append("동일 endpoint를 가리켜요")

    desc_sim = _jaccard(_tokens(description), _tokens(other_description))
    if desc_sim > 0:
        total += _W_DESCRIPTION * desc_sim
        reasons.append(f"설명 유사도 {round(desc_sim * 100)}%")

    # 소스 본문 축. Skill 자산엔 이게 유일한 능력 신호예요.
    if content and other_content:
        if _normalize_content(content) == _normalize_content(other_content):
            # 완전 동일 — 이름만 바꾼 복제예요. 다른 축을 기다릴 이유가 없어요.
            total += _W_CONTENT
            reasons.append("소스 본문이 완전히 같아요")
        else:
            content_sim = _jaccard(_tokens(content), _tokens(other_content))
            if content_sim > 0:
                total += _W_CONTENT * content_sim
                reasons.append(f"소스 본문 유사도 {round(content_sim * 100)}%")

    return min(100, int(round(total))), reasons


def _normalize_content(text: str) -> str:
    """본문 동일성 비교용 정규화 — 공백·개행 차이를 무시해요."""
    return " ".join(text.split())


def band_of(score: int) -> str:
    if score >= HIGH_THRESHOLD:
        return "high"
    if score >= MEDIUM_THRESHOLD:
        return "medium"
    return "low"
