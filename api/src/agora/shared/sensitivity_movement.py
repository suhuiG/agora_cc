"""Typed contracts between catalog sensitivity sagas and runtime deployment."""

from __future__ import annotations

from typing import Literal, NotRequired, Protocol, TypedDict


class SensitivityMovementRequest(TypedDict):
    kind: Literal[
        "sensitivity_change",
        "legacy_split",
        "legacy_restore",
    ]
    request_id: str
    record_id: str
    record_version: str
    asset_name: str
    tool_name: str
    before: str | None
    after: str | None
    target_before: str | None
    target_after: str | None
    descriptors: dict
    previous_movement: dict


class SensitivityMovementResult(TypedDict):
    status: Literal["applied", "failed"]
    stage: str
    retryable: bool
    coordinates: dict
    error: NotRequired[str]
    mode: NotRequired[str]
    gateway_targets: NotRequired[list[dict]]
    unassigned_tools: NotRequired[list[dict]]


class SensitivityPropagator(Protocol):
    def __call__(
        self,
        request: SensitivityMovementRequest,
    ) -> SensitivityMovementResult: ...


class RedeploySensitivityRequest(TypedDict):
    record_id: str
    tools_inline: str
    actor: str


class RedeploySensitivityDecision(TypedDict):
    status: Literal[
        "ready",
        "pending_approval",
        "pending_propagation",
        "applying",
        "failed",
        "blocked",
        "unknown",
    ]
    reason: NotRequired[str]
    change: NotRequired[dict]
    changes: NotRequired[list[dict]]
    impact: NotRequired[list[dict]]
    tools_inline: NotRequired[str]


class RedeploySensitivityGuard(Protocol):
    def __call__(
        self,
        request: RedeploySensitivityRequest,
    ) -> RedeploySensitivityDecision: ...
