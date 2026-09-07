"""DynamoBundleStore — JsonBundleStore와 동일 시그니처의 DynamoDB 백엔드(멀티인스턴스 공유).

단일 테이블 PK/SK:
  BUNDLE / {bundle_id}    번들 1건(data=Bundle.to_dict())

번들 수가 많지 않고 list()·remove_member_everywhere()가 전수 조회라, 등급 셀 매트릭스
(GOV#TIER)와 같은 단일 파티션 패턴을 써요 — PK 고정, SK=bundle_id 한 번의 query로 전량 조회.

Fargate처럼 컨테이너가 여러 개거나 재시작하면 로컬 JSON은 휘발해요(HP-02). 공유 DynamoDB로
영속화하고, 인메모리 캐시를 두지 않아 매 호출 조회 — 프로세스 간 상태가 항상 일치해요.
"""
from __future__ import annotations

from boto3.dynamodb.conditions import Key

from .models import Bundle

_PK = "BUNDLE"


class DynamoBundleStore:
    def __init__(self, *, table_name, region, client=None) -> None:
        self._table_name = table_name
        self._region = region
        self._table = client

    def _tbl(self):
        if self._table is None:
            import boto3
            self._table = boto3.resource(
                "dynamodb", region_name=self._region).Table(self._table_name)
        return self._table

    def _all_items(self) -> list[dict]:
        # 페이지네이션 — query는 페이지당 최대 1MB라 첫 페이지만 읽으면 번들이 많을 때 조용히
        # 누락돼요(list()·remove_member_everywhere가 전수 조회라 특히 위험). 끝까지 이어 읽어요.
        tbl = self._tbl()
        items: list[dict] = []
        kwargs: dict = {"KeyConditionExpression": Key("PK").eq(_PK)}
        while True:
            resp = tbl.query(**kwargs)
            items.extend(resp.get("Items", []))
            last = resp.get("LastEvaluatedKey")
            if not last:
                return items
            kwargs["ExclusiveStartKey"] = last

    def put(self, bundle: Bundle) -> None:
        self._tbl().put_item(
            Item={"PK": _PK, "SK": bundle.bundle_id, "data": bundle.to_dict()})

    def get(self, bundle_id: str) -> Bundle | None:
        item = self._tbl().get_item(
            Key={"PK": _PK, "SK": bundle_id}).get("Item")
        return Bundle.from_dict(item["data"]) if item else None

    def list(self) -> list[Bundle]:
        bundles = [Bundle.from_dict(it["data"]) for it in self._all_items()]
        return sorted(bundles, key=lambda b: b.updated_at, reverse=True)

    def delete(self, bundle_id: str) -> bool:
        resp = self._tbl().delete_item(
            Key={"PK": _PK, "SK": bundle_id}, ReturnValues="ALL_OLD")
        return bool(resp.get("Attributes"))

    def remove_member_everywhere(self, record_id: str) -> int:
        """모든 번들에서 record_id 멤버십을 제거해요. 제거가 일어난 번들 수 반환(하드 삭제 정리용)."""
        n = 0
        for it in self._all_items():
            bundle = Bundle.from_dict(it["data"])
            if record_id in bundle.member_ids:
                bundle.member_ids = [m for m in bundle.member_ids if m != record_id]
                self.put(bundle)
                n += 1
        return n
