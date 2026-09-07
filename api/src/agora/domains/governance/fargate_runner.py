"""FargateScanRunner — ScannerPort 구현(실제 격리 Fargate task에서 스캐너 실행).

로컬 API가 ① 소스를 scan-I/O 버킷 input/{scanId}/ 에 put ② ecs.run_task 로 격리 서브넷
(PRIVATE_ISOLATED)에서 scan-runner 컨테이너 기동 ③ describe_tasks 폴링(STOPPED) ④ output/
{scanId}/result.json 을 읽어 ScanOutcome 파싱. 어느 단계든 실패 → risk=high fail-closed.

AGORA_SCANNER=fargate 일 때만 deps.get_scanner()가 이걸 반환(기본 static → 무회귀).
boto3 클라이언트는 지연 생성(report_service 패턴) — 테스트가 주입/monkeypatch 가능.
"""
from __future__ import annotations

import json
import re
import time

from .scanner import Finding, ScanOutcome  # noqa: F401 (Finding: 계약 참조)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "scan").lower()).strip("-")[:40] or "scan"


class FargateScanRunner:
    def __init__(self, cluster, task_def, subnets, security_groups, bucket,
                 region="ap-northeast-2", timeout=300, s3_client=None, ecs_client=None,
                 poll_interval=5, sleep=time.sleep):
        self._cluster = cluster
        self._task_def = task_def
        self._subnets = list(subnets)
        self._sgs = list(security_groups)
        self._bucket = bucket
        self._region = region
        self._timeout = timeout
        self._poll = poll_interval
        self._sleep = sleep
        self.__s3 = s3_client
        self.__ecs = ecs_client

    # boto3 지연 생성 (테스트 주입 가능)
    @property
    def _s3_client(self):
        if self.__s3 is None:
            import boto3
            self.__s3 = boto3.client("s3", region_name=self._region)
        return self.__s3

    @property
    def _ecs_client(self):
        if self.__ecs is None:
            import boto3
            self.__ecs = boto3.client("ecs", region_name=self._region)
        return self.__ecs

    def scan(self, asset_type, descriptors, source_files) -> ScanOutcome:
        try:
            scan_id = str((descriptors or {}).get("scan_id") or "")
            if not scan_id:
                first = source_files[0][0] if source_files else asset_type
                scan_id = _slug(f"{asset_type}-{first}")
            in_prefix = f"input/{scan_id}/"
            out_key = f"output/{scan_id}/result.json"
            self._put_sources(in_prefix, source_files)
            self._run_task(scan_id, in_prefix, out_key)
            return self._await_result(out_key)
        except Exception as e:
            return self._fail_closed(f"fargate runner error: {type(e).__name__}: {e}")

    def _put_sources(self, prefix, source_files):
        for path, content in (source_files or []):
            body = content if isinstance(content, (bytes, bytearray)) else str(content).encode("utf-8")
            self._s3_client.put_object(Bucket=self._bucket, Key=f"{prefix}{path}", Body=body)

    def _run_task(self, scan_id, in_prefix, out_key):
        resp = self._ecs_client.run_task(
            cluster=self._cluster,
            taskDefinition=self._task_def,
            launchType="FARGATE",
            networkConfiguration={"awsvpcConfiguration": {
                "subnets": self._subnets, "securityGroups": self._sgs, "assignPublicIp": "DISABLED"}},
            overrides={"containerOverrides": [{
                "name": "scan-runner",
                "environment": [
                    {"name": "INPUT_URI", "value": f"s3://{self._bucket}/{in_prefix}"},
                    {"name": "OUTPUT_URI", "value": f"s3://{self._bucket}/{out_key}"},
                    {"name": "SCAN_ID", "value": scan_id},
                ]}]},
        )
        if resp.get("failures") or not resp.get("tasks"):
            reason = (resp.get("failures") or [{}])[0].get("reason", "no task started")
            raise RuntimeError(f"run_task failed: {reason}")
        self._task_arn = resp["tasks"][0]["taskArn"]

    def _await_result(self, out_key) -> ScanOutcome:
        deadline = time.monotonic() + self._timeout
        while True:
            desc = self._ecs_client.describe_tasks(cluster=self._cluster, tasks=[self._task_arn])
            tasks = desc.get("tasks", [])
            if tasks and tasks[0].get("lastStatus") == "STOPPED":
                break
            if time.monotonic() >= deadline:
                return self._fail_closed(f"scan timeout {self._timeout}s")
            self._sleep(self._poll)
        # result.json 파싱
        try:
            obj = self._s3_client.get_object(Bucket=self._bucket, Key=out_key)
            data = json.loads(obj["Body"].read())
            return ScanOutcome(risk=data.get("risk", "high"), findings=data.get("findings", []),
                               scanned_areas=data.get("scanned_areas", []))
        except Exception:
            return self._fail_closed("result.json 부재/파싱 실패")

    def _fail_closed(self, reason) -> ScanOutcome:
        return ScanOutcome(risk="high", findings=[
            {"code": "SCANNER_ERROR", "severity": "high", "detail": reason, "location": ""}])
