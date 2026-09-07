"""scan-runner 컨테이너 진입점 — S3 소스 read → gitleaks 실행 → 결과 S3 write.

env:
  INPUT_URI   s3://bucket/input/{scanId}/   (소스 파일들의 prefix)
  OUTPUT_URI  s3://bucket/output/{scanId}/result.json
  SCAN_TIMEOUT (선택, 기본 240초)

계약: 항상 exit 0. 결과·실패는 OUTPUT_URI의 result.json({"risk","findings"})으로만 표현.
gitleaks exit=1(누출 발견)은 정상 경로, 그 외 예외·타임아웃 → fail-closed(risk=high).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from urllib.parse import urlparse

import boto3

from normalize import combine, fail_closed, normalize_gitleaks, normalize_semgrep


def _parse_s3(uri: str) -> tuple[str, str]:
    p = urlparse(uri)
    return p.netloc, p.path.lstrip("/")


def _download_sources(s3, bucket: str, prefix: str, dest: str) -> int:
    n = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            rel = key[len(prefix):].lstrip("/")
            if not rel:
                continue
            local = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(local), exist_ok=True)
            s3.download_file(bucket, key, local)
            n += 1
    return n


def _put_result(s3, uri: str, result: dict) -> None:
    bucket, key = _parse_s3(uri)
    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(result).encode("utf-8"),
                  ContentType="application/json")


def _run_gitleaks(src: str, outdir: str, timeout: int) -> dict:
    """gitleaks로 secret 스캔 → normalize. 실패해도 fail_closed finding으로 흡수."""
    report_path = os.path.join(outdir, "gl.json")
    proc = subprocess.run(
        ["gitleaks", "detect", "--source", src, "--no-git",
         "--report-format", "json", "--report-path", report_path, "--exit-code", "0"],
        capture_output=True, timeout=timeout,
    )
    if proc.returncode not in (0, 1):
        return fail_closed(f"gitleaks exit {proc.returncode}: {proc.stderr.decode('utf-8','replace')[:400]}")
    with open(report_path) as fh:
        leaks = json.load(fh) if os.path.getsize(report_path) else []
    # gitleaks File은 임시 스캔 디렉토리(src) 기준 절대경로 → 자산 소스 기준 상대경로로.
    for leak in leaks:
        f = leak.get("File", "")
        if f:
            leak["File"] = os.path.relpath(f, src)
    return normalize_gitleaks(leaks)


def _semgrep_configs(rules_dir: str) -> list:
    """번들된 룰에서 언어별 보안 룰 디렉토리를 찾아 --config 인자 목록으로.

    최상위 전체를 config로 주면 taint/특수 룰이 섞여 오탐·저검출이 생겨요. 언어별
    `*/lang/security`(또는 `*/security`) 보안 룰만 골라 정확도를 높여요. 없으면 최상위 폴백.
    """
    configs = []
    for lang in sorted(os.listdir(rules_dir)):
        for sub in (os.path.join(lang, "lang", "security"), os.path.join(lang, "security")):
            full = os.path.join(rules_dir, sub)
            if os.path.isdir(full):
                configs.append(full)
    return configs or [rules_dir]


def _run_semgrep(src: str, outdir: str, timeout: int) -> dict:
    """이미지에 번들된 semgrep 공식 룰로 SAST 스캔 → normalize (air-gap, 인터넷 불요).

    --config는 SEMGREP_RULES_DIR(빌드 시 굽은 룰 디렉토리)의 언어별 보안 룰을 가리켜요.
    --config auto/레지스트리는 인터넷이 필요해 air-gap에서 못 써요. semgrep exit:
    0=findings없음, 1=있음, 그 외=오류.
    """
    report_path = os.path.join(outdir, "sg.json")
    rules_dir = os.getenv("SEMGREP_RULES_DIR", "/opt/semgrep-rules")
    cmd = ["semgrep", "scan"]
    for cfg in _semgrep_configs(rules_dir):
        cmd += ["--config", cfg]
    cmd += ["--json", "--output", report_path, "--quiet", "--metrics", "off", src]
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if proc.returncode not in (0, 1):
        return fail_closed(f"semgrep exit {proc.returncode}: {proc.stderr.decode('utf-8','replace')[:400]}")
    with open(report_path) as fh:
        data = json.load(fh) if os.path.getsize(report_path) else {"results": []}
    for r in data.get("results", []):
        p = r.get("path", "")
        if p:
            r["path"] = os.path.relpath(p, src)
    return normalize_semgrep(data)


def _log(msg: str) -> None:
    """CloudWatch에 남길 진행 로그. flush로 즉시 스트림에 반영해요(관측성)."""
    print(f"[scan-runner] {msg}", flush=True)


def main() -> int:
    input_uri = os.environ["INPUT_URI"]
    output_uri = os.environ["OUTPUT_URI"]
    scan_id = os.getenv("SCAN_ID", "?")
    timeout = int(os.getenv("SCAN_TIMEOUT", "240"))
    _log(f"start scan_id={scan_id} input={input_uri}")
    s3 = boto3.client("s3")
    try:
        in_bucket, in_prefix = _parse_s3(input_uri)
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as outdir:
            n = _download_sources(s3, in_bucket, in_prefix, src)
            _log(f"downloaded {n} source file(s)")
            # 각 스캐너를 독립 실행 — 하나가 죽어도 다른 area는 정상 검사되게. 개별
            # 예외는 그 area의 fail_closed로 흡수하고, scanned_areas엔 시도한 area를 남겨요.
            outcomes = []
            for area, runner in (("secret", _run_gitleaks), ("sast", _run_semgrep)):
                _log(f"scanner start: {area}")
                try:
                    out = runner(src, outdir, timeout)
                    _log(f"scanner done: {area} risk={out.get('risk')} findings={len(out.get('findings', []))}")
                    outcomes.append((area, out))
                except subprocess.TimeoutExpired:
                    _log(f"scanner TIMEOUT: {area} ({timeout}s)")
                    outcomes.append((area, fail_closed(f"{area} scan timeout {timeout}s")))
                except Exception as e:
                    _log(f"scanner ERROR: {area}: {type(e).__name__}: {e}")
                    outcomes.append((area, fail_closed(f"{area} scan error: {type(e).__name__}: {e}")))
            result = combine(outcomes)
    except Exception as e:  # 소스 로드 등 공통 단계 실패 → 전체 fail-closed
        _log(f"FATAL scan-runner error: {type(e).__name__}: {e}")
        result = fail_closed(f"scan-runner error: {type(e).__name__}: {e}")
    _log(f"result risk={result.get('risk')} areas={result.get('scanned_areas', [])} findings={len(result.get('findings', []))}")
    try:
        _put_result(s3, output_uri, result)
        _log(f"result written to {output_uri}")
    except Exception as e:
        print(f"[scan-runner] FATAL: result put 실패: {e}", file=sys.stderr, flush=True)
        return 0  # 어댑터가 result 부재를 fail-closed로 처리
    return 0


if __name__ == "__main__":
    sys.exit(main())
