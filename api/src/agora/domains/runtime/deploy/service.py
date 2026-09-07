"""DeployService — 게이트·규약검증·job 생성·poll·teardown을 조립하는 파사드.

catalog/mcp 라우터가 이 서비스만 호출해요. 모든 협력자는 주입 — 테스트가
FakeDeployPort·FakeJobStore·페이크 게이트로 전부 대체 가능(AWS 단일화).
"""
from __future__ import annotations

import logging
import re
import threading
import uuid
from dataclasses import replace

from ....shared.bedrock_models import is_model_metadata, model_metadata
from ....shared.governance import (
    DeploymentAuthorizationRequest,
    GovernanceDecision,
)
from ....shared.gateway_tools import (
    McpGatewayTargetError,
    gateway_tool_names,
    mcp_gateway_target_index,
)
from ....shared.permission_group import PermissionGroup, allowed_tags
from ....shared.sensitivity_movement import RedeploySensitivityGuard
from ....shared.slug import resolved_gateway_target_name
from ....shared.memory import (
    MEMORY_NAMESPACE_TEMPLATES,
    SUPPORTED_MEMORY_STRATEGIES,
    UnsupportedMemoryStrategyError,
    configured_memory_namespaces,
    require_supported_memory_strategies,
)
from ....shared.agent_blueprint import reject_disabled_builtin_tools
from ...catalog.registry.models import (
    DescriptorType,
    RecordStatus,
    endpoint_of_descriptors,
)
from ...catalog.sourcestore.bindings import get_binding
from .agent_jobs import (
    advance_agent,
    compensate_failure as compensate_agent_failure,
)
from .jobs import advance, compensate_failure as compensate_mcp_failure
from .models import (
    ACTIVE_NAME_STATUSES,
    AgentCompensationTarget,
    ConcurrentJobAdmission,
    ConcurrentJobAdvance,
    DEAD_NAME_STATUSES,
    DeployJob,
    DeployPhase,
    GateRejected,
    NameCheck,
    ProvisioningCheckpointError,
    RuntimeNameConflict,
    SourceRef,
    name_conflict_message,
    terminal,
    AgentMonitoringJobProjection,
)
from .spec_check import SpecCheckError, check_agent_spec, check_mcp_spec, decide_build_type
from .verify import ExpectedTool, NegativeControl

_log = logging.getLogger(__name__)
_MEMORY_STRATEGY_NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,47}$")
def _valid_memory_strategy_configs(
    configs: object,
    strategies: list[str],
) -> bool:
    if not isinstance(configs, dict) or set(configs) != set(strategies):
        return False
    for strategy in strategies:
        config = configs.get(strategy)
        if not isinstance(config, dict):
            return False
        fields = set(config)
        if fields != {"name", "namespaces"} and not (
            strategy == "SUMMARIZATION" and fields == {"name"}
        ):
            return False
        name = config.get("name")
        if (
            not isinstance(name, str)
            or _MEMORY_STRATEGY_NAME.fullmatch(name) is None
        ):
            return False
        if "namespaces" in config and config["namespaces"] != [
            MEMORY_NAMESPACE_TEMPLATES[strategy]
        ]:
            return False
    return True


def _valid_conversation_manager(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"name", "parameters"}:
        return False
    name = value.get("name")
    parameters = value.get("parameters")
    if name == "NullConversationManager":
        return parameters == {}
    return (
        name == "SlidingWindowConversationManager"
        and isinstance(parameters, dict)
        and set(parameters) == {"window_size"}
        and isinstance(parameters["window_size"], int)
        and not isinstance(parameters["window_size"], bool)
        and parameters["window_size"] >= 10
    )


def _validated_agent_tool_requests(
    raw_requests: object,
    dependencies: dict | None,
) -> list[dict[str, str]]:
    """Cross-check durable request metadata against the canonical declaration."""
    if raw_requests is None or raw_requests == ():
        return []
    if not isinstance(raw_requests, list):
        raise SpecCheckError("tool_requests 형식이 올바르지 않아요.")
    if not raw_requests:
        return []

    declarations = {
        str(asset.get("assetId") or "").strip(): asset
        for asset in (dependencies or {}).get("mcpAssets") or ()
        if isinstance(asset, dict)
        and str(asset.get("assetId") or "").strip()
    }
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_requests:
        if not isinstance(item, dict):
            raise SpecCheckError("tool_requests 항목 형식이 올바르지 않아요.")
        asset_id = str(item.get("asset_id") or "").strip()
        asset_version = str(item.get("asset_version") or "").strip()
        operation_id = str(item.get("operation_id") or "").strip()
        justification = str(
            item.get("request_justification") or ""
        ).strip()
        key = (asset_id, operation_id)
        if key in seen:
            raise SpecCheckError(
                "tool_requests에 같은 MCP operation이 중복됐어요: "
                f"{asset_id}/{operation_id}"
            )
        seen.add(key)

        declaration = declarations.get(asset_id)
        operations = (
            declaration.get("operations") or ()
            if declaration is not None
            else ()
        )
        if declaration is None or operation_id not in operations:
            raise SpecCheckError(
                "이 Agent가 선언하지 않은 tool_requests 항목이에요: "
                f"{asset_id}/{operation_id}"
            )
        declared_version = str(
            declaration.get("version")
            or declaration.get("assetVersion")
            or ""
        ).strip()
        if asset_version != declared_version:
            raise SpecCheckError(
                "tool_requests의 MCP version이 배포 선언과 달라요: "
                f"{asset_id} expected={declared_version} "
                f"actual={asset_version}"
            )
        if not justification:
            raise SpecCheckError(
                "tool_requests 신청 사유는 공백일 수 없어요: "
                f"{asset_id}/{operation_id}"
            )
        normalized.append({
            "asset_id": asset_id,
            "asset_version": asset_version,
            "operation_id": operation_id,
            "request_justification": justification,
        })
    return normalized


def _probe_value(schema: dict) -> tuple[object, str]:
    """Create a deterministic non-secret value for a READ operation input."""
    if "const" in schema:
        return schema["const"], ""
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0], ""
    for keyword in ("oneOf", "anyOf"):
        variants = schema.get(keyword)
        if isinstance(variants, list) and variants and isinstance(variants[0], dict):
            return _probe_value(variants[0])
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        schema_type = next(
            (item for item in schema_type if item != "null"),
            "null",
        )
    if schema_type in (None, "string"):
        if schema.get("pattern"):
            return None, "pattern 입력은 안전한 합성값을 결정할 수 없어요."
        values = {
            "date": "2000-01-01",
            "date-time": "2000-01-01T00:00:00Z",
            "email": "agora-verifier@example.invalid",
            "hostname": "verifier.example.invalid",
            "ipv4": "192.0.2.1",
            "uri": "https://example.invalid/agora-verifier",
            "uuid": "00000000-0000-4000-8000-000000000000",
        }
        value = values.get(schema.get("format"), "agora-verifier-probe")
        minimum = int(schema.get("minLength") or 0)
        maximum = schema.get("maxLength")
        if isinstance(maximum, int) and minimum > maximum:
            return None, "minLength가 maxLength보다 커요."
        if len(value) < minimum:
            value += "x" * (minimum - len(value))
        if isinstance(maximum, int) and len(value) > maximum:
            return None, "maxLength 안에 안전한 합성값을 만들 수 없어요."
        return value, ""
    if schema_type == "integer":
        value = schema.get("minimum", 0)
        if "exclusiveMinimum" in schema:
            value = max(value, schema["exclusiveMinimum"] + 1)
        value = int(value)
        if "maximum" in schema and value > schema["maximum"]:
            return None, "minimum이 maximum보다 커요."
        return value, ""
    if schema_type == "number":
        value = float(schema.get("minimum", 0.0))
        if "exclusiveMinimum" in schema:
            value = max(value, float(schema["exclusiveMinimum"]) + 1.0)
        if "maximum" in schema and value > float(schema["maximum"]):
            return None, "minimum이 maximum보다 커요."
        return value, ""
    if schema_type == "boolean":
        return False, ""
    if schema_type == "array":
        minimum = int(schema.get("minItems") or 0)
        item_schema = schema.get("items") or {}
        if not isinstance(item_schema, dict):
            return None, "array items schema가 올바르지 않아요."
        item, error = _probe_value(item_schema)
        if error:
            return None, error
        return [item for _ in range(minimum)], ""
    if schema_type == "object":
        return _probe_arguments(schema)
    return None, f"지원하지 않는 input type이에요: {schema_type}"


def _probe_arguments(schema: dict) -> tuple[dict, str]:
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    if not isinstance(properties, dict) or not isinstance(required, list):
        return {}, "input schema의 properties/required가 올바르지 않아요."
    arguments = {}
    for name in required:
        property_schema = properties.get(name)
        if not isinstance(name, str) or not isinstance(property_schema, dict):
            return {}, f"필수 입력 {name!r}의 schema가 없어요."
        value, error = _probe_value(property_schema)
        if error:
            return {}, f"{name}: {error}"
        arguments[name] = value
    return arguments, ""


def _mcp_target_name(record) -> str:
    """agoraDependencies.mcpAssets에 실을 target 이름을 정해요(결함 #10).

    Gateway가 라이브 tool 접두어(`{target}___{op}`)에 쓰는 정규화된 이름을 써야
    scaffold의 authorization·selfcheck 기준과 일치해요. 등록 시 저장된
    gatewayTargetName을 우선 쓰고, 없으면(기존 자산) slug(record.name)를 Gateway와
    같은 규칙으로 정규화해 폴백해요 — `.`·`_` 자산도 backfill 없이 정합해요.
    (비-ASCII 기존 자산은 slug가 이미 hash8로 손실돼 폴백으론 복원 불가.)
    """
    descriptors = getattr(record, "descriptors", None)
    mcp = descriptors.get("mcp") if isinstance(descriptors, dict) else None
    stored = mcp.get("gatewayTargetName") if isinstance(mcp, dict) else None
    return resolved_gateway_target_name(record.name, stored)


def _split_mcp_target_bindings(
    record,
    operations: list[str],
) -> list[dict] | None:
    """Project a split MCP descriptor into operation-scoped Agent bindings."""
    if not operations:
        raise SpecCheckError(
            "MCP Gateway Target 선택에는 비어 있지 않은 operations가 필요해요."
        )
    try:
        index = mcp_gateway_target_index(
            getattr(record, "descriptors", None)
        )
        if not index.split:
            return None
        return [
            {
                "name": target.name,
                "sensitivity": target.sensitivity,
                "operations": list(target.operations),
            }
            for target in index.select(operations)
        ]
    except McpGatewayTargetError as exc:
        raise SpecCheckError(str(exc)) from exc


def _mcp_redeploy_target_snapshot(record) -> dict:
    """Capture the Registry-owned topology before an MCP redeploy starts."""
    try:
        index = mcp_gateway_target_index(
            getattr(record, "descriptors", None)
        )
    except McpGatewayTargetError as exc:
        return {
            "mode": "unknown",
            "reason": str(exc),
        }
    if not index.split:
        return {
            "mode": "legacy",
            "gateway_target_name": index.legacy_target_name or "",
        }
    return {
        "mode": "split",
        "targets": [
            {
                "sensitivity": target.sensitivity,
                "gateway_target_name": target.name,
                "operations": list(target.operations),
            }
            for target in index.targets
        ],
    }


def _requires_mcp_name_resolution(asset_id: str) -> bool:
    """작성 시점에 알 수 없는 asset ID placeholder인지 판정해요."""
    normalized = asset_id.strip().upper()
    return not normalized or normalized.startswith(
        ("REPLACE_WITH", "PLACEHOLDER", "<")
    )


def inherit_agent_redeploy_metadata(meta: dict, record) -> dict:
    """Apply the current Registry identity and model to an agent redeploy."""
    inherited = {**meta, "name": record.name}
    inherited.pop("_existing_model", None)
    agent_node = (record.descriptors or {}).get("agent") or {}
    existing_model = agent_node.get("model")
    existing_bedrock_model_id = agent_node.get("bedrockModelId")
    if (
        "model" not in inherited
        and is_model_metadata(existing_model, existing_bedrock_model_id)
    ):
        inherited["_existing_model"] = {
            "model": existing_model,
            "bedrockModelId": existing_bedrock_model_id,
        }
    return inherited


def inherited_memory_id_for_redeploy(record, requested: dict) -> str | None:
    """Preserve an owned Memory coordinate and reject unsupported config changes."""
    agent_node = (record.descriptors or {}).get("agent") or {}
    existing = agent_node.get("memory") or {"mode": "DISABLED"}
    if not isinstance(existing, dict):
        raise SpecCheckError("기존 AgentCore Memory descriptor가 올바르지 않아요.")
    existing_mode = existing.get("mode")
    if existing_mode not in {"DISABLED", "MANAGED"}:
        raise SpecCheckError("기존 AgentCore Memory mode를 지원하지 않아요.")
    memory_id = str(existing.get("memoryId") or "").strip() or None
    if existing_mode == "MANAGED":
        if memory_id is None:
            raise SpecCheckError("기존 MANAGED Memory ID가 없어 재배포할 수 없어요.")
        if requested.get("mode") == "MANAGED":
            existing_config = {
                "strategies": existing.get("strategies"),
                "strategy_configs": existing.get("strategy_configs"),
                "retention_days": existing.get("retention_days"),
            }
            requested_config = {
                "strategies": requested.get("strategies"),
                "strategy_configs": requested.get("strategy_configs"),
                "retention_days": requested.get("retention_days"),
            }
            if existing_config != requested_config:
                raise SpecCheckError(
                    "재배포에서 기존 AgentCore Memory 설정 변경은 지원하지 않아요."
                )
    return memory_id


class DeployService:
    def __init__(self, *, port, store, gateway_id, exec_role_arn,
                 source_store, registry, registry_id, gate,
                 new_id, now, cognito=None, deploy_region="",
                 agent_exec_role_arn="", verifier=None,
                 identity_issuer=None, oauth_gateway_url="",
                 oauth_gateway_id="", owner_id="",
                 fail_closed_on_unknown_authorization=False,
                 fail_closed_on_unknown_builtin_tools=False,
                 fail_closed_on_identity_outbound=True,
                 builtin_execution_role_arn="", builtin_recording_bucket="",
                 stage="", builtin_verifier=None,
                 per_agent_roles_enabled=True,
                 agent_shared_policy_arn="",
                 agent_permissions_boundary_arn="",
                 redeploy_sensitivity_guard: RedeploySensitivityGuard | None = None,
                 provision_shared_policy=None,
                 issue_call_handle=None,
                 provision_read_access=None,
                 read_tool_approval_states=None,
                 submit_tool_requests=None,
                 record_tool_request=None):
        self.port = port
        self.store = store
        self.gateway_id = gateway_id
        # M2 OAuth short ID가 있으면 deploy-mode Lambda target도 agent outbound와
        # 같은 Gateway에 붙여요. 미지정 환경은 기존 deploy Gateway로 되돌아가요.
        self.target_gateway_id = oauth_gateway_id or gateway_id
        self.exec_role_arn = exec_role_arn
        # agent Runtime 실행롤(bedrock-agentcore trust)은 Lambda 실행롤과 trust가 달라요.
        # 미지정 시 exec_role_arn으로 폴백(하위호환) — MCP 경로는 이 값을 안 써요.
        self.agent_exec_role_arn = agent_exec_role_arn or exec_role_arn
        self.cognito = cognito or {}
        # deploy_region은 서비스 생성 시 1회만 읽어요(agent invoke URL 구성용).
        # 매 poll마다 load_config()를 재호출하지 않기 위함(MCP 경로가 region을 1회 읽는 것과 동일).
        self.deploy_region = deploy_region
        self.source_store = source_store
        self.registry = registry
        self.registry_id = registry_id
        self.gate = gate
        self.new_id = new_id
        self.now = now
        self.owner_id = owner_id or uuid.uuid4().hex
        # AgentVerifier — VERIFYING 단계에서 도구 동작을 검증해요. None이면 검증을
        # 건너뛰고 바로 등재해요(테스트·미배선 환경 하위호환).
        self.verifier = verifier
        self.builtin_verifier = builtin_verifier
        # AgentIdentityIssuer — 배포 전 OAuth client를 만들고 완료 시 binding을 기록해요.
        # duck-typed 협력자라 runtime 도메인이 identity 도메인을 직접 import하지 않아요.
        # None이면 발급을 건너뛰어요(테스트·미배선 하위호환).
        self.identity_issuer = identity_issuer
        self.oauth_gateway_url = oauth_gateway_url.rstrip("/")
        self.fail_closed_on_unknown_authorization = (
            fail_closed_on_unknown_authorization
        )
        self.fail_closed_on_unknown_builtin_tools = (
            fail_closed_on_unknown_builtin_tools
        )
        self.fail_closed_on_identity_outbound = fail_closed_on_identity_outbound
        self.builtin_config = {
            "execution_role_arn": builtin_execution_role_arn,
            "recording_bucket": builtin_recording_bucket,
            "stage": stage,
        }
        self.agent_role_config = {
            "enabled": per_agent_roles_enabled,
            "shared_policy_arn": agent_shared_policy_arn,
            "permissions_boundary_arn": agent_permissions_boundary_arn,
            "stage": stage,
        }
        self.redeploy_sensitivity_guard = redeploy_sensitivity_guard
        # IA-71: Gateway 공유 Cedar 정책 provisioning 훅. `None` 이면 안 불러요 — 테스트와
        # 구환경 호환이에요. 프로덕션 배선은 `shared.deps` 가 넣어줘요.
        self.provision_shared_policy = provision_shared_policy
        # IA-61 후속: 검증 호출에 실을 delegation handle 발급자. `None` 이면 handle 없이
        # 호출해요(테스트·interceptor 없는 Gateway 호환).
        self.issue_call_handle = issue_call_handle
        # ADR-0094: 카탈로그 READ 기본 권한 부여 훅. `None` 이면 안 불러요.
        self.provision_read_access = provision_read_access
        # ADR-0104(IH-153): VERIFYING 이 「승인 대기 도구」를 실패가 아니라 관측된 상태로
        # 다루려면 ④ 원장을 읽어야 해요. 기대값의 소유자를 원장으로 고정하려고 **주입**받아요
        # (runtime 도메인이 identity 를 직접 import 하지 않아요 — `issue_call_handle` 과 같은
        # 패턴). `None` 이면 원장을 못 본 것이라 verify 가 옛 엄격한 기대값으로 되돌아가요.
        self.read_tool_approval_states = read_tool_approval_states
        # IH-83: 비-READ 권한 신청 제출과 요청 로그 기록은 shared.deps에서 주입해 runtime이
        # identity/catalog 구현을 직접 import하지 않게 해요.
        self.submit_tool_requests = submit_tool_requests
        self.record_tool_request = record_tool_request
        # API poll과 백그라운드 poller가 같은 프로세스에서 동일 job을 동시에 읽고
        # 전진시키지 못하게 해요. job별 lock이라 서로 다른 배포는 병렬로 진행돼요.
        self._poll_locks_guard = threading.Lock()
        self._poll_locks: dict[str, threading.Lock] = {}

    # ── 생성 ──────────────────────────────────────────────────────────
    def create_job(self, source_ref: SourceRef, meta: dict, principal: str,
                   *, redeploy_runtime_id: str = "",
                   redeploy_record_id: str = "",
                   selected_tools: list[str] | tuple[str, ...] = (),
                   job_id: str = "") -> DeployJob:
        """배포 job을 만들어요.

        redeploy_* 를 주면 재배포 job이 돼요 — 이름 충돌 검사를 건너뛰고(그 이름의
        주인이 자기 자신이니까요) UpdateAgentRuntime으로 기존 runtime을 갱신해요.
        소유권 확인은 라우터가 먼저 해요(_require_owner).
        """
        if job_id:
            existing = self.store.get(job_id)
            if existing is not None:
                return existing
        asset_type = meta.get("asset_type", "mcp")
        manifest = self.source_store.get_manifest(source_ref.asset_id, source_ref.version)
        paths = manifest.paths()
        build_type = decide_build_type(paths)
        agent_dependencies = None
        inherited_memory_id = None
        inherited_builtin_resources: dict = {}
        inherited_execution_role_arn = None
        redeploy_target_snapshot = None

        if asset_type == "agent":
            check_agent_spec(manifest)  # 미충족 시 SpecCheckError (AgentBinding.validate 위임)
            agent_dependencies = self._agent_dependencies(source_ref, paths=paths)
            meta = {
                **meta,
                "tool_requests": _validated_agent_tool_requests(
                    meta.get("tool_requests"),
                    agent_dependencies,
                ),
            }
            if redeploy_record_id:
                current_record = self.registry.get_record(
                    self.registry_id,
                    redeploy_record_id,
                )
                requested_memory = (
                    dict(
                        agent_dependencies.get("memory")
                        or {"mode": "DISABLED"}
                    )
                    if agent_dependencies is not None
                    else {"mode": "DISABLED"}
                )
                inherited_memory_id = inherited_memory_id_for_redeploy(
                    current_record,
                    requested_memory,
                )
                agent_node = (current_record.descriptors or {}).get("agent") or {}
                execution_binding = agent_node.get("executionBinding") or {}
                if isinstance(execution_binding, dict):
                    inherited_execution_role_arn = (
                        str(execution_binding.get("executionRoleArn") or "")
                        or None
                    )
                existing_builtin_tools = tuple(
                    agent_node.get("builtinTools") or ()
                )
                requested_builtin_tools = tuple(
                    agent_dependencies.get("builtinTools") or ()
                    if agent_dependencies is not None
                    else ()
                )
                if existing_builtin_tools != requested_builtin_tools:
                    raise SpecCheckError(
                        "재배포에서 기존 AgentCore 내장 도구 선언 변경은 "
                        "지원하지 않아요."
                    )
                raw_resources = agent_node.get("builtinToolResources") or {}
                if requested_builtin_tools and not isinstance(raw_resources, dict):
                    raise SpecCheckError(
                        "기존 AgentCore 내장 도구 resource descriptor가 "
                        "올바르지 않아요."
                    )
                inherited_builtin_resources = {
                    kind: {
                        **dict(raw_resources[kind]),
                        "network_mode": str(
                            raw_resources[kind].get("networkMode")
                            or raw_resources[kind].get("network_mode")
                            or "PUBLIC"
                        ),
                    }
                    for kind in requested_builtin_tools
                    if isinstance(raw_resources.get(kind), dict)
                    and raw_resources[kind].get("id")
                    and raw_resources[kind].get("arn")
                }
                if len(inherited_builtin_resources) != len(
                    requested_builtin_tools
                ):
                    raise SpecCheckError(
                        "기존 AgentCore 내장 도구 ID/ARN이 없어 "
                        "재배포할 수 없어요."
                    )
        else:
            def _reader(path: str) -> bytes:
                return self.source_store.read_file(source_ref.asset_id, source_ref.version, path)
            check_mcp_spec(paths, _reader)  # 미충족 시 SpecCheckError
            if redeploy_record_id:
                current_record = self.registry.get_record(
                    self.registry_id,
                    redeploy_record_id,
                )
                redeploy_target_snapshot = _mcp_redeploy_target_snapshot(
                    current_record
                )
            if selected_tools:
                from .tool_extract import discover_mcp_tools

                source_files = {
                    path: _reader(path).decode("utf-8", errors="ignore")
                    for path in paths
                    if path.endswith(".py")
                }
                discovered = {
                    str(tool.get("name"))
                    for tool in discover_mcp_tools(source_files)
                    if tool.get("name")
                }
                missing = sorted(set(selected_tools) - discovered)
                if missing:
                    raise SpecCheckError(
                        "선택한 tool을 업로드 소스에서 찾을 수 없어요: "
                        + ", ".join(missing)
                    )

        name = meta.get("name") or source_ref.asset_id.split("/")[-1]
        job_id = job_id or self.new_id()
        replay_record = None
        if asset_type == "agent" and not (
            redeploy_runtime_id or redeploy_record_id
        ):
            replay_record = self._find_replay_record(
                source_ref,
                name,
                principal,
            )
        release_record_reference = None
        reserved_record_id = ""
        record_reference_added = False
        job_admitted = False

        def _reserve_record_reference(record_id: str):
            reserve_reference = getattr(
                self.store,
                "reserve_job_reference_for_record",
                None,
            )
            release_reference = getattr(
                self.store,
                "release_job_reference_for_record",
                None,
            )
            if reserve_reference is None or release_reference is None:
                raise RuntimeError(
                    "deploy job store does not support "
                    "record references"
                )
            added = reserve_reference(record_id, job_id=job_id)
            return release_reference, added

        def _initialize_record_reference(record_id: str):
            initialize_reference = getattr(
                self.store,
                "initialize_job_reference_for_record",
                None,
            )
            release_reference = getattr(
                self.store,
                "release_job_reference_for_record",
                None,
            )
            if initialize_reference is None or release_reference is None:
                raise RuntimeError(
                    "deploy job store does not support "
                    "record references"
                )
            initialize_reference(record_id, job_id=job_id)
            return release_reference

        try:
            if replay_record is not None:
                reserved_record_id = replay_record.record_id
                (
                    release_record_reference,
                    record_reference_added,
                ) = _reserve_record_reference(
                    reserved_record_id,
                )
                if not record_reference_added:
                    existing = self.store.get(job_id)
                    if existing is not None:
                        job_admitted = True
                        return existing
                    raise ConcurrentJobAdmission(
                        f"deploy job admission is already in progress: {job_id}"
                    )
            # 사전확인 API와 같은 판정·문구를 사용해 경쟁 조건으로 실제 배포에서 막혀도
            # 사용자가 동일한 원인을 보게 해요. 재배포는 현재 runtime이 이름의 주인이므로 예외예요.
            if (
                asset_type == "agent"
                and not (redeploy_runtime_id or redeploy_record_id)
            ):
                if replay_record is None:
                    check = self.agent_name_check(name)
                else:
                    check = NameCheck(
                        available=not self.port.runtime_name_exists(
                            name.strip()
                        ),
                        reason="runtime_exists",
                    )
                if not check.available:
                    raise RuntimeNameConflict(
                        name_conflict_message(check.reason, name),
                    )
            # 배포 허가는 등록 판정과 다른 계약이에요(ADR-017 결정 7) — 이 시점엔 Registry
            # 레코드가 아직 없어서 record_id 기반 등록 hook을 쓸 수 없고, 판단 근거도
            # 소스·대상·재배포 여부예요. 현행 동작 보존: REJECTED만 차단하고 PENDING_REVIEW는
            # 진행시켜요. PENDING_REVIEW를 대기 상태로 표현하는 건 runtime 상태 모델 결정이
            # 필요해서(ADR-017 open question 3) 별도 작업으로 남겼어요.
            decision = self.gate.authorize(DeploymentAuthorizationRequest(
                principal=principal,
                asset_name=name,
                asset_id=source_ref.asset_id,
                version=source_ref.version,
                asset_type=asset_type,
                is_redeploy=bool(
                    redeploy_record_id or redeploy_runtime_id
                ),
                target_record_id=redeploy_record_id or "",
            ))
            if decision == GovernanceDecision.REJECTED:
                raise GateRejected("거버넌스 게이트에서 반려됐어요.")

            oauth_client_id = ""
            provision_with_provenance = getattr(
                self.identity_issuer,
                "provision_managed_runtime_client_with_provenance",
                None,
            )
            provision = getattr(
                self.identity_issuer,
                "provision_managed_runtime_client",
                None,
            )
            get_existing = getattr(
                self.identity_issuer,
                "get_existing_client_id",
                None,
            )
            if (
                asset_type == "agent"
                and redeploy_record_id
                and get_existing is not None
            ):
                oauth_client_id = get_existing(redeploy_record_id)
            job = DeployJob(
                job_id=job_id,
                phase=DeployPhase.QUEUED,
                principal=principal,
                source_ref=source_ref,
                meta=meta,
                build_type=build_type,
                asset_type=asset_type,
                created_at=self.now(),
                updated_at=self.now(),
                redeploy_runtime_id=redeploy_runtime_id or None,
                redeploy_record_id=redeploy_record_id or None,
                redeploy_target_snapshot=redeploy_target_snapshot,
                selected_tools=tuple(dict.fromkeys(selected_tools)),
                oauth_client_id=oauth_client_id or None,
                mcp_assets=(
                    tuple(agent_dependencies["mcpAssets"])
                    if agent_dependencies is not None
                    else None
                ),
                memory_config=(
                    dict(
                        agent_dependencies.get("memory")
                        or {"mode": "DISABLED"}
                    )
                    if agent_dependencies is not None
                    else {"mode": "DISABLED"}
                ),
                conversation_manager_config=(
                    dict(agent_dependencies["conversationManager"])
                    if agent_dependencies is not None
                    and agent_dependencies.get("conversationManager")
                    is not None
                    else None
                ),
                memory_id=inherited_memory_id,
                builtin_tools=tuple(
                    agent_dependencies.get("builtinTools") or ()
                    if agent_dependencies is not None
                    else ()
                ),
                builtin_resources=inherited_builtin_resources,
                builtin_owner_ids={},
                execution_role_arn=inherited_execution_role_arn,
            )
            if asset_type == "agent":
                if redeploy_record_id:
                    job.record_id = redeploy_record_id
                else:
                    if replay_record is None:
                        record = self._create_agent_draft(job)
                        job.record_created = True
                    else:
                        record = replay_record
                    job.record_id = record.record_id
                    job.provisioning_owner_id = self.owner_id
                    if release_record_reference is None:
                        reserved_record_id = job.record_id
                        try:
                            release_record_reference = (
                                _initialize_record_reference(
                                    reserved_record_id,
                                )
                            )
                        except Exception:
                            rollback_reference = getattr(
                                self.store,
                                "rollback_job_reference_initialization_for_record",
                                None,
                            )
                            rollback_safe = False
                            if rollback_reference is not None:
                                try:
                                    rollback_safe = rollback_reference(
                                        reserved_record_id,
                                        job_id=job_id,
                                    )
                                except Exception:  # noqa: BLE001
                                    _log.exception(
                                        "agent draft reference rollback failed: "
                                        "job_id=%s record_id=%s",
                                        job_id,
                                        reserved_record_id,
                                    )
                            if rollback_safe:
                                try:
                                    self.registry.delete_record(
                                        self.registry_id,
                                        reserved_record_id,
                                    )
                                except Exception:
                                    try:
                                        restore_release = (
                                            _initialize_record_reference(
                                                reserved_record_id,
                                            )
                                        )
                                        restore_release(
                                            reserved_record_id,
                                            job_id=job_id,
                                        )
                                    except Exception:  # noqa: BLE001
                                        _log.exception(
                                            "agent draft reference restore failed: "
                                            "job_id=%s record_id=%s",
                                            job_id,
                                            reserved_record_id,
                                        )
                                    raise
                            raise
                        record_reference_added = True
                if (
                    not oauth_client_id
                    and provision_with_provenance is not None
                ):
                    provisioned_client = provision_with_provenance(
                        job.record_id
                    )
                    oauth_client_id = provisioned_client.client_id
                    job.oauth_client_created = provisioned_client.created
                elif not oauth_client_id and provision is not None:
                    oauth_client_id = oauth_client_id or provision(
                        job.record_id
                    )
                job.oauth_client_id = oauth_client_id or None
            put_if_absent = getattr(self.store, "put_if_absent", None)
            if put_if_absent is None:
                self.store.put(job)
            elif not put_if_absent(job):
                existing = self.store.get(job_id)
                if existing is None:
                    raise RuntimeError(
                        "deploy job create conflicted but existing job was "
                        "not found"
                    )
                job_admitted = True
                return existing
            job_admitted = True
            return job
        finally:
            if (
                not job_admitted
                and record_reference_added
                and release_record_reference is not None
                and reserved_record_id
            ):
                release_record_reference(
                    reserved_record_id,
                    job_id=job_id,
                )

    def agent_name_check(self, name: str) -> NameCheck:
        """배포 전 이름을 확인하고 Runtime/Registry 충돌 사유까지 돌려줘요.

        반려·폐기 Registry 레코드도 name + recordVersion unique key를 점유하므로 허용할
        수 없어요. Registry 조회 실패는 사전확인 편의 기능을 깨지 않도록 관대하게 넘겨요.
        """
        if not name or not name.strip():
            return NameCheck(available=True)
        target = name.strip()
        if self.port.runtime_name_exists(target):
            return NameCheck(available=False, reason="runtime_exists")
        status = self._registry_name_status(target)
        if status in ACTIVE_NAME_STATUSES:
            return NameCheck(
                available=False,
                reason="active_record",
                conflicting_status=status,
            )
        if status in DEAD_NAME_STATUSES:
            return NameCheck(
                available=False,
                reason="rejected_remains",
                conflicting_status=status,
            )
        return NameCheck(available=True)

    def agent_name_available(self, name: str) -> bool:
        """기존 bool 계약을 유지해요."""
        return self.agent_name_check(name).available

    def _registry_name_status(self, name: str) -> str:
        """동명 Registry 레코드 상태. 여러 건이면 활성 상태를 우선해요."""
        from ....shared.deps import get_registry, get_registry_id

        try:
            records = get_registry().list_records(get_registry_id())
        except Exception:
            return ""
        statuses = set()
        for record in records:
            if (getattr(record, "name", "") or "") != name:
                continue
            status = getattr(record, "status", None)
            statuses.add(
                status.value if hasattr(status, "value") else str(status),
            )
        for candidate in ACTIVE_NAME_STATUSES:
            if candidate in statuses:
                return candidate
        for candidate in DEAD_NAME_STATUSES:
            if candidate in statuses:
                return candidate
        return ""

    # ── 조회 ──────────────────────────────────────────────────────────
    def list_deployments(self) -> list[DeployJob]:
        """관리 콘솔용 read model의 원본 job 목록을 반환해요."""
        return self.store.list()

    def get_deployment(self, job_id: str) -> DeployJob | None:
        """관리 콘솔용 read model의 원본 job 한 건을 반환해요."""
        return self.store.get(job_id)

    def get_agent_monitoring_job(
        self,
        record_id: str,
    ) -> AgentMonitoringJobProjection | None:
        return self.store.get_monitoring(record_id)

    def batch_agent_monitoring_jobs(
        self,
        record_ids: list[str],
    ) -> dict[str, AgentMonitoringJobProjection]:
        return self.store.batch_get_monitoring(record_ids)

    # ── 진행 ──────────────────────────────────────────────────────────
    def poll(self, job_id: str):
        with self._poll_locks_guard:
            job_lock = self._poll_locks.setdefault(job_id, threading.Lock())
        with job_lock:
            job = self.store.get(job_id)
            if job is None:
                return None
            if terminal(job.phase):
                return job
            expected_phase = job.phase
            try:
                if job.asset_type == "agent":
                    cognito = dict(self.cognito)
                    if job.oauth_client_id:
                        cognito["oauth_client_id"] = job.oauth_client_id
                        cognito["provider_name"] = (
                            f"agora-agent-{job.oauth_client_id}"[:64]
                        )
                    cognito["mcp_assets"] = job.mcp_assets or ()
                    cognito["fail_closed_on_identity_outbound"] = (
                        self.fail_closed_on_identity_outbound
                    )
                    job = advance_agent(
                        job, self.port,
                        exec_role_arn=self.agent_exec_role_arn,
                        cognito=cognito,
                        register=self._register_agent,
                        now=self.now,
                        verify=(
                            self._verify_agent
                            if self.verifier is not None
                            else None
                        ),
                        compensate_record=self._compensate_agent_record,
                        finalize=self._finalize_agent,
                        owner_id=self.owner_id,
                        fail_closed_on_unknown_authorization=(
                            self.fail_closed_on_unknown_authorization
                        ),
                        fail_closed_on_unknown_builtin_tools=(
                            self.fail_closed_on_unknown_builtin_tools
                        ),
                        builtin_config=self.builtin_config,
                        agent_role_config=self.agent_role_config,
                        checkpoint_role=self._checkpoint_agent_role,
                    )
                else:
                    job = advance(
                        job, self.port,
                        gateway_id=self.target_gateway_id,
                        exec_role_arn=self.exec_role_arn,
                        register=self._register, now=self.now,
                        owner_id=self.owner_id,
                        redeploy_sensitivity_guard=(
                            self.redeploy_sensitivity_guard
                        ),
                        provision_shared_policy=(
                            self.provision_shared_policy
                        ),
                        provision_read_access=self.provision_read_access,
                    )
            except ConcurrentJobAdvance:
                _log.warning(
                    "deploy job checkpoint skipped; another actor advanced it: "
                    "job_id=%s expected_phase=%s",
                    job_id,
                    expected_phase.value,
                )
                return self.store.get(job_id)
            put_if_phase = getattr(self.store, "put_if_phase", None)
            if (
                put_if_phase is not None
                and not self._put_next_revision(job, expected_phase)
            ):
                # 폴러를 꺼도 프론트 poll·수동 배포가 같은 경로를 호출할 수 있어요.
                # stale 결과를 쓰지 않고 winner 상태를 반환해 terminal 전이를 단조롭게 지켜요.
                _log.warning(
                    "deploy job phase write skipped; another actor advanced it: "
                    "job_id=%s expected_phase=%s attempted_phase=%s",
                    job_id,
                    expected_phase.value,
                    job.phase.value,
                )
                if job.phase is DeployPhase.FAILED:
                    _log.error(
                        "deploy compensation skipped: failure claim lost; "
                        "job_id=%s",
                        job_id,
                    )
                return self.store.get(job_id)
            if put_if_phase is None:
                job.state_revision += 1
                self.store.put(job)
            if job.asset_type == "agent" and terminal(job.phase):
                # 요청 로그는 원장·Cedar 중간 상태가 아니라 CAS로 확정된 배포 결과를 말해요.
                # 이후 poll은 terminal 단축 경로로 빠지므로 transition 승자만 한 번 써요.
                self._record_tool_request_log(job)
            compensated = False
            if (
                expected_phase is not DeployPhase.FAILED
                and job.phase is DeployPhase.FAILED
            ):
                if job.asset_type == "agent":
                    compensated = compensate_agent_failure(
                        job,
                        self.port,
                        owner_id=self.owner_id,
                        compensate_record=self._compensate_agent_record,
                    )
                else:
                    compensated = compensate_mcp_failure(
                        job,
                        self.port,
                        gateway_id=self.target_gateway_id,
                        owner_id=self.owner_id,
                    )
            if compensated:
                if put_if_phase is not None:
                    if not self._put_next_revision(job, DeployPhase.FAILED):
                        return self.store.get(job_id)
                else:
                    job.state_revision += 1
                    self.store.put(job)
            return job

    def _put_next_revision(
        self,
        job: DeployJob,
        expected_phase: DeployPhase,
    ) -> bool:
        expected_revision = job.state_revision
        job.state_revision = expected_revision + 1
        try:
            stored = self.store.put_if_phase(
                job,
                expected_phase,
                expected_revision,
            )
        except Exception:
            job.state_revision = expected_revision
            raise
        if not stored:
            job.state_revision = expected_revision
        return stored

    def _checkpoint_agent_role(self, job: DeployJob) -> None:
        if not self._put_next_revision(job, DeployPhase.BUILDING):
            raise ConcurrentJobAdvance

    # ── MCP 등재 (REGISTERING에서 advance가 호출) ────────────────────
    def _register(self, job: DeployJob) -> str:
        manifest = self.source_store.get_manifest(
            job.source_ref.asset_id, job.source_ref.version)
        s3_prefix = f"mcp/{job.source_ref.asset_id}/{job.source_ref.version}/"
        m = job.meta
        name = m.get("name") or job.source_ref.asset_id.split("/")[-1]
        gateway_targets = [
            {
                "sensitivity": target["sensitivity"],
                "gatewayTargetName": target["gateway_target_name"],
                "gatewayTargetId": target["target_id"],
                "gatewayTargetState": str(target["state"]).lower(),
                "operations": list(target.get("operations") or ()),
            }
            for target in job.gateway_targets
            if (
                target.get("sensitivity")
                and target.get("target_id")
                and target.get("state") == "READY"
            )
        ]
        descriptors = get_binding("mcp").build_descriptors(
            manifest,
            {"s3_prefix": s3_prefix, "endpoint": job.gateway_url,
             "tools_inline": job.tools_inline,
             "gateway_identifier": self.target_gateway_id,
             "gateway_targets": gateway_targets,
             "unassigned_tools": list(job.unassigned_tools)},
        )
        if job.redeploy_record_id:
            # 재배포: 새 레코드를 만들지 않고 기존 것을 갱신 — 리뷰·조회수·bundle 멤버십 유지(ADR-0021,
            # _register_agent와 대칭). 버전(recordVersion)만 새 값으로. 승인 재스캔은 poll의
            # process_deploy_governance(is_redeploy)가 새 버전에 대해 다시 태워요.
            rec = self.registry.update_record_descriptors(
                self.registry_id, job.redeploy_record_id, name,
                DescriptorType.MCP, descriptors, job.source_ref.version,
                description=m.get("description", ""),
            )
            return rec.record_id
        rec = self.registry.create_record(
            self.registry_id, name,
            DescriptorType.MCP, descriptors, job.source_ref.version,
            description=m.get("description", ""), owner_team=m.get("owner_team", ""),
            escalation_contact=m.get("escalation_contact", ""),
            # 1차 담당자는 업로드 티켓 meta 로 운반된 principal email 이에요 (CA-29).
            owner_contact=m.get("owner_contact", ""),
            owner_user=job.principal, tags=tuple(m.get("tags", [])),
            category=m.get("category", ""),
        )
        return rec.record_id

    # ── Agent 프로비저닝 (VERIFYING 전에 advance_agent가 호출) ───────
    def _expected_tools(self, job: DeployJob) -> tuple[ExpectedTool, ...]:
        """Build operation expectations from the resolved deployment declaration."""
        import json

        expected: list[ExpectedTool] = []
        for asset in job.mcp_assets or ():
            asset_id = str(asset.get("assetId") or "").strip()
            target_bindings: list[tuple[str, tuple[str, ...]]] = []
            if "gatewayTargets" in asset:
                raw_targets = asset.get("gatewayTargets")
                if isinstance(raw_targets, (list, tuple)):
                    target_bindings = [
                        (
                            str(target.get("name") or "").strip(),
                            tuple(
                                str(operation)
                                for operation in target.get("operations") or ()
                                if str(operation).strip()
                            ),
                        )
                        for target in raw_targets
                        if isinstance(target, dict)
                    ]
            else:
                target_bindings = [(
                    str(asset.get("name") or "").strip(),
                    tuple(
                        str(operation)
                        for operation in asset.get("operations") or ()
                        if str(operation).strip()
                    ),
                )]
            tool_documents: dict[str, dict] = {}
            lookup_error = ""
            try:
                record = self.registry.get_record(self.registry_id, asset_id)
                mcp_node = (
                    record.descriptors.get("mcp")
                    if isinstance(record.descriptors, dict)
                    else None
                )
                tools_node = (
                    mcp_node.get("tools")
                    if isinstance(mcp_node, dict)
                    else None
                )
                inline = (
                    tools_node.get("inlineContent")
                    if isinstance(tools_node, dict)
                    else ""
                )
                document = json.loads(inline) if inline else {}
                tool_documents = {
                    str(tool["name"]): tool
                    for tool in (
                        document.get("tools", [])
                        if isinstance(document, dict)
                        else ()
                    )
                    if isinstance(tool, dict) and tool.get("name")
                }
            except Exception as error:
                lookup_error = f"{type(error).__name__}: {error}"

            for target_name, operations in target_bindings:
                if not target_name or not operations:
                    expected.append(
                        ExpectedTool(
                            name=target_name or asset_id,
                            probe_error="operation declaration이 없어요.",
                        )
                    )
                    continue
                for operation in operations:
                    names = gateway_tool_names(target_name, operation)
                    document = tool_documents.get(operation, {})
                    document_error = (
                        ""
                        if document
                        else f"Registry에서 {operation} schema를 찾을 수 없어요."
                    )
                    schema = document.get("inputSchema") or {}
                    if (
                        isinstance(schema, dict)
                        and isinstance(schema.get("json"), dict)
                    ):
                        schema = schema["json"]
                    if not isinstance(schema, dict):
                        schema = {}
                    explicit_sensitivity = str(
                        document.get("sensitivity") or ""
                    ).strip().upper()
                    sensitivity = (
                        explicit_sensitivity
                        if explicit_sensitivity in allowed_tags(
                            PermissionGroup.FULL_ACCESS
                        )
                        else None
                    )
                    sensitivity_error = ""
                    if not explicit_sensitivity:
                        sensitivity_error = (
                            "Registry 명시 sensitivity 태그가 없어요."
                        )
                    elif sensitivity is None:
                        sensitivity_error = (
                            "Registry sensitivity 태그를 판정할 수 없어요: "
                            f"{explicit_sensitivity}"
                        )
                    arguments, argument_error = _probe_arguments(schema)
                    expected.append(
                        ExpectedTool(
                            name=names[0],
                            aliases=names[1:],
                            sensitivity=sensitivity,
                            probe_arguments=arguments,
                            probe_error=(
                                lookup_error
                                or document_error
                                or sensitivity_error
                                or argument_error
                            ),
                        )
                    )

        if self._declares_skill(job):
            expected.append(ExpectedTool(name="skills"))
        return tuple(expected)

    def _declares_skill(self, job: DeployJob) -> bool:
        import json

        try:
            raw = self.source_store.read_file(
                job.source_ref.asset_id,
                job.source_ref.version,
                "agent-card.json",
            )
            card = json.loads(
                raw.decode("utf-8") if isinstance(raw, bytes) else raw
            )
        except Exception:
            return False
        for skill in card.get("skills") or ():
            if not isinstance(skill, dict):
                continue
            tags = {
                tag.strip().lower()
                for tag in skill.get("tags") or ()
                if isinstance(tag, str)
            }
            if "skill" in tags:
                return True
        return False

    def _verify_agent(self, job: DeployJob):
        """VERIFYING 단계 훅 — 배포된 agent에 A2A로 도구 등록·대화를 확인해요."""
        from ....shared.actor import derive_scoped_actor_id
        from .verify import VerifyReport

        builtin_observability = None
        if self.builtin_verifier is not None:
            builtin_report = self.builtin_verifier.verify(
                expected_tools=job.builtin_tools,
                resources=job.builtin_resources or {},
                record_id=job.record_id or "",
            )
            builtin_observability = builtin_report.to_dict()
            if not builtin_report.ok:
                return VerifyReport(
                    ok=False,
                    verdict=builtin_report.verdict,
                    reason=builtin_report.reason,
                    builtin_observability=builtin_observability,
                )
        elif job.builtin_tools:
            return VerifyReport(
                ok=False,
                verdict="unknown",
                reason="내장 도구 제어플레인 verifier가 배선되지 않았어요.",
                builtin_observability={
                    "ok": False,
                    "verdict": "unknown",
                    "resources": {},
                    "network_modes": {},
                    "spans": "unknown",
                    "metrics": "not_checked_until_first_use",
                    "reason": "control-plane verifier is not wired",
                },
            )

        memory_config = job.memory_config or {"mode": "DISABLED"}
        memory_mode = str(memory_config.get("mode") or "DISABLED").upper()
        expected_memory_namespaces = configured_memory_namespaces(memory_config)
        verify_kwargs = {
            "runtime_arn": job.runtime_arn or "",
            "expected_tools": self._expected_tools(job),
            "negative_control": self._negative_control(job),
            "policy_revision": (
                f"{job.record_id}:r{job.provisioned_policy_revision}"
                if job.record_id and job.provisioned_policy_revision is not None
                else ""
            ),
            "actor_id": derive_scoped_actor_id(
                job.record_id or job.source_ref.asset_id,
                "agora-deploy-verifier",
            ),
            "memory_required": bool(
                memory_mode == "MANAGED"
            ),
            "expected_memory_mode": memory_mode,
            "expected_memory_namespaces": expected_memory_namespaces,
            "expected_conversation_manager":
                job.conversation_manager_config,
        }
        if job.builtin_tools:
            verify_kwargs["unprobed_builtin_tools"] = tuple(
                "browser" if kind == "browser" else "code_interpreter"
                for kind in job.builtin_tools
            )
        # IA-61 후속: 검증 호출도 `X-Agora-Call` handle 이 필요해요. interceptor 가 붙은
        # 뒤로 handle 없는 `initialize` 는 거부돼서 agent 가 도구를 하나도 못 받아요
        # (실측 2026-08-29: `reason=invalid_delegation`). 실사용 경로와 같은 방식으로 실어요.
        #
        # 사람 축은 **배포를 시작한 사용자**예요(`job.principal`). 검증은 그 사람 대신
        # 도구를 부르는 것이라 그게 정직한 귀속이에요. 시스템 자신을 주체로 두면 원장에
        # 사람 없는 delegation 이 생겨요.
        verify_kwargs["call_handle"] = self._verify_call_handle(job)
        states, states_reason = self._tool_approval_states(job)
        verify_kwargs["tool_approval_states"] = states
        verify_kwargs["tool_authorization_reason"] = states_reason
        report = self.verifier.verify(
            **verify_kwargs,
        )
        if builtin_observability is None:
            return report
        return replace(
            report,
            builtin_observability=builtin_observability,
        )

    def _tool_approval_states(
        self, job: DeployJob
    ) -> tuple[dict[str, str] | None, str]:
        """④ 원장의 `gateway_action → approval_state`. 실패는 `(None, 사유)` 예요.

        `None` 을 빈 dict 으로 접지 않아요 — 빈 dict 는 「전부 미신청이라고 관측함」이고
        `None` 은 「관측 못 함」이에요. 후자를 전자로 접으면 원장 조회가 죽은 배포가 「도구가
        하나도 승인 안 됨」으로 조용히 통과해요(ADR-0037 §4).
        """
        if self.read_tool_approval_states is None:
            return None, "④ 원장 조회 훅이 배선되지 않았어요"
        record_id = job.record_id or ""
        if not record_id:
            return None, "job 에 agent record ID 가 없어요"
        try:
            states = self.read_tool_approval_states(record_id)
        except Exception as exc:
            _log.warning(
                "tool approval 상태 조회 실패 job_id=%s failure=%s",
                job.job_id, type(exc).__name__,
            )
            return None, f"{type(exc).__name__}"
        if not isinstance(states, dict):
            return None, "④ 원장 조회가 dict 을 돌려주지 않았어요"
        return states, ""

    def _verify_call_handle(self, job: DeployJob) -> str | None:
        """검증용 delegation handle 을 발급해요. 실패는 검증을 막지 않아요.

        발급이 안 되면 `None` 을 돌려 handle 없이 호출해요 — 그러면 interceptor 가 거부하고
        verify 가 "선언된 도구가 없어요" 로 실패해요. 그게 **맞는 결과**예요: handle 을 못
        만드는 상태에서 배포를 통과시키면 실사용에서도 못 부르는 agent 가 READY 가 돼요.
        발급 실패 자체를 여기서 감추지 않고 경고로 남겨요.
        """
        if self.issue_call_handle is None:
            return None
        try:
            return self.issue_call_handle(job)
        except Exception as exc:
            _log.warning(
                "verify call handle 발급 실패 job_id=%s failure=%s",
                job.job_id, type(exc).__name__,
            )
            return None

    def _negative_control(self, job: DeployJob) -> NegativeControl | None:
        """Choose an approved, explicit READ operation outside this agent's grant.

        Two passes, tightest first:

        1. A READ operation of an MCP the agent *does* declare but did not
           request. Same asset, same Gateway — the cleanest control.
        2. A READ operation of an approved MCP the agent declares **not at all**.
           Needed because pass 1 finds nothing when an agent legitimately
           requests every safe READ operation of its only MCP — measured
           2026-08-21 with `weather-mcp` (`get_today_weather` +
           `get_weekly_forecast` granted, `resolve_region` is UPDATE), which made
           the deploy unsatisfiable: `Gateway에 존재하면서 이 agent에는 미승인인
           안전한 READ operation을 찾지 못해...`. Both passes stay on the same
           Gateway so a denial cannot be confused with unreachability.
        """
        declared_asset_ids = {
            str(asset.get("assetId") or "").strip()
            for asset in job.mcp_assets or ()
            if str(asset.get("assetId") or "").strip()
        }
        for asset in job.mcp_assets or ():
            asset_id = str(asset.get("assetId") or "").strip()
            declared = {
                str(operation)
                for operation in asset.get("operations") or ()
                if str(operation).strip()
            }
            try:
                record = self.registry.get_record(self.registry_id, asset_id)
            except Exception:
                continue
            candidate = self._negative_control_from_record(record, declared)
            if candidate is not None:
                return candidate
        # Pass 2 — approved MCPs this agent never declared.
        try:
            records = self.registry.list_records(
                self.registry_id,
                statuses=(RecordStatus.APPROVED,),
            )
        except Exception:
            return None
        for record in records:
            if getattr(record, "record_id", "") in declared_asset_ids:
                continue
            candidate = self._negative_control_from_record(record, set())
            if candidate is not None:
                return candidate
        return None

    def _negative_control_from_record(
        self,
        record,
        declared: set[str],
    ) -> NegativeControl | None:
        """Pick one safe READ operation of `record` that is not in `declared`."""
        import json

        if (
            getattr(record, "descriptor_type", DescriptorType.MCP)
            is not DescriptorType.MCP
            or getattr(record, "status", RecordStatus.APPROVED)
            is not RecordStatus.APPROVED
        ):
            return None
        descriptors = getattr(record, "descriptors", None)
        mcp_node = (
            descriptors.get("mcp") if isinstance(descriptors, dict) else None
        )
        tools_node = (
            mcp_node.get("tools") if isinstance(mcp_node, dict) else None
        )
        inline = (
            tools_node.get("inlineContent")
            if isinstance(tools_node, dict)
            else ""
        )
        try:
            document = json.loads(inline) if inline else {}
        except (TypeError, ValueError):
            return None
        endpoint = self.oauth_gateway_url or endpoint_of_descriptors(descriptors)
        if not endpoint:
            return None
        for tool in (
            document.get("tools", []) if isinstance(document, dict) else ()
        ):
            if not isinstance(tool, dict):
                continue
            operation = str(tool.get("name") or "").strip()
            sensitivity = str(tool.get("sensitivity") or "").strip().upper()
            if not operation or operation in declared or sensitivity != "READ":
                continue
            schema = tool.get("inputSchema") or {}
            if isinstance(schema, dict) and isinstance(schema.get("json"), dict):
                schema = schema["json"]
            if not isinstance(schema, dict):
                continue
            arguments, error = _probe_arguments(schema)
            if error:
                continue
            try:
                split_targets = _split_mcp_target_bindings(
                    record,
                    [operation],
                )
            except SpecCheckError:
                continue
            if split_targets is None:
                target_name = _mcp_target_name(record)
            elif len(split_targets) == 1:
                target_name = str(split_targets[0]["name"])
            else:
                continue
            return NegativeControl(
                tool=gateway_tool_names(target_name, operation)[0],
                endpoint=endpoint,
                arguments=arguments,
            )
        return None

    def _agent_requires_runtime_policy(
        self,
        source_ref: SourceRef,
        paths: tuple[str, ...],
    ) -> bool:
        import json

        if "main.py" in paths and any(path.startswith("agent/") for path in paths):
            return True
        for card_name in ("agent-card.json", "agent.json"):
            if card_name not in paths:
                continue
            try:
                raw = self.source_store.read_file(
                    source_ref.asset_id,
                    source_ref.version,
                    card_name,
                )
                card = json.loads(
                    raw.decode("utf-8") if isinstance(raw, bytes) else raw
                )
            except (TypeError, ValueError, OSError):
                continue
            for skill in card.get("skills") or () if isinstance(card, dict) else ():
                if not isinstance(skill, dict):
                    continue
                tags = {
                    tag.strip().lower()
                    for tag in skill.get("tags") or ()
                    if isinstance(tag, str)
                }
                if "mcp" in tags:
                    return True
        return False

    def _agent_dependencies(
        self,
        source_ref: SourceRef,
        *,
        paths: tuple[str, ...] = (),
    ) -> dict | None:
        """Initializr policy의 MCP asset/endpoint 결속을 카탈로그와 대조해요."""
        import json

        policy_required = self._agent_requires_runtime_policy(source_ref, paths)
        try:
            raw = self.source_store.read_file(
                source_ref.asset_id, source_ref.version, "agora-policy.json"
            )
        except Exception as exc:
            if policy_required:
                raise SpecCheckError(
                    "MCP를 선언한 agent에는 agora-policy.json이 필요해요."
                ) from exc
            return None
        try:
            policy = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except (TypeError, ValueError) as exc:
            raise SpecCheckError("agora-policy.json 형식이 올바르지 않아요.") from exc
        if not isinstance(policy, dict) or policy.get("version") != 2:
            raise SpecCheckError(
                "Agora runtime policy version 2가 필요해요."
            )
        assets = policy.get("mcpAssets")
        if not isinstance(assets, list):
            raise SpecCheckError("agora-policy.json의 mcpAssets는 배열이어야 해요.")

        canonical: list[dict] = []
        seen: set[str] = set()
        for item in assets:
            if not isinstance(item, dict):
                raise SpecCheckError("MCP dependency 형식이 올바르지 않아요.")
            asset_id = str(item.get("assetId") or "").strip()
            declared_name = str(item.get("name") or "").strip()
            endpoint = str(item.get("endpoint") or "").strip()
            if _requires_mcp_name_resolution(asset_id):
                if not declared_name:
                    raise SpecCheckError("이름으로 찾을 MCP dependency name이 없어요.")
                records = self.registry.list_records(
                    self.registry_id,
                    statuses=(RecordStatus.APPROVED,),
                )
                matches = [
                    record
                    for record in records
                    if record.name == declared_name
                    and record.descriptor_type is DescriptorType.MCP
                    and record.status is RecordStatus.APPROVED
                ]
                if not matches:
                    raise SpecCheckError(
                        f"승인된 MCP를 이름으로 찾을 수 없어요: {declared_name}"
                    )
                if len(matches) > 1:
                    raise SpecCheckError(
                        f"승인된 동명 MCP가 여러 개예요: {declared_name}"
                    )
                record = matches[0]
                asset_id = record.record_id
            else:
                try:
                    record = self.registry.get_record(self.registry_id, asset_id)
                except Exception as exc:
                    raise SpecCheckError(f"MCP 자산을 찾을 수 없어요: {asset_id}") from exc
            if asset_id in seen:
                raise SpecCheckError("MCP dependency assetId가 중복됐어요.")
            canonical_endpoint = endpoint_of_descriptors(record.descriptors)
            expected_endpoint = self.oauth_gateway_url or canonical_endpoint
            if (
                record.descriptor_type is not DescriptorType.MCP
                or record.status is not RecordStatus.APPROVED
                or not canonical_endpoint
                or endpoint != expected_endpoint
            ):
                raise SpecCheckError(
                    f"승인된 MCP 자산과 endpoint가 일치하지 않아요: {asset_id}"
                )
            declared_operations = item.get("operations")
            if (
                not isinstance(declared_operations, list)
                or not declared_operations
                or not all(
                    isinstance(operation, str) and operation.strip()
                    for operation in declared_operations
                )
            ):
                raise SpecCheckError(
                    "Agora runtime policy operations는 비어 있지 않은 "
                    "문자열 배열이어야 해요."
                )
            mcp_node = (
                record.descriptors.get("mcp")
                if isinstance(record.descriptors, dict)
                else None
            )
            tools_node = (
                mcp_node.get("tools")
                if isinstance(mcp_node, dict)
                else None
            )
            tools_inline = (
                tools_node.get("inlineContent")
                if isinstance(tools_node, dict)
                else ""
            )
            try:
                tools_document = (
                    json.loads(tools_inline) if tools_inline else {}
                )
            except (TypeError, ValueError) as exc:
                raise SpecCheckError(
                    f"MCP operation 목록이 올바르지 않아요: {asset_id}"
                ) from exc
            available_operations = {
                str(tool.get("name"))
                for tool in (
                    tools_document.get("tools", [])
                    if isinstance(tools_document, dict)
                    else []
                )
                if isinstance(tool, dict) and tool.get("name")
            }
            if not available_operations:
                raise SpecCheckError(
                    f"MCP operation 목록을 확인할 수 없어요: {asset_id}"
                )
            operations = list(dict.fromkeys(
                operation.strip() for operation in declared_operations
            ))
            missing_operations = sorted(
                set(operations) - available_operations
            )
            if missing_operations:
                raise SpecCheckError(
                    "승인된 MCP에 없는 operation이에요: "
                    + ", ".join(missing_operations)
                )
            seen.add(asset_id)
            binding = {
                "assetId": asset_id,
                "name": _mcp_target_name(record),
                "endpoint": expected_endpoint,
            }
            split_targets = _split_mcp_target_bindings(record, operations)
            if split_targets is not None:
                binding["gatewayTargets"] = split_targets
            record_version = str(getattr(record, "version", "") or "").strip()
            if record_version:
                binding["version"] = record_version
            binding["operations"] = operations
            canonical.append(binding)
        memory = policy.get("memory") or {"mode": "DISABLED"}
        try:
            builtin_tools = list(
                reject_disabled_builtin_tools(policy.get("builtinTools"))
            )
        except ValueError as exc:
            raise SpecCheckError(str(exc)) from exc
        if not isinstance(memory, dict):
            raise SpecCheckError("AgentCore Memory 설정 형식이 올바르지 않아요.")
        mode = memory.get("mode")
        if mode == "DISABLED":
            if set(memory) != {"mode"}:
                raise SpecCheckError(
                    "DISABLED Memory에는 관리형 설정을 넣을 수 없어요."
                )
        elif mode == "MANAGED":
            strategies = memory.get("strategies")
            strategy_configs = memory.get("strategy_configs")
            retention_days = memory.get("retention_days")
            allowed_strategies = set(SUPPORTED_MEMORY_STRATEGIES)
            try:
                require_supported_memory_strategies(strategies)
            except UnsupportedMemoryStrategyError as exc:
                raise SpecCheckError(str(exc)) from exc
            if (
                set(memory)
                not in (
                    {"mode", "strategies", "retention_days"},
                    {
                        "mode",
                        "strategies",
                        "strategy_configs",
                        "retention_days",
                    },
                )
                or not isinstance(strategies, list)
                or not strategies
                or any(
                    not isinstance(strategy, str)
                    or strategy not in allowed_strategies
                    for strategy in strategies
                )
                or len(strategies) != len(set(strategies))
                or isinstance(retention_days, bool)
                or not isinstance(retention_days, int)
                or not 3 <= retention_days <= 365
                or (
                    strategy_configs is not None
                    and not _valid_memory_strategy_configs(
                        strategy_configs,
                        strategies,
                    )
                )
            ):
                raise SpecCheckError(
                    "MANAGED AgentCore Memory 설정이 올바르지 않아요."
                )
            if strategy_configs is not None:
                memory = {
                    **memory,
                    "strategy_configs": {
                        strategy: {
                            **strategy_configs[strategy],
                            "namespaces": [
                                MEMORY_NAMESPACE_TEMPLATES[strategy]
                            ],
                        }
                        for strategy in strategies
                    },
                }
        else:
            raise SpecCheckError("지원하지 않는 AgentCore Memory 설정이에요.")

        # Wrapper version 1 is the internal canonical descriptor format.
        # Per-asset presence of operations carries the subset contract.
        result = {
            "version": 1,
            "mcpAssets": canonical,
        }
        if "memory" in policy:
            result["memory"] = memory
        conversation_manager = policy.get("conversationManager")
        if conversation_manager is not None:
            if not _valid_conversation_manager(conversation_manager):
                raise SpecCheckError(
                    "conversation manager 선언이 올바르지 않아요."
                )
            result["conversationManager"] = conversation_manager
        result["builtinTools"] = builtin_tools
        return result

    def _create_agent_draft(self, job: DeployJob):
        descriptors = self._agent_descriptors(job)
        meta = job.meta
        name = meta.get("name") or job.source_ref.asset_id.split("/")[-1]
        return self.registry.create_record(
            self.registry_id,
            name,
            DescriptorType.AGENT,
            descriptors,
            job.source_ref.version,
            description=meta.get("description", ""),
            owner_team=meta.get("owner_team", ""),
            escalation_contact=meta.get("escalation_contact", ""),
            # 1차 담당자는 업로드 티켓 meta 로 운반된 principal email 이에요 (CA-29).
            owner_contact=meta.get("owner_contact", ""),
            owner_user=job.principal,
            tags=tuple(meta.get("tags", [])),
            category=meta.get("category", ""),
        )

    def _register_agent(self, job: DeployJob) -> str:
        is_provisioning_replay = job.provisioning_checkpointed
        descriptors = self._agent_descriptors(job)
        m = job.meta
        name = m.get("name") or job.source_ref.asset_id.split("/")[-1]
        if job.redeploy_record_id:
            record_id = job.redeploy_record_id
        else:
            record_id = job.record_id
            if not record_id:
                rec = self._find_replay_record(
                    job.source_ref,
                    name,
                    job.principal,
                )
                if rec is None:
                    rec = self._create_agent_draft(job)
                record_id = rec.record_id
        job.record_id = record_id
        job.provisioning_checkpointed = True
        self._checkpoint_provisioning(job)
        self._issue_managed_identity(record_id, job)
        baseline_result = self._auto_provision_readonly_bindings(
            record_id,
            agent_descriptors=(
                descriptors if job.redeploy_record_id else None
            ),
            recover_interrupted_policy=is_provisioning_replay,
            resume_policy_revision=job.provisioned_policy_revision,
            resume_policy_id=job.provisioning_policy_id,
        )
        self._record_provisioning_result(job, baseline_result)
        request_result = self._submit_deploy_tool_requests(
            job,
            agent_descriptors=descriptors,
        )
        self._provision_shared_policy_for_bindings(
            job,
            baseline_result,
            request_result,
        )
        return record_id

    @staticmethod
    def _binding_coordinate(binding) -> dict[str, str]:
        def value(name: str) -> str:
            if isinstance(binding, dict):
                return str(binding.get(name) or "")
            return str(getattr(binding, name, "") or "")

        return {
            "asset_id": value("asset_id"),
            "asset_version": value("asset_version"),
            "operation_id": value("operation_id"),
        }

    def _merge_provisioned_bindings(
        self,
        job: DeployJob,
        bindings,
    ) -> None:
        merged_bindings = list(job.provisioned_tool_bindings)
        existing_keys = {
            (
                binding["asset_id"],
                binding["asset_version"],
                binding["operation_id"],
            )
            for binding in merged_bindings
        }
        for raw_binding in bindings:
            binding = self._binding_coordinate(raw_binding)
            key = (
                binding["asset_id"],
                binding["asset_version"],
                binding["operation_id"],
            )
            if key not in existing_keys:
                merged_bindings.append(binding)
                existing_keys.add(key)
        job.provisioned_tool_bindings = tuple(merged_bindings)

    def _submit_deploy_tool_requests(
        self,
        job: DeployJob,
        *,
        agent_descriptors: dict,
    ) -> dict | None:
        if self.submit_tool_requests is None:
            return None
        proposals = list(job.meta.get("tool_requests") or ())
        try:
            result = self.submit_tool_requests(
                job.record_id,
                proposals,
                principal_id=job.principal,
                agent_descriptors=agent_descriptors,
            )
        except Exception as exc:
            detail = (
                "도구 권한 신청 제출 중 서버 오류가 발생했어요: "
                f"{type(exc).__name__}: {exc}"
            )
            result = {
                "created": [],
                "newly_created": [],
                "skipped_non_read": [],
                "shared_policy_created": False,
                "batch_error": None,
                "errors": [
                    {
                        "assetId": str(item.get("asset_id") or ""),
                        "assetVersion": str(
                            item.get("asset_version") or ""
                        ),
                        "operationId": str(
                            item.get("operation_id") or ""
                        ),
                        "code": 500,
                        "detail": detail,
                    }
                    for item in proposals
                    if isinstance(item, dict)
                ],
            }
            _log.warning(
                "deploy tool request submission failed job_id=%s failure=%s",
                job.job_id,
                type(exc).__name__,
            )

        batch_error = result.get("batch_error")
        if batch_error is not None:
            result["errors"] = [
                {
                    "assetId": str(item.get("asset_id") or ""),
                    "assetVersion": str(item.get("asset_version") or ""),
                    "operationId": str(item.get("operation_id") or ""),
                    "code": int(batch_error.get("code") or 500),
                    "detail": batch_error.get("detail") or "",
                }
                for item in proposals
                if isinstance(item, dict)
            ]
        requested = [
            self._binding_coordinate(binding)
            for binding in (result.get("created") or ())
        ]
        failed = [
            {
                "asset_id": str(error.get("assetId") or ""),
                "operation_id": str(error.get("operationId") or ""),
                "code": int(error.get("code") or 500),
                "detail": error.get("detail") or "",
            }
            for error in (result.get("errors") or ())
        ]
        job.tool_request_report = {
            "requested": requested,
            "failed": failed,
            "skipped_non_read": list(
                result.get("skipped_non_read") or ()
            ),
        }
        # `provisioned_tool_bindings` 는 실패 보상에서 키만 보고 삭제해요. 따라서 여기에는
        # ReadOnly baseline 이 만든 행만 들어가야 해요. 신청 행의 durable marker 는
        # `tool_request_report.requested` 로 따로 남겨 shared-policy 재시도를 결정해요.
        self._checkpoint_provisioning(job)
        return result

    def _provision_shared_policy_for_bindings(
        self,
        job: DeployJob,
        baseline_result: dict | None,
        request_result: dict | None,
    ) -> None:
        """New baseline or request bindings trigger one shared-policy update.

        The injected job seam keeps the provisioner's default 60-poll budget.
        HTTP's 8-poll helper must not enter this path. Failures remain
        non-blocking and are retained in ``shared_policy_report``.
        """
        if self.provision_shared_policy is None:
            return
        baseline_created = bool(
            baseline_result
            and baseline_result.get("created_bindings")
        )
        request_created = bool(
            request_result
            and request_result.get("shared_policy_created")
        )
        request_checkpointed = bool(
            job.tool_request_report
            and job.tool_request_report.get("requested")
        )
        created_this_poll = baseline_created or request_created
        if (
            not created_this_poll
            and job.shared_policy_report is not None
            and job.shared_policy_report.get("ok") is True
        ):
            return
        if (
            not created_this_poll
            and not job.provisioned_tool_bindings
            and not request_checkpointed
        ):
            return
        try:
            job.shared_policy_report = self.provision_shared_policy(job)
        except Exception as exc:
            job.shared_policy_report = {
                "ok": False,
                "verdict": "unknown",
                "reason": (
                    "Agent tool binding 뒤 Gateway 공유 정책 provisioning 에 "
                    f"실패했어요: {type(exc).__name__}: {exc}"
                ),
            }
            _log.warning(
                "binding 뒤 공유 정책 provisioning 실패 job_id=%s failure=%s",
                job.job_id, type(exc).__name__,
            )

    def _record_tool_request_log(
        self,
        job: DeployJob,
    ) -> None:
        if self.record_tool_request is None or job.tool_request_report is None:
            return
        report = job.tool_request_report
        requested = report.get("requested") or ()
        failed = report.get("failed") or ()
        skipped = report.get("skipped_non_read") or ()
        if not requested and not failed and not skipped:
            return
        reasons: list[str] = []
        remediations: list[str] = []
        if failed:
            operations = ", ".join(
                str(item.get("operation_id") or "")
                for item in failed
            )
            reasons.append(
                f"도구 권한 신청 {len(failed)}건을 접수하지 못했어요: "
                f"{operations}"
            )
            remediations.append(
                "요청 상세의 항목별 오류를 확인한 뒤 선언·버전·사유를 고쳐 다시 배포해 주세요."
            )
        if skipped:
            operations = ", ".join(
                str(item.get("operation_id") or "")
                for item in skipped
            )
            reasons.append(
                "서버가 비-READ로 판정한 도구에 신청 사유가 없어 "
                f"접수되지 않았어요: {operations}"
            )
            remediations.append(
                "Initializr에서 해당 도구의 신청 사유를 입력해 다시 배포해 주세요."
            )

        shared_report = job.shared_policy_report or {}
        if (
            requested
            and shared_report
            and shared_report.get("ok") is not True
        ):
            reasons.append(
                "도구 권한 신청은 저장됐지만 공유 Gateway 정책 provisioning을 "
                f"완료하지 못했어요: {shared_report.get('reason') or 'unknown'}"
            )
            remediations.append(
                "원장 신청은 되돌리지 말고 IH-154 공유 정책 수렴 경로로 복구해 주세요."
            )
        if job.phase is DeployPhase.FAILED:
            deploy_error = job.error or {}
            phase = str(deploy_error.get("phase") or "unknown")
            message = str(
                deploy_error.get("message") or "배포가 완료되지 않았어요."
            )
            reasons.append(
                f"배포 검증·완료 경로가 {phase} 단계에서 실패했어요: {message}"
            )
            remediations.append(
                "배포 실패 원인을 해결한 뒤 원장 신청 상태를 확인하고 다시 배포해 주세요."
            )

        error = (
            {
                "reason": " / ".join(reasons),
                "remediation": " / ".join(dict.fromkeys(remediations)),
            }
            if reasons
            else None
        )
        name = (
            str(job.meta.get("name") or "").strip()
            or job.source_ref.asset_id.rsplit("/", 1)[-1]
        )
        count = len(job.meta.get("tool_requests") or ())
        try:
            self.record_tool_request(
                request_id=f"{job.job_id}-tool-request",
                principal=job.principal,
                record_id=job.record_id or "",
                title=f"「{name}」 도구 권한 신청 {count}건",
                status="failed" if error else "succeeded",
                error=error,
            )
        except Exception as exc:
            _log.warning(
                "tool request log write failed job_id=%s failure=%s",
                job.job_id,
                type(exc).__name__,
            )

    def _record_provisioning_result(
        self, job: DeployJob, result: dict | None
    ) -> None:
        if not result:
            return
        created_bindings = tuple(result.get("created_bindings") or ())
        if created_bindings:
            self._merge_provisioned_bindings(job, created_bindings)
        job.provisioned_policy_revision = result.get("policy_revision")
        job.provisioning_policy_outcome = result.get("policy_outcome")
        job.provisioning_policy_id = result.get("policy_id")
        job.provisioning_policy_findings = tuple(
            str(finding) for finding in (result.get("policy_findings") or ())
        )
        job.authorization_verdict = result.get("authorization_verdict")
        self._checkpoint_provisioning(job)

    def _checkpoint_provisioning(self, job: DeployJob) -> None:
        try:
            put_if_phase = getattr(self.store, "put_if_phase", None)
            if put_if_phase is not None:
                stored = self._put_next_revision(
                    job,
                    DeployPhase.PROVISIONING,
                )
                if not stored:
                    raise ConcurrentJobAdvance
            else:
                job.state_revision += 1
                self.store.put(job)
        except (ConcurrentJobAdvance, ProvisioningCheckpointError):
            raise
        except Exception as exc:
            raise ProvisioningCheckpointError(
                f"PROVISIONING progress persistence failed: {exc}"
            ) from exc

    def _agent_descriptors(self, job: DeployJob) -> dict:
        manifest = self.source_store.get_manifest(
            job.source_ref.asset_id, job.source_ref.version)
        # s3_prefix: agent/{asset_id}/{version}/
        s3_prefix = f"agent/{job.source_ref.asset_id}/{job.source_ref.version}/"
        # 카드 본문을 extra에 실어 AgentBinding이 agentCard.inlineContent로 담아요.
        # legacy agent.json도 허용(AgentBinding.validate와 동일 정책) — 순서대로 시도.
        agent_card_text = None
        for card_name in ("agent-card.json", "agent.json"):
            try:
                raw = self.source_store.read_file(
                    job.source_ref.asset_id, job.source_ref.version, card_name)
                agent_card_text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                break
            except Exception:
                continue
        descriptors = get_binding("agent").build_descriptors(
            manifest,
            {"s3_prefix": s3_prefix, "agent_card": agent_card_text},
        )
        mcp_assets = job.mcp_assets
        if mcp_assets is None:
            # 이 필드 도입 전에 저장된 진행 중 job은 기존처럼 policy를 다시 읽어
            # descriptor가 사라지지 않게 해요. 신규 job은 확정 snapshot을 사용해요.
            dependencies = self._agent_dependencies(job.source_ref)
            if dependencies is not None:
                mcp_assets = tuple(dependencies["mcpAssets"])
        if mcp_assets is not None:
            descriptors["agent"]["agoraDependencies"] = {
                "version": 1,
                "mcpAssets": list(mcp_assets),
            }
        descriptors["agent"]["memory"] = {
            **(job.memory_config or {"mode": "DISABLED"}),
            **({"memoryId": job.memory_id} if job.memory_id else {}),
        }
        report = job.verify_report or {}
        observed_manager = report.get("conversation_manager")
        if (
            job.conversation_manager_config is not None
            or observed_manager is not None
        ):
            descriptors["agent"]["conversationManager"] = {
                "declared": job.conversation_manager_config,
                "actual": observed_manager,
                "verdict": (
                    "observed"
                    if job.conversation_manager_config is None
                    else report.get("verdict", "not_run")
                ),
            }
        descriptors["agent"]["builtinTools"] = list(job.builtin_tools)
        descriptors["agent"]["builtinToolResources"] = {
            kind: {
                "id": resource["id"],
                "arn": resource["arn"],
                "status": resource["status"],
                "networkMode": resource["network_mode"],
                **(
                    {"recording_prefix": resource["recording_prefix"]}
                    if resource.get("recording_prefix")
                    else {}
                ),
                "logsConfigured": (
                    resource.get("logs_configured") is True
                    or resource.get("logsConfigured") is True
                ),
            }
            for kind, resource in (job.builtin_resources or {}).items()
        }
        selected_model = job.meta.get("model")
        if isinstance(selected_model, str) and selected_model:
            descriptors["agent"].update(model_metadata(selected_model))
        else:
            existing_model = job.meta.get("_existing_model")
            if (
                isinstance(existing_model, dict)
                and is_model_metadata(
                    existing_model.get("model"),
                    existing_model.get("bedrockModelId"),
                )
            ):
                descriptors["agent"].update(existing_model)
        # AgentCore Runtime 정보를 descriptor에 추가.
        descriptors["agent"]["runtimeArn"] = job.runtime_arn
        if job.runtime_arn:
            binding = {
                "kind": "RUNTIME",
                "runtimeArn": job.runtime_arn,
                "sourceAssetId": job.source_ref.asset_id,
                "sourceVersion": job.source_ref.version,
            }
            if self.deploy_region:
                binding["region"] = self.deploy_region
            if job.runtime_id:
                binding["runtimeId"] = job.runtime_id
            if job.execution_role_arn:
                binding["executionRoleArn"] = job.execution_role_arn
            descriptors["agent"]["executionBinding"] = binding
        if self.cognito.get("authorization_url") and not (
            job.workload_id and job.workload_public_key
        ):
            raise RuntimeError("Runtime workload identity가 생성되지 않았어요.")
        if job.workload_id and job.workload_public_key:
            descriptors["agent"]["workloadIdentity"] = {
                "version": 1,
                "id": job.workload_id,
                "algorithm": "Ed25519",
                "publicKey": job.workload_public_key,
            }
        # A2A invoke endpoint를 구성해 넣어요(실측 스파이크 형식). aws_mapping이 이 값을
        # agentCard.url로 채우고(스키마 0.3 필수), 프론트가 클라이언트 연결 안내에 써요.
        if job.runtime_arn and self.deploy_region:
            descriptors["agent"]["endpoint"] = (
                f"https://bedrock-agentcore.{self.deploy_region}.amazonaws.com"
                f"/runtimes/{job.runtime_arn}/invocations")
        return descriptors

    def _find_replay_record(
        self,
        source_ref: SourceRef,
        name: str,
        principal: str,
    ):
        """fresh PROVISIONING replay가 이미 만든 DRAFT record를 회수해요."""
        try:
            records = self.registry.list_records(
                self.registry_id,
                statuses=(RecordStatus.DRAFT,),
                max_results=1000,
            )
        except AttributeError:
            # 좁은 legacy/test adapter만 list_records가 없어요.
            return None
        matches = [
            record
            for record in records
            if record.name == name
            and record.descriptor_type is DescriptorType.AGENT
            and record.version == source_ref.version
            and record.status is RecordStatus.DRAFT
            and getattr(record, "owner_user", "") == principal
        ]
        return matches[0] if len(matches) == 1 else None

    def _finalize_agent(self, job: DeployJob) -> None:
        """검증 통과 뒤에만 재배포 descriptors/version을 live record에 반영해요."""
        descriptors = self._agent_descriptors(job)
        name = job.meta.get("name") or job.source_ref.asset_id.split("/")[-1]
        if job.redeploy_record_id and not job.reuses_runtime:
            # The remote update can succeed before a readback timeout. From this
            # point onward retain the Runtime rather than risk a dangling binding.
            job.record_bound = True
        self.registry.update_record_descriptors(
            self.registry_id,
            job.redeploy_record_id or job.record_id or "",
            name,
            DescriptorType.AGENT,
            descriptors,
            job.source_ref.version,
            description=job.meta.get("description", ""),
        )

    def _issue_managed_identity(self, record_id: str, job: DeployJob) -> None:
        """관리형 런타임 agent의 OAuth AgentIdentityBinding을 기록해요.

        배포 전에 발급한 agent별 client_id가 OAuthUser Cedar principal이에요.
        binding 기록 실패는 배포 실패로 전파해 Cognito·원장·Runtime이 갈라진 채
        READY로 표시되지 않게 해요.
        """
        if self.identity_issuer is None or not self.agent_exec_role_arn:
            return
        binding = self.identity_issuer.issue_managed_runtime_binding(
            agent_record_id=record_id,
            runtime_role_arn=self.agent_exec_role_arn,
            workload_identity_name=job.workload_id or "",
            client_id=job.oauth_client_id or "",
        )
        if binding is None:
            raise RuntimeError("managed agent identity binding was not recorded")

    def _auto_provision_readonly_bindings(
        self,
        record_id: str,
        *,
        agent_descriptors: dict | None = None,
        recover_interrupted_policy: bool = False,
        resume_policy_revision: int | None = None,
        resume_policy_id: str | None = None,
    ) -> dict | None:
        """배포 직후 ReadOnly 베이스라인 tool binding을 자동 생성해요(IA-30, ADR-0023).

        선언한 MCP 의존성의 READ 태그 operation만 APPROVED+ALLOWED로 자동 승인하고 Cedar
        policy를 컴파일·배포해, 배포 직후 빈 정책(no tool access)으로 admin이 tool을 하나씩
        허용해야 하는 문제를 없애요. CREATE/UPDATE/DELETE·미분류는 admin 상향으로 남겨요.

        identity 도메인을 직접 import하지 않고 shared.deps seam을 lazy import해요
        (`_registry_name_status`와 동일 패턴, 도메인 경계 보존). 프로비저닝 예외는 호출자에게
        전파해 PROVISIONING 실패로 기록해요.
        """
        from ....shared.deps import auto_provision_readonly_tool_bindings

        if agent_descriptors is None and not recover_interrupted_policy:
            return auto_provision_readonly_tool_bindings(record_id)
        return auto_provision_readonly_tool_bindings(
            record_id,
            agent_descriptors=agent_descriptors,
            recover_interrupted_policy=recover_interrupted_policy,
            resume_policy_revision=resume_policy_revision,
            resume_policy_id=resume_policy_id,
        )

    def _compensate_agent_record(self, job: DeployJob) -> list[str]:
        """신규 agent의 PROVISIONING 산출물을 모두 best-effort로 되돌려요."""
        errors: list[str] = []

        def _cleanup(label: str, action) -> bool:
            try:
                action()
                return True
            except Exception as exc:  # noqa: BLE001 - 나머지 보상도 계속해야 해요.
                errors.append(f"{label}: {type(exc).__name__}: {exc}")
                _log.exception(
                    "agent deploy compensation step failed: job_id=%s step=%s",
                    job.job_id,
                    label,
                )
                return False

        def _skip(label: str, reason: str) -> None:
            message = f"{label}: cleanup skipped: {reason}"
            errors.append(message)
            _log.warning(
                "agent deploy compensation step skipped: "
                "job_id=%s step=%s reason=%s",
                job.job_id,
                label,
                reason,
            )

        record_id = job.record_id or ""
        if job.is_redeploy:
            if record_id and (
                job.provisioned_tool_bindings
                or job.provisioned_policy_revision is not None
            ):
                def _rollback_redeploy_authorization() -> None:
                    from ....shared.deps import rollback_agent_provisioning

                    rollback_agent_provisioning(
                        record_id,
                        created_bindings=job.provisioned_tool_bindings,
                        policy_revision=job.provisioned_policy_revision,
                    )

                _cleanup(
                    "redeploy authorization rollback",
                    _rollback_redeploy_authorization,
                )
            return errors
        shared_reason = ""
        compensation_target_pending = False
        update_compensation_target = None
        delete_compensation_target = None
        if record_id:
            claim_exclusive_reference = getattr(
                self.store,
                "claim_exclusive_job_reference_for_record",
                None,
            )
            put_compensation_target = getattr(
                self.store,
                "put_agent_compensation_target",
                None,
            )
            update_compensation_target = getattr(
                self.store,
                "update_agent_compensation_target",
                None,
            )
            delete_compensation_target = getattr(
                self.store,
                "delete_agent_compensation_target",
                None,
            )
            if (
                claim_exclusive_reference is None
                or put_compensation_target is None
                or update_compensation_target is None
                or delete_compensation_target is None
            ):
                shared_reason = (
                    "record exclusivity could not be claimed: deploy job "
                    "store does not support atomic reference claims and "
                    "durable reconciliation targets"
                )
            else:
                try:
                    target_time = self.now()
                    target_created = put_compensation_target(
                        AgentCompensationTarget(
                            job_id=job.job_id,
                            record_id=record_id,
                            oauth_client_id=job.oauth_client_id or "",
                            record_created=job.record_created,
                            oauth_client_created=job.oauth_client_created,
                            created_at=target_time,
                            updated_at=target_time,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    shared_reason = (
                        "record reconciliation target could not be persisted: "
                        f"{type(exc).__name__}: {exc}"
                    )
                else:
                    if not target_created:
                        shared_reason = (
                            f"record {record_id} already has pending "
                            f"compensation for job {job.job_id}"
                        )
                    else:
                        try:
                            claimed = claim_exclusive_reference(
                                record_id,
                                job_id=job.job_id,
                            )
                        except Exception as exc:  # noqa: BLE001
                            compensation_target_pending = True
                            shared_reason = (
                                "record exclusivity could not be claimed: "
                                f"{type(exc).__name__}: {exc}"
                            )
                        else:
                            if claimed:
                                compensation_target_pending = True
                            else:
                                try:
                                    delete_compensation_target(job.job_id)
                                except Exception as exc:  # noqa: BLE001
                                    compensation_target_pending = True
                                    errors.append(
                                        "reconciliation target: "
                                        f"{type(exc).__name__}: {exc}"
                                    )
                                shared_reason = (
                                    f"record {record_id} exclusivity was not "
                                    "proven by an atomic reference claim"
                                )
        delete_client_for_record = (
            getattr(
                self.identity_issuer,
                "delete_managed_runtime_client_for_record",
                None,
            )
            if self.identity_issuer is not None
            else None
        )
        owned_client = bool(
            job.oauth_client_id
            and self.identity_issuer is not None
            and job.oauth_client_created
        )
        authorization_cleanup_failed = False
        client_cleanup_failed = False
        client_cleanup_combined = False
        if record_id:
            if shared_reason:
                _skip("authorization artifacts", shared_reason)
            elif not job.record_created:
                _skip(
                    "authorization artifacts",
                    "registry record was not created by this job",
                )
            elif owned_client and delete_client_for_record is None:
                authorization_cleanup_failed = True
                _skip(
                    "authorization artifacts",
                    "record-aware oauth client cleanup is not supported",
                )
            else:
                def _delete_authorization_artifacts() -> None:
                    from ....shared.deps import (
                        delete_agent_authorization_artifacts,
                    )

                    if owned_client:
                        delete_agent_authorization_artifacts(
                            record_id,
                            managed_client_id=job.oauth_client_id or "",
                            delete_managed_client=lambda client_id: (
                                delete_client_for_record(
                                    record_id,
                                    client_id,
                                )
                            ),
                        )
                    else:
                        delete_agent_authorization_artifacts(
                            record_id,
                            revoke_managed_client=False,
                        )

                client_cleanup_combined = owned_client
                authorization_cleanup_failed = not _cleanup(
                    "authorization artifacts",
                    _delete_authorization_artifacts,
                )
        if job.oauth_client_id and self.identity_issuer is not None:
            if shared_reason:
                _skip("oauth client", shared_reason)
            elif not job.oauth_client_created:
                _skip(
                    "oauth client",
                    "client was not created by this job",
                )
            elif client_cleanup_combined:
                if authorization_cleanup_failed:
                    client_cleanup_failed = True
                    _skip(
                        "oauth client",
                        "authorization cleanup failed",
                    )
            elif delete_client_for_record is None:
                client_cleanup_failed = True
                _skip(
                    "oauth client",
                    "record-aware cleanup is not supported",
                )
            else:
                def _delete_client() -> None:
                    if not delete_client_for_record(
                        record_id,
                        job.oauth_client_id or "",
                    ):
                        raise RuntimeError(
                            "managed client reservation no longer matches"
                        )

                client_cleanup_failed = not _cleanup(
                    "oauth client",
                    _delete_client,
                )
        if record_id:
            if shared_reason:
                _skip("registry record", shared_reason)
            elif not job.record_created:
                _skip(
                    "registry record",
                    "record was not created by this job",
                )
            elif authorization_cleanup_failed:
                _skip(
                    "registry record",
                    "authorization cleanup failed",
                )
            elif client_cleanup_failed:
                _skip(
                    "registry record",
                    "oauth client cleanup failed",
                )
            else:
                def _delete_record() -> None:
                    self.registry.delete_record(self.registry_id, record_id)
                    job.record_id = None

                _cleanup("registry record", _delete_record)
        if compensation_target_pending:
            if errors:
                try:
                    updated = update_compensation_target(
                        job.job_id,
                        errors=tuple(errors),
                        updated_at=self.now(),
                    )
                    if not updated:
                        errors.append(
                            "reconciliation target: pending target disappeared"
                        )
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        f"reconciliation target: {type(exc).__name__}: {exc}"
                    )
                    _log.exception(
                        "agent compensation target update failed: job_id=%s",
                        job.job_id,
                    )
            else:
                try:
                    delete_compensation_target(job.job_id)
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        f"reconciliation target: {type(exc).__name__}: {exc}"
                    )
                    _log.exception(
                        "agent compensation target delete failed: job_id=%s",
                        job.job_id,
                    )
        return errors

    # ── 삭제 정리 ─────────────────────────────────────────────────────
    def teardown_for_record(self, record_id: str) -> None:
        """record_id에 매칭되는 모든 deploy job의 AWS 리소스를 정리해요.

        한 자산에 여러 job(멀티 버전·재배포)이 있을 수 있어, early-return 없이
        전부 순회해요. 각 job은 종류(lambda_arn vs runtime_id)로 분기해요.
        """
        errors: list[str] = []
        for job in self.store.list():
            if (
                job.record_id != record_id
                and job.redeploy_record_id != record_id
            ):
                continue
            runtime_delete_confirmed = not job.runtime_id
            # MCP 배포형: Lambda + Gateway target 정리.
            if job.lambda_arn:
                # IA-69가 record의 모든 job 좌표를 모아 Target을 전부 회수한 뒤
                # 공유 Lambda를 지우는 순서를 맡아요. IA-66은 현재 job 내부 순서만
                # 보장하며 이 loop를 record 단위 teardown으로 재구성하지 않아요.
                target_ids = list(dict.fromkeys(
                    str(target.get("target_id") or "")
                    for target in job.gateway_targets
                    if target.get("target_id")
                ))
                if target_ids:
                    targets_deleted = True
                    for target_id in target_ids:
                        try:
                            self.port.delete_target(
                                self.target_gateway_id,
                                target_id,
                            )
                        except Exception as exc:  # noqa: BLE001 - continue slices.
                            targets_deleted = False
                            errors.append(
                                f"{job.job_id} target {target_id}: "
                                f"{type(exc).__name__}: {exc}"
                            )
                    if targets_deleted:
                        try:
                            self.port.teardown(
                                job.lambda_arn,
                                self.target_gateway_id,
                                None,
                            )
                        except Exception as exc:  # noqa: BLE001 - continue jobs.
                            errors.append(
                                f"{job.job_id} lambda: "
                                f"{type(exc).__name__}: {exc}"
                            )
                    else:
                        errors.append(
                            f"{job.job_id} lambda: retained because "
                            "target deletion is incomplete"
                        )
                else:
                    try:
                        self.port.teardown(
                            job.lambda_arn,
                            self.target_gateway_id,
                            job.target_id,
                        )
                    except Exception as exc:  # noqa: BLE001 - continue jobs.
                        errors.append(
                            f"{job.job_id} runtime: "
                            f"{type(exc).__name__}: {exc}"
                        )
            # agent 배포형: AgentCore Runtime 정리(lambda_arn 없이 runtime_id만 있음).
            elif job.runtime_id:
                try:
                    self.port.delete_runtime(job.runtime_id)
                    runtime_delete_confirmed = True
                except Exception as exc:  # noqa: BLE001 - other jobs still clean up.
                    errors.append(
                        f"{job.job_id} runtime: {type(exc).__name__}: {exc}"
                    )
            identity_resources = (
                (
                    "workload identity",
                    job.workload_identity_name,
                    job.workload_identity_created,
                    job.workload_identity_owner_id,
                    self.port.delete_workload_identity,
                ),
                (
                    "oauth provider",
                    job.oauth_provider_name,
                    job.oauth_provider_created,
                    job.oauth_provider_owner_id,
                    self.port.delete_oauth2_credential_provider,
                ),
            )
            for label, name, created, owner_checkpoint, delete in identity_resources:
                if not name or not created or not owner_checkpoint:
                    continue
                if not runtime_delete_confirmed:
                    errors.append(
                        f"{job.job_id} {label}: runtime deletion not confirmed"
                    )
                    continue
                try:
                    delete(name)
                except Exception as exc:  # noqa: BLE001 - sibling cleanup continues.
                    errors.append(
                        f"{job.job_id} {label}: {type(exc).__name__}: {exc}"
                    )
            if job.execution_role_name and job.execution_role_owner_id:
                if not runtime_delete_confirmed:
                    errors.append(
                        f"{job.job_id} execution role: "
                        "runtime deletion not confirmed"
                    )
                else:
                    try:
                        self.port.delete_agent_execution_role(
                            job.execution_role_name,
                            shared_policy_arn=(
                                job.execution_role_shared_policy_arn or ""
                            ),
                        )
                    except Exception as exc:  # noqa: BLE001 - continue jobs.
                        errors.append(
                            f"{job.job_id} execution role: "
                            f"{type(exc).__name__}: {exc}"
                        )
        if errors:
            raise RuntimeError("; ".join(errors))
