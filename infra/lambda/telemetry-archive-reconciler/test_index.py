import gzip
import importlib
import io
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


DIRECTORY = Path(__file__).parent
sys.path.insert(0, str(DIRECTORY))
config_module = importlib.import_module("config")
reconciler = importlib.import_module("index")
manifest = importlib.import_module("manifest")

RUNTIME_PREFIX = "/aws/bedrock-agentcore/runtimes/"
RUNTIME_ONE = f"{RUNTIME_PREFIX}rt-1-DEFAULT"
RUNTIME_TWO = f"{RUNTIME_PREFIX}rt-2-DEFAULT"
FOREIGN_RUNTIME = f"{RUNTIME_PREFIX}foreign-DEFAULT"


def telemetry_config(**overrides):
    values = {
        "stage": "dev",
        "runtime_log_group_prefix": RUNTIME_PREFIX,
        "shared_span_log_group": "aws/spans",
        "manage_shared_spans": False,
        "destination_arn": "arn:firehose:dev",
        "subscription_role_arn": "arn:iam:dev-logs-role",
        "subscription_filter_name": "agora-telemetry-archive-dev",
        "shared_span_filter_name": "agora-telemetry-archive-shared-spans",
        "deploy_jobs_table": "AgoraDeployJobs-dev",
        "coverage_table": "AgoraTelemetryCoverage-dev",
        "archive_bucket": "archive-dev",
        "max_ingest_lag_seconds": 900,
    }
    values.update(overrides)
    return config_module.TelemetryConfig(**values)


class FakeJobsTable:
    def __init__(self, runtime_ids=()):
        self.items = [
            {"asset_type": "agent", "runtime_id": runtime_id}
            for runtime_id in runtime_ids
        ]
        self.items.append({"asset_type": "mcp", "runtime_id": "not-agent"})

    def scan(self, **_request):
        return {"Items": self.items}


class FakeCoverageTable:
    def __init__(self, items=None):
        self.items = {
            key: {"log_group_name": key, **value}
            for key, value in (items or {}).items()
        }

    def get_item(self, *, Key, **_kwargs):
        item = self.items.get(Key["log_group_name"])
        return {"Item": dict(item)} if item else {}

    def update_item(self, *, Key, ExpressionAttributeValues, **request):
        item = self.items.setdefault(
            Key["log_group_name"],
            {"log_group_name": Key["log_group_name"]},
        )
        names = request.get("ExpressionAttributeNames")
        if names:
            for index, name in enumerate(names.values()):
                item[name] = ExpressionAttributeValues[f":value{index}"]
        else:
            expression = request["UpdateExpression"]
            mapping = {
                "backfill_last_object_key": ":key",
                "backfill_last_object_sha256": ":sha",
                "backfill_last_object_at": ":at",
                "backfill_last_manifest_key": ":manifest",
                "last_live_delivery_at": ":at",
                "last_live_object_key": ":key",
                "last_live_object_sha256": ":sha",
                "last_live_manifest_key": ":manifest",
            }
            for field, token in mapping.items():
                if field in expression:
                    item[field] = ExpressionAttributeValues[token]
            for field, token in {
                "backfill_object_count": ":one",
                "live_object_count": ":one",
                "live_event_count": ":events",
            }.items():
                if field in expression:
                    item[field] = item.get(field, 0) + (
                        ExpressionAttributeValues[token]
                    )
        return {"Attributes": dict(item)}


class FakeLogs:
    def __init__(self, groups, subscriptions=None):
        self.groups = groups
        self.subscriptions = {
            key: list(value)
            for key, value in (subscriptions or {}).items()
        }
        self.described_groups = []
        self.put_subscriptions = []
        self.created_exports = []
        self.export_tasks = {}

    def describe_log_groups(self, **request):
        prefix = request["logGroupNamePrefix"]
        self.described_groups.append(prefix)
        matches = [
            {"logGroupName": name, **details}
            for name, details in self.groups.items()
            if name.startswith(prefix)
        ]
        return {"logGroups": matches[: request.get("limit", 50)]}

    def describe_subscription_filters(self, *, logGroupName):
        return {
            "subscriptionFilters": list(
                self.subscriptions.get(logGroupName, ())
            )
        }

    def put_subscription_filter(self, **request):
        self.put_subscriptions.append(request)
        self.subscriptions.setdefault(
            request["logGroupName"], []
        ).append(request)

    def create_export_task(self, **request):
        task_id = f"task-{len(self.created_exports) + 1}"
        self.created_exports.append(request)
        self.export_tasks[task_id] = "PENDING"
        return {"taskId": task_id}

    def describe_export_tasks(self, *, taskId):
        status = self.export_tasks.get(taskId)
        return {
            "exportTasks": (
                [{"taskId": taskId, "status": {"code": status}}]
                if status
                else []
            )
        }


def owned_subscription(config, filter_name=None):
    return {
        "filterName": filter_name or config.subscription_filter_name,
        "filterPattern": "",
        "destinationArn": config.destination_arn,
        "roleArn": config.subscription_role_arn,
    }


def complete_coverage(now_ms):
    return {
        "log_group_creation_time": 1,
        "subscription_confirmed": True,
        "subscription_confirmed_at": now_ms - 60_000,
        "subscription_destination_arn": "arn:firehose:dev",
        "subscription_role_arn": "arn:iam:dev-logs-role",
        "subscription_filter_pattern": "",
        "backfill_status": "COMPLETED",
        "backfill_evidence_required": True,
        "backfill_last_object_sha256": "backfill-sha",
        "backfill_last_manifest_key": "manifests/backfill.json",
        "last_live_delivery_at": now_ms - 30_000,
        "last_live_object_sha256": "abc123",
        "last_live_manifest_key": "manifests/live.json",
    }


class ReconcilerTest(unittest.TestCase):
    def test_filter_confirmation_time_is_after_put_returns(self):
        logs = FakeLogs({RUNTIME_ONE: {"creationTime": 1}})

        with patch.object(
            reconciler.time,
            "time",
            side_effect=[1.0, 2.0],
        ):
            reconciler.reconcile(
                logs,
                FakeJobsTable(["rt-1"]),
                FakeCoverageTable(),
                telemetry_config(),
            )

        self.assertEqual(logs.created_exports[0]["to"], 2_000)

    def test_foreign_runtime_is_never_described_or_mutated(self):
        config = telemetry_config()
        logs = FakeLogs({
            RUNTIME_ONE: {"creationTime": 1},
            FOREIGN_RUNTIME: {"creationTime": 1},
        })

        result = reconciler.reconcile(
            logs,
            FakeJobsTable(["rt-1"]),
            FakeCoverageTable(),
            config,
            now_ms=1_000_000,
        )

        self.assertEqual(result["expected"], [RUNTIME_ONE])
        self.assertEqual(logs.described_groups, [RUNTIME_ONE])
        self.assertNotIn(FOREIGN_RUNTIME, str(logs.put_subscriptions))
        self.assertNotIn(FOREIGN_RUNTIME, str(logs.created_exports))

    def test_stage_filter_does_not_overwrite_another_stage(self):
        dev = telemetry_config()
        prod = telemetry_config(
            stage="prod",
            destination_arn="arn:firehose:prod",
            subscription_role_arn="arn:iam:prod-logs-role",
            subscription_filter_name="agora-telemetry-archive-prod",
        )
        logs = FakeLogs(
            {RUNTIME_ONE: {"creationTime": 1}},
            {RUNTIME_ONE: [owned_subscription(dev)]},
        )

        reconciler.reconcile(
            logs,
            FakeJobsTable(["rt-1"]),
            FakeCoverageTable(),
            prod,
            now_ms=1_000_000,
        )

        self.assertEqual(
            [item["filterName"] for item in logs.subscriptions[RUNTIME_ONE]],
            [
                "agora-telemetry-archive-dev",
                "agora-telemetry-archive-prod",
            ],
        )

    def test_shared_span_filter_allows_only_one_stack_owner(self):
        dev = telemetry_config(manage_shared_spans=True)
        prod = telemetry_config(
            stage="prod",
            manage_shared_spans=True,
            destination_arn="arn:firehose:prod",
            subscription_role_arn="arn:iam:prod-logs-role",
            subscription_filter_name="agora-telemetry-archive-prod",
        )
        logs = FakeLogs({"aws/spans": {"creationTime": 1}})

        reconciler.reconcile(
            logs,
            FakeJobsTable(),
            FakeCoverageTable(),
            dev,
            now_ms=1_000_000,
        )
        result = reconciler.reconcile(
            logs,
            FakeJobsTable(),
            FakeCoverageTable(),
            prod,
            now_ms=1_000_001,
        )

        self.assertEqual(len(logs.subscriptions["aws/spans"]), 1)
        self.assertEqual(
            result["failed"]["aws/spans"],
            "foreign_filter_ownership",
        )

    def test_same_name_with_foreign_destination_is_not_overwritten(self):
        config = telemetry_config()
        coverage = FakeCoverageTable()
        logs = FakeLogs(
            {RUNTIME_ONE: {"creationTime": 1}},
            {
                RUNTIME_ONE: [{
                    **owned_subscription(config),
                    "destinationArn": "arn:firehose:foreign",
                }],
            },
        )

        result = reconciler.reconcile(
            logs,
            FakeJobsTable(["rt-1"]),
            coverage,
            config,
            now_ms=1_000_000,
        )

        self.assertEqual(
            result["failed"][RUNTIME_ONE],
            "foreign_filter_ownership",
        )
        self.assertEqual(result["subscription_drift"], [RUNTIME_ONE])
        self.assertEqual(logs.put_subscriptions, [])
        self.assertEqual(result["coverage_status"], "unknown")
        self.assertEqual(
            coverage.items[RUNTIME_ONE]["subscription_destination_arn"],
            "arn:firehose:foreign",
        )
        self.assertFalse(
            coverage.items[RUNTIME_ONE]["subscription_confirmed"],
        )

    def test_adopting_previously_foreign_generation_resets_evidence(self):
        now_ms = 2_000_000
        old_config = telemetry_config()
        adopted_config = telemetry_config(
            destination_arn="arn:firehose:adopted",
            subscription_role_arn="arn:iam:adopted-role",
        )
        adopted_subscription = owned_subscription(adopted_config)
        logs = FakeLogs(
            {RUNTIME_ONE: {"creationTime": 1}},
            {RUNTIME_ONE: [adopted_subscription]},
        )
        coverage = FakeCoverageTable({
            RUNTIME_ONE: complete_coverage(now_ms),
        })

        first = reconciler.reconcile(
            logs,
            FakeJobsTable(["rt-1"]),
            coverage,
            old_config,
            now_ms=now_ms,
        )
        second = reconciler.reconcile(
            logs,
            FakeJobsTable(["rt-1"]),
            coverage,
            adopted_config,
            now_ms=now_ms + 1,
        )

        self.assertEqual(first["coverage_status"], "unknown")
        self.assertEqual(second["coverage_status"], "unknown")
        self.assertEqual(logs.created_exports[0]["to"], now_ms + 1)
        self.assertEqual(
            coverage.items[RUNTIME_ONE]["subscription_confirmed_at"],
            now_ms + 1,
        )

    def test_destination_and_role_generation_change_resets_coverage(self):
        now_ms = 2_000_000
        config = telemetry_config()
        logs = FakeLogs(
            {RUNTIME_ONE: {"creationTime": 1}},
            {RUNTIME_ONE: [owned_subscription(config)]},
        )
        previous = complete_coverage(now_ms)
        previous["subscription_destination_arn"] = "arn:firehose:old"
        previous["subscription_role_arn"] = "arn:iam:old-role"
        coverage = FakeCoverageTable({RUNTIME_ONE: previous})

        result = reconciler.reconcile(
            logs,
            FakeJobsTable(["rt-1"]),
            coverage,
            config,
            now_ms=now_ms,
        )

        self.assertEqual(result["coverage_status"], "unknown")
        self.assertEqual(result["subscription_drift"], [RUNTIME_ONE])
        self.assertEqual(logs.created_exports[0]["to"], now_ms)
        self.assertEqual(
            coverage.items[RUNTIME_ONE]["subscription_destination_arn"],
            config.destination_arn,
        )
        self.assertEqual(
            coverage.items[RUNTIME_ONE]["subscription_role_arn"],
            config.subscription_role_arn,
        )

    def test_filter_pattern_drift_is_unknown(self):
        now_ms = 2_000_000
        config = telemetry_config()
        drifted = {
            **owned_subscription(config),
            "filterPattern": '"ERROR"',
        }
        coverage = FakeCoverageTable({
            RUNTIME_ONE: complete_coverage(now_ms),
        })

        result = reconciler.reconcile(
            FakeLogs(
                {RUNTIME_ONE: {"creationTime": 1}},
                {RUNTIME_ONE: [drifted]},
            ),
            FakeJobsTable(["rt-1"]),
            coverage,
            config,
            now_ms=now_ms,
        )

        self.assertEqual(
            result["failed"][RUNTIME_ONE],
            "subscription_filter_pattern_drift",
        )
        self.assertEqual(result["coverage_status"], "unknown")
        self.assertEqual(
            coverage.items[RUNTIME_ONE]["subscription_filter_pattern"],
            '"ERROR"',
        )

    def test_full_backfill_covers_the_pre_subscription_interval(self):
        config = telemetry_config()
        logs = FakeLogs(
            {RUNTIME_ONE: {"creationTime": 100}},
            {RUNTIME_ONE: [owned_subscription(config)]},
        )
        coverage = FakeCoverageTable({
            RUNTIME_ONE: {
                "log_group_creation_time": 100,
                "subscription_confirmed": True,
                "subscription_confirmed_at": 900_000,
                "subscription_destination_arn": config.destination_arn,
                "subscription_role_arn": config.subscription_role_arn,
                "subscription_filter_pattern": "",
                "backfill_status": "PENDING",
                "last_live_delivery_at": 950_000,
                "last_live_object_sha256": "sha",
            },
        })

        reconciler.reconcile(
            logs,
            FakeJobsTable(["rt-1"]),
            coverage,
            config,
            now_ms=1_000_000,
        )

        self.assertEqual(logs.created_exports[0]["fromTime"], 100)
        self.assertEqual(logs.created_exports[0]["to"], 900_000)

    def test_recreated_subscription_resets_backfill_evidence(self):
        now_ms = 2_000_000
        config = telemetry_config()
        logs = FakeLogs({
            RUNTIME_ONE: {"creationTime": 1},
        })
        coverage = FakeCoverageTable({
            RUNTIME_ONE: complete_coverage(now_ms),
        })

        reconciler.reconcile(
            logs,
            FakeJobsTable(["rt-1"]),
            coverage,
            config,
            now_ms=now_ms,
        )

        self.assertEqual(
            coverage.items[RUNTIME_ONE]["backfill_status"],
            "RUNNING",
        )
        self.assertEqual(
            coverage.items[RUNTIME_ONE]["subscription_confirmed_at"],
            now_ms,
        )

    def test_recreated_log_group_cannot_reuse_previous_coverage(self):
        now_ms = 2_000_000
        config = telemetry_config()
        logs = FakeLogs(
            {RUNTIME_ONE: {"creationTime": 2}},
            {RUNTIME_ONE: [owned_subscription(config)]},
        )
        coverage = FakeCoverageTable({
            RUNTIME_ONE: complete_coverage(now_ms),
        })

        reconciler.reconcile(
            logs,
            FakeJobsTable(["rt-1"]),
            coverage,
            config,
            now_ms=now_ms,
        )

        self.assertEqual(logs.created_exports[0]["fromTime"], 2)
        self.assertEqual(logs.created_exports[0]["to"], now_ms)
        self.assertEqual(
            coverage.items[RUNTIME_ONE]["backfill_last_object_sha256"],
            "",
        )

    def test_backfill_checksum_is_required_for_complete_coverage(self):
        now_ms = 2_000_000
        config = telemetry_config()
        logs = FakeLogs(
            {RUNTIME_ONE: {"creationTime": 1}},
            {RUNTIME_ONE: [owned_subscription(config)]},
        )
        state = complete_coverage(now_ms)
        state.pop("backfill_last_object_sha256")
        state.pop("backfill_last_manifest_key")

        result = reconciler.reconcile(
            logs,
            FakeJobsTable(["rt-1"]),
            FakeCoverageTable({RUNTIME_ONE: state}),
            config,
            now_ms=now_ms,
        )

        self.assertEqual(result["coverage_status"], "unknown")

    def test_quota_failure_does_not_stop_later_groups(self):
        config = telemetry_config()
        logs = FakeLogs(
            {
                RUNTIME_ONE: {"creationTime": 1},
                RUNTIME_TWO: {"creationTime": 1},
            },
            {
                RUNTIME_ONE: [
                    {"filterName": "foreign-a"},
                    {"filterName": "foreign-b"},
                ],
            },
        )

        result = reconciler.reconcile(
            logs,
            FakeJobsTable(["rt-1", "rt-2"]),
            FakeCoverageTable(),
            config,
            now_ms=1_000_000,
        )

        self.assertEqual(
            result["failed"][RUNTIME_ONE],
            "subscription_filter_quota",
        )
        self.assertEqual(result["subscription_drift"], [RUNTIME_ONE])
        self.assertIn(RUNTIME_TWO, result["subscribed"])
        self.assertEqual(
            logs.put_subscriptions[0]["logGroupName"],
            RUNTIME_TWO,
        )

    def test_unexpected_group_failure_does_not_stop_later_groups(self):
        class FailingLogs(FakeLogs):
            def describe_subscription_filters(self, *, logGroupName):
                if logGroupName == RUNTIME_ONE:
                    raise RuntimeError("probe failure")
                return super().describe_subscription_filters(
                    logGroupName=logGroupName,
                )

        logs = FailingLogs({
            RUNTIME_ONE: {"creationTime": 1},
            RUNTIME_TWO: {"creationTime": 1},
        })

        result = reconciler.reconcile(
            logs,
            FakeJobsTable(["rt-1", "rt-2"]),
            FakeCoverageTable(),
            telemetry_config(),
            now_ms=1_000_000,
        )

        self.assertEqual(
            result["failed"][RUNTIME_ONE],
            "reconcile_failed:RuntimeError",
        )
        self.assertIn(RUNTIME_TWO, result["subscribed"])

    def test_failure_reason_names_the_denied_api_and_code(self):
        """AccessDeniedException 하나만 남으면 어느 권한인지 알 수 없다."""

        class DeniedError(Exception):
            operation_name = "PutSubscriptionFilter"
            response = {
                "Error": {
                    "Code": "AccessDeniedException",
                    "Message": "not authorized to perform ...",
                },
            }

        class DeniedLogs(FakeLogs):
            def put_subscription_filter(self, **kwargs):
                raise DeniedError()

        logs = DeniedLogs({RUNTIME_ONE: {"creationTime": 1}})

        result = reconciler.reconcile(
            logs,
            FakeJobsTable(["rt-1"]),
            FakeCoverageTable(),
            telemetry_config(),
            now_ms=1_000_000,
        )

        self.assertEqual(
            result["failed"][RUNTIME_ONE],
            "reconcile_failed:PutSubscriptionFilter:AccessDeniedException",
        )
        self.assertEqual(result["coverage_status"], "unknown")

    def test_zero_expected_is_unknown_not_success(self):
        result = reconciler.reconcile(
            FakeLogs({}),
            FakeJobsTable(),
            FakeCoverageTable(),
            telemetry_config(),
            now_ms=1_000_000,
        )

        self.assertEqual(result["expected"], [])
        self.assertEqual(result["coverage_status"], "unknown")

    def test_same_subscription_generation_keeps_complete_coverage(self):
        now_ms = 2_000_000
        config = telemetry_config()
        logs = FakeLogs(
            {RUNTIME_ONE: {"creationTime": 1}},
            {RUNTIME_ONE: [owned_subscription(config)]},
        )

        result = reconciler.reconcile(
            logs,
            FakeJobsTable(["rt-1"]),
            FakeCoverageTable({
                RUNTIME_ONE: complete_coverage(now_ms),
            }),
            config,
            now_ms=now_ms,
        )

        self.assertEqual(result["coverage_status"], "complete")
        self.assertEqual(result["covered"], [RUNTIME_ONE])
        self.assertEqual(logs.created_exports, [])


class ManifestTest(unittest.TestCase):
    def test_live_object_records_sha256_and_per_group_delivery(self):
        document = {
            "messageType": "DATA_MESSAGE",
            "logGroup": RUNTIME_ONE,
            "logEvents": [{"id": "1"}, {"id": "2"}],
        }
        payload = gzip.compress(
            __import__("json").dumps(document).encode()
        )
        table = FakeCoverageTable()
        s3 = _FakeS3(payload)

        groups = manifest.record_object(
            s3,
            table,
            bucket="archive-dev",
            key="stage=dev/year=2026/object",
            event_time_ms=1_000_000,
        )

        self.assertEqual(groups, [RUNTIME_ONE])
        item = table.items[RUNTIME_ONE]
        self.assertEqual(item["last_live_delivery_at"], 1_000_000)
        self.assertEqual(item["live_event_count"], 2)
        self.assertEqual(len(item["last_live_object_sha256"]), 64)
        self.assertEqual(
            item["last_live_manifest_key"],
            s3.puts[0]["Key"],
        )
        self.assertTrue(s3.puts[0]["Key"].startswith("manifests/"))
        self.assertIn("ChecksumSHA256", s3.puts[0])

    def test_backfill_object_records_independent_checksum(self):
        table = FakeCoverageTable()
        s3 = _FakeS3(b"history")
        encoded = __import__("urllib.parse").parse.quote(
            RUNTIME_ONE, safe=""
        )

        groups = manifest.record_object(
            s3,
            table,
            bucket="archive-dev",
            key=f"backfill/stage=dev/log-group={encoded}/part.gz",
            event_time_ms=1_000_000,
        )

        self.assertEqual(groups, [RUNTIME_ONE])
        self.assertEqual(
            len(table.items[RUNTIME_ONE]["backfill_last_object_sha256"]),
            64,
        )
        self.assertEqual(
            table.items[RUNTIME_ONE]["backfill_last_manifest_key"],
            s3.puts[0]["Key"],
        )


class ConfigTest(unittest.TestCase):
    def test_config_requires_lambda_role_and_exact_stage_filter(self):
        env = {
            "AGORA_ROLE": "portal",
            "AGORA_TELEMETRY_STAGE": "dev",
            "AGORA_TELEMETRY_RUNTIME_LOG_GROUP_PREFIX": RUNTIME_PREFIX,
            "AGORA_TELEMETRY_SHARED_SPAN_LOG_GROUP": "aws/spans",
            "AGORA_TELEMETRY_MANAGE_SHARED_SPANS": "false",
            "AGORA_TELEMETRY_DESTINATION_ARN": "arn:firehose:dev",
            "AGORA_TELEMETRY_SUBSCRIPTION_ROLE_ARN": "arn:iam:role",
            "AGORA_TELEMETRY_SUBSCRIPTION_FILTER_NAME":
                "agora-telemetry-archive-dev",
            "AGORA_TELEMETRY_SHARED_SPAN_FILTER_NAME":
                "agora-telemetry-archive-shared-spans",
            "AGORA_TELEMETRY_DEPLOY_JOBS_TABLE": "jobs",
            "AGORA_TELEMETRY_COVERAGE_TABLE": "coverage",
            "AGORA_TELEMETRY_ARCHIVE_BUCKET": "bucket",
            "AGORA_TELEMETRY_MAX_INGEST_LAG_SECONDS": "900",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "AGORA_ROLE=lambda"):
                config_module.TelemetryConfig.from_env()
        env["AGORA_ROLE"] = "lambda"
        env["AGORA_TELEMETRY_SUBSCRIPTION_FILTER_NAME"] = "foreign"
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "must equal"):
                config_module.TelemetryConfig.from_env()
        env["AGORA_TELEMETRY_SUBSCRIPTION_FILTER_NAME"] = (
            "agora-telemetry-archive-dev"
        )
        env["AGORA_TELEMETRY_SHARED_SPAN_FILTER_NAME"] = "stage-specific"
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "SHARED_SPAN_FILTER"):
                config_module.TelemetryConfig.from_env()


class _FakeS3:
    def __init__(self, payload):
        self.payload = payload
        self.puts = []

    def get_object(self, **_request):
        return {"Body": io.BytesIO(self.payload)}

    def put_object(self, **request):
        self.puts.append(request)
        return {}


if __name__ == "__main__":
    unittest.main()
