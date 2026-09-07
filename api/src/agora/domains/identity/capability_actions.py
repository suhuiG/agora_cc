"""Map business capabilities to narrowly scoped IAM actions."""
from __future__ import annotations

from collections.abc import Iterable

_SUFFIX_ACTIONS: dict[str, tuple[str, ...]] = {
    "read": ("dynamodb:GetItem",),
}


def iam_actions_for(capabilities: Iterable[str]) -> list[str]:
    """Return deterministic IAM actions for explicitly supported capabilities."""
    actions: set[str] = set()
    for capability in capabilities:
        suffix = capability.rsplit(".", 1)[-1].strip().lower()
        actions.update(_SUFFIX_ACTIONS.get(suffix, ()))
    return sorted(actions)
