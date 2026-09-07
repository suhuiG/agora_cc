"""MCP 프로토콜 클라이언트 — Streamable HTTP + JSON-RPC로 tool 목록을 가져와요.

initialize → notifications/initialized → tools/list 순서로 핸드셰이크해요.
등록/연결테스트에서 endpoint의 실제 tool 목록·서버 메타를 확보하는 데 써요.
네트워크는 http_post로 주입 가능해요(테스트는 스텁, 기본은 urllib).
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass

from .registry import validate_endpoint
from ..registry.models import SensitivityTag

_PROTOCOL_VERSION = "2025-06-18"
_CLIENT_INFO = {"name": "agora", "version": "0.1.0"}
_ACCEPT = "application/json, text/event-stream"

# 악의적/오동작 MCP가 거대한 바디나 수많은 tool을 흘려보내 descriptor에 박제되고
# 모든 카탈로그 뷰어 브라우저로 전달되는 걸 막는 상한이에요.
MAX_RESPONSE_BYTES = 5 * 1024 * 1024   # 5 MB
MAX_TOOLS = 200
MAX_CATALOG_PAGES = 200
MAX_CATALOG_BYTES = 5 * 1024 * 1024

# 상류 MCP가 준 문자열(오류 message 등)을 그대로 옮기지 않고 이 길이로 잘라요.
MAX_UPSTREAM_SNIPPET = 200


class McpProtocolError(Exception):
    """MCP 프로토콜 조회 실패(도달은 되나 initialize/tools/list 실패).

    호출자(`catalog/router.py`)는 이 예외를 **422** 로 옮겨요. 그래서 상류 응답이
    기대와 다른 타입·구조일 때는 `TypeError`/`AttributeError`/`JSONDecodeError` 가
    새어 나가 500 이 되지 않게, 이 파일의 파싱 헬퍼로 전부 이 예외로 바꿔요(IA-67).
    """


@dataclass(frozen=True)
class McpToolInfo:
    name: str
    description: str
    input_schema: dict
    # 프로토콜 조회 결과는 미분류일 수 있지만, 등록 직전에 IA-22b 엔진이 항상 채워요.
    sensitivity: SensitivityTag | None = None


@dataclass(frozen=True)
class McpServerInfo:
    name: str | None
    version: str | None
    instructions: str | None
    tools: list[McpToolInfo]


def _json_type(value) -> str:
    """value의 JSON 타입 이름 — 오류 메시지에 상류 원문 대신 이걸 넣어요.

    사람이 "무엇이 잘못됐나"를 알려면 타입은 필요하지만, 값 자체를 옮기면 상류가
    통제하는 문자열이 화면·로그로 흘러가요. 그래서 타입 이름만 남겨요.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):     # bool은 int의 하위형이라 먼저 걸러야 해요
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _snippet(text: str) -> str:
    """상류 문자열을 화면에 실을 수 있게 제어문자 제거 + 길이 제한을 걸어요."""
    cleaned = "".join(
        ch if ch.isprintable() or ch == " " else " " for ch in text
    ).strip()
    if len(cleaned) > MAX_UPSTREAM_SNIPPET:
        return cleaned[:MAX_UPSTREAM_SNIPPET] + "…"
    return cleaned


def _require_object(value, *, label: str) -> dict:
    """object(dict)여야 하는 자리 — 아니면 McpProtocolError(→422)."""
    if not isinstance(value, dict):
        raise McpProtocolError(
            f"MCP 응답 {label} — object가 아니에요 (받은 타입: {_json_type(value)})."
        )
    return value


def _optional_object(value, *, label: str) -> dict:
    """없어도 되는 object 자리 — 누락·null은 `{}`, 다른 타입은 오류예요.

    누락(`not_applicable`)과 타입 위반(관측 실패)은 다른 사건이에요. 타입이 어긋난 걸
    조용히 `{}`로 접으면 "서버가 안 줬다"로 보이게 되니 그건 하지 않아요.
    """
    if value is None:
        return {}
    return _require_object(value, label=label)


def _optional_str(value, *, label: str) -> str | None:
    """없어도 되는 문자열 자리 — 누락·null은 None, 다른 타입은 오류예요."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise McpProtocolError(
            f"MCP 응답 {label} — 문자열이 아니에요 (받은 타입: {_json_type(value)})."
        )
    return value


def _error_reason(error) -> str:
    """JSON-RPC error 객체에서 사람이 읽을 이유를 뽑아요(타입 방어 + 길이 제한)."""
    code = error.get("code") if isinstance(error, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    parts: list[str] = []
    if isinstance(code, int) and not isinstance(code, bool):
        parts.append(f"code={code}")
    if isinstance(message, str) and message.strip():
        parts.append(_snippet(message))
    else:
        # 이유를 못 읽은 걸 "이유 없음"으로 접지 않아요 — 못 읽었다고 적어요.
        parts.append(
            f"이유를 읽을 수 없어요 (message 타입: {_json_type(message)})"
        )
    return " ".join(parts)


def _read_capped(resp) -> bytes:
    """resp에서 최대 MAX_RESPONSE_BYTES만 읽고, 초과하면 McpProtocolError를 던져요.

    거대한 바디가 통째로 메모리에 올라와 descriptor에 박제되는 걸 막아요.
    상한+1바이트까지 읽어서, 실제 길이가 상한을 넘었는지 판별해요.
    """
    body = resp.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise McpProtocolError(
            f"MCP 응답이 너무 커요 (최대 {MAX_RESPONSE_BYTES} bytes).")
    return body


def _default_post(url, body, headers, timeout):
    req = urllib.request.Request(url, data=body, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    # urllib 기본 HTTPRedirectHandler가 3xx를 따라가요(http/https만). 최종 URL 스킴 재검증.
    resp = urllib.request.urlopen(req, timeout=timeout)
    final_url = resp.geturl()
    if not final_url.startswith(("http://", "https://")):
        raise McpProtocolError(f"리다이렉트 최종 URL이 http(s)가 아니에요: {final_url}")
    status = getattr(resp, "status", None) or resp.getcode()
    resp_headers = {k.lower(): v for k, v in resp.headers.items()}
    text = _read_capped(resp).decode("utf-8", errors="replace")
    return (status, resp_headers, text)


def _loads_object(payload: str, *, label: str) -> dict:
    """JSON 문자열을 object로 파싱해요. 비-JSON·비-object는 McpProtocolError."""
    try:
        parsed = json.loads(payload)
    except ValueError as e:
        # JSONDecodeError 메시지는 위치(line/column)만 담고 원문은 담지 않아요.
        raise McpProtocolError(f"MCP 응답 {label} — JSON이 아니에요: {e}") from e
    return _require_object(parsed, label=label)


def _parse_body(headers: dict, text: str) -> dict:
    """단일 JSON 또는 SSE(data: 라인) 응답에서 JSON-RPC 객체를 뽑아요.

    상류는 우리가 통제하지 않으니 배열·스칼라·비-JSON도 와요. 그런 응답에서
    `dict.get`을 부르면 AttributeError → 500이 되므로 여기서 전부 걸러요.
    """
    ctype = (headers.get("content-type") or "").lower()
    if "text/event-stream" in ctype:
        # 마지막 data: 라인의 JSON을 써요(result가 담긴 message).
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("data:"):
                payload = line[len("data:"):].strip()
                if payload and payload != "[DONE]":
                    return _loads_object(payload, label="SSE data 라인")
        raise McpProtocolError("SSE 응답에서 data 라인을 찾지 못했어요.")
    if not text.strip():
        return {}
    return _loads_object(text, label="응답 본문")


def _rpc(
    http_post,
    endpoint,
    timeout,
    session_id,
    *,
    method,
    msg_id=None,
    notify=False,
    cursor: str | None = None,
):
    body = {"jsonrpc": "2.0", "method": method}
    if not notify:
        body["id"] = msg_id
    if method == "initialize":
        body["params"] = {"protocolVersion": _PROTOCOL_VERSION,
                          "capabilities": {}, "clientInfo": _CLIENT_INFO}
    elif method == "tools/list":
        body["params"] = {"cursor": cursor} if cursor is not None else {}
    headers = {"Content-Type": "application/json", "Accept": _ACCEPT}
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    try:
        status, resp_headers, text = http_post(
            endpoint, json.dumps(body).encode(), headers, timeout)
    except McpProtocolError:
        raise
    except Exception as e:
        raise McpProtocolError(f"MCP 요청 실패({method}): {e}") from e
    if notify:
        return None, resp_headers, len(text.encode("utf-8"))
    if status >= 400:
        raise McpProtocolError(f"MCP 응답 오류({method}): HTTP {status}")
    obj = _parse_body(resp_headers, text)
    error = obj.get("error")
    if error is not None:
        # error가 object가 아닐 수도 있어요(문자열·배열). _error_reason이 타입을 안 믿어요.
        raise McpProtocolError(f"MCP 오류({method}): {_error_reason(error)}")
    result = obj.get("result")
    if result is None:
        # JSON-RPC는 result·error 중 하나가 반드시 있어야 해요. 없는 응답을 빈 result로
        # 접으면 "tool 0개"처럼 엉뚱한 이유가 화면에 뜨거든요.
        raise McpProtocolError(f"MCP 응답에 result가 없어요({method}).")
    return (
        _require_object(result, label=f"{method} result"),
        resp_headers,
        len(text.encode("utf-8")),
    )


def fetch_mcp_tools(endpoint: str, *, timeout: float = 10.0,
                    http_post=_default_post) -> McpServerInfo:
    """endpoint에 MCP 핸드셰이크로 tool 목록·서버 메타를 가져와요. 실패 시 McpProtocolError."""
    validate_endpoint(endpoint)  # SSRF·주입 방어를 fetch보다 먼저
    # 1) initialize
    init_result, headers, _ = _rpc(
        http_post,
        endpoint,
        timeout,
        None,
        method="initialize",
        msg_id=1,
    )
    session_id = headers.get("mcp-session-id")
    # `capabilities`·tool 호출의 `content`는 이 파서가 읽지 않아요(저장소 전체에서 읽는
    # 곳이 없어요). 안 읽는 필드에 타입 문을 세우면 우리가 쓰지도 않는 값 때문에 등록이
    # 막혀요. 나중에 읽게 되면 위의 `_optional_object`/`_optional_str`를 그때 쓰세요.
    server_info = _optional_object(
        init_result.get("serverInfo"), label="initialize의 serverInfo")
    instructions = _optional_str(
        init_result.get("instructions"), label="initialize의 instructions")
    # 2) initialized 알림 (응답 무시)
    _rpc(http_post, endpoint, timeout, session_id,
         method="notifications/initialized", notify=True)
    # 3) tools/list — nextCursor가 없어질 때까지 전 페이지를 읽어요. 일부 페이지만
    # 원장과 대조하면 뒤 페이지의 정상 도구를 MISSING으로 오인해 회수할 수 있어요.
    raw_tools: list = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    msg_id = 2
    page_count = 0
    catalog_bytes = 0
    while True:
        tools_result, _, response_bytes = _rpc(
            http_post,
            endpoint,
            timeout,
            session_id,
            method="tools/list",
            msg_id=msg_id,
            cursor=cursor,
        )
        page_count += 1
        catalog_bytes += response_bytes
        if catalog_bytes > MAX_CATALOG_BYTES:
            raise McpProtocolError(
                "MCP tools/list 전체 응답이 너무 커요 "
                f"(최대 {MAX_CATALOG_BYTES} bytes)."
            )
        page_tools = tools_result.get("tools")
        if page_tools is None:
            # `or []`로 접으면 tools가 누락·null·0·{}인 페이지가 "도구 0개"로 보이고,
            # drift 대조에서는 살아 있는 도구가 MISSING으로 뒤집혀 회수까지 가요.
            raise McpProtocolError("MCP tools/list 응답에 tools가 없어요.")
        if not isinstance(page_tools, list):
            raise McpProtocolError(
                "MCP tools/list의 tools가 list가 아니에요 "
                f"(받은 타입: {_json_type(page_tools)})."
            )
        if len(raw_tools) + len(page_tools) > MAX_TOOLS:
            raise McpProtocolError(
                f"tool이 너무 많아요 (최대 {MAX_TOOLS}개)."
            )
        raw_tools.extend(page_tools)

        next_cursor = tools_result.get("nextCursor")
        if next_cursor is None:
            break
        if page_count >= MAX_CATALOG_PAGES:
            raise McpProtocolError(
                "MCP tools/list 페이지가 너무 많아요 "
                f"(최대 {MAX_CATALOG_PAGES}페이지)."
            )
        if not isinstance(next_cursor, str) or not next_cursor.strip():
            raise McpProtocolError(
                "MCP tools/list의 nextCursor가 비어 있거나 문자열이 아니에요 "
                f"(받은 타입: {_json_type(next_cursor)})."
            )
        if next_cursor in seen_cursors:
            raise McpProtocolError(
                "MCP tools/list가 같은 nextCursor를 반복했어요."
            )
        seen_cursors.add(next_cursor)
        cursor = next_cursor
        msg_id += 1

    tools: list[McpToolInfo] = []
    seen_names: set[str] = set()
    for index, raw_tool in enumerate(raw_tools):
        if not isinstance(raw_tool, dict):
            raise McpProtocolError(
                f"MCP tools/list의 tools[{index}]가 object가 아니에요 "
                f"(받은 타입: {_json_type(raw_tool)})."
            )
        name = raw_tool.get("name")
        if not isinstance(name, str) or not name.strip():
            raise McpProtocolError(
                f"MCP tools/list의 tools[{index}].name이 문자열이 아니에요 "
                f"(받은 타입: {_json_type(name)})."
            )
        if name in seen_names:
            # 같은 이름을 두 번 주면 descriptor의 `toolSensitivitySources`(이름 키
            # dict)와 Cedar action 이름이 뒤에 온 것으로 덮여요. 도구 N개를 선언하고
            # N-1개만 원장에 남는 건 성공이 아니라 부분 실패예요.
            raise McpProtocolError(
                f"MCP tools/list가 같은 tool 이름을 두 번 줬어요: {_snippet(name)}"
            )
        seen_names.add(name)
        description = _optional_str(
            raw_tool.get("description"),
            label=f"tools[{index}].description",
        )
        input_schema = _optional_object(
            raw_tool.get("inputSchema"),
            label=f"tools[{index}].inputSchema",
        )
        tools.append(McpToolInfo(
            name=name,
            description=description or "",
            input_schema=input_schema,
        ))
    return McpServerInfo(
        name=_optional_str(server_info.get("name"), label="serverInfo.name"),
        version=_optional_str(server_info.get("version"), label="serverInfo.version"),
        instructions=instructions,
        tools=tools,
    )
