"""Manual replay for monitoring subscription events retained in the Lambda DLQ."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Callable, Literal

from botocore.exceptions import ClientError

from .ingest_lambda import _subscription_batch


@dataclass(frozen=True)
class ReplayReport:
    mode: Literal["dry-run", "apply"]
    status: Literal["dry-run", "partial", "completed"]
    discovered_count: int
    recovered_count: int
    failed_count: int
    recovered_intervals: tuple[tuple[str, str], ...]
    errors: dict[str, str]


def _event(message: dict) -> tuple[dict, str | None, str | None]:
    value = json.loads(message["Body"])
    if not isinstance(value, dict) or "awslogs" not in value:
        raise ValueError("DLQ body is not a CloudWatch Logs subscription event")
    batch = _subscription_batch(value)
    return value, batch.started_at, batch.ended_at


def _receive(
    sqs_client,
    *,
    queue_url: str,
    maximum: int,
    dry_run: bool,
) -> list[dict]:
    response = sqs_client.receive_message(
        QueueUrl=queue_url,
        MaxNumberOfMessages=min(10, maximum),
        WaitTimeSeconds=0 if dry_run else 1,
        VisibilityTimeout=0 if dry_run else 300,
    )
    return response.get("Messages", [])


def _sets(item: dict, name: str) -> set[str]:
    value = item.get(name)
    return {str(entry) for entry in value} if isinstance(value, set) else set()


def _queue_empty(sqs_client, *, queue_url: str) -> bool:
    attributes = sqs_client.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=[
            "ApproximateNumberOfMessages",
            "ApproximateNumberOfMessagesNotVisible",
        ],
    )["Attributes"]
    return (
        int(attributes["ApproximateNumberOfMessages"]) == 0
        and int(attributes["ApproximateNumberOfMessagesNotVisible"]) == 0
    )


def _partial_update(
    table,
    *,
    now: str,
    seen_ids: set[str],
    recovered_ids: set[str],
    recovered_intervals: set[str],
) -> None:
    failed = len(seen_ids - recovered_ids)
    set_parts = [
        "replay_status=:status",
        "replay_started_at=if_not_exists(replay_started_at,:now)",
        "replay_updated_at=:now",
        "replay_expected_count=:expected",
        "replay_recovered_count=:recovered",
        "replay_failed_count=:failed",
        "replay_seen_message_ids=:seen_ids",
    ]
    values: dict = {
        ":status": "PARTIAL",
        ":now": now,
        ":expected": len(seen_ids),
        ":recovered": len(recovered_ids),
        ":failed": failed,
        ":seen_ids": seen_ids,
    }
    if recovered_ids:
        set_parts.append("replay_recovered_message_ids=:recovered_ids")
        values[":recovered_ids"] = recovered_ids
    if recovered_intervals:
        set_parts.append("replay_recovered_intervals=:recovered_intervals")
        values[":recovered_intervals"] = recovered_intervals
    table.update_item(
        Key={"PK": "PIPELINE", "SK": "STATUS"},
        UpdateExpression=f"SET {', '.join(set_parts)}",
        ExpressionAttributeValues=values,
    )


def _complete_update(
    table,
    *,
    now: str,
    baseline: dict,
    seen_ids: set[str],
    recovered_ids: set[str],
    recovered_intervals: set[str],
) -> bool:
    set_parts = [
        "replay_status=:status",
        "replay_started_at=if_not_exists(replay_started_at,:now)",
        "replay_updated_at=:now",
        "replay_completed_at=:now",
        "replay_expected_count=:expected",
        "replay_recovered_count=:recovered",
        "replay_failed_count=:zero",
        "replay_seen_message_ids=:seen_ids",
        "replay_recovered_message_ids=:recovered_ids",
        "replay_resolved_failure_count=:resolved_failures",
    ]
    values: dict = {
        ":status": "COMPLETED",
        ":now": now,
        ":expected": len(seen_ids),
        ":recovered": len(recovered_ids),
        ":zero": 0,
        ":seen_ids": seen_ids,
        ":recovered_ids": recovered_ids,
        ":resolved_failures": int(baseline.get("failure_count", 0)),
    }
    if recovered_intervals:
        set_parts.append("replay_recovered_intervals=:recovered_intervals")
        values[":recovered_intervals"] = recovered_intervals
    if "failure_count" in baseline:
        condition = "failure_count=:baseline_failures"
        values[":baseline_failures"] = int(baseline["failure_count"])
    else:
        condition = "attribute_not_exists(failure_count)"
    try:
        table.update_item(
            Key={"PK": "PIPELINE", "SK": "STATUS"},
            UpdateExpression=(
                f"SET {', '.join(set_parts)} "
                "REMOVE failure_count, coverage_gap_started_at, "
                "coverage_gap_ended_at, failure_evidence_count"
            ),
            ConditionExpression=condition,
            ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == (
            "ConditionalCheckFailedException"
        ):
            return False
        raise
    return True


def replay_monitoring_dlq(
    *,
    sqs_client,
    queue_url: str,
    state_table,
    ingest: Callable[[dict, object | None], dict],
    apply: bool = False,
    max_messages: int = 100,
    now=None,
) -> ReplayReport:
    """Replay observed DLQ messages; dry-run only peeks at one SQS page."""
    if max_messages < 1:
        raise ValueError("max_messages must be positive")
    clock = now or (lambda: datetime.now(timezone.utc))
    baseline = (
        state_table.get_item(
            Key={"PK": "PIPELINE", "SK": "STATUS"},
            ConsistentRead=True,
        ).get("Item", {})
        if apply
        else {}
    )
    seen_ids = _sets(baseline, "replay_seen_message_ids")
    recovered_ids = _sets(baseline, "replay_recovered_message_ids")
    recovered_tokens = _sets(baseline, "replay_recovered_intervals")
    errors: dict[str, str] = {}
    discovered = 0
    drained = False

    while discovered < max_messages:
        messages = _receive(
            sqs_client,
            queue_url=queue_url,
            maximum=max_messages - discovered,
            dry_run=not apply,
        )
        if not messages:
            drained = True
            break
        for message in messages:
            message_id = str(message["MessageId"])
            discovered += 1
            if not apply:
                try:
                    _event(message)
                except Exception as exc:
                    errors[message_id] = f"{type(exc).__name__}: {exc}"
                continue
            seen_ids.add(message_id)
            try:
                event, started_at, ended_at = _event(message)
                ingest(event, None)
                sqs_client.delete_message(
                    QueueUrl=queue_url,
                    ReceiptHandle=message["ReceiptHandle"],
                )
            except Exception as exc:
                errors[message_id] = f"{type(exc).__name__}: {exc}"
                continue
            recovered_ids.add(message_id)
            if started_at is not None and ended_at is not None:
                recovered_tokens.add(
                    f"{message_id}|{started_at}|{ended_at}"
                )
        if not apply:
            break

    if not apply:
        return ReplayReport(
            mode="dry-run",
            status="dry-run",
            discovered_count=discovered,
            recovered_count=0,
            failed_count=len(errors),
            recovered_intervals=(),
            errors=errors,
        )

    if discovered == 0 and not seen_ids:
        return ReplayReport(
            mode="apply",
            status="partial",
            discovered_count=0,
            recovered_count=0,
            failed_count=0,
            recovered_intervals=(),
            errors={},
        )

    queue_observed_empty = False
    if drained and not errors:
        try:
            queue_observed_empty = _queue_empty(
                sqs_client,
                queue_url=queue_url,
            )
        except Exception as exc:
            errors["<queue-observation>"] = f"{type(exc).__name__}: {exc}"
    completed = (
        bool(seen_ids)
        and queue_observed_empty
        and not errors
        and seen_ids == recovered_ids
    )
    timestamp = clock().astimezone(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    if completed:
        completed = _complete_update(
            state_table,
            now=timestamp,
            baseline=baseline,
            seen_ids=seen_ids,
            recovered_ids=recovered_ids,
            recovered_intervals=recovered_tokens,
        )
    if not completed:
        _partial_update(
            state_table,
            now=timestamp,
            seen_ids=seen_ids,
            recovered_ids=recovered_ids,
            recovered_intervals=recovered_tokens,
        )
    intervals = tuple(sorted(
        (parts[1], parts[2])
        for token in recovered_tokens
        if len(parts := token.split("|", 2)) == 3
    ))
    return ReplayReport(
        mode="apply",
        status="completed" if completed else "partial",
        discovered_count=discovered,
        recovered_count=len(recovered_ids),
        failed_count=len(seen_ids - recovered_ids),
        recovered_intervals=intervals,
        errors=errors,
    )
