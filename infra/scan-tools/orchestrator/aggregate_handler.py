"""aggregate Lambda 핸들러 (SP-4) — SF Map 결과(도구별 result)를 fan-in 병합.

event = {"scan_id", "record_id"(선택), "results": [{"risk","findings","scanned_areas"}...]}
반환 = {"scan_id","risk","findings","scanned_areas"}.

record_id + AGORA_GOV_TABLE env가 있으면 병합 결과를 DynamoGovStore 스키마(SCAN#{id})에
done ScanRecord로 직접 write해요(push). verdict는 계산하지 않아요 — 앱이 tier·게이트로 판정.
"""
from __future__ import annotations

import os

_RISK_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}


def _ddb_resource(region=None):
    import boto3
    return boto3.resource("dynamodb", region_name=region)


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _write_gov_store(record_id: str, scan_id: str, risk: str,
                     findings: list, areas: list) -> None:
    """DynamoGovStore SCAN# 파티션에 done ScanRecord를 멱등 write(scan_id를 SK로)."""
    table_name = os.environ.get("AGORA_GOV_TABLE")
    if not table_name or not record_id:
        return
    region = os.environ.get("AGORA_GOV_REGION") or os.environ.get("AWS_REGION")
    table = _ddb_resource(region).Table(table_name)
    data = {
        "ts": _now_iso(), "risk": risk, "findings": findings,
        "trigger": "stepfn", "principal": "", "status": "done",
        "scanned_areas": areas, "scanner_kind": "stepfn", "scan_id": scan_id,
    }
    table.put_item(Item={
        "PK": f"SCAN#{record_id}", "SK": f"TS#{scan_id}", "data": data})


def handler(event: dict, context) -> dict:
    results = event.get("results", []) or []
    findings: list = []
    areas: list = []
    worst = "none"
    for r in results:
        findings.extend(r.get("findings", []) or [])
        for a in (r.get("scanned_areas") or []):
            if a not in areas:
                areas.append(a)
        risk = r.get("risk", "none")
        if _RISK_ORDER.get(risk, 0) > _RISK_ORDER.get(worst, 0):
            worst = risk

    scan_id = event.get("scan_id", "?")
    record_id = event.get("record_id", "")
    try:
        _write_gov_store(record_id, scan_id, worst, findings, areas)
    except Exception:
        pass  # write 실패는 반환을 막지 않음(앱 폴러의 sync_running_stepfn이 폴백 경로)

    return {"scan_id": scan_id, "risk": worst,
            "findings": findings, "scanned_areas": areas}
