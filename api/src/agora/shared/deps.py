"""DI — 도메인 라우터가 공유하는 백엔드 싱글톤 + 접근자.

라우터는 get_registry() / get_aux() / get_source_store() / get_registry_id() 로만
백엔드에 접근해요 — 직접 어댑터를 생성하지 않아요.

백엔드는 AWS 단일이에요:
  Registry    : AwsRegistryAdapter (us-east-1 AWS Agent Registry / AgentCore)
  SourceStore : S3DynamoSourceStore (S3 + DynamoDB, 서울)

AGORA_TABLE_NAME / AGORA_BUCKET_NAME / AGORA_REGION(us-east-1)이 필요해요
(config.load_config()가 부팅 시 검증). 인프라는 infra/ 의 CatalogStorageStack.

배선은 lazy 예요 — 첫 get_*() 호출 시 _ensure_backends()가 어댑터를 만들어요.
(모듈 import 시점에 즉시 배선하면 테스트 collection 단계에서 AWS를 건드려요.)
"""

from __future__ import annotations

import logging
import pathlib as _pathlib

from .monitoring_traffic import INVOCATION_OUTCOME_SUCCESS
from .sensitivity_movement import (
    RedeploySensitivityDecision,
    RedeploySensitivityRequest,
)
from ..domains.catalog.aux_dual_read import DualReadAuxStore
from ..domains.catalog.aux_dynamo_store import DynamoAuxStore
from ..domains.catalog.aux_store import AuxStore
from ..domains.catalog.registry.seed import REGISTRY_NAME
from ..domains.identity.models import filter_platform_roles
from .config import (
    cognito_discovery_url_from_pool_id,
    external_oauth_return_url,
    load_config,
)

# 로컬 영속화 파일의 리포 루트 기준.
_ROOT = _pathlib.Path(__file__).resolve().parent.parent.parent.parent
_LOG = logging.getLogger(__name__)


def _build_backends():
    """AWS 어댑터 튜플 (registry_adapter, registry_id, source_store, aux)를 생성해요.

    aux 인스턴스를 여기서 한 번만 만들어, AwsRegistryAdapter에 주입하는 인스턴스와
    get_aux()가 돌려주는 인스턴스가 반드시 같도록 해요(병합 일관성의 전제).

    **Dynamo primary + JSON fallback으로 감싸요.** Dynamo만 읽게 하면 아직 이관되지 않은
    `.agora-aux.json`의 tags·category·원본 descriptors가 조회 시점에 사라져요(MCP tool 목록·
    skill markdown 무손실 왕복이 ADR-011 결정 3의 근거). 이관 스크립트
    (`api/scripts/migrate_aux_to_dynamo.py`)를 돌린 뒤에도 래퍼는 그대로 둬요 — miss일 때만
    폴백하므로 비용이 없고, JSON 파일이 없는 새 배포에서도 조용히 동작해요.

    쓰기는 Dynamo로만 가요. 양쪽에 쓰면 두 소스가 갈라지고 JSON이 계속 커져요.
    """
    config = load_config()
    aux = DualReadAuxStore(
        primary=DynamoAuxStore(
            table_name=config.table_name,
            region=config.source_region,
        ),
        fallback=AuxStore(store_path=_ROOT / ".agora-aux.json"),
    )

    from ..domains.catalog.registry.aws_adapter import AwsRegistryAdapter
    from ..domains.catalog.sourcestore.dynamo_store import S3DynamoSourceStore

    adapter = AwsRegistryAdapter(
        region=config.region,
        registry_id=config.registry_id or "",
        namespace=config.registry_namespace,
        aux=aux,
    )
    # registry_id 미지정 시 이름으로 조회/생성(네트워크). 지정 시 그대로 재사용.
    registry_id = config.registry_id or adapter.create_registry(REGISTRY_NAME).rsplit("/", 1)[-1]
    adapter.registry_id = registry_id
    # 소스스토어(실물)는 source_region — 메타=us-east-1 Registry / 실물=서울 분리.
    source_store = S3DynamoSourceStore(
        bucket=config.bucket_name,
        table_name=config.table_name,
        region=config.source_region,
    )
    return adapter, registry_id, source_store, aux


# lazy 싱글톤 — 첫 접근 시 배선.
_adapter = None
_registry_id = None
_source_store = None
_aux = None
_backends_ready = False
_source_store_injected = False   # set_source_store로 주입된 적이 있으면 실 store로 안 덮어써요.
_monitoring_aggregate_reader = None


def get_monitoring_aggregate_reader():
    global _monitoring_aggregate_reader
    if _monitoring_aggregate_reader is None:
        config = load_config()
        if not (
            config.monitoring_aggregate_table
            and config.monitoring_ingest_dlq_url
        ):
            from ..domains.monitoring.aggregate import DisabledAggregateReader
            _monitoring_aggregate_reader = DisabledAggregateReader()
        elif not config.monitoring_ingest_owner:
            from ..domains.monitoring.aggregate import NonOwnerAggregateReader
            _monitoring_aggregate_reader = NonOwnerAggregateReader()
        else:
            from ..domains.monitoring.aggregate import DynamoAggregateReader
            _monitoring_aggregate_reader = DynamoAggregateReader(
                table_name=config.monitoring_aggregate_table,
                region=config.source_region,
                dlq_url=config.monitoring_ingest_dlq_url,
                max_lag_seconds=config.monitoring_max_ingest_lag_seconds,
            )
    return _monitoring_aggregate_reader


def set_monitoring_aggregate_reader(reader) -> None:
    global _monitoring_aggregate_reader
    _monitoring_aggregate_reader = reader


def _ensure_backends():
    """첫 접근 시 registry/aux를 실 배선해요. source_store는 이미 주입됐으면 보존.

    set_source_store()가 _ensure_backends 이전에 불릴 수 있어서(lazy 순서 의존성),
    주입된 source는 여기서 덮어쓰지 않아요. 주입 자체는 아무 배선도 트리거하지 않고요
    (registry monkeypatch 테스트가 실 AWS를 건드리지 않도록).
    """
    global _adapter, _registry_id, _source_store, _aux, _backends_ready
    if not _backends_ready:
        adapter, registry_id, source_store, aux = _build_backends()
        _adapter, _registry_id, _aux = adapter, registry_id, aux
        if not _source_store_injected:
            _source_store = source_store
        _backends_ready = True


def backend_mode() -> dict:
    """현재 백엔드 요약 — 콘솔 배지·부팅 로그·진단용. AWS 단일이에요."""
    import os
    _ensure_backends()
    return {
        "scanner": os.getenv("AGORA_SCANNER", "static"),
        "registry_adapter": type(_adapter).__name__,
        "source_store": type(_source_store).__name__,
    }


def log_backend_mode() -> None:
    """부팅 시 현재 모드를 stderr에 한 줄 찍어요."""
    import sys
    m = backend_mode()
    print(
        f"[agora] scanner={m['scanner']} "
        f"registry={m['registry_adapter']} source={m['source_store']}",
        file=sys.stderr,
    )


def get_registry():
    """카탈로그 SoT 어댑터(RegistryPort 구현)."""
    _ensure_backends()
    return _adapter


def get_registry_id() -> str:
    """단일 레지스트리 ID."""
    _ensure_backends()
    return _registry_id


def find_catalog_record(record_id: str):
    """카탈로그 레코드 단건 조회. 없는 ID는 None으로 정규화해요."""
    from ..domains.catalog.registry.models import RecordNotFound

    try:
        return get_registry().get_record(get_registry_id(), record_id)
    except RecordNotFound:
        return None


def get_catalog_endpoint(descriptors: dict) -> str:
    """카탈로그 descriptor의 호출 endpoint를 공용 계약으로 해석해요."""
    from ..domains.catalog.registry.models import endpoint_of_descriptors

    return endpoint_of_descriptors(descriptors)


def get_aux():
    """부가 데이터(조회수·확장메타) 스토어.

    실배선은 `DualReadAuxStore`(Dynamo primary + JSON fallback)이지만, 테스트는 JSON
    `AuxStore`를 직접 주입해요. 세 구현이 같은 공개 계약을 만족하므로 반환 타입을 한 클래스로
    좁히지 않아요(좁히면 주입 지점마다 타입이 어긋나요).
    """
    _ensure_backends()
    return _aux


def get_source_store():
    """소스 스토어(SourceStorePort 구현)."""
    _ensure_backends()
    return _source_store


def set_source_store(store) -> None:
    """테스트/배포에서 SourceStorePort 구현을 주입해요.

    배선을 트리거하지 않고 주입 사실만 표시해요. 이후 _ensure_backends()가 돌아도
    registry/aux만 실 배선하고 여기 주입한 store는 그대로 보존해요.
    """
    global _source_store, _source_store_injected
    _source_store = store
    _source_store_injected = True


# --- 거버넌스 콘솔 (도구·등급·설정·스캔) ---
_GOV_PATH = _ROOT / ".agora-gov.json"
_gov_store = None
_gov_seeded_ref = None   # seed_tiers를 실행한 store 객체(내가 만든 store만 대상)


def _create_gov_store():
    cfg = load_config()
    # HP-04: 테이블이 설정되면 stage 무관 항상 DynamoGovStore(공유). 로컬·배포 모두 같은 스토어를
    # 쓰게 해 "로컬 OK·배포 깨짐" 버그를 없애요. 예전엔 stage==prod까지 요구해 dev로 도는
    # 배포 포털이 gov_table이 있어도 로컬 JSON으로 떨어졌어요(#69의 잔여 함정).
    if cfg.gov_table:
        from ..domains.governance.dynamo_store import DynamoGovStore
        # GovTable은 스캔 인프라(scan-tools 스택)와 같은 scan_region(서울)에 배포돼요.
        # cfg.region(us-east-1=레지스트리)을 쓰면 테이블을 못 찾아요.
        store = DynamoGovStore(table_name=cfg.gov_table, region=cfg.scan_region)
    else:
        from ..domains.governance.store import GovStore
        store = GovStore(store_path=_GOV_PATH)
    # 이 함수가 만든 store만 seed 대상임을 표시(외부 주입 store와 구분).
    store._agora_self_created = True
    return store


def get_gov_store(*, seed: bool = True):
    """거버넌스 콘솔 데이터 스토어. prod+gov_table이면 DynamoGovStore(공유), 아니면 GovStore(JSON).

    seed_tiers는 멱등 upsert(write 포함)라, 기본은 기동/거버넌스 경로에서 tier matrix를
    보강해요. 다만 순수 read 경로(예: 자산 상세의 trust 요약)는 seed=False로 호출해
    read 요청이 governance write를 유발하지 않게 해요.

    seed는 **이 함수가 새로 만든 store**에만 적용해요(생성 store는 `_agora_self_created`로
    표시). 외부에서 `_gov_store`에 직접 주입한 store(테스트 `_setup`·배포)는 baseline
    동작대로 절대 seed하지 않아요 — 주입자가 tier 상태를 직접 소유하거든요.
    `_gov_seeded_ref`가 seed한 그 store 객체를 가리켜 프로세스당 한 번만 seed해요.
    """
    global _gov_store, _gov_seeded_ref
    if _gov_store is None:
        _gov_store = _create_gov_store()
    if (
        seed
        and _gov_seeded_ref is not _gov_store
        and getattr(_gov_store, "_agora_self_created", False)
    ):
        from ..domains.governance.seed import seed_tiers
        seed_tiers(_gov_store)   # 멱등 reconcile — 두 구현 공통 시그니처
        _gov_seeded_ref = _gov_store
    return _gov_store


def get_governance_monitoring_snapshots(records) -> dict[str, dict]:
    """Read-only, metadata-only governance projections for admin monitoring."""
    from ..domains.governance.monitoring import governance_monitoring_snapshots

    return governance_monitoring_snapshots(
        records,
        get_gov_store(seed=False),
    )


_scan_log_reader = None


def get_scan_log_reader():
    """게이트 스캔 로그 리더(CloudWatch). scan_region·stage로 배선. lazy 싱글톤."""
    global _scan_log_reader
    if _scan_log_reader is None:
        from ..domains.governance.scan_logs import ScanLogReader
        cfg = load_config()
        # 로컬 prod-mode(cognito 로그인)에서 스캔 도구 리소스는 dev 계정에 -dev 접미사로
        # 배포돼 있어요. AGORA_SCAN_TOOLS_STAGE(cfg.scan_tools_stage)로 로그그룹 조회 stage만
        # 따로 오버라이드해 실제 리소스(agora-tool-*-dev / scan-tool/semgrep-dev)에 맞춰요.
        # 미지정 시 cfg.stage를 그대로 써요.
        scan_stage = cfg.scan_tools_stage or cfg.stage
        _scan_log_reader = ScanLogReader(region=cfg.scan_region, stage=scan_stage)
    return _scan_log_reader


def set_scan_log_reader(reader) -> None:
    global _scan_log_reader
    _scan_log_reader = reader


_runtime_log_reader = None


def get_runtime_log_reader():
    """배포 agent 런타임 로그 리더(CloudWatch). lazy 싱글톤.

    리전은 deploy_region이에요 — AgentCore Runtime이 거기 배포되니까요.
    스캔 리더(scan_region)와 기본값이 같아도 의미가 달라 명시적으로 구분해요.
    """
    global _runtime_log_reader
    if _runtime_log_reader is None:
        from ..domains.playground.runtime_logs import AgentRuntimeLogReader
        cfg = load_config()
        _runtime_log_reader = AgentRuntimeLogReader(region=cfg.deploy_region)
    return _runtime_log_reader


def set_runtime_log_reader(reader) -> None:
    global _runtime_log_reader
    _runtime_log_reader = reader


# --- Identity P1 (Connection·AccessGrant·Delegation·Audit) ---
_identity_store = None
_delegation_service = None
_authorization_service = None
_workload_token_verifier = None
_cognito_user_directory = None
_user_management_service = None
_credential_broker = None
_agent_policy_service = None
_agent_policy_deployer = None
_gateway_policy_console = None
_domain_policy_service = None
_agent_policy_scope_observer = None
_agent_identity_issuer = None
_dev_identity_service = None
_dev_identity_retry_actor_available = None
_mcp_reregistration_cleanup = None


def get_identity_store():
    """Identity P1 단일 DynamoDB 스토어."""
    global _identity_store
    if _identity_store is None:
        from ..domains.identity.dynamo_store import DynamoIdentityStore

        cfg = load_config()
        if not cfg.identity_table:
            raise RuntimeError("AGORA_IDENTITY_TABLE이 필요해요.")
        _identity_store = DynamoIdentityStore(
            table_name=cfg.identity_table,
            region=cfg.identity_region,
        )
    return _identity_store


def set_identity_store(store) -> None:
    """테스트에서 IdentityStore를 주입하고 캡처한 서비스를 함께 리셋해요."""
    global _identity_store, _delegation_service, _authorization_service
    global _workload_assertion_verifier
    global _user_management_service
    global _agent_policy_service, _agent_policy_scope_observer
    global _dev_identity_service
    global _mcp_reregistration_cleanup
    global _domain_policy_service
    _identity_store = store
    _delegation_service = None
    _authorization_service = None
    _workload_assertion_verifier = None
    _user_management_service = None
    _agent_policy_service = None
    _agent_policy_scope_observer = None
    _dev_identity_service = None
    _mcp_reregistration_cleanup = None
    # 도메인 규칙 서비스도 store 를 캡처해요 — 안 지우면 옛 store 를 계속 써요.
    _domain_policy_service = None


def get_mcp_reregistration_cleanup():
    """Catalog collision observation plus Identity-owned unknown audit."""
    global _mcp_reregistration_cleanup
    if _mcp_reregistration_cleanup is None:
        from ..domains.catalog.mcp.approval_cleanup import (
            McpReregistrationApprovalCleanup,
        )

        _mcp_reregistration_cleanup = McpReregistrationApprovalCleanup(
            get_registry(),
            get_registry_id(),
            _new_asset_capability_cleaner(),
        )
    return _mcp_reregistration_cleanup


def _new_asset_capability_cleaner():
    import uuid
    from datetime import datetime, timezone

    from ..domains.identity.asset_capability_cleanup import (
        IdentityAssetCapabilityCleaner,
    )

    return IdentityAssetCapabilityCleaner(
        get_identity_store(),
        now=lambda: datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        new_id=lambda: uuid.uuid4().hex,
    )


def _new_access_grant_cleaner():
    """⑦ access grant 삭제·감사 — identity 소유 구현이에요.

    ⑤ cleaner 와 같은 모양으로 배선해요. 미주입이면 purge 가 그 단계를 `skipped` 로 남기고
    ⑦ 잔재가 조용히 살아남으니, **프로덕션 그래프에 반드시 들어가야 해요**(테스트가 대조해요).
    """
    import uuid
    from datetime import datetime, timezone

    from ..domains.identity.access_grant_cleanup import (
        IdentityAccessGrantCleaner,
    )

    return IdentityAccessGrantCleaner(
        get_identity_store(),
        now=lambda: datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        new_id=lambda: uuid.uuid4().hex,
    )


def get_cognito_user_directory():
    """Human Cognito user directory. dev/test는 hermetic fake를 사용해요."""
    global _cognito_user_directory
    if _cognito_user_directory is None:
        from ..domains.identity.user_directory import (
            AwsCognitoUserDirectory,
            FakeCognitoUserDirectory,
        )

        cfg = load_config()
        if cfg.auth_mode in ("dev", "test"):
            _cognito_user_directory = FakeCognitoUserDirectory()
        else:
            from .config import cognito_pool_location

            if not cfg.auth_cognito_issuer:
                raise RuntimeError("AGORA_AUTH_COGNITO_ISSUER가 필요해요.")
            region, pool_id = cognito_pool_location(cfg.auth_cognito_issuer)
            _cognito_user_directory = AwsCognitoUserDirectory(
                user_pool_id=pool_id,
                region=region,
            )
    return _cognito_user_directory


def set_cognito_user_directory(directory) -> None:
    """테스트에서 CognitoUserDirectoryPort 구현을 주입해요."""
    global _cognito_user_directory, _user_management_service
    _cognito_user_directory = directory
    _user_management_service = None


def get_user_management_service():
    global _user_management_service
    if _user_management_service is None:
        from ..domains.identity.users_service import UserManagementService

        _user_management_service = UserManagementService(
            get_cognito_user_directory(),
            get_identity_store(),
        )
    return _user_management_service


def get_delegation_service():
    global _delegation_service
    if _delegation_service is None:
        from ..domains.identity.delegation import DelegationService

        _delegation_service = DelegationService(get_identity_store())
    return _delegation_service


def get_delegated_asset_ids(descriptors: dict) -> tuple[str, ...]:
    """Agent descriptor에 고정된 MCP 자산 allowlist를 읽어요."""
    from ..domains.identity.delegation import delegated_asset_ids

    return delegated_asset_ids(descriptors)


def enforce_agent_invoke_gate(
    *,
    agent_id: str,
    owner_user: str,
    caller_principal_id: str,
    caller_groups: tuple[str, ...],
) -> None:
    """agent 호출 자격 게이트(§4.8) — 비인가 호출을 delegation 발급 전에 차단해요.

    Playground 등 identity 밖 도메인이 도메인 간 직접 import 없이 같은 판정을 쓰도록
    identity 헬퍼를 이 accessor로 감싸요. 소유자·allowlist·group만 통과, 그 외 403.
    """
    from ..domains.identity.access_router import enforce_agent_invoke_gate as _gate

    _gate(
        agent_id=agent_id,
        owner_user=owner_user,
        caller_principal_id=caller_principal_id,
        caller_groups=caller_groups,
    )


def record_agent_invoke_audit(
    *,
    invocation_id: str,
    principal_id: str,
    agent_id: str,
    mode: str = "OAUTH_GATEWAY",
    invocation_outcome: str = INVOCATION_OUTCOME_SUCCESS,
) -> None:
    """Append an ALLOW audit event with the Runtime invocation outcome.

    Wraps the identity helper so non-identity domains (Playground) record a
    queryable invoke audit without a direct cross-domain import (finding #5).
    """
    from ..domains.identity.access_router import (
        record_agent_invoke_audit as _record,
    )

    _record(
        invocation_id=invocation_id,
        principal_id=principal_id,
        agent_id=agent_id,
        mode=mode,
        invocation_outcome=invocation_outcome,
    )


def observe_agent_invoke_traffic(
    *,
    from_time: str,
    to_time: str,
):
    """Observe successful agent calls from the identity-owned audit timeline."""
    from .monitoring_traffic import (
        INVOCATION_OUTCOME_FAILURE,
        TrafficObservation,
    )

    cursor = None
    invocation_count = 0
    invoked_agent_ids: set[str] = set()
    failed_invocation_observed = False
    missing_outcome_observed = False
    coverage_reason = None
    try:
        while True:
            page = get_identity_store().list_audit_timeline(
                from_time=from_time if cursor is None else None,
                to_time=to_time if cursor is None else None,
                limit=200 if cursor is None else None,
                cursor=cursor,
            )
            for event in page.items:
                if (
                    event.operation_id != "agent-invoke"
                    or getattr(event.decision, "value", event.decision)
                    != "ALLOW"
                ):
                    continue
                outcome = getattr(event, "invocation_outcome", None)
                if outcome == INVOCATION_OUTCOME_SUCCESS:
                    invocation_count += 1
                    agent_id = getattr(event, "agent_id", "") or ""
                    if agent_id:
                        invoked_agent_ids.add(agent_id)
                elif outcome == INVOCATION_OUTCOME_FAILURE:
                    failed_invocation_observed = True
                else:
                    missing_outcome_observed = True
            if page.coverage.status != "ok":
                coverage_reason = page.coverage.reason or "coverage_unknown"
            cursor = page.next_cursor
            if cursor is None:
                break
    except (RuntimeError, ValueError):
        return TrafficObservation(
            status="unknown",
            invocation_count=None,
            reason="audit_query_unavailable",
        )

    if invocation_count > 0:
        return TrafficObservation(
            status="ok",
            invocation_count=invocation_count,
            agent_ids=tuple(sorted(invoked_agent_ids)),
        )
    if coverage_reason is not None:
        return TrafficObservation(
            status="unknown",
            invocation_count=None,
            reason=coverage_reason,
        )
    if missing_outcome_observed:
        return TrafficObservation(
            status="unknown",
            invocation_count=None,
            reason="invoke_outcome_unobserved",
        )
    if failed_invocation_observed:
        return TrafficObservation(
            status="unknown",
            invocation_count=None,
            reason="no_successful_traffic_observed",
        )
    # 조회 창은 이미 양끝 마진으로 잘라 **정착된 구간**이고 coverage 도 완전해요.
    # 그 안에서 성공한 호출이 0건인 건 관측 실패가 아니라 관측된 사실이에요. 여기서
    # '증명 불가'로 닫으면 마진을 둔 의미가 없어지고, MO-36 이 없애려던 "호출이
    # 없었는데 값이 사라지는" 상태가 그대로 남아요.
    return TrafficObservation(status="ok", invocation_count=0)


def record_agent_invoke_usage(
    *,
    invocation_id: str,
    principal_id: str,
    agent_id: str,
    session_id: str,
    usage: dict,
) -> None:
    """Persist body-free invoke usage through the identity-owned store."""
    from ..domains.identity.access_router import (
        record_agent_invoke_usage as _record,
    )

    _record(
        invocation_id=invocation_id,
        principal_id=principal_id,
        agent_id=agent_id,
        session_id=session_id,
        usage=usage,
    )


def get_current_principal(request):
    """Return the middleware-verified principal without cross-domain imports."""
    from ..domains.identity.context import current_principal

    return current_principal(request)


def get_asset_responsibility_port():
    """Catalog-owned responsibility module assembled behind a shared interface."""
    from ..domains.catalog.responsibility import CatalogAssetResponsibility

    return CatalogAssetResponsibility(
        get_registry(),
        get_registry_id(),
        get_aux(),
    )


def auto_provision_readonly_tool_bindings(
    agent_record_id: str, *, agent_descriptors: dict | None = None,
    recover_interrupted_policy: bool = False,
    resume_policy_revision: int | None = None,
    resume_policy_id: str | None = None,
) -> dict:
    """배포 직후 선언 MCP 의존성의 READ operation을 ReadOnly 베이스라인으로 자동 승인해요(IA-30).

    runtime(배포) 도메인이 identity 도메인을 직접 import하지 않도록 이 accessor로 감싸요
    (record_agent_invoke_audit seam과 동일). 실패는 여기서 잡지 않고 호출부(deploy)가
    fail-open으로 처리해요 — 자동 프로비저닝 실패가 배포를 되돌리면 안 돼요.
    """
    from ..domains.identity.access_router import (
        auto_provision_readonly_tool_bindings as _provision,
    )

    return _provision(
        agent_record_id,
        agent_descriptors=agent_descriptors,
        recover_interrupted_policy=recover_interrupted_policy,
        resume_policy_revision=resume_policy_revision,
        resume_policy_id=resume_policy_id,
    )


def submit_agent_tool_requests(
    agent_record_id: str,
    proposals,
    *,
    principal_id: str,
    agent_descriptors: dict | None = None,
) -> dict:
    """Submit deploy-owned tool requests behind the runtime/identity seam."""
    from ..domains.identity.access_router import (
        submit_agent_tool_requests as _submit,
    )

    return _submit(
        agent_record_id,
        proposals,
        principal_id=principal_id,
        agent_descriptors=agent_descriptors,
    )


def record_agent_tool_request(
    *,
    request_id: str,
    principal: str,
    record_id: str,
    title: str,
    status: str,
    error: dict | None,
) -> str:
    """Write one terminal tool-request row without exposing catalog types."""
    from ..domains.catalog.requests.models import RequestKind, RequestStatus
    from ..domains.catalog.router import _log_request

    return _log_request(
        RequestKind.TOOL_REQUEST,
        title,
        principal,
        record_id=record_id,
        status=RequestStatus(status),
        error=error,
        request_id=request_id,
    )


def delete_agent_authorization_artifacts(
    agent_record_id: str,
    *,
    managed_client_id: str = "",
    fallback_managed_client_id: str = "",
    delete_managed_client=None,
    revoke_managed_client: bool = True,
) -> None:
    """Agent의 Cedar·Cognito client·identity/tool/policy 원장을 모두 지워요."""
    if not revoke_managed_client:
        get_agent_policy_service().delete_agent_artifacts(agent_record_id)
        return
    if delete_managed_client is None:
        def delete_managed_client(client_id: str) -> bool:
            return (
                get_agent_identity_issuer()
                .delete_managed_runtime_client_for_record(
                    agent_record_id,
                    client_id,
                )
            )
    delete_kwargs = {
        "delete_managed_client": delete_managed_client,
        "managed_client_id": managed_client_id,
    }
    if fallback_managed_client_id:
        delete_kwargs["fallback_managed_client_id"] = (
            fallback_managed_client_id
        )
    get_agent_policy_service().delete_agent_artifacts(
        agent_record_id,
        **delete_kwargs,
    )


def reclaim_orphan_agent_authorization_artifacts(
    agent_record_id: str,
) -> None:
    """Catalog 부재를 다시 확인한 뒤 고아 Agent 인가를 명시적으로 회수해요."""
    from ..domains.catalog.registry.models import RecordNotFound

    try:
        get_registry().get_record(get_registry_id(), agent_record_id)
    except RecordNotFound:
        pass
    else:
        raise RuntimeError(
            f"catalog record exists; refusing orphan reclaim: {agent_record_id}"
        )
    delete_agent_authorization_artifacts(agent_record_id)


def rollback_agent_provisioning(
    agent_record_id: str, *, created_bindings: tuple[dict, ...],
    policy_revision: int | None,
) -> None:
    """실패한 재배포가 추가한 baseline binding/policy revision만 되돌려요."""
    get_agent_policy_service().rollback_provisioning(
        agent_record_id,
        created_bindings=created_bindings,
        policy_revision=policy_revision,
    )


def get_delegated_workload_binding(descriptors: dict) -> tuple[str, str]:
    """Agent descriptor의 Runtime workload ID와 공개키를 읽어요."""
    from ..domains.identity.delegation import delegated_workload_binding

    return delegated_workload_binding(descriptors)


def get_authorization_service():
    global _authorization_service
    if _authorization_service is None:
        from ..domains.catalog.registry.models import RecordStatus
        from ..domains.identity.authorization import AuthorizationService

        def asset_version(asset_id: str) -> str | None:
            try:
                record = get_registry().get_record(get_registry_id(), asset_id)
            except Exception:
                return None
            if record.status is not RecordStatus.APPROVED:
                return None
            return record.version

        _authorization_service = AuthorizationService(
            get_identity_store(), asset_version=asset_version
        )
    return _authorization_service


def get_agent_policy_service():
    """AgentPolicyService — AgentToolBinding을 Cedar policy로 컴파일·기록. lazy 싱글톤."""
    global _agent_policy_service
    if _agent_policy_service is None:
        from ..domains.identity.agent_policy_service import AgentPolicyService

        _agent_policy_service = AgentPolicyService(
            get_identity_store(),
            declaration_resolver=lambda agent_id: get_registry().get_record(
                get_registry_id(), agent_id
            ).descriptors,
            deployer_factory=get_agent_policy_deployer,
            deployer_configured=lambda: bool(
                load_config().m2_oauth_policy_engine_arn
                if load_config().authorization_mode == "agent_policy"
                else load_config().m2_policy_engine_id
            ),
            scope_observer=get_agent_policy_scope_observer(),
        )
    return _agent_policy_service


def set_agent_policy_service(svc) -> None:
    """테스트에서 AgentPolicyService(또는 fake)를 주입해요. None이면 다음 호출 때 재배선."""
    global _agent_policy_service
    _agent_policy_service = svc


def get_agent_policy_scope_observer():
    """Complete Cognito resource-server scope inventory, observed on demand."""
    global _agent_policy_scope_observer
    if _agent_policy_scope_observer is None:
        import boto3

        from ..domains.identity.agent_policy_scope import (
            CognitoScopeObserver,
        )

        cfg = load_config()
        region = cfg.deploy_region
        _agent_policy_scope_observer = CognitoScopeObserver(
            None,
            user_pool_id=cfg.m2_oauth_user_pool_id or "",
            client_factory=lambda: boto3.client(
                "cognito-idp",
                region_name=region,
            ),
        )
    return _agent_policy_scope_observer


def set_agent_policy_scope_observer(observer) -> None:
    """Inject or reset Cognito scope observation for tests."""
    global _agent_policy_scope_observer, _agent_policy_service
    _agent_policy_scope_observer = observer
    _agent_policy_service = None


def _policy_engine_id(value: str) -> str:
    """policyEngineId는 12~59자 짧은 ID예요(IA-31). config가 ARN
    (arn:...:policy-engine/<id>)이면 마지막 세그먼트(ID)만 반환해요 — ARN을 통째로 넘기면
    "length between 12 and 59" ValidationException으로 정책 배포가 실패해요."""
    return value.rsplit("/", 1)[-1] if value.startswith("arn:") else value


def get_agent_policy_deployer():
    """AgentPolicyDeployer — compiler 산출물을 M2 Policy Engine에 배포. lazy 싱글톤."""
    global _agent_policy_deployer
    if _agent_policy_deployer is None:
        from ..domains.identity.agent_policy_deployer import (
            AgentPolicyDeployer,
            BotoPolicyEngineClient,
        )
        from .config import load_config
        cfg = load_config()
        engine_id = (
            cfg.m2_oauth_policy_engine_arn
            if cfg.authorization_mode == "agent_policy"
            else cfg.m2_policy_engine_id
        ) or ""
        if not engine_id:
            raise RuntimeError(
                "AGORA_M2_OAUTH_POLICY_ENGINE_ARN is not set; "
                "deploy the M2 OAuth gateway stack first"
            )
        # IA-31: ARN이면 짧은 ID로 정규화(create_policy policyEngineId는 12~59자).
        engine_id = _policy_engine_id(engine_id)
        import boto3
        boto_client = boto3.client(
            "bedrock-agentcore-control", region_name=cfg.deploy_region
        )
        _agent_policy_deployer = AgentPolicyDeployer(
            BotoPolicyEngineClient(boto_client),
            get_identity_store(),
            engine_id=engine_id,
            cognito_client=boto3.client(
                "cognito-idp", region_name=cfg.deploy_region
            ),
            user_pool_id=cfg.m2_oauth_user_pool_id or "",
        )
    return _agent_policy_deployer


def set_agent_policy_deployer(svc) -> None:
    """테스트에서 AgentPolicyDeployer(또는 fake)를 주입해요. None이면 다음 호출 때 재배선."""
    global _agent_policy_deployer, _dev_identity_service
    _agent_policy_deployer = svc
    _dev_identity_service = None


def get_gateway_policy_console():
    """GatewayPolicyConsole — Gateway 를 출발점으로 라이브 Cedar 정책을 훑어요.

    `get_agent_policy_deployer` 와 달리 엔진을 config 로 고정하지 않아요. 관리자 화면은
    모든 Gateway 를 봐야 하고, 원장에 없는 정책(옛 PoC·컷오버 잔재)이 바로 그 화면이
    찾아야 하는 대상이거든요.
    """
    global _gateway_policy_console
    if _gateway_policy_console is None:
        import boto3

        from ..domains.identity.policy_console import GatewayPolicyConsole
        from .config import load_config
        cfg = load_config()
        _gateway_policy_console = GatewayPolicyConsole(
            boto3.client(
                "bedrock-agentcore-control", region_name=cfg.deploy_region
            )
        )
    return _gateway_policy_console


def set_gateway_policy_console(svc) -> None:
    """테스트에서 fake 를 주입해요. None 이면 다음 호출 때 재배선."""
    global _gateway_policy_console
    _gateway_policy_console = svc


def _list_mcp_assets_for_domain_policy():
    """도메인 규칙 폼이 고를 수 있는 MCP 자산을 (id, version, name, descriptors) 로 줘요.

    identity 도메인이 catalog 를 직접 import 하지 않도록 여기서 주입해요(AGENTS.md 의 도메인
    경계). APPROVED 만 봐요 — 승인되지 않은 자산의 도구는 Gateway Target 이 없어서 Cedar
    action 이름을 만들 수 없어요.
    """
    from ..domains.catalog.registry.models import DescriptorType, RecordStatus

    records = get_registry().list_records(
        get_registry_id(),
        statuses=(RecordStatus.APPROVED,),
        max_results=None,
    )
    return [
        (record.record_id, record.version, record.name, record.descriptors)
        for record in records
        if record.descriptor_type is DescriptorType.MCP
    ]


def get_domain_policy_service():
    """DomainPolicyService — 도메인 규칙 Cedar 정책의 CRUD (IH-132).

    좌표(Gateway·policy engine)는 **config 에서만** 읽어요. 클라이언트가 Gateway 를 고를 수
    있으면 인가 대상 자원을 클라이언트가 정하는 셈이에요. 대상 Gateway 는 하나예요.
    """
    global _domain_policy_service
    if _domain_policy_service is None:
        import boto3

        from ..domains.identity.domain_policy import (
            DomainPolicyService,
            GatewayCoordinates,
        )
        from .config import load_config
        cfg = load_config()
        gateway_arn = (cfg.m2_oauth_gateway_arn or "").strip()
        gateway_id = (cfg.m2_oauth_gateway_id or "").strip()
        engine_arn = (cfg.m2_oauth_policy_engine_arn or "").strip()
        missing = [
            name
            for name, value in (
                ("AGORA_M2_OAUTH_GATEWAY_ARN", gateway_arn),
                ("AGORA_M2_OAUTH_GATEWAY_ID", gateway_id),
                ("AGORA_M2_OAUTH_POLICY_ENGINE_ARN", engine_arn),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                "도메인 정책 좌표가 없어요: " + ", ".join(missing)
            )
        _domain_policy_service = DomainPolicyService(
            control_client=boto3.client(
                "bedrock-agentcore-control", region_name=cfg.deploy_region
            ),
            store=get_identity_store(),
            coordinates=GatewayCoordinates(
                gateway_arn=gateway_arn,
                gateway_id=gateway_id,
                engine_id=_policy_engine_id(engine_arn),
            ),
            list_mcp_assets=_list_mcp_assets_for_domain_policy,
        )
    return _domain_policy_service


def set_domain_policy_service(svc) -> None:
    """테스트에서 fake 를 주입해요. None 이면 다음 호출 때 재배선."""
    global _domain_policy_service
    _domain_policy_service = svc


def provision_catalog_read_access(job) -> dict:
    """배포된 MCP 의 READ 도구를 회원 그룹에 기본 부여해요 (ADR-0094).

    민감도는 `job.gateway_targets` 에서 읽어요 — Registry 의 민감도별 Target 원장에서 파생된
    값이에요.

    ADR-0094 결정 3 은 **`heuristic_sensitivity` 의 추론값을 인가 근거로 쓰지 말라고 정해요.**

    ⚠️ **배포형 경로는 지금 그 결정을 지키지 않아요 (2026-09-05 실측).** 배포형 자산의 Target
    원장 태그를 만든 것이 `runtime/deploy/tool_extract.py` → `heuristic_sensitivity()` 예요.
    Registry·Target 원장을 거치는 건 **저장 경로**일 뿐 근거의 출처를 바꾸지 않아요.
    **그러니 ADR 의 규칙을 문장으로 완화하지 말고, 위반을 위반으로 두세요** —
    `docs/06-risks.md` 09-05 IH-166 「기준 공백」 행에 기록돼 있어요.

    단 **모든 태그가 추론값은 아니에요.** 연결형은 MCP 자기 선언(`descriptor`)을 보존할 수
    있고, 남은 것만 이름 동사 → Bedrock 분류 → **보수적 기본(fallback)** 순서로 채워요.
    관리자가 `admin` 출처로 확정할 수도 있어요. 행별 출처는 drift 원장의
    `ToolLedgerEntry.sensitivity_source` 에 있어요. 계보와 실측은 `AGENTS.md`
    §Runtime Authorization Hard Rules 의 민감도 문단에 있어요.

    MCP 자산에만 적용해요. agent 배포에는 부여할 operation 이 없어요.
    """
    from ..domains.identity.catalog_read_access import provision_read_access

    if str(getattr(job, "asset_type", "") or "") == "agent":
        return {"skipped": "agent 배포에는 부여할 operation 이 없어요"}
    record_id = str(getattr(job, "record_id", "") or "").strip()
    if not record_id:
        return {"skipped": "record_id 가 없어요"}

    asset_version_outcome = _record_asset_version(job, record_id)

    operations: dict[str, str] = {}
    for target in getattr(job, "gateway_targets", None) or ():
        sensitivity = str(target.get("sensitivity") or "").strip().upper()
        for operation_id in target.get("operations") or ():
            name = str(operation_id or "").strip()
            if name and sensitivity:
                operations[name] = sensitivity
    if not operations:
        return {
            "skipped": "민감도 태그가 있는 operation 이 없어요",
            "assetVersion": asset_version_outcome,
        }

    version = str(
        getattr(getattr(job, "source_ref", None), "version", "") or ""
    ).strip()
    report = provision_read_access(
        get_identity_store(),
        asset_id=record_id,
        asset_version=version,
        operations=operations,
        granted_by="deploy-job",
    )
    return {**report.to_dict(), "assetVersion": asset_version_outcome}


def _record_asset_version(job, record_id: str) -> str:
    """원장의 «자산 현재 버전» 행을 갱신해요 — ④ 대조의 기대값이에요 (ADR-0099 결정 13).

    등록·재등록이 이 행을 쓰는 유일한 지점이에요. 승인 흐름은 이 행을 쓰지 않아요 — 기대값의
    소유자가 대상과 달라야 대조가 성립해요(ADR-0037 §4). 옛 ⑤ 는 승인 경로가
    `asset_version=binding.asset_version` 으로 복사해 넣어서 구조적으로 통과했어요.

    **실패해도 배포를 막지 않아요.** 행이 없으면 interceptor 가 그 자산의 도구를 전부 거부해요
    (fail-closed) — 열리는 방향으로 실패하지 않으니 다음 배포에서 다시 시도해도 안전해요.
    반환값은 `written` · `no_version` · `failed: <타입>` 이고, 배포 보고서에 그대로 실어요.
    """
    from datetime import datetime, timezone

    from ..domains.identity.models import AssetVersionRecord

    version = str(
        getattr(getattr(job, "source_ref", None), "version", "") or ""
    ).strip()
    if not version:
        return "no_version"
    try:
        get_identity_store().put_asset_version(AssetVersionRecord(
            asset_id=record_id,
            asset_version=version,
            record_id=record_id,
            updated_at=datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            updated_by="deploy-job",
            source="register",
        ))
    except Exception as exc:
        _LOG.warning(
            "자산 현재 버전 행을 쓰지 못했어요 — 그 자산의 도구는 거부돼요(fail-closed): "
            "asset=%s version=%s error=%s",
            record_id, version, type(exc).__name__,
        )
        return f"failed: {type(exc).__name__}"
    return "written"


def _principal_email_of(principal_id: str) -> str:
    """사람의 로그인 email 을 디렉토리에서 읽어요. 실패하면 빈 문자열.

    MCP 에 넘길 `agora_user_id` 값이에요(ADR-0095). 배포 검증 경로에서만 써요 —
    Gateway interceptor 는 외부 조회가 금지돼서(ADR-0091) 이미 원장에 박힌 값만 봐요.

    빈 문자열이면 `agora_user_id` 를 선언한 도구 호출이 **거부돼요.** 못 읽었으면 봇이 실어
    보낸 값을 통과시키지 않는다는 뜻이에요.
    """
    try:
        return str(get_cognito_user_directory().get_user(principal_id).email or "")
    except Exception as exc:
        _LOG.warning(
            "principal email 을 읽지 못했어요. agora_user_id 를 선언한 도구는 거부돼요: "
            "principal=%s failure=%s",
            principal_id, type(exc).__name__,
        )
        return ""


def _principal_groups_of(principal_id: str) -> tuple[str, ...]:
    """사람의 그룹을 디렉토리에서 읽어요. 실패하면 빈 tuple (fail-closed).

    **배포 경로에서만 써요.** Gateway interceptor 는 외부 조회가 금지돼서(ADR-0091) 그쪽은
    delegation 행에 담긴 값만 봐요. 여기는 배포 job 이라 조회가 허용돼요.

    빈 tuple 이면 그룹 단위 grant 가 적용되지 않아요 — 조회 실패를 "그룹 없음" 으로 접는 게
    아니라, 못 읽었으면 권한을 넓히지 않는다는 뜻이에요.
    """
    try:
        return filter_platform_roles(
            get_cognito_user_directory().list_groups(principal_id)
        )
    except Exception as exc:
        _LOG.warning(
            "principal 그룹을 읽지 못해 그룹 grant 없이 진행해요: principal=%s failure=%s",
            principal_id, type(exc).__name__,
        )
        return ()


def issue_verify_call_handle(job) -> str | None:
    """배포 검증용 delegation handle 을 발급해요 (IA-61 후속, 2026-08-29).

    Gateway REQUEST interceptor 가 붙은 뒤로 **검증 호출도** `X-Agora-Call` handle 이
    필요해요. 없으면 `initialize` 부터 거부돼서 agent 가 도구를 하나도 못 받고 verify 가
    "선언된 도구가 없어요" 로 실패해요(실측 2026-08-29:
    `gateway_interceptor_denied method=initialize reason=invalid_delegation`).

    사람 축은 **배포를 시작한 사용자**예요(`job.principal`). 검증은 그 사람 대신 도구를
    부르는 것이라 그게 정직한 귀속이고, 원장에 사람 없는 delegation 이 생기지 않아요.

    허용 자산은 이 agent 가 **선언한 MCP** 로 한정해요. 비워 두면 handle 하나로 그 Gateway 의
    모든 자산을 부를 수 있게 돼요.
    """
    record_id = str(getattr(job, "record_id", "") or "").strip()
    principal_id = str(getattr(job, "principal", "") or "").strip()
    if not record_id or not principal_id:
        return None
    asset_ids = tuple(
        str(asset.get("assetId") or "").strip()
        for asset in (getattr(job, "mcp_assets", None) or ())
        if str(asset.get("assetId") or "").strip()
    )
    # 배포자의 그룹을 실어요 — 그룹 단위 grant(예: 회원 기본 READ)로 승인된 도구를
    # 검증이 부를 수 있어야 해요. 안 실으면 검증만 막혀서 "실사용은 되는데 배포가 실패" 예요.
    job_groups = getattr(job, "principal_groups", ()) or ()
    groups = filter_platform_roles(job_groups)
    if not job_groups:
        groups = _principal_groups_of(principal_id)
    # job 에는 email 이 없어서 디렉토리에서 읽어요. 이건 배포 job 이라 조회가 허용돼요.
    email = str(getattr(job, "principal_email", "") or "")
    if not email:
        email = _principal_email_of(principal_id)
    handle, _context = get_delegation_service().issue(
        principal_id=principal_id,
        agent_id=record_id,
        workload_id=str(getattr(job, "workload_identity_name", "") or ""),
        allowed_asset_ids=asset_ids,
        principal_groups=groups,
        principal_email=email,
    )
    return handle


def read_tool_approval_states(agent_record_id: str) -> dict[str, str]:
    """이 agent 의 ④ binding 상태를 `gateway_action → approval_state` 로 읽어요.

    ADR-0104(IH-153): VERIFYING 이 「승인 대기 도구」를 배포 실패가 아니라 관측된 상태로
    다루려면 이 값이 필요해요. **기대값의 소유자를 원장으로 고정**하는 게 요점이에요 —
    컴파일러 산출물이나 runtime 자기보고에서 가져오면 대조가 자기를 검증해요(ADR-0037 §4).

    소비자(interceptor)와 **같은 Query 경로**로 읽어요(`list_agent_tool_bindings`). scan 으로
    읽으면 파티션이 어긋난 행이 「있다」로 보여요(2026-08-29 그룹 grant 사고).

    `desired_state != ALLOWED` 인 행은 넣지 않아요 — 회수된 도구는 「승인 대기」도 「승인됨」도
    아니고, 열거에서도 빠져요. 그러면 verify 는 그 도구를 「미신청」으로 봐요.

    ⚠️ **한 action 에 행이 둘일 수 있어요.** SK 가 `TOOL#<asset>#<ver>#<op>` 라서 재등록 후에는
    같은 `gateway_action` 에 v1·v2 행이 함께 있을 수 있어요. dict 은 하나만 담으니 우선순위를
    **명시**해요 — `APPROVED` > `REQUESTED` > 나머지. 순서를 정하지 않으면 `list_` 반환 순서에
    따라 같은 원장이 「승인됨」과 「승인 대기」로 갈려서, 도구가 안 붙은 배포가 어떤 날은
    통과하고 어떤 날은 실패해요.

    `APPROVED` 를 이기게 두는 건 소비자와 같은 판정이에요: interceptor 는 `APPROVED` 행이
    정확히 하나면 진행하고, 그 뒤 버전 대조에서 걸러요(ADR-0090). 즉 「승인된 행이 있다」면
    그 도구는 `tools/list` 에 **있어야** 하고, 없으면 결함이에요.

    실패는 감추지 않고 예외로 올려요. 호출자(`DeployService._tool_approval_states`)가 그걸
    `None`(= 관측 못 함)으로 바꿔서 verify 를 엄격한 기대값으로 되돌려요.
    """
    from ..domains.identity.models import ApprovalState, DesiredState

    precedence = {
        ApprovalState.APPROVED.value: 2,
        ApprovalState.REQUESTED.value: 1,
    }
    states: dict[str, str] = {}
    for binding in get_identity_store().list_agent_tool_bindings(
        agent_record_id
    ):
        if binding.desired_state is not DesiredState.ALLOWED:
            continue
        action = binding.gateway_action
        if not action:
            continue
        state = binding.approval_state.value
        current = states.get(action)
        if current is None or precedence.get(state, 0) > precedence.get(
            current, 0
        ):
            states[action] = state
    return states


def provision_shared_gateway_policy(
    _job=None,
    *,
    active_max_polls: int | None = None,
) -> dict:
    """Gateway 공유 Cedar 정책을 지금 Target 원장에 맞춰요 (IA-71, ADR-0093).

    MCP 배포 job 이 Target 을 조정하고 카탈로그에 등재한 뒤 불러요. `_job` 은 호출 규약을
    맞추려고 받지만 쓰지 않아요 — **정책은 gateway 전체 자원**이라 이 job 의 자산만 보면
    안 돼요. 같은 Gateway의 다른 자산에서 승인된 action이 빠지면 그 도구가 조용히 거부돼요.

    `request_interceptor_attached` 는 넘기지 않아요. provisioner 가 Gateway 에서 직접
    관측해요 — 호출자가 정하면 subject 가 자기 기대값을 정하는 셈이에요(ADR-0037 §4).

    `active_max_polls=None` 은 배포 job용 기본 60폴을 그대로 써요. 관리자 HTTP 경로만
    `shared_policy_trigger`가 짧은 예산을 명시해서 ALB 요청 수명과 분리해요.
    """
    gateway_id, engine_id, spec = _shared_gateway_policy_inputs()
    provisioner_kwargs = {}
    if active_max_polls is not None:
        provisioner_kwargs["active_max_polls"] = active_max_polls
    return _shared_policy_provisioner(**provisioner_kwargs).provision(
        gateway_id=gateway_id,
        engine_id=engine_id,
        spec_without_interceptor=spec,
        created_by="deploy-job",
    ).to_dict()


def reclaim_stale_shared_gateway_policy_revisions() -> dict:
    """공유 ① 정책의 옛 ACTIVE 리비전을 회수해요 (IH-154).

    `provision_shared_gateway_policy` 와 **같은 좌표·같은 소유자 관측**을 쓰지만 정책을
    만들지는 않아요. 활성화 예산을 넘긴 옛 리비전은 Cedar permit 합집합에서 계속 이겨서
    다음 회수를 무력화해요.

    ⚠️ **정상 상태에서는 spec 을 만들지 않아요.** `_shared_gateway_policy_inputs()` 는 APPROVED
    registry 를 전량 열거하고 descriptor 를 검증해요(record 당 aux 읽기 포함). 60초마다 그걸
    하면 회수할 게 없는 대부분의 주기에 쓸모없는 비용이에요. 그래서 `list_policies` 로 먼저
    보고 소유 리비전이 둘 이상일 때만 spec 을 만들어요.
    """
    provisioner = _shared_policy_provisioner()
    gateway_id, engine_id, gateway_arn = _shared_gateway_policy_coordinates()
    early = provisioner.reclaim_precheck(
        engine_id=engine_id, gateway_arn=gateway_arn
    )
    if early is not None:
        return early.to_dict()
    _gateway_id, _engine_id, spec = _shared_gateway_policy_inputs()
    return provisioner.reclaim_stale_revisions(
        gateway_id=gateway_id,
        engine_id=engine_id,
        spec_without_interceptor=spec,
    ).to_dict()


def shared_gateway_policy_coordinates_configured() -> bool:
    """공유 정책 좌표 네 개가 모두 설정돼 있나요? (파괴적 pass 의 정렬 가드용)"""
    from .config import load_config

    cfg = load_config()
    return all((
        (cfg.m2_oauth_gateway_arn or "").strip(),
        (cfg.m2_oauth_gateway_id or "").strip(),
        (cfg.m2_oauth_scope or "").strip(),
        (cfg.m2_oauth_policy_engine_arn or "").strip(),
    ))


def _shared_gateway_policy_coordinates() -> tuple[str, str, str]:
    """`(gateway_id, engine_id, gateway_arn)` — registry 를 읽지 않는 좌표만."""
    from .config import load_config

    cfg = load_config()
    gateway_arn = (cfg.m2_oauth_gateway_arn or "").strip()
    gateway_id = (cfg.m2_oauth_gateway_id or "").strip()
    engine_arn = (cfg.m2_oauth_policy_engine_arn or "").strip()
    missing = [
        name
        for name, value in (
            ("AGORA_M2_OAUTH_GATEWAY_ARN", gateway_arn),
            ("AGORA_M2_OAUTH_GATEWAY_ID", gateway_id),
            ("AGORA_M2_OAUTH_POLICY_ENGINE_ARN", engine_arn),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Gateway 공유 정책 좌표가 없어요: " + ", ".join(missing)
        )
    return gateway_id, _policy_engine_id(engine_arn), gateway_arn


def _shared_policy_provisioner(**provisioner_kwargs):
    import boto3

    from ..domains.identity.shared_policy_provisioner import (
        SharedPolicyProvisioner,
    )
    from .config import load_config

    cfg = load_config()
    return SharedPolicyProvisioner(
        boto3.client(
            "bedrock-agentcore-control", region_name=cfg.deploy_region
        ),
        get_identity_store(),
        **provisioner_kwargs,
    )


def _shared_gateway_policy_inputs():
    """`(gateway_id, engine_id, spec)` — provisioning 과 회수가 **같은** 입력을 쓰게 해요."""
    from ..domains.catalog.registry.models import DescriptorType, RecordStatus
    from ..domains.identity.agent_policy_compiler import SharedGatewayPolicySpec
    from .config import load_config

    cfg = load_config()
    gateway_arn = (cfg.m2_oauth_gateway_arn or "").strip()
    gateway_id = (cfg.m2_oauth_gateway_id or "").strip()
    invoke_scope = (cfg.m2_oauth_scope or "").strip()
    engine_arn = (cfg.m2_oauth_policy_engine_arn or "").strip()
    missing = [
        name
        for name, value in (
            ("AGORA_M2_OAUTH_GATEWAY_ARN", gateway_arn),
            ("AGORA_M2_OAUTH_GATEWAY_ID", gateway_id),
            ("AGORA_M2_OAUTH_SCOPE", invoke_scope),
            ("AGORA_M2_OAUTH_POLICY_ENGINE_ARN", engine_arn),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Gateway 공유 정책 provisioning 좌표가 없어요: " + ", ".join(missing)
        )
    records = get_registry().list_records(
        get_registry_id(),
        statuses=(RecordStatus.APPROVED,),
        max_results=None,
    )
    descriptors = tuple(
        record.descriptors
        for record in records
        if record.descriptor_type is DescriptorType.MCP
    )
    spec = SharedGatewayPolicySpec.from_registry(
        gateway_arn=gateway_arn,
        mcp_descriptors=descriptors,
        invoke_scope=invoke_scope,
        # 컴파일러가 `invoke_scope` 가 이 목록에 있는지 확인해요. 빈 tuple 을 넘기면
        # 검증에서 막혀요(폐기된 컷오버 스크립트의 결함이었어요).
        #
        # `/danger` scope 는 더 이상 넘기지 않아요 (ADR-0099 결정 8) — 백스톱이 없어졌고,
        # 발급되지도 않던 scope 를 요구하는 게 IH-130 의 원인이었어요.
        scope_names=(invoke_scope,),
    )
    return gateway_id, _policy_engine_id(engine_arn), spec


def _requires_agent_authorization_ledger(record) -> bool:
    """현재 인가 원장 행이 있어야 하는 관리형 Runtime Agent인지 판정해요.

    연결형 Agent는 APPROVED여도 endpoint만 있고 원장 없이 호출되는 것이 정상이고,
    미승인·거부·폐기 record는 현재 인가 대상이 아니에요. 따라서 게이트 분모는
    APPROVED Agent 중 배포 좌표(runtimeArn)가 있는 record로만 정의해요.
    """
    from ..domains.catalog.registry.models import DescriptorType, RecordStatus

    if (
        record.descriptor_type is not DescriptorType.AGENT
        or record.status is not RecordStatus.APPROVED
    ):
        return False
    descriptors = record.descriptors
    if not isinstance(descriptors, dict):
        return False
    agent = descriptors.get("agent")
    return (
        isinstance(agent, dict)
        and isinstance(agent.get("runtimeArn"), str)
        and bool(agent["runtimeArn"].strip())
    )


def observe_agent_policy_inventory_with_catalog_agents():
    """Inventory와 같은 Registry 열거에서 인가 대상 Agent 집합도 반환해요."""
    from ..domains.catalog.registry.models import RecordStatus
    from ..domains.identity.agent_policy_deployer import PolicyInventoryReport

    try:
        cfg = load_config()
        gateway_arn = (
            cfg.m2_oauth_gateway_arn
            if cfg.authorization_mode == "agent_policy"
            else cfg.m2_gateway_arn
        ) or ""
        if not gateway_arn:
            return (
                PolicyInventoryReport.unknown(
                    "policy gateway scope is not configured"
                ),
                None,
            )
        try:
            records = get_registry().list_records(
                get_registry_id(),
                statuses=tuple(RecordStatus),
                max_results=None,
            )
            catalog_record_ids = {
                record.record_id for record in records
            }
            catalog_record_statuses = {
                record.record_id: record.status.value for record in records
            }
            catalog_agent_ids = tuple(sorted(
                record.record_id
                for record in records
                if _requires_agent_authorization_ledger(record)
            ))
            catalog_observation_error = ""
        except Exception as exc:  # noqa: BLE001
            catalog_record_ids = None
            catalog_record_statuses = None
            catalog_agent_ids = None
            catalog_observation_error = f"{type(exc).__name__}: {exc}"
        return (
            get_agent_policy_deployer().inventory(
                catalog_record_ids=catalog_record_ids,
                catalog_record_statuses=catalog_record_statuses,
                authorization_target_agent_ids=(
                    set(catalog_agent_ids)
                    if catalog_agent_ids is not None
                    else None
                ),
                catalog_observation_error=catalog_observation_error,
                gateway_arn=gateway_arn,
            ),
            catalog_agent_ids,
        )
    except Exception as exc:  # noqa: BLE001 - 관측 실패를 빈 리포트로 접지 않아요.
        return (
            PolicyInventoryReport.unknown(
                f"policy inventory unobservable: {type(exc).__name__}: {exc}"
            ),
            None,
        )


def observe_agent_policy_inventory():
    """Catalog·identity ledger·live Cedar를 읽어 fleet policy inventory를 만들어요."""
    report, _catalog_agent_ids = (
        observe_agent_policy_inventory_with_catalog_agents()
    )
    return report


def delete_unmanaged_agent_policies(
    policy_ids: tuple[str, ...] | list[str],
) -> tuple[str, ...]:
    """원장이 소유를 모르는 정책을 명시 요청으로만 삭제해요(ADR-0067).

    자동 회수 경로가 아니에요 — 호출자가 policy_id 를 지정해야 하고, deployer 가 삭제
    직전에 fresh snapshot 으로 `unmanaged` 여부를 다시 확인해요.
    """
    from ..domains.catalog.registry.models import RecordStatus

    cfg = load_config()
    gateway_arn = (
        cfg.m2_oauth_gateway_arn
        if cfg.authorization_mode == "agent_policy"
        else cfg.m2_gateway_arn
    ) or ""
    if not gateway_arn:
        raise RuntimeError("policy gateway scope is not configured")
    records = get_registry().list_records(
        get_registry_id(), statuses=tuple(RecordStatus), max_results=None,
    )
    return get_agent_policy_deployer().delete_unmanaged_policies(
        policy_ids,
        catalog_record_ids={record.record_id for record in records},
        gateway_arn=gateway_arn,
    )


def get_agent_identity_issuer():
    """AgentIdentityIssuer(IA-08) — 사람 pool에서 agent M2M binding을 발급."""
    global _agent_identity_issuer
    if _agent_identity_issuer is None:
        from ..domains.identity.agent_identity_issuer import AgentIdentityIssuer

        cfg = load_config()
        import boto3

        # IA-78 이후 legacy 이름인 M2_OAUTH_* 좌표의 값은 사람 pool을 가리켜요.
        # 기존 봇 pool client/원장은 이 경로에서 이전하거나 삭제하지 않아요.
        _agent_identity_issuer = AgentIdentityIssuer(
            get_identity_store(),
            cognito_client=boto3.client(
                "cognito-idp", region_name=cfg.deploy_region
            ),
            user_pool_id=cfg.m2_oauth_user_pool_id or "",
            invoke_scope=cfg.m2_oauth_scope or "",
        )
    return _agent_identity_issuer


def set_agent_identity_issuer(svc) -> None:
    """테스트에서 AgentIdentityIssuer(또는 fake)를 주입해요. None이면 다음 호출 때 재배선."""
    global _agent_identity_issuer
    _agent_identity_issuer = svc


def get_dev_identity_service():
    """Temporary local-development OAuth identity broker."""
    global _dev_identity_service
    if _dev_identity_service is None:
        from ..domains.identity.dev_identity_cognito import (
            DevCognitoClientProvisioner,
            DevCognitoTokenIssuer,
        )
        from ..domains.identity.dev_identity_service import DevIdentityService
        from ..domains.identity.dev_identity_store import DynamoDevIdentityStore
        from ..domains.identity.shared_policy_trigger import (
            HTTP_ACTIVE_MAX_POLLS,
        )
        from .dev_identity_catalog import CatalogDevAssetResolver

        cfg = load_config()
        if not cfg.identity_table:
            raise RuntimeError("AGORA_IDENTITY_TABLE이 필요해요.")
        required = {
            "AGORA_M2_OAUTH_COGNITO_USER_POOL_ID": cfg.m2_oauth_user_pool_id,
            "AGORA_M2_OAUTH_SCOPE": cfg.m2_oauth_scope,
            "AGORA_M2_OAUTH_TOKEN_URL": cfg.m2_oauth_token_url,
            "AGORA_M2_OAUTH_GATEWAY_ARN": cfg.m2_oauth_gateway_arn,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(f"{', '.join(missing)} 설정이 필요해요.")
        import boto3

        cognito = boto3.client("cognito-idp", region_name=cfg.deploy_region)
        asset_resolver = CatalogDevAssetResolver(
            get_registry(),
            get_registry_id(),
        )
        _dev_identity_service = DevIdentityService(
            DynamoDevIdentityStore(
                table_name=cfg.identity_table,
                region=cfg.identity_region,
            ),
            get_identity_store(),
            client_provisioner=DevCognitoClientProvisioner(
                cognito,
                user_pool_id=cfg.m2_oauth_user_pool_id,
                invoke_scope=cfg.m2_oauth_scope,
                token_ttl_minutes=cfg.dev_identity_token_ttl_minutes,
            ),
            policy_manager=get_agent_policy_deployer(),
            # 호출 시점에 사람의 **현재** 그룹을 다시 읽는 통로예요. 배선을 빼면
            # `call_handle` 이 fail-closed 로 막아요(얼려둔 그룹으로 넘어가지 않아요).
            group_directory=get_cognito_user_directory(),
            token_issuer=DevCognitoTokenIssuer(
                cognito,
                user_pool_id=cfg.m2_oauth_user_pool_id,
                token_url=cfg.m2_oauth_token_url,
                invoke_scope=cfg.m2_oauth_scope,
            ),
            gateway_arn=cfg.m2_oauth_gateway_arn,
            credential_ttl_days=cfg.dev_identity_credential_ttl_days,
            credential_ttl_max_days=cfg.dev_identity_credential_ttl_max_days,
            asset_context_resolver=asset_resolver.resolve,
            now=lambda: int(__import__("time").time()),
            # ④ 행을 만들고 지우는 경로라 공유 ① 열거를 바꿔요 (IH-152 결정 2, ADR-0108).
            # 배선을 빼면 만료된 크리덴셜의 도구가 계속 `tools/list` 에 남고, 그 자산이
            # purge 될 때 완전성 게이트가 그 Gateway 의 정책 쓰기를 전부 막아요(IH-157).
            #
            # 짧은 예산을 써요 — 발급·회수는 HTTP 요청 안이고, 수렴은 실패해도
            # `shared_policy_reclaimer` 와 다음 provisioning 이 이어받아요.
            provision_shared_policy=lambda: provision_shared_gateway_policy(
                active_max_polls=HTTP_ACTIVE_MAX_POLLS,
            ),
            # 서비스가 server를 import하면 순환 의존이고 테스트가 env에 매달려요.
            # composition root가 server의 단일 cleanup-owner 판정을 등록하고, 여기서는
            # 그 callable을 그대로 넘겨 사용자 응답의 자동 재시도 약속만 정직하게 만들어요.
            retry_actor_available=_dev_identity_retry_actor_available,
        )
    return _dev_identity_service


def set_dev_identity_retry_actor_available(check) -> None:
    """서버 composition root의 공유 cleanup owner 판정을 주입해요."""
    global _dev_identity_retry_actor_available, _dev_identity_service
    _dev_identity_retry_actor_available = check
    # 이미 만든 서비스가 옛 판정을 캡처하지 않게 다음 접근에서 다시 조립해요.
    _dev_identity_service = None


def set_dev_identity_service(service) -> None:
    global _dev_identity_service
    _dev_identity_service = service


def get_credential_broker():
    """Credential broker adapter. Non-production stages stay offline by default."""
    global _credential_broker
    if _credential_broker is None:
        from ..domains.identity.credential_broker import AwsStsBroker, FakeBroker

        cfg = load_config()
        if cfg.stage == "prod":
            _credential_broker = AwsStsBroker(region=cfg.identity_region)
        else:
            _credential_broker = FakeBroker()
    return _credential_broker


def set_credential_broker(broker) -> None:
    """Inject a credential broker adapter for tests and controlled live checks."""
    global _credential_broker
    _credential_broker = broker


def _deploy_machine_oauth_coordinates(cfg) -> dict[str, str]:
    """Return the four coordinates owned by one human-pool machine client.

    Discovery, client ID, scope, and token URL must be repointed together.
    Cognito's hosted token domain is not safely derivable from its discovery URL,
    so the deployment environment supplies all four values explicitly.
    """
    return {
        "discovery_url": cfg.deploy_cognito_discovery_url or "",
        "client_id": cfg.deploy_cognito_client_id or "",
        "scope": cfg.deploy_cognito_scope or "",
        "token_url": cfg.deploy_cognito_token_url or "",
    }


def get_workload_token_verifier():
    global _workload_token_verifier
    if _workload_token_verifier is None:
        from ..domains.identity.token_verifier import IdentityConfigurationError
        from ..domains.identity.workload import (
            WorkloadTokenVerifier,
            issuer_from_discovery_url,
        )

        cfg = load_config()
        coordinates = _deploy_machine_oauth_coordinates(cfg)
        if not all(
            coordinates[key]
            for key in ("discovery_url", "client_id", "scope")
        ):
            raise IdentityConfigurationError(
                "Runtime workload Cognito 설정이 필요해요."
            )
        # Runtime allowedClients에는 사람 web client도 있지만, 이 endpoint는
        # client_credentials로 호출하는 배포용 기계 client 하나만 인증해요.
        _workload_token_verifier = WorkloadTokenVerifier(
            issuer=issuer_from_discovery_url(coordinates["discovery_url"]),
            client_id=coordinates["client_id"],
            required_scope=coordinates["scope"],
        )
    return _workload_token_verifier


def set_workload_token_verifier(verifier) -> None:
    global _workload_token_verifier
    _workload_token_verifier = verifier


_workload_assertion_verifier = None


def get_workload_assertion_verifier():
    global _workload_assertion_verifier
    if _workload_assertion_verifier is None:
        from ..domains.identity.workload import WorkloadAssertionVerifier

        _workload_assertion_verifier = WorkloadAssertionVerifier(
            claim_nonce=get_identity_store().claim_workload_nonce
        )
    return _workload_assertion_verifier


def set_workload_assertion_verifier(verifier) -> None:
    global _workload_assertion_verifier
    _workload_assertion_verifier = verifier


_trust_summary_port = None


def get_trust_summary_port():
    global _trust_summary_port
    if _trust_summary_port is None:
        from ..domains.governance.trust_adapter import GovernanceTrustAdapter
        # trust 요약은 순수 read라 seed(write)를 트리거하지 않아요(자산 상세 GET 부작용 방지).
        _trust_summary_port = GovernanceTrustAdapter(
            get_gov_store(seed=False), get_registry(), get_registry_id())
    return _trust_summary_port


def set_trust_summary_port(port) -> None:
    global _trust_summary_port
    _trust_summary_port = port


_registration_gate = None


def get_registration_gate():
    """등록 라이프사이클 hook (ADR-017 결정 5).

    Registry·GovStore를 governance 구현에 주입해요. catalog와 runtime은 이 접근자만 쓰고
    governance 구현을 직접 import하지 않아요 — 도메인 간 직접 의존을 굳히지 않고 테스트
    교체점을 한 곳(set_registration_gate)에 두려는 거예요.

    seed=False: 등록 hook은 상태 전이만 하므로 tier baseline seed(write)를 트리거하지
    않아요(trust 요약과 같은 이유).
    """
    global _registration_gate
    if _registration_gate is None:
        from ..domains.governance.registration_gate import GovernanceRegistrationGate

        _registration_gate = GovernanceRegistrationGate(
            get_registry(), get_registry_id(), get_gov_store(seed=False))
    return _registration_gate


def set_registration_gate(gate) -> None:
    global _registration_gate
    _registration_gate = gate


_deployment_gate = None


def get_deployment_gate():
    """배포 허가 게이트 (ADR-017 결정 7).

    등록 판정과 분리된 별도 계약이에요. 거버넌스 배포 정책이 도입되기 전까지는
    `OpenDeploymentGate`(허가)라 현행 동작과 동일해요.
    """
    global _deployment_gate
    if _deployment_gate is None:
        from .governance import OpenDeploymentGate

        _deployment_gate = OpenDeploymentGate()
    return _deployment_gate


def set_deployment_gate(gate) -> None:
    global _deployment_gate
    _deployment_gate = gate


_scanner = None


def get_scanner():
    """거버넌스 스캐너 (AGORA_SCANNER=static|noop|fargate, 기본 static).

    fargate는 CDK(AgoraGovernanceScan) 출력값을 env로 받아 실제 격리 Fargate task에서
    스캐너를 실행해요. 미설정 시 StaticScanner라 기존 테스트·로컬 동작 무영향.
    """
    global _scanner
    if _scanner is None:
        import os
        mode = os.getenv("AGORA_SCANNER", "static")
        if mode == "fargate":
            from ..domains.governance.fargate_runner import FargateScanRunner
            _scanner = FargateScanRunner(
                cluster=os.environ["AGORA_SCAN_CLUSTER"],
                task_def=os.environ["AGORA_SCAN_TASKDEF"],
                subnets=[s for s in os.getenv("AGORA_SCAN_SUBNETS", "").split(",") if s],
                security_groups=[s for s in os.getenv("AGORA_SCAN_SG", "").split(",") if s],
                bucket=os.environ["AGORA_SCAN_BUCKET"],
                region=os.getenv("AGORA_SCAN_REGION", "ap-northeast-2"),
                timeout=int(os.getenv("AGORA_SCAN_TIMEOUT", "300")),
            )
        elif mode == "stepfn":
            from ..domains.governance.stepfn_runner import StepFunctionScanRunner
            from ..domains.governance.tool_catalog import list_catalog_tools
            sm = os.getenv("AGORA_SFN_ARN", "")
            region = os.getenv("AGORA_SCAN_REGION") or os.getenv("AWS_REGION", "ap-northeast-2")
            bucket = os.getenv("AGORA_SCAN_BUCKET", "")
            # cells_provider는 폴백(자산 tier는 scan_service가 descriptors.cells로 전달 — SP-4 P2).
            _scanner = StepFunctionScanRunner(
                state_machine_arn=sm, region=region, bucket=bucket,
                cells_provider=lambda: get_gov_store().get_tier("minimal"),
                tools_provider=lambda: list_catalog_tools())
        elif mode == "noop":
            from ..domains.governance.scanner import NoopScanner
            _scanner = NoopScanner()
        else:
            from ..domains.governance.scanner import StaticScanner
            _scanner = StaticScanner()
    return _scanner


_scan_service = None


def get_scan_service():
    """스캔 오케스트레이션 서비스.

    store·scanner 는 주입하지 않고 ScanService.run() 시점에 get_gov_store()/get_scanner()로
    해석하게 둬요 — 테스트 conftest가 _gov_store 를 테스트마다 교체하지만 _scan_service 는
    리셋하지 않으므로, 생성 시점에 스토어를 캡처하면 낡은 스토어를 물어 기록이 유실돼요.
    """
    global _scan_service
    if _scan_service is None:
        from ..domains.governance.scan_service import ScanService
        _scan_service = ScanService(
            source_store=get_source_store(), registry=get_registry(), registry_id=get_registry_id(),
        )
    return _scan_service


_report_service = None


def get_report_service():
    """위협리포트 생성 서비스 (§2.1·§3-F).

    AGORA_REPORT_LLM=bedrock(기본)|template. bedrock이면 Sonnet 4.6(global)로 생성하고
    LLM 실패 시 템플릿으로 폴백. 스토어는 get_gov_store()로 항상 현재 인스턴스를 물어
    conftest 격리와 충돌하지 않게 해요(ReportService가 latest_scan을 호출 시점에 조회).
    """
    global _report_service
    if _report_service is None:
        import os
        from ..domains.governance.report_service import (
            BedrockReportGenerator, TemplateReportGenerator, ReportService,
        )
        template = TemplateReportGenerator()
        mode = os.getenv("AGORA_REPORT_LLM", "bedrock")
        if mode == "template":
            generator, fallback = template, None
        else:
            model_id = os.getenv("AGORA_REPORT_MODEL", "global.anthropic.claude-sonnet-4-6")
            region = os.getenv("AGORA_REPORT_REGION") or os.getenv("AWS_REGION", "us-west-2")
            generator, fallback = BedrockReportGenerator(model_id=model_id, region=region), template
        _report_service = ReportService(generator=generator, store=get_gov_store(), fallback=fallback)
    return _report_service


_bedrock_runtime_clients = {}


def get_bedrock_runtime_client(region: str):
    """Shared lazy Bedrock runtime client for registration-time classifiers."""
    client = _bedrock_runtime_clients.get(region)
    if client is None:
        import boto3

        client = boto3.client("bedrock-runtime", region_name=region)
        _bedrock_runtime_clients[region] = client
    return client


_compute_classifier = None


def get_compute_classifier():
    """도구 compute(lambda|fargate) 분류기 (SP-2).

    AGORA_COMPUTE_LLM=bedrock(기본)|rule. bedrock이면 Claude로 추론하고 실패 시 규칙 폴백,
    rule이면 규칙만. report_service 배선 패턴 계승.
    """
    global _compute_classifier
    if _compute_classifier is None:
        import os
        from ..domains.governance.compute_classifier import (
            BedrockComputeClassifier, RuleComputeClassifier, WithFallback,
        )
        rule = RuleComputeClassifier()
        mode = os.getenv("AGORA_COMPUTE_LLM", "bedrock")
        if mode == "rule":
            _compute_classifier = rule
        else:
            model_id = os.getenv("AGORA_COMPUTE_MODEL", "global.anthropic.claude-sonnet-4-6")
            region = os.getenv("AGORA_COMPUTE_REGION") or os.getenv("AWS_REGION", "us-west-2")
            _compute_classifier = WithFallback(
                BedrockComputeClassifier(model_id=model_id, region=region), rule)
    return _compute_classifier


_sensitivity_classifier = None


def get_sensitivity_classifier():
    """MCP sensitivity Tier-2 classifier using the existing compute LLM settings."""
    global _sensitivity_classifier
    import os

    if os.getenv("AGORA_COMPUTE_LLM", "bedrock") == "rule":
        return None
    if _sensitivity_classifier is None:
        from ..domains.catalog.mcp.sensitivity_suggest import (
            BedrockSensitivityClassifier,
        )

        model_id = os.getenv(
            "AGORA_COMPUTE_MODEL", "global.anthropic.claude-sonnet-4-6"
        )
        region = os.getenv("AGORA_COMPUTE_REGION") or os.getenv(
            "AWS_REGION", "us-west-2"
        )
        _sensitivity_classifier = BedrockSensitivityClassifier(
            model_id=model_id,
            region=region,
        )
    return _sensitivity_classifier


_deploy_service = None


def _deploy_cognito_config(cfg) -> dict[str, str]:
    """DeployService에 넘길 Cognito/AgentCore Identity 좌표를 조립해요."""
    machine = _deploy_machine_oauth_coordinates(cfg)
    oauth_pool_id = cfg.m2_oauth_user_pool_id or ""
    oauth_discovery_url = (
        cognito_discovery_url_from_pool_id(
            cfg.deploy_region,
            oauth_pool_id,
        )
        if oauth_pool_id
        else ""
    )
    return {
        "discoveryUrl": machine["discovery_url"],
        "client_id": machine["client_id"],
        # IA-89 ②: Runtime authorizer 인바운드를 기계 client 하나로 되돌려요. IA-79 가 사람 web
        # client 를 넣었지만 소비자가 0건이에요 — 사람 토큰을 Runtime `Authorization` 으로 싣는
        # 생산자가 저장소에 없고(사람 토큰은 별 헤더 `X-Agora-User-Token` 로 흘러요), IA-79 ④ 자신도
        # 「사람 web client 는 workload identity 가 아니다」라며 `/internal/authorization/decide` 를
        # 기계 client 하나로 좁혔어요. 소비자 없는 입장 확대라 남의 agent 를 실행하는 우회 경로가
        # 됐어요(IA-79 「인가 우회」 체인 ③). `deploy_cognito_human_client_id` 는 폐기 예정(config.py 참고).
        "allowed_clients": (machine["client_id"],),
        # Credential provider discovery는 client를 발급한 pool ID에서 파생해요. Gateway
        # inbound issuer 입력을 재사용하면 두 좌표가 부분 컷오버로 갈라질 수 있어요.
        "oauth_discovery_url": oauth_discovery_url,
        "oauth_scope": cfg.m2_oauth_scope or "",
        "oauth_pool_id": oauth_pool_id,
        "oauth_return_url": (
            external_oauth_return_url(cfg.web_base_url)
            if cfg.web_base_url
            else ""
        ),
    }


def get_deploy_service():
    """MCP 배포 파이프라인 서비스 (runtime 도메인) — AWS 단일 경로.

    AwsDeployAdapter + DynamoJobStore로 배선해요(mock 분기 없음, AWS 단일화).
    lazy 싱글톤 — 첫 호출 시에만 생성해 pytest collection이 크리덴셜 없이 통과해요.
    tool 발견은 CodeBuild 빌드 단계가 담당(tools_inline)하므로 discover 클로저 불필요.
    카탈로그 등재는 get_registry()(RegistryPort)를 경유해 SoT 직접 쓰기를 피해요.
    테스트는 set_deploy_service로 fake를 주입해 실제 AWS 배선을 건너뛰어요.
    """
    global _deploy_service
    if _deploy_service is None:
        import uuid
        from datetime import datetime, timezone
        from ..domains.runtime.deploy.aws_adapter import AwsDeployAdapter
        from ..domains.runtime.deploy.jobs_dynamo import DynamoJobStore
        from ..domains.runtime.deploy.service import DeployService
        from ..domains.runtime.deploy.verify import (
            AgentVerifier,
            BuiltinToolVerifier,
        )
        cfg = load_config()

        def _now() -> str:
            return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        def _redeploy_sensitivity_guard(
            request: RedeploySensitivityRequest,
        ) -> RedeploySensitivityDecision:
            return get_mcp_drift_service().prepare_redeploy(
                str(request.get("record_id") or ""),
                str(request.get("tools_inline") or ""),
                actor=str(request.get("actor") or "unknown"),
            )

        deploy_store = DynamoJobStore(
            table_name=cfg.deploy_jobs_table,
            region=cfg.deploy_region,
        )
        deploy_adapter = AwsDeployAdapter(
                region=cfg.deploy_region,
                codebuild_project=cfg.deploy_codebuild_project or "",
                artifact_bucket=cfg.deploy_artifact_bucket or "",
                # 업로드 소스(sourcestore)는 서울 버킷 — CodeBuild가 여기서 내려받아요.
                source_bucket=cfg.bucket_name,
                source_region=cfg.source_region,
            )
        _deploy_service = DeployService(
            port=deploy_adapter,
            store=deploy_store,
            gateway_id=cfg.deploy_gateway_id or "",
            oauth_gateway_id=cfg.m2_oauth_gateway_id or "",
            exec_role_arn=cfg.deploy_exec_role_arn or "",
            # agent 경로는 AgentCore Runtime 전용 실행롤(bedrock-agentcore trust)을 써요.
            agent_exec_role_arn=cfg.deploy_agent_exec_role_arn or "",
            agent_shared_policy_arn=(
                cfg.deploy_agent_shared_policy_arn or ""
            ),
            agent_permissions_boundary_arn=(
                cfg.deploy_agent_permissions_boundary_arn or ""
            ),
            per_agent_roles_enabled=cfg.runtime_per_agent_roles_enabled,
            builtin_execution_role_arn=cfg.deploy_builtin_exec_role_arn or "",
            builtin_recording_bucket=cfg.deploy_builtin_recording_bucket or "",
            stage=cfg.stage,
            deploy_region=cfg.deploy_region,
            # cognito는 agent 배포 경로(advance_agent→create_agent_runtime)에서만 사용해요.
            # 키는 aws_adapter가 읽는 이름과 정확히 맞춰야 해요: discoveryUrl,
            # client_id(snake_case), allowed_clients. Runtime authorizer는 같은 사람 pool의
            # public web client와 기계 client를 모두 받고, 기계 client_id 하나만 workload
            # 토큰 발행·내부 검증에 써요(IA-79). oauth_* 값은 Identity P2 outbound용 —
            # 배포 agent가 Gateway 호출 토큰을 얻고, workload identity의 3LO 복귀 주소
            # 허용목록도 포털 origin에 맞춰요(IA-76).
            # IA-23(ADR-0016): OAuth Gateway Cedar가 tool 인가 단일 지점이라 client-side 런타임
            # 인가(_authorize)를 폐지했어요. `authorization_url`을 배선하지 않으면 aws_adapter가
            # workload key 발급·AGORA_AUTHORIZATION_URL/WORKLOAD env 주입을 일관되게 건너뛰어요.
            # (어댑터의 주입 능력 자체는 롤백 대비로 남겨 둬요 — 배선만 끊어요.)
            cognito=_deploy_cognito_config(cfg),
            oauth_gateway_url=cfg.m2_oauth_gateway_url or "",
            source_store=get_source_store(), registry=get_registry(),
            registry_id=get_registry_id(), gate=get_deployment_gate(),
            new_id=lambda: uuid.uuid4().hex, now=_now,
            # VERIFYING 단계 — 배포된 agent에 A2A로 도구 등록·대화를 확인해요.
            # invoke와 같은 Cognito 토큰 경로를 재사용해요(get_agentcore_invoker).
            verifier=AgentVerifier(
                invoker=get_agentcore_invoker(),
                qualification_scope=cfg.stage,
                qualification_store=deploy_store,
            ),
            builtin_verifier=BuiltinToolVerifier(
                observer=deploy_adapter,
                stage=cfg.stage,
                recording_bucket=cfg.deploy_builtin_recording_bucket or "",
            ),
            # IA-08 — 배포 완료 시 관리형 런타임 IAM 신원(AgentIdentityBinding)을 발급해요.
            identity_issuer=get_agent_identity_issuer(),
            fail_closed_on_unknown_authorization=(
                cfg.runtime_fail_closed_on_unknown_authorization
            ),
            fail_closed_on_unknown_builtin_tools=(
                cfg.runtime_fail_closed_on_unknown_builtin_tools
            ),
            fail_closed_on_identity_outbound=(
                cfg.runtime_fail_closed_on_identity_outbound
            ),
            redeploy_sensitivity_guard=_redeploy_sensitivity_guard,
            # IA-71·IA-85: MCP Target operation 목록이 바뀌면 Gateway 공유 Cedar 정책을
            # 다시 맞춰요. 배선을 빼면 승인 action이 열거에서 빠져 배포 후 조용히 거부돼요.
            provision_shared_policy=provision_shared_gateway_policy,
            # IA-61 후속: 검증 호출에 실을 handle 을 발급해요. interceptor 가 붙은 뒤로
            # 이게 없으면 모든 agent 배포가 VERIFYING 에서 실패해요.
            issue_call_handle=issue_verify_call_handle,
            # ADR-0094: 카탈로그 READ 도구를 회원 그룹에 기본 부여해요. 이게 없으면 자산을
            # 등록해도 등록자조차 도구를 못 불러요(interceptor 사람 축 1단이 비어서요).
            provision_read_access=provision_catalog_read_access,
            # ADR-0104(IH-153): VERIFYING 이 승인 대기 도구를 실패로 보지 않게 ④ 원장을
            # 읽어요. 배선을 빼면 원장을 못 본 것이 되어(=`unknown`) 옛 엄격한 기대값으로
            # 되돌아가고, 비-READ 도구를 가진 신규 agent 배포가 다시 막혀요.
            read_tool_approval_states=read_tool_approval_states,
            # IH-83: 비-READ 신청을 브라우저가 아니라 deploy job이 제출해요. identity
            # 구현은 shared seam 뒤에 두어 runtime 도메인 직접 의존을 막아요.
            submit_tool_requests=submit_agent_tool_requests,
            # 신청 결과는 terminal `tool-request` 요청 로그 한 행으로 관측해요.
            record_tool_request=record_agent_tool_request,
        )
    return _deploy_service


def set_deploy_service(svc) -> None:
    """테스트에서 DeployService(또는 fake)를 주입해요. None이면 다음 호출 때 재배선."""
    global _deploy_service
    _deploy_service = svc


_connect_gateway_service = None


def get_connect_gateway_service():
    """connect MCP를 agent별 Cedar가 붙은 M2 OAuth Gateway에 등재해요.

    lazy 싱글톤 — 첫 호출 시에만 생성해 pytest collection이 크리덴셜 없이 통과해요.
    테스트는 set_connect_gateway_service로 fake를 주입해 실제 AWS 배선을 건너뛰어요.
    """
    global _connect_gateway_service
    if _connect_gateway_service is None:
        from datetime import datetime, timezone
        from ..domains.catalog.mcp.gateway_connect import ConnectGatewayService
        from ..domains.runtime.deploy.aws_adapter import AwsDeployAdapter
        cfg = load_config()
        _connect_gateway_service = ConnectGatewayService(
            port=AwsDeployAdapter(region=cfg.deploy_region,
                                  artifact_bucket=cfg.deploy_artifact_bucket or ""),
            gateway_id=cfg.m2_oauth_gateway_id or "",
            # IA-42 이전 connect descriptor에는 gateway ID가 없어요. 삭제/rollback
            # 시 그 target이 있던 deploy gateway를 찾는 호환 좌표예요.
            legacy_gateway_id=cfg.deploy_gateway_id or "",
            now=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    return _connect_gateway_service


def set_connect_gateway_service(svc) -> None:
    """테스트에서 ConnectGatewayService(또는 fake)를 주입해요. None이면 다음 호출 때 재배선."""
    global _connect_gateway_service
    _connect_gateway_service = svc


_request_log = None


def get_request_log():
    """요청 로그 스토어(RequestLog). 배포 job과 같은 deploy_jobs 테이블에 REQ# PK로 저장해요.

    lazy 싱글톤 — 첫 호출 시에만 생성해 pytest collection이 크리덴셜 없이 통과해요.
    테스트는 set_request_log로 fake를 주입해 실제 AWS 배선을 건너뛰어요.
    """
    global _request_log
    if _request_log is None:
        from ..domains.catalog.requests.store import DynamoRequestLog
        cfg = load_config()
        _request_log = DynamoRequestLog(
            table_name=cfg.deploy_jobs_table, region=cfg.deploy_region)
    return _request_log


def set_request_log(store) -> None:
    """테스트에서 RequestLog(또는 fake)를 주입해요. None이면 다음 호출 때 재배선."""
    global _request_log
    _request_log = store


_bundle_store = None


def get_bundle_store():
    """Bundle 스토어. AGORA_BUNDLE_TABLE이 설정되면 DynamoBundleStore(공유), 없으면 로컬 JSON.

    Fargate 등 다중 인스턴스·재시작 환경에서 로컬 JSON은 휘발해요(HP-02). 로컬·배포 모두
    테이블 env를 배선해 항상 Dynamo를 쓰는 게 HP-04 원칙이에요 — JSON 폴백은 테이블 미설정
    환경(예: fake 주입 없이 도는 단위 테스트)의 무해한 안전망이에요. 리전은 카탈로그 실물과
    같은 source_region(서울)이에요.
    """
    global _bundle_store
    if _bundle_store is None:
        cfg = load_config()
        if cfg.bundle_table:
            from ..domains.bundle.dynamo_store import DynamoBundleStore
            _bundle_store = DynamoBundleStore(
                table_name=cfg.bundle_table, region=cfg.source_region)
        else:
            from ..domains.bundle.store import JsonBundleStore
            _bundle_store = JsonBundleStore(store_path=_ROOT / ".agora-bundle.json")
    return _bundle_store


def set_bundle_store(store) -> None:
    """테스트에서 BundleStore(또는 fake)를 주입해요. None이면 다음 호출 때 재배선."""
    global _bundle_store
    _bundle_store = store


_bundle_service = None


def get_bundle_service():
    """Bundle 서비스 — CRUD + 멤버 확장. registry는 get_registry()로 주입(SoT 직접 쓰기 회피)."""
    global _bundle_service
    if _bundle_service is None:
        import uuid
        from datetime import datetime, timezone
        from ..domains.bundle.service import BundleService

        def _now() -> str:
            return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        _bundle_service = BundleService(
            store=get_bundle_store(), registry=get_registry(),
            registry_id=get_registry_id(), new_id=lambda: uuid.uuid4().hex, now=_now,
        )
    return _bundle_service


def set_bundle_service(svc) -> None:
    """테스트에서 BundleService(또는 fake)를 주입해요. None이면 다음 호출 때 재배선."""
    global _bundle_service
    _bundle_service = svc


_connection_store = None
_credential_store = None
_connection_service = None


def get_connection_store():
    """RepoConnection 스토어. AGORA_CONNECTION_TABLE이 설정되면 DynamoConnectionStore, 없으면 JSON.

    BundleStore와 같은 배선(HP-03). Fargate 재시작 시 로컬 JSON은 휘발하므로 로컬·배포 모두
    테이블 env를 배선해 Dynamo를 써요. 리전은 source_region(서울)이에요.
    """
    global _connection_store
    if _connection_store is None:
        cfg = load_config()
        if cfg.connection_table:
            from ..domains.catalog.publish.dynamo_store import DynamoConnectionStore
            _connection_store = DynamoConnectionStore(
                table_name=cfg.connection_table, region=cfg.source_region)
        else:
            from ..domains.catalog.publish.store import JsonConnectionStore
            _connection_store = JsonConnectionStore(
                store_path=_ROOT / ".agora-publish-conn.json")
    return _connection_store


def set_connection_store(store) -> None:
    global _connection_store
    _connection_store = store


def get_credential_store():
    """PAT credential 스토어 — 항상 Secrets Manager(HP-04).

    PAT 원문은 평문 파일(로컬 JSON)이 아니라 Secrets Manager에 둬야 해요(보안). 예전엔 dev만
    로컬 JSON을 썼는데, 그러면 stage=dev로 도는 배포 포털에서 컨테이너 재시작 시 PAT가 휘발해
    repo publish가 깨졌어요(connection은 Dynamo로 남지만 원문이 사라지는 같은 계열 버그).
    로컬 dev·Fargate 실행롤 모두 secretsmanager 권한이 있어요. JSON 스토어는 테스트에서
    set_credential_store로 fake를 주입할 때만 써요. 리전은 기존 prod 경로 그대로 cfg.region.
    """
    global _credential_store
    if _credential_store is None:
        from ..domains.catalog.publish.credentials import SecretsManagerCredentialStore
        cfg = load_config()
        _credential_store = SecretsManagerCredentialStore(region=cfg.region)
    return _credential_store


def set_credential_store(store) -> None:
    global _credential_store
    _credential_store = store


def get_publisher(provider: str = "github"):
    """provider별 PublisherPort 어댑터. 현재 github만."""
    if provider == "github":
        from ..domains.catalog.publish.github import GitHubPublisher
        return GitHubPublisher()
    raise ValueError(f"지원하지 않는 provider: {provider!r}")


def get_connection_service():
    """ConnectionService — repo 연결·검증. 기본 publisher는 github."""
    global _connection_service
    if _connection_service is None:
        from datetime import datetime, timezone
        from ..domains.catalog.publish.service import ConnectionService

        def _now() -> str:
            return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        _connection_service = ConnectionService(
            store=get_connection_store(), credentials=get_credential_store(),
            publisher=get_publisher("github"), now=_now,
        )
    return _connection_service


def set_connection_service(svc) -> None:
    global _connection_service
    _connection_service = svc


_publish_service = None


def get_publish_service():
    """PublishService — bundle을 서피스별 plugin으로 push. lazy 싱글톤."""
    global _publish_service
    if _publish_service is None:
        from datetime import datetime, timezone
        from ..domains.catalog.publish.publish_service import PublishService

        def _now() -> str:
            return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        _publish_service = PublishService(
            registry=get_registry(), registry_id=get_registry_id(),
            source_store=get_source_store(),
            connection_service=get_connection_service(),
            publisher=get_publisher("github"), now=_now,
        )
    return _publish_service


def set_publish_service(svc) -> None:
    global _publish_service
    _publish_service = svc


_prompt_service = None


def get_prompt_service():
    """system prompt 생성 서비스 (Agent Initializr).

    AGORA_PROMPT_LLM=bedrock(기본)|template. bedrock이면 invoke_model로 생성하고
    LLM 실패 시 템플릿으로 폴백. report_service 배선 패턴 계승.
    """
    global _prompt_service
    if _prompt_service is None:
        import os
        from ..domains.playground.prompt_service import (
            BedrockPromptGenerator, TemplatePromptGenerator, PromptService,
        )
        template = TemplatePromptGenerator()
        mode = os.getenv("AGORA_PROMPT_LLM", "bedrock")
        if mode == "template":
            _prompt_service = PromptService(generator=template, fallback=None)
        else:
            model_id = os.getenv("AGORA_PROMPT_MODEL", "global.anthropic.claude-sonnet-4-6")
            region = os.getenv("AGORA_PROMPT_REGION") or os.getenv("AWS_REGION", "us-west-2")
            _prompt_service = PromptService(
                generator=BedrockPromptGenerator(model_id=model_id, region=region),
                fallback=template)
    return _prompt_service


def set_prompt_service(svc) -> None:
    """테스트에서 PromptService(또는 fake)를 주입해요. None이면 다음 호출 때 재배선."""
    global _prompt_service
    _prompt_service = svc


_agentcore_invoker = None


class _DisabledAgentCoreInvoker:
    """Fail closed when Playground or the deployment verifier uses OAuth invoke."""

    def __init__(self, missing: list[str]):
        self._reason = (
            "AgentCore OAuth invoke is disabled; "
            f"{', '.join(missing)} 설정이 필요해요."
        )

    @property
    def reason(self) -> str:
        return self._reason

    # AgentVerifier uses both methods: probes call(), while its smoke test uses invoke().
    def call(self, **_kwargs):
        from ..domains.playground.invoke_service import InvokeError

        raise InvokeError(self.reason, status=503)

    def invoke(self, **_kwargs):
        from ..domains.playground.invoke_service import InvokeError

        raise InvokeError(self.reason, status=503)


def get_agentcore_invoker():
    """Playground 대화 호출기 (OAuth Bearer invoke). Cognito 토큰 provider 배선.

    secret은 CognitoTokenProvider가 describe로만 확보(미저장). pool_id는 discovery_url
    에서 파생해요. discovery/client/scope/token URL은 같은 사람 pool 기계 client의
    한 묶음이며, token URL만 옛 domain에 남으면 발급 요청이 401로 실패해요.
    """
    global _agentcore_invoker
    if _agentcore_invoker is None:
        from ..domains.playground.invoke_service import (
            AgentCoreInvoker, CognitoTokenProvider,
        )
        from .config import cognito_pool_id_from_discovery
        cfg = load_config()
        coordinates = _deploy_machine_oauth_coordinates(cfg)
        # PortalStack's SHARED_ALLOWLIST only copies a value supplied by the
        # deployment environment; it does not derive a domain from other coordinates.
        required = {
            "AGORA_DEPLOY_COGNITO_DISCOVERY_URL": coordinates["discovery_url"],
            "AGORA_DEPLOY_COGNITO_CLIENT_ID": coordinates["client_id"],
            "AGORA_DEPLOY_COGNITO_SCOPE": coordinates["scope"],
            "AGORA_DEPLOY_COGNITO_TOKEN_URL": coordinates["token_url"],
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            _agentcore_invoker = _DisabledAgentCoreInvoker(missing)
            _LOG.warning(_agentcore_invoker.reason)
        else:
            pool_id = cognito_pool_id_from_discovery(
                coordinates["discovery_url"]
            )
            provider = CognitoTokenProvider(
                pool_id=pool_id,
                client_id=coordinates["client_id"],
                token_url=coordinates["token_url"],
                scope=coordinates["scope"],
                region=cfg.deploy_region,
            )
            _agentcore_invoker = AgentCoreInvoker(
                region=cfg.deploy_region,
                token_provider=provider,
                force_trace_sampling=(
                    cfg.runtime_force_trace_sampling_enabled
                ),
            )
    return _agentcore_invoker


def set_agentcore_invoker(inv) -> None:
    """테스트에서 AgentCoreInvoker(또는 fake)를 주입해요. None이면 다음 호출 때 재배선."""
    global _agentcore_invoker
    _agentcore_invoker = inv


_agentcore_memory_control_client = None


def get_agentcore_memory_control_client():
    """AgentCore GetMemory control-plane client for independent observation."""
    global _agentcore_memory_control_client
    if _agentcore_memory_control_client is None:
        import boto3

        cfg = load_config()
        _agentcore_memory_control_client = boto3.client(
            "bedrock-agentcore-control",
            region_name=cfg.deploy_region,
        )
    return _agentcore_memory_control_client


def set_agentcore_memory_control_client(client) -> None:
    global _agentcore_memory_control_client
    _agentcore_memory_control_client = client


_purge_service = None


def get_purge_service():
    """PurgeService — 자산 완전 삭제 오케스트레이터. lazy 싱글톤."""
    global _purge_service
    if _purge_service is None:
        from ..domains.catalog.purge.service import PurgeService
        _purge_service = PurgeService(
            registry=get_registry(), registry_id=get_registry_id(),
            deploy_service=get_deploy_service(),
            connect_gateway=get_connect_gateway_service(),
            source_store=get_source_store(),
            deploy_port=get_deploy_service().port,
            gov_store=get_gov_store(), request_log=get_request_log(),
            bundle_store=get_bundle_store(),
            stats_store=get_stats_store(),
            identity_store=get_identity_store(),
            authorization_reclaimer=delete_agent_authorization_artifacts,
            asset_capability_cleaner=_new_asset_capability_cleaner(),
            access_grant_cleaner=_new_access_grant_cleaner(),
            provision_shared_policy=provision_shared_gateway_policy,
        )
    return _purge_service


def set_purge_service(svc) -> None:
    """테스트에서 PurgeService(또는 fake)를 주입해요. None이면 다음 호출 때 재배선."""
    global _purge_service
    _purge_service = svc


# --- 다운로드 집계 + 인기 톱5 (StatsPort) ---
_stats_store = None


def get_stats_store():
    """다운로드 집계 스토어(StatsPort 구현).

    주입된 store가 있으면 그걸 돌려줘요. 없으면 lazy로 DynamoStatsStore를 생성해요.
    카운터·스냅샷 모두 카탈로그 테이블(AGORA_TABLE_NAME)과 AGORA_SOURCE_REGION을 써요.
    """
    global _stats_store
    if _stats_store is None:
        from ..domains.catalog.stats.dynamo_store import DynamoStatsStore
        cfg = load_config()
        _stats_store = DynamoStatsStore(
            table_name=cfg.table_name,
            region=cfg.source_region,
        )
    return _stats_store


def set_stats_store(store) -> None:
    """테스트/배포에서 StatsPort 구현을 주입해요. None이면 다음 호출 때 재배선."""
    global _stats_store
    _stats_store = store


# --- MCP 도구 목록 드리프트 (LC-03) ---
_mcp_drift_store = None
_mcp_drift_service = None


def get_mcp_drift_store():
    """MCP 도구 드리프트 원장 스토어.

    새 테이블을 만들지 않아요 — 카탈로그 테이블(AGORA_TABLE_NAME)의 `MCPDRIFT#` 키
    공간을 써요(`AUX#`·`STATS#`와 같은 방식). 리전은 source_region(서울)이에요.
    """
    global _mcp_drift_store
    if _mcp_drift_store is None:
        from ..domains.catalog.mcp.drift_store import DynamoMcpDriftStore
        cfg = load_config()
        _mcp_drift_store = DynamoMcpDriftStore(
            table_name=cfg.table_name, region=cfg.source_region)
    return _mcp_drift_store


def set_mcp_drift_store(store) -> None:
    """테스트/배포 주입점. None이면 다음 호출 때 재배선해요."""
    global _mcp_drift_store, _mcp_drift_service
    _mcp_drift_store = store
    _mcp_drift_service = None   # store가 바뀌면 서비스도 다시 만들어야 해요


def get_mcp_drift_service():
    """MCP 도구 드리프트 서비스. registry는 lazy 싱글톤을 그대로 써요."""
    global _mcp_drift_service
    if _mcp_drift_service is None:
        from ..domains.catalog.mcp.drift_service import McpDriftService
        from ..domains.runtime.deploy.sensitivity import SensitivityTargetMover
        from ..domains.runtime.deploy.target_slices import gateway_target_names
        deploy_service = get_deploy_service()
        _mcp_drift_service = McpDriftService(
            registry=get_registry(),
            registry_id=get_registry_id(),
            store=get_mcp_drift_store(),
            # 단계 ②의 슬라이스 접근자를 주입해 배포와 화면이 같은 이름을 써요.
            target_name_resolver=gateway_target_names,
            propagate=SensitivityTargetMover(
                store=deploy_service.store,
                port=deploy_service.port,
            ),
            synchronize_connected_target=(
                deploy_service.port.synchronize_gateway_target
            ),
        )
    return _mcp_drift_service


def set_mcp_drift_service(service) -> None:
    """테스트/배포에서 서비스를 주입해요. None이면 다음 호출 때 재배선."""
    global _mcp_drift_service
    _mcp_drift_service = service


def observe_mcp_tool_drift(record_id: str) -> dict:
    """Read the drift ledger used by authorization-facing projections.

    Registry descriptors intentionally retain live Target coordinates while a
    MISSING deployed tool waits for IA-68. They are therefore not sufficient
    evidence that a tool is currently classified. Identity callers use this
    network-free ledger snapshot. For deployed Targets, Agora owns the inline
    schema, so the seeded ledger is authoritative without an upstream network
    probe. Connected Targets still require a successful tools/list observation.
    """
    try:
        snapshot = get_mcp_drift_service().snapshot_for(record_id)
    except Exception as exc:  # noqa: BLE001 - authorization must not guess.
        return {
            "known": False,
            "reason": f"mcp_drift_unobservable:{type(exc).__name__}",
        }
    status = str(getattr(snapshot.check_status, "value", ""))
    target_mode = str(getattr(snapshot.target_mode, "value", ""))
    if target_mode != "deployed" and status != "ok":
        return {
            "known": False,
            "reason": f"mcp_drift_{status or 'status_unobservable'}",
        }
    return {
        "known": True,
        "tools": {
            entry.tool_name: {
                "state": entry.state.value,
                "sensitivity": entry.sensitivity,
                "sensitivity_source": entry.sensitivity_source,
                "reappeared": entry.reappeared,
            }
            for entry in snapshot.entries
        },
    }


def count_agents_bound_to_tool(
    *, gateway_target_name: str, tool_name: str
) -> dict:
    """이 MCP 도구(operation)에 인가 원장 binding 이 있는 agent 를 세요.

    민감도 태그를 바꾸기 전에 "누가 이미 이 도구를 쓰고 있나"를 보여주는 근거예요.
    `catalog` 가 `identity` 를 직접 import 하지 않도록 이 seam 을 거쳐요
    (`record_agent_invoke_audit`·`observe_agent_invoke_traffic`와 같은 방식).

    반환은 항상 dict 예요 — `{"known": True, "count": n, "names": [...]}` 또는
    `{"known": False, "reason": "..."}`. **못 세면 0 이 아니라 확인 불가예요**(ADR-0037 §4):
    0 으로 내려보내면 화면이 "영향 없음"으로 읽히는데 그건 관측한 사실이 아니에요.

    매칭은 **이름 축**으로 해요 — Cedar 컴파일이 `gateway_target_name + operation_id` 로
    permit action 을 만들기 때문에, id 축으로 세면 MCP 재등록 후 새 id 가 생겨도
    옛 binding 이 이름 축에선 여전히 permit 이 살아 있어 거짓 0 이 나와요(LC-06).
    """
    from ..domains.catalog.registry.models import DescriptorType, RecordStatus

    # target 이름을 모르면 이름 축으로 셀 수 없어요.
    if not gateway_target_name:
        return {
            "known": False,
            "reason": (
                "이 자산의 gateway target 이름을 알 수 없어 영향 범위를 이름 축으로 셀 수 없어요."
            ),
        }

    try:
        records = get_registry().list_records(
            get_registry_id(),
            statuses=tuple(RecordStatus),
            max_results=None,
        )
    except Exception as exc:
        return {"known": False, "reason": f"자산 목록을 읽지 못했어요: {exc}"}

    agents = [r for r in records if r.descriptor_type is DescriptorType.AGENT]

    store = get_identity_store()
    matched_agents: list[tuple[str, str]] = []
    for agent in agents:
        try:
            bindings = store.list_agent_tool_bindings(agent.record_id)
        except Exception as exc:
            # 한 agent 라도 못 읽으면 합계가 사실이 아니에요 — 부분 합계를 내지 않아요.
            return {"known": False, "reason": f"인가 원장 조회에 실패했어요: {exc}"}
        matched = False
        for b in bindings:
            if b.operation_id != tool_name:
                continue
            # operation 이 일치하는 binding 은 반드시 이름 축에 놓을 수 있어야 해요.
            # gateway_target_name 이 비어 있으면 이 binding 을 이름 축에 놓을 수 없어요
            # (옛 스키마 binding 등) — 부분 합계 금지 원칙에 따라 즉시 known:False.
            if not b.gateway_target_name:
                return {
                    "known": False,
                    "reason": (
                        f"이름 축이 비어 있는 binding 이 있어 영향 범위를 확신할 수 없어요:"
                        f" agent={agent.record_id}"
                    ),
                }
            if b.gateway_target_name == gateway_target_name:
                matched = True
                break
        if matched:
            matched_agents.append(
                (agent.record_id, agent.name or agent.record_id)
            )
    matched_agents.sort(key=lambda item: item[0])
    return {
        "known": True,
        "count": len(matched_agents),
        "names": [name for _agent_id, name in matched_agents],
        "agent_ids": [
            agent_id for agent_id, _name in matched_agents
        ],
    }
