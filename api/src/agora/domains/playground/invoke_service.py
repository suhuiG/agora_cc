# api/src/agora/domains/playground/invoke_service.py
"""배포된 AgentCore Runtime을 OAuth(JWT)로 호출해 Playground 대화 테스트를 지원해요.

배포된 agent는 customJWTAuthorizer(Cognito M2M OAuth)로 보호돼요. Agora가 신뢰된
대리인으로서 토큰을 받아 Bearer로 호출해요. client_secret은 Cognito describe로만
확보하고(어디에도 저장 안 함) 메모리에 TTL 캐시해요.
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from ...shared.observability import (
    RequestedTraceContext,
    requested_xray_trace_context,
)

_log = logging.getLogger(__name__)

_USAGE_REDEPLOY_REMEDIATION = (
    "사용량 기록을 시작하려면 agent를 최신 scaffold로 재배포하세요."
)
_USAGE_COUNT_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_input_tokens",
    "cache_write_input_tokens",
    "cycle_count",
)
_USAGE_DURATION_FIELDS = ("latency_ms",)
_TOOL_METRIC_COUNT_FIELDS = ("call_count", "success_count", "error_count")
_TOOL_METRIC_DURATION_FIELDS = ("total_time",)
_TOOL_METRICS_MAX = 50
_TOOL_NAME_MAX_LENGTH = 200
_TOOL_NAME_CHARS = re.compile(r"^[A-Za-z0-9_.:-]+$")
_TOOL_METRICS_UNKNOWN_REASON = "invalid_or_excess_tool_names"
_A2A_STRUCTURE_KEYS = {
    "artifacts",
    "contextId",
    "error",
    "history",
    "id",
    "jsonrpc",
    "kind",
    "message",
    "messageId",
    "metadata",
    "parts",
    "result",
    "role",
    "state",
    "status",
    "taskId",
}


def _unknown_usage(reason: str) -> dict:
    return {
        "status": "unknown",
        "source": None,
        "reason": reason,
        "remediation": _USAGE_REDEPLOY_REMEDIATION,
        "metrics": None,
    }


def _non_negative_number(value, *, integer: bool):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    if integer and not isinstance(value, int):
        return None
    return value


def _safe_tool_metric_name(name) -> bool:
    if (
        not isinstance(name, str)
        or len(name) > _TOOL_NAME_MAX_LENGTH
        or name.count("___") != 1
    ):
        return False
    target, operation = name.split("___", 1)
    return bool(
        target
        and operation
        and _TOOL_NAME_CHARS.fullmatch(target)
        and _TOOL_NAME_CHARS.fullmatch(operation)
    )


def _sanitize_agent_usage(raw) -> tuple[dict | None, dict]:
    if not isinstance(raw, dict):
        return None, {}
    metrics = {}
    for field in _USAGE_COUNT_FIELDS:
        value = _non_negative_number(raw.get(field), integer=True)
        if value is not None:
            metrics[field] = value
    for field in _USAGE_DURATION_FIELDS:
        value = _non_negative_number(raw.get(field), integer=False)
        if value is not None:
            metrics[field] = value
    durations = raw.get("cycle_durations")
    if isinstance(durations, list):
        metrics["cycle_durations"] = [
            value
            for item in durations
            if (value := _non_negative_number(item, integer=False)) is not None
        ]
    raw_tools = raw.get("tool_metrics")
    tool_observation = {}
    if isinstance(raw_tools, dict):
        tools = {}
        discarded = 0
        for name, raw_metric in raw_tools.items():
            if (
                not _safe_tool_metric_name(name)
                or not isinstance(raw_metric, dict)
                or len(tools) >= _TOOL_METRICS_MAX
            ):
                discarded += 1
                continue
            metric = {}
            for field in _TOOL_METRIC_COUNT_FIELDS:
                value = _non_negative_number(raw_metric.get(field), integer=True)
                if value is not None:
                    metric[field] = value
            for field in _TOOL_METRIC_DURATION_FIELDS:
                value = _non_negative_number(raw_metric.get(field), integer=False)
                if value is not None:
                    metric[field] = value
            tools[name] = metric
        metrics["tool_metrics"] = tools
        reported_discarded = raw.get("tool_metrics_discarded_count")
        if (
            raw.get("tool_metrics_status") == "unknown"
            and raw.get("tool_metrics_reason") == _TOOL_METRICS_UNKNOWN_REASON
            and isinstance(reported_discarded, int)
            and not isinstance(reported_discarded, bool)
            and reported_discarded > 0
        ):
            discarded += reported_discarded
        if discarded:
            tool_observation = {
                "tool_metrics_status": "unknown",
                "tool_metrics_reason": _TOOL_METRICS_UNKNOWN_REASON,
                "tool_metrics_discarded_count": discarded,
            }
    return metrics or None, tool_observation


def _extract_agent_usage(data) -> dict:
    raw_usage = None
    if isinstance(data, dict):
        result = data.get("result")
        message = result.get("message") if isinstance(result, dict) else None
        metadata = message.get("metadata") if isinstance(message, dict) else None
        if not isinstance(metadata, dict):
            error = data.get("error")
            error_data = error.get("data") if isinstance(error, dict) else None
            metadata = (
                error_data.get("metadata")
                if isinstance(error_data, dict)
                else None
            )
        agora = metadata.get("agora") if isinstance(metadata, dict) else None
        raw_usage = agora.get("usage") if isinstance(agora, dict) else None
    if raw_usage is None:
        return _unknown_usage("agent_usage_not_reported")
    metrics, tool_observation = _sanitize_agent_usage(raw_usage)
    if metrics is None:
        return _unknown_usage("agent_usage_invalid")
    return {
        "status": "observed",
        "source": "agent_report",
        "reason": None,
        "remediation": None,
        "metrics": metrics,
        **tool_observation,
    }


class InvokeError(Exception):
    def __init__(
        self,
        message: str,
        status: int = 502,
        detail: dict[str, str] | None = None,
        usage: dict | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.detail = detail
        self.usage = usage or _unknown_usage("agent_usage_not_reported")


_CREATE_EVENT_TOO_LARGE_MESSAGE = "입력이 너무 커서 대화 기억에 저장하지 못했어요."
_CREATE_EVENT_TOO_LARGE_REMEDIATION = (
    "이 세션의 이전 대화를 이어가지 못할 수 있어요. "
    "입력을 줄여서 다시 보내거나, 새 세션을 시작해 주세요. "
    "긴 문서는 나눠서 보내면 처리돼요."
)
_REQUEST_TOO_LARGE_MESSAGE = "요청이 너무 커서 에이전트에 전달되지 못했어요."
_REQUEST_TOO_LARGE_REMEDIATION = (
    "입력을 줄여서 다시 보내 주세요. 긴 문서는 나눠서 보내면 처리돼요."
)
_CREATE_EVENT_413_PATTERN = re.compile(
    r"an\s+error\s+occurred\s*\(\s*413\s*\)\s+"
    r"when\s+calling\s+the\s+createevent\s+operation\b",
    re.IGNORECASE,
)


def _classify_invoke_error(
    raw_message: str,
    *,
    fallback_message: str,
    fallback_status: int,
    http_status: int | None = None,
) -> InvokeError:
    if _CREATE_EVENT_413_PATTERN.search(raw_message):
        _log.warning("AgentCore CreateEvent 413 원문: %s", raw_message)
        return InvokeError(
            _CREATE_EVENT_TOO_LARGE_MESSAGE,
            status=413,
            detail={
                "message": _CREATE_EVENT_TOO_LARGE_MESSAGE,
                "remediation": _CREATE_EVENT_TOO_LARGE_REMEDIATION,
            },
        )
    if http_status == 413:
        _log.warning("AgentCore HTTP 413 원문: %s", raw_message)
        return InvokeError(
            _REQUEST_TOO_LARGE_MESSAGE,
            status=413,
            detail={
                "message": _REQUEST_TOO_LARGE_MESSAGE,
                "remediation": _REQUEST_TOO_LARGE_REMEDIATION,
            },
        )
    return InvokeError(fallback_message, status=fallback_status)


#: 동기 invoke 의 클라이언트측 타임아웃(초).
#:
#: **60초에서 올렸어요(2026-08-30).** 대화 턴이 쌓이면 한 턴이 이력 재생 + 도구 호출 +
#: 생성이라 60초를 넘겨요 — `weather-assistant-sonnet5` 가 3번째 턴부터 여기서 죽었어요.
#:
#: AgentCore 는 **단일 동기 요청의 하드 타임아웃을 문서화하지 않아요**
#: (`docs/agentcore-runtime-invocation-guide.md` §미확인). 세션 수명 8시간·유휴 15분은
#: 확인됐지만 요청 상한은 아니에요. 그래서 공식 A2A 예시 클라이언트가 쓰는 값(300초)을
#: 따라요 — 근거 있는 값이고, 무한 대기가 아니에요.
INVOKE_TIMEOUT_SECONDS = 300.0

_USER_TOKEN_EXPIRING_MESSAGE = (
    "로그인 토큰의 남은 시간이 에이전트 최대 실행시간보다 짧아요."
)
_USER_TOKEN_EXPIRY_UNKNOWN_MESSAGE = (
    "로그인 토큰의 만료 시각을 확인할 수 없어요."
)
_USER_TOKEN_REFRESH_REMEDIATION = (
    "세션을 갱신한 뒤 다시 시도해 주세요. 계속되면 로그아웃 후 다시 로그인하세요."
)


def _now_epoch_seconds() -> float:
    return time.time()


def require_user_token_lifetime(token_expires_at: int) -> None:
    """사람 토큰이 동기 invoke의 최대 실행시간 전체를 덮는지 확인해요."""
    valid_expiry = (
        not isinstance(token_expires_at, bool)
        and isinstance(token_expires_at, (int, float))
        and math.isfinite(token_expires_at)
        and token_expires_at > 0
    )
    if not valid_expiry:
        raise InvokeError(
            _USER_TOKEN_EXPIRY_UNKNOWN_MESSAGE,
            status=409,
            detail={
                "message": _USER_TOKEN_EXPIRY_UNKNOWN_MESSAGE,
                "remediation": _USER_TOKEN_REFRESH_REMEDIATION,
                "reason": "user_token_expiry_unknown",
            },
        )
    if token_expires_at - _now_epoch_seconds() < INVOKE_TIMEOUT_SECONDS:
        raise InvokeError(
            _USER_TOKEN_EXPIRING_MESSAGE,
            status=409,
            detail={
                "message": _USER_TOKEN_EXPIRING_MESSAGE,
                "remediation": _USER_TOKEN_REFRESH_REMEDIATION,
                "reason": "user_token_expiring",
            },
        )


def _default_http_post(
    url: str,
    *,
    headers: dict,
    data: bytes,
    timeout: float = INVOKE_TIMEOUT_SECONDS,
) -> tuple[int, bytes]:
    """urllib 기반 POST. (status, body) 반환. 4xx/5xx도 예외 없이 status로 돌려줘요.

    **타임아웃은 `InvokeError`(504)로 바꿔요.** 그대로 올리면 FastAPI 가 500
    `Internal Server Error` 로 만들고, 화면에는 원인이 하나도 안 남아요 — 사용자는 agent 가
    깨진 줄 알아요. 실제로는 "오래 걸렸다" 예요.
    """
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except TimeoutError as exc:
        raise InvokeError(
            f"agent 응답이 {int(timeout)}초 안에 오지 않았어요.",
            status=504,
            detail={
                "message": f"agent 응답이 {int(timeout)}초 안에 오지 않았어요.",
                "remediation": (
                    "대화가 길어지면 한 턴에 이력 재생·도구 호출·생성이 모두 들어가서 "
                    "느려져요. 새 세션으로 시작하거나, 질문을 짧게 나눠서 물어보세요. "
                    "도구 호출이 많으면 max iterations 를 낮추는 것도 방법이에요."
                ),
            },
        ) from exc


class CognitoTokenProvider:
    """Cognito M2M client_credentials 토큰 발급. secret은 describe로만 확보(미저장), TTL 캐시."""

    def __init__(self, *, pool_id: str, client_id: str, token_url: str, scope: str,
                 region: str, cognito_client=None, http_post=None, now=None):
        self._pool_id = pool_id
        self._client_id = client_id
        self._token_url = token_url
        self._scope = scope
        self._region = region
        self._cognito = cognito_client
        self._http_post = http_post or _default_http_post
        self._now = now or time.time
        self._token = ""
        self._exp = 0.0   # epoch seconds
        self._refresh_lock = threading.Lock()

    def _cog(self):
        if self._cognito is None:
            import boto3
            self._cognito = boto3.client("cognito-idp", region_name=self._region)
        return self._cognito

    def _client_secret(self) -> str:
        # 매 발급마다 조회해요(캐시는 토큰 레벨에서). describe는 IAM 권한이 문지기예요.
        resp = self._cog().describe_user_pool_client(
            UserPoolId=self._pool_id, ClientId=self._client_id)
        return resp["UserPoolClient"]["ClientSecret"]

    def token(self) -> str:
        # 만료 60초 여유를 두고 캐시된 토큰 재사용.
        if self._token and self._now() < self._exp - 60:
            return self._token
        with self._refresh_lock:
            if self._token and self._now() < self._exp - 60:
                return self._token
            secret = self._client_secret()
            form = urllib.parse.urlencode({
                "grant_type": "client_credentials",
                "scope": self._scope,
            }).encode()
            import base64
            basic = base64.b64encode(
                f"{self._client_id}:{secret}".encode()
            ).decode()
            status, body = self._http_post(
                self._token_url,
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data=form,
            )
            if status != 200:
                raise InvokeError(
                    f"토큰 발급 실패 (status={status}): {body[:200]!r}",
                    status=502,
                )
            data = json.loads(body)
            self._token = data["access_token"]
            self._exp = self._now() + float(data.get("expires_in", 3600))
            return self._token


_ENDPOINT = "https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{arn}/invocations"


class AgentCoreInvokeResult(str):
    """Reply text with the trace context requested on the outbound call."""

    trace_id: str | None
    span_id: str | None
    requested_sampling: float | None
    usage: dict

    def __new__(
        cls,
        text: str,
        *,
        trace_id: str | None,
        span_id: str | None,
        requested_sampling: float | None,
        usage: dict | None = None,
    ) -> "AgentCoreInvokeResult":
        result = super().__new__(cls, text)
        result.trace_id = trace_id
        result.span_id = span_id
        result.requested_sampling = requested_sampling
        result.usage = usage or _unknown_usage("agent_usage_not_reported")
        return result


class AgentCoreCallResult(dict):
    """JSON-RPC payload with the trace context requested on the call."""

    def __init__(
        self,
        payload: dict,
        *,
        trace_context: RequestedTraceContext | None,
    ) -> None:
        super().__init__(payload)
        self.trace_id = trace_context.trace_id if trace_context else None
        self.span_id = trace_context.span_id if trace_context else None
        self.requested_sampling = (
            trace_context.requested_sampling if trace_context else None
        )


class AgentCoreInvoker:
    def __init__(
        self,
        *,
        region: str,
        token_provider,
        http_post=None,
        now=None,
        force_trace_sampling: bool = False,
    ):
        self._region = region
        self._tp = token_provider
        self._http_post = http_post or _default_http_post
        self._now = now or time.time
        self._force_trace_sampling = force_trace_sampling

    def _requested_trace_context(self) -> RequestedTraceContext | None:
        if not self._force_trace_sampling:
            return None
        return requested_xray_trace_context(now=self._now)

    def call(
        self,
        *,
        runtime_arn: str,
        method: str,
        params: dict,
        session_id: str,
        actor_id: str | None = None,
        call_handle: str | None = None,
        user_token: str | None = None,
        timeout_sec: float = INVOKE_TIMEOUT_SECONDS,
    ) -> AgentCoreCallResult:
        """배포된 agent에 A2A JSON-RPC를 그대로 보내고 파싱한 응답(dict)을 돌려줘요.

        message/send 말고도 임의 메서드를 부를 수 있어요 — 배포 검증이
        `agora/selfcheck`를 호출하는 데 써요. HTTP 오류·A2A error는 InvokeError로,
        JSON-RPC error 객체는 그대로 담아 돌려줘요(호출부가 code로 분기하게).
        """
        if not runtime_arn:
            raise InvokeError("배포된 runtime이 없어요. 먼저 DEPLOY 하세요.", status=422)
        if not isinstance(params, dict):
            raise InvokeError("JSON-RPC params는 객체여야 해요.", status=400)
        token = self._tp.token()
        url = _ENDPOINT.format(
            region=self._region,
            arn=urllib.parse.quote(runtime_arn, safe=""),
        )
        payload_params = dict(params)
        payload_params.pop("agoraContext", None)
        agora_context = {}
        if actor_id:
            if "\r" in actor_id or "\n" in actor_id:
                raise InvokeError("actor header가 유효하지 않아요.", status=400)
            agora_context["actorId"] = actor_id
        if call_handle:
            # IA-61: 생성 코드가 이걸 `X-Agora-Call` 헤더로 실어 Gateway 를 불러요.
            #
            # `agoraContext` 로 보내요, A2A `message.metadata` 가 아니에요. handle 은 bearer
            # 성격의 자격증명이고 `message.metadata` 는 대화 메시지의 일부라 세션 메모리·
            # 대화 이력에 남을 수 있어요.
            if "\r" in call_handle or "\n" in call_handle:
                raise InvokeError("call handle이 유효하지 않아요.", status=400)
            agora_context["callHandle"] = call_handle
        if user_token:
            # IA-81: 토큰 원문은 헤더에만 두고, body에는 포털이 사람 토큰을
            # 실었다는 독립 선언만 남겨요. 생성 Runtime은 이 선언과 실제 헤더를
            # 대조해 allowlist 누락을 기계 호출과 구분해요.
            agora_context["userTokenForwarded"] = True
        if agora_context:
            payload_params["agoraContext"] = agora_context
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": method,
            "params": payload_params,
        }).encode("utf-8")
        trace_context = self._requested_trace_context()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
        }
        if trace_context is not None:
            headers["X-Amzn-Trace-Id"] = trace_context.header
        if actor_id:
            headers["X-Agora-Actor-Id"] = actor_id
        if user_token:
            if "\r" in user_token or "\n" in user_token:
                raise InvokeError("사용자 token header가 유효하지 않아요.", status=400)
            headers["X-Agora-User-Token"] = user_token
        status, body = self._http_post(
            url,
            headers=headers,
            data=payload,
            timeout=timeout_sec,
        )
        if status >= 400:
            raw_error = body.decode("utf-8", errors="replace")
            raise _classify_invoke_error(
                raw_error,
                fallback_message=(
                    f"AgentCore 호출 실패 (status={status}): {raw_error[:300]}"
                ),
                fallback_status=status,
                http_status=status,
            )
        try:
            response = json.loads(body)
        except (ValueError, TypeError):
            response = {"result": body.decode("utf-8", errors="replace")}
        return AgentCoreCallResult(response, trace_context=trace_context)

    def invoke(
        self,
        *,
        runtime_arn: str,
        prompt: str,
        session_id: str,
        actor_id: str | None = None,
        call_handle: str | None = None,
        metadata: dict | None = None,
        user_token: str | None = None,
        timeout_sec: float = INVOKE_TIMEOUT_SECONDS,
    ) -> AgentCoreInvokeResult:
        """대화 1턴(message/send) — 응답 텍스트를 돌려줘요."""
        message = {
            "role": "user",
            "parts": [{"kind": "text", "text": prompt}],
            "messageId": uuid.uuid4().hex,
        }
        if metadata:
            message["metadata"] = metadata
        data = self.call(
            runtime_arn=runtime_arn, method="message/send", session_id=session_id,
            params={"message": message}, actor_id=actor_id,
            call_handle=call_handle,
            user_token=user_token,
            timeout_sec=timeout_sec,
        )
        # A2A 규약: HTTP 200이어도 error 필드가 있으면 실패예요.
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            error = _classify_invoke_error(
                msg,
                fallback_message=f"에이전트 오류: {msg}",
                fallback_status=502,
            )
            error.usage = _extract_agent_usage(data)
            raise error
        return AgentCoreInvokeResult(
            _extract_reply_text(data),
            trace_id=data.trace_id,
            span_id=data.span_id,
            requested_sampling=data.requested_sampling,
            usage=_extract_agent_usage(data),
        )


def _text_parts(container) -> list[str]:
    """A2A `parts` 배열에서 text 조각만 모아요."""
    if not isinstance(container, dict):
        return []
    return [
        part["text"]
        for part in container.get("parts") or ()
        if isinstance(part, dict) and part.get("text")
    ]


def _safe_structure_keys(value) -> list[str]:
    if not isinstance(value, dict):
        return []
    keys = [
        key if key in _A2A_STRUCTURE_KEYS else "<other>"
        for key in value
    ]
    return sorted(set(keys))[:50]


def _part_kinds(parts) -> list[str]:
    allowed = {"text", "data", "file"}
    if not isinstance(parts, list):
        return []
    return [
        kind if kind in allowed else "unknown"
        for part in parts[:100]
        for kind in [part.get("kind") if isinstance(part, dict) else None]
    ]


def _a2a_shape_summary(data) -> dict:
    """Return bounded A2A structure diagnostics without any payload values."""
    try:
        serialized_bytes = len(
            json.dumps(
                data,
                ensure_ascii=False,
                separators=(",", ":"),
                default=lambda _value: None,
            ).encode("utf-8")
        )
    except (TypeError, ValueError, OverflowError):
        serialized_bytes = None
    summary = {
        "input_type": type(data).__name__,
        "serialized_bytes": serialized_bytes,
        "top_level_keys": _safe_structure_keys(data),
    }
    result = data.get("result") if isinstance(data, dict) else None
    summary["result_keys"] = _safe_structure_keys(result)
    message = result.get("message") if isinstance(result, dict) else None
    message_parts = message.get("parts") if isinstance(message, dict) else None
    summary["message_part_count"] = (
        len(message_parts) if isinstance(message_parts, list) else 0
    )
    summary["message_part_kinds"] = _part_kinds(message_parts)
    artifacts = result.get("artifacts") if isinstance(result, dict) else None
    artifacts = artifacts if isinstance(artifacts, list) else []
    artifact_parts = [
        part
        for artifact in artifacts[:100]
        if isinstance(artifact, dict)
        for part in (
            artifact.get("parts")
            if isinstance(artifact.get("parts"), list)
            else []
        )[:100]
    ]
    summary["artifact_count"] = len(artifacts)
    summary["artifact_part_count"] = len(artifact_parts)
    summary["artifact_part_kinds"] = _part_kinds(artifact_parts)
    return summary


def _extract_reply_text(data) -> str:
    """A2A message/send 응답에서 텍스트를 뽑아요.

    텍스트를 못 찾으면 **응답 구조를 문자열화해 돌려주지 않아요.** 이전에는
    `str(result)` 로 폴백해서, agent 의 event loop 이 실패해 빈 응답을 준 턴에 사용자가
    **raw A2A envelope 을 그대로 보게 됐어요**(실측 2026-08-21, IH-70). A2A 는 HTTP 200 +
    error 없음이어도 텍스트가 없을 수 있어서(agent 내부 실패) 그걸 성공처럼 보여주면 안 돼요.
    본문 없는 구조 요약만 로그로 남기고 사용자에게는 무슨 일인지 알려요.
    """
    if isinstance(data, dict):
        result = data.get("result")
        if isinstance(result, dict):
            texts = _text_parts(result.get("message"))
            if texts:
                return " ".join(texts).strip()
            # A2A task 형태(artifacts[].parts[].text)도 정식 응답이에요.
            for artifact in result.get("artifacts") or ():
                texts = _text_parts(artifact)
                if texts:
                    return " ".join(texts).strip()
    _log.error(
        "A2A 응답에서 텍스트를 찾지 못했어요 — agent 턴이 실패했을 수 있어요: %s",
        _a2a_shape_summary(data),
    )
    raise InvokeError(
        "에이전트가 응답 텍스트를 주지 않았어요. agent 로그를 확인해 주세요.",
        status=502,
    )
