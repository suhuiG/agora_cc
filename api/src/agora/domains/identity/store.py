"""Identity P1 저장소 계약과 hermetic 인메모리 구현."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import RLock
from typing import TYPE_CHECKING, Literal
import uuid

from .models import (
    AccessGrant,
    AgentAuthorizationLedgerSnapshot,
    AgentClientClaim,
    AgentIdentityBinding,
    AgentInvokeAuthorization,
    AgentPolicyDeployment,
    AgentToolBinding,
    ApprovalState,
    AssetCapability,
    AssetCapabilityStatus,
    AssetVersionRecord,
    AuditEvent,
    AuthorizationOutcome,
    Connection,
    ConnectionCapability,
    DelegationContext,
    DesiredState,
    DomainPolicyRule,
    EffectiveState,
    ExternalWorkloadIdentity,
    GrantStatus,
    InvocationUsage,
    PolicyDeploymentStatus,
)
from .audit_timeline import (
    format_audit_timestamp,
    parse_audit_timestamp,
    resolve_audit_range,
)

if TYPE_CHECKING:
    from .agent_policy_cutover import GatewayPolicyCutover


@dataclass(frozen=True)
class AuditTimelineCoverage:
    status: Literal["ok", "unknown"]
    reason: Literal[
        "",
        "index_backfilling",
        "index_missing",
        "index_disappeared",
        "backfill_not_run",
        "backfill_in_progress",
        "backfill_failed",
        "range_before_backfill",
        "range_partially_backfilled",
        "coverage_access_denied",
        "coverage_throttled",
        "coverage_unavailable",
        "query_access_denied",
        "query_throttled",
    ] = ""
    backfilled_from: str | None = None
    backfill_status: Literal[
        "unknown", "not_run", "in_progress", "completed", "failed"
    ] = "completed"


# 도구 요약을 붙이는 감사 행 종류. `AGENT_INVOKE` 만 `INVOCATION#<id>/USAGE` 짝을 가져요.
# 다른 종류(정책 배포·도구 승인 등)의 `operation_id`·`target` 은 「도구」가 아니에요.
AUDIT_TOOL_USAGE_EVENT_TYPE = "AGENT_INVOKE"


@dataclass(frozen=True)
class AuditToolCall:
    """agent 가 자기보고한 도구 호출 카운터 한 줄.

    이름과 카운터만 담아요. 도구 인자·결과는 담지 않아요(ADR-0065 — 관리자에게도 보이지
    않아야 하는 값이에요).
    """

    name: str
    call_count: int = 0
    success_count: int = 0
    error_count: int = 0


@dataclass(frozen=True)
class AuditToolUsage:
    """`AGENT_INVOKE` 감사 행 하나에 붙는 도구 호출 요약.

    `status` 네 상태를 서로 접지 않아요.

    - `observed` + `tools` 있음 → 그 도구들을 불렀다고 agent 가 보고했어요.
    - `observed` + `tools` 빈 목록 → 도구를 안 썼다고 보고했어요.
    - `unknown` → 관측 실패예요. 이름 일부를 버렸을 수 있으니 「도구 없음」이 아니에요.
      (`reason` 에 이유가 들어가요. `tools` 가 비어 있지 않을 수도 있어요.)
    - `not_recorded` → 그 invocation 에 `USAGE` 항목 자체가 없어요(옛 레코드).
    """

    event_id: str
    invocation_id: str
    status: Literal["observed", "unknown", "not_recorded"]
    # ADR-0065: 포털은 `source=agent_report` 로 표시해요. Agora 의 독립 관측이 아니에요.
    source: str = ""
    reason: str = ""
    discarded_count: int = 0
    tools: tuple[AuditToolCall, ...] = ()


def audit_tool_usage_for(
    event: AuditEvent, usage: InvocationUsage | None
) -> AuditToolUsage:
    """감사 행 하나 + (있으면) 사용량 원장 항목 → 화면용 도구 요약.

    기대값은 원장 writer(`record_agent_invoke_usage`)가 남긴 필드에서만 와요. 이 함수가
    자기 출력으로 상태를 정하지 않아요.
    """
    if usage is None:
        return AuditToolUsage(
            event_id=event.event_id,
            invocation_id=event.invocation_id,
            status="not_recorded",
        )
    tools = tuple(
        AuditToolCall(
            name=metric.name,
            call_count=metric.call_count,
            success_count=metric.success_count,
            error_count=metric.error_count,
        )
        for metric in usage.tool_metrics
    )
    status: Literal["observed", "unknown", "not_recorded"] = "observed"
    reason = ""
    if usage.status != "observed":
        status = "unknown"
        reason = usage.reason or f"usage_status_{usage.status or 'missing'}"
    elif usage.tool_metrics_status and usage.tool_metrics_status != "observed":
        status = "unknown"
        reason = usage.tool_metrics_reason or (
            f"tool_metrics_status_{usage.tool_metrics_status}"
        )
    return AuditToolUsage(
        event_id=event.event_id,
        invocation_id=event.invocation_id,
        status=status,
        source=usage.source,
        reason=reason,
        discarded_count=usage.tool_metrics_discarded_count,
        tools=tools,
    )


def _is_invoke_row(event) -> bool:
    """`AGENT_INVOKE` 행인지 봐요.

    `getattr` 기본값은 `event_type` 속성 자체가 없는 duck-typed 행(모니터링 테스트가
    `audit_events` 에 직접 넣는 부분 이벤트) 때문이에요. 실제 원장 행은
    `AuditEvent.event_type` 기본값이 있어서 항상 이 속성을 가져요. 속성이 없으면 호출 행으로
    **간주하지 않아요** — 없는 근거로 도구를 지어내지 않는 쪽이 안전한 방향이에요.
    """
    return getattr(event, "event_type", "") == AUDIT_TOOL_USAGE_EVENT_TYPE


def audit_tool_usage_invocation_ids(
    events: Iterable[AuditEvent],
) -> tuple[str, ...]:
    """도구 요약 조회가 필요한 invocation id (중복 제거, 원래 순서 유지)."""
    seen: dict[str, None] = {}
    for event in events:
        if _is_invoke_row(event) and event.invocation_id:
            seen.setdefault(event.invocation_id, None)
    return tuple(seen)


def collect_audit_tool_usage(
    events: Iterable[AuditEvent],
    *,
    found: dict[str, InvocationUsage],
    unobserved: frozenset[str] = frozenset(),
    unobserved_reason: str = "",
) -> tuple[AuditToolUsage, ...]:
    """`AGENT_INVOKE` 행에만 도구 요약을 붙여요.

    `unobserved` 는 조회 자체가 실패한 invocation 이에요. 「없음」으로 접으면 미관측을 부재로
    주장하게 되니 `unknown` 으로 남겨요.
    """
    rows: list[AuditToolUsage] = []
    for event in events:
        if not _is_invoke_row(event):
            continue
        if not event.invocation_id:
            rows.append(
                AuditToolUsage(
                    event_id=event.event_id,
                    invocation_id="",
                    status="unknown",
                    reason="invocation_id_missing",
                )
            )
            continue
        if event.invocation_id in unobserved:
            rows.append(
                AuditToolUsage(
                    event_id=event.event_id,
                    invocation_id=event.invocation_id,
                    status="unknown",
                    reason=unobserved_reason or "tool_usage_unavailable",
                )
            )
            continue
        rows.append(
            audit_tool_usage_for(event, found.get(event.invocation_id))
        )
    return tuple(rows)


@dataclass(frozen=True)
class AuditTimelinePage:
    items: tuple[AuditEvent, ...]
    next_cursor: str | None
    coverage: AuditTimelineCoverage
    # `items` 중 `AGENT_INVOKE` 행에만 있는 사이드카예요. `event_id` 로 이어 붙여요.
    tool_usage: tuple[AuditToolUsage, ...] = ()


@dataclass(frozen=True)
class AuditEventRead:
    event: AuditEvent | None
    error: str = ""
    event_id: str = ""
    event_type: str = ""
    principal_id: str = ""
    timestamp: str = ""
    policy_hash: str = ""

    @classmethod
    def observed(cls, event: AuditEvent) -> AuditEventRead:
        return cls(
            event=event,
            event_id=event.event_id,
            event_type=event.event_type,
            principal_id=event.principal_id,
            timestamp=event.timestamp,
            policy_hash=event.policy_hash,
        )


@dataclass(frozen=True)
class ManagedClientReservation:
    acquired: bool
    client_id: str = ""
    deleting: bool = False


class IdentityRecordNotFound(LookupError):
    pass


class IdentityVersionConflict(RuntimeError):
    pass


class IdentitySchemaTooNew(RuntimeError):
    """읽은 행에 이 코드가 모르는 속성이 있어요 — 쓴 쪽이 더 새로워요.

    identity 테이블은 **배포 주기가 다른 세 독자**가 같이 읽어요: 로컬 백엔드, 포털 ECS
    task, 그리고 Gateway REQUEST interceptor Lambda. Lambda 는 `cdk deploy` 로만 갱신되니
    로컬이 새 필드를 쓰기 시작하면 Lambda 쪽이 뒤처져요.

    그 상태로 **진행하면 안 돼요.** 모르는 속성이 권한을 좁히는 것일 수도 있어서(취소 사유,
    호출 상한 같은 것) 무시하는 게 곧 권한을 넓히는 게 될 수 있어요. 그래서 조용히 버리지
    않고 여기서 멈춰요 — fail-closed 예요.

    **속성 이름은 비밀이 아니라 스키마**라서 로그에 실어요. 값은 절대 싣지 않아요.
    2026-08-29 에 이 상황이 `failure_type=TypeError` 한 줄로만 남아서, 원인이 낡은 Lambda
    인데 verify 는 "선언된 도구가 없어요" 로 도구 선언을 탓했어요.
    """

    def __init__(self, model: str, unknown: tuple[str, ...]) -> None:
        self.model = model
        self.unknown = unknown
        super().__init__(
            f"{model} 행에 모르는 속성이 있어요: {', '.join(unknown)} — "
            "이 코드가 쓴 쪽보다 낡았어요"
        )


def grant_key(
    *,
    principal_id: str = "",
    subject_group: str = "",
    asset_id: str,
    operation_id: str,
) -> tuple[str, str]:
    """⑦ grant 행의 **파티션과 정렬 키를 함께** 조립해요 — 이 함수가 유일한 출처예요.

    ```
    PK = GROUP#<group>  |  PRINCIPAL#<sub>
    SK = GRANT#<asset_id>#<operation_id>
    ```

    **왜 한 함수인가.** 2026-08-29 에 그룹 grant 를 `PRINCIPAL#` 으로 **쓰고** `GROUP#` 에서
    **읽어서**, 관리 화면(scan)은 「있다」로 보이는데 interceptor 는 전부 거부했어요. 파티션만
    한 곳에 모아도 ADR-0099 가 정렬 키에 의미를 실으니 **같은 사고가 SK 에서 재현될 수
    있어요**(AGENTS.md). writer · 조건부 갱신 · reader · DELETE 가 전부 이 함수를 불러요.

    **메모리 스토어도 이 키로 키잉해요.** `grant_id` 로 키잉하면 같은 도구에 행이 여러 개
    생겨서 메모리만 「둘 다 있다」로 답하고 Dynamo 는 덮어써요 — 그 차이는 실 AWS 에서만
    드러나요.

    **빈 `asset_id`·`operation_id` 를 거부해요.** 두 필드는 옛 행 디코딩용으로 dataclass 기본값
    이 있지만, 키의 재료로는 비어 있으면 안 돼요 — `GRANT##` 같은 행이 생기면 서로 다른 도구가
    한 행으로 뭉쳐서 **부여하지 않은 도구가 열려요.** 조용히 넘기지 않고 여기서 멈춰요.
    """
    asset = asset_id.strip()
    operation = operation_id.strip()
    if not asset or not operation:
        raise ValueError(
            "grant 키에 빈 asset_id·operation_id 를 쓸 수 없어요: "
            f"asset_id={asset_id!r} operation_id={operation_id!r}"
        )
    group = subject_group.strip()
    partition = f"GROUP#{group}" if group else f"PRINCIPAL#{principal_id}"
    return partition, f"GRANT#{asset}#{operation}"


def grant_key_for(grant: AccessGrant) -> tuple[str, str]:
    """`AccessGrant` 한 행의 (PK, SK) — `grant_key` 로 위임해요."""
    return grant_key(
        principal_id=grant.principal_id,
        subject_group=grant.subject_group,
        asset_id=grant.asset_id,
        operation_id=grant.operation_id,
    )


#: 자산 «현재 버전» 행의 정렬 키. 조립은 `asset_version_key` 하나로만 해요.
ASSET_VERSION_SK = "VERSION"


def asset_version_key(asset_id: str) -> tuple[str, str]:
    """`AssetVersionRecord` 한 행의 (PK, SK) — 이 함수가 유일한 출처예요.

    `grant_key` 와 같은 이유로 함수 하나예요. writer(`put_asset_version`) · reader
    (`get_asset_version` — interceptor 가 부르는 그 자리) · DELETE(`delete_asset_version`) 가
    전부 여기를 거쳐야 키가 표류하지 않아요. purge 가 지우는 키와 interceptor 가 읽는 키가
    갈라지면 「지웠다」가 거짓이 되고, 그 상태는 scan 으로 봐야만 드러나요.

    **검증은 일부러 넣지 않았어요.** 빈 `asset_id` 를 `ValueError` 로 막으면 지금 fail-closed
    거부로 끝나는 읽기 경로(`get_asset_version` → `IdentityRecordNotFound` → 거부)가 예외로
    바뀌어요. 이 함수는 조립만 해요.
    """
    return f"ASSET#{asset_id}", ASSET_VERSION_SK


class IdentityStore:
    """DynamoDB adapter와 테스트 double이 공유하는 최소 계약."""

    def put_connection(self, connection: Connection) -> None:
        raise NotImplementedError

    def get_connection(self, connection_id: str) -> Connection:
        raise NotImplementedError

    def list_connections(self) -> list[Connection]:
        raise NotImplementedError

    def put_connection_capabilities(
        self,
        connection_id: str,
        capabilities: Iterable[ConnectionCapability],
        *,
        expected_version: int | None = None,
    ) -> None:
        raise NotImplementedError

    def get_connection_capabilities_version(self, connection_id: str) -> int:
        raise NotImplementedError

    def list_connection_capabilities(
        self, connection_id: str
    ) -> list[ConnectionCapability]:
        raise NotImplementedError

    def put_grant(self, grant: AccessGrant) -> None:
        raise NotImplementedError

    def get_grant(self, grant_id: str) -> AccessGrant:
        """`grant_id` 로 찾아요 — **관리 API 전용이고 인가 경로가 아니에요.**

        ADR-0099 이후 `grant_id` 는 정렬 키에 없어요. 인가 판정은
        `get_tool_grant(asset_id, operation_id, ...)` 로 **정확한 키 하나**를 읽어요.
        """
        raise NotImplementedError

    def get_tool_grant(
        self,
        *,
        asset_id: str,
        operation_id: str,
        principal_id: str = "",
        subject_group: str = "",
    ) -> AccessGrant:
        """⑦ 를 **소비자가 읽는 그대로** — 정확한 키 하나로 읽어요 (ADR-0099).

        인가 경로의 유일한 ⑦ 조회예요. 이 계약을 `list_grants` 로 대체하면 안 돼요: 그쪽은
        `principal_id=None` 일 때 전체 scan 이라 **저장 위치를 무시**해서, 잘못된 키에 있는
        행도 「있다」로 읽혀요(2026-08-29 실사고). 검증도 이 메서드로 해야 같은 질문이에요.
        """
        raise NotImplementedError

    def list_grants(
        self,
        *,
        principal_id: str | None = None,
        connection_id: str | None = None,
        subject_groups: tuple[str, ...] = (),
    ) -> list[AccessGrant]:
        raise NotImplementedError

    def list_group_grants(self, group: str) -> list[AccessGrant]:
        """이 그룹의 grant 를 **소비자가 읽는 그대로** 돌려줘요.

        `list_grants` 는 `principal_id=None` 일 때 전체 scan 이라 저장 위치를 무시해요. 그래서
        grant 가 잘못된 파티션에 있어도 "있다" 로 읽히고, 정작 Gateway interceptor 는 못 찾아요
        (2026-08-29 실사고). 그룹 grant 의 존재를 **판정 근거로 쓰려면** 이 메서드를 써요.

        Dynamo 는 `GROUP#{group}` 파티션만 Query 해요 — 다른 곳에 있는 행은 여기 안 나와요.
        그게 이 메서드의 요점이에요.
        """
        raise NotImplementedError

    def delete_grant_with_audit(
        self,
        *,
        asset_id: str,
        operation_id: str,
        principal_id: str = "",
        subject_group: str = "",
        expected_version: int,
        event: AuditEvent,
    ) -> bool:
        """⑦ 한 행을 **지우고** 같은 쓰기에 감사 이벤트를 남겨요 — 자산 purge 전용이에요.

        키는 `grant_key` 가 조립해요. writer·reader·조건부 갱신이 쓰는 그 함수여야 파티션·SK
        가 표류하지 않아요(2026-08-29 실사고). 특히 `subject_group` 을 안 넘기면 그룹 grant 를
        `PRINCIPAL#` 파티션에서 지우려 해서 **조용히 아무것도 안 지워요.**

        행이 없으면 `False` 예요(멱등). `expected_version` 이 다르면 `IdentityVersionConflict`
        — 그 사이 누가 부여를 갱신했다는 뜻이라 지우지 않아요.

        **왜 `REVOKED` 회수가 아니라 삭제인가**는 `access_grant_cleanup` 모듈 docstring 에
        있어요. 관리 API 의 `revoke_tool_grant` 는 여전히 회수예요 — 자산이 살아 있을 때
        하드 삭제하면 회원 기본 부여가 다음 실행에 되살려서 회수가 무효가 되거든요.
        """
        raise NotImplementedError

    def put_asset_version(self, record: AssetVersionRecord) -> None:
        """자산의 «현재 버전» 행을 써요 — 등록·재등록·명시 동기화만 불러요 (ADR-0099 결정 13)."""
        raise NotImplementedError

    def get_asset_version(self, asset_id: str) -> AssetVersionRecord:
        """④ 버전 대조의 기대값. 없으면 `IdentityRecordNotFound` — 호출자가 **거부**로 바꿔요."""
        raise NotImplementedError

    def delete_asset_version(self, asset_id: str) -> bool:
        """자산 «현재 버전» 행을 지워요 — 자산 purge 전용이에요.

        키는 `asset_version_key` 가 조립해요. `get_asset_version`(interceptor 가 읽는 자리)과
        같은 함수여야 「지웠다」가 거짓이 되지 않아요.

        행이 없으면 `False` 예요 — **멱등이어야 해요.** purge 실패의 처방이 재시도라서, 두 번째
        실행이 예외로 멈추면 나머지 정리가 영원히 안 끝나요.

        **감사 이벤트를 남기지 않아요.** 이 행에는 주체가 없어요 — ⑤·⑦ 은 「누가 무엇을 부를 수
        있었나」라서 승인을 조용히 회수한 기록이 필요하지만, 이 행은 ④ 세대 대조의 기대값
        좌표예요(ADR-0099 결정 13). `AuditEvent` 는 `decision`·`reason` 이 필수라서 남기려면
        일어나지 않은 인가 판정을 감사 타임라인에 적게 돼요. ④ binding 정리
        (`rollback_agent_provisioning`)도 승인 상태를 들고 있으면서 감사 없이 지워요 — 주체 없는
        좌표 행의 선례가 그쪽이에요. 삭제 사실은 purge 보고서의 단계 좌표로 드러나요.
        """
        raise NotImplementedError

    def put_asset_capability(
        self,
        capability: AssetCapability,
        *,
        expected_version: int | None = None,
        expected_capabilities_version: int | None = None,
    ) -> None:
        raise NotImplementedError

    def get_asset_capability(
        self, asset_id: str, operation_id: str
    ) -> AssetCapability:
        raise NotImplementedError

    def list_asset_capabilities(self, asset_id: str) -> list[AssetCapability]:
        raise NotImplementedError

    def list_all_asset_capabilities(self) -> list[AssetCapability]:
        raise NotImplementedError

    def list_all_asset_versions(self) -> list[AssetVersionRecord]:
        """자산별 «현재 버전» 행 전부 — 목록 화면이 ④ 대조를 벌크로 하려고 써요.

        `scan` 이지만 판정 가치가 소비자와 같아요: 파티션이 `ASSET#{asset_id}` 하나로
        결정적이고 주체 분기가 없어서, `GetItem` 과 다른 답이 나올 수 없어요. grant 는
        파티션이 주체에 따라 갈려서 이 논리가 성립하지 않아요(그래서 ⑦은 미뤄요).
        """
        raise NotImplementedError

    def delete_asset_capability_with_audit(
        self,
        asset_id: str,
        operation_id: str,
        *,
        expected_version: int,
        event: AuditEvent,
    ) -> bool:
        raise NotImplementedError

    def approve_asset_capability(
        self,
        asset_id: str,
        operation_id: str,
        *,
        expected_version: int,
        approved_by: str,
        updated_at: str,
    ) -> AssetCapability:
        raise NotImplementedError

    def reject_asset_capability(
        self,
        asset_id: str,
        operation_id: str,
        *,
        expected_version: int,
        updated_at: str,
        expected_capabilities_version: int | None = None,
    ) -> AssetCapability:
        raise NotImplementedError

    def put_delegation(self, context: DelegationContext) -> None:
        raise NotImplementedError

    def get_delegation(self, handle_hash: str) -> DelegationContext:
        raise NotImplementedError

    def claim_workload_nonce(
        self, workload_id: str, nonce: str, expires_at: int
    ) -> bool:
        raise NotImplementedError

    def put_external_workload_identity(
        self, identity: ExternalWorkloadIdentity
    ) -> None:
        raise NotImplementedError

    def get_external_workload_identity(
        self, agent_id: str
    ) -> ExternalWorkloadIdentity:
        raise NotImplementedError

    def delete_external_workload_identity(self, agent_id: str) -> bool:
        raise NotImplementedError

    # ── 신원 변경 + 감사를 한 트랜잭션으로 (원자성) ──────────────────────
    # 신원 평면 변경과 그 감사는 **둘 다 성공하거나 둘 다 실패**해야 해요. 따로 쓰면
    # "변경됐는데 감사 없음"이나 "감사만 있고 변경 없음" 중 하나가 남고, 감사 기록만으로는
    # 실제 상태를 판단할 수 없어요(짝 없는 이벤트가 무엇을 뜻하는지 증명 불가).
    # 신원과 감사가 같은 테이블에 있으니 DynamoDB TransactWriteItems로 원자 커밋해요 —
    # 별도 outbox 재처리기 없이 계약을 만족해요.

    def put_external_workload_identity_with_audit(
        self, identity: ExternalWorkloadIdentity, event: AuditEvent
    ) -> None:
        raise NotImplementedError

    def delete_external_workload_identity_with_audit(
        self, agent_id: str, event: AuditEvent
    ) -> bool:
        raise NotImplementedError

    # ── 축1 M1: agent 신원 기반 tool 인가 원장 ──────────────────────────
    def put_agent_tool_binding(self, binding: AgentToolBinding) -> None:
        raise NotImplementedError

    def get_agent_tool_binding(
        self,
        agent_record_id: str,
        asset_id: str,
        asset_version: str,
        operation_id: str,
    ) -> AgentToolBinding:
        raise NotImplementedError

    def list_agent_tool_bindings(
        self, agent_record_id: str
    ) -> list[AgentToolBinding]:
        raise NotImplementedError

    def set_agent_tool_binding_effective_state(
        self,
        binding: AgentToolBinding,
        *,
        effective_state: EffectiveState,
        policy_revision: int,
    ) -> AgentToolBinding:
        raise NotImplementedError

    def approve_agent_tool_binding(
        self,
        agent_record_id: str,
        asset_id: str,
        asset_version: str,
        operation_id: str,
        *,
        approved_by: str,
        approved_at: str,
    ) -> AgentToolBinding:
        raise NotImplementedError

    def reject_agent_tool_binding(
        self,
        agent_record_id: str,
        asset_id: str,
        asset_version: str,
        operation_id: str,
        *,
        updated_by: str,
        updated_at: str,
    ) -> AgentToolBinding:
        raise NotImplementedError

    def approve_agent_tool_binding_with_audit(
        self,
        agent_record_id: str,
        asset_id: str,
        asset_version: str,
        operation_id: str,
        *,
        approved_by: str,
        approved_at: str,
        sensitivity: str | None = None,
        event: AuditEvent,
    ) -> AgentToolBinding:
        raise NotImplementedError

    def reject_agent_tool_binding_with_audit(
        self,
        agent_record_id: str,
        asset_id: str,
        asset_version: str,
        operation_id: str,
        *,
        updated_by: str,
        updated_at: str,
        event: AuditEvent,
    ) -> AgentToolBinding:
        raise NotImplementedError

    def put_agent_identity_binding(self, binding: AgentIdentityBinding) -> None:
        raise NotImplementedError

    def reserve_managed_client(
        self, agent_record_id: str, reservation_id: str
    ) -> ManagedClientReservation:
        raise NotImplementedError

    def get_managed_client_reservation(
        self,
        agent_record_id: str,
    ) -> ManagedClientReservation | None:
        """삭제·재조정용으로 reservation의 실제 client 좌표를 읽어요."""
        raise NotImplementedError

    def complete_managed_client_reservation(
        self,
        agent_record_id: str,
        reservation_id: str,
        client_id: str,
    ) -> None:
        raise NotImplementedError

    def complete_observed_managed_client_reservation(
        self,
        agent_record_id: str,
        client_id: str,
    ) -> None:
        raise NotImplementedError

    def release_managed_client_reservation(
        self, agent_record_id: str, reservation_id: str
    ) -> None:
        raise NotImplementedError

    def tombstone_managed_client_reservation(
        self,
        agent_record_id: str,
        client_id: str,
    ) -> bool:
        """완료 reservation이 대상 client일 때 삭제 중으로 조건부 전환해요."""
        raise NotImplementedError

    def finalize_managed_client_reservation_deletion(
        self,
        agent_record_id: str,
        client_id: str,
    ) -> bool:
        """일치하는 삭제 중 reservation만 최종 제거해요."""
        raise NotImplementedError

    def get_agent_identity_binding(
        self, agent_record_id: str
    ) -> AgentIdentityBinding:
        raise NotImplementedError

    def find_agent_identity_binding_by_role(
        self, gateway_role_arn: str
    ) -> AgentIdentityBinding | None:
        raise NotImplementedError

    def get_agent_authorization_ledger_snapshot(
        self,
    ) -> AgentAuthorizationLedgerSnapshot:
        raise NotImplementedError

    def delete_agent_authorization_artifacts(self, agent_record_id: str) -> None:
        """agent identity·tool bindings·policy ledger를 함께 제거해요."""
        raise NotImplementedError

    def rollback_agent_provisioning(
        self,
        agent_record_id: str,
        *,
        created_bindings: tuple[dict, ...],
        policy_revision: int | None,
    ) -> None:
        raise NotImplementedError

    def put_agent_policy_deployment(
        self, deployment: AgentPolicyDeployment, *, expected_revision: int
    ) -> None:
        raise NotImplementedError

    def get_latest_agent_policy_deployment(
        self, agent_record_id: str
    ) -> AgentPolicyDeployment | None:
        raise NotImplementedError

    def list_agent_policy_deployments(
        self, agent_record_id: str
    ) -> list[AgentPolicyDeployment]:
        raise NotImplementedError

    def list_all_agent_policy_deployments(
        self,
    ) -> list[AgentPolicyDeployment]:
        raise NotImplementedError

    def mark_agent_policy_deployment(
        self,
        agent_record_id: str,
        revision: int,
        *,
        status: "PolicyDeploymentStatus",
        agentcore_policy_id: str = "",
        deployed_policy_hash: str = "",
        deployed_at: str = "",
        validation_findings: tuple[str, ...] = (),
    ) -> "AgentPolicyDeployment":
        raise NotImplementedError

    def put_gateway_policy_cutover(
        self,
        cutover: GatewayPolicyCutover,
        *,
        expected_revision: int,
    ) -> None:
        raise NotImplementedError

    def update_gateway_policy_cutover(
        self,
        cutover: GatewayPolicyCutover,
        *,
        expected_version: int,
    ) -> None:
        raise NotImplementedError

    def get_latest_gateway_policy_cutover(
        self,
        gateway_arn: str,
    ) -> GatewayPolicyCutover | None:
        raise NotImplementedError

    def list_gateway_policy_cutovers(
        self,
        gateway_arn: str,
    ) -> list[GatewayPolicyCutover]:
        raise NotImplementedError

    # ── 도메인 규칙 Cedar 정책 원장 (`domain_policy.py`) ──────────────────
    def put_domain_policy_rule(self, rule: DomainPolicyRule) -> None:
        raise NotImplementedError

    def get_domain_policy_rule(self, rule_id: str) -> DomainPolicyRule:
        raise NotImplementedError

    def list_domain_policy_rules(self) -> list[DomainPolicyRule]:
        raise NotImplementedError

    def delete_domain_policy_rule(self, rule_id: str) -> bool:
        raise NotImplementedError

    def put_agent_invoke_authorization(
        self, authz: AgentInvokeAuthorization
    ) -> None:
        raise NotImplementedError

    def get_agent_invoke_authorization(
        self, agent_id: str
    ) -> AgentInvokeAuthorization:
        raise NotImplementedError

    def append_audit(self, event: AuditEvent) -> None:
        raise NotImplementedError

    def append_audit_once(
        self,
        event: AuditEvent,
        *,
        idempotency_key: str,
    ) -> bool:
        raise NotImplementedError

    def put_audit_evidence_chunks(
        self,
        invocation_id: str,
        evidence_id: str,
        chunks: tuple[str, ...],
    ) -> None:
        raise NotImplementedError

    def get_audit_evidence_chunks(
        self,
        invocation_id: str,
        evidence_id: str,
        chunk_count: int,
    ) -> tuple[str, ...]:
        raise NotImplementedError

    def put_invocation_usage(self, usage: InvocationUsage) -> None:
        raise NotImplementedError

    def get_invocation_usage(self, invocation_id: str) -> InvocationUsage:
        raise NotImplementedError

    def list_audit(self, invocation_id: str) -> list[AuditEvent]:
        raise NotImplementedError

    def list_audit_reads(self, invocation_id: str) -> list[AuditEventRead]:
        return [
            AuditEventRead.observed(event)
            for event in self.list_audit(invocation_id)
        ]

    def list_audit_timeline(
        self,
        *,
        from_time: str | None = None,
        to_time: str | None = None,
        decision: AuthorizationOutcome | None = None,
        agent_id: str | None = None,
        principal_id: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> AuditTimelinePage:
        raise NotImplementedError


class InMemoryIdentityStore(IdentityStore):
    """테스트용. 허용 판정 로직은 Dynamo 구현과 동일 계약을 사용해요."""

    def __init__(self, *, now=None) -> None:
        self.connections: dict[str, Connection] = {}
        self.connection_capabilities: dict[str, dict[str, ConnectionCapability]] = {}
        # (PK, SK) → grant. **`grant_key` 가 만든 그 키예요** — `grant_id` 로 키잉하면 같은
        # 도구에 행이 여럿 생겨서 메모리만 「둘 다 있다」로 답해요(Dynamo 는 덮어써요).
        self.grants: dict[tuple[str, str], AccessGrant] = {}
        self.asset_capabilities: dict[tuple[str, str], AssetCapability] = {}
        # asset_id → 자산 현재 버전 (ADR-0099 결정 13). ④ 대조의 기대값이에요.
        self.asset_versions: dict[str, AssetVersionRecord] = {}
        self.delegations: dict[str, DelegationContext] = {}
        self.workload_nonces: dict[tuple[str, str], int] = {}
        self.external_workload_identities: dict[str, ExternalWorkloadIdentity] = {}
        self.audit_events: list[AuditEvent] = []
        self._audit_idempotency_keys: set[tuple[str, str]] = set()
        self._audit_evidence_chunks: dict[
            tuple[str, str], tuple[str, ...]
        ] = {}
        self.invocation_usage: dict[str, InvocationUsage] = {}
        self._audit_timeline_cursors: dict[str, dict] = {}
        self._now = now or (lambda: datetime.now(timezone.utc))
        # connection_id → capability 목록 version(교체마다 +1). 없으면 0.
        self.connection_capabilities_version: dict[str, int] = {}
        self._asset_capability_lock = RLock()
        self._asset_version_lock = RLock()
        self._connection_capability_lock = RLock()
        self._grant_lock = RLock()
        self._workload_nonce_lock = RLock()
        self._audit_lock = RLock()
        # 축1 M1 원장. 키는 Dynamo 구현과 동일 계약(문서 §4).
        self.agent_tool_bindings: dict[
            tuple[str, str, str, str], AgentToolBinding
        ] = {}
        self.agent_identity_bindings: dict[str, AgentIdentityBinding] = {}
        self.managed_client_reservations: dict[
            str,
            tuple[str, str, bool],
        ] = {}
        self.agent_client_claims: dict[str, str] = {}
        self.agent_policy_deployments: dict[
            str, dict[int, AgentPolicyDeployment]
        ] = {}
        self.gateway_policy_cutovers: dict[
            str, dict[int, GatewayPolicyCutover]
        ] = {}
        self.agent_invoke_authorizations: dict[str, AgentInvokeAuthorization] = {}
        self.domain_policy_rules: dict[str, DomainPolicyRule] = {}
        self._agent_lock = RLock()

    def put_connection(self, connection: Connection) -> None:
        from .connection_normalize import normalize_connection
        self.connections[connection.connection_id] = normalize_connection(connection)

    def get_connection(self, connection_id: str) -> Connection:
        try:
            return self.connections[connection_id]
        except KeyError as exc:
            raise IdentityRecordNotFound(f"connection not found: {connection_id}") from exc

    def list_connections(self) -> list[Connection]:
        return sorted(self.connections.values(), key=lambda item: item.created_at)

    def put_connection_capabilities(
        self,
        connection_id: str,
        capabilities: Iterable[ConnectionCapability],
        *,
        expected_version: int | None = None,
    ) -> None:
        self.get_connection(connection_id)
        with self._connection_capability_lock:
            current = self.connection_capabilities_version.get(connection_id, 0)
            if expected_version is not None and current != expected_version:
                raise IdentityVersionConflict(
                    f"connection capabilities version changed: {current}"
                )
            self.connection_capabilities[connection_id] = {
                item.name: item for item in capabilities
            }
            self.connection_capabilities_version[connection_id] = current + 1

    def get_connection_capabilities_version(self, connection_id: str) -> int:
        return self.connection_capabilities_version.get(connection_id, 0)

    def list_connection_capabilities(
        self, connection_id: str
    ) -> list[ConnectionCapability]:
        self.get_connection(connection_id)
        return sorted(
            self.connection_capabilities.get(connection_id, {}).values(),
            key=lambda item: item.name,
        )

    def put_grant(self, grant: AccessGrant) -> None:
        """**Dynamo 와 같은 키로** 넣어요 — 같은 (주체, 자산, 도구) 는 한 행이에요.

        `grant_id` 로 키잉하면 같은 도구에 행이 여러 개 생겨서 메모리 스토어만 「둘 다 있다」로
        답해요. Dynamo 는 덮어써요. 그 차이가 parity 테스트를 통과하면서 실 AWS 에서만 다르게
        동작하는 원인이에요.
        """
        key = grant_key_for(grant)
        with self._grant_lock:
            self.grants[key] = grant

    def get_grant(self, grant_id: str) -> AccessGrant:
        for grant in self.grants.values():
            if grant.grant_id == grant_id:
                return grant
        raise IdentityRecordNotFound(f"grant not found: {grant_id}")

    def get_tool_grant(
        self,
        *,
        asset_id: str,
        operation_id: str,
        principal_id: str = "",
        subject_group: str = "",
    ) -> AccessGrant:
        key = grant_key(
            principal_id=principal_id,
            subject_group=subject_group,
            asset_id=asset_id,
            operation_id=operation_id,
        )
        try:
            return self.grants[key]
        except KeyError as exc:
            raise IdentityRecordNotFound(
                f"grant not found: {key[0]} / {key[1]}"
            ) from exc

    def list_grants(
        self,
        *,
        principal_id: str | None = None,
        connection_id: str | None = None,
        subject_groups: tuple[str, ...] = (),
    ) -> list[AccessGrant]:
        """사람 grant + 그룹 grant 합집합. Dynamo 구현과 같은 계약이어야 해요."""
        items = list(self.grants.values())
        if principal_id is not None:
            groups = {g.strip() for g in subject_groups if g and g.strip()}
            items = [
                item
                for item in items
                if item.principal_id == principal_id
                or (item.subject_group and item.subject_group in groups)
            ]
        if connection_id is not None:
            items = [item for item in items if item.connection_id == connection_id]
        return sorted(items, key=lambda item: item.created_at)

    def list_group_grants(self, group: str) -> list[AccessGrant]:
        """메모리에는 파티션이 없으니 `subject_group` 이 곧 파티션이에요.

        Dynamo 쪽은 `GROUP#{group}` 만 Query 해요. 이 구현이 같은 답을 주는 건 메모리에는
        "잘못된 파티션" 이라는 상태가 존재할 수 없기 때문이에요 — 그래서 **파티션 어긋남 회귀는
        Dynamo(moto) 테스트로만 잡혀요**(`test_identity_dynamo_store.py`).
        """
        wanted = group.strip()
        if not wanted:
            return []
        return sorted(
            (g for g in self.grants.values() if g.subject_group == wanted),
            key=lambda item: item.created_at,
        )

    def delete_grant_with_audit(
        self,
        *,
        asset_id: str,
        operation_id: str,
        principal_id: str = "",
        subject_group: str = "",
        expected_version: int,
        event: AuditEvent,
    ) -> bool:
        key = grant_key(
            principal_id=principal_id,
            subject_group=subject_group,
            asset_id=asset_id,
            operation_id=operation_id,
        )
        with self._grant_lock:
            current = self.grants.get(key)
            if current is None:
                return False
            if current.version != expected_version:
                raise IdentityVersionConflict(
                    f"grant version changed: {current.version}"
                )
            del self.grants[key]
            self.audit_events.append(event)
            return True

    def revoke_grant(self, grant_id: str, *, updated_at: str) -> AccessGrant:
        with self._grant_lock:
            current = self.get_grant(grant_id)
            revoked = replace(
                current,
                status=GrantStatus.REVOKED,
                version=current.version + 1,
                updated_at=updated_at,
            )
            self.put_grant(revoked)
            return revoked

    def put_asset_version(self, record: AssetVersionRecord) -> None:
        with self._asset_version_lock:
            self.asset_versions[record.asset_id] = record

    def get_asset_version(self, asset_id: str) -> AssetVersionRecord:
        try:
            return self.asset_versions[asset_id]
        except KeyError as exc:
            raise IdentityRecordNotFound(
                f"asset version not found: {asset_id}"
            ) from exc

    def delete_asset_version(self, asset_id: str) -> bool:
        """메모리에는 파티션이 없어서 `asset_id` 가 곧 키예요 — writer·reader 와 같은 dict 예요.

        Dynamo 쪽은 `asset_version_key` 로 `ASSET#{asset_id}`/`VERSION` 을 지워요. 「지운 키와
        읽는 키가 같은가」는 인메모리로 증명할 수 없어요(잘못된 파티션이라는 상태가 없어서요) —
        그 회귀는 `test_identity_dynamo_store.py`(moto)가 봐요.
        """
        with self._asset_version_lock:
            return self.asset_versions.pop(asset_id, None) is not None

    def put_asset_capability(
        self,
        capability: AssetCapability,
        *,
        expected_version: int | None = None,
        expected_capabilities_version: int | None = None,
    ) -> None:
        with self._connection_capability_lock, self._asset_capability_lock:
            if expected_capabilities_version is not None:
                current_capabilities_version = (
                    self.connection_capabilities_version.get(
                        capability.connection_id, 0
                    )
                )
                if current_capabilities_version != expected_capabilities_version:
                    raise IdentityVersionConflict(
                        "connection capabilities version changed"
                    )
            if expected_version is not None:
                key = (capability.asset_id, capability.operation_id)
                current = self.asset_capabilities.get(key)
                # 신규(current 없음)든 stale이든, expected와 어긋나면 충돌이에요.
                # expected를 준 쪽은 "그 버전을 봤다"고 주장하므로, 그 버전이 실재하지
                # 않으면 stale로 취급해요(신규 생성은 라우터가 expected 없이 호출).
                existing = current.version if current is not None else None
                if existing != expected_version:
                    raise IdentityVersionConflict(
                        f"asset capability version changed: {existing}"
                    )
            self.asset_capabilities[
                (capability.asset_id, capability.operation_id)
            ] = capability

    def get_asset_capability(
        self, asset_id: str, operation_id: str
    ) -> AssetCapability:
        try:
            return self.asset_capabilities[(asset_id, operation_id)]
        except KeyError as exc:
            raise IdentityRecordNotFound(
                f"asset capability not found: {asset_id}/{operation_id}"
            ) from exc

    def list_asset_capabilities(self, asset_id: str) -> list[AssetCapability]:
        return sorted(
            (
                item
                for (stored_asset_id, _), item in self.asset_capabilities.items()
                if stored_asset_id == asset_id
            ),
            key=lambda item: item.operation_id,
        )

    def list_all_asset_capabilities(self) -> list[AssetCapability]:
        return sorted(
            self.asset_capabilities.values(),
            key=lambda item: (item.asset_id, item.operation_id),
        )

    def list_all_asset_versions(self) -> list[AssetVersionRecord]:
        return sorted(self.asset_versions.values(), key=lambda item: item.asset_id)

    def delete_asset_capability_with_audit(
        self,
        asset_id: str,
        operation_id: str,
        *,
        expected_version: int,
        event: AuditEvent,
    ) -> bool:
        key = (asset_id, operation_id)
        with self._asset_capability_lock:
            current = self.asset_capabilities.get(key)
            if current is None:
                return False
            if current.version != expected_version:
                raise IdentityVersionConflict(
                    f"asset capability version changed: {current.version}"
                )
            del self.asset_capabilities[key]
            self.audit_events.append(event)
            return True

    def approve_asset_capability(
        self,
        asset_id: str,
        operation_id: str,
        *,
        expected_version: int,
        approved_by: str,
        updated_at: str,
    ) -> AssetCapability:
        with self._asset_capability_lock:
            current = self.get_asset_capability(asset_id, operation_id)
            if current.version != expected_version:
                raise IdentityVersionConflict(
                    f"asset capability version changed: {current.version}"
                )
            approved = replace(
                current,
                status=AssetCapabilityStatus.APPROVED,
                approved_by=approved_by,
                version=current.version + 1,
                updated_at=updated_at,
            )
            self.asset_capabilities[(asset_id, operation_id)] = approved
            return approved

    def reject_asset_capability(
        self,
        asset_id: str,
        operation_id: str,
        *,
        expected_version: int,
        updated_at: str,
        expected_capabilities_version: int | None = None,
    ) -> AssetCapability:
        with self._connection_capability_lock, self._asset_capability_lock:
            current = self.get_asset_capability(asset_id, operation_id)
            capabilities_version = self.connection_capabilities_version.get(
                current.connection_id, 0
            )
            if (
                current.version != expected_version
                or (
                    expected_capabilities_version is not None
                    and capabilities_version != expected_capabilities_version
                )
            ):
                raise IdentityVersionConflict(
                    "asset capability or connection capabilities version changed"
                )
            rejected = replace(
                current,
                status=AssetCapabilityStatus.REJECTED,
                approved_by=None,
                version=current.version + 1,
                updated_at=updated_at,
            )
            self.asset_capabilities[(asset_id, operation_id)] = rejected
            return rejected

    def put_delegation(self, context: DelegationContext) -> None:
        self.delegations[context.handle_hash] = context

    def get_delegation(self, handle_hash: str) -> DelegationContext:
        try:
            return self.delegations[handle_hash]
        except KeyError as exc:
            raise IdentityRecordNotFound("delegation not found") from exc

    def claim_workload_nonce(
        self, workload_id: str, nonce: str, expires_at: int
    ) -> bool:
        key = (workload_id, nonce)
        with self._workload_nonce_lock:
            if key in self.workload_nonces:
                return False
            self.workload_nonces[key] = expires_at
            return True

    def put_external_workload_identity(
        self, identity: ExternalWorkloadIdentity
    ) -> None:
        self.external_workload_identities[identity.agent_id] = identity

    def get_external_workload_identity(
        self, agent_id: str
    ) -> ExternalWorkloadIdentity:
        try:
            return self.external_workload_identities[agent_id]
        except KeyError as exc:
            raise IdentityRecordNotFound(
                "external workload identity not found"
            ) from exc

    def delete_external_workload_identity(self, agent_id: str) -> bool:
        return self.external_workload_identities.pop(agent_id, None) is not None

    def put_external_workload_identity_with_audit(
        self, identity: ExternalWorkloadIdentity, event: AuditEvent
    ) -> None:
        # 인메모리는 단일 스레드 원자성으로 충분해요(둘 다 실패 없이 함께 적용).
        self.external_workload_identities[identity.agent_id] = identity
        self.audit_events.append(event)

    def delete_external_workload_identity_with_audit(
        self, agent_id: str, event: AuditEvent
    ) -> bool:
        if self.external_workload_identities.pop(agent_id, None) is None:
            return False
        self.audit_events.append(event)
        return True

    # ── 축1 M1 ───────────────────────────────────────────────────────────
    def put_agent_tool_binding(self, binding: AgentToolBinding) -> None:
        key = (
            binding.agent_record_id,
            binding.asset_id,
            binding.asset_version,
            binding.operation_id,
        )
        with self._agent_lock:
            self.agent_tool_bindings[key] = binding

    def get_agent_tool_binding(
        self,
        agent_record_id: str,
        asset_id: str,
        asset_version: str,
        operation_id: str,
    ) -> AgentToolBinding:
        try:
            return self.agent_tool_bindings[
                (agent_record_id, asset_id, asset_version, operation_id)
            ]
        except KeyError as exc:
            raise IdentityRecordNotFound(
                f"agent tool binding not found: {agent_record_id}/{operation_id}"
            ) from exc

    def list_agent_tool_bindings(
        self, agent_record_id: str
    ) -> list[AgentToolBinding]:
        return sorted(
            (
                b
                for (rid, _, _, _), b in self.agent_tool_bindings.items()
                if rid == agent_record_id
            ),
            key=lambda b: (b.asset_id, b.asset_version, b.operation_id),
        )

    def set_agent_tool_binding_effective_state(
        self,
        binding: AgentToolBinding,
        *,
        effective_state: EffectiveState,
        policy_revision: int,
    ) -> AgentToolBinding:
        if binding.policy_revision > policy_revision:
            raise IdentityVersionConflict(
                "tool binding policy revision cannot move backwards"
            )
        key = (
            binding.agent_record_id,
            binding.asset_id,
            binding.asset_version,
            binding.operation_id,
        )
        with self._agent_lock:
            current = self.get_agent_tool_binding(*key)
            if current != binding:
                raise IdentityVersionConflict(
                    "tool binding changed during policy deployment"
                )
            updated = replace(
                current,
                effective_state=effective_state,
                policy_revision=policy_revision,
            )
            self.agent_tool_bindings[key] = updated
            return updated

    def approve_agent_tool_binding(
        self,
        agent_record_id: str,
        asset_id: str,
        asset_version: str,
        operation_id: str,
        *,
        approved_by: str,
        approved_at: str,
    ) -> AgentToolBinding:
        with self._agent_lock:
            current = self.get_agent_tool_binding(
                agent_record_id, asset_id, asset_version, operation_id
            )
            if current.approval_state is not ApprovalState.REQUESTED:
                raise IdentityVersionConflict("tool binding is not REQUESTED")
            approved = replace(
                current,
                approval_state=ApprovalState.APPROVED,
                approved_by=approved_by,
                approved_at=approved_at,
                updated_by=approved_by,
            )
            self.agent_tool_bindings[
                (agent_record_id, asset_id, asset_version, operation_id)
            ] = approved
            return approved

    def reject_agent_tool_binding(
        self,
        agent_record_id: str,
        asset_id: str,
        asset_version: str,
        operation_id: str,
        *,
        updated_by: str,
        updated_at: str,
    ) -> AgentToolBinding:
        with self._agent_lock:
            current = self.get_agent_tool_binding(
                agent_record_id, asset_id, asset_version, operation_id
            )
            rejected = replace(
                current,
                approval_state=ApprovalState.REJECTED,
                desired_state=DesiredState.REVOKED,
                effective_state=EffectiveState.REVOKED,
                updated_by=updated_by,
            )
            self.agent_tool_bindings[
                (agent_record_id, asset_id, asset_version, operation_id)
            ] = rejected
            return rejected

    def approve_agent_tool_binding_with_audit(
        self,
        agent_record_id: str,
        asset_id: str,
        asset_version: str,
        operation_id: str,
        *,
        approved_by: str,
        approved_at: str,
        sensitivity: str | None = None,
        event: AuditEvent,
    ) -> AgentToolBinding:
        with self._agent_lock:
            current = self.get_agent_tool_binding(
                agent_record_id, asset_id, asset_version, operation_id
            )
            if current.approval_state is not ApprovalState.REQUESTED:
                raise IdentityVersionConflict("tool binding is not REQUESTED")
            approved = replace(
                current,
                approval_state=ApprovalState.APPROVED,
                approved_by=approved_by,
                approved_at=approved_at,
                updated_by=approved_by,
                sensitivity=(
                    sensitivity
                    if sensitivity is not None
                    else current.sensitivity
                ),
            )
            # 테스트 double도 영속 저장소와 같은 all-or-nothing 순서를 지켜요. 감사 쓰기가
            # 실패하면 binding 교체 전에 예외가 나므로 호출자가 안전하게 재시도할 수 있어요.
            self.append_audit(event)
            self.agent_tool_bindings[
                (agent_record_id, asset_id, asset_version, operation_id)
            ] = approved
            return approved

    def reject_agent_tool_binding_with_audit(
        self,
        agent_record_id: str,
        asset_id: str,
        asset_version: str,
        operation_id: str,
        *,
        updated_by: str,
        updated_at: str,
        event: AuditEvent,
    ) -> AgentToolBinding:
        with self._agent_lock:
            current = self.get_agent_tool_binding(
                agent_record_id, asset_id, asset_version, operation_id
            )
            if current.approval_state is not ApprovalState.REQUESTED:
                raise IdentityVersionConflict("tool binding is not REQUESTED")
            rejected = replace(
                current,
                approval_state=ApprovalState.REJECTED,
                desired_state=DesiredState.REVOKED,
                effective_state=EffectiveState.REVOKED,
                updated_by=updated_by,
            )
            self.append_audit(event)
            self.agent_tool_bindings[
                (agent_record_id, asset_id, asset_version, operation_id)
            ] = rejected
            return rejected

    def put_agent_identity_binding(self, binding: AgentIdentityBinding) -> None:
        with self._agent_lock:
            for other_id, existing in self.agent_identity_bindings.items():
                same_client = (
                    binding.client_id
                    and existing.client_id == binding.client_id
                )
                same_legacy_role = (
                    not binding.client_id
                    and binding.gateway_role_arn
                    and existing.gateway_role_arn == binding.gateway_role_arn
                )
                if other_id != binding.agent_record_id and (
                    same_client or same_legacy_role
                ):
                    raise IdentityVersionConflict(
                        "identity already bound to another agent"
                    )
            self.agent_identity_bindings[binding.agent_record_id] = binding
            if binding.client_id:
                self.agent_client_claims[binding.client_id] = (
                    binding.agent_record_id
                )

    def reserve_managed_client(
        self, agent_record_id: str, reservation_id: str
    ) -> ManagedClientReservation:
        with self._agent_lock:
            current = self.managed_client_reservations.get(agent_record_id)
            if current is None:
                self.managed_client_reservations[agent_record_id] = (
                    reservation_id,
                    "",
                    False,
                )
                return ManagedClientReservation(acquired=True)
            owner_id, client_id, deleting = current
            return ManagedClientReservation(
                acquired=(
                    owner_id == reservation_id
                    and not client_id
                    and not deleting
                ),
                client_id="" if deleting else client_id,
                deleting=deleting,
            )

    def get_managed_client_reservation(
        self,
        agent_record_id: str,
    ) -> ManagedClientReservation | None:
        with self._agent_lock:
            current = self.managed_client_reservations.get(agent_record_id)
            if current is None:
                return None
            _, client_id, deleting = current
            return ManagedClientReservation(
                acquired=False,
                client_id=client_id,
                deleting=deleting,
            )

    def complete_managed_client_reservation(
        self,
        agent_record_id: str,
        reservation_id: str,
        client_id: str,
    ) -> None:
        with self._agent_lock:
            current = self.managed_client_reservations.get(agent_record_id)
            if (
                current is None
                or current[0] != reservation_id
                or current[2]
            ):
                raise IdentityVersionConflict(
                    "managed client reservation is owned by another request"
                )
            if current[1] and current[1] != client_id:
                raise IdentityVersionConflict(
                    "managed client reservation already has a different client"
                )
            self.managed_client_reservations[agent_record_id] = (
                reservation_id,
                client_id,
                False,
            )

    def complete_observed_managed_client_reservation(
        self,
        agent_record_id: str,
        client_id: str,
    ) -> None:
        with self._agent_lock:
            current = self.managed_client_reservations.get(agent_record_id)
            if (
                current is None
                or current[2]
                or (current[1] and current[1] != client_id)
            ):
                raise IdentityVersionConflict(
                    "managed client reservation cannot adopt observed client"
                )
            self.managed_client_reservations[agent_record_id] = (
                current[0],
                client_id,
                False,
            )

    def release_managed_client_reservation(
        self, agent_record_id: str, reservation_id: str
    ) -> None:
        with self._agent_lock:
            current = self.managed_client_reservations.get(agent_record_id)
            if (
                current is None
                or current[0] != reservation_id
                or current[1]
                or current[2]
            ):
                raise IdentityVersionConflict(
                    "managed client reservation cannot be released"
                )
            del self.managed_client_reservations[agent_record_id]

    def tombstone_managed_client_reservation(
        self,
        agent_record_id: str,
        client_id: str,
    ) -> bool:
        with self._agent_lock:
            current = self.managed_client_reservations.get(agent_record_id)
            if current is None or current[1] != client_id:
                return False
            self.managed_client_reservations[agent_record_id] = (
                current[0],
                current[1],
                True,
            )
            return True

    def finalize_managed_client_reservation_deletion(
        self,
        agent_record_id: str,
        client_id: str,
    ) -> bool:
        with self._agent_lock:
            current = self.managed_client_reservations.get(agent_record_id)
            if (
                current is None
                or current[1] != client_id
                or not current[2]
            ):
                return False
            del self.managed_client_reservations[agent_record_id]
            return True

    def get_agent_identity_binding(
        self, agent_record_id: str
    ) -> AgentIdentityBinding:
        try:
            return self.agent_identity_bindings[agent_record_id]
        except KeyError as exc:
            raise IdentityRecordNotFound(
                f"agent identity binding not found: {agent_record_id}"
            ) from exc

    def find_agent_identity_binding_by_role(
        self, gateway_role_arn: str
    ) -> AgentIdentityBinding | None:
        for existing in self.agent_identity_bindings.values():
            if existing.gateway_role_arn == gateway_role_arn:
                return existing
        return None

    def get_agent_authorization_ledger_snapshot(
        self,
    ) -> AgentAuthorizationLedgerSnapshot:
        with self._agent_lock:
            return AgentAuthorizationLedgerSnapshot(
                identities=tuple(
                    self.agent_identity_bindings[record_id]
                    for record_id in sorted(self.agent_identity_bindings)
                ),
                tool_bindings=tuple(sorted(
                    self.agent_tool_bindings.values(),
                    key=lambda binding: (
                        binding.agent_record_id,
                        binding.asset_id,
                        binding.asset_version,
                        binding.operation_id,
                    ),
                )),
                policy_deployments=tuple(
                    deployment
                    for record_id in sorted(self.agent_policy_deployments)
                    for deployment in self.list_agent_policy_deployments(
                        record_id
                    )
                ),
                client_claims=tuple(
                    AgentClientClaim(
                        client_id=client_id,
                        agent_record_id=self.agent_client_claims[client_id],
                    )
                    for client_id in sorted(self.agent_client_claims)
                ),
            )

    def delete_agent_authorization_artifacts(self, agent_record_id: str) -> None:
        with self._agent_lock:
            identity = self.agent_identity_bindings.pop(agent_record_id, None)
            if identity is not None and identity.client_id:
                self.agent_client_claims.pop(identity.client_id, None)
            self.managed_client_reservations.pop(agent_record_id, None)
            self.agent_policy_deployments.pop(agent_record_id, None)
            self.agent_invoke_authorizations.pop(agent_record_id, None)
            for key in list(self.agent_tool_bindings):
                if key[0] == agent_record_id:
                    del self.agent_tool_bindings[key]

    def rollback_agent_provisioning(
        self,
        agent_record_id: str,
        *,
        created_bindings: tuple[dict, ...],
        policy_revision: int | None,
    ) -> None:
        with self._agent_lock:
            for binding in created_bindings:
                self.agent_tool_bindings.pop(
                    (
                        agent_record_id,
                        binding["asset_id"],
                        binding["asset_version"],
                        binding["operation_id"],
                    ),
                    None,
                )
            if policy_revision is not None:
                revisions = self.agent_policy_deployments.get(
                    agent_record_id, {}
                )
                revisions.pop(policy_revision, None)
                if not revisions:
                    self.agent_policy_deployments.pop(agent_record_id, None)

    def put_agent_policy_deployment(
        self, deployment: AgentPolicyDeployment, *, expected_revision: int
    ) -> None:
        with self._agent_lock:
            revisions = self.agent_policy_deployments.setdefault(
                deployment.agent_record_id, {}
            )
            current_latest = max(revisions) if revisions else 0
            if current_latest != expected_revision:
                raise IdentityVersionConflict(
                    f"policy revision changed: {current_latest}"
                )
            revisions[deployment.revision] = deployment

    def get_latest_agent_policy_deployment(
        self, agent_record_id: str
    ) -> AgentPolicyDeployment | None:
        revisions = self.agent_policy_deployments.get(agent_record_id, {})
        if not revisions:
            return None
        return revisions[max(revisions)]

    def list_agent_policy_deployments(
        self, agent_record_id: str
    ) -> list[AgentPolicyDeployment]:
        revisions = self.agent_policy_deployments.get(agent_record_id, {})
        return [revisions[r] for r in sorted(revisions)]

    def list_all_agent_policy_deployments(
        self,
    ) -> list[AgentPolicyDeployment]:
        return [
            deployment
            for agent_record_id in sorted(self.agent_policy_deployments)
            for deployment in self.list_agent_policy_deployments(agent_record_id)
        ]

    def mark_agent_policy_deployment(
        self, agent_record_id, revision, *, status,
        agentcore_policy_id="", deployed_policy_hash="",
        deployed_at="", validation_findings=(),
    ):
        with self._agent_lock:
            # agent_policy_deployments 는 중첩 dict: {agent_record_id: {revision: dep}}
            # (store.py:277 `dict[str, dict[int, AgentPolicyDeployment]]`).
            by_revision = self.agent_policy_deployments.get(agent_record_id, {})
            current = by_revision.get(revision)
            if current is None:
                raise IdentityRecordNotFound(
                    f"policy deployment not found: {agent_record_id} r{revision}"
                )
            updated = replace(
                current,
                status=status,
                agentcore_policy_id=agentcore_policy_id or current.agentcore_policy_id,
                deployed_policy_hash=(
                    deployed_policy_hash or current.deployed_policy_hash
                ),
                deployed_at=deployed_at or current.deployed_at,
                validation_findings=(
                    tuple(validation_findings)
                    if status is PolicyDeploymentStatus.ACTIVE
                    else tuple(validation_findings) or current.validation_findings
                ),
            )
            by_revision[revision] = updated
            self.agent_policy_deployments[agent_record_id] = by_revision
            return updated

    def put_gateway_policy_cutover(
        self,
        cutover: GatewayPolicyCutover,
        *,
        expected_revision: int,
    ) -> None:
        with self._agent_lock:
            revisions = self.gateway_policy_cutovers.setdefault(
                cutover.gateway_arn,
                {},
            )
            current_revision = max(revisions, default=0)
            if (
                current_revision != expected_revision
                or cutover.revision != expected_revision + 1
                or cutover.version != 1
            ):
                raise IdentityVersionConflict(
                    "Gateway policy cutover revision changed"
                )
            revisions[cutover.revision] = cutover

    def update_gateway_policy_cutover(
        self,
        cutover: GatewayPolicyCutover,
        *,
        expected_version: int,
    ) -> None:
        with self._agent_lock:
            revisions = self.gateway_policy_cutovers.get(
                cutover.gateway_arn,
                {},
            )
            current = revisions.get(cutover.revision)
            if (
                current is None
                or current.version != expected_version
                or cutover.version != expected_version + 1
            ):
                raise IdentityVersionConflict(
                    "Gateway policy cutover version changed"
                )
            revisions[cutover.revision] = cutover

    def get_latest_gateway_policy_cutover(
        self,
        gateway_arn: str,
    ) -> GatewayPolicyCutover | None:
        cutovers = self.list_gateway_policy_cutovers(gateway_arn)
        return cutovers[-1] if cutovers else None

    def list_gateway_policy_cutovers(
        self,
        gateway_arn: str,
    ) -> list[GatewayPolicyCutover]:
        revisions = self.gateway_policy_cutovers.get(gateway_arn, {})
        return [revisions[revision] for revision in sorted(revisions)]

    def put_domain_policy_rule(self, rule: DomainPolicyRule) -> None:
        with self._agent_lock:
            self.domain_policy_rules[rule.rule_id] = rule

    def get_domain_policy_rule(self, rule_id: str) -> DomainPolicyRule:
        try:
            return self.domain_policy_rules[rule_id]
        except KeyError as exc:
            raise IdentityRecordNotFound(
                f"domain policy rule {rule_id}"
            ) from exc

    def list_domain_policy_rules(self) -> list[DomainPolicyRule]:
        with self._agent_lock:
            return list(self.domain_policy_rules.values())

    def delete_domain_policy_rule(self, rule_id: str) -> bool:
        with self._agent_lock:
            return self.domain_policy_rules.pop(rule_id, None) is not None

    def put_agent_invoke_authorization(
        self, authz: AgentInvokeAuthorization
    ) -> None:
        with self._agent_lock:
            self.agent_invoke_authorizations[authz.agent_id] = authz

    def get_agent_invoke_authorization(
        self, agent_id: str
    ) -> AgentInvokeAuthorization:
        try:
            return self.agent_invoke_authorizations[agent_id]
        except KeyError as exc:
            raise IdentityRecordNotFound(
                f"agent invoke authorization not found: {agent_id}"
            ) from exc

    def append_audit(self, event: AuditEvent) -> None:
        self.audit_events.append(event)

    def append_audit_once(
        self,
        event: AuditEvent,
        *,
        idempotency_key: str,
    ) -> bool:
        key = (event.invocation_id, idempotency_key)
        with self._audit_lock:
            if key in self._audit_idempotency_keys:
                return False
            self.audit_events.append(event)
            self._audit_idempotency_keys.add(key)
            return True

    def put_audit_evidence_chunks(
        self,
        invocation_id: str,
        evidence_id: str,
        chunks: tuple[str, ...],
    ) -> None:
        if not invocation_id or not evidence_id or not chunks:
            raise ValueError("invalid audit evidence coordinates")
        key = (invocation_id, evidence_id)
        with self._audit_lock:
            existing = self._audit_evidence_chunks.get(key)
            if existing is not None and existing != chunks:
                raise IdentityVersionConflict(
                    "audit evidence content changed"
                )
            self._audit_evidence_chunks[key] = chunks

    def get_audit_evidence_chunks(
        self,
        invocation_id: str,
        evidence_id: str,
        chunk_count: int,
    ) -> tuple[str, ...]:
        if chunk_count < 1:
            raise ValueError("invalid audit evidence chunk count")
        try:
            chunks = self._audit_evidence_chunks[
                (invocation_id, evidence_id)
            ]
        except KeyError as exc:
            raise IdentityRecordNotFound(
                f"audit evidence not found: {evidence_id}"
            ) from exc
        if len(chunks) != chunk_count:
            raise IdentityVersionConflict(
                "audit evidence chunk count changed"
            )
        return chunks

    def put_invocation_usage(self, usage: InvocationUsage) -> None:
        self.invocation_usage[usage.invocation_id] = usage

    def get_invocation_usage(self, invocation_id: str) -> InvocationUsage:
        try:
            return self.invocation_usage[invocation_id]
        except KeyError as exc:
            raise IdentityRecordNotFound(
                f"invocation usage not found: {invocation_id}"
            ) from exc

    def list_audit(self, invocation_id: str) -> list[AuditEvent]:
        return sorted(
            (
                event
                for event in self.audit_events
                if event.invocation_id == invocation_id
            ),
            key=lambda event: (event.timestamp, event.event_id),
        )

    def list_audit_timeline(
        self,
        *,
        from_time: str | None = None,
        to_time: str | None = None,
        decision: AuthorizationOutcome | None = None,
        agent_id: str | None = None,
        principal_id: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> AuditTimelinePage:
        state = None
        if cursor is not None:
            state = self._audit_timeline_cursors.get(cursor)
            if state is None:
                raise ValueError("invalid audit timeline cursor")

        if state is None:
            page_limit = 50 if limit is None else limit
            if page_limit < 1 or page_limit > 200:
                raise ValueError("audit timeline limit must be between 1 and 200")
            lower, upper = resolve_audit_range(
                from_time,
                to_time,
                now=self._now(),
            )
            query = {
                "from_time": format_audit_timestamp(lower),
                "to_time": format_audit_timestamp(upper),
                "decision": decision.value if decision else "",
                "agent_id": agent_id or "",
                "principal_id": principal_id or "",
                "limit": page_limit,
            }
            matching = [
                event
                for event in self.audit_events
                if lower <= parse_audit_timestamp(event.timestamp) < upper
                and (decision is None or event.decision is decision)
                and (not agent_id or event.agent_id == agent_id)
                and (not principal_id or event.principal_id == principal_id)
            ]
            matching.sort(
                key=lambda event: (
                    parse_audit_timestamp(event.timestamp),
                    event.event_id,
                ),
                reverse=True,
            )
            state = {"query": query, "items": tuple(matching), "offset": 0}
        else:
            query = state["query"]
            supplied = {
                "from_time": (
                    format_audit_timestamp(parse_audit_timestamp(from_time))
                    if from_time
                    else None
                ),
                "to_time": (
                    format_audit_timestamp(parse_audit_timestamp(to_time))
                    if to_time
                    else None
                ),
                "decision": decision.value if decision else None,
                "agent_id": agent_id,
                "principal_id": principal_id,
                "limit": limit,
            }
            if any(
                value is not None and value != query[name]
                for name, value in supplied.items()
            ):
                raise ValueError("audit timeline cursor query does not match")
            resolve_audit_range(
                query["from_time"],
                query["to_time"],
                now=self._now(),
            )

        start = state["offset"]
        end = start + state["query"]["limit"]
        items = state["items"][start:end]
        next_cursor = None
        if end < len(state["items"]):
            next_cursor = f"v1.{uuid.uuid4().hex}"
            self._audit_timeline_cursors[next_cursor] = {
                **state,
                "offset": end,
            }
        return AuditTimelinePage(
            items=items,
            next_cursor=next_cursor,
            coverage=AuditTimelineCoverage(status="ok"),
            tool_usage=collect_audit_tool_usage(
                items,
                found={
                    invocation_id: self.invocation_usage[invocation_id]
                    for invocation_id in audit_tool_usage_invocation_ids(items)
                    if invocation_id in self.invocation_usage
                },
            ),
        )
