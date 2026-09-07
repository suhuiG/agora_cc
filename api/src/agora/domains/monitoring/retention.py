"""Raw telemetry availability boundary shared by monitoring API consumers."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from enum import StrEnum
from importlib.resources import files

_POLICY = json.loads(
    files(__package__).joinpath("retention-policy.json").read_text(encoding="utf-8")
)
RAW_TELEMETRY_RETENTION_DAYS = int(_POLICY["rawTelemetryRetentionDays"])


class RawTelemetryPolicyWindow(StrEnum):
    CLOUDWATCH = "cloudwatch_retention_window"
    RESTORE = "archive_restore_window"


class RawTelemetryAvailability(StrEnum):
    OBSERVED = "observed"
    UNKNOWN = "unknown"


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("raw telemetry timestamps must be timezone-aware")


def raw_telemetry_retained_until(observed_at: datetime) -> datetime:
    _require_aware(observed_at)
    return observed_at + timedelta(days=RAW_TELEMETRY_RETENTION_DAYS)


def raw_telemetry_policy_window(
    observed_at: datetime,
    *,
    as_of: datetime,
) -> RawTelemetryPolicyWindow:
    _require_aware(as_of)
    return (
        RawTelemetryPolicyWindow.CLOUDWATCH
        if as_of < raw_telemetry_retained_until(observed_at)
        else RawTelemetryPolicyWindow.RESTORE
    )


def raw_telemetry_availability(
    *,
    cloudwatch_evidence: str | None = None,
    archive_evidence: str | None = None,
) -> dict:
    evidence = {
        location: value
        for location, value in (
            ("cloudwatch", cloudwatch_evidence),
            ("archive", archive_evidence),
        )
        if value
    }
    return {
        "status": (
            RawTelemetryAvailability.OBSERVED
            if evidence
            else RawTelemetryAvailability.UNKNOWN
        ),
        "locations": sorted(evidence),
        "evidence": evidence,
    }
