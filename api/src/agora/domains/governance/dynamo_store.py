"""DynamoGovStore — GovStore와 동일 시그니처의 DynamoDB 백엔드(멀티인스턴스 공유).

단일 테이블 PK/SK:
  GOV#TIER / TIER#{tier}          등급 셀 매트릭스
  GOV#SETTINGS / SETTINGS         콘솔 설정
  GOV#TARGETTIER / REC#{id}       자산별 목표 등급
  GOV#APPROVALBLOCK / REC#{id}    자동승인 차단 사유(자산별 최신 1건)
  SCAN#{id} / TS#{ts}             스캔 결과(append; scan_id 있으면 그걸 SK로 멱등)
  OVERLAP#{id} / TS#{ts}          중복검토 결과(append)
  DECISION#{id} / TS#{ts}         판정 이력(append-only)

인메모리 캐시가 없어요 — 매 호출 조회라 프로세스 간 상태가 항상 일치해요(캐시 불일치 제거).
"""
from __future__ import annotations

from dataclasses import asdict, fields
from threading import local

from boto3.dynamodb.conditions import Key

from .models import (
    ApprovalBlockRecord,
    ConsoleSettings,
    DecisionRecord,
    OverlapRecord,
    ScanRecord,
    MonitoringScanProjection,
    TierCell,
)

DEFAULT_TIER = "minimal"
_SETTINGS_FIELDS = {f.name for f in fields(ConsoleSettings)}
_APPROVAL_BLOCK_FIELDS = {f.name for f in fields(ApprovalBlockRecord)}


class DynamoGovStore:
    def __init__(self, *, table_name, region, client=None) -> None:
        self._table_name = table_name
        self._region = region
        self._table = client
        self._thread_local = local()

    def _tbl(self):
        if self._table is not None:
            return self._table
        table = getattr(self._thread_local, "table", None)
        if table is None:
            import boto3
            # boto3 resource/Table은 thread-safe가 아니므로 요청 enrich worker마다
            # 독립 인스턴스를 유지해요. 저수준 client와 달리 resource를 공유하면 안 돼요.
            table = boto3.resource(
                "dynamodb", region_name=self._region).Table(self._table_name)
            self._thread_local.table = table
        return table

    def _put(self, pk: str, sk: str, payload: dict) -> None:
        item = {"PK": pk, "SK": sk, **payload}
        self._tbl().put_item(Item={k: v for k, v in item.items() if v is not None})

    def _query(self, pk: str) -> list[dict]:
        """파티션 전체를 읽어요 — `LastEvaluatedKey` 를 끝까지 따라가요.

        ⚠️ 첫 페이지만 읽으면 **조용히 틀린 답**이 나와요. 같은 클래스의
        `_latest_scan_by_record` 가 그 이유를 이미 적어 뒀는데(「scan 은 페이지당 1MB라 첫
        페이지만 읽으면 뒷페이지 레코드를 조용히 놓쳐요」) 이 헬퍼는 안 고쳐져 있었어요
        (2026-09-06, PR #218 적대적 리뷰가 잡았어요). `latest_scan` 이 이걸 써서 그 파티션의
        `data.ts` 최댓값을 고르므로, `SCAN#<record>` 가 1MB 를 넘으면 **뒷페이지의 더 최신
        `failed`·`running` 을 못 보고 첫 페이지의 옛 `done` 을 최신으로 답해요.** 그 값이
        고객·감사용 위협리포트의 위험도·findings·스캔 상태로 나가고, `ReportService` 의 캐시
        서명(`scan_ts`)까지 그 값이라 캐시가 새 스캔으로 갱신되지 않아요.

        페이지를 더 읽는 것은 어떤 호출자도 깨지 않아요 — 5개 호출자 전부 결과를 필터·정렬해서
        쓰고, 「항목이 더 많으면 안 되는」 곳이 없어요.
        """
        items: list[dict] = []
        kwargs: dict = {"KeyConditionExpression": Key("PK").eq(pk)}
        while True:
            resp = self._tbl().query(**kwargs)
            items.extend(resp.get("Items", []))
            token = resp.get("LastEvaluatedKey")
            if not token:
                return items
            kwargs["ExclusiveStartKey"] = token

    # --- tiers ---
    def get_tier(self, tier: str) -> list[TierCell]:
        items = self._query("GOV#TIER")
        for it in items:
            if it["SK"] == f"TIER#{tier}":
                return [TierCell(**c) for c in it.get("cells", [])]
        return []

    def put_tier(self, tier: str, cells: list[TierCell]) -> None:
        self._put("GOV#TIER", f"TIER#{tier}", {"cells": [asdict(c) for c in cells]})

    # --- settings ---
    def get_settings(self) -> ConsoleSettings:
        resp = self._tbl().get_item(Key={"PK": "GOV#SETTINGS", "SK": "SETTINGS"})
        raw = (resp.get("Item") or {}).get("data", {})
        s = ConsoleSettings(**{k: v for k, v in raw.items() if k in _SETTINGS_FIELDS})
        if not s.asset_tier_map:
            s.asset_tier_map = {"skill": "minimal", "mcp": "standard", "agent": "strong"}
        if not s.judge_model_map:
            s.judge_model_map = {"minimal": "haiku-4-5", "standard": "sonnet-4-6", "strong": "sonnet-5"}
        return s

    def put_settings(self, settings: ConsoleSettings) -> None:
        self._put("GOV#SETTINGS", "SETTINGS", {"data": asdict(settings)})

    # --- categories ---
    def get_categories(self) -> list[str]:
        response = self._tbl().get_item(
            Key={"PK": "GOV#CATEGORY", "SK": "LIST"},
        )
        raw = (response.get("Item") or {}).get("items", [])
        return [str(item) for item in raw]

    def put_categories(self, items: list[str]) -> None:
        from .store import normalize_categories

        self._put(
            "GOV#CATEGORY",
            "LIST",
            {"items": normalize_categories(items)},
        )

    # --- scans ---
    def add_scan(self, record_id: str, scan: ScanRecord) -> None:
        # scan_id 있으면 SK로 써서 같은 실행 재기록이 덮어쓰기(멱등). 없으면 ts(append).
        sk = f"TS#{scan.scan_id}" if scan.scan_id else f"TS#{scan.ts}"
        self._put(f"SCAN#{record_id}", sk, {"data": asdict(scan)})
        projection = MonitoringScanProjection(
            ts=scan.ts,
            risk=scan.risk,
            status=scan.status,
            version=scan.version,
        )
        try:
            self._tbl().put_item(
                Item={
                    "PK": "GOV#MONITORING#SCAN",
                    "SK": f"REC#{record_id}",
                    **projection.to_dict(),
                },
                ConditionExpression=(
                    "attribute_not_exists(PK) OR "
                    "attribute_not_exists(#ts) OR #ts <= :ts"
                ),
                ExpressionAttributeNames={"#ts": "ts"},
                ExpressionAttributeValues={":ts": scan.ts},
            )
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code != "ConditionalCheckFailedException":
                raise

    def latest_scan(self, record_id: str) -> ScanRecord | None:
        items = self._query(f"SCAN#{record_id}")
        if not items:
            return None
        # ts 기준 최신. scan_id-SK 혼재 대비 data.ts로 정렬.
        items.sort(key=lambda it: it.get("data", {}).get("ts", it["SK"]))
        return ScanRecord(**items[-1]["data"])

    def monitoring_latest_scans(
        self,
        record_ids: list[str],
    ) -> dict[str, MonitoringScanProjection]:
        """Read safe latest scan projections without scan history or findings."""
        unique_ids = list(dict.fromkeys(record_ids))
        if not unique_ids:
            return {}
        table = self._tbl()
        client = table.meta.client
        table_name = table.name
        output: dict[str, MonitoringScanProjection] = {}
        for offset in range(0, len(unique_ids), 100):
            pending = [
                {
                    "PK": "GOV#MONITORING#SCAN",
                    "SK": f"REC#{record_id}",
                }
                for record_id in unique_ids[offset : offset + 100]
            ]
            while pending:
                response = client.batch_get_item(
                    RequestItems={
                        table_name: {
                            "Keys": pending,
                            "ProjectionExpression": (
                                "PK, SK, #ts, risk, #status, version"
                            ),
                            "ExpressionAttributeNames": {
                                "#ts": "ts",
                                "#status": "status",
                            },
                            "ConsistentRead": True,
                        }
                    }
                )
                for item in response.get("Responses", {}).get(table_name, []):
                    record_id = str(item["SK"])[len("REC#") :]
                    output[record_id] = MonitoringScanProjection(
                        ts=str(item.get("ts") or ""),
                        risk=str(item.get("risk") or ""),
                        status=str(item.get("status") or ""),
                        version=str(item.get("version") or ""),
                    )
                pending = response.get("UnprocessedKeys", {}).get(
                    table_name, {}
                ).get("Keys", [])
        return output

    def _latest_scan_by_record(self) -> dict[str, dict]:
        """record_id → 최신 스캔 data(SCAN# 파티션 scan). running/done 열거 공용.

        페이지네이션 — scan은 페이지당 1MB라 첫 페이지만 읽으면 SCAN# 행이 쌓였을 때 뒷페이지의
        레코드를 조용히 놓쳐요(폴러가 그 레코드의 running/done을 못 봐 고착·verdict 미적용). 끝까지 읽어요.
        """
        latest: dict[str, dict] = {}
        tbl = self._tbl()
        kwargs: dict = {}
        while True:
            resp = tbl.scan(**kwargs)
            for it in resp.get("Items", []):
                pk = it.get("PK", "")
                if not pk.startswith("SCAN#"):
                    continue
                rid = pk[len("SCAN#"):]
                data = it.get("data", {})
                prev = latest.get(rid)
                if prev is None or data.get("ts", "") > prev.get("ts", ""):
                    latest[rid] = data
            last = resp.get("LastEvaluatedKey")
            if not last:
                return latest
            kwargs["ExclusiveStartKey"] = last

    def running_record_ids(self) -> list[str]:
        """최신 스캔 status가 'running'인 record_id 목록(SCAN# 파티션 scan)."""
        return [rid for rid, d in self._latest_scan_by_record().items()
                if d.get("status") == "running"]

    def done_record_ids(self) -> list[str]:
        """최신 스캔 status가 'done'인 record_id 목록(verdict backstop 후보).

        aggregate Lambda가 done을 push하면 폴러가 running 창을 놓쳐 verdict 미적용 고착이
        생겨요. backstop이 이 목록에서 done인데 non-terminal인 레코드를 잡아 재적용해요.
        """
        return [rid for rid, d in self._latest_scan_by_record().items()
                if d.get("status") == "done"]

    # --- overlaps ---
    def add_overlap(self, record_id: str, overlap: OverlapRecord) -> None:
        self._put(f"OVERLAP#{record_id}", f"TS#{overlap.ts}", {"data": asdict(overlap)})

    def latest_overlap(self, record_id: str) -> OverlapRecord | None:
        items = self._query(f"OVERLAP#{record_id}")
        if not items:
            return None
        items.sort(key=lambda it: it.get("data", {}).get("ts", it["SK"]))
        return OverlapRecord(**items[-1]["data"])

    def all_overlaps(self) -> dict[str, OverlapRecord]:
        """record_id → 최신 OverlapRecord. 단일 scan + PK prefix 필터(all_decisions와 같은 패턴).

        자산마다 query하면 N+1이라 인벤토리 화면이 자산 수만큼 왕복해요. `running_record_ids`가
        하는 "PK별 최신 하나 고르기"와 같은 방식으로 한 번에 모아요.
        """
        latest: dict[str, dict] = {}
        for it in self._tbl().scan().get("Items", []):
            pk = it.get("PK", "")
            if not pk.startswith("OVERLAP#"):
                continue
            record_id = pk[len("OVERLAP#"):]
            data = it.get("data", {})
            prev = latest.get(record_id)
            if prev is None or data.get("ts", "") > prev.get("ts", ""):
                latest[record_id] = data
        return {rid: OverlapRecord(**data) for rid, data in latest.items()}

    # --- decisions ---
    def add_decision(self, record_id: str, decision: DecisionRecord) -> None:
        self._put(f"DECISION#{record_id}", f"TS#{decision.ts}", {"data": asdict(decision)})

    def list_decisions(self, record_id: str) -> list[DecisionRecord]:
        items = self._query(f"DECISION#{record_id}")
        items.sort(key=lambda it: it["SK"])
        return [DecisionRecord(**it["data"]) for it in items]

    def all_decisions(self) -> list[tuple[str, DecisionRecord]]:
        out: list[tuple[str, DecisionRecord]] = []
        for it in self._tbl().scan().get("Items", []):
            pk = it.get("PK", "")
            if pk.startswith("DECISION#"):
                out.append((pk[len("DECISION#"):], DecisionRecord(**it["data"])))
        return out

    # --- approval blocks (자동승인 차단 사유, 자산별 최신 1건) ---
    def put_approval_block(self, record_id: str, block: ApprovalBlockRecord) -> None:
        self._put("GOV#APPROVALBLOCK", f"REC#{record_id}", {"data": asdict(block)})

    def get_approval_block(self, record_id: str) -> ApprovalBlockRecord | None:
        resp = self._tbl().get_item(
            Key={"PK": "GOV#APPROVALBLOCK", "SK": f"REC#{record_id}"})
        raw = (resp.get("Item") or {}).get("data")
        if not raw:
            return None
        return ApprovalBlockRecord(
            **{k: v for k, v in raw.items() if k in _APPROVAL_BLOCK_FIELDS})

    def clear_approval_block(self, record_id: str) -> None:
        self._tbl().delete_item(
            Key={"PK": "GOV#APPROVALBLOCK", "SK": f"REC#{record_id}"})

    # --- target tier ---
    def get_target_tier(self, record_id: str) -> str:
        resp = self._tbl().get_item(Key={"PK": "GOV#TARGETTIER", "SK": f"REC#{record_id}"})
        item = resp.get("Item")
        return item["tier"] if item else DEFAULT_TIER

    def set_target_tier(self, record_id: str, tier: str) -> None:
        self._put("GOV#TARGETTIER", f"REC#{record_id}", {"tier": tier})

    def purge_record(self, record_id: str) -> None:
        tbl = self._tbl()
        for pk in (f"SCAN#{record_id}", f"OVERLAP#{record_id}", f"DECISION#{record_id}"):
            for it in self._query(pk):
                tbl.delete_item(Key={"PK": pk, "SK": it["SK"]})
        tbl.delete_item(Key={"PK": "GOV#TARGETTIER", "SK": f"REC#{record_id}"})
        tbl.delete_item(
            Key={"PK": "GOV#MONITORING#SCAN", "SK": f"REC#{record_id}"}
        )
        tbl.delete_item(
            Key={"PK": "GOV#APPROVALBLOCK", "SK": f"REC#{record_id}"}
        )
