"""Connect MCP authorization cutover verdicts.

The expected sets and observed sets deliberately enter through separate fields:
Registry/identity ledgers own expectations, while Gateway and Policy Engine
read-backs own observations. Missing observations remain UNKNOWN.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CutoverVerdict(str, Enum):
    READY = "ready"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CutoverCheck:
    verdict: CutoverVerdict
    reasons: tuple[str, ...]

    @property
    def enforce_allowed(self) -> bool:
        return self.verdict is CutoverVerdict.READY


@dataclass(frozen=True)
class TargetInventoryObservation:
    recorded_targets: frozenset[tuple[str, str]]
    observed_deploy_targets: frozenset[tuple[str, str]]
    observed_m2_targets: frozenset[tuple[str, str]]
    registry_complete: bool
    deploy_gateway_complete: bool
    m2_gateway_complete: bool
    registry_targets_ready: bool = True


def assess_target_inventory(
    observation: TargetInventoryObservation,
) -> CutoverCheck:
    incomplete = tuple(
        name
        for name, complete in (
            ("registry_enumeration_incomplete", observation.registry_complete),
            (
                "deploy_gateway_enumeration_incomplete",
                observation.deploy_gateway_complete,
            ),
            ("m2_gateway_enumeration_incomplete", observation.m2_gateway_complete),
        )
        if not complete
    )
    if not observation.registry_targets_ready:
        incomplete += ("registry_targets_not_ready",)
    if incomplete:
        return CutoverCheck(CutoverVerdict.UNKNOWN, incomplete)

    observed = (
        observation.observed_deploy_targets
        | observation.observed_m2_targets
    )
    reasons: list[str] = []
    if observed - observation.recorded_targets:
        reasons.append("orphan_targets_present")
    if observation.recorded_targets - observed:
        reasons.append("recorded_targets_missing")
    if reasons:
        return CutoverCheck(CutoverVerdict.BLOCKED, tuple(reasons))
    return CutoverCheck(CutoverVerdict.READY, ())


@dataclass(frozen=True)
class PermitObservation:
    expected_permits_from_ledger: frozenset[tuple[str, str]]
    active_permits_from_policy_engine: frozenset[tuple[str, str]]
    expected_call_ids: frozenset[str]
    gateway_decision_call_ids: frozenset[str]
    policy_readback_complete: bool
    gateway_log_query_complete: bool
    ledger_read_complete: bool = True


def assess_permits(observation: PermitObservation) -> CutoverCheck:
    unknown: list[str] = []
    if not observation.ledger_read_complete:
        unknown.append("permit_ledger_read_incomplete")
    if not observation.policy_readback_complete:
        unknown.append("policy_readback_incomplete")
    if not observation.gateway_log_query_complete:
        unknown.append("gateway_log_query_incomplete")
    if not observation.expected_call_ids:
        unknown.append("expected_calls_absent")
    elif observation.expected_call_ids - observation.gateway_decision_call_ids:
        unknown.append("gateway_decision_datapoints_missing")
    if unknown:
        return CutoverCheck(CutoverVerdict.UNKNOWN, tuple(unknown))

    missing = (
        observation.expected_permits_from_ledger
        - observation.active_permits_from_policy_engine
    )
    unexpected = (
        observation.active_permits_from_policy_engine
        - observation.expected_permits_from_ledger
    )
    reasons: list[str] = []
    if missing:
        reasons.append("expected_active_permits_missing")
    if unexpected:
        reasons.append("unexpected_active_permits_present")
    if reasons:
        return CutoverCheck(CutoverVerdict.BLOCKED, tuple(reasons))
    return CutoverCheck(CutoverVerdict.READY, ())
