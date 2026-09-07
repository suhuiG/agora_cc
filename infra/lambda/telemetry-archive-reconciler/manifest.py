"""Record independent S3 delivery evidence for archived telemetry."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
from datetime import datetime
from typing import Any
from urllib.parse import unquote

from config import TelemetryConfig


def _json_documents(payload: bytes) -> list[dict]:
    try:
        decoded = gzip.decompress(payload).decode("utf-8")
    except (gzip.BadGzipFile, UnicodeDecodeError):
        decoded = payload.decode("utf-8")
    decoder = json.JSONDecoder()
    documents: list[dict] = []
    offset = 0
    while offset < len(decoded):
        while offset < len(decoded) and decoded[offset].isspace():
            offset += 1
        if offset == len(decoded):
            break
        value, offset = decoder.raw_decode(decoded, offset)
        if isinstance(value, dict):
            documents.append(value)
    return documents


def _backfill_log_group(key: str) -> str | None:
    for part in key.split("/"):
        if part.startswith("log-group="):
            return unquote(part.removeprefix("log-group="))
    return None


def _write_immutable_manifest(
    s3: Any,
    *,
    bucket: str,
    source_key: str,
    source_sha256: str,
    event_time_ms: int,
    groups: dict[str, int | None],
) -> str:
    key_digest = hashlib.sha256(source_key.encode()).hexdigest()
    manifest_key = (
        f"manifests/source-key-sha256={key_digest}/"
        f"object-sha256={source_sha256}.json"
    )
    body = json.dumps(
        {
            "bucket": bucket,
            "event_time_ms": event_time_ms,
            "groups": groups,
            "source_key": source_key,
            "source_sha256": source_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    s3.put_object(
        Bucket=bucket,
        Key=manifest_key,
        Body=body,
        ContentType="application/json",
        ChecksumSHA256=base64.b64encode(
            hashlib.sha256(body).digest()
        ).decode(),
    )
    return manifest_key


def record_object(
    s3: Any,
    coverage_table: Any,
    *,
    bucket: str,
    key: str,
    event_time_ms: int,
) -> list[str]:
    response = s3.get_object(Bucket=bucket, Key=key)
    payload = response["Body"].read()
    sha256 = hashlib.sha256(payload).hexdigest()
    if key.startswith("backfill/"):
        log_group = _backfill_log_group(key)
        if not log_group:
            raise ValueError(f"backfill key has no log-group partition: {key}")
        manifest_key = _write_immutable_manifest(
            s3,
            bucket=bucket,
            source_key=key,
            source_sha256=sha256,
            event_time_ms=event_time_ms,
            groups={log_group: None},
        )
        coverage_table.update_item(
            Key={"log_group_name": log_group},
            UpdateExpression=(
                "SET backfill_last_object_key = :key, "
                "backfill_last_object_sha256 = :sha, "
                "backfill_last_object_at = :at, "
                "backfill_last_manifest_key = :manifest "
                "ADD backfill_object_count :one"
            ),
            ExpressionAttributeValues={
                ":key": key,
                ":sha": sha256,
                ":at": event_time_ms,
                ":manifest": manifest_key,
                ":one": 1,
            },
        )
        return [log_group]

    groups: dict[str, int] = {}
    for document in _json_documents(payload):
        log_group = str(document.get("logGroup") or "")
        if not log_group:
            continue
        groups[log_group] = groups.get(log_group, 0) + len(
            document.get("logEvents") or ()
        )
    if not groups:
        raise ValueError(f"live archive object has no logGroup evidence: {key}")
    manifest_key = _write_immutable_manifest(
        s3,
        bucket=bucket,
        source_key=key,
        source_sha256=sha256,
        event_time_ms=event_time_ms,
        groups=groups,
    )
    for log_group, event_count in groups.items():
        coverage_table.update_item(
            Key={"log_group_name": log_group},
            UpdateExpression=(
                "SET last_live_delivery_at = :at, "
                "last_live_object_key = :key, "
                "last_live_object_sha256 = :sha, "
                "last_live_manifest_key = :manifest "
                "ADD live_object_count :one, live_event_count :events"
            ),
            ExpressionAttributeValues={
                ":at": event_time_ms,
                ":key": key,
                ":sha": sha256,
                ":manifest": manifest_key,
                ":one": 1,
                ":events": event_count,
            },
        )
    return sorted(groups)


def handler(event: dict, _context: Any) -> dict[str, Any]:
    import boto3

    config = TelemetryConfig.from_env()
    s3 = boto3.client("s3")
    table = boto3.resource("dynamodb").Table(config.coverage_table)
    observed: set[str] = set()
    for record in event.get("Records", ()):
        bucket = record["s3"]["bucket"]["name"]
        if bucket != config.archive_bucket:
            raise ValueError(f"unexpected archive bucket: {bucket}")
        key = unquote(record["s3"]["object"]["key"])
        event_time = record["eventTime"]
        timestamp_ms = int(
            datetime.fromisoformat(
                event_time.replace("Z", "+00:00")
            ).timestamp()
            * 1000
        )
        observed.update(
            record_object(
                s3,
                table,
                bucket=bucket,
                key=key,
                event_time_ms=timestamp_ms,
            )
        )
    return {"observed_log_groups": sorted(observed)}
