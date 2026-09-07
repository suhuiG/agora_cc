"""자산 purge 가 남기던 ⑦ access grant 를 회수하고 감사 기록을 남겨요.

## 무엇이 문제였나 (2026-09-06 실측)

MCP 자산을 purge 하면 Registry 레코드 · Gateway Target · Cedar 공유 정책 열거 · ④ agent tool
binding · ⑤ AssetCapability 는 전부 정리되는데, **⑦ access grant 만 그대로 남았어요.** purge
의 어느 단계도 grant 계열 메서드(`put_grant`·`get_tool_grant`·`list_grants`·
`list_group_grants`)를 부르지 않았거든요.

**이건 인가 구멍이 아니라 위생 결함이에요.** interceptor 는 ④ binding 을 먼저 찾고 그
`binding.asset_id` 로 ⑦ 를 정확 키 조회해요 — ④ 가 0행이면 ⑦ 는 읽히지 않아요. Gateway
Target 도 없고 공유 정책 열거에도 없어서 `tools/list` 에조차 안 나와요. 남는 건 **원장이
실체와 어긋난 상태** 하나예요.

## 왜 회수(`REVOKED`)가 아니라 삭제인가

`revoke_tool_grant`(관리 API)는 일부러 **삭제하지 않고** `REVOKED` 로 내려요. 자산이 살아
있을 때 하드 삭제하면 회원 기본 READ 자동 부여(`catalog_read_access.ensure_member_tool_grants`)
가 다음 실행에 같은 행을 되살려서 회수가 조용히 무효가 되거든요(ADR-0099 §6.1).

**purge 에는 그 이유가 성립하지 않아요.** 자동 부여는 `asset_id`(= Registry `record_id`) 로
행을 만들고, purge 는 그 레코드를 지워요. 재등록은 새 `record_id` 를 받으니 같은 키로 되살아날
경로가 없어요(ADR-0090: 재등록은 승인을 물려받지 않아요). 그래서 삭제를 무효화하는 힘이
없어요.

그리고 옆 단계의 선례가 삭제예요 — ④ binding 은 `rollback_agent_provisioning` 으로 **삭제**
되고, ⑤ AssetCapability 는 `delete_asset_capability_with_audit` 로 **삭제 + 감사**돼요. ⑦ 만
남기면 사라진 자산을 가리키는 행이 원장에 영구히 쌓여요(자산을 지울수록 단조 증가해요).

## 감사 요구사항은 삭제로 잃지 않아요

「그 자산의 권한을 누가 갖고 있었나」는 삭제 뒤에도 물을 수 있어야 해요. 그래서 행을 지우는
같은 쓰기에 `AuditEvent` 를 남겨요(`delete_grant_with_audit`). 주체는
`failure_type` 의 `group:<name>` · `principal:<sub>` 라벨로 남아요 — `principal_id` 는 「누가
purge 했나」(actor)라서 주체를 거기 실으면 두 사실이 한 칸에서 섞여요.

`DecisionReason` 에 새 값을 만들지 않아요. 배포된 Lambda 두 개(runtime-authorizer ·
gateway-interceptor)가 `DecisionReason(data["reason"])` 로 디코딩해서, 새 값을 쓰면 낡은
Lambda 가 감사 행을 읽다 터져요. 기존 `CAPABILITY_NOT_GRANTED` 를 써요.

## 어떻게 찾나 — 소비자가 읽는 방식으로

⑦ 는 `PK=<주체>` · `SK=GRANT#<asset_id>#<operation_id>` 라 **주체가 파티션 키**예요. 「이
자산의 모든 grant」는 파티션을 가로질러요.

- **그룹 축은 `list_group_grants(group)`** — `GROUP#{group}` 파티션 Query 예요. interceptor 가
  읽는 그 자리이고, 그룹은 `PLATFORM_ROLES` 로 좁혀져 있어요(`_validated_grant_subject`).
  이 축을 빼면 라이브 잔재의 대부분을 놓쳐요.
- **사람 축은 `list_grants()`** — 파티션이 주체별로 갈려서 열거 경로가 scan 뿐이에요. 그래서
  scan 이 찾은 행을 **`get_tool_grant` 로 다시 확인**하고 나서 지워요. scan 은 잘못된 키의
  행도 「있다」로 보여주거든요(2026-08-29 실사고).
- `asset_id` 는 항목의 `data` Map 안에 있어요. 최상위 속성으로 읽으면 못 찾아요 — 여기서는
  스토어가 `data` 를 디코딩한 `AccessGrant` 를 받으니 `grant.asset_id` 가 안전하고, 그
  값으로 만든 정확 키가 실제로 그 행을 가리키는지는 `get_tool_grant` 가 확인해줘요.

## 관측 실패를 「없음」으로 접지 않아요

조회가 하나라도 실패하면 `observed=False` 로 돌려요. 호출자(purge)는 그걸 실패로 다뤄
Registry 레코드를 **보존**해요 — 레코드가 사라지면 재시도 좌표를 잃어요.
"""
from __future__ import annotations

import logging

from ...shared.access_grant_cleanup import (
    AccessGrantCleanupReport,
    GrantCoordinate,
)
from .models import (
    PLATFORM_ROLES,
    AccessGrant,
    AuditEvent,
    AuthorizationOutcome,
    DecisionReason,
)
from .store import IdentityRecordNotFound

_log = logging.getLogger(__name__)

#: 행 하나를 가리키는 내부 키 — 스토어의 `grant_key` 재료와 같은 네 값이에요.
_GrantKey = tuple[str, str, str, str]


class _ObservationFailed(RuntimeError):
    """원장을 읽지 못했어요. 「0행」과 구분해야 하니 예외로 갈라요."""


class IdentityAccessGrantCleaner:
    def __init__(self, store, *, now, new_id) -> None:
        self._store = store
        self._now = now
        self._new_id = new_id

    def delete_record_grants(
        self,
        record_ids: tuple[str, ...],
        *,
        actor: str,
    ) -> AccessGrantCleanupReport:
        wanted = {str(record_id) for record_id in record_ids if record_id}
        if not wanted:
            return AccessGrantCleanupReport(observed=True)

        try:
            candidates = self._discover(wanted)
        except _ObservationFailed as exc:
            return AccessGrantCleanupReport(observed=False, reason=str(exc))

        deleted: list[GrantCoordinate] = []
        unknown: dict[_GrantKey, GrantCoordinate] = {}
        for key in sorted(candidates):
            grant = candidates[key]
            coordinate = _coordinate(grant)
            try:
                self._delete_one(grant, actor=actor)
            except Exception as exc:  # noqa: BLE001 - 좌표를 보고서에 남겨요.
                unknown[key] = coordinate
                reason = f"delete_failed:{type(exc).__name__}:{coordinate[0]}"
                self._record_unknown_quietly(
                    grant.asset_id,
                    actor=actor,
                    reason=reason,
                )
                continue
            deleted.append(coordinate)

        # 「받아들여졌다」는 「끝났다」가 아니에요 — 처음과 **같은 발견식**으로 다시 훑어
        # 잔재를 확인해요. 지운 행 목록을 근거로 삼으면 파티션·SK 표류가 있을 때 고아를
        # 성공으로 기록하게 돼요.
        try:
            leftover = self._discover(wanted)
        except _ObservationFailed as exc:
            return AccessGrantCleanupReport(
                observed=False,
                deleted=tuple(deleted),
                unknown=tuple(unknown.values()),
                reason=str(exc),
            )
        for key in sorted(leftover):
            if key in unknown:
                continue
            unknown[key] = _coordinate(leftover[key])

        return AccessGrantCleanupReport(
            observed=True,
            deleted=tuple(deleted),
            unknown=tuple(unknown[key] for key in sorted(unknown)),
        )

    # ── 발견 ────────────────────────────────────────────────────────────────
    def _discover(self, wanted: set[str]) -> dict[_GrantKey, AccessGrant]:
        """이 자산들을 가리키는 ⑦ 행을 두 축에서 모아요.

        그룹 축이 먼저예요 — 소비자가 읽는 파티션 Query 라 발견의 근거가 더 강해요. 사람 축
        scan 은 같은 행을 다시 덮지 않아요(`setdefault`).
        """
        found: dict[_GrantKey, AccessGrant] = {}
        for group in PLATFORM_ROLES:
            try:
                rows = self._store.list_group_grants(group)
            except Exception as exc:  # noqa: BLE001 - 관측 실패로 접어요.
                raise _ObservationFailed(
                    f"group_query_failed:{group}:{type(exc).__name__}"
                ) from exc
            for grant in rows:
                if grant.asset_id in wanted:
                    found[_grant_key(grant)] = grant
        try:
            everywhere = self._store.list_grants()
        except Exception as exc:  # noqa: BLE001 - 관측 실패로 접어요.
            raise _ObservationFailed(
                f"principal_scan_failed:{type(exc).__name__}"
            ) from exc
        for grant in everywhere:
            if grant.asset_id in wanted:
                found.setdefault(_grant_key(grant), grant)
        return found

    # ── 삭제 ────────────────────────────────────────────────────────────────
    def _delete_one(self, grant: AccessGrant, *, actor: str) -> None:
        # scan 이 찾은 행을 interceptor 가 읽는 정확한 키로 다시 봐요. 여기서
        # `IdentityRecordNotFound` 면 그 행의 저장 위치가 `data` 와 어긋난다는 뜻이라
        # **지우지 않고** unknown 으로 남겨요(추측으로 지우면 다른 행을 지울 수 있어요).
        confirmed = self._store.get_tool_grant(
            asset_id=grant.asset_id,
            operation_id=grant.operation_id,
            principal_id=grant.principal_id,
            subject_group=grant.subject_group,
        )
        self._store.delete_grant_with_audit(
            asset_id=grant.asset_id,
            operation_id=grant.operation_id,
            principal_id=grant.principal_id,
            subject_group=grant.subject_group,
            expected_version=confirmed.version,
            event=self._deleted_event(grant, actor=actor),
        )
        try:
            self._store.get_tool_grant(
                asset_id=grant.asset_id,
                operation_id=grant.operation_id,
                principal_id=grant.principal_id,
                subject_group=grant.subject_group,
            )
        except IdentityRecordNotFound:
            return
        raise RuntimeError(
            "삭제한 ⑦ 행이 소비자 키에서 아직 읽혀요: "
            f"{_coordinate(grant)}"
        )

    def record_unknown(
        self,
        record_id: str,
        *,
        actor: str,
        reason: str,
    ) -> None:
        self._store.append_audit(
            self._event(
                asset_id=record_id,
                operation_id="",
                actor=actor,
                event_type="ACCESS_GRANT_ASSET_PURGE_CLEANUP_UNKNOWN",
                failure_type=reason,
            )
        )

    def _record_unknown_quietly(
        self,
        record_id: str,
        *,
        actor: str,
        reason: str,
    ) -> None:
        try:
            self.record_unknown(record_id, actor=actor, reason=reason)
        except Exception:  # noqa: BLE001 - 보고서·로그가 좌표를 들고 있어요.
            _log.exception(
                "access grant purge unknown audit failed; "
                "record_id=%s actor=%s reason=%s",
                record_id,
                actor,
                reason,
            )

    # ── 감사 ────────────────────────────────────────────────────────────────
    def _deleted_event(self, grant: AccessGrant, *, actor: str) -> AuditEvent:
        return self._event(
            asset_id=grant.asset_id,
            operation_id=grant.operation_id,
            actor=actor,
            event_type="ACCESS_GRANT_ASSET_PURGE_DELETED",
            failure_type=(
                f"ASSET_PURGE_REVOKES_ACCESS_GRANT:{_subject_label(grant)}"
            ),
            connection_id=grant.connection_id,
            capabilities=grant.capabilities,
        )

    def _event(
        self,
        *,
        asset_id: str,
        operation_id: str,
        actor: str,
        event_type: str,
        failure_type: str,
        connection_id: str = "",
        capabilities: tuple[str, ...] = (),
    ) -> AuditEvent:
        return AuditEvent(
            event_id=self._new_id(),
            invocation_id=f"asset-purge:{asset_id}",
            # 누가 purge 했나예요. 권한을 갖고 있던 **주체**는 `failure_type` 라벨이에요 —
            # 그룹 grant 는 `principal_id` 가 비어 있어서 한 칸에 두 사실을 실을 수 없어요.
            principal_id=actor,
            agent_id="",
            asset_id=asset_id,
            operation_id=operation_id,
            connection_id=connection_id,
            capabilities=capabilities,
            decision=AuthorizationOutcome.DENY,
            # 새 `DecisionReason` 값을 만들지 않아요 — 배포된 Lambda 두 개가 이 값을
            # enum 으로 디코딩해서, 모르는 값을 만나면 감사 행 읽기가 터져요.
            reason=DecisionReason.CAPABILITY_NOT_GRANTED,
            target="",
            timestamp=self._now(),
            workload_id="",
            event_type=event_type,
            failure_type=failure_type,
        )


def _grant_key(grant: AccessGrant) -> _GrantKey:
    return (
        grant.principal_id,
        grant.subject_group,
        grant.asset_id,
        grant.operation_id,
    )


def _subject_label(grant: AccessGrant) -> str:
    if grant.subject_group:
        return f"group:{grant.subject_group}"
    return f"principal:{grant.principal_id}"


def _coordinate(grant: AccessGrant) -> GrantCoordinate:
    return (_subject_label(grant), grant.asset_id, grant.operation_id)
