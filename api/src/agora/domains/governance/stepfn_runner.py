"""StepFunctionScanRunner — ScannerPort 구현(Step Functions 비동기 오케스트레이션, SP-4).

scan()이 등급 도구목록을 resolve해 sfn.start_execution(name=scan_id)로 비동기 시작하고
running placeholder ScanOutcome을 반환해요. SF Map이 도구별 실행+fan-in을 담당하고, 완료
반영(done 전이)은 SF→앱 콜백/폴링(후속). 멱등: 같은 scan_id 재실행은 ExecutionAlreadyExists
를 흡수(중복 방지 계승). 예상외 오류만 fail-closed(risk=high).

AGORA_SCANNER=stepfn 일 때만 deps.get_scanner()가 이걸 반환(기본 static 무회귀).
"""
from __future__ import annotations

import datetime as _dt
import json
import re

from .models import ScanRecord
from .scanner import ScanOutcome
from .scan_orchestration import merge_partial_scan, resolve_tools


def _slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", (text or "scan").lower()).strip("-")[:80] or "scan"


def _is_already_exists(e: Exception) -> bool:
    """ExecutionAlreadyExists 판정 — ClientError.response["Error"]["Code"] 또는 문자열 매칭."""
    resp = getattr(e, "response", None)
    if isinstance(resp, dict) and resp.get("Error", {}).get("Code") == "ExecutionAlreadyExists":
        return True
    return "ExecutionAlreadyExists" in str(e)


class StepFunctionScanRunner:
    def __init__(self, state_machine_arn, region, bucket,
                 cells_provider, tools_provider, sfn_client=None, s3_client=None):
        self._sm = state_machine_arn
        self._region = region
        self._bucket = bucket
        self._cells_provider = cells_provider
        self._tools_provider = tools_provider
        self.__sfn = sfn_client
        self.__s3 = s3_client

    @property
    def _sfn(self):
        if self.__sfn is None:
            import boto3
            self.__sfn = boto3.client("stepfunctions", region_name=self._region)
        return self.__sfn

    @property
    def _s3(self):
        if self.__s3 is None:
            import boto3
            self.__s3 = boto3.client("s3", region_name=self._region)
        return self.__s3

    def scan(self, asset_type, descriptors, source_files) -> ScanOutcome:
        try:
            scan_id = str((descriptors or {}).get("scan_id") or "")
            if not scan_id:
                first = source_files[0][0] if source_files else asset_type
                scan_id = _slug(f"{asset_type}-{first}")
            # descriptors에 cells가 있으면(자산 tier 반영) 그걸로, 없으면 provider 폴백.
            from .models import TierCell
            desc_cells = (descriptors or {}).get("cells")
            if desc_cells:
                cells = [TierCell(tier=c.get("tier", ""), tool_id=c["tool_id"],
                                  enforcement=c.get("enforcement", "off"),
                                  threshold=c.get("threshold", ""), asset_type_scope=c.get("asset_type_scope", "*"))
                         for c in desc_cells if c.get("tool_id")]
            else:
                cells = self._cells_provider()
            # 소스 유무는 호출부(scan_service)가 descriptors.has_source로 알려줘요.
            # 미지정이면 source_files 유무로 추정(하위호환).
            has_source = (descriptors or {}).get("has_source")
            if has_source is None:
                has_source = bool(source_files)
            tools = resolve_tools(cells, self._tools_provider(), asset_type,
                                   judge_model=str((descriptors or {}).get("judge_model", "")),
                                   has_source=bool(has_source))
            # 도구가 읽도록 소스를 scan-I/O 버킷 input/{scan_id}/ 에 올려요.
            for path, content in (source_files or []):
                body = content if isinstance(content, (bytes, bytearray)) else str(content).encode("utf-8")
                self._s3.put_object(Bucket=self._bucket, Key=f"input/{scan_id}/{path}", Body=body)
            # 소스가 없으면 llm-judge가 읽을 입력을 descriptor에서 만들어 같은 prefix에 올려요
            # (MCP tool 이름·설명·inputSchema, agent card). 이게 connect형 자산의 유일한 검사 입력.
            judge_doc = (descriptors or {}).get("judge_document") or ""
            if not source_files and judge_doc:
                self._s3.put_object(Bucket=self._bucket,
                                    Key=f"input/{scan_id}/asset-descriptor.md",
                                    Body=judge_doc.encode("utf-8"))
            payload = {
                "scan_id": scan_id,
                # record_id는 aggregate Lambda가 DynamoGovStore SCAN#{record_id}에 결과를 write할 때
                # 써요. Map은 top-level $.record_id를 보존하므로 aggregateTask가 그대로 읽어요.
                # 없으면 빈 문자열(하위호환 — aggregate가 no-op, 서버측 폴러가 폴백 경로).
                "record_id": str((descriptors or {}).get("record_id") or ""),
                "asset_type": asset_type,
                "bucket": self._bucket,
                "input_prefix": f"input/{scan_id}/",
                "output_prefix": f"output/{scan_id}/",
                "tools": tools,
            }
            try:
                self._sfn.start_execution(
                    stateMachineArn=self._sm, name=scan_id, input=json.dumps(payload))
            except Exception as e:  # 멱등: 이미 같은 name 실행 중이면 흡수
                if not _is_already_exists(e):
                    raise
            # 비동기 계약: running placeholder. 실제 결과는 SF 완료 후 반영(후속).
            return ScanOutcome(risk="none", findings=[], scanned_areas=[], pending=True)
        except Exception as e:
            return ScanOutcome(risk="high", findings=[
                {"code": "SCANNER_ERROR", "severity": "high",
                 "detail": f"stepfn runner error: {type(e).__name__}: {e}", "location": ""}])

    def _execution_arn(self, scan_id: str) -> str:
        # arn:...:stateMachine:{name} → arn:...:execution:{name}:{scan_id}
        return self._sm.replace(":stateMachine:", ":execution:") + f":{scan_id}"

    def sync_execution(self, record_id: str, scan_id: str, store) -> "ScanRecord | None":
        """describe_execution으로 SF 완료를 앱 done ScanRecord로 전이해요.

        SUCCEEDED → aggregate output(risk/findings/scanned_areas)을 done으로 기록.
        RUNNING → None(그대로 running). FAILED/TIMED_OUT/ABORTED → fail-closed done(가짜 통과 방지).
        """
        try:
            resp = self._sfn.describe_execution(executionArn=self._execution_arn(scan_id))
        except Exception:
            return None  # 조회 실패 시 running 유지(다음 폴링 재시도)
        status = resp.get("status")
        now = _dt.datetime.now(_dt.timezone.utc).isoformat()
        if status not in ("SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"):
            return None  # RUNNING/PENDING → 그대로

        # 부분 재스캔이면(running에 rescan_area가 실려 있음) base(running에 실린 findings/
        # scanned_areas)를 SUCCEEDED·FAILED 양쪽에서 보존해요 — 안 그러면 이 부분 결과/실패가
        # latest가 돼 다른 area가 not_run으로 회귀(전체 결과 소실). rescan_area 없으면 전체
        # 스캔이라 기존 로직 그대로(하위호환). latest_scan 조회 실패는 running=None(전체 스캔 처리).
        try:
            running = store.latest_scan(record_id)
        except Exception:
            running = None
        rescan_area = getattr(running, "rescan_area", "") if running is not None else ""
        base_findings = (getattr(running, "findings", []) or []) if running is not None else []
        base_areas = (getattr(running, "scanned_areas", []) or []) if running is not None else []

        if status == "SUCCEEDED":
            try:
                out = json.loads(resp.get("output") or "{}")
            except Exception:
                out = {}
            new_findings = out.get("findings", [])
            new_areas = out.get("scanned_areas", [])
            risk = out.get("risk", "high")
            if rescan_area:
                # base done에서 재실행한 area만 SF 결과로 교체.
                merged = merge_partial_scan(base_findings, base_areas,
                                            rescan_area, new_findings, new_areas)
                new_findings, new_areas, risk = (
                    merged["findings"], merged["scanned_areas"], merged["risk"])
            rec = ScanRecord(
                ts=now, risk=risk, findings=new_findings,
                trigger="stepfn", principal="", status="done",
                scanned_areas=new_areas, scanner_kind="stepfn",
                scan_id=scan_id)  # running행과 같은 SK(TS#{scan_id})로 덮어써 orphan 방지(Important #2)
                # rescan_area는 done엔 실지 않아요("" 기본) — 완결된 전체 스냅샷이 되도록.
            store.add_scan(record_id, rec)
            return rec

        # FAILED / TIMED_OUT / ABORTED
        if rescan_area:
            # 부분 재스캔 실패: base의 다른 area 결과는 보존하고, 재실행한 area만 실패로 표시해요
            # (SUCCEEDED와 대칭 — 전체 게이트를 fail-closed로 회귀시키지 않아요). SCANNER_ERROR에
            # area 필드를 실어 gate.py가 "전 게이트 회귀"가 아니라 그 area만 실패로 잡게 해요.
            # merge_partial_scan으로 rescan_area 몫을 이 finding 1개로 교체하고, scanned_areas는
            # 그 area를 빼요(실패라 검사 못 함). 다른 area는 base 그대로 유지.
            err_finding = {"code": "SCANNER_ERROR", "severity": "high", "area": rescan_area,
                           "detail": f"Step Functions 실행 {status}", "location": ""}
            merged = merge_partial_scan(base_findings, base_areas, rescan_area,
                                        new_findings=[err_finding], new_areas=[])
            rec = ScanRecord(
                ts=now, risk=merged["risk"], findings=merged["findings"],
                trigger="stepfn", principal="", status="done",
                scanned_areas=merged["scanned_areas"], scanner_kind="stepfn", scan_id=scan_id)
        else:
            err_finding = {"code": "SCANNER_ERROR", "severity": "high",
                           "detail": f"Step Functions 실행 {status}", "location": ""}
            # 전체 스캔 실패: 어떤 area도 검사 못 함 → 전 게이트 fail-closed(기존 하위호환).
            rec = ScanRecord(
                ts=now, risk="high", findings=[err_finding],
                trigger="stepfn", principal="", status="done", scanned_areas=[],
                scanner_kind="stepfn", scan_id=scan_id)
        store.add_scan(record_id, rec)
        return rec
