"""Typed configuration for the telemetry archive Lambdas."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _boolean(name: str) -> bool:
    value = _required(name).lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be 'true' or 'false'")
    return value == "true"


def _positive_integer(name: str) -> int:
    value = _required(name)
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return parsed


@dataclass(frozen=True)
class TelemetryConfig:
    stage: str
    runtime_log_group_prefix: str
    shared_span_log_group: str
    manage_shared_spans: bool
    destination_arn: str
    subscription_role_arn: str
    subscription_filter_name: str
    shared_span_filter_name: str
    deploy_jobs_table: str
    coverage_table: str
    archive_bucket: str
    max_ingest_lag_seconds: int

    @classmethod
    def from_env(cls) -> "TelemetryConfig":
        if _required("AGORA_ROLE") != "lambda":
            raise ValueError("telemetry archive requires AGORA_ROLE=lambda")
        stage = _required("AGORA_TELEMETRY_STAGE")
        if stage not in {"dev", "prod"}:
            raise ValueError("AGORA_TELEMETRY_STAGE must be 'dev' or 'prod'")
        filter_name = _required(
            "AGORA_TELEMETRY_SUBSCRIPTION_FILTER_NAME"
        )
        expected_filter_name = f"agora-telemetry-archive-{stage}"
        if filter_name != expected_filter_name:
            raise ValueError(
                "AGORA_TELEMETRY_SUBSCRIPTION_FILTER_NAME must equal "
                f"{expected_filter_name!r}"
            )
        shared_span_filter_name = _required(
            "AGORA_TELEMETRY_SHARED_SPAN_FILTER_NAME"
        )
        if shared_span_filter_name != "agora-telemetry-archive-shared-spans":
            raise ValueError(
                "AGORA_TELEMETRY_SHARED_SPAN_FILTER_NAME must equal "
                "'agora-telemetry-archive-shared-spans'"
            )
        return cls(
            stage=stage,
            runtime_log_group_prefix=_required(
                "AGORA_TELEMETRY_RUNTIME_LOG_GROUP_PREFIX"
            ),
            shared_span_log_group=_required(
                "AGORA_TELEMETRY_SHARED_SPAN_LOG_GROUP"
            ),
            manage_shared_spans=_boolean(
                "AGORA_TELEMETRY_MANAGE_SHARED_SPANS"
            ),
            destination_arn=_required(
                "AGORA_TELEMETRY_DESTINATION_ARN"
            ),
            subscription_role_arn=_required(
                "AGORA_TELEMETRY_SUBSCRIPTION_ROLE_ARN"
            ),
            subscription_filter_name=filter_name,
            shared_span_filter_name=shared_span_filter_name,
            deploy_jobs_table=_required(
                "AGORA_TELEMETRY_DEPLOY_JOBS_TABLE"
            ),
            coverage_table=_required(
                "AGORA_TELEMETRY_COVERAGE_TABLE"
            ),
            archive_bucket=_required(
                "AGORA_TELEMETRY_ARCHIVE_BUCKET"
            ),
            max_ingest_lag_seconds=_positive_integer(
                "AGORA_TELEMETRY_MAX_INGEST_LAG_SECONDS"
            ),
        )
