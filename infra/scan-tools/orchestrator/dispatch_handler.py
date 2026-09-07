"""dispatcher Lambda 핸들러 (SP-4 P2) — SF Map 브랜치가 호출.

item의 compute·image_ref를 보고 실제 도구를 실행하고 result를 반환해요:
  - image_ref 없음 → not_run(area를 scanned_areas에 넣지 않아 gate가 미실행 판정)
  - compute=lambda  → lambda.invoke(agora-tool-{tool_id}-{stage})
  - compute=fargate → ecs.run_task(agora-tool-{tool_id}-{stage}) + describe_tasks 폴링 → S3 result 읽기
도구 실패는 그 도구만 fail_closed(SCANNER_ERROR). 반환 {tool_id, area, risk, findings, scanned_areas}.
"""
from __future__ import annotations

import json
import os
import time


def _client(clients, name, region):
    if clients and name in clients:
        return clients[name]
    import boto3
    return boto3.client(name, region_name=region)


def _fail_closed(tool_id, area, reason):
    return {"tool_id": tool_id, "area": area, "risk": "high",
            "findings": [{"code": "SCANNER_ERROR", "severity": "high", "detail": reason, "location": ""}],
            "scanned_areas": [area]}


def handler(event, context, clients=None, sleep=time.sleep):
    tool_id = event.get("tool_id", "?")
    area = event.get("area", "")
    compute = event.get("compute", "lambda")
    image_ref = event.get("image_ref", "") or ""
    scan_id = event.get("scan_id", "?")
    bucket = event.get("bucket", "")
    in_prefix = event.get("input_prefix", f"input/{scan_id}/")
    out_prefix = event.get("output_prefix", f"output/{scan_id}/")
    stage = os.getenv("AGORA_STAGE", "dev")
    region = os.getenv("AWS_REGION", "ap-northeast-2")

    # 이미지 없는 도구 → 실행 스킵, not_run(area 미포함).
    if not image_ref:
        return {"tool_id": tool_id, "area": area, "risk": "none", "findings": [], "scanned_areas": []}

    input_uri = f"s3://{bucket}/{in_prefix}"
    out_key = f"{out_prefix}{tool_id}.json"
    output_uri = f"s3://{bucket}/{out_key}"
    try:
        if compute == "lambda":
            lam = _client(clients, "lambda", region)
            payload = {"input_uri": input_uri, "output_uri": output_uri, "scan_id": scan_id}
            if event.get("model_alias"):
                payload["model_alias"] = event["model_alias"]
            resp = lam.invoke(
                FunctionName=f"agora-tool-{tool_id}-{stage}",
                Payload=json.dumps(payload).encode())
            data = json.loads(resp["Payload"].read())
            return {"tool_id": tool_id, "area": area, "risk": data.get("risk", "high"),
                    "findings": data.get("findings", []), "scanned_areas": data.get("scanned_areas", [])}
        else:  # fargate
            ecs = _client(clients, "ecs", region)
            subnets = [s for s in os.getenv("SCAN_SUBNETS", "").split(",") if s]
            sgs = [s for s in os.getenv("SCAN_SG", "").split(",") if s]
            resp = ecs.run_task(
                cluster=os.getenv("SCAN_CLUSTER", ""),
                taskDefinition=f"agora-tool-{tool_id}-{stage}",
                launchType="FARGATE",
                networkConfiguration={"awsvpcConfiguration": {
                    "subnets": subnets, "securityGroups": sgs, "assignPublicIp": "DISABLED"}},
                overrides={"containerOverrides": [{
                    "name": "scanner",
                    "environment": [
                        {"name": "INPUT_URI", "value": input_uri},
                        {"name": "OUTPUT_URI", "value": output_uri},
                        {"name": "SCAN_ID", "value": scan_id},
                        {"name": "SCAN_TIMEOUT", "value": os.getenv("SCAN_TIMEOUT", "240")},
                    ]}]})
            if resp.get("failures") or not resp.get("tasks"):
                raise RuntimeError((resp.get("failures") or [{}])[0].get("reason", "run_task 실패"))
            task_arn = resp["tasks"][0]["taskArn"]
            deadline = time.monotonic() + int(os.getenv("SCAN_TIMEOUT", "300"))
            while True:
                desc = ecs.describe_tasks(cluster=os.getenv("SCAN_CLUSTER", ""), tasks=[task_arn])
                tasks = desc.get("tasks", [])
                if tasks and tasks[0].get("lastStatus") == "STOPPED":
                    break
                if time.monotonic() >= deadline:
                    return _fail_closed(tool_id, area, f"{tool_id} fargate 폴링 타임아웃")
                sleep(5)
            s3 = _client(clients, "s3", region)
            obj = s3.get_object(Bucket=bucket, Key=out_key)
            data = json.loads(obj["Body"].read())
            return {"tool_id": tool_id, "area": area, "risk": data.get("risk", "high"),
                    "findings": data.get("findings", []), "scanned_areas": data.get("scanned_areas", [])}
    except Exception as e:
        return _fail_closed(tool_id, area, f"{tool_id} dispatch 오류: {type(e).__name__}: {e}")
