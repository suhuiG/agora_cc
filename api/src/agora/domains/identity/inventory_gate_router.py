"""인가 원장 inventory와 fail-closed 전환 승인 게이트."""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import hashlib
import json
from datetime import datetime, timezone
from typing import Literal
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field

from ...shared import deps
from ..governance.authz import require_role
from .agent_policy_deployer import PolicyInventoryReport
from .context import current_principal
from .models import (
    AuditEvent,
    AuthorizationOutcome,
    DecisionReason,
)

router = APIRouter(
    prefix="/api/admin/identity/fail-closed-authorization-gate",
    tags=["identity-admin"],
    dependencies=[Depends(require_role("admin"))],
)

_GATE_INVOCATION_ID = "fail-closed-unknown-authorization-gate"
_GATE_EVENT_TYPE = "FAIL_CLOSED_AUTHORIZATION_GATE_APPROVED"
_GATE_TARGET = "AGORA_RUNTIME_FAIL_CLOSED_ON_UNKNOWN_AUTHORIZATION"
_EVIDENCE_CHUNK_SIZE = 240_000
_EVIDENCE_V1_CHUNK_SIZE = 240_000


class GateObservationSnapshot(BaseModel):
    schema_version: Literal[1] = 1
    inventory: dict[str, object]
    inventory_status: str
    inventory_reason: str
    inventory_summary: dict[str, object]
    catalog_agent_ids: tuple[str, ...]
    unknown_agent_ids: tuple[str, ...]


class GateAssessment(BaseModel):
    state: Literal["not_applicable", "unknown", "observed"]
    observation_id: str
    target_agent_count: int | None
    evaluated_agent_count: int | None
    affected_agent_count: int | None
    affected_agent_ids: tuple[str, ...]
    reason: str
    can_approve: bool


class GateApprovalEvidence(BaseModel):
    gate_state: str
    target_agent_count: int
    evaluated_agent_count: int
    affected_agent_count: int
    affected_agent_ids: tuple[str, ...]
    inventory_summary: dict[str, object]
    observation_snapshot: GateObservationSnapshot


class GateApprovalEvidenceManifestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    storage: Literal["identity_store_chunks"] = "identity_store_chunks"
    encoding: Literal["base64"] = "base64"
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_count: int = Field(ge=1)
    chunk_count: int = Field(ge=1, le=4096)


class GateApprovalEvidenceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    storage: Literal["identity_store_chunks"] = "identity_store_chunks"
    encoding: Literal["base64"] = "base64"
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_count: int = Field(ge=1)
    chunk_size: int = Field(ge=1, le=350_000)
    chunk_count: int = Field(ge=1, le=4096)


class GateApproval(BaseModel):
    approved_by: str
    approved_at: str
    observation_id: str
    evidence: GateApprovalEvidence


class GateApprovalHistoryItem(BaseModel):
    state: Literal["observed", "unknown"]
    reason: str = ""
    approved_by: str
    approved_at: str
    observation_id: str


@dataclass(frozen=True)
class _GateApprovalRead:
    history: GateApprovalHistoryItem
    approval: GateApproval | None


class GateView(BaseModel):
    inventory: dict[str, object]
    gate: GateAssessment
    approval_observation: Literal["observed", "unknown"]
    approval_observation_reason: str = ""
    latest_approval: GateApproval | None
    approval_history: tuple[GateApprovalHistoryItem, ...]
    approved: bool


class GateApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation_id: str
    confirmation: Literal["approve_observed_inventory"]


def _inventory_payload(report: PolicyInventoryReport) -> dict[str, object]:
    inventory = jsonable_encoder(report)

    def sort_rows(field: str, key: str) -> list[dict[str, object]]:
        rows = inventory.get(field)
        if not isinstance(rows, list):
            return []
        rows.sort(key=lambda row: str(row.get(key, "")))
        return rows

    def sort_string_fields(
        rows: list[dict[str, object]],
        fields: tuple[str, ...],
    ) -> None:
        for row in rows:
            for field in fields:
                values = row.get(field)
                if (
                    isinstance(values, list)
                    and all(isinstance(value, str) for value in values)
                ):
                    row[field] = sorted(values)

    for policy_field in ("managed", "orphan", "unmanaged"):
        sort_rows(policy_field, "policy_id")
    reconciliation = sort_rows("reconciliation", "agent_record_id")
    sort_string_fields(
        reconciliation,
        ("in_sync", "stale", "drift", "missing"),
    )
    agents = sort_rows("agents", "record_id")
    sort_string_fields(
        agents,
        (
            "cedar_in_sync",
            "cedar_stale",
            "cedar_drift",
            "cedar_missing",
        ),
    )
    sort_rows("orphan_clients", "client_id")
    reclaim_candidates = sort_rows("reclaim_candidates", "record_id")
    sort_string_fields(reclaim_candidates, ("policy_ids",))
    return inventory


def _observation_snapshot(
    report: PolicyInventoryReport,
    catalog_agent_ids: tuple[str, ...] | None,
) -> GateObservationSnapshot:
    target_agent_ids = set(catalog_agent_ids or ())
    return GateObservationSnapshot(
        inventory=_inventory_payload(report),
        inventory_status=report.status,
        inventory_reason=report.reason,
        inventory_summary=jsonable_encoder(report.summary),
        catalog_agent_ids=tuple(sorted(catalog_agent_ids or ())),
        unknown_agent_ids=tuple(sorted(
            agent.record_id
            for agent in report.agents
            if (
                agent.record_id in target_agent_ids
                and agent.authorization_verdict == "unknown"
            )
        )),
    )


def _observation_id(snapshot: GateObservationSnapshot) -> str:
    canonical = json.dumps(
        jsonable_encoder(snapshot),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _observed_count(value: int | str) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _observed_distribution_count(
    value: dict[str, int] | str,
    state: str,
) -> int | None:
    if not isinstance(value, dict):
        return None
    counts = tuple(value.values())
    if any(
        not isinstance(count, int)
        or isinstance(count, bool)
        or count < 0
        for count in counts
    ):
        return None
    return value.get(state, 0)


def _assessment(
    report: PolicyInventoryReport,
    catalog_agent_ids: tuple[str, ...] | None,
) -> GateAssessment:
    snapshot = _observation_snapshot(report, catalog_agent_ids)
    observation_id = _observation_id(snapshot)
    if report.status != "observed" or catalog_agent_ids is None:
        return GateAssessment(
            state="unknown",
            observation_id=observation_id,
            target_agent_count=None,
            evaluated_agent_count=None,
            affected_agent_count=None,
            affected_agent_ids=(),
            reason=report.reason or "catalog_agent_inventory_unobservable",
            can_approve=False,
        )

    catalog_agent_ids = snapshot.catalog_agent_ids
    identity_count = _observed_count(report.summary.identity_total)
    evaluated_agent_count = _observed_count(
        report.summary.gate_evaluated_agent_count
    )
    affected_agent_count = _observed_count(
        report.summary.gate_unknown_agent_count
    )
    active_tool_count = _observed_count(
        report.summary.gate_active_tool_count
    )
    if (
        identity_count is None
        or evaluated_agent_count is None
        or affected_agent_count is None
        or active_tool_count is None
    ):
        return GateAssessment(
            state="unknown",
            observation_id=observation_id,
            target_agent_count=None,
            evaluated_agent_count=None,
            affected_agent_count=None,
            affected_agent_ids=(),
            reason="inventory_summary_unobservable",
            can_approve=False,
        )

    if not catalog_agent_ids:
        return GateAssessment(
            state="not_applicable",
            observation_id=observation_id,
            target_agent_count=0,
            evaluated_agent_count=0,
            affected_agent_count=0,
            affected_agent_ids=(),
            reason="authorization_target_agents_empty",
            can_approve=False,
        )

    inventory_agent_ids = {agent.record_id for agent in report.agents}
    if set(catalog_agent_ids) - inventory_agent_ids:
        return GateAssessment(
            state="unknown",
            observation_id=observation_id,
            target_agent_count=len(catalog_agent_ids),
            evaluated_agent_count=evaluated_agent_count,
            affected_agent_count=None,
            affected_agent_ids=(),
            reason="registry_agent_inventory_incomplete",
            can_approve=False,
        )

    if active_tool_count == 0:
        return GateAssessment(
            state="unknown",
            observation_id=observation_id,
            target_agent_count=len(catalog_agent_ids),
            evaluated_agent_count=evaluated_agent_count,
            affected_agent_count=affected_agent_count,
            affected_agent_ids=snapshot.unknown_agent_ids,
            reason="ih48_negative_control_unqualified",
            can_approve=False,
        )

    affected_agent_ids = snapshot.unknown_agent_ids
    return GateAssessment(
        state="observed",
        observation_id=observation_id,
        target_agent_count=len(catalog_agent_ids),
        evaluated_agent_count=evaluated_agent_count,
        affected_agent_count=affected_agent_count,
        affected_agent_ids=affected_agent_ids,
        reason=(
            "affected_agents_observed"
            if affected_agent_count
            else "no_affected_agents_observed"
        ),
        can_approve=True,
    )


def _evidence_manifest_and_chunks(
    evidence: GateApprovalEvidence,
) -> tuple[GateApprovalEvidenceManifest, tuple[str, ...]]:
    raw = evidence.model_dump_json().encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    encoded = base64.b64encode(raw).decode("ascii")
    chunks = tuple(
        encoded[offset:offset + _EVIDENCE_CHUNK_SIZE]
        for offset in range(0, len(encoded), _EVIDENCE_CHUNK_SIZE)
    )
    return (
        GateApprovalEvidenceManifest(
            sha256=digest,
            byte_count=len(raw),
            chunk_size=_EVIDENCE_CHUNK_SIZE,
            chunk_count=len(chunks),
        ),
        chunks,
    )


def _approval_from_event(event: AuditEvent, store) -> GateApproval:
    if not event.validation_findings:
        raise ValueError("gate approval evidence is missing")
    finding = event.validation_findings[0]
    payload = json.loads(finding)
    if not isinstance(payload, dict):
        raise ValueError("gate approval evidence must be an object")
    if payload.get("storage") == "identity_store_chunks":
        if payload.get("schema_version") == 1:
            manifest = GateApprovalEvidenceManifestV1.model_validate(
                payload
            )
            chunk_size = _EVIDENCE_V1_CHUNK_SIZE
        else:
            manifest = GateApprovalEvidenceManifest.model_validate(payload)
            chunk_size = manifest.chunk_size
        encoded_size = 4 * ((manifest.byte_count + 2) // 3)
        expected_chunk_count = (
            encoded_size + chunk_size - 1
        ) // chunk_size
        if manifest.chunk_count != expected_chunk_count:
            raise ValueError("gate approval evidence chunk count changed")
        chunks = store.get_audit_evidence_chunks(
            event.invocation_id,
            manifest.sha256,
            manifest.chunk_count,
        )
        for index, chunk in enumerate(chunks):
            expected_size = min(
                chunk_size,
                encoded_size - (index * chunk_size),
            )
            if len(chunk) != expected_size:
                raise ValueError(
                    "gate approval evidence chunk size changed"
                )
        try:
            raw = base64.b64decode("".join(chunks), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(
                "gate approval evidence encoding is invalid"
            ) from exc
        if len(raw) != manifest.byte_count:
            raise ValueError("gate approval evidence size changed")
        if hashlib.sha256(raw).hexdigest() != manifest.sha256:
            raise ValueError("gate approval evidence digest changed")
        evidence = GateApprovalEvidence.model_validate_json(raw)
    else:
        evidence = GateApprovalEvidence.model_validate(payload)
    if (
        _observation_id(evidence.observation_snapshot)
        != event.policy_hash
    ):
        raise ValueError(
            "gate approval evidence does not match its observation"
        )
    return GateApproval(
        approved_by=event.principal_id,
        approved_at=event.timestamp,
        observation_id=event.policy_hash,
        evidence=evidence,
    )


def _approvals() -> tuple[_GateApprovalRead, ...]:
    store = deps.get_identity_store()
    approvals: list[_GateApprovalRead] = []
    for read in store.list_audit_reads(_GATE_INVOCATION_ID):
        event = read.event
        if event is None:
            if read.event_type and read.event_type != _GATE_EVENT_TYPE:
                continue
            approvals.append(_GateApprovalRead(
                history=GateApprovalHistoryItem(
                    state="unknown",
                    reason=f"audit event unreadable: {read.error}",
                    approved_by=read.principal_id,
                    approved_at=read.timestamp,
                    observation_id=read.policy_hash,
                ),
                approval=None,
            ))
            continue
        if event.event_type != _GATE_EVENT_TYPE:
            continue
        try:
            approval = _approval_from_event(event, store)
        except Exception as exc:  # noqa: BLE001 - 손상은 해당 승인에만 격리해요.
            approvals.append(_GateApprovalRead(
                history=GateApprovalHistoryItem(
                    state="unknown",
                    reason=f"{type(exc).__name__}: {exc}",
                    approved_by=event.principal_id,
                    approved_at=event.timestamp,
                    observation_id=event.policy_hash,
                ),
                approval=None,
            ))
            continue
        approvals.append(_GateApprovalRead(
            history=GateApprovalHistoryItem(
                state="observed",
                approved_by=approval.approved_by,
                approved_at=approval.approved_at,
                observation_id=approval.observation_id,
            ),
            approval=approval,
        ))
    return tuple(approvals)


def _view(
    report: PolicyInventoryReport,
    catalog_agent_ids: tuple[str, ...] | None,
) -> GateView:
    gate = _assessment(report, catalog_agent_ids)
    try:
        approvals = _approvals()
    except Exception as exc:  # noqa: BLE001 - 미관측 승인을 통과로 접지 않아요.
        gate = gate.model_copy(update={
            "state": "unknown",
            "affected_agent_count": None,
            "affected_agent_ids": (),
            "reason": "approval_ledger_unobservable",
            "can_approve": False,
        })
        return GateView(
            inventory=_inventory_payload(report),
            gate=gate,
            approval_observation="unknown",
            approval_observation_reason=(
                f"{type(exc).__name__}: {exc}"
            ),
            latest_approval=None,
            approval_history=(),
            approved=False,
        )
    observed_approvals = tuple(
        item.approval
        for item in approvals
        if item.approval is not None
    )
    matching_approvals = tuple(
        approval
        for approval in observed_approvals
        if approval.observation_id == gate.observation_id
    )
    current_approval = (
        matching_approvals[-1] if matching_approvals else None
    )
    latest_approval = (
        current_approval
        or (observed_approvals[-1] if observed_approvals else None)
    )
    return GateView(
        inventory=_inventory_payload(report),
        gate=gate,
        approval_observation="observed",
        latest_approval=latest_approval,
        approval_history=tuple(item.history for item in approvals),
        approved=bool(
            gate.can_approve
            and current_approval is not None
        ),
    )


def _approval_evidence(
    report: PolicyInventoryReport,
    gate: GateAssessment,
    catalog_agent_ids: tuple[str, ...] | None,
) -> GateApprovalEvidence:
    snapshot = _observation_snapshot(report, catalog_agent_ids)
    return GateApprovalEvidence(
        gate_state=gate.state,
        target_agent_count=gate.target_agent_count or 0,
        evaluated_agent_count=gate.evaluated_agent_count or 0,
        affected_agent_count=gate.affected_agent_count or 0,
        affected_agent_ids=gate.affected_agent_ids,
        inventory_summary=jsonable_encoder(report.summary),
        observation_snapshot=snapshot,
    )


@router.get("", response_model=GateView)
def get_gate():
    """한 inventory snapshot과 그 snapshot에 대한 승인 상태를 반환해요."""
    report, catalog_agent_ids = (
        deps.observe_agent_policy_inventory_with_catalog_agents()
    )
    return _view(report, catalog_agent_ids)


@router.post("/approve", response_model=GateView)
def approve_gate(body: GateApprovalRequest, request: Request):
    """현재 관측 snapshot만 승인하고 실제 fail-closed 전환은 수행하지 않아요."""
    report, catalog_agent_ids = (
        deps.observe_agent_policy_inventory_with_catalog_agents()
    )
    current = _view(report, catalog_agent_ids)
    gate = current.gate
    if current.approval_observation != "observed":
        raise HTTPException(
            409,
            "승인 원장을 관측할 수 없어 게이트를 승인할 수 없어요.",
        )
    if not gate.can_approve:
        detail = "관측 결과가 unknown이라 승인할 수 없어요."
        if gate.reason == "ih48_negative_control_unqualified":
            detail = "IH-48 독립 관측 자격이 없어 승인할 수 없어요."
        elif gate.reason == "registry_agent_inventory_incomplete":
            detail = (
                "인가 대상 Runtime Agent 평가가 완전하지 않아 "
                "승인할 수 없어요."
            )
        raise HTTPException(
            409,
            detail,
        )
    if body.observation_id != gate.observation_id:
        raise HTTPException(
            409,
            "관측 결과가 변경됐어요. 새 inventory를 확인한 뒤 승인하세요.",
        )
    if current.approved:
        return current

    evidence = _approval_evidence(report, gate, catalog_agent_ids)
    manifest, chunks = _evidence_manifest_and_chunks(evidence)
    timestamp = datetime.now(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")
    principal_id = current_principal(request).principal_id
    store = deps.get_identity_store()
    try:
        store.put_audit_evidence_chunks(
            _GATE_INVOCATION_ID,
            manifest.sha256,
            chunks,
        )
        store.append_audit_once(AuditEvent(
            event_id=str(uuid.uuid4()),
            invocation_id=_GATE_INVOCATION_ID,
            principal_id=principal_id,
            agent_id="fleet",
            asset_id="",
            operation_id="approve_fail_closed_transition_gate",
            connection_id="",
            capabilities=(),
            decision=AuthorizationOutcome.ALLOW,
            reason=DecisionReason.ALLOWED,
            target=_GATE_TARGET,
            timestamp=timestamp,
            workload_id="",
            event_type=_GATE_EVENT_TYPE,
            mode="GATE_APPROVAL",
            policy_deployment_outcome="GATE_APPROVED",
            policy_revision=gate.affected_agent_count or 0,
            policy_hash=gate.observation_id,
            validation_findings=(manifest.model_dump_json(),),
            request_justification=body.confirmation,
        ), idempotency_key=gate.observation_id)
    except Exception as exc:  # noqa: BLE001 - 근거 없는 승인을 남기지 않아요.
        raise HTTPException(
            503,
            "승인 근거 저장에 실패해 승인을 기록하지 않았어요.",
        ) from exc
    return _view(report, catalog_agent_ids)
