"""semgrep Fargate entrypoint — S3 소스 read → semgrep(sast) → S3 result.json write.

env: INPUT_URI, OUTPUT_URI, SCAN_ID, SEMGREP_RULES_DIR, SCAN_TIMEOUT(기본 240).
계약: 항상 exit 0. result.json = {"risk","findings","scanned_areas"}. 예외 → fail_closed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from urllib.parse import urlparse

import boto3

from normalize import normalize_semgrep, fail_closed, log_proc


def _parse_s3(uri: str) -> tuple[str, str]:
    p = urlparse(uri)
    return p.netloc, p.path.lstrip("/")


def _download(s3, bucket, prefix, dest) -> int:
    n = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            rel = obj["Key"][len(prefix):].lstrip("/")
            if not rel:
                continue
            local = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(local), exist_ok=True)
            s3.download_file(bucket, obj["Key"], local)
            n += 1
    return n


def _semgrep_configs(rules_dir: str) -> list:
    configs = []
    for lang in sorted(os.listdir(rules_dir)):
        for sub in (os.path.join(lang, "lang", "security"), os.path.join(lang, "security")):
            full = os.path.join(rules_dir, sub)
            if os.path.isdir(full):
                configs.append(full)
    return configs or [rules_dir]


def _run_semgrep(src, outdir, timeout, scan_id="") -> dict:
    report = os.path.join(outdir, "sg.json")
    rules_dir = os.getenv("SEMGREP_RULES_DIR", "/opt/semgrep-rules")
    cmd = ["semgrep", "scan"]
    for cfg in _semgrep_configs(rules_dir):
        cmd += ["--config", cfg]
    # --quiet 제거: 룰 로드·진행 로그를 stderr로 흘려 콘솔 로그 modal에 노출(결과 JSON은 --output 파일).
    cmd += ["--json", "--output", report, "--metrics", "off", src]
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    log_proc("semgrep", proc, scan_id)   # 도구 진행·요약 로그를 scan_id 태그와 함께 CloudWatch로 노출
    if proc.returncode not in (0, 1):
        return fail_closed(f"semgrep exit {proc.returncode}: {proc.stderr.decode('utf-8','replace')[:300]}")
    with open(report) as fh:
        data = json.load(fh) if os.path.getsize(report) else {"results": []}
    for r in data.get("results", []):
        p = r.get("path", "")
        if p:
            r["path"] = os.path.relpath(p, src)
    return normalize_semgrep(data)


def main() -> int:
    # scan_id를 로그에 남겨 콘솔 게이트 로그 modal이 CloudWatch filter(scan_id)로 조회해요.
    scan_id = os.getenv("SCAN_ID", "?")
    print(f"[semgrep] scan {scan_id} start", flush=True)
    s3 = boto3.client("s3", region_name=os.getenv("AWS_REGION", "ap-northeast-2"))
    timeout = int(os.getenv("SCAN_TIMEOUT", "240"))
    try:
        in_bucket, in_prefix = _parse_s3(os.environ["INPUT_URI"])
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as outdir:
            _download(s3, in_bucket, in_prefix, src)
            try:
                result = _run_semgrep(src, outdir, timeout, scan_id)
            except subprocess.TimeoutExpired:
                result = fail_closed(f"semgrep timeout {timeout}s")
    except Exception as e:
        result = fail_closed(f"semgrep runner error: {type(e).__name__}: {e}")
    try:
        out_bucket, out_key = _parse_s3(os.environ["OUTPUT_URI"])
        s3.put_object(Bucket=out_bucket, Key=out_key,
                      Body=json.dumps(result).encode("utf-8"), ContentType="application/json")
    except Exception as e:
        print(f"[semgrep] FATAL result put 실패: {e}", file=sys.stderr, flush=True)
    print(f"[semgrep] scan {scan_id} done: risk={result.get('risk')} "
          f"findings={len(result.get('findings', []))}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
