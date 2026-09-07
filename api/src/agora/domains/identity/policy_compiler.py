"""Compile structured identity resources into deterministic IAM policies."""
from __future__ import annotations

from collections.abc import Iterable

from .resource_ref import ResourceRef


def compile_policy(ref: ResourceRef, actions: Iterable[str]) -> dict | None:
    """Return an IAM policy for an AWS resource, or None when no grant applies."""
    resource = ref.to_arn()
    if resource is None:
        return None

    # Normalize surrounding whitespace only; stricter validation could reject valid IAM actions.
    normalized_actions = sorted(
        {action.strip() for action in actions if action.strip()}
    )
    if not normalized_actions:
        return None

    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": normalized_actions,
                "Resource": resource,
            }
        ],
    }
