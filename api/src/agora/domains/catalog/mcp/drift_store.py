"""MCP 도구 드리프트 원장 스토어.

카탈로그 테이블의 `MCPDRIFT#` 키 공간을 써요 — 새 테이블·새 `[S]` 좌표를 만들지 않아요
(`AUX#`가 같은 테이블을 쓰는 것과 같은 방식이에요).

    PK = "MCPDRIFT#{asset_key}"     SK = "META"                         관측 메타 + snapshot 포인터
    PK = "MCPDRIFT#{asset_key}"     SK = "LEDGER#{id}#TOOL#{tool_name}" 불변 도구 snapshot
    PK = "MCPDRIFT#{asset_key}"     SK = "TOOL#{tool_name}"             rolling 호환 mirror
    PK = "MCPDRIFTLOG#{asset_key}"  SK = "{at}#{event_id}"   민감도 변경 이력(append-only)

이력은 **다른 파티션**이에요. 같은 파티션에 쌓으면 원장 한 번 읽을 때마다 이력 전체를
같이 읽게 되고(원장 조회는 화면을 열 때마다 일어나요), 이력이 길어질수록 목록 조회가
느려져요. 이력은 대화상자를 열 때만 읽어요.

`asset_key`가 **이름 기반**인 게 핵심이에요. `record_id`를 키로 쓰면 재등록할 때마다
원장이 고아가 돼요(IH-22). 도구 하나가 아이템 하나라, 도구가 많아도 400KB 아이템
한도에 걸리지 않아요.

도구 최대 200개는 DynamoDB transaction 100-item 한도를 넘을 수 있어요. writer는 고유 ID
아래 snapshot을 먼저 완성하고 `META`의 포인터만 CAS로 바꿔요. reader는 포인터 전후를 강한
일관성으로 재확인해 새 메타와 옛 도구 행을 섞지 않아요. `TOOL#` mirror는 이 배포 이전
코드를 실행 중인 ECS task의 롤링 교체 표면일 뿐이고 새 reader의 판단 근거가 아니에요.
구버전 reader에는 `META.data`를 항상 `unknown`으로 주고 새 reader만 `snapshot_data`를
읽어요. MISSING mirror에서는 구버전 poller가 자동 요청을 만들지 못하도록 현재·직전 태그를
모두 숨겨요.
구버전 writer가 snapshot 포인터 없이 META를 덮으면 새 reader는 도구 행을 합치지 않고
증거 없음으로 반환해 다음 snapshot-aware seed/resync가 복구하게 해요.

숫자를 저장하지 않아요. DynamoDB 는 숫자를 `Decimal`로 되돌려주는데 로컬 대체 구현은
`int`라 경계에서 타입이 갈리거든요 — 개수는 항상 읽는 쪽에서 세요.
"""
from __future__ import annotations

import json
import logging
from dataclasses import replace
from threading import RLock, local
from uuid import uuid4

from .drift_models import (
    AssetDriftLedger,
    SensitivityChangeEvent,
    SensitivityChangeRequest,
    SensitivityChangeStatus,
    ToolLedgerEntry,
)

_PK_PREFIX = "MCPDRIFT#"
_LOG_PK_PREFIX = "MCPDRIFTLOG#"
_META_SK = "META"
_TOOL_SK_PREFIX = "TOOL#"
_SNAPSHOT_SK_PREFIX = "LEDGER#"
_CHANGE_SK_PREFIX = "CHANGE#"
_ACTIVE_CHANGE_SK = "SENSITIVITY_CHANGE"
_LEDGER_READ_RETRIES = 3
_SNAPSHOT_READER_REQUIRED = "snapshot-aware ledger reader required"
_OBSOLETE_DRIFT_OBSERVER = "system:mcp-drift-observer"
_OBSOLETE_MISSING_REASON = (
    "상류 MCP tools/list에서 사라진 도구의 Target 제거 전파를 요청했어요."
)

_log = logging.getLogger(__name__)

#: 이력 조회 상한. 화면은 최근 것부터 보여주고, 전량 감사는 원장 export 로 해요.
HISTORY_LIMIT = 100


class DriftLedgerConflict(Exception):
    """낙관적 잠금 충돌 — 저장된 버전이 write 가 기대한 버전과 달라요(LC-05).

    폴러가 admin 편집을 조건 없이 덮어쓰던 결함을 막아요. 위험한 writer 는 폴러라
    admin write 만 조건부로 하면 소용이 없어요 — `put` **자체**가 compare-and-swap 이에요.
    """


class DriftLedgerReadError(Exception):
    """A committed ledger snapshot could not be read consistently."""


class DriftChangeConflict(Exception):
    """A sensitivity saga changed since the caller observed it."""


def _pk(asset_key: str) -> str:
    return f"{_PK_PREFIX}{asset_key}"


def _log_pk(asset_key: str) -> str:
    return f"{_LOG_PK_PREFIX}{asset_key}"


def _log_sk(event: SensitivityChangeEvent) -> str:
    return f"{event.at}#{event.event_id}"


def _change_sk(tool_name: str) -> str:
    return f"{_CHANGE_SK_PREFIX}{tool_name}"


def _snapshot_prefix(snapshot_id: str) -> str:
    return f"{_SNAPSHOT_SK_PREFIX}{snapshot_id}#TOOL#"


def _snapshot_sk(snapshot_id: str, tool_name: str) -> str:
    return f"{_snapshot_prefix(snapshot_id)}{tool_name}"


def _legacy_meta(ledger: AssetDriftLedger) -> dict:
    """Keep pre-snapshot readers from treating a non-atomic mirror as observed."""
    return {
        **ledger.meta_to_dict(),
        "check_status": "unknown",
        "check_error": _SNAPSHOT_READER_REQUIRED,
    }


def _legacy_mirror_payload(data: dict) -> dict:
    """Hide MISSING tags from 7727 readers that auto-created removal requests."""
    payload = dict(data)
    if str(payload.get("state") or "") == "MISSING":
        payload["sensitivity"] = None
        payload["sensitivity_source"] = None
        payload["previous_sensitivity"] = None
    return payload


def _is_obsolete_missing_change(change: SensitivityChangeRequest) -> bool:
    if (
        change.requested_by != _OBSOLETE_DRIFT_OBSERVER
        or change.reason != _OBSOLETE_MISSING_REASON
        or change.state_after != "MISSING"
        or change.after is not None
        or change.direction != "upgrade"
        or change.target_after is not None
    ):
        return False
    if change.status is SensitivityChangeStatus.PENDING_APPROVAL:
        return not change.approved_by and not change.movement
    if (
        change.status
        is not SensitivityChangeStatus.APPROVED_PENDING_PROPAGATION
        or change.approved_by != _OBSOLETE_DRIFT_OBSERVER
    ):
        return False
    movement = change.movement
    coordinates = movement.get("coordinates")
    return (
        movement.get("status") == "blocked"
        and movement.get("stage") == "propagation_pending"
        and movement.get("ticket") == "IA-68"
        and movement.get("consistency") == "unchanged"
        and isinstance(coordinates, dict)
        and coordinates.get("after_target") is None
    )


def _is_condition_conflict(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    code = response.get("Error", {}).get("Code")
    if code == "ConditionalCheckFailedException":
        return True
    if code != "TransactionCanceledException":
        return False
    return any(
        str(reason.get("Code") or "") == "ConditionalCheckFailed"
        for reason in (response.get("CancellationReasons") or ())
        if isinstance(reason, dict)
    )


class InMemoryMcpDriftStore:
    """테스트·로컬 대체 구현. Dynamo 구현과 같은 공개 계약이에요."""

    def __init__(self) -> None:
        self._meta: dict[str, dict] = {}
        self._tools: dict[str, dict[str, dict]] = {}
        self._history: dict[str, list[dict]] = {}
        self._changes: dict[str, dict[str, dict]] = {}
        self._active_changes: dict[str, str] = {}
        self._versions: dict[str, int] = {}
        self._lock = RLock()

    def get(self, asset_key: str) -> AssetDriftLedger:
        entries = [
            ToolLedgerEntry.from_dict(data)
            for data in self._tools.get(asset_key, {}).values()
        ]
        return AssetDriftLedger.from_parts(
            asset_key, self._meta.get(asset_key), entries,
            version=self._versions.get(asset_key, 0))

    def put(self, ledger: AssetDriftLedger) -> int:
        # CAS: 저장 버전이 write 가 읽은 버전과 같을 때만 +1 로 persist 해요.
        with self._lock:
            if self._versions.get(ledger.asset_key, 0) != ledger.version:
                raise DriftLedgerConflict(ledger.asset_key)
            new_version = ledger.version + 1
            self._versions[ledger.asset_key] = new_version
            self._meta[ledger.asset_key] = ledger.meta_to_dict()
            self._tools[ledger.asset_key] = {
                entry.tool_name: entry.to_dict() for entry in ledger.entries
            }
            return new_version

    def append_history(self, event: SensitivityChangeEvent) -> None:
        self._history.setdefault(event.asset_key, []).append(event.to_dict())

    def list_history(self, asset_key: str,
                     limit: int = HISTORY_LIMIT) -> list[SensitivityChangeEvent]:
        rows = sorted(
            self._history.get(asset_key, []),
            key=lambda d: (str(d.get("at") or ""), str(d.get("event_id") or "")),
            reverse=True,
        )
        return [SensitivityChangeEvent.from_dict(row) for row in rows[:limit]]

    def list_changes(self, asset_key: str) -> list[SensitivityChangeRequest]:
        return sorted(
            (
                SensitivityChangeRequest.from_dict(row)
                for row in self._changes.get(asset_key, {}).values()
            ),
            key=lambda change: (change.requested_at, change.request_id),
        )

    def get_change(
        self,
        asset_key: str,
        tool_name: str,
    ) -> SensitivityChangeRequest | None:
        row = self._changes.get(asset_key, {}).get(tool_name)
        return SensitivityChangeRequest.from_dict(row) if row else None

    def discard_obsolete_missing_changes(
        self,
        asset_key: str,
    ) -> tuple[str, ...]:
        """Remove only 7727 observer requests; append-only history stays intact."""
        with self._lock:
            removed: list[str] = []
            changes = self._changes.get(asset_key, {})
            for tool_name, row in list(changes.items()):
                change = SensitivityChangeRequest.from_dict(row)
                if not _is_obsolete_missing_change(change):
                    continue
                active = self._active_changes.get(asset_key)
                if active not in {None, change.request_id}:
                    continue
                changes.pop(tool_name, None)
                if active == change.request_id:
                    self._active_changes.pop(asset_key, None)
                removed.append(change.request_id)
            if not changes:
                self._changes.pop(asset_key, None)
            return tuple(sorted(removed))

    def create_change(
        self,
        change: SensitivityChangeRequest,
        event: SensitivityChangeEvent,
    ) -> SensitivityChangeRequest:
        """Atomically create a request and its REQUESTED audit event."""
        self.discard_obsolete_missing_changes(change.asset_key)
        with self._lock:
            active_request_id = self._active_changes.get(change.asset_key)
            unfinished = next(
                (
                    item
                    for item in self.list_changes(change.asset_key)
                    if item.status is not SensitivityChangeStatus.APPLIED
                ),
                None,
            )
            if active_request_id or unfinished is not None:
                raise DriftChangeConflict(
                    active_request_id
                    or unfinished.request_id
                    or change.request_id
                )
            existing = self.get_change(change.asset_key, change.tool_name)
            if (
                existing is not None
                and existing.status is not SensitivityChangeStatus.APPLIED
            ):
                raise DriftChangeConflict(change.request_id)
            persisted = replace(change, request_version=1)
            # Append first so an injected audit failure leaves no request behind.
            self.append_history(event)
            self._changes.setdefault(change.asset_key, {})[
                change.tool_name
            ] = persisted.to_dict()
            self._active_changes[change.asset_key] = change.request_id
            return persisted

    @staticmethod
    def _status_values(statuses) -> set[str]:
        return {
            status.value if isinstance(status, SensitivityChangeStatus) else str(status)
            for status in statuses
        }

    def transition_change(
        self,
        change: SensitivityChangeRequest,
        event: SensitivityChangeEvent,
        *,
        expected_statuses,
    ) -> SensitivityChangeRequest:
        """Atomically transition one request and append its stage event."""
        with self._lock:
            current = self.get_change(change.asset_key, change.tool_name)
            expected = self._status_values(expected_statuses)
            if (
                current is None
                or current.request_id != change.request_id
                or current.request_version != change.request_version
                or current.status.value not in expected
                or self._active_changes.get(
                    change.asset_key,
                    change.request_id,
                ) != change.request_id
            ):
                raise DriftChangeConflict(change.request_id)
            persisted = replace(
                change,
                request_version=current.request_version + 1,
            )
            self.append_history(event)
            self._changes[change.asset_key][change.tool_name] = (
                persisted.to_dict()
            )
            self._active_changes[change.asset_key] = change.request_id
            return persisted

    def commit_change(
        self,
        ledger: AssetDriftLedger,
        change: SensitivityChangeRequest,
        event: SensitivityChangeEvent,
        *,
        expected_status: SensitivityChangeStatus,
    ) -> tuple[int, SensitivityChangeRequest]:
        """Atomically commit the ledger row, APPLIED request, and MOVED event."""
        with self._lock:
            if self._versions.get(ledger.asset_key, 0) != ledger.version:
                raise DriftLedgerConflict(ledger.asset_key)
            current = self.get_change(change.asset_key, change.tool_name)
            if (
                current is None
                or current.request_id != change.request_id
                or current.request_version != change.request_version
                or current.status is not expected_status
                or self._active_changes.get(
                    change.asset_key,
                    change.request_id,
                ) != change.request_id
            ):
                raise DriftChangeConflict(change.request_id)

            new_version = ledger.version + 1
            persisted = replace(
                change,
                request_version=current.request_version + 1,
            )
            # All validations precede the event; subsequent in-memory assignments cannot
            # partially fail. An injected event failure therefore leaves everything intact.
            self.append_history(event)
            self._versions[ledger.asset_key] = new_version
            self._meta[ledger.asset_key] = ledger.meta_to_dict()
            self._tools[ledger.asset_key] = {
                entry.tool_name: entry.to_dict() for entry in ledger.entries
            }
            self._changes[change.asset_key][change.tool_name] = (
                persisted.to_dict()
            )
            self._active_changes.pop(change.asset_key, None)
            return new_version, persisted


class DynamoMcpDriftStore:
    """카탈로그 테이블 기반 구현. 프로세스 간 상태가 갈리지 않게 캐시를 두지 않아요."""

    def __init__(
        self,
        *,
        table_name: str,
        region: str,
        table=None,
        transact_client=None,
    ) -> None:
        self._table_name = table_name
        self._region = region
        self._thread_local = local()
        self._transact_client = transact_client
        if table is not None:
            self._thread_local.table = table

    @property
    def _table(self):
        # boto3 resource/Table 은 thread-safe 가 아니라 스레드마다 따로 만들어요
        # (DynamoAuxStore 와 같은 방식).
        table = getattr(self._thread_local, "table", None)
        if table is None:
            import boto3
            table = boto3.resource(
                "dynamodb", region_name=self._region).Table(self._table_name)
            self._thread_local.table = table
        return table

    def _items(
        self,
        asset_key: str,
        *,
        consistent_read: bool = False,
        sort_key_prefix: str | None = None,
    ) -> list[dict]:
        from boto3.dynamodb.conditions import Key

        items: list[dict] = []
        key_condition = Key("PK").eq(_pk(asset_key))
        if sort_key_prefix is not None:
            key_condition = key_condition & Key("SK").begins_with(
                sort_key_prefix
            )
        kwargs: dict = {
            "KeyConditionExpression": key_condition,
            "ConsistentRead": consistent_read,
        }
        while True:
            # query 는 페이지당 1MB 라 끝까지 이어 읽어요 — 첫 페이지만 보면 도구가
            # 많은 자산에서 조용히 누락되고, 그 누락이 곧 "MISSING 오판"이에요.
            resp = self._table.query(**kwargs)
            items.extend(resp.get("Items") or [])
            last = resp.get("LastEvaluatedKey")
            if not last:
                return items
            kwargs["ExclusiveStartKey"] = last

    def _meta_item(
        self,
        asset_key: str,
        *,
        consistent_read: bool = True,
    ) -> dict | None:
        response = self._table.get_item(
            Key={"PK": _pk(asset_key), "SK": _META_SK},
            ConsistentRead=consistent_read,
        )
        item = response.get("Item")
        return item if isinstance(item, dict) else None

    def get(self, asset_key: str) -> AssetDriftLedger:
        for _attempt in range(_LEDGER_READ_RETRIES):
            meta_item = self._meta_item(asset_key)
            if meta_item is None:
                return AssetDriftLedger.from_parts(
                    asset_key,
                    None,
                    (),
                    version=0,
                )
            meta = (
                json.loads(
                    meta_item.get("snapshot_data")
                    or meta_item["data"]
                )
                if meta_item.get("snapshot_data") or meta_item.get("data")
                else None
            )
            # legacy Number와 snapshot-aware numeric string을 모두 int로
            # 정규화해 서비스/인메모리 계약을 같게 유지해요.
            version = (
                int(meta_item["version"])
                if meta_item.get("version") is not None
                else 0
            )
            snapshot_id = str(
                meta_item.get("snapshot_id") or ""
            ).strip()
            if not snapshot_id:
                # The pre-snapshot writer commits META before rewriting TOOL# rows.
                # Even two consistent reads cannot tell whether that writer is
                # between those steps, so combining the rows would manufacture an
                # observation. Return no tool evidence and let the service reseed or
                # resync through a snapshot-aware CAS.
                unobservable_meta = dict(meta or {})
                unobservable_meta["check_status"] = "unknown"
                unobservable_meta["check_error"] = (
                    "legacy ledger snapshot is not atomically observable"
                )
                unobservable_meta["catalog_snapshot_unobservable"] = True
                return AssetDriftLedger.from_parts(
                    asset_key,
                    unobservable_meta,
                    (),
                    version=version,
                )

            snapshot_items = self._items(
                asset_key,
                consistent_read=True,
                sort_key_prefix=_snapshot_prefix(snapshot_id),
            )
            after = self._meta_item(asset_key)
            after_snapshot_id = str(
                (after or {}).get("snapshot_id") or ""
            ).strip()
            after_version = (
                int(after["version"])
                if after is not None and after.get("version") is not None
                else 0
            )
            if (
                after_snapshot_id != snapshot_id
                or after_version != version
            ):
                continue
            expected_count = int(meta_item.get("entry_count") or 0)
            if len(snapshot_items) != expected_count:
                raise DriftLedgerReadError(
                    f"{asset_key}: committed ledger snapshot is incomplete"
                )
            entries = [
                ToolLedgerEntry.from_dict(json.loads(item["data"]))
                for item in snapshot_items
                if item.get("data")
            ]
            return AssetDriftLedger.from_parts(
                asset_key,
                meta,
                entries,
                version=version,
            )
        raise DriftLedgerReadError(
            f"{asset_key}: ledger snapshot changed while being read"
        )

    def list_changes(self, asset_key: str) -> list[SensitivityChangeRequest]:
        changes = [
            SensitivityChangeRequest.from_dict(json.loads(item["data"]))
            for item in self._items(asset_key, consistent_read=True)
            if str(item.get("SK") or "").startswith(_CHANGE_SK_PREFIX)
            and item.get("data")
        ]
        return sorted(
            changes,
            key=lambda change: (change.requested_at, change.request_id),
        )

    def get_change(
        self,
        asset_key: str,
        tool_name: str,
    ) -> SensitivityChangeRequest | None:
        response = self._table.get_item(
            Key={"PK": _pk(asset_key), "SK": _change_sk(tool_name)},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item or not item.get("data"):
            return None
        return SensitivityChangeRequest.from_dict(json.loads(item["data"]))

    def discard_obsolete_missing_changes(
        self,
        asset_key: str,
    ) -> tuple[str, ...]:
        """Delete obsolete observer state and lock, never its audit history."""
        items = [
            item
            for item in self._items(asset_key, consistent_read=True)
            if str(item.get("SK") or "").startswith(_CHANGE_SK_PREFIX)
            and item.get("data")
        ]
        active_response = self._table.get_item(
            Key={"PK": _pk(asset_key), "SK": _ACTIVE_CHANGE_SK},
            ConsistentRead=True,
        )
        active_request_id = str(
            (active_response.get("Item") or {}).get("request_id") or ""
        )
        candidates: list[tuple[dict, SensitivityChangeRequest]] = []
        for item in items:
            change = SensitivityChangeRequest.from_dict(
                json.loads(item["data"])
            )
            if _is_obsolete_missing_change(change):
                candidates.append((item, change))
        candidates.sort(
            key=lambda candidate: (
                candidate[1].request_id != active_request_id,
                candidate[1].requested_at,
                candidate[1].request_id,
            )
        )

        removed: list[str] = []
        for item, change in candidates:
            values = self._ddb_item({
                ":request_id": change.request_id,
                ":request_version": change.request_version,
                ":status": change.status.value,
                ":data": item["data"],
            })
            lock_values = self._ddb_item({
                ":request_id": change.request_id,
            })
            try:
                self._ddb_client.transact_write_items(TransactItems=[
                    {"Delete": {
                        "TableName": self._table_name,
                        "Key": self._ddb_item({
                            "PK": _pk(asset_key),
                            "SK": _change_sk(change.tool_name),
                        }),
                        "ConditionExpression": (
                            "#request_id = :request_id AND "
                            "#request_version = :request_version AND "
                            "#status = :status AND #data = :data"
                        ),
                        "ExpressionAttributeNames": {
                            "#request_id": "request_id",
                            "#request_version": "request_version",
                            "#status": "status",
                            "#data": "data",
                        },
                        "ExpressionAttributeValues": values,
                    }},
                    {"Delete": {
                        "TableName": self._table_name,
                        "Key": self._ddb_item({
                            "PK": _pk(asset_key),
                            "SK": _ACTIVE_CHANGE_SK,
                        }),
                        "ConditionExpression": (
                            "attribute_not_exists(PK) OR "
                            "#request_id = :request_id"
                        ),
                        "ExpressionAttributeNames": {
                            "#request_id": "request_id",
                        },
                        "ExpressionAttributeValues": lock_values,
                    }},
                ])
            except Exception as exc:
                if _is_condition_conflict(exc):
                    continue
                raise
            removed.append(change.request_id)
        return tuple(sorted(removed))

    def _stage_snapshot(
        self,
        ledger: AssetDriftLedger,
        snapshot_id: str,
    ) -> None:
        pk = _pk(ledger.asset_key)
        try:
            for entry in ledger.entries:
                self._table.put_item(Item={
                    "PK": pk,
                    "SK": _snapshot_sk(snapshot_id, entry.tool_name),
                    "data": json.dumps(
                        entry.to_dict(),
                        ensure_ascii=False,
                    ),
                })
        except Exception:
            self._discard_snapshot(ledger.asset_key, snapshot_id)
            raise

    def _delete_snapshot(
        self,
        asset_key: str,
        snapshot_id: str,
    ) -> None:
        for item in self._items(
            asset_key,
            consistent_read=True,
            sort_key_prefix=_snapshot_prefix(snapshot_id),
        ):
            self._table.delete_item(Key={
                "PK": _pk(asset_key),
                "SK": item["SK"],
            })

    def _discard_snapshot(
        self,
        asset_key: str,
        snapshot_id: str,
    ) -> None:
        try:
            self._delete_snapshot(asset_key, snapshot_id)
        except Exception:
            _log.exception(
                "failed to discard uncommitted MCP drift snapshot: asset=%s",
                asset_key,
            )

    def _cleanup_previous_snapshot(
        self,
        asset_key: str,
        previous_snapshot_id: str,
    ) -> None:
        try:
            if previous_snapshot_id:
                self._delete_snapshot(asset_key, previous_snapshot_id)
        except Exception:
            # The committed META points only at the new immutable snapshot.
            # Cleanup failure leaks storage but cannot expose mixed ledger data.
            _log.exception(
                "failed to clean previous MCP drift snapshot: asset=%s",
                asset_key,
            )

    def _sync_legacy_mirror(
        self,
        ledger: AssetDriftLedger,
        snapshot_id: str,
        version: int,
    ) -> None:
        """Keep rolling-deployment readers on the pre-snapshot schema current."""
        try:
            current = self._meta_item(ledger.asset_key)
            if (
                str((current or {}).get("snapshot_id") or "") != snapshot_id
                or int((current or {}).get("version") or 0) != version
            ):
                return
            pk = _pk(ledger.asset_key)
            keep = {
                f"{_TOOL_SK_PREFIX}{entry.tool_name}"
                for entry in ledger.entries
            }
            for item in self._items(
                ledger.asset_key,
                consistent_read=True,
                sort_key_prefix=_TOOL_SK_PREFIX,
            ):
                if item["SK"] not in keep:
                    self._table.delete_item(Key={
                        "PK": pk,
                        "SK": item["SK"],
                })
            for entry in ledger.entries:
                # 7727c965 kept current MISSING tags and also promoted a previous
                # tag when current was empty. Either form made its old poller create
                # an automatic approval request, so the rolling mirror hides both.
                payload = _legacy_mirror_payload(entry.to_dict())
                self._table.put_item(Item={
                    "PK": pk,
                    "SK": f"{_TOOL_SK_PREFIX}{entry.tool_name}",
                    "data": json.dumps(
                        payload,
                        ensure_ascii=False,
                    ),
                })
        except Exception:
            # Snapshot-aware readers are already atomic. This mirror exists only
            # for old ECS tasks during a rolling replacement.
            _log.exception(
                "failed to update legacy MCP drift mirror: asset=%s",
                ledger.asset_key,
            )

    def _sanitize_legacy_missing_mirror(self, asset_key: str) -> None:
        """Make pre-snapshot MISSING rows safe before publishing a new META."""
        for item in self._items(
            asset_key,
            consistent_read=True,
            sort_key_prefix=_TOOL_SK_PREFIX,
        ):
            if not item.get("data"):
                continue
            raw = json.loads(item["data"])
            payload = _legacy_mirror_payload(raw)
            if payload == raw:
                continue
            self._table.put_item(Item={
                **item,
                "data": json.dumps(payload, ensure_ascii=False),
            })

    def put(self, ledger: AssetDriftLedger) -> int:
        pk = _pk(ledger.asset_key)
        expected = ledger.version
        new_version = expected + 1
        previous_meta = self._meta_item(ledger.asset_key)
        previous_snapshot_id = str(
            (previous_meta or {}).get("snapshot_id") or ""
        ).strip()
        snapshot_id = uuid4().hex
        # A 7727 task ignores META.check_status and can create an automatic
        # request from an old tagged MISSING TOOL row. Sanitize that row before
        # the snapshot META cutover, not afterwards.
        self._sanitize_legacy_missing_mirror(ledger.asset_key)
        # 최대 200개 도구는 DynamoDB transaction 100-item 한도를 넘을 수 있어요.
        # 그래서 고유한 불변 snapshot을 먼저 완성하고 META 포인터를 CAS로 전환해요.
        # 실패한 writer의 snapshot은 어느 META도 가리키지 않아 reader에게 보이지 않아요.
        self._stage_snapshot(ledger, snapshot_id)
        # version 은 조건식이 참조할 실제 속성이 필요해요(data JSON 안에 넣으면
        # 조건식이 못 봐요). Snapshot writer는 numeric string을 써요. 7727 reader의
        # int()는 이를 읽지만, 그 writer가 조건값으로 보내는 Dynamo Number는 타입이
        # 달라 snapshot META를 덮을 수 없어요.
        # `expected == 0` 의 뜻은 "아이템이 없다"가 아니라 **"버전이 아직 기록되지 않았다"**
        # 예요. LC-05 이전에 쓰인 META 아이템은 실재하면서 `version` 속성이 없어요(실측
        # 2026-08-27: dev `AgoraCatalog` 의 `MCPDRIFT#deepwiki-ca34 / META` 속성이
        # `[PK, SK, data]` 뿐이었어요). 여기를 `attribute_not_exists(PK)` 로 두면 그 legacy
        # 아이템이 영구히 저장 불가가 되고, 민감도 확정이 매번 409 로 떨어져요.
        # `attribute_not_exists(#v)` 는 신규 아이템과 legacy 아이템을 함께 통과시키고,
        # 첫 성공 write 가 버전을 찍어 그 뒤부터는 정확한 CAS 가 돼요.
        if expected == 0:
            condition = "attribute_not_exists(#v)"
            names = {"#v": "version"}
            values = None
        else:
            condition = "#v = :expected"
            names = {"#v": "version"}
            values = {
                ":expected": (
                    str(expected)
                    if isinstance((previous_meta or {}).get("version"), str)
                    else expected
                )
            }
        kwargs: dict = {
            "Item": {
                "PK": pk, "SK": _META_SK,
                # JSON 문자열로 저장해요 — 중첩 dict 를 그대로 넣으면 빈 문자열·숫자 왕복에서
                # 타입이 갈려요(Decimal 함정).
                # `data`는 snapshot을 모르는 롤링 task가 비원자 TOOL# mirror를
                # 성공으로 읽지 못하게 하는 호환 투영이고, 새 reader만 snapshot_data를
                # 실제 관측 메타로 사용해요.
                "data": json.dumps(_legacy_meta(ledger), ensure_ascii=False),
                "snapshot_data": json.dumps(
                    ledger.meta_to_dict(),
                    ensure_ascii=False,
                ),
                "version": str(new_version),
                "snapshot_id": snapshot_id,
                "entry_count": len(ledger.entries),
            },
            "ConditionExpression": condition,
        }
        if names is not None:
            kwargs["ExpressionAttributeNames"] = names
        if values is not None:
            kwargs["ExpressionAttributeValues"] = values
        try:
            self._table.put_item(**kwargs)
        except Exception as exc:
            self._discard_snapshot(ledger.asset_key, snapshot_id)
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                raise DriftLedgerConflict(ledger.asset_key) from exc
            raise
        self._sync_legacy_mirror(ledger, snapshot_id, new_version)
        self._cleanup_previous_snapshot(
            ledger.asset_key,
            previous_snapshot_id,
        )
        return new_version

    def append_history(self, event: SensitivityChangeEvent) -> None:
        """이력을 덧붙여요. append-only 라 같은 키를 덮어쓰지 않아요(event_id 가 유일)."""
        self._table.put_item(Item={
            "PK": _log_pk(event.asset_key), "SK": _log_sk(event),
            "data": json.dumps(event.to_dict(), ensure_ascii=False),
        })

    @staticmethod
    def _ddb_item(item: dict) -> dict:
        from boto3.dynamodb.types import TypeSerializer

        serializer = TypeSerializer()
        return {key: serializer.serialize(value) for key, value in item.items()}

    def _change_item(self, change: SensitivityChangeRequest) -> dict:
        return {
            "PK": _pk(change.asset_key),
            "SK": _change_sk(change.tool_name),
            "request_id": change.request_id,
            "request_version": change.request_version,
            "status": change.status.value,
            "data": json.dumps(change.to_dict(), ensure_ascii=False),
        }

    @staticmethod
    def _active_change_item(change: SensitivityChangeRequest) -> dict:
        return {
            "PK": _pk(change.asset_key),
            "SK": _ACTIVE_CHANGE_SK,
            "request_id": change.request_id,
            "tool_name": change.tool_name,
        }

    def _history_item(self, event: SensitivityChangeEvent) -> dict:
        return {
            "PK": _log_pk(event.asset_key),
            "SK": _log_sk(event),
            "data": json.dumps(event.to_dict(), ensure_ascii=False),
        }

    @property
    def _ddb_client(self):
        # A resource-derived meta.client applies resource conversion again and
        # turns already serialized AttributeValues into nested maps. Keep the
        # transaction path on a distinct low-level client.
        if self._transact_client is None:
            import boto3

            self._transact_client = boto3.client(
                "dynamodb",
                region_name=self._region,
            )
        return self._transact_client

    def create_change(
        self,
        change: SensitivityChangeRequest,
        event: SensitivityChangeEvent,
    ) -> SensitivityChangeRequest:
        self.discard_obsolete_missing_changes(change.asset_key)
        unfinished = next(
            (
                item
                for item in self.list_changes(change.asset_key)
                if item.status is not SensitivityChangeStatus.APPLIED
            ),
            None,
        )
        if unfinished is not None:
            raise DriftChangeConflict(unfinished.request_id)
        persisted = replace(change, request_version=1)
        try:
            self._ddb_client.transact_write_items(TransactItems=[
                {"Put": {
                    "TableName": self._table_name,
                    "Item": self._ddb_item(
                        self._active_change_item(persisted)
                    ),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }},
                {"Put": {
                    "TableName": self._table_name,
                    "Item": self._ddb_item(self._change_item(persisted)),
                    "ConditionExpression": (
                        "attribute_not_exists(PK) OR #status = :applied"
                    ),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": self._ddb_item({
                        ":applied": SensitivityChangeStatus.APPLIED.value,
                    }),
                }},
                {"Put": {
                    "TableName": self._table_name,
                    "Item": self._ddb_item(self._history_item(event)),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }},
            ])
        except Exception as exc:
            if _is_condition_conflict(exc):
                raise DriftChangeConflict(change.request_id) from exc
            raise
        return persisted

    def transition_change(
        self,
        change: SensitivityChangeRequest,
        event: SensitivityChangeEvent,
        *,
        expected_statuses,
    ) -> SensitivityChangeRequest:
        statuses = [
            status.value if isinstance(status, SensitivityChangeStatus) else str(status)
            for status in expected_statuses
        ]
        if not statuses:
            raise ValueError("expected_statuses must not be empty")
        persisted = replace(
            change,
            request_version=change.request_version + 1,
        )
        status_tokens = [f":status_{index}" for index in range(len(statuses))]
        values = {
            ":request_id": change.request_id,
            ":request_version": change.request_version,
            **dict(zip(status_tokens, statuses, strict=True)),
        }
        condition = (
            "#request_id = :request_id AND #request_version = :request_version "
            f"AND #status IN ({', '.join(status_tokens)})"
        )
        lock_values = self._ddb_item({
            ":request_id": change.request_id,
        })
        try:
            self._ddb_client.transact_write_items(TransactItems=[
                {"Put": {
                    "TableName": self._table_name,
                    "Item": self._ddb_item(
                        self._active_change_item(persisted)
                    ),
                    "ConditionExpression": (
                        "attribute_not_exists(PK) OR "
                        "#request_id = :request_id"
                    ),
                    "ExpressionAttributeNames": {
                        "#request_id": "request_id",
                    },
                    "ExpressionAttributeValues": lock_values,
                }},
                {"Put": {
                    "TableName": self._table_name,
                    "Item": self._ddb_item(self._change_item(persisted)),
                    "ConditionExpression": condition,
                    "ExpressionAttributeNames": {
                        "#request_id": "request_id",
                        "#request_version": "request_version",
                        "#status": "status",
                    },
                    "ExpressionAttributeValues": self._ddb_item(values),
                }},
                {"Put": {
                    "TableName": self._table_name,
                    "Item": self._ddb_item(self._history_item(event)),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }},
            ])
        except Exception as exc:
            if _is_condition_conflict(exc):
                raise DriftChangeConflict(change.request_id) from exc
            raise
        return persisted

    def commit_change(
        self,
        ledger: AssetDriftLedger,
        change: SensitivityChangeRequest,
        event: SensitivityChangeEvent,
        *,
        expected_status: SensitivityChangeStatus,
    ) -> tuple[int, SensitivityChangeRequest]:
        new_ledger_version = ledger.version + 1
        previous_meta = self._meta_item(ledger.asset_key)
        previous_snapshot_id = str(
            (previous_meta or {}).get("snapshot_id") or ""
        ).strip()
        snapshot_id = uuid4().hex
        self._sanitize_legacy_missing_mirror(ledger.asset_key)
        self._stage_snapshot(ledger, snapshot_id)
        persisted = replace(
            change,
            request_version=change.request_version + 1,
        )
        if ledger.version == 0:
            meta_condition = "attribute_not_exists(#ledger_version)"
            meta_values = None
        else:
            meta_condition = "#ledger_version = :ledger_version"
            meta_values = self._ddb_item({
                ":ledger_version": (
                    str(ledger.version)
                    if isinstance((previous_meta or {}).get("version"), str)
                    else ledger.version
                ),
            })
        change_values = self._ddb_item({
            ":request_id": change.request_id,
            ":request_version": change.request_version,
            ":status": expected_status.value,
        })
        lock_values = self._ddb_item({
            ":request_id": change.request_id,
        })
        meta_put: dict = {
            "TableName": self._table_name,
            "Item": self._ddb_item({
                "PK": _pk(ledger.asset_key),
                "SK": _META_SK,
                "data": json.dumps(
                    _legacy_meta(ledger),
                    ensure_ascii=False,
                ),
                "snapshot_data": json.dumps(
                    ledger.meta_to_dict(),
                    ensure_ascii=False,
                ),
                "version": str(new_ledger_version),
                "snapshot_id": snapshot_id,
                "entry_count": len(ledger.entries),
            }),
            "ConditionExpression": meta_condition,
            "ExpressionAttributeNames": {
                "#ledger_version": "version",
            },
        }
        if meta_values is not None:
            meta_put["ExpressionAttributeValues"] = meta_values
        try:
            self._ddb_client.transact_write_items(TransactItems=[
                {"Put": meta_put},
                {"Put": {
                    "TableName": self._table_name,
                    "Item": self._ddb_item(self._change_item(persisted)),
                    "ConditionExpression": (
                        "#request_id = :request_id AND "
                        "#request_version = :request_version AND "
                        "#status = :status"
                    ),
                    "ExpressionAttributeNames": {
                        "#request_id": "request_id",
                        "#request_version": "request_version",
                        "#status": "status",
                    },
                    "ExpressionAttributeValues": change_values,
                }},
                {"Put": {
                    "TableName": self._table_name,
                    "Item": self._ddb_item(self._history_item(event)),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }},
                {"Delete": {
                    "TableName": self._table_name,
                    "Key": self._ddb_item({
                        "PK": _pk(change.asset_key),
                        "SK": _ACTIVE_CHANGE_SK,
                    }),
                    "ConditionExpression": (
                        "attribute_not_exists(PK) OR "
                        "#request_id = :request_id"
                    ),
                    "ExpressionAttributeNames": {
                        "#request_id": "request_id",
                    },
                    "ExpressionAttributeValues": lock_values,
                }},
            ])
        except Exception as exc:
            self._discard_snapshot(ledger.asset_key, snapshot_id)
            if _is_condition_conflict(exc):
                raise DriftLedgerConflict(ledger.asset_key) from exc
            raise
        self._sync_legacy_mirror(
            ledger,
            snapshot_id,
            new_ledger_version,
        )
        self._cleanup_previous_snapshot(
            ledger.asset_key,
            previous_snapshot_id,
        )
        return new_ledger_version, persisted

    def list_history(self, asset_key: str,
                     limit: int = HISTORY_LIMIT) -> list[SensitivityChangeEvent]:
        """최근 변경부터 돌려줘요. SK 가 시각이라 역방향 query 로 상한을 걸 수 있어요."""
        from boto3.dynamodb.conditions import Key

        resp = self._table.query(
            KeyConditionExpression=Key("PK").eq(_log_pk(asset_key)),
            ScanIndexForward=False,
            Limit=max(1, limit),
        )
        return [
            SensitivityChangeEvent.from_dict(json.loads(item["data"]))
            for item in (resp.get("Items") or [])
            if item.get("data")
        ]
