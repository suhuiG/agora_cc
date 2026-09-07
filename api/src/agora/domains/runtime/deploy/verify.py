"""Verify a deployed agent against declaration-owned tool expectations."""

from __future__ import annotations

import json
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial

from ....shared.gateway_denial import denial_message_for_reason

_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_ATTEMPTS = 5
_BACKOFF_SEC = 4
#: verify 전체 예산(초). **120초에서 올렸어요(2026-08-30).**
#:
#: `user-info-bot` 이 `user-table-mcp-read___list_users` 도달성 관측에서 죽었어요. 한 번의
#: 도구 probe 가 실제로는 (1) agent runtime 콜드 스타트 (2) LLM 턴 (3) MCP Lambda 콜드
#: 스타트 (4) DynamoDB Scan 100행 (5) 그 결과로 LLM 응답 생성이라, 60초로는 부족했어요.
#:
#: 초과하면 크래시가 아니라 `_deadline_report` 로 `unknown` 을 남겨요 — 관측 실패를 통과로
#: 적지 않아요(ADR-0037 §4). 리스 제약은 없어요: 폴러는 조건부 쓰기로 job 을 전진시켜요.
_TOTAL_TIMEOUT_SEC = 300.0
_MAX_PROBES = 10
_MAX_PARALLEL_PROBES = 6
_VERIFIER_VERSION = "v4"
_OBSERVATION_TEXT_LIMIT = 4_000
_OBSERVATION_DEPTH_LIMIT = 6
_OBSERVATION_NODE_LIMIT = 64
_OBSERVATION_FIELD_LIMIT = 24
_OBSERVATION_VALUE_LIMIT = 500
_BEDROCK_AGENTCORE_QUALIFIED_VERSIONS = ("1.22.0",)
_SENSITIVE_FIELD_PARTS = (
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+\S+")
_EMAIL_RE = re.compile(
    r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(?:[\"'])?(?:access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"client[_-]?secret|api[_-]?key|password|credential|authorization)"
    r"(?:[\"'])?\s*[:=]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)"
)
_SAFE_ENUM_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SAFE_COORDINATE_RE = re.compile(r"^[A-Za-z0-9_.:/=;-]{1,200}$")
#: Gateway 도구 이름의 구분자. 이걸 가진 기대 도구만 ④ 원장 판정 대상이에요 —
#: `skills` 나 내장 도구는 ④ binding 이 없는 게 정상이라 「신청 안 됨」으로 몰면 안 돼요.
_GATEWAY_TOOL_SEPARATOR = "___"
#: 도구별 ④ 승인 상태 어휘. 원장 값과 같은 문자열을 써요.
_APPROVAL_APPROVED = "APPROVED"
_APPROVAL_REQUESTED = "REQUESTED"
#: `tool_authorization_status` 어휘. `unknown` 은 **통과가 아니에요** — 그 경우 선언 전체를
#: 요구하는 옛 엄격한 기대값으로 되돌아가요.
_AUTHORIZATION_OBSERVED = "observed"
_AUTHORIZATION_UNKNOWN = "unknown"
_AUTHORIZATION_NOT_APPLICABLE = "not_applicable"
_SMOKE_PROMPT = (
    "간단히 인사하고, 지금 사용할 수 있는 도구가 있으면 이름만 알려줘."
)


@dataclass(frozen=True)
class ExpectedTool:
    """An operation expectation owned by the deployment declaration."""

    name: str
    aliases: tuple[str, ...] = ()
    sensitivity: str | None = None
    probe_arguments: dict = field(default_factory=dict)
    probe_error: str = ""

    @property
    def accepted_names(self) -> frozenset[str]:
        return frozenset((self.name, *self.aliases))


@dataclass(frozen=True)
class NegativeControl:
    """An existing Gateway READ operation not authorized for this agent."""

    tool: str
    endpoint: str
    arguments: dict = field(default_factory=dict)


@dataclass(frozen=True)
class VerifyReport:
    ok: bool
    verdict: str
    tools: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    unexpected: tuple[str, ...] = ()
    probed: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)
    reason: str = ""
    selfcheck_supported: bool = True
    negative_control_denied: bool | None = None
    negative_control_verdict: str = "unknown"
    unprobed_builtin_tools: tuple[str, ...] = ()
    #: 선언했지만 ④ 원장이 아직 부를 수 없다고 말하는 도구들 (ADR-0104, IH-153).
    #:
    #: 두 상태를 **섞지 않아요.** `pending_approval_tools` 는 `REQUESTED` ④ 행이 있는 도구
    #: (관리자 승인만 남았어요), `unrequested_tools` 는 ④ 행이 아예 없는 도구예요(권한 신청이
    #: 아직 접수되지 않았어요). 둘 다 **관측된 상태**라 `unknown` 이 아니고, 둘 다 배포를
    #: 막지 않아요 — 비-READ 도구는 첫 배포 시점에 승인될 수가 없거든요.
    #:
    #: `tool_authorization_status` 가 `unknown` 이면 원장을 읽지 못한 거예요. 그때는 위 두
    #: 목록이 비고, 선언 전체를 요구하는 **옛 엄격한 기대값**으로 되돌아가요 — 못 본 것을
    #: 「승인 대기라서 없는 것」으로 접으면 진짜 결함이 통과해요(AGENTS.md).
    #:
    #: ⚠️ **기본값은 `unknown` 이에요, `not_applicable` 이 아니에요.** ④ 분류보다 **먼저**
    #: 반환하는 리포트(selfcheck 미지원·SDK 버전 불일치·내장 도구 verifier 미배선 등)는 이
    #: 축을 아예 보지 않았어요. 기본값을 `not_applicable` 로 두면 그 리포트가 「Gateway 도구가
    #: 없음」을 **주장**해요 — 안 본 것을 다른 사실로 적는 셈이에요. `not_applicable` 은
    #: `_classify_tool_authorization` 이 실제로 「판정 대상이 0개」를 관측했을 때만 붙어요.
    pending_approval_tools: tuple[str, ...] = ()
    unrequested_tools: tuple[str, ...] = ()
    tool_authorization_status: str = "unknown"
    builtin_observability: dict | None = None
    expected_conversation_manager: dict | None = None
    conversation_manager: dict | None = None
    expected_bedrock_agentcore_version: str = (
        _BEDROCK_AGENTCORE_QUALIFIED_VERSIONS[0]
    )
    bedrock_agentcore_version: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "verdict": self.verdict,
            "tools": list(self.tools),
            "missing": list(self.missing),
            "unexpected": list(self.unexpected),
            "probed": list(self.probed),
            "warnings": list(self.warnings),
            "reason": self.reason,
            "selfcheck_supported": self.selfcheck_supported,
            "negative_control_denied": self.negative_control_denied,
            "negative_control_verdict": self.negative_control_verdict,
            "unprobed_builtin_tools": list(self.unprobed_builtin_tools),
            "pending_approval_tools": list(self.pending_approval_tools),
            "unrequested_tools": list(self.unrequested_tools),
            "tool_authorization_status": self.tool_authorization_status,
            "builtin_observability": self.builtin_observability,
            "expected_conversation_manager":
                self.expected_conversation_manager,
            "conversation_manager": self.conversation_manager,
            "expected_bedrock_agentcore_version":
                self.expected_bedrock_agentcore_version,
            "bedrock_agentcore_version": self.bedrock_agentcore_version,
        }


@dataclass(frozen=True)
class BuiltinObservabilityReport:
    """Independent control-plane observation of declared builtin resources."""

    ok: bool
    verdict: str
    resources: dict[str, str] = field(default_factory=dict)
    network_modes: dict[str, str] = field(default_factory=dict)
    spans: str = "unknown"
    metrics: str = "not_checked_until_first_use"
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "verdict": self.verdict,
            "resources": dict(self.resources),
            "network_modes": dict(self.network_modes),
            "spans": self.spans,
            "metrics": self.metrics,
            "reason": self.reason,
        }


class BuiltinToolVerifier:
    """Compare deployment-owned declarations with AgentCore control-plane state."""

    def __init__(self, *, observer, stage: str, recording_bucket: str) -> None:
        self._observer = observer
        self._stage = stage
        self._recording_bucket = recording_bucket

    def verify(
        self,
        *,
        expected_tools: tuple[str, ...],
        resources: dict,
        record_id: str,
    ) -> BuiltinObservabilityReport:
        expected = set(expected_tools)
        actual = set(resources)
        if not expected:
            if actual:
                return BuiltinObservabilityReport(
                    ok=False,
                    verdict="diverged",
                    resources={kind: "unexpected" for kind in sorted(actual)},
                    spans="not_applicable",
                    metrics="not_applicable",
                    reason="선언되지 않은 내장 도구 리소스가 배포 원장에 남아 있어요.",
                )
            return BuiltinObservabilityReport(
                ok=True,
                verdict="not_applicable",
                spans="not_applicable",
                metrics="not_applicable",
            )
        if expected != actual:
            missing = sorted(expected - actual)
            unexpected = sorted(actual - expected)
            parts = []
            if missing:
                parts.append("리소스 누락: " + ", ".join(missing))
            if unexpected:
                parts.append("선언 외 리소스: " + ", ".join(unexpected))
            return BuiltinObservabilityReport(
                ok=False,
                verdict="diverged",
                resources={
                    kind: ("missing" if kind in missing else "unexpected")
                    for kind in sorted(expected | actual)
                    if kind in missing or kind in unexpected
                },
                reason="; ".join(parts),
            )

        results: dict[str, str] = {}
        network_modes: dict[str, str] = {}
        for kind in expected_tools:
            resource = resources.get(kind)
            if not isinstance(resource, dict):
                results[kind] = "failed"
                return BuiltinObservabilityReport(
                    ok=False,
                    verdict="diverged",
                    resources=results,
                    reason=f"{kind} 배포 원장 descriptor가 올바르지 않아요.",
                )
            resource_id = str(resource.get("id") or "")
            resource_arn = str(resource.get("arn") or "")
            if not resource_id or not resource_arn:
                results[kind] = "failed"
                return BuiltinObservabilityReport(
                    ok=False,
                    verdict="diverged",
                    resources=results,
                    reason=f"{kind} 리소스 ID 또는 ARN이 배포 원장에 없어요.",
                )
            try:
                observed = self._observer.observe_builtin_tool(kind, resource_id)
            except Exception as exc:
                results[kind] = "unknown"
                return BuiltinObservabilityReport(
                    ok=False,
                    verdict="unknown",
                    resources=results,
                    reason=(
                        f"{kind} 제어플레인 상태를 관측할 수 없어요: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                )
            mismatches = []
            if observed.get("id") != resource_id:
                mismatches.append("id")
            if observed.get("arn") != resource_arn:
                mismatches.append("arn")
            if observed.get("status") != "READY":
                mismatches.append("status")
            expected_network_mode = str(
                resource.get("network_mode") or ""
            )
            observed_network_mode = str(
                observed.get("network_mode") or ""
            )
            network_modes[kind] = observed_network_mode
            if (
                not expected_network_mode
                or observed_network_mode != expected_network_mode
            ):
                mismatches.append("networkConfiguration.networkMode")
            tags = observed.get("tags")
            if not isinstance(tags, dict) or tags.get(
                "agora:record-id"
            ) != record_id:
                mismatches.append("tags.agora:record-id")
            if not isinstance(tags, dict) or tags.get(
                "agora:stage"
            ) != self._stage:
                mismatches.append("tags.agora:stage")
            if not isinstance(tags, dict) or tags.get(
                "agora:network-mode"
            ) != expected_network_mode:
                mismatches.append("tags.agora:network-mode")
            if kind == "browser":
                recording = observed.get("recording")
                location = (
                    recording.get("s3Location")
                    if isinstance(recording, dict)
                    else None
                )
                expected_prefix = str(resource.get("recording_prefix") or "")
                if not isinstance(recording, dict) or recording.get(
                    "enabled"
                ) is not True:
                    mismatches.append("recording.enabled")
                if not isinstance(location, dict) or location.get(
                    "bucket"
                ) != self._recording_bucket:
                    mismatches.append("recording.s3Location.bucket")
                if not isinstance(location, dict) or location.get(
                    "prefix"
                ) != expected_prefix or not expected_prefix:
                    mismatches.append("recording.s3Location.prefix")
            if mismatches:
                results[kind] = "failed"
                return BuiltinObservabilityReport(
                    ok=False,
                    verdict="diverged",
                    resources=results,
                    network_modes=network_modes,
                    reason=(
                        f"{kind} 제어플레인 상태가 선언과 달라요: "
                        + ", ".join(mismatches)
                    ),
                )
            results[kind] = "verified"

        # Transaction Search is an account-wide prerequisite and is currently
        # unmet in dev. Policy permits deployment but never records this as pass.
        return BuiltinObservabilityReport(
            ok=True,
            verdict="unknown",
            resources=results,
            network_modes=network_modes,
            spans="unknown",
        )


def _session_id() -> str:
    """AgentCore runtimeSessionId must contain at least 33 characters."""
    # Sessions get dedicated microVMs; ID reuse shares one microVM and cached Agent.
    return f"agoraverify{uuid.uuid4().hex}"


_STATUS_FIELDS = frozenset({"status", "status_code", "http_status"})
#: interceptor 거부의 JSON-RPC 코드예요 (`gateway_request_interceptor.py` `_deny`).
#: 2026-08-31(IH-128) 부터 그 거부는 **HTTP 200 + JSON-RPC error** 로 와요 — status 만 보면
#: 「거부 candidate 를 하나도 못 봤다」로 읽혀서 진단이 엉뚱한 곳을 탓하게 돼요.
_INTERCEPTOR_DENIAL_CODE = -32001


def _contains_field(value, *, fields: frozenset[str], expected: int) -> bool:
    """Inspect an untrusted observation without unbounded recursion."""
    stack = [(value, 0)]
    visited = 0
    while stack and visited < _OBSERVATION_NODE_LIMIT:
        current, depth = stack.pop()
        visited += 1
        if depth > _OBSERVATION_DEPTH_LIMIT:
            continue
        if isinstance(current, dict):
            for index, (key, item) in enumerate(current.items()):
                if index >= _OBSERVATION_FIELD_LIMIT:
                    break
                if key in fields and item == expected:
                    return True
                stack.append((item, depth + 1))
        elif isinstance(current, (list, tuple)):
            stack.extend(
                (item, depth + 1)
                for item in current[:_OBSERVATION_FIELD_LIMIT]
            )
    return False


def _contains_status(value, expected: int) -> bool:
    return _contains_field(value, fields=_STATUS_FIELDS, expected=expected)


def _is_sensitive_field(name: object) -> bool:
    normalized = str(name).strip().lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_FIELD_PARTS)


def _safe_diagnostic_text(value: object) -> str:
    text = str(value)
    text = _BEARER_RE.sub("<redacted:bearer>", text)
    text = _EMAIL_RE.sub("<redacted:email>", text)
    return _SECRET_ASSIGNMENT_RE.sub(
        "<redacted:secret-assignment>",
        text,
    )


def _safe_message(value: object) -> str:
    text = _safe_diagnostic_text(value)
    if len(text) > _OBSERVATION_VALUE_LIMIT:
        return text[:_OBSERVATION_VALUE_LIMIT] + "...[truncated]"
    return text


def _safe_coordinate(value: object):
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str) and _SAFE_COORDINATE_RE.fullmatch(value):
        return value
    return None


def _value_length(value: object) -> int | None:
    if isinstance(value, (str, bytes, list, tuple, dict)):
        return len(value)
    return None


def _data_keys(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []
    keys = []
    for index, key in enumerate(value):
        if index >= _OBSERVATION_FIELD_LIMIT:
            break
        keys.append(str(key)[:80])
    return keys


def _mcp_error_coordinates(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    output = {}
    code = _safe_coordinate(value.get("code"))
    if code is not None:
        output["code"] = code
    if "message" in value:
        output["message"] = _safe_message(value["message"])
    keys = _data_keys(value.get("data"))
    if keys:
        output["data_keys"] = keys
    if isinstance(value.get("redaction"), dict):
        output["redaction"] = {
            str(key)[:80]: item
            for key, item in value["redaction"].items()
            if isinstance(item, int)
        }
    return output


def _probe_result_coordinates(value: object, *, tool: str = "") -> dict:
    if not isinstance(value, dict):
        return {}
    output = {}
    if tool:
        output["tool"] = tool
    # Gateway 인가 거부 사유 (IH-183). 배포 agent 가 보낸 값이라 **알려진 슬러그만** 남겨요 —
    # `_safe_coordinate` 만 통과시키면 모르는 문자열이 그대로 화면에 실려요.
    if denial_message_for_reason(value.get("denial_reason")):
        output["denial_reason"] = str(value["denial_reason"]).strip()
    for name in (
        "error_type",
        "code",
        "http_status",
        "status",
        "request_id",
        "trace_id",
        "content_length",
        "structured_content_length",
        "isError",
    ):
        observed = _safe_coordinate(value.get(name))
        if observed is not None:
            output[name] = observed
    if "content_length" not in output:
        content_length = _value_length(value.get("content"))
        if content_length is not None:
            output["content_length"] = content_length
    if "structured_content_length" not in output:
        structured = value.get("structuredContent")
        if structured is None:
            structured = value.get("structured_content")
        structured_length = _value_length(structured)
        if structured_length is not None:
            output["structured_content_length"] = structured_length
    return output


def _jsonrpc_error_coordinates(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    output = {}
    code = _safe_coordinate(value.get("code"))
    if code is not None:
        output["code"] = code
    if "message" in value:
        output["message"] = _safe_message(value["message"])
    keys = _data_keys(value.get("data"))
    if keys:
        output["data_keys"] = keys
    return output


def _positive_probe_error_coordinates(
    data: object,
    error: object,
    *,
    tool: str,
) -> dict:
    output = {
        "tool": tool,
        "error_type": "jsonrpc_error",
    }
    if isinstance(data, dict):
        request_id = _safe_coordinate(data.get("id"))
        if request_id is not None:
            output["request_id"] = request_id
        output.update(_probe_result_coordinates(data))
    if isinstance(error, dict):
        output.update(_probe_result_coordinates(error))
    return output


def _bounded_observation(value, *, depth: int = 0, budget: list[int] | None = None):
    """Sanitize one explicitly allowlisted diagnostic field."""
    if budget is None:
        budget = [_OBSERVATION_NODE_LIMIT]
    if budget[0] <= 0 or depth > _OBSERVATION_DEPTH_LIMIT:
        return "<omitted:observation-limit>"
    budget[0] -= 1
    if isinstance(value, dict):
        output = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _OBSERVATION_FIELD_LIMIT:
                output["_omitted_fields"] = len(value) - index
                break
            safe_key = str(key)[:80]
            if safe_key.lower() in {"body", "raw_body", "response_body"}:
                output[safe_key] = "<omitted:raw-body>"
            elif _is_sensitive_field(safe_key):
                output[safe_key] = "<redacted:sensitive-field>"
            else:
                output[safe_key] = _bounded_observation(
                    item,
                    depth=depth + 1,
                    budget=budget,
                )
        return output
    if isinstance(value, (list, tuple)):
        output = [
            _bounded_observation(item, depth=depth + 1, budget=budget)
            for item in value[:_OBSERVATION_FIELD_LIMIT]
        ]
        if len(value) > _OBSERVATION_FIELD_LIMIT:
            output.append(
                f"<omitted:{len(value) - _OBSERVATION_FIELD_LIMIT}-items>"
            )
        return output
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str) and _SAFE_ENUM_RE.fullmatch(value):
        return value
    return "<omitted:string>"


def _allowlisted_exception(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    output = {}
    for key in ("relation", "type", "http_status", "status", "request_id", "trace_id"):
        observed = _safe_coordinate(value.get(key))
        if observed is not None:
            output[key] = observed
    if "message" in value:
        output["message"] = _safe_message(value["message"])
    mcp_error = _mcp_error_coordinates(value.get("mcp_error"))
    if mcp_error:
        output["mcp_error"] = mcp_error
    if isinstance(value.get("redaction"), dict):
        output["redaction"] = {
            str(key)[:80]: item
            for key, item in value["redaction"].items()
            if isinstance(item, int)
        }
    return output


def _allowlisted_candidate(
    value: object,
    *,
    depth: int = 0,
    budget: list[int] | None = None,
) -> dict:
    """Retain diagnostic coordinates, never an arbitrary subject response."""
    if budget is None:
        budget = [_OBSERVATION_NODE_LIMIT]
    if (
        not isinstance(value, dict)
        or depth > _OBSERVATION_DEPTH_LIMIT
        or budget[0] <= 0
    ):
        return {}
    budget[0] -= 1
    output = {}
    for key in (
        "outcome",
        "source",
        "kind",
        "phase",
        "http_status",
        "status",
        "request_id",
        "trace_id",
        "tool",
    ):
        observed = _safe_coordinate(value.get(key))
        if observed is not None:
            output[key] = observed
    gateway_identifier = value.get("gateway_identifier")
    if isinstance(gateway_identifier, dict):
        identifier = {}
        for key in ("code", "reason"):
            observed = _safe_coordinate(gateway_identifier.get(key))
            if observed is not None:
                identifier[key] = observed
        if identifier:
            output["gateway_identifier"] = identifier
    protocol_error = _probe_result_coordinates(value.get("protocol_error"))
    if protocol_error:
        output["protocol_error"] = protocol_error
    request_id = _safe_coordinate(value.get("id"))
    if request_id is not None:
        output["id"] = request_id
    jsonrpc_error = _jsonrpc_error_coordinates(value.get("error"))
    if jsonrpc_error:
        output["error"] = jsonrpc_error
    exceptions = value.get("exceptions")
    if isinstance(exceptions, (list, tuple)):
        output["exceptions"] = [
            detail
            for item in exceptions[:_OBSERVATION_FIELD_LIMIT]
            if (detail := _allowlisted_exception(item))
        ]
    candidate = value.get("candidate")
    if isinstance(candidate, dict):
        output["candidate"] = _allowlisted_candidate(
            candidate,
            depth=depth + 1,
            budget=budget,
        )
    return output


def _render_observation(value) -> str:
    text = json.dumps(
        _allowlisted_candidate(value),
        ensure_ascii=False,
        default=str,
    )
    if len(text) > _OBSERVATION_TEXT_LIMIT:
        return text[:_OBSERVATION_TEXT_LIMIT] + "...[truncated]"
    return text


def _exception_reason(error: BaseException) -> str:
    details: list[str] = []
    seen: set[int] = set()
    stack = [error]
    while stack and len(details) < 12:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        message = _safe_diagnostic_text(current)
        if len(message) > 500:
            message = message[:500] + "...[truncated]"
        detail = f"{type(current).__name__}: {message}"
        response = getattr(current, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            detail += f" [status={status}]"
        if response is not None:
            headers = getattr(response, "headers", None)
            if headers is not None:
                for name, label in (
                    ("x-amzn-requestid", "request_id"),
                    ("x-amz-request-id", "request_id"),
                    ("x-amzn-trace-id", "trace_id"),
                ):
                    observed = _safe_coordinate(headers.get(name))
                    if observed is not None:
                        detail += f" [{label}={observed}]"
        details.append(detail)
        if isinstance(current, BaseExceptionGroup):
            remaining = 12 - len(details)
            stack.extend(reversed(current.exceptions[:remaining]))
        if current.__cause__ is not None:
            stack.append(current.__cause__)
        if (
            current.__context__ is not None
            and current.__context__ is not current.__cause__
        ):
            stack.append(current.__context__)
    return " | ".join(details)


class AgentVerifier:
    """Probe a deployed agent through its externally reachable A2A endpoint."""

    def __init__(
        self,
        *,
        invoker,
        qualification_scope: str,
        qualification_store,
        sleep=None,
        now=None,
        monotonic=None,
        attempts: int = _ATTEMPTS,
        backoff_sec: float = _BACKOFF_SEC,
        total_timeout_sec: float = _TOTAL_TIMEOUT_SEC,
        max_probes: int = _MAX_PROBES,
        require_negative_control: bool | None = None,
        issue_call_handle=None,
    ) -> None:
        self._invoker = invoker
        # IA-61 후속(2026-08-29): Gateway REQUEST interceptor 가 붙은 뒤로 **검증 호출도**
        # `X-Agora-Call` handle 이 필요해요. handle 이 없으면 `initialize` 부터 거부돼서
        # (`gateway_interceptor_denied method=initialize reason=invalid_delegation`, 실측)
        # agent 가 도구를 하나도 못 받고 "선언된 도구가 없어요" 로 실패해요.
        #
        # 실사용 경로(`message/send`)와 **같은 방식으로** handle 을 실어요. 검증만 다른 길로
        # 가면 "검증은 통과했는데 실사용에서 막힘" 이 생겨요.
        #
        # 도메인 경계 때문에 `identity` 를 직접 import 하지 않고 주입받아요.
        # `None` 이면 handle 없이 호출해요 — 테스트·interceptor 없는 Gateway 호환이에요.
        self._issue_call_handle = issue_call_handle
        self._qualification_scope = qualification_scope
        self._qualification_store = qualification_store
        self._attempts = max(1, attempts)
        self._backoff = backoff_sec
        self._total_timeout_sec = max(0.1, total_timeout_sec)
        self._max_probes = max(1, max_probes)
        # 음성 대조를 못 돌렸을 때 배포를 막을지. **기본은 막지 않아요** — 관측 불가는
        # `unknown` 이고, 진행 차단은 명시적 정책이어야 해요(AGENTS.md).
        # `AGORA_DEPLOY_REQUIRE_NEGATIVE_CONTROL=1` 로 켜면 막아요.
        if require_negative_control is None:
            import os
            require_negative_control = os.getenv(
                "AGORA_DEPLOY_REQUIRE_NEGATIVE_CONTROL", ""
            ).lower() in ("1", "true", "yes")
        self._require_negative_control = require_negative_control
        if sleep is None:
            import time

            sleep = time.sleep
        self._sleep = sleep
        if now is None:
            import time

            now = time.time
        self._now = now
        if monotonic is None:
            import time

            monotonic = time.monotonic
        self._monotonic = monotonic

    def verify(
        self,
        *,
        runtime_arn: str,
        expected_tools: tuple[ExpectedTool, ...],
        negative_control: NegativeControl | None,
        policy_revision: str,
        actor_id: str,
        memory_required: bool,
        expected_conversation_manager: dict | None = None,
        expected_memory_mode: str | None = None,
        expected_memory_namespaces: tuple[str, ...] | None = None,
        unprobed_builtin_tools: tuple[str, ...] = (),
        call_handle: str | None = None,
        tool_approval_states: dict[str, str] | None = None,
        tool_authorization_reason: str = "",
    ) -> VerifyReport:
        """`tool_approval_states` 는 ④ 원장이 말하는 `gateway_action → approval_state` 예요.

        기대값의 소유자를 못박아요(ADR-0037 §4): 어느 도구가 「승인 대기」인지는 **원장**이
        정해요 — 컴파일러 산출물도, runtime 자기보고도 아니에요. `None` 은 원장을 읽지
        못했다는 뜻(`unknown`)이고, 그때는 선언 전체를 요구하는 옛 기대값으로 되돌아가요.
        """
        deadline = self._monotonic() + self._total_timeout_sec
        self._call_handle = call_handle
        state = self._selfcheck(runtime_arn, actor_id, deadline)
        if isinstance(state, VerifyReport):
            return state
        result, warnings = state
        if result is None:
            return VerifyReport(
                ok=False,
                verdict="unknown",
                warnings=tuple(warnings),
                reason="agora/selfcheck를 지원하지 않아 등록 도구를 관측할 수 없어요.",
                selfcheck_supported=False,
            )

        errors = tuple(result.get("errors") or ())
        if errors:
            return VerifyReport(
                ok=False,
                verdict="diverged",
                warnings=tuple(warnings),
                reason="; ".join(str(error) for error in errors),
            )

        installed_sdk = result.get("bedrock_agentcore_version")
        if not isinstance(installed_sdk, str) or not installed_sdk:
            return VerifyReport(
                ok=False,
                verdict="unknown",
                warnings=tuple(warnings),
                reason=(
                    "배포 Runtime에 설치된 bedrock-agentcore 버전을 "
                    "관측할 수 없어요."
                ),
                bedrock_agentcore_version=(
                    installed_sdk if isinstance(installed_sdk, str) else None
                ),
            )
        if installed_sdk not in _BEDROCK_AGENTCORE_QUALIFIED_VERSIONS:
            return VerifyReport(
                ok=False,
                verdict="diverged",
                warnings=tuple(warnings),
                reason=(
                    "배포 Runtime의 bedrock-agentcore가 검증된 버전과 "
                    f"달라요: actual={installed_sdk}, qualified="
                    f"{','.join(_BEDROCK_AGENTCORE_QUALIFIED_VERSIONS)}"
                ),
                bedrock_agentcore_version=installed_sdk,
            )

        # ④ 원장이 말하는 「지금 부를 수 없는 도구」를 먼저 갈라요 (ADR-0104, IH-153).
        #
        # 왜 필요한가: 비-READ 도구는 **첫 배포 시점에 승인될 수가 없어요.** 그런데 VERIFYING
        # 은 선언 전체를 runtime 도구 목록에서 찾았고, 미승인 도구는 Gateway 가 `tools/list`
        # 를 공유 Cedar 로 필터해서 안 보였어요. 그래서 CREATE·UPDATE·DELETE 를 하나라도
        # 고른 agent 는 첫 배포를 통과할 수 없었어요(2026-09-04 실측).
        #
        # 여기서 갈라낸 도구는 `missing` 에서 빠지지만 **리포트에는 남아요** — 조용히
        # 통과시키면 사용자가 「배포됐으니 다 쓸 수 있다」고 읽어요.
        (
            pending_approval_tools,
            unrequested_tools,
            authorization_status,
        ) = self._classify_tool_authorization(
            expected_tools, tool_approval_states
        )
        if authorization_status == _AUTHORIZATION_UNKNOWN:
            warnings.append(
                "④ tool binding 원장을 읽지 못해 도구별 승인 상태를 관측하지 못했어요 — "
                "선언 전체를 요구하는 엄격한 기대값으로 검증해요"
                + (
                    f" ({tool_authorization_reason})"
                    if tool_authorization_reason
                    else ""
                )
            )
        # 조용히 통과시키지 않아요 — 배포가 성공해도 이 도구들은 아직 못 불러요.
        # 경고에 넣으면 실패한 배포의 리포트에도 남아서, 다른 이유로 실패했을 때에도
        # 「이 도구는 승인 대기」라는 사실이 사라지지 않아요.
        if pending_approval_tools:
            warnings.append(
                f"도구 {len(pending_approval_tools)}개가 관리자 승인 대기 중이에요: "
                + ", ".join(pending_approval_tools)
            )
        if unrequested_tools:
            warnings.append(
                f"도구 {len(unrequested_tools)}개는 권한 신청이 접수되지 않았어요: "
                + ", ".join(unrequested_tools)
            )
        report_for_sdk = partial(
            VerifyReport,
            bedrock_agentcore_version=installed_sdk,
            pending_approval_tools=pending_approval_tools,
            unrequested_tools=unrequested_tools,
            tool_authorization_status=authorization_status,
        )
        conversation_manager = result.get("conversation_manager")
        if expected_conversation_manager is not None:
            if "conversation_manager" not in result:
                return report_for_sdk(
                    ok=False,
                    verdict="unknown",
                    warnings=tuple(warnings),
                    reason=(
                        "배포 Runtime의 conversation manager 설정을 "
                        "관측할 수 없어요."
                    ),
                    expected_conversation_manager=(
                        expected_conversation_manager
                    ),
                )
            if conversation_manager != expected_conversation_manager:
                return report_for_sdk(
                    ok=False,
                    verdict="diverged",
                    warnings=tuple(warnings),
                    reason=(
                        "배포 Runtime의 conversation manager가 "
                        "원장 선언과 달라요."
                    ),
                    expected_conversation_manager=(
                        expected_conversation_manager
                    ),
                    conversation_manager=conversation_manager,
                )

        memory = result.get("memory")
        memory_mode = expected_memory_mode or (
            "MANAGED" if memory_required else None
        )
        if memory_mode == "DISABLED":
            if not isinstance(memory, dict):
                return report_for_sdk(
                    ok=False,
                    verdict="unknown",
                    warnings=tuple(warnings),
                    reason="DISABLED Memory runtime 상태를 관측할 수 없어요.",
                    expected_conversation_manager=expected_conversation_manager,
                    conversation_manager=conversation_manager,
                )
            if (
                memory.get("status") == "degraded"
                or memory.get("session_manager_attached") is not False
                or memory.get("mode") != "DISABLED"
            ):
                return report_for_sdk(
                    ok=False,
                    verdict="diverged",
                    warnings=tuple(warnings),
                    reason=str(
                        memory.get("reason")
                        or "DISABLED Memory 선언과 runtime 부착 상태가 달라요."
                    ),
                    expected_conversation_manager=expected_conversation_manager,
                    conversation_manager=conversation_manager,
                )
            if memory.get("configuration_status") != "ok":
                return report_for_sdk(
                    ok=False,
                    verdict="unknown",
                    warnings=tuple(warnings),
                    reason="DISABLED Memory runtime 구성을 관측할 수 없어요.",
                    expected_conversation_manager=expected_conversation_manager,
                    conversation_manager=conversation_manager,
                )
        if (
            memory_required
            and isinstance(memory, dict)
            and memory.get("status") == "degraded"
        ):
            return report_for_sdk(
                ok=False,
                verdict="diverged",
                warnings=tuple(warnings),
                reason=str(
                    memory.get("reason")
                    or "MANAGED Memory가 degraded 상태예요."
                ),
                expected_conversation_manager=expected_conversation_manager,
                conversation_manager=conversation_manager,
            )
        if memory_required and not (
            isinstance(memory, dict)
            and memory.get("session_manager_attached") is True
            and memory.get("memory_id_present") is True
        ):
            return report_for_sdk(
                ok=False,
                verdict="diverged",
                warnings=tuple(warnings),
                reason=(
                    "MANAGED Memory session manager가 실제 Agent에 "
                    "부착된 것을 관측하지 못했어요."
                ),
                expected_conversation_manager=expected_conversation_manager,
                conversation_manager=conversation_manager,
            )
        if memory_required and memory.get("configuration_status") != "ok":
            return report_for_sdk(
                ok=False,
                verdict="unknown",
                warnings=tuple(warnings),
                reason=(
                    "MANAGED Memory runtime 구성을 관측할 수 없어요."
                ),
                expected_conversation_manager=expected_conversation_manager,
                conversation_manager=conversation_manager,
            )
        if memory_required:
            if not expected_memory_namespaces:
                return report_for_sdk(
                    ok=False,
                    verdict="unknown",
                    warnings=tuple(warnings),
                    reason=(
                        "원장에서 MANAGED Memory retrieval namespace "
                        "기대값을 읽을 수 없어요."
                    ),
                    expected_conversation_manager=expected_conversation_manager,
                    conversation_manager=conversation_manager,
                )
            runtime_namespaces = memory.get("runtime_retrieval_namespaces")
            if (
                not isinstance(runtime_namespaces, list)
                or not runtime_namespaces
                or any(
                    not isinstance(namespace, str) or not namespace.strip()
                    for namespace in runtime_namespaces
                )
            ):
                return report_for_sdk(
                    ok=False,
                    verdict="unknown",
                    warnings=tuple(warnings),
                    reason=(
                        "MANAGED Memory retrieval namespace를 "
                        "관측할 수 없어요."
                    ),
                    expected_conversation_manager=expected_conversation_manager,
                    conversation_manager=conversation_manager,
                )
            if sorted(runtime_namespaces) != sorted(expected_memory_namespaces):
                return report_for_sdk(
                    ok=False,
                    verdict="diverged",
                    warnings=tuple(warnings),
                    reason=(
                        "MANAGED Memory retrieval namespace가 "
                        "원장 선언과 달라요."
                    ),
                    expected_conversation_manager=expected_conversation_manager,
                    conversation_manager=conversation_manager,
                )

        tools = tuple(dict.fromkeys(str(tool) for tool in result.get("tools") or ()))
        actual = set(tools)
        # 아직 부를 수 없는 도구는 목록에 있든 없든 `missing` 이 아니에요 — 그 사실은
        # `pending_approval_tools`·`unrequested_tools` 가 따로 말해요.
        blocked_names = set(pending_approval_tools) | set(unrequested_tools)
        missing = tuple(
            expected.name
            for expected in expected_tools
            if expected.name not in blocked_names
            and not actual.intersection(expected.accepted_names)
        )
        missing += tuple(
            name for name in unprobed_builtin_tools if name not in actual
        )
        allowed = set().union(
            *(expected.accepted_names for expected in expected_tools)
        ) if expected_tools else set()
        allowed.update(unprobed_builtin_tools)
        unexpected = tuple(tool for tool in tools if tool not in allowed)
        if missing or unexpected:
            parts = []
            if missing and not tools and expected_tools:
                # 하나도 못 받았어요. 선언 불일치가 아니라 **Gateway 가 `initialize` 부터
                # 막은** 모양이에요 — 도구가 일부만 빠지면 선언 문제지만, 전멸은 인가예요.
                #
                # 2026-08-29 에 이 구분이 없어서 낡은 interceptor Lambda 가 원인인 실패를
                # "도구 선언이 틀렸다" 로 읽었어요. 진단이 한 시간 걸렸어요.
                parts.append(
                    "runtime 이 도구를 하나도 받지 못했어요 — Gateway 인가가 "
                    "`initialize` 에서 막혔을 수 있어요. interceptor 로그를 보세요"
                    f" (기대: {', '.join(missing)})"
                )
            elif missing:
                parts.append(f"선언된 도구가 없어요: {', '.join(missing)}")
            if unexpected:
                parts.append(f"선언되지 않은 도구가 노출됐어요: {', '.join(unexpected)}")
            return report_for_sdk(
                ok=False,
                verdict="diverged",
                tools=tools,
                missing=missing,
                unexpected=unexpected,
                warnings=tuple(warnings),
                reason=" / ".join(parts),
                unprobed_builtin_tools=unprobed_builtin_tools,
            )

        if not expected_tools:
            smoke_error = self._smoke(runtime_arn, actor_id, deadline)
            if smoke_error:
                return report_for_sdk(
                    ok=False,
                    verdict="diverged",
                    tools=tools,
                    warnings=tuple(warnings),
                    reason=smoke_error,
                    unprobed_builtin_tools=unprobed_builtin_tools,
                    expected_conversation_manager=(
                        expected_conversation_manager
                    ),
                    conversation_manager=conversation_manager,
                )
            if unprobed_builtin_tools:
                return report_for_sdk(
                    ok=True,
                    verdict="unknown",
                    tools=tools,
                    warnings=tuple(warnings),
                    reason=(
                        "선언된 내장 도구의 이름은 관측했지만 "
                        "도달성 probe는 실행하지 않았어요."
                    ),
                    unprobed_builtin_tools=unprobed_builtin_tools,
                    expected_conversation_manager=(
                        expected_conversation_manager
                    ),
                    conversation_manager=conversation_manager,
                )
            return report_for_sdk(
                ok=True,
                verdict="not_applicable",
                tools=tools,
                warnings=tuple(warnings),
                expected_conversation_manager=expected_conversation_manager,
                conversation_manager=conversation_manager,
            )

        unclassified = tuple(
            expected
            for expected in expected_tools
            # 부를 수 없는 도구는 probe 하지 않으니 probe 안전성을 판정할 필요도 없어요.
            if expected.name not in blocked_names
            and expected.sensitivity is None
            and expected.probe_error
        )
        if unclassified:
            return report_for_sdk(
                ok=False,
                verdict="unknown",
                tools=tools,
                warnings=tuple(warnings),
                reason="operation probe 안전성을 확인할 수 없어요: "
                + "; ".join(
                    f"{tool.name}: {tool.probe_error}" for tool in unclassified
                ),
            )

        # 도달성 probe 는 **부를 수 있는** READ 도구만 대상이에요. 승인 대기·미신청 READ 를
        # 부르면 interceptor 가 정당하게 거부하고, 그 거부가 「도구가 안 된다」로 읽혀요.
        readable = tuple(
            expected
            for expected in expected_tools
            if expected.sensitivity == "READ"
            and expected.name not in blocked_names
        )
        if not readable:
            return report_for_sdk(
                ok=False,
                verdict="unknown",
                tools=tools,
                warnings=tuple(warnings),
                reason=(
                    "READ operation이 없어 도구 도달성을 안전하게 관측할 수 없어요. "
                    "배포하려면 부작용 없는 READ qualification operation 계약이 필요해요."
                    + (
                        " (선언된 READ operation 이 있지만 전부 ④ 승인 대기·미신청이에요 — "
                        "ReadOnly baseline 승인이 돌지 않았는지 확인해 주세요.)"
                        if any(
                            expected.sensitivity == "READ"
                            for expected in expected_tools
                        )
                        else ""
                    )
                ),
            )
        unprobeable = tuple(tool for tool in readable if tool.probe_error)
        if unprobeable:
            return report_for_sdk(
                ok=False,
                verdict="unknown",
                tools=tools,
                warnings=tuple(warnings),
                reason="READ probe 입력을 만들 수 없어요: "
                + "; ".join(
                    f"{tool.name}: {tool.probe_error}" for tool in unprobeable
                ),
            )
        if len(readable) > self._max_probes:
            return report_for_sdk(
                ok=False,
                verdict="unknown",
                tools=tools,
                warnings=tuple(warnings),
                reason=(
                    "READ 도달성 probe 개수가 검증 상한을 넘어요: "
                    f"{len(readable)} > {self._max_probes}"
                ),
            )
        # 음성 대조 후보가 없는 것은 **관측 불가**예요 — 배포 실패가 아니에요.
        #
        # 후보는 "Gateway 에 있고 이 agent 에는 미승인인 안전한 READ" 예요. 그런데 자산의
        # READ 를 **전부** 승인받는 건 정상 사용이고(도구 4개짜리 MCP 에서 READ 2개를 다
        # 고르면 후보가 0 이에요), 그때 배포가 실패하면 안 돼요.
        #
        # AGENTS.md: "`unknown` 을 통과로 기록·표시하지 말되, **진행을 막을지는 명시적이고
        # 문서화된 정책 스위치**로만 정하고 암묵적 기본값으로 두지 말 것." 예전 코드는
        # `ok=False` 로 암묵적으로 막았어요.
        #
        # 게다가 비대칭이었어요 — 음성 대조가 **돌았지만 증거가 신뢰 불가**한 경우(이 함수
        # 끝의 `unknown_authorization`)는 `ok=True` 로 통과시키면서, **아예 못 돌린** 경우만
        # 막았어요. 두 경우 모두 "강제됨을 증명하지 못함" 이라 같게 다뤄야 해요.
        skip_negative_control = negative_control is None
        if skip_negative_control:
            warnings.append(
                "인가 negative control 후보가 없어요 — Gateway 에 있고 이 agent 에는 "
                "미승인인 안전한 READ operation 이 하나도 없어요(승인된 READ 가 자산의 "
                "READ 전부일 때 정상적으로 생기는 상태예요). 거부를 관측하지 못했으니 "
                "인가 강제는 `unknown` 이에요 — 검증됨으로 읽지 마세요."
            )
            if self._require_negative_control:
                return report_for_sdk(
                    ok=False,
                    verdict="unknown",
                    tools=tools,
                    warnings=tuple(warnings),
                    reason=(
                        "인가 negative control 후보가 없고 "
                        "AGORA_DEPLOY_REQUIRE_NEGATIVE_CONTROL=1 이라 배포를 막아요."
                    ),
                )
        # revision 부재도 같은 부류예요 — 음성 대조를 못 돌리는 조건이라 `unknown` 으로
        # 남기고 진행해요. 막는 건 정책 스위치가 켜졌을 때만이에요.
        if not policy_revision and not skip_negative_control:
            skip_negative_control = True
            warnings.append(
                "Cedar policy revision 을 확인할 수 없어 인가 negative control 을 "
                "실행하지 못했어요 — 인가 강제는 `unknown` 이에요."
            )
            if self._require_negative_control:
                return report_for_sdk(
                    ok=False,
                    verdict="unknown",
                    tools=tools,
                    warnings=tuple(warnings),
                    reason=(
                        "Cedar policy revision 을 확인할 수 없고 "
                        "AGORA_DEPLOY_REQUIRE_NEGATIVE_CONTROL=1 이라 배포를 막아요."
                    ),
                )

        if self._remaining(deadline) <= 0:
            return self._deadline_report(
                tools,
                warnings,
                bedrock_agentcore_version=installed_sdk,
                pending_approval_tools=pending_approval_tools,
                unrequested_tools=unrequested_tools,
                tool_authorization_status=authorization_status,
            )

        observed_names = tuple(
            next(
                name
                for name in (expected.name, *expected.aliases)
                if name in actual
            )
            for expected in readable
        )
        task_count = len(readable) + (0 if skip_negative_control else 1)
        task_count = max(1, task_count)
        # Cap concurrent cold starts/LLM calls at six to bound runtime load.
        # The negative control still runs once per deployment even when a READ
        # probe fails. Smoke stays sequential because its free-form prompt can
        # invoke state-changing tools and must not run after an earlier failure.
        with ThreadPoolExecutor(
            max_workers=min(task_count, _MAX_PARALLEL_PROBES)
        ) as executor:
            read_futures = tuple(
                executor.submit(
                    self._call_tool,
                    runtime_arn,
                    observed_name,
                    expected.probe_arguments,
                    actor_id,
                    deadline,
                )
                for expected, observed_name in zip(
                    readable, observed_names, strict=True
                )
            )
            # 후보가 없으면 이 task 를 아예 만들지 않아요. READ 도달성 probe 는 그대로
            # 돌려요 — 승인된 도구가 실제로 동작하는지는 음성 대조와 별개 관측이에요.
            authorization_future = (
                None if skip_negative_control
                else executor.submit(
                    self._qualify_negative_control,
                    runtime_arn,
                    negative_control,
                    policy_revision,
                    actor_id,
                    deadline,
                )
            )

        read_observations = tuple(
            future.result() for future in read_futures
        )
        if authorization_future is None:
            # 관측하지 못했으니 `unknown` 이에요. `allowed`(=강제 실패)도, `denied`(=강제
            # 확인)도 주장할 수 없어요.
            authorization_verdict, authorization_warning = "unknown", ""
        else:
            authorization_verdict, authorization_warning = (
                authorization_future.result()
            )
        probed = [
            expected.name
            for expected, observation in zip(
                readable, read_observations, strict=True
            )
            if observation is None
        ]
        for observation in read_observations:
            if observation is not None:
                verdict, reason = observation
                return report_for_sdk(
                    ok=False,
                    verdict=verdict,
                    tools=tools,
                    probed=tuple(probed),
                    warnings=tuple(warnings),
                    reason=reason,
                )

        if authorization_verdict == "diverged":
            return report_for_sdk(
                ok=False,
                verdict=authorization_verdict,
                tools=tools,
                probed=tuple(probed),
                warnings=tuple(warnings),
                reason=authorization_warning,
                negative_control_denied=False,
                negative_control_verdict="allowed",
            )

        if self._remaining(deadline) <= 0:
            return self._deadline_report(
                tools,
                warnings,
                probed,
                bedrock_agentcore_version=installed_sdk,
                pending_approval_tools=pending_approval_tools,
                unrequested_tools=unrequested_tools,
                tool_authorization_status=authorization_status,
            )
        report_warnings = tuple(warnings)
        if authorization_warning:
            report_warnings += (authorization_warning,)
        smoke_error = self._smoke(runtime_arn, actor_id, deadline)
        if smoke_error:
            return report_for_sdk(
                ok=False,
                verdict="diverged",
                tools=tools,
                probed=tuple(probed),
                warnings=report_warnings,
                reason=smoke_error,
                negative_control_denied=None,
                negative_control_verdict="unknown",
            )
        # v4 has no trusted deny evidence. IH-48 must add an independent,
        # platform-owned observation before "denied" can be returned here.
        return report_for_sdk(
            ok=True,
            verdict=(
                "unknown"
                if unprobed_builtin_tools
                else "unknown_authorization"
            ),
            tools=tools,
            probed=tuple(probed),
            warnings=report_warnings,
            # 성공한 배포의 결과 문구예요 — 「다 쓸 수 있다」로 읽히지 않게 승인 대기
            # 개수를 여기에도 남겨요(같은 사실이 `warnings` 에도 있어요).
            reason=(
                f"도구 {len(pending_approval_tools)}개가 관리자 승인 대기 중이에요."
                if pending_approval_tools
                else ""
            ),
            negative_control_denied=None,
            negative_control_verdict="unknown",
            unprobed_builtin_tools=unprobed_builtin_tools,
            expected_conversation_manager=expected_conversation_manager,
            conversation_manager=conversation_manager,
        )

    @staticmethod
    def _classify_tool_authorization(
        expected_tools: tuple[ExpectedTool, ...],
        tool_approval_states: dict[str, str] | None,
    ) -> tuple[tuple[str, ...], tuple[str, ...], str]:
        """④ 원장 상태로 「지금 부를 수 없는 도구」를 갈라요 (ADR-0104).

        기대값의 소유자는 **원장**이에요 — 이 함수는 runtime 이 보고한 목록을 보지 않아요.
        그래서 「도구가 안 보인다」와 「승인이 아직 없다」가 서로를 증명하지 않아요.

        Gateway 도구가 아닌 기대값(`skills`·내장 도구)은 ④ binding 이 없는 게 정상이라
        판정 대상이 아니에요.
        """
        gateway_tools = tuple(
            expected
            for expected in expected_tools
            if _GATEWAY_TOOL_SEPARATOR in expected.name
        )
        if not gateway_tools:
            return (), (), _AUTHORIZATION_NOT_APPLICABLE
        if tool_approval_states is None:
            return (), (), _AUTHORIZATION_UNKNOWN
        pending: list[str] = []
        unrequested: list[str] = []
        for expected in gateway_tools:
            state = next(
                (
                    tool_approval_states[name]
                    for name in (expected.name, *expected.aliases)
                    if name in tool_approval_states
                ),
                "",
            )
            if state == _APPROVAL_APPROVED:
                continue
            if state == _APPROVAL_REQUESTED:
                pending.append(expected.name)
            else:
                # 행이 없거나 `REJECTED` 예요. 둘 다 「승인 대기」가 아니에요 — 신청이
                # 접수되지 않았거나 반려된 상태라 관리자가 승인할 큐에 없어요.
                unrequested.append(expected.name)
        return (
            tuple(dict.fromkeys(pending)),
            tuple(dict.fromkeys(unrequested)),
            _AUTHORIZATION_OBSERVED,
        )

    def _remaining(self, deadline: float) -> float:
        return deadline - self._monotonic()

    #: 개별 호출 상한(초). 전체 예산과 별개로 한 호출이 예산을 다 먹는 걸 막아요 —
    #: 하나가 멈춰도 나머지 probe 를 시도할 여지를 남겨요.
    _CALL_TIMEOUT_SEC = 120.0

    @classmethod
    def _call_timeout(cls, remaining: float) -> float:
        return min(cls._CALL_TIMEOUT_SEC, max(0.1, remaining))

    def _deadline_report(
        self,
        tools: tuple[str, ...],
        warnings: list[str] | tuple[str, ...],
        probed: list[str] | tuple[str, ...] = (),
        *,
        bedrock_agentcore_version: str | None = None,
        pending_approval_tools: tuple[str, ...] = (),
        unrequested_tools: tuple[str, ...] = (),
        tool_authorization_status: str = _AUTHORIZATION_UNKNOWN,
    ) -> VerifyReport:
        # ④ 승인 축을 그대로 실어요. 호출부가 안 넘기면 `unknown` 이에요 — deadline 리포트가
        # 「Gateway 도구가 없음」을 주장하면 관측 못 한 것을 다른 사실로 적는 셈이에요.
        return VerifyReport(
            ok=False,
            verdict="unknown",
            tools=tools,
            probed=tuple(probed),
            warnings=tuple(warnings),
            reason="배포 verifier 전체 deadline을 초과해 도달성을 관측할 수 없어요.",
            bedrock_agentcore_version=bedrock_agentcore_version,
            pending_approval_tools=pending_approval_tools,
            unrequested_tools=unrequested_tools,
            tool_authorization_status=tool_authorization_status,
        )

    def _selfcheck(
        self,
        runtime_arn: str,
        actor_id: str,
        deadline: float,
    ):
        warnings: list[str] = []
        last_error = ""
        for attempt in range(self._attempts):
            remaining = self._remaining(deadline)
            if remaining <= 0:
                return self._deadline_report((), warnings)
            try:
                data = self._invoker.call(
                    runtime_arn=runtime_arn,
                    method="agora/selfcheck",
                    params={},
                    session_id=_session_id(),
                    actor_id=actor_id,
                    call_handle=getattr(self, "_call_handle", None),
                    timeout_sec=self._call_timeout(remaining),
                )
            except Exception as error:
                last_error = f"{type(error).__name__}: {error}"
            else:
                error = data.get("error") if isinstance(data, dict) else None
                if (
                    isinstance(error, dict)
                    and error.get("code") == _METHOD_NOT_FOUND
                ):
                    warnings.append("agora/selfcheck 미지원 agent예요.")
                    return None, warnings
                if isinstance(error, dict):
                    last_error = error.get("message") or str(error)
                else:
                    result = data.get("result") if isinstance(data, dict) else None
                    if isinstance(result, dict):
                        return result, warnings
                    last_error = (
                        f"selfcheck 응답 형식이 예상과 달라요: {str(data)[:200]}"
                    )
            if attempt < self._attempts - 1:
                remaining = self._remaining(deadline)
                if remaining <= 0:
                    return self._deadline_report((), warnings)
                self._sleep(min(self._backoff, remaining))
        return VerifyReport(
            ok=False,
            verdict="unknown",
            reason=f"검증 실패(selfcheck): {last_error}",
        )

    def _call_tool(
        self,
        runtime_arn: str,
        tool: str,
        arguments: dict,
        actor_id: str,
        deadline: float,
    ) -> tuple[str, str] | None:
        remaining = self._remaining(deadline)
        if remaining <= 0:
            return (
                "unknown",
                "배포 verifier 전체 deadline을 초과해 READ 도달성을 관측할 수 없어요.",
            )
        try:
            data = self._invoker.call(
                runtime_arn=runtime_arn,
                method="agora/tool-call",
                params={"tool": tool, "arguments": arguments},
                session_id=_session_id(),
                actor_id=actor_id,
                call_handle=getattr(self, "_call_handle", None),
                timeout_sec=self._call_timeout(remaining),
            )
        except Exception as error:
            return (
                "unknown",
                f"READ 도구 도달성을 관측할 수 없어요({tool}): "
                f"{_exception_reason(error)}",
            )
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            if error.get("code") == _METHOD_NOT_FOUND:
                return (
                    "unknown",
                    "옛 scaffold 산출물이라 agora/tool-call probe method를 지원하지 "
                    "않아요. ADR-0036의 단일 scaffold 생성 경로로 agent를 재생성한 "
                    "뒤 다시 배포해 주세요.",
                )
            message = str(error.get("message") or "")
            if (
                error.get("code") == _INVALID_PARAMS
                and "READ probe" in message
            ):
                return (
                    "unknown",
                    "Registry의 명시 sensitivity와 생성 산출물의 probe 허용 태그가 "
                    f"상충해 호출하지 않았어요({tool}). agent를 재생성해 주세요.",
                )
            coordinates = _positive_probe_error_coordinates(
                data,
                error,
                tool=tool,
            )
            return (
                "diverged",
                "READ 도구 호출이 실패했어요"
                f"({tool}): {json.dumps(coordinates, ensure_ascii=False)}",
            )
        result = data.get("result") if isinstance(data, dict) else None
        if not isinstance(result, dict):
            return (
                "unknown",
                f"READ 도구 호출 응답을 해석할 수 없어요({tool}).",
            )
        if result.get("status") == "error":
            coordinates = _probe_result_coordinates(result, tool=tool)
            rendered = json.dumps(coordinates, ensure_ascii=False)
            # 사유를 알아봤으면 좌표만 던지지 않고 **문장으로** 말해요 (IH-183).
            #
            # 2026-09-06 실사고: interceptor 는 `호출한 사람에게 이 도구 권한(grant)이
            # 없어요.` 라고 정확히 말했는데 이 자리의 문구가 `error_type: "dict",
            # content_length: 1` 뿐이라, 원인을 찾는 데 CloudWatch 4개 로그 그룹과 DynamoDB
            # 3개 파티션을 뒤져야 했어요.
            #
            # 문장은 **포털 쪽 표에서** 렌더해요 — agent 가 보낸 문자열을 그대로 쓰지 않아요.
            # 판정(`ok`·`verdict`)은 건드리지 않아요. 거부를 실패가 아니라 관측된 인가 상태로
            # 다루는 건 ④ 축과 대칭을 맞추는 별 티켓이에요.
            denial = denial_message_for_reason(coordinates.get("denial_reason"))
            if denial:
                return (
                    "diverged",
                    f"READ 도구 호출이 Gateway 인가에서 거부됐어요({tool}): "
                    f"{denial} {rendered}",
                )
            return (
                "diverged",
                f"READ 도구 호출이 실패했어요({tool}): {rendered}",
            )
        return None

    def _qualify_negative_control(
        self,
        runtime_arn: str,
        control: NegativeControl,
        policy_revision: str,
        actor_id: str,
        deadline: float,
    ) -> tuple[str, str]:
        """Observe an untrusted v4 deny candidate for one policy revision."""
        try:
            remaining = self._remaining(deadline)
            if remaining <= 0:
                return (
                    "unknown",
                    "배포 verifier 전체 deadline을 초과해 negative control을 관측할 수 없어요.",
                )
            data = self._invoker.call(
                runtime_arn=runtime_arn,
                method="agora/authorization-probe",
                params={
                    "tool": control.tool,
                    "endpoint": control.endpoint,
                    "arguments": control.arguments,
                },
                session_id=_session_id(),
                actor_id=actor_id,
                # 음성 대조에도 handle 을 실어요. 그러면 거부 이유가 "handle 없음" 이 아니라
                # **"이 도구는 이 봇에 승인되지 않음"** 이 돼요 — 훨씬 강한 대조예요.
                call_handle=getattr(self, "_call_handle", None),
                timeout_sec=self._call_timeout(remaining),
            )
        except Exception as error:
            return (
                "unknown",
                "negative control을 관측할 수 없어요: "
                f"{_exception_reason(error)}",
            )
        data = data if isinstance(data, dict) else {}
        result = data.get("result")
        if not isinstance(result, dict):
            return (
                "unknown",
                "negative control subject 응답은 신뢰된 Gateway deny 증거가 "
                "아니에요. untrusted candidate="
                f"{_render_observation(data)}",
            )
        outcome = str(result.get("outcome") or "").upper()
        if outcome == "ALLOW":
            return (
                "diverged",
                "negative control이 미승인 Gateway operation 호출을 허용했어요.",
            )
        # 거부 candidate 는 두 모양으로 와요. Cedar·인바운드 인증은 HTTP 403 이고,
        # interceptor 거부는 IH-128 이후 **HTTP 200 + JSON-RPC -32001** 이에요. 후자를
        # 안 세면 「아무 거부도 못 봤다」로 읽혀서 도구 선언·배포를 엉뚱하게 탓해요.
        if _contains_status(result, 403):
            status_candidate = " HTTP 403 candidate를 관측했지만"
        elif _contains_field(
            result,
            fields=frozenset({"code"}),
            expected=_INTERCEPTOR_DENIAL_CODE,
        ):
            status_candidate = (
                f" JSON-RPC {_INTERCEPTOR_DENIAL_CODE} 거부 candidate를 관측했지만"
            )
        else:
            status_candidate = ""
        return (
            "unknown",
            "negative control subject 응답은 신뢰된 Gateway deny 증거가 "
            f"아니에요.{status_candidate} Gateway/Cedar 소유 식별자와 "
            "독립 관측 경로가 확정되지 않아 통과시키지 않아요. "
            f"untrusted candidate={_render_observation(result)}",
        )

    def _smoke(
        self,
        runtime_arn: str,
        actor_id: str,
        deadline: float,
    ) -> str:
        remaining = self._remaining(deadline)
        if remaining <= 0:
            return "배포 verifier 전체 deadline을 초과해 스모크 테스트를 실행할 수 없어요."
        try:
            reply = self._invoker.invoke(
                runtime_arn=runtime_arn,
                prompt=_SMOKE_PROMPT,
                session_id=_session_id(),
                actor_id=actor_id,
                timeout_sec=self._call_timeout(remaining),
            )
        except Exception as error:
            return f"스모크 테스트 실패: {type(error).__name__}: {error}"
        if not (reply or "").strip():
            return "스모크 테스트 실패: agent가 빈 응답을 줬어요."
        return ""
