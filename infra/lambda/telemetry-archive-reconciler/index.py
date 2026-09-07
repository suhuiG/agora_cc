"""Reconcile owned AgentCore log groups into the telemetry archive."""

from __future__ import annotations

import hashlib
import time
from typing import Any
from urllib.parse import quote

from config import TelemetryConfig


def _failure_detail(exc: Exception) -> str:
    """`AccessDeniedException` 하나로는 어느 API·권한이 막혔는지 알 수 없다.

    2026-08-24 실측: 78개 그룹이 모두 `reconcile_failed:AccessDeniedException`
    으로만 남아서 원인 API 를 CloudTrail 로만 찾을 수 있었다. botocore 가 들고 있는
    operation 이름과 오류 코드를 원장·반환값에 함께 남긴다. 메시지 본문은 로그 그룹
    이름 등 자원 식별자를 담을 수 있어 넣지 않는다.
    """
    name = type(exc).__name__
    operation = getattr(exc, "operation_name", None)
    code = (
        getattr(exc, "response", None) or {}
    ).get("Error", {}).get("Code")
    parts = [part for part in (operation, code or name) if part]
    return ":".join(str(part) for part in parts)


def _owned_runtime_log_groups(
    jobs_table: Any,
    *,
    prefix: str,
) -> set[str]:
    names: set[str] = set()
    request: dict[str, Any] = {
        "ProjectionExpression": "runtime_id, asset_type",
    }
    while True:
        page = jobs_table.scan(**request)
        for item in page.get("Items", ()):
            runtime_id = str(item.get("runtime_id") or "").strip()
            if item.get("asset_type") == "agent" and runtime_id:
                names.add(f"{prefix}{runtime_id}-DEFAULT")
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            return names
        request["ExclusiveStartKey"] = last_key


def _describe_exact_log_group(logs: Any, name: str) -> dict | None:
    page = logs.describe_log_groups(
        logGroupNamePrefix=name,
        limit=1,
    )
    return next(
        (
            dict(item)
            for item in page.get("logGroups", ())
            if item.get("logGroupName") == name
        ),
        None,
    )


def _subscription_status(
    logs: Any,
    *,
    log_group_name: str,
    filter_name: str,
    config: TelemetryConfig,
) -> tuple[bool, str, bool, dict[str, str]]:
    existing = logs.describe_subscription_filters(
        logGroupName=log_group_name,
    ).get("subscriptionFilters", ())
    same_name = next(
        (
            item
            for item in existing
            if item.get("filterName") == filter_name
        ),
        None,
    )
    if same_name is not None:
        observed = {
            "destination_arn": str(same_name.get("destinationArn") or ""),
            "role_arn": str(same_name.get("roleArn") or ""),
            "filter_pattern": str(same_name.get("filterPattern") or ""),
        }
        if (
            observed["destination_arn"] != config.destination_arn
            or observed["role_arn"] != config.subscription_role_arn
        ):
            return False, "foreign_filter_ownership", False, observed
        if observed["filter_pattern"] != "":
            return False, "subscription_filter_pattern_drift", False, observed
        return True, "", False, observed
    if len(existing) >= 2:
        return False, "subscription_filter_quota", False, {
            "destination_arn": "",
            "role_arn": "",
            "filter_pattern": "",
        }
    logs.put_subscription_filter(
        logGroupName=log_group_name,
        filterName=filter_name,
        filterPattern="",
        destinationArn=config.destination_arn,
        roleArn=config.subscription_role_arn,
    )
    return True, "", True, {
        "destination_arn": config.destination_arn,
        "role_arn": config.subscription_role_arn,
        "filter_pattern": "",
    }


class CoverageLedger:
    """Archive evidence for administrator review, never deletion approval."""

    def __init__(self, table: Any) -> None:
        self._table = table

    def get(self, log_group_name: str) -> dict:
        response = self._table.get_item(
            Key={"log_group_name": log_group_name},
            ConsistentRead=True,
        )
        return dict(response.get("Item") or {})

    def update(self, log_group_name: str, **fields: Any) -> dict:
        clean = {
            key: value
            for key, value in fields.items()
            if value is not None
        }
        names = {
            f"#field{index}": key
            for index, key in enumerate(clean)
        }
        values = {
            f":value{index}": value
            for index, value in enumerate(clean.values())
        }
        assignments = [
            f"{name} = :value{index}"
            for index, name in enumerate(names)
        ]
        response = self._table.update_item(
            Key={"log_group_name": log_group_name},
            UpdateExpression="SET " + ", ".join(assignments),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
        return dict(response.get("Attributes") or {})


def _backfill_prefix(stage: str, log_group_name: str) -> str:
    return (
        f"backfill/stage={stage}/"
        f"log-group={quote(log_group_name, safe='')}/"
    )


def _start_backfill(
    logs: Any,
    ledger: CoverageLedger,
    *,
    log_group_name: str,
    group: dict,
    state: dict,
    config: TelemetryConfig,
    subscription_confirmed_at: int,
) -> tuple[dict, str]:
    if state.get("backfill_status") == "COMPLETED":
        return state, ""
    task_id = str(state.get("backfill_task_id") or "")
    if task_id:
        tasks = logs.describe_export_tasks(taskId=task_id).get(
            "exportTasks", ()
        )
        if not tasks:
            return state, "backfill_task_unobservable"
        status = str((tasks[0].get("status") or {}).get("code") or "")
        if status == "COMPLETED":
            return ledger.update(
                log_group_name,
                backfill_status="COMPLETED",
                backfill_completed_at=int(time.time() * 1000),
            ), ""
        if status in {"FAILED", "CANCELLED"}:
            return ledger.update(
                log_group_name,
                backfill_status=status,
            ), f"backfill_{status.lower()}"
        return state, ""

    start = int(group.get("creationTime") or 0)
    if start >= subscription_confirmed_at:
        return ledger.update(
            log_group_name,
            backfill_status="COMPLETED",
            backfill_completed_at=subscription_confirmed_at,
            backfill_from=start,
            backfill_to=subscription_confirmed_at,
            backfill_evidence_required=False,
        ), ""
    digest = hashlib.sha256(log_group_name.encode()).hexdigest()[:16]
    try:
        response = logs.create_export_task(
            taskName=(
                f"agora-{config.stage}-{digest}-"
                f"{subscription_confirmed_at}"
            )[:512],
            logGroupName=log_group_name,
            fromTime=start,
            to=subscription_confirmed_at,
            destination=config.archive_bucket,
            destinationPrefix=_backfill_prefix(
                config.stage,
                log_group_name,
            ),
        )
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if code == "LimitExceededException":
            return ledger.update(
                log_group_name,
                backfill_status="PENDING",
            ), ""
        return state, f"backfill_start_failed:{_failure_detail(exc)}"
    return ledger.update(
        log_group_name,
        backfill_status="RUNNING",
        backfill_task_id=str(response["taskId"]),
        backfill_from=start,
        backfill_to=subscription_confirmed_at,
        backfill_evidence_required=True,
    ), ""


def _backfill_is_verified(state: dict) -> bool:
    if state.get("backfill_status") != "COMPLETED":
        return False
    return (
        state.get("backfill_evidence_required") is False
        or (
            bool(state.get("backfill_last_object_sha256"))
            and bool(state.get("backfill_last_manifest_key"))
        )
    )


def _delivery_is_fresh(
    state: dict,
    *,
    subscription_confirmed_at: int,
    now_ms: int,
    max_lag_seconds: int,
) -> bool:
    delivered_at = int(state.get("last_live_delivery_at") or 0)
    return (
        delivered_at >= subscription_confirmed_at
        and now_ms - delivered_at <= max_lag_seconds * 1000
        and bool(state.get("last_live_object_sha256"))
        and bool(state.get("last_live_manifest_key"))
    )


def reconcile(
    logs: Any,
    jobs_table: Any,
    coverage_table: Any,
    config: TelemetryConfig,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    fixed_now = now_ms is not None
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    confirmation_clock = (
        (lambda: now_ms)
        if fixed_now
        else (lambda: int(time.time() * 1000))
    )
    ledger = CoverageLedger(coverage_table)
    expected = _owned_runtime_log_groups(
        jobs_table,
        prefix=config.runtime_log_group_prefix,
    )
    if config.manage_shared_spans:
        expected.add(config.shared_span_log_group)

    matched: list[str] = []
    subscribed: list[str] = []
    covered: list[str] = []
    failed: dict[str, str] = {}
    subscription_drift: list[str] = []

    def reconcile_group(name: str) -> None:
        group = _describe_exact_log_group(logs, name)
        state = ledger.get(name)
        if group is None:
            failed[name] = "log_group_not_found"
            ledger.update(
                name,
                owner_stage=config.stage,
                coverage_status="unknown",
                failure_reason=failed[name],
                checked_at=now_ms,
            )
            return
        matched.append(name)
        filter_name = (
            config.shared_span_filter_name
            if name == config.shared_span_log_group
            else config.subscription_filter_name
        )
        owned, reason, subscription_created, observed_subscription = (
            _subscription_status(
                logs,
                log_group_name=name,
                filter_name=filter_name,
                config=config,
            )
        )
        if not owned:
            subscription_drift.append(name)
            failed[name] = reason
            state = ledger.update(
                name,
                owner_stage=config.stage,
                subscription_destination_arn=(
                    observed_subscription["destination_arn"]
                ),
                subscription_role_arn=observed_subscription["role_arn"],
                subscription_filter_pattern=(
                    observed_subscription["filter_pattern"]
                ),
                subscription_confirmed=False,
                coverage_status="unknown",
                failure_reason=reason,
                checked_at=now_ms,
            )
            return

        subscribed.append(name)
        previously_confirmed = state.get("subscription_confirmed") is True
        creation_time = int(group.get("creationTime") or 0)
        recorded_creation_time = state.get("log_group_creation_time")
        generation_changed = bool(state) and (
            recorded_creation_time is None
            or int(recorded_creation_time) != creation_time
        )
        subscription_generation_changed = previously_confirmed and any(
            state.get(field) != observed_subscription[value]
            for field, value in (
                ("subscription_destination_arn", "destination_arn"),
                ("subscription_role_arn", "role_arn"),
                ("subscription_filter_pattern", "filter_pattern"),
            )
        )
        subscription_reset = (
            subscription_created
            or generation_changed
            or subscription_generation_changed
            or not previously_confirmed
        )
        if subscription_reset and previously_confirmed:
            subscription_drift.append(name)
        confirmed_at = (
            confirmation_clock()
            if subscription_reset
            else int(state.get("subscription_confirmed_at") or now_ms)
        )
        state = ledger.update(
            name,
            owner_stage=config.stage,
            log_group_creation_time=creation_time,
            subscription_confirmed=True,
            subscription_confirmed_at=confirmed_at,
            subscription_destination_arn=(
                observed_subscription["destination_arn"]
            ),
            subscription_role_arn=observed_subscription["role_arn"],
            subscription_filter_pattern=(
                observed_subscription["filter_pattern"]
            ),
            failure_reason="",
            checked_at=now_ms,
            **(
                {
                    "backfill_status": "PENDING",
                    "backfill_task_id": "",
                    "backfill_last_object_key": "",
                    "backfill_last_object_sha256": "",
                    "backfill_last_manifest_key": "",
                    "backfill_object_count": 0,
                }
                if subscription_reset
                else {}
            ),
        )
        state, backfill_error = _start_backfill(
            logs,
            ledger,
            log_group_name=name,
            group=group,
            state=state,
            config=config,
            subscription_confirmed_at=confirmed_at,
        )
        if backfill_error:
            failed[name] = backfill_error
            state = ledger.update(
                name,
                coverage_status="unknown",
                failure_reason=backfill_error,
            )

        complete = (
            _backfill_is_verified(state)
            and _delivery_is_fresh(
                state,
                subscription_confirmed_at=confirmed_at,
                now_ms=now_ms,
                max_lag_seconds=config.max_ingest_lag_seconds,
            )
        )
        state = ledger.update(
            name,
            coverage_status="complete" if complete else "unknown",
        )
        if complete:
            covered.append(name)

    for name in sorted(expected):
        try:
            reconcile_group(name)
        except Exception as exc:
            failed[name] = f"reconcile_failed:{_failure_detail(exc)}"
            try:
                ledger.update(
                    name,
                    owner_stage=config.stage,
                    coverage_status="unknown",
                    failure_reason=failed[name],
                    checked_at=now_ms,
                )
            except Exception:
                pass

    coverage_status = (
        "unknown"
        if not expected
        or failed
        or len(matched) != len(expected)
        or len(subscribed) != len(expected)
        or len(covered) != len(expected)
        else "complete"
    )
    return {
        "stage": config.stage,
        "expected": sorted(expected),
        "matched": matched,
        "subscribed": subscribed,
        "covered": covered,
        "failed": failed,
        "coverage_status": coverage_status,
        "subscription_drift": subscription_drift,
    }


def _emit_coverage_metrics(cloudwatch: Any, result: dict[str, Any]) -> None:
    expected = len(result["expected"])
    failed = len(result["failed"])
    dimensions = [{"Name": "Stage", "Value": result["stage"]}]
    cloudwatch.put_metric_data(
        Namespace="Agora/TelemetryArchive",
        MetricData=[
            {
                "MetricName": "ExpectedLogGroups",
                "Value": expected,
                "Unit": "Count",
                "Dimensions": dimensions,
            },
            {
                "MetricName": "FailedLogGroups",
                "Value": failed,
                "Unit": "Count",
                "Dimensions": dimensions,
            },
            {
                "MetricName": "MatchedLogGroups",
                "Value": len(result["matched"]),
                "Unit": "Count",
                "Dimensions": dimensions,
            },
            {
                "MetricName": "SubscribedLogGroups",
                "Value": len(result["subscribed"]),
                "Unit": "Count",
                "Dimensions": dimensions,
            },
            {
                "MetricName": "CoveredLogGroups",
                "Value": len(result["covered"]),
                "Unit": "Count",
                "Dimensions": dimensions,
            },
            {
                "MetricName": "SubscriptionDrift",
                "Value": len(result["subscription_drift"]),
                "Unit": "Count",
                "Dimensions": dimensions,
            },
            {
                "MetricName": "CoverageIncomplete",
                "Value": 0 if result["coverage_status"] == "complete" else 1,
                "Unit": "Count",
                "Dimensions": dimensions,
            },
            {
                "MetricName": "ZeroExpected",
                "Value": 1 if expected == 0 else 0,
                "Unit": "Count",
                "Dimensions": dimensions,
            },
        ],
    )


def handler(_event: dict, _context: Any) -> dict[str, Any]:
    import boto3

    config = TelemetryConfig.from_env()
    dynamodb = boto3.resource("dynamodb")
    result = reconcile(
        boto3.client("logs"),
        dynamodb.Table(config.deploy_jobs_table),
        dynamodb.Table(config.coverage_table),
        config,
    )
    _emit_coverage_metrics(boto3.client("cloudwatch"), result)
    return result
