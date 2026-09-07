"""DynamoStatsStore — DynamoDB 기반 다운로드 집계 + 인기 톱5 구현.

키 구조 (spec §2.1·2.2):
  카운터   PK="STAT#DOWNLOADS"  SK="REC#{record_id}"
           downloads(N), name(S), descriptor_type(S), updated_at(S)
  스냅샷   PK="STAT#TOP"        SK="SNAPSHOT"
           computed_at(S), items(L of M)

increment_download: DynamoDB ADD 연산으로 원자 카운터 증가 (read-modify-write 금지).
top_downloads: 1시간 TTL 스냅샷 캐싱 + stale-while-error. 만료·없음이면 Query로 재계산.
invalidate_snapshot: SNAPSHOT 아이템을 DDB에서 삭제해 다음 호출 시 재계산 강제.
"""
from __future__ import annotations

import datetime
import logging
from typing import Callable

import boto3
import boto3.dynamodb.conditions

from .grouping import group_by_type
from .models import GroupedSnapshot, TopEntry, TopSnapshot, TypeGroup

_COUNTER_PK = "STAT#DOWNLOADS"
_SNAPSHOT_PK = "STAT#TOP"
_SNAPSHOT_SK = "SNAPSHOT"
_SNAPSHOT_TTL_SECONDS = 3600

UTC = datetime.timezone.utc


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(UTC)


def _iso(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime.datetime:
    """ISO 8601 UTC 문자열을 aware datetime으로 파싱해요."""
    return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


class DynamoStatsStore:
    """StatsPort Protocol의 DynamoDB 구현체.

    Args:
        table_name: DynamoDB 테이블 이름 (AgoraCatalog 권장).
        region:     AWS 리전.
        now:        UTC 현재 시각을 반환하는 콜러블 (테스트 시간 주입용).
                    None이면 datetime.now(UTC) 사용.
    """

    def __init__(
        self,
        table_name: str,
        region: str,
        now: Callable[[], datetime.datetime] | None = None,
    ) -> None:
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)
        self._now: Callable[[], datetime.datetime] = now or _utcnow

    # ── StatsPort 구현 ────────────────────────────────────────────────────────

    def increment_download(
        self,
        record_id: str,
        *,
        name: str,
        descriptor_type: str,
        owner_user: str = "",
    ) -> int:
        """record_id의 다운로드 수를 원자적으로 +1하고 새 값을 반환해요.

        DynamoDB ADD 연산을 사용해 read-modify-write 없이 원자 증가해요.
        name·descriptor_type·owner_user는 매번 덮어써서 최신 메타를 유지해요.
        """
        resp = self._table.update_item(
            Key={"PK": _COUNTER_PK, "SK": f"REC#{record_id}"},
            UpdateExpression=(
                "ADD downloads :one "
                "SET #n = :n, descriptor_type = :dt, owner_user = :ou, updated_at = :ts"
            ),
            ExpressionAttributeNames={"#n": "name"},
            ExpressionAttributeValues={
                ":one": 1,
                ":n": name,
                ":dt": descriptor_type,
                ":ou": owner_user,
                ":ts": _iso(self._now()),
            },
            ReturnValues="UPDATED_NEW",
        )
        # boto3 resource는 숫자를 Decimal로 반환해요 → int 변환 필수
        return int(resp["Attributes"]["downloads"])

    def get_download_count(self, record_id: str) -> int:
        """누적 다운로드 수. 없으면 0."""
        resp = self._table.get_item(
            Key={"PK": _COUNTER_PK, "SK": f"REC#{record_id}"},
            ProjectionExpression="downloads",
        )
        item = resp.get("Item")
        if item is None:
            return 0
        return int(item.get("downloads", 0))

    def top_downloads(self, limit: int = 5, *, force: bool = False) -> TopSnapshot:
        """1시간 TTL 스냅샷 캐싱 + stale-while-error.

        1. DDB에서 SNAPSHOT 아이템 읽기
        2. computed_at이 1시간 이내 → 재계산 없이 반환 (force면 건너뜀)
        3. 만료·없음·force → _compute_snapshot()(Query) 시도
        4. 재계산(Query) 실패 → stale 스냅샷 반환(있으면), 없으면 items=[]
        5. 저장(_write_snapshot_item) 실패는 별개 — 방금 계산한 fresh는 정확하므로
           로깅만 하고 fresh를 그대로 반환해요.

        force=True면 신선도를 무시하고 재집계해요(새로고침 버튼).
        """
        now = self._now()
        stale: TopSnapshot | None = None

        # 기존 스냅샷 읽기
        snapshot_item = self._read_snapshot_item()
        if snapshot_item is not None:
            try:
                computed_at_dt = _parse_iso(snapshot_item["computed_at"])
                age = (now - computed_at_dt).total_seconds()
                if not force and age <= _SNAPSHOT_TTL_SECONDS:
                    # fresh → 재계산 없이 반환
                    return self._deserialize_snapshot(snapshot_item)
                else:
                    # 만료·force — stale-while-error용으로 보관
                    stale = self._deserialize_snapshot(snapshot_item)
            except Exception:
                # computed_at 파싱 오류 등 → 재계산 진행
                pass

        # 재계산(Query) 시도 — 실패 시 stale-while-error
        try:
            fresh = self._compute_snapshot(limit=limit)
        except Exception:
            if stale is not None:
                return stale
            return TopSnapshot(computed_at=_iso(now), items=[])

        # 저장은 별개 단계 — 실패해도 fresh는 정확하므로 로깅만 하고 반환해요.
        try:
            self._write_snapshot_item(fresh)
        except Exception:
            logging.exception("스냅샷 저장 실패 — 재계산 결과는 정상 반환해요")
        return fresh

    def top_downloads_by_type(
        self, limit: int = 5, *, force: bool = False
    ) -> list[TypeGroup]:
        """타입마다 독립 톱N. 그룹핑은 공유 순수함수(group_by_type)가 담당해요."""
        return self.top_downloads_grouped(limit=limit, force=force).groups

    def top_downloads_grouped(
        self, limit: int = 5, *, force: bool = False
    ) -> GroupedSnapshot:
        """그룹 + 계산 시각.

        타입별 상위 N을 뽑으려면 카운터 **전량**이 필요해요(전체 톱N만 있으면
        하위 타입이 잘려요). 그래서 flat 스냅샷 캐시를 쓰지 않고 Query로 전량을 읽어요.
        Query가 실패하면 flat 스냅샷을 폴백으로 그룹핑해요(stale-while-error).

        force 인자는 계약 대칭을 위해 받지만, 이 경로는 항상 Query로 실집계하므로
        동작에 영향이 없어요.
        """
        now_iso = _iso(self._now())
        try:
            entries = self._all_entries()
        except Exception:
            logging.exception("그룹 집계 Query 실패 — flat 스냅샷으로 폴백해요")
            snapshot_item = self._read_snapshot_item()
            if snapshot_item is None:
                return GroupedSnapshot(
                    computed_at=now_iso, groups=group_by_type([], limit=limit)
                )
            stale = self._deserialize_snapshot(snapshot_item)
            return GroupedSnapshot(
                computed_at=stale.computed_at,
                groups=group_by_type(stale.items, limit=limit),
            )
        return GroupedSnapshot(
            computed_at=now_iso, groups=group_by_type(entries, limit=limit)
        )

    def purge_record(self, record_id: str) -> None:
        """record_id의 카운터·메타를 삭제해요. 없으면 noop.

        삭제된 자산이 톱5에 남으면 안 되므로 스냅샷도 무효화해요.
        """
        self._table.delete_item(
            Key={"PK": _COUNTER_PK, "SK": f"REC#{record_id}"},
        )
        self.invalidate_snapshot()

    def invalidate_snapshot(self) -> None:
        """캐시된 스냅샷 아이템을 DDB에서 삭제해요.

        다음 top_downloads 호출 시 재계산을 강제해요.
        computed_at을 빈 값으로 두면 파싱 분기가 늘어나므로 삭제 방식을 사용해요.
        """
        self._table.delete_item(
            Key={"PK": _SNAPSHOT_PK, "SK": _SNAPSHOT_SK},
        )

    # ── 내부 헬퍼 ────────────────────────────────────────────────────────────

    def _query_all_counters(self) -> list[dict]:
        """STAT#DOWNLOADS 파티션의 모든 카운터 아이템을 Query + 페이지네이션으로 읽어요."""
        condition = boto3.dynamodb.conditions.Key("PK").eq(_COUNTER_PK)
        items: list[dict] = []
        exclusive_start_key = None
        while True:
            kwargs: dict = {"KeyConditionExpression": condition}
            if exclusive_start_key:
                kwargs["ExclusiveStartKey"] = exclusive_start_key
            resp = self._table.query(**kwargs)
            items.extend(resp.get("Items", []))
            exclusive_start_key = resp.get("LastEvaluatedKey")
            if not exclusive_start_key:
                break
        return items

    def _all_entries(self) -> list[TopEntry]:
        """카운터 전량을 Query로 읽어 TopEntry 목록으로 반환해요(정렬 없음)."""
        return [
            TopEntry(
                record_id=item["SK"][len("REC#"):],  # SK="REC#{record_id}"
                name=item.get("name", ""),
                descriptor_type=item.get("descriptor_type", ""),
                downloads=int(item.get("downloads", 0)),
                owner_user=item.get("owner_user", ""),
            )
            for item in self._query_all_counters()
        ]

    def _compute_snapshot(self, limit: int = 5) -> TopSnapshot:
        """카운터를 Query로 읽어 내림차순 정렬 후 TopSnapshot을 반환해요."""
        entries = sorted(
            self._all_entries(),
            key=lambda e: (-e.downloads, e.record_id),
        )[:limit]
        return TopSnapshot(computed_at=_iso(self._now()), items=entries)

    def _read_snapshot_item(self) -> dict | None:
        """DDB에서 SNAPSHOT 아이템을 읽어요. 없으면 None."""
        resp = self._table.get_item(
            Key={"PK": _SNAPSHOT_PK, "SK": _SNAPSHOT_SK},
        )
        return resp.get("Item")

    def _write_snapshot_item(self, snap: TopSnapshot) -> None:
        """TopSnapshot을 DDB의 SNAPSHOT 아이템으로 저장(덮어쓰기)해요."""
        items_serialized = [
            {
                "record_id": entry.record_id,
                "name": entry.name,
                "descriptor_type": entry.descriptor_type,
                "downloads": entry.downloads,
                "owner_user": entry.owner_user,
            }
            for entry in snap.items
        ]
        self._table.put_item(
            Item={
                "PK": _SNAPSHOT_PK,
                "SK": _SNAPSHOT_SK,
                "computed_at": snap.computed_at,
                "items": items_serialized,
            }
        )

    def _deserialize_snapshot(self, item: dict) -> TopSnapshot:
        """DDB 아이템에서 TopSnapshot을 복원해요."""
        entries = [
            TopEntry(
                record_id=e["record_id"],
                name=e.get("name", ""),
                descriptor_type=e.get("descriptor_type", ""),
                downloads=int(e.get("downloads", 0)),
                owner_user=e.get("owner_user", ""),
            )
            for e in item.get("items", [])
        ]
        return TopSnapshot(computed_at=item["computed_at"], items=entries)
