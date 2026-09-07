"""LLM-judge Lambda 핸들러 — S3 소스 read → Bedrock(등급별 모델) → 위협 판정 → S3 result.json.

event: {"input_uri": "s3://b/input/{id}/", "output_uri": "s3://b/output/{id}/llm-judge.json",
        "scan_id", "model_alias"(optional, 없으면 haiku-4-5 폴백)}
계약: 항상 결과 반환(예외는 fail_closed). result.json = {"risk","findings","scanned_areas"}.

주의: 분석 대상은 신뢰할 수 없는 업로드 콘텐츠예요. rubric.py의 인젝션 방어 프롬프트로
Bedrock에 격리 전달하고, normalize_llm_judge가 모델 출력을 한 번 더 정제해요.
"""
from __future__ import annotations

import json
import os
from urllib.parse import urlparse

import boto3

from normalize import normalize_llm_judge, fail_closed
from rubric import SYSTEM_RUBRIC, build_messages

# ---------------------------------------------------------------------------
# 모델 별칭 → Bedrock inference profile ID 매핑 (Global Constraint)
# 실측(2026-07-17 ap-northeast-2 e2e): Sonnet 5 등은 on-demand 직접 호출 불가
# ("ValidationException: on-demand throughput isn't supported") — inference profile
# ID(global. 프리픽스)를 써야 해요. list-inference-profiles로 실측한 정확한 ID예요.
# ---------------------------------------------------------------------------

_MODEL_IDS = {
    "haiku-4-5": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "sonnet-4-6": "global.anthropic.claude-sonnet-4-6",
    "sonnet-5": "global.anthropic.claude-sonnet-5",
}


def _model_id(alias: str) -> str:
    """별칭 → Bedrock 모델 ID. 알 수 없는 별칭은 haiku-4-5 폴백."""
    return _MODEL_IDS.get(alias, _MODEL_IDS["haiku-4-5"])


# ---------------------------------------------------------------------------
# S3 헬퍼
# ---------------------------------------------------------------------------

def _parse_s3(uri: str) -> tuple[str, str]:
    p = urlparse(uri)
    return p.netloc, p.path.lstrip("/")


def _download_text(s3, bucket: str, prefix: str) -> str:
    """S3 prefix 아래 모든 객체를 읽어 텍스트로 이어붙여요. (테스트에서 monkeypatch.)"""
    parts: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            try:
                parts.append(body.decode("utf-8", errors="replace"))
            except Exception:
                parts.append(repr(body))
    return "\n".join(parts)


def _put(s3, uri: str, result: dict) -> None:
    bucket, key = _parse_s3(uri)
    s3.put_object(
        Bucket=bucket, Key=key,
        Body=json.dumps(result).encode("utf-8"),
        ContentType="application/json",
    )


# ---------------------------------------------------------------------------
# Bedrock 호출 (테스트에서 monkeypatch)
# ---------------------------------------------------------------------------

def _bedrock_judge(src_text: str, model_id: str) -> dict:
    """Bedrock invoke_model → 구조화 판정 dict 반환.

    ASSUMPTION: Bedrock 응답은 report_service.py(lines 141-149)에서 검증된
    invoke_model 형태를 따라요. content[].text를 이어붙여 strict JSON으로 파싱해요.
    실제 Claude 모델이 스키마를 준수하는지는 Task 7 배포 시 e2e로 검증 예정이에요.
    """
    client = boto3.client("bedrock-runtime", region_name=os.getenv("AWS_REGION", "ap-northeast-2"))
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 4000,
        "system": SYSTEM_RUBRIC,
        "messages": build_messages(src_text),
    }
    resp = client.invoke_model(modelId=model_id, body=json.dumps(body))
    out = json.loads(resp["body"].read())
    text = "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text")
    return _extract_json(text)  # 파싱 실패 시 ValueError → 호출자가 fail_closed 처리


def _extract_json(text: str) -> dict:
    """모델 응답 텍스트에서 JSON 객체를 추출해요.

    실측(2026-07-17): Claude는 순수 JSON이 아니라 ```json 코드펜스 + 설명 산문을
    섞어서 반환해요. 코드펜스를 걷어내고, 그래도 안 되면 첫 '{'~마지막 '}'를
    슬라이스해 파싱해요(설명 텍스트가 앞뒤로 붙어도 견고).
    """
    t = text.strip()
    # ```json ... ``` 또는 ``` ... ``` 코드펜스 제거
    if t.startswith("```"):
        t = t.split("```", 2)[1] if t.count("```") >= 2 else t.lstrip("`")
        if t.lstrip().startswith("json"):
            t = t.lstrip()[4:]
        t = t.strip()
    try:
        return json.loads(t)
    except ValueError:
        # 설명 산문이 앞뒤로 붙은 경우: 첫 '{'~마지막 '}' 슬라이스.
        start, end = t.find("{"), t.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(t[start:end + 1])


# ---------------------------------------------------------------------------
# 핵심 스캔 로직 (_scan) — 테스트가 직접 호출
# ---------------------------------------------------------------------------

def _scan(s3, input_uri: str, output_uri: str, model_alias: str, scan_id: str) -> dict:
    """S3 소스 다운로드 → Bedrock 판정 → normalize → S3 기록 → 결과 dict 반환."""
    try:
        in_bucket, in_prefix = _parse_s3(input_uri)
        src_text = _download_text(s3, in_bucket, in_prefix)
        mid = _model_id(model_alias)
        verdict = _bedrock_judge(src_text, mid)
        result = normalize_llm_judge(verdict)
    except Exception as e:
        result = fail_closed(f"llm-judge lambda error: {type(e).__name__}: {e}")

    try:
        _put(s3, output_uri, result)
    except Exception:
        pass  # 결과 write 실패 → 오케스트레이터가 result 부재를 fail-closed 처리

    return result


# ---------------------------------------------------------------------------
# Lambda 핸들러 (얇은 래퍼)
# ---------------------------------------------------------------------------

def handler(event: dict, context) -> dict:
    scan_id = event.get("scan_id", "?")
    model_alias = event.get("model_alias", "haiku-4-5")
    # scan_id를 로그에 남겨 콘솔 게이트 로그 modal이 CloudWatch filter(scan_id)로 조회해요.
    print(f"[llm-judge] scan {scan_id} start model={model_alias}", flush=True)
    s3 = boto3.client("s3", region_name=os.getenv("AWS_REGION", "ap-northeast-2"))
    result = _scan(s3, event["input_uri"], event["output_uri"], model_alias, scan_id)
    print(f"[llm-judge] scan {scan_id} done: risk={result.get('risk')} "
          f"findings={len(result.get('findings', []))}", flush=True)
    return {"scan_id": scan_id, "area": "agent_intent", **result}
