"""DynamoJobStore — DeployJob를 DynamoDB에 왕복(aws 백엔드).

PK=JOB#{job_id}. to_dict/from_dict로 직렬화하고, dict/list 필드는 DDB Map/List로
그대로 저장돼요(boto3 resource Table).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from .models import (
    AgentCompensationTarget,
    AgentMonitoringJobProjection,
    DeployJob,
    DeployPhase,
)
from .monitoring_projection import monitoring_projection

_MONITORING_FIELDS = (
    "job_id",
    "record_id",
    "phase",
    "source_version",
    "created_at",
    "updated_at",
    "otel_instrumentation_status",
    "verify_verdict",
    "observed_tools",
    "observed_source",
    "reason",
)
_RECORD_REFERENCES_PREFIX = "RECORD_REFERENCES#"
_AGENT_COMPENSATION_PREFIX = "AGENT_COMPENSATION#"


def _dynamo_numbers(value: Any) -> Any:
    """Convert nested floats to Decimal at the DynamoDB write boundary."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: _dynamo_numbers(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_dynamo_numbers(item) for item in value]
    return value


def _native_numbers(value: Any) -> Any:
    """DynamoDB가 돌려준 Decimal을 읽기 경계에서 native int/float로 되돌려요.

    boto3 resource Table은 **모든 숫자를 `Decimal`로** 돌려줘요. 그 값이 job의
    dict 필드(`memory_config.retention_days`, `limits` 등)에 그대로 실려 downstream
    으로 흐르면, registry descriptor를 만드는 `json.dumps`가
    `TypeError: Object of type Decimal is not JSON serializable`로 터져요
    (실측 2026-08-21, job agent-client-9b644c9158c23f52de976c0fbf6ec38d →
    `Agent 프로비저닝 실패: Object of type Decimal is not JSON serializable`).

    로컬 JSON 스토어는 `int`를 돌려주기 때문에 이 결함은 **DynamoDB 백엔드에서만**
    드러나요 — 타입을 바꾸는 스토어 경계는 실제 타입으로 테스트해야 해요.
    """
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: _native_numbers(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_native_numbers(item) for item in value]
    return value


class DynamoJobStore:
    def __init__(self, *, table_name, region, client=None) -> None:
        self._table_name = table_name
        self._region = region
        self._table = client

    def _tbl(self):
        if self._table is None:
            import boto3  # 지연 import
            self._table = boto3.resource(
                "dynamodb", region_name=self._region).Table(self._table_name)
        return self._table

    def put(self, job: DeployJob) -> None:
        item = job.to_dict()
        item["PK"] = f"JOB#{job.job_id}"
        # None 값은 DDB가 싫어하지 않지만, 깔끔하게 제거(빈 문자열은 유지).
        self._tbl().put_item(
            Item=_dynamo_numbers(
                {k: v for k, v in item.items() if v is not None}
            )
        )
        self._put_monitoring(job)

    def put_if_absent(self, job: DeployJob) -> bool:
        """Create a job without overwriting a concurrent deterministic job."""
        item = job.to_dict()
        item["PK"] = f"JOB#{job.job_id}"
        try:
            self._tbl().put_item(
                Item=_dynamo_numbers(
                    {k: v for k, v in item.items() if v is not None}
                ),
                ConditionExpression="attribute_not_exists(PK)",
            )
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                return False
            raise
        self._put_monitoring(job)
        return True

    def put_if_phase(
        self,
        job: DeployJob,
        expected_phase: DeployPhase,
        expected_revision: int,
    ) -> bool:
        """Store one step only while its read phase and revision are current."""
        item = job.to_dict()
        item["PK"] = f"JOB#{job.job_id}"
        try:
            self._tbl().put_item(
                Item=_dynamo_numbers(
                    {k: v for k, v in item.items() if v is not None}
                ),
                ConditionExpression=(
                    "#phase = :expected_phase AND "
                    "(#revision = :expected_revision OR "
                    "(attribute_not_exists(#revision) "
                    "AND :expected_revision = :zero))"
                ),
                ExpressionAttributeNames={
                    "#phase": "phase",
                    "#revision": "state_revision",
                },
                ExpressionAttributeValues={
                    ":expected_phase": expected_phase.value,
                    ":expected_revision": expected_revision,
                    ":zero": 0,
                },
            )
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                return False
            raise
        self._put_monitoring(job)
        return True

    def _put_monitoring(self, job: DeployJob) -> None:
        projected = monitoring_projection(job)
        if projected is None:
            return
        item = {
            "PK": f"MONITORING#AGENT#{projected.record_id}",
            **projected.to_dict(),
        }
        try:
            self._tbl().put_item(
                Item=_dynamo_numbers(
                    {
                        key: value
                        for key, value in item.items()
                        if value is not None
                    }
                ),
                ConditionExpression=(
                    "attribute_not_exists(PK) OR "
                    "attribute_not_exists(updated_at) OR updated_at <= :updated_at"
                ),
                ExpressionAttributeValues={":updated_at": projected.updated_at},
            )
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code != "ConditionalCheckFailedException":
                raise

    def get_monitoring(
        self,
        record_id: str,
    ) -> AgentMonitoringJobProjection | None:
        names = {f"#f{index}": field for index, field in enumerate(_MONITORING_FIELDS)}
        response = self._tbl().get_item(
            Key={"PK": f"MONITORING#AGENT#{record_id}"},
            ProjectionExpression=", ".join(names),
            ExpressionAttributeNames=names,
            ConsistentRead=True,
        )
        item = response.get("Item")
        return AgentMonitoringJobProjection.from_dict(item) if item else None

    def batch_get_monitoring(
        self,
        record_ids: list[str],
    ) -> dict[str, AgentMonitoringJobProjection]:
        """Read body-free latest jobs in bounded BatchGet requests."""
        unique_ids = list(dict.fromkeys(record_ids))
        if not unique_ids:
            return {}
        table = self._tbl()
        client = table.meta.client
        table_name = table.name
        names = {f"#f{index}": field for index, field in enumerate(_MONITORING_FIELDS)}
        projection = ", ".join(("#pk", *names))
        expression_names = {"#pk": "PK", **names}
        output: dict[str, AgentMonitoringJobProjection] = {}
        for offset in range(0, len(unique_ids), 100):
            pending = [
                {"PK": f"MONITORING#AGENT#{record_id}"}
                for record_id in unique_ids[offset : offset + 100]
            ]
            while pending:
                response = client.batch_get_item(
                    RequestItems={
                        table_name: {
                            "Keys": pending,
                            "ProjectionExpression": projection,
                            "ExpressionAttributeNames": expression_names,
                            "ConsistentRead": True,
                        }
                    }
                )
                for item in response.get("Responses", {}).get(table_name, []):
                    projected = AgentMonitoringJobProjection.from_dict(item)
                    output[projected.record_id] = projected
                pending = response.get("UnprocessedKeys", {}).get(
                    table_name, {}
                ).get("Keys", [])
        return output

    def get(self, job_id: str) -> DeployJob | None:
        resp = self._tbl().get_item(
            Key={"PK": f"JOB#{job_id}"},
            ConsistentRead=True,
        )
        item = resp.get("Item")
        if not item:
            return None
        item.pop("PK", None)
        return DeployJob.from_dict(_native_numbers(item))

    def list(self) -> list[DeployJob]:
        out: list[DeployJob] = []
        scan_args: dict = {"ConsistentRead": True}
        while True:
            response = self._tbl().scan(**scan_args)
            for raw_item in response.get("Items", []):
                # JOB# PK만 골라 왕복(같은 테이블에 REQ#도 살아서 필터 필요).
                if not raw_item.get("PK", "").startswith("JOB#"):
                    continue
                item = dict(raw_item)
                item.pop("PK", None)
                out.append(DeployJob.from_dict(_native_numbers(item)))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            scan_args["ExclusiveStartKey"] = last_key
        return out

    def initialize_job_reference_for_record(
        self,
        record_id: str,
        *,
        job_id: str,
    ) -> None:
        """새 DRAFT의 단일 참조 항목을 최초 job으로 생성해요."""
        self._tbl().put_item(
            Item={
                "PK": f"{_RECORD_REFERENCES_PREFIX}{record_id}",
                "record_id": record_id,
                "job_ids": {job_id},
            },
            ConditionExpression="attribute_not_exists(PK)",
        )

    def reserve_job_reference_for_record(
        self,
        record_id: str,
        *,
        job_id: str,
    ) -> bool:
        """기존 record 항목에 job을 원자적으로 추가하고 신규 추가 여부를 반환해요."""
        try:
            response = self._tbl().update_item(
                Key={"PK": f"{_RECORD_REFERENCES_PREFIX}{record_id}"},
                UpdateExpression="ADD #job_ids :job_ids",
                ConditionExpression="attribute_exists(PK)",
                ExpressionAttributeNames={"#job_ids": "job_ids"},
                ExpressionAttributeValues={":job_ids": {job_id}},
                ReturnValues="ALL_OLD",
            )
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                raise RuntimeError(
                    f"record reference registry is unavailable: {record_id}"
                ) from exc
            raise
        previous = response.get("Attributes") or {}
        return job_id not in set(previous.get("job_ids") or ())

    def release_job_reference_for_record(
        self,
        record_id: str,
        *,
        job_id: str,
    ) -> bool:
        """admission 실패 호출자가 추가한 자기 job ID만 조건부로 제거해요."""
        try:
            self._tbl().update_item(
                Key={"PK": f"{_RECORD_REFERENCES_PREFIX}{record_id}"},
                UpdateExpression="DELETE #job_ids :job_ids",
                ConditionExpression="contains(#job_ids, :job_id)",
                ExpressionAttributeNames={"#job_ids": "job_ids"},
                ExpressionAttributeValues={
                    ":job_ids": {job_id},
                    ":job_id": job_id,
                },
            )
            return True
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                return False
            raise

    def rollback_job_reference_initialization_for_record(
        self,
        record_id: str,
        *,
        job_id: str,
    ) -> bool:
        """초기화가 없었거나 자기 singleton일 때만 참조 항목을 되돌려요."""
        try:
            self._tbl().delete_item(
                Key={"PK": f"{_RECORD_REFERENCES_PREFIX}{record_id}"},
                ConditionExpression=(
                    "attribute_not_exists(PK) OR #job_ids = :job_ids"
                ),
                ExpressionAttributeNames={"#job_ids": "job_ids"},
                ExpressionAttributeValues={":job_ids": {job_id}},
            )
            return True
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                return False
            raise

    def claim_exclusive_job_reference_for_record(
        self,
        record_id: str,
        *,
        job_id: str,
    ) -> bool:
        """job 집합이 자신 하나일 때만 항목을 지워 배타성을 획득해요."""
        try:
            self._tbl().delete_item(
                Key={"PK": f"{_RECORD_REFERENCES_PREFIX}{record_id}"},
                ConditionExpression="#job_ids = :job_ids",
                ExpressionAttributeNames={"#job_ids": "job_ids"},
                ExpressionAttributeValues={":job_ids": {job_id}},
            )
            return True
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                return False
            raise

    def put_agent_compensation_target(
        self,
        target: AgentCompensationTarget,
    ) -> bool:
        """claim 전에 IA-64용 보상 좌표를 한 번만 만들어요."""
        try:
            self._tbl().put_item(
                Item={
                    "PK": f"{_AGENT_COMPENSATION_PREFIX}{target.job_id}",
                    "kind": "AGENT_COMPENSATION",
                    **target.to_dict(),
                },
                ConditionExpression="attribute_not_exists(PK)",
            )
            return True
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                return False
            raise

    def get_agent_compensation_target(
        self,
        job_id: str,
    ) -> AgentCompensationTarget | None:
        response = self._tbl().get_item(
            Key={"PK": f"{_AGENT_COMPENSATION_PREFIX}{job_id}"},
            ConsistentRead=True,
        )
        item = response.get("Item")
        return AgentCompensationTarget.from_dict(item) if item else None

    def list_agent_compensation_targets(
        self,
    ) -> list[AgentCompensationTarget]:
        targets: list[AgentCompensationTarget] = []
        scan_args: dict = {}
        while True:
            response = self._tbl().scan(**scan_args)
            for item in response.get("Items", []):
                if str(item.get("PK") or "").startswith(
                    _AGENT_COMPENSATION_PREFIX
                ):
                    targets.append(AgentCompensationTarget.from_dict(item))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            scan_args["ExclusiveStartKey"] = last_key
        return sorted(targets, key=lambda item: item.job_id)

    def update_agent_compensation_target(
        self,
        job_id: str,
        *,
        errors: tuple[str, ...],
        updated_at: str,
    ) -> bool:
        try:
            self._tbl().update_item(
                Key={"PK": f"{_AGENT_COMPENSATION_PREFIX}{job_id}"},
                UpdateExpression=(
                    "SET #errors = :errors, #updated_at = :updated_at"
                ),
                ConditionExpression="attribute_exists(PK)",
                ExpressionAttributeNames={
                    "#errors": "errors",
                    "#updated_at": "updated_at",
                },
                ExpressionAttributeValues={
                    ":errors": list(errors),
                    ":updated_at": updated_at,
                },
            )
            return True
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                return False
            raise

    def delete_agent_compensation_target(self, job_id: str) -> None:
        self._tbl().delete_item(
            Key={"PK": f"{_AGENT_COMPENSATION_PREFIX}{job_id}"}
        )

    def delete(self, job_id: str) -> None:
        """JOB#{job_id} 아이템을 삭제해요(하드 삭제 정리용). 없으면 no-op."""
        self._tbl().delete_item(Key={"PK": f"JOB#{job_id}"})

    def qualification_is_current(self, key: str, now: int) -> bool:
        """Read a replica-shared negative-control qualification.

        Verifier v4 qualifies no evidence and does not call this method.
        IH-48 will reuse it after an independent observation channel exists.
        """
        response = self._tbl().get_item(
            Key={"PK": f"QUALIFICATION#{key}"},
            ConsistentRead=True,
        )
        item = response.get("Item") or {}
        return int(item.get("expires_at") or 0) > now

    def put_qualification(self, key: str, expires_at: int) -> None:
        """Persist a qualification with the table's DynamoDB TTL attribute.

        Verifier v4 qualifies no evidence and does not call this method.
        IH-48 will reuse it after an independent observation channel exists.
        """
        self._tbl().put_item(
            Item=_dynamo_numbers(
                {
                    "PK": f"QUALIFICATION#{key}",
                    "kind": "AGENT_VERIFIER_NEGATIVE_CONTROL",
                    "expires_at": expires_at,
                }
            )
        )
