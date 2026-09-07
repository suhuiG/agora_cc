"""AwsDeployAdapter — DeployPort의 boto3 구현.

D1(Lambda tool-provider) 아키텍처:
  codebuild(빌드 트리거·상태) + lambda(함수 생성·삭제) + bedrock-agentcore-control(gateway 타깃).
지연 클라이언트 생성은 registry/aws_adapter.py 패턴. 각 메서드는 boto3 응답을
DeployPort 상태 값객체로 매핑해요.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from ....shared.config import cognito_pool_id_from_discovery
from ....shared.memory import (
    MEMORY_NAMESPACE_TEMPLATES,
    require_supported_memory_strategies,
)
from .models import BuildArtifact, SourceRef
from .port import (
    AgentExecutionRoleDeployment,
    BuildStatus,
    BuiltinToolDeployment,
    BuiltinToolStatus,
    IdentityOutboundDeployment,
    IdentityOutboundProvisioningError,
    LambdaDeployment,
    MemoryDeployment,
    MemoryStatus,
    RuntimeDeployment,
    RuntimeStatus,
    TargetStatus,
)

_log = logging.getLogger(__name__)

_BUILTIN_TOOL_NETWORK_MODES = {
    "browser": "PUBLIC",
    "code_interpreter": "PUBLIC",
}
# Dev measurement (2026-08-21, ap-northeast-2): browser-custom rejects
# APPLICATION_LOGS; both CUSTOM tool kinds accept TRACES via XRAY.
_BUILTIN_TOOL_LOG_TYPES = {
    "browser": ("USAGE_LOGS", "TRACES"),
    "code_interpreter": ("APPLICATION_LOGS", "USAGE_LOGS", "TRACES"),
}
_MEMORY_STRATEGY_NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,47}$")
_LAMBDA_OWNER_DESCRIPTION_KEY = "agora_mcp_lambda_owner"
_LAMBDA_OWNER_DESCRIPTION_VERSION = 3
_LAMBDA_OWNER_PRINCIPAL_DESCRIPTION_VERSION = 2
_LAMBDA_OWNER_LEGACY_DESCRIPTION_VERSION = 1
_LAMBDA_DESCRIPTION_MAX_LENGTH = 256
_SOURCE_VERSION = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$"
)
_JSON_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")
_LAMBDA_OWNER_MARKER_MAX_DEPTH = 64


@dataclass(frozen=True)
class _LambdaOwner:
    schema_version: int
    job_id: str
    asset_id: str
    principal: str | None
    source_version: str | None


def _parse_source_version(version: str) -> tuple[int, int, int]:
    match = _SOURCE_VERSION.match(version or "")
    if match is None:
        raise ValueError(f"invalid Lambda owner source version: {version!r}")
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
    )


def _decode_json_unicode_escapes(value: str) -> str:
    decoded = _JSON_UNICODE_ESCAPE.sub(
        lambda match: chr(int(match.group(1), 16)),
        value,
    )
    if r"\u" in decoded:
        raise ValueError("invalid JSON unicode escape")
    return decoded


def _payload_has_lambda_owner_marker(payload: object, *, depth: int = 0) -> bool:
    if depth > _LAMBDA_OWNER_MARKER_MAX_DEPTH:
        raise RecursionError("Lambda owner marker nesting is too deep")
    if isinstance(payload, dict):
        if _LAMBDA_OWNER_DESCRIPTION_KEY in payload:
            return True
        return any(
            _payload_has_lambda_owner_marker(value, depth=depth + 1)
            for value in payload.values()
        )
    if isinstance(payload, list):
        return any(
            _payload_has_lambda_owner_marker(value, depth=depth + 1)
            for value in payload
        )
    return False


def _json_object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"duplicate JSON key: {key!r}")
        payload[key] = value
    return payload


def _lambda_owner_description(
    *,
    job_id: str,
    asset_id: str,
    principal: str,
    source_version: str,
) -> str:
    if not principal:
        raise ValueError("Lambda ownership principal must not be empty")
    _parse_source_version(source_version)
    owner = {
        "asset_id": asset_id,
        "job_id": job_id,
        "version": _LAMBDA_OWNER_DESCRIPTION_VERSION,
        "principal": principal,
        "source_version": source_version,
    }
    description = json.dumps(
        {_LAMBDA_OWNER_DESCRIPTION_KEY: owner},
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(description) > _LAMBDA_DESCRIPTION_MAX_LENGTH:
        raise ValueError(
            "Lambda ownership description exceeds the 256-character limit: "
            f"job={job_id!r} asset={asset_id!r}"
        )
    return description


def _lambda_owner(
    configuration: dict,
) -> _LambdaOwner | None:
    description = configuration.get("Description")
    if not isinstance(description, str):
        return None
    try:
        payload = json.loads(
            description,
            object_pairs_hook=_json_object_without_duplicate_keys,
        )
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    if set(payload) != {_LAMBDA_OWNER_DESCRIPTION_KEY}:
        return None
    owner = payload.get(_LAMBDA_OWNER_DESCRIPTION_KEY)
    if not isinstance(owner, dict):
        return None
    version = owner.get("version")
    if type(version) is not int or version not in {
        _LAMBDA_OWNER_LEGACY_DESCRIPTION_VERSION,
        _LAMBDA_OWNER_PRINCIPAL_DESCRIPTION_VERSION,
        _LAMBDA_OWNER_DESCRIPTION_VERSION,
    }:
        return None
    expected_fields = {"asset_id", "job_id", "version"}
    if version in {
        _LAMBDA_OWNER_PRINCIPAL_DESCRIPTION_VERSION,
        _LAMBDA_OWNER_DESCRIPTION_VERSION,
    }:
        expected_fields.add("principal")
    if version == _LAMBDA_OWNER_DESCRIPTION_VERSION:
        expected_fields.add("source_version")
    if set(owner) != expected_fields:
        return None
    job_id = owner.get("job_id")
    asset_id = owner.get("asset_id")
    if not isinstance(job_id, str) or not job_id:
        return None
    if not isinstance(asset_id, str) or not asset_id:
        return None
    principal = owner.get("principal")
    if version in {
        _LAMBDA_OWNER_PRINCIPAL_DESCRIPTION_VERSION,
        _LAMBDA_OWNER_DESCRIPTION_VERSION,
    }:
        if not isinstance(principal, str) or not principal:
            return None
    else:
        principal = None
    source_version = owner.get("source_version")
    if version == _LAMBDA_OWNER_DESCRIPTION_VERSION:
        if not isinstance(source_version, str):
            return None
        try:
            _parse_source_version(source_version)
        except ValueError:
            return None
    else:
        source_version = None
    return _LambdaOwner(
        schema_version=version,
        job_id=job_id,
        asset_id=asset_id,
        principal=principal,
        source_version=source_version,
    )


def _lambda_owner_marker_present(configuration: dict) -> bool:
    description = configuration.get("Description")
    if not isinstance(description, str):
        return False
    raw_marker_present = _LAMBDA_OWNER_DESCRIPTION_KEY in description
    try:
        decoded_marker_present = (
            _LAMBDA_OWNER_DESCRIPTION_KEY
            in _decode_json_unicode_escapes(description)
        )
    except (TypeError, ValueError, RecursionError):
        return True
    try:
        payload = json.loads(description)
    except RecursionError:
        return True
    except (TypeError, ValueError):
        return raw_marker_present or decoded_marker_present
    try:
        structured_marker_present = _payload_has_lambda_owner_marker(payload)
    except RecursionError:
        return True
    return (
        raw_marker_present
        or decoded_marker_present
        or structured_marker_present
    )


def _lambda_ownership_conflict_message(
    *,
    name: str,
    existing_owner: _LambdaOwner | None,
    expected_job_id: str,
    expected_asset_id: str,
    expected_principal: str,
) -> str:
    if existing_owner is None:
        owner_detail = "job, asset, and principal ownership are unproven"
    else:
        owner_detail = (
            f"deploy job {existing_owner.job_id!r} "
            f"for asset {existing_owner.asset_id!r}"
        )
        if existing_owner.principal is not None:
            owner_detail += f" by principal {existing_owner.principal!r}"
    expected_detail = (
        f"deploy job {expected_job_id!r} for asset {expected_asset_id!r}"
    )
    if expected_principal:
        expected_detail += f" by principal {expected_principal!r}"
    return (
        f"Lambda name {name!r} is already used by {owner_detail}; "
        f"{expected_detail} cannot reuse or delete it"
    )


def _runtime_mcp_asset(asset: dict) -> dict:
    """Project one Registry MCP dependency into the runtime binding shape.

    ⚠️ 이 blob 은 `AGORA_MCP_ASSETS` **env value 하나**로 들어가고 AgentCore 는 value 당
    5,000자 한도를 둬요. 그래서 **파생 가능한 필드는 싣지 않아요** — 실측 2026-09-04 에
    자산 3개·19 operation 이 5,068자로 넘겨 `CreateAgentRuntime` 이 ValidationException 으로
    배포를 실패시켰어요(cs-bot-ver1).

    빼는 두 필드와 소비자 쪽 파생 근거:

    - `allowed_tool_names` → `scaffold._binding_allowed_tool_names` 가 `operations` + `name`
      으로 재구성해요. `gateway_tool_names` 와 같은 집합이고 `_belongs_to_binding` 은 멤버십
      검사라 순서도 무관해요.
    - `tool_prefixes` → `scaffold._binding_tool_prefixes` 가 `name` 으로 재구성해요.
      `gateway_tool_prefixes` 와 **글자 단위로 같아요**(하이픈·언더스코어 변형 + `___`).

    생산자와 소비자는 항상 같은 배포에서 함께 갱신돼요(`create_agent_runtime`·
    `update_agent_runtime` 이 같은 `_agent_runtime_payload` 를 써요), 그래서 이미 배포된
    runtime 과의 형식 호환을 걱정하지 않아요. 새 필드를 여기 더할 때는 파생 불가능한지와
    한도 여유를 함께 보세요 (`docs/06-risks.md` IH-158).
    """
    binding = {
        "asset_id": asset["assetId"],
        "name": asset["name"],
        "endpoint": asset["endpoint"],
    }
    if "operations" in asset:
        binding["operations"] = list(asset["operations"])
    return binding


def _runtime_mcp_assets(asset: dict) -> list[dict]:
    """Expand one Registry MCP dependency into runtime target bindings."""
    if "gatewayTargets" not in asset:
        return [_runtime_mcp_asset(asset)]
    targets = asset.get("gatewayTargets")
    if not isinstance(targets, (list, tuple)):
        raise ValueError("MCP gatewayTargets must be an array")

    bindings: list[dict] = []
    for target in targets:
        if not isinstance(target, dict):
            raise ValueError("MCP gatewayTargets entries must be objects")
        name = str(target.get("name") or "").strip()
        operations = target.get("operations")
        if (
            not name
            or not isinstance(operations, (list, tuple))
            or not operations
            or not all(
                isinstance(operation, str) and operation.strip()
                for operation in operations
            )
        ):
            raise ValueError("MCP gatewayTargets entry is incomplete")
        bindings.append(_runtime_mcp_asset({
            "assetId": asset["assetId"],
            "name": name,
            "endpoint": asset["endpoint"],
            "operations": list(operations),
        }))
    return bindings


def _sanitize_runtime_name(name: str) -> str:
    """AgentCore agentRuntimeName 제약에 맞게 이름을 정제해요.

    규약(실측 2026-07-17): `[a-zA-Z][a-zA-Z0-9_]{0,47}` — 하이픈·점 등 불가,
    영숫자와 언더스코어만, 첫 글자는 영문, 최대 48자. asset 이름엔 하이픈이 흔해서
    (예: chatbot-agent) 언더스코어로 치환해요.
    """
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    if not cleaned or not cleaned[0].isalpha():
        cleaned = "a" + cleaned          # 첫 글자는 반드시 영문
    return cleaned[:48]


def _legacy_memory_strategy_projection(
    memory_name: str,
    selected: str,
) -> tuple[str, dict]:
    strategy_map = {
        "SEMANTIC": (
            "semanticMemoryStrategy",
            "semantic",
            [MEMORY_NAMESPACE_TEMPLATES["SEMANTIC"]],
        ),
        "SUMMARIZATION": (
            "summaryMemoryStrategy",
            "summary",
            [MEMORY_NAMESPACE_TEMPLATES["SUMMARIZATION"]],
        ),
    }
    key, suffix, namespace_templates = strategy_map[selected]
    strategy_name = f"{memory_name[:47 - len(suffix)]}_{suffix}"
    return key, {
        "name": strategy_name,
        "namespaceTemplates": namespace_templates,
    }


def _configured_memory_strategy_projection(
    selected: str,
    config: dict,
) -> tuple[str, dict]:
    key = {
        "SEMANTIC": "semanticMemoryStrategy",
        "SUMMARIZATION": "summaryMemoryStrategy",
    }[selected]
    name = str(config.get("name") or "")
    if _MEMORY_STRATEGY_NAME.fullmatch(name) is None:
        raise ValueError(f"invalid AgentCore Memory strategy name: {name!r}")
    projected = {"name": name}
    projected["namespaceTemplates"] = list(config["namespaces"])
    return key, projected


def _memory_strategy_projection(
    memory_name: str,
    config: dict,
) -> list[dict]:
    require_supported_memory_strategies(config.get("strategies"))
    configured = config.get("strategy_configs")
    strategies = []
    for selected in config.get("strategies") or ():
        if configured is None:
            key, projected = _legacy_memory_strategy_projection(
                memory_name,
                selected,
            )
        else:
            key, projected = _configured_memory_strategy_projection(
                selected,
                configured[selected],
            )
        strategies.append({key: projected})
    return strategies


def _expected_memory_strategy_state(
    memory_name: str,
    config: dict,
) -> list[dict]:
    type_by_key = {
        "semanticMemoryStrategy": "SEMANTIC",
        "summaryMemoryStrategy": "SUMMARIZATION",
    }
    expected = []
    for strategy in _memory_strategy_projection(memory_name, config):
        key, projected = next(iter(strategy.items()))
        state = {
            "type": type_by_key[key],
            "name": projected["name"],
        }
        state["namespaceTemplates"] = projected["namespaceTemplates"]
        expected.append(state)
    return sorted(expected, key=lambda item: item["type"])


def _actual_memory_strategy_state(memory: dict) -> list[dict]:
    actual = []
    for strategy in memory.get("strategies") or ():
        strategy_type = str(strategy.get("type") or "")
        state = {
            "type": strategy_type,
            "name": str(strategy.get("name") or ""),
        }
        if strategy_type in {"SEMANTIC", "SUMMARIZATION"}:
            state["namespaceTemplates"] = list(
                strategy.get("namespaceTemplates") or ()
            )
        actual.append(state)
    return sorted(actual, key=lambda item: item["type"])


def _is_conflict(e: Exception) -> bool:
    """예외가 AWS ConflictException(이미 존재)인지 판별해요. botocore ClientError 구조 기반."""
    resp = getattr(e, "response", None)
    if isinstance(resp, dict):
        code = resp.get("Error", {}).get("Code", "")
        if code in ("ConflictException", "ResourceConflictException"):
            return True
    # 폴백: 클래스명·메시지로도 판별(어댑터 계약 테스트·SDK 버전 차 방어).
    text = f"{type(e).__name__}: {e}"
    return "ConflictException" in text or "ResourceConflictException" in text


def _is_not_found(e: Exception) -> bool:
    response = getattr(e, "response", None)
    if isinstance(response, dict):
        return response.get("Error", {}).get("Code") in {
            "ResourceNotFoundException",
            "NotFoundException",
            "NoSuchEntity",
        }
    return False


def _is_gateway_target_deleting(e: Exception) -> bool:
    """DeleteGatewayTarget가 이미 진행 중이라는 ValidationException인지 판별해요."""
    response = getattr(e, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error", {})
    if error.get("Code") != "ValidationException":
        return False
    message = str(error.get("Message") or e).lower()
    return "deletegatewaytarget" in message and "deleting state" in message


def _identity_probe_is_unobservable(exc: Exception) -> bool:
    """Token probe가 실패한 게 아니라 관측 자체가 불가능했는지 분류해요."""
    response = getattr(exc, "response", None)
    code = (
        response.get("Error", {}).get("Code", "")
        if isinstance(response, dict)
        else ""
    )
    if code in {
        "ResourceNotFoundException",
        "AccessDeniedException",
        "UnauthorizedException",
        "UnrecognizedClientException",
        "ExpiredTokenException",
        "InvalidClientTokenId",
        "ThrottlingException",
        "TooManyRequestsException",
        "InternalServerException",
        "ServiceUnavailableException",
        "RequestTimeout",
        "RequestTimeoutException",
    }:
        return True
    return type(exc).__name__ in {
        "EndpointConnectionError",
        "ConnectTimeoutError",
        "ReadTimeoutError",
        "ConnectionClosedError",
        "ProxyConnectionError",
        "NoCredentialsError",
        "PartialCredentialsError",
    }


def _identity_probe_is_not_ready(exc: Exception) -> bool:
    """Identity 생성 직후 아직 조회되지 않는 eventual consistency인지 봐요."""
    response = getattr(exc, "response", None)
    return (
        isinstance(response, dict)
        and response.get("Error", {}).get("Code")
        == "ResourceNotFoundException"
    )


def _is_already_exists(e: Exception) -> bool:
    """"이미 있음"을 뜻하는 예외인지 판별해요 — Conflict보다 넓게 봐요.

    Identity 리소스는 중복 생성 시 ConflictException이 아니라 ValidationException에
    "already exists" 메시지를 담아 던져요(실측 2026-07-27, CreateOauth2CredentialProvider).
    get-or-create를 멱등하게 만들려면 메시지까지 봐야 해요.
    """
    response = getattr(e, "response", None)
    code = (
        response.get("Error", {}).get("Code", "")
        if isinstance(response, dict)
        else ""
    )
    return (
        _is_conflict(e)
        or code == "ResourceAlreadyExistsException"
        or code == "EntityAlreadyExists"
        or "already exists" in str(e).lower()
    )


def _put_tagged_once(operation, tags: dict, /, **kwargs):
    """`Put*` upsert에 tags를 **생성 시에만** 붙여요.

    CloudWatch Logs vended delivery의 `PutDeliverySource`·`PutDeliveryDestination`은
    이름 기준 upsert인데, **이미 존재하는 리소스에 `tags=`를 보내면 거부해요**:
    `ConflictException: Tags can only be provided when a resource is being created,
    not updated.` (실측 2026-08-21, dev/ap-northeast-2 — CUSTOM code interpreter에
    같은 이름으로 `put_delivery_source`를 두 번 호출해 재현했고, tags 없이 부르면 통과).

    destination 이름이 stage 단위라 첫 배포가 만들고 나면 **이후 모든 배포가 이 지점에서
    실패**해요. 그래서 tags를 포함해 한 번 시도하고, ConflictException이면 tags 없이
    다시 불러요. 두 번째 호출도 실패하면 원래 예외를 올려요(조용히 삼키지 않아요).
    """
    try:
        return operation(**kwargs, tags=tags)
    except Exception as exc:
        if not _is_conflict(exc):
            raise
        try:
            return operation(**kwargs)
        except Exception:
            raise exc from None


def _oidc_discovery_url(url: str) -> str:
    """OIDC discovery URL로 정규화해요.

    JWT authorizer에 쓰는 URL은 `/.well-known/openid-configuration` 없이 올 수 있는데,
    credential provider의 oauthDiscovery는 전체 경로를 요구해요(실측 2026-07-27).
    """
    suffix = "/.well-known/openid-configuration"
    return url if url.endswith(suffix) else url.rstrip("/") + suffix


_PREVIEW_MSG = "AwsDeployAdapter는 boto3가 필요해요. `pip install boto3` 후 다시 시도하세요."

# AgentCore ToolDefinition은 닫힌 shape(name/description/inputSchema/outputSchema)라
# Agora의 sensitivity sibling을 보내면 SDK/API validation이 실패해요. Gateway target에
# 실을 tool은 이 키만 투영해요(durable source는 catalog/Aux descriptor).
_GATEWAY_TOOL_KEYS = {"name", "description", "inputSchema", "outputSchema"}


def _gateway_tools_from_inline(tools_inline: str) -> list[dict]:
    """inline tool schema JSON을 Gateway ToolDefinition 리스트로 정규화해요.

    빈 값·malformed는 ValueError로 계약 위반을 짚어요(wire_lambda_target·
    update_gateway_target 공용 — I1). AgentCore가 허용하는 닫힌 키만 남겨요.
    """
    if not tools_inline:
        raise ValueError(
            "tool 스키마가 비어있어요 — 빌드가 tools.json을 산출하지 못했을 수 있어요.")
    try:
        parsed = json.loads(tools_inline)
        tools = parsed["tools"]
    except (ValueError, KeyError, TypeError) as e:
        raise ValueError(
            f"tool 스키마 JSON이 올바르지 않아요(tools 키 필요): {e}") from e
    return [
        {key: value for key, value in tool.items() if key in _GATEWAY_TOOL_KEYS}
        for tool in tools
        if isinstance(tool, dict)
    ]


def _tools_index(tools: list[dict]) -> dict[str, dict]:
    """tool 리스트를 name→정의 dict로 색인해요. 순서 무관·add/remove/change 감지용."""
    return {str(tool.get("name")): tool for tool in tools if tool.get("name")}


def _current_target_tools(target: dict) -> list[dict]:
    """GetGatewayTarget 응답에서 lambda target의 현재 inline tool 정의를 뽑아요.

    구조가 없으면(비-lambda·비어있음) 빈 리스트 — 비교 기준을 빈 세트로 취급해요.
    """
    try:
        payload = (
            target["targetConfiguration"]["mcp"]["lambda"]
            ["toolSchema"]["inlinePayload"]
        )
        if not isinstance(payload, list):
            return []
        # _gateway_tools_from_inline(new)와 **대칭**이 되도록 같은 닫힌 키로 projection해요.
        # GetGatewayTarget이 정규화/추가 키를 실어도 no-op 비교가 매번 UpdateGatewayTarget
        # 으로 새지 않게(CA-17 리뷰). null/비-list payload는 빈 세트로(try 안에서 가드).
        return [
            {k: v for k, v in tool.items() if k in _GATEWAY_TOOL_KEYS}
            for tool in payload
            if isinstance(tool, dict)
        ]
    except (KeyError, TypeError):
        return []


# Gateway lambda target의 targetConfiguration·credential. create(wire_lambda_target)와
# update(update_gateway_target)가 **공유**해요 — UpdateGatewayTarget이 full-replace라
# create에만 필드를 추가하고 update에 안 넣으면 재배포 때 조용히 유실돼요(CA-17 리뷰 #1).
_GATEWAY_CRED_CONFIG = [{"credentialProviderType": "GATEWAY_IAM_ROLE"}]
# Service Quotas 의 실제 이름이에요 — dev 실측(2026-08-28): `Targets per gateway`
# (`L-3601D726`, 값 100, Adjustable). 이전 값 "Number of targets per gateway" 는 어느
# 쿼터와도 맞지 않아서 probe 가 항상 `unknown` 이었어요(70% 트리거가 영구 미작동).
# 이름이 또 바뀔 수 있으니 코드로도 찾을 수 있게 둘 다 둬요.
_GATEWAY_TARGET_QUOTA_NAME = "Targets per gateway"
_GATEWAY_TARGET_QUOTA_CODE = "L-3601D726"
_GATEWAY_TARGET_QUOTA_THRESHOLD = 0.7


def _lambda_target_config(lambda_arn: str, tools: list[dict]) -> dict:
    return {"mcp": {"lambda": {
        "lambdaArn": lambda_arn,
        "toolSchema": {"inlinePayload": tools},
    }}}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass(frozen=True)
class _RuntimeWorkloadKey:
    workload_id: str
    private_key: str
    public_key: str


def _runtime_workload_key(
    workload_id: str, private_key: str | None = None
) -> _RuntimeWorkloadKey:
    """Ed25519 key를 만들거나 Runtime env의 기존 key에서 공개키를 복원해요."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if private_key:
        key = Ed25519PrivateKey.from_private_bytes(_b64url_decode(private_key))
    else:
        key = Ed25519PrivateKey.generate()
        private_key = _b64url(
            key.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
        )
    public_key = _b64url(
        key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    )
    return _RuntimeWorkloadKey(workload_id, private_key, public_key)


class AwsDeployAdapter:
    def __init__(self, *, region, codebuild_project="",
                 artifact_bucket="", source_bucket="", source_region="",
                 codebuild_client=None, lambda_client=None,
                 control_client=None, s3_client=None, cognito_client=None,
                 logs_client=None, service_quotas_client=None,
                 iam_client=None, agentcore_client=None,
                 synchronization_sleep=None,
                 synchronization_attempts: int = 20) -> None:
        self.region = region
        self.codebuild_project = codebuild_project
        self.artifact_bucket = artifact_bucket
        # sourcestore(업로드 소스) 버킷·리전 — CodeBuild가 여기서 소스를 내려받아요.
        # sourcestore는 서울(ap-northeast-2), 빌드는 us-east-1이라 cross-region일 수 있어요.
        self.source_bucket = source_bucket
        self.source_region = source_region
        self._cb = codebuild_client
        self._lam = lambda_client
        self._ctrl = control_client
        self._s3 = s3_client
        # cognito-idp — client secret을 describe로만 확보해요(Identity P2, 미저장).
        self._cog = cognito_client
        self._logs = logs_client
        self._service_quotas = service_quotas_client
        self._iam = iam_client
        # bedrock-agentcore data plane — outbound token qualification probe.
        self._agentcore = agentcore_client
        self._synchronization_sleep = synchronization_sleep or self._sleep
        self._synchronization_attempts = max(1, synchronization_attempts)

    def _clients(self):
        if (self._cb is None or self._lam is None or self._ctrl is None
                or self._s3 is None):
            try:
                import boto3
            except ImportError as e:  # pragma: no cover
                raise RuntimeError(_PREVIEW_MSG) from e
            self._cb = self._cb or boto3.client("codebuild", region_name=self.region)
            self._lam = self._lam or boto3.client("lambda", region_name=self.region)
            self._ctrl = self._ctrl or boto3.client(
                "bedrock-agentcore-control", region_name=self.region)
            self._s3 = self._s3 or boto3.client("s3", region_name=self.region)
        return self._cb, self._lam, self._ctrl, self._s3

    def _logs_client(self):
        if self._logs is None:
            try:
                import boto3
            except ImportError as e:  # pragma: no cover
                raise RuntimeError(_PREVIEW_MSG) from e
            self._logs = boto3.client("logs", region_name=self.region)
        return self._logs

    def _iam_client(self):
        if self._iam is None:
            try:
                import boto3
            except ImportError as e:  # pragma: no cover
                raise RuntimeError(_PREVIEW_MSG) from e
            self._iam = boto3.client("iam")
        return self._iam

    @staticmethod
    def _agent_role_name(agent_key: str) -> str:
        # The authorization key must not collide after IAM's 64-character
        # limit. Keep 208 digest bits instead of truncating a readable key.
        digest = hashlib.sha256(agent_key.encode()).hexdigest()[:52]
        return f"agora-agent-{digest}"

    def ensure_agent_execution_role(
        self,
        agent_key: str,
        name: str,
        stage: str,
        *,
        shared_policy_arn: str,
        permissions_boundary_arn: str,
        provider_name: str = "",
        existing_role_arn: str = "",
        on_created: (
            Callable[[AgentExecutionRoleDeployment], None] | None
        ) = None,
    ) -> AgentExecutionRoleDeployment:
        """Create or reconcile one path-scoped role for an agent."""
        if not shared_policy_arn or not permissions_boundary_arn:
            raise ValueError("Agent execution role IAM coordinates are incomplete")
        iam = self._iam_client()
        role_name = (
            existing_role_arn.rsplit("/", 1)[-1]
            if existing_role_arn
            else self._agent_role_name(agent_key)
        )
        tags = [
            {"Key": "agora:record-id", "Value": agent_key},
            {"Key": "agora:stage", "Value": stage},
            {"Key": "agora:managed", "Value": "true"},
        ]
        trust = {
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {
                    "Service": "bedrock-agentcore.amazonaws.com",
                },
                "Action": "sts:AssumeRole",
            }],
        }
        created = False
        try:
            response = iam.create_role(
                Path="/agora/agent/",
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(
                    trust, separators=(",", ":")
                ),
                PermissionsBoundary=permissions_boundary_arn,
                Description=f"Agora isolated AgentCore role for {name}"[:1000],
                Tags=tags,
            )
            role_arn = str(response["Role"]["Arn"])
            created = True
            if on_created is not None:
                on_created(AgentExecutionRoleDeployment(
                    role_name, role_arn, created=True
                ))
        except Exception as exc:
            if not _is_already_exists(exc):
                raise
            role = iam.get_role(RoleName=role_name)["Role"]
            if str(role.get("Path") or "") != "/agora/agent/":
                raise RuntimeError("existing agent role has an unexpected IAM path")
            actual_boundary = str(
                (role.get("PermissionsBoundary") or {}).get(
                    "PermissionsBoundaryArn"
                )
                or ""
            )
            if actual_boundary != permissions_boundary_arn:
                raise RuntimeError(
                    "existing agent role has an unexpected permissions boundary"
                )
            role_tags = {
                str(tag.get("Key") or ""): str(tag.get("Value") or "")
                for tag in role.get("Tags") or ()
            }
            if role_tags.get("agora:record-id") != agent_key:
                raise RuntimeError(
                    "existing agent role record-id ownership tag does not "
                    f"match {agent_key!r}"
                )
            role_arn = str(role["Arn"])
        iam.tag_role(RoleName=role_name, Tags=tags)
        iam.attach_role_policy(
            RoleName=role_name,
            PolicyArn=shared_policy_arn,
        )
        arn_parts = permissions_boundary_arn.split(":")
        partition, account = arn_parts[1], arn_parts[4]
        workload_name = f"agora-agent-{_sanitize_runtime_name(name)}"[:64]
        identity_resources = [
            (
                f"arn:{partition}:bedrock-agentcore:{self.region}:{account}:"
                "token-vault/default"
            ),
            (
                f"arn:{partition}:bedrock-agentcore:{self.region}:{account}:"
                "workload-identity-directory/default"
            ),
        ]
        if provider_name:
            identity_resources.extend([
                (
                    f"arn:{partition}:bedrock-agentcore:{self.region}:{account}:"
                    "token-vault/default/oauth2credentialprovider/"
                    f"{provider_name}"
                ),
                (
                    f"arn:{partition}:secretsmanager:{self.region}:{account}:secret:"
                    "bedrock-agentcore-identity!default/oauth2/"
                    f"{provider_name}-*"
                ),
            ])
        identity_resources.append(
            f"arn:{partition}:bedrock-agentcore:{self.region}:{account}:"
            "workload-identity-directory/default/workload-identity/"
            f"{workload_name}"
        )
        inline = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "AgentIdentityAccess",
                    "Effect": "Allow",
                    "Action": [
                        "bedrock-agentcore:GetResourceOauth2Token",
                        "bedrock-agentcore:GetWorkloadAccessToken",
                        "secretsmanager:GetSecretValue",
                    ],
                    "Resource": identity_resources,
                },
            ],
        }
        iam.put_role_policy(
            RoleName=role_name,
            PolicyName="AgentIdentityAccess",
            PolicyDocument=json.dumps(inline, separators=(",", ":")),
        )
        return AgentExecutionRoleDeployment(role_name, role_arn, created)

    def get_agent_execution_role(self, role_name: str) -> str:
        return str(
            self._iam_client().get_role(RoleName=role_name)["Role"]["Arn"]
        )

    def retag_agent_execution_role(
        self, role_name: str, *, record_id: str, stage: str,
    ) -> None:
        self._iam_client().tag_role(
            RoleName=role_name,
            Tags=[
                {"Key": "agora:record-id", "Value": record_id},
                {"Key": "agora:stage", "Value": stage},
                {"Key": "agora:managed", "Value": "true"},
            ],
        )

    def delete_agent_execution_role(
        self, role_name: str, *, shared_policy_arn: str,
    ) -> None:
        iam = self._iam_client()
        operations = (
            lambda: iam.delete_role_policy(
                RoleName=role_name,
                PolicyName="AgentIdentityAccess",
            ),
            lambda: iam.detach_role_policy(
                RoleName=role_name,
                PolicyArn=shared_policy_arn,
            ),
            lambda: iam.delete_role(RoleName=role_name),
        )
        for operation in operations:
            try:
                operation()
            except Exception as exc:
                if not _is_not_found(exc):
                    raise

    # ── 빌드 ──────────────────────────────────────────────────────────
    def start_build(self, job_id: str, source_ref: SourceRef, build_type: str,
                    asset_type: str = "mcp") -> str:
        cb, _, _, _ = self._clients()
        # sourcestore S3 prefix 규약: {asset_type}/{asset_id}/{version}/.
        # agent는 sourcestore가 agent/ prefix로 업로드해요(mcp/ 하드코딩이면 소스를 못 받음).
        src_prefix = f"{asset_type}/{source_ref.asset_id}/{source_ref.version}/"
        resp = cb.start_build(
            projectName=self.codebuild_project,
            environmentVariablesOverride=[
                {"name": "ASSET_ID", "value": source_ref.asset_id},
                {"name": "VERSION", "value": source_ref.version},
                {"name": "BUILD_TYPE", "value": build_type},
                # buildspec이 agent/mcp를 분기해요(agent는 tool 추출·하네스 주입 생략, 소스만 zip).
                {"name": "ASSET_TYPE", "value": asset_type},
                {"name": "ARTIFACT_BUCKET", "value": self.artifact_bucket},
                {"name": "SOURCE_PREFIX", "value": job_id},
                # CodeBuild(NO_SOURCE)가 install 단계에서 여기서 소스를 내려받아요.
                {"name": "SOURCE_BUCKET", "value": self.source_bucket},
                {"name": "SOURCE_S3_PREFIX", "value": src_prefix},
                {"name": "SOURCE_REGION", "value": self.source_region},
            ],
        )
        return resp["build"]["id"]

    def get_build_status(self, build_id: str) -> BuildStatus:
        cb, _, _, s3 = self._clients()
        builds = cb.batch_get_builds(ids=[build_id]).get("builds", [])
        if not builds:
            return BuildStatus(state="FAILED", reason="build not found")
        b = builds[0]
        st = b["buildStatus"]
        if st == "IN_PROGRESS":
            return BuildStatus(state="IN_PROGRESS")
        if st == "SUCCEEDED":
            env_vars = {e["name"]: e["value"]
                        for e in b.get("exportedEnvironmentVariables", [])}
            artifact_key = env_vars.get("ARTIFACT_KEY", "")
            mcp_module = env_vars.get("MCP_MODULE", "")
            otel_instrumented = (
                env_vars.get("OTEL_ENTRYPOINT_ENABLED", "").lower() == "true"
            )
            # I2 사이드카: tool 스키마는 TOOLS_INLINE(5120자 한계) 대신 tools.json을
            # S3에서 fetch해요. TOOLS_S3_KEY가 있으면 그 key로 읽고, 없으면 하위호환으로
            # TOOLS_INLINE exported-var를 그대로 써요(작은 스키마용 폴백).
            tools_s3_key = env_vars.get("TOOLS_S3_KEY", "")
            tools_inline = env_vars.get("TOOLS_INLINE", "")
            if tools_s3_key:
                try:
                    obj = s3.get_object(Bucket=self.artifact_bucket, Key=tools_s3_key)
                    tools_inline = obj["Body"].read().decode("utf-8")
                except Exception as e:
                    return BuildStatus(
                        state="FAILED",
                        reason=f"tools.json fetch 실패(s3://{self.artifact_bucket}/"
                               f"{tools_s3_key}): {e}",
                    )
            return BuildStatus(
                state="SUCCEEDED",
                artifact=BuildArtifact(
                    build_type="codezip",
                    uri=artifact_key,
                    tools_inline=tools_inline,
                    tools_s3_key=tools_s3_key,
                    mcp_module=mcp_module,
                    otel_instrumented=otel_instrumented,
                ),
            )
        return BuildStatus(state="FAILED", reason=f"buildStatus={st}")

    # ── Lambda 함수 ───────────────────────────────────────────────────
    def _update_existing_lambda_code(
        self,
        lam,
        name: str,
        artifact: BuildArtifact,
        *,
        configuration: dict,
        owner_job_id: str,
        owner_asset_id: str,
        owner_principal: str,
        owner_source_version: str,
    ) -> LambdaDeployment:
        """관측 R0에 marker/env를 쓰고 응답 R1에 코드를 결속해요."""
        existing_owner = _lambda_owner(configuration)
        if (
            existing_owner is None
            and _lambda_owner_marker_present(configuration)
        ) or (
            existing_owner is not None
            and (
                existing_owner.asset_id != owner_asset_id
                or (
                    existing_owner.principal is None
                    and existing_owner.job_id != owner_job_id
                )
                or (
                    existing_owner.principal is not None
                    and existing_owner.principal != owner_principal
                )
            )
        ):
            raise RuntimeError(
                _lambda_ownership_conflict_message(
                    name=name,
                    existing_owner=existing_owner,
                    expected_job_id=owner_job_id,
                    expected_asset_id=owner_asset_id,
                    expected_principal=owner_principal,
                )
            )
        incoming_source_version = _parse_source_version(owner_source_version)
        if (
            existing_owner is not None
            and existing_owner.source_version is not None
            and _parse_source_version(existing_owner.source_version)
            > incoming_source_version
        ):
            raise RuntimeError(
                f"Lambda {name!r} has source version "
                f"{existing_owner.source_version!r}, which is newer than "
                f"incoming source version {owner_source_version!r}; "
                "configuration and code were not updated"
            )
        revision_id = configuration.get("RevisionId")
        if not isinstance(revision_id, str) or not revision_id:
            raise RuntimeError(
                f"Lambda revision could not be observed for {name!r}; "
                "code was not updated"
            )
        configuration_kwargs = {
            "FunctionName": name,
            "RevisionId": revision_id,
            "Description": _lambda_owner_description(
                job_id=owner_job_id,
                asset_id=owner_asset_id,
                principal=owner_principal,
                source_version=owner_source_version,
            ),
        }
        if artifact.mcp_module:
            configuration_kwargs["Environment"] = {
                "Variables": {
                    "AGORA_MCP_MODULE": artifact.mcp_module,
                }
            }
        configuration_resp = lam.update_function_configuration(
            **configuration_kwargs
        )
        configured_revision_id = configuration_resp.get("RevisionId")
        if (
            not isinstance(configured_revision_id, str)
            or not configured_revision_id
        ):
            raise RuntimeError(
                f"Lambda revision could not be observed after configuration "
                f"update for {name!r}; code was not updated"
            )
        lam.get_waiter("function_updated_v2").wait(
            FunctionName=name,
            WaiterConfig={"Delay": 3, "MaxAttempts": 40},
        )
        # **waiter 뒤에 revision 을 다시 읽어요.**
        #
        # `update_function_configuration` 응답의 `RevisionId` 는 `LastUpdateStatus` 가 아직
        # `InProgress` 인 시점의 값이에요. 업데이트가 정착하면서 AWS 가 revision 을 **한 번 더
        # 올려서**, 그 값으로 코드를 쓰면 실패해요 (2026-08-30 실측:
        # `PreconditionFailedException: The Revision Id provided does not match the latest`
        # — MCP 재배포가 여기서 전부 막혔어요).
        # 초기 읽기와 **같은 접근자**(`get_function`)를 써요 — 계약을 하나로 유지해요.
        settled = lam.get_function(FunctionName=name).get("Configuration") or {}
        settled_revision_id = settled.get("RevisionId")
        if not isinstance(settled_revision_id, str) or not settled_revision_id:
            # 정착 후 revision 을 못 읽으면 설정 응답 값으로 진행해요. 그때는 이 수정 이전과
            # **같은 동작**이라 나빠지지 않아요 — 실패하면 `PreconditionFailed` 로 드러나요.
            settled_revision_id = configured_revision_id
        # revision 을 다시 읽어도 낙관적 동시성은 유지돼요. 소유 판정은 **설정 갱신 전**에
        # 이미 했고(위 `existing_owner` 검사), 그 갱신이 우리 표식을 박았어요. 이 재조회와
        # 아래 코드 쓰기 사이에 남이 끼어들면 그쪽이 revision 을 올려서
        # `update_function_code` 가 스스로 `PreconditionFailed` 로 막아요 — 그게 이 인자의
        # 용도예요.
        resp = lam.update_function_code(
            FunctionName=name,
            S3Bucket=self.artifact_bucket,
            S3Key=artifact.uri,
            Publish=False,
            RevisionId=settled_revision_id,
        )
        function_arn = resp["FunctionArn"]
        lam.get_waiter("function_updated_v2").wait(
            FunctionName=name,
            WaiterConfig={"Delay": 3, "MaxAttempts": 40},
        )
        return LambdaDeployment(arn=function_arn, created=False)

    def create_lambda(
        self,
        name: str,
        artifact: BuildArtifact,
        *,
        exec_role_arn: str,
        owner_job_id: str,
        owner_asset_id: str,
        owner_principal: str,
        owner_source_version: str,
    ) -> LambdaDeployment:
        _, lam, _, _ = self._clients()
        # C1: 하네스(agora_mcp_handler)가 AGORA_MCP_MODULE env로 MCP 모듈을 import해요.
        # 빌드가 산출한 모듈명(artifact.mcp_module)을 Lambda 환경변수로 배선해요.
        create_kwargs = dict(
            FunctionName=name,
            Runtime="python3.12",
            Handler="agora_mcp_handler.lambda_handler",
            Role=exec_role_arn,
            Code={"S3Bucket": self.artifact_bucket, "S3Key": artifact.uri},
            Description=_lambda_owner_description(
                job_id=owner_job_id,
                asset_id=owner_asset_id,
                principal=owner_principal,
                source_version=owner_source_version,
            ),
            Timeout=60,
            MemorySize=512,
            Architectures=["arm64"],
        )
        if artifact.mcp_module:
            create_kwargs["Environment"] = {
                "Variables": {"AGORA_MCP_MODULE": artifact.mcp_module}
            }
        try:
            resp = lam.create_function(**create_kwargs)
            function_arn = resp["FunctionArn"]
            created = True
        except Exception as e:
            if not _is_conflict(e):
                raise
            existing = lam.get_function(FunctionName=name)
            configuration = existing.get("Configuration")
            if not isinstance(configuration, dict):
                raise RuntimeError(
                    f"Lambda ownership could not be observed for {name!r}: "
                    "GetFunction response has no Configuration"
                ) from e
            existing_owner = _lambda_owner(configuration)
            same_job_reentry = (
                existing_owner is not None
                and existing_owner.job_id == owner_job_id
                and existing_owner.asset_id == owner_asset_id
                and (
                    existing_owner.principal is None
                    or existing_owner.principal == owner_principal
                )
                and (
                    existing_owner.source_version is None
                    or existing_owner.source_version == owner_source_version
                )
            )
            cross_job_update = (
                existing_owner is not None
                and existing_owner.job_id != owner_job_id
                and existing_owner.asset_id == owner_asset_id
                and existing_owner.principal == owner_principal
            )
            if same_job_reentry or cross_job_update:
                # marker는 configuration 의도이지 code 적용 완료 증거가 아니므로,
                # 허용된 충돌은 모두 create 폴백 없는 조건부 사슬로 다시 써요.
                return self._update_existing_lambda_code(
                    lam,
                    name,
                    artifact,
                    configuration=configuration,
                    owner_job_id=owner_job_id,
                    owner_asset_id=owner_asset_id,
                    owner_principal=owner_principal,
                    owner_source_version=owner_source_version,
                )
            raise RuntimeError(
                _lambda_ownership_conflict_message(
                    name=name,
                    existing_owner=existing_owner,
                    expected_job_id=owner_job_id,
                    expected_asset_id=owner_asset_id,
                    expected_principal=owner_principal,
                )
            ) from e
        # 생성 직후 Lambda는 Pending 상태예요. Active가 되기 전에 Gateway 타깃에 붙이면
        # "not ready or resource conflict"로 거부돼요(실측: 2026-07-16). Active까지 대기.
        try:
            lam.get_waiter("function_active_v2").wait(
                FunctionName=name,
                WaiterConfig={"Delay": 3, "MaxAttempts": 40},
            )
        except Exception:
            pass  # waiter 미지원/타임아웃이어도 arn은 반환 — 후속 wire 재시도가 방어선
        return LambdaDeployment(arn=function_arn, created=created)

    def update_lambda_code(
        self,
        name: str,
        artifact: BuildArtifact,
        *,
        exec_role_arn: str,
        owner_job_id: str,
        owner_asset_id: str,
        owner_principal: str,
        owner_source_version: str,
    ) -> LambdaDeployment:
        """재배포: 기존 Lambda의 marker·MCP 모듈 env·코드를 갱신해요(ADR-0021).

        같은 함수명이라 ARN이 유지돼, Gateway target(clientToken=hash(gateway:name:arn))도
        멱등 재사용돼 endpoint가 보존돼요. create_function은 코드를 갱신하지 않으므로 재배포엔
        이 update 경로가 필수예요.

        upsert: 레코드는 있지만 Lambda가 없으면(수동 삭제·미배포 레코드 재배포) 새로 생성해요.
        `Function not found`로 실패하는 대신 create로 폴백해 재배포가 복원 역할을 하게 해요.
        """
        _, lam, _, _ = self._clients()
        try:
            existing = lam.get_function(FunctionName=name)
        except Exception as exc:
            if _is_not_found(exc):
                return self.create_lambda(
                    name,
                    artifact,
                    exec_role_arn=exec_role_arn,
                    owner_job_id=owner_job_id,
                    owner_asset_id=owner_asset_id,
                    owner_principal=owner_principal,
                    owner_source_version=owner_source_version,
                )
            raise
        configuration = existing.get("Configuration")
        if not isinstance(configuration, dict):
            raise RuntimeError(
                f"Lambda ownership could not be observed for {name!r}: "
                "GetFunction response has no Configuration"
            )
        return self._update_existing_lambda_code(
            lam,
            name,
            artifact,
            configuration=configuration,
            owner_job_id=owner_job_id,
            owner_asset_id=owner_asset_id,
            owner_principal=owner_principal,
            owner_source_version=owner_source_version,
        )

    def verify_lambda_owner(
        self,
        lambda_arn: str,
        *,
        expected_owner_job_id: str,
        expected_owner_asset_id: str,
        expected_owner_principal: str,
    ) -> None:
        """Lambda 제어 평면의 owner 표식을 자동 보상 전에 대조해요."""
        _, lam, _, _ = self._clients()
        try:
            existing = lam.get_function(FunctionName=lambda_arn)
        except Exception as exc:
            detail = (
                "function was not found"
                if _is_not_found(exc)
                else f"{type(exc).__name__}: {exc}"
            )
            raise RuntimeError(
                "Lambda ownership could not be observed for "
                f"{lambda_arn!r}: {detail}; automatic compensation "
                "will preserve its coordinates"
            ) from exc
        configuration = existing.get("Configuration")
        if not isinstance(configuration, dict):
            raise RuntimeError(
                "Lambda ownership could not be observed for "
                f"{lambda_arn!r}: GetFunction response has no Configuration; "
                "automatic compensation will preserve its coordinates"
            )
        existing_owner = _lambda_owner(configuration)
        if (
            existing_owner is None
            or existing_owner.principal is None
            or existing_owner.job_id != expected_owner_job_id
            or existing_owner.asset_id != expected_owner_asset_id
            or existing_owner.principal != expected_owner_principal
        ):
            raise RuntimeError(
                _lambda_ownership_conflict_message(
                    name=lambda_arn,
                    existing_owner=existing_owner,
                    expected_job_id=expected_owner_job_id,
                    expected_asset_id=expected_owner_asset_id,
                    expected_principal=expected_owner_principal,
                )
            )

    # ── Gateway 타깃 ──────────────────────────────────────────────────
    def find_gateway_target(self, gateway_id: str, name: str) -> str | None:
        """gateway에서 주어진 이름의 target id를 찾아요. 없으면 None(ADR-0021 재배포용).

        재배포는 기존 target을 재사용해야 endpoint·tool prefix가 보존돼요. 이름은 재배포에서
        고정(기존 gatewayTargetName)이라, list로 찾아 재사용/재생성을 결정해요.
        """
        _, _, ctrl, _ = self._clients()
        next_token = None
        while True:
            kwargs = {"gatewayIdentifier": gateway_id}
            if next_token:
                kwargs["nextToken"] = next_token
            resp = ctrl.list_gateway_targets(**kwargs)
            items = resp.get("items")
            if not isinstance(items, list):
                raise ValueError(
                    "ListGatewayTargets response has no items list"
                )
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError(
                        "ListGatewayTargets response has a non-object item"
                    )
                if item.get("name") == name:
                    target_id = item.get("targetId")
                    if not isinstance(target_id, str) or not target_id:
                        raise ValueError(
                            "ListGatewayTargets matching item has no targetId"
                        )
                    return target_id
            next_token = resp.get("nextToken")
            if not next_token:
                return None

    def wire_lambda_target(
        self, gateway_id: str, name: str, lambda_arn: str, tools_inline: str,
        *, client_token_seed: str = "",
    ) -> str:
        _, _, ctrl, _ = self._clients()
        # I1: 빌드 산출 tool 스키마가 비어있거나 malformed면 명확한 에러로 반려해요.
        # (advance의 try/except가 잡아 teardown+FAILED로 이어지되, 원인이 불투명해지지
        #  않도록 여기서 계약 위반을 짚어요.)
        # TODO(IA-22): AgentCore ToolDefinition에 metadata 확장점이 생기면 sensitivity를 전달.
        gateway_tools = _gateway_tools_from_inline(tools_inline)
        # CreateGatewayTarget의 clientToken으로 같은 job의 중복 poll을 AWS 수준에서 멱등화해요.
        # client_token_seed(=job_id)를 섞어 배포 job마다 유니크하게 만들어, 이전(삭제된) target의
        # 토큰과 충돌하지 않게 해요(ADR-0021 재배포). 같은 job의 동시 poll은 같은 seed→같은 토큰이라
        # 멱등이 유지돼요. (재배포에서 기존 target이 있으면 create 자체를 건너뛰고 재사용해요 — jobs.py.)
        client_token = hashlib.sha256(
            f"{gateway_id}:{name}:{lambda_arn}:{client_token_seed}".encode("utf-8")
        ).hexdigest()
        resp = ctrl.create_gateway_target(
            gatewayIdentifier=gateway_id,
            name=name,
            clientToken=client_token,
            targetConfiguration=_lambda_target_config(lambda_arn, gateway_tools),
            credentialProviderConfigurations=_GATEWAY_CRED_CONFIG,
        )
        return resp["targetId"]

    def update_gateway_target(
        self, gateway_id: str, target_id: str, name: str, lambda_arn: str,
        tools_inline: str,
    ) -> bool:
        """재배포 시 재사용 target의 inline tool schema를 새 세트로 갱신해요(CA-17).

        재배포는 기존 Gateway target을 멱등 재사용(jobs.py)하는데, tool 세트가 바뀌어도
        target 스키마가 옛 세트로 남는 문제를 고쳐요. 현재 target 스키마와 새 세트를 비교해
        동일하면 UpdateGatewayTarget을 호출하지 않고 False를 돌려줘요(멱등·불필요한
        재동기화 회피). 다르면 전체 config를 교체 갱신하고 True를 돌려줘요(target이
        UPDATING→READY로 재동기화 — get_target_status가 UPDATING을 SYNCHRONIZING으로 매핑).

        UpdateGatewayTarget은 full-replace라 name·targetConfiguration·credential을 모두
        실어야 해요(실측: gatewayIdentifier·targetId·name·targetConfiguration 필수).
        갱신 실패는 예외로 던져 advance의 try/except가 명확한 에러로 반려하게 해요 —
        절반만 갱신된 target을 조용히 남기지 않아요.
        """
        _, _, ctrl, _ = self._clients()
        new_tools = _gateway_tools_from_inline(tools_inline)
        current = ctrl.get_gateway_target(
            gatewayIdentifier=gateway_id, targetId=target_id)
        current_tools = _current_target_tools(current)
        if _tools_index(current_tools) == _tools_index(new_tools):
            return False
        ctrl.update_gateway_target(
            gatewayIdentifier=gateway_id,
            targetId=target_id,
            name=name,
            targetConfiguration=_lambda_target_config(lambda_arn, new_tools),
            credentialProviderConfigurations=_GATEWAY_CRED_CONFIG,
        )
        return True

    @staticmethod
    def _synchronization_time(value: object) -> float | None:
        if isinstance(value, datetime):
            observed = value
        elif isinstance(value, str) and value.strip():
            try:
                observed = datetime.fromisoformat(
                    value.strip().replace("Z", "+00:00")
                )
            except ValueError:
                return None
        else:
            return None
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        return observed.timestamp()

    @staticmethod
    def _connected_target_endpoint(observation: dict) -> str:
        configuration = observation.get("targetConfiguration")
        if not isinstance(configuration, dict):
            return ""
        mcp = configuration.get("mcp")
        if not isinstance(mcp, dict):
            return ""
        server = mcp.get("mcpServer")
        if not isinstance(server, dict):
            return ""
        return str(server.get("endpoint") or "").strip()

    @classmethod
    def _connected_target_coordinate_error(
        cls,
        observation: dict,
        *,
        expected_gateway_id: str,
        expected_target_id: str,
        expected_target_name: str,
        expected_endpoint: str,
    ) -> str:
        gateway_arn = str(observation.get("gatewayArn") or "")
        if gateway_arn.rsplit("/", 1)[-1] != expected_gateway_id:
            return "GetGatewayTarget gateway ARN did not match the mutation gateway"
        if str(observation.get("targetId") or "") != expected_target_id:
            return "GetGatewayTarget target ID did not match the mutation target"
        if str(observation.get("name") or "") != expected_target_name:
            return "GetGatewayTarget target name did not match the policy action"
        if cls._connected_target_endpoint(observation) != expected_endpoint:
            return "GetGatewayTarget mcpServer endpoint did not match the catalog"
        return ""

    def synchronize_gateway_target(
        self,
        gateway_id: str,
        target_id: str,
        *,
        expected_target_name: str,
        expected_endpoint: str,
    ) -> dict:
        """Explicitly refresh one connected MCP Target and observe completion.

        SynchronizeGatewayTargets returns before the refresh completes. Its response
        is never success evidence: a newer ``lastSynchronizedAt`` on a READY
        GetGatewayTarget observation is required, and that same observation supplies
        the endpoint used by the caller's independent post-sync tools/list probe.
        """
        _, _, control, _ = self._clients()

        def unknown(reason: str) -> dict:
            return {"status": "unknown", "reason": reason}

        try:
            before = control.get_gateway_target(
                gatewayIdentifier=gateway_id,
                targetId=target_id,
            )
        except Exception as exc:
            return unknown(
                f"GetGatewayTarget baseline observation failed: {exc}"
            )
        coordinate_error = self._connected_target_coordinate_error(
            before,
            expected_gateway_id=gateway_id,
            expected_target_id=target_id,
            expected_target_name=expected_target_name,
            expected_endpoint=expected_endpoint,
        )
        if coordinate_error:
            return unknown(coordinate_error)
        before_time = self._synchronization_time(
            before.get("lastSynchronizedAt")
        )
        try:
            control.synchronize_gateway_targets(
                gatewayIdentifier=gateway_id,
                targetIdList=[target_id],
            )
        except Exception as exc:
            return unknown(f"SynchronizeGatewayTargets failed: {exc}")

        last_status = ""
        saw_stale_ready = False
        for attempt in range(self._synchronization_attempts):
            try:
                observation = control.get_gateway_target(
                    gatewayIdentifier=gateway_id,
                    targetId=target_id,
                )
            except Exception as exc:
                return unknown(
                    f"GetGatewayTarget completion observation failed: {exc}"
                )
            coordinate_error = self._connected_target_coordinate_error(
                observation,
                expected_gateway_id=gateway_id,
                expected_target_id=target_id,
                expected_target_name=expected_target_name,
                expected_endpoint=expected_endpoint,
            )
            if coordinate_error:
                return unknown(coordinate_error)
            last_status = str(observation.get("status") or "").upper()
            synchronized_at = observation.get("lastSynchronizedAt")
            synchronized_time = self._synchronization_time(synchronized_at)
            is_new_observation = (
                before_time is not None
                and synchronized_time is not None
                and synchronized_time > before_time
            )
            if last_status == "READY" and is_new_observation:
                endpoint = self._connected_target_endpoint(observation)
                if not endpoint:
                    return unknown(
                        "GetGatewayTarget READY observation has no mcpServer endpoint"
                    )
                return {
                    "status": "ready",
                    "endpoint": endpoint,
                    "last_synchronized_at": synchronized_at,
                }
            if last_status == "READY":
                saw_stale_ready = True
            if last_status in {
                "FAILED",
                "UPDATE_UNSUCCESSFUL",
                "SYNCHRONIZE_UNSUCCESSFUL",
                "CREATE_PENDING_AUTH",
                "UPDATE_PENDING_AUTH",
                "SYNCHRONIZE_PENDING_AUTH",
            }:
                return unknown(
                    f"GetGatewayTarget synchronization failed with {last_status}"
                )
            if attempt + 1 < self._synchronization_attempts:
                self._synchronization_sleep(3)

        if saw_stale_ready:
            return unknown(
                "GetGatewayTarget remained READY without a newer "
                "lastSynchronizedAt observation"
            )
        return unknown(
            "GetGatewayTarget synchronization did not complete; "
            f"last status was {last_status or 'unknown'}"
        )

    def wire_mcp_server_target(self, gateway_id: str, name: str,
                               endpoint: str, tools_inline: str = "") -> str:
        """외부 MCP endpoint를 Gateway mcpServer 타깃으로 등재. 반환: target_id.

        mcpServer 타깃은 아웃바운드 IAM을 지원하지 않아요(OAuth/None만). 공개 endpoint
        전제로 credential을 생략해요(NONE).

        tool 스키마는 넘기지 않아요 — 실측(2026-07-17): mcpServer 타깃의 `mcpToolSchema`는
        AUTHORIZATION_CODE(3LO OAuth) grant일 때만 허용돼요("mcpToolSchema is only
        supported for MCP Server targets with AUTHORIZATION_CODE grant type"). 공개(NONE)
        endpoint에선 Gateway가 SYNCHRONIZING 중 upstream에 tools/list를 쳐서 tool을 직접
        발견해요. 그래서 tools_inline은 시그니처 호환용으로만 남기고 사용하지 않아요.
        sensitivity는 catalog/Aux descriptor에 남고, static schema를 지원하는 인증 방식이
        도입되면 AgentCore ToolDefinition 확장 가능 여부를 다시 확인해 반영해요.
        """
        _, _, ctrl, _ = self._clients()
        resp = ctrl.create_gateway_target(
            gatewayIdentifier=gateway_id, name=name,
            targetConfiguration={"mcp": {"mcpServer": {"endpoint": endpoint}}})
        return resp["targetId"]

    def delete_target(self, gateway_id: str, target_id: str) -> None:
        """gateway target만 삭제(Lambda 없는 connect MCP용).

        실패를 호출자에게 전파해야 PurgeService가 레코드를 지우기 전에 재시도 좌표를
        보존할 수 있어요. 성공처럼 삼키면 IA-42 gateway 이관 뒤 orphan을 찾을 수 없어요.
        """
        _, _, ctrl, _ = self._clients()
        if target_id:
            try:
                ctrl.delete_gateway_target(
                    gatewayIdentifier=gateway_id,
                    targetId=target_id,
                )
            except Exception as exc:
                if _is_not_found(exc):
                    return
                if _is_gateway_target_deleting(exc):
                    self._confirm_gateway_target_absent(
                        ctrl,
                        gateway_id=gateway_id,
                        target_id=target_id,
                        deletion_error=exc,
                    )
                    return
                raise
            self._confirm_gateway_target_absent(
                ctrl,
                gateway_id=gateway_id,
                target_id=target_id,
                deletion_error=None,
            )

    def _confirm_gateway_target_absent(
        self,
        ctrl,
        *,
        gateway_id: str,
        target_id: str,
        deletion_error: Exception | None,
    ) -> None:
        """삭제 요청 뒤 Target이 실제로 사라졌는지 bounded poll로 확인해요."""
        last_status = "unknown"
        for attempt in range(self._synchronization_attempts):
            try:
                observation = ctrl.get_gateway_target(
                    gatewayIdentifier=gateway_id,
                    targetId=target_id,
                )
            except Exception as exc:
                if _is_not_found(exc):
                    return
                raise RuntimeError(
                    "Gateway target deletion could not be observed: "
                    f"{gateway_id}/{target_id}: {type(exc).__name__}: {exc}"
                ) from exc
            if not isinstance(observation, dict):
                raise RuntimeError(
                    "Gateway target deletion returned an unobservable response: "
                    f"{gateway_id}/{target_id}: {type(observation).__name__}"
                )
            last_status = str(observation.get("status") or "unknown")
            if attempt + 1 < self._synchronization_attempts:
                self._synchronization_sleep(1)
        raise RuntimeError(
            "Gateway target remained observable after deletion was requested: "
            f"{gateway_id}/{target_id}; status={last_status}"
        ) from deletion_error

    def get_target_status(self, gateway_id: str, target_id: str) -> TargetStatus:
        _, _, ctrl, _ = self._clients()
        resp = ctrl.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
        st = resp.get("status", "")
        if st in ("CREATING", "UPDATING", "SYNCHRONIZING"):
            return TargetStatus(state="SYNCHRONIZING")
        if st == "READY":
            return TargetStatus(state="READY")
        return TargetStatus(state="FAILED", reason=st)

    def gateway_endpoint(self, gateway_id: str) -> str:
        _, _, ctrl, _ = self._clients()
        resp = ctrl.get_gateway(gatewayIdentifier=gateway_id)
        return resp.get("gatewayUrl", "")

    def observe_gateway_target_quota(self, gateway_id: str) -> dict:
        """Compare independently observed target inventory with applied quota."""
        sources = {
            "current": "ListGatewayTargets",
            "quota": "ServiceQuotas.GetServiceQuota",
        }

        def _unknown(reason: str) -> dict:
            return {
                "status": "unknown",
                "current": None,
                "quota": None,
                "usage_ratio": None,
                "threshold_ratio": _GATEWAY_TARGET_QUOTA_THRESHOLD,
                "threshold_reached": None,
                "reason": reason,
                "sources": sources,
            }

        if not gateway_id:
            return _unknown("gateway identifier is not configured")
        try:
            _, _, control, _ = self._clients()
            current = 0
            next_token = None
            while True:
                request = {
                    "gatewayIdentifier": gateway_id,
                    "maxResults": 1000,
                }
                if next_token:
                    request["nextToken"] = next_token
                response = control.list_gateway_targets(**request)
                items = response.get("items")
                if not isinstance(items, list):
                    raise ValueError(
                        "ListGatewayTargets response has no items list"
                    )
                current += len(items)
                next_token = response.get("nextToken")
                if not next_token:
                    break

            quota_code = ""
            quotas = self._service_quotas_client()
            next_token = None
            while True:
                request = {"ServiceCode": "bedrock-agentcore"}
                if next_token:
                    request["NextToken"] = next_token
                response = quotas.list_service_quotas(**request)
                quota_items = response.get("Quotas")
                if not isinstance(quota_items, list):
                    raise ValueError(
                        "ListServiceQuotas response has no Quotas list"
                    )
                for item in quota_items:
                    if (
                        str(item.get("QuotaName") or "").strip().casefold()
                        == _GATEWAY_TARGET_QUOTA_NAME.casefold()
                        or str(item.get("QuotaCode") or "").strip()
                        == _GATEWAY_TARGET_QUOTA_CODE
                    ):
                        quota_code = str(item.get("QuotaCode") or "")
                        break
                if quota_code:
                    break
                next_token = response.get("NextToken")
                if not next_token:
                    break
            if not quota_code:
                raise ValueError(
                    f"Service Quota not found: {_GATEWAY_TARGET_QUOTA_NAME}"
                )
            quota_response = quotas.get_service_quota(
                ServiceCode="bedrock-agentcore",
                QuotaCode=quota_code,
            )
            raw_quota = (quota_response.get("Quota") or {}).get("Value")
            if isinstance(raw_quota, bool) or not isinstance(
                raw_quota,
                (int, float),
            ):
                raise ValueError("GetServiceQuota response has no numeric Value")
            quota_value = float(raw_quota)
            if quota_value <= 0:
                raise ValueError("GetServiceQuota returned a non-positive Value")
            quota = (
                int(quota_value)
                if quota_value.is_integer()
                else quota_value
            )
            usage_ratio = current / quota_value
            return {
                "status": "ok",
                "current": current,
                "quota": quota,
                "usage_ratio": usage_ratio,
                "threshold_ratio": _GATEWAY_TARGET_QUOTA_THRESHOLD,
                "threshold_reached": (
                    usage_ratio >= _GATEWAY_TARGET_QUOTA_THRESHOLD
                ),
                "reason": None,
                "sources": sources,
            }
        except Exception as exc:
            return _unknown(f"{type(exc).__name__}: {exc}")

    # ── AgentCore Runtime (agent 호스팅 경로) ─────────────────────────
    # ── Identity P2: outbound OAuth (배포 agent → Cognito 보호 Gateway) ──
    def _cognito(self):
        if self._cog is None:
            try:
                import boto3
            except ImportError as e:  # pragma: no cover
                raise RuntimeError(_PREVIEW_MSG) from e
            self._cog = boto3.client("cognito-idp", region_name=self.region)
        return self._cog

    def _control(self):
        if self._ctrl is None:
            try:
                import boto3
            except ImportError as e:  # pragma: no cover
                raise RuntimeError(_PREVIEW_MSG) from e
            self._ctrl = boto3.client(
                "bedrock-agentcore-control", region_name=self.region
            )
        return self._ctrl

    def _agentcore_data(self):
        if self._agentcore is None:
            try:
                import boto3
            except ImportError as e:  # pragma: no cover
                raise RuntimeError(_PREVIEW_MSG) from e
            self._agentcore = boto3.client(
                "bedrock-agentcore",
                region_name=self.region,
            )
        return self._agentcore

    def _probe_identity_outbound_token(
        self,
        *,
        workload_name: str,
        provider_name: str,
        scope: str,
    ) -> None:
        """Issue one M2M resource token without retaining either token."""
        client = self._agentcore_data()
        for attempt in range(self._synchronization_attempts):
            try:
                workload_response = client.get_workload_access_token(
                    workloadName=workload_name,
                )
                workload_token = str(
                    workload_response.get("workloadAccessToken") or ""
                )
                if not workload_token:
                    raise RuntimeError(
                        "GetWorkloadAccessToken returned no workloadAccessToken"
                    )
                resource_response = client.get_resource_oauth2_token(
                    workloadIdentityToken=workload_token,
                    resourceCredentialProviderName=provider_name,
                    scopes=[scope] if scope else [],
                    oauth2Flow="M2M",
                    forceAuthentication=True,
                )
                if not str(resource_response.get("accessToken") or ""):
                    raise RuntimeError(
                        "GetResourceOauth2Token returned no accessToken"
                    )
                return
            except Exception as exc:
                if (
                    not _identity_probe_is_not_ready(exc)
                    or attempt + 1 >= self._synchronization_attempts
                ):
                    raise
                self._synchronization_sleep(1)

    def ensure_oauth2_credential_provider(
        self,
        *,
        name: str,
        discovery_url: str,
        user_pool_id: str,
        client_id: str,
        client_secret: str,
    ) -> bool:
        """AgentCore Identity credential provider get-or-create (멱등).

        secret은 이 호출 payload에만 실려 Token Vault로 들어가고 저장하지 않아요.
        provider 이름은 agent별 client_id에서 파생돼 agent별 Token Vault 항목이 생겨요.
        discovery URL은 그 secret을 읽은 Cognito pool과 반드시 같아야 해요.

        주의(실측 2026-07-27): 이미 있을 때 나오는 건 ConflictException이 아니라
        **ValidationException**("Credential provider with name: X already exists")이에요.
        _is_conflict로는 못 걸러내서, 두 번째 배포부터 env 주입이 조용히 빠졌어요.
        그래서 먼저 get으로 존재를 확인하고, create 예외도 "already exists"까지 봐요.
        """
        expected_discovery_url = _oidc_discovery_url(discovery_url)
        requested_pool_id = cognito_pool_id_from_discovery(
            expected_discovery_url
        )
        if requested_pool_id != user_pool_id:
            raise RuntimeError(
                "OAuth credential provider pool mismatch: "
                f"secret_pool_id={user_pool_id!r}, "
                f"discovery_pool_id={requested_pool_id or '<unparseable>'!r}"
            )

        def validate_existing(existing: dict) -> None:
            provider_output = existing.get("oauth2ProviderConfigOutput")
            provider_output = (
                provider_output if isinstance(provider_output, dict) else {}
            )
            custom_output = provider_output.get(
                "customOauth2ProviderConfig"
            )
            custom_output = (
                custom_output if isinstance(custom_output, dict) else {}
            )
            existing_discovery = custom_output.get("oauthDiscovery")
            existing_discovery = (
                existing_discovery
                if isinstance(existing_discovery, dict)
                else {}
            )
            actual_discovery_url = str(
                existing_discovery.get("discoveryUrl") or ""
            )
            actual_pool_id = cognito_pool_id_from_discovery(
                actual_discovery_url
            )
            if (
                actual_pool_id != user_pool_id
                or actual_discovery_url != expected_discovery_url
            ):
                raise RuntimeError(
                    "OAuth credential provider discovery mismatch: "
                    f"expected_pool_id={user_pool_id!r}, "
                    f"existing_pool_id={actual_pool_id or '<unparseable>'!r}"
                )
            if custom_output.get("clientId") != client_id:
                raise RuntimeError(
                    "OAuth credential provider client_id mismatch"
                )

        ctrl = self._control()
        try:
            existing = ctrl.get_oauth2_credential_provider(name=name)
        except Exception:
            pass                                     # 없거나 조회 불가 → create 시도
        else:
            validate_existing(existing)
            return False
        try:
            ctrl.create_oauth2_credential_provider(
                name=name,
                credentialProviderVendor="CustomOauth2",
                oauth2ProviderConfigInput={
                    "customOauth2ProviderConfig": {
                        "oauthDiscovery": {
                            "discoveryUrl": expected_discovery_url
                        },
                        "clientId": client_id,
                        "clientSecret": client_secret,
                    }
                },
            )
        except Exception as e:
            if not _is_already_exists(e):
                raise
            try:
                existing = ctrl.get_oauth2_credential_provider(name=name)
            except Exception as read_error:
                raise RuntimeError(
                    "OAuth credential provider already exists but its "
                    "discovery configuration could not be validated"
                ) from read_error
            validate_existing(existing)
            return False
        return True

    def ensure_oauth2_credential_provider_for_cognito_client(
        self,
        *,
        name: str,
        discovery_url: str,
        user_pool_id: str,
        client_id: str,
    ) -> None:
        """Read a client secret only long enough to place it in Token Vault."""
        secret = self._cognito_client_secret(user_pool_id, client_id)
        self.ensure_oauth2_credential_provider(
            name=name,
            discovery_url=discovery_url,
            user_pool_id=user_pool_id,
            client_id=client_id,
            client_secret=secret,
        )

    def ensure_workload_identity(
        self,
        name: str,
        *,
        allowed_resource_oauth2_return_urls: tuple[str, ...],
    ) -> bool:
        """우리 소유 workload identity create-or-reconcile (멱등).

        Runtime이 자동 생성하는 identity는 service-linked라 컨테이너가
        GetWorkloadAccessToken을 호출하면 ValidationException이 나요(실측 2026-07-27:
        "WorkloadIdentity is linked to a service"). 그래서 별도로 하나 만들어
        그 이름을 env로 주입해요.

        ADR-0101의 outbound 3LO가 redirect될 수 있는 포털 주소를 정확히 한 개만
        허용해요. 기존 identity도 목록을 읽고 다르면 UpdateWorkloadIdentity로 맞춰서,
        create 인자만 고친 뒤 라이브 identity가 빈 목록으로 남는 구멍을 막아요.
        """
        expected_urls = list(dict.fromkeys(
            url.strip()
            for url in allowed_resource_oauth2_return_urls
            if url.strip()
        ))
        if not expected_urls:
            raise ValueError(
                "allowedResourceOauth2ReturnUrls must not be empty"
            )

        _, _, ctrl, _ = self._clients()
        try:
            current = ctrl.get_workload_identity(name=name)
        except Exception as e:
            if not _is_not_found(e):
                raise
            try:
                ctrl.create_workload_identity(
                    name=name,
                    allowedResourceOauth2ReturnUrls=expected_urls,
                )
            except Exception as create_error:
                # Get/Create 사이에 다른 poller가 만든 경쟁은 기존 identity를 다시
                # 관측해 reconcile해요. 그 밖의 생성 실패는 숨기지 않아요.
                if not _is_already_exists(create_error):
                    raise
                current = ctrl.get_workload_identity(name=name)
            else:
                return True

        observed_urls = list(
            current.get("allowedResourceOauth2ReturnUrls") or ()
        )
        if observed_urls != expected_urls:
            ctrl.update_workload_identity(
                name=name,
                allowedResourceOauth2ReturnUrls=expected_urls,
            )
        return False

    def _cognito_client_secret(self, pool_id: str, client_id: str) -> str:
        """secret을 describe로만 확보해요(미저장) — invoke 경로와 동일 패턴."""
        resp = self._cognito().describe_user_pool_client(
            UserPoolId=pool_id, ClientId=client_id)
        return resp["UserPoolClient"]["ClientSecret"]

    def _service_quotas_client(self):
        if self._service_quotas is None:
            try:
                import boto3
            except ImportError as e:  # pragma: no cover
                raise RuntimeError(_PREVIEW_MSG) from e
            self._service_quotas = boto3.client(
                "service-quotas",
                region_name=self.region,
            )
        return self._service_quotas

    def _oauth_provider_capacity_warning(self) -> tuple[str, ...]:
        """Compare paginated provider inventory with this account's live quota."""
        ctrl = self._control()
        count = 0
        next_token = None
        while True:
            request = {"maxResults": 20}
            if next_token:
                request["nextToken"] = next_token
            response = ctrl.list_oauth2_credential_providers(**request)
            count += len(response.get("credentialProviders") or ())
            next_token = response.get("nextToken")
            if not next_token:
                break

        quota = None
        next_token = None
        quotas = self._service_quotas_client()
        while True:
            request = {"ServiceCode": "bedrock-agentcore"}
            if next_token:
                request["NextToken"] = next_token
            response = quotas.list_service_quotas(**request)
            for item in response.get("Quotas") or ():
                name = str(item.get("QuotaName") or "").lower()
                if "oauth2 credential provider" in name:
                    quota = float(item["Value"])
                    break
            if quota is not None:
                break
            next_token = response.get("NextToken")
            if not next_token:
                break
        if not quota:
            raise RuntimeError(
                "AgentCore OAuth2 credential provider quota was not returned"
            )
        if (count + 1) / quota < 0.8:
            return ()
        return (
            "AgentCore Identity OAuth2 credential provider quota가 80% 이상이에요: "
            f"현재 {count}개, 계정 quota {int(quota)}개.",
        )

    def _identity_outbound_for_agent(
        self,
        sanitized_name: str,
        cognito: dict,
    ) -> tuple[dict, IdentityOutboundDeployment]:
        """Prepare OAuth env and retain create provenance for owned cleanup."""
        if not (cognito.get("mcp_assets") or ()):
            return {}, IdentityOutboundDeployment(
                identity_outbound_status="not_applicable",
            )
        provider_name = cognito.get("provider_name") or ""
        pool_id = cognito.get("oauth_pool_id") or ""
        client_id = cognito.get("oauth_client_id") or ""
        return_url = str(cognito.get("oauth_return_url") or "").strip()
        if not (provider_name and pool_id and client_id and return_url):
            message = "Identity outbound 설정이 불완전해요."
            deployment = IdentityOutboundDeployment(
                identity_outbound_status="failed",
                identity_outbound_error=message,
                oauth_provider_name=provider_name,
            )
            if cognito.get("fail_closed_on_identity_outbound", True):
                raise IdentityOutboundProvisioningError(message, deployment)
            return {}, deployment
        workload_name = f"agora-agent-{sanitized_name}"[:64]
        warnings: tuple[str, ...] = ()
        try:
            warnings = self._oauth_provider_capacity_warning()
            for warning in warnings:
                _log.warning(warning)
        except Exception as exc:
            _log.warning(
                "AgentCore Identity OAuth2 provider quota를 관측할 수 없어요: %s",
                exc,
            )
        provider_created = False
        workload_created = False
        try:
            secret = self._cognito_client_secret(pool_id, client_id)
            provider_created = self.ensure_oauth2_credential_provider(
                name=provider_name,
                discovery_url=_oidc_discovery_url(
                    cognito.get("oauth_discovery_url") or ""
                ),
                user_pool_id=pool_id,
                client_id=client_id, client_secret=secret)
            workload_created = self.ensure_workload_identity(
                workload_name,
                allowed_resource_oauth2_return_urls=(return_url,),
            )
        except Exception as e:
            cause = f"{type(e).__name__}: {e}"
            deployment = IdentityOutboundDeployment(
                identity_outbound_status="failed",
                identity_outbound_error=cause,
                identity_outbound_warnings=warnings,
                oauth_provider_name=provider_name,
                oauth_provider_created=provider_created,
                workload_identity_name=workload_name,
                workload_identity_created=workload_created,
            )
            message = f"Identity outbound 배선 실패({provider_name}): {cause}"
            if cognito.get("fail_closed_on_identity_outbound", True):
                raise IdentityOutboundProvisioningError(
                    message,
                    deployment,
                ) from e
            _log.warning(
                "%s — 레거시 정책으로 MCP 도구 없이 배포해요.",
                message,
            )
            return {}, deployment
        try:
            # Probe once per BUILDING attempt. Role-propagation retries can
            # repeat this block (up to ~15 times at the current poll/deadline),
            # but a Runtime that reaches MCP initialization without a token is
            # permanently connection-failed, so probe before the Runtime write.
            self._probe_identity_outbound_token(
                workload_name=workload_name,
                provider_name=provider_name,
                scope=str(cognito.get("oauth_scope") or ""),
            )
        except Exception as e:
            cause = f"{type(e).__name__}: {e}"
            status = (
                "unknown"
                if _identity_probe_is_unobservable(e)
                else "failed"
            )
            deployment = IdentityOutboundDeployment(
                identity_outbound_status=status,
                identity_outbound_error=cause,
                identity_outbound_warnings=warnings,
                oauth_provider_name=provider_name,
                oauth_provider_created=provider_created,
                workload_identity_name=workload_name,
                workload_identity_created=workload_created,
            )
            message = (
                f"Identity outbound 토큰 preflight 실패"
                f"({provider_name}, status={status}): {cause}"
            )
            if (
                status == "failed"
                or cognito.get("fail_closed_on_identity_outbound", True)
            ):
                raise IdentityOutboundProvisioningError(
                    message,
                    deployment,
                ) from e
            _log.warning(
                "%s — 레거시 정책으로 MCP 도구 없이 배포해요.",
                message,
            )
            return {}, deployment
        env = {
            "AGORA_OAUTH_PROVIDER_NAME": provider_name,
            "AGORA_OAUTH_SCOPE": cognito.get("oauth_scope") or "",
            "AGORA_WORKLOAD_IDENTITY_NAME": workload_name,
        }
        return env, IdentityOutboundDeployment(
            identity_outbound_status="ready",
            identity_outbound_warnings=warnings,
            oauth_provider_name=provider_name,
            oauth_provider_created=provider_created,
            workload_identity_name=workload_name,
            workload_identity_created=workload_created,
        )

    @staticmethod
    def _workload_key_for_agent(
        sanitized_name: str,
        cognito: dict,
        existing_env: dict | None = None,
    ) -> _RuntimeWorkloadKey | None:
        """authorization이 켜진 Runtime의 기존 key를 재사용하고 없으면 생성해요."""
        authorization_url = str(cognito.get("authorization_url") or "").strip()
        if not authorization_url:
            return None
        existing_env = existing_env or {}
        workload_id = (
            str(existing_env.get("AGORA_WORKLOAD_ID") or "").strip()
            or f"agora-agent-{sanitized_name}"[:100]
        )
        private_key = str(
            existing_env.get("AGORA_WORKLOAD_PRIVATE_KEY") or ""
        ).strip()
        try:
            return _runtime_workload_key(workload_id, private_key or None)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("기존 Runtime workload private key가 손상됐어요.") from exc

    def _runtime_environment(self, runtime_id: str) -> dict:
        _, _, ctrl, _ = self._clients()
        response = ctrl.get_agent_runtime(agentRuntimeId=runtime_id)
        value = response.get("environmentVariables")
        return value if isinstance(value, dict) else {}

    def create_agent_runtime(
        self, name: str, artifact: BuildArtifact, *,
        exec_role_arn: str, cognito: dict
    ) -> RuntimeDeployment:
        """AgentCore Runtime과 Runtime별 workload 공개 신원을 생성해요.

        멱등: poll 겹침(프론트+백그라운드 폴러)으로 create가 두 번 불려도, ConflictException이
        나면 기존 runtime을 조회해 재사용해요(get-or-create). 경쟁조건에 고아 runtime을 안 남겨요.

        cognito dict 키: discoveryUrl·allowed_clients는 inbound JWT authorizer용(필수),
        client_id는 workload 기계 client, provider_name·pool_id·scope·oauth_return_url은
        outbound OAuth env 주입용(Identity P2, 선택).
        """
        _, _, ctrl, _ = self._clients()
        sanitized = _sanitize_runtime_name(name)
        workload_key = self._workload_key_for_agent(sanitized, cognito)
        identity_env, identity = self._identity_outbound_for_agent(
            sanitized,
            cognito,
        )
        try:
            resp = ctrl.create_agent_runtime(
                agentRuntimeName=sanitized,
                **self._agent_runtime_payload(
                    sanitized,
                    artifact,
                    exec_role_arn,
                    cognito,
                    workload_key=workload_key,
                    identity_env=identity_env,
                ),
            )
            return RuntimeDeployment(
                runtime_id=resp["agentRuntimeId"],
                runtime_arn=resp["agentRuntimeArn"],
                workload_id=workload_key.workload_id if workload_key else "",
                workload_public_key=workload_key.public_key if workload_key else "",
                created=True,
                **self._identity_deployment_kwargs(identity),
            )
        except Exception as e:
            if not _is_conflict(e):
                if (
                    identity.oauth_provider_created
                    or identity.workload_identity_created
                ):
                    raise IdentityOutboundProvisioningError(
                        f"{type(e).__name__}: {e}",
                        identity,
                    ) from e
                raise
            existing = self._find_runtime_by_name(sanitized)
            if existing is None:
                raise
            runtime_id, runtime_arn = existing
            # 동시 poll 중 승자가 실제 Runtime에 넣은 key를 읽어요. 패자의 새 key를
            # descriptor에 쓰면 Runtime과 공개키가 달라져 모든 호출이 거부돼요.
            existing_env = self._runtime_environment(runtime_id)
            if (
                str(cognito.get("authorization_url") or "").strip()
                and not existing_env.get("AGORA_WORKLOAD_PRIVATE_KEY")
            ):
                raise RuntimeError(
                    "기존 Runtime의 workload key를 확인할 수 없어요."
                ) from e
            workload_key = self._workload_key_for_agent(
                sanitized,
                cognito,
                existing_env,
            )
            return RuntimeDeployment(
                runtime_id=runtime_id,
                runtime_arn=runtime_arn,
                workload_id=workload_key.workload_id if workload_key else "",
                workload_public_key=workload_key.public_key if workload_key else "",
                created=False,
                **self._identity_deployment_kwargs(identity),
            )

    @staticmethod
    def _identity_deployment_kwargs(
        deployment: IdentityOutboundDeployment,
    ) -> dict:
        return {
            "identity_outbound_status": deployment.identity_outbound_status,
            "identity_outbound_error": deployment.identity_outbound_error,
            "identity_outbound_warnings": deployment.identity_outbound_warnings,
            "oauth_provider_name": deployment.oauth_provider_name,
            "oauth_provider_created": deployment.oauth_provider_created,
            "workload_identity_name": deployment.workload_identity_name,
            "workload_identity_created": deployment.workload_identity_created,
        }

    def _agent_runtime_payload(
        self,
        sanitized: str,
        artifact: BuildArtifact,
        exec_role_arn: str,
        cognito: dict,
        *,
        workload_key: _RuntimeWorkloadKey | None = None,
        identity_env: dict | None = None,
    ) -> dict:
        """create/update가 공유하는 Runtime 설정 payload.

        UpdateAgentRuntime도 같은 필드를 받아요(agentRuntimeName만 빼고) — 두 경로가
        갈라지면 재배포 때 env·authorizer가 빠지는 식으로 조용히 어긋나요.
        """
        env_vars = dict(identity_env or {})
        # 생성 코드가 "여기는 배포 런타임" 을 단정할 수 있는 **유일한 신호**예요. 조건 없이
        # 항상 넣어요 — 여기 아래 값들은 전부 조건부라서, MCP 도구가 없는 agent 는
        # environmentVariables 자체가 생략돼요(`if env_vars` 가드). 그때 파생 신호
        # (AGORA_OAUTH_PROVIDER_NAME 등)로 판별하면 배포 런타임을 로컬로 오인하고,
        # 소스에 섞여 들어온 `.env` 의 dev 크리덴셜 폴백이 되살아나요 — 그러면 호출 신원이
        # ZIP 을 내려받은 사람으로 바뀌어요.
        env_vars["AGORA_RUNTIME_ENV"] = "deployed"
        mcp_assets = cognito.get("mcp_assets") or ()
        if mcp_assets:
            runtime_assets = [
                binding
                for asset in mcp_assets
                for binding in _runtime_mcp_assets(asset)
            ]
            env_vars["AGORA_MCP_ASSETS"] = json.dumps(
                runtime_assets,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        memory_id = str(cognito.get("memory_id") or "").strip()
        if memory_id:
            env_vars["AGORA_MEMORY_ID"] = memory_id
        builtin_tool_ids = cognito.get("builtin_tool_ids") or {}
        if builtin_tool_ids.get("browser"):
            env_vars["AGORA_BROWSER_ID"] = str(
                builtin_tool_ids["browser"]
            )
        if builtin_tool_ids.get("code_interpreter"):
            env_vars["AGORA_CODE_INTERPRETER_ID"] = str(
                builtin_tool_ids["code_interpreter"]
            )
        authorization_url = str(cognito.get("authorization_url") or "").strip()
        if authorization_url and workload_key:
            env_vars.update(
                {
                    "AGORA_AUTHORIZATION_URL": authorization_url,
                    "AGORA_AUTHORIZATION_REGION": self.region,
                    "AGORA_WORKLOAD_ID": workload_key.workload_id,
                    "AGORA_WORKLOAD_PRIVATE_KEY": workload_key.private_key,
                }
            )
        configured_clients = cognito.get("allowed_clients")
        if configured_clients is None:
            configured_clients = (cognito["client_id"],)
        elif isinstance(configured_clients, str):
            configured_clients = (configured_clients,)
        allowed_clients = [
            str(client_id).strip() for client_id in configured_clients
        ]
        if not allowed_clients or any(not client_id for client_id in allowed_clients):
            raise ValueError(
                "Runtime JWT authorizer allowed_clients must contain "
                "non-empty client IDs."
            )
        return {
            "agentRuntimeArtifact": {
                "codeConfiguration": {
                    "code": {"s3": {"bucket": self.artifact_bucket, "prefix": artifact.uri}},
                    # 빌드 이미지(AL2023 py3.12)와 정합해야 cp312 네이티브 wheel(pydantic_core)이
                    # import 돼요. 3.13이면 ModuleNotFoundError(실측 2026-07-25).
                    "runtime": "PYTHON_3_12",
                    "entryPoint": (
                        ["opentelemetry-instrument", "main.py"]
                        if artifact.otel_instrumented
                        else ["main.py"]
                    ),
                }
            },
            "roleArn": exec_role_arn,
            "networkConfiguration": {"networkMode": "PUBLIC"},
            "protocolConfiguration": {"serverProtocol": "A2A"},
            "authorizerConfiguration": {
                "customJWTAuthorizer": {
                    "discoveryUrl": cognito["discoveryUrl"],
                    "allowedClients": allowed_clients,
                }
            },
            "requestHeaderConfiguration": {
                "requestHeaderAllowlist": ["X-Agora-User-Token"],
            },
            # env가 없으면 파라미터 자체를 생략해요(기존 동작 보존).
            **({"environmentVariables": env_vars} if env_vars else {}),
        }

    def update_agent_runtime(
        self, runtime_id: str, artifact: BuildArtifact, *,
        exec_role_arn: str, cognito: dict, name: str = ""
    ) -> RuntimeDeployment:
        """기존 Runtime을 새 artifact로 갱신하고 workload key를 유지해요.

        이름·ARN·endpoint가 그대로 유지돼서 카탈로그 레코드와 Playground 이력이 살아요.
        AgentCore가 내부적으로 agentRuntimeVersion을 올려요.

        name은 env 주입용 workload identity 이름을 만들 때만 써요(create와 같은 규칙).
        비우면 runtime_id에서 유추해요 — 실사용은 항상 넘겨줘요.
        """
        _, _, ctrl, _ = self._clients()
        sanitized = _sanitize_runtime_name(name) if name else runtime_id.rsplit("-", 1)[0]
        # 직전 갱신이 끝나기 전에 또 부르면 ConflictException("...while it's UPDATING")이
        # 나요(실측 2026-07-27). 연속 재배포에서 실제로 밟히니 READY가 될 때까지 기다려요.
        self._await_runtime_ready(runtime_id)
        workload_key = self._workload_key_for_agent(
            sanitized,
            cognito,
            self._runtime_environment(runtime_id),
        )
        identity_env, identity = self._identity_outbound_for_agent(
            sanitized,
            cognito,
        )
        payload = self._agent_runtime_payload(
            sanitized,
            artifact,
            exec_role_arn,
            cognito,
            workload_key=workload_key,
            identity_env=identity_env,
        )
        try:
            resp = ctrl.update_agent_runtime(agentRuntimeId=runtime_id, **payload)
        except Exception as exc:
            if (
                identity.oauth_provider_created
                or identity.workload_identity_created
            ):
                raise IdentityOutboundProvisioningError(
                    f"{type(exc).__name__}: {exc}",
                    identity,
                ) from exc
            raise
        return RuntimeDeployment(
            runtime_id=resp.get("agentRuntimeId", runtime_id),
            runtime_arn=resp["agentRuntimeArn"],
            workload_id=workload_key.workload_id if workload_key else "",
            workload_public_key=workload_key.public_key if workload_key else "",
            created=False,
            **self._identity_deployment_kwargs(identity),
        )

    def _await_runtime_ready(self, runtime_id: str, *, attempts: int = 20) -> None:
        """runtime이 전이 중이면 끝날 때까지 기다려요(최대 ~60초). best-effort.

        조회 실패는 무시하고 넘어가요 — 최종 방어선은 UpdateAgentRuntime 자체예요.
        get_runtime_status가 CREATING/UPDATING을 같은 state로 매핑해서 둘 다 대기해요.
        """
        for _ in range(attempts):
            try:
                if self.get_runtime_status(runtime_id).state != "CREATING":
                    return
            except Exception:
                return
            self._sleep(3)

    @staticmethod
    def _sleep(seconds: float) -> None:
        """테스트가 대체할 수 있게 분리해요(대기 때문에 테스트가 느려지지 않도록)."""
        import time
        time.sleep(seconds)

    def _find_runtime_by_name(self, sanitized_name: str) -> "tuple[str, str] | None":
        """sanitize된 이름으로 기존 runtime의 (id, arn)을 조회해요. 없으면 None."""
        _, _, ctrl, _ = self._clients()
        try:
            next_token = None
            while True:
                kwargs = {"maxResults": 100}
                if next_token:
                    kwargs["nextToken"] = next_token
                resp = ctrl.list_agent_runtimes(**kwargs)
                for rt in resp.get("agentRuntimes", []):
                    if rt.get("agentRuntimeName") == sanitized_name:
                        rid = rt.get("agentRuntimeId")
                        arn = rt.get("agentRuntimeArn")
                        if rid and arn:
                            return rid, arn
                next_token = resp.get("nextToken")
                if not next_token:
                    return None
        except Exception:
            return None

    def get_runtime_status(self, runtime_id: str) -> RuntimeStatus:
        """AgentCore Runtime 상태를 조회해요. runtime_id(arn 아님)로 호출."""
        _, _, ctrl, _ = self._clients()
        resp = ctrl.get_agent_runtime(agentRuntimeId=runtime_id)
        st = resp.get("status", "")
        reason = resp.get("failureReason", "")
        if st in ("CREATING", "UPDATING"):
            return RuntimeStatus(state="CREATING")
        if st == "READY":
            return RuntimeStatus(state="READY")
        if st in ("CREATE_FAILED", "UPDATE_FAILED"):
            return RuntimeStatus(state="CREATE_FAILED", reason=reason)
        # DELETING 등 나머지는 그대로 통과
        return RuntimeStatus(state=st, reason=reason)

    def _runtime_absent(self, runtime_id: str) -> bool:
        """`list_agent_runtimes` 로 이 runtime 이 정말 없는지 **독립 관측**해요.

        delete 호출이 낸 오류 코드는 부재의 근거가 아니에요 — 실측(2026-08-29)에서
        이미 삭제된 `weather_assistant-1eGR4h4b5H` 에 `DeleteAgentRuntime` 을 부르니
        `ResourceNotFoundException` 이 아니라 **`AccessDeniedException`** 이 왔어요.
        존재를 숨기려고 그렇게 돌려주는 서비스가 있어서, 오류 코드로 부재를 단정하면
        권한 문제와 구분이 안 돼요(ADR-0037 §4 — 기대값의 출처가 대상 자신이면 검사가
        아니에요).

        조회 자체가 실패하면 **부재를 주장하지 않아요**(`False`). 관측 못 한 것을 통과로
        접으면 살아 있는 runtime 을 지웠다고 보고해요.
        """
        _, _, ctrl, _ = self._clients()
        try:
            next_token = None
            while True:
                kwargs = {"maxResults": 100}
                if next_token:
                    kwargs["nextToken"] = next_token
                resp = ctrl.list_agent_runtimes(**kwargs)
                for rt in resp.get("agentRuntimes", []):
                    if rt.get("agentRuntimeId") == runtime_id:
                        return False
                next_token = resp.get("nextToken")
                if not next_token:
                    return True
        except Exception:
            return False

    def delete_runtime(self, runtime_id: str) -> None:
        """AgentCore Runtime 삭제를 요청해요. 이미 없으면 멱등 성공이에요.

        not-found 가 아닌 실패는 목록 조회로 부재를 확인한 뒤에만 성공으로 봐요.
        확인하지 못하면 원래 예외를 그대로 올려요.
        """
        _, _, ctrl, _ = self._clients()
        try:
            ctrl.delete_agent_runtime(agentRuntimeId=runtime_id)
        except Exception as exc:
            if _is_not_found(exc):
                return
            if self._runtime_absent(runtime_id):
                return
            raise

    def delete_oauth2_credential_provider(self, name: str) -> None:
        """이미 없으면 성공으로 봐요 — delete 의 계약은 '없는 상태로 만들기' 예요.

        같은 파일의 `delete_agent_execution_role` 이 이미 이 규약을 쓰고 있었는데
        identity 리소스 두 개만 빠져 있었어요. 부분 정리된 자원이 남은 자산을 purge 하면
        `ResourceNotFoundException` 이 teardown 을 통째로 실패시켜 **아무것도 지워지지
        않아요**(2026-08-29 실측: `agora-agent-weather_assistant` 가 이미 없어서
        `PurgeService` 가 `deleted: []` 로 멈췄어요). not-found 만 통과시키고
        AccessDenied·Throttling·Conflict 는 그대로 올려요.
        """
        try:
            self._control().delete_oauth2_credential_provider(name=name)
        except Exception as exc:
            if not _is_not_found(exc):
                raise

    def delete_workload_identity(self, name: str) -> None:
        """이미 없으면 성공으로 봐요. 근거는 위 `delete_oauth2_credential_provider` 와 같아요."""
        try:
            self._control().delete_workload_identity(name=name)
        except Exception as exc:
            if not _is_not_found(exc):
                raise

    def runtime_name_exists(self, name: str) -> bool:
        """같은 이름(sanitize 기준)의 AgentCore Runtime이 이미 있는지 조회해요.

        CreateAgentRuntime은 이름이 유일해야 해서, 배포 시작 전에 list로 확인해
        ConflictException(빌드 이후 실패)을 예방해요. 조회 실패 시엔 보수적으로
        False를 반환해 배포를 막지 않아요(최종 방어선은 CreateAgentRuntime).
        """
        target = _sanitize_runtime_name(name)
        _, _, ctrl, _ = self._clients()
        try:
            paginator_next = None
            while True:
                kwargs = {"maxResults": 100}
                if paginator_next:
                    kwargs["nextToken"] = paginator_next
                resp = ctrl.list_agent_runtimes(**kwargs)
                for rt in resp.get("agentRuntimes", []):
                    if rt.get("agentRuntimeName") == target:
                        return True
                paginator_next = resp.get("nextToken")
                if not paginator_next:
                    return False
        except Exception:
            return False

    def ensure_agent_memory(
        self, name: str, config: dict, *, client_token_seed: str,
        agent_key: str = "", stage: str = "",
    ) -> MemoryDeployment:
        """Get or create the agent-owned Memory with stable identifiers."""
        require_supported_memory_strategies(config.get("strategies"))
        _, _, ctrl, _ = self._clients()
        memory_name = f"{_sanitize_runtime_name(name)}_memory"[:48]
        existing = self._find_memory_by_name(memory_name)
        if existing:
            self._validate_memory_config(existing, config)
            if agent_key:
                self._ensure_agent_memory_owner(
                    existing, record_id=agent_key, stage=stage
                )
            return MemoryDeployment(existing, created=False)
        strategies = _memory_strategy_projection(memory_name, config)
        token = hashlib.sha256(
            f"agora-memory:{client_token_seed}:{memory_name}".encode()
        ).hexdigest()
        create_args = {
            "name": memory_name,
            "description": f"Agora managed memory for {name}"[:200],
            "eventExpiryDuration": int(config["retention_days"]),
            "memoryStrategies": strategies,
            "clientToken": token,
            **({
                "tags": {
                    "agora:record-id": agent_key,
                    "agora:stage": stage,
                },
            } if agent_key else {}),
        }
        first_error: Exception | None = None
        for attempt in range(2):
            try:
                response = ctrl.create_memory(**create_args)
                return MemoryDeployment(response["memory"]["id"], created=True)
            except Exception as exc:
                existing = self._find_memory_by_name(memory_name)
                if existing:
                    self._validate_memory_config(existing, config)
                    if agent_key:
                        self._ensure_agent_memory_owner(
                            existing, record_id=agent_key, stage=stage
                        )
                    # The pre-create lookup was empty, so this is the resource
                    # created by this idempotent request or its concurrent replay.
                    return MemoryDeployment(existing, created=True)
                if _is_conflict(exc):
                    raise
                if attempt == 0:
                    first_error = exc
                    continue
                raise first_error from exc

    def _ensure_agent_memory_owner(
        self, memory_id: str, *, record_id: str, stage: str,
    ) -> None:
        _, _, ctrl, _ = self._clients()
        memory = ctrl.get_memory(memoryId=memory_id).get("memory", {})
        arn = str(memory.get("arn") or memory.get("memoryArn") or "")
        if not arn:
            raise RuntimeError(
                "AgentCore Memory ARN is unavailable for ownership validation"
            )
        tags = ctrl.list_tags_for_resource(
            resourceArn=arn
        ).get("tags") or {}
        actual_owner = str(tags.get("agora:record-id") or "")
        if actual_owner == record_id:
            return
        if actual_owner:
            raise RuntimeError(
                "AgentCore Memory ownership conflict: "
                f"{actual_owner!r} does not match {record_id!r}"
            )
        ctrl.tag_resource(
            resourceArn=arn,
            tags={
                "agora:record-id": record_id,
                "agora:stage": stage,
            },
        )

    def tag_agent_memory(
        self, memory_id: str, *, record_id: str, stage: str,
    ) -> None:
        _, _, ctrl, _ = self._clients()
        memory = ctrl.get_memory(memoryId=memory_id).get("memory", {})
        arn = str(memory.get("arn") or memory.get("memoryArn") or "")
        if not arn:
            raise RuntimeError("AgentCore Memory ARN is unavailable for tagging")
        ctrl.tag_resource(
            resourceArn=arn,
            tags={
                "agora:record-id": record_id,
                "agora:stage": stage,
            },
        )

    def _validate_memory_config(self, memory_id: str, expected: dict) -> None:
        _, _, ctrl, _ = self._clients()
        memory = ctrl.get_memory(memoryId=memory_id).get("memory", {})
        actual_retention = memory.get("eventExpiryDuration")
        expected_retention = int(expected["retention_days"])
        if actual_retention != expected_retention:
            raise ValueError(
                "existing AgentCore Memory retention does not match "
                f"(expected={expected_retention}, actual={actual_retention})"
            )
        actual_strategies = sorted(
            str(strategy.get("type") or "")
            for strategy in memory.get("strategies") or ()
        )
        expected_strategies = sorted(expected.get("strategies") or ())
        if actual_strategies != expected_strategies:
            raise ValueError(
                "existing AgentCore Memory strategies do not match "
                f"(expected={expected_strategies}, actual={actual_strategies})"
            )
        if "strategy_configs" in expected:
            expected_state = _expected_memory_strategy_state(
                str(memory.get("name") or ""),
                expected,
            )
            actual_state = _actual_memory_strategy_state(memory)
            if actual_state != expected_state:
                raise ValueError(
                    "existing AgentCore Memory strategy configuration "
                    "does not match "
                    f"(expected={expected_state}, actual={actual_state})"
                )

    def _find_memory_by_name(self, name: str) -> str | None:
        _, _, ctrl, _ = self._clients()
        token = None
        while True:
            kwargs = {"maxResults": 100}
            if token:
                kwargs["nextToken"] = token
            response = ctrl.list_memories(**kwargs)
            summaries = response.get(
                "memories",
                response.get("memorySummaries", []),
            )
            for summary in summaries:
                memory_id = summary.get("id")
                if not memory_id:
                    continue
                memory = ctrl.get_memory(memoryId=memory_id).get("memory", {})
                if memory.get("name") == name:
                    return str(memory_id)
            token = response.get("nextToken")
            if not token:
                return None

    def get_memory_status(self, memory_id: str) -> MemoryStatus:
        _, _, ctrl, _ = self._clients()
        memory = ctrl.get_memory(memoryId=memory_id).get("memory", {})
        return MemoryStatus(
            state=str(memory.get("status") or ""),
            reason=str(memory.get("failureReason") or ""),
        )

    def delete_memory(self, memory_id: str) -> None:
        """Delete an Agora-owned Memory from an administrative cleanup path."""
        _, _, ctrl, _ = self._clients()
        try:
            ctrl.delete_memory(memoryId=memory_id)
        except Exception as exc:
            if not _is_not_found(exc):
                raise

    def ensure_builtin_tool(
        self,
        kind: str,
        name: str,
        *,
        agent_key: str,
        stage: str,
        execution_role_arn: str,
        recording_bucket: str,
    ) -> BuiltinToolDeployment:
        """Create or recover one deterministic CUSTOM built-in tool."""
        if not execution_role_arn:
            raise ValueError("Builtin tool execution role is not configured")
        _, _, ctrl, _ = self._clients()
        network_mode = _BUILTIN_TOOL_NETWORK_MODES[kind]
        suffix = "browser" if kind == "browser" else "code_interpreter"
        digest = hashlib.sha256(f"{agent_key}:{kind}".encode()).hexdigest()[:10]
        resource_name = (
            f"{_sanitize_runtime_name(name)[:32]}_{suffix[:5]}_{digest}"
        )[:48]
        existing = self._find_builtin_tool(kind, resource_name)
        if existing:
            return BuiltinToolDeployment(
                existing["id"],
                existing["arn"],
                existing["status"],
                False,
                network_mode,
            )
        token = hashlib.sha256(
            f"agora-builtin:{agent_key}:{kind}".encode()
        ).hexdigest()
        args = {
            "name": resource_name,
            "description": f"Agora managed {kind} for {name}"[:200],
            "executionRoleArn": execution_role_arn,
            "networkConfiguration": {"networkMode": network_mode},
            "clientToken": token,
            "tags": {
                "agora:record-id": agent_key,
                "agora:stage": stage,
                "agora:network-mode": network_mode,
            },
        }
        if kind == "browser":
            if not recording_bucket:
                raise ValueError("Browser recording bucket is not configured")
            args["recording"] = {
                "enabled": True,
                "s3Location": {
                    "bucket": recording_bucket,
                    "prefix": f"{agent_key}/browser/",
                },
            }
            create = ctrl.create_browser
            id_key, arn_key = "browserId", "browserArn"
        else:
            create = ctrl.create_code_interpreter
            id_key, arn_key = "codeInterpreterId", "codeInterpreterArn"
        try:
            response = create(**args)
            return BuiltinToolDeployment(
                str(response[id_key]),
                str(response[arn_key]),
                str(response.get("status") or "CREATING"),
                True,
                network_mode,
            )
        except Exception as exc:
            existing = self._find_builtin_tool(kind, resource_name)
            if existing:
                return BuiltinToolDeployment(
                    existing["id"],
                    existing["arn"],
                    existing["status"],
                    True,
                    network_mode,
                )
            code = (
                getattr(exc, "response", {})
                .get("Error", {})
                .get("Code", "")
            )
            if code in {
                "ServiceQuotaExceededException",
                "LimitExceededException",
            }:
                raise RuntimeError(
                    f"{kind} CUSTOM resource quota is exhausted"
                ) from exc
            raise

    @staticmethod
    def _builtin_log_source_name(
        kind: str, resource_id: str, log_type: str
    ) -> str:
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "-", resource_id)[:20]
        kind_key = "browser" if kind == "browser" else "ci"
        suffix = {
            "APPLICATION_LOGS": "application",
            "USAGE_LOGS": "usage",
            "TRACES": "traces",
        }[log_type]
        digest = hashlib.sha256(
            f"{kind}:{resource_id}:{log_type}".encode()
        ).hexdigest()[:8]
        return f"agora-{kind_key}-{safe_id}-{suffix}-{digest}"

    @staticmethod
    def _all_deliveries(logs) -> list[dict]:
        deliveries = []
        token = None
        while True:
            kwargs = {"limit": 50}
            if token:
                kwargs["nextToken"] = token
            response = logs.describe_deliveries(**kwargs)
            deliveries.extend(response.get("deliveries") or ())
            token = response.get("nextToken")
            if not token:
                return deliveries

    def _ensure_builtin_log_deliveries(
        self,
        kind: str,
        resource_id: str,
        resource_arn: str,
        stage: str,
    ) -> None:
        logs = self._logs_client()
        log_group_name = (
            f"/aws/vendedlogs/bedrock-agentcore/agora-builtin-{stage}"
        )
        try:
            logs.create_log_group(logGroupName=log_group_name)
        except Exception as exc:
            if not _is_already_exists(exc):
                raise
        groups = logs.describe_log_groups(
            logGroupNamePrefix=log_group_name,
            limit=1,
        ).get("logGroups") or ()
        group = next(
            (
                item
                for item in groups
                if item.get("logGroupName") == log_group_name
            ),
            None,
        )
        if not group or not (group.get("logGroupArn") or group.get("arn")):
            raise RuntimeError("Builtin vended log group ARN is unavailable")
        log_group_arn = str(
            group.get("logGroupArn") or group.get("arn")
        ).removesuffix(":*")
        cwl_destination = _put_tagged_once(
            logs.put_delivery_destination,
            {"agora:stage": stage},
            name=f"agora-builtin-{stage}",
            deliveryDestinationConfiguration={
                "destinationResourceArn": log_group_arn,
            },
            outputFormat="json",
        ).get("deliveryDestination") or {}
        cwl_destination_arn = str(cwl_destination.get("arn") or "")
        if not cwl_destination_arn:
            raise RuntimeError("Builtin vended log destination ARN is unavailable")
        trace_destination = _put_tagged_once(
            logs.put_delivery_destination,
            {"agora:stage": stage},
            name=f"agora-builtin-traces-{stage}",
            deliveryDestinationType="XRAY",
        ).get("deliveryDestination") or {}
        trace_destination_arn = str(trace_destination.get("arn") or "")
        if not trace_destination_arn:
            raise RuntimeError(
                "Builtin trace delivery destination ARN is unavailable"
            )

        deliveries = self._all_deliveries(logs)
        for log_type in _BUILTIN_TOOL_LOG_TYPES[kind]:
            destination_arn = (
                trace_destination_arn
                if log_type == "TRACES"
                else cwl_destination_arn
            )
            source_name = self._builtin_log_source_name(
                kind, resource_id, log_type
            )
            _put_tagged_once(
                logs.put_delivery_source,
                {"agora:stage": stage},
                name=source_name,
                resourceArn=resource_arn,
                logType=log_type,
            )
            if any(
                delivery.get("deliverySourceName") == source_name
                and delivery.get("deliveryDestinationArn") == destination_arn
                for delivery in deliveries
            ):
                continue
            try:
                created = logs.create_delivery(
                    deliverySourceName=source_name,
                    deliveryDestinationArn=destination_arn,
                    tags={"agora:stage": stage},
                ).get("delivery") or {}
                deliveries.append(created)
            except Exception as exc:
                recovered = self._all_deliveries(logs)
                if not any(
                    delivery.get("deliverySourceName") == source_name
                    and delivery.get("deliveryDestinationArn")
                    == destination_arn
                    for delivery in recovered
                ):
                    raise exc

    def _delete_builtin_log_deliveries(
        self, kind: str, resource_id: str
    ) -> None:
        logs = self._logs_client()
        source_names = {
            self._builtin_log_source_name(kind, resource_id, log_type)
            for log_type in _BUILTIN_TOOL_LOG_TYPES[kind]
        }
        for delivery in self._all_deliveries(logs):
            if delivery.get("deliverySourceName") not in source_names:
                continue
            delivery_id = str(delivery.get("id") or "")
            if delivery_id:
                logs.delete_delivery(id=delivery_id)
        for source_name in source_names:
            try:
                logs.delete_delivery_source(name=source_name)
            except Exception as exc:
                if not _is_not_found(exc):
                    raise

    def ensure_builtin_observability(
        self,
        kind: str,
        resource_id: str,
        resource_arn: str,
        *,
        stage: str,
    ) -> None:
        """Wire vended logs after resource ownership is durably checkpointed."""
        self._ensure_builtin_log_deliveries(
            kind,
            resource_id,
            resource_arn,
            stage,
        )

    def _find_builtin_tool(self, kind: str, name: str) -> dict | None:
        _, _, ctrl, _ = self._clients()
        list_call = (
            ctrl.list_browsers
            if kind == "browser"
            else ctrl.list_code_interpreters
        )
        summaries_key = (
            "browserSummaries"
            if kind == "browser"
            else "codeInterpreterSummaries"
        )
        id_key = "browserId" if kind == "browser" else "codeInterpreterId"
        arn_key = "browserArn" if kind == "browser" else "codeInterpreterArn"
        token = None
        while True:
            kwargs = {"type": "CUSTOM", "maxResults": 100}
            if token:
                kwargs["nextToken"] = token
            response = list_call(**kwargs)
            for summary in response.get(summaries_key, ()):
                if summary.get("name") == name:
                    return {
                        "id": str(summary[id_key]),
                        "arn": str(summary[arn_key]),
                        "status": str(summary.get("status") or ""),
                    }
            token = response.get("nextToken")
            if not token:
                return None

    def get_builtin_tool_status(
        self, kind: str, resource_id: str
    ) -> BuiltinToolStatus:
        _, _, ctrl, _ = self._clients()
        response = (
            ctrl.get_browser(browserId=resource_id)
            if kind == "browser"
            else ctrl.get_code_interpreter(codeInterpreterId=resource_id)
        )
        return BuiltinToolStatus(
            state=str(response.get("status") or ""),
            reason=str(response.get("failureReason") or ""),
        )

    def tag_builtin_tool(
        self,
        resource_arn: str,
        *,
        record_id: str,
        stage: str,
        network_mode: str,
    ) -> None:
        _, _, ctrl, _ = self._clients()
        ctrl.tag_resource(
            resourceArn=resource_arn,
            tags={
                "agora:record-id": record_id,
                "agora:stage": stage,
                "agora:network-mode": network_mode,
            },
        )

    def observe_builtin_tool(self, kind: str, resource_id: str) -> dict:
        """Read control-plane state and tags without trusting the agent."""
        _, _, ctrl, _ = self._clients()
        if kind == "browser":
            response = ctrl.get_browser(browserId=resource_id)
            id_key, arn_key = "browserId", "browserArn"
        else:
            response = ctrl.get_code_interpreter(
                codeInterpreterId=resource_id
            )
            id_key, arn_key = "codeInterpreterId", "codeInterpreterArn"
        resource_arn = str(response.get(arn_key) or "")
        tags = ctrl.list_tags_for_resource(
            resourceArn=resource_arn
        ).get("tags") or {}
        return {
            "id": str(response.get(id_key) or ""),
            "arn": resource_arn,
            "status": str(response.get("status") or ""),
            "network_mode": str(
                (response.get("networkConfiguration") or {}).get(
                    "networkMode"
                )
                or ""
            ),
            "recording": response.get("recording"),
            "tags": dict(tags),
        }

    def delete_builtin_tool(self, kind: str, resource_id: str) -> None:
        _, _, ctrl, _ = self._clients()
        token = hashlib.sha256(
            f"agora-delete-builtin:{kind}:{resource_id}".encode()
        ).hexdigest()
        try:
            if kind == "browser":
                ctrl.delete_browser(browserId=resource_id, clientToken=token)
            else:
                ctrl.delete_code_interpreter(
                    codeInterpreterId=resource_id,
                    clientToken=token,
                )
        except Exception as exc:
            if not _is_not_found(exc):
                raise
        self._delete_builtin_log_deliveries(kind, resource_id)

    # ── 삭제 ──────────────────────────────────────────────────────────
    def teardown(
        self,
        lambda_arn: str,
        gateway_id: str,
        target_id: str,
    ) -> None:
        _, lam, ctrl, _ = self._clients()
        if target_id:
            try:
                ctrl.delete_gateway_target(
                    gatewayIdentifier=gateway_id,
                    targetId=target_id,
                )
            except Exception as exc:
                if _is_not_found(exc):
                    pass
                elif _is_gateway_target_deleting(exc):
                    self._confirm_gateway_target_absent(
                        ctrl,
                        gateway_id=gateway_id,
                        target_id=target_id,
                        deletion_error=exc,
                    )
                else:
                    raise
            else:
                # 정상 응답은 삭제 요청 접수일 뿐 부재 증거가 아니므로 독립 관측해요.
                self._confirm_gateway_target_absent(
                    ctrl,
                    gateway_id=gateway_id,
                    target_id=target_id,
                    deletion_error=None,
                )
        if lambda_arn:
            try:
                lam.delete_function(FunctionName=lambda_arn)
            except Exception as exc:
                if not _is_not_found(exc):
                    raise

    def purge_artifacts(self, asset_id: str, version: str) -> int:
        """빌드 아티팩트(S3 {asset_id}/{version}/ 아래)를 삭제해요. best-effort — 실패 시 0.

        ECR 이미지는 이번 범위 밖(정리 API 미구현) — S3 아티팩트만 정리해요.
        """
        if not self.artifact_bucket:
            return 0
        _, _, _, s3 = self._clients()
        prefix = f"{asset_id}/{version}/"
        deleted = 0
        try:
            token = None
            while True:
                kwargs = {"Bucket": self.artifact_bucket, "Prefix": prefix}
                if token:
                    kwargs["ContinuationToken"] = token
                resp = s3.list_objects_v2(**kwargs)
                keys = [{"Key": o["Key"]} for o in resp.get("Contents", [])]
                if keys:
                    s3.delete_objects(
                        Bucket=self.artifact_bucket, Delete={"Objects": keys})
                    deleted += len(keys)
                token = resp.get("NextContinuationToken")
                if not token:
                    break
        except Exception:
            return deleted
        return deleted
