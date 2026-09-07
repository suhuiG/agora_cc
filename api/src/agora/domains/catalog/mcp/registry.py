"""MCP 등록 — connect(tool 조회·hosted) / deploy(pending)."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

from .models import McpRegistration, McpRegisterResult


class McpHealthError(Exception):
    """endpoint HTTP 도달성 검증 실패."""


class McpEndpointError(Exception):
    """endpoint 입력값 검증 실패(스킴·주입 등). 도달성 실패(McpHealthError)와 구분해요."""


class McpDeployModeRemoved(Exception):
    """`mode="deploy"`로 등록 시도. 배포형은 소스 업로드 배포 흐름을 써야 해요.

    조용히 무시하거나 400 `unknown mode`로 뭉개지 않고 별도 예외로 두는 이유: 이 mode를 쓰던
    외부 클라이언트가 **무엇을 대신 써야 하는지** 알아야 하거든요.
    """


# 생성되는 `claude mcp add ... {endpoint}` 셸 명령을 탈출할 수 있는 문자예요.
# 정상 MCP URL에는 이 중 어느 것도 없어야 해요(공백 포함).
_SHELL_METACHARS = set(";|&$`()<>\n\r\t \"'\\")


def validate_endpoint(endpoint: str, *, require_https: bool = True) -> None:
    """connect endpoint를 검증해요. 스킴·netloc을 보고 셸 메타문자를 거부해요.

    (a) copy-paste 셸 명령 주입, (b) urlopen SSRF(file://·IMDS 등) 표면을 함께 막아요.
    실패 시 McpEndpointError를 던져요(입력 검증이라 도달성 실패와 구분).

    `require_https=True`(기본)는 **`https` 만** 받아요 — AgentCore가 MCP server target
    endpoint에 `https://.*` 정규식을 강제하거든요(실측 2026-08-26: `CreateGatewayTarget`이
    `ValidationException ... Member must satisfy regular expression pattern: https://.*`).
    예전에는 `http`가 connect 테스트를 통과해 도구 목록까지 보여준 뒤, 퍼블리시 **마지막
    단계**에서 502로 떨어졌고 원인(스킴)이 어디에도 표시되지 않았어요(CA-31).

    `require_https=False`는 Gateway target을 만들지 않는 호출자(A2A agent-card 조회)만
    써요 — 그 경로는 AgentCore 제약을 받지 않아서 여기서 좁히면 동작하는 등록이 깨져요.
    """
    if not endpoint:
        raise McpEndpointError("connect 모드는 endpoint가 필요해요.")
    # 셸 메타문자/공백 — 명령 탈출 방지. urlparse보다 먼저 봐요(원문 기준).
    bad = _SHELL_METACHARS.intersection(endpoint)
    if bad:
        raise McpEndpointError(
            f"endpoint에 허용되지 않는 문자가 있어요: {''.join(sorted(bad))!r}")
    parsed = urllib.parse.urlparse(endpoint)
    allowed = ("https",) if require_https else ("http", "https")
    if parsed.scheme not in allowed:
        if require_https and parsed.scheme == "http":
            raise McpEndpointError(
                "endpoint는 `https://` 여야 해요 — AgentCore Gateway가 MCP server "
                "target에 https 만 허용해요. `http://` 주소는 등록할 수 없으니 TLS 로 "
                "공개된 주소를 넣어 주세요."
            )
        raise McpEndpointError(
            f"endpoint는 {'https' if require_https else 'http/https'}만 허용해요 "
            f"(받은 스킴: {parsed.scheme or '없음'!r})."
        )
    if not parsed.netloc:
        raise McpEndpointError("endpoint에 호스트(netloc)가 없어요.")


def _default_head(url: str, timeout: float):
    req = urllib.request.Request(url, method="HEAD")
    return urllib.request.urlopen(req, timeout=timeout)


def _default_get(url: str, timeout: float):
    req = urllib.request.Request(url, method="GET")
    return urllib.request.urlopen(req, timeout=timeout)


def _status_of(resp) -> int:
    # urllib response는 .status, 테스트 스텁은 .status_code
    return getattr(resp, "status", None) or getattr(resp, "status_code")


def healthcheck(endpoint: str, *, timeout: float = 5.0,
                http_head=_default_head, http_get=_default_get) -> None:
    """HTTP 도달성만 확인. 2xx/4xx=생존(통과), 5xx/연결불가/타임아웃=실패."""
    try:
        resp = http_head(endpoint, timeout=timeout)
        status = _status_of(resp)
        if status in (405, 501):          # HEAD 미지원 → GET 폴백
            resp = http_get(endpoint, timeout=timeout)
            status = _status_of(resp)
    except McpHealthError:
        raise
    except Exception as e:                # 연결 거부·타임아웃·DNS 등
        raise McpHealthError(f"endpoint 헬스체크 실패: {e}") from e
    if status >= 500:
        raise McpHealthError(f"endpoint 헬스체크 실패: HTTP {status}")


# validate_endpoint 정의 이후에 import — protocol.py가 registry.validate_endpoint를
# 역참조하므로, 이 위치에서 가져와야 순환 import를 피해요.
from .protocol import fetch_mcp_tools, McpProtocolError  # noqa: E402


def _sensitivity_source(tool) -> str:
    """이 도구의 민감도 태그가 어디서 왔나 (`SensitivitySource` 값).

    * MCP 가 `tools/list`에서 직접 선언했으면 `descriptor`.
    * 아니면 이름 첫 낱말로 갈렸는지 보고 `name_guess`.
    * 둘 다 아니면 `auto` — 등록 시점 자동 분류(LLM 또는 보수적 기본)가 채운 값이에요.
    """
    from ....shared.mcp_sensitivity import leading_verb_sensitivity
    from .drift_models import SensitivitySource

    if getattr(tool, "sensitivity", None) is not None:
        return SensitivitySource.DESCRIPTOR.value
    if leading_verb_sensitivity(tool.name) is not None:
        return SensitivitySource.NAME_GUESS.value
    return SensitivitySource.AUTO.value


def register_mcp(reg: McpRegistration, *, fetch=fetch_mcp_tools,
                 gateway_svc=None, sensitivity_classifier=None) -> McpRegisterResult:
    if reg.mode == "connect":
        validate_endpoint(reg.endpoint or "")  # SSRF·주입 방어 (fetch 내부에서도 하지만 명시)
        info = fetch(reg.endpoint)             # initialize+tools/list. 실패 시 McpProtocolError
        if not info.tools:
            raise McpProtocolError(
                "tool을 하나도 제공하지 않는 MCP는 등록할 수 없어요.")
        if sensitivity_classifier is None:
            from ....shared.deps import get_sensitivity_classifier

            sensitivity_classifier = get_sensitivity_classifier()
        from .sensitivity_suggest import suggest_sensitivities

        suggestions = suggest_sensitivities(
            info.tools, classifier=sensitivity_classifier
        )
        tools_inline = json.dumps({"tools": [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.input_schema,
                "sensitivity": suggestion.value,
            }
            for t, (suggestion, _reason) in zip(info.tools, suggestions, strict=True)
        ]})
        gw = (gateway_svc.register_target(reg.name, reg.endpoint, tools_inline)
              if gateway_svc else {"target_id": None, "gateway_url": None})
        effective_endpoint = gw["gateway_url"] or reg.endpoint
        node = {
            "endpoint": effective_endpoint,
            "server": {"schemaVersion": "2025-12-11",
                       "name": info.name, "instructions": info.instructions},
            "tools": {"inlineContent": tools_inline},
            # 태그의 출처를 남겨요(티켓 A). "MCP 가 선언한 DELETE"와 "우리가 이름으로 짐작한
            # DELETE"를 관리자 화면이 구별해야 하는데, 태그만 보면 갈릴 수 없거든요.
            # `tools.inlineContent` **밖에** 두는 게 중요해요 — 그 문자열은 Gateway target
            # 페이로드로 그대로 나가니까요.
            "toolSensitivitySources": {
                t.name: _sensitivity_source(t) for t in info.tools
            },
        }
        if gw["gateway_url"]:
            node["upstreamEndpoint"] = reg.endpoint  # 원본 보존
        if gw["target_id"]:
            node["gatewayTargetId"] = gw["target_id"]  # teardown용
        if gw.get("gateway_id"):
            # target ID는 gateway 안에서만 유일해요. 이 좌표가 있어야 OAuth
            # 전환 뒤에도 purge/rollback이 실제 소유 gateway를 찾을 수 있어요.
            node["gatewayIdentifier"] = gw["gateway_id"]
        # Gateway가 라이브 tool 접두어에 쓰는 정규화된 target name을 보존해요(결함 #10).
        # scaffold의 authorization 비교가 이 값을 기준으로 삼아, `.`·`_`·비-ASCII 이름도
        # 어긋나지 않게 해요. .get()으로 읽어 옛 반환형과 하위호환을 지켜요.
        if gw.get("target_name"):
            node["gatewayTargetName"] = gw["target_name"]
        descriptors = {"mcp": node}
        return McpRegisterResult(hosting="hosted", endpoint=reg.endpoint,
                                 descriptors=descriptors)
    if reg.mode == "deploy":
        # 배포형 MCP는 소스 업로드 → CodeBuild → Lambda → Gateway target 파이프라인
        # (`POST /api/mcp/deploy/*`)이 담당해요. 이 경로는 Runtime 연동 전의 자리표시자로,
        # endpoint 없는 `pending` 레코드만 만들고 아무도 그 상태를 진전시키지 않았어요 —
        # 카탈로그에 영구 미완성 자산이 쌓이는 길이라 막아요.
        raise McpDeployModeRemoved(
            "배포형 MCP는 이 엔드포인트로 등록할 수 없어요. "
            "소스 업로드 배포 흐름(POST /api/mcp/deploy/init)을 사용해 주세요.")
    raise ValueError(f"unknown mode: {reg.mode}")
