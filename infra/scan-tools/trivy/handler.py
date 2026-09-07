"""trivy Lambda 핸들러 — S3 소스 read → trivy fs(sbom_cve) → S3 result.json write.

event: {"input_uri":"s3://b/input/{id}/", "output_uri":"s3://b/output/{id}/trivy.json", "scan_id"}
계약: 항상 결과 반환(예외는 fail_closed). result.json = {"risk","findings","scanned_areas"}.
DB는 이미지 빌드 시 번들 — 런타임은 --skip-db-update로 완전 offline(air-gap).
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from urllib.parse import urlparse

import boto3

from normalize import normalize_trivy, fail_closed, log_proc


def _parse_s3(uri: str) -> tuple[str, str]:
    p = urlparse(uri)
    return p.netloc, p.path.lstrip("/")


def _download(s3, bucket: str, prefix: str, dest: str) -> int:
    n = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            rel = obj["Key"][len(prefix):].lstrip("/")
            if not rel:
                continue
            local = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(local), exist_ok=True)
            s3.download_file(bucket, obj["Key"], local)
            n += 1
    return n


def _run_trivy_raw(src: str, scan_id: str = "") -> dict:
    """trivy fs 실행 → JSON dict. (테스트는 이 함수를 monkeypatch.)

    air-gap: --skip-db-update/--skip-java-db-update로 번들 DB만 사용, 인터넷 접근 없음.
    --cache-dir는 읽기전용 파일시스템(Lambda) 대응으로 /tmp에 둠(빌드 시 DB를 여기로 복사).
    """
    report = os.path.join(tempfile.gettempdir(), "trivy.json")
    proc = subprocess.run(
        ["trivy", "fs", "--scanners", "vuln", "--skip-db-update", "--skip-java-db-update",
         "--offline-scan", "--cache-dir", os.getenv("TRIVY_CACHE_DIR", "/tmp/trivy-cache"),
         "--format", "json", "--output", report, src],
        capture_output=True, timeout=int(os.getenv("SCAN_TIMEOUT", "240")))
    log_proc("trivy", proc, scan_id)   # 도구 진행·요약 로그를 scan_id 태그와 함께 CloudWatch로 노출
    if proc.returncode != 0:
        raise RuntimeError(f"trivy exit {proc.returncode}: {proc.stderr.decode('utf-8','replace')[:300]}")
    with open(report) as fh:
        return json.load(fh) if os.path.getsize(report) else {"Results": []}


def _put(s3, uri: str, result: dict) -> None:
    bucket, key = _parse_s3(uri)
    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(result).encode("utf-8"),
                  ContentType="application/json")


def handler(event: dict, context) -> dict:
    scan_id = event.get("scan_id", "?")
    # scan_id를 로그에 남겨 콘솔 게이트 로그 modal이 CloudWatch filter(scan_id)로 조회해요.
    print(f"[trivy] scan {scan_id} start", flush=True)
    s3 = boto3.client("s3", region_name=os.getenv("AWS_REGION", "ap-northeast-2"))
    try:
        in_bucket, in_prefix = _parse_s3(event["input_uri"])
        with tempfile.TemporaryDirectory() as src:
            _download(s3, in_bucket, in_prefix, src)
            data = _run_trivy_raw(src, scan_id)
            result = normalize_trivy(data)
    except Exception as e:
        result = fail_closed(f"trivy lambda error: {type(e).__name__}: {e}")
    try:
        _put(s3, event["output_uri"], result)
    except Exception:
        pass  # 결과 write 실패 → 오케스트레이터가 result 부재를 fail-closed 처리
    print(f"[trivy] scan {scan_id} done: risk={result.get('risk')} "
          f"findings={len(result.get('findings', []))}", flush=True)
    return {"scan_id": scan_id, "area": "sbom_cve", **result}
